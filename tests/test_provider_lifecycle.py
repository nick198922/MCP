"""Guard for CLAUDE.md invariant 7: providers survive aclose().

The MCP lifespan runs once per connection and ends with `Runtime.aclose()`, while the Runtime (and its providers) lives
on for the next connection. So every provider must work again after a close: `Http` builds a fresh httpx client on next
use, `Exchanges` builds fresh ccxt instances. This test, without network, builds one Runtime with fake transports, calls
one method per provider client (every `Http` a provider holds is used at least once), closes the runtime, checks the
close really closed those clients, and repeats the calls over two more sessions. Each repeat must succeed and reach the
transport again (caches are off: cache_scale=0).

KNOWN: providers that do not survive a close and are not fixed yet (each one also in docs/BACKLOG.md); a stale entry
fails the test, so a fixed provider leaves the list.
"""

from __future__ import annotations

import io
import zipfile
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import httpx

from conftest import NOW, FakeEx, hl_payload
from tradedesk.config import Settings
from tradedesk.core.envelope import Fetched
from tradedesk.core.http import Http
from tradedesk.providers.exchanges import Exchanges
from tradedesk.providers.thegraph import POLYMARKET_SUBGRAPH_ID
from tradedesk.runtime import Runtime

SESSIONS = 3  # first connection + two reconnects, each ending with rt.aclose()
ADDR = "0x" + "1" * 40

# Violations found and not fixed yet (also listed in docs/BACKLOG.md). Format: {"runtime attribute": "what is wrong"}.
KNOWN: dict[str, str] = {}


def _kline_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("BTCUSDT-1h-2026-10-10.csv", "1791590400000,82000,82100,81900,82050,10,1791593999999,820500,100,5,410000,0\n")
    return buf.getvalue()


S3_LISTING = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>'
    b"<CommonPrefixes><Prefix>data/futures/um/monthly/klines/BTCUSDT/</Prefix></CommonPrefixes></ListBucketResult>"
)

# Minimal valid answer per Http client name (the names Runtime gives its clients). Shapes only, no numbers under test.
BODIES: dict[str, Any] = {
    "deribit": {"jsonrpc": "2.0", "result": []},
    "deribit_history": {"jsonrpc": "2.0", "result": []},
    "hyperliquid": hl_payload(),
    "hyperliquid:stats": {"leaderboardRows": []},
    "binance_futures": [],
    "bybit": {"retCode": 0, "retMsg": "OK", "result": {"list": []}},
    "binance_spot": [],
    "coinbase": [],
    "binance_vision": _kline_zip(),
    "binance_vision:s3": S3_LISTING,
    "alternative_me": {"data": []},
    "forexfactory": [],
    "dexscreener": {"pairs": None},
    "goplus": {"code": 1, "message": "OK", "result": {ADDR: {"is_honeypot": "0"}}},
    "defillama": [],
    "defillama:stablecoins": {"peggedAssets": []},
    "pendle": {"markets": []},
    "coinalyze": [],
    "polymarket:gamma": {"id": "516700"},
    "polymarket:clob": {"market": "TOK_YES", "bids": [], "asks": []},
    "polymarket:data": {"data": [], "pagination": {"next_cursor": None}},
    "thegraph": {"data": {"_meta": {}}},
    "coingecko": {"data": {}},
}

Call = Callable[[Runtime], Awaitable[Fetched]]

# One call per Http client of each provider (by Runtime attribute), so every client goes through a close.
CALLS: dict[str, list[Call]] = {
    "deribit": [lambda rt: rt.deribit.book_summary("BTC", "option")],
    "deribit_history": [lambda rt: rt.deribit_history.book_summary("BTC", "option")],
    "hyperliquid": [lambda rt: rt.hyperliquid.asset_contexts(), lambda rt: rt.hyperliquid.leaderboard()],
    "binance_futures": [lambda rt: rt.binance_futures.premium_index()],
    "bybit": [lambda rt: rt.bybit.long_short_account_ratio("BTCUSDT", "1h", 10)],
    "binance_spot": [lambda rt: rt.binance_spot.klines("BTCUSDT", "1h", 1791590400000, 1791593999999)],
    "coinbase": [lambda rt: rt.coinbase.candles("BTC-USD", 3600, 1791590400000, 1791593999999)],
    "binance_vision": [
        lambda rt: rt.binance_vision.list_dir("data/futures/um/monthly/klines/"),
        lambda rt: rt.binance_vision.um_klines_daily("BTCUSDT", "1h", "2026-10-10"),
    ],
    "alternative_me": [lambda rt: rt.alternative_me.fear_greed(1)],
    "forexfactory": [lambda rt: rt.forexfactory.this_week()],
    "dexscreener": [lambda rt: rt.dexscreener.token_pairs([ADDR])],
    "goplus": [lambda rt: rt.goplus.token_security("1", ADDR)],
    "defillama": [lambda rt: rt.defillama.chain_tvl("Ethereum"), lambda rt: rt.defillama.stablecoin_assets()],
    "pendle": [lambda rt: rt.pendle.markets()],
    "coinalyze": [lambda rt: rt.coinalyze.funding_rate_history("BTCUSDT_PERP.A", "daily", 1791504000, 1791590400)],
    "polymarket": [
        lambda rt: rt.polymarket.market("516700"),
        lambda rt: rt.polymarket.book("TOK_YES"),
        lambda rt: rt.polymarket.positions(ADDR),
    ],
    "thegraph": [lambda rt: rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)],
    "coingecko": [lambda rt: rt.coingecko.global_market()],
    "exchanges": [lambda rt: rt.exchanges.ticker("binanceusdm", "BTC/USDT:USDT")],
}


