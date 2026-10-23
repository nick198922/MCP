"""collector/coinalyze_history.py: daily Coinalyze history (OI USD, funding, long/short) of Binance BTC/ETH perps.

Samples: DoubleTop's relay of Coinalyze captured 2026-10-18 (tests/fixtures/doubletop/coinalyze__*_2026-10-18.json,
BTCUSDT_PERP.A daily, 4 rows, real Coinalyze data but not a live call with our key). The OI sample is in COINS; the
"USD" OI below is that sample times a fixed price (synthetic, only to exercise the USD path). The ETH series reuse the
BTC sample: the job does not care which numbers come back for which symbol.
"""

import dataclasses
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tradedesk.collector.coinalyze_history import (
    CHUNK_DAYS,
    DAY,
    METRICS,
    SYMBOLS,
    CoinalyzeHistoryJob,
    history_rows,
    oi_not_usd,
    plan_window,
    split_bad_oi,
)
from tradedesk.collector.scheduler import _retention_sweep, build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime

FIXDIR = Path(__file__).parent / "fixtures/doubletop"


def _load(name):
    return json.loads((FIXDIR / name).read_text())[0]["history"]


OI_COINS = _load("coinalyze__open_interest_history_BTCUSDT_PERP.A_daily_2026-10-18.json")
FUND = _load("coinalyze__funding_rate_history_BTCUSDT_PERP.A_daily_2026-10-18.json")
LSR = _load("coinalyze__long_short_ratio_history_BTCUSDT_PERP.A_daily_2026-10-18.json")
PRICE = 80_000.0  # synthetic conversion of the coin sample to USD
OI_USD = [{"t": h["t"], **{k: h[k] * PRICE for k in "ohlc"}} for h in OI_COINS]
SECRET = "k3y-SECRET-0123"
NOW = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)  # the sample's last bar (2026-10-17) is closed
TODAY = int(NOW.timestamp()) // DAY * DAY
FIRST, LAST = OI_COINS[0]["t"], OI_COINS[-1]["t"]
PATHS = {"/v1/open-interest-history": "oi_usd", "/v1/funding-rate-history": "funding", "/v1/long-short-ratio-history": "long_short"}


def sample_handler(oi=OI_USD, fail=()):
    samples = {"oi_usd": oi, "funding": FUND, "long_short": LSR}

    def handler(req):
        metric = PATHS[req.url.path]
        if metric in fail:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[{"symbol": req.url.params["symbols"], "history": samples[metric]}])

    return handler


def make_rt(tmp_path, handler, key=SECRET, **kw):
    seen: list[httpx.Request] = []

    def wrapped(req):
        seen.append(req)
        return handler(req)

    s = Settings(http_retries=0, cache_scale=0.0, coinalyze_api_key=key, db_path=str(tmp_path / "c.sqlite"), **kw)
    rt = Runtime(s, transports={"coinalyze": httpx.MockTransport(wrapped)})
    rt.coinalyze.http._limiter = TokenBucket(1_000.0, 1_000)  # the 40/min production limit only slows the test
    rt.now = lambda: NOW
    return rt, seen


# ---- pure helpers -------------------------------------------------------------------------------------------


def test_plan_window_backfill_resume_and_up_to_date():
    now = TODAY + 6 * 3600
    # Nothing stored: from backfill_days ago, at most CHUNK_DAYS.
    assert plan_window(now, None, None, 365, 365) == (TODAY - 365 * DAY, TODAY - 365 * DAY + CHUNK_DAYS * DAY)
    # Short backfill: the window stops at today 00:00 UTC (only closed days).
    assert plan_window(now, None, None, 10, 365) == (TODAY - 10 * DAY, TODAY)
    # Retention shorter than backfill bounds the start (the sweep would delete older rows and the backfill refetch them).
    assert plan_window(now, None, None, 365, 30) == (TODAY - 30 * DAY, TODAY)
    # Retention 0 = never prune: backfill_days alone.
    assert plan_window(now, None, None, 500, 0)[0] == TODAY - 500 * DAY
    # Resume the day after the last stored bar; up to date once yesterday is stored.
    assert plan_window(now, TODAY - 3 * DAY, None, 365, 365) == (TODAY - 2 * DAY, TODAY)
    assert plan_window(now, TODAY - DAY, None, 365, 365) is None
    # The cursor (the end of an empty window already walked) is a lower bound on the start.
    assert plan_window(now, None, TODAY - 5 * DAY, 365, 365) == (TODAY - 5 * DAY, TODAY)
    assert plan_window(now, None, TODAY, 365, 365) is None
    assert plan_window(now, TODAY - 300 * DAY, TODAY - 50 * DAY, 365, 0) == (TODAY - 50 * DAY, TODAY)
    assert plan_window(now, TODAY - 3 * DAY, TODAY - 50 * DAY, 365, 0) == (TODAY - 2 * DAY, TODAY)


