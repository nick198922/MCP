"""S12 v4 modules from the weekly audit of 2026-11-03 (IDEA-20261030-demo-setups-depth, registration «Попередня
реєстрація хвилі S12 v4 з аудиту 03.11»).

Pure functions in the *long frame* (shorts are passed mirrored): no I/O, every value reads only the bars named in its
docstring, all of them closed by the close of the touch bar j (the v4 entry).

- L ``sweep_level`` / ``sweep_flag``: the nearest (highest) unswept 3-candle Williams fractal low on 1h inside the
  lower half of the gap (``full <= level <= mid``), confirmed before the touch bar opens; the flag is set when the
  touch bar's low trades below it and the bar closes above it.
- A ``acceptance_hours``: how many of the touch bar's 1h sub-bars closed above the gap's middle.
"""

from __future__ import annotations

import math

import numpy as np

HOUR_MS = 3_600_000


def fractal_lows(t: np.ndarray, low: np.ndarray, lo: int, end: int, step_ms: int = HOUR_MS) -> np.ndarray:
    """Indices k with ``lo <= k`` and ``k + 1 < end`` that are 3-candle Williams fractal lows: ``low[k] < low[k-1]``
    and ``low[k] < low[k+1]`` (strict), with k-1, k, k+1 consecutive bars (no hole). Reads bars ``max(lo-1, 0) ..
    end-1`` only."""
    lo = max(int(lo), 1)
    end = min(int(end), len(low))
    if end - lo < 2:
        return np.zeros(0, dtype=np.int64)
    k = np.arange(lo, end - 1)
    lw = np.asarray(low, dtype=float)
    tt = np.asarray(t, dtype=np.int64)
    ok = (lw[k] < lw[k - 1]) & (lw[k] < lw[k + 1]) & (tt[k + 1] - tt[k - 1] == 2 * step_ms)
    return k[ok]


def sweep_level(t: np.ndarray, low: np.ndarray, lo: int, end: int, full: float, mid: float, step_ms: int = HOUR_MS) -> float:
    """L: the highest fractal low k (``fractal_lows(t, low, lo, end)``) with ``full <= low[k] <= mid`` that is still
    unswept at ``end`` (no bar k+1 .. end-1 with ``low <= low[k]``); NaN if none.

    ``lo`` is the first 1h bar of the gap's first candle (i-2), ``end`` the first 1h bar of the touch bar j (exclusive):
    the level is known before the touch bar opens. Reads bars ``lo-1 .. end-1`` only."""
    lw = np.asarray(low, dtype=float)
    best = math.nan
    for k in fractal_lows(t, lw, lo, end, step_ms):
        v = float(lw[k])
        if not (full <= v <= mid):
            continue
        if np.any(lw[k + 1 : end] <= v):
            continue
        if not math.isfinite(best) or v > best:
            best = v
    return best


def sweep_flag(level: float, low_j: float, close_j: float) -> bool:
    """L flag: the touch bar traded below the level and closed above it (strictly); False without a level."""
    return math.isfinite(level) and low_j < level < close_j


def acceptance_hours(closes: np.ndarray, mid: float, per: int = 4) -> int | None:
    """A: number of the touch bar's 1h sub-bars that closed above ``mid``; None unless all ``per`` closes are given
    (finite). Reads only the touch bar's own hours (all closed by the v4 entry at its close)."""
    c = np.asarray(closes, dtype=float)
    if c.size != per or not np.all(np.isfinite(c)):
        return None
    return int(np.sum(c > mid))
