"""PayPal holds: authorise at join, capture (final) or void at settlement. SANDBOX only.

`PayPalHolds` is a thin client over closure.paypal.Client (token, base URL, CA context). `FakePayPal`
(CROWD_PAYPAL=fake) mirrors what the sandbox did in docs/DECISIONS.md F-08, so tests and the Togi stone
run fast and free:

    authorise                       -> CREATED, expires in 29 days
    capture <= amount, final        -> COMPLETED, fee = 2.59% + 0.49 (half-up cents), auth CAPTURED
    capture > amount                -> MAX_CAPTURE_AMOUNT_EXCEEDED
    second capture                  -> AUTHORIZATION_ALREADY_CAPTURED
    void                            -> VOIDED;  capture after void -> AUTHORIZATION_VOIDED
    decline (decline_next / card 9) -> declined at capture, the hold stays open

Every call carries a deterministic PayPal-Request-Id, so a retry never doubles money; the fake keeps the
same idempotency (same request id, same answer).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import ladder as lad

DECLINE_CARD_INDEX = 9      # decline at capture: flow E (a charge fails at the end)
# The sandbox's negative-testing code that makes a capture fail with 422 TRANSACTION_REFUSED and leaves the hold open
# (probed 2026-10-27: INSTRUMENT_DECLINED is not accepted for captures, it returns an empty 403).
MOCK_CAPTURE_DECLINE = "TRANSACTION_REFUSED"
HOLD_DAYS = 29              # observed in the sandbox (F-08)

# Sandbox test cards (all but index 9 placed a hold in the sandbox on 2026-10-27; 5105105105105100 was refused as
# invalid and is not used). Index 9 is an ordinary card whose capture is made to fail (PayPal-Mock-Response in the
# sandbox, a modelled decline in the fake). Never a real card.
_CARD_NUMBERS = ["4111111111111111", "4012888888881881", "5555555555554444", "5454545454545454",
                 "6011111111111117", "6011000990139424", "3530111333300000", "3566002020360505",
                 "4111111111111111", "4111111111111111"]
TEST_CARDS = [{"number": n, "expiry": "2030-12", "security_code": "123", "name": "Test Buyer"} for n in _CARD_NUMBERS]


def get_card(i: int) -> dict:
    if not isinstance(i, int) or not 0 <= i < len(TEST_CARDS):
        raise HoldError("bad_card", f"card index must be 0..{len(TEST_CARDS) - 1}")
    return dict(TEST_CARDS[i])


def request_id_for(drop: str, buyer: str, action: str, attempt: int = 1) -> str:
    """crowd-<drop>-<buyer>-<action>, with the buyer's attempt number from the 2nd attempt on, so a
    buyer who leaves and rejoins never reuses an earlier request's answer."""
    return f"crowd-{drop}-{buyer}-{action}" + (str(attempt) if attempt > 1 else "")


# ---- errors ------------------------------------------------------------------------------------

class HoldError(Exception):
    """A PayPal refusal or failure, as a clean exception. `code` is a stable slug. `order_id` is set when the
    failure happened after PayPal had already created the order (so the caller can record it)."""
    code = "paypal_error"

    def __init__(self, code: str | None = None, message: str = "", issue: str = "", http: int | None = None,
                 debug_id: str | None = None, issues: list[str] | None = None):
        super().__init__(message or code or self.code)
        self.code = code or self.code
        self.message, self.issue, self.http, self.debug_id = message, issue, http, debug_id
        # every error name PayPal gave for this refusal (details[].issue, else the top-level name): the classification
        # below needs all of them, because one unfamiliar name beside a familiar one makes it unfamiliar
        self.issues: list[str] = list(issues) if issues is not None else ([issue] if issue else [])
        self.order_id: str | None = None


class PayPalRefused(HoldError):
    """PayPal read the request and said no: a non-transient 4xx. Retrying the same call will not help, so settlement
    records it against that one buyer and carries on with the next (it never stops the whole settlement)."""
    code = "paypal_refused"


class MaxCaptureAmountExceeded(PayPalRefused):
    code = "max_capture_amount_exceeded"


class AuthorizationAlreadyCaptured(PayPalRefused):
    code = "authorization_already_captured"


class AuthorizationVoided(PayPalRefused):
    code = "authorization_voided"


