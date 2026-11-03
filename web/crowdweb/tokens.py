"""Buyer secrets: how a web buyer proves a join is theirs, to see it or to leave.

The browser makes a random secret BEFORE it asks to join and sends it with the join; only its hash is kept, beside the
drop store (`<store>.web.db`). The hash is written down (a "claim", not yet bound to anything) before PayPal is asked, so a
join whose reply never reached the browser can be asked again with the same secret and is handed back, never placed twice.

A secret proves one hold, not a name: once the engine has recorded the hold, the claim is BOUND to that hold's authorisation
and to the drop's creation time. A buyer of the same name joining later (the assistant, a test, someone else), or a drop id
reused after the store was reset, has a different hold or a different creation time, so an old secret proves nothing about it.

A claim records the join attempt (the engine's attempt number for the buyer) it started, and the engine-clock moment it was written.
That is how a later retry tells the open join it began from an open join someone else began on the same name (the assistant, another
browser): only an open join of that attempt that the engine stamped within moments of the claim is the claim's own (api.py `_story`).
A claim whose attempt the engine has moved past (the join was released, failed, or its hold left) is over: it is kept, so a retry is
always told so, but it never starts a join again.

A secret whose claim was overwritten by another secret's, or whose attempt is over, is RETIRED (its own small table, because the claim
row is one per buyer and the next claim replaces it): it is never again allowed to start a join, however late its page asks, so a lost
answer can never turn into a hold nobody expects.

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
  auth_id TEXT, created TEXT NOT NULL, attempt INTEGER, PRIMARY KEY (drop_id, buyer))"""


RETIRED = """CREATE TABLE IF NOT EXISTS retired_secrets (
  drop_id TEXT NOT NULL, secret_sha256 TEXT NOT NULL, drop_created TEXT NOT NULL, PRIMARY KEY (drop_id, secret_sha256, drop_created))"""


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def well_formed(secret) -> bool:
    return isinstance(secret, str) and bool(SECRET.match(secret))


