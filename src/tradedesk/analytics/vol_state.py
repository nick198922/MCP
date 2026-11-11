"""Market volatility state at a signal: realized volatility of hourly bars and its trailing percentile.

IDEA-20261030-demo-setups-depth, item «S14, стан волатильності ринку як вага» (registration «Попередня реєстрація —
S14, стан волатильності»). Pure functions, no I/O; times are bar open times in ms.

* ``log_returns``: ln(c[k] / c[k-1]) when bar k directly follows bar k-1, else NaN (a hole gives no return).
* ``realized_vol``: annualized realized volatility at the close of each bar from the returns of the bars opened in
  (t[k] - window * bar, t[k]], defined when at least ``min_count`` of them exist. Decimal (0.45 = 45 %).
* ``trailing_percentile``: mid-rank percentile of x[k] among the defined x[j] of strictly earlier bars within the
  trailing window (t[k] - window_ms <= t[j] < t[k]); 0 = lowest, 1 = highest.
* ``value_at``: the value of the last bar that has closed at or before a query time (no lookahead).
"""

from __future__ import annotations

import numpy as np

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def log_returns(t: np.ndarray, c: np.ndarray, bar_ms: int = HOUR_MS) -> np.ndarray:
    """One-bar log returns; NaN for the first bar and for a bar that does not follow its predecessor by ``bar_ms``."""
    tv = np.asarray(t, dtype=np.int64)
    cv = np.asarray(c, dtype=float)
    r = np.full(cv.size, np.nan)
    if cv.size < 2:
        return r
    ok = (np.diff(tv) == bar_ms) & (cv[1:] > 0) & (cv[:-1] > 0)
    r[1:][ok] = np.log(cv[1:][ok] / cv[:-1][ok])
    return r


def realized_vol(
    t: np.ndarray,
    c: np.ndarray,
    window: int = 168,
    min_count: int = 160,
    bar_ms: int = HOUR_MS,
    periods_per_year: float = 8760.0,
) -> np.ndarray:
    """sqrt(periods_per_year * mean r^2) over the returns of the bars opened in (t[k] - window * bar_ms, t[k]].

    The window is in time, not in rows, so a hole shortens it instead of reaching further back. Uses bars <= k only."""
    tv = np.asarray(t, dtype=np.int64)
    r = log_returns(tv, c, bar_ms)
    ok = np.isfinite(r)
    s2 = np.concatenate([[0.0], np.cumsum(np.where(ok, r * r, 0.0))])
    cnt = np.concatenate([[0], np.cumsum(ok.astype(np.int64))])
    lo = np.searchsorted(tv, tv - window * bar_ms, side="right")  # first bar opened after t[k] - window
    hi = np.arange(tv.size) + 1
    n = cnt[hi] - cnt[lo]
    out = np.full(tv.size, np.nan)
    good = n >= min_count
    out[good] = np.sqrt(periods_per_year * (s2[hi] - s2[lo])[good] / n[good])
    return out


def trailing_percentile(t: np.ndarray, x: np.ndarray, window_ms: int, min_obs: int) -> np.ndarray:
    """Mid-rank percentile (#less + 0.5 #equal) / n of x[k] among the defined x[j] with t[k] - window_ms <= t[j] < t[k].

    NaN when x[k] is undefined or fewer than ``min_obs`` earlier values are defined. ``t`` must be increasing."""
    tv = np.asarray(t, dtype=np.int64)
    xv = np.asarray(x, dtype=float)
    lo = np.searchsorted(tv, tv - window_ms, side="left")
    hi = np.searchsorted(tv, tv, side="left")  # strictly earlier
    out = np.full(xv.size, np.nan)
    for k in range(xv.size):
        if not np.isfinite(xv[k]):
            continue
        past = xv[lo[k] : hi[k]]
        past = past[np.isfinite(past)]
        if past.size < min_obs:
            continue
        out[k] = (np.count_nonzero(past < xv[k]) + 0.5 * np.count_nonzero(past == xv[k])) / past.size
    return out


def value_at(t: np.ndarray, x: np.ndarray, query_ms: np.ndarray, bar_ms: int = HOUR_MS, max_stale_ms: int = 6 * HOUR_MS) -> np.ndarray:
    """x of the last bar whose close (t + bar_ms) is <= the query time; NaN if there is none or it closed more than
    ``max_stale_ms`` before the query."""
    tv = np.asarray(t, dtype=np.int64)
    xv = np.asarray(x, dtype=float)
    q = np.asarray(query_ms, dtype=np.int64)
    i = np.searchsorted(tv + bar_ms, q, side="right") - 1
    out = np.full(q.size, np.nan)
    ok = i >= 0
    ok[ok] = q[ok] - (tv[i[ok]] + bar_ms) <= max_stale_ms
    out[ok] = xv[i[ok]]
    return out
