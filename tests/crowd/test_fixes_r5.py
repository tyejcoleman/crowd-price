"""Crowd fix round 5 (docs/crowd-fixes-r5.md): what a fifth refuter found against 7540330, each with a test that failed before the fix.
Fake PayPal only. The principles that stand: nobody is charged twice or above the final price; nobody who saw an error on join, or asked
to leave before the close or the deadline, is charged (D-11, D-11b); nothing is inferred when PayPal can be asked; each drop talks only to
its own PayPal; every stuck state has a working way out that a person is told about; verify never passes while money is held or taken
without the engine's approval. The fake must never prove more than the real PayPal API does.

Items: 1 a leave asked in time lost the drop lock to `close` and the buyer was charged; 2 the uncaptured remainder of a partly captured hold
stayed open while verify passed; 3 a failed capture whose hold somebody then captured blocked settlement for ever; 4 an unreadable earlier
order of a join intent blocked `close` for ever; 5 a sandbox drop with no recorded app belonged to any app; 6 a hold PayPal answered 404 for
was reported by verify for ever; 7 the fake proved more than the real API (the request that made a capture, a refund total on the capture, the
retry window of the capture POST, request ids a buyer name could collide with); 8 the sandbox probe list (docs/DECISIONS.md F-17)."""
import os
import threading
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
from test_fixes_r1 import Wrap, code, edit_fake, fake_state
from test_fixes_r2 import CallSpy, auth_of, blocked, evs, pp_status
from test_fixes_r3 import refused, togi_err
from test_fixes_r4 import captures_on, final_20_drop, stop_settlement_before
from test_paypal_wire import holds, net  # noqa: F401,F811  (pytest fixtures)

D = Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def four(engine, drop_id="d_r5", ladder="3:40,4:36", n=4, minutes=60):
    """A live drop with n buyers (buyer-01 ... buyer-0n), minimum 3, 40.00 then 36.00 at four buyers."""
    d = engine.create_drop("Mugs", ladder, 3, minutes, drop_id=drop_id)
    engine.publish(d)
    join_many(engine, d, n)
    return d


def ends_of(engine, d):
    return parse_iso(engine.get(d).ends_at)


def settle_ok(engine, d):
    return engine.settle(d, expect=plan_token(engine.plan(d)))


class AtClock:
    """A clock that reads one instant (not the test's): the moment a request was TAKEN."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class RunsBetweenReadAndLock:
    """The leave's clock: it hands back the instant the request was taken, and BEFORE the leave gets to the drop lock something else
    (a close) runs to completion: exactly what happens when `close` wins the file lock against a request that read its clock first."""

    def __init__(self, t, then):
        self.t, self.then = t, then

    def __call__(self):
        if self.then is not None:
            then, self.then = self.then, None
            then()
        return self.t


# ===== 1. a leave asked in time lost the drop lock to `close` (p1, p1b) ===================================================

def test_1_a_leave_asked_before_the_deadline_that_loses_the_lock_to_a_close_after_the_deadline_is_honoured(engine, fake, store, clock):
    """p1: asked 5 s before ends_at; the deadline passes and `close` runs (as of the deadline) before the leave gets the lock. It was
    refused `drop_not_live` before anything was written, and the buyer who saw that error was charged at settle."""
    d = four(engine)
    ends = ends_of(engine, d)

    def deadline_passes_and_close_runs():
        clock.t = ends + timedelta(seconds=20)
        Engine(store, fake, clock).close(d)

    clock.t = ends - timedelta(seconds=5)
    leaver = Engine(store, fake, RunsBetweenReadAndLock(clock.t, deadline_passes_and_close_runs))
    dee = auth_of(engine, d, "buyer-04")
    out = leaver.leave(d, "buyer-04")
    assert out["state"] == "closed" and out["buyers"] == 3
    assert [e["buyer"] for e in evs(engine, d, "leave_started")] == ["buyer-04"] and engine.get(d).buyers["buyer-04"].status == "left"
    assert pp_status(fake, dee) == "VOIDED"
    closed = evs(engine, d, "drop_closed")[0]
    assert closed["reason"] == "expired" and closed["count"] == 4        # the crowd as of the deadline still had the leaver: the drop moves on
    assert engine.get(d).count == 3                                      # ... and the projection counts the leaver out of it
    assert plan_token(engine.plan(d)) == "capture:3:36.00:108.00"
    out = settle_ok(engine, d)
    assert out["counts"] == {"holds": 4, "captured": 3, "voided": 1, "failed": 0, "left": 1}
    assert captures_on(fake, dee) == [] and V.verify(d, engine)["ok"]


def test_1_a_leave_asked_after_the_deadline_is_still_refused_even_when_close_has_not_run(engine, clock):
    d = four(engine)
    clock.t = ends_of(engine, d) + timedelta(seconds=1)
    assert code(engine.leave, d, "buyer-04") == "drop_ended"
    engine.close(d)
    assert code(engine.leave, d, "buyer-04") == "drop_not_live"
    assert not evs(engine, d, "leave_started")


def test_1_a_leave_asked_before_a_manual_close_was_asked_is_honoured_and_one_asked_after_is_not(engine, fake, store, clock):
    """The drop's close time is when the close was ASKED for (read before it waited for the lock), `closed_at` on `drop_closed`."""
    d = four(engine)
    t0 = clock.t
    other = Engine(store, fake, AtClock(t0 + timedelta(seconds=1)))       # the maker's close, asked one second after the leave
    leaver = Engine(store, fake, RunsBetweenReadAndLock(t0, lambda: other.close(d)))
    assert leaver.leave(d, "buyer-04")["buyers"] == 3                       # asked at t0 < t0 + 1 s: honoured although close ran first
    closed = evs(engine, d, "drop_closed")[0]
    assert closed["reason"] == "manual" and closed["closed_at"] == iso(t0 + timedelta(seconds=1))
    assert engine.get(d).closed_at == iso(t0 + timedelta(seconds=1))
    late = Engine(store, fake, AtClock(t0 + timedelta(seconds=2)))        # asked after the close was
    assert code(late.leave, d, "buyer-03") == "drop_not_live"
    same = Engine(store, fake, AtClock(t0 + timedelta(seconds=1)))        # asked at the very instant of the close: not before it
    assert code(same.leave, d, "buyer-03") == "drop_not_live"
    assert engine.get(d).buyers["buyer-03"].status == "held"


def test_1_close_reads_its_clock_before_the_lock_and_records_that_instant_as_closed_at(engine, fake, store, clock):
    d = four(engine)
    reads = iter([clock.t + timedelta(seconds=3), clock.t + timedelta(seconds=9)] + [clock.t + timedelta(seconds=9)] * 50)
    Engine(store, fake, lambda: next(reads)).close(d)
    ev = [e for e in store.events(d) if e["type"] == "drop_closed"][0]
    assert ev["payload"]["closed_at"] == iso(clock.t + timedelta(seconds=3)) and ev["ts"] == iso(clock.t + timedelta(seconds=9))


def test_1_a_closed_drop_from_older_code_is_judged_by_the_time_its_close_event_was_written(engine, fake, store, clock):
    """No `closed_at` on the payload (written before this round): the event's own time stands in for it."""
    d = four(engine)
    engine.close(d)
    ev = [e for e in store.events(d) if e["type"] == "drop_closed"][0]
    assert engine.get(d).closed_at == iso(clock.t) == ev["ts"]


def test_1_a_timely_leave_after_a_close_forces_a_fresh_approval_of_the_plan(engine, fake, store, clock):
    d = four(engine)
    t0 = clock.t
    clock.advance(seconds=10)
    engine.close(d)                                                   # closed_at = t0 + 10 s
    old = plan_token(engine.plan(d))
    assert old == "capture:4:36.00:144.00"
    assert Engine(store, fake, AtClock(t0 + timedelta(seconds=5))).leave(d, "buyer-04")["buyers"] == 3
    e = refused(engine.settle, d, expect=old)
    assert e.code == "plan_changed" and e.ctx["actual"] == "capture:3:36.00:108.00"
    assert not evs(engine, d, "hold_captured")                          # nothing was captured on the old plan
    out = engine.settle(d, expect=e.ctx["actual"])
    assert out["counts"]["captured"] == 3 and V.verify(d, engine)["ok"]


