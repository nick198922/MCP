from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_iv_rank_reports_missing_without_history(state):
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "iv_rank", currency="BTC", expiry="2026-11-27T08:00:00+00:00", lookback_days=30)
        assert env["ok"] is False
        assert any(m["provider"] == "store" for m in env["missing"])
    finally:
        await rt.aclose()


async def test_iv_rank_computes_rank_and_percentile(state):
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()

    expiry = "2026-11-27T08:00:00+00:00"
    now_ts = int(NOW.timestamp())
    # Insert 20 synthetic ATM-IV points: rising from 0.30 to 0.49 (step 0.01).
    for i in range(20):
        ts = now_ts - (19 - i) * 3600  # one point per hour, oldest first
        await rt.store.insert_atm_term(
            ts=ts,
            currency="BTC",
            rows=[{"expiry": expiry, "days": 48.0, "forward": 83000.0, "atm_iv": 0.30 + 0.01 * i}],
        )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "iv_rank", currency="BTC", expiry=expiry, lookback_days=2, min_points=10)
        d = env["data"]
        assert d["count"] == 20
        assert d["min_iv"] == 0.30 and d["max_iv"] == 0.49
        assert d["latest"]["atm_iv"] == 0.49
        assert d["iv_rank"] == 1.0  # latest at the top of the window
        assert d["percentile"] == pytest.approx(1 - 0.5 / 20)  # 19 below + half tie / 20
    finally:
        await rt.aclose()


async def test_funding_zscore_computes_stats(state):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=":memory:"),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    now_ts = int(NOW.timestamp())
    # Insert 15 rising funding points (annualized from 0.08 to 0.22).
    for i in range(15):
        ts = now_ts - (14 - i) * 3600
        await rt.store.insert_funding(
            ts=ts,
            rows=[
                {
                    "venue": "binanceusdm",
                    "base": "BTC",
                    "rate_per_interval": 0.0001,
                    "interval_hours": 8.0,
                    "annualized": 0.08 + 0.01 * i,
                }
            ],
        )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "funding_zscore", venue="binanceusdm", base="BTC", lookback_days=1, min_points=10)
        d = env["data"]["annualized"]
        assert d["count"] == 15
        assert d["latest"] == 0.22 and d["min"] == 0.08 and d["max"] == 0.22
        assert d["rank"] == 1.0
        assert d["z_score"] > 1.0  # latest well above the mean
    finally:
        await rt.aclose()


async def test_skew_percentile_reads_rr25(state):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=":memory:"),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    now_ts = int(NOW.timestamp())
    expiry = "2026-11-27T08:00:00+00:00"
    # 12 points of rr25 that drift from -0.055 up to 0.000 (puts getting cheaper).
    for i in range(12):
        ts = now_ts - (11 - i) * 3600
        await rt.store.insert_atm_term(
            ts=ts,
            currency="BTC",
            rows=[
                {
                    "expiry": expiry,
                    "days": 48.0,
                    "forward": 83000.0,
                    "atm_iv": 0.42,
                    "rr25": -0.055 + 0.005 * i,
                    "fly25": 0.009,
                }
            ],
        )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "skew_percentile", currency="BTC", expiry=expiry, delta_target="25", lookback_days=1, min_points=10)
        d = env["data"]["rr"]
        assert d["count"] == 12
        assert d["min"] == -0.055 and d["max"] == 0.0
        assert d["rank"] == 1.0  # latest = max
        assert d["z_score"] > 1.0
        # Missing-column path: no rr10 stored → ok=false
        env10 = await _call(srv, "skew_percentile", currency="BTC", expiry=expiry, delta_target="10", lookback_days=1, min_points=10)
        assert env10["ok"] is False
    finally:
        await rt.aclose()


async def test_iv_rank_without_store_reports_config_miss(state):
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is None
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "iv_rank", currency="BTC", expiry="2026-11-27T08:00:00+00:00")
        assert env["ok"] is False
        assert any(m["reason"] == "config" for m in env["missing"])
    finally:
        await rt.aclose()
