"""attest ledger: the authoritative paper account.

SQLite, one file. Every amount is a Decimal in memory and a string on disk;
no money math is ever done in float. Positions use the average cost method,
kept as a total cost basis so a full close leaves no rounding residue:

    buy:   cost_basis += qty * price + fee       cash -= qty * price + fee
    sell:  removed = cost_basis * sold / held    cash += qty * price - fee
           realized = qty * price - fee - removed

So realized + unrealized always equals equity - starting balance exactly.

Phase 2 broker: every fill is priced from a live quote (best bid and ask).
    market buy  -> ask * (1 + slippage)     market sell -> bid * (1 - slippage)
    limit       -> fills only if marketable now (buy limit >= ask, sell limit
                   <= bid), at the same market price plus slippage; otherwise
                   rejected, because attest holds no resting orders.
    fee         -> notional * fee_bps / 10000
A quote older than max_quote_age_seconds is never used. If there is no fresh
quote the order is rejected; there is no fallback price.

This module has no Hermes imports and no network code; the price source is
passed in, so scripts/ledger_check.py can run it with a fake one.
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

SCHEMA_VERSION = 2
CURRENCY = "USDT"
DEFAULT_STARTING_BALANCE = Decimal("10000")
DEFAULT_POLICY = {"fee_bps": 10, "slippage_bps": 5, "max_quote_age_seconds": 10}
SOURCE = "attest ledger"
PREC = 50  # digits of Decimal precision for ledger math
BPS = Decimal("10000")

# Quote columns added in schema 2. Nullable: Phase 1 trades have no quote.
_QUOTE_COLUMNS = ("quote_bid", "quote_ask", "quote_fetched_at", "price_source", "slippage_bps")

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
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id  TEXT UNIQUE,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty              TEXT NOT NULL,
    price            TEXT NOT NULL,
    fee              TEXT NOT NULL,
    realized_pnl     TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    note             TEXT,
    quote_bid        TEXT,
    quote_ask        TEXT,
    quote_fetched_at TEXT,
    price_source     TEXT,
    slippage_bps     TEXT
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
# Pairs quoted in something other than USDT can't be settled against USDT cash.
_OTHER_QUOTES = ("USDC", "FDUSD", "BUSD", "TUSD", "DAI", "BTC", "ETH", "BNB", "EUR", "TRY", "BRL", "JPY")
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


def ts_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    """'btc/usdt', 'BTC-USDT', 'BTCUSD', 'btc' -> 'BTCUSDT'. USDT pairs only."""
    if not isinstance(raw, str) or not raw.strip():
        raise Rejected("missing_symbol", "symbol is required, for example BTC or BTCUSDT.")
    sym = re.sub(r"[\s/_-]", "", raw).upper()
    if not _SYMBOL_RE.match(sym):
        raise Rejected("invalid_symbol", f"symbol {raw!r} is not valid, use something like BTC or BTCUSDT.")
    if sym.endswith("USDT") and len(sym) > 4:
        return sym
    if sym.endswith("USD") and len(sym) > 3:
        return sym + "T"
    for q in _OTHER_QUOTES:
        if sym.endswith(q) and len(sym) > len(q) + 1:
            raise Rejected("unsupported_pair",
                           f"{sym} is quoted in {q}; the paper account holds {CURRENCY}, so only "
                           f"{CURRENCY} pairs are supported (for example {sym[:-len(q)]}USDT).")
    return sym + "USDT"  # a bare base asset such as BTC or ETH


def _quote_view(q, age) -> dict:
    return {"bid": fmt(q.bid), "ask": fmt(q.ask), "fetched_at": ts_iso(q.fetched_at),
            "age_seconds": float(round(age, 3)), "source": q.source}


class Ledger:
    """Opens a fresh connection per operation, so it is safe across threads and
    a gateway restart simply means the next call reads the same file.

    prices: an object with quote(symbol) -> prices.Quote, raising prices.PriceError.
    policy: a dict, or a callable returning one, with fee_bps, slippage_bps,
            max_quote_age_seconds (read on every order, so config edits apply).
    clock:  returns unix seconds; tests pass a fake one.
    """

    def __init__(self, path, starting_balance=DEFAULT_STARTING_BALANCE, prices=None, policy=None,
                 clock=time.time):
        self.path = Path(path)
        self.default_start = Decimal(starting_balance)
        self.prices = prices
        self._policy = policy if policy is not None else dict(DEFAULT_POLICY)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)
            con.execute("BEGIN IMMEDIATE")
            try:
                self._migrate(con)
                self._ensure_account(con)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
        return _Closing(con)

    @staticmethod
    def _migrate(con) -> None:
        """Schema 1 -> 2: add the nullable quote columns to trades. Nothing else changes."""
        have = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        for col in _QUOTE_COLUMNS:
            if col not in have:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col} TEXT")
        con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))

    def _ensure_account(self, con, balance: Decimal | None = None) -> None:
        if con.execute("SELECT 1 FROM account WHERE id = 1").fetchone() is None:
            start = self.default_start if balance is None else balance
            con.execute("INSERT OR IGNORE INTO account (id, cash, starting_balance, currency, created_at) "
                        "VALUES (1, ?, ?, ?, ?)", (str(start), str(start), CURRENCY, now_iso()))

    @staticmethod
    def _account(con) -> sqlite3.Row:
        return con.execute("SELECT * FROM account WHERE id = 1").fetchone()

    def policy(self) -> dict:
        raw = self._policy() if callable(self._policy) else self._policy
        out = {}
        for key, default in DEFAULT_POLICY.items():
            try:
                out[key] = Decimal(str(raw.get(key, default)))
            except Exception:
                out[key] = Decimal(default)
        return out

    # -- orders -----------------------------------------------------------

    def place_order(self, args) -> dict:
        """Validate, price and fill one order. Returns a structured dict; a refused
        order comes back as {"ok": False, "status": "rejected", ...}, never raises
        for bad input or a bad feed."""
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
        limit = None
        if order_type == "limit":
            limit = to_decimal(args.get("limit_price"), "limit_price")
            if limit <= 0:
                raise Rejected("invalid_limit_price", f"limit_price must be greater than 0, got {fmt(limit)}.")
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
        return {"side": side, "symbol": symbol, "qty": qty, "limit": limit,
                "order_type": order_type, "note": note, "coid": coid}

    @staticmethod
    def _check_duplicate(con, coid) -> None:
        if coid is None:
            return
        dup = con.execute("SELECT id FROM trades WHERE client_order_id = ?", (coid,)).fetchone()
        if dup is not None:
            raise Rejected("duplicate_client_order_id",
                           f"client_order_id {coid!r} was already used by trade {dup['id']}; "
                           "nothing new was placed.", existing_trade_id=dup["id"])

    def _quote(self, symbol: str, max_age: Decimal):
        """A fresh quote and its age in seconds, or Rejected. Never a fallback price."""
        if self.prices is None:
            raise Rejected("feed_unavailable", "no price feed is configured, so nothing can be filled.")
        try:
            q = self.prices.quote(symbol)
        except Exception as exc:
            code = getattr(exc, "code", "feed_unavailable")
            reason = getattr(exc, "reason", None) or f"{exc.__class__.__name__}: {exc}"
            if code not in ("unknown_symbol", "bad_quote"):
                code = "feed_unavailable"
            raise Rejected(code, f"{reason} The order was not filled; attest never fills at a guessed price.")
        age = Decimal(str(self.clock())) - Decimal(str(q.fetched_at))
        if age > max_age or age < -1:
            raise Rejected("stale_quote",
                           f"the latest {symbol} quote is {fmt(age.quantize(Decimal('0.001')))} seconds old, "
                           f"over the {fmt(max_age)} second limit. The order was not filled.",
                           quote=_quote_view(q, float(age)))
        return q, age

    def _place(self, args) -> dict:
        o = self._parse(args)
        pol = self.policy()
        with self._connect() as con:  # fail fast on a reused id, before touching the network
            self._check_duplicate(con, o["coid"])
        q, age = self._quote(o["symbol"], pol["max_quote_age_seconds"])
        with localcontext() as ctx, self._connect() as con:
            ctx.prec = PREC
            con.execute("BEGIN IMMEDIATE")
            try:
                result = self._fill(con, o, q, age, pol)
                con.execute("COMMIT")
                return result
            except BaseException:
                con.execute("ROLLBACK")
                raise

    def _fill(self, con, o, q, age, pol) -> dict:
        self._check_duplicate(con, o["coid"])
        quote = _quote_view(q, float(age))
        sym, qty, side, limit = o["symbol"], o["qty"], o["side"], o["limit"]
        slip, fee_rate = pol["slippage_bps"] / BPS, pol["fee_bps"] / BPS
        if side == "buy":
            if limit is not None and limit < q.ask:
                raise Rejected("not_marketable",
                               f"buy limit {fmt(limit)} is below the current ask {fmt(q.ask)}. attest does "
                               "not hold resting orders, so the order was not placed.", quote=quote)
            price = q.ask * (1 + slip)
        else:
            if limit is not None and limit > q.bid:
                raise Rejected("not_marketable",
                               f"sell limit {fmt(limit)} is above the current bid {fmt(q.bid)}. attest does "
                               "not hold resting orders, so the order was not placed.", quote=quote)
            price = q.bid * (1 - slip)
        notional = qty * price
        fee = notional * fee_rate

        acct = self._account(con)
        cash = Decimal(acct["cash"])
        pos = con.execute("SELECT * FROM positions WHERE symbol = ?", (sym,)).fetchone()
        held = Decimal(pos["qty"]) if pos else Decimal("0")
        basis = Decimal(pos["cost_basis"]) if pos else Decimal("0")
        ts = now_iso()

        if side == "buy":
            cost = notional + fee
            if cost > cash:
                raise Rejected("insufficient_cash",
                               f"buy needs {fmt(cost)} {CURRENCY} including fee but only {fmt(cash)} "
                               f"{CURRENCY} is available.", needed=fmt(cost), available=fmt(cash), quote=quote)
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
            "INSERT INTO trades (client_order_id, symbol, side, qty, price, fee, realized_pnl, timestamp, note, "
            "quote_bid, quote_ask, quote_fetched_at, price_source, slippage_bps) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o["coid"], sym, side, str(qty), str(price), str(fee), str(realized), ts, o["note"],
             str(q.bid), str(q.ask), quote["fetched_at"], q.source, str(pol["slippage_bps"])))
        if held == 0:
            con.execute("DELETE FROM positions WHERE symbol = ?", (sym,))
        else:
            con.execute("INSERT INTO positions (symbol, qty, cost_basis, last_price, updated_at) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET qty = excluded.qty, "
                        "cost_basis = excluded.cost_basis, last_price = excluded.last_price, "
                        "updated_at = excluded.updated_at",
                        (sym, str(held), str(basis), str(price), ts))
        trade = {"id": cur.lastrowid, "client_order_id": o["coid"], "symbol": sym, "side": side,
                 "order_type": o["order_type"], "quantity": fmt(qty), "price": fmt(price),
                 "notional": fmt(notional), "fee": fmt(fee), "fee_bps": fmt(pol["fee_bps"]),
                 "slippage_bps": fmt(pol["slippage_bps"]), "quote": quote, "timestamp": ts, "note": o["note"]}
        if limit is not None:
            trade["limit_price"] = fmt(limit)
        if side == "sell":
            trade["realized_pnl"] = fmt(realized)
        return {
            "ok": True, "status": "filled", "trade": trade,
            "cash_after": fmt(cash), "currency": CURRENCY,
            "position_after": {"symbol": sym, "quantity": fmt(held),
                               "avg_cost": fmt(basis / held) if held else "0"},
            "fill_policy": self.fill_policy(pol), "source": SOURCE, "as_of": ts,
        }

    @staticmethod
    def fill_policy(pol) -> str:
        return (f"Filled from the live quote: buys at ask, sells at bid, plus {fmt(pol['slippage_bps'])} bps "
                f"slippage against the trader, and a {fmt(pol['fee_bps'])} bps fee. Limit orders fill only "
                "when marketable now, at that same price (not at the limit); attest holds no resting orders.")

    # -- reporting --------------------------------------------------------

    def _read(self):
        with self._connect() as con:
            acct = self._account(con)
            rows = con.execute("SELECT * FROM positions ORDER BY symbol").fetchall()
            t = con.execute("SELECT COUNT(*) AS n, "
                            "SUM(CASE WHEN side = 'buy' THEN 1 ELSE 0 END) AS buys, "
                            "SUM(CASE WHEN side = 'sell' THEN 1 ELSE 0 END) AS sells FROM trades").fetchone()
            pnl_rows = con.execute("SELECT realized_pnl, fee FROM trades").fetchall()
        return acct, rows, t, pnl_rows

    def _mark(self, symbol: str, last_fill: Decimal, max_age: Decimal) -> dict:
        """Live bid if a fresh quote is available, else the last fill price labelled stale."""
        try:
            q, age = self._quote(symbol, max_age)
            return {"price": q.bid, "live": True, "source": "live bid",
                    "quote_fetched_at": ts_iso(q.fetched_at), "quote_source": q.source}
        except Rejected as r:
            return {"price": last_fill, "live": False, "source": "last fill (STALE)",
                    "stale_reason": r.reason.split(" The order")[0]}

    def _snapshot(self) -> dict:
        acct, rows, t, pnl_rows = self._read()
        max_age = self.policy()["max_quote_age_seconds"]
        cash = Decimal(acct["cash"])
        start = Decimal(acct["starting_balance"])
        positions, value, basis_total, stale = [], Decimal("0"), Decimal("0"), []
        for r in rows:
            qty, basis = Decimal(r["qty"]), Decimal(r["cost_basis"])
            m = self._mark(r["symbol"], Decimal(r["last_price"]), max_age)
            mv = qty * m["price"]
            value += mv
            basis_total += basis
            p = {"symbol": r["symbol"], "quantity": fmt(qty), "avg_cost": fmt(basis / qty),
                 "mark_price": fmt(m["price"]), "mark_source": m["source"], "mark_is_live": m["live"],
                 "market_value": fmt(mv), "unrealized_pnl": fmt(mv - basis)}
            if m["live"]:
                p["mark_quote_fetched_at"] = m["quote_fetched_at"]
            else:
                p["stale_reason"] = m["stale_reason"]
                stale.append(r["symbol"])
            positions.append(p)
        realized = sum((Decimal(r["realized_pnl"]) for r in pnl_rows), Decimal("0"))
        fees = sum((Decimal(r["fee"]) for r in pnl_rows), Decimal("0"))
        if not positions:
            note = "no open positions, nothing to mark"
        elif stale:
            note = (f"STALE MARKS: {', '.join(stale)} could not get a live quote and "
                    f"{'is' if len(stale) == 1 else 'are'} marked at the last fill price. Unrealized P&L "
                    "and equity are NOT live; say so when reporting them.")
        else:
            note = "marks: live Binance bid"
        return {"acct": acct, "cash": cash, "start": start, "positions": positions, "value": value,
                "basis": basis_total, "realized": realized, "fees": fees, "marks_live": not stale,
                "mark_note": note, "trades": t["n"] or 0, "buys": t["buys"] or 0, "sells": t["sells"] or 0}

    def portfolio(self) -> dict:
        with localcontext() as ctx:
            ctx.prec = PREC
            s = self._snapshot()
            return {
                "ok": True, "source": SOURCE, "as_of": now_iso(), "currency": CURRENCY,
                "cash": fmt(s["cash"]), "positions": s["positions"],
                "positions_value": fmt(s["value"]), "total_equity": fmt(s["cash"] + s["value"]),
                "starting_balance": fmt(s["start"]), "account_created": s["acct"]["created_at"],
                "marks_live": s["marks_live"], "mark_note": s["mark_note"],
            }

    def pnl(self) -> dict:
        with localcontext() as ctx:
            ctx.prec = PREC
            s = self._snapshot()
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
                "marks_live": s["marks_live"], "mark_note": s["mark_note"],
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
