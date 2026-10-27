import json
from decimal import Decimal

import pytest

from conftest import LADDER, join_many
from crowd import ladder as lad
from crowd import paypal_holds as P
from crowd.engine import CrowdError, Engine

D = Decimal


def code(fn, *a, **kw):
    with pytest.raises(CrowdError) as e:
        fn(*a, **kw)
    return e.value.code


# ---- creating and publishing ---------------------------------------------------------------------

def test_create_validates_title_minimum_duration_and_costs(engine):
    ok = lad.parse(LADDER)
    assert code(engine.create_drop, "  ", ok, 3, 60) == "bad_title"
    assert code(engine.create_drop, "x", ok, 4, 60) == "minimum_mismatch"
    assert code(engine.create_drop, "x", ok, 3, 0) == "duration_invalid"
    assert code(engine.create_drop, "x", ok, 3, 72 * 60 + 1) == "duration_too_long"      # PayPal honours 3 days
    assert code(engine.create_drop, "x", ok, 3, 60, currency="dollars") == "bad_currency"
    assert code(engine.create_drop, "x", "3:40,5:41", 3, 60) == "bad_ladder"
    assert code(engine.create_drop, "x", ok, 3, 60, costs={"unit_cost": 39, "fixed_cost": 0}) == "ladder_loses_money"
    d = engine.create_drop("x", ok, 3, 72 * 60, costs={"unit_cost": 9, "fixed_cost": 20})          # exactly 72 h is fine
    assert engine.get(d).state == "draft"


def test_publish_starts_the_clock_once(engine, clock):
    d = engine.create_drop("Mugs", lad.parse(LADDER), 3, 90)
    assert code(engine.join, d, "A") == "drop_not_published"
    v = engine.publish(d)
    assert v["state"] == "live" and v["time_left"] == "1h 30m" and v["ends_at"].startswith("2026-11-01T13:30:00")
    assert code(engine.publish, d) == "not_draft"
    assert code(engine.publish, "d_nope") == "drop_not_found"


# ---- joining: the price falls, steps lock ----------------------------------------------------------

def test_join_holds_the_start_price_and_reports_the_next_step(engine, drop, fake):
    r = engine.join(drop, "Ada Lovelace")
    assert r["hold"] == "40.00" and r["price"] == "40.00" and r["buyers"] == 1 and r["buyer"] == "ada-lovelace"
    assert r["next_message"] == "2 more buyers to reach the minimum of 3"
    assert fake.get(r["auth_id"])["amount"] == "40.00"                    # the hold is the TOP of the ladder


def test_the_price_steps_down_for_everyone_and_the_step_locks(engine, drop):
    prices = [engine.join(drop, f"B{i}")["price"] for i in range(1, 9)]
    assert prices == ["40.00", "40.00", "40.00", "40.00", "36.00", "36.00", "33.00", "33.00"]
    v = engine.get(drop)
    assert v.locked_step == 2 and v.price() == D("33.00")
    locks = [e["payload"]["step"] for e in engine.store.events(drop) if e["type"] == "step_locked"]
    assert locks == [0, 1, 2]
    r = engine.join(drop, "Late")
    assert r["next_step"] is None and r["step_locked"] is None


def test_join_reports_the_step_it_just_locked(engine, drop):
    join_many(engine, drop, 4)
    r = engine.join(drop, "Fifth")
    assert r["step_locked"] == 1 and r["price"] == "36.00" and r["next_step"] == {"more": 2, "price": "33.00", "at_buyers": 7}


def test_a_locked_step_survives_a_leave(engine, drop):
    join_many(engine, drop, 5)                                            # step 1 (36.00) locked at 5
    r = engine.leave(drop, "Buyer 05")
    assert r["buyers"] == 4 and r["price"] == "36.00" and r["locked_step"] == 1      # the crowd is below 5, price stays
    r = engine.join(drop, "Newcomer")                                     # back to 5: no new lock
    assert r["price"] == "36.00" and r["step_locked"] is None
    assert [e["payload"]["step"] for e in engine.store.events(drop) if e["type"] == "step_locked"] == [0, 1]


