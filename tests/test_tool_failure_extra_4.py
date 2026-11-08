"""Invariant 1 / 5: failures and corrupt stored values -> envelope, never an exception (item 285, BACKLOG 6.53).

Item 285 names ohlcv_history, liquidations_summary, alerts_recent, skew_percentile, funding_zscore and touch_probability.
The first five already have a failing-SELECT test and string / blob / inf rows (test_tool_failure_extra_2.py / _3.py), so
this file covers what those left out: finite stored values near the float limit (1e308, another writer), an invalid UTF-8
blob in a text column, an all-malformed alert history and a time bound beyond SQLite INTEGER. Found on the way:
skew_percentile and funding_zscore raised OverflowError in the variance on +-1e308 (and answered Infinity / NaN on a
constant 1e308), liquidations_summary answered Infinity / NaN on such notionals and failed its reply on a blob in
symbol / side, ohlcv_history raised OverflowError on until_ts_ms = 10**20, alerts_recent read an all-malformed history as
"no alerts" (ok=true). touch_probability has neither a provider nor a store: its dependency is the analytics, so the
tests make that raise or return NaN, and check it with no store and every venue failing.
"""

from __future__ import annotations

import json
import math

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from test_tools_provider_outage import failing_runtime
from tradedesk.analytics import probability
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools._util import first_nonfinite

EXPIRY = "2026-11-27T08:00:00+00:00"
NOW_TS = int(NOW.timestamp())
HUGE = 1e308  # finite, so store_num keeps it; its square or a sum of two overflows


async def _call(srv, name: str, *, _finite: bool = True, **args) -> dict:
    async with Client(srv) as c:
        res = await c.call_tool(name, args)
    assert not res.is_error, res.content[0].text[:300] if res.content else ""
    text = json.loads(res.content[0].text)  # the text reply keeps Infinity / NaN, the structured one shows them as null
    # first_nonfinite also reads a string such as "INF" as a number: `_finite=False` for a reply that holds one on purpose.
    assert not _finite or first_nonfinite(text.get("data")) is None, str(text)[:300]
    data = res.structured_content if res.structured_content is not None else text
    return data["result"] if set(data) == {"result"} else data


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


def _missing(env: dict, field: str, reason: str) -> bool:
    return any(m["field"] == field and m["reason"] == reason for m in env["missing"])


# ---------- funding_zscore / skew_percentile ----------


async def _funding(rt, values: list) -> None:
    n = len(values)
    await _rows(
        rt,
        "INSERT INTO funding_hist VALUES (?,?,?,?,?,?)",
        [(NOW_TS - (n - 1 - i) * 3600, "binanceusdm", "BTC", 0.0001, 8.0, v) for i, v in enumerate(values)],
    )


async def _atm(rt, rr25: list) -> None:
    n = len(rr25)
    await _rows(
        rt,
        "INSERT INTO atm_term VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(NOW_TS - (n - 1 - i) * 3600, "BTC", EXPIRY, 48.0, 83000.0, 0.42, v, 0.01, None, None) for i, v in enumerate(rr25)],
    )


ALTERNATING = [(-1) ** i * HUGE for i in range(12)]  # mean ~0, (v - mean) ** 2 raised OverflowError before item 285
CONSTANT = [HUGE] * 12  # the sum is inf: mean / stdev Infinity, z_score NaN before item 285


@pytest.mark.parametrize("values", [ALTERNATING, CONSTANT], ids=["alternating", "constant"])
async def test_funding_zscore_overflowing_values_are_not_ok(rt, values):
    await _funding(rt, values)
    env = await _call(build_server(rt)[0], "funding_zscore", venue="binanceusdm", base="BTC", lookback_days=1)
    assert env["ok"] is False and env["data"] is None and _missing(env, "funding_hist", "data_unavailable")


@pytest.mark.parametrize("values", [ALTERNATING, CONSTANT], ids=["alternating", "constant"])
async def test_skew_percentile_overflowing_values_are_not_ok(rt, values):
    await _atm(rt, values)
    env = await _call(build_server(rt)[0], "skew_percentile", currency="BTC", expiry=EXPIRY, lookback_days=1)
    assert env["ok"] is False and env["data"] is None and _missing(env, "atm_term", "data_unavailable")


async def test_funding_zscore_large_but_safe_values_unchanged(rt):
    # 1e150 squared is still finite: the stats are computed as before, without a warning.
    await _funding(rt, [1e150 * (1 + 0.01 * i) for i in range(12)])
    env = await _call(build_server(rt)[0], "funding_zscore", venue="binanceusdm", base="BTC", lookback_days=1)
    assert env["ok"] is True and env["data"]["annualized"]["count"] == 12 and env["warnings"] == []
    assert math.isclose(env["data"]["annualized"]["latest"], 1.11e150, rel_tol=1e-9)


# ---------- liquidations_summary ----------

LIQ_SQL = "INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd) VALUES (?,?,?,?,?,?,?)"


async def test_liquidations_summary_overflowing_notionals_are_not_ok(rt):
    await _rows(rt, LIQ_SQL, [(NOW_TS - 60 + i, "bybit", "BTCUSDT", "sell", 0.1, 80000.0, HUGE) for i in range(3)])
    env = await _call(build_server(rt)[0], "liquidations_summary", window_minutes=10)
    assert env["ok"] is False and env["data"] is None and _missing(env, "liquidations", "data_unavailable")


