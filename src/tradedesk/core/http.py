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
# Failures that happen before a single byte of the request leaves this machine (DNS / TCP / TLS, no free pooled
# connection): the server surely never saw the request, so repeating it cannot duplicate a side effect.
_UNSENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# For APIs that report failures inside an HTTP 200 body (GraphQL `errors`, GoPlus codes): a pure function of the decoded
# body returning None when it is usable, or (reason, detail) when it is a failure. A failure is recorded in health once,
# as an error (never also as a success), its detail is scrubbed like any other, and it is never cached.
BodyCheck = Callable[[Any], tuple[str, str] | None]


class _Absent:
    """A cached HTTP 404 (`data_unavailable`): the resource is not there, which is an answer like any other. Kept for
    the call's own TTL so callers on a timer (snapshot every 10 min, ForexFactory's next week until it is published)
    do not hit the host again and count a new failure each time. Other failures are never cached."""

    __slots__ = ("detail",)

    def __init__(self, detail: str) -> None:
        self.detail = detail


def _retry_after(resp: httpx.Response) -> str | None:
    """Seconds to wait from a 429: the Retry-After header, else Telegram's body field `parameters.retry_after`."""
    header = resp.headers.get("retry-after")
    if header:
        return header
    try:
        value = resp.json().get("parameters", {}).get("retry_after")
    except (ValueError, AttributeError):
        return None
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _clean_args(params: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (params or {}).items() if not _SECRET_KEY.search(str(k))}


def _header_secrets(headers: dict[str, str] | None) -> tuple[str, ...]:
    """Credentials this client sends in its own headers (`api_key`, `x-cg-demo-api-key`, `Authorization: Bearer <k>`),
    so a venue echoing one back in an error body is scrubbed even when the provider passed no `secrets=`. Only the last
    word of the value (the token after a scheme); shorter than 6 chars is skipped, it would garble ordinary text."""
    out = []
    for name, value in (headers or {}).items():
        if _SECRET_KEY.search(name) or name.lower() == "authorization":
            token = str(value).rsplit(" ", 1)[-1]
            if len(token) >= 6:
                out.append(token)
    return tuple(out)


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
        retry_unsent_only: bool = False,
    ) -> None:
        self.provider = provider
        # Exact strings (API keys sent in headers) scrubbed from every error detail, before health and ProviderError see it.
        self._secrets = tuple(s for s in (*secrets, *_header_secrets(headers)) if s)
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
        # For non-idempotent calls (Telegram sendMessage): retry only when the server surely did not act on the request,
        # i.e. a connection that never opened (_UNSENT) or an HTTP 429. A read timeout, a dropped connection or a 5xx
        # after the body went out may mean it was already processed, so those fail at once instead of repeating it.
        self._unsent_only = retry_unsent_only
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
                if self._unsent_only and not isinstance(e, _UNSENT):
                    break
            except httpx.HTTPError as e:
                last = ProviderError(self.provider, "network", f"{type(e).__name__}: {e}")
                if self._unsent_only and not isinstance(e, _UNSENT):
                    break
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
                if self._unsent_only and code != 429:
                    break  # a 5xx may come after the server already acted on the request
                retry_after = _retry_after(resp)
                if retry_after and retry_after.isdigit() and attempt < self._retries:
                    await asyncio.sleep(min(float(retry_after), 5.0))
                    continue
            if attempt < self._retries:
                # Jitter scales with the base (0.1 s at the default 0.4), so backoff_base=0 means no wait at all.
                await asyncio.sleep(min(self._backoff * (2**attempt) + random.random() * self._backoff / 4, 5.0))
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
        eff_ttl = ttl * self._scale

        async def fetch() -> Fetched | _Absent:
            try:
                data, ms = await self._once(method, path, params, json)
            except ProviderError as e:
                if e.reason == "data_unavailable" and eff_ttl > 0:
                    return _Absent(e.detail)  # already recorded in health once by _once
                raise
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

        if eff_ttl <= 0:
            fetched, from_cache = await fetch(), False  # without a TTL fetch() re-raises a 404 instead of _Absent
        else:
            fetched, from_cache = await self._cache.get_or_fetch(key, eff_ttl, fetch)
        if isinstance(fetched, _Absent):
            raise ProviderError(self.provider, "data_unavailable", fetched.detail)
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
