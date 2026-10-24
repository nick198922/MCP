"""Binance TradFi perps spread: pure spread / round-trip cost (analytics/tradfi_spread.py), the table tradfi_spread and the
collector job `tradfi_spread` (IDEA-20261024-tradfi-perp-offhours-reopen, early cost check)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from conftest import State, make_transports
from tradedesk.analytics.tradfi_spread import (
    ROUND_TRIP_FEE,
    is_tradfi_perp,
    relative_spread,
    round_trip_cost,
    session_costs,
    spread_at,
)
from tradedesk.collector.scheduler import _retention_sweep, build_default_scheduler
from tradedesk.collector.tradfi_spread import TradfiSpreadJob, build_rows, scan_tradfi_spread, tradfi_symbols
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

FIXTURE = Path(__file__).parent / "fixtures" / "tradfi_spread_binance_2026-10-24.json"
SAMPLE = json.loads(FIXTURE.read_text())  # live fapi.binance.com, 2026-10-24 12:33 UTC (a Saturday), trimmed
NOW = datetime(2026, 10, 24, 12, 33, 16, tzinfo=UTC)
KEPT = ["AAPLUSDT", "CLUSDT", "SPYUSDT", "XAUUSDT"]  # US equity/ETF + commodity, USDT-quoted
MIN = 60_000


def _ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


# ----------------------------------------------------------------------------- pure functions


def test_relative_spread_hand_computed():
    assert relative_spread(99.0, 101.0) == pytest.approx(0.02)  # 2 / mid 100
    assert relative_spread(761.88, 761.89) == pytest.approx(0.01 / 761.885)
    assert relative_spread(100.0, 100.0) == 0.0
    for bid, ask in ((None, 1.0), (1.0, None), (0.0, 1.0), (1.0, 0.0), (101.0, 99.0)):  # missing / empty / crossed
        assert relative_spread(bid, ask) is None


def test_spread_at_is_as_of_and_drops_stale_quotes():
    q = [(1_000, 99.0, 101.0), (61_000, 99.5, 100.5), (121_000, 90.0, 110.0)]
    assert spread_at(q, 61_000) == pytest.approx(0.01)  # a quote exactly at t counts
    assert spread_at(q, 120_999) == pytest.approx(0.01)  # never reads the later, wider quote
    assert spread_at(q, 999) is None  # nothing at or before t
    assert spread_at(q, 61_000 + 5 * MIN) == pytest.approx(0.2)  # the 121 000 quote is the latest
    assert spread_at(q, 121_000 + 5 * MIN + 1) is None  # older than 5 min: a gap, not observed
    assert spread_at(q, 121_000 + 10 * MIN, max_stale_ms=10 * MIN) == pytest.approx(0.2)
    assert spread_at([], 1) is None


def test_round_trip_cost_is_fee_plus_two_full_spreads():
    assert ROUND_TRIP_FEE == pytest.approx(0.0009)  # taker 0.045 % x 2
    assert round_trip_cost(0.001, 0.002) == pytest.approx(0.0039)
    assert round_trip_cost(0.001, 0.002, fee=0.0) == pytest.approx(0.003)


def test_session_costs_use_0925_and_1030_new_york_time_across_dst():
    # Fri 2026-10-23 (EDT, UTC-4): 09:25 ET = 13:25 UTC, 10:30 ET = 14:30 UTC.
    # Tue 2026-12-01 (EST, UTC-5): 09:25 ET = 14:25 UTC, 10:30 ET = 15:30 UTC.
    quotes = [
        (_ms(2026, 10, 23, 13, 24), 99.95, 100.05),  # 0.1 %: the entry quote (09:24 ET, 1 min old at 09:25)
        (_ms(2026, 10, 23, 13, 26), 90.0, 110.0),  # after 09:25: must not be read for the entry
        (_ms(2026, 10, 23, 14, 30), 99.9, 100.1),  # 0.2 %: exactly at 10:30 ET
        (_ms(2026, 12, 1, 14, 25), 99.8, 100.2),  # 0.4 %
        (_ms(2026, 12, 1, 15, 29), 99.7, 100.3),  # 0.6 %
    ]
    out = session_costs(quotes, date(2026, 10, 23), date(2026, 12, 1))
    by = {r["day"]: r for r in out}
    assert date(2026, 10, 24) not in by and date(2026, 11, 26) not in by  # weekend, Thanksgiving
    oct23, dec1 = by[date(2026, 10, 23)], by[date(2026, 12, 1)]
    assert (oct23["entry_ms"], oct23["exit_ms"]) == (_ms(2026, 10, 23, 13, 25), _ms(2026, 10, 23, 14, 30))
    assert (dec1["entry_ms"], dec1["exit_ms"]) == (_ms(2026, 12, 1, 14, 25), _ms(2026, 12, 1, 15, 30))
    assert oct23["entry_spread"] == pytest.approx(0.001) and oct23["exit_spread"] == pytest.approx(0.002)
    assert oct23["cost"] == pytest.approx(0.0009 + 0.001 + 0.002)
    assert dec1["cost"] == pytest.approx(0.0009 + 0.004 + 0.006)
    gap = by[date(2026, 10, 26)]  # no quote within 5 min: not observed
    assert gap["entry_spread"] is None and gap["cost"] is None


def test_universe_filter_on_the_live_sample():
    kept = sorted(tradfi_symbols(SAMPLE["exchange_info"]))
    assert kept == KEPT  # crypto, HK/KR equity, pre-IPO, FX and the USD1-quoted equity are out
    assert is_tradfi_perp({"symbol": "QQQUSDT", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY"})
    assert not is_tradfi_perp({"symbol": "QQQUSDT", "contractType": "PERPETUAL", "underlyingType": "EQUITY"})
    assert tradfi_symbols(None) == frozenset() and tradfi_symbols({"symbols": "x"}) == frozenset()


def test_build_rows_one_side_missing_and_empty_book():
    book = [{"symbol": "SPYUSDT", "bidPrice": "0", "askPrice": "761.89", "time": 5}, {"symbol": "BTCUSDT", "bidPrice": "1"}]
    rows = build_rows({"SPYUSDT", "AAPLUSDT"}, book, None)
    assert rows == [
        {"symbol": "SPYUSDT", "bid": None, "ask": 761.89, "mark_price": None, "index_price": None, "book_ms": 5, "premium_ms": None}
    ]


# ----------------------------------------------------------------------------- collector


def _rt(tmp_path, *, opts: dict | None = None, **kw) -> tuple[Runtime, list]:
    """Fake Binance USD-M serving the live sample. ``opts`` (read on every request): set "down" to a set of endpoint
    names ("bookTicker", "premiumIndex", "exchangeInfo") answering 500."""
    calls: list = []
    opts = opts if opts is not None else {}
    opts.setdefault("down", set())

    def binance(req: httpx.Request) -> httpx.Response:
        name = req.url.path.rsplit("/", 1)[-1]
        calls.append((name, dict(req.url.params)))
        if name in opts["down"]:
            return httpx.Response(500, text="down")
        body = {"bookTicker": SAMPLE["book_ticker"], "premiumIndex": SAMPLE["premium_index"], "exchangeInfo": SAMPLE["exchange_info"]}
        return httpx.Response(200, json=body[name]) if name in body else httpx.Response(404, json={"code": -1})

    tr = make_transports(State())
    tr["binance_futures"] = httpx.MockTransport(binance)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite"), **kw), transports=tr)
    rt.now = lambda: NOW
    rt.binance_futures.http._backoff = 0.0
    return rt, calls


async def test_job_stores_best_bid_ask_mark_index_of_tradfi_perps(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        job = TradfiSpreadJob()
        assert await job(rt) == 4
        rows = await rt.store.tradfi_spread_rows()
        assert [r["symbol"] for r in rows] == KEPT and {r["ts"] for r in rows} == {int(NOW.timestamp())}
        spy = next(r for r in rows if r["symbol"] == "SPYUSDT")
        assert (spy["bid"], spy["ask"], spy["mark_price"]) == (761.88, 761.89, 761.89)
        assert spy["index_price"] == pytest.approx(761.49067872)
        assert (spy["book_ms"], spy["premium_ms"]) == (1792845196075, 1792845194844)
        assert relative_spread(spy["bid"], spy["ask"]) == pytest.approx(1.31e-5, abs=1e-7)  # Saturday, ~0.0013 %
        # all-symbol requests: no `symbol` parameter; exchangeInfo once
        assert [c[0] for c in calls] == ["exchangeInfo", "bookTicker", "premiumIndex"] and all(c[1] == {} for c in calls)
        rt.now = lambda: NOW + timedelta(minutes=1)
        assert await job(rt) == 4
        assert [c[0] for c in calls].count("exchangeInfo") == 1  # the list is cached for a day
        rt.now = lambda: NOW + timedelta(days=1, minutes=1)
        assert await job(rt) == 4
        assert [c[0] for c in calls].count("exchangeInfo") == 2  # daily refresh
        assert len(await rt.store.tradfi_spread_rows(symbol="XAUUSDT")) == 3
    finally:
        await rt.aclose()


async def test_one_request_down_keeps_the_other_and_never_raises(tmp_path):
    rt, _ = _rt(tmp_path, opts={"down": {"bookTicker"}})
    try:
        b = Builder()
        rows = await scan_tradfi_spread(rt, b, KEPT)
        assert len(rows) == 4 and all(r["bid"] is None and r["mark_price"] for r in rows)
        assert [(m.field, m.provider) for m in b.missing] == [("binance_book_ticker", "binance_futures")]
        assert await TradfiSpreadJob()(rt) == 4  # partial snapshot stored, the run succeeds
    finally:
        await rt.aclose()


async def test_full_outage_scan_never_raises_job_reports(tmp_path):
    opts = {"down": {"bookTicker", "premiumIndex"}}
    rt, _ = _rt(tmp_path, opts=opts)
    try:
        b = Builder()
        assert await scan_tradfi_spread(rt, b, KEPT) == []
        assert {m.field for m in b.missing} == {"binance_book_ticker", "binance_premium_index"}
        job = TradfiSpreadJob()
        with pytest.raises(RuntimeError, match="tradfi_spread: no rows; ") as e:  # a failed run for the scheduler
            await job(rt)
        assert "binance_book_ticker: backend_error" in str(e.value) and "binance_premium_index: backend_error" in str(e.value)
        assert await rt.store.tradfi_spread_rows() == []
        opts["down"] = set()
        assert await job(rt) == 4  # recovers on the next tick
    finally:
        await rt.aclose()


async def test_exchange_info_down_first_tick_fails_then_old_list_is_kept(tmp_path):
    opts = {"down": {"exchangeInfo"}}
    rt, calls = _rt(tmp_path, opts=opts)
    try:
        job = TradfiSpreadJob()
        with pytest.raises(RuntimeError, match="binance_exchange_info"):
            await job(rt)
        assert not [c for c in calls if c[0] != "exchangeInfo"]  # no list: quotes not requested
        opts["down"] = set()
        assert await job(rt) == 4
        opts["down"] = {"exchangeInfo"}
        rt.now = lambda: NOW + timedelta(days=2)  # refresh due but failing: the previous list is used
        assert await job(rt) == 4
        assert job.symbols == frozenset(KEPT)
    finally:
        await rt.aclose()


async def test_retention_prunes_old_rows(tmp_path):
    rt, _ = _rt(tmp_path, retention_tradfi_spread_days=30)
    try:
        await rt.store.init()
        old = int(NOW.timestamp()) - 31 * 86_400
        assert await rt.store.insert_tradfi_spread(old, [{"symbol": "SPYUSDT", "bid": 1.0}]) == 1
        assert await rt.store.insert_tradfi_spread(int(NOW.timestamp()), [{"symbol": "SPYUSDT", "bid": 1.0}]) == 1
        await _retention_sweep(rt)
        assert [r["ts"] for r in await rt.store.tradfi_spread_rows()] == [int(NOW.timestamp())]
    finally:
        await rt.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    s = Settings()
    assert s.collect_tradfi_spread_enabled is False and s.collect_tradfi_spread_seconds == 60
    assert s.retention_tradfi_spread_days == 365
    for name in (
        "TRADEDESK_COLLECT_TRADFI_SPREAD_ENABLED",
        "TRADEDESK_COLLECT_TRADFI_SPREAD_SECONDS",
        "TRADEDESK_RETENTION_TRADFI_SPREAD_DAYS",
    ):
        assert allowed(name)
    monkeypatch.setenv("TRADEDESK_COLLECT_TRADFI_SPREAD_ENABLED", "1")
    assert Settings.from_env().collect_tradfi_spread_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_tradfi_spread_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "tradfi_spread" not in names(off) and "tradfi_spread" in names(on)
    job = next(j for j in build_default_scheduler(on).jobs if j.name == "tradfi_spread")
    assert job.period_seconds == 60 and isinstance(job.fn, TradfiSpreadJob)


async def test_migration_29_adds_the_table_to_a_v28_store(tmp_path):
    path = tmp_path / "v28.sqlite"
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= 28:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 28")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 29
        row = {"symbol": "SPYUSDT", "bid": 1.0, "ask": 1.1, "mark_price": 1.05, "index_price": 1.04, "book_ms": 7, "premium_ms": 8}
        assert await s.insert_tradfi_spread(5, [row, {"bid": 2.0}]) == 1  # a row without symbol is dropped
        assert await s.insert_tradfi_spread(5, [dict(row, bid=0.9)]) == 1  # idempotent per (ts, symbol)
        got = await s.tradfi_spread_rows()
        assert len(got) == 1 and got[0] == {"ts": 5, **row, "bid": 0.9}
    finally:
        await s.aclose()
