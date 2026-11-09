"""Arithmetic, broker and rejection checks for the attest ledger.

Runs against databases in a fresh temporary folder, never the real one, and
uses fake price sources (the real exchange source classes with their HTTP
request replaced): this script makes no network calls.
Prints PASS for a case only when it ran and every assertion held.

    python3 scripts/ledger_check.py
"""
import sqlite3
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from ledger import Ledger, Rejected, normalize_symbol  # noqa: E402
from prices import (BINANCE_SOURCE, KRAKEN_SOURCE, OKX_SOURCE, BinanceSource, FallbackSource,  # noqa: E402
                    KrakenSource, OkxSource, PriceError, Quote, kraken_pair, okx_inst_id, parse_binance,
                    parse_book_ticker, parse_kraken, parse_okx)

D = Decimal
RESULTS = []
T0 = 1_800_000_000.0  # fixed fake "now"
# Phase 1 cases test cash and position rules, so the Phase 3 risk limits are set out of the way.
NO_COSTS = {"fee_bps": 0, "slippage_bps": 0, "max_quote_age_seconds": 10,
            "max_order_notional": 10**9, "max_daily_loss": 10**9, "dedupe_window_seconds": 10}
LIVE_COSTS = {"fee_bps": 10, "slippage_bps": 5, "max_quote_age_seconds": 10,
              "max_order_notional": 1000, "max_daily_loss": 500, "dedupe_window_seconds": 10}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSource:
    """quotes: symbol -> (bid, ask, age_seconds). down=True makes every call fail."""

    def __init__(self, clock):
        self.clock, self.quotes, self.down, self.calls = clock, {}, False, 0

    def set(self, symbol, bid, ask, age=0.5):
        self.quotes[symbol] = (D(str(bid)), D(str(ask)), age)

    def quote(self, symbol):
        self.calls += 1
        if self.down:
            raise PriceError("feed_unavailable", "could not reach the Binance price feed (fake outage).")
        if symbol not in self.quotes:
            raise PriceError("unknown_symbol", f"Binance does not list {symbol}.")
        bid, ask, age = self.quotes[symbol]
        return Quote(symbol, bid, ask, self.clock() - age, "fake feed")


def case(name):
    def wrap(fn):
        def run(*a):
            try:
                fn(*a)
            except AssertionError as exc:
                RESULTS.append((name, False))
                print(f"FAIL  {name}: {exc}")
                return
            except Exception:
                RESULTS.append((name, False))
                print(f"FAIL  {name}: crashed")
                traceback.print_exc()
                return
            RESULTS.append((name, True))
            print(f"PASS  {name}")
        return run
    return wrap


def eq(actual, expected, what):
    assert D(str(actual)) == D(str(expected)), f"{what}: expected {expected}, got {actual}"


def stable(snapshot):
    return {k: v for k, v in snapshot.items() if k != "as_of"}


def state(led):
    return stable(led.portfolio()), stable(led.pnl())


def expect_reject(led, args, code, what):
    before = state(led)
    out = led.place_order(args)
    assert out["ok"] is False and out["status"] == "rejected", f"{what} was not rejected: {out}"
    assert out["reason_code"] == code, f"{what}: expected {code}, got {out['reason_code']} ({out['reason']})"
    assert out["reason"], f"{what}: empty reason"
    assert state(led) == before, f"{what}: ledger changed on a rejection"
    return out


def trade_row(path, trade_id):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())
    finally:
        con.close()


# ---------------------------------------------------------------- Phase 1 cases
# Same orders and expected numbers as Phase 1. Fee and slippage are 0 here and
# the fake quotes make each limit marketable, so fills land on the same prices.

@case("arithmetic: buy 0.01 @ 60000, sell 0.005 @ 62000")
def arithmetic(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=NO_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60000)
    # Same shape the probe saw: quantity as float, limit_price as int.
    buy = led.place_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01,
                           "order_type": "limit", "limit_price": 60000})
    assert buy["ok"], f"buy rejected: {buy}"
    eq(buy["cash_after"], "9400", "cash after buy")
    feed.set("BTCUSDT", 62000, 62010)
    sell = led.place_order({"symbol": "BTCUSDT", "side": "sell", "quantity": 0.005,
                            "order_type": "limit", "limit_price": 62000})
    assert sell["ok"], f"sell rejected: {sell}"
    eq(sell["trade"]["realized_pnl"], "10", "realized on the sell")
    p, q = led.portfolio(), led.pnl()
    eq(p["cash"], "9710", "cash")
    assert len(p["positions"]) == 1, f"expected 1 position, got {len(p['positions'])}"
    pos = p["positions"][0]
    assert pos["symbol"] == "BTCUSDT", pos["symbol"]
    eq(pos["quantity"], "0.005", "position quantity")
    eq(pos["avg_cost"], "60000", "average cost")
    eq(pos["mark_price"], "62000", "mark price (live bid)")
    eq(pos["market_value"], "310", "market value")
    eq(q["realized_pnl"], "10", "realized P&L")
    eq(q["unrealized_pnl"], "10", "unrealized P&L")
    eq(q["total_pnl"], "20", "total P&L")
    eq(p["total_equity"], "10020", "equity")
    eq(q["return_pct"], "0.2", "return %")
    assert q["trade_count"] == 2, f"trade count {q['trade_count']}"
    assert q["reconciles"] is True, "realized + unrealized != equity - start"
    assert p["source"] == "attest ledger" and p["as_of"], "missing source or as_of"


