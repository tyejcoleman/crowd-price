"""The price ladder: pure functions, Decimal only, no I/O.

A ladder is a list of steps (min_buyers, price). Buyer thresholds strictly increase, prices strictly
decrease, and the first threshold is the drop's minimum. Every buyer authorises the START price (the
top of the ladder, the most they can ever pay); the price falls for everyone as the crowd crosses
thresholds, and a reached step stays locked.

Prices from `propose` sit on a whole-dollar grid, or a .50 grid when the range is too tight for
whole dollars. The fee model is the one observed in the PayPal sandbox (2.59% + 0.49 per charge,
rounded half-up to the cent: fee(32.00) = 1.32, fee(250.00) = 6.97).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
HALF = Decimal("0.50")
MAX_PRICE = Decimal("10000")       # no price (a step, or the start price) may exceed this
ONE = Decimal("1")
ZERO = Decimal("0")
MAX_STEPS = 8
DEFAULT_MARKUP = Decimal("2.25")   # start price = this x (unit cost + fixed cost / minimum), on the grid
LAST_STEP_RATIO = Decimal("0.75")  # the last step's price aims at this share of the start price


class LadderError(ValueError):
    """A ladder or its inputs are not acceptable. `code` is a stable slug."""

    def __init__(self, code: str, message: str = "", **detail):
        super().__init__(message or code)
        self.code, self.detail = code, detail


def D(x, what: str = "amount") -> Decimal:
    """Money in as Decimal. Floats go through str so 0.0259 stays 0.0259. Infinity and NaN are not numbers here."""
    if isinstance(x, bool) or x is None:
        raise LadderError("bad_number", f"{what} is not a number: {x!r}", field=what)
    if isinstance(x, Decimal):
        out = x
    else:
        try:
            out = Decimal(str(x).strip())
        except (InvalidOperation, ValueError):
            raise LadderError("bad_number", f"{what} is not a number: {x!r}", field=what) from None
    if not out.is_finite():
        raise LadderError("bad_number", f"{what} is not a finite number: {x!r}", field=what)
    return out


def check_price(x, what: str = "price") -> Decimal:
    """A price a buyer can be held or charged: finite, in whole cents (at most 2 decimal places), above 0 and at most
    MAX_PRICE. PayPal is given the amount as text with 2 decimals, so anything finer would make the engine and PayPal
    disagree about what was held. Raises LadderError('bad_price')."""
    p = D(x, what)
    if p <= 0 or p > MAX_PRICE or p != p.quantize(CENT):       # range first: quantize() chokes on 1E+30
        raise LadderError("bad_price", f"{what} must be a positive amount of at most 2 decimal places, no more than "
                                       f"{MAX_PRICE:,.0f}: got {x!r}", field=what, value=str(x)[:40])
    return p.quantize(CENT)


def money(x) -> Decimal:
    """Round to cents, half up."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FeeModel:
    pct: Decimal = Decimal("0.0259")
    fixed: Decimal = Decimal("0.49")

    def __post_init__(self):
        object.__setattr__(self, "pct", D(self.pct, "fee pct"))
        object.__setattr__(self, "fixed", D(self.fixed, "fee fixed"))

    def to_dict(self) -> dict:
        return {"pct": str(self.pct), "fixed": str(self.fixed)}

    @staticmethod
    def from_dict(d: dict) -> "FeeModel":
        return FeeModel(pct=d["pct"], fixed=d["fixed"])


DEFAULT_FEE = FeeModel()


def fee(amount, model: FeeModel = DEFAULT_FEE) -> Decimal:
    """PayPal's fee on one charge of `amount`, rounded half up to the cent."""
    a = D(amount)
    if a <= 0:
        return ZERO.quantize(CENT)
    return (a * model.pct + model.fixed).quantize(CENT, rounding=ROUND_HALF_UP)


def profit(n: int, price, unit_cost, fixed_cost, model: FeeModel = DEFAULT_FEE) -> Decimal:
    """n * price - n * fee(price) - fixed - n * unit_cost, in cents."""
    p = D(price)
    return (n * p - n * fee(p, model) - D(fixed_cost, "fixed cost") - n * D(unit_cost, "unit cost")).quantize(CENT)


@dataclass(frozen=True)
class Step:
    min_buyers: int
    price: Decimal
    profit: Decimal | None = field(default=None, compare=False)  # at exactly min_buyers (the worst case for this price)

    def to_dict(self) -> dict:
        d = {"min_buyers": self.min_buyers, "price": str(self.price)}
        if self.profit is not None:
            d["profit"] = str(self.profit)
        return d


