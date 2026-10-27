"""Crowd fix round 1 (docs/crowd-fixes-r1.md): money-safety gaps an independent refuter found, each with a test that
failed before the fix. Fake PayPal only. The engine's promises under test: nobody is charged twice or above the final
price, a missed minimum charges nobody, no hold is left open after settle, settle resumes after any crash, settlement
runs only through an approved `drop settle`, and `verify` catches any mismatch with PayPal."""
import inspect
import json
import os
import sys
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import LADDER, join_many
from crowd import ladder as lad
from crowd import paypal_holds as P
from crowd import togi_bundle as tb
from crowd import verify as V
from crowd.engine import CrowdError, Engine, m, plan_token
from crowd.paypal_holds import FakePayPal
from crowd.store import Store, parse_iso

D = Decimal


def code(fn, *a, **kw):
    with pytest.raises(CrowdError) as e:
        fn(*a, **kw)
    return e.value.code


def edit_fake(fake, fn):
    st = json.load(open(fake.path))
    fn(st)
    json.dump(st, open(fake.path, "w"))


def fake_state(fake):
    return json.load(open(fake.path))


def open_auths(fake):
    return {k: a for k, a in fake_state(fake)["auths"].items() if a["status"] not in ("CAPTURED", "VOIDED", "EXPIRED")}


def types(engine, drop):
    return [e["type"] for e in engine.store.events(drop)]


def settle_drop(engine, drop, n):
    join_many(engine, drop, n)
    engine.close(drop)
    return engine.settle(drop)


class Wrap:
    """A holds backend wrapped the way the refuter's reproductions do it: everything passes through unless overridden."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)


class LostReply(Wrap):
    """The refuter's p1 wrapper, which knows nothing of `on_order`: the hold is placed, the answer never arrives."""

    def __init__(self, inner, victim):
        super().__init__(inner)
        self.victim = victim

    def authorize(self, amount, currency, label, request_id, card=None):
        r = self.inner.authorize(amount, currency, label, request_id, card=card)
        if label.endswith(":" + self.victim):
            raise P.PayPalUnavailable(message="read timeout after the hold was placed")
        return r


class DieAfterAuthorize(Wrap):
    """The process dies (a BaseException, like a power cut) right after PayPal granted the hold."""

    def authorize(self, *a, **k):
        self.inner.authorize(*a, **k)
        raise KeyboardInterrupt("power cut between PayPal's answer and the event write")


class DieAfterOrder(Wrap):
    """The process dies once the order exists but before the card is touched."""

    def authorize(self, *a, on_order=None, **k):
        def cb(order_id):
            if on_order:
                on_order(order_id)
            raise KeyboardInterrupt("power cut after the order was created")
        return self.inner.authorize(*a, on_order=cb, **k)


# ---- R1: an orphan hold after a lost join reply ---------------------------------------------------------------

def test_a_join_is_written_down_before_paypal_is_asked_and_the_order_as_soon_as_it_exists(engine, drop, fake):
    engine.join(drop, "Ada")
    ev = engine.store.events(drop)
    seq = [e["type"] for e in ev if e["type"] in ("join_started", "order_created", "hold_placed")]
    assert seq == ["join_started", "order_created", "hold_placed"]
    started = [e["payload"] for e in ev if e["type"] == "join_started"][0]
    order = [e["payload"] for e in ev if e["type"] == "order_created"][0]
    placed = [e["payload"] for e in ev if e["type"] == "hold_placed"][0]
    assert started["buyer"] == "ada" and started["card_index"] == 0 and started["request_ids"][0] == started["request_id"]
    assert order["order_id"] == placed["order_id"] and placed["request_id"] == started["request_id"]
    assert engine.get(drop).unresolved == {}


def test_a_lost_join_reply_no_longer_orphans_the_hold(engine, drop, fake):
    """Before: the hold stayed open at PayPal forever and verify said ok. Now close finds it from the trail."""
    join_many(engine, drop, 3)
    fake.lose_reply("authorize", match=":dora")
    assert code(engine.join, drop, "Dora") == "paypal_unavailable"
    d = engine.get(drop)
    assert d.count == 3 and len(d.unresolved) == 1 and engine.view(d)["unresolved_joins"] == 1
    assert [t for t in types(engine, drop)[-2:]] == ["join_started", "order_created"]      # a trail, no hold_placed
    assert len(open_auths(fake)) == 4                                     # PayPal really holds Dora's card
    out = engine.close(drop)                                              # D-11: Dora saw an error, so her hold is released
    assert out["reconciled"]["released"] == ["dora"] and engine.get(drop).unresolved == {}
    assert engine.get(drop).buyers.get("dora") is None or engine.get(drop).buyers["dora"].status != "held"
    assert engine.get(drop).count == 3 and len(open_auths(fake)) == 3      # only the three confirmed holds remain
    engine.settle(drop)
    assert open_auths(fake) == {}
    v = V.verify(drop, engine)
    assert v["ok"], v["problems"]
    assert v["counts"]["captured"] == 3


def test_the_refuters_wrapper_without_on_order_is_recovered_by_replaying_the_create(store, fake, clock):
    """p1 as written: the wrapper's authorize() has no on_order parameter, so only join_started is on record."""
    eng = Engine(store, LostReply(fake, "dora"), clock)
    d = eng.create_drop("Mugs", lad.parse(LADDER), 3, 60)
    eng.publish(d)
    for n in ("ada", "bob", "cy"):
        eng.join(d, n)
    assert code(eng.join, d, "dora") == "paypal_unavailable"
    assert "order_created" not in types(eng, d)
    eng.close(d)
    eng.settle(d)
    assert open_auths(fake) == {} and V.verify(d, eng)["ok"]
    assert sorted(b["buyer"] for b in eng.buyers(d) if b["status"] == "captured") == ["ada", "bob", "cy"]   # D-11


def test_a_crash_after_paypal_granted_the_hold_is_recovered(store, fake, clock):
    """p1b: the process dies between PayPal's answer and the event write."""
    eng = Engine(store, fake, clock)
    d = eng.create_drop("Mugs", lad.parse(LADDER), 3, 60)
    eng.publish(d)
    for n in ("ada", "bob", "cy"):
        eng.join(d, n)
    with pytest.raises(KeyboardInterrupt):
        Engine(store, DieAfterAuthorize(fake), clock).join(d, "dora")
    assert len(open_auths(fake)) == 4 and len(eng.get(d).unresolved) == 1
    eng.close(d)                                       # the maker closes; Dora never retried
    eng.settle(d)
    assert open_auths(fake) == {}
    assert V.verify(d, eng)["ok"]


def test_a_crash_after_the_order_exists_but_before_the_card_is_touched(store, fake, clock):
    eng = Engine(store, fake, clock)
    d = eng.create_drop("Mugs", lad.parse(LADDER), 3, 60)
    eng.publish(d)
    for n in ("ada", "bob", "cy"):
        eng.join(d, n)
    with pytest.raises(KeyboardInterrupt):
        Engine(store, DieAfterOrder(fake), clock).join(d, "dora")
    assert types(eng, d)[-2:] == ["join_started", "order_created"]       # the order was written down before the card
    assert open_auths(fake) == {k: a for k, a in open_auths(fake).items()} and len(open_auths(fake)) == 3
    out = eng.close(d)
    assert out["reconciled"]["unconfirmed"] == ["dora"] and eng.get(d).count == 3
    hf = [e["payload"] for e in eng.store.events(d) if e["type"] == "hold_failed"][0]
    assert hf["code"] == "join_unconfirmed" and hf["order_id"]
    eng.settle(d)
    assert V.verify(d, eng)["ok"]


