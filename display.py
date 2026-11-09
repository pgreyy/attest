"""Display rounding. The ledger keeps full precision; only what people and the
model read is rounded here:
    USDT amounts and returns  -> 2 decimals
    prices                    -> 2 decimals (8 for prices under 1 USDT, so small coins stay readable)
    coin quantities           -> 8 decimals
/attest status also adds thousands separators.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_KEYS = frozenset({
    "cash", "cash_after", "total_equity", "positions_value", "starting_balance", "market_value",
    "unrealized_pnl", "realized_pnl", "total_pnl", "fees_paid", "notional", "requested_notional", "fee",
    "needed", "available", "order_value", "max_order_notional", "max_daily_loss", "start_equity", "pnl",
    "loss_limit_remaining",
})
PRICE_KEYS = frozenset({"price", "bid", "ask", "avg_cost", "mark_price", "limit_price"})
QTY_KEYS = frozenset({"quantity", "held", "requested"})
PCT_KEYS = frozenset({"return_pct"})
NOTE = ("USDT amounts are rounded to 2 decimals and coin quantities to 8 for display; "
        "the ledger keeps full precision.")


def _dec(value):
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            d = Decimal(value.replace(",", ""))
            return d if d.is_finite() else None
        except (InvalidOperation, ValueError):
            return None
    return None


def _q(d: Decimal, places: str) -> Decimal:
    q = d.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return abs(q) if q == 0 else q


def money(value, sep: bool = False) -> str:
    d = _dec(value)
    if d is None:
        return str(value)
    return f"{_q(d, '0.01'):{',' if sep else ''}.2f}"


def price(value, sep: bool = False) -> str:
    d = _dec(value)
    if d is None:
        return str(value)
    if abs(d) >= 1:
        return money(d, sep)
    return format(_q(d, "0.00000001").normalize(), "f")


def qty(value) -> str:
    d = _dec(value)
    if d is None:
        return str(value)
    return f"{_q(d, '0.00000001'):.8f}"


def pct(value) -> str:
    d = _dec(value)
    if d is None:
        return str(value)
    return f"{_q(d, '0.01'):.2f}"


def round_result(obj, top: bool = True):
    """A copy of a tool result with every known amount rounded for display."""
    if isinstance(obj, list):
        return [round_result(v, False) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            out[key] = round_result(value, False)
        elif value is None or isinstance(value, bool) or _dec(value) is None:
            out[key] = value
        elif key in MONEY_KEYS:
            out[key] = money(value)
        elif key in PRICE_KEYS:
            out[key] = price(value)
        elif key in QTY_KEYS:
            out[key] = qty(value)
        elif key in PCT_KEYS:
            out[key] = pct(value)
        else:
            out[key] = value
    if top and out.get("ok") is not None:
        out["display_note"] = NOTE
    return out
