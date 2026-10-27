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
BREAKER_LIMIT = 2                    # this many capture failures in a row with the same code stop settlement
BREAKER_EXEMPT = ("hold_expired",)   # PayPal said EXPIRED: a fact about that hold, not a sign of a shared cause


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
            "duration_minutes": duration_minutes, "ladder": lg.to_dict(), "costs": payload_costs}, ts=self.now())
        return drop_id

    def publish(self, drop: str) -> dict:
        with self.store.lock(drop):
            d = self.get(drop)
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
            self._require_joinable(d, asked)
            name = re.sub(r"\s+", " ", str(buyer_name or "")).strip()
            bid = slug(name)
            if not bid or len(name) > MAX_BUYER_NAME or any(ord(c) < 32 for c in name):
                raise CrowdError("bad_buyer", "a buyer name of 1 to 60 characters is needed")
            b = d.buyers.get(bid)
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
            replayed = bool(hold.get("replayed")) or rid in earlier
            if replayed or hold.get("status") not in OPEN_AUTH:
                hold = self._confirm_hold(d, bid, rid, hold)
            items = self._hold_items(d, bid, name, card_index, hold, attempt, rid,
                                     **({"replayed": True, "confirmed_status": hold.get("status")} if replayed else {}))
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
            right = lad.D(cur["amount"]) == d.ladder.start_price and (cur.get("currency") or d.currency) == d.currency
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
        clock is read once and stamps both the intent and the outcome."""
        with self.store.lock(drop):
            now = self.now()      # the moment of the request is what the events are stamped with, not when PayPal answers
            d = self.get(drop)
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
            except HoldError as e:          # PayPal said no: the hold stays theirs and the leave is over
                self._leave_failed(d, b, e, now)
                raise CrowdError("paypal_error", drop=d.id, buyer=b.id, issue=e.issue or e.code, http=e.http) from None
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

    def close(self, drop: str) -> dict:
        """Stop joining. In order: settle any join or leave whose answer was lost (`reconcile`); ask PayPal about every
        hold on record and drop from the crowd any that is no longer open (`revalidate`); then count the crowd as it stood
        at the deadline if the clock already ran out, or as it stands now. The funded decision, the price and every plan
        that follows come from that revalidated crowd, never from the engine's memory of it."""
        with self.store.lock(drop):
            d = self.get(drop)
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

    def _revalidate(self, d: Drop, stamp: datetime, now: datetime) -> list[dict]:
        """GET every hold the engine thinks is held. One that PayPal no longer shows as CREATED or PENDING is recorded with
        PayPal's own status (`hold_voided{reason: paypal_says_<status>}`) and leaves the crowd. A status the engine has no
        rule for is voided, and counted as released only if the void goes through. If PayPal cannot be asked, nothing is
        closed: a crowd counted on a guess is the thing this is here to stop."""
        found = []
        for b in list(d.holding):
            try:
                cur = self.holds.get(b.auth_id)
            except PayPalUnavailable:
                raise CrowdError("paypal_unavailable", drop=d.id) from None
            except HoldNotFound:              # PayPal's own answer: it has no such authorisation, so it holds nothing; verify
                cur = {"status": "NOT_FOUND"}  # keeps reporting it (it cannot be read back), and the crowd does not count it
            except HoldError as e:
                raise CrowdError("paypal_error", drop=d.id, buyer=b.id, issue=e.issue or e.code, http=e.http) from None
            status = cur.get("status")
            if status in OPEN_AUTH:
                continue
            ev = {"buyer": b.id, "auth_id": b.auth_id, "reason": f"paypal_says_{str(status).lower()}",
                  "paypal_status": status, "checked_at": iso(now), **self._evidence()}
            if status in GONE_AUTH or status in TAKEN_AUTH or status == "NOT_FOUND":
                if status == "EXPIRED":
                    ev["expired"] = True
                if status in TAKEN_AUTH:      # money left a buyer's card outside the engine: verify will say so
                    ev["taken_outside_engine"] = True
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

    # ---- a join or a leave whose answer never came ----------------------------------------------------

    def reconcile(self, drop: str) -> dict:
        """Settle every join and leave that was started but never confirmed (a lost reply, a crash between PayPal acting
        and the engine writing it down). For each join, ask PayPal what is on its order: a hold is released (D-11: never
        adopted); no hold is recorded as unconfirmed. For each leave, read the authorisation: VOIDED means the buyer left.
        Runs by itself at the start of `close` and `settle`."""
        with self.store.lock(drop):
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
        """A leave that was started and never confirmed is resolved by reading the authorisation, not by guessing from
        the error the buyer saw: gone at PayPal (VOIDED, EXPIRED) means the buyer left, and the leave is recorded as of
        the moment it was asked; open means the leave did not happen and the buyer is still in."""
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
            else:
                self.store.append(d.id, "leave_failed", {
                    "buyer": bid, "auth_id": b.auth_id, "reason": f"paypal_says_{str(status).lower()}",
                    "paypal_status": status, "reconciled": True, **self._evidence()}, ts=self.now())
                out["leaves_kept"].append(bid)

    def _reconcile_one(self, d: Drop, rid: str, j: dict, out: dict, reason: str | None = None) -> str:
        """Resolve one unconfirmed join. Returns what became of it: 'released' (a hold existed and is gone), 'nothing'
        (PayPal holds nothing for it), 'unconfirmed' (PayPal would not say), or 'stuck' (a hold exists that could not be
        released, or money moved). Whatever the answer, the intent is closed by an event that says so, except when PayPal
        cannot be reached at all, which raises and leaves it open."""
        bid, name = j["buyer"], j.get("name") or j["buyer"]
        card_index, attempt = int(j.get("card_index", 0)), int(j.get("attempt", 1))
        base = {"buyer": bid, "name": name, "request_id": rid, "reconciled": True}
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
            right_amount = lad.D(auth["amount"]) == d.ladder.start_price and (auth.get("currency") or d.currency) == d.currency
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

    # ---- releasing a hold ----------------------------------------------------------------------------------

    def _void_call(self, d: Drop, bid: str, attempt: int, auth_id: str) -> str:
        """One void. Returns 'voided' (released, or PayPal says it already is) or 'expired' (PayPal says it lapsed on its
        own). A refusal that only NAMES the hold as voided or expired is not taken at its word: PayPal is asked, and unless
        it says VOIDED or EXPIRED the refusal stands. A transient failure becomes CrowdError('paypal_unavailable'); any
        other refusal propagates for the caller to decide."""
        rid = request_id_for(d.id, bid, "void", attempt)
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
        """(funded | not_funded, final price) of the crowd that closed the drop, or of the settlement already started."""
        if d.settlement:
            return d.settlement["outcome"], lad.D(d.settlement["final_price"])
        if d.closed and d.closed.get("price") is not None:
            return ("funded" if d.closed.get("funded") else "not_funded"), lad.D(d.closed["price"])
        return ("funded" if d.count >= d.minimum else "not_funded"), d.price()

    def plan(self, drop: str) -> dict:
        """Exactly what settle will do, from engine numbers. Moves nothing. (A hold our clock says has run out is looked up
        at PayPal, read-only; it is left out only if PayPal says EXPIRED.)"""
        d = self.get(drop)
        if d.state == "settled":
            return {"drop": d.id, "action": "none", "summary": "already settled", "already_settled": True,
                    **self._settled_numbers(d)}
        if d.state not in ("closed", "settling"):
            raise CrowdError("drop_not_closed", drop=d.id, state=self.display_state(d))
        outcome, final = self._outcome(d)
        holding = d.holding
        expired = [b for b in holding if self._paypal_says_expired(b)]    # PayPal says EXPIRED: nothing can be captured
        n = len(holding) - len(expired)                                    # holds still to process
        # failed captures whose hold is still open: settling releases them, so the approval must name them too
        releases = sum(1 for b in d.buyers.values() if b.status == "capture_failed" and not b.released
                       and not b.void_failed and b.error != "hold_expired")
        tail = (f", {plural(len(expired), 'expired hold')} cannot be taken" if expired else "") + \
               (f", then release {plural(releases, 'failed hold')}" if releases else "")
        base = {"drop": d.id, "outcome": outcome, "holds": n, "resuming": d.state == "settling",
                "releases": releases, "expired": len(expired)}
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

        Before anything moves: joins and leaves left open are reconciled, and (when `expect`, the approved plan token, is
        given) the plan is worked out again under the lock and must match it exactly, releases included. A PayPal refusal
        that is about one buyer (an allow-list of names) is recorded against that buyer and settling goes on. Any other
        refusal, or two capture failures in a row with the same code, stops settlement with `settle_blocked` before
        anything else is captured or released, and leaves the drop settling for a person. A transient failure also stops
        it, to be resumed. Failed holds are released only once every held buyer has a final outcome."""
        with self.store.lock(drop, wait=False):
            d = self.get(drop)
            if d.state == "settled":
                return {**self._settled_numbers(d), "already_settled": True}
            if d.state not in ("closed", "settling"):
                raise CrowdError("drop_not_closed", drop=d.id, state=self.display_state(d))
            self._check_expect(d.id, expect)       # nothing moves unless the plan is the one that was approved
            self._reconcile(d.id)
            self._check_expect(d.id, expect)       # and again once any lost joins have been settled
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
        if expect is None:
            return
        actual = plan_token(self.plan(drop))
        if str(expect).strip() != actual:
            raise CrowdError("plan_changed", drop=drop, expected=str(expect).strip(), actual=actual)

    def _block(self, d: Drop, e: HoldError | None, b: Buyer | None, phase: str, message: str | None = None,
               code: str | None = None, **more) -> None:
        """Stop settlement for a person. Writes `settle_blocked` (PayPal's error name, http status and debug id when it
        gave them), touches nothing else, and raises. The drop stays `settling`; a later `settle` carries on from here, and
        the streak that tripped the breaker starts again from this event."""
        ev = self._evidence(e)
        code = code or ev.get("issue") or "settle_blocked"
        text = (message or (str(e) if e is not None else "") or code)[:200]
        payload = {"phase": phase, "code": code, "message": text, **ev, **more}
        if b is not None:
            payload.update(buyer=b.id, auth_id=b.auth_id)
        self.store.append(d.id, "settle_blocked", payload, ts=self.now())
        who = f" for {b.id}" if b is not None else ""
        shown = b.id if b is not None else (", ".join(str(x) for x in more.get("buyers", [])) or "the drop")
        raise CrowdError(
            "settle_blocked",
            f"settlement of {d.id} is stopped: {code}{who} while {phase}. {text} This is not something to retry blindly: "
            f"a person must look at it first.", drop=d.id, phase=phase, issue=ev.get("issue") or code,
            http=ev.get("http") if ev.get("http") is not None else "n/a", debug_id=ev.get("debug_id") or "none given",
            buyer=shown,
            next="a person (the maker who owns the PayPal account) must look at PayPal's answer, fix the cause, and approve "
                 "a fresh plan before settle is run again")

    def _check_breaker(self, d: Drop) -> None:
        """Two capture failures in a row with the same code are not two unlucky buyers: they share a cause. Stop before
        anything is released. The count starts again after every capture that goes through and after every block, so a
        person who looks and runs settle again is not stopped by the failures they already looked at."""
        streak, last = [], None
        for ev in self.store.events(d.id):
            t, p = ev["type"], ev["payload"]
            if t in ("settle_blocked", "hold_captured"):
                streak, last = [], None
            elif t == "capture_failed" and p.get("code") not in BREAKER_EXEMPT:
                code = p.get("issue") or p.get("code")
                if code == last:
                    streak.append(p.get("buyer"))
                else:
                    streak, last = [p.get("buyer")], code
        if len(streak) >= BREAKER_LIMIT:
            self._block(d, None, None, "capture", code=str(last), buyers=streak[-BREAKER_LIMIT:],
                        message=f"{len(streak)} captures in a row failed with {last}")

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
                c, recovered = self._recover(b), True
                if not self._same_amount(c, final):
                    self._block(d, None, b, "capture", code="captured_not_as_approved",
                                message=f"PayPal says the hold is {cur.get('status')}, but no capture of the final price can be "
                                        f"found for it.")
            else:
                c = self.holds.capture(b.auth_id, final, rid, final=True, currency=d.currency,
                                       simulate_decline=(b.card_index == DECLINE_CARD_INDEX))
        except AuthorizationAlreadyCaptured as e:
            c, recovered = self._recover(b), True      # captured on an earlier run that crashed before it was recorded
            if not self._same_amount(c, final):        # PayPal's word that it is captured, and nothing we can match to it
                self._block(d, e, b, "capture", message="PayPal says the hold was already captured, but no capture of the "
                                                        "final price can be found for it.")
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

    def _recover(self, b: Buyer) -> dict | None:
        try:
            return self.holds.recover_capture(b.order_id) if b.order_id else None
        except HoldError:
            return None

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
