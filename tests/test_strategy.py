from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.strategy import aggregate_greeks, breakevens, payoff_curve, prob_of_profit, summarise
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# ---------- pure analytics ----------


def test_long_straddle_payoff_breakevens_and_pop():
    # Long 1x 100 call @ 5 and 1x 100 put @ 5 => max loss 10 at S=100, breakevens 90/110.
    legs = [
        {"kind": "call", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0},
        {"kind": "put", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0},
    ]
    prices = [80.0 + i for i in range(41)]
    pnl = payoff_curve(legs, prices)
    assert pnl[prices.index(100.0)] == pytest.approx(-10.0)
    assert pnl[prices.index(90.0)] == pytest.approx(0.0)
    assert pnl[prices.index(110.0)] == pytest.approx(0.0)
    be = breakevens(prices, pnl)
    assert sorted(round(x, 6) for x in be) == [90.0, 110.0]
    # PoP: under lognormal with vol=0.4, 1y, S0=100, probability |ret| > ~10% is well above 50%.
    pop = prob_of_profit(legs, prices, pnl, forward=100.0, days=365.0, iv=0.4)
    assert pop is not None and 0.5 < pop < 0.95


def test_bull_call_spread_payoff_and_greeks():
    # Buy 1x 100 call @ 6, sell 1x 110 call @ 2. Max profit = 10 - 4 = 6 at S >= 110.
    legs = [
        {"kind": "call", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 6.0},
        {"kind": "call", "side": "short", "size": 1.0, "strike": 110.0, "entry_premium": 2.0},
    ]
    prices = [80.0, 100.0, 110.0, 130.0]
    pnl = payoff_curve(legs, prices)
    assert pnl == pytest.approx([-4.0, -4.0, 6.0, 6.0])
    greeks = aggregate_greeks(legs, forward=100.0, days=30.0, iv=0.3)
    # Long call delta > short call delta (further OTM) → net positive delta.
    assert greeks["delta"] > 0


def test_summarise_returns_structured_block():
    legs = [
        {"kind": "call", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0},
        {"kind": "put", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0},
    ]
    out = summarise(legs, forward=100.0, days=90.0, iv=0.4, width_pct=0.3, grid=61)
    assert len(out["prices"]) == 61 and len(out["pnl"]) == 61
    assert len(out["breakevens"]) == 2
    assert out["max_loss"] == pytest.approx(-10.0)
    assert out["pop"] is not None and 0.0 <= out["pop"] <= 1.0
    assert out["greeks"]["gamma"] > 0  # long gamma straddle


# ---------- MCP tool ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def test_strategy_payoff_mcp_round_trip(state):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        legs = [
            {"kind": "call", "side": "long", "size": 1.0, "strike": 83000.0, "entry_premium": 1500.0},
            {"kind": "put", "side": "long", "size": 1.0, "strike": 83000.0, "entry_premium": 1500.0},
        ]
        env = await _call(srv, "strategy_payoff", legs=legs, spot=83000.0, days=20.0, iv=0.4, kind="summary")
        d = env["data"]
        assert len(d["breakevens"]) == 2
        assert d["max_loss_usd"] == pytest.approx(-3000.0, abs=1.0)
        assert "prices" not in d  # summary mode drops the curve
        assert d["pop"] is not None
    finally:
        await rt.aclose()


async def test_strategy_payoff_rejects_bad_leg(state):
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        # Missing strike on an option leg.
        env = await _call(
            srv,
            "strategy_payoff",
            legs=[{"kind": "call", "side": "long", "size": 1.0, "entry_premium": 5.0}],
            spot=100.0,
            days=30.0,
            iv=0.3,
        )
        assert env["ok"] is False
    finally:
        await rt.aclose()
