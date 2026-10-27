"""Event-sourced SQLite store for drops.

One append-only `events` table (id, ts, drop_id, type, payload_json). Drop state is never stored: it is
a projection rebuilt from the events (`project`). SQLite triggers reject UPDATE and DELETE on events;
the whole store is wiped only by deleting the database file (`reset`, used by `test reset`).

Path: env CROWD_DB; else, when CROWD_ENV=test, $TOGI_HOME/state/crowd/crowd-test.db under Togi (isolated per run)
or data/crowd-test.db; else, with CROWD_PAYPAL=fake, data/crowd-fake.db; else data/crowd.db under the repo (data/ is
git-ignored). A store belongs to one PayPal (every drop records which one it was created with, see engine.require_paypal),
and the default path follows the PayPal mode so a fake can never be pointed at the real drops by accident.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from . import ladder as lad

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVENT_TYPES = {
    "drop_created", "drop_published", "step_locked", "drop_closed", "settlement_started",
    "buyer_joined", "buyer_left",
    "hold_placed", "hold_failed", "hold_captured", "capture_failed", "failed_hold_released", "hold_voided",
    "void_failed",
    # a join is written down BEFORE PayPal is asked, and closed by hold_placed / hold_failed / orphan_hold_released
    "join_started", "order_created", "orphan_hold_released",
    # a leave is written down BEFORE PayPal is asked to void, and closed by buyer_left / leave_failed
    "leave_started", "leave_failed",
    # a person took one buyer out of the crowd (settle was stuck on them, or PayPal would not say what their hold is)
    "buyer_excluded",
    # a capture is written down BEFORE PayPal is asked to make it, so that a capture found on a hold later can be told from one
    # somebody else made: only a capture the engine has an attempt on record for can be the engine's own lost one
    "capture_started",
    # a person bound a drop that recorded no PayPal (written before drops did) to the PayPal now in use
    "paypal_bound",
    # settlement stopped because PayPal refused something that is not about one buyer (or a streak of identical failures)
    "settle_blocked",
    "drop_settled",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  drop_id TEXT NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_drop ON events(drop_id, id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class StoreError(Exception):
    def __init__(self, code: str, message: str = "", **ctx):
        super().__init__(message or code)
        self.code, self.ctx = code, ctx


def default_path() -> str:
    if os.environ.get("CROWD_DB"):
        return os.path.abspath(os.environ["CROWD_DB"])
    if os.environ.get("CROWD_ENV") == "test":
        # Under Togi (stone runs, agent sandboxes) each run has its own TOGI_HOME: keep test drops there so
        # parallel runs never share, or wipe, each other's store, and never touch the repo's data/.
        home = os.environ.get("TOGI_HOME")
        if home:
            return os.path.join(os.path.abspath(home), "state", "crowd", "crowd-test.db")
        return os.path.join(ROOT, "data", "crowd-test.db")
    if os.environ.get("CROWD_PAYPAL", "").strip().lower() == "fake":
        return os.path.join(ROOT, "data", "crowd-fake.db")      # never data/crowd.db: that one holds sandbox drops
    return os.path.join(ROOT, "data", "crowd.db")


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_utc(s) -> datetime:
    """A timestamp from outside (PayPal's expiry) as an aware UTC datetime. A time with no zone is read as UTC. Raises
    ValueError for anything that is not a time at all, so the caller decides what an unreadable time means."""
    if isinstance(s, datetime):
        dt = s
    elif isinstance(s, str) and s.strip():
        try:
            dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ValueError(f"not a timestamp: {s[:40]!r}") from None
    else:
        raise ValueError(f"not a timestamp: {s!r}"[:60])
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def slug(name: str) -> str:
    """Buyer id from a name: 'Sim 001' -> 'sim-001', 'Åsa' -> 'asa'. ASCII only, because it goes into HTTP headers;
    a name with no ASCII letters at all (for example Japanese) gets a stable id from its hash."""
    folded = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    out = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:40].strip("-")
    if not out and str(name).strip():
        out = "b" + hashlib.sha1(str(name).strip().encode()).hexdigest()[:10]
    return out


class Store:
    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path) if path else default_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with contextlib.closing(self._connect()) as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    # ---- writing -------------------------------------------------------------------------------

    def append(self, drop_id: str, type: str, payload: dict | None = None, ts: datetime | None = None) -> dict:
        return self.append_many(drop_id, [(type, payload or {})], ts=ts)[0]

    def append_many(self, drop_id: str, items: list[tuple[str, dict]], ts: datetime | None = None) -> list[dict]:
        """Append several events of one drop in a single transaction (all or none)."""
        for type, _ in items:
            if type not in EVENT_TYPES:
                raise StoreError("unknown_event", f"unknown event type {type}", type=type)
        stamp = iso(ts or datetime.now(timezone.utc))
        out = []
        with contextlib.closing(self._connect()) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for type, payload in items:
                    body = json.dumps(payload, sort_keys=True, default=_json_default)
                    cur = c.execute("INSERT INTO events(ts, drop_id, type, payload_json) VALUES (?,?,?,?)",
                                    (stamp, drop_id, type, body))
                    out.append({"id": cur.lastrowid, "ts": stamp, "drop_id": drop_id, "type": type,
                                "payload": json.loads(body)})
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return out

    # ---- reading -------------------------------------------------------------------------------

    def events(self, drop_id: str | None = None) -> list[dict]:
        q, args = "SELECT id, ts, drop_id, type, payload_json FROM events", ()
        if drop_id is not None:
            q, args = q + " WHERE drop_id = ?", (drop_id,)
        with contextlib.closing(self._connect()) as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        return [{"id": r[0], "ts": r[1], "drop_id": r[2], "type": r[3], "payload": json.loads(r[4])} for r in rows]

    def drop_ids(self) -> list[str]:
        """Every drop id, oldest first."""
        with contextlib.closing(self._connect()) as c:
            rows = c.execute("SELECT drop_id FROM events GROUP BY drop_id ORDER BY MIN(id)").fetchall()
        return [r[0] for r in rows]

    def exists(self, drop_id: str) -> bool:
        with contextlib.closing(self._connect()) as c:
            return c.execute("SELECT 1 FROM events WHERE drop_id = ? AND type = 'drop_created' LIMIT 1",
                             (drop_id,)).fetchone() is not None

    def project(self, drop_id: str, as_of: datetime | None = None) -> "Drop":
        """The drop's state from its events. With `as_of`, only events stamped at or before that instant count: the drop
        as it stood then (used to count a drop's crowd at its deadline, whenever it is closed)."""
        events = self.events(drop_id)
        if not events or events[0]["type"] != "drop_created":
            raise StoreError("drop_not_found", f"no drop {drop_id}", drop=drop_id)
        if as_of is not None:
            events = [e for e in events if parse_iso(e["ts"]) <= as_of]
        return reduce(events)

    # ---- one writer per drop -------------------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, drop_id: str, wait: bool = True):
        """Advisory per-drop lock (flock) so two processes never move money for one drop at once."""
        d = self.path + ".locks"
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, re.sub(r"[^A-Za-z0-9_-]", "_", drop_id) + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                raise StoreError("drop_busy", f"drop {drop_id} is being changed by another process", drop=drop_id) from None
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def reset(self) -> None:
        """Wipe the whole store: delete the database (events are append-only to SQL). Caller checks the environment."""
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path + suffix)
        d = self.path + ".locks"
        if os.path.isdir(d):
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
        with contextlib.closing(self._connect()) as c:
            c.executescript(SCHEMA)


def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return iso(o)
    raise TypeError(f"not serialisable: {type(o).__name__}")


# ---- the projection ----------------------------------------------------------------------------

BUYER_STATES = ("held", "left", "captured", "voided", "capture_failed", "hold_failed", "void_failed", "excluded")


@dataclass
class Buyer:
    id: str
    name: str
    card_index: int = 0
    status: str = "hold_failed"        # held | left | captured | voided | capture_failed | hold_failed | void_failed | excluded
    attempts: int = 0                  # hold_placed + hold_failed events so far (request-id suffix)
    joined_at: str | None = None
    order_id: str | None = None
    auth_id: str | None = None
    hold_amount: Decimal | None = None
    expires: str | None = None
    capture_id: str | None = None
    captured: Decimal | None = None
    fee: Decimal | None = None
    net: Decimal | None = None
    error: str | None = None           # last failure code (hold_failed / capture_failed)
    released: bool = False             # a failed capture's hold was voided afterwards (nothing left open)
    void_failed: bool = False          # PayPal refused to release this hold: it may still be open on the buyer's card
    excluded: bool = False             # a person took this buyer out of the crowd (`drop exclude`): they cannot rejoin

    def to_dict(self) -> dict:
        d = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.__dict__.items()}
        return d


