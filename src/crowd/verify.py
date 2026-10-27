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
from .engine import NO_MONEY_CAPTURE, CrowdError, Engine, default_engine, m
from .paypal_holds import HoldError, HoldNotFound, mode_of
from .store import parse_iso

TIEOUT_MARGIN = timedelta(seconds=30)


CAPTURED = ("CAPTURED", "PARTIALLY_CAPTURED")
GONE = ("VOIDED", "EXPIRED", "DENIED")   # PayPal statuses in which a hold no longer holds money (DENIED: never granted)


def with_next(problem: str, next_step: str | None) -> str:
    """A problem always says what to do about it: the text, then `Next: ...` (once)."""
    return problem if (not next_step or "Next:" in problem) else f"{problem} Next: {next_step}"


def _scan(events: list[dict]) -> dict:
    """What the event log says about every hold, order and join (from events, not the projection)."""
    holds, by_auth = [], {}
    known: set = set()          # authorisations the log accounts for: placed, or released as an orphan
    released: set = set()       # orphan authorisations the log says were released: PayPal must say so too
    stuck: set = set()          # orphan authorisations the engine could not release: PayPal must show them gone
    orders: dict = {}           # every PayPal order the log knows of -> who it was for
    for e in events:
        t, p = e["type"], e["payload"]
        if p.get("order_id"):
            orders.setdefault(p["order_id"], p.get("buyer"))
        if t == "hold_placed":
            h = {"buyer": p["buyer"], "auth_id": p["auth_id"], "amount": lad.D(p["amount"]), "end": None,
                 "capture_id": None, "engine_amount": None, "engine_fee": None, "mode": p.get("paypal_mode"),
                 "expires": p.get("expires"), "failed": None, "void_failed": None, "released": False,
                 "released_reason": None, "voided_expired": False, "voided_status": None, "released_by_void": False,
                 "taken": False, "order_id": p.get("order_id"), "taken_captures": []}
            holds.append(h)
            by_auth[p["auth_id"]] = h
            known.add(p["auth_id"])
        elif t == "orphan_hold_released" and p.get("auth_id"):
            known.add(p["auth_id"])
            released.add(p["auth_id"])
        elif t in ("hold_captured", "hold_voided", "capture_failed") and p.get("auth_id") in by_auth:
            h = by_auth[p["auth_id"]]
            h["end"] = {"hold_captured": "captured", "hold_voided": "voided", "capture_failed": "failed"}[t]
            if t == "hold_captured":
                h["capture_id"], h["engine_amount"], h["engine_fee"] = p["capture_id"], lad.D(p["amount"]), lad.D(p["fee"])
            elif t == "capture_failed":
                h["failed"] = p
            else:
                h["voided_expired"] = bool(p.get("expired"))
                h["voided_status"] = p.get("paypal_status")
                h["released_by_void"] = bool(p.get("released_by_void"))
                h["taken"] = bool(p.get("taken_outside_engine"))
                h["taken_captures"] = list(p.get("capture_ids") or [])
        elif t == "failed_hold_released" and p.get("auth_id") in by_auth:
            by_auth[p["auth_id"]]["released"] = True
            by_auth[p["auth_id"]]["released_reason"] = p.get("reason")
        elif t == "void_failed" and p.get("orphan") and p.get("auth_id"):
            known.add(p["auth_id"])
            stuck.add(p["auth_id"])
        elif t == "void_failed" and not p.get("orphan") and p.get("auth_id") in by_auth:
            h = by_auth[p["auth_id"]]
            h["void_failed"] = p
            if h["end"] is None:
                h["end"] = "void_failed"
    return {"holds": holds, "known": known, "orders": orders, "released": released, "stuck": stuck}


def _holds(events: list[dict]) -> list[dict]:
    """Every hold ever placed in a drop with the way it ended, from events (not the projection)."""
    return _scan(events)["holds"]


