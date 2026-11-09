#!/usr/bin/env python3
"""Sandbox seeder. Creates known activity in the PayPal SANDBOX and logs what it intended.

The log (data/seeder-log.jsonl, git-ignored) is the scorecard's answer key: one JSON line per
action with the amounts WE sent and the ids PayPal returned. It never reads Transaction Search.

Usage:
  python3 harness/seeder.py --dry-run          print the plan, call nothing
  python3 harness/seeder.py                    run today's batch (safe to rerun: same-day request ids dedupe)
  python3 harness/seeder.py --batch NAME       use another batch name (a fresh set of activity)
Batch = ~10 actions: test-card captures, a partial refund, a payout, one EUR capture.
"""
import argparse, json, os, sys, urllib.error, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import killtest as k  # reuses load_env (refuses non-sandbox), BASE, CTX, call

LOG = os.path.join(k.ROOT, "data", "seeder-log.jsonl")
CARD = {"number": "4111111111111111", "expiry": "2030-12", "security_code": "123", "name": "Test Buyer"}
PAYOUT_TO = "closure-payee@example.com"

# (label, currency, amount, refund_amount or None)
CAPTURES = [
    ("coffee-beans",   "USD", "42.50",  None),
    ("design-retainer", "USD", "1200.00", "300.00"),   # partial refund
    ("print-run",      "USD", "86.13",  None),
    ("workshop-seat",  "USD", "250.00", None),
    ("stickers",       "USD", "19.99",  None),
    ("consulting-hr",  "USD", "175.00", None),
    ("berlin-order",   "EUR", "310.40", None),          # the currency-conversion case
]
PAYOUT = ("freelancer-payout", "USD", "150.00")


def api(method, path, token, body=None, request_id=None):
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(k.BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60, context=k.CTX) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def log(entry):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    entry["at"] = datetime.now(timezone.utc).isoformat()
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def seen(batch):
    """{(kind, label): entry} for this batch's successful actions."""
    if not os.path.exists(LOG):
        return {}
    out = {}
    for line in open(LOG):
        e = json.loads(line)
        if e.get("batch") == batch and e.get("ok"):
            out[(e["kind"], e["label"])] = e
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    a = ap.parse_args()
    env = k.load_env()

    if a.dry_run:
        for label, cur, amt, ref in CAPTURES:
            print(f"capture  {label:16} {cur} {amt}" + (f"  then refund {ref}" if ref else ""))
        print(f"payout   {PAYOUT[0]:16} {PAYOUT[1]} {PAYOUT[2]}")
        return

    st, tok = k.call("POST", "/v1/oauth2/token", basic=f"{env['PAYPAL_CLIENT_ID']}:{env['PAYPAL_CLIENT_SECRET']}",
                     data=b"grant_type=client_credentials")
    if st != 200:
        sys.exit(f"token failed: http {st}")
    token = tok["access_token"]
    done = seen(a.batch)
    failures = 0

    for label, cur, amt, ref in CAPTURES:
        rid = f"closure-{a.batch}-cap-{label}"
        if ("capture", label) in done:
            print(f"skip     capture {label} (already done in batch {a.batch})")
            cap_id = done[("capture", label)]["capture_id"]
        else:
            st, o = api("POST", "/v2/checkout/orders", token, request_id=rid, body={
                "intent": "CAPTURE",
                "purchase_units": [{"reference_id": label, "amount": {"currency_code": cur, "value": amt}}],
                "payment_source": {"card": CARD}})
            cap = (((o.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [{}])[0]
            ok = st in (200, 201) and cap.get("status") == "COMPLETED"
            log({"batch": a.batch, "kind": "capture", "label": label, "ok": ok, "currency": cur, "amount": amt,
                 "order_id": o.get("id"), "capture_id": cap.get("id"),
                 "paypal_fee": ((cap.get("seller_receivable_breakdown") or {}).get("paypal_fee") or {}).get("value"),
                 "http": st, "error": None if ok else (o.get("name") or o.get("message"))})
            print(("ok       " if ok else "FAIL     ") + f"capture {label} {cur} {amt} -> http {st} {cap.get('id', '')}")
            failures += not ok
            cap_id = cap.get("id") if ok else None
        if ref and ("refund", label) in done:
            print(f"skip     refund  {label} (already done in batch {a.batch})")
        elif ref and cap_id:  # also retries a refund that failed on an earlier run of this batch
            st, r = api("POST", f"/v2/payments/captures/{cap_id}/refund", token, request_id=rid + "-refund",
                        body={"amount": {"currency_code": cur, "value": ref}})
            rok = st in (200, 201) and r.get("status") == "COMPLETED"
            log({"batch": a.batch, "kind": "refund", "label": label, "ok": rok, "currency": cur, "amount": ref,
                 "capture_id": cap_id, "refund_id": r.get("id"), "http": st,
                 "error": None if rok else (r.get("name") or r.get("message"))})
            print(("ok       " if rok else "FAIL     ") + f"refund  {label} {cur} {ref} -> http {st} {r.get('id', '')}")
            failures += not rok

    label, cur, amt = PAYOUT
    if ("payout", label) in done:
        print(f"skip     payout  {label} (already done in batch {a.batch})")
    else:
        st, p = api("POST", "/v1/payments/payouts", token, request_id=f"closure-{a.batch}-payout-{label}", body={
            "sender_batch_header": {"sender_batch_id": f"closure-{a.batch}-{label}", "email_subject": "Payment"},
            "items": [{"recipient_type": "EMAIL", "receiver": PAYOUT_TO, "note": label,
                       "amount": {"currency": cur, "value": amt}, "sender_item_id": f"{a.batch}-{label}"}]})
        pok = st in (200, 201)
        log({"batch": a.batch, "kind": "payout", "label": label, "ok": pok, "currency": cur, "amount": amt,
             "payout_batch_id": (p.get("batch_header") or {}).get("payout_batch_id"), "http": st,
             "error": None if pok else (p.get("name") or p.get("message"))})
        print(("ok       " if pok else "FAIL     ") + f"payout  {label} {cur} {amt} -> http {st}")
        failures += not pok

    print(f"\nlog: {LOG}\n{'DONE' if not failures else f'{failures} action(s) failed'} (batch {a.batch})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
