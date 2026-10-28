"""Hyperliquid open-interest-cap state: one snapshot per UTC hour of every listed main-dex perp.

Docs: docs/ideas/IDEA-20261028-hl-oi-cap-state-forward.md, step 0 (forward-only collection; nothing here trades). Two
public ``POST /info`` requests through the existing provider (`rt.hyperliquid`, `core.http.Http`: rate limit, retries,
health) in one ``asyncio.gather``: ``perpsAtOpenInterestCap`` (the capped tickers) and ``metaAndAssetCtxs`` (OI, funding,
mark, oracle, 24 h notional volume). Rows are built by `analytics/hl_oi_cap.snapshot_rows` into `hl_oi_cap_snapshot`:
every listed perp, delisted ones dropped (live 2026-10-28 all 7 capped tickers were delisted perps with OI 0).

The key is the UTC hour (``ts`` = its start); ``fetched_ts`` is the real fetch time. The job ticks more often than hourly
(default 10 min) and does nothing once the hour is stored, so an MCP restart or a failed attempt does not leave a hole.
If either request fails, nothing is written for that attempt (a capped state without its context, or the context
without the state, is useless to the card): the failure is logged with the ``missing`` reasons and the job returns 0;
it never raises on a provider failure. Off by default: TRADEDESK_COLLECT_HL_OI_CAP_ENABLED=1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..analytics.hl_oi_cap import hour_ts, snapshot_rows
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

PROVIDER = "hyperliquid"


async def collect_hl_oi_cap(rt: "Runtime") -> int:
    """Collector job. Returns the rows written (0: the hour is already stored, or a request failed and was logged)."""
    if rt.store is None:
        log.warning("hl_oi_cap: no SQLite store (set TRADEDESK_DB_PATH), nothing recorded")
        return 0
    await rt.store.init()
    now = rt.now()  # one clock read: the hour key and the fetch time
    ts = hour_ts(now)
    if await rt.store.hl_oi_cap_has_hour(ts):
        return 0
    b = Builder()
    capped, contexts = await asyncio.gather(
        b.guard("hl_perps_at_oi_cap", PROVIDER, rt.hyperliquid.perps_at_oi_cap()),
        b.guard("hl_asset_contexts", PROVIDER, rt.hyperliquid.asset_contexts(include_delisted=True)),
    )
    if capped is None or not contexts:
        if contexts is not None and not contexts:
            b.miss("hl_asset_contexts", PROVIDER, "data_unavailable", "metaAndAssetCtxs lists no perp")
        why = "; ".join(f"{m.field}: {m.reason} {m.detail}".strip() for m in b.missing[:4])
        log.warning("hl_oi_cap: hour %d not recorded (retried on the next tick): %s", ts, why)
        return 0
    rows = snapshot_rows(capped, contexts)
    written = await rt.store.insert_hl_oi_cap_snapshot(ts, int(now.timestamp()), rows)
    at_cap = [r["coin"] for r in rows if r["at_cap"]]
    log.info(
        "hl_oi_cap: hour %d, %d perps, %d at cap (%s), %d capped delisted skipped",
        ts, len(rows), len(at_cap), ",".join(at_cap)[:200], len(set(capped) - set(at_cap)),
    )  # fmt: skip
    return written
