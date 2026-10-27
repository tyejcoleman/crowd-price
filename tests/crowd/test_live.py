"""Opt-in live run against the PayPal SANDBOX: CROWD_LIVE=1 python3 -m pytest -s -q tests/crowd/test_live.py

Small amounts only: a funded drop (3 buyers, minimum 2, hold 5.00, final price 4.00) and a not-funded drop
(2 buyers, minimum 3), each followed by `verify`. The report printed at the end lists the PayPal ids.
"""
import json
import os
from decimal import Decimal

import pytest

from crowd import ladder as lad
from crowd import verify as V
from crowd.engine import Engine
from crowd.paypal_holds import DECLINE_CARD_INDEX, PayPalHolds
from crowd.store import Store

pytestmark = pytest.mark.skipif(os.environ.get("CROWD_LIVE") != "1", reason="live sandbox run is opt-in: CROWD_LIVE=1")

REPORT = {}


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    store = Store(str(tmp_path_factory.mktemp("live") / "crowd.db"))
    return Engine(store, PayPalHolds())


def ids(engine, drop):
    d = engine.get(drop)
    return [{"buyer": b.id, "status": b.status, "order": b.order_id, "auth": b.auth_id, "capture": b.capture_id,
             "charged": str(b.captured) if b.captured is not None else None, "fee": str(b.fee) if b.fee is not None else None}
            for b in d.buyers.values()]


def test_live_funded_drop(eng):
    d = eng.create_drop("Live funded", lad.parse("2:5,3:4"), 2, 60)
    eng.publish(d)
    joins = [eng.join(d, f"Live Buyer {i}", card_index=i) for i in range(3)]       # three different test cards
    assert [j["price"] for j in joins] == ["5.00", "5.00", "4.00"] and joins[2]["step_locked"] == 1
    eng.close(d)
    plan = eng.plan(d)
    assert plan["summary"] == "capture 3 holds at $4.00 = $12.00"
    out = eng.settle(d)
    assert out["outcome"] == "funded" and out["counts"] == {"holds": 3, "captured": 3, "voided": 0, "failed": 0, "left": 0}
    assert out["totals"]["gross"] == "12.00"
    v = V.verify(d, eng)
    assert v["ok"], v["problems"]
    again = eng.settle(d)                                                           # idempotent against PayPal too
    assert again["already_settled"] is True
    rows = ids(eng, d)
    modelled = lad.fee(Decimal("4.00"))
    REPORT["funded"] = {"drop": d, "summary": plan["summary"], "holds": rows, "totals": out["totals"],
                        "verify_ok": v["ok"], "modelled_fee_each": str(modelled),
                        "paypal_fee_each": sorted({r["fee"] for r in rows})}
    REPORT["tieout"] = V.tieout(d, eng)


def test_live_not_funded_drop(eng):
    d = eng.create_drop("Live not funded", lad.parse("3:5,4:4"), 3, 60)
    eng.publish(d)
    for i in range(2):
        eng.join(d, f"Short Buyer {i}", card_index=i)
    eng.close(d)
    assert eng.plan(d)["summary"] == "void 2 holds, nobody pays"
    out = eng.settle(d)
    assert out["outcome"] == "not_funded" and out["counts"]["voided"] == 2 and out["totals"]["gross"] == "0.00"
    v = V.verify(d, eng)
    assert v["ok"], v["problems"]
    REPORT["not_funded"] = {"drop": d, "holds": ids(eng, d), "verify_ok": v["ok"], "totals": out["totals"]}


def test_live_a_leave_voids_at_paypal_and_the_step_stays_locked(eng):
    d = eng.create_drop("Live leave", lad.parse("2:5,3:4"), 2, 60)
    eng.publish(d)
    for i in range(3):
        eng.join(d, f"Leave Buyer {i}", card_index=i)
    out = eng.leave(d, "Leave Buyer 2")
    assert out["price"] == "4.00" and out["buyers"] == 2 and out["locked_step"] == 1       # locked after the leave
    auth = eng.get(d).buyers["leave-buyer-2"].auth_id
    assert eng.holds.get(auth)["status"] == "VOIDED"
    eng.close(d)
    out = eng.settle(d)
    assert out["final_price"] == "4.00" and out["counts"] == {"holds": 3, "captured": 2, "voided": 1, "failed": 0, "left": 1}
    v = V.verify(d, eng)
    assert v["ok"], v["problems"]
    REPORT["leave"] = {"drop": d, "holds": ids(eng, d), "verify_ok": v["ok"]}


def test_live_a_declined_capture_is_recorded_not_hidden(eng):
    """Flow E. Uses the sandbox's negative-testing header on capture; reports honestly if PayPal ignores it."""
    d = eng.create_drop("Live decline", lad.parse("1:5"), 1, 60)
    eng.publish(d)
    eng.join(d, "Card Nine", card_index=DECLINE_CARD_INDEX)
    eng.close(d)
    out = eng.settle(d)
    b = eng.get(d).buyers["card-nine"]
    REPORT["decline"] = {"drop": d, "outcome": out["counts"], "buyer": ids(eng, d), "honoured_mock_header": b.status == "capture_failed"}
    if b.status == "capture_failed":
        # Since round 1 a failed capture's hold is released by settle itself: nothing is left on the card.
        assert out["counts"]["failed"] == 1 and eng.holds.get(b.auth_id)["status"] == "VOIDED"
        assert V.verify(d, eng)["ok"]
    else:
        assert b.status == "captured"                                                # the mock header was not honoured


def test_zz_print_report():
    print("\n=== LIVE SANDBOX REPORT ===")
    print(json.dumps(REPORT, indent=1, default=str))
