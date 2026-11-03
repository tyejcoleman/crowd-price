"""The web layer: the buyer's page and the maker's view over the engine. Offline: a temp store and the fake PayPal.

What must hold: a page shows only the engine's numbers; a buyer can join and leave only their own hold (a token proves it);
nothing on the web can settle, exclude or bind (no route, no call); the maker's view needs the maker key.
"""
import json
import re
import threading
import time
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

@pytest.fixture
def http(api):
    httpd = serve(api, KEY, "127.0.0.1", 0)
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
    assert http(f"/api/maker/drops/{drop}", headers={"X-Maker-Key": KEY})[0] == 429      # from this address, for a minute


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
