"""Crowd fix round 2 (docs/crowd-fixes-r2.md): the money-safety gaps a second refuter found, each with a test that failed
before the fix. Fake PayPal only. The principle behind all of them: never infer PayPal's state from our own clock or from
an error class when PayPal can be asked; when unsure, stop and hand it to a person; never release or charge money on a
guess. Decision D-11 stands: a hold the engine never confirmed is released, never adopted."""
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import Clock, join_many
from crowd import paypal_holds as P
from crowd import togi_bundle as tb
from crowd import verify as V
from crowd.engine import CrowdError, Engine, m, plan_token
from crowd.paypal_holds import FakePayPal
from crowd.store import Store, parse_iso, parse_utc
from test_fixes_r1 import Wrap, code, edit_fake, fake_state, open_auths, types

D = Decimal


def evs(engine, drop, *kinds):
    return [e["payload"] for e in engine.store.events(drop) if e["type"] in kinds]


def auth_of(engine, drop, buyer):
    return engine.get(drop).buyers[buyer].auth_id


def pp_status(fake, auth_id):
    return fake_state(fake)["auths"][auth_id]["status"]


def all_statuses(fake):
    return sorted({a["status"] for a in fake_state(fake)["auths"].values()})


def blocked(fn, *a, **kw):
    with pytest.raises(CrowdError) as e:
        fn(*a, **kw)
    assert e.value.code == "settle_blocked", e.value.code
    return e.value


def closed_drop(engine, drop, n=5):
    join_many(engine, drop, n)
    engine.close(drop)
    return plan_token(engine.plan(drop))


# ===== F1: an account-level refusal must not void (or fail) every hold ================================================

SYSTEMIC = [("NOT_AUTHORIZED", 403), ("PERMISSION_DENIED", 403), ("PAYEE_ACCOUNT_RESTRICTED", 422),
            ("PAYEE_ACCOUNT_LOCKED", 422), ("INVALID_REQUEST", 400), ("SOMETHING_NEW", 409), ("UNPROCESSABLE_ENTITY", 422)]


@pytest.mark.parametrize("issue,http", SYSTEMIC)
def test_f1_a_refusal_that_may_be_about_the_account_stops_settle_and_voids_nothing(engine, drop, fake, issue, http):
    """q1: every capture refused for the merchant's sake used to mark all buyers failed and then VOID every hold."""
    tok = closed_drop(engine, drop)
    fake.refuse("capture", issue, http=http, times=None)
    e = blocked(engine.settle, drop, expect=tok)
    assert e.ctx["issue"] == issue and e.ctx["http"] == http and e.ctx["debug_id"].startswith("FAKEDBG")
    assert "person" in e.ctx["next"] and e.ctx["phase"] == "capture"
    d = engine.get(drop)
    assert d.state == "settling" and all(b.status == "held" for b in d.buyers.values())
    sb = evs(engine, drop, "settle_blocked")
    assert len(sb) == 1 and sb[0]["code"] == issue and sb[0]["debug_id"] == e.ctx["debug_id"] and sb[0]["buyer"] == "buyer-01"
    assert not evs(engine, drop, "capture_failed", "hold_voided", "failed_hold_released", "drop_settled")
    assert all_statuses(fake) == ["CREATED"]                      # nothing captured, nothing voided


def test_f1_a_blocked_settle_resumes_once_a_person_has_fixed_the_cause(engine, drop, fake):
    tok = closed_drop(engine, drop)
    fake.refuse("capture", "PAYEE_ACCOUNT_RESTRICTED", times=None)
    blocked(engine.settle, drop, expect=tok)
    edit_fake(fake, lambda st: st.update(faults=[]))              # the person fixed the account
    out = engine.settle(drop, expect=tok)
    assert out["counts"]["captured"] == 5 and out["counts"]["failed"] == 0 and out["state"] == "settled"
    assert V.verify(drop, engine)["ok"]
    assert engine.get(drop).blocked is None                       # settled: no longer blocked


def test_f1_an_empty_403_body_is_systemic(engine, drop, fake, store, clock):
    tok = closed_drop(engine, drop)

    class Empty403(Wrap):
        def capture(self, *a, **k):
            raise P._error_from(403, {})                          # PayPal's empty 403 (F-11c)

    eng = Engine(store, Empty403(fake), clock)
    e = blocked(eng.settle, drop, expect=tok)
    assert e.ctx["http"] == 403 and all_statuses(fake) == ["CREATED"]


def test_f1_credentials_that_are_refused_stop_settle_the_same_way(engine, drop, fake, store, clock):
    tok = closed_drop(engine, drop)

    class BadCredentials(Wrap):
        def capture(self, *a, **k):
            raise P.HoldError("paypal_not_configured", "PayPal refused the sandbox credentials (http 401)", http=401)

    blocked(Engine(store, BadCredentials(fake), clock).settle, drop, expect=tok)
    assert evs(engine, drop, "settle_blocked")[0]["http"] == 401 and all_statuses(fake) == ["CREATED"]


def test_f1_a_403_is_systemic_even_when_it_carries_a_buyer_specific_name(engine, drop, fake):
    tok = closed_drop(engine, drop)
    fake.refuse("capture", "INSTRUMENT_DECLINED", http=403, times=None)     # F-13 asks whether this ever happens
    blocked(engine.settle, drop, expect=tok)
    assert not evs(engine, drop, "capture_failed")


def test_f1_one_unfamiliar_name_beside_a_familiar_one_makes_the_refusal_unfamiliar(engine, drop, fake, store, clock):
    tok = closed_drop(engine, drop)

    class Mixed(Wrap):
        def capture(self, *a, **k):
            raise P._error_from(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"},
                                                                                  {"issue": "PAYEE_ACCOUNT_RESTRICTED"}]})

    blocked(Engine(store, Mixed(fake), clock).settle, drop, expect=tok)


BUYER_SPECIFIC = ["INSTRUMENT_DECLINED", "TRANSACTION_REFUSED", "PAYER_ACTION_REQUIRED", "AUTHORIZATION_EXPIRED",
                  "AUTHORIZATION_VOIDED", "AUTHORIZATION_DENIED", "MAX_CAPTURE_AMOUNT_EXCEEDED"]


