"""What the pages ask for, built from the engine.

Public (the buyer's page): a published drop's price, crowd, ladder and clock; joining and leaving with the browser's own
secret (tokens.py says what a secret proves).
Maker (needs the maker key): the same plus the buyers, the activity feed, the settlement, any approval waiting in Togi, and
the two read-only checks against PayPal (verify, tie-out).

Nothing here invents money. Prices, totals, fees and counts are the engine's. Two figures are derived, both from the
engine's own Decimals and with the same subtraction the bundle's `drop explain` uses for `released_each`: how much less
than the start price everyone pays now (`saved_each`), and how much less than their hold a buyer was charged (`saved`).
The only other arithmetic is counting buyers by status.
"""
from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from crowd import ladder as lad
from crowd import verify as vf
from crowd.engine import CrowdError, Engine, m
from crowd.paypal_holds import HoldError, TEST_CARDS
from crowd.store import StoreError, iso, parse_iso, slug

from .feed import display_name, feed, how_released, may_be_open
from .tokens import Tokens, digest, well_formed

# The cards a web buyer may pick: sandbox test numbers, never a real card. Index 8 repeats index 0, and index 9 is the card
# whose capture the sandbox is told to fail (for the agent's tests), so the page offers 0 to 7.
WEB_CARDS = list(range(8))
PUBLIC_STATES = {"live", "ended", "closed", "settling", "settled"}
HOLDING = {"held", "captured", "capture_failed", "void_failed"}     # a buyer in one of these still has a hold (or a charge) on record
LOCK_WAIT = 10        # seconds a join or leave waits for the drop's turn (a slow PayPal call can hold it) before it answers 503, busy
CLAIM_WINDOW = 10     # seconds: a join the engine stamped this soon after a claim was written is that claim's own; a later one is not
CLAIM_SLACK = 1       # ... and one stamped this much BEFORE the claim (clocks, millisecond rounding) still is
MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}
FATE_STATUSES = ("left", "voided", "excluded")     # the buyer states whose words depend on what became of the hold (see `_fate`)

# What PayPal said when settlement stopped, in words a maker can act on. The code is shown beside it, for searching.
ISSUES = {
    "INSTRUMENT_DECLINED": "a buyer's card was declined", "TRANSACTION_REFUSED": "PayPal refused a charge",
    "AUTHORIZATION_EXPIRED": "a hold had already expired", "PAYER_ACTION_REQUIRED": "a buyer has to act at PayPal first",
    "PERMISSION_DENIED": "PayPal refused this app's access", "NOT_AUTHORIZED": "PayPal refused this app's access",
    "UNPROCESSABLE_ENTITY": "PayPal could not process a step", "INTERNAL_SERVER_ERROR": "PayPal had an internal error",
    "RATE_LIMIT_REACHED": "PayPal asked for fewer requests", "AUTHORIZATION_VOIDED": "a hold had already been released",
    "MAX_CAPTURE_AMOUNT_EXCEEDED": "a charge was larger than its hold",
}


# Characters a name may not hold: controls, invisible formatting (right-to-left overrides, zero-width), private use,
# unassigned and lone surrogates. They can reorder or hide what sits beside the name on the maker's screen.
HIDDEN = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def clean_name(name) -> str:
    """A buyer name the pages can show safely: visible characters only, no currency symbols (so a name cannot pose as an
    amount), at least one letter or digit, 1 to 60 characters. Spaces are folded the way the engine folds them."""
    if not isinstance(name, str):
        raise ApiError(400, "bad_buyer", "Use a name of 1 to 60 characters.")
    name = re.sub(r"\s+", " ", name).strip()
    if not 1 <= len(name) <= 60 or not any(c.isalnum() for c in name):
        raise ApiError(400, "bad_buyer", "Use a name of 1 to 60 characters, with at least one letter or number.")
    if any(unicodedata.category(c) in HIDDEN for c in name):
        raise ApiError(400, "bad_buyer", "Use visible letters, numbers and ordinary punctuation in your name.")
    if any(unicodedata.category(c) == "Sc" for c in name):
        raise ApiError(400, "bad_buyer", "Names cannot contain currency symbols.")
    return name


def load_details(path: str | None) -> dict:
    """Product details per drop title, from a JSON file the maker writes: {"Speckled mugs": {"what": "...", "ships": "...",
    "photo": "mugs.jpg"}}. A photo is a file beside the JSON (JPEG, PNG or WebP); only files named here are ever served."""
    if not path:
        return {}
    base = Path(path).resolve().parent
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for title, info in raw.items():
        if not isinstance(info, dict):
            continue
        row = {k: str(info[k])[:200] for k in ("what", "ships") if info.get(k)}
        photo = info.get("photo")
        if photo:
            f = (base / str(photo)).resolve()
            ctype = mimetypes.guess_type(f.name)[0]
            if f.is_file() and f.parent == base and ctype in MEDIA_TYPES:
                row["photo_file"], row["photo_type"] = str(f), ctype
        out[str(title).strip().lower()] = row
    return out


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


