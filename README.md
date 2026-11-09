# attest

A paper trading ledger for Hermes agents. The agent decides what to trade; attest
owns the money. Every simulated order is priced from a live public exchange quote,
checked against risk limits and recorded in a SQLite ledger, and the agent gets its
balance, positions and P&L by calling attest's tools instead of stating them.

No real money is traded. attest needs no exchange account and no keys.

## Tools the agent gets

| Tool | What it does |
|---|---|
| `paper_order` | Buy or sell by quantity or by USDT amount. Market or limit (limits fill only if marketable now, never worse than the limit). |
| `get_quote` | Live best bid and ask, with the exchange, quote time and age. |
| `get_portfolio` | Cash, positions marked at the live bid, equity, risk limits, kill switch state. |
| `get_pnl` | Realized, unrealized and total P&L, return %, today's P&L against the daily loss limit. |

## Commands for you

- `/attest status`: the ledger, the limits, the kill switch and paper mode.
- `/attest resume`: clear a tripped daily loss kill switch. This is a command, not a tool, so the agent cannot clear the switch itself.
- `/attest reset [amount]`: wipe the paper account and start again.

## Risk checks

Before any fill: a per-order size limit (`max_order_notional`, 1000 USDT), a daily loss
kill switch (`max_daily_loss`, 500 USDT, blocks buys until the next UTC day or
`/attest resume`), a reused `client_order_id` returns the original fill, and an identical
order in the same session within `dedupe_window_seconds` (10) is rejected.

## Paper mode and a guarded order tool

attest can guard one external order tool you name in `intercept_tool`, with
`intercept_args` mapping attest's order fields to that tool's argument names.

- `paper_mode: true` (the default): every call to that tool is blocked before it runs and
  sent through attest's paper broker instead, with the same risk checks. The agent receives
  the paper fill or the rejection, labelled "attest paper mode: this order was simulated,
  nothing was sent to <tool>". If the call can't be mapped, or attest hits an error while
  handling it, the call is blocked with the reason; it is never let through by accident.
- `paper_mode: false`: that tool runs untouched. **attest then only watches its own tools**:
  orders sent through the external tool are real and are not on the attest ledger.

Only the one named tool is guarded. attest does not detect or intercept other order tools.

## Claim check

When a reply in a session that used attest states a figure next to cash, equity, balance,
P&L, profit, loss, realized, unrealized or return, and the figure disagrees with the ledger
by more than 1 USDT or 0.5% (0.05 points for return %), attest appends one line:
"attest check: the ledger shows <field> <value>, not <stated>". It never changes the
reply's text and skips any figure it cannot tie to exactly one ledger field. Mark-based
figures are only checked while every position has a live price.

## Network

Read-only HTTPS GETs of public ticker data, sending only the trading pair: `www.okx.com`
first, then `api.kraken.com`, then `data-api.binance.vision` as fallbacks. Each has a
3 second deadline. A quote older than `max_quote_age_seconds` (10) is never used; with no
fresh quote the order is rejected.

## Checks

`python3 scripts/ledger_check.py` runs every case above against a temporary database with
fake price sources and no network, and prints PASS only for cases that ran and passed.
