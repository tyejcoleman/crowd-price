# Crowd Price engine + Togi bundle: spec v0.1 (2026-10-27)

Product context: the README. Sandbox evidence: `docs/DECISIONS.md` F-08.
Togi contract (how commands, effects, approvals, skills and the manifest work): Togi's `SPEC.md` §2.

**Rules:**
- PayPal SANDBOX only.
- Never commit secrets.
- Don't edit `src/closure/**`: import from it.
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
`CROWD_DB`; the fake PayPal's default is `data/crowd-fake.db`, see section 8).
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
- Every call sends a deterministic `PayPal-Request-Id` (`crowd-<drop>.<buyer>.<action>[.<attempt>]`, §10), so a
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

## 7. Fix round 2 (2026-10-27): ask PayPal, or stop

See `docs/crowd-fixes-r2.md` for the list and `docs/DECISIONS.md` D-11 and F-13 for the reasons and the sandbox probes.
Principle: never infer PayPal's state from our own clock or from an error class when PayPal can be asked; when unsure, stop
and hand it to a person.

- **Events added:** `leave_started`, `leave_failed`, `settle_blocked`. `hold_voided` also records what PayPal said at close
  (`reason: paypal_says_<status>`); `hold_placed` carries `replayed` and `confirmed_status` when it came from a replay.
- **Refusals** are classified by PayPal's names on an allow-list (`paypal_holds.buyer_specific`). Buyer-specific: recorded
  (`capture_failed` / `void_failed`) and settle goes on. Everything else, including every 401 and 403: `settle_blocked`, nothing
  more captured, voided or released, the drop stays `settling`, and the refusal's `next` names a person. Two capture failures
  in a row with the same code stop it the same way. Failed holds are released last, and only when nothing blocked.
- **`close`** reconciles joins and leaves, then GETs every held authorisation and drops any that is not CREATED or PENDING from
  the crowd; the funded decision, the price and the plan come from that crowd.
- **`leave`** is written down before PayPal is asked; `reconcile` resolves it by reading the authorisation.
- **`join`** reads the clock once; a replayed or non-open answer is confirmed with a GET before it is recorded; a refused retry
  or a retry on another card first resolves (releases) the earlier unconfirmed try.
- **Expiry:** our clock only prompts a question. A hold is expired when PayPal says EXPIRED; otherwise it is tried.
- **`verify`** also checks capture currency and parent authorisation, released orphans, unconfirmed leaves and a blocked settle;
  every problem has a `next`.
- **`drop settle`** refuses an approval of scope "test" unless the PayPal is the fake (`ctx.approval_scope`).

## 8. Fix round 3 (2026-10-27): one store, one PayPal; ask again before money moves

See `docs/crowd-fixes-r3.md` for the list and `docs/DECISIONS.md` D-11b and F-15 for the reasons and the sandbox probes. Principle:
the engine trusts neither the PayPal this process happens to use nor its own records when PayPal can be asked right before money moves.

- **Events added:** `buyer_excluded`. `drop_created` also records `paypal_mode` / `paypal_ref`; `hold_voided` may carry `capture_ids`
  and `taken_outside_engine`; `orphan_hold_released` / `void_failed` may carry `superseded` (an earlier order of the same join intent).
- **One store, one PayPal.** `Engine.require_paypal(drop)` runs first in join, leave, publish, close, reconcile, plan, settle, exclude and
  `verify`: a mismatch of mode or ref with anything the drop recorded is `wrong_paypal`, with nothing written. A new drop is refused in a store
  that holds another PayPal's drops. Default store path: `data/crowd.db` (sandbox), `data/crowd-fake.db` (`CROWD_PAYPAL=fake`),
  `$TOGI_HOME/state/crowd/crowd-test.db` (`CROWD_ENV=test` under Togi). `drop settle` takes a "test"-scope approval only for the fake with
  `CROWD_ENV=test` and a store whose `foreign_holds` is empty. `togi.toml`: `[policy] test_approvals = true`, `require_lock = true`.
