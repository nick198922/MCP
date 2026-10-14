"""Centralised-exchange data through ccxt (public endpoints only in Phase 0-1)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import ccxt
import ccxt.async_support as ccxt_async

from ..core.cache import TTLCache
from ..core.envelope import Evidence, Fetched
from ..core.errors import ProviderError
from ..core.health import HealthRegistry, redact

# Exchanges we expose for linear USDT perps (unified symbol BASE/USDT:USDT).

# Max funding-history rows each venue returns per request; anything larger is silently capped.
FUNDING_PAGE = {"binanceusdm": 1000, "bybit": 200, "okx": 100}
PERP_EXCHANGES = ("binanceusdm", "bybit", "okx")


def map_ccxt_error(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, (ccxt.RateLimitExceeded, ccxt.DDoSProtection)):
        return "rate_limited", str(exc)
    if isinstance(exc, (ccxt.AuthenticationError, ccxt.PermissionDenied)):
        return "auth", str(exc)
    if isinstance(exc, ccxt.NetworkError):
        return "network", str(exc)
    if isinstance(exc, (ccxt.BadSymbol, ccxt.NotSupported)):
        return "data_unavailable", str(exc)
    if isinstance(exc, ccxt.ExchangeError):
        return "backend_error", str(exc)
    return "unknown", f"{type(exc).__name__}: {exc}"


class Exchanges:
    name = "ccxt"

    def __init__(
        self,
        health: HealthRegistry,
        *,
        cache: TTLCache | None = None,
        cache_scale: float = 1.0,
        factory: Callable[[str], Any] | None = None,
        timeout_ms: int = 10_000,
    ) -> None:
        self._health = health
        self._cache = cache or TTLCache()
        self._scale = cache_scale
        self._factory = factory or self._default_factory
        self._timeout_ms = timeout_ms
        self._ex: dict[str, Any] = {}

    def _default_factory(self, ex_id: str) -> Any:
        cls = getattr(ccxt_async, ex_id, None)
        if cls is None:
            raise ProviderError("ccxt", "config", f"unknown exchange '{ex_id}'")
        return cls({"enableRateLimit": True, "timeout": self._timeout_ms})

    def get(self, ex_id: str) -> Any:
        ex_id = ex_id.lower()
        if ex_id not in self._ex:
            self._ex[ex_id] = self._factory(ex_id)
        return self._ex[ex_id]

    async def aclose(self) -> None:
        exchanges, self._ex = list(self._ex.values()), {}  # next use builds fresh instances
        for ex in exchanges:
            close = getattr(ex, "close", None)
            if close:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - shutting down
                    pass

    async def _call(self, ex_id: str, tool: str, args: dict[str, Any], ttl: float, fn: Callable[[Any], Awaitable[Any]]) -> Fetched:
        label = f"ccxt:{ex_id.lower()}"
        self._health.register(label)
        key = (label, tool, tuple(sorted((k, str(v)) for k, v in args.items())))

        async def fetch() -> Fetched:
            t0 = time.perf_counter()
            try:
                data = await fn(self.get(ex_id))
            except ProviderError as e:
                self._health.error(label, e.reason, e.detail)
                raise ProviderError(label, e.reason, e.detail) from e
            except Exception as e:  # noqa: BLE001 - mapped below
                reason, detail = map_ccxt_error(e)
                self._health.error(label, reason, detail)
                raise ProviderError(label, reason, redact(detail)) from e
            ms = (time.perf_counter() - t0) * 1000
            self._health.ok(label, ms)
            return Fetched(data, Evidence(provider=label, tool=tool, args=args, latency_ms=int(ms)))

        eff = ttl * self._scale
        if eff <= 0:
            return await fetch()
        res, cached = await self._cache.get_or_fetch(key, eff, fetch)
        return Fetched(res.data, res.evidence.model_copy(update={"cached": True})) if cached else res

    async def ticker(self, ex_id: str, symbol: str) -> Fetched:
        return await self._call(ex_id, "fetch_ticker", {"symbol": symbol}, 2, lambda ex: ex.fetch_ticker(symbol))

    async def tickers(self, ex_id: str, symbols: list[str] | None = None) -> Fetched:
        async def go(ex: Any) -> Any:
            has = getattr(ex, "has", {}) or {}
            if has.get("fetchTickers") is False:
                raise ccxt.NotSupported(f"{ex_id} has no fetchTickers")
            return await ex.fetch_tickers(symbols) if symbols else await ex.fetch_tickers()

        a = {"symbols": ",".join(symbols)} if symbols else {}
        return await self._call(ex_id, "fetch_tickers", a, 2, go)

    async def markets(self, ex_id: str) -> Fetched:
        return await self._call(ex_id, "load_markets", {}, 3600, lambda ex: ex.load_markets())

    async def trades(self, ex_id: str, symbol: str, limit: int) -> Fetched:
        a = {"symbol": symbol, "limit": limit}
        return await self._call(ex_id, "fetch_trades", a, 2, lambda ex: ex.fetch_trades(symbol, limit=limit))

    async def ohlcv(
        self, ex_id: str, symbol: str, timeframe: str, limit: int, since_ms: int | None = None, until_ms: int | None = None
    ) -> Fetched:
        """OHLCV bars. With ``since_ms`` the window starts there; ``until_ms`` also bounds it above (ccxt ``until``), so
        venues that would otherwise return the *latest* ``limit`` bars (Bybit sends only ``start``) page deterministically."""
        a = {"symbol": symbol, "timeframe": timeframe, "limit": limit, "since_ms": since_ms}
        if until_ms is not None:
            a["until_ms"] = until_ms
        if since_ms is None:
            return await self._call(ex_id, "fetch_ohlcv", a, 15, lambda ex: ex.fetch_ohlcv(symbol, timeframe, limit=limit))
        params = {"until": until_ms} if until_ms is not None else {}
        if params:
            return await self._call(
                ex_id, "fetch_ohlcv", a, 15, lambda ex: ex.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit, params=params)
            )
        return await self._call(ex_id, "fetch_ohlcv", a, 15, lambda ex: ex.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit))

    async def order_book(self, ex_id: str, symbol: str, limit: int) -> Fetched:
        a = {"symbol": symbol, "limit": limit}
        return await self._call(ex_id, "fetch_order_book", a, 1, lambda ex: ex.fetch_order_book(symbol, limit))

    async def funding_rate(self, ex_id: str, symbol: str) -> Fetched:
        return await self._call(ex_id, "fetch_funding_rate", {"symbol": symbol}, 10, lambda ex: ex.fetch_funding_rate(symbol))

    async def funding_rates(self, ex_id: str) -> Fetched:
        """Current funding for every perp on the venue in one request."""

        async def go(ex: Any) -> Any:
            has = getattr(ex, "has", {}) or {}
            if has.get("fetchFundingRates") is False:
                raise ccxt.NotSupported(f"{ex_id} has no fetchFundingRates")
            return await ex.fetch_funding_rates()

        return await self._call(ex_id, "fetch_funding_rates", {}, 30, go)

    async def funding_rate_history(self, ex_id: str, symbol: str, since_ms: int | None, limit: int) -> Fetched:
        a = {"symbol": symbol, "since_ms": since_ms, "limit": limit}

        async def go(ex: Any) -> Any:
            has = getattr(ex, "has", {}) or {}
            if has.get("fetchFundingRateHistory") is False:
                raise ccxt.NotSupported(f"{ex_id} has no fetchFundingRateHistory")
            return await ex.fetch_funding_rate_history(symbol, since=since_ms, limit=limit)

        return await self._call(ex_id, "fetch_funding_rate_history", a, 60, go)

    async def funding_intervals(self, ex_id: str) -> Fetched:
        """Per-symbol funding interval and rate cap/floor (Binance's bulk funding omits the interval)."""

        async def go(ex: Any) -> Any:
            has = getattr(ex, "has", {}) or {}
            if has.get("fetchFundingIntervals") is False:
                raise ccxt.NotSupported(f"{ex_id} has no fetchFundingIntervals")
            return await ex.fetch_funding_intervals()

        return await self._call(ex_id, "fetch_funding_intervals", {}, 3600, go)

    async def funding_rate_history_range(self, ex_id: str, symbol: str, since_ms: int, until_ms: int, *, max_pages: int = 60) -> Fetched:
        """Funding history from ``since_ms`` to ``until_ms`` walked page by page.

        One request returns at most a venue-specific number of rows, and ccxt sizes Bybit's request window
        from the symbol's *current* interval, so a short page does not mean the history ended: walk forward
        from the newest row seen. An empty window (before a listing) is skipped by ``page`` hours, the
        smallest window a page can cover at a 1h interval, so no rows are jumped over. Returns
        ``{"rows": [...], "complete": bool}``; ``complete`` is False when ``max_pages`` was hit or a later
        page failed (rows fetched so far are kept). A failure on the first page raises.
        """
        page = FUNDING_PAGE.get(ex_id.lower(), 500)
        rows: dict[int, dict[str, Any]] = {}
        since, pages, latency, cached, complete = since_ms, 0, 0, True, True
        while since < until_ms:
            if pages >= max_pages:
                complete = False
                break
            try:
                f = await self.funding_rate_history(ex_id, symbol, since, page)
            except ProviderError:
                if pages == 0:
                    raise
                complete = False
                break
            pages += 1
            latency += f.evidence.latency_ms or 0
            cached = cached and f.evidence.cached
            batch = [x for x in (f.data or []) if x.get("timestamp") is not None]
            if not batch:
                since += page * 3_600_000
                continue
            for x in batch:
                rows[int(x["timestamp"])] = x
            nxt = max(int(x["timestamp"]) for x in batch) + 1
            if nxt <= since:
                break
            since = nxt
        ev = Evidence(
            provider=f"ccxt:{ex_id.lower()}",
            tool="fetch_funding_rate_history",
            args={"symbol": symbol, "since_ms": since_ms, "until_ms": until_ms, "pages": pages},
            latency_ms=latency,
            cached=cached and pages > 0,
        )
        return Fetched({"rows": [rows[k] for k in sorted(rows) if k <= until_ms], "complete": complete}, ev)

    async def open_interest(self, ex_id: str, symbol: str) -> Fetched:
        async def go(ex: Any) -> Any:
            if hasattr(ex, "has") and not ex.has.get("fetchOpenInterest"):
                raise ccxt.NotSupported(f"{ex_id} has no fetchOpenInterest")
            return await ex.fetch_open_interest(symbol)

        return await self._call(ex_id, "fetch_open_interest", {"symbol": symbol}, 10, go)
