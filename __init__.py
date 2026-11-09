"""attest: an authoritative paper trading ledger for Hermes agents.

The agent makes decisions; the plugin owns the money. Orders go through
paper_order, and the agent learns its balance and P&L only by calling
get_portfolio and get_pnl, which read straight from the ledger.

Phase 1: ledger, the three tools and /attest. Price feed and fees come in
Phase 2, risk controls (a real pre_tool_call) in Phase 3.
"""
import threading
from decimal import Decimal
from pathlib import Path

from . import commands, tools
from .ledger import DEFAULT_STARTING_BALANCE, Ledger

DB_NAME = "attest.db"

_CTX = None
_LEDGER = None
_LOCK = threading.Lock()
DB_PATH = None


def _starting_balance() -> Decimal:
    try:
        raw = _CTX.get_config("starting_balance", None) if _CTX is not None else None
        if raw is None or isinstance(raw, bool):
            return DEFAULT_STARTING_BALANCE
        value = Decimal(str(raw))
        return value if value.is_finite() and value > 0 else DEFAULT_STARTING_BALANCE
    except Exception:  # InvalidOperation or a bad config value: use the default
        return DEFAULT_STARTING_BALANCE


def _data_dir() -> Path:
    try:
        return Path(_CTX.state.data_dir)
    except Exception:
        return Path(__file__).parent / "data"


def get_ledger() -> Ledger:
    global _LEDGER, DB_PATH
    if _LEDGER is None:
        with _LOCK:
            if _LEDGER is None:
                DB_PATH = _data_dir() / DB_NAME
                _LEDGER = Ledger(DB_PATH, _starting_balance())
    return _LEDGER


def register(ctx):
    global _CTX
    _CTX = ctx
    tools.register(ctx, get_ledger)
    ctx.register_command(
        "attest",
        commands.make_handler(get_ledger, _starting_balance),
        description="attest: paper trading ledger status and reset",
        args_hint="status | reset [amount]",
    )
