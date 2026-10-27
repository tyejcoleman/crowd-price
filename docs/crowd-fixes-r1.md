# Crowd fix round 1 (from the Opus refuter, 2026-10-27)

Reproductions: /private/tmp/claude-501/-Users-taikicoleman--claude-spares-spare-3Et1Yw/bab3acdc-abaa-469c-b9d0-c4087670b940/scratchpad/refute-crowd/ (p1, p1b, p2, p3, p5, p5b, p7, p9, p10).

Each fix needs a regression test that fails before the fix and passes after. Use the fake PayPal only. If a
fact about the real sandbox is needed (for example, what capturing an expired hold returns), make the
conservative assumption and note it in DECISIONS.md.

1. **R1: orphan hold on a lost join reply.**
   - Record intent before acting: append `join_started{buyer, request_ids, card_index}` before calling
     PayPal, and `order_created{order_id}` as soon as the order exists, before authorising.
   - Add `reconcile(drop)`, run automatically at the start of `close` and `settle`. For every started join
     without a `hold_placed` or `hold_failed`, ask PayPal for the order (by the recorded order id, or by
     replaying the idempotent create).
     - If it holds an authorisation and the drop is still joinable, record `hold_placed`.
     - Otherwise void it and record `orphan_hold_released`.
   - `verify` must fail when any started join is unresolved, or any PayPal authorisation for the drop's
     orders is open and not in the log.
   - `verify` must compare each hold's PayPal amount with the engine's hold amount.
2. **R2: `test settle` / `test reset` reachable by agents.**
   - Both refuse unless `CROWD_ENV=test` and `CROWD_PAYPAL=fake` and `TOGI_AGENT` is unset.
   - `test settle` also refuses if the store contains any hold placed in a mode other than fake: every
     `hold_placed` now records `paypal_mode`.
   - `test reset` refuses unless every hold in the store is fake.
3. **R4: an unrecognised PayPal refusal stops settle.**
   - Map `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_DENIED`, `PAYER_ACTION_REQUIRED`, `INSTRUMENT_DECLINED`,
     `TRANSACTION_REFUSED`, `UNPROCESSABLE_ENTITY` and any other 4xx with a name.
   - During settle, any non-transient per-buyer PayPal error records `capture_failed` (with PayPal's error
     name and debug id), or `void_failed`, and settle continues with the next buyer.
   - Only a transient failure (network, 5xx, 429) aborts so it can resume.
   - `verify` reports `void_failed` holds as problems, naming the next step.
4. **R5: sub-cent prices.**
   - Every price (ladder steps, start price) must be a finite Decimal with at most 2 decimal places, > 0
     and ≤ 10000. Otherwise refuse at propose/create with a clear error.
   - `verify` compares PayPal amounts exactly.
5. **R6.** `capture_failed` and `void_failed` events record PayPal's error name, debug id and
   `paypal_mode`. `verify` treats a failed capture with no PayPal evidence as a problem.
6. **R7.** The plan token includes pending releases (`capture:N:P:T` plus `release:K` when K failed holds
   are still open). Settle refuses if the releases differ from the approved plan.
7. **Clock edges.**
   - `leave` is refused once `now >= ends_at` (joining and leaving both stop at the deadline).
   - Before settle starts, close counts holds as of `ends_at`.
8. **Holds near expiry.** Before settle, any hold past its expiry is recorded as `capture_failed{code:
   "hold_expired"}`; nothing tries to capture it.
9. **R3: `togi run` can import the engine directly.**
   - This is fixed in Togi (FIXES-r1 #4: the code-mode child runs sandboxed and cannot read bundle repos
     or `.env`).
   - In Crowd, add a test that documents the expectation: the money path is only `drop settle` through
     the host.
   - Also: `paypal_holds` reads credentials only from the repo `.env` or the environment, never from a
     path the caller controls.
