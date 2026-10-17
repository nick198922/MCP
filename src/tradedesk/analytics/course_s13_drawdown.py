"""Course setup S13 (IDEA-20261016-course-setups-crypto, step 6): buying BTC after a drawdown from its maximum.

Descriptive event study on daily closes. Everything works on a contiguous **daily grid** (index = calendar day from the
first day of data); a missing day is ``NaN``. The drawdown from the running maximum (or a trailing local maximum) and
the event days are causal: the value on day ``t`` uses closes ``<= t`` only (``tests/test_course_s13_drawdown.py``
checks that changing later closes does not change them). Forward returns look ahead by design.

Returns are decimal fractions (0.10 = 10 %) on notional, which for an unlevered spot hold is also on capital. The cost
``c`` is per side as a fraction of each side's notional: ``net = gross - c * (1 + exit / entry)``.

The main test compares the mean forward return after the events with the mean over all eligible days ("a random
day") and takes its p-value from a **circular-shift placebo**: the event indicator is rotated against the return series
by every shift of at least ``min_shift`` days, which keeps the number of events and their spacing (hence the overlap of
their forward windows) and so accounts for overlapping horizons.
"""

from __future__ import annotations

from typing import Any

import numpy as np

__all__ = [
    "ath_cross_events",
    "cell_stats",
    "cluster_bootstrap_mean",
    "drawdown",
    "episodes",
    "forward_return",
    "local_cross_events",
    "net_return",
    "path_after",
    "rolling_max",
    "running_max",
    "shift_null",
    "sma",
    "state_days",
]


def running_max(c: np.ndarray) -> np.ndarray:
    """Running maximum of the closes (``NaN`` days skipped; ``NaN`` before the first close)."""
    return np.fmax.accumulate(np.asarray(c, dtype=float))


def rolling_max(c: np.ndarray, window: int) -> np.ndarray:
    """Maximum of the closes over the trailing ``window`` calendar days including ``t`` (``NaN`` days skipped)."""
    x = np.where(np.isfinite(c), c, -np.inf)
    pad = np.concatenate([np.full(window - 1, -np.inf), x])
    out = np.lib.stride_tricks.sliding_window_view(pad, window).max(axis=1)
    return np.where(np.isfinite(out), out, np.nan)