@dataclass(frozen=True)
class Ladder:
    steps: tuple[Step, ...]
    start_price: Decimal | None = None  # what each buyer authorises; defaults to the first step's price

    def __post_init__(self):
        steps = tuple(s if isinstance(s, Step) else Step(int(s[0]), D(s[1], "price")) for s in self.steps)
        steps = tuple(Step(s.min_buyers, check_price(s.price, f"the price of step {i + 1}"), s.profit)   # 36 -> 36.00
                      for i, s in enumerate(steps))
        object.__setattr__(self, "steps", steps)
        sp = check_price(self.start_price, "the start price") if self.start_price is not None else (
            steps[0].price if steps else None)
        object.__setattr__(self, "start_price", sp)
        check_shape(self)

    @property
    def minimum(self) -> int:
        return self.steps[0].min_buyers

    def to_dict(self) -> dict:
        return {"start_price": str(self.start_price), "steps": [s.to_dict() for s in self.steps]}

    @staticmethod
    def from_dict(d: dict) -> "Ladder":
        return Ladder(tuple(Step(int(s["min_buyers"]), D(s["price"]),
                                 D(s["profit"]) if s.get("profit") is not None else None) for s in d["steps"]),
                      D(d["start_price"]))

    def spec(self) -> str:
        """The compact text form accepted by `parse`: '30:40,45:36,60:33'."""
        return ",".join(f"{s.min_buyers}:{_plain(s.price)}" for s in self.steps)


