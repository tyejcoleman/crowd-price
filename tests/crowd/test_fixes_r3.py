"""Crowd fix round 3 (docs/crowd-fixes-r3.md): the money-safety gaps a third refuter found, each with a test that failed before
the fix. Fake PayPal only. They are mostly one class: the engine trusted whichever PayPal the current process uses rather than
the one each hold was made with, and it planned settlement from its own records instead of asking PayPal right before money
moves. Decisions D-11 (a hold the engine never confirmed is released) and D-11b (a leave asked for before the deadline is
honoured even if PayPal errored) stand: nobody who saw an error is charged, and nothing is inferred when PayPal can be asked."""
import json
import os
import re
import tomllib
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from conftest import join_many
from crowd import paypal_holds as P
from crowd import store as S
from crowd import togi_bundle as tb
from crowd import verify as V
from crowd.engine import CrowdError, Engine, foreign_holds, plan_token
from crowd.paypal_holds import FakePayPal
from crowd.store import Store
from test_fixes_r1 import Wrap, code, edit_fake, fake_state, open_auths
from test_fixes_r2 import all_statuses, auth_of, blocked, closed_drop, evs, pp_status
from test_paypal_wire import StubClient
from test_paypal_wire import holds, net  # noqa: F401,F811  (pytest fixtures)

D = Decimal
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class StandInSandbox(Wrap):
    """A fake that calls itself the sandbox (so nothing real is touched): its holds are recorded as `sandbox`."""
    kind = "sandbox"
    ref = None


def other_fake(tmp_path, name="other"):
    return FakePayPal(str(tmp_path / f"{name}.fakepaypal.json"))


def refused(fn, *a, **kw):
    """The CrowdError a call raises."""
    with pytest.raises(CrowdError) as e:
        fn(*a, **kw)
    return e.value


# ===== A. one store, one PayPal (r9, r9b, r9c, r12) ==============================================================================

def test_a_the_drop_records_which_paypal_it_was_made_with(engine, drop, fake, store):
    created = [e["payload"] for e in store.events(drop) if e["type"] == "drop_created"][0]
    assert created["paypal_mode"] == "fake" and created["paypal_ref"] == fake.ref
    assert engine.recorded_paypals(drop) == {("fake", fake.ref)}


OPS_ON_A_LIVE_DROP = {
    "publish": lambda e, d: e.publish(d), "join": lambda e, d: e.join(d, "Late"),
    "leave": lambda e, d: e.leave(d, "buyer-01"), "close": lambda e, d: e.close(d),
    "reconcile": lambda e, d: e.reconcile(d), "exclude": lambda e, d: e.exclude(d, "buyer-01"),
}


@pytest.mark.parametrize("op", sorted(OPS_ON_A_LIVE_DROP))
def test_a_every_operation_refuses_with_wrong_paypal_and_writes_nothing_on_a_live_drop(engine, drop, store, tmp_path, clock, op):
    """r9 / r9c: another PayPal reads every real hold as NOT_FOUND, closes a funded drop as unfunded, or locks ladder steps with
    holds the real PayPal never had."""
    join_many(engine, drop, 4)
    elsewhere = other_fake(tmp_path)
    before = len(store.events())
    e = refused(OPS_ON_A_LIVE_DROP[op], Engine(store, elsewhere, clock), drop)
    assert e.code == "wrong_paypal" and e.ctx["drop"] == drop and e.ctx["recorded"].startswith("fake")
    assert len(store.events()) == before                                   # nothing written
    assert not os.path.exists(elsewhere.path) or not fake_state(elsewhere)["auths"]    # and the other PayPal was never asked
    assert engine.view(engine.get(drop))["buyers"] == 4


def test_a_a_funded_drop_closed_through_another_paypal_is_refused_and_still_closes_funded_on_the_right_one(engine, drop, store, tmp_path, clock):
    """r9, verbatim: close via PayPal B used to read five real holds as NOT_FOUND and close the funded drop as unfunded."""
    join_many(engine, drop, 5)
    assert code(Engine(store, other_fake(tmp_path), clock).close, drop) == "wrong_paypal"
    out = engine.close(drop)
    assert out["buyers"] == 5 and out["minimum_met"] is True and "revalidated" not in out
    assert engine.settle(drop, expect=plan_token(engine.plan(drop)))["outcome"] == "funded"
    assert all_statuses(engine.holds) == ["CAPTURED"]


@pytest.mark.parametrize("op", ["plan", "settle", "reconcile", "verify"])
def test_a_plan_settle_reconcile_and_verify_refuse_another_paypal_too(engine, drop, store, tmp_path, clock, op):
    join_many(engine, drop, 4)
    engine.close(drop)
    elsewhere = Engine(store, other_fake(tmp_path), clock)
    calls = {"plan": lambda: elsewhere.plan(drop), "settle": lambda: elsewhere.settle(drop),
             "reconcile": lambda: elsewhere.reconcile(drop), "verify": lambda: V.verify(drop, elsewhere)}
    before = len(store.events())
    if op == "verify":                      # verify needs a settled drop; settle it on the right PayPal first
        engine.settle(drop)
        before = len(store.events())
    assert refused(calls[op]).code == "wrong_paypal"
    assert len(store.events()) == before
    assert engine.get(drop).state == ("settled" if op == "verify" else "closed")


def test_a_a_store_belongs_to_one_paypal_so_no_drop_is_made_in_another_paypals_store(engine, drop, store, clock, fake):
    join_many(engine, drop, 2)
    sandbox = Engine(store, StandInSandbox(fake), clock)
    e = refused(sandbox.create_drop, "Another", "3:40,5:36", 3, 60, drop_id="d_two")
    assert e.code == "wrong_paypal" and not store.exists("d_two")


def test_a_a_second_sandbox_app_cannot_operate_on_a_drop_made_with_the_first(tmp_path, clock):
    """Same mode, different client-id fingerprint."""
    store = Store(str(tmp_path / "crowd.db"))
    base = FakePayPal(str(tmp_path / "crowd.db.fakepaypal.json"))

    class App(StandInSandbox):
        def __init__(self, inner, ref):
            super().__init__(inner)
            self.ref = ref

    one = Engine(store, App(base, "aaaaaaaaaaaa"), clock)
    d = one.create_drop("Real", "3:40,5:36", 3, 60, drop_id="d_sbx")
    one.publish(d)
    join_many(one, d, 3)
    assert one.recorded_paypals(d) == {("sandbox", "aaaaaaaaaaaa")}
    two = Engine(store, App(base, "bbbbbbbbbbbb"), clock)
    assert refused(two.close, d).code == "wrong_paypal"
    assert refused(Engine(store, StandInSandbox(base), clock).close, d).code == "wrong_paypal"    # no ref at all is not a match either
    assert one.close(d)["state"] == "closed"


def test_a_a_drop_written_before_refs_existed_is_not_locked_out(tmp_path, clock):
    """A sandbox drop from an older version has a mode and no ref: it cannot be compared more closely, so it is let through
    (otherwise every drop from the live runs would be unreachable)."""
    store = Store(str(tmp_path / "crowd.db"))
    base = FakePayPal(str(tmp_path / "crowd.db.fakepaypal.json"))
    old = Engine(store, StandInSandbox(base), clock)
    d = old.create_drop("Old", "3:40,5:36", 3, 60, drop_id="d_old")
    old.publish(d)
    join_many(old, d, 3)

    class App(StandInSandbox):
        ref = "cccccccccccc"

    assert Engine(store, App(base), clock).close(d)["state"] == "closed"


def test_a_the_real_client_s_ref_is_a_fingerprint_of_the_client_id_never_the_secret(monkeypatch):
    class Client(StubClient):
        env = {"PAYPAL_ENV": "sandbox", "PAYPAL_CLIENT_ID": "AaBbCcClientIdXyz", "PAYPAL_CLIENT_SECRET": "s3cr3t-value"}

    holds = P.PayPalHolds(client=Client())
    assert re.fullmatch(r"[0-9a-f]{12}", holds.ref)
    assert "AaBbCc" not in holds.ref and "s3cr3t" not in holds.ref
    Client2 = type("Client2", (Client,), {"env": {**Client.env, "PAYPAL_CLIENT_ID": "AnotherClientId"}})
    assert P.PayPalHolds(client=Client2()).ref != holds.ref
    assert P.PayPalHolds(client=StubClient()).ref is None             # no client id to fingerprint: no ref, never a guess
    assert P.mode_of(holds) == "sandbox"


