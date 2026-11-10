"""Count of malformed provider rows a collector job dropped, per run and in total (item 307).

`collector/bad_rows.py`: job code calls `note_bad_rows(n)` next to its "dropped" / "stored as NULL" log line; the
scheduler opens a fresh count per run and stores `last_rows_dropped` / `rows_dropped_total` on `JobMetrics`, which
`system_health` -> `collector[]` shows. No network: the fakes of test_collector_bad_rows / test_coinalyze_history_collector.
"""

from __future__ import annotations

import asyncio
import copy
import json

from mcp import Client

import test_coinalyze_history_collector as czh
import test_coingecko_trending as cgt
import test_collector_bad_rows as tbr
import test_hl_oi_cap as hoc
import test_tradfi_spread as tfs
from conftest import wait_until
from test_collector import _rt_with_memory_store
from tradedesk.collector.bad_rows import count_bad_rows, note_bad_rows
from tradedesk.collector.coinalyze_history import CoinalyzeHistoryJob
from tradedesk.collector.market_data import funding_entries
from tradedesk.collector.scheduler import PeriodicJob, Scheduler
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# ---------- the helper ----------


def test_note_outside_a_run_is_a_noop_and_a_block_counts():
    note_bad_rows(3)  # an MCP tool calling a shared parser: no count open, nothing happens
    with count_bad_rows() as outer:
        note_bad_rows(2)
        note_bad_rows(0)
        note_bad_rows(-1)  # never lowers the count
        with count_bad_rows() as inner:
            note_bad_rows(4)
        note_bad_rows(1)
    note_bad_rows(5)
    assert (outer.n, inner.n) == (3, 4)


# ---------- the scheduler ----------


def _blocking_job(per_run: list, seen: list, job_ref: list, fail_on: frozenset[int] = frozenset()):
    """A job that notes ``per_run[k]`` bad rows in run k, records the metrics it sees at its start (the previous run's)
    and blocks forever once ``per_run`` is used up, so the metrics stay on the last counted run."""

    async def fn(_: Runtime) -> int:
        m = job_ref[0].metrics
        seen.append((m.last_rows_dropped, m.rows_dropped_total))
        k = len(seen) - 1
        if k >= len(per_run):
            await asyncio.Event().wait()
        note_bad_rows(per_run[k])
        if k in fail_on:
            raise RuntimeError(f"run {k} failed after dropping rows")
        return 10

    return fn


async def test_count_resets_per_run_and_accumulates(state):
    rt = await _rt_with_memory_store(state)
    seen: list = []
    ref: list = []
    job = PeriodicJob(name="drops", period_seconds=0, fn=_blocking_job([2, 0, 5], seen, ref))
    ref.append(job)
    sched = Scheduler(rt, [job])
    try:
        assert (job.metrics.last_rows_dropped, job.metrics.rows_dropped_total) == (None, 0)
        await sched.start()
        await wait_until(lambda: job.metrics.runs == 3)
        # at the start of run k the metrics show run k-1: the second run's 0 is not the first run's 2
        assert seen[:3] == [(None, 0), (2, 2), (0, 2)]
        assert (job.metrics.last_rows_dropped, job.metrics.rows_dropped_total) == (5, 7)
        assert job.metrics.successes == 3 and job.metrics.last_rows_written == 10
    finally:
        await sched.stop()
        await rt.aclose()


async def test_a_failed_run_keeps_what_it_dropped(state):
    rt = await _rt_with_memory_store(state)
    seen: list = []
    ref: list = []
    job = PeriodicJob(name="drops", period_seconds=0, fn=_blocking_job([3, 1], seen, ref, fail_on=frozenset({0})))
    ref.append(job)
    sched = Scheduler(rt, [job])
    try:
        await sched.start()
        await wait_until(lambda: job.metrics.runs == 2)
        assert seen[1] == (3, 3)  # the failed run: its 3 dropped rows were still lost, so they are counted
        m = job.metrics
        assert (m.failures, m.successes) == (1, 1) and "failed after dropping rows" in m.last_failure
        assert (m.last_rows_dropped, m.rows_dropped_total) == (1, 4)
    finally:
        await sched.stop()
        await rt.aclose()


async def test_concurrent_jobs_do_not_mix_counts(state):
    # Each job is its own task (its own context): interleaved notes stay with their job. Sub-tasks a job spawns with
    # gather copy the context and add to the job's count.
    rt = await _rt_with_memory_store(state)
    done = {"a": False, "b": False}

    def make(name: str, steps: int, sub: int):
        async def fn(_: Runtime) -> int:
            if done[name]:
                await asyncio.Event().wait()
            for _ in range(steps):
                note_bad_rows(1)
                await asyncio.sleep(0)  # let the other job run in between

            async def child() -> None:
                await asyncio.sleep(0)
                note_bad_rows(sub)

            await asyncio.gather(child(), child())
            done[name] = True
            return 0

        return fn

    a = PeriodicJob(name="a", period_seconds=0, fn=make("a", 10, 2))
    b = PeriodicJob(name="b", period_seconds=0, fn=make("b", 3, 0))
    sched = Scheduler(rt, [a, b])
    try:
        await sched.start()
        await wait_until(lambda: a.metrics.runs == 1 and b.metrics.runs == 1)
        assert (a.metrics.last_rows_dropped, a.metrics.rows_dropped_total) == (14, 14)
        assert (b.metrics.last_rows_dropped, b.metrics.rows_dropped_total) == (3, 3)
    finally:
        await sched.stop()
        await rt.aclose()
    note_bad_rows(1)  # the test's own context never had a count open


