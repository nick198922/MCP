from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.providers.liquidations import parse_binance_force_order
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


# ---------- parser ----------


def _event(**overrides):
    base = {
        "e": "forceOrder",
        "E": 1_568_014_460_893,
        "o": {
            "s": "BTCUSDT",
            "S": "SELL",
            "o": "LIMIT",
            "f": "IOC",
            "q": "0.014",
            "p": "9910",
            "ap": "9910",
            "X": "FILLED",
            "l": "0.014",
            "z": "0.014",
            "T": 1_568_014_460_893,
        },
    }
    base["o"].update(overrides)
    return base


def test_parse_sell_is_long_liquidation():
    row = parse_binance_force_order(_event())
    assert row == {
        "ts": 1_568_014_460,
        "venue": "binanceusdm",
        "symbol": "BTCUSDT",
        "side": "sell",
        "qty": pytest.approx(0.014),
        "price": pytest.approx(9910.0),
        "notional_usd": pytest.approx(0.014 * 9910.0),
    }


def test_parse_buy_maps_to_short_liquidation():
    row = parse_binance_force_order(_event(S="BUY"))
    assert row["side"] == "buy"


def test_parse_rejects_bad_events():
    assert parse_binance_force_order({"e": "trade"}) is None
    assert parse_binance_force_order({"e": "forceOrder", "o": {}}) is None
    # qty = 0
    bad = _event(l="0")
    bad["o"]["z"] = "0"
    bad["o"]["q"] = "0"
    assert parse_binance_force_order(bad) is None
    # unknown side
    assert parse_binance_force_order(_event(S="SIDE")) is None


# ---------- MCP summary tool ----------


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "liq.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_liquidations_summary_empty_window(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "liquidations_summary", window_minutes=10)
        assert env["ok"] is True
        d = env["data"]
        assert d["count"] == 0 and d["total_notional_usd"] == 0.0
    finally:
        await rt.aclose()


async def test_liquidations_summary_aggregates(state, tmp_path):
    rt = await _rt(state, tmp_path)
    now_ts = int(NOW.timestamp())
    # Three liquidations in the trailing 10 minutes: two longs flushed, one short squeezed.
    await rt.store.insert_liquidations(
        [
            {
                "ts": now_ts - 60,
                "venue": "binanceusdm",
                "symbol": "BTCUSDT",
                "side": "sell",
                "qty": 1.0,
                "price": 60_000.0,
                "notional_usd": 60_000.0,
            },
            {
                "ts": now_ts - 120,
                "venue": "binanceusdm",
                "symbol": "BTCUSDT",
                "side": "sell",
                "qty": 2.0,
                "price": 60_000.0,
                "notional_usd": 120_000.0,
            },
            {
                "ts": now_ts - 90,
                "venue": "binanceusdm",
                "symbol": "ETHUSDT",
                "side": "buy",
                "qty": 10.0,
                "price": 3_000.0,
                "notional_usd": 30_000.0,
            },
        ]
    )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "liquidations_summary", window_minutes=10, top_n=5)
        d = env["data"]
        assert d["count"] == 3
        assert d["total_notional_usd"] == 210_000
        assert d["sell_notional_usd"] == 180_000
        assert d["buy_notional_usd"] == 30_000
        # imbalance = (buy - sell) / total = -150000 / 210000 ≈ -0.7143
        assert d["imbalance"] == pytest.approx(-0.7143, abs=1e-4)
        syms = {s["symbol"] for s in d["per_symbol"]}
        assert syms == {"BTCUSDT", "ETHUSDT"}
        # Top entry should be the biggest sell print.
        assert d["top"][0]["symbol"] == "BTCUSDT" and d["top"][0]["notional_usd"] == 120_000
    finally:
        await rt.aclose()


async def test_liquidations_summary_filters_by_symbol(state, tmp_path):
    rt = await _rt(state, tmp_path)
    now_ts = int(NOW.timestamp())
    await rt.store.insert_liquidations(
        [
            {
                "ts": now_ts - 30,
                "venue": "binanceusdm",
                "symbol": "BTCUSDT",
                "side": "sell",
                "qty": 1.0,
                "price": 60_000.0,
                "notional_usd": 60_000.0,
            },
            {
                "ts": now_ts - 30,
                "venue": "binanceusdm",
                "symbol": "ETHUSDT",
                "side": "buy",
                "qty": 10.0,
                "price": 3_000.0,
                "notional_usd": 30_000.0,
            },
        ]
    )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "liquidations_summary", window_minutes=10, symbol="BTCUSDT")
        d = env["data"]
        assert d["count"] == 1 and d["sell_notional_usd"] == 60_000
    finally:
        await rt.aclose()
