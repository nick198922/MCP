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
from .providers import binance_vision as vision_mod
from .providers import bybit as bybit_mod
from .providers import coinalyze as coinalyze_mod
from .providers import coingecko as cg_mod
from .providers import defillama as llama_mod
from .providers import deribit as deribit_mod
from .providers import dexscreener as dexs_mod
from .providers import forexfactory as ff_mod
from .providers import hyperliquid as hl_mod
from .providers import pendle as pendle_mod
from .providers import polymarket as poly_mod
from .providers import upbit as upbit_mod
from .providers.alternative_me import AlternativeMe
from .providers.binance_futures import BinanceFutures
from .providers.binance_vision import BinanceVision
from .providers.bybit import Bybit
from .providers.coinalyze import Coinalyze
from .providers.coingecko import CoinGecko
from .providers.defillama import DefiLlama
from .providers.deribit import Deribit
from .providers.dexscreener import DexScreener
from .providers.exchanges import Exchanges
from .providers.forexfactory import ForexFactory
from .providers.hyperliquid import Hyperliquid
from .providers.pendle import Pendle
from .providers.polymarket import Polymarket
from .providers.upbit import Upbit
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

        def http(name: str, base: str, rate: float, burst: int, retries: int | None = None) -> Http:
            return Http(
                name,
                base,
                rate=rate,
                burst=burst,
                timeout=s.http_timeout,
                retries=s.http_retries if retries is None else retries,
                cache=self.cache,
                cache_scale=s.cache_scale,
                health=self.health,
                transport=tr.get(name),
            )

        self.deribit = Deribit(http("deribit", deribit_mod.BASE_URL, rate=8, burst=10))
        # Archive host for historical trades of expired instruments (research scripts, not tools).
        self.deribit_history = Deribit(http("deribit_history", deribit_mod.HISTORY_URL, rate=5, burst=5))
        self.hyperliquid = Hyperliquid(
            http("hyperliquid", hl_mod.BASE_URL, rate=5, burst=8), stats=http("hyperliquid:stats", hl_mod.STATS_URL, rate=1, burst=2)
        )
        self.binance_futures = BinanceFutures(http("binance_futures", binance_mod.BASE_URL, rate=20, burst=40))
        self.bybit = Bybit(http("bybit", bybit_mod.BASE_URL, rate=20, burst=40))
        # Public kline archive incl. delisted symbols (research scripts, not tools).
        self.binance_vision = BinanceVision(
            files=http("binance_vision", vision_mod.FILES_URL, rate=20, burst=40),
            listing=http("binance_vision:s3", vision_mod.LISTING_URL, rate=5, burst=10),
        )
        self.alternative_me = AlternativeMe(http("alternative_me", altme_mod.BASE_URL, rate=2, burst=4))
        self.forexfactory = ForexFactory(http("forexfactory", ff_mod.BASE_URL, rate=0.5, burst=2))
        # Cloudflare in front of Upbit answers 429 / 1015 above ~1 request per 6 s, and from a datacenter IP to a
        # non-browser client on most cache misses even below that (measured from the server 2026-10-19: ~30-50 % of
        # single requests pass), so Upbit gets more retries; the token bucket still spaces them 6 s apart.
        self.upbit = Upbit(
            http("upbit", upbit_mod.BASE_URL, rate=upbit_mod.RATE_PER_SECOND, burst=1, retries=max(s.http_retries, upbit_mod.RETRIES))
        )
        self.dexscreener = DexScreener(http("dexscreener", dexs_mod.BASE_URL, rate=4, burst=8))
        self.defillama = DefiLlama(http("defillama", llama_mod.BASE_URL, rate=4, burst=8))
        self.pendle = Pendle(http("pendle", pendle_mod.BASE_URL, rate=2, burst=4))
        self.coinalyze = Coinalyze(
            Http(
                "coinalyze",
                coinalyze_mod.BASE_URL,
                rate=coinalyze_mod.RATE_PER_SECOND,
                burst=5,
                timeout=s.http_timeout,
                retries=s.http_retries,
                headers=coinalyze_mod.auth_header(s.coinalyze_api_key),
                cache=self.cache,
                cache_scale=s.cache_scale,
                health=self.health,
                transport=tr.get("coinalyze"),
            ),
            has_key=bool(s.coinalyze_api_key),
        )
        self.polymarket = Polymarket(
            gamma=http("polymarket:gamma", poly_mod.GAMMA_URL, rate=5, burst=10),
            clob=http("polymarket:clob", poly_mod.CLOB_URL, rate=5, burst=10),
            # Trade prints (research scripts, not tools).
            data=http("polymarket:data", poly_mod.DATA_URL, rate=3, burst=5),
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
        await self.deribit_history.http.aclose()
        await self.hyperliquid.aclose()
        await self.binance_futures.http.aclose()
        await self.bybit.http.aclose()
        await self.binance_vision.aclose()
        await self.alternative_me.http.aclose()
        await self.forexfactory.http.aclose()
        await self.upbit.http.aclose()
        await self.dexscreener.http.aclose()
        await self.defillama.http.aclose()
        await self.pendle.http.aclose()
        await self.coinalyze.http.aclose()
        await self.polymarket.aclose()
        await self.coingecko.http.aclose()
        await self.exchanges.aclose()
        if self.store is not None:
            await self.store.aclose()