def test_a_hold_found_after_the_deadline_is_voided_not_adopted(engine, drop, fake, clock):
    join_many(engine, drop, 3)
    fake.lose_reply("authorize", match=":dora")
    assert code(engine.join, drop, "Dora") == "paypal_unavailable"
    clock.advance(minutes=61)                                             # the drop ran out before anyone closed it
    out = engine.close(drop)
    assert out["reconciled"]["released"] == ["dora"] and engine.get(drop).count == 3
    rel = [e["payload"] for e in engine.store.events(drop) if e["type"] == "orphan_hold_released"][0]
    assert rel["buyer"] == "dora" and rel["reason"] == "drop_not_joinable" and rel["auth_id"]
    assert fake.get(rel["auth_id"])["status"] == "VOIDED"
    engine.settle(drop)
    assert open_auths(fake) == {} and V.verify(drop, engine)["ok"]


def test_settle_reconciles_too_for_a_drop_closed_by_older_code(engine, drop, fake):
    join_many(engine, drop, 3)
    fake.lose_reply("authorize", match=":dora")
    assert code(engine.join, drop, "Dora") == "paypal_unavailable"
    engine.store.append(drop, "drop_closed", {"reason": "manual", "count": 3, "funded": True, "price": "40.00"})
    engine.settle(drop)                                                   # a closed drop can no longer take Dora
    assert open_auths(fake) == {} and "orphan_hold_released" in types(engine, drop)
    v = V.verify(drop, engine)
    assert v["ok"] and v["counts"]["captured"] == 3


def test_a_buyer_who_retries_after_a_lost_reply_gets_the_same_hold(engine, drop, fake):
    fake.lose_reply("authorize", match=":dora")
    assert code(engine.join, drop, "Dora") == "paypal_unavailable"
    r = engine.join(drop, "Dora")                                         # same request id: PayPal answers with the hold it made
    assert r["buyers"] == 1
    assert len([a for a in fake_state(fake)["auths"].values() if a["label"].endswith(":dora")]) == 1
    assert engine.get(drop).unresolved == {}
    assert types(engine, drop).count("join_started") == 2 and types(engine, drop).count("hold_placed") == 1


def test_close_waits_if_paypal_cannot_say_what_a_join_left_behind(engine, drop, fake, store, clock):
    fake.lose_reply("authorize", match=":dora")
    assert code(engine.join, drop, "Dora") == "paypal_unavailable"

    class Down(Wrap):
        def recover_join(self, *a, **k):
            raise P.PayPalUnavailable(message="down")

    assert code(Engine(store, Down(fake), clock).close, drop) == "paypal_unavailable"
    assert engine.get(drop).state == "live"                               # not closed on a guess
    assert engine.close(drop)["state"] == "closed"


def test_reconcile_can_be_called_by_itself(engine, drop, fake):
    fake.lose_reply("authorize", match=":dora")
    code(engine.join, drop, "Dora")
    out = engine.reconcile(drop)
    assert out["released"] == ["dora"] and engine.get(drop).state == "live" and engine.get(drop).count == 0  # D-11
    assert open_auths(fake) == {}
    assert engine.reconcile(drop)["checked"] == 0                         # nothing left to resolve


def test_verify_fails_when_a_started_join_is_unresolved(engine, drop):
    settle_drop(engine, drop, 3)
    engine.store.append(drop, "join_started", {"buyer": "ghost", "name": "Ghost", "request_id": "crowd-x-ghost-authorize",
                                               "attempt": 1, "card_index": 0, "amount": "40.00"})
    v = V.verify(drop, engine)
    assert not v["ok"] and any("never confirmed" in p and "ghost" in p for p in v["problems"])


def test_verify_fails_on_an_open_paypal_authorisation_the_log_does_not_know(engine, drop, fake):
    settle_drop(engine, drop, 3)
    order = [e["payload"]["order_id"] for e in engine.store.events(drop) if e["type"] == "hold_placed"][0]
    edit_fake(fake, lambda st: st["auths"].update(FKASTRAY={"order_id": order, "status": "CREATED", "amount": "40.00",
                                                          "currency": "USD", "expires": "2030-01-01T00:00:00Z",
                                                          "label": "stray", "capture_id": None}))
    v = V.verify(drop, engine)
    assert not v["ok"] and any("FKASTRAY" in p for p in v["problems"])


def test_verify_compares_each_holds_paypal_amount_with_the_engines(engine, drop, fake):
    settle_drop(engine, drop, 2)                                          # missed: both voided
    auth = engine.get(drop).buyers["buyer-01"].auth_id
    edit_fake(fake, lambda st: st["auths"][auth].update(amount="39.00"))
    v = V.verify(drop, engine)
    assert not v["ok"] and any("buyer-01" in p and "39.00" in p and "40.00" in p for p in v["problems"])


def test_verify_compares_the_hold_amount_of_a_captured_hold_too(engine, drop, fake):
    settle_drop(engine, drop, 3)
    auth = engine.get(drop).buyers["buyer-01"].auth_id
    edit_fake(fake, lambda st: st["auths"][auth].update(amount="25.00"))
    assert not V.verify(drop, engine)["ok"]


# ---- R2: test settle / test reset must not be reachable by an agent --------------------------------------------

@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    return tmp_path


def make(minimum=3, publish=True):
    d = tb.drop_create(title="Speckled mugs", minimum=minimum, hours=1, ladder=LADDER)["drop"]
    if publish:
        tb.drop_publish(d)
    return d


def err(fn, *a, **kw):
    with pytest.raises(tb.TogiError) as e:
        fn(*a, **kw)
    return e.value


def test_agents_cannot_use_test_settle_or_test_reset(env, monkeypatch):
    d = make()
    tb.sim_join(d, 3)
    tb.drop_close(d)
    for marker in ("maker-demo", "stone", ""):                            # even an empty marker is an agent session
        monkeypatch.setenv("TOGI_AGENT", marker)
        for fn, args in ((tb.settle_without_approval, (d,)), (tb.reset_test_store, ())):
            e = err(fn, *args)
            assert (e.code, e.status) == ("test_tools_not_for_agents", "refused")
        assert tb.drop_status(d)["state"] == "closed"                     # nothing settled, nothing wiped
    monkeypatch.delenv("TOGI_AGENT")
    assert tb.settle_without_approval(d)["outcome"] == "funded"           # a person in a test shell still can


def test_test_reset_also_wants_the_fake_paypal(env, monkeypatch):
    make()
    monkeypatch.setenv("CROWD_PAYPAL", "sandbox")
    assert err(tb.reset_test_store).code == "stone_requires_fake"
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    assert len(tb.drop_list()) == 1                                       # the refused reset wiped nothing
    assert tb.reset_test_store()["drops_removed"] == 1


def test_every_hold_records_the_paypal_it_was_placed_in(engine, drop, fake):
    engine.join(drop, "Ada")
    p = [e["payload"] for e in engine.store.events(drop) if e["type"] == "hold_placed"][0]
    assert p["paypal_mode"] == "fake" and p["paypal_ref"] == fake.ref


def sandbox_drop(env, kind="sandbox"):
    """A drop whose holds were placed by something other than this store's fake PayPal."""
    store = Store(str(env / "crowd.db"))

    class Pretend(Wrap):
        pass

    holds = Pretend(FakePayPal(str(env / "crowd.db.fakepaypal.json")))
    holds.kind = kind
    eng = Engine(store, holds)
    d = eng.create_drop("Mugs", LADDER, 3, 60, drop_id="d_real1")
    eng.publish(d)
    join_many(eng, d, 3)
    eng.close(d)
    return d, eng


