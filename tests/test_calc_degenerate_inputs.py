"""Degenerate numbers into the pure calculator tools (CLAUDE.md invariants 1 and 4; BACKLOG 6.53, card item 271).

The schema of a float parameter (`gt=0` and the like) lets +-inf through: the strings "inf" / "1e400" coerce to it, and
so does the JSON number 1e400; inside list and dict members "nan" passes too. Before the fix such inputs either raised
out of the tool (option_calc and the strategy templates: log(F / inf); odds_devig: 1 / 0; touch_probability with
iv=1e-300: division by sigma^2 = 0; an int 10**400 inside a leg dict: OverflowError) or answered ok=true with NaN /
Infinity in the numbers (which the structured content shows as null). Now: an envelope with ok=false and
`missing[0].reason == "invalid_input"`, and no non-finite number anywhere in the answer.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from mcp import Client

from test_tools_bad_input import check, working_runtime
from tradedesk.server import build_server
from tradedesk.tools._util import first_nonfinite

LEG = {"kind": "call", "side": "long", "size": 1, "strike": 100.0, "entry_premium": 5.0}
LINEAR = {"kind": "linear", "side": "long", "size": 1, "entry": 100.0}
BASE: dict[str, dict[str, Any]] = {
    "odds_devig": {"odds": [1.9, 2.0]},
    "option_calc": {"forward": 100.0, "strike": 100.0, "days": 30.0, "iv": 0.5},
    "touch_probability": {"spot": 100.0, "barrier": 110.0, "days": 30.0, "iv": 0.5},
    "fib_retracement": {"p0": 100.0, "l_point": 90.0},
    "strategy_payoff": {"legs": [LEG], "spot": 100.0, "days": 30.0, "iv": 0.5},
    "strategy_straddle": {"strike": 100.0, "call_premium": 5.0, "put_premium": 5.0, "spot": 100.0, "days": 30.0, "iv": 0.5},
    "strategy_strangle": {
        "call_strike": 110.0,
        "put_strike": 90.0,
        "call_premium": 2.0,
        "put_premium": 2.0,
        "spot": 100.0,
        "days": 30.0,
        "iv": 0.5,
    },
    "strategy_iron_condor": {
        "long_put_strike": 80.0,
        "short_put_strike": 90.0,
        "short_call_strike": 110.0,
        "long_call_strike": 120.0,
        "long_put_premium": 0.5,
        "short_put_premium": 2.0,
        "short_call_premium": 2.0,
        "long_call_premium": 0.5,
        "spot": 100.0,
        "days": 30.0,
        "iv": 0.5,
    },
    "strategy_risk_reversal": {
        "call_strike": 110.0,
        "put_strike": 90.0,
        "call_premium": 2.0,
        "put_premium": 2.5,
        "spot": 100.0,
        "days": 30.0,
        "iv": 0.5,
    },
    "strategy_scenario_matrix": {"legs": [LEG], "spot": 100.0},
}
# Scalars: values that pass `gt=0` / `ge=0` and become +inf ("nan" fails `gt=0` in the schema itself).
SCALAR_BAD = ["inf", "1e400"]
# List and dict members carry no bounds: NaN and -inf get through as well.
MEMBER_BAD = ["inf", "1e400", "nan", "-inf"]


def _cases() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for tool, base in BASE.items():
        for param, value in base.items():
            if isinstance(value, float):
                out += [(tool, {**base, param: v}) for v in SCALAR_BAD]
        optional = ["forward"] if tool.startswith("strategy_") else []
        optional += ["days", "iv"] if tool == "strategy_scenario_matrix" else []
        out += [(tool, {**base, p: v}) for p in optional for v in SCALAR_BAD]
        if tool in ("strategy_straddle", "strategy_strangle", "strategy_iron_condor", "strategy_risk_reversal"):
            out += [(tool, {**base, "size": v}) for v in SCALAR_BAD]
    out += [("odds_devig", {"odds": [v, 2.0]}) for v in MEMBER_BAD]
    out += [("fib_retracement", {"p0": 100.0, "l_point": 90.0, "levels": [0.5, v]}) for v in MEMBER_BAD]
    for tool in ("strategy_payoff", "strategy_scenario_matrix"):
        base = BASE[tool]
        for key in ("size", "strike", "entry_premium"):
            out += [(tool, {**base, "legs": [{**LEG, key: v}]}) for v in [*MEMBER_BAD, 10**400]]
        out += [(tool, {**base, "legs": [{**LINEAR, "entry": v}]}) for v in [*MEMBER_BAD, 10**400]]
    sm = BASE["strategy_scenario_matrix"]
    out += [("strategy_scenario_matrix", {**sm, "horizons": [{"days": v, "iv": 0.5}]}) for v in MEMBER_BAD]
    out += [("strategy_scenario_matrix", {**sm, "horizons": [{"days": 10.0, "iv": v}]}) for v in MEMBER_BAD]
    out += [("strategy_scenario_matrix", {**sm, "price_grid_pct": [0.0, v]}) for v in MEMBER_BAD]
    return out


# Finite inputs whose maths breaks: each raised out of the tool or put inf into the answer before the fix.
EXTREME: list[tuple[str, dict[str, Any]]] = [
    ("odds_devig", {"odds": [1.001, 1.001, 1e300], "method": "power"}),  # p**k underflows to 0 -> 1 / 0
    ("option_calc", {"forward": 1e-300, "strike": 1e300, "days": 30.0, "iv": 0.5}),  # F / K underflows -> log(0)
    ("touch_probability", {"spot": 100.0, "barrier": 110.0, "days": 30.0, "iv": 1e-300}),  # sigma^2 underflows -> / 0
    ("fib_retracement", {"p0": 1e308, "l_point": 1e-300, "levels": [1e308]}),  # ladder price overflows
    ("strategy_payoff", {"legs": [{**LEG, "size": 1e300, "entry_premium": 1e300}], "spot": 100.0, "days": 30.0, "iv": 0.5}),
    ("strategy_straddle", {"strike": 1e300, "call_premium": 1.0, "put_premium": 1.0, "spot": 1e-300, "days": 30.0, "iv": 0.5}),
    ("strategy_scenario_matrix", {"legs": [{**LEG, "size": 1e300}], "spot": 1e300, "price_grid_pct": [1e300]}),
]


def _text_envelope(res) -> dict[str, Any]:
    """The envelope from the text block: it keeps NaN / Infinity, which the structured content would show as null."""
    return json.loads(res.content[0].text)


def _schema_rejected(res) -> bool:
    """The MCP layer refused the arguments before the tool ran (e.g. iv='inf' against `lt=5`): not a raise."""
    return bool(res.is_error and res.content and "validation error for" in res.content[0].text)


def _assert_invalid(res, tool: str, args: dict[str, Any]) -> None:
    assert check(res) is None, (tool, args, check(res))
    env = _text_envelope(res)
    assert first_nonfinite(env) is None, (tool, args, first_nonfinite(env))
    assert env["ok"] is False, (tool, args, env["data"])
    # data_unavailable: the strike-order checks of strangle / iron condor, which already turned some of these away.
    assert env["missing"] and env["missing"][0]["reason"] in ("invalid_input", "data_unavailable"), (tool, args, env["missing"])


async def test_nonfinite_inputs_answer_invalid_input():
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
    assert len(cases) >= 100
    assert reached == set(BASE)  # every calculator really ran on a non-finite input


@pytest.mark.parametrize(("tool", "args"), EXTREME, ids=[t for t, _ in EXTREME])
async def test_extreme_finite_inputs_answer_invalid_input(tool, args):
    srv, _ = build_server(working_runtime(""))
    async with Client(srv) as c:
        _assert_invalid(await c.call_tool(tool, args), tool, args)


async def test_normal_inputs_unchanged():
    """The guard does not touch a normal answer: every base case is ok=true with finite numbers."""
    srv, _ = build_server(working_runtime(""))
    async with Client(srv) as c:
        for tool, args in BASE.items():
            res = await c.call_tool(tool, args)
            assert check(res) is None
            env = _text_envelope(res)
            assert env["ok"] is True and env["missing"] == [], (tool, env["missing"])
            assert first_nonfinite(env) is None
        price = _text_envelope(await c.call_tool("option_calc", BASE["option_calc"]))["data"]["price_usd"]
        # ATM Black-76 with r=0: F * (2 N(sigma sqrt(T) / 2) - 1); 100, 30 d, 50 % -> ~5.71
        assert price == pytest.approx(100.0 * (2 * 0.5 * (1 + math.erf(0.5 * math.sqrt(30 / 365) / 2 / math.sqrt(2))) - 1), abs=1e-3)


def test_first_nonfinite_paths():
    assert first_nonfinite({"a": 1.0, "b": [1, 2, {"c": 3.5}], "s": "x", "t": True}) is None
    assert first_nonfinite({"a": [1.0, float("nan")]}) == "a.1"
    assert first_nonfinite({"legs": [{"size": 10**400}]}) == "legs.0.size"
    assert first_nonfinite({"legs": [{"size": "inf", "kind": "call"}]}) == "legs.0.size"
    assert first_nonfinite({"x": None, "y": "1e5"}) is None
    assert first_nonfinite(float("-inf")) == "value"