def test_plan_window_backfill_days_below_one_still_collects_new_days():
    now = TODAY + 6 * 3600
    assert plan_window(now, None, None, 0, 0) == (TODAY - DAY, TODAY)
    assert plan_window(now, None, None, -5, 365) == (TODAY - DAY, TODAY)
    assert plan_window(now, TODAY - DAY, None, 0, 0) is None


def test_plan_window_waits_for_the_day_to_settle():
    # 00:30 UTC: yesterday is closed but not settled, so it is not asked yet.
    assert plan_window(TODAY + 1800, TODAY - 2 * DAY, None, 365, 0) is None
    assert plan_window(TODAY + 1800, None, None, 10, 0) == (TODAY - 11 * DAY, TODAY - DAY)
    # 02:30 UTC: it is.
    assert plan_window(TODAY + 9000, TODAY - 2 * DAY, None, 365, 0) == (TODAY - DAY, TODAY)


def test_history_rows_units_and_window():
    f = history_rows("funding", FUND, 0, 2**40)
    assert [r["ts"] for r in f] == [h["t"] for h in FUND]
    for r, h in zip(f, FUND, strict=True):  # percent -> decimal fraction per funding interval
        assert r["close"] == pytest.approx(h["c"] / 100) and r["high"] == pytest.approx(h["h"] / 100)
    ls = history_rows("long_short", LSR, 0, 2**40)
    assert ls[-1]["ratio"] == pytest.approx(LSR[-1]["r"])
    assert ls[-1]["long_share"] == pytest.approx(LSR[-1]["l"] / 100) and 0 <= ls[-1]["short_share"] <= 1
    oi = history_rows("oi_usd", OI_USD, FIRST + DAY, LAST)  # [start, end): drops the first and the last bar
    assert [r["ts"] for r in oi] == [FIRST + DAY, FIRST + 2 * DAY]
    assert history_rows("funding", "not a list", 0, 2**40) == []
    bad = [{"t": "x", "o": 1, "h": 1, "l": 1, "c": 1}, {"t": FIRST, "o": 1, "h": 1, "l": 1, "c": float("nan")}]
    assert history_rows("funding", bad, 0, 2**40) == []


def test_oi_unit_guard_on_the_coin_sample():
    assert oi_not_usd(history_rows("oi_usd", OI_COINS, 0, 2**40))  # ~94k BTC: coins
    assert not oi_not_usd(history_rows("oi_usd", OI_USD, 0, 2**40))  # ~7.5e9 USD
    assert not oi_not_usd([])


def test_oi_unit_guard_uses_the_median_and_drops_single_bad_bars():
    usd = history_rows("oi_usd", OI_USD, 0, 2**40)
    one_zero = [dict(r, close=0.0) if i == 1 else r for i, r in enumerate(usd)]
    assert not oi_not_usd(one_zero)  # one damaged bar does not refuse a USD series
    kept, dropped = split_bad_oi(one_zero)
    assert [r["ts"] for r in dropped] == [usd[1]["ts"]] and len(kept) == 3
    coins = history_rows("oi_usd", OI_COINS, 0, 2**40)
    assert oi_not_usd([dict(coins[0], close=8e9), *coins[1:]])  # one USD-looking bar does not pass a coin series


# ---- the job ------------------------------------------------------------------------------------------------


async def test_job_happy_path_writes_all_series(tmp_path):
    rt, seen = make_rt(tmp_path, sample_handler(), collect_coinalyze_history_backfill_days=10)
    try:
        written = await CoinalyzeHistoryJob()(rt)
        assert written == len(SYMBOLS) * len(METRICS) * 4
        assert len(seen) == 6
        for req in seen:
            assert req.headers["api_key"] == SECRET and SECRET not in str(req.url)
            assert req.url.params["interval"] == "daily"
            assert int(req.url.params["from"]) == TODAY - 10 * DAY and int(req.url.params["to"]) == TODAY - 1
        oi_req = next(r for r in seen if r.url.path == "/v1/open-interest-history")
        assert oi_req.url.params["convert_to_usd"] == "true"
        assert {r.url.params["symbols"] for r in seen} == {"BTCUSDT_PERP.A", "ETHUSDT_PERP.A"}
        fund = await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "funding")
        assert [r["ts"] for r in fund] == [h["t"] for h in FUND]
        assert fund[-1]["close"] == pytest.approx(FUND[-1]["c"] / 100) and fund[-1]["ratio"] is None
        ls = await rt.store.coinalyze_history_rows("ETHUSDT_PERP.A", "long_short")
        assert ls[-1]["long_share"] == pytest.approx(LSR[-1]["l"] / 100) and ls[-1]["close"] is None
        oi = await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "oi_usd")
        assert oi[-1]["close"] == pytest.approx(OI_COINS[-1]["c"] * PRICE) and oi[-1]["fetched_ts"] == int(NOW.timestamp())
    finally:
        await rt.aclose()
    raw = (tmp_path / "c.sqlite").read_bytes()
    assert SECRET.encode() not in raw


