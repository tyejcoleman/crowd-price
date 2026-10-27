import sqlite3
from decimal import Decimal

import pytest

from crowd import store as S
from crowd import ladder as lad


def test_events_are_append_only(store):
    store.append("d_1", "drop_created", {"title": "x"})
    con = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE events SET type = 'drop_closed'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM events")
    assert len(store.events("d_1")) == 1


def test_unknown_event_type_is_refused_and_a_batch_is_all_or_nothing(store):
    with pytest.raises(S.StoreError):
        store.append("d_1", "money_appeared", {})
    with pytest.raises(S.StoreError):
        store.append_many("d_1", [("drop_created", {}), ("nope", {})])
    assert store.events("d_1") == []


def test_path_defaults_and_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv("TOGI_HOME", raising=False)                  # outside Togi: the repo's data/ dir
    assert S.default_path().endswith("data/crowd.db")
    monkeypatch.setenv("CROWD_ENV", "test")
    assert S.default_path().endswith("data/crowd-test.db")           # tests never touch real drops
    monkeypatch.setenv("CROWD_DB", str(tmp_path / "mine.db"))
    assert S.default_path() == str(tmp_path / "mine.db")             # explicit override wins
    s = S.Store()
    assert s.path == str(tmp_path / "mine.db")


def test_projection_is_rebuilt_from_events_only(engine, drop):
    for i in range(1, 6):
        engine.join(drop, f"B{i}")
    d1 = engine.store.project(drop)
    d2 = S.reduce(engine.store.events(drop))                          # no hidden state anywhere
    assert (d1.state, d1.count, d1.locked_step, d1.price()) == (d2.state, d2.count, d2.locked_step, d2.price())
    assert d1.count == 5 and d1.locked_step == 1 and d1.price() == Decimal("36.00")
    # a fresh Store on the same file sees the same drop
    assert S.Store(engine.store.path).project(drop).count == 5


def test_projection_of_unknown_drop(store):
    with pytest.raises(S.StoreError) as e:
        store.project("d_nope")
    assert e.value.code == "drop_not_found"


def test_drop_ids_oldest_first_and_exists(engine):
    a = engine.create_drop("A", lad.parse("2:10"), 2, 10)
    b = engine.create_drop("B", lad.parse("2:10"), 2, 10)
    assert engine.store.drop_ids() == [a, b]
    assert engine.store.exists(a) and not engine.store.exists("d_zzzz")


def test_reset_wipes_the_store(engine, drop):
    engine.store.reset()
    assert engine.store.drop_ids() == []


def test_lock_is_exclusive_between_users(store):
    with store.lock("d_1"):
        with pytest.raises(S.StoreError) as e:
            with store.lock("d_1", wait=False):
                pass
        assert e.value.code == "drop_busy"
        with store.lock("d_2", wait=False):      # other drops are independent
            pass
    with store.lock("d_1", wait=False):          # released
        pass


def test_slug():
    assert S.slug("Sim 001") == "sim-001" and S.slug("  Åsa B.  ") == "asa-b" and S.slug("   ") == ""
    jp = S.slug("田中 太郎")                                   # no ASCII letters: a stable hash id, never empty
    assert jp.startswith("b") and jp == S.slug("田中 太郎") and jp.isascii() and jp != S.slug("山田 花子")


def test_test_store_lives_in_the_togi_home_so_parallel_runs_never_share(monkeypatch, tmp_path):
    from crowd import store as S
    monkeypatch.delenv("CROWD_DB", raising=False)
    monkeypatch.setenv("CROWD_ENV", "test")
    monkeypatch.setenv("TOGI_HOME", str(tmp_path / "a"))
    a = S.default_path()
    monkeypatch.setenv("TOGI_HOME", str(tmp_path / "b"))
    assert a != S.default_path() and a.startswith(str(tmp_path / "a"))
    monkeypatch.delenv("CROWD_ENV")
    assert S.default_path().endswith("data/crowd.db")