@case("restart: close and reopen the database, numbers identical")
def restart(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=NO_COSTS, clock=clock)
    before = state(led)
    del led
    # A different default balance must not touch the existing account.
    reopened = Ledger(path, D("99999"), prices=feed, policy=NO_COSTS, clock=clock)
    after = state(reopened)
    assert before == after, f"changed after reopen:\n{before}\n{after}"
    eq(after[0]["cash"], "9710", "cash after reopen")


@case("reject: oversell")
def oversell(path, feed, clock):
    feed.set("BTCUSDT", 62000, 62000)
    expect_reject(Ledger(path, prices=feed, policy=NO_COSTS, clock=clock),
                  {"symbol": "BTCUSDT", "side": "sell", "quantity": 0.006, "limit_price": 62000},
                  "insufficient_position", "oversell")


@case("reject: overspend")
def overspend(path, feed, clock):
    feed.set("BTCUSDT", 60000, 60000)
    expect_reject(Ledger(path, prices=feed, policy=NO_COSTS, clock=clock),
                  {"symbol": "BTCUSDT", "side": "buy", "quantity": 1, "limit_price": 60000},
                  "insufficient_cash", "overspend")


@case("reject: quantity 0")
def qty_zero(path, feed, clock):
    expect_reject(Ledger(path, prices=feed, policy=NO_COSTS, clock=clock),
                  {"symbol": "BTCUSDT", "side": "buy", "quantity": 0, "limit_price": 60000},
                  "invalid_quantity", "quantity 0")


@case("reject: unknown side, missing price; reused client_order_id returns the original fill")
def other_rejects(path, feed, clock):
    led = Ledger(path, prices=feed, policy=NO_COSTS, clock=clock)
    expect_reject(led, {"symbol": "BTCUSDT", "side": "hold", "quantity": 1, "limit_price": 1},
                  "invalid_side", "unknown side")
    expect_reject(led, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001, "order_type": "limit"},
                  "missing_limit_price", "missing price")
    feed.set("ETHUSDT", 3000, 3000)
    first = led.place_order({"symbol": "ETHUSDT", "side": "buy", "quantity": "0.1",
                             "limit_price": "3000", "client_order_id": "abc-1"})
    assert first["ok"], f"first order rejected: {first}"
    dup_args = {"symbol": "ETHUSDT", "side": "buy", "quantity": "0.1",
                "limit_price": "3000", "client_order_id": "abc-1"}
    before = state(led)
    calls = feed.calls
    dup = led.place_order(dup_args)
    assert feed.calls == calls, "a duplicate order fetched a quote"
    assert dup["ok"] is True and dup.get("duplicate") is True, f"duplicate not returned as original: {dup}"
    assert dup["trade"]["id"] == first["trade"]["id"], "duplicate did not return the first trade"
    assert state(led) == before, "ledger changed on a duplicate"


# ---------------------------------------------------------------- Phase 2 cases

@case("market buy and sell: ask/bid, 5 bps slippage, 10 bps fee, quote stored")
def market_math(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    buy = led.place_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01, "order_type": "market"})
    assert buy["ok"], f"buy rejected: {buy}"
    t = buy["trade"]
    eq(t["price"], "60040.005", "buy fill = ask 60010 * 1.0005")
    eq(t["notional"], "600.40005", "buy notional")
    eq(t["fee"], "0.60040005", "buy fee 0.1%")
    eq(buy["cash_after"], "9398.99954995", "cash after buy")
    eq(buy["position_after"]["avg_cost"], "60100.045005", "avg cost includes the buy fee")
    row = trade_row(path, t["id"])
    eq(row["quote_bid"], "59990", "stored quote bid")
    eq(row["quote_ask"], "60010", "stored quote ask")
    assert row["quote_fetched_at"] and row["quote_fetched_at"].endswith("Z"), row["quote_fetched_at"]
    assert row["price_source"] == "fake feed", row["price_source"]
    eq(row["slippage_bps"], "5", "stored slippage")
    eq(row["fee"], "0.60040005", "stored fee")

    feed.set("BTCUSDT", 61990, 62010)
    sell = led.place_order({"symbol": "BTCUSDT", "side": "sell", "quantity": 0.005})  # no type: market
    assert sell["ok"], f"sell rejected: {sell}"
    t = sell["trade"]
    assert t["order_type"] == "market", t["order_type"]
    eq(t["price"], "61959.005", "sell fill = bid 61990 * 0.9995")
    eq(t["fee"], "0.309795025", "sell fee 0.1%")
    eq(t["realized_pnl"], "8.98500495", "realized = proceeds - half the cost basis")
    p, q = led.portfolio(), led.pnl()
    eq(p["cash"], "9708.484779925", "cash after sell")
    eq(p["positions"][0]["avg_cost"], "60100.045005", "avg cost unchanged by a sell")
    eq(p["positions"][0]["mark_price"], "61990", "marked at live bid")
    assert p["positions"][0]["mark_is_live"] is True and p["marks_live"] is True, "mark should be live"
    eq(q["unrealized_pnl"], "9.449774975", "unrealized")
    eq(q["total_pnl"], "18.434779925", "total P&L")
    eq(p["total_equity"], "10018.434779925", "equity")
    eq(q["fees_paid"], "0.910195075", "fees paid")
    assert q["reconciles"] is True, "realized + unrealized != equity - start"


