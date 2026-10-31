"""Weekly Deribit condor legs, BTC and ETH: one snapshot a day of bid / ask / mark / IV of every leg.

Docs: docs/ideas/IDEA-20261031-structural-edges-depth.md item 8d; why: docs/verification/2026-10-31-options-modules.md
(«Що далі»: the trade-print cache has fresh marks on all four condor legs in only ~60 % of weeks, so H1 / H4 can only be
checked forward). Measurement only, nothing here trades. `option_xvenue` does not cover this: it reads the nearest
DAILY expiry (2-20 h away), ATM +- 3 strikes, so neither the weekly series nor the 10-delta strikes or the +-15 % wings.

Read-only public data through the shared Deribit provider (`rt.deribit`, `core.http.Http`: own rate limit, retries,
health): per currency one ``get_book_summary_by_currency?kind=option`` (the expiry's smile, forward and index pick the
legs, `analytics/option_legs.plan_legs`) and one ``public/ticker`` per distinct leg instrument (<= 10) in one
``asyncio.gather`` for bid / ask with sizes and IVs. A failed ticker leaves that leg on its book-summary quote
(``src = 'summary'``). Expiry: the nearest Friday 08:00 UTC after the snapshot (on Fridays the next week's, i.e. H1's
entry; on the other days the same series as it decays). Saturday to Thursday the instruments of the last Friday entry
(read from the stored Friday rows) are quoted too, under ``hold_<role>`` (<= 20 tickers in all, one per distinct
instrument); no stored Friday entry (the collector was down) skips them with a warning.

Schedule: due from 09:00 UTC (the backtest's reference hour), one snapshot per (UTC day, currency); the job ticks every
`collect_option_legs_seconds` (default 10 min) and a tick with the day stored makes no request, so a restart or a failed
attempt is retried within minutes. ``ts`` is the real snapshot time (a late snapshot after an outage stays visible).
A currency whose book summary failed writes nothing and is retried on the next tick; the run raises (the scheduler
records it) only when nothing was written and a provider reported a problem. Off by default:
TRADEDESK_COLLECT_OPTION_LEGS_ENABLED=1.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..analytics.option_legs import entry_day, expiry_surface, hold_legs, leg_row, plan_legs, snapshot_day, weekly_expiry
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

CURRENCIES = ("BTC", "ETH")


async def scan_option_legs(
    rt: "Runtime", b: Builder, currency: str, now: datetime, entry_rows: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Leg rows of ``currency`` at ``now`` (see ``Store.OPTION_LEG_COLUMNS``). Never raises on a provider failure: it
    lands in ``b.missing`` (book summary down or the expiry absent: no rows; a ticker down: that leg from the summary).
    ``entry_rows`` (Saturday to Thursday: the stored rows of the last Friday entry, possibly empty) adds the held legs
    ``hold_<role>``; none of them matching the expiry is a warning, the re-picked legs are written anyway."""
    summary = await b.guard(f"deribit_options:{currency}", "deribit", rt.deribit.book_summary(currency, "option"))
    if summary is None:
        return []
    expiry = weekly_expiry(now)
    exp = expiry_surface(summary, currency, expiry, now)
    if exp is None:
        why = f"book summary has no {currency} option expiring {expiry.isoformat()} with a forward, index and mark IV"
        b.miss(f"weekly_expiry:{currency}", "deribit", "data_unavailable", why)
        return []
    legs, lost = plan_legs(exp)
    if lost:
        b.warn(f"{currency}: legs not placed (no delta strike on the smile): {', '.join(lost)}")
    if entry_rows is not None:
        held = hold_legs(entry_rows, expiry)
        if not held:
            b.warn(f"{currency}: no Friday entry snapshot for the {expiry:%d%b%y} expiry stored; hold_ legs skipped")
        legs += held
    names = sorted({leg["instrument"] for leg in legs})  # re-picked and held legs share most instruments: one read each
    got = await asyncio.gather(*(b.guard(f"deribit_ticker:{n}", "deribit", rt.deribit.ticker(n)) for n in names))
    tickers = {n: g for n, g in zip(names, got, strict=True) if isinstance(g, dict)}
    out = []
    for leg in legs:
        t = tickers.get(leg["instrument"])
        if t is None and leg["instrument"] not in exp["rows"]:  # a held instrument gone from the book: nothing to store
            b.warn(f"{currency}: {leg['role']} {leg['instrument']} has no ticker and no book summary row; skipped")
            continue
        out.append(leg_row(leg, exp, t, currency=currency, expiry=expiry, now=now))
    return out


async def collect_option_legs(rt: "Runtime") -> int:
    """Collector job. Returns the rows written (0: before 09:00 UTC or the day already stored)."""
    if rt.store is None:
        raise RuntimeError("option_legs: needs a SQLite store (set TRADEDESK_DB_PATH)")
    await rt.store.init()
    now = rt.now()  # one clock read: the day key, the row ts and the base of tau_days
    day = snapshot_day(now)
    if day is None:
        return 0
    b = Builder()
    written = 0
    friday = entry_day(now)
    for cur in CURRENCIES:
        if await rt.store.option_legs_has_day(day, cur):
            continue
        entry = None
        if friday is not None:
            entry = [r for r in await rt.store.option_leg_rows(friday, cur) if r["day"] == friday]
        rows = await scan_option_legs(rt, b, cur, now, entry)
        for r in rows:
            r["ts"] = int(now.timestamp())
        written += await rt.store.insert_option_legs(day, rows)
    for w in b.warnings:  # the day is stored now and not retried: a gap must at least reach the log
        log.warning("option_legs %s: %s", day, w)
    if b.missing:
        why = "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:6])
        if not written:
            raise RuntimeError(f"option_legs {day}: no rows, retried on the next tick; {why}")
        log.warning("option_legs %s: partial snapshot, missing: %s", day, why)
    return written
