"""/attest slash command for a human. Every path returns a string and never raises.

resume and reset live here and nowhere else: they are not tools, so the agent
can't clear the kill switch or wipe the account.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from .display import money, pct, price, qty
from .ledger import Rejected

CHECK_SCRIPT = Path(__file__).resolve().parent / "scripts" / "ledger_check.py"
CHECK_TIMEOUT_SECONDS = 180

USAGE = (
    "Usage:\n"
    "  /attest status           cash, positions, P&L, risk limits, kill switch, paper mode\n"
    "  /attest check            read-only self test: every ledger check on a temporary\n"
    "                           database with fake price sources (no network, nothing real touched)\n"
    "  /attest resume           clear a tripped daily loss kill switch (today's loss count\n"
    "                           restarts from the next order)\n"
    "  /attest reset [amount]   wipe the paper account and start fresh\n"
    "                           (default: starting_balance from config, 10000 USDT)"
)


def _risk_lines(risk, cur) -> list:
    ks, today = risk["kill_switch"], risk["today"]
    lines = [f"  limits:         max order {money(risk['max_order_notional'], True)} {cur}, "
             f"max daily loss {money(risk['max_daily_loss'], True)} {cur}"]
    if ks["tripped"]:
        lines.append(f"  KILL SWITCH TRIPPED at {ks.get('tripped_at')}: {ks.get('reason')}")
        lines.append("    buys are blocked; sells still allowed. /attest resume to clear it.")
        return lines
    if today.get("start_equity") is None:
        lines.append(f"  today ({today['date']}): no order yet today, so no starting equity recorded")
    else:
        live = "" if today.get("pnl_is_live", True) else " (NOT live: stale marks)"
        lines.append(f"  today ({today['date']}): P&L {money(today['pnl'], True)} from "
                     f"{money(today['start_equity'], True)}{live}, {money(today['loss_limit_remaining'], True)} "
                     "left before the kill switch")
    lines.append("  kill switch:    off")
    return lines


def _settings(paper_settings):
    if paper_settings is None:
        return None, None
    try:
        return paper_settings(), None
    except Exception as exc:
        return None, exc


def _paper_lines(s, err) -> list:
    if s is None:
        return [f"  paper mode:     unknown (settings unreadable: {err.__class__.__name__})"] if err else []
    tool = s.get("intercept_tool") or ""
    if not tool:
        return [f"  paper mode:     {'on' if s.get('paper_mode', True) else 'off'}; no external order tool "
                "guarded (attest's own tools only)"]
    if s.get("paper_mode", True):
        return [f"  paper mode:     on; guarding {tool}: its calls are simulated on this ledger, never sent"]
    return [f"  paper mode:     OFF; {tool} runs for real, attest only watches its own tools"]


def _demo_banner(s) -> list:
    mode = (s or {}).get("demo_feed", "off")
    if mode == "stale":
        what = "every price source returns a quote 60 s old, so every order is rejected as stale"
    elif mode == "down":
        what = "every price source fails, so every order is rejected and marks are stale"
    else:
        return []
    bar = "!" * 72
    return [bar, f"!!! DEMO FEED: {mode.upper()}: {what}.", "!!! Turn it off: set demo_feed to off.", bar]


def _status(ledger, paper_settings=None) -> str:
    s, err = _settings(paper_settings)
    p, q = ledger.portfolio(), ledger.pnl()
    cur = p["currency"]
    lines = _demo_banner(s)
    lines += [f"attest paper account (source: {p['source']}, as of {p['as_of']})",
              f"  cash:           {money(p['cash'], True)} {cur}"]
    if p["positions"]:
        lines.append("  positions:")
        for pos in p["positions"]:
            lines.append(f"    {pos['symbol']}: {qty(pos['quantity'])} @ avg {price(pos['avg_cost'], True)}, "
                         f"mark {price(pos['mark_price'], True)} ({pos['mark_source']}), "
                         f"value {money(pos['market_value'], True)}, "
                         f"unrealized {money(pos['unrealized_pnl'], True)}")
            if not pos.get("mark_is_live", True):
                lines.append(f"      {pos.get('stale_reason', '')}")
    else:
        lines.append("  positions:      none")
    lines += [
        f"  total equity:   {money(p['total_equity'], True)} {cur} "
        f"(started with {money(p['starting_balance'], True)})",
        f"  realized P&L:   {money(q['realized_pnl'], True)} {cur}",
        f"  unrealized P&L: {money(q['unrealized_pnl'], True)} {cur}",
        f"  total P&L:      {money(q['total_pnl'], True)} {cur} ({pct(q['return_pct'])}%)",
        f"  fees paid:      {money(q['fees_paid'], True)} {cur}",
        f"  trades:         {q['trade_count']} ({q['buys']} buys, {q['sells']} sells)",
    ]
    lines += _risk_lines(q["risk"], cur)
    lines += _paper_lines(s, err)
    lines.append(f"  {p['mark_note']}")
    lines.append("  (display rounded: USDT to 2 decimals, quantities to 8; the ledger keeps full precision)")
    return "\n".join(lines)


def _check() -> str:
    """Run scripts/ledger_check.py in a separate Python process: its own temporary
    database, fake price sources, no network. A separate process so the script's
    imports never mix with the gateway's modules."""
    if not CHECK_SCRIPT.is_file():
        return f"attest check: {CHECK_SCRIPT.name} is missing; nothing was run."
    with tempfile.TemporaryDirectory(prefix="attest-selfcheck-") as tmp:
        try:
            done = subprocess.run([sys.executable, "-I", str(CHECK_SCRIPT)], cwd=tmp, capture_output=True,
                                  text=True, timeout=CHECK_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return f"attest check: did not finish within {CHECK_TIMEOUT_SECONDS} seconds; treat it as FAILED."
    keep = [ln for ln in done.stdout.splitlines() if ln.startswith(("PASS", "FAIL")) or ln.endswith("passed")]
    if not any(ln.endswith("passed") for ln in keep):
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-5:]
        return "attest check: the self test did not complete; treat it as FAILED.\n" + "\n".join(tail)
    verdict = "all checks passed" if done.returncode == 0 else "SOME CHECKS FAILED"
    return ("attest check (temporary database, fake price sources, no network; your ledger was not touched):\n"
            + "\n".join(keep) + f"\nresult: {verdict}")


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
            if sub == "check":
                return _check()
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
                return (f"attest: paper account reset. Starting balance {money(out['starting_balance'], True)} "
                        f"{out['currency']}, no trades, no positions, kill switch off.")
            return USAGE
        except Rejected as r:
            return f"attest: {r.reason}"
        except Exception as exc:
            return f"attest: that didn't work ({exc.__class__.__name__}: {exc})"
    return handle
