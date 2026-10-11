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


async def _collect_funding(rt: "Runtime") -> int:
    """Collect current cross-venue funding snapshots for BTC/ETH into `funding_hist`."""
    from ..analytics.derivatives import annualize_funding, parse_interval_hours  # noqa: PLC0415
    from ..providers.exchanges import PERP_EXCHANGES  # noqa: PLC0415

    assert rt.store is not None
    ts = int(rt.now().timestamp())
    total = 0
    for base in ("BTC", "ETH"):
        symbol = f"{base}/USDT:USDT"
        rows: list[dict[str, object]] = []
        for ex in PERP_EXCHANGES:
            try:
                fr = (await rt.exchanges.funding_rate(ex, symbol)).data
            except Exception:  # noqa: BLE001 - a single venue failing is fine
                continue
            rate = fr.get("fundingRate")
            if rate is None:
                continue
            hours = parse_interval_hours(fr.get("interval"))
            rows.append(
                {
                    "venue": ex,
                    "base": base,
                    "rate_per_interval": float(rate),
                    "interval_hours": hours,
                    "annualized": annualize_funding(float(rate), hours),
                }
            )
        try:
            ctx = (await rt.hyperliquid.asset_contexts()).data or []
            row = next((c for c in ctx if c["coin"] == base), None)
            if row and row.get("funding_1h") is not None:
                rows.append(
                    {
                        "venue": "hyperliquid",
                        "base": base,
                        "rate_per_interval": float(row["funding_1h"]),
                        "interval_hours": 1.0,
                        "annualized": float(row.get("funding_annualized") or annualize_funding(float(row["funding_1h"]), 1.0)),
                    }
                )
        except Exception:  # noqa: BLE001
            pass
        if base in ("BTC", "ETH"):
            try:
                fut = (await rt.deribit.book_summary(base, "future")).data or []
                perp = next((x for x in fut if x.get("instrument_name", "").endswith("PERPETUAL")), None)
                if perp and perp.get("funding_8h") is not None:
                    rate = float(perp["funding_8h"])
                    rows.append(
                        {
                            "venue": "deribit",
                            "base": base,
                            "rate_per_interval": rate,
                            "interval_hours": 8.0,
                            "annualized": annualize_funding(rate, 8.0),
                        }
                    )
            except Exception:  # noqa: BLE001
                pass
        total += await rt.store.insert_funding(ts=ts, rows=rows)
    return total


async def _collect_basis(rt: "Runtime") -> int:
    """Collect Deribit dated-futures basis snapshot (per currency × expiry) into `basis_hist`."""
    from ..analytics.derivatives import basis as basis_calc  # noqa: PLC0415
    from ..analytics.instruments import parse_future_name  # noqa: PLC0415

    assert rt.store is not None
    now = rt.now()
    ts = int(now.timestamp())
    total = 0
    for currency in ("BTC", "ETH"):
        try:
            fut = (await rt.deribit.book_summary(currency, "future")).data or []
        except Exception:  # noqa: BLE001
            continue
        rows: list[dict[str, object]] = []
        for f in fut:
            parsed = parse_future_name(f.get("instrument_name", ""))
            if parsed is None or parsed[1] is None:
                continue  # skip perpetual
            mark, idx = f.get("mark_price"), f.get("estimated_delivery_price")
            if not mark or not idx:
                continue
            bs = basis_calc(float(mark), float(idx), parsed[1], now)
            rows.append(
                {
                    "expiry": parsed[1].isoformat(),
                    "days": bs["days_to_expiry"],
                    "basis_pct": bs["basis_pct"],
                    "annualized_simple": bs.get("annualized_simple"),
                }
            )
        total += await rt.store.insert_basis(ts=ts, currency=currency, rows=rows)
    return total


async def _retention_sweep(rt: "Runtime") -> int:
    """Trim stored history to the retention windows configured in Settings."""
    assert rt.store is not None
    s = rt.settings
    now_ts = int(rt.now().timestamp())
    deleted = 0
    pairs = (
        ("atm_term", s.retention_atm_term_days),
        ("funding_hist", s.retention_funding_days),
        ("basis_hist", s.retention_basis_days),
    )
    for table, days in pairs:
        if days <= 0:
            continue
        deleted += await rt.store.prune(table, now_ts - days * 86_400)
    return deleted


def build_default_scheduler(rt: "Runtime") -> Scheduler | None:
    """Return a scheduler with the default jobs, or None when disabled or no store."""
    s = rt.settings
    if not s.collector_enabled or rt.store is None:
        return None
    jobs = [
        PeriodicJob(name="atm_term", period_seconds=s.collect_atm_term_seconds, fn=_collect_atm_term),
        PeriodicJob(name="funding", period_seconds=s.collect_funding_seconds, fn=_collect_funding),
        PeriodicJob(name="basis", period_seconds=s.collect_basis_seconds, fn=_collect_basis),
        PeriodicJob(name="retention", period_seconds=s.retention_seconds, fn=_retention_sweep),
    ]
    return Scheduler(rt, jobs)
