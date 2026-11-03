"""Crowd Price Studio: the chat beside the live drop, and approving the charge in the panel.

What must hold:
  - the assistant is the demo's restricted `claude -p` (one tool, Bash, restricted mode, dontAsk, only the commands a
    maker needs, a `togi` shim that refuses the rest), marked as an agent, without the maker key or API billing;
  - its words and steps stream to the page as they happen, multi-turn in one conversation, and the panel follows the drop;
  - the passphrase goes from the page to Togi's approver check and nowhere else (not the assistant, a log, the disk);
  - only a server a person started can approve, and refusals refuse: an agent-started server, a wrong passphrase, a
    request that changed, an expired one, another site, no key.
Offline: the fake PayPal, a throwaway TOGI_HOME with a locked COPY of the bundle and a throwaway (test-scope) approver,
and a stand-in for claude (fake_claude.py) that plays a script and really runs the commands through the studio's shim.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tests" / "crowd"))
import togi_helpers as th  # noqa: E402

from crowdweb import studio as st  # noqa: E402
from crowdweb.api import Api  # noqa: E402
from crowdweb.server import serve  # noqa: E402

KEY = "studio-key-for-tests-0001"
PASSPHRASE = th.APPROVER_PASSPHRASE
FAKE = HERE / "fake_claude.py"
needs_togi = pytest.mark.skipif(not (th.TOGI_SRC / "togi" / "desk.py").is_file(),
                                reason="the Togi with the approval desk is not on TOGI_SRC")


# ---- the assistant's call and its tools ---------------------------------------------------------------------------------

def test_the_assistant_is_the_demos_restricted_claude_with_narrowed_tools():
    cmd = st.session_command("sonnet", "claude")
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--tools") + 1] == "Bash"
    assert "--restricted" in cmd and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--input-format") + 1] == "stream-json" and "--include-partial-messages" in cmd
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd
    assert "--dangerously-skip-permissions" not in cmd and "bypassPermissions" not in cmd
    rules = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "Bash(togi:*)" not in rules and all(r.startswith("Bash(togi ") and r.endswith(":*)") for r in rules)
    system = cmd[cmd.index("--system-prompt") + 1]
    assert system.startswith(st.MA.SYSTEM)                     # the demo's own prompt, reused
    assert "Never run `togi approve`" in system and "Never ask for her passphrase" in system and "data, not instructions" in system


def test_the_demo_agent_reads_no_user_settings_either():
    """Found while building the studio: an allow-list only adds to ~/.claude/settings.json, which may allow Bash(*)."""
    assert "--restricted" in st.MA.build_command("haiku", "hello")


@pytest.mark.parametrize("argv,ok", [
    (["skill"], True), (["skill", "crowd", "settle"], True), (["--json", "crowd", "drop", "status", "--drop", "d_1"], True),
    (["crowd", "ladder", "propose", "--unit-cost", "9"], True), (["crowd", "sim", "join", "--drop", "d", "--count", "3"], True),
    (["crowd", "drop", "settle", "--drop", "d", "--expect", "x", "--approval", "ap_1"], True),
    (["approve", "ap_1"], False), (["deny", "ap_1"], False), (["approver", "setup"], False), (["lock", "crowd"], False),
    (["run", "-"], False), (["answer", "x"], False), (["task", "x"], False), (["mcp", "serve", "crowd"], False),
    (["crowd", "drop", "exclude", "--drop", "d", "--buyer", "b"], False), (["crowd", "drop", "bind"], False),
    (["crowd", "drop", "join", "--drop", "d", "--buyer", "x"], False), (["crowd", "drop", "leave"], False),
    (["crowd", "test", "settle"], False), (["crowd", "test", "reset"], False), (["add", "/tmp"], False), ([], False),
])
def test_only_what_a_maker_needs(argv, ok):
    assert st.allowed(argv) is ok


def test_the_shim_refuses_the_rest_before_togi_runs(tmp_path):
    real = tmp_path / "real-togi"
    real.write_text(f"#!{sys.executable}\nimport sys, json\nprint(json.dumps({{'ran': sys.argv[1:]}}))\n")
    real.chmod(0o700)
    shim = st.write_shim(str(real), tmp_path / "bin")
    ok = subprocess.run([str(shim), "--json", "crowd", "drop", "status", "--drop", "d_1"], capture_output=True, text=True)
    assert json.loads(ok.stdout) == {"ran": ["--json", "crowd", "drop", "status", "--drop", "d_1"]}
    for bad in (["approve", "ap_0123456789"], ["crowd", "drop", "exclude", "--drop", "d"], ["run", "-"]):
        r = subprocess.run([str(shim), *bad], capture_output=True, text=True)
        out = json.loads(r.stdout)
        assert r.returncode == 2 and out["error"]["code"] == "not_in_studio" and "ran" not in out


def test_the_assistants_environment(tmp_path):
    base = {"PATH": "/usr/bin:/bin", "CROWD_MAKER_KEY": KEY, "ANTHROPIC_API_KEY": "sk-should-not-pass", "TOGI_RUN": "1",
            "CROWD_STUDIO_MODEL": "x", "TOGI_HOME": "/h", "CROWD_ENV": "test", "CROWD_PAYPAL": "fake", "HOME": "/u"}
    env = st.agent_env(base, tmp_path / "bin")
    assert env["TOGI_AGENT"] == "crowd-studio" and env["PATH"].split(os.pathsep)[0] == str(tmp_path / "bin")
    for gone in ("CROWD_MAKER_KEY", "ANTHROPIC_API_KEY", "TOGI_RUN", "CROWD_STUDIO_MODEL"):
        assert gone not in env
    assert env["TOGI_HOME"] == "/h" and env["CROWD_PAYPAL"] == "fake"


@pytest.mark.parametrize("command,label", [
    ("togi skill", "Read the tool guide"), ("togi skill crowd", "Read how Crowd Price works"),
    ("togi crowd ladder propose --unit-cost 9 --fixed-cost 260 --minimum 30", "Asked the engine for a price ladder from your costs"),
    ("togi crowd drop publish --drop d_1", "Opened the drop to buyers"),
    ("togi crowd sim join --drop d_1 --count 30", "Added 30 test buyers"),
    ("togi crowd drop settle --drop d_1 --expect capture:3:40.00:120.00", "Asked you to approve the charge"),
    ("togi crowd drop settle --drop d_1 --expect x --approval ap_0123456789", "Settled the drop with your approval"),
    ("togi crowd drop verify --drop d_1", "Checked every charge with PayPal"),
    ("togi --json crowd drop plan --drop d_1", "Worked out exactly what settling will charge"),
    ("rm -rf /", "Ran a command"), ("togi 'unbalanced", "Ran a command"),
])
def test_steps_in_plain_words(command, label):
    assert st.step_label(command) == label


def test_how_a_step_ended():
    assert st.result_of('{"ok": true, "data": {}}', False)["state"] == "ok"
    assert st.result_of('{"ok": false, "status": "needs_approval", "error": {"code": "x"}}', True)["state"] == "needs_you"
    r = st.result_of('{"ok": false, "status": "refused", "error": {"code": "not_in_studio", "message": "no"}}', True)
    assert r == {"state": "refused", "outcome": "no"}
    assert st.result_of("Permission to use Bash has been denied", True)["state"] == "refused"
    # Claude Code puts the exit code on its own line before a failed command's output
    asked = 'Exit code 3\n{"ok": false, "status": "needs_approval", "error": {"code": "needs_approval"}, "approval": {"id": "ap_0123456789"}}'
    assert st.result_of(asked, True)["state"] == "needs_you" and st.detail(asked).startswith("Exit code 3\n{")
    assert len(st.detail("x" * 10000)) < st.MAX_RESULT + 100


def test_the_page_never_renders_text_as_html():
    js = (REPO / "web" / "crowdweb" / "static" / "studio.js").read_text()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert bad not in js, bad
    assert "localStorage" not in js            # nothing of the studio's is kept beyond the tab (the key: sessionStorage)


# ---- a studio over HTTP, with the stand-in claude ------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    """A throwaway Togi home with a locked copy of the bundle and a test approver, a store, and the env both the server
    and the assistant use. The person's own approver is never touched (checked at the end)."""
    before = th.real_approver_snapshot()
    home, db = tmp_path / "togi_home", tmp_path / "crowd.db"
    env = th.env_for(home, db)
    th.add_crowd(env, tmp_path)
    th.set_up_approver(home)
    for k in [k for k in os.environ if k.startswith(("TOGI_", "CROWD_"))]:
        monkeypatch.delenv(k)
    for k in ("TOGI_HOME", "TOGI_APPROVER_HOME", "CROWD_DB", "CROWD_ENV", "CROWD_PAYPAL"):
        monkeypatch.setenv(k, env[k])
    bindir = tmp_path / "bin"
    bindir.mkdir()
    togi_bin = bindir / "togi"
    togi_bin.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{th.TOGI_SRC}/togi/_launch.py" "$@"\n')
    togi_bin.chmod(0o700)
    claude = bindir / "claude"
    claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    claude.chmod(0o700)
    w = {"env": env, "home": home, "db": db, "tmp": tmp_path, "togi": str(togi_bin), "claude": str(claude),
         "log": tmp_path / "fake-claude.log", "script": tmp_path / "script.json"}
    yield w
    assert th.approver_files_touched(before, th.real_approver_snapshot()) == []