async def test_job_is_idempotent_and_resumes(tmp_path):
    rt, seen = make_rt(tmp_path, sample_handler(), collect_coinalyze_history_backfill_days=10)
    job = CoinalyzeHistoryJob()
    try:
        assert await job(rt) == 24
        n = len(seen)
        # Same day: every series is up to date, no request, nothing rewritten.
        assert await job(rt) == 0 and len(seen) == n
        # A repeated bar replaces the old one: same row count, new values.
        rows = await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "funding")
        assert await rt.store.insert_coinalyze_history("BTCUSDT_PERP.A", "funding", [dict(rows[-1], close=0.5)], 1) == 1
        again = await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "funding")
        assert len(again) == len(rows) and again[-1]["close"] == 0.5 and again[-1]["fetched_ts"] == 1
        # Next day: resume from the day after the last stored bar; the overlapping sample rows are filtered out.
        rt.now = lambda: datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
        assert await job(rt) == 0
        new = seen[n:]
        assert len(new) == 6 and {int(r.url.params["from"]) for r in new} == {LAST + DAY}
        assert len(await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "oi_usd")) == 4
    finally:
        await rt.aclose()


async def test_backfill_is_chunked_and_walks_past_empty_windows_with_a_warning(tmp_path, caplog):
    rt, seen = make_rt(tmp_path, lambda req: httpx.Response(200, json=[]))  # defaults: backfill 1460, no retention
    job = CoinalyzeHistoryJob()
    start = TODAY - 1460 * DAY
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await job(rt) == 0
        froms = {int(r.url.params["from"]) for r in seen}
        tos = {int(r.url.params["to"]) for r in seen}
        assert froms == {start} and tos == {start + CHUNK_DAYS * DAY - 1}
        # Every skipped window is logged, one line per series.
        lo, hi = (datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d") for t in (start, start + CHUNK_DAYS * DAY))
        assert caplog.text.count(f"empty for [{lo}, {hi}), skipped") == 6
        assert "BTCUSDT_PERP.A funding empty" in caplog.text
        n = len(seen)
        assert await job(rt) == 0  # the empty window is not asked again: the next chunk is
        assert {int(r.url.params["from"]) for r in seen[n:]} == {start + CHUNK_DAYS * DAY}
    finally:
        await rt.aclose()


async def test_empty_window_up_to_the_last_settled_day_is_retried_not_skipped(tmp_path, caplog):
    rt, seen = make_rt(tmp_path, lambda req: httpx.Response(200, json=[]), collect_coinalyze_history_backfill_days=10)
    job = CoinalyzeHistoryJob()
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await job(rt) == 0
            assert await job(rt) == 0
        assert len(seen) == 12  # the same six requests twice: the cursor did not move
        assert {int(r.url.params["from"]) for r in seen} == {TODAY - 10 * DAY}
        assert job.cursor == {}
        assert caplog.text.count("up to the last settled day, retried next run") == 12
        assert "skipped" not in caplog.text
    finally:
        await rt.aclose()


async def test_yesterday_is_fetched_only_after_the_settle_delay(tmp_path):
    rt, seen = make_rt(tmp_path, sample_handler(), collect_coinalyze_history_backfill_days=10)
    job = CoinalyzeHistoryJob()
    try:
        rt.now = lambda: datetime(2026, 10, 18, 0, 30, tzinfo=UTC)  # LAST (2026-10-17) closed 30 min ago
        assert await job(rt) == 18  # three bars per series, not LAST
        assert {int(r.url.params["to"]) for r in seen} == {LAST - 1}
        assert await rt.store.coinalyze_history_last_ts("BTCUSDT_PERP.A", "oi_usd") == LAST - DAY
        n = len(seen)
        rt.now = lambda: datetime(2026, 10, 18, 1, 59, tzinfo=UTC)
        assert await job(rt) == 0 and len(seen) == n  # still settling: no request
        rt.now = lambda: datetime(2026, 10, 18, 2, 30, tzinfo=UTC)
        assert await job(rt) == 6  # LAST of each series
        assert {int(r.url.params["from"]) for r in seen[n:]} == {LAST}
        assert await rt.store.coinalyze_history_last_ts("ETHUSDT_PERP.A", "long_short") == LAST
    finally:
        await rt.aclose()


async def test_provider_failure_records_error_and_keeps_other_series(tmp_path):
    rt, _ = make_rt(tmp_path, sample_handler(fail=("funding",)), collect_coinalyze_history_backfill_days=10)
    try:
        assert await CoinalyzeHistoryJob()(rt) == 16  # OI and long/short of both symbols
        assert await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "funding") == []
    finally:
        await rt.aclose()