def _plain(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    return s


def check_shape(ladder: Ladder) -> None:
    steps = ladder.steps
    if not steps:
        raise LadderError("bad_ladder", "a ladder needs at least one step")
    if len(steps) > MAX_STEPS:
        raise LadderError("bad_ladder", f"at most {MAX_STEPS} steps", steps=len(steps))
    for i, s in enumerate(steps):
        if not isinstance(s.min_buyers, int) or s.min_buyers < 1:
            raise LadderError("bad_ladder", "buyer thresholds must be whole numbers of at least 1", step=i)
        check_price(s.price, f"the price of step {i + 1}")
        if i and s.min_buyers <= steps[i - 1].min_buyers:
            raise LadderError("bad_ladder", "buyer thresholds must strictly increase", step=i)
        if i and s.price >= steps[i - 1].price:
            raise LadderError("bad_ladder", "prices must strictly decrease", step=i)
    if ladder.start_price < steps[0].price:
        raise LadderError("bad_ladder", "the start price cannot be below the first step's price")


def parse(text: str, start_price=None) -> Ladder:
    """'30:40,45:36,60:33' -> Ladder. The first threshold is the minimum."""
    steps = []
    try:
        for part in str(text).split(","):
            n, p = part.strip().split(":")
            steps.append(Step(int(n), D(p, "price")))
    except (ValueError, LadderError):
        raise LadderError("bad_ladder", "write the ladder as buyers:price pairs, like 30:40,45:36,60:33",
                          text=str(text)[:80]) from None
    return Ladder(tuple(steps), D(start_price, "start price") if start_price is not None else None)


# ---- what a crowd of n buyers means ------------------------------------------------------------

def step_index(ladder: Ladder, n: int) -> int:
    """Highest step whose threshold n has reached; -1 below the minimum."""
    idx = -1
    for i, s in enumerate(ladder.steps):
        if n >= s.min_buyers:
            idx = i
    return idx


def price_at(ladder: Ladder, n: int, locked_step: int = -1) -> Decimal:
    """The price for everyone when n buyers hold and steps up to `locked_step` are locked.
    Prices only fall: a locked step is kept even if buyers leave."""
    idx = max(step_index(ladder, n), locked_step if locked_step is not None else -1)
    return ladder.start_price if idx < 0 else ladder.steps[idx].price


def next_step(ladder: Ladder, n: int, locked_step: int = -1) -> dict | None:
    """The next price drop: {step, more, price, min_buyers}. None at the bottom of the ladder."""
    idx = max(step_index(ladder, n), locked_step if locked_step is not None else -1)
    j = idx + 1
    if j >= len(ladder.steps):
        return None
    s = ladder.steps[j]
    return {"step": j, "more": max(0, s.min_buyers - n), "price": s.price, "min_buyers": s.min_buyers}


# ---- the maker's side: does each step make money -----------------------------------------------

def profit_table(ladder: Ladder, unit_cost, fixed_cost, model: FeeModel = DEFAULT_FEE) -> list[dict]:
    rows = []
    for i, s in enumerate(ladder.steps):
        f = fee(s.price, model)
        rows.append({"step": i, "min_buyers": s.min_buyers, "price": s.price, "fee_each": f,
                     "net_each": (s.price - f - D(unit_cost, "unit cost")).quantize(CENT),
                     "profit": profit(s.min_buyers, s.price, unit_cost, fixed_cost, model)})
    return rows


def validate(ladder: Ladder, unit_cost, fixed_cost, fee: FeeModel = DEFAULT_FEE, margin=0) -> Ladder:  # noqa: A002
    """Raise if the shape is wrong or any step loses money (profit below `margin`) at its threshold."""
    check_shape(ladder)
    need = D(margin, "margin")
    for row in profit_table(ladder, unit_cost, fixed_cost, fee):
        if row["profit"] < need:
            raise LadderError("ladder_loses_money",
                              f"step {row['step']} ({row['min_buyers']} buyers at {row['price']}) makes {row['profit']}",
                              step=row["step"], min_buyers=row["min_buyers"], price=str(row["price"]),
                              profit=str(row["profit"]))
    return ladder


# ---- proposing a ladder ------------------------------------------------------------------------

def _grid_ceil(x: Decimal, grid: Decimal = HALF) -> Decimal:
    return (x / grid).to_integral_value(rounding=ROUND_CEILING) * grid


def _grid_round(x: Decimal, grid: Decimal) -> Decimal:
    return (x / grid).to_integral_value(rounding=ROUND_HALF_UP) * grid


def break_even_price(n: int, unit_cost, fixed_cost, model: FeeModel = DEFAULT_FEE, margin=0) -> Decimal:
    """Lowest price on the .50 grid at which n buyers make at least `margin` of profit."""
    unit, fixed, m = D(unit_cost, "unit cost"), D(fixed_cost, "fixed cost"), D(margin, "margin")
    est = (unit + model.fixed + (fixed + m) / n) / (1 - model.pct)
    p = max(_grid_ceil(est) - 2 * HALF, HALF)  # start a little under the estimate, then climb exactly
    while profit(n, p, unit, fixed, model) < m:
        p += HALF
    return p


def _thresholds(minimum: int, steps: int) -> list[int]:
    out = [minimum]
    for k in range(1, steps):
        out.append(max(out[-1] + 1, int((Decimal(minimum) * (1 + Decimal(k) / 2)).to_integral_value(ROUND_HALF_UP))))
    return out


def propose(unit_cost, fixed_cost, minimum, start_price=None, steps: int = 4,
            fee: FeeModel = DEFAULT_FEE, margin=0) -> Ladder:  # noqa: A002 (spec name)
    """A ladder whose every step makes at least `margin` (dollars) of profit at its threshold.

    Thresholds are the minimum, then 1.5x, 2x, 2.5x ... of it. The top price is `start_price`, or
    DEFAULT_MARKUP times the cost per mug at the minimum. Prices fall in even strokes toward
    LAST_STEP_RATIO of the start price, never below a step's break-even. Fewer steps than asked come
    back when the price range is too tight to keep prices strictly falling."""
    model = fee
    unit, fixed, m = D(unit_cost, "unit cost"), D(fixed_cost, "fixed cost"), D(margin, "margin")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise LadderError("bad_number", "the minimum must be a whole number of buyers, at least 1", field="minimum")
    if not isinstance(steps, int) or not 1 <= steps <= MAX_STEPS:
        raise LadderError("bad_number", f"steps must be between 1 and {MAX_STEPS}", field="steps")
    if unit < 0 or fixed < 0 or m < 0:
        raise LadderError("bad_number", "costs and margin cannot be negative")

    ths = _thresholds(minimum, steps)
    floors = [break_even_price(n, unit, fixed, model, m) for n in ths]
    if start_price is None:
        top = max(_grid_ceil(DEFAULT_MARKUP * (unit + fixed / minimum)), floors[0])
    else:
        top = check_price(start_price, "the start price")      # refused, never rounded: PayPal must hold what was shown
        if top < floors[0]:
            raise LadderError("start_price_loses_money",
                              f"at {minimum} buyers a price below {floors[0]} loses money", floor=str(floors[0]),
                              start_price=str(top))
    end = max(_grid_ceil(top * LAST_STEP_RATIO), floors[-1])

    chosen = None
    for grid in (ONE, HALF):
        prices = [top]
        for k in range(1, len(ths)):
            raw = top - (top - end) * k / (len(ths) - 1) if len(ths) > 1 else top
            prices.append(max(_grid_round(raw, grid), floors[k]))
        # keep the longest strictly falling prefix
        keep = 1
        while keep < len(prices) and prices[keep] < prices[keep - 1]:
            keep += 1
        if keep == len(prices):
            chosen = prices
            break
        if chosen is None or keep > len(chosen):
            chosen = prices[:keep]
    prices = chosen
    n_steps = len(prices)
    rows = tuple(Step(ths[i], money(prices[i]), profit(ths[i], prices[i], unit, fixed, model)) for i in range(n_steps))
    return Ladder(rows, money(top))