@case("limit: marketable fills at market plus slippage capped at the limit, non-marketable rejected")
def limits(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    out = expect_reject(led, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01, "limit_price": 60000},
                        "not_marketable", "buy limit below ask")
    assert "resting orders" in out["reason"], out["reason"]
    buy = led.place_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01, "limit_price": 60010})
    assert buy["ok"], f"marketable buy rejected: {buy}"
    eq(buy["trade"]["price"], "60010", "buy limit at the ask: ask + slippage capped at the limit")
    out = expect_reject(led, {"symbol": "BTCUSDT", "side": "sell", "quantity": 0.01, "limit_price": 60000},
                        "not_marketable", "sell limit above bid")
    assert "resting orders" in out["reason"], out["reason"]
    sell = led.place_order({"symbol": "BTCUSDT", "side": "sell", "quantity": 0.01, "limit_price": 59000})
    assert sell["ok"], f"marketable sell rejected: {sell}"
    eq(sell["trade"]["price"], "59960.005", "marketable sell fills at bid - slippage, not at the limit")


@case("stale quote rejected, including one served from the feed cache")
def stale(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010, age=11)
    out = expect_reject(led, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001},
                        "stale_quote", "11 second old quote")
    assert out["quote"]["age_seconds"] == 11.0, out["quote"]
    # The real source's cache: a quote is reused for 2 seconds, keeps its
    # fetched_at, and the ledger still refuses it once it is too old.
    src_clock = Clock()
    src = BinanceSource(clock=src_clock)
    fetches = []

    def fake_fetch(symbol):
        fetches.append(symbol)
        return Quote(symbol, D("59990"), D("60010"), src_clock() - 0.2, "fake binance")
    src._fetch = fake_fetch
    src.quote("BTCUSDT")
    src_clock.t += 1
    src.quote("BTCUSDT")
    assert len(fetches) == 1, f"expected the cache to serve the second call, fetched {len(fetches)}"
    src_clock.t += 3
    src.quote("BTCUSDT")
    assert len(fetches) == 2, "expected a refetch after the cache window"
    old = Ledger(path, prices=src, policy={"fee_bps": 10, "slippage_bps": 5, "max_quote_age_seconds": 1},
                 clock=lambda: src_clock() + 5)
    expect_reject(old, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001}, "stale_quote",
                  "cached quote older than the limit")


@case("feed failure and unknown symbol rejected, never filled")
def feed_failures(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    feed.down = True
    try:
        out = expect_reject(led, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001},
                            "feed_unavailable", "feed down")
        assert "guessed price" in out["reason"], out["reason"]
    finally:
        feed.down = False
    expect_reject(led, {"symbol": "NOPECOIN", "side": "buy", "quantity": 1}, "unknown_symbol",
                  "unknown symbol")
    none = Ledger(path, prices=None, policy=LIVE_COSTS, clock=clock)
    expect_reject(none, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001}, "feed_unavailable",
                  "no price source at all")
    # Malformed feed answers are refused too.
    for payload, what in (({"symbol": "BTCUSDT", "bidPrice": "0", "askPrice": "1"}, "zero bid"),
                          ({"symbol": "BTCUSDT", "bidPrice": "2", "askPrice": "1"}, "crossed"),
                          ({"symbol": "ETHUSDT", "bidPrice": "1", "askPrice": "2"}, "wrong symbol")):
        try:
            parse_book_ticker("BTCUSDT", payload, T0)
        except PriceError as exc:
            assert exc.code == "bad_quote", f"{what}: {exc.code}"
        else:
            raise AssertionError(f"{what} quote was accepted")
    good = parse_book_ticker("BTCUSDT", {"symbol": "BTCUSDT", "bidPrice": "60000.01000000",
                                         "bidQty": "1", "askPrice": "60000.02000000", "askQty": "1"}, T0)
    eq(good.bid, "60000.01", "parsed bid")
    eq(good.ask, "60000.02", "parsed ask")


@case("feed hang: hard deadline, then fail fast without reusing any price")
def feed_hang(path, feed, clock):
    import threading
    import time as _time
    release = threading.Event()
    src_clock = Clock()
    src = BinanceSource(timeout=0.3, clock=src_clock)
    requests = []

    def hanging(symbol):  # stands in for a DNS lookup that never answers
        requests.append(symbol)
        release.wait(5)
        raise PriceError("feed_unavailable", "never answered")
    src._request = hanging
    try:
        start = _time.monotonic()
        try:
            src.quote("BTCUSDT")
        except PriceError as exc:
            assert exc.code == "feed_unavailable", exc.code
        else:
            raise AssertionError("a hanging feed returned a quote")
        took = _time.monotonic() - start
        assert took < 1.0, f"deadline not enforced, waited {took:.2f}s"
        start = _time.monotonic()
        try:
            src.quote("ETHUSDT")
        except PriceError as exc:
            assert exc.code == "feed_unavailable", exc.code
        else:
            raise AssertionError("fail-fast window returned a quote")
        assert _time.monotonic() - start < 0.1, "second call did not fail fast"
        assert requests == ["BTCUSDT"], f"fail-fast still sent requests: {requests}"
        src_clock.t += 6  # after the window, it tries the network again
        src._request = lambda s: Quote(s, D("1"), D("2"), src_clock(), "fake binance")
        eq(src.quote("BTCUSDT").bid, "1", "fresh quote after recovery")
    finally:
        release.set()


