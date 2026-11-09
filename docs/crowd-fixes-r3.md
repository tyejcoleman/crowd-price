# Crowd fix round 3 (Opus refuter pass 3, 2026-10-27, against 68e2279)

Reproductions are in the reviewer's scratch folder (refute-crowd-3, not published)
(r1–r13, r20 fuzz). Re-run each one against your fix. Each fix needs a regression test (fake PayPal only)
that fails before and passes after. The code is committed, so use `git stash` or a worktree to show the
before result.

## A. One store, one PayPal (fixes r9, r9b, r9c, r12)
- A store is bound to exactly one PayPal at creation. Record `paypal_mode` plus a ref (for the fake, its
  instance ref; for the sandbox, the client-id fingerprint, never the secret) in a store-level
  `store_created` record, or on `drop_created`.
- Every engine operation (join, leave, close, reconcile, settle, verify) refuses with `wrong_paypal` when the
  current client's mode or ref differs from what the drop's holds were made with. Nothing is written.
- The default store path depends on the PayPal mode: fake mode never resolves to `data/crowd.db`; use
  `data/crowd-fake.db`, or the Togi home in test.
- `drop settle`'s test-scope refusal: refuse `approval_scope == "test"` unless the PayPal mode is fake AND
  `CROWD_ENV=test` AND the store holds only holds from this fake (`foreign_holds` is empty).
- Togi round 4 adds `[policy] test_approvals` and `[policy] require_lock`. Set
  `test_approvals = true` (Crowd guards it as above) and `require_lock = true` in `togi.toml`.

## B. Re-check PayPal right before money moves (fixes r5, r6, r2)
- At the start of settle, after reconcile and before any capture, GET every remaining held authorisation,
  and, where the API allows, the order's captures for it.
  - A hold that is no longer CREATED/PENDING, or that already has a capture, leaves the crowd. Record its
    real state. A stray existing capture is reported for a refund, never captured on top.
  - Re-derive the plan from the revalidated crowd. If it differs from the approved token, refuse with
    `plan_changed` and the new token. A person approves the new plan; nothing is captured or released on
    the old one.
- `verify` enumerates every capture on each authorisation (via the order) and fails on any capture the
  engine didn't record or on a total above the final price. A missing currency counts as a mismatch.
- The approval summary says the plan was checked against PayPal at plan time; settle re-checks it.

## C. Classification and the breaker (fixes r1, r3a, r3b, r2, r13)
- **Systemic codes.** Use PayPal's top-level `name` as well as the details. If the top-level name is
  systemic (`INVALID_REQUEST`, `UNPROCESSABLE_ENTITY` with no buyer detail, `NOT_AUTHORIZED`,
  `PERMISSION_DENIED`, `PAYEE_*`, …) the refusal is systemic, whatever the detail says.
- **Buyer codes.** Add `PAYER_ACCOUNT_LOCKED_OR_CLOSED`, `PAYER_ACCOUNT_RESTRICTED` and
  `REDIRECT_PAYER_FOR_ALTERNATE_FUNDING` to the buyer-specific list.
- **Breaker.** Two consecutive capture failures of *any* buyer-specific codes, or more than half of
  attempted captures failing once at least 4 have been attempted, stop settle (`settle_blocked`) before
  any release.
  - `AUTHORIZATION_EXPIRED` and the engine's own `hold_expired` don't count toward the breaker.
  - Item B removes expired holds from the plan before approval anyway.
- **Breaker on resume.** At the start of every settle, before the release pass, the breaker is evaluated
  from the full event history, so a crash between the second failure and the block can't skip it.
- **Block guidance.** A block's `next` names the exact manual action. When a buyer-specific blocker
  persists, it says "void hold <id> at PayPal" or "exclude buyer <id>" via a new `drop exclude` [write]
  command. That command records `buyer_excluded` and releases that buyer's hold.

## D. Recovery and replays (fixes r4, r8)
- `_recover` (and every recovery lookup) distinguishes transient failures, which raise `paypal_unavailable`
  so the operation resumes later, from real answers. It never turns an outage into "not found".
- **A retry that gets a different order id than the one recorded for the same join intent:** treat it as a
  new hold.
  - Release the earlier order's authorisation per D-11, if one exists.
  - Confirm the new one with GET before recording it.
  - Never mark it `replayed` unless the order id matches.

## E. Leaves that hit an error (fix r7)
- **D-11b** (extends Tye's D-11: "nobody who saw an error is charged"): a leave the buyer asked for before
  the deadline is honoured even if PayPal errored.
- At close and settle, an unresolved `leave_started` is resolved by releasing that hold (retry the void),
  and the buyer is out of the crowd.
- Add D-11b to `docs/DECISIONS.md`.

## F. Smaller items
- r10: an authorisation whose status lookup is refused (403) blocks close with a person-facing `next`
  naming the hold and the account issue (as designed), and `drop exclude` resolves it.
- verify's "recorded against the PayPal being checked" check includes `hold_voided` events.
- A stray CAPTURED hold's next step says **refund** (with the capture id), not void.

## After the fixes
- `python3 -m pytest -q tests` green (404 + 5 skipped now, plus the new tests).
- `togi stone crowd --driver script` passes (7/7, 0 forbidden).
- The private-term and secret scan passes.
- Real `~/.togi` approver files are never created.
- No live sandbox, no `claude -p`, no broad pkill, no commit.
