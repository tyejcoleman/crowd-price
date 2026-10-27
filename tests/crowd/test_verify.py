"""The settlement check must pass on honest settlements and name the problem when PayPal disagrees."""
import json
import pytest

from conftest import join_many
from crowd import verify as V
from crowd.engine import CrowdError


def settled(engine, drop, n, close=True):
    join_many(engine, drop, n)
    engine.close(drop)
    return engine.settle(drop)


def edit_fake(fake, fn):
    st = json.load(open(fake.path))
    fn(st)
    json.dump(st, open(fake.path, "w"))


def test_verify_passes_a_funded_settlement(engine, drop):
    settled(engine, drop, 6)
    v = V.verify(drop, engine)
    assert v["ok"] and v["problems"] == []
    assert v["counts"] == {"holds": 6, "captured": 6, "voided": 0, "failed": 0}
    assert v["totals"] == {"gross": "216.00", "fees": "8.52", "net": "207.48"}          # 6 x (36.00 - 1.42)
    names = [c["check"] for c in v["checks"]]
    assert "holds accounted for" in names and "final price follows the ladder" in names and "totals match PayPal" in names
    assert len(v["holds"]) == 6 and all(h["ok"] for h in v["holds"])


def test_verify_passes_a_not_funded_settlement(engine, drop):
    settled(engine, drop, 2)
    v = V.verify(drop, engine)
    assert v["ok"] and v["outcome"] == "not_funded" and v["counts"]["voided"] == 2 and v["totals"]["gross"] == "0.00"
    assert {c["check"] for c in v["checks"]} >= {"minimum was missed", "nobody was charged"}


def test_verify_counts_buyers_who_left(engine, drop):
    join_many(engine, drop, 4)
    engine.leave(drop, "Buyer 04")
    engine.close(drop)
    engine.settle(drop)
    v = V.verify(drop, engine)
    assert v["ok"] and v["counts"] == {"holds": 4, "captured": 3, "voided": 1, "failed": 0}


