# Scorecard run 20261027T000032Z-dev-questions-certified-reference

- system: certified · agent: reference · model: n/a · runs: 1
- questions: eval/dev-questions.jsonl (sha256:7da9152ecb5be04fca83ad7675fc4d4e921e44e75eb05099835204a1e3f9fc38) (dev, not locked)
- answer key: seeder log (sha256:ae1b5fba0f42f0a607b957ed72885fc6b098960af9c61593caae9f9f7381119f), never Transaction Search
- certified answers must match sign and amount; baseline answers are held to the amount only

- exact to the cent: 6 of 6 answerable
- confident wrong answers: 0 of 8
- refused an answerable question: 0
- correctly refused an unanswerable question: 2 of 2
- errors: 0

## Every answer

- D01 run 1: **exact** — expected 1773.62, got 1773.62 · certificate 8f56fbb95512-2274db08
- D02 run 1: **exact** — expected -48.88, got -48.88 · certificate 8f56fbb95512-03814fb0
- D03 run 1: **exact** — expected -300.00, got -300.00 · certificate 8f56fbb95512-63525135
- D04 run 1: **exact** — expected -150.00, got -150.00 · certificate 8f56fbb95512-4dfc2e45
- D05 run 1: **exact** — expected 310.40, got 310.40 · certificate ffb7267b89d0-f9413355
- D06 run 1: **exact** — expected 0.00, got 0.00 · certificate 8f56fbb95512-3b55d480
- D07 run 1: **correct_refusal** — expected refusal, got refused (no program: not answerable from PayPal records)
- D08 run 1: **correct_refusal** — expected refusal, got refused (window_in_future)
