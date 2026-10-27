#!/usr/bin/env python3
"""The demo's AI client: Juniper the potter's assistant, driving Crowd Price through the `togi` CLI.

It runs `claude -p` in an empty temp directory with ONE tool (Bash) limited to `togi ...` commands, and prints
a readable transcript as the agent works. The agent never sets a price (the engine does), and it cannot
approve a settlement (a person has to, in their own terminal).

  python3 demo/maker_agent.py --fake                      # whole story, no PayPal, free and fast
  python3 demo/maker_agent.py --fake --scenario open      # stop at "needs approval"
  python3 demo/maker_agent.py --scenario missed           # a drop that misses its minimum (live sandbox)
  python3 demo/maker_agent.py --scenario finish --drop d_ab12cd34 --approval ap_0123456789

Scenarios: open (open + fill + close + ask to settle), missed (a drop that misses its minimum),
finish (settle with an approval the person gave, verify, explain), full (open, wait for you to approve,
then finish). --fake uses the fake PayPal and a throwaway store; without it the agent works in the PayPal
sandbox (needs .env sandbox credentials).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time

SYSTEM = (
    "You are the assistant of Juniper, a potter who sells small-batch mugs through Crowd Price drops. "
    "Run `togi skill` first, then `togi skill crowd`, and follow it. Use only `togi` commands in Bash. "
    "Every price, total and count you state must be copied from a command's output; you never calculate or "
    "choose a price. Never run `togi approve` or `togi deny`: when a command needs a person's approval, stop "
    "and tell Juniper what she is approving and the approval id. Keep your replies short and plain."
)

SCENARIOS = {
    "open": (
        "I'm Juniper. I'm firing a batch of speckled mugs. Clay and glaze cost me 9 dollars a mug and the kiln "
        "firing is 260 dollars, and I need at least 30 mugs sold or I lose money. Please open a 48 hour drop "
        "called 'Speckled mugs' with a price ladder built from my costs, and publish it. Then, because this is "
        "a demo, add 52 test buyers in a few batches (30, then 15, then 7) and after each batch tell me the price "
        "everyone pays now. When that is done, close the drop and ask for it to be settled. Stop when it needs me "
        "and tell me exactly what will be charged and the approval id."),
    "missed": (
        "I'm Juniper. Open a second 48 hour drop called 'Glazed plates' for me: plates cost 7 dollars each, the "
        "firing is 180 dollars, and I need at least 30 sold. Build the ladder from those costs and publish it. "
        "For the demo add 22 test buyers, then close the drop and ask for it to be settled. Stop when it needs "
        "me, and tell me what happens to the buyers' money."),
    "finish": (
        "I'm Juniper. I approved the settlement for drop {drop}; the approval id is {approval}. Please settle it "
        "now with that approval (get the --expect token from `togi crowd drop plan`), check it against PayPal "
        "with the verify command, and explain the outcome to me in plain words."),
}


def build_command(model, prompt, claude="claude"):
    """The exact `claude -p` call: one tool (Bash), only `togi` commands, no saved session, no MCP servers."""
    return [claude, "-p", prompt, "--model", model, "--system-prompt", SYSTEM,
            "--tools", "Bash", "--allowedTools", "Bash(togi:*)", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--output-format", "stream-json", "--verbose"]


def agent_env(fake):
    env = dict(os.environ)
    env["TOGI_AGENT"] = "maker-demo"
    for k in ("CROWD_ENV", "CROWD_PAYPAL"):
        env.pop(k, None)
    if fake:
        env.update(CROWD_ENV="test", CROWD_PAYPAL="fake")
    return env


def setup_env(env):
    """The environment for setup commands run by this launcher: the agent's, without the agent marker."""
    return {k: v for k, v in env.items() if k != "TOGI_AGENT"}


