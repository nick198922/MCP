from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.regime import _ols_slope, classify_regime
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# ---------- pure classifier ----------


def _bars(closes: list[float]) -> list[dict]:
    return [{"ts": 1_700_000_000_000 + i * 3_600_000, "close": c} for i, c in enumerate(closes)]


def test_ols_slope_matches_known_cases():
    # Flat line → slope 0.
    assert _ols_slope([1.0] * 5) == 0.0
    # Perfect ramp y=x → slope 1.
    assert _ols_slope([float(i) for i in range(10)]) == pytest.approx(1.0)


def test_classify_regime_up_trend():
    # Monotone 1 % per bar growth → clear up.
    bars = _bars([100.0 * (1.01**i) for i in range(60)])
    r = classify_regime(bars, lookback=60, trend_threshold=0.5)
    assert r["regime"] == "up"
    assert r["slope_per_bar_log"] > 0
    assert r["window_return_pct"] > 50  # 60 bars at +1% ≈ +82 %


def test_classify_regime_down_trend():
    bars = _bars([100.0 * (0.99**i) for i in range(60)])
    r = classify_regime(bars, lookback=60)
    assert r["regime"] == "down"
    assert r["slope_per_bar_log"] < 0


def test_classify_regime_chop():
    # Oscillating closes around 100 → high vol, near-zero slope → chop.
    bars = _bars([100.0 + (5.0 if i % 2 == 0 else -5.0) for i in range(60)])
    r = classify_regime(bars, lookback=60)
    assert r["regime"] == "chop"
    assert abs(r["slope_per_bar_log"]) < 0.001


def test_classify_regime_handles_short_input():
    assert classify_regime([], lookback=10)["regime"] == "unknown"
    assert classify_regime(_bars([100.0, 101.0]), lookback=10)["regime"] == "unknown"


# ---------- MCP tool ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "regime.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_market_regime_from_store(state, tmp_path):
    rt = await _rt(state, tmp_path)
    # Seed 120 hourly bars rising ~1 % per bar.
    start_ms = int(NOW.timestamp() * 1000) - 120 * 3_600_000
    bars = [[start_ms + i * 3_600_000, 100 * (1.01**i), 101 * (1.01**i), 99 * (1.01**i), 100 * (1.01**i), 1.0] for i in range(120)]
    await rt.store.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "1h", bars)
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "market_regime",
            venue="binanceusdm",
            symbol="BTC/USDT:USDT",
            timeframe="1h",
            lookback_bars=60,
            source="store",
        )
        d = env["data"]
        assert d["regime"] == "up"
        assert d["count"] == 60
        assert d["slope_pct_per_bar"] > 0
    finally:
        await rt.aclose()


async def test_market_regime_missing_store_bars(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "market_regime", venue="binanceusdm", symbol="BTC/USDT:USDT", timeframe="1h", source="store")
        assert env["ok"] is False
    finally:
        await rt.aclose()


async def test_market_regime_live_source(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "market_regime",
            venue="binanceusdm",
            symbol="BTC/USDT:USDT",
            timeframe="1h",
            lookback_bars=50,
            source="live",
        )
        d = env["data"]
        assert d["regime"] in ("up", "down", "chop")
        assert d["count"] >= 3
    finally:
        await rt.aclose()
