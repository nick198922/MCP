"""Descriptive calibration of the order-book wall collector (IDEA-20261114-book-walls item 1, "first day of data").

Pure functions over rows of ``book_wall_event``, ``book_depth_minute`` and ``book_stream_hour`` (schema 41), read by
``scripts/book_walls_calibration.py``. Blind by construction: only properties of the walls (size, bar, life, distance,
traded share, reason) and of the stream are used; no mid after a wall event, no price change, no outcome, so the
December test of the card (item 2) stays unseen.

Exact vs estimated (``candidate``): the stored rows hold each wall's largest size and the bar it passed, so a *higher*
coin minimum (or K) can be applied to them only approximately. A wall passes the new bar when its ``size_max`` does;
under the new bar it would have opened later and closed earlier (it stays open while >= ``HOLD_FRACTION`` x the bar),
and one old wall may split into several new ones, so the counts and the walls open at once are estimates. When the old
bar was the coin minimum the median term is only known to be <= minimum / K, so a new K gives a range (``sure`` /
``maybe``). A *lower* bar is not derivable at all (the levels below the old bar were never stored).
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

DAY_S = 86_400
HOUR_S = 3_600
REOPEN_MS = 60_000  # a wall that opens on the same side and price within this long of the previous one's close: flicker


def quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already sorted list (nan when empty)."""
    if not sorted_vals:
        return math.nan
    pos = q * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def describe(values: Iterable[float]) -> dict[str, float]:
    """n, p10, p50, p90, max of the finite values."""
    v = sorted(x for x in values if x is not None and math.isfinite(x))
    return {
        "n": len(v),
        "p10": quantile(v, 0.1),
        "p50": quantile(v, 0.5),
        "p90": quantile(v, 0.9),
        "max": v[-1] if v else math.nan,
    }


def key_of(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row["venue"]), str(row["symbol"])


def event_table(
    events: Iterable[Mapping[str, Any]], hours: float, min_qty: Mapping[tuple[str, str], float], near_dist: float
) -> dict[tuple[str, str], dict[str, Any]]:
    """Per venue x symbol: stored walls per reason (and per day over ``hours``), life (s), size_max / bar, the share of
    walls whose bar was the coin minimum (``min_qty`` per key), distances (decimal), traded share of size_max, walls
    with a feed break, and the ``pulled_near`` walls that already opened within ``near_dist`` of the mid."""
    by: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for e in events:
        by[key_of(e)].append(e)
    scale = 24.0 / hours if hours > 0 else math.nan
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, evs in sorted(by.items()):
        reasons: dict[str, int] = defaultdict(int)
        for e in evs:
            reasons[str(e["reason"])] += 1
        floor = min_qty.get(key)
        near = [e for e in evs if e["reason"] == "pulled_near"]
        out[key] = {
            "n": len(evs),
            "per_day": len(evs) * scale,
            "reasons": dict(reasons),
            "reasons_per_day": {r: n * scale for r, n in reasons.items()},
            "life_s": describe((e["close_ms"] - e["open_ms"]) / 1000 for e in evs),
            "life_s_by_reason": {r: describe((e["close_ms"] - e["open_ms"]) / 1000 for e in evs if e["reason"] == r) for r in reasons},
            "ratio": describe(e["size_max"] / e["threshold"] for e in evs if e["threshold"] > 0),
            "at_min_share": (sum(1 for e in evs if e["threshold"] <= floor * (1 + 1e-9)) / len(evs) if floor is not None else math.nan),
            "dist_open": describe(e["dist_open"] for e in evs),
            "dist_min": describe(e["dist_min"] for e in evs),
            "dist_close": describe(e["dist_close"] for e in evs),
            "traded_share": describe(e["traded"] / e["size_max"] for e in evs if e["size_max"] > 0),
            "traded_any": sum(1 for e in evs if e["traded"] > 0) / len(evs),
            "gaps_share": sum(1 for e in evs if (e["gaps"] or 0) > 0) / len(evs),
            "near_born_near": sum(1 for e in near if e["dist_open"] <= near_dist),
        }
    return out