class Painter:
    def __init__(self, color):
        self.color = color

    def c(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.color else s

    def agent(self, s):
        return self.c("1;35", "Agent  ") + " " + textwrap.fill(s.strip(), 100, subsequent_indent=" " * 8)

    def cmd(self, s):
        return self.c("1;36", "$ ") + self.c("36", s)

    def out(self, s):
        return self.c("2", s)

    def alert(self, s):
        return self.c("1;33", s)


def brief(result_text, limit=420):
    """A tool result as one readable line: envelope status first, data trimmed."""
    text = result_text.strip()
    try:
        d = json.loads(text)
    except ValueError:
        return text[:limit] + (" ..." if len(text) > limit else "")
    if isinstance(d, dict) and "ok" in d:
        if d["ok"]:
            body = json.dumps(d.get("data"), separators=(",", ":"), ensure_ascii=False)
            return "ok " + body[:limit] + (" ..." if len(body) > limit else "")
        err = d.get("error") or {}
        line = f"{d.get('status', 'error')}: {err.get('code', '')} - {err.get('message', '')}"
        if d.get("next"):
            line += f"  [next: {d['next']}]"
        return line[:limit + 120]
    return text[:limit]


def result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


class Transcript:
    """Turns claude's stream-json events into readable lines, and remembers the ids the story needs."""

    def __init__(self, painter, out=print):
        self.p, self.out, self.drop, self.approval, self.final = painter, out, None, None, None
        self.last_text = None
        self.cost = self.turns = self.ms = None

    def feed(self, ev):
        t = ev.get("type")
        msg = ev.get("message") or {}
        if t == "assistant":
            for b in msg.get("content") or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    self.last_text = b["text"].strip()
                    self.out(self.p.agent(b["text"]))
                elif b.get("type") == "tool_use":
                    cmd = (b.get("input") or {}).get("command", "")
                    self.out(self.p.cmd(cmd if cmd else json.dumps(b.get("input"))))
        elif t == "user":
            blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
            for b in blocks:
                if b.get("type") == "tool_result":
                    txt = result_text(b)
                    self._remember(txt)
                    line = brief(txt)
                    self.out("  " + (self.p.alert(line) if "needs_approval" in line else self.p.out(line)))
        elif t == "result":
            self.final = ev.get("result")
            self.cost, self.turns, self.ms = ev.get("total_cost_usd"), ev.get("num_turns"), ev.get("duration_ms")

    def _remember(self, txt):
        m = re.search(r'"approval"\s*:\s*\{\s*"id"\s*:\s*"(ap_[0-9a-f]+)"', txt)
        if m:
            self.approval = m.group(1)
        m = re.search(r'"drop"\s*:\s*"(d_[a-z0-9]+)"', txt)
        if m and (self.drop is None or re.search(r'"state"\s*:\s*"draft"', txt)):
            self.drop = m.group(1)  # the newest drop the agent created, else the first one it mentioned

    def summary(self):
        bits = []
        if self.turns is not None:
            bits.append(f"{self.turns} turns")
        if self.ms:
            bits.append(f"{self.ms / 1000:.0f}s")
        if self.cost is not None:
            bits.append(f"reported cost ${self.cost:.4f}")
        return "done: " + ", ".join(bits) if bits else "done"


def run_agent(model, prompt, env, painter, raw=False, claude="claude"):
    tmp = tempfile.mkdtemp(prefix="crowd-demo-")  # an empty working directory: the agent sees no repo files
    tr = Transcript(painter)
    try:
        proc = subprocess.Popen(build_command(model, prompt, claude), cwd=tmp, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if raw:
                print(line)
            try:
                tr.feed(json.loads(line))
            except ValueError:
                continue
        proc.wait()
        if proc.returncode:
            err = (proc.stderr.read() or "").strip()
            print(painter.alert(f"claude exited with {proc.returncode}: {err[-300:]}"), file=sys.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if tr.final and tr.final.strip() != tr.last_text:  # claude repeats the last message as the result: show it once
        print()
        print(painter.agent(tr.final))
    print(painter.out(tr.summary()))
    return tr


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="sonnet", help="claude model alias (default sonnet; haiku for cheap smoke runs)")
    ap.add_argument("--scenario", default="full", choices=["full", "open", "missed", "finish"])
    ap.add_argument("--fake", action="store_true", help="fake PayPal and a throwaway store (CROWD_ENV=test CROWD_PAYPAL=fake)")
    ap.add_argument("--drop", help="drop id (finish)")
    ap.add_argument("--approval", help="approval id the person granted (finish)")
    ap.add_argument("--no-reset", action="store_true", help="with --fake, keep the existing test store")
    ap.add_argument("--raw", action="store_true", help="also print claude's raw stream-json lines")
    a = ap.parse_args(argv)

    if not shutil.which("togi"):
        sys.exit("togi is not on PATH; install it and run `togi add <this repo>` first")
    if not shutil.which("claude"):
        sys.exit("claude is not on PATH")
    env, painter = agent_env(a.fake), Painter(sys.stdout.isatty())
    print(painter.alert("fake PayPal, throwaway store" if a.fake else "PayPal SANDBOX (test cards, no real money)"))
    if a.fake and not a.no_reset and a.scenario in ("full", "open", "missed"):
        # the reset is the host's setup, not the agent's: `test reset` refuses any session that carries TOGI_AGENT
        subprocess.run(["togi", "crowd", "test", "reset"], env=setup_env(env), capture_output=True)

    if a.scenario == "finish":
        if not (a.drop and a.approval):
            sys.exit("--scenario finish needs --drop and --approval")
        run_agent(a.model, SCENARIOS["finish"].format(drop=a.drop, approval=a.approval), env, painter, a.raw)
        return 0

    first = run_agent(a.model, SCENARIOS["missed" if a.scenario == "missed" else "open"], env, painter, a.raw)
    if a.scenario != "full":
        if first.approval:
            print(painter.alert(f"\nA person must approve in their own terminal:  togi approve {first.approval}"
                                f"   (then type the drop id{' ' + first.drop if first.drop else ''})"))
        return 0
    if not (first.approval and first.drop):
        print(painter.alert("\nThe agent did not reach the approval step; nothing to finish."))
        return 1
    print(painter.alert(f"\nWaiting for you. In another terminal run:  togi approve {first.approval}\n"
                        f"and type the drop id ({first.drop}) when asked. Then press Enter here."))
    try:
        input()
    except EOFError:
        time.sleep(1)
    run_agent(a.model, SCENARIOS["finish"].format(drop=first.drop, approval=first.approval), env, painter, a.raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
