#!/usr/bin/env python3
"""Gate 0 kill test (PLAN.md). Read-only checks against the PayPal SANDBOX. Never prints secrets.

Checks:
  scope    token carries the Transaction Search scope
  search   unfiltered Transaction Search works; reports whether ending_balance is populated
  filtered same query with a filter: ending_balance should come back empty (confirms the toolkit gotcha)
  window   a range over 31 days is rejected (confirms the 31-day limit the stock toolkit hides)
  balances Balances API answers, and answers for a past as_of_time
  summary  balance net/daily summary endpoints answer in sandbox
Usage: python3 harness/killtest.py        (reads .env in the repo root)
"""
import base64, json, os, ssl, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://api-m.sandbox.paypal.com"
# python.org builds ship without CA certs on macOS; fall back to the system bundle.
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem") if os.path.exists("/etc/ssl/cert.pem") else ssl.create_default_context()


def load_env():
    env = {}
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                env[k] = v
    if env.get("PAYPAL_ENV") != "sandbox":
        sys.exit("refusing to run: PAYPAL_ENV must be sandbox")
    return env


def call(method, path, token=None, basic=None, data=None, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=data)
    if basic:
        req.add_header("Authorization", "Basic " + base64.b64encode(basic.encode()).decode())
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S-0000")


def main():
    env = load_env()
    st, tok = call("POST", "/v1/oauth2/token", basic=f"{env['PAYPAL_CLIENT_ID']}:{env['PAYPAL_CLIENT_SECRET']}",
                   data=b"grant_type=client_credentials")
    if st != 200:
        sys.exit(f"token failed: http {st}")
    token, scope = tok["access_token"], tok.get("scope", "")
    now = datetime.now(timezone.utc)
    results = {}

    results["scope"] = ("reporting/search" in scope, "has Transaction Search scope" if "reporting/search" in scope
                        else "token has NO reporting/search scope (feature not active yet, or app lacks it)")

    st, d = call("GET", "/v1/reporting/transactions", token, params={
        "start_date": iso(now - timedelta(days=3)), "end_date": iso(now), "fields": "all",
        "balance_affecting_records_only": "Y", "page_size": 100})
    txs = d.get("transaction_details", []) if st == 200 else []
    eb = sum(1 for t in txs if (t.get("transaction_info") or {}).get("ending_balance"))
    results["search"] = (st == 200, f"http {st}; {len(txs)} transactions; ending_balance on {eb}; "
                         f"last_refreshed={d.get('last_refreshed_datetime')}" if st == 200 else f"http {st} {d.get('name')}")

    st, d = call("GET", "/v1/reporting/transactions", token, params={
        "start_date": iso(now - timedelta(days=3)), "end_date": iso(now), "fields": "all",
        "transaction_status": "S", "page_size": 100})
    ftxs = d.get("transaction_details", []) if st == 200 else []
    feb = sum(1 for t in ftxs if (t.get("transaction_info") or {}).get("ending_balance"))
    results["filtered"] = (st == 200, f"http {st}; {len(ftxs)} transactions; ending_balance on {feb} "
                           f"(expected 0 when a filter is passed)" if st == 200 else f"http {st}")

    st, d = call("GET", "/v1/reporting/transactions", token, params={
        "start_date": iso(now - timedelta(days=45)), "end_date": iso(now), "fields": "transaction_info"})
    results["window"] = (st in (400, 422), f"45-day range -> http {st} {d.get('name', '')} (400 confirms the 31-day limit)")

    st, d = call("GET", "/v1/reporting/balances", token, params={"as_of_time": iso(now), "currency_code": "USD"})
    st2, _ = call("GET", "/v1/reporting/balances", token, params={"as_of_time": iso(now - timedelta(days=1)), "currency_code": "USD"})
    results["balances"] = (st == 200 and st2 == 200, f"now -> http {st}; yesterday -> http {st2}")

    sums = []
    for path in ("/v1/reporting/transactions/balance-net-summary", "/v1/reporting/transactions/daily-summary"):
        s, _ = call("GET", path, token, params={"start_date": iso(now - timedelta(days=2)), "end_date": iso(now)})
        sums.append(f"{path.rsplit('/', 1)[1]} -> http {s}")
    results["summary"] = (False, "; ".join(sums) + " (informational: paths unverified, see PayPal docs)")

    width = max(len(k) for k in results)
    for k, (ok, msg) in results.items():
        print(f"{'PASS' if ok else 'FAIL'}  {k.ljust(width)}  {msg}")
    gate = results["search"][0] or results["balances"][0]
    print("\nGATE 0:", "ALIVE (at least one balance source reachable)" if gate else "BLOCKED (no balance source reachable yet)")


if __name__ == "__main__":
    main()
