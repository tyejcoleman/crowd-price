"""Buyer tokens: how a web buyer proves a join is theirs, to see it or to leave.

The engine knows a buyer by name. The web adds a secret per join, kept only as a hash in a small database beside the drop
store (`<store>.web.db`). A buyer with no token (one the assistant or a test added) cannot be seen or released from the web.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import sqlite3
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS buyer_tokens (
  drop_id TEXT NOT NULL, buyer TEXT NOT NULL, token_sha256 TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY (drop_id, buyer))"""


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Tokens:
    def __init__(self, path: str):
        self.path = path
        with self._db() as c:
            c.execute(SCHEMA)

    @contextlib.contextmanager
    def _db(self):
        c = sqlite3.connect(self.path, timeout=10)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def issue(self, drop: str, buyer: str) -> str:
        """A new token for this buyer of this drop. It replaces any earlier one (the same name joining again)."""
        token = secrets.token_urlsafe(24)
        with self._db() as c:
            c.execute("INSERT OR REPLACE INTO buyer_tokens VALUES (?, ?, ?, ?)", (drop, buyer, _sha(token), time.time()))
        return token

    def check(self, drop: str, buyer: str, token) -> bool:
        if not isinstance(token, str) or not token or not isinstance(buyer, str):
            return False
        with self._db() as c:
            row = c.execute("SELECT token_sha256 FROM buyer_tokens WHERE drop_id = ? AND buyer = ?", (drop, buyer)).fetchone()
        return bool(row) and hmac.compare_digest(row[0], _sha(token))
