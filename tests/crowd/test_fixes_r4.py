"""Crowd fix round 4 (docs/crowd-fixes-r4.md): what a fourth refuter found against 1f939a5, each with a test that failed before the
fix. Fake PayPal only. The principles that stand: nobody is charged twice or above the final price; nobody who saw an error on join
or asked to leave in time is charged (D-11, D-11b); nothing is inferred from our clock or from an error NAME when PayPal can be
asked; each drop only talks to the PayPal it was made with; money moves only through approved commands that refuse a throwaway
approver's yes outside test mode.

Items: 1 a buyer charged twice (an outside partial capture read as the engine's own); 2 `drop exclude` took a test approval; 3 a
leave queued behind the lock read the clock too late; 4 an error NAME trusted without asking; 5 a third retry orphaned the first
order; 6 a pending leave on an unreadable hold bricked close; 7 a drop with no recorded PayPal belonged to any PayPal; 8 a refunded
stray never cleared; 9 the lock must cover the code behind every money command."""
import dataclasses
import inspect
import json
import os
import re
import threading
import time
import tomllib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import Clock, join_many
from crowd import ladder as lad
from crowd import paypal_holds as P
from crowd import togi_bundle as tb
from crowd import verify as V
from crowd.engine import CrowdError, Engine, plan_token
from crowd.paypal_holds import FakePayPal
from crowd.store import Store, iso, parse_iso
from test_fixes_r1 import Wrap, code, edit_fake, fake_state, open_auths
from test_fixes_r2 import CallSpy, auth_of, blocked, closed_drop, evs, pp_status
from test_fixes_r3 import StandInSandbox, refused, togi_err
from togi_helpers import add_crowd, env_for, person_in_a_terminal, real_approver_snapshot, set_up_approver, togi, togi_available

D = Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def captures_on(fake, auth_id):
    return [c for c in fake_state(fake)["captures"].values() if c["auth_id"] == auth_id]


def buyers_with(engine, drop, status):
    return sorted(b.id for b in engine.get(drop).buyers.values() if b.status == status)


# ===== 1. a buyer charged twice (s1) ========================================================================================

def final_20_drop(engine, n=4, drop_id="d_s1"):
    """Minimum 3, then 20 at 4 buyers: a 40.00 hold and a 20.00 final price, so a second capture of 20 fits inside the hold and
    inside PayPal's 115% cap alike."""
    d = engine.create_drop("Mugs", "3:40,4:20", 3, 60, drop_id=drop_id)
    engine.publish(d)
    join_many(engine, d, n)
    engine.close(d)
    return d


def stop_settlement_before(engine, drop, fake, last):
    """Settlement starts and captures everyone but `last`, whose capture meets an outage (a stopped, resumable settlement)."""
    tok = plan_token(engine.plan(drop))
    fake.unavailable("capture", match=auth_of(engine, drop, last))
    assert code(engine.settle, drop, expect=tok) == "paypal_unavailable"
    return tok


def test_1_an_outside_partial_capture_during_a_stopped_settlement_is_never_captured_on_top_of(engine, fake, store, clock):
    """s1: the maker captures buyer-04's hold by hand for exactly the final price (not final: PARTIALLY_CAPTURED). The engine used
    to read it as its own lost capture, plan to capture buyer-04 again, and charge 40.00 where the price is 20.00."""
    d = final_20_drop(engine)
    stop_settlement_before(engine, d, fake, "buyer-02")                            # buyer-01 captured; buyer-02 met an outage
    dee = auth_of(engine, d, "buyer-04")                                            # settlement has not got to buyer-04
    fake.capture(dee, D("20.00"), "maker-dashboard-1", final=False)
    assert pp_status(fake, dee) == "PARTIALLY_CAPTURED"
    p = engine.plan(d)
    assert plan_token(p) == "capture:2:20.00:40.00"                                 # buyer-04 is out of the crowd, not "ours"
    assert [(c["buyer"], c["verdict"]) for c in p["changed"]] == [("buyer-04", "taken")]
    spy = CallSpy(fake)
    out = Engine(store, spy, clock).settle(d, expect=plan_token(p))
    assert [c for c in spy.calls if c == ("capture", dee)] == []                    # no capture is ever sent for a hold with money on it
    on_dee = captures_on(fake, dee)
    assert len(on_dee) == 1 and on_dee[0]["request_id"] == "maker-dashboard-1"      # buyer-04 was charged once, by the maker, 20.00
    assert out["counts"]["captured"] == 3 and buyers_with(engine, d, "voided") == ["buyer-04"]
    hv = [e for e in evs(engine, d, "hold_voided") if e["buyer"] == "buyer-04"][0]
    assert hv["taken_outside_engine"] is True and hv["capture_ids"] == [on_dee[0]["capture_id"]]
    assert not [e for e in evs(engine, d, "hold_captured") if e["buyer"] == "buyer-04"]
    v = V.verify(d, engine)                                                         # and the stray is reported for a person to refund
    assert not v["ok"] and any("refund capture " + on_dee[0]["capture_id"] in x for x in v["problems"])
    fake.refund(on_dee[0]["capture_id"])
    assert V.verify(d, engine)["ok"]


@pytest.mark.parametrize("ladder,final,clock_days", [("3:40,4:36", "36.00", 0), ("3:40,4:20", "20.00", 0), ("3:40,4:20", "20.00", 3650)])
def test_1_an_outside_partial_capture_is_taken_not_adopted_whatever_the_price_or_the_date(engine, fake, store, clock, ladder, final, clock_days):
    """s1b: the same outside capture, a price whose second capture would not fit inside the hold, and a clock far past the date PayPal
    gave the hold (the branch that used to ADOPT the outside capture as the engine's own, recording it as `hold_captured`)."""
    d = engine.create_drop("Mugs", ladder, 3, 60, drop_id="d_s1b")
    engine.publish(d)
    join_many(engine, d, 4)
    engine.close(d)
    stop_settlement_before(engine, d, fake, "buyer-02")
    dee = auth_of(engine, d, "buyer-04")
    fake.capture(dee, D(final), "outside-1", final=False)
    clock.advance(days=clock_days)
    spy = CallSpy(fake)
    Engine(store, spy, clock).settle(d, expect=plan_token(engine.plan(d)))
    assert len(captures_on(fake, dee)) == 1 and ("capture", dee) not in spy.calls
    assert [e["buyer"] for e in evs(engine, d, "hold_captured")] == ["buyer-01", "buyer-02", "buyer-03"]
    assert [e["buyer"] for e in evs(engine, d, "hold_voided")] == ["buyer-04"]


