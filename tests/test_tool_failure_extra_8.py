"""Invariant 1: two open defects of BACKLOG 6.53 (item 291).

(a) `**` in the rules DSL was unbounded: `9 ** 9 ** 9` computed an integer of hundreds of millions of digits and blocked
the event loop (rules_test, and the alerts job on a stored rule). Now an exponent beyond +-1000, an integer result beyond
4096 bits or a complex result is an expression error: compile_expr refuses it over literals (rules_add / rules_test answer
ok=false, nothing stored), the evaluator over metric values (rules_test ok=false, the alerts job skips the rule). The
alerts job also raised OverflowError on a truthy integer beyond the float range (`9 ** 999`); such a rule is skipped.

(b) An id beyond SQLite INTEGER (10**20) raised OverflowError out of the store lookup in positions_close, rules_disable,
journal_close, plan_add (rec_paper_id), carry_paper_status and carry_paper_close (plan_link was fixed in item 289). Now
ok=false with `invalid_input` on that id; 2**63 - 1 still reaches the store and is just not found.

Each test makes all its calls in one MCP session (the in-memory store is reopened empty on every connection).
"""

from __future__ import annotations

import time

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_7 import LIQ_SQL, _liq, _missing, _rows, _session
from tradedesk.alerts.engine import evaluate_dsl_rules
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.rules.dsl import MAX_INT_BITS, DslError, compile_expr, evaluate
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store import Store
from tradedesk.tools._util import reject_bad_id, reject_bad_ts

NOW_TS = int(NOW.timestamp())
SQLITE_INT_MAX = 2**63 - 1
SQLITE_INT_MIN = -(2**63)
TOO_BIG = [10**20, 2**63, 10**400]
TOO_SMALL = [-(10**20), -(2**63) - 1, -(10**400)]


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


# ---------- (a) `**` in the rules DSL ----------

HUGE_POW = ["9 ** 9 ** 9", "2 ** 5000", "10 ** 1001", "(2 ** 1000) ** 5", "(10 ** 1000) * (10 ** 1000)", "(-8) ** 0.5"]


@pytest.mark.parametrize("expr", HUGE_POW)
def test_compile_refuses_oversized_literal_pow(expr):
    t0 = time.monotonic()
    with pytest.raises(DslError):
        compile_expr(expr)
    assert time.monotonic() - t0 < 5  # computed nothing big


@pytest.mark.parametrize(
    ("expr", "names"),
    [
        ("x ** y", {"x": 9, "y": 9**9}),
        ("x ** 2", {"x": 2**4000}),
        ("x * x", {"x": 2**4000}),
        ("x ** 0.5", {"x": -8}),
        ("x + 0.5", {"x": 2**4000}),  # a big int mixed with a float: OverflowError, now a DslError
        ("x * 1.5", {"x": 2**4000}),
        ("x ** 0.5", {"x": 2**4000}),
    ],
)
async def test_evaluate_refuses_oversized_pow_from_names(expr, names):
    t0 = time.monotonic()
    with pytest.raises(DslError):
        await evaluate(compile_expr(expr), {"names": names})
    assert time.monotonic() - t0 < 5


async def test_dsl_pow_within_bounds_unchanged():
    assert await evaluate("10 ** 1000", {}) == 10**1000 and (10**1000).bit_length() <= MAX_INT_BITS
    assert await evaluate("2 ** -1000", {}) == pytest.approx(2.0**-1000)
    assert await evaluate("9 ** 999", {}) == 9**999
    assert await evaluate("x ** 2 > 0.01", {"names": {"x": 0.3}}) is True
    assert await evaluate("1.5 ** 1000", {}) == pytest.approx(1.5**1000)
    with pytest.raises(ZeroDivisionError):  # other literal errors are still left to evaluation
        await evaluate(compile_expr("0 ** -1"), {})


