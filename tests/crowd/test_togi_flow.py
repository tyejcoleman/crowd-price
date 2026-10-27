"""End to end through the real Togi host (CLI), fake PayPal, isolated TOGI_HOME and store."""
import json

import pytest

from togi_helpers import REPO, add_crowd, env_for, person_in_a_terminal, set_up_approver, togi, togi_available

pytestmark = pytest.mark.skipif(not togi_available(), reason="togi is not installed (set TOGI_SRC or install it)")

LAD = "3:40,5:36,7:33"


@pytest.fixture
def env(tmp_path):
    e = env_for(tmp_path / "togi_home", tmp_path / "crowd.db")
    add_crowd(e, tmp_path)
    set_up_approver(tmp_path / "togi_home")           # Togi refuses every approval until a person has set a passphrase
    return e


def run(env, *argv, expect=0):
    code, out = togi(env, *argv)
    assert code == expect, (argv, code, out)
    return out


def new_drop(env, joins=0, drop_id="d_t1"):
    run(env, "crowd", "drop", "create", "--drop-id", drop_id, "--title", "Speckled mugs", "--minimum", "3", "--hours", "1",
        "--ladder", LAD)
    run(env, "crowd", "drop", "publish", "--drop", drop_id)
    if joins:
        run(env, "crowd", "sim", "join", "--drop", drop_id, "--count", str(joins))
    return drop_id


def test_the_bundle_registers_and_serves_its_skill(env):
    assert run(env, "bundles")["data"][0]["name"] == "crowd"
    skill = (REPO / "skills" / "SKILL.md").read_text()
    assert len(skill.splitlines()) <= 60
    code, text = togi(env, "skill", "crowd")
    assert code == 0 and "togi crowd drop settle" in text and "Never run `togi approve`" in text
    for topic in ("ladder", "settle", "troubleshooting"):
        assert (REPO / "skills" / f"{topic}.md").is_file()
        code, t = togi(env, "skill", "crowd", topic)
        assert code == 0 and len(t) > 100
    index = togi(env, "skill")[1]
    assert "- crowd: Group-price drops on PayPal holds" in index


def test_tools_list_matches_the_declared_effects(env):
    from crowd import togi_bundle as tb
    tools = run(env, "tools", "crowd", "--json", "--full")["data"]      # --full: Togi caps long listings
    got = {t["command"]: t["effect"] for t in tools}
    assert got == tb.EFFECTS


def test_full_flow_to_needs_approval_then_a_person_approves(env):
    run(env, "crowd", "test", "reset")
    prop = run(env, "crowd", "ladder", "propose", "--unit-cost", "9", "--fixed-cost", "260", "--minimum", "30")["data"]
    assert prop["ladder"] == "30:40,45:37,60:33,75:30"
    d = run(env, "crowd", "drop", "create", "--title", "Speckled mugs", "--minimum", "30", "--hours", "48",
            "--from-proposal", "--unit-cost", "9", "--fixed-cost", "260")["data"]["drop"]
    assert run(env, "crowd", "drop", "publish", "--drop", d)["data"]["state"] == "live"
    run(env, "crowd", "sim", "join", "--drop", d, "--count", "30")
    s = run(env, "crowd", "drop", "status", "--drop", d)["data"]
    assert (s["price"], s["buyers"], s["locked_step"]) == ("40.00", 30, 0)
    run(env, "crowd", "sim", "join", "--drop", d, "--count", "15")
    s = run(env, "crowd", "drop", "status", "--drop", d)["data"]
    assert (s["price"], s["buyers"], s["locked_step"], s["locked_price"]) == ("37.00", 45, 1, "37.00")
    run(env, "crowd", "drop", "leave", "--drop", d, "--buyer", "sim-045")
    s = run(env, "crowd", "drop", "status", "--drop", d)["data"]
    assert (s["price"], s["buyers"]) == ("37.00", 44)                       # the step stays locked after a leave
    assert run(env, "crowd", "drop", "close", "--drop", d)["data"]["state"] == "closed"
    plan = run(env, "crowd", "drop", "plan", "--drop", d)["data"]
    assert plan["summary"] == "capture 44 holds at $37.00 = $1,628.00"

    # settle: the host stops it, writes a pending approval and does NOT run the function
    out = run(env, "crowd", "drop", "settle", "--drop", d, "--expect", plan["expect"], expect=3)
    assert out["status"] == "needs_approval"
    ap = out["approval"]
    assert ap["args"] == {"drop": d, "expect": plan["expect"]} and plan["expect"] in ap["summary"]
    assert run(env, "crowd", "drop", "status", "--drop", d)["data"]["state"] == "closed"        # nothing moved
    assert run(env, "crowd", "drop", "verify", "--drop", d, expect=2)["error"]["code"] == "drop_not_settled"

    # an agent cannot approve (no terminal / TOGI_AGENT set)
    code, o = togi(env, "approve", ap["id"], input=d + "\n")
    assert code == 2 and o["error"]["code"] == "needs_tty"
    code, o = person_in_a_terminal({**env, "TOGI_AGENT": "stone"}, ["approve", ap["id"]], [d])
    assert code == 2 and "human_only" in o

    # a person approves in a terminal by typing the drop id
    code, o = person_in_a_terminal(env, ["approve", ap["id"]], [d])
    assert code == 0, o
    done = run(env, "crowd", "drop", "settle", "--drop", d, "--expect", plan["expect"], "--approval", ap["id"])["data"]
    assert done["outcome"] == "funded" and done["final_price"] == "37.00" and done["totals"]["gross"] == "1628.00"
    # single use
    again = run(env, "crowd", "drop", "settle", "--drop", d, "--expect", plan["expect"], "--approval", ap["id"], expect=2)
    assert again["error"]["code"] == "approval_used"
    v = run(env, "crowd", "drop", "verify", "--drop", d)["data"]
    assert v["ok"] is True and v["counts"] == {"holds": 45, "captured": 44, "voided": 1, "failed": 0}
    text = run(env, "crowd", "drop", "explain", "--drop", d)["data"]["text"]
    assert "was funded" in text and "$37.00" in text
    assert run(env, "crowd", "drop", "tieout", "--drop", d)["data"]["reason"] == "no_transaction_search"


