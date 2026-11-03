# Crowd Price

The more people join, the lower the price, for everyone. Nobody pays until the drop closes, and nobody pays more
than the final price.

A maker opens a **drop**: a batch of one product with a minimum number of buyers and a price ladder (for example
$40 from 30 buyers, $37 from 45, $33 from 60). Buyers join with a **PayPal hold** at the starting price, so nothing is
charged yet. As the crowd grows the price steps down, and a step once reached stays reached. At close:

- **Minimum met:** each hold is captured once, at the final price (a partial, final capture); PayPal releases the rest.
- **Minimum missed:** every hold is voided. Nobody pays.

An AI assistant runs the drop for the maker. It proposes a ladder from the maker's costs, opens and watches the drop,
and explains what happened. It never sets or computes a price, and it cannot approve the money step: a person
approves the exact settlement plan in their own terminal. A deterministic engine moves the money, a settlement check
reads every hold back from PayPal afterwards, and a balance tie-out proves the captures against PayPal's own balance.

## Who it is for

Makers who produce in batches with a fixed cost up front: a potter's kiln firing, a print run, a small-batch food
order, a run of hardware. They need to know they have enough buyers before they pay that cost. Today they take
pre-orders and refund if the batch falls through, or they guess. With Crowd Price the buyers' money is held, not
taken; the batch happens only if the minimum is met; and a bigger crowd lowers everyone's price, which gives buyers a
reason to bring friends.

## How it works

- **Holds, not charges.** Each buyer is an Orders v2 order with `intent: AUTHORIZE`, authorised for the starting
  price. The maker's balance does not move until settlement.
- **One charge at the end.** Settlement captures each hold once, at the final price, with `final_capture: true`, so
  PayPal releases the difference. Below the minimum, every authorisation is voided.
- **The engine decides, the assistant explains.** Prices, totals and counts come only from the engine. The
  assistant copies them; it cannot pick or compute one.
- **A person approves the money.** `drop settle` returns `needs_approval` (exit 3) with the exact plan, such as
  `capture 52 holds at $37.00 = $1,924.00`. A person runs `togi approve` in their own terminal and types a
  passphrase the assistant never sees. The approval is for that plan only: if anything changed, settle refuses.
- **Read back from PayPal before money moves.** `drop plan` and `drop settle` read every hold at PayPal first and
  refuse if what they find no longer matches. Every POST carries a deterministic `PayPal-Request-Id`, so a retry never
  charges twice.
- **Proved afterwards.** `drop verify` reads every hold and capture back from PayPal. `drop tieout` runs the Closure
  certifier ([docs/closure.md](docs/closure.md)): PayPal's balance change over the settlement window must equal the
  drop's captures plus everything else, to the cent, with every transaction counted once.

Rules the engine keeps, each with tests ([docs/DECISIONS.md](docs/DECISIONS.md) D-11, D-11b):

- Nobody pays more than the final price, and nobody is charged twice.
- A buyer who asked to leave before the deadline is never charged, even if PayPal errored when they asked.
- A hold the engine never confirmed (a lost reply) is always released, never charged.

## What is new in this hackathon