@pytest.mark.parametrize("expr", HUGE_POW)
async def test_rules_add_refuses_oversized_pow(rt, expr):
    async with _session(rt) as call:
        env = await call("rules_add", name="huge", expr=expr)
        assert env["ok"] is False and env["data"] is None and _missing(env, "expr", "data_unavailable")
        assert (await call("rules_list"))["data"]["count"] == 0


@pytest.mark.parametrize("expr", HUGE_POW)
async def test_rules_test_refuses_oversized_literal_pow(rt, expr):
    async with _session(rt) as call:
        env = await call("rules_test", expr=expr)
        assert env["ok"] is False and env["data"] == {"expr": expr, "parsed": False} and _missing(env, "expr", "data_unavailable")


async def test_rules_test_refuses_oversized_pow_from_a_metric(rt):
    await _rows(rt, LIQ_SQL, _liq(5000.0))
    async with _session(rt) as call:
        env = await call("rules_test", expr="2 ** liquidations_total(60) > 1")
        assert env["ok"] is False and env["data"]["evaluated"] is False and _missing(env, "eval", "data_unavailable", "exponent")
        env = await call("rules_test", expr="liquidations_total(60) ** 2")  # within bounds: unchanged
        assert env["ok"] is True and env["data"]["value"] == pytest.approx(25e6)


async def test_alerts_job_skips_oversized_pow_rules():
    s = Store(":memory:")
    await s.init()
    try:
        for name, expr in [("pow", "9 ** 9 ** 9"), ("beyond_float", "9 ** 999"), ("complex", "(-8) ** 0.5"), ("ok", "2 ** 10 > 3")]:
            await s.rule_insert(name=name, expr=expr, message="", cooldown_seconds=60, enabled=True, created_ts=NOW_TS)
        t0 = time.monotonic()
        fired = await evaluate_dsl_rules(s, now_ts=NOW_TS)  # `9 ** 999` raised OverflowError out of the job before
        assert time.monotonic() - t0 < 5
        assert [f.rule_id for f in fired] == ["db:ok"]
    finally:
        await s.aclose()


# ---------- (b) id beyond SQLite INTEGER ----------


@pytest.mark.parametrize("big", TOO_BIG)
async def test_positions_close_id_beyond_sqlite_integer(rt, big):
    async with _session(rt) as call:
        env = await call("positions_close", id=big)
        assert env["ok"] is False and env["data"] is None and _missing(env, "id", "invalid_input", "SQLite INTEGER")
        env = await call("positions_close", id=SQLITE_INT_MAX)
        assert env["ok"] is False and _missing(env, "position", "data_unavailable", "not found")


@pytest.mark.parametrize("big", TOO_BIG)
async def test_rules_disable_id_beyond_sqlite_integer(rt, big):
    async with _session(rt) as call:
        assert (await call("rules_add", name="r", expr="1 > 0"))["ok"] is True
        for kw in ({}, {"enable": True}, {"delete": True}):
            env = await call("rules_disable", id=big, **kw)
            assert env["ok"] is False and env["data"] is None and _missing(env, "id", "invalid_input", "SQLite INTEGER")
        env = await call("rules_disable", id=SQLITE_INT_MAX)
        assert env["ok"] is False and _missing(env, "rule", "data_unavailable", "not found")
        rules = (await call("rules_list"))["data"]["rules"]
        assert [(x["name"], x["enabled"]) for x in rules] == [("r", True)]


@pytest.mark.parametrize("big", TOO_BIG)
async def test_journal_close_id_beyond_sqlite_integer(rt, big):
    async with _session(rt) as call:
        for kw in ({"exit": 100.0}, {"cancelled": True}):
            env = await call("journal_close", id=big, **kw)
            assert env["ok"] is False and env["data"] is None and _missing(env, "id", "invalid_input", "SQLite INTEGER")
        env = await call("journal_close", id=SQLITE_INT_MAX, exit=100.0)
        assert env["ok"] is False and _missing(env, "journal", "data_unavailable", "not found")


