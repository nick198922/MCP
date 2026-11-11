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
import json
import logging
import random
import zipfile
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, hl_payload, make_transports
from tradedesk.config import Settings
from tradedesk.core.envelope import Fetched
from tradedesk.core.http import Http
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.providers.exchanges import Exchanges
from tradedesk.providers.thegraph import POLYMARKET_SUBGRAPH_ID
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

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
    "wikimedia": {"items": []},
    "wikimedia:enwiki": {"query": {"pages": [{"pageid": 1, "ns": 0, "title": "Bitcoin"}]}},
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
    "wikimedia": [lambda rt: rt.wikimedia.pageviews_daily("Bitcoin", "20261001", "20261002"), lambda rt: rt.wikimedia.page("Bitcoin")],
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
    # The GoPlus limit (0.2 req/s, burst 2) made the third session wait 5 s; the test checks clients, not pacing.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
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


async def test_store_survives_runtime_aclose(tmp_path):
    # The store is not a provider, but the same lifespan closes it: server.lifespan calls store.init() at the start of
    # every connection and Runtime.aclose() at the end. Each session must write again, and see what earlier ones wrote.
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / "lifecycle.sqlite")))
    assert rt.store is not None
    for session in range(SESSIONS):
        await rt.store.init()  # as server.lifespan does
        row = {"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0001, "interval_hours": 8.0, "annualized": 0.1095}
        assert await rt.store.insert_funding(1_791_590_400 + 3600 * session, [row]) == 1
        assert await rt.store.table_count("funding_hist") == session + 1  # earlier sessions' rows are still there
        old = rt.store.conn
        await rt.aclose()
        with pytest.raises(ValueError):  # aiosqlite: "no active connection", the connection was really closed
            await old.execute("SELECT 1")
        with pytest.raises(RuntimeError):  # and the store knows it: no silent use of a closed connection
            await rt.store.table_count("funding_hist")


FUNDING_ROW = {"venue": "binanceusdm", "base": "BTC", "rate_per_interval": 0.0001, "interval_hours": 8.0, "annualized": 0.1095}


async def test_runtime_without_store_closes_twice():
    # db_path="" means no store (Runtime builds Store only for a non-empty path); aclose() must not trip over None.
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=""))
    assert rt.store is None
    await rt.aclose()
    await rt.aclose()


async def test_store_aclose_is_idempotent(tmp_path):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / "twice.sqlite")))
    assert rt.store is not None
    await rt.aclose()  # a lifespan that failed before store.init(): nothing open, still no error
    await rt.store.init()
    await rt.aclose()
    await rt.aclose()  # second close of an already closed store
    with pytest.raises(RuntimeError):  # Store.conn refuses once closed
        _ = rt.store.conn


async def test_new_runtime_reopens_same_db_file(tmp_path):
    # A restart builds a fresh Runtime on the same file: the closed store must leave it usable (no lock, migrations done).
    path = str(tmp_path / "reopen.sqlite")
    first = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=path))
    assert first.store is not None
    await first.store.init()
    assert await first.store.insert_funding(1_791_590_400, [FUNDING_ROW]) == 1
    version = await first.store.version()
    await first.aclose()

    second = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=path))
    assert second.store is not None
    await second.store.init()
    try:
        assert await second.store.version() == version > 0
        assert await second.store.table_count("funding_hist") == 1  # the first runtime's row survived the close
        assert await second.store.insert_funding(1_791_594_000, [FUNDING_ROW]) == 1  # and the file takes writes
        assert await second.store.table_count("funding_hist") == 2
    finally:
        await second.aclose()


def _lifespan_runtime(db_path: str) -> Runtime:
    # Collector on, so the lifespan would start the scheduler on a healthy store; fake transports, no real network.
    settings = Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=db_path, collector_enabled=True)
    rt = Runtime(settings, transports=make_transports(State()), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


def _payload(res) -> dict:
    assert not res.is_error, res.content[0].text[:300] if res.content else ""
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


async def test_lifespan_survives_corrupt_store(tmp_path, caplog):
    # Item 222: a DB file that is not SQLite at all made `store.init()` raise in the lifespan, so the connection failed
    # and the session had no tools. Now the lifespan logs it, starts no collector and the market tools still answer.
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(random.Random(222).randbytes(64 * 1024))
    rt = _lifespan_runtime(str(path))
    srv, _ = build_server(rt)
    with caplog.at_level(logging.ERROR, logger="tradedesk.store"):
        async with Client(srv) as c:
            assert rt.scheduler is None  # every collector job writes to the store
            ticker = _payload(await c.call_tool("market_ticker", {"exchange": "binanceusdm", "symbol": "BTC/USDT:USDT"}))
            journal = _payload(await c.call_tool("journal_list", {}))
    assert ticker["ok"] is True and ticker["data"]["last"] == 82750.0
    assert journal["ok"] is False
    assert [m for m in journal["missing"] if m["field"] == "store" and "file is not a database" in m["detail"]]
    assert any("store init failed" in r.getMessage() and "DatabaseError" in r.getMessage() for r in caplog.records)
    with pytest.raises(RuntimeError):  # finally still ran rt.aclose(); the store was never left half-open
        _ = rt.store.conn


async def test_lifespan_healthy_store_starts_collector(tmp_path):
    rt = _lifespan_runtime(str(tmp_path / "healthy.sqlite"))
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        assert rt.scheduler is not None and rt.scheduler.jobs  # as before item 222
        journal = _payload(await c.call_tool("journal_list", {}))
    assert journal["ok"] is True and journal["data"]["count"] == 0
    assert not [m for m in journal["missing"] if m["field"] == "store"]
