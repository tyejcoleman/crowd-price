# Crowd fix round 4 (Opus refuter pass 4, 2026-10-27, against 1f939a5)

Reproductions are in the reviewer's scratch folder (refute-crowd-4, not published)
(s1–s8, plus the fuzz s9b_fuzz_r3.py). Each fix needs a regression test (fake PayPal only) that fails at
1f939a5 (use `git stash` or a worktree) and passes after.

1. **s1 (real money, high): a buyer charged twice.**
   - `_could_be_ours` accepts only a final CAPTURED status with exactly one capture, at the final price, that
     the engine has a recorded attempt for (the request id matches the engine's deterministic capture request
     id, when PayPal exposes it). Otherwise the capture was made outside the engine: record it as `taken`, and
     never capture on top of it.
   - Settle never POSTs a capture for a hold it found with any existing capture.
   - The "ours" decision must not depend on our clock. Both branches of `_capture_one` use the same rule.
   - Make the fake enforce PayPal's capture cap: the total of captures on one authorisation is at most about
     115% of the authorised amount. A partial capture leaves PARTIALLY_CAPTURED.
2. **s2 (high): `drop exclude` accepts a test-scope approval outside test mode.** `drop_exclude` takes `ctx`
   and calls `_refuse_test_scope_approval` exactly as settle does. Add a test that every gated command in
   the bundle performs the scope check (introspect `EFFECTS` / the command signatures).
3. **s5 (real money, medium): a leave queued behind the lock is charged.** `leave` (and `join`) read the clock
   *before* taking the drop lock. That instant decides the deadline and stamps the event (D-11b: a leave asked
   in time stands).
4. **s4 (medium): `AUTHORIZATION_EXPIRED` is trusted without asking.** It is exempt from the breaker only when
   a GET confirms EXPIRED. Otherwise it counts as a failure (and trips the breaker).
5. **s3: a third retry orphans the first order.** Run `_release_stale_orders` on the join path whenever an
   intent has more than one recorded order, replayed or not.
6. **s6 (liveness).** In `_reconcile_leaves`, treat a 404 as missing and a 403 as `hold_unreadable`, with the
   `drop exclude` way out, exactly as `_revalidate` does. Never a bare `paypal_error`.
7. **s7: drops with no recorded PayPal mode.**
   - A drop with no recorded mode belongs to no PayPal until a person binds it: add a `drop bind` [money]
     command that records the current mode and ref after showing the holds it will check.
   - `tieout` also calls `require_paypal`.
   - The existing `d_mugs07` in `data/crowd-test.db` is test data; leave it alone.
8. **s8: a refunded stray never clears.** verify passes a `taken` hold once its stray capture shows REFUNDED
   (full refund) and reports `PARTIALLY_REFUNDED` with the remaining amount.
9. **Togi round 5 (H1): the lock must cover the code behind every money command.**
   - In `togi.toml`, lock every file of the `crowd` package except `src/crowd/ladder.py`, which stays
     reshape. That means adding `src/crowd/__init__.py` and any other module the gated commands import.
   - Add a test that every `.py` under `src/crowd/` is locked or reshape.

**After the fixes:**
- `python3 -m pytest -q tests` green (508 + 5 skipped now, plus new tests).
- The private-term and secret scan passes.
- The fuzz `s9b_fuzz_r3.py` (1500 seeds) shows 0 double charges and 0 charges to timely leavers.
- `togi stone crowd --driver script` passes. Togi round 5 is being built in parallel; if the stone or Togi
  CLI misbehaves because of it, re-run once and report.
- Real `~/.togi` stays free of approver files.
- No live sandbox, no `claude -p`, no broad pkill, no commit.
