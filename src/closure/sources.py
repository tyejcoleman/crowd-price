"""Where records and balances come from: live PayPal sandbox, or a recorded fixture for tests."""
import json
from datetime import timedelta
from decimal import Decimal

from . import ledger
from .paypal import Client, parse_time


class Live:
    def __init__(self, client=None):
        self.client = client or Client()

    def transactions(self, start, end):
        return self.client.transactions(start, end)

    def balance(self, at, currency):
        return self.client.balance(at, currency)


class Fixture:
    """Recorded Transaction Search output. Balances are read off PayPal's own ending_balance
    (the last record at or before the moment), so tests exercise the same chain the live API does."""

    def __init__(self, path_or_dict):
        d = path_or_dict if isinstance(path_or_dict, dict) else json.load(open(path_or_dict))
        self.records = d["transaction_details"]
        self.last_refreshed = parse_time(d["last_refreshed_datetime"])

    def transactions(self, start, end):
        out = [r for r in self.records
               if start <= parse_time(r["transaction_info"]["transaction_initiation_date"]) < end]
        return out, self.last_refreshed, len(out)

    def balance(self, at, currency):
        events = [e for e in ledger.from_paypal(self.records) if e.currency == currency]
        bal = None
        for e in events:
            if parse_time(e.at) <= at:
                bal = e.ending_balance
        if bal is None:
            bal = (events[0].ending_balance - events[0].net) if events else Decimal("0")
        return str(bal)


def like_stock_toolkit(source, start, end, page_size=100):
    """What PayPal's agent-toolkit list_transactions hands an agent (read from its code, 2026-10-25):
    a range over 31 days is silently reset to the last 31 days, and only the first page comes back.
    Returns (records, notes) where notes say what was lost, so the demo can show it."""
    notes = []
    if end - start > timedelta(days=31):
        notes.append(f"range cut to 31 days: start moved from {start:%Y-%m-%d} to {(end - timedelta(days=31)):%Y-%m-%d}")
        start = end - timedelta(days=31)
    records = source.transactions(start, end)[0]
    if len(records) > page_size:
        notes.append(f"only page 1 returned: {page_size} of {len(records)} records")
        records = records[:page_size]
    return records, notes
