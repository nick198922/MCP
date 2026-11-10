"""Invariants 1 and 3: the two open findings of BACKLOG 6.53 left after item 299 (item 300).

(a) `analytics.pm_threshold.history_alignment` parsed trades and `/prices-history` points without a guard: text, None,
inf, a 300+-digit int or a missing key raised out of it (and out of scripts/verify_pm_threshold.py), and a 1e308 price
overflowed the mean distances. Now such a trade / point is skipped and counted in `skipped_trades` / `skipped_points`;
the rest is scored as before.

(b) A text / blob in the latest stored funding row read as "no value" (value null, ok=true) in rules_test with no
warning, and a window metric (funding_mean / funding_zscore) dropped such rows silently (funding_zscore then took an
older row as "latest"). Now rules_test names the metric and the column in `warnings`, and the alerts job logs it once per
rule. The JSON-rule path of the alerts job (`evaluate_rules`) compared the stored string with the threshold and raised
TypeError out of the job; it now reads it as no value with a logged warning.

Each MCP test makes its calls and store checks in one session (the in-memory store is reopened empty on every connection).
"""

from __future__ import annotations

import logging
import math

import pytest

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_7 import FUNDING_SQL, _funding
from test_tool_failure_extra_10 import _rows, _session
from tradedesk.alerts.engine import Rule, evaluate_dsl_rules, evaluate_rules
from tradedesk.analytics.pm_threshold import history_alignment
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.store import Store

NOW_TS = int(NOW.timestamp())
HUGE = 1e308
INF = float("inf")
NAN = float("nan")
BLOB = b"\xff\xfe"


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


# ---------- (a) history_alignment on garbage trades / points ----------

TRADES = [{"timestamp": 100 * i, "price": 0.1 + 0.01 * i, "asset": "Y"} for i in range(1, 20)]
POINTS = [{"t": 100 * i + 5, "p": 0.1 + 0.01 * i} for i in range(1, 20)]

BAD_VALUES = {"text": "abc", "none": None, "inf": INF, "neg_inf": -INF, "nan": NAN, "1e308": HUGE, "10e400": 10**400, "blob": BLOB}
BAD_PRICES = {**BAD_VALUES, "negative": -0.5, "above_one": 1.5}


def _bad_rows(time_key: str, price_key: str, good: dict) -> dict[str, object]:
    # A 1e308 / 10**400 time is a valid (far-future) int: see test_history_alignment_huge_time_is_a_valid_row.
    rows: dict[str, object] = {f"time_{k}": {**good, time_key: v} for k, v in BAD_VALUES.items() if k not in ("1e308", "10e400")}
    rows |= {f"price_{k}": {**good, price_key: v} for k, v in BAD_PRICES.items()}
    rows |= {"no_time": {k: v for k, v in good.items() if k != time_key}, "no_price": {k: v for k, v in good.items() if k != price_key}}
    rows |= {"row_none": None, "row_text": "abc", "row_list": [1, 2]}
    return rows


BAD_TRADES = _bad_rows("timestamp", "price", {"timestamp": 250, "price": 0.5, "asset": "Y"})
BAD_POINTS = _bad_rows("t", "p", {"t": 250, "p": 0.5})


def _finite(al: dict) -> bool:
    return all(v is None or math.isfinite(v) for v in al.values())


def test_history_alignment_clean_input_reports_no_skips():
    al = history_alignment(POINTS, TRADES, "Y")
    assert al["match_prev_trade"] == 1.0 and al["n"] == 19
    assert al["skipped_trades"] == 0 and al["skipped_points"] == 0


@pytest.mark.parametrize("bad", BAD_TRADES.values(), ids=BAD_TRADES.keys())
def test_history_alignment_skips_a_garbage_trade(bad):
    # int("abc") / float(None) / int(inf) / float(10**400) raised out of history_alignment before item 300.
    clean = history_alignment(POINTS, TRADES, "Y")
    al = history_alignment(POINTS, [*TRADES[:5], bad, *TRADES[5:]], "Y")
    assert al["skipped_trades"] == 1 and al["skipped_points"] == 0
    assert {k: v for k, v in al.items() if k != "skipped_trades"} == {k: v for k, v in clean.items() if k != "skipped_trades"}


