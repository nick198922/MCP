"""Bybit v5 public market endpoints (no key).

The ccxt layer covers the common ticker/orderbook/funding calls uniformly, but Bybit's long/short
account ratio and open-interest history live under `/v5/market/*` with their own shape and
response wrapping (`{retCode, retMsg, result: {list: [...]}}`), so we hit them directly through
our own `Http` to keep retries / rate-limit / cache / health consistent.
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Evidence, Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.bybit.com"

VALID_PERIODS = ("5min", "15min", "30min", "1h", "4h", "1d")
PERIOD_MS = {"5min": 300_000, "15min": 900_000, "30min": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
OI_PAGE = 200


def check_period(period: str) -> str:
    if period not in VALID_PERIODS:
        raise ProviderError("bybit", "data_unavailable", f"period must be one of {VALID_PERIODS}, got {period!r}")
    return period


class Bybit:
    name = "bybit"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def _get(self, path: str, params: dict[str, Any], ttl: float, tool: str) -> Fetched:
        f = await self.http.get_json(path, params=params, ttl=ttl, tool=tool, args=params)
        body = f.data
        if not isinstance(body, dict):
            raise ProviderError(self.name, "backend_error", f"unexpected response shape at {path}")
        if body.get("retCode") not in (0, "0"):
            raise ProviderError(self.name, "backend_error", f"{body.get('retCode')}: {body.get('retMsg')}")
        result = body.get("result") or {}
        rows = result.get("list") or []
        return Fetched(rows, f.evidence)

    async def long_short_account_ratio(self, symbol: str, period: str, limit: int) -> Fetched:
        return await self._get(
            "/v5/market/account-ratio",
            {"category": "linear", "symbol": symbol, "period": check_period(period), "limit": limit},
            ttl=30,
            tool="v5/market/account-ratio",
        )

    async def open_interest(
        self, symbol: str, interval_time: str, limit: int, *, start_ms: int | None = None, end_ms: int | None = None
    ) -> Fetched:
        """OI rows ``[{openInterest (base coins), timestamp}]``, newest first; with a window, the newest ``limit`` in it."""
        params: dict[str, Any] = {"category": "linear", "symbol": symbol, "intervalTime": check_period(interval_time), "limit": limit}
        if start_ms is not None and end_ms is not None:
            params.update({"startTime": int(start_ms), "endTime": int(end_ms)})
        return await self._get("/v5/market/open-interest", params, ttl=30, tool="v5/market/open-interest")

    async def open_interest_range(self, symbol: str, interval_time: str, start_ms: int, end_ms: int, *, max_pages: int = 5) -> Fetched:
        """OI snapshots with timestamp in [start_ms, end_ms], oldest first: ``{"rows": [...], "complete": bool}``.

        One request returns at most 200 rows (checked live 2026-10-28: a 300-interval window gives its newest 200), so
        the range is cut into windows of 200 intervals walked backward from ``end_ms``. ``complete`` is False when
        ``max_pages`` ran out or a later page failed (rows so far are kept); a failure on the first page raises.
        """
        step = PERIOD_MS[check_period(interval_time)]
        rows: dict[int, dict[str, Any]] = {}
        hi, pages, latency, cached, complete = int(end_ms), 0, 0, True, True
        while hi >= start_ms:
            if pages >= max_pages:
                complete = False
                break
            lo = max(int(start_ms), hi - (OI_PAGE - 1) * step)
            try:
                f = await self.open_interest(symbol, interval_time, OI_PAGE, start_ms=lo, end_ms=hi)
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
                    t = int(x["timestamp"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if start_ms <= t <= end_ms:
                    rows[t] = x
            hi = lo - 1
        ev = Evidence(
            provider=self.name,
            tool="v5/market/open-interest",
            args={"symbol": symbol, "intervalTime": interval_time, "start_ms": start_ms, "end_ms": end_ms, "pages": pages},
            latency_ms=latency,
            cached=cached and pages > 0,
        )
        return Fetched({"rows": [rows[k] for k in sorted(rows)], "complete": complete}, ev)
