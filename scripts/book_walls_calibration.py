"""Order-book walls: descriptive calibration on the first day of data (IDEA-20261114-book-walls, item 1).

    uv run python scripts/book_walls_calibration.py [--db PATH] [--start ISO] [--end ISO] [--split ISO]
        [--old-min BTC=25,ETH=750] [--old-k 50] [--cpu-seconds N] [--out PATH]
    # the post-deploy check: --start <restart time>; the first day: --old-min BTC=25,ETH=750

Reads ``book_wall_event``, ``book_depth_minute`` and ``book_stream_hour`` through a READ-ONLY connection (``mode=ro``:
no migration, no write, no network) over the window [--start, --end) (default: the first depth row and 24 h after it;
walls by their close time). Per venue x symbol: walls per day by reason, life, size / bar, distances, traded share,
flicker (the same level reopening within 60 s), short ``withdrawn`` walls dropped, walls open at once, how often the
visible book is narrower than the +-2 % zone; stream failures per hour split at ``--split`` (default: the server pull of
the IPv4 WS change, 2026-11-25 06:20:03 UTC, from the server clone's reflog); rows and bytes on disk per day
(``dbstat``); and estimates under candidate higher bars (``--old-k`` and the coin minimum the rows were collected with:
by default the current one of each venue, ``analytics/book_walls.min_qty_for``; ``--old-min BTC=25,ETH=750`` sets one
for every venue, which a window before the restart with the 2026-11-25 calibration needs, because Binance spot ETH was
on 750 then; analytics/book_walls_calibration.py says what is exact and what is an estimate). ``--cpu-seconds N`` also
samples the CPU of the running ``tradedesk-mcp`` process from /proc for N seconds (the whole service, not only this job).

Blind for item 2 of the card: no mid after a wall, no price change, no outcome is read or printed.
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics import book_walls as bw
from tradedesk.analytics import book_walls_calibration as cal
from tradedesk.collector.book_walls import VENUE_ID

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
# The server clone pulled the merge of the IPv4 WS change (PR #1628, docs/changes/2026-11-25-ws-stream-stalls.md) at
# this time (reflog of ~/tradedesk-mcp, 1795587603) and tradedesk-update restarted the service after it.
DEFAULT_SPLIT = "2026-11-25T06:20:03+00:00"
# The bar the first-day rows (2026-11-24 11:21 .. the restart with the calibration of 2026-11-25) were collected with,
# on every venue: pass it as --old-min for that window.
FIRST_DAY_MIN = "BTC=25,ETH=750"
OLD_K = 50.0
JOB_OF = {table: job for job, table in VENUE_ID.items()}  # venue as stored in the tables -> collector job name
# Candidate coin minimums per base (all >= the old ones: only a higher bar is derivable) and K values.
CANDIDATE_MIN = {
    "BTC": (25.0, 50.0, 75.0, 100.0, 125.0, 150.0, 200.0),
    "ETH": (750.0, 1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 3500.0, 4000.0, 5000.0),
}
CANDIDATE_K = (50.0, 100.0, 200.0)
TABLES = ("book_wall_event", "book_wall_event_close", "book_depth_minute", "book_stream_hour")


def base_of(symbol: str) -> str:
    return symbol.upper().removesuffix("USDT").removesuffix("-USD")


def floor_of(key: tuple[str, str], old_min: dict[str, float] | None) -> float | None:
    """The coin minimum the rows of (table venue, symbol) were collected with: ``old_min`` per base when given, else
    the current code's for that venue (None for a base without one)."""
    if old_min:
        return old_min.get(base_of(key[1]))
    return bw.min_qty_for(key[1], JOB_OF.get(key[0]))


def parse_ts(s: str) -> int:
    when = datetime.fromisoformat(s)
    return int((when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp())


def _t(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur]


def disk(con: sqlite3.Connection) -> tuple[dict[str, int], dict[str, int], float]:
    """(bytes per table / index from dbstat, rows per table, hours the tables span); empty without dbstat."""
    pages: dict[str, int] = {}
    try:
        for name in TABLES:
            (b,) = con.execute("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = ?", (name,)).fetchone()
            pages[name] = int(b)
    except sqlite3.OperationalError:  # SQLite built without dbstat
        pages = {}
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES if t != "book_wall_event_close"}  # noqa: S608
    lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM book_depth_minute").fetchone()
    return pages, counts, ((hi - lo + 60) / 3600 if lo is not None else math.nan)


