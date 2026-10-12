from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.event_study import (
    MEASURES,
    summarise_events,
    windowed_measure,
)
from tradedesk.analytics.event_study import (
    event_study as pure_event_study,
)
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


def _bar(ts_ms: int, close: float, hl_delta: float = 0.5) -> dict:
    return {
        "ts": ts_ms,
        "open": close,
        "high": close + hl_delta,
        "low": close - hl_delta,
        "close": close,
        "volume": 1.0,
    }


def _hourly_series(start_ms: int, closes: list[float]) -> list[dict]:
    return [_bar(start_ms + i * 3_600_000, c) for i, c in enumerate(closes)]


# ---------- pure ----------


def test_windowed_measure_direction():
    bars = _hourly_series(1_700_000_000_000, [100.0, 101.0, 102.0, 103.0, 104.0])
    ev_ts = 1_700_000_000_000 + 1 * 3_600_000  # the second bar is t0
    v = windowed_measure(bars, ev_ts, before_ms=3_600_000, after_ms=2 * 3_600_000, measure="direction")
    assert v == 1.0  # price was higher after
    down = _hourly_series(1_700_000_000_000, [100.0, 100.0, 99.0, 98.0, 97.0])
    v2 = windowed_measure(down, ev_ts, before_ms=3_600_000, after_ms=2 * 3_600_000, measure="direction")
    assert v2 == 0.0


def test_windowed_measure_max_up_and_dd():
    bars = _hourly_series(1_700_000_000_000, [100.0, 100.0, 105.0, 95.0, 100.0])
    ev_ts = 1_700_000_000_000 + 1 * 3_600_000
    up = windowed_measure(bars, ev_ts, before_ms=0, after_ms=3 * 3_600_000, measure="max_up")
    dd = windowed_measure(bars, ev_ts, before_ms=0, after_ms=3 * 3_600_000, measure="max_dd")
    assert up == pytest.approx((105.5 / 100.0) - 1.0, rel=1e-6)  # high = close+0.5
    assert dd == pytest.approx((94.5 / 100.0) - 1.0, rel=1e-6)


def test_windowed_measure_returns_none_outside_series():
    bars = _hourly_series(1_700_000_000_000, [100.0, 100.0, 100.0])
    too_early = 1_700_000_000_000 - 10 * 3_600_000
    assert windowed_measure(bars, too_early, before_ms=3_600_000, after_ms=3_600_000, measure="direction") is None


def test_summarise_events_hit_rate_and_flags():
    s = summarise_events([1.0, 0.0, 1.0, 1.0, 1.0], measure="direction")
    assert s["n_sample"] == 5 and s["hit_rate"] == pytest.approx(0.8) and s["small_sample_flag"] is True
    s2 = summarise_events([], measure="direction")
    assert s2["n_sample"] == 0 and s2["hit_rate"] is None and s2["small_sample_flag"] is True


def test_summarise_events_percentiles():
    s = summarise_events([-2.0, -1.0, 0.0, 1.0, 2.0], measure="close_vs_t0")
    assert s["median"] == pytest.approx(0.0)
    assert s["p25"] == pytest.approx(-1.0)
    assert s["p75"] == pytest.approx(1.0)
    assert s["hit_rate"] is None  # only reported for 'direction'


def test_pure_event_study_counts_evaluated_vs_requested():
    bars = _hourly_series(1_700_000_000_000, [100.0] * 10)
    ev_in = 1_700_000_000_000 + 3 * 3_600_000
    ev_out = 1_600_000_000_000  # outside
    res = pure_event_study(bars, [ev_in, ev_out], before_minutes=60, after_minutes=120, measure="direction")
    assert res["n_events_requested"] == 2
    assert res["n_events_evaluated"] == 1


# ---------- MCP tool ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_event_study_mcp_end_to_end(state, tmp_path):
    """Seed a few hundred rising BTC bars + three FOMC-like events, then run
    event_study('direction') with a 1h/2h window and expect hit_rate=1.0."""
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "es.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()

    # 500 hourly bars, monotonically rising.
    start_ms = int(NOW.timestamp() * 1000) - 500 * 3_600_000
    bars_list = [[start_ms + i * 3_600_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, 1.0] for i in range(500)]
    await rt.store.insert_ohlcv_batch("binanceusdm", "BTC/USDT:USDT", "1h", bars_list)

    # Three event timestamps inside the series.
    for i in (100, 200, 300):
        ev_ts_sec = (start_ms + i * 3_600_000) // 1000
        await rt.store.event_insert(ts=ev_ts_sec, kind="fomc", label=f"Synthetic FOMC #{i}", impact="high")

    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "event_study",
            venue="binanceusdm",
            symbol="BTC/USDT:USDT",
            timeframe="1h",
            event_kind="fomc",
            measure="direction",
            before_minutes=60,
            after_minutes=120,
        )
        d = env["data"]
        assert d["stats"]["n_sample"] == 3
        assert d["stats"]["hit_rate"] == 1.0
        assert d["stats"]["small_sample_flag"] is True
        assert len(d["per_event"]) == 3 and all(pe["value"] == 1.0 for pe in d["per_event"])
    finally:
        await rt.aclose()


async def test_event_study_requires_ohlcv_hist(state, tmp_path):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "es2.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    await rt.store.init()
    await rt.store.event_insert(ts=int(NOW.timestamp()), kind="fomc", label="X", impact="high")
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "event_study",
            venue="binanceusdm",
            symbol="BTC/USDT:USDT",
            timeframe="1h",
            event_kind="fomc",
        )
        assert env["ok"] is False
        assert any(m["field"] == "ohlcv_hist" for m in env["missing"])
    finally:
        await rt.aclose()


def test_measures_list_is_exported():
    assert set(MEASURES) == {"direction", "close_vs_t0", "max_up", "max_dd", "volatility"}
