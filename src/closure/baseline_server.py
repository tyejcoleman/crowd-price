"""Baseline MCP server: a replica of PayPal agent-toolkit's list_transactions, for the scorecard.

Behaviour copied from the toolkit's source (typescript/src/shared/functions.ts, read 2026-10-25):
neither date -> last 31 days; one date -> 31 days from it; both dates over 31 days -> start reset to
end - 31 days, silently; one request, so one page; other params passed through unchanged. This is a
replica, not PayPal's package (installing that is a separate decision): the scorecard says so.

Run:  python3 -m closure.baseline_server
"""
import json
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from .paypal import Client, iso, parse_time

mcp = FastMCP("paypal-transactions")
_client = None
PASS_THROUGH = {"transaction_id", "transaction_type", "transaction_status", "transaction_amount",
                "transaction_currency", "payment_instrument_type", "store_id", "terminal_id", "fields",
                "balance_affecting_records_only", "page_size", "page"}


def client():
    global _client
    if _client is None:
        _client = Client()
    return _client


def list_transactions_like_toolkit(params):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    month = timedelta(days=31)
    start = parse_time(params.pop("start_date")) if params.get("start_date") else None
    end = parse_time(params.pop("end_date")) if params.get("end_date") else None
    if start is None and end is None:
        end = datetime.now(timezone.utc)
        start = end - month
    elif end is None:
        end = start + month
    elif start is None:
        start = end - month
    elif end - start > month:
        start = end - month
    q = {k: v for k, v in params.items() if k in PASS_THROUGH}
    q.update(start_date=iso(start), end_date=iso(end))
    return client()._call("GET", "/v1/reporting/transactions", params=q)


@mcp.tool()
def list_transactions(start_date: str = "", end_date: str = "", transaction_status: str = "",
                      transaction_type: str = "", transaction_currency: str = "", fields: str = "",
                      page_size: int = 0, page: int = 0) -> str:
    """List transactions from PayPal Transaction Search. Dates are ISO 8601 (e.g. 2026-10-01T00:00:00Z).
    transaction_status: S success, P pending, V reversed, D denied. fields: e.g. transaction_info or all."""
    out = list_transactions_like_toolkit({
        "start_date": start_date, "end_date": end_date, "transaction_status": transaction_status,
        "transaction_type": transaction_type, "transaction_currency": transaction_currency, "fields": fields,
        "page_size": page_size or None, "page": page or None})
    return json.dumps(out)


if __name__ == "__main__":
    mcp.run()
