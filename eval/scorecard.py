#!/usr/bin/env python3
"""Scorecard (PLAN.md): the same questions to the certified system and to the baseline, scored
against an answer key that never comes from Transaction Search.

  answer key   data/seeder-log.jsonl: the amounts WE sent and PayPal's capture responses
  certified    agent + describe_activity / ask_money / verify (src/closure/mcp_server.py)
  baseline     same agent + a replica of PayPal agent-toolkit list_transactions
               (src/closure/baseline_server.py)

Agents:
  reference    hand-written programs from eval/reference_programs.json. NOT an AI run: it tests
               the certifier path end to end. Certified system only.
  claude       the Claude Code CLI in print mode, tools limited to that system's MCP server.

Usage:
  python3 eval/scorecard.py --set eval/dev-questions.jsonl --system certified --agent reference
  python3 eval/scorecard.py --set eval/questions.jsonl --system baseline --agent claude --model sonnet --runs 3
  python3 eval/scorecard.py --lock      record the hash of eval/questions.jsonl (do this once, before final runs)

The locked set refuses to run if eval/questions.jsonl no longer matches eval/questions.lock.
Every run is written to eval/runs/ and is meant to be published, misses included.
"""
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from closure import service  # noqa: E402
from closure.paypal import parse_time  # noqa: E402
from closure.sources import Live  # noqa: E402

LOCKED = os.path.join(ROOT, "eval", "questions.jsonl")
LOCK = os.path.join(ROOT, "eval", "questions.lock")
SEED_LOG = os.path.join(ROOT, "data", "seeder-log.jsonl")
RUNS = os.path.join(ROOT, "eval", "runs")
SIGN = {"amount": 1, "paypal_fee": -1}  # sign each kind carries in PayPal's records
OUT_KINDS = {"refund", "payout"}

CERTIFIED_PROMPT = """You answer money questions about one PayPal account using only your tools, and you never state a number yourself.
1. Call describe_activity for the question's currency and window to see which kinds of records exist (no amounts).
2. Write a program for ask_money: parts that together split the whole balance change (end with a part
   {"name": "everything else", "take": "rest"}), and set answer to the part the question asks about. Use the
   question's window exactly, as whole-second UTC times.
3. If ask_money returns a refusal you can fix (left over, counted twice, invalid program), fix the program and retry.
   If the question cannot be answered from PayPal records, do not call ask_money again.
Finish with exactly one last line, nothing after it:
RESULT: certificate <certificate_id>
or
RESULT: refused <short reason>"""

BASELINE_PROMPT = """You answer money questions about one PayPal account using only your tools.
Use list_transactions to fetch the records you need and work out the answer.
Finish with exactly one last line, nothing after it:
RESULT: <amount as a plain number, e.g. 1234.56>
or
RESULT: refused <short reason>"""


def sha(path):
    return "sha256:" + hashlib.sha256(open(path, "rb").read()).hexdigest()


def load_questions(path):
    return [json.loads(l) for l in open(path) if l.strip()]


def answer_key(q, log_path=SEED_LOG):
    """Expected absolute amount (Decimal) or None for questions that should be refused."""
    k = q["key"]
    if k["kind"] == "refuse":
        return None
    if k["kind"] == "zero":
        return Decimal("0.00")
    start, end = parse_time(q["start"]), parse_time(q["end"])
    total = Decimal("0")
    for line in open(log_path):
        e = json.loads(line)
        if not e.get("ok") or e["kind"] != k["kind"] or e["currency"] != q["currency"]:
            continue
        if start <= parse_time(e["at"]) < end and e.get(k["field"]) is not None:
            total += Decimal(e[k["field"]])
    return total.quantize(Decimal("0.01"))


# ---------- agents ----------

def run_reference(q, programs):
    p = programs.get(q["id"])
    if p is None or p == "refuse":
        return {"status": "refused", "reason": "no program: not answerable from PayPal records", "text": ""}
    out = service.ask(Live(), p)
    if out["kind"] == "refusal":
        return {"status": "refused", "reason": out["reason"], "text": json.dumps(out["detail"])[:500]}
    return {"status": "answered", "value": out["answer"]["value"], "certificate_id": out["certificate_id"], "text": ""}


