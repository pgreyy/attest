"""attest ledger: the authoritative paper account.

SQLite, one file. Every amount is a Decimal in memory and a string on disk;
no money math is ever done in float. Positions use the average cost method,
kept as a total cost basis so a full close leaves no rounding residue:

    buy:   cost_basis += qty * price + fee       cash -= qty * price + fee
    sell:  removed = cost_basis * sold / held    cash += qty * price - fee
           realized = qty * price - fee - removed

So realized + unrealized always equals equity - starting balance exactly.

This module has no Hermes imports, so scripts/ledger_check.py can run it
against a throwaway database.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

SCHEMA_VERSION = 1
CURRENCY = "USDT"
DEFAULT_STARTING_BALANCE = Decimal("10000")
SOURCE = "attest ledger"
MARK_NOTE = "mark: last fill, live prices arrive in Phase 2"
FILL_POLICY = "Phase 1: fills at limit_price, no fee, no slippage. Live prices and fees arrive in Phase 2."
PREC = 50  # digits of Decimal precision for ledger math

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    cash             TEXT NOT NULL,
    starting_balance TEXT NOT NULL,
    currency         TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty             TEXT NOT NULL,
    price           TEXT NOT NULL,
    fee             TEXT NOT NULL,
    realized_pnl    TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    note            TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    symbol     TEXT PRIMARY KEY,
    qty        TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    last_price TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
_MAX_NOTE = 500
_MAX_COID = 64


class Rejected(Exception):
    """An order the ledger refuses. Carries a machine code and a plain reason."""

    def __init__(self, code: str, reason: str, **extra):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.extra = extra


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def to_decimal(value, field: str) -> Decimal:
    """Turn a tool argument into a Decimal at the boundary. Floats go through
    str() so 0.01 stays 0.01 and does not become 0.01000000000000000020816..."""
    if value is None or value == "":
        raise Rejected("missing_" + field, f"{field} is required.")
    if isinstance(value, bool):
        raise Rejected("invalid_" + field, f"{field} must be a number, not true/false.")
    try:
        if isinstance(value, float):
            d = Decimal(str(value))
        elif isinstance(value, (int, Decimal)):
            d = Decimal(value)
        elif isinstance(value, str):
            d = Decimal(value.strip().replace(",", ""))
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise Rejected("invalid_" + field, f"{field} must be a number, got {value!r}.")
    if not d.is_finite():
        raise Rejected("invalid_" + field, f"{field} must be a finite number.")
    return d


def fmt(d: Decimal) -> str:
    """Exact plain-decimal string, no exponent, no trailing zeros, at most 12 places."""
    if d == 0:
        return "0"
    if d.as_tuple().exponent < -12:
        d = d.quantize(Decimal("1e-12"))
    s = format(d.normalize(), "f")
    return "0" if s in ("-0", "0") else s


def normalize_symbol(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise Rejected("missing_symbol", "symbol is required, for example BTCUSDT.")
    sym = re.sub(r"[\s/_-]", "", raw).upper()
    if not _SYMBOL_RE.match(sym):
        raise Rejected("invalid_symbol", f"symbol {raw!r} is not a valid pair, use something like BTCUSDT.")
    return sym


class Ledger:
    """Opens a fresh connection per operation, so it is safe across threads and
    a gateway restart simply means the next call reads the same file."""

    def __init__(self, path, starting_balance=DEFAULT_STARTING_BALANCE):
        self.path = Path(path)
        self.default_start = Decimal(starting_balance)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),))
            self._ensure_account(con)

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
        return _Closing(con)

    def _ensure_account(self, con, balance: Decimal | None = None) -> None:
        if con.execute("SELECT 1 FROM account WHERE id = 1").fetchone() is None:
            start = self.default_start if balance is None else balance
            con.execute("INSERT OR IGNORE INTO account (id, cash, starting_balance, currency, created_at) "
                        "VALUES (1, ?, ?, ?, ?)", (str(start), str(start), CURRENCY, now_iso()))

    @staticmethod
    def _account(con) -> sqlite3.Row:
        return con.execute("SELECT * FROM account WHERE id = 1").fetchone()

    # -- orders -----------------------------------------------------------

    def place_order(self, args) -> dict:
        """Validate and fill one order. Returns a structured dict; a refused
        order comes back as {"ok": False, "status": "rejected", ...}, never raises
        for bad input."""
        try:
            return self._place(args)
        except Rejected as r:
            out = {"ok": False, "status": "rejected", "reason_code": r.code, "reason": r.reason,
                   "source": SOURCE, "as_of": now_iso()}
            out.update(r.extra)
            return out

    def _parse(self, args) -> dict:
        if not isinstance(args, dict):
            raise Rejected("invalid_args", "order arguments must be an object.")
        side = str(args.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            raise Rejected("invalid_side", f"side must be 'buy' or 'sell', got {args.get('side')!r}.")
        symbol = normalize_symbol(args.get("symbol"))
        qty = to_decimal(args.get("quantity"), "quantity")
        if qty <= 0:
            raise Rejected("invalid_quantity", f"quantity must be greater than 0, got {fmt(qty)}.")
        has_price = args.get("limit_price") not in (None, "")
        order_type = str(args.get("order_type") or ("limit" if has_price else "market")).strip().lower()
        if order_type not in ("market", "limit"):
            raise Rejected("invalid_order_type", f"order_type must be 'market' or 'limit', got {order_type!r}.")
        if order_type == "market":
            raise Rejected("market_not_available",
                           "Market orders need a live price. The price feed arrives in Phase 2; "
                           "for now place a limit order with a limit_price.")
        price = to_decimal(args.get("limit_price"), "limit_price")
        if price <= 0:
            raise Rejected("invalid_limit_price", f"limit_price must be greater than 0, got {fmt(price)}.")
        note = args.get("note")
        note = str(note)[:_MAX_NOTE] if note not in (None, "") else None
        coid = args.get("client_order_id")
        if coid in (None, ""):
            coid = None
        else:
            coid = str(coid).strip()
            if not coid or len(coid) > _MAX_COID:
                raise Rejected("invalid_client_order_id",
                               f"client_order_id must be 1 to {_MAX_COID} characters.")
        return {"side": side, "symbol": symbol, "qty": qty, "price": price,
                "order_type": order_type, "note": note, "coid": coid}

    def _place(self, args) -> dict:
        o = self._parse(args)
        fee = Decimal("0")  # Phase 2 sets the fee policy
        with localcontext() as ctx, self._connect() as con:
            ctx.prec = PREC
            con.execute("BEGIN IMMEDIATE")
            try:
                result = self._fill(con, o, fee)
                con.execute("COMMIT")
                return result
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def _fill(self, con, o, fee) -> dict:
        if o["coid"] is not None:
            dup = con.execute("SELECT id FROM trades WHERE client_order_id = ?", (o["coid"],)).fetchone()
            if dup is not None:
                raise Rejected("duplicate_client_order_id",
                               f"client_order_id {o['coid']!r} was already used by trade {dup['id']}; "
                               "nothing new was placed.", existing_trade_id=dup["id"])
        acct = self._account(con)
        cash = Decimal(acct["cash"])
        sym, qty, price = o["symbol"], o["qty"], o["price"]
        notional = qty * price
        pos = con.execute("SELECT * FROM positions WHERE symbol = ?", (sym,)).fetchone()
        held = Decimal(pos["qty"]) if pos else Decimal("0")
        basis = Decimal(pos["cost_basis"]) if pos else Decimal("0")
        ts = now_iso()

        if o["side"] == "buy":
            cost = notional + fee
            if cost > cash:
                raise Rejected("insufficient_cash",
                               f"buy needs {fmt(cost)} {CURRENCY} but only {fmt(cash)} {CURRENCY} is available.",
                               needed=fmt(cost), available=fmt(cash))
            cash -= cost
            held += qty
            basis += cost
            realized = Decimal("0")
        else:
            if qty > held:
                raise Rejected("insufficient_position",
                               f"cannot sell {fmt(qty)} {sym}, the account holds {fmt(held)}.",
                               requested=fmt(qty), held=fmt(held))
            removed = basis if qty == held else basis * qty / held
            proceeds = notional - fee
            realized = proceeds - removed
            cash += proceeds
            held -= qty
            basis -= removed

        con.execute("UPDATE account SET cash = ? WHERE id = 1", (str(cash),))
        cur = con.execute(
            "INSERT INTO trades (client_order_id, symbol, side, qty, price, fee, realized_pnl, timestamp, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o["coid"], sym, o["side"], str(qty), str(price), str(fee), str(realized), ts, o["note"]))
        if held == 0:
            con.execute("DELETE FROM positions WHERE symbol = ?", (sym,))
        else:
            con.execute("INSERT INTO positions (symbol, qty, cost_basis, last_price, updated_at) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET qty = excluded.qty, "
                        "cost_basis = excluded.cost_basis, last_price = excluded.last_price, "
                        "updated_at = excluded.updated_at",
                        (sym, str(held), str(basis), str(price), ts))
        trade = {"id": cur.lastrowid, "client_order_id": o["coid"], "symbol": sym, "side": o["side"],
                 "quantity": fmt(qty), "price": fmt(price), "notional": fmt(notional), "fee": fmt(fee),
                 "timestamp": ts, "note": o["note"]}
        if o["side"] == "sell":
            trade["realized_pnl"] = fmt(realized)
        return {
            "ok": True, "status": "filled", "trade": trade,
            "cash_after": fmt(cash), "currency": CURRENCY,
            "position_after": {"symbol": sym, "quantity": fmt(held),
                               "avg_cost": fmt(basis / held) if held else "0"},
            "fill_policy": FILL_POLICY, "source": SOURCE, "as_of": ts,
        }

    # -- reporting --------------------------------------------------------

    def _snapshot(self, con) -> dict:
        acct = self._account(con)
        cash = Decimal(acct["cash"])
        start = Decimal(acct["starting_balance"])
        rows = con.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
        positions, value, basis_total = [], Decimal("0"), Decimal("0")
        for r in rows:
            qty, basis, mark = Decimal(r["qty"]), Decimal(r["cost_basis"]), Decimal(r["last_price"])
            mv = qty * mark
            value += mv
            basis_total += basis
            positions.append({"symbol": r["symbol"], "quantity": fmt(qty), "avg_cost": fmt(basis / qty),
                              "mark_price": fmt(mark), "mark_source": "last fill",
                              "market_value": fmt(mv), "unrealized_pnl": fmt(mv - basis)})
        t = con.execute("SELECT COUNT(*) AS n, "
                        "SUM(CASE WHEN side = 'buy' THEN 1 ELSE 0 END) AS buys, "
                        "SUM(CASE WHEN side = 'sell' THEN 1 ELSE 0 END) AS sells FROM trades").fetchone()
        realized = sum((Decimal(r[0]) for r in con.execute("SELECT realized_pnl FROM trades")), Decimal("0"))
        fees = sum((Decimal(r[0]) for r in con.execute("SELECT fee FROM trades")), Decimal("0"))
        return {"acct": acct, "cash": cash, "start": start, "positions": positions, "value": value,
                "basis": basis_total, "realized": realized, "fees": fees,
                "trades": t["n"] or 0, "buys": t["buys"] or 0, "sells": t["sells"] or 0}

    def portfolio(self) -> dict:
        with localcontext() as ctx, self._connect() as con:
            ctx.prec = PREC
            s = self._snapshot(con)
            return {
                "ok": True, "source": SOURCE, "as_of": now_iso(), "currency": CURRENCY,
                "cash": fmt(s["cash"]), "positions": s["positions"],
                "positions_value": fmt(s["value"]), "total_equity": fmt(s["cash"] + s["value"]),
                "starting_balance": fmt(s["start"]), "account_created": s["acct"]["created_at"],
                "mark_note": MARK_NOTE,
            }

    def pnl(self) -> dict:
        with localcontext() as ctx, self._connect() as con:
            ctx.prec = PREC
            s = self._snapshot(con)
            equity = s["cash"] + s["value"]
            unrealized = s["value"] - s["basis"]
            total = s["realized"] + unrealized
            ret = (total / s["start"] * 100).quantize(Decimal("0.0001")) if s["start"] else Decimal("0")
            return {
                "ok": True, "source": SOURCE, "as_of": now_iso(), "currency": CURRENCY,
                "realized_pnl": fmt(s["realized"]), "unrealized_pnl": fmt(unrealized),
                "total_pnl": fmt(total), "return_pct": fmt(ret),
                "starting_balance": fmt(s["start"]), "total_equity": fmt(equity),
                "fees_paid": fmt(s["fees"]),
                "trade_count": s["trades"], "buys": s["buys"], "sells": s["sells"],
                "reconciles": total == equity - s["start"],
                "mark_note": MARK_NOTE,
            }

    # -- admin ------------------------------------------------------------

    def reset(self, balance=None) -> dict:
        """Wipe trades and positions and start a fresh account."""
        start = self.default_start if balance is None else to_decimal(balance, "amount")
        if start <= 0:
            raise Rejected("invalid_amount", "starting balance must be greater than 0.")
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DELETE FROM trades")
                con.execute("DELETE FROM positions")
                con.execute("DELETE FROM account")
                con.execute("DELETE FROM sqlite_sequence WHERE name = 'trades'")
                self._ensure_account(con, start)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        return {"ok": True, "starting_balance": fmt(start), "currency": CURRENCY}


class _Closing:
    """Context manager that closes the connection (sqlite3's own only ends a transaction)."""

    def __init__(self, con):
        self._con = con

    def __enter__(self):
        return self._con

    def __exit__(self, *exc):
        self._con.close()
        return False

    def __getattr__(self, name):
        return getattr(self._con, name)
