"""Windowed event-study aggregator.

Given a historical OHLCV series and a list of event timestamps, measures
one scalar per event over the window ``[t0 - before, t0 + after]`` and
returns distribution stats across the sample.

Measures:

* ``direction``   — 1 when ``close(after) >= close(t0)``, 0 otherwise.
  Hit-rate = share of ups. Example DoubleTop finding: "price was higher
  at 21:00 than at 16:30 on 71 % of FOMC days".
* ``close_vs_t0`` — ``close(after) / close(t0) - 1``. Signed return.
* ``max_up``      — max intra-window up-excursion from ``close(t0)``
  (always ≥ 0).
* ``max_dd``      — max intra-window drawdown from ``close(t0)``
  (always ≤ 0).
* ``volatility`` — stdev of per-bar log-returns over the window.

Reports ``n_sample`` and ``small_sample_flag=True`` when n < 20 so the
caller cannot miss the caveat (a DoubleTop pattern).
"""

from __future__ import annotations

import math
from typing import Any

MEASURES = ("direction", "close_vs_t0", "max_up", "max_dd", "volatility")


def _closest_bar_idx(bars: list[dict[str, Any]], ts_ms: int) -> int | None:
    """Index of the bar whose ts is <= ts_ms AND the next one is >= ts_ms
    (so we take the bar in which the event timestamp falls). Returns None
    when ts_ms is outside the series range."""
    if not bars or ts_ms < bars[0]["ts"] or ts_ms > bars[-1]["ts"]:
        return None
    lo, hi = 0, len(bars) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid]["ts"] <= ts_ms:
            lo = mid
        else:
            hi = mid - 1
    return lo


def windowed_measure(
    bars: list[dict[str, Any]],
    event_ts_ms: int,
    *,
    before_ms: int,
    after_ms: int,
    measure: str,
) -> float | None:
    """One scalar for one event. Returns None when the window is incomplete."""
    if measure not in MEASURES:
        raise ValueError(f"unknown measure: {measure!r}")
    if not bars:
        return None
    i0 = _closest_bar_idx(bars, event_ts_ms)
    if i0 is None:
        return None

    lo_ts = event_ts_ms - before_ms
    hi_ts = event_ts_ms + after_ms
    i_lo = _closest_bar_idx(bars, lo_ts)
    i_hi = _closest_bar_idx(bars, hi_ts)
    if i_lo is None or i_hi is None or i_hi <= i0 or i_lo > i0:
        return None
    window = bars[i_lo : i_hi + 1]
    c0 = bars[i0]["close"]
    c_after = bars[i_hi]["close"]
    if c0 <= 0:
        return None

    if measure == "direction":
        return 1.0 if c_after >= c0 else 0.0
    if measure == "close_vs_t0":
        return c_after / c0 - 1.0
    if measure == "max_up":
        peak = max(b["high"] for b in bars[i0 : i_hi + 1])
        return max(peak / c0 - 1.0, 0.0)
    if measure == "max_dd":
        trough = min(b["low"] for b in bars[i0 : i_hi + 1])
        return min(trough / c0 - 1.0, 0.0)
    if measure == "volatility":
        rets = []
        for a, b in zip(window, window[1:], strict=False):
            if a["close"] > 0 and b["close"] > 0:
                rets.append(math.log(b["close"] / a["close"]))
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var) * math.sqrt(len(rets))  # window-level realised stdev
    return None


def _percentile(xs: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in [0, 1]). None for empty input."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    w = pos - lo
    return s[lo] + w * (s[hi] - s[lo])


def summarise_events(values: list[float], *, measure: str) -> dict[str, Any]:
    """Distribution stats for a measure column. ``hit_rate`` is reported for
    ``direction`` (mean of 0/1 flags); for other measures it is None."""
    n = len(values)
    if n == 0:
        return {
            "n_sample": 0,
            "hit_rate": None,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
            "small_sample_flag": True,
        }
    mean = sum(values) / n
    hit = mean if measure == "direction" else None
    return {
        "n_sample": n,
        "hit_rate": hit,
        "mean": mean,
        "median": _percentile(values, 0.5),
        "p25": _percentile(values, 0.25),
        "p75": _percentile(values, 0.75),
        "min": min(values),
        "max": max(values),
        "small_sample_flag": n < 20,
    }


def event_study(
    bars: list[dict[str, Any]],
    event_ts_ms: list[int],
    *,
    before_minutes: float,
    after_minutes: float,
    measure: str,
) -> dict[str, Any]:
    """End-to-end: value per event, then distribution stats + per-event list.

    ``bars`` are store rows ({ts, open, high, low, close, volume}); ``ts`` must
    be in milliseconds (matches ohlcv_hist).
    """
    before_ms = int(before_minutes * 60_000)
    after_ms = int(after_minutes * 60_000)
    per_event: list[dict[str, Any]] = []
    values: list[float] = []
    for ts in event_ts_ms:
        v = windowed_measure(bars, int(ts), before_ms=before_ms, after_ms=after_ms, measure=measure)
        per_event.append({"ts_ms": int(ts), "value": v})
        if v is not None:
            values.append(v)
    stats = summarise_events(values, measure=measure)
    return {
        "measure": measure,
        "before_minutes": before_minutes,
        "after_minutes": after_minutes,
        "stats": stats,
        "per_event": per_event,
        "n_events_requested": len(event_ts_ms),
        "n_events_evaluated": len(values),
    }
