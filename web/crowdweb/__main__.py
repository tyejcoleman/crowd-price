"""python3 -m crowdweb [--host 127.0.0.1] [--port 8780] [--maker-name NAME] [--maker-key KEY]

Run it with the same environment as the assistant (CROWD_ENV, CROWD_PAYPAL, TOGI_HOME) so both see the same drops.
PYTHONPATH must include the repo's src/ and web/ (the README's command does both).
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

from .api import Api
from .server import serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="crowdweb", description="Crowd Price: the buyer's drop page and the maker's view.")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this machine only)")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--maker-name", default=os.environ.get("CROWD_MAKER_NAME"), help="shown on the buyer page, e.g. Juniper")
    ap.add_argument("--maker-key", default=os.environ.get("CROWD_MAKER_KEY"),
                    help="the maker view's key (default: a new random one, printed at start)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    a = ap.parse_args(argv)
    key = a.maker_key or secrets.token_urlsafe(18)
    try:
        api = Api(maker_name=a.maker_name)
    except Exception as e:  # noqa: BLE001 - say what is missing and stop
        print(f"crowdweb: cannot start: {e}", file=sys.stderr)
        return 2
    httpd = serve(api, key, a.host, a.port, quiet=not a.verbose)
    base = f"http://{'localhost' if a.host in ('127.0.0.1', '0.0.0.0') else a.host}:{a.port}"
    mode = os.environ.get("CROWD_PAYPAL", "sandbox")
    print(f"Crowd Price on {base}  (PayPal: {mode}; store: {api.engine.store.path})")
    for d in api.drops():
        print(f"  {d['state']:<9} {base}/d/{d['drop']}   {d['title']}")
    print(f"  maker view: {base}/maker#key={key}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