def _evidence_of(p: dict) -> bool:
    """Does a failed capture carry something PayPal itself said (an error name, a debug id, a status)? Without it the
    failure is just the engine's say-so. (A hold the engine let lapse on its own clock's word is not evidence either:
    `hold_expired` needs PayPal's EXPIRED.)"""
    if p.get("code") == "hold_expired":
        return p.get("paypal_status") == "EXPIRED"
    return bool(p.get("issue") or p.get("debug_id") or p.get("paypal_status"))


def _settle_cmd(drop: str) -> str:
    return f"togi crowd drop plan --drop {drop}"


def _verify_cmd(drop: str) -> str:
    return f"`togi crowd drop verify --drop {drop}`"


def _blocked_report(eng: Engine, d, verbose: bool) -> dict:
    """A settlement that stopped for a person (`settle_blocked`) is not settled and not fine: say what stopped it."""
    b = d.blocked or {}
    said = b.get("issue") or b.get("code") or "a refusal"
    dbg = f", debug id {b['debug_id']}" if b.get("debug_id") else ""
    who = f" while handling {b['buyer']}" if b.get("buyer") else ""
    held = d.buyers.get(str(b.get("buyer"))) if b.get("buyer") else None
    way = (f"void hold {held.auth_id} at PayPal by hand, or run `togi crowd drop exclude --drop {d.id} --buyer {held.id}` to "
           f"release it and leave {held.id} out") if held is not None and held.auth_id else None
    nxt = (f"a person (the maker, who owns the PayPal account) looks at PayPal's answer{dbg}, fixes the cause"
           + (f" (to go on without that one hold: {way})" if way else "")
           + f", then runs `{_settle_cmd(d.id)}` and approves the fresh plan")
    if b.get("http") is None and not b.get("issue"):       # the engine's own stop (a run of failed captures), not a PayPal answer
        said_how = f"settlement is blocked: {b.get('message') or said}"
    else:
        said_how = f"settlement is blocked: PayPal answered {said} (http {b.get('http')}){dbg}"
    problem = with_next(f"{said_how} during {b.get('phase', 'settlement')}{who}, so the engine stopped. Nothing past that point "
                        f"was captured or released, and the drop is still settling.", nxt)
    holding = len(d.holding)
    return {"drop": d.id, "ok": False, "blocked": True, "outcome": (d.settlement or {}).get("outcome"),
            "checks": [{"check": "settlement is not blocked", "ok": False, "detail": problem}],
            "problems": [problem], "problem_details": [{"problem": problem, "next": nxt}], "next": nxt,
            "counts": {"holds_still_open": holding}}


def _capture_audit(eng: Engine, scan: dict, d, final) -> list[dict]:
    """Read back EVERY capture PayPal shows on each of the drop's orders (not just the id the engine recorded) and compare:
    a capture the engine did not record is a problem (it is somebody's money to refund, whoever took it); so is a capture in
    another currency or none, and a hold charged more in total than the final price. Returns [{problem, next}]."""
    listing = getattr(eng.holds, "order_captures", None)
    again = f"then run {_verify_cmd(d.id)} again"
    if listing is None:
        return [{"problem": "this PayPal client cannot list the captures on an order, so a capture the engine did not make cannot "
                            "be ruled out",
                 "next": f"open each of the drop's orders in the PayPal sandbox and check its captures, {again}"}]
    holds = scan["holds"]
    recorded = {h["capture_id"] for h in holds if h["capture_id"]}
    by_order = {h["order_id"]: h for h in holds if h.get("order_id")}
    out = []
    for oid in sorted(scan["orders"]):
        try:
            caps = listing(oid)
        except HoldNotFound:
            caps = []
        except HoldError as e:
            out.append({"problem": f"could not read the captures on order {oid} from PayPal ({e.code}), so a capture the engine "
                                   f"did not make cannot be ruled out",
                        "next": f"run verify again when PayPal answers; if it keeps failing, open order {oid} in the PayPal "
                                f"sandbox and check its captures"})
            continue
        total = lad.ZERO
        for c in caps:
            if c.get("status") in NO_MONEY_CAPTURE:
                continue
            cid, amount = c.get("capture_id"), lad.D(c["amount"]) if c.get("amount") is not None else None
            if c.get("currency") != d.currency:
                out.append({"problem": f"capture {cid} on order {oid} is in {c.get('currency') or 'an unstated currency'} but the "
                                       f"drop is in {d.currency}",
                            "next": f"open capture {cid} in the PayPal sandbox and work out which currency is right, {again}"})
            if cid not in recorded:
                out.append({"problem": f"PayPal shows capture {cid} ({m(amount) if amount is not None else '?'} {c.get('currency')}, "
                                       f"{c.get('status')}) on order {oid} that the engine did not record: money was taken from a "
                                       f"buyer outside the engine",
                            "next": f"refund capture {cid} in the PayPal sandbox (it was taken from the buyer's hold outside the "
                                    f"engine), {again}"})
            total += amount or lad.ZERO
        h = by_order.get(oid)
        if h is not None and total > final:
            out.append({"problem": f"{h['buyer']} was charged {m(total)} in total on order {oid}, more than the final price "
                                   f"{m(final)}",
                        "next": f"open order {oid} in the PayPal sandbox and refund the capture that should not have been taken, "
                                f"{again}"})
    return out


