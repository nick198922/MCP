"""Course setups, step 8 (IDEA-20261016-course-setups-crypto): S19 / H6 — price drift into a macro event.

H6: if BTC rose >= 3 % over the 3 days before a CPI or FOMC release, its return over the 5 days after the release is
lower than without such a rise. Pure functions, no I/O; time is always a parameter. Conventions:
  * hourly bars keyed by open time (epoch ms, sorted); the price at an arbitrary time ``t`` is the close of the last
    bar that closed at or before ``t`` (open <= t - 1 h), and only if that bar closed no earlier than ``max_age`` before
    ``t`` — so the price "at the release" is the last price before it, never a later one;
  * returns are simple decimals on notional (0.03 = 3 %);
  * clusters ("episodes") merge events whose windows [t0 - pre; t0 + post] overlap, chained.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
COST_RT = 0.0014  # taker 0.05 % + slippage 0.02 % per side, round trip

# ----------------------------------------------------------------------------- prices and windows


def price_at(open_ms: np.ndarray, close: np.ndarray, t_ms: int, *, max_age_ms: int = 3 * HOUR_MS) -> float | None:
    """Close of the last hourly bar that ended at or before ``t_ms``; None if it ended more than ``max_age_ms`` earlier."""
    i = int(np.searchsorted(open_ms, t_ms - HOUR_MS, side="right")) - 1
    if i < 0:
        return None
    end = int(open_ms[i]) + HOUR_MS
    if end < t_ms - max_age_ms:
        return None
    v = float(close[i])
    return v if v > 0 and math.isfinite(v) else None


def window_returns(
    open_ms: np.ndarray, close: np.ndarray, t0_ms: int, *, pre_h: int = 72, post_h: int = 120, delay_h: int = 0
) -> tuple[float, float] | None:
    """(pre, post): pre = P(t0)/P(t0 - pre_h) - 1, post = P(t0 + delay + post_h)/P(t0 + delay) - 1. None if a price is missing."""
    p_a = price_at(open_ms, close, t0_ms - pre_h * HOUR_MS)
    p_0 = price_at(open_ms, close, t0_ms)
    p_e = price_at(open_ms, close, t0_ms + delay_h * HOUR_MS)
    p_b = price_at(open_ms, close, t0_ms + (delay_h + post_h) * HOUR_MS)
    if p_a is None or p_0 is None or p_e is None or p_b is None:
        return None
    return p_0 / p_a - 1.0, p_b / p_e - 1.0


def pre_return(open_ms: np.ndarray, close: np.ndarray, t0_ms: int, *, pre_h: int = 72) -> float | None:
    """Only the pre-event part (uses prices up to ``t0_ms``)."""
    p_a = price_at(open_ms, close, t0_ms - pre_h * HOUR_MS)
    p_0 = price_at(open_ms, close, t0_ms)
    if p_a is None or p_0 is None:
        return None
    return p_0 / p_a - 1.0


def daily_vol(open_ms: np.ndarray, close: np.ndarray, t0_ms: int, *, days: int = 30, min_share: float = 0.8) -> float | None:
    """SD of daily log returns between 00:00 UTC closes over the ``days`` days before ``t0_ms`` (only prices <= t0)."""
    d0 = (t0_ms // DAY_MS) * DAY_MS
    px = [price_at(open_ms, close, d0 - k * DAY_MS) for k in range(days + 1)]
    rs = [math.log(px[k] / px[k + 1]) for k in range(days) if px[k] is not None and px[k + 1] is not None]
    if len(rs) < max(2, int(min_share * days)):
        return None
    return float(np.std(rs, ddof=1))


# ----------------------------------------------------------------------------- calendars


def episodes(t0_ms: Sequence[int], pre_ms: int, post_ms: int) -> np.ndarray:
    """Cluster id per event (input order): windows [t0 - pre; t0 + post] that overlap, chained, share an id."""
    t = np.asarray(t0_ms, dtype=np.int64)
    out = np.zeros(t.size, dtype=int)
    if t.size == 0:
        return out
    order = np.argsort(t, kind="stable")
    cid, end = 0, None
    for i in order:
        s, e = int(t[i]) - pre_ms, int(t[i]) + post_ms
        if end is not None and s > end:
            cid += 1
        end = e if end is None else max(end, e)
        out[i] = cid
    return out


def placebo_days(start: date, end: date, events: Iterable[tuple[date, str]], *, main_days: int = 5, nfp_days: int = 1) -> list[date]:
    """Weekdays in [start; end] with no CPI/FOMC date within ±``main_days`` and no NFP date within ±``nfp_days``."""
    evs = list(events)
    main = {d for d, k in evs if k in ("cpi", "fomc")}
    nfp = {d for d, k in evs if k == "nfp"}
    out = []
    d = start
    one = timedelta(days=1)
    while d <= end:
        if d.weekday() < 5:
            bad = any(d + timedelta(days=k) in main for k in range(-main_days, main_days + 1)) or any(
                d + timedelta(days=k) in nfp for k in range(-nfp_days, nfp_days + 1)
            )
            if not bad:
                out.append(d)
        d += one
    return out


def shifted_days(
    days: Iterable[date], main_events: Iterable[date], shifts: Sequence[int] = (-21, -14, 14, 21), *, clear: int = 5
) -> list[tuple[date, int]]:
    """(shifted day, shift) for each event day and shift, kept only if no main event lies within ±``clear`` days of it."""
    main = set(main_events)
    out = []
    for d in days:
        for s in shifts:
            x = d + timedelta(days=s)
            if not any(x + timedelta(days=k) in main for k in range(-clear, clear + 1)):
                out.append((x, s))
    return out


# ----------------------------------------------------------------------------- statistics


def _cluster_sums(cols: Sequence[np.ndarray], clusters: Sequence[Any]) -> list[np.ndarray]:
    keys, inv = np.unique(np.asarray(clusters), return_inverse=True)
    return [np.bincount(inv, weights=np.asarray(c, float), minlength=keys.size) for c in cols]


def _boot_idx(k: int, n_boot: int, seed: int) -> Iterable[np.ndarray]:
    rng = np.random.default_rng(seed)
    chunk = max(1, 2_000_000 // max(1, k))
    for lo in range(0, n_boot, chunk):
        yield rng.integers(0, k, size=(min(n_boot, lo + chunk) - lo, k))


def group_diff(
    values: Sequence[float], in_group: Sequence[bool], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261017
) -> dict[str, Any]:
    """Δ = mean(values | group) - mean(values | not group), cluster bootstrap (whole clusters resampled).

    Returns stat, n_in, n_out, clusters_in (clusters with >= 1 in-group value), ci (2.5 / 97.5 %), se, p_neg
    (one-sided p for Δ < 0: (1 + #{Δ* >= 0}) / (1 + B)), p_pos (for Δ > 0) and the bootstrap draws."""
    x = np.asarray(values, float)
    g = np.asarray(in_group, bool)
    cl = np.asarray(clusters)
    n_in, n_out = int(g.sum()), int((~g).sum())
    res: dict[str, Any] = {"n_in": n_in, "n_out": n_out, "clusters_in": int(np.unique(cl[g]).size) if n_in else 0}
    if n_in == 0 or n_out == 0:
        res.update(stat=float("nan"), ci=None, se=float("nan"), p_neg=None, p_pos=None, draws=np.array([]))
        return res
    res["stat"] = float(x[g].mean() - x[~g].mean())
    s_in, c_in, s_out, c_out = _cluster_sums([np.where(g, x, 0.0), g.astype(float), np.where(~g, x, 0.0), (~g).astype(float)], cl)
    draws = []
    for idx in _boot_idx(s_in.size, n_boot, seed):
        a_c, b_c = c_in[idx].sum(1), c_out[idx].sum(1)
        with np.errstate(invalid="ignore", divide="ignore"):
            draws.append(s_in[idx].sum(1) / a_c - s_out[idx].sum(1) / b_c)
    d = np.concatenate(draws)
    d = d[np.isfinite(d)]
    res["draws"] = d
    if d.size < 10:
        res.update(ci=None, se=float("nan"), p_neg=None, p_pos=None)
        return res
    res["ci"] = (float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975)))
    res["se"] = float(d.std(ddof=1))
    res["p_neg"] = float((np.sum(d >= 0) + 1) / (d.size + 1))
    res["p_pos"] = float((np.sum(d <= 0) + 1) / (d.size + 1))
    return res