def test_test_settle_and_reset_refuse_a_store_with_sandbox_holds(env):
    d, eng = sandbox_drop(env, "sandbox")
    for fn, args in ((tb.settle_without_approval, (d,)), (tb.reset_test_store, ())):
        e = err(fn, *args)
        assert (e.code, e.status) == ("store_has_real_holds", "refused")
    assert eng.get(d).state == "closed" and len(eng.get(d).holding) == 3          # nothing settled, nothing wiped


def test_test_settle_refuses_holds_that_live_in_another_fake_the_refuters_p5b(env):
    """p5b: the 'sandbox' there was another fake. Mode alone says fake; the fake's identity does not match this store's."""
    store = Store(str(env / "crowd.db"))
    other = FakePayPal(str(env / "SANDBOX-standin.json"))
    eng = Engine(store, other)
    d = eng.create_drop("Mugs", LADDER, 3, 60, drop_id="d_real2")
    eng.publish(d)
    join_many(eng, d, 4)
    eng.close(d)
    assert err(tb.settle_without_approval, d).code == "store_has_real_holds"
    assert eng.get(d).state == "closed" and "settlement_started" not in types(eng, d)
    assert eng.settle(d)["counts"]["captured"] == 4                       # the real settle path is untouched


def test_test_commands_refuse_holds_from_a_version_that_did_not_record_the_mode(env):
    store = Store(str(env / "crowd.db"))
    eng = Engine(store, FakePayPal(str(env / "crowd.db.fakepaypal.json")))
    d = eng.create_drop("Mugs", LADDER, 3, 60, drop_id="d_old1")
    eng.publish(d)
    eng.store.append(d, "hold_placed", {"buyer": "ada", "name": "Ada", "auth_id": "OLDAUTH", "amount": "40.00"})
    assert err(tb.reset_test_store).code == "store_has_real_holds"
    assert err(tb.settle_without_approval, d).code == "store_has_real_holds"


def test_the_demo_launcher_resets_as_the_host_not_as_the_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "maker_agent", os.path.join(os.path.dirname(__file__), "..", "..", "demo", "maker_agent.py"))
    ma = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ma)
    env = ma.agent_env(True)
    assert env["TOGI_AGENT"] == "maker-demo" and "TOGI_AGENT" not in ma.setup_env(env)
    assert ma.setup_env(env)["CROWD_ENV"] == "test"


# ---- R4: an unrecognised PayPal refusal must not stop settle -----------------------------------------------------

@pytest.mark.parametrize("issue,exc", [
    ("AUTHORIZATION_EXPIRED", P.AuthorizationExpired), ("AUTHORIZATION_DENIED", P.AuthorizationDenied),
    ("PAYER_ACTION_REQUIRED", P.PayerActionRequired), ("INSTRUMENT_DECLINED", P.CardDeclined),
    ("TRANSACTION_REFUSED", P.CardDeclined), ("SOMETHING_NEW", P.UnprocessableEntity)])
def test_paypal_refusals_are_mapped_by_name(issue, exc):
    e = P._error_from(422, {"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg1", "details": [{"issue": issue}]})
    assert type(e) is exc and isinstance(e, P.PayPalRefused) and e.issue == issue and e.debug_id == "dbg1"


@pytest.mark.parametrize("http,body", [(400, {"name": "INVALID_REQUEST"}), (403, {}), (404, {"name": "RESOURCE_NOT_FOUND"}),
                                       (409, {"name": "CONFLICT"}), (422, {"name": "SOMETHING_ELSE_ENTIRELY"})])
def test_any_other_4xx_is_a_refusal_not_an_outage(http, body):
    e = P._error_from(http, body)
    assert isinstance(e, P.PayPalRefused) and not isinstance(e, P.PayPalUnavailable) and e.http == http


@pytest.mark.parametrize("http", [408, 429, 500, 502, 503, 504])
def test_only_network_5xx_and_429_are_transient(http):
    assert isinstance(P._error_from(http, {}), P.PayPalUnavailable)


def victim_of(engine, drop, buyer="buyer-02"):
    return engine.get(drop).buyers[buyer].auth_id


def test_a_refused_capture_is_recorded_and_settle_goes_on_with_the_next_buyer(engine, drop, fake):
    """p3 (funded): AUTHORIZATION_EXPIRED on one hold used to abort settle for everyone after it, every time."""
    join_many(engine, drop, 4)
    engine.close(drop)
    victim = victim_of(engine, drop)
    fake.refuse("capture", "AUTHORIZATION_EXPIRED", match=victim, times=None)     # still CREATED when read; the capture is refused
    out = engine.settle(drop)
    assert out["state"] == "settled" and out["counts"] == {"holds": 4, "captured": 3, "voided": 0, "failed": 1, "left": 0}
    b = engine.get(drop).buyers["buyer-02"]
    assert b.status == "capture_failed" and b.error == "authorization_expired"
    cf = [e["payload"] for e in engine.store.events(drop) if e["type"] == "capture_failed"][0]
    assert cf["issue"] == "AUTHORIZATION_EXPIRED" and cf["debug_id"].startswith("FAKEDBG") and cf["paypal_mode"] == "fake"
    assert [x.status for x in engine.get(drop).buyers.values()].count("captured") == 3     # buyers after bob were charged
    assert V.verify(drop, engine)["ok"]
    assert engine.settle(drop)["already_settled"] is True


def test_a_buyer_specific_refusal_the_engine_has_no_class_for_is_recorded_the_same_way(engine, drop, fake):
    """(Round 2 narrowed this: a name nobody has seen is no longer treated as one buyer's; see test_fixes_r2.)
    A name on the buyer-specific list is recorded per buyer whatever http status carries it."""
    join_many(engine, drop, 4)
    engine.close(drop)
    fake.refuse("capture", "PAYER_ACTION_REQUIRED", http=409, match=victim_of(engine, drop, "buyer-01"), times=None)
    out = engine.settle(drop)
    assert out["counts"]["captured"] == 3 and out["counts"]["failed"] == 1
    cf = [e["payload"] for e in engine.store.events(drop) if e["type"] == "capture_failed"][0]
    assert cf["issue"] == "PAYER_ACTION_REQUIRED" and cf["http"] == 409


def test_a_hold_that_cannot_be_released_is_recorded_and_settle_still_finishes(engine, drop, fake):
    """p3 (void path): the failed hold's release is refused too. Recorded as void_failed; verify names the next step."""
    join_many(engine, drop, 4)
    engine.close(drop)
    victim = victim_of(engine, drop)
    fake.refuse("capture", "AUTHORIZATION_EXPIRED", match=victim, times=None)
    fake.refuse("void", "PAYER_ACTION_REQUIRED", match=victim, times=None)
    out = engine.settle(drop)
    assert out["state"] == "settled" and out["counts"]["failed"] == 1 and out["unreleased"] == 1
    vf = [e["payload"] for e in engine.store.events(drop) if e["type"] == "void_failed"][0]
    assert vf["auth_id"] == victim and vf["issue"] == "PAYER_ACTION_REQUIRED" and vf["debug_id"] and vf["paypal_mode"] == "fake"
    assert engine.get(drop).buyers["buyer-02"].void_failed
    v = V.verify(drop, engine)
    assert not v["ok"]
    msg = [p for p in v["problems"] if "buyer-02" in p][0]
    assert victim in msg and "void it by hand" in msg and "Next:" in msg and vf["debug_id"] in msg
    assert "could not be released" in tb_explain(engine, drop)
    edit_fake(fake, lambda st: st["auths"][victim].update(status="VOIDED"))      # the maker voids it at PayPal
    assert V.verify(drop, engine)["ok"]


