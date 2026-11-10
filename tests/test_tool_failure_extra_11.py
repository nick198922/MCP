"""Invariant 1: the open findings of BACKLOG 6.53 left after item 296 (item 299).

(a) plan_review raised OverflowError in its summary when several resolved plans of another writer had r_result ~1e308
(`statistics.fmean` -> "intermediate overflow in fsum"), so the tool failed. Now a plan with |r_result| over 1e9 is left
out of the summary and the lists with a warning naming it; the summary has no inf / NaN.

(b) The option branch of `review_open` (a plan linked to a settled rec_paper row) took settled_ts / opened_ts /
delivery_price / r_worst_case of the paper trade unchecked: int() of text or inf raised ValueError / OverflowError, a
1e308 / 1e20 time overflowed the store binding, and a text / inf / 1e308 price or R was written into the plan. Any of
them failed plan_review and the hourly plan_resolve job for every other plan. Now such a plan is skipped with a warning
and stays open; the other linked plan is still scored.

(c) The rules DSL bounded `**` (item 291) but not sequences: `"a" * 10**9` allocated a gigabyte in rules_test and in the
alerts job on a stored rule, and so did `"%999999999d" % 1`. Now a str / bytes / list / tuple result beyond 100 000 items
is an expression error (a repetition is checked before it is computed) and `%` on a string is refused; compile_expr
refuses it over literals (rules_add / rules_test answer ok=false). The alerts job also formatted the rule's message
unchecked: `{value[0]}` / `{value.x}` raised TypeError / AttributeError out of the whole job and `{value:999999999}` built
a gigabyte string; such a template is now left unformatted.

Each test makes its calls and store checks in one MCP session (the in-memory store is reopened empty on every connection).
"""

from __future__ import annotations

import math
import time

import pytest

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_7 import _missing
from test_tool_failure_extra_10 import GOOD_PLAN, PLAN_SQL, _plan, _plan_row, _rows, _session
from tradedesk.alerts.engine import _format_message, evaluate_dsl_rules
from tradedesk.collector.trade_plan import collect_plan_resolve
from tradedesk.config import Settings
from tradedesk.rules.dsl import MAX_SEQ_LEN, DslError, compile_expr, evaluate
from tradedesk.runtime import Runtime
from tradedesk.store import Store
from tradedesk.tools._util import first_nonfinite

NOW_TS = int(NOW.timestamp())
HUGE = 1e308
INF = float("inf")


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


# ---------- (a) plan_review summary on r_result ~1e308 ----------

RESOLVED_SQL = PLAN_SQL.replace("status)", "status, outcome, r_result, resolved_ts)").replace("?)", "?,?,?,?)")


def _resolved(pid: int, r_result: float, tags: str) -> tuple:
    return (*_plan(id=pid, status="resolved", tags=tags), "target", r_result, NOW_TS - 60 * pid)


def _all_stats(env: dict, key: str) -> dict:
    return env["data"][key]["by_verdict"]["enter"]


@pytest.mark.parametrize(("tags", "key"), [('["src:own"]', "summary"), ('["src:demo-shadow"]', "demo_shadow")], ids=["own", "shadow"])
@pytest.mark.parametrize("bad", [[HUGE, HUGE], [HUGE, -HUGE, HUGE], [1e300] * 5, [1e10, -1e10]], ids=["two", "mixed", "five", "1e10"])
async def test_plan_review_summary_skips_huge_r_results(rt, tags, key, bad):
    # fmean([1e308, 1e308]) raised OverflowError out of plan_review before item 299.
    await _rows(rt, RESOLVED_SQL, [_resolved(3 + i, v, tags) for i, v in enumerate(bad)] + [_resolved(20, 1.5, tags)])
    async with _session(rt) as call:
        env = await call("plan_review")
        s = _all_stats(env, key)
        assert env["ok"] is True and s["n"] == 1 and s["mean_r"] == pytest.approx(1.5), str(env)[:600]
        assert first_nonfinite(env["data"]) is None
        ids = str(list(range(3, 3 + len(bad)))[::-1])  # listed newest first
        assert any(w.startswith(f"{len(bad)} stored plan rows skipped") and ids in w for w in env["warnings"]), env["warnings"]
        if key == "summary":
            assert [p["id"] for p in env["data"]["recent_resolved"]] == [20]


async def test_plan_review_large_but_bounded_r_results_still_summarised(rt):
    # 1e9 R is the bound itself: kept, and the mean / CI of a few of them is finite.
    tags = '["src:own"]'
    await _rows(rt, RESOLVED_SQL, [_resolved(3, 1e9, tags), _resolved(4, -1e9, tags), _resolved(5, 1e9, tags)])
    async with _session(rt) as call:
        env = await call("plan_review")
        s = _all_stats(env, "summary")
        assert env["ok"] is True and s["n"] == 3 and s["mean_r"] == pytest.approx(1e9 / 3)
        assert s["ci95_mean_r"] is not None and all(math.isfinite(x) for x in s["ci95_mean_r"])
        assert not any("skipped" in w for w in env["warnings"]), env["warnings"]


