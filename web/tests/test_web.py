"""The web layer: the buyer's page and the maker's view over the engine. Offline: a temp store and the fake PayPal.

What must hold: a page shows only the engine's numbers; a buyer can join and leave only their own hold (a token proves it);
nothing on the web can settle, exclude or bind (no route, no call); the maker's view needs the maker key.
"""
import contextlib
import json
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crowd import ladder as lad
from crowd.engine import CrowdError, Engine
from crowd.paypal_holds import FakePayPal
from crowd.store import Store
from crowdweb.api import Api, ApiError
from crowdweb.feed import feed
from crowdweb.server import JOINS_PER_MINUTE, serve
from crowdweb.tokens import Tokens, digest

WEB = Path(__file__).resolve().parents[1] / "crowdweb"
KEY = "maker-key-for-tests"


def sec(tag: str = "a") -> str:
    """A browser secret: what drop.js makes with crypto.getRandomValues before it asks to join."""
    return (tag * 43)[:43]


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

def test_join_places_a_real_hold_and_binds_the_secret(api, engine, fake, drop):
    r = api.join(drop, "Ada Lovelace", 2, sec())
    b = engine.get(drop).buyers[r["buyer"]]
    assert b.status == "held" and str(b.hold_amount) == "40.00" and b.card_index == 2
    assert fake.get(b.auth_id)["status"] in ("CREATED", "AUTHORIZED")
    assert r["me"]["held"] == "40.00" and r["me"]["card"] == "Test card ending 4444" and r["me"]["can_leave"] is True
    assert api.me(drop, r["buyer"], sec())["me"]["status"] == "held"


@pytest.mark.parametrize("card", [8, 9, -1, "0", True, None, 1.0])
def test_only_the_offered_test_cards(api, engine, drop, card):
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", card, sec())
    assert e.value.code == "bad_card" and not engine.get(drop).buyers


def test_a_taken_name_is_refused_in_words(api, drop):
    api.join(drop, "Ada", 0, sec("a"))
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", 1, sec("b"))
    assert e.value.status == 409 and "taken" in e.value.message


def test_a_secret_is_needed_to_see_or_leave_a_hold(api, engine, drop):
    r = api.join(drop, "Ada", 0, sec())
    for bad in ("", "x" * 32, None, 7, sec() + "x", sec("b"), "\ud800" * 40):
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
    r = api.join(drop, "Ada", 0, sec())
    auth = engine.get(drop).buyers[r["buyer"]].auth_id
    out = api.leave(drop, r["buyer"], sec())
    assert out["released"] == "40.00" and out["me"]["status"] == "left"
    assert fake.get(auth)["status"] == "VOIDED"
    assert api.drop(drop)["buyers"] == 0


def test_joining_after_the_deadline_is_refused(api, engine, clock, drop):
    clock.advance(minutes=61)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Late", 0, sec())
    assert e.value.status == 409 and not engine.get(drop).buyers


# ---- settled: every figure is the engine's ---------------------------------------------------------------------

def settle(engine, drop):
    engine.close(drop)
    p = engine.plan(drop)
    from crowd.engine import plan_token
    return engine.settle(drop, plan_token(p))


def test_after_settlement_the_buyer_sees_what_they_paid(api, engine, drop):
    r = api.join(drop, "Ada", 0, sec())
    sim(engine, drop, 4)
    settle(engine, drop)
    me = api.me(drop, r["buyer"], sec())["me"]
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
    r = api.join(drop, "Ada", 0, sec())
    settle(engine, drop)
    me = api.me(drop, r["buyer"], sec())["me"]
    assert me["status"] == "voided" and me["charged"] is None
    assert api.drop(drop)["settled"]["outcome"] != "funded"


# ---- the maker's feed and approvals ------------------------------------------------------------------------------

def test_the_feed_groups_a_batch_and_says_when_the_price_drops(api, engine, drop):
    sim(engine, drop, 5)
    lines = [(f["who"], f["text"]) for f in api.maker(drop)["feed"]]
    assert lines[:4] == [(None, "5 buyers: everyone now pays $36.00"),
                         ("Simulated buyer 004 and Simulated buyer 005", "joined. $40.00 held each"),
                         (None, "Minimum reached with 3 buyers: everyone pays $40.00"),
                         ("Simulated buyer 001, Simulated buyer 002 and Simulated buyer 003", "joined. $40.00 held each")]
    assert lines[-1] == (None, "Drop created: Speckled mugs, minimum 3")
    many = [{"ts": "2026-11-01T12:00:00Z", "type": "hold_placed", "payload": {"buyer": f"b{i}", "name": f"B{i}", "amount": "40.00"}}
            for i in range(6)]
    top = feed(many, {}, "T")[0]
    assert (top["who"], top["text"]) == ("B0, B1 and 4 others", "joined. $40.00 held each")


def test_feed_does_not_repeat_a_leave(engine, drop, api):
    r = api.join(drop, "Ada", 0, sec())
    api.leave(drop, r["buyer"], sec())
    lines = [(f["who"], f["text"]) for f in api.maker(drop)["feed"]]
    assert ("Ada", "left. Hold released") in lines and not any(t == "hold released" for _, t in lines)


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

def test_the_web_never_calls_settle_exclude_bind_or_close():
    src = "\n".join(p.read_text() for p in WEB.rglob("*.py"))
    assert not re.search(r"engine\.(settle|exclude|bind|bind_plan|close|reset|reconcile|plan|publish|create_drop)\(", src)
    assert not re.search(r"\.(settle|exclude|bind_plan|reconcile)\(", src)
    assert "test_settle" not in src and "togi_bundle" not in src


# ---- over HTTP ------------------------------------------------------------------------------------------------------

@contextlib.contextmanager
def running(api, **kw):
    """The server on a free port, and a `call` that talks to it (status, headers, body)."""
    httpd = serve(api, KEY, "127.0.0.1", 0, **kw)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

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
    call.port = port
    call.httpd = httpd
    try:
        yield call
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def http(api):
    with running(api) as call:
        yield call


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
    st, _, body = http(f"/api/drops/{drop}/join", body={"name": "Ada", "card": 0, "secret": sec()})
    data = json.loads(body)["data"]
    assert st == 200 and "token" not in data and engine.get(drop).buyers["ada"].status == "held"
    assert http(f"/api/drops/{drop}/join", raw=b'{"name":"Bo","card":0}', ctype="text/plain")[0] == 415
    assert http(f"/api/drops/{drop}/join", raw=b"x" * 5000)[0] == 413
    assert http(f"/api/drops/{drop}/join", raw=b"[1]")[0] == 400


def test_joins_are_rate_limited_per_address(http, drop):
    codes = [http(f"/api/drops/{drop}/join", body={"name": f"P {i}", "card": 0, "secret": sec(str(i % 10))})[0]
             for i in range(JOINS_PER_MINUTE + 2)]
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


# ---- what the crowd saved, the mode, product details ---------------------------------------------------------------

def test_saved_each_is_the_engines_start_minus_the_engines_price(api, engine, drop):
    assert api.drop(drop)["saved_each"] is None                  # nobody yet: no price reached
    sim(engine, drop, 4)
    assert api.drop(drop)["saved_each"] is None                  # minimum met at the start price: nothing saved yet
    sim(engine, drop, 1, start=5)
    d = engine.get(drop)
    assert api.drop(drop)["saved_each"] == str(d.ladder.start_price - d.price()) == "4.00"


def test_a_charged_buyer_sees_what_they_saved(api, engine, drop):
    r = api.join(drop, "Ada", 0, sec())
    sim(engine, drop, 6)
    settle(engine, drop)
    me = api.me(drop, r["buyer"], sec())["me"]
    b = engine.get(drop).buyers[r["buyer"]]
    assert me["charged"] == "33.00" and me["saved"] == str(b.hold_amount - b.captured) == "7.00"
    assert api.drop(drop)["saved_each"] == "7.00"


def test_the_pages_say_which_paypal(api, drop):
    assert api.info()["paypal"] == "fake" and api.drop(drop)["paypal"] == "fake"


def test_simulated_buyers_are_named_as_such(api, engine, drop):
    sim(engine, drop, 2)
    api.join(drop, "Ada", 0, sec())
    mk = api.maker(drop)
    assert [b["name"] for b in mk["buyers_list"]] == ["Simulated buyer 001", "Simulated buyer 002", "Ada"]
    assert mk["simulated"] == 2


