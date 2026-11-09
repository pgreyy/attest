"""/attest slash command for a human. Every path returns a string and never raises."""
from __future__ import annotations

import shlex

from .ledger import Rejected

USAGE = (
    "Usage:\n"
    "  /attest status           cash, positions and P&L from the ledger\n"
    "  /attest reset [amount]   wipe the paper account and start fresh\n"
    "                           (default: starting_balance from config, 10000 USDT)"
)


def _status(ledger) -> str:
    p, q = ledger.portfolio(), ledger.pnl()
    cur = p["currency"]
    lines = [f"attest paper account (source: {p['source']}, as of {p['as_of']})",
             f"  cash:           {p['cash']} {cur}"]
    if p["positions"]:
        lines.append("  positions:")
        for pos in p["positions"]:
            lines.append(f"    {pos['symbol']}: {pos['quantity']} @ avg {pos['avg_cost']}, "
                         f"mark {pos['mark_price']} ({pos['mark_source']}), value {pos['market_value']}, "
                         f"unrealized {pos['unrealized_pnl']}")
            if not pos.get("mark_is_live", True):
                lines.append(f"      {pos.get('stale_reason', '')}")
    else:
        lines.append("  positions:      none")
    lines += [
        f"  total equity:   {p['total_equity']} {cur} (started with {p['starting_balance']})",
        f"  realized P&L:   {q['realized_pnl']}",
        f"  unrealized P&L: {q['unrealized_pnl']}",
        f"  total P&L:      {q['total_pnl']} ({q['return_pct']}%)",
        f"  trades:         {q['trade_count']} ({q['buys']} buys, {q['sells']} sells)",
        f"  {p['mark_note']}",
    ]
    return "\n".join(lines)


def make_handler(get_ledger, default_balance):
    def handle(raw_args: str = "", **kwargs) -> str:
        try:
            try:
                words = shlex.split(raw_args or "")
            except ValueError:
                words = (raw_args or "").split()
            sub = words[0].lower() if words else "status"
            if sub == "status":
                return _status(get_ledger())
            if sub == "reset":
                if len(words) > 2:
                    return USAGE
                amount = words[1] if len(words) == 2 else default_balance()
                out = get_ledger().reset(amount)
                return (f"attest: paper account reset. Starting balance {out['starting_balance']} "
                        f"{out['currency']}, no trades, no positions.")
            return USAGE
        except Rejected as r:
            return f"attest: {r.reason}"
        except Exception as exc:
            return f"attest: that didn't work ({exc.__class__.__name__}: {exc})"
    return handle
