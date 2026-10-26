"""What an agent can call. Shared by the MCP server and the CLI.

The agent sees which kinds of activity exist (codes, counts, no amounts) so it can write a
program; money only ever comes back inside a certificate the certifier issued.
"""
import json, os, re
from collections import Counter

from . import certify as cz, ledger, sign
from .paypal import ROOT, parse_time

STORE = os.path.join(ROOT, "data", "certificates")

HINTS = {
    "invalid_program": "Fix the program shape and try again.",
    "window_in_future": "Choose an end time in the past.",
    "not_yet_indexed": "PayPal indexes activity with a delay (up to ~3 hours). Ask again later or end the window earlier.",
    "no_running_balance": "Cannot check this data; report that you cannot answer.",
    "incomplete_fetch": "Fewer or more records arrived than PayPal reports. Do not answer; try again.",
    "record_outside_window": "PayPal returned records outside the window. Do not answer.",
    "duplicate_record": "A record arrived twice. Do not answer; try again.",
    "chain_break": "PayPal's records disagree with its own running balance. Do not answer; report the record named.",
    "balance_mismatch": "The records do not explain the balance change. Do not answer.",
    "counted_twice": "Two parts claim the same money. Make the parts' filters disjoint.",
    "left_over": "Some money is in no part. Add a part for it, or end with a rest part.",
}


def describe(source, currency, start, end):
    records, last_refreshed, _ = source.transactions(parse_time(start), parse_time(end))
    events = [e for e in ledger.from_paypal(records) if e.currency == currency.upper()]
    kinds = Counter((e.code, e.family, e.status, "in" if e.gross > 0 else "out" if e.gross < 0 else "zero") for e in events)
    return {"currency": currency.upper(), "start": start, "end": end, "records": len(events),
            "indexed_through": last_refreshed and last_refreshed.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kinds": [{"code": c, "family": f, "status": s, "sign": g, "count": n}
                      for (c, f, s, g), n in sorted(kinds.items())],
            "note": "Amounts are not shown. Write a program; money comes back only in a certificate."}


def save(cert):
    os.makedirs(STORE, exist_ok=True)
    cid = cert["data_hash"].split(":")[1][:12] + "-" + cert["program_hash"].split(":")[1][:8]
    with open(os.path.join(STORE, cid + ".json"), "w") as f:
        json.dump(cert, f, indent=1)
    return cid


def load(cid):
    if not re.fullmatch(r"[0-9a-f]{12}-[0-9a-f]{8}", cid or ""):
        raise ValueError("bad certificate id")
    with open(os.path.join(STORE, cid + ".json")) as f:
        return json.load(f)


# What an agent may see of a refusal: reasons, ids and counts. Never an amount: a left-over or a
# chain gap is itself an uncertified answer an agent could aim for. Full detail goes to the log.
AGENT_SAFE = {"explanation", "error", "ids", "id", "at", "code", "component", "first", "second",
              "paypal_count", "fetched", "indexed_through", "end", "now"}
REFUSALS = os.path.join(ROOT, "data", "refusals.jsonl")


def _safe(v):
    if isinstance(v, dict):
        return {k: _safe(x) for k, x in v.items() if k in AGENT_SAFE or k == "examples"}
    if isinstance(v, list):
        return [_safe(x) for x in v]
    return v


def for_agent(refusal):
    return {"kind": "refusal", "reason": refusal["reason"], "question": refusal.get("question"),
            "detail": _safe(refusal.get("detail") or {}),
            "what_to_do": HINTS.get(refusal["reason"], "Do not answer.")}


def _log_refusal(out):
    os.makedirs(os.path.dirname(REFUSALS), exist_ok=True)
    with open(REFUSALS, "a") as f:
        f.write(json.dumps(out) + "\n")


def ask(source, program):
    out = cz.certify(program, source)
    if out["kind"] == "refusal":
        _log_refusal(out)
        return for_agent(out)
    out = sign.sign(out)
    cid = save(out)
    return {"kind": "certificate", "certificate_id": cid, "question": out["question"],
            "answer": out["answer"], "currency": out["currency"], "start": out["start"], "end": out["end"],
            "opening": out["opening"]["value"], "closing": out["closing"]["value"], "left_over": out["left_over"],
            "parts": [{"name": p["name"], "value": p["value"], "transactions": sorted({l["id"] for l in p["lines"]})}
                      for p in out["parts"]],
            "say": f"Quote answer.value exactly ({out['answer']['value']} {out['currency']}) with certificate {cid}. "
                   "Do not round, restate, or add numbers of your own."}


def verify(source, cid):
    """Holds or not, and why. Never a new figure: a changed answer is uncertified until asked again."""
    cert = load(cid)
    problems = cz.check_offline(cert, trusted_key=sign.public_hex(sign.private_key()))
    if problems:
        return {"certificate_id": cid, "holds": False, "why": "certificate fails its own checks", "problems": problems[:5]}
    r = cz.replay(cert, source)
    if not r["holds"] and r.get("detail"):
        _log_refusal({"kind": "replay", "certificate_id": cid, **r})
    return {"certificate_id": cid, "holds": r["holds"],
            "why": r["why"] if r["holds"] or "detail" not in r else f"PayPal's records no longer certify: {r['why']}"}
