"""The settlement check. No AI; it reads PayPal and compares with what the engine says it did.

`verify(drop)` is the immediate proof from the Payments API: every hold is read back and must be in the
state the engine recorded, with the right amounts, and holds = captured + voided + failed.

`tieout(drop)` is the balance proof, once PayPal has indexed the window: it builds a Closure program whose
parts are this drop's capture ids (gross and fee) plus a rest part, and hands it to the certifier
(closure.certify). The certifier's answer is returned as it is: a certificate, or its refusal (for
example not_yet_indexed). Nothing here loosens the certifier.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from . import ladder as lad
from .engine import CrowdError, Engine, default_engine, m
from .paypal_holds import HoldError, mode_of
from .store import parse_iso

TIEOUT_MARGIN = timedelta(seconds=30)


CAPTURED = ("CAPTURED", "PARTIALLY_CAPTURED")
GONE = ("VOIDED", "EXPIRED")          # PayPal statuses in which a hold no longer holds money


def _scan(events: list[dict]) -> dict:
    """What the event log says about every hold, order and join (from events, not the projection)."""
    holds, by_auth = [], {}
    known: set = set()          # authorisations the log accounts for: placed, or voided as an orphan
    orders: dict = {}           # every PayPal order the log knows of -> who it was for
    for e in events:
        t, p = e["type"], e["payload"]
        if p.get("order_id"):
            orders.setdefault(p["order_id"], p.get("buyer"))
        if t == "hold_placed":
            h = {"buyer": p["buyer"], "auth_id": p["auth_id"], "amount": lad.D(p["amount"]), "end": None,
                 "capture_id": None, "engine_amount": None, "engine_fee": None, "mode": p.get("paypal_mode"),
                 "expires": p.get("expires"), "failed": None, "void_failed": None, "released": False,
                 "released_reason": None, "voided_expired": False}
            holds.append(h)
            by_auth[p["auth_id"]] = h
            known.add(p["auth_id"])
        elif t == "orphan_hold_released" and p.get("auth_id"):
            known.add(p["auth_id"])
        elif t in ("hold_captured", "hold_voided", "capture_failed") and p.get("auth_id") in by_auth:
            h = by_auth[p["auth_id"]]
            h["end"] = {"hold_captured": "captured", "hold_voided": "voided", "capture_failed": "failed"}[t]
            if t == "hold_captured":
                h["capture_id"], h["engine_amount"], h["engine_fee"] = p["capture_id"], lad.D(p["amount"]), lad.D(p["fee"])
            elif t == "capture_failed":
                h["failed"] = p
            else:
                h["voided_expired"] = bool(p.get("expired"))
        elif t == "failed_hold_released" and p.get("auth_id") in by_auth:
            by_auth[p["auth_id"]]["released"] = True
            by_auth[p["auth_id"]]["released_reason"] = p.get("reason")
        elif t == "void_failed" and not p.get("orphan") and p.get("auth_id") in by_auth:
            h = by_auth[p["auth_id"]]
            h["void_failed"] = p
            if h["end"] is None:
                h["end"] = "void_failed"
    return {"holds": holds, "known": known, "orders": orders}


def _holds(events: list[dict]) -> list[dict]:
    """Every hold ever placed in a drop with the way it ended, from events (not the projection)."""
    return _scan(events)["holds"]


def _evidence_of(p: dict) -> bool:
    """Does a failed capture carry something PayPal itself said (an error name, a debug id, a status), or, for a hold the
    engine let lapse by the clock, PayPal's expiry time? Without it the failure is just the engine's say-so."""
    return bool(p.get("issue") or p.get("debug_id") or p.get("paypal_status") or
                (p.get("code") == "hold_expired" and p.get("expires")))


def verify(drop: str, engine: Engine | None = None, verbose: bool = True) -> dict:
    eng = engine or default_engine()
    d = eng.get(drop)
    if d.state != "settled":
        raise CrowdError("drop_not_settled", drop=d.id, state=eng.display_state(d))
    events = eng.store.events(d.id)
    scan = _scan(events)
    holds = scan["holds"]
    final = lad.D(d.settled["final_price"])
    outcome = d.settled["outcome"]
    mode_here = mode_of(eng.holds)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    rows = []
    gross = fees = net = lad.ZERO
    unreadable = 0
    amount_bad = 0
    for h in holds:
        row = {"buyer": h["buyer"], "auth_id": h["auth_id"], "engine": h["end"], "paypal": None, "ok": True, "problem": None}
        rows.append(row)

        def bad(msg: str, row=row) -> None:
            if row["ok"]:      # the first problem is the one shown; later ones would only repeat it
                row["ok"], row["problem"] = False, msg

        if h["end"] is None:
            bad("hold never settled: the engine has no capture, void or failure for it")
            continue
        try:
            auth = eng.holds.get(h["auth_id"])
            row["paypal"] = auth["status"]
            status = auth["status"]
            held = lad.D(auth["amount"]) if auth.get("amount") is not None else None
            # PayPal's own figure for the hold must be the one the engine recorded (after a capture PayPal may show the
            # amount it took instead of the amount it held; anything else is a mismatch)
            allowed = {h["amount"]} | ({h["engine_amount"]} if h["end"] == "captured" else set())
            if held is None or held not in allowed or (auth.get("currency") or d.currency) != d.currency:
                amount_bad += 1
                bad(f"PayPal shows this hold as {auth.get('amount')} {auth.get('currency')} but the engine recorded "
                    f"{m(h['amount'])} {d.currency}")
            if h["end"] == "captured":
                cap = eng.holds.get_capture(h["capture_id"])
                row["capture_id"] = h["capture_id"]
                amount, cfee = lad.D(cap["amount"]), lad.D(cap["fee"] or "0")
                if status != "CAPTURED":
                    bad(f"engine says captured but PayPal says the hold is {status}")
                elif cap["status"] not in ("COMPLETED", "PENDING"):
                    bad(f"PayPal says the capture is {cap['status']}")
                elif amount != final:
                    bad(f"captured {m(amount)} but the final price is {m(final)}")
                elif amount > h["amount"]:
                    bad(f"captured {m(amount)} which is more than the {m(h['amount'])} hold")
                elif cap.get("final") is not True:
                    bad("the capture is not marked final")
                elif amount != h["engine_amount"] or cfee != h["engine_fee"]:
                    bad("PayPal's amount or fee differs from what the engine recorded")
                else:
                    gross, fees, net = gross + amount, fees + cfee, net + lad.D(cap["net"] or (amount - cfee))
                    row["amount"], row["fee"] = m(amount), m(cfee)
            elif h["end"] == "voided":
                if not (status == "VOIDED" or (h["voided_expired"] and status in GONE)):
                    bad(f"engine says voided but PayPal says the hold is {status}")
            elif h["end"] == "void_failed":      # a missed minimum, and PayPal would not release this hold
                if status in CAPTURED:
                    bad(f"the release was refused and PayPal now says the hold is {status}: money was taken from a drop "
                        f"that missed its minimum")
                elif status not in GONE:
                    bad(_unreleased(h, status))
            else:  # failed capture: nothing may have been taken, and the hold must have been released
                fp = h["failed"] or {}
                if not _evidence_of(fp):
                    bad("the capture is recorded as failed with no PayPal evidence (no error name, debug id or status "
                        "from PayPal): it cannot be told apart from a guess")
                elif status in CAPTURED:
                    bad(f"engine says the capture failed but PayPal says the hold is {status}")
                elif fp.get("code") == "hold_expired" and status not in GONE:
                    bad(f"engine says the hold expired but PayPal says it is still {status}: it could have been captured")
                elif h["void_failed"] and status not in GONE:
                    bad(_unreleased(h, status))
                elif h["released"] and h["released_reason"] == "expired" and status not in GONE:
                    bad(f"engine says the failed hold lapsed but PayPal says it is {status}")
                elif h["released"] and h["released_reason"] != "expired" and status != "VOIDED":
                    bad(f"engine says the failed hold was released but PayPal says it is {status}")
                elif not h["released"] and not h["void_failed"] and status not in GONE:
                    bad("the capture failed and the hold is still open on the buyer's card")
        except HoldError as e:
            unreadable += 1
            bad(f"could not read this hold from PayPal: {e.code}")

    captured = [h for h in holds if h["end"] == "captured"]
    voided = [h for h in holds if h["end"] == "voided"]
    failed = [h for h in holds if h["end"] == "failed"]
    void_failed = [h for h in holds if h["end"] == "void_failed"]
    open_ = [h for h in holds if h["end"] is None]
    counts = d.settled["counts"]

    check("settled", True, f"outcome {outcome}")
    check("holds accounted for", not open_ and len(holds) == len(captured) + len(voided) + len(failed) + len(void_failed),
          f"{len(holds)} holds = {len(captured)} captured + {len(voided)} voided + {len(failed)} failed"
          + (f" + {len(void_failed)} that could not be released" if void_failed else ""))
    check("engine counts match its events",
          (counts["holds"], counts["captured"], counts["voided"], counts["failed"], counts.get("void_failed", 0)) ==
          (len(holds), len(captured), len(voided), len(failed), len(void_failed)), f"settled record says {counts}")
    # a join PayPal may have acted on that the log never closed: a hold the engine does not know about
    check("no join left unconfirmed", not d.unresolved,
          "every join was confirmed or released" if not d.unresolved else
          f"{len(d.unresolved)} join(s) started but never confirmed (" + ", ".join(
              sorted(str(j.get('buyer')) for j in d.unresolved.values())) + "): a PayPal hold may be open that the "
          "engine does not know about. Next: run `drop close` or `drop settle` again to reconcile, then verify")
    strays = _strays(eng, scan)
    check("every PayPal authorisation on the drop's orders is in the log", not strays,
          f"{len(scan['orders'])} order(s) read back from PayPal, none holds anything unaccounted for" if not strays
          else "; ".join(strays))
    check("every hold's amount matches PayPal's", amount_bad == 0,
          "each hold read back from PayPal at the amount the engine recorded" if not amount_bad
          else f"{amount_bad} hold(s) differ from what the engine recorded")
    modes = {h["mode"] for h in holds if h["mode"]} | {p.get("paypal_mode") for e in events if e["type"] in (
        "capture_failed", "void_failed", "hold_captured") for p in [e["payload"]] if p.get("paypal_mode")}
    other = modes - {mode_here, "unknown"}      # 'unknown' = a wrapper that would not say: nothing to compare
    check("recorded against the PayPal being checked", not other,
          f"events were recorded against {', '.join(sorted(modes)) or 'an unrecorded PayPal'}; checking against {mode_here}")
    check("every hold is in the state the engine says", all(r["ok"] for r in rows),
          f"{sum(1 for r in rows if r['ok'])} of {len(rows)} holds read back from PayPal as recorded"
          + (f" ({unreadable} unreadable)" if unreadable else ""))
    n_at_start = (d.settlement or {}).get("count", 0)
    if outcome == "funded":
        check("minimum was met", n_at_start >= d.minimum, f"{n_at_start} holds at close, minimum {d.minimum}")
        want = lad.price_at(d.ladder, n_at_start, d.locked_step)
        check("final price follows the ladder", want == final, f"ladder gives {m(want)} for {n_at_start} buyers, charged {m(final)}")
    else:
        check("minimum was missed", n_at_start < d.minimum, f"{n_at_start} holds at close, minimum {d.minimum}")
        check("nobody was charged", not captured and not failed and gross == 0, "no captures, no failed captures")
    st = d.settled["totals"]
    check("totals match PayPal", (m(gross), m(fees), m(net)) == (st["gross"], st["fees"], st["net"]),
          f"PayPal: gross {m(gross)}, fees {m(fees)}, net {m(net)}; engine: gross {st['gross']}, fees {st['fees']}, net {st['net']}")

    problems = [c["detail"] for c in checks if not c["ok"]] + [f"{r['buyer']}: {r['problem']}" for r in rows if not r["ok"]]
    out = {"drop": d.id, "ok": not problems, "outcome": outcome, "final_price": m(final), "checks": checks,
           "problems": problems, "counts": {"holds": len(holds), "captured": len(captured), "voided": len(voided),
                                            "failed": len(failed)},
           "totals": {"gross": m(gross), "fees": m(fees), "net": m(net)}}
    if void_failed:
        out["counts"]["void_failed"] = len(void_failed)
    if verbose or problems:
        out["holds"] = rows if verbose else [r for r in rows if not r["ok"]]
    return out


def _unreleased(h: dict, status: str) -> str:
    vf = h["void_failed"] or {}
    said = vf.get("issue") or vf.get("code") or "a refusal"
    dbg = f", debug id {vf['debug_id']}" if vf.get("debug_id") else ""
    return (f"PayPal refused to release this hold ({said}{dbg}) and it is still {status}: it may be held on the buyer's "
            f"card until it lapses{' on ' + h['expires'] if h.get('expires') else ''}. Next: open authorisation "
            f"{h['auth_id']} in the PayPal sandbox and void it by hand (or let it lapse), then run `drop verify` again")


def _strays(eng: Engine, scan: dict) -> list[str]:
    """Every authorisation PayPal has on the drop's orders that the log does not account for and that is not already
    gone: a hold on a buyer's card the engine never recorded (or recorded and then lost track of)."""
    out = []
    for oid in sorted(scan["orders"]):
        try:
            auths = eng.holds.order_authorizations(oid)
        except HoldError as e:
            out.append(f"could not read order {oid} from PayPal ({e.code}), so a hold on it cannot be ruled out")
            continue
        for a in auths:
            if a["auth_id"] not in scan["known"] and a["status"] not in ("VOIDED", "EXPIRED", "DENIED"):
                out.append(f"PayPal has authorisation {a['auth_id']} ({a['status']}, {a['amount']}) on order {oid} that the "
                           f"log does not account for. Next: void it in the PayPal sandbox if it is a hold, then run "
                           f"`drop verify` again")
    return out