def test_product_details_and_only_named_photos(tmp_path, engine, drop, http_factory=None):
    from crowdweb.api import load_details
    folder = tmp_path / "details"
    folder.mkdir()
    (folder / "mugs.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    (folder / "secret.txt").write_text("no")
    (folder / "d.json").write_text(json.dumps({
        "Speckled mugs": {"what": "One 12 oz stoneware mug", "ships": "Ships in 3 weeks", "photo": "mugs.jpg"},
        "Other": {"photo": "../d.json"}, "Txt": {"photo": "secret.txt"}}))
    a = Api(engine, details=load_details(str(folder / "d.json")), togi_home=str(tmp_path / "togi"))
    det = a.drop(drop)["details"]
    assert det == {"what": "One 12 oz stoneware mug", "ships": "Ships in 3 weeks", "photo": "/media/0"}
    assert a.photo("0") == (b"\xff\xd8\xff fake jpeg", "image/jpeg")
    for bad in ("1", "2", "../d.json", "0/../1"):
        with pytest.raises(ApiError):
            a.photo(bad)


def test_a_settled_drop_points_buyers_to_the_makers_open_drops(api, engine, drop):
    other = engine.create_drop("Speckled cups", lad.parse("3:24"), 3, 60, maker="Juniper")
    engine.publish(other)
    sim(engine, drop, 3)
    settle(engine, drop)
    assert [o["drop"] for o in api.drop(drop)["others"]] == [other]


# ---- the refuter's breaks, kept as regression tests (round 1 of the web layer) -------------------------------------

def test_a_join_asked_again_after_a_lost_reply_is_handed_back_not_placed_twice(api, engine, drop):
    first = api.join(drop, "Grace Hopper", 2, sec("g"))            # the reply never reached the browser
    again = api.join(drop, "Grace Hopper", 2, sec("g"))            # the browser asks again with the same secret
    assert again["buyer"] == first["buyer"] and again.get("again") is True
    d = engine.get(drop)
    assert d.count == 1 and [b.status for b in d.buyers.values()] == ["held"]
    with pytest.raises(ApiError) as e:                              # someone else, same name: refused, nothing placed
        api.join(drop, "Grace Hopper", 2, sec("h"))
    assert e.value.code == "already_joined" and engine.get(drop).count == 1


def test_a_stale_secret_cannot_release_a_later_buyer_of_the_same_name(api, engine, drop):
    r = api.join(drop, "Ada Lovelace", 0, sec())
    api.leave(drop, r["buyer"], sec())
    engine.join(drop, "Ada Lovelace", 3)                           # the assistant adds a buyer of that name
    for call in (api.me, api.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "ada-lovelace", sec())
        assert e.value.code == "not_yours"
    assert engine.get(drop).buyers["ada-lovelace"].status == "held"


def test_a_secret_from_before_a_store_reset_proves_nothing(tmp_path, clock):
    db = str(tmp_path / "c.db")
    fake = FakePayPal(str(tmp_path / "pp2.json"))
    e1 = Engine(Store(db), fake, clock)
    e1.create_drop("Vases", lad.parse("3:50"), 3, 60, drop_id="d_same")
    e1.publish("d_same")
    Api(e1, togi_home=str(tmp_path / "togi")).join("d_same", "Linus", 0, sec())
    Store(db).reset()                                              # what `togi crowd test reset` does; the web db stays
    clock.advance(minutes=1)
    e2 = Engine(Store(db), fake, clock)
    e2.create_drop("Vases", lad.parse("3:50"), 3, 60, drop_id="d_same")
    e2.publish("d_same")
    e2.join("d_same", "Linus", 1)
    a2 = Api(e2, togi_home=str(tmp_path / "togi"))
    for call in (a2.me, a2.leave):
        with pytest.raises(ApiError):
            call("d_same", "linus", sec())
    assert e2.get("d_same").buyers["linus"].status == "held"


def test_a_refused_join_leaves_no_claim_behind(api, engine, fake, drop):
    fake.decline_next_authorize(1)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", 0, sec())
    assert e.value.code == "join_declined" and api.tokens.row(drop, "ada") is None
    engine.join(drop, "Ada", 3)                                    # later, the assistant adds an Ada
    with pytest.raises(ApiError):
        api.leave(drop, "ada", sec())
    assert engine.get(drop).buyers["ada"].status == "held"


def test_a_hold_whose_secret_cannot_be_recorded_is_released(api, engine, fake, drop, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(api.tokens, "bind", broken)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", 0, sec())
    b = engine.get(drop).buyers["ada"]
    assert e.value.code == "not_recorded" and b.status == "left" and fake.get(b.auth_id)["status"] == "VOIDED"
    assert engine.get(drop).count == 0


@pytest.mark.parametrize("name", ["\u202eevil", "\u200b\u200b", "a\u0007b", "Bo\u2066x", "$1.00 Bo", "Pay \u00a540", "   ", "",
                                  "x" * 61, None, 5, "\ud800abc"])
def test_names_that_hide_reorder_or_pose_as_money_are_refused(api, engine, drop, name):
    with pytest.raises(ApiError) as e:
        api.join(drop, name, 0, sec())
    assert e.value.code == "bad_buyer" and not engine.get(drop).buyers


def test_ordinary_names_are_fine(api, engine, drop):
    for i, n in enumerate(["Åsa Öberg", "O'Neil-Smith", "山田 太郎", "Ada L."]):
        api.join(drop, n, 0, sec(str(i)))
    assert engine.get(drop).count == 4


def test_a_malformed_approval_record_does_not_take_the_maker_view_down(api, drop, tmp_path):
    home = tmp_path / "togi"
    (home / "approvals").mkdir(parents=True)
    (home / "approvals" / "ap_bad1.json").write_text("[1, 2]")
    (home / "approvals" / "ap_bad2.json").write_text(json.dumps({"bundle": "crowd", "command": "drop settle", "args": "x"}))
    (home / "approvals" / "ap_bad3.json").write_text(json.dumps({"bundle": "crowd", "command": "drop settle",
                                                                 "args": {"drop": drop}, "expires": 5}))
    write_approval(home, args={"drop": drop, "expect": "void:1"})
    assert [a["id"] for a in api.maker(drop)["approvals"]] == ["ap_0123456789"]


# ---- the server's own limits ---------------------------------------------------------------------------------------

def test_a_short_maker_key_is_refused(api):
    with pytest.raises(ValueError):
        serve(api, "short", "127.0.0.1", 0)


def test_wrong_keys_are_slowed_then_stopped(http, drop, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)
    codes = [http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": f"wrong-key-{i:08d}"})[0] for i in range(22)]
    assert codes[:20] == [401] * 20 and codes[-1] == 429
    assert http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": "wrong-key-again-1234"})[0] == 429   # wrong keys stay stopped
    assert http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": KEY})[0] == 200      # the right key always works


def test_an_unknown_host_is_refused(http, drop):
    assert http(f"/api/drops/{drop}", headers={"Host": "evil.example:80"})[0] == 421
    assert http(f"/api/drops/{drop}", headers={"Host": f"localhost:{http.port}"})[0] == 200


def test_other_methods_answer_json_with_the_policy(http, drop):
    for verb in ("PUT", "DELETE", "OPTIONS", "PATCH"):
        st, h, body = http(f"/api/drops/{drop}", method=verb, raw=b"{}")
        assert st == 405 and json.loads(body)["ok"] is False and "script-src 'self'" in h["Content-Security-Policy"]


def test_a_stalled_request_is_cut_off(api, monkeypatch):
    import socket
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "READ_TIMEOUT", 1)
    httpd = serve(api, KEY, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"POST /api/drops/x/join HTTP/1.1\r\nHost: localhost:%d\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 100\r\n\r\n{" % port)
        s.settimeout(6)
        t0 = time.monotonic()
        while s.recv(4096):                  # whatever the server says, it must end the connection itself
            pass
        assert time.monotonic() - t0 < 4     # after READ_TIMEOUT (1 s here), not when the client gives up
        s.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---- the refuter's breaks, round 2: one join intent never becomes two holds; nobody releases a hold they did not place -----

class Died(BaseException):
    """What a killed process looks like to the code that was running: nothing catches it, nothing after it runs."""


def open_auths(fake) -> list[str]:
    """The holds PayPal (the fake) still has open, read from its own file."""
    st = json.loads(Path(fake.path).read_text())
    return sorted(k for k, a in st["auths"].items() if a["status"] in ("CREATED", "AUTHORIZED", "PENDING"))


def join_body(name, tag, card=0):
    return {"name": name, "card": card, "secret": sec(tag)}


def restarted(api, tmp_path):
    """The web server started again over the same stores."""
    return Api(api.engine, maker_name="Juniper", togi_home=str(tmp_path / "togi"))


# S1: a retry after a lost reply must come back as the same join, whatever the limiter or a 500 did meanwhile

def test_s1_a_retry_after_a_lost_reply_is_not_turned_away_by_the_join_limit(http, engine, fake, drop):
    st, _, _ = http(f"/api/drops/{drop}/join", body=join_body("Alex", "a"))          # the hold is placed, the reply is lost
    assert st == 200
    for _ in range(JOINS_PER_MINUTE * 2):                                             # invalid requests from this address ...
        assert http(f"/api/drops/{drop}/join", body={"name": "x", "card": 99, "secret": sec("z")})[0] == 400
    fill = [http(f"/api/drops/{drop}/join", body=join_body(f"Fill {i}", "f"))[0] for i in range(JOINS_PER_MINUTE)]
    assert fill[:JOINS_PER_MINUTE - 1] == [200] * (JOINS_PER_MINUTE - 1) and fill[-1] == 429     # ... never used the budget up
    st, _, body = http(f"/api/drops/{drop}/join", body=join_body("Alex", "a"))        # the page asks again, limit full
    assert st == 200 and json.loads(body)["data"]["again"] is True
    assert http(f"/api/drops/{drop}/join", body=join_body("Someone", "s"))[0] == 429  # a stranger is still held back
    assert engine.get(drop).count == JOINS_PER_MINUTE and len(open_auths(fake)) == JOINS_PER_MINUTE


def test_s1_the_limit_is_taken_only_by_a_request_that_is_valid_and_not_a_hand_back(api, engine, drop):
    asked = []
    admit = lambda: asked.append(1)                                                  # noqa: E731
    for name, card, secret in (("Bo", 99, sec("b")), ("Bo", 0, "short"), ("", 0, sec("b")), ("‮Bo", 0, sec("b"))):
        with pytest.raises(ApiError):
            api.join(drop, name, card, secret, admit)
    assert asked == []                                                                # an invalid request never counts
    api.join(drop, "Bo", 0, sec("b"), admit)
    assert asked == [1]
    assert api.join(drop, "Bo", 0, sec("b"), admit)["again"] is True
    assert asked == [1]                                                               # a hand-back never counts


def test_s1_a_reply_that_fails_after_the_hold_is_handed_back_on_the_retry(http, api, engine, fake, drop, monkeypatch):
    real, seen = api._mine, []

    def flaky(d, bid):
        if not seen:
            seen.append(1)
            raise RuntimeError("fails after the hold is placed and bound")
        return real(d, bid)
    monkeypatch.setattr(api, "_mine", flaky)
    assert http(f"/api/drops/{drop}/join", body=join_body("Alex", "a"))[0] == 500
    st, _, body = http(f"/api/drops/{drop}/join", body=join_body("Alex", "a"))
    assert st == 200 and json.loads(body)["data"]["again"] is True
    assert engine.get(drop).count == 1 and len(open_auths(fake)) == 1
    st, _, body = http(f"/api/drops/{drop}/join", body=join_body("Alex", "b"))      # another browser: refused, no second hold
    assert st == 409 and json.loads(body)["error"]["code"] == "already_joined" and len(open_auths(fake)) == 1


# S2: a crash between the hold and its binding

def test_s2_a_crash_between_the_hold_and_the_binding_is_never_guessed_into_a_proof(api, engine, fake, drop, tmp_path, monkeypatch):
    def die(*a, **k):
        raise Died("killed after the hold, before the bind")
    monkeypatch.setattr(api.tokens, "bind", die)
    with pytest.raises(Died):
        api.join(drop, "Bea", 0, sec("a"))
    api2 = restarted(api, tmp_path)
    assert api2.tokens.row(drop, "bea")["auth_id"] is None and engine.get(drop).buyers["bea"].status == "held"
    with pytest.raises(ApiError) as e:                                                # the page's retry, same secret
        api2.join(drop, "Bea", 0, sec("a"))
    assert e.value.status == 409 and e.value.code == "join_unconfirmed"
    assert "maker" in e.value.message and "another name" not in e.value.message
    for call in (api2.me, api2.leave):                                                # no proof, so no seeing and no releasing
        with pytest.raises(ApiError) as e:
            call(drop, "bea", sec("a"))
        assert e.value.code == "not_yours"
    with pytest.raises(ApiError) as e:                                                # someone else's secret: the name is taken
        api2.join(drop, "Bea", 0, sec("b"))
    assert e.value.code == "already_joined"
    assert engine.get(drop).count == 1 and len(open_auths(fake)) == 1                 # one hold, left for the maker to see


def test_s2_a_failed_binding_that_cannot_release_does_not_say_released(api, engine, fake, drop, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(api.tokens, "bind", broken)
    fake.unavailable("void")                                                          # PayPal cannot release it right now
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", 0, sec())
    assert e.value.code == "not_recorded_held" and "was released" not in e.value.message and "maker" in e.value.message
    assert "ada" in engine.get(drop).leaving                                          # the leave is on record for close and settle
    monkeypatch.setattr(api.engine, "leave", lambda *a, **k: (_ for _ in ()).throw(CrowdError("drop_ended", drop=drop)))
    with pytest.raises(ApiError) as e:                                                # the release is refused outright (too late)
        api.join(drop, "Bo", 0, sec("b"))
    assert e.value.code == "not_recorded_held" and "was released" not in e.value.message and "maker" in e.value.message


# S3: a second browser must not inherit a hold an unconfirmed join may have placed

def test_s3_a_second_browser_cannot_inherit_the_hold_of_an_unconfirmed_join(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "paypal_unavailable"
    mine = api.tokens.row(drop, "alex")
    assert mine["auth_id"] is None and len(open_auths(fake)) == 1                     # PayPal placed it; nobody wrote it down
    with pytest.raises(ApiError) as e:                                                # browser B, same name
        api.join(drop, "Alex", 0, sec("b"))
    assert e.value.status == 409 and e.value.code == "join_in_progress"
    assert api.tokens.row(drop, "alex") == mine                                       # A's claim is not overwritten
    got = api.join(drop, "Alex", 0, sec("a"))                                         # A's own retry completes it
    assert got["buyer"] == "alex" and engine.get(drop).count == 1 and len(open_auths(fake)) == 1
    for call in (api.me, api.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "alex", sec("b"))
        assert e.value.code == "not_yours"
    assert api.leave(drop, "alex", sec("a"))["me"]["status"] == "left"


def test_s3_a_hold_the_assistant_replays_is_not_the_web_buyers_to_release(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))
    engine.join(drop, "Alex", 0)                                                      # the assistant joins by name: a replay
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "join_unconfirmed"
    for call in (api.me, api.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "alex", sec("a"))
        assert e.value.code == "not_yours"
    assert engine.get(drop).buyers["alex"].status == "held"


def test_s3_a_web_buyer_cannot_take_over_a_join_the_assistant_left_unconfirmed(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(CrowdError):
        engine.join(drop, "Alex", 0)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("b"))
    assert e.value.code == "join_in_progress" and api.tokens.row(drop, "alex") is None


def test_s3_a_claim_never_replaces_the_proof_of_a_hold_still_there(tmp_path):
    t = Tokens(str(tmp_path / "t.db"))
    t.claim("d1", "ada", sec("a"), "c1", "now")
    assert t.bind("d1", "ada", sec("a"), "AUTH1") and not t.bind("d1", "ada", sec("a"), "AUTH2")   # a binding is final
    assert t.claim("d1", "ada", sec("b"), "c1", "now", hold_auth_id="AUTH1") is False
    assert t.owns("d1", "ada", sec("a"), "c1", "AUTH1")
    t.drop_claim("d1", "ada", sec("b"))
    t.drop_claim("d1", "ada", sec("a"))                                               # a refused join only forgets an UNBOUND claim
    assert t.owns("d1", "ada", sec("a"), "c1", "AUTH1")
    assert t.claim("d1", "ada", sec("b"), "c1", "now", hold_auth_id=None) is True     # the hold is gone: a new join may follow
    assert t.bind("d1", "ada", sec("b"), "AUTH1") and not t.owns("d1", "ada", sec("a"), "c1", "AUTH1")
    assert t.claim("d1", "ada", sec("c"), "c2", "now", hold_auth_id="AUTH1") is True  # another life of the drop: old proof is dead
    assert not t.owns("d1", "ada", sec("b"), "c1", "AUTH1") and t.claimed("d1", "ada", sec("c"), "c2")


# S4: after settlement a stranger's refused join must not erase a charged buyer's proof

def paid_ada(api, engine, drop):
    api.join(drop, "Ada", 0, sec("a"))
    sim(engine, drop, 4)
    settle(engine, drop)


def test_s4_a_strangers_refused_join_leaves_a_charged_buyers_proof_alone(api, engine, drop):
    paid_ada(api, engine, drop)
    before = api.tokens.row(drop, "ada")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", 0, sec("x"))
    assert e.value.code == "drop_closed" and api.tokens.row(drop, "ada") == before
    assert api.me(drop, "ada", sec("a"))["me"]["charged"] == "36.00"


def test_s4_a_charged_buyers_own_retry_gets_their_join_back(api, engine, drop):
    paid_ada(api, engine, drop)
    before = api.tokens.row(drop, "ada")
    again = api.join(drop, "Ada", 0, sec("a"))
    assert again["again"] is True and again["me"]["status"] == "captured" and again["me"]["charged"] == "36.00"
    assert api.tokens.row(drop, "ada") == before


def test_s4_a_closed_drop_refuses_a_join_before_any_claim_is_written(api, engine, drop, monkeypatch):
    def never(*a, **k):
        raise AssertionError("a claim was written for a drop that takes no joins")
    monkeypatch.setattr(api.tokens, "claim", never)
    engine.close(drop)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Zed", 0, sec("z"))
    assert e.value.code == "drop_closed"


def test_s4_a_refused_join_keeps_its_claim_only_while_the_engine_holds_an_open_intent(api, engine, fake, drop):
    fake.refuse("authorize", "UNPROCESSABLE_ENTITY")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Ada", 0, sec())
    assert e.value.code == "paypal_error" and e.value.extra.get("nothing_held") is True and api.tokens.row(drop, "ada") is None
    fake.lose_reply("authorize")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Bo", 0, sec("b"))
    assert e.value.code == "paypal_unavailable" and "nothing_held" not in e.value.extra and api.tokens.row(drop, "bo")


# the server's own limits

@pytest.mark.parametrize("phase", ["body", "headers"])
def test_a_slow_request_is_cut_off_at_a_total_deadline_not_a_per_read_one(api, monkeypatch, phase):
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "READ_TIMEOUT", 5)                  # every single read is fast enough ...
    monkeypatch.setattr(srv, "REQUEST_DEADLINE", 1.0)            # ... but the whole request may not take longer than this
    with running(api) as call:
        s = socket.create_connection(("127.0.0.1", call.port))
        if phase == "body":
            s.sendall(b"POST /api/drops/x/join HTTP/1.1\r\nHost: localhost:%d\r\nContent-Type: application/json\r\n"
                      b"Content-Length: 100\r\n\r\n" % call.port)
        else:
            s.sendall(b"GET /api/info HTTP/1.1\r\nHost: localhost:%d\r\nX-Pad: " % call.port)    # a header that never ends
        t0, closed, said = time.monotonic(), None, b""
        while time.monotonic() - t0 < 4:
            try:
                s.sendall(b" ")                                  # one byte every 0.2 s: never idle for READ_TIMEOUT
                s.settimeout(0.2)
                said = s.recv(4096)                              # the server's answer, or its closing the connection
                closed = time.monotonic() - t0
                break
            except socket.timeout:
                continue
            except OSError:
                closed = time.monotonic() - t0
                break
        s.close()
        assert closed is not None and closed < 3, "the server let a slow client keep its slot"
        assert said == b"" or b" 408 " in said.split(b"\r\n")[0]    # told "too slow" (or just cut off), never served


def test_one_address_cannot_hold_every_connection(api, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "MAX_CONNECTIONS_PER_ADDRESS", 3)
    monkeypatch.setattr(srv, "READ_TIMEOUT", 5)
    monkeypatch.setattr(srv, "REQUEST_DEADLINE", 5)
    with running(api) as call:
        idle = [socket.create_connection(("127.0.0.1", call.port)) for _ in range(3)]
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", call.port))
        extra.settimeout(2)
        try:
            assert extra.recv(1) == b""                          # turned away at once, not left waiting
        except ConnectionError:
            pass
        extra.close()
        for s in idle:
            s.settimeout(0.3)
            with pytest.raises(socket.timeout):                  # the first three are still being served
                s.recv(1)
        idle.pop().close()
        time.sleep(0.5)
        assert call("/api/info")[0] == 200                       # a slot freed: the address can ask again
        for s in idle:
            s.close()


def test_behind_a_proxy_the_right_most_forwarded_address_is_the_client(api, drop, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)
    with running(api, trust_proxy=True) as call:
        def wrong(i, real="203.0.113.9"):
            return call(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": f"wrong-key-{i:08d}",
                                                              "X-Forwarded-For": f"10.0.{i}.1, {real}"})[0]
        codes = [wrong(i) for i in range(22)]                    # the client rotates the left-most entry it wrote itself
        assert codes[:20] == [401] * 20 and codes[20:] == [429, 429]
        assert wrong(30, real="203.0.113.77") == 401             # a different client, as the proxy saw it, has its own budget
        assert call(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": KEY, "X-Forwarded-For": "1.1.1.1, 203.0.113.9"})[0] == 200


def test_the_right_key_always_works_after_a_flood_of_wrong_ones(http, drop, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv.time, "sleep", lambda s: None)
    codes = [http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": f"wrong-key-{i:08d}"})[0] for i in range(25)]
    assert codes[-1] == 429
    for _ in range(3):
        assert http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": KEY})[0] == 200
    assert http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": "wrong-key-after-a-right-one"})[0] == 429


# P2

def test_a_photo_is_served_only_for_a_published_drop_with_that_title(tmp_path, engine):
    from crowdweb.api import load_details
    (tmp_path / "details").mkdir()
    (tmp_path / "details" / "mugs.jpg").write_bytes(b"\xff\xd8\xff fake jpeg")
    (tmp_path / "details" / "d.json").write_text(json.dumps({"Speckled mugs": {"photo": "mugs.jpg"}, "Vases": {"photo": "mugs.jpg"}}))
    a = Api(engine, details=load_details(str(tmp_path / "details" / "d.json")), togi_home=str(tmp_path / "togi"))
    with pytest.raises(ApiError) as e:                           # no drop by that title yet
        a.photo("0")
    assert e.value.status == 404
    d = engine.create_drop("Speckled mugs", lad.parse("3:40"), 3, 60)
    with pytest.raises(ApiError):                                # a draft is not public, nor is its photo
        a.photo("0")
    engine.publish(d)
    assert a.photo("0")[1] == "image/jpeg"
    with pytest.raises(ApiError):                                # a title nobody has published
        a.photo("1")


def test_the_maker_key_is_printed_only_when_it_was_made_here(monkeypatch, capsys):
    import crowdweb.__main__ as cli

    class Httpd:
        def serve_forever(self):
            pass

        def server_close(self):
            pass

    class Stub:
        mode = "fake"
        engine = type("E", (), {"store": type("S", (), {"path": "/tmp/x.db"})()})()

        def drops(self):
            return []
    monkeypatch.setattr(cli, "Api", lambda **k: Stub())
    monkeypatch.setattr(cli, "serve", lambda api, key, *a, **k: Httpd())
    monkeypatch.delenv("CROWD_MAKER_KEY", raising=False)
    assert cli.main(["--port", "8799"]) == 0
    out = capsys.readouterr().out
    assert "#key=" in out                                        # made here, so it has to be shown once
    for argv, env in ((["--port", "8799", "--maker-key", "given-on-the-command-line"], None),
                      (["--port", "8799"], "given-in-the-environment")):
        if env:
            monkeypatch.setenv("CROWD_MAKER_KEY", env)
        assert cli.main(argv) == 0
        out = capsys.readouterr().out
        assert "given-on-the-command-line" not in out and "given-in-the-environment" not in out and "#key=" not in out
        assert "/maker" in out


# ---- drop.js itself: what the buyer's page does with its secret (run for real, in node, against a scripted server) ---------

# The page's own code runs unchanged in a node `vm` with a fake document and a scripted API: no browser, no network.
# A scenario is {store, persist, drop_life, drop_state, join: [answers], me: [answers], steps: [...]}; steps are ["tick"], ["advance", ms],
# ["submit", name], ["start", name] (a submit left waiting) with ["finish"], ["starttick"] (a poll left waiting) with ["finishtick"],
# ["click", button text], and ["tab", record] (another tab of the same browser writes the shared record; null erases it).
# An answer may carry a `delay` in ms.
DROP_JS_HARNESS = r'''
const fs = require("fs"), vm = require("vm");
const sc = JSON.parse(fs.readFileSync(0, "utf8"));
const src = fs.readFileSync(process.argv[2], "utf8");
const KEY = "crowd-price:d_test";
const persist = sc.persist !== false;
let now = 1000000, ticker = null, nsecret = 0, lastCard = [];
const mem = new Map();
if (sc.store) mem.set(KEY, JSON.stringify(sc.store));
const calls = [], forms = {}, joinQ = [...(sc.join || [])], meQ = [...(sc.me || [])];
const SETTLED = sc.drop_state === "settled";
const DROP = { drop: "d_test", title: "Mugs", state: sc.drop_state || "live", joinable: !sc.drop_state, price: "40.00", start_price: "40.00", minimum: 3,
  minimum_met: SETTLED, buyers: SETTLED ? 3 : 0, steps: [{ step: 0, from_buyers: 3, price: "40.00" }], locked_step: null, next_step: null,
  next_message: null, details: null, paypal: "fake", maker_name: "Juniper", settled: SETTLED ? { outcome: "funded", final_price: "40.00", paid: 3 } : null,
  saved_each: null, next_saving: null,
  ends_at: "2999-01-01T00:00:00Z", time_left_seconds: 100, server_time: "2026-11-01T12:00:00Z", life: sc.drop_life || "L1" };
const ME = { buyer: "alex", name: "Alex", status: "held", card: "Test card ending 4444", held: "40.00", charged: null, saved: null,
  start_price: "40.00", can_leave: true, leaving: false };

async function api(path, opts = {}) {
  if (path === "/api/cards") return [{ index: 0, label: "Test card ending 4444" }];
  const act = path.split("/").pop();
  calls.push({ act, body: opts.body });
  if (act === "join") {
    const r = joinQ.shift();
    if (r && r.delay) await new Promise(done => setTimeout(done, r.delay));
    if (!r) throw { code: "offline", message: "The drop could not be reached." };
    if (r.error) throw r.error;
    return { buyer: "alex", me: ME, drop: DROP, ...r.ok };
  }
  if (act === "me") {
    const r = meQ.shift() || {};
    if (r.delay) await new Promise(done => setTimeout(done, r.delay));
    if (r.error) throw r.error;
    return { me: ME, drop: DROP, ...r.ok };
  }
  if (act === "leave") return { me: { ...ME, status: "left", can_leave: false }, drop: DROP };
  return DROP;
}
function store(k, v) {
  if (!persist) return null;
  if (v === undefined) return JSON.parse(mem.get(k) || "null");
  if (v === null) mem.delete(k); else mem.set(k, JSON.stringify(v));
}
function el(tag, attrs, ...kids) {
  const n = { tag, attrs: attrs || {}, kids: kids.flat(Infinity) };
  if (n.attrs.onsubmit) forms.submit = n.attrs.onsubmit;
  if (n.attrs.id === "name") forms.name = n.attrs;
  return n;
}
const words = n => (typeof n === "string" ? [n] : n && n.kids ? [n.attrs.text, ...n.kids.flatMap(words)].filter(Boolean) : []);
const findButton = (nodes, text) => {
  for (const n of nodes) {
    if (!n || !n.attrs) continue;
    if (n.attrs.onclick && n.attrs.text === text) return n.attrs.onclick;
    const f = findButton(n.kids || [], text);
    if (f) return f;
  }
  return null;
};
const els = {};
function fake(id) {
  return { id, textContent: "", hidden: false, classList: { add() {}, remove() {}, toggle() {} }, style: {}, value: "",
    replaceChildren(...k) { if (id === "join") lastCard = k.flat(Infinity); }, getBoundingClientRect: () => ({ width: 700, top: 0, bottom: 100 }),
    contains: () => false, getAttribute: () => null, setAttribute() {}, addEventListener() {}, focus() {}, scrollIntoView() {} };
}
const document = { getElementById: id => els[id] || (els[id] = fake(id)), title: "", activeElement: null, querySelector: () => null,
  addEventListener() {}, hidden: false };
const CP = { el, api, store, secret: () => ("T" + ++nsecret).padEnd(43, "x"), showMode() {}, money: s => "$" + s, short: s => "$" + s,
  splitMoney: s => ({ whole: String(s).split(".")[0], cents: "." + (String(s).split(".")[1] || "00") }),
  plural: (n, a, b) => `${n} ${n === 1 ? a : b || a + "s"}`, clockFrom: () => () => 100, duration: s => s + "s", ladder() {},
  poll: t => { ticker = t; }, calm: () => true };
const ctx = { CP, document, window: { innerWidth: 1200, innerHeight: 800, addEventListener() {} }, location: { pathname: "/d/d_test", origin: "http://x" },
  navigator: {}, setTimeout, clearTimeout, setInterval: () => 0, Date: { now: () => now, parse: Date.parse }, console, JSON, Math, parseFloat, parseInt,
  String, Number, Object, Array, Set, Promise, Symbol };
vm.createContext(ctx);
vm.runInContext(src, ctx);

(async () => {
  while (!ticker) await new Promise(r => setTimeout(r, 1));
  const trace = [];
  let waiting = null, ticking = null;
  for (const [op, arg] of sc.steps) {
    if (op === "tick") await ticker();
    else if (op === "starttick") ticking = ticker();
    else if (op === "finishtick") await ticking;
    else if (op === "advance") now += arg;
    else if (op === "tab") { if (arg === null) mem.delete(KEY); else mem.set(KEY, JSON.stringify(arg)); }
    else if (op === "click") {
      const f = findButton(lastCard, arg);
      if (!f) throw new Error("no button " + arg + "; the card says: " + lastCard.flatMap(words).join(" | "));
      await f();
    }
    else if (op === "submit") { forms.name.oninput({ target: { value: arg } }); await forms.submit({ preventDefault() {} }); }
    else if (op === "start") { forms.name.oninput({ target: { value: arg } }); waiting = forms.submit({ preventDefault() {} }); }
    else if (op === "finish") await waiting;
    trace.push({ op, store: persist ? JSON.parse(mem.get(KEY) || "null") : null, said: lastCard.flatMap(words),
      joins: calls.filter(c => c.act === "join").length });
  }
  console.log(JSON.stringify({ trace, calls }));
})();
'''

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the page's own code")
SECRET_A = "S" * 43
PENDING = {"name": "Alex", "card": 0, "secret": SECRET_A, "pending": True}


def drop_js(tmp_path, **scenario) -> dict:
    harness = tmp_path / "drop_js_harness.js"
    harness.write_text(DROP_JS_HARNESS)
    run = subprocess.run(["node", str(harness), str(WEB / "static" / "drop.js")], input=json.dumps(scenario), text=True,
                         capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# The maker's view runs the same way: its own code, a scripted API, and what it draws read back as rows of words.
MAKER_JS_HARNESS = r'''
const fs = require("fs"), vm = require("vm");
const sc = JSON.parse(fs.readFileSync(0, "utf8"));
const src = fs.readFileSync(process.argv[2], "utf8");
let drawn = [];
const app = { replaceChildren(...k) { drawn = k.flat(Infinity); } };
function el(tag, attrs, ...kids) { return { tag, attrs: attrs || {}, kids: kids.flat(Infinity) }; }
const words = n => (typeof n === "string" ? [n] : n && n.kids ? [n.attrs.text, ...n.kids.flatMap(words)].filter(Boolean) : []);
const find = (n, test, out = []) => { if (n && n.attrs) { if (test(n)) out.push(n); n.kids.forEach(k => find(k, test, out)); } return out; };
const stub = { id: "x", textContent: "", hidden: false, style: {}, getAttribute: () => null, setAttribute() {}, replaceChildren() {} };
async function api(path) {
  if (path === "/api/info") return { paypal: "fake" };
  if (path === "/api/drops") return [{ drop: "d_test", title: "Mugs", state: sc.maker.state }];
  return sc.maker;
}
const CP = { el, api, showMode() {}, money: s => "$" + s, plural: (n, a, b) => `${n} ${n === 1 ? a : b || a + "s"}`, clockFrom: () => () => 100,
  duration: s => s + "s", hhmm: s => s, ladder() {}, store: (k, v) => (v === undefined ? "a-maker-key" : null) };
const ctx = { CP, document: { getElementById: id => (id === "app" ? app : null), title: "" }, window: { addEventListener() {} },
  location: { hash: "#drop=d_test", pathname: "/maker" }, history: { replaceState() {} }, sessionStorage: {}, URLSearchParams,
  requestAnimationFrame: f => f(), setTimeout, clearTimeout, setInterval: () => 0, console, JSON, Math, Object, Array, Set, Promise, Date, String, Number };
vm.createContext(ctx);
vm.runInContext(src, ctx);
setTimeout(() => {
  const rows = drawn.flatMap(n => find(n, x => x.attrs.class === "buyers")).flatMap(ul => ul.kids.map(li => li.kids.flatMap(words)));
  const counts = drawn.flatMap(n => find(n, x => x.attrs.class === "panel buyers-panel")).flatMap(p => find(p, x => x.attrs.class === "counts")).flatMap(words);
  console.log(JSON.stringify({ rows, counts }));
  process.exit(0);
}, 150);
'''


def maker_js(tmp_path, maker: dict) -> dict:
    harness = tmp_path / "maker_js_harness.js"
    harness.write_text(MAKER_JS_HARNESS)
    run = subprocess.run(["node", str(harness), str(WEB / "static" / "maker.js")], input=json.dumps({"maker": maker}), text=True,
                         capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def err(code, message="Words from the server.", **extra):
    return {"error": {"code": code, "message": message, **extra}}


@needs_node
@pytest.mark.parametrize("code", ["slow_down", "internal", "offline", "paypal_unavailable", "earlier_attempt_unresolved", "join_unconfirmed",
                                  "not_recorded_held", "paypal_error", "error", "http_502", "join_in_progress", "already_joined", "busy",
                                  "drop_closed", "drop_ended"])
def test_the_page_keeps_its_secret_on_every_answer_that_does_not_prove_nothing_was_held(tmp_path, code):
    out = drop_js(tmp_path, store=PENDING, join=[err(code)], steps=[["tick"]])
    last = out["trace"][-1]
    assert last["store"] == PENDING, code                                  # still the same secret, still pending
    assert not any("Join the drop" in w for w in last["said"])             # and no form to type another name into


@needs_node
@pytest.mark.parametrize("answer", [err("bad_card"), err("bad_buyer"),           # (refused before any claim exists: nothing can be held)
                                    err("join_declined", nothing_held=True), err("hold_not_open", nothing_held=True),
                                    err("not_recorded", nothing_held=True), err("wrong_paypal", nothing_held=True),
                                    err("paypal_error", nothing_held=True), err("drop_closed", nothing_held=True),
                                    err("drop_ended", nothing_held=True), err("already_joined", nothing_held=True),
                                    err("join_stale", nothing_held=True), err("join_over", nothing_held=True)])
def test_the_page_forgets_its_secret_only_when_nothing_was_held(tmp_path, answer):
    out = drop_js(tmp_path, store=PENDING, join=[answer], steps=[["tick"]])
    assert out["trace"][-1]["store"] is None
    assert any("Words from the server." in w for w in out["trace"][-1]["said"])


@needs_node
def test_s1_the_page_asks_again_with_the_same_secret_after_a_lost_reply_and_a_429(tmp_path):
    out = drop_js(tmp_path, store=PENDING, join=[err("slow_down"), err("internal"), {"ok": {"again": True}}],
                  steps=[["tick"], ["tick"], ["advance", 4000], ["tick"], ["advance", 8000], ["tick"]])
    joins = [c["body"] for c in out["calls"] if c["act"] == "join"]
    assert len(joins) == 3 and all(b["secret"] == SECRET_A and b["name"] == "Alex" for b in joins)   # same secret, same name, every time
    assert [t["store"] for t in out["trace"][:4]] == [PENDING] * 4                                 # kept through the 429 and the 500
    assert out["trace"][-1]["store"] == {"buyer": "alex", "secret": SECRET_A}                      # then the join is handed back


@needs_node
def test_s1_a_retry_is_not_asked_again_before_its_wait_is_over(tmp_path):
    out = drop_js(tmp_path, store=PENDING, join=[err("slow_down")], steps=[["tick"], ["tick"], ["tick"]])
    assert [c["act"] for c in out["calls"]].count("join") == 1


@needs_node
def test_s1_a_retry_that_finds_the_name_taken_never_suggests_another_name(tmp_path):
    taken = "That name is taken in this drop. Try another, for example with an initial."
    out = drop_js(tmp_path, store=PENDING, join=[err("already_joined", taken)], steps=[["tick"]])
    last = out["trace"][-1]
    assert last["store"] == PENDING
    said = " ".join(last["said"])
    assert "may have gone through" in said and "Try another" not in said and "another name" in said      # "don't join under another name"
    assert "keeps checking" in said


@needs_node
def test_a_first_ask_that_finds_the_name_taken_says_so_and_lets_go(tmp_path):
    taken = "That name is taken in this drop. Try another, for example with an initial."
    out = drop_js(tmp_path, join=[err("already_joined", taken)], steps=[["tick"], ["submit", "Alex"]])
    last = out["trace"][-1]
    assert last["store"] is None and taken in last["said"]                 # nothing of this secret was ever sent before: really taken


@needs_node
def test_a_first_ask_whose_answer_is_lost_is_kept_and_asked_again(tmp_path):
    out = drop_js(tmp_path, join=[err("offline"), {"ok": {"again": True}}], steps=[["tick"], ["submit", "Alex"], ["advance", 5000], ["tick"]])
    joins = [c["body"] for c in out["calls"] if c["act"] == "join"]
    assert len(joins) == 2 and joins[0]["secret"] == joins[1]["secret"] and out["trace"][2]["store"]["pending"] is True
    assert out["trace"][-1]["store"] == {"buyer": "alex", "secret": joins[0]["secret"]}


@needs_node
@pytest.mark.parametrize("other", [{"buyer": "alex", "secret": SECRET_A}, PENDING])
def test_a_second_tab_cannot_overwrite_a_stored_join_with_a_new_pending_one(tmp_path, other):
    out = drop_js(tmp_path, steps=[["tick"], ["tab", other], ["submit", "Zed"]], join=[err("offline")])
    assert not any(c["act"] == "join" and c["body"]["name"] == "Zed" for c in out["calls"])           # no new secret was ever sent
    assert out["trace"][-1]["store"] == other                                                         # and the stored one is untouched


@needs_node
def test_a_browser_that_will_not_keep_the_record_still_keeps_the_secret_while_the_page_is_open(tmp_path):
    out = drop_js(tmp_path, persist=False, join=[err("offline"), {"ok": {"again": True}}],
                  steps=[["tick"], ["submit", "Alex"], ["advance", 5000], ["tick"]])
    joins = [c["body"] for c in out["calls"] if c["act"] == "join"]
    assert len(joins) == 2 and joins[0]["secret"] == joins[1]["secret"]
    assert any("Confirming your join" in w for w in out["trace"][1]["said"])                          # no form to type another name into


# ---- round 3: narrower paths to the same two failures (two holds from one intent; a hold released by someone who did not place it) ----

def die_after_the_hold(api, monkeypatch):
    def die(*a, **k):
        raise Died("killed after the hold, before the bind")
    monkeypatch.setattr(api.tokens, "bind", die)


def charge(engine, drop):
    from crowd.engine import plan_token
    engine.settle(drop, plan_token(engine.plan(drop)))


def test_r3_s2_a_buyer_charged_after_a_crash_is_told_so_never_that_they_were_not_in(api, engine, fake, drop, tmp_path, monkeypatch):
    die_after_the_hold(api, monkeypatch)
    with pytest.raises(Died):
        api.join(drop, "Bea", 0, sec("a"))
    api2 = restarted(api, tmp_path)
    sim(engine, drop, 3)
    engine.close(drop)
    with pytest.raises(ApiError) as e:                                       # closed, the hold still there
        api2.join(drop, "Bea", 0, sec("a"))
    assert e.value.code == "join_unconfirmed" and "nothing_held" not in e.value.extra
    charge(engine, drop)
    assert engine.get(drop).buyers["bea"].status == "captured"
    with pytest.raises(ApiError) as e:                                       # settled: she was charged
        api2.join(drop, "Bea", 0, sec("a"))
    assert e.value.code == "join_unconfirmed" and "nothing_held" not in e.value.extra
    assert "$40.00" in e.value.message and "charged" in e.value.message and "refund" in e.value.message
    for call in (api2.me, api2.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "bea", sec("a"))
        assert e.value.code == "not_yours"


def test_r3_s2_a_hold_the_assistant_adopted_and_settlement_charged_is_still_told_to_the_page(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))
    engine.join(drop, "Alex", 0)                                             # the assistant's join replays A's request
    sim(engine, drop, 3)
    engine.close(drop)
    charge(engine, drop)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "join_unconfirmed" and "charged" in e.value.message


def test_r3_s2_the_maker_view_marks_a_hold_that_sits_beside_an_unbound_claim(api, engine, drop, monkeypatch):
    api.join(drop, "Ada", 0, sec("a"))                                       # an ordinary join, bound
    die_after_the_hold(api, monkeypatch)
    with pytest.raises(Died):
        api.join(drop, "Bea", 0, sec("b"))
    rows = {r["name"]: r for r in api.maker(drop)["buyers_list"]}
    assert rows["Ada"]["unconfirmed_web_join"] is False
    assert rows["Bea (unconfirmed web join)"]["unconfirmed_web_join"] is True and rows["Bea (unconfirmed web join)"]["status"] == "held"


# S3 through the assistant: the join the claim started is not the join that is open now

def test_r3_s3_a_join_the_assistant_opens_after_a_claim_is_not_the_claims_to_inherit(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))                                  # intent 1 open; A's claim unbound, for attempt 1
    engine.join(drop, "Alex", 0)                                             # replays intent 1 and adopts its hold
    engine.leave(drop, "alex")
    fake.lose_reply("authorize")
    with pytest.raises(CrowdError):
        engine.join(drop, "Alex", 0)                                         # the assistant's OWN new join, its reply lost (attempt 2)
    mine = api.tokens.row(drop, "alex")
    assert mine["attempt"] == 1 and [j["attempt"] for j in engine.get(drop).unresolved.values()] == [2]
    for who in ("a", "b"):
        with pytest.raises(ApiError) as e:
            api.join(drop, "Alex", 0, sec(who))
        assert e.value.code == "join_in_progress"
    assert api.tokens.row(drop, "alex") == mine and len(open_auths(fake)) == 1        # only the assistant's hold; nobody inherited it
    for call in (api.me, api.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "alex", sec("a"))
        assert e.value.code == "not_yours"
    engine.reconcile(drop)                                                   # the maker sorts the assistant's join out: released
    with pytest.raises(ApiError) as e:                                       # A's own attempt ended long ago (the assistant adopted it and
        api.join(drop, "Alex", 0, sec("a"))                                  # left): it never starts another join, however it is asked
    assert e.value.code == "join_over" and e.value.extra["nothing_held"] is True and open_auths(fake) == []
    got = api.join(drop, "Alex", 0, sec("c"))                                # the buyer asks again, with a new secret
    assert got["buyer"] == "alex" and len(open_auths(fake)) == 1 and api.leave(drop, "alex", sec("c"))["me"]["status"] == "left"


def test_r3_s3_a_claim_from_before_attempts_were_recorded_cannot_pass_the_guard(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))
    with contextlib.closing(sqlite3.connect(api.tokens.path)) as c:
        c.execute("UPDATE buyer_secrets SET attempt = NULL")
        c.commit()
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "join_in_progress" and len(open_auths(fake)) == 1


def test_r3_a_claims_table_from_before_the_attempt_column_is_upgraded(tmp_path):
    path = str(tmp_path / "old.web.db")
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("CREATE TABLE buyer_secrets (drop_id TEXT NOT NULL, buyer TEXT NOT NULL, secret_sha256 TEXT NOT NULL, "
                  "drop_created TEXT NOT NULL, auth_id TEXT, created TEXT NOT NULL, PRIMARY KEY (drop_id, buyer))")
        c.execute("INSERT INTO buyer_secrets VALUES ('d', 'ada', ?, 'c1', 'AUTH1', 'now')", (digest(sec("a")),))
        c.commit()
    t = Tokens(path)
    assert t.owns("d", "ada", sec("a"), "c1", "AUTH1") and t.row("d", "ada")["attempt"] is None
    assert t.claim("d", "bo", sec("b"), "c1", "now", attempt=2) and t.claim_of("d", "bo", sec("b"), "c1")["attempt"] == 2


# R2: a secret that never had anything is told so

def test_r3_r2_a_secret_with_no_claim_is_told_nothing_was_held_when_the_name_is_taken(api, engine, drop):
    def full():
        raise ApiError(429, "slow_down", "Too many joins right now. Wait a minute.")
    with pytest.raises(ApiError) as e:                                       # A's first ask is turned away by the limit
        api.join(drop, "Alex", 0, sec("a"), full)
    assert e.value.code == "slow_down" and api.tokens.row(drop, "alex") is None
    api.join(drop, "Alex", 2, sec("b"))                                      # B takes the name
    for _ in range(2):
        with pytest.raises(ApiError) as e:                                   # A's retries: let go, say it is taken
            api.join(drop, "Alex", 0, sec("a"))
        assert e.value.code == "already_joined" and e.value.extra["nothing_held"] is True and "taken" in e.value.message


def test_r3_r2_a_refusal_beside_this_secrets_own_open_join_does_not_say_nothing_was_held(api, engine, fake, drop, clock):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))                                  # A's claim, and a join PayPal may have acted on
    with pytest.raises(ApiError) as e:                                       # someone else's secret: that one holds nothing
        api.join(drop, "Alex", 0, sec("b"))
    assert e.value.code == "join_in_progress" and e.value.extra["nothing_held"] is True
    clock.advance(minutes=61)                                                # the deadline passes with A's join still open
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "drop_ended" and "will be released" in e.value.message and "nothing_held" not in e.value.extra
    assert len(open_auths(fake)) == 1
    engine.close(drop)                                                       # the maker closes: every unconfirmed join is released (D-11)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "drop_closed" and e.value.extra["nothing_held"] is True and open_auths(fake) == []