# Engine refusals, in words a buyer or maker can act on. Anything not listed keeps its code and a generic sentence.
MESSAGES = {
    "drop_not_found": (404, "This drop does not exist."),
    "drop_not_published": (404, "This drop does not exist."),
    "drop_closed": (409, "Joining has closed for this drop."),
    "drop_ended": (409, "Joining has closed for this drop."),
    "drop_not_live": (409, "This drop is no longer open."),
    "already_joined": (409, "That name is taken in this drop. Try another, for example with an initial."),
    "bad_buyer": (400, "Use a name of 1 to 60 characters."),
    "buyer_excluded": (409, "That name is taken in this drop. Try another, for example with an initial."),
    "join_declined": (402, "The card was declined, so nothing was held. Try another test card."),
    "paypal_unavailable": (503, "PayPal did not answer. If a hold was placed it will be released, never charged. Try again in a minute."),
    "paypal_error": (502, "PayPal refused the request. Nothing was held."),
    "earlier_attempt_unresolved": (409, "An earlier try with this name is still being sorted out with PayPal. Try again in a minute."),
    "join_in_progress": (409, "An earlier try with this name is still being sorted out with PayPal, so it cannot be used right now. "
                              "Try again in a minute."),
    "join_unconfirmed": (409, "Your earlier try may have placed a hold on your card, but it was never confirmed here, so this page "
                              "cannot show or release it. Please do not join again. The maker can see the hold and can release it."),
    "join_stale": (409, "This drop was reset since this page last asked to join. Reload the page and join again."),
    "join_over": (409, "Your earlier try is over, and nothing is held on your card now. If you still want in, join again."),
    "release_stuck": (409, "Your earlier try may have left a hold on your card that PayPal would not release or describe as planned. "
                           "This page cannot show or release it. The maker can see it and will sort it out. Please do not join again."),
    "busy": (503, "This drop is busy with other joins right now. Try again in a moment."),
    "not_recorded": (503, "Your join could not be recorded, so the hold was released. Try again."),
    "not_recorded_held": (503, "Your join could not be recorded, and we could not release the hold just now. The maker can see it "
                               "and will release it. Please do not join again."),
    "hold_not_open": (409, "PayPal did not confirm the hold, so you are not in. Nothing will be charged. Try again."),
    "wrong_paypal": (409, "This drop belongs to another PayPal setup."),
    "drop_unbound": (409, "This drop belongs to another PayPal setup."),
    "buyer_not_found": (404, "You are not in this drop."),
    "no_active_hold": (409, "You have no hold in this drop to release."),
    "leave_pending": (202, "Your leave is recorded. PayPal has not released the hold yet; it will be released, and you will not be charged."),
    "void_conflict": (409, "Your hold was already charged, so it cannot be released here. The maker can refund it."),
    "charged_before_leave": (409, "Your hold was already charged, so it cannot be released here. The maker can refund it."),
    "drop_not_settled": (409, "This drop is not settled yet."),
    "nothing_captured": (409, "Nothing was charged in this drop, so there is nothing to tie out."),
}


def from_engine(e: Exception) -> ApiError:
    code = getattr(e, "code", "error")
    status, message = MESSAGES.get(code, (400, "That did not work."))
    return ApiError(status, code, message)


def _refusal(code: str, message: str | None = None, **extra) -> ApiError:
    status, text = MESSAGES[code]
    return ApiError(status, code, message or text, **{k: v for k, v in extra.items() if v is not False})


# A drop that takes no joins, said to a buyer whose earlier try (this secret's claim) may still hold something at PayPal. Closing
# the drop releases every join nobody confirmed (D-11), so this is a promise the engine keeps.
UNSURE_CLOSED = "Joining has closed. If a hold was placed it will be released, and you will not be charged."


# An engine refusal that says "nothing was held", said to a page whose claim was NOT forgotten: the engine's log cannot show that, so
# the words must not either (a hold the claim's own earlier try placed may be open, or the engine could not be read).
UNSURE_REFUSED = ("That try did not go through, but this page cannot tell whether an earlier try placed a hold. Please do not join again "
                  "under another name; this page keeps checking, and the maker can see any hold.")
NOTHING_WAS_HELD = {"join_declined", "hold_not_open", "paypal_error"}


def _ends(events: list[dict]) -> dict:
    """(buyer, auth_id) -> (event type, payload) of the LAST event that says what became of that hold: released (`hold_voided`,
    `failed_hold_released`) or refused by PayPal (`void_failed`, not an orphan's). A hold is the pair, not the buyer: a buyer who
    left, joined again and left again has had two."""
    out = {}
    for e in events:
        p = e["payload"]
        if e["type"] in ("hold_voided", "failed_hold_released") or (e["type"] == "void_failed" and not p.get("orphan")):
            if p.get("buyer") and p.get("auth_id"):
                out[(p["buyer"], p["auth_id"])] = (e["type"], p)
    return out


