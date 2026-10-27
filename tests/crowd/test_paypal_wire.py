"""PayPalHolds against a stubbed network: the request shapes, headers and error mapping. No credentials."""
import io
import json
import urllib.error

import pytest

from crowd import paypal_holds as P


class StubClient:
    env = {"PAYPAL_ENV": "sandbox"}
    _token = None

    def token(self):
        return "tok"


class Response:
    def __init__(self, raw, status):
        self._raw, self.status = raw, status

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Net:
    """Stands in for urllib.request.urlopen. Responses are queued; requests are recorded."""

    def __init__(self, monkeypatch):
        self.sent, self.queue = [], []
        monkeypatch.setattr("urllib.request.urlopen", self._open)
        monkeypatch.setattr("time.sleep", lambda s: None)

    def reply(self, body=None, status=200):
        self.queue.append((status, body))

    def _open(self, req, timeout=None, context=None):
        self.sent.append({"method": req.get_method(), "url": req.full_url,
                          "headers": {k.lower(): v for k, v in req.header_items()},
                          "body": json.loads(req.data) if req.data else None})
        status, body = self.queue.pop(0)
        raw = json.dumps(body).encode() if body is not None else b""
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(raw))
        return Response(raw, status)


@pytest.fixture
def net(monkeypatch):
    return Net(monkeypatch)


@pytest.fixture
def holds():
    return P.PayPalHolds(client=StubClient())


CREATED_ORDER = {"id": "ORD1", "status": "CREATED"}
AUTHORISED = {"id": "ORD1", "status": "COMPLETED", "purchase_units": [{"payments": {"authorizations": [
    {"id": "AUTH1", "status": "CREATED", "expiration_time": "2026-11-25T00:00:00Z"}]}}]}
REFUSED = {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "TRANSACTION_REFUSED"}]}


