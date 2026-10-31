"""Polymarket model modules (IDEA-20261031-structural-edges-depth, item 5): pure functions, no I/O.

* H11 - the ladder of one daily "above" event as a vol forecast: probit fit of YES quotes on log-moneyness gives the
  12 h standard deviation the market implies (``ladder_sigma``), scored against realised variance with QLIKE.
* H7 - an hour-of-day x weekend variance profile (``intraday_profile``) and an EWMA of de-seasonalised returns
  (``ewma_hourly_var``) give the variance of a window that is not a whole day (``window_weight``).
* H8 - the shift of a model probability when the forward is moved by the Binance-vs-index gap ``g`` (``shift_delta``).
* Shared statistics: Brier / log-loss, a block bootstrap of a ratio of sums, Cochran's Q.

Units: decimals (0.01 = 1 %), time in unix seconds, variances of log returns over the stated window (not annualised
unless named so). Every function takes the evaluation time as a parameter; nothing reads data after it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Any

from .pm_threshold import DAY_S, HOUR_S, YEAR_S, digital_above
from .probability import prob_touch

_N = NormalDist()

# ----------------------------------------------------------------------------- H11: ladder -> implied 12 h sigma


def ladder_sigma(
    strikes: Sequence[float], quotes: Sequence[float], s0: float, *, lo: float = 0.03, hi: float = 0.97, min_n: int = 2
) -> dict[str, float] | None:
    """OLS fit of ``inv_norm(q_i) = alpha - beta * ln(K_i / s0)`` over strikes with ``lo <= q <= hi``.

    Under a lognormal terminal price ``P(S_T > K) = N((ln(s0/K) + m) / s)``, so ``s = 1 / beta`` is the standard
    deviation of the log price over the market's horizon (sigma * sqrt(tau)) and ``alpha = m / s``. Returns
    ``{"s", "alpha", "beta", "n"}`` or None (fewer than ``min_n`` usable strikes, all strikes equal, or beta <= 0).
    """
    if s0 <= 0:
        return None
    pts = [(math.log(k / s0), _N.inv_cdf(q)) for k, q in zip(strikes, quotes, strict=True) if k > 0 and lo <= q <= hi]
    if len(pts) < min_n:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    mz = sum(z for _, z in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (z - mz) for x, z in pts) / sxx  # dz/dx = -beta
    beta = -slope
    if beta <= 0:
        return None
    return {"s": 1.0 / beta, "alpha": mz + beta * mx, "beta": beta, "n": float(n)}


def qlike(rv: float, v: float) -> float:
    """QLIKE loss of a variance forecast ``v`` against a realised proxy ``rv``: rv/v - ln(rv/v) - 1 (>= 0, 0 iff equal).

    Its ranking of forecasts is robust to an unbiased noisy proxy (Patton 2011)."""
    if rv <= 0 or v <= 0:
        raise ValueError("rv and v must be positive")
    x = rv / v
    return x - math.log(x) - 1.0


def realized_var(closes: Sequence[tuple[int, float]], t0: int, t1: int) -> float | None:
    """Sum of squared log returns of consecutive closes with ``t0 <= close_time <= t1`` (the first close is the one at
    t0, so the returns cover (t0, t1]). None if the window is not fully covered by hourly closes."""
    pts = [(t, c) for t, c in closes if t0 <= t <= t1 and c > 0]
    if len(pts) < 2 or pts[0][0] != t0 or pts[-1][0] != t1 or len(pts) != (t1 - t0) // HOUR_S + 1:
        return None
    return sum(math.log(b / a) ** 2 for (_, a), (_, b) in zip(pts, pts[1:], strict=False))


# ----------------------------------------------------------------------------- H7: intraday x weekend profile


def is_weekend(ts: int) -> bool:
    """True for a moment inside [Fri 21:00 UTC; Sun 21:00 UTC) (after the US Friday close until the Sunday reopen)."""
    wd, h = (ts // DAY_S + 3) % 7, (ts // HOUR_S) % 24  # Mon = 0 (1970-01-01 was a Thursday)
    return (wd == 4 and h >= 21) or wd == 5 or (wd == 6 and h < 21)


def cell_of(ts: int) -> tuple[int, bool]:
    """Profile cell of an hourly bar opening at ``ts``: (UTC hour, weekend flag)."""
    return (ts // HOUR_S) % 24, is_weekend(ts)


def intraday_profile(bar_returns: Iterable[tuple[int, float]]) -> dict[tuple[int, bool], float]:
    """Variance weight per (UTC hour, weekend) cell from ``(bar_open_ts, log_return)`` pairs.

    w = (mean |r| of the cell / mean |r| of all bars)^2 (Andersen-Bollerslev style, robust to outliers), then scaled so
    the average weight over one calendar week (each hour of the week counted once) is 1. Cells without bars get 1.
    """
    s: dict[tuple[int, bool], float] = {}
    n: dict[tuple[int, bool], int] = {}
    tot = cnt = 0
    for ts, r in bar_returns:
        c = cell_of(ts)
        s[c] = s.get(c, 0.0) + abs(r)
        n[c] = n.get(c, 0) + 1
        tot += abs(r)
        cnt += 1
    if cnt == 0 or tot <= 0:
        return {(h, w): 1.0 for h in range(24) for w in (False, True)}
    mean_all = tot / cnt
    raw = {(h, w): ((s[(h, w)] / n[(h, w)]) / mean_all) ** 2 if n.get((h, w)) else 1.0 for h in range(24) for w in (False, True)}
    # weekly average: walk the 168 hours of a reference week (Mon 2024-01-01 00:00 UTC)
    ref = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    avg = sum(raw[cell_of(ref + i * HOUR_S)] for i in range(168)) / 168.0
    return {c: v / avg for c, v in raw.items()}


def weight_at(profile: Mapping[tuple[int, bool], float], bar_open_ts: int, shift_h: int = 0) -> float:
    """Profile weight of the bar opening at ``bar_open_ts``; ``shift_h`` moves the lookup time (placebo)."""
    return profile[cell_of(bar_open_ts + shift_h * HOUR_S)]


def window_weight(profile: Mapping[tuple[int, bool], float], t0: int, t1: int, shift_h: int = 0) -> float:
    """Sum of weights of the hourly bars opening in [t0; t1): the window's variance in units of an average hour."""
    return sum(weight_at(profile, t, shift_h) for t in range(t0, t1, HOUR_S))


