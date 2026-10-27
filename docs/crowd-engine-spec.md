# Crowd Price engine + Togi bundle: spec v0.1 (2026-10-27)

Product context: `docs/crowd-price.html`. Sandbox evidence: `docs/DECISIONS.md` F-08.
Togi contract (how commands, effects, approvals, skills and the manifest work): `~/Development/togi/SPEC.md` §2.

**Repo rules still apply (CLAUDE.md):**
- PayPal SANDBOX only.
- Never commit secrets.
- Don't edit `src/closure/**`: import from it.
- `harness/guard.sh --full` must pass.
- The AI never states or sets a price; only the engine's ladder does.

## 1. What it does

A maker opens a **drop**:
1. Buyers join by authorising a PayPal hold at the starting price.
2. The price steps down for everyone as the number of buyers crosses ladder steps, and a reached
   step is locked.
3. At close, if the minimum was reached, every hold is captured at the final (lowest reached)
   price, each capture marked final. Otherwise every hold is voided.
4. A settlement check proves the outcome.

## 2. Modules (`src/crowd/`)

**`ladder.py`: pure functions.**
- `propose(unit_cost, fixed_cost, minimum, start_price=None, steps=4, fee=FeeModel(pct=0.0259, fixed=0.49), margin=0) -> Ladder`
  - Steps are `(min_buyers, price)`: buyer thresholds strictly increasing, prices strictly
    decreasing.
  - The first threshold is `minimum`.
  - Prices are rounded to whole dollars, or .50.
  - Each step returns its profit at that count:
    `n*price - n*fee(price) - fixed - n*unit_cost`.
- `validate(ladder, unit_cost, fixed_cost, fee)` raises if any step loses money.
- Also: `price_at(ladder, n, locked_step)` and `fee(amount)` (Decimal, rounded half-up to cents;
  fee(32.00) must equal 1.32 and fee(250.00) must equal 6.97, both observed in sandbox).

**`store.py`: SQLite, path `data/crowd.db` by default** (`data/` is git-ignored; override with env
`CROWD_DB`).
- An append-only `events` table: `id, ts, drop_id, type, payload_json`.
- State is a projection rebuilt from events (`project(drop_id)`), never stored separately.
- Event types:
  - drop: `drop_created`, `drop_published`, `step_locked`, `drop_closed`, `settlement_started`
  - buyer: `buyer_joined`, `buyer_left`
  - holds: `hold_placed`, `hold_failed`, `hold_captured`, `capture_failed`, `hold_voided`
  - outcome: `drop_settled`

**`paypal_holds.py`: thin client on `closure.paypal.Client` (token, base URL, CTX).**
- `authorize(amount, currency, label, request_id, card=TEST_CARDS[i])` is Orders v2
  `intent: AUTHORIZE` with a card payment_source. It returns
  `{order_id, auth_id, status, expires}`.
- `capture(auth_id, amount, request_id, final=True)`.
- `void(auth_id, request_id)`.
- `get(auth_id)`.
- Every call sends a deterministic `PayPal-Request-Id` (`crowd-<drop>-<buyer>-<action>`), so a
  retry never doubles money.
- PayPal error names map to clean exceptions: `MAX_CAPTURE_AMOUNT_EXCEEDED`,
  `AUTHORIZATION_ALREADY_CAPTURED`, `AUTHORIZATION_VOIDED`, `REAUTHORIZATION_TOO_SOON`, card
  declined, and so on.
- Refuses to run unless `PAYPAL_ENV=sandbox`.

**`engine.py`: the only code that moves money. No AI. It is the locked tier.**
- `create_drop(title, ladder, minimum, duration_minutes, currency="USD", maker="...")` makes a
  draft. Duration must be ≤ 72 h, because PayPal's honour period is 3 days.
- `publish(drop)` takes it from draft to live; `ends_at` = now + duration.
- `join(drop, buyer_name, card_index=0)`:
  - only while live and before `ends_at`;
  - authorises the **starting price** (the top of the ladder);
  - records the hold, recounts, and locks any newly reached step;
  - returns the new price and the next step ("N more and it's $X").
