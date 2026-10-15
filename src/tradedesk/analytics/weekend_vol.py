"""Weekend vs mid-week variance and straddle pricing on BTC (IDEA-20261014-weekend-vol-discount).

Pure functions, no I/O; time is always a parameter (epoch ms, UTC). Conventions (see the card's pre-registration):
  * a week is keyed by its Tuesday 00:00 UTC; control window Tue 16:00 -> Thu 08:00, weekend window
    Fri 16:00 -> Sun 08:00 (both 40 hours);
  * the price at time t is the close of the hourly bar opened at t - 1 h;
  * realized variance of a window = sum of squared hourly log returns, not annualized, no mean adjustment;
  * implied variance of a window = IV^2 * T with IV a decimal (0.45 = 45 %) and T in years (days / 365);
  * straddle results are fractions of entry spot per 1 BTC notional (0.01 = 1 % of spot).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS

T = TypeVar("T")


def prices_from_bars(bars: Sequence[Sequence[float]], *, step_ms: int = HOUR_MS) -> dict[int, float]:
    """``{t: close}`` where t is the close time (open + step) of each bar ``[open_ms, open, high, low, close, ...]``."""
    return {int(b[0]) + step_ms: float(b[4]) for b in bars if b and float(b[4]) > 0}


def window_rv(prices: Mapping[int, float], start_ms: int, end_ms: int, *, step_ms: int = HOUR_MS) -> float | None:
    """Sum of squared log returns on the grid start, start + step, ..., end; None if any grid price is missing."""
    if end_ms <= start_ms or (end_ms - start_ms) % step_ms:
        raise ValueError("window must be a positive multiple of the step")
    prev = prices.get(start_ms)
    if prev is None:
        return None
    s = 0.0
    for t in range(start_ms + step_ms, end_ms + 1, step_ms):
        p = prices.get(t)
        if p is None:
            return None
        s += math.log(p / prev) ** 2
        prev = p
    return s


def week_windows(tuesday_ms: int) -> dict[str, tuple[int, int]]:
    """Control (Tue 16:00 -> Thu 08:00) and weekend (Fri 16:00 -> Sun 08:00) windows of the week keyed by its Tuesday."""
    return {
        "ctrl": (tuesday_ms + 16 * HOUR_MS, tuesday_ms + 2 * DAY_MS + 8 * HOUR_MS),
        "wkd": (tuesday_ms + 3 * DAY_MS + 16 * HOUR_MS, tuesday_ms + 5 * DAY_MS + 8 * HOUR_MS),
    }


def weekend_share(prices: Mapping[int, float], monday_ms: int) -> tuple[float, float] | None:
    """(variance Sat 08:00 -> Mon 08:00, variance of the whole week Mon 08:00 -> next Mon 08:00); None on gaps."""
    week = window_rv(prices, monday_ms + 8 * HOUR_MS, monday_ms + WEEK_MS + 8 * HOUR_MS)
    wk = window_rv(prices, monday_ms + 5 * DAY_MS + 8 * HOUR_MS, monday_ms + WEEK_MS + 8 * HOUR_MS)
    if week is None or wk is None:
        return None
    return wk, week


def event_weeks(event_days_ms: Sequence[int], weekdays: Sequence[int]) -> set[int]:
    """Tuesdays (00:00 UTC) of weeks with an event on one of ``weekdays`` (0 = Tuesday, 1 = Wednesday, ..., 3 = Friday)."""
    out: set[int] = set()
    for ms in event_days_ms:
        d = (int(ms) // DAY_MS) * DAY_MS
        # 1970-01-06 was a Tuesday: offset of d from the Tuesday on or before it
        off = ((d // DAY_MS) - 5) % 7
        if off in weekdays:
            out.add(d - off * DAY_MS)
    return out


def pooled_ratio(rows: Sequence[tuple[float, float]]) -> float:
    """Sum of the first column over the sum of the second (pooled ratio)."""
    den = sum(b for _, b in rows)
    return sum(a for a, _ in rows) / den if den else float("nan")


def ratio_diff(rows: Sequence[tuple[float, float, float, float]]) -> float:
    """D = r_IV - r_RV with rows (implied_wkd, implied_ctrl, realized_wkd, realized_ctrl), both ratios pooled."""
    return pooled_ratio([(a, b) for a, b, _, _ in rows]) - pooled_ratio([(c, d) for _, _, c, d in rows])


def median_weekly_diff(rows: Sequence[tuple[float, float, float, float]]) -> float:
    """Median over weeks of (implied_wkd / implied_ctrl - realized_wkd / realized_ctrl): the card's original, heavy-tailed estimator."""
    v = sorted(a / b - c / d for a, b, c, d in rows if b > 0 and d > 0)
    if not v:
        return float("nan")
    m = len(v) // 2
    return v[m] if len(v) % 2 else 0.5 * (v[m - 1] + v[m])