def test_a_no_event_carries_a_secret_or_the_client_id(tmp_path, clock):
    class Client(StubClient):
        env = {"PAYPAL_ENV": "sandbox", "PAYPAL_CLIENT_ID": "AaBbCcClientIdXyz", "PAYPAL_CLIENT_SECRET": "s3cr3t-value"}

    real = P.PayPalHolds(client=Client())
    store = Store(str(tmp_path / "crowd.db"))
    Engine(store, real, clock).create_drop("Real", "3:40,5:36", 3, 60, drop_id="d_real")
    text = json.dumps(store.events())
    assert real.ref in text and "AaBbCc" not in text and "s3cr3t" not in text


# ---- the default store path follows the PayPal mode ---------------------------------------------------------------------------

def test_a_the_default_store_path_for_the_fake_is_never_the_real_stores(monkeypatch, tmp_path):
    for k in ("CROWD_DB", "CROWD_ENV", "CROWD_PAYPAL", "TOGI_HOME"):
        monkeypatch.delenv(k, raising=False)
    real = os.path.join(S.ROOT, "data", "crowd.db")
    assert S.default_path() == real                                        # sandbox (the default) keeps data/crowd.db
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    assert S.default_path() == os.path.join(S.ROOT, "data", "crowd-fake.db") != real
    monkeypatch.setenv("CROWD_PAYPAL", " FAKE ")
    assert S.default_path().endswith("crowd-fake.db")
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("TOGI_HOME", str(tmp_path / "th"))
    assert S.default_path() == str(tmp_path / "th" / "state" / "crowd" / "crowd-test.db")      # under Togi, the Togi home
    monkeypatch.delenv("TOGI_HOME")
    assert S.default_path() == os.path.join(S.ROOT, "data", "crowd-test.db")
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "chosen.db"))
    assert S.default_path() == str(tmp_path / "chosen.db")                 # an explicit path wins (the binding still guards it)


def test_a_a_fake_pointed_at_the_real_store_by_hand_still_cannot_touch_its_drops(tmp_path, clock):
    path = str(tmp_path / "crowd.db")
    store = Store(path)
    real = Engine(store, StandInSandbox(FakePayPal(path + ".fakepaypal.json")), clock)
    d = real.create_drop("Real", "3:40,5:36", 3, 60, drop_id="d_real")
    real.publish(d)
    join_many(real, d, 3)
    careless = Engine(store, FakePayPal(str(tmp_path / "elsewhere.json")), clock)       # CROWD_PAYPAL=fake CROWD_DB=<the real db>
    assert refused(careless.close, d).code == "wrong_paypal"


# ---- through the bundle -------------------------------------------------------------------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    return tmp_path


def standin_drop(env, clock=None, n=5, name="d_real"):
    """A drop whose holds belong to a stand-in sandbox, in the store the bundle's fake would also open."""
    store = Store(str(env / "crowd.db"))
    eng = Engine(store, StandInSandbox(FakePayPal(str(env / "standin-sandbox.json"))), clock)
    eng.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id=name)
    eng.publish(name)
    join_many(eng, name, n)
    return name, eng


def togi_err(fn, *a, **kw):
    with pytest.raises(tb.TogiError) as e:
        fn(*a, **kw)
    return e.value


def test_a_the_bundle_s_close_join_and_sim_join_refuse_a_store_whose_holds_belong_to_another_paypal(env):
    """r9b and r9c, as the bundle runs them (effect write, no approval): a fake on a sandbox store."""
    name, eng = standin_drop(env, n=4)
    for call in (lambda: tb.drop_close(name), lambda: tb.sim_join(name, 4), lambda: tb.drop_join(name, "Late"),
                 lambda: tb.drop_leave(name, "buyer-01"), lambda: tb.drop_exclude(name, "buyer-01")):
        e = togi_err(call)
        assert e.code == "wrong_paypal" and e.status == "refused"
        assert "sandbox" in e.message and "fake" in e.message and "{" not in e.message + (e.next or "")
    assert eng.view(eng.get(name))["buyers"] == 4 and eng.get(name).locked_step == 0       # no phantom hold locked a step


def test_a_a_test_approval_cannot_settle_a_real_drop_the_chain_in_r12(env):
    """r12: close (no approval) through a fake, then settle with a throwaway approver's yes. Both ends are now shut."""
    name, eng = standin_drop(env)
    assert togi_err(tb.drop_close, name).code == "wrong_paypal"
    eng.close(name)
    tok = plan_token(eng.plan(name))
    ctx = SimpleNamespace(approval_scope="test")
    assert togi_err(tb.drop_settle, name, tok, ctx=ctx).code == "test_approval_outside_test_mode"     # foreign holds in the store
    assert eng.get(name).state == "closed"
    assert togi_err(tb.drop_settle, name, tok, ctx=SimpleNamespace(approval_scope="default")).code == "wrong_paypal"
    assert eng.settle(name, expect=tok)["outcome"] == "funded"            # only the PayPal it belongs to settles it


def test_a_test_approvals_need_the_fake_crowd_env_test_and_a_store_of_this_fakes_holds_only(env, monkeypatch):
    """The three conditions of the rule, one at a time. The fake + CROWD_ENV=test + clean store case settles (test_fixes_r2)."""
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 3)
    tb.drop_close(d)
    tok = tb.drop_plan(d)["expect"]
    ctx = SimpleNamespace(approval_scope="test")
    monkeypatch.delenv("CROWD_ENV")
    assert "CROWD_ENV" in togi_err(tb.drop_settle, d, tok, ctx=ctx).message
    monkeypatch.setenv("CROWD_ENV", "test")
    store = Store(str(env / "crowd.db"))
    store.append("d_other", "drop_created", {"title": "x", "maker": "m", "currency": "USD", "minimum": 3, "duration_minutes": 60,
                                           "ladder": tb._eng().get(d).ladder.to_dict(), "paypal_mode": "fake", "paypal_ref": "000000000000"})
    assert "did not come from this fake" in togi_err(tb.drop_settle, d, tok, ctx=ctx).message      # another fake's drop is foreign too
    assert tb._eng().get(d).state == "closed"


def test_a_the_manifest_opts_in_to_test_approvals_and_requires_the_lock():
    with open(os.path.join(ROOT, "togi.toml"), "rb") as f:
        policy = tomllib.load(f)["policy"]
    assert policy["test_approvals"] is True and policy["require_lock"] is True and policy["approve_effects"] == ["money"]


def test_a_foreign_holds_counts_a_drop_made_by_another_paypal_even_before_any_hold(engine, drop, fake, store, tmp_path, clock):
    assert foreign_holds(store, fake.ref) == []
    # (a second PayPal cannot make a drop in this store any more, so write what an older version or a person might have)
    store.append("d_x", "drop_created", {"title": "x", "maker": "m", "currency": "USD", "minimum": 3, "duration_minutes": 60,
                                       "ladder": engine.get(drop).ladder.to_dict(), "paypal_mode": "sandbox"})
    assert [f["mode"] for f in foreign_holds(store, fake.ref)] == ["sandbox"]


# ===== B. PayPal is asked again right before money moves (r5, r6, r2) ========================================================

def test_b_a_hold_voided_after_close_leaves_the_crowd_and_the_funded_decision_follows_it(engine, drop, fake):
    """r6: closed at exactly the minimum, PayPal voids one hold, and the plan a person signs captured minimum-1 buyers at the
    step-0 price (verify passed). The plan is now worked out from what PayPal shows."""
    join_many(engine, drop, 3)
    assert engine.close(drop)["minimum_met"] is True
    fake.void(auth_of(engine, drop, "buyer-03"), "issuer-or-paypal-void")
    p = engine.plan(drop)
    assert plan_token(p) == "void:2" and p["checked_at_paypal"] is True
    assert p["changed"] == [{"buyer": "buyer-03", "paypal_status": "VOIDED", "verdict": "gone"}]
    assert "no longer open at PayPal" in p["summary"]
    out = engine.settle(drop, expect="void:2")
    assert out["outcome"] == "not_funded" and out["totals"]["gross"] == "0.00" and not fake_state(fake)["captures"]
    assert V.verify(drop, engine)["ok"] and all_statuses(fake) == ["VOIDED"]


