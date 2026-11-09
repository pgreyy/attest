"""attest ledger: the authoritative paper account.

SQLite, one file. Every amount is a Decimal in memory and a string on disk;
no money math is ever done in float. Positions use the average cost method,
kept as a total cost basis so a full close leaves no rounding residue:

    buy:   cost_basis += qty * price + fee       cash -= qty * price + fee
    sell:  removed = cost_basis * sold / held    cash += qty * price - fee
           realized = qty * price - fee - removed

So realized + unrealized always equals equity - starting balance exactly.

Broker (Phase 2, limit cap from Phase 3): every fill is priced from a live quote.
    market buy  -> ask * (1 + slippage)       market sell -> bid * (1 - slippage)
    limit buy   -> fills only if limit >= ask, at min(ask * (1 + slippage), limit)
    limit sell  -> fills only if limit <= bid, at max(bid * (1 - slippage), limit)
    fee         -> notional * fee_bps / 10000
    notional    -> an order may give a USDT amount instead of a quantity; the
                   quantity is notional / ask (buys) or / bid (sells), rounded
                   down to 8 decimals.
A quote older than max_quote_age_seconds is never used. No fresh quote, no fill.

Risk (Phase 3), checked before any fill:
    order size  -> qty * ask (buys) or bid (sells) above max_order_notional is rejected.
    kill switch -> the first order of each UTC day snapshots that day's starting
                   equity. Once equity at live marks has fallen by max_daily_loss or
                   more, the switch trips and is stored. While tripped, buys are
                   rejected (the stored state is checked before any network call);
                   sells, which can only reduce a position, still go through.
                   It clears at the next UTC day or when a human runs /attest resume.
    idempotency -> a reused client_order_id returns the original fill unchanged.
                   An identical order in the same session within
                   dedupe_window_seconds, without a client_order_id, is rejected.

This module has no Hermes imports and no network code; the price source is
passed in, so scripts/ledger_check.py can run it with fake ones.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation, localcontext
from pathlib import Path

SCHEMA_VERSION = 4
CURRENCY = "USDT"
DEFAULT_STARTING_BALANCE = Decimal("10000")
DEFAULT_POLICY = {"fee_bps": 10, "slippage_bps": 5, "max_quote_age_seconds": 10,
                  "max_order_notional": 1000, "max_daily_loss": 500, "dedupe_window_seconds": 10}
SOURCE = "attest ledger"
PREC = 50  # digits of Decimal precision for ledger math
BPS = Decimal("10000")
QTY_STEP = Decimal("0.00000001")  # quantity precision when an order is given in USDT
FUTURE_TOLERANCE = Decimal("2")   # seconds a quote time may be ahead of our clock
NOT_FILLED = " The order was not filled; attest never fills at a guessed price."

# Columns added after schema 1, all nullable: older trades simply have no value.
_ADDED_TRADE_COLUMNS = (
    ("quote_bid", "TEXT"), ("quote_ask", "TEXT"), ("quote_fetched_at", "TEXT"),
    ("price_source", "TEXT"), ("slippage_bps", "TEXT"),                         # schema 2
    ("quote_time", "TEXT"), ("session_id", "TEXT"), ("order_key", "TEXT"),
    ("ts_epoch", "REAL"), ("result_json", "TEXT"),                              # schema 3
)

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
    note             TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    symbol     TEXT PRIMARY KEY,
    qty        TEXT NOT NULL,
    cost_basis TEXT NOT NULL,
    last_price TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    tripped     INTEGER NOT NULL DEFAULT 0,
    tripped_day TEXT,
    tripped_at  TEXT,
    reason      TEXT,
    resumed_at  TEXT
);
CREATE TABLE IF NOT EXISTS risk_days (
    day          TEXT PRIMARY KEY,
    start_equity TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attest_sessions (
    session_id TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
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


def ts_iso(ts: float, ms: bool = True) -> str:
    spec = "milliseconds" if ms else "seconds"
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec=spec).replace("+00:00", "Z")


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def now_iso() -> str:
    return ts_iso(time.time(), ms=False)


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
            d = Decimal(value.strip().replace(",", "").lstrip("$"))
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


def money(d: Decimal) -> str:
    """A USDT amount for a message: 2 decimals. Display only; the ledger keeps full precision."""
    q = Decimal(d).quantize(Decimal("0.01"))
    return f"{abs(q) if q == 0 else q:.2f}"


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


def _quote_time(q) -> float:
    t = getattr(q, "quote_time", None)
    return t if t is not None else q.fetched_at


def _quote_view(q, age) -> dict:
    view = {"bid": fmt(q.bid), "ask": fmt(q.ask), "quote_time": ts_iso(_quote_time(q)),
            "fetched_at": ts_iso(q.fetched_at), "age_seconds": float(round(age, 3)), "source": q.source}
    skipped = getattr(q, "skipped", ())
    if skipped:
        view["fell_back_from"] = [{"source": n, "reason": r} for n, r in skipped]
    return view


class Ledger:
    """Opens a fresh connection per operation, so it is safe across threads and
    a gateway restart simply means the next call reads the same file.

    prices: an object with quote(symbol) -> prices.Quote, raising prices.PriceError.
    policy: a dict, or a callable returning one, with the DEFAULT_POLICY keys
            (read on every call, so config edits apply without a restart).
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
            with self._txn(con):
                self._migrate(con)
                self._ensure_account(con)

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
        return _Closing(con)

    @staticmethod
    def _txn(con):
        return _Txn(con)

    def _now(self) -> float:
        return float(self.clock())

    def _iso(self) -> str:
        return ts_iso(self._now(), ms=False)

    @staticmethod
    def _migrate(con) -> None:
        """Add any missing nullable trade columns, the risk tables' row, bump the version."""
        have = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        for col, typ in _ADDED_TRADE_COLUMNS:
            if col not in have:
                con.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
        con.execute("CREATE INDEX IF NOT EXISTS trades_dedupe ON trades (session_id, order_key, ts_epoch)")
        con.execute("INSERT OR IGNORE INTO risk_state (id, tripped) VALUES (1, 0)")
        con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))

    def _ensure_account(self, con, balance: Decimal | None = None) -> None:
        if con.execute("SELECT 1 FROM account WHERE id = 1").fetchone() is None:
            start = self.default_start if balance is None else balance
            con.execute("INSERT OR IGNORE INTO account (id, cash, starting_balance, currency, created_at) "
                        "VALUES (1, ?, ?, ?, ?)", (str(start), str(start), CURRENCY, self._iso()))

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

    # -- quotes -----------------------------------------------------------

    def _quote(self, symbol: str, max_age: Decimal):
        """A fresh quote and its age in seconds, or Rejected. Never a fallback price."""
        if self.prices is None:
            raise Rejected("feed_unavailable", "no price feed is configured.")
        try:
            q = self.prices.quote(symbol)
        except Exception as exc:
            code = getattr(exc, "code", "feed_unavailable")
            reason = getattr(exc, "reason", None) or f"{exc.__class__.__name__}: {exc}"
            if code not in ("unknown_symbol", "bad_quote", "stale_quote"):
                code = "feed_unavailable"
            raise Rejected(code, reason)
        age = Decimal(str(self._now())) - Decimal(str(_quote_time(q)))
        if age > max_age or age < -FUTURE_TOLERANCE:
            raise Rejected("stale_quote",
                           f"the latest {symbol} quote is {fmt(age.quantize(Decimal('0.001')))} seconds old, "
                           f"outside the {fmt(max_age)} second limit.", quote=_quote_view(q, float(age)))
        return q, age

    def get_quote(self, raw_symbol) -> dict:
        """Live bid/ask for the get_quote tool, or a structured error."""
        try:
            symbol = normalize_symbol(raw_symbol)
            max_age = self.policy()["max_quote_age_seconds"]
            q, age = self._quote(symbol, max_age)
        except Rejected as r:
            out = {"ok": False, "reason_code": r.code, "reason": r.reason + " No price is available; do not "
                   "state one.", "source": "attest price feed", "as_of": self._iso()}
            out.update(r.extra)
            return out
        view = _quote_view(q, float(age))
        out = {"ok": True, "symbol": symbol, "bid": view["bid"], "ask": view["ask"],
               "price_source": q.source, "quote_time": view["quote_time"], "fetched_at": view["fetched_at"],
               "age_seconds": view["age_seconds"], "max_quote_age_seconds": fmt(max_age),
               "source": "attest price feed", "as_of": self._iso(),
               "note": "Live best bid and ask. Report these numbers; never state a price from memory."}
        if "fell_back_from" in view:
            out["fell_back_from"] = view["fell_back_from"]
        return out

    # -- orders -----------------------------------------------------------

    def place_order(self, args, session_id=None) -> dict:
        """Validate, risk-check, price and fill one order. Returns a structured
        dict; a refused order comes back as {"ok": False, "status": "rejected", ...},
        never raises for bad input or a bad feed."""
        try:
            with localcontext() as ctx:
                ctx.prec = PREC
                return self._place(args, str(session_id or ""))
        except Rejected as r:
            out = {"ok": False, "status": "rejected", "reason_code": r.code, "reason": r.reason,
                   "source": SOURCE, "as_of": self._iso()}
            out.update(r.extra)
            return out

    def _parse(self, args) -> dict:
        if not isinstance(args, dict):
            raise Rejected("invalid_args", "order arguments must be an object.")
        side = str(args.get("side") or "").strip().lower()
        if side not in ("buy", "sell"):
            raise Rejected("invalid_side", f"side must be 'buy' or 'sell', got {args.get('side')!r}.")
        symbol = normalize_symbol(args.get("symbol"))
        raw_q, raw_n = args.get("quantity"), args.get("notional")
        has_q, has_n = raw_q not in (None, ""), raw_n not in (None, "")
        qty = notional = None
        if has_q and has_n:
            # Models sometimes send the unused one as 0; ignore a zero partner.
            q_val, n_val = to_decimal(raw_q, "quantity"), to_decimal(raw_n, "notional")
            if q_val == 0 and n_val != 0:
                has_q = False
            elif n_val == 0 and q_val != 0:
                has_n = False
        if has_q == has_n:
            raise Rejected("size_required", "give exactly one of quantity (number of coins) or notional "
                                            f"(amount in {CURRENCY}, e.g. 500 for $500 of BTC).")
        if has_q:
            qty = to_decimal(raw_q, "quantity")
            if qty <= 0:
                raise Rejected("invalid_quantity", f"quantity must be greater than 0, got {fmt(qty)}.")
        else:
            notional = to_decimal(raw_n, "notional")
            if notional <= 0:
                raise Rejected("invalid_notional", f"notional must be greater than 0, got {fmt(notional)}.")
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
        size = f"qty={fmt(qty)}" if qty is not None else f"notional={fmt(notional)}"
        key = f"{symbol}|{side}|{size}|{order_type}|{fmt(limit) if limit is not None else '-'}"
        return {"side": side, "symbol": symbol, "qty": qty, "notional": notional, "limit": limit,
                "order_type": order_type, "note": note, "coid": coid, "key": key}

    # idempotency ---------------------------------------------------------

    def _original(self, con, coid):
        """The original fill for a reused client_order_id, marked duplicate, or None."""
        if coid is None:
            return None
        row = con.execute("SELECT * FROM trades WHERE client_order_id = ?", (coid,)).fetchone()
        if row is None:
            return None
        out = None
        if row["result_json"]:
            try:
                out = json.loads(row["result_json"])
            except ValueError:
                out = None
        if out is None:  # trades from before schema 3 kept no result; rebuild the essentials
            trade = {"id": row["id"], "client_order_id": row["client_order_id"], "symbol": row["symbol"],
                     "side": row["side"], "quantity": fmt(Decimal(row["qty"])),
                     "price": fmt(Decimal(row["price"])), "fee": fmt(Decimal(row["fee"])),
                     "timestamp": row["timestamp"], "note": row["note"]}
            if row["side"] == "sell":
                trade["realized_pnl"] = fmt(Decimal(row["realized_pnl"]))
            out = {"ok": True, "status": "filled", "trade": trade, "currency": CURRENCY, "source": SOURCE}
        out["ok"] = True
        out["duplicate"] = True
        out["duplicate_note"] = (f"client_order_id {coid!r} was already filled as trade {row['id']}. "
                                 "This is that original fill; nothing new was placed.")
        return out

    def _check_recent_identical(self, con, o, session, window: Decimal) -> None:
        if o["coid"] is not None or window <= 0:
            return
        since = self._now() - float(window)
        row = con.execute("SELECT id, ts_epoch FROM trades WHERE session_id = ? AND order_key = ? "
                          "AND ts_epoch >= ? ORDER BY id DESC LIMIT 1", (session, o["key"], since)).fetchone()
        if row is not None:
            ago = max(0.0, self._now() - row["ts_epoch"])
            raise Rejected("possible_duplicate",
                           f"an identical order was filled {ago:.1f} seconds ago in this session (trade "
                           f"{row['id']}), so this looks like an accidental repeat and was not placed. If it "
                           "was a retry, the first fill stands. If you really want a second order, send it "
                           "again with a new client_order_id.", existing_trade_id=row["id"])

    # risk ----------------------------------------------------------------

    def _risk_row(self, con):
        return con.execute("SELECT * FROM risk_state WHERE id = 1").fetchone()

    def _tripped_today(self, con) -> bool:
        row = self._risk_row(con)
        return bool(row and row["tripped"] and row["tripped_day"] == utc_day(self._now()))

    def _evaluate_risk(self, equity: Decimal, marks_live: bool, pol: dict, snapshot_if_missing: bool) -> dict:
        """Roll the UTC day, snapshot today's starting equity if asked, trip the
        kill switch if today's loss reached the limit. Commits, then returns the view."""
        today = utc_day(self._now())
        with self._connect() as con, self._txn(con):
            row = self._risk_row(con)
            if row["tripped"] and row["tripped_day"] != today:
                con.execute("UPDATE risk_state SET tripped = 0, tripped_day = NULL, tripped_at = NULL, "
                            "reason = NULL WHERE id = 1")
            day = con.execute("SELECT * FROM risk_days WHERE day = ?", (today,)).fetchone()
            if day is None and snapshot_if_missing and marks_live:
                con.execute("INSERT OR IGNORE INTO risk_days (day, start_equity, created_at) VALUES (?, ?, ?)",
                            (today, str(equity), self._iso()))
                day = con.execute("SELECT * FROM risk_days WHERE day = ?", (today,)).fetchone()
            if day is not None and marks_live and not self._tripped_today(con):
                start = Decimal(day["start_equity"])
                loss = start - equity
                if loss >= pol["max_daily_loss"]:
                    reason = (f"equity fell from {money(start)} to {money(equity)} {CURRENCY} today, a loss "
                              f"of {money(loss)}, which reached the {money(pol['max_daily_loss'])} {CURRENCY} "
                              "daily loss limit.")
                    con.execute("UPDATE risk_state SET tripped = 1, tripped_day = ?, tripped_at = ?, reason = ? "
                                "WHERE id = 1", (today, self._iso(), reason))
        return self._risk_view(equity, marks_live, pol)

    def _risk_view(self, equity: Decimal, marks_live: bool, pol: dict) -> dict:
        today = utc_day(self._now())
        with self._connect() as con:
            row = self._risk_row(con)
            day = con.execute("SELECT * FROM risk_days WHERE day = ?", (today,)).fetchone()
        tripped = bool(row["tripped"] and row["tripped_day"] == today)
        ks = {"tripped": tripped,
              "resets": "at the next UTC day, or when a human runs /attest resume (not available to the agent)"}
        if tripped:
            ks.update(tripped_at=row["tripped_at"], reason=row["reason"],
                      effect="buys are blocked; sells that reduce a position are still allowed")
        view = {"max_order_notional": fmt(pol["max_order_notional"]), "max_daily_loss": fmt(pol["max_daily_loss"]),
                "kill_switch": ks}
        if day is None:
            view["today"] = {"date": today, "start_equity": None, "pnl": None,
                             "note": "today's starting equity is recorded at the first order of the UTC day"}
        else:
            start = Decimal(day["start_equity"])
            pnl = equity - start
            view["today"] = {"date": today, "start_equity": fmt(start), "pnl": fmt(pnl), "pnl_is_live": marks_live,
                             "loss_limit_remaining": fmt(max(Decimal("0"), pol["max_daily_loss"] + pnl))}
        return view

    def _kill_switch_rejection(self, pol) -> Rejected:
        with self._connect() as con:
            row = self._risk_row(con)
        return Rejected("kill_switch_tripped",
                        f"the daily loss kill switch is tripped: {row['reason'] or ''} New buys are blocked until "
                        "the next UTC day or until a human runs /attest resume. Sells that reduce a position are "
                        "still allowed.", kill_switch={"tripped": True, "tripped_at": row["tripped_at"]})

    def _too_large(self, value: Decimal, pol) -> Rejected:
        return Rejected("order_too_large",
                        f"order size {money(value)} {CURRENCY} is above the per-order "
                        f"limit of {money(pol['max_order_notional'])} {CURRENCY}. Place a smaller order.",
                        order_value=fmt(value), max_order_notional=fmt(pol["max_order_notional"]))

    # fill ----------------------------------------------------------------

    def _place(self, args, session: str) -> dict:
        o = self._parse(args)
        pol = self.policy()
        adds_exposure = o["side"] == "buy"  # no shorting: every valid sell reduces a position

        # 1. Checks that need no network, in order: reused id, stored kill switch,
        #    accidental repeat, size of a USDT-amount order.
        with self._connect() as con:
            original = self._original(con, o["coid"])
            if original is not None:
                return original
            if adds_exposure and self._tripped_today(con):
                raise self._kill_switch_rejection(pol)
            self._check_recent_identical(con, o, session, pol["dedupe_window_seconds"])
        if o["notional"] is not None and o["notional"] > pol["max_order_notional"]:
            raise self._too_large(o["notional"], pol)

        # 2. Price it from a fresh quote.
        try:
            q, age = self._quote(o["symbol"], pol["max_quote_age_seconds"])
        except Rejected as r:
            raise Rejected(r.code, r.reason + NOT_FILLED, **r.extra)
        quote = _quote_view(q, float(age))
        side, limit = o["side"], o["limit"]
        slip, fee_rate = pol["slippage_bps"] / BPS, pol["fee_bps"] / BPS
        ref = q.ask if side == "buy" else q.bid
        if side == "buy":
            if limit is not None and limit < q.ask:
                raise Rejected("not_marketable",
                               f"buy limit {fmt(limit)} is below the current ask {fmt(q.ask)}. attest does "
                               "not hold resting orders, so the order was not placed.", quote=quote)
            price = q.ask * (1 + slip)
            capped = limit is not None and price > limit
            price = min(price, limit) if limit is not None else price
        else:
            if limit is not None and limit > q.bid:
                raise Rejected("not_marketable",
                               f"sell limit {fmt(limit)} is above the current bid {fmt(q.bid)}. attest does "
                               "not hold resting orders, so the order was not placed.", quote=quote)
            price = q.bid * (1 - slip)
            capped = limit is not None and price < limit
            price = max(price, limit) if limit is not None else price
        qty = o["qty"]
        if qty is None:
            qty = (o["notional"] / ref).quantize(QTY_STEP, rounding=ROUND_DOWN)
            if qty <= 0:
                raise Rejected("invalid_notional", f"notional {fmt(o['notional'])} {CURRENCY} is too small to buy "
                                                   f"any {o['symbol']} at {fmt(ref)}.", quote=quote)
        order_value = qty * ref
        if order_value > pol["max_order_notional"]:
            raise self._too_large(order_value, pol)

        # 3. Daily loss: snapshot today's equity, trip the switch if the loss limit is reached.
        snap = self._snapshot()
        equity = snap["cash"] + snap["value"]
        if adds_exposure and not snap["marks_live"]:
            raise Rejected("risk_unverifiable",
                           f"the daily loss limit can't be checked because {', '.join(snap['stale'])} has no "
                           "live price, so buys are paused until live prices return.")
        risk = self._evaluate_risk(equity, snap["marks_live"], pol, snapshot_if_missing=True)
        if adds_exposure and risk["kill_switch"]["tripped"]:
            raise self._kill_switch_rejection(pol)

        # 4. The fill, with every check repeated inside one write transaction.
        with self._connect() as con, self._txn(con):
            original = self._original(con, o["coid"])
            if original is not None:
                return original
            if adds_exposure and self._tripped_today(con):
                raise self._kill_switch_rejection(pol)
            self._check_recent_identical(con, o, session, pol["dedupe_window_seconds"])
            return self._fill(con, o, q, quote, price, qty, capped, pol, session)

    def _fill(self, con, o, q, quote, price, qty, capped, pol, session) -> dict:
        sym, side, limit = o["symbol"], o["side"], o["limit"]
        notional = qty * price
        fee = notional * pol["fee_bps"] / BPS
        acct = self._account(con)
        cash = Decimal(acct["cash"])
        pos = con.execute("SELECT * FROM positions WHERE symbol = ?", (sym,)).fetchone()
        held = Decimal(pos["qty"]) if pos else Decimal("0")
        basis = Decimal(pos["cost_basis"]) if pos else Decimal("0")
        now = self._now()
        ts = ts_iso(now, ms=False)

        if side == "buy":
            cost = notional + fee
            if cost > cash:
                raise Rejected("insufficient_cash",
                               f"buy needs {money(cost)} {CURRENCY} including fee but only {money(cash)} "
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
            "quote_bid, quote_ask, quote_fetched_at, price_source, slippage_bps, quote_time, session_id, "
            "order_key, ts_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o["coid"], sym, side, str(qty), str(price), str(fee), str(realized), ts, o["note"],
             str(q.bid), str(q.ask), quote["fetched_at"], q.source, str(pol["slippage_bps"]),
             quote["quote_time"], session, o["key"], now))
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
                 "slippage_bps": fmt(pol["slippage_bps"]), "price_source": q.source, "quote": quote,
                 "timestamp": ts, "note": o["note"]}
        if o["notional"] is not None:
            trade["requested_notional"] = fmt(o["notional"])
        if limit is not None:
            trade["limit_price"] = fmt(limit)
            trade["limit_capped"] = capped
        if side == "sell":
            trade["realized_pnl"] = fmt(realized)
        result = {
            "ok": True, "status": "filled", "trade": trade,
            "cash_after": fmt(cash), "currency": CURRENCY,
            "position_after": {"symbol": sym, "quantity": fmt(held),
                               "avg_cost": fmt(basis / held) if held else "0"},
            "fill_policy": self.fill_policy(pol), "source": SOURCE, "as_of": ts,
        }
        con.execute("UPDATE trades SET result_json = ? WHERE id = ?", (json.dumps(result), cur.lastrowid))
        return result

    @staticmethod
    def fill_policy(pol) -> str:
        return (f"Filled from the live quote: buys at ask, sells at bid, plus {fmt(pol['slippage_bps'])} bps "
                f"slippage against the trader, and a {fmt(pol['fee_bps'])} bps fee. Limit orders fill only when "
                "marketable now, never worse than the limit; attest holds no resting orders.")

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
                    "quote_time": ts_iso(_quote_time(q)), "quote_source": q.source}
        except Rejected as r:
            return {"price": last_fill, "live": False, "source": "last fill (STALE)", "stale_reason": r.reason}

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
                p["mark_quote_time"] = m["quote_time"]
                p["mark_price_source"] = m["quote_source"]
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
            note = "marks: live bid"
        return {"acct": acct, "cash": cash, "start": start, "positions": positions, "value": value,
                "basis": basis_total, "realized": realized, "fees": fees, "marks_live": not stale,
                "stale": stale, "mark_note": note,
                "trades": t["n"] or 0, "buys": t["buys"] or 0, "sells": t["sells"] or 0}

    def portfolio(self) -> dict:
        with localcontext() as ctx:
            ctx.prec = PREC
            s = self._snapshot()
            equity = s["cash"] + s["value"]
            risk = self._evaluate_risk(equity, s["marks_live"], self.policy(), snapshot_if_missing=False)
            return {
                "ok": True, "source": SOURCE, "as_of": self._iso(), "currency": CURRENCY,
                "cash": fmt(s["cash"]), "positions": s["positions"],
                "positions_value": fmt(s["value"]), "total_equity": fmt(equity),
                "starting_balance": fmt(s["start"]), "account_created": s["acct"]["created_at"],
                "marks_live": s["marks_live"], "mark_note": s["mark_note"], "risk": risk,
            }

    def pnl(self) -> dict:
        with localcontext() as ctx:
            ctx.prec = PREC
            s = self._snapshot()
            equity = s["cash"] + s["value"]
            unrealized = s["value"] - s["basis"]
            total = s["realized"] + unrealized
            ret = (total / s["start"] * 100).quantize(Decimal("0.0001")) if s["start"] else Decimal("0")
            risk = self._evaluate_risk(equity, s["marks_live"], self.policy(), snapshot_if_missing=False)
            return {
                "ok": True, "source": SOURCE, "as_of": self._iso(), "currency": CURRENCY,
                "realized_pnl": fmt(s["realized"]), "unrealized_pnl": fmt(unrealized),
                "total_pnl": fmt(total), "return_pct": fmt(ret),
                "starting_balance": fmt(s["start"]), "total_equity": fmt(equity),
                "fees_paid": fmt(s["fees"]),
                "trade_count": s["trades"], "buys": s["buys"], "sells": s["sells"],
                "reconciles": total == equity - s["start"],
                "marks_live": s["marks_live"], "mark_note": s["mark_note"], "risk": risk,
            }

    # -- sessions that used attest (for the claim check; survives restarts) --

    def note_session(self, session_id) -> None:
        if not session_id:
            return
        now = self._now()
        with self._connect() as con:
            con.execute("INSERT INTO attest_sessions (session_id, first_seen, last_seen) VALUES (?, ?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET last_seen = excluded.last_seen",
                        (str(session_id), now, now))
            con.execute("DELETE FROM attest_sessions WHERE last_seen < ?", (now - 90 * 86400,))

    def session_used(self, session_id) -> bool:
        if not session_id:
            return False
        with self._connect() as con:
            return con.execute("SELECT 1 FROM attest_sessions WHERE session_id = ?",
                               (str(session_id),)).fetchone() is not None

    def figures(self) -> dict:
        """The headline figures as Decimals, read-only (no risk writes), for the claim check."""
        with localcontext() as ctx:
            ctx.prec = PREC
            s = self._snapshot()
            equity = s["cash"] + s["value"]
            unrealized = s["value"] - s["basis"]
            total = s["realized"] + unrealized
            ret = total / s["start"] * 100 if s["start"] else Decimal("0")
            return {"cash": s["cash"], "equity": equity, "realized": s["realized"], "unrealized": unrealized,
                    "total": total, "return_pct": ret, "marks_live": s["marks_live"]}

    # -- admin (human only: reached from /attest, never from a tool) ------

    def resume(self) -> dict:
        """Clear a tripped kill switch and restart today's loss count from now."""
        today = utc_day(self._now())
        with self._connect() as con, self._txn(con):
            was = self._tripped_today(con)
            con.execute("UPDATE risk_state SET tripped = 0, tripped_day = NULL, tripped_at = NULL, reason = NULL, "
                        "resumed_at = ? WHERE id = 1", (self._iso(),))
            con.execute("DELETE FROM risk_days WHERE day = ?", (today,))
        return {"ok": True, "was_tripped": was, "day": today}

    def reset(self, balance=None) -> dict:
        """Wipe trades, positions and risk state and start a fresh account."""
        start = self.default_start if balance is None else to_decimal(balance, "amount")
        if start <= 0:
            raise Rejected("invalid_amount", "starting balance must be greater than 0.")
        with self._connect() as con, self._txn(con):
            con.execute("DELETE FROM trades")
            con.execute("DELETE FROM positions")
            con.execute("DELETE FROM account")
            con.execute("DELETE FROM risk_days")
            con.execute("UPDATE risk_state SET tripped = 0, tripped_day = NULL, tripped_at = NULL, reason = NULL "
                        "WHERE id = 1")
            con.execute("DELETE FROM sqlite_sequence WHERE name = 'trades'")
            self._ensure_account(con, start)
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


class _Txn:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception (a Rejected included)."""

    def __init__(self, con):
        self._con = con

    def __enter__(self):
        self._con.execute("BEGIN IMMEDIATE")
        return self._con

    def __exit__(self, exc_type, *exc):
        self._con.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
