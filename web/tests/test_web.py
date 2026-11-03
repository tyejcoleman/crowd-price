"""The web layer: the buyer's page and the maker's view over the engine. Offline: a temp store and the fake PayPal.

What must hold: a page shows only the engine's numbers; a buyer can join and leave only their own hold (a token proves it);
nothing on the web can settle, exclude or bind (no route, no call); the maker's view needs the maker key.
"""
import json
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crowd import ladder as lad
from crowd.engine import Engine
from crowd.paypal_holds import FakePayPal
from crowd.store import Store
from crowdweb.api import Api, ApiError
from crowdweb.feed import feed
from crowdweb.server import JOINS_PER_MINUTE, serve

WEB = Path(__file__).resolve().parents[1] / "crowdweb"
KEY = "maker-key-for-tests"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("CROWD_DB", "CROWD_ENV", "CROWD_PAYPAL", "TOGI_AGENT", "TOGI_APPROVER_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TOGI_HOME", str(tmp_path / "togi"))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake(tmp_path):
    return FakePayPal(str(tmp_path / "pp.json"))


@pytest.fixture
def engine(tmp_path, fake, clock):
    return Engine(Store(str(tmp_path / "crowd.db")), fake, clock)


@pytest.fixture
def api(engine, tmp_path):
    return Api(engine, maker_name="Juniper", togi_home=str(tmp_path / "togi"))


@pytest.fixture
def drop(engine):
    d = engine.create_drop("Speckled mugs", lad.parse("3:40,5:36,7:33"), 3, 60, maker="Juniper")
    engine.publish(d)
    return d


def sim(engine, drop, n, start=1):
    for i in range(start, start + n):
        engine.join(drop, f"Sim {i:03d}", card_index=i % 8)


# ---- reading -------------------------------------------------------------------------------------------------

def test_a_draft_is_not_shown(api, engine):
    d = engine.create_drop("Not yet", lad.parse("3:40"), 3, 60)
    with pytest.raises(ApiError) as e:
        api.drop(d)
    assert e.value.status == 404
    assert all(x["drop"] != d for x in api.drops())


def test_the_public_view_is_the_engines_numbers(api, engine, drop):
    sim(engine, drop, 5)
    v, ev = api.drop(drop), engine.view(engine.get(drop))
    for k in ("price", "start_price", "buyers", "minimum", "minimum_met", "locked_step", "next_step", "next_message", "state"):
        assert v[k] == ev[k], k
    assert v["price"] == "36.00" and v["joinable"] is True and v["maker_name"] == "Juniper"
    assert [s["price"] for s in v["steps"]] == ["40.00", "36.00", "33.00"]
    assert [s["from_buyers"] for s in v["steps"]] == [3, 5, 7]


# ---- the buyer -----------------------------------------------------------------------------------------------

def test_join_places_a_real_hold_and_returns_a_token(api, engine, fake, drop):
    r = api.join(drop, "Ada Lovelace", 2)
    b = engine.get(drop).buyers[r["buyer"]]
    assert b.status == "held" and str(b.hold_amount) == "40.00" and b.card_index == 2
    assert fake.get(b.auth_id)["status"] in ("CREATED", "AUTHORIZED")
    assert r["me"]["held"] == "40.00" and r["me"]["card"] == "Test card ending 4444" and r["me"]["can_leave"] is True
    assert api.me(drop, r["buyer"], r["token"])["me"]["status"] == "held"


@pytest.mark.parametrize("card", [8, 9, -1, "0", True, None, 1.0])
def test_only_the_offered_test_cards(api, engine, drop, card):
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", card)
    assert e.value.code == "bad_card" and not engine.get(drop).buyers


def test_a_taken_name_is_refused_in_words(api, drop):
    api.join(drop, "Ada", 0)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", 1)
    assert e.value.status == 409 and "already" in e.value.message


def test_a_token_is_needed_to_see_or_leave_a_hold(api, engine, drop):
    r = api.join(drop, "Ada", 0)
    for bad in ("", "x" * 32, None, 7, r["token"] + "x"):
        with pytest.raises(ApiError) as e:
            api.leave(drop, r["buyer"], bad)
        assert e.value.code == "not_yours"
        with pytest.raises(ApiError):
            api.me(drop, r["buyer"], bad)
    assert engine.get(drop).buyers[r["buyer"]].status == "held"


def test_a_buyer_the_assistant_added_cannot_be_released_from_the_web(api, engine, drop):
    sim(engine, drop, 1)
    with pytest.raises(ApiError) as e:
        api.leave(drop, "sim-001", "anything")
    assert e.value.code == "not_yours" and engine.get(drop).buyers["sim-001"].status == "held"