@case("symbols: BTC -> BTCUSDT, eth -> ETHUSDT, non-USDT pair refused")
def symbols(path, feed, clock):
    for raw, want in (("BTC", "BTCUSDT"), ("eth", "ETHUSDT"), ("btc/usdt", "BTCUSDT"),
                      ("BTC-USDT", "BTCUSDT"), ("BTCUSD", "BTCUSDT"), ("SOLUSDT", "SOLUSDT")):
        got = normalize_symbol(raw)
        assert got == want, f"{raw!r} -> {got}, expected {want}"
    try:
        normalize_symbol("ETHBTC")
    except Rejected as r:
        assert r.code == "unsupported_pair", r.code
    else:
        raise AssertionError("ETHBTC was accepted against USDT cash")
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    out = led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.001})
    assert out["ok"] and out["trade"]["symbol"] == "BTCUSDT", out


@case("marks: live bid, then STALE last-fill label when the feed is down or old")
def marks(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    buy = led.place_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01})
    assert buy["ok"], buy
    fill = buy["trade"]["price"]
    feed.set("BTCUSDT", 61000, 61020)
    p = led.portfolio()
    assert p["marks_live"] is True and p["positions"][0]["mark_is_live"] is True, p
    eq(p["positions"][0]["mark_price"], "61000", "live mark = bid")
    for label, setup in (("feed down", lambda: setattr(feed, "down", True)),
                         ("old quote", lambda: feed.set("BTCUSDT", 61000, 61020, age=30))):
        setup()
        try:
            p, q = led.portfolio(), led.pnl()
        finally:
            feed.down = False
        pos = p["positions"][0]
        assert p["marks_live"] is False and q["marks_live"] is False, f"{label}: marks_live not False"
        assert pos["mark_is_live"] is False, f"{label}: position mark claims to be live"
        assert "STALE" in pos["mark_source"], f"{label}: mark_source {pos['mark_source']}"
        assert pos.get("stale_reason"), f"{label}: no stale_reason"
        assert "STALE" in p["mark_note"] and "STALE" in q["mark_note"], f"{label}: note lacks STALE"
        eq(pos["mark_price"], fill, f"{label}: falls back to the last fill price")
        assert q["reconciles"] is True, f"{label}: does not reconcile"
    feed.set("BTCUSDT", 61000, 61020)
    assert led.portfolio()["marks_live"] is True, "mark did not return to live"