def ewma_hourly_var(
    closes: Sequence[tuple[int, float]],
    t0: int,
    *,
    profile: Mapping[tuple[int, bool], float] | None = None,
    shift_h: int = 0,
    span_days: float = 30.0,
    lookback_days: int = 90,
) -> float | None:
    """EWMA of squared hourly log returns known at ``t0`` (per-hour variance, not annualised).

    Same recursion as ``pm_threshold.realized_vol`` (lambda = 1 - 2 / (span bars + 1), at least 48 closes); with a
    ``profile`` every squared return is divided by its bar's weight, so the result is the variance of an average hour.
    """
    lo_t = t0 - lookback_days * DAY_S
    pts = [(t, c) for t, c in closes if lo_t <= t <= t0 and c > 0]
    if len(pts) < 48:
        return None
    lam = 1.0 - 2.0 / (span_days * DAY_S / HOUR_S + 1.0)
    var: float | None = None
    for (_ta, a), (tb, b) in zip(pts, pts[1:], strict=False):
        r2 = math.log(b / a) ** 2
        if profile is not None:
            r2 /= weight_at(profile, tb - HOUR_S, shift_h)
        var = r2 if var is None else lam * var + (1.0 - lam) * r2
    return var


def prob_from_var(s0: float, strike: float, var: float) -> float:
    """P(S_T > K) for a driftless lognormal with log-variance ``var`` over the horizon (digital_above with tau = 1)."""
    return digital_above(s0, strike, math.sqrt(var), 1.0) if var > 0 else (1.0 if s0 > strike else 0.0)


def brier(p: float, y: int) -> float:
    return (p - y) ** 2


def log_loss(p: float, y: int, eps: float = 1e-4) -> float:
    p = min(1.0 - eps, max(eps, p))
    return -math.log(p if y else 1.0 - p)


# ----------------------------------------------------------------------------- H8: Binance-vs-index shift


def shift_delta(kind: str, forward: float, strike: float, iv: float, tau_years: float, g: float, direction: str | None = None) -> float:
    """Change of the model probability when the forward moves from F to F * (1 + g), same IV and horizon.

    ``kind='above'``: N(d2) digital; ``kind='touch'``: continuous barrier ``prob_touch`` ('up' = reach, 'down' = dip).
    The smile slope term of ``prob_above`` is the same at both forwards to first order, so the flat-IV difference is the
    correction to add to a stored model probability.
    """
    if forward <= 0 or strike <= 0 or iv <= 0 or tau_years <= 0:
        raise ValueError("forward, strike, iv and tau must be positive")
    f1 = forward * (1.0 + g)
    if kind == "above":
        return digital_above(f1, strike, iv, tau_years) - digital_above(forward, strike, iv, tau_years)
    if kind == "touch":
        if direction not in ("up", "down"):
            raise ValueError(f"direction {direction!r}")
        return prob_touch(f1, strike, iv, tau_years) - prob_touch(forward, strike, iv, tau_years)
    raise ValueError(f"kind {kind!r}")


def tau_of_hours(hours: float) -> float:
    return hours * HOUR_S / YEAR_S


# ----------------------------------------------------------------------------- statistics