def mcp_config(system, tmp):
    module = "closure.mcp_server" if system == "certified" else "closure.baseline_server"
    name = "certified-money" if system == "certified" else "paypal-transactions"
    cfg = {"mcpServers": {name: {"command": sys.executable, "args": ["-m", module], "cwd": ROOT,
                                 "env": {"PYTHONPATH": os.path.join(ROOT, "src")}}}}
    path = os.path.join(tmp, f"mcp-{system}.json")
    json.dump(cfg, open(path, "w"))
    return path, name


RESULT = re.compile(r"^RESULT:\s*(.+)$", re.M)
MONEY_IN_TEXT = re.compile(r"-?\d[\d,]*\.\d{2}\b")


def run_claude(q, system, model, timeout=600):
    with tempfile.TemporaryDirectory() as tmp:  # empty cwd: no project CLAUDE.md
        cfg, name = mcp_config(system, tmp)
        prompt = (f"Question: {q['question']}\nCurrency: {q['currency']}\n"
                  f"Window (UTC, start inclusive, end exclusive): {q['start']} to {q['end']}")
        cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json",
               "--system-prompt", CERTIFIED_PROMPT if system == "certified" else BASELINE_PROMPT,
               "--tools", "", "--strict-mcp-config", "--mcp-config", cfg,
               "--allowedTools", f"mcp__{name}", "--no-session-persistence"]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"status": "error", "reason": "timeout", "text": ""}
        took = round(time.time() - t0, 1)
    try:
        out = json.loads(p.stdout)
        text = out.get("result") or ""
    except json.JSONDecodeError:
        return {"status": "error", "reason": f"cli exit {p.returncode}", "text": (p.stderr or p.stdout)[-500:]}
    m = RESULT.findall(text)
    base = {"text": text[-1500:], "seconds": took, "turns": out.get("num_turns"), "cost_usd_reported": out.get("total_cost_usd")}
    if not m:
        return dict(base, status="answered_no_result_line", value=None)
    last = m[-1].strip()
    if last.lower().startswith("refused"):
        return dict(base, status="refused", reason=last[7:].strip())
    if system == "certified":
        cid = last.split()[-1] if last.lower().startswith("certificate") else ""
        try:
            cert = service.load(cid)
        except (OSError, ValueError):
            return dict(base, status="answered", value=None, reason=f"certificate id not found: {cid!r}")
        value = cert["answer"]["value"]
        # Any figure the agent writes must be on its certificate (the answer or a part); else it made one up.
        certified = {abs(Decimal(value))} | {abs(Decimal(x["value"])) for x in cert["parts"]}
        stated = {abs(Decimal(x.replace(",", ""))) for x in MONEY_IN_TEXT.findall(text)}
        misquoted = bool(stated - certified)
        return dict(base, status="answered", value=value, certificate_id=cid, misquoted=misquoted)
    try:
        return dict(base, status="answered", value=str(Decimal(last.replace(",", "").replace("$", "").split()[0])))
    except (InvalidOperation, IndexError):
        return dict(base, status="answered_unparseable", value=None, reason=last[:200])


# ---------- scoring ----------

def score(q, res, key):
    if res["status"] == "error":
        return "error"
    answered = res["status"].startswith("answered")
    if key is None:
        return "wrong" if answered else "correct_refusal"
    if not answered:
        return "refused"
    try:
        ok = res.get("value") is not None and abs(Decimal(res["value"])) == abs(key)
    except InvalidOperation:
        ok = False
    return "exact" if ok else "wrong"


