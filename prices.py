"""attest price feed: live best bid and ask from public exchange market data.

Sources, in order (all read-only HTTPS GETs, no key, no account):
  1. OKX     https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT
             bidPx / askPx; quote time = the exchange's own "ts" (ms).
  2. Kraken  https://api.kraken.com/0/public/Ticker?pair=XBTUSDT
             b[0] / a[0]; no timestamp, so quote time = fetched_at.
  3. Binance https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT
             bidPrice / askPrice; no timestamp, so quote time = fetched_at.

FallbackSource tries them in order and moves on when one fails, times out,
or returns a bad, crossed or stale quote. If none gives a fresh quote it
raises PriceError; there is never a fallback price.

Each source has a hard deadline (urllib's own timeout does not cover the DNS
lookup), a 2 second cache that keeps the quote's original times, and a short
fail-fast memory after an outage that remembers the failure, never a price.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

TIMEOUT_SECONDS = 3
CACHE_SECONDS = 2.0
FUTURE_TOLERANCE_SECONDS = 2.0  # a quote time this far ahead of our clock is treated as bad
_USER_AGENT = "attest-paper-trading/0.3"
QUOTE_ASSET = "USDT"


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    fetched_at: float          # unix seconds when the response arrived here
    source: str
    quote_time: float | None = None  # exchange timestamp when it sends one
    skipped: tuple = ()        # (source, reason) for sources tried before this one

    @property
    def time(self) -> float:
        """The time the staleness check uses: the exchange's, else ours."""
        return self.quote_time if self.quote_time is not None else self.fetched_at


