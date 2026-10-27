"""The Crowd Price Togi bundle (Togi SPEC section 2).

Every command is a thin wrapper over the engine; the AI that drives them never states a price. All prices
come back from a command, and the only money-moving command (`drop settle`, effect "money") is stopped by
the host until a person approves it. Error wording lives in togi_messages.toml (polish tier).
"""
import os
import tomllib
from decimal import Decimal
from functools import wraps

try:
    from togi import Bundle, TogiError
except ImportError:  # togi is not installed: a minimal stand-in so the module (and its tests) still import
    class TogiError(Exception):
        def __init__(self, code, message="", next=None, status="error"):
            super().__init__(message or code)
            self.code, self.message, self.next, self.status = code, message or code, next, status

    class Bundle:
        def __init__(self, name):
            self.name, self.commands = name, {}

        def command(self, words, *, effect="read", examples=(), summary=None, confirm=None):
            def deco(fn):
                self.commands[words] = {"func": fn, "effect": effect, "examples": list(examples), "confirm": confirm}
                return fn
            return deco

from . import ladder as lad
from . import verify as vf
from .engine import CrowdError, Engine, foreign_holds, m, plan_token
from .paypal_holds import FakePayPal, HoldError, mode_of, ref_of
from .store import Store, StoreError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MESSAGES_PATH = os.path.join(ROOT, "togi_messages.toml")
MAX_SIM = 60

bundle = Bundle("crowd")
EFFECTS: dict[str, str] = {}  # command words -> effect, kept here so tests can assert the guard levels


def _eng() -> Engine:
    if os.environ.get("TOGI_AGENT") == "stone" and os.environ.get("CROWD_PAYPAL", "").lower() != "fake":
        raise TogiError(*_say("stone_requires_fake", {}))
    try:
        return Engine()
    except HoldError as e:
        raise TogiError(*_say("paypal_not_configured" if e.code in ("paypal_not_configured", "not_sandbox", "bad_mode")
                              else "paypal_error", {"reason": str(e)})) from None


# ---- error wording ------------------------------------------------------------------------------------

_MSG_CACHE: dict = {}


def messages() -> dict:
    """togi_messages.toml, read fresh when the file changes (hone edits it)."""
    try:
        mtime = os.path.getmtime(MESSAGES_PATH)
        if _MSG_CACHE.get("mtime") != mtime:
            with open(MESSAGES_PATH, "rb") as f:
                _MSG_CACHE.update(data=tomllib.load(f), mtime=mtime)
        return _MSG_CACHE["data"]
    except (OSError, tomllib.TOMLDecodeError):
        return {"errors": {}, "explain": {}}


