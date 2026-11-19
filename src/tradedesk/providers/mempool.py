"""mempool.space public REST API (Bitcoin mainnet, no key).

Endpoints (shapes checked live on 2026-11-19, sample in tests/fixtures/mempool_space_2026-11-19.json):
- `/api/v1/fees/recommended` -> {fastestFee, halfHourFee, hourFee, economyFee, minimumFee} in sat/vB;
- `/api/mempool` -> {count, vsize (vB), total_fee (sats), fee_histogram: [[fee rate sat/vB, vsize vB], ...]};
- `/api/blocks/tip/height` -> a bare integer; `/api/v1/blocks` -> the latest 15 blocks, newest first, with `extras`;
- `/api/v1/mining/hashrate/3d` -> {hashrates: [{timestamp, avgHashrate H/s}], currentHashrate, currentDifficulty};
- `/api/address/{address}` -> {address, chain_stats, mempool_stats}, sums in sats (HTTP 400 for an invalid address).
mempool.space does not publish a fixed request limit for the public API (it answers 429 when hit), so the runtime keeps
a slow bucket. Text fields (pool names, coinbase tags) are untrusted third-party data.
"""

from __future__ import annotations

from typing import Any

from ..analytics.bitcoin import valid_btc_address
from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://mempool.space"


class Mempool:
    name = "mempool"

    def __init__(self, http: Http) -> None:
        self.http = http

    def _shape(self, f: Fetched, typ: type | tuple[type, ...], path: str) -> Fetched:
        if not isinstance(f.data, typ) or isinstance(f.data, bool):
            raise ProviderError(self.name, "backend_error", f"unexpected response shape at {path}")
        return f

    async def _get(self, path: str, typ: type | tuple[type, ...], ttl: float, args: dict[str, Any] | None = None) -> Fetched:
        f = await self.http.get_json(path, ttl=ttl, tool=path, args=args or {})
        return self._shape(f, typ, path)

    async def fees_recommended(self) -> Fetched:
        return await self._get("/api/v1/fees/recommended", dict, ttl=30)

    async def mempool(self) -> Fetched:
        return await self._get("/api/mempool", dict, ttl=30)

    async def tip_height(self) -> Fetched:
        f = await self._get("/api/blocks/tip/height", int, ttl=15)
        if not 0 <= f.data < 2**63:
            raise ProviderError(self.name, "backend_error", "tip height out of range at /api/blocks/tip/height")
        return f

    async def blocks(self) -> Fetched:
        f = await self._get("/api/v1/blocks", list, ttl=15)
        if not f.data:
            raise ProviderError(self.name, "data_unavailable", "no blocks at /api/v1/blocks")
        return f

    async def hashrate_3d(self) -> Fetched:
        return await self._get("/api/v1/mining/hashrate/3d", dict, ttl=600)

    async def address(self, address: str) -> Fetched:
        # The address becomes a URL path segment: re-checked here even though the tool validates it first.
        addr, err = valid_btc_address(address)
        if addr is None:
            raise ProviderError(self.name, "unknown", f"invalid address: {err}")
        return await self._get(f"/api/address/{addr}", dict, ttl=60, args={"address": addr})