def test_1_a_timely_leave_that_takes_a_closed_drop_below_its_minimum_turns_the_plan_into_a_void(engine, fake, store, clock):
    d = four(engine, n=3)
    t0 = clock.t
    clock.advance(seconds=10)
    engine.close(d)
    old = plan_token(engine.plan(d))
    assert old == "capture:3:40.00:120.00"
    Engine(store, fake, AtClock(t0 + timedelta(seconds=5))).leave(d, "buyer-03")
    e = refused(engine.settle, d, expect=old)
    assert e.code == "plan_changed" and e.ctx["actual"] == "void:2"
    out = engine.settle(d, expect="void:2")
    assert out["outcome"] == "not_funded" and not fake_state(fake)["captures"] and V.verify(d, engine)["ok"]


def test_1_a_timely_leave_whose_void_meets_an_outage_stays_on_record_and_settle_completes_it(engine, fake, store, clock):
    """D-11b on a closed drop: the buyer saw an error, the leave is on record, and settle honours it before anything is captured."""
    d = four(engine)
    t0 = clock.t
    clock.advance(seconds=10)
    engine.close(d)
    dee = auth_of(engine, d, "buyer-04")
    fake.unavailable("void", match=dee)
    assert code(Engine(store, fake, AtClock(t0 + timedelta(seconds=5))).leave, d, "buyer-04") == "paypal_unavailable"
    assert "buyer-04" in engine.get(d).leaving
    e = refused(engine.settle, d, expect=plan_token(engine.plan(d)))       # the plan still counted the pending leaver
    assert e.code == "plan_changed"
    out = engine.settle(d, expect=e.ctx["actual"])
    assert out["counts"]["captured"] == 3 and captures_on(fake, dee) == [] and pp_status(fake, dee) == "VOIDED"
    assert V.verify(d, engine)["ok"]


def test_1_a_timely_leave_on_a_settling_drop_releases_a_buyer_not_yet_captured(engine, fake, store, clock):
    d = four(engine)
    t0 = clock.t
    clock.advance(seconds=10)
    engine.close(d)
    stop_settlement_before(engine, d, fake, "buyer-02")                  # buyer-01 captured; settlement waits on an outage
    dee = auth_of(engine, d, "buyer-04")
    Engine(store, fake, AtClock(t0 + timedelta(seconds=5))).leave(d, "buyer-04")
    assert engine.get(d).buyers["buyer-04"].status == "left" and pp_status(fake, dee) == "VOIDED"
    e = refused(engine.settle, d, expect="capture:3:36.00:108.00")        # the approval given before the leave no longer matches
    assert e.code == "plan_changed" and e.ctx["actual"] == "capture:2:36.00:72.00"
    out = engine.settle(d, expect=e.ctx["actual"])
    assert out["counts"] == {"holds": 4, "captured": 3, "voided": 1, "failed": 0, "left": 1} and captures_on(fake, dee) == []
    assert V.verify(d, engine)["ok"]


def captured_before_the_leave(engine, fake, store, clock, settle=True):
    """Settlement has run; then a leave that was asked before the close reaches the drop: the lock went to settle first."""
    d = four(engine)
    t0 = clock.t
    clock.advance(seconds=10)
    engine.close(d)
    if settle:
        settle_ok(engine, d)
    return d, Engine(store, fake, AtClock(t0 + timedelta(seconds=5)))


def test_1_a_timely_leave_that_finds_the_buyer_already_captured_is_recorded_and_verify_names_the_capture_to_refund(engine, fake, store, clock):
    d, leaver = captured_before_the_leave(engine, fake, store, clock)
    dee = engine.get(d).buyers["buyer-04"]
    e = refused(leaver.leave, d, "buyer-04")
    assert e.code == "charged_before_leave" and e.ctx["capture_id"] == dee.capture_id and e.ctx["amount"] == "36.00"
    ev = [x for x in store.events(d) if x["type"] == "leave_failed"]
    assert len(ev) == 1 and ev[0]["payload"]["reason"] == "captured_before_leave" and ev[0]["payload"]["capture_id"] == dee.capture_id
    assert not engine.get(d).leaving and dee.status == "captured"          # the money did move: the engine does not pretend otherwise
    assert code(leaver.leave, d, "buyer-04") == "charged_before_leave"
    assert len([x for x in store.events(d) if x["type"] == "leave_failed"]) == 1        # asked twice, written once
    v = V.verify(d, engine)
    assert not v["ok"]
    text = " ".join(v["problems"])
    assert "buyer-04 asked to leave" in text and f"capture {dee.capture_id}" in text and f"refund capture {dee.capture_id}" in v["next"]
    assert v["totals"]["gross"] == "144.00"                                 # (it was captured: the engine's own totals still agree with PayPal's)
    fake.refund(dee.capture_id)
    v = V.verify(d, engine)
    assert v["ok"], v["problems"]
    assert [h for h in v["holds"] if h["buyer"] == "buyer-04"][0]["refunded"] == [dee.capture_id]


def test_1_a_part_refunded_capture_of_a_timely_leaver_still_fails_and_says_what_is_left(engine, fake, store, clock):
    d, leaver = captured_before_the_leave(engine, fake, store, clock)
    cid = engine.get(d).buyers["buyer-04"].capture_id
    assert code(leaver.leave, d, "buyer-04") == "charged_before_leave"
    fake.refund(cid, D("10.00"))
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund the remaining 26.00 of capture {cid}" in v["next"] and "26.00 of it is still taken" in " ".join(v["problems"])
    fake.refund(cid, D("26.00"))
    assert V.verify(d, engine)["ok"]


def test_1_a_leave_asked_after_the_close_finds_a_captured_buyer_and_writes_nothing(engine, fake, store, clock):
    d, _ = captured_before_the_leave(engine, fake, store, clock)
    before = len(store.events(d))
    assert code(Engine(store, fake, AtClock(clock.t + timedelta(seconds=1))).leave, d, "buyer-04") == "drop_not_live"
    assert len(store.events(d)) == before


def test_1_the_bundle_says_who_was_charged_and_what_to_refund(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,4:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 4)
    eng = tb._eng()
    t0 = eng.now()
    tb.drop_close(d)
    tb.drop_settle(d, tb.drop_plan(d)["expect"], ctx=SimpleNamespace(approval_scope="test"))
    cid = eng.get(d).buyers["sim-004"].capture_id
    early = Engine(eng.store, eng.holds, AtClock(t0 - timedelta(seconds=1)))
    monkeypatch.setattr(tb, "_eng", lambda: early)
    e = togi_err(tb.drop_leave, d, "sim-004")
    assert e.code == "charged_before_leave" and e.status == "refused" and cid in e.message and cid in e.next
    assert "{" not in e.message + e.next


class StepClock:
    """A clock the test moves by hand and many threads may read. `view(name)` is one request's own clock: it remembers the first instant
    that request read (the moment it was TAKEN) and tells the test when that happened."""

    def __init__(self, t):
        self.t, self.first, self.read = t, {}, {}
        self._lock = threading.Lock()

    def view(self, name):
        self.read[name] = threading.Event()

        def now():
            with self._lock:
                self.first.setdefault(name, self.t)
                self.read[name].set()
                return self.t
        return now