def test_verify_passes_with_a_named_failed_capture(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    fake.decline_next(1)
    engine.settle(drop)
    v = V.verify(drop, engine)
    assert v["ok"] and v["counts"] == {"holds": 3, "captured": 2, "voided": 0, "failed": 1}


def test_verify_needs_a_settled_drop(engine, drop):
    join_many(engine, drop, 3)
    with pytest.raises(CrowdError) as e:
        V.verify(drop, engine)
    assert e.value.code == "drop_not_settled"


def test_verify_catches_a_hold_voided_after_the_engine_captured_it(engine, drop, fake):
    settled(engine, drop, 3)
    auth = engine.get(drop).buyers["buyer-02"].auth_id
    edit_fake(fake, lambda st: st["auths"][auth].update(status="VOIDED"))
    v = V.verify(drop, engine)
    assert not v["ok"]
    assert any("buyer-02" in p and "VOIDED" in p for p in v["problems"])


def test_verify_catches_a_capture_of_the_wrong_amount(engine, drop, fake):
    settled(engine, drop, 3)
    cap = engine.get(drop).buyers["buyer-01"].capture_id
    edit_fake(fake, lambda st: st["captures"][cap].update(amount="39.00"))
    v = V.verify(drop, engine)
    assert not v["ok"] and any("final price" in p for p in v["problems"])


def test_verify_catches_a_capture_that_is_not_final(engine, drop, fake):
    settled(engine, drop, 3)
    cap = engine.get(drop).buyers["buyer-01"].capture_id
    edit_fake(fake, lambda st: st["captures"][cap].update(final=False))
    assert any("not marked final" in p for p in V.verify(drop, engine)["problems"])


def test_verify_catches_a_released_hold_that_paypal_still_holds(engine, drop, fake):
    settled(engine, drop, 2)
    auth = engine.get(drop).buyers["buyer-01"].auth_id
    edit_fake(fake, lambda st: st["auths"][auth].update(status="CREATED"))
    v = V.verify(drop, engine)
    assert not v["ok"] and any("voided" in p and "CREATED" in p for p in v["problems"])


def test_verify_catches_money_taken_from_a_failed_capture(engine, drop, fake):
    join_many(engine, drop, 3)
    engine.close(drop)
    fake.decline_next(1)
    engine.settle(drop)
    failed = [b for b in engine.get(drop).buyers.values() if b.status == "capture_failed"][0]
    edit_fake(fake, lambda st: st["auths"][failed.auth_id].update(status="CAPTURED"))
    assert not V.verify(drop, engine)["ok"]


def test_verify_catches_a_hold_the_engine_lost_track_of(engine, drop, fake):
    settled(engine, drop, 3)
    # a hold PayPal knows about and the engine recorded as placed, but with no ending event
    engine.store.append(drop, "hold_placed", {"buyer": "ghost", "auth_id": "FKAghost", "amount": "40.00"})
    v = V.verify(drop, engine)
    assert not v["ok"] and any("never settled" in p for p in v["problems"])


def test_verify_reports_totals_that_disagree_with_the_engine(engine, drop, fake):
    settled(engine, drop, 3)
    cap = engine.get(drop).buyers["buyer-01"].capture_id
    edit_fake(fake, lambda st: st["captures"][cap].update(fee="9.99", net="30.01"))
    assert not V.verify(drop, engine)["ok"]


def test_verify_reports_unreadable_holds_instead_of_passing(engine, drop, fake):
    settled(engine, drop, 3)
    edit_fake(fake, lambda st: st["auths"].clear())
    v = V.verify(drop, engine)
    assert not v["ok"] and any("could not read" in p for p in v["problems"])


def test_verify_quiet_mode_lists_only_problem_holds(engine, drop):
    settled(engine, drop, 3)
    assert "holds" not in V.verify(drop, engine, verbose=False)


# ---- the balance tie-out ----------------------------------------------------------------------------------------------

def test_tieout_refuses_in_fake_mode_because_there_is_no_transaction_search(engine, drop):
    settled(engine, drop, 3)
    out = V.tieout(drop, engine)
    assert out["kind"] == "refusal" and out["reason"] == "no_transaction_search"


class SandboxLike:
    """The fake pretending to be the sandbox, so the program-building path can be checked offline."""

    def __init__(self, fake):
        self.fake, self.kind = fake, "sandbox"

    def __getattr__(self, name):
        return getattr(self.fake, name)


def test_tieout_program_is_this_drops_captures_plus_a_rest_part(engine, drop, fake):
    settled(engine, drop, 3)
    eng = type(engine)(engine.store, SandboxLike(fake), engine._clock)
    prog = V.tieout_program(drop, eng)
    caps = sorted(b.capture_id for b in engine.get(drop).buyers.values())
    assert prog["parts"][0] == {"name": "drop gross", "take": "gross", "where": {"id": caps}} or \
        sorted(prog["parts"][0]["where"]["id"]) == caps
    assert prog["parts"][1]["take"] == "fee" and prog["parts"][2] == {"name": "everything else", "take": "rest"}
    assert prog["answer"] == ["drop gross", "drop fees"]
    from closure import program as cp
    # capture ids from the fake are not PayPal-shaped, but the structure must satisfy the certifier's own validator
    prog["parts"][0]["where"]["id"] = ["56290850S0726000J"]
    prog["parts"][1]["where"]["id"] = ["56290850S0726000J"]
    cp.validate(prog)


def test_tieout_hands_the_program_to_the_certifier_and_returns_its_answer_as_is(engine, drop, fake, monkeypatch):
    settled(engine, drop, 3)
    eng = type(engine)(engine.store, SandboxLike(fake), engine._clock)
    seen = {}

    def fake_ask(source, program):
        seen["program"], seen["source"] = program, source
        return {"kind": "refusal", "reason": "not_yet_indexed", "what_to_do": "later"}

    monkeypatch.setattr("closure.service.ask", fake_ask)
    out = V.tieout(drop, eng, source="SRC")
    assert out == {"kind": "refusal", "reason": "not_yet_indexed", "what_to_do": "later"}      # the refusal is untouched
    assert seen["source"] == "SRC" and seen["program"]["currency"] == "USD"

    def cert(source, program):
        return {"kind": "certificate", "answer": {"parts": ["drop gross", "drop fees"], "value": engine.get(drop).settled["totals"]["net"]}}

    monkeypatch.setattr("closure.service.ask", cert)
    out = V.tieout(drop, eng, source="SRC")
    assert out["matches_engine"] is True and out["engine_net"] == engine.get(drop).settled["totals"]["net"]


def test_tieout_with_no_captures_says_so(engine, drop, fake):
    settled(engine, drop, 2)
    eng = type(engine)(engine.store, SandboxLike(fake), engine._clock)
    assert V.tieout(drop, eng)["reason"] == "nothing_captured"


def test_tieout_maps_transaction_search_not_having_the_window_yet_to_not_yet_indexed(engine, drop, fake, monkeypatch):
    from closure.paypal import PayPalError
    settled(engine, drop, 3)
    eng = type(engine)(engine.store, SandboxLike(fake), engine._clock)

    def boom(source, program):
        raise PayPalError("GET /v1/reporting/transactions -> http 404 INVALID_REQUEST")

    monkeypatch.setattr("closure.service.ask", boom)
    out = V.tieout(drop, eng, source="SRC")
    assert out["kind"] == "refusal" and out["reason"] == "not_yet_indexed" and "later" in out["what_to_do"].lower()
    monkeypatch.setattr("closure.service.ask", lambda s, p: (_ for _ in ()).throw(PayPalError("GET x -> http 500")))
    assert V.tieout(drop, eng, source="SRC")["reason"] == "transaction_search_unavailable"