async def test_system_health_shows_the_counts(state, monkeypatch):
    rt = await _rt_with_memory_store(state)
    seen: list = []
    ref: list = []
    job = PeriodicJob(name="drops", period_seconds=0, fn=_blocking_job([4, 2], seen, ref))
    ref.append(job)
    sched = Scheduler(rt, [job])
    try:
        await sched.start()
        await wait_until(lambda: job.metrics.runs == 2)
        monkeypatch.setattr("tradedesk.collector.build_default_scheduler", lambda _rt: sched)
        srv, _ = build_server(rt)
        async with Client(srv) as c:
            res = await c.call_tool("system_health", {})
        data = res.structured_content or json.loads(res.content[0].text)
        data = data.get("result", data)
        (row,) = [j for j in data["data"]["collector"] if j["name"] == "drops"]
        assert (row["last_rows_dropped"], row["rows_dropped_total"]) == (2, 6)
        old = {"name", "period_seconds", "runs", "successes", "failures", "last_run_ts", "last_duration_ms"}
        old |= {"last_rows_written", "last_error", "last_failure", "last_failure_ts", "stream"}
        assert old < set(row) and row["runs"] == 2 and row["last_rows_written"] == 10  # existing keys unchanged
    finally:
        await sched.stop()
        await rt.aclose()


# ---------- the drop sites (each next to its log line) ----------


def test_funding_entries_counts_dropped_rows():
    rows = copy.deepcopy(tbr.FUNDING)
    rows[1]["fundingRate"], rows[3]["timestamp"] = "abc", 10**20
    with count_bad_rows() as c:
        assert len(funding_entries(rows)) == 3
    assert c.n == 2
    assert len(funding_entries(rows)) == 3  # the carry_paper tools call it outside a run: no count, no error


async def test_tradfi_spread_counts_nulled_fields(tmp_path, monkeypatch):
    bad = copy.deepcopy(tfs.SAMPLE)
    for part, sym, field in tbr.TRADFI:
        next(r for r in bad[part] if r.get("symbol") == sym)[field] = "abc"
    with count_bad_rows() as c:
        await tbr._tradfi_rows(tmp_path, monkeypatch, "bad", bad)
    assert c.n == len(tbr.TRADFI) == 6


async def test_coingecko_trending_counts_nulled_fields(tmp_path):
    bad = copy.deepcopy(cgt.SAMPLE)
    for rank, part, field in tbr.CG:
        item = bad["coins"][rank - 1]["item"]
        (item if part == "item" else item["data"])[field] = "abc"
    with count_bad_rows() as c:
        await tbr._cg_rows(tmp_path, "bad", bad)
    assert c.n == 6  # price_btc falls back to data.price_btc: not a stored numeric column


async def test_hl_oi_cap_counts_nulled_fields(tmp_path, monkeypatch):
    bad = copy.deepcopy(hoc.SAMPLE)
    _, ctxs = bad["meta_and_asset_ctxs"]
    for i, (field, _cols) in tbr.HL.items():
        ctxs[i][field] = "Infinity"
    with count_bad_rows() as c:
        await tbr._hl_rows(tmp_path, monkeypatch, "bad", bad)
    assert c.n == 5  # funding, mark + the oi_usd built from it, oracle, vol24h


async def test_coinalyze_history_counts_malformed_and_damaged_bars(tmp_path):
    with count_bad_rows() as c:
        n, _ = await tbr._cz_rows(tmp_path, "bad", "value", "abc")
    assert n == 21 and c.n == 3  # one bad BTC bar in each of oi_usd, funding, long_short
    oi = [dict(h, c=0.0) if i == 1 else h for i, h in enumerate(czh.OI_USD)]
    rt, _ = czh.make_rt(tmp_path, czh.sample_handler(oi=oi), collect_coinalyze_history_backfill_days=10)
    try:
        with count_bad_rows() as c:
            assert await CoinalyzeHistoryJob()(rt) == 22
    finally:
        await rt.aclose()
    assert c.n == 2  # one damaged OI bar (close below MIN_OI_USD) per symbol


async def test_radar_archive_counts_dropped_prints(tmp_path):
    with count_bad_rows() as c:
        await tbr._radar_rows(tbr._radar_rt(tmp_path, "bad", "abc", {3: "index_price"}))
    assert c.n == 14  # one bad print in each of 7 days x BTC, ETH
