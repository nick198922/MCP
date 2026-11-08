"""Degenerate numbers into the carry templates and the scenario matrix; malformed `/prices-history` points
(CLAUDE.md invariants 1 and 4; BACKLOG 6.53, card item 274; continues tests/test_calc_degenerate_inputs.py).

Before the fix `strategy_cash_and_carry` / `strategy_funding_carry` ran the shared `_packed` without any check: a price
of "inf" / "1e400" answered ok=true with Infinity in `legs` and `forward`; a negative `days` in a horizon of
`strategy_scenario_matrix` was silently clamped to 1e-9 (an expiry-time row labelled with a past date); a point of
`/prices-history` with a null / "nan" / inf field raised out of `lag_profile` / `jump_proximity` or put NaN in a lag.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp import Client

from test_calc_degenerate_inputs import LEG, _assert_invalid, _schema_rejected, _text_envelope
from test_tools_bad_input import check, working_runtime
from tradedesk.analytics.pm_calibration import jump_proximity, lag_profile
from tradedesk.server import build_server
from tradedesk.tools._util import first_nonfinite

BASE: dict[str, dict[str, Any]] = {
    "strategy_cash_and_carry": {"spot_price": 100.0, "futures_price": 102.0, "days": 30.0, "iv": 0.5},
    "strategy_funding_carry": {"long_mark": 100.0, "short_mark": 100.5, "days": 30.0, "iv": 0.5},
}
# "inf" / "1e400" pass `gt=0` and become +inf; the rest the schema should refuse (a validation error, not a raise).
BAD = ["inf", "1e400", "nan", "-inf", None, "abc", 10**400]


def _cases() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for tool, base in BASE.items():
        for param in [*base, "size"]:
            out += [(tool, {**base, param: v}) for v in BAD]
    return out


# Finite inputs whose result overflows: Infinity in basis / spread or in the mid of the marks before the fix.
EXTREME: list[tuple[str, dict[str, Any]]] = [
    ("strategy_cash_and_carry", {"spot_price": 1e-300, "futures_price": 1e300, "days": 30.0, "iv": 0.5}),  # basis_pct
    ("strategy_cash_and_carry", {"spot_price": 1e300, "futures_price": 1e300, "days": 30.0, "iv": 0.5, "size": 1e300}),
    ("strategy_funding_carry", {"long_mark": 1e308, "short_mark": 1e308, "days": 30.0, "iv": 0.5}),  # mid = inf
    ("strategy_funding_carry", {"long_mark": 1e-300, "short_mark": 1e300, "days": 30.0, "iv": 0.5}),  # spread_bps
]


async def test_carry_nonfinite_inputs_answer_invalid_input():
    srv, _ = build_server(working_runtime(""))
    cases = _cases()
    reached: set[str] = set()
    async with Client(srv) as c:
        for tool, args in cases:
            res = await c.call_tool(tool, args)
            if _schema_rejected(res):
                continue
            _assert_invalid(res, tool, args)
            reached.add(tool)
    assert reached == set(BASE)  # both tools really ran on a non-finite input


@pytest.mark.parametrize(("tool", "args"), EXTREME, ids=[f"{t}-{i}" for i, (t, _) in enumerate(EXTREME)])
async def test_carry_extreme_finite_inputs_answer_invalid_input(tool, args):
    srv, _ = build_server(working_runtime(""))
    async with Client(srv) as c:
        _assert_invalid(await c.call_tool(tool, args), tool, args)


async def test_carry_normal_inputs_unchanged():
    srv, _ = build_server(working_runtime(""))
    async with Client(srv) as c:
        for tool, args in BASE.items():
            res = await c.call_tool(tool, args)
            assert check(res) is None
            env = _text_envelope(res)
            assert env["ok"] is True and env["missing"] == [], (tool, env["missing"])
            assert first_nonfinite(env) is None
        cc = _text_envelope(await c.call_tool("strategy_cash_and_carry", BASE["strategy_cash_and_carry"]))["data"]
        assert cc["basis_abs"] == pytest.approx(2.0) and cc["basis_pct"] == pytest.approx(2.0)
        assert cc["forward"] == pytest.approx(100.0) and len(cc["legs"]) == 2
        fc = _text_envelope(await c.call_tool("strategy_funding_carry", BASE["strategy_funding_carry"]))["data"]
        assert fc["spread_abs"] == pytest.approx(0.5) and fc["spread_bps"] == pytest.approx(50.0)
        assert fc["forward"] == pytest.approx(100.25)


SM = {"legs": [LEG], "spot": 100.0}


@pytest.mark.parametrize("days", [-0.5, -30.0, -1e300])
async def test_scenario_matrix_negative_horizon_days_rejected(days):
    srv, _ = build_server(working_runtime(""))
    args = {**SM, "horizons": [{"days": 10.0, "iv": 0.5}, {"days": days, "iv": 0.5}]}
    async with Client(srv) as c:
        res = await c.call_tool("strategy_scenario_matrix", args)
    _assert_invalid(res, "strategy_scenario_matrix", args)
    env = _text_envelope(res)
    assert env["missing"][0]["reason"] == "invalid_input" and "horizons.1.days" in env["missing"][0]["detail"]


async def test_scenario_matrix_zero_days_horizon_still_answers():
    """days = 0 is the expiry row (intrinsic value), a valid horizon: unchanged by the negative-days check."""
    srv, _ = build_server(working_runtime(""))
    async with Client(srv) as c:
        env = _text_envelope(await c.call_tool("strategy_scenario_matrix", {**SM, "horizons": [{"days": 0.0, "iv": 0.5}]}))
    assert env["ok"] is True and env["missing"] == []
    # At expiry the long 100-call bought for 5 is worth max(S - 100, 0) - 5 on every grid price.
    row = env["data"]["matrix"][0]
    assert row["pnl_usd"] == pytest.approx([max(p - 100.0, 0.0) - 5.0 for p in row["prices"]], abs=0.01)


TRADES = [{"timestamp": 100, "price": 0.40, "asset": "Y"}, {"timestamp": 200, "price": 0.40, "asset": "N"}]
GOOD_POINTS = [{"t": 150, "p": 0.40}, {"t": 230, "p": 0.60}]
BAD_POINTS = [
    {"t": 160, "p": None},
    {"t": 170, "p": "nan"},
    {"t": 175, "p": float("inf")},
    {"t": 180, "p": "abc"},
    {"t": None, "p": 0.5},
    {"t": float("inf"), "p": 0.5},
    {"t": float("nan"), "p": 0.5},
    {"t": "1e400", "p": 0.5},  # int("1e400") fails; an int t, even 10**400, is a valid (if absurd) time and is kept
    {"t": 185, "p": 10**400},
    {"p": 0.5},
    {"t": 190},
    "not a dict",
]


def test_prices_history_bad_points_are_skipped():
    """A malformed point is dropped (like a malformed print in `_yes_prints`): no raise, no NaN, same answer."""
    ks = (-60, 0, 60)
    mixed = [GOOD_POINTS[0], *BAD_POINTS, GOOD_POINTS[1]]
    assert lag_profile(mixed, TRADES, "Y", ks) == lag_profile(GOOD_POINTS, TRADES, "Y", ks)
    assert first_nonfinite(lag_profile(mixed, TRADES, "Y", ks)) is None
    jp_points = [{"t": 170, "p": 0.41}, {"t": 210, "p": 0.59}]
    assert jump_proximity([*BAD_POINTS, *jp_points], TRADES, "Y") == jump_proximity(jp_points, TRADES, "Y")
    assert jump_proximity(jp_points, TRADES, "Y")["before_0_60"] == [False]