def test_1_with_real_threads_and_a_real_file_lock_a_close_that_wins_the_lock_does_not_cost_a_leaver_the_hold(engine, fake, store, clock):
    """p1b: a slow request holds the drop lock across the deadline. The maker's close is queued at ends-6 s, the buyer's leave at ends-5 s,
    and the close gets the lock first (after the deadline, so it closes the drop as of ends_at). The leave was asked in time."""
    for trial in range(4):
        d = four(engine, drop_id=f"d_t{trial}")
        ends = ends_of(engine, d)
        sc = StepClock(ends - timedelta(seconds=6))
        got = {}
        threads = {"close": threading.Thread(target=lambda: got.update(close=_try(Engine(store, fake, sc.view("close")).close, d))),
                   "leave": threading.Thread(target=lambda: got.update(leave=_try(Engine(store, fake, sc.view("leave")).leave, d, "buyer-04")))}
        with store.lock(d):                                         # a slow join on a slow PayPal
            threads["close"].start()
            sc.read["close"].wait(0.5)                     # (it reads its clock before it queues for the lock; code that read it inside the lock never sets this)
            sc.t = ends - timedelta(seconds=5)
            threads["leave"].start()
            assert sc.read["leave"].wait(5)
            sc.t = ends + timedelta(seconds=20)                    # it finishes after the deadline
        for t in threads.values():
            t.join(10)
        assert got["close"][0] == "ok" and got["leave"][0] == "ok", got
        assert evs(engine, d, "drop_closed")[0]["reason"] == "expired" and engine.get(d).buyers["buyer-04"].status == "left"
        clock.t = ends + timedelta(seconds=20)
        out = settle_ok(engine, d)
        assert out["counts"]["captured"] == 3 and captures_on(fake, auth_of(engine, d, "buyer-04")) == [] and V.verify(d, engine)["ok"]


def _try(fn, *a):
    try:
        fn(*a)
        return ("ok",)
    except CrowdError as e:
        return ("err", e.code)


class RaceClock:
    """Every read of the clock is one millisecond later than the last, so the requests are totally ordered by when they were TAKEN; who
    then gets the lock is the operating system's business. Remembers each request's first read."""

    def __init__(self, t):
        self.t, self.first = t, {}
        self._lock = threading.Lock()

    def view(self, name):
        def now():
            with self._lock:
                self.t += timedelta(milliseconds=1)
                self.first.setdefault(name, self.t)
                return self.t
        return now


def test_1_a_real_race_for_the_lock_between_a_leave_and_a_manual_close_is_judged_by_when_each_was_asked_not_by_who_won(engine, fake, store, clock):
    """No lock held by the test: two threads race for the real file lock, 24 times. Whichever wins it: a leave asked before the close was
    asked stands (the buyer is not charged); a leave is refused only if it was asked at or after the close (and then the buyer is charged, with a
    clear error); and a leave that happens to run first on the still-live drop stands whenever it was asked."""
    honoured = refused_ = 0
    for trial in range(24):
        d = four(engine, drop_id=f"d_r{trial}", minutes=600)
        rc = RaceClock(clock.t)
        got, go = {}, threading.Barrier(2)

        def do_close():
            go.wait()
            got["close"] = _try(Engine(store, fake, rc.view("close")).close, d)

        def do_leave():
            go.wait()
            got["leave"] = _try(Engine(store, fake, rc.view("leave")).leave, d, "buyer-04")

        ts = [threading.Thread(target=do_close), threading.Thread(target=do_leave)]
        [t.start() for t in ts]
        [t.join(15) for t in ts]
        assert got["close"] == ("ok",), got
        closed_at = parse_iso(evs(engine, d, "drop_closed")[0]["closed_at"])
        asked = rc.first["leave"]
        out = settle_ok(engine, d)
        dee = auth_of(engine, d, "buyer-04")
        if got["leave"] == ("ok",):
            honoured += 1
            assert engine.get(d).buyers["buyer-04"].status == "left" and out["counts"]["captured"] == 3 and captures_on(fake, dee) == []
        else:
            refused_ += 1
            assert got["leave"] == ("err", "drop_not_live") and asked >= closed_at, (got, asked, closed_at)     # asked after the close
            assert out["counts"]["captured"] == 4
        assert V.verify(d, engine)["ok"]
    assert honoured + refused_ == 24


# ===== 2. the uncaptured remainder of a partly captured hold (p2) ===========================================================

@pytest.mark.parametrize("phase", ["between close and settle", "during a stopped settlement"])
def test_2_the_remainder_of_a_partly_captured_hold_is_voided_when_the_hold_is_recorded_as_taken(engine, fake, store, clock, phase):
    d = four(engine)
    engine.close(d)
    if phase.startswith("during"):
        stop_settlement_before(engine, d, fake, "buyer-02")
    dee = auth_of(engine, d, "buyer-04")
    cid = fake.capture(dee, D("10.00"), "maker-dashboard-1", final=False)["capture_id"]
    assert pp_status(fake, dee) == "PARTIALLY_CAPTURED"
    out = settle_ok(engine, d)
    assert out["counts"]["captured"] == 3
    hv = [e for e in evs(engine, d, "hold_voided") if e["buyer"] == "buyer-04"][0]
    assert hv["taken_outside_engine"] is True and hv["capture_ids"] == [cid] and hv["remainder_voided"] is True
    assert not evs(engine, d, "remainder_open") and pp_status(fake, dee) == "VOIDED"
    with pytest.raises(P.AuthorizationVoided):                                  # the rest can no longer be captured by anyone
        fake.capture(dee, D("30.00"), "maker-dashboard-2", final=True)
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund capture {cid}" in v["next"]               # the 10.00 already taken is still for a person to give back
    fake.refund(cid)
    assert V.verify(d, engine)["ok"]


def partly_taken_with_a_refusing_paypal(engine, fake):
    d = four(engine)
    engine.close(d)
    dee = auth_of(engine, d, "buyer-04")
    fake.void_partially_captured(False)                  # the other thing the sandbox may answer (F-17)
    cid = fake.capture(dee, D("10.00"), "maker-dashboard-1", final=False)["capture_id"]
    out = settle_ok(engine, d)
    return d, dee, cid, out


def test_2_if_paypal_refuses_to_void_the_remainder_remainder_open_is_recorded_and_verify_keeps_failing_after_the_refund(engine, fake):
    d, dee, cid, out = partly_taken_with_a_refusing_paypal(engine, fake)
    assert out["counts"]["captured"] == 3 and pp_status(fake, dee) == "PARTIALLY_CAPTURED"
    ro = evs(engine, d, "remainder_open")
    assert len(ro) == 1 and ro[0]["buyer"] == "buyer-04" and ro[0]["auth_id"] == dee and ro[0]["code"] == "authorization_already_captured"
    assert ro[0]["paypal_status"] == "PARTIALLY_CAPTURED"
    hv = [e for e in evs(engine, d, "hold_voided") if e["buyer"] == "buyer-04"][0]
    assert "remainder_voided" not in hv
    v = V.verify(d, engine)
    assert not v["ok"] and "still PARTIALLY_CAPTURED" in " ".join(v["problems"]) and f"capture {cid}" in " ".join(v["problems"])
    nexts = [x["next"] for x in v["problem_details"]]
    assert f"refund capture {cid}" in v["next"] and any(f"refund capture {cid}" in n and f"void authorisation {dee}" in n for n in nexts)
    fake.refund(cid)                                      # the person does what the first half of the next step says ...
    v = V.verify(d, engine)                               # ... and verify reads the authorisation AGAIN: the rest is still open
    assert not v["ok"], "verify passed while the rest of the hold was still authorised on the buyer's card"
    text = " ".join(v["problems"])
    assert "refunded, but the authorisation is still PARTIALLY_CAPTURED" in text and "could be captured later" in text
    assert "authorization_already_captured" not in text and "AUTHORIZATION_ALREADY_CAPTURED" in text         # (and PayPal's own refusal is quoted)
    assert v["next"].startswith(f"void authorisation {dee}")
    fake.void_partially_captured(True)                    # a person voids the rest at PayPal
    fake.void(dee, "person-void")
    assert V.verify(d, engine)["ok"]


def test_2_an_authorisation_that_lapses_also_clears_it(engine, fake):
    d, dee, cid, out = partly_taken_with_a_refusing_paypal(engine, fake)
    fake.refund(cid)
    assert not V.verify(d, engine)["ok"]
    fake.expire(dee)
    assert pp_status(fake, dee) == "EXPIRED" and V.verify(d, engine)["ok"]