def test_authorize_creates_an_order_without_a_card_then_authorises_it_with_the_card(holds, net):
    net.reply(CREATED_ORDER, 201)
    net.reply(AUTHORISED, 201)
    out = holds.authorize("40", "USD", "d_1:b1", "crowd-d_1-b1-authorize", card=P.get_card(0))
    create, authz = net.sent
    assert create["method"] == "POST" and create["url"].endswith("/v2/checkout/orders")
    assert create["headers"]["paypal-request-id"] == "crowd-d_1-b1-authorize"
    assert create["headers"]["authorization"] == "Bearer tok"
    assert create["body"]["intent"] == "AUTHORIZE" and "payment_source" not in create["body"]      # no card touched yet
    assert create["body"]["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "40.00"}
    assert authz["url"].endswith("/v2/checkout/orders/ORD1/authorize")
    assert authz["body"]["payment_source"]["card"]["number"] == "4111111111111111"
    assert out == {"order_id": "ORD1", "auth_id": "AUTH1", "status": "CREATED", "amount": "40.00",
                   "expires": "2026-11-25T00:00:00Z"}


def test_a_replayed_join_finds_the_hold_the_first_try_made_instead_of_holding_twice(holds, net):
    """Crash after PayPal authorised but before the engine recorded it: the same request id comes back."""
    net.reply(CREATED_ORDER, 200)         # create replays (200, not 201): same order
    net.reply(AUTHORISED, 200)            # GET order: it already carries the authorisation
    out = holds.authorize("40", "USD", "x", "rid", card=P.get_card(0))
    assert out["auth_id"] == "AUTH1" and out["order_id"] == "ORD1"
    assert [s["method"] + " " + s["url"].rsplit("/v2", 1)[1] for s in net.sent] == \
        ["POST /checkout/orders", "GET /checkout/orders/ORD1"]               # the card was never sent again


def test_a_lost_response_is_not_mistaken_for_a_decline(holds, net):
    """The sandbox answers 422 TRANSACTION_REFUSED to a repeat authorise; read the order before believing it."""
    net.reply(CREATED_ORDER, 201)
    net.reply(REFUSED, 422)               # the retry of an authorise that had in fact worked
    net.reply(AUTHORISED, 200)            # GET order shows the authorisation
    assert holds.authorize("5", "USD", "x", "rid", card=P.get_card(0))["auth_id"] == "AUTH1"


def test_a_real_decline_is_a_decline(holds, net):
    net.reply(CREATED_ORDER, 201)
    net.reply({"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]}, 422)
    net.reply(CREATED_ORDER, 200)         # GET order: still no authorisation
    with pytest.raises(P.CardDeclined):
        holds.authorize("5", "USD", "x", "rid", card=P.get_card(0))


def test_an_order_without_an_authorization_is_a_decline(holds, net):
    net.reply(CREATED_ORDER, 201)
    net.reply({"id": "ORD2", "status": "COMPLETED", "purchase_units": [{"payments": {}}]}, 201)
    with pytest.raises(P.CardDeclined):
        holds.authorize("40", "USD", "x", "r")


def test_capture_is_a_final_capture_and_reads_the_fee(holds, net):
    net.reply({"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "32.00"},
               "final_capture": True, "create_time": "2026-10-27T02:07:00Z",
               "seller_receivable_breakdown": {"gross_amount": {"value": "32.00"}, "paypal_fee": {"value": "1.32"},
                                               "net_amount": {"value": "30.68"}}}, 201)
    out = holds.capture("AUTH1", "32", "crowd-d_1-b1-capture", final=True)
    s = net.sent[0]
    assert s["url"].endswith("/v2/payments/authorizations/AUTH1/capture")
    assert s["body"] == {"amount": {"currency_code": "USD", "value": "32.00"}, "final_capture": True}
    assert s["headers"]["paypal-request-id"] == "crowd-d_1-b1-capture"
    assert "paypal-mock-response" not in s["headers"]
    assert out["fee"] == "1.32" and out["net"] == "30.68" and out["final"] is True and out["capture_id"] == "CAP1"


def test_simulated_decline_uses_the_sandbox_mock_header(holds, net):
    net.reply({"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "TRANSACTION_REFUSED"}]}, 422)
    with pytest.raises(P.CardDeclined):
        holds.capture("AUTH1", "32", "r", simulate_decline=True)
    assert json.loads(net.sent[0]["headers"]["paypal-mock-response"]) == {"mock_application_codes": "TRANSACTION_REFUSED"}


def test_void_and_get(holds, net):
    net.reply(None, 204)
    assert holds.void("AUTH1", "r") == {"auth_id": "AUTH1", "status": "VOIDED"}
    assert net.sent[0]["url"].endswith("/v2/payments/authorizations/AUTH1/void")
    net.reply({"id": "AUTH1", "status": "VOIDED", "amount": {"currency_code": "USD", "value": "40.00"}})
    assert holds.get("AUTH1")["status"] == "VOIDED"


@pytest.mark.parametrize("issue,exc", [
    ("MAX_CAPTURE_AMOUNT_EXCEEDED", P.MaxCaptureAmountExceeded),
    ("AUTHORIZATION_ALREADY_CAPTURED", P.AuthorizationAlreadyCaptured),
    ("AUTHORIZATION_VOIDED", P.AuthorizationVoided),
    ("REAUTHORIZATION_TOO_SOON", P.ReauthorizationTooSoon),
    ("INSTRUMENT_DECLINED", P.CardDeclined),
])
def test_paypal_errors_become_clean_exceptions(holds, net, issue, exc):
    net.reply({"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg", "details": [{"issue": issue}]}, 422)
    with pytest.raises(exc) as e:
        holds.capture("AUTH1", "10", "r")
    assert e.value.issue == issue and e.value.http == 422 and e.value.debug_id == "dbg"


def test_unknown_4xx_errors_are_a_refusal_of_that_request_not_an_outage(holds, net):
    net.reply({"name": "INVALID_REQUEST", "debug_id": "dbg9", "details": [{"issue": "SOMETHING_NEW"}]}, 400)
    with pytest.raises(P.PayPalRefused) as e:
        holds.void("AUTH1", "r")
    assert type(e.value) is P.PayPalRefused and "SOMETHING_NEW" in str(e.value)
    assert (e.value.issue, e.value.http, e.value.debug_id) == ("SOMETHING_NEW", 400, "dbg9")
    assert not isinstance(e.value, P.PayPalUnavailable) and len(net.sent) == 1          # and it is not retried


def test_a_nameless_4xx_is_still_a_refusal(holds, net):
    net.reply(None, 403)
    with pytest.raises(P.PayPalRefused) as e:
        holds.void("AUTH1", "r")
    assert e.value.http == 403 and e.value.issue == "HTTP_403"


def test_repeated_401s_mean_bad_credentials_not_a_refusal_of_one_hold(holds, net):
    for _ in range(3):
        net.reply({"name": "AUTHENTICATION_FAILURE"}, 401)
    with pytest.raises(P.HoldError) as e:
        holds.void("AUTH1", "r")
    assert e.value.code == "paypal_not_configured" and not isinstance(e.value, P.PayPalRefused)


def test_a_reply_that_cannot_be_read_counts_as_a_lost_reply(holds, net, monkeypatch):
    class Junk(Response):
        def read(self):
            return b"<html>gateway</html>"
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Junk(b"", 200))
    with pytest.raises(P.PayPalUnavailable):
        holds.get("AUTH1")


def test_authorize_reports_the_order_as_soon_as_it_exists_and_names_it_on_a_later_failure(holds, net):
    net.reply(CREATED_ORDER, 201)
    net.reply({"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYER_ACTION_REQUIRED"}]}, 422)
    net.reply(CREATED_ORDER, 200)           # the order read after the refusal: still no authorisation
    seen = []
    with pytest.raises(P.PayPalRefused) as e:
        holds.authorize("5", "USD", "x", "rid", card=P.get_card(0), on_order=seen.append)
    assert seen == ["ORD1"] and e.value.order_id == "ORD1"
    assert [s["method"] for s in net.sent] == ["POST", "POST", "GET"]                  # on_order came before the card POST


def test_recover_join_finds_the_hold_by_order_id_or_by_replaying_the_create(holds, net):
    net.reply(AUTHORISED, 200)
    out = holds.recover_join("rid", "40", "USD", "x", order_id="ORD1")
    assert out["auth"] == {"auth_id": "AUTH1", "status": "CREATED", "amount": None, "currency": None,
                           "expires": "2026-11-25T00:00:00Z"} and [s["method"] for s in net.sent] == ["GET"]
    net.sent.clear()
    net.reply(CREATED_ORDER, 200)           # replaying the create: same request id, same order
    net.reply(CREATED_ORDER, 200)           # the order holds nothing
    out = holds.recover_join("rid", "40", "USD", "x")
    assert out == {"order_id": "ORD1", "auth": None} and net.sent[0]["headers"]["paypal-request-id"] == "rid"
    assert "payment_source" not in net.sent[0]["body"]                                  # no card is ever sent by a recovery
    net.sent.clear()
    net.reply({"name": "RESOURCE_NOT_FOUND"}, 404)
    assert holds.recover_join("rid", "40", "USD", "x", order_id="GONE") == {"order_id": "GONE", "auth": None}


def test_order_authorizations_lists_every_hold_on_an_order(holds, net):
    net.reply({"id": "ORD1", "purchase_units": [{"payments": {"authorizations": [
        {"id": "A1", "status": "CREATED", "amount": {"value": "40.00", "currency_code": "USD"}},
        {"id": "A2", "status": "VOIDED", "amount": {"value": "40.00", "currency_code": "USD"}}]}}]})
    assert [(a["auth_id"], a["status"], a["amount"]) for a in holds.order_authorizations("ORD1")] == \
        [("A1", "CREATED", "40.00"), ("A2", "VOIDED", "40.00")]


def test_server_errors_are_retried_then_reported_as_unavailable(holds, net):
    for _ in range(3):
        net.reply({}, 503)
    with pytest.raises(P.PayPalUnavailable):
        holds.void("AUTH1", "r")
    assert len(net.sent) == 3 and len({s["headers"]["paypal-request-id"] for s in net.sent}) == 1   # same id each try


def test_a_retry_after_a_blip_succeeds(holds, net):
    net.reply({}, 502)
    net.reply(CREATED_ORDER, 201)
    net.reply(AUTHORISED, 201)
    assert holds.authorize("5", "USD", "x", "rid", card=P.get_card(1))["auth_id"] == "AUTH1"


def test_refuses_to_run_outside_the_sandbox():
    class Live(StubClient):
        env = {"PAYPAL_ENV": "live"}
    with pytest.raises(P.HoldError) as e:
        P.PayPalHolds(client=Live())
    assert e.value.code == "not_sandbox"
