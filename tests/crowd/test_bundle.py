"""The Togi bundle's commands, called as Python functions (the same code the host runs)."""
import glob
import os
import re
import tomllib
from decimal import Decimal

import pytest

from crowd import paypal_holds as P
from crowd import togi_bundle as tb
from crowd.togi_bundle import TogiError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LADDER = "3:40,5:36,7:33"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "crowd.db"))
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    return tmp_path


def make(minimum=3, ladder=LADDER, publish=True, **kw):
    d = tb.drop_create(title="Speckled mugs", minimum=minimum, hours=1, ladder=ladder, **kw)["drop"]
    if publish:
        tb.drop_publish(d)
    return d


def err(fn, *a, **kw):
    with pytest.raises(TogiError) as e:
        fn(*a, **kw)
    return e.value


# ---- the contract: effects and shapes ------------------------------------------------------------------------

def test_effects_are_what_the_spec_says():
    assert tb.EFFECTS == {
        "ladder propose": "read", "drop create": "write", "drop publish": "write", "drop list": "read",
        "drop status": "read", "drop join": "write", "drop leave": "write", "drop close": "write",
        "drop plan": "read", "drop settle": "money", "drop verify": "read", "drop tieout": "read",
        "drop explain": "read", "buyers list": "read", "sim join": "write", "test settle": "admin", "test reset": "admin",
    }
    assert [w for w, e in tb.EFFECTS.items() if e == "money"] == ["drop settle"]     # the only way money moves


def test_manifest_matches_the_bundle_and_locks_what_moves_money():
    m = tomllib.load(open(os.path.join(ROOT, "togi.toml"), "rb"))
    assert m["bundle"]["name"] == "crowd" == tb.bundle.name and m["bundle"]["module"] == "crowd.togi_bundle:bundle"
    assert m["policy"]["approve_effects"] == ["money"]
    assert m["tiers"]["polish"] == ["skills/**", "togi_messages.toml"] and m["tiers"]["reshape"] == ["src/crowd/ladder.py"]
    for f in ("togi.toml", "stone/**", "src/crowd/engine.py", "src/crowd/paypal_holds.py", "src/crowd/verify.py",
              "src/closure/**", "src/crowd/togi_bundle.py", "src/crowd/store.py"):
        assert f in m["tiers"]["locked"], f
    assert m["stone"] == {"tests": "tests", "tasks": "stone/tasks",
                          "env": {"CROWD_ENV": "test", "CROWD_PAYPAL": "fake"}}      # the stone never touches PayPal


def test_ladder_propose_shape(env):
    out = tb.ladder_propose(Decimal("9"), Decimal("260"), 30)
    assert out["ladder"] == "30:40,45:37,60:33,75:30" and out["start_price"] == "40.00" and out["minimum"] == 30
    assert [s["from_buyers"] for s in out["steps"]] == [30, 45, 60, 75]
    assert out["steps"][0] == {"step": 1, "from_buyers": 30, "price": "40.00", "fee_each": "1.53", "net_each": "29.47",
                               "profit_at_that_count": "624.10"}
    assert "--ladder 30:40,45:37,60:33,75:30" in out["next"]
    assert err(tb.ladder_propose, Decimal("9"), Decimal("260"), 30, start_price=Decimal("10")).code == "start_price_loses_money"
    assert err(tb.ladder_propose, Decimal("9"), Decimal("260"), 0).code == "bad_number"


def test_drop_create_variants(env):
    a = tb.drop_create(title="A", minimum=3, minutes=90, ladder=LADDER)
    assert a["state"] == "draft" and a["duration_minutes"] == 90 and a["ladder"] == LADDER and a["drop"].startswith("d_")
    assert a["next"] == f"togi crowd drop publish --drop {a['drop']}"
    b = tb.drop_create(title="B", minimum=30, hours=48, from_proposal=True, unit_cost=Decimal("9"), fixed_cost=Decimal("260"))
    assert b["ladder"] == "30:40,45:37,60:33,75:30" and len(b["profit_at_each_step"]) == 4
    c = tb.drop_create(title="C", minimum=3, hours=1, minutes=30, ladder="3:36,5:33", start_price=Decimal("40"),
                       drop_id="d_mine")
    assert c["duration_minutes"] == 90 and c["start_price"] == "40.00" and c["drop"] == "d_mine"
    assert err(tb.drop_create, title="D", minimum=3, hours=1, ladder=LADDER, drop_id="d_mine").code == "drop_exists"
    assert err(tb.drop_create, title="D", minimum=3, hours=1, ladder=LADDER, drop_id="Mine!").code == "bad_drop_id"