def _fate(b, ends: dict) -> str | None:
    """What became of a buyer's current hold, for a buyer who is out of the crowd, as the engine's log says it (never guessed):
      released        released at PayPal (a leave, or the maker taking the buyer out, went through)
      not_released    PayPal REFUSED to release it (`void_failed`): it may still be open on the buyer's card
      missed_minimum  released because the drop missed its minimum
      lapsed          ran out at PayPal
      gone            PayPal no longer shows it (voided elsewhere, or unknown to it)
      taken_outside   money was taken from it outside the engine (the maker can refund it)
    None: out of the crowd with no word on the hold. Only `left`, `voided` and `excluded` buyers have a fate; a buyer who left
    and the log does not say otherwise was released."""
    if b.status not in FATE_STATUSES:
        return None
    end = ends.get((b.id, b.auth_id)) if b.auth_id else None
    if end is None:
        return "released" if b.status == "left" else None
    kind, p = end
    if kind == "void_failed":
        return "not_released"
    return how_released(p, kind)


def _card_label(i) -> str | None:
    try:
        return "Test card ending " + TEST_CARDS[int(i)]["number"][-4:]
    except (TypeError, ValueError, IndexError):
        return None


CARDS = [{"index": i, "label": _card_label(i)} for i in WEB_CARDS]


