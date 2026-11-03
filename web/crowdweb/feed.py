"""The maker's activity feed: a drop's event log in plain words.

Built from the events as recorded, newest first. Runs of the same thing (thirty buyers joining in one batch, fifty-two
captures at settlement) become one line with a count; a batch of the assistant's test buyers is one line of its own ("30 test
buyers joined"), and a real buyer who joins in the middle of one gets a line of their own between its parts. Bookkeeping events (a join or capture written down before PayPal is
asked) are left out; what they lead to is shown instead. Every amount is copied from an event; nothing is added up.
"""
from __future__ import annotations

import re
from datetime import datetime

# event type -> the kind of line it makes; consecutive events of the same kind are grouped into one line
KINDS = {
    "drop_created": "created", "drop_published": "published", "hold_placed": "joined", "hold_failed": "declined",
    "step_locked": "step", "buyer_left": "left", "drop_closed": "closed", "settlement_started": "settling",
    "hold_captured": "captured", "capture_failed": "capture_failed", "hold_voided": "released", "void_failed": "void_failed",
    "buyer_excluded": "excluded", "settle_blocked": "blocked", "drop_settled": "settled",
}
GROUPED = {"joined", "declined", "left", "left_held", "captured", "capture_failed", "released", "void_failed", "unconfirmed",
           "lapsed", "gone", "taken_outside"}
LEFT_HELD = "left_held"      # a buyer left and PayPal would not release the hold: one line, not "released" and then "would not release"
# What the engine writes as the code of a `hold_failed` that is not a decline: a join nobody confirmed that was then looked into.
# `join_unconfirmed`: PayPal holds nothing for it (or would not say). The two below: a hold exists (or money moved) and PayPal would
# not release it: the same line as any hold PayPal would not release. Anything else is PayPal (or the card) refusing the join.
UNCONFIRMED = "join_unconfirmed"
STUCK_RELEASE = {"orphan_void_failed", "orphan_unexpected_status"}
UNREADABLE = "PayPal would not say"          # how the engine's message starts when PayPal would not say whether a join left a hold


def how_released(p: dict, event: str = "hold_voided") -> str:
    """What a `hold_voided` (or `failed_hold_released`) event says became of the hold: `released` (a leave, a removal, a failed
    capture's hold voided), `missed_minimum`, `lapsed` (ran out at PayPal), `gone` (PayPal no longer shows it), or `taken_outside`
    (money was taken from it outside the engine: it is out of the crowd, and it was NOT released)."""
    reason = str(p.get("reason") or "")
    if p.get("taken_outside_engine") or reason == "taken_outside_engine":
        return "taken_outside"
    if reason in ("left", "excluded") or p.get("released_by_void") or (event == "failed_hold_released" and reason == "voided"):
        return "released"           # (`released_by_void`: PayPal showed a status nobody has a rule for, and the engine voided it)
    if reason == "not_funded":
        return "missed_minimum"
    if p.get("expired") or "expired" in reason:
        return "lapsed"
    return "released" if event == "failed_hold_released" else "gone"


def may_be_open(p: dict) -> bool:
    """Does this `hold_failed` leave a hold that may still be open at PayPal: one PayPal would not release, or one it would not describe?
    (A decline, and a join PayPal holds nothing for, leave none.)"""
    return p.get("code") in STUCK_RELEASE or (p.get("code") == UNCONFIRMED and str(p.get("message") or "").startswith(UNREADABLE))
GROUP_GAP_SECONDS = 60


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def display_name(name: str) -> str:
    """How a buyer is shown. The test buyers the assistant adds are named `Sim 001`; say what they are."""
    m = re.fullmatch(r"Sim (\d+)", name or "")
    return f"Simulated buyer {m.group(1)}" if m else name


def _names(items: list[dict]) -> str:
    names = [display_name(i["name"]) for i in items]
    if len(names) <= 3:
        return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]
    return f"{names[0]}, {names[1]} and {len(names) - 2} others"


