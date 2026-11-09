"""/attest slash command for a human. Every path returns a string and never raises.

resume and reset live here and nowhere else: they are not tools, so the agent
can't switch the kill switch back on or wipe the account.
"""
from __future__ import annotations

import shlex

from .ledger import Rejected

USAGE = (
    "Usage:\n"
    "  /attest status           cash, positions, P&L, risk limits, kill switch, paper mode\n"
    "  /attest resume           clear a tripped daily loss kill switch (today's loss count\n"
    "                           restarts from the next order)\n"
    "  /attest reset [amount]   wipe the paper account and start fresh\n"
    "                           (default: starting_balance from config, 10000 USDT)"
)


def _risk_lines(risk) -> list:
    ks, today = risk["kill_switch"], risk["today"]
    lines = [f"  limits:         max order {risk['max_order_notional']}, max daily loss {risk['max_daily_loss']}"]
    if today.get("start_equity") is None:
        lines.append(f"  today ({today['date']}): no order yet today, so no starting equity recorded")
    else:
        live = "" if today.get("pnl_is_live", True) else " (NOT live: stale marks)"
        lines.append(f"  today ({today['date']}): P&L {today['pnl']} from {today['start_equity']}{live}, "
                     f"{today['loss_limit_remaining']} left before the kill switch")
    if ks["tripped"]:
        lines.append(f"  KILL SWITCH TRIPPED at {ks.get('tripped_at')}: {ks.get('reason')}")
        lines.append("    buys are blocked; sells still allowed. /attest resume to clear it.")
    else:
        lines.append("  kill switch:    off")
    return lines


def _paper_lines(paper_settings) -> list:
    if paper_settings is None:
        return []
    try:
        s = paper_settings()
    except Exception as exc:
        return [f"  paper mode:     unknown (settings unreadable: {exc.__class__.__name__})"]
    tool = s.get("intercept_tool") or ""
    if not tool:
        return [f"  paper mode:     {'on' if s.get('paper_mode', True) else 'off'}; no external order tool "
                "guarded (attest's own tools only)"]
    if s.get("paper_mode", True):
        return [f"  paper mode:     on; guarding {tool}: its calls are simulated on this ledger, never sent"]
    return [f"  paper mode:     OFF; {tool} runs for real, attest only watches its own tools"]


def _status(ledger, paper_settings=None) -> str:
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
    ]
    lines += _risk_lines(q["risk"])
    lines += _paper_lines(paper_settings)
    lines.append(f"  {p['mark_note']}")
    return "\n".join(lines)


def make_handler(get_ledger, default_balance, paper_settings=None):
    def handle(raw_args: str = "", **kwargs) -> str:
        try:
            try:
                words = shlex.split(raw_args or "")
            except ValueError:
                words = (raw_args or "").split()
            sub = words[0].lower() if words else "status"
            if sub == "status":
                return _status(get_ledger(), paper_settings)
            if sub == "resume":
                out = get_ledger().resume()
                if out["was_tripped"]:
                    return (f"attest: kill switch cleared for {out['day']}. Today's loss count restarts from "
                            "the next order's equity.")
                return (f"attest: the kill switch was not tripped for {out['day']}; today's loss count restarts "
                        "from the next order's equity.")
            if sub == "reset":
                if len(words) > 2:
                    return USAGE
                amount = words[1] if len(words) == 2 else default_balance()
                out = get_ledger().reset(amount)
                return (f"attest: paper account reset. Starting balance {out['starting_balance']} "
                        f"{out['currency']}, no trades, no positions, kill switch off.")
            return USAGE
        except Rejected as r:
            return f"attest: {r.reason}"
        except Exception as exc:
            return f"attest: that didn't work ({exc.__class__.__name__}: {exc})"
    return handle
