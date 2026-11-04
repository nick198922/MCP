"""Why pm_gap v1 positions lack a snapshot near +24 h: counters and causes only (standing item 44, 2026-11-03).

Peeking ban until the v1 verdict (2027-02-23): this script never prints or keeps a CLV, P&L, price of a position or
the settled result of a position's market (v1 or v2-g); settled results are counted only for markets without any
position (:func:`settled_counts` refuses to print if the two sets intersect). It reuses the report's own definitions
(``pm_gap_report._horizon_status``: 'n/a' when the market ends < 26 h after entry, 'pending', 'ok' or 'missing' when no
row of the market with a side mid lies within +-1 h of entry + 24 h; ``_close_status`` for the closing line) and only
reads the status label; the value is discarded on the spot. Output is aggregate counters only: no line names the week,
asset or hours of an individual missing position (the per-week line has positions and 'n/a' only). With fewer than
``SMALL_N`` values an aggregate range prints coarse buckets instead of min-max (:func:`agg_range`, standing item 49):
on n = 3 the exact bounds are two positions' own values.

Usage: uv run python scripts/verify_pm_gap_24h_gaps.py [--db PATH] [--now UNIX ...]
"""

from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)
from pm_gap_paper_report import COLS, DEFAULT_DB

from tradedesk.analytics.pm_gap_paper import (
    HOUR_S,
    MODEL_V2G,
    PRIMARY_H,
    EntryRule,
    accepted_rule_hashes,
    open_positions,
    side_book,
    with_snapshot_g,
)
from tradedesk.analytics.pm_gap_report import T0_TS, T0_V2G_TS, _close_status, _horizon_status, main_period

REPORT_NOW = int(datetime(2026, 11, 1, 18, 10, tzinfo=UTC).timestamp())  # 'Станом на' of pm_gap_REPORT.md
WIDER_H = (1, 2, 3, 6)  # descriptive only: the registered tolerance is 1 h
SMALL_N = 5  # below this an aggregate range prints coarse buckets, not min-max
HOUR_BUCKETS = ((24.0, "<24 h"), (48.0, "24-48 h"), (math.inf, ">=48 h"))
COUNT_BUCKETS = ((1.0, "0"), (3.0, "1-2"), (math.inf, ">=3"))
RANGE_BUCKETS = {"collector moments in +-1 h of +24 h": COUNT_BUCKETS}  # every other range key is in hours


def _bucket(x: float, buckets: tuple[tuple[float, str], ...]) -> str:
    return next(label for upper, label in buckets if x < upper)


def agg_range(key: str, values: list[float]) -> str:
    """``key min-max`` (one decimal) when ``len(values) >= SMALL_N``; otherwise only the coarse buckets of min and max
    ('all >=48 h', '<24 h .. 24-48 h'), so a small n never prints a position's exact value."""
    if len(values) >= SMALL_N:
        return f"{key} {min(values):.1f}-{max(values):.1f}"
    b = RANGE_BUCKETS.get(key, HOUR_BUCKETS)
    lo, hi = _bucket(min(values), b), _bucket(max(values), b)
    return f"{key} (n<{SMALL_N}) " + (f"all {lo}" if lo == hi else f"{lo} .. {hi}")


def load(db: str) -> list[dict[str, Any]]:
    con = cmn.connect_ro(Path(db))
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(pm_gap_snapshot)")}
        sel = [c for c in COLS if c in have]
        rows = [dict(zip(sel, r, strict=True)) for r in con.execute(f"SELECT {', '.join(sel)} FROM pm_gap_snapshot ORDER BY ts, market_id")]
    finally:
        con.close()
    for r in rows:
        for c in COLS:
            r.setdefault(c, None)
    return rows


def cadence(times: list[int]) -> str:
    d = [b - a for a, b in zip(times, times[1:], strict=False)]
    if not d:
        return "cadence: <2 moments"
    q = statistics.quantiles(d, n=20)
    gaps = [(a, b) for a, b in zip(times, times[1:], strict=False) if b - a > 90 * 60]
    out = (
        f"cadence: moments {len(times)}, step median {statistics.median(d) / 60:.1f} min, p5 {q[0] / 60:.1f}, p95 {q[-1] / 60:.1f}, "
        f"max {max(d) / 60:.1f} min; steps > 90 min: {len(gaps)}"
    )
    for a, b in gaps[:10]:
        out += (
            f"\n  gap {datetime.fromtimestamp(a, UTC):%m-%d %H:%M} -> {datetime.fromtimestamp(b, UTC):%m-%d %H:%M} ({(b - a) / 3600:.1f} h)"
        )
    return out


def settled_counts(db: str, market_ids: set[str], position_markets: set[str]) -> Counter | None:
    """Settled-result counts (1 / 0 / none) of markets WITHOUT a position. ``position_markets`` = markets of every v1
    and v2-g position; if it intersects ``market_ids`` nothing is read and None is returned (peeking guard)."""
    if market_ids & position_markets:
        return None
    con = cmn.connect_ro(Path(db))
    try:
        got = {str(m): y for m, y in con.execute("SELECT market_id, outcome_yes FROM pm_gap_outcome")}
    finally:
        con.close()
    return Counter("yes" if got.get(m) == 1 else "no" if got.get(m) == 0 else "none" for m in market_ids)


