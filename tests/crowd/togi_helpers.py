"""Run the real `togi` host against this repo, in an isolated $TOGI_HOME, for end-to-end tests."""
import json
import os
import pty
import pwd
import select
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TOGI_SRC = Path(os.environ.get("TOGI_SRC") or Path.home() / "Development" / "togi" / "src")


def togi_available() -> bool:
    return (TOGI_SRC / "togi" / "cli.py").is_file()


# The person's own approver lives here, whatever $HOME or $TOGI_HOME say (Togi: <pw_dir>/.togi). A test must never create,
# change or read it: every approver a test needs is a throwaway one under TOGI_APPROVER_HOME (whose approvals are scope "test").
REAL_APPROVER_FILES = ("approver", "approver.key", "approver.state")


def real_approver_dir() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".togi"


def approver_home_for(home: Path) -> Path:
    """The throwaway approver home that goes with a throwaway TOGI_HOME: a sibling directory, so it is created fresh with
    the test's tmp path and removed with it. Never the person's own."""
    home = Path(home)
    out = home.with_name(home.name + "-approver")
    assert out.resolve() != real_approver_dir().resolve(), "a test approver home must not be the person's own"
    return out


def real_approver_snapshot(directory: Path | None = None) -> dict:
    """{file: (mtime_ns, size)} of the person's approver files that exist right now. Read-only: it only looks."""
    directory = Path(directory) if directory is not None else real_approver_dir()
    out = {}
    for name in REAL_APPROVER_FILES:
        try:
            st = (directory / name).stat()
        except OSError:
            continue
        out[name] = (st.st_mtime_ns, st.st_size)
    return out


def approver_files_touched(before: dict, after: dict) -> list[str]:
    """Names in the person's approver directory that appeared, vanished or changed between two snapshots."""
    return sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))


def env_for(home: Path, db: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TOGI_", "CROWD_"))}
    env.update(TOGI_HOME=str(home), TOGI_APPROVER_HOME=str(approver_home_for(home)), PYTHONPATH=str(TOGI_SRC),
               PYTHONDONTWRITEBYTECODE="1", CROWD_DB=str(db), CROWD_ENV="test", CROWD_PAYPAL="fake")
    env.update({k: v for k, v in extra.items() if v is not None})
    for k in [k for k, v in extra.items() if v is None and k != "TOGI_APPROVER_HOME"]:
        env.pop(k, None)
    assert env.get("TOGI_APPROVER_HOME") and Path(env["TOGI_APPROVER_HOME"]).resolve() != real_approver_dir().resolve()
    return env


def togi(env: dict, *argv, input=None, timeout=120):
    """(exit code, parsed JSON or raw text) like an agent's non-interactive shell sees it."""
    r = subprocess.run([sys.executable, "-m", "togi", *argv], input=input, capture_output=True, text=True, env=env,
                       timeout=timeout, cwd=str(REPO.parent))
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        return r.returncode, r.stdout + r.stderr


APPROVER_PASSPHRASE = "correct horse battery staple"     # the approver passphrase of every throwaway test home


def set_up_approver(home: Path) -> None:
    """What a person does once with `togi approver setup`, minus the terminal: write a THROWAWAY approver record (under
    TOGI_APPROVER_HOME, so its approvals are scope "test") with a cheap scrypt cost, which the record stores. Without it
    Togi refuses every approval. It never goes near the person's own approver in <pw_dir>/.togi."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOGI_")}
    env.update(TOGI_HOME=str(home), TOGI_APPROVER_HOME=str(approver_home_for(home)), PYTHONPATH=str(TOGI_SRC),
               PYTHONDONTWRITEBYTECODE="1")
    assert env["TOGI_APPROVER_HOME"] and Path(env["TOGI_APPROVER_HOME"]).resolve() != real_approver_dir().resolve()
    r = subprocess.run([sys.executable, "-c", f"from togi import approver; approver.create({APPROVER_PASSPHRASE!r}, n=2 ** 10)"],
                       env=env, capture_output=True, text=True, cwd=str(REPO.parent))
    assert r.returncode == 0, r.stderr


def person_in_a_terminal(env: dict, argv, typed, passphrase: str = APPROVER_PASSPHRASE, timeout=60):
    """What a person does: run togi on a real pseudo-terminal that is the child's controlling terminal (so /dev/tty
    works), type the confirmation word(s) in `typed` when asked for them, then the approver passphrase."""
    answers = []
    for word in typed:
        answers += [('Type "', word), ("assphrase", passphrase)]
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(str(REPO.parent))
            os.execve(sys.executable, [sys.executable, "-m", "togi", *argv], env)
        finally:
            os._exit(127)
    out, consumed, deadline, status = b"", 0, time.time() + timeout, None
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if answers:
            expect, line = answers[0]
            text = out.decode("utf-8", "replace")
            if expect in text[consumed:]:
                consumed = text.index(expect, consumed) + len(expect)
                time.sleep(0.15)               # let the prompt finish switching echo off before typing
                os.write(fd, (line + "\n").encode())
                answers.pop(0)
        if not r:
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
    try:
        while True:                           # drain what is left
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            out += chunk
    except OSError:
        pass
    if status is None:
        try:
            _, status = os.waitpid(pid, 0)
        except ChildProcessError:
            status = 0
    os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")
