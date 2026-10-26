"""The certifier. Fixed code, no AI. Accepts an answer only if PayPal's balance change over the
window equals the program's parts exactly, with nothing left over, every part traced to
transaction ids. Otherwise it refuses and says why.

Two PayPal sources must agree:
  Balances API          opening balance at `start`, closing balance at `end`
  Transaction Search    every balance-affecting record in between, each with PayPal's own
                        running ending_balance
Never loosen a check to make a certificate pass. A refusal is a correct output.
"""
import hashlib, json
from datetime import datetime, timezone
from decimal import Decimal

from . import ledger, program as prog
from .paypal import SECOND, parse_time

VERSION = "certifier/1"
CENT = Decimal("0.01")
OPENING_SRC = "PayPal Balances API as_of start - 1s"
CLOSING_SRC = "PayPal Balances API as_of end - 1s"
CHECKS = ["program valid", "window fully indexed by PayPal", "record count equals PayPal's total_items",
          "no record fetched twice", "every record inside the window", "every record carries ending_balance",
          "running balance follows record by record", "records explain opening -> closing exactly",
          "every component in exactly one part", "parts sum to the balance change, 0.00 left over"]
CERT_KEYS = {"kind", "certifier", "question", "program", "program_hash", "currency", "start", "end", "answer",
             "opening", "closing", "record_count", "parts", "left_over", "transactions", "data_hash",
             "indexed_through", "issued_at", "checks", "signature"}
TX_KEYS = {"id", "at", "code", "status", "currency", "gross", "fee", "ending_balance", "note", "family"}


def _d(v):
    return Decimal(str(v)).quantize(CENT)


def _program_hash(p):
    return "sha256:" + hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _order_group(group, running):
    """Order one second's records so each ending_balance follows. Depth-first, so a valid order is
    found whenever one exists (groups are small: one second of one account)."""
    if not group:
        return []
    for k, e in enumerate(group):
        if e.ending_balance is None or running + e.net == e.ending_balance:
            rest = _order_group(group[:k] + group[k + 1:], running + e.net)
            if rest is not None:
                return [e] + rest
    return None


def chain(events, opening):
    """Replay PayPal's running balance from `opening`. Same-second records may arrive in any order,
    so within one second search for the order whose ending_balances follow. Returns (ordered, break)
    where break is None or a dict naming the first record that does not follow."""
    running, ordered, i = opening, [], 0
    while i < len(events):
        j = i
        while j < len(events) and events[j].at == events[i].at:
            j += 1
        group = list(events[i:j])
        found = _order_group(group, running) if len(group) <= 8 else None
        if found is None:  # report the first record, in PayPal's order, that cannot follow
            for e in group:
                if e.ending_balance is not None and running + e.net != e.ending_balance:
                    return ordered, {"id": e.id, "at": e.at, "code": e.code, "before": str(running),
                                     "change": str(e.net), "expected": str(running + e.net),
                                     "paypal_says": str(e.ending_balance),
                                     "gap": str(e.ending_balance - (running + e.net))}
                running += e.net
                ordered.append(e)
        else:
            for e in found:
                running += e.net
                ordered.append(e)
        i = j
    return ordered, None


def refuse(p, reason, detail=None, **extra):
    out = {"kind": "refusal", "certifier": VERSION, "question": (p or {}).get("question"),
           "reason": reason, "detail": detail or {}}
    out.update(extra)
    return out