@pytest.mark.parametrize("issue", BUYER_SPECIFIC)
def test_f1_a_refusal_about_one_hold_is_recorded_against_that_buyer_and_settle_goes_on(engine, drop, fake, issue):
    tok = closed_drop(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-02")
    fake.refuse("capture", issue, http=422, match=victim, times=None)
    out = engine.settle(drop, expect=tok)
    assert out["counts"] == {"holds": 4, "captured": 3, "voided": 0, "failed": 1, "left": 0}
    cf = evs(engine, drop, "capture_failed")[0]
    assert cf["issue"] == issue and cf["buyer"] == "buyer-02" and cf["debug_id"]
    assert pp_status(fake, victim) == "VOIDED" and V.verify(drop, engine)["ok"]      # released, once everyone had an outcome


def test_f1_the_card_declined_family_is_buyer_specific_too():
    for issue in ("CARD_EXPIRED", "INSUFFICIENT_FUNDS", "CREDIT_CARD_REFUSED", "PAYER_CANNOT_PAY", "INVALID_SECURITY_CODE"):
        assert P.buyer_specific(P._error_from(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": issue}]}))
    for http, body in ((401, {}), (403, {}), (400, {"name": "INVALID_REQUEST"}), (422, {"name": "UNPROCESSABLE_ENTITY"}),
                       (404, {"name": "RESOURCE_NOT_FOUND"}), (409, {"details": [{"issue": "WHATEVER"}]})):
        assert not P.buyer_specific(P._error_from(http, body)), (http, body)


def test_f1_a_void_refused_for_a_systemic_reason_stops_a_missed_settlement_too(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.close(drop)
    fake.refuse("void", "NOT_AUTHORIZED", http=403, times=None)
    e = blocked(engine.settle, drop)
    assert e.ctx["phase"] == "void" and engine.get(drop).state == "settling"
    assert not evs(engine, drop, "void_failed", "hold_voided") and all_statuses(fake) == ["CREATED"]


def test_f1_a_hold_paypal_will_not_release_for_a_buyer_reason_is_still_recorded_and_settle_goes_on(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.close(drop)
    fake.refuse("void", "PAYER_ACTION_REQUIRED", match=auth_of(engine, drop, "buyer-01"), times=None)
    out = engine.settle(drop)
    assert out["unreleased"] == 1 and out["counts"]["voided"] == 1


def test_f1_the_release_pass_is_also_stopped_by_a_systemic_refusal(engine, drop, fake):
    tok = closed_drop(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-02")
    fake.refuse("capture", "TRANSACTION_REFUSED", match=victim, times=None)
    fake.refuse("void", "NOT_AUTHORIZED", http=403, match=victim, times=None)
    e = blocked(engine.settle, drop, expect=tok)
    assert e.ctx["phase"] == "release" and engine.get(drop).state == "settling"
    assert not evs(engine, drop, "void_failed", "failed_hold_released")


def test_f1_two_capture_failures_in_a_row_with_the_same_code_stop_settle_before_any_release(engine, drop, fake):
    """The circuit breaker: two identical failures in a row share a cause, so stop with every hold still held."""
    tok = closed_drop(engine, drop)
    for n in ("buyer-01", "buyer-02"):
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    e = blocked(engine.settle, drop, expect=tok)
    d = engine.get(drop)
    assert [b.status for b in d.buyers.values()] == ["capture_failed", "capture_failed", "held", "held", "held"]
    assert e.ctx["issue"] == "TRANSACTION_REFUSED" and e.ctx["phase"] == "capture"
    assert not evs(engine, drop, "failed_hold_released", "hold_voided")
    assert pp_status(fake, auth_of(engine, drop, "buyer-01")) == "CREATED"        # not released
    sb = evs(engine, drop, "settle_blocked")[0]
    assert sb["code"] == "TRANSACTION_REFUSED" and sb["buyers"] == ["buyer-01", "buyer-02"]


def test_f1_the_breaker_through_the_bundle_is_a_complete_refusal_with_no_loose_placeholders(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 4)
    tb.drop_close(d)
    tok = tb.drop_plan(d)["expect"]
    FakePayPal(str(tmp_path / "crowd.db") + ".fakepaypal.json").refuse("capture", "TRANSACTION_REFUSED", times=None)
    with pytest.raises(tb.TogiError) as e:
        tb.drop_settle(d, tok)
    assert e.value.code == "settle_blocked" and "{" not in e.value.message + e.value.next
    assert "TRANSACTION_REFUSED" in e.value.message and "sim-001, sim-002" in e.value.message


def test_f1_a_block_is_no_longer_shown_once_settlement_has_gone_on(engine, drop, fake):
    tok = closed_drop(engine, drop)
    fake.refuse("capture", "PAYEE_ACCOUNT_RESTRICTED", times=1)
    blocked(engine.settle, drop, expect=tok)
    assert engine.get(drop).blocked is not None
    fake.unavailable("capture", match=auth_of(engine, drop, "buyer-03"), times=1)      # the next run gets a few buyers done, then stops
    assert code(engine.settle, drop, expect=tok) == "paypal_unavailable"
    assert engine.get(drop).blocked is None and engine.view(engine.get(drop)).get("settle_blocked") is None
    with pytest.raises(CrowdError) as ex:
        V.verify(drop, engine)
    assert ex.value.code == "drop_not_settled"


def test_f1_after_the_breaker_a_person_can_look_and_settle_resumes_with_a_fresh_count(engine, drop, fake):
    tok = closed_drop(engine, drop)
    for n in ("buyer-01", "buyer-02"):
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    blocked(engine.settle, drop, expect=tok)
    new = plan_token(engine.plan(drop))
    assert new == "capture:3:36.00:108.00+release:2" and new != tok          # the approval now covers the two releases
    assert code(engine.settle, drop, expect=tok) == "plan_changed"
    out = engine.settle(drop, expect=new)
    assert out["counts"] == {"holds": 5, "captured": 3, "voided": 0, "failed": 2, "left": 0}
    assert V.verify(drop, engine)["ok"] and open_auths(fake) == {}


def test_f1_failures_that_are_not_in_a_row_do_not_trip_the_breaker_whatever_their_codes(engine, drop, fake):
    """(Round 3 C: two in a row of ANY buyer-specific codes now trip it, see test_fixes_r3; failures a capture apart do not.)"""
    tok = closed_drop(engine, drop)
    fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, "buyer-01"), times=None)
    fake.refuse("capture", "PAYER_ACTION_REQUIRED", match=auth_of(engine, drop, "buyer-03"), times=None)
    assert engine.settle(drop, expect=tok)["counts"]["failed"] == 2


def test_f1_a_capture_that_goes_through_between_two_failures_resets_the_count(engine, drop, fake):
    tok = closed_drop(engine, drop)
    for n in ("buyer-01", "buyer-03"):
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    assert engine.settle(drop, expect=tok)["counts"]["failed"] == 2


def test_f1_failed_holds_are_released_only_after_every_held_buyer_has_an_outcome(engine, drop, fake):
    tok = closed_drop(engine, drop)
    fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, "buyer-01"), times=None)
    engine.settle(drop, expect=tok)
    kinds = [e["type"] for e in engine.store.events(drop)]
    last_outcome = max(i for i, k in enumerate(kinds) if k in ("hold_captured", "capture_failed"))
    assert kinds.index("failed_hold_released") > last_outcome


def test_f1_verify_reports_a_blocked_settle_as_a_problem_with_the_next_step(engine, drop, fake):
    tok = closed_drop(engine, drop)
    fake.refuse("capture", "PAYEE_ACCOUNT_RESTRICTED", times=None)
    blocked(engine.settle, drop, expect=tok)
    v = V.verify(drop, engine)
    assert v["ok"] is False and v["blocked"] is True and len(v["problems"]) == 1
    assert "PAYEE_ACCOUNT_RESTRICTED" in v["problems"][0] and "debug id FAKEDBG" in v["problems"][0]
    assert "Next:" in v["problems"][0] and "person" in v["next"] and "drop plan" in v["next"]


def test_f1_the_plan_and_status_show_that_settlement_is_blocked(engine, drop, fake):
    tok = closed_drop(engine, drop)
    fake.refuse("capture", "PAYEE_ACCOUNT_RESTRICTED", times=None)
    blocked(engine.settle, drop, expect=tok)
    assert engine.plan(drop)["blocked"]["issue"] == "PAYEE_ACCOUNT_RESTRICTED"
    v = engine.view(engine.get(drop))
    assert v["state"] == "settling" and v["settle_blocked"]["issue"] == "PAYEE_ACCOUNT_RESTRICTED"


def test_f1_through_the_bundle_a_blocked_settle_is_a_refusal_that_names_a_person(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 3)
    tb.drop_close(d)
    tok = tb.drop_plan(d)["expect"]
    FakePayPal(str(tmp_path / "crowd.db") + ".fakepaypal.json").refuse("capture", "PAYEE_ACCOUNT_LOCKED", times=None)
    with pytest.raises(tb.TogiError) as e:
        tb.drop_settle(d, tok)
    assert e.value.code == "settle_blocked" and e.value.status == "refused"
    assert "PAYEE_ACCOUNT_LOCKED" in e.value.message and "FAKEDBG" in e.value.message
    assert "person" in e.value.next and "{" not in e.value.message + e.value.next
    out = tb.drop_verify(d)
    assert out["ok"] is False and "person" in out["next"] and out["next"] != f"togi crowd drop explain --drop {d}"
    assert "stopped for a person" in tb.drop_explain(d)["text"]


# ===== F2: a lost reply on a leave must not fund a drop that missed its minimum =======================================

def test_f2_a_leave_whose_reply_is_lost_is_settled_by_reading_the_authorisation(engine, drop, fake):
    """q8: PayPal voided cy's hold, the reply was lost, the engine kept counting cy and 'met' the minimum."""
    join_many(engine, drop, 3)
    cy = auth_of(engine, drop, "buyer-03")
    fake.lose_reply("void", match=cy)
    assert code(engine.leave, drop, "buyer-03") == "paypal_unavailable"
    assert pp_status(fake, cy) == "VOIDED" and engine.get(drop).buyers["buyer-03"].status == "held"
    assert engine.get(drop).leaving["buyer-03"]["auth_id"] == cy and engine.view(engine.get(drop))["unresolved_leaves"] == 1
    out = engine.close(drop)
    assert out["reconciled"]["leaves_completed"] == ["buyer-03"]
    d = engine.get(drop)
    assert d.buyers["buyer-03"].status == "left" and d.leaving == {} and d.closed["count"] == 2 and d.closed["funded"] is False
    assert plan_token(engine.plan(drop)) == "void:2"
    s = engine.settle(drop, expect="void:2")
    assert s["outcome"] == "not_funded" and s["totals"]["gross"] == "0.00" and V.verify(drop, engine)["ok"]


def test_f2_the_leave_is_written_down_before_paypal_is_asked(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.leave(drop, "buyer-02")
    ev = [e["type"] for e in engine.store.events(drop)]
    assert ev[-3:] == ["leave_started", "hold_voided", "buyer_left"]
    started = evs(engine, drop, "leave_started")[0]
    assert started["buyer"] == "buyer-02" and started["auth_id"] and started["paypal_mode"] == "fake"
    assert engine.get(drop).leaving == {}


def test_f2_a_leave_that_never_reached_paypal_is_honoured_at_close_by_releasing_the_hold(engine, drop, fake):
    """(Decision D-11b, round 3: this used to keep the buyer in and charge them. The buyer asked to leave before the deadline,
    saw an error, and so is never charged: close retries the void and the buyer is out.)"""
    join_many(engine, drop, 3)
    cy = auth_of(engine, drop, "buyer-03")
    fake.unavailable("void", match=cy)
    assert code(engine.leave, drop, "buyer-03") == "paypal_unavailable"
    out = engine.close(drop)
    assert out["reconciled"]["leaves_completed"] == ["buyer-03"] and out["reconciled"]["leaves_kept"] == []
    assert engine.get(drop).buyers["buyer-03"].status == "left" and engine.get(drop).closed["count"] == 2
    assert pp_status(fake, cy) == "VOIDED" and not evs(engine, drop, "leave_failed")


def test_f2_a_leave_paypal_refused_stays_on_record_and_is_completed_at_close(engine, drop, fake):
    """(D-11b, round 3: a refused leave used to be closed at once with the hold left theirs, and they were charged. The buyer
    asked in time and saw an error, so the leave stands; close retries the void.)"""
    join_many(engine, drop, 3)
    victim = auth_of(engine, drop, "buyer-03")
    fake.refuse("void", "PAYER_ACTION_REQUIRED", match=victim)
    assert code(engine.leave, drop, "buyer-03") == "leave_pending"
    assert "buyer-03" in engine.get(drop).leaving and not evs(engine, drop, "leave_failed")
    out = engine.close(drop)
    assert out["reconciled"]["leaves_completed"] == ["buyer-03"] and engine.get(drop).buyers["buyer-03"].status == "left"
    assert pp_status(fake, victim) == "VOIDED"


def test_f2_a_leave_on_a_hold_captured_elsewhere_is_a_conflict_and_is_closed(engine, drop, fake):
    join_many(engine, drop, 3)
    fake.capture(auth_of(engine, drop, "buyer-03"), D("40"), "someone-else")
    assert code(engine.leave, drop, "buyer-03") == "void_conflict"
    assert engine.get(drop).leaving == {}


def test_f2_a_resolved_leave_is_dated_when_it_was_asked_so_the_deadline_crowd_excludes_it(engine, drop, fake, clock):
    join_many(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-04")
    asked = clock.t
    fake.lose_reply("void", match=victim)
    assert code(engine.leave, drop, "buyer-04") == "paypal_unavailable"
    clock.advance(hours=3)                                          # nobody closed the drop for hours
    out = engine.close(drop)
    assert out["reconciled"]["leaves_completed"] == ["buyer-04"]
    left = [e for e in engine.store.events(drop) if e["type"] == "buyer_left"][0]
    assert parse_iso(left["ts"]) == asked
    assert engine.get(drop).closed["count"] == 3


def test_f2_close_revalidates_every_hold_and_a_dead_one_does_not_count_toward_the_minimum(engine, drop, fake):
    join_many(engine, drop, 3)
    victim = auth_of(engine, drop, "buyer-02")
    fake.void(victim, "buyer-cancelled-at-paypal")               # behind the engine's back, no leave was ever asked
    out = engine.close(drop)
    assert out["revalidated"] == [{"buyer": "buyer-02", "paypal_status": "VOIDED", "action": "recorded"}]
    hv = evs(engine, drop, "hold_voided")[0]
    assert hv["reason"] == "paypal_says_voided" and hv["paypal_status"] == "VOIDED" and hv["buyer"] == "buyer-02"
    d = engine.get(drop)
    assert d.closed["count"] == 2 and d.closed["funded"] is False and d.buyers["buyer-02"].status == "voided"
    assert plan_token(engine.plan(drop)) == "void:2"             # the plan is derived from the revalidated crowd
    assert engine.settle(drop, expect="void:2")["outcome"] == "not_funded"
    assert V.verify(drop, engine)["ok"]


def test_f2_the_price_follows_the_revalidated_crowd(engine, drop, fake):
    join_many(engine, drop, 5)                                     # the step to 36.00 is locked at 5
    fake.expire(auth_of(engine, drop, "buyer-05"))
    engine.close(drop)
    d = engine.get(drop)
    assert d.closed["count"] == 4 and d.closed["funded"] is True and m(d.price()) == "36.00"    # a reached step stays locked
    hv = evs(engine, drop, "hold_voided")[0]
    assert hv["paypal_status"] == "EXPIRED" and hv["expired"] is True
    engine.settle(drop)
    assert V.verify(drop, engine)["ok"]


def test_f2_a_late_close_counts_a_dead_hold_out_of_the_crowd_as_of_the_deadline(engine, drop, fake, clock):
    join_many(engine, drop, 3)
    fake.void(auth_of(engine, drop, "buyer-01"), "x")
    clock.advance(hours=5)                                         # closed long after the deadline
    engine.close(drop)
    d = engine.get(drop)
    assert d.closed["reason"] == "expired" and d.closed["count"] == 2 and d.closed["funded"] is False


def test_f2_close_does_not_close_on_a_guess_when_paypal_cannot_be_asked(engine, drop, fake, store, clock):
    join_many(engine, drop, 3)

    class Down(Wrap):
        def get(self, auth_id):
            raise P.PayPalUnavailable(message="down")

    assert code(Engine(store, Down(fake), clock).close, drop) == "paypal_unavailable"
    assert engine.get(drop).state == "live" and "drop_closed" not in types(engine, drop)
    assert engine.close(drop)["state"] == "closed"


def test_f2_a_hold_captured_outside_the_engine_leaves_the_crowd_and_verify_says_so(engine, drop, fake):
    join_many(engine, drop, 4)
    fake.capture(auth_of(engine, drop, "buyer-02"), D("40"), "someone-else")
    out = engine.close(drop)
    assert out["revalidated"][0]["paypal_status"] == "CAPTURED"
    assert evs(engine, drop, "hold_voided")[0]["taken_outside_engine"] is True
    engine.settle(drop)
    v = V.verify(drop, engine)
    assert not v["ok"] and any("taken from the buyer outside the engine" in p and "refund" in p for p in v["problems"])


def test_f2_a_hold_paypal_cannot_find_is_left_out_of_the_crowd_and_verify_resolves_it_with_a_note(engine, drop, fake):
    """(Round 5, item 6: this used to be "verify keeps reporting it", with "run again when PayPal answers" for ever. PayPal HAD answered:
    404. The engine accepted that answer at close; verify now accepts the same answer, asks once more, and says so.)"""
    join_many(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-02")
    edit_fake(fake, lambda st: st["auths"].pop(victim))
    engine.close(drop)
    assert engine.get(drop).closed["count"] == 3
    engine.settle(drop)
    v = V.verify(drop, engine)
    assert v["ok"], v["problems"]
    row = [h for h in v["holds"] if h["buyer"] == "buyer-02"][0]
    assert row["paypal"] == "NOT_FOUND" and "no record of this authorisation (404)" in row["note"]


def test_f2_a_status_nobody_has_a_rule_for_is_voided_not_assumed_gone(engine, drop, fake):
    join_many(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-02")
    edit_fake(fake, lambda st: st["auths"][victim].update(status="PARTIALLY_CREATED"))
    out = engine.close(drop)
    assert out["revalidated"][0]["action"] == "voided" and pp_status(fake, victim) == "VOIDED"
    hv = evs(engine, drop, "hold_voided")[0]
    assert hv["released_by_void"] is True and hv["paypal_status"] == "PARTIALLY_CREATED"
    engine.settle(drop)
    assert V.verify(drop, engine)["ok"]


def test_f2_verify_fails_on_a_leave_nobody_confirmed(engine, drop):
    join_many(engine, drop, 3)
    engine.close(drop)
    engine.settle(drop)
    engine.store.append(drop, "leave_started", {"buyer": "buyer-01", "auth_id": "X"})
    v = V.verify(drop, engine)
    assert not v["ok"] and any("leave(s) started but never confirmed" in p and "Next:" in p for p in v["problems"])


# ===== F3: an unknown or missing authorisation status ===================================================================

def lose_ada(engine, drop, fake, status):
    fake.lose_reply("authorize", match=":ada")
    with pytest.raises(CrowdError):
        engine.join(drop, "ada")
    ada = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":ada")][0]
    edit_fake(fake, lambda st: st["auths"][ada].update(status=status))
    return ada


@pytest.mark.parametrize("status", ["PARTIALLY_CREATED", None, "DENIED", "SOMETHING_ELSE"])
def test_f3_a_hold_in_a_status_nobody_has_a_rule_for_is_voided_never_just_recorded_as_released(engine, drop, fake, status):
    """q5: it used to be recorded as released while PayPal still held the money, and verify never looked again."""
    ada = lose_ada(engine, drop, fake, status)
    rec = engine.reconcile(drop)
    assert rec["released"] == ["ada"] and pp_status(fake, ada) == "VOIDED"
    rel = evs(engine, drop, "orphan_hold_released")[0]
    assert rel["paypal_status"] == status and rel["auth_id"] == ada
    join_many(engine, drop, 3, start=2)
    engine.close(drop)
    engine.settle(drop)
    assert V.verify(drop, engine)["ok"]


def test_f3_a_hold_in_an_odd_status_that_cannot_be_voided_is_reported_not_released(engine, drop, fake):
    ada = lose_ada(engine, drop, fake, "PARTIALLY_CREATED")
    fake.refuse("void", "PAYER_ACTION_REQUIRED", match=ada, times=None)
    rec = engine.reconcile(drop)
    assert rec["released"] == [] and rec["unconfirmed"] == ["ada"]
    assert not evs(engine, drop, "orphan_hold_released") and evs(engine, drop, "void_failed")[0]["orphan"] is True
    assert pp_status(fake, ada) == "PARTIALLY_CREATED"
    join_many(engine, drop, 3, start=2)
    engine.close(drop)
    engine.settle(drop)
    v = V.verify(drop, engine)
    assert not v["ok"] and any(ada in p and "still PARTIALLY_CREATED" in p and "Next:" in p for p in v["problems"])


@pytest.mark.parametrize("status", ["VOIDED", "EXPIRED"])
def test_f3_only_voided_or_expired_is_taken_as_already_gone_without_a_void(engine, drop, fake, store, clock, status):
    ada = lose_ada(engine, drop, fake, status)

    class Spy(Wrap):
        calls = []

        def void(self, *a, **k):
            self.calls.append(a)
            return self.inner.void(*a, **k)

    spy = Spy(fake)
    Engine(store, spy, clock).reconcile(drop)
    assert spy.calls == [] and evs(engine, drop, "orphan_hold_released")[0]["reason"] == f"already_{status.lower()}"


def test_f3_verify_reads_back_released_orphans_too(engine, drop, fake):
    ada = lose_ada(engine, drop, fake, "CREATED")
    join_many(engine, drop, 3, start=2)
    engine.close(drop)                                              # voids ada's orphan hold
    engine.settle(drop)
    assert V.verify(drop, engine)["ok"]
    edit_fake(fake, lambda st: st["auths"][ada].update(status="CREATED"))      # PayPal shows it open again
    v = V.verify(drop, engine)
    assert not v["ok"] and any(ada in p and "recorded" in p and "released" in p and "CREATED" in p and "Next:" in p
                               for p in v["problems"])


def test_f3_a_void_refusal_that_merely_names_the_hold_voided_is_not_believed_unless_paypal_agrees(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.close(drop)
    victim = auth_of(engine, drop, "buyer-01")
    fake.refuse("void", "AUTHORIZATION_VOIDED", match=victim, times=None)      # but PayPal says CREATED
    out = engine.settle(drop)
    assert out["counts"].get("void_failed") == 1 and out["unreleased"] == 1
    assert pp_status(fake, victim) == "CREATED"
    v = V.verify(drop, engine)
    assert not v["ok"] and any("buyer-01" in p and "void it by hand" in p for p in v["problems"])


def test_f3_a_void_refusal_that_names_the_hold_voided_is_believed_when_paypal_says_so(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.close(drop)
    victim = auth_of(engine, drop, "buyer-01")
    fake.void(victim, "elsewhere")
    out = engine.settle(drop)
    assert out["counts"]["voided"] == 2 and "unreleased" not in out and V.verify(drop, engine)["ok"]


# ===== F4: a stale "live" hold after a replayed create ===================================================================

def test_f4_the_fake_answers_a_replay_with_the_hold_as_it_is_now(fake):
    first = fake.authorize(D("40"), "USD", "d_x:ada", "rid-1")
    assert first["status"] == "CREATED" and "replayed" not in first
    fake.void(first["auth_id"], "v1")
    again = fake.authorize(D("40"), "USD", "d_x:ada", "rid-1")
    assert again["status"] == "VOIDED" and again["replayed"] is True and again["auth_id"] == first["auth_id"]
    fake.expire(first["auth_id"])                                  # and when it lapses, the replay says so
    assert fake.authorize(D("40"), "USD", "d_x:ada", "rid-1")["status"] == "EXPIRED"


def test_f4_a_replayed_hold_that_is_no_longer_open_is_not_counted(engine, drop, fake):
    """q6: reconcile voided ada's hold, the reply was lost, and ada's retry got the cached CREATED answer for a dead hold."""
    fake.lose_reply("authorize", match=":ada")
    with pytest.raises(CrowdError):
        engine.join(drop, "ada")
    join_many(engine, drop, 2, start=2)
    ada = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":ada")][0]
    fake.lose_reply("void", match=ada)
    assert code(engine.close, drop) == "paypal_unavailable"
    assert pp_status(fake, ada) == "VOIDED" and engine.get(drop).state == "live"
    assert code(engine.join, drop, "ada") == "hold_not_open"
    d = engine.get(drop)
    assert d.count == 2 and d.unresolved == {} and d.buyers["ada"].status != "held"       # the dead hold is not a buyer
    rel = evs(engine, drop, "orphan_hold_released")[0]
    assert rel["reason"] == "already_voided" and rel["auth_id"] == ada
    assert engine.join(drop, "ada")["buyers"] == 3                  # a new attempt, a new hold
    assert pp_status(fake, d.buyers["buyer-02"].auth_id) == "CREATED"
    engine.close(drop)
    out = engine.settle(drop, expect=plan_token(engine.plan(drop)))
    assert out["counts"]["captured"] == 3 and V.verify(drop, engine)["ok"]


class GetSpy(Wrap):
    def __init__(self, inner):
        super().__init__(inner)
        self.gets = []

    def get(self, auth_id):
        self.gets.append(auth_id)
        return self.inner.get(auth_id)


def test_f4_a_replayed_open_hold_is_confirmed_with_a_get_before_it_is_recorded(engine, drop, fake, store, clock):
    fake.lose_reply("authorize", match=":ada")
    with pytest.raises(CrowdError):
        engine.join(drop, "ada")
    spy = GetSpy(fake)
    r = Engine(store, spy, clock).join(drop, "ada")
    assert len(spy.gets) == 1 and spy.gets[0] == r["auth_id"]
    hp = evs(engine, drop, "hold_placed")[0]
    assert hp["replayed"] is True and hp["confirmed_status"] == "CREATED"


def test_f4_a_fresh_join_does_not_pay_for_a_confirming_get(engine, drop, fake, store, clock):
    spy = GetSpy(fake)
    Engine(store, spy, clock).join(drop, "ada")
    assert spy.gets == []


def test_f4_if_the_confirming_get_cannot_be_answered_the_join_stays_open_for_reconcile(engine, drop, fake, store, clock):
    fake.lose_reply("authorize", match=":ada")
    with pytest.raises(CrowdError):
        engine.join(drop, "ada")

    class Down(Wrap):
        def get(self, auth_id):
            raise P.PayPalUnavailable(message="down")

    assert code(Engine(store, Down(fake), clock).join, drop, "ada") == "paypal_unavailable"
    assert len(engine.get(drop).unresolved) == 1 and engine.get(drop).count == 0
    assert engine.reconcile(drop)["released"] == ["ada"] and open_auths(fake) == {}       # D-11


def test_f4_the_real_client_marks_a_replay_and_hands_back_any_status_but_denied(monkeypatch):
    from test_paypal_wire import CREATED_ORDER, AUTHORISED, Net, StubClient
    net = Net(monkeypatch)
    holds = P.PayPalHolds(client=StubClient())
    net.reply(CREATED_ORDER, 200)                                 # PayPal answers the create with an existing order
    net.reply({**CREATED_ORDER, "purchase_units": [{"payments": {"authorizations": [
        {"id": "A1", "status": "VOIDED", "amount": {"value": "40.00", "currency_code": "USD"}}]}}]}, 200)
    out = holds.authorize("40", "USD", "d_1:b1", "rid", card=P.get_card(0))
    assert out["replayed"] is True and out["status"] == "VOIDED" and out["auth_id"] == "A1"


# ===== F5: one clock read per join / leave ===============================================================================

class Ticking(Clock):
    """Every read of the clock is a little later than the one before (a descheduled process, a pause)."""

    def __init__(self, tick_ms=2):
        super().__init__()
        self.tick = timedelta(milliseconds=tick_ms)
        self.reads = 0

    def __call__(self):
        out = self.t
        self.t += self.tick
        self.reads += 1
        return out


def test_f5_a_join_at_the_edge_of_the_deadline_is_stamped_with_the_instant_it_was_checked(store, fake):
    """q2: checked at ends-1ms, stamped after ends: counted by the join, not by the deadline crowd."""
    clock = Ticking(0)
    eng = Engine(store, fake, clock)
    d = eng.create_drop("Mugs", "3:40,5:36", 3, 60)
    eng.publish(d)
    join_many(eng, d, 4)
    ends = parse_iso(eng.get(d).ends_at)
    clock.t, clock.tick = ends - timedelta(milliseconds=1), timedelta(milliseconds=2)
    r = eng.join(d, "eve")
    assert r["buyers"] == 5 and r["step_locked"] == 1
    stamps = {e["ts"] for e in eng.store.events(d) if e["type"] in ("join_started", "order_created", "buyer_joined", "hold_placed", "step_locked")
              and parse_iso(e["ts"]) >= ends - timedelta(milliseconds=1)}
    assert len(stamps) == 1 and parse_iso(stamps.pop()) < ends       # one instant, and it is before the deadline
    clock.tick = timedelta(0)
    clock.t = ends + timedelta(minutes=5)
    eng.close(d)
    assert eng.get(d).closed["count"] == 5 and m(eng.get(d).price()) == "36.00"
    out = eng.settle(d, expect=plan_token(eng.plan(d)))
    assert out["final_price"] == "36.00" and out["counts"]["captured"] == 5 and V.verify(d, eng)["ok"]


def test_f5_a_join_stamps_every_event_with_one_instant_though_the_clock_keeps_moving(store, fake):
    clock = Ticking(2)
    eng = Engine(store, fake, clock)
    d = eng.create_drop("Mugs", "3:40,5:36", 3, 60)
    eng.publish(d)
    eng.join(d, "ada")
    # one read decides the deadline and stamps every event of the join
    stamps = {e["ts"] for e in eng.store.events(d) if e["type"] in ("join_started", "order_created", "buyer_joined", "hold_placed")}
    assert len(stamps) == 1


def test_f5_a_leave_at_the_edge_of_the_deadline_is_stamped_with_the_instant_it_was_checked(store, fake):
    clock = Ticking(0)
    eng = Engine(store, fake, clock)
    d = eng.create_drop("Mugs", "3:40,5:36", 3, 60)
    eng.publish(d)
    join_many(eng, d, 4)
    ends = parse_iso(eng.get(d).ends_at)
    clock.t, clock.tick = ends - timedelta(milliseconds=1), timedelta(milliseconds=2)
    eng.leave(d, "buyer-04")
    stamps = {e["ts"] for e in eng.store.events(d) if e["type"] in ("leave_started", "hold_voided", "buyer_left")}
    assert len(stamps) == 1 and parse_iso(stamps.pop()) < ends
    clock.tick = timedelta(0)
    clock.t = ends + timedelta(minutes=5)
    eng.close(d)
    assert eng.get(d).closed["count"] == 3


# ===== F6: resuming after the expiry date =============================================================================

def test_f6_a_capture_that_went_through_before_the_expiry_is_recovered_not_called_expired(engine, drop, fake, clock):
    """q3: PayPal captured bob, the reply was lost, settle resumed after the date: bob was marked hold_expired by the clock
    while PayPal showed a capture, and cy was marked expired though PayPal still held the money."""
    join_many(engine, drop, 3)
    engine.close(drop)
    bob = auth_of(engine, drop, "buyer-02")
    fake.lose_reply("capture", match=bob)
    assert code(engine.settle, drop, expect=plan_token(engine.plan(drop))) == "paypal_unavailable"
    clock.advance(days=30)
    tok = plan_token(engine.plan(drop))
    assert tok == "capture:2:40.00:80.00"                         # bob (already captured) and cy are both still to be recorded
    out = engine.settle(drop, expect=tok)
    assert out["counts"] == {"holds": 3, "captured": 3, "voided": 0, "failed": 0, "left": 0}
    assert not evs(engine, drop, "capture_failed") and evs(engine, drop, "hold_captured")[1]["buyer"] == "buyer-02"
    assert evs(engine, drop, "hold_captured")[1]["recovered"] is True        # found at PayPal, not charged a second time
    assert len(fake_state(fake)["captures"]) == 3 and V.verify(drop, engine)["ok"]


class CannotListCaptures(Wrap):
    """A PayPal client that cannot list the captures on an order (so only the authorisation's status is known)."""
    order_captures = None


def test_f6_a_hold_paypal_says_is_captured_but_whose_capture_cannot_be_found_stops_settle(engine, drop, fake, store, clock):
    """Settlement is under way and the engine has an attempt on record for a hold (its capture reached PayPal; the reply was lost), so
    a capture on it may be the engine's own. PayPal says the hold is CAPTURED, but the one capture of the final price cannot be found
    (here: what PayPal shows is for another amount): nothing is guessed, a person looks. (Round 4: a CAPTURED hold the engine never
    tried to capture is somebody else's, not a block; see tests/crowd/test_fixes_r4.py.)"""
    join_many(engine, drop, 3)
    engine.close(drop)
    victim = auth_of(engine, drop, "buyer-02")
    eng = Engine(store, CannotListCaptures(fake), clock)
    fake.lose_reply("capture", match=victim)
    assert code(eng.settle, drop) == "paypal_unavailable"             # the capture reached PayPal; its reply never came back
    clock.advance(days=3650)
    edit_fake(fake, lambda st: [c.update(amount="39.00") for c in st["captures"].values() if c["auth_id"] == victim])
    e = blocked(eng.settle, drop)
    assert e.ctx["phase"] == "capture" and not evs(engine, drop, "capture_failed")
    assert evs(engine, drop, "settle_blocked")[0]["code"] == "captured_not_as_approved"


class CallSpy(Wrap):
    def __init__(self, inner):
        super().__init__(inner)
        self.calls = []

    def capture(self, auth_id, *a, **k):
        self.calls.append(("capture", auth_id))
        return self.inner.capture(auth_id, *a, **k)

    def void(self, auth_id, *a, **k):
        self.calls.append(("void", auth_id))
        return self.inner.void(auth_id, *a, **k)


def test_f6_a_hold_our_clock_calls_expired_is_still_captured_if_paypal_says_it_is_open(engine, drop, fake, store, clock):
    join_many(engine, drop, 3)
    engine.close(drop)
    clock.advance(days=3650)                                       # our clock: every hold is long expired
    spy = CallSpy(fake)
    eng = Engine(store, spy, clock)
    p = eng.plan(drop)
    assert p["holds"] == 3 and p["expired"] == 0
    out = eng.settle(drop, expect=plan_token(p))
    assert out["counts"]["captured"] == 3 and len([c for c in spy.calls if c[0] == "capture"]) == 3


def test_f6_a_hold_paypal_says_is_expired_is_not_captured_and_the_evidence_is_paypals(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    clock.advance(days=3650)
    victim = auth_of(engine, drop, "buyer-02")
    fake.expire(victim)
    spy = CallSpy(fake)
    eng = Engine(store, spy, clock)
    p = eng.plan(drop)
    assert p["holds"] == 3 and p["expired"] == 1 and plan_token(p) == "capture:3:40.00:120.00"
    out = eng.settle(drop, expect=plan_token(p))
    assert out["counts"] == {"holds": 4, "captured": 3, "voided": 1, "failed": 0, "left": 0} and victim not in [a for _, a in spy.calls]
    hv = evs(engine, drop, "hold_voided")[0]                       # round 3: recorded at the read, as PayPal's word
    assert hv["reason"] == "paypal_says_expired" and hv["paypal_status"] == "EXPIRED" and hv["expired"] is True and hv["checked_at"]
    assert V.verify(drop, eng)["ok"]


class LapsesAfterTheFirstLook(Wrap):
    """PayPal shows this hold as open the first time it is read, and EXPIRED every time after: it lapses mid-settlement."""

    def __init__(self, inner, victim):
        super().__init__(inner)
        self.victim, self.looks = victim, 0

    def get(self, auth_id):
        out = self.inner.get(auth_id)
        if auth_id == self.victim:
            self.looks += 1
            if self.looks == 1:
                return {**out, "status": "CREATED"}
        return out


def test_f6_a_hold_that_lapses_between_the_check_and_its_capture_is_recorded_with_paypals_word(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    clock.advance(days=3650)
    victim = auth_of(engine, drop, "buyer-02")
    fake.expire(victim)
    eng = Engine(store, LapsesAfterTheFirstLook(fake, victim), clock)
    out = eng.settle(drop)
    assert out["counts"]["failed"] == 1 and out["shortfall"] == 0
    cf = evs(engine, drop, "capture_failed")[0]
    assert cf["code"] == "hold_expired" and cf["paypal_status"] == "EXPIRED" and cf["checked_at"]
    assert V.verify(drop, engine)["ok"]


def test_f6_a_capture_that_answers_authorization_expired_is_recorded_as_a_buyer_refusal(engine, drop, fake):
    tok = closed_drop(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-02")
    fake.refuse("capture", "AUTHORIZATION_EXPIRED", match=victim, times=None)
    out = engine.settle(drop, expect=tok)
    assert out["counts"]["failed"] == 1 and engine.get(drop).buyers["buyer-02"].error == "authorization_expired"


def test_f6_in_a_missed_drop_a_hold_our_clock_calls_expired_is_still_voided_if_paypal_says_open(engine, drop, fake, store, clock):
    join_many(engine, drop, 2)
    engine.close(drop)
    clock.advance(days=3650)
    spy = CallSpy(fake)
    out = Engine(store, spy, clock).settle(drop)
    assert out["counts"]["voided"] == 2 and len([c for c in spy.calls if c[0] == "void"]) == 2
    assert all_statuses(fake) == ["VOIDED"]


def _strip_key(store, drop, event_type, key):
    """A test-only edit of a recorded event (the log is append-only to SQL, so go round it): remove one payload key."""
    import sqlite3
    c = sqlite3.connect(store.path)
    c.execute("DROP TRIGGER events_no_update")
    for i, body in c.execute("SELECT id, payload_json FROM events WHERE drop_id=? AND type=?", (drop, event_type)).fetchall():
        p = json.loads(body)
        p.pop(key, None)
        c.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(p, sort_keys=True), i))
    c.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;")
    c.commit()
    c.close()


def test_f6_a_hold_called_expired_without_paypals_word_is_flagged_by_verify(engine, drop, fake, store):
    join_many(engine, drop, 4)
    engine.close(drop)
    victim = auth_of(engine, drop, "buyer-02")
    fake.expire(victim)
    engine._clock = lambda: datetime(2036, 1, 1, tzinfo=timezone.utc)
    Engine(store, LapsesAfterTheFirstLook(fake, victim), engine._clock).settle(drop)
    assert V.verify(drop, engine)["ok"]                                      # PayPal said EXPIRED, and it is recorded
    _strip_key(store, drop, "capture_failed", "paypal_status")               # the same event as an engine that went by its clock alone
    v = V.verify(drop, engine)
    assert not v["ok"] and any("buyer-02" in p and "own clock" in p and "Next:" in p for p in v["problems"])


# ===== F7: a refused retry must not close an unconfirmed join ===========================================================

def test_f7_a_refused_retry_releases_the_hold_the_lost_reply_left_before_the_refusal_is_recorded(engine, drop, fake):
    """q4: the retry's refusal wrote hold_failed for the same request id, closing the intent that pointed at the hold."""
    fake.lose_reply("authorize", match=":ada")
    assert code(engine.join, drop, "ada") == "paypal_unavailable"
    ada = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":ada")][0]
    fake.refuse("authorize", "TRANSACTION_REFUSED", match=":ada")
    assert code(engine.join, drop, "ada") == "join_declined"
    assert pp_status(fake, ada) == "VOIDED"                        # released, not abandoned
    kinds = types(engine, drop)
    assert kinds.index("orphan_hold_released") < len(kinds) - 1 - kinds[::-1].index("hold_failed")   # released first, then the refusal
    assert engine.get(drop).unresolved == {}
    join_many(engine, drop, 3, start=2)
    engine.close(drop)
    engine.settle(drop)
    assert open_auths(fake) == {} and V.verify(drop, engine)["ok"]


def test_f7_if_the_earlier_hold_cannot_be_reached_the_refusal_is_not_recorded_at_all(engine, drop, fake, store, clock):
    fake.lose_reply("authorize", match=":ada")
    code(engine.join, drop, "ada")
    fake.refuse("authorize", "TRANSACTION_REFUSED", match=":ada")

    class Down(Wrap):
        def recover_join(self, *a, **k):
            raise P.PayPalUnavailable(message="down")

    assert code(Engine(store, Down(fake), clock).join, drop, "ada") == "paypal_unavailable"
    assert "hold_failed" not in types(engine, drop) and len(engine.get(drop).unresolved) == 1       # the intent is still open
    assert engine.reconcile(drop)["released"] == ["ada"]


# ===== F8: verify's blind spots ============================================================================================

def funded_drop(engine, drop):
    join_many(engine, drop, 3)
    engine.close(drop)
    engine.settle(drop)
    return drop


def test_f8_verify_compares_each_captures_currency_with_the_drops(engine, drop, fake):
    funded_drop(engine, drop)
    edit_fake(fake, lambda st: [c.update(currency="EUR") for c in st["captures"].values()])
    v = V.verify(drop, engine)
    assert not v["ok"] and any("EUR" in p and "USD" in p and "Next:" in p for p in v["problems"])


def test_f8_verify_compares_each_captures_parent_authorisation_with_the_recorded_one(engine, drop, fake):
    funded_drop(engine, drop)

    def swap(st):
        caps = list(st["captures"].values())
        caps[0]["auth_id"], caps[1]["auth_id"] = caps[1]["auth_id"], caps[0]["auth_id"]

    edit_fake(fake, swap)
    v = V.verify(drop, engine)
    assert not v["ok"] and any("belongs to authorisation" in p and "Next:" in p for p in v["problems"])
    assert [c for c in v["checks"] if c["check"].startswith("every capture belongs")][0]["ok"] is False


def test_f8_a_capture_whose_parent_paypal_does_not_name_is_reported_not_silently_passed(engine, drop, fake):
    funded_drop(engine, drop)
    edit_fake(fake, lambda st: [c.update(auth_id=None) for c in st["captures"].values()])
    v = V.verify(drop, engine)
    assert v["ok"] is True
    c = [c for c in v["checks"] if c["check"].startswith("every capture belongs")][0]
    assert "did not name the parent of 3" in c["detail"]


def test_f8_the_real_client_reads_the_parent_authorisation_from_either_place_paypal_puts_it():
    assert P._capture_view({"id": "C", "supplementary_data": {"related_ids": {"authorization_id": "A9"}}})["auth_id"] == "A9"
    assert P._capture_view({"id": "C", "links": [{"rel": "up", "href": "https://api.sandbox.paypal.com/v2/payments/authorizations/A7"}]})["auth_id"] == "A7"
    assert P._capture_view({"id": "C", "links": [{"rel": "up", "href": "https://api.sandbox.paypal.com/v2/checkout/orders/O1"}]})["auth_id"] is None
    assert P._capture_view({"id": "C"})["auth_id"] is None


def test_f8_every_problem_carries_a_concrete_next_step_that_is_not_just_explain(engine, drop, fake):
    funded_drop(engine, drop)
    auths = [b.auth_id for b in engine.get(drop).buyers.values()]
    edit_fake(fake, lambda st: (st["auths"][auths[0]].update(amount="39.00"), st["captures"].update(),
                                [c.update(currency="EUR") for c in st["captures"].values()]))
    v = V.verify(drop, engine)
    assert not v["ok"] and v["problem_details"] and len(v["problem_details"]) == len(v["problems"])
    for pd in v["problem_details"]:
        assert pd["next"] and "explain" not in pd["next"] and "Next:" in pd["problem"], pd
    assert v["next"] and "explain" not in v["next"]


def test_f8_the_bundle_points_at_the_problem_not_at_explain_when_verify_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 3)
    tb.drop_close(d)
    tb.drop_settle(d, tb.drop_plan(d)["expect"])
    assert tb.drop_verify(d)["next"] == f"togi crowd drop explain --drop {d}"            # nothing wrong: explain
    fk = FakePayPal(str(tmp_path / "crowd.db") + ".fakepaypal.json")
    st = json.load(open(fk.path))
    next(iter(st["auths"].values()))["amount"] = "1.00"
    json.dump(st, open(fk.path, "w"))
    out = tb.drop_verify(d)
    assert out["ok"] is False and out["next"] != f"togi crowd drop explain --drop {d}" and "PayPal sandbox" in out["next"]


@pytest.mark.parametrize("stamp", ["2026-11-30T00:00:00", "2026-11-30 00:00:00", "2026-11-30T00:00:00+02:00",
                                   "2026-11-30T00:00:00.123456Z", "not a date", "", None, 12345, "2026-13-45T99:99:99Z"])
def test_f8_the_expiry_check_never_crashes_whatever_paypal_sent(engine, drop, stamp):
    join_many(engine, drop, 1)
    b = engine.get(drop).buyers["buyer-01"]
    b.expires = stamp
    assert engine._expired(b) in (True, False)


def test_f8_an_expiry_with_no_zone_is_read_as_utc_and_the_plan_works(engine, drop, fake, store, clock):
    """q12: a naive expiry compared with the aware clock raised TypeError out of plan."""
    class NaiveExpiry(Wrap):
        def authorize(self, *a, **k):
            out = self.inner.authorize(*a, **k)
            out["expires"] = "2026-11-20T00:00:00"                   # no zone
            return out

    eng = Engine(store, NaiveExpiry(fake), clock)
    join_many(eng, drop, 3)
    eng.close(drop)
    assert eng.plan(drop)["holds"] == 3
    clock.t = datetime(2026, 11, 20, 0, 0, 1, tzinfo=timezone.utc)      # one second past it, read as UTC
    assert eng._expired(eng.get(drop).buyers["buyer-01"]) is True
    assert eng.plan(drop)["holds"] == 3 and eng.plan(drop)["expired"] == 0       # PayPal still shows them open
    assert eng.settle(drop)["counts"]["captured"] == 3
    assert parse_utc("2026-11-20T00:00:00") == datetime(2026, 11, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_utc("tomorrow")


# ===== F9: a retry with a different card ===============================================================================

def test_f9_a_retry_on_another_card_releases_the_first_cards_hold_before_holding_the_new_one(engine, drop, fake):
    fake.lose_reply("authorize", match=":ada")
    assert code(engine.join, drop, "ada", 0) == "paypal_unavailable"
    first = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":ada")][0]
    r = engine.join(drop, "ada", 1)
    assert pp_status(fake, first) == "VOIDED" and r["auth_id"] != first and r["buyers"] == 1
    mine = {k: a["status"] for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":ada")}
    assert sorted(mine.values()) == ["CREATED", "VOIDED"]            # exactly one open hold, and it is the new card's
    rel = evs(engine, drop, "orphan_hold_released")[0]
    assert rel["reason"] == "card_changed" and rel["auth_id"] == first
    hp = evs(engine, drop, "hold_placed")[0]
    assert hp["attempt"] == 2 and hp["request_id"].endswith("authorize.2") and hp["auth_id"] == r["auth_id"]
    assert engine.get(drop).buyers["ada"].card_index == 1 and engine.get(drop).unresolved == {}
    join_many(engine, drop, 2, start=2)
    engine.close(drop)
    engine.settle(drop)
    assert open_auths(fake) == {} and V.verify(drop, engine)["ok"]


