"""Crowd Price Studio: start it with the launcher, in your own terminal:  python3 -I web/studio.py [--maker-name Juniper]

The same server as `python3 -m crowdweb` (the buyer's drop page, the maker view), plus the studio page: a chat with your
assistant beside a live panel of the drop it is working on, and approving the charge in that panel with your approver
passphrase. Run it with the same environment as the assistant's drops (CROWD_ENV, CROWD_PAYPAL, TOGI_HOME).

Approving in the panel is Togi's approval desk (togi.desk). It opens here only when a person started this server: not
under TOGI_AGENT or `togi run`, in an isolated interpreter (the launcher's `python3 -I`; the passphrase arrives in this
process), with an approver set up, and, for your own approver, from a terminal. Otherwise the studio still runs and the
panel says how to approve in your terminal instead. It listens on this machine only.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import signal
import sys

from .api import Api, load_details
from .server import serve
from .studio import Studio

DESK_COMMANDS = [("crowd", "drop settle")]
TOGI_SRC = None      # set by the launcher (web/studio.py): the Togi source it put on sys.path, the one the `togi` launcher runs
TOGI_WHY = None      # or why it put none there


def open_desk():
    """(desk, None) or (None, why). Togi decides; this only asks, and only of the Togi the launcher chose."""
    if TOGI_WHY:
        return None, TOGI_WHY
    try:
        from togi import desk
    except ImportError:
        return None, {"code": "no_togi", "message": "Togi is not importable here (start the studio with python3 -I web/studio.py)"}
    if TOGI_SRC and not os.path.realpath(desk.__file__).startswith(os.path.join(TOGI_SRC, "")):
        return None, {"code": "togi_src_mismatch", "message": "the Togi imported here is not the one your togi launcher runs"}
    return desk.open_or_reason("crowd studio", DESK_COMMANDS)


def main(argv=None) -> int:
    # allow_abbrev=False: the launcher (web/studio.py) reads --togi exactly as written to choose the desk's Togi; argparse used to
    # read --tog, --t and --to= as --togi too, which could give the assistant a different Togi from the desk's
    ap = argparse.ArgumentParser(prog="studio", description="Crowd Price Studio: run a drop from a chat with your assistant.",
                                 allow_abbrev=False)
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--maker-name", default=os.environ.get("CROWD_MAKER_NAME"), help="shown on the buyer page, e.g. Juniper")
    ap.add_argument("--maker-key", default=os.environ.get("CROWD_MAKER_KEY"),
                    help="the studio's key, 16+ characters (better: set CROWD_MAKER_KEY; default: a new random one)")
    ap.add_argument("--details", default=os.environ.get("CROWD_DETAILS"), help="product details per drop title (JSON)")
    ap.add_argument("--model", default=os.environ.get("CROWD_STUDIO_MODEL", "sonnet"), help="claude model alias (default sonnet)")
    ap.add_argument("--claude", default=shutil.which("claude"), help="the claude executable (default: on PATH)")
    ap.add_argument("--togi", default=shutil.which("togi"), help="the togi launcher the assistant uses (default: on PATH)")
    ap.add_argument("--verbose", action="store_true", help="log every request (request lines only, never a body)")
    a = ap.parse_args(argv)
    key = a.maker_key or secrets.token_urlsafe(18)
    try:
        api = Api(maker_name=a.maker_name, details=load_details(a.details))
    except Exception as e:  # noqa: BLE001
        print(f"studio: cannot start: {e}", file=sys.stderr)
        return 2
    desk, why = open_desk()          # before anything else runs in this process, and before any assistant exists
    studio = Studio(api, model=a.model, claude=a.claude, togi=a.togi, desk=desk, desk_why=why)
    api.studio = studio
    try:
        httpd = serve(api, key, "127.0.0.1", a.port, quiet=not a.verbose)
    except (ValueError, OSError) as e:
        print(f"studio: {e}", file=sys.stderr)
        studio.close()
        return 2
    base = f"http://localhost:{httpd.server_address[1]}"
    mode = "Test mode: no money moves" if api.mode == "fake" else "PayPal sandbox: test money only"
    print(f"Crowd Price Studio on {base}  ({mode}; store: {api.engine.store.path})")
    print(f"  studio:      {base}/studio#key={key}")
    print(f"  maker view:  {base}/maker#key={key}")
    if desk is not None:
        scope = " (a TEST approver: Crowd honours it only in test mode)" if desk.scope == "test" else ""
        print(f"  approving in the panel: on{scope}")
    else:
        print(f"  approving in the panel: off ({why['message']}); approve in your terminal with togi approve")
    if not studio.claude:
        print("  the assistant: claude is not on PATH")
    sys.stdout.flush()

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)       # stopped from outside: the assistant goes with the server
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        studio.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