def cpu_sample(seconds: float) -> list[tuple[int, float, float, float]]:
    """(pid, % of one core over the sample, % of one core since the process started, its start as unix s) of each
    running tradedesk-mcp process, read from /proc (utime + stime, starttime). The start is the service's last restart,
    i.e. the boundary of a new wall regime after a deploy."""
    tick = os.sysconf("SC_CLK_TCK")

    def procs() -> dict[int, tuple[float, float]]:
        out = {}
        with open("/proc/uptime", encoding="ascii") as f:
            uptime = float(f.read().split()[0])
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                cmd = Path(f"/proc/{d}/cmdline").read_bytes().split(b"\0")
                if not any(c.endswith(b"bin/tradedesk-mcp") for c in cmd[:2]):
                    continue
                f = Path(f"/proc/{d}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
                cpu = (int(f[11]) + int(f[12])) / tick  # utime, stime (fields 14, 15)
                age = uptime - int(f[19]) / tick  # starttime (field 22)
                out[int(d)] = (cpu, age)
            except (OSError, IndexError, ValueError):
                continue
        return out

    a = procs()
    t0 = time.monotonic()
    time.sleep(seconds)
    b = procs()
    dt = time.monotonic() - t0
    now = time.time()
    return [
        (pid, 100 * (b[pid][0] - a[pid][0]) / dt, 100 * b[pid][0] / max(b[pid][1], 1e-9), now - b[pid][1]) for pid in sorted(a) if pid in b
    ]


def fmt_d(d: dict[str, float], mult: float = 1.0, digits: int = 1) -> str:
    if not d["n"]:
        return "-"
    return " / ".join(f"{d[k] * mult:.{digits}f}" for k in ("p10", "p50", "p90", "max"))


def build(
    con: sqlite3.Connection,
    start: int | None,
    end: int | None,
    split: int | None,
    old_min: dict[str, float] | None,
    old_k: float,
) -> tuple[int, str]:
    lo_all, hi_all = con.execute("SELECT MIN(ts), MAX(ts) FROM book_depth_minute").fetchone()
    if lo_all is None:
        return 1, "no book_depth_minute rows\n"
    lo = start if start is not None else int(lo_all)
    hi = end if end is not None else lo + cal.DAY_S
    covered_hi = min(hi, int(hi_all) + 60)
    hours = (covered_hi - lo) / 3600
    if hours <= 0:
        return 1, f"no depth rows in {_t(lo)} .. {_t(hi)}\n"
    events = rows(con, "SELECT * FROM book_wall_event WHERE close_ms >= ? AND close_ms < ?", (lo * 1000, hi * 1000))
    depth = rows(con, "SELECT * FROM book_depth_minute WHERE ts >= ? AND ts < ?", (lo, hi))
    h_lo = -(-lo // cal.HOUR_S) * cal.HOUR_S  # full hours only: the first and the running one are partial
    streams = rows(con, "SELECT * FROM book_stream_hour WHERE hour_ts >= ? AND hour_ts + 3600 <= ?", (h_lo, covered_hi))
    keys = sorted({cal.key_of(r) for r in depth} | {cal.key_of(e) for e in events})
    floor = {k: m for k in keys if (m := floor_of(k, old_min)) is not None}
    ev = cal.event_table(events, hours, floor, bw.NEAR_DIST)
    fl = cal.flicker(events)
    dp = cal.depth_table(depth, hours, bw.BAND, bw.VIEW_MARGIN)
    st = cal.stream_table(streams, split)
    pages, counts, span_h = disk(con)

    o: list[str] = ["# Book walls: calibration on the first day (descriptive, blind to outcomes)"]
    o.append(
        f"window {_t(lo)} .. {_t(hi)} UTC; data up to {_t(covered_hi)} UTC = {hours:.2f} h covered; per-day figures "
        f"scaled to 24 h. Bar of these rows: K {old_k:g}, minimum "
        f"{', '.join(f'{v} {s} {m:g}' for (v, s), m in floor.items())} ({'--old-min' if old_min else 'current code'}); "
        f"NEAR_DIST {bw.NEAR_DIST * 1e4:g} bp, "
        f"MIN_LIFE {bw.MIN_LIFE_MS / 1000:g} s, zone +-{bw.BAND:.0%} x view margin {bw.VIEW_MARGIN:g}."
    )
    o.append("Distributions: p10 / p50 / p90 / max. Distances in bp of the mid.\n")

    o.append("## Stored walls per day by reason")
    o.append("| venue | symbol | stored/day | consumed | pulled_near | withdrawn | gap | out_of_view | short withdrawn dropped/day |")
    o.append("|---|---|---|---|---|---|---|---|---|")
    for k in keys:
        e, d = ev.get(k), dp.get(k)
        rp = e["reasons_per_day"] if e else {}
        o.append(
            f"| {k[0]} | {k[1]} | {e['per_day'] if e else 0:.0f} | "
            + " | ".join(f"{rp.get(r, 0):.0f}" for r in bw.REASONS)
            + f" | {d['short_per_day'] if d else 0:.0f} |"
        )

    o.append("\n## Wall properties")
    o.append(
        "| venue | symbol | life s | size_max / bar | bar = coin minimum | dist_open bp | dist_min bp | dist_close bp | "
        "traded / size_max | any traded | with feed break | pulled_near born within NEAR_DIST |"
    )
    o.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k in keys:
        e = ev.get(k)
        if not e:
            continue
        o.append(
            f"| {k[0]} | {k[1]} | {fmt_d(e['life_s'], digits=0)} | {fmt_d(e['ratio'], digits=2)} | {e['at_min_share']:.1%} | "
            f"{fmt_d(e['dist_open'], 1e4)} | {fmt_d(e['dist_min'], 1e4)} | {fmt_d(e['dist_close'], 1e4)} | "
            f"{fmt_d(e['traded_share'], digits=2)} | {e['traded_any']:.0%} | {e['gaps_share']:.0%} | "
            f"{e['near_born_near']} of {e['reasons'].get('pulled_near', 0)} |"
        )
    o.append("\nLife by reason (p10 / p50 / p90 / max, s):")
    for k in keys:
        e = ev.get(k)
        if e:
            o.append(f"- {k[0]} {k[1]}: " + "; ".join(f"{r} {fmt_d(d, digits=0)}" for r, d in sorted(e["life_s_by_reason"].items())))

    o.append(f"\n## Flicker (same side and price reopening within {cal.REOPEN_MS // 1000} s of the previous close)")
    o.append("| venue | symbol | walls | distinct levels | walls per level p50 / p90 / max | reopened | share |")
    o.append("|---|---|---|---|---|---|---|")
    for k in keys:
        f = fl.get(k)
        if f:
            pl = f["per_level"]
            o.append(
                f"| {k[0]} | {k[1]} | {f['walls']} | {f['levels']} | {pl['p50']:.0f} / {pl['p90']:.0f} / {pl['max']:.0f} | "
                f"{f['reopen']} | {f['reopen_share']:.0%} |"
            )

    o.append("\n## Walls open at once and the visible book (minute rows)")
    o.append(
        "| venue | symbol | minutes | open mean | open p50 / p90 / max | minutes with a wall | book breaks | bid view bp p10 / p50 | "
        "ask view bp p10 / p50 | view x margin < zone (bid / ask) |"
    )
    o.append("|---|---|---|---|---|---|---|---|---|---|")
    for k in keys:
        d = dp.get(k)
        if d:
            w = d["open"]
            o.append(
                f"| {k[0]} | {k[1]} | {d['minutes']} | {d['open_mean']:.2f} | {w['p50']:.0f} / {w['p90']:.0f} / {w['max']:.0f} | "
                f"{d['any_share']:.0%} | {d['gaps']} | {d['bid_view']['p10'] * 1e4:.0f} / {d['bid_view']['p50'] * 1e4:.0f} | "
                f"{d['ask_view']['p10'] * 1e4:.0f} / {d['ask_view']['p50'] * 1e4:.0f} | {d['narrow_bid']:.0%} / {d['narrow_ask']:.0%} |"
            )

    o.append("\n## Candidate higher bars (estimates; `sure` = above the high bound of the new bar, `maybe` = above the low one)")
    o.append(
        "`open (spans)`: mean walls open at the minute rows' times from the stored walls' own open / close spans; it "
        "misses walls still open at the end of the data or lost at a restart (and short ones), so at the current bar it "
        "is below the minute rows' count. `open (corrected)` = spans x (minute rows / spans at the current bar)."
    )
    o.append("| venue | symbol | minimum | K | sure/day | maybe/day | sure by reason/day | open (spans) | open (corrected) |")
    o.append("|---|---|---|---|---|---|---|---|---|")
    for k in keys:
        if k not in floor:
            continue
        f0 = floor[k]
        evs = [e for e in events if cal.key_of(e) == k]
        samples = [int(r["ts"]) for r in depth if cal.key_of(r) == k]
        grid = [(m, old_k) for m in sorted({f0, *(m for m in CANDIDATE_MIN.get(base_of(k[1]), ()) if m >= f0)})]
        grid += [(f0, kk) for kk in CANDIDATE_K if kk > old_k]
        base_spans = cal.open_at(evs, samples)
        rows_open = dp[k]["open_mean"] if k in dp else math.nan
        corr = rows_open / base_spans if base_spans > 0 else math.nan
        for m, kk in grid:
            c = cal.candidate(evs, samples, hours, min_old=f0, min_new=m, k_old=old_k, k_new=kk)
            rs = ", ".join(f"{r} {n:.0f}" for r, n in c["reasons_per_day"].items())
            o.append(
                f"| {k[0]} | {k[1]} | {m:g} | {kk:g} | {c['sure_per_day']:.0f} | {c['maybe_per_day']:.0f} | {rs or '-'} | "
                f"{c['open_mean']:.2f} | {c['open_mean'] * corr:.2f} |"
            )

    o.append("\n## Stream failures (book_stream_hour)")
    sp = f"split at {_t(split)} UTC; the hour containing it is `mixed`" if split is not None else "no split"
    o.append(f"{sp}. Descriptive only.")
    o.append("| stream | part | hours | failures | reconnects | failures/hour | connected |")
    o.append("|---|---|---|---|---|---|---|")
    for name, parts in st.items():
        for p in ("before", "mixed", "after", "all"):
            if p in parts:
                d = parts[p]
                o.append(
                    f"| {name} | {p} | {d['hours']} | {d['failures']} | {d['reconnects']} | {d['failures_per_hour']:.2f} | "
                    f"{d['connected_share']:.1%} |"
                )

    o.append("\n## Rows and bytes on disk (whole tables)")
    if pages:
        tb = cal.table_bytes(pages, counts, span_h)
        o.append(f"tables span {span_h:.1f} h (first to last depth row); per day = scaled to 24 h")
        o.append("| table / index | rows | rows/day | MB | MB/day |")
        o.append("|---|---|---|---|---|")
        for name, d in tb.items():
            n = f"{d['rows']:.0f} | {d['rows_per_day']:.0f}" if name in counts else "(index) | -"
            o.append(f"| {name} | {n} | {d['bytes'] / 1e6:.1f} | {d['bytes_per_day'] / 1e6:.1f} |")
        tot = sum(d["bytes_per_day"] for d in tb.values())
        o.append(f"total {tot / 1e6:.1f} MB/day")
    else:
        o.append(f"dbstat not available; rows: {counts}")
    return 0, "\n".join(o) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--start", default=None, help="ISO time (no offset = UTC); default: the first depth row")
    ap.add_argument("--end", default=None, help="ISO time; default: --start + 24 h")
    ap.add_argument("--split", default=DEFAULT_SPLIT, help="ISO time to split stream failures at ('' = no split)")
    ap.add_argument(
        "--old-min",
        default="",
        help=f"coin minimum the rows were collected with on every venue, BASE=coins,... (default: the current one per "
        f"venue; the first day before the 2026-11-25 restart: {FIRST_DAY_MIN})",
    )
    ap.add_argument("--old-k", type=float, default=OLD_K, help="K the rows were collected with")
    ap.add_argument("--cpu-seconds", type=float, default=0.0, help="also sample the service CPU from /proc for N s")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    old_min = {p.split("=")[0].strip().upper(): float(p.split("=")[1]) for p in a.old_min.split(",") if "=" in p} or None
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    try:
        code, text = build(
            con,
            parse_ts(a.start) if a.start else None,
            parse_ts(a.end) if a.end else None,
            parse_ts(a.split) if a.split else None,
            old_min,
            a.old_k,
        )
    finally:
        con.close()
    if a.cpu_seconds > 0:
        lines = [f"\n## CPU of the running service (/proc, {a.cpu_seconds:g} s sample; the whole process, all jobs)"]
        sample = cpu_sample(a.cpu_seconds)
        lines += [
            f"- pid {p}: {now:.1f} % of one core over the sample, {life:.1f} % since its start ({_t(start)} UTC)"
            for p, now, life, start in sample
        ]
        text += "\n".join(lines if sample else lines + ["- no tradedesk-mcp process found"]) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