def test_a_leave_releases_the_hold_immediately(engine, drop, fake):
    r = engine.join(drop, "Quitter")
    out = engine.leave(drop, "quitter")
    assert fake.get(r["auth_id"])["status"] == "VOIDED" and out["released"] == "40.00" and out["buyers"] == 0
    b = engine.get(drop).buyers["quitter"]
    assert b.status == "left"


def test_a_buyer_can_rejoin_after_leaving_with_a_fresh_hold(engine, drop, fake):
    first = engine.join(drop, "Boomerang")
    engine.leave(drop, "Boomerang")
    second = engine.join(drop, "Boomerang")
    assert second["auth_id"] != first["auth_id"] and fake.get(second["auth_id"])["status"] == "CREATED"
    rids = [e["payload"]["request_id"] for e in engine.store.events(drop) if e["type"] == "hold_placed"]
    assert rids == [f"crowd-{drop}-boomerang-authorize", f"crowd-{drop}-boomerang-authorize2"]


# ---- the state-machine guards ---------------------------------------------------------------------------

def test_cannot_join_twice(engine, drop):
    engine.join(drop, "Ada")
    assert code(engine.join, drop, "ada") == "already_joined"
    assert engine.get(drop).count == 1


def test_bad_join_input(engine, drop):
    assert code(engine.join, drop, "   ") == "bad_buyer"
    assert code(engine.join, drop, "x" * 61) == "bad_buyer"
    assert code(engine.join, drop, "Ada", card_index=99) == "bad_card"
    assert engine.get(drop).count == 0


def test_no_join_after_the_clock_runs_out(engine, drop, clock):
    engine.join(drop, "Early")
    clock.advance(minutes=61)
    assert code(engine.join, drop, "Late") == "drop_ended"
    assert engine.view(engine.get(drop))["state"] == "ended"
    assert engine.close(drop)["state"] == "closed"
    closed = [e for e in engine.store.events(drop) if e["type"] == "drop_closed"][0]
    assert closed["payload"]["reason"] == "expired"


def test_no_join_or_leave_after_close(engine, drop):
    join_many(engine, drop, 3)
    engine.close(drop)
    assert code(engine.join, drop, "Late") == "drop_closed"
    assert code(engine.leave, drop, "Buyer 01") == "drop_not_live"


def test_close_is_idempotent_and_needs_a_live_drop(engine, drop):
    engine.close(drop)
    again = engine.close(drop)
    assert again["already_closed"] is True
    assert len([e for e in engine.store.events(drop) if e["type"] == "drop_closed"]) == 1
    d2 = engine.create_drop("draft", lad.parse(LADDER), 3, 60)
    assert code(engine.close, d2) == "drop_not_live"


def test_leave_guards(engine, drop):
    assert code(engine.leave, drop, "nobody") == "buyer_not_found"
    engine.join(drop, "Ada")
    engine.leave(drop, "Ada")
    assert code(engine.leave, drop, "Ada") == "no_active_hold"


def test_no_settle_before_close(engine, drop, clock):
    join_many(engine, drop, 3)
    assert code(engine.settle, drop) == "drop_not_closed"
    assert code(engine.plan, drop) == "drop_not_closed"
    clock.advance(minutes=120)                                            # ended but never closed: still no
    assert code(engine.settle, drop) == "drop_not_closed"


def test_a_failed_join_is_recorded_but_does_not_count(engine, drop, fake):
    fake.decline_next_authorize(1)
    assert code(engine.join, drop, "Declined Dan") == "join_declined"
    v = engine.get(drop)
    assert v.count == 0 and v.buyers["declined-dan"].status == "hold_failed"
    r = engine.join(drop, "Declined Dan", card_index=1)                  # can try again with another card
    assert r["buyers"] == 1
    rids = [e["payload"].get("request_id") for e in engine.store.events(drop) if e["type"] == "hold_placed"]
    assert rids == [f"crowd-{drop}-declined-dan-authorize2"]              # a new request id: no replayed decline


# ---- settlement -------------------------------------------------------------------------------------------

