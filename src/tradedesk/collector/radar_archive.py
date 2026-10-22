"""Radar archive job: one row per day and asset (BTC, ETH) with the Deribit surface at 19:00 UTC.

Docs: docs/ideas/IDEA-20261018-assistant.md («Радар», Progress item «щоденний архів поверхні»). The surface is built from
the option trade prints of the 6 hours before 19:00 UTC on history.deribit.com (`analytics/radar.py`), the same code as
the one-year backfill `scripts/radar_backfill.py`, so «a week ago» and the skew history come from one method. Read-only
market data into its own table `radar_surface`; independent of `journal`. Off by default:
TRADEDESK_COLLECT_RADAR_ENABLED=1.

Idempotent: a (day, asset) already stored is never fetched again (PRIMARY KEY). A day is written only from 20:00 UTC
(`due_days` lag of 60 min) so history.deribit.com has ingested the prints before 19:00 when the row is frozen. Each run
also fills the missing days of the last week, so a collector outage heals itself and «a week ago» exists a week after the
job is switched on.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..analytics.radar import build_surface, due_days, radar_row, window_ms
from ..core.errors import ProviderError

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

ASSETS = ("BTC", "ETH")
LOOKBACK_DAYS = 7
KEEP = ("trade_id", "instrument_name", "timestamp", "iv", "price", "mark_price", "index_price", "direction")


async def fetch_trades(rt: "Runtime", currency: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """All option trades of ``currency`` in [start_ms, end_ms] from history.deribit.com (1000 per page, timestamp paging).

    Raises ProviderError when a page fails."""
    cur = start_ms
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    while True:
        d = (await rt.deribit_history.last_trades_by_currency_and_time(currency, "option", cur, end_ms, count=1000)).data
        for t in d["trades"]:
            if t.get("trade_id") not in seen:
                seen.add(t.get("trade_id"))
                out.append({k: t.get(k) for k in KEEP})
        if not d["has_more"] or not d["trades"]:
            break
        nxt = int(d["trades"][-1]["timestamp"])
        cur = nxt if nxt > cur else cur + 1  # a full page inside one millisecond: step over it
    return out


async def archive_day(rt: "Runtime", asset: str, day_ms: int) -> dict[str, Any] | None:
    """The radar row of one UTC day (None when the window has no usable surface). Raises ProviderError on a fetch failure."""
    start, end = window_ms(day_ms)
    trades = await fetch_trades(rt, asset, start, end)
    return radar_row(build_surface(trades, day_ms, asset))


async def collect_radar_archive(rt: "Runtime") -> int:
    """Collector job. Returns the rows written. Raises (the scheduler records it) when a fetch failed; days without a
    usable surface only log (they are retried on the next runs while inside the one-week lookback)."""
    if rt.store is None:
        raise RuntimeError("radar archive: needs a SQLite store (set TRADEDESK_DB_PATH)")
    await rt.store.init()
    days = due_days(rt.now(), LOOKBACK_DAYS)
    if not days:
        return 0
    n = 0
    errors: list[str] = []
    for asset in ASSETS:
        have = await rt.store.radar_days(asset, since=_iso(days[-1]))
        for d in days:
            if _iso(d) in have:
                continue
            try:
                row = await archive_day(rt, asset, d)
            except ProviderError as e:
                errors.append(f"{asset} {_iso(d)}: {e}")
                continue
            if row is None:
                log.info("radar archive: no %s surface for %s", asset, _iso(d))
                continue
            row["asset"] = asset
            row["created_ts"] = int(rt.now().timestamp())
            n += await rt.store.radar_insert(row)
    if errors:
        raise RuntimeError(f"radar archive: wrote {n}, fetch failed, will retry: " + "; ".join(errors))
    return n


def _iso(day_ms: int) -> str:
    return datetime.fromtimestamp(day_ms / 1000, UTC).strftime("%Y-%m-%d")