def left_early_touch(rows: list[dict[str, Any]], now_ts: int, exclude: set[str]) -> set[str]:
    """Ended touch markets without a position (``exclude``) whose last row is > 2 h before their end."""
    last: dict[str, int] = {}
    meta: dict[str, tuple[str, int]] = {}
    for r in rows:
        last[str(r["market_id"])] = max(int(r["ts"]), last.get(str(r["market_id"]), 0))
        meta[str(r["market_id"])] = (str(r["kind"]), int(r["end_ts"]))
    return {
        m for m, t in last.items() if m not in exclude and meta[m][0] == "touch" and meta[m][1] <= now_ts and t < meta[m][1] - 2 * HOUR_S
    }


def market_dropouts(rows: list[dict[str, Any]], times: list[int], now_ts: int, exclude: set[str]) -> list[str]:
    """Collector-level (all markets, no positions): markets that leave the list well before their end, and holes in a
    market's own series between its first and last row. The mechanism line (model_note of a market's last row) skips
    every market with a v1 or v2-g position (``exclude``), so it says nothing about a position."""
    span: dict[str, list[int]] = defaultdict(list)
    meta: dict[str, tuple[str, int]] = {}
    last_note: dict[str, str] = {}
    for r in rows:  # rows are sorted by ts: the last assignment is the market's last row
        span[str(r["market_id"])].append(int(r["ts"]))
        meta[str(r["market_id"])] = (str(r["kind"]), int(r["end_ts"]))
        last_note[str(r["market_id"])] = str(r["model_note"])
    notes: dict[str, Counter] = {"left_early": Counter(), "stayed": Counter()}
    for mid, ts in span.items():
        kind, end = meta[mid]
        if kind != "touch" or mid in exclude or end > now_ts:
            continue
        notes["left_early" if max(ts) < end - 2 * HOUR_S else "stayed"][last_note[mid]] += 1
    left = Counter()
    total = Counter()
    holes = Counter()
    hole_markets = Counter()
    lead_h: dict[str, list[float]] = defaultdict(list)
    for mid, ts in span.items():
        kind, end = meta[mid]
        last, first = max(ts), min(ts)
        total[kind] += 1
        expected = bisect_right(times, last) - bisect_left(times, first)
        h = expected - len(set(ts))
        holes[kind] += h
        hole_markets[kind] += int(h > 0)
        if last < min(end, now_ts) - 2 * HOUR_S:  # gone > 2 h before its end (or before now, if still running)
            left[kind] += 1
            lead_h[kind].append((end - last) / 3600.0)
    out = ["markets by kind: total / left the list > 2 h before end (median h before end) / markets with holes / missing moments"]
    for k in sorted(total):
        med = f"{statistics.median(lead_h[k]):.1f}" if lead_h[k] else "-"
        out.append(f"  {k}: {total[k]} / {left[k]} ({med}) / {hole_markets[k]} / {holes[k]}")
    out.append("ended touch markets without a v1/v2-g position, model_note of the last row:")
    out += [f"  {g}: {dict(sorted(c.items()))}" for g, c in notes.items()]
    return out