class AuthorizationExpired(PayPalRefused):
    code = "authorization_expired"


class AuthorizationDenied(PayPalRefused):
    code = "authorization_denied"


class PayerActionRequired(PayPalRefused):
    code = "payer_action_required"


class UnprocessableEntity(PayPalRefused):
    code = "unprocessable_entity"


class ReauthorizationTooSoon(PayPalRefused):
    code = "reauthorization_too_soon"


class CardDeclined(PayPalRefused):
    code = "card_declined"


class HoldNotFound(PayPalRefused):
    code = "hold_not_found"


class PayPalUnavailable(HoldError):
    """Network trouble, a timeout, 408, 429 or 5xx after retries. Safe to retry: calls are idempotent. This is the
    only kind of failure that stops a settlement part-way (so it can resume)."""
    code = "paypal_unavailable"


_ISSUES = {
    "MAX_CAPTURE_AMOUNT_EXCEEDED": MaxCaptureAmountExceeded,
    "AUTHORIZATION_ALREADY_CAPTURED": AuthorizationAlreadyCaptured,
    "PREVIOUSLY_CAPTURED": AuthorizationAlreadyCaptured,
    "AUTHORIZATION_VOIDED": AuthorizationVoided,
    "PREVIOUSLY_VOIDED": AuthorizationVoided,
    "AUTHORIZATION_EXPIRED": AuthorizationExpired,
    "AUTHORIZATION_DENIED": AuthorizationDenied,
    "PAYER_ACTION_REQUIRED": PayerActionRequired,
    "REAUTHORIZATION_TOO_SOON": ReauthorizationTooSoon,
    "INSTRUMENT_DECLINED": CardDeclined,
    "TRANSACTION_REFUSED": CardDeclined,
    "CREDIT_CARD_REFUSED": CardDeclined,
    "CARD_EXPIRED": CardDeclined,
    "INVALID_CARD_NUMBER": CardDeclined,
    "INVALID_SECURITY_CODE": CardDeclined,
    "INSUFFICIENT_FUNDS": CardDeclined,
    "PAYER_CANNOT_PAY": CardDeclined,
    "RESOURCE_NOT_FOUND": HoldNotFound,
    "INVALID_RESOURCE_ID": HoldNotFound,
    "UNPROCESSABLE_ENTITY": UnprocessableEntity,
}
TRANSIENT_HTTP = (408, 429, 500, 502, 503, 504)

# Refusals that are about ONE buyer's hold or card (docs/crowd-fixes-r2.md F1). Only these may be recorded against that
# buyer (capture_failed / void_failed) while settlement goes on to the next. Anything else, whatever its name, an unknown
# name, an empty body, any 401 or 403, may be about the merchant account or the request itself, so it would hit every buyer
# alike: settlement stops and a person looks at it. An allow-list, not a deny-list: a name nobody has seen is never trusted.
BUYER_SPECIFIC_ISSUES = frozenset({
    "INSTRUMENT_DECLINED", "TRANSACTION_REFUSED", "CREDIT_CARD_REFUSED", "CARD_EXPIRED", "INVALID_CARD_NUMBER",
    "INVALID_SECURITY_CODE", "INSUFFICIENT_FUNDS", "PAYER_CANNOT_PAY",          # the card or instrument said no
    "PAYER_ACTION_REQUIRED", "AUTHORIZATION_EXPIRED", "AUTHORIZATION_VOIDED", "AUTHORIZATION_DENIED",
    "MAX_CAPTURE_AMOUNT_EXCEEDED"})                                              # this one authorisation, not the account
ACCOUNT_LEVEL_HTTP = (401, 403)


def buyer_specific(e: "HoldError") -> bool:
    """Is this refusal about one buyer's hold (safe to record against them and carry on)? Judged by the names PayPal gave,
    never by the exception class: every name must be on the allow-list, and a 401 or 403 is never buyer-specific."""
    if getattr(e, "http", None) in ACCOUNT_LEVEL_HTTP:
        return False
    issues = getattr(e, "issues", None) or []
    return bool(issues) and all(i in BUYER_SPECIFIC_ISSUES for i in issues)


