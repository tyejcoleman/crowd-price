# Product decisions and sandbox findings

What the PayPal sandbox showed (F-), the product rules settled during the build (D-11, D-11b), and what each
independent refuter pass found (GATE). Extracted from the project's full decision log; numbering follows it.

FINDINGS (2026-10-25, live sandbox; evidence: harness/killtest.py, tests/fixtures/sandbox-2026-10-26.json)

F-01 Unfiltered Transaction Search carries ending_balance on every record (15/15); filtered search carries none (0/14).

F-02 The running balance chains to the cent record by record in USD and EUR, including the EUR->USD conversion
     (T0200 both sides), the partial refund (T1107), and the payout hold (T1503) / pending payout (T0001 P) /
     release (T1105). One uniform rule (balance += gross + fee) explains every record so far.

F-03 Balances API as_of_time is exact to the second (5000.48 at 00:29:00, 5909.82 at 00:29:10), so opening and
     closing balances come from a source independent of the record list. It may return every currency; pick ours.

F-04 PayPal agent-toolkit listTransactions (read from its source): a range over 31 days has its start silently
     reset to end - 31 days; one request, no paging of its own. balance-net-summary / daily-summary: 404 in sandbox.

F-05 Event family T11 holds both refunds (T1107) and payout-hold releases (T1105): a "refunds = T11*" rule is wrong
     but still balances. The certificate proves completeness, not classification; it shows codes + IDs per part.

GATE (2026-10-25): independent refuter (Opus) REFUTED certifier v1: zero-net record pairs could be dropped with the
      chain intact; duplicates absorbed by the rest part; check_offline did not recompute program_hash or re-run the
      program; window edges off by the as_of / inclusive-date rules. Fixed: total_items count check, duplicate refusal,
      opening/closing at start-1s / end-1s with inclusive-window fetch (probed live), rewritten check_offline.
      17 tests, each finding has a regression test.

GATE (2026-10-25): second refuter pass REFUTED v1.1: fractional-second window ends dropped an edge record with every
      check passing (the one false certificate found); records outside the window not refused; check_offline
      ignored fixed fields; note not hashed; free-text labels could carry a dollar figure; same-second ordering
      could falsely refuse. Fixed: whole-second windows only, record_outside_window refusal, fixed-field checks,
      note in data_hash, money figures banned in question/part names, depth-first same-second ordering, Ed25519
      signatures (key in data/, git-ignored; verifiers pin the public key). 23 tests. Live certificate signed,
      replayed, holds.

GATE (2026-10-26): third refuter pass: no false certificate found (claims 1-2 survive: completeness, missing /
      duplicate / out-of-window refusal). REFUTED on "the agent never sees uncertified money": refusal details
      carried exact amounts (an aimed incomplete program could read the answer off left_over); where-strings could
      carry a dollar figure into a signed certificate; verify returned a fresh unsigned answer; the keyless check
      allowed extra keys and inconsistent time/family fields; the money check missed fullwidth/foreign forms; a lone
      surrogate crashed signing. Fixed: agent-facing refusals carry reasons, ids and counts only (full detail to
      data/refusals.jsonl); verify returns holds + why only; where codes/ids/statuses are letters+digits, note
      filters get the money check; NFKC-normalised money check incl. every currency symbol; key allow-lists and
      time/family checks; ASCII-escaped signing body. 30 tests.
      Gate-honesty gap (still open): offline, Fixture.balance is read off the same records' ending_balance and its
      count is len(records), so balance_mismatch / incomplete_fetch and the Balances as_of timing are only proven
      by live probes, not by an independent offline source. Same-second groups over 8 records fall back to a strict
      check (false refusal possible on big batch payouts, never a false certificate).

SCORECARD (2026-10-26)
- eval/questions.jsonl LOCKED 2026-10-26T23:36:08Z, sha256:e2428a27...c0b1 (eval/questions.lock). 12 questions over
  clean daily seeder batches Oct 27 - Nov 10 (10 answerable incl. 2 zero answers, 2 that must be refused). Scoreable
  from Nov 11 ~03:00 UTC (indexing lag). The lock is only tamper-evident once committed.
- eval/dev-questions.jsonl: unlocked, over the Oct 26 batch, for development.
- Answer key = data/seeder-log.jsonl (our sent amounts + PayPal capture fees), never Transaction Search. Questions
  only ask what the log fully determines (no conversion amounts, no payout fees, no opening deposit).
- Dev results 2026-10-26: reference programs 8/8; Haiku smoke runs certified D02 D03 exact, refused D07; baseline
  Haiku D01 exact (small data: the stock cut cannot bite at 15 records).

F-06 (OPEN probe): does Transaction Search select by initiation or update time? All 15 records so far have equal
      times. Re-check once the pending payout 0T20228522546222J settles; if it selects by update time, a record
      can leave a window after the fact (count still agrees) and windows need a settled-records rule.

F-07 Labels: money figures (currency symbols, decimals, thousands separators) are refused in question and part
      names; a bare integer like 300 is not caught (years must pass). Known limit.

