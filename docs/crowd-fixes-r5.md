# Crowd fix round 5 (Opus refuter pass 5, 2026-10-27, against 7540330)

Reproductions are in the reviewer's scratch folder (refute-crowd-5, not published)
(p1–p7, plus the fuzz f5_fuzz_r4_paths.py). Each fix needs a regression test (fake PayPal only) that fails
at 7540330 and passes after.

1. **Real money: a timely leave loses the lock to `close` (p1, p1b).**
   - A leave whose pre-lock asked-time is before the drop's close time (close records its own pre-lock time
     as `closed_at`) and before `ends_at` is always honoured, even if close ran first:
     - write `leave_started`, release the hold, and take the buyer out of the crowd;
     - if the plan was already derived, `plan_changed` forces a fresh approval;
     - if settle already captured that buyer, record it and have verify name the capture to refund.
   - `leave` must not raise `drop_not_live` before recording a timely request.
   - Test with real threads and flock, as p1b does, not only with a lock the test holds.
2. **Held money: the remainder of a partially captured hold stays open (p2).**
   - When a hold is recorded as `taken` with PayPal showing PARTIALLY_CAPTURED, the engine tries to void the
     remainder. If PayPal refuses, record `remainder_open`.
   - verify re-reads the authorisation status after any refund. A `taken` hold passes only when its captures
     are fully refunded **and** the authorisation is VOIDED, CAPTURED (fully) or EXPIRED. Otherwise it reports
     the open remainder with a next step.
   - Make the fake allow voiding a PARTIALLY_CAPTURED authorisation, behind a switch defaulting to allowed, and
     test both behaviours. Add "void of a partially captured authorization" to the probe list.
3. **Stuck after a failed-then-outside-captured hold (p5).** When the release pass's void gets
   AUTHORIZATION_ALREADY_CAPTURED:
   - GET the authorisation and the order's captures;
   - record the hold as `taken`, with the capture ids, instead of blocking;
   - verify names the capture to refund.
   - `drop exclude` handles the same case.
4. **Close stuck on an unreadable earlier order (p6).**
   - A 403/404 on an earlier order of a join intent records `order_unreadable{order_id}` and close proceeds;
     the buyer's current order is handled as usual.
   - verify reports the unreadable order with a next step ("check order X at PayPal; void its hold if one
     exists").
   - Nothing raises `earlier_attempt_unresolved` forever.
5. **A sandbox drop with no recorded app ref (p4).**
   - Treat it like a drop with no mode (r4 item 7): `drop_unbound` until a person runs `drop bind`.
   - `drop bind` checks that the current app knows every hold.
6. **A "not found" hold that verify reports forever (p3).**
   - A hold the engine recorded as not found (404) is resolved in verify, with a note.
   - verify re-checks it once: if PayPal now returns it as open, that's a problem.
   - No more "run again when PayPal answers" on every run.
7. **The fake must not prove more than the real API (p7, plus findings 7b–7d).**
   - **(a)** The fake must not expose which request made a capture (real PayPal doesn't). Remove that field,
     or put it behind a test-only switch that defaults off. The "is it ours" rule must work without it: an
     outside final capture at the final price, after an engine attempt that never reached PayPal, may be
     adopted. That is no money harm, so document it.
   - **(b)** Refunded amounts: read refunds from the order (`purchase_units[].payments.refunds`), summing per
     capture, rather than a field on the capture. The test must use the order shape the real API returns. If
     unsure, note it in the probe list.
   - **(c)** F-16's read-before-capture window covers the client's retries (up to 3 × 60 s plus backoff). Say
     so, and cap it: the capture POST gets a single attempt with no retry. A transient failure stops settle
     for resume.
   - **(d)** Request ids: use a separator that buyer slugs can't contain (e.g. `.`), so no buyer name can
     collide with another buyer's operation. Keep backward compatibility for recorded in-flight intents (read
     the id from the event, don't recompute it).
8. **F-17 in `docs/DECISIONS.md`:** the new sandbox probes. Void on PARTIALLY_CAPTURED; where refunds appear
   on GET order and GET capture; request-id scoping across endpoints; how long request ids are remembered.

**After the fixes:**
- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests` green (566 + 5 skipped now, plus
  new tests).
- The private-term and secret scan passes.
- `togi stone crowd --driver script` passes.
- The f5 fuzz over 1000 seeds shows 0 charged timely leavers and 0 open remainders with verify ok.
- Real `~/.togi` stays free of approver files.
- No live sandbox, no `claude -p`, no broad pkill, no commit.