def test_f9_the_same_card_still_replays_the_same_hold(engine, drop, fake):
    fake.lose_reply("authorize", match=":ada")
    code(engine.join, drop, "ada", 0)
    r = engine.join(drop, "ada", 0)
    assert len([a for a in fake_state(fake)["auths"].values() if a["label"].endswith(":ada")]) == 1 and r["buyers"] == 1


def test_f9_if_the_first_hold_cannot_be_released_no_second_hold_is_placed(engine, drop, fake):
    fake.lose_reply("authorize", match=":ada")
    code(engine.join, drop, "ada", 0)
    first = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":ada")][0]
    fake.refuse("void", "PAYER_ACTION_REQUIRED", match=first, times=None)
    assert code(engine.join, drop, "ada", 1) == "earlier_attempt_unresolved"
    assert len([a for a in fake_state(fake)["auths"].values() if a["label"].endswith(":ada")]) == 1
    assert engine.get(drop).count == 0 and evs(engine, drop, "void_failed")[0]["orphan"] is True


# ===== F10: Togi's approval scope =========================================================================================

def sandbox_kind_engine(tmp_path, monkeypatch):
    """The bundle's engine, but one whose PayPal calls itself the sandbox (it is still the fake underneath)."""
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    fake = FakePayPal(str(tmp_path / "crowd.db") + ".fakepaypal.json")

    class AsSandbox(Wrap):
        kind = "sandbox"
        ref = "asandboxapp1"

    return lambda: Engine(Store(str(tmp_path / "crowd.db")), AsSandbox(fake))


