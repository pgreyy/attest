"""attest: an authoritative paper trading ledger for Hermes agents.

The agent makes decisions; the plugin owns the money. Orders go through
paper_order, and the agent learns its balance and P&L only by calling
get_portfolio, get_pnl and get_quote, which read straight from the ledger.

Orders are priced from live exchange quotes (prices.py: OKX, then Kraken,
then Binance) with slippage and fees, after the risk checks in ledger.py.
intercept.py guards one named external order tool in paper mode (Phase 4);
claims.py appends a soft note when the reply states a figure the ledger
contradicts (Phase 5).
"""
import threading
import time
from decimal import Decimal
from pathlib import Path

from . import claims, commands, display, intercept, tools
from .ledger import DEFAULT_POLICY, DEFAULT_STARTING_BALANCE, Ledger
from .prices import BinanceSource, DemoSource, FallbackSource, KrakenSource, OkxSource

DB_NAME = "attest.db"

# config key -> (lowest, highest) accepted; anything else falls back to the default
_POLICY_LIMITS = {"fee_bps": (0, 1000), "slippage_bps": (0, 1000), "max_quote_age_seconds": (1, 300),
                  "max_order_notional": (Decimal("0.01"), Decimal("1e12")),
                  "max_daily_loss": (Decimal("0.01"), Decimal("1e12")),
                  "dedupe_window_seconds": (0, 3600)}

_CTX = None
_LEDGER = None
_LOCK = threading.Lock()
DB_PATH = None

# Sessions that called an attest tool; the claim check runs only in these. Kept in
# the ledger db (attest_sessions) so a gateway restart doesn't forget them; this
# set only saves repeat writes within one process.
_SESSIONS = set()
_SESSIONS_MAX = 2000

# Paper mode settings, re-read at most every SETTINGS_TTL seconds (pre_tool_call
# runs on every tool call). The last good read is kept if a read fails.
SETTINGS_TTL = 5.0
_SETTINGS = {"at": -1e18, "value": {"paper_mode": True, "intercept_tool": "", "intercept_args": {},
                                     "demo_feed": "off"}}
DEMO_FEED_MODES = ("off", "stale", "down")


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


def _flag(key, default: bool) -> bool:
    raw = _CTX.get_config(key, default) if _CTX is not None else default
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "off", "")
    return default if raw is None else bool(raw)


def _starting_balance() -> Decimal:
    value = _number("starting_balance", DEFAULT_STARTING_BALANCE)
    return value if value > 0 else DEFAULT_STARTING_BALANCE


def _policy() -> dict:
    return {key: _number(key, DEFAULT_POLICY[key], *_POLICY_LIMITS[key]) for key in DEFAULT_POLICY}


def paper_settings() -> dict:
    now = time.monotonic()
    if now - _SETTINGS["at"] < SETTINGS_TTL:
        return _SETTINGS["value"]
    try:
        tool = _CTX.get_config("intercept_tool", "") if _CTX is not None else ""
        mapping = _CTX.get_config("intercept_args", {}) if _CTX is not None else {}
        demo = _CTX.get_config("demo_feed", "off") if _CTX is not None else "off"
        demo = str(demo or "off").strip().lower()
        _SETTINGS["value"] = {"paper_mode": _flag("paper_mode", True),
                              "intercept_tool": tool.strip() if isinstance(tool, str) else "",
                              "intercept_args": mapping if mapping is not None else {},
                              "demo_feed": demo if demo in DEMO_FEED_MODES else "off"}
        _SETTINGS["at"] = now
    except Exception:
        pass  # keep the last good settings
    return _SETTINGS["value"]


def demo_mode() -> str:
    return paper_settings().get("demo_feed", "off")


def note_session(session_id) -> None:
    """Remember that this session used attest, in memory and in the ledger db."""
    if not session_id or str(session_id) in _SESSIONS:
        return
    get_ledger().note_session(session_id)
    if len(_SESSIONS) >= _SESSIONS_MAX:
        _SESSIONS.clear()
    _SESSIONS.add(str(session_id))


def session_used(session_id) -> bool:
    if not session_id:
        return False
    return str(session_id) in _SESSIONS or get_ledger().session_used(session_id)


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
                feed = FallbackSource([DemoSource(src, demo_mode) for src in (OkxSource(), KrakenSource(),
                                                                              BinanceSource())],
                                      max_age=lambda: float(_policy()["max_quote_age_seconds"]))
                _LEDGER = Ledger(DB_PATH, _starting_balance(), prices=feed, policy=_policy)
    return _LEDGER


def on_transform_llm_output(response_text=None, session_id=None, **kwargs):
    """Append one 'attest check' note when the reply states a figure the ledger
    contradicts. Fail open: any problem leaves the reply exactly as it was."""
    try:
        if not isinstance(response_text, str) or not response_text.strip():
            return None
        if not claims.find_claims(response_text):
            return None  # nothing groundable: don't even read the ledger
        if not session_used(session_id):
            return None
        note = claims.check(response_text, get_ledger().figures())
        if not note:
            return None
        return response_text.rstrip() + "\n\n" + note
    except Exception:
        return None


GUARD = intercept.Guard(paper_settings, lambda: get_ledger(), note_session, display.round_result)


def register(ctx):
    global _CTX
    _CTX = ctx
    tools.register(ctx, get_ledger, note_session)
    try:
        stub_on = _flag("demo_stub_tool", False)
    except Exception:
        stub_on = False
    if stub_on:  # test only: a fake "real" order tool for proving interception
        ctx.register_tool(name="demo_broker_order", toolset=tools.TOOLSET, schema=intercept.DEMO_STUB_SCHEMA,
                          handler=intercept.demo_stub_handler,
                          description=intercept.DEMO_STUB_SCHEMA["description"])
    ctx.register_hook("pre_tool_call", GUARD)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_command(
        "attest",
        commands.make_handler(get_ledger, _starting_balance, paper_settings),
        description="attest: paper trading ledger status, self-check, kill switch resume, reset",
        args_hint="status | check | resume | reset [amount]",
    )
