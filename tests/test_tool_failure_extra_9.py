"""Invariant 1 / 5: analytics failure, garbage legs and extreme values -> envelope, never an exception (item 292, BACKLOG 6.53).

The last seven tools of the 6.53 list: strategy_payoff, strategy_straddle, strategy_strangle, strategy_risk_reversal,
strategy_iron_condor, strategy_funding_carry and strategy_scenario_matrix. They have neither a store nor a provider,
so their dependency is the analytics (`summarise`, `scenario_matrix`): the tests make it raise or return NaN / inf,
and check them with no store and every venue failing. Strings and bytes reach these tools only inside the caller's leg
dicts (the MCP arguments are JSON, so a lone surrogate stands in for invalid UTF-8). Found on the way: strategy_payoff
and strategy_scenario_matrix answered ok=true on a leg with an unknown kind or side, a size <= 0 or a bool, an option
strike <= 0 or a negative premium (the leg was dropped from the payoff, or an unknown kind was priced as a put in the
greeks / MTM); strategy_scenario_matrix answered ok=true with an empty matrix for `horizons=[]` and priced a negative
horizon iv as zero vol. All of these now answer ok=false (`invalid_input`).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from test_tools_provider_outage import failing_runtime
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools import strategy as strategy_tools
from tradedesk.tools._util import first_nonfinite

HUGE = 1e308
NUMERIC_ERRORS = (ValueError, ZeroDivisionError, OverflowError)
MEGA = "x" * 1_000_000

LEG = {"kind": "call", "side": "long", "size": 1.0, "strike": 100.0, "entry_premium": 5.0}
MARKET = {"spot": 100.0, "days": 30.0, "iv": 0.5}
PAYOFF = {"legs": [LEG], **MARKET}
MATRIX = {"legs": [LEG], "spot": 100.0}
STRADDLE = {"strike": 100.0, "call_premium": 5.0, "put_premium": 5.0, **MARKET}
STRANGLE = {"call_strike": 110.0, "put_strike": 90.0, "call_premium": 2.0, "put_premium": 2.0, **MARKET}
RR = {"call_strike": 110.0, "put_strike": 90.0, "call_premium": 2.0, "put_premium": 3.0, **MARKET}
CONDOR = {"long_put_strike": 80.0, "short_put_strike": 90.0, "short_call_strike": 110.0, "long_call_strike": 120.0}
CONDOR |= {"long_put_premium": 1.0, "short_put_premium": 2.0, "short_call_premium": 2.0, "long_call_premium": 1.0, **MARKET}
FUNDING = {"long_mark": 100.0, "short_mark": 101.0, "days": 30.0, "iv": 0.5}
TEMPLATES = [
    ("strategy_straddle", STRADDLE),
    ("strategy_strangle", STRANGLE),
    ("strategy_risk_reversal", RR),
    ("strategy_iron_condor", CONDOR),
    ("strategy_funding_carry", FUNDING),
]
ALL = [("strategy_payoff", PAYOFF), *TEMPLATES, ("strategy_scenario_matrix", MATRIX)]
IDS = [t.removeprefix("strategy_") for t, _ in ALL]


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    yield rt
    await rt.aclose()


@asynccontextmanager
async def _session(rt):
    async with Client(build_server(rt)[0]) as c:

        async def call(tool: str, /, **args) -> dict:
            res = await c.call_tool(tool, args)
            assert not res.is_error, res.content[0].text[:300] if res.content else ""
            text = json.loads(res.content[0].text)  # the text reply keeps Infinity / NaN, the structured one shows null
            assert first_nonfinite(text.get("data")) is None, str(text)[:300]
            data = res.structured_content if res.structured_content is not None else text
            return data["result"] if set(data) == {"result"} else data

        yield call


def _missing(env: dict, field: str, reason: str, detail: str = "") -> bool:
    return any(m["field"] == field and m["reason"] == reason and detail in m["detail"] for m in env["missing"])


def _not_ok(env: dict, field: str, reason: str, detail: str = "") -> None:
    assert env["ok"] is False and env["data"] is None and _missing(env, field, reason, detail), str(env)[:400]


def _boom(exc):
    def f(*_a, **_k):
        raise exc("numeric failure")

    return f


# ---------- analytics failure ----------


@pytest.mark.parametrize("exc", NUMERIC_ERRORS)
@pytest.mark.parametrize(("tool", "args"), TEMPLATES, ids=[t for t, _ in TEMPLATES])
async def test_template_analytics_error_goes_to_missing(rt, monkeypatch, tool, args, exc):
    monkeypatch.setattr(strategy_tools, "summarise", _boom(exc))
    async with _session(rt) as call:
        _not_ok(await call(tool, **args), "strategy", "invalid_input", "numeric failure")


@pytest.mark.parametrize("exc", (*NUMERIC_ERRORS, KeyError, TypeError))
@pytest.mark.parametrize(
    ("tool", "fn", "args"), [("strategy_payoff", "summarise", PAYOFF), ("strategy_scenario_matrix", "scenario_matrix", MATRIX)]
)
async def test_leg_tool_analytics_error_goes_to_missing(rt, monkeypatch, tool, fn, args, exc):
    monkeypatch.setattr(strategy_tools, fn, _boom(exc))
    async with _session(rt) as call:
        _not_ok(await call(tool, **args), "legs", "data_unavailable", "numeric failure")


def _nan_summary(*_a, **_k):
    return {"breakevens": [], "max_profit": float("nan"), "max_loss": 0.0, "mtm_usd": 0.0, "pop": None, "greeks": {},
            "grid": [1.0], "prices": [1.0], "pnl": [0.0]}  # fmt: skip


def _inf_matrix(*_a, **_k):
    return [{"days": 1.0, "iv": 0.5, "prices": [1.0], "pnl": [float("inf")], "greeks": {"delta": 0.0}}]


@pytest.mark.parametrize(("tool", "args"), ALL, ids=IDS)
async def test_non_finite_analytics_result_is_not_ok(rt, monkeypatch, tool, args):
    monkeypatch.setattr(strategy_tools, "summarise", _nan_summary)
    monkeypatch.setattr(strategy_tools, "scenario_matrix", _inf_matrix)
    async with _session(rt) as call:
        _not_ok(await call(tool, **args), "result", "invalid_input")


@pytest.mark.parametrize(("tool", "args"), ALL, ids=IDS)
async def test_needs_no_store_or_venue(tool, args):
    # No store (db_path empty), every HTTP request 500 and every ccxt call failing: the calculator still answers.
    calls: list[str] = []
    rt = failing_runtime("down", "", calls)
    try:
        async with _session(rt) as call:
            env = await call(tool, **args)
    finally:
        await rt.aclose()
    assert calls == [] and env["ok"] is True and env["missing"] == [] and env["evidence"] == []


# ---------- garbage legs (strategy_payoff, strategy_scenario_matrix) ----------

BAD_LEGS = {
    "kind_typo": {**LEG, "kind": "calll"},
    "kind_mega": {**LEG, "kind": MEGA},
    "kind_surrogate": {**LEG, "kind": "\udcff"},
    "kind_missing": {k: v for k, v in LEG.items() if k != "kind"},
    "side_typo": {**LEG, "side": "lng"},
    "side_list": {**LEG, "side": ["long"]},
    "size_zero": {**LEG, "size": 0},
    "size_negative": {**LEG, "size": -1.0},
    "size_bool": {**LEG, "size": True},
    "size_str": {**LEG, "size": "abc"},
    "size_none": {**LEG, "size": None},
    "strike_zero": {**LEG, "strike": 0},
    "strike_negative": {**LEG, "strike": -100.0},
    "strike_missing": {k: v for k, v in LEG.items() if k != "strike"},
    "strike_mega": {**LEG, "strike": MEGA},
    "strike_dict": {**LEG, "strike": {"a": 1}},
    "premium_negative": {**LEG, "entry_premium": -5.0},
    "premium_str": {**LEG, "entry_premium": "abc"},
    "linear_size_zero": {"kind": "linear", "side": "short", "size": 0, "entry": 100.0},
}


@pytest.mark.parametrize("tool", ["strategy_payoff", "strategy_scenario_matrix"])
@pytest.mark.parametrize("leg", BAD_LEGS.values(), ids=BAD_LEGS.keys())
async def test_garbage_leg_is_invalid_input(rt, tool, leg):
    # Before item 292 most of these answered ok=true: the leg was silently dropped (unknown side, size <= 0), an
    # unknown kind was priced as nothing in the payoff but as a put in the greeks / MTM, a strike <= 0 was priced in
    # the payoff only.
    args = (PAYOFF if tool == "strategy_payoff" else MATRIX) | {"legs": [LEG, leg]}
    async with _session(rt) as call:
        env = await call(tool, **args)
        _not_ok(env, "legs", "invalid_input", "legs.1.")
        assert len(json.dumps(env)) < 10_000  # the message does not echo a megabyte value


async def test_scenario_matrix_empty_horizons_or_negative_iv_is_invalid(rt):
    async with _session(rt) as call:
        # ok=true with an empty matrix before item 292
        _not_ok(await call("strategy_scenario_matrix", **MATRIX, horizons=[]), "input", "invalid_input", "horizons is empty")
        # a negative iv was priced as zero vol (intrinsic value) before item 292
        env = await call("strategy_scenario_matrix", **MATRIX, horizons=[{"days": 10.0, "iv": 0.5}, {"days": 10.0, "iv": -0.5}])
        _not_ok(env, "input", "invalid_input", "horizons.1.iv is negative")
        env = await call("strategy_scenario_matrix", **MATRIX, horizons=[{"days": 10.0, "iv": 0.0}])  # zero vol stays allowed
        assert env["ok"] is True and env["data"]["matrix"][0]["pnl_usd"][3] == pytest.approx(-5.0)


# ---------- non-finite and overflowing inputs ----------


@pytest.mark.parametrize(
    ("tool", "args"),
    [("strategy_funding_carry", FUNDING | {k: v}) for k in ("long_mark", "short_mark", "days", "iv", "size") for v in ("inf", "1e400")]
    + [(t, a | {"size": v}) for t, a in TEMPLATES[:4] for v in ("inf", "1e400")]
    + [("strategy_payoff", PAYOFF | {"legs": [LEG | {"entry_premium": v}]}) for v in ("inf", "1e400", "nan")]
    + [("strategy_scenario_matrix", MATRIX | {"horizons": [{"days": v, "iv": 0.5}]}) for v in ("inf", "1e400")],
)
async def test_non_finite_input_is_invalid(rt, tool, args):
    async with _session(rt) as call:
        _not_ok(await call(tool, **args), "input", "invalid_input")


@pytest.mark.parametrize(
    ("tool", "args", "field"),
    [
        ("strategy_payoff", {"legs": [LEG | {"strike": HUGE}], "spot": HUGE, "days": 30.0, "iv": 0.5}, "result"),
        ("strategy_straddle", STRADDLE | {"call_premium": HUGE, "put_premium": HUGE}, "result"),
        ("strategy_iron_condor", CONDOR | {"long_call_strike": HUGE, "long_call_premium": HUGE, "size": HUGE}, "result"),
        ("strategy_funding_carry", FUNDING | {"long_mark": 1e-308, "short_mark": HUGE}, "result"),
        ("strategy_funding_carry", FUNDING | {"long_mark": HUGE, "short_mark": HUGE}, "input"),  # the mid overflows
        ("strategy_scenario_matrix", MATRIX | {"spot": HUGE, "price_grid_pct": [100.0]}, "result"),
    ],
    ids=["payoff", "straddle", "condor", "funding_spread", "funding_mid", "matrix"],
)
async def test_overflowing_result_is_not_ok(rt, tool, args, field):
    async with _session(rt) as call:
        _not_ok(await call(tool, **args), field, "invalid_input")


async def test_extreme_but_valid_inputs_answer(rt):
    async with _session(rt) as call:
        env = await call("strategy_payoff", **PAYOFF | {"legs": [LEG | {"strike": 10**20}], "days": 10**20})
        assert env["ok"] is True and env["data"]["max_loss_usd"] == pytest.approx(-5.0)
        env = await call("strategy_payoff", **PAYOFF | {"legs": [LEG | {"note": MEGA}]})  # an unknown key is ignored
        assert env["ok"] is True and env["data"]["breakevens"] == [105.0]
        env = await call("strategy_funding_carry", **FUNDING | {"days": 10**20})
        assert env["ok"] is True and env["data"]["spread_bps"] == pytest.approx(100.0)
        env = await call("strategy_risk_reversal", **RR | {"call_strike": 90.0, "put_strike": 110.0})  # crossed strikes: a real structure
        assert env["ok"] is True and env["missing"] == []
        env = await call("strategy_scenario_matrix", **MATRIX | {"horizons": [{"days": 10**20, "iv": 0.5}]})
        assert env["ok"] is True and env["data"]["matrix"][0]["pnl_usd"][3] == pytest.approx(95.0)
        env = await call("strategy_scenario_matrix", **MATRIX | {"price_grid_pct": [1e306]})
        assert env["ok"] is True and env["data"]["matrix"][-1]["pnl_usd"][0] == pytest.approx(1e306, rel=1e-6)
        env = await call("strategy_iron_condor", **CONDOR | {"long_put_strike": 95.0})  # bad strike order
        _not_ok(env, "strikes", "data_unavailable", "long_put < short_put")
        env = await call("strategy_strangle", **STRANGLE | {"put_strike": 110.0})
        _not_ok(env, "strikes", "data_unavailable", "put_strike must be below call_strike")