class PriceError(Exception):
    """code: feed_unavailable, unknown_symbol, bad_quote or stale_quote."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _dec(raw, field: str, source: str) -> Decimal:
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise PriceError("bad_quote", f"{source} sent an unreadable {field}: {raw!r}.")
    if not d.is_finite() or d <= 0:
        raise PriceError("bad_quote", f"{source} sent an unusable {field}: {raw!r}.")
    return d


def _checked(symbol, bid, ask, fetched_at, source, quote_time=None) -> Quote:
    if ask < bid:
        raise PriceError("bad_quote", f"{source} sent a crossed quote (bid {bid} > ask {ask}).")
    return Quote(symbol, bid, ask, fetched_at, source, quote_time)


def base_asset(symbol: str) -> str:
    if not symbol.endswith(QUOTE_ASSET) or len(symbol) <= len(QUOTE_ASSET):
        raise PriceError("unknown_symbol", f"{symbol} is not a {QUOTE_ASSET} pair.")
    return symbol[: -len(QUOTE_ASSET)]


# ------------------------------------------------------------------ parsers
# Pure functions, so scripts/ledger_check.py can test them on sample payloads.

OKX_SOURCE = "okx (www.okx.com /api/v5/market/ticker)"
KRAKEN_SOURCE = "kraken (api.kraken.com /0/public/Ticker)"
BINANCE_SOURCE = "binance (data-api.binance.vision /api/v3/ticker/bookTicker)"
_KRAKEN_BASE = {"BTC": "XBT"}


def okx_inst_id(symbol: str) -> str:
    return f"{base_asset(symbol)}-{QUOTE_ASSET}"


def kraken_pair(symbol: str) -> str:
    base = base_asset(symbol)
    return f"{_KRAKEN_BASE.get(base, base)}{QUOTE_ASSET}"


def parse_okx(symbol: str, status: int, payload, fetched_at: float) -> Quote:
    inst = okx_inst_id(symbol)
    if not isinstance(payload, dict):
        raise PriceError("bad_quote", "okx sent a response that is not an object.")
    code = str(payload.get("code", ""))
    if code == "51001":
        raise PriceError("unknown_symbol", f"okx does not list {inst}.")
    if code != "0" or status != 200:
        raise PriceError("feed_unavailable", f"okx answered HTTP {status} code {code or '?'}: "
                                             f"{str(payload.get('msg', ''))[:120]}")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise PriceError("bad_quote", "okx sent no single ticker.")
    row = data[0]
    if row.get("instId") != inst:
        raise PriceError("bad_quote", f"okx answered for {row.get('instId')!r}, not {inst}.")
    try:
        ts = int(str(row.get("ts"))) / 1000.0
    except (TypeError, ValueError):
        raise PriceError("bad_quote", f"okx sent an unreadable ts: {row.get('ts')!r}.")
    return _checked(symbol, _dec(row.get("bidPx"), "bid", "okx"), _dec(row.get("askPx"), "ask", "okx"),
                    fetched_at, OKX_SOURCE, quote_time=ts)


def parse_kraken(symbol: str, status: int, payload, fetched_at: float) -> Quote:
    pair = kraken_pair(symbol)
    if not isinstance(payload, dict):
        raise PriceError("bad_quote", "kraken sent a response that is not an object.")
    errors = payload.get("error") or []
    if any("Unknown asset pair" in str(e) for e in errors):
        raise PriceError("unknown_symbol", f"kraken does not list {pair}.")
    if errors or status != 200:
        raise PriceError("feed_unavailable", f"kraken answered HTTP {status}: {str(errors)[:120]}")
    result = payload.get("result")
    if not isinstance(result, dict) or len(result) != 1:
        raise PriceError("bad_quote", f"kraken sent {len(result) if isinstance(result, dict) else 'no'} "
                                      "pairs, expected exactly one.")
    key, row = next(iter(result.items()))
    # Don't assume the key name: it must be the pair we asked for (or Kraken's
    # X-prefixed form of it), otherwise the quote belongs to something else.
    if str(key).upper() not in (pair, "X" + pair):
        raise PriceError("bad_quote", f"kraken answered for {key!r}, not {pair}.")
    try:
        bid_raw, ask_raw = row["b"][0], row["a"][0]
    except (TypeError, KeyError, IndexError):
        raise PriceError("bad_quote", "kraken sent a ticker without bid/ask.")
    return _checked(symbol, _dec(bid_raw, "bid", "kraken"), _dec(ask_raw, "ask", "kraken"),
                    fetched_at, KRAKEN_SOURCE)


def parse_binance(symbol: str, status: int, payload, fetched_at: float) -> Quote:
    if isinstance(payload, dict) and payload.get("code") == -1121:
        raise PriceError("unknown_symbol", f"binance does not list {symbol}.")
    if status != 200:
        raise PriceError("feed_unavailable", f"binance answered HTTP {status}.")
    if not isinstance(payload, dict) or payload.get("symbol") != symbol:
        raise PriceError("bad_quote", f"binance answered for the wrong symbol: {payload!r:.200}.")
    return _checked(symbol, _dec(payload.get("bidPrice"), "bid", "binance"),
                    _dec(payload.get("askPrice"), "ask", "binance"), fetched_at, BINANCE_SOURCE)


def parse_book_ticker(symbol: str, payload: dict, fetched_at: float, source: str = BINANCE_SOURCE) -> Quote:
    """Phase 2 name for the Binance parser, kept for callers and checks."""
    return parse_binance(symbol, 200, payload, fetched_at)


# ------------------------------------------------------------------ sources

class HttpSource:
    """One exchange. Subclasses set name and implement url_for and parse."""

    name = "source"
    FAIL_FAST_SECONDS = 5.0

    def __init__(self, timeout: float = TIMEOUT_SECONDS, cache_seconds: float = CACHE_SECONDS,
                 clock=time.time):
        self.timeout, self.cache_seconds, self.clock = timeout, cache_seconds, clock
        self._cache: dict[str, Quote] = {}
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._down_reason = ""

    def url_for(self, symbol: str) -> str:
        raise NotImplementedError

    def parse(self, symbol: str, status: int, payload, fetched_at: float) -> Quote:
        raise NotImplementedError

    def quote(self, symbol: str) -> Quote:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(symbol)
            down_until, down_reason = self._down_until, self._down_reason
        if hit is not None and 0 <= now - hit.fetched_at <= self.cache_seconds:
            return hit
        if now < down_until:
            # Remembers a failure, never a price: the answer is still "no quote".
            raise PriceError("feed_unavailable", down_reason)
        try:
            q = self._fetch(symbol)
        except PriceError as exc:
            if exc.code == "feed_unavailable":
                with self._lock:
                    self._down_until, self._down_reason = self.clock() + self.FAIL_FAST_SECONDS, exc.reason
            raise
        with self._lock:
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[symbol] = q
            self._down_until = 0.0
        return q

    def _fetch(self, symbol: str) -> Quote:
        """_request with a hard deadline: it runs on a daemon thread and we stop
        waiting after self.timeout seconds, DNS lookup included."""
        box = {}

        def run():
            try:
                box["quote"] = self._request(symbol)
            except BaseException as exc:  # handed back to the caller below
                box["error"] = exc

        worker = threading.Thread(target=run, name=f"attest-{self.name}-fetch", daemon=True)
        worker.start()
        worker.join(self.timeout)
        if worker.is_alive():
            raise PriceError("feed_unavailable", f"{self.name} did not answer within {self.timeout:g} seconds.")
        if "error" in box:
            raise box["error"]
        return box["quote"]

    def _request(self, symbol: str) -> Quote:
        url = self.url_for(symbol)
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, body = resp.status, resp.read(65536)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(65536)
            except Exception:
                body = b""
            if status >= 500 or status == 429 or status == 418:
                raise PriceError("feed_unavailable", f"{self.name} answered HTTP {status}.")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PriceError("feed_unavailable", f"could not reach {self.name} ({getattr(exc, 'reason', exc)}).")
        fetched_at = self.clock()
        try:
            payload = json.loads(body)
        except ValueError:
            raise PriceError("bad_quote" if status == 200 else "feed_unavailable",
                             f"{self.name} sent a response that is not JSON (HTTP {status}).")
        return self.parse(symbol, status, payload, fetched_at)


class OkxSource(HttpSource):
    name = "okx"
    URL = "https://www.okx.com/api/v5/market/ticker"

    def url_for(self, symbol):
        return f"{self.URL}?{urllib.parse.urlencode({'instId': okx_inst_id(symbol)})}"

    def parse(self, symbol, status, payload, fetched_at):
        return parse_okx(symbol, status, payload, fetched_at)


class KrakenSource(HttpSource):
    name = "kraken"
    URL = "https://api.kraken.com/0/public/Ticker"

    def url_for(self, symbol):
        return f"{self.URL}?{urllib.parse.urlencode({'pair': kraken_pair(symbol)})}"

    def parse(self, symbol, status, payload, fetched_at):
        return parse_kraken(symbol, status, payload, fetched_at)


class BinanceSource(HttpSource):
    name = "binance"
    URL = "https://data-api.binance.vision/api/v3/ticker/bookTicker"

    def url_for(self, symbol):
        return f"{self.URL}?{urllib.parse.urlencode({'symbol': symbol})}"

    def parse(self, symbol, status, payload, fetched_at):
        return parse_binance(symbol, status, payload, fetched_at)


class FallbackSource:
    """Tries each source in order and returns the first fresh, sane quote."""

    def __init__(self, sources, max_age=lambda: 10, clock=time.time):
        self.sources = list(sources)
        self.max_age = max_age
        self.clock = clock

    def quote(self, symbol: str) -> Quote:
        failures = []
        for src in self.sources:
            name = getattr(src, "name", src.__class__.__name__)
            try:
                q = src.quote(symbol)
            except PriceError as exc:
                failures.append((name, exc.code, exc.reason))
                continue
            except Exception as exc:  # a source bug must not stop the next source
                failures.append((name, "feed_unavailable", f"{exc.__class__.__name__}: {exc}"))
                continue
            age = self.clock() - q.time
            limit = float(self.max_age() if callable(self.max_age) else self.max_age)
            if age > limit:
                failures.append((name, "stale_quote", f"{name} quote is {age:.3f} seconds old (limit {limit:g})."))
                continue
            if age < -FUTURE_TOLERANCE_SECONDS:
                failures.append((name, "bad_quote", f"{name} quote time is {-age:.3f} seconds in the future."))
                continue
            return replace(q, skipped=tuple((n, r) for n, _c, r in failures))
        codes = {c for _n, c, _r in failures}
        if codes == {"unknown_symbol"}:
            code = "unknown_symbol"
        elif "stale_quote" in codes:
            code = "stale_quote"
        else:
            code = "feed_unavailable"
        detail = "; ".join(f"{n}: {r}" for n, _c, r in failures) or "no sources configured"
        raise PriceError(code, f"no fresh {symbol} quote from any source ({detail}).")
