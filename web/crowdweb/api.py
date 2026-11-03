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
from crowd.store import StoreError, slug

from .feed import display_name, feed
from .tokens import Tokens, well_formed

# The cards a web buyer may pick: sandbox test numbers, never a real card. Index 8 repeats index 0, and index 9 is the card
# whose capture the sandbox is told to fail (for the agent's tests), so the page offers 0 to 7.
WEB_CARDS = list(range(8))
PUBLIC_STATES = {"live", "ended", "closed", "settling", "settled"}
MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}

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
        self.media = {str(i): (r["photo_file"], r["photo_type"])
                      for i, r in enumerate(self.details.values()) if r.get("photo_file")}
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

    def info(self) -> dict:
        return {"paypal": self.mode, "maker_name": self.maker_name}

    def photo(self, key: str) -> tuple[bytes, str]:
        if key not in self.media:
            raise ApiError(404, "not_found", "Nothing here.")
        f, ctype = self.media[key]
        return Path(f).read_bytes(), ctype

    def _details(self, d) -> dict | None:
        row = self.details.get(d.title.strip().lower())
        if not row:
            return None
        out = {k: row[k] for k in ("what", "ships") if row.get(k)}
        for k, (f, _) in self.media.items():
            if f == row.get("photo_file"):
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
        out["steps"] = [{"step": i, "from_buyers": s.min_buyers, "price": m(s.price)} for i, s in enumerate(d.ladder.steps)]
        out["joinable"] = v["state"] == "live"
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
                "leaving": b.id in d.leaving}

    def _owned(self, d, buyer, secret):
        b = d.buyers.get(buyer) if isinstance(buyer, str) else None
        if b is None or not self.tokens.owns(d.id, buyer, secret, d.created_at, b.auth_id):
            raise ApiError(403, "not_yours", "This browser does not hold that join.")
        return b

    def join(self, drop: str, name, card, secret) -> dict:
        """Hold the start price for a web buyer. `secret` is made by the browser before it asks, so asking again after a lost
        reply hands back the same join instead of placing a second hold."""
        d = self._drop(drop)
        if not isinstance(card, int) or isinstance(card, bool) or card not in WEB_CARDS:
            raise ApiError(400, "bad_card", "Pick one of the test cards.")
        if not well_formed(secret):
            raise ApiError(400, "bad_secret", "Reload the page and try again.")
        name = clean_name(name)
        bid = slug(name)
        with self._lock(d.id):
            d = self.engine.get(d.id)
            b = d.buyers.get(bid)
            if b is not None and b.status == "held":
                if self.tokens.owns(d.id, bid, secret, d.created_at, b.auth_id):     # a retry of a join that went through
                    return {"buyer": bid, "me": self._mine(d, bid), "drop": self._public(d), "again": True}
                raise ApiError(409, "already_joined", MESSAGES["already_joined"][1])
            self.tokens.claim(d.id, bid, secret, d.created_at, self.engine.now().isoformat())
            try:
                out = self.engine.join(d.id, name, card)
            except (CrowdError, StoreError, HoldError) as e:
                err = from_engine(e)
                if err.code not in ("paypal_unavailable", "earlier_attempt_unresolved"):   # refused outright: nothing is held
                    self.tokens.drop_claim(d.id, bid, secret)
                raise err from None
            try:
                bound = self.tokens.bind(d.id, out["buyer"], secret, out["auth_id"])
            except Exception:  # noqa: BLE001 - a hold nobody on the web can prove is released at once
                bound = False
            if not bound:
                self._release(d.id, out["buyer"])
                raise ApiError(503, "not_recorded", "Your join could not be recorded, so the hold was released. Try again.")
            d = self.engine.get(d.id)
            return {"buyer": out["buyer"], "me": self._mine(d, out["buyer"]), "drop": self._public(d)}

    def _release(self, drop: str, buyer: str) -> None:
        try:
            self.engine.leave(drop, buyer)
        except (CrowdError, StoreError, HoldError):
            pass        # a leave the engine could not finish stays on record; close and settle honour it (D-11b)

    def me(self, drop: str, buyer, secret) -> dict:
        d = self._drop(drop)
        self._owned(d, buyer, secret)
        return {"me": self._mine(d, buyer), "drop": self._public(d)}

    def leave(self, drop: str, buyer, secret) -> dict:
        d = self._drop(drop)
        with self._lock(d.id):
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
        out["buyers_list"] = [{"name": display_name(r["name"]), "status": r["status"], "held": r["held"],
                               "charged": r["charged"], "card": _card_label(r["card"])} for r in rows]
        out["simulated"] = sum(1 for r in rows if display_name(r["name"]) != r["name"])
        counts: dict = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out["buyers_by_status"] = counts
        out["feed"] = feed(self.engine.store.events(d.id), {b.id: b.name for b in d.buyers.values()}, d.title)
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