def test_b_settle_refuses_an_approved_plan_that_no_longer_matches_and_captures_nothing_on_it(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    approved = plan_token(engine.plan(drop))
    assert approved == "capture:3:40.00:120.00"
    victim = auth_of(engine, drop, "buyer-03")
    fake.void(victim, "paypal-voided-it-after-the-person-approved")
    e = refused(engine.settle, drop, expect=approved)
    assert e.code == "plan_changed" and e.ctx["expected"] == approved and e.ctx["actual"] == "void:2"      # the new token to approve
    assert not fake_state(fake)["captures"] and sorted(pp_status(fake, auth_of(engine, drop, b)) for b in ("buyer-01", "buyer-02")) \
        == ["CREATED", "CREATED"]                                           # nothing captured or released on the old plan
    assert not evs(engine, drop, "settlement_started")
    hv = evs(engine, drop, "hold_voided")[0]                                # ... but PayPal's word about the hold is on record
    assert hv["auth_id"] == victim and hv["paypal_status"] == "VOIDED" and hv["reason"] == "paypal_says_voided"
    assert engine.settle(drop, expect="void:2")["outcome"] == "not_funded"  # a person approves the new plan and it runs


def test_b_through_the_bundle_the_refusal_names_the_new_token(env):
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 3)
    tb.drop_close(d)
    approved = tb.drop_plan(d)["expect"]
    fake = FakePayPal(str(env / "crowd.db.fakepaypal.json"))
    fake.void(tb._eng().buyers(d)[2]["auth_id"], "paypal")
    e = togi_err(tb.drop_settle, d, approved)
    assert e.code == "plan_changed" and e.status == "refused" and "void:2" in e.next and approved in e.message
    assert tb.drop_status(d)["state"] == "closed"
    assert tb.drop_settle(d, tb.drop_plan(d)["expect"])["outcome"] == "not_funded"


def test_b_a_capture_made_outside_the_engine_is_never_captured_on_top_and_verify_says_to_refund_it(engine, drop, fake):
    """r5: a partial capture of 4.00 taken from a 40.00 hold between close and settle (the PayPal dashboard); settle then captured
    36.00 on top and verify said ok. The stray capture is named, left alone, and the hold leaves the crowd."""
    join_many(engine, drop, 5)
    engine.close(drop)
    tok = plan_token(engine.plan(drop))
    ada = auth_of(engine, drop, "buyer-01")
    stray = fake.capture(ada, "4.00", "outside-engine-partial", final=False)
    assert pp_status(fake, ada) == "PARTIALLY_CAPTURED"
    assert plan_token(engine.plan(drop)) == "capture:4:36.00:144.00" != tok
    assert refused(engine.settle, drop, expect=tok).code == "plan_changed"
    assert [c["amount"] for c in fake_state(fake)["captures"].values()] == ["4.00"]          # still only the stray one
    out = engine.settle(drop, expect="capture:4:36.00:144.00")
    on_ada = [c["amount"] for c in fake_state(fake)["captures"].values() if c["auth_id"] == ada]
    assert on_ada == ["4.00"] and out["counts"] == {"holds": 5, "captured": 4, "voided": 1, "failed": 0, "left": 0}
    hv = [e for e in evs(engine, drop, "hold_voided") if e["buyer"] == "buyer-01"][0]
    assert hv["taken_outside_engine"] is True and hv["capture_ids"] == [stray["capture_id"]]
    v = V.verify(drop, engine)
    assert not v["ok"]
    text = " ".join(v["problems"])
    assert f"refund capture {stray['capture_id']}" in text and "void it" not in text


class ListsAStrayCapture(Wrap):
    """The authorisation still reads CREATED, but PayPal lists a capture on the hold's order."""

    def __init__(self, inner, order_id):
        super().__init__(inner)
        self.order_id = order_id

    def order_captures(self, order_id):
        out = self.inner.order_captures(order_id)
        if order_id == self.order_id:
            out = out + [{"capture_id": "STRAYCAP1", "status": "COMPLETED", "amount": "4.00", "currency": "USD", "auth_id": None}]
        return out


