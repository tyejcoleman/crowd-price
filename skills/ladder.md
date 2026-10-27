---
name: ladder
summary: How the price ladder works and how to read a proposal.
---
# The price ladder

A ladder is a list of steps `buyers:price`, like `30:40,45:37,60:33,75:30`.
- The first number is the drop's minimum. Below it, nobody is charged.
- Prices only go down as buyers go up. Every buyer holds the first (top) price: the most they can ever pay.
- When the crowd reaches a step's buyers, that step is locked for good, even if someone leaves.
- At close everyone is charged the lowest price reached, once, as a final capture.

## `ladder propose`
`togi crowd ladder propose --unit-cost 9 --fixed-cost 260 --minimum 30 [--start-price 40] [--steps 4] [--margin 0]`
- `unit-cost` is what one item costs; `fixed-cost` is the one-off batch cost (a kiln firing, a print run).
- Each step lists `profit_at_that_count`: what the maker earns when the crowd is exactly that size, after
  PayPal's fee (2.59% + 0.49 per charge). More buyers at the same price earn more.
- The engine picks the prices. You may pass a different `--start-price`, but a price that loses money at the
  minimum is refused and the error names the lowest price that works.
- `--margin N` makes every step earn at least N dollars.
- Fewer steps than asked can come back when the range is too tight; the note says so.

## Offering the maker a different ladder
Run `ladder propose` again with other flags, or give your own pairs to `drop create --ladder ...` together with
`--unit-cost` and `--fixed-cost`; the engine refuses any ladder with a step that loses money.
Never make up prices that did not come from a command or from the maker's own ladder.