def test_2_a_stray_that_took_the_whole_hold_passes_once_refunded_because_nothing_more_can_be_captured(engine, fake):
    d = four(engine)
    engine.close(d)
    dee = auth_of(engine, d, "buyer-04")
    cid = fake.capture(dee, D("40.00"), "maker-dashboard-1", final=True)["capture_id"]
    settle_ok(engine, d)
    assert not V.verify(d, engine)["ok"]
    fake.refund(cid)
    assert pp_status(fake, dee) == "CAPTURED" and V.verify(d, engine)["ok"]


def test_2_verify_never_passes_a_taken_hold_whatever_the_authorisation_still_is(engine, fake, store):
    """The rule, hold by hold: a `taken` hold passes only when every capture is refunded in full AND the authorisation is VOIDED,
    CAPTURED or EXPIRED. PARTIALLY_CAPTURED, CREATED, PENDING and DENIED all fail."""
    d = four(engine)
    engine.close(d)
    dee = auth_of(engine, d, "buyer-04")
    cid = fake.capture(dee, D("10.00"), "maker-dashboard-1", final=False)["capture_id"]
    settle_ok(engine, d)
    fake.refund(cid)
    assert pp_status(fake, dee) == "VOIDED" and V.verify(d, engine)["ok"]
    for status in ("PARTIALLY_CAPTURED", "CREATED", "PENDING", "DENIED"):
        edit_fake(fake, lambda s, status=status: s["auths"][dee].update(status=status))
        v = V.verify(d, engine)
        assert not v["ok"] and f"the authorisation is still {status}" in " ".join(v["problems"]) and f"void authorisation {dee}" in v["next"], status
    for status in ("VOIDED", "CAPTURED", "EXPIRED"):
        edit_fake(fake, lambda s, status=status: s["auths"][dee].update(status=status))
        assert V.verify(d, engine)["ok"], status


# ===== 3. a failed capture whose hold somebody then captured (p5) =============================================================

def failed_then_taken(engine, fake, amount="36.00", final=True):
    """bob's capture is refused (recorded, settlement goes on); cy's meets an outage (settlement stops before the release pass); then the
    maker charges bob by hand: the hold was still open after the refusal."""
    d = four(engine)
    engine.close(d)
    tok = plan_token(engine.plan(d))
    bob, cy = auth_of(engine, d, "buyer-02"), auth_of(engine, d, "buyer-03")
    fake.refuse("capture", "TRANSACTION_REFUSED", match=bob)
    fake.unavailable("capture", match=cy)
    assert code(engine.settle, d, expect=tok) == "paypal_unavailable"
    assert engine.get(d).buyers["buyer-02"].status == "capture_failed"
    cid = fake.capture(bob, D(amount), "maker-dashboard-bob", final=final)["capture_id"]
    return d, bob, cid


def test_3_a_failed_hold_that_somebody_then_captured_is_recorded_as_taken_and_settlement_finishes(engine, fake):
    d, bob, cid = failed_then_taken(engine, fake)
    assert pp_status(fake, bob) == "CAPTURED"
    out = settle_ok(engine, d)                              # was: settle_blocked at the release pass, for ever
    assert out["state"] == "settled" and out["counts"]["captured"] == 3 and out["counts"]["failed"] == 1
    ev = evs(engine, d, "failed_hold_released")
    assert len(ev) == 1 and ev[0]["buyer"] == "buyer-02" and ev[0]["reason"] == "taken_outside_engine" and ev[0]["capture_ids"] == [cid]
    assert engine.get(d).buyers["buyer-02"].status == "capture_failed" and engine.get(d).buyers["buyer-02"].released
    assert not evs(engine, d, "settle_blocked")
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund capture {cid}" in v["next"] and "buyer-02" in " ".join(v["problems"])
    fake.refund(cid)
    assert V.verify(d, engine)["ok"]


def test_3_an_outside_partial_capture_on_a_failed_hold_has_its_remainder_released_too(engine, fake):
    d, bob, cid = failed_then_taken(engine, fake, amount="10.00", final=False)
    assert pp_status(fake, bob) == "PARTIALLY_CAPTURED"
    out = settle_ok(engine, d)
    assert out["state"] == "settled" and pp_status(fake, bob) == "VOIDED"     # the release pass's own void released the rest
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund capture {cid}" in " ".join(v["problems"] + [v["next"]])      # and the 10.00 taken is named (order audit)
    fake.refund(cid)
    assert V.verify(d, engine)["ok"]


def test_3_the_same_with_a_paypal_that_will_not_void_a_partly_captured_hold_records_the_open_remainder(engine, fake):
    d, bob, cid = failed_then_taken(engine, fake, amount="10.00", final=False)
    fake.void_partially_captured(False)
    out = settle_ok(engine, d)
    assert out["state"] == "settled" and pp_status(fake, bob) == "PARTIALLY_CAPTURED"
    ev = evs(engine, d, "failed_hold_released")[0]
    assert ev["reason"] == "taken_outside_engine" and ev["paypal_status"] == "PARTIALLY_CAPTURED" and ev["capture_ids"] == [cid]
    assert [e["buyer"] for e in evs(engine, d, "remainder_open")] == ["buyer-02"]
    fake.refund(cid)
    v = V.verify(d, engine)
    assert not v["ok"] and f"void authorisation {bob}" in v["next"]


def test_3_paypal_saying_captured_when_nothing_is_taken_is_still_a_name_not_a_fact(engine, fake):
    """The refusal alone does not make a hold taken: PayPal is asked, shows nothing on it, and settlement stops for a person as before."""
    d = four(engine)
    engine.close(d)
    tok = plan_token(engine.plan(d))
    bob, cy = auth_of(engine, d, "buyer-02"), auth_of(engine, d, "buyer-03")
    fake.refuse("capture", "TRANSACTION_REFUSED", match=bob)
    fake.unavailable("capture", match=cy)
    assert code(engine.settle, d, expect=tok) == "paypal_unavailable"
    fake.refuse("void", "AUTHORIZATION_ALREADY_CAPTURED", match=bob, times=None)
    e = blocked(engine.settle, d, expect=plan_token(engine.plan(d)))
    assert e.ctx["phase"] == "release" and e.ctx["buyer"] == "buyer-02" and not evs(engine, d, "failed_hold_released")


def test_3_exclude_handles_a_failed_hold_that_somebody_captured_while_settlement_is_stopped_for_another_reason(engine, fake):
    d = four(engine, n=5, ladder="3:40,5:36")
    engine.close(d)
    tok = plan_token(engine.plan(d))
    bob, cy = auth_of(engine, d, "buyer-02"), auth_of(engine, d, "buyer-03")
    fake.refuse("capture", "TRANSACTION_REFUSED", match=bob)
    fake.refuse("capture", "NOT_AUTHORIZED", http=403, match=cy)           # not about one buyer: settlement stops for a person
    blocked(engine.settle, d, expect=tok)
    assert engine.get(d).buyers["buyer-02"].status == "capture_failed"
    cid = fake.capture(bob, D("36.00"), "maker-dashboard-bob", final=True)["capture_id"]
    out = engine.exclude(d, "buyer-02")                                      # was: void_conflict, nothing written, and no other way out
    assert out["excluded"] is True and out["released"] is False and out["taken"] is True and "refund" in out["next"]
    assert engine.get(d).buyers["buyer-02"].excluded and engine.get(d).buyers["buyer-02"].released
    ev = evs(engine, d, "failed_hold_released")[0]
    assert ev["reason"] == "taken_outside_engine" and ev["capture_ids"] == [cid]
    engine.exclude(d, "buyer-03")                                            # (and the other buyer, whose card is the trouble)
    out = settle_ok(engine, d)
    assert out["state"] == "settled"
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund capture {cid}" in " ".join(v["problems"] + [v["next"]])
    fake.refund(cid)
    assert V.verify(d, engine)["ok"]


def test_3_exclude_of_a_held_buyer_whose_hold_was_captured_is_still_a_conflict(engine, fake):
    d = four(engine)
    fake.capture(auth_of(engine, d, "buyer-02"), D("40"), "someone-else")
    before = len(engine.store.events(d))
    assert code(engine.exclude, d, "buyer-02") == "void_conflict" and len(engine.store.events(d)) == before