def test_b_a_capture_on_the_order_takes_the_hold_out_of_the_crowd_even_when_the_status_still_reads_open(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    victim = engine.get(drop).buyers["buyer-02"]
    eng = Engine(store, ListsAStrayCapture(fake, victim.order_id), clock)
    p = eng.plan(drop)
    assert p["holds"] == 3 and p["changed"][0]["verdict"] == "taken" and p["changed"][0]["buyer"] == "buyer-02"
    eng.settle(drop, expect=plan_token(p))
    hv = evs(engine, drop, "hold_voided")[0]
    assert hv["capture_ids"] == ["STRAYCAP1"] and hv["taken_outside_engine"] is True and hv["reason"] == "paypal_shows_a_capture"
    assert pp_status(fake, victim.auth_id) == "CREATED"                      # the engine did not touch it


class CallLog(Wrap):
    def __init__(self, inner):
        super().__init__(inner)
        self.calls = []

    def get(self, auth_id):
        self.calls.append(("get", auth_id))
        return self.inner.get(auth_id)

    def order_captures(self, order_id):
        self.calls.append(("order_captures", order_id))
        return self.inner.order_captures(order_id)

    def capture(self, auth_id, *a, **k):
        self.calls.append(("capture", auth_id))
        return self.inner.capture(auth_id, *a, **k)

    def void(self, auth_id, *a, **k):
        self.calls.append(("void", auth_id))
        return self.inner.void(auth_id, *a, **k)


def test_b_settle_reads_every_hold_and_its_order_at_paypal_before_the_first_capture(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    spy = CallLog(fake)
    eng = Engine(store, spy, clock)
    eng.settle(drop, expect=plan_token(eng.plan(drop)))
    first_capture = next(i for i, c in enumerate(spy.calls) if c[0] == "capture")
    before = spy.calls[:first_capture]
    auths = {b.auth_id for b in eng.get(drop).buyers.values()}
    orders = {b.order_id for b in eng.get(drop).buyers.values()}
    assert {a for k, a in before if k == "get"} == auths and {o for k, o in before if k == "order_captures"} == orders
    # the plan call read each hold once; settle read each hold AGAIN on its own, still before the first capture
    assert sum(1 for k, a in before if k == "get") >= 2 * len(auths) and sum(1 for k, o in before if k == "order_captures") >= 2 * len(orders)


def test_b_a_hold_that_pays_the_engine_s_own_capture_is_not_mistaken_for_a_stray_when_settle_resumes(engine, drop, fake, store, clock):
    """The lost reply of the engine's OWN capture (r4's setting): on resume PayPal shows the hold captured, once, at the final price
    for the settlement under way, so it is recovered and recorded, not dropped from the crowd as someone else's."""
    join_many(engine, drop, 3)
    engine.close(drop)
    ada = auth_of(engine, drop, "buyer-01")
    fake.lose_reply("capture", match=ada)
    assert code(engine.settle, drop, expect=plan_token(engine.plan(drop))) == "paypal_unavailable"
    p = engine.plan(drop)
    assert p["holds"] == 3 and not p.get("changed") and plan_token(p) == "capture:3:40.00:120.00"
    out = engine.settle(drop, expect=plan_token(p))
    assert out["counts"] == {"holds": 3, "captured": 3, "voided": 0, "failed": 0, "left": 0} and len(fake_state(fake)["captures"]) == 3
    assert V.verify(drop, engine)["ok"]


class OneGetRefused(Wrap):
    """403 NOT_AUTHORIZED / PERMISSION_DENIED on the status lookup of one authorisation (the account cannot read it)."""
    bad = None

    def get(self, auth_id):
        if auth_id == self.bad:
            raise P._error_from(403, {"name": "NOT_AUTHORIZED", "details": [{"issue": "PERMISSION_DENIED"}], "debug_id": "DBG403"})
        return self.inner.get(auth_id)


def test_b_an_unreadable_hold_at_settle_blocks_with_the_way_out_and_exclude_resolves_it(engine, drop, fake, store, clock):
    join_many(engine, drop, 5)
    engine.close(drop)
    tok = plan_token(engine.plan(drop))
    refusing = OneGetRefused(fake)
    victim = auth_of(engine, drop, "buyer-03")
    refusing.bad = victim
    eng = Engine(store, refusing, clock)
    assert refused(eng.plan, drop).code == "hold_unreadable"               # a plan cannot be made for a hold PayPal will not describe
    e = blocked(eng.settle, drop, expect=tok)
    assert e.ctx["phase"] == "check" and e.ctx["issue"] == "PERMISSION_DENIED" and e.ctx["debug_id"] == "DBG403"
    assert f"void hold {victim} at PayPal" in e.ctx["next"] and f"togi crowd drop exclude --drop {drop} --buyer buyer-03" in e.ctx["next"]
    assert not fake_state(fake)["captures"] and not evs(engine, drop, "settlement_started")
    eng.exclude(drop, "buyer-03")                                           # the person takes that one buyer out
    assert pp_status(fake, victim) == "VOIDED"
    new = plan_token(eng.plan(drop))
    assert new == "capture:4:36.00:144.00"                                  # the locked step stays; four buyers are charged
    assert eng.settle(drop, expect=new)["counts"]["captured"] == 4


# ---- verify reads every capture on every order -------------------------------------------------------------------------------

def stray_capture(fake, auth_id, capture_id="FKCSTRAY1", amount="4.00", currency="USD", status="COMPLETED"):
    def add(st):
        st["captures"][capture_id] = {"capture_id": capture_id, "status": status, "amount": amount, "currency": currency,
                                      "fee": "0.00", "net": amount, "final": False, "created": "2026-11-01T12:00:00Z",
                                      "auth_id": auth_id}
    edit_fake(fake, add)
    return capture_id


def test_b_verify_fails_on_a_capture_the_engine_did_not_record_and_on_a_total_above_the_final_price(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    engine.settle(drop)
    assert V.verify(drop, engine)["ok"]
    stray_capture(fake, auth_of(engine, drop, "buyer-01"))
    v = V.verify(drop, engine)
    assert not v["ok"]
    text = " ".join(v["problems"])
    assert "FKCSTRAY1" in text and "did not record" in text and "refund capture FKCSTRAY1" in text
    assert "44.00 in total" in text and "40.00" in text                      # 40.00 recorded + 4.00 stray, above the final price
    assert any(c["check"].startswith("every capture on the drop's orders") and not c["ok"] for c in v["checks"])


def test_b_a_capture_that_has_been_refunded_or_declined_is_not_a_charge(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    engine.settle(drop)
    stray_capture(fake, auth_of(engine, drop, "buyer-01"), "FKCREFUNDED", status="REFUNDED")
    stray_capture(fake, auth_of(engine, drop, "buyer-02"), "FKCDECLINED", status="DECLINED")
    assert V.verify(drop, engine)["ok"]                                      # a person's refund puts verify right


def test_b_a_missing_currency_is_a_mismatch_not_a_pass(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    engine.settle(drop)
    ada = auth_of(engine, drop, "buyer-01")
    cap = fake_state(fake)["auths"][ada]["capture_id"]
    edit_fake(fake, lambda st: st["auths"][ada].update(currency=None))               # PayPal states no currency for the hold
    v = V.verify(drop, engine)
    assert not v["ok"] and any("buyer-01" in p and "engine recorded" in p for p in v["problems"])
    edit_fake(fake, lambda st: st["auths"][ada].update(currency="USD"))
    assert V.verify(drop, engine)["ok"]
    edit_fake(fake, lambda st: st["captures"][cap].update(currency=None))            # ... or for the capture
    v = V.verify(drop, engine)
    assert not v["ok"] and any("unstated currency" in p for p in v["problems"])


class NoCurrency(Wrap):
    def get(self, auth_id):
        out = dict(self.inner.get(auth_id))
        out.pop("currency", None)
        return out


def test_b_a_hold_that_comes_back_without_a_currency_is_not_recorded_on_a_replayed_join(engine, drop, fake, store, clock):
    fake.lose_reply("authorize", match=":ada")
    assert code(engine.join, drop, "ada") == "paypal_unavailable"
    ada = next(a for a in fake_state(fake)["auths"])
    assert code(Engine(store, NoCurrency(fake), clock).join, drop, "ada") == "hold_not_open"
    assert engine.get(drop).count == 0 and not evs(engine, drop, "hold_placed")
    assert pp_status(fake, ada) == "VOIDED" and evs(engine, drop, "orphan_hold_released")[0]["reason"] == "join_unconfirmed"


def test_b_the_real_client_lists_every_capture_on_an_order(holds, net):
    net.reply({"id": "ORD1", "purchase_units": [
        {"payments": {"captures": [{"id": "C1", "status": "COMPLETED", "final_capture": True,
                                    "amount": {"value": "36.00", "currency_code": "USD"}}]}},
        {"payments": {"captures": [{"id": "C2", "status": "PENDING", "amount": {"value": "4.00", "currency_code": "USD"}}]}}]})
    out = holds.order_captures("ORD1")
    assert [(c["capture_id"], c["amount"], c["currency"], c["status"]) for c in out] == \
        [("C1", "36.00", "USD", "COMPLETED"), ("C2", "4.00", "USD", "PENDING")]
    assert net.sent[0]["method"] == "GET" and net.sent[0]["url"].endswith("/v2/checkout/orders/ORD1")
    net.reply({"id": "ORD2"})
    assert holds.order_captures("ORD2") == []


def test_b_the_approval_says_the_plan_was_checked_against_paypal_and_settle_checks_again(env):
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 3)
    tb.drop_close(d)
    plan = tb.drop_plan(d)
    assert plan["checked_at_paypal"] is True and "read at PayPal just now" in plan["checked"] and "again before any money moves" in plan["checked"]
    cmd = tb.bundle.commands["drop settle"] if hasattr(tb.bundle, "commands") else None
    summary = getattr(cmd, "summary", None) if cmd is not None and not isinstance(cmd, dict) else (cmd or {}).get("summary")
    text = summary or open(os.path.join(ROOT, "src", "crowd", "togi_bundle.py")).read()
    assert "checked against PayPal when made" in text and "re-checks every hold first" in text


def test_b_a_hold_lost_since_close_is_counted_out_of_the_crowd_by_the_projection(engine, drop, fake, store):
    join_many(engine, drop, 4)
    engine.close(drop)
    assert engine.get(drop).count == 4 and engine.get(drop).lost_after_close == 0
    fake.void(auth_of(engine, drop, "buyer-04"), "x")
    engine.settle(drop, expect="capture:3:40.00:120.00")
    d = engine.get(drop)
    assert d.lost_after_close == 1 and d.settlement["count"] == 3 and d.count == 3        # frozen at the revalidated crowd


# ===== C. classification and the breaker (r1, r3a, r3b, r2, r13) =============================================================

UE = "UNPROCESSABLE_ENTITY"
TABLE = [
    (422, {"name": UE, "details": [{"issue": "TRANSACTION_REFUSED"}]}, True),
    (400, {"name": "INVALID_REQUEST", "details": [{"issue": "TRANSACTION_REFUSED"}]}, False),        # a systemic top-level name wins
    (422, {"name": "PAYEE_ACCOUNT_RESTRICTED", "details": [{"issue": "TRANSACTION_REFUSED"}]}, False),
    (403, {"name": "NOT_AUTHORIZED", "details": [{"issue": "TRANSACTION_REFUSED"}]}, False),
    (409, {"name": "SOMETHING_NEW", "details": [{"issue": "TRANSACTION_REFUSED"}]}, False),         # a name nobody has seen
    (422, {"name": UE, "details": [{"issue": "TRANSACTION_REFUSED"}, {"issue": ""}]}, False),       # a detail that names nothing
    (422, {"name": UE, "details": [{"issue": "TRANSACTION_REFUSED"}, {"description": "x"}]}, False),
    (422, {"name": UE, "details": ["PAYEE_ACCOUNT_RESTRICTED", {"issue": "TRANSACTION_REFUSED"}]}, False),
    (422, {"name": UE, "details": "TRANSACTION_REFUSED"}, False),
    (422, {"name": UE}, False),                                                                       # UNPROCESSABLE_ENTITY, no detail
    (422, {"name": UE, "details": []}, False),
    (422, {"name": UE, "details": [{"issue": "PAYER_ACCOUNT_LOCKED_OR_CLOSED"}]}, True),             # buyer-level (PayPal's docs)
    (422, {"name": UE, "details": [{"issue": "PAYER_ACCOUNT_RESTRICTED"}]}, True),
    (422, {"name": UE, "details": [{"issue": "REDIRECT_PAYER_FOR_ALTERNATE_FUNDING"}]}, True),
    (422, {"name": "INSTRUMENT_DECLINED"}, True),                                                     # the buyer name as the top-level one
    (402, {"name": "", "details": [{"issue": "INSTRUMENT_DECLINED"}]}, True),
    (422, {"name": UE, "details": [{"issue": "SOMETHING_NEW"}]}, False),
    (404, {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]}, False),
    (403, {"name": UE, "details": [{"issue": "TRANSACTION_REFUSED"}]}, False),                        # any 403
    (401, {"name": UE, "details": [{"issue": "TRANSACTION_REFUSED"}]}, False),
]


@pytest.mark.parametrize("http,body,expected", TABLE)
def test_c_a_refusal_is_buyer_specific_only_if_the_top_level_name_and_every_detail_say_so(http, body, expected):
    e = P._error_from(http, body)
    assert P.buyer_specific(e) is expected, (body, e.name, e.detail_names)


def test_c_the_error_carries_what_paypal_called_it_at_the_top_and_in_each_detail():
    e = P._error_from(422, {"name": UE, "details": [{"issue": "TRANSACTION_REFUSED"}, {"description": "x"}]})
    assert e.name == UE and e.detail_names == ["TRANSACTION_REFUSED", None] and e.issues == ["TRANSACTION_REFUSED"]


def test_c_two_failures_in_a_row_of_different_buyer_codes_stop_settle_before_any_release(engine, drop, fake):
    """r1: the refusals alternate between two allow-listed names, so 'the same code twice' never fired; every capture failed, every
    hold was released, settle reported settled and verify said ok."""
    tok = closed_drop(engine, drop, 6)
    fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, "buyer-01"), times=None)
    fake.refuse("capture", "INSTRUMENT_DECLINED", match=auth_of(engine, drop, "buyer-02"), times=None)
    e = blocked(engine.settle, drop, expect=tok)
    assert e.ctx["issue"] == "captures_failing_in_a_row" and "buyer-01" in e.ctx["buyer"] and "buyer-02" in e.ctx["buyer"]
    assert all_statuses(fake) == ["CREATED"] and not fake_state(fake)["captures"]            # nobody captured, nobody released
    assert not evs(engine, drop, "failed_hold_released") and engine.get(drop).state == "settling"
    assert V.verify(drop, engine)["ok"] is False


def test_c_more_than_half_of_the_captures_failing_stops_settle_once_four_were_tried(engine, drop, fake):
    """r1's variant: a success between every pair of failures resets 'in a row' forever."""
    tok = closed_drop(engine, drop, 6)
    for n in ("buyer-01", "buyer-03", "buyer-05"):
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    e = blocked(engine.settle, drop, expect=tok)
    assert e.ctx["issue"] == "most_captures_failing"
    assert [b.status for b in engine.get(drop).buyers.values()].count("held") == 1           # the sixth was never tried
    assert not evs(engine, drop, "failed_hold_released")                                     # and nothing was released
    assert evs(engine, drop, "settle_blocked")[0]["buyers"] == ["buyer-01", "buyer-03", "buyer-05"]


def test_c_exactly_half_or_failures_a_capture_apart_below_four_attempts_do_not_stop_settle(engine, drop, fake):
    tok = closed_drop(engine, drop, 6)
    for n in ("buyer-01", "buyer-04"):          # F ok ok F ok ok: two of six, never in a row, never more than half
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    assert engine.settle(drop, expect=tok)["counts"]["failed"] == 2


def test_c_a_lapsed_authorisation_is_a_fact_about_one_hold_and_never_counts_toward_the_breaker(engine, drop, fake):
    """r2: PayPal lapses holds early; every hold lapsing once used to need a fresh person approval per two buyers."""
    tok = closed_drop(engine, drop, 8)
    for n in ("buyer-01", "buyer-02", "buyer-03", "buyer-04"):
        fake.refuse("capture", "AUTHORIZATION_EXPIRED", match=auth_of(engine, drop, n), times=None)
    out = engine.settle(drop, expect=tok)
    assert out["counts"]["failed"] == 4 and out["counts"]["captured"] == 4 and not evs(engine, drop, "settle_blocked")
    assert V.verify(drop, engine)["ok"]


def test_c_the_engine_s_own_hold_expired_does_not_count_either(engine, drop, fake):
    closed_drop(engine, drop, 4)
    for n in ("buyer-01", "buyer-02", "buyer-03"):
        engine.store.append(drop, "capture_failed", {"buyer": n, "auth_id": auth_of(engine, drop, n), "code": "hold_expired",
                                                    "paypal_status": "EXPIRED"})
    engine._check_breaker(engine.get(drop))                                                  # no block


def test_c_the_count_starts_again_after_a_block_so_a_person_who_looked_is_not_stopped_by_what_they_saw(engine, drop, fake):
    tok = closed_drop(engine, drop, 6)
    for n in ("buyer-01", "buyer-02"):
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    blocked(engine.settle, drop, expect=tok)
    out = engine.settle(drop, expect=plan_token(engine.plan(drop)))          # the same two failures: already looked at
    assert out["counts"]["failed"] == 2 and out["counts"]["captured"] == 4 and open_auths(fake) == {}


def test_c_a_buyer_whose_account_paypal_says_is_locked_is_recorded_against_them_and_settle_goes_on(engine, drop, fake):
    """r3b: PAYER_ACCOUNT_LOCKED_OR_CLOSED was not on the list, so one locked buyer stopped every settle, forever."""
    tok = closed_drop(engine, drop, 5)
    bob = auth_of(engine, drop, "buyer-02")
    for issue in ("PAYER_ACCOUNT_LOCKED_OR_CLOSED",):
        fake.refuse("capture", issue, match=bob, times=None)
    out = engine.settle(drop, expect=tok)
    assert out["counts"] == {"holds": 5, "captured": 4, "voided": 0, "failed": 1, "left": 0}
    assert pp_status(fake, bob) == "VOIDED" and V.verify(drop, engine)["ok"]
    assert evs(engine, drop, "capture_failed")[0]["issue"] == "PAYER_ACCOUNT_LOCKED_OR_CLOSED"


class KillAfterNthFailure(Store):
    """The process dies right after the Nth `capture_failed` has been written (before anything looks at it)."""

    def __init__(self, path, n):
        super().__init__(path)
        self.n, self.seen = n, 0

    def append(self, drop_id, type, payload=None, ts=None):
        out = super().append(drop_id, type, payload, ts)
        if type == "capture_failed":
            self.seen += 1
            if self.seen == self.n:
                raise KeyboardInterrupt("killed right after the 2nd failed capture was written")
        return out


def test_c_the_breaker_is_checked_again_on_resume_so_a_crash_before_the_block_cannot_skip_it(engine, drop, fake, store, clock):
    """r13: the check ran only after a NEW failure was written, so a crash between the second failure and the block went straight to
    the release pass on resume and voided both holds."""
    tok = closed_drop(engine, drop, 4)
    for n in ("buyer-03", "buyer-04"):
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    with pytest.raises(KeyboardInterrupt):
        Engine(KillAfterNthFailure(store.path, 2), fake, clock).settle(drop, expect=tok)
    assert [e["type"] for e in store.events(drop) if e["type"] in ("capture_failed", "settle_blocked")] == ["capture_failed"] * 2
    resumed = blocked(engine.settle, drop, expect=plan_token(engine.plan(drop)))
    assert resumed.ctx["phase"] == "capture"
    assert pp_status(fake, auth_of(engine, drop, "buyer-03")) == "CREATED" == pp_status(fake, auth_of(engine, drop, "buyer-04"))   # not voided
    assert not evs(engine, drop, "failed_hold_released")


def test_c_a_block_names_the_exact_manual_action_and_says_so_when_the_same_buyer_blocks_again(engine, drop, fake):
    tok = closed_drop(engine, drop, 5)
    bob = auth_of(engine, drop, "buyer-02")
    fake.refuse("capture", "NOT_AUTHORIZED", http=403, match=bob, times=None)
    e = blocked(engine.settle, drop, expect=tok)
    assert f"void hold {bob} at PayPal" in e.ctx["next"] and f"togi crowd drop exclude --drop {drop} --buyer buyer-02" in e.ctx["next"]
    assert "stopped settlement before" not in e.ctx["next"]
    again = blocked(engine.settle, drop, expect=plan_token(engine.plan(drop)))
    assert "buyer-02 has stopped settlement before" in again.ctx["next"] and "exclude" in again.ctx["next"]
    assert evs(engine, drop, "settle_blocked")[-1]["buyer"] == "buyer-02"


def test_c_the_breaker_s_block_names_every_buyer_to_look_at(engine, drop, fake):
    tok = closed_drop(engine, drop, 6)
    for n in ("buyer-01", "buyer-02"):
        fake.refuse("capture", "TRANSACTION_REFUSED", match=auth_of(engine, drop, n), times=None)
    e = blocked(engine.settle, drop, expect=tok)
    nxt = e.ctx["next"]
    assert auth_of(engine, drop, "buyer-01") in nxt and auth_of(engine, drop, "buyer-02") in nxt and "drop exclude" in nxt


def test_c_explain_describes_a_breaker_stop_as_the_engine_s_own_not_as_a_paypal_answer(env):
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36,7:33")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 6)
    tb.drop_close(d)
    tok = tb.drop_plan(d)["expect"]
    fake = FakePayPal(str(env / "crowd.db.fakepaypal.json"))
    for i, issue in ((0, "TRANSACTION_REFUSED"), (1, "INSTRUMENT_DECLINED")):
        fake.refuse("capture", issue, match=tb._eng().buyers(d)[i]["auth_id"], times=None)
    assert togi_err(tb.drop_settle, d, tok).code == "settle_blocked"
    text = tb.drop_explain(d)["text"]
    assert "2 captures in a row failed (TRANSACTION_REFUSED, INSTRUMENT_DECLINED)" in text and "PayPal answered" not in text
    assert "{" not in text


def test_c_explain_still_quotes_paypal_s_answer_for_a_paypal_refusal(env):
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 5)
    tb.drop_close(d)
    tok = tb.drop_plan(d)["expect"]
    FakePayPal(str(env / "crowd.db.fakepaypal.json")).refuse("capture", "NOT_AUTHORIZED", http=403, match=tb._eng().buyers(d)[1]["auth_id"], times=None)
    togi_err(tb.drop_settle, d, tok)
    assert "PayPal answered NOT_AUTHORIZED" in tb.drop_explain(d)["text"]