@pytest.mark.parametrize("kw,code", [
    (dict(title="x", minimum=3, ladder=LADDER), "duration_missing"),
    (dict(title="x", minimum=3, hours=1), "ladder_required"),
    (dict(title="x", minimum=3, hours=1, ladder=LADDER, from_proposal=True), "ladder_conflict"),
    (dict(title="x", minimum=3, hours=1, from_proposal=True), "costs_incomplete"),
    (dict(title="x", minimum=3, hours=1, ladder=LADDER, unit_cost=Decimal("9")), "costs_incomplete"),
    (dict(title="x", minimum=4, hours=1, ladder=LADDER), "minimum_mismatch"),
    (dict(title="x", minimum=3, hours=73, ladder=LADDER), "duration_too_long"),
    (dict(title="x", minimum=3, hours=1, ladder="3:40,5:41"), "bad_ladder"),
    (dict(title="x", minimum=3, hours=1, ladder="nope"), "bad_ladder"),
    (dict(title="x", minimum=3, hours=1, ladder=LADDER, unit_cost=Decimal("39"), fixed_cost=Decimal("0")), "ladder_loses_money"),
    (dict(title="  ", minimum=3, hours=1, ladder=LADDER), "bad_title"),
])
def test_drop_create_errors_have_a_code_a_message_and_a_next(env, kw, code):
    e = err(tb.drop_create, **kw)
    assert e.code == code and e.message and e.next and "{" not in e.message and "{" not in e.next


def test_guard_errors_are_refused_and_input_errors_are_errors(env):
    d = make()
    assert err(tb.drop_status, "d_nope").status == "error"
    assert err(tb.drop_settle, d, "void:0").status == "refused" and err(tb.drop_settle, d, "void:0").code == "drop_not_closed"
    tb.drop_join(d, "Ada")
    e = err(tb.drop_join, d, "Ada")
    assert (e.code, e.status) == ("already_joined", "refused") and e.next == f"togi crowd drop status --drop {d}"
    assert err(tb.drop_join, d, "Bob", card=99).code == "bad_card"
    assert err(tb.drop_create, title="x", minimum=3, hours=100, ladder=LADDER).status == "refused"


def test_every_error_code_in_the_code_has_wording_and_a_next():
    t = tomllib.load(open(tb.MESSAGES_PATH, "rb"))["errors"]
    for code, ent in t.items():
        assert ent["message"].strip() and ent["next"].strip() and ent["status"] in ("error", "refused"), code
    used = set()
    for f in glob.glob(os.path.join(ROOT, "src", "crowd", "*.py")):
        if f.endswith("togi_bundle.py"):
            continue
        used |= set(re.findall(r'(?:CrowdError|LadderError|StoreError)\(\s*"([a-z_]+)"', open(f).read()))
    used |= set(re.findall(r'_togi_error\(\s*"([a-z_]+)"', open(os.path.join(ROOT, "src/crowd/togi_bundle.py")).read()))
    used -= {"unknown_event", "corrupt"}          # internal store faults: a bug, not a message for the agent
    assert used - set(t) == set(), f"errors without wording: {sorted(used - set(t))}"


