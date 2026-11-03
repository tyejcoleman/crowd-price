"""Buyer secrets: how a web buyer proves a join is theirs, to see it or to leave.

The browser makes a random secret BEFORE it asks to join and sends it with the join; only its hash is kept, beside the
drop store (`<store>.web.db`). The hash is written down (a "claim", not yet bound to anything) before PayPal is asked, so a
join whose reply never reached the browser can be asked again with the same secret and is handed back, never placed twice.

A secret proves one hold, not a name: once the engine has recorded the hold, the claim is BOUND to that hold's authorisation
and to the drop's creation time. A buyer of the same name joining later (the assistant, a test, someone else), or a drop id
reused after the store was reset, has a different hold or a different creation time, so an old secret proves nothing about it.

A claim not yet bound proves nothing at all, however it came to be: a join that PayPal may have acted on, or a process that died
between the engine's hold and the binding. Nothing here guesses a binding. The engine's log does not say which request placed
a hold, so a hold found beside an unbound claim is left for the maker to see, and the buyer is told so (api.py `join_unconfirmed`).

Three rules keep one join intent from becoming two holds and keep a hold from being released by someone who did not place it:
a claim never replaces the row bound to a hold that is still there; a refused join forgets only its OWN unbound claim; a
binding, once made, is final.

A buyer with no secret (one the assistant or a test added) can never be seen or released from the web.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import re
import sqlite3

SECRET = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

SCHEMA = """CREATE TABLE IF NOT EXISTS buyer_secrets (
  drop_id TEXT NOT NULL, buyer TEXT NOT NULL, secret_sha256 TEXT NOT NULL, drop_created TEXT NOT NULL,
  auth_id TEXT, created TEXT NOT NULL, PRIMARY KEY (drop_id, buyer))"""


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def well_formed(secret) -> bool:
    return isinstance(secret, str) and bool(SECRET.match(secret))


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

    def row(self, drop: str, buyer: str) -> dict | None:
        with self._db() as c:
            r = c.execute("SELECT secret_sha256, drop_created, auth_id, created FROM buyer_secrets WHERE drop_id = ? AND buyer = ?",
                          (drop, buyer)).fetchone()
        return dict(zip(("secret_sha256", "drop_created", "auth_id", "created"), r)) if r else None

    def claim(self, drop: str, buyer: str, secret: str, drop_created: str, at: str, hold_auth_id: str | None = None) -> bool:
        """Write down, before PayPal is asked, that this secret is asking to join as this buyer. Not yet bound to a hold.

        `hold_auth_id` is the hold this buyer still has in the engine, if any. A row bound to it is the proof of that hold, so
        the claim is refused (False, nothing written) rather than replace it. A row bound to a hold that is gone (the buyer left)
        or to an earlier life of the drop is replaced."""
        with self._db() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT drop_created, auth_id FROM buyer_secrets WHERE drop_id = ? AND buyer = ?", (drop, buyer)).fetchone()
            if r and r[1] and hold_auth_id and r[0] == drop_created and r[1] == hold_auth_id:
                return False
            c.execute("INSERT OR REPLACE INTO buyer_secrets VALUES (?, ?, ?, ?, NULL, ?)",
                      (drop, buyer, digest(secret), drop_created, at))
        return True

    def claimed(self, drop: str, buyer: str, secret, drop_created: str | None) -> bool:
        """Does this secret have a claim on this buyer, in this life of the drop, that no hold is bound to yet?"""
        if not well_formed(secret) or not drop_created:
            return False
        r = self.row(drop, buyer)
        return bool(r) and r["auth_id"] is None and r["drop_created"] == drop_created \
            and hmac.compare_digest(r["secret_sha256"], digest(secret))

    def bind(self, drop: str, buyer: str, secret: str, auth_id: str) -> bool:
        """Bind the claim to the hold the engine placed. False if the claim is no longer this secret's, or is already bound."""
        with self._db() as c:
            n = c.execute("UPDATE buyer_secrets SET auth_id = ? WHERE drop_id = ? AND buyer = ? AND secret_sha256 = ? "
                          "AND auth_id IS NULL", (auth_id, drop, buyer, digest(secret))).rowcount
        return n == 1

    def drop_claim(self, drop: str, buyer: str, secret: str) -> None:
        """Forget this secret's own unbound claim after a join that was refused outright (nothing was held), so it can never
        bind to a later hold. A row bound to a hold, or any other secret's row, is never touched."""
        with self._db() as c:
            c.execute("DELETE FROM buyer_secrets WHERE drop_id = ? AND buyer = ? AND secret_sha256 = ? AND auth_id IS NULL",
                      (drop, buyer, digest(secret)))

    def owns(self, drop: str, buyer: str, secret, drop_created: str | None, auth_id: str | None) -> bool:
        """Does this secret prove this buyer's CURRENT hold in THIS life of the drop?"""
        if not well_formed(secret) or not isinstance(buyer, str) or not auth_id or not drop_created:
            return False
        r = self.row(drop, buyer)
        return bool(r) and hmac.compare_digest(r["secret_sha256"], digest(secret)) and r["drop_created"] == drop_created \
            and r["auth_id"] == auth_id