def test_c_through_the_bundle_a_block_shows_the_manual_action_in_next(env):
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 5)
    tb.drop_close(d)
    tok = tb.drop_plan(d)["expect"]
    bob = tb._eng().buyers(d)[1]
    FakePayPal(str(env / "crowd.db.fakepaypal.json")).refuse("capture", "NOT_AUTHORIZED", http=403, match=bob["auth_id"], times=None)
    e = togi_err(tb.drop_settle, d, tok)
    assert e.code == "settle_blocked" and e.status == "refused" and "{" not in e.message + e.next
    assert f"void hold {bob['auth_id']} at PayPal" in e.next and f"togi crowd drop exclude --drop {d} --buyer sim-002" in e.next
    out = tb.drop_exclude(d, "sim-002")
    assert out["excluded"] is True and out["released"] is True and out["next"] == f"togi crowd drop plan --drop {d}"
    assert tb.drop_settle(d, tb.drop_plan(d)["expect"])["counts"]["captured"] == 4


# ---- drop exclude ----------------------------------------------------------------------------------------------------------

def test_c_exclude_releases_the_hold_records_the_exclusion_and_the_buyer_cannot_rejoin(engine, drop, fake):
    tok = closed_drop(engine, drop, 5)
    bob = auth_of(engine, drop, "buyer-02")
    out = engine.exclude(drop, "buyer-02")
    assert out["excluded"] is True and out["released"] is True and pp_status(fake, bob) == "VOIDED"
    kinds = [e["type"] for e in engine.store.events(drop) if e["type"] in ("buyer_excluded", "hold_voided")]
    assert kinds == ["buyer_excluded", "hold_voided"] and evs(engine, drop, "hold_voided")[0]["reason"] == "excluded"
    assert engine.get(drop).buyers["buyer-02"].status == "excluded" and engine.get(drop).buyers["buyer-02"].excluded
    assert refused(engine.settle, drop, expect=tok).code == "plan_changed"                   # the plan changed: a person approves it
    new = plan_token(engine.plan(drop))
    assert new == "capture:4:36.00:144.00"
    out = engine.settle(drop, expect=new)
    assert out["counts"] == {"holds": 5, "captured": 4, "voided": 1, "failed": 0, "left": 0} and V.verify(drop, engine)["ok"]
    assert code(engine.exclude, drop, "buyer-02") == "cannot_exclude"                        # settled now


