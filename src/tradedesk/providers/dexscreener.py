"""DexScreener public API (no key): pools that trade given token addresses.

`/latest/dex/tokens/{addresses}` takes up to 30 comma-separated addresses and returns
`{"schemaVersion": ..., "pairs": [...]}` (`pairs` may be null when nothing is found). Public limit is ~300
requests/minute for this route.
"""

from __future__ import annotations

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.dexscreener.com"


class DexScreener:
    name = "dexscreener"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def token_pairs(self, addresses: list[str]) -> Fetched:
        joined = ",".join(addresses)
        f = await self.http.get_json(
            f"/latest/dex/tokens/{joined}",
            ttl=15,
            tool="tokens",
            args={"addresses": addresses},
        )
        body = f.data
        if not isinstance(body, dict):
            raise ProviderError(self.name, "backend_error", "unexpected response shape at /latest/dex/tokens")
        pairs = body.get("pairs")
        if pairs is None:
            pairs = []
        if not isinstance(pairs, list):
            raise ProviderError(self.name, "backend_error", "pairs is not a list at /latest/dex/tokens")
        return Fetched(pairs, f.evidence)