def _error_from(http: int, body: dict) -> HoldError:
    """PayPal's answer to a failed call as the most specific exception. Any 4xx that is not transient is a PayPalRefused,
    named or not: an unrecognised refusal must never be mistaken for an outage."""
    issues = [str(d.get("issue", "")) for d in (body.get("details") or []) if isinstance(d, dict)]
    name = str(body.get("name", ""))
    debug_id = body.get("debug_id")
    for issue in issues + [name]:
        cls = _ISSUES.get(issue)
        if cls:
            # matched on the generic name (UNPROCESSABLE_ENTITY): keep the specific issue PayPal gave as the evidence
            named = issue if issue in issues else (issues[0] if issues else issue)
            return cls(message=f"{named or name}", issue=named or name, http=http, debug_id=debug_id,
                       issues=_names(issues, name))
    label = issues[0] if issues else name or f"HTTP_{http}"
    if http in TRANSIENT_HTTP:
        return PayPalUnavailable(message=f"PayPal http {http}", http=http, debug_id=debug_id)
    if 400 <= http < 500:
        return PayPalRefused(message=f"PayPal said {label} (http {http})", issue=label, http=http, debug_id=debug_id,
                             issues=_names(issues, name))
    return HoldError("paypal_error", f"PayPal said {label} (http {http})", issue=label, http=http, debug_id=debug_id,
                     issues=_names(issues, name))


def _names(issues: list[str], name: str) -> list[str]:
    """The specific names PayPal gave: the detail issues, or the top-level name when it gave no detail."""
    return [i for i in issues if i] or ([name] if name else [])


# ---- the real sandbox client ---------------------------------------------------------------------