- **Asked again.** `plan` and `settle` read every held authorisation and the captures on its order (`order_captures`, new on both
  clients). Not open, or captured: out of the crowd, recorded with PayPal's word; a stray capture is for a person to refund. `settle`
  re-derives the plan from that crowd and refuses a different one with `plan_changed` and the new token. An unreadable hold (403) stops
  `close`/`plan` (`hold_unreadable`) or blocks `settle`; `drop exclude --drop D --buyer B` [write] resolves it. `verify` also lists every
  capture on every order of the drop.
- **Classification and breaker.** `buyer_specific` reads the top-level `name` and each `details[]` entry (`HoldError.name`,
  `.detail_names`). Two capture failures in a row of any buyer codes, or more than half failing once four were tried, stop settle; checked
  from the whole history at the start of every settle. `AUTHORIZATION_EXPIRED` / `hold_expired` do not count.
- **Recovery.** `_recover` raises `paypal_unavailable` on an outage and blocks on a refusal; only a 404 means none. A join retry that gets a
  different order is a new hold: the earlier order's hold is released first (D-11).
- **Leaves (D-11b).** An unresolved or refused leave stands; close and settle retry the void and the buyer is out either way.

## 9. Fix round 4 (2026-10-27): ask just before sending; a name is a claim; lock the whole package

See `docs/crowd-fixes-r4.md` for the list and `docs/DECISIONS.md` F-16 for the reasons and the probes still to run on the sandbox.
Principle: the engine acts on what PayPal says a moment ago, never on what the same hold looked like when settlement started, what
our clock says about its date, or what an error is called.

- **Events added:** `capture_started` (written before PayPal is asked to capture a hold: buyer, auth id, amount, deterministic request
  id) and `paypal_bound`. Commands added: `drop holds` [read] and `drop bind` [money]. The drop projection has `capturing`.
- **Whose capture is it (item 1).** `_capture_one` reads the hold (the authorisation and the captures on its order) first. Open with
  nothing taken: the attempt is written down, then the capture sent. A capture already there is the engine's own (`_could_be_ours`)
  only if settlement is funded, the authorisation is CAPTURED (the engine's capture is final, so PARTIALLY_CAPTURED never is), a
  `capture_started` exists for that hold at the final price, exactly one completed capture is on it at exactly the final price, not
  marked other than final, and, where PayPal says which request made it, that is the attempt's request id. Then it is recorded
  (`hold_captured{recovered}`), not made again. Any other capture is somebody else's: `hold_voided{taken_outside_engine, capture_ids}`,
  the hold leaves the crowd, nothing is ever POSTed on top of it, and `verify` names the capture to refund. Our clock is not read.
  An ALREADY_CAPTURED answer to a capture sends it through the same look again. The fake enforces PayPal's cap (all captures on one
  authorisation add up to at most 115% of the hold; a non-final capture leaves PARTIALLY_CAPTURED and room for another) and says
  which request made each capture (`request_id`; the real API does not say, and `_capture_view` leaves it empty).
- **`drop exclude` (item 2)** takes `ctx` and refuses a throwaway approver's yes outside test mode exactly as settle does; so does
  `drop bind`. Every command the manifest gates calls `_refuse_test_scope_approval` first; `togi_bundle.COMMANDS` and a test check it.
- **The clock (item 3).** `join` and `leave` read it before waiting for the drop lock. That instant decides the deadline and stamps
  the events, so a request asked in time stands however long it queued behind another request.
- **A name is a claim (item 4).** A capture refused as AUTHORIZATION_EXPIRED is followed by a GET; only an EXPIRED answer
  (`capture_failed{paypal_status: EXPIRED}`) keeps it out of the breaker. If PayPal shows the hold open, or will not say, it counts.
- **Several orders (item 5).** `join` releases every earlier order of the join intent whenever more than one is on record, replay or not.
- **Pending leaves (item 6).** `_reconcile_leaves` answers a 404 as `_revalidate` does (nothing is held: the buyer left) and a 403 with
  `hold_unreadable` and the `drop exclude` way out (settle: `settle_blocked`). Never a bare `paypal_error`.
