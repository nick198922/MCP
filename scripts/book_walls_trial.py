"""Order-book walls: live trial of lower coin minimums on one venue (IDEA-20261114-book-walls item 1).

    uv run python scripts/book_walls_trial.py [--venue binance_spot] [--seconds 1200] [--min ETHUSDT=150,250,375,500,750]

A lower bar cannot be derived from stored walls (levels below the bar were never written), so this runs the venue's
stream live: one ``BookWallsJob`` holds the stream and the books, and extra ``WallTracker`` objects with the given
minimums are fed the same book states and trades. No DB, nothing written; network only through the job's own stream
(providers). Prints walls by reason, a crude per-day rate and the mean walls open at once (minute rows) per minimum.
Blind for item 2 of the card: no mid after a wall, no outcome is read or printed.

Used 2026-11-25 09:48-10:08 UTC (1205 s) for Binance spot ETH (docs/changes/2026-11-25-book-walls-calibration.md):
150 -> 417 walls, 250 -> 142, 375 -> 7 (0.24 open at once), 500 and 750 -> 0. Twenty minutes of one morning hour:
the per-day rate is a rough extrapolation.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import time

from tradedesk.analytics.book_walls import WallTracker
from tradedesk.collector.book_walls import VENUE_ID, BookWallsJob
from tradedesk.runtime import Runtime

DEFAULT_MINS = "BTCUSDT=5,10,15,25;ETHUSDT=150,250,375,500,750"


def parse_mins(s: str) -> dict[str, tuple[float, ...]]:
    """'BTCUSDT=5,10;ETHUSDT=150' -> {'BTCUSDT': (5.0, 10.0), 'ETHUSDT': (150.0,)}."""
    out: dict[str, tuple[float, ...]] = {}
    for part in s.split(";"):
        if "=" in part:
            sym, vals = part.split("=", 1)
            out[sym.strip().upper()] = tuple(float(v) for v in vals.split(",") if v.strip())
    return out


async def trial(venue: str, mins: dict[str, tuple[float, ...]], seconds: float) -> None:
    rt = Runtime()
    job = BookWallsJob(venue, list(mins))
    extra = {s: [WallTracker(VENUE_ID[venue], s, m) for m in ms] for s, ms in mins.items()}
    events: dict[tuple[str, float], list] = collections.defaultdict(list)
    open_rows: dict[tuple[str, float], list[int]] = collections.defaultdict(list)
    t0 = time.time()
    stream = job._stream(seconds)
    try:
        async for row in stream:
            await job._handle(rt, row)
            kind, sym = row.get("kind"), row.get("symbol")
            if kind in ("_open", "_drop"):
                for ts in extra.values():
                    for t in ts:
                        t.depth_gap()
                        t.trades_feed(kind == "_open")
                continue
            if sym not in extra:
                continue
            book = job.books[sym]
            for t in extra[sym]:
                if kind == "trade":
                    events[(sym, t.min_qty)] += t.on_trade(row["ts"], row["price"], row["qty"], row["side"])
                elif kind in ("depth", "snapshot", "delta") and book.ready:
                    evs, dr = t.on_book(int(row["ts"]), book)
                    events[(sym, t.min_qty)] += evs
                    if dr is not None:
                        open_rows[(sym, t.min_qty)].append(dr["walls_bid"] + dr["walls_ask"])
    finally:
        await stream.aclose()
        await rt.aclose()
    dt = time.time() - t0
    print(f"trial {venue} {dt:.0f} s; resyncs {job.resyncs}; feeds", {k: (v.failures, v.reconnects) for k, v in job.stats.feeds.items()})
    for sym, ts in extra.items():
        for t in ts:
            k = (sym, t.min_qty)
            evs, rows = events[k], open_rows[k]
            reasons = collections.Counter(e["reason"] for e in evs)
            mean_open = sum(rows) / len(rows) if rows else float("nan")
            print(
                f"{sym} min {t.min_qty:g}: stored {len(evs)} {dict(reasons)}, ~{len(evs) * 86400 / dt:.0f}/day (rough); "
                f"still open at the end {len(t.open)}; minute rows {len(rows)}, mean open {mean_open:.2f}"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--venue", default="binance_spot", choices=sorted(VENUE_ID))
    ap.add_argument("--seconds", type=float, default=1200.0)
    ap.add_argument("--min", default=DEFAULT_MINS, help="SYMBOL=min,min;SYMBOL=... (coins)")
    a = ap.parse_args(argv)
    asyncio.run(trial(a.venue, parse_mins(a.min), a.seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
