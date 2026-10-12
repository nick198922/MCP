from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.strategy import (
    aggregate_greeks,
    breakevens,
    build_cash_and_carry,
    build_iron_condor,
    build_risk_reversal,
    build_straddle,
    build_strangle,
    payoff_curve,
    prob_of_profit,
    scenario_matrix,
    summarise,
)
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


# ---------- template builders ----------


def test_build_straddle_shape():
    legs = build_straddle(strike=100.0, call_premium=5.0, put_premium=5.0, side="long", size=2.0)
    assert [leg["kind"] for leg in legs] == ["call", "put"]
    assert all(leg["side"] == "long" and leg["size"] == 2.0 and leg["strike"] == 100.0 for leg in legs)


def test_build_strangle_separates_strikes():
    legs = build_strangle(call_strike=110.0, put_strike=90.0, call_premium=2.0, put_premium=2.0)
    assert legs[0]["strike"] == 110.0 and legs[1]["strike"] == 90.0


def test_build_risk_reversal_sides():
    bull = build_risk_reversal(call_strike=110.0, put_strike=90.0, call_premium=3.0, put_premium=2.0, direction="bullish")
    assert [leg["side"] for leg in bull] == ["long", "short"]
    bear = build_risk_reversal(call_strike=110.0, put_strike=90.0, call_premium=3.0, put_premium=2.0, direction="bearish")
    assert [leg["side"] for leg in bear] == ["short", "long"]
    with pytest.raises(ValueError):
        build_risk_reversal(call_strike=110.0, put_strike=90.0, call_premium=3.0, put_premium=2.0, direction="sideways")


def test_build_iron_condor_rejects_bad_strikes():
    with pytest.raises(ValueError):
        build_iron_condor(
            long_put_strike=90.0,
            short_put_strike=100.0,  # not < short_call
            short_call_strike=100.0,
            long_call_strike=110.0,
            long_put_premium=1.0,
            short_put_premium=3.0,
            short_call_premium=3.0,
            long_call_premium=1.0,
        )


def test_cash_and_carry_payoff_locks_in_basis():
    # Buy spot at 100, sell futures at 102 → at expiry future converges to spot,
    # net PnL = (S - 100) + (102 - S) = 2 regardless of S.
    legs = build_cash_and_carry(spot_price=100.0, futures_price=102.0, size=1.0)
    pnl = payoff_curve(legs, [80.0, 100.0, 102.0, 120.0])
    assert pnl == pytest.approx([2.0, 2.0, 2.0, 2.0])


async def test_strategy_straddle_mcp(state):
    from tradedesk.runtime import Runtime  # noqa: PLC0415
    from tradedesk.server import build_server  # noqa: PLC0415

    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "strategy_straddle",
            strike=83000.0,
            call_premium=1500.0,
            put_premium=1500.0,
            spot=83000.0,
            days=20.0,
            iv=0.4,
        )
        d = env["data"]
        assert len(d["legs"]) == 2
        assert len(d["breakevens"]) == 2
        assert d["max_loss_usd"] == pytest.approx(-3000.0, abs=1.0)
        assert d["greeks"]["gamma"] > 0
    finally:
        await rt.aclose()


async def test_strategy_cash_and_carry_reports_basis(state):
    from tradedesk.runtime import Runtime  # noqa: PLC0415
    from tradedesk.server import build_server  # noqa: PLC0415

    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "strategy_cash_and_carry",
            spot_price=83000.0,
            futures_price=84000.0,
            days=90.0,
            iv=0.4,
        )
        d = env["data"]
        assert d["basis_abs"] == pytest.approx(1000.0)
        assert d["basis_pct"] == pytest.approx((84000 / 83000 - 1) * 100, abs=1e-3)  # rounded to 4 dp
        # locked-in basis: PnL at any S should be 1000
        assert d["max_profit_usd"] == pytest.approx(1000.0)
        assert d["max_loss_usd"] == pytest.approx(1000.0)
    finally:
        await rt.aclose()


# ---------- scenario matrix ----------


def test_scenario_matrix_shape_and_time_decay():
    # Long ATM straddle with same vol across horizons — nearer expiries should
    # price cheaper (less theta remaining) so the ATM MTM becomes more negative
    # (we paid premium, time decay eats it).
    legs = [
        {"kind": "call", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0},
        {"kind": "put", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0},
    ]
    horizons = [{"days": 30, "iv": 0.5}, {"days": 10, "iv": 0.5}, {"days": 1, "iv": 0.5}]
    prices = [90.0, 100.0, 110.0]
    out = scenario_matrix(legs, initial_forward=100.0, horizons=horizons, prices=prices)
    assert len(out) == 3 and all(len(row["pnl"]) == 3 for row in out)
    # ATM MTM at the 1-day horizon should be lower than at the 30-day horizon
    # (less time value remains in the options).
    atm_30d = out[0]["pnl"][1]
    atm_1d = out[2]["pnl"][1]
    assert atm_1d < atm_30d
    # Greeks block exists per horizon.
    for row in out:
        for k in ("delta", "gamma", "vega", "theta"):
            assert k in row["greeks"]


def test_scenario_matrix_linear_leg_is_horizon_invariant():
    # Linear leg's MTM depends only on (S - entry), not on time or vol.
    legs = [{"kind": "linear", "side": "long", "size": 1.0, "entry": 100.0}]
    horizons = [{"days": 1, "iv": 0.3}, {"days": 100, "iv": 0.9}]
    prices = [90.0, 100.0, 110.0]
    out = scenario_matrix(legs, initial_forward=100.0, horizons=horizons, prices=prices)
    assert out[0]["pnl"] == out[1]["pnl"] == [-10.0, 0.0, 10.0]


async def test_strategy_scenario_matrix_mcp(state):
    from tradedesk.config import Settings  # noqa: PLC0415
    from tradedesk.runtime import Runtime  # noqa: PLC0415
    from tradedesk.server import build_server  # noqa: PLC0415

    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=""),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    try:
        env = await _call(
            srv,
            "strategy_scenario_matrix",
            legs=[
                {"kind": "call", "side": "long", "size": 1.0, "strike": 83000.0, "entry_premium": 1500.0},
                {"kind": "put", "side": "long", "size": 1.0, "strike": 83000.0, "entry_premium": 1500.0},
            ],
            spot=83000.0,
            days=20.0,
            iv=0.4,
            price_grid_pct=[-10.0, 0.0, 10.0],
        )
        d = env["data"]
        assert d["n_horizons"] == 3
        assert d["matrix"][0]["pct_moves"] == [-10.0, 0.0, 10.0]
        assert len(d["matrix"][0]["pnl_usd"]) == 3
        # The ATM MTM at the shortest horizon should be lower (closer to expiry).
        atm_short = d["matrix"][0]["pnl_usd"][1]
        atm_long = d["matrix"][-1]["pnl_usd"][1]
        assert atm_short < atm_long
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
