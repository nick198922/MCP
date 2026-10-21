"""Trade-plan scoring job: every hour resolve the open plans of the assistant on candles (local bookkeeping, no orders).

Same code as the tool `plan_review` (`tools.trade_plan.review_open`). Behind TRADEDESK_PAPER_PLAN_RESOLVE_ENABLED; read-only
towards the venues (public OHLCV only).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")


async def collect_plan_resolve(rt: "Runtime") -> int:
    """Collector job. Returns the number of plans resolved; raises (the scheduler records it) when a venue failed, after
    writing what the other venues allowed, so the failed plans are retried next period."""
    if rt.store is None:
        raise RuntimeError("plan resolve: needs a SQLite store (set TRADEDESK_DB_PATH)")
    from ..tools.trade_plan import review_open  # noqa: PLC0415 - lazy: importing the tool module registers nothing

    b = Builder()
    await rt.store.init()
    resolved = await review_open(rt, b)
    if resolved:
        log.info("plan resolve: resolved %s", resolved)
    if b.missing:
        raise RuntimeError(f"plan resolve: resolved {len(resolved)}; missing: " + "; ".join(f"{m.field}: {m.detail}" for m in b.missing))
    return len(resolved)
