"""The web layer: the buyer's page and the maker's view over the engine. Offline: a temp store and the fake PayPal.

What must hold: a page shows only the engine's numbers; a buyer can join and leave only their own hold (a token proves it);
nothing on the web can settle, exclude or bind (no route, no call); the maker's view needs the maker key.
"""
import contextlib
import json
import re
import shutil
import socket
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
from crowdweb.tokens import Tokens

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
# A scenario is {store, persist, join: [answers], steps: [...]}; steps are ["tick"], ["advance", ms], ["submit", name], ["tab", record].
DROP_JS_HARNESS = r'''
const fs = require("fs"), vm = require("vm");
const sc = JSON.parse(fs.readFileSync(0, "utf8"));
const src = fs.readFileSync(process.argv[2], "utf8");
const KEY = "crowd-price:d_test";
const persist = sc.persist !== false;
let now = 1000000, ticker = null, nsecret = 0, lastCard = [];
const mem = new Map();
if (sc.store) mem.set(KEY, JSON.stringify(sc.store));
const calls = [], forms = {}, joinQ = [...(sc.join || [])];
const DROP = { drop: "d_test", title: "Mugs", state: "live", joinable: true, price: "40.00", start_price: "40.00", minimum: 3,
  minimum_met: false, buyers: 0, steps: [{ step: 0, from_buyers: 3, price: "40.00" }], locked_step: null, next_step: null,
  next_message: null, details: null, paypal: "fake", maker_name: "Juniper", settled: null, saved_each: null, next_saving: null,
  ends_at: "2999-01-01T00:00:00Z", time_left_seconds: 100, server_time: "2026-11-01T12:00:00Z" };
const ME = { buyer: "alex", name: "Alex", status: "held", card: "Test card ending 4444", held: "40.00", charged: null, saved: null,
  start_price: "40.00", can_leave: true, leaving: false };

async function api(path, opts = {}) {
  if (path === "/api/cards") return [{ index: 0, label: "Test card ending 4444" }];
  const act = path.split("/").pop();
  calls.push({ act, body: opts.body });
  if (act === "join") {
    const r = joinQ.shift();
    if (!r) throw { code: "offline", message: "The drop could not be reached." };
    if (r.error) throw r.error;
    return { buyer: "alex", me: ME, drop: DROP, ...r.ok };
  }
  if (act === "me") return { me: ME, drop: DROP };
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
  for (const [op, arg] of sc.steps) {
    if (op === "tick") await ticker();
    else if (op === "advance") now += arg;
    else if (op === "tab") mem.set(KEY, JSON.stringify(arg));
    else if (op === "submit") { forms.name.oninput({ target: { value: arg } }); await forms.submit({ preventDefault() {} }); }
    trace.push({ op, store: persist ? JSON.parse(mem.get(KEY) || "null") : null, said: lastCard.flatMap(words) });
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


def err(code, message="Words from the server.", **extra):
    return {"error": {"code": code, "message": message, **extra}}


@needs_node
@pytest.mark.parametrize("code", ["slow_down", "internal", "offline", "paypal_unavailable", "earlier_attempt_unresolved", "join_unconfirmed",
                                  "not_recorded_held", "paypal_error", "error", "http_502", "join_in_progress", "already_joined"])
def test_the_page_keeps_its_secret_on_every_answer_that_does_not_prove_nothing_was_held(tmp_path, code):
    out = drop_js(tmp_path, store=PENDING, join=[err(code)], steps=[["tick"]])
    last = out["trace"][-1]
    assert last["store"] == PENDING, code                                  # still the same secret, still pending
    assert any("Confirming your join" in w for w in last["said"]) and not any("Join the drop" in w for w in last["said"])


@needs_node
@pytest.mark.parametrize("answer", [err("bad_card"), err("bad_buyer"), err("join_declined"), err("hold_not_open"), err("drop_closed"),
                                    err("drop_ended"), err("not_recorded"), err("paypal_error", nothing_held=True)])
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