@pytest.mark.parametrize("clock_days", [0, 3650])
def test_1_an_outside_final_capture_the_engine_never_tried_to_make_is_not_ours_however_much_it_looks_like_one(engine, fake, store, clock, clock_days):
    """A hold CAPTURED once, for exactly the final price: the shape of the engine's own lost capture. But the engine has no attempt on
    record for it (settlement had not got that far), so it is somebody else's, and the date makes no difference."""
    d = final_20_drop(engine, drop_id="d_s1c")
    stop_settlement_before(engine, d, fake, "buyer-02")
    dee = auth_of(engine, d, "buyer-04")
    assert set(engine.get(d).capturing) == {"buyer-02"}                 # only the buyer the outage hit was ever tried; buyer-04 was not
    fake.capture(dee, D("20.00"), "maker-final-1", final=True)
    assert pp_status(fake, dee) == "CAPTURED"
    clock.advance(days=clock_days)
    p = engine.plan(d)
    assert [(c["buyer"], c["verdict"]) for c in p["changed"]] == [("buyer-04", "taken")]
    Engine(store, fake, clock).settle(d, expect=plan_token(p))
    assert len(captures_on(fake, dee)) == 1
    assert "buyer-04" not in [e["buyer"] for e in evs(engine, d, "hold_captured")]


@pytest.mark.parametrize("clock_days", [0, 3650])
def test_1_the_engines_own_lost_capture_is_still_recovered_not_captured_again_whatever_the_date(engine, fake, store, clock, clock_days):
    """Liveness is kept: a capture the engine started, whose reply was lost, is found at PayPal and recorded, once."""
    d = final_20_drop(engine, drop_id="d_s1d")
    tok = plan_token(engine.plan(d))
    dee = auth_of(engine, d, "buyer-04")
    fake.lose_reply("capture", match=dee)
    assert code(engine.settle, d, expect=tok) == "paypal_unavailable"
    assert engine.get(d).capturing["buyer-04"]["request_id"] == "crowd-d_s1d.buyer-04.capture"      # the attempt is on record
    clock.advance(days=clock_days)
    p = engine.plan(d)
    assert p["holds"] == 1 and not p.get("changed")                      # it still counts: it is ours
    spy = CallSpy(fake)
    out = Engine(store, spy, clock).settle(d, expect=plan_token(p))
    assert ("capture", dee) not in spy.calls and len(captures_on(fake, dee)) == 1
    assert out["counts"]["captured"] == 4 and evs(engine, d, "hold_captured")[-1]["recovered"] is True
    assert V.verify(d, engine)["ok"]


def test_1_an_outside_final_capture_at_the_final_price_after_an_attempt_that_never_reached_paypal_is_adopted_not_doubled(engine, fake, store, clock):
    """Round 5 (7a). This used to be "not ours, because the fake said a different request made it": PayPal does not say which request
    made a capture (the fake no longer does), so the engine cannot tell this capture from its own lost one. It is adopted
    (`hold_captured{recovered}`): the buyer is charged ONCE, at the final price, which is exactly what settlement would have taken."""
    d = final_20_drop(engine, drop_id="d_s1e")
    tok = plan_token(engine.plan(d))
    dee = auth_of(engine, d, "buyer-04")
    fake.unavailable("capture", match=dee)
    assert code(engine.settle, d, expect=tok) == "paypal_unavailable"
    assert "buyer-04" in engine.get(d).capturing
    fake.capture(dee, D("20.00"), "maker-final-2", final=True)
    p = engine.plan(d)
    assert p["holds"] == 1 and not p.get("changed")                                   # it still counts: the engine takes it as its own
    spy = CallSpy(fake)
    out = Engine(store, spy, clock).settle(d, expect=plan_token(p))
    assert ("capture", dee) not in spy.calls and len(captures_on(fake, dee)) == 1     # and nothing is POSTed on top of it
    assert out["counts"]["captured"] == 4 and evs(engine, d, "hold_captured")[-1]["recovered"] is True
    assert V.verify(d, engine)["ok"]


def test_1_could_be_ours_says_yes_only_to_one_final_completed_capture_at_the_final_price_that_the_engine_tried_to_make(engine, fake):
    d = final_20_drop(engine, drop_id="d_s1f")
    tok = plan_token(engine.plan(d))
    dee = auth_of(engine, d, "buyer-04")
    fake.lose_reply("capture", match=dee)
    assert code(engine.settle, d, expect=tok) == "paypal_unavailable"
    drop, b = engine.get(d), engine.get(d).buyers["buyer-04"]
    mine = fake.get_capture(captures_on(fake, dee)[0]["capture_id"])
    ours = lambda status="CAPTURED", caps=None, d_=drop: engine._could_be_ours(d_, b, status, [mine] if caps is None else caps, [mine] if caps is None else caps)   # noqa: E731
    assert ours() is True
    assert ours(status="PARTIALLY_CAPTURED") is False                                              # the engine's capture is final
    assert ours(caps=[mine, {**mine, "capture_id": "OTHER"}]) is False                             # two captures on it
    assert ours(caps=[]) is False and ours(caps=[{**mine, "amount": "19.99"}]) is False            # none, or not the final price
    assert ours(caps=[{**mine, "currency": "EUR"}]) is False and ours(caps=[{**mine, "status": "PENDING"}]) is True
    assert ours(caps=[{**mine, "final": False}]) is False                                           # said not to be a final capture
    assert "request_id" not in mine                                                                 # PayPal does not say which request made a capture
    assert engine._could_be_ours(drop, b, "CAPTURED", None, []) is True                             # cannot list captures: recovery decides
    other = engine.get(d).buyers["buyer-01"]                                                        # no attempt on record for this hold
    assert engine._could_be_ours(drop, other, "CAPTURED", [mine], [mine]) is False
    assert engine._could_be_ours(dataclasses.replace(drop, settlement=None), b, "CAPTURED", [mine], [mine]) is False   # settlement not started
    missed = dataclasses.replace(drop, settlement={**drop.settlement, "outcome": "not_funded"})
    assert engine._could_be_ours(missed, b, "CAPTURED", [mine], [mine]) is False                    # a missed minimum captures nothing


def test_1_a_capture_that_lands_between_the_read_and_the_post_is_looked_at_again_and_the_one_final_capture_is_the_hold_s(engine, fake, store, clock):
    """PayPal answers the engine's capture with ALREADY_CAPTURED (a FINAL capture of exactly the final price slipped in after the read):
    the hold is looked at again. With the attempt on record, one final capture at the final price is all the engine can tell from its
    own (PayPal does not say which request made it): the buyer is charged once, at the final price, and it is recorded as the engine's.
    (A capture that is not that, a partial or another amount, still leaves the crowd: test_1_an_outside_partial_capture_*.)"""
    d = final_20_drop(engine, drop_id="d_s1g")
    tok = plan_token(engine.plan(d))
    dee = auth_of(engine, d, "buyer-04")

    class Slips(Wrap):
        def capture(self, auth_id, *a, **k):
            if auth_id == dee:
                self.inner.capture(auth_id, D("20.00"), "maker-slipped-in", final=True)
            return self.inner.capture(auth_id, *a, **k)

    out = Engine(store, Slips(fake), clock).settle(d, expect=tok)
    assert len(captures_on(fake, dee)) == 1 and captures_on(fake, dee)[0]["request_id"] == "maker-slipped-in"     # (the fake's own record)
    assert out["counts"]["captured"] == 4 and evs(engine, d, "hold_captured")[-1]["recovered"] is True


