"""USDe supply vs perp funding: weekly alignment and small-sample regression statistics (pure, no I/O).

Used by ``scripts/verify_usde_funding.py`` (IDEA-20261025-ethena-usde-supply-funding-regime). Times are epoch ms (UTC),
rates are decimals (0.10 = 10 % APR). Every lookup is "as of" a time passed in, so values before ``t`` never depend on
data after ``t``.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
_MONDAY0_MS = 4 * DAY_MS  # 1970-01-05 00:00 UTC was a Monday


def monday_anchors(start_ms: int, end_ms: int) -> list[int]:
    """Mondays 00:00 UTC in ``[start_ms, end_ms]``."""
    t = _MONDAY0_MS + -(-(start_ms - _MONDAY0_MS) // WEEK_MS) * WEEK_MS  # ceil to the next Monday
    out = []
    while t <= end_ms:
        out.append(t)
        t += WEEK_MS
    return out


def value_asof(times: Sequence[float], values: Sequence[float], t_ms: float) -> float | None:
    """Last value stamped at or before ``t_ms`` (``times`` sorted ascending); None if there is none."""
    i = bisect_right(times, t_ms)
    return None if i == 0 else float(values[i - 1])


def window_funding_apr(
    times: Sequence[float],
    rates: Sequence[float],
    interval_hours: Sequence[float],
    start_ms: int,
    end_ms: int,
    min_cover: float = 0.9,
) -> float | None:
    """Funding earned by a short over ``[start_ms, end_ms)`` as APR: sum of settled rates x 365 / days.

    Settlements are counted by their time; the window is accepted only when the settled intervals cover at least
    ``min_cover`` of its hours (gaps -> None)."""
    lo, hi = bisect_right(times, start_ms - 1), bisect_right(times, end_ms - 1)
    hours = sum(interval_hours[lo:hi])
    span_h = (end_ms - start_ms) / 3_600_000
    if span_h <= 0 or hours < min_cover * span_h:
        return None
    days = span_h / 24
    return float(sum(rates[lo:hi])) * 365.0 / days


def lag1_autocorr(v: Sequence[float]) -> float:
    a = np.asarray(v, dtype=float)
    a = a - a.mean()
    den = float(a @ a)
    return float(a[1:] @ a[:-1]) / den if den > 0 else 0.0


def ols_residuals(y: Sequence[float], controls: Sequence[Sequence[float]]) -> np.ndarray:
    """Residuals of ``y`` on a constant and the control columns (each control a sequence aligned with ``y``)."""
    ya = np.asarray(y, dtype=float)
    cols = [np.ones_like(ya)] + [np.asarray(c, dtype=float) for c in controls]
    x = np.column_stack(cols)
    beta, *_ = np.linalg.lstsq(x, ya, rcond=None)
    return ya - x @ beta


def adf_tstat(v: Sequence[float], lags: int = 1) -> float:
    """Augmented Dickey-Fuller t-statistic (constant, no trend): t of rho in dv_t = a + rho v_{t-1} + sum dv_{t-j}.

    5 % critical value about -2.88 for n ~ 100-250 (MacKinnon); more negative = stationary."""
    a = np.asarray(v, dtype=float)
    d = np.diff(a)
    rows = len(d) - lags
    if rows < lags + 5:
        raise ValueError("series too short for ADF")
    y = d[lags:]
    cols = [np.ones(rows), a[lags:-1]] + [d[lags - j : len(d) - j] for j in range(1, lags + 1)]
    x = np.column_stack(cols)
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    u = y - x @ beta
    s2 = float(u @ u) / (rows - x.shape[1])
    cov = s2 * np.linalg.inv(x.T @ x)
    return float(beta[1] / math.sqrt(cov[1, 1]))


def mde_partial_corr(n: int, k: int, rho_x: float = 0.0, rho_y: float = 0.0, z: float = 2.80) -> tuple[float, float]:
    """Minimum detectable |partial correlation| (two-sided 5 %, power 80 % for z = 2.80) and the effective n.

    n_eff = min(n, n (1 - rho_x rho_y) / (1 + rho_x rho_y)) (Bartlett correction for two AR(1) series);
    MDE = tanh(z / sqrt(n_eff - 3 - k))."""
    p = rho_x * rho_y
    n_eff = min(float(n), n * (1 - p) / (1 + p)) if p > -1 else float(n)
    dof = n_eff - 3 - k
    if dof <= 0:
        return 1.0, n_eff
    return math.tanh(z / math.sqrt(dof)), n_eff


@dataclass(frozen=True)
class PartialCorr:
    r: float
    se: float
    lo: float
    hi: float
    n: int
    rho_x: float  # lag-1 autocorrelation of the residualised x
    rho_y: float
    contrib: tuple[float, ...]  # per-observation product of standardised residuals (sum / n = r)


def partial_corr_nw(y: Sequence[float], x: Sequence[float], controls: Sequence[Sequence[float]], lag: int = 4) -> PartialCorr:
    """Partial correlation of ``y`` and ``x`` given ``controls`` with a Newey-West (Bartlett, ``lag``) standard error.

    Frisch-Waugh: residualise both on a constant + controls, standardise, regress one on the other; the slope is r.
    The HAC variance gets a small-sample factor n / (n - k - 2) for the k controls, constant and slope."""
    ey = ols_residuals(y, controls)
    ex = ols_residuals(x, controls)
    n = len(ey)
    sx, sy = float(ex.std()), float(ey.std())
    if sx == 0 or sy == 0:
        raise ValueError("degenerate residuals")
    zx, zy = ex / sx, ey / sy
    r = float(zx @ zy) / float(zx @ zx)
    u = zy - r * zx
    g = zx * u
    s = float(g @ g)
    for j in range(1, lag + 1):
        s += 2 * (1 - j / (lag + 1)) * float(g[j:] @ g[:-j])
    k = len(controls)
    var = max(s, 0.0) / float(zx @ zx) ** 2 * n / max(n - k - 2, 1)
    se = math.sqrt(var)
    return PartialCorr(r, se, r - 1.96 * se, r + 1.96 * se, n, lag1_autocorr(ex), lag1_autocorr(ey), tuple(float(c) for c in zx * zy))


def block_bootstrap_diff(
    values: Sequence[float], mask: Sequence[bool], block: int = 8, n_boot: int = 2000, seed: int = 7
) -> tuple[float, float, float]:
    """Mean(values[mask]) - mean(values[~mask]) with a circular block-bootstrap 95 % interval (blocks of weeks)."""
    v = np.asarray(values, dtype=float)
    m = np.asarray(mask, dtype=bool)
    n = len(v)
    point = float(v[m].mean() - v[~m].mean())
    rng = np.random.default_rng(seed)
    nb = math.ceil(n / block)
    out = []
    for _ in range(n_boot):
        starts = rng.integers(0, n, nb)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
        mm = m[idx]
        if mm.all() or not mm.any():
            continue
        out.append(float(v[idx][mm].mean() - v[idx][~mm].mean()))
    lo, hi = np.percentile(out, [2.5, 97.5]) if out else (math.nan, math.nan)
    return point, float(lo), float(hi)


def forward_drawdown(times: Sequence[float], lows: Sequence[float], ref: float, start_ms: int, end_ms: int) -> float | None:
    """min(low over bars opening in ``[start_ms, end_ms)``) / ref - 1; None if no bar."""
    lo, hi = bisect_right(times, start_ms - 1), bisect_right(times, end_ms - 1)
    if hi <= lo or ref <= 0:
        return None
    return float(min(lows[lo:hi])) / ref - 1.0


FundingSeries = tuple[Sequence[float], Sequence[float], Sequence[float]]  # settlement times, rates, interval hours


def _mean_full(vals: list[float | None], full: bool) -> float | None:
    return sum(vals) / len(vals) if full and None not in vals else None  # type: ignore[arg-type]


def weekly_panel(
    anchors: Sequence[int],
    now_ms: int,
    usde_t: Sequence[float],
    usde_v: Sequence[float],
    oi: Sequence[float | None],
    funding: tuple[FundingSeries, FundingSeries],
    funding_alt: tuple[FundingSeries, FundingSeries],
    bar_t: Sequence[float],
    bar_close: Sequence[float],
    bar_low: Sequence[float],
    usde_lag_ms: int = 2 * DAY_MS,
) -> list[dict[str, float | None]]:
    """One row per Monday anchor T (epoch ms), every field computed only from data stamped as stated.

    - ``usde``: last USDe supply stamped <= T - ``usde_lag_ms`` (``usde_nolag``: <= T);
    - ``oi``: open interest snapshot for T (the caller aligns it, e.g. Sunday 23:55 UTC), passed through;
    - ``f_btc`` / ``f_eth`` / ``f``: funding APR of ``funding`` legs (BTC, ETH) and their mean over [T, T + 7 d);
      ``f_byb``: mean of the ``funding_alt`` legs. All funding fields are None unless the week is complete
      (T + 7 d <= ``now_ms``) and covered (see :func:`window_funding_apr`);
    - ``ret``: log(close of the daily bar opening at T + 6 d / close of the bar opening at T - 1 d), i.e. the
      return over week [T, T + 7 d) known at T + 7 d; ``dd7``: forward drawdown of daily lows over the week vs the
      close before T."""
    rows: list[dict[str, float | None]] = []
    for t, oi_t in zip(anchors, oi, strict=True):
        full = t + WEEK_MS <= now_ms
        fb = [window_funding_apr(*s, t, t + WEEK_MS) for s in funding]
        fy = [window_funding_apr(*s, t, t + WEEK_MS) for s in funding_alt]
        c_prev = value_asof(bar_t, bar_close, t - DAY_MS)
        c_end = value_asof(bar_t, bar_close, t + WEEK_MS - DAY_MS) if full else None
        rows.append(
            {
                "t": float(t),
                "usde": value_asof(usde_t, usde_v, t - usde_lag_ms),
                "usde_nolag": value_asof(usde_t, usde_v, t),
                "oi": oi_t,
                "f_btc": fb[0] if full else None,
                "f_eth": fb[1] if full else None,
                "f": _mean_full(fb, full),
                "f_byb": _mean_full(fy, full),
                "ret": math.log(c_end / c_prev) if c_end and c_prev else None,
                "dd7": forward_drawdown(bar_t, bar_low, c_prev, t, t + WEEK_MS) if c_prev and full else None,
            }
        )
    return rows


def h1_rows(
    w: Sequence[dict[str, float | None]], fkey: str = "f", ukey: str = "usde", shift: int = 0, norm: str = "oi"
) -> dict[str, list[float]]:
    """Weekly H1 design from :func:`weekly_panel` rows ``w`` (F_i = ``w[i][fkey]``, the funding of week [T_i, T_{i+1})).

    For row i (labelled ``t`` = T_i), all regressors are known at T_{i+1}:
      x_i = (USDe(T_{i+1}) - USDe(T_i)) / OI(T_{i+1}) with the panel's lagged USDe stamps (``norm="usde"``:
      divided by USDe(T_i) instead); c1 = F_i - F_{i-1}; c2 = F_{i-1} - F_{i-2}; c3 = BTC log return over week i;
      ``lvl`` = F_i. Target y_i = F_{i+1} - F_i (funding of [T_{i+1}, T_{i+2}) minus that of week i).
    ``shift`` moves x in time (placebo: x_{i+shift})."""

    def x_at(j: int) -> float | None:
        if j < 0 or j + 1 >= len(w):
            return None
        a, b, oi = w[j][ukey], w[j + 1][ukey], w[j + 1]["oi"]
        if a is None or b is None or not oi:
            return None
        return (b - a) / (oi if norm == "oi" else a)

    out: dict[str, list[float]] = {k: [] for k in ("t", "y", "x", "c1", "c2", "c3", "lvl")}
    for i in range(2, len(w) - 1):
        f = [w[i + 1][fkey], w[i][fkey], w[i - 1][fkey], w[i - 2][fkey]]
        x = x_at(i + shift)
        ret = w[i]["ret"]
        if None in f or x is None or ret is None:
            continue
        f1, f0, fm1, fm2 = (float(v) for v in f)  # type: ignore[arg-type]
        out["t"].append(float(w[i]["t"]))  # type: ignore[arg-type]
        out["y"].append(f1 - f0)
        out["x"].append(x)
        out["c1"].append(f0 - fm1)
        out["c2"].append(fm1 - fm2)
        out["c3"].append(float(ret))
        out["lvl"].append(f0)
    return out


def h1_rows_4w(w: Sequence[dict[str, float | None]], fkey: str = "f") -> dict[str, list[float]]:
    """4-week H1 variant (overlapping rows, one per week i, labelled T_i), regressors known at T_{i+1}:
    x = (USDe(T_{i+1}) - USDe(T_{i-3})) / OI(T_{i+1}); c1 = mean F(i-3..i) - mean F(i-7..i-4); c3 = sum of BTC log
    returns of weeks i-3..i; ``lvl`` = mean F(i-3..i). Target y = mean F(i+1..i+4) - mean F(i-3..i)."""
    out: dict[str, list[float]] = {k: [] for k in ("t", "y", "x", "c1", "c3", "lvl")}
    for i in range(7, len(w) - 4):
        fs = [w[j][fkey] for j in range(i - 7, i + 5)]
        rs = [w[j]["ret"] for j in range(i - 3, i + 1)]
        a, b, oi = w[i - 3]["usde"], w[i + 1]["usde"], w[i + 1]["oi"]
        if None in fs or None in rs or a is None or b is None or not oi:
            continue
        fv = [float(v) for v in fs]  # type: ignore[arg-type]
        nxt, cur, prv = sum(fv[8:12]) / 4, sum(fv[4:8]) / 4, sum(fv[0:4]) / 4
        out["t"].append(float(w[i]["t"]))  # type: ignore[arg-type]
        out["y"].append(nxt - cur)
        out["x"].append((b - a) / oi)
        out["c1"].append(cur - prv)
        out["c3"].append(float(sum(rs)))  # type: ignore[arg-type]
        out["lvl"].append(cur)
    return out


def tercile_top(values: Sequence[float]) -> list[bool]:
    """True where the value is in the top tercile of the sample (> 2/3 quantile)."""
    a = np.asarray(values, dtype=float)
    q = float(np.quantile(a, 2 / 3))
    return [bool(v > q) for v in a]


def expanding_tercile_top(values: Sequence[float], min_hist: int = 26) -> list[bool | None]:
    """Top-tercile flag using only values up to and including each point (None before ``min_hist`` points)."""
    out: list[bool | None] = []
    for i in range(len(values)):
        if i + 1 < min_hist:
            out.append(None)
            continue
        q = float(np.quantile(np.asarray(values[: i + 1], dtype=float), 2 / 3))
        out.append(bool(values[i] > q))
    return out
