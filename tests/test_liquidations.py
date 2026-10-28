from __future__ import annotations

import dataclasses
import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.providers.liquidations import (
    parse_binance_force_order,
    parse_bybit_liquidation,
    parse_okx_liquidation,
)
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


def test_parse_bybit_snapshot_yields_rows():
    event = {
        "topic": "allLiquidation.BTCUSDT",
        "type": "snapshot",
        "ts": 1_703_485_267_706,
        "data": [
            {"T": 1_703_485_267_706, "s": "BTCUSDT", "S": "Sell", "v": "0.03", "p": "40000"},
            {"T": 1_703_485_267_800, "s": "BTCUSDT", "S": "Buy", "v": "0.01", "p": "40010"},
        ],
    }
    rows = parse_bybit_liquidation(event)
    assert [r["side"] for r in rows] == ["sell", "buy"]
    assert all(r["venue"] == "bybit" and r["symbol"] == "BTCUSDT" for r in rows)
    assert rows[0]["notional_usd"] == pytest.approx(0.03 * 40000.0)


def test_parse_bybit_ignores_other_topics():
    assert parse_bybit_liquidation({"topic": "trade.BTCUSDT", "data": [{}]}) == []
    assert parse_bybit_liquidation({"op": "subscribe", "success": True}) == []


def test_parse_okx_liquidation_yields_rows():
    event = {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [
            {
                "instType": "SWAP",
                "instId": "BTC-USDT-SWAP",
                "details": [
                    {"side": "sell", "sz": "0.5", "bkPx": "40000", "posSide": "long", "ts": "1703485267706"},
                    {"side": "buy", "sz": "0.1", "bkPx": "40020", "posSide": "short", "ts": "1703485267800"},
                ],
            }
        ],
    }
    rows = parse_okx_liquidation(event)
    assert len(rows) == 2
    assert rows[0]["venue"] == "okx" and rows[0]["symbol"] == "BTC-USDT-SWAP"
    assert rows[0]["side"] == "sell" and rows[1]["side"] == "buy"
    assert rows[0]["notional_usd"] == pytest.approx(0.5 * 40000.0)


def test_parse_okx_ignores_other_channels():
    assert parse_okx_liquidation({"arg": {"channel": "trades"}, "data": [{}]}) == []


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


# ---------- collector jobs ----------


def test_enabling_liquidations_without_venues_starts_binance_only(state, tmp_path, monkeypatch):
    # The Bybit (side) and OKX (contracts read as coins) parsers are known to be wrong (docs/BACKLOG.md): a bare
    # switch-on must not start them.
    monkeypatch.delenv("TRADEDESK_COLLECT_LIQUIDATIONS_VENUES", raising=False)
    monkeypatch.setenv("TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED", "1")
    s = Settings.from_env()
    assert Settings().collect_liquidations_venues == "binance" and s.collect_liquidations_venues == "binance"
    rt = Runtime(
        dataclasses.replace(s, collector_enabled=True, db_path=str(tmp_path / "liq.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    sched = build_default_scheduler(rt)
    assert sched is not None
    assert sorted(j.name for j in sched.jobs if j.name.startswith("liquidations")) == ["liquidations:binance"]


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
        assert any("lower bound" in w for w in env["warnings"])  # Binance: one liquidation per symbol per second
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
