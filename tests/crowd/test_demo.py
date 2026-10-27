"""The demo client: its claude call is locked down, and its transcript reader copes with claude's stream."""
import importlib.util
import json
import os

spec = importlib.util.spec_from_file_location(
    "maker_agent", os.path.join(os.path.dirname(__file__), "..", "..", "demo", "maker_agent.py"))
ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ma)


def test_claude_is_called_with_one_tool_restricted_to_togi():
    cmd = ma.build_command("haiku", "hello")
    assert cmd[:3] == ["claude", "-p", "hello"]
    assert cmd[cmd.index("--tools") + 1] == "Bash"
    assert cmd[cmd.index("--allowedTools") + 1] == "Bash(togi:*)"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd and "--permission-mode" not in cmd
    assert "Never run `togi approve`" in ma.SYSTEM


def test_environment_marks_the_agent_and_picks_fake_or_sandbox(monkeypatch):
    monkeypatch.setenv("CROWD_PAYPAL", "sandbox")
    fake = ma.agent_env(True)
    assert fake["TOGI_AGENT"] == "maker-demo" and fake["CROWD_ENV"] == "test" and fake["CROWD_PAYPAL"] == "fake"
    live = ma.agent_env(False)
    assert "CROWD_PAYPAL" not in live and "CROWD_ENV" not in live and live["TOGI_AGENT"] == "maker-demo"


def test_scenarios_never_tell_the_agent_a_price():
    for text in ma.SCENARIOS.values():
        assert "$37" not in text and "$40" not in text
    assert "togi crowd drop plan" in ma.SCENARIOS["finish"]


def test_transcript_reads_the_stream_and_remembers_ids():
    lines = []
    tr = ma.Transcript(ma.Painter(False), out=lines.append)
    tr.feed({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Opening the drop."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "togi crowd drop publish --drop d_ab12cd34"}}]}})
    tr.feed({"type": "user", "message": {"content": [{"type": "tool_result", "content": [
        {"type": "text", "text": json.dumps({"ok": True, "data": {"drop": "d_ab12cd34", "state": "draft"}})}]}]}})
    tr.feed({"type": "user", "message": {"content": [{"type": "tool_result", "content": json.dumps({
        "ok": False, "status": "needs_approval", "error": {"code": "needs_approval", "message": "needs a person"},
        "next": "a person runs: togi approve ap_0123456789",
        "approval": {"id": "ap_0123456789", "command": "crowd drop settle"}})}]}})
    tr.feed({"type": "result", "result": "Needs your approval.", "num_turns": 7, "total_cost_usd": 0.0123, "duration_ms": 9000})
    text = "\n".join(lines)
    assert "Agent" in text and "$ togi crowd drop publish --drop d_ab12cd34" in text
    assert "needs_approval: needs_approval - needs a person" in text and "togi approve ap_0123456789" in text
    assert tr.drop == "d_ab12cd34" and tr.approval == "ap_0123456789" and tr.final == "Needs your approval."
    assert tr.summary() == "done: 7 turns, 9s, reported cost $0.0123"


def test_the_final_message_is_not_printed_twice(monkeypatch):
    lines = []
    tr = ma.Transcript(ma.Painter(False), out=lines.append)
    tr.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "All done."}]}})
    tr.feed({"type": "result", "result": "All done."})
    assert tr.final.strip() == tr.last_text and len(lines) == 1


def test_transcript_ignores_shapes_it_does_not_know():
    tr = ma.Transcript(ma.Painter(False), out=lambda s: None)
    for ev in ({"type": "system", "subtype": "init"}, {"type": "assistant", "message": {}},
               {"type": "user", "message": {"content": "plain text"}}, {"type": "mystery"}):
        tr.feed(ev)
    assert tr.summary() == "done"


def test_brief_trims_long_output_and_handles_non_json():
    assert ma.brief("plain words") == "plain words"
    big = json.dumps({"ok": True, "data": {"x": "y" * 1000}})
    assert ma.brief(big).endswith("...") and len(ma.brief(big)) < 500