def test_leave_releases_the_hold(api, engine, fake, drop):
    r = api.join(drop, "Ada", 0)
    auth = engine.get(drop).buyers[r["buyer"]].auth_id
    out = api.leave(drop, r["buyer"], r["token"])
    assert out["released"] == "40.00" and out["me"]["status"] == "left"
    assert fake.get(auth)["status"] == "VOIDED"
    assert api.drop(drop)["buyers"] == 0


def test_joining_after_the_deadline_is_refused(api, engine, clock, drop):
    clock.advance(minutes=61)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Late", 0)
    assert e.value.status == 409 and not engine.get(drop).buyers


# ---- settled: every figure is the engine's ---------------------------------------------------------------------

def settle(engine, drop):
    engine.close(drop)
    p = engine.plan(drop)
    from crowd.engine import plan_token
    return engine.settle(drop, plan_token(p))


def test_after_settlement_the_buyer_sees_what_they_paid(api, engine, drop):
    r = api.join(drop, "Ada", 0)
    sim(engine, drop, 4)
    settle(engine, drop)
    me = api.me(drop, r["buyer"], r["token"])["me"]
    b = engine.get(drop).buyers[r["buyer"]]
    assert me["status"] == "captured" and me["charged"] == str(b.captured) == "36.00" and me["can_leave"] is False
    pub = api.drop(drop)
    assert pub["settled"] == {"outcome": "funded", "final_price": "36.00", "paid": 5}


def test_the_maker_sees_the_engines_totals(api, engine, drop):
    sim(engine, drop, 5)
    settle(engine, drop)
    mk = api.maker(drop)
    s = engine.get(drop).settled
    assert mk["settlement"]["totals"] == s["totals"] and mk["settlement"]["counts"] == s["counts"]
    assert mk["buyers_by_status"] == {"captured": 5}
    assert mk["feed"][0]["text"].startswith("Settled: 5 paid $36.00")


def test_a_missed_minimum_releases_everyone(api, engine, fake, drop):
    r = api.join(drop, "Ada", 0)
    settle(engine, drop)
    me = api.me(drop, r["buyer"], r["token"])["me"]
    assert me["status"] == "voided" and me["charged"] is None
    assert api.drop(drop)["settled"]["outcome"] != "funded"


# ---- the maker's feed and approvals ------------------------------------------------------------------------------

def test_the_feed_groups_a_batch_and_says_when_the_price_drops(api, engine, drop):
    sim(engine, drop, 5)
    lines = [f["text"] for f in api.maker(drop)["feed"]]
    assert lines[:4] == ["5 buyers: everyone now pays $36.00", "Sim 004 and Sim 005 joined. $40.00 held each",
                         "Minimum reached with 3 buyers: everyone pays $40.00", "Sim 001, Sim 002 and Sim 003 joined. $40.00 held each"]
    assert lines[-1] == "Drop created: Speckled mugs, minimum 3"
    many = [{"ts": "2026-11-01T12:00:00Z", "type": "hold_placed", "payload": {"buyer": f"b{i}", "name": f"B{i}", "amount": "40.00"}}
            for i in range(6)]
    assert feed(many, {}, "T")[0]["text"] == "B0, B1 and 4 others joined. $40.00 held each"


def test_feed_does_not_repeat_a_leave(engine, drop, api):
    r = api.join(drop, "Ada", 0)
    api.leave(drop, r["buyer"], r["token"])
    texts = [f["text"] for f in api.maker(drop)["feed"]]
    assert "Ada left. Hold released" in texts and not any("hold released" in t and "Ada's" in t for t in texts)


def write_approval(home: Path, **rec):
    (home / "approvals").mkdir(parents=True, exist_ok=True)
    base = {"id": "ap_0123456789", "bundle": "crowd", "command": "drop settle", "status": "pending",
            "created": "2026-11-01T12:00:00.000Z", "expires": "2999-01-01T00:00:00.000Z", "summary": "crowd drop settle"}
    base.update(rec)
    (home / "approvals" / f"{base['id']}.json").write_text(json.dumps(base))