@case("migration: a Phase 1 database opens, gains the schema 2 and 3 columns, keeps its data")
def migration(path, feed, clock):
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE account (id INTEGER PRIMARY KEY CHECK (id = 1), cash TEXT NOT NULL,
            starting_balance TEXT NOT NULL, currency TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT UNIQUE,
            symbol TEXT NOT NULL, side TEXT NOT NULL CHECK (side IN ('buy', 'sell')), qty TEXT NOT NULL,
            price TEXT NOT NULL, fee TEXT NOT NULL, realized_pnl TEXT NOT NULL, timestamp TEXT NOT NULL,
            note TEXT);
        CREATE TABLE positions (symbol TEXT PRIMARY KEY, qty TEXT NOT NULL, cost_basis TEXT NOT NULL,
            last_price TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        INSERT INTO account VALUES (1, '9400', '10000', 'USDT', '2026-11-09T12:00:00Z');
        INSERT INTO trades (symbol, side, qty, price, fee, realized_pnl, timestamp)
            VALUES ('BTCUSDT', 'buy', '0.01', '60000', '0', '0', '2026-11-09T12:00:01Z');
        INSERT INTO positions VALUES ('BTCUSDT', '0.01', '600', '60000', '2026-11-09T12:00:01Z');
    """)
    con.commit()
    con.close()
    feed.set("BTCUSDT", 60000, 60010)
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    con = sqlite3.connect(str(path))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        version = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    finally:
        con.close()
    for col in ("quote_bid", "quote_ask", "quote_fetched_at", "price_source", "slippage_bps",
                "quote_time", "session_id", "order_key", "ts_epoch", "result_json"):
        assert col in cols, f"missing column {col}"
    assert version == "3", f"schema_version {version}"
    p, q = led.portfolio(), led.pnl()
    eq(p["cash"], "9400", "cash kept")
    eq(p["positions"][0]["quantity"], "0.01", "position kept")
    assert q["trade_count"] == 1, "old trade lost"
    old = trade_row(path, 1)
    assert old["quote_bid"] is None, "old trade got an invented quote"


# ---------------------------------------------------------------- Phase 3 cases

class Market:
    """A fake market behind the REAL OkxSource / KrakenSource / BinanceSource:
    only their HTTP request is replaced, so parsing, the per-source deadline,
    fail-fast memory and FallbackSource all run as in production."""

    def __init__(self, clock):
        self.clock = clock
        self.prices = {}
        self.mode = {"okx": "ok", "kraken": "ok", "binance": "ok"}
        self.okx_ts_age = 0.0
        self.requests = []

    def set(self, symbol, bid, ask):
        self.prices[symbol] = (D(str(bid)), D(str(ask)))

    def payload(self, name, symbol):
        mode = self.mode[name]
        if symbol not in self.prices or mode == "unknown":
            return {"okx": (200, {"code": "51001", "data": [], "msg": "Instrument ID doesn't exist."}),
                    "kraken": (200, {"error": ["EQuery:Unknown asset pair"]}),
                    "binance": (400, {"code": -1121, "msg": "Invalid symbol."})}[name]
        bid, ask = self.prices[symbol]
        if mode == "crossed":
            bid, ask = ask + 5, bid
        if name == "okx":
            ts_ms = int((self.clock() - self.okx_ts_age) * 1000)
            return 200, {"code": "0", "msg": "", "data": [{"instId": okx_inst_id(symbol), "bidPx": str(bid),
                                                           "askPx": str(ask), "ts": str(ts_ms)}]}
        if name == "kraken":
            return 200, {"error": [], "result": {kraken_pair(symbol): {"a": [str(ask), "1", "1.000"],
                                                                       "b": [str(bid), "1", "1.000"]}}}
        return 200, {"symbol": symbol, "bidPrice": str(bid), "askPrice": str(ask)}

    def stack(self, max_age=10):
        sources = []
        for cls in (OkxSource, KrakenSource, BinanceSource):
            src = cls(clock=self.clock, cache_seconds=-1)  # no cache: each scenario sees the market as set

            def request(symbol, src=src):
                self.requests.append(src.name)
                if self.mode[src.name] == "down":
                    raise PriceError("feed_unavailable", f"could not reach {src.name} (fake outage).")
                status, payload = self.payload(src.name, symbol)
                return src.parse(symbol, status, payload, self.clock())
            src._request = request
            sources.append(src)
        return FallbackSource(sources, max_age=max_age, clock=self.clock)


def load_plugin_package():
    """The real plugin package, loaded under a private name, for the /attest command
    and the tool registration checks."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("attest_under_test", PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["attest_under_test"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


@case("sources: OKX when healthy, Kraken when OKX fails, Binance when both fail, all down rejected")
def fallback_chain(path, feed, clock):
    market = Market(clock)
    market.set("BTCUSDT", 59990, 60010)
    led = Ledger(path, D("10000"), prices=market.stack(), policy=LIVE_COSTS, clock=clock)
    order = lambda coid: led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.001,
                                         "client_order_id": coid})
    r = order("f1")
    assert r["ok"] and r["trade"]["price_source"] == OKX_SOURCE, f"healthy: {r.get('trade', r)}"
    assert trade_row(path, r["trade"]["id"])["price_source"] == OKX_SOURCE, "price_source not stored"
    market.mode["okx"] = "down"
    led.prices = market.stack()
    r = order("f2")
    assert r["ok"] and r["trade"]["price_source"] == KRAKEN_SOURCE, f"okx down: {r.get('trade', r)}"
    assert r["trade"]["quote"]["fell_back_from"][0]["source"] == "okx", "fallback not recorded"
    market.mode.update(okx="crossed", kraken="down")
    led.prices = market.stack()
    r = order("f3")
    assert r["ok"] and r["trade"]["price_source"] == BINANCE_SOURCE, f"okx crossed, kraken down: {r.get('trade', r)}"
    assert [f["source"] for f in r["trade"]["quote"]["fell_back_from"]] == ["okx", "kraken"], "fallback trail"
    market.mode.update(okx="down", kraken="down", binance="down")
    led.prices = market.stack()
    out = expect_reject(led, {"symbol": "BTC", "side": "buy", "quantity": 0.001, "client_order_id": "f4"},
                        "feed_unavailable", "all three sources down")
    for name in ("okx", "kraken", "binance"):
        assert name in out["reason"], f"reason does not name {name}: {out['reason']}"
    market.mode.update(okx="unknown", kraken="unknown", binance="unknown")
    led.prices = market.stack()
    expect_reject(led, {"symbol": "NOPE", "side": "buy", "quantity": 1, "client_order_id": "f5"},
                  "unknown_symbol", "symbol no source lists")


@case("sources: OKX ts drives staleness (old ts skipped, fresh ts used with its own age)")
def okx_ts(path, feed, clock):
    market = Market(clock)
    market.set("BTCUSDT", 59990, 60010)
    led = Ledger(path, D("10000"), prices=market.stack(), policy=LIVE_COSTS, clock=clock)
    market.okx_ts_age = 5.0  # fetched just now, but the exchange says the quote is 5 s old
    r = led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.001, "client_order_id": "t1"})
    assert r["ok"] and r["trade"]["price_source"] == OKX_SOURCE, r
    assert r["trade"]["quote"]["age_seconds"] == 5.0, f"age not from ts: {r['trade']['quote']}"
    assert trade_row(path, r["trade"]["id"])["quote_time"] != trade_row(path, r["trade"]["id"])["quote_fetched_at"], \
        "quote_time should be the exchange ts, not fetched_at"
    market.okx_ts_age = 30.0
    r = led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.001, "client_order_id": "t2"})
    assert r["ok"] and r["trade"]["price_source"] == KRAKEN_SOURCE, f"stale OKX ts not skipped: {r}"
    market.mode.update(kraken="down", binance="down")
    led.prices = market.stack()
    out = expect_reject(led, {"symbol": "BTC", "side": "buy", "quantity": 0.001, "client_order_id": "t3"},
                        "stale_quote", "only a stale OKX quote")
    assert "okx quote is 30" in out["reason"], out["reason"]