def drawdown(c: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """``c / ref - 1`` (<= 0 when ``ref`` is a maximum that includes ``c``)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.asarray(c, dtype=float) / ref - 1.0


def sma(c: np.ndarray, n: int, min_valid: int | None = None) -> np.ndarray:
    """Simple mean of the closes over the trailing ``n`` calendar days (needs ``min_valid`` closes, default ``n - 5``)."""
    need = n - 5 if min_valid is None else min_valid
    ok = np.isfinite(c)
    cs = np.concatenate([[0.0], np.cumsum(np.where(ok, c, 0.0))])
    cn = np.concatenate([[0], np.cumsum(ok)])
    idx = np.arange(len(c))
    lo = np.maximum(idx + 1 - n, 0)
    s, k = cs[idx + 1] - cs[lo], cn[idx + 1] - cn[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        out = s / k
    return np.where((k >= need) & (idx + 1 >= n), out, np.nan)


def ath_cross_events(c: np.ndarray, theta: float, start: int = 0) -> np.ndarray:
    """Days on which the drawdown from the running maximum first crosses ``-theta`` in each max-to-new-max episode.

    A crossing needs the previous available day above ``-theta`` and the day itself at or below it. After an event the
    next one needs a new running maximum first. Days before ``start`` neither count nor use up the first event."""
    m = running_max(c)
    dd = drawdown(c, m)
    out: list[int] = []
    armed, prev = True, np.nan
    for t in range(len(c)):
        if not np.isfinite(c[t]):
            continue
        if t >= start:
            if c[t] >= m[t]:
                armed = True
            if armed and dd[t] <= -theta and prev > -theta:
                out.append(t)
                armed = False
        prev = dd[t]
    return np.asarray(out, dtype=np.int64)


def local_cross_events(c: np.ndarray, theta: float, window: int = 90, rearm: float = 0.5, cooldown: int = 30, start: int = 0) -> np.ndarray:
    """Crossings of ``-theta`` by the drawdown from the trailing ``window``-day maximum.

    After an event the rule re-arms once the drawdown recovers above ``-theta * rearm``, and a new event must be at
    least ``cooldown`` days after the previous one."""
    dd = drawdown(c, rolling_max(c, window))
    out: list[int] = []
    armed, prev, last = True, np.nan, -(10**9)
    for t in range(len(c)):
        if not np.isfinite(c[t]):
            continue
        if t >= start:
            if not armed and dd[t] > -theta * rearm:
                armed = True
            if armed and dd[t] <= -theta and prev > -theta and t - last >= cooldown:
                out.append(t)
                armed, last = False, t
        prev = dd[t]
    return np.asarray(out, dtype=np.int64)


def state_days(c: np.ndarray, theta: float, start: int = 0) -> np.ndarray:
    """All days with a drawdown from the running maximum at or below ``-theta`` (not just the first crossing)."""
    dd = drawdown(c, running_max(c))
    idx = np.flatnonzero(np.isfinite(dd) & (dd <= -theta))
    return idx[idx >= start]


def _last_valid(c: np.ndarray) -> np.ndarray:
    ok = np.isfinite(c)
    lv = np.where(ok, np.arange(len(c)), -1)
    return np.maximum.accumulate(lv)


def forward_return(c: np.ndarray, h: int, max_gap: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Gross return from the close of ``t`` to the close of ``t + h`` and the exit/entry ratio.

    If day ``t + h`` is missing the last available close at most ``max_gap`` days earlier is used; otherwise (or past
    the end of the data, or when ``t`` itself is missing) the result is ``NaN``."""
    n = len(c)
    g = np.full(n, np.nan)
    if h >= n:
        return g, g.copy()
    lv = _last_valid(c)
    t = np.arange(n - h)
    j = lv[t + h]
    ok = np.isfinite(c[t]) & (j >= t + h - max_gap) & (j > t)
    g[t[ok]] = c[j[ok]] / c[t[ok]] - 1.0
    return g, 1.0 + g


def net_return(gross: np.ndarray, cost: float) -> np.ndarray:
    """``gross - cost * (1 + exit / entry)`` with ``cost`` per side (0.0012 = 0.12 %)."""
    return gross - cost * (2.0 + gross)


def path_after(c: np.ndarray, t: int, h: int, level: float) -> tuple[float, bool]:
    """(worst close in ``t+1 … t+h`` relative to the close of ``t`` minus 1, whether a close reached ``level``)."""
    w = c[t + 1 : t + h + 1]
    w = w[np.isfinite(w)]
    if w.size == 0:
        return float("nan"), False
    return float(w.min() / c[t] - 1.0), bool(w.max() >= level)


def episodes(days: np.ndarray, h: int) -> np.ndarray:
    """Cluster labels for sorted event days: a new cluster starts when the gap to the previous event is ``>= h``
    (their ``[t, t + h]`` windows no longer overlap)."""
    d = np.asarray(days, dtype=np.int64)
    if d.size == 0:
        return np.zeros(0, dtype=np.int64)
    return np.concatenate([[0], np.cumsum(np.diff(d) >= h)])


def cluster_bootstrap_mean(values: np.ndarray, clusters: np.ndarray, n_boot: int = 10_000, seed: int = 20261017) -> dict[str, Any]:
    """Mean with a 95 % CI from resampling whole clusters; ``ci`` is ``None`` with fewer than two clusters."""
    v = np.asarray(values, dtype=float)
    cl = np.asarray(clusters)
    if v.size == 0:
        return {"mean": float("nan"), "ci": None, "se": None}
    u, inv = np.unique(cl, return_inverse=True)
    if u.size < 2:
        return {"mean": float(v.mean()), "ci": None, "se": None}
    sums = np.bincount(inv, weights=v)
    cnt = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, u.size, size=(n_boot, u.size))
    bm = sums[pick].sum(axis=1) / cnt[pick].sum(axis=1)
    return {"mean": float(v.mean()), "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))), "se": float(bm.std())}


def shift_null(values: np.ndarray, event_pos: np.ndarray, min_shift: int) -> np.ndarray:
    """Means of ``values`` at the event positions rotated by every shift ``s`` with ``min(s, N - s) >= min_shift``."""
    v = np.asarray(values, dtype=float)
    n = v.size
    shifts = np.arange(min_shift, n - min_shift + 1)
    e = np.asarray(event_pos, dtype=np.int64)
    if e.size == 0 or shifts.size == 0:
        return np.zeros(0)
    out = np.empty(shifts.size)
    for k0 in range(0, shifts.size, 512):  # chunks keep the (events x shifts) index matrix small
        s = shifts[k0 : k0 + 512]
        out[k0 : k0 + s.size] = v[(e[:, None] + s[None, :]) % n].mean(axis=0)
    return out


def cell_stats(
    net: np.ndarray,
    events: np.ndarray,
    eligible: np.ndarray,
    h: int,
    *,
    min_shift: int = 90,
    n_boot: int = 10_000,
    seed: int = 20261017,
) -> dict[str, Any]:
    """Event mean against the all-eligible-days baseline for one (threshold, horizon) cell.

    ``net`` — forward net return on the daily grid; ``events`` — event days (grid indices); ``eligible`` — boolean grid
    mask of the period (days with a finite ``net`` only are used). Returns n, episodes, the event mean with an
    episode-bootstrap CI, the baseline, delta, the one-sided circular-shift p, SE and MDE (2.8 SE) of the placebo, and
    a CI for delta from inverting the placebo distribution."""
    ok = eligible & np.isfinite(net)
    pos_of = np.full(len(net), -1, dtype=np.int64)
    days = np.flatnonzero(ok)
    pos_of[days] = np.arange(days.size)
    ev = np.asarray(events, dtype=np.int64)
    ev = np.sort(ev[(ev >= 0) & (ev < len(net))])
    ev = ev[ok[ev]]
    vals = net[days]
    base = float(vals.mean()) if vals.size else float("nan")
    out: dict[str, Any] = {"n": int(ev.size), "n_ep": 0, "base": base, "n_days": int(days.size), "days": ev}
    if ev.size == 0:
        out.update(
            mean=float("nan"), ci=None, delta=float("nan"), p=None, se=None, mde=None, ci_delta=None, median=float("nan"), win=float("nan")
        )
        return out
    ret = net[ev]
    cl = episodes(ev, h)
    bs = cluster_bootstrap_mean(ret, cl, n_boot, seed)
    delta = float(ret.mean()) - base
    null = shift_null(vals, pos_of[ev], min_shift) - base
    out.update(
        n_ep=int(cl.max() + 1),
        mean=float(ret.mean()),
        ci=bs["ci"],
        median=float(np.median(ret)),
        win=float((ret > 0).mean()),
        delta=delta,
    )
    if null.size:
        out.update(
            p=float((1 + np.sum(null >= delta)) / (1 + null.size)),
            se=float(null.std()),
            mde=float(2.8 * null.std()),
            ci_delta=(float(delta - np.quantile(null, 0.975)), float(delta - np.quantile(null, 0.025))),
            n_shift=int(null.size),
        )
    else:
        out.update(p=None, se=None, mde=None, ci_delta=None)
    return out