# R3: a secret from an earlier life of the drop id

def test_r3_r3_a_secret_from_an_earlier_life_of_the_drop_is_told_it_is_stale(tmp_path, clock):
    db, fake = str(tmp_path / "c.db"), FakePayPal(str(tmp_path / "pp3.json"))
    e1 = Engine(Store(db), fake, clock)
    e1.create_drop("Vases", lad.parse("3:50"), 3, 60, drop_id="d_same")
    e1.publish("d_same")
    a1 = Api(e1, togi_home=str(tmp_path / "togi"))
    life1 = a1.drop("d_same")["life"]
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        a1.join("d_same", "Linus", 0, sec())                                 # the page keeps {pending, secret}
    Store(db).reset()                                                        # what `togi crowd test reset` does; the web db stays
    clock.advance(minutes=1)
    e2 = Engine(Store(db), fake, clock)
    e2.create_drop("Vases", lad.parse("3:50"), 3, 60, drop_id="d_same")
    e2.publish("d_same")
    a2 = Api(e2, togi_home=str(tmp_path / "togi"))
    assert a2.drop("d_same")["life"] != life1                                # the page can tell the two lives apart
    with pytest.raises(ApiError) as e:
        a2.join("d_same", "Linus", 0, sec())
    assert e.value.code == "join_stale" and e.value.extra["nothing_held"] is True
    assert not e2.get("d_same").buyers and not e2.store.events("d_same")[2:]       # nothing was joined in the new life