def test_wording_comes_from_the_message_file_and_survives_a_broken_one(env, monkeypatch, tmp_path):
    custom = tmp_path / "msgs.toml"
    custom.write_text('[errors.drop_not_found]\nmessage = "Nothing called {drop}."\nnext = "togi crowd drop list"\nstatus = "error"\n')
    monkeypatch.setattr(tb, "MESSAGES_PATH", str(custom))
    tb._MSG_CACHE.clear()
    assert err(tb.drop_status, "d_x").message == "Nothing called d_x."
    custom.write_text("this is [not toml")
    tb._MSG_CACHE.clear()
    e = err(tb.drop_status, "d_x")                     # a damaged message file never crashes the command
    assert e.code == "drop_not_found" and e.message
    monkeypatch.undo()
    tb._MSG_CACHE.clear()


# ---- driving a drop -----------------------------------------------------------------------------------------------

def test_lifecycle_through_the_commands(env):
    d = make()
    s = tb.sim_join(d, 4)
    assert (s["joined"], s["buyers"], s["price"]) == (4, 4, "40.00") and s["locked_step"] == 0
    s = tb.sim_join(d, 3)
    assert (s["buyers"], s["price"], s["locked_step"]) == (7, "33.00", 2)
    assert [b["buyer"] for b in tb.buyers_list(d)][-1] == "sim-007"                 # numbering carries on
    st = tb.drop_status(d, verbose=True)
    assert st["price"] == "33.00" and st["buyers_by_status"] == {"held": 7} and st["next_step"] is None
    assert tb.drop_list()[0]["drop"] == d and tb.drop_list(state="draft") == []
    left = tb.drop_leave(d, "sim-007")
    assert left["buyers"] == 6 and left["price"] == "33.00" and left["released"] == "40.00"       # locked
    assert tb.drop_close(d)["state"] == "closed"
    p = tb.drop_plan(d)
    assert p["summary"] == "capture 6 holds at $33.00 = $198.00" and p["expect"] == "capture:6:33.00:198.00"
    assert p["next"] == f"togi crowd drop settle --drop {d} --expect capture:6:33.00:198.00"


def test_settle_runs_only_with_the_exact_plan_token(env):
    d = make()
    tb.sim_join(d, 4)
    tb.drop_close(d)
    e = err(tb.drop_settle, d, "capture:5:40.00:200.00")
    assert e.code == "plan_changed" and e.status == "refused" and "capture:4:40.00:160.00" in e.next
    assert tb.drop_status(d)["state"] == "closed"                                    # nothing moved
    out = tb.drop_settle(d, "capture:4:40.00:160.00")
    assert out["outcome"] == "funded" and out["totals"]["gross"] == "160.00" and out["next"].endswith(f"verify --drop {d}")
    again = tb.drop_settle(d, "settled")                                              # idempotent
    assert again["already_settled"] is True
    assert err(tb.drop_settle, d, "capture:4:40.00:160.00").code == "plan_changed"


def test_not_funded_through_the_commands(env):
    d = make()
    tb.sim_join(d, 2)
    tb.drop_close(d)
    p = tb.drop_plan(d)
    assert p["summary"] == "void 2 holds, nobody pays" and p["expect"] == "void:2"
    out = tb.drop_settle(d, "void:2")
    assert out["outcome"] == "not_funded" and out["totals"]["gross"] == "0.00"
    v = tb.drop_verify(d)
    assert v["ok"] and "holds" not in v and v["next"].endswith(f"explain --drop {d}")
    assert "holds" in tb.drop_verify(d, verbose=True)


def test_settle_never_runs_on_an_alias_or_an_unknown_drop(env):
    make()
    assert err(tb.drop_settle, "latest", "void:0").code == "drop_not_found"


def test_explain_in_every_state_uses_only_engine_numbers(env):
    d = tb.drop_create(title="Speckled mugs", minimum=3, hours=2, ladder=LADDER)["drop"]
    texts = {"draft": tb.drop_explain(d)["text"]}
    tb.drop_publish(d)
    texts["live"] = tb.drop_explain(d)["text"]
    tb.sim_join(d, 3)
    texts["minimum_met"] = tb.drop_explain(d)["text"]
    tb.drop_close(d)
    texts["closed"] = tb.drop_explain(d)["text"]
    tb.drop_settle(d, tb.drop_plan(d)["expect"])
    texts["settled"] = tb.drop_explain(d)["text"]
    for k, t in texts.items():
        assert t and "{" not in t and "}" not in t, (k, t)
    assert "2 hours" in texts["draft"] and "$40.00" in texts["draft"]
    assert "3 joined" in texts["live"] or "3 joined" in texts["minimum_met"]
    assert "funded" in texts["settled"] and "$40.00" in texts["settled"] and "$120.00" not in texts["settled"]
    facts = tb.drop_explain(d)["facts"]
    assert facts["final_price"] == "40.00" and facts["captured"] == 3 and facts["gross"] == "120.00"