# ===== 4. close stuck on an unreadable earlier order (p6) =====================================================================

class OrderWontSay(Wrap):
    """PayPal answers 403 (or 404) whenever this order is looked at."""

    def __init__(self, inner, order_id, http=403):
        super().__init__(inner)
        self.order_id, self.http, self.asked = order_id, http, []

    def _no(self):
        body = ({"name": "NOT_AUTHORIZED", "details": [{"issue": "PERMISSION_DENIED"}]} if self.http == 403
                else {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})
        raise P._error_from(self.http, body)

    def recover_join(self, request_id, amount, currency, label, order_id=None):
        if order_id == self.order_id:
            self.asked.append(order_id)
            self._no()
        return self.inner.recover_join(request_id, amount, currency, label, order_id=order_id)

    def order_authorizations(self, order_id):
        if order_id == self.order_id:
            self._no()
        return self.inner.order_authorizations(order_id)

    def order_captures(self, order_id):
        if order_id == self.order_id:
            self._no()
        return self.inner.order_captures(order_id)


def two_orders_for_dee(engine, fake, drop_id="d_p6"):
    """dee's first try places a hold and loses the reply; PayPal then forgets the request id; the second try does the same on a NEW order.
    One join intent, two orders, both with a hold."""
    d = engine.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id=drop_id)
    engine.publish(d)
    join_many(engine, d, 3)
    for _ in range(2):
        fake.lose_reply("authorize", match=":dee")
        assert code(engine.join, d, "dee") == "paypal_unavailable"
        rids = [k for k, j in engine.get(d).unresolved.items() if j.get("buyer") == "dee"]

        def forget(s, rids=rids):
            for k in rids:
                s["requests"].pop(k, None)
                s["order_rids"].pop(k, None)
        edit_fake(fake, forget)
    rid = [k for k, j in engine.get(d).unresolved.items() if j.get("buyer") == "dee"][0]
    first, second = engine.get(d).unresolved[rid]["order_ids"]
    return d, rid, first, second


@pytest.mark.parametrize("http", [403, 404])
def test_4_an_earlier_order_paypal_will_not_describe_is_recorded_and_close_goes_on(engine, fake, store, clock, http):
    d, rid, first, second = two_orders_for_dee(engine, fake)
    wont = OrderWontSay(fake, first, http)
    e2 = Engine(store, wont, clock)
    out = e2.close(d)                                       # was: earlier_attempt_unresolved on every close, for ever
    assert out["state"] == "closed" and not e2.get(d).unresolved
    ev = evs(engine, d, "order_unreadable")
    assert len(ev) == 1 and ev[0]["order_id"] == first and ev[0]["buyer"] == "dee" and ev[0]["request_id"] == rid and ev[0]["http"] == http
    assert wont.asked == [first]                            # asked once, and never again
    released = [e for e in evs(engine, d, "orphan_hold_released") if not e.get("superseded")]
    assert [e["order_id"] for e in released] == [second]    # the current order is handled as usual: its hold is released (D-11)
    assert sorted(a["status"] for a in fake_state(fake)["auths"].values() if a["label"].endswith(":dee")) == ["CREATED", "VOIDED"]
    settle_ok(e2, d)
    assert wont.asked == [first]


def test_4_verify_reports_the_unreadable_order_with_the_way_out_and_clears_when_a_person_has_dealt_with_it(engine, fake, store, clock):
    d, rid, first, second = two_orders_for_dee(engine, fake)
    wont = OrderWontSay(fake, first, 403)
    e2 = Engine(store, wont, clock)
    e2.close(d)
    settle_ok(e2, d)
    v = V.verify(d, e2)
    assert not v["ok"]
    text = " ".join(v["problems"])
    assert f"order {first}" in text and "could not be read" in text and "dee" in text
    assert f"check order {first} at PayPal" in v["next"] and "void its hold if one exists" in v["next"]
    assert sum(first in p for p in v["problems"]) == 1       # said once, not once per check
    # PayPal answers again: the order's hold is open and the log does not account for it, so that is what verify says ...
    v = V.verify(d, engine)
    assert not v["ok"] and any("does not account for" in p for p in v["problems"])
    # ... and once a person has voided it, verify passes
    stray = [a for a, st in fake_state(fake)["auths"].items() if st["order_id"] == first][0]
    fake.void(stray, "person-void")
    assert V.verify(d, engine)["ok"]


def test_4_a_join_that_meets_an_unreadable_earlier_order_is_refused_once_and_then_goes_on(engine, fake, store, clock):
    d, rid, first, second = two_orders_for_dee(engine, fake, "d_p6j")
    wont = Engine(store, OrderWontSay(fake, first, 403), clock)
    e = refused(wont.join, d, "dee")
    assert e.code == "earlier_attempt_unresolved"                      # no new hold is put on top of one that could not be looked at
    assert len(evs(engine, d, "order_unreadable")) == 1
    out = wont.join(d, "dee")                                          # asking again is not refused for ever
    assert out["buyers"] == 4


# ===== 3b. the same dead end, one level down: a captured hold the log never knew (a join whose reply was lost) =====================

def orphan_taken(engine, fake, amount, final):
    """dee's join reaches PayPal and the reply is lost; before the engine looks, the maker captures that hold by hand."""
    d = engine.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id="d_orph")
    engine.publish(d)
    join_many(engine, d, 3)
    fake.lose_reply("authorize", match=":dee")
    assert code(engine.join, d, "dee") == "paypal_unavailable"
    orphan = [a for a, st in fake_state(fake)["auths"].items() if st["label"].endswith(":dee")][0]
    cid = fake.capture(orphan, D(amount), "maker-dashboard-dee", final=final)["capture_id"]
    engine.close(d)
    settle_ok(engine, d)
    return d, orphan, cid


def test_3b_a_hold_the_log_never_knew_that_was_captured_clears_once_the_capture_is_refunded(engine, fake):
    d, orphan, cid = orphan_taken(engine, fake, "40.00", True)
    assert evs(engine, d, "hold_failed")[0]["code"] == "orphan_unexpected_status"
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund capture {cid}" in v["next"]
    fake.refund(cid)
    assert pp_status(fake, orphan) == "CAPTURED" and V.verify(d, engine)["ok"], "a refunded stray stayed in verify for ever (CAPTURED is not 'gone')"


def test_3b_a_partly_captured_one_needs_the_rest_voided_as_well(engine, fake):
    d, orphan, cid = orphan_taken(engine, fake, "10.00", False)
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund capture {cid}" in v["next"] and f"also void authorisation {orphan}" in v["next"]
    fake.refund(cid)
    v = V.verify(d, engine)
    assert not v["ok"] and f"void authorisation {orphan}" in v["next"]
    fake.void(orphan, "person-void")
    assert V.verify(d, engine)["ok"]


# ===== 5. a sandbox drop with no recorded app (p4) ==========================================================================

class SandboxApp(FakePayPal):
    """A fake that is the sandbox app `ref`, with its own PayPal state (a second app cannot see the first one's holds)."""
    kind = "sandbox"

    def __init__(self, path, ref):
        super().__init__(path)
        self._ref = ref

    @property
    def ref(self):
        return self._ref


def refless_sandbox_drop(store, app_a, clock, drop_id="d_old", n=4):
    """The log a version from before refs left behind: events say `sandbox` and name no app. The holds are app A's."""
    lg = lad.parse("3:40,4:36")
    ev = {"paypal_mode": "sandbox"}
    store.append(drop_id, "drop_created", {"title": "Mugs", "maker": "m", "currency": "USD", "minimum": 3, "duration_minutes": 60,
                                          "ladder": lg.to_dict(), "costs": None, **ev}, ts=clock())
    store.append(drop_id, "drop_published", {"ends_at": iso(clock() + timedelta(minutes=60))}, ts=clock())
    for i in range(n):
        name = f"b{i}"
        h = app_a.authorize(lg.start_price, "USD", f"{drop_id}:{name}", f"crowd-{drop_id}-{name}-authorize")
        store.append_many(drop_id, [("buyer_joined", {"buyer": name, "name": name, "card_index": 0}),
                                    ("hold_placed", {"buyer": name, "name": name, "order_id": h["order_id"], "auth_id": h["auth_id"],
                                                     "amount": "40.00", "currency": "USD", "status": "CREATED", "expires": h["expires"],
                                                     "attempt": 1, "request_id": f"crowd-{drop_id}-{name}-authorize", **ev})], ts=clock())
    return drop_id


