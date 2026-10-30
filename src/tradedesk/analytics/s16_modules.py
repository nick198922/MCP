"""S16 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): pure helpers for the registered modules H6 (half out at
the range middle, the rest to the opposite edge), H7 (time exit without progress to the middle), H8 (target at the
edge of the range's value area), H1 (closes outside the range) and H3 (strength of the bar closing back in).
Registration: the card's section «Попередня реєстрація модулів S16».

Everything works in the long price space (a short is simulated on mirrored bars, as ``simulate_stop_rule`` does). No
I/O; times are parameters (ms UTC). Every feature depends only on bars closed at or before the entry (the close of the
return bar), which the tests check by changing later data.
"""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, simulate_stop_rule

HOUR_MS = 3_600_000
NO_BE = StopRule("none")
VA_SHARE = 0.70


# ---------------------------------------------------------------- H6: half out at a fixed price, the rest further


def half_exit(
    path: Bars, t_in: int, entry: float, stop: float, fix: float, far: float, hold_ms: int, fees: Fees
) -> tuple[float, Outcome | None, Outcome | None]:
    """H6: half 1 targets ``fix`` (limit, maker) with the base stop and no break-even; half 2 keeps the base stop,
    targets ``far`` and moves to break-even (with fees, from the next bar) after the first high >= ``fix``. Same risk
    unit for both halves. Returns (R of the position, half 1, half 2); NaN without a path."""
    if not (stop < entry < fix < far):
        raise ValueError("need stop < entry < fix < far")
    h1 = simulate_stop_rule(path, t_in, entry, stop, fix, hold_ms, NO_BE, fees=fees)
    h2 = simulate_stop_rule(path, t_in, entry, stop, far, hold_ms, StopRule("H6", be_frac=(fix - entry) / (far - entry)), fees=fees)
    if h1 is None or h2 is None:
        return math.nan, h1, h2
    return 0.5 * h1.r_net + 0.5 * h2.r_net, h1, h2


# ---------------------------------------------------------------- H7: time exit without progress to the middle


def window_high(path: Bars, t0: int, t1: int) -> float:
    """Highest high of the 1h bars opening in [t0, t1); NaN if none."""
    i0, i1 = int(np.searchsorted(path.t, t0)), int(np.searchsorted(path.t, t1))
    return float(np.max(path.h[i0:i1])) if i1 > i0 else math.nan


def time_exit(
    path: Bars, t_in: int, entry: float, stop: float, target: float, mid: float, n_ms: int, rule: StopRule, fees: Fees, base: Outcome
) -> tuple[float, bool, bool]:
    """H7: if the trade is still open ``n_ms`` after the entry and no 1h high in that window reached ``mid``, exit at
    the close of the window's last 1h bar (taker); otherwise the base outcome. Until ``mid`` is touched the base and
    the module follow the same path (the base's break-even trigger lies at or above ``mid``). Returns (R, alive at the
    deadline, fired)."""
    short = simulate_stop_rule(path, t_in, entry, stop, target, n_ms, rule, fees=fees)
    if short is None or short.exit_kind != "time":
        return base.r_net, False, False
    hi = window_high(path, t_in, t_in + n_ms)
    if np.isfinite(hi) and hi < mid:
        return short.r_net, True, True
    return base.r_net, True, False


def placebo_hours(u: float, n_ms: int) -> int:
    """Random exit moment of the H7 placebo, in whole hours after the entry: ceil(u * window hours), at least 1."""
    n_h = n_ms // HOUR_MS
    return int(min(n_h, max(1, math.ceil(u * n_h))))


# ---------------------------------------------------------------- H8: value area of the range


def volume_profile(low: np.ndarray, high: np.ndarray, vol: np.ndarray, bin_w: float, max_bins: int = 2000) -> tuple[np.ndarray, np.ndarray]:
    """Bin edges from min(low) to max(high) in steps of ``bin_w`` (the last bin truncated at the top) and the volume
    per bin, each bar's volume spread uniformly over its [low, high] (all of it into one bin if low == high)."""
    lo, hi = float(np.min(low)), float(np.max(high))
    nb = max(1, min(max_bins, math.ceil((hi - lo) / bin_w - 1e-12)))
    edges = lo + bin_w * np.arange(nb + 1, dtype=float)
    edges[-1] = hi
    out = np.zeros(nb)
    for lo_b, hi_b, v in zip(low, high, vol, strict=True):
        if not (np.isfinite(v) and v > 0):
            continue
        if hi_b <= lo_b:
            j = min(nb - 1, max(0, int(np.searchsorted(edges, lo_b, side="right")) - 1))
            out[j] += v
            continue
        ov = np.clip(np.minimum(edges[1:], hi_b) - np.maximum(edges[:-1], lo_b), 0.0, None)
        out += v * ov / (hi_b - lo_b)
    return edges, out


def value_area(edges: np.ndarray, vol: np.ndarray, share: float = VA_SHARE) -> tuple[float, float, float]:
    """(VAL, POC price, VAH): from the largest bin (first on ties) add the neighbouring bin with more volume (above on
    ties) until ``share`` of the volume; VAL / VAH are the outer edges. NaN without volume."""
    tot = float(np.sum(vol))
    if not tot > 0:
        return math.nan, math.nan, math.nan
    p = int(np.argmax(vol))
    a = b = p
    acc = float(vol[p])
    while acc < share * tot - 1e-12 * tot:
        up = float(vol[b + 1]) if b + 1 < vol.size else -1.0
        dn = float(vol[a - 1]) if a - 1 >= 0 else -1.0
        if up < 0 and dn < 0:
            break
        if up >= dn:
            b += 1
            acc += up
        else:
            a -= 1
            acc += dn
    return float(edges[a]), 0.5 * float(edges[p] + edges[p + 1]), float(edges[b + 1])


def range_vah(path: Bars, vol: np.ndarray, t0: int, t1: int, bin_w: float) -> float:
    """VAH (long space) of the 1h bars opening in [t0, t1) with their quote volume ``vol`` (aligned with ``path``)."""
    i0, i1 = int(np.searchsorted(path.t, t0)), int(np.searchsorted(path.t, t1))
    if i1 <= i0 or not (np.isfinite(bin_w) and bin_w > 0):
        return math.nan
    edges, v = volume_profile(path.low[i0:i1], path.h[i0:i1], vol[i0:i1], bin_w)
    return value_area(edges, v)[2]


# ---------------------------------------------------------------- H1 / H3: features of the deviation


def strong_return(o: float, h: float, low: float, c: float, edge: float, atr: float) -> tuple[bool, bool, bool]:
    """H3 conditions on the bar closing back in the range (long space): body c - o >= 0.5 ATR; close in the upper third
    of the bar ((c - low) >= 2/3 (h - low)); close >= edge + 0.3 ATR."""
    if not (np.isfinite(atr) and atr > 0):
        return False, False, False
    return c - o >= 0.5 * atr, (c - low) >= (2.0 / 3.0) * (h - low), c >= edge + 0.3 * atr


def depth_atr(edge: float, extreme: float, atr: float) -> float:
    """Depth of the deviation below the edge in ATR (the H2 control basket)."""
    return (edge - extreme) / atr if np.isfinite(atr) and atr > 0 else math.nan
