"""The drop engine. The only code that moves money. No AI is in here.

A drop: draft -> live -> closed -> settled. Buyers join by authorising a hold at the START price; the
price for everyone falls as the crowd crosses ladder steps, and a reached step is locked. At settlement,
if the minimum was met every hold is captured at the final price (a final capture: the rest of each
hold is released), otherwise every hold is voided. Settlement is idempotent and resumable.

What the engine promises, and where it is enforced:
- nobody is charged twice or above the final price: deterministic PayPal request ids, one final capture per hold;
- a missed minimum charges nobody: `settle` only voids then;
- no hold is left open after settle: a join is written down before PayPal is asked and `reconcile` (run by `close`
  and `settle`) follows the trail of any join whose answer was lost; a refused release is recorded as `void_failed`;
- settle is resumable after any crash or outage, and a PayPal refusal about ONE buyer (an allow-list of names) never
  stops it for the rest, while any other refusal (the account, the request, a name nobody has seen) stops it for a person;
- settlement runs only for the plan a person approved (`settle(expect=...)` re-derives it under the lock);
- PayPal is asked, not guessed at: a hold counts toward the crowd only if PayPal still shows it open when the drop closes,
  a hold is called released only if PayPal says VOIDED or EXPIRED (or a void succeeded), and a hold is called expired
  only if PayPal says so, never because our clock passed the date it gave;
- a drop belongs to the PayPal it was created with: every operation refuses (`wrong_paypal`, nothing written) when the PayPal
  in use now is not the one the drop's holds were made with;
- PayPal is asked again right before money moves: `settle` reads every remaining hold (and the captures on its order), records
  what it finds, and refuses a plan that no longer matches what PayPal shows (`plan_changed`);
- nobody who saw an error is charged (D-11, D-11b): a hold the engine never confirmed is released, and a leave asked for before
  the deadline is honoured even if PayPal errored;
- `verify` (verify.py) reads everything back from PayPal and reports any mismatch.

Every guard raises CrowdError with a stable `code`; the Togi bundle turns codes into messages.
"""
from __future__ import annotations

import inspect
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from . import ladder as lad
from .paypal_holds import (DECLINE_CARD_INDEX, AuthorizationAlreadyCaptured, AuthorizationExpired, AuthorizationVoided,
                           CardDeclined, HoldError, HoldNotFound, PayPalRefused, PayPalUnavailable, buyer_specific, get_card,
                           make_holds, mode_of, ref_of, request_id_for)
from .store import Buyer, Drop, Store, StoreError, iso, parse_iso, parse_utc, slug

MAX_DURATION_MINUTES = 72 * 60  # PayPal only promises a hold for its 3-day honour period
MAX_BUYER_NAME = 60
OPEN_AUTH = ("CREATED", "PENDING")   # PayPal authorisation statuses that still hold money on a card
GONE_AUTH = ("VOIDED", "EXPIRED")    # the only statuses in which the engine may call a hold released without voiding it
TAKEN_AUTH = ("CAPTURED", "PARTIALLY_CAPTURED")   # money moved
NO_MONEY_CAPTURE = ("DECLINED", "FAILED", "REFUNDED")   # capture statuses in which nothing is (any longer) taken from the buyer
BREAKER_LIMIT = 2                    # this many capture failures in a row, of any buyer-specific codes, stop settlement
BREAKER_RATE_MIN = 4                 # ... and so does more than half of the captures failing once this many were attempted
BREAKER_EXEMPT = ("hold_expired", "authorization_expired")   # a fact about that one hold (PayPal says it lapsed), not a shared cause


class CrowdError(Exception):
    """A guard said no, or something failed. `code` is a stable slug; `ctx` names the specifics."""

    def __init__(self, code: str, message: str = "", **ctx):
        super().__init__(message or code)
        self.code, self.ctx = code, {k: v for k, v in ctx.items() if v is not None}


def m(x) -> str:
    """Money as text with cents: '33.00'. Rounds half up, exactly as the PayPal client does when it writes the amount."""
    return f"{lad.money(x):.2f}"


def _accepts(fn, name: str) -> bool:
    """Does this callable take a keyword argument called `name`? (A holds wrapper written before `on_order` existed
    does not, and must keep working.)"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def plan_token(p: dict) -> str:
    """A plan as a short token, e.g. capture:52:33.00:1716.00 or void:22, with +release:K when K failed holds are still
    open and settling will release them. A person's approval is bound to it."""
    if p.get("already_settled"):
        return "settled"
    base = f"capture:{p['holds']}:{p['price']}:{p['total']}" if p["action"] == "capture" else f"void:{p['holds']}"
    return base + (f"+release:{p['releases']}" if p.get("releases") else "")


def foreign_holds(store: Store, ref: str | None) -> list[dict]:
    """Holds (and started joins) in the store that were not made by the fake PayPal identified by `ref`: made in the
    sandbox, by some other fake, or by a version that did not write down which PayPal it used. Anything that cannot be
    shown to be this fake's counts as foreign, so test tooling never touches money that might be real."""
    out = []
    for e in store.events():
        t, p = e["type"], e["payload"]
        if t in ("hold_placed", "join_started") or p.get("paypal_mode") is not None:
            mode = p.get("paypal_mode")
            if mode != "fake" or p.get("paypal_ref") != ref:
                out.append({"drop": e["drop_id"], "buyer": p.get("buyer"), "mode": mode or "unknown"})
    return out


class _Unreadable(Exception):
    """PayPal answered a read of this buyer's hold with a refusal that is not 'not found' (403 and the like): it will not say."""

    def __init__(self, buyer: Buyer, err: HoldError):
        super().__init__(str(err))
        self.buyer, self.err = buyer, err


def _manual_exit(drop: str, b: Buyer, again: str) -> str:
    """The exact ways past one buyer's hold that PayPal will not let the engine read or release."""
    return (f"A person (the maker, who owns the PayPal account) looks at hold {b.auth_id} in PayPal and why it cannot be read. "
            f"To go on without it: void hold {b.auth_id} at PayPal by hand, or run `togi crowd drop exclude --drop {drop} "
            f"--buyer {b.id}` to release it and leave {b.id} out of the crowd. Then run `{again}`.")


def _describe_paypal(mode: str, ref: str | None) -> str:
    """'fake 3fa9c1' / 'sandbox app 9ab12c' for a message; never anything secret (a ref is a short hash)."""
    return f"{mode} {'app ' if mode == 'sandbox' and ref else ''}{ref or ''}".strip()


def dollars(x) -> str:
    """'$1,716.00' for people-facing summaries."""
    return f"${lad.D(x):,.2f}"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def human_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    h, rem = divmod(seconds, 3600)
    mins = rem // 60
    return f"{h}h {mins:02d}m" if h else f"{mins}m"