# the server: waiting for a turn is the server's queue, not the client's connection

def test_r3_a_join_waiting_too_long_for_the_drops_turn_answers_503_and_writes_nothing(api, engine, drop, monkeypatch):
    import crowdweb.api as capi
    monkeypatch.setattr(capi, "LOCK_WAIT", 0.3)
    api.join(drop, "Ada", 0, sec("a"))                                       # a buyer who owns a hold: her leave has to wait its turn
    lock = api._lock(drop)
    lock.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(ApiError) as e:
            api.join(drop, "Zed", 0, sec("z"))
        assert e.value.status == 503 and e.value.code == "busy" and time.monotonic() - t0 < 3
        with pytest.raises(ApiError) as e:
            api.leave(drop, "ada", sec("a"))
        assert e.value.status == 503 and e.value.code == "busy"
    finally:
        lock.release()
    assert api.tokens.row(drop, "zed") is None and engine.get(drop).buyers["ada"].status == "held"


# drop.js: the narrow paths

@needs_node
def test_r3_the_poll_does_not_send_a_second_join_while_the_first_is_waiting(tmp_path):
    out = drop_js(tmp_path, join=[{"ok": {}, "delay": 300}], steps=[["tick"], ["start", "Alex"], ["tick"], ["finish"]])
    assert out["trace"][2]["joins"] == 1 and out["trace"][-1]["joins"] == 1          # the poll waited for the click's own ask
    assert out["trace"][-1]["store"]["buyer"] == "alex"