def verify(drop: str, engine: Engine | None = None, verbose: bool = True) -> dict:
    eng = engine or default_engine()
    d = eng.get(drop)
    eng.require_paypal(d.id)        # a hold read from another PayPal says nothing: refuse, never report
    if d.state != "settled":
        if d.state == "settling" and d.blocked:
            return _blocked_report(eng, d, verbose)
        raise CrowdError("drop_not_settled", drop=d.id, state=eng.display_state(d))
    events = eng.store.events(d.id)
    scan = _scan(events)
    holds = scan["holds"]
    final = lad.D(d.settled["final_price"])
    outcome = d.settled["outcome"]
    mode_here = mode_of(eng.holds)
    checks: list[dict] = []
    details: list[dict] = []        # every problem with the concrete step that deals with it

    def check(name: str, ok: bool, detail: str, next_step: str | None = None) -> bool:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            details.append({"problem": with_next(detail, next_step), "next": next_step})
        return ok

    rows = []
    gross = fees = net = lad.ZERO
    unreadable = 0
    amount_bad = 0
    parent_unknown = parent_bad = 0
    for h in holds:
        row = {"buyer": h["buyer"], "auth_id": h["auth_id"], "engine": h["end"], "paypal": None, "ok": True,
               "problem": None, "next": None}
        rows.append(row)
        hand = (f"open authorisation {h['auth_id']} in the PayPal sandbox, compare it with what the engine recorded, put right "
                f"whichever is wrong, then run {_verify_cmd(d.id)} again")

        def bad(msg: str, next_step: str | None = None, row=row, hand=hand) -> None:
            if row["ok"]:      # the first problem is the one shown; later ones would only repeat it
                row["ok"], row["problem"], row["next"] = False, msg, next_step or hand

        if h["end"] is None:
            bad("hold never settled: the engine has no capture, void or failure for it",
                f"run `{_settle_cmd(d.id)}` and have a person approve the plan, so the hold gets an outcome")
            continue
        try:
            auth = eng.holds.get(h["auth_id"])
            row["paypal"] = auth["status"]
            status = auth["status"]
            held = lad.D(auth["amount"]) if auth.get("amount") is not None else None
            # PayPal's own figure for the hold must be the one the engine recorded (after a capture PayPal may show the
            # amount it took instead of the amount it held; anything else is a mismatch)
            allowed = {h["amount"]} | ({h["engine_amount"]} if h["end"] == "captured" else set())
            if held is None or held not in allowed or auth.get("currency") != d.currency:      # no currency is a mismatch
                amount_bad += 1
                bad(f"PayPal shows this hold as {auth.get('amount')} {auth.get('currency')} but the engine recorded "
                    f"{m(h['amount'])} {d.currency}")
            if h["end"] == "captured":
                cap = eng.holds.get_capture(h["capture_id"])
                row["capture_id"] = h["capture_id"]
                amount, cfee = lad.D(cap["amount"]), lad.D(cap["fee"] or "0")
                cap_hand = (f"open capture {h['capture_id']} in the PayPal sandbox, work out which of PayPal and the engine is "
                            f"right (refund it if it should not have been taken), then run {_verify_cmd(d.id)} again")
                if status != "CAPTURED":
                    bad(f"engine says captured but PayPal says the hold is {status}", cap_hand)
                elif cap["status"] not in ("COMPLETED", "PENDING"):
                    bad(f"PayPal says the capture is {cap['status']}", cap_hand)
                elif cap.get("currency") != d.currency:
                    bad(f"PayPal captured in {cap.get('currency') or 'an unstated currency'} but the drop is in {d.currency}",
                        cap_hand)
                elif cap.get("auth_id") and cap["auth_id"] != h["auth_id"]:
                    parent_bad += 1
                    bad(f"PayPal says capture {h['capture_id']} belongs to authorisation {cap['auth_id']}, not to the hold "
                        f"{h['auth_id']} the engine recorded it against", cap_hand)
                elif amount != final:
                    bad(f"captured {m(amount)} but the final price is {m(final)}", cap_hand)
                elif amount > h["amount"]:
                    bad(f"captured {m(amount)} which is more than the {m(h['amount'])} hold", cap_hand)
                elif cap.get("final") is not True:
                    bad("the capture is not marked final", cap_hand)
                elif amount != h["engine_amount"] or cfee != h["engine_fee"]:
                    bad("PayPal's amount or fee differs from what the engine recorded", cap_hand)
                else:
                    if not cap.get("auth_id"):
                        parent_unknown += 1
                    gross, fees, net = gross + amount, fees + cfee, net + lad.D(cap["net"] or (amount - cfee))
                    row["amount"], row["fee"] = m(amount), m(cfee)
            elif h["end"] == "voided":
                if h["taken"] or status in CAPTURED:
                    caps = ", ".join(h["taken_captures"])
                    bad(f"engine says voided but PayPal says the hold is {status}: money was taken from the buyer outside "
                        f"the engine", (f"refund capture {caps} in the PayPal sandbox (it was taken from authorisation "
                                        f"{h['auth_id']} outside the engine)" if caps else
                                        f"open authorisation {h['auth_id']} in the PayPal sandbox, find the capture on it, and "
                                        f"refund it") + f" if the buyer should not have been charged; then run {_verify_cmd(d.id)} again")
                elif h["released_by_void"]:
                    if status != "VOIDED":
                        bad(f"engine says it voided this hold but PayPal says it is {status}")
                elif h["voided_status"]:     # recorded from what PayPal itself said: it must still say it
                    if status != h["voided_status"]:
                        bad(f"engine recorded PayPal saying {h['voided_status']} but PayPal now says {status}")
                elif not (status == "VOIDED" or (h["voided_expired"] and status in GONE)):
                    bad(f"engine says voided but PayPal says the hold is {status}")
            elif h["end"] == "void_failed":      # PayPal would not release this hold
                if status in CAPTURED:
                    bad(f"the release was refused and PayPal now says the hold is {status}: money was taken from a drop "
                        f"that missed its minimum", f"open authorisation {h['auth_id']} in the PayPal sandbox and refund the "
                        f"capture; then run {_verify_cmd(d.id)} again")
                elif status not in GONE:
                    bad(_unreleased(h, status, d.id))
            else:  # failed capture: nothing may have been taken, and the hold must have been released
                fp = h["failed"] or {}
                if not _evidence_of(fp):
                    bad("the capture is recorded as failed with no PayPal evidence (no error name, debug id or status "
                        "from PayPal): it cannot be told apart from a guess"
                        + (" (an expiry judged by the engine's own clock is not evidence)" if fp.get("code") == "hold_expired"
                           else ""))
                elif status in CAPTURED:
                    bad(f"engine says the capture failed but PayPal says the hold is {status}")
                elif fp.get("code") == "hold_expired" and status not in GONE:
                    bad(f"engine says the hold expired but PayPal says it is still {status}: it could have been captured")
                elif h["void_failed"] and status not in GONE:
                    bad(_unreleased(h, status, d.id))
                elif h["released"] and h["released_reason"] == "expired" and status not in GONE:
                    bad(f"engine says the failed hold lapsed but PayPal says it is {status}")
                elif h["released"] and h["released_reason"] != "expired" and status != "VOIDED":
                    bad(f"engine says the failed hold was released but PayPal says it is {status}")
                elif not h["released"] and not h["void_failed"] and status not in GONE:
                    bad("the capture failed and the hold is still open on the buyer's card",
                        f"run `{_settle_cmd(d.id)}` and settle again to release it, or void authorisation {h['auth_id']} "
                        f"in the PayPal sandbox by hand; then run {_verify_cmd(d.id)} again")
        except HoldError as e:
            unreadable += 1
            bad(f"could not read this hold from PayPal: {e.code}",
                f"run {_verify_cmd(d.id)} again when PayPal answers; if it keeps failing, open authorisation "
                f"{h['auth_id']} in the PayPal sandbox")

    captured = [h for h in holds if h["end"] == "captured"]
    voided = [h for h in holds if h["end"] == "voided"]
    failed = [h for h in holds if h["end"] == "failed"]
    void_failed = [h for h in holds if h["end"] == "void_failed"]
    open_ = [h for h in holds if h["end"] is None]
    counts = d.settled["counts"]

    check("settled", True, f"outcome {outcome}")
    check("holds accounted for", not open_ and len(holds) == len(captured) + len(voided) + len(failed) + len(void_failed),
          f"{len(holds)} holds = {len(captured)} captured + {len(voided)} voided + {len(failed)} failed"
          + (f" + {len(void_failed)} that could not be released" if void_failed else ""),
          f"run `{_settle_cmd(d.id)}` and settle again so every hold gets an outcome")
    check("engine counts match its events",
          (counts["holds"], counts["captured"], counts["voided"], counts["failed"], counts.get("void_failed", 0)) ==
          (len(holds), len(captured), len(voided), len(failed), len(void_failed)), f"settled record says {counts}",
          f"treat the event log as the record: run `togi crowd drop explain --drop {d.id}` and compare, and tell the owner "
          f"the settled record disagrees with its own events")
    # a join PayPal may have acted on that the log never closed: a hold the engine does not know about
    check("no join left unconfirmed", not d.unresolved,
          "every join was confirmed or released" if not d.unresolved else
          f"{len(d.unresolved)} join(s) started but never confirmed (" + ", ".join(
              sorted(str(j.get('buyer')) for j in d.unresolved.values())) + "): a PayPal hold may be open that the "
          "engine does not know about",
          f"run `togi crowd drop close --drop {d.id}` or `{_settle_cmd(d.id)}` again to reconcile, then verify")
    check("no leave left unconfirmed", not d.leaving,
          "every leave was confirmed or dropped" if not d.leaving else
          f"{len(d.leaving)} leave(s) started but never confirmed (" + ", ".join(sorted(d.leaving)) + "): the hold may be "
          "voided at PayPal while the engine still counts the buyer",
          f"run `togi crowd drop close --drop {d.id}` or `{_settle_cmd(d.id)}` again to reconcile, then verify")
    strays = _strays(eng, scan, d.id)
    check("every PayPal authorisation on the drop's orders is accounted for", not strays,
          f"{len(scan['orders'])} order(s) read back from PayPal, every authorisation on them is accounted for and in the "
          f"state the log says" if not strays else "; ".join(s["problem"] for s in strays),
          strays[0]["next"] if strays else None)
    check("every hold's amount matches PayPal's", amount_bad == 0,
          "each hold read back from PayPal at the amount the engine recorded" if not amount_bad
          else f"{amount_bad} hold(s) differ from what the engine recorded",
          "open each named authorisation in the PayPal sandbox and compare its amount with the engine's")
    check("every capture belongs to the hold the engine recorded", parent_bad == 0,
          f"{len(captured) - parent_unknown} of {len(captured)} captures read back with their parent authorisation"
          + (f"; PayPal did not name the parent of {parent_unknown}, so those were not compared" if parent_unknown else "")
          if not parent_bad else f"{parent_bad} capture(s) belong to a different authorisation than the engine recorded",
          "open each named capture in the PayPal sandbox and find which authorisation it was taken from")
    recorded = {(p.get("paypal_mode"), p.get("paypal_ref")) for e in events for p in [e["payload"]] if p.get("paypal_mode")}
    modes = {r[0] for r in recorded}
    other = {r for r in recorded if not eng._same_paypal(r)}        # 'unknown' = a wrapper that would not say: nothing to compare
    check("recorded against the PayPal being checked", not other,
          f"every event that records something PayPal did or refused (placed, voided, captured, refused) was recorded against "
          f"{', '.join(sorted(modes)) or 'an unrecorded PayPal'}; checking against {mode_here}" if not other else
          f"events were recorded against {', '.join(sorted(r[0] for r in other))}; checking against {mode_here}",
          "run verify with the same PayPal (fake or sandbox) the drop was settled against")
    audit = _capture_audit(eng, scan, d, final)
    check("every capture on the drop's orders is one the engine recorded", not audit,
          f"every capture PayPal shows on the drop's {len(scan['orders'])} order(s) is one the engine recorded, in the drop's "
          f"currency, and no hold was charged more than the final price" if not audit else
          "; ".join(a["problem"] for a in audit), audit[0]["next"] if audit else None)
    check("every hold is in the state the engine says", all(r["ok"] for r in rows),
          f"{sum(1 for r in rows if r['ok'])} of {len(rows)} holds read back from PayPal as recorded"
          + (f" ({unreadable} unreadable)" if unreadable else ""),
          next((r["next"] for r in rows if not r["ok"]), None))
    n_at_start = (d.settlement or {}).get("count", 0)
    if outcome == "funded":
        check("minimum was met", n_at_start >= d.minimum, f"{n_at_start} holds at close, minimum {d.minimum}",
              f"tell the owner: a funded settlement ran below the minimum; open `togi crowd drop explain --drop {d.id}`")
        want = lad.price_at(d.ladder, n_at_start, d.locked_step)
        check("final price follows the ladder", want == final, f"ladder gives {m(want)} for {n_at_start} buyers, charged {m(final)}",
              f"compare the charged price with the ladder (`togi crowd drop status --drop {d.id}`) and tell the owner")
    else:
        check("minimum was missed", n_at_start < d.minimum, f"{n_at_start} holds at close, minimum {d.minimum}",
              f"tell the owner: a missed-minimum settlement ran at or above the minimum; open `togi crowd drop explain --drop {d.id}`")
        check("nobody was charged", not captured and not failed and gross == 0, "no captures, no failed captures",
              f"open the named captures in the PayPal sandbox and refund them; the drop missed its minimum")
    st = d.settled["totals"]
    check("totals match PayPal", (m(gross), m(fees), m(net)) == (st["gross"], st["fees"], st["net"]),
          f"PayPal: gross {m(gross)}, fees {m(fees)}, net {m(net)}; engine: gross {st['gross']}, fees {st['fees']}, net {st['net']}",
          "compare the captures one by one (`togi crowd drop verify --drop " + d.id + " --verbose`) and find the one that differs")

    row_details = [{"problem": with_next(f"{r['buyer']}: {r['problem']}", r["next"]), "next": r["next"]}
                   for r in rows if not r["ok"]]
    problem_details = details + row_details
    problems = [x["problem"] for x in problem_details]
    out = {"drop": d.id, "ok": not problems, "outcome": outcome, "final_price": m(final), "checks": checks,
           "problems": problems, "problem_details": problem_details,
           "next": next((x["next"] for x in problem_details if x["next"]), None),
           "counts": {"holds": len(holds), "captured": len(captured), "voided": len(voided), "failed": len(failed)},
           "totals": {"gross": m(gross), "fees": m(fees), "net": m(net)}}
    if void_failed:
        out["counts"]["void_failed"] = len(void_failed)
    if verbose or problems:
        out["holds"] = rows if verbose else [r for r in rows if not r["ok"]]
    return out


