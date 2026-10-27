from decimal import Decimal

import pytest

from crowd import ladder as L

D = Decimal


def test_fee_matches_the_sandbox_observations():
    assert L.fee(D("32.00")) == D("1.32")      # F-08: capture of 32.00 -> fee 1.32
    assert L.fee(D("250.00")) == D("6.97")     # observed earlier; 6.965 rounds half UP
    assert L.fee(32) == D("1.32") and L.fee("32") == D("1.32")
    assert L.fee(0) == D("0.00")


def test_fee_model_takes_floats_without_drift():
    m = L.FeeModel(pct=0.0259, fixed=0.49)
    assert m.pct == D("0.0259") and m.fixed == D("0.49")
    assert L.fee(D("32.00"), m) == D("1.32")


def test_profit_formula():
    # n*price - n*fee(price) - fixed - n*unit_cost
    assert L.profit(30, 40, 9, 260) == D("30") * 40 - 30 * D("1.53") - 260 - 30 * 9
    assert L.profit(30, 40, 9, 260) == D("624.10")


def test_propose_example_is_a_clean_four_step_ladder():
    lg = L.propose(9, 260, 30)
    assert lg.spec() == "30:40,45:37,60:33,75:30"
    assert lg.steps[0].min_buyers == 30            # first threshold is the minimum
    assert lg.start_price == D("40.00")
    assert [s.profit for s in lg.steps] == [D("624.10"), D("934.75"), D("1099.60"), D("1219.75")]
    L.validate(lg, 9, 260)


@pytest.mark.parametrize("unit,fixed,minimum,start,steps,margin", [
    (9, 260, 30, None, 4, 0), (9, 260, 30, 40, 6, 100), (0, 0, 1, 10, 4, 0), (1, 0, 2, 5, 4, 0),
    (10, 0, 5, 13, 4, 0), (3.5, 80, 12, None, 5, 25), (0.25, 5, 100, None, 8, 0), (20, 1000, 8, 160, 3, 0),
])
def test_proposed_ladders_are_strictly_ordered_on_a_grid_and_never_lose_money(unit, fixed, minimum, start, steps, margin):
    lg = L.propose(unit, fixed, minimum, start, steps, margin=margin)
    assert 1 <= len(lg.steps) <= steps
    assert lg.steps[0].min_buyers == minimum
    ths = [s.min_buyers for s in lg.steps]
    prices = [s.price for s in lg.steps]
    assert ths == sorted(set(ths)) and prices == sorted(set(prices), reverse=True)
    assert all(p % D("0.50") == 0 for p in prices)                          # whole dollars or .50
    assert all(p >= D("0.50") for p in prices)
    for s in lg.steps:                                                       # every step pays at its threshold
        assert s.profit >= margin
        assert s.profit == L.profit(s.min_buyers, s.price, unit, fixed)
    L.validate(lg, unit, fixed, margin=margin)
    if start is not None:
        assert lg.start_price == D(str(start)).quantize(D("0.01"))


def test_propose_refuses_a_start_price_that_loses_money_at_the_minimum():
    with pytest.raises(L.LadderError) as e:
        L.propose(9, 260, 30, start_price=10)
    assert e.value.code == "start_price_loses_money" and e.value.detail["floor"] == "19.00"


def test_propose_rejects_nonsense():
    for bad in ({"minimum": 0}, {"minimum": 2.5}, {"steps": 0}, {"steps": 99}):
        kw = {"unit_cost": 1, "fixed_cost": 1, "minimum": 5, **bad}
        with pytest.raises(L.LadderError):
            L.propose(**kw)
    with pytest.raises(L.LadderError):
        L.propose(-1, 0, 5)
    with pytest.raises(L.LadderError):
        L.propose("abc", 0, 5)


def test_tight_ranges_return_fewer_steps_not_a_broken_ladder():
    lg = L.propose(1, 0, 2, start_price=3, steps=4)
    assert len(lg.steps) < 4
    assert [s.price for s in lg.steps] == sorted({s.price for s in lg.steps}, reverse=True)


def test_validate_raises_when_any_step_loses_money():
    lg = L.parse("30:40,45:20")               # 45 buyers at 20: 45*(20-1.01-9)=449.55 - 260 > 0 ok
    L.validate(lg, 9, 260)
    bad = L.parse("30:40,45:12")              # 45*(12-0.80-9) = 99 - 260 < 0
    with pytest.raises(L.LadderError) as e:
        L.validate(bad, 9, 260)
    assert e.value.code == "ladder_loses_money" and e.value.detail["step"] == 1


def test_shape_rules():
    for text in ("30:40,30:36", "30:40,45:41", "30:36,20:30", "0:40", "30:0", "30:-5", "x", "", "30:40,"):
        with pytest.raises(L.LadderError):
            L.parse(text)
    with pytest.raises(L.LadderError):
        L.Ladder((L.Step(30, D("40")),), start_price=D("30"))     # start price below the first step
    ok = L.Ladder((L.Step(30, D("36")), L.Step(45, D("33"))), start_price=D("40"))   # hold 40, 36 at the minimum
    assert ok.minimum == 30 and L.price_at(ok, 5) == D("40.00") and L.price_at(ok, 30) == D("36.00")


def test_price_at_and_locked_steps():
    lg = L.parse("30:40,45:36,60:33")
    assert [L.price_at(lg, n) for n in (0, 29, 30, 44, 45, 59, 60, 500)] == \
        [D(x) for x in ("40.00", "40.00", "40.00", "40.00", "36.00", "36.00", "33.00", "33.00")]
    # a locked step holds even when the crowd shrinks
    assert L.price_at(lg, 10, locked_step=1) == D("36.00")
    assert L.price_at(lg, 50, locked_step=2) == D("33.00")
    assert L.price_at(lg, 50, locked_step=0) == D("36.00")           # the crowd is ahead of the lock


def test_next_step():
    lg = L.parse("30:40,45:36,60:33")
    assert L.next_step(lg, 43) == {"step": 1, "more": 2, "price": D("36.00"), "min_buyers": 45}
    assert L.next_step(lg, 0)["step"] == 0 and L.next_step(lg, 0)["more"] == 30
    assert L.next_step(lg, 60) is None
    assert L.next_step(lg, 10, locked_step=2) is None


def test_roundtrip():
    lg = L.propose(9, 260, 30)
    assert L.Ladder.from_dict(lg.to_dict()) == lg
    assert L.parse(lg.spec()).spec() == lg.spec()
    assert L.parse("30:36,45:33", start_price=40).start_price == D("40")
