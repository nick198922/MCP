"""Invariant 1 / 5: store / analytics failure, corrupt stored rows and extreme values -> envelope, never an exception
(item 289, BACKLOG 6.53).

Item 289 names positions_list, positions_risk, plan_link, plan_list, rules_test and odds_devig. positions_list,
positions_risk and plan_list already have their own tests (test_tool_failure_extra_3.py), so the next three of the 6.53
list take their place: fib_retracement, option_calc and strategy_cash_and_carry. plan_link and rules_test read the store;
the four calculators have neither a store nor a provider, so their dependency is the analytics: the tests make it raise
or return NaN, and check them with no store and every venue failing. Found on the way: plan_link raised on a stored plan
with a string / blob / inf level, entry = stop or 0, a string created_ts (after it had written the link), failed its reply
on a blob, returned Infinity / NaN from an inf level or NaN tags, and raised OverflowError on an id of 10**20; rules_test
answered ok=true with Infinity / NaN (an inf literal, an overflow, inf in a stored funding / liquidation row) or a 400-digit
int, answered ok=true when the evaluation failed, and reported a failing store query as an evaluation error;
fib_retracement returned Infinity with ok=true when a huge custom level overflowed a ladder below the noise floor; plan_list
returned NaN from NaN tags (same `_listable` as plan_link). All of these now answer ok=false (or leave the row out with a
warning).

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls and store checks in
one session.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from test_tools_provider_outage import failing_runtime
from tradedesk.analytics import odds as odds_mod
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools import quant as quant_tools
from tradedesk.tools import strategy as strategy_tools
from tradedesk.tools._util import first_nonfinite

NOW_TS = int(NOW.timestamp())
HUGE = 1e308  # finite, so the store keeps it
LONG = 10_000  # a list far longer than any real call (see test_calculators_extreme_but_finite_inputs_answer)
CORRUPT = ("abc", b"\x00\x01", b"\xff\xfe", float("inf"))
NUMERIC_ERRORS = (ValueError, ZeroDivisionError, OverflowError)


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
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


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


def _fails(rt, monkeypatch, method: str) -> None:
    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.store, method, boom)


def _missing(env: dict, field: str, reason: str, detail: str = "") -> bool:
    return any(m["field"] == field and m["reason"] == reason and detail in m["detail"] for m in env["missing"])


def _store_failed(env: dict) -> None:
    assert env["ok"] is False and env["data"] is None and _missing(env, "store", "backend_error", "disk I/O error")


# ---------- plan_link ----------

PLAN_NAMES = ["id", "created_ts", "exchange", "symbol", "instrument", "side", "entry_type", "entry", "stop", "target"]
PLAN_NAMES += ["expires_ts", "verdict", "thesis", "tags", "rules", "status"]
PLAN_SQL = f"INSERT INTO trade_plan ({', '.join(PLAN_NAMES)}) VALUES ({', '.join('?' * len(PLAN_NAMES))})"
GOOD_PLAN = (1, NOW_TS - 60, "binanceusdm", "BTC/USDT:USDT", "perp", "long", "market", 100.0, 95.0, 110.0, NOW_TS + 86400)
GOOD_PLAN += ("enter", None, '["src:own"]', '{"5": "pass"}', "open")
JOURNAL_SQL = "INSERT INTO journal (id, opened_ts, symbol, side, size, entry, status) VALUES (?,?,?,?,?,?,?)"
GOOD_TRADE = (1, NOW_TS - 30, "BTC/USDT:USDT", "long", 1.0, 100.0, "open")


def _plan(**cols) -> tuple:
    row = dict(zip(PLAN_NAMES, GOOD_PLAN, strict=True)) | cols
    return tuple(row[n] for n in PLAN_NAMES)


async def _linked(rt) -> object:
    return (await rt.store.conn.execute_fetchall("SELECT journal_id FROM trade_plan WHERE id = 1"))[0][0]


@pytest.mark.parametrize("method", ["plan_get", "journal_get", "plan_link_journal"])
async def test_plan_link_store_failure_goes_to_missing(rt, monkeypatch, method):
    await _rows(rt, PLAN_SQL, [GOOD_PLAN])
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE])
    _fails(rt, monkeypatch, method)
    async with _session(rt) as call:
        _store_failed(await call("plan_link", id=1, journal_id=1))


@pytest.mark.parametrize(
    "cols",
    [{"entry": b} for b in CORRUPT]
    + [{"stop": 100.0}, {"entry": 0.0}, {"created_ts": "abc"}, {"symbol": b"\xff\xfe"}, {"thesis": b"\xff\xfe"}]
    + [{"tags": "[NaN]"}, {"rules": '{"1": Infinity}'}],
    ids=["entry_str", "entry_blob", "entry_bad_utf8", "entry_inf", "entry_eq_stop", "entry_zero", "created_str"]
    + ["symbol_blob", "thesis_blob", "tags_nan", "rules_inf"],
)
async def test_plan_link_corrupt_plan_is_linked_without_its_row(rt, cols):
    # Linking reads no stored level, so it goes ahead; before item 289 `_view` raised (after the link had been written),
    # the blob failed the reply and inf / NaN came back as Infinity / NaN.
    await _rows(rt, PLAN_SQL, [_plan(**cols)])
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE])
    async with _session(rt) as call:
        env = await call("plan_link", id=1, journal_id=1)
        assert env["ok"] is True and env["data"] == {"id": 1, "journal_id": 1}
        assert any("its stored row is malformed" in w for w in env["warnings"])
        assert await _linked(rt) == 1


@pytest.mark.parametrize(("pid", "jid"), [(10**20, 1), (1, 10**20), (2**63, 2**63)])
async def test_plan_link_id_beyond_sqlite_integer_is_not_found(rt, pid, jid):
    # 10**20 raised OverflowError out of plan_get / journal_get before item 289.
    await _rows(rt, PLAN_SQL, [GOOD_PLAN])
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE])
    async with _session(rt) as call:
        env = await call("plan_link", id=pid, journal_id=jid)
        assert env["ok"] is False and env["data"] is None and _missing(env, "plan_link", "invalid_input", "not found")
        assert await _linked(rt) is None


async def test_plan_link_missing_plan_or_trade(rt):
    await _rows(rt, PLAN_SQL, [GOOD_PLAN])
    async with _session(rt) as call:
        env = await call("plan_link", id=1, journal_id=1)  # no journal trade yet
        assert env["ok"] is False and _missing(env, "plan_link", "invalid_input", "journal trade #1 not found")
        await _rows(rt, JOURNAL_SQL, [GOOD_TRADE])
        env = await call("plan_link", id=2, journal_id=1)
        assert env["ok"] is False and _missing(env, "plan_link", "invalid_input", "plan #2")
        assert await _linked(rt) is None


async def test_plan_link_extreme_but_valid_plan_unchanged(rt):
    await _rows(rt, PLAN_SQL, [_plan(target=HUGE, thesis="t" * 1_000_000)])
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE])
    async with _session(rt) as call:
        env = await call("plan_link", id=1, journal_id=1)
        d = env["data"]
        assert env["ok"] is True and env["warnings"] == [] and d["journal_id"] == 1 and d["target"] == HUGE
        assert d["risk_pct"] == pytest.approx(0.05) and d["rr"] == pytest.approx(2e307, rel=1e-9) and len(d["thesis"]) == 1_000_000
        assert d["tags"] == ["src:own"] and d["rules"] == {"5": "pass"}


async def test_plan_list_skips_a_plan_with_nan_tags(rt):
    # Same `_listable` as plan_link: NaN in the tags JSON came back as NaN with ok=true before item 289.
    await _rows(rt, PLAN_SQL, [GOOD_PLAN, _plan(id=2, tags="[NaN]")])
    async with _session(rt) as call:
        env = await call("plan_list")
        assert env["ok"] is True and env["data"]["count"] == 1 and env["data"]["plans"][0]["id"] == 1
        assert any(w.startswith("1 stored plan rows skipped") and "tags / rules" in w for w in env["warnings"])


# ---------- rules_test ----------

FUNDING_SQL = "INSERT INTO funding_hist VALUES (?,?,?,?,?,?)"
LIQ_SQL = "INSERT INTO liquidations (ts, venue, symbol, side, price, qty, notional_usd) VALUES (?,?,?,?,?,?,?)"


def _funding(*values) -> list[tuple]:
    return [(NOW_TS - 60 * (len(values) - i), "binanceusdm", "BTC", 0.0001, 8.0, v) for i, v in enumerate(values)]


def _liq(*notionals) -> list[tuple]:
    return [(NOW_TS - 60, "binanceusdm", "BTC/USDT:USDT", "buy", 1.0, 1.0, n) for n in notionals]


@pytest.mark.parametrize(
    ("method", "expr"),
    [
        ("funding_series", 'funding("binanceusdm","BTC") > 0.3'),
        ("atm_term_series", 'atm_iv_zscore("BTC","27NOV26", 30)'),
        ("basis_series", 'basis("BTC","27NOV26")'),
        ("liquidations_in_window", "liquidations_total(60) > 1000000"),
    ],
)
async def test_rules_test_store_failure_goes_to_missing(rt, monkeypatch, method, expr):
    # Before item 289 the failing query was reported as `missing: eval` with ok=true.
    _fails(rt, monkeypatch, method)
    async with _session(rt) as call:
        _store_failed(await call("rules_test", expr=expr))


@pytest.mark.parametrize("expr", ["1e400 + 0", "1e308 * 10", "-1e308 * 10", "1e400 - 1e400", "max(1e308, 1e400)", "10 ** 400"])
async def test_rules_test_non_finite_value_is_not_ok(rt, expr):
    # Before item 289: ok=true with Infinity / NaN (or a 400-digit int) in `value`.
    async with _session(rt) as call:
        env = await call("rules_test", expr=expr)
        assert env["ok"] is False and env["data"] == {"expr": expr, "parsed": True, "evaluated": True, "value": None, "truthy": True}
        assert _missing(env, "value", "data_unavailable", "NaN, inf")


@pytest.mark.parametrize(
    ("sql", "rows", "expr"),
    [
        (FUNDING_SQL, _funding(float("inf")), 'funding("binanceusdm","BTC")'),
        (FUNDING_SQL, _funding(HUGE, HUGE, HUGE), 'funding_mean("binanceusdm","BTC", 1)'),
        (LIQ_SQL, _liq(float("inf")), "liquidations_total(60)"),
        (LIQ_SQL, _liq(float("inf")), "liquidations_imbalance(60)"),
    ],
    ids=["funding_inf", "funding_mean_huge", "liq_total_inf", "liq_imbalance_nan"],
)
async def test_rules_test_non_finite_stored_metric_is_not_ok(rt, sql, rows, expr):
    await _rows(rt, sql, rows)
    async with _session(rt) as call:
        env = await call("rules_test", expr=expr)
        assert env["ok"] is False and env["data"]["value"] is None and _missing(env, "value", "data_unavailable")


@pytest.mark.parametrize(
    ("sql", "rows", "expr"),
    [
        (LIQ_SQL, _liq("abc"), "liquidations_total(60)"),
        (LIQ_SQL, _liq(b"\xff\xfe"), "liquidations_imbalance(60)"),
        (FUNDING_SQL, _funding(*[(-1) ** i * HUGE for i in range(6)]), 'funding_zscore("binanceusdm","BTC", 1)'),
        (None, [], 'funding_mean("binanceusdm","BTC", 1e308)'),
        (None, [], 'funding_mean("binanceusdm","BTC", 1e400)'),
        (None, [], 'funding_mean("binanceusdm","BTC", 10**20)'),
        (None, [], "1 / 0"),
        (None, [], "x" * 10_000),  # the longest name the DSL compiles (item 301 refuses longer expressions)
    ],
    ids=["liq_str", "liq_blob", "zscore_overflow", "days_huge", "days_inf", "days_10e20", "div_zero", "longest_name"],
)
async def test_rules_test_evaluation_failure_is_not_ok(rt, sql, rows, expr):
    # A corrupt stored row or an extreme argument raised inside the metric; before item 289 that answered ok=true.
    if sql:
        await _rows(rt, sql, rows)
    async with _session(rt) as call:
        env = await call("rules_test", expr=expr)
        assert env["ok"] is False and env["data"] == {"expr": expr, "parsed": True, "evaluated": False}
        assert _missing(env, "eval", "data_unavailable")


@pytest.mark.parametrize("bad", ["abc", b"\xff\xfe"])
async def test_rules_test_unreadable_latest_funding_reads_as_no_value(rt, bad):
    # The metric reads a string / blob as "no value" (None, falsy), like the alerts job does; no exception.
    await _rows(rt, FUNDING_SQL, _funding(0.1, bad))
    async with _session(rt) as call:
        env = await call("rules_test", expr='funding("binanceusdm","BTC")')
        assert env["ok"] is True and env["data"]["value"] is None and env["data"]["truthy"] is False


async def test_rules_test_extreme_but_valid_values_unchanged(rt):
    await _rows(rt, FUNDING_SQL, _funding(0.1, HUGE))
    async with _session(rt) as call:
        env = await call("rules_test")
        assert env["ok"] is True and "funding(venue, base)" in env["data"]["available_metrics"]
        env = await call("rules_test", expr='funding("binanceusdm","BTC")')
        assert env["ok"] is True and env["data"]["value"] == HUGE and env["missing"] == []
        env = await call("rules_test", expr="1e400 > 1e308")
        assert env["ok"] is True and env["data"]["value"] is True
        env = await call("rules_test", expr='"' + "m" * 9_998 + '"')  # 10 000 characters: the DSL bound since item 301
        assert env["ok"] is True and len(env["data"]["value"]) == 9_998
        env = await call("rules_test", expr="1 +")
        assert env["ok"] is False and env["data"] == {"expr": "1 +", "parsed": False} and _missing(env, "expr", "data_unavailable")


# ---------- calculators: odds_devig, option_calc, fib_retracement, strategy_cash_and_carry ----------

ODDS = {"odds": [1.9, 2.1]}
OPTION = {"forward": 100.0, "strike": 105.0, "days": 30.0, "iv": 0.5}
FIB = {"p0": 100.0, "l_point": 90.0}
CARRY = {"spot_price": 100.0, "futures_price": 101.0, "days": 30.0, "iv": 0.5}


def _boom(exc):
    def f(*_a, **_k):
        raise exc("numeric failure")

    return f


@pytest.mark.parametrize(
    ("exc", "reason"), [(ValueError, "config"), (ZeroDivisionError, "invalid_input"), (OverflowError, "invalid_input")]
)
async def test_odds_devig_analytics_error_goes_to_missing(rt, monkeypatch, exc, reason):
    monkeypatch.setattr(odds_mod, "devig", _boom(exc))
    async with _session(rt) as call:
        env = await call("odds_devig", **ODDS)
        assert env["ok"] is False and env["data"] is None and _missing(env, "odds", reason, "numeric failure")


@pytest.mark.parametrize("fn", ["black76_greeks", "black76_price"])
@pytest.mark.parametrize("exc", NUMERIC_ERRORS)
async def test_option_calc_analytics_error_goes_to_missing(rt, monkeypatch, fn, exc):
    monkeypatch.setattr(quant_tools, fn, _boom(exc))
    async with _session(rt) as call:
        env = await call("option_calc", **OPTION)
        assert env["ok"] is False and env["data"] is None and _missing(env, "option", "invalid_input", "numeric failure")


@pytest.mark.parametrize("exc", NUMERIC_ERRORS)
async def test_strategy_cash_and_carry_analytics_error_goes_to_missing(rt, monkeypatch, exc):
    monkeypatch.setattr(strategy_tools, "summarise", _boom(exc))
    async with _session(rt) as call:
        env = await call("strategy_cash_and_carry", **CARRY)
        assert env["ok"] is False and env["data"] is None and _missing(env, "strategy", "invalid_input", "numeric failure")


def _nan_devig(_odds, method):
    return {"method": method, "margin": float("nan"), "implied_raw": [0.5, 0.5], "fair_probs": [0.5, 0.5], "fair_odds": [2.0, 2.0]}


def _nan_fib(applicable: bool):
    def f(p0, l_point, **_k):
        return {"p0": p0, "l": l_point, "diff_abs": 1.0, "diff_pct": 1.0, "direction": "drop", "applicable": applicable,
                "min_drop_pct": 2.0, "ladder": [{"level": 0.5, "price": float("nan")}]}  # fmt: skip

    return f


def _nan_carry(*_a, **_k):
    return {"breakevens": [], "max_profit": float("inf"), "max_loss": 0.0, "mtm_usd": 0.0, "pop": None, "greeks": {}}


@pytest.mark.parametrize(
    ("module", "fn", "fake", "tool", "args"),
    [
        (odds_mod, "devig", _nan_devig, "odds_devig", ODDS),
        (quant_tools, "black76_price", lambda *_a: float("nan"), "option_calc", OPTION),
        (quant_tools, "summarise_retracement", _nan_fib(True), "fib_retracement", FIB),
        (quant_tools, "summarise_retracement", _nan_fib(False), "fib_retracement", FIB),
        (strategy_tools, "summarise", _nan_carry, "strategy_cash_and_carry", CARRY),
    ],
    ids=["odds", "option", "fib_applicable", "fib_not_applicable", "carry"],
)
async def test_calculator_non_finite_analytics_result_is_not_ok(rt, monkeypatch, module, fn, fake, tool, args):
    # fib_not_applicable answered ok=true with NaN / Infinity before item 289 (only the applicable branch was checked).
    monkeypatch.setattr(module, fn, fake)
    async with _session(rt) as call:
        env = await call(tool, **args)
        assert env["ok"] is False and env["data"] is None and _missing(env, "result", "invalid_input")


@pytest.mark.parametrize(
    ("tool", "args"),
    [("odds_devig", {"odds": [2.0, v]}) for v in ("inf", "1e400", "-inf")]
    + [("option_calc", OPTION | {k: v}) for k in ("forward", "strike", "days") for v in ("inf", "1e400")]
    + [("fib_retracement", FIB | {k: v}) for k in ("p0", "l_point") for v in ("inf", "1e400")]
    + [("fib_retracement", FIB | {"levels": [0.5, v]}) for v in ("inf", "1e400", "nan")]
    + [
        ("strategy_cash_and_carry", CARRY | {k: v}) for k in ("spot_price", "futures_price", "days", "iv", "size") for v in ("inf", "1e400")
    ],
)
async def test_calculator_non_finite_input_is_invalid(rt, tool, args):
    async with _session(rt) as call:
        env = await call(tool, **args)
        assert env["ok"] is False and env["data"] is None and _missing(env, "input", "invalid_input")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("fib_retracement", {"p0": HUGE, "l_point": 0.99 * HUGE, "levels": [HUGE]}),  # Infinity with ok=true before item 289
        ("fib_retracement", {"p0": HUGE, "l_point": 1e-300, "levels": [HUGE]}),
        ("strategy_cash_and_carry", CARRY | {"spot_price": HUGE, "futures_price": HUGE, "size": HUGE}),
        ("strategy_cash_and_carry", CARRY | {"spot_price": 1e-308, "futures_price": HUGE}),
    ],
    ids=["fib_not_applicable_overflow", "fib_applicable_overflow", "carry_huge", "carry_basis_overflow"],
)
async def test_calculator_overflowing_result_is_not_ok(rt, tool, args):
    async with _session(rt) as call:
        env = await call(tool, **args)
        assert env["ok"] is False and env["data"] is None and _missing(env, "result", "invalid_input")


async def test_calculators_extreme_but_finite_inputs_answer(rt):
    # LONG: no tool caps the list length and every step over it is linear, so 10k elements exercise the same code as
    # 100k did (item 304: 100k cost ~4.5 s, mostly round() of 1e308-sized fib levels and the non-finite scans).
    async with _session(rt) as call:
        env = await call("odds_devig", odds=[HUGE, 1.01])
        assert env["ok"] is True and env["data"]["fair_probs"] == [0.0, 1.0]
        env = await call("odds_devig", odds=[10**20, 1.5], method="power")
        assert env["ok"] is True and env["missing"] == []
        env = await call("odds_devig", odds=[2.0] * LONG)
        assert env["ok"] is True and len(env["data"]["fair_probs"]) == LONG
        env = await call("option_calc", forward=HUGE, strike=1.0, days=1.0, iv=0.5)
        assert env["ok"] is True and env["data"]["price_usd"] == pytest.approx(HUGE) and env["data"]["delta"] == 1.0
        env = await call("option_calc", **OPTION | {"days": 10**20})
        assert env["ok"] is True and env["data"]["price_in_underlying"] == pytest.approx(1.0)
        env = await call("fib_retracement", p0=HUGE, l_point=1.0, levels=[0.5] * LONG)
        assert env["ok"] is True and len(env["data"]["ladder"]) == LONG and env["data"]["applicable"] is True
        env = await call("strategy_cash_and_carry", **CARRY | {"spot_price": HUGE, "futures_price": 1.0})
        assert env["ok"] is True and env["data"]["basis_pct"] == pytest.approx(-100.0)
        env = await call("strategy_cash_and_carry", **CARRY | {"days": 10**20, "iv": HUGE})
        assert env["ok"] is True and env["data"]["basis_abs"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("tool", "args"),
    [("odds_devig", ODDS), ("option_calc", OPTION), ("fib_retracement", FIB), ("strategy_cash_and_carry", CARRY)],
)
async def test_calculator_needs_no_store_or_venue(tool, args):
    # No store (db_path empty), every HTTP request 500 and every ccxt call failing: the calculator still answers.
    calls: list[str] = []
    rt = failing_runtime("down", "", calls)
    try:
        async with _session(rt) as call:
            env = await call(tool, **args)
    finally:
        await rt.aclose()
    assert calls == [] and env["ok"] is True and env["missing"] == [] and env["evidence"] == []