@needs_node
@pytest.mark.parametrize("code", ["join_unconfirmed", "join_in_progress", "already_joined"])
def test_r3_a_stuck_page_asks_again_about_once_a_minute(tmp_path, code):
    out = drop_js(tmp_path, store=PENDING, join=[err(code), err(code)],
                  steps=[["tick"], ["advance", 30000], ["tick"], ["advance", 31000], ["tick"]])
    assert [t["joins"] for t in out["trace"] if t["op"] == "tick"] == [1, 1, 2]
    assert out["trace"][-1]["store"] == PENDING


@needs_node
def test_r3_a_pending_record_from_another_life_of_the_drop_is_dropped_not_joined(tmp_path):
    out = drop_js(tmp_path, store={**PENDING, "life": "L0"}, drop_life="L1", join=[{"ok": {}}], steps=[["tick"]])
    assert out["trace"][-1]["store"] is None and out["trace"][-1]["joins"] == 0
    out = drop_js(tmp_path, store={**PENDING, "life": "L1"}, drop_life="L1", join=[{"ok": {}}], steps=[["tick"]])
    assert out["trace"][-1]["joins"] == 1                                    # the same life carries on
    out = drop_js(tmp_path, join=[err("offline")], drop_life="L7", steps=[["tick"], ["submit", "Alex"]])
    assert out["trace"][-1]["store"]["life"] == "L7"                         # and a new ask remembers which life it was made in


@needs_node
def test_r3_a_join_that_may_be_charged_or_held_is_never_wrapped_in_nothing_was_held(tmp_path):
    charged = "Your earlier try may have been charged $40.00. This page never confirmed it. The maker can see the charge and refund it."
    out = drop_js(tmp_path, store=PENDING, join=[err("join_unconfirmed", charged)], steps=[["tick"]])
    said = " ".join(out["trace"][-1]["said"])
    assert charged in said and "We could not confirm your join" in said and "nothing was held" not in said
    assert out["trace"][-1]["store"] == PENDING
    ended = "Joining has closed. If a hold was placed it will be released, and you will not be charged."
    out = drop_js(tmp_path, store=PENDING, join=[err("drop_ended", ended)], steps=[["tick"]])
    said = " ".join(out["trace"][-1]["said"])
    assert ended in said and "nothing was held" not in said and out["trace"][-1]["store"] == PENDING


# ---- round 4: a queue is made of connections too; a claim is a record of ONE attempt, read from the engine's log ----------------------
#
# The refuter's probes (scratchpad r4/probes) are the originals of these: p4/p6/p7 (finding 1), p1 (2), twotabs.js (3), p9/p3 (4),
# p2 (5), p8 (6), and the stuck page's minute-by-minute retry (7).