def test_1_the_fake_enforces_paypals_capture_cap_and_a_partial_capture_leaves_the_hold_partially_captured(fake):
    h = fake.authorize(D("40"), "USD", "d:x", "r-auth")
    a = h["auth_id"]
    assert "request_id" not in fake.capture(a, D("20"), "c1", final=False)             # PayPal does not say which request made a capture
    assert pp_status(fake, a) == "PARTIALLY_CAPTURED"
    fake.capture(a, D("20"), "c2", final=False)                                      # 40 in all: the whole hold, still not final
    assert pp_status(fake, a) == "PARTIALLY_CAPTURED"
    fake.capture(a, D("6.00"), "c3", final=False)                                    # 46 = 115% of 40
    with pytest.raises(P.MaxCaptureAmountExceeded):
        fake.capture(a, D("0.01"), "c4", final=False)                                # one cent more than the cap allows
    h2 = fake.authorize(D("40"), "USD", "d:y", "r-auth-2")
    with pytest.raises(P.MaxCaptureAmountExceeded):
        fake.capture(h2["auth_id"], D("46.01"), "c5")                                # nor in one capture
    fake.capture(h2["auth_id"], D("46.00"), "c6", final=True)
    assert pp_status(fake, h2["auth_id"]) == "CAPTURED"
    with pytest.raises(P.AuthorizationAlreadyCaptured):
        fake.capture(h2["auth_id"], D("1"), "c7")
    assert "request_id" not in fake.get_capture(captures_on(fake, a)[0]["capture_id"])
    assert captures_on(fake, a)[0]["request_id"] == "c1"                              # (the fake's own bookkeeping, not what it answers)


# ===== 2. `drop exclude` took a throwaway approver's yes outside test mode (s2) ==============================================

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    return tmp_path


