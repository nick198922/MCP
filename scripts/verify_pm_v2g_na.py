"""Why most Polymarket v2-g positions are 'n/a' at +24 h: counters of causes only (standing item 454, 2026-11-24).

Accounting, not a hypothesis. Peeking ban (v2-g verdict not before 2027-03-07, v1 not before 2027-02-23): the script
never computes, prints or keeps a CLV, P&L, Brier, price or settled result of a position. It reuses the report's own
status (``pm_gap_report._horizon_status``: 'n/a' when the market ends < 26 h after entry, 'pending' while entry + 24 h
+ 1 h is in the future, else 'ok' / 'missing'); the CLV value of 'ok' is discarded on the spot. The positions are those
of ``pm_gap_report.weekly_v2g`` (v2-g's own period from ``T0_V2G_TS``, model v2-g, snapshot g) and, for comparison of
denominators only, v1's (``weekly_descriptive``: v1's period from ``T0_TS``).

Each 'n/a' gets one cause (:func:`na_cause`), decided by timestamps only:
  * ``listed_late``      the market's first collector row is < 26 h before its end, with no collector gap in the
                         26 h before that row: the market reached the collector too late for any +24 h point;
  * ``listed_late_gap``  same, but the collector had a step > 90 min in [end - 26 h, first row]: a hole may have hidden
                         earlier rows of the market (a collector cause);
  * ``late_entry``       the market was in the collector >= 26 h before its end, but its first qualifying snapshot
                         (the entry) came < 26 h before the end;
  * ``rows_missing``     no rows of the market at all (should not happen: the entry is a row).
A ``late_entry`` is split further by :func:`early_reason` (filters of the rule, labels only; no edge is printed).
'Defined' = 'ok' + 'missing'; an early resolution or close of a market can only make a defined point 'missing', never
'n/a' (the bar uses the scheduled ``end_ts``).
Output: aggregate counters and coarse hour buckets only; no line names a market, an asset, a week or an exact value.

Usage: uv run python scripts/verify_pm_v2g_na.py [--db PATH] [--date YYYY-MM-DD | --now UNIX]
"""

from __future__ import annotations

import argparse
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)
from pm_gap_paper_report import DEFAULT_DB
from verify_pm_gap_24h_gaps import load

from tradedesk.analytics.pm_gap_paper import (
    HOUR_S,
    MODEL_V2G,
    PRIMARY_H,
    EntryRule,
    accepted_rule_hashes,
    eligible,
    open_positions,
    win_probs,
    with_snapshot_g,
)
from tradedesk.analytics.pm_gap_report import T0_TS, T0_V2G_TS, _horizon_status, main_period

NEED_S = (PRIMARY_H + 2) * HOUR_S  # the report's 'defined' bar: market ends >= 26 h after entry
GAP_S = 90 * 60  # a collector step longer than this counts as a hole (hourly cadence)
ENTRY_BUCKETS = ((6, "<6"), (12, "6-12"), (24, "12-24"), (26, "24-26"), (48, "26-48"), (10**9, ">=48"))
CAUSES = ("listed_late", "listed_late_gap", "late_entry", "rows_missing")


def bucket(hours: float) -> str:
    return next(lbl for upper, lbl in ENTRY_BUCKETS if hours < upper)


def has_gap(times: Sequence[int], lo: int, hi: int, gap_s: int = GAP_S) -> bool:
    """True if the sorted collector moments ``times`` leave a step > ``gap_s`` anywhere inside [lo, hi] (the edges
    count: a first moment later than lo + gap_s, or a last one earlier than hi - gap_s)."""
    if hi <= lo:
        return False
    inside = times[bisect_left(times, lo) : bisect_right(times, hi)]
    pts = [lo, *inside, hi]
    return any(b - a > gap_s for a, b in zip(pts, pts[1:], strict=False))


def na_cause(pos: Mapping[str, Any], market_ts: Sequence[int], collector_ts: Sequence[int]) -> str:
    """Cause of one 'n/a' position (see the module docstring). ``market_ts``: ts of every row of its market up to now;
    ``collector_ts``: sorted moments of the whole collector up to now. Timestamps only: no price is read."""
    if not market_ts:
        return "rows_missing"
    end, first = int(pos["end_ts"]), min(int(t) for t in market_ts)
    if end - first >= NEED_S:
        return "late_entry"
    return "listed_late_gap" if has_gap(collector_ts, end - NEED_S, first) else "listed_late"


def _not_eligible_reason(row: Mapping[str, Any], rule: EntryRule, acc: set[str]) -> str:
    """The first filter of :func:`eligible` a row fails, as a label (no price is read)."""
    if row.get("model_note") != "ok":
        return f"note:{row.get('model_note')}"
    if row.get("model_prob") is None or row.get("model_covered") != 1:
        return "no_model_or_uncovered"
    if str(row.get("rules_hash")) not in acc:
        return "rules_not_accepted"
    if not rule.min_hours_to_end <= (int(row["end_ts"]) - int(row["ts"])) / HOUR_S <= rule.max_hours_to_end:
        return "outside_horizon"
    return "quote_skew"