def certify(p, source, now=None):
    """`source` gives transactions(start, end) -> (records, last_refreshed, paypal_count) and
    balance(at, currency)."""
    try:
        prog.validate(p)
        start, end = parse_time(p["start"]), parse_time(p["end"])
    except (prog.ProgramError, ValueError, TypeError) as e:
        return refuse(p if isinstance(p, dict) else None, "invalid_program", {"error": str(e)})
    if end <= start:
        return refuse(p, "invalid_program", {"error": "end must be after start"})
    now = now or datetime.now(timezone.utc)
    if end > now:
        return refuse(p, "window_in_future", {"end": p["end"], "now": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    cur = p["currency"].upper()

    # Window is [start, end) in whole seconds. Balances as_of T includes records stamped T, so the
    # opening is read one second before start and the closing one second before end.
    last_second = end - SECOND
    records, last_refreshed, expected = source.transactions(start, end)
    if last_refreshed is None or last_refreshed < last_second:
        return refuse(p, "not_yet_indexed", {
            "explanation": "PayPal has not finished indexing this window; an answer now could miss transactions.",
            "indexed_through": last_refreshed and last_refreshed.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": p["end"]})
    if len(records) != expected:
        return refuse(p, "incomplete_fetch", {
            "explanation": "PayPal reports a different number of records than were fetched.",
            "paypal_count": expected, "fetched": len(records)})
    everything = ledger.from_paypal(records)
    seen, dups = set(), []
    for e in everything:
        key = (e.id, e.currency)  # a conversion shares no id across its two sides, but be exact
        if key in seen:
            dups.append(e.id)
        seen.add(key)
    if dups:
        return refuse(p, "duplicate_record", {"explanation": "The same record was fetched twice.", "ids": dups[:10]})
    outside = [e.id for e in everything if not start <= parse_time(e.at) < end]
    if outside:
        return refuse(p, "record_outside_window", {"explanation": "PayPal returned records outside the window.",
                                                   "ids": outside[:10]})
    events = [e for e in everything if e.currency == cur]
    missing_eb = [e.id for e in events if e.ending_balance is None]
    if missing_eb:
        return refuse(p, "no_running_balance", {
            "explanation": "PayPal omitted ending_balance (it does when a search is filtered); cannot check the chain.",
            "ids": missing_eb[:10]})

    opening, closing = _d(source.balance(start - SECOND, cur)), _d(source.balance(last_second, cur))
    ordered, brk = chain(events, opening)
    if brk:
        return refuse(p, "chain_break", dict(brk, explanation=(
            "PayPal's running balance does not follow from the records before it: a record is missing, "
            "duplicated or misread.")), opening=str(opening))
    replayed = opening + sum((e.net for e in ordered), Decimal("0"))
    if replayed != closing:
        return refuse(p, "balance_mismatch", {
            "explanation": "The records do not explain the whole balance change.",
            "opening": str(opening), "closing": str(closing), "records_explain": str(replayed - opening),
            "balance_moved": str(closing - opening), "unexplained": str(closing - replayed)})

    parts, conflicts, unclaimed = prog.evaluate(p, ordered)
    if conflicts:
        return refuse(p, "counted_twice", {"explanation": "A transaction is claimed by two parts.",
                                           "examples": [dict(zip(("id", "component", "first", "second"), c)) for c in conflicts[:10]]})
    if unclaimed:
        return refuse(p, "left_over", {"explanation": "Some money is in no part; add a part or a rest part.",
                                       "left_over": str(sum((a for _, _, a in unclaimed), Decimal("0"))),
                                       "examples": [{"id": i, "component": c, "amount": str(a)} for i, c, a in unclaimed[:10]]})
    total = sum((x["value"] for x in parts), Decimal("0"))
    left = (closing - opening) - total
    if left != 0:  # unreachable if the checks above hold; kept as the final law
        return refuse(p, "left_over", {"left_over": str(left)})

    names = [p["answer"]] if isinstance(p["answer"], str) else p["answer"]
    answer = sum((x["value"] for x in parts if x["name"] in names), Decimal("0.00"))
    return {
        "kind": "certificate", "certifier": VERSION,
        "question": p.get("question"), "program": p, "program_hash": _program_hash(p),
        "currency": cur, "start": p["start"], "end": p["end"],
        "answer": {"parts": names, "value": str(answer)},
        "opening": {"value": str(opening), "source": OPENING_SRC},
        "closing": {"value": str(closing), "source": CLOSING_SRC},
        "record_count": {"fetched": len(records), "paypal_says": expected},
        "parts": [{"name": x["name"], "take": x["take"], "value": str(x["value"]),
                   "lines": [{"id": l["id"], "component": l["component"], "amount": str(l["amount"])} for l in x["lines"]]}
                  for x in parts],
        "left_over": "0.00",
        "transactions": [e.public() for e in ordered],
        "data_hash": ledger.digest(ordered),
        "indexed_through": last_refreshed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": CHECKS,
    }


def check_offline(cert, trusted_key=None):
    """Recheck a certificate with nothing but the certificate: the program re-run over the listed
    transactions must give exactly the listed parts and answer, and the arithmetic must close.
    Proves the certificate is consistent, not where it came from: replay() against PayPal does that.
    With trusted_key (the issuer's public key, hex), the Ed25519 signature must also verify, which
    catches edits that keep the arithmetic consistent (question, window, labels).
    Returns a list of problems (empty = consistent)."""
    try:
        problems = _check_offline(cert)
    except Exception as e:  # malformed certificate
        return [f"malformed certificate: {type(e).__name__}: {e}"]
    if trusted_key is not None:
        from . import sign
        if not sign.verify(cert, trusted_key):
            problems.append("signature does not verify against the trusted key")
    return problems


def _check_offline(cert):
    if not isinstance(cert, dict) or cert.get("kind") != "certificate":
        return ["not a certificate"]
    problems = []
    p = cert["program"]
    try:
        prog.validate(p)
    except prog.ProgramError as e:
        return [f"program invalid: {e}"]
    if _program_hash(p) != cert["program_hash"]:
        problems.append("program_hash does not match the program")
    for k in ("question", "start", "end"):
        if cert.get(k) != p.get(k):
            problems.append(f"{k} differs from the program's")
    if cert["currency"] != p["currency"].upper():
        problems.append("currency differs from the program's")
    names = [p["answer"]] if isinstance(p["answer"], str) else p["answer"]
    if cert["answer"]["parts"] != names:
        problems.append("answer names different parts than the program")
    if cert.get("certifier") != VERSION:
        problems.append(f"certifier is not {VERSION}")
    if cert.get("left_over") != "0.00":
        problems.append("left_over is not 0.00")
    if cert.get("checks") != CHECKS:
        problems.append("checks list is not the certifier's")
    rc = cert.get("record_count") or {}
    if rc.get("fetched") != rc.get("paypal_says") or not isinstance(rc.get("fetched"), int) \
            or rc["fetched"] < len(cert["transactions"]):
        problems.append("record_count is inconsistent")
    if cert["opening"].get("source") != OPENING_SRC or cert["closing"].get("source") != CLOSING_SRC:
        problems.append("balance sources are not the certifier's")
    # Nothing but what the certifier writes: an extra key could carry text a reader takes as certified.
    if set(cert) - CERT_KEYS:
        problems.append(f"unexpected keys: {sorted(set(cert) - CERT_KEYS)}")
    if set(cert["answer"]) != {"parts", "value"} or set(cert["opening"]) != {"value", "source"} \
            or set(cert["closing"]) != {"value", "source"} or set(rc) != {"fetched", "paypal_says"}:
        problems.append("unexpected keys inside answer / opening / closing / record_count")
    if any(set(t) != TX_KEYS or t["family"] != ledger.FAMILIES.get(t["code"][:3], "unknown") for t in cert["transactions"]):
        problems.append("a transaction has unexpected keys or a family that does not match its code")
    end_last = parse_time(p["end"]) - SECOND
    if parse_time(cert["indexed_through"]) < end_last:
        problems.append("indexed_through is before the window's end")
    if parse_time(cert["issued_at"]) < parse_time(p["end"]):
        problems.append("issued_at is before the window's end")

    events = [ledger.Event(id=t["id"], seq=n, at=t["at"], code=t["code"], status=t["status"],
                           currency=t["currency"], gross=Decimal(t["gross"]), fee=Decimal(t["fee"]),
                           ending_balance=Decimal(t["ending_balance"]), note=t.get("note", ""))
              for n, t in enumerate(cert["transactions"])]
    if ledger.digest(events) != cert["data_hash"]:
        problems.append("data_hash does not match the transactions")
    if any(e.currency != cert["currency"] for e in events):
        problems.append("a transaction is in another currency")
    ids = [e.id for e in events]
    if len(set(ids)) != len(ids):
        problems.append("a transaction is listed twice")
    start, end = parse_time(p["start"]), parse_time(p["end"])
    if any(not start <= parse_time(e.at) < end for e in events):
        problems.append("a transaction is outside the window")

    parts, conflicts, unclaimed = prog.evaluate(p, events)
    if conflicts or unclaimed:
        problems.append("the program does not split these transactions exactly once")
    want = [{"name": x["name"], "take": x["take"], "value": str(x["value"]),
             "lines": [{"id": l["id"], "component": l["component"], "amount": str(l["amount"])} for l in x["lines"]]}
            for x in parts]
    if want != cert["parts"]:
        problems.append("parts are not what the program gives on these transactions")
    answer = sum((x["value"] for x in parts if x["name"] in names), Decimal("0.00"))
    if str(answer) != cert["answer"]["value"]:
        problems.append("answer is not the sum of its named parts")

    opening, closing = Decimal(cert["opening"]["value"]), Decimal(cert["closing"]["value"])
    running = opening
    for e in events:
        running += e.net
        if e.ending_balance != running:
            problems.append(f"running balance breaks at {e.id}")
            break
    if opening + sum((x["value"] for x in parts), Decimal("0")) != closing:
        problems.append("opening + parts != closing")
    return problems


def replay(cert, source, now=None):
    """Re-run a certificate's program against PayPal now. Same records and same answer -> holds."""
    again = certify(cert["program"], source, now=now)
    if again["kind"] != "certificate":
        return {"holds": False, "why": again["reason"], "detail": again["detail"]}
    same = again["data_hash"] == cert["data_hash"] and again["answer"]["value"] == cert["answer"]["value"]
    return {"holds": same, "answer": again["answer"]["value"],
            "why": "same records, same answer" if same else "PayPal's records changed since issue"}
