"""alternative.me Crypto Fear & Greed Index (public, no key).

Single endpoint: `/fng/?limit=N` returns the daily index as a list of
`{"value":"40","value_classification":"Fear","timestamp":"<unix>","time_until_update":"..."}`.
The index is CRYPTO-ONLY (BTC-weighted sentiment composite) and updates once per day around 00:00 UTC.
"""

from __future__ import annotations

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.alternative.me"


class AlternativeMe:
    name = "alternative_me"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def fear_greed(self, limit: int) -> Fetched:
        f = await self.http.get_json(
            "/fng/",
            params={"limit": limit, "format": "json"},
            ttl=600,
            tool="fng",
            args={"limit": limit},
        )
        body = f.data
        if not isinstance(body, dict):
            raise ProviderError(self.name, "backend_error", "unexpected response shape at /fng/")
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ProviderError(self.name, "backend_error", "missing data array at /fng/")
        return Fetched(rows, f.evidence)
