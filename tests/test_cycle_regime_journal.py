"""Cycle regime journal (IDEA-20261028-btc-cycle-regime-gauge, step 2): analytics/cycle_regime_journal.py, the collector
job collector/cycle_regime_log.py, the store table cycle_regime_log and scripts/cycle_regime_eval.py."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from test_cycle_regime import FX_NOW, FixtureEx, _down, _fx_handler, serve_daily
from tradedesk.analytics import cycle_regime as cr
from tradedesk.analytics import cycle_regime_journal as crj
from tradedesk.collector.cycle_regime_log import collect_cycle_regime_log
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cycle_regime_eval  # noqa: E402

DAY = cr.DAY_MS
DATES = ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"]


# ---------------------------------------------------------------------------------------------------- due days


def _dates(closes: list[int]) -> list[str]:
    return [datetime.fromtimestamp((c - DAY) / 1000, UTC).date().isoformat() for c in closes]


def test_due_days_wait_five_minutes_after_the_close_and_skip_stored_days():
    at_0003 = datetime(2026, 10, 28, 0, 3, tzinfo=UTC)
    at_0005 = datetime(2026, 10, 28, 0, 5, tzinfo=UTC)
    assert _dates(crj.due_days(at_0003)) == ["2026-10-20", *DATES[:-1]]  # 27.10 closed 3 min ago: not yet
    assert _dates(crj.due_days(at_0005)) == DATES  # one week back, oldest first
    assert _dates(crj.due_days(at_0005, have={"2026-10-27", "2026-10-22"})) == ["2026-10-21", *DATES[2:-1]]
    assert crj.due_days(at_0005, have=DATES) == []
    assert crj.due_days(at_0005)[-1] == int(datetime(2026, 10, 28, tzinfo=UTC).timestamp() * 1000)


# ------------------------------------------------------------------------------------------- evaluation (analytic)

# Ten days, close 100, 110, ..., 190; states A A G G G A - A A A (A = accumulation, G = growth, - = undecided).
CLOSES = {f"2026-01-{i:02d}": 100.0 + 10 * (i - 1) for i in range(1, 11)}
STATES = ["accumulation", "accumulation", "growth", "growth", "growth", "accumulation", None, "accumulation", "accumulation",
          "accumulation"]  # fmt: skip
ROWS = [{"date": d, "state": s, "close": c, "version": cr.VERSION} for (d, c), s in zip(CLOSES.items(), STATES, strict=True)]
NOW_EV = datetime(2026, 1, 11, 12, 0, tzinfo=UTC)  # the last closed day is 10.01


def r(a: int, b: int) -> float:
    """Return from day a to day b of January (closes 100 + 10 (day - 1))."""
    return CLOSES[f"2026-01-{b:02d}"] / CLOSES[f"2026-01-{a:02d}"] - 1


def test_episodes_break_on_a_change_and_on_an_undecided_day():
    eps = crj.episodes(ROWS)
    assert [(e["state"], e["start"], e["days"]) for e in eps] == [
        ("accumulation", "2026-01-01", 2), ("growth", "2026-01-03", 3), ("accumulation", "2026-01-06", 1),
        ("accumulation", "2026-01-08", 3),
    ]  # fmt: skip


def test_evaluator_on_a_small_analytic_example():
    ev = crj.evaluate_journal(ROWS + [{"date": "2026-01-03", "state": "decline", "close": 1.0}, {"date": "x", "close": 5}], NOW_EV, (2, 5))
    assert ev["rows"] == 10 and ev["first"] == "2026-01-01" and ev["last"] == "2026-01-10"  # duplicates and bad rows dropped
    assert ev["undecided_days"] == 1 and ev["gap_days"] == 0 and ev["episodes_total"] == 4 and ev["versions"] == [cr.VERSION]
    assert ev["thin"] and ev["months"] == pytest.approx(10 / 30.44)
    acc, gro = ev["states"]["accumulation"], ev["states"]["growth"]
    assert (acc["days"], acc["episodes"], gro["days"], gro["episodes"]) == (6, 3, 3, 1)
    a2 = acc["horizons"]["2"]
    # accumulation days 1, 2, 6, 8 have their day + 2 inside the journal; 9 and 10 are pending
    assert a2["by_day"]["n"] == 4 and a2["pending_days"] == 2 and a2["no_price_days"] == 0
    assert a2["by_day"]["mean"] == pytest.approx((r(1, 3) + r(2, 4) + r(6, 8) + r(8, 10)) / 4, abs=1e-12)
    assert a2["by_day"]["share_up"] == 1.0
    assert a2["by_episode"]["n"] == 3  # episodes start on 1, 6, 8
    assert a2["by_episode"]["mean"] == pytest.approx((r(1, 3) + r(6, 8) + r(8, 10)) / 3, abs=1e-12)
    assert a2["by_episode"]["median"] == pytest.approx(r(6, 8), abs=1e-12)
    g5 = gro["horizons"]["5"]
    assert g5["by_day"]["n"] == 3 and g5["by_day"]["mean"] == pytest.approx((r(3, 8) + r(4, 9) + r(5, 10)) / 3, abs=1e-12)
    assert g5["by_episode"]["n"] == 1 and g5["by_episode"]["mean"] == pytest.approx(r(3, 8), abs=1e-12)
    a5 = acc["horizons"]["5"]
    assert a5["by_day"]["n"] == 2 and a5["pending_days"] == 4  # only days 1 and 2 have day + 5 <= 10
    # the baseline: every day with a state (not the undecided 7th)
    assert ev["all_days"]["2"]["n"] == 7 and ev["all_days"]["2"]["mean"] == pytest.approx(
        (r(1, 3) + r(2, 4) + r(3, 5) + r(4, 6) + r(5, 7) + r(6, 8) + r(8, 10)) / 7, abs=1e-12
    )
    assert ev["states"]["bottom"]["days"] == 0 and ev["states"]["bottom"]["horizons"]["2"]["by_day"]["mean"] is None


def test_evaluator_fills_only_elapsed_horizons_and_counts_gaps():
    rows = [x for x in ROWS if x["date"] != "2026-01-04"]  # a missing day: its close is not there
    ev = crj.evaluate_journal(rows, NOW_EV, (2,))
    assert ev["gap_days"] == 1 and ev["states"]["accumulation"]["horizons"]["2"]["no_price_days"] == 1  # day 2 + 2 = 4
    ev = crj.evaluate_journal(ROWS, datetime(2026, 1, 10, 23, 0, tzinfo=UTC), (2,))  # 10.01 has not closed yet
    assert ev["states"]["accumulation"]["horizons"]["2"]["by_day"]["n"] == 3  # day 8 -> 10 is pending now
    assert crj.evaluate_journal([], NOW_EV)["rows"] == 0


def test_render_ua_says_the_data_is_thin_and_shows_n():
    text = crj.render_ua(crj.evaluate_journal(ROWS, NOW_EV, (2, 5)))
    assert text.startswith("# Режим циклу BTC: стан → дохідність BTC за 2 / 5 днів (журнал)")
    assert "**Даних замало:**" in text and "12 місяців" in text and "Контекст, не сигнал" in text
    acc = next(line for line in text.splitlines() if line.startswith("| накопичення |"))
    assert acc.startswith("| накопичення | 6 | 3 | ") and "(n=4)" in acc and "(n=3), мало" in acc
    assert "| усі дні (безумовно) | 9 | 4 |" in text
    assert "порожній" in crj.render_ua(crj.evaluate_journal([], NOW_EV))
    two = [dict(x, version="cycle-regime-v0") if x["date"] == "2026-01-01" else x for x in ROWS]
    assert "кілька версій правил" in crj.render_ua(crj.evaluate_journal(two, NOW_EV))


# ------------------------------------------------------------------------------------------------ the job (fixture)


def _rt(tmp_path, spot=_fx_handler, futures=_fx_handler, ex=FixtureEx, now=FX_NOW, **kw) -> tuple[Runtime, list]:
    seen: list = []

    def spy(handler):
        def h(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.path)
            return handler(req)

        return h

    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite"), **kw),
        transports={"binance_spot": httpx.MockTransport(spy(spot)), "binance_futures": httpx.MockTransport(spy(futures))},
        exchange_factory=ex,
    )
    rt.now = lambda: now
    return rt, seen


async def test_job_writes_the_last_week_on_real_binance_responses_and_is_idempotent(tmp_path):
    rt, seen = _rt(tmp_path)
    try:
        assert await collect_cycle_regime_log(rt) == 7
        rows = await rt.store.cycle_regime_log_rows("BTC")
        assert [x["date"] for x in rows] == DATES
        last = rows[-1]
        # the same numbers as the tool on the same responses (tests/test_cycle_regime.py) and the critic's live call
        assert last["state"] == "accumulation" and last["trend"] == "growth" and last["trend_confirmed"] == 0
        assert last["close"] == pytest.approx(80909.8, abs=0.1) and last["sma365"] == pytest.approx(77494.7, abs=0.1)
        assert last["rsi14"] == pytest.approx(45.49, abs=0.01) and last["basis7"] == pytest.approx(-0.000465, abs=1e-6)
        assert last["funding7"] == pytest.approx(0.0635, abs=1e-4) and last["premium7"] == pytest.approx(-0.000435, abs=1e-6)
        assert last["perp_close"] > 0 and last["missing"] is None and last["version"] == cr.VERSION
        assert last["created_ts"] == int(FX_NOW.timestamp())
        assert last["components"]["funding_settlements"] == 21 and last["components"]["premium_negative_days"] == 7
        # each day equals the tool's history state for it (computed from data up to that day's close only)
        ref = cr.cycle_regime(*_fixture_series(), FX_NOW, history_days=7)
        assert [x["state"] for x in rows] == [h["state"] for h in ref["history"]]
        assert [x["close"] for x in rows] == [h["close"] for h in ref["history"]]
        n_requests = len(seen)
        assert n_requests >= 3
        # re-run: nothing due, no request, nothing duplicated
        assert await collect_cycle_regime_log(rt) == 0 and len(seen) == n_requests
        assert await rt.store.table_count("cycle_regime_log") == 7
    finally:
        await rt.aclose()


def _fixture_series():
    from test_cycle_regime import FX

    spot = cr.parse_closes(FX["binance_spot_klines_1d"])
    perp = cr.parse_closes(FX["binance_perp_klines_1d"])
    prem = cr.parse_closes(FX["binance_premium_index_klines_1d"], positive=False)
    funding = [(int(x["fundingTime"]), float(x["fundingRate"])) for x in FX["binance_funding_rate"]]
    return spot, perp, funding, prem


async def test_a_stored_day_is_never_overwritten(tmp_path, caplog):
    rt, _ = _rt(tmp_path)
    try:
        await rt.store.init()
        await collect_cycle_regime_log(rt)
        stored = (await rt.store.cycle_regime_log_rows("BTC", since="2026-10-27"))[0]
        other = {k: stored[k] for k in rt.store.CYCLE_REGIME_LOG_COLUMNS} | {"state": "decline", "components": "{}"}
        assert await rt.store.cycle_regime_log_insert(other) == 0  # INSERT OR IGNORE: the first row stays
        assert (await rt.store.cycle_regime_log_rows("BTC", since="2026-10-27"))[0]["state"] == "accumulation"

        # a concurrent writer stored a different state between our date check and our insert: kept and logged
        await rt.store.conn.execute("UPDATE cycle_regime_log SET state = 'decline' WHERE date = '2026-10-27'")
        await rt.store.conn.commit()

        async def nothing_stored(base, since=""):
            return set()

        rt.store.cycle_regime_log_dates = nothing_stored
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_cycle_regime_log(rt) == 0
        assert "stored state decline kept, this run computed accumulation" in caplog.text
        assert (await rt.store.cycle_regime_log_rows("BTC", since="2026-10-27"))[0]["state"] == "decline"
    finally:
        await rt.aclose()


async def test_provider_failure_writes_nothing_never_raises_and_heals(tmp_path, caplog):
    from test_cycle_regime import DownEx

    rt, _ = _rt(tmp_path, futures=_down, ex=DownEx)  # perp, premium index and funding down
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_cycle_regime_log(rt) == 0
        assert "7 due day(s) not written" in caplog.text and await rt.store.table_count("cycle_regime_log") == 0
    finally:
        await rt.aclose()
    rt, _ = _rt(tmp_path, spot=_down)  # spot down
    try:
        assert await collect_cycle_regime_log(rt) == 0 and await rt.store.table_count("cycle_regime_log") == 0
    finally:
        await rt.aclose()
    rt, _ = _rt(tmp_path)  # the next tick, venues back: the whole week is filled
    try:
        assert await collect_cycle_regime_log(rt) == 7
    finally:
        await rt.aclose()


async def test_premium_index_failure_is_written_as_missing(tmp_path):
    def no_premium(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/fapi/v1/premiumIndexKlines":
            return httpx.Response(503, text="maintenance")
        return serve_daily(req) or httpx.Response(404, text="not mocked")

    rt, _ = _rt(tmp_path, futures=no_premium)
    try:
        assert await collect_cycle_regime_log(rt) == 7
        last = (await rt.store.cycle_regime_log_rows("BTC"))[-1]
        assert last["premium7"] is None and last["missing"] == "premium_index" and last["state"] == "accumulation"
    finally:
        await rt.aclose()


async def test_before_00_05_utc_the_closing_day_waits(tmp_path):
    rt, _ = _rt(tmp_path, now=datetime(2026, 10, 28, 0, 3, tzinfo=UTC))
    try:
        assert await collect_cycle_regime_log(rt) == 7
        assert [x["date"] for x in await rt.store.cycle_regime_log_rows("BTC")] == ["2026-10-20", *DATES[:-1]]
    finally:
        await rt.aclose()


async def test_no_store_never_raises(tmp_path):
    rt, seen = _rt(tmp_path)
    try:
        rt.store = None
        assert await collect_cycle_regime_log(rt) == 0 and seen == []
    finally:
        await rt.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    s = Settings()
    assert s.collect_cycle_regime_enabled is False and s.collect_cycle_regime_seconds == 300
    assert allowed("TRADEDESK_COLLECT_CYCLE_REGIME_ENABLED") and allowed("TRADEDESK_COLLECT_CYCLE_REGIME_SECONDS")
    monkeypatch.setenv("TRADEDESK_COLLECT_CYCLE_REGIME_ENABLED", "1")
    assert Settings.from_env().collect_cycle_regime_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_cycle_regime_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "cycle_regime_log" not in names(off) and "cycle_regime_log" in names(on)
    job = next(j for j in build_default_scheduler(on).jobs if j.name == "cycle_regime_log")
    assert job.period_seconds == 300 and job.fn is collect_cycle_regime_log


async def test_migration_33_adds_the_table_to_a_v32_store(tmp_path):
    path = tmp_path / "v32.sqlite"
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= 32:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 32")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 33
        row = {"date": "2026-10-27", "base": "BTC", "state": "growth", "close": 80000.0, "components": json.dumps({"a": 1}),
               "version": cr.VERSION, "created_ts": 1}  # fmt: skip
        assert await s.cycle_regime_log_insert(row) == 1 and await s.cycle_regime_log_insert(row) == 0
        got = await s.cycle_regime_log_rows("BTC")
        assert len(got) == 1 and got[0]["components"] == {"a": 1} and got[0]["sma365"] is None
        assert await s.cycle_regime_log_dates("BTC", since="2026-10-28") == set()
    finally:
        await s.aclose()


def test_log_row_needs_a_state():
    assert crj.log_row({"state": None, "components": {"close": 1.0}, "date": "2026-10-27"}, "BTC", 1) is None
    assert crj.log_row({"state": "growth", "components": None, "date": "2026-10-27"}, "BTC", 1) is None


# ---------------------------------------------------------------------------------------------- the eval script


async def test_eval_script_reads_the_journal_read_only(tmp_path, capsys):
    rt, _ = _rt(tmp_path)
    try:
        await collect_cycle_regime_log(rt)
    finally:
        await rt.aclose()
    db = str(tmp_path / "x.sqlite")
    out = tmp_path / "rep" / "cycle.md"
    assert cycle_regime_eval.main(["--db", db, "--now", "2026-10-28T12:00:00Z", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert text == out.read_text(encoding="utf-8")
    assert "Журнал: 2026-10-21 … 2026-10-27 (7 днів" in text and "**Даних замало:**" in text
    assert "| накопичення |" in text  # no horizon has elapsed yet: every cell is «—»
    empty = tmp_path / "empty.sqlite"
    sqlite3.connect(empty).close()
    assert cycle_regime_eval.main(["--db", str(empty)]) == 0 and "порожній" in capsys.readouterr().out
    assert cycle_regime_eval.main(["--db", str(tmp_path / "nope" / "x.sqlite")]) == 1
