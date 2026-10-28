"""Coinbase Exchange public candles (no key): `GET /products/{BASE-USD}/candles`.

Used for the Coinbase premium (Coinbase BTC-USD against Binance BTC-USDT; tools/flow.py). Response rows are
``[time_s, low, high, open, close, volume]``, newest first, numbers as JSON numbers; ``start`` / ``end`` are ISO-8601
UTC and both inclusive; at most 300 candles per request (checked live 2026-10-28). An unknown product answers HTTP 404
``{"message": "NotFound"}``, which `Http` turns into ProviderError("data_unavailable").
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ..core.envelope import Evidence, Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.exchange.coinbase.com"
MAX_CANDLES = 300
GRANULARITIES = (60, 300, 900, 3600, 21600, 86400)
_PRODUCT = re.compile(r"^[A-Z0-9]{1,15}-[A-Z]{3,5}$")


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Coinbase:
    name = "coinbase"

    def __init__(self, http: Http) -> None:
        self.http = http

    def _check(self, product: str, granularity: int) -> None:
        if not _PRODUCT.match(product or ""):
            raise ProviderError(self.name, "data_unavailable", "product must look like 'BTC-USD'")
        if granularity not in GRANULARITIES:
            raise ProviderError(self.name, "data_unavailable", f"granularity must be one of {GRANULARITIES}, got {granularity!r}")

    async def candles(self, product: str, granularity: int, start_ms: int, end_ms: int) -> Fetched:
        """One request: candles with open time in [start_ms, end_ms] (both inclusive, at most 300 of them)."""
        self._check(product, granularity)
        params = {"granularity": granularity, "start": _iso(start_ms), "end": _iso(end_ms)}
        f = await self.http.get_json(
            f"/products/{product}/candles", params=params, ttl=300, tool="products/candles", args={"product": product, **params}
        )
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", "unexpected response shape at /products/{id}/candles")
        return f

    async def candles_range(self, product: str, granularity: int, start_ms: int, end_ms: int, *, max_pages: int = 8) -> Fetched:
        """Candles with open time in [start_ms, end_ms), oldest first, fetched in chunks of 300.

        Returns ``{"rows": [...], "complete": bool}``; ``complete`` is False when ``max_pages`` ran out or a later chunk
        failed (rows so far are kept). A failure on the first chunk raises.
        """
        self._check(product, granularity)
        step = granularity * 1000
        rows: dict[int, list[Any]] = {}
        lo, pages, latency, cached, complete = int(start_ms), 0, 0, True, True
        while lo < end_ms:
            if pages >= max_pages:
                complete = False
                break
            hi = min(int(end_ms), lo + MAX_CANDLES * step) - step
            try:
                f = await self.candles(product, granularity, lo, hi)
            except ProviderError:
                if pages == 0:
                    raise
                complete = False
                break
            pages += 1
            latency += f.evidence.latency_ms or 0
            cached = cached and f.evidence.cached
            for x in f.data:
                try:
                    t = int(x[0]) * 1000
                except (KeyError, TypeError, ValueError, IndexError, OverflowError):
                    continue
                if start_ms <= t < end_ms:
                    rows[t] = x
            lo = hi + step
        ev = Evidence(
            provider=self.name,
            tool="products/candles",
            args={"product": product, "granularity": granularity, "start_ms": start_ms, "end_ms": end_ms, "pages": pages},
            latency_ms=latency,
            cached=cached and pages > 0,
        )
        return Fetched({"rows": [rows[k] for k in sorted(rows)], "complete": complete}, ev)