- `leave(drop, buyer)`: voids that buyer's hold. Locked steps stay locked.
- `close(drop)`: stops joining (also implied once `now >= ends_at`).
- `settle(drop)`: closed drops only. It is idempotent and resumable.
  - **Minimum met:** capture each held authorisation at the final price, with
    `final_capture=True`.
  - **Minimum missed:** void each held authorisation.
  - A capture that fails is recorded as `capture_failed`. The buyer is excluded and the locked
    price is kept for everyone else.
  - Ends with `drop_settled{outcome: funded|not_funded, final_price, counts, totals}`.
- State-machine guards: no capture after a void, no double capture, no settle before close, no
  join after close. Each guard raises a clean error code.

**`verify.py`**
- `verify(drop)` gives an immediate proof from the Payments API:
  - `GET` every authorisation;
  - engine says captured ⇒ PayPal says CAPTURED, with a capture amount equal to the final price;
  - engine says voided ⇒ PayPal says VOIDED;
  - holds = captured + voided + failed;
  - totals: gross, fees from the captures' `seller_receivable_breakdown`, net.
  - Returns `{ok, checks: [...], problems: [...]}`.
- `tieout(drop)` gives the balance proof once PayPal has indexed the window:
  - uses `closure.certify.certify` with a program whose parts are this drop's capture ids (gross
    and fee) plus a rest part;
  - returns the certificate, or the certifier's refusal (e.g. `not_yet_indexed`) as is.
  - Never loosen the certifier.

**`togi_bundle.py`: the Togi bundle, per the contract.** Commands (effect in brackets):
- `ladder propose` [read]: `--unit-cost --fixed-cost --minimum [--start-price] [--steps]`. Steps
  plus a profit table.
- `drop create` [write]: `--title --minimum --minutes [--hours]`, and either `--ladder "30:40,45:36,60:33"`
  or `--from-proposal` with the same cost flags.
- `drop publish` [write], `drop list` [read], `drop status` [read].
  - Status shows price now, buyers, locked step, next step, time left, state.
- `drop join` [write]: `--drop --buyer [--card N]`. A sandbox test buyer.
- `drop leave` [write], `drop close` [write].
- `drop settle` [**money**]: needs approval. Its summary says exactly what will happen, e.g.
  "capture 52 holds at $33.00 = $1,716.00" or "void 22 holds, nobody pays".
- `drop verify` [read], `drop tieout` [read], `drop explain` [read]:
  - `explain` gives a plain-language outcome built only from engine numbers. Template text lives in
    `togi_messages.toml`.
- `buyers list` [read], `sim join` [write]: `--drop --count N` (N ≤ 60) for demos and stone setup.
- `test reset` [admin]: wipes the local store only, and only when `CROWD_ENV=test`.

**Errors:** every error is a `TogiError` with a stable code. Its message text comes from
`togi_messages.toml` (polish tier, so hone can improve wording without touching code). Every error
gives a `next` command.

## 3. Bundle files at the repo root

- `togi.toml`: tiers and policy as in the Togi SPEC example.
  - polish: `skills/**`, `togi_messages.toml`
  - reshape: `src/crowd/ladder.py`
  - locked: the manifest, `stone/**`, `src/crowd/engine.py`, `src/crowd/paypal_holds.py`,
    `src/crowd/settle*`, `src/crowd/verify.py`, `src/closure/**`
  - policy: `approve_effects = ["money"]`.
- `skills/SKILL.md`: what Crowd does, the normal workflow, the hard rules, the topic list. ≤ 60 lines.
  - Workflow: propose ladder → create → publish → watch → close → settle (needs a person) → verify → explain.
  - Hard rules: never state a price that didn't come from a command; never try to approve; settle
    only after close.