def post_status(port, path, body, source_port=None, timeout=15):
    """One POST, as its status or the name of what went wrong (a connection the server turned away raises)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout, source_address=("127.0.0.1", source_port) if source_port else None)
    try:
        c.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        r.read()
        return r.status
    except Exception as e:  # noqa: BLE001
        return type(e).__name__
    finally:
        c.close()


def free_ports(n):
    held = []
    for _ in range(n):
        sk = socket.socket()
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind(("127.0.0.1", 0))
        held.append(sk)
    ports = [sk.getsockname()[1] for sk in held]
    for sk in held:
        sk.close()
    return ports


# Finding 1 (P1): one address must not be able to use up every slot with requests that are waiting for the drop's turn

def test_r4_a_leave_that_is_nobodys_is_refused_before_it_waits_for_the_drops_turn(api, drop, monkeypatch):
    import crowdweb.api as capi
    import crowdweb.server as srv
    monkeypatch.setattr(capi, "LOCK_WAIT", 1.5)
    monkeypatch.setattr(srv, "MAX_CONNECTIONS_PER_ADDRESS", 64)              # (this is about waiting, not about how many connect at once)
    api.join(drop, "Ada", 0, sec("a"))
    lock, results = api._lock(drop), []
    with running(api) as call:
        lock.acquire()                                                       # one slow PayPal call (or the assistant's close) has the turn
        try:
            t0 = time.monotonic()
            threads = [threading.Thread(target=lambda i=i: results.append(post_status(call.port, f"/api/drops/{drop}/leave",
                                                                                     {"buyer": ["x", "ada"][i % 2], "secret": sec("z")})))
                       for i in range(40)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            assert time.monotonic() - t0 < 1.2, "a leave that is nobody's waited for the drop's turn"
            assert set(results) == {403}                                     # not_yours, at once
            assert call("/api/info")[0] == 200
        finally:
            lock.release()


def test_r4_a_join_the_limit_would_turn_away_does_not_wait_for_the_drops_turn(http, api, drop, monkeypatch):
    import crowdweb.api as capi
    monkeypatch.setattr(capi, "LOCK_WAIT", 1.5)
    for i in range(JOINS_PER_MINUTE):
        assert http(f"/api/drops/{drop}/join", body=join_body(f"Fill {i}", "f"))[0] == 200
    lock = api._lock(drop)
    lock.acquire()
    try:
        t0 = time.monotonic()
        st, _, body = http(f"/api/drops/{drop}/join", body=join_body("Someone", "s"))
        assert st == 429 and json.loads(body)["error"]["code"] == "slow_down" and time.monotonic() - t0 < 1.0
        st, _, body = http(f"/api/drops/{drop}/join", body=join_body("Fill 0", "f"))      # a retry of a join that went through: still handed back
        assert st == 200 and json.loads(body)["data"]["again"] is True
    finally:
        lock.release()


@pytest.mark.parametrize("kind", ["leave", "join"])
def test_r4_requests_waiting_for_the_drops_turn_count_against_their_address_and_cannot_shut_out_another(api, drop, monkeypatch, kind):
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "MAX_CONNECTIONS", 6)
    monkeypatch.setattr(srv, "MAX_CONNECTIONS_PER_ADDRESS", 3)
    api.join(drop, "Ada", 0, sec("a"))                                       # she owns a hold, so her leaves are real ones
    victim_ports = set(free_ports(3))
    httpd = serve(api, KEY, "127.0.0.1", 0)
    orig = httpd.process_request
    httpd.process_request = lambda request, addr: orig(request, ("10.9.9.9", addr[1]) if addr[1] in victim_ports else addr)   # the visitor's address
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    lock, results = api._lock(drop), []
    path, bodies = ((f"/api/drops/{drop}/leave", [{"buyer": "ada", "secret": sec("a")}] * 6) if kind == "leave"
                    else (f"/api/drops/{drop}/join", [join_body(f"Flood {i}", "f", i % 8) for i in range(6)]))
    threads = [threading.Thread(target=lambda b=b: results.append(post_status(port, path, b))) for b in bodies]
    lock.acquire()                                                           # stands in for one slow PayPal call
    try:
        for t in threads:
            t.start()
            time.sleep(0.02)
        time.sleep(0.6)
        assert httpd._open == {"127.0.0.1": 3}, httpd._open                  # three wait in the queue, and are counted ...
        assert httpd._slots._value == 6 - 3                                  # ... the other three were turned away at the door
        for sp in victim_ports:                                              # another address is served, and so is the maker
            assert post_status(port, "/api/drops/nowhere/me", {"buyer": "x", "secret": sec()}, source_port=sp) == 404
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5, source_address=("127.0.0.1", next(iter(victim_ports))))
        c.request("GET", f"/api/maker/drops/{drop}", headers={"X-Maker-Key": KEY, "Host": f"127.0.0.1:{port}"})
        assert c.getresponse().status == 200
        c.close()
    finally:
        lock.release()
        for t in threads:
            t.join(15)
        httpd.shutdown()
        httpd.server_close()
    served = [r for r in results if isinstance(r, int)]
    assert len(served) == 3 and len(results) == 6, results                   # three were served, once the turn came; three never were
    assert httpd._open == {} and httpd._slots._value == 6                    # and everything is given back


def test_r4_a_request_refused_before_the_turn_gives_its_place_back(api, drop, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "MAX_CONNECTIONS_PER_ADDRESS", 2)
    with running(api) as call:
        for _ in range(8):                                                   # more than the address's share, one after the other
            assert call(f"/api/drops/{drop}/leave", body={"buyer": "x", "secret": sec("z")})[0] == 403
            assert call(f"/api/drops/{drop}/join", body={"name": "x", "card": 99, "secret": sec("z")})[0] == 400
        assert call.httpd._open == {} and call.httpd._slots._value == srv.MAX_CONNECTIONS


# Finding 2 (P1): a hold the assistant placed is not the web buyer's, whatever the web claim says

def test_r4_a_hold_the_assistant_placed_on_another_card_is_not_labelled_or_blamed_on_the_web_buyer(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))                                  # the web's join; its reply is lost
    assert e.value.code == "paypal_unavailable"
    web_hold = open_auths(fake)
    engine.join(drop, "Alex", 3)                                             # the assistant, on card 3: the engine releases the web's hold
    assert len(open_auths(fake)) == 1 and open_auths(fake) != web_hold       # ... and places its own
    rows = api.maker(drop)["buyers_list"]
    assert [(r["name"], r["unconfirmed_web_join"]) for r in rows] == [("Alex", False)]
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))                                  # the web page asks again
    assert e.value.code == "already_joined" and e.value.extra["nothing_held"] is True and "charged" not in e.value.message
    sim(engine, drop, 3)
    settle(engine, drop)
    rows = api.maker(drop)["buyers_list"]
    assert [r["unconfirmed_web_join"] for r in rows if r["name"].startswith("Alex")] == [False]
    assert [r["name"] for r in rows if r["status"] == "captured" and "Alex" in r["name"]] == ["Alex"]
    with pytest.raises(ApiError) as e:                                       # after settlement: never "may have been charged"
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "drop_closed" and e.value.extra["nothing_held"] is True and "charged" not in e.value.message


# Finding 4 (P0, rare): a claim that never reached the engine must not inherit a join made much later by someone else

def test_r4_a_claim_the_engine_never_heard_of_cannot_inherit_a_much_later_assistant_join(api, engine, fake, drop, clock, tmp_path, monkeypatch):
    def killed(*a, **k):
        raise Died("killed after the claim, before the engine heard of it")
    with monkeypatch.context() as mp:
        mp.setattr(api.engine, "join", killed)
        with pytest.raises(Died):
            api.join(drop, "Alex", 0, sec("a"))
    mine = api.tokens.row(drop, "alex")
    assert mine["attempt"] == 1 and mine["auth_id"] is None and not engine.get(drop).unresolved
    api2 = restarted(api, tmp_path)
    clock.advance(minutes=20)
    fake.lose_reply("authorize")
    with pytest.raises(CrowdError):
        engine.join(drop, "Alex", 0)                                         # the assistant's own join, much later; its reply is lost
    held = open_auths(fake)
    assert len(held) == 1
    clock.advance(minutes=1)
    with pytest.raises(ApiError) as e:                                       # the page comes back: this open join is not its own
        api2.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "join_in_progress" and e.value.extra["nothing_held"] is True
    for call in (api2.me, api2.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "alex", sec("a"))
        assert e.value.code == "not_yours"
    assert open_auths(fake) == held                                          # nobody released the assistant's hold
    engine.join(drop, "Alex", 0)                                             # the assistant tries again and is handed its hold
    assert engine.get(drop).buyers["alex"].status == "held"
    assert [r["unconfirmed_web_join"] for r in api2.maker(drop)["buyers_list"]] == [False]     # and it is not labelled the web's
    with pytest.raises(ApiError) as e:
        api2.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "already_joined" and e.value.extra["nothing_held"] is True
    assert open_auths(fake) == held


def test_r4_an_error_the_engine_did_not_name_forgets_the_claim_only_if_the_engine_never_heard_of_the_try(api, engine, fake, drop, monkeypatch):
    real = api.engine.join

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(api.engine, "join", locked)
    with pytest.raises(sqlite3.OperationalError):
        api.join(drop, "Bea", 0, sec("b"))
    assert api.tokens.row(drop, "bea") is None                               # nothing reached the engine: nothing to remember

    def after_the_engine_wrote_it_down(*a, **k):
        fake.lose_reply("authorize")
        with pytest.raises(CrowdError):
            real(*a, **k)
        raise RuntimeError("something else fails after the engine has the join open")
    monkeypatch.setattr(api.engine, "join", after_the_engine_wrote_it_down)
    with pytest.raises(RuntimeError):
        api.join(drop, "Cy", 0, sec("c"))
    assert api.tokens.row(drop, "cy") is not None and len(open_auths(fake)) == 1     # the open join is the claim's: it stays
    monkeypatch.setattr(api.engine, "join", real)
    got = api.join(drop, "Cy", 0, sec("c"))                                  # and the page's retry completes it
    assert got["buyer"] == "cy" and len(open_auths(fake)) == 1


def test_r4_a_join_the_assistant_opens_between_the_claim_and_the_engine_is_not_inherited(api, engine, fake, drop, monkeypatch):
    orig = api.tokens.claim

    def claim_then_assistant(*a, **k):
        ok = orig(*a, **k)
        fake.lose_reply("authorize")
        with pytest.raises(CrowdError):
            engine.join(drop, "Alex", 0)                                     # lands after the web's first look, before the engine is asked
        return ok
    monkeypatch.setattr(api.tokens, "claim", claim_then_assistant)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "busy" and api.tokens.row(drop, "alex") is None   # not started, nothing remembered
    monkeypatch.setattr(api.tokens, "claim", orig)
    held = open_auths(fake)
    assert len(held) == 1 and len(engine.get(drop).unresolved) == 1
    for call in (api.me, api.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "alex", sec("a"))
        assert e.value.code == "not_yours"
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "join_in_progress" and e.value.extra["nothing_held"] is True and open_auths(fake) == held


def test_r4_a_hold_the_assistant_and_the_web_asked_for_at_once_is_not_bound_to_the_web_secret(api, engine, fake, drop, monkeypatch):
    real = api.engine.join

    def assistant_lands_in_the_gap(*a, **k):
        fake.lose_reply("authorize")
        with pytest.raises(CrowdError):
            real(drop, "Alex", 0)                                            # after the web's last look, before the engine's lock
        return real(*a, **k)                                                 # the web's own ask replays it: one hold for two askers
    monkeypatch.setattr(api.engine, "join", assistant_lands_in_the_gap)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "join_unconfirmed"
    assert api.tokens.row(drop, "alex")["auth_id"] is None and engine.get(drop).buyers["alex"].status == "held"
    for call in (api.me, api.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "alex", sec("a"))
        assert e.value.code == "not_yours"
    assert len(open_auths(fake)) == 1
    assert [r["unconfirmed_web_join"] for r in api.maker(drop)["buyers_list"]] == [True]     # whose it is cannot be told: the maker sees it


def test_r4_an_assistant_join_that_failed_in_the_gap_is_not_mistaken_for_a_second_ask_of_the_same_join(api, engine, fake, drop, monkeypatch):
    real = api.engine.join

    def assistant_is_declined_in_the_gap(*a, **k):
        fake.decline_next_authorize()
        with pytest.raises(CrowdError):
            real(drop, "Alex", 0)                                            # its own, different request (attempt 1); the web's is attempt 2
        return real(*a, **k)
    monkeypatch.setattr(api.engine, "join", assistant_is_declined_in_the_gap)
    got = api.join(drop, "Alex", 0, sec("a"))                                # the web's hold is its own: bound as usual
    assert got["buyer"] == "alex" and api.me(drop, "alex", sec("a"))["me"]["status"] == "held" and len(open_auths(fake)) == 1


def test_r4_a_claim_follows_the_number_the_engine_gave_its_join(api, engine, fake, drop, monkeypatch):
    real = api.engine.join

    def assistant_is_declined_in_the_gap(*a, **k):
        fake.decline_next_authorize()
        with pytest.raises(CrowdError):
            real(drop, "Alex", 0)
        fake.lose_reply("authorize")
        return real(*a, **k)                                                 # the web's own join is attempt 2, and its reply is lost
    monkeypatch.setattr(api.engine, "join", assistant_is_declined_in_the_gap)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "paypal_unavailable" and api.tokens.row(drop, "alex")["attempt"] == 2
    monkeypatch.setattr(api.engine, "join", real)
    got = api.join(drop, "Alex", 0, sec("a"))                                # the page's retry is its own join: handed the same hold
    assert got["buyer"] == "alex" and len(open_auths(fake)) == 1 and api.leave(drop, "alex", sec("a"))["me"]["status"] == "left"


# Finding 5 (P1): an attempt that has finished never starts another join

def test_r4_after_the_maker_releases_an_unconfirmed_hold_the_pages_retry_does_not_place_a_new_one(api, engine, fake, drop, tmp_path, monkeypatch):
    die_after_the_hold(api, monkeypatch)
    with pytest.raises(Died):
        api.join(drop, "Bea", 0, sec("b"))
    api2 = restarted(api, tmp_path)
    with pytest.raises(ApiError) as e:
        api2.join(drop, "Bea", 0, sec("b"))
    assert e.value.code == "join_unconfirmed"
    assert [r["unconfirmed_web_join"] for r in api2.maker(drop)["buyers_list"]] == [True]
    engine.leave(drop, "bea")                                                # the maker releases it, as the page promised
    assert open_auths(fake) == []
    for _ in range(3):                                                       # the page's retries, and the retry of an answer it never got
        with pytest.raises(ApiError) as e:
            api2.join(drop, "Bea", 0, sec("b"))
        assert e.value.code == "join_over" and e.value.extra["nothing_held"] is True and "join again" in e.value.message
    assert open_auths(fake) == [] and engine.get(drop).buyers["bea"].status == "left"
    assert [r["unconfirmed_web_join"] for r in api2.maker(drop)["buyers_list"]] == [False]
    got = api2.join(drop, "Bea", 0, sec("c"))                                # the buyer asks again: that is a new join, with a new secret
    assert got["buyer"] == "bea" and len(open_auths(fake)) == 1


# Finding 6 (P2): a hold PayPal will not release is not "nothing held", and the maker is told what became of each failed join

def test_r4_a_hold_paypal_refuses_to_release_is_never_reported_as_nothing_held(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "paypal_unavailable" and "never charged" in e.value.message
    fake.refuse("void", "INTERNAL_SERVER_ERROR", http=422, times=None)
    sim(engine, drop, 3)
    engine.close(drop)                                                       # reconcile cannot release the web's hold
    assert engine.get(drop).buyers["alex"].error == "orphan_void_failed" and len(open_auths(fake)) == 4
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "release_stuck" and "nothing_held" not in e.value.extra
    assert "maker" in e.value.message and "do not join again" in e.value.message
    for call in (api.me, api.leave):
        with pytest.raises(ApiError) as e:
            call(drop, "alex", sec("a"))
        assert e.value.code == "not_yours"
    row = next(r for r in api.maker(drop)["buyers_list"] if "Alex" in r["name"])
    assert row["error"] == "orphan_void_failed" and row["unconfirmed_web_join"] is True and "unconfirmed web join" in row["name"]
    assert [f["text"] for f in api.maker(drop)["feed"] if "Alex" in (f["who"] or "")] == ["PayPal would not release the hold"]
    assert row["hold_may_be_open"] is True


@needs_node
def test_r4_the_maker_view_tells_a_decline_a_released_join_and_a_stuck_hold_apart(api, engine, fake, drop, tmp_path):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))
    engine.reconcile(drop)                                                   # PayPal holds it, the engine releases it: never confirmed
    fake.decline_next_authorize()
    with pytest.raises(CrowdError):
        engine.join(drop, "Dee", 0)                                          # a real decline
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Eve", 0, sec("e"))
    fake.refuse("void", "INTERNAL_SERVER_ERROR", http=422, times=None)
    engine.reconcile(drop)                                                   # PayPal will not release this one
    rows = {r["name"].split(" (")[0]: r for r in api.maker(drop)["buyers_list"]}
    assert (rows["Alex"]["status"], rows["Dee"]["status"], rows["Eve"]["status"]) == ("hold_failed",) * 3
    assert rows["Alex"]["error"] in (None, "join_unconfirmed") and rows["Dee"]["error"] not in (None, "join_unconfirmed", "orphan_void_failed")
    assert rows["Eve"]["error"] == "orphan_void_failed"
    shown = maker_js(tmp_path, api.maker(drop))
    said = {r[0].split(" (")[0]: " ".join(r[1:]) for r in shown["rows"]}
    assert said["Alex"] == "never confirmed, nothing held" and said["Dee"] == "declined"
    assert said["Eve"] == "a hold may still be open"
    assert "1 never confirmed, nothing held" in shown["counts"][0] and "1 a hold may still be open" in shown["counts"][0]
    assert [r["hold_may_be_open"] for r in api.maker(drop)["buyers_list"]] == [False, False, True]
    assert any("unconfirmed web join" in r[0] for r in shown["rows"] if r[0].startswith("Eve"))


def test_r4_the_feed_says_what_a_failed_hold_was():
    ev = lambda code, i: {"id": i, "ts": f"2026-11-01T12:0{i}:00.000Z", "drop_id": "d", "type": "hold_failed",    # noqa: E731
                          "payload": {"buyer": f"b{i}", "name": f"B{i}", "code": code}}
    lines = feed([ev("INSTRUMENT_DECLINED", 1), ev("join_unconfirmed", 2), ev("orphan_void_failed", 3), ev("orphan_unexpected_status", 4)], {}, "T")
    assert [(f["kind"], f["text"]) for f in reversed(lines)] == [
        ("declined", "card declined, nothing held"), ("unconfirmed", "join never confirmed, nothing held"),
        ("void_failed", "PayPal would not release the hold")]            # the last two stuck ones are one line, as any run of the same thing is


def test_r4_tokens_stamp_a_claim_when_it_is_written_and_list_the_unbound_ones(tmp_path):
    t = Tokens(str(tmp_path / "t.db"))
    stamps = iter(["2026-11-01T12:00:05.000Z"])
    assert t.claim("d1", "ada", sec("a"), "c1", lambda: next(stamps), attempt=2)
    assert t.claim("d1", "bo", sec("b"), "c1", "2026-11-01T12:00:00.000Z", attempt=1) and t.bind("d1", "bo", sec("b"), "AUTH")
    assert t.unbound_claims("d1", "c1") == {"ada": {"created": "2026-11-01T12:00:05.000Z", "attempt": 2}}
    assert t.unbound_claims("d1", "c2") == {}


# Findings 3 and 7 (drop.js, run for real)

S1, S2 = "S" * 43, "T" * 43
HELD1, HELD2 = {"buyer": "alex", "secret": S1}, {"buyer": "alex", "secret": S2}


@needs_node
def test_r4_a_late_answer_about_an_old_secret_does_not_erase_the_record_of_the_current_one(tmp_path):
    late = err("not_yours", "This browser does not hold that join.")
    out = drop_js(tmp_path, store=HELD1, me=[{}, {"delay": 200, "error": late["error"]}],
                  steps=[["tick"], ["starttick"], ["tab", HELD2], ["finishtick"]])        # another tab left and joined again meanwhile
    assert out["trace"][-1]["store"] == HELD2
    out = drop_js(tmp_path, store=HELD1, me=[{"delay": 100, "error": late["error"]}, {}],
                  steps=[["tick"], ["tick"]])                                              # but a record whose own answer says so is let go
    assert out["trace"][0]["store"] is None


@needs_node
def test_r4_a_late_nothing_held_answer_does_not_erase_the_record_of_another_secret(tmp_path):
    out = drop_js(tmp_path, store=PENDING, join=[{"delay": 200, "error": err("bad_card")["error"]}],
                  steps=[["starttick"], ["tab", HELD2], ["finishtick"]])
    assert out["trace"][-1]["store"] == HELD2
    out = drop_js(tmp_path, store=PENDING, join=[err("bad_card")], steps=[["tick"]])
    assert out["trace"][-1]["store"] is None                                               # and its own is let go as before


@needs_node
def test_r4_leave_without_a_record_says_so_instead_of_crashing(tmp_path):
    out = drop_js(tmp_path, store=HELD1, steps=[["tick"], ["tab", None], ["click", "Leave the drop"], ["click", "Yes, leave"]])
    said = " ".join(out["trace"][-1]["said"])
    assert "Cannot read" not in said and "lost track of your join" in said
    assert not any(c["act"] == "leave" for c in out["calls"])                                # and asked the server for nothing


@needs_node
def test_r4_join_again_does_not_erase_a_join_another_tab_made(tmp_path):
    out = drop_js(tmp_path, store=HELD1, me=[{"ok": {"me": {"buyer": "alex", "name": "Alex", "status": "left", "start_price": "40.00",
                                                           "can_leave": False, "leaving": False, "card": "Test card ending 4444"}}}],
                  steps=[["tick"], ["tab", HELD2], ["click", "Join again"]])
    assert "You left" in " ".join(out["trace"][1]["said"]) and out["trace"][-1]["store"] == HELD2


@needs_node
def test_r4_a_stuck_page_stops_asking_once_the_drop_is_settled(tmp_path):
    charged = "Your earlier try may have been charged $40.00. This page never confirmed it. The maker can see the charge and refund it."
    out = drop_js(tmp_path, store=PENDING, drop_state="settled", join=[err("join_unconfirmed", charged)] * 5,
                  steps=[["tick"], ["advance", 61000], ["tick"], ["advance", 61000], ["tick"], ["advance", 3600000], ["tick"]])
    assert [t["joins"] for t in out["trace"] if t["op"] == "tick"] == [1, 1, 1, 1]          # asked once, and the answer is final
    said = " ".join(out["trace"][-1]["said"])
    assert charged in said and "keeps checking" not in said
    final = out["trace"][-1]["store"]
    assert final["secret"] == SECRET_A and final["pending"] is True and final["final"]["text"] == charged
    again = drop_js(tmp_path, store=final, drop_state="settled", join=[err("join_unconfirmed", charged)], steps=[["tick"], ["tick"]])
    assert again["trace"][-1]["joins"] == 0 and charged in " ".join(again["trace"][-1]["said"])      # nor does a reloaded page ask again


@needs_node
def test_r4_a_stuck_page_keeps_asking_while_the_answer_can_still_change(tmp_path):
    out = drop_js(tmp_path, store=PENDING, join=[err("join_unconfirmed")] * 3,
                  steps=[["tick"], ["advance", 61000], ["tick"], ["advance", 61000], ["tick"]])      # the drop is live: the maker may yet release it
    assert [t["joins"] for t in out["trace"] if t["op"] == "tick"] == [1, 2, 3]


@needs_node
@pytest.mark.parametrize("code", ["release_stuck", "join_unconfirmed"])
def test_r4_the_page_says_what_the_server_says_about_a_hold_that_may_be_open(tmp_path, code):
    words = "Your earlier try left a hold on your card that PayPal would not release as planned. The maker can see it."
    out = drop_js(tmp_path, store=PENDING, join=[err(code, words)], steps=[["tick"]])
    said = " ".join(out["trace"][-1]["said"])
    assert words in said and "nothing was held" not in said and out["trace"][-1]["store"] == PENDING


@needs_node
def test_r4_a_join_that_is_over_lets_the_page_go_and_say_so(tmp_path):
    words = "Your earlier try is over, and nothing is held on your card now. If you still want in, join again."
    out = drop_js(tmp_path, store=PENDING, join=[err("join_over", words, nothing_held=True)], steps=[["tick"]])      # (the server marks it)
    assert out["trace"][-1]["store"] is None and words in out["trace"][-1]["said"]


def test_r4_a_secret_told_its_join_is_over_stays_over_after_another_browser_takes_the_name(api, engine, fake, drop, tmp_path, monkeypatch):
    die_after_the_hold(api, monkeypatch)
    with pytest.raises(Died):
        api.join(drop, "Bea", 0, sec("c"))
    api2 = restarted(api, tmp_path)
    engine.leave(drop, "bea")                                                # the maker releases the unconfirmed hold
    with pytest.raises(ApiError) as e:
        api2.join(drop, "Bea", 0, sec("c"))
    assert e.value.code == "join_over"                                       # (the answer is lost on its way to the page)
    api2.join(drop, "Bea", 0, sec("a"))                                      # another browser takes the name, uses it, and leaves
    api2.leave(drop, "bea", sec("a"))
    assert api2.tokens.row(drop, "bea")["secret_sha256"] == digest(sec("a"))         # the first browser's claim row is gone ...
    held = open_auths(fake)
    for _ in range(2):                                                       # ... but its page asks again, and still places nothing
        with pytest.raises(ApiError) as e:
            api2.join(drop, "Bea", 0, sec("c"))
        assert e.value.code == "join_over" and e.value.extra["nothing_held"] is True
    assert open_auths(fake) == held == []


def test_r4_a_claim_that_another_secrets_claim_overwrites_retires_its_secret(tmp_path):
    t = Tokens(str(tmp_path / "t.db"))
    assert t.claim("d1", "ada", sec("a"), "c1", "now", attempt=1)
    assert not t.retired("d1", sec("a"), "c1")
    assert t.claim("d1", "ada", sec("a"), "c1", "later", attempt=1) and not t.retired("d1", sec("a"), "c1")       # its own rewrite: no
    assert t.claim("d1", "ada", sec("b"), "c1", "now", attempt=2)
    assert t.retired("d1", sec("a"), "c1") and not t.retired("d1", sec("b"), "c1")
    assert not t.retired("d1", sec("a"), "c2") and not t.retired("d2", sec("a"), "c1")                          # per life, per drop
    assert t.bind("d1", "ada", sec("b"), "AUTH") and t.claim("d1", "ada", sec("c"), "c1", "now", attempt=3)     # a BOUND row's secret is not retired:
    assert not t.retired("d1", sec("b"), "c1")                                                                  # the page does not ask again with it


def test_r4_a_join_whose_hold_paypal_would_not_even_describe_is_not_reported_as_nothing_held(api, engine, fake, drop, monkeypatch):
    from crowd.paypal_holds import PayPalRefused
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))

    def will_not_say(*a, **k):
        raise PayPalRefused("paypal_refused", "403 on the order", http=403)
    monkeypatch.setattr(fake, "recover_join", will_not_say)
    engine.reconcile(drop)                                                   # the engine closes the join: PayPal would not say what is there
    b = engine.get(drop).buyers["alex"]
    assert b.status == "hold_failed" and b.error == "join_unconfirmed" and len(open_auths(fake)) == 1
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "release_stuck" and "nothing_held" not in e.value.extra         # a hold may be open: not "over", not "nothing held"
    row = api.maker(drop)["buyers_list"][0]
    assert row["hold_may_be_open"] is True and row["unconfirmed_web_join"] is True
    assert [f["text"] for f in api.maker(drop)["feed"] if f["who"] == "Alex"][0] == "PayPal would not say whether a hold exists"


def test_r4_another_browser_cannot_overwrite_the_claim_of_a_hold_paypal_would_not_release(api, engine, fake, drop):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError):
        api.join(drop, "Alex", 0, sec("a"))
    fake.refuse("void", "INTERNAL_SERVER_ERROR", http=422, times=None)
    engine.reconcile(drop)                                                   # stuck: the web's hold is open at PayPal and the engine gave up
    assert engine.get(drop).buyers["alex"].error == "orphan_void_failed"
    mine = api.tokens.row(drop, "alex")
    with pytest.raises(ApiError) as e:                                       # someone else asks for the same name
        api.join(drop, "Alex", 0, sec("b"))
    assert e.value.code == "join_in_progress" and e.value.extra["nothing_held"] is True and len(open_auths(fake)) == 1
    assert api.tokens.row(drop, "alex") == mine and not api.tokens.retired(drop, sec("a"), engine.get(drop).created_at)
    with pytest.raises(ApiError) as e:                                       # and the first browser is still told the truth
        api.join(drop, "Alex", 0, sec("a"))
    assert e.value.code == "release_stuck"


def test_r4_a_claim_never_overwrites_an_unbound_claim_while_a_hold_is_on_record_for_the_buyer(tmp_path):
    t = Tokens(str(tmp_path / "t.db"))
    assert t.claim("d1", "ada", sec("a"), "c1", "now", attempt=1)
    assert t.claim("d1", "ada", sec("b"), "c1", "now", hold_auth_id="AUTH1", attempt=2) is False
    assert t.own("d1", "ada", sec("a")) is not None and not t.retired("d1", sec("a"), "c1")
    assert t.claim("d1", "ada", sec("a"), "c1", "again", hold_auth_id="AUTH1", attempt=1) is True      # its own, rewritten: fine


# ---- round 5: what became of a hold is said as the log says it; the page forgets only what the server says is nothing; a read is a slot ----
#
# The refuter's probes (scratchpad r5/probes) are the originals of these: pa (F1), ph (F2), pb (F3), pc (F4), flood/legit/mkbig (F8).

HELD_ME = {"buyer": "alex", "name": "Alex", "status": "held", "card": "Test card ending 4444", "held": None, "charged": None, "saved": None,
           "start_price": "40.00", "can_leave": False, "leaving": False}
HELD_REC = {"buyer": "alex", "secret": SECRET_A}


def page_says(tmp_path, **me) -> str:
    """What the buyer's page draws for a `me` the server answered with these fields."""
    out = drop_js(tmp_path, store=HELD_REC, me=[{"ok": {"me": {**HELD_ME, **me}}}], steps=[["tick"]])
    return " | ".join(out["trace"][-1]["said"])


