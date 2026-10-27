"""The stone's agent tasks: well-formed, and solvable by their own `script` (a stand-in for the script driver)."""
import glob
import os
import re
import shlex
import tomllib

import pytest

from togi_helpers import REPO, env_for, togi, togi_available

TASKS = sorted(glob.glob(str(REPO / "stone" / "tasks" / "*.toml")))


def load(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_task_set_shape():
    tasks = [load(p) for p in TASKS]
    assert 5 <= len(tasks) <= 8
    assert len({t["id"] for t in tasks}) == len(tasks)
    assert sum(1 for t in tasks if t["holdout"]) >= 2 and sum(1 for t in tasks if not t["holdout"]) >= 4
    for p, t in zip(TASKS, tasks):
        assert os.path.basename(p).endswith(t["id"] + ".toml")
        assert t["instruction"].strip() and isinstance(t["budget_turns"], int) and t["budget_turns"] >= 8
        assert t["setup"][0] == "togi crowd test reset", "every task starts from an empty store"
        assert t["check"] and all(("cmd" in c) != ("transcript_contains" in c) for c in t["check"])
        prefixes = [f["command_prefix"] for f in t["forbid"]]
        assert "togi approve" in prefixes, f"{t['id']} must forbid approving"
        assert "togi crowd test" in prefixes
        for cmd in t["script"]:
            assert not any(cmd.startswith(x) for x in prefixes), (t["id"], cmd)
        assert "togi approve" not in t["instruction"]


def test_the_stone_covers_the_flows_the_spec_asks_for():
    ids = {load(p)["id"] for p in TASKS}
    assert ids == {"open-drop", "price-after-joins", "request-settlement", "missed-minimum", "leave-after-lock",
                   "explain-outcome", "recover-wrong-flag"}
    assert {load(p)["id"] for p in TASKS if load(p)["holdout"]} == {"explain-outcome", "recover-wrong-flag"}


def jsonpath(doc, path):
    cur = doc
    for tok in re.findall(r"[^.\[\]$]+|\[\d+\]", path):
        cur = cur[int(tok[1:-1])] if tok.startswith("[") else cur[tok]
    return cur


@pytest.mark.skipif(not togi_available(), reason="togi is not installed")
@pytest.mark.parametrize("path", TASKS, ids=[os.path.basename(p)[:-5] for p in TASKS])
def test_each_task_is_solved_by_its_script_and_its_checks_pass(path, tmp_path):
    task = load(path)
    env = env_for(tmp_path / "home", tmp_path / "crowd.db")
    assert togi(env, "add", str(REPO))[0] == 0

    def sh(cmd):
        argv = shlex.split(cmd)
        assert argv[0] == "togi", cmd
        return togi(env, *argv[1:])

    for cmd in task["setup"]:
        code, out = sh(cmd)
        assert code == 0, (cmd, out)
    transcript = ""
    for cmd in task["script"]:
        code, out = sh(cmd)
        transcript += out if isinstance(out, str) else repr(out)
    for chk in task["check"]:
        if "cmd" in chk:
            code, out = sh(chk["cmd"])
            assert code == 0, (chk, out)
            assert jsonpath(out, chk["expect_jsonpath"]) == chk["equals"], (task["id"], chk, out)
        else:
            assert chk["transcript_contains"] in transcript, (task["id"], chk)