def test_funded_settlement_captures_every_hold_at_the_final_price(engine, drop, fake):
    join_many(engine, drop, 8)
    engine.leave(drop, "Buyer 08")
    engine.close(drop)
    plan = engine.plan(drop)
    assert plan["summary"] == "capture 7 holds at $33.00 = $231.00" and plan["released_each"] == "7.00"
    out = engine.settle(drop)
    assert out["outcome"] == "funded" and out["final_price"] == "33.00" and out["already_settled"] is False
    assert out["counts"] == {"holds": 8, "captured": 7, "voided": 1, "failed": 0, "left": 1}
    assert out["totals"] == {"gross": "231.00", "fees": "9.38", "net": "221.62"}      # 7 x (33.00 - 1.34)
    d = engine.get(drop)
    assert d.state == "settled"
    for b in d.buyers.values():
        if b.status == "captured":
            cap = fake.get_capture(b.capture_id)
            assert cap["amount"] == "33.00" and cap["final"] is True              # final capture at the FINAL price
            assert fake.get(b.auth_id)["status"] == "CAPTURED"
            assert b.hold_amount == D("40.00")                                    # the rest of the hold is released
        else:
            assert fake.get(b.auth_id)["status"] == "VOIDED"


def test_not_funded_settlement_voids_every_hold(engine, drop, fake):
    join_many(engine, drop, 2)                                            # minimum is 3
    engine.close(drop)
    assert engine.plan(drop)["summary"] == "void 2 holds, nobody pays"
    out = engine.settle(drop)
    assert out["outcome"] == "not_funded" and out["counts"] == {"holds": 2, "captured": 0, "voided": 2, "failed": 0, "left": 0}
    assert out["totals"] == {"gross": "0.00", "fees": "0.00", "net": "0.00"}
    assert all(a["status"] == "VOIDED" for a in json.load(open(fake.path))["auths"].values())
    assert not json.load(open(fake.path))["captures"]                       # nothing was ever charged


def test_minimum_is_judged_on_the_crowd_at_close_not_the_best_it_ever_was(engine, drop):
    join_many(engine, drop, 3)                                            # minimum reached, step 0 locked
    engine.leave(drop, "Buyer 03")
    engine.close(drop)
    assert engine.settle(drop)["outcome"] == "not_funded"


def test_funded_exactly_at_the_minimum_charges_the_start_price(engine, drop):
    join_many(engine, drop, 3)
    engine.close(drop)
    out = engine.settle(drop)
    assert out["outcome"] == "funded" and out["final_price"] == "40.00" and out["totals"]["gross"] == "120.00"


def test_hold_may_be_larger_than_the_charge_when_the_start_price_is_above_the_first_step(engine, clock):
    d = engine.create_drop("Mugs", lad.Ladder((lad.Step(3, D("36")), lad.Step(5, D("33"))), D("40")), 3, 60)
    engine.publish(d)
    r = join_many(engine, d, 3)
    assert r["hold"] == "40.00" and r["price"] == "36.00"
    engine.close(d)
    out = engine.settle(d)
    assert out["final_price"] == "36.00"


def test_settle_is_idempotent(engine, drop, fake):
    join_many(engine, drop, 5)
    engine.close(drop)
    first = engine.settle(drop)
    state_after_first = open(fake.path).read()
    again = engine.settle(drop)
    assert again["already_settled"] is True and again["counts"] == first["counts"] and again["totals"] == first["totals"]
    assert open(fake.path).read() == state_after_first                   # no PayPal call changed anything
    assert len([e for e in engine.store.events(drop) if e["type"] == "drop_settled"]) == 1
    assert engine.plan(drop)["already_settled"] is True


class CrashAfter:
    """Wraps a holds backend and dies (like a killed process) after N successful capture/void calls."""

    def __init__(self, inner, n, after_effect=False):
        self.inner, self.n, self.after_effect = inner, n, after_effect
        self.kind = inner.kind

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _maybe(self, fn, *a, **kw):
        if self.n <= 0:
            raise KeyboardInterrupt("simulated crash")
        self.n -= 1
        out = fn(*a, **kw)
        if self.n == 0 and self.after_effect:
            self.n = -1
            raise KeyboardInterrupt("simulated crash after PayPal acted but before it was recorded")
        return out

    def capture(self, *a, **kw):
        return self._maybe(self.inner.capture, *a, **kw)

    def void(self, *a, **kw):
        return self._maybe(self.inner.void, *a, **kw)


