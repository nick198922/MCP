"""What-if over the imported journal (read-only): the owner's draft rules replayed on his real closed trades.

    uv run python scripts/journal_rules.py [--db PATH]

Optimistic by construction: a per-trade loss cap assumes the stop would have filled at the cap (no gaps), and a
daily stop assumes the skipped trades would not have been replaced by others."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime


def load(db: str) -> list[tuple[int, int, str, str, float]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT opened_ts, closed_ts, symbol, side, pnl FROM journal WHERE tags LIKE '%\"import\"%' AND tags NOT LIKE '%\"airdrop\"%' AND status='closed' ORDER BY closed_ts"
    ).fetchall()
    con.close()
    return [(int(a or b), int(b), s, d, float(p or 0)) for a, b, s, d, p in rows]


def what_if(rows: list[tuple[int, int, str, str, float]]) -> dict[str, float]:
    base = sum(r[4] for r in rows)
    cap = sum(max(r[4], -100.0) for r in rows)
    # daily stop: after 2 losing trades in a UTC day, or -$200 that day, later trades that day are skipped
    by_day: dict[str, list[float]] = defaultdict(list)
    kept = 0.0
    for r in rows:
        d = datetime.fromtimestamp(r[1], UTC).strftime("%Y-%m-%d")
        day = by_day[d]
        if sum(p < 0 for p in day) >= 2 or sum(day) <= -200:
            continue
        day.append(r[4])
        kept += r[4]
    both = 0.0
    by_day2: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        d = datetime.fromtimestamp(r[1], UTC).strftime("%Y-%m-%d")
        day = by_day2[d]
        if sum(p < 0 for p in day) >= 2 or sum(day) <= -200:
            continue
        p = max(r[4], -100.0)
        day.append(p)
        both += p
    days: dict[str, float] = defaultdict(float)
    for r in rows:
        days[datetime.fromtimestamp(r[1], UTC).strftime("%Y-%m-%d")] += r[4]
    worst_day = min(days.values())
    longs = sum(r[4] for r in rows if r[3] == "long")
    return {
        "n": len(rows),
        "base": base,
        "cap100": cap,
        "daily_stop": kept,
        "both": both,
        "worst_day": worst_day,
        "longs": longs,
        "shorts": base - longs,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default="/var/lib/tradedesk-mcp/tradedesk.sqlite")
    a = ap.parse_args(argv)
    w = what_if(load(a.db))
    print(f"trades {w['n']}; actual {w['base']:+,.0f} USD; worst day {w['worst_day']:+,.0f}")
    print(f"loss capped at -100 per trade: {w['cap100']:+,.0f}")
    print(f"daily stop (2 losses or -200 a day): {w['daily_stop']:+,.0f}")
    print(f"both: {w['both']:+,.0f}")
    print(f"longs {w['longs']:+,.0f}, shorts {w['shorts']:+,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