def test_c_an_excluded_buyer_cannot_rejoin_a_live_drop(engine, drop, fake):
    join_many(engine, drop, 4)
    engine.exclude(drop, "buyer-04")
    assert code(engine.join, drop, "Buyer 04") == "buyer_excluded"
    assert engine.get(drop).count == 3 and code(engine.exclude, drop, "buyer-04") == "already_excluded"


def test_c_if_paypal_refuses_the_release_the_buyer_is_still_excluded_and_the_open_hold_is_on_record(engine, drop, fake):
    closed_drop(engine, drop, 5)
    bob = auth_of(engine, drop, "buyer-02")
    fake.refuse("void", "NOT_AUTHORIZED", http=403, match=bob, times=None)
    out = engine.exclude(drop, "buyer-02")
    assert out["excluded"] is True and out["released"] is False and f"void hold {bob} at PayPal by hand" in out["next"]
    assert [e["type"] for e in engine.store.events(drop) if e["type"] in ("buyer_excluded", "void_failed")] == ["buyer_excluded", "void_failed"]
    assert pp_status(fake, bob) == "CREATED"
    engine.settle(drop, expect=plan_token(engine.plan(drop)))
    v = V.verify(drop, engine)
    assert not v["ok"] and any("buyer-02" in p and bob in p and "void it by hand" in p for p in v["problems"])


def test_c_exclude_on_a_hold_that_is_already_captured_is_a_conflict_and_records_nothing(engine, drop, fake):
    join_many(engine, drop, 3)
    fake.capture(auth_of(engine, drop, "buyer-02"), D("40"), "someone-else")
    before = len(engine.store.events(drop))
    assert code(engine.exclude, drop, "buyer-02") == "void_conflict"
    assert len(engine.store.events(drop)) == before


def test_c_exclude_does_not_record_anything_while_paypal_is_down(engine, drop, fake):
    join_many(engine, drop, 3)
    fake.unavailable("void", match=auth_of(engine, drop, "buyer-02"))
    assert code(engine.exclude, drop, "buyer-02") == "paypal_unavailable"
    assert not evs(engine, drop, "buyer_excluded") and engine.get(drop).buyers["buyer-02"].status == "held"


def test_c_exclude_guards(engine, drop):
    assert code(engine.exclude, drop, "nobody") == "buyer_not_found"
    d = engine.create_drop("Draft", "3:40,5:36", 3, 60)
    assert code(engine.exclude, d, "x") == "cannot_exclude"
    join_many(engine, drop, 3)
    engine.leave(drop, "buyer-03")
    assert code(engine.exclude, drop, "buyer-03") == "no_active_hold"


def test_c_a_failed_captures_hold_that_cannot_be_released_is_resolved_by_excluding_that_buyer(engine, drop, fake):
    join_many(engine, drop, 4)
    engine.join(drop, "Card Nine", card_index=P.DECLINE_CARD_INDEX)
    engine.close(drop)
    nine = auth_of(engine, drop, "card-nine")
    fake.refuse("void", "NOT_AUTHORIZED", http=403, match=nine, times=None)
    e = blocked(engine.settle, drop, expect=plan_token(engine.plan(drop)))
    assert e.ctx["phase"] == "release" and "exclude --drop" in e.ctx["next"] and "--buyer card-nine" in e.ctx["next"]
    out = engine.exclude(drop, "card-nine")
    assert out["excluded"] is True and out["released"] is False                               # the same 403 on the release
    settled = engine.settle(drop, expect=plan_token(engine.plan(drop)))
    assert settled["state"] == "settled" and settled["counts"]["captured"] == 4
    assert [e["type"] for e in engine.store.events(drop) if e["type"] in ("buyer_excluded", "void_failed")][:1] == ["buyer_excluded"]


# ===== D. recovery and replays (r4, r8) ======================================================================================

class FlakyRecover(Wrap):
    """The lookup that finds a capture whose reply was lost fails once with an outage (or a refusal)."""

    def __init__(self, inner, error):
        super().__init__(inner)
        self.error = error

    def recover_capture(self, order_id):
        if self.error is not None:
            err, self.error = self.error, None
            raise err
        return self.inner.recover_capture(order_id)


def lost_capture_then_a_later_resume(engine, drop, fake, clock):
    join_many(engine, drop, 3)
    engine.close(drop)
    fake.lose_reply("capture", match=auth_of(engine, drop, "buyer-01"))      # the capture goes through, the reply is lost
    assert code(engine.settle, drop, expect=plan_token(engine.plan(drop))) == "paypal_unavailable"
    clock.advance(days=30)                                                   # resumed later: our clock now passes the expiry


def test_d_an_outage_while_looking_for_a_capture_resumes_later_it_is_never_read_as_not_found(engine, drop, fake, store, clock):
    """r4: the lookup swallowed every error, the caller read None as 'no capture of the final price can be found' and wrote
    settle_blocked{captured_not_as_approved}: an outage became a person-block asserting something PayPal never said."""
    lost_capture_then_a_later_resume(engine, drop, fake, clock)
    flaky = Engine(store, FlakyRecover(fake, P.PayPalUnavailable(message="outage on GET order")), clock)
    assert code(flaky.settle, drop, expect=plan_token(flaky.plan(drop))) == "paypal_unavailable"
    assert not evs(engine, drop, "settle_blocked") and engine.get(drop).blocked is None
    out = flaky.settle(drop, expect=plan_token(flaky.plan(drop)))            # PayPal answers: the capture is found and recorded
    assert out["counts"]["captured"] == 3 and len(fake_state(fake)["captures"]) == 3 and V.verify(drop, engine)["ok"]
    assert [e for e in evs(engine, drop, "hold_captured") if e.get("recovered")][0]["buyer"] == "buyer-01"


def test_d_a_refusal_to_the_lookup_itself_stops_settle_for_a_person_with_the_truth_in_the_message(engine, drop, fake, store, clock):
    lost_capture_then_a_later_resume(engine, drop, fake, clock)
    refusing = Engine(store, FlakyRecover(fake, P._error_from(403, {"name": "NOT_AUTHORIZED", "details": [{"issue": "PERMISSION_DENIED"}]})), clock)
    blocked(refusing.settle, drop, expect=plan_token(refusing.plan(drop)))
    block = evs(engine, drop, "settle_blocked")[0]
    assert block["code"] == "PERMISSION_DENIED" and "would not say which capture" in block["message"]
    assert "captured_not_as_approved" not in json.dumps(evs(engine, drop, "settle_blocked"))