def refused_leave_then_close(api, engine, fake, drop, who="Alex", tag="a"):
    """A web buyer leaves while PayPal refuses the void (D-11b: the leave stands, pending); close retries and PayPal refuses again."""
    api.join(drop, who, 0, sec(tag))
    fake.refuse("void", "UNPROCESSABLE_ENTITY", times=None)
    assert api.leave(drop, who.lower(), sec(tag))["pending"] is True
    sim(engine, drop, 3)
    engine.close(drop)


# F1: a buyer who left but whose hold PayPal would not release is not told it was released

def test_r5_f1_a_leave_paypal_would_not_release_reaches_the_buyer_and_the_maker(api, engine, fake, drop):
    refused_leave_then_close(api, engine, fake, drop)
    b = engine.get(drop).buyers["alex"]
    assert b.status == "left" and b.void_failed and len(open_auths(fake)) == 4          # the engine knows: left, and the hold is still open
    me = api.me(drop, "alex", sec("a"))["me"]
    assert (me["status"], me["fate"]) == ("left", "not_released")
    row = next(r for r in api.maker(drop)["buyers_list"] if r["name"] == "Alex")
    assert (row["status"], row["fate"]) == ("left", "not_released")
    mine = [f["text"] for f in api.maker(drop)["feed"] if f["who"] == "Alex"]
    assert mine == ["left. PayPal would not release the hold"]                         # one line, never "Hold released" and then "would not"


def test_r5_f1_a_leave_that_went_through_is_released(api, engine, fake, drop):
    api.join(drop, "Alex", 0, sec("a"))
    assert api.leave(drop, "alex", sec("a"))["released"] == "40.00"
    assert api.me(drop, "alex", sec("a"))["me"]["fate"] == "released"
    assert api.maker(drop)["buyers_list"][0]["fate"] == "released"
    assert [f["text"] for f in api.maker(drop)["feed"] if f["who"] == "Alex"][:1] == ["left. Hold released"]


def test_r5_f1_a_refused_leave_of_an_earlier_hold_says_nothing_about_a_later_one(api, engine, fake, drop):
    api.join(drop, "Alex", 0, sec("a"))
    fake.refuse("void", "UNPROCESSABLE_ENTITY", times=2)                               # the leave, and the retry that completes it
    assert api.leave(drop, "alex", sec("a"))["pending"] is True
    engine.reconcile(drop)                                                             # the leave stands; the hold stays open at PayPal
    b = engine.get(drop).buyers["alex"]
    assert b.status == "left" and b.void_failed
    assert api.join(drop, "Alex", 0, sec("b"))["me"]["status"] == "held"               # joins again (a second hold), then leaves cleanly
    assert len(open_auths(fake)) == 2                                                  # (SECURITY.md: the first is still open beside it)
    assert api.leave(drop, "alex", sec("b"))["released"] == "40.00"
    assert engine.get(drop).buyers["alex"].void_failed                                 # (the engine's flag is about the buyer, and stays set)
    assert api.me(drop, "alex", sec("b"))["me"]["fate"] == "released"                  # the fate is about THIS hold
    assert [r["fate"] for r in api.maker(drop)["buyers_list"]] == ["released"]


@needs_node
def test_r5_f1_the_page_does_not_say_released_for_a_hold_paypal_would_not_release(tmp_path):
    said = page_says(tmp_path, status="left", fate="not_released")
    assert "You left" in said and "has not released your $40.00 hold" in said and "You will not be charged" in said
    assert "maker can see it" in said
    assert "was released" not in said and "Join again" not in said                    # and no button to hold the card a second time
    said = page_says(tmp_path, status="left", fate="released")
    assert "Your $40.00 hold was released. You pay nothing." in said and "Join again" in said


@needs_node
def test_r5_f1_the_maker_sees_a_leave_whose_hold_is_still_open_as_a_problem(api, engine, fake, drop, tmp_path):
    refused_leave_then_close(api, engine, fake, drop)
    shown = maker_js(tmp_path, api.maker(drop))
    said = {r[0]: " ".join(r[1:]) for r in shown["rows"]}
    assert said["Alex"] == "left, hold not released"
    assert "1 left, hold not released" in shown["counts"][0]


# F2: the page forgets its secret only when the server says nothing was held

def test_r5_f2_a_hold_taken_outside_the_engine_keeps_the_claim_and_does_not_promise_nothing_is_charged(api, engine, fake, drop, clock):
    fake.lose_reply("authorize")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("w"))
    assert e.value.code == "paypal_unavailable"
    h1 = open_auths(fake)[0]
    fake.capture(h1, "40.00", "outside-capture-1")                                      # somebody charges the hold before the page asks again
    clock.advance(seconds=4)
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("w"))
    assert e.value.code == "hold_not_open" and "nothing_held" not in e.value.extra     # the server kept the claim ...
    assert api.tokens.row(drop, "alex") is not None
    assert "Nothing will be charged" not in e.value.message and "you are not in" not in e.value.message    # ... and does not say it is nothing
    with pytest.raises(ApiError) as e:
        api.join(drop, "Alex", 0, sec("w"))
    assert e.value.code == "release_stuck"                                              # what the page learns if it still has the secret


def test_r5_f2_a_decline_the_server_forgot_the_claim_for_says_nothing_was_held(api, engine, fake, drop):
    fake.decline_next_authorize()
    with pytest.raises(ApiError) as e:
        api.join(drop, "Dee", 0, sec("d"))
    assert e.value.code == "join_declined" and e.value.extra["nothing_held"] is True
    assert "nothing was held" in e.value.message and api.tokens.row(drop, "dee") is None