def test_the_maker_sees_a_waiting_approval_for_this_drop_only(api, drop, tmp_path):
    home = tmp_path / "togi"
    write_approval(home, args={"drop": drop, "expect": "capture:5:36.00:180.00"})
    write_approval(home, id="ap_other00000", args={"drop": "d_other", "expect": "void:1"})
    write_approval(home, id="ap_sheet00000", bundle="sheet", command="drop", args={"table": "x"})
    write_approval(home, id="ap_old0000000", created="2026-10-01T00:00:00.000Z", expires="2026-10-01T00:15:00.000Z",
                   args={"drop": drop, "expect": "capture:3:40.00:120.00"})
    got = api.maker(drop)["approvals"]
    assert [(a["id"], a["status"]) for a in got] == [("ap_0123456789", "pending"), ("ap_old0000000", "expired")]
    assert got[0]["expect"] == "capture:5:36.00:180.00"


def test_feed_unit_grouping_respects_time_gaps():
    ev = [{"ts": f"2026-11-01T12:0{i}:00Z", "type": "hold_placed", "payload": {"buyer": f"b{i}", "name": f"B{i}", "amount": "40.00"}}
          for i in range(3)]
    ev[2]["ts"] = "2026-11-01T12:09:00Z"
    out = feed(ev, {}, "T")
    assert [f["count"] for f in out] == [1, 2]


# ---- nothing on the web moves money on its own --------------------------------------------------------------------

def test_the_web_never_calls_settle_exclude_or_bind():
    src = "\n".join(p.read_text() for p in WEB.rglob("*.py"))
    assert not re.search(r"\.(settle|exclude|bind|bind_plan|reset|reconcile)\(", src)
    assert "test_settle" not in src and "togi_bundle" not in src


# ---- over HTTP ------------------------------------------------------------------------------------------------------

@pytest.fixture
def http(api):
    httpd = serve(api, KEY, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, headers=None, method=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"))
        if data is not None and ctype:
            req.add_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
    yield call
    httpd.shutdown()
    httpd.server_close()


def test_pages_and_files_are_served_with_a_strict_policy(http, drop):
    st, h, body = http(f"/d/{drop}")
    assert st == 200 and b"drop.js" in body
    assert "script-src 'self'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"
    assert http("/static/app.css")[0] == 200
    for bad in ("/static/../api.py", "/static/api.py", "/static/drop.html%2e%2e", "/d/../../etc/passwd", "/d/a%2Fb"):
        assert http(bad)[0] == 404, bad


def test_the_maker_view_needs_the_key(http, drop):
    assert http(f"/api/maker/drops/{drop}")[0] == 401
    assert http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": "wrong"})[0] == 401
    st, _, body = http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": KEY})
    assert st == 200 and json.loads(body)["data"]["drop"] == drop


def test_there_is_no_route_that_settles(http, drop):
    for path in (f"/api/maker/drops/{drop}/settle", f"/api/drops/{drop}/settle", f"/api/maker/drops/{drop}/exclude",
                 f"/api/maker/drops/{drop}/bind", f"/api/drops/{drop}/close"):
        st, _, _ = http(path, body={}, headers={"X-Maker-Key": KEY})
        assert st == 404, path


def test_join_over_http_and_its_guards(http, engine, drop):
    st, _, body = http(f"/api/drops/{drop}/join", body={"name": "Ada", "card": 0})
    data = json.loads(body)["data"]
    assert st == 200 and data["token"] and engine.get(drop).buyers["ada"].status == "held"
    assert http(f"/api/drops/{drop}/join", raw=b'{"name":"Bo","card":0}', ctype="text/plain")[0] == 415
    assert http(f"/api/drops/{drop}/join", raw=b"x" * 5000)[0] == 413
    assert http(f"/api/drops/{drop}/join", raw=b"[1]")[0] == 400


def test_joins_are_rate_limited_per_address(http, drop):
    codes = [http(f"/api/drops/{drop}/join", body={"name": f"P {i}", "card": 0})[0] for i in range(JOINS_PER_MINUTE + 2)]
    assert codes[:JOINS_PER_MINUTE].count(200) == JOINS_PER_MINUTE and codes[-1] == 429


def test_errors_never_carry_a_trace(http, api, monkeypatch, drop):
    def boom(*a, **k):
        raise RuntimeError("secret internals /Users/x")
    monkeypatch.setattr(api, "drop", boom)
    st, _, body = http(f"/api/drops/{drop}")
    assert st == 500 and b"secret" not in body and b"Traceback" not in body


def test_static_files_use_no_inline_script_or_style():
    for f in (WEB / "static").glob("*.html"):
        t = f.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", t), f.name
        assert " style=" not in t and "<style" not in t, f.name
    js = "\n".join(p.read_text() for p in (WEB / "static").glob("*.js"))
    assert "innerHTML" not in js and "setAttribute(\"style\"" not in js