# ---------- (b) the rec_paper branch of review_open ----------

REC = dict(
    currency="BTC", structure="long_straddle", expiry_ts=NOW_TS - 3600, spot=80000.0, forward=80000.0, t_years=0.01,
    legs=[[80000.0, True, 1.0, 0.5, 900.0]], premium_per_btc=900.0, entry_fees_per_btc=24.0, max_loss_per_btc=950.0,
    risk_basis="max_loss", size_btc=0.1, deposit_usd=10000.0, risk_pct=1.0, risk_usd=95.0, within_risk=True,
)  # fmt: skip


async def _settled_rec(rt) -> int:
    rid = await rt.store.rec_paper_insert(opened_ts=NOW_TS - 8 * 3600, **REC)
    await rt.store.rec_paper_settle(
        rid, settled_ts=NOW_TS - 3600, delivery_price=81000.0, pnl_usd=7.6, pnl_pct_spot=0.001, r_worst_case=0.08, r_study=0.08
    )
    return rid


BAD_REC = {
    **{f"settled_{k}": ("settled_ts", v) for k, v in (("text", "abc"), ("blob", b"\x00"), ("inf", INF), ("huge", HUGE), ("1e20", 1e20), ("null", None))},
    **{f"opened_{k}": ("opened_ts", v) for k, v in (("text", "abc"), ("blob", b"\xff"), ("inf", INF), ("huge", HUGE), ("1e20", 1e20))},
    **{f"delivery_{k}": ("delivery_price", v) for k, v in (("text", "abc"), ("blob", b"\xff"), ("inf", INF), ("huge", HUGE), ("1e20", 1e20), ("zero", 0.0), ("negative", -81000.0))},
    **{f"r_{k}": ("r_worst_case", v) for k, v in (("text", "abc"), ("blob", b"\xff"), ("inf", INF), ("nan_text", "NaN"), ("huge", HUGE), ("1e20", 1e20), ("neg_1e20", -1e20))},
}  # fmt: skip


async def _linked_pair(rt, col: str, value) -> None:
    good, bad = await _settled_rec(rt), await _settled_rec(rt)
    await rt.store.conn.execute(f"UPDATE rec_paper SET {col} = ? WHERE id = ?", (value, bad))
    await _rows(rt, PLAN_SQL, [_plan(id=1, instrument="option", rec_paper_id=good), _plan(id=2, instrument="option", rec_paper_id=bad)])


@pytest.mark.parametrize(("col", "value"), BAD_REC.values(), ids=BAD_REC.keys())
async def test_plan_review_skips_a_plan_with_a_corrupt_rec_paper(rt, col, value):
    await _linked_pair(rt, col, value)
    async with _session(rt) as call:
        env = await call("plan_review")
        assert env["ok"] is True and env["data"]["resolved_now"] == [1], str(env)[:600]
        assert any(w.startswith("plan 2 skipped: its linked rec_paper #2") and col in w for w in env["warnings"]), env["warnings"]
        by = {p["id"]: p for p in env["data"]["recent_resolved"]}
        assert by[1]["outcome"] == "expired" and by[1]["exit"] == 81000.0 and by[1]["r_result"] == pytest.approx(0.08)
        assert await _plan_row(rt, 2) == ("open", None, None)


@pytest.mark.parametrize(("col", "value"), BAD_REC.values(), ids=BAD_REC.keys())
async def test_plan_resolve_job_skips_a_plan_with_a_corrupt_rec_paper(rt, col, value):
    await _linked_pair(rt, col, value)
    assert await collect_plan_resolve(rt) == 1
    assert (await _plan_row(rt, 1))[0] == "resolved"
    assert await _plan_row(rt, 2) == ("open", None, None)


async def test_plan_review_rec_paper_null_price_and_r_still_score(rt):
    # NULL delivery_price / r_worst_case is no garbage: the plan resolves with a null exit / R, as before.
    rid = await _settled_rec(rt)
    await rt.store.conn.execute("UPDATE rec_paper SET delivery_price = NULL, r_worst_case = NULL WHERE id = ?", (rid,))
    await _rows(rt, PLAN_SQL, [GOOD_PLAN, _plan(id=2, instrument="option", rec_paper_id=rid)])
    async with _session(rt) as call:
        env = await call("plan_review")
        assert env["ok"] is True and sorted(env["data"]["resolved_now"]) == [1, 2]
        assert not any("skipped" in w for w in env["warnings"]), env["warnings"]


