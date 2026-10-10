"""Wires settings, cache, health and providers together."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from .config import Settings
from .core.cache import TTLCache
from .core.envelope import utcnow
from .core.health import HealthRegistry
from .core.http import Http
from .providers import alternative_me as altme_mod
from .providers import binance_futures as binance_mod
from .providers import bybit as bybit_mod
from .providers import coingecko as cg_mod
from .providers import deribit as deribit_mod
from .providers import hyperliquid as hl_mod
from .providers import polymarket as poly_mod
from .providers.alternative_me import AlternativeMe
from .providers.binance_futures import BinanceFutures
from .providers.bybit import Bybit
from .providers.coingecko import CoinGecko
from .providers.deribit import Deribit
from .providers.exchanges import Exchanges
from .providers.hyperliquid import Hyperliquid
from .providers.polymarket import Polymarket
from .store import Store


class Runtime:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transports: dict[str, httpx.AsyncBaseTransport] | None = None,
        exchange_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env()
        s = self.settings
        self.now: Callable[[], datetime] = utcnow  # overridable clock (tests, replay)
        self.health = HealthRegistry()
        self.cache = TTLCache()
        tr = transports or {}

        def http(name: str, base: str, rate: float, burst: int) -> Http:
            return Http(
                name,
                base,
                rate=rate,
                burst=burst,
                timeout=s.http_timeout,
                retries=s.http_retries,
                cache=self.cache,
                cache_scale=s.cache_scale,
                health=self.health,
                transport=tr.get(name),
            )

        self.deribit = Deribit(http("deribit", deribit_mod.BASE_URL, rate=8, burst=10))
        self.hyperliquid = Hyperliquid(http("hyperliquid", hl_mod.BASE_URL, rate=5, burst=8))
        self.binance_futures = BinanceFutures(http("binance_futures", binance_mod.BASE_URL, rate=20, burst=40))
        self.bybit = Bybit(http("bybit", bybit_mod.BASE_URL, rate=20, burst=40))
        self.alternative_me = AlternativeMe(http("alternative_me", altme_mod.BASE_URL, rate=2, burst=4))
        self.polymarket = Polymarket(
            gamma=http("polymarket:gamma", poly_mod.GAMMA_URL, rate=5, burst=10),
            clob=http("polymarket:clob", poly_mod.CLOB_URL, rate=5, burst=10),
        )
        self.store: Store | None = Store(s.db_path) if s.db_path else None
        self.scheduler: Any | None = None  # built after providers below (see server.lifespan)
        cg_rate = 5.0 if (s.coingecko_api_key or s.coingecko_plan == "pro") else 0.5
        self.coingecko = CoinGecko(
            Http(
                "coingecko",
                cg_mod.base_url(s.coingecko_plan),
                rate=cg_rate,
                burst=max(2, int(cg_rate * 2)),
                timeout=s.http_timeout,
                retries=s.http_retries,
                headers=cg_mod.auth_header(s.coingecko_plan, s.coingecko_api_key),
                cache=self.cache,
                cache_scale=s.cache_scale,
                health=self.health,
                transport=tr.get("coingecko"),
            )
        )
        self.exchanges = Exchanges(
            self.health,
            cache=self.cache,
            cache_scale=s.cache_scale,
            factory=exchange_factory,
            timeout_ms=int(s.http_timeout * 1000),
        )

    async def aclose(self) -> None:
        await self.deribit.http.aclose()
        await self.hyperliquid.http.aclose()
        await self.binance_futures.http.aclose()
        await self.bybit.http.aclose()
        await self.alternative_me.http.aclose()
        await self.polymarket.aclose()
        await self.coingecko.http.aclose()
        await self.exchanges.aclose()
        if self.store is not None:
            await self.store.aclose()
