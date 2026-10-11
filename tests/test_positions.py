from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.portfolio import aggregate_greeks, scenario_pnl
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


# ---------- pure helpers ----------


def test_aggregate_greeks_signs_short_correctly():
    pos = [
        {"side": "long", "size": 1.0, "delta": 0.5, "gamma": 0.0001, "vega": 100.0, "theta": -10.0, "mark": 1000.0, "instrument": "A"},
        {"side": "short", "size": 2.0, "delta": 0.4, "gamma": 0.0002, "vega": 50.0, "theta": -5.0, "mark": 500.0, "instrument": "B"},
    ]
    out = aggregate_greeks(pos)["totals"]
    # long contributes +, short contributes - size * greek
    assert out["delta"] == pytest.approx(1 * 0.5 - 2 * 0.4)  # -0.3
    assert out["gamma"] == pytest.approx(1 * 0.0001 - 2 * 0.0002)  # -0.0003
    assert out["vega"] == pytest.approx(1 * 100.0 - 2 * 50.0)  # 0
    assert out["theta"] == pytest.approx(1 * -10.0 - 2 * -5.0)  # 0
    # notional uses raw size (always positive), not signed
    assert out["notional_usd"] == pytest.approx(1 * 1000.0 + 2 * 500.0)
    assert out["count"] == 2


def test_aggregate_greeks_ignores_bad_rows():
    pos = [
        {"side": "neutral", "size": 1.0, "delta": 1.0},  # bad side
        {"side": "long", "size": 0, "delta": 1.0},  # zero size
        {"side": "long", "size": -1, "delta": 1.0},  # negative size
        {"side": "long", "size": 1.0, "delta": 0.3},  # good
    ]
    out = aggregate_greeks(pos)["totals"]
    assert out["count"] == 1
    assert out["delta"] == pytest.approx(0.3)


def test_scenario_pnl_uses_reference_spot_for_dspot():
    pos = [
        # long call: delta=0.5, gamma=0.0001, vega=200, theta=-20, mark=1000
        {"side": "long", "size": 1.0, "delta": 0.5, "gamma": 0.0001, "vega": 200.0, "theta": -20.0, "mark": 1000.0, "instrument": "C"},
    ]
    # +10% spot → dSpot = 10000. PnL = 0.5*10000 + 0.5*0.0001*10000^2 = 5000 + 5000 = 10000
    res = scenario_pnl(pos, d_spot_pct=0.10, reference_spot=100_000.0)
    assert res["d_spot_abs"] == pytest.approx(10_000.0)
    assert res["total_pnl"] == pytest.approx(10_000.0)

    # +5 vol-points → +1000; +0.5 days → -10
    res2 = scenario_pnl(pos, d_vol_points=5.0, days=0.5)
    assert res2["total_pnl"] == pytest.approx(5.0 * 200.0 - 20.0 * 0.5)


# ---------- MCP tools round-trip ----------


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "positions.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_positions_add_list_close(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        a = await _call(
            srv,
            "positions_add",
            instrument="BTC-30OCT26-82000-C",
            kind="option",
            side="long",
            size=5.0,
            entry=0.07,
            mark=83000.0,
            delta=0.52,
            gamma=0.00003,
            vega=120.0,
            theta=-55.0,
            tags=["btc", "atm"],
        )
        assert a["data"]["id"] == 1
        lst = (await _call(srv, "positions_list", status="open"))["data"]
        assert lst["count"] == 1 and lst["positions"][0]["instrument"] == "BTC-30OCT26-82000-C"

        closed = (await _call(srv, "positions_close", id=1, note="early exit"))["data"]
        assert closed["status"] == "closed" and closed["note"] == "early exit"

        # Second close must fail
        again = await _call(srv, "positions_close", id=1)
        assert again["ok"] is False
    finally:
        await rt.aclose()


async def test_positions_risk_runs_default_scenarios(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        # long call + short put against the same spot: creates non-trivial net greeks.
        await _call(
            srv,
            "positions_add",
            instrument="BTC-30OCT26-82000-C",
            kind="option",
            side="long",
            size=10.0,
            mark=83000.0,
            delta=0.52,
            gamma=0.00003,
            vega=120.0,
            theta=-55.0,
        )
        await _call(
            srv,
            "positions_add",
            instrument="BTC-30OCT26-80000-P",
            kind="option",
            side="short",
            size=5.0,
            mark=83000.0,
            delta=-0.38,
            gamma=0.00002,
            vega=90.0,
            theta=-42.0,
        )
        risk = (await _call(srv, "positions_risk"))["data"]
        g = risk["net_greeks"]
        # long call +5.2 delta, short put adds +1.9 delta (short * -delta_put = -(5)*(-0.38) = 1.9)
        assert g["delta"] == pytest.approx(10 * 0.52 - 5 * (-0.38))
        # Default scenario set has 7 rows
        assert len(risk["scenarios"]) == 7
        # +10% spot scenario must appear with a positive pnl (we're net long delta / long gamma)
        up = next(s for s in risk["scenarios"] if s["d_spot_pct"] == 0.10 and s["d_vol_points"] == 0.0)
        assert up["pnl_usd"] > 0
    finally:
        await rt.aclose()


async def test_positions_risk_no_open_positions(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "positions_risk")
        assert env["ok"] is False
        assert any(m["field"] == "positions" for m in env["missing"])
    finally:
        await rt.aclose()
