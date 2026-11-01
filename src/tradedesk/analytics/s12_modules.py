"""S12 v4 modules (item 8/7b of IDEA-20261030-demo-setups-depth, registration «Попередня реєстрація модулів S12»).

Pure functions on bars in the *long frame* (shorts are passed as ``Bars.mirrored()``): no I/O, every value uses only
the bars up to the moment named in its docstring.

- H1 ``clv``: where the touch bar closed in its range.
- H2 ``early_entry``: the first 1h close above the gap's middle in the first three hours of the touch bar.
- H3 ``gap_atr``: the gap's width in ATR14 of its third candle.
- H5 ``edge_touch``: bars after the gap that entered it without reaching the middle, before the touch bar.
- H8 ``dry``: mean quote volume per bar of the pullback (after the gap's third candle through the touch bar) over the
  mean of the gap's three candles.
- H2 event set ``h2_in_set`` / ``h2_event`` / ``h2_delta``: fixed at the early-entry time t2 (list, guard, free coin
  at t2); the v4 trade's membership in V (read from the 4h close) only picks the baseline Δ subtracts.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.s28_modules import ranks

HOUR_MS = 3_600_000


def clv(high: float, low: float, close: float) -> float:
    """H1: (close - low) / (high - low) of the touch bar; NaN for a bar without range."""
    rng = high - low
    return (close - low) / rng if rng > 0 else math.nan


def gap_atr(near: float, full: float, atr: float) -> float:
    """H3: (near edge - far edge) / ATR14 of the gap's third candle (near = low of candle 3, far = high of candle 1)."""
    return (near - full) / atr if atr > 0 else math.nan


def edge_touch(low: np.ndarray, i: int, j: int, near: float) -> int:
    """H5: bars i+1 .. j-1 whose low reached the gap's near edge (``low[k] <= near``). Before the first test of the
    middle on bar j none of them reached the middle, so these are entries into the gap that did not reach it. Reads
    bars i+1 .. j-1 only."""
    if j <= i + 1:
        return 0
    return int(np.sum(np.asarray(low[i + 1 : j]) <= near))


def dry(qv: np.ndarray, i: int, j: int) -> float:
    """H8: mean quote volume per bar over i+1 .. j (the pullback, the touch bar included) / mean over i-2 .. i (the
    gap's three candles); NaN if either is missing or the impulse volume is not positive. Reads bars i-2 .. j."""
    if i < 2 or j <= i:
        return math.nan
    imp = float(np.mean(qv[i - 2 : i + 1]))
    pull = float(np.mean(qv[i + 1 : j + 1]))
    if not (math.isfinite(imp) and math.isfinite(pull)) or imp <= 0:
        return math.nan
    return pull / imp


def bar_volume(h_t: np.ndarray, h_qv: np.ndarray, bar_t: np.ndarray, bar_ms: int) -> np.ndarray:
    """Quote volume of each bar opening at ``bar_t`` (length ``bar_ms``): the sum of the 1h volumes inside it."""
    out = np.zeros(bar_t.size)
    if h_t.size == 0 or bar_t.size == 0:
        return out
    k = np.searchsorted(bar_t, h_t, side="right") - 1
    ok = (k >= 0) & (h_t < bar_t[np.clip(k, 0, None)] + bar_ms)
    np.add.at(out, k[ok], h_qv[ok])
    return out


def touch_hours(path: Bars, bar_open: int, bar_ms: int) -> int:
    """Index in ``path`` (1h) of the first hour of the bar opening at ``bar_open``; -1 unless all its hours exist."""
    per = bar_ms // HOUR_MS
    k0 = int(np.searchsorted(path.t, bar_open))
    if k0 + per > len(path):
        return -1
    want = bar_open + HOUR_MS * np.arange(per)
    return k0 if np.array_equal(path.t[k0 : k0 + per], want) else -1


def first_touch_hour(path: Bars, k0: int, per: int, mid: float) -> int:
    """First hour k0 .. k0+per-1 whose low reaches ``mid``; -1 if none."""
    hit = np.flatnonzero(path.low[k0 : k0 + per] <= mid)
    return k0 + int(hit[0]) if hit.size else -1


