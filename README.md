# Closure (working title)

Every money answer carries a certificate.

An AI agent asked "how much did we refund last month?" usually fetches some transactions and adds them
up. If the fetch quietly missed records, the answer is wrong and sounds just as sure. Here the agent never
states a number. It writes a small program that splits the account's **whole** balance change into named
parts. A fixed certifier, with no AI in it, accepts the answer only if:

    PayPal balance at end − PayPal balance at start  =  sum of the parts,  0.00 left over

with every transaction's gross and fee in exactly one part, each part traced to PayPal transaction IDs,
and PayPal's own running balance following record by record. Otherwise it refuses and says why.

## How it works

- **Two PayPal sources must agree.** Opening and closing balances come from the Balances API (`as_of_time`,
  exact to the second). Records come from Transaction Search: unfiltered (so each record carries PayPal's
  running `ending_balance`), split into 31-day windows, every page.
- **The program is a fixed vocabulary, not code.** Parts filter by event code, code prefix, status, sign,
  ID or note, and take `gross`, `fee`, `net`, or `rest`. No network, no files, nothing to escape.
- **Refusals name the problem:** a record missing (with the exact gap), money counted twice, money left
  over, a window PayPal has not finished indexing, a search with no running balance.
- **Certificates recheck offline** (`check_offline`) and **replay** against PayPal later (`verify`).

What the certificate proves, trusting PayPal's API responses: nothing was left out or counted twice (the
fetched record count must equal PayPal's own `total_items`, no record may arrive twice, and the running
balance must follow record by record). What it does not prove: that each transaction is in the right part,
or that PayPal's data is true. That is why every part lists its transaction IDs and PayPal codes.
`check_offline` proves a certificate is consistent with itself; only `verify` (replay against PayPal) shows
where it came from.

## Run it (PayPal sandbox only)

```bash
cp .env.example .env        # sandbox client id + secret; PAYPAL_ENV=sandbox
python3 -m pytest -q        # offline, recorded sandbox fixture
PYTHONPATH=src python3 -m closure describe USD 2026-10-25T00:00:00Z 2026-10-26T01:00:00Z
PYTHONPATH=src python3 -m closure ask examples/refunds.json
PYTHONPATH=src python3 -m closure verify <certificate_id>
```

MCP server (tools `describe_activity`, `ask_money`, `verify`):

```json
{"mcpServers": {"certified-money": {"command": "python3", "args": ["-m", "closure.mcp_server"],
  "cwd": "/path/to/closure", "env": {"PYTHONPATH": "src"}}}}
```

## Scorecard

`eval/scorecard.py` puts the same questions to the certified system and to a baseline (same model, a
replica of PayPal's stock `list_transactions`). The answer key is the seeder's own log of what it sent,
never Transaction Search. The question set was locked (hashed) before any final run; every run, misses
included, is written to `eval/runs/`.

```bash
python3 eval/scorecard.py --set eval/dev-questions.jsonl --system certified --agent reference
python3 eval/scorecard.py --system certified --agent claude --model sonnet --runs 3
python3 eval/scorecard.py --system baseline  --agent claude --model sonnet --runs 3
```

## PayPal APIs used

- `POST /v1/oauth2/token`: client credentials.
- `GET /v1/reporting/transactions`: Transaction Search, read only.
- `GET /v1/reporting/balances`: Balances, read only.
- `harness/seeder.py` creates sandbox activity (Orders v2 captures, a refund, Payouts) so the scorecard has
  an answer key that never comes from Transaction Search.

## Layout

- `src/closure/certify.py`: the certifier, offline check, replay
- `src/closure/program.py`: the program vocabulary and evaluator
- `src/closure/paypal.py`: read-only sandbox client; `ledger.py`: records → events
- `src/closure/sources.py`: live / fixture sources, and a copy of what PayPal's stock agent toolkit returns
- `src/closure/mcp_server.py`, `service.py`, `__main__.py`: the agent's tools and the CLI
- `harness/`: Gate 0 kill test, seeder, repo guard

Status: hackathon build in progress. License: to be added before the repo goes public.