def _unreleased(h: dict, status: str, drop: str = "") -> str:
    vf = h["void_failed"] or {}
    said = vf.get("issue") or vf.get("code") or "a refusal"
    dbg = f", debug id {vf['debug_id']}" if vf.get("debug_id") else ""
    return (f"PayPal refused to release this hold ({said}{dbg}) and it is still {status}: it may be held on the buyer's "
            f"card until it lapses{' on ' + h['expires'] if h.get('expires') else ''}. Next: open authorisation "
            f"{h['auth_id']} in the PayPal sandbox and void it by hand (or let it lapse), then run "
            f"`togi crowd drop verify --drop {drop}` again" if drop else
            f"PayPal refused to release this hold ({said}{dbg}) and it is still {status}. Next: open authorisation "
            f"{h['auth_id']} in the PayPal sandbox and void it by hand (or let it lapse), then run `drop verify` again")


def _strays(eng: Engine, scan: dict, drop: str = "") -> list[dict]:
    """Read back EVERY authorisation PayPal has on the drop's orders and compare it with what the log says about it: one the
    log does not account for must be gone; one the log calls a released orphan must be gone; one the log could not release
    must be gone. An authorisation that has been CAPTURED is not a hold to void: the money is the capture's to refund, so the
    next step names the capture. Returns [{problem, next}]."""
    out = []
    again = f"then run `togi crowd drop verify --drop {drop}` again" if drop else "then run `drop verify` again"
    listing = getattr(eng.holds, "order_captures", None)
    recorded = {h["capture_id"] for h in scan["holds"] if h["capture_id"]}

    def refund_or_void(aid: str, oid: str, st: str) -> str:
        if st not in CAPTURED:
            return f"open authorisation {aid} in the PayPal sandbox and void it by hand, {again}"
        try:       # the captures on this authorisation that the engine did not make (PayPal's order listing may not say whose)
            ids = [c["capture_id"] for c in (listing(oid) if listing else [])
                   if c.get("status") not in NO_MONEY_CAPTURE and c.get("capture_id") not in recorded
                   and c.get("auth_id") in (None, aid)]
        except HoldError:
            ids = []
        return (f"refund capture {', '.join(ids)} in the PayPal sandbox (authorisation {aid} was captured, so there is no hold left "
                f"to void), {again}" if ids else
                f"open authorisation {aid} in the PayPal sandbox, find the capture on it and refund it (it was captured, so there "
                f"is no hold left to void), {again}")

    for oid in sorted(scan["orders"]):
        try:
            auths = eng.holds.order_authorizations(oid)
        except HoldError as e:
            out.append({"problem": f"could not read order {oid} from PayPal ({e.code}), so a hold on it cannot be ruled out",
                        "next": f"run verify again when PayPal answers; if it keeps failing, open order {oid} in the PayPal "
                                f"sandbox and check its authorisations"})
            continue
        for a in auths:
            aid, st = a["auth_id"], a["status"]
            if aid in scan["stuck"] and st not in GONE:
                out.append({"problem": f"PayPal refused to release orphan hold {aid} on order {oid} and it is still {st}",
                            "next": refund_or_void(aid, oid, st)})
            elif aid in scan["released"] and st not in GONE:
                out.append({"problem": f"the engine recorded authorisation {aid} on order {oid} as released but PayPal says "
                                       f"it is {st}",
                            "next": refund_or_void(aid, oid, st)})
            elif aid not in scan["known"] and st not in GONE:
                out.append({"problem": f"PayPal has authorisation {aid} ({st}, {a['amount']}) on order {oid} that the log does "
                                       f"not account for",
                            "next": refund_or_void(aid, oid, st) if st in CAPTURED else
                                    f"void it in the PayPal sandbox if it is a hold, {again}"})
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
