"""DefiLlama public API (no key): chain TVL, DEX volume and fees.

* `/v2/historicalChainTvl/{chain}` -> `[{"date": unix, "tvl": usd}, ...]` (daily)
* `/overview/dexs/{chain}` and `/overview/fees/{chain}` -> object with `total24h`, `total7d`, `total30d`, ...
  Chain names are case-sensitive upstream ("Solana", "Ethereum", "BSC"). The overview bodies carry hundreds of
  KB of protocol breakdowns that we do not use; only the scalar totals are kept (and chart payloads are not requested).
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.llama.fi"

_KEEP = (
    "chain",
    "total24h",
    "total48hto24h",
    "total7d",
    "total14dto7d",
    "total30d",
    "total60dto30d",
    "total7DaysAgo",
    "total30DaysAgo",
)


class DefiLlama:
    name = "defillama"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def chain_tvl(self, chain: str) -> Fetched:
        f = await self.http.get_json(f"/v2/historicalChainTvl/{chain}", ttl=300, tool="historicalChainTvl", args={"chain": chain})
        if not isinstance(f.data, list):
            raise ProviderError(self.name, "backend_error", "unexpected response shape at historicalChainTvl")
        return f

    async def _overview(self, kind: str, chain: str) -> Fetched:
        f = await self.http.get_json(
            f"/overview/{kind}/{chain}",
            params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
            ttl=300,
            tool=f"overview_{kind}",
            args={"chain": chain},
        )
        if not isinstance(f.data, dict):
            raise ProviderError(self.name, "backend_error", f"unexpected response shape at overview/{kind}")
        slim: dict[str, Any] = {k: f.data.get(k) for k in _KEEP}
        return Fetched(slim, f.evidence)

    async def dex_overview(self, chain: str) -> Fetched:
        return await self._overview("dexs", chain)

    async def fees_overview(self, chain: str) -> Fetched:
        return await self._overview("fees", chain)
