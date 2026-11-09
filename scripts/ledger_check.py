"""Arithmetic and rejection checks for the attest ledger.

Runs against a database in a fresh temporary folder, never the real one.
Prints PASS for a case only when it ran and every assertion held.

    python3 scripts/ledger_check.py
"""
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from ledger import Ledger  # noqa: E402

D = Decimal
RESULTS = []


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


@case("arithmetic: buy 0.01 @ 60000, sell 0.005 @ 62000")
def arithmetic(path):
    led = Ledger(path, D("10000"))
    # Same shape the probe saw: quantity as float, limit_price as int.
    buy = led.place_order({"symbol": "BTCUSDT", "side": "buy", "quantity": 0.01,
                           "order_type": "limit", "limit_price": 60000})
    assert buy["ok"], f"buy rejected: {buy}"
    eq(buy["cash_after"], "9400", "cash after buy")
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
    eq(pos["mark_price"], "62000", "mark price (last fill)")
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
def restart(path):
    led = Ledger(path, D("10000"))
    before = (stable(led.portfolio()), stable(led.pnl()))
    del led
    reopened = Ledger(path, D("99999"))  # a different default must not touch the existing account
    after = (stable(reopened.portfolio()), stable(reopened.pnl()))
    assert before == after, f"changed after reopen:\n{before}\n{after}"
    eq(after[0]["cash"], "9710", "cash after reopen")


def expect_reject(led, args, code, what):
    before = (stable(led.portfolio()), stable(led.pnl()))
    out = led.place_order(args)
    assert out["ok"] is False and out["status"] == "rejected", f"{what} was not rejected: {out}"
    assert out["reason_code"] == code, f"{what}: expected {code}, got {out['reason_code']}"
    assert out["reason"], f"{what}: empty reason"
    after = (stable(led.portfolio()), stable(led.pnl()))
    assert before == after, f"{what}: ledger changed on a rejection"
    return out


@case("reject: oversell")
def oversell(path):
    expect_reject(Ledger(path), {"symbol": "BTCUSDT", "side": "sell", "quantity": 0.006,
                                 "limit_price": 62000}, "insufficient_position", "oversell")


@case("reject: overspend")
def overspend(path):
    expect_reject(Ledger(path), {"symbol": "BTCUSDT", "side": "buy", "quantity": 1,
                                 "limit_price": 60000}, "insufficient_cash", "overspend")


@case("reject: quantity 0")
def qty_zero(path):
    expect_reject(Ledger(path), {"symbol": "BTCUSDT", "side": "buy", "quantity": 0,
                                 "limit_price": 60000}, "invalid_quantity", "quantity 0")


@case("reject: market order")
def market(path):
    out = expect_reject(Ledger(path), {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001,
                                       "order_type": "market"}, "market_not_available", "market order")
    assert "Phase 2" in out["reason"], f"reason does not mention Phase 2: {out['reason']}"


@case("reject: unknown side, missing price, duplicate client_order_id")
def other_rejects(path):
    led = Ledger(path)
    expect_reject(led, {"symbol": "BTCUSDT", "side": "hold", "quantity": 1, "limit_price": 1},
                  "invalid_side", "unknown side")
    expect_reject(led, {"symbol": "BTCUSDT", "side": "buy", "quantity": 0.001, "order_type": "limit"},
                  "missing_limit_price", "missing price")
    first = led.place_order({"symbol": "ETHUSDT", "side": "buy", "quantity": "0.1",
                             "limit_price": "3000", "client_order_id": "abc-1"})
    assert first["ok"], f"first order rejected: {first}"
    dup = expect_reject(led, {"symbol": "ETHUSDT", "side": "buy", "quantity": "0.1",
                              "limit_price": "3000", "client_order_id": "abc-1"},
                        "duplicate_client_order_id", "duplicate client_order_id")
    assert dup["existing_trade_id"] == first["trade"]["id"], "duplicate did not point at the first trade"


def main():
    with tempfile.TemporaryDirectory(prefix="attest-check-") as tmp:
        path = Path(tmp) / "attest-check.db"
        assert ".hermes" not in str(path), "refusing to run against a Hermes folder"
        print(f"temporary db: {path}")
        # Order matters: each case builds on the account the arithmetic case left.
        for check in (arithmetic, restart, oversell, overspend, qty_zero, market, other_rejects):
            check(path)
    passed = sum(1 for _n, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) and RESULTS else 1


if __name__ == "__main__":
    sys.exit(main())