D-11 (decided 2026-10-27, Tye): a hold the engine never confirmed is always released, never adopted. If a join's answer is
      lost and a hold is found later by `reconcile`, it is voided (`orphan_hold_released`), even if the drop could still take
      it; the buyer saw an error, so they are never charged for it and can simply join again. This supersedes the
      "DECISION FOR TYE" paragraph in F-12 (which had the hold adopted). The buyer's own retry is different: a retry that
      replays the same request id gets the hold back, and the engine records it only after PayPal confirms it is open.

D-11b (decided 2026-10-27, Tye; extends D-11: "nobody who saw an error is charged"): a leave the buyer asked for before the deadline is
      honoured even if PayPal errored. A leave that hit an outage, a lost reply or a refusal stays on record (`leave_started`, no
      `leave_failed`); at close and at settle the void is retried and the buyer is out of the crowd either way. If the retry succeeds the
      hold is VOIDED and the leave is dated when it was asked. If PayPal still refuses, the buyer is out anyway and the hold that could
      not be released is recorded as `void_failed`, which `verify` keeps reporting with the hold to void by hand. If PayPal cannot be
      reached at all, nothing is decided and close or settle stops, to be resumed. The one leave that cannot be honoured is a hold that
      is already captured (`leave_failed`, reported by verify). This supersedes F-13's "a leave PayPal refused is closed at once and the
      hold stays theirs" and "an unconfirmed leave whose hold PayPal shows open is recorded as `leave_failed`".

F-08 Sandbox pressure test 2026-10-27 02:07 UTC (scratch script, card payment_source, intent AUTHORIZE):
      holds 5YV10314E1219424V / 4GK89254U2801613Y / 22931798M8461154G, expiry 29 days (2026-11-25); balance unchanged
      by holds; capture 32.00 of a 40.00 hold with final_capture -> COMPLETED, fee 1.32 (= 2.59% + 0.49), auth
      CAPTURED; second capture -> AUTHORIZATION_ALREADY_CAPTURED; capture 60 of 40 -> MAX_CAPTURE_AMOUNT_EXCEEDED;
      void -> VOIDED, capture after void -> AUTHORIZATION_VOIDED; reauthorize on day 0 -> REAUTHORIZATION_TOO_SOON.
      Balances API did not reflect the captures within seconds (refresh lag). Open: reauthorize after day 3 on
      4GK... is already captured; place a fresh hold to test day-4 reauthorize.

F-09 Per-order tracking (POST /v2/checkout/orders/{id}/track) works in sandbox (201); /v1/shipping/trackers-batch
      returns NOT_AUTHORIZED (needs the tracking feature enabled on the app).

F-10 Crowd Price live run 2026-10-27 (sandbox, tests/crowd/test_live.py with CROWD_LIVE=1, amounts of 4 to 5 dollars):
     funded drop d_2772a154 (3 buyers on cards 0,1,2; hold 5.00, final price 4.00): captures 4YL468278P318933A,
     18F65182KE631211N, 9BW98396281176152 (auths 33B16131D24026201, 2P033737FA8861448, 17J03576J37863304); gross 12.00,
     fees 1.77 (0.59 each = the 2.59% + 0.49 model on 4.00), net 10.23; verify ok. Not-funded drop d_a9a0e221 (2 of 3):
     holds 705227381L175583S and 02550737TC9532028 voided, nothing charged; verify ok. A buyer who left after the step
     locked (d_d28c766c): hold 2EH15283NT2450605 voided at PayPal, price stayed 4.00; verify ok. A capture made to fail
     (d_f391b0b8, hold 7G261723XW241554P): recorded as capture_failed, hold left open then voided; verify ok.

F-11 Where the sandbox differs from the spec (found building the engine; the code follows the sandbox):
     (a) POST /v2/checkout/orders with a card is NOT replay-safe: the same PayPal-Request-Id again answers 422
         TRANSACTION_REFUSED instead of the first response, so a lost response would look like a decline and leave an
         unknown hold. Fixed by a two-step join: create the order with no card (idempotent: same id, same order), read it,
         authorise it with the card, and after any doubt read the order again. Replay now returns the same hold.
     (b) Capture with the same request id returns the same capture (idempotent); a new id gives
         AUTHORIZATION_ALREADY_CAPTURED. Void with the same id succeeds again; a new id on a voided hold gives
         PREVIOUSLY_VOIDED (422); void after capture gives PREVIOUSLY_CAPTURED (422). All mapped to clean errors.
     (c) Failing a capture for flow E: header PayPal-Mock-Response {"mock_application_codes":"TRANSACTION_REFUSED"} gives
         422 TRANSACTION_REFUSED and the hold stays CREATED. INSTRUMENT_DECLINED is not accepted for captures (empty 403).
     (d) Transaction Search answers 404 INVALID_REQUEST "Data for the given start date is not available" for a window that
         starts after its index horizon (about 1.7 hours behind at 03:08 UTC). The tie-out reports it as not_yet_indexed.
     (e) Test card 5105105105105100 is refused (422 VALIDATION_ERROR); 5454545454545454 works. Cards 4111..., 4012888888881881,
         5555..., 6011111111111117, 6011000990139424, 3530..., 3566... all place holds.
     (f) Togi's approval summary is static (command text plus the arguments), so `drop settle` takes --expect, the plan token
         from `drop plan` (for example capture:52:33.00:1716.00). The approval is bound to it and settle refuses if the plan
         differs, so a person approves exactly what runs.