@pytest.mark.parametrize("bad", BAD_POINTS.values(), ids=BAD_POINTS.keys())
def test_history_alignment_skips_a_garbage_point(bad):
    clean = history_alignment(POINTS, TRADES, "Y")
    al = history_alignment([*POINTS[:5], bad, *POINTS[5:]], TRADES, "Y")
    assert al["skipped_points"] == 1 and al["skipped_trades"] == 0
    assert {k: v for k, v in al.items() if k != "skipped_points"} == {k: v for k, v in clean.items() if k != "skipped_points"}


def test_history_alignment_huge_time_is_a_valid_row():
    # A 1e308 / 10**400 time is an int, not garbage: the point is simply after every trade (n grows by one, finite output).
    al = history_alignment([*POINTS, {"t": HUGE, "p": 0.3}, {"t": 10**400, "p": 0.3}], TRADES, "Y")
    assert al["n"] == 21 and al["skipped_points"] == 0 and _finite(al)


def test_history_alignment_all_garbage_is_empty_not_an_exception():
    al = history_alignment(list(BAD_POINTS.values()), list(BAD_TRADES.values()), "Y")
    assert al["n"] == 0 and al["match_prev_trade"] is None and al["mean_abs_to_prev"] is None
    assert al["skipped_points"] == len(BAD_POINTS) and al["skipped_trades"] == len(BAD_TRADES)


def test_history_alignment_no_side_price_is_mirrored_and_string_numbers_parse():
    # The NO-token mirror and numeric strings (as the API sends them) are unchanged by the guard.
    trades = [{"timestamp": "100", "price": "0.93", "asset": "N"}]
    al = history_alignment([{"t": "105", "p": "0.07"}], trades, "Y")
    assert al["n"] == 1 and al["match_prev_trade"] == 1.0 and al["skipped_trades"] == 0 and al["skipped_points"] == 0


# ---------- (b) text / blob in a stored funding row ----------

FUNDING = 'funding("binanceusdm","BTC")'


@pytest.mark.parametrize("bad", ["abc", BLOB], ids=["text", "blob"])
async def test_rules_test_unreadable_latest_funding_is_warned(rt, bad):
    # Before item 300: value null, ok=true and no warning.
    await _rows(rt, FUNDING_SQL, _funding(0.1, bad))
    async with _session(rt) as call:
        env = await call("rules_test", expr=FUNDING)
        assert env["ok"] is True and env["data"]["value"] is None and env["data"]["truthy"] is False
        w = [x for x in env["warnings"] if x.startswith("funding('binanceusdm', 'BTC'): the latest stored `annualized`")]
        assert len(w) == 1 and "not a number" in w[0] and "read as no value" in w[0], env["warnings"]
        assert "abc" not in w[0] and "\\xff" not in w[0]  # the stored text is not echoed


async def test_rules_test_same_metric_twice_warns_once(rt):
    await _rows(rt, FUNDING_SQL, _funding(0.1, "abc"))
    async with _session(rt) as call:
        env = await call("rules_test", expr=f"{FUNDING} == {FUNDING}")
        assert env["ok"] is True
        assert sum("latest stored `annualized`" in w for w in env["warnings"]) == 1, env["warnings"]


@pytest.mark.parametrize(
    ("expr", "values", "expected", "latest_bad"),
    [
        ('funding_mean("binanceusdm","BTC", 1)', (0.1, "abc", 0.3), 0.2, False),
        ('funding_mean("binanceusdm","BTC", 1)', (0.1, 0.3, BLOB), 0.2, True),
        ('funding_zscore("binanceusdm","BTC", 1)', (0.1, 0.2, 0.3, "abc"), 1.0, True),
    ],
    ids=["mean_middle", "mean_latest_blob", "zscore_latest_text"],
)
async def test_rules_test_window_metric_warns_on_skipped_rows(rt, expr, values, expected, latest_bad):
    # The rest is still used; before item 300 the dropped rows (and zscore's older "latest") were silent.
    await _rows(rt, FUNDING_SQL, _funding(*values))
    async with _session(rt) as call:
        env = await call("rules_test", expr=expr)
        assert env["ok"] is True and env["data"]["value"] == pytest.approx(expected)
        w = [x for x in env["warnings"] if f"1 of {len(values)} stored `annualized` values in the window are not numbers" in x]
        assert len(w) == 1, env["warnings"]
        assert ("(the latest one among them)" in w[0]) is latest_bad


