from __future__ import annotations

import asyncio
from dataclasses import replace

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector.scheduler import PeriodicJob, Scheduler, _collect_atm_term
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
