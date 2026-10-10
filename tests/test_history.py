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
