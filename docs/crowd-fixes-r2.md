# Crowd fix round 2 (Opus refuter pass 2, 2026-10-27, against D-11)

Reproductions are in /private/tmp/claude-501/-Users-taikicoleman--claude-spares-spare-3Et1Yw/bab3acdc-abaa-469c-b9d0-c4087670b940/scratchpad/refute-crowd-2/
(q1 … q13). Re-run each against your fix. Each fix needs a regression test that fails before and passes
after, using the fake PayPal only.

**Principle for this round: never infer PayPal's state from our own clock or from an error class when
PayPal can be asked. When unsure, stop and hand it to a person; do not release money on a guess.**

1. **F1 (high): an account-level refusal makes settle void every hold.**
   - **Classify capture and void refusals:**
     - **Buyer-specific** (allow-list): card or instrument declined, `INSTRUMENT_DECLINED`,
       `TRANSACTION_REFUSED`, `PAYER_ACTION_REQUIRED`, `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_VOIDED`,
       `AUTHORIZATION_DENIED`, `MAX_CAPTURE_AMOUNT_EXCEEDED`. These record `capture_failed` and may be
       released.
     - **Everything else** (any 401/403, `NOT_AUTHORIZED`, `PERMISSION_DENIED`,
       `PAYEE_ACCOUNT_RESTRICTED`, `PAYEE_ACCOUNT_LOCKED`, `INVALID_REQUEST`, unknown names, empty
       bodies) is systemic: settle stops, records `settle_blocked{code, debug_id}`, voids nothing,
       leaves the drop in `settling`, and returns a refusal with a `next` step that names a person.
   - **Circuit breaker:** two consecutive capture failures with the same code stop settle the same way,
     before any release.
   - The release pass for failed holds runs only after every held buyer has a final outcome and settle
     was not blocked.
   - `verify` reports a blocked settle as a problem.
2. **F2 (high): a lost reply on a leave funds a drop that missed its minimum.**
   - `leave` logs intent first (`leave_started{buyer, auth_id}`). Reconcile resolves it by reading the
     authorisation; VOIDED means `buyer_left`.
   - **Close revalidates every recorded hold:** GET each held authorisation. A hold no longer
     CREATED/PENDING at PayPal is recorded with its real status (`hold_voided{reason: "paypal_says_<status>"}`)
     and does not count toward the crowd.
   - The funded decision uses the revalidated count.
   - The plan is derived only after revalidation.
3. **F3: an unknown or missing authorisation status.**
   - Never record a hold as released unless PayPal says VOIDED or EXPIRED.
   - For any other or unknown status, try to void. If PayPal refuses, record `void_failed` and report it.
   - `verify` reads back every authorisation tied to the drop's orders, including released orphans.
4. **F4: a stale "live" hold after a replayed create.**
   - Whenever a join's answer came from a replay (or after any lost reply), confirm the authorisation's
     current status with a GET before recording `hold_placed`; it must be CREATED/PENDING.
   - Fix the fake so a replayed create returns the current state, not a cached one.
   - Close revalidation (item 2) backstops it.
5. **F5: a two-clock-read deadline race.** `join` (and `leave`) read the clock once and use that instant for
   the deadline check, the step-lock decision and the event stamp.
6. **F6: resuming after expiry.** Before recording `hold_expired`, ask PayPal. If the capture exists, record
   it (recover). Skip the capture attempt only if PayPal says EXPIRED or the capture answers
   `AUTHORIZATION_EXPIRED`.
7. **F7: a refused retry closes an unconfirmed join.** When a join retry is refused while an earlier
   `join_started` for that buyer is unresolved, don't close that intent. Run reconcile for that request id
   first (D-11: release it), then record the refusal.
8. **F8: verify's blind spots.**
   - Compare each capture's currency with the drop's.
   - Compare each capture's parent authorisation id with the one the engine recorded.
   - Every problem carries a concrete `next` (bundle and engine), not always `drop explain`.
   - `_expired` handles timestamps with no zone (treat as UTC) and any parse error, without crashing.
9. **A retry with a different card** releases the hold on the original card (and records that) before
   holding the new card, under a new attempt id.
10. **Togi approval scope** (Togi round 2 adds `ctx.approval_scope`: "default" | "test" | None).
    - `drop settle` must refuse when `approval_scope == "test"` unless the PayPal mode is fake.
    - Use `getattr(ctx, "approval_scope", None)` so it works before and after the Togi change.
    - Test both ways.

**After the fixes:**
- `python3 -m pytest -q tests` is green (302 + 5 skipped now, plus the new tests).
- `togi stone crowd --driver script` passes all tests and 7/7 tasks with 0 forbidden. (`togi lock crowd` is
  person-only now; do not try it. Doctor will show locked-file drift until the person re-locks.)
- `harness/guard.sh --full` passes.
- Add F-13 to `docs/DECISIONS.md` listing the sandbox facts to probe live: the status and body of a
  replayed no-card create after authorise and after void; whether a 403 on capture is ever buyer-specific;
  and PayPal's status list for authorisations.
- Do not commit; do not touch the live sandbox; no `claude -p`; no broad pkill.
