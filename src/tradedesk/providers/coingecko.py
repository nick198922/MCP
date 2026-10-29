"""CoinGecko public REST API.

Free (unauthenticated) tier is capped around ~30 requests/minute. A demo API key
raises that; a paid Pro key uses a different base URL and header name.

* demo / none → BASE = `https://api.coingecko.com/api/v3`, header `x-cg-demo-api-key`
* pro         → BASE = `https://pro-api.coingecko.com/api/v3`, header `x-cg-pro-api-key`
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

FREE_URL = "https://api.coingecko.com/api/v3"
PRO_URL = "https://pro-api.coingecko.com/api/v3"


def base_url(plan: str) -> str:
    return PRO_URL if plan == "pro" else FREE_URL


def auth_header(plan: str, api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"x-cg-pro-api-key" if plan == "pro" else "x-cg-demo-api-key": api_key}


class CoinGecko:
    name = "coingecko"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def coin(self, coin_id: str) -> Fetched:
        """Full coin card: market data, supply, ATH/ATL."""
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        }
        f = await self.http.get_json(f"/coins/{coin_id}", params=params, ttl=60, tool=f"coins/{coin_id}", args=params)
        if not isinstance(f.data, dict):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /coins/{id}")
        return f

    async def global_market(self) -> Fetched:
        f = await self.http.get_json("/global", params={}, ttl=120, tool="global", args={})
        if not isinstance(f.data, dict):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /global")
        inner = f.data.get("data")
        if not isinstance(inner, dict):
            raise ProviderError(self.name, "backend_error", "missing data at /global")
        return Fetched(inner, f.evidence)

    async def coins_markets(self, vs_currency: str, per_page: int, page: int) -> Fetched:
        params: dict[str, Any] = {
            "vs_currency": vs_currency,
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false",
            "price_change_percentage": "24h,7d,30d",
        }
        f = await self.http.get_json("/coins/markets", params=params, ttl=60, tool="coins/markets", args=params)
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", "unexpected shape at /coins/markets")
        return f

    async def search_trending(self) -> Fetched:
        """``GET /search/trending`` (free tier): the trending coins list, ``{"coins": [{"item": {...}}, ...], "nfts": [...],
        "categories": [...]}``. Only the top-level shape is checked here (``coins`` must be a list); the items are parsed
        defensively by `analytics.coingecko_trending.snapshot_rows`. Returns the whole object."""
        f = await self.http.get_json("/search/trending", params={}, ttl=60, tool="search/trending", args={})
        if not isinstance(f.data, dict) or not isinstance(f.data.get("coins"), list):
            raise ProviderError(self.name, "backend_error", "unexpected /search/trending shape (no coins list)")
        return f