class PayPalHolds:
    """Orders v2 (intent AUTHORIZE, card source) and Payments v2 (capture, void, get) in the sandbox.

    Credentials come only from the repo's .env or the process environment (closure.paypal.load_env with its default
    path). Nothing here, and nothing a command can pass in, names another file to read them from."""
    kind = "sandbox"
    ref = None

    def __init__(self, client=None, retries: int = 3):
        from closure import paypal as cp  # token, BASE, CTX live there; load_env refuses non-sandbox
        self._cp = cp
        try:
            self.client = client or cp.Client()
        except cp.PayPalError as e:
            raise HoldError("paypal_not_configured", str(e)) from None
        if (self.client.env or {}).get("PAYPAL_ENV") != "sandbox" or "sandbox" not in cp.BASE:
            raise HoldError("not_sandbox", "refusing to run: PAYPAL_ENV must be sandbox")
        self.retries = retries

    # -- transport
    def _token(self) -> str:
        try:
            return self.client.token()
        except KeyError:
            raise HoldError("paypal_not_configured", "sandbox credentials are missing from .env") from None
        except self._cp.PayPalError as e:
            if "http 4" in str(e):
                raise HoldError("paypal_not_configured", f"PayPal refused the sandbox credentials ({e})") from None
            raise PayPalUnavailable(message=f"could not get a PayPal token: {e}") from None

    def _request(self, method: str, path: str, body: dict | None = None, rid: str | None = None,
                 headers: dict | None = None, want_status: bool = False):
        cp = self._cp
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(cp.BASE + path, data=data, method=method)
            req.add_header("Authorization", "Bearer " + self._token())
            req.add_header("Accept", "application/json")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if rid:
                req.add_header("PayPal-Request-Id", rid)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=60, context=cp.CTX) as r:
                    raw = r.read()
                    try:
                        out = json.loads(raw) if raw else {}
                    except ValueError:  # PayPal acted (2xx) but the answer is unreadable: same as a lost reply
                        raise PayPalUnavailable(message="PayPal answered with something unreadable") from None
                    return (r.status, out) if want_status else out
            except urllib.error.HTTPError as e:
                raw = e.read() or b"{}"
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = {}
                if e.code == 401:
                    if attempt + 1 < self.retries:
                        self.client._token = None  # token expired mid-run: fetch a new one
                        continue
                    # not a refusal of this one request: the credentials are wrong, for every buyer alike
                    raise HoldError("paypal_not_configured", "PayPal refused the sandbox credentials (http 401)",
                                    http=401) from None
                if e.code in TRANSIENT_HTTP:
                    last = PayPalUnavailable(message=f"PayPal http {e.code}", http=e.code)
                    if attempt + 1 < self.retries:
                        time.sleep(2 ** attempt)
                        continue
                    raise last from None
                raise _error_from(e.code, parsed if isinstance(parsed, dict) else {}) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = PayPalUnavailable(message=f"could not reach PayPal: {getattr(e, 'reason', e)}")
                if attempt + 1 < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise last from None
        raise last or PayPalUnavailable(message="no attempt made")

    # -- the four calls
    @staticmethod
    def _order_body(value: str, currency: str, label: str) -> dict:
        return {"intent": "AUTHORIZE",
                "purchase_units": [{"reference_id": str(label)[:120], "custom_id": str(label)[:120],
                                    "amount": {"currency_code": currency, "value": value}}]}

    def authorize(self, amount, currency: str, label: str, request_id: str, card: dict | None = None,
                  on_order=None) -> dict:
        """Hold `amount` on a test card. Returns {order_id, auth_id, status, expires, amount}.

        Two steps, because the sandbox does not replay a card order: POSTing the same PayPal-Request-Id again to
        /v2/checkout/orders with a card answers 422 TRANSACTION_REFUSED, so a lost response would look like a decline
        and leave an unknown hold on the buyer's card (probed 2026-10-27). Instead: (1) create the order with no card,
        which IS idempotent by request id (same id, same order); (2) read the order, and if it already carries an
        authorisation return that; (3) otherwise authorise it with the card. After any doubt, read the order again.

        `on_order(order_id)` is called as soon as the order exists, before the card is touched, so the caller can write
        down which order a hold may appear on. A failure after that carries the order id on the exception."""
        value = f"{lad.money(amount):.2f}"
        status, order = self._request("POST", "/v2/checkout/orders", rid=request_id, want_status=True,
                                      headers={"Prefer": "return=representation"},
                                      body=self._order_body(value, currency, label))
        order_id = order.get("id")
        if not order_id:
            raise HoldError("paypal_error", "PayPal returned no order id", http=status)
        replayed = status != 201      # an earlier attempt made this order: whatever it shows is a past answer, not news
        try:
            if on_order is not None:
                on_order(order_id)
            auth = _authorization_of(order)
            if auth is None and status != 201:  # an earlier attempt created this order: has it been authorised?
                auth = _authorization_of(self.get_order(order_id))
            if auth is None:
                try:
                    authorised = self._request("POST", f"/v2/checkout/orders/{order_id}/authorize",
                                               rid=request_id + "-authorize", headers={"Prefer": "return=representation"},
                                               body={"payment_source": {"card": card if card is not None else dict(TEST_CARDS[0])}})
                    auth = _authorization_of(authorised)
                except (PayPalRefused, PayPalUnavailable):
                    auth = _authorization_of(self.get_order(order_id))  # an earlier try may have gone through
                    if auth is None:
                        raise
                    replayed = True      # found by reading the order after a failed call: confirm it before trusting it
            # No authorisation at all, or PayPal's explicit DENIED, is a card that was refused: nothing is held. Any other
            # status is returned as it is. The caller asks PayPal about it and decides; this client does not guess.
            if auth is None or auth.get("status") == "DENIED":
                raise CardDeclined(message=f"authorisation not granted: hold {(auth or {}).get('status') or 'none'}",
                                   issue=(auth or {}).get("status") or "NO_AUTHORIZATION")
        except HoldError as e:
            e.order_id = e.order_id or order_id
            raise
        return {"order_id": order_id, "auth_id": auth["id"], "status": auth.get("status"), "amount": value,
                "expires": auth.get("expiration_time"), "replayed": replayed}

    def recover_join(self, request_id: str, amount, currency: str, label: str, order_id: str | None = None) -> dict:
        """What a join whose answer was lost left at PayPal: {order_id, auth} where auth is the hold on the order, or
        None if there is none. Finds the order by its recorded id, or by replaying the idempotent create (same request
        id: same order; if the first create never arrived this makes an empty order, which holds nothing)."""
        if order_id is None:
            value = f"{lad.money(amount):.2f}"
            _, order = self._request("POST", "/v2/checkout/orders", rid=request_id, want_status=True,
                                     headers={"Prefer": "return=representation"},
                                     body=self._order_body(value, currency, label))
            order_id = order.get("id")
            if not order_id:
                raise HoldError("paypal_error", "PayPal returned no order id")
        try:
            order = self.get_order(order_id)
        except HoldNotFound:
            return {"order_id": order_id, "auth": None}
        auth = _authorization_of(order)
        return {"order_id": order_id, "auth": _auth_view(auth) if auth else None}

    def order_authorizations(self, order_id: str) -> list[dict]:
        """Every authorisation PayPal has on this order, as {auth_id, status, amount, currency, expires}."""
        order = self.get_order(order_id)
        out = []
        for pu in order.get("purchase_units") or []:
            for a in ((pu.get("payments") or {}).get("authorizations") or []):
                out.append(_auth_view(a))
        return out

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture(self, auth_id: str, amount, request_id: str, final: bool = True, currency: str = "USD",
                simulate_decline: bool = False) -> dict:
        """Capture `amount` of a hold. Returns {capture_id, status, amount, fee, net, final}."""
        value = f"{lad.money(amount):.2f}"
        headers = {"Prefer": "return=representation"}
        if simulate_decline:  # sandbox negative testing: this capture is refused, the hold stays open
            headers["PayPal-Mock-Response"] = json.dumps({"mock_application_codes": MOCK_CAPTURE_DECLINE})
        c = self._request("POST", f"/v2/payments/authorizations/{auth_id}/capture", rid=request_id, headers=headers,
                          body={"amount": {"currency_code": currency, "value": value}, "final_capture": bool(final)})
        return _capture_view(c)

    def void(self, auth_id: str, request_id: str) -> dict:
        self._request("POST", f"/v2/payments/authorizations/{auth_id}/void", rid=request_id, headers={})
        return {"auth_id": auth_id, "status": "VOIDED"}

    def get(self, auth_id: str) -> dict:
        a = self._request("GET", f"/v2/payments/authorizations/{auth_id}")
        amt = a.get("amount") or {}
        return {"auth_id": a.get("id", auth_id), "status": a.get("status"), "amount": amt.get("value"),
                "currency": amt.get("currency_code"), "expires": a.get("expiration_time")}

    def get_capture(self, capture_id: str) -> dict:
        return _capture_view(self._request("GET", f"/v2/payments/captures/{capture_id}"))

    def recover_capture(self, order_id: str) -> dict | None:
        """The capture made against an order's hold (used when a capture succeeded but was never recorded)."""
        o = self._request("GET", f"/v2/checkout/orders/{order_id}")
        caps = (((o.get("purchase_units") or [{}])[0].get("payments") or {}).get("captures") or [])
        return self.get_capture(caps[0]["id"]) if caps else None


