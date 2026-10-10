"""Minimal periodic-job scheduler.

Each :class:`PeriodicJob` wraps an async callable. :class:`Scheduler.start`
spawns one task per job; :class:`Scheduler.stop` cancels them cleanly. The
runtime holds the scheduler so :func:`system_health` can read the metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid import cycle
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

JobCallable = Callable[["Runtime"], Awaitable[int]]


@dataclass
class JobMetrics:
    name: str
    period_seconds: int
    runs: int = 0
    successes: int = 0
    failures: int = 0
    last_run_ts: float | None = None
    last_duration_ms: float | None = None
    last_rows_written: int | None = None
    last_error: str | None = None


@dataclass
class PeriodicJob:
    name: str
    period_seconds: int
    fn: JobCallable
    metrics: JobMetrics = field(init=False)

    def __post_init__(self) -> None:
        self.metrics = JobMetrics(name=self.name, period_seconds=self.period_seconds)


class Scheduler:
    def __init__(self, rt: "Runtime", jobs: list[PeriodicJob]) -> None:
        self.rt = rt
        self.jobs = jobs
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def metrics(self) -> list[JobMetrics]:
        return [j.metrics for j in self.jobs]

    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        for j in self.jobs:
            self._tasks.append(asyncio.create_task(self._run_job(j), name=f"collector:{j.name}"))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    async def _run_job(self, job: PeriodicJob) -> None:
        while not self._stop.is_set():
            start = time.perf_counter()
            try:
                written = await job.fn(self.rt)
                job.metrics.successes += 1
                job.metrics.last_rows_written = written
                job.metrics.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                job.metrics.failures += 1
                job.metrics.last_error = f"{type(e).__name__}: {e}"
                log.warning("collector job %s failed: %s", job.name, e)
            finally:
                job.metrics.runs += 1
                job.metrics.last_duration_ms = (time.perf_counter() - start) * 1000.0
                job.metrics.last_run_ts = time.time()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=job.period_seconds)
                return  # stop was set
            except TimeoutError:
                continue


# ---------- default job set ----------


async def _collect_atm_term(rt: "Runtime") -> int:
    """Collect ATM-IV term structure (per currency × expiry) into `atm_term`."""
    from ..analytics.instruments import parse_option_name  # noqa: PLC0415
    from ..analytics.options import atm_iv, build_chain, build_smile, group_by_expiry, rr_fly  # noqa: PLC0415

    assert rt.store is not None  # scheduler is only built when the store exists
    now = rt.now()
    ts = int(now.timestamp())
    total = 0
    for currency in ("BTC", "ETH"):
        raw = await rt.deribit.book_summary(currency, "option")
        chain = build_chain(raw.data, now)
        rows_out: list[dict[str, object]] = []
        for exp, rs in group_by_expiry(chain).items():
            smile = build_smile(rs)
            if smile is None:
                continue
            # parse_option_name gives exp with UTC tz; store ISO form.
            # exp comes from group_by_expiry so it is already a tz-aware datetime.
            _ = parse_option_name  # keep import live for lint; not needed here
            rf25 = rr_fly(smile, 0.25)
            rf10 = rr_fly(smile, 0.10)
            rows_out.append(
                {
                    "expiry": exp.isoformat(),
                    "days": (exp - now).total_seconds() / 86400.0,
                    "forward": smile.forward,
                    "atm_iv": atm_iv(smile),
                    "rr25": rf25.get("rr"),
                    "fly25": rf25.get("fly"),
                    "rr10": rf10.get("rr"),
                    "fly10": rf10.get("fly"),
                }
            )
        total += await rt.store.insert_atm_term(ts=ts, currency=currency, rows=rows_out)
    return total


def build_default_scheduler(rt: "Runtime") -> Scheduler | None:
    """Return a scheduler with the default jobs, or None when disabled or no store."""
    s = rt.settings
    if not s.collector_enabled or rt.store is None:
        return None
    jobs = [PeriodicJob(name="atm_term", period_seconds=s.collect_atm_term_seconds, fn=_collect_atm_term)]
    return Scheduler(rt, jobs)
