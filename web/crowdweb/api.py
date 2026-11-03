"""What the pages ask for, built from the engine.

Public (the buyer's page): a published drop's price, crowd, ladder and clock; joining and leaving with a buyer token.
Maker (needs the maker key): the same plus the buyers, the activity feed, the settlement, any approval waiting in Togi, and
the two read-only checks against PayPal (verify, tie-out).

Nothing here computes money. Prices, totals, fees and counts are the engine's; the only arithmetic is counting buyers by
status for a summary line.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from crowd import ladder as lad
from crowd import verify as vf
from crowd.engine import CrowdError, Engine, m
from crowd.paypal_holds import HoldError, TEST_CARDS
from crowd.store import StoreError

from .feed import feed
from .tokens import Tokens

# The cards a web buyer may pick: sandbox test numbers, never a real card. Index 8 repeats index 0, and index 9 is the card
# whose capture the sandbox is told to fail (for the agent's tests), so the page offers 0 to 7.
WEB_CARDS = list(range(8))
PUBLIC_STATES = {"live", "ended", "closed", "settling", "settled"}


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
    "already_joined": (409, "Someone with that name is already in this drop. Add an initial to your name."),
    "bad_buyer": (400, "Use a name of 1 to 60 characters."),
    "buyer_excluded": (409, "That name cannot join this drop."),
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
    def __init__(self, engine: Engine | None = None, maker_name: str | None = None, togi_home: str | None = None):
        self.engine = engine or Engine()
        self.maker_name = maker_name
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

    def _public(self, d) -> dict:
        v = self.engine.view(d)
        out = {k: v.get(k) for k in ("drop", "title", "state", "currency", "price", "start_price", "buyers", "minimum",
                                      "minimum_met", "locked_step", "next_step", "next_message", "ends_at", "time_left",
                                      "time_left_seconds", "outcome")}
        out["maker_name"] = self.maker_name or (d.maker if d.maker and d.maker != "maker" else None)
        out["steps"] = [{"step": i, "from_buyers": s.min_buyers, "price": m(s.price)} for i, s in enumerate(d.ladder.steps)]
        out["joinable"] = v["state"] == "live"
        if d.settled:
            s = d.settled
            out["settled"] = {"outcome": s["outcome"], "final_price": s["final_price"],
                              "paid": (s.get("counts") or {}).get("captured", 0)}
        out["server_time"] = datetime.now(timezone.utc).isoformat()
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
        return {"buyer": b.id, "name": b.name, "status": b.status, "card": _card_label(b.card_index),
                "held": m(b.hold_amount) if b.status == "held" and b.hold_amount is not None else None,
                "charged": m(b.captured) if b.captured is not None else None,
                "start_price": m(d.ladder.start_price), "can_leave": live and b.status == "held",
                "leaving": b.id in d.leaving}

    def join(self, drop: str, name, card) -> dict:
        d = self._drop(drop)
        if not isinstance(card, int) or isinstance(card, bool) or card not in WEB_CARDS:
            raise ApiError(400, "bad_card", "Pick one of the test cards.")
        if not isinstance(name, str):
            raise ApiError(400, "bad_buyer", MESSAGES["bad_buyer"][1])
        try:
            out = self.engine.join(d.id, name, card)
        except (CrowdError, StoreError, HoldError) as e:
            raise from_engine(e) from None
        token = self.tokens.issue(d.id, out["buyer"])
        d = self.engine.get(d.id)
        return {"buyer": out["buyer"], "token": token, "me": self._mine(d, out["buyer"]), "drop": self._public(d)}

    def me(self, drop: str, buyer, token) -> dict:
        d = self._drop(drop)
        if not self.tokens.check(d.id, buyer, token):
            raise ApiError(403, "not_yours", "This browser does not hold that join.")
        return {"me": self._mine(d, buyer), "drop": self._public(d)}

    def leave(self, drop: str, buyer, token) -> dict:
        d = self._drop(drop)
        if not self.tokens.check(d.id, buyer, token):
            raise ApiError(403, "not_yours", "This browser does not hold that join.")
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
            args = rec.get("args") or {}
            if rec.get("bundle") != "crowd" or rec.get("command") != "drop settle" or args.get("drop") != drop:
                continue
            status = rec.get("status", "pending")
            try:
                if status not in ("denied", "consumed") and time.time() > _epoch(rec["expires"]):
                    status = "expired"
            except (KeyError, ValueError):
                pass
            out.append({"id": rec.get("id"), "status": status, "summary": rec.get("summary"), "expect": args.get("expect"),
                        "created": rec.get("created"), "expires": rec.get("expires")})
        return sorted(out, key=lambda a: a.get("created") or "", reverse=True)

    def maker(self, drop: str) -> dict:
        d = self._drop(drop)
        out = self._public(d)
        rows = self.engine.buyers(d.id)
        out["buyers_list"] = [{"name": r["name"], "status": r["status"], "held": r["held"], "charged": r["charged"],
                               "card": _card_label(r["card"])} for r in rows]
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
