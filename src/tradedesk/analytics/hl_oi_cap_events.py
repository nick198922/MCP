"""Hyperliquid OI-cap events and the step-1 gates G1/G2 (pure: no I/O, the period is passed in).

Docs: docs/ideas/IDEA-20261028-hl-oi-cap-state-forward.md, «Попередня реєстрація», step 1. Input: the hourly rows of
``hl_oi_cap_snapshot`` as ``(ts, coin, at_cap, funding)``. A run is a maximal sequence of a coin's hourly keys spaced
exactly ``HOUR_S`` apart with ``at_cap`` set; a missing hour (no row for the coin) breaks it. An event is a run of at
least ``min_hours`` hours; ``t0`` is the key of its ``min_hours``-th hour. It counts when the run starts inside the
exposure window ``[first_ts + warmup_s, cutoff)``, ``t0 < cutoff`` and the coin's previous qualifying run (counted or
not) ended at least ``lookback_s`` before the run's start. Event ``funding`` is the row's funding at ``t0`` (hourly decimal fraction).

Only data with ``ts < cutoff`` is used, so an event list up to a time t does not depend on rows after t.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

HOUR_S = 3600
WEEK_S = 7 * 24 * HOUR_S
G1_MIN_PER_WEEK = 8.0
G2_LOW, G2_HIGH = 0.25, 0.75


@dataclass(frozen=True)
class Event:
    coin: str
    start_ts: int  # key of the run's first hour
    t0: int  # key of the run's min_hours-th hour
    funding: float | None  # hourly funding at t0


def runs(rows: Iterable[tuple[int, str, int, float | None]], cutoff: int) -> dict[str, list[tuple[int, int]]]:
    """Per coin, the at-cap runs as ``(start_ts, last_ts)`` from rows with ``ts < cutoff`` (oldest first)."""
    capped: dict[str, list[int]] = defaultdict(list)
    for ts, coin, at_cap, _ in rows:
        if ts < cutoff and at_cap:
            capped[coin].append(int(ts))
    out: dict[str, list[tuple[int, int]]] = {}
    for coin, hours in capped.items():
        hours = sorted(set(hours))
        segs: list[tuple[int, int]] = []
        start = prev = hours[0]
        for h in hours[1:]:
            if h - prev != HOUR_S:
                segs.append((start, prev))
                start = h
            prev = h
        segs.append((start, prev))
        out[coin] = segs
    return out


def events(
    rows: Iterable[tuple[int, str, int, float | None]],
    *,
    first_ts: int,
    cutoff: int,
    min_hours: int = 6,
    warmup_s: int = 24 * HOUR_S,
    lookback_s: int | None = 24 * HOUR_S,
) -> list[Event]:
    """Events (see the module doc), sorted by ``t0`` then coin. ``lookback_s=None`` drops the «first in 24 h» rule."""
    rows = [r for r in rows if r[0] < cutoff]
    funding = {(int(ts), coin): f for ts, coin, _, f in rows}
    lo = first_ts + warmup_s
    out: list[Event] = []
    for coin, segs in runs(rows, cutoff).items():
        last_end: int | None = None  # last hour of the previous run of >= min_hours
        for start, last in segs:
            if (last - start) // HOUR_S + 1 < min_hours:
                continue
            t0 = start + (min_hours - 1) * HOUR_S
            # fresh: at least lookback_s between the end of the previous qualifying run's last hour and this start
            fresh = lookback_s is None or last_end is None or start - (last_end + HOUR_S) >= lookback_s
            if fresh and lo <= start and t0 < cutoff:
                out.append(Event(coin, start, t0, funding.get((t0, coin))))
            last_end = last
    return sorted(out, key=lambda e: (e.t0, e.coin))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a share k/n (None when n = 0)."""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def gates(evs: list[Event], *, first_ts: int, cutoff: int, warmup_s: int = 24 * HOUR_S) -> dict:
    """G1 (events per week over the exposure window >= 8) and G2 (share with funding > 0 in [0.25, 0.75])."""
    weeks = max(cutoff - (first_ts + warmup_s), 0) / WEEK_S
    n = len(evs)
    rate = n / weeks if weeks > 0 else None
    known = [e.funding for e in evs if e.funding is not None]
    pos = sum(1 for f in known if f > 0)
    share = pos / len(known) if known else None
    per_week: Counter[int] = Counter()
    for e in evs:
        per_week[(cutoff - 1 - e.t0) // WEEK_S] += 1  # 0 = the last whole week before cutoff
    coins = Counter(e.coin for e in evs)
    g1 = rate is not None and rate >= G1_MIN_PER_WEEK
    g2 = share is not None and G2_LOW <= share <= G2_HIGH
    return {
        "n": n,
        "exposure_weeks": weeks,
        "rate_per_week": rate,
        "g1_pass": g1,
        "funding_known": len(known),
        "funding_pos": pos,
        "share_pos": share,
        "share_pos_ci95": wilson(pos, len(known)),
        "g2_pass": g2,
        "per_week_back": dict(sorted(per_week.items())),
        "coins": len(coins),
        "top_coin_share": (coins.most_common(1)[0][1] / n) if n else None,
        "top_coins": coins.most_common(10),
        "pass": g1 and g2,
    }


def coverage(hours: Iterable[int], first_ts: int, cutoff: int) -> dict:
    """Stored distinct hours vs the expected hourly keys in ``[first_ts, cutoff)``: count, missing, longest gap."""
    have = sorted({h for h in hours if first_ts <= h < cutoff})
    expected = max((cutoff - first_ts + HOUR_S - 1) // HOUR_S, 0)
    longest, prev = 0, first_ts - HOUR_S
    for h in [*have, cutoff]:
        longest = max(longest, (h - prev) // HOUR_S - 1)
        prev = h
    return {"expected": expected, "stored": len(have), "missing": expected - len(have), "longest_gap_h": longest}