@case("parsers: real OKX, Kraken and Binance payload shapes; wrong pair or crossed refused")
def parsers(path, feed, clock):
    assert okx_inst_id("BTCUSDT") == "BTC-USDT" and kraken_pair("BTCUSDT") == "XBTUSDT", "pair mapping"
    assert kraken_pair("ETHUSDT") == "ETHUSDT", "ETH mapping"
    okx = parse_okx("BTCUSDT", 200, {"code": "0", "msg": "", "data": [{"instType": "SPOT", "instId": "BTC-USDT",
                    "last": "74679", "askPx": "74680", "bidPx": "74679.9", "ts": "1794230466072"}]}, T0)
    eq(okx.bid, "74679.9", "okx bid")
    eq(okx.ask, "74680", "okx ask")
    assert okx.quote_time == 1794230466.072 and okx.fetched_at == T0, "okx times"
    kr = parse_kraken("BTCUSDT", 200, {"error": [], "result": {"XBTUSDT": {"a": ["74686.40000", "1", "1.000"],
                      "b": ["74674.50000", "1", "1.000"], "c": ["74638.8", "0.02"]}}}, T0)
    eq(kr.bid, "74674.5", "kraken bid")
    eq(kr.ask, "74686.4", "kraken ask")
    assert kr.quote_time is None and kr.time == T0, "kraken quote time must be fetched_at"
    bad = [
        (lambda: parse_kraken("BTCUSDT", 200, {"error": [], "result": {"XETHZUSD": {"a": ["1"], "b": ["1"]}}}, T0),
         "bad_quote", "kraken answered for another pair"),
        (lambda: parse_kraken("BTCUSDT", 200, {"error": [], "result": {"XBTUSDT": {"a": ["2"], "b": ["1"]},
                                                                       "ETHUSDT": {"a": ["2"], "b": ["1"]}}}, T0),
         "bad_quote", "kraken answered with two pairs"),
        (lambda: parse_kraken("BTCUSDT", 200, {"error": ["EQuery:Unknown asset pair"]}, T0),
         "unknown_symbol", "kraken unknown pair"),
        (lambda: parse_okx("BTCUSDT", 200, {"code": "0", "data": [{"instId": "ETH-USDT", "bidPx": "1",
                                                                   "askPx": "2", "ts": "1"}]}, T0),
         "bad_quote", "okx answered for another instrument"),
        (lambda: parse_okx("BTCUSDT", 200, {"code": "0", "data": [{"instId": "BTC-USDT", "bidPx": "3",
                                                                   "askPx": "2", "ts": "1"}]}, T0),
         "bad_quote", "okx crossed"),
        (lambda: parse_okx("BTCUSDT", 200, {"code": "51001", "data": [], "msg": "x"}, T0),
         "unknown_symbol", "okx unknown instrument"),
        (lambda: parse_binance("BTCUSDT", 400, {"code": -1121, "msg": "Invalid symbol."}, T0),
         "unknown_symbol", "binance invalid symbol"),
    ]
    for fn, code, what in bad:
        try:
            fn()
        except PriceError as exc:
            assert exc.code == code, f"{what}: expected {code}, got {exc.code}"
        else:
            raise AssertionError(f"{what}: accepted")


@case("limit cap: buy never above the limit, sell never below it")
def limit_cap(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)  # ask + 5 bps = 60040.005, bid - 5 bps = 59960.005
    for limit, want, capped in ((60010, "60010", True), (60020, "60020", True), (70000, "60040.005", False)):
        r = led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.01, "limit_price": limit})
        assert r["ok"], f"buy limit {limit}: {r}"
        eq(r["trade"]["price"], want, f"buy limit {limit}")
        assert r["trade"]["limit_capped"] is capped, f"buy limit {limit}: limit_capped {r['trade']['limit_capped']}"
    for limit, want, capped in ((59990, "59990", True), (59980, "59980", True), (50000, "59960.005", False)):
        r = led.place_order({"symbol": "BTC", "side": "sell", "quantity": 0.01, "limit_price": limit})
        assert r["ok"], f"sell limit {limit}: {r}"
        eq(r["trade"]["price"], want, f"sell limit {limit}")
        assert r["trade"]["limit_capped"] is capped, f"sell limit {limit}: limit_capped"