@pytest.mark.parametrize("col", [2, 3, 1])  # symbol, side, venue
async def test_liquidations_summary_skips_a_blob_text_cell(rt, col):
    # An invalid UTF-8 blob in symbol / side failed the whole reply before item 285.
    bad = ["", "bybit", "ETHUSDT", "sell", 0.1, 3000.0, 300.0]
    bad[0], bad[col] = NOW_TS - 50, b"\xff\xfe"
    await _rows(rt, LIQ_SQL, [(NOW_TS - 60, "bybit", "BTCUSDT", "sell", 0.1, 80000.0, 8000.0), tuple(bad)])
    env = await _call(build_server(rt)[0], "liquidations_summary", window_minutes=10)
    assert env["ok"] is True and env["data"]["count"] == 1 and env["data"]["total_notional_usd"] == 8000.0
    assert [s["symbol"] for s in env["data"]["per_symbol"]] == ["BTCUSDT"]
    assert any(w.startswith("1 stored liquidation rows with a non-numeric") and "blob" in w for w in env["warnings"])


async def test_liquidations_summary_symbol_named_inf_is_not_an_overflow(rt):
    # The overflow check looks at the sums only: a text cell that float() reads as inf is still a valid symbol.
    await _rows(rt, LIQ_SQL, [(NOW_TS - 60, "bybit", "INF", "buy", 1.0, 2.0, 2.0)])
    env = await _call(build_server(rt)[0], "liquidations_summary", _finite=False, window_minutes=10)
    assert env["ok"] is True and env["data"]["per_symbol"][0]["symbol"] == "INF" and env["data"]["buy_notional_usd"] == 2.0
    assert env["data"]["total_notional_usd"] == 2.0 and env["data"]["imbalance"] == 1.0 and env["missing"] == []


# ---------- alerts_recent ----------

ALERT_SQL = "INSERT INTO alerts (rule_id, ts, payload, channel, delivered, delivery_error) VALUES (?,?,?,?,?,?)"


@pytest.mark.parametrize("bad", [("abc", "{}"), (NOW_TS, b"\xff\xfe")], ids=["string_ts", "blob_payload"])
async def test_alerts_recent_only_corrupt_alerts_is_not_ok(rt, bad):
    # An empty history stays ok=true (test_tool_failure_extra_3.py); an all-malformed one must not read as "no alerts".
    ts, payload = bad
    await _rows(rt, ALERT_SQL, [("r1", ts, payload, "log", 0, None), ("r2", ts, payload, "log", 0, None)])
    env = await _call(build_server(rt)[0], "alerts_recent")
    assert env["ok"] is False and env["data"] == {"count": 0, "alerts": []}
    assert _missing(env, "alerts", "data_unavailable") and "all 2 stored alert rows are malformed" in env["missing"][0]["detail"]


# ---------- ohlcv_history ----------

OH_ARGS = {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"}


@pytest.mark.parametrize("arg", ["since_ts_ms", "until_ts_ms"])
@pytest.mark.parametrize("value", [10**20, -(10**20)])
async def test_ohlcv_history_time_bound_beyond_sqlite_integer_is_invalid(rt, arg, value):
    # Before item 285 the query binding raised OverflowError ("Python int too large to convert to SQLite INTEGER").
    env = await _call(build_server(rt)[0], "ohlcv_history", **OH_ARGS, **{arg: value})
    assert env["ok"] is False and env["data"] is None and _missing(env, "input", "invalid_input")


async def test_ohlcv_history_int64_edge_bound_still_queries(rt):
    await _rows(
        rt,
        "INSERT INTO ohlcv_hist VALUES (?,?,?,?,?,?,?,?,?)",
        [("binanceusdm", "BTC/USDT:USDT", "1h", NOW_TS * 1000, 1.0, 2.0, 0.5, 1.5, 10.0)],
    )
    env = await _call(build_server(rt)[0], "ohlcv_history", **OH_ARGS, until_ts_ms=2**63 - 1)
    assert env["ok"] is True and env["data"]["count"] == 1 and env["warnings"] == []


# ---------- touch_probability ----------

TOUCH = {"spot": 100.0, "barrier": 110.0, "days": 7.0, "iv": 0.5}


@pytest.mark.parametrize("exc", [OverflowError, ValueError, ZeroDivisionError])
@pytest.mark.parametrize("fn", ["prob_touch", "mc_touch"])
async def test_touch_probability_analytics_error_goes_to_missing(rt, monkeypatch, fn, exc):
    def boom(*_a, **_k):
        raise exc("numeric failure")

    monkeypatch.setattr(probability, fn, boom)
    env = await _call(build_server(rt)[0], "touch_probability", **TOUCH)
    assert env["ok"] is False and env["data"] is None and _missing(env, "touch", "invalid_input")
    assert "numeric failure" in env["missing"][0]["detail"]


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
async def test_touch_probability_non_finite_analytics_result_is_not_ok(rt, monkeypatch, value):
    monkeypatch.setattr(probability, "mc_touch", lambda *_a, **_k: value)
    env = await _call(build_server(rt)[0], "touch_probability", **TOUCH)
    assert env["ok"] is False and env["data"] is None and _missing(env, "result", "invalid_input")


async def test_touch_probability_needs_no_store_or_venue():
    # No store (db_path empty), every HTTP request 500 and every ccxt call failing: the calculator still answers.
    calls: list[str] = []
    rt = failing_runtime("down", "", calls)
    try:
        env = await _call(build_server(rt)[0], "touch_probability", **TOUCH)
    finally:
        await rt.aclose()
    assert calls == [] and env["ok"] is True and env["missing"] == [] and env["evidence"] == []
    assert 0.0 < env["data"]["analytic"] < 1.0 and abs(env["data"]["analytic"] - env["data"]["monte_carlo"]) < 0.02