# ---------- (c) sequence size in the rules DSL ----------

HUGE_SEQ = [
    '"a" * 10 ** 9',
    '10 ** 9 * "a"',
    'b"a" * 10 ** 9',
    f'"ab" * {MAX_SEQ_LEN // 2 + 1}',
    '("a" * 1000) * 1000',
    f'("a" * {MAX_SEQ_LEN}) + "b"',
    '"%999999999d" % 1',
    '"%s" % 1',
]


@pytest.mark.parametrize("expr", HUGE_SEQ)
def test_compile_refuses_oversized_literal_sequence(expr):
    t0 = time.monotonic()
    with pytest.raises(DslError):
        compile_expr(expr)
    assert time.monotonic() - t0 < 5  # allocated nothing big


@pytest.mark.parametrize(
    ("expr", "names"),
    [
        ("x * n", {"x": "a", "n": 10**9}),
        ("n * x", {"x": "a", "n": 10**9}),
        ("x * n", {"x": [0], "n": 10**9}),
        ("x * n", {"x": (0, 1), "n": 10**9}),
        ("x * n", {"x": b"a", "n": 2**4000}),
        ("x + x", {"x": "a" * MAX_SEQ_LEN}),
        ("x % 1", {"x": "%999999999d"}),
    ],
)
async def test_evaluate_refuses_oversized_sequence_from_names(expr, names):
    t0 = time.monotonic()
    with pytest.raises(DslError):
        await evaluate(compile_expr(expr), {"names": names})
    assert time.monotonic() - t0 < 5


async def test_dsl_sequences_within_bounds_unchanged():
    assert await evaluate('"a" * 3 == "aaa"', {}) is True
    assert len(await evaluate(f'"ab" * {MAX_SEQ_LEN // 2}', {})) == MAX_SEQ_LEN
    assert await evaluate('"a" + "b"', {}) == "ab"
    assert await evaluate('"a" * -5', {}) == ""
    assert await evaluate("7 % 3", {}) == 1 and await evaluate("x % 0.5", {"names": {"x": 1.25}}) == pytest.approx(0.25)
    with pytest.raises(TypeError):  # other errors of a literal subtree are still left to evaluation
        await evaluate(compile_expr('-"a"'), {})
    with pytest.raises(TypeError):
        await evaluate(compile_expr('"a" + 1'), {})


@pytest.mark.parametrize("expr", HUGE_SEQ)
async def test_rules_add_and_rules_test_refuse_oversized_sequence(rt, expr):
    async with _session(rt) as call:
        env = await call("rules_add", name="huge", expr=expr)
        assert env["ok"] is False and env["data"] is None and _missing(env, "expr", "data_unavailable")
        assert (await call("rules_list"))["data"]["count"] == 0
        env = await call("rules_test", expr=expr)
        assert env["ok"] is False and env["data"] == {"expr": expr, "parsed": False} and _missing(env, "expr", "data_unavailable")


async def test_alerts_job_skips_oversized_sequence_rules_and_bad_messages():
    s = Store(":memory:")
    await s.init()
    try:
        rules = [
            ("rep", '"a" * 10 ** 9', ""),
            ("fmt", '"%999999999d" % 1', ""),
            ("subscript", "2 > 1", "{value[0]}"),  # TypeError out of the whole job before item 299
            ("attr", "2 > 1", "{value.x}"),  # AttributeError
            ("width", "2 > 1", "{value:999999999}"),  # a gigabyte string
            ("nested", "2 > 1", "{value:{value_pct}}"),
            ("ok", "3", "v={value:.1f} pct={value_pct:.0f}"),
        ]
        for name, expr, msg in rules:
            await s.rule_insert(name=name, expr=expr, message=msg, cooldown_seconds=60, enabled=True, created_ts=NOW_TS)
        t0 = time.monotonic()
        fired = await evaluate_dsl_rules(s, now_ts=NOW_TS)
        assert time.monotonic() - t0 < 5
        got = {f.rule_id: f.message for f in fired}
        assert got == {
            "db:subscript": "{value[0]}",
            "db:attr": "{value.x}",
            "db:width": "{value:999999999}",
            "db:nested": "{value:{value_pct}}",
            "db:ok": "v=3.0 pct=300",
        }
    finally:
        await s.aclose()


def test_threshold_rule_message_bounded():
    assert _format_message("{value:.1f} > {threshold}", 1.0, 2.0) == "1.0 > 2.0"
    assert _format_message("{value:>12.3f}", 1.0, 2.0) == "       1.000"
    for bad in ("{value:999999999}", "{value:.99999f}", "{value[0]}", "{value.x}", "{0}", "{", "{value:{threshold}}"):
        assert _format_message(bad, 1.0, 2.0) == bad