def two_apps(tmp_path, store, clock):
    a, b = SandboxApp(str(tmp_path / "a.json"), "aaaaaaaaaaaa"), SandboxApp(str(tmp_path / "b.json"), "bbbbbbbbbbbb")
    return a, b, Engine(store, a, clock), Engine(store, b, clock)


REFLESS_OPS = {
    "join": lambda e, d: e.join(d, "Late"), "leave": lambda e, d: e.leave(d, "b0"), "close": lambda e, d: e.close(d),
    "reconcile": lambda e, d: e.reconcile(d), "exclude": lambda e, d: e.exclude(d, "b0"), "publish": lambda e, d: e.publish(d),
    "verify": lambda e, d: V.verify(d, e), "tieout": lambda e, d: V.tieout(d, e), "tieout_program": lambda e, d: V.tieout_program(d, e),
}


@pytest.mark.parametrize("op", sorted(REFLESS_OPS))
@pytest.mark.parametrize("which", ["a", "b"])
def test_5_a_sandbox_drop_that_names_no_app_belongs_to_no_app_until_a_person_binds_it(tmp_path, store, clock, op, which):
    a, b, ea, eb = two_apps(tmp_path, store, clock)
    d = refless_sandbox_drop(store, a, clock)
    assert ea.recorded_paypals(d) == {("sandbox", None)}
    before = len(store.events())
    e = refused(REFLESS_OPS[op], {"a": ea, "b": eb}[which], d)             # not even the app whose holds they are, until it is bound
    assert e.code == "drop_unbound" and e.ctx["drop"] == d
    assert len(store.events()) == before and {x["status"] for x in fake_state(a)["auths"].values()} == {"CREATED"}


def test_5_p4_a_second_app_cannot_close_the_drop_unfunded_and_lock_the_first_out(tmp_path, store, clock):
    a, b, ea, eb = two_apps(tmp_path, store, clock)
    d = refless_sandbox_drop(store, a, clock)
    assert refused(eb.close, d).code == "drop_unbound"                          # was: ok, "crowd 0", closed unfunded
    plan = eb.bind_plan(d)
    assert len(plan["unmatched"]) == 4                                            # app B does not know one of A's holds
    assert refused(eb.bind, d, plan["expect"]).code == "bind_mismatch" and not [e for e in store.events(d) if e["type"] == "paypal_bound"]
    pa = ea.bind_plan(d)
    assert pa["unmatched"] == [] and pa["expect"].startswith("bind:sandbox:aaaaaaaaaaaa:4:") and not pa.get("bound")
    assert refused(ea.bind, d, "bind:sandbox:aaaaaaaaaaaa:4:00000000").code == "bind_changed"
    assert ea.bind(d, pa["expect"])["bound"] is True
    ev = [e for e in store.events(d) if e["type"] == "paypal_bound"]
    assert len(ev) == 1 and ev[0]["payload"]["paypal_ref"] == "aaaaaaaaaaaa" and ev[0]["payload"]["paypal_mode"] == "sandbox"
    assert code(ea.bind, d, pa["expect"]) == "already_bound"
    assert refused(eb.close, d).code == "wrong_paypal"                           # and now B is the stranger
    assert ea.close(d)["state"] == "closed" and ea.get(d).closed["count"] == 4
    assert settle_ok(ea, d)["outcome"] == "funded" and V.verify(d, ea)["ok"]


def test_5_a_fake_cannot_bind_a_sandbox_drop_and_the_holds_view_says_what_would_be_checked(tmp_path, store, clock):
    a, b, ea, eb = two_apps(tmp_path, store, clock)
    d = refless_sandbox_drop(store, a, clock)
    fake = Engine(store, FakePayPal(str(tmp_path / "f.json")), clock)
    assert refused(fake.bind_plan, d).code == "wrong_paypal" and refused(fake.holds_view, d).code == "wrong_paypal"
    view = ea.holds_view(d)
    assert view["bound"] is False and [h["status"] for h in view["holds"]] == ["CREATED"] * 4 and view["expect"].startswith("bind:sandbox:aaaaaaaaaaaa:4:")


def test_5_a_drop_that_records_its_app_is_not_touched_by_any_of_this(tmp_path, store, clock):
    a, b, ea, eb = two_apps(tmp_path, store, clock)
    d = ea.create_drop("Mugs", "3:40,4:36", 3, 60, drop_id="d_new")
    ea.publish(d)
    join_many(ea, d, 4)
    assert ea.recorded_paypals(d) == {("sandbox", "aaaaaaaaaaaa")} and refused(eb.close, d).code == "wrong_paypal"
    assert ea.close(d)["state"] == "closed"


def test_5_through_the_bundle_a_refless_drop_shows_what_to_bind_and_the_bind_needs_an_approval_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    store = Store(str(tmp_path / "crowd.db"))
    a = SandboxApp(str(tmp_path / "a.json"), "aaaaaaaaaaaa")
    d = refless_sandbox_drop(store, a, Clock())
    eng = Engine(store, a, Clock())
    monkeypatch.setattr(tb, "_eng", lambda: eng)
    e = togi_err(tb.drop_close, d)
    assert e.code == "drop_unbound" and e.status == "refused" and "{" not in e.message + e.next and "app" in e.message
    seen = tb.drop_holds(d)
    assert seen["bound"] is False and not seen["unmatched"] and f"togi crowd drop bind --drop {d} --expect {seen['expect']}" in seen["next"]
    assert togi_err(tb.drop_bind, d, seen["expect"], ctx=SimpleNamespace(approval_scope="test")).code == "test_approval_outside_test_mode"
    assert tb.drop_bind(d, seen["expect"], ctx=SimpleNamespace(approval_scope="default"))["bound"] is True
    assert tb.drop_close(d)["state"] == "closed"


# ===== 6. a hold PayPal answered 404 for (p3) ====================================================================================

@pytest.mark.parametrize("path", ["a pending leave", "the close revalidation"])
def test_6_a_hold_the_engine_recorded_as_not_found_is_resolved_in_verify_with_a_note(engine, fake, path):
    d = four(engine)
    dee = auth_of(engine, d, "buyer-04")
    if path == "a pending leave":
        fake.unavailable("void", match=dee)
        assert code(engine.leave, d, "buyer-04") == "paypal_unavailable"
    edit_fake(fake, lambda s: s["auths"].pop(dee))                        # PayPal answers 404 for this authorisation from now on
    engine.close(d)
    settle_ok(engine, d)
    assert [e["paypal_status"] for e in evs(engine, d, "hold_voided") if e["buyer"] == "buyer-04"] == ["NOT_FOUND"]
    for _ in range(2):                                                        # ... every time, not "run again when PayPal answers"
        v = V.verify(d, engine)
        assert v["ok"], v["problems"]
        row = [h for h in v["holds"] if h["buyer"] == "buyer-04"][0]
        assert row["ok"] and row["paypal"] == "NOT_FOUND" and "no record of this authorisation (404)" in row["note"]
        assert "answers" not in " ".join(c["detail"] for c in v["checks"])


def not_found_then_back(engine, fake, status):
    d = four(engine)
    dee = auth_of(engine, d, "buyer-04")
    saved = dict(fake_state(fake)["auths"][dee])
    edit_fake(fake, lambda s: s["auths"].pop(dee))
    engine.close(d)
    settle_ok(engine, d)
    assert V.verify(d, engine)["ok"]
    edit_fake(fake, lambda s: s["auths"].update({dee: {**saved, "status": status}}))
    return d, dee


