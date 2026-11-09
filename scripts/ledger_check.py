"""Arithmetic, broker and rejection checks for the attest ledger.

Runs against databases in a fresh temporary folder, never the real one, and
uses a fake price source: this script makes no network calls.
Prints PASS for a case only when it ran and every assertion held.

    python3 scripts/ledger_check.py
"""
import sqlite3
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from ledger import Ledger, Rejected, normalize_symbol  # noqa: E402
from prices import BinanceSource, PriceError, Quote, parse_book_ticker  # noqa: E402

D = Decimal
RESULTS = []
T0 = 1_800_000_000.0  # fixed fake "now"
NO_COSTS = {"fee_bps": 0, "slippage_bps": 0, "max_quote_age_seconds": 10}
LIVE_COSTS = {"fee_bps": 10, "slippage_bps": 5, "max_quote_age_seconds": 10}


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


@case("reject: unknown side, missing price, duplicate client_order_id")
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
    dup = expect_reject(led, dup_args, "duplicate_client_order_id", "duplicate client_order_id")
    assert dup["existing_trade_id"] == first["trade"]["id"], "duplicate did not point at the first trade"
    calls = feed.calls
    led.place_order(dup_args)
    assert feed.calls == calls, "a duplicate order fetched a quote before being refused"


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


@case("limit: marketable fills at market plus slippage, non-marketable rejected")
def limits(path, feed, clock):
    led = Ledger(path, D("10000"), prices=feed, policy=LIVE_COSTS, clock=clock)
    feed.set("BTCUSDT", 59990, 60010)
    out = expect_reject(led, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01, "limit_price": 60000},
                        "not_marketable", "buy limit below ask")
    assert "resting orders" in out["reason"], out["reason"]
    buy = led.place_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01, "limit_price": 60010})
    assert buy["ok"], f"marketable buy rejected: {buy}"
    eq(buy["trade"]["price"], "60040.005", "marketable buy fills at ask + slippage, not at the limit")
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


@case("migration: a Phase 1 database opens, gains quote columns, keeps its data")
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
    for col in ("quote_bid", "quote_ask", "quote_fetched_at", "price_source", "slippage_bps"):
        assert col in cols, f"missing column {col}"
    assert version == "2", f"schema_version {version}"
    p, q = led.portfolio(), led.pnl()
    eq(p["cash"], "9400", "cash kept")
    eq(p["positions"][0]["quantity"], "0.01", "position kept")
    assert q["trade_count"] == 1, "old trade lost"
    old = trade_row(path, 1)
    assert old["quote_bid"] is None, "old trade got an invented quote"


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
