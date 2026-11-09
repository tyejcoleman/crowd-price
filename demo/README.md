# Crowd Price demo: Juniper the potter

An AI assistant opens a drop, fills it with test buyers and asks for settlement. A person approves the
money step. A deterministic engine moves the money and a settlement check proves it. The assistant never
sets or computes a price, and cannot approve.

## What you need

- `togi` on your PATH, and this repo registered once: `togi add ./crowd-price` (from the folder above it)
- `claude` (Claude Code) on your PATH, signed in. The demo calls `claude -p`.
- For the live run only: sandbox credentials in `.env` (see `.env.example`). PayPal SANDBOX only.

## Fake mode: free, fast, no PayPal

```bash
python3 demo/maker_agent.py --fake --model sonnet            # whole story; waits for your approval
python3 demo/maker_agent.py --fake --scenario open --model haiku   # stops at "needs approval" (cheap smoke run)
```

`--fake` sets `CROWD_ENV=test CROWD_PAYPAL=fake`, wipes the throwaway test store, and uses a stand-in for PayPal
that behaves like the real sandbox did (partial final capture, double-capture and over-capture refused,
voids). Nothing leaves your machine except the model call.

## Live sandbox run

```bash
python3 demo/maker_agent.py --model sonnet                   # same story with real sandbox holds
python3 demo/maker_agent.py --scenario missed --model sonnet # a second drop that misses its minimum
```

Every test buyer places a real PayPal sandbox hold at the starting price. The full story adds 52 buyers
(holds are authorisations, so the balance does not move until settlement). Use `missed` for a short run.

## The approval step (the gavel moment)

The agent stops at `needs_approval` and prints an approval id. In a second terminal, as the person:

```bash
togi approvals                     # what is waiting, with the exact plan
togi approve ap_0123456789         # type the drop id when asked
```

Then press Enter in the demo terminal (scenario `full`), or run the second half yourself:

```bash
python3 demo/maker_agent.py --scenario finish --drop d_ab12cd34 --approval ap_0123456789 [--fake]
```

The agent settles with the approval, runs the settlement check against PayPal and explains the outcome.
An agent cannot approve: `togi approve` needs a real terminal and refuses when `TOGI_AGENT` is set.

## Flags

- `--model` any `claude` model alias (default `sonnet`; `haiku` for cheap runs).
- `--scenario full|open|missed|finish`, `--fake`, `--drop`, `--approval`, `--no-reset`, `--raw` (also print
  claude's raw stream).

## What to look for on screen

- Each `$ togi crowd ...` line is the agent's only way to act. The price after each batch of buyers is
  copied from the engine's output; so is the plan the person approves.
- `drop plan` shows the exact charge (for example `capture 52 holds at $37.00 = $1,924.00`).
- After settlement, `drop verify` reads every hold back from PayPal; `drop explain` tells the maker
  what happened in plain words. `drop tieout` proves the captures against PayPal's balance once PayPal has
  indexed them (it may take a few hours; the refusal says so).

## Notes

- The agent runs in an empty temp directory with one tool: Bash restricted to `togi ...`.
- `claude -p` uses your Claude plan. Use `--model haiku` while rehearsing.

## The same assistant in a browser

`python3 -I web/studio.py --maker-name Juniper` runs an assistant like this one as a chat beside a live panel of the drop
(on the same Crowd Price tools, reached over MCP instead of the CLI, with no other tool), and lets Juniper approve the
charge in that panel with her passphrase instead of a terminal. See the main README, step 6.