def settled_ready(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 3)
    tb.drop_close(d)
    return d, tb.drop_plan(d)["expect"]


def test_f10_a_test_scope_approval_settles_against_the_fake(monkeypatch, tmp_path):
    d, tok = settled_ready(monkeypatch, tmp_path)
    out = tb.drop_settle(d, tok, ctx=SimpleNamespace(approval_scope="test"))
    assert out["outcome"] == "funded"


def test_f10_a_test_scope_approval_is_refused_outside_fake_mode_and_nothing_moves(monkeypatch, tmp_path):
    d, tok = settled_ready(monkeypatch, tmp_path)
    monkeypatch.setattr(tb, "_eng", sandbox_kind_engine(tmp_path, monkeypatch))
    with pytest.raises(tb.TogiError) as e:
        tb.drop_settle(d, tok, ctx=SimpleNamespace(approval_scope="test"))
    assert e.value.code == "test_approval_outside_test_mode" and e.value.status == "refused"
    assert "sandbox" in e.value.message and "{" not in e.value.message + e.value.next
    assert Engine(Store(str(tmp_path / "crowd.db")), FakePayPal(str(tmp_path / "crowd.db") + ".fakepaypal.json")).get(d).state == "closed"


@pytest.mark.parametrize("scope", ["default", None])
def test_f10_the_persons_own_approval_and_a_togi_that_reports_no_scope_still_settle(monkeypatch, tmp_path, scope):
    """Against a PayPal that calls itself the sandbox, with a drop that really belongs to it (round 3: a drop made with one
    PayPal can no longer be settled through another)."""
    factory = sandbox_kind_engine(tmp_path, monkeypatch)
    eng = factory()
    d = eng.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id="d_sbx1")
    eng.publish(d)
    join_many(eng, d, 3)
    eng.close(d)
    monkeypatch.setattr(tb, "_eng", factory)
    ctx = SimpleNamespace(approval_scope=scope)
    assert tb.drop_settle(d, tb.drop_plan(d)["expect"], ctx=ctx)["outcome"] == "funded"


