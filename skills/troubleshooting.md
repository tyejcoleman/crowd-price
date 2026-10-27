---
name: troubleshooting
summary: Error codes you may see and what to run.
---
# Troubleshooting

Every error has a `next` command: run it. Common ones:
- `drop_not_found`: wrong id. `togi crowd drop list` shows the ids.
- `drop_not_published`: the drop is still a draft. `drop publish --drop D`.
- `drop_closed` / `drop_ended`: joining has stopped. A drop whose clock ran out still needs `drop close`.
- `drop_not_closed`: close first. `drop close --drop D`.
- `already_joined`: that buyer already holds a place. Use another name, or `drop leave` first.
- `join_declined`: the test card was declined. Try `--card 1` (cards 0 to 8 are valid test cards).
- `plan_changed`: the `--expect` token does not match what settle would do. Use the token the error shows.
- `ladder_loses_money` / `start_price_loses_money`: a step would lose money. Run `ladder propose`.
- `bad_price`: a price must be dollars with at most 2 decimals, above 0 and at most 10000. Use `ladder propose`.
- `minimum_mismatch`: `--minimum` must equal the first number in the ladder.
- `duration_too_long`: drops run at most 72 hours (PayPal only promises a hold for 3 days).
- `paypal_unavailable`: PayPal could not be reached. Every call is safe to repeat.
- `paypal_not_configured`: no sandbox credentials here. For practice use `CROWD_ENV=test CROWD_PAYPAL=fake`.
- `test_tools_not_for_agents` / `store_has_real_holds`: `togi crowd test ...` is for a person setting up a test store.
  Money moves only through `drop settle`, which waits for a person. Do not look for a way round it.
- `drop_busy`: another process is settling this drop. Wait, then `drop status`.
- Exit 2 (`refused`) means a guard said no; do not retry the same call. Exit 3 means ask a person to approve.

A wrong flag: `togi crowd <command> --help` lists the flags. Money flags are dollars with cents, like `33.00`.
Do not try `togi approve`. If a person is needed, say so and wait.