class _Fill(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def fill(template: str, ctx: dict) -> str:
    try:
        return str(template).format_map(_Fill({k: v for k, v in ctx.items()}))
    except (ValueError, IndexError, AttributeError):
        return str(template)


def _say(code: str, ctx: dict) -> tuple:
    """(code, message, next, status) for a TogiError from the message file."""
    ent = messages().get("errors", {}).get(code) or messages().get("errors", {}).get("unknown") or {}
    ctx = {**ctx, "reason": ctx.get("reason") or code.replace("_", " ")}
    msg = fill(ent.get("message", "{reason}"), ctx)
    nxt = fill(ent["next"], ctx) if ent.get("next") else None
    return code, msg, nxt, ent.get("status", "error")


def _togi_error(code: str, ctx: dict) -> TogiError:
    c, msg, nxt, status = _say(code, {k: str(v) for k, v in ctx.items()})
    return TogiError(c, msg, nxt, status)


def _guarded(fn):
    """Turn engine/ladder/store/PayPal failures into TogiErrors with message-file wording."""
    @wraps(fn)
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except TogiError:
            raise
        except CrowdError as e:
            raise _togi_error(e.code, {**e.ctx, **kw, "reason": str(e)}) from None
        except lad.LadderError as e:
            raise _togi_error(e.code, {**e.detail, **kw, "reason": str(e)}) from None
        except StoreError as e:
            raise _togi_error(e.code, {**e.ctx, **kw, "reason": str(e)}) from None
        except HoldError as e:
            code = "paypal_not_configured" if e.code == "paypal_not_configured" else (
                "paypal_unavailable" if e.code == "paypal_unavailable" else "paypal_error")
            raise _togi_error(code, {**kw, "issue": e.issue or e.code, "reason": str(e)}) from None
    return run


def cmd(words: str, effect: str, examples: list, **kw):
    EFFECTS[words] = effect

    def deco(fn):
        return bundle.command(words, effect=effect, examples=examples, **kw)(_guarded(fn))
    return deco


def _rows(rows: list[dict]) -> list[dict]:
    return [{k: str(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in rows]


# ---- the ladder ------------------------------------------------------------------------------------------

@cmd("ladder propose", "read", ["togi crowd ladder propose --unit-cost 9 --fixed-cost 260 --minimum 30"])
def ladder_propose(unit_cost: Decimal, fixed_cost: Decimal, minimum: int, start_price: Decimal | None = None,
                   steps: int = 4, margin: Decimal = Decimal("0")) -> dict:
    """Propose a price ladder from costs, with the profit at every step.

    Args:
        unit_cost: what one item costs to make, in dollars
        fixed_cost: one-off cost of the batch (a kiln firing, a print run), in dollars
        minimum: fewest buyers for which the drop goes ahead
        start_price: top price every buyer authorises; chosen from the costs when left out
        steps: how many price steps, including the first (default 4)
        margin: smallest profit, in dollars, every step must make at its threshold (default 0)
    """
    lg = lad.propose(unit_cost, fixed_cost, minimum, start_price, steps, margin=margin)
    table = lad.profit_table(lg, unit_cost, fixed_cost)
    out = {"ladder": lg.spec(), "start_price": m(lg.start_price), "minimum": lg.minimum,
           "steps": [{"step": i + 1, "from_buyers": r["min_buyers"], "price": m(r["price"]), "fee_each": m(r["fee_each"]),
                      "net_each": m(r["net_each"]), "profit_at_that_count": m(r["profit"])}
                     for i, r in enumerate(table)],
           "note": "Profit is at exactly the step's buyer count; more buyers at the same price earn more. "
                   "The engine, not the agent, chose these prices.",
           "next": f"togi crowd drop create --title \"<title>\" --minimum {lg.minimum} --hours <hours> --ladder {lg.spec()}"
                   f" --unit-cost {unit_cost} --fixed-cost {fixed_cost}"}
    if len(lg.steps) < steps:
        out["note"] += f" Only {len(lg.steps)} steps fit: the price range is too tight for {steps}."
    return out


# ---- a drop's life -----------------------------------------------------------------------------------------

@cmd("drop create", "write", ['togi crowd drop create --title "Speckled mugs" --minimum 30 --hours 48 --ladder 30:40,45:36,60:33',
                              'togi crowd drop create --title "Speckled mugs" --minimum 30 --hours 48 --from-proposal --unit-cost 9 --fixed-cost 260'])
def drop_create(title: str, minimum: int, minutes: int | None = None, hours: float | None = None,
                ladder: str | None = None, from_proposal: bool = False, unit_cost: Decimal | None = None,
                fixed_cost: Decimal | None = None, start_price: Decimal | None = None, steps: int = 4,
                margin: Decimal = Decimal("0"), maker: str = "maker", drop_id: str | None = None) -> dict:
    """Create a draft drop. Give a ladder, or costs with --from-proposal. Nothing is live until published.

    Args:
        title: what is being sold
        minimum: fewest buyers for which the drop goes ahead
        minutes: how long the drop runs once published (max 4320 = 72 h)
        hours: the same in hours; added to minutes if both are given
        ladder: buyers:price pairs like 30:40,45:36,60:33 (the first must equal the minimum)
        from_proposal: build the ladder from --unit-cost and --fixed-cost
        unit_cost: cost to make one item, in dollars
        fixed_cost: one-off batch cost, in dollars
        start_price: top price each buyer holds (with --from-proposal, or above the ladder's first price)
        steps: number of steps for --from-proposal
        margin: smallest profit per step for --from-proposal
        maker: who is selling
        drop_id: choose the id (d_ then letters or digits); left out, one is made up
    """
    if minutes is None and hours is None:
        raise _togi_error("duration_missing", {})
    duration = int(round((minutes or 0) + (hours or 0) * 60))
    if bool(ladder) == bool(from_proposal):
        raise _togi_error("ladder_conflict" if ladder else "ladder_required", {})
    if (unit_cost is None) != (fixed_cost is None):
        raise _togi_error("costs_incomplete", {})
    if from_proposal:
        if unit_cost is None:
            raise _togi_error("costs_incomplete", {})
        lg = lad.propose(unit_cost, fixed_cost, minimum, start_price, steps, margin=margin)
    else:
        lg = lad.parse(ladder, start_price)
    costs = {"unit_cost": unit_cost, "fixed_cost": fixed_cost} if unit_cost is not None else None
    eng = _eng()
    drop = eng.create_drop(title, lg, minimum, duration, maker=maker, costs=costs, drop_id=drop_id)
    out = {"drop": drop, "state": "draft", "title": title, "minimum": minimum, "ladder": lg.spec(),
           "start_price": m(lg.start_price), "duration_minutes": duration,
           "next": f"togi crowd drop publish --drop {drop}"}
    if costs:
        out["profit_at_each_step"] = [{"from_buyers": r["min_buyers"], "price": m(r["price"]), "profit": m(r["profit"])}
                                      for r in lad.profit_table(lg, unit_cost, fixed_cost)]
    return out


@cmd("drop publish", "write", ["togi crowd drop publish --drop d_7f3a"])
def drop_publish(drop: str) -> dict:
    """Make a draft drop live and start its clock; buyers can join from now."""
    out = _eng().publish(drop)
    out["next"] = f"togi crowd drop status --drop {drop}"
    return out


@cmd("drop list", "read", ["togi crowd drop list"])
def drop_list(state: str | None = None) -> list:
    """List drops, newest first, with their state and current price."""
    rows = _eng().list()
    if state:
        rows = [r for r in rows if r["state"] == state]
    return [{k: r.get(k) for k in ("drop", "title", "state", "price", "buyers", "minimum", "time_left")} for r in rows]


@cmd("drop status", "read", ["togi crowd drop status --drop d_7f3a"])
def drop_status(drop: str, verbose: bool = False) -> dict:
    """Show a drop's current price, buyers, locked step, next step, time left and state."""
    eng = _eng()
    d = eng.get(drop)
    out = eng.view(d)
    if verbose:
        counts: dict = {}
        for b in d.buyers.values():
            counts[b.status] = counts.get(b.status, 0) + 1
        out["buyers_by_status"] = counts
        out["profit_table"] = None
        if d.costs:
            out["profit_table"] = [{"from_buyers": r["min_buyers"], "price": m(r["price"]), "profit": m(r["profit"])}
                                   for r in lad.profit_table(d.ladder, d.costs["unit_cost"], d.costs["fixed_cost"])]
    return out


@cmd("drop join", "write", ["togi crowd drop join --drop d_7f3a --buyer \"Ada Lovelace\""])
def drop_join(drop: str, buyer: str, card: int = 0) -> dict:
    """Add a sandbox test buyer: places a hold at the starting price and recounts the price for everyone."""
    return _eng().join(drop, buyer, card)


@cmd("drop leave", "write", ["togi crowd drop leave --drop d_7f3a --buyer ada-lovelace"])
def drop_leave(drop: str, buyer: str) -> dict:
    """A buyer leaves: their hold is released at once. Steps already reached stay locked."""
    return _eng().leave(drop, buyer)


@cmd("drop exclude", "money", ["togi crowd drop exclude --drop d_7f3a --buyer ada-lovelace"])
def drop_exclude(drop: str, buyer: str) -> dict:
    """Take ONE buyer out of the crowd and release their hold. For a person getting past a single hold the engine cannot read or
    release (a block names the exact buyer). The plan changes afterwards, so a fresh plan is approved.
    A money effect: removing a buyer can cost the maker a sale or drop the crowd below its minimum, so a person approves it.

    Args:
        drop: the drop
        buyer: the buyer id the block or `buyers list` names
    """
    out = _eng().exclude(drop, buyer)
    out["next"] = out.get("next") or f"togi crowd drop plan --drop {drop}"
    return out


@cmd("drop close", "write", ["togi crowd drop close --drop d_7f3a"])
def drop_close(drop: str) -> dict:
    """Close a live drop: joining stops. Settlement is a separate step that needs a person."""
    out = _eng().close(drop)
    out["next"] = f"togi crowd drop plan --drop {drop}"
    return out


@cmd("drop plan", "read", ["togi crowd drop plan --drop d_7f3a"])
def drop_plan(drop: str) -> dict:
    """Show exactly what settling would do, in engine numbers. Moves nothing."""
    p = _eng().plan(drop)
    p["expect"] = _expect(p)
    if not p.get("already_settled"):
        p["checked"] = ("every remaining hold was read at PayPal just now (the authorisation and the captures on its order); "
                        "settle reads them again before any money moves and refuses if this plan no longer matches")
    p["next"] = f"togi crowd drop settle --drop {drop} --expect {p['expect']}"
    return p


def _expect(p: dict) -> str:
    """The plan as a short token, e.g. capture:52:33.00:1716.00 or void:22 (plus +release:K while K failed holds are
    still open). A person's approval is bound to it."""
    return plan_token(p)


def _refuse_test_scope_approval(eng: Engine, ctx) -> None:
    """Togi tells a command which approver signed the approval it is running on (`ctx.approval_scope`): "default" is the
    person's own approver, "test" a throwaway one under TOGI_APPROVER_HOME (tests, the stone). A throwaway approver's
    yes is worth nothing where real value could move, so a "test" approval is accepted only when ALL of these hold: the PayPal
    is the fake, CROWD_ENV is "test", and the store holds nothing but this fake's holds (`foreign_holds` is empty). The manifest
    opts Crowd in to test approvals (`[policy] test_approvals`) on the strength of this check.
    `getattr` so this works before and after the Togi that adds the attribute (None = no scope was reported)."""
    scope = getattr(ctx, "approval_scope", None)
    if scope != "test":
        return
    mode = mode_of(eng.holds)
    why = None
    if mode != "fake":
        why = f"the PayPal being used is {mode}, not the fake"
    elif os.environ.get("CROWD_ENV") != "test":
        why = "CROWD_ENV is not 'test'"
    else:
        foreign = foreign_holds(eng.store, ref_of(eng.holds))
        if foreign:
            why = (f"the store holds {len({(x['drop'], x['buyer']) for x in foreign})} hold(s) or drop(s) that did not come from "
                   f"this fake (first: drop {foreign[0]['drop']}, {foreign[0]['mode']})")
    if why:
        raise _togi_error("test_approval_outside_test_mode", {"scope": scope, "mode": mode, "why": why})


@cmd("drop settle", "money", ["togi crowd drop settle --drop d_7f3a --expect capture:52:33.00:1716.00"],
     summary="Settle a closed drop: capture every hold at the final price, or void them all (PayPal sandbox). The plan was "
             "checked against PayPal when made; settle re-checks every hold first.",
     confirm="drop")
def drop_settle(drop: str, expect: str, ctx=None) -> dict:
    """Settle a closed drop. Needs a person's approval; pass --expect from `drop plan`.

    Args:
        drop: the closed drop
        expect: the plan token from `togi crowd drop plan` (capture:HOLDS:PRICE:TOTAL or void:HOLDS, plus
            +release:K while K failed holds are still to be released); the approval is bound to it, so the person
            approves exactly what runs
    """
    eng = _eng()
    _refuse_test_scope_approval(eng, ctx)
    # the engine checks the PayPal, reads every remaining hold at PayPal, works the plan out again from that, under the lock, and
    # compares it with the approved one (`plan_changed`, with the new token, when it differs)
    out = eng.settle(drop, expect=expect.strip())
    out["next"] = f"togi crowd drop verify --drop {drop}"
    return out


@cmd("drop verify", "read", ["togi crowd drop verify --drop d_7f3a"])
def drop_verify(drop: str, verbose: bool = False) -> dict:
    """Check a settled drop against PayPal: every hold read back, amounts and totals compared."""
    out = vf.verify(drop, _eng(), verbose=verbose)
    if not verbose:
        out.pop("holds", None)
    if not out["ok"]:             # a problem names its own next step; "explain" is only the next step when nothing is wrong
        out["next"] = out.get("next") or f"togi crowd drop status --drop {drop}"
    else:
        out["next"] = f"togi crowd drop explain --drop {drop}"
    return out


@cmd("drop tieout", "read", ["togi crowd drop tieout --drop d_7f3a"])
def drop_tieout(drop: str) -> dict:
    """Prove the captures against PayPal's balance (needs PayPal to have indexed the window; may refuse)."""
    return vf.tieout(drop, _eng())


@cmd("drop explain", "read", ["togi crowd drop explain --drop d_7f3a"])
def drop_explain(drop: str) -> dict:
    """Explain a drop's state or outcome in plain words, built only from the engine's numbers."""
    eng = _eng()
    d = eng.get(drop)
    v = eng.view(d)
    state = v["state"]
    t = messages().get("explain", {})
    ctx = {**v, "title": d.title, "duration": human_minutes(d.duration_minutes), "time_left": v.get("time_left") or "no time",
           "next_message": v.get("next_message") or "It is at the lowest price on the ladder",
           "ladder": d.ladder.spec(), "buyers": v["buyers"]}
    if state == "live":
        key = "live_minimum_met" if v["minimum_met"] else "live"
    elif state == "closed":
        key = "closed_funded" if v["minimum_met"] else "closed_not_funded"
    elif state == "settling" and d.blocked:
        b = d.blocked
        ctx.update(blocked_issue=b.get("issue") or b.get("code") or "a refusal", blocked_debug_id=b.get("debug_id") or "none given",
                   blocked_phase=b.get("phase") or "settling", blocked_message=str(b.get("message") or "")[:200].rstrip("."))
        # the engine's own stop (a run of failed captures: no PayPal answer to quote) reads differently from a PayPal refusal
        key = "settling_blocked" if b.get("issue") or b.get("http") is not None else "settling_blocked_failures"
    elif state == "settled":
        s = d.settled
        c, tot = s["counts"], s["totals"]
        at_close = (d.settlement or {}).get("count", 0)
        funded = s["outcome"] == "funded"
        ctx.update(final_price=s["final_price"], captured=c["captured"], failed=c["failed"], voided=c["voided"],
                   holds=c["holds"], gross=tot["gross"], fees=tot["fees"], net=tot["net"], buyers_at_close=at_close,
                   released_each=m(d.ladder.start_price - lad.D(s["final_price"])) if funded else "0.00",
                   short_by=max(0, d.minimum - at_close), shortfall=s.get("shortfall", 0))
        ctx["unreleased"] = s.get("unreleased", 0)
        key = "settled_funded" if funded else ("settled_not_funded_open" if ctx["unreleased"] else "settled_not_funded")
    else:
        key = state if state in t else "live"
    text = fill(t.get(key, "{title} is {state}."), ctx)
    if state == "settled" and ctx.get("failed"):
        text += fill(t.get("failed_charges", ""), ctx)
        if ctx.get("shortfall"):
            text += fill(t.get("failed_shortfall", ""), ctx)
    if state == "settled" and ctx.get("unreleased") and key == "settled_funded":   # (a missed minimum says it already)
        text += fill(t.get("unreleased_holds", ""), ctx)
    facts = {k: ctx[k] for k in ("state", "price", "buyers", "minimum", "start_price", "final_price", "captured", "failed",
                                 "voided", "gross", "fees", "net") if k in ctx}
    return {"drop": drop, "state": state, "text": text, "facts": facts}


def human_minutes(n: int) -> str:
    h, mins = divmod(int(n), 60)
    return f"{h}h {mins:02d}m" if h and mins else (f"{h} hours" if h else f"{mins} minutes")


# ---- people and simulation ------------------------------------------------------------------------------------

@cmd("buyers list", "read", ["togi crowd buyers list --drop d_7f3a"])
def buyers_list(drop: str) -> list:
    """List a drop's buyers with their hold or charge state."""
    out = []
    for b in _eng().buyers(drop):
        row = {"buyer": b["buyer"], "status": b["status"], "held": b["held"], "charged": b["charged"],
               "capture_id": b["capture_id"], "error": b["error"]}
        out.append({k: v for k, v in row.items() if v is not None})
    return out


@cmd("sim join", "write", ["togi crowd sim join --drop d_7f3a --count 45"])
def sim_join(drop: str, count: int) -> dict:
    """Join N simulated sandbox buyers (for demos and tests, at most 60 at a time)."""
    if not 1 <= count <= MAX_SIM:
        raise _togi_error("too_many", {"max": MAX_SIM, "count": count, "drop": drop})
    eng = _eng()
    d = eng.get(drop)
    n = max([int(b.id.split("-")[1]) for b in d.buyers.values() if b.id.startswith("sim-") and b.id[4:].isdigit()] or [0])
    joined = declined = 0
    last = None
    for i in range(n + 1, n + count + 1):
        try:
            last = eng.join(drop, f"Sim {i:03d}", card_index=i % 8)
            joined += 1
        except CrowdError as e:
            if e.code != "join_declined":
                raise
            declined += 1
    out = {"drop": drop, "joined": joined, "declined": declined}
    out.update({k: (last or eng.view(eng.get(drop))).get(k) for k in ("price", "buyers", "locked_step", "next_message")})
    return out


def _require_test_sandbox(what: str) -> None:
    """Test tooling runs only where nothing real can be touched: CROWD_ENV=test, the fake PayPal, and no agent. An agent
    session (TOGI_AGENT set, even empty) never gets to settle or wipe anything this way; the money path for an agent is
    `drop settle`, which waits for a person."""
    if os.environ.get("CROWD_ENV") != "test":
        raise _togi_error("not_test_env", {})
    if os.environ.get("CROWD_PAYPAL", "").lower() != "fake":
        raise _togi_error("stone_requires_fake", {})
    if "TOGI_AGENT" in os.environ:
        raise _togi_error("test_tools_not_for_agents", {"command": what})


def _require_only_fake_holds(store: Store) -> None:
    """The store must hold nothing but this fake's holds: a sandbox hold (or one of unknown origin) means real money may be
    on a card, and no test command may settle or erase it."""
    foreign = foreign_holds(store, FakePayPal(store.path + ".fakepaypal.json").ref)
    if foreign:
        f = foreign[0]
        n = len({(x["drop"], x["buyer"]) for x in foreign})
        raise _togi_error("store_has_real_holds", {"count": n, "drop": f["drop"], "mode": f["mode"]})


@cmd("test settle", "admin", ["togi crowd test settle --drop d_mugs06"])
def settle_without_approval(drop: str) -> dict:
    """Settle a closed drop with no approval. Test setup only: needs CROWD_ENV=test, the fake PayPal, a store of fake holds
    only, and no agent session."""
    _require_test_sandbox("test settle")
    eng = _eng()
    _require_only_fake_holds(eng.store)
    return eng.settle(drop)


@cmd("test reset", "admin", ["togi crowd test reset"])
def reset_test_store() -> dict:
    """Wipe the local store and the fake PayPal. Needs CROWD_ENV=test, the fake PayPal, a store of fake holds only, and no
    agent session."""
    _require_test_sandbox("test reset")
    store = Store()
    _require_only_fake_holds(store)
    n = len(store.drop_ids())
    store.reset()
    FakePayPal(store.path + ".fakepaypal.json").reset()
    return {"reset": True, "drops_removed": n, "store": os.path.basename(store.path)}