Everything here was written during the submission period: the first commit is 26 Oct 2026. Crowd Price runs on
[Togi](https://github.com/OWNER/togi), an agent-native command-line host by the same author, also started in the
submission period (26 Oct 2026) and published as its own open-source project. Third-party code: the Python standard
library, the `mcp` package (optional, for the MCP server) and `pytest`.

## Run it

Needs Python 3.11 or newer. Tested on macOS with Python 3.12. Linux should work; there, Togi's code mode runs without
the macOS sandbox.

### 1. Install

```bash
git clone https://github.com/OWNER/crowd-price.git
git clone https://github.com/OWNER/togi.git
mkdir -p ~/.local/bin && ln -s "$PWD/togi/bin/togi" ~/.local/bin/togi   # ~/.local/bin must be on your PATH
togi add ./crowd-price
python3 -m pip install pytest
```

### 2. Run the tests (no PayPal account, no AI, about a minute)

```bash
cd crowd-price
PYTHONPATH=../togi/src python3 -m pytest -q      # 701 passed, 5 skipped (the 5 live sandbox tests)
```

The tests run against a pretend PayPal built from what the sandbox actually answered (docs/DECISIONS.md F-08 to F-17).

### 3. Set up the person's side, once

Money commands need a person. These two steps are deliberately things an AI agent cannot do: each one asks for a
passphrase in a real terminal.

```bash
togi approver setup      # choose a passphrase of 12+ characters; it lives in ~/.togi/approver.d
togi lock crowd          # read the list of money code, then lock it: the money path runs only locked code
```

### 4. A drop with a pretend PayPal (no PayPal account; AI optional)

The same commands the assistant runs, typed yourself:

```bash
export CROWD_ENV=test CROWD_PAYPAL=fake
togi crowd test reset
togi crowd ladder propose --unit-cost 9 --fixed-cost 260 --minimum 30      # steps and the profit at each
togi crowd drop create --title "Speckled mugs" --minimum 30 --hours 48 --ladder 30:40,45:37,60:33,75:30
togi crowd drop publish --drop d_...                 # the id the last command printed
togi crowd sim join --drop d_... --count 30          # 30 test buyers: the minimum is met, price $40
togi crowd sim join --drop d_... --count 15          # 45 buyers: everyone's price is now $37
togi crowd drop close --drop d_...
togi crowd drop plan --drop d_...                    # capture 45 holds at $37.00 = $1,665.00, and an expect token
togi crowd drop settle --drop d_... --expect capture:...    # exit 3: needs approval, prints ap_...
togi approve ap_...                                  # you, the person: read the plan, type the passphrase
togi crowd drop settle --drop d_... --expect capture:... --approval ap_...
togi crowd drop verify --drop d_...                  # every hold and capture read back
togi crowd drop explain --drop d_...                 # what happened, in plain words
```

With the AI assistant (needs [Claude Code](https://claude.com/claude-code) installed and signed in):

```bash
python3 demo/maker_agent.py --fake --model sonnet           # the whole story; it stops for your approval
python3 demo/maker_agent.py --fake --scenario missed        # a drop that misses its minimum: nobody pays
```

The assistant runs in an empty folder with one tool, Bash limited to `togi ...` commands. See
[demo/README.md](demo/README.md).

### 5. The pages: the buyer's drop page and the maker's view

In a second terminal, with the same settings as the assistant (so both see the same drops):

```bash
CROWD_ENV=test CROWD_PAYPAL=fake CROWD_DETAILS=web/demo/details.json PYTHONPATH=src:web python3 -m crowdweb --maker-name Juniper
```

It prints a link per drop and a maker link with its key (set your own with `CROWD_MAKER_KEY`, 16+ characters).
`CROWD_DETAILS` names a JSON file of what each drop sells (`web/demo/details.json` is Juniper's), with an optional photo
beside it. On the **drop page** a buyer joins with a sandbox test card and
watches the price step down as the crowd grows; they can leave until the drop closes, and afterwards see what they paid.
The **maker view** shows the ladder with the profit at each step, the activity, the buyers, and the settlement waiting
for your `togi approve`; once settled, it checks every hold and capture with PayPal and runs the balance proof. Neither
page can settle, exclude or bind: those move money, and only a person approves them, in Togi. How to host it behind a proxy, what
it limits, and the rare cases it does not close: [web/SECURITY.md](web/SECURITY.md).

### 6. The studio: run a drop from a chat, approve the charge in the panel

The whole story in one page, from your own terminal:

```bash
export CROWD_ENV=test CROWD_PAYPAL=fake CROWD_DETAILS=web/demo/details.json
python3 -I web/studio.py --maker-name Juniper       # prints the studio link with its key; this machine only
```

Open the studio link. On the left is a chat with your assistant; on the right, the drop it is working on, live: the
price, the ladder with your profit at each step, the buyers as they join, the activity, the outcome. Tell it your costs
("clay and glaze are 9 dollars a mug, the firing is 260, I need 30 sold"); it asks the engine for a ladder, opens the
drop and runs it. Each thing it does shows as a step in plain words ("Asked the engine for a price ladder from your
costs", "Checked every charge with PayPal"); click a step to see the exact `togi` command and what it returned. On a
phone the chat and the drop are two tabs. When the drop closes it asks to settle, and the panel shows exactly what will
be charged, the lines `togi approve` would print, and a box for your approver passphrase. Approve there; the assistant
settles, checks every charge with PayPal and explains the outcome.

How the approval stays yours:

- **It is Togi's own check, not a shortcut around it.** The panel uses Togi's approval desk (`togi.desk`). Togi
  recomputes the request from the live repo exactly as `togi approve` does, fixes the expiry it will sign, and shows you
  that; your yes is bound to what you were shown, and if the request, the plan or the locked code changed since, nothing
  is approved. The passphrase is checked against the same approver (same record, the same five-wrong lock-out, shared
  with the terminal), and the grant is the same signed, single-use, 15-minute one.
- **Only a server you started can approve.** The desk opens once, as the studio starts, and not under `TOGI_AGENT` or
  `togi run`, not in an interpreter that is not isolated (so use `python3 -I web/studio.py`; `python3 -m crowdweb` cannot
  approve), not without an approver, and, for your own approver, only from a terminal. If it did not open, the panel
  shows the `togi approve` command instead and says why.
- **The passphrase goes from your browser to that check and nowhere else**: not to the assistant, its context, a log
  (the server logs request lines only) or the disk.
- **The assistant cannot reach it.** It is the demo's `claude -p` (on your own Claude plan; `ANTHROPIC_API_KEY` is removed
  from its environment, so never per-call API billing) with one tool, Bash, in Claude Code's restricted mode (your own
  settings files are not read, so an allow-all rule there cannot widen it) with `dontAsk`, allowed only the `togi`
  commands a maker needs, behind a `togi` shim that refuses the rest, with `TOGI_AGENT` set and without the studio's key.
  Buyer names reach it inside command output; they are data, and nothing it can run moves money without you. Every studio
  route needs the key, every POST must come from the studio page itself, and the server listens on 127.0.0.1 only.

Options: `--port` (8790), `--model` (sonnet), `--maker-key` or `CROWD_MAKER_KEY`, `--claude`, `--togi`; `TOGI_SRC` if
Togi's source is not found from the `togi` on your PATH. With a throwaway test approver (`TOGI_APPROVER_HOME` set) the
desk opens without a terminal, and Crowd honours its yes only on the pretend PayPal.

### The same tools from any MCP client or the command line

The studio is one client of Crowd Price; the engine does not know or care which agent drives it. Any agent can use the
same commands, with the same rule that money waits for a person:

```bash
togi skill crowd                  # what an agent reads first: the commands, the rules, the next step
togi tools crowd --json           # the same commands as a JSON tool list
togi crowd drop status --drop d_...   # the CLI: compact JSON, an exact `next` command on every refusal
togi mcp serve crowd              # an MCP server over stdio (pip install mcp): crowd_skill, crowd_drop_create, ...
```

For an MCP client (Claude Desktop, an IDE, your own), the server entry is:

```json
{"mcpServers": {"crowd": {"command": "togi", "args": ["mcp", "serve", "crowd"],
                          "env": {"TOGI_AGENT": "my-agent", "CROWD_ENV": "test", "CROWD_PAYPAL": "fake"}}}}
```

A money tool (`crowd_drop_settle`) returns `needs_approval` with an approval id; a person grants it with `togi approve`
in their terminal or in the studio's panel, then the agent calls the tool again with `approval`. No MCP tool, CLI command
or flag can grant one.

### 7. The same in the PayPal sandbox

Create a free sandbox app at [developer.paypal.com](https://developer.paypal.com/) (Apps & Credentials, Sandbox), then:

```bash
cp .env.example .env     # put the sandbox app's client id and secret in it; PAYPAL_ENV=sandbox
unset CROWD_ENV CROWD_PAYPAL
python3 demo/maker_agent.py --scenario missed --model sonnet    # a short run with real sandbox holds
```

Test buyers pay with PayPal's published sandbox test card numbers (in `src/crowd/paypal_holds.py`); no real card is
ever used. The client refuses to start unless `PAYPAL_ENV=sandbox`, and its address is PayPal's sandbox, fixed in code.
`drop tieout` needs PayPal to have indexed the captures, which can take a few hours; until then it refuses and says so.
The live tests: `CROWD_LIVE=1 PYTHONPATH=../togi/src python3 -m pytest -q tests/crowd/test_live.py`.

## PayPal APIs used

- `POST /v1/oauth2/token`: client credentials.
- Orders v2: `POST /v2/checkout/orders` (one order per buyer, `intent: AUTHORIZE`, a sandbox test card),
  `POST /v2/checkout/orders/{id}/authorize` (the hold), `GET /v2/checkout/orders/{id}` (the captures and refunds on an
  order, to recover from lost replies and to check refunds).
- Payments v2: `POST /v2/payments/authorizations/{id}/capture` (one partial, final capture at the final price),
  `POST /v2/payments/authorizations/{id}/void` (a missed minimum, a buyer who left, the rest of a partly captured hold),
  `GET /v2/payments/authorizations/{id}` (every hold read back before money moves, and in verify),
  `GET /v2/payments/captures/{id}`.
- Reporting, for the tie-out: `GET /v1/reporting/transactions` (unfiltered, every page, in 31-day windows) and
  `GET /v1/reporting/balances` (`as_of_time`).

## AI tools used

- **In the product:** Claude, through Claude Code (`claude -p`, Sonnet or Haiku), is the maker's assistant in
  `demo/maker_agent.py` and in the studio (`web/studio.py`), where it streams its words and steps beside the live drop.
  It learns Crowd Price from `togi skill crowd` ([skills/](skills/)) and acts only through `togi crowd ...` commands. Togi serves the same commands as a JSON tool list, a code-mode API (`togi run`) and MCP
  tools (`togi mcp serve`), so any agent can drive it.
- **Machine-checked agent tasks:** [stone/tasks](stone/tasks) holds 7 tasks (2 held out) that an agent must complete
  against the pretend PayPal: open a drop, report the price after joins, request settlement, handle a missed minimum,
  a leave after a step locked, explain an outcome, recover from a wrong flag.
- **In development:** built with Claude Code. The author designed the product and directed the work; Claude models
  wrote and reviewed code under that direction. Five independent review rounds tried to break the money rules, and
  every finding became a fix with a regression test: [docs/crowd-fixes-r1.md](docs/crowd-fixes-r1.md) to
  [r5](docs/crowd-fixes-r5.md).

## How it was checked

- 641 engine tests against the pretend PayPal, including real threads racing on the store's file lock, and 60 for the
  pages: only the engine's numbers shown; a join asked again after a lost reply is handed back, never placed twice; a
  browser can release only the hold it placed; names that hide or reorder text are refused; no route that settles.
- 5 live sandbox tests, run after every review round: a funded drop, a missed minimum, a leave after a step locked,
  a capture made to fail, and the report (docs/DECISIONS.md F-10, F-14).
- What the sandbox actually answered, and where it differs from the docs, is recorded in docs/DECISIONS.md (F-08 to
  F-17), with the probes still to run.

## Limits

- PayPal honours a hold's full amount for 3 days (the authorisation lasts 29), so drops should be 72 hours or less.
  Reauthorising after day 3 is untested.
- Buyers pay with PayPal's sandbox test cards, which the server sends to PayPal (Orders v2 with a card payment source).
  There is no PayPal wallet checkout yet.
- One product and one currency (USD) per drop. No shipping or tax.
- Togi's guarantees hold when the agent reaches tools only through Togi (a shell restricted to `togi`, code mode or
  MCP). An agent with an unrestricted shell as the same user could edit Togi itself.

## Layout

- `src/crowd/`: the engine (`engine.py`), price ladder (`ladder.py`), PayPal holds client and pretend PayPal
  (`paypal_holds.py`), store, settlement check (`verify.py`), Togi bundle (`togi_bundle.py`)
- `src/closure/`: the balance certifier used by `drop tieout` ([docs/closure.md](docs/closure.md))
- `web/crowdweb/`: the drop page, the maker view and the studio (standard library server, plain HTML, CSS and JavaScript;
  `studio.py`, `studio_http.py`, `studio_main.py`, launched by `web/studio.py`); `web/tests/`
- `skills/`: what the assistant reads; `stone/tasks/`: the agent tasks; `togi.toml`: tiers, locks and policy
- `demo/`: the assistant; `tests/`: the test suite; `harness/`: sandbox seeder and checks; `eval/`: the certifier's
  scorecard

## License

MIT. See [LICENSE](LICENSE).