@dataclass
class Drop:
    id: str
    title: str = ""
    maker: str = ""
    currency: str = "USD"
    minimum: int = 0
    duration_minutes: int = 0
    ladder: lad.Ladder | None = None
    costs: dict | None = None
    state: str = "draft"               # draft | live | closed | settling | settled
    created_at: str | None = None
    published_at: str | None = None
    ends_at: str | None = None
    closed_at: str | None = None
    closed_reason: str | None = None
    locked_step: int = -1
    buyers: dict[str, Buyer] = field(default_factory=dict)
    closed: dict | None = None         # drop_closed payload (the crowd that closed the drop)
    settlement: dict | None = None     # settlement_started payload
    settled: dict | None = None        # drop_settled payload
    unresolved: dict[str, dict] = field(default_factory=dict)   # request id -> join_started payload, not yet closed
    leaving: dict[str, dict] = field(default_factory=dict)      # buyer -> leave_started payload (+ asked_at), not yet closed
    capturing: dict[str, dict] = field(default_factory=dict)    # buyer -> capture_started payload, no outcome recorded yet
    blocked: dict | None = None        # the settle_blocked payload while settlement is stopped for a person (cleared on settle)
    lost_after_close: int = 0          # holds that left the crowd after the drop closed and before settlement started
    event_count: int = 0

    @property
    def holding(self) -> list[Buyer]:
        """Buyers whose hold counts toward the crowd, in join order."""
        return [b for b in self.buyers.values() if b.status == "held"]

    @property
    def count(self) -> int:
        """The crowd: holds placed and not released. Once the drop is closed it is the crowd that closed it, counted as of its
        deadline, MINUS every hold that has left the crowd since (`lost_after_close`: settle reads each hold at PayPal before any
        money moves and records one that was voided, lapsed or captured meanwhile, so the funded decision and the price follow
        the crowd that can actually be charged). Once settlement has started it is frozen at the crowd that settlement was
        started for (holds then turn into captures, voids or failures, but the crowd the price was set for does not change)."""
        if self.settlement:
            return int(self.settlement["count"])
        if self.closed and self.closed.get("count") is not None:
            return max(0, int(self.closed["count"]) - self.lost_after_close)
        return len(self.holding)

    def price(self) -> Decimal:
        return lad.price_at(self.ladder, self.count, self.locked_step)


