"""Binance spot public market data (no key): klines with the taker-buy split.

ccxt's unified OHLCV drops the taker-buy columns that spot CVD needs, so `/api/v3/klines` is read directly through
our own `Http` (retries, rate limit, cache, health, `Evidence`). Rows are Binance's as-is:
``[open_ms, open, high, low, close, base_volume, close_ms, quote_volume, trades, taker_buy_base, taker_buy_quote,
ignore]`` with numbers as strings. One request returns at most 1000 klines; `klines_range` walks longer windows.
`walk_klines` is shared with the USD-M klines of `providers.binance_futures` (same row shape, 1500 per request).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..core.envelope import Evidence, Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.binance.com"
KLINES_PAGE = 1000
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def check_interval(provider: str, interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ProviderError(provider, "data_unavailable", f"interval must be one of {tuple(INTERVAL_MS)}, got {interval!r}")
    return INTERVAL_MS[interval]


async def walk_klines(
    provider: str,
    tool: str,
    page_fn: Callable[[int, int, int], Awaitable[Fetched]],
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    page: int,
    max_pages: int,
) -> Fetched:
    """Klines with open time in [start_ms, end_ms), oldest first, walked forward page by page.

    ``page_fn(start, end_inclusive, limit)`` fetches one page. A short page or an empty one ends the walk. Returns
    ``{"rows": [...], "complete": bool}``; ``complete`` is False when ``max_pages`` was hit before ``end_ms`` or a later
    page failed (rows fetched so far are kept). A failure on the first page raises.
    """
    step = INTERVAL_MS[interval]
    rows: dict[int, list[Any]] = {}
    since, pages, latency, cached, complete = start_ms, 0, 0, True, True
    while since < end_ms:
        if pages >= max_pages:
            complete = False
            break
        try:
            f = await page_fn(since, end_ms - 1, page)
        except ProviderError:
            if pages == 0:
                raise
            complete = False
            break
        pages += 1
        latency += f.evidence.latency_ms or 0
        cached = cached and f.evidence.cached
        batch = []
        for x in f.data:
            try:
                t = int(x[0])
            except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                continue
            if start_ms <= t < end_ms:
                batch.append((t, x))
        for t, x in batch:
            rows[t] = x
        if not batch or len(f.data) < page:
            break
        nxt = max(t for t, _ in batch) + step
        if nxt <= since:
            break
        since = nxt
    ev = Evidence(
        provider=provider,
        tool=tool,
        args={"symbol": symbol, "interval": interval, "start_ms": start_ms, "end_ms": end_ms, "pages": pages},
        latency_ms=latency,
        cached=cached and pages > 0,
    )
    return Fetched({"rows": [rows[k] for k in sorted(rows)], "complete": complete}, ev)


class BinanceSpot:
    name = "binance_spot"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = KLINES_PAGE) -> Fetched:
        """One page of `/api/v3/klines` with open time in [start_ms, end_ms] (both inclusive), at most 1000 rows."""
        check_interval(self.name, interval)
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": min(int(limit), KLINES_PAGE),
        }
        f = await self.http.get_json("/api/v3/klines", params=params, ttl=300, tool="api/v3/klines", args=params)
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", "unexpected response shape at /api/v3/klines")
        return f

    async def klines_range(self, symbol: str, interval: str, start_ms: int, end_ms: int, *, max_pages: int = 6) -> Fetched:
        """Spot klines with open time in [start_ms, end_ms): ``{"rows": [...], "complete": bool}`` (see `walk_klines`)."""
        check_interval(self.name, interval)
        return await walk_klines(
            self.name,
            "api/v3/klines",
            lambda s, e, n: self.klines(symbol, interval, s, e, n),
            symbol,
            interval,
            start_ms,
            end_ms,
            KLINES_PAGE,
            max_pages,
        )