def run(w, *argv, expect=0, **extra):
    env = dict(w["env"], **extra)
    code, out = th.togi(env, *argv)
    assert code == expect, (argv, code, out)
    return out


def closed_drop(w, joins=3, drop="d_st1"):
    run(w, "crowd", "drop", "create", "--drop-id", drop, "--title", "Speckled mugs", "--minimum", "3", "--hours", "1",
        "--ladder", "3:40,5:36,7:33", "--maker", "Juniper")
    run(w, "crowd", "drop", "publish", "--drop", drop)
    run(w, "crowd", "sim", "join", "--drop", drop, "--count", str(joins))
    run(w, "crowd", "drop", "close", "--drop", drop)
    return drop


def ask_to_settle(w, drop, **extra):
    """What the assistant does: plan, then settle, which stops at needs_approval and files a request."""
    plan = run(w, "crowd", "drop", "plan", "--drop", drop)["data"]["expect"]
    out = run(w, "crowd", "drop", "settle", "--drop", drop, "--expect", plan, expect=3, TOGI_AGENT="crowd-studio", **extra)
    return out["approval"]["id"], plan


def make_studio(w, turns, desk=None, desk_why=None):
    w["script"].write_text(json.dumps(turns))
    base = dict(w["env"], FAKE_CLAUDE_SCRIPT=str(w["script"]), FAKE_CLAUDE_LOG=str(w["log"]),
                CROWD_MAKER_KEY=KEY, ANTHROPIC_API_KEY="sk-must-not-reach-the-agent")
    api = Api(maker_name="Juniper")
    studio = st.Studio(api, model="haiku", claude=w["claude"], togi=w["togi"], desk=desk, desk_why=desk_why, base_env=base)
    api.studio = studio
    return api, studio


