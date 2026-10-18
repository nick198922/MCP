"""Slow macro and crypto on monthly horizons (IDEA-20261017-macro, part B3). Pure functions, no I/O.

Conventions:
  * a month key is ``date(year, month, 1)``; a month's value of a daily series is its last observation on or before
    the month's last day (and inside that month);
  * changes, never levels: log changes for prices, money stock and the dollar index; differences for yields
    (decimals, 0.0412 = 4.12 %);
  * a predictive pair for month m uses the signal of month m - lag (known before m starts) and the return of m;
  * the M2 timing rule uses only signal values already published before the month it trades.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

import numpy as np

# ----------------------------------------------------------------------------- months


def month_key(d: date) -> date:
    return date(d.year, d.month, 1)


def add_months(m: date, k: int) -> date:
    y, mo = divmod(m.year * 12 + (m.month - 1) + k, 12)
    return date(y, mo + 1, 1)


def month_range(first: date, last: date) -> list[date]:
    """Month keys from ``first`` to ``last`` inclusive."""
    out, m = [], month_key(first)
    while m <= month_key(last):
        out.append(m)
        m = add_months(m, 1)
    return out


def month_end_values(daily: Mapping[date, float | None]) -> dict[date, float]:
    """{month key: value of the last non-missing observation dated inside that month}."""
    best: dict[date, tuple[date, float]] = {}
    for d, v in daily.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        k = month_key(d)
        if k not in best or d > best[k][0]:
            best[k] = (d, float(v))
    return {k: v for k, (_, v) in best.items()}


def candle_month_close(ohlcv: Sequence[Sequence[float]]) -> dict[date, float]:
    """Daily candles ``[open_ms, o, h, l, c, v]`` (UTC) -> {month key: close of the month's last daily candle}.

    The close of the candle that opens at 00:00 UTC on the last day is the price at 00:00 UTC of the next month."""
    daily = {datetime.fromtimestamp(int(r[0]) / 1000, tz=UTC).date(): float(r[4]) for r in ohlcv}
    return month_end_values(daily)


def changes(values: Mapping[date, float], *, kind: str = "log", span: int = 1) -> dict[date, float]:
    """{m: change from month m - span to month m} for months where both ends exist."""
    out: dict[date, float] = {}
    for m, v in values.items():
        p = values.get(add_months(m, -span))
        if p is None:
            continue
        if kind == "log":
            if p > 0 and v > 0:
                out[m] = math.log(v / p)
        else:
            out[m] = v - p
    return out


def lagged_pairs(
    signal: Mapping[date, float], ret: Mapping[date, float], months: Sequence[date], lag: int
) -> list[tuple[date, float, float]]:
    """[(m, signal[m - lag], ret[m])] for m in ``months`` where both exist (lag 0 = same month)."""
    out = []
    for m in months:
        s, r = signal.get(add_months(m, -lag)), ret.get(m)
        if s is not None and r is not None:
            out.append((m, s, r))
    return out


# ----------------------------------------------------------------------------- parsers (pure)

_OBS = re.compile(r"<frb:Obs\s+([^>]*?)/?>")
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def parse_frb_series(xml: str, series_name: str) -> dict[date, float]:
    """Federal Reserve Data Download Program SDMX XML (H.6, H.10 packages) -> {date: value} for ``series_name``.

    Observations with status other than 'A' or non-numeric values (ND, NA) are dropped."""
    m = re.search(r'<kf:Series\b[^>]*SERIES_NAME="' + re.escape(series_name) + r'"[^>]*>', xml)
    if not m:
        return {}
    end = xml.find("</kf:Series>", m.end())
    block = xml[m.end() : end if end >= 0 else len(xml)]
    out: dict[date, float] = {}
    for o in _OBS.finditer(block):
        a = dict(_ATTR.findall(o.group(1)))
        if a.get("OBS_STATUS", "A") != "A":
            continue
        try:
            v = float(a["OBS_VALUE"])
            d = date.fromisoformat(a["TIME_PERIOD"])
        except (KeyError, ValueError):
            continue
        if math.isnan(v):
            continue
        out[d] = v
    return out


# ----------------------------------------------------------------------------- statistics


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    from tradedesk.analytics.macro_regimes import pearson

    def rank(a: np.ndarray) -> np.ndarray:
        order = a.argsort(kind="mergesort")
        r = np.empty(len(a), float)
        r[order] = np.arange(len(a), dtype=float)
        # average ties
        for v in np.unique(a):
            idx = np.where(a == v)[0]
            if len(idx) > 1:
                r[idx] = r[idx].mean()
        return r

    a, b = np.asarray(x, float), np.asarray(y, float)
    return pearson(rank(a), rank(b))


def block_bootstrap(n: int, stat: Any, *, block: int = 6, reps: int = 2000, seed: int = 20261018) -> dict[str, Any]:
    """Moving-block bootstrap of row indices: point ``stat(arange(n))``, percentile 95 % and 99 % CI and SE."""
    point = stat(np.arange(n)) if n else float("nan")
    res: dict[str, Any] = {"n": n, "stat": point, "ci95": None, "ci99": None, "se": None}
    if n < max(3, block):
        return res
    rng = np.random.default_rng(seed)
    k = -(-n // block)
    vals = []
    for _ in range(reps):
        starts = rng.integers(0, n - block + 1, size=k)
        v = stat((starts[:, None] + np.arange(block)[None, :]).ravel()[:n])
        if not math.isnan(v):
            vals.append(v)
    if len(vals) < 10:
        return res
    a = np.sort(np.array(vals))
    res["ci95"] = (float(np.quantile(a, 0.025)), float(np.quantile(a, 0.975)))
    res["ci99"] = (float(np.quantile(a, 0.005)), float(np.quantile(a, 0.995)))
    res["se"] = float(a.std(ddof=1))
    return res


def circular_shift_null(x: Sequence[float], y: Sequence[float], *, min_shift: int = 6) -> dict[str, float]:
    """Correlation of x and y against the null of circular shifts of x by ``min_shift`` .. n - min_shift rows.

    Shifting keeps the autocorrelation of each series and breaks only their alignment (a permutation null that
    does not assume independent rows). Returns the observed correlation, the two-sided share of shifts with
    |corr| >= |observed| and the SD of the null correlations."""
    from tradedesk.analytics.macro_regimes import pearson

    a, b = np.asarray(x, float), np.asarray(y, float)
    n = len(a)
    obs = pearson(a, b)
    if n < 2 * min_shift + 2:
        return {"stat": obs, "p": float("nan"), "sd": float("nan"), "n": n}
    null = np.array([pearson(np.roll(a, k), b) for k in range(min_shift, n - min_shift + 1)])
    return {"stat": obs, "p": float((np.abs(null) >= abs(obs) - 1e-12).mean()), "sd": float(null.std()), "n": n}


def mde(se: float | None, k: float = 2.8) -> float:
    """Minimum detectable effect at 80 % power, two-sided 5 % (≈ 2.8 standard errors)."""
    return float("nan") if se is None else k * se


def predictive_verdict(
    stat: float, ci95: tuple[float, float] | None, half2: float, expected_sign: int, mde_value: float, *, gate: float = 0.30
) -> str:
    """Registered rule for P1-P4 (IDEA-20261017-macro B3).

    ✓ CI excludes 0 with the expected sign and the second-half (OOS) estimate has the same sign;
    ≈ CI excludes 0 with the expected sign, second half disagrees; '✗ протилежний знак' CI excludes 0 the other way;
    ✗ CI includes 0 and MDE <= gate; ◯ otherwise (CI includes 0 and MDE > gate, or no CI)."""
    if ci95 is None or math.isnan(stat):
        return "◯"
    lo, hi = ci95[0] * expected_sign, ci95[1] * expected_sign
    lo, hi = min(lo, hi), max(lo, hi)
    if lo > 0:
        return "✓" if half2 * expected_sign > 0 else "≈"
    if hi < 0:
        return "✗ протилежний знак"
    return "✗" if mde_value <= gate else "◯"


def phase_verdict(
    stat: float,
    ci95: tuple[float, float] | None,
    n_eps_cut: int,
    n_eps_hike: int,
    halves: Sequence[float],
    mde_value: float,
    *,
    gate: float = 0.05,
) -> str:
    """Registered rule for P5: d = mean r(cut months) - mean r(hike months), expected > 0.

    ``n_eps_*`` count episodes of >= 3 months; ``halves`` the d estimate in each half (NaN when not estimable)."""
    if ci95 is None or math.isnan(stat):
        return "◯"
    lo, hi = ci95
    if lo > 0:
        ok = n_eps_cut >= 2 and n_eps_hike >= 2 and all(not math.isnan(h) and h > 0 for h in halves)
        return "✓" if ok else "≈"
    if hi < 0:
        return "✗ протилежний знак"
    return "✗" if mde_value <= gate else "◯"


# ----------------------------------------------------------------------------- M2 timing rule


def timing_positions(
    signal: Mapping[date, float], months: Sequence[date], *, lag: int = 2, window: int = 60, min_hist: int = 36
) -> dict[date, int | None]:
    """Position for month m: 1 if signal[m - lag] > median of signal over the ``window`` months m-lag-window .. m-lag-1
    (at least ``min_hist`` present), else 0; ``None`` when not enough history or the signal is missing.

    Uses only signal values dated m - lag or earlier, so a month's position never depends on later data."""
    out: dict[date, int | None] = {}
    for m in months:
        s = signal.get(add_months(m, -lag))
        hist = [signal[k] for j in range(1, window + 1) if (k := add_months(m, -lag - j)) in signal]
        if s is None or len(hist) < min_hist:
            out[m] = None
            continue
        out[m] = 1 if s > float(np.median(hist)) else 0
    return out


def timing_returns(positions: Sequence[int], log_ret: Sequence[float], *, cost: float = 0.001) -> list[float]:
    """Net simple monthly returns on capital of a long/flat spot rule: pos * (e^r - 1) - cost * |pos - prev pos|.

    Rows are consecutive months; the first row pays for entering if it starts long."""
    out, prev = [], 0
    for p, r in zip(positions, log_ret, strict=True):
        out.append(p * (math.exp(r) - 1.0) - cost * abs(p - prev))
        prev = p
    return out


def exposure_alpha(net: np.ndarray, pos: np.ndarray, log_ret: np.ndarray) -> float:
    """Mean net rule return minus f x mean buy-and-hold simple return over the same months (f = share of months long)."""
    if len(net) == 0:
        return float("nan")
    bh = np.expm1(log_ret)
    return float(net.mean() - pos.mean() * bh.mean())


def rule_alpha_stat(pos: np.ndarray, log_ret: np.ndarray, cost: float) -> Any:
    """Closure for the block bootstrap: exposure alpha on a resample of month indices (costs from the resampled order).

    Position changes are re-priced on the resampled sequence, which slightly overstates turnover at block joins
    (conservative for the rule)."""

    def stat(i: np.ndarray) -> float:
        p, r = pos[i], log_ret[i]
        net = np.array(timing_returns(list(p.astype(int)), list(r), cost=cost))
        return exposure_alpha(net, p, r)

    return stat