def tieout_program(drop: str, engine: Engine | None = None) -> dict:
    """The Closure program for this drop: its capture ids (gross, fee) plus a rest part. Needs live captures."""
    eng = engine or default_engine()
    d = eng.get(drop)
    if d.state != "settled":
        raise CrowdError("drop_not_settled", drop=d.id, state=eng.display_state(d))
    ids = [b.capture_id for b in d.buyers.values() if b.status == "captured" and b.capture_id]
    if not ids:
        raise CrowdError("nothing_captured", drop=d.id)
    times = [parse_iso(eng.holds.get_capture(i)["created"]) for i in ids]
    start = min(times).astimezone(timezone.utc).replace(microsecond=0) - TIEOUT_MARGIN
    end = max(times).astimezone(timezone.utc).replace(microsecond=0) + TIEOUT_MARGIN
    z = "%Y-%m-%dT%H:%M:%SZ"
    return {"question": "Do this drop's captures tie out to the change in PayPal's balance?",
            "currency": d.currency, "start": start.strftime(z), "end": end.strftime(z),
            "answer": ["drop gross", "drop fees"],
            "parts": [{"name": "drop gross", "take": "gross", "where": {"id": ids}},
                      {"name": "drop fees", "take": "fee", "where": {"id": ids}},
                      {"name": "everything else", "take": "rest"}]}