def test_6_if_paypal_now_returns_it_as_open_that_is_a_problem_with_the_way_out(engine, fake):
    d, dee = not_found_then_back(engine, fake, "CREATED")
    v = V.verify(d, engine)
    assert not v["ok"]
    assert "no such authorisation, but PayPal now shows it CREATED" in " ".join(v["problems"]) and "open on the buyer's card" in " ".join(v["problems"])
    assert f"void authorisation {dee}" in v["next"]
    fake.void(dee, "person-void")
    v = V.verify(d, engine)
    assert v["ok"] and "nothing is held" in [h for h in v["holds"] if h["buyer"] == "buyer-04"][0]["note"]


def test_6_if_paypal_now_returns_it_as_captured_that_is_money_taken(engine, fake):
    d, dee = not_found_then_back(engine, fake, "CAPTURED")
    v = V.verify(d, engine)
    assert not v["ok"] and "money was taken from the buyer outside the engine" in " ".join(v["problems"])


# ===== 7. the fake must not prove more than the real API ==========================================================================

def test_7a_nothing_the_fake_answers_says_which_request_made_a_capture(fake):
    a = fake.authorize(D("40"), "USD", "d:x", "r-auth")["auth_id"]
    order = fake_state(fake)["auths"][a]["order_id"]
    made = fake.capture(a, D("20"), "c1", final=False)
    for view in (made, fake.get_capture(made["capture_id"]), fake.recover_capture(order), *fake.order_captures(order)):
        assert "request_id" not in view and set(view) <= set(P._capture_view({})), set(view) - set(P._capture_view({}))
    wire = fake._wire_order(fake_state(fake), order)
    assert "request_id" not in str(wire) and "c1" not in str(wire)
    assert P._capture_view({"id": "C", "request_id": "crowd-x"}).get("request_id") is None
    assert captures_on(fake, a)[0]["request_id"] == "c1"                      # (the fake keeps it for its own idempotency, and for tests)


class RealShaped(Wrap):
    """What the real client's answers look like to the engine, on top of the fake: nothing but the mapping of PayPal's JSON."""
    kind = "fake"

    @property
    def ref(self):
        return self.inner.ref


@pytest.mark.parametrize("wrap", [lambda f: f, RealShaped], ids=["the fake itself", "a real-shaped client"])
def test_7a_an_outside_final_capture_at_the_final_price_after_an_attempt_that_never_reached_paypal_is_adopted(engine, fake, store, clock, wrap):
    """p7: the engine's capture POST never reached PayPal (an outage), then somebody captured the hold, FINAL, at exactly the final price.
    The engine cannot tell it from its own lost capture (PayPal does not say which request made it), so both clients behave alike:
    it is adopted, the buyer is charged once at the final price, and verify passes. No money harm; documented (engine._could_be_ours)."""
    eng = Engine(store, wrap(fake), clock)
    d = eng.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id="d_p7")
    eng.publish(d)
    join_many(eng, d, 3)
    eng.close(d)
    tok = plan_token(eng.plan(d))
    cy = auth_of(eng, d, "buyer-03")
    fake.unavailable("capture", match=cy)
    assert code(eng.settle, d, expect=tok) == "paypal_unavailable"
    fake.capture(cy, D("40.00"), "maker-dashboard-cy", final=True)
    eng.settle(d, expect=plan_token(eng.plan(d)))
    ev = [e for e in evs(eng, d, "hold_captured") if e["buyer"] == "buyer-03"][0]
    assert ev["recovered"] is True and [c["amount"] for c in captures_on(fake, cy)] == ["40.00"]
    assert V.verify(d, eng)["ok"]


def test_7a_the_same_outside_capture_without_an_attempt_on_record_is_still_taken_and_refunded_by_a_person(engine, fake, store, clock):
    """What is NOT adopted: a capture the engine never tried to make (no `capture_started` for the hold), or any other shape."""
    d = final_20_drop(engine, drop_id="d_p7b")
    stop_settlement_before(engine, d, fake, "buyer-02")
    dee = auth_of(engine, d, "buyer-04")
    cid = fake.capture(dee, D("20.00"), "maker-dashboard", final=True)["capture_id"]
    settle_ok(engine, d)
    assert "buyer-04" not in [e["buyer"] for e in evs(engine, d, "hold_captured")]
    assert not V.verify(d, engine)["ok"] and f"refund capture {cid}" in V.verify(d, engine)["next"]


def test_7b_refunds_are_read_from_the_order_in_the_shape_paypal_returns_it(fake):
    a = fake.authorize(D("40"), "USD", "d:x", "r-auth")
    cid = fake.capture(a["auth_id"], D("40"), "c1", final=True)["capture_id"]
    fake.refund(cid, D("15.00"))
    order = fake._wire_order(fake_state(fake), a["order_id"])
    pay = order["purchase_units"][0]["payments"]
    assert [c["id"] for c in pay["captures"]] == [cid] and "refunded" not in str(pay["captures"]) and "total_refunded" not in str(pay["captures"])
    (refund,) = pay["refunds"]
    assert refund["status"] == "COMPLETED" and refund["amount"] == {"currency_code": "USD", "value": "15.00"}
    assert [lk["rel"] for lk in refund["links"]] == ["self", "up"] and refund["links"][1]["href"].endswith(f"/v2/payments/captures/{cid}")
    (view,) = fake.order_captures(a["order_id"])
    assert view["status"] == "PARTIALLY_REFUNDED" and view["refunded"] == "15.00"
    assert fake.get_capture(cid)["refunded"] is None                 # on its own, a capture says only its status
    fake.refund(cid)
    assert [r["amount"]["value"] for r in fake._wire_order(fake_state(fake), a["order_id"])["purchase_units"][0]["payments"]["refunds"]] == ["15.00", "25.00"]
    assert fake.order_captures(a["order_id"])[0]["refunded"] == "40.00" and fake.get_capture(cid)["status"] == "REFUNDED"


def test_7b_verify_trusts_the_orders_refunds_not_a_status_the_capture_alone_would_give(engine, fake):
    """The order lists a refund of the whole stray capture; the capture JSON still says COMPLETED. PayPal's order is what is read."""
    d = four(engine)
    engine.close(d)
    dee = auth_of(engine, d, "buyer-04")
    cid = fake.capture(dee, D("40.00"), "maker-dashboard-1", final=True)["capture_id"]
    settle_ok(engine, d)
    assert not V.verify(d, engine)["ok"]
    edit_fake(fake, lambda s: s["refunds"].append({"id": "FKRX", "capture_id": cid, "amount": "40.00", "currency": "USD", "status": "COMPLETED"}))
    assert fake_state(fake)["captures"][cid]["status"] == "COMPLETED"
    assert V.verify(d, engine)["ok"]
    edit_fake(fake, lambda s: s["refunds"][0].update(amount="25.00", status="PENDING"))      # a refund that has not completed is not money back
    v = V.verify(d, engine)
    assert not v["ok"] and f"refund capture {cid}" in v["next"]


def test_7b_the_real_client_reads_the_order_for_refunds(holds, net):
    cap = {"id": "C1", "status": "PARTIALLY_REFUNDED", "final_capture": True, "amount": {"value": "40.00", "currency_code": "USD"}}
    net.reply({"id": "ORD1", "purchase_units": [{"payments": {"captures": [cap], "refunds": [
        {"id": "R1", "status": "COMPLETED", "amount": {"value": "12.50", "currency_code": "USD"},
         "links": [{"rel": "up", "href": "https://api.sandbox.paypal.com/v2/payments/captures/C1", "method": "GET"}]}]}}]})
    (view,) = holds.order_captures("ORD1")
    assert (view["capture_id"], view["status"], view["refunded"]) == ("C1", "PARTIALLY_REFUNDED", "12.50")
    assert V._remaining(view) == D("27.50")


def test_7c_the_capture_post_is_sent_once_and_never_retried(holds, net):
    for failure in (503, 504, 429, 408):
        net.sent.clear()
        net.queue[:] = [(failure, {})]
        with pytest.raises(P.PayPalUnavailable):
            holds.capture("AUTH1", "32", "crowd-d_1.b1.capture", final=True)
        assert [s["url"].rsplit("/v2", 1)[1] for s in net.sent] == ["/payments/authorizations/AUTH1/capture"], failure
    net.sent.clear()
    net.queue[:] = [(401, {})]
    with pytest.raises(P.HoldError) as e:
        holds.capture("AUTH1", "32", "crowd-d_1.b1.capture", final=True)
    assert e.value.code == "paypal_not_configured" and len(net.sent) == 1          # (a refused token is not retried either)