def summarize(rows, meta):
    from collections import Counter
    c = Counter(r["score"] for r in rows)
    n = len(rows)
    ans = [r for r in rows if r["expected"] is not None]
    lines = [f"# Scorecard run {meta['run_id']}", "",
             f"- system: {meta['system']} · agent: {meta['agent']} · model: {meta.get('model') or 'n/a'} · runs: {meta['runs']}",
             f"- questions: {meta['set']} ({meta['set_hash']}){' LOCKED' if meta['locked'] else ' (dev, not locked)'}",
             f"- answer key: seeder log ({meta['key_hash']}), never Transaction Search", "",
             f"- exact to the cent: {c['exact']} of {len(ans)} answerable",
             f"- confident wrong answers: {c['wrong']} of {n}",
             f"- refused an answerable question: {c['refused']}",
             f"- correctly refused an unanswerable question: {c['correct_refusal']} of {n - len(ans)}",
             f"- errors: {c['error']}"]
    mis = sum(1 for r in rows if r.get("misquoted"))
    if meta["system"] == "certified" and meta["agent"] != "reference":
        lines.append(f"- agent wrote a figure that is not on its certificate: {mis}")
    lines += ["", "## Every answer", ""]
    for r in rows:
        lines.append(f"- {r['id']} run {r['run']}: **{r['score']}** — expected {r['expected'] if r['expected'] is not None else 'refusal'}, "
                     f"got {r.get('value') if r.get('value') is not None else r['status']}"
                     + (f" ({r['reason']})" if r.get("reason") else "")
                     + (f" · certificate {r['certificate_id']}" if r.get("certificate_id") else ""))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=LOCKED)
    ap.add_argument("--system", choices=["certified", "baseline"], default="certified")
    ap.add_argument("--agent", choices=["reference", "claude"], default="reference")
    ap.add_argument("--model", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--lock", action="store_true")
    a = ap.parse_args()

    if a.lock:
        if os.path.exists(LOCK):
            sys.exit(f"already locked: {open(LOCK).read().strip()} (delete the lock file only with Tye's OK)")
        with open(LOCK, "w") as f:
            f.write(f"{sha(LOCKED)} locked {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n")
        print(open(LOCK).read().strip())
        return
    locked = os.path.abspath(a.set) == LOCKED
    if locked:
        if not os.path.exists(LOCK):
            sys.exit("eval/questions.jsonl is not locked yet: run --lock first")
        if open(LOCK).read().split()[0] != sha(LOCKED):
            sys.exit("eval/questions.jsonl changed since it was locked: refusing to run")
    if a.agent == "reference" and a.system != "certified":
        sys.exit("the reference agent only exists for the certified system")
    if a.agent == "claude" and not a.model:
        sys.exit("--model is required for --agent claude")

    qs = load_questions(a.set)
    if a.only:
        keep = set(a.only.split(","))
        qs = [q for q in qs if q["id"] in keep]
    programs = json.load(open(os.path.join(ROOT, "eval", "reference_programs.json")))
    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y%m%dT%H%M%SZ}-{os.path.basename(a.set).split('.')[0]}-{a.system}-{a.agent}" + (f"-{a.model}" if a.model else "")
    meta = {"run_id": run_id, "system": a.system, "agent": a.agent, "model": a.model, "runs": a.runs,
            "set": os.path.relpath(a.set, ROOT), "set_hash": sha(a.set), "locked": locked,
            "key_hash": sha(SEED_LOG), "started": f"{now:%Y-%m-%dT%H:%M:%SZ}",
            "baseline_note": "baseline uses a replica of PayPal agent-toolkit list_transactions" if a.system == "baseline" else None}
    rows = []
    for run in range(1, a.runs + 1):
        for q in qs:
            key = answer_key(q)
            res = run_reference(q, programs) if a.agent == "reference" else run_claude(q, a.system, a.model)
            row = dict(id=q["id"], run=run, expected=None if key is None else str(key), **res)
            row["score"] = score(q, res, key)
            rows.append(row)
            print(f"{q['id']} run {run}: {row['score']:16} expected {row['expected']} got {row.get('value', row['status'])}", flush=True)
    os.makedirs(RUNS, exist_ok=True)
    with open(os.path.join(RUNS, run_id + ".jsonl"), "w") as f:
        f.write(json.dumps({"meta": meta}) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
    md = summarize(rows, meta)
    open(os.path.join(RUNS, run_id + ".md"), "w").write(md)
    print("\n" + md)


if __name__ == "__main__":
    main()