def tieout(drop: str, engine: Engine | None = None, source=None) -> dict:
    """The balance proof. Returns the certifier's answer as it is (agent-safe view): a certificate summary,
    or its refusal. With a certificate, `engine_net` and `matches_engine` compare it with the engine's own net."""
    eng = engine or default_engine()
    if getattr(eng.holds, "kind", "") != "sandbox":
        return {"kind": "refusal", "reason": "no_transaction_search",
                "what_to_do": "The fake PayPal has no Transaction Search; run against the sandbox for a balance tie-out."}
    d = eng.get(drop)
    try:
        program = tieout_program(drop, eng)
    except CrowdError as e:
        if e.code == "nothing_captured":
            return {"kind": "refusal", "reason": "nothing_captured",
                    "what_to_do": "No money was captured, so there is no balance change to tie out; `drop verify` shows the holds were released."}
        raise
    from closure import service
    from closure.paypal import PayPalError
    from closure.sources import Live
    try:
        out = service.ask(source or Live(), program)
    except PayPalError as e:
        # Reality (probed 2026-10-27): before the certifier can say not_yet_indexed, Transaction Search itself answers
        # 404 "Data for the given start date is not available" while PayPal's index lags behind the window.
        if "http 404" in str(e):
            return {"kind": "refusal", "reason": "not_yet_indexed", "question": program["question"],
                    "detail": {"explanation": "PayPal has not indexed this window yet: Transaction Search says data for the "
                                              "start date is not available."},
                    "what_to_do": service.HINTS["not_yet_indexed"]}
        return {"kind": "refusal", "reason": "transaction_search_unavailable", "question": program["question"],
                "detail": {"explanation": "PayPal's Transaction Search could not be read."},
                "what_to_do": "Try again later; nothing was changed."}
    if out.get("kind") == "certificate":
        engine_net = lad.D(d.settled["totals"]["net"])
        got = lad.D(out["answer"]["value"])
        out["engine_net"] = m(engine_net)
        out["matches_engine"] = got == engine_net
    return out
