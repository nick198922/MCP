from __future__ import annotations

import asyncio
from dataclasses import replace

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.scheduler import (
    PeriodicJob,
    Scheduler,
    _collect_atm_term,
    _collect_basis,
    _collect_funding,
    _evaluate_alerts,
    _retention_sweep,
)
from tradedesk.config import Settings
from tradedesk.runtime import Runtime


async def _rt_with_memory_store(state) -> Runtime:
    settings = Settings(http_retries=1, cache_scale=0.0, db_path=":memory:", collector_enabled=True)
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_atm_term_job_writes_rows(state):
    rt = await _rt_with_memory_store(state)
    try:
        written = await _collect_atm_term(rt)
        # Fake Deribit returns the same option_book for BTC and ETH → 3 expiries × 2 currencies.
        assert written == 6
        assert await rt.store.table_count("atm_term") == 6
        series = await rt.store.atm_term_series("BTC", "2026-10-30T08:00:00+00:00", 0, 2_000_000_000)
        assert len(series) == 1 and series[0]["atm_iv"] > 0
    finally:
        await rt.aclose()


async def test_scheduler_runs_once_and_stops(state):
    rt = await _rt_with_memory_store(state)
    # Fast period just to confirm the loop ticks once; test will stop immediately.
    sched = Scheduler(rt, [PeriodicJob(name="atm_term", period_seconds=60, fn=_collect_atm_term)])
    try:
        await sched.start()
        # Wait for the first iteration to complete.
        for _ in range(50):
            if sched.jobs[0].metrics.runs > 0:
                break
            await asyncio.sleep(0.05)
        assert sched.jobs[0].metrics.runs == 1
        assert sched.jobs[0].metrics.successes == 1
        assert sched.jobs[0].metrics.failures == 0
        assert sched.jobs[0].metrics.last_rows_written == 6
    finally:
        await sched.stop()
        await rt.aclose()


async def test_scheduler_captures_failure(state):
    rt = await _rt_with_memory_store(state)

    async def boom(_: Runtime) -> int:
        raise RuntimeError("nope")

    sched = Scheduler(rt, [PeriodicJob(name="boom", period_seconds=60, fn=boom)])
    try:
        await sched.start()
        for _ in range(50):
            if sched.jobs[0].metrics.runs > 0:
                break
            await asyncio.sleep(0.05)
        m = sched.jobs[0].metrics
        assert m.runs == 1 and m.failures == 1 and m.successes == 0
        assert m.last_error and "nope" in m.last_error
    finally:
        await sched.stop()
        await rt.aclose()


async def test_build_default_scheduler_respects_flag(state):
    tr = make_transports(state)
    # Disabled by default.
    rt_off = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=":memory:"), transports=tr, exchange_factory=FakeEx)
    assert rt_off.store is not None
    await rt_off.store.init()
    from tradedesk.collector import build_default_scheduler  # noqa: PLC0415

    assert build_default_scheduler(rt_off) is None
    await rt_off.aclose()

    # Enabled but no store path → still None.
    rt_no_db = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path="", collector_enabled=True), transports=tr, exchange_factory=FakeEx
    )
    assert rt_no_db.store is None
    assert build_default_scheduler(rt_no_db) is None
    await rt_no_db.aclose()

    # Keep the Settings-defaults contract explicit.
    _ = replace  # silence unused-import complaints when the file is edited later


async def test_funding_job_writes_rows(state):
    rt = await _rt_with_memory_store(state)
    try:
        written = await _collect_funding(rt)
        # FakeEx: binanceusdm + bybit yield funding (OKX raises); HL provides BTC+ETH;
        # Deribit perpetual has funding_8h in the FUTURES fixture → 1 row per currency.
        # Expect something non-trivial for BTC and ETH, plus HL + Deribit rows.
        assert written >= 4
        snap = await rt.store.funding_series("binanceusdm", "BTC", 0, 2_000_000_000)
        assert len(snap) == 1 and snap[0]["annualized"] != 0
    finally:
        await rt.aclose()


async def test_basis_job_writes_rows(state):
    rt = await _rt_with_memory_store(state)
    try:
        written = await _collect_basis(rt)
        # Fake Deribit FUTURES fixture (3 dated + 1 perp) is returned for both BTC and ETH
        # in the fake transport → 3 × 2 = 6 dated rows total.
        assert written == 6
        assert await rt.store.table_count("basis_hist") == 6
    finally:
        await rt.aclose()


async def test_evaluate_alerts_fires_and_writes(state, tmp_path):
    rules_path = tmp_path / "rules.json"
    import json

    rules_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "id": "funding_hot",
                        "enabled": True,
                        "cooldown_seconds": 600,
                        "metric": {"kind": "funding", "venue": "binanceusdm", "base": "BTC"},
                        "when": {"op": ">", "value": 0.30},
                        "message": "hot {value_pct:.1f}%",
                    }
                ]
            }
        )
    )
    settings = Settings(
        http_retries=1,
        cache_scale=0.0,
        db_path=":memory:",
        collector_enabled=True,
        alerts_enabled=True,
        alerts_rules_path=str(rules_path),
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    try:
        # Seed a funding row above threshold at NOW.
        await rt.store.insert_funding(
            ts=int(NOW.timestamp()),
            rows=[
                {
                    "venue": "binanceusdm",
                    "base": "BTC",
                    "rate_per_interval": 0.0003,
                    "interval_hours": 8.0,
                    "annualized": 0.32,
                }
            ],
        )
        rows = await _evaluate_alerts(rt)
        assert rows == 1
        recent = await rt.store.recent_alerts()
        assert len(recent) == 1
        assert recent[0]["rule_id"] == "funding_hot"
        assert recent[0]["channel"] == "log"
        assert recent[0]["delivered"] is True

        # Second evaluation within the cooldown must not create a new row.
        rows2 = await _evaluate_alerts(rt)
        assert rows2 == 0
    finally:
        await rt.aclose()


async def test_retention_sweep_deletes_old_rows(state):
    settings = Settings(
        http_retries=1,
        cache_scale=0.0,
        db_path=":memory:",
        collector_enabled=True,
        retention_atm_term_days=5,
        retention_funding_days=0,  # 0 disables prune for that table
        retention_basis_days=0,
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    try:
        now_ts = int(rt.now().timestamp())
        rows = [
            {"expiry": "2026-10-30T08:00:00+00:00", "days": 20.0, "forward": 83000.0, "atm_iv": 0.42},
        ]
        await rt.store.insert_atm_term(ts=now_ts - 10 * 86_400, currency="BTC", rows=rows)  # 10 days ago
        await rt.store.insert_atm_term(ts=now_ts - 1 * 86_400, currency="BTC", rows=rows)  # 1 day ago
        assert await rt.store.table_count("atm_term") == 2
        deleted = await _retention_sweep(rt)
        assert deleted == 1
        assert await rt.store.table_count("atm_term") == 1
    finally:
        await rt.aclose()
