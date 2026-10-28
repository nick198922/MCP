"""Daily cycle regime journal: one row per closed UTC day of BTC's state and components in `cycle_regime_log`.

Docs: docs/ideas/IDEA-20261028-btc-cycle-regime-gauge.md, step 2 (the forward journal the card judges after 12 months).
Read-only public data through the providers only (the cycle_regime tool's own fetch, `tools.cycle.fetch_daily`:
Binance spot / USD-M daily klines, premium-index klines, ccxt funding history); the state and components come from the
pure `analytics.cycle_regime`, the row from `analytics.cycle_regime_journal.log_row`. Nothing here trades.

A day is due from 00:05 UTC after its 00:00 close (LOG_LAG). The job ticks every few minutes; a tick with nothing due
makes no request. Each due tick also fills the missing days of the last week (one fetch for all of them, each day
computed only from data up to its own close), so a restart or an outage heals itself. Idempotent: a stored (date, base)
is never fetched again nor replaced (INSERT OR IGNORE); when a concurrent writer already stored a different state for
the day, the stored row is kept and a warning is logged. A day is not written while a source its rules read (spot,
perp, funding) failed or while its state is undecided: it is retried on the next ticks inside the lookback. A failed
premium index (informational, no rule uses it) is written as NULL and named in `missing`. It never raises on a
provider failure. Off by default: TRADEDESK_COLLECT_CYCLE_REGIME_ENABLED=1.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..analytics import cycle_regime as cr
from ..analytics import cycle_regime_journal as crj
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

BASES = ("BTC",)
RULE_SOURCES = frozenset({"spot_klines", "perp_klines", "funding"})  # a failure here leaves the day for a retry


async def collect_cycle_regime_log(rt: "Runtime") -> int:
    """Collector job. Returns the rows written (0: nothing due, or nothing could be written yet)."""
    if rt.store is None:
        log.warning("cycle_regime_log: no SQLite store (set TRADEDESK_DB_PATH), nothing recorded")
        return 0
    await rt.store.init()
    from ..tools.cycle import fetch_daily  # noqa: PLC0415 - tools import the MCP server stack

    now = rt.now()
    written = 0
    for base in BASES:
        since = datetime.fromtimestamp((now.timestamp() - (crj.LOOKBACK_DAYS + 2) * 86_400), UTC).date().isoformat()
        due = crj.due_days(now, await rt.store.cycle_regime_log_dates(base, since=since))
        if not due:
            continue
        b = Builder()
        end = int(now.timestamp() * 1000) // cr.DAY_MS * cr.DAY_MS
        f = await fetch_daily(rt, b, base, now, (end - due[0]) // cr.DAY_MS + 1)
        failed = {m.field for m in b.missing}
        if failed & RULE_SOURCES or not f["spot"]:
            why = "; ".join(f"{m.field}: {m.reason} {m.detail}"[:160] for m in b.missing) or "no spot klines"
            log.warning("cycle_regime_log %s: %d due day(s) not written, retried on the next tick: %s", base, len(due), why)
            continue
        for close_ms in due:
            res = cr.cycle_regime(f["spot"], f["perp"], f["funding"], f["premium"], datetime.fromtimestamp(close_ms / 1000, UTC))
            row = crj.log_row(res, base, int(rt.now().timestamp()), missing=sorted(failed))
            if row is None:
                log.warning("cycle_regime_log %s %s: no state yet (undecided by %s), retried", base, res["date"], res["undecided_by"])
                continue
            n = await rt.store.cycle_regime_log_insert(row)
            written += n
            if n == 0:
                stored = next(iter(await rt.store.cycle_regime_log_rows(base, since=row["date"])), None)
                if stored is not None and stored["date"] == row["date"] and stored["state"] != row["state"]:
                    log.warning(
                        "cycle_regime_log %s %s: stored state %s kept, this run computed %s (not overwritten)",
                        base, row["date"], stored["state"], row["state"],
                    )  # fmt: skip
        log.info("cycle_regime_log %s: %d row(s) written for %d due day(s)", base, written, len(due))
    return written