def _line(kind: str, items: list[dict], title: str) -> tuple[str | None, str]:
    """(who, what): the buyer names on their own, and the words. The page shows the names in their own element, so a
    name can never read as part of a sentence about money."""
    p = items[-1]["payload"]
    n = len(items)
    who = _names(items)
    if kind == "created":
        return None, f"Drop created: {title}, minimum {p.get('minimum')}"
    if kind == "published":
        return None, "Published. Buyers can join"
    if kind == "joined" and items[0].get("sim"):        # a batch of the assistant's test buyers: said as one, never with a real name in it
        return None, (f"{n} test buyers joined · ${p.get('amount')} held each" if n > 1 else f"1 test buyer joined · ${p.get('amount')} held")
    if kind == "joined":
        return who, (f"joined. ${p.get('amount')} held each" if n > 1 else f"joined. ${p.get('amount')} held")
    if kind == "declined":
        return who, "card declined, nothing held"
    if kind == "unconfirmed":
        return who, "join never confirmed, nothing held"
    if kind == "step":
        if p.get("step") == 0:
            return None, f"Minimum reached with {p.get('count')} buyers: everyone pays ${p.get('price')}"
        return None, f"{p.get('count')} buyers: everyone now pays ${p.get('price')}"
    if kind == "left":
        return who, "left. Hold released"
    if kind == LEFT_HELD:
        return who, "left. PayPal would not release the hold"
    if kind == "closed":
        how = "Time ran out" if p.get("reason") == "expired" else "Closed"
        met = "minimum met" if p.get("funded") else "minimum missed"
        return None, f"{how} with {p.get('count')} buyers, {met}"
    if kind == "settling":
        if p.get("outcome") == "funded":
            return None, f"Settling: {p.get('count')} holds, each charged ${p.get('final_price')}"
        return None, f"Settling: minimum missed, releasing {p.get('count')} holds"
    if kind == "captured":
        return (None, f"{n} charged ${p.get('amount')}") if n > 1 else (who, f"charged ${p.get('amount')}")
    if kind == "capture_failed":
        return who, "charge failed"
    if kind == "released":
        return (None, f"{n} holds released") if n > 1 else (who, "hold released")
    if kind == "void_failed":
        return who, ("PayPal would not say whether a hold exists" if p.get("code") == UNCONFIRMED else "PayPal would not release the hold")
    if kind == "excluded":
        return who, "taken out of the drop"
    if kind == "lapsed":
        return who, "hold ran out at PayPal, so out of the drop"
    if kind == "gone":
        return who, "hold no longer at PayPal, so out of the drop"
    if kind == "taken_outside":
        return who, "hold was charged outside the drop, so out of the drop. It was not released: refund it at PayPal"
    if kind == "blocked":
        return None, "Settlement stopped: it needs you"
    if kind == "settled":
        c = p.get("counts") or {}
        if p.get("outcome") == "funded":
            return None, f"Settled: {c.get('captured', 0)} paid ${p.get('final_price')}"
        return None, f"Settled: minimum missed, {c.get('voided', 0)} holds released, nobody paid"
    return None, kind


def _left_held(events: list[dict]) -> set:
    """The positions (in `events`) of a leave PayPal would not release: a `void_failed{reason: left}` and the `buyer_left` before it."""
    out: set = set()
    last_left: dict = {}
    for i, e in enumerate(events):
        p = e["payload"]
        if e["type"] == "buyer_left":
            last_left[p.get("buyer")] = i
        elif e["type"] == "void_failed" and p.get("reason") == "left" and not p.get("orphan") and p.get("buyer") in last_left:
            out.update({i, last_left[p["buyer"]]})
    return out


def feed(events: list[dict], buyers: dict, title: str, limit: int = 60) -> list[dict]:
    """[{at, kind, who, text, count}] newest first; `who` is the buyer names or None. `buyers` maps id -> name."""
    groups: list[dict] = []
    held_after_leaving = _left_held(events)
    for at, e in enumerate(events):
        kind = KINDS.get(e["type"])
        p = e["payload"]
        if e["type"] == "hold_failed":              # not every failed hold is a decline: say what the engine recorded
            kind = "void_failed" if may_be_open(p) else ("unconfirmed" if p.get("code") == UNCONFIRMED else "declined")
        if at in held_after_leaving:           # the leave stands, the hold was not released: said once, in the leave's line
            kind = LEFT_HELD if e["type"] == "buyer_left" else None
        if e["type"] == "hold_voided":          # a hold that ran out, or was charged outside the drop, is not "released"
            kind = {"lapsed": "lapsed", "gone": "gone", "taken_outside": "taken_outside"}.get(how_released(p), "released")
        if kind is None or (e["type"] == "hold_voided" and p.get("reason") == "left"):   # a leave says so in its own line
            continue
        item = {"ts": e["ts"], "payload": p, "name": p.get("name") or buyers.get(p.get("buyer"), p.get("buyer") or "")}
        item["sim"] = display_name(item["name"]) != item["name"]
        last = groups[-1] if groups else None
        if (last and kind in GROUPED and last["kind"] == kind
                and (kind != "joined" or last["items"][-1]["sim"] == item["sim"])      # test buyers and real ones never share a line
                and (_ts(e["ts"]) - _ts(last["items"][-1]["ts"])).total_seconds() <= GROUP_GAP_SECONDS):
            last["items"].append(item)
        else:
            groups.append({"kind": kind, "items": [item]})
    out = []
    for g in groups:
        who, what = _line(g["kind"], g["items"], title)
        out.append({"at": g["items"][-1]["ts"], "kind": g["kind"], "who": who, "text": what, "count": len(g["items"])})
    return list(reversed(out))[:limit]
