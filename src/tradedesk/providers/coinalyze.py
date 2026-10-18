"""Coinalyze API (free key): derivatives history per exchange (symbol suffix = exchange code; '.A' = Binance).

* `/liquidation-history?symbols=BTCUSDT_PERP.A&interval=daily&from=&to=&convert_to_usd=` ->
  `[{"symbol": ..., "history": [{"t": unix_s, "l": long, "s": short}, ...]}]` (`[]` when no data in range).
* The key goes in the `api_key` request header only (never in the URL, params, cache key or evidence).
  Free tier is ~40 requests/minute per key; without a key every call raises ProviderError("config").
"""

from __future__ import annotations

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.coinalyze.net/v1"
RATE_PER_SECOND = 40 / 60


def auth_header(api_key: str | None) -> dict[str, str]:
    return {"api_key": api_key} if api_key else {}


class Coinalyze:
    name = "coinalyze"

    def __init__(self, http: Http, *, has_key: bool) -> None:
        self.http = http
        self.has_key = has_key

    async def liquidation_history(self, symbol: str, interval: str, start: int, end: int, *, convert_to_usd: bool = True) -> Fetched:
        """Rows `[{"t","l","s"}]` for one symbol (interval is the API's name: daily, 1hour, ...)."""
        if not self.has_key:
            raise ProviderError(self.name, "config", "COINALYZE_API_KEY is not set (free key at coinalyze.net)")
        f = await self.http.get_json(
            "/liquidation-history",
            params={
                "symbols": symbol,
                "interval": interval,
                "from": int(start),
                "to": int(end),
                "convert_to_usd": "true" if convert_to_usd else "false",
            },
            ttl=300,
            tool="liquidation-history",
        )
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", "unexpected response shape at liquidation-history")
        for item in f.data:
            if isinstance(item, dict) and item.get("symbol") == symbol and isinstance(item.get("history"), list):
                return Fetched(item["history"], f.evidence)
        return Fetched([], f.evidence)
