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
    for k in ("CROWD_DB", "CROWD_ENV", "CROWD_PAYPAL", "TOGI_AGENT", "TOGI_APPROVER_HOME"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _tests_never_touch_the_persons_approver():
    """A test run must not create, change or remove the person's own Togi approver (<pw_dir>/.togi/approver, approver.key,
    approver.state): a known passphrase planted there would let anything approve money. Every test approver is a throwaway
    under TOGI_APPROVER_HOME. This only LOOKS (it never creates or deletes those files). If they already exist when the
    session starts we cannot tell whose they are, so the session warns and then fails only if they CHANGE; if they do not
    exist, the session fails if any appears."""
    import warnings
    from togi_helpers import approver_files_touched, real_approver_snapshot
    before = real_approver_snapshot()
    if before:
        warnings.warn(f"{sorted(before)} already exist in ~/.togi at the start of this test session; whether a person or an "
                      f"earlier test run made them is unknown. They are left alone; the session fails if they change.")
    yield
    touched = approver_files_touched(before, real_approver_snapshot())
    assert not touched, (f"this test session created or changed the person's own Togi approver files in ~/.togi: {touched}. "
                         f"Every test approver must live under TOGI_APPROVER_HOME. Do not delete them from a test; tell the owner.")


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
