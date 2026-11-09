"""attest: an authoritative paper trading ledger for Hermes agents.

The agent makes decisions; the plugin owns the money. Orders go through
paper_order, and the agent learns its balance and P&L only by calling
get_portfolio and get_pnl, which read straight from the ledger.

Phase 2: orders are priced from live Binance quotes (prices.py) with
slippage and fees. Risk controls (a real pre_tool_call) come in Phase 3.
"""
import threading
from decimal import Decimal
from pathlib import Path

from . import commands, tools
from .ledger import DEFAULT_POLICY, DEFAULT_STARTING_BALANCE, Ledger
from .prices import BinanceSource

DB_NAME = "attest.db"

# config key -> (lowest, highest) accepted; anything else falls back to the default
_POLICY_LIMITS = {"fee_bps": (0, 1000), "slippage_bps": (0, 1000), "max_quote_age_seconds": (1, 300)}

_CTX = None
_LEDGER = None
_LOCK = threading.Lock()
DB_PATH = None


def _number(key, default, low=None, high=None) -> Decimal:
    try:
        raw = _CTX.get_config(key, None) if _CTX is not None else None
        if raw is None or isinstance(raw, bool):
            return Decimal(default)
        value = Decimal(str(raw))
        if not value.is_finite() or (low is not None and value < low) or (high is not None and value > high):
            return Decimal(default)
        return value
    except Exception:  # InvalidOperation or a bad config value: use the default
        return Decimal(default)


def _starting_balance() -> Decimal:
    value = _number("starting_balance", DEFAULT_STARTING_BALANCE)
    return value if value > 0 else DEFAULT_STARTING_BALANCE


def _policy() -> dict:
    return {key: _number(key, DEFAULT_POLICY[key], *_POLICY_LIMITS[key]) for key in DEFAULT_POLICY}


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
                _LEDGER = Ledger(DB_PATH, _starting_balance(), prices=BinanceSource(), policy=_policy)
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