- Topics: `skills/ladder.md`, `skills/settle.md`, `skills/troubleshooting.md`.
- `stone/tasks/*.toml`: 5–6 agent tasks; at least 2 are holdout. All forbid `togi approve`.
  - open a drop from costs;
  - report the price after N joins;
  - close and request settlement, ending at needs_approval with the right summary;
  - a drop that misses its minimum;
  - a buyer leaves after a step locked;
  - (holdout) explain an outcome to the maker;
  - (holdout) recover from a wrong flag.
  - The tasks use `CROWD_ENV=test` and a fake-PayPal mode (below) so the stone is fast and free.
- `demo/maker_agent.py`: the demo AI client.
  - Runs `claude -p` with a maker scenario (Juniper the potter) and Bash limited to `togi`.
  - Streams a readable transcript to the terminal.
  - Flags: `--model`, `--scenario`, `--fake`.
- `demo/README.md`: how to run the demo end to end (live sandbox and fake mode).

## 4. Fake PayPal (for tests and the stone)

`CROWD_PAYPAL=fake` swaps in `paypal_holds.FakePayPal`. Its behaviour is in-memory, persisted to
the store dir. It mirrors F-08 exactly:
- authorise → CREATED;
- capture ≤ amount, final → CAPTURED (fee = 2.59% + 0.49);
- over-capture → MAX_CAPTURE_AMOUNT_EXCEEDED;
- second capture → AUTHORIZATION_ALREADY_CAPTURED;
- capture after void → AUTHORIZATION_VOIDED;
- `decline_next` / card index 9 → declined at capture, for testing flow E.

## 5. Tests (`tests/crowd/`)

**Offline, using fake PayPal:**
- ladder math and validation (including the 1.32 and 6.97 fee checks);
- projection rebuild;
- every state-machine guard;
- step locking, including after a leave;
- funded and not-funded settlement, settle idempotency and resume after a crash mid-settle;
- capture failure handling;
- verify problems detected;
- bundle command shapes;
- the money effect → needs_approval → approved flow (via the togi test helpers, once togi is
  installed).

**Live** (`CROWD_LIVE=1`, opt-in): one small funded drop (3 buyers, min 2) and one not-funded
drop, against the sandbox, then `verify`.

## 6. Fix round 1 (2026-10-27): what the engine now guarantees

See `docs/crowd-fixes-r1.md` for the list and `docs/DECISIONS.md` F-12 for the reasons and the open probes.

- **Events added:** `join_started`, `order_created`, `orphan_hold_released`, `void_failed`. A join is written down before
  PayPal is asked; `hold_placed`, `hold_failed` or `orphan_hold_released` closes it. Every event that records something
  PayPal did or refused carries `paypal_mode` (and the fake's `paypal_ref`); failures also carry PayPal's error name
  (`issue`), `http` and `debug_id`.
- **`reconcile(drop)`** runs at the start of `close` and `settle`: a hold found on an unconfirmed join is recorded if the
  drop can still take it, else voided; nothing found is recorded as `hold_failed{join_unconfirmed}`.
- **`settle(drop, expect=None)`** reconciles, then (with `expect`) re-derives the plan under its lock and refuses a
  different one. Plan token: `capture:N:P:T` or `void:N`, plus `+release:K` while K failed holds are still open. A PayPal
  refusal for one buyer is recorded (`capture_failed` / `void_failed`) and settle goes on; only network trouble, 408, 429
  and 5xx stop it. Holds past their `expires` are never captured (`capture_failed{hold_expired}`).
- **Clock:** `leave` is refused at and after `ends_at`; a drop closed late counts its crowd as of `ends_at`.
- **Prices:** every price is finite, at most 2 decimal places, above 0 and at most 10000, else refused.
- **`verify`** also fails on: an unconfirmed join; an open PayPal authorisation on the drop's orders that the log does not
  hold; a hold whose PayPal amount differs from the engine's; a `void_failed` hold PayPal still shows as open; a failed
  capture with no PayPal evidence; events recorded against a different PayPal.
- **`test settle` / `test reset`:** CROWD_ENV=test, CROWD_PAYPAL=fake, TOGI_AGENT unset, and a store whose holds were all
  placed by this store's own fake.