def early_reason(pos: Mapping[str, Any], period_rows: Sequence[Mapping[str, Any]], rule: EntryRule, acc: set[str]) -> str:
    """For a 'late_entry' position: why its market gave no signal while >= 26 h were left. ``period_rows``: the
    market's rows inside the rule's entry period (v2-g: with ``snapshot_g``). Labels: ``no_early_rows_in_period``
    (rows >= 26 h before the end exist only before the period start), ``not_eligible:<most common filter>``,
    ``eligible_no_model_prob`` (e.g. v2-g without g) or ``priced_no_signal`` (model priced, but net edge below the
    threshold or ask / depth outside the rule). Edges are not printed."""
    end, entry = int(pos["end_ts"]), int(pos["ts"])
    early = [r for r in period_rows if end - int(r["ts"]) >= NEED_S and int(r["ts"]) < entry]
    if not early:
        return "no_early_rows_in_period"
    elig = [r for r in early if eligible(r, rule, acc)]
    if not elig:
        return "not_eligible:" + Counter(_not_eligible_reason(r, rule, acc) for r in early).most_common(1)[0][0]
    if all(win_probs(r, rule) is None for r in elig):
        return "eligible_no_model_prob"
    return "priced_no_signal"


def account(
    positions: Sequence[Mapping[str, Any]],
    by_mkt: Mapping[str, Sequence[Mapping[str, Any]]],
    collector_ts: Sequence[int],
    now_ts: int,
    period_by_mkt: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    rule: EntryRule | None = None,
    acc: set[str] | None = None,
) -> dict[str, Any]:
    """Counters for ``positions``: +24 h status (the report's), 'n/a' by cause, by kind, by hours entry -> end and by
    hours the market was in the collector before its end; with ``period_by_mkt`` also :func:`early_reason` of every
    'late_entry'. Only labels and counts are returned."""
    status: Counter = Counter()
    cause: Counter = Counter()
    by_kind: dict[str, Counter] = defaultdict(Counter)
    entry_h: Counter = Counter()
    listed_h: Counter = Counter()
    early: Counter = Counter()
    cause_h: Counter = Counter()
    for p in positions:
        mk = by_mkt.get(str(p["market_id"]), [])
        _, s = _horizon_status(p, mk, PRIMARY_H, now_ts)  # value discarded: label only
        status[s] += 1
        by_kind[str(p["kind"])][s] += 1
        if s != "n/a":
            continue
        mts = [int(r["ts"]) for r in mk]
        c = na_cause(p, mts, collector_ts)
        cause[c] += 1
        eb = bucket((int(p["end_ts"]) - int(p["ts"])) / 3600)
        entry_h[eb] += 1
        cause_h[f"{c}/{eb}"] += 1
        if mts:
            listed_h[bucket((int(p["end_ts"]) - min(mts)) / 3600)] += 1
        if c == "late_entry" and period_by_mkt is not None:
            early[early_reason(p, period_by_mkt.get(str(p["market_id"]), []), rule or EntryRule(), acc or set())] += 1
    return {
        "n": len(positions),
        "status": dict(status),
        "na_cause": dict(cause),
        "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
        "na_entry_to_end_h": dict(entry_h),
        "na_cause_by_entry_h": dict(cause_h),
        "na_listed_before_end_h": dict(listed_h),
        "late_entry_early_reason": dict(early),
    }


def v2g_positions(rows: Iterable[Mapping[str, Any]], now_ts: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], EntryRule, set[str]]:
    """Exactly ``weekly_v2g``'s ``p2`` for t0 = T0_V2G_TS, with its period rows, rule and accepted hashes."""
    rows = list(rows)
    start, end = main_period(T0_V2G_TS)
    period = with_snapshot_g(r for r in rows if start <= int(r["ts"]) < end and int(r["ts"]) <= now_ts)
    rule, acc = EntryRule(model=MODEL_V2G), accepted_rule_hashes(rows)
    return open_positions(period, rule, acc), period, rule, acc


def v1_positions(rows: Iterable[Mapping[str, Any]], now_ts: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], EntryRule, set[str]]:
    """Exactly ``weekly_descriptive``'s positions for t0 = T0_TS (v1 rule, v1's own period)."""
    rows = list(rows)
    start, end = main_period(T0_TS)
    period = [r for r in rows if start <= int(r["ts"]) < end and int(r["ts"]) <= now_ts]
    rule, acc = EntryRule(), accepted_rule_hashes(rows)
    return open_positions(period, rule, acc), period, rule, acc


