"""Item 395 (review docs/reviews/2026-11-19-twin-tool-descriptions.md): unit claims in the descriptions of
perps_open_interest, ohlcv_history and positions_list, each checked against the code that backs it.

Every key phrase is pinned twice: the phrase is in the description, and the code does what the phrase says. If a test
here fails after a logic change, update the description in the same change (the model reads the units from it).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

NOW_MS = int(NOW.timestamp()) * 1000


async def _tool(server, name: str):
    async with Client(server) as c:
        tools = (await c.list_tools()).tools
    return next(t for t in tools if t.name == name)


async def _description(server, name: str) -> str:
    return " ".join((await _tool(server, name)).description.split())


@pytest.fixture
async def store_rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


@asynccontextmanager
async def _session(rt):
    """One MCP connection: the in-memory store is reopened on every connection, so a test makes all calls in one."""
    async with Client(build_server(rt)[0]) as c:

        async def go(tool: str, /, **args) -> dict:
            res = await c.call_tool(tool, args)
            assert not res.is_error, res.content[0].text[:300] if res.content else ""
            data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
            return data["result"] if set(data) == {"result"} else data

        yield go


# ---------- perps_open_interest ----------


async def test_open_interest_description_units(server):
    d = await _description(server, "perps_open_interest")
    for phrase in (
        "base-asset coins on binanceusdm and bybit, but CONTRACTS on okx",  # ccxt okx: openInterestAmount = `oi` (contracts)
        "do not compare oi_base across venues",
        "Hyperliquid open interest in coins; null for Deribit",
        "estimated as oi_base x last price, named in `warnings`",
        "Hyperliquid = coins x mark",
        "Deribit = the perpetual's USD open interest",
        "share is a fraction 0-1 of that total",
        "left out of the total; a failing venue goes to `missing`",
    ):
        assert phrase in d, phrase


async def test_open_interest_deribit_and_hyperliquid_units(server, rt):
    env = await call(server, "perps_open_interest", base="BTC")
    rows = {v["venue"]: v for v in env["data"]["venues"]}
    # Deribit: oi_base null, oi_usd = book_summary open_interest of the PERPETUAL (USD on Deribit's inverse perp).
    perp = next(x for x in (await rt.deribit.book_summary("BTC", "future")).data if x["instrument_name"].endswith("PERPETUAL"))
    assert rows["deribit"]["oi_base"] is None and rows["deribit"]["oi_usd"] == pytest.approx(perp["open_interest"])
    # Hyperliquid: oi_base in coins, oi_usd = coins x mark.
    hl = next(c for c in (await rt.hyperliquid.asset_contexts()).data if c["coin"] == "BTC")
    assert rows["hyperliquid"]["oi_base"] == pytest.approx(hl["open_interest"])
    assert rows["hyperliquid"]["oi_usd"] == pytest.approx(hl["open_interest"] * hl["mark_px"])
    # share: a fraction 0-1 of total_oi_usd.
    shares = [v["share"] for v in env["data"]["venues"]]
    assert all(0 <= s <= 1 for s in shares) and sum(shares) == pytest.approx(1.0, abs=1e-3)


# ---------- ohlcv_history ----------


async def test_ohlcv_history_description_units(server):
    d = await _description(server, "ohlcv_history")
    for phrase in (
        "unix MILLISECONDS",
        "bar ts is Unix epoch milliseconds (UTC) of the bar's open",
        "open/high/low/close are in the symbol's quote currency",
        "volume is ccxt's unified bar volume as stored, in base-asset units",
        "never converted to USD",
        "first_ts_ms / last_ts_ms are the inclusive range of the bars returned (after `limit`",
        "not of everything stored",
    ):
        assert phrase in d, phrase
    assert "inclusive date range of what's actually stored" not in d  # the old wording, wrong with a limit
    params = (await _tool(server, "ohlcv_history")).input_schema["properties"]
    assert "counted from the OLDEST" in params["limit"]["description"]
    assert "inclusive" in params["since_ts_ms"]["description"] and "inclusive" in params["until_ts_ms"]["description"]


async def test_ohlcv_history_range_is_of_returned_bars(store_rt):
    # Volume and prices pass through as stored (the collector stores ccxt's [ts, o, h, l, c, v] rows unconverted).
    bars = [[NOW_MS - (5 - i) * 3_600_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 7.25 + i] for i in range(5)]
    await store_rt.store.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "1h", bars)
    async with _session(store_rt) as go:
        env = await go("ohlcv_history", venue="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h", limit=2)
        d = env["data"]
        assert env["ok"] is True and d["count"] == 2
        # limit counts from the oldest: last_ts_ms is the second stored bar, not the newest stored one.
        assert (d["first_ts_ms"], d["last_ts_ms"]) == (bars[0][0], bars[1][0])
        assert [b["volume"] for b in d["bars"]] == [7.25, 8.25]
        # since / until are inclusive bounds in milliseconds.
        env = await go("ohlcv_history", venue="binanceusdm", symbol="BTC/USDT:USDT", since_ts_ms=bars[1][0], until_ts_ms=bars[3][0])
        assert [b["ts"] for b in env["data"]["bars"]] == [bars[1][0], bars[2][0], bars[3][0]]


# ---------- positions_list ----------


async def test_positions_list_description_units(server):
    d = await _description(server, "positions_list")
    for phrase in (
        "newest-first (by opened_ts)",
        "opened_ts / closed_ts in Unix seconds (UTC)",
        "size in contracts or coins (always > 0, the sign is `side`)",
        "mark in USD per contract / coin",
        "per-contract greeks in the units they were entered with, not converted or checked",
        "delta in coins, i.e. USD per $1 spot move",
        "vega in USD per 1 vol point",
        "theta in USD per calendar day",
        "one whole stored tag, not a substring",
        "ASCII letters case-insensitive",
        "non-ASCII tags such as Cyrillic match",
        "`%` / `_` are literal characters",
    ):
        assert phrase in d, phrase


async def test_positions_list_units_as_stored_and_tag_limits(store_rt):
    async with _session(store_rt) as go:
        greeks = {"delta": 0.42, "gamma": 0.00003, "vega": 61.5, "theta": -38.2}
        await go(
            "positions_add",
            instrument="BTC-27NOV26-90000-C",
            kind="option",
            side="short",
            size=2.0,
            mark=1500.0,
            tags=["Core"],
            opened_ts=1,
            **greeks,
        )
        await go("positions_add", instrument="ETH/USDT:USDT", kind="perp", side="long", size=1.0, tags=["axb", "хедж", "a%b"], opened_ts=2)
        env = await go("positions_list")
        rows = env["data"]["positions"]
        assert [p["opened_ts"] for p in rows] == [2, 1]  # newest first, Unix seconds as entered
        opt = rows[1]
        # greeks, size and side are returned as entered: size stays > 0 and is not signed by side.
        assert {k: opt[k] for k in greeks} == greeks and opt["size"] == 2.0 and opt["side"] == "short"

        async def ids(tag: str) -> list[str]:
            return [p["instrument"] for p in (await go("positions_list", tag=tag))["data"]["positions"]]

        assert await ids("core") == ["BTC-27NOV26-90000-C"]  # ASCII case-insensitive
        assert await ids("co") == []  # one whole tag, not a substring
        # Item 396a: stored \u-escaped by json.dumps, the Cyrillic tag still matches; `%` / `_` are not wildcards.
        assert await ids("хедж") == ["ETH/USDT:USDT"]
        assert await ids("a_b") == []
        assert await ids("a%") == []
        assert await ids("a%b") == ["ETH/USDT:USDT"]