@case("notional: $ amount turned into quantity from the live ask (buy) or bid (sell)")
def notional(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    r = led.place_order({"symbol": "BTC", "side": "buy", "notional": 500})
    assert r["ok"], r
    eq(r["trade"]["quantity"], "0.00833194", "500 / ask 60010, rounded down to 8 decimals")
    eq(r["trade"]["price"], "60040.005", "fill still at ask + slippage")
    eq(r["trade"]["requested_notional"], "500", "requested notional recorded")
    eq(r["trade"]["notional"], str(D("0.00833194") * D("60040.005")), "actual notional")
    r = led.place_order({"symbol": "BTC", "side": "sell", "notional": "$200"})
    assert r["ok"], r
    eq(r["trade"]["quantity"], "0.00333388", "200 / bid 59990, rounded down to 8 decimals")
    eq(r["trade"]["price"], "59960.005", "sell fill at bid - slippage")
    expect_reject(led, {"symbol": "BTC", "side": "buy", "quantity": 0.001, "notional": 50},
                  "size_required", "both quantity and notional")
    expect_reject(led, {"symbol": "BTC", "side": "buy"}, "size_required", "neither quantity nor notional")
    r = led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0, "notional": 100})
    assert r["ok"] and r["trade"]["requested_notional"] == "100", f"zero partner not ignored: {r}"


@case("get_quote: fresh quote returned, stale or missing quote is an error with no price")
def get_quote(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010, age=1.5)
    q = led.get_quote("btc")
    assert q["ok"] and q["symbol"] == "BTCUSDT", q
    eq(q["bid"], "59990", "bid")
    eq(q["ask"], "60010", "ask")
    assert q["price_source"] == "fake feed" and q["age_seconds"] == 1.5 and q["quote_time"], q
    feed.set("BTCUSDT", 59990, 60010, age=30)
    q = led.get_quote("BTC")
    assert q["ok"] is False and q["reason_code"] == "stale_quote" and "bid" not in q, f"stale: {q}"
    feed.down = True
    try:
        q = led.get_quote("BTC")
    finally:
        feed.down = False
    assert q["ok"] is False and q["reason_code"] == "feed_unavailable", f"down: {q}"


@case("order size limit: over max_order_notional rejected with both numbers; $ size checked before the feed")
def oversize(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    out = expect_reject(led, {"symbol": "BTC", "side": "buy", "quantity": 0.02}, "order_too_large", "0.02 BTC")
    assert "1200.2" in out["reason"] and "1000" in out["reason"], out["reason"]
    out = expect_reject(led, {"symbol": "BTC", "side": "buy", "notional": 5000}, "order_too_large", "$5000")
    assert "5000" in out["reason"] and "1000" in out["reason"], out["reason"]
    calls = feed.calls
    led.place_order({"symbol": "BTC", "side": "buy", "notional": 5000})
    assert feed.calls == calls, "a $5000 order fetched a quote before being refused"
    r = led.place_order({"symbol": "BTC", "side": "buy", "notional": 1000})
    assert r["ok"], f"an order of exactly the limit was rejected: {r}"


KILL = {"fee_bps": 0, "slippage_bps": 0, "max_quote_age_seconds": 10,
        "max_order_notional": 1000, "max_daily_loss": 500, "dedupe_window_seconds": 10}


@case("kill switch: trips at the loss limit, blocks buys before the feed, allows a reducing sell, survives restart")
def kill_switch(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=KILL, clock=clock)
    feed.set("BTCUSDT", 60000, 60000)
    feed.set("ETHUSDT", 3000, 3000)
    assert led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.01})["ok"], "first buy"
    risk = led.pnl()["risk"]
    eq(risk["today"]["start_equity"], "10000", "today's starting equity = equity before the first order")
    feed.set("BTCUSDT", 10001, 10001)  # equity 9400 + 100.01: loss 499.99, just under the limit
    risk = led.pnl()["risk"]
    eq(risk["today"]["pnl"], "-499.99", "today's P&L")
    assert risk["kill_switch"]["tripped"] is False, "tripped below the limit"
    feed.set("BTCUSDT", 10000, 10000)  # loss exactly 500; the order itself must trip the switch
    out = led.place_order({"symbol": "ETH", "side": "buy", "quantity": 0.01})
    assert out["ok"] is False and out["reason_code"] == "kill_switch_tripped", f"buy at the loss limit: {out}"
    assert "a loss of 500" in out["reason"] and "/attest resume" in out["reason"], out["reason"]
    q = led.pnl()
    assert q["risk"]["kill_switch"]["tripped"] is True, "trip was not stored"
    assert q["trade_count"] == 1 and q["buys"] == 1, "the rejected buy was recorded"
    calls = feed.calls
    r = led.place_order({"symbol": "ETH", "side": "buy", "quantity": 0.001})
    assert r["reason_code"] == "kill_switch_tripped" and feed.calls == calls, "stored state not checked before the feed"
    sell = led.place_order({"symbol": "BTC", "side": "sell", "quantity": 0.005})
    assert sell["ok"], f"reducing sell blocked: {sell}"
    reopened = Ledger(path, D("10000"), prices=feed, policy=KILL, clock=clock)
    p = reopened.portfolio()
    assert p["risk"]["kill_switch"]["tripped"] is True, "tripped state lost on restart"
    assert reopened.place_order({"symbol": "ETH", "side": "buy", "quantity": 0.001})["reason_code"] == \
        "kill_switch_tripped", "buy allowed after restart"


