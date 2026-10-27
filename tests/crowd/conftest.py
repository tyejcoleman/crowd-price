"""Shared fixtures for the Crowd Price tests. Everything is offline: a temp store and the fake PayPal."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from crowd import ladder as lad  # noqa: E402
from crowd.engine import Engine  # noqa: E402
from crowd.paypal_holds import FakePayPal  # noqa: E402
from crowd.store import Store  # noqa: E402


class Clock:
    """A clock the tests move by hand."""

    def __init__(self):
        self.t = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CROWD_DB", "CROWD_ENV", "CROWD_PAYPAL", "TOGI_AGENT"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(tmp_path):
    return FakePayPal(str(tmp_path / "pp.json"))


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "crowd.db"))


@pytest.fixture
def engine(store, fake, clock):
    return Engine(store, fake, clock)


LADDER = "3:40,5:36,7:33"   # minimum 3; 40 -> 36 at 5 buyers -> 33 at 7 buyers


@pytest.fixture
def drop(engine):
    """A live drop: minimum 3, 60 minutes, ladder 3:40,5:36,7:33."""
    d = engine.create_drop("Speckled mugs", lad.parse(LADDER), 3, 60, maker="Juniper")
    engine.publish(d)
    return d


def join_many(engine, drop, n, start=1):
    out = None
    for i in range(start, start + n):
        out = engine.join(drop, f"Buyer {i:02d}", card_index=i % 8)
    return out