PLAN = {
    "exchange": "deribit",
    "symbol": "BTC/USD:BTC",
    "instrument": "option",
    "side": "long",
    "entry_type": "market",
    "entry": 100.0,
    "stop": 95.0,
    "target": 115.0,
    "expires_ts": NOW_TS + 86400,
    "verdict": "enter",
}


@pytest.mark.parametrize("big", TOO_BIG)
async def test_plan_add_rec_paper_id_beyond_sqlite_integer(rt, big):
    async with _session(rt) as call:
        env = await call("plan_add", **PLAN, rec_paper_id=big)
        assert env["ok"] is False and env["data"] is None and _missing(env, "rec_paper_id", "invalid_input", "SQLite INTEGER")
        env = await call("plan_add", **PLAN, rec_paper_id=SQLITE_INT_MAX)
        assert env["ok"] is False and _missing(env, "rec_paper_id", "invalid_input", "not found")
        assert (await call("plan_list"))["data"]["count"] == 0
        assert (await call("plan_add", **PLAN))["ok"] is True  # without the link: unchanged


@pytest.mark.parametrize("big", TOO_BIG)
async def test_carry_paper_id_beyond_sqlite_integer(rt, big):
    async with _session(rt) as call:
        env = await call("carry_paper_status", id=big)
        assert env["ok"] is False and env["data"] is None and _missing(env, "id", "invalid_input", "SQLite INTEGER")
        env = await call("carry_paper_close", id=big, reason="manual")
        assert env["ok"] is False and env["data"] is None and _missing(env, "id", "invalid_input", "SQLite INTEGER")
        for tool, kw in (("carry_paper_status", {}), ("carry_paper_close", {"reason": "manual"})):
            env = await call(tool, id=SQLITE_INT_MAX, **kw)
            assert env["ok"] is False and _missing(env, "carry_paper", "data_unavailable", "not found")


@pytest.mark.parametrize("small", TOO_SMALL)
def test_reject_bad_id_below_sqlite_integer(small):
    # sqlite3 raises OverflowError below -2**63 as well; the helper checks the full range, not only the upper bound.
    env = reject_bad_id(Builder(), id=small)
    assert env is not None and env["ok"] is False and _missing(env, "id", "invalid_input", "SQLite INTEGER")


def test_reject_bad_id_sqlite_integer_edges_pass():
    for v in (SQLITE_INT_MIN, SQLITE_INT_MAX, None, 1):
        assert reject_bad_id(Builder(), id=v, rec_paper_id=v) is None


ID_TOOLS = [
    ("positions_close", "id", {}),
    ("rules_disable", "id", {}),
    ("journal_close", "id", {"exit": 100.0}),
    ("plan_add", "rec_paper_id", PLAN),
    ("carry_paper_status", "id", {}),
    ("carry_paper_close", "id", {"reason": "manual"}),
]


@pytest.mark.parametrize("small", TOO_SMALL)
async def test_negative_id_stopped_by_the_schema(rt, small):
    # Every id parameter is Field(ge=1): a negative id is a validation error of the call, never reaches the store and
    # raises nothing out of the server; the session stays usable.
    async with Client(build_server(rt)[0]) as c:
        for tool, field, kw in ID_TOOLS:
            res = await c.call_tool(tool, {**kw, field: small})
            assert res.is_error and "greater than or equal to 1" in res.content[0].text
        assert not (await c.call_tool("rules_list", {})).is_error


@pytest.mark.parametrize("ts", [-(10**20), -(2**63) - 1, -(2**63), -(10**12)])
def test_reject_bad_ts_negative_lower_bound(ts):
    # Item 289 had no lower-bound gap: datetime refuses anything before year 1 (~ -6.2e10 s), well inside SQLite INTEGER.
    env = reject_bad_ts(Builder(), "since_ts", ts)
    assert env is not None and env["ok"] is False and _missing(env, "since_ts", "invalid_input", "datetime range")