def test_explain_a_missed_minimum_and_a_failed_charge(env):
    d = make()
    tb.sim_join(d, 2)
    tb.drop_close(d)
    tb.drop_settle(d, "void:2")
    t = tb.drop_explain(d)["text"]
    assert "did not reach its minimum: 2 of the 3 needed joined (short by 1). Nobody was charged." in t and "{" not in t
    d2 = make()
    tb.sim_join(d2, 2)
    tb.drop_join(d2, "Card Nine", card=P.DECLINE_CARD_INDEX)
    tb.drop_close(d2)
    tb.drop_settle(d2, tb.drop_plan(d2)["expect"])
    t2 = tb.drop_explain(d2)["text"]
    assert "1 charge(s) failed" in t2 and "{" not in t2 and "1 buyer(s) short of its minimum" in t2     # 2 charged, 3 needed
    d3 = make()
    tb.sim_join(d3, 3)
    tb.drop_join(d3, "Card Nine", card=P.DECLINE_CARD_INDEX)
    tb.drop_close(d3)
    tb.drop_settle(d3, tb.drop_plan(d3)["expect"])
    t3 = tb.drop_explain(d3)["text"]
    assert "1 charge(s) failed" in t3 and "short of its minimum" not in t3                             # 3 charged is enough


# ---- test helpers and safety nets ---------------------------------------------------------------------------------

def test_sim_join_limits(env):
    d = make()
    assert err(tb.sim_join, d, 61).code == "too_many" and err(tb.sim_join, d, 0).code == "too_many"
    assert tb.sim_join(d, 60)["buyers"] == 60


def test_test_reset_wipes_only_in_the_test_environment(env, monkeypatch):
    d = make()
    tb.sim_join(d, 2)
    assert tb.reset_test_store()["drops_removed"] == 1
    assert tb.drop_list() == []
    assert err(tb.drop_status, d).code == "drop_not_found"
    monkeypatch.setenv("CROWD_ENV", "prod")
    e = err(tb.reset_test_store)
    assert (e.code, e.status) == ("not_test_env", "refused")
    monkeypatch.delenv("CROWD_ENV")
    assert err(tb.reset_test_store).code == "not_test_env"


def test_test_settle_is_for_the_fake_in_the_test_environment_only(env, monkeypatch):
    d = make()
    tb.sim_join(d, 3)
    tb.drop_close(d)
    monkeypatch.setenv("CROWD_ENV", "prod")
    assert err(tb.settle_without_approval, d).code == "not_test_env"
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("CROWD_PAYPAL", "sandbox")
    assert err(tb.settle_without_approval, d).code == "stone_requires_fake"
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    assert tb.settle_without_approval(d)["outcome"] == "funded"


def test_a_stone_run_is_refused_unless_paypal_is_the_fake(env, monkeypatch):
    monkeypatch.setenv("TOGI_AGENT", "stone")
    monkeypatch.setenv("CROWD_PAYPAL", "sandbox")
    e = err(tb.drop_list)
    assert (e.code, e.status) == ("stone_requires_fake", "refused")
    monkeypatch.setenv("CROWD_PAYPAL", "fake")
    assert tb.drop_list() == []


def test_missing_sandbox_credentials_are_a_clear_error(env, monkeypatch):
    monkeypatch.setenv("CROWD_PAYPAL", "sandbox")
    monkeypatch.setattr("closure.paypal.load_env", lambda path=None: {"PAYPAL_ENV": "live"})
    e = err(tb.drop_list)
    assert e.code == "paypal_not_configured" and "CROWD_PAYPAL=fake" in e.next