def _fmt(d: Mapping[str, int], order: Sequence[str] | None = None) -> str:
    keys = [k for k in order if k in d] if order else sorted(d)
    return ", ".join(f"{k} {d[k]}" for k in keys) or "-"


def render(name: str, a: Mapping[str, Any]) -> list[str]:
    st = a["status"]
    na, n = st.get("n/a", 0), a["n"]
    defined = st.get("ok", 0) + st.get("missing", 0)
    order = [lbl for _, lbl in ENTRY_BUCKETS]
    return [
        f"{name}: positions {n}; +24 h status: {_fmt(st, ('n/a', 'pending', 'ok', 'missing'))}",
        f"  n/a share of all {na}/{n}"
        + (f" = {na / n:.0%}" if n else "")
        + f"; missing share of defined (ok+missing) {st.get('missing', 0)}/{defined}",
        f"  n/a by cause: {_fmt(a['na_cause'], CAUSES)} (sum {sum(a['na_cause'].values())})",
        "  status by kind: " + "; ".join(f"{k}: {_fmt(v, ('n/a', 'pending', 'ok', 'missing'))}" for k, v in a["by_kind"].items()),
        f"  n/a by hours entry -> end: {_fmt(a['na_entry_to_end_h'], order)}",
        f"  n/a by cause / hours entry -> end: {_fmt(a['na_cause_by_entry_h'])}",
        f"  n/a by hours first collector row -> end: {_fmt(a['na_listed_before_end_h'], order)}",
        f"  late_entry: why no signal while >= 26 h were left: {_fmt(a['late_entry_early_reason'])}",
    ]


def listing_lead(by_mkt: Mapping[str, Sequence[Mapping[str, Any]]], end_after: int) -> str:
    """Collector level, positions or not: markets ending after ``end_after`` by kind and by hours from their first
    collector row to their end (a market ending later than that could have been seen >= 26 h before it, the
    collector already running)."""
    c: dict[str, Counter] = defaultdict(Counter)
    for rs in by_mkt.values():
        end = int(rs[0]["end_ts"])
        if end > end_after:
            c[str(rs[0]["kind"])][bucket((end - min(int(r["ts"]) for r in rs)) / 3600)] += 1
    order = [lbl for _, lbl in ENTRY_BUCKETS]
    return "; ".join(f"{k}: {_fmt(v, order)}" for k, v in sorted(c.items()))


def at_date(s: str | None, now: int | None) -> int:
    wall = int(time.time())
    if now is not None:
        return min(now, wall)
    if s:
        return min(int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()) + 86_400 - 1, wall)
    return wall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--date", help="state at the end of this UTC day (capped at the wall clock), like checkpoint_report")
    ap.add_argument("--now", type=int, help="state at this unix time (overrides --date)")
    a = ap.parse_args(argv)
    now_ts = at_date(a.date, a.now)
    t = time.time()
    rows = [r for r in load(a.db) if int(r["ts"]) <= now_ts]
    print(f"loaded {len(rows)} rows in {time.time() - t:.1f} s, peak RSS {cmn.rss_mb():.0f} MB", flush=True)
    if not rows:
        return 1
    by_mkt: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by_mkt[str(r["market_id"])].append(r)
    times = sorted({int(r["ts"]) for r in rows})
    s0 = T0_V2G_TS - NEED_S
    steps = [(x, y) for x, y in zip(times, times[1:], strict=False) if y > s0 and y - x > GAP_S]
    print(f"now {datetime.fromtimestamp(now_ts, UTC):%Y-%m-%d %H:%M} UTC; collector moments {len(times)}")
    print(
        f"collector steps > {GAP_S // 60} min since T0 v2-g - 26 h: {len(steps)}"
        + "".join(
            f"\n  {datetime.fromtimestamp(x, UTC):%m-%d %H:%M} -> {datetime.fromtimestamp(y, UTC):%m-%d %H:%M}" for x, y in steps[:10]
        )
    )
    print(f"all markets ending after T0 v2-g + 26 h, hours first collector row -> end, by kind: {listing_lead(by_mkt, T0_V2G_TS + NEED_S)}")
    for name, build in (("v2-g (own, from T0 v2-g)", v2g_positions), ("v1 (from T0 v1, denominators only)", v1_positions)):
        pos, period, rule, acc = build(rows, now_ts)
        mkts = {str(p["market_id"]) for p in pos}
        period_by_mkt: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for r in period:
            if str(r["market_id"]) in mkts:
                period_by_mkt[str(r["market_id"])].append(r)
        del period
        print("\n".join(render(name, account(pos, by_mkt, times, now_ts, period_by_mkt, rule, acc))), flush=True)
    print(f"peak RSS {cmn.rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