def mean_boot(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261017) -> dict[str, Any]:
    """Mean with a cluster-bootstrap CI and one-sided p-values (p_pos for mean > 0, p_neg for mean < 0)."""
    x = np.asarray(values, float)
    if x.size == 0:
        return {"n": 0, "stat": float("nan"), "ci": None, "se": float("nan"), "p_pos": None, "p_neg": None, "draws": np.array([])}
    s, c = _cluster_sums([x, np.ones_like(x)], clusters)
    d = np.concatenate([s[idx].sum(1) / c[idx].sum(1) for idx in _boot_idx(s.size, n_boot, seed)])
    out: dict[str, Any] = {"n": int(x.size), "clusters": int(s.size), "stat": float(x.mean()), "draws": d}
    if s.size < 2:
        out.update(ci=None, se=float("nan"), p_pos=None, p_neg=None)
        return out
    out.update(
        ci=(float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))),
        se=float(d.std(ddof=1)),
        p_pos=float((np.sum(d <= 0) + 1) / (d.size + 1)),
        p_neg=float((np.sum(d >= 0) + 1) / (d.size + 1)),
    )
    return out


def slope_boot(
    x: Sequence[float], y: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261017
) -> dict[str, Any]:
    """OLS slope of y on x with a cluster bootstrap (draws returned for differences of slopes)."""
    a, b = np.asarray(x, float), np.asarray(y, float)
    if a.size < 3:
        return {"stat": float("nan"), "ci": None, "draws": np.array([])}

    def slope(sx: Any, sy: Any, sxx: Any, sxy: Any, n: Any) -> Any:
        with np.errstate(invalid="ignore", divide="ignore"):
            return (sxy - sx * sy / n) / (sxx - sx * sx / n)

    stat = float(slope(a.sum(), b.sum(), (a * a).sum(), (a * b).sum(), a.size))
    sx, sy, sxx, sxy, n = _cluster_sums([a, b, a * a, a * b, np.ones_like(a)], clusters)
    d = np.concatenate(
        [slope(sx[i].sum(1), sy[i].sum(1), sxx[i].sum(1), sxy[i].sum(1), n[i].sum(1)) for i in _boot_idx(sx.size, n_boot, seed)]
    )
    d = d[np.isfinite(d)]
    ci = (float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))) if d.size >= 10 else None
    return {"stat": stat, "ci": ci, "draws": d}