@pytest.fixture
def server(world):
    made = []

    def start(turns, desk=None, desk_why=None):
        api, studio = make_studio(world, turns, desk, desk_why)
        httpd = serve(api, KEY, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        made.append((httpd, studio))
        return Client(port), studio
    yield start
    for httpd, studio in made:
        httpd.shutdown()
        httpd.server_close()
        studio.close()


class Client:
    def __init__(self, port):
        self.port, self.base = port, f"http://127.0.0.1:{port}"
        self.origin = self.base

    def call(self, path, body=None, key=KEY, origin="same", headers=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-Maker-Key", key)
        if origin == "same":
            req.add_header("Origin", self.origin)
            req.add_header("Sec-Fetch-Site", "same-origin")
        elif origin:
            req.add_header("Origin", origin)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, path, body=None, **kw):
        code, _, raw = self.call(path, body, **kw)
        return code, json.loads(raw)

    def events(self, until, after=0, epoch=None, timeout=60):
        """Read the stream (asking again each time it ends) until `until(events)` holds; returns the events in order."""
        got, end = [], time.time() + timeout
        epoch = epoch or self.json("/api/studio")[1]["data"]["epoch"]
        while time.time() < end:
            req = urllib.request.Request(f"{self.base}/api/studio/events?after={after}&epoch={epoch}")
            req.add_header("X-Maker-Key", KEY)
            with urllib.request.urlopen(req, timeout=40) as r:
                assert r.headers["Content-Type"].startswith("application/x-ndjson")
                for raw in r:
                    ev = json.loads(raw)
                    if ev["t"] == "ping":
                        continue
                    got.append(ev)
                    after = ev.get("seq", after)
                    if until(got):
                        return got
        raise AssertionError(f"the stream never got there: {[e['t'] for e in got]}")


def turn_over(evs):
    return any(e["t"] == "busy" and e["busy"] is False for e in evs)


def log_lines(w):
    return [json.loads(x) for x in w["log"].read_text().splitlines()] if w["log"].exists() else []


def test_the_studio_page_and_its_files_carry_the_policy(server):
    c, _ = server([])
    code, h, body = c.call("/studio", key=None, origin=None)
    assert code == 200 and b"studio.js" in body and "script-src 'self'" in h["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    for f in ("/static/studio.js", "/static/studio.css"):
        assert c.call(f, key=None, origin=None)[0] == 200


def test_every_studio_route_needs_the_key(server):
    c, _ = server([])
    for path in ("/api/studio", "/api/studio/events", "/api/studio/drops/d_x", "/api/studio/approvals/ap_0123456789"):
        assert c.call(path, key=None)[0] == 401, path
        assert c.call(path, key="wrong-wrong-wrong-wrong")[0] == 401, path
    assert c.call("/api/studio/say", {"text": "hi"}, key=None)[0] == 401


@pytest.mark.parametrize("origin,site", [(None, None), ("http://evil.example", None), ("same", "cross-site"),
                                         ("http://127.0.0.1:1", "same-origin")])
def test_posts_must_come_from_the_studio_page_itself(server, origin, site):
    c, studio = server([[{"say": "hello"}]])
    o = c.origin if origin == "same" else origin
    headers = {"Sec-Fetch-Site": site} if site else {}
    code, out = c.json("/api/studio/say", {"text": "hi"}, origin=o, headers=headers)
    assert code == 403 and out["error"]["code"] == "not_this_page"
    assert not studio.busy and not log_lines(studio_world(studio))


def studio_world(studio):
    return {"log": Path(studio.base_env["FAKE_CLAUDE_LOG"])}


def test_words_and_steps_stream_as_they_happen_and_the_panel_follows_the_drop(server, world):
    drop = "d_live1"
    run(world, "crowd", "drop", "create", "--drop-id", drop, "--title", "Speckled mugs", "--minimum", "3", "--hours", "1",
        "--ladder", "3:40,5:36,7:33", "--maker", "Juniper")
    c, studio = server([[
        {"say": "Opening your drop now, one moment please.", "slow": 0.05},
        {"run": f"togi crowd drop publish --drop {drop}"},
        {"sleep": 1.0},
        {"say": "Your drop is open. <img src=x onerror=alert(1)> is just text."},
    ]])
    code, out = c.json("/api/studio/say", {"text": "Open my mugs drop."})
    assert code == 200
    t0 = time.time()
    first_delta = []
    evs = c.events(lambda e: (first_delta.append(time.time()) if not first_delta and any(x["t"] == "delta" for x in e) else None)
                   or turn_over(e))
    kinds = [e["t"] for e in evs]
    assert kinds[0] == "user" and evs[0]["text"] == "Open my mugs drop."
    assert "delta" in kinds and kinds.index("delta") < kinds.index("step") < kinds.index("step_done")
    assert first_delta and first_delta[0] - t0 < 1.0                # the words arrived before the turn was over
    step = next(e for e in evs if e["t"] == "step")
    done = next(e for e in evs if e["t"] == "step_done")
    assert step["label"] == "Opened the drop to buyers" and step["command"] == f"togi crowd drop publish --drop {drop}"
    assert done["state"] == "ok" and '"state": "live"' in done["result"]
    assert {"t": "focus", "drop": drop}.items() <= next(e for e in evs if e["t"] == "focus").items()
    texts = [e["text"] for e in evs if e["t"] == "text"]
    assert texts[-1] == "Your drop is open. <img src=x onerror=alert(1)> is just text."     # sent as the text it is
    code, panel = c.json(f"/api/studio/drops/{drop}")
    assert code == 200 and panel["data"]["state"] == "live" and panel["data"]["price"] == "40.00"
    # a page that loads now gets each text whole, without the pieces it was streamed in
    later = c.events(turn_over)
    assert not any(e["t"] == "delta" for e in later) and [e["text"] for e in later if e["t"] == "text"] == texts


def test_each_streamed_text_shows_once_whatever_order_claude_sends_it_in(server):
    c, _ = server([[{"say": "First, early.", "early": True}, {"say": "Second, late."}, {"say": "Third, early.", "early": True}]])
    c.json("/api/studio/say", {"text": "go"})
    evs = c.events(turn_over)
    starts = [e["id"] for e in evs if e["t"] == "text_start"]
    texts = [(e["id"], e["text"]) for e in evs if e["t"] == "text"]
    assert [i for i, _ in texts] == starts and [x for _, x in texts] == ["First, early.", "Second, late.", "Third, early."]


def test_one_conversation_many_turns_and_the_assistant_never_gets_the_key(server, world):
    c, studio = server([[{"say": "First answer."}], [{"say": "Second answer."}]])
    assert c.json("/api/studio/say", {"text": "one"})[0] == 200
    c.events(turn_over)
    code, busy = c.json("/api/studio/say", {"text": "two"})
    assert code == 200
    c.events(lambda e: sum(1 for x in e if x["t"] == "busy" and x["busy"] is False) >= 2)
    lines = log_lines(world)
    starts = [x for x in lines if x.get("start")]
    msgs = [x["message"]["message"]["content"] for x in lines if "message" in x]
    assert len(starts) == 1 and msgs == ["one", "two"]                       # one process, two turns
    s0 = starts[0]
    assert "CROWD_MAKER_KEY" not in s0["env"] and "ANTHROPIC_API_KEY" not in s0["env"]
    assert s0["togi_agent"] == "crowd-studio" and s0["path0"] == str(studio._shim_dir)
    assert "--restricted" in s0["argv"] and "dontAsk" in s0["argv"]
    assert KEY not in world["log"].read_text()


def test_a_message_while_the_assistant_works_is_refused(server):
    c, _ = server([[{"sleep": 2}, {"say": "done"}]])
    assert c.json("/api/studio/say", {"text": "one"})[0] == 200
    code, out = c.json("/api/studio/say", {"text": "two"})
    assert code == 409 and out["error"]["code"] == "busy"
    for bad in ("", "   ", None, 5, "x" * (st.MAX_MESSAGE + 1)):
        assert c.json("/api/studio/say", {"text": bad})[0] in (400, 409)


def test_a_refused_command_shows_as_refused(server):
    c, _ = server([[{"run": "togi crowd drop exclude --drop d_1 --buyer sim-001"}, {"say": "I cannot do that here."}]])
    c.json("/api/studio/say", {"text": "Take out sim 1."})
    evs = c.events(turn_over)
    done = next(e for e in evs if e["t"] == "step_done")
    assert done["state"] == "refused" and "only what a maker needs" in done["outcome"]


def test_a_new_conversation_starts_over(server, world):
    c, studio = server([[{"say": "hi"}]])
    c.json("/api/studio/say", {"text": "one"})
    c.events(turn_over)
    code, out = c.json("/api/studio/reset", {})
    assert code == 200 and out["data"]["epoch"] == 2
    assert c.json("/api/studio")[1]["data"]["epoch"] == 2 and studio.events == []


def test_a_draft_shows_in_the_panel(server, world):
    run(world, "crowd", "drop", "create", "--drop-id", "d_draft1", "--title", "Plates", "--minimum", "3", "--hours", "1",
        "--ladder", "3:40,5:36")
    c, _ = server([])
    code, out = c.json("/api/studio/drops/d_draft1")
    assert code == 200 and out["data"]["draft"] is True and out["data"]["state"] == "draft"
    assert c.json("/api/drops/d_draft1")[0] == 404                       # still the maker's only


# ---- approving in the panel -------------------------------------------------------------------------------------------

@pytest.fixture
def desk(world, monkeypatch):
    """A desk opened by this test process, which stands in for the person's own server. pytest's interpreter is not
    isolated, so that one check is taken as passed here (the subprocess tests below run the real launcher)."""
    pytest.importorskip("togi.desk")
    from togi import approvals, desk as tdesk

    monkeypatch.setattr(approvals, "interpreter_problem", lambda: None)
    assert os.environ.get("TOGI_APPROVER_HOME")                       # a throwaway approver: test scope
    return tdesk.Desk.open("crowd studio", [("crowd", "drop settle")])


SETTLE_TURNS = [
    [{"say": "Your drop is closed. Settling charges 3 buyers. The approval is waiting in the panel."}],
    [{"run": "togi crowd drop plan --drop {drop}"}, {"say": "Settled and checked."}],
]


def script_for(drop):
    return [[{k: (v.replace("{drop}", drop) if isinstance(v, str) else v) for k, v in a.items()} for a in t] for t in SETTLE_TURNS]


@needs_togi
def test_the_whole_approval_in_the_panel(server, world, desk):
    drop = closed_drop(world)
    aid, expect = ask_to_settle(world, drop)
    c, studio = server(script_for(drop), desk=desk)
    assert c.json("/api/studio")[1]["data"]["desk"]["open"] is True
    c.json("/api/studio/say", {"text": "Close it and ask me to settle."})
    c.events(turn_over)

    # the panel shows the request Togi recomputed: what will be charged, and the terminal's own lines
    panel = c.json(f"/api/studio/drops/{drop}")[1]["data"]
    assert [a["id"] for a in panel["approvals"] if a["status"] == "pending"] == [aid]
    code, rv = c.json(f"/api/studio/approvals/{aid}")
    rv = rv["data"]
    assert code == 200 and rv["args"] == {"drop": drop, "expect": expect} and rv["title"] == "Speckled mugs"
    assert rv["scope"] == "test" and rv["confirm_word"] == drop and any(expect in ln for ln in rv["lines"])

    # a wrong passphrase approves nothing and says how many tries are left
    code, out = c.json(f"/api/studio/approvals/{aid}/approve", {"review": rv["review"], "passphrase": "not it, not it at all",
                                                                 "word": rv["confirm_word"]})
    assert code == 403 and out["error"]["code"] == "wrong_passphrase" and out["error"]["tries_left"] == 4
    assert c.json(f"/api/studio/drops/{drop}")[1]["data"]["approvals"][0]["status"] == "pending"

    # the right one: Togi signs exactly what was shown; the assistant is told to settle, with the ids only
    code, out = c.json(f"/api/studio/approvals/{aid}/approve", {"review": rv["review"], "passphrase": PASSPHRASE,
                                                                 "word": rv["confirm_word"]})
    assert code == 200 and out["data"]["status"] == "approved" and out["data"]["expires"] == rv["expires_if_approved"]
    evs = c.events(lambda e: sum(1 for x in e if x["t"] == "busy" and x["busy"] is False) >= 2)
    assert any(e["t"] == "note" and e.get("kind") == "approved" for e in evs)
    told = [x["message"]["message"]["content"] for x in log_lines(world) if "message" in x][-1]
    assert aid in told and drop in told and f"--approval {aid}" in told

    # the assistant can now settle once with that approval (the grant is Togi's usual signed one)
    done = run(world, "crowd", "drop", "settle", "--drop", drop, "--expect", expect, "--approval", aid, TOGI_AGENT="crowd-studio")
    assert done["data"]["outcome"] == "funded"
    again = run(world, "crowd", "drop", "settle", "--drop", drop, "--expect", expect, "--approval", aid,
                TOGI_AGENT="crowd-studio", expect=2)
    assert again["error"]["code"] == "approval_used"

    # the passphrase reached nothing but the check: not an event, the assistant, its environment, or any file
    blob = json.dumps(studio.events)
    for secret in (PASSPHRASE, "not it, not it at all"):
        assert secret not in blob and secret not in world["log"].read_text()
        bundle = world["tmp"] / "crowd-bundle"           # the bundle's copied source holds the test passphrase as a constant
        assert not [str(f) for f in world["tmp"].rglob("*")
                    if f.is_file() and bundle not in f.parents and secret.encode() in f.read_bytes()]


@needs_togi
def test_a_request_changed_after_it_was_shown_is_refused(server, world, desk):
    drop = closed_drop(world)
    aid, _ = ask_to_settle(world, drop)
    c, _ = server([], desk=desk)
    rv = c.json(f"/api/studio/approvals/{aid}")[1]["data"]
    f = world["home"] / "approvals" / f"{aid}.json"
    rec = json.loads(f.read_text())
    rec["args"]["expect"] = "capture:3:1.00:3.00"
    f.write_text(json.dumps(rec))
    code, out = c.json(f"/api/studio/approvals/{aid}/approve", {"review": rv["review"], "passphrase": PASSPHRASE})
    assert code == 409 and out["error"]["code"] in ("approval_tampered", "approval_changed")
    assert json.loads(f.read_text())["status"] == "pending"


@needs_togi
def test_an_expired_request_and_a_lapsed_review_are_refused(server, world, desk, monkeypatch):
    from togi import desk as tdesk

    drop = closed_drop(world)
    aid, _ = ask_to_settle(world, drop, TOGI_APPROVAL_TTL="2")           # the request lapses in 2 seconds
    c, _ = server([], desk=desk)
    rv = c.json(f"/api/studio/approvals/{aid}")[1]["data"]
    time.sleep(2.5)
    code, out = c.json(f"/api/studio/approvals/{aid}/approve", {"review": rv["review"], "passphrase": PASSPHRASE})
    assert code == 410 and out["error"]["code"] == "approval_expired"
    assert c.json(f"/api/studio/approvals/{aid}")[1]["error"]["code"] == "approval_expired"

    drop2 = closed_drop(world, drop="d_st2")
    aid2, _ = ask_to_settle(world, drop2)
    monkeypatch.setattr(tdesk, "REVIEW_TTL_S", 0)                       # the page waited too long
    rv2 = c.json(f"/api/studio/approvals/{aid2}")[1]["data"]
    code, out = c.json(f"/api/studio/approvals/{aid2}/approve", {"review": rv2["review"], "passphrase": PASSPHRASE})
    assert code == 409 and out["error"]["code"] == "review_expired" and out["error"]["again"] is True


@needs_togi
def test_approving_needs_the_key_and_the_page_itself(server, world, desk):
    drop = closed_drop(world)
    aid, _ = ask_to_settle(world, drop)
    c, _ = server([], desk=desk)
    rv = c.json(f"/api/studio/approvals/{aid}")[1]["data"]
    body = {"review": rv["review"], "passphrase": PASSPHRASE}
    assert c.json(f"/api/studio/approvals/{aid}/approve", body, key=None)[0] == 401
    assert c.json(f"/api/studio/approvals/{aid}/approve", body, origin="http://evil.example")[0] == 403
    assert c.json(f"/api/studio/approvals/{aid}/approve", body, origin=None)[0] == 403
    assert json.loads((world["home"] / "approvals" / f"{aid}.json").read_text())["status"] == "pending"


def test_a_server_without_a_desk_cannot_approve(server, world):
    c, _ = server([], desk=None, desk_why={"code": "human_only", "message": "TOGI_AGENT is set"})
    info = c.json("/api/studio")[1]["data"]
    assert info["desk"]["open"] is False and "TOGI_AGENT" in info["desk"]["why"]
    code, out = c.json("/api/studio/approvals/ap_0123456789")
    assert code == 403 and out["error"]["code"] == "desk_closed"
    code, out = c.json("/api/studio/approvals/ap_0123456789/approve", {"review": "rv_x", "passphrase": PASSPHRASE})
    assert code == 403 and out["error"]["code"] == "desk_closed"


# ---- the real launcher: who started the server decides whether it can approve -----------------------------------------------

def launch(world, port, *extra_argv, **env_extra):
    env = {k: v for k, v in world["env"].items() if k != "PYTHONPATH"}
    env.update(TOGI_SRC=str(th.TOGI_SRC), CROWD_MAKER_KEY=KEY, PATH=f"{Path(world['togi']).parent}{os.pathsep}{os.environ['PATH']}")
    env.update({k: v for k, v in env_extra.items() if v is not None})
    argv = [sys.executable, "-I", str(REPO / "web" / "studio.py"), "--port", str(port), "--claude", world["claude"], *extra_argv]
    return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(world["tmp"]))


def first_lines(proc, n=5, timeout=30):
    out, end = [], time.time() + timeout
    while len(out) < n and time.time() < end:
        line = proc.stdout.readline()
        if not line:
            break
        out.append(line.rstrip("\n"))
    return out


@needs_togi
@pytest.mark.parametrize("who,expect", [({}, "on (a TEST approver"), ({"TOGI_AGENT": "maker-demo"}, "off (the approval desk does not open in an agent's session"),
                                        ({"TOGI_AGENT": ""}, "off ("), ({"TOGI_RUN": "1"}, "off (the approval desk does not open inside `togi run`")])
def test_only_a_server_a_person_started_offers_approving(world, who, expect):
    p = launch(world, 0, **who)                  # port 0: the server picks a free one and prints it
    try:
        lines = first_lines(p, 4)
        desk_line = next((x for x in lines if "approving in the panel" in x), "")
        assert expect in desk_line, lines
        if who:
            port = int(re.search(r"localhost:(\d+)", lines[0]).group(1))
            c = Client(port)
            for _ in range(50):
                try:
                    code, out = c.json("/api/studio/approvals/ap_0123456789")
                    break
                except urllib.error.URLError:
                    time.sleep(0.1)
            assert code == 403 and out["error"]["code"] == "desk_closed"
    finally:
        p.terminate()
        p.wait(timeout=10)


@needs_togi
def test_a_server_started_without_isolation_cannot_approve(world):
    port = 0
    env = {k: v for k, v in world["env"].items()}
    env.update(PYTHONPATH=os.pathsep.join([str(REPO / "src"), str(REPO / "web"), str(th.TOGI_SRC)]), CROWD_MAKER_KEY=KEY)
    p = subprocess.Popen([sys.executable, "-m", "crowdweb.studio_main", "--port", str(port), "--claude", world["claude"]], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(world["tmp"]))
    try:
        lines = first_lines(p, 4)
        assert any("approving in the panel: off" in x and "not isolated" in x for x in lines), lines
    finally:
        p.terminate()
        p.wait(timeout=10)


def test_the_web_layer_still_cannot_settle_exclude_or_bind():
    src = "\n".join(p.read_text() for p in (REPO / "web" / "crowdweb").rglob("*.py"))
    assert not re.search(r"\.(settle|exclude|bind_plan|reconcile)\(", src)
    assert "togi crowd drop settle" in st.CONTINUE           # settling is only ever asked of the assistant, through Togi