def test_f10_a_test_approval_is_refused_when_crowd_env_is_not_test_even_with_the_fake(monkeypatch, tmp_path):
    d, tok = settled_ready(monkeypatch, tmp_path)
    monkeypatch.delenv("CROWD_ENV")
    with pytest.raises(tb.TogiError) as e:
        tb.drop_settle(d, tok, ctx=SimpleNamespace(approval_scope="test"))
    assert e.value.code == "test_approval_outside_test_mode" and "CROWD_ENV" in e.value.message and "{" not in e.value.message
    assert tb._eng().get(d).state == "closed"


def test_f10_a_test_approval_is_refused_when_the_store_holds_anything_that_is_not_this_fakes(monkeypatch, tmp_path):
    d, tok = settled_ready(monkeypatch, tmp_path)
    store = Store(str(tmp_path / "crowd.db"))
    store.append("d_other", "drop_created", {"title": "Real", "maker": "m", "currency": "USD", "minimum": 3, "duration_minutes": 60,
                                           "ladder": tb._eng().get(d).ladder.to_dict(), "paypal_mode": "sandbox"})
    with pytest.raises(tb.TogiError) as e:
        tb.drop_settle(d, tok, ctx=SimpleNamespace(approval_scope="test"))
    assert e.value.code == "test_approval_outside_test_mode" and "did not come from this fake" in e.value.message
    assert tb._eng().get(d).state == "closed"
    assert tb.drop_settle(d, tok, ctx=SimpleNamespace(approval_scope="default"))["outcome"] == "funded"   # a person's own yes still works


