"""Read-only PayPal SANDBOX client: Transaction Search (all windows, all pages) and Balances.

Never prints or returns secrets. Refuses to run unless PAYPAL_ENV=sandbox.
"""
import base64, json, os, ssl, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = "https://api-m.sandbox.paypal.com"
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem") if os.path.exists("/etc/ssl/cert.pem") else ssl.create_default_context()
WINDOW = timedelta(days=31)  # Transaction Search rejects longer ranges
SECOND = timedelta(seconds=1)  # PayPal times are whole seconds
PAGE_SIZE = 500              # API maximum


class PayPalError(Exception):
    pass


def load_env(path=None):
    env = dict(os.environ)
    path = path or os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.rstrip("\n").split("=", 1)
                    env.setdefault(k, v)
    if env.get("PAYPAL_ENV") != "sandbox":
        raise PayPalError("refusing to run: PAYPAL_ENV must be sandbox")
    return env


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")


def parse_time(s):
    """Accept '2026-10-26T00:29:25Z', '...+0000', '...-0000' or a bare date."""
    s = s.strip()
    if len(s) == 10:
        s += "T00:00:00Z"
    s = s.replace("Z", "+00:00")
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Client:
    def __init__(self, env=None):
        self.env = env or load_env()
        self._token, self._expires = None, 0.0

    def _call(self, method, path, params=None, data=None, basic=False, retries=3):
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(retries):
            req = urllib.request.Request(url, method=method, data=data)
            if basic:
                cred = f"{self.env['PAYPAL_CLIENT_ID']}:{self.env['PAYPAL_CLIENT_SECRET']}"
                req.add_header("Authorization", "Basic " + base64.b64encode(cred.encode()).decode())
            else:
                req.add_header("Authorization", "Bearer " + self.token())
            try:
                with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                body = e.read() or b"{}"
                if e.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                    time.sleep(2 ** attempt)
                    continue
                try:
                    name = json.loads(body).get("name", "")
                except Exception:
                    name = ""
                raise PayPalError(f"{method} {path} -> http {e.code} {name}".strip()) from None
            except urllib.error.URLError as e:
                if attempt + 1 < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise PayPalError(f"{method} {path} -> {e.reason}") from None
        raise PayPalError(f"{method} {path} -> no attempts made")

    def token(self) -> str:
        if not self._token or time.time() > self._expires - 60:
            d = self._call("POST", "/v1/oauth2/token", data=b"grant_type=client_credentials", basic=True)
            self._token, self._expires = d["access_token"], time.time() + d.get("expires_in", 3000)
        return self._token

    def transactions(self, start, end):
        """Every balance-affecting record in [start, end) at one-second resolution: unfiltered (so
        ending_balance is present), split into 31-day windows, every page.

        PayPal's start_date and end_date are both inclusive (probed 2026-10-25), so each window asks
        for [cursor, stop - 1s] and the next starts at stop. Returns (records, last_refreshed,
        expected) where expected is the sum of PayPal's own total_items per window."""
        out, last_refreshed, expected, cursor = [], None, 0, start
        while cursor < end:
            stop = min(cursor + WINDOW, end)
            page = 1
            while True:
                d = self._call("GET", "/v1/reporting/transactions", params={
                    "start_date": iso(cursor), "end_date": iso(stop - SECOND), "fields": "all",
                    "balance_affecting_records_only": "Y", "page_size": PAGE_SIZE, "page": page})
                out.extend(d.get("transaction_details", []))
                if page == 1:
                    expected += int(d.get("total_items") or 0)
                lr = d.get("last_refreshed_datetime")
                if lr:
                    lr = parse_time(lr)
                    last_refreshed = lr if last_refreshed is None else min(last_refreshed, lr)
                if page >= int(d.get("total_pages") or 1):
                    break
                page += 1
            cursor = stop
        return out, last_refreshed, expected

    def balance(self, at, currency):
        """PayPal's own total balance for one currency at an exact moment (Balances API). Includes
        records stamped exactly `at` (probed 2026-10-25)."""
        d = self._call("GET", "/v1/reporting/balances", params={"as_of_time": iso(at), "currency_code": currency})
        # The API may return every currency; pick ours. A currency never held is reported absent.
        for b in d.get("balances", []):
            if b.get("currency") == currency:
                return b["total_balance"]["value"]
        return "0.00"