def block_bootstrap_ratio(
    blocks: Sequence[tuple[float, int]], *, n_boot: int = 10_000, seed: int = 20261031, stat: Callable[[float, int], float] | None = None
) -> dict[str, float | None]:
    """Mean over units as a ratio of sums, resampling whole blocks ``(sum_of_values, n_units)`` with replacement.

    Returns ``{"mean", "lo", "hi", "p", "se", "n_blocks", "n"}``: percentile 95 % CI, two-sided p = 2 * min(share of
    replicates <= 0, share >= 0) (capped at 1), ``se`` = SD of the replicates.
    """
    blocks = [(float(s), int(n)) for s, n in blocks if n > 0]
    nb = len(blocks)
    tot_n = sum(n for _, n in blocks)
    if nb == 0 or tot_n == 0:
        return {"mean": None, "lo": None, "hi": None, "p": None, "se": None, "n_blocks": nb, "n": tot_n}
    mean = sum(s for s, _ in blocks) / tot_n
    rng = random.Random(seed)
    reps: list[float] = []
    for _ in range(n_boot):
        ss = nn = 0.0
        for _ in range(nb):
            s, n = blocks[rng.randrange(nb)]
            ss += s
            nn += n
        reps.append(ss / nn)
    reps.sort()
    m = sum(reps) / n_boot
    se = math.sqrt(sum((x - m) ** 2 for x in reps) / (n_boot - 1))
    le = sum(1 for x in reps if x <= 0) / n_boot
    ge = sum(1 for x in reps if x >= 0) / n_boot
    return {
        "mean": mean,
        "lo": reps[int(0.025 * n_boot)],
        "hi": reps[min(n_boot - 1, int(0.975 * n_boot))],
        "p": min(1.0, 2.0 * min(le, ge)),
        "se": se,
        "n_blocks": nb,
        "n": tot_n,
    }


def block_bootstrap_logratio(
    blocks: Sequence[tuple[float, float]], *, n_boot: int = 10_000, seed: int = 20261031
) -> dict[str, float | None]:
    """ln(sum num / sum den) with blocks ``(sum_num, sum_den)`` resampled whole; 95 % percentile CI and two-sided p
    of ln-ratio = 0 (as in :func:`block_bootstrap_ratio`)."""
    blocks = [(float(a), float(b)) for a, b in blocks if b > 0]
    nb = len(blocks)
    if nb == 0:
        return {"mean": None, "lo": None, "hi": None, "p": None, "se": None, "n_blocks": 0}
    est = math.log(sum(a for a, _ in blocks) / sum(b for _, b in blocks))
    rng = random.Random(seed)
    reps = []
    for _ in range(n_boot):
        a = b = 0.0
        for _ in range(nb):
            x, y = blocks[rng.randrange(nb)]
            a += x
            b += y
        reps.append(math.log(a / b))
    reps.sort()
    m = sum(reps) / n_boot
    se = math.sqrt(sum((x - m) ** 2 for x in reps) / (n_boot - 1))
    le = sum(1 for x in reps if x <= 0) / n_boot
    ge = sum(1 for x in reps if x >= 0) / n_boot
    return {
        "mean": est,
        "lo": reps[int(0.025 * n_boot)],
        "hi": reps[min(n_boot - 1, int(0.975 * n_boot))],
        "p": min(1.0, 2.0 * min(le, ge)),
        "se": se,
        "n_blocks": nb,
    }


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> dict[str, float]:
    """Cochran's Q and I^2 of group means with standard errors; p from the chi-square with k-1 df."""
    w = [1.0 / (s * s) for s in ses]
    mw = sum(wi * m for wi, m in zip(w, means, strict=True)) / sum(w)
    q = sum(wi * (m - mw) ** 2 for wi, m in zip(w, means, strict=True))
    df = len(means) - 1
    i2 = max(0.0, (q - df) / q) if q > 0 else 0.0
    return {"q": q, "df": float(df), "p": _chi2_sf(q, df), "i2": i2, "pooled": mw}


def _chi2_sf(x: float, k: int) -> float:
    """Survival function of the chi-square distribution (series of the regularised lower gamma)."""
    if k <= 0:
        return float("nan")
    if x <= 0:
        return 1.0
    a, xx = k / 2.0, x / 2.0
    term = summ = 1.0 / a
    n = 1
    while n < 1000:
        term *= xx / (a + n)
        summ += term
        if abs(term) < abs(summ) * 1e-14:
            break
        n += 1
    lower = summ * math.exp(-xx + a * math.log(xx) - math.lgamma(a))
    return max(0.0, min(1.0, 1.0 - lower))


def holm(pvals: Mapping[str, float], alpha: float = 0.05) -> dict[str, dict[str, Any]]:
    """Holm step-down: sorted p_(i) is compared with alpha / (m - i); once one fails, the rest fail."""
    m = len(pvals)
    out: dict[str, dict[str, Any]] = {}
    ok = True
    for i, (k, p) in enumerate(sorted(pvals.items(), key=lambda kv: kv[1])):
        thr = alpha / (m - i)
        ok = ok and p <= thr
        out[k] = {"p": p, "threshold": thr, "reject": ok}
    return out
