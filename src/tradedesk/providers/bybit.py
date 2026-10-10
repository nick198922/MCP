"""Bybit v5 public market endpoints (no key).

The ccxt layer covers the common ticker/orderbook/funding calls uniformly, but Bybit's long/short
account ratio and open-interest history live under `/v5/market/*` with their own shape and
response wrapping (`{retCode, retMsg, result: {list: [...]}}`), so we hit them directly through
our own `Http` to keep retries / rate-limit / cache / health consistent.
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.bybit.com"

VALID_PERIODS = ("5min", "15min", "30min", "1h", "4h", "1d")


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

    async def open_interest(self, symbol: str, interval_time: str, limit: int) -> Fetched:
        return await self._get(
            "/v5/market/open-interest",
            {"category": "linear", "symbol": symbol, "intervalTime": check_period(interval_time), "limit": limit},
            ttl=30,
            tool="v5/market/open-interest",
        )