def early_entry(path: Bars, bar_open: int, bar_ms: int, mid: float, stop: float, target: float) -> tuple[int, float] | None:
    """H2: (1h index m, entry price) of the first hour m >= h (h: the first hour of the touch bar whose low reaches
    the middle) among the first ``per - 1`` hours of the touch bar whose close is above ``mid``; entry at that close.
    None if the hours are incomplete, no such hour exists (the entry stays v4's 4h close), an hour h .. m touched the
    stop, or the close is at / above the target. Reads hours up to m only."""
    per = bar_ms // HOUR_MS
    k0 = touch_hours(path, bar_open, bar_ms)
    if k0 < 0:
        return None
    h = first_touch_hour(path, k0, per, mid)
    if h < 0:
        return None
    for m in range(h, k0 + per - 1):
        if path.low[m] <= stop:
            return None
        c = float(path.c[m])
        if c > mid:
            return (m, c) if stop < c < target else None
    return None


def h2_in_set(passes_at_t2: bool, coin_free_at_t2: bool) -> bool:
    """H2 event-inclusion rule, fixed at t2 (the close of the early-entry hour m): the event is in the H2 set iff the
    early entry passes the demo list and the guard at t2 and the coin is not inside a V trade at t2. Whether the v4
    trade of the same touch bar is in V is deliberately NOT an input: V's guard reads the R:R from the 4h close, which
    is after t2 (the registered rule «(a) = v4 in V» selected events on the future)."""
    return bool(passes_at_t2) and bool(coin_free_at_t2)


def h2_delta(in_set: bool, v4_in_v: bool, r_h2: float, r_v4: float) -> float:
    """Δ of one event: R_H2 - R_v4 * [v4 in V] if the event is in the H2 set, else 0 (the fallback is the v4 entry
    itself, so the strategies coincide). ``v4_in_v`` only picks the baseline being replaced, never membership."""
    if not in_set:
        return 0.0
    return r_h2 - (r_v4 if v4_in_v else 0.0)


def h2_event(
    path: Bars, bar_open: int, bar_ms: int, mid: float, stop: float, target: float, passes_at: Callable[[int, float], bool]
) -> tuple[int, int, float] | None:
    """H2 event fixed at the early-entry time: (m, t2, entry) if ``early_entry`` exists and ``passes_at(t2, entry)``
    (demo list, guard and free coin at t2 = close of hour m; it must read nothing after t2) is true; None otherwise,
    i.e. the fallback to the v4 entry with Δ = 0. Reads hours up to m only."""
    ee = early_entry(path, bar_open, bar_ms, mid, stop, target)
    if ee is None:
        return None
    m, e = ee
    t2 = int(path.t[m]) + HOUR_MS
    return (m, t2, e) if passes_at(t2, e) else None


def entry_at_hour(path: Bars, h: int, u: int, stop: float, target: float) -> float | None:
    """H2 placebo: entry at the close of hour u (h <= u) without a condition on the close; None if an hour h .. u
    touched the stop or the close is outside (stop, target). Reads hours up to u only."""
    if u < h or np.any(path.low[h : u + 1] <= stop):
        return None
    c = float(path.c[u])
    return c if stop < c < target else None


def partial_spearman(x: Sequence[float], y: Sequence[float], z: Sequence[float]) -> float:
    """Spearman's rho of x and y with z partialled out (Pearson of the residuals of the ranks of x and y on the
    ranks of z); NaN with fewer than 4 points or no spread."""
    rx, ry, rz = (ranks(v) for v in (x, y, z))
    if rx.size < 4:
        return math.nan
    a = np.column_stack([np.ones(rz.size), rz])
    ex = rx - a @ np.linalg.lstsq(a, rx, rcond=None)[0]
    ey = ry - a @ np.linalg.lstsq(a, ry, rcond=None)[0]
    den = math.sqrt(float(ex @ ex) * float(ey @ ey))
    return float(ex @ ey) / den if den > 0 else math.nan
