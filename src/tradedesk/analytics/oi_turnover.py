"""OI-to-turnover "leverage in the book" signal for a weekly cross-section of USDT-M perps.

Idea card: docs/ideas/IDEA-20261028-oi-turnover-leverage-short-basket.md. Pure functions, no I/O; time is a parameter.

L(t) = OI value in USD at the end of day t-1 (archive ``metrics`` ``sum_open_interest_value``, last row observed no
later than 23:55 UTC) / median daily quote volume (USD) of days t-7 .. t-1. Units: L in days of turnover (0.5 = OI
equals half a typical day's volume); returns and volatilities are decimals (0.03 = 3 %).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence

import numpy as np

from .funding_crowding import Bars
from .oi_flush import SEMANTICS_SWITCH_MS, archive_obs_times
from .xs_factors import rank_ic, residualize

DAY_MS = 86_400_000
MIN_MS = 60_000
OI_VALUE_COL = 2  # providers.binance_vision.parse_metrics_ratios_zip: [create_ms, oi, oi_value, top_acc, top_pos, crowd, taker]
OI_COL = 1


def oi_value_snapshot(
    rows: Sequence[Sequence[float]], day: int, *, margin_ms: int = 5 * MIN_MS, switch_ms: int = SEMANTICS_SWITCH_MS
) -> dict[str, float] | None:
    """Last row of UTC ``day`` observed in ``[day 00:00; day end - margin_ms]`` with a finite positive OI value.

    Observation times follow ``oi_flush.archive_obs_times`` (create_time, +5 min after the 2024-03-04 switch).
    Returns {"obs_ms", "oi", "oi_value"} (contracts in coins, USD) or None."""
    if not rows:
        return None
    lo, cutoff = day * DAY_MS, (day + 1) * DAY_MS - margin_ms
    obs = archive_obs_times(np.array([r[0] for r in rows], dtype=np.int64), switch_ms=switch_ms)
    for i in np.argsort(obs, kind="stable")[::-1]:
        if obs[i] > cutoff or obs[i] < lo:
            continue
        r = rows[int(i)]
        v = float(r[OI_VALUE_COL])
        if math.isfinite(v) and v > 0:
            return {"obs_ms": float(obs[i]), "oi": float(r[OI_COL]), "oi_value": v}
    return None


def median_qv(bars: Bars, t: int, *, window: int = 7, min_days: int = 5) -> float | None:
    """Median daily quote volume (USD) of days ``t - window .. t - 1``; None with fewer than ``min_days`` bars."""
    vals = [bars.qv[d] for d in range(t - window, t) if d in bars.qv]
    return statistics.median(vals) if len(vals) >= min_days else None


def realized_vol(bars: Bars, t: int, *, window: int = 30, min_days: int = 20) -> float | None:
    """SD (ddof 1) of daily close-to-close log returns of days ``t - window .. t - 1`` (daily decimal, not annualised)."""
    r = [math.log(bars.close[d] / bars.close[d - 1]) for d in range(t - window, t) if d in bars.close and d - 1 in bars.close]
    return statistics.stdev(r) if len(r) >= max(min_days, 2) else None


def leverage_ratio(oi_value: float | None, med_qv: float | None) -> float | None:
    """L = OI value / median daily turnover (both USD); None when either is missing or non-positive."""
    if oi_value is None or med_qv is None or oi_value <= 0 or med_qv <= 0:
        return None
    return oi_value / med_qv


def neutralized_signal(log_l: Sequence[float], controls: Sequence[Sequence[float]]) -> np.ndarray:
    """Residual of the cross-sectional OLS of log L on [1, controls...] (e.g. log turnover, log 30-d vol)."""
    return residualize(np.asarray(log_l, dtype=float), [np.asarray(c, dtype=float) for c in controls])


def beta_residual(r: float, beta: float, r_btc: float) -> float:
    """Return net of the BTC beta exposure: r - beta * r_btc (a BTC hedge of size beta)."""
    return r - beta * r_btc


def quintiles(signal: Mapping[str, float], *, q: int = 5) -> tuple[list[str], list[str]]:
    """(bottom, top): the floor(N / q) lowest and highest signals (ties by symbol); empty if N < q."""
    n = len(signal) // q
    if n == 0:
        return [], []
    order = sorted(signal, key=lambda s: (signal[s], s))
    return order[:n], order[-n:]


def weekly_ics(weeks: Sequence[tuple[Sequence[float], Sequence[float]]]) -> list[float]:
    """Spearman IC per week of (signal, return) pairs."""
    return [rank_ic(s, r) for s, r in weeks]


def turnover(prev: Sequence[str], cur: Sequence[str]) -> float:
    """One-side turnover of an equal-weight basket per unit of its notional: share of names replaced, times 2 (exit +
    entry), plus full entry when there was no previous basket. Drift is ignored."""
    if not cur:
        return 0.0 if not prev else 1.0
    if not prev:
        return 1.0
    w_prev, w_cur = 1.0 / len(prev), 1.0 / len(cur)
    p, c = set(prev), set(cur)
    return sum(abs((w_cur if x in c else 0.0) - (w_prev if x in p else 0.0)) for x in p | c)


def split_diff(xs: Sequence[float], flags: Sequence[bool]) -> tuple[float, float, int, int]:
    """(mean(flagged) - mean(rest), Welch SE of the difference, n_flagged, n_rest); nan when a side has < 2 values."""
    a = [x for x, f in zip(xs, flags, strict=True) if f]
    b = [x for x, f in zip(xs, flags, strict=True) if not f]
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan"), len(a), len(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return statistics.fmean(a) - statistics.fmean(b), se, len(a), len(b)