def test_f10_a_context_without_the_attribute_and_no_context_at_all_still_work(monkeypatch, tmp_path):
    d, tok = settled_ready(monkeypatch, tmp_path)
    assert tb.drop_settle(d, tok, ctx=SimpleNamespace())["outcome"] == "funded"
    d2 = tb.drop_create(title="Mugs 2", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d2)
    tb.sim_join(d2, 3)
    tb.drop_close(d2)
    assert tb.drop_settle(d2, tb.drop_plan(d2)["expect"])["outcome"] == "funded"        # ctx=None


def test_f10_the_settle_command_declares_the_context_parameter_togi_injects():
    import inspect
    assert "ctx" in inspect.signature(tb.drop_settle).parameters


# ===== the test tooling never touches the person's own approver ===========================================================

def test_the_test_helpers_only_ever_use_a_throwaway_approver(tmp_path):
    from togi_helpers import approver_home_for, env_for, real_approver_dir
    e = env_for(tmp_path / "home", tmp_path / "crowd.db")
    assert e["TOGI_APPROVER_HOME"] and e["TOGI_APPROVER_HOME"] != str(real_approver_dir())
    assert e["TOGI_APPROVER_HOME"].startswith(str(tmp_path))
    assert approver_home_for(tmp_path / "home") != real_approver_dir()
    e2 = env_for(tmp_path / "home", tmp_path / "crowd.db", TOGI_APPROVER_HOME=None)         # cannot be removed
    assert e2["TOGI_APPROVER_HOME"] == e["TOGI_APPROVER_HOME"]


def test_the_session_guard_notices_an_approver_that_appears_or_changes(tmp_path):
    from togi_helpers import REAL_APPROVER_FILES, approver_files_touched, real_approver_snapshot
    assert real_approver_snapshot(tmp_path) == {}
    before = real_approver_snapshot(tmp_path)
    (tmp_path / "approver").write_text("x")
    after = real_approver_snapshot(tmp_path)
    assert approver_files_touched(before, after) == ["approver"]
    assert approver_files_touched(after, after) == []
    (tmp_path / "approver").write_text("changed!")
    assert approver_files_touched(after, real_approver_snapshot(tmp_path)) == ["approver"]
    assert approver_files_touched(real_approver_snapshot(tmp_path), {}) == ["approver"]
    assert set(REAL_APPROVER_FILES) == {"approver", "approver.key", "approver.state"}
