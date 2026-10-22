"""Backfill the radar archive (table ``radar_surface``) from history.deribit.com: one row per UTC day and asset.

The same code as the collector job ``radar_archive`` (``tradedesk.collector.radar_archive.archive_day``): option trade
prints of the 6 hours before 19:00 UTC -> ``put_skew.build_day`` -> ``analytics.radar.radar_row`` (spot, ATM IV at
7/30/90/180 days, 30-day 25-delta put skew, RR and smile, ATM by expiry). Read-only market data; network only through
the project's ``Http`` (provider rate limit 5 requests/s). Days already stored are skipped, so the run is resumable.

    uv run python scripts/radar_backfill.py --days 365                      # into TRADEDESK_DB_PATH
    uv run python scripts/radar_backfill.py --out /tmp/radar.sqlite --days 3 --end 2026-10-18

``--end`` is the last day (default: the latest day whose 19:00 UTC has passed by the 60 min lag, i.e. after 20:00 UTC). Writing into the server's database
applies the schema migrations of this checkout (v22 adds ``radar_surface``).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import time
from datetime import UTC, datetime

from tradedesk.analytics.radar import DAY_MS, due_days
from tradedesk.collector.radar_archive import ASSETS, archive_day
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.runtime import Runtime


def plan_days(now: datetime, days: int, end: str | None) -> list[int]:
    """Day starts (epoch ms), oldest first."""
    if end:
        last = int(datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)
        latest = due_days(now, 1)[0]
        last = min(last, latest)
        out = [last - i * DAY_MS for i in range(days)]
    else:
        out = due_days(now, days)
    return sorted(out)


def iso(day_ms: int) -> str:
    return datetime.fromtimestamp(day_ms / 1000, UTC).strftime("%Y-%m-%d")


def pct(x: float | None) -> str:
    return "  —  " if x is None else f"{x * 100:5.1f}"


async def run(out: str, days: int, end: str | None, assets: list[str]) -> int:
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=out))
    assert rt.store is not None
    failed = 0
    try:
        await rt.store.init()
        todo = plan_days(rt.now(), days, end)
        print(f"radar backfill -> {out}: {iso(todo[0])} .. {iso(todo[-1])}, {len(todo)} days x {', '.join(assets)}", flush=True)
        t0 = time.perf_counter()
        for asset in assets:
            have = await rt.store.radar_days(asset, since=iso(todo[0]))
            for i, d in enumerate(todo):
                if iso(d) in have:
                    continue
                try:
                    row = await archive_day(rt, asset, d)
                except ProviderError as e:
                    failed += 1
                    print(f"  {asset} {iso(d)}: fetch failed: {e}", flush=True)
                    continue
                if row is None:
                    print(f"  {asset} {iso(d)}: no usable surface", flush=True)
                    continue
                row["asset"] = asset
                row["created_ts"] = int(time.time())
                await rt.store.radar_insert(row)
                a = row["atm"]
                print(
                    f"  {asset} {row['day']}: spot {row['spot']:>9.1f}  ATM 7/30/90/180 {pct(a['7'])} {pct(a['30'])} {pct(a['90'])} "
                    f"{pct(a['180'])}  put skew {pct(row['put_skew_25'])}  RR {pct(row['rr_25'])}  prints {row['n_prints']}"
                    f"  [{i + 1}/{len(todo)}, {time.perf_counter() - t0:.0f}s]",
                    flush=True,
                )
    finally:
        await rt.aclose()
    return failed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=Settings.from_env().db_path, help="SQLite path (default: TRADEDESK_DB_PATH)")
    ap.add_argument("--days", type=int, default=365, help="number of days ending at --end (default 365)")
    ap.add_argument("--end", default=None, help="last day YYYY-MM-DD (default: the latest day past 20:00 UTC)")
    ap.add_argument("--assets", default=",".join(ASSETS), help="comma-separated, default BTC,ETH")
    a = ap.parse_args()
    if not a.out:
        sys.exit("--out is empty (TRADEDESK_DB_PATH unset)")
    assets = [x.strip().upper() for x in a.assets.split(",") if x.strip()]
    failed = asyncio.run(run(a.out, max(a.days, 1), a.end, assets))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