def _auth_view(a: dict) -> dict:
    amt = a.get("amount") or {}
    return {"auth_id": a.get("id"), "status": a.get("status"), "amount": amt.get("value"),
            "currency": amt.get("currency_code"), "expires": a.get("expiration_time")}


def _authorization_of(order: dict) -> dict | None:
    pay = ((order.get("purchase_units") or [{}])[0].get("payments") or {})
    auths = pay.get("authorizations") or []
    return auths[0] if auths else None


def _parent_auth_id(c: dict) -> str | None:
    """The authorisation a capture belongs to, as PayPal says it: related_ids.authorization_id, or an `up` link that points
    at an authorisation. None when PayPal does not say (an `up` link to an order is not an answer)."""
    rel = ((c.get("supplementary_data") or {}).get("related_ids") or {}).get("authorization_id")
    if rel:
        return str(rel)
    for link in c.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "up" and "/authorizations/" in str(link.get("href", "")):
            return str(link["href"]).rstrip("/").rsplit("/", 1)[-1]
    return None


def _capture_view(c: dict) -> dict:
    amt = c.get("amount") or {}
    br = c.get("seller_receivable_breakdown") or {}
    fee = (br.get("paypal_fee") or {}).get("value")
    net = (br.get("net_amount") or {}).get("value")
    return {"capture_id": c.get("id"), "status": c.get("status"), "amount": amt.get("value"),
            "currency": amt.get("currency_code"), "fee": fee, "net": net, "final": c.get("final_capture"),
            "created": c.get("create_time"), "auth_id": _parent_auth_id(c)}


# ---- the fake ----------------------------------------------------------------------------------

