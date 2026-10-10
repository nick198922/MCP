"""Binance USDT-M futures-data endpoints (public, no key).

These are the `/futures/data/*` endpoints — a separate surface from the ccxt-handled matching engine.
Account long/short ratio and open-interest history are not exposed uniformly through ccxt, so we hit the
underlying REST directly through our own `Http` so retries / rate-limit / cache / health all apply.
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://fapi.binance.com"

VALID_PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")


def check_period(period: str) -> str:
    if period not in VALID_PERIODS:
        raise ProviderError("binance_futures", "data_unavailable", f"period must be one of {VALID_PERIODS}, got {period!r}")
    return period


class BinanceFutures:
    name = "binance_futures"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def _get(self, path: str, params: dict[str, Any], ttl: float, tool: str) -> Fetched:
        f = await self.http.get_json(path, params=params, ttl=ttl, tool=tool, args=params)
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", f"unexpected response shape at {path}")
        return f

    async def long_short_account_ratio(self, symbol: str, period: str, limit: int) -> Fetched:
        return await self._get(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": check_period(period), "limit": limit},
            ttl=30,
            tool="futures/data/globalLongShortAccountRatio",
        )

    async def open_interest_hist(self, symbol: str, period: str, limit: int) -> Fetched:
        return await self._get(
            "/futures/data/openInterestHist",
            {"symbol": symbol, "period": check_period(period), "limit": limit},
            ttl=30,
            tool="futures/data/openInterestHist",
        )