def test_d_a_not_found_answer_is_a_real_answer_and_still_blocks_as_before(engine, drop, fake, store, clock):
    lost_capture_then_a_later_resume(engine, drop, fake, clock)
    nf = Engine(store, FlakyRecover(fake, P._error_from(404, {"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})), clock)
    blocked(nf.settle, drop, expect=plan_token(nf.plan(drop)))
    assert evs(engine, drop, "settle_blocked")[0]["code"] == "captured_not_as_approved"


class GetLog(Wrap):
    def __init__(self, inner):
        super().__init__(inner)
        self.gets = []

    def get(self, auth_id):
        self.gets.append(auth_id)
        return self.inner.get(auth_id)


def forget_request(fake, rid):
    """PayPal's idempotency window for this request id has passed."""
    edit_fake(fake, lambda st: (st["requests"].pop(rid, None), st["order_rids"].pop(rid, None)))


def test_d_a_retry_that_gets_a_different_order_is_a_new_hold_and_the_earlier_one_is_released(engine, drop, fake, store, clock):
    """r8: a join's reply is lost (hold #1); the retry reaches PayPal after it forgot the request id, so it makes a NEW order and a
    new hold. The engine called it a replay of hold #1, never reconciled #1, and the buyer carried two holds through the drop."""
    join_many(engine, drop, 2)
    fake.lose_reply("authorize", match=":cy")
    assert code(engine.join, drop, "cy") == "paypal_unavailable"
    rid = next(iter(engine.get(drop).unresolved))
    first = fake_state(fake)["requests"][rid]
    forget_request(fake, rid)
    spy = GetLog(fake)
    r = Engine(store, spy, clock).join(drop, "cy")
    assert r["auth_id"] != first["auth_id"] and r["auth_id"] in spy.gets                     # confirmed with a GET before it was recorded
    hp = evs(engine, drop, "hold_placed")[-1]
    assert "replayed" not in hp and hp["supersedes_order"] == first["order_id"] and hp["confirmed_status"] == "CREATED"
    assert hp["order_id"] != first["order_id"]
    assert pp_status(fake, first["auth_id"]) == "VOIDED" and pp_status(fake, r["auth_id"]) == "CREATED"      # one open hold for cy
    rel = evs(engine, drop, "orphan_hold_released")[0]
    assert rel["superseded"] is True and rel["auth_id"] == first["auth_id"] and rel["order_id"] == first["order_id"]
    assert engine.get(drop).unresolved == {} and engine.get(drop).buyers["cy"].auth_id == r["auth_id"]
    engine.close(drop)
    out = engine.settle(drop, expect=plan_token(engine.plan(drop)))
    assert out["counts"]["captured"] == 3 and V.verify(drop, engine)["ok"] and open_auths(fake) == {}


def test_d_the_earlier_orders_void_does_not_reuse_the_request_id_of_the_new_holds_void(engine, drop, fake, store, clock):
    """PayPal answers a repeated PayPal-Request-Id with the first answer: two different holds of one intent must not share one."""
    join_many(engine, drop, 2)
    fake.lose_reply("authorize", match=":cy")
    assert code(engine.join, drop, "cy") == "paypal_unavailable"
    rid = next(iter(engine.get(drop).unresolved))
    first = fake_state(fake)["requests"][rid]
    forget_request(fake, rid)
    voids = []

    class RecordsVoidIds(Wrap):
        def void(self, auth_id, request_id):
            voids.append((auth_id, request_id))
            return self.inner.void(auth_id, request_id)

    eng = Engine(store, RecordsVoidIds(fake), clock)
    second = eng.join(drop, "cy")["auth_id"]
    eng.leave(drop, "cy")                                                    # the new hold's own void, later
    assert [a for a, _ in voids] == [first["auth_id"], second]
    assert len({r for _, r in voids}) == 2 and all(len(r) <= 108 for _, r in voids)


def test_d_a_retry_that_gets_the_same_order_back_is_still_a_replay(engine, drop, fake):
    fake.lose_reply("authorize", match=":cy")
    assert code(engine.join, drop, "cy") == "paypal_unavailable"
    engine.join(drop, "cy")
    hp = evs(engine, drop, "hold_placed")[-1]
    assert hp["replayed"] is True and "supersedes_order" not in hp and not evs(engine, drop, "orphan_hold_released")


def test_d_if_the_earlier_hold_cannot_be_released_yet_the_join_stays_open_and_close_releases_both(engine, drop, fake, store, clock):
    join_many(engine, drop, 2)
    fake.lose_reply("authorize", match=":cy")
    assert code(engine.join, drop, "cy") == "paypal_unavailable"
    rid = next(iter(engine.get(drop).unresolved))
    first = fake_state(fake)["requests"][rid]
    forget_request(fake, rid)
    fake.unavailable("void", match=first["auth_id"])
    assert code(engine.join, drop, "cy") == "paypal_unavailable"             # the new hold exists, the old one could not be released
    assert len(evs(engine, drop, "hold_placed")) == 2 and "cy" not in engine.get(drop).buyers
    assert len(engine.get(drop).unresolved[rid]["order_ids"]) == 2           # the intent knows both orders
    out = engine.close(drop)
    assert out["buyers"] == 2 and sorted(a for a in all_statuses(fake)) == ["CREATED", "VOIDED"]
    assert all(pp_status(fake, a) == "VOIDED" for a, rec in fake_state(fake)["auths"].items() if rec["label"].endswith(":cy"))
    engine.settle(drop, expect=plan_token(engine.plan(drop)))
    assert V.verify(drop, engine)["ok"] and engine.get(drop).unresolved == {}


def test_d_reconcile_releases_the_hold_of_every_order_one_intent_made(engine, drop, fake):
    join_many(engine, drop, 3)
    fake.lose_reply("authorize", match=":cy")
    assert code(engine.join, drop, "cy") == "paypal_unavailable"
    rid = next(iter(engine.get(drop).unresolved))
    forget_request(fake, rid)
    fake.lose_reply("authorize", match=":cy")
    assert code(engine.join, drop, "cy") == "paypal_unavailable"             # a second order, a second hold, both unrecorded
    cy = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":cy")]
    assert len(cy) == 2 and all(pp_status(fake, k) == "CREATED" for k in cy)
    out = engine.close(drop)
    assert out["buyers"] == 3 and all(pp_status(fake, k) == "VOIDED" for k in cy)
    assert engine.settle(drop, expect=plan_token(engine.plan(drop)))["counts"]["captured"] == 3


# ===== E. leaves that hit an error (r7, D-11b) ===============================================================================

def test_e_a_leave_asked_for_before_the_deadline_is_honoured_even_though_paypal_was_down(engine, drop, fake, clock):
    """r7: the outage left leave_started open, the deadline passed (leave is refused after it), close read CREATED, called it
    leave_failed, kept the buyer, and the buyer who last said 'leave' was charged and funded the drop."""
    join_many(engine, drop, 3)
    clock.advance(minutes=59)
    victim = auth_of(engine, drop, "buyer-03")
    fake.unavailable("void", match=victim)
    assert code(engine.leave, drop, "buyer-03") == "paypal_unavailable"
    clock.advance(minutes=2)
    assert code(engine.leave, drop, "buyer-03") == "drop_ended"
    c = engine.close(drop)
    assert c["buyers"] == 2 and c["minimum_met"] is False and c["reconciled"]["leaves_completed"] == ["buyer-03"]
    assert pp_status(fake, victim) == "VOIDED" and engine.get(drop).buyers["buyer-03"].status == "left"
    out = engine.settle(drop, expect=plan_token(engine.plan(drop)))
    assert out["outcome"] == "not_funded" and engine.get(drop).buyers["buyer-03"].captured is None
    assert V.verify(drop, engine)["ok"] and not fake_state(fake)["captures"]


def test_e_the_honoured_leave_is_dated_when_it_was_asked(engine, drop, fake, clock):
    join_many(engine, drop, 4)
    asked = clock.t
    fake.unavailable("void", match=auth_of(engine, drop, "buyer-04"))
    assert code(engine.leave, drop, "buyer-04") == "paypal_unavailable"
    clock.advance(hours=3)
    engine.close(drop)
    left = [e for e in engine.store.events(drop) if e["type"] == "buyer_left"][0]
    assert S.parse_iso(left["ts"]) == asked and engine.get(drop).closed["count"] == 3


def test_e_if_paypal_still_refuses_at_close_the_buyer_is_out_anyway_and_the_open_hold_is_reported(engine, drop, fake):
    join_many(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-04")
    fake.refuse("void", "NOT_AUTHORIZED", http=403, match=victim, times=None)
    assert code(engine.leave, drop, "buyer-04") == "leave_pending"
    c = engine.close(drop)
    assert c["buyers"] == 3 and c["reconciled"]["leaves_completed"] == ["buyer-04"]
    assert [e["type"] for e in engine.store.events(drop) if e["type"] in ("buyer_left", "void_failed")] == ["buyer_left", "void_failed"]
    out = engine.settle(drop, expect=plan_token(engine.plan(drop)))
    assert out["counts"]["captured"] == 3 and out["unreleased"] == 1 and engine.get(drop).buyers["buyer-04"].captured is None
    v = V.verify(drop, engine)
    assert not v["ok"] and any("buyer-04" in p and victim in p and "void it by hand" in p for p in v["problems"])


def test_e_if_paypal_cannot_be_reached_at_close_nothing_is_decided(engine, drop, fake):
    join_many(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-04")
    fake.unavailable("void", match=victim, times=2)
    assert code(engine.leave, drop, "buyer-04") == "paypal_unavailable"
    assert code(engine.close, drop) == "paypal_unavailable" and engine.get(drop).state == "live"
    assert engine.get(drop).leaving.keys() == {"buyer-04"}
    assert engine.close(drop)["buyers"] == 3 and pp_status(fake, victim) == "VOIDED"


def test_e_a_leave_on_a_hold_that_was_captured_cannot_be_honoured(engine, drop, fake):
    join_many(engine, drop, 3)
    victim = auth_of(engine, drop, "buyer-03")
    fake.unavailable("void", match=victim)
    assert code(engine.leave, drop, "buyer-03") == "paypal_unavailable"
    fake.capture(victim, D("40"), "someone-else")
    c = engine.close(drop)
    assert c["reconciled"]["leaves_kept"] == ["buyer-03"] and evs(engine, drop, "leave_failed")[0]["reason"] == "paypal_says_captured"


def test_e_settle_also_resolves_a_leave_nobody_confirmed(engine, drop, fake, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    victim = auth_of(engine, drop, "buyer-04")
    engine.store.append(drop, "leave_started", {"buyer": "buyer-04", "auth_id": victim, "request_id": "r", "paypal_mode": "fake",
                                               "paypal_ref": fake.ref}, ts=clock.t - timedelta(minutes=5))
    out = engine.settle(drop, expect="capture:3:40.00:120.00")
    assert out["counts"]["captured"] == 3 and pp_status(fake, victim) == "VOIDED" and engine.get(drop).buyers["buyer-04"].status == "left"
    assert V.verify(drop, engine)["ok"]


def test_e_d_11b_is_in_the_decisions_log():
    text = open(os.path.join(ROOT, "docs", "DECISIONS.md")).read()
    assert "D-11b" in text and "honoured even if PayPal errored" in text


# ===== F. smaller items ======================================================================================================

def test_f_one_unreadable_hold_blocks_close_with_a_next_that_names_the_hold_and_exclude_resolves_it(engine, drop, fake, store, clock):
    """r10: one authorisation answered 403 to its status lookup, so the drop could never close, never settle, and every buyer's hold
    stayed on their card until it lapsed."""
    join_many(engine, drop, 4)
    victim = auth_of(engine, drop, "buyer-03")
    refusing = OneGetRefused(fake)
    refusing.bad = victim
    clock.advance(minutes=61)
    eng = Engine(store, refusing, clock)
    for _ in range(2):
        e = refused(eng.close, drop)
        assert e.code == "hold_unreadable" and e.ctx["buyer"] == "buyer-03" and e.ctx["auth_id"] == victim
        assert e.ctx["issue"] == "PERMISSION_DENIED" and e.ctx["http"] == 403 and e.ctx["debug_id"] == "DBG403"
        assert f"void hold {victim} at PayPal" in e.ctx["next"] and f"exclude --drop {drop} --buyer buyer-03" in e.ctx["next"]
    assert engine.get(drop).state == "live"                                  # nothing closed on a guess
    eng.exclude(drop, "buyer-03")
    out = eng.close(drop)
    assert out["state"] == "closed" and out["buyers"] == 3 and pp_status(fake, victim) == "VOIDED"
    assert eng.settle(drop, expect=plan_token(eng.plan(drop)))["counts"]["captured"] == 3


def test_f_through_the_bundle_the_unreadable_hold_is_a_refusal_with_no_loose_placeholders(env, monkeypatch):
    d = tb.drop_create(title="Mugs", minimum=3, hours=1, ladder="3:40,5:36")["drop"]
    tb.drop_publish(d)
    tb.sim_join(d, 4)
    eng = tb._eng()
    victim = eng.buyers(d)[2]
    refusing = OneGetRefused(eng.holds)
    refusing.bad = victim["auth_id"]
    monkeypatch.setattr(tb, "_eng", lambda: Engine(eng.store, refusing, lambda: eng.now().replace(year=2036)))
    e = togi_err(tb.drop_close, d)
    assert e.code == "hold_unreadable" and e.status == "refused" and "{" not in e.message + e.next
    assert victim["auth_id"] in e.message and "PERMISSION_DENIED" in e.message and "DBG403" in e.message
    assert f"togi crowd drop exclude --drop {d} --buyer sim-003" in e.next


def test_f_verify_refuses_a_hold_voided_event_recorded_against_another_paypal(engine, drop, fake):
    """The 'recorded against the PayPal being checked' check ignored hold_voided events; now every event counts."""
    join_many(engine, drop, 3)
    engine.close(drop)
    engine.settle(drop)
    assert V.verify(drop, engine)["ok"]
    engine.store.append(drop, "hold_voided", {"buyer": "buyer-01", "auth_id": "X", "reason": "left", "paypal_mode": "sandbox"})
    e = refused(V.verify, drop, engine)
    assert e.code == "wrong_paypal" and e.ctx["recorded"].startswith("sandbox")


def test_f_a_stray_captured_authorisation_s_next_step_says_refund_with_the_capture_id_not_void(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    engine.settle(drop)
    ada = fake_state(fake)["auths"][auth_of(engine, drop, "buyer-01")]

    def add(st):
        st["auths"]["FKASTRAY"] = {**ada, "status": "CAPTURED", "capture_id": "FKCSTRAYAUTH", "label": "d:x"}
        st["captures"]["FKCSTRAYAUTH"] = {"capture_id": "FKCSTRAYAUTH", "status": "COMPLETED", "amount": "40.00", "currency": "USD",
                                          "fee": "1.53", "net": "38.47", "final": True, "created": "2026-11-01T12:00:00Z",
                                          "auth_id": "FKASTRAY"}
    edit_fake(fake, add)
    v = V.verify(drop, engine)
    assert not v["ok"]
    stray = [p for p in v["problems"] if "FKASTRAY" in p and "does not account for" in p][0]
    assert "refund capture FKCSTRAYAUTH" in stray and "void it" not in stray
    # ... while a stray hold that is merely open is still told to be voided
    edit_fake(fake, lambda st: st["auths"]["FKASTRAY"].update(status="CREATED", capture_id=None))
    edit_fake(fake, lambda st: st["captures"].pop("FKCSTRAYAUTH"))
    assert any("void it" in p for p in V.verify(drop, engine)["problems"] if "FKASTRAY" in p)


# ===== the test harness itself ===============================================================================================

@pytest.mark.skipif(not __import__("togi_helpers").togi_available(), reason="togi is not installed")
def test_the_locked_copy_the_togi_tests_register_carries_no_secrets_and_leaves_the_repo_and_the_persons_home_alone(tmp_path):
    import hashlib

    from togi_helpers import REPO, add_crowd, approver_files_touched, env_for, real_approver_snapshot

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    repo_lock = [digest(REPO / ".togi" / "locked.sha256"), digest(REPO / ".togi" / "audit.jsonl")]
    approver_before = real_approver_snapshot()
    env = env_for(tmp_path / "home", tmp_path / "crowd.db")
    add_crowd(env, tmp_path)
    copy = tmp_path / "crowd-bundle"
    assert (copy / "togi.toml").is_file() and (copy / ".togi" / "locked.sha256").is_file()      # locked in the COPY
    names = {p.name for p in copy.rglob("*")}
    assert not names & {".env", ".git", "data", "crowd.db"} and not any(n.startswith(".env") for n in names)
    assert repo_lock == [digest(REPO / ".togi" / "locked.sha256"), digest(REPO / ".togi" / "audit.jsonl")]
    assert not approver_files_touched(approver_before, real_approver_snapshot())
    assert (tmp_path / "home" / "audit.key").is_file()                                          # the key is the throwaway home's own