def diagnose(rows: list[dict[str, Any]], now_ts: int, t0: int, db: str) -> list[str]:
    start, end = main_period(t0)
    rule = EntryRule()
    acc = accepted_rule_hashes(rows)
    visible = [r for r in rows if int(r["ts"]) <= now_ts]
    period = [r for r in visible if start <= int(r["ts"]) < end]
    by_mkt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in visible:
        by_mkt[str(r["market_id"])].append(r)
    times = sorted({int(r["ts"]) for r in visible})
    positions = open_positions(period, rule, acc)
    # v2-g positions (v1's main period and v2-g's own one): their markets are excluded from the mechanism lines too.
    v2 = EntryRule(model=MODEL_V2G)
    v2_pos = [
        p
        for s0, e0 in (main_period(t0), main_period(T0_V2G_TS))
        for p in open_positions(with_snapshot_g(r for r in visible if s0 <= int(r["ts"]) < e0), v2, acc)
    ]
    pos_mkts = {str(p["market_id"]) for p in positions} | {str(p["market_id"]) for p in v2_pos}

    status = Counter()
    cause = Counter()
    kinds = Counter()
    week: dict[str, Counter] = defaultdict(Counter)
    wider = Counter()
    rng: dict[str, list[float]] = defaultdict(list)  # aggregate ranges only, never tied to a week or an asset
    cl_status = Counter()
    cl_cause = Counter()
    for p in positions:
        mk = by_mkt.get(str(p["market_id"]), [])
        _, s = _horizon_status(p, mk, PRIMARY_H, now_ts)  # value discarded: status label only
        _, sc = _close_status(p, mk, now_ts)  # value discarded: source label only
        status[s] += 1
        wk = str(p["week_entry"])
        week[wk]["positions"] += 1
        week[wk]["n/a"] += int(s == "n/a")
        ts, end_ts = int(p["ts"]), int(p["end_ts"])
        mts = sorted(int(r["ts"]) for r in mk)
        cl_status[sc] += 1
        if sc == "missing":  # no side mid in [end - 6 h, end - 1 h] after entry (closing_value)
            lo_c, hi_c = max(ts + 1, end_ts - 6 * HOUR_S), end_ts - HOUR_S
            if lo_c > hi_c:
                c2 = "no_window_after_entry"
            elif not any(lo_c <= t <= hi_c for t in times):
                c2 = "collector_gap"
            elif not any(lo_c <= t <= hi_c for t in mts):
                c2 = "market_gone_for_good" if mts and max(mts) < lo_c else "market_absent_returned"
            else:
                c2 = "no_mid"
            cl_cause[f"{p['kind']}/{c2}"] += 1
        if s != "missing":
            continue
        t24 = ts + PRIMARY_H * HOUR_S
        lo, hi = t24 - HOUR_S, t24 + HOUR_S
        with_mid = sorted(int(r["ts"]) for r in mk if side_book(r, str(p["side"]))["mid"] is not None)
        coll_in = [t for t in times if lo <= t <= hi]
        if not coll_in:
            c = "collector_gap"
        elif not any(lo <= t <= hi for t in mts):
            c = "market_gone_for_good" if mts and max(mts) < lo else "market_absent_returned"
        else:
            c = "no_mid"
        cause[c] += 1
        kinds[str(p["kind"])] += 1
        nearest = min((abs(t - t24) for t in with_mid), default=None)
        for w in WIDER_H:
            wider[w] += int(nearest is not None and nearest <= w * HOUR_S)
        rng["entry->end h"].append((end_ts - ts) / 3600)
        rng["collector moments in +-1 h of +24 h"].append(len(coll_in))
        if mts:
            rng["last market row, h after entry"].append((max(mts) - ts) / 3600)
            rng["last market row, h before end"].append((end_ts - max(mts)) / 3600)

    defined = status["ok"] + status["missing"]
    closed = cl_status["close"] + cl_status["late"] + cl_status["missing"]
    settled = settled_counts(db, left_early_touch(visible, now_ts, pos_mkts), pos_mkts)
    out = [
        f"now {datetime.fromtimestamp(now_ts, UTC):%Y-%m-%d %H:%M} UTC, T0 {datetime.fromtimestamp(t0, UTC):%Y-%m-%d %H:%M}, "
        f"rows {len(visible)}, moments {len(times)}, positions {len(positions)} (v2-g positions excluded from mechanism: {len(v2_pos)})",
        f"+24 h status: {dict(sorted(status.items()))}; missing share of defined {status['missing']}/{defined}"
        + (f" = {status['missing'] / defined:.1%}" if defined else ""),
        f"+24 h missing causes: {dict(sorted(cause.items()))}; by kind: {dict(sorted(kinds.items()))}",
        "+24 h missing, aggregate ranges (min-max): " + "; ".join(agg_range(k, v) for k, v in rng.items() if v),
        "+24 h missing recoverable with a wider tolerance (descriptive, nearest row with mid): "
        + ", ".join(f"+-{w} h {wider[w]}" for w in WIDER_H),
        "per entry week (positions / n/a only): " + "; ".join(f"{w}: {c['positions']} / {c['n/a']}" for w, c in sorted(week.items())),
        f"closing line source: {dict(sorted(cl_status.items()))}; missing share of closed {cl_status['missing']}/{closed}"
        + (f" = {cl_status['missing'] / closed:.1%}" if closed else ""),
        f"closing missing by kind/cause: {dict(sorted(cl_cause.items()))}",
        cadence(times),
        *market_dropouts(visible, times, now_ts, pos_mkts),
        "ended touch markets without a v1/v2-g position that left > 2 h before end, settled result: "
        + ("REFUSED (sets intersect)" if settled is None else str(dict(sorted(settled.items())))),
    ]
    # Share of n/a by hours from entry to end (registration: defined only if the market ends >= 26 h after entry).
    hb = Counter()
    for p in positions:
        h = (int(p["end_ts"]) - int(p["ts"])) / 3600.0
        hb["<6" if h < 6 else "6-12" if h < 12 else "12-26" if h < 26 else "26-48" if h < 48 else ">=48"] += 1
    out.append("positions by hours entry->end: " + ", ".join(f"{k} {hb[k]}" for k in ("<6", "6-12", "12-26", "26-48", ">=48")))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--now", type=int, action="append", help="report time(s), unix seconds (default: report time and now)")
    a = ap.parse_args(argv)
    t = time.time()
    rows = load(a.db)
    print(f"loaded {len(rows)} rows in {time.time() - t:.1f} s, peak RSS {cmn.rss_mb():.0f} MB", flush=True)
    if not rows:
        return 1
    t0 = min(int(r["ts"]) for r in rows)
    print(f"first snapshot {t0} (registered T0 {T0_TS}, equal: {t0 == T0_TS})")
    for now_ts in a.now or [REPORT_NOW, int(time.time())]:
        print("\n".join(["", *diagnose(rows, now_ts, t0, a.db)]), flush=True)
    print(f"peak RSS {cmn.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
