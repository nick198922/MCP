"""BTC 15-minute shock -> lagging-altcoin continuation study (IDEA-20261025-btc-shock-alt-lag).

Pure functions, no I/O. Time is a parameter (epoch milliseconds, UTC). Returns are decimal fractions on notional
(0.01 = 1 %). All series live on one regular 5-minute grid; a missing bar is NaN and is never interpolated.

Pre-registered definitions (see the idea card, "Попередня реєстрація"):
  r15(i)   = close[i] / close[i - 3] - 1 (shock bars i-2, i-1, i);
  sigma(i) = sample SD of r15 over bars [i - window, i - 1] (strictly before the event bar), >= min_frac valid;
  event    = first bar with |r15| >= ret_threshold and |r15| >= z_threshold * sigma, then nothing until ts_i + cooldown;
  beta     = OLS slope of alt returns on BTC returns over bars [i - window, i - 1] (closes sampled every ``step`` bars);
  residual = s * (r_alt - beta * r_btc), both returns over the same post-entry window, s = sign of the BTC shock.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

FIVE_MIN_MS = 300_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
BARS_30D = 30 * 24 * 12


def lag_return(close: np.ndarray, k: int) -> np.ndarray:
    """r[i] = close[i] / close[i - k] - 1; NaN for i < k or when either close is missing."""
    c = np.asarray(close, dtype=float)
    out = np.full(c.shape, np.nan)
    if c.size > k:
        out[k:] = c[k:] / c[:-k] - 1.0
    return out


def prev_window_std(x: np.ndarray, window: int, min_count: int) -> np.ndarray:
    """out[i] = sample SD (ddof=1) of the valid x[j], j in [i - window, i - 1]; NaN with fewer than ``min_count``.

    Strictly before i: out[i] never depends on x[i:]."""
    x = np.asarray(x, dtype=float)
    ok = ~np.isnan(x)
    v = np.where(ok, x, 0.0)
    c0 = np.concatenate([[0.0], np.cumsum(ok.astype(float))])
    c1 = np.concatenate([[0.0], np.cumsum(v)])
    c2 = np.concatenate([[0.0], np.cumsum(v * v)])
    idx = np.arange(x.size)
    lo = np.maximum(idx - window, 0)
    n = c0[idx] - c0[lo]
    s1 = c1[idx] - c1[lo]
    s2 = c2[idx] - c2[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (s2 - s1 * s1 / n) / (n - 1)
    var = np.where(var < 0, 0.0, var)
    out = np.sqrt(var)
    out[(n < max(2, min_count))] = np.nan
    return out


def detect_shocks(
    ts: np.ndarray,
    close: np.ndarray,
    *,
    k: int = 3,
    ret_threshold: float = 0.012,
    z_threshold: float = 4.0,
    window: int = BARS_30D,
    min_frac: float = 0.8,
    cooldown_ms: int = 2 * HOUR_MS,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Shock events on a regular grid (``ts`` = bar opens, ``close`` with NaN gaps), in time order.

    An event at bar i uses only closes up to bar i; the cooldown counts from the accepted event's bar open."""
    ts = np.asarray(ts, dtype=np.int64)
    r = lag_return(close, k)
    sig = prev_window_std(r, window, int(min_frac * window))
    with np.errstate(invalid="ignore"):
        cond = (np.abs(r) >= ret_threshold) & (np.abs(r) >= z_threshold * sig)
    cand = np.flatnonzero(cond)
    events: list[dict[str, Any]] = []
    next_ok = -(2**62)
    for i in cand:
        t = int(ts[i])
        if start_ms is not None and t < start_ms:
            continue
        if end_ms is not None and t >= end_ms:
            break
        if t < next_ok:
            continue
        events.append({"i": int(i), "ts": t, "r15": float(r[i]), "sigma": float(sig[i]), "sign": 1 if r[i] > 0 else -1})
        next_ok = t + cooldown_ms
    return events


def beta_from_closes(btc_close: np.ndarray, alt_close: np.ndarray, *, step: int = 1, min_count: int = 2) -> float | None:
    """OLS slope of alt returns on BTC returns from two aligned close windows.

    Closes are sampled every ``step`` bars counting back from the last one (step=1: 5-minute returns; step=12:
    non-overlapping 1-hour returns). Pairs with a missing return on either side are dropped; None with fewer than
    ``min_count`` pairs or zero BTC variance."""
    xb = np.asarray(btc_close, dtype=float)[::-1][::step][::-1]
    ya = np.asarray(alt_close, dtype=float)[::-1][::step][::-1]
    if xb.size < 2:
        return None
    x = xb[1:] / xb[:-1] - 1.0
    y = ya[1:] / ya[:-1] - 1.0
    ok = ~(np.isnan(x) | np.isnan(y))
    if int(ok.sum()) < max(2, min_count):
        return None
    x, y = x[ok], y[ok]
    dx = x - x.mean()
    vx = float(dx @ dx)
    if vx <= 0:
        return None
    return float(dx @ (y - y.mean()) / vx)


