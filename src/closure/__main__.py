"""CLI.  PYTHONPATH=src python3 -m closure <command>

  describe CUR START END        kinds of records (no amounts)
  ask PROGRAM.json              certificate or refusal
  verify CERT_ID                recheck + replay against PayPal now
  baseline CUR START END CODE   what the stock toolkit's data gives for one event code (no checks)
"""
import json, sys
from decimal import Decimal

from . import ledger, service
from .paypal import parse_time
from .sources import Live, like_stock_toolkit


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    src = Live()
    if cmd == "describe":
        out = service.describe(src, *args)
    elif cmd == "ask":
        out = service.ask(src, json.load(open(args[0])))
    elif cmd == "verify":
        out = service.verify(src, args[0])
    elif cmd == "baseline":
        cur, start, end, code = args
        records, notes = like_stock_toolkit(src, parse_time(start), parse_time(end))
        ev = [e for e in ledger.from_paypal(records) if e.currency == cur and e.code == code]
        out = {"stock_toolkit_total": str(sum((e.gross for e in ev), Decimal("0"))), "records_seen": len(records),
               "lost": notes or ["nothing lost in this range"], "checked": False}
    else:
        print(__doc__)
        return 2
    print(json.dumps(out, indent=1))
    return 0 if out.get("kind") != "refusal" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
