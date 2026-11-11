"""Perp premium state on a sweep candle (IDEA-20261030-demo-setups-depth, item «S3, премія перпа на свічці свипу»).

Pure functions, no I/O; time is a parameter (epoch ms). The premium index is a decimal: 0.001 means the perp's impact
price is 0.1 % above the spot index (Binance ``premiumIndexKlines``). Every series here is hourly and keyed by the
bar's open time; a bar opening at ``t`` covers ``[t, t + 1 h)``.

The feature of a sweep candle ``[start, start + n_hours h)``:

1. the wick hour: the 1h price bar holding the sweep extreme (lowest low for a long, highest high for a short);
2. the premium statistic of that hour: the premium low for a long, the premium high for a short (``stat="extreme"``),
   or the premium close of that hour (``stat="close"``);
3. its z-score against the same statistic of the bars opening in the ``window_ms`` strictly before the wick hour
   (mean / population SD; ``robust=True``: median / 1.4826 MAD), or the raw level (``mode="level"``);
4. signed by the side: ``z`` for a long, ``-z`` for a short. A low signed value means the perp was pushed beyond spot in
   the sweep's direction (below for a long sweep of the low, above for a short sweep of the high).

Everything read lies inside the sweep candle or before it, so the value is known at the candle's close (the entry).
"""

from __future__ import annotations

import math

import numpy as np

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WINDOW_MS = 30 * DAY_MS
MIN_PRIOR = 648  # 90 % of the 720 hourly bars of 30 days
MAD_SCALE = 1.4826


def wick_hour(t1h: np.ndarray, low1h: np.ndarray, high1h: np.ndarray, start_ms: int, n_hours: int, side: str) -> int:
    """Index of the 1h price bar with the sweep extreme inside ``[start_ms, start_ms + n_hours h)``.

    Lowest low for ``side == "long"``, highest high for ``"short"`` (first bar on ties). ``t1h`` is sorted and unique.
    Returns -1 unless all ``n_hours`` bars of the candle are present."""
    i = int(np.searchsorted(t1h, start_ms, side="left"))
    j = i + n_hours
    if j > t1h.size or int(t1h[i]) != start_ms or int(t1h[j - 1]) != start_ms + (n_hours - 1) * HOUR_MS:
        return -1
    if side == "long":
        return i + int(np.argmin(low1h[i:j]))
    if side == "short":
        return i + int(np.argmax(high1h[i:j]))
    raise ValueError(f"side must be long or short, got {side!r}")


def trailing_z(
    t: np.ndarray, v: np.ndarray, at_ms: int, *, window_ms: int = WINDOW_MS, min_n: int = MIN_PRIOR, robust: bool = False
) -> float:
    """z of the bar opening at ``at_ms`` against the bars opening in ``[at_ms - window_ms, at_ms)``.

    ``t`` sorted and unique. Mean / population SD (``robust``: median / 1.4826 MAD). NaN if the bar at ``at_ms`` is
    missing or not finite, fewer than ``min_n`` earlier values are finite, or the spread is 0."""
    k = int(np.searchsorted(t, at_ms, side="left"))
    if k >= t.size or int(t[k]) != at_ms or not math.isfinite(float(v[k])):
        return math.nan
    a = int(np.searchsorted(t, at_ms - window_ms, side="left"))
    w = v[a:k]
    w = w[np.isfinite(w)]
    if w.size < min_n:
        return math.nan
    if robust:
        mid = float(np.median(w))
        sd = MAD_SCALE * float(np.median(np.abs(w - mid)))
    else:
        mid = float(w.mean())
        sd = float(w.std())
    if not sd > 0:
        return math.nan
    return (float(v[k]) - mid) / sd


def sweep_premium(
    price: tuple[np.ndarray, np.ndarray, np.ndarray],
    prem: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    start_ms: int,
    n_hours: int,
    side: str,
    *,
    stat: str = "extreme",
    mode: str = "z",
    shift_ms: int = 0,
    window_ms: int = WINDOW_MS,
    min_n: int = MIN_PRIOR,
) -> float:
    """Signed premium feature of one sweep candle (see the module docstring); NaN if undefined.

    ``price`` = (t, low, high) of the perp's 1h bars; ``prem`` = (t, high, low, close) of the 1h premium bars.
    ``mode``: ``"z"``, ``"robust"`` (median / MAD z) or ``"level"`` (the raw premium, signed). ``shift_ms`` reads the
    premium series at the wick hour + shift (placebo); the wick hour itself always comes from the real candle."""
    pt, plow, phigh = price
    w = wick_hour(pt, plow, phigh, start_ms, n_hours, side)
    if w < 0:
        return math.nan
    qt, qh, ql, qc = prem
    if stat == "extreme":
        v = ql if side == "long" else qh
    elif stat == "close":
        v = qc
    else:
        raise ValueError(f"stat must be extreme or close, got {stat!r}")
    at = int(pt[w]) + shift_ms
    sign = 1.0 if side == "long" else -1.0
    if mode == "level":
        k = int(np.searchsorted(qt, at, side="left"))
        if k >= qt.size or int(qt[k]) != at or not math.isfinite(float(v[k])):
            return math.nan
        return sign * float(v[k])
    if mode not in ("z", "robust"):
        raise ValueError(f"mode must be z, robust or level, got {mode!r}")
    return sign * trailing_z(qt, v, at, window_ms=window_ms, min_n=min_n, robust=mode == "robust")


def implied_funding(p_avg: float, interest: float = 0.0001, clamp: float = 0.0005) -> float:
    """Binance USDT-M funding of one 8h interval from the average premium: P + clamp(I - P, -0.05 %, +0.05 %)."""
    return p_avg + min(max(interest - p_avg, -clamp), clamp)
