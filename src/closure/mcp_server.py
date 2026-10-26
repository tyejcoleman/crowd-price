"""MCP server: certified money answers over a PayPal sandbox account.

Run:  python3 -m closure.mcp_server      (stdio; reads .env, sandbox only)
"""
import json

from mcp.server.fastmcp import FastMCP

from . import service
from .sources import Live

INSTRUCTIONS = """You answer money questions about one PayPal account, and you never state a number yourself.
1. Call describe_activity to see which kinds of records exist (no amounts).
2. Write a program: name parts that together split the WHOLE balance change (end with a rest part),
   and set answer to the part(s) the question asks about. Call ask_money with it.
3. If it returns a certificate, quote answer.value exactly with the certificate id. If it returns a
   refusal, say you cannot give a certified answer and why. Never estimate or fill in a number."""

mcp = FastMCP("certified-money", instructions=INSTRUCTIONS)
_source = None


def source():
    global _source
    if _source is None:
        _source = Live()
    return _source


@mcp.tool()
def describe_activity(currency: str, start: str, end: str) -> str:
    """Kinds of balance-affecting records in [start, end): PayPal event code, family, status, sign
    (in/out) and count. No amounts. Times are ISO UTC, e.g. 2026-10-01T00:00:00Z."""
    return json.dumps(service.describe(source(), currency, start, end))


@mcp.tool()
def ask_money(program: dict) -> str:
    """Run a program and get a certificate or a refusal. Program:
    {"question": str, "currency": "USD", "start": ISO, "end": ISO, "answer": part name or list,
     "parts": [{"name": str, "take": "gross"|"fee"|"net", "where": {code?, code_prefix?, status?,
                sign? ("in"|"out"), id?, note_contains?}}, ..., {"name": "everything else", "take": "rest"}]}
    Each transaction's gross and fee must land in exactly one part. Lists are lists of strings."""
    return json.dumps(service.ask(source(), program))


@mcp.tool()
def verify(certificate_id: str) -> str:
    """Recheck a certificate's arithmetic and re-run it against PayPal now."""
    return json.dumps(service.verify(source(), certificate_id))


if __name__ == "__main__":
    mcp.run()