async def test_all_requests_failing_raises_for_the_scheduler(tmp_path):
    rt, _ = make_rt(tmp_path, sample_handler(fail=METRICS), collect_coinalyze_history_backfill_days=10)
    try:
        with pytest.raises(RuntimeError, match="all 6 requests failed") as exc:
            await CoinalyzeHistoryJob()(rt)
        assert SECRET not in str(exc.value)
    finally:
        await rt.aclose()


async def test_oi_in_coins_is_refused_not_stored(tmp_path, caplog):
    rt, _ = make_rt(tmp_path, sample_handler(oi=OI_COINS), collect_coinalyze_history_backfill_days=10)
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await CoinalyzeHistoryJob()(rt) == 16
        assert await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "oi_usd") == []
        assert "oi_usd:BTCUSDT_PERP.A: data_unavailable" in caplog.text
    finally:
        await rt.aclose()


async def test_one_damaged_oi_bar_is_dropped_the_rest_is_stored(tmp_path, caplog):
    oi = [dict(h, c=0.0) if i == 1 else h for i, h in enumerate(OI_USD)]
    rt, _ = make_rt(tmp_path, sample_handler(oi=oi), collect_coinalyze_history_backfill_days=10)
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await CoinalyzeHistoryJob()(rt) == 24 - 2  # one bar fewer in each symbol's OI
        stored = await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "oi_usd")
        assert [r["ts"] for r in stored] == [FIRST, FIRST + 2 * DAY, LAST]
        bad_day = datetime.fromtimestamp(FIRST + DAY, UTC).strftime("%Y-%m-%d")
        assert f"BTCUSDT_PERP.A oi_usd dropped 1 damaged bar(s) with close below 1e+07: {bad_day}" in caplog.text
        assert "data_unavailable" not in caplog.text
    finally:
        await rt.aclose()


async def test_no_key_is_a_logged_no_op(tmp_path, caplog):
    rt, seen = make_rt(tmp_path, sample_handler(), key=None)
    job = CoinalyzeHistoryJob()
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await job(rt) == 0
            assert await job(rt) == 0
        assert seen == []
        assert caplog.text.count("COINALYZE_API_KEY is not set") == 1
    finally:
        await rt.aclose()


async def test_scheduled_retention_sweep_prunes_the_table_only_when_set(tmp_path):
    rt, _ = make_rt(tmp_path, sample_handler(), collect_coinalyze_history_backfill_days=10)
    try:
        assert await CoinalyzeHistoryJob()(rt) == 24
        # Default retention 0: the sweep leaves this table alone (every other table here is empty).
        assert await _retention_sweep(rt) == 0
        # 4 days at 2026-10-18 06:00 UTC: cutoff 2026-10-14 06:00, so only the first bar (FIRST, 00:00) goes.
        rt.settings = dataclasses.replace(rt.settings, retention_coinalyze_history_days=4)
        assert await _retention_sweep(rt) == 6  # the first bar of each of the six series
        assert [r["ts"] for r in await rt.store.coinalyze_history_rows("BTCUSDT_PERP.A", "funding")] == [FIRST + DAY, FIRST + 2 * DAY, LAST]
    finally:
        await rt.aclose()


def test_scheduler_registers_the_job_only_when_enabled(tmp_path):
    base = {"collector_enabled": True, "db_path": str(tmp_path / "s.sqlite")}
    off = build_default_scheduler(Runtime(Settings(**base)))
    on = build_default_scheduler(Runtime(Settings(**base, collect_coinalyze_history_enabled=True)))
    assert "coinalyze_history" not in {j.name for j in off.jobs}
    assert "coinalyze_history" in {j.name for j in on.jobs}


def test_env_flag_is_off_by_default(monkeypatch):
    monkeypatch.setenv("TRADEDESK_COLLECT_COINALYZE_HISTORY_ENABLED", "1")
    s = Settings.from_env()
    assert s.collect_coinalyze_history_enabled and s.retention_coinalyze_history_days == 0  # never pruned
    assert s.collect_coinalyze_history_backfill_days == 1460
    monkeypatch.delenv("TRADEDESK_COLLECT_COINALYZE_HISTORY_ENABLED")
    assert not Settings.from_env().collect_coinalyze_history_enabled


async def test_migration_from_26(tmp_path):
    from tradedesk.store import SCHEMA_VERSION, Store

    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version = 26")
    con.commit()
    con.close()
    s = Store(path)
    try:
        await s.init()
        assert await s.version() == SCHEMA_VERSION >= 27
        assert await s.coinalyze_history_last_ts("BTCUSDT_PERP.A", "funding") is None
    finally:
        await s.aclose()
