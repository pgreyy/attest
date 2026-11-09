"""attest price feed: live best bid and ask from Binance public market data.

One read-only GET, no key, no account:
    https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT
    -> {"symbol": "BTCUSDT", "bidPrice": "...", "bidQty": "...", "askPrice": "...", "askQty": "..."}

The endpoint carries no timestamp, so fetched_at is the moment the response
arrived here. The ledger refuses any quote older than max_quote_age_seconds.
A failure is always raised as PriceError; there is no fallback price.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

BINANCE_URL = "https://data-api.binance.vision/api/v3/ticker/bookTicker"
BINANCE_SOURCE = "binance data-api.binance.vision bookTicker"
TIMEOUT_SECONDS = 3
CACHE_SECONDS = 2.0
_USER_AGENT = "attest-paper-trading/0.2"


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    fetched_at: float  # unix seconds when the quote arrived
    source: str


class PriceError(Exception):
    """code: feed_unavailable, unknown_symbol or bad_quote."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _dec(raw, field: str) -> Decimal:
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise PriceError("bad_quote", f"price feed sent an unreadable {field}: {raw!r}.")
    if not d.is_finite() or d <= 0:
        raise PriceError("bad_quote", f"price feed sent an unusable {field}: {raw!r}.")
    return d


def parse_book_ticker(symbol: str, payload: dict, fetched_at: float, source: str = BINANCE_SOURCE) -> Quote:
    if not isinstance(payload, dict) or payload.get("symbol") != symbol:
        raise PriceError("bad_quote", f"price feed answered for the wrong symbol: {payload!r:.200}.")
    bid, ask = _dec(payload.get("bidPrice"), "bid"), _dec(payload.get("askPrice"), "ask")
    if ask < bid:
        raise PriceError("bad_quote", f"price feed sent a crossed quote (bid {bid} > ask {ask}).")
    return Quote(symbol, bid, ask, fetched_at, source)


class BinanceSource:
    """Fetches bookTicker quotes, caching each one for CACHE_SECONDS. A cached
    quote keeps its original fetched_at, so the ledger's age check still applies."""

    def __init__(self, url: str = BINANCE_URL, timeout: float = TIMEOUT_SECONDS,
                 cache_seconds: float = CACHE_SECONDS, clock=time.time):
        self.url, self.timeout, self.cache_seconds, self.clock = url, timeout, cache_seconds, clock
        self._cache: dict[str, Quote] = {}
        self._lock = threading.Lock()
        self._down_until = 0.0  # after a feed outage, fail fast for FAIL_FAST_SECONDS
        self._down_reason = ""

    FAIL_FAST_SECONDS = 5.0

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
        """_request with a hard deadline. urllib's timeout does not cover the DNS
        lookup, which can hang for 15 seconds or more, so the request runs on a
        daemon thread and we stop waiting after self.timeout seconds."""
        box = {}

        def run():
            try:
                box["quote"] = self._request(symbol)
            except BaseException as exc:  # handed back to the caller below
                box["error"] = exc

        worker = threading.Thread(target=run, name="attest-price-fetch", daemon=True)
        worker.start()
        worker.join(self.timeout)
        if worker.is_alive():
            raise PriceError("feed_unavailable",
                             f"the Binance price feed did not answer within {self.timeout:g} seconds.")
        if "error" in box:
            raise box["error"]
        return box["quote"]

    def _request(self, symbol: str) -> Quote:
        url = f"{self.url}?{urllib.parse.urlencode({'symbol': symbol})}"
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read(65536)
            fetched_at = self.clock()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read(4096) or b"{}")
            except Exception:
                pass
            if exc.code == 400 and isinstance(detail, dict) and detail.get("code") == -1121:
                raise PriceError("unknown_symbol", f"Binance does not list {symbol}.")
            raise PriceError("feed_unavailable", f"Binance price feed returned HTTP {exc.code}.")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PriceError("feed_unavailable", f"could not reach the Binance price feed ({reason}).")
        try:
            payload = json.loads(body)
        except ValueError:
            raise PriceError("bad_quote", "Binance price feed sent a response that is not JSON.")
        return parse_book_ticker(symbol, payload, fetched_at)
