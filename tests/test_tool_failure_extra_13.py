"""Invariant 1: the three open gaps of BACKLOG 6.53 nobody had checked (item 301).

(a) range_detect / range_setup / market_regime with `source='store'` handed the ohlcv_hist rows to the analytics
unchecked: a text / blob price raised ValueError out of the tool, an inf close gave Infinity / NaN in the reply, and a
text / blob / 1e20 timestamp or a 1e308 price went into the ranges and the regime as if it were a bar. Now such a bar is
skipped with a warning (the result equals the one without that bar); all bars corrupt -> ok=false.

(b) Integer time parameters: journal_add / journal_close / positions_add / positions_close (`reject_bad_ts`), events_add
and plan_add echoed the value in their invalid_input message, so a 5000-digit integer raised ValueError (str() limit of
4300 digits) out of the tool. A negative opened_ts / closed_ts (before 1970) was stored with ok=true, and a negative
since_ts_ms / until_ts_ms of ohlcv_history read as "no bars stored". Now all of them are ok=false, invalid_input.

(c) The rules DSL accepted `1 + 1 + ... + 1` with 5000 terms: rules_add stored it and every evaluation raised
RecursionError, which the alerts job swallowed silently on each tick; a 50k-term comparison chain held the event loop
~0.5 s per evaluation. Now an expression over 10 000 characters or 100 levels deep is an expression error at compile time
(rules_add / rules_test answer ok=false), and the alerts job logs a refused stored expression once per rule. Parsing
alone cost ~1.5 s per megabyte of tokens on every tick, so the bound is on characters: the megabyte string literal item
289 kept as "extreme but valid" is refused now too (test_tool_failure_extra_7 checks a 10 000-character one).

Each MCP test makes its calls and store checks in one session (the in-memory store is reopened empty on every connection).
"""

from __future__ import annotations

import logging
import math
import time

import pytest

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_7 import _missing
from test_tool_failure_extra_10 import _rows, _session
from tradedesk.alerts.engine import evaluate_dsl_rules
from tradedesk.config import Settings
from tradedesk.rules.dsl import MAX_DEPTH, MAX_EXPR_LEN, DslError, compile_expr, evaluate
from tradedesk.runtime import Runtime
from tradedesk.store import Store
from tradedesk.tools._util import first_nonfinite, shown_int

NOW_TS = int(NOW.timestamp())
HUGE = 1e308
INF = float("inf")


@pytest.fixture
async def rt(state, migrated_db):
    # A copy of the migrated template instead of ":memory:" (a fresh in-memory store migrates for ~0.2 s per test).
    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, db_path=str(migrated_db)), transports=make_transports(state), exchange_factory=FakeEx
    )
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


# ---------- (a) corrupt stored bars in range_detect / range_setup / market_regime ----------

BAR_SQL = "INSERT INTO ohlcv_hist VALUES (?,?,?,?,?,?,?,?,?)"
N_BARS = 60
BAD_IDX = 30
COLS = {"ts": 3, "open": 4, "high": 5, "low": 6, "close": 7}


def _bar(i: int) -> list:
    c = 100 + 3 * math.sin(i / 3)
    return ["binanceusdm", "BTC/USDT:USDT", "1h", (NOW_TS - (N_BARS - i) * 3600) * 1000, c, c + 1, c - 1, c + 0.2, 10.0]


BAD_BARS = {
    **{f"close_{k}": ("close", v) for k, v in (("text", "abc"), ("blob", b"\xff"), ("inf", INF), ("neg_inf", -INF), ("1e308", HUGE))},
    **{f"close_{k}": ("close", v) for k, v in (("zero", 0.0), ("negative", -5.0), ("1e13", 1e13))},
    "open_text": ("open", "n/a"),
    "high_1e308": ("high", HUGE),
    "low_neg_1e308": ("low", -HUGE),
    "ts_1e20": ("ts", 1e20),  # SQLite keeps 10**20 as REAL in an INTEGER column
    "ts_text": ("ts", "abc"),  # TEXT sorts above every number: it came out as the newest bar
    "ts_blob": ("ts", b"\x00"),
    "ts_beyond_9999": ("ts", 2**62),  # an integer, but no datetime in ms
    "ts_fraction": ("ts", (NOW_TS - 1800) * 1000 + 0.5),
}  # fmt: skip