class Api:
    def __init__(self, engine: Engine | None = None, maker_name: str | None = None, togi_home: str | None = None,
                 details: dict | None = None):
        self.engine = engine or Engine()
        self.maker_name = maker_name
        self.mode = self.engine.paypal_key()[0]          # "fake" or "sandbox": the pages say which, every time
        self.details = details or {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.media = {str(i): (r["photo_file"], r["photo_type"], title)       # key -> (file, type, the title it belongs to)
                      for i, (title, r) in enumerate(self.details.items()) if r.get("photo_file")}
        self.tokens = Tokens(self.engine.store.path + ".web.db")
        self.togi_home = Path(togi_home or os.environ.get("TOGI_HOME") or Path.home() / ".togi").expanduser()

    # ---- reading ------------------------------------------------------------------------------------

    def _drop(self, drop: str):
        try:
            d = self.engine.get(str(drop))
        except (CrowdError, StoreError) as e:
            raise from_engine(e) from None
        if d.state == "draft":                     # a draft is the maker's, not yet anyone else's
            raise ApiError(404, "drop_not_found", MESSAGES["drop_not_found"][1])
        return d

    def _lock(self, drop: str) -> threading.Lock:
        """One web request at a time per drop for joins and leaves, so two browsers can never interleave a claim and a join."""
        with self._locks_guard:
            return self._locks.setdefault(drop, threading.Lock())

    @contextlib.contextmanager
    def _turn(self, drop: str, on_turn=None):
        """Wait for the drop's turn, but not for ever: a request queued behind a slow PayPal call answers 503 (busy) and the page,
        which keeps its secret on a 503, asks again. Nothing has been written when it gives up. `on_turn` is called once the turn is
        won: the server counts a request against its address until then (server.py), because a queue is not free."""
        lock = self._lock(drop)
        if not lock.acquire(timeout=LOCK_WAIT):
            raise _refusal("busy")
        try:
            if on_turn is not None:
                on_turn()
            yield
        finally:
            lock.release()

    def info(self) -> dict:
        return {"paypal": self.mode, "maker_name": self.maker_name}

    def photo(self, key: str) -> tuple[bytes, str]:
        """A product photo, but only for a drop that is public: some published drop has the title the photo was written for."""
        entry = self.media.get(key)
        if entry is None or not self._published(entry[2]):
            raise ApiError(404, "not_found", "Nothing here.")
        f, ctype, _ = entry
        return Path(f).read_bytes(), ctype

    def _published(self, title: str) -> bool:
        for i in reversed(self.engine.store.drop_ids()):
            try:
                d = self.engine.get(i)
            except (CrowdError, StoreError):
                continue
            if d.state != "draft" and d.title.strip().lower() == title:
                return True
        return False

    def _details(self, d) -> dict | None:
        title = d.title.strip().lower()
        row = self.details.get(title)
        if not row:
            return None
        out = {k: row[k] for k in ("what", "ships") if row.get(k)}
        for k, (_f, _ctype, t) in self.media.items():
            if t == title:                                   # the photo written for THIS title, never another's with the same file
                out["photo"] = f"/media/{k}"
        return out

    def _maker_name(self, d) -> str | None:
        return self.maker_name or (d.maker if d.maker and d.maker != "maker" else None)

    def _public(self, d) -> dict:
        v = self.engine.view(d)
        out = {k: v.get(k) for k in ("drop", "title", "state", "currency", "price", "start_price", "buyers", "minimum",
                                      "minimum_met", "locked_step", "next_step", "next_message", "ends_at", "time_left",
                                      "time_left_seconds", "outcome")}
        out["maker_name"] = self._maker_name(d)
        out["paypal"] = self.mode
        out["details"] = self._details(d)
        start = d.ladder.start_price
        final = lad.D(d.settled["final_price"]) if d.settled and d.settled.get("outcome") == "funded" else None
        now = final if final is not None else (d.price() if d.count >= d.minimum else start)
        out["saved_each"] = m(start - now) if now < start else None
        nxt = v.get("next_step")                     # what the next step will save everyone (same subtraction, engine numbers)
        out["next_saving"] = (m(start - lad.D(nxt["price"])) if nxt and d.count >= d.minimum and lad.D(nxt["price"]) < start
                              else None)
        out["steps"] = [{"step": i, "from_buyers": s.min_buyers, "price": m(s.price)} for i, s in enumerate(d.ladder.steps)]
        out["joinable"] = v["state"] == "live"
        out["life"] = d.created_at                  # which life of this drop id it is: a page that asked in an earlier life must not join this one
        if d.settled:
            s = d.settled
            out["settled"] = {"outcome": s["outcome"], "final_price": s["final_price"],
                              "paid": (s.get("counts") or {}).get("captured", 0)}
        if d.state == "settled":                    # where a buyer can go next
            out["others"] = self._others(d)
        out["server_time"] = datetime.now(timezone.utc).isoformat()
        return out

    def _others(self, d) -> list[dict]:
        """The same maker's other drops that are open now: where a buyer can go next."""
        out = []
        for i in reversed(self.engine.store.drop_ids()):
            if i == d.id or len(out) >= 3:
                continue
            try:
                o = self.engine.get(i)
            except (CrowdError, StoreError):
                continue
            if self.engine.display_state(o) == "live" and o.maker == d.maker:
                out.append({"drop": o.id, "title": o.title, "price": m(o.price())})
        return out

    def drops(self) -> list[dict]:
        out = []
        for i in reversed(self.engine.store.drop_ids()):
            try:
                d = self.engine.get(i)
            except (CrowdError, StoreError):
                continue
            if d.state != "draft":
                v = self._public(d)
                out.append({k: v[k] for k in ("drop", "title", "state", "price", "buyers", "minimum", "time_left")})
        return out

    def drop(self, drop: str) -> dict:
        return self._public(self._drop(drop))

    def cards(self) -> list[dict]:
        return CARDS

    # ---- the buyer -----------------------------------------------------------------------------------

    def _mine(self, d, bid: str) -> dict:
        b = d.buyers.get(bid)
        if b is None:
            raise ApiError(404, "buyer_not_found", MESSAGES["buyer_not_found"][1])
        live = self.engine.display_state(d) == "live"
        saved = (m(b.hold_amount - b.captured) if b.captured is not None and b.hold_amount is not None
                 and b.hold_amount > b.captured else None)
        return {"buyer": b.id, "name": display_name(b.name), "status": b.status, "card": _card_label(b.card_index),
                "held": m(b.hold_amount) if b.status == "held" and b.hold_amount is not None else None,
                "charged": m(b.captured) if b.captured is not None else None, "saved": saved,
                "start_price": m(d.ladder.start_price), "can_leave": live and b.status == "held",
                "leaving": b.id in d.leaving, "fate": self._fate_of(d, b)}

    def _fate_of(self, d, b) -> str | None:
        """`_fate` for one buyer's page. The log is read only for a buyer whose words depend on it: a buyer who left and whose release
        the engine never saw refused needs no look."""
        if b.status == "left" and not b.void_failed:
            return "released"
        if b.status not in FATE_STATUSES:
            return None
        return _fate(b, _ends(self.engine.store.events(d.id)))

    def _owned(self, d, buyer, secret):
        b = d.buyers.get(buyer) if isinstance(buyer, str) else None
        if b is None or not self.tokens.owns(d.id, buyer, secret, d.created_at, b.auth_id):
            raise ApiError(403, "not_yours", "This browser does not hold that join.")
        return b

    def join(self, drop: str, name, card, secret, admit=None) -> dict:
        """Hold the start price for a web buyer. `secret` is made by the browser before it asks, so asking again after a lost
        reply hands back the same join instead of placing a second hold.

        The rules that keep one join intent from becoming two holds, and a hold from being released by someone who did not place
        it (tokens.py has the other half):
          * A retry whose secret already proves the buyer's hold is handed back whatever state that hold is in now, before anything
            else is looked at: never refused, never counted against a limit, never able to touch its own proof.
          * A claim is this secret's record of ONE engine join attempt. What became of that attempt is read from the engine's log
            (`_story`), never guessed: it is open (PayPal may have acted on it), it placed the hold on record (`join_unconfirmed`:
            worded by what is on record, held or charged; the maker sees it), it left a hold PayPal would not release
            (`release_stuck`), or it is over (released, failed, its hold gone). A claim that is over never starts a join again
            (`join_over`: nothing is held now, join again if you still want in), however often the page asks and whatever happened
            to the answer. A hold on record that the claim's attempt did NOT place is someone else's: it is no reason to say a
            thing about the buyer's money.
          * A join the engine has open for the buyer is this claim's own only if it is the claim's attempt AND the engine stamped it
            within moments of the claim being written (both on the engine's clock). A claim whose process died before the engine
            heard of it proves nothing about a join made much later by anyone else.
          * Another secret cannot join a name while the engine has a join for it that PayPal may have acted on and nobody has
            confirmed: it would be handed that earlier request's hold (`join_in_progress`). Nor can this one, unless every such
            join is its own.
          * A drop that takes no joins is refused before any claim is written.
          * `admit` (the server's rate limit) is asked last, only for a request that will really reach PayPal: junk, hand-backs and
            refusals made from our own records never use up a buyer's allowance. If it has a `peek`, a request the limit would turn
            away is refused before it waits for the drop's turn; if it has a `turn`, that is called when the turn is won.
          * The engine is read again just before it is asked (after the claim is written), and the join is not started if anything
            about the buyer moved; after it answers, a hold that someone else also asked the engine for is not bound.
        A refusal that shows this secret holds nothing says so (`nothing_held`), so the page can let go of the secret; one that cannot
        (this secret's attempt is open, or placed a hold) does not, and the page lets go of the secret only on that mark (or on input
        refused before any claim was written; drop.js): a refusal that keeps the claim never promises that nothing is held."""
        d = self._drop(drop)
        if not isinstance(card, int) or isinstance(card, bool) or card not in WEB_CARDS:
            raise ApiError(400, "bad_card", "Pick one of the test cards.")
        if not well_formed(secret):
            raise ApiError(400, "bad_secret", "Reload the page and try again.")
        name = clean_name(name)
        bid = slug(name)
        again = self._handed_back(d, bid, secret)
        if again is not None:                         # a retry of a join that went through is only read: it never waits for anyone
            return again
        if admit is not None and getattr(admit, "peek", None) is not None:
            admit.peek()                              # a request the limit would turn away does not queue behind everyone else's
        with self._turn(d.id, getattr(admit, "turn", None)):
            d = self.engine.get(d.id)
            again = self._handed_back(d, bid, secret)
            if again is not None:
                return again
            if self.tokens.retired(d.id, secret, d.created_at):       # its attempt ended, or its claim was overwritten: never again
                raise _refusal("join_over", nothing_held=True)
            b = d.buyers.get(bid)
            theirs = self.tokens.own(d.id, bid, secret)
            if theirs is not None and theirs["drop_created"] != d.created_at:      # asked in an earlier life of this drop id (a reset)
                raise _refusal("join_stale", nothing_held=True)
            claim = self.tokens.claim_of(d.id, bid, secret, d.created_at)
            legacy = claim is not None and claim["attempt"] is None                # from before attempts were recorded: proves nothing
            intents = [j for j in d.unresolved.values() if j.get("buyer") == bid]
            ours, beside, stuck, over = [], False, False, False
            if legacy:
                beside = b is not None and b.status in HOLDING
            elif claim is not None:
                story = self._story(d, self.engine.store.events(d.id), bid, claim["attempt"], claim["created"])
                ours = [j for j in intents if story["own"] and int(j.get("attempt") or 0) == claim["attempt"]]
                beside, stuck = self._beside(b, story), story["stuck"]
                over = b is not None and b.attempts >= claim["attempt"] and not beside and not stuck    # the engine has moved on
            if beside:
                raise self._unconfirmed(b)
            if stuck:
                raise _refusal("release_stuck")
            if b is not None and b.status == "held":
                raise _refusal("already_joined", nothing_held=True)
            state = self.engine.display_state(d)
            if state != "live":
                code = "drop_ended" if state == "ended" else "drop_closed"
                unsure = bool(ours) or (legacy and bool(intents))        # this secret's claim sits beside a join PayPal may have acted on
                raise _refusal(code, UNSURE_CLOSED) if unsure else _refusal(code, nothing_held=True)
            if len(ours) != len(intents):
                raise _refusal("join_in_progress", nothing_held=not (ours or legacy))
            if over:
                self.tokens.retire(d.id, secret, d.created_at)
                raise _refusal("join_over", nothing_held=True)
            if self._stranger_beside(d, bid, secret):
                raise _refusal("join_in_progress", nothing_held=True)
            if admit is not None:
                admit()
            attempt = (b.attempts if b is not None else 0) + 1
            fresh = not ours                          # a claim whose own join is open is asked again as it is; any other is written anew
            if fresh:
                still_there = b.auth_id if b is not None and b.status in HOLDING else None
                if not self.tokens.claim(d.id, bid, secret, d.created_at, lambda: iso(self.engine.now()), still_there, attempt):
                    raise _refusal("already_joined", nothing_held=True)
            # The last look before PayPal is asked. Anything another actor (the assistant, the maker) did to this buyer between the
            # first look and now makes this a different join: do not start it, say so, and let the page ask again. Whatever is left of
            # the window between this look and the engine taking its file lock is the engine's, and is closed after the fact below.
            try:
                seen = self.engine.store.events(d.id)                 # the log first, then the state: whatever lands between the two is
                mark = seen[-1]["id"] if seen else 0                  # in the state (and stops the join below), and whatever lands after
                d2 = self.engine.get(d.id)                            # the mark is counted when the engine has answered
            except Exception:                         # noqa: BLE001 - nothing was started: the claim goes with it
                if fresh:
                    self.tokens.drop_claim(d.id, bid, secret)
                raise
            if self._signature(d2, bid) != self._signature(d, bid):
                if fresh:
                    self.tokens.drop_claim(d.id, bid, secret)
                raise _refusal("busy")
            try:
                out = self.engine.join(d.id, name, card)
            except (CrowdError, StoreError, HoldError) as e:
                err = from_engine(e)
                if self._forget_untraced(d.id, bid, secret, mark, fresh):          # refused outright: nothing is held, nothing to remember
                    err.extra["nothing_held"] = True
                elif err.code in NOTHING_WAS_HELD:                                 # the claim stays: the log does not show nothing is held
                    err.message = UNSURE_REFUSED
                raise err from None
            except Exception:  # noqa: BLE001 - a crash the engine did not name: forget the claim only if the engine never heard of the try
                self._forget_untraced(d.id, bid, secret, mark, fresh)
                raise
            d = self.engine.get(d.id)
            asked = self._joins_since(self.engine.store.events(d.id), bid, mark)
            if len(asked) != len(set(asked)):
                # Someone else asked the engine for this very join (the same request id) between the last look and the engine's lock:
                # it answered both with one hold, and nothing says whose it is. It is not bound; the maker sees it.
                raise self._unconfirmed(d.buyers[out["buyer"]])
            try:
                bound = self.tokens.bind(d.id, out["buyer"], secret, out["auth_id"])
            except Exception:  # noqa: BLE001 - a hold nobody on the web can prove is released at once
                bound = False
            if not bound:
                released = self._release(d.id, out["buyer"]) == "released"
                raise _refusal("not_recorded", nothing_held=True) if released else _refusal("not_recorded_held")
            d = self.engine.get(d.id)
            return {"buyer": out["buyer"], "me": self._mine(d, out["buyer"]), "drop": self._public(d)}

    def _stranger_beside(self, d, bid: str, secret) -> bool:
        """Is there another secret's unbound claim on this name whose attempt left a hold PayPal would not release, or is still open?
        Writing this secret's claim would overwrite it, and with it the only record that the hold may be that browser's: the page that
        made it would then be told nothing is held."""
        old = self.tokens.row(d.id, bid)
        if (old is None or old["auth_id"] is not None or old["drop_created"] != d.created_at or old["attempt"] is None
                or old["secret_sha256"] == digest(secret)):
            return False
        story = self._story(d, self.engine.store.events(d.id), bid, old["attempt"], old["created"])
        return story["stuck"] or story["open"]

    def _handed_back(self, d, bid: str, secret) -> dict | None:
        """The answer to a retry of a join that went through (this secret proves the buyer's hold), or None. It only reads."""
        b = d.buyers.get(bid)
        if b is not None and self.tokens.owns(d.id, bid, secret, d.created_at, b.auth_id):
            return {"buyer": bid, "me": self._mine(d, bid), "drop": self._public(d), "again": True}
        return None

    @staticmethod
    def _open_intent(d, bid: str) -> bool:
        """Has the engine a join for this buyer that PayPal may have acted on and nobody has confirmed or released?"""
        return any(j.get("buyer") == bid for j in d.unresolved.values())

    @staticmethod
    def _signature(d, bid: str) -> tuple:
        """What a join depends on, for one buyer: how many attempts the engine has closed, the buyer's status and hold, and which
        joins are open. If it differs between two looks, someone else acted in between."""
        b = d.buyers.get(bid)
        return (b.attempts if b else 0, b.status if b else None, b.auth_id if b else None,
                tuple(sorted(r for r, j in d.unresolved.items() if j.get("buyer") == bid)))

    @staticmethod
    def _joins_since(events: list[dict], bid: str, mark: int) -> list[str]:
        """The request ids of the joins started for this buyer after the event `mark`. The engine holds the drop's file lock from the
        moment it writes one to the moment it has answered, so after one request's own join no other can be written before it is done:
        the same id twice means another actor asked for the very same join first."""
        return [e["payload"].get("request_id") for e in events
                if e["id"] > mark and e["type"] == "join_started" and e["payload"].get("buyer") == bid]

    @staticmethod
    def _story(d, events: list[dict], bid: str, attempt: int, created: str | None, anywhere: bool = False) -> dict:
        """What the engine's log says became of the join attempt a claim started: {own, open, placed, stuck, ended}.

        `own`: the engine stamped a join for this buyer and attempt within CLAIM_WINDOW of the claim being written, so it is what the
        claim started (`anywhere`: the buyer's newest join, whenever and whatever attempt, for a request that has just asked the engine
        itself). `open`: PayPal may have acted on it and nobody has confirmed or released it. `placed`: the auth id of the hold it
        placed. `stuck`: it ended with a hold PayPal would not release or read. `ended`: it is closed, one way or another."""
        out = {"own": False, "open": False, "placed": None, "stuck": False, "ended": False, "attempt": None}
        try:
            made = parse_iso(created) if created else None
        except (TypeError, ValueError):
            made = None
        start = None
        for i, e in enumerate(events):
            p = e["payload"]
            if e["type"] != "join_started" or p.get("buyer") != bid:
                continue
            if anywhere:
                start = i                                          # keep going: the newest is the one
                continue
            if int(p.get("attempt") or 0) != attempt:
                continue
            try:
                gap = (parse_iso(e["ts"]) - made).total_seconds()
            except (TypeError, ValueError, AttributeError):
                continue
            if -CLAIM_SLACK <= gap <= CLAIM_WINDOW:
                start = i
                break
        if start is None:
            return out
        rid, out["own"], out["attempt"] = events[start]["payload"].get("request_id"), True, int(events[start]["payload"].get("attempt") or 0)
        for e in events[start + 1:]:
            p = e["payload"]
            if p.get("request_id") != rid:
                continue
            if e["type"] == "hold_placed":
                out["placed"], out["ended"] = p.get("auth_id"), True
            elif e["type"] == "hold_failed":
                out["ended"] = True
                out["stuck"] = out["stuck"] or may_be_open(p)
            elif e["type"] == "orphan_hold_released" and not p.get("superseded"):
                out["ended"] = True
        out["open"] = not out["ended"] and rid in d.unresolved
        return out

    @staticmethod
    def _beside(b, story: dict) -> bool:
        """Is the hold the engine has on record for this buyer (held, charged, or stuck) the one the claim's own attempt placed?"""
        return b is not None and b.status in HOLDING and story["placed"] is not None and story["placed"] == b.auth_id

    def _forget_untraced(self, drop: str, bid: str, secret: str, mark: int, fresh: bool) -> bool:
        """After a join that raised: forget this secret's unbound claim, unless the engine has a join this request started on record
        and it is open, placed a hold, or left one PayPal would not release; or unless this was a retry of a claim whose own earlier
        join is open (that join stands, whatever this ask came to). True when the claim is gone: nothing of the try is held anywhere.
        If the engine cannot be read, nothing is forgotten and nothing is claimed."""
        try:
            d = self.engine.get(drop)
            events = self.engine.store.events(drop)
            claim = self.tokens.claim_of(d.id, bid, secret, d.created_at)
            if claim is None:
                return not self._open_intent(d, bid)
            if self._joins_since(events, bid, mark):               # this request's own ask reached the engine's log
                story = self._story(d, events, bid, claim["attempt"], claim["created"], anywhere=True)
                if story["open"] or story["placed"] is not None or story["stuck"]:
                    if story["attempt"] and story["attempt"] != claim["attempt"]:     # the engine numbered it differently (another actor's
                        self.tokens.retag(drop, bid, secret, story["attempt"])        # try closed in between): the claim follows the engine
                    return False
            elif not fresh:
                return False
            self.tokens.drop_claim(drop, bid, secret)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _unconfirmed(b) -> ApiError:
        """The web cannot say what an earlier try of this secret came to, but the engine has money on record for the buyer: say what
        it is, and that the maker can see it."""
        if b.status == "captured":
            what = (f"Your earlier try may have been charged ${m(b.captured)}. This page never confirmed it, so it cannot "
                    "show or release it. The maker can see the charge and refund it. Please do not join again.")
        elif b.status in ("capture_failed", "void_failed"):
            what = ("Your earlier try left a hold on your card that could not be charged or released as planned. This page never "
                    "confirmed it. The maker can see it and will sort it out. Please do not join again.")
        else:
            what = MESSAGES["join_unconfirmed"][1]
        return _refusal("join_unconfirmed", what)

    def _release(self, drop: str, buyer: str) -> str:
        """Ask the engine to release a hold the web cannot prove: 'released', 'pending' (the leave is on record, PayPal has not
        voided it yet) or 'refused'. Close and settle honour a recorded leave (D-11b); a refused one is the maker's to sort out."""
        try:
            self.engine.leave(drop, buyer)
        except CrowdError as e:
            return "pending" if e.code == "leave_pending" else "refused"
        except (StoreError, HoldError):
            return "refused"
        return "released"

    def me(self, drop: str, buyer, secret) -> dict:
        d = self._drop(drop)
        self._owned(d, buyer, secret)
        return {"me": self._mine(d, buyer), "drop": self._public(d)}

    def leave(self, drop: str, buyer, secret, on_turn=None) -> dict:
        """Release the hold this browser placed. Whether the hold is this browser's is checked on a fresh read BEFORE waiting for the
        drop's turn, so a request that is not (anyone's guess at a buyer or a secret) never queues behind a slow PayPal call or takes a
        place in the line from a buyer who owns a hold; it is checked again once the turn is won."""
        d = self._drop(drop)
        self._owned(d, buyer, secret)
        with self._turn(d.id, on_turn):
            d = self.engine.get(d.id)
            self._owned(d, buyer, secret)
            try:
                out = self.engine.leave(d.id, buyer)
            except (CrowdError, StoreError, HoldError) as e:
                err = from_engine(e)
                if err.code != "leave_pending":
                    raise err from None
                out = {"pending": True}
            d = self.engine.get(d.id)
            return {"released": out.get("released"), "pending": bool(out.get("pending")), "me": self._mine(d, buyer),
                    "drop": self._public(d)}

    # ---- the maker -----------------------------------------------------------------------------------

    def approvals(self, drop: str) -> list[dict]:
        """Settlement approvals Togi is holding for this drop, read (never written) from $TOGI_HOME/approvals. The person
        approves in their own terminal; this only shows that one is waiting, and for what."""
        out = []
        folder = self.togi_home / "approvals"
        for f in (sorted(folder.glob("ap_*.json")) if folder.is_dir() else []):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            try:          # one malformed record is skipped; it never takes the maker's view down
                args = rec.get("args") or {}
                if rec.get("bundle") != "crowd" or rec.get("command") != "drop settle" or args.get("drop") != drop:
                    continue
                status = str(rec.get("status", "pending"))
                if status not in ("denied", "consumed") and time.time() > _epoch(str(rec["expires"])):
                    status = "expired"
                out.append({"id": str(rec.get("id")), "status": status, "expect": str(args.get("expect") or ""),
                            "created": str(rec.get("created") or ""), "expires": str(rec.get("expires") or "")})
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda a: a.get("created") or "", reverse=True)

    def maker(self, drop: str) -> dict:
        d = self._drop(drop)
        out = self._public(d)
        rows = self.engine.buyers(d.id)
        events = self.engine.store.events(d.id)
        unsure = self._unconfirmed_web_joins(d, events)        # a web join whose hold was never bound to its browser: only the maker can see it
        failed = {e["payload"].get("buyer"): e["payload"] for e in events if e["type"] == "hold_failed"}    # what each buyer's last failure was
        ends = _ends(events)
        out["buyers_list"] = [{"name": display_name(r["name"]) + (" (unconfirmed web join)" if r["buyer"] in unsure else ""),
                               "status": r["status"], "held": r["held"], "charged": r["charged"], "card": _card_label(r["card"]),
                               "error": r["error"],      # the engine's own code for a failure, so the maker is not told "declined" for what is not
                               "hold_may_be_open": r["status"] == "hold_failed" and may_be_open(failed.get(r["buyer"]) or {}),
                               "fate": _fate(d.buyers[r["buyer"]], ends),     # what became of the hold of a buyer who is out (see `_fate`)
                               "unconfirmed_web_join": r["buyer"] in unsure} for r in rows]
        out["simulated"] = sum(1 for r in rows if display_name(r["name"]) != r["name"])
        counts: dict = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out["buyers_by_status"] = counts
        out["feed"] = feed(events, {b.id: b.name for b in d.buyers.values()}, d.title)
        out["approvals"] = self.approvals(d.id)
        if d.costs:
            out["profit"] = [{"from_buyers": r["min_buyers"], "price": m(r["price"]), "profit": m(r["profit"])}
                             for r in lad.profit_table(d.ladder, d.costs["unit_cost"], d.costs["fixed_cost"])]
        if d.closed:
            out["closed"] = {k: d.closed.get(k) for k in ("reason", "count", "funded", "price", "closed_at")}
        if d.settled:
            s = d.settled
            out["settlement"] = {"outcome": s["outcome"], "final_price": s["final_price"], "counts": s.get("counts"),
                                 "totals": s.get("totals"), "shortfall": s.get("shortfall", 0),
                                 "unreleased": s.get("unreleased", 0)}
        elif d.settlement:
            out["settlement"] = {"outcome": d.settlement["outcome"], "final_price": d.settlement["final_price"],
                                 "in_progress": True}
        if d.blocked:
            out["blocked"] = {k: d.blocked.get(k) for k in ("issue", "phase", "at") if d.blocked.get(k) is not None}
            issue = str(d.blocked.get("issue") or d.blocked.get("code") or "")
            out["blocked"]["words"] = ISSUES.get(issue.upper(), "PayPal refused a step")
        return out

    def _unconfirmed_web_joins(self, d, events: list[dict]) -> set[str]:
        """The buyers whose hold (or charge, or hold PayPal would not release) sits beside a web claim that never got bound: the claim's
        own attempt placed it, or left it. A claim whose attempt placed nothing of the sort (it was released, the hold on record is
        the assistant's, the claim never reached the engine) marks nobody."""
        out = set()
        for bid, claim in self.tokens.unbound_claims(d.id, d.created_at).items():
            b = d.buyers.get(bid)
            if claim["attempt"] is None:                       # from before attempts were recorded: it may be
                if b is not None and b.status in HOLDING:
                    out.add(bid)
                continue
            story = self._story(d, events, bid, claim["attempt"], claim["created"])
            if self._beside(b, story) or story["stuck"]:
                out.add(bid)
        return out

    def verify(self, drop: str) -> dict:
        d = self._drop(drop)
        try:
            r = vf.verify(d.id, self.engine)
        except (CrowdError, StoreError, HoldError) as e:
            raise from_engine(e) from None
        return {"ok": bool(r.get("ok")), "checked_at": datetime.now(timezone.utc).isoformat(), "report": _plain(r)}

    def tieout(self, drop: str) -> dict:
        d = self._drop(drop)
        try:
            r = vf.tieout(d.id, self.engine)
        except (CrowdError, StoreError, HoldError) as e:
            raise from_engine(e) from None
        return {"checked_at": datetime.now(timezone.utc).isoformat(), "report": _plain(r)}


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _plain(x):
    """A report as JSON-safe values (the engine's Decimals become their exact strings)."""
    return json.loads(json.dumps(x, default=str))
