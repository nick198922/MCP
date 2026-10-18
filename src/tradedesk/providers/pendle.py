"""Pendle public API (api-v2.pendle.finance, no key): fixed-yield (PT) markets.

`GET /core/v1/markets/all?isActive=true` returns `{"markets": [...]}` across all chains; each market has
`pt`/`yt`/`sy` ids of the form `<chainId>-<0xaddress>` and a `details` object (`impliedApy`, `underlyingApy`,
`liquidity`, `totalTvl`, `tradingVolume`, ... all decimals / USD). The list is large (~250 KB), so it is cached.
"""

from __future__ import annotations

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api-v2.pendle.finance"


class Pendle:
    name = "pendle"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def markets(self) -> Fetched:
        f = await self.http.get_json(
            "/core/v1/markets/all",
            params={"isActive": "true"},
            ttl=60,
            tool="markets_all",
            args={"isActive": True},
        )
        body = f.data
        if not isinstance(body, dict):
            raise ProviderError(self.name, "backend_error", "unexpected response shape at /core/v1/markets/all")
        rows = body.get("markets")
        if not isinstance(rows, list):
            raise ProviderError(self.name, "backend_error", "markets is not a list at /core/v1/markets/all")
        return Fetched(rows, f.evidence)
