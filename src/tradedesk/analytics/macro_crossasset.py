"""Dollar, oil, gold and crypto (IDEA-20261017-macro, part B1b). Pure functions, no I/O.

Conventions (shared with ``macro_regimes``):
  * a "day" is a trading day of the Nasdaq Composite; ETF closes are 16:00 ET (13:00 on early-close days) and crypto is
    sampled at that same NY close, so a same-day pair covers the same window;
  * changes, never levels: log returns, decimals (0.012 = 1.2 %);
  * a signal on day i uses only returns up to and including day i (the trailing scale uses days i-window .. i-1);
  * a crypto response starts at the NY close of the signal day, i.e. strictly after the signal is known.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np

from tradedesk.analytics.macro_regimes import HOUR_MS, pearson

# ----------------------------------------------------------------------------- signals (causal)


def trailing_z(r: Sequence[float | None], *, window: int = 60, min_valid: int = 40) -> list[float | None]:
    """z[i] = r[i] / SD(r[i-window .. i-1]) (sample SD, ddof=1); ``None`` when r[i] is missing, fewer than ``min_valid``
    past values are present or the SD is 0. Never uses r[j] for j > i."""
    out: list[float | None] = []
    for i, x in enumerate(r):
        if x is None or i < window:
            out.append(None)
            continue
        past = [v for v in r[i - window : i] if v is not None]
        if len(past) < min_valid:
            out.append(None)
            continue
        sd = float(np.std(np.array(past, float), ddof=1))
        out.append(x / sd if sd > 0 else None)
    return out


def trailing_rank_flag(r: Sequence[float | None], *, window: int = 250, q: float = 0.9, min_valid: int = 200) -> list[bool | None]:
    """flag[i] = r[i] > the ``q`` quantile of r[i-window .. i-1] (a causal "top decile" day); ``None`` if not enough past."""
    out: list[bool | None] = []
    for i, x in enumerate(r):
        if x is None or i < window:
            out.append(None)
            continue
        past = [v for v in r[i - window : i] if v is not None]
        if len(past) < min_valid:
            out.append(None)
            continue
        out.append(bool(x > float(np.quantile(np.array(past, float), q))))
    return out


def forward_log_return(closes_by_open_ms: Mapping[int, float], t_ms: int, h_ms: int, *, bar_ms: int = HOUR_MS) -> float | None:
    """ln P(t + h) / P(t), where P(x) is the close of the bar that ends at x (opened at x - bar)."""
    a = closes_by_open_ms.get(t_ms - bar_ms)
    b = closes_by_open_ms.get(t_ms + h_ms - bar_ms)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return math.log(b / a)


UUP_EXDIV_LOG = -0.015  # log change below this in the second half of December = UUP distribution day


def is_uup_exdiv(day: date, r: float) -> bool:
    """True for a probable UUP ex-distribution day: the Nasdaq series is not adjusted for the annual December
    distribution, which shows up as a drop of 1.5-6 % in the second half of December. Such a change is a data defect
    (like an unadjusted split), so callers drop it (set the return to ``None``)."""
    return day.month == 12 and day.day >= 15 and r < UUP_EXDIV_LOG


def split_index(n: int, frac: float = 0.75) -> int:
    """First row of the out-of-sample tail: rows [0, k) are in-sample, [k, n) out-of-sample."""
    return int(math.floor(n * frac))


# ----------------------------------------------------------------------------- statistics


def event_diff(flag: Sequence[bool] | np.ndarray, y: Sequence[float] | np.ndarray, *, min_n: int = 3) -> float:
    """Mean of y on flagged rows minus mean of y on the other rows (NaN if either group has < ``min_n`` rows)."""
    f = np.asarray(flag, bool)
    v = np.asarray(y, float)
    if f.sum() < min_n or (~f).sum() < min_n:
        return float("nan")
    return float(v[f].mean() - v[~f].mean())


def shift_null(
    signal: Sequence[float] | np.ndarray,
    y: Sequence[float] | np.ndarray,
    stat: Callable[[np.ndarray, np.ndarray], float],
    *,
    min_shift: int = 20,
) -> dict[str, float]:
    """Two-sided circular-shift p-value of ``stat(signal, y)``: the signal is rotated by k = min_shift .. n - min_shift
    rows against y. Keeps the autocorrelation and clustering of each series and breaks only their alignment."""
    s, v = np.asarray(signal), np.asarray(y, float)
    n = len(v)
    obs = stat(s, v)
    if n < 2 * min_shift + 2 or math.isnan(obs):
        return {"stat": obs, "p": float("nan"), "sd": float("nan"), "n": n}
    null = np.array([stat(np.roll(s, k), v) for k in range(min_shift, n - min_shift + 1)])
    null = null[~np.isnan(null)]
    return {"stat": obs, "p": float((np.abs(null) >= abs(obs) - 1e-12).mean()), "sd": float(null.std()), "n": n}


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values (same order as the input, monotone, capped at 1)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    out = [0.0] * m
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * pvalues[i]))
        out[i] = run
    return out


def ols_coefs(y: Sequence[float], xs: Sequence[Sequence[float]]) -> list[float]:
    """OLS coefficients [intercept, b1, ...] of y on the columns ``xs``."""
    Y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(Y))] + [np.asarray(c, float) for c in xs])
    coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
    return [float(c) for c in coef]


def corr_stat(s: np.ndarray, y: np.ndarray) -> float:
    return pearson(s, y)


# ----------------------------------------------------------------------------- registered verdict rules


def g_verdict(
    stat: float,
    ci95: tuple[float, float] | None,
    p_holm: float,
    is_est: float,
    oos_est: float,
    mde_value: float,
    gate: float,
) -> str:
    """Registered rule for G1-G3 (expected sign +), IDEA-20261017-macro B1b.

    ✓ Holm p < 0.05, CI lower > 0, estimate > 0 in-sample and out-of-sample; ≈ CI lower > 0 otherwise;
    '✗ протилежний знак' CI upper < 0; ✗ (не виявлено) CI spans 0 and MDE <= gate; ◯ otherwise."""
    if ci95 is None or math.isnan(stat):
        return "◯"
    lo, hi = ci95
    if lo > 0:
        if p_holm < 0.05 and is_est > 0 and oos_est > 0:
            return "✓"
        return "≈"
    if hi < 0:
        return "✗ протилежний знак"
    return "✗ (не виявлено)" if mde_value <= gate else "◯ (ворота потужності)"


def r_verdict(g1: str, ci95: tuple[float, float] | None, oos_mean: float, mde_value: float, gate: float) -> str:
    """Registered rule R: ✓ only if G1 is ✓, the CI of the mean net per trade is above 0 and the OOS mean net > 0;
    ◯ if G1 is ◯ or MDE > gate; otherwise ✗ (no edge found)."""
    if g1 == "✓" and ci95 is not None and ci95[0] > 0 and oos_mean > 0:
        return "✓"
    if g1.startswith("◯") or not (mde_value <= gate):
        return "◯ (ворота потужності)"
    return "✗ (переваги не виявлено)"


def summary(x: Sequence[float]) -> dict[str, Any]:
    a = np.asarray(x, float)
    return {"n": len(a), "mean": float(a.mean()) if len(a) else float("nan"), "sd": float(a.std(ddof=1)) if len(a) > 1 else float("nan")}