class FakePayPal:
    """In-memory PayPal holds persisted to a JSON file, so separate CLI processes share it.

    Like the real thing, a join is two steps (create an order, then authorise it), so a test can crash between them.
    Failures for tests are armed with `refuse`, `unavailable`, `lose_reply` and `expire` below; they live in the state
    file too, so they reach engines running in other processes."""
    kind = "fake"

    def __init__(self, path: str, fee_model: lad.FeeModel = lad.DEFAULT_FEE):
        self.path, self.fee_model = os.path.abspath(path), fee_model
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    @property
    def ref(self) -> str:
        """Which fake this is (a hash of its state file's path). Holds record it, so a store whose holds live in some
        other PayPal, even another fake, is never mistaken for this one."""
        return hashlib.sha1(self.path.encode()).hexdigest()[:12]

    @contextlib.contextmanager
    def _state(self):
        fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                with open(self.path) as f:
                    st = json.load(f)
            except (FileNotFoundError, ValueError):
                st = {}
            for k, v in (("auths", {}), ("captures", {}), ("requests", {}), ("orders", {}), ("order_rids", {}),
                         ("faults", []), ("decline_next", 0), ("decline_auth_next", 0)):
                st.setdefault(k, v)
            try:
                yield st
            finally:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(st, f)
                os.replace(tmp, self.path)
        finally:
            os.close(fd)

    # -- test controls
    def decline_next(self, n: int = 1) -> None:
        """The next n captures are declined (the hold stays open), like an expired card at the end."""
        with self._state() as st:
            st["decline_next"] = n

    def decline_next_authorize(self, n: int = 1) -> None:
        with self._state() as st:
            st["decline_auth_next"] = n

    def refuse(self, op: str, issue: str, http: int = 422, match: str | None = None, times: int | None = 1) -> None:
        """PayPal refuses the next `times` calls of `op` ('authorize' | 'capture' | 'void') with this issue name (for
        example AUTHORIZATION_EXPIRED) and does nothing. `match` limits it to a hold or label containing that text;
        times=None refuses every matching call."""
        self._arm({"op": op, "kind": "refuse", "issue": issue, "http": http}, match, times)

    def unavailable(self, op: str, match: str | None = None, times: int | None = 1) -> None:
        """The next `times` calls of `op` fail with a transient outage before PayPal does anything."""
        self._arm({"op": op, "kind": "unavailable"}, match, times)

    def lose_reply(self, op: str, match: str | None = None, times: int | None = 1) -> None:
        """The next `times` calls of `op` go through at PayPal, but the answer never arrives (the caller sees a
        transient outage). The worst case for money: PayPal acted and nobody wrote it down."""
        self._arm({"op": op, "kind": "lose_reply"}, match, times)

    def expire(self, auth_id: str) -> None:
        """PayPal lets this hold lapse: status EXPIRED, which can be neither captured nor voided."""
        with self._state() as st:
            st["auths"][auth_id]["status"] = "EXPIRED"

    def _arm(self, fault: dict, match: str | None = None, times: int | None = 1) -> None:
        with self._state() as st:
            st["faults"].append({**fault, "match": match, "times": times})

    def _take(self, st: dict, op: str, key: str, kinds: tuple) -> dict | None:
        for f in st["faults"]:
            if f["op"] == op and f["kind"] in kinds and (not f.get("match") or f["match"] in key) \
                    and (f["times"] is None or f["times"] > 0):
                if f["times"] is not None:
                    f["times"] -= 1
                return f
        return None

    def _before(self, op: str, key: str) -> None:
        with self._state() as st:
            f = self._take(st, op, key, ("refuse", "unavailable"))
        if f and f["kind"] == "unavailable":
            raise PayPalUnavailable(message="simulated outage")
        if f:
            raise _error_from(f.get("http", 422), {"name": "UNPROCESSABLE_ENTITY", "debug_id": self._debug_id(),
                                                   "details": [{"issue": f["issue"]}]})

    def _after(self, op: str, key: str) -> None:
        with self._state() as st:
            f = self._take(st, op, key, ("lose_reply",))
        if f:
            raise PayPalUnavailable(message="simulated lost reply: PayPal acted, the answer never arrived")

    @staticmethod
    def _debug_id() -> str:
        return "FAKEDBG" + secrets.token_hex(5).upper()

    def _deny(self, cls, issue: str, http: int = 422):
        return cls(message=issue, issue=issue, http=http, debug_id=self._debug_id())

    def reset(self) -> None:
        for suffix in ("", ".lock", ".tmp"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path + suffix)

    # -- the calls
    def authorize(self, amount, currency: str, label: str, request_id: str, card: dict | None = None,
                  on_order=None) -> dict:
        self._before("authorize", label)
        out = self._authorize(amount, currency, label, request_id, on_order)
        self._after("authorize", label)
        return out

    def _authorize(self, amount, currency, label, request_id, on_order) -> dict:
        value = lad.money(amount)
        with self._state() as st:
            if request_id in st["requests"]:
                # a replay answers with the hold as it is NOW (it may have been voided or lapsed since), never the first
                # answer frozen: a cached CREATED on a dead hold would be counted toward the crowd
                resp = dict(st["requests"][request_id])
                a = st["auths"].get(resp.get("auth_id"))
                if a is not None:
                    resp.update(status=a["status"], amount=a["amount"], expires=a["expires"])
                resp["replayed"] = True
                return resp
            if value <= 0:
                raise self._deny(HoldError, "INVALID_PARAMETER_VALUE")
            order_id = st["order_rids"].get(request_id)         # step 1: the order, idempotent by request id
            if order_id is None:
                order_id = "FKO" + secrets.token_hex(7).upper()
                st["orders"][order_id] = {"rid": request_id, "label": label, "amount": f"{value:.2f}",
                                          "currency": currency}
                st["order_rids"][request_id] = order_id
        try:
            if on_order is not None:
                on_order(order_id)
            with self._state() as st:                           # step 2: the card
                if st["decline_auth_next"] > 0:
                    st["decline_auth_next"] -= 1
                    raise self._deny(CardDeclined, "INSTRUMENT_DECLINED")
                auth_id = "FKA" + secrets.token_hex(7).upper()
                expires = (datetime.now(timezone.utc) + timedelta(days=HOLD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
                st["auths"][auth_id] = {"order_id": order_id, "status": "CREATED", "amount": f"{value:.2f}",
                                        "currency": currency, "expires": expires, "label": label, "capture_id": None}
                resp = {"order_id": order_id, "auth_id": auth_id, "status": "CREATED", "amount": f"{value:.2f}",
                        "expires": expires}
                st["requests"][request_id] = resp
                return dict(resp)
        except HoldError as e:
            e.order_id = e.order_id or order_id
            raise

    def capture(self, auth_id: str, amount, request_id: str, final: bool = True, currency: str = "USD",
                simulate_decline: bool = False) -> dict:
        self._before("capture", auth_id)
        out = self._capture(auth_id, amount, request_id, final, currency, simulate_decline)
        self._after("capture", auth_id)
        return out

    def _capture(self, auth_id, amount, request_id, final, currency, simulate_decline) -> dict:
        value = lad.money(amount)
        with self._state() as st:
            if request_id in st["requests"]:
                return dict(st["requests"][request_id])
            a = st["auths"].get(auth_id)
            if a is None:
                raise self._deny(HoldNotFound, "RESOURCE_NOT_FOUND", 404)
            if a["status"] == "VOIDED":
                raise self._deny(AuthorizationVoided, "AUTHORIZATION_VOIDED")
            if a["status"] == "EXPIRED":
                raise self._deny(AuthorizationExpired, "AUTHORIZATION_EXPIRED")
            if a["status"] == "CAPTURED":
                raise self._deny(AuthorizationAlreadyCaptured, "AUTHORIZATION_ALREADY_CAPTURED")
            if value <= 0 or value > Decimal(a["amount"]):
                raise self._deny(MaxCaptureAmountExceeded, "MAX_CAPTURE_AMOUNT_EXCEEDED")
            if st["decline_next"] > 0 or simulate_decline:
                if st["decline_next"] > 0:
                    st["decline_next"] -= 1
                raise self._deny(CardDeclined, "TRANSACTION_REFUSED")
            f = lad.fee(value, self.fee_model)
            cap_id = "FKC" + secrets.token_hex(7).upper()
            rec = {"capture_id": cap_id, "status": "COMPLETED", "amount": f"{value:.2f}", "currency": currency,
                   "fee": f"{f:.2f}", "net": f"{value - f:.2f}", "final": bool(final),
                   "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "auth_id": auth_id}
            st["captures"][cap_id] = rec
            a["status"] = "CAPTURED" if final else "PARTIALLY_CAPTURED"
            a["capture_id"] = cap_id
            resp = dict(rec)
            st["requests"][request_id] = resp
            return dict(resp)

    def void(self, auth_id: str, request_id: str) -> dict:
        self._before("void", auth_id)
        out = self._void(auth_id, request_id)
        self._after("void", auth_id)
        return out

    def _void(self, auth_id, request_id) -> dict:
        with self._state() as st:
            a = st["auths"].get(auth_id)
            if a is None:
                raise self._deny(HoldNotFound, "RESOURCE_NOT_FOUND", 404)
            if a["status"] == "VOIDED":
                raise self._deny(AuthorizationVoided, "AUTHORIZATION_VOIDED")
            if a["status"] == "EXPIRED":
                raise self._deny(AuthorizationExpired, "AUTHORIZATION_EXPIRED")
            if a["status"] in ("CAPTURED", "PARTIALLY_CAPTURED"):
                raise self._deny(AuthorizationAlreadyCaptured, "AUTHORIZATION_ALREADY_CAPTURED")
            a["status"] = "VOIDED"
            return {"auth_id": auth_id, "status": "VOIDED"}

    def get(self, auth_id: str) -> dict:
        with self._state() as st:
            a = st["auths"].get(auth_id)
            if a is None:
                raise self._deny(HoldNotFound, "RESOURCE_NOT_FOUND", 404)
            return {"auth_id": auth_id, "status": a["status"], "amount": a["amount"], "currency": a["currency"],
                    "expires": a["expires"]}

    def get_capture(self, capture_id: str) -> dict:
        with self._state() as st:
            c = st["captures"].get(capture_id)
            if c is None:
                raise self._deny(HoldNotFound, "RESOURCE_NOT_FOUND", 404)
            return dict(c)

    def recover_capture(self, order_id: str) -> dict | None:
        with self._state() as st:
            for a in st["auths"].values():
                if a["order_id"] == order_id and a["capture_id"]:
                    return dict(st["captures"][a["capture_id"]])
        return None

    def recover_join(self, request_id: str, amount, currency: str, label: str, order_id: str | None = None) -> dict:
        with self._state() as st:
            oid = order_id or st["order_rids"].get(request_id)
            if oid is None:
                return {"order_id": None, "auth": None}
            for aid, a in st["auths"].items():
                if a["order_id"] == oid:
                    return {"order_id": oid, "auth": {"auth_id": aid, "status": a["status"], "amount": a["amount"],
                                                      "currency": a["currency"], "expires": a["expires"]}}
            return {"order_id": oid, "auth": None}

    def order_authorizations(self, order_id: str) -> list[dict]:
        with self._state() as st:
            out = [{"auth_id": aid, "status": a["status"], "amount": a["amount"], "currency": a["currency"],
                    "expires": a["expires"]} for aid, a in st["auths"].items() if a["order_id"] == order_id]
            if not out and order_id not in st["orders"]:
                raise self._deny(HoldNotFound, "RESOURCE_NOT_FOUND", 404)
            return out


def mode_of(holds) -> str:
    """'fake' | 'sandbox' | 'unknown' for a holds backend. A wrapper that cannot say (its attributes are not strings) is
    'unknown', never a lie: events recorded under 'unknown' are not trusted by the test tooling."""
    kind = getattr(holds, "kind", None)
    return kind if isinstance(kind, str) and kind else "unknown"


def ref_of(holds) -> str | None:
    ref = getattr(holds, "ref", None)
    return ref if isinstance(ref, str) and ref else None


def make_holds(store_path: str | None = None):
    """The holds backend for this process: CROWD_PAYPAL=fake -> FakePayPal next to the store, else the sandbox."""
    mode = os.environ.get("CROWD_PAYPAL", "sandbox").strip().lower()
    if mode == "fake":
        from .store import default_path
        return FakePayPal((store_path or default_path()) + ".fakepaypal.json")
    if mode in ("sandbox", "live", ""):
        return PayPalHolds()
    raise HoldError("bad_mode", f"CROWD_PAYPAL must be 'fake' or 'sandbox', not {mode!r}")