async def test_rules_test_clean_funding_has_no_warning(rt):
    await _rows(rt, FUNDING_SQL, _funding(0.1, 0.2))
    async with _session(rt) as call:
        env = await call("rules_test", expr=FUNDING)
        assert env["ok"] is True and env["data"]["value"] == pytest.approx(0.2) and env["warnings"] == []
        env = await call("rules_test", expr='funding_mean("binanceusdm","BTC", 1)')
        assert env["ok"] is True and env["warnings"] == []


async def test_rules_test_eval_failure_still_carries_the_note(rt):
    # The note is kept when the expression then fails (None + 1 raises TypeError -> missing: eval).
    await _rows(rt, FUNDING_SQL, _funding(0.1, "abc"))
    async with _session(rt) as call:
        env = await call("rules_test", expr=f"{FUNDING} + 1")
        assert env["ok"] is False and env["data"]["evaluated"] is False
        assert any("latest stored `annualized`" in w for w in env["warnings"]), env["warnings"]


async def _store_with(values) -> Store:
    s = Store(":memory:")
    await s.init()
    await s.conn.executemany(FUNDING_SQL, _funding(*values))
    await s.conn.commit()
    return s


@pytest.mark.parametrize("bad", ["abc", BLOB], ids=["text", "blob"])
async def test_alerts_job_dsl_rule_logs_unreadable_funding_once(caplog, bad):
    s = await _store_with((0.5, bad))
    name = f"hot_{type(bad).__name__}"  # a distinct rule id per case: the log is once per process
    try:
        await s.rule_insert(name=name, expr=f"{FUNDING} > 0.3", message="", cooldown_seconds=60, enabled=True, created_ts=NOW_TS)
        caplog.set_level(logging.WARNING, logger="tradedesk.alerts.engine")
        assert await evaluate_dsl_rules(s, now_ts=NOW_TS) == []  # no value -> does not fire on the older 0.5
        hits = [r.getMessage() for r in caplog.records if f"db:{name}" in r.getMessage()]
        assert len(hits) == 1 and "latest stored `annualized` is not a number" in hits[0], hits
        caplog.clear()
        assert await evaluate_dsl_rules(s, now_ts=NOW_TS) == []
        assert not [r for r in caplog.records if f"db:{name}" in r.getMessage()]
    finally:
        await s.aclose()


@pytest.mark.parametrize("bad", ["abc", BLOB], ids=["text", "blob"])
async def test_alerts_job_json_rule_reads_unreadable_funding_as_no_value(caplog, bad):
    # `"abc" > 0.3` raised TypeError out of evaluate_rules (the whole alerts job) before item 300.
    s = await _store_with((0.5, bad))
    rid = f"json_hot_{type(bad).__name__}"
    rules = [
        Rule(
            id=rid,
            enabled=True,
            metric={"kind": "funding", "venue": "binanceusdm", "base": "BTC"},
            when={"op": ">", "value": 0.3},
            cooldown_seconds=60,
        ),
        Rule(
            id=f"{rid}_ok",
            enabled=True,
            metric={"kind": "funding", "venue": "binanceusdm", "base": "ETH"},
            when={"op": ">", "value": 0.3},
            cooldown_seconds=60,
        ),
    ]
    await s.conn.execute(FUNDING_SQL, (NOW_TS - 30, "binanceusdm", "ETH", 0.0001, 8.0, 0.4))
    await s.conn.commit()
    try:
        caplog.set_level(logging.WARNING, logger="tradedesk.alerts.engine")
        fired = await evaluate_rules(rules, s, now_ts=NOW_TS)
        assert [f.rule_id for f in fired] == [f"{rid}_ok"] and fired[0].value == pytest.approx(0.4)
        assert any(rid in r.getMessage() and "funding annualized is not a number" in r.getMessage() for r in caplog.records)
    finally:
        await s.aclose()
