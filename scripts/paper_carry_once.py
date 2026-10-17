"""Run one forward-paper carry cycle against the LIVE venues into a throwaway SQLite file (read-only market data, no orders).

Checks the engine on real data before the collector job is enabled. Usage:
    uv run python scripts/paper_carry_once.py [--db /tmp/paper_carry_check.sqlite] [--strategy A|B|both]
Prints, per strategy, the cycle outcome, the universe, exclusions, the top candidates and the recorded leg sides
(book spread, VWAP cost vs the modelled cost). Expect several minutes: the funding history of the whole universe is fetched.

WARNING (review R4): do NOT run this with an empty cache (a fresh --db) while the collector engine is running. The Binance
request pacer (``SHARED_PACER``, 400 calls / 5 min) is per process, so this script, the MCP server and the collector each
spend their own budget against the same per-IP limit (Binance fundingRate: 500 / 5 min). A cold start here fetches the
whole universe's history and can push the shared IP over the limit (418/429 ban) for the running engine. Run it before
the engine is enabled, or when it is stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import time

from tradedesk.analytics.carry_report import cost_realism
from tradedesk.analytics.carry_rules import cycle_grid
from tradedesk.collector.carry_paper_engine import PARAMS, run_cycle
from tradedesk.collector.carry_paper_feed import LiveFeed
from tradedesk.config import Settings
from tradedesk.runtime import Runtime


async def main(db: str, which: str) -> None:
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=db))
    assert rt.store is not None
    await rt.store.init()
    try:
        now = int(time.time() * 1000)
        cycle = cycle_grid(now)
        feed = LiveFeed(rt)
        for key in ("A", "B") if which == "both" else (which,):
            t0 = time.time()
            out = await run_cycle(rt.store, feed, PARAMS[key], cycle, clock=lambda: int(time.time() * 1000))
            print(f"\n== {key} ({PARAMS[key].tag}) cycle {cycle}: {out} in {time.time() - t0:.0f}s")
            c = await rt.store.cp_cycle_get(key, cycle)
            if not c:
                continue
            pl = c["payload"]
            print(
                f"universe {pl['universe_size']}, with data {pl['with_data']}, excluded {pl['excluded_counts']}, flags {len(pl['flags'])}"
            )
            for cand in pl["candidates"][:10]:
                print("  candidate", {k: v for k, v in cand.items() if k in ("symbol", "trailing_apr", "spread", "rank", "open", "gap")})
            for d in pl["decisions"]:
                print("  decision", d)
            sides = [s["payload"] for s in await rt.store.cp_logs(key, "side")]
            for s in sides:
                print(
                    f"  side {s['leg']:5} {s['side']:4} {s['venue']:12} {s['symbol']}: spread {s.get('spread_bps')} bps, "
                    f"actual {s.get('actual_cost')} vs model {s['model_cost']}, short depth {s.get('depth_short')}"
                )
            print("  cost realism", cost_realism(sides))
    finally:
        await rt.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/tmp/paper_carry_check.sqlite")
    ap.add_argument("--strategy", choices=["A", "B", "both"], default="both")
    a = ap.parse_args()
    asyncio.run(main(a.db, a.strategy))