def beta_window(i: int, window: int) -> slice:
    """Bars ``[i - window, i)`` for the pre-event beta: strictly before the event bar ``i`` (bar i itself excluded)."""
    if window < 1 or i < window:
        raise ValueError("beta window does not fit before bar i")
    return slice(i - window, i)


def post_entry_return(open_: np.ndarray, i: int, entry_lag: int, n_bars: int) -> float:
    """open[i + entry_lag + n_bars] / open[i + entry_lag] - 1: enter at the open ``entry_lag`` bars after the event bar
    ``i``, exit ``n_bars`` bars later at the open. Only those two opens are read; NaN if either is missing or the exit
    falls off the array."""
    if entry_lag < 1 or n_bars < 1:
        raise ValueError("entry_lag and n_bars must be >= 1 (the signal is known only at the close of bar i)")
    a, b = i + entry_lag, i + entry_lag + n_bars
    if i < 0 or b >= len(open_):
        return float("nan")
    en, ex = float(open_[a]), float(open_[b])
    if not (math.isfinite(en) and math.isfinite(ex)) or en <= 0:
        return float("nan")
    return ex / en - 1.0


def max_abs_log_jump(close: np.ndarray, lo: int, hi: int) -> float:
    """Largest |log(c[k+1] / c[k])| over adjacent bars inside ``close[lo:hi]`` (pairs with a missing side skipped);
    NaN if no pair. A value above log(1.5) flags a probable bad print or ticker re-use."""
    c = np.asarray(close[max(lo, 0) : hi], dtype=float)
    if c.size < 2:
        return float("nan")
    with np.errstate(invalid="ignore", divide="ignore"):
        d = np.abs(np.diff(np.log(c)))
    d = d[np.isfinite(d)]
    return float(d.max()) if d.size else float("nan")


def winsorize(x: np.ndarray, lo_q: float = 0.01, hi_q: float = 0.99) -> np.ndarray:
    """Clip finite values to their ``lo_q``/``hi_q`` quantiles; NaN stays NaN."""
    x = np.asarray(x, dtype=float)
    ok = np.isfinite(x)
    if not ok.any():
        return x.copy()
    lo, hi = np.quantile(x[ok], [lo_q, hi_q])
    return np.where(ok, np.clip(x, lo, hi), np.nan)


def directional_residual(sign: int, r_alt: float, r_btc: float, beta: float) -> float:
    """s * (r_alt - beta * r_btc): the alt's move in the shock direction that BTC's own move does not explain."""
    return sign * (r_alt - beta * r_btc)


def tercile(rank: int, n: int = 60) -> str:
    """'high' (most liquid), 'mid' or 'low' for a 1-based turnover rank within a top-``n`` list."""
    if not 1 <= rank <= n:
        raise ValueError("rank out of range")
    third = n / 3
    if rank <= third:
        return "high"
    if rank <= 2 * third:
        return "mid"
    return "low"


def cluster_se(values: Sequence[float], clusters: Sequence[Any]) -> float:
    """Cluster-robust standard error of the pooled mean (CR1: sqrt(G / (G - 1)) small-sample factor)."""
    v = np.asarray(values, dtype=float)
    if v.size == 0:
        return math.nan
    m = float(v.mean())
    sums: dict[Any, float] = {}
    for x, c in zip(v, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + (float(x) - m)
    g = len(sums)
    if g < 2:
        return math.nan
    ss = sum(s * s for s in sums.values())
    return math.sqrt(ss * g / (g - 1)) / v.size


def mde(se: float, factor: float = 2.8) -> float:
    """Minimum detectable effect at 5 % two-sided / 80 % power: 2.8 standard errors."""
    return factor * se


def placebo_times(event_ts: int, all_event_ts: Sequence[int], offsets_ms: Sequence[int], *, guard_ms: int = 2 * HOUR_MS) -> list[int]:
    """event_ts + offset for each offset, keeping only times with no event within ``guard_ms``."""
    evs = np.sort(np.asarray(all_event_ts, dtype=np.int64))
    out = []
    for off in offsets_ms:
        t = int(event_ts) + int(off)
        j = int(np.searchsorted(evs, t - guard_ms, side="left"))
        if j < evs.size and evs[j] <= t + guard_ms:
            continue
        out.append(t)
    return out


def adverse_excursion(sign: int, entry: float, highs: np.ndarray, lows: np.ndarray) -> float | None:
    """Worst move against a position that bets on ``sign`` (+1 long, -1 short) over the bars, as a fraction of entry.

    Short (sign -1): max(high) / entry - 1; long: 1 - min(low) / entry. None when no bar is valid."""
    h = np.asarray(highs, dtype=float)
    lo = np.asarray(lows, dtype=float)
    if sign < 0:
        h = h[~np.isnan(h)]
        return None if h.size == 0 else float(h.max() / entry - 1.0)
    lo = lo[~np.isnan(lo)]
    return None if lo.size == 0 else float(1.0 - lo.min() / entry)
