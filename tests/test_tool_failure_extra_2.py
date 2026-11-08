"""Invariant 1 / 5: store failure or corrupt stored rows -> envelope, never an exception (item 279, BACKLOG 6.53).

Five store tools that only the generic sweeps covered (test_tools_no_store / test_tools_corrupt_store: no store, a
damaged file, a write lock): funding_zscore, skew_percentile, liquidations_summary, event_study, ohlcv_history. Here each
gets a failing read query (the file is fine at init, the SELECT raises) and, where the tool computes on stored values,
rows that another writer left with a string, a blob or inf in a numeric column (SQLite keeps any type in a REAL or
INTEGER column). Found on the way: funding_zscore, skew_percentile and liquidations_summary raised TypeError in their
sums on such a row, event_study raised ValueError / OverflowError on an event timestamp and TypeError on a bar; such
rows are now skipped with a warning.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

EXPIRY = "2026-11-27T08:00:00+00:00"
NOW_TS = int(NOW.timestamp())
CORRUPT = ("abc", b"\x00\x01", float("inf"))


async def _call(srv, name: str, **args) -> dict:
    async with Client(srv) as c:
        res = await c.call_tool(name, args)
    assert not res.is_error, res.content[0].text[:300] if res.content else ""
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


def _read_fails(rt, monkeypatch, method: str) -> None:
    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.store, method, boom)


def _store_failed(env: dict) -> None:
    assert env["ok"] is False and env["data"] is None
    assert any(m["field"] == "store" and m["reason"] == "backend_error" and "disk I/O error" in m["detail"] for m in env["missing"])


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


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


async def _liqs(rt, notionals: list) -> None:
    await _rows(
        rt,
        "INSERT INTO liquidations (ts, venue, symbol, side, qty, price, notional_usd) VALUES (?,?,?,?,?,?,?)",
        [(NOW_TS - 60 + i, "bybit", "BTCUSDT", "sell", 0.1, 80000.0, v) for i, v in enumerate(notionals)],
    )


# ---------- funding_zscore ----------


async def test_funding_zscore_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "funding_series")
    _store_failed(await _call(build_server(rt)[0], "funding_zscore", venue="binanceusdm", base="BTC"))


@pytest.mark.parametrize("bad", CORRUPT)
async def test_funding_zscore_skips_a_corrupt_row_instead_of_raising(rt, bad):
    # Before item 279 sum() met the string / blob and the TypeError escaped; inf made mean inf and stdev NaN.
    await _funding(rt, [0.10 + 0.01 * i for i in range(11)] + [bad])
    env = await _call(build_server(rt)[0], "funding_zscore", venue="binanceusdm", base="BTC", lookback_days=1)
    assert env["ok"] is True
    stats = env["data"]["annualized"]
    assert stats["count"] == 11 and stats["latest"] == pytest.approx(0.20) and stats["max"] == pytest.approx(0.20)
    assert env["data"]["latest_ts"] == NOW_TS - 3600  # the ts of the latest valid row, not of the skipped one
    assert any("1 stored rows with a non-numeric or non-finite annualized skipped" in w for w in env["warnings"])


async def test_funding_zscore_only_corrupt_rows_is_not_ok(rt):
    await _funding(rt, ["abc"] * 12)
    env = await _call(build_server(rt)[0], "funding_zscore", venue="binanceusdm", base="BTC", lookback_days=1)
    assert env["ok"] is False and any(m["field"] == "funding_hist" and m["reason"] == "data_unavailable" for m in env["missing"])


# ---------- skew_percentile ----------


async def test_skew_percentile_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "atm_term_series")
    _store_failed(await _call(build_server(rt)[0], "skew_percentile", currency="BTC", expiry=EXPIRY))


@pytest.mark.parametrize("bad", CORRUPT)
async def test_skew_percentile_skips_a_corrupt_row_instead_of_raising(rt, bad):
    await _atm(rt, [-0.05 + 0.005 * i for i in range(11)] + [bad])
    env = await _call(build_server(rt)[0], "skew_percentile", currency="BTC", expiry=EXPIRY, lookback_days=1)
    assert env["ok"] is True
    assert env["data"]["rr"]["count"] == 11 and env["data"]["rr"]["latest"] == pytest.approx(0.0, abs=1e-12)
    assert env["data"]["latest_ts"] == NOW_TS - 3600
    assert any("1 stored rows with a non-numeric or non-finite rr25 skipped" in w for w in env["warnings"])


async def test_skew_percentile_null_wing_is_skipped_without_a_warning(rt):
    # NULL is what the collector writes when it cannot fit a wing: skipped as before, but latest_ts now follows the value.
    await _atm(rt, [-0.05 + 0.005 * i for i in range(11)] + [None])
    env = await _call(build_server(rt)[0], "skew_percentile", currency="BTC", expiry=EXPIRY, lookback_days=1)
    assert env["ok"] is True and env["data"]["rr"]["count"] == 11 and env["data"]["latest_ts"] == NOW_TS - 3600
    assert not any("skipped" in w for w in env["warnings"])


# ---------- liquidations_summary ----------


async def test_liquidations_summary_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "liquidations_in_window")
    _store_failed(await _call(build_server(rt)[0], "liquidations_summary", window_minutes=10))


@pytest.mark.parametrize("bad", CORRUPT)
async def test_liquidations_summary_skips_a_corrupt_row_instead_of_raising(rt, bad):
    await _liqs(rt, [8000.0, bad, 4000.0])
    env = await _call(build_server(rt)[0], "liquidations_summary", window_minutes=10)
    assert env["ok"] is True
    assert env["data"]["count"] == 2 and env["data"]["total_notional_usd"] == 12000.0 and env["data"]["sell_notional_usd"] == 12000.0
    assert any("1 stored liquidation rows with a non-numeric or non-finite" in w for w in env["warnings"])


async def test_liquidations_summary_only_corrupt_rows_is_not_ok(rt):
    # An all-corrupt window must not read as "no liquidations" (the empty window stays ok=true, test_liquidations.py).
    await _liqs(rt, ["abc", b"\x00"])
    env = await _call(build_server(rt)[0], "liquidations_summary", window_minutes=10)
    assert env["ok"] is False and env["data"] is None
    assert any(m["field"] == "liquidations" and m["reason"] == "data_unavailable" for m in env["missing"])


# ---------- event_study ----------


async def _event_study_data(rt, *, bad_event=None, bad_close=None) -> None:
    # Hourly bars rising by 1 per hour over two days; two fomc events 24 h and 12 h before NOW.
    bars = []
    for i in range(48):
        ts_ms = (NOW_TS - (47 - i) * 3600) * 1000
        c = bad_close if (bad_close is not None and i == 36) else 80000.0 + i
        bars.append(("binanceusdm", "BTC/USDT:USDT", "1h", ts_ms, c, c, c, c, 1.0))
    await _rows(rt, "INSERT INTO ohlcv_hist VALUES (?,?,?,?,?,?,?,?,?)", bars)
    events = [(NOW_TS - 24 * 3600, "fomc", "a"), (NOW_TS - 12 * 3600, "fomc", "b")]
    if bad_event is not None:
        events.append((bad_event, "fomc", "corrupt"))
    await _rows(rt, "INSERT INTO events (ts, kind, label) VALUES (?,?,?)", events)


ES_ARGS = {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT", "measure": "direction"}


async def test_event_study_read_failure_goes_to_missing(rt, monkeypatch):
    await _event_study_data(rt)
    _read_fails(rt, monkeypatch, "ohlcv_range")
    _store_failed(await _call(build_server(rt)[0], "event_study", **ES_ARGS))


@pytest.mark.parametrize("bad", CORRUPT)
async def test_event_study_skips_a_corrupt_event_timestamp_instead_of_raising(rt, bad):
    # Before item 279 int(e["ts"]) raised ValueError on the string / blob and OverflowError on inf.
    await _event_study_data(rt, bad_event=bad)
    env = await _call(build_server(rt)[0], "event_study", **ES_ARGS)
    assert env["ok"] is True and env["data"]["n_events_requested"] == 2
    assert env["data"]["stats"]["n_sample"] == 2 and env["data"]["stats"]["hit_rate"] == 1.0
    assert any("skipped 1 stored event(s) with a non-integer timestamp" in w for w in env["warnings"])


@pytest.mark.parametrize("bad", CORRUPT)
async def test_event_study_skips_a_corrupt_bar_instead_of_raising(rt, bad):
    # Bar 36 (NOW - 11 h) sits inside the second event's window; before item 279 the comparison with the string raised.
    await _event_study_data(rt, bad_close=bad)
    env = await _call(build_server(rt)[0], "event_study", **ES_ARGS)
    assert env["ok"] is True and env["data"]["stats"]["n_sample"] == 2 and env["data"]["stats"]["hit_rate"] == 1.0
    assert any("1 stored bars with a non-integer timestamp or a non-numeric / non-finite price skipped" in w for w in env["warnings"])


# ---------- ohlcv_history ----------


async def test_ohlcv_history_read_failure_goes_to_missing(rt, monkeypatch):
    _read_fails(rt, monkeypatch, "ohlcv_range")
    _store_failed(await _call(build_server(rt)[0], "ohlcv_history", venue="binanceusdm", symbol="BTC/USDT:USDT"))


async def test_ohlcv_history_window_outside_the_stored_bars_is_not_ok(rt):
    await _event_study_data(rt)
    env = await _call(
        build_server(rt)[0], "ohlcv_history", venue="binanceusdm", symbol="BTC/USDT:USDT", until_ts_ms=(NOW_TS - 30 * 86400) * 1000
    )
    assert env["ok"] is False and env["data"]["count"] == 0 and env["data"]["bars"] == []
    assert any(m["field"] == "ohlcv_hist" and m["reason"] == "data_unavailable" for m in env["missing"])