def reduce(events: list[dict]) -> Drop:
    """Fold a drop's events into its current state. Pure; the only source of state."""
    d: Drop | None = None
    for e in events:
        t, p = e["type"], e["payload"]
        if d is not None and d.blocked and d.state in ("closed", "settling") and t in (
                "hold_captured", "hold_voided", "capture_failed", "failed_hold_released", "void_failed", "buyer_excluded",
                "settlement_started"):
            d.blocked = None       # settlement went on after a block: it is no longer stopped (a new block sets it again)
        held_before = {b.id for b in d.holding} if d is not None and d.state == "closed" else None
        if t == "drop_created":
            d = Drop(id=e["drop_id"], title=p["title"], maker=p.get("maker", ""), currency=p.get("currency", "USD"),
                     minimum=int(p["minimum"]), duration_minutes=int(p["duration_minutes"]),
                     ladder=lad.Ladder.from_dict(p["ladder"]), costs=p.get("costs"), state="draft",
                     created_at=e["ts"])
            continue
        if d is None:
            raise StoreError("corrupt", "events do not start with drop_created", drop=e["drop_id"])
        d.event_count += 1
        if t == "drop_published":
            d.state, d.published_at, d.ends_at = "live", e["ts"], p["ends_at"]
        elif t == "step_locked":
            d.locked_step = max(d.locked_step, int(p["step"]))
        elif t == "drop_closed":
            d.state, d.closed_at, d.closed_reason, d.closed = "closed", e["ts"], p.get("reason"), p
        elif t == "settlement_started":
            d.state, d.settlement = "settling", p
        elif t == "drop_settled":
            d.state, d.settled, d.blocked = "settled", p, None
        elif t == "buyer_joined":
            b = d.buyers.get(p["buyer"]) or Buyer(id=p["buyer"], name=p["name"])
            b.name, b.card_index, b.joined_at = p["name"], int(p.get("card_index", 0)), e["ts"]
            d.buyers[b.id] = b
        elif t == "join_started":
            prior = d.unresolved.get(p["request_id"])      # the same request asked again: it is still the same order
            kept = {k: prior[k] for k in ("order_id", "order_ids", "released_orders") if prior and prior.get(k)}
            d.unresolved[p["request_id"]] = {**kept, **p}
        elif t == "leave_started":
            d.leaving[p["buyer"]] = {**p, "asked_at": e["ts"]}
        elif t == "leave_failed":
            d.leaving.pop(p["buyer"], None)
        elif t == "settle_blocked":
            d.blocked = {**p, "at": e["ts"]}
        elif t == "order_created":
            j = d.unresolved.get(p.get("request_id"))
            if j is not None:
                j["order_id"] = p.get("order_id")
                ids = j.setdefault("order_ids", [])        # every order this intent has made: PayPal may answer the same
                if p.get("order_id") and p["order_id"] not in ids:    # request with a NEW order once it has forgotten the first
                    ids.append(p["order_id"])
        elif t == "orphan_hold_released" and p.get("superseded"):
            j = d.unresolved.get(p.get("request_id"))      # an EARLIER order's hold, released: the intent itself stays open
            if j is not None and p.get("order_id"):
                j.setdefault("released_orders", []).append(p["order_id"])
        elif t == "orphan_hold_released":
            d.unresolved.pop(p.get("request_id"), None)
            if p.get("buyer"):      # that attempt's request id is spent (PayPal would replay the voided hold): move on
                b = d.buyers.setdefault(p["buyer"], Buyer(id=p["buyer"], name=p.get("name", p["buyer"])))
                b.attempts += 1
        elif t == "capture_started":
            d.capturing[p["buyer"]] = p
        elif t == "hold_placed":
            d.unresolved.pop(p.get("request_id"), None)
            d.capturing.pop(p["buyer"], None)
            b = d.buyers.setdefault(p["buyer"], Buyer(id=p["buyer"], name=p.get("name", p["buyer"])))
            b.status, b.attempts = "held", b.attempts + 1
            b.order_id, b.auth_id, b.expires = p.get("order_id"), p["auth_id"], p.get("expires")
            b.hold_amount, b.error = lad.D(p["amount"]), None
            b.capture_id = b.captured = b.fee = b.net = None
        elif t == "hold_failed":
            d.unresolved.pop(p.get("request_id"), None)
            b = d.buyers.setdefault(p["buyer"], Buyer(id=p["buyer"], name=p.get("name", p["buyer"])))
            b.attempts += 1
            if b.status != "held":
                b.status = "hold_failed"
            b.error = p.get("code")
            b.card_index = int(p.get("card_index", b.card_index))
        elif t == "buyer_left":
            b = d.buyers[p["buyer"]]
            b.status = "left"
            d.leaving.pop(p["buyer"], None)
            d.capturing.pop(p["buyer"], None)
        elif t == "hold_voided":
            b = d.buyers[p["buyer"]]
            d.capturing.pop(p["buyer"], None)
            if b.status == "held":  # a `left` buyer was already marked by buyer_left
                b.status = "voided"
        elif t == "hold_captured":
            b = d.buyers[p["buyer"]]
            d.capturing.pop(p["buyer"], None)
            b.status, b.capture_id = "captured", p["capture_id"]
            b.captured, b.fee, b.net = lad.D(p["amount"]), lad.D(p.get("fee", "0")), lad.D(p.get("net", p["amount"]))
        elif t == "capture_failed":
            b = d.buyers[p["buyer"]]
            d.capturing.pop(p["buyer"], None)
            b.status, b.error = "capture_failed", p.get("code")
        elif t == "failed_hold_released":
            d.buyers[p["buyer"]].released = True
        elif t == "buyer_excluded":
            b = d.buyers[p["buyer"]]
            b.excluded, b.status = True, "excluded"        # out of the crowd for good; the events that follow say what became of the hold
            d.leaving.pop(p["buyer"], None)
            d.capturing.pop(p["buyer"], None)
        elif t == "void_failed":
            if p.get("orphan") and p.get("superseded"):     # an earlier order's hold that could not be released: resolved as stuck
                j = d.unresolved.get(p.get("request_id"))
                if j is not None and p.get("order_id"):
                    j.setdefault("released_orders", []).append(p["order_id"])
            if not p.get("orphan"):  # an orphan hold was never a buyer's: the event only names the open hold
                b = d.buyers[p["buyer"]]
                b.void_failed, b.error = True, p.get("code") or b.error
                if b.status == "held":
                    b.status = "void_failed"
        if held_before is not None and d.state == "closed":      # between close and the start of settlement
            d.lost_after_close += len(held_before - {b.id for b in d.holding})
    if d is None:
        raise StoreError("drop_not_found", "no events")
    return d


def project(drop_id: str, store: "Store | None" = None) -> Drop:
    """The current state of a drop, rebuilt from its events."""
    return (store or Store()).project(drop_id)
