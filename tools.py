"""The four agent tools: schemas and handlers.

Descriptions are written in the words a user would say, because the model
finds plugin tools by searching them (tool_search). A search for jargon such
as "BTCUSDT limit order" found nothing in the Phase 0 probe; "paper trading
order" did.

Every handler returns a JSON string and never raises: a crash comes back as
{"ok": false, "error": ...}. There is deliberately no tool that resets the
account or resumes the kill switch; those are /attest commands for a human.
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
        "trade, invest in, open or close a position in crypto such as BTC or ETH. "
        "Give the size as either quantity (number of coins) or notional (a dollar amount in USDT), "
        "never both: \"buy $500 of BTC\" means notional 500, \"buy 0.01 BTC\" means quantity 0.01. "
        "You never need to know or guess the price; the order is priced from the live market. "
        "Orders fill at the live price (buy at the ask, sell at the bid) plus slippage and a fee. "
        "A limit order fills only if the market is already at or better than the limit, and never "
        "worse than the limit; otherwise it is rejected, because there are no resting orders. "
        "Set a new unique client_order_id for every order you intend to place, and reuse the same "
        "client_order_id if you retry that same order; a reused id returns the original fill instead "
        "of trading twice. Orders can be rejected by risk limits (a maximum order size and a daily "
        "loss kill switch); report the reason as given. The result is the ledger's record of the "
        "fill or the reason it was rejected; never make up a price or a fill."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Coin or pair, for example BTC, ETH or BTCUSDT"},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "number", "description": "How many coins, for example 0.01 BTC. Use this or notional."},
            "notional": {"type": "number",
                         "description": "Dollar amount in USDT, for example 500 for \"$500 of BTC\". Use this or quantity."},
            "order_type": {"type": "string", "enum": ["market", "limit"],
                           "description": "market (default) fills now at the live price; limit fills now only if marketable"},
            "limit_price": {"type": "number", "description": "Price per coin in USDT, for limit orders only"},
            "note": {"type": "string", "description": "Optional reason for the trade"},
            "client_order_id": {"type": "string",
                                "description": "A new unique id for each intended order; reuse it only when retrying that same order"},
        },
        "required": ["symbol", "side"],
    },
}

GET_QUOTE_SCHEMA = {
    "name": "get_quote",
    "description": (
        "Live crypto price: get the current price quote (best bid and ask) for a coin such as BTC or ETH, "
        "with its exchange source, quote time and age. Use it whenever the user asks for a price or quote, "
        "how much is BTC, what's ETH trading at, or what something costs right now. Always call it instead "
        "of stating a price from memory; if it returns an error, say no live price is available."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Coin or pair, for example BTC, ETH or BTCUSDT"},
        },
        "required": ["symbol"],
    },
}

GET_PORTFOLIO_SCHEMA = {
    "name": "get_portfolio",
    "description": (
        "Paper trading account: get the portfolio, balance, cash, holdings and positions, "
        "total account value (equity), the risk limits and whether the daily loss kill switch is on. "
        "Use it whenever the user asks what do I own, what's my balance, how much cash do I have, "
        "show my account or my holdings. " + _TRUTH
    ),
    "parameters": {"type": "object", "properties": {}},
}

GET_PNL_SCHEMA = {
    "name": "get_pnl",
    "description": (
        "Paper trading account: get profit and loss (P&L), realized and unrealized gains, returns, "
        "performance and today's P&L against the daily loss limit. Use it whenever the user asks how am "
        "I doing, am I up or down, how much have I made or lost, what's my return. " + _TRUTH
    ),
    "parameters": {"type": "object", "properties": {}},
}


def _safe(fn):
    def handler(args=None, **kwargs):
        try:
            return json.dumps(fn(args if isinstance(args, dict) else {}, kwargs), default=str)
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}",
                               "source": SOURCE, "as_of": now_iso(),
                               "note": "Internal error; nothing was changed. Do not guess the figures."})
    handler.__name__ = getattr(fn, "__name__", "handler")
    return handler


def register(ctx, get_ledger) -> None:
    tools = [
        (PAPER_ORDER_SCHEMA, lambda a, kw: get_ledger().place_order(a, session_id=kw.get("session_id"))),
        (GET_QUOTE_SCHEMA, lambda a, kw: get_ledger().get_quote(a.get("symbol"))),
        (GET_PORTFOLIO_SCHEMA, lambda a, kw: get_ledger().portfolio()),
        (GET_PNL_SCHEMA, lambda a, kw: get_ledger().pnl()),
    ]
    for schema, fn in tools:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=_safe(fn), description=schema["description"])
