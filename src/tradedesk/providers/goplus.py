"""GoPlus Security public API (free tier, no key): token contract security flags.

`GET /api/v1/token_security/{chain_id}?contract_addresses={address}` returns
`{"code": 1, "message": "OK", "result": {"<lower-case address>": {...flags...}}}`. Flags are strings
("1"/"0", empty when GoPlus could not tell), taxes and holder shares are decimal strings (0.05 = 5 %).
`code` 1 is a complete answer, 2 a partial one; anything else is an error even with HTTP 200 (4029 = request
limit). An empty `result` means GoPlus has nothing for that address on that chain (live 2026-10-23 for a mistyped
PEPE address, `...ddbf47...` instead of `...ddbe47...`; the real PEPE answers in full, live 2026-11-20).

Solana: `GET /api/v1/solana/token_security?contract_addresses={mint}` (free, no key; live 2026-11-19) answers in the
same envelope, keyed by the mint address exactly as sent (base58 is case-sensitive, so it is never lower-cased), with
another schema: authorities as `{"authority": [...], "status": "0"/"1"}` (mintable, freezable, closable,
balance_mutable_authority, *_upgradable), `transfer_fee`, `transfer_hook`, `default_account_state`,
`non_transferable`, `holders` (by `account`), `dex` (pools with `tvl`, `type`, `burn_percent` in percent 0-100) and
`metadata`. Code 7012 ("Not fungible spl token address") comes for unknown or non-fungible mints (live 2026-11-19 for
a random address and for a mistyped BONK mint; the real BONK mint answers in full, live 2026-11-20), so it is read as
"GoPlus has no data" (data_unavailable), not as a provider failure.

Caching: GoPlus reports errors such as 4029 inside an HTTP 200 body, so the generic `Http` cache would keep an
error answer for its whole TTL and health would show the provider as ok. The request therefore goes through
`Http` uncached, and this class caches only successful answers itself; non-success codes go through the `Http` body
check, so health records them once, as errors (not as a success plus an error).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..core.cache import TTLCache
from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.health import HealthRegistry
from ..core.http import Http

BASE_URL = "https://api.gopluslabs.io"
OK_CODES = (1, 2)  # 1 = complete, 2 = partial data
RATE_LIMIT_CODE = 4029
SOLANA_NO_TOKEN_CODE = 7012  # "Not fungible spl token address" (live 2026-11-19: random and mistyped-BONK addresses)
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


def _solana_body_check(body: Any) -> tuple[str, str] | None:
    """As `_body_check`, but code 7012 (GoPlus does not know the mint as a fungible SPL token) is a valid answer
    without data: the provider is healthy, the tool reports data_unavailable."""
    if isinstance(body, dict) and _code(body) == SOLANA_NO_TOKEN_CODE:
        return None
    return _body_check(body)


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

    async def _fetch_solana(self, mint: str) -> Fetched:
        f = await self.http.get_json(
            "/api/v1/solana/token_security",
            params={"contract_addresses": mint},
            ttl=0,  # see _fetch
            tool="solana_token_security",
            args={"chain": "solana", "address": mint},
            check=_solana_body_check,
        )
        body = f.data
        code = _code(body)
        if code == SOLANA_NO_TOKEN_CODE:
            raise ProviderError(self.name, "data_unavailable", "GoPlus does not know this mint as a fungible SPL token (code 7012)")
        token = body["result"].get(mint)  # keyed by the mint exactly as sent (base58 is case-sensitive)
        if not isinstance(token, dict) or not token:
            raise ProviderError(self.name, "data_unavailable", "GoPlus returned no data for this Solana token")
        return Fetched({"code": code, "token": token}, f.evidence)

    async def _cached(self, key: tuple[str, str], fetch: Callable[[], Awaitable[Fetched]]) -> Fetched:
        if self._ttl <= 0:
            return await fetch()
        fetched, from_cache = await self._cache.get_or_fetch(key, self._ttl, fetch)
        if from_cache:
            return Fetched(fetched.data, fetched.evidence.model_copy(update={"cached": True}))
        return fetched

    async def token_security(self, chain_id: str, address: str) -> Fetched:
        """Flags for one EVM token. Returns Fetched({"code": int, "token": dict}, evidence).

        `address` must already be validated; it is sent and looked up lower-case. Only successful answers
        (code 1 or 2 with data) are cached.
        """
        addr = address.lower()
        return await self._cached((chain_id, addr), lambda: self._fetch(chain_id, addr))

    async def solana_token_security(self, mint: str) -> Fetched:
        """Security fields for one SPL token mint. Returns Fetched({"code": int, "token": dict}, evidence).

        `mint` must already be validated base58; it is sent and looked up exactly (case-sensitive). Only successful
        answers are cached; code 7012 and an empty result raise data_unavailable.
        """
        return await self._cached(("solana", mint), lambda: self._fetch_solana(mint))
