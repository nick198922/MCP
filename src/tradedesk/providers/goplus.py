"""GoPlus Security public API (free tier, no key): token contract security flags.

`GET /api/v1/token_security/{chain_id}?contract_addresses={address}` returns
`{"code": 1, "message": "OK", "result": {"<lower-case address>": {...flags...}}}`. Flags are strings
("1"/"0", empty when GoPlus could not tell), taxes and holder shares are decimal strings (0.05 = 5 %).
`code` 1 is a complete answer, 2 a partial one; anything else is an error even with HTTP 200 (4029 = request
limit). An empty `result` means GoPlus has nothing for that token (seen live 2026-10-23 for PEPE on chain 1).
Only EVM chains go through this route (Solana has a different endpoint and shape).

Caching: GoPlus reports errors such as 4029 inside an HTTP 200 body, so the generic `Http` cache would keep an
error answer for its whole TTL and health would show the provider as ok. The request therefore goes through
`Http` uncached, and this class caches only successful answers itself; non-success codes go through the `Http` body
check, so health records them once, as errors (not as a success plus an error).
"""

from __future__ import annotations

from typing import Any

from ..core.cache import TTLCache
from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.health import HealthRegistry
from ..core.http import Http

BASE_URL = "https://api.gopluslabs.io"
OK_CODES = (1, 2)  # 1 = complete, 2 = partial data
RATE_LIMIT_CODE = 4029
TTL_SECONDS = 60.0


def _code(body: dict[str, Any]) -> int | None:
    c = body.get("code")
    if isinstance(c, bool):
        return None
    try:
        return int(c)
    except (TypeError, ValueError):
        return None


def _body_check(body: Any) -> tuple[str, str] | None:
    """`Http` body check: a non-success code (or shape) in an HTTP 200 answer is a failure, counted once in health."""
    if not isinstance(body, dict):
        return "backend_error", "unexpected response shape at /token_security"
    code = _code(body)
    if code not in OK_CODES:
        # `message` is third-party text: never echoed, only the numeric code.
        return ("rate_limited" if code == RATE_LIMIT_CODE else "backend_error"), f"GoPlus code {code}"
    if not isinstance(body.get("result"), dict):
        return "backend_error", "result is not an object at /token_security"
    return None


class GoPlus:
    name = "goplus"

    def __init__(self, http: Http, *, health: HealthRegistry | None = None, ttl: float = TTL_SECONDS) -> None:
        self.http = http
        self._health = health  # kept for the constructor contract; failures reach health through Http (see _body_check)
        self._ttl = ttl
        self._cache = TTLCache(max_items=512)

    async def _fetch(self, chain_id: str, addr: str) -> Fetched:
        f = await self.http.get_json(
            f"/api/v1/token_security/{chain_id}",
            params={"contract_addresses": addr},
            ttl=0,  # never cache in Http: an HTTP 200 body may carry an error code (see module docstring)
            tool="token_security",
            args={"chain_id": chain_id, "address": addr},
            check=_body_check,
        )
        body = f.data
        code = _code(body)
        result = body["result"]
        token = next((v for k, v in result.items() if isinstance(k, str) and k.lower() == addr), None)
        if not isinstance(token, dict) or not token:
            raise ProviderError(self.name, "data_unavailable", "GoPlus returned no data for this token on this chain")
        return Fetched({"code": code, "token": token}, f.evidence)

    async def token_security(self, chain_id: str, address: str) -> Fetched:
        """Flags for one EVM token. Returns Fetched({"code": int, "token": dict}, evidence).

        `address` must already be validated; it is sent and looked up lower-case. Only successful answers
        (code 1 or 2 with data) are cached.
        """
        addr = address.lower()
        if self._ttl <= 0:
            return await self._fetch(chain_id, addr)
        fetched, from_cache = await self._cache.get_or_fetch((chain_id, addr), self._ttl, lambda: self._fetch(chain_id, addr))
        if from_cache:
            return Fetched(fetched.data, fetched.evidence.model_copy(update={"cached": True}))
        return fetched
