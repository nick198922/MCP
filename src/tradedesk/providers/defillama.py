"""DefiLlama public API (no key): chain TVL, DEX volume and fees.

* `/v2/historicalChainTvl/{chain}` -> `[{"date": unix, "tvl": usd}, ...]` (daily)
* `/overview/dexs/{chain}` and `/overview/fees/{chain}` -> object with `total24h`, `total7d`, `total30d`, ...
  Chain names are case-sensitive upstream ("Solana", "Ethereum", "BSC"). The overview bodies carry hundreds of
  KB of protocol breakdowns that we do not use; only the scalar totals are kept (and chart payloads are not requested).

Stablecoins live on a separate host, `https://stablecoins.llama.fi` (public, no key):

* `/stablecoins?includePrices=true` -> `{"peggedAssets": [{"id", "name", "symbol", "pegType", "pegMechanism", "price",
  "circulating": {"peggedUSD": n}, "circulatingPrevDay": {...}, "circulatingPrevWeek": {...},
  "circulatingPrevMonth": {...}, "chainCirculating": {...}, "chains": [...]}, ...]}`. Per-chain breakdowns are dropped.
* `/stablecoincharts/all` -> `[{"date": "unix seconds as a string", "totalCirculating": {peg: n},
  "totalCirculatingUSD": {peg: usd}, ...}, ...]` (daily). Only `date` and `totalCirculatingUSD` are kept.
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.llama.fi"
STABLECOINS_URL = "https://stablecoins.llama.fi"

_ASSET_KEEP = (
    "id",
    "name",
    "symbol",
    "pegType",
    "pegMechanism",
    "price",
    "circulating",
    "circulatingPrevDay",
    "circulatingPrevWeek",
    "circulatingPrevMonth",
)

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
    # DefiLlama's own changes, in PERCENT rounded to 2 dp (not decimals): kept only to cross-check our recomputation
    # (change_1d, change_7d, change_1m ~ our change_30d, change_7dover7d ~ our week_over_week).
    "change_1d",
    "change_7d",
    "change_1m",
    "change_7dover7d",
)


class DefiLlama:
    name = "defillama"

    def __init__(self, http: Http, stablecoins: Http | None = None) -> None:
        self.http = http
        self.stablecoins = stablecoins  # stablecoins.llama.fi host

    async def aclose(self) -> None:
        await self.http.aclose()
        if self.stablecoins is not None:
            await self.stablecoins.aclose()

    def _stables(self) -> Http:
        if self.stablecoins is None:
            raise ProviderError(self.name, "config", "stablecoins client not configured")
        return self.stablecoins

    async def stablecoin_assets(self) -> Fetched:
        """Current circulating supply per stablecoin with DefiLlama's own day / week / month-ago snapshots."""
        f = await self._stables().get_json("/stablecoins", params={"includePrices": "true"}, ttl=600, tool="stablecoins", args={})
        rows = f.data.get("peggedAssets") if isinstance(f.data, dict) else None
        if not isinstance(rows, list):
            raise ProviderError(f.evidence.provider, "backend_error", "unexpected response shape at stablecoins")
        slim = [{k: r.get(k) for k in _ASSET_KEEP} for r in rows if isinstance(r, dict)]
        return Fetched(slim, f.evidence)

    async def stablecoin_chart_all(self) -> Fetched:
        """Daily total stablecoin supply across all stablecoins (USD value per peg type)."""
        f = await self._stables().get_json("/stablecoincharts/all", ttl=600, tool="stablecoincharts/all", args={})
        if not isinstance(f.data, list):
            raise ProviderError(f.evidence.provider, "backend_error", "unexpected response shape at stablecoincharts/all")
        slim = [{"date": r.get("date"), "totalCirculatingUSD": r.get("totalCirculatingUSD")} for r in f.data if isinstance(r, dict)]
        return Fetched(slim, f.evidence)

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
