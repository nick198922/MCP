from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.retracement import DEFAULT_LEVELS, fib_levels, summarise_retracement
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# ---------- pure math ----------


def test_fib_levels_bullish_drop():
    # P0=100, L=50 → 50% level = 75, 100% level pins at P0.
    rows = fib_levels(100.0, 50.0, levels=[0.0, 0.5, 1.0])
    assert [r["price"] for r in rows] == pytest.approx([50.0, 75.0, 100.0])


def test_fib_levels_bearish_rally():
    # P0=100, L=150 → 0% at L, 50% at 125, 100% at P0.
    rows = fib_levels(100.0, 150.0, levels=[0.0, 0.5, 1.0])
    assert [r["price"] for r in rows] == pytest.approx([150.0, 125.0, 100.0])


def test_default_levels_match_fibonacci():
    assert DEFAULT_LEVELS == (0.236, 0.382, 0.5, 0.618, 0.786)


def test_summarise_retracement_applicable_flag():
    big = summarise_retracement(100.0, 90.0, min_drop_pct=2.0)
    assert big["direction"] == "drop" and big["applicable"] is True
    small = summarise_retracement(100.0, 99.0, min_drop_pct=2.0)
    assert small["direction"] == "drop" and small["applicable"] is False


def test_summarise_retracement_flat_input():
    out = summarise_retracement(100.0, 100.0, min_drop_pct=0.0)
    assert out["direction"] == "flat" and out["applicable"] is False


def test_summarise_retracement_rejects_non_positive():
    assert summarise_retracement(0.0, 10.0)["applicable"] is False
    assert summarise_retracement(10.0, 0.0)["applicable"] is False


# ---------- MCP tool ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_fib_retracement_mcp_happy(state):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "fib_retracement", p0=83000.0, l_point=78000.0)
        d = env["data"]
        assert d["applicable"] is True
        assert d["direction"] == "drop"
        assert d["diff_pct"] == pytest.approx((5000 / 83000) * 100, abs=1e-4)
        prices = {lv["level"]: lv["price"] for lv in d["ladder"]}
        # 50% retracement sits halfway between L (78000) and P0 (83000) → 80500
        assert prices[0.5] == pytest.approx(80500.0)
    finally:
        await rt.aclose()


async def test_fib_retracement_small_move_not_applicable(state):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "fib_retracement", p0=100.0, l_point=99.5, min_drop_pct=2.0)
        d = env["data"]
        assert d["applicable"] is False
        assert d["reason"]
        # ladder is still returned for inspection
        assert len(d["ladder"]) == 5
    finally:
        await rt.aclose()
