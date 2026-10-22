"""Forecast-journal job: settle matured forecasts, and once a day (after 09:00 UTC) record today's range forecasts.

Docs: docs/ideas/IDEA-20261018-assistant.md ("Журнал прогнозів"). Local bookkeeping only. The rows are risk-neutral
probabilities from the Deribit smile (`analytics/forecast_log.py`), a hypothesis under test. The same code runs in the
tool `forecast_log_status` (`tools.forecast_log`). Off by default: TRADEDESK_PAPER_FORECAST_LOG_ENABLED=1.

Idempotent: one forecast per (UTC day, asset, kind), enforced by a UNIQUE key, so a retry after a failed run (or two
processes running the job) never duplicates a day. Issue time is the first successful run after 09:00 UTC, i.e. after the
08:00 delivery, so on Fridays the 7-day target lands on the next weekly expiry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

DECISION_HOUR_UTC = 9


async def collect_forecast_log(rt: "Runtime") -> int:
    """Collector job. Returns the rows settled plus the rows written. Raises (the scheduler records it) when recording failed.

    Settlement gaps (delivery price not yet published, feed down) only log: the rows stay open for the next period."""
    if rt.store is None:
        raise RuntimeError("forecast log: needs a SQLite store (set TRADEDESK_DB_PATH)")
    # lazy: importing the tool module registers nothing, it only gives the shared code
    from ..tools.forecast_log import record_today, settle_due  # noqa: PLC0415

    await rt.store.init()
    sb = Builder()
    n = await settle_due(rt, sb)
    if sb.missing:
        log.info("forecast log: settlement pending: %s", "; ".join(f"{m.field}: {m.detail}" for m in sb.missing))
    if rt.now().hour < DECISION_HOUR_UTC:
        return n
    rb = Builder()
    n += await record_today(rt, rb)
    if rb.missing:
        raise RuntimeError("forecast log: no data, will retry: " + "; ".join(f"{m.field}: {m.detail}" for m in rb.missing))
    return n