class ClosableEx(FakeEx):
    """FakeEx that, like a real ccxt instance after close(), refuses every call once closed."""

    def __init__(self, ex_id: str, made: list[ClosableEx]) -> None:
        super().__init__(ex_id)
        self.closed = False
        made.append(self)

    async def fetch_ticker(self, symbol):
        if self.closed:
            raise RuntimeError("Session is closed")
        return await super().fetch_ticker(symbol)

    async def close(self):
        self.closed = True


class _Routes(dict):
    """Gives every Http client the Runtime builds a fake transport that answers BODIES[name] and counts hits."""

    def __init__(self, hits: Counter) -> None:
        super().__init__({"*": None})  # non-empty: Runtime treats an empty mapping as "no transports"
        self._hits = hits

    def get(self, key, default=None):
        def handler(req: httpx.Request) -> httpx.Response:
            self._hits[key] += 1
            body = BODIES.get(key)
            if body is None:
                return httpx.Response(404, text=f"no body for {key}")
            if isinstance(body, bytes):
                return httpx.Response(200, content=body)
            return httpx.Response(200, json=body)

        return httpx.MockTransport(handler)


def providers(rt: Runtime) -> dict[str, Any]:
    """Runtime attributes that are providers: they hold an Http client, or are the ccxt wrapper."""
    return {
        name: obj
        for name, obj in vars(rt).items()
        if isinstance(obj, Exchanges) or any(isinstance(v, Http) for v in getattr(obj, "__dict__", {}).values())
    }


def clients(obj: Any) -> list[Http]:
    return [v for v in vars(obj).values() if isinstance(v, Http)]


async def test_every_provider_survives_runtime_aclose():
    hits: Counter = Counter()
    made: list[ClosableEx] = []
    leaks: list[str] = []
    settings = Settings(
        http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, coinalyze_api_key="lifecycle-test", graph_api_key="lifecycle-test"
    )
    rt = Runtime(settings, transports=_Routes(hits), exchange_factory=lambda ex_id: ClosableEx(ex_id, made))
    rt.now = lambda: NOW
    provs = providers(rt)
    assert set(provs) == set(CALLS), "every provider in Runtime needs a lifecycle call in CALLS (and nothing else)"

    async def no_real_network(transport, request):
        leaks.append(f"{request.method} {request.url.host}{request.url.path}")
        raise httpx.ConnectError("real network is off in this test", request=request)

    first_failures: dict[str, str] = {}
    violations: dict[str, str] = {}
    unclosed: dict[str, list[str]] = {}
    with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", no_real_network):
        for session in range(SESSIONS):
            for name, calls in CALLS.items():
                for i, call in enumerate(calls):
                    try:
                        res = await call(rt)
                        assert isinstance(res, Fetched), f"returned {type(res).__name__}"
                    except Exception as e:  # noqa: BLE001 - collected and reported below
                        bucket = first_failures if session == 0 else violations
                        bucket.setdefault(name, f"session {session + 1}, call {i}: {type(e).__name__}: {str(e)[:200]}")
            live = {h.provider: h._client_obj for obj in provs.values() for h in clients(obj)}
            await rt.aclose()
            for name, obj in provs.items():
                open_ = [h.provider for h in clients(obj) if live[h.provider] is None or not live[h.provider].is_closed]
                if open_:
                    unclosed[name] = open_

    assert leaks == []  # every request went to the fake transports
    assert first_failures == {}, "the first call must work: fix the fake body in BODIES or the call in CALLS"
    new = {name: why for name, why in violations.items() if name not in KNOWN}
    assert new == {}, "fix these providers (lazy client re-creation), or (if not clear-cut) list them in KNOWN and docs/BACKLOG.md"
    assert set(KNOWN) - set(violations) == set(), "these providers survive aclose() now: remove them from KNOWN"
    # Every client was really used and closed in each session, so the repeats prove recreation, not an untouched client.
    assert unclosed == {}, "Runtime.aclose() did not close these clients (or a call did not reach them)"
    names = {h.provider for name, obj in provs.items() if name not in KNOWN for h in clients(obj)}
    assert {n: hits[n] for n in names if hits[n] < SESSIONS} == {}, "each client must reach its transport in every session"
    if "exchanges" not in KNOWN:
        assert len(made) == SESSIONS, "Exchanges must build a fresh ccxt instance after each aclose()"
        assert all(ex.closed for ex in made), "Runtime.aclose() must close every ccxt instance"