def test_an_approval_is_bound_to_the_exact_drop_and_plan(env):
    a, b = new_drop(env, 3, "d_a1"), new_drop(env, 4, "d_b2")
    for d in (a, b):
        run(env, "crowd", "drop", "close", "--drop", d)
    pa = run(env, "crowd", "drop", "plan", "--drop", a)["data"]["expect"]
    ap = run(env, "crowd", "drop", "settle", "--drop", a, "--expect", pa, expect=3)["approval"]["id"]
    assert person_in_a_terminal(env, ["approve", ap], [a])[0] == 0
    pb = run(env, "crowd", "drop", "plan", "--drop", b)["data"]["expect"]
    out = run(env, "crowd", "drop", "settle", "--drop", b, "--expect", pb, "--approval", ap, expect=2)
    assert out["error"]["code"] == "approval_mismatch"                       # approved for drop a, not drop b
    wrong = run(env, "crowd", "drop", "settle", "--drop", a, "--expect", "capture:9:9.00:81.00", "--approval", ap, expect=2)
    assert wrong["error"]["code"] == "approval_mismatch"                     # nor for a different plan
    assert run(env, "crowd", "drop", "status", "--drop", b)["data"]["state"] == "closed"


def test_a_person_can_deny_and_nothing_is_settled(env):
    d = new_drop(env, 3)
    run(env, "crowd", "drop", "close", "--drop", d)
    plan = run(env, "crowd", "drop", "plan", "--drop", d)["data"]["expect"]
    ap = run(env, "crowd", "drop", "settle", "--drop", d, "--expect", plan, expect=3)["approval"]["id"]
    assert person_in_a_terminal(env, ["deny", ap], [d])[0] == 0
    out = run(env, "crowd", "drop", "settle", "--drop", d, "--expect", plan, "--approval", ap, expect=2)
    assert out["error"]["code"] == "approval_denied"
    assert run(env, "crowd", "drop", "status", "--drop", d)["data"]["state"] == "closed"


def test_dry_run_of_settle_does_not_ask_or_move_anything(env):
    d = new_drop(env, 3)
    run(env, "crowd", "drop", "close", "--drop", d)
    out = run(env, "crowd", "drop", "settle", "--drop", d, "--expect", "capture:3:40.00:120.00", "--dry-run")["data"]
    assert out["dry_run"] is True and out["requires_approval"] is True
    assert run(env, "crowd", "drop", "status", "--drop", d)["data"]["state"] == "closed"


def test_error_envelopes_name_the_next_command(env):
    out = run(env, "crowd", "drop", "status", "--drop", "d_nope", expect=1)
    assert out["error"]["code"] == "drop_not_found" and out["next"] == "togi crowd drop list"
    d = new_drop(env, 3)
    out = run(env, "crowd", "drop", "settle", "--drop", d, "--expect", "void:0", "--approval", "ap_nope", expect=2)
    assert out["ok"] is False
    out = run(env, "crowd", "drop", "join", "--drop", d, "--buyer", "sim-001", expect=2)
    assert out["error"]["code"] == "already_joined" and out["next"].startswith("togi crowd")
    # a wrong flag gets a pointer to --help, and settle without --expect says how to pass it
    out = run(env, "crowd", "drop", "create", "--title", "x", "--minimum", "3", "--days", "2", "--ladder", LAD, expect=1)
    assert out["error"]["code"] == "unknown_flag" and "--help" in out["next"]
    out = run(env, "crowd", "drop", "settle", "--drop", d, expect=1)
    assert out["error"]["code"] == "missing_arg" and "--expect" in out["next"]


def test_an_agent_in_a_stone_run_cannot_reach_real_paypal(env):
    out = run({**env, "TOGI_AGENT": "stone", "CROWD_PAYPAL": "sandbox"}, "crowd", "drop", "list", expect=2)
    assert out["error"]["code"] == "stone_requires_fake"


def test_journal_records_commands_without_values(env):
    d = new_drop(env, 1)
    run(env, "crowd", "drop", "status", "--drop", d)
    code, j = togi(env, "journal", "crowd", "--json")
    blob = json.dumps(j)
    assert code == 0 and "drop status" in blob and d not in blob and "Speckled" not in blob
