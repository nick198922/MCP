"""SMT divergence as a sweep filter (IDEA-20261016-course-setups-crypto, step 10; H1 of LIB-20261016-ict-concepts).

Two positively correlated assets A and B on a joint time grid. A sweeps its latest swing low (a wick below the swing
and a close back above it). The sweep is *confirmed* when B also printed a lower low than its own low around the time
of A's swing (within +-``lag`` bars), up to the sweep bar; otherwise it is an *SMT* sweep (B holds a higher low).
The thesis: SMT sweeps revert more than confirmed ones.

Pure functions, no I/O. Bars are ``course_s14_ema.Bars`` (open times in ms UTC, sorted). Everything is written for
the long frame (a sweep of a low); the bearish side is the same logic on negated prices of both assets
(``Bars.mirrored``). ``pos`` is the position of each bar inside its data segment (``course_s14_ema.segment_pos``), so
swings, ATR and horizons never cross a hole in the data. Detectors are causal: an event decided at the close of bar
t uses bars <= t only (``tests/test_smt_divergence.py`` checks that events up to t do not change when bars after t
change). Outcomes look forward by design. ``y`` is in ATR units of A; trade returns are decimal fractions on notional.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.per_asset import chi2_sf

__all__ = [
    "SweepEvent",
    "cochran_q",
    "depth_stratified_diff",
    "forward_move",
    "fractal_lows",
    "joint_cluster_diff",
    "joint_grid",
    "resample_min",
    "smt_verdict",
    "sweep_events",
]


# ---------------------------------------------------------------- bars


def resample_min(b: Bars, step_ms: int, min_count: int) -> Bars:
    """Aggregate bars into ``step_ms`` buckets aligned to 00:00 UTC, keeping buckets with >= ``min_count`` source
    bars: open of the first, close of the last, max high, min low (for gappy sources such as forex 5m bid bars)."""
    if len(b) == 0:
        return Bars(b.t, b.o, b.h, b.low, b.c, step_ms)
    key = b.t // step_ms
    starts = np.flatnonzero(np.concatenate([[True], key[1:] != key[:-1]]))
    counts = np.diff(np.concatenate([starts, [key.size]]))
    ends = starts + counts - 1
    ok = counts >= min_count
    h = np.maximum.reduceat(b.h, starts)[ok]
    lo = np.minimum.reduceat(b.low, starts)[ok]
    s, e = starts[ok], ends[ok]
    return Bars(key[s] * step_ms, b.o[s].copy(), h, lo, b.c[e].copy(), step_ms)


def joint_grid(a: Bars, b: Bars, shift_b_ms: int = 0) -> tuple[Bars, Bars]:
    """Both series restricted to the open times present in both. ``shift_b_ms`` > 0 pairs A at time T with B at
    T - shift (B's bars are relabelled to T = t_B + shift); the returned B carries A's time labels."""
    tb = b.t + shift_b_ms
    common, ia, ib = np.intersect1d(a.t, tb, assume_unique=True, return_indices=True)
    a2 = Bars(common, a.o[ia], a.h[ia], a.low[ia], a.c[ia], a.bar_ms)
    b2 = Bars(common, b.o[ib], b.h[ib], b.low[ib], b.c[ib], b.bar_ms)
    return a2, b2


# ---------------------------------------------------------------- swings and sweeps


def fractal_lows(low: np.ndarray, pos: np.ndarray, n: int = 1) -> np.ndarray:
    """mask[f]: low f strictly below the ``n`` lows on each side, bars f-n .. f+n in one segment. Known after the
    close of f + n, usable on bars >= f + n + 1. n = 1 is the 3-candle fractal of ``markup.fractals``."""
    m = low.size
    out = np.zeros(m, dtype=bool)
    if m < 2 * n + 1:
        return out
    f = np.arange(n, m - n)
    ok = pos[f + n] >= 2 * n
    for d in range(1, n + 1):
        ok &= (low[f] < low[f - d]) & (low[f] < low[f + d])
    out[f[ok]] = True
    return out


@dataclass(frozen=True)
class SweepEvent:
    t: int  # decision bar (the sweep bar, or ``after`` bars later)
    sweep: int  # the bar whose low first went below the swing low
    f: int  # the swing (fractal) bar of A
    level_a: float  # A's swing low
    level_b: float  # B's corresponding low (min over f - lag .. min(f + lag, sweep - 1))
    smt: bool  # True: B did not print a lower low than level_b up to the decision bar
    close_back: bool  # True: the sweep bar closed above level_a (a sweep); False: a breakdown
    depth_atr: float  # (level_a - low_A[sweep]) / ATR_A[sweep]


def sweep_events(
    a: Bars,
    b: Bars,
    pos: np.ndarray,
    atr_a: np.ndarray,
    *,
    n: int = 1,
    lag: int = 2,
    after: int = 0,
    warmup: int = 100,
    mode: str = "sweep",
) -> list[SweepEvent]:
    """Sweeps of A's latest untaken swing low on a joint grid with B (long frame).

    The reference swing at bar i is the latest fractal low f usable at i (f + n + 1 <= i; a newer fractal replaces an
    older one), not taken by bars f+1 .. i-1. The first bar with low < low[f] takes it (no further events on f); it
    is an event when ``mode == "sweep"`` and it closes above low[f], or always when ``mode == "any"``. B's level is
    min low_B over f - lag .. min(f + lag, i - 1) (same segment); the sweep is confirmed when B's low on the bars after
    that window up to the decision bar i + ``after`` goes below it, else SMT. Events need pos >= ``warmup``, a finite
    ATR, and the decision bar in the same segment as the sweep."""
    if mode not in ("sweep", "any"):
        raise ValueError("mode must be 'sweep' or 'any'")
    la, ca, lb = a.low, a.c, b.low
    nbar = la.size
    fr = fractal_lows(la, pos, n)
    out: list[SweepEvent] = []
    ref = -1
    for i in range(nbar):
        if pos[i] == 0:
            ref = -1
        f_new = i - n - 1
        if f_new >= 0 and pos[i] >= n + 1 and fr[f_new]:
            ref = f_new
        if ref < 0 or not la[i] < la[ref]:
            continue
        f, ref = ref, -1
        lvl = float(la[f])
        back = bool(ca[i] > lvl)
        if mode == "sweep" and not back:
            continue
        t = i + after
        if pos[i] < warmup or t >= nbar or pos[t] != pos[i] + after or not math.isfinite(atr_a[i]):
            continue
        seg0 = i - int(pos[i])
        lo, hi = max(f - lag, seg0), min(f + lag, i - 1)
        lvl_b = float(np.min(lb[lo : hi + 1]))
        conf = bool(np.min(lb[hi + 1 : t + 1]) < lvl_b)
        out.append(SweepEvent(t, i, f, lvl, lvl_b, not conf, back, (lvl - float(la[i])) / float(atr_a[i])))
    return out


def forward_move(c: np.ndarray, pos: np.ndarray, t: int, k: int) -> float:
    """c[t + k] - c[t] when bar t + k exists in the same segment, else NaN."""
    j = t + k
    if j >= c.size or pos[j] != pos[t] + k:
        return float("nan")
    return float(c[j] - c[t])


# ---------------------------------------------------------------- statistics


def joint_cluster_diff(
    a: Sequence[float], a_cl: Sequence[Any], b: Sequence[float], b_cl: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261017
) -> dict[str, Any]:
    """mean(a) - mean(b) with a joint cluster bootstrap (the same resampled clusters feed both means). Returns diff,
    95 % percentile CI, one-sided p for diff > 0 ((1 + #{d* <= 0}) / (1 + B)) and se (SD of the bootstrap)."""
    keys = sorted(set(a_cl) | set(b_cl), key=str)
    pos = {k: i for i, k in enumerate(keys)}
    g = len(keys)
    sa, na, sb, nb = (np.zeros(g) for _ in range(4))
    for v, k in zip(a, a_cl, strict=True):
        sa[pos[k]] += v
        na[pos[k]] += 1
    for v, k in zip(b, b_cl, strict=True):
        sb[pos[k]] += v
        nb[pos[k]] += 1
    if na.sum() == 0 or nb.sum() == 0:
        return {"diff": float("nan"), "ci": None, "p": None, "se": float("nan")}
    d = float(sa.sum() / na.sum() - sb.sum() / nb.sum())
    if g < 2:
        return {"diff": d, "ci": None, "p": None, "se": float("nan")}
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    chunk = max(1, 2_000_000 // g)
    for lo in range(0, n_boot, chunk):
        hi = min(n_boot, lo + chunk)
        idx = rng.integers(0, g, size=(hi - lo, g))
        with np.errstate(invalid="ignore", divide="ignore"):
            out[lo:hi] = sa[idx].sum(1) / na[idx].sum(1) - sb[idx].sum(1) / nb[idx].sum(1)
    out = out[np.isfinite(out)]
    return {
        "diff": d,
        "ci": (float(np.quantile(out, 0.025)), float(np.quantile(out, 0.975))),
        "p": float((np.sum(out <= 0) + 1) / (out.size + 1)),
        "se": float(np.std(out)),
    }


def depth_stratified_diff(y: Sequence[float], smt: Sequence[bool], depth: Sequence[float], cuts: Sequence[float]) -> dict[str, Any]:
    """Mean(y | SMT) - mean(y | confirmed) inside bins of ``depth`` (ascending ``cuts``), weighted by the SMT count of
    each bin; bins without both groups are skipped. Returns the weighted difference and the bins used."""
    yy = np.asarray(y, dtype=float)
    s = np.asarray(smt, dtype=bool)
    bins = np.searchsorted(np.asarray(cuts, dtype=float), np.asarray(depth, dtype=float), side="left")
    tot_w, acc, used = 0, 0.0, 0
    for k in np.unique(bins):
        m = bins == k
        a, c = yy[m & s], yy[m & ~s]
        a, c = a[np.isfinite(a)], c[np.isfinite(c)]
        if a.size == 0 or c.size == 0:
            continue
        acc += a.size * (float(a.mean()) - float(c.mean()))
        tot_w += a.size
        used += 1
    return {"diff": acc / tot_w if tot_w else float("nan"), "bins": used, "n_smt": tot_w}


def cochran_q(estimates: Sequence[float], ses: Sequence[float]) -> dict[str, Any]:
    """Cochran's Q of per-asset estimates with standard errors: Q = sum w (x - xbar_w)^2, w = 1/se^2; p from
    chi-square(k - 1); I^2 = max(0, (Q - df)/Q)."""
    x = np.asarray(estimates, dtype=float)
    se = np.asarray(ses, dtype=float)
    ok = np.isfinite(x) & np.isfinite(se) & (se > 0)
    x, se = x[ok], se[ok]
    k = int(x.size)
    if k < 2:
        return {"k": k, "q": float("nan"), "df": k - 1, "p": None, "i2": float("nan")}
    w = 1.0 / se**2
    m = float(np.sum(w * x) / np.sum(w))
    q = float(np.sum(w * (x - m) ** 2))
    df = k - 1
    return {"k": k, "q": q, "df": df, "p": chi2_sf(q, df), "i2": max(0.0, (q - df) / q) if q > 0 else 0.0, "mean": m}


def smt_verdict(
    *,
    n_dates: int,
    mde: float,
    diff: float,
    ci_day: tuple[float, float] | None,
    ci_week: tuple[float, float] | None,
    p_holm: float | None,
    oos_diff: float,
    oos_p: float | None,
    trade_ci_is: tuple[float, float] | None,
    trade_oos_mean: float,
    min_dates: int = 200,
    gate: float = 0.20,
    min_effect: float = 0.20,
) -> str:
    """The registered verdict of one timeframe (card, "Попередня реєстрація — крок 10")."""
    if n_dates < min_dates or not mde <= gate:
        return "inconclusive"
    if not diff >= min_effect or ci_day is None or ci_day[0] <= 0:
        return "rejected"
    if (
        ci_week is not None
        and ci_week[0] > 0
        and p_holm is not None
        and p_holm < 0.05
        and oos_diff > 0
        and oos_p is not None
        and oos_p < 0.05
        and trade_ci_is is not None
        and trade_ci_is[0] > 0
        and trade_oos_mean > 0
    ):
        return "confirmed"
    return "inconclusive"
