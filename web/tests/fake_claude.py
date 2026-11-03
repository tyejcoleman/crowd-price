#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format stream-json --include-partial-messages`, for the
studio's tests: no model, no network, no subscription. It is an MCP client, as claude is: it starts the one server the
studio's `--mcp-config` names (the studio's gate, which runs the real `togi mcp serve crowd`), does the handshake, lists its
tools and calls them for real.

It reads one user message per stdin line and plays the next turn of a script (JSON, path in FAKE_CLAUDE_SCRIPT):
  [[{"say": "words"}, {"call": "crowd_drop_status", "args": {"drop": "d_x"}}, ...], [...next turn...]]
`say` streams the words as partial text deltas, then the whole text. `call` emits a tool_use for `mcp__togi__<tool>` and,
like claude, refuses it with claude's own denial if `--allowedTools` does not name it; with "bypass": true it skips that
check (a stand-in for a hole in claude's permissions) and the call goes to the server, so the gate's own refusal is what
comes back. An argument "$APPROVAL" is the approval id the user message names (the studio's message after an approval).
`tool` emits a tool_use for any other tool name (a built-in such as Bash) with a scripted `output` as its result,
as if it had run: the studio must never read it. `sleep` pauses. A turn ends with a `result` event. Every message
received, the argv, the environment's names, the tools the server listed and every call are appended to FAKE_CLAUDE_LOG.
"""
import json
import os
import re
import subprocess
import sys
import time

SCRIPT = json.load(open(os.environ["FAKE_CLAUDE_SCRIPT"], encoding="utf-8"))
LOG = os.environ["FAKE_CLAUDE_LOG"]
ARGV = sys.argv[1:]
n_msg = 0


def flag(name):
    return ARGV[ARGV.index(name) + 1] if name in ARGV and ARGV.index(name) + 1 < len(ARGV) else None


def out(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


def note(rec):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


class Server:
    """The one MCP server of --mcp-config, over stdio: newline-delimited JSON-RPC."""

    def __init__(self, name, spec):
        self.name, self.n = name, 0
        env = dict(os.environ, **(spec.get("env") or {}))
        self.p = subprocess.Popen([spec["command"], *spec.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  env=env, text=True, bufsize=1)
        init = self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "fake-claude", "version": "0"}})
        assert "result" in init, init
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = [t["name"] for t in self.rpc("tools/list", {})["result"]["tools"]]

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def rpc(self, method, params):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("the MCP server went away")
            msg = json.loads(line)
            if msg.get("id") == self.n and "method" not in msg:
                return msg

    def call(self, tool, args):
        r = self.rpc("tools/call", {"name": tool, "arguments": args})
        if "error" in r:
            return r["error"].get("message", "error"), True
        res = r["result"]
        return "".join(c.get("text", "") for c in res.get("content") or []), bool(res.get("isError"))


cfg = json.load(open(flag("--mcp-config"), encoding="utf-8")) if flag("--mcp-config") else {"mcpServers": {}}
servers = {name: Server(name, spec) for name, spec in cfg["mcpServers"].items()}
allowed = set((flag("--allowedTools") or "").split(","))
tools = [f"mcp__{s}__{t}" for s, srv in servers.items() for t in srv.tools]
note({"start": True, "pid": os.getpid(), "argv": ARGV, "env": sorted(os.environ), "cwd": os.getcwd(),
      "togi_agent": os.environ.get("TOGI_AGENT"), "tools": tools})
out({"type": "system", "subtype": "init", "tools": tools, "permissionMode": flag("--permission-mode")})


def tool_use(name, args):
    global n_msg
    n_msg += 1
    tid = f"toolu_{turn}_{n_msg}"
    out({"type": "assistant", "message": {"id": f"msg_{n_msg}", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": args}]}})
    return tid


def tool_result(tid, text, is_error):
    out({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}], "is_error": is_error}]}})


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
        elif "call" in a:
            server = a.get("server", "togi")
            name = f"mcp__{server}__{a['call']}"
            told = re.search(r"ap_[0-9a-f]{6,32}", json.dumps(msg))       # "$APPROVAL": the approval id the message names
            args = {k: (told.group(0) if v == "$APPROVAL" and told else v) for k, v in (a.get("args") or {}).items()}
            tid = tool_use(name, args)
            if name not in allowed and not a.get("bypass"):
                text, err = (f"Permission to use {name} has been denied because Claude Code is running in don't ask mode.", True)
            else:
                text, err = servers[server].call(a["call"], args)
            note({"call": a["call"], "args": args, "is_error": err, "reply": text[:400]})
            tool_result(tid, text, err)
        elif "tool" in a:
            tid = tool_use(a["tool"], a.get("input") or {})
            tool_result(tid, a.get("output", ""), bool(a.get("is_error")))
    out({"type": "result", "subtype": "success", "num_turns": turn, "result": ""})
