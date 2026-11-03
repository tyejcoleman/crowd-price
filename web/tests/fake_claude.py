#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format stream-json --include-partial-messages`, for the
studio's tests: no model, no network, no subscription.

It reads one user message per stdin line and plays the next turn of a script (JSON, path in FAKE_CLAUDE_SCRIPT):
  [[{"say": "words"}, {"run": "togi crowd drop status --drop d_x"}, ...], [...next turn...]]
`say` streams the words as partial text deltas, then the whole text; `run` emits a tool_use, really runs the command
(through the PATH the studio gave it, so the studio's `togi` shim and TOGI_AGENT apply) and returns its output as the
tool_result, as claude's Bash tool would. `sleep` pauses (to prove the page gets words before the turn ends). A turn ends
with a `result` event. Every message received, the argv and the environment's names are appended to FAKE_CLAUDE_LOG.
"""
import json
import os
import shlex
import subprocess
import sys
import time

SCRIPT = json.load(open(os.environ["FAKE_CLAUDE_SCRIPT"], encoding="utf-8"))
LOG = os.environ["FAKE_CLAUDE_LOG"]
n_msg = 0


def out(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def note(rec):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


note({"start": True, "pid": os.getpid(), "argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd(),
      "togi_agent": os.environ.get("TOGI_AGENT"), "path0": os.environ.get("PATH", "").split(os.pathsep)[0]})
out({"type": "system", "subtype": "init", "tools": ["Bash"], "permissionMode": "dontAsk"})
turn = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    note({"pid": os.getpid(), "message": msg})
    actions = SCRIPT[turn] if turn < len(SCRIPT) else [{"say": "That is all I was scripted to say."}]
    turn += 1
    for a in actions:
        if "sleep" in a:
            time.sleep(a["sleep"])
        elif "say" in a:
            n_msg += 1
            mid = f"msg_{n_msg}"
            out({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
            out({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}})
            words = a["say"].split(" ")
            for i, w in enumerate(words):
                out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                       "delta": {"type": "text_delta", "text": (" " if i else "") + w}}})
                if a.get("slow"):
                    time.sleep(a["slow"])
            whole = {"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": a["say"]}]}}
            if a.get("early"):          # claude sometimes sends the whole block before the block's stop event
                out(whole)
            out({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}})
            if not a.get("early"):
                out(whole)
        elif "run" in a:
            n_msg += 1
            tid = f"toolu_{turn}_{n_msg}"
            out({"type": "assistant", "message": {"id": f"msg_{n_msg}", "content": [
                {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": a["run"]}}]}})
            r = subprocess.run(shlex.split(a["run"]), capture_output=True, text=True, timeout=120)
            text = (r.stdout or "") + (r.stderr or "")
            if r.returncode:                  # as Claude Code's Bash tool reports a non-zero exit
                text = f"Exit code {r.returncode}\n{text}"
            out({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": text.strip(), "is_error": r.returncode not in (0, 2, 3)}]}})
    out({"type": "result", "subtype": "success", "num_turns": turn, "result": ""})