class Tokens:
    def __init__(self, path: str):
        self.path = path
        with self._db() as c:
            c.execute(SCHEMA)
            c.execute(RETIRED)
            if "attempt" not in [r[1] for r in c.execute("PRAGMA table_info(buyer_secrets)")]:      # a store from before claims knew their attempt
                c.execute("ALTER TABLE buyer_secrets ADD COLUMN attempt INTEGER")

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
            r = c.execute("SELECT secret_sha256, drop_created, auth_id, created, attempt FROM buyer_secrets "
                          "WHERE drop_id = ? AND buyer = ?", (drop, buyer)).fetchone()
        return dict(zip(("secret_sha256", "drop_created", "auth_id", "created", "attempt"), r)) if r else None

    def unbound(self, drop: str, drop_created: str | None) -> set[str]:
        """The buyers of this life of the drop that have a claim no hold is bound to (for the maker's view)."""
        with self._db() as c:
            rows = c.execute("SELECT buyer FROM buyer_secrets WHERE drop_id = ? AND drop_created = ? AND auth_id IS NULL",
                             (drop, drop_created)).fetchall()
        return {r[0] for r in rows}

    def unbound_claims(self, drop: str, drop_created: str | None) -> dict[str, dict]:
        """buyer -> claim (secret hash left out), for the buyers of this life of the drop that have a claim no hold is bound to."""
        with self._db() as c:
            rows = c.execute("SELECT buyer, created, attempt FROM buyer_secrets WHERE drop_id = ? AND drop_created = ? "
                             "AND auth_id IS NULL", (drop, drop_created)).fetchall()
        return {r[0]: {"created": r[1], "attempt": r[2]} for r in rows}

    def claim(self, drop: str, buyer: str, secret: str, drop_created: str, at, hold_auth_id: str | None = None,
              attempt: int | None = None) -> bool:
        """Write down, before PayPal is asked, that this secret is asking to join as this buyer. Not yet bound to a hold.
        `attempt` is the engine's attempt number this join will be (the buyer's attempts so far, plus one). `at` is the moment
        (or a function that gives it): a function is called once the write has its turn, so a wait for the database does not
        make the claim older than the join that follows it.

        `hold_auth_id` is the hold this buyer still has in the engine, if any. A row bound to it is the proof of that hold, so
        the claim is refused (False, nothing written) rather than replace it. A row bound to a hold that is gone (the buyer left)
        or to an earlier life of the drop is replaced."""
        with self._db() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT drop_created, auth_id, secret_sha256 FROM buyer_secrets WHERE drop_id = ? AND buyer = ?",
                          (drop, buyer)).fetchone()
            if r and r[1] and hold_auth_id and r[0] == drop_created and r[1] == hold_auth_id:
                return False
            if r and r[1] is None and hold_auth_id and r[0] == drop_created and r[2] != digest(secret):    # an unbound claim of another
                return False                                                                              # secret may be beside that hold
            if r and r[1] is None and r[0] == drop_created and r[2] != digest(secret):     # another secret's unbound claim goes: so does its right
                c.execute("INSERT OR IGNORE INTO retired_secrets VALUES (?, ?, ?)", (drop, r[2], r[0]))     # to start a join
            c.execute("INSERT OR REPLACE INTO buyer_secrets (drop_id, buyer, secret_sha256, drop_created, auth_id, created, attempt) "
                      "VALUES (?, ?, ?, ?, NULL, ?, ?)", (drop, buyer, digest(secret), drop_created, at() if callable(at) else at, attempt))
        return True

    def own(self, drop: str, buyer: str, secret) -> dict | None:
        """This buyer's row, if it is this secret's (whatever life of the drop it is from, and bound or not)."""
        if not well_formed(secret):
            return None
        r = self.row(drop, buyer)
        return r if r and hmac.compare_digest(r["secret_sha256"], digest(secret)) else None

    def claim_of(self, drop: str, buyer: str, secret, drop_created: str | None) -> dict | None:
        """This secret's claim on this buyer, in this life of the drop, that no hold is bound to yet; None if it has none."""
        r = self.own(drop, buyer, secret)
        return r if r and drop_created and r["auth_id"] is None and r["drop_created"] == drop_created else None

    def claimed(self, drop: str, buyer: str, secret, drop_created: str | None) -> bool:
        return self.claim_of(drop, buyer, secret, drop_created) is not None

    def bind(self, drop: str, buyer: str, secret: str, auth_id: str) -> bool:
        """Bind the claim to the hold the engine placed. False if the claim is no longer this secret's, or is already bound."""
        with self._db() as c:
            n = c.execute("UPDATE buyer_secrets SET auth_id = ? WHERE drop_id = ? AND buyer = ? AND secret_sha256 = ? "
                          "AND auth_id IS NULL", (auth_id, drop, buyer, digest(secret))).rowcount
        return n == 1

    def retire(self, drop: str, secret: str, drop_created: str | None) -> None:
        """This secret may never again start a join in this life of the drop."""
        if drop_created:
            with self._db() as c:
                c.execute("INSERT OR IGNORE INTO retired_secrets VALUES (?, ?, ?)", (drop, digest(secret), drop_created))

    def retired(self, drop: str, secret, drop_created: str | None) -> bool:
        if not well_formed(secret) or not drop_created:
            return False
        with self._db() as c:
            return c.execute("SELECT 1 FROM retired_secrets WHERE drop_id = ? AND secret_sha256 = ? AND drop_created = ?",
                             (drop, digest(secret), drop_created)).fetchone() is not None

    def retag(self, drop: str, buyer: str, secret: str, attempt: int) -> None:
        """Correct the attempt number of this secret's own unbound claim to the one the engine gave the join it started."""
        with self._db() as c:
            c.execute("UPDATE buyer_secrets SET attempt = ? WHERE drop_id = ? AND buyer = ? AND secret_sha256 = ? AND auth_id IS NULL",
                      (attempt, drop, buyer, digest(secret)))

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
