"""The maker's activity feed: a drop's event log in plain words.

Built from the events as recorded, newest first. Runs of the same thing (thirty buyers joining in one batch, fifty-two
captures at settlement) become one line with a count. Bookkeeping events (a join or capture written down before PayPal is
asked) are left out; what they lead to is shown instead. Every amount is copied from an event; nothing is added up.
"""
from __future__ import annotations

from datetime import datetime

# event type -> the kind of line it makes; consecutive events of the same kind are grouped into one line
KINDS = {
    "drop_created": "created", "drop_published": "published", "hold_placed": "joined", "hold_failed": "declined",
    "step_locked": "step", "buyer_left": "left", "drop_closed": "closed", "settlement_started": "settling",
    "hold_captured": "captured", "capture_failed": "capture_failed", "hold_voided": "released", "void_failed": "void_failed",
    "buyer_excluded": "excluded", "settle_blocked": "blocked", "drop_settled": "settled",
}
GROUPED = {"joined", "declined", "left", "captured", "capture_failed", "released", "void_failed"}
GROUP_GAP_SECONDS = 60


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _names(items: list[dict]) -> str:
    names = [i["name"] for i in items]
    if len(names) <= 3:
        return ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1]
    return f"{names[0]}, {names[1]} and {len(names) - 2} others"


def _line(kind: str, items: list[dict], title: str) -> str:
    p = items[-1]["payload"]
    n = len(items)
    if kind == "created":
        return f"Drop created: {title}, minimum {p.get('minimum')}"
    if kind == "published":
        return "Published. Buyers can join"
    if kind == "joined":
        return f"{_names(items)} joined. ${p.get('amount')} held each" if n > 1 else f"{_names(items)} joined. ${p.get('amount')} held"
    if kind == "declined":
        return f"{_names(items)}: card declined, nothing held"
    if kind == "step":
        if p.get("step") == 0:
            return f"Minimum reached with {p.get('count')} buyers: everyone pays ${p.get('price')}"
        return f"{p.get('count')} buyers: everyone now pays ${p.get('price')}"
    if kind == "left":
        return f"{_names(items)} left. Hold released"
    if kind == "closed":
        how = "Time ran out" if p.get("reason") == "expired" else "Closed"
        met = "minimum met" if p.get("funded") else "minimum missed"
        return f"{how} with {p.get('count')} buyers, {met}"
    if kind == "settling":
        if p.get("outcome") == "funded":
            return f"Settling: {p.get('count')} holds, each charged ${p.get('final_price')}"
        return f"Settling: minimum missed, releasing {p.get('count')} holds"
    if kind == "captured":
        return f"{n} charged ${p.get('amount')}" if n > 1 else f"{_names(items)} charged ${p.get('amount')}"
    if kind == "capture_failed":
        return f"Charge failed for {_names(items)}"
    if kind == "released":
        return f"{n} holds released" if n > 1 else f"{_names(items)}'s hold released"
    if kind == "void_failed":
        return f"PayPal would not release the hold of {_names(items)}"
    if kind == "excluded":
        return f"{_names(items)} taken out of the drop"
    if kind == "blocked":
        return "Settlement stopped: it needs you"
    if kind == "settled":
        c = p.get("counts") or {}
        if p.get("outcome") == "funded":
            return f"Settled: {c.get('captured', 0)} paid ${p.get('final_price')}"
        return f"Settled: minimum missed, {c.get('voided', 0)} holds released, nobody paid"
    return kind


def feed(events: list[dict], buyers: dict, title: str, limit: int = 60) -> list[dict]:
    """[{at, kind, text, count}] newest first. `buyers` maps buyer id -> display name."""
    groups: list[dict] = []
    for e in events:
        kind = KINDS.get(e["type"])
        p = e["payload"]
        if kind is None or (e["type"] == "hold_voided" and p.get("reason") == "left"):   # a leave says so in its own line
            continue
        item = {"ts": e["ts"], "payload": p, "name": p.get("name") or buyers.get(p.get("buyer"), p.get("buyer") or "")}
        last = groups[-1] if groups else None
        if (last and kind in GROUPED and last["kind"] == kind
                and (_ts(e["ts"]) - _ts(last["items"][-1]["ts"])).total_seconds() <= GROUP_GAP_SECONDS):
            last["items"].append(item)
        else:
            groups.append({"kind": kind, "items": [item]})
    out = [{"at": g["items"][-1]["ts"], "kind": g["kind"], "text": _line(g["kind"], g["items"], title),
            "count": len(g["items"])} for g in groups]
    return list(reversed(out))[:limit]