def tb_explain(engine, drop):
    import tomllib
    t = tomllib.load(open(tb.MESSAGES_PATH, "rb"))["explain"]
    s = engine.get(drop).settled
    return t["unreleased_holds"].format(unreleased=s["unreleased"]) if s.get("unreleased") else ""


def test_a_missed_minimum_with_one_hold_paypal_will_not_release_still_settles(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.close(drop)
    victim = victim_of(engine, drop, "buyer-01")
    fake.refuse("void", "PAYER_ACTION_REQUIRED", match=victim, times=None)
    out = engine.settle(drop)
    assert out["outcome"] == "not_funded" and out["counts"]["voided"] == 1 and out["counts"]["void_failed"] == 1
    assert out["unreleased"] == 1 and engine.get(drop).buyers["buyer-01"].status == "void_failed"
    assert fake.get(engine.get(drop).buyers["buyer-02"].auth_id)["status"] == "VOIDED"      # the next buyer was released
    v = V.verify(drop, engine)
    assert not v["ok"] and any("buyer-01" in p and "void it by hand" in p for p in v["problems"])
    assert v["counts"]["void_failed"] == 1
    assert engine.settle(drop)["already_settled"] is True                  # and it does not loop


def test_only_a_transient_failure_stops_settle_and_it_resumes(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.close(drop)
    fake.unavailable("void", match=victim_of(engine, drop, "buyer-02"), times=1)
    assert code(engine.settle, drop) == "paypal_unavailable"
    d = engine.get(drop)
    assert d.state == "settling" and [b.status for b in d.buyers.values()] == ["voided", "held"]
    out = engine.settle(drop)
    assert out["counts"]["voided"] == 2 and out["counts"].get("void_failed", 0) == 0


def test_a_refusal_that_is_not_about_one_buyer_still_stops_settle(engine, drop, fake, store, clock):
    """Wrong credentials affect every buyer alike: record nothing, stop, resume once fixed."""
    join_many(engine, drop, 3)
    engine.close(drop)

    class BadCredentials(Wrap):
        def capture(self, *a, **k):
            raise P.HoldError("paypal_not_configured", "PayPal refused the sandbox credentials (http 401)", http=401)

    assert code(Engine(store, BadCredentials(fake), clock).settle, drop) == "settle_blocked"     # round 2: a person looks first
    assert "capture_failed" not in types(engine, drop) and "settle_blocked" in types(engine, drop)
    assert engine.settle(drop)["counts"]["captured"] == 3


# ---- R5: sub-cent and absurd prices ---------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["40.025", "40.001", "40.000000001", "0", "-5", "10000.01", "1E+30", "0.001"])
def test_a_price_must_be_a_positive_amount_in_whole_cents_up_to_10000(bad):
    with pytest.raises(lad.LadderError) as e:
        lad.parse("3:" + bad)
    assert e.value.code in ("bad_price", "bad_ladder")
    with pytest.raises(lad.LadderError) as e:
        lad.parse("3:40,5:36", start_price=bad)
    assert e.value.code == "bad_price"


@pytest.mark.parametrize("bad", ["Infinity", "-Infinity", "NaN", "sNaN"])
def test_non_finite_prices_are_refused_with_a_clear_error(bad):
    with pytest.raises(lad.LadderError):
        lad.parse("3:40,5:36", start_price=bad)
    with pytest.raises(lad.LadderError):
        lad.parse("3:" + bad)
    with pytest.raises(lad.LadderError):
        lad.D(D(bad), "price")


def test_the_edges_of_the_allowed_range_are_accepted():
    assert lad.parse("1:10000").steps[0].price == D("10000.00")
    assert lad.parse("1:0.01").steps[0].price == D("0.01")
    assert lad.parse("3:40.5,5:36", start_price="41.25").start_price == D("41.25")


def test_propose_refuses_a_sub_cent_start_price_instead_of_rounding_it():
    with pytest.raises(lad.LadderError) as e:
        lad.propose(9, 260, 30, start_price=D("40.025"))
    assert e.value.code == "bad_price"
    with pytest.raises(lad.LadderError):
        lad.propose(9, 260, 30, start_price=D("Infinity"))
    with pytest.raises(lad.LadderError):
        lad.propose(D("NaN"), 260, 30)
    assert lad.propose(9, 260, 30, start_price=D("40.25")).start_price == D("40.25")


def test_no_way_of_building_a_ladder_gets_a_sub_cent_price_into_the_engine(engine):
    """Every Ladder is checked as it is built, so create_drop never sees one (a stored ladder is checked the same way)."""
    with pytest.raises(lad.LadderError):
        lad.Ladder((lad.Step(3, D("40")), lad.Step(5, D("36"))), D("40.025"))
    with pytest.raises(lad.LadderError):
        lad.Ladder((lad.Step(3, D("40.005")),))
    with pytest.raises(lad.LadderError):
        lad.Ladder.from_dict({"start_price": "40.025", "steps": [{"min_buyers": 3, "price": "40"}]})
    assert code(engine.create_drop, "x", "3:40,5:35.999", 3, 60) == "bad_price"
    assert engine.list() == []                                            # nothing was created


def test_the_bundle_says_so_plainly(env):
    for kw in (dict(ladder="3:40.005,5:36"), dict(ladder="3:40,5:36", start_price=D("40.025")),
               dict(ladder="3:40,5:36", start_price=D("1E+30")), dict(ladder="3:10001")):
        e = err(tb.drop_create, title="x", minimum=3, hours=1, **kw)
        assert e.code == "bad_price" and e.status == "error" and e.message and e.next and "{" not in e.message
    assert err(tb.ladder_propose, D("9"), D("260"), 30, start_price=D("40.025")).code == "bad_price"
    assert err(tb.ladder_propose, D("9"), D("260"), 30, start_price=D("NaN")).code in ("bad_price", "bad_number")
    assert tb.drop_list() == []                                           # nothing was created


def test_money_text_rounds_half_up_like_the_paypal_client_writes_it():
    assert m("40.025") == "40.03" and m("40.005") == "40.01" and m(D("33")) == "33.00"


def test_verify_compares_paypal_amounts_exactly(engine, drop, fake):
    settle_drop(engine, drop, 3)
    cap = engine.get(drop).buyers["buyer-01"].capture_id
    edit_fake(fake, lambda st: st["captures"][cap].update(amount="39.999"))
    assert not V.verify(drop, engine)["ok"]                               # not rounded into agreement


# ---- R6: failures carry PayPal's own evidence -------------------------------------------------------------------------

def test_a_failed_capture_records_paypals_error_name_debug_id_and_mode(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    fake.decline_next(1)
    engine.settle(drop)
    cf = [e["payload"] for e in engine.store.events(drop) if e["type"] == "capture_failed"][0]
    assert cf["issue"] == "TRANSACTION_REFUSED" and cf["http"] == 422 and cf["debug_id"].startswith("FAKEDBG")
    assert cf["paypal_mode"] == "fake" and cf["paypal_ref"] == fake.ref and cf["code"] == "card_declined"
    assert V.verify(drop, engine)["ok"]


def test_verify_treats_a_failed_capture_with_no_paypal_evidence_as_a_problem(engine, drop, fake):
    """p5b's damage was exactly this: capture_failed written by something that never spoke to PayPal."""
    settle_drop(engine, drop, 3)
    ghost = fake.authorize(D("40"), "USD", "ghost", "crowd-ghost-authorize")
    fake.void(ghost["auth_id"], "v")
    engine.store.append(drop, "hold_placed", {"buyer": "ghost", "name": "Ghost", "order_id": ghost["order_id"],
                                             "auth_id": ghost["auth_id"], "amount": "40.00", "paypal_mode": "fake"})
    engine.store.append(drop, "capture_failed", {"buyer": "ghost", "auth_id": ghost["auth_id"], "code": "resource_not_found",
                                                "message": "made up"})
    engine.store.append(drop, "failed_hold_released", {"buyer": "ghost", "auth_id": ghost["auth_id"]})
    v = V.verify(drop, engine)
    assert not v["ok"] and any("no PayPal evidence" in p and "ghost" in p for p in v["problems"])


def test_verify_refuses_a_drop_with_events_recorded_against_another_paypal(engine, drop, fake):
    """Round 3 (A): a drop belongs to one PayPal. verify used to report this as a failed check; it now refuses outright, because
    a hold read from the wrong PayPal says nothing about the drop (see test_fixes_r3 for every operation)."""
    settle_drop(engine, drop, 3)
    engine.store.append(drop, "capture_failed", {"buyer": "buyer-01", "auth_id": "x", "issue": "X", "paypal_mode": "sandbox"})
    e = pytest.raises(CrowdError, V.verify, drop, engine).value
    assert e.code == "wrong_paypal" and e.ctx["recorded"].startswith("sandbox")


# ---- R7: the approval covers the releases too ------------------------------------------------------------------------------

def failed_capture_with_release_pending(engine, drop, fake):
    join_many(engine, drop, 5)
    engine.join(drop, "Card Nine", card_index=P.DECLINE_CARD_INDEX)
    engine.close(drop)
    victim = victim_of(engine, drop, "card-nine")
    fake.unavailable("void", match=victim, times=1)
    assert code(engine.settle, drop) == "paypal_unavailable"             # captured five, failed one, release interrupted
    return victim


def test_the_plan_token_names_failed_holds_still_to_be_released(engine, drop, fake):
    victim = failed_capture_with_release_pending(engine, drop, fake)
    p = engine.plan(drop)
    assert p["releases"] == 1 and p["holds"] == 0 and plan_token(p) == "capture:0:36.00:0.00+release:1"
    assert "release 1 failed hold" in p["summary"]
    assert fake.get(victim)["status"] == "CREATED"
    assert code(engine.settle, drop, expect="capture:0:36.00:0.00") == "plan_changed"      # the releases were not approved
    assert fake.get(victim)["status"] == "CREATED" and engine.get(drop).state == "settling"
    out = engine.settle(drop, expect="capture:0:36.00:0.00+release:1")
    assert fake.get(victim)["status"] == "VOIDED" and out["state"] == "settled"
    assert V.verify(drop, engine)["ok"]


def test_the_engine_re_derives_the_plan_under_its_lock_not_only_the_bundle(engine, drop):
    join_many(engine, drop, 4)
    engine.close(drop)
    e = pytest.raises(CrowdError, engine.settle, drop, expect="capture:5:40.00:200.00").value
    assert e.code == "plan_changed" and e.ctx["actual"] == "capture:4:40.00:160.00"
    assert engine.get(drop).state == "closed"                             # not even settlement_started
    assert engine.settle(drop, expect="capture:4:40.00:160.00")["outcome"] == "funded"


def test_the_bundle_binds_the_approval_to_the_releases(env):
    d = make()
    tb.sim_join(d, 5)
    tb.drop_join(d, "Card Nine", card=P.DECLINE_CARD_INDEX)
    tb.drop_close(d)
    fake = FakePayPal(str(env / "crowd.db.fakepaypal.json"))
    auth = [b for b in tb._eng().buyers(d) if b["buyer"] == "card-nine"][0]["auth_id"]
    fake.unavailable("void", match=auth, times=1)
    assert err(tb.drop_settle, d, "capture:6:36.00:216.00").code == "paypal_unavailable"
    plan = tb.drop_plan(d)
    assert plan["expect"] == "capture:0:36.00:0.00+release:1" and plan["next"].endswith("+release:1")
    assert err(tb.drop_settle, d, "capture:0:36.00:0.00").code == "plan_changed"
    assert tb.drop_settle(d, plan["expect"])["state"] == "settled"


# ---- clock edges ------------------------------------------------------------------------------------------------------------

def test_leave_stops_at_the_deadline_just_as_joining_does(engine, drop, clock):
    join_many(engine, drop, 3)
    ends = parse_iso(engine.get(drop).ends_at)
    clock.t = ends - timedelta(microseconds=1)
    assert engine.leave(drop, "Buyer 03")["buyers"] == 2                  # still before the end
    engine.join(drop, "Replacement")
    clock.t = ends
    assert code(engine.leave, drop, "Buyer 01") == "drop_ended"           # at the deadline exactly: no
    assert code(engine.join, drop, "Late") == "drop_ended"
    clock.t = ends + timedelta(hours=5)
    assert code(engine.leave, drop, "Buyer 01") == "drop_ended"
    assert engine.get(drop).count == 3 and engine.close(drop)["state"] == "closed"
    assert engine.plan(drop)["summary"].startswith("capture 3 holds")     # p10: five hours later the drop was unfunded


def test_a_leave_is_stamped_with_the_moment_it_was_asked_not_when_paypal_answered(engine, drop, clock, fake):
    join_many(engine, drop, 3)
    ends = parse_iso(engine.get(drop).ends_at)
    clock.t = ends - timedelta(seconds=1)

    class Slow(Wrap):
        def void(self, *a, **k):
            out = self.inner.void(*a, **k)
            clock.t = ends + timedelta(seconds=5)        # PayPal took six seconds: the deadline passed meanwhile
            return out

    Engine(engine.store, Slow(fake), clock).leave(drop, "Buyer 01")
    left = [e for e in engine.store.events(drop) if e["type"] == "buyer_left"][0]
    assert parse_iso(left["ts"]) < ends


def test_close_counts_the_crowd_as_it_stood_at_the_deadline(engine, drop, clock):
    """Events stamped after the deadline (an older version, or a race) must not change who made the minimum."""
    join_many(engine, drop, 3)
    ends = parse_iso(engine.get(drop).ends_at)
    late = ends + timedelta(hours=5)
    engine.store.append_many(drop, [("hold_voided", {"buyer": "buyer-03", "auth_id": engine.get(drop).buyers["buyer-03"].auth_id,
                                                     "reason": "left"}), ("buyer_left", {"buyer": "buyer-03"})], ts=late)
    clock.t = late
    out = engine.close(drop)
    closed = [e["payload"] for e in engine.store.events(drop) if e["type"] == "drop_closed"][0]
    assert closed["reason"] == "expired" and closed["count"] == 3 and closed["funded"] is True
    assert closed["price"] == "40.00" and out["state"] == "closed"
    assert engine.get(drop).count == 3


def test_a_manual_close_before_the_deadline_counts_the_crowd_now(engine, drop):
    join_many(engine, drop, 4)
    engine.leave(drop, "Buyer 04")
    engine.close(drop)
    closed = [e["payload"] for e in engine.store.events(drop) if e["type"] == "drop_closed"][0]
    assert closed["reason"] == "manual" and closed["count"] == 3


# ---- holds near expiry ---------------------------------------------------------------------------------------------------------

class Spy(Wrap):
    def __init__(self, inner):
        super().__init__(inner)
        self.calls = []

    def capture(self, auth_id, *a, **k):
        self.calls.append(("capture", auth_id))
        return self.inner.capture(auth_id, *a, **k)

    def void(self, auth_id, *a, **k):
        self.calls.append(("void", auth_id))
        return self.inner.void(auth_id, *a, **k)


def test_every_hold_past_its_expiry_is_excluded_from_the_plan(engine, drop, fake, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    clock.advance(days=3650)                          # long after every hold PayPal gave out
    for b in engine.get(drop).buyers.values():
        fake.expire(b.auth_id)                        # and PayPal agrees (round 2: the clock alone decides nothing)
    p = engine.plan(drop)
    assert p["expired"] == 4 and p["holds"] == 0 and plan_token(p) == "void:0"        # round 3: no crowd left, so not funded


def test_expiry_is_judged_per_hold_and_only_the_expired_one_is_skipped(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    victim = victim_of(engine, drop)
    # one hold carries an earlier expiry than the rest (PayPal's own date for that authorisation)
    ev = engine.store.events(drop)
    first = [e for e in ev if e["type"] == "hold_placed" and e["payload"]["auth_id"] == victim][0]["payload"]
    short = (parse_iso(first["expires"]) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_expiry(store, drop, victim, short)
    clock.t = parse_iso(short) + timedelta(hours=1)
    fake.expire(victim)
    spy = Spy(fake)
    eng = Engine(store, spy, clock)
    p = eng.plan(drop)
    assert p["holds"] == 3 and p["expired"] == 1 and "1 expired hold cannot be taken" in p["summary"]
    assert plan_token(p) == "capture:3:40.00:120.00"
    out = eng.settle(drop, expect="capture:3:40.00:120.00")
    assert out["counts"] == {"holds": 4, "captured": 3, "voided": 1, "failed": 0, "left": 0} and out["shortfall"] == 0
    assert victim not in [a for _, a in spy.calls]                        # nothing tried to capture, or void, the lapsed hold
    hv = [e["payload"] for e in store.events(drop) if e["type"] == "hold_voided"][0]      # round 3: PayPal's EXPIRED, recorded
    assert hv["reason"] == "paypal_says_expired" and hv["paypal_status"] == "EXPIRED" and hv["auth_id"] == victim
    assert not [e for e in store.events(drop) if e["type"] in ("capture_failed", "failed_hold_released")]
    assert V.verify(drop, eng)["ok"]


def _rewrite_expiry(store, drop, auth, expires):
    """A test-only change of the stored expiry (the log is append-only to SQL, so go round it with a fresh store)."""
    import sqlite3
    c = sqlite3.connect(store.path)
    c.execute("DROP TRIGGER events_no_update")
    rows = c.execute("SELECT id, payload_json FROM events WHERE drop_id=? AND type='hold_placed'", (drop,)).fetchall()
    for i, body in rows:
        p = json.loads(body)
        if p["auth_id"] == auth:
            p["expires"] = expires
            c.execute("UPDATE events SET payload_json=? WHERE id=?", (json.dumps(p, sort_keys=True), i))
    c.execute("CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;")
    c.commit()
    c.close()


def test_verify_checks_that_a_hold_the_engine_called_expired_really_is(engine, drop, fake, store, clock):
    join_many(engine, drop, 4)
    engine.close(drop)
    victim = victim_of(engine, drop)
    first = [e for e in engine.store.events(drop) if e["type"] == "hold_placed" and e["payload"]["auth_id"] == victim][0]["payload"]
    short = (parse_iso(first["expires"]) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_expiry(store, drop, victim, short)
    clock.t = parse_iso(short) + timedelta(hours=1)
    fake.expire(victim)
    engine.settle(drop)
    assert V.verify(drop, engine)["ok"]                                  # PayPal said EXPIRED when it was recorded, and still does
    edit_fake(fake, lambda st: st["auths"][victim].update(status="CREATED"))      # ... unless PayPal now says otherwise
    v = V.verify(drop, engine)
    assert not v["ok"] and any("EXPIRED" in p and "CREATED" in p for p in v["problems"])


def test_a_lapsed_hold_in_a_missed_drop_needs_no_void_and_nobody_is_charged(engine, drop, fake, store, clock):
    join_many(engine, drop, 2)
    engine.close(drop)
    victim = victim_of(engine, drop, "buyer-01")
    first = [e for e in engine.store.events(drop) if e["type"] == "hold_placed" and e["payload"]["auth_id"] == victim][0]["payload"]
    short = (parse_iso(first["expires"]) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _rewrite_expiry(store, drop, victim, short)
    clock.t = parse_iso(short) + timedelta(hours=1)
    fake.expire(victim)
    spy = Spy(fake)
    out = Engine(store, spy, clock).settle(drop)
    assert out["outcome"] == "not_funded" and out["counts"]["voided"] == 2 and out["counts"]["failed"] == 0
    assert victim not in [a for _, a in spy.calls] and V.verify(drop, engine)["ok"]


# ---- R3: the only road to money is `drop settle` through the host -------------------------------------------------------------

def test_exactly_one_command_moves_money_and_it_waits_for_a_person():
    assert sorted(w for w, e in tb.EFFECTS.items() if e == "money") == ["drop exclude", "drop settle"]  # both wait for a person
    assert tb.EFFECTS["test settle"] == "admin" and tb.EFFECTS["test reset"] == "admin"


def test_paypal_credentials_come_only_from_the_repo_env_or_the_environment(monkeypatch):
    """Nothing a caller passes can name another credentials file: PayPalHolds takes no path, and the sandbox client is
    built by closure.paypal.Client() with load_env's default (the repo's own .env, then the environment)."""
    params = inspect.signature(P.PayPalHolds.__init__).parameters
    assert set(params) == {"self", "client", "retries"}
    seen = {}

    def load_env(path=None):
        seen["path"] = path
        return {"PAYPAL_ENV": "sandbox", "PAYPAL_CLIENT_ID": "x", "PAYPAL_CLIENT_SECRET": "y"}

    monkeypatch.setattr("closure.paypal.load_env", load_env)
    for var in ("PAYPAL_ENV_FILE", "ENV_FILE", "DOTENV_PATH", "CLOSURE_ENV", "CROWD_ENV_FILE"):
        monkeypatch.setenv(var, "/tmp/evil.env")
    P.PayPalHolds()
    assert seen["path"] is None
    assert P.make_holds.__code__.co_argcount == 1                        # the store path, nothing about credentials
    import re
    client = inspect.getsource(P).split("class PayPalHolds")[1].split("class FakePayPal")[0]
    assert not re.search(r"(?<![a-z])open\(", client) and "os.environ" not in client and "getenv" not in client   # no file, no variable of its own


from togi_helpers import add_crowd, env_for, togi, togi_available  # noqa: E402


@pytest.mark.skipif(not togi_available(), reason="togi is not installed")
def test_through_the_host_an_agent_cannot_settle_without_a_person(tmp_path):
    """The whole road, as an agent sees it: settle waits for approval, and the test back doors are shut."""
    db = tmp_path / "crowd.db"
    env = env_for(tmp_path / "home", db)
    add_crowd(env, tmp_path)
    setup = [("crowd", "drop", "create", "--drop-id", "d_r3a", "--title", "Mugs", "--minimum", "3", "--hours", "1",
              "--ladder", "3:40,5:36"), ("crowd", "drop", "publish", "--drop", "d_r3a"),
             ("crowd", "sim", "join", "--drop", "d_r3a", "--count", "4"), ("crowd", "drop", "close", "--drop", "d_r3a")]
    for argv in setup:
        assert togi(env, *argv)[0] == 0, argv
    agent = {**env, "TOGI_AGENT": "maker-demo"}
    code_, out = togi(agent, "crowd", "drop", "settle", "--drop", "d_r3a", "--expect", "capture:4:40.00:160.00")
    assert out["status"] == "needs_approval"
    code_, out = togi(agent, "crowd", "test", "settle", "--drop", "d_r3a")
    assert out["ok"] is False and out["error"]["code"] == "test_tools_not_for_agents"
    code_, out = togi(agent, "crowd", "test", "reset")
    assert out["ok"] is False and out["error"]["code"] == "test_tools_not_for_agents"
    assert togi(env, "crowd", "drop", "status", "--drop", "d_r3a")[1]["data"]["state"] == "closed"     # nothing moved
    assert togi(env, "crowd", "test", "settle", "--drop", "d_r3a")[1]["data"]["outcome"] == "funded"   # setup still can


@pytest.mark.skipif(not togi_available(), reason="togi is not installed")
def test_a_togi_run_script_must_not_be_able_to_import_the_engine_and_settle(tmp_path):
    """R3 is closed in Togi (FIXES-r1 #4: the `togi run` child is sandboxed and cannot read bundle repos or .env).
    Crowd's side of the expectation: whatever the script does, the money path stays `drop settle` through the host.
    Until the Togi build under test has that sandbox, this is an expected failure, reported rather than hidden."""
    import subprocess
    db = tmp_path / "crowd.db"
    env = env_for(tmp_path / "home", db)
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    add_crowd(env, tmp_path)
    eng = Engine(Store(str(db)), FakePayPal(str(db) + ".fakepaypal.json"))
    d = eng.create_drop("Mugs", "3:40,5:36", 3, 60, drop_id="d_r3b")
    eng.publish(d)
    join_many(eng, d, 4)
    eng.close(d)
    script = (f"import os, sys\nsys.modules.pop('crowd', None)\n"
              f"os.environ.update(CROWD_PAYPAL='fake', CROWD_DB={str(db)!r})\nsys.path.insert(0, {repo + '/src'!r})\n"
              f"from crowd.engine import Engine\nprint(Engine().settle('d_r3b'))\n")
    agent = {**env, "TOGI_AGENT": "maker-demo"}
    r = subprocess.run([sys.executable, "-m", "togi", "run", "-"], input=script, capture_output=True, text=True, env=agent,
                       cwd=os.path.dirname(repo), timeout=120)
    if eng.get(d).state == "settled":
        pytest.xfail("this Togi's `togi run` does not yet sandbox the child (Togi FIXES-r1 #4): the engine was settled "
                     "with no approval. " + (r.stdout or r.stderr)[:120])
    assert eng.get(d).state == "closed"


# ---- more of the same: lost replies mid-settlement, and what a reconcile does when PayPal will not answer ------------------

def test_a_capture_whose_reply_is_lost_is_not_charged_twice_when_settle_resumes(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    fake.lose_reply("capture", match=victim_of(engine, drop, "buyer-02"), times=1)
    assert code(engine.settle, drop) == "paypal_unavailable"
    assert len(fake_state(fake)["captures"]) == 2                          # PayPal did charge buyer-02; nobody wrote it down
    out = engine.settle(drop)
    assert out["counts"]["captured"] == 3 and len(fake_state(fake)["captures"]) == 3
    assert V.verify(drop, engine)["ok"]


def test_a_void_whose_reply_is_lost_is_not_a_failure_when_settle_resumes(engine, drop, fake):
    join_many(engine, drop, 2)
    engine.close(drop)
    fake.lose_reply("void", match=victim_of(engine, drop, "buyer-01"), times=1)
    assert code(engine.settle, drop) == "paypal_unavailable"
    out = engine.settle(drop)
    assert out["counts"]["voided"] == 2 and "void_failed" not in out["counts"] and "unreleased" not in out
    assert open_auths(fake) == {} and V.verify(drop, engine)["ok"]


def test_a_hold_with_the_wrong_amount_found_by_reconcile_is_voided_not_adopted(engine, drop, fake):
    fake.lose_reply("authorize", match=":dora")
    code(engine.join, drop, "Dora")
    auth = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":dora")][0]
    edit_fake(fake, lambda st: st["auths"][auth].update(amount="12.00"))
    out = engine.close(drop)
    assert out["reconciled"]["released"] == ["dora"] and engine.get(drop).count == 0
    rel = [e["payload"] for e in engine.store.events(drop) if e["type"] == "orphan_hold_released"][0]
    assert rel["reason"] == "wrong_amount" and fake.get(auth)["status"] == "VOIDED"


def test_the_request_id_of_a_released_orphan_is_not_reused_by_the_buyers_next_try(engine, drop, fake, clock):
    fake.lose_reply("authorize", match=":dora")
    code(engine.join, drop, "Dora")
    engine.reconcile(drop)                                                 # D-11: released, never adopted
    assert open_auths(fake) == {}
    fake.lose_reply("authorize", match=":dora")
    code(engine.join, drop, "Dora")                                        # attempt 2, lost again
    clock.advance(minutes=61)
    engine.close(drop)                                                     # too late for her: voided
    d = engine.get(drop)
    assert d.buyers["dora"].attempts == 2 and d.unresolved == {}           # request ids 1 and 2 are both spent: the next is 3


def test_when_paypal_will_not_answer_the_recovery_the_join_is_marked_unconfirmed_and_verify_keeps_asking(engine, drop, fake, store, clock):
    join_many(engine, drop, 3)
    fake.lose_reply("authorize", match=":dora")
    code(engine.join, drop, "Dora")

    class Refuses(Wrap):
        def recover_join(self, *a, **k):
            raise P.UnprocessableEntity(message="x", issue="SOMETHING_NEW", http=422, debug_id="dbgR")

    eng = Engine(store, Refuses(fake), clock)
    out = eng.close(drop)
    assert out["reconciled"]["unconfirmed"] == ["dora"] and eng.get(drop).unresolved == {}
    hf = [e["payload"] for e in store.events(drop) if e["type"] == "hold_failed"][0]
    assert hf["code"] == "join_unconfirmed" and hf["issue"] == "SOMETHING_NEW" and hf["debug_id"] == "dbgR"
    eng.settle(drop)
    v = V.verify(drop, engine)               # the hold is still there, and the log does not account for it
    assert not v["ok"] and any("authorisation" in p and "does not account" in p for p in v["problems"])


def test_an_orphan_hold_that_cannot_be_voided_is_recorded_and_verify_names_it(engine, drop, fake, clock):
    join_many(engine, drop, 3)
    fake.lose_reply("authorize", match=":dora")
    code(engine.join, drop, "Dora")
    auth = [k for k, a in fake_state(fake)["auths"].items() if a["label"].endswith(":dora")][0]
    fake.refuse("void", "SOMETHING_NEW", match=auth, times=None)
    clock.advance(minutes=61)
    out = engine.close(drop)
    assert out["reconciled"]["unconfirmed"] == ["dora"] and engine.get(drop).count == 3
    vf = [e["payload"] for e in engine.store.events(drop) if e["type"] == "void_failed"][0]
    assert vf["orphan"] is True and vf["auth_id"] == auth and vf["issue"] == "SOMETHING_NEW"
    engine.settle(drop)
    v = V.verify(drop, engine)
    assert not v["ok"] and any(auth in p for p in v["problems"])


# ---- the fake's own controls and the store's projection of the new events -----------------------------------------------------

def test_the_fake_builds_a_hold_in_two_steps_and_can_be_stopped_between_them(fake):
    seen = []

    def stop(order_id):
        seen.append(order_id)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        fake.authorize(D("40"), "USD", "lbl", "rid1", on_order=stop)
    assert fake_state(fake)["auths"] == {} and seen                         # an order, no hold
    assert fake.recover_join("rid1", D("40"), "USD", "lbl") == {"order_id": seen[0], "auth": None}
    hold = fake.authorize(D("40"), "USD", "lbl", "rid1")                    # the same request id: the same order, now held
    assert hold["order_id"] == seen[0]
    again = fake.recover_join("rid1", D("40"), "USD", "lbl")
    assert again["auth"]["auth_id"] == hold["auth_id"] and again["auth"]["status"] == "CREATED"
    assert fake.order_authorizations(hold["order_id"])[0]["amount"] == "40.00"
    with pytest.raises(P.HoldNotFound):
        fake.order_authorizations("FKOnothing")


def test_the_fake_controls_do_what_they_say(fake):
    h = fake.authorize(D("40"), "USD", "a", "r-a")
    fake.refuse("capture", "AUTHORIZATION_DENIED", match=h["auth_id"], times=1)
    with pytest.raises(P.AuthorizationDenied) as e:
        fake.capture(h["auth_id"], D("30"), "c1")
    assert e.value.debug_id.startswith("FAKEDBG") and e.value.http == 422
    assert fake.capture(h["auth_id"], D("30"), "c1")["status"] == "COMPLETED"       # the refusal was one-shot and did nothing
    h2 = fake.authorize(D("40"), "USD", "b", "r-b")
    fake.unavailable("void", times=1)
    with pytest.raises(P.PayPalUnavailable):
        fake.void(h2["auth_id"], "v1")
    assert fake.get(h2["auth_id"])["status"] == "CREATED"                          # an outage before the call does nothing
    fake.lose_reply("void", times=1)
    with pytest.raises(P.PayPalUnavailable):
        fake.void(h2["auth_id"], "v2")
    assert fake.get(h2["auth_id"])["status"] == "VOIDED"                           # a lost reply happens after the effect
    h3 = fake.authorize(D("40"), "USD", "c", "r-c")
    fake.expire(h3["auth_id"])
    assert fake.get(h3["auth_id"])["status"] == "EXPIRED"
    with pytest.raises(P.AuthorizationExpired):
        fake.capture(h3["auth_id"], D("10"), "c3")
    with pytest.raises(P.AuthorizationExpired):
        fake.void(h3["auth_id"], "v3")


def test_the_fake_has_an_identity_that_follows_its_state_file(tmp_path):
    a, b = FakePayPal(str(tmp_path / "a.json")), FakePayPal(str(tmp_path / "b.json"))
    assert a.ref == FakePayPal(str(tmp_path / "a.json")).ref != b.ref and len(a.ref) == 12


def test_the_projection_follows_joins_through_to_their_resolution(store):
    d = "d_proj1"
    store.append(d, "drop_created", {"title": "t", "currency": "USD", "minimum": 1, "duration_minutes": 5,
                                    "ladder": lad.parse("1:10").to_dict()})
    store.append(d, "join_started", {"buyer": "a", "name": "A", "request_id": "r1", "attempt": 1})
    store.append(d, "join_started", {"buyer": "b", "name": "B", "request_id": "r2", "attempt": 1})
    store.append(d, "order_created", {"buyer": "a", "request_id": "r1", "order_id": "O1"})
    pr = store.project(d)
    assert set(pr.unresolved) == {"r1", "r2"} and pr.unresolved["r1"]["order_id"] == "O1" and pr.buyers == {}
    store.append(d, "hold_placed", {"buyer": "a", "name": "A", "auth_id": "A1", "amount": "10.00", "request_id": "r1"})
    store.append(d, "hold_failed", {"buyer": "b", "name": "B", "code": "x", "request_id": "r2"})
    assert store.project(d).unresolved == {}
    store.append(d, "join_started", {"buyer": "c", "name": "C", "request_id": "r3", "attempt": 1})
    store.append(d, "orphan_hold_released", {"buyer": "c", "name": "C", "request_id": "r3", "auth_id": "A3"})
    pr = store.project(d)
    assert pr.unresolved == {} and pr.buyers["c"].attempts == 1


def test_project_as_of_counts_only_events_up_to_that_instant(store, clock):
    d = "d_proj2"
    t0 = clock.t
    store.append(d, "drop_created", {"title": "t", "currency": "USD", "minimum": 1, "duration_minutes": 5,
                                    "ladder": lad.parse("1:10").to_dict()}, ts=t0)
    store.append(d, "hold_placed", {"buyer": "a", "name": "A", "auth_id": "A1", "amount": "10.00"}, ts=t0 + timedelta(minutes=1))
    store.append_many(d, [("hold_voided", {"buyer": "a", "auth_id": "A1"}), ("buyer_left", {"buyer": "a"})],
                      ts=t0 + timedelta(minutes=9))
    assert store.project(d).count == 0
    assert store.project(d, as_of=t0 + timedelta(minutes=5)).count == 1
    assert store.project(d, as_of=t0 + timedelta(minutes=1)).count == 1 and store.project(d, as_of=t0).count == 0


def test_a_void_failed_event_marks_the_buyer_but_a_failed_capture_stays_a_failed_capture(store):
    d = "d_proj3"
    store.append(d, "drop_created", {"title": "t", "currency": "USD", "minimum": 1, "duration_minutes": 5,
                                    "ladder": lad.parse("1:10").to_dict()})
    for b in ("a", "b"):
        store.append(d, "hold_placed", {"buyer": b, "name": b, "auth_id": "A" + b, "amount": "10.00"})
    store.append(d, "void_failed", {"buyer": "a", "auth_id": "Aa", "code": "unprocessable_entity"})
    store.append(d, "capture_failed", {"buyer": "b", "auth_id": "Ab", "code": "authorization_expired"})
    store.append(d, "void_failed", {"buyer": "b", "auth_id": "Ab", "code": "unprocessable_entity"})
    store.append(d, "void_failed", {"buyer": "zz", "auth_id": "Azz", "orphan": True})
    pr = store.project(d)
    assert pr.buyers["a"].status == "void_failed" and pr.buyers["b"].status == "capture_failed"
    assert pr.buyers["a"].void_failed and pr.buyers["b"].void_failed and "zz" not in pr.buyers
    assert pr.holding == []                                                # neither is `held`: settle will not loop on them


def test_a_join_asked_before_the_deadline_counts_even_if_paypal_answers_after_it(engine, drop, fake, clock):
    join_many(engine, drop, 2)
    ends = parse_iso(engine.get(drop).ends_at)
    clock.t = ends - timedelta(seconds=1)

    class Slow(Wrap):
        def authorize(self, *a, **k):
            out = self.inner.authorize(*a, **k)
            clock.t = ends + timedelta(seconds=5)       # six seconds at PayPal: the deadline passed in the meantime
            return out

    Engine(engine.store, Slow(fake), clock).join(drop, "Last Minute")
    placed = [e for e in engine.store.events(drop) if e["type"] == "hold_placed"][-1]
    assert parse_iso(placed["ts"]) < ends
    engine.close(drop)
    closed = [e["payload"] for e in engine.store.events(drop) if e["type"] == "drop_closed"][0]
    assert closed["reason"] == "expired" and closed["count"] == 3 and closed["funded"] is True
    assert engine.settle(drop)["counts"]["captured"] == 3 and V.verify(drop, engine)["ok"]