class Engine:
    def __init__(self, store: Store | None = None, holds=None, clock=None):
        self.store = store or Store()
        self.holds = holds if holds is not None else make_holds(self.store.path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        return self._clock()

    # ---- one store, one PayPal -------------------------------------------------------------------

    def paypal_key(self) -> tuple[str, str | None]:
        """Which PayPal this engine talks to: ('fake', the fake's ref), ('sandbox', the app's client-id fingerprint)."""
        return mode_of(self.holds), ref_of(self.holds)

    def recorded_paypals(self, drop: str | None = None) -> set[tuple[str, str | None]]:
        """Every PayPal the log says a drop (or, with none given, any drop in the store) was made with or acted against: the
        `paypal_mode` / `paypal_ref` that `drop_created` and every event that records something PayPal did carry."""
        return {(e["payload"]["paypal_mode"], e["payload"].get("paypal_ref"))
                for e in self.store.events(drop) if e["payload"].get("paypal_mode") is not None}

    def _same_paypal(self, recorded: tuple[str, str | None]) -> bool:
        """Is the PayPal in use the one that was recorded? A mode or a ref that differs says no. A recorded 'unknown' (a
        wrapper that would not say) or a recorded sandbox with no ref (written before refs existed) cannot be compared and
        is let through; an engine that cannot say what it is (mode 'unknown') only ever exists in tests."""
        mode, ref = self.paypal_key()
        r_mode, r_ref = recorded
        if "unknown" in (mode, r_mode):
            return True
        return r_mode == mode and (r_ref is None or r_ref == ref)

    def require_paypal(self, drop: str | None = None) -> None:
        """Refuse (`wrong_paypal`, nothing written) unless the PayPal in use is the one this drop's holds were made with. A
        process pointed at another PayPal (a fake on a sandbox store, another sandbox app) reads every real hold as missing
        and would close, price and settle the drop on a crowd that PayPal never had. With no drop given the whole store is
        checked: a store belongs to one PayPal from the first drop made in it."""
        for rec in sorted(self.recorded_paypals(drop), key=str):
            if not self._same_paypal(rec):
                mode, ref = self.paypal_key()
                raise CrowdError("wrong_paypal", drop=drop or "the store", recorded=_describe_paypal(*rec),
                                 current=_describe_paypal(mode, ref), recorded_mode=rec[0])

    # ---- reading ---------------------------------------------------------------------------------

    def get(self, drop: str) -> Drop:
        try:
            return self.store.project(str(drop))
        except StoreError as e:
            if e.code == "drop_not_found":
                raise CrowdError("drop_not_found", drop=str(drop)) from None
            raise

    def display_state(self, d: Drop) -> str:
        if d.state == "live" and d.ends_at and self.now() >= parse_iso(d.ends_at):
            return "ended"  # the clock ran out; joining has stopped, the maker still has to close it
        return d.state

    def view(self, d: Drop) -> dict:
        """Status of a drop from engine numbers only."""
        n = d.count
        nxt = lad.next_step(d.ladder, n, d.locked_step)
        out = {"drop": d.id, "title": d.title, "state": self.display_state(d), "currency": d.currency,
               "price": m(d.price()), "start_price": m(d.ladder.start_price), "buyers": n, "minimum": d.minimum,
               "minimum_met": n >= d.minimum,
               "locked_step": d.locked_step if d.locked_step >= 0 else None,
               "locked_price": m(d.ladder.steps[d.locked_step].price) if d.locked_step >= 0 else None,
               "next_step": None, "ladder": d.ladder.spec()}
        if nxt:
            out["next_step"] = {"more": nxt["more"], "price": m(nxt["price"]), "at_buyers": nxt["min_buyers"]}
            out["next_message"] = self.next_message(d, n)
        if d.ends_at:
            left = int((parse_iso(d.ends_at) - self.now()).total_seconds())
            out["ends_at"] = d.ends_at
            out["time_left"] = human_duration(left) if d.state == "live" else None
            out["time_left_seconds"] = max(left, 0) if d.state == "live" else None
        if d.settled:
            out["outcome"] = d.settled["outcome"]
        if d.unresolved:  # a join PayPal may have acted on, never confirmed: close/settle sort it out
            out["unresolved_joins"] = len(d.unresolved)
        if d.leaving:     # a leave PayPal may have acted on, never confirmed: close/settle read the hold and sort it out
            out["unresolved_leaves"] = len(d.leaving)
        if d.blocked:     # settlement stopped for a person
            out["settle_blocked"] = {k: d.blocked.get(k) for k in ("issue", "http", "debug_id", "phase", "buyer", "at")
                                     if d.blocked.get(k) is not None}
        return out

    def next_message(self, d: Drop, n: int) -> str | None:
        nxt = lad.next_step(d.ladder, n, d.locked_step)
        if not nxt:
            return None
        if nxt["step"] == 0 and n < d.minimum:
            return f"{plural(nxt['more'], 'more buyer')} to reach the minimum of {d.minimum}"
        return f"{plural(nxt['more'], 'more buyer')} and the price drops to {dollars(nxt['price'])} for everyone"

    def list(self) -> list[dict]:
        """Every drop's status, newest first."""
        return [self.view(self.get(i)) for i in reversed(self.store.drop_ids())]

    def buyers(self, drop: str) -> list[dict]:
        d = self.get(drop)
        return [{"buyer": b.id, "name": b.name, "status": b.status, "card": b.card_index,
                 "held": m(b.hold_amount) if b.hold_amount is not None and b.status in ("held",) else None,
                 "charged": m(b.captured) if b.captured is not None else None,
                 "capture_id": b.capture_id, "auth_id": b.auth_id, "error": b.error} for b in d.buyers.values()]

    # ---- drop lifecycle ----------------------------------------------------------------------------

    def create_drop(self, title: str, ladder, minimum: int, duration_minutes: int, currency: str = "USD",
                    maker: str = "maker", costs: dict | None = None, drop_id: str | None = None) -> str:
        self.require_paypal()          # a store belongs to one PayPal: a drop is never made in another PayPal's store
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not title or len(title) > 120 or any(ord(c) < 32 for c in title):
            raise CrowdError("bad_title", "a title of 1 to 120 characters is needed")
        currency = str(currency or "").upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CrowdError("bad_currency", currency=currency)
        try:
            lg = ladder if isinstance(ladder, lad.Ladder) else lad.parse(ladder)
            if int(minimum) != lg.minimum:
                raise CrowdError("minimum_mismatch", minimum=int(minimum), first_step=lg.minimum)
            if costs:
                lad.validate(lg, costs["unit_cost"], costs["fixed_cost"],
                             lad.FeeModel.from_dict(costs["fee"]) if costs.get("fee") else lad.DEFAULT_FEE)
        except lad.LadderError as e:
            raise CrowdError(e.code, str(e), **{k: str(v) for k, v in e.detail.items()}) from None
        if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int) or duration_minutes < 1:
            raise CrowdError("duration_invalid", minutes=str(duration_minutes))
        if duration_minutes > MAX_DURATION_MINUTES:
            raise CrowdError("duration_too_long", minutes=duration_minutes, max_minutes=MAX_DURATION_MINUTES)
        payload_costs = None
        if costs:
            payload_costs = {"unit_cost": m(costs["unit_cost"]), "fixed_cost": m(costs["fixed_cost"])}
        if drop_id is not None:  # a chosen id (demos, tests): short, plain, and never reused
            if not re.fullmatch(r"d_[a-z0-9]{2,16}", str(drop_id)):
                raise CrowdError("bad_drop_id", drop=str(drop_id)[:40])
            if self.store.exists(drop_id):
                raise CrowdError("drop_exists", drop=drop_id)
        else:
            for _ in range(5):
                drop_id = "d_" + uuid4().hex[:8]
                if not self.store.exists(drop_id):
                    break
        self.store.append(drop_id, "drop_created", {
            "title": title, "maker": str(maker)[:80], "currency": currency, "minimum": lg.minimum,
            "duration_minutes": duration_minutes, "ladder": lg.to_dict(), "costs": payload_costs,
            **self._evidence()}, ts=self.now())         # the PayPal this drop (and so its holds) belongs to
        return drop_id

    def publish(self, drop: str) -> dict:
        with self.store.lock(drop):
            d = self.get(drop)
            self.require_paypal(d.id)
            if d.state != "draft":
                raise CrowdError("not_draft", drop=d.id, state=self.display_state(d))
            now = self.now()
            ends = now + timedelta(minutes=d.duration_minutes)
            self.store.append(d.id, "drop_published", {"ends_at": iso(ends)}, ts=now)
            return self.view(self.get(drop))

    # ---- evidence ------------------------------------------------------------------------------------

    def _evidence(self, e: HoldError | None = None) -> dict:
        """Which PayPal an event's facts came from, and (for a failure) what PayPal itself said: its error name,
        http status and debug id. Written onto every event that records something PayPal did or refused."""
        out = {"paypal_mode": mode_of(self.holds)}
        if ref_of(self.holds):
            out["paypal_ref"] = ref_of(self.holds)
        if e is not None:
            out.update({"issue": e.issue or e.code, "http": e.http, "debug_id": e.debug_id})
        return {k: v for k, v in out.items() if v is not None}

    def _joinable(self, d: Drop, now: datetime | None = None) -> bool:
        now = now or self.now()
        return d.state == "live" and not (d.ends_at and now >= parse_iso(d.ends_at))

    # ---- joining and leaving -------------------------------------------------------------------------

    @staticmethod
    def _new_out() -> dict:
        return {"checked": 0, "adopted": [], "released": [], "unconfirmed": [], "leaves_completed": [], "leaves_kept": []}

    def join(self, drop: str, buyer_name: str, card_index: int = 0) -> dict:
        """Hold the start price on the buyer's card. The join is written down BEFORE PayPal is asked (`join_started`),
        and the order the moment it exists (`order_created`), so a reply that never arrives, or a crash after PayPal
        acted, leaves a trail `reconcile` follows instead of a hold nobody knows about.

        The clock is read ONCE, when the request is taken: that instant decides whether the drop still takes joins and
        stamps every event of the join, so a slow PayPal can never put half a join on each side of the deadline.

        A hold that came back from a replay (the buyer is trying again after a lost reply), or in any status but open, is
        never trusted as it arrived: PayPal is asked for its current status before the hold is recorded."""
        with self.store.lock(drop):
            asked = self.now()
            d = self.get(drop)
            self.require_paypal(d.id)
            self._require_joinable(d, asked)
            name = re.sub(r"\s+", " ", str(buyer_name or "")).strip()
            bid = slug(name)
            if not bid or len(name) > MAX_BUYER_NAME or any(ord(c) < 32 for c in name):
                raise CrowdError("bad_buyer", "a buyer name of 1 to 60 characters is needed")
            b = d.buyers.get(bid)
            if b and b.excluded:
                raise CrowdError("buyer_excluded", drop=d.id, buyer=bid)
            if b and b.status == "held":
                raise CrowdError("already_joined", drop=d.id, buyer=bid)
            try:
                card = get_card(card_index)
            except HoldError:
                raise CrowdError("bad_card", card=str(card_index)) from None
            d = self._release_other_cards(d, bid, card_index)     # an unconfirmed try on another card is released first
            b = d.buyers.get(bid)
            attempt = (b.attempts if b else 0) + 1
            rid = request_id_for(d.id, bid, "authorize", attempt)
            earlier = [r for r, j in d.unresolved.items() if j.get("buyer") == bid]   # tries PayPal may have acted on
            prior_order = (d.unresolved.get(rid) or {}).get("order_id")   # the order an earlier ask of THIS request made
            self.store.append(d.id, "join_started", {
                "buyer": bid, "name": name, "card_index": card_index, "attempt": attempt, "request_id": rid,
                "request_ids": [rid, rid + "-authorize"], "amount": m(d.ladder.start_price), "currency": d.currency,
                **self._evidence()}, ts=asked)
            noted: dict = {}
            try:
                hold = self._authorize(d, bid, rid, card, noted, asked)
            except CardDeclined as e:
                self._settle_earlier(d.id, earlier)      # a refusal now says nothing about the hold an earlier try left
                self._join_failed(d, bid, name, card_index, rid, e, asked)
                raise CrowdError("join_declined", drop=d.id, buyer=bid, card=card_index) from None
            except PayPalUnavailable as e:    # the join stays open in the log: reconcile finds out what PayPal did
                self._note_order(d, bid, rid, e, noted, asked)
                raise CrowdError("paypal_unavailable") from None
            except PayPalRefused as e:        # PayPal read it and said no
                self._settle_earlier(d.id, earlier)
                self._join_failed(d, bid, name, card_index, rid, e, asked)
                raise CrowdError("paypal_error", issue=e.issue or e.code, http=e.http) from None
            except HoldError as e:
                self._note_order(d, bid, rid, e, noted, asked)
                raise CrowdError("paypal_error", issue=e.issue or e.code, http=e.http) from None
            d = self.get(drop)
            # PayPal answered an ask it had already seen with a DIFFERENT order (it had forgotten the request id): this is a
            # new hold, not a replay of the one the lost reply left behind. That earlier order's hold is released first
            # (D-11), the new one is confirmed with a GET, and it is never called `replayed` (the order id does not match).
            superseded = bool(prior_order) and hold.get("order_id") != prior_order
            replayed = (bool(hold.get("replayed")) or rid in earlier) and not superseded
            if superseded:
                self._release_stale_orders(d, bid, rid, hold.get("order_id"))
                d = self.get(drop)
            if replayed or superseded or hold.get("status") not in OPEN_AUTH:
                hold = self._confirm_hold(d, bid, rid, hold)
            extra = {"replayed": True, "confirmed_status": hold.get("status")} if replayed else (
                {"supersedes_order": prior_order, "confirmed_status": hold.get("status")} if superseded else {})
            items = self._hold_items(d, bid, name, card_index, hold, attempt, rid, **extra)
            self.store.append_many(d.id, items, ts=asked)
            locks = [p["step"] for t, p in items if t == "step_locked"]
            d = self.get(drop)
            out = self.view(d)
            out.update({"buyer": bid, "name": name, "hold": m(d.ladder.start_price), "auth_id": hold["auth_id"],
                        "step_locked": locks[-1] if locks else None})
            return out

    def _authorize(self, d: Drop, bid: str, rid: str, card: dict, noted: dict, asked: datetime) -> dict:
        kw: dict = {"card": card}
        if _accepts(self.holds.authorize, "on_order"):
            def on_order(order_id: str) -> None:       # the order exists, no card touched yet: write it down
                noted["order_id"] = order_id
                self.store.append(d.id, "order_created", {"buyer": bid, "request_id": rid, "order_id": order_id}, ts=asked)
            kw["on_order"] = on_order
        return self.holds.authorize(d.ladder.start_price, d.currency, f"{d.id}:{bid}", rid, **kw)

    def _note_order(self, d: Drop, bid: str, rid: str, e: HoldError, noted: dict, asked: datetime) -> None:
        """A failure that carries the order it happened on (a holds backend that cannot call back as the order is made)."""
        if getattr(e, "order_id", None) and noted.get("order_id") != e.order_id:
            noted["order_id"] = e.order_id
            self.store.append(d.id, "order_created", {"buyer": bid, "request_id": rid, "order_id": e.order_id}, ts=asked)

    def _join_failed(self, d: Drop, bid: str, name: str, card_index: int, rid: str, e: HoldError, ts: datetime) -> None:
        self.store.append(d.id, "hold_failed", {
            "buyer": bid, "name": name, "card_index": card_index, "code": e.code, "message": str(e)[:200],
            "request_id": rid, "order_id": getattr(e, "order_id", None), **self._evidence(e)}, ts=ts)

    def _settle_earlier(self, drop: str, earlier: list[str]) -> None:
        """Before a refused retry is written down: resolve this buyer's earlier tries that were never confirmed (D-11:
        whatever hold PayPal shows for them is released). Writing the refusal first would close the intent that
        points at that hold, and nothing would ever look at it again. If PayPal cannot be asked, this raises and the
        refusal is not recorded at all."""
        for r in earlier:
            d = self.get(drop)
            if r in d.unresolved:
                self._reconcile_one(d, r, d.unresolved[r], self._new_out())

    def _release_other_cards(self, d: Drop, bid: str, card_index: int) -> Drop:
        """A retry on a different card must not leave the first card's hold behind (nor be handed it back by PayPal's
        replay and recorded as if it were on the new card): release the earlier try's hold first, record that, and go on
        under a new attempt id. If it cannot be released, the new hold is not placed."""
        others = [r for r, j in d.unresolved.items() if j.get("buyer") == bid and int(j.get("card_index", 0)) != card_index]
        for r in others:
            d = self.get(d.id)
            if r not in d.unresolved:
                continue
            if self._reconcile_one(d, r, d.unresolved[r], self._new_out(), reason="card_changed") in ("unconfirmed", "stuck"):
                raise CrowdError("earlier_attempt_unresolved", drop=d.id, buyer=bid)
        return self.get(d.id)

    def _confirm_hold(self, d: Drop, bid: str, rid: str, hold: dict) -> dict:
        """Ask PayPal what this hold is NOW. A replayed answer is a past answer: the hold may have been voided or lapsed
        since (the fake once answered from a cache and counted a dead hold toward the crowd). Only a hold PayPal shows as
        open, at the right amount, is recorded; anything else is resolved through `reconcile` (voided unless PayPal says
        it is already gone) and the buyer is told to try again. If PayPal cannot be asked the join is left open."""
        try:
            cur = self.holds.get(hold["auth_id"])
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None
        except HoldError as e:
            raise CrowdError("paypal_error", drop=d.id, buyer=bid, issue=e.issue or e.code, http=e.http) from None
        try:
            right = lad.D(cur["amount"]) == d.ladder.start_price and cur.get("currency") == d.currency   # no currency is no match
        except (lad.LadderError, TypeError, ValueError, ArithmeticError, KeyError):
            right = False
        if cur.get("status") in OPEN_AUTH and right:
            return {**hold, "status": cur["status"], "expires": cur.get("expires") or hold.get("expires")}
        d = self.get(d.id)
        if rid in d.unresolved:
            self._reconcile_one(d, rid, d.unresolved[rid], self._new_out())
        raise CrowdError("hold_not_open", drop=d.id, buyer=bid, status=str(cur.get("status")))

    def _hold_items(self, d: Drop, bid: str, name: str, card_index: int, hold: dict, attempt: int, rid: str,
                    **extra) -> list[tuple[str, dict]]:
        """The events for a hold that now exists: the buyer joins, the hold is placed, and any ladder step the new crowd
        reaches locks. Counted on the drop as it is NOW."""
        count = d.count + 1
        items = [("buyer_joined", {"buyer": bid, "name": name, "card_index": card_index}),
                 ("hold_placed", {"buyer": bid, "name": name, "order_id": hold["order_id"], "auth_id": hold["auth_id"],
                                  "amount": m(d.ladder.start_price), "currency": d.currency, "status": hold["status"],
                                  "expires": hold.get("expires"), "attempt": attempt, "request_id": rid,
                                  **self._evidence(), **extra})]
        for i in range(d.locked_step + 1, len(d.ladder.steps)):
            s = d.ladder.steps[i]
            if count < s.min_buyers:
                break
            items.append(("step_locked", {"step": i, "min_buyers": s.min_buyers, "price": m(s.price), "count": count}))
        return items

    def leave(self, drop: str, buyer: str) -> dict:
        """A buyer leaves and their hold is released. Joining and leaving both stop at the deadline: after `ends_at` the
        crowd is fixed, so nobody can tip a funded drop under its minimum (or lift a missed one) by moving late.

        Like a join, a leave is written down before PayPal is asked (`leave_started`): if the void goes through but the
        reply is lost, `reconcile` reads the authorisation and, finding it VOIDED, records the leave as it happened. The
        clock is read once and stamps both the intent and the outcome. Decision D-11b: if PayPal cannot be reached or refuses
        the void, the leave still stands, and close or settle completes it (retries the void; the buyer is out of the crowd
        either way); nobody who saw an error is charged. A hold that is already captured is the one thing that cannot be left."""
        with self.store.lock(drop):
            now = self.now()      # the moment of the request is what the events are stamped with, not when PayPal answers
            d = self.get(drop)
            self.require_paypal(d.id)
            if d.state != "live":
                raise CrowdError("drop_not_live", drop=d.id, state=self.display_state(d))
            if d.ends_at and now >= parse_iso(d.ends_at):
                raise CrowdError("drop_ended", drop=d.id, ended_at=d.ends_at)
            bid = slug(buyer)
            b = d.buyers.get(bid)
            if b is None:
                raise CrowdError("buyer_not_found", drop=d.id, buyer=bid)
            if b.status != "held":
                raise CrowdError("no_active_hold", drop=d.id, buyer=bid, status=b.status)
            self.store.append(d.id, "leave_started", {
                "buyer": bid, "auth_id": b.auth_id, "request_id": request_id_for(d.id, bid, "void", b.attempts),
                **self._evidence()}, ts=now)
            try:
                how = self._void_call(d, b.id, b.attempts, b.auth_id)
            except AuthorizationAlreadyCaptured as e:
                self._leave_failed(d, b, e, now)
                raise CrowdError("void_conflict", drop=d.id, buyer=b.id) from None
            except HoldError as e:
                # PayPal said no. The buyer asked in time and is shown an error, so by D-11b they are not charged for it: the
                # leave stays on record (no `leave_failed`), and close or settle retries the void and honours it. Only a hold
                # that is already captured cannot be (above).
                raise CrowdError("leave_pending", drop=d.id, buyer=b.id, issue=e.issue or e.code, http=e.http) from None
            voided = {"buyer": bid, "auth_id": b.auth_id, "reason": "left", **self._evidence()}
            if how == "expired":
                voided["expired"] = True
            self.store.append_many(d.id, [("hold_voided", voided), ("buyer_left", {"buyer": bid})], ts=now)
            out = self.view(self.get(drop))
            out.update({"buyer": bid, "released": m(b.hold_amount)})
            return out

    def _leave_failed(self, d: Drop, b: Buyer, e: HoldError | None, ts: datetime, reason: str | None = None) -> None:
        self.store.append(d.id, "leave_failed", {
            "buyer": b.id, "auth_id": b.auth_id, "reason": reason or (e.issue or e.code if e else "leave_failed"),
            **self._evidence(e)}, ts=ts)

    def exclude(self, drop: str, buyer: str) -> dict:
        """A person takes ONE buyer out of the crowd: their hold is released, `buyer_excluded` is recorded, and they cannot rejoin.
        This is how a person gets past a single hold the engine cannot get past on its own (PayPal will not describe it at close,
        or refuses to capture or release it for a reason that is not about the account). The drop goes on without that buyer;
        the plan changes, so a person approves the fresh one. Allowed while the drop is live, closed or settling, for a buyer
        whose hold is held or whose capture failed. If PayPal refuses the release the buyer is still excluded and the hold is
        recorded as `void_failed`, which `verify` keeps reporting (void it at PayPal by hand). If PayPal cannot be reached, or
        the hold was already captured, nothing is recorded."""
        with self.store.lock(drop):
            d = self.get(drop)
            self.require_paypal(d.id)
            if d.state not in ("live", "closed", "settling"):
                raise CrowdError("cannot_exclude", drop=d.id, state=self.display_state(d))
            bid = slug(buyer)
            b = d.buyers.get(bid)
            if b is None:
                raise CrowdError("buyer_not_found", drop=d.id, buyer=bid)
            if b.excluded:
                raise CrowdError("already_excluded", drop=d.id, buyer=bid)
            if b.status not in ("held", "capture_failed"):
                raise CrowdError("no_active_hold", drop=d.id, buyer=bid, status=b.status)
            now = self.now()
            # Before the drop is closed, a buyer taken out after the deadline is out of the crowd AS OF the deadline (as a hold found
            # dead at close is): the events are stamped at `ends_at`, so the crowd that closes the drop does not count them.
            stamp = parse_iso(d.ends_at) if d.state == "live" and d.ends_at and now >= parse_iso(d.ends_at) else now
            failed_capture = b.status == "capture_failed"
            excluded = ("buyer_excluded", {"buyer": bid, "auth_id": b.auth_id, "phase": d.state, **self._evidence()})
            released = True
            try:
                how = self._void_call(d, b.id, b.attempts, b.auth_id)
            except AuthorizationAlreadyCaptured:
                raise CrowdError("void_conflict", drop=d.id, buyer=b.id) from None
            except PayPalRefused as e:
                released = False
                items = [excluded, ("void_failed", {"buyer": bid, "auth_id": b.auth_id, "code": e.code, "message": str(e)[:200],
                                                    "reason": "excluded", **self._evidence(e)})]
            except HoldError as e:
                raise CrowdError("paypal_error", drop=d.id, buyer=b.id, issue=e.issue or e.code, http=e.http) from None
            else:
                voided = {"buyer": bid, "auth_id": b.auth_id, "reason": "excluded", **self._evidence()}
                if how == "expired":
                    voided["expired"] = True
                items = [excluded, ("failed_hold_released" if failed_capture else "hold_voided",
                                    {"buyer": bid, "auth_id": b.auth_id, "reason": "excluded"} if failed_capture else voided)]
            self.store.append_many(d.id, items, ts=stamp)
            out = self.view(self.get(drop))
            out.update({"buyer": bid, "excluded": True, "released": released, "auth_id": b.auth_id})
            if not released:
                out["next"] = f"void hold {b.auth_id} at PayPal by hand; `togi crowd drop verify` keeps reporting it until PayPal shows it gone"
            return out

    def close(self, drop: str) -> dict:
        """Stop joining. In order: settle any join or leave whose answer was lost (`reconcile`); ask PayPal about every
        hold on record and drop from the crowd any that is no longer open (`revalidate`); then count the crowd as it stood
        at the deadline if the clock already ran out, or as it stands now. The funded decision, the price and every plan
        that follows come from that revalidated crowd, never from the engine's memory of it."""
        with self.store.lock(drop):
            d = self.get(drop)
            self.require_paypal(d.id)
            if d.state in ("closed", "settling", "settled"):
                out = self.view(d)
                out["already_closed"] = True
                return out
            if d.state != "live":
                raise CrowdError("drop_not_live", drop=d.id, state=d.state)
            now = self.now()
            rec = self._reconcile(d.id)
            d = self.get(drop)
            ends = parse_iso(d.ends_at) if d.ends_at else None
            expired = ends is not None and now >= ends
            # A hold found dead belongs to the crowd as of the deadline too: when closing late, its event is stamped at the
            # deadline (the real moment of the check is in `checked_at`), so counting the crowd "as of ends_at" sees it.
            rev = self._revalidate(d, ends if expired else now, now)
            d = self.get(drop)
            crowd = self.store.project(d.id, as_of=ends) if expired else d    # the drop as it stood at its deadline
            self.store.append(d.id, "drop_closed", {
                "reason": "expired" if expired else "manual", "count": crowd.count, "funded": crowd.count >= crowd.minimum,
                "price": m(crowd.price()), "as_of": iso(ends) if expired else iso(now),
                **({"revalidated": rev} if rev else {})}, ts=now)
            out = self.view(self.get(drop))
            if rec["checked"]:
                out["reconciled"] = rec
            if rev:
                out["revalidated"] = rev
            return out

    def _revalidate(self, d: Drop, stamp: datetime, now: datetime, unreadable=None) -> list[dict]:
        """Ask PayPal about every hold the engine thinks is held (the authorisation AND the captures on its order). One that PayPal
        no longer shows as CREATED or PENDING, or that already has a capture, is recorded with PayPal's own word
        (`hold_voided{reason: paypal_says_<status>}`) and leaves the crowd; a capture the engine did not make is named so it
        can be refunded, and is never captured on top of. A status the engine has no rule for is voided, and counted as
        released only if the void goes through. If PayPal cannot be asked, nothing is closed: a crowd counted on a guess is the
        thing this is here to stop. A hold PayPal will not even describe (a refusal that is not a 404) stops the caller: close
        refuses with a `next` that names the hold, settle blocks; `unreadable(u)` is how the caller says which."""
        found = []
        for b in list(d.holding):
            try:
                seen = self._inspect(d, b)
            except _Unreadable as u:
                if unreadable is not None:
                    unreadable(u)
                raise self._unreadable_error(d, u) from None
            verdict, status = seen["verdict"], seen["status"]
            if verdict in ("open", "ours"):
                continue
            ev = {"buyer": b.id, "auth_id": b.auth_id, "reason": f"paypal_says_{str(status).lower()}",
                  "paypal_status": status, "checked_at": iso(now), **self._evidence()}
            if verdict != "odd":              # gone, missing, or money taken: PayPal's own answer, recorded as it is
                if status == "EXPIRED":
                    ev["expired"] = True
                if verdict == "taken":        # money left a buyer's card outside the engine: verify will say so
                    ev["taken_outside_engine"] = True
                    ev["capture_ids"] = [c["capture_id"] for c in seen["money"] if c.get("capture_id")]
                    if status not in TAKEN_AUTH:
                        ev["reason"] = "paypal_shows_a_capture"
                self.store.append(d.id, "hold_voided", ev, ts=stamp)
                found.append({"buyer": b.id, "paypal_status": status, "action": "recorded"})
                continue
            try:                              # DENIED, or a status nobody has a rule for: release it, do not assume
                self._void_call(d, b.id, b.attempts, b.auth_id)
            except PayPalRefused as e:
                self.store.append(d.id, "void_failed", {
                    "buyer": b.id, "auth_id": b.auth_id, "code": e.code, "message": str(e)[:200],
                    "paypal_status": status, **self._evidence(e)}, ts=stamp)
                found.append({"buyer": b.id, "paypal_status": status, "action": "void_failed"})
                continue
            except HoldError as e:
                raise CrowdError("paypal_error", drop=d.id, buyer=b.id, issue=e.issue or e.code, http=e.http) from None
            self.store.append(d.id, "hold_voided", {**ev, "released_by_void": True}, ts=stamp)
            found.append({"buyer": b.id, "paypal_status": status, "action": "voided"})
        return found

    # ---- asking PayPal what a hold is right now ----------------------------------------------------------

    def _captures_on(self, d: Drop, b: Buyer) -> list[dict] | None:
        """Every capture PayPal shows on this hold's order, whoever made it; None when this PayPal client cannot list them (then
        the authorisation's status is all there is to go on). Not being able to reach PayPal stops the caller; an order PayPal
        does not know has no captures; any other refusal means PayPal will not say."""
        listing = getattr(self.holds, "order_captures", None)
        if listing is None or not b.order_id:
            return None
        try:
            return list(listing(b.order_id))
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None
        except HoldNotFound:
            return []
        except HoldError as e:
            raise _Unreadable(b, e) from None

    def _could_be_ours(self, d: Drop, status: str | None, captures: list[dict] | None, money: list[dict]) -> bool:
        """Settlement is under way and PayPal shows a hold as captured: is that plausibly the engine's own capture, whose reply was
        lost or whose crash came before it was written down? Only if the settlement is a funded one, the hold is CAPTURED, and the
        one capture on it is for exactly the final price. `_capture_one` then recovers and records it (or blocks if it cannot).
        Before settlement starts the engine has captured nothing, so any capture is somebody else's."""
        if d.settlement is None or d.settlement.get("outcome") != "funded" or status not in TAKEN_AUTH:
            return False
        if captures is None:                  # the client cannot list captures: the recovery path in _capture_one decides
            return True
        c = money[0] if len(money) == 1 else None
        return (c is not None and c.get("status") in ("COMPLETED", "PENDING") and c.get("currency") == d.currency
                and self._same_amount(c, lad.D(d.settlement["final_price"])))

    def _inspect(self, d: Drop, b: Buyer) -> dict:
        """What PayPal says about one hold RIGHT NOW (read-only): the authorisation and every capture on its order. `verdict`:
          open     CREATED or PENDING, and nothing captured: it counts, and can be captured or released
          ours     settlement is under way and the hold shows the engine's own capture (see `_could_be_ours`)
          taken    money was taken from it (CAPTURED, PARTIALLY_CAPTURED, or any capture on its order): out of the crowd, and
                   the capture is for a person to refund
          gone     VOIDED or EXPIRED: nothing is held
          missing  PayPal has no such authorisation
          odd      any other status (DENIED, PARTIALLY_CREATED, nothing at all): PayPal may still hold the money
        A transient failure raises CrowdError('paypal_unavailable'); a refusal that is not a 404 raises _Unreadable."""
        try:
            cur = self.holds.get(b.auth_id)
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None
        except HoldNotFound:                  # PayPal's own answer: it has no such authorisation, so it holds nothing
            cur = {"status": "NOT_FOUND"}
        except HoldError as e:
            raise _Unreadable(b, e) from None
        status = cur.get("status")
        captures = self._captures_on(d, b)
        money = [c for c in captures or [] if c.get("status") not in NO_MONEY_CAPTURE]
        if status in TAKEN_AUTH or money:
            verdict = "ours" if self._could_be_ours(d, status, captures, money) else "taken"
        elif status in OPEN_AUTH:
            verdict = "open"
        elif status in GONE_AUTH:
            verdict = "gone"
        elif status == "NOT_FOUND":
            verdict = "missing"
        else:
            verdict = "odd"
        return {"status": status, "verdict": verdict, "auth": cur, "captures": captures, "money": money}

    def _unreadable_error(self, d: Drop, u: "_Unreadable", again: str = "close") -> CrowdError:
        """A hold PayPal will not describe (403 and the like): close and plan stop, and a person gets the exact way out. `again` is
        the command that picks the drop up once the hold is dealt with (`close` for a live drop, `plan` for a closed one)."""
        e, b = u.err, u.buyer
        return CrowdError("hold_unreadable", drop=d.id, buyer=b.id, auth_id=b.auth_id, issue=e.issue or e.code,
                          http=e.http if e.http is not None else "n/a", debug_id=e.debug_id or "none given",
                          next=_manual_exit(d.id, b, again=f"togi crowd drop {again} --drop {d.id}"))

    # ---- a join or a leave whose answer never came ----------------------------------------------------

    def reconcile(self, drop: str) -> dict:
        """Settle every join and leave that was started but never confirmed (a lost reply, a crash between PayPal acting
        and the engine writing it down). For each join, ask PayPal what is on its order: a hold is released (D-11: never
        adopted); no hold is recorded as unconfirmed. For each leave, read the authorisation: VOIDED means the buyer left.
        Runs by itself at the start of `close` and `settle`."""
        with self.store.lock(drop):
            self.require_paypal(self.get(drop).id)
            return self._reconcile(drop)

    def _reconcile(self, drop: str) -> dict:
        out = self._new_out()
        self._reconcile_leaves(drop, out)
        for rid in list(self.get(drop).unresolved):
            d = self.get(drop)
            if rid not in d.unresolved:
                continue
            out["checked"] += 1
            self._reconcile_one(d, rid, d.unresolved[rid], out)
        return out

    def _reconcile_leaves(self, drop: str, out: dict) -> None:
        """A leave that was started and never confirmed is resolved by reading the authorisation and, if the hold is still open,
        by releasing it. Decision D-11b (Tye, 2026-10-27, extending D-11): a leave the buyer asked for before the deadline is
        honoured even if PayPal errored, because nobody who saw an error is charged. So:
          - gone at PayPal (VOIDED, EXPIRED): the buyer left, recorded as of the moment they asked;
          - open (or any status but captured): the void is retried now. If it goes through the buyer left. If PayPal refuses it,
            the buyer is still out of the crowd and the hold is recorded as `void_failed`, which `verify` keeps reporting
            (with the hold to void by hand) until PayPal shows it gone. If PayPal cannot be reached, nothing is decided: the
            leave stays open and the caller stops (to be resumed);
          - captured: money moved before the leave was honoured, so it cannot be: `leave_failed`, and verify reports it."""
        for bid, j in list(self.get(drop).leaving.items()):
            d = self.get(drop)
            if bid not in d.leaving:
                continue
            out["checked"] += 1
            b = d.buyers.get(bid)
            asked = parse_iso(j["asked_at"])
            if b is None or b.status != "held" or b.auth_id != j.get("auth_id"):
                self.store.append(d.id, "leave_failed", {"buyer": bid, "auth_id": j.get("auth_id"), "reason": "hold_changed"},
                                  ts=self.now())
                continue
            try:
                cur = self.holds.get(b.auth_id)
            except PayPalUnavailable:
                raise CrowdError("paypal_unavailable", drop=d.id) from None
            except HoldError as e:
                raise CrowdError("paypal_error", drop=d.id, buyer=bid, issue=e.issue or e.code, http=e.http) from None
            status = cur.get("status")
            if status in GONE_AUTH:
                voided = {"buyer": bid, "auth_id": b.auth_id, "reason": "left", "reconciled": True, "paypal_status": status,
                          **self._evidence()}
                if status == "EXPIRED":
                    voided["expired"] = True
                self.store.append_many(d.id, [("hold_voided", voided), ("buyer_left", {"buyer": bid})], ts=asked)
                out["leaves_completed"].append(bid)
                continue
            if status in TAKEN_AUTH:
                self.store.append(d.id, "leave_failed", {
                    "buyer": bid, "auth_id": b.auth_id, "reason": f"paypal_says_{str(status).lower()}",
                    "paypal_status": status, "reconciled": True, **self._evidence()}, ts=self.now())
                out["leaves_kept"].append(bid)
                continue
            try:
                how = self._void_call(d, bid, b.attempts, b.auth_id)
            except AuthorizationAlreadyCaptured as e:        # taken between the read and the void
                self._leave_failed(d, b, e, self.now())
                out["leaves_kept"].append(bid)
                continue
            except PayPalRefused as e:      # PayPal said no: honoured anyway, and the hold that could not be released is on record
                self.store.append_many(d.id, [
                    ("buyer_left", {"buyer": bid}),
                    ("void_failed", {"buyer": bid, "auth_id": b.auth_id, "code": e.code, "message": str(e)[:200],
                                     "paypal_status": status, "reason": "left", "reconciled": True, **self._evidence(e)})],
                    ts=asked)
                out["leaves_completed"].append(bid)
                continue
            except HoldError as e:
                raise CrowdError("paypal_error", drop=d.id, buyer=bid, issue=e.issue or e.code, http=e.http) from None
            voided = {"buyer": bid, "auth_id": b.auth_id, "reason": "left", "reconciled": True, "released_by_void": True,
                      "paypal_status": status, **self._evidence()}
            if how == "expired":
                voided["expired"] = True
            self.store.append_many(d.id, [("hold_voided", voided), ("buyer_left", {"buyer": bid})], ts=asked)
            out["leaves_completed"].append(bid)

    def _reconcile_one(self, d: Drop, rid: str, j: dict, out: dict, reason: str | None = None) -> str:
        """Resolve one unconfirmed join. Returns what became of it: 'released' (a hold existed and is gone), 'nothing'
        (PayPal holds nothing for it), 'unconfirmed' (PayPal would not say), or 'stuck' (a hold exists that could not be
        released, or money moved). Whatever the answer, the intent is closed by an event that says so, except when PayPal
        cannot be reached at all, which raises and leaves it open."""
        bid, name = j["buyer"], j.get("name") or j["buyer"]
        card_index, attempt = int(j.get("card_index", 0)), int(j.get("attempt", 1))
        base = {"buyer": bid, "name": name, "request_id": rid, "reconciled": True}
        self._release_stale_orders(d, bid, rid, j.get("order_id"))      # an earlier order of this same ask: release its hold too
        d = self.get(d.id)
        try:
            found = self.holds.recover_join(rid, j["amount"], j.get("currency", d.currency), f"{d.id}:{bid}",
                                            order_id=j.get("order_id"))
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None
        except PayPalRefused as e:     # PayPal will not tell us: say so, and let verify keep asking
            self.store.append(d.id, "hold_failed", {**base, "card_index": card_index, "code": "join_unconfirmed",
                                                    "message": "PayPal would not say whether a hold exists: " + str(e)[:150],
                                                    "order_id": j.get("order_id"), **self._evidence(e)}, ts=self.now())
            out["unconfirmed"].append(bid)
            return "unconfirmed"
        except HoldError as e:
            raise CrowdError("paypal_error", drop=d.id, issue=e.issue or e.code, http=e.http) from None
        order_id, auth = found.get("order_id"), found.get("auth")
        items: list[tuple[str, dict]] = []
        if order_id and order_id != j.get("order_id"):
            items.append(("order_created", {"buyer": bid, "request_id": rid, "order_id": order_id}))
        if auth is None:
            self.store.append_many(d.id, items + [("hold_failed", {
                **base, "card_index": card_index, "code": "join_unconfirmed", "order_id": order_id,
                "message": "PayPal holds nothing for this join", **self._evidence()})], ts=self.now())
            out["unconfirmed"].append(bid)
            return "nothing"
        b = d.buyers.get(bid)
        auth_id, status = auth["auth_id"], auth.get("status")
        try:
            right_amount = lad.D(auth["amount"]) == d.ladder.start_price and auth.get("currency") == d.currency
        except (lad.LadderError, TypeError, ValueError, ArithmeticError, KeyError):   # no readable amount: not a hold we can vouch for
            right_amount = False
        # Decision D-11 (Tye, 2026-10-27): a hold the engine never confirmed is always released, never adopted.
        # The buyer saw an error, so they are never charged for it; they can simply join again.
        released = {**base, "order_id": order_id, "auth_id": auth_id, "amount": auth.get("amount"), "paypal_status": status,
                    **self._evidence()}
        if status in TAKEN_AUTH:    # money moved on a join the engine never recorded
            self.store.append_many(d.id, items + [("hold_failed", {
                **released, "card_index": card_index, "code": "orphan_unexpected_status",
                "message": f"PayPal shows the hold as {status}"})], ts=self.now())
            out["unconfirmed"].append(bid)
            return "stuck"
        if status in GONE_AUTH:     # PayPal itself says nothing is held: the only status that needs no void
            self.store.append_many(d.id, items + [("orphan_hold_released", {
                **released, "reason": f"already_{str(status).lower()}", **({"context": reason} if reason else {})})],
                ts=self.now())
            out["released"].append(bid)
            return "released"
        # Open, or a status nobody has a rule for (DENIED, PARTIALLY_CREATED, nothing at all): PayPal may still hold the
        # money, so release it, and call it released only if PayPal agrees.
        why = reason or ("buyer_already_holding" if b and b.status == "held" else
                         "wrong_amount" if not right_amount else
                         "drop_not_joinable" if not self._joinable(d) else "join_unconfirmed")
        try:
            self._void_call(d, bid, attempt, auth_id)
        except PayPalRefused as e:
            self.store.append_many(d.id, items + [
                ("void_failed", {**released, "orphan": True, "code": e.code, **self._evidence(e)}),
                ("hold_failed", {**released, "card_index": card_index, "code": "orphan_void_failed",
                                 "message": f"the hold could not be released: {e.issue or e.code}"})], ts=self.now())
            out["unconfirmed"].append(bid)
            return "stuck"
        except HoldError as e:
            raise CrowdError("paypal_error", drop=d.id, issue=e.issue or e.code, http=e.http) from None
        self.store.append_many(d.id, items + [("orphan_hold_released", {**released, "reason": why})], ts=self.now())
        out["released"].append(bid)
        return "released"

    def _release_stale_orders(self, d: Drop, bid: str, rid: str, current_order: str | None) -> None:
        """One join intent (one request id) can make more than one order: PayPal forgets a request id after a while, and the
        same ask then makes a NEW order and a new hold, leaving the first order's hold (from the reply that was lost) behind. Every
        order of the intent other than `current_order` is read, and any hold on it is released (D-11), written down as
        `orphan_hold_released{superseded}` (the intent itself stays open until the current order is settled too). If PayPal
        cannot be asked, this raises and the intent stays open; if it will not say, the new hold is not placed on top of it."""
        j = self.get(d.id).unresolved.get(rid) or {}
        orders = list(j.get("order_ids") or ([j["order_id"]] if j.get("order_id") else []))
        stale = [o for o in orders if o and o != current_order and o not in (j.get("released_orders") or [])]
        if current_order is None and j.get("order_id") in stale:
            stale.remove(j["order_id"])     # no current order to compare with: the intent's own order is handled by the caller
        for oid in stale:
            try:
                found = self.holds.recover_join(rid, j["amount"], j.get("currency", d.currency), f"{d.id}:{bid}", order_id=oid)
            except PayPalUnavailable:
                raise CrowdError("paypal_unavailable", drop=d.id) from None
            except PayPalRefused:
                raise CrowdError("earlier_attempt_unresolved", drop=d.id, buyer=bid) from None
            except HoldError as e:
                raise CrowdError("paypal_error", drop=d.id, issue=e.issue or e.code, http=e.http) from None
            auth = found.get("auth")
            if auth is None:
                continue
            auth_id, status = auth["auth_id"], auth.get("status")
            ev = {"buyer": bid, "name": j.get("name") or bid, "request_id": rid, "order_id": oid, "auth_id": auth_id,
                  "amount": auth.get("amount"), "paypal_status": status, "superseded": True, "reconciled": True,
                  **self._evidence()}
            if status in GONE_AUTH:
                self.store.append(d.id, "orphan_hold_released", {**ev, "reason": f"already_{str(status).lower()}"}, ts=self.now())
                continue
            if status in TAKEN_AUTH:        # money moved on a hold nobody recorded: a person refunds it; verify keeps saying so
                self.store.append(d.id, "void_failed", {**ev, "orphan": True, "code": "orphan_unexpected_status"}, ts=self.now())
                continue
            try:
                self._void_call(d, bid, int(j.get("attempt", 1)), auth_id, distinct=True)
            except PayPalRefused as e:
                self.store.append(d.id, "void_failed", {**ev, "orphan": True, "code": e.code, **self._evidence(e)}, ts=self.now())
                raise CrowdError("earlier_attempt_unresolved", drop=d.id, buyer=bid) from None
            except HoldError as e:
                raise CrowdError("paypal_error", drop=d.id, issue=e.issue or e.code, http=e.http) from None
            self.store.append(d.id, "orphan_hold_released", {**ev, "reason": "order_superseded"}, ts=self.now())

    # ---- releasing a hold ----------------------------------------------------------------------------------

    def _void_call(self, d: Drop, bid: str, attempt: int, auth_id: str, distinct: bool = False) -> str:
        """One void. Returns 'voided' (released, or PayPal says it already is) or 'expired' (PayPal says it lapsed on its
        own). A refusal that only NAMES the hold as voided or expired is not taken at its word: PayPal is asked, and unless
        it says VOIDED or EXPIRED the refusal stands. A transient failure becomes CrowdError('paypal_unavailable'); any
        other refusal propagates for the caller to decide."""
        rid = request_id_for(d.id, bid, "void", attempt)
        if distinct:        # a second hold of the same join intent (an earlier order's): PayPal must not take its void for the first's
            rid = f"{rid}-{auth_id}"[:108]
        try:
            self.holds.void(auth_id, rid)
            return "voided"
        except (AuthorizationVoided, AuthorizationExpired):
            status = self._read_status(d, auth_id)
            if status == "VOIDED":
                return "voided"       # an earlier attempt got through: the goal state holds, and PayPal says so
            if status == "EXPIRED":
                return "expired"      # nothing is held any more, and PayPal says so
            raise
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None

    def _read_status(self, d: Drop, auth_id: str) -> str | None:
        """PayPal's current status for an authorisation, or None if it will not say (a refusal). Not being able to reach
        PayPal at all stops the caller (the same CrowdError as everywhere else)."""
        try:
            return self.holds.get(auth_id).get("status")
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None
        except HoldError:
            return None

    def _record_void_failed(self, d: Drop, b: Buyer, e: PayPalRefused) -> None:
        self.store.append(d.id, "void_failed", {
            "buyer": b.id, "auth_id": b.auth_id, "code": e.code, "message": str(e)[:200], **self._evidence(e)},
            ts=self.now())

    # ---- settlement ----------------------------------------------------------------------------------------

    def _expired(self, b: Buyer) -> bool:
        """Does OUR clock say this hold ran out? Only a hint that PayPal should be asked: nothing is decided by it. (No
        expiry recorded, or one that cannot be read, or one with no zone, which is read as UTC: never crashes, and an
        unreadable date is simply not known to have passed.)"""
        if not b.expires:
            return False
        try:
            return self.now() >= parse_utc(b.expires)
        except (ValueError, TypeError, OverflowError, AttributeError):
            return False

    def _ask_if_clock_expired(self, b: Buyer) -> dict | None:
        """If the clock says a hold has run out, ask PayPal about it (read-only) and return its answer, whatever it is. A hold
        the clock does not call expired is not asked about: the real step answers for itself. No answer (PayPal refuses the
        read) is None, and the real step runs."""
        if not self._expired(b):
            return None
        try:
            return self.holds.get(b.auth_id)
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable") from None
        except HoldError:
            return None

    def _paypal_says_expired(self, b: Buyer) -> dict | None:
        """PayPal's answer for a hold our clock calls expired, but only when that answer is EXPIRED. Any other answer (still
        open, captured, voided) means the date proves nothing and the real step must run."""
        cur = self._ask_if_clock_expired(b)
        return cur if cur is not None and cur.get("status") == "EXPIRED" else None

    def _outcome(self, d: Drop) -> tuple[str, Decimal]:
        """(funded | not_funded, final price) of the settlement already started, or else of the crowd the records show PayPal
        still holds."""
        if d.settlement:
            return d.settlement["outcome"], lad.D(d.settlement["final_price"])
        return ("funded" if d.count >= d.minimum else "not_funded"), d.price()

    def plan(self, drop: str) -> dict:
        """Exactly what settle will do, from engine numbers and from what PayPal shows right now. Moves nothing. Every remaining
        hold is read at PayPal (the authorisation and the captures on its order): one that is no longer open, or already has a
        capture, is left out of the plan (and named under `changed`), and the funded decision and the price follow the crowd
        that is left. Settle asks again before any money moves and refuses a plan that no longer matches."""
        d = self.get(drop)
        if d.state == "settled":
            return {"drop": d.id, "action": "none", "summary": "already settled", "already_settled": True,
                    **self._settled_numbers(d)}
        if d.state not in ("closed", "settling"):
            raise CrowdError("drop_not_closed", drop=d.id, state=self.display_state(d))
        self.require_paypal(d.id)
        seen = {}
        for b in d.holding:
            try:
                seen[b.id] = self._inspect(d, b)
            except _Unreadable as u:
                raise self._unreadable_error(d, u, again="plan") from None
        return self._plan(d, seen)

    def _plan(self, d: Drop, seen: dict | None) -> dict:
        """The plan for a closed or settling drop. With `seen` (what `_inspect` found for each held buyer) the holds PayPal no
        longer shows as open are left out; with None the records are taken as they stand (settle uses that right after it has
        recorded what PayPal showed)."""
        holding = d.holding
        left = [b for b in holding if seen is not None and seen[b.id]["verdict"] not in ("open", "ours")]
        kept = [b for b in holding if b not in left]
        n = len(kept)
        if d.settlement:
            outcome, final = d.settlement["outcome"], lad.D(d.settlement["final_price"])
        else:
            crowd = d.count - len(left)        # the crowd that closed the drop, less every hold gone since (recorded, or just seen)
            outcome = "funded" if crowd >= d.minimum else "not_funded"
            final = lad.price_at(d.ladder, crowd, d.locked_step)
        expired = [b for b in left if seen[b.id]["status"] == "EXPIRED"]    # PayPal says EXPIRED: nothing can be captured
        changed = [b for b in left if b not in expired]
        # failed captures whose hold is still open: settling releases them, so the approval must name them too
        releases = sum(1 for b in d.buyers.values() if b.status == "capture_failed" and not b.released
                       and not b.void_failed and b.error != "hold_expired")
        tail = (f", {plural(len(expired), 'expired hold')} cannot be taken" if expired else "") + \
               (f", {plural(len(changed), 'hold')} no longer open at PayPal left out" if changed else "") + \
               (f", then release {plural(releases, 'failed hold')}" if releases else "")
        base = {"drop": d.id, "outcome": outcome, "holds": n, "resuming": d.state == "settling",
                "releases": releases, "expired": len(expired), "checked_at_paypal": seen is not None}
        if changed:
            base["changed"] = [{"buyer": b.id, "paypal_status": seen[b.id]["status"], "verdict": seen[b.id]["verdict"]}
                               for b in changed]
        if d.unresolved:
            base["unresolved_joins"] = len(d.unresolved)
        if d.blocked:
            base["blocked"] = {k: d.blocked.get(k) for k in ("code", "issue", "http", "debug_id", "phase", "buyer", "at")
                               if d.blocked.get(k) is not None}
        if outcome == "funded":
            exp_fee = sum((lad.fee(final) for _ in range(n)), lad.ZERO.quantize(lad.CENT))
            total = (final * n).quantize(lad.CENT)
            return {**base, "action": "capture", "price": m(final), "total": m(total), "expected_fees": m(exp_fee),
                    "expected_net": m(total - exp_fee), "released_each": m(d.ladder.start_price - final),
                    "summary": f"capture {plural(n, 'hold')} at {dollars(final)} = {dollars(total)}" + tail}
        return {**base, "action": "void", "price": None, "total": "0.00",
                "summary": f"void {plural(n, 'hold')}, nobody pays" + tail}

    def settle(self, drop: str, expect: str | None = None) -> dict:
        """Capture every hold at the final price (minimum met) or void every hold (missed). Idempotent
        and resumable: a second call after a crash carries on where the first stopped, and every PayPal
        call carries a deterministic request id, so nothing is charged or released twice.

        Before anything moves, in this order: the PayPal in use must be the one the drop was made with; joins and leaves left
        open are reconciled; the breaker is evaluated from the whole event history (so a crash between the failure that should
        have tripped it and the block cannot skip it); every remaining hold is READ AT PAYPAL (the authorisation and the
        captures on its order) and recorded as PayPal shows it, so a hold voided, lapsed or already captured since the drop
        closed leaves the crowd; and (when `expect`, the approved plan token, is given) the plan is worked out again from that
        revalidated crowd under the lock and must match it exactly, releases included. If it does not, nothing is captured or
        released and the refusal carries the new token for a person to approve. A PayPal refusal that is about one buyer (an
        allow-list of names) is recorded against that buyer and settling goes on. Any other refusal, or two capture failures in a
        row (of any buyer-specific codes), or more than half of the captures failing once four were tried, stops settlement with
        `settle_blocked` before anything else is captured or released, and leaves the drop settling for a person. A transient
        failure also stops it, to be resumed. Failed holds are released only once every held buyer has a final outcome."""
        with self.store.lock(drop, wait=False):
            d = self.get(drop)
            if d.state == "settled":
                if expect is not None and str(expect).strip() != plan_token({"already_settled": True}):
                    raise CrowdError("plan_changed", drop=d.id, expected=str(expect).strip(), actual="settled")
                return {**self._settled_numbers(d), "already_settled": True}
            if d.state not in ("closed", "settling"):
                raise CrowdError("drop_not_closed", drop=d.id, state=self.display_state(d))
            self.require_paypal(d.id)
            self._reconcile(d.id)
            d = self.get(drop)
            self._check_breaker(d)                 # on every settle, before anything is captured or released
            read_at = self.now()
            self._revalidate(d, read_at, read_at, unreadable=lambda u: self._block(d, u.err, u.buyer, "check"))
            self._check_expect(d.id, expect)       # nothing moves unless the plan, from the crowd PayPal still shows, is the approved one
            d = self.get(drop)
            if d.settlement is None:
                outcome, final = self._outcome(d)
                self.store.append(d.id, "settlement_started", {
                    "outcome": outcome, "final_price": m(final), "count": d.count,
                    "buyers": [b.id for b in d.holding], **self._evidence()}, ts=self.now())
                d = self.get(drop)
            outcome, final = d.settlement["outcome"], lad.D(d.settlement["final_price"])
            for b in list(self.get(drop).buyers.values()):
                if b.status != "held":
                    continue  # already captured / voided / failed: skipped on resume
                if outcome == "funded":
                    self._capture_one(d, b, final)
                else:
                    self._void_one(d, b)
            still_held = [b.id for b in self.get(drop).buyers.values() if b.status == "held"]
            if still_held:        # a hold with no outcome: nothing may be released until every one has one
                self._block(d, None, None, "capture" if outcome == "funded" else "void", code="held_without_outcome",
                            message=f"{len(still_held)} hold(s) have no outcome yet")
            # A failed capture must not leave money held on the buyer's card: release it. Only now, when every buyer has a
            # final outcome and nothing blocked settlement, and in a separate pass so a crash here resumes cleanly (the
            # buyer is no longer `held`, but is not yet `released`).
            for b in list(self.get(drop).buyers.values()):
                if b.status == "capture_failed" and not b.released and not b.void_failed:
                    self._release_failed(d, b)
            d = self.get(drop)
            return {**self._finish(d), "already_settled": False}

    def _check_expect(self, drop: str, expect: str | None) -> None:
        """Refuse unless the plan, worked out now from the records (which settle has just made agree with PayPal), is exactly the
        one a person approved. The refusal carries the new token: a person approves THAT plan; nothing is captured or released on
        the old one."""
        if expect is None:
            return
        actual = plan_token(self._plan(self.get(drop), None))
        if str(expect).strip() != actual:
            raise CrowdError("plan_changed", drop=drop, expected=str(expect).strip(), actual=actual)

    def _block(self, d: Drop, e: HoldError | None, b: Buyer | None, phase: str, message: str | None = None,
               code: str | None = None, **more) -> None:
        """Stop settlement for a person. Writes `settle_blocked` (PayPal's error name, http status and debug id when it
        gave them), touches nothing else, and raises. The drop stays `settling`; a later `settle` carries on from here, and
        the streak that tripped the breaker starts again from this event. The refusal's `next` names the exact way on: void
        the hold at PayPal, or `drop exclude` the buyer, and when the same buyer blocked the last attempt too it says so."""
        ev = self._evidence(e)
        code = code or ev.get("issue") or "settle_blocked"
        text = (message or (str(e) if e is not None else "") or code)[:200]
        payload = {"phase": phase, "code": code, "message": text, **ev, **more}
        if b is not None:
            payload.update(buyer=b.id, auth_id=b.auth_id)
        again = d.blocked is not None and b is not None and d.blocked.get("buyer") == b.id    # the same buyer stopped it before
        self.store.append(d.id, "settle_blocked", payload, ts=self.now())
        who = f" for {b.id}" if b is not None else ""
        shown = b.id if b is not None else (", ".join(str(x) for x in more.get("buyers", [])) or "the drop")
        raise CrowdError(
            "settle_blocked",
            f"settlement of {d.id} is stopped: {code}{who} while {phase}. {text} This is not something to retry blindly: "
            f"a person must look at it first.", drop=d.id, phase=phase, issue=ev.get("issue") or code,
            http=ev.get("http") if ev.get("http") is not None else "n/a", debug_id=ev.get("debug_id") or "none given",
            buyer=shown, next=self._block_next(d, b, more.get("buyers"), again))

    def _block_next(self, d: Drop, b: Buyer | None, buyers: list | None, again: bool = False) -> str:
        """What a person does about a block: the exact hold or buyer, and the exact command."""
        plan = f"togi crowd drop plan --drop {d.id}"
        if b is not None:
            held = (f"{b.id} has stopped settlement before, so the cause is that one hold. " if again else "")
            return (f"{held}A person (the maker, who owns the PayPal account) looks at PayPal's answer for hold {b.auth_id}. To go "
                    f"on without it: void hold {b.auth_id} at PayPal by hand, or run `togi crowd drop exclude --drop {d.id} "
                    f"--buyer {b.id}` to release it and leave {b.id} out. If the cause is fixed instead, nothing more is needed. "
                    f"Then run `{plan}` and have a person approve the fresh plan before settle is run again.")
        if buyers:
            named = []
            for x in buyers:
                nb = d.buyers.get(str(x))
                named.append(f"hold {nb.auth_id} ({nb.id})" if nb is not None and nb.auth_id else str(x))
            return (f"A person (the maker, who owns the PayPal account) looks at PayPal's answers for {', '.join(named)}. A shared "
                    f"cause (the account, the request, the cards) is fixed at PayPal; a single bad buyer is either voided at PayPal "
                    f"by hand ('void hold <id>') or taken out with `togi crowd drop exclude --drop {d.id} --buyer <buyer>`. Then "
                    f"run `{plan}` and have a person approve the fresh plan before settle is run again.")
        return (f"A person (the maker, who owns the PayPal account) must look at PayPal's answer, fix the cause, and approve a fresh "
                f"plan (`{plan}`) before settle is run again")

    def _check_breaker(self, d: Drop) -> None:
        """Failed captures that share a cause are not unlucky buyers. Stop, before anything is released, on either sign:
          - two capture failures in a row, of ANY buyer-specific codes (a shared cause need not show the same code twice);
          - more than half of the captures attempted failing, once at least four were attempted.
        `AUTHORIZATION_EXPIRED` and the engine's own `hold_expired` are facts about one hold and do not count (settle removes
        holds PayPal says have lapsed before the plan is approved anyway). The count starts again after every capture that goes
        through (for the streak) and after every block (for both), so a person who looked and ran settle again is not stopped
        by the failures already looked at. Evaluated from the whole event history at the start of EVERY settle, so a crash
        between the failure that should have tripped it and the block cannot skip it."""
        streak, attempts, failed = [], 0, []
        for ev in self.store.events(d.id):
            t, p = ev["type"], ev["payload"]
            if t == "settle_blocked":
                streak, attempts, failed = [], 0, []
            elif t == "hold_captured":
                streak, attempts = [], attempts + 1
            elif t == "capture_failed":
                if p.get("code") in BREAKER_EXEMPT or str(p.get("issue") or "").upper() == "AUTHORIZATION_EXPIRED":
                    continue
                attempts += 1
                failed.append(p.get("buyer"))
                streak.append((p.get("buyer"), p.get("issue") or p.get("code")))
        if len(streak) >= BREAKER_LIMIT:
            last = streak[-BREAKER_LIMIT:]
            codes = {c for _, c in last}
            self._block(d, None, None, "capture", code=str(next(iter(codes))) if len(codes) == 1 else "captures_failing_in_a_row",
                        buyers=[b for b, _ in last],
                        message=f"{BREAKER_LIMIT} captures in a row failed ({', '.join(str(c) for _, c in last)})")
        if attempts >= BREAKER_RATE_MIN and len(failed) * 2 > attempts:
            self._block(d, None, None, "capture", code="most_captures_failing", buyers=failed,
                        message=f"{len(failed)} of the {attempts} captures tried failed")

    def _same_amount(self, c: dict | None, amount: Decimal) -> bool:
        try:
            return c is not None and lad.D(c["amount"]) == amount
        except (lad.LadderError, TypeError, ValueError, ArithmeticError, KeyError):
            return False

    def _capture_one(self, d: Drop, b: Buyer, final: Decimal) -> None:
        if final > b.hold_amount or final <= 0:
            raise CrowdError("capture_exceeds_hold", drop=d.id, buyer=b.id)
        cur = self._ask_if_clock_expired(b)
        if cur is not None and cur.get("status") == "EXPIRED":      # our clock said so, and PayPal agrees: the only reason not to try
            self.store.append(d.id, "capture_failed", {
                "buyer": b.id, "auth_id": b.auth_id, "code": "hold_expired", "message": "PayPal says the hold has expired",
                "paypal_status": "EXPIRED", "expires": b.expires, "checked_at": iso(self.now()), **self._evidence()},
                ts=self.now())
            return
        rid = request_id_for(d.id, b.id, "capture", b.attempts)
        failure, proof, c, recovered = None, {}, None, False
        try:
            if cur is not None and cur.get("status") in TAKEN_AUTH:
                # The date passed while a capture was already made (its reply lost, or settle stopped). PayPal says the money
                # was taken: find that capture and record it, rather than call the hold expired or capture it again.
                c, recovered = self._recover(d, b), True
                if not self._same_amount(c, final):
                    self._block(d, None, b, "capture", code="captured_not_as_approved",
                                message=f"PayPal says the hold is {cur.get('status')}, but no single capture of the final price "
                                        f"can be found for it.")
            else:
                c = self.holds.capture(b.auth_id, final, rid, final=True, currency=d.currency,
                                       simulate_decline=(b.card_index == DECLINE_CARD_INDEX))
        except AuthorizationAlreadyCaptured as e:
            c, recovered = self._recover(d, b), True   # captured on an earlier run that crashed before it was recorded
            if not self._same_amount(c, final):        # PayPal's word that it is captured, and nothing we can match to it
                self._block(d, e, b, "capture", message="PayPal says the hold was already captured, but no single capture of "
                                                        "the final price can be found for it.")
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None
        except PayPalRefused as e:
            if not buyer_specific(e):     # the account, the request, or a name nobody has seen: not this buyer's doing
                self._block(d, e, b, "capture")
            failure, proof = (e.code, str(e)[:200]), self._evidence(e)
        except HoldError as e:            # not about this buyer (credentials, configuration): stop
            self._block(d, e, b, "capture")
        if failure is None and c is not None and c.get("auth_id") and c["auth_id"] != b.auth_id:
            self._block(d, None, b, "capture", code="capture_on_another_hold", paypal_capture_id=c.get("capture_id"),
                        message=f"PayPal's capture {c.get('capture_id')} belongs to authorisation {c['auth_id']}, not to "
                                f"the hold {b.auth_id}.")
        if failure is None and (c is None or c.get("status") not in ("COMPLETED", "PENDING")):
            failure = ("capture_not_completed", f"capture status {c.get('status') if c else 'unknown'}")
            proof = {**self._evidence(), "paypal_status": c.get("status") if c else None}
        if failure is None and lad.D(c["amount"]) != final:
            failure = ("capture_amount_differs", "PayPal captured a different amount")
            proof = {**self._evidence(), "paypal_status": c.get("status"), "paypal_amount": str(c["amount"])}
        if failure:
            if c and c.get("capture_id"):    # PayPal did make a capture: name it so it can be found and refunded
                proof = {**proof, "paypal_capture_id": c["capture_id"]}
            self.store.append(d.id, "capture_failed", {"buyer": b.id, "auth_id": b.auth_id, "code": failure[0],
                                                       "message": failure[1], **proof}, ts=self.now())
            self._check_breaker(self.get(d.id))
            return
        fee = lad.D(c["fee"]) if c.get("fee") is not None else lad.fee(final)
        net = lad.D(c["net"]) if c.get("net") is not None else final - fee
        self.store.append(d.id, "hold_captured", {
            "buyer": b.id, "auth_id": b.auth_id, "capture_id": c["capture_id"], "amount": m(final), "fee": m(fee),
            "net": m(net), "status": c.get("status"), "final": True, **({"recovered": True} if recovered else {}),
            **self._evidence()}, ts=self.now())

    def _recover(self, d: Drop, b: Buyer) -> dict | None:
        """The capture PayPal shows on this hold's order, when the engine's own reply for it was lost. None only when PayPal
        ANSWERS that there is none (or more than one, which cannot be matched with certainty). An outage is never read as 'not
        found': it raises `paypal_unavailable` so settle resumes later; a refusal PayPal gives to the lookup itself stops
        settlement for a person (it cannot be said what was captured)."""
        if not b.order_id:
            return None
        try:
            c = self.holds.recover_capture(b.order_id)
            listing = getattr(self.holds, "order_captures", None)
            if c is not None and listing is not None:
                if len([x for x in listing(b.order_id) if x.get("status") not in NO_MONEY_CAPTURE]) > 1:
                    return None      # a second capture is on the order: whichever one this is, it cannot be told apart for sure
            return c
        except PayPalUnavailable:
            raise CrowdError("paypal_unavailable", drop=d.id) from None
        except HoldNotFound:
            return None
        except HoldError as e:
            self._block(d, e, b, "capture", message="PayPal would not say which capture is on this hold, so it cannot be "
                                                    "told what was taken")

    def _release_failed(self, d: Drop, b: Buyer) -> None:
        """A failed capture's hold must not stay open on the buyer's card. Release it; if PayPal refuses for a reason about
        this hold, write that down (`void_failed`, with its error name and debug id) and go on: verify names the hold and
        the next step. Any other refusal stops settlement."""
        if b.error == "hold_expired":     # PayPal said EXPIRED when this was recorded: nothing to void
            self.store.append(d.id, "failed_hold_released", {"buyer": b.id, "auth_id": b.auth_id, "reason": "expired"},
                              ts=self.now())
            return
        try:
            how = self._void_call(d, b.id, b.attempts, b.auth_id)
        except PayPalRefused as e:
            if not buyer_specific(e):
                self._block(d, e, b, "release")
            self._record_void_failed(d, b, e)
            return
        except HoldError as e:
            self._block(d, e, b, "release")
        self.store.append(d.id, "failed_hold_released", {"buyer": b.id, "auth_id": b.auth_id,
                                                         "reason": "expired" if how == "expired" else "voided"},
                          ts=self.now())

    def _void_one(self, d: Drop, b: Buyer) -> None:
        if self._paypal_says_expired(b):      # nothing to void: PayPal itself says it lapsed
            self.store.append(d.id, "hold_voided", {
                "buyer": b.id, "auth_id": b.auth_id, "reason": "expired", "expired": True, "paypal_status": "EXPIRED",
                "expires": b.expires, "checked_at": iso(self.now()), **self._evidence()}, ts=self.now())
            return
        try:
            how = self._void_call(d, b.id, b.attempts, b.auth_id)
        except PayPalRefused as e:
            if not buyer_specific(e):
                self._block(d, e, b, "void")
            self._record_void_failed(d, b, e)
            return
        except HoldError as e:
            self._block(d, e, b, "void")
        self.store.append(d.id, "hold_voided", {"buyer": b.id, "auth_id": b.auth_id, "reason": "not_funded",
                                                **({"expired": True} if how == "expired" else {}),
                                                **self._evidence()}, ts=self.now())

    def _finish(self, d: Drop) -> dict:
        events = self.store.events(d.id)
        counts = Counter(e["type"] for e in events)
        gross = sum((b.captured for b in d.buyers.values() if b.status == "captured"), lad.ZERO)
        fees = sum((b.fee for b in d.buyers.values() if b.status == "captured"), lad.ZERO)
        net = sum((b.net for b in d.buyers.values() if b.status == "captured"), lad.ZERO)
        captured = counts["hold_captured"]
        failed_auths = {e["payload"].get("auth_id") for e in events if e["type"] == "capture_failed"}
        unreleased = {e["payload"].get("auth_id") for e in events
                      if e["type"] == "void_failed" and not e["payload"].get("orphan")}
        c = {"holds": counts["hold_placed"], "captured": captured, "voided": counts["hold_voided"],
             "failed": counts["capture_failed"], "left": counts["buyer_left"]}
        if unreleased - failed_auths:      # holds PayPal would not release at all (missed minimum): a hold of their own
            c["void_failed"] = len(unreleased - failed_auths)
        payload = {
            "outcome": d.settlement["outcome"], "final_price": d.settlement["final_price"], "counts": c,
            "totals": {"gross": m(gross), "fees": m(fees), "net": m(net)},
            "shortfall": max(0, d.minimum - captured) if d.settlement["outcome"] == "funded" else 0}
        if unreleased:                      # any hold that may still be open on a buyer's card
            payload["unreleased"] = len(unreleased)
        self.store.append(d.id, "drop_settled", payload, ts=self.now())
        return self._settled_numbers(self.get(d.id))

    def _settled_numbers(self, d: Drop) -> dict:
        s = d.settled
        out = {"drop": d.id, "state": "settled", "outcome": s["outcome"], "final_price": s["final_price"],
               "counts": s["counts"], "totals": s["totals"], "shortfall": s.get("shortfall", 0)}
        if s.get("unreleased"):
            out["unreleased"] = s["unreleased"]
        return out

    # ---- guards ----------------------------------------------------------------------------------------

    def _require_joinable(self, d: Drop, now: datetime | None = None) -> None:
        now = now or self.now()
        if d.state == "draft":
            raise CrowdError("drop_not_published", drop=d.id)
        if d.state != "live":
            raise CrowdError("drop_closed", drop=d.id, state=d.state)
        if d.ends_at and now >= parse_iso(d.ends_at):
            raise CrowdError("drop_ended", drop=d.id, ended_at=d.ends_at)


def default_engine() -> Engine:
    """An engine from the environment: CROWD_DB, CROWD_PAYPAL (fake | sandbox)."""
    return Engine()