@case("kill switch: /attest resume (a human command, not a tool) clears it")
def kill_resume(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=KILL, clock=clock)  # the tripped account from above
    assert led.pnl()["risk"]["kill_switch"]["tripped"] is True, "precondition: tripped"
    pkg = load_plugin_package()
    tools, commands = {}, {}
    ctx = SimpleNamespace(register_tool=lambda **kw: tools.__setitem__(kw["name"], kw),
                          register_command=lambda name, h, **kw: commands.__setitem__(name, h))
    pkg.tools.register(ctx, lambda: led)
    assert set(tools) == {"paper_order", "get_quote", "get_portfolio", "get_pnl"}, f"tools: {sorted(tools)}"
    handle = pkg.commands.make_handler(lambda: led, lambda: D("10000"))
    msg = handle("resume")
    assert "kill switch cleared" in msg, msg
    risk = led.pnl()["risk"]
    assert risk["kill_switch"]["tripped"] is False and risk["today"]["start_equity"] is None, risk
    r = led.place_order({"symbol": "ETH", "side": "buy", "quantity": 0.001})
    assert r["ok"], f"buy after resume: {r}"
    assert "KILL SWITCH" not in handle("status") and "kill switch:    off" in handle("status"), "status text"


@case("kill switch: clears at the next UTC day")
def kill_next_day(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=KILL, clock=clock)
    feed.set("BTCUSDT", 60000, 60000)
    feed.set("ETHUSDT", 3000, 3000)
    assert led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.01})["ok"]
    feed.set("BTCUSDT", 9000, 9000)
    assert led.place_order({"symbol": "ETH", "side": "buy", "quantity": 0.001})["reason_code"] == "kill_switch_tripped"
    day1 = led.pnl()["risk"]["today"]["date"]
    clock.t += 86400
    risk = led.pnl()["risk"]
    assert risk["kill_switch"]["tripped"] is False and risk["today"]["date"] != day1, f"next day: {risk}"
    r = led.place_order({"symbol": "ETH", "side": "buy", "quantity": 0.001})
    assert r["ok"], f"buy on the next UTC day: {r}"
    eq(led.pnl()["risk"]["today"]["start_equity"], "9490", "new day snapshot (9400 cash + 0.01 BTC at 9000)")


@case("idempotency: reused client_order_id returns the original; identical repeat blocked inside the window")
def idempotency(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    first = led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.001, "client_order_id": "ord-1"},
                            session_id="s1")
    assert first["ok"], first
    before, calls = state(led), feed.calls
    again = led.place_order({"symbol": "BTC", "side": "buy", "quantity": 0.002, "client_order_id": "ord-1"},
                            session_id="s1")
    assert feed.calls == calls, "the retry fetched a quote"
    assert again["ok"] is True and again["duplicate"] is True, again
    rest = {k: v for k, v in again.items() if k not in ("duplicate", "duplicate_note")}
    assert rest == first, f"not the original fill:\n{rest}\n{first}"
    assert state(led) == before, "the retry changed the ledger"
    args = {"symbol": "BTC", "side": "buy", "quantity": 0.002}
    assert led.place_order(dict(args), session_id="s1")["ok"], "first plain order"
    before = state(led)
    dup = led.place_order(dict(args), session_id="s1")
    assert dup["ok"] is False and dup["reason_code"] == "possible_duplicate", f"repeat not blocked: {dup}"
    assert "new client_order_id" in dup["reason"], dup["reason"]
    assert state(led) == before, "the blocked repeat changed the ledger"
    assert led.place_order(dict(args, client_order_id="ord-2"), session_id="s1")["ok"], "with a new client_order_id"
    assert led.place_order(dict(args), session_id="s2")["ok"], "same order in another session"
    clock.t += 11
    assert led.place_order(dict(args), session_id="s1")["ok"], "same order outside the 10 s window"


def main():
    groups = (
        ("phase1.db", (arithmetic, restart, oversell, overspend, qty_zero, other_rejects)),
        ("market.db", (market_math,)),
        ("limits.db", (limits,)),
        ("stale.db", (stale,)),
        ("feed.db", (feed_failures, feed_hang)),
        ("symbols.db", (symbols,)),
        ("marks.db", (marks,)),
        ("migration.db", (migration,)),
        ("fallback.db", (fallback_chain,)),
        ("okx_ts.db", (okx_ts,)),
        ("parsers.db", (parsers,)),
        ("limit_cap.db", (limit_cap,)),
        ("notional.db", (notional,)),
        ("get_quote.db", (get_quote,)),
        ("oversize.db", (oversize,)),
        ("kill.db", (kill_switch, kill_resume)),
        ("kill_next_day.db", (kill_next_day,)),
        ("idempotency.db", (idempotency,)),
    )
    with tempfile.TemporaryDirectory(prefix="attest-check-") as tmp:
        assert ".hermes" not in tmp, "refusing to run against a Hermes folder"
        print(f"temporary folder: {tmp}  (fake price source, no network)")
        for name, checks in groups:
            clock = Clock()
            feed = FakeSource(clock)
            # Phase 1 cases share one account, in order, like the Phase 1 script.
            for check in checks:
                check(Path(tmp) / name, feed, clock)
    passed = sum(1 for _n, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
