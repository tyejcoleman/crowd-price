"""The fake must behave like the sandbox did in DECISIONS.md F-08."""
import pytest

from crowd import paypal_holds as P


def hold(fp, amount="40.00", rid="r-auth"):
    return fp.authorize(amount, "USD", "lbl", rid, card=P.get_card(0))


def test_authorize_creates_a_hold_that_expires_in_29_days(fake):
    h = hold(fake)
    assert h["status"] == "CREATED" and h["auth_id"].startswith("FKA") and h["amount"] == "40.00"
    assert fake.get(h["auth_id"])["status"] == "CREATED"
    from datetime import datetime, timezone
    days = (datetime.strptime(h["expires"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
    assert 28 <= days <= 29


def test_partial_final_capture_completes_with_the_observed_fee(fake):
    h = hold(fake)
    c = fake.capture(h["auth_id"], "32.00", "r-cap", final=True)
    assert c["status"] == "COMPLETED" and c["amount"] == "32.00" and c["fee"] == "1.32" and c["net"] == "30.68"
    assert c["final"] is True
    assert fake.get(h["auth_id"])["status"] == "CAPTURED"
    assert fake.get_capture(c["capture_id"])["amount"] == "32.00"


def test_over_capture_is_refused(fake):
    h = hold(fake)
    with pytest.raises(P.MaxCaptureAmountExceeded):
        fake.capture(h["auth_id"], "60.00", "r1")
    assert fake.get(h["auth_id"])["status"] == "CREATED"              # nothing moved


def test_second_capture_is_refused(fake):
    h = hold(fake)
    fake.capture(h["auth_id"], "32.00", "r1")
    with pytest.raises(P.AuthorizationAlreadyCaptured):
        fake.capture(h["auth_id"], "32.00", "r2")


def test_void_then_capture_is_refused(fake):
    h = hold(fake)
    assert fake.void(h["auth_id"], "r-void")["status"] == "VOIDED"
    assert fake.get(h["auth_id"])["status"] == "VOIDED"
    with pytest.raises(P.AuthorizationVoided):
        fake.capture(h["auth_id"], "10.00", "r2")
    with pytest.raises(P.AuthorizationVoided):
        fake.void(h["auth_id"], "r-void-2")


def test_void_after_capture_is_refused(fake):
    h = hold(fake)
    fake.capture(h["auth_id"], "32.00", "r1")
    with pytest.raises(P.AuthorizationAlreadyCaptured):
        fake.void(h["auth_id"], "r2")


def test_request_ids_make_retries_safe(fake):
    a = hold(fake, rid="same")
    b = hold(fake, rid="same")
    assert {**b} == {**a, "replayed": True}                           # one hold, not two; the second is marked as a replay
    c1 = fake.capture(a["auth_id"], "32.00", "cap-same")
    c2 = fake.capture(a["auth_id"], "32.00", "cap-same")              # a retry returns the same capture
    assert c1 == c2


def test_decline_next_declines_at_capture_and_keeps_the_hold(fake):
    h = hold(fake)
    fake.decline_next(1)
    with pytest.raises(P.CardDeclined):
        fake.capture(h["auth_id"], "32.00", "r1")
    assert fake.get(h["auth_id"])["status"] == "CREATED"
    assert fake.capture(h["auth_id"], "32.00", "r2")["status"] == "COMPLETED"   # only the next one declined


def test_simulate_decline_is_what_card_nine_does(fake):
    h = hold(fake)
    with pytest.raises(P.CardDeclined):
        fake.capture(h["auth_id"], "32.00", "r1", simulate_decline=True)


def test_authorize_decline(fake):
    fake.decline_next_authorize(1)
    with pytest.raises(P.CardDeclined):
        hold(fake)
    assert hold(fake, rid="r2")["status"] == "CREATED"


def test_unknown_hold(fake):
    with pytest.raises(P.HoldNotFound):
        fake.get("FKAnope")
    with pytest.raises(P.HoldNotFound):
        fake.capture("FKAnope", "1.00", "r")


def test_state_is_shared_between_instances_like_separate_cli_processes(fake, tmp_path):
    h = hold(fake)
    other = P.FakePayPal(fake.path)
    assert other.get(h["auth_id"])["status"] == "CREATED"
    other.void(h["auth_id"], "v")
    assert fake.get(h["auth_id"])["status"] == "VOIDED"


def test_recover_capture_by_order(fake):
    h = hold(fake)
    assert fake.recover_capture(h["order_id"]) is None
    c = fake.capture(h["auth_id"], "32.00", "r1")
    assert fake.recover_capture(h["order_id"])["capture_id"] == c["capture_id"]


def test_bad_amount_and_cards(fake):
    with pytest.raises(P.HoldError):
        fake.authorize("0", "USD", "x", "r")
    with pytest.raises(P.HoldError):
        P.get_card(10)
    assert len(P.TEST_CARDS) == 10 and P.get_card(0)["number"] == "4111111111111111"


def test_request_id_shape():
    assert P.request_id_for("d_1", "sim-001", "authorize") == "crowd-d_1-sim-001-authorize"
    assert P.request_id_for("d_1", "sim-001", "void", 2) == "crowd-d_1-sim-001-void2"


def test_make_holds_modes(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    assert P.make_holds(str(tmp_path / "x.db")).kind == "fake"
    monkeypatch.setenv("CROWD_PAYPAL", "bitcoin")
    with pytest.raises(P.HoldError):
        P.make_holds()
