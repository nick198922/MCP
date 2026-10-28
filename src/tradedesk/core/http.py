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
from collections.abc import Callable
from typing import Any

import httpx

from .cache import TTLCache
from .envelope import Evidence, Fetched
from .errors import ProviderError
from .health import HealthRegistry, redact
from .ratelimit import TokenBucket

_SECRET_KEY = re.compile(r"(?i)(key|token|secret|signature|pass)")
_RETRY_STATUS = {429, 500, 502, 503, 504}

# For APIs that report failures inside an HTTP 200 body (GraphQL `errors`, GoPlus codes): a pure function of the decoded
# body returning None when it is usable, or (reason, detail) when it is a failure. A failure is recorded in health once,
# as an error (never also as a success), its detail is scrubbed like any other, and it is never cached.
BodyCheck = Callable[[Any], tuple[str, str] | None]


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
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.provider = provider
        # Exact strings (API keys sent in headers) scrubbed from every error detail, before health and ProviderError see it.
        self._secrets = tuple(s for s in secrets if s)
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

    def _unsecret(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def scrub(self, text: str) -> str:
        """Error text without this client's secrets and without key=value pairs (see `health.redact`), max 200 chars.
        Secrets go first: a length cut before it could leave a prefix of a secret behind."""
        return redact(self._unsecret(text))

    def _fail(self, reason: str, detail: str) -> ProviderError:
        detail = self.scrub(detail)
        if self._health:
            self._health.error(self.provider, reason, detail)
        return ProviderError(self.provider, reason, detail)

    async def _once(self, method: str, path: str, params: Any, json: Any, *, raw: bool = False) -> tuple[Any, float]:
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
                    if raw:
                        return resp.content, ms
                    try:
                        return resp.json(), ms
                    except (ValueError, jsonlib.JSONDecodeError):
                        raise self._fail("backend_error", f"non-JSON response ({resp.headers.get('content-type')})") from None
                snippet = self._unsecret(resp.text)[:160].replace("\n", " ")
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
        check: BodyCheck | None = None,
    ) -> Fetched:
        key = (self.provider, method, path, tuple(sorted((params or {}).items())), jsonlib.dumps(json, sort_keys=True))

        async def fetch() -> Fetched:
            data, ms = await self._once(method, path, params, json)
            bad = check(data) if check is not None else None
            if bad is not None:
                raise self._fail(*bad)
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

    async def get_bytes(self, path: str, *, params: dict[str, Any] | None = None, tool: str | None = None) -> Fetched:
        """Raw response body (archives, XML listings). Same limiter, retries, error mapping and health as JSON calls;
        never cached in memory (payloads can be large; callers keep their own file cache)."""
        data, ms = await self._once("GET", path, params, None, raw=True)
        if self._health:
            self._health.ok(self.provider, ms)
        return Fetched(data, Evidence(provider=self.provider, tool=tool or path, args=_clean_args(params), latency_ms=int(ms)))
