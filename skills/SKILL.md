---
name: crowd
summary: Group-price drops on PayPal holds: the more people join, the lower the price.
---
# Crowd Price

A maker opens a drop. Buyers join by letting PayPal hold the starting price. The price steps down for
everyone as the crowd grows, and a reached step is locked. At close, if the minimum was met every hold is
charged the final (lowest reached) price; otherwise every hold is released and nobody pays. The engine moves
the money. You (the agent) plan, watch and explain. You never set or compute a price.

## Normal workflow
1. `togi crowd ladder propose --unit-cost 9 --fixed-cost 260 --minimum 30` -> steps and profit per step.
2. `togi crowd drop create --title "Speckled mugs" --minimum 30 --hours 48 --ladder 30:40,45:37,60:33,75:30`
   (copy the `ladder` string from step 1) -> a draft with an id like `d_7f3a`. Or add `--from-proposal` with the costs.
3. `togi crowd drop publish --drop d_7f3a` -> live; the clock starts.
4. Watch: `drop status --drop D` (price, buyers, locked step, next step, time left), `drop list`, `buyers list --drop D`.
   Demos and tests: `togi crowd sim join --drop D --count 45` adds sandbox buyers.
5. `togi crowd drop close --drop D` -> joining stops.
6. `togi crowd drop plan --drop D` -> what settling will do, plus an `expect` token.
7. `togi crowd drop settle --drop D --expect <token from plan>` -> exit 3, `needs_approval`. STOP. Tell the
   person the plan summary and the approval id, and ask them to run `togi approve <id>` themselves.
8. After they approve, run the same settle command again with `--approval <id>`.
9. `togi crowd drop verify --drop D` (checks PayPal), then `drop explain --drop D` (plain words for the maker).

## Hard rules
- Every price, total and count you say must come from a command's output. Copy it; never calculate or round.
- Never run `togi approve` or `togi deny`, and never try to get around an approval. Money needs a person.
- Settle only after close. Never edit or re-open a published drop's ladder; open a new drop instead.
- If a command returns an error, run its `next` command. Do not guess flags: use `--help`.
- `drop exclude` takes a buyer out of a drop and releases their hold. That is the maker's decision, never yours: report the buyer and hold a block names and let them choose.
- Test buyers are sandbox cards only. Nothing here touches real money.

## Money words
- hold = PayPal reserves the starting price on the buyer's card; nothing is charged yet.
- final capture = one charge at the final price; the rest of the hold is released.
- not funded = under the minimum at close; every hold is released and nobody pays.

Topics: `togi skill crowd ladder`, `togi skill crowd settle`, `togi skill crowd troubleshooting`.
