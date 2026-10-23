"""Coinalyze API (free key): derivatives history per exchange (symbol suffix = exchange code; '.A' = Binance).

All history endpoints take `symbols=BTCUSDT_PERP.A&interval=daily&from=&to=` (unix seconds) and answer
`[{"symbol": ..., "history": [...]}]` (`[]` when no data in range):

* `/liquidation-history` (+ `convert_to_usd`) -> rows `{"t", "l": long, "s": short}`;
* `/open-interest-history` (+ `convert_to_usd`) -> rows `{"t", "o", "h", "l", "c"}` (coins, or USD);
* `/funding-rate-history` -> rows `{"t", "o", "h", "l", "c"}` in PERCENT (0.01 = 0.01 %);
* `/long-short-ratio-history` -> rows `{"t", "r": ratio, "l": % long, "s": % short}`.

The key goes in the `api_key` request header only (never in the URL, params, cache key or evidence).
Free tier is ~40 requests/minute per key; without a key every call raises ProviderError("config").
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.coinalyze.net/v1"
RATE_PER_SECOND = 40 / 60


def auth_header(api_key: str | None) -> dict[str, str]:
    return {"api_key": api_key} if api_key else {}


def _usd(flag: bool) -> str:
    return "true" if flag else "false"


class Coinalyze:
    name = "coinalyze"

    def __init__(self, http: Http, *, has_key: bool) -> None:
        self.http = http
        self.has_key = has_key

    async def _history(self, endpoint: str, symbol: str, interval: str, start: int, end: int, **extra: Any) -> Fetched:
        """Rows of `history` for one symbol from `/{endpoint}` (interval is the API's name: daily, 1hour, ...)."""
        if not self.has_key:
            raise ProviderError(self.name, "config", "COINALYZE_API_KEY is not set (free key at coinalyze.net)")
        params: dict[str, Any] = {"symbols": symbol, "interval": interval, "from": int(start), "to": int(end), **extra}
        f = await self.http.get_json(f"/{endpoint}", params=params, ttl=300, tool=endpoint)
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", f"unexpected response shape at {endpoint}")
        for item in f.data:
            if isinstance(item, dict) and item.get("symbol") == symbol and isinstance(item.get("history"), list):
                return Fetched(item["history"], f.evidence)
        return Fetched([], f.evidence)

    async def liquidation_history(self, symbol: str, interval: str, start: int, end: int, *, convert_to_usd: bool = True) -> Fetched:
        """Rows `[{"t","l","s"}]` for one symbol."""
        return await self._history("liquidation-history", symbol, interval, start, end, convert_to_usd=_usd(convert_to_usd))

    async def open_interest_history(self, symbol: str, interval: str, start: int, end: int, *, convert_to_usd: bool = True) -> Fetched:
        """OHLC rows `[{"t","o","h","l","c"}]` of open interest (base coins, or USD with convert_to_usd)."""
        return await self._history("open-interest-history", symbol, interval, start, end, convert_to_usd=_usd(convert_to_usd))

    async def funding_rate_history(self, symbol: str, interval: str, start: int, end: int) -> Fetched:
        """OHLC rows `[{"t","o","h","l","c"}]` of the funding rate, in PERCENT per funding interval (as the API sends)."""
        return await self._history("funding-rate-history", symbol, interval, start, end)

    async def long_short_ratio_history(self, symbol: str, interval: str, start: int, end: int) -> Fetched:
        """Rows `[{"t","r","l","s"}]`: r = long/short accounts, l and s in percent of accounts."""
        return await self._history("long-short-ratio-history", symbol, interval, start, end)