def block_bootstrap(
    rows: Sequence[T], stat: Callable[[Sequence[T]], float], *, n_boot: int = 10_000, seed: int = 20261015
) -> dict[str, Any]:
    """Point statistic and percentile 95 % CI, resampling whole rows (one row = one week = one block)."""
    n = len(rows)
    point = stat(rows) if n else float("nan")
    if n < 2:
        return {"n": n, "stat": point, "ci95": None, "se": None}
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        vals.append(stat([rows[rng.randrange(n)] for _ in range(n)]))
    vals = sorted(v for v in vals if not math.isnan(v))
    mean = sum(vals) / len(vals)
    se = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1))
    return {"n": n, "stat": point, "ci95": (vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]), "se": se}


def mean(rows: Sequence[float]) -> float:
    return sum(rows) / len(rows) if rows else float("nan")


def h0_gate(pooled: float, ci_hi: float, yearly: Mapping[str, float], *, r_max: float = 0.79, years_min: int = 5) -> dict[str, Any]:
    """Pre-registered stage-0 gate: pooled ratio <= r_max, CI upper bound < 1 and per-year ratio <= r_max in >= years_min years."""
    ok_years = sum(1 for v in yearly.values() if v <= r_max)
    return {
        "pooled_ok": pooled <= r_max,
        "ci_ok": ci_hi < 1.0,
        "years_ok": ok_years >= years_min,
        "years_passed": ok_years,
        "passed": pooled <= r_max and ci_hi < 1.0 and ok_years >= years_min,
    }


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def straddle_delta(F: float, K: float, T_years: float, sigma: float) -> float:
    """Black-76 delta of a long call + put on the same strike (r = 0): 2 N(d1) - 1; at expiry the sign of F - K."""
    if T_years <= 0 or sigma <= 0:
        return 1.0 if F > K else (-1.0 if F < K else 0.0)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T_years) / (sigma * math.sqrt(T_years))
    return 2.0 * _ncdf(d1) - 1.0


def hedged_short_straddle(
    q: Mapping[str, Any],
    prices: Mapping[int, float],
    s_t: float,
    *,
    rebalance_h: int = 4,
    fee: float = 0.0005,
) -> float | None:
    """Short straddle delta-hedged with spot every ``rebalance_h`` hours from t0 to expiry, fraction of S0 (exploratory).

    Delta from Black-76 with the entry IV (mean of the legs) on the hourly spot ``prices``; the hedge is opened at t0,
    re-set at each rebalance and closed at the expiry price; ``fee`` is charged on traded hedge notional. No funding.
    """
    t0, exp, K, iv = int(q["t0"]), int(q["expiry"]), float(q["K"]), 0.5 * (float(q["iv_call"]) + float(q["iv_put"]))
    grid = list(range(t0, exp, rebalance_h * HOUR_MS)) + [exp]
    px = [prices.get(t) for t in grid]
    if any(p is None for p in px):
        return None
    pnl, cost, h_prev = 0.0, 0.0, 0.0
    for i, t in enumerate(grid[:-1]):
        s = float(px[i])  # type: ignore[arg-type]
        h = straddle_delta(s, K, (exp - t) / (365.0 * DAY_MS), iv)
        cost += fee * abs(h - h_prev) * s
        pnl += h * (float(px[i + 1]) - s)  # type: ignore[arg-type]
        h_prev = h
    cost += fee * abs(h_prev) * float(px[-1])  # type: ignore[arg-type]
    short = float(q["premium"]) - abs(s_t - K)
    return (short + pnl - cost) / float(q["spot"])


def window_extremes(bars: Sequence[Sequence[float]], start_ms: int, end_ms: int) -> tuple[float, float] | None:
    """(lowest low, highest high) of hourly bars opened in [start_ms, end_ms); None if none."""
    sel = [b for b in bars if start_ms <= int(b[0]) < end_ms]
    if not sel:
        return None
    return min(float(b[3]) for b in sel), max(float(b[2]) for b in sel)