- **Unbound drops (item 7).** A drop whose events record no PayPal is refused by every operation (`drop_unbound`, nothing written, no PayPal
  asked) until a person runs `drop bind --drop D --expect <token>`. `drop holds` lists the holds the log says the drop placed, each read
  at the PayPal in use, with the token `bind:MODE:REF:HOLDS:DIGEST`; `bind` reads them again and refuses (`bind_mismatch`) if that PayPal
  does not know one, else records `paypal_mode` and `paypal_ref`. `tieout` and `tieout_program` also require the PayPal.
- **Refunds (item 8).** A `taken` hold clears in `verify` once every capture the engine named reads REFUNDED (and not otherwise); a
  PARTIALLY_REFUNDED one fails with `refund the remaining N`. The order audit counts only what is left of a partly refunded capture.
- **The lock (item 9).** `togi.toml` locks every file of the crowd package except `src/crowd/ladder.py` (reshape), including
  `__init__.py`. `tests/crowd/test_fixes_r4.py` fails for a `.py` under `src/crowd/` that is neither, and for a crowd module the bundle
  imports that is not covered. After this round the repo's own lock (`.togi/locked.sha256`) no longer verifies: a person re-locks.

## 10. Fix round 5 (2026-10-27): judge a request by when it was asked; leave nothing half open; the fake proves no more than PayPal does

See `docs/crowd-fixes-r5.md` for the list and `docs/DECISIONS.md` F-17 for the reasons and the sandbox probes still to run. Principle: nobody
who asked to leave in time is charged however the lock race went; every state the engine cannot leave by itself has a way out a person is told
about; `verify` passes only when no money is held or taken without the engine's approval.

- **Events added:** `remainder_open` (the rest of a partly captured hold PayPal would not release) and `order_unreadable` (an earlier order of a join
  intent PayPal would not describe). `drop_closed` also carries `closed_at`, the instant the close was ASKED for. `hold_voided` may carry
  `remainder_voided`; `failed_hold_released` may carry `reason: taken_outside_engine`, `capture_ids` and `paypal_status`; `leave_failed` may carry
  `reason: captured_before_leave` and `capture_id`.
- **Leaves (item 1).** `leave` reads its clock before the lock; `close` now does too (`closed_at`). A leave is timely if it was asked before `ends_at` and,
  when the drop is closed, before `closed_at` (for a drop closed after its deadline, as of `ends_at`, only the deadline counts; ties go against the leave).
  A timely leave on a closed, settling or settled drop releases the hold and takes the buyer out of the crowd (`plan_changed` follows for a plan already
  approved). One that finds the buyer captured by settlement is recorded and refused as `charged_before_leave`; `verify` names the capture to refund and clears
  it once it is refunded in full. Request ids already on record are never recomputed.
- **Remainders (item 2).** Recording a hold as `taken` while PayPal shows it PARTIALLY_CAPTURED voids the rest (`remainder_voided`) or records
  `remainder_open`. `verify` reads the authorisation after any refund: a taken hold passes only when its captures are refunded in full and the authorisation
  is VOIDED, CAPTURED or EXPIRED. The fake allows that void by default (`void_partially_captured(False)` makes it refuse).
- **Failed then taken (item 3).** The release pass's void answered ALREADY_CAPTURED: PayPal is asked; money taken means the hold is recorded
  (`failed_hold_released{taken_outside_engine}`) and settlement goes on; nothing taken means `settle_blocked` as before. `drop exclude` does the same for a
  buyer whose capture failed.
- **Unreadable earlier order (item 4).** A refusal on an earlier order of a join intent is recorded once (`order_unreadable`) and close goes on; a join is
  refused that one time; `verify` says to check the order at PayPal.
- **Sandbox with no app (item 5).** `drop_unbound` until `drop bind` (which checks that the app in use knows every hold). A mode or ref that differs is
  still `wrong_paypal`, checked first.
- **Not found (item 6).** A hold the engine recorded as not found resolves in `verify` with a note (asked once more; open means a problem).
- **The fake (item 7).** Its answers are PayPal's JSON through the real client's mapping: no request id on a capture, refunds only on the order
  (`payments.refunds[]`, linked to the capture, COMPLETED, summed per capture). The capture POST is sent once, never retried, so the read-before-capture
  window is one attempt. Request ids are `crowd-<drop>.<buyer>.<action>[.<attempt>]`, the join's card step `<id>.card`.
