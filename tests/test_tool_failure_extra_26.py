"""Invariant 1 / 5: store off / unreachable / empty, and every provider down -> envelope, never an exception (item 396,
BACKLOG 6.53).

Item 396 names skew_percentile, funding_zscore, liquidations_summary, event_study, ohlcv_history, positions_risk, rules_test
and touch_probability. Their failing-SELECT, corrupt-row and float-limit cases already have their own tests
(test_tool_failure_extra_2.py / _3.py / _4.py / _7.py); the generic sweeps (test_tools_no_store.py) only check that some
envelope comes back. This file pins, per tool, what that envelope says:
  - store switched off (`db_path=""`): ok=false, `missing: store` (config), no provider call;
  - store that cannot be opened (`Store.init()` raises): ok=false, `missing: store` (backend_error);
  - freshly migrated, empty store: ok=false with `data_unavailable` for the tool's table (liquidations_summary keeps its
    documented ok=true "no liquidations in the window"; rules_test reads a metric without rows as null, falsy);
  - every HTTP request 500 and every ccxt call failing, with valid stored data: the store tools answer ok=true without
    touching a provider (they have none), and touch_probability (no store, no provider) answers as well.
Plus event_study's events query failing (only its bars query had a test), and event_study on events whose bar windows
are all incomplete: before item 396 that answered ok=true with every statistic null and nothing in `missing`.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from mcp import Client

from conftest import NOW
from test_tools_provider_outage import failing_runtime
from tradedesk.server import build_server
from tradedesk.store import Store
from tradedesk.tools._util import first_nonfinite

EXPIRY = "2026-11-27T08:00:00+00:00"
NOW_TS = int(NOW.timestamp())
TOUCH = {"spot": 100.0, "barrier": 110.0, "days": 7.0, "iv": 0.5}
ES_ARGS = {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT", "measure": "direction"}

# Arguments per store tool, and the `missing` field each reports on an empty store.
STORE_TOOLS: dict[str, dict] = {
    "funding_zscore": {"venue": "binanceusdm", "base": "BTC", "lookback_days": 1},
    "skew_percentile": {"currency": "BTC", "expiry": EXPIRY, "lookback_days": 1},
    "liquidations_summary": {"window_minutes": 10},
    "event_study": ES_ARGS,
    "ohlcv_history": {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT"},
    "positions_risk": {},
    "rules_test": {"expr": 'funding("binanceusdm","BTC") > 0.1'},
}
EMPTY_FIELD = {
    "funding_zscore": "funding_hist",
    "skew_percentile": "atm_term",
    "event_study": "events",
    "ohlcv_history": "ohlcv_hist",
    "positions_risk": "positions",
}


@pytest.fixture
def calls() -> list[str]:
    return []


async def _runtime(tmp_path, calls: list[str], *, db: bool = True):
    rt = failing_runtime("down", str(tmp_path / "store.sqlite") if db else "", calls)
    if rt.store is not None:
        await rt.store.init()
    return rt


async def _call(rt, name: str, *, unreachable_dir=None, **args) -> dict:
    async with Client(build_server(rt)[0]) as c:
        if unreachable_dir is not None:
            # As in test_tools_no_store: the server starts on a good store, then the tool sees one that cannot be opened.
            blocker = unreachable_dir / "a_file"
            blocker.write_text("not a directory")
            await rt.store.aclose()
            rt.store = Store(blocker / "store.sqlite")
        res = await c.call_tool(name, args)
    assert not res.is_error, res.content[0].text[:300] if res.content else ""
    text = json.loads(res.content[0].text)  # the text reply keeps Infinity / NaN, the structured one shows them as null
    assert first_nonfinite(text.get("data")) is None, str(text)[:300]
    data = res.structured_content if res.structured_content is not None else text
    return data["result"] if set(data) == {"result"} else data


def _missing(env: dict, field: str, reason: str, detail: str = "") -> bool:
    return any(m["field"] == field and m["reason"] == reason and detail in m["detail"] for m in env["missing"])


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


async def _bars(rt, start_ts: int, n: int) -> None:
    # Hourly bars rising by 1 per hour from start_ts (seconds).
    rows = [("binanceusdm", "BTC/USDT:USDT", "1h", (start_ts + i * 3600) * 1000, *(80000.0 + i,) * 4, 1.0) for i in range(n)]
    await _rows(rt, "INSERT INTO ohlcv_hist VALUES (?,?,?,?,?,?,?,?,?)", rows)


async def _events(rt, *ts: int) -> None:
    await _rows(rt, "INSERT INTO events (ts, kind, label) VALUES (?,?,?)", [(t, "fomc", f"e{i}") for i, t in enumerate(ts)])


async def _seed(rt) -> None:
    """Valid stored data for every store tool above."""
    await _rows(
        rt,
        "INSERT INTO funding_hist VALUES (?,?,?,?,?,?)",
        [(NOW_TS - (11 - i) * 3600, "binanceusdm", "BTC", 0.0001, 8.0, 0.10 + 0.01 * i) for i in range(12)],
    )
    await _rows(
        rt,
        "INSERT INTO atm_term VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(NOW_TS - (11 - i) * 3600, "BTC", EXPIRY, 48.0, 83000.0, 0.42, -0.05 + 0.005 * i, 0.01, None, None) for i in range(12)],
    )
    await _rows(
        rt,
        "INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd) VALUES (?,?,?,?,?,?,?)",
        [(NOW_TS - 60, "bybit", "BTCUSDT", "sell", 0.1, 80000.0, 8000.0), (NOW_TS - 30, "bybit", "BTCUSDT", "buy", 0.05, 80000.0, 4000.0)],
    )
    await _bars(rt, NOW_TS - 47 * 3600, 48)
    await _events(rt, NOW_TS - 24 * 3600, NOW_TS - 12 * 3600)
    await _rows(
        rt,
        "INSERT INTO positions (opened_ts, instrument, kind, side, size, mark, delta, gamma, vega, theta) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(NOW_TS - 10, "BTC-PERP", "perp", "long", 2.0, 100.0, 1.0, 0.0, 0.0, 0.0)],
    )


# ---------- store switched off / cannot be opened ----------


@pytest.mark.parametrize("tool", sorted(STORE_TOOLS))
async def test_store_off_is_missing_config(tmp_path, calls, tool):
    rt = await _runtime(tmp_path, calls, db=False)
    try:
        env = await _call(rt, tool, **STORE_TOOLS[tool])
    finally:
        await rt.aclose()
    assert env["ok"] is False and env["data"] is None and calls == []
    assert _missing(env, "store", "config", "TRADEDESK_DB_PATH")


@pytest.mark.parametrize("tool", sorted(STORE_TOOLS))
async def test_store_unreachable_is_backend_error(tmp_path, calls, tool):
    rt = await _runtime(tmp_path, calls)
    try:
        env = await _call(rt, tool, unreachable_dir=tmp_path, **STORE_TOOLS[tool])
    finally:
        await rt.aclose()
    assert env["ok"] is False and env["data"] is None and calls == []
    assert _missing(env, "store", "backend_error")


# ---------- empty store ----------


@pytest.mark.parametrize("tool", sorted(EMPTY_FIELD))
async def test_empty_store_is_not_ok(tmp_path, calls, tool):
    rt = await _runtime(tmp_path, calls)
    try:
        env = await _call(rt, tool, **STORE_TOOLS[tool])
    finally:
        await rt.aclose()
    assert env["ok"] is False and calls == []
    assert _missing(env, EMPTY_FIELD[tool], "data_unavailable")


async def test_liquidations_summary_empty_store_is_an_empty_window(tmp_path, calls):
    # Documented (test_liquidations.py): a window without liquidations is ok=true with count 0, not a failure.
    rt = await _runtime(tmp_path, calls)
    try:
        env = await _call(rt, "liquidations_summary", window_minutes=10)
    finally:
        await rt.aclose()
    assert env["ok"] is True and env["data"]["count"] == 0 and env["data"]["imbalance"] is None and env["missing"] == []


async def test_rules_test_empty_store(tmp_path, calls):
    # A metric with no stored row is null (falsy), as the alerts job reads it; comparing null with a number is an
    # evaluation failure in `missing`, never an exception.
    rt = await _runtime(tmp_path, calls)
    try:
        bare = await _call(rt, "rules_test", expr='funding("binanceusdm","BTC")')
        compared = await _call(rt, "rules_test", **STORE_TOOLS["rules_test"])
    finally:
        await rt.aclose()
    assert bare["ok"] is True and bare["data"]["value"] is None and bare["data"]["truthy"] is False
    assert compared["ok"] is False and compared["data"]["evaluated"] is False and _missing(compared, "eval", "data_unavailable")
    assert calls == []


# ---------- every provider down ----------


@pytest.mark.parametrize("tool", sorted(STORE_TOOLS))
async def test_store_tools_answer_with_every_provider_down(tmp_path, calls, tool):
    rt = await _runtime(tmp_path, calls)
    await _seed(rt)
    try:
        env = await _call(rt, tool, **STORE_TOOLS[tool])
    finally:
        await rt.aclose()
    assert env["ok"] is True and env["missing"] == [] and calls == [], env
    assert env["data"] is not None


async def test_touch_probability_unreachable_store_and_providers_down(tmp_path, calls):
    rt = await _runtime(tmp_path, calls)
    try:
        env = await _call(rt, "touch_probability", unreachable_dir=tmp_path, **TOUCH)
    finally:
        await rt.aclose()
    assert env["ok"] is True and env["missing"] == [] and calls == [] and 0.0 < env["data"]["analytic"] < 1.0


# ---------- event_study ----------


async def test_event_study_events_query_failure_goes_to_missing(tmp_path, calls, monkeypatch):
    rt = await _runtime(tmp_path, calls)
    await _seed(rt)

    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.store, "events_list", boom)
    try:
        env = await _call(rt, "event_study", **ES_ARGS)
    finally:
        await rt.aclose()
    assert env["ok"] is False and env["data"] is None and _missing(env, "store", "backend_error", "disk I/O error")


async def test_event_study_no_complete_window_is_not_ok(tmp_path, calls):
    # Bars stop two hours before the first event, so no event has a bar after t0; before item 396: ok=true, all stats
    # null, nothing in `missing`.
    rt = await _runtime(tmp_path, calls)
    await _bars(rt, NOW_TS - 30 * 3600, 10)
    await _events(rt, NOW_TS - 18 * 3600, NOW_TS - 17 * 3600)
    try:
        env = await _call(rt, "event_study", **ES_ARGS)
    finally:
        await rt.aclose()
    assert env["ok"] is False and _missing(env, "ohlcv_hist", "data_unavailable", "no event")
    d = env["data"]
    assert d["n_events_requested"] == 2 and d["n_events_evaluated"] == 0 and d["stats"]["n_sample"] == 0
    assert [pe["value"] for pe in d["per_event"]] == [None, None]


async def test_event_study_partly_evaluated_stays_ok(tmp_path, calls):
    # One event with a full window, one past the last bar: ok=true on the evaluated one, as before.
    rt = await _runtime(tmp_path, calls)
    await _bars(rt, NOW_TS - 47 * 3600, 30)
    await _events(rt, NOW_TS - 30 * 3600, NOW_TS - 2 * 3600)
    try:
        env = await _call(rt, "event_study", **ES_ARGS)
    finally:
        await rt.aclose()
    assert env["ok"] is True and env["missing"] == []
    assert env["data"]["n_events_evaluated"] == 1 and env["data"]["stats"]["hit_rate"] == 1.0
