"""The three agent tools: schemas and handlers.

Descriptions are written in the words a user would say, because the model
finds plugin tools by searching them (tool_search). A search for jargon such
as "BTCUSDT limit order" found nothing in the Phase 0 probe; "paper trading
order" did.

Every handler returns a JSON string and never raises: a crash comes back as
{"ok": false, "error": ...}.
"""
from __future__ import annotations

import json

from .ledger import SOURCE, now_iso

TOOLSET = "attest"

_TRUTH = ("This is the only source of truth for the paper trading account. Always call it to get "
          "these numbers; never estimate, remember or calculate them yourself, and report the "
          "figures exactly as returned.")

PAPER_ORDER_SCHEMA = {
    "name": "paper_order",
    "description": (
        "Paper trading: place a pretend buy or sell order in the attest paper trading account "
        "(simulated money, nothing real is traded). Use it whenever the user wants to buy, sell, "
        "trade, invest in, open or close a position in crypto such as BTC or ETH. The result is the "
        "ledger's record of the fill or the reason it was rejected; report it as returned. "
        "Right now orders must be limit orders with a limit_price; they fill at that price."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Crypto pair, for example BTCUSDT or ETHUSDT"},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "number", "description": "How many coins, for example 0.01 BTC"},
            "order_type": {"type": "string", "enum": ["market", "limit"],
                           "description": "limit (works now) or market (needs the live price feed, not yet available)"},
            "limit_price": {"type": "number", "description": "Price per coin in USDT; required for limit orders"},
            "note": {"type": "string", "description": "Optional reason for the trade"},
            "client_order_id": {"type": "string",
                                "description": "Optional unique id for this order; reusing one never places a second trade"},
        },
        "required": ["symbol", "side", "quantity"],
    },
}

GET_PORTFOLIO_SCHEMA = {
    "name": "get_portfolio",
    "description": (
        "Paper trading account: get the portfolio, balance, cash, holdings and positions, "
        "total account value (equity). Use it whenever the user asks what do I own, what's my "
        "balance, how much cash do I have, show my account or my holdings. " + _TRUTH
    ),
    "parameters": {"type": "object", "properties": {}},
}

GET_PNL_SCHEMA = {
    "name": "get_pnl",
    "description": (
        "Paper trading account: get profit and loss (P&L), realized and unrealized gains, returns "
        "and performance. Use it whenever the user asks how am I doing, am I up or down, how much "
        "have I made or lost, what's my return. " + _TRUTH
    ),
    "parameters": {"type": "object", "properties": {}},
}


def _safe(fn):
    def handler(args=None, **kwargs):
        try:
            return json.dumps(fn(args if isinstance(args, dict) else {}), default=str)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}",
                               "source": SOURCE, "as_of": now_iso(),
                               "note": "Internal error; nothing was changed. Do not guess the figures."})
    handler.__name__ = getattr(fn, "__name__", "handler")
    return handler


def register(ctx, get_ledger) -> None:
    tools = [
        (PAPER_ORDER_SCHEMA, lambda a: get_ledger().place_order(a)),
        (GET_PORTFOLIO_SCHEMA, lambda a: get_ledger().portfolio()),
        (GET_PNL_SCHEMA, lambda a: get_ledger().pnl()),
    ]
    for schema, fn in tools:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=_safe(fn), description=schema["description"])