def test_7c_a_timeout_on_the_capture_post_is_not_retried_either(holds, net, monkeypatch):
    import urllib.error
    calls = []

    def boom(req, timeout=None, context=None):
        calls.append((req.get_method(), req.full_url, timeout))
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(P.PayPalUnavailable):
        holds.capture("AUTH1", "32", "r", final=True)
    assert len(calls) == 1 and calls[0][2] == 60                  # one attempt, at most one 60 s timeout: the window the engine documents
    calls.clear()
    with pytest.raises(P.PayPalUnavailable):
        holds.get("AUTH1")                                        # a read is still retried (it moves no money)
    assert len(calls) == holds.retries


def test_7c_a_transient_failure_of_the_capture_stops_settlement_for_a_resume_that_reads_the_hold_again(engine, fake, store, clock):
    d = four(engine)
    engine.close(d)
    tok = plan_token(engine.plan(d))
    cy = auth_of(engine, d, "buyer-03")
    fake.unavailable("capture", match=cy)
    assert code(engine.settle, d, expect=tok) == "paypal_unavailable"
    assert engine.get(d).state == "settling" and "buyer-03" in engine.get(d).capturing
    spy = CallSpy(fake)
    out = Engine(store, spy, clock).settle(d, expect=plan_token(engine.plan(d)))
    assert out["counts"]["captured"] == 4 and spy.calls.count(("capture", cy)) == 1


BUYER_NAMES = ["ada", "ada-authorize", "ada-void", "ada-capture", "ada-void2", "ada-authorize2", "ada-authorize-authorize", "x", "x-capture"]


def test_7d_no_buyer_name_can_build_another_buyers_request_id():
    seen = {}
    for buyer in BUYER_NAMES:
        for action in ("authorize", "void", "capture"):
            for attempt in (1, 2, 3, 12):
                rid = P.request_id_for("d_x", buyer, action, attempt)
                assert rid.startswith("crowd-") and len(rid) <= 108
                assert rid not in seen, (rid, seen[rid], (buyer, action, attempt))
                seen[rid] = (buyer, action, attempt)
                step2 = P.card_request_id(rid)                              # the join's second step has an id of its own too
                assert step2 not in seen or seen[step2] == ("card", rid), (step2, seen[step2])
                seen[step2] = ("card", rid)
    old_collision = ("crowd-d_x-ada-authorize-authorize", "ada-authorize")      # the old form: buyer 'ada-authorize' == the card step of 'ada'
    assert P.request_id_for("d_x", old_collision[1], "authorize") != P.card_request_id(P.request_id_for("d_x", "ada", "authorize"))
    from crowd.store import slug
    assert "." not in slug("a.b") and "." not in "".join(slug(n) for n in ("Ada.Lovelace", "x.y.z", "..", ".-."))
    assert P.request_id_for("d_x", "ada", "authorize") == "crowd-d_x.ada.authorize"
    assert P.card_request_id("crowd-d_x.ada.authorize") == "crowd-d_x.ada.authorize.card"
    assert P.card_request_id("crowd-d_x-ada-authorize") == "crowd-d_x-ada-authorize-authorize"        # an id already on record keeps its form


def test_7d_a_buyer_whose_name_looks_like_anothers_operation_gets_a_hold_of_his_own(engine, fake):
    d = engine.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id="d_7d")
    engine.publish(d)
    a = engine.join(d, "ada")
    b = engine.join(d, "ada authorize")                         # slug 'ada-authorize'
    assert a["auth_id"] != b["auth_id"]
    rids = {e["buyer"]: e["request_id"] for e in evs(engine, d, "hold_placed")}
    assert rids == {"ada": "crowd-d_7d.ada.authorize", "ada-authorize": "crowd-d_7d.ada-authorize.authorize"}
    assert len(fake_state(fake)["requests"]) == 2


def test_7d_an_intent_recorded_under_the_old_request_id_is_replayed_under_that_id_not_a_recomputed_one(engine, fake, store, clock):
    """In-flight at the upgrade: the join was asked under 'crowd-<drop>-<buyer>-authorize', its answer lost. The retry finds the hold
    PayPal made under THAT id (replay), instead of asking under a new id and holding the buyer's card twice."""
    d = engine.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id="d_old7")
    engine.publish(d)
    old_rid = "crowd-d_old7-ada-authorize"
    h = fake.authorize(D("40"), "USD", "d_old7:ada", old_rid)                      # PayPal acted under the old id; the engine never heard
    store.append(d, "join_started", {"buyer": "ada", "name": "ada", "card_index": 0, "attempt": 1, "request_id": old_rid,
                                    "request_ids": [old_rid, old_rid + "-authorize"], "amount": "40.00", "currency": "USD",
                                    "paypal_mode": "fake", "paypal_ref": fake.ref}, ts=clock())
    store.append(d, "order_created", {"buyer": "ada", "request_id": old_rid, "order_id": h["order_id"]}, ts=clock())
    out = engine.join(d, "ada")
    assert out["auth_id"] == h["auth_id"] and out["buyers"] == 1                   # the same hold, replayed
    hp = evs(engine, d, "hold_placed")[0]
    assert hp["request_id"] == old_rid and hp["replayed"] is True
    assert len([a for a in fake_state(fake)["auths"].values()]) == 1


def test_7d_a_capture_attempt_recorded_under_the_old_id_is_repeated_under_that_id(engine, fake, store, clock):
    d = final_20_drop(engine, drop_id="d_old8")
    dee = auth_of(engine, d, "buyer-04")
    old_rid = "crowd-d_old8-buyer-04-capture"
    store.append(d, "capture_started", {"buyer": "buyer-04", "auth_id": dee, "amount": "20.00", "currency": "USD", "request_id": old_rid,
                                       "attempt": 1, "paypal_mode": "fake", "paypal_ref": fake.ref}, ts=clock())
    seen = []

    class Rids(Wrap):
        def capture(self, auth_id, amount, request_id, **k):
            seen.append((auth_id, request_id))
            return self.inner.capture(auth_id, amount, request_id, **k)

    engine.close(d) if engine.get(d).state == "live" else None
    Engine(store, Rids(fake), clock).settle(d, expect=plan_token(engine.plan(d)))
    assert (dee, old_rid) in seen and len([r for a, r in seen if a == dee]) == 1
    assert [e["request_id"] for e in evs(engine, d, "capture_started") if e["buyer"] == "buyer-04"] == [old_rid]     # not written again under a new id


def test_7d_a_leave_on_record_under_the_old_id_is_completed_under_that_id(engine, fake, store, clock):
    d = four(engine)
    dee = auth_of(engine, d, "buyer-04")
    old_rid = "crowd-d_r5-buyer-04-void"
    store.append(d, "leave_started", {"buyer": "buyer-04", "auth_id": dee, "request_id": old_rid, "paypal_mode": "fake",
                                     "paypal_ref": fake.ref}, ts=clock())
    seen = []

    class Rids(Wrap):
        def void(self, auth_id, request_id):
            seen.append((auth_id, request_id))
            return self.inner.void(auth_id, request_id)

    Engine(store, Rids(fake), clock).close(d)
    assert seen == [(dee, old_rid)] and engine.get(d).buyers["buyer-04"].status == "left"


# ===== 8. the probe list ==========================================================================================================

def test_8_f17_in_the_decisions_file_lists_the_new_sandbox_probes():
    text = open(os.path.join(ROOT, "docs", "DECISIONS.md")).read()
    assert "F-17" in text
    f17 = text[text.index("F-17"):]
    for needle in ("void of a partially captured authorization", "payments.refunds", "GET capture", "request-id scoping",
                   "how long", "one attempt", "'.'"):
        assert needle.lower() in f17.lower(), needle