F-12 Crowd fix round 1 (2026-10-27, docs/crowd-fixes-r1.md; fake PayPal only, no live run). What changed, and what to probe
     in the sandbox later because the code had to assume it (each assumption leans to the safe side):
     - Lost join replies: every join writes `join_started` before PayPal is asked and `order_created` the moment the
       order exists; `close` and `settle` first run `reconcile`, which reads the order back and either records the hold
       (drop still joinable), voids it (`orphan_hold_released`), or records `hold_failed{join_unconfirmed}`. `verify` fails
       on an unconfirmed join, on any open PayPal authorisation on the drop's orders that the log does not hold, and on a
       hold whose PayPal amount differs from the engine's.
     - DECISION FOR TYE: a hold found by `reconcile` while the drop can still take it is ADOPTED (that buyer is in the
       crowd and is charged the final price), as the fix list says, even though that buyer was shown "PayPal
       unavailable". The alternative is to void every unconfirmed hold and never charge anyone who saw an error; it costs
       a place in the crowd and changes nothing else. One-line change in `Engine._reconcile_one` if you want it.
     - `test settle` / `test reset` refuse any session with TOGI_AGENT set (even empty), need CROWD_ENV=test and
       CROWD_PAYPAL=fake, and refuse a store with any hold not made by this store's own fake (every hold now records
       `paypal_mode` and, for the fake, `paypal_ref`, a hash of its state file). An agent can still clear TOGI_AGENT in its
       own shell; what stops that reaching real money is the store check. A fake-only store is test money anyway.
     - Any non-transient 4xx from PayPal is a refusal of that one request (`PayPalRefused`), named or not. In `settle` it
       is recorded per buyer (`capture_failed` or `void_failed`, with PayPal's error name, http status, debug id and
       `paypal_mode`) and settle goes on; only network trouble, 408, 429 and 5xx stop it (to resume). Repeated 401 is
       the credentials, not a buyer, and also stops it.
     - ASSUMED, to probe: (1) capturing a hold past its `expiration_time` is refused (AUTHORIZATION_EXPIRED is the guess);
       the engine never tries: past `expires` it records `capture_failed{hold_expired}` itself (in a missed drop, a hold
       that lapsed counts as released). (2) Voiding a lapsed hold is refused too; AUTHORIZATION_EXPIRED on a void is read as
       "already gone" and verify then needs PayPal to say EXPIRED or VOIDED. Any other refusal of a void is `void_failed`,
       which verify reports until PayPal shows the hold gone. (3) GET authorization after a final capture still reports
       the amount that was held (verify also accepts the captured amount, nothing else). (4) GET order lists every
       authorisation made on it, voided ones included, and a recorded order stays readable until verify runs (an
       unreadable order is reported, not skipped). (5) Replaying POST /v2/checkout/orders with the same request id after
       the first create never arrived makes an empty order, which holds nothing. (6) The error names AUTHORIZATION_EXPIRED,
       AUTHORIZATION_DENIED and PAYER_ACTION_REQUIRED are from the refuter's list, not yet seen from the sandbox.
     - Prices: finite, at most 2 decimal places, above 0 and at most 10000, for every step and the start price; refused
       (never rounded) at propose/create. Money text rounds half up, like the PayPal client.
     - The plan token carries pending releases (`capture:3:36.00:108.00+release:1`); `settle` works the plan out again under
       its lock, after reconcile, and refuses if it differs from the approved one.
     - Leave stops at the deadline like join, and is stamped with the moment it was asked; a join is stamped likewise. A
       drop closed late counts its crowd as of `ends_at`, and settle prices that crowd, not whatever moved afterwards.
     - R3: `togi run` importing the engine is closed in Togi (FIXES-r1 #4), not here. Crowd's test for it is an expected
       failure until the Togi build under test has the sandbox.

F-13 Crowd fix round 2 (2026-10-27, docs/crowd-fixes-r2.md; fake PayPal only, no live run). Principle: never infer PayPal's
     state from our own clock or from an error class when PayPal can be asked; when unsure, stop and hand it to a person.
     What changed, and the sandbox facts to PROBE LIVE because the code had to assume them (each assumption leans safe):
     - Refusals are classified by the NAMES PayPal gives, on an allow-list (card or instrument declined, INSTRUMENT_DECLINED,
       TRANSACTION_REFUSED, PAYER_ACTION_REQUIRED, AUTHORIZATION_EXPIRED, AUTHORIZATION_VOIDED, AUTHORIZATION_DENIED,
       MAX_CAPTURE_AMOUNT_EXCEEDED, plus the card family: CARD_EXPIRED, INSUFFICIENT_FUNDS, CREDIT_CARD_REFUSED,
       PAYER_CANNOT_PAY, INVALID_*). Only those are recorded against one buyer. Anything else (any 401 or 403, NOT_AUTHORIZED,
       PERMISSION_DENIED, PAYEE_ACCOUNT_RESTRICTED or LOCKED, INVALID_REQUEST, a name nobody has seen, an empty body, or one
       unfamiliar name beside a familiar one) writes `settle_blocked{code, http, debug_id, phase, buyer}`, voids and
       captures nothing further, leaves the drop `settling`, and raises a refusal whose `next` names a person. Two capture
       failures in a row with the same code do the same (before any release). The count restarts after a capture that goes
       through and after every block. Failed holds are released only after every held buyer has an outcome and nothing blocked.
       The approval token changes when a block leaves failed holds to release (`+release:K`), so a person re-approves.
     - Close asks PayPal about every hold on record and drops from the crowd any that is no longer CREATED or PENDING
       (`hold_voided{reason: paypal_says_<status>}`, stamped as of the deadline when closing late). The funded decision, the
       price and the plan all come from that crowd. A status with no rule (DENIED, PARTIALLY_CREATED, nothing) is voided and
       counted released only if the void goes through; CAPTURED at PayPal is recorded as taken outside the engine and verify
       reports it; a 404 on the authorisation is recorded and verify keeps reporting it as unreadable.
     - A leave writes `leave_started` before PayPal is asked and is closed by `buyer_left` or `leave_failed`; `reconcile` reads
       the authorisation (VOIDED or EXPIRED means the buyer left, dated when they asked).
     - A replayed join (or any join after a lost reply, or any status but open) is confirmed with a GET before `hold_placed`;
       a hold that is not open is closed through reconcile and the buyer gets `hold_not_open`. A retry refused by PayPal
       reconciles the earlier try first; a retry on another card releases the first card's hold first (`card_changed`).
     - A hold is "expired" only when PayPal says EXPIRED (the clock is a hint to ask). Otherwise the capture is tried, and
       AUTHORIZATION_EXPIRED is a buyer-level refusal. A refusal that only NAMES a hold voided or expired in a void answer is
       believed only if a GET says VOIDED or EXPIRED. Only VOIDED and EXPIRED are ever taken as released without a void.
     - Verify also compares a capture's currency and its parent authorisation, reads back released orphans, reports a blocked
       settle and unconfirmed leaves, and every problem carries a concrete `next` (also in the bundle's `next`).
     - `drop settle` refuses an approval whose scope is "test" (Togi's throwaway approver) unless the PayPal is the fake.
       Crowd's tests build their approver under TOGI_APPROVER_HOME and a session guard fails if ~/.togi gains approver files.
     PROBE LIST for the sandbox (none of these was seen live; do them with small amounts, test cards only):
       1. A replayed POST /v2/checkout/orders (no card, same PayPal-Request-Id): status code (200 or 201) and body (does it
          carry the authorisation?) after the order was authorised, and again after the authorisation was voided. The code
          assumes 200 for a replay and treats any non-201 as a replay.
       2. Whether a 403 (or the empty 403 seen in F-11c) on capture or void is ever about one buyer; the code treats every 401
          and 403 as systemic.
       3. PayPal's full status list for an authorisation and for an order's authorisation entry (documented: CREATED,
          CAPTURED, DENIED, EXPIRED, PARTIALLY_CAPTURED, VOIDED, PENDING; is PARTIALLY_CREATED ever returned?). Which of
          them can be voided; what a void of DENIED, PENDING or EXPIRED answers.
       4. Whether GET /v2/payments/captures/{id} (and the capture response) names its parent authorisation: supplementary_data
          .related_ids.authorization_id, or an `up` link to /authorizations/{id}. If neither, verify cannot compare the parent
          and says so in its checks instead of passing silently.
       5. What capturing a hold past its expiration_time answers (AUTHORIZATION_EXPIRED is assumed), and whether GET then says
          EXPIRED; whether a hold ever lapses earlier than the date PayPal gave.
       6. What voiding an already-voided or already-expired hold answers (PREVIOUSLY_VOIDED seen; the expired case not seen),
          and that a repeated void with the same request id still succeeds.
       7. The error names for a restricted or locked merchant account on capture and on void (PAYEE_ACCOUNT_RESTRICTED and
          PAYEE_ACCOUNT_LOCKED are guesses), and that AUTHORIZATION_DENIED, PAYER_ACTION_REQUIRED, AUTHORIZATION_EXPIRED
          really are what a buyer-level capture refusal looks like.
       8. GET /v2/payments/authorizations/{id} for an id PayPal does not know: 404 RESOURCE_NOT_FOUND is assumed (close
          records such a hold as out of the crowd and verify reports it).

F-14 Live sandbox answers to F-13 probes (2026-10-27):
     (8) GET of an unknown authorization -> 404 RESOURCE_NOT_FOUND / INVALID_RESOURCE_ID.
     (4) GET of a capture names its parent: supplementary_data.related_ids.authorization_id (and order_id), plus an
         `up` link. verify's parent-authorisation check is backed by real data.
     (1) A replayed no-card create (same PayPal-Request-Id) after the order was authorised answers 200 (not 201) with
         the same order id and order status COMPLETED, and still COMPLETED after the authorisation was voided (auth
         status VOIDED). So the order status never reflects a void: F4 is real-world, and the client's rule "re-read the
         authorisation whenever the replayed create is not a fresh 201" is exactly what catches it.
     Still open: 403 buyer-specific vs account-level, authorisation status list, expired-hold capture/void answers,
     restricted-account error names (cannot be produced on demand in sandbox; current rule: treat as systemic).
     Live suite after round 2: 5/5 (funded, not funded, leave after lock, declined capture now released, report).

F-15 Crowd fix round 3 (2026-10-27, docs/crowd-fixes-r3.md; fake PayPal only, no live run). Mostly one class: the engine trusted whichever
     PayPal the current process uses rather than the one each hold was made with, and planned settlement from its own records
     instead of asking PayPal right before money moves. What changed, and the sandbox facts the code had to assume (each leans safe):
     - A (one store, one PayPal). Every `drop_created` records `paypal_mode` and `paypal_ref` (the fake's state-file hash; for the sandbox a
       12-hex SHA-256 fingerprint of the client id, never the id or the secret). Join, leave, publish, close, reconcile, plan, settle, exclude
       and verify refuse with `wrong_paypal` (nothing written, the other PayPal never asked) when the PayPal in use is not the one any event of
       the drop was recorded against; a drop is not created in a store that already holds another PayPal's drops. A recorded mode with no
       ref (a drop from before refs existed) and a recorded 'unknown' are let through, so the live-run drops stay reachable. The default store
       path follows the mode: `CROWD_PAYPAL=fake` without `CROWD_ENV=test` is `data/crowd-fake.db`, never `data/crowd.db` (with `CROWD_ENV=test`
       the Togi home as before). `drop settle` accepts an approval of scope "test" only if the PayPal is the fake AND `CROWD_ENV=test` AND
       `foreign_holds` is empty. `togi.toml` now says `[policy] test_approvals = true` (Crowd guards it as above) and `require_lock = true`.
     - B (ask again before money moves). `plan` reads every remaining hold at PayPal (the authorisation, and the captures on its order via
       `order_captures`: GET /v2/checkout/orders/{id}) and leaves out any that is no longer CREATED or PENDING or already has a capture;
       the funded decision and the price follow the crowd that is left. `settle` does the same after reconcile and before any capture,
       records what it found (`hold_voided{reason: paypal_says_<status>}`, with `taken_outside_engine` and `capture_ids` for a stray capture, which
       is never captured on top of and is for a person to refund), re-derives the plan from that, and refuses a plan that differs with
       `plan_changed` and the new token. During a resume, a hold CAPTURED once for exactly the final price is the engine's own lost capture
       and is recovered, not dropped. `verify` lists every capture on each of the drop's orders and fails on one the engine did not record
       (a REFUNDED, DECLINED or FAILED capture is not a charge) and on a hold charged more than the final price; a missing currency, on a
       hold or a capture, is a mismatch everywhere. The settle approval summary says the plan was checked against PayPal and is re-checked.
       The projection counts a closed drop's crowd as the crowd at the deadline less every hold gone since (`lost_after_close`).
     - C (classification, breaker). A refusal is buyer-specific only if the top-level `name` is absent, UNPROCESSABLE_ENTITY or itself on the
       list, AND every `details[]` entry names an issue on the list (an entry that names nothing counts as unfamiliar); a systemic top-level
       name (INVALID_REQUEST, NOT_AUTHORIZED, PERMISSION_DENIED, PAYEE_*, a name nobody has seen) wins whatever the details say.
       PAYER_ACCOUNT_LOCKED_OR_CLOSED, PAYER_ACCOUNT_RESTRICTED and REDIRECT_PAYER_FOR_ALTERNATE_FUNDING are buyer-specific. The breaker
       stops settle (`settle_blocked`) on two capture failures in a row of ANY buyer codes, or more than half of the captures failing once
       four were tried; AUTHORIZATION_EXPIRED and the engine's own `hold_expired` do not count. It is evaluated from the whole event history at
       the start of every settle, so a crash before the block cannot skip it. A block's `next` names the exact hold ("void hold <id> at
       PayPal") and the new `drop exclude --drop D --buyer B` [write], which releases that buyer's hold, records `buyer_excluded`, and keeps
       them from rejoining; if PayPal refuses the release the buyer is still excluded and `verify` keeps reporting the open hold.
     - D (recovery, replays). The capture lookup after a lost reply distinguishes an outage (`paypal_unavailable`, resumed later) from a real
       answer (404 = none; any other refusal = settle_blocked "would not say"); an outage is never read as "not found". A join retry that
       gets a different order id than the one recorded for the same request is a new hold: the earlier order's hold is released (D-11,
       `orphan_hold_released{superseded}`), the new one is confirmed with a GET, and it is never marked `replayed` unless the order matches.
       An intent that made two orders is reconciled for both.
     - E (leaves). D-11b above.
     - F. An authorisation whose status lookup is refused (403) stops `close` and `plan` with `hold_unreadable` (the hold, the issue name, the
       debug id, and the two ways past it); `drop exclude` resolves it. verify's "recorded against the PayPal being checked" now covers every
       event (hold_voided included) and refuses outright on a mismatch. A stray CAPTURED authorisation's next step says to refund the
       capture, with its id, not to void it.
     PROBE LIST for the sandbox (none seen live; small amounts, test cards only):
       1. GET /v2/checkout/orders/{id} after a final capture: does `purchase_units[].payments.captures[]` list it with its amount, currency and
          status, and after a PARTIAL capture taken from the dashboard? The code assumes it does (and treats an order it cannot read as unreadable).
       2. After a partial capture, does GET authorization say PARTIALLY_CAPTURED? Can a second capture be made on the rest? (The fake allows both.)
       3. Does PayPal keep a PayPal-Request-Id for longer than a join's retry window? (r8 assumes a retry can get a NEW order for the same id.)
       4. The names PayPal gives for a locked or restricted PAYER account on capture (PAYER_ACCOUNT_LOCKED_OR_CLOSED, PAYER_ACCOUNT_RESTRICTED
          and REDIRECT_PAYER_FOR_ALTERNATE_FUNDING are from PayPal's documentation, not seen), and whether their top-level name is
          UNPROCESSABLE_ENTITY (assumed; any other top-level name is treated as systemic).
       5. What a void of a hold that is already past its date answers (still unseen), now that D-11b retries it at close.

F-16 Crowd fix round 4 (2026-10-27, docs/crowd-fixes-r4.md; fake PayPal only, no live run). A fourth refuter found a double charge and
     five smaller gaps in the round-3 engine. Same lesson as F-13 and F-15: nothing is inferred from our own clock, our own records or an error
     NAME when PayPal can be asked a moment before the money moves.
     - 1 (real money). The engine read an outside partial capture of exactly the final price as its own lost capture and captured the hold
       AGAIN (40.00 on a 20.00 price); a branch that trusted our clock adopted an outside capture as the engine's. Now a capture is POSTed only
       for a hold read as open with nothing on it just before; a capture already there is the engine's only if the engine wrote down an attempt
       for it (new event `capture_started`), the hold is CAPTURED (not PARTIALLY_CAPTURED), one completed final capture sits on it at exactly
       the final price, and the request id matches where PayPal says it; anything else is `taken` (recorded, left out of the crowd, refunded by
       a person, named by verify). The fake now enforces the 115% capture cap, leaves PARTIALLY_CAPTURED after a non-final capture, and says
       which request made a capture.
     - 2. `drop exclude` took a throwaway approver's yes outside test mode (settle did not). It now takes `ctx` and refuses; the new `drop bind` too;
       a test walks every gated command.
     - 3 (real money). `leave`/`join` read the clock after waiting for the drop lock, so a leave asked in time behind a slow join was refused as late and
       the buyer charged. The clock is read first. (D-11b: a leave asked in time stands.)
     - 4. `AUTHORIZATION_EXPIRED` was trusted by name and kept out of the breaker even when PayPal's GET showed the hold open: every capture failed,
       every hold was released, verify passed. Exempt only if a GET says EXPIRED.
     - 5. A third retry that replayed the second order left the first order's hold open. Stale orders are released whenever an intent has more than one.
     - 6. A pending leave on a hold PayPal answered 404/403 for bricked close with a bare `paypal_error`; now 404 = left, 403 = `hold_unreadable` + way out.
     - 7. A drop with no recorded PayPal (older than round 1) was operable through ANY PayPal, so a fake could close a sandbox drop unfunded. It belongs to
       none until a person binds it (`drop holds`, then `drop bind --expect`); `tieout` asks which PayPal too. `d_mugs07` in data/crowd-test.db is test
       data and was left alone.
     - 8. A refunded stray never cleared in verify. Cleared once REFUNDED; PARTIALLY_REFUNDED reports what remains.
     - 9. The lock covers the whole crowd package but `ladder.py`; Togi round 5 refuses a gated command whose code is not locked or reshape.
     RESIDUAL RISK, stated plainly: between the engine's read of a hold and its capture POST there is one HTTP round trip. A capture made by
     someone else inside it cannot be seen beforehand, and PayPal's own cap allows a second one on a PARTIALLY_CAPTURED hold. `verify` then reports
     a hold charged above the final price, with the capture to refund; nothing can undo it automatically. (It is not closed by anything on our side.)
     PROBE LIST for the sandbox (not run; small amounts, test cards only):
       1. Does GET capture / the order's captures expose the PayPal-Request-Id or the `invoice_id`? The engine only compares a request id when one is
          given; against the real API it relies on the recorded attempt + CAPTURED + one final capture at the final price.
       2. After a final capture, is GET authorization CAPTURED? After a partial one PARTIALLY_CAPTURED? Is a second capture inside 115% accepted?
       3. What does GET capture show after a full refund (REFUNDED) and a partial one (PARTIALLY_REFUNDED with seller_receivable_breakdown.total_refunded_amount)?
       4. What does GET authorization answer for a hold that was voided long ago (404 or VOIDED)? A 404 on a pending leave is read as "nothing held".

F-17 Crowd fix round 5 (2026-10-27, docs/crowd-fixes-r5.md; fake PayPal only, no live run). A fifth refuter confirmed the round-4 double-charge fix and
     found one more way to charge a buyer who had asked to leave, two ways to leave a hold open while verify passed, two stuck states, and a fake that
     proved more than PayPal does. Same lesson as F-13, F-15, F-16: the engine acts on what PayPal says a moment ago, and a person is always told the
     way out of a state the engine cannot leave by itself.
     - 1 (real money). A leave asked before the deadline, queued for the drop lock behind a slow request, lost the lock to `close` and was refused
       `drop_not_live` before anything was written; the buyer saw an error and was charged at settle (D-11b says they are not). A leave is now judged by
       when it was ASKED (its clock is read before the lock), against when the drop was closed: `close` also reads its clock before the lock and records
       that instant as `closed_at` on `drop_closed`. A leave asked before the deadline, and before the close if the close was asked for earlier than the
       deadline (a manual close; a close that ran after the deadline closes the drop as of `ends_at`, so only the deadline counts), is honoured on a drop
       that is already closed, settling or settled: `leave_started`, the hold voided, `buyer_left`. The plan the person approved no longer matches, so
       `settle` refuses it with `plan_changed` and the fresh token. If settlement already CAPTURED that buyer, the leave cannot release the money: it is
       recorded (`leave_started` + `leave_failed{reason: captured_before_leave, capture_id}`), the leaver sees `charged_before_leave`, and `verify` fails
       until the capture is refunded in full (it names the capture, and what is left of a part refund). Ties go against the leave (asked at the very
       instant of the close is not before it). Tested with real threads on the real file lock (a lock held by the test across the deadline, and a bare
       race 24 times), not only a held lock.
     - 2 (held money). A hold recorded as `taken` while PayPal showed it PARTIALLY_CAPTURED kept the rest of the buyer's hold authorised (it can be
       captured again inside PayPal's cap), and once the person refunded the stray capture verify passed. Now the engine voids the remainder when it
       records the hold as taken (`hold_voided{remainder_voided}`; the void is sent before the write, so a crash in between leaves nothing recorded and the
       next run finds the hold again); if PayPal refuses, `remainder_open` is recorded. `verify` reads the authorisation AGAIN after any refund: a taken hold
       passes only when every capture the engine named is refunded in full AND the authorisation is VOIDED, CAPTURED (in full) or EXPIRED; otherwise it names
       the open rest, quotes PayPal's refusal, and says to void the authorisation (or let it lapse on its date).
     - 3. A capture refused for one buyer (recorded `capture_failed`, never read again) whose hold a person then captured: the release pass's void got
       AUTHORIZATION_ALREADY_CAPTURED, which is not a buyer-specific name, so settlement blocked on every resume and `drop exclude` answered
       `void_conflict`. Now that answer is a claim that is asked about: PayPal shows money taken, the hold is recorded as taken
       (`failed_hold_released{reason: taken_outside_engine, capture_ids}`, a remainder released as in item 2) and settlement finishes; `verify` names the
       capture to refund. If PayPal shows nothing taken the old block stands. `drop exclude` does the same for a failed-capture buyer (a held buyer whose
       hold is captured is still `void_conflict`: the next plan or settle records it).
     - 4. A join intent can make two orders (PayPal forgot the request id). If PayPal would not describe the FIRST one (403, 404), every `close` and `settle`
       raised `earlier_attempt_unresolved`, for ever: the buyer holds nothing, so `exclude` did not apply either. Now it is recorded once
       (`order_unreadable{order_id}`) and not asked about again; close goes on and the intent's current order is handled as usual. A `join` is refused that
       one time instead of putting a new hold on top of one it could not look at, and goes on the next. `verify` reports the order with "check order X at
       PayPal; void its hold if one exists", and clears once PayPal answers and shows nothing open. (A 404 on an order is now an answer in verify's
       stray check too: no such order, no authorisation on it, as the capture check already read it.)
     - 5. A drop whose events record `sandbox` and no app ref (written between round 1 and round 3) was let through for ANY sandbox app: another app, which
       cannot see the holds, read them as missing and closed the drop unfunded, and its own ref on the log then locked the owning app out. Such a drop is now
       `drop_unbound` like one with no mode, until a person runs `drop holds` and `drop bind`; `bind` reads every hold at the app in use and is refused
       (`bind_mismatch`) if it does not know one, or `wrong_paypal` if the mode differs. A mode or ref that differs from what is recorded is still
       `wrong_paypal`, checked first. Consequence: drops from the live runs of rounds 1 and 2, if any are kept, need a bind before verify or tieout.
     - 6. A hold the engine recorded as not found (404 at close or on a pending leave) made verify say "could not read this hold, run again when PayPal
       answers" on every run: PayPal HAD answered. verify now asks once more; the same 404 resolves the hold with a note; any other answer is judged on its
       own (open: a problem with the way out; captured: money taken).
     - 7. What the fake no longer proves: (a) it no longer says which request made a capture (the real API does not; the fake's captures, order and answers
       are built as PayPal's JSON and read by the same functions as the real client's). The engine's "is it ours" rule never needed it: an outside FINAL
       capture at exactly the final price, after an attempt of the engine's that never reached PayPal, is adopted as the engine's. No money harm (one
       charge, at the final price, which is what settlement would have taken); a partial or other-amount capture is still taken, and a capture with no attempt on
       record is still taken. (b) Refunds are read from the ORDER (`purchase_units[].payments.refunds[]`, each linked to its capture by its `up` link, only
       COMPLETED ones, summed per capture), not from a field on the capture (that total is on a REFUND's breakdown). (c) The window between the engine's
       read of a hold and its capture POST used to cover the client's retries (3 x 60 s plus 1 s and 2 s of backoff, about three minutes); the capture POST
       is now sent once, never retried, so it is one attempt (at most one 60 s timeout). A transient failure stops settlement for a resume, which reads the
       hold again. The residual risk of F-16 stands, narrower. (d) Request ids are `crowd-<drop>.<buyer>.<action>[.<attempt>]`; '.' is in no drop id and no
       buyer slug, so no buyer name can build another buyer's id (the old '-' form: a buyer named 'ada-authorize' had the id of the card step of 'ada'). The
       card step is `<id>.card`. An id already on record (join_started, leave_started, capture_started) is read from its event, never recomputed.
     PROBE LIST for the sandbox (not run; small amounts, test cards only). The code assumes the answer in brackets and leans safe if wrong:
       1. Void of a partially captured authorization: POST /v2/payments/authorizations/{id}/void on one that is PARTIALLY_CAPTURED. Accepted (status after:
          VOIDED?) or refused (AUTHORIZATION_ALREADY_CAPTURED, another name)? [either: refused is `remainder_open`, verify keeps failing]. Do the captures stay
          listed on the order afterwards [yes]?
       2. Where refunds appear: GET /v2/checkout/orders/{id}, `purchase_units[].payments.refunds[]` with a `links` entry rel `up` pointing at the capture, status
          COMPLETED/PENDING, amount [assumed]. And on GET /v2/payments/captures/{id}: status REFUNDED / PARTIALLY_REFUNDED [assumed], and is there ANY refund total
          on the capture itself (r4 read seller_receivable_breakdown.total_refunded_amount; the docs put it on the refund's seller_payable_breakdown)? A refund the
          order does not link to its capture counts for nothing, so verify then keeps asking for it.
       3. Request-id scoping across endpoints: is PayPal-Request-Id scoped per endpoint or per merchant (the same string on POST /orders and POST
          /orders/{id}/authorize, or on a capture and a void)? The '.' form is unambiguous either way; the answer says whether the old form could ever have collided.
       4. How long PayPal remembers a request id (the engine assumes a retry may get a NEW order for the same id: F-15 probe 3), and what a replay with a
          different body answers.
       5. Does a capture embedded in GET order carry its parent authorisation (supplementary_data.related_ids.authorization_id, as GET capture does)? The fake
          gives it; verify tolerates its absence ("PayPal did not name the parent").
       6. What GET authorization answers after a partial capture and a void: VOIDED, or something else [verify passes only VOIDED, CAPTURED, EXPIRED].

F-18 (2026-11-02, fresh public copy, throwaway TOGI_HOME): a Togi lock verifies only on the machine that made it. The
      latest `lock` audit entry is signed with that machine's audit key, and a new $TOGI_HOME has no head pointer, so on
      another machine `drop settle` is refused with lock_check_failed and `togi doctor` reports "no head pointer (new
      machine ...); togi lock re-anchors the log". A judge therefore runs `togi approver setup` and `togi lock crowd`
      before any money command, even with the pretend PayPal (README step 3). Everything up to settle ran as documented.

F-20 (2026-11-03, desk refuter + design review, one fix pass): the studio's assistant now has NO built-in tools (Claude
      Code still auto-allowed its read-only Bash built-ins, echo, ls, pwd, git status..., in restricted mode, and an echo
      could fake a Togi reply the studio believed). It is an MCP client of `togi mcp serve crowd` only, through a gate that
      lists and passes only the maker's tools; the studio reads replies only from calls that passed its own check. The
      approval card is built from Togi's signed review; the drop's name (the assistant's words) is shown only quoted,
      escaped and labelled. The launcher puts Closure and Togi after the standard library (a planted hashlib.py saw the
      passphrase) and accepts TOGI_SRC only when it is the Togi the `togi` launcher runs. Togi (web-approval): an
      unencodable passphrase is a wrong one (was a 500 with no journal line, no attempt counted), and a TOGI_APPROVER_HOME
      that is the person's own approver by another path needs the terminal on the desk. The desk tests fail, never skip.

F-19 (2026-11-03): `--allowedTools "Bash(togi:*)"` alone did not confine the demo assistant on Tye's Mac: an allow-list adds
      to ~/.claude/settings.json, which allows Bash(*) in auto mode, so `cat /etc/hosts` ran. demo/maker_agent.py now passes
      `--restricted` (no user/project/local settings read); the studio adds `--permission-mode dontAsk`, an allow-list of the
      maker's commands only, and a `togi` shim that refuses the rest. Togi's README threat model says so.

F-21 (2026-11-08, live sandbox run on the submission code): `CROWD_LIVE=1 python3 -m pytest -s -q tests/crowd/test_live.py`
      passed 5 of 5 in 113 s: a funded drop (3 holds, partial final captures at the final price), a missed minimum (every
      hold voided), a leave after a step locked (voided at PayPal, the step stays), and a capture made to fail with
      PayPal's mock header (recorded as capture_failed, not hidden), each followed by `drop verify`. Supersedes "after
      every review round" in the README: the earlier live runs are F-10 and F-14 (after round 2); rounds 3-5 ran on the
      fake only until this run.
