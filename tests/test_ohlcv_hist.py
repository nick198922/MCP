from __future__ import annotations

import json

from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.scheduler import _collect_ohlcv_hist, _parse_ohlcv_markets
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store import Store

# ---------- parser ----------


def test_parse_markets_handles_colon_in_symbol():
    out = _parse_ohlcv_markets("binanceusdm:BTC/USDT:USDT:1h, bybit:ETH/USDT:USDT:4h ,  ")
    assert out == [
        ("binanceusdm", "BTC/USDT:USDT", "1h"),
        ("bybit", "ETH/USDT:USDT", "4h"),
    ]


def test_parse_markets_rejects_bad_entries():
    assert _parse_ohlcv_markets("") == []
    assert _parse_ohlcv_markets("binanceusdm:BTC") == []  # no timeframe segment


# ---------- store CRUD ----------


async def test_insert_and_read_ohlcv_round_trip():
    s = Store(":memory:")
    await s.init()
    bars = [[1_700_000_000_000 + i * 3_600_000, 100 + i, 101 + i, 99 + i, 100 + i, 10.0] for i in range(5)]
    assert await s.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "1h", bars) == 5
    rows = await s.ohlcv_range(venue="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h", since_ts=0)
    assert len(rows) == 5 and rows[0]["close"] == 100.0 and rows[-1]["close"] == 104.0
    last = await s.ohlcv_last_ts("binanceusdm", "BTC/USDT:USDT", "1h")
    assert last == bars[-1][0]
    # Idempotent REPLACE: resubmit the same ts with a tweaked close.
    bars[-1][4] = 999.0
    await s.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "1h", [bars[-1]])
    rows = await s.ohlcv_range(venue="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h", since_ts=0)
    assert len(rows) == 5 and rows[-1]["close"] == 999.0
    await s.aclose()


# ---------- collector ----------


async def _rt(state, tmp_path, **overrides) -> Runtime:
    settings = Settings(
        http_retries=1,
        cache_scale=0.0,
        db_path=str(tmp_path / "ohlcv.sqlite"),
        collect_ohlcv_hist_enabled=True,
        collect_ohlcv_hist_markets="binanceusdm:BTC/USDT:USDT:1h",
        collect_ohlcv_hist_backfill_days=1,
        **overrides,
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_collect_ohlcv_hist_writes_rows(state, tmp_path):
    rt = await _rt(state, tmp_path)
    try:
        written = await _collect_ohlcv_hist(rt)
        assert written > 0
        rows = await rt.store.ohlcv_range(venue="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h", since_ts=0)
        assert len(rows) == written
        # Second tick on the same tick-clock has nothing new.
        again = await _collect_ohlcv_hist(rt)
        assert again == 0
    finally:
        await rt.aclose()


# ---------- MCP tool ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_ohlcv_history_tool_returns_stored_bars(state, tmp_path):
    rt = await _rt(state, tmp_path)
    try:
        await _collect_ohlcv_hist(rt)
        srv, _ = build_server(rt)
        env = await _call(srv, "ohlcv_history", venue="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
        d = env["data"]
        assert d["count"] > 0
        assert d["first_ts_ms"] < d["last_ts_ms"]
    finally:
        await rt.aclose()


async def test_ohlcv_history_tool_missing_without_data(state, tmp_path):
    rt = await _rt(state, tmp_path)
    try:
        srv, _ = build_server(rt)
        env = await _call(srv, "ohlcv_history", venue="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h")
        assert env["ok"] is False
        assert any(m["field"] == "ohlcv_hist" for m in env["missing"])
    finally:
        await rt.aclose()