def diff_draws(a: np.ndarray, b: np.ndarray, stat_a: float, stat_b: float) -> dict[str, Any]:
    """Difference of two independent bootstrap statistics: stat, CI and one-sided p (p_neg: a - b < 0)."""
    k = min(a.size, b.size)
    if k < 10:
        return {"stat": stat_a - stat_b, "ci": None, "p_neg": None, "p_pos": None, "se": float("nan")}
    d = a[:k] - b[:k]
    return {
        "stat": stat_a - stat_b,
        "ci": (float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))),
        "se": float(d.std(ddof=1)),
        "p_neg": float((np.sum(d >= 0) + 1) / (k + 1)),
        "p_pos": float((np.sum(d <= 0) + 1) / (k + 1)),
    }


def fade_net(pre: Sequence[float], post: Sequence[float], thr: float, *, cost: float = COST_RT, side: int = 0) -> np.ndarray:
    """Net return on notional of trading against the pre-event move when |pre| >= thr (NaN otherwise).

    ``side``: 0 both, +1 only after rises (short), -1 only after drops (long)."""
    p, q = np.asarray(pre, float), np.asarray(post, float)
    sel = np.abs(p) >= thr
    if side > 0:
        sel &= p > 0
    elif side < 0:
        sel &= p < 0
    return np.where(sel, -np.sign(p) * q - cost, np.nan)


def mde(se: float) -> float:
    """Minimum detectable effect (80 % power, 5 % two-sided): 2.8 * SE."""
    return 2.8 * se


def h6_verdict(
    *,
    delta: float,
    ci: tuple[float, float] | None,
    clusters_u: int,
    mde_value: float,
    dd: float,
    dd_ci: tuple[float, float] | None,
    p_adj: float | None,
    oos_n_u: int,
    oos_delta: float,
    min_clusters: int = 20,
    gate: float = 0.04,
    min_effect: float = -0.02,
) -> str:
    """Registered verdict rule of step 8 (H6). The thesis is Δ < 0."""
    if clusters_u < min_clusters or not (mde_value <= gate) or ci is None:
        return "inconclusive"
    if delta > min_effect or ci[1] >= 0:
        return "rejected"
    if dd_ci is not None and dd < 0 and dd_ci[1] < 0 and p_adj is not None and p_adj < 0.05 and oos_n_u >= 5 and oos_delta < 0:
        return "confirmed"
    return "inconclusive"
