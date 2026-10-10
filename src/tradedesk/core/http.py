"""HTTP client with rate limiting, retries, caching and health tracking.

Every provider talks to its API through `Http`, so failures are classified the same way
(see `errors.REASONS`) and every successful call yields an `Evidence` record.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import random
import re
import time
from typing import Any

import httpx

from .cache import TTLCache
from .envelope import Evidence, Fetched
from .errors import ProviderError
from .health import HealthRegistry, redact
from .ratelimit import TokenBucket

_SECRET_KEY = re.compile(r"(?i)(key|token|secret|signature|pass)")
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _clean_args(params: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (params or {}).items() if not _SECRET_KEY.search(str(k))}


class Http:
    def __init__(
        self,
        provider: str,
        base_url: str,
        *,
        rate: float = 5.0,
        burst: int | None = None,
        timeout: float = 10.0,
        retries: int = 2,
        headers: dict[str, str] | None = None,
        cache: TTLCache | None = None,
        cache_scale: float = 1.0,
        health: HealthRegistry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base: float = 0.4,
    ) -> None:
        self.provider = provider
        self._client_args: dict[str, Any] = {
            "base_url": base_url,
            "timeout": timeout,
            "headers": {"User-Agent": "tradedesk-mcp/0.1", **(headers or {})},
            "transport": transport,
        }
        self._client_obj: httpx.AsyncClient | None = None
        self._limiter = TokenBucket(rate, burst)
        self._retries = retries
        self._cache = cache if cache is not None else TTLCache()
        self._scale = cache_scale
        self._health = health
        self._backoff = backoff_base
        if health:
            health.register(provider)

    @property
    def _client(self) -> httpx.AsyncClient:
        """Created on first use and re-created after aclose(), so a server can be restarted in-process
        (MCP sessions come and go; the runtime must survive them)."""
        if self._client_obj is None or self._client_obj.is_closed:
            self._client_obj = httpx.AsyncClient(**self._client_args)
        return self._client_obj

    async def aclose(self) -> None:
        if self._client_obj is not None and not self._client_obj.is_closed:
            await self._client_obj.aclose()
        self._client_obj = None

    def _fail(self, reason: str, detail: str) -> ProviderError:
        detail = redact(detail)
        if self._health:
            self._health.error(self.provider, reason, detail)
        return ProviderError(self.provider, reason, detail)

    async def _once(self, method: str, path: str, params: Any, json: Any) -> tuple[Any, float]:
        last: ProviderError | None = None
        for attempt in range(self._retries + 1):
            await self._limiter.acquire()
            t0 = time.perf_counter()
            try:
                resp = await self._client.request(method, path, params=params, json=json)
            except httpx.TimeoutException as e:
                last = ProviderError(self.provider, "network", f"timeout: {type(e).__name__}")
            except httpx.HTTPError as e:
                last = ProviderError(self.provider, "network", f"{type(e).__name__}: {e}")
            else:
                ms = (time.perf_counter() - t0) * 1000
                code = resp.status_code
                if code < 400:
                    try:
                        return resp.json(), ms
                    except (ValueError, jsonlib.JSONDecodeError):
                        raise self._fail("backend_error", f"non-JSON response ({resp.headers.get('content-type')})")
                snippet = resp.text[:160].replace("\n", " ")
                if code in (401, 403):
                    raise self._fail("auth", f"HTTP {code}: {snippet}")
                if code == 402:
                    raise self._fail("provider_tier", f"HTTP 402: {snippet}")
                if code == 404:
                    raise self._fail("data_unavailable", f"HTTP 404: {snippet}")
                if code not in _RETRY_STATUS:
                    raise self._fail("backend_error", f"HTTP {code}: {snippet}")
                reason = "rate_limited" if code == 429 else "backend_error"
                last = ProviderError(self.provider, reason, f"HTTP {code}: {snippet}")
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and attempt < self._retries:
                    await asyncio.sleep(min(float(retry_after), 5.0))
                    continue
            if attempt < self._retries:
                await asyncio.sleep(min(self._backoff * (2**attempt) + random.random() * 0.1, 5.0))
        assert last is not None
        raise self._fail(last.reason, last.detail)

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        ttl: float = 0.0,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> Fetched:
        key = (self.provider, method, path, tuple(sorted((params or {}).items())), jsonlib.dumps(json, sort_keys=True))

        async def fetch() -> Fetched:
            data, ms = await self._once(method, path, params, json)
            if self._health:
                self._health.ok(self.provider, ms)
            ev = Evidence(
                provider=self.provider,
                tool=tool or path,
                args=args if args is not None else _clean_args(params) | ({"body": json} if json else {}),
                latency_ms=int(ms),
            )
            return Fetched(data, ev)

        eff_ttl = ttl * self._scale
        if eff_ttl <= 0:
            return await fetch()
        fetched, from_cache = await self._cache.get_or_fetch(key, eff_ttl, fetch)
        if from_cache:
            return Fetched(fetched.data, fetched.evidence.model_copy(update={"cached": True}))
        return fetched

    async def get_json(self, path: str, **kw: Any) -> Fetched:
        return await self.request_json("GET", path, **kw)

    async def post_json(self, path: str, **kw: Any) -> Fetched:
        return await self.request_json("POST", path, **kw)