@pytest.mark.parametrize("how", ["decline", "refuse"])
def test_r5_f2_a_refusal_made_while_the_claim_stays_does_not_say_nothing_was_held(api, engine, fake, drop, monkeypatch, how):
    monkeypatch.setattr(api, "_forget_untraced", lambda *a, **k: False)       # the claim stays (the log shows an earlier try may hold something)
    if how == "decline":
        fake.decline_next_authorize()
    else:
        fake.refuse("authorize", "UNPROCESSABLE_ENTITY")
    with pytest.raises(ApiError) as e:
        api.join(drop, "Dee", 0, sec("d"))
    assert e.value.code in ("join_declined", "paypal_error") and "nothing_held" not in e.value.extra
    assert "nothing was held" not in e.value.message.lower() and "do not join again" in e.value.message


@needs_node
@pytest.mark.parametrize("code", ["hold_not_open", "join_declined", "not_recorded", "wrong_paypal", "drop_unbound", "paypal_error"])
def test_r5_f2_the_page_keeps_its_secret_when_the_server_did_not_say_nothing_was_held(tmp_path, code):
    out = drop_js(tmp_path, store=PENDING, join=[err(code, "PayPal did not confirm the hold, so you are not in. Nothing will be charged.")],
                  steps=[["tick"]])
    last = out["trace"][-1]
    assert last["store"] == PENDING                                                     # the same secret, still pending
    said = " ".join(last["said"])
    assert "Join the drop" not in said and "Nothing will be charged" not in said      # no form for another name, no promise it cannot keep


@needs_node
def test_r5_f2_after_a_hold_not_open_the_page_asks_again_with_the_same_secret_and_hears_the_truth(tmp_path):
    stuck = "Your earlier try may have left a hold on your card that PayPal would not release or describe as planned."
    out = drop_js(tmp_path, store=PENDING, join=[err("hold_not_open"), err("release_stuck", stuck)],
                  steps=[["tick"], ["advance", 4000], ["tick"]])
    joins = [c["body"] for c in out["calls"] if c["act"] == "join"]
    assert len(joins) == 2 and all(b["secret"] == SECRET_A for b in joins)
    assert out["trace"][-1]["store"] == PENDING and stuck in " ".join(out["trace"][-1]["said"])


# F3: "the drop missed its minimum" is said only when it did

@pytest.fixture
def funded_with_two_holds_gone(api, engine, fake, drop):
    """A funded drop. Alex's hold is captured at PayPal outside the engine; Bo's hold lapses. Close records both as voided."""
    api.join(drop, "Alex", 0, sec("a"))
    api.join(drop, "Bo", 1, sec("b"))
    sim(engine, drop, 4)
    fake.capture(engine.get(drop).buyers["alex"].auth_id, "40.00", "outside-capture-1")
    fake.expire(engine.get(drop).buyers["bo"].auth_id)
    engine.close(drop)
    charge(engine, drop)
    assert engine.get(drop).settled["outcome"] == "funded"
    return drop


def test_r5_f3_a_hold_captured_outside_or_lapsed_on_a_funded_drop_is_not_a_missed_minimum(api, funded_with_two_holds_gone):
    drop = funded_with_two_holds_gone
    assert [api.me(drop, w, sec(t))["me"]["fate"] for w, t in (("alex", "a"), ("bo", "b"))] == ["taken_outside", "lapsed"]
    rows = {r["name"]: r for r in api.maker(drop)["buyers_list"]}
    assert (rows["Alex"]["status"], rows["Alex"]["fate"]) == ("voided", "taken_outside")
    assert (rows["Bo"]["status"], rows["Bo"]["fate"]) == ("voided", "lapsed")
    said = {f["who"]: f["text"] for f in api.maker(drop)["feed"] if f["who"] in ("Alex", "Bo")}
    assert "charged outside the drop" in said["Alex"] and "released" not in said["Alex"].replace("not released", "")
    assert "ran out" in said["Bo"]
    assert not any(f["text"].endswith("holds released") or f["text"] == "hold released" for f in api.maker(drop)["feed"])


def test_r5_f3_a_hold_released_because_the_drop_missed_its_minimum_says_so(api, engine, drop):
    api.join(drop, "Alex", 0, sec("a"))
    engine.close(drop)
    charge(engine, drop)
    assert engine.get(drop).settled["outcome"] != "funded"
    assert api.me(drop, "alex", sec("a"))["me"]["fate"] == "missed_minimum"
    assert api.maker(drop)["buyers_list"][0]["fate"] == "missed_minimum"


@needs_node
def test_r5_f3_the_page_says_missed_minimum_only_when_that_is_what_happened(tmp_path):
    for fate, must, mustnt in (("missed_minimum", "The drop missed its minimum, so your hold was released", None),
                               ("lapsed", "Your hold ran out at PayPal", "missed its minimum"),
                               ("gone", "PayPal no longer shows your hold", "missed its minimum"),
                               ("released", "Your hold was released", "missed its minimum"),
                               ("taken_outside", "money was taken from your hold by something other than this drop", "You paid nothing"),
                               (None, "The maker can see what became of it", "You paid nothing")):
        said = page_says(tmp_path, status="voided", fate=fate)
        assert must in said, (fate, said)
        if mustnt:
            assert mustnt not in said, (fate, said)


@needs_node
def test_r5_f3_the_maker_sees_a_hold_charged_outside_the_drop_as_a_problem_not_as_released(api, funded_with_two_holds_gone, tmp_path):
    shown = maker_js(tmp_path, api.maker(funded_with_two_holds_gone))
    said = {r[0]: " ".join(r[1:]) for r in shown["rows"]}
    assert said["Alex"] == "charged outside the drop" and said["Bo"] == "hold ran out"
    assert "released" not in said["Alex"] and "released" not in said["Bo"]


def test_r5_f3_the_feed_tells_a_released_hold_from_one_that_lapsed_was_taken_or_vanished():
    def ev(i, buyer, **p):
        return {"id": i, "ts": f"2026-11-01T12:0{i}:00.000Z", "drop_id": "d", "type": "hold_voided",
                "payload": {"buyer": buyer, "name": buyer.title(), "auth_id": f"A{i}", **p}}
    lines = feed([ev(1, "a", reason="not_funded"), ev(2, "b", reason="paypal_says_expired", expired=True),
                  ev(3, "c", reason="paypal_says_captured", taken_outside_engine=True), ev(4, "d", reason="paypal_says_voided"),
                  ev(5, "e", reason="paypal_says_denied", released_by_void=True)], {}, "T")
    assert [(f["kind"], f["text"]) for f in reversed(lines)] == [
        ("released", "hold released"), ("lapsed", "hold ran out at PayPal, so out of the drop"),
        ("taken_outside", "hold was charged outside the drop, so out of the drop. It was not released: refund it at PayPal"),
        ("gone", "hold no longer at PayPal, so out of the drop"), ("released", "hold released")]


# F4: a buyer the maker took out is told what happened to them

def test_r5_f4_a_buyer_taken_out_with_the_hold_released_is_told_so(api, engine, fake, drop):
    api.join(drop, "Alex", 0, sec("a"))
    assert engine.exclude(drop, "alex")["released"] is True
    me = api.me(drop, "alex", sec("a"))["me"]
    assert (me["status"], me["fate"]) == ("excluded", "released")
    assert api.maker(drop)["buyers_list"][0]["fate"] == "released"


def test_r5_f4_a_buyer_taken_out_whose_hold_paypal_would_not_release_is_told_that(api, engine, fake, drop):
    api.join(drop, "Alex", 0, sec("a"))
    fake.refuse("void", "UNPROCESSABLE_ENTITY", times=None)
    assert engine.exclude(drop, "alex")["released"] is False
    assert api.me(drop, "alex", sec("a"))["me"]["fate"] == "not_released"
    assert api.maker(drop)["buyers_list"][0]["fate"] == "not_released"


@needs_node
def test_r5_f4_the_page_does_not_say_the_card_could_not_be_released_when_it_was(tmp_path):
    said = page_says(tmp_path, status="excluded", fate="released")
    assert "You were taken out of this drop" in said and "Your $40.00 hold was released. You pay nothing." in said
    assert "could not be charged or released" not in said and "Something needs the maker" not in said
    said = page_says(tmp_path, status="excluded", fate="not_released")
    assert "PayPal has not released your hold yet" in said and "You will not be charged for this drop" in said
    assert "was released" not in said
    said = page_says(tmp_path, status="excluded", fate="taken_outside")
    assert "money was taken from your hold" in said and "You pay nothing" not in said
    assert "The maker can see what became of your hold" in page_says(tmp_path, status="excluded", fate=None)
    assert "Something needs the maker" in page_says(tmp_path, status="capture_failed", fate=None)         # the other words are as they were


@needs_node
def test_r5_f4_the_maker_sees_whether_a_removed_buyers_hold_was_released(api, engine, fake, drop, tmp_path):
    api.join(drop, "Alex", 0, sec("a"))
    api.join(drop, "Bo", 1, sec("b"))
    engine.exclude(drop, "alex")
    fake.refuse("void", "UNPROCESSABLE_ENTITY", times=None)
    engine.exclude(drop, "bo")
    said = {r[0]: " ".join(r[1:]) for r in maker_js(tmp_path, api.maker(drop))["rows"]}
    assert said == {"Alex": "taken out", "Bo": "taken out, hold not released"}


# F8: every connection counts against its address until its response is written

def get_status(port, path, source_port=None, headers=None, timeout=15):
    """One GET, as its status or the name of what went wrong (a connection the server turned away raises)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout, source_address=("127.0.0.1", source_port) if source_port else None)
    try:
        c.request("GET", path, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        r.read()
        return r.status
    except Exception as e:  # noqa: BLE001
        return type(e).__name__
    finally:
        c.close()


def test_r5_f8_plain_reads_count_against_their_address_until_they_are_written_and_cannot_shut_out_another(api, drop, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "MAX_CONNECTIONS", 6)
    monkeypatch.setattr(srv, "MAX_CONNECTIONS_PER_ADDRESS", 3)
    victim_ports = set(free_ports(3))
    httpd = serve(api, KEY, "127.0.0.1", 0)
    orig = httpd.process_request
    httpd.process_request = lambda request, addr: orig(request, ("10.9.9.9", addr[1]) if addr[1] in victim_ports else addr)   # the visitor's address
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    real, inside, release = api.drop, threading.Semaphore(0), threading.Event()

    def long_read(d):                                                        # a drop with a long log: the read takes a while
        inside.release()
        release.wait(20)
        return real(d)
    monkeypatch.setattr(api, "drop", long_read)
    results = []
    threads = [threading.Thread(target=lambda: results.append(get_status(port, f"/api/drops/{drop}"))) for _ in range(8)]
    try:
        for t in threads:
            t.start()
            time.sleep(0.02)
        for _ in range(3):
            assert inside.acquire(timeout=5)                                 # three reads are under way ...
        time.sleep(0.4)
        assert httpd._open == {"127.0.0.1": 3}, httpd._open                  # ... and counted to their address while they are
        assert httpd._slots._value == 6 - 3                                  # the other five were turned away at the door
        assert get_status(port, f"/api/drops/{drop}") != 200                 # (that address cannot start a fourth)
        for sp in victim_ports:                                              # another address is served, and so is the maker
            assert get_status(port, "/api/info", source_port=sp) == 200
        assert get_status(port, f"/api/maker/drops/{drop}", source_port=next(iter(victim_ports)), headers={"X-Maker-Key": KEY}) == 200
    finally:
        release.set()
        for t in threads:
            t.join(15)
        httpd.shutdown()
        httpd.server_close()
    assert sorted(r for r in results if isinstance(r, int)) == [200] * 3 and len(results) == 8, results      # three were served, five never were
    assert httpd._open == {} and httpd._slots._value == 6                    # and everything is given back


def test_r5_f8_every_kind_of_request_gives_its_place_back_when_it_is_over(api, drop, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "MAX_CONNECTIONS_PER_ADDRESS", 2)
    with running(api) as call:
        for _ in range(8):                                                   # more than the address's share, one after the other
            assert call(f"/api/drops/{drop}")[0] == 200 and call("/")[0] == 200 and call("/static/app.js")[0] == 200
            assert call(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": KEY})[0] == 200
            assert call(f"/api/drops/{drop}/me", body={"buyer": "x", "secret": sec("z")})[0] == 403
            assert call(f"/api/drops/{drop}", method="DELETE")[0] == 405
            assert call("/api/nowhere")[0] == 404
            assert call(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": "wrong-key-xxxxxxxx"})[0] in (401, 429)
        assert call.httpd._open == {} and call.httpd._slots._value == srv.MAX_CONNECTIONS


def test_r5_f8_ordinary_use_is_not_locked_out_by_the_count(api, drop, monkeypatch):
    """A page load, the polls and the maker's view together are a handful of overlapping reads, far under an address's share."""
    real, release = api.drop, threading.Event()

    def slowish(d):
        release.wait(0.3)
        return real(d)
    monkeypatch.setattr(api, "drop", slowish)
    with running(api) as call:
        results = []
        threads = [threading.Thread(target=lambda: results.append(call(f"/api/drops/{drop}")[0])) for _ in range(10)]    # ten at once
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        assert results == [200] * 10
        assert call.httpd._open == {}


def test_r5_f8_a_join_or_leave_that_has_won_the_turn_is_the_servers_own_work_and_not_counted(api, engine, drop, monkeypatch):
    import crowdweb.server as srv
    monkeypatch.setattr(srv, "MAX_CONNECTIONS_PER_ADDRESS", 2)
    api.join(drop, "Ada", 0, sec("a"))
    real, inside, release = engine.leave, threading.Event(), threading.Event()

    def slow_paypal(*a, **k):                                                # PayPal answers slowly
        inside.set()
        release.wait(20)
        return real(*a, **k)
    monkeypatch.setattr(engine, "leave", slow_paypal)
    with running(api) as call:
        results = []
        t = threading.Thread(target=lambda: results.append(post_status(call.port, f"/api/drops/{drop}/leave", {"buyer": "ada", "secret": sec("a")})))
        t.start()
        try:
            assert inside.wait(5)
            assert call.httpd._open == {}                                    # it has the turn: its address does not pay for PayPal's slowness
            assert call.httpd._slots._value == srv.MAX_CONNECTIONS - 1       # (it still holds a thread and a slot)
        finally:
            release.set()
            t.join(15)
        assert results == [200]