def flicker(events: Iterable[Mapping[str, Any]], reopen_ms: int = REOPEN_MS) -> dict[tuple[str, str], dict[str, Any]]:
    """Per venue x symbol: distinct (side, price) levels, walls per level (p50 / p90 / max), and the walls that opened on
    the same side and price within ``reopen_ms`` of the previous wall's close there (the same level blinking)."""
    by: dict[tuple[str, str], dict[tuple[str, float], list[tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    for e in events:
        by[key_of(e)][(str(e["side"]), float(e["price"]))].append((int(e["open_ms"]), int(e["close_ms"])))
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, levels in sorted(by.items()):
        reopen = 0
        n = 0
        for spans in levels.values():
            spans.sort()
            n += len(spans)
            reopen += sum(1 for (_, c0), (o1, _) in zip(spans, spans[1:], strict=False) if 0 <= o1 - c0 <= reopen_ms)
        per_level = describe(float(len(s)) for s in levels.values())
        out[key] = {"walls": n, "levels": len(levels), "per_level": per_level, "reopen": reopen, "reopen_share": reopen / n}
    return out


def depth_table(rows: Iterable[Mapping[str, Any]], hours: float, band: float, view_margin: float) -> dict[tuple[str, str], dict[str, Any]]:
    """Per venue x symbol from the minute rows: minutes covered, walls open at once (both sides), the share of minutes
    with any, short ``withdrawn`` walls dropped (total and per day), book breaks, the visible distance per side and the
    share of minutes where ``view_margin`` x that distance is narrower than ``band`` (walls are looked for only there)."""
    by: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by[key_of(r)].append(r)
    scale = 24.0 / hours if hours > 0 else math.nan
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, rs in sorted(by.items()):
        walls = [float((r["walls_bid"] or 0) + (r["walls_ask"] or 0)) for r in rs]
        short = sum(int(r["short_walls"] or 0) for r in rs)
        out[key] = {
            "minutes": len(rs),
            "open": describe(walls),
            "open_mean": sum(walls) / len(walls),
            "any_share": sum(1 for w in walls if w > 0) / len(walls),
            "short": short,
            "short_per_day": short * scale,
            "gaps": sum(int(r["gaps"] or 0) for r in rs),
            "bid_view": describe(float(r["bid_view"]) for r in rs if r["bid_view"] is not None),
            "ask_view": describe(float(r["ask_view"]) for r in rs if r["ask_view"] is not None),
            "narrow_bid": sum(1 for r in rs if (r["bid_view"] or 0.0) * view_margin < band) / len(rs),
            "narrow_ask": sum(1 for r in rs if (r["ask_view"] or 0.0) * view_margin < band) / len(rs),
        }
    return out


def downtime(minutes: Iterable[int], min_gap_s: int = 120) -> list[tuple[int, int]]:
    """(last minute seen, next minute seen) of every pause in the union of depth-row minutes (unix s) longer than
    ``min_gap_s``: no venue wrote a row, i.e. the collector (service) was down, e.g. a restart after a deploy."""
    ts = sorted(set(int(m) for m in minutes))
    return [(a, b) for a, b in zip(ts, ts[1:], strict=False) if b - a > min_gap_s]


def stream_table(hours_rows: Iterable[Mapping[str, Any]], split_s: int | None) -> dict[str, dict[str, dict[str, float]]]:
    """Per stream: hours, failures, reconnects and connected share, split into whole hours before ``split_s`` (unix s),
    the hour containing it (``mixed``) and whole hours after; everything in ``all`` (``split_s`` None: only ``all``)."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for r in hours_rows:
        h = int(r["hour_ts"])
        parts = ["all"]
        if split_s is not None:
            parts.append("before" if h + HOUR_S <= split_s else "after" if h >= split_s else "mixed")
        for p in parts:
            d = out.setdefault(str(r["stream"]), {}).setdefault(
                p, {"hours": 0, "failures": 0, "reconnects": 0, "connected_s": 0.0, "frames": 0}
            )
            d["hours"] += 1
            d["failures"] += int(r["failures"] or 0)
            d["reconnects"] += int(r["reconnects"] or 0)
            d["connected_s"] += float(r["connected_s"] or 0.0)
            d["frames"] += int(r["frames"] or 0)
    for parts in out.values():
        for d in parts.values():
            d["failures_per_hour"] = d["failures"] / d["hours"]
            d["connected_share"] = d["connected_s"] / (d["hours"] * HOUR_S)
    return dict(sorted(out.items()))


def new_bar(e: Mapping[str, Any], min_old: float, min_new: float, k_old: float, k_new: float) -> tuple[float, float]:
    """(low, high) bounds of the bar a stored wall would have met with coin minimum ``min_new`` and multiplier ``k_new``
    instead of ``min_old`` / ``k_old``. When the old bar was above the minimum it was K x median, so the median is exact
    and so is the new bar; when it was the minimum, the median is only known to be <= min_old / k_old."""
    thr = float(e["threshold"])
    if thr > min_old * (1 + 1e-9):
        b = max(min_new, thr * k_new / k_old)
        return b, b
    return min_new, max(min_new, min_old * k_new / k_old)


def candidate(
    events: Iterable[Mapping[str, Any]],
    sample_s: Iterable[int],
    hours: float,
    *,
    min_old: float,
    min_new: float,
    k_old: float,
    k_new: float,
) -> dict[str, Any]:
    """Estimate for one venue x symbol under a higher bar (``min_new`` >= ``min_old``, ``k_new`` >= ``k_old``): stored
    walls a day that would still pass (``sure``: above the high bound, ``maybe``: above the low bound; by reason for
    ``sure``) and the mean walls open at the sampled minutes (``sample_s``, unix s), counted from the walls' own open /
    close spans (``sure``). Estimates (module docstring); the same span count at the current bar is the check of the
    method against the stored minute rows (walls open at a restart are lost, short ones are not stored)."""
    if min_new < min_old or k_new < k_old:
        raise ValueError("only a higher bar is derivable from stored walls")
    scale = 24.0 / hours if hours > 0 else math.nan
    sure: list[Mapping[str, Any]] = []
    maybe = 0
    for e in events:
        lo, hi = new_bar(e, min_old, min_new, k_old, k_new)
        if e["size_max"] >= lo:
            maybe += 1
        if e["size_max"] >= hi:
            sure.append(e)
    reasons: dict[str, int] = defaultdict(int)
    for e in sure:
        reasons[str(e["reason"])] += 1
    return {
        "min_qty": min_new,
        "k": k_new,
        "sure_per_day": len(sure) * scale,
        "maybe_per_day": maybe * scale,
        "reasons_per_day": {r: n * scale for r, n in sorted(reasons.items())},
        "open_mean": open_at(sure, sample_s),
    }


def open_at(events: Iterable[Mapping[str, Any]], sample_s: Iterable[int]) -> float:
    """Mean number of walls whose [open, close) span covers each sampled time (unix s); nan without samples."""
    opens = sorted(int(e["open_ms"]) for e in events)
    closes = sorted(int(e["close_ms"]) for e in events)
    counts = [bisect.bisect_right(opens, t * 1000) - bisect.bisect_right(closes, t * 1000) for t in sample_s]
    return sum(counts) / len(counts) if counts else math.nan


def table_bytes(pages: Mapping[str, int], rows: Mapping[str, int], hours: float) -> dict[str, dict[str, float]]:
    """Bytes (``pages``: table or index name -> bytes on disk, from ``dbstat``) and rows per table, scaled to a day."""
    scale = 24.0 / hours if hours > 0 else math.nan
    out = {name: {"bytes": float(b), "bytes_per_day": b * scale, "rows": float(rows.get(name, 0))} for name, b in pages.items()}
    for d in out.values():
        d["rows_per_day"] = d["rows"] * scale
    return dict(sorted(out.items()))