def captures(fake):
    return json.load(open(fake.path))["captures"]


def test_settle_resumes_after_a_crash_without_double_charging(engine, drop, fake, store, clock):
    join_many(engine, drop, 6)
    engine.close(drop)
    crashing = Engine(store, CrashAfter(fake, 2), clock)
    with pytest.raises(KeyboardInterrupt):
        crashing.settle(drop)
    d = engine.get(drop)
    assert d.state == "settling" and len(captures(fake)) == 2
    assert engine.plan(drop)["holds"] == 4 and engine.plan(drop)["resuming"] is True
    out = engine.settle(drop)                                              # a normal engine picks it up
    assert out["counts"]["captured"] == 6 and len(captures(fake)) == 6
    assert out["final_price"] == "36.00" and out["totals"]["gross"] == "216.00"
    assert engine.settle(drop)["already_settled"] is True


def test_settle_resumes_when_paypal_acted_but_the_engine_never_recorded_it(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    crashing = Engine(store, CrashAfter(fake, 2, after_effect=True), clock)
    with pytest.raises(KeyboardInterrupt):
        crashing.settle(drop)
    assert len(captures(fake)) == 2 and sum(1 for e in engine.store.events(drop) if e["type"] == "hold_captured") == 1
    out = engine.settle(drop)                                              # same request id: PayPal returns the same capture
    assert out["counts"]["captured"] == 4 and len(captures(fake)) == 4


def test_resume_recovers_a_capture_even_if_the_request_id_is_forgotten(engine, drop, fake, store, clock):
    join_many(engine, drop, 3)
    engine.close(drop)
    with pytest.raises(KeyboardInterrupt):
        Engine(store, CrashAfter(fake, 1, after_effect=True), clock).settle(drop)
    st = json.load(open(fake.path))
    st["requests"] = {k: v for k, v in st["requests"].items() if "capture" not in k}   # PayPal's memory of the call expired
    json.dump(st, open(fake.path, "w"))
    out = engine.settle(drop)
    assert out["counts"] == {"holds": 3, "captured": 3, "voided": 0, "failed": 0, "left": 0}
    assert len(captures(fake)) == 3                                        # recovered by order lookup, charged once


def test_not_funded_settlement_also_resumes(engine, drop, fake, store, clock):
    join_many(engine, drop, 2)
    engine.close(drop)
    with pytest.raises(KeyboardInterrupt):
        Engine(store, CrashAfter(fake, 1), clock).settle(drop)
    out = engine.settle(drop)
    assert out["outcome"] == "not_funded" and out["counts"]["voided"] == 2


def test_two_settles_cannot_run_at_once(engine, drop):
    join_many(engine, drop, 3)
    engine.close(drop)
    with engine.store.lock(drop):                                         # another process is mid-settle
        from crowd.store import StoreError
        with pytest.raises(StoreError) as e:
            engine.settle(drop)
        assert e.value.code == "drop_busy"


# ---- a capture fails at the end (flow E) -------------------------------------------------------------------

def test_a_failed_capture_excludes_that_buyer_and_keeps_the_price_for_everyone_else(engine, drop, fake):
    join_many(engine, drop, 5)                                            # price 36.00, locked
    engine.join(drop, "Card Nine", card_index=P.DECLINE_CARD_INDEX)        # the 6th buyer's capture will be declined
    engine.close(drop)
    out = engine.settle(drop)
    assert out["outcome"] == "funded" and out["final_price"] == "36.00"    # not re-priced by the failure
    assert out["counts"] == {"holds": 6, "captured": 5, "voided": 0, "failed": 1, "left": 0}
    assert out["totals"]["gross"] == "180.00" and out["shortfall"] == 0
    b = engine.get(drop).buyers["card-nine"]
    assert b.status == "capture_failed" and b.error == "card_declined" and b.released
    assert fake.get(b.auth_id)["status"] == "VOIDED"                       # nothing taken, and nothing left held


def test_decline_next_also_models_a_failed_capture(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    fake.decline_next(1)
    out = engine.settle(drop)
    assert out["counts"]["captured"] == 2 and out["counts"]["failed"] == 1 and out["shortfall"] == 1


def test_a_hold_voided_behind_the_engines_back_is_a_failed_capture(engine, drop, fake):
    r = join_many(engine, drop, 3)
    engine.close(drop)
    auth = engine.get(drop).buyers["buyer-01"].auth_id
    fake.void(auth, "someone-else")
    out = engine.settle(drop)
    assert out["counts"]["failed"] == 1 and engine.get(drop).buyers["buyer-01"].error == "authorization_voided"


class Flaky:
    """Capture raises a transient error for the first call."""

    def __init__(self, inner):
        self.inner, self.kind, self.boom = inner, inner.kind, True

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def capture(self, *a, **kw):
        if self.boom:
            self.boom = False
            raise P.PayPalUnavailable(message="blip")
        return self.inner.capture(*a, **kw)


def test_a_paypal_outage_leaves_settlement_resumable_not_failed(engine, drop, fake, store, clock):
    join_many(engine, drop, 3)
    engine.close(drop)
    flaky = Engine(store, Flaky(fake), clock)
    assert code(flaky.settle, drop) == "paypal_unavailable"
    assert engine.get(drop).state == "settling" and not captures(fake)
    assert engine.settle(drop)["counts"]["captured"] == 3                  # nobody was marked failed for an outage


def test_never_captures_more_than_the_hold(engine, drop):
    join_many(engine, drop, 3)
    engine.close(drop)
    d = engine.get(drop)
    assert code(engine._capture_one, d, d.buyers["buyer-01"], D("41.00")) == "capture_exceeds_hold"
    assert code(engine._capture_one, d, d.buyers["buyer-01"], D("0")) == "capture_exceeds_hold"


# ---- reading ------------------------------------------------------------------------------------------------------

def test_list_is_newest_first_and_status_has_the_headline_numbers(engine, drop, clock):
    older = drop
    clock.advance(minutes=1)
    newer = engine.create_drop("Plates", lad.parse("2:20"), 2, 30)
    assert [v["drop"] for v in engine.list()] == [newer, older]
    join_many(engine, older, 4)
    v = engine.view(engine.get(older))
    assert (v["state"], v["price"], v["buyers"], v["minimum"], v["minimum_met"], v["locked_step"]) == \
        ("live", "40.00", 4, 3, True, 0)
    assert v["next_step"] == {"more": 1, "price": "36.00", "at_buyers": 5} and v["time_left"] == "59m"


def test_buyers_list(engine, drop):
    join_many(engine, drop, 2)
    rows = engine.buyers(drop)
    assert [r["buyer"] for r in rows] == ["buyer-01", "buyer-02"] and rows[0]["held"] == "40.00" and rows[0]["status"] == "held"


def test_status_after_settlement_still_shows_the_crowd_that_closed_the_drop(engine, drop):
    join_many(engine, drop, 6)
    engine.close(drop)
    engine.settle(drop)
    v = engine.view(engine.get(drop))
    assert (v["state"], v["buyers"], v["price"], v["minimum_met"], v["outcome"]) == ("settled", 6, "36.00", True, "funded")
    assert [r["buyers"] for r in engine.list()] == [6]


def test_status_after_a_missed_minimum(engine, drop):
    join_many(engine, drop, 2)
    engine.close(drop)
    engine.settle(drop)
    v = engine.view(engine.get(drop))
    assert (v["state"], v["buyers"], v["minimum_met"], v["outcome"]) == ("settled", 2, False, "not_funded")


def test_a_failed_capture_release_resumes_after_a_crash(engine, drop, fake, monkeypatch):
    join_many(engine, drop, 5)
    engine.join(drop, "Card Nine", card_index=P.DECLINE_CARD_INDEX)
    engine.close(drop)
    real_void = engine.holds.void
    def boom(*a, **k):
        raise P.PayPalUnavailable("down")
    monkeypatch.setattr(engine.holds, "void", boom)
    with pytest.raises(Exception):
        engine.settle(drop)                                                # crashed while releasing the failed hold
    monkeypatch.setattr(engine.holds, "void", real_void)
    out = engine.settle(drop)                                              # resume: release it, then finish
    b = engine.get(drop).buyers["card-nine"]
    assert b.released and fake.get(b.auth_id)["status"] == "VOIDED" and out["counts"]["failed"] == 1
