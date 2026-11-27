"""Reconciliation of Coinalyze daily bars with a venue's own history (pure: no I/O, the data and times are passed in).

Used by ``scripts/verify_coinalyze_bybit.py`` (docs/verification/2026-11-27-coinalyze-bybit-check.md). Times are unix
SECONDS; a daily bar is keyed by its 00:00 UTC open. Rates are decimal fractions (0.0001 = 0.01 %).

* :func:`coverage`: rows, duplicates, missing and misaligned days of a stored daily series;
* :func:`funding_candidates_by_day`: for each UTC day the venue settlements a daily funding bar could represent
  (first / last / max / min / mean of the settlements stamped inside the day, the first one stamped on the next
  day, i.e. the rate accrued over the day's last interval, and the last one of the previous day);
* :func:`settlements_per_day`: how many days have 1, 2, 3, ... settlements;
* :func:`informative_days`: on how many days the matching candidate differs from every other one (the days on which
  a match can tell the definitions apart);
* :func:`mismatch_breakdown`: on the days a bar field does not equal the expected candidate, what it equals instead
  (the previous day's last rate, a base rate such as 0.0001, neither) and whether it lies outside the day's range;
* :func:`compare_series` / :func:`compare_pairs`: share of pairs within a fixed tolerance and the unit scale
  (median |ours| / |ref|, about 1 when the units agree, about 100 when one side is in percent);
* :func:`liquidation_sum_check`: does a multi-venue sum equal the sum of its parts on shared intervals;
* :func:`verdict`: the ✓ / ≈ / ✗ / ◯ mark from a match share, with thresholds fixed before any comparison was run.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

DAY = 86_400
FUNDING_CANDIDATES = ("first_in_day", "last_in_day", "max_in_day", "min_in_day", "mean_in_day", "next_first", "prev_last")
BASE_FUNDING_RATE = 1e-4  # the usual default rate per 8 h interval on Binance and Bybit USDT perps (0.01 %)

# Verdict thresholds, fixed 2026-11-27 before the first comparison (owner's protocol: no tuning after seeing results).
MIN_N = 30  # fewer shared points than this: ◯ (cannot tell)
FULL_SHARE = 0.99  # ✓ when at least this share of the pairs is within tolerance
PARTIAL_SHARE = 0.90  # ≈ when at least this share; ✗ below


def day_floor(ts: int) -> int:
    """00:00 UTC (unix seconds) of the day holding ``ts``."""
    return int(ts) // DAY * DAY


def coverage(ts_values: Iterable[int], last_settled_day: int) -> dict[str, Any]:
    """Shape of a stored daily series: ``rows``, ``distinct``, ``duplicates``, ``first`` / ``last`` (ts), ``missing_days``
    between them (and up to 10 of them in ``missing``), ``misaligned`` (ts not at 00:00 UTC) and ``has_last_settled``
    (the bar of ``last_settled_day``, 00:00 UTC of the last closed and settled day, is present)."""
    ts = [int(t) for t in ts_values]
    seen = set(ts)
    if not ts:
        empty: dict[str, Any] = dict.fromkeys(("rows", "distinct", "duplicates", "missing_days", "misaligned"), 0)
        return {**empty, "first": None, "last": None, "missing": [], "has_last_settled": False}
    first, last = min(seen), max(seen)
    missing = [d for d in range(day_floor(first), last + 1, DAY) if d not in seen]
    return {
        "rows": len(ts),
        "distinct": len(seen),
        "duplicates": len(ts) - len(seen),
        "first": first,
        "last": last,
        "missing_days": len(missing),
        "missing": missing[:10],
        "misaligned": sum(1 for t in seen if t % DAY),
        "has_last_settled": last_settled_day in seen,
    }


def funding_candidates_by_day(settlements: Iterable[tuple[int, float]]) -> dict[int, dict[str, float]]:
    """``{day: {candidate: rate}}`` from venue settlements ``(ts_seconds, rate)``. Candidates of day D: the settlements
    stamped in [D, D+1d) (``first_in_day``, ``last_in_day``, ``max_in_day``, ``min_in_day``, ``mean_in_day``) and
    ``next_first``, the first settlement stamped in [D+1d, D+2d) (the rate set over D's last interval), and
    ``prev_last``, the last settlement stamped in [D-1d, D) (a rate carried over from the previous day). A day with no
    settlement of its own still gets ``next_first`` / ``prev_last`` when a neighbouring day has one. Repeated
    timestamps keep the last rate."""
    by_ts = {int(t): float(r) for t, r in settlements}
    groups: dict[int, list[tuple[int, float]]] = {}
    for t in sorted(by_ts):
        groups.setdefault(day_floor(t), []).append((t, by_ts[t]))
    out: dict[int, dict[str, float]] = {}
    for day in sorted(set(groups) | {d - DAY for d in groups} | {d + DAY for d in groups}):
        c: dict[str, float] = {}
        own = [r for _, r in groups.get(day, [])]
        if own:
            c.update(first_in_day=own[0], last_in_day=own[-1], max_in_day=max(own), min_in_day=min(own), mean_in_day=sum(own) / len(own))
        nxt = groups.get(day + DAY)
        if nxt:
            c["next_first"] = nxt[0][1]
        prv = groups.get(day - DAY)
        if prv:
            c["prev_last"] = prv[-1][1]
        out[day] = c
    return out


def settlements_per_day(settlement_ts: Iterable[int]) -> dict[int, int]:
    """``{k: number of UTC days with exactly k distinct settlement timestamps}`` (unix seconds)."""
    per: dict[int, int] = {}
    for t in set(int(x) for x in settlement_ts):
        per[day_floor(t)] = per.get(day_floor(t), 0) + 1
    hist: dict[int, int] = {}
    for k in per.values():
        hist[k] = hist.get(k, 0) + 1
    return dict(sorted(hist.items()))


def informative_days(
    cands: Mapping[int, Mapping[str, float]], days: Iterable[int], correct: str, abs_tol: float, rel_tol: float = 0.0
) -> dict[str, int]:
    """Of ``days`` where ``correct`` is a candidate (``n``), on how many does it differ (beyond tolerance) from every
    other candidate present that day (``informative``): only on those can a match pick ``correct`` over the rest."""
    n = inf = 0
    for d in days:
        c = cands.get(d) or {}
        if correct not in c:
            continue
        n += 1
        others = [v for k, v in c.items() if k != correct]
        inf += all(not within(c[correct], v, abs_tol, rel_tol) for v in others)
    return {"n": n, "informative": inf}


def mismatch_breakdown(
    ours: Mapping[int, float],
    cands: Mapping[int, Mapping[str, float]],
    expected: str,
    abs_tol: float,
    base_rate: float = BASE_FUNDING_RATE,
) -> dict[str, Any]:
    """On the days where ``ours[d]`` is not within ``abs_tol`` of ``cands[d][expected]``, what is it instead?

    ``n`` days compared, ``mismatch`` days that differ; of these ``eq_prev_last`` (= the previous day's last
    settlement), ``eq_base`` (= ``base_rate``), ``eq_neither``, ``above_day_max`` / ``below_day_min`` (outside the
    range of the day's own settlements) and ``by_year``. ``distinguishing``: mismatch days where the previous day's
    last rate is neither ``base_rate`` nor the day's first settlement, so "carried rate" and "base rate" (or "the
    open") predict different values; ``distinguishing_eq_prev_last`` / ``distinguishing_eq_base`` count which one the
    bar took on those days."""
    keys = ("mismatch", "eq_prev_last", "eq_base", "eq_neither", "above_day_max", "below_day_min")
    out: dict[str, Any] = dict.fromkeys(("n", *keys, "distinguishing", "distinguishing_eq_prev_last", "distinguishing_eq_base"), 0)
    by_year: dict[int, int] = {}
    for d in sorted(ours):
        c = cands.get(d) or {}
        if expected not in c:
            continue
        out["n"] += 1
        v = ours[d]
        if within(v, c[expected], abs_tol):
            continue
        out["mismatch"] += 1
        y = datetime.fromtimestamp(d, tz=UTC).year
        by_year[y] = by_year.get(y, 0) + 1
        prev = c.get("prev_last")
        eq_prev = prev is not None and within(v, prev, abs_tol)
        eq_base = within(v, base_rate, abs_tol)
        out["eq_prev_last"] += eq_prev
        out["eq_base"] += eq_base
        out["eq_neither"] += not eq_prev and not eq_base
        if "max_in_day" in c:
            out["above_day_max"] += v > c["max_in_day"] + abs_tol
            out["below_day_min"] += v < c["min_in_day"] - abs_tol
        if prev is not None and not within(prev, base_rate, abs_tol) and not within(prev, c.get("first_in_day", base_rate), abs_tol):
            out["distinguishing"] += 1
            out["distinguishing_eq_prev_last"] += eq_prev
            out["distinguishing_eq_base"] += eq_base
    out["by_year"] = dict(sorted(by_year.items()))
    return out


def within(a: float, b: float, abs_tol: float, rel_tol: float = 0.0) -> bool:
    """|a - b| <= abs_tol + rel_tol * |b| (``b`` is the reference)."""
    return abs(a - b) <= abs_tol + rel_tol * abs(b)


def compare_pairs(pairs: Sequence[tuple[float, float]], abs_tol: float, rel_tol: float = 0.0) -> dict[str, Any]:
    """``(ours, ref)`` pairs -> ``n``, ``n_match`` and ``share`` within tolerance, ``max_abs_diff``, ``median_abs_diff``
    and ``scale`` (median |ours| / |ref| over pairs where both are non-zero; None when there is none)."""
    n = len(pairs)
    diffs = [abs(a - b) for a, b in pairs]
    ratios = [abs(a) / abs(b) for a, b in pairs if a != 0 and b != 0]
    n_match = sum(1 for a, b in pairs if within(a, b, abs_tol, rel_tol))
    return {
        "n": n,
        "n_match": n_match,
        "share": n_match / n if n else None,
        "max_abs_diff": max(diffs) if diffs else None,
        "median_abs_diff": statistics.median(diffs) if diffs else None,
        "scale": statistics.median(ratios) if ratios else None,
    }


def compare_series(
    ours: Mapping[int, float], ref: Mapping[int, float], offset: int, abs_tol: float, rel_tol: float = 0.0
) -> dict[str, Any]:
    """:func:`compare_pairs` of ``ours[t]`` against ``ref[t + offset]`` over every ``t`` present in both (after the
    shift), plus ``first`` / ``last`` of the compared ``t``."""
    keys = [t for t in sorted(ours) if t + offset in ref]
    out = compare_pairs([(ours[t], ref[t + offset]) for t in keys], abs_tol, rel_tol)
    out.update(first=keys[0] if keys else None, last=keys[-1] if keys else None, offset=offset)
    return out


def verdict(stats: Mapping[str, Any], min_n: int = MIN_N, full: float = FULL_SHARE, partial: float = PARTIAL_SHARE) -> str:
    """✓ when ``share`` >= ``full``, ≈ when >= ``partial``, ✗ below, ◯ when ``n`` < ``min_n`` (cannot tell)."""
    n, share = stats.get("n") or 0, stats.get("share")
    if n < min_n or share is None:
        return "◯"
    if share >= full:
        return "✓"
    return "≈" if share >= partial else "✗"


def best_key(results: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Key of the comparison with the largest match share (ties: more pairs, then the key order given)."""
    scored = [(r.get("share") or 0.0, r.get("n") or 0, -i, k) for i, (k, r) in enumerate(results.items()) if r.get("n")]
    return max(scored)[3] if scored else None


def liquidation_sum_check(
    total_rows: Sequence[Mapping[str, Any]],
    part_rows: Sequence[Sequence[Mapping[str, Any]]],
    abs_tol: float,
    rel_tol: float = 0.0,
    fields: Sequence[str] = ("long", "short"),
    skip_ts: Iterable[int] = (),
) -> dict[str, Any]:
    """Does each ``total_rows`` interval equal the sum of the ``part_rows`` intervals with the same ``ts``?

    Only intervals present in the total and in every part are compared (``n``), each field separately (``n_match``
    counts intervals where every field is within tolerance); ``skip_ts`` (e.g. the still-open interval) is left out.
    ``only_total`` / ``only_parts``: intervals in the total but missing from a part, and in every part but missing
    from the total."""
    skip = set(skip_ts)
    tot = {int(r["ts"]): r for r in total_rows if int(r["ts"]) not in skip}
    parts = [{int(r["ts"]): r for r in rows if int(r["ts"]) not in skip} for rows in part_rows]
    in_all = set.intersection(*(set(p) for p in parts)) if parts else set()
    shared = sorted(set(tot) & in_all)
    n_match, max_diff = 0, 0.0
    for t in shared:
        ok = True
        for f in fields:
            s = sum(float(p[t][f]) for p in parts)
            d = abs(float(tot[t][f]) - s)
            max_diff = max(max_diff, d)
            ok = ok and within(float(tot[t][f]), s, abs_tol, rel_tol)
        n_match += ok
    return {
        "n": len(shared),
        "n_match": n_match,
        "share": n_match / len(shared) if shared else None,
        "max_abs_diff": max_diff if shared else None,
        "only_total": len(set(tot) - in_all),
        "only_parts": len(in_all - set(tot)),
    }