def bundle_drop(joins=4, ladder="3:40,5:36", close=True):
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder=ladder)["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, joins)
    if close:
        tb.drop_close(d)
    return d


def test_2_exclude_refuses_a_test_scope_approval_outside_test_mode_and_nothing_happens(env, monkeypatch):
    d = bundle_drop()
    eng = tb._eng()
    before = len(eng.store.events())
    monkeypatch.delenv("CROWD_ENV")
    e = togi_err(tb.drop_exclude, d, "sim-001", ctx=SimpleNamespace(approval_scope="test"))
    assert e.code == "test_approval_outside_test_mode" and e.status == "refused" and "CROWD_ENV" in e.message
    assert "drop exclude" in e.message and "{" not in e.message + (e.next or "")
    assert len(eng.store.events()) == before and open_auths(eng.holds).keys() == {a.auth_id for a in eng.get(d).buyers.values()}
    # a person's own approval still goes through, and so does a Togi that reports no scope
    assert tb.drop_exclude(d, "sim-001", ctx=SimpleNamespace(approval_scope="default"))["excluded"] is True
    assert tb.drop_exclude(d, "sim-002", ctx=SimpleNamespace())["excluded"] is True
    assert tb.drop_exclude(d, "sim-003")["excluded"] is True


def test_2_exclude_takes_a_test_approval_in_test_mode_on_a_store_of_this_fakes_holds_only(env):
    d = bundle_drop()
    assert tb.drop_exclude(d, "sim-001", ctx=SimpleNamespace(approval_scope="test"))["excluded"] is True


def test_2_exclude_also_refuses_when_the_store_holds_anything_that_is_not_this_fakes(env):
    d = bundle_drop()
    Store(str(env / "crowd.db")).append("d_other", "drop_created", {
        "title": "x", "maker": "m", "currency": "USD", "minimum": 3, "duration_minutes": 60, "ladder": tb._eng().get(d).ladder.to_dict(),
        "paypal_mode": "sandbox"})
    e = togi_err(tb.drop_exclude, d, "sim-001", ctx=SimpleNamespace(approval_scope="test"))
    assert e.code == "test_approval_outside_test_mode" and "did not come from this fake" in e.message


def gated_commands():
    """Every command the manifest gates behind a person's approval: whatever `[policy] approve_effects` lists."""
    with open(os.path.join(ROOT, "togi.toml"), "rb") as f:
        gated_effects = set(tomllib.load(f)["policy"]["approve_effects"])
    return sorted(w for w, e in tb.EFFECTS.items() if e in gated_effects)


def test_2_the_manifest_gates_exactly_the_money_commands_and_there_are_some():
    assert gated_commands() == ["drop bind", "drop exclude", "drop settle"]


@pytest.mark.parametrize("words", gated_commands())
def test_2_every_gated_command_takes_the_approval_context_and_checks_its_scope_first(env, monkeypatch, words):
    """Togi's `test_approvals = true` is only safe if no gated command forgets the scope check. Each one is called, with a throwaway
    approver's context, outside test mode, on a drop that does not even exist: the only way to get `test_approval_outside_test_mode`
    (and not `drop_not_found`) is to check the scope BEFORE reading or writing anything."""
    fn = tb.COMMANDS[words]
    params = inspect.signature(fn).parameters
    assert "ctx" in params, f"{words} cannot see which approver signed its approval"
    monkeypatch.delenv("CROWD_ENV")
    kwargs = {n: ("d_nothing" if n == "drop" else "x") for n, p in params.items() if n != "ctx" and p.default is inspect.Parameter.empty}
    e = togi_err(fn, **kwargs, ctx=SimpleNamespace(approval_scope="test"))
    assert e.code == "test_approval_outside_test_mode", (words, e.code)
    assert not os.path.exists(env / "crowd.db") or not Store(str(env / "crowd.db")).events()      # and nothing was written


def test_2_the_gated_functions_all_call_the_scope_check(env):
    src = inspect.getsource(tb)
    for words in gated_commands():
        fn = inspect.unwrap(tb.COMMANDS[words])
        assert "_refuse_test_scope_approval(" in inspect.getsource(fn), words
    assert src.count("_refuse_test_scope_approval(") >= len(gated_commands()) + 1       # the definition and a call per gated command


@pytest.mark.skipif(not togi_available(), reason="togi is not installed")
def test_2_through_the_host_a_throwaway_approvers_yes_does_not_run_exclude_outside_test_mode(tmp_path):
    """s2, end to end: the host runs the command on a throwaway approver's approval; the command itself says no."""
    before = real_approver_snapshot()
    env = env_for(tmp_path / "home", tmp_path / "crowd.db")
    add_crowd(env, tmp_path)
    set_up_approver(tmp_path / "home")

    def run(*a):
        return togi(env, "crowd", *a)

    for argv in (("drop", "create", "--drop-id", "d_s2", "--title", "Mugs", "--minimum", "3", "--hours", "1", "--ladder", "3:40,5:36"),
                 ("drop", "publish", "--drop", "d_s2"), ("sim", "join", "--drop", "d_s2", "--count", "4"),
                 ("drop", "close", "--drop", "d_s2")):
        assert run(*argv)[0] == 0, argv
    outside = {k: v for k, v in env.items() if k != "CROWD_ENV"}           # the same host, no longer in test mode
    code_, out = togi(outside, "crowd", "drop", "exclude", "--drop", "d_s2", "--buyer", "sim-001")
    assert code_ == 3 and out["status"] == "needs_approval", out
    ap = out["approval"]["id"]
    assert person_in_a_terminal(outside, ["approve", ap], ["d_s2"])[0] == 0
    code_, out = togi(outside, "crowd", "drop", "exclude", "--drop", "d_s2", "--buyer", "sim-001", "--approval", ap)
    assert code_ == 2 and out["error"]["code"] == "test_approval_outside_test_mode", out
    assert {a["status"] for a in json.load(open(tmp_path / "crowd.db.fakepaypal.json"))["auths"].values()} == {"CREATED"}
    assert real_approver_snapshot() == before


# ===== 3. a leave (or join) queued behind the lock read the clock too late (s5) ==========================================

class CountingClock:
    """The test clock, counting how often it is read (a request reads it once, when it is taken)."""

    def __init__(self, inner):
        self.inner, self.reads = inner, 0

    def __call__(self):
        self.reads += 1
        return self.inner()

    def advance(self, **kw):
        self.inner.advance(**kw)


def wait_for(cond, timeout=1.5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def queued_behind_the_lock(store, drop, clock, ends, call):
    """Run `call` in a thread while the test holds the drop's lock (another request on a slow PayPal). It is asked five seconds before
    the deadline, and the deadline passes while it waits. Returns what the call returned or raised."""
    clock.inner.t = ends - timedelta(seconds=5)
    got = {}

    def work():
        try:
            got["ok"] = call()
        except CrowdError as e:
            got["err"] = e.code

    with store.lock(drop):
        t = threading.Thread(target=work)
        t.start()
        wait_for(lambda: clock.reads >= 1)          # the request has been taken (post-fix: it read the clock before the lock)
        time.sleep(0.2)
        clock.advance(seconds=20)                   # the deadline passes while it waits
    t.join(15)
    assert not t.is_alive()
    return got


def test_3_a_leave_asked_five_seconds_before_the_deadline_stands_however_long_it_waits_for_the_lock(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    ends = parse_iso(engine.get(drop).ends_at)
    counting = CountingClock(clock)
    eng = Engine(store, fake, counting)
    got = queued_behind_the_lock(store, drop, counting, ends, lambda: eng.leave(drop, "buyer-04"))
    assert "ok" in got, got
    started = [e for e in store.events(drop) if e["type"] == "leave_started"]
    assert len(started) == 1 and parse_iso(started[0]["ts"]) < ends                     # stamped when it was asked
    eng.close(drop)
    out = eng.settle(drop, expect=plan_token(eng.plan(drop)))
    assert out["counts"]["captured"] == 3 and buyers_with(engine, drop, "left") == ["buyer-04"]    # not charged
    assert pp_status(fake, auth_of(engine, drop, "buyer-04")) == "VOIDED" and V.verify(drop, engine)["ok"]


def test_3_a_join_asked_in_time_stands_too_and_counts_in_the_crowd_at_the_deadline(engine, drop, fake, store, clock):
    join_many(engine, drop, 2)
    ends = parse_iso(engine.get(drop).ends_at)
    counting = CountingClock(clock)
    eng = Engine(store, fake, counting)
    got = queued_behind_the_lock(store, drop, counting, ends, lambda: eng.join(drop, "Late Larry"))
    assert "ok" in got, got
    placed = [e for e in store.events(drop) if e["type"] == "hold_placed" and e["payload"]["buyer"] == "late-larry"]
    assert placed and parse_iso(placed[0]["ts"]) < ends
    closed = eng.close(drop)
    assert closed["buyers"] == 3 and closed["minimum_met"] is True


def test_3_a_request_asked_after_the_deadline_is_still_refused_whatever_it_waits_for(engine, drop, fake, store, clock):
    join_many(engine, drop, 3)
    clock.advance(minutes=61)
    assert code(engine.leave, drop, "buyer-03") == "drop_ended" and code(engine.join, drop, "Late") == "drop_ended"


# ===== 4. AUTHORIZATION_EXPIRED trusted without asking (s4) =================================================================

def test_4_the_name_authorization_expired_does_not_exempt_a_refusal_paypal_contradicts(engine, drop, fake):
    """s4: every capture answers AUTHORIZATION_EXPIRED while GET says CREATED. The name was exempt from the breaker, so settle never
    stopped: every hold of a funded drop was recorded as failed and then RELEASED, irreversibly, and verify passed."""
    join_many(engine, drop, 8)
    engine.close(drop)
    tok = plan_token(engine.plan(drop))
    fake.refuse("capture", "AUTHORIZATION_EXPIRED", times=None)
    e = blocked(engine.settle, drop, expect=tok)
    assert e.ctx["issue"] == "AUTHORIZATION_EXPIRED" and "person" in e.ctx["next"]
    failed = evs(engine, drop, "capture_failed")
    assert len(failed) == 2 and all(f.get("paypal_status") == "CREATED" for f in failed)      # PayPal's own answer is on record
    assert not evs(engine, drop, "failed_hold_released", "hold_voided")                         # nothing was released
    assert {a["status"] for a in fake_state(fake)["auths"].values()} == {"CREATED"}
    assert engine.get(drop).state == "settling" and not V.verify(drop, engine)["ok"]


def test_4_a_name_with_no_answer_from_paypal_counts_too(engine, drop, fake, store, clock):
    """PayPal will not say (the read is refused): the name is still only a claim, so the failure counts."""
    join_many(engine, drop, 4)
    engine.close(drop)
    tok = plan_token(engine.plan(drop))

    class ReadsRefusedAfterARefusal(Wrap):
        armed = False

        def capture(self, *a, **k):
            try:
                return self.inner.capture(*a, **k)
            finally:
                self.armed = True

        def get(self, auth_id):
            if self.armed:
                self.armed = False
                raise P._error_from(403, {"name": "NOT_AUTHORIZED", "details": [{"issue": "PERMISSION_DENIED"}], "debug_id": "D"})
            return self.inner.get(auth_id)

    fake.refuse("capture", "AUTHORIZATION_EXPIRED", times=None)
    e = blocked(Engine(store, ReadsRefusedAfterARefusal(fake), clock).settle, drop, expect=tok)
    assert all("paypal_status" not in f for f in evs(engine, drop, "capture_failed")) and len(evs(engine, drop, "capture_failed")) == 2
    assert e.ctx["issue"] in ("AUTHORIZATION_EXPIRED", "captures_failing_in_a_row")


def test_4_the_breaker_counts_the_name_only_when_the_event_says_paypal_said_expired(engine, drop):
    closed_drop(engine, drop, 4)
    by = lambda n: auth_of(engine, drop, n)                                                       # noqa: E731
    for n in ("buyer-01", "buyer-02", "buyer-03"):                  # PayPal, asked, said EXPIRED: facts about three holds
        engine.store.append(drop, "capture_failed", {"buyer": n, "auth_id": by(n), "code": "authorization_expired",
                                                    "issue": "AUTHORIZATION_EXPIRED", "paypal_status": "EXPIRED"})
    engine._check_breaker(engine.get(drop))                                                       # no block
    engine.store.append(drop, "capture_failed", {"buyer": "buyer-04", "auth_id": by("buyer-04"), "code": "authorization_expired",
                                                "issue": "AUTHORIZATION_EXPIRED", "paypal_status": "CREATED"})
    engine.store.append(drop, "capture_failed", {"buyer": "buyer-01", "auth_id": by("buyer-01"), "code": "authorization_expired",
                                                "issue": "AUTHORIZATION_EXPIRED"})
    with pytest.raises(CrowdError) as e:                                                          # the name alone, twice: it stops
        engine._check_breaker(engine.get(drop))
    assert e.value.code == "settle_blocked"
    # and the engine's own hold_expired is only a fact when it carries PayPal's EXPIRED too
    engine.store.append(drop, "settle_blocked", {"phase": "capture", "code": "x"})
    engine.store.append(drop, "capture_failed", {"buyer": "buyer-02", "auth_id": by("buyer-02"), "code": "hold_expired"})
    engine.store.append(drop, "capture_failed", {"buyer": "buyer-03", "auth_id": by("buyer-03"), "code": "hold_expired"})
    with pytest.raises(CrowdError):
        engine._check_breaker(engine.get(drop))


# ===== 5. a third retry orphaned the first order (s3) =======================================================================

def lose_two_orders(engine, drop, fake, name="dee"):
    """One join intent makes order O1 (hold H1, reply lost); PayPal forgets the request id; the retry makes O2 (hold H2), reply lost
    too; PayPal now remembers the id, so the third try replays O2."""
    fake.lose_reply("authorize", match=f":{name}")
    assert code(engine.join, drop, name) == "paypal_unavailable"
    rid = next(iter(engine.get(drop).unresolved))

    def forget(s):
        s["requests"].pop(rid, None)
        s["order_rids"].pop(rid, None)

    edit_fake(fake, forget)
    fake.lose_reply("authorize", match=f":{name}")
    assert code(engine.join, drop, name) == "paypal_unavailable"
    return rid


def test_5_a_third_retry_that_replays_the_second_order_still_releases_the_first(engine, drop, fake):
    join_many(engine, drop, 3)
    rid = lose_two_orders(engine, drop, fake)
    assert len(engine.get(drop).unresolved[rid]["order_ids"]) == 2
    out = engine.join(drop, "dee")                                       # the third try: PayPal replays the second order
    assert out["buyers"] == 4
    holds = {k: a["status"] for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":dee")}
    assert sorted(holds.values()) == ["CREATED", "VOIDED"]               # the first order's hold was released, the second is the buyer's
    assert holds[auth_of(engine, drop, "dee")] == "CREATED"
    assert [e["superseded"] for e in evs(engine, drop, "orphan_hold_released")] == [True]
    engine.close(drop)
    engine.settle(drop, expect=plan_token(engine.plan(drop)))
    assert V.verify(drop, engine)["ok"] and not [a for a in open_auths(fake)]


def test_5_if_paypal_will_not_say_what_the_first_order_holds_the_new_hold_is_not_recorded(engine, drop, fake, store, clock):
    join_many(engine, drop, 3)
    rid = lose_two_orders(engine, drop, fake)
    first = engine.get(drop).unresolved[rid]["order_ids"][0]

    class WontSay(Wrap):
        def recover_join(self, request_id, amount, currency, label, order_id=None):
            if order_id == first:
                raise P._error_from(403, {"name": "NOT_AUTHORIZED", "details": [{"issue": "PERMISSION_DENIED"}]})
            return self.inner.recover_join(request_id, amount, currency, label, order_id=order_id)

    assert code(Engine(store, WontSay(fake), clock).join, drop, "dee") == "earlier_attempt_unresolved"
    assert engine.get(drop).buyers.get("dee") is None or engine.get(drop).buyers["dee"].status != "held"
    assert engine.get(drop).count == 3


# ===== 6. a leave pending on an unreadable hold bricked close (s6) ==========================================================

class RefusesReadsOf(Wrap):
    """PayPal refuses to void or to describe one hold, with the given status and body."""

    def __init__(self, inner, auth_id, http, body):
        super().__init__(inner)
        self.auth_id, self.http, self.body = auth_id, http, {**body, "debug_id": "DBG"}

    def void(self, a, rid):
        if a == self.auth_id:
            raise P._error_from(self.http, self.body)
        return self.inner.void(a, rid)

    def get(self, a):
        if a == self.auth_id:
            raise P._error_from(self.http, self.body)
        return self.inner.get(a)


NOT_FOUND = (404, {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})
FORBIDDEN = (403, {"name": "NOT_AUTHORIZED", "details": [{"issue": "PERMISSION_DENIED"}]})


def test_6_a_pending_leave_whose_hold_paypal_says_does_not_exist_is_completed_at_close(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    dee = auth_of(engine, drop, "buyer-04")
    flaky = Engine(store, RefusesReadsOf(fake, dee, *NOT_FOUND), clock)
    assert code(flaky.leave, drop, "buyer-04") == "leave_pending"
    out = flaky.close(drop)                                              # no bare paypal_error: a 404 means nothing is held
    assert out["state"] == "closed" and out["reconciled"]["leaves_completed"] == ["buyer-04"] and out["buyers"] == 3
    hv = [e for e in evs(engine, drop, "hold_voided") if e["buyer"] == "buyer-04"][0]
    assert hv["reason"] == "left" and hv["paypal_status"] == "NOT_FOUND"
    assert buyers_with(engine, drop, "left") == ["buyer-04"]


def test_6_a_pending_leave_whose_hold_paypal_will_not_describe_stops_close_with_the_way_out(env, engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    dee = auth_of(engine, drop, "buyer-04")
    flaky = Engine(store, RefusesReadsOf(fake, dee, *FORBIDDEN), clock)
    assert code(flaky.leave, drop, "buyer-04") == "leave_pending"
    before = len(store.events(drop))
    e = refused(flaky.close, drop)
    assert e.code == "hold_unreadable" and e.ctx["buyer"] == "buyer-04" and e.ctx["auth_id"] == dee and e.ctx["issue"] == "PERMISSION_DENIED"
    assert f"drop exclude --drop {drop} --buyer buyer-04" in e.ctx["next"] and f"void hold {dee}" in e.ctx["next"]
    assert len(store.events(drop)) == before and flaky.get(drop).state == "live"       # nothing written, drop still live
    assert code(flaky.reconcile, drop) == "hold_unreadable"                              # and the same for the public reconcile
    msg = tb._togi_error(e.code, {**e.ctx, "drop": drop, "reason": str(e)})
    assert "exclude" in msg.next and msg.code == "hold_unreadable" and "{" not in msg.message + msg.next
    flaky.exclude(drop, "buyer-04")                                                      # the way out works
    assert flaky.close(drop)["state"] == "closed"


# ===== 7. a drop that recorded no PayPal belonged to any PayPal (s7) =======================================================

def old_drop(store, fake, clock, drop_id="d_old", n=3, closed=False, real_holds=True):
    """A drop written before drops recorded a PayPal: no event carries `paypal_mode`. With real_holds its holds exist at `fake`;
    without, they are ids no fake has (as a sandbox drop's are, to a fake)."""
    lg = lad.parse("3:40,5:36")
    store.append(drop_id, "drop_created", {"title": "Old drop", "maker": "m", "currency": "USD", "minimum": 3, "duration_minutes": 600,
                                          "ladder": lg.to_dict(), "costs": None}, ts=clock())
    store.append(drop_id, "drop_published", {"ends_at": iso(clock() + timedelta(hours=10))}, ts=clock())
    for i in range(n):
        name = f"b{i}"
        if real_holds:
            h = fake.authorize(lg.start_price, "USD", f"{drop_id}:{name}", f"crowd-{drop_id}-{name}-authorize")
        else:
            h = {"order_id": f"SBXO{i}", "auth_id": f"SBXA{i}", "expires": None}
        store.append_many(drop_id, [("buyer_joined", {"buyer": name, "name": name, "card_index": 0}),
                                    ("hold_placed", {"buyer": name, "name": name, "order_id": h["order_id"], "auth_id": h["auth_id"],
                                                     "amount": "40.00", "currency": "USD", "status": "CREATED", "expires": h["expires"],
                                                     "attempt": 1, "request_id": f"crowd-{drop_id}-{name}-authorize"})], ts=clock())
    if closed:
        store.append(drop_id, "drop_closed", {"reason": "manual", "count": n, "funded": n >= 3, "price": "40.00",
                                              "as_of": iso(clock())}, ts=clock())
    return drop_id


UNBOUND_OPS = {
    "join": lambda e, d: e.join(d, "Late"), "leave": lambda e, d: e.leave(d, "b0"), "publish": lambda e, d: e.publish(d),
    "close": lambda e, d: e.close(d), "reconcile": lambda e, d: e.reconcile(d), "exclude": lambda e, d: e.exclude(d, "b0"),
    "verify": lambda e, d: V.verify(d, e), "tieout": lambda e, d: V.tieout(d, e), "tieout_program": lambda e, d: V.tieout_program(d, e),
}


@pytest.mark.parametrize("op", sorted(UNBOUND_OPS))
def test_7_a_drop_that_recorded_no_paypal_belongs_to_none_until_a_person_binds_it(engine, fake, store, clock, op):
    d = old_drop(store, fake, clock)
    assert engine.recorded_paypals(d) == set()
    before = len(store.events())
    e = refused(UNBOUND_OPS[op], engine, d)
    assert e.code == "drop_unbound" and e.ctx["drop"] == d
    assert len(store.events()) == before and {a["status"] for a in fake_state(fake)["auths"].values()} == {"CREATED"}


@pytest.mark.parametrize("op", ["plan", "settle"])
def test_7_plan_and_settle_refuse_an_unbound_closed_drop_too(engine, fake, store, clock, op):
    d = old_drop(store, fake, clock, closed=True)
    before = len(store.events())
    assert refused(lambda: getattr(engine, op)(d)).code == "drop_unbound"
    assert len(store.events()) == before


def test_7_s7_a_fake_can_no_longer_close_a_sandbox_drop_whose_holds_it_cannot_see(engine, fake, store, clock):
    """The refuter's reproduction: the fake used to read every real hold as NOT_FOUND and close the drop unfunded."""
    d = old_drop(store, fake, clock, real_holds=False)
    assert refused(engine.close, d).code == "drop_unbound"
    stand_in = Engine(store, StandInSandbox(fake), clock)
    assert refused(stand_in.plan, d).code in ("drop_unbound", "drop_not_closed")
    assert engine.get(d).state == "live" and engine.get(d).closed is None


def test_7_binding_shows_the_holds_checks_them_at_this_paypal_and_records_its_mode_and_ref(engine, fake, store, clock):
    d = old_drop(store, fake, clock)
    p = engine.bind_plan(d)
    assert [h["status"] for h in p["holds"]] == ["CREATED"] * 3 and p["unmatched"] == [] and p["paypal"].startswith("fake ")
    assert p["expect"].startswith(f"bind:fake:{fake.ref}:3:")
    out = engine.bind(d, p["expect"])
    assert out["bound"] is True
    ev = [e for e in store.events(d) if e["type"] == "paypal_bound"]
    assert len(ev) == 1 and ev[0]["payload"]["paypal_mode"] == "fake" and ev[0]["payload"]["paypal_ref"] == fake.ref
    assert ev[0]["payload"]["holds"] == [h["auth_id"] for h in p["holds"]]
    assert engine.recorded_paypals(d) == {("fake", fake.ref)}
    assert engine.close(d)["state"] == "closed"                          # and now the drop works, through this PayPal
    assert engine.settle(d, expect=plan_token(engine.plan(d)))["outcome"] == "funded"
    assert refused(V.verify, d, Engine(store, StandInSandbox(fake), clock)).code == "wrong_paypal"  # and through no other
    assert code(engine.bind, d, p["expect"]) == "already_bound"


def test_7_a_bind_is_refused_when_this_paypal_does_not_know_the_drops_holds_and_nothing_is_written(engine, fake, store, clock, tmp_path):
    d = old_drop(store, fake, clock)
    elsewhere = Engine(store, FakePayPal(str(tmp_path / "other.fakepaypal.json")), clock)
    p = elsewhere.bind_plan(d)
    assert p["unmatched"] and {h["status"] for h in p["holds"]} == {"NOT_FOUND"}
    before = len(store.events())
    e = refused(elsewhere.bind, d, p["expect"])
    assert e.code == "bind_mismatch" and len(store.events()) == before and elsewhere.recorded_paypals(d) == set()


def test_7_a_bind_is_for_exactly_the_approved_paypal_and_holds(engine, fake, store, clock):
    d = old_drop(store, fake, clock)
    tok = engine.bind_plan(d)["expect"]
    before = len(store.events())
    e = refused(engine.bind, d, tok.replace("fake:", "sandbox:"))
    assert e.code == "bind_changed" and e.ctx["actual"] == tok and len(store.events()) == before
    assert engine.bind_plan(d)["expect"] == tok                            # the token is a function of the PayPal and the logged holds
    assert engine.bind(d, tok)["bound"] is True


def test_7_through_the_bundle_a_person_sees_the_holds_then_binds_and_a_test_approval_cannot(env):
    store = Store(str(env / "crowd.db"))
    fake = FakePayPal(str(env / "crowd.db") + ".fakepaypal.json")
    d = old_drop(store, fake, Clock())
    seen = tb.drop_holds(d)
    assert seen["bound"] is False and len(seen["holds"]) == 3 and f"--expect {seen['expect']}" in seen["next"]
    assert togi_err(tb.drop_close, d).code == "drop_unbound"
    ctx_test = SimpleNamespace(approval_scope="test")
    e = togi_err(tb.drop_bind, d, seen["expect"], ctx=ctx_test)         # an old drop's holds are not this fake's own: foreign
    assert e.code == "test_approval_outside_test_mode" and "did not come from this fake" in e.message
    assert tb._eng().recorded_paypals(d) == set()
    assert togi_err(tb.drop_bind, d, "bind:fake:x:3:00000000", ctx=SimpleNamespace(approval_scope="default")).code == "bind_changed"
    out = tb.drop_bind(d, seen["expect"], ctx=SimpleNamespace(approval_scope="default"))
    assert out["bound"] is True and out["next"].endswith(f"drop status --drop {d}")
    again = tb.drop_holds(d)
    assert again["bound"] is True and len(again["holds"]) == 3
    assert togi_err(tb.drop_bind, d, seen["expect"]).code == "already_bound"


@pytest.mark.skipif(not togi_available(), reason="togi is not installed")
def test_7_through_the_host_bind_waits_for_a_person_shows_its_holds_first_and_a_throwaway_approver_cannot_bind_an_old_drop(tmp_path):
    before = real_approver_snapshot()
    env = env_for(tmp_path / "home", tmp_path / "crowd.db")
    add_crowd(env, tmp_path)
    set_up_approver(tmp_path / "home")
    store = Store(str(tmp_path / "crowd.db"))
    old_drop(store, FakePayPal(str(tmp_path / "crowd.db") + ".fakepaypal.json"), Clock(), drop_id="d_old")
    code_, out = togi(env, "crowd", "drop", "close", "--drop", "d_old")
    assert code_ == 2 and out["error"]["code"] == "drop_unbound" and "drop holds" in out["next"]      # nothing touches PayPal for it
    code_, seen = togi(env, "crowd", "drop", "holds", "--drop", "d_old")
    assert code_ == 0 and seen["data"]["bound"] is False and len(seen["data"]["holds"]) == 3
    token = seen["data"]["expect"]
    code_, out = togi(env, "crowd", "drop", "bind", "--drop", "d_old", "--expect", token)
    assert code_ == 3 and out["status"] == "needs_approval" and out["approval"]["args"] == {"drop": "d_old", "expect": token}
    ap = out["approval"]["id"]
    assert person_in_a_terminal(env, ["approve", ap], ["d_old"])[0] == 0
    code_, out = togi(env, "crowd", "drop", "bind", "--drop", "d_old", "--expect", token, "--approval", ap)
    assert code_ == 2 and out["error"]["code"] == "test_approval_outside_test_mode" and "did not come from this fake" in out["error"]["message"]
    assert [e["type"] for e in store.events("d_old")].count("paypal_bound") == 0
    assert real_approver_snapshot() == before


def test_7_the_bundle_says_do_not_bind_when_the_paypal_in_use_does_not_know_the_holds(env):
    store = Store(str(env / "crowd.db"))
    d = old_drop(store, FakePayPal(str(env / "elsewhere.json")), Clock(), real_holds=False)
    seen = tb.drop_holds(d)
    assert seen["unmatched"] and "do NOT bind" in seen["next"] and "--expect" not in seen["next"]
    assert togi_err(tb.drop_bind, d, seen["expect"], ctx=SimpleNamespace(approval_scope="default")).code == "bind_mismatch"


def test_7_tieout_asks_which_paypal_first(engine, fake, store, clock):
    """`tieout` read captures through whatever PayPal it was handed. A fake is no stand-in for a sandbox drop's PayPal."""
    sandbox = Engine(store, StandInSandbox(fake), clock)
    d = sandbox.create_drop("Real", "3:40,5:36", 3, 60, drop_id="d_real")
    sandbox.publish(d)
    join_many(sandbox, d, 3)
    sandbox.close(d)
    sandbox.settle(d)
    assert refused(V.tieout, d, engine).code == "wrong_paypal"                 # was: the harmless-looking no_transaction_search
    assert refused(V.tieout_program, d, engine).code == "wrong_paypal"
    assert V.tieout_program(d, sandbox)["parts"][0]["take"] == "gross"


# ===== 8. a refunded stray never cleared (s8) ===============================================================================

def settled_with_a_stray(engine, drop, fake, amount="40.00"):
    join_many(engine, drop, 4)
    dee = auth_of(engine, drop, "buyer-04")
    engine.close(drop)
    stray = fake.capture(dee, D(amount), "outside-1", final=True)
    engine.settle(drop, expect=plan_token(engine.plan(drop)))
    return stray["capture_id"]


def test_8_a_stray_capture_that_was_refunded_in_full_clears_and_verify_passes(engine, drop, fake):
    cid = settled_with_a_stray(engine, drop, fake)
    v = V.verify(drop, engine)
    assert not v["ok"] and f"refund capture {cid}" in v["next"]
    fake.refund(cid)
    assert fake.get_capture(cid)["status"] == "REFUNDED"
    v = V.verify(drop, engine)
    assert v["ok"], v["problems"]
    assert [h for h in v["holds"] if h["buyer"] == "buyer-04"][0]["refunded"] == [cid]


def test_8_refunded_by_editing_the_status_alone_clears_too_as_the_refuters_reproduction_did(engine, drop, fake):
    cid = settled_with_a_stray(engine, drop, fake)
    edit_fake(fake, lambda s: s["captures"][cid].update(status="REFUNDED"))
    assert V.verify(drop, engine)["ok"]


def test_8_a_partly_refunded_stray_does_not_clear_and_names_what_is_still_taken(engine, drop, fake):
    cid = settled_with_a_stray(engine, drop, fake)
    fake.refund(cid, D("15.00"))
    assert fake.get_capture(cid)["status"] == "PARTIALLY_REFUNDED"
    v = V.verify(drop, engine)
    assert not v["ok"]
    text = " ".join(v["problems"])
    assert "PARTIALLY_REFUNDED" in text and "25.00 of it is still taken" in text
    assert f"refund the remaining 25.00 of capture {cid}" in text
    assert "remaining 25.00" in v["next"]
    fake.refund(cid, D("25.00"))
    assert V.verify(drop, engine)["ok"]


def test_8_a_stray_the_engine_did_not_name_is_still_reported_with_what_is_left_of_it(engine, drop, fake):
    """A capture on the order that appeared after the engine recorded its hold: the order audit reports it (partly refunded: the rest)."""
    join_many(engine, drop, 4)
    engine.close(drop)
    engine.settle(drop, expect=plan_token(engine.plan(drop)))
    lead = engine.get(drop).buyers["buyer-01"]
    edit_fake(fake, lambda s: s["captures"].update({"FKCSTRAY": {
        "capture_id": "FKCSTRAY", "status": "COMPLETED", "amount": "10.00", "currency": "USD", "fee": "0.00", "net": "0.00",
        "final": False, "created": "2026-11-01T12:00:00Z", "auth_id": lead.auth_id, "request_id": "stray-1"}}))
    fake.refund("FKCSTRAY", D("4.00"))                      # the order lists a 4.00 refund against it (PARTIALLY_REFUNDED)
    v = V.verify(drop, engine)
    assert not v["ok"] and any("FKCSTRAY" in p and "6.00 of it still taken" in p for p in v["problems"])
    assert any("charged 46.00 in total" in p for p in v["problems"])        # 40.00 captured + the 6.00 still taken (not 50.00: the refund counts)
    fake.refund("FKCSTRAY")
    assert V.verify(drop, engine)["ok"]


def test_8_a_stray_already_refunded_when_the_engine_first_sees_it_is_named_and_clears_too(engine, drop, fake):
    """The capture was refunded before settlement ever looked: the hold is still CAPTURED at PayPal, the engine records it as taken,
    and the refunded capture's id is kept so verify can read it back and see it was given back."""
    join_many(engine, drop, 4)
    engine.close(drop)
    dee = auth_of(engine, drop, "buyer-04")
    cid = fake.capture(dee, D("40.00"), "outside-0", final=True)["capture_id"]
    fake.refund(cid)
    engine.settle(drop, expect=plan_token(engine.plan(drop)))
    hv = [e for e in evs(engine, drop, "hold_voided") if e["buyer"] == "buyer-04"][0]
    assert hv["taken_outside_engine"] is True and hv["capture_ids"] == [cid]
    assert V.verify(drop, engine)["ok"]


def test_8_the_real_client_reads_what_was_refunded_from_the_order_not_from_the_capture():
    """Round 5 (7b): the refund total is not a field of a capture (it is on a REFUND's breakdown); an order lists its refunds, each
    linked to its capture, and that is where the client reads them. The capture alone says only REFUNDED or PARTIALLY_REFUNDED."""
    cap = {"id": "C1", "status": "PARTIALLY_REFUNDED", "amount": {"value": "40.00", "currency_code": "USD"}}
    view = P._capture_view(cap)
    assert view["refunded"] is None and view["status"] == "PARTIALLY_REFUNDED" and "request_id" not in view
    assert V._remaining(view) == D("40.00")                          # nothing says how much: all of it is still taken, never less
    order = {"id": "O1", "purchase_units": [{"payments": {"captures": [cap, {**cap, "id": "C2", "status": "COMPLETED"}], "refunds": [
        {"id": "R1", "status": "COMPLETED", "amount": {"value": "15.00", "currency_code": "USD"},
         "links": [{"rel": "self", "href": "https://x/v2/payments/refunds/R1"}, {"rel": "up", "href": "https://x/v2/payments/captures/C1"}]},
        {"id": "R2", "status": "PENDING", "amount": {"value": "5.00", "currency_code": "USD"},
         "links": [{"rel": "up", "href": "https://x/v2/payments/captures/C1"}]},
        {"id": "R3", "status": "COMPLETED", "amount": {"value": "9.00", "currency_code": "USD"}}]}}]}
    c1, c2 = P._captures_of_order(order)
    assert (c1["capture_id"], c1["refunded"], c2["capture_id"], c2["refunded"]) == ("C1", "15.00", "C2", "0.00")   # pending and unlinked count for nothing
    assert V._remaining(c1) == D("25.00") and V._remaining({**c1, "status": "REFUNDED"}) == D("0.00")


# ===== 9. the lock must cover the code behind every money command ===========================================================

def tier_of(path, tiers):
    """First match wins: locked > reshape > polish (Togi's rule). `**` crosses folders, `*` does not."""
    def match(pat):
        rx = re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(rx, path) is not None

    for tier in ("locked", "reshape", "polish"):
        if any(match(p) for p in tiers.get(tier, [])):
            return tier
    return None


def manifest():
    with open(os.path.join(ROOT, "togi.toml"), "rb") as f:
        return tomllib.load(f)


def test_9_every_python_file_of_the_crowd_package_is_locked_or_reshape_and_only_ladder_is_reshape():
    tiers = manifest()["tiers"]
    importable = (".py", ".pyw", ".pyc", ".pyo", ".so", ".pyd", ".pth")        # whatever the import system could load (as Togi counts it)
    files = sorted(os.path.relpath(os.path.join(dp, f), ROOT) for dp, dn, fn in os.walk(os.path.join(ROOT, "src", "crowd"))
                   for f in fn if f.endswith(importable) and "__pycache__" not in dp)
    assert "src/crowd/__init__.py" in files and "src/crowd/engine.py" in files
    got = {f: tier_of(f, tiers) for f in files}
    assert all(t in ("locked", "reshape") for t in got.values()), {f: t for f, t in got.items() if t not in ("locked", "reshape")}
    assert {f for f, t in got.items() if t == "reshape"} == {"src/crowd/ladder.py"}


def test_9_every_crowd_module_the_gated_commands_import_is_covered():
    """Whatever the bundle pulls in from the crowd package at run time, not just the files someone remembered to list."""
    import sys
    tiers = manifest()["tiers"]
    for name, mod in sorted(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if name == "crowd" or name.startswith("crowd."):
            rel = os.path.relpath(f, ROOT)
            assert tier_of(rel, tiers) in ("locked", "reshape"), (name, rel)
    assert tier_of("src/crowd/__init__.py", tiers) == "locked" and tier_of("src/crowd/ladder.py", tiers) == "reshape"
    assert tier_of("src/crowd/new_module.py", tiers) is None          # a module added tomorrow is NOT covered until it is listed