STORE_TOOLS = {
    "range_detect": {},
    "range_setup": {"source": "store", "timeframe": "1h"},
    "market_regime": {},
}
ARGS = {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"}


def _skip_warnings(env: dict) -> list[str]:
    return [w for w in env["warnings"] if "stored bars skipped" in w]


@pytest.mark.parametrize("tool", STORE_TOOLS)
@pytest.mark.parametrize(("col", "value"), BAD_BARS.values(), ids=BAD_BARS.keys())
async def test_store_bar_tools_skip_a_corrupt_bar(rt, tool, col, value):
    # Before item 301: ValueError (text / blob price), Infinity / NaN (inf), or the garbage bar used as is.
    good = [_bar(i) for i in range(N_BARS) if i != BAD_IDX]
    bad = _bar(BAD_IDX)
    bad[COLS[col]] = value
    async with _session(rt) as call:
        await _rows(rt, BAR_SQL, good)
        clean = await call(tool, **ARGS, **STORE_TOOLS[tool])
        assert _skip_warnings(clean) == []
        await _rows(rt, BAR_SQL, [bad])
        env = await call(tool, **ARGS, **STORE_TOOLS[tool])
        assert env["ok"] == clean["ok"] and env["data"] == clean["data"] and env["missing"] == clean["missing"], str(env)[:600]
        assert first_nonfinite(env["data"]) is None
        w = _skip_warnings(env)
        assert len(w) == 1 and w[0].startswith("1 stored bars skipped"), env["warnings"]


@pytest.mark.parametrize("tool", STORE_TOOLS)
async def test_store_bar_tools_all_corrupt_is_not_ok(rt, tool):
    rows = []
    for i in range(30):
        row = _bar(i)
        row[COLS["close"]] = ("abc", b"\xff", INF, HUGE)[i % 4]
        rows.append(row)
    async with _session(rt) as call:
        await _rows(rt, BAR_SQL, rows)
        env = await call(tool, **ARGS, **STORE_TOOLS[tool])
        assert env["ok"] is False and env["data"] is None
        assert any("only 0 bars" in m["detail"] for m in env["missing"]), env["missing"]
        assert any(w.startswith("30 stored bars skipped") for w in env["warnings"]), env["warnings"]


@pytest.mark.parametrize("tool", STORE_TOOLS)
async def test_store_bar_tools_valid_bars_unchanged(rt, tool):
    # A text volume is not read by the ranges or the regime: the bar is kept, no warning; a negative ts is outside the
    # query (ts >= 0) as before.
    rows = [_bar(i) for i in range(N_BARS)]
    rows[5][8] = "n/a"
    async with _session(rt) as call:
        await _rows(rt, BAR_SQL, rows)
        await _rows(rt, BAR_SQL, [[*_bar(0)[:3], -5, 1.0, 2.0, 0.5, 1.5, 1.0]])
        env = await call(tool, **ARGS, **STORE_TOOLS[tool])
        assert _skip_warnings(env) == []
        if tool == "market_regime":
            assert env["ok"] is True and env["data"]["count"] == N_BARS
        elif tool == "range_detect":
            assert env["ok"] is True and env["data"]["n_bars_evaluated"] == N_BARS


# ---------- (b) integer time bounds: 10**20, negative, a 5000-digit integer ----------

TIME_VALUES = {"1e20": 10**20, "neg_1e20": -(10**20), "minus_one": -1, "1e400": 10**400, "1e5000": 10**5000, "neg_1e5000": -(10**5000)}

PLAN = dict(
    exchange="binance", symbol="BTC/USDT:USDT", instrument="perp", side="long", entry_type="market",
    entry=100.0, stop=90.0, target=120.0, verdict="enter",
)  # fmt: skip

TIME_TOOLS = {
    "journal_add": ("journal_add", "opened_ts", "opened_ts", {"symbol": "X", "side": "long", "size": 1.0, "entry": 1.0}, "journal"),
    "journal_close": ("journal_close", "closed_ts", "closed_ts", {"id": 1, "exit": 1.0}, None),
    "positions_add": ("positions_add", "opened_ts", "opened_ts", {"instrument": "X", "kind": "perp", "side": "long", "size": 1.0}, "positions"),
    "positions_close": ("positions_close", "closed_ts", "closed_ts", {"id": 1}, None),
    "plan_add": ("plan_add", "expires_ts", "plan", PLAN, "trade_plan"),
    "events_add": ("events_add", "ts_ms", "ts_ms", {"kind": "fomc", "label": "x"}, "events"),
    "ohlcv_history_since": ("ohlcv_history", "since_ts_ms", "input", ARGS, None),
    "ohlcv_history_until": ("ohlcv_history", "until_ts_ms", "input", ARGS, None),
}  # fmt: skip


async def _count(rt, table: str) -> int:
    cur = await rt.store.conn.execute(f"SELECT COUNT(*) FROM {table}")
    return (await cur.fetchone())[0]


@pytest.mark.parametrize("value", TIME_VALUES.values(), ids=TIME_VALUES.keys())
@pytest.mark.parametrize(("tool", "param", "field", "kw", "table"), TIME_TOOLS.values(), ids=TIME_TOOLS.keys())
async def test_time_parameter_out_of_range_is_invalid_input(rt, tool, param, field, kw, table, value):
    # Before item 301: a 5000-digit value raised ValueError out of the message, -1 was stored (journal / positions) or
    # read as "no bars" (ohlcv_history).
    async with _session(rt) as call:
        env = await call(tool, **kw, **{param: value})
        assert env["ok"] is False and _missing(env, field, "invalid_input"), env["missing"]
        assert all(len(m["detail"]) < 300 for m in env["missing"])  # a huge value is not echoed
        if table is not None:
            assert await _count(rt, table) == 0


async def test_time_parameters_in_range_still_accepted(rt):
    async with _session(rt) as call:
        env = await call("journal_add", symbol="X", side="long", size=1.0, entry=1.0, opened_ts=0)
        assert env["ok"] is True and env["data"]["opened_ts"] == 0
        env = await call("journal_close", id=env["data"]["id"], exit=2.0, closed_ts=NOW_TS)
        assert env["ok"] is True
        env = await call("positions_add", instrument="X", kind="perp", side="long", size=1.0, opened_ts=NOW_TS - 60)
        assert env["ok"] is True
        env = await call("ohlcv_history", **ARGS, since_ts_ms=0, until_ts_ms=2**63 - 1)
        assert env["ok"] is False and _missing(env, "ohlcv_hist", "data_unavailable")  # an empty store, not invalid_input
        env = await call("plan_add", **PLAN, expires_ts=NOW_TS + 86_400)
        assert env["ok"] is True


def test_shown_int_bounds_the_echo():
    assert shown_int(-1) == "-1" and shown_int(10**20) == "100000000000000000000" and shown_int(10**100 - 1) == "9" * 100
    assert shown_int(10**100) == shown_int(-(10**5000)) == "an integer beyond +-1e100"


# ---------- (c) deep / long rule expressions ----------


def _chain(n: int, op: str = "+") -> str:
    return op.join(["1"] * n)


TOO_BIG = {
    "chain_101": _chain(MAX_DEPTH + 1),
    "chain_5000": _chain(5000),  # compiled, then RecursionError on every evaluation before item 301
    "chain_50k": _chain(50_000),
    "unary_minus_200": "-" * 200 + "1",
    "not_150": "not " * 150 + "1",
    "nested_calls_120": "funding(" * 120 + '"x"' + ")" * 120,
    "nested_parens_mixed": "-(" * 120 + "1" + ")" * 120,
    "long_flat_compare": _chain(MAX_EXPR_LEN // 2 + 1, "<"),  # flat, but beyond 10 000 characters
    "long_flat_and": _chain(50_000, " and "),
    "padding": "1" + " " * MAX_EXPR_LEN,
}


@pytest.mark.parametrize("expr", TOO_BIG.values(), ids=TOO_BIG.keys())
async def test_compile_refuses_too_deep_or_too_long(expr):
    t0 = time.monotonic()
    with pytest.raises(DslError, match="longer than|nested deeper|too long or too deeply"):
        compile_expr(expr)
    with pytest.raises(DslError):  # the source-string path compiles first
        await evaluate(expr, {})
    assert time.monotonic() - t0 < 5


async def test_dsl_bounds_themselves_still_evaluate():
    assert await evaluate(_chain(MAX_DEPTH), {}) == MAX_DEPTH  # depth exactly at the bound
    assert await evaluate("-" * (MAX_DEPTH - 1) + "1", {}) == -1
    assert await evaluate("1" + " " * (MAX_EXPR_LEN - 1), {}) == 1  # length exactly at the bound
    assert await evaluate("(" * 150 + "2 > 1" + ")" * 150, {}) is True  # parentheses add no level
    flat = _chain(MAX_EXPR_LEN // 6, " and ")  # ~10 000 characters, 1666 operands, one level
    assert len(flat) <= MAX_EXPR_LEN and await evaluate(flat, {}) == 1
    flat_cmp = _chain(MAX_EXPR_LEN // 3, "<=")
    assert len(flat_cmp) <= MAX_EXPR_LEN and await evaluate(flat_cmp, {}) is True


@pytest.mark.parametrize(("op", "width"), [(" and ", 6), ("<=", 3)], ids=["flat_and", "flat_compare"])
async def test_flat_expressions_at_the_length_bound_evaluate_in_linear_time(op, width):
    # Relative, not wall-clock: an absolute "< 2 s" failed at 2.65 s in a loaded sequential run (item 372). The chain at
    # the length bound has 8x the operands of the small one: linear evaluation measures ~8-10x, quadratic ~50-60x
    # (simulated). Small and big runs are interleaved and the best of each kept, so a slow machine slows both alike.
    big_n = MAX_EXPR_LEN // width
    small, big = _chain(big_n // 8, op), _chain(big_n, op)
    assert len(big) <= MAX_EXPR_LEN
    best_small = best_big = math.inf
    for _ in range(7):
        t0 = time.perf_counter()
        await evaluate(small, {})
        t1 = time.perf_counter()
        await evaluate(big, {})
        t2 = time.perf_counter()
        best_small, best_big = min(best_small, t1 - t0), min(best_big, t2 - t1)
    assert best_big < 24 * best_small, (best_small, best_big)


@pytest.mark.parametrize("key", ["chain_5000", "unary_minus_200", "long_flat_compare"])
async def test_rules_add_and_rules_test_refuse_deep_expression(rt, key):
    expr = TOO_BIG[key]
    async with _session(rt) as call:
        env = await call("rules_add", name="deep", expr=expr)
        assert env["ok"] is False and env["data"] is None and _missing(env, "expr", "data_unavailable")
        assert (await call("rules_list"))["data"]["count"] == 0
        env = await call("rules_test", expr=expr)
        assert env["ok"] is False and env["data"] == {"expr": expr, "parsed": False} and _missing(env, "expr", "data_unavailable")


async def test_alerts_job_skips_deep_stored_rules_with_one_log_each(caplog):
    # Rules another writer stored past rules_add: the job does not raise, answers fast, fires the good rule and logs
    # each refused expression once per process (it was silent on every tick before item 301).
    s = Store(":memory:")
    await s.init()
    bad = {f"i301_{k}": TOO_BIG[k] for k in ("chain_5000", "chain_50k", "not_150", "long_flat_and")}
    try:
        for name, expr in {**bad, "i301_ok": "2 > 1"}.items():
            await s.rule_insert(name=name, expr=expr, message="", cooldown_seconds=60, enabled=True, created_ts=NOW_TS)
        caplog.set_level(logging.WARNING, logger="tradedesk.alerts.engine")
        t0 = time.monotonic()
        fired = await evaluate_dsl_rules(s, now_ts=NOW_TS)
        assert time.monotonic() - t0 < 5
        assert [f.rule_id for f in fired] == ["db:i301_ok"]
        for name in bad:
            hits = [r.getMessage() for r in caplog.records if f"'db:{name}' skipped: expression error" in r.getMessage()]
            assert len(hits) == 1, (name, hits)
        caplog.clear()
        assert [f.rule_id for f in await evaluate_dsl_rules(s, now_ts=NOW_TS)] == ["db:i301_ok"]  # no cooldown row stored
        assert not [r for r in caplog.records if "i301_" in r.getMessage()]
    finally:
        await s.aclose()


async def test_alerts_job_data_error_stays_silent(caplog):
    # `None > 0.3` while a metric has no value is data, not the expression: not logged as an expression error.
    s = Store(":memory:")
    await s.init()
    try:
        expr = 'funding("binanceusdm","BTC") > 0.3'
        await s.rule_insert(name="i301_nodata", expr=expr, message="", cooldown_seconds=60, enabled=True, created_ts=NOW_TS)
        caplog.set_level(logging.WARNING, logger="tradedesk.alerts.engine")
        assert await evaluate_dsl_rules(s, now_ts=NOW_TS) == []
        assert not [r for r in caplog.records if "expression error" in r.getMessage()]
    finally:
        await s.aclose()
