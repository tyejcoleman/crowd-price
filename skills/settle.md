---
name: settle
summary: Closing, the approval step, the two outcomes, and checking the result.
---
# Closing and settling

1. `drop close --drop D` stops joining. A drop also stops accepting joins (and leaves) when its clock runs out, but
   it still has to be closed before it can be settled. Closing first sorts out any join whose answer was lost.
2. `drop plan --drop D` shows exactly what settle will do and an `expect` token:
   - funded: `capture 52 holds at $33.00 = $1,716.00` (`capture:52:33.00:1716.00`). Each hold is charged the
     final price in one final capture and the rest of the hold is released.
   - not funded: `void 22 holds, nobody pays` (`void:22`). The minimum was not met at close.
   - a token can end `+release:1`: one failed charge's hold is still open and settling will release it. The
     approval covers exactly that, so use the token as shown.
3. `drop settle --drop D --expect <token>` is a money command. The host answers `needs_approval` (exit 3) with an
   approval id and does nothing. Tell the person the plan summary and that they approve it with
   `togi approve <id>` in their own terminal (they type the drop id). You cannot and must not do this.
4. After they approve, repeat the same command with `--approval <id>`. An approval works once, for these exact
   arguments, and expires. If the plan changed, settle refuses and shows the new token.
5. Settle is safe to repeat: a second run changes nothing; after a crash it carries on where it stopped.

## Afterwards
- `drop verify --drop D` reads every hold back from PayPal and checks the counts, amounts and totals.
  `ok: true` means the books agree. Add `--verbose` for every hold.
- `drop explain --drop D` gives a plain-language outcome for the maker, built from the same numbers.
- `drop tieout --drop D` proves the captures against PayPal's balance. PayPal indexes with a delay of up to a
  few hours, so it may refuse with `not_yet_indexed`; try again later and say so honestly.

## If a charge fails at the end
A declined or refused capture is recorded (with PayPal's own error name) and that buyer is not charged; the price
stays the same for everyone else, and settling carries on with the next buyer. `verify` still passes and names the
failure. Tell the maker how many charges failed and by how many the drop is now short of its minimum.
A hold is called expired only when PayPal says so (`hold_expired`); a hold that is merely past its date on the engine's
clock is still tried, because PayPal decides.

## If settlement stops (`settle_blocked`)
Some PayPal answers are not about one buyer (the account is restricted, the credentials are refused, a name nobody has
seen, or two charges in a row fail the same way). Settle then stops at once: nothing more is captured and nothing is
released or voided, and the drop stays "settling". This is a refusal that names a person: say plainly that settlement is
stopped, give the issue name and debug id from the error, and that the maker must look into it with PayPal. Do not run
settle again in a loop. When the cause is fixed, `drop plan` gives a fresh token for a person to approve. `drop verify`
reports the stop as a problem until then.

If PayPal will not release a hold, settle still finishes and `verify` reports `ok: false` with the hold's id and the
next step (void it by hand in the PayPal sandbox, then verify again). Tell the maker plainly; do not say "all holds
were released" until verify is ok.
