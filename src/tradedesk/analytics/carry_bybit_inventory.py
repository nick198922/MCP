"""Item 8q: Bybit inventory for carry A around delisting announcements (counts only, no P&L).

Pure functions, no I/O; every time is a parameter (ms since epoch, UTC). Rates are decimals per funding payment;
APR is a decimal (0.20 = 20 %). Daily bars are ``(day_start_ms, close, volume)``; a bar with zero volume is not a
bar. Funding payments are ``(ts_ms, rate)``.

* ``trailing_apr`` — the strategy-A entry signal: sum of rates paid in ``(t - lookback; t]`` x 365 / lookback days,
  ``None`` without a full lookback of history (first payment later than ``t - lookback``).
* ``unit_inventory`` — one unit (a perp named by a delisting announcement, or a base whose spot was delisted):
  did the spot exist at the announcement, was the symbol "hot" before it, could A have entered after it, and which
  leg disappeared first. Causal in the APR: at cycle ``t`` only payments with ``ts <= t`` are used.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

DAY_MS = 86_400_000
STEP_MS = 8 * 3_600_000

Bar = tuple[int, float, float]  # (day_start_ms, close, volume)
Pay = tuple[int, float]  # (ts_ms, rate)


def active_days(bars: Sequence[Bar]) -> dict[int, float]:
    """``{day_start_ms: close}`` of the bars with volume > 0 and a positive close."""
    return {int(d) // DAY_MS * DAY_MS: float(c) for d, c, v in bars if float(v) > 0 and float(c) > 0}


def last_active_day(days: Mapping[int, float]) -> int | None:
    return max(days) if days else None


def has_bar_in(days: Mapping[int, float], lo: int, hi: int) -> bool:
    """A bar with volume whose day starts in ``[lo - 1 day; hi]`` (the day containing ``lo`` counts)."""
    return any(lo - DAY_MS < d <= hi for d in days)


def price_level_dev(perp: Mapping[int, float], spot: Mapping[int, float], mult: int, lo: int, hi: int) -> float | None:
    """Median ``|perp / (spot x mult) - 1|`` over common days in ``[lo; hi]``; ``None`` without a common day."""
    devs = [abs(perp[d] / (spot[d] * mult) - 1.0) for d in perp if lo <= d <= hi and d in spot]
    return statistics.median(devs) if devs else None


def trailing_apr(pay: Sequence[Pay], t: int, lookback_days: float = 7.0) -> float | None:
    """Sum of rates with ``t - lookback < ts <= t`` x 365 / lookback; ``None`` if history starts after ``t - lookback``."""
    if not pay:
        return None
    lb = int(lookback_days * DAY_MS)
    if pay[0][0] > t - lb:
        return None
    ts = [p[0] for p in pay]
    lo = bisect.bisect_right(ts, t - lb)
    hi = bisect.bisect_right(ts, t)
    return sum(p[1] for p in pay[lo:hi]) * 365.0 / lookback_days


def cycles(lo: int, hi: int, step: int = STEP_MS) -> list[int]:
    """Decision cycles (multiples of ``step``) in ``[lo; hi]``."""
    first = -(-lo // step) * step
    return list(range(first, hi + 1, step))


def _both_legs(t: int, perp: Mapping[int, float], spot: Mapping[int, float]) -> bool:
    d = t // DAY_MS * DAY_MS
    return d in perp and d in spot


def max_apr(pay: Sequence[Pay], lo: int, hi: int, perp: Mapping[int, float], spot: Mapping[int, float]) -> float | None:
    """Max trailing APR over cycles in ``[lo; hi)`` with full history and both legs traded that day."""
    vals = [a for t in cycles(lo, hi - 1) if _both_legs(t, perp, spot) and (a := trailing_apr(pay, t)) is not None]
    return max(vals) if vals else None


def first_entry_cycle(
    pay: Sequence[Pay], lo: int, hi: int, perp: Mapping[int, float], spot: Mapping[int, float], entry_apr: float = 0.20
) -> int | None:
    """First cycle in ``[lo; hi]`` where the A entry condition holds (APR >= entry_apr, both legs traded that day)."""
    for t in cycles(lo, hi):
        if not _both_legs(t, perp, spot):
            continue
        a = trailing_apr(pay, t)
        if a is not None and a >= entry_apr:
            return t
    return None


def unit_inventory(
    t_a: int,
    *,
    perp_bars: Sequence[Bar],
    spot_bars: Sequence[Bar] | None,
    funding: Sequence[Pay] | None,
    mult: int = 1,
    spot_empty_means_absent: bool = False,
    spot_by_announcement: bool = False,
    entry_apr: float = 0.20,
    hot_days: float = 180.0,
    window_days: float = 30.0,
    spot_days: float = 7.0,
    level_days: float = 60.0,
    max_level_dev: float = 0.05,
) -> dict[str, Any]:
    """Counters of one unit announced at ``t_a`` (see the item-8q registration in the idea card).

    ``spot_bars`` None = the spot request failed (not measured). An empty spot answer means "no spot" only when
    ``spot_empty_means_absent`` (the source was validated on delisted pairs); otherwise it is "not measured", unless
    ``spot_by_announcement`` (a spot-delisting announcement names the base) proves the spot existed.
    ``spot`` is 'yes' / 'no' / 'unmeasured' / 'collision'; the other fields are None unless ``spot == 'yes'``.
    """
    pd, sd = active_days(perp_bars), active_days(spot_bars or [])
    out: dict[str, Any] = {"spot": "unmeasured", "level_dev": None, "perp_end": last_active_day(pd), "spot_end": None}
    out |= {"hot": None, "max_apr_before": None, "entry_after": None, "first_leg_gone": None}
    if spot_bars is None:
        return out
    if not has_bar_in(sd, t_a - int(spot_days * DAY_MS), t_a):
        if spot_by_announcement:
            out["spot"] = "unmeasured"  # spot existed, but no bar near the announcement: cannot use it
        elif spot_empty_means_absent:
            out["spot"] = "no"
        return out
    dev = price_level_dev(pd, sd, mult, t_a - int(level_days * DAY_MS), t_a)
    out["level_dev"] = dev
    if dev is None or dev > max_level_dev:
        out["spot"] = "collision" if dev is not None else "unmeasured"
        return out
    out["spot"] = "yes"
    pe, se = out["perp_end"], last_active_day(sd)
    out["spot_end"] = se
    if pe is not None and se is not None:
        out["first_leg_gone"] = "perp" if pe < se else ("spot" if se < pe else "same_day")
    pay = list(funding or [])
    if not pay:
        return out  # funding not measured: hot / entry_after stay None
    m = max_apr(pay, t_a - int(hot_days * DAY_MS), t_a, pd, sd)
    out["max_apr_before"] = m
    out["hot"] = m is not None and m >= entry_apr
    end = t_a + int(window_days * DAY_MS)
    for leg_end in (pe, se):
        if leg_end is not None:
            end = min(end, leg_end + DAY_MS - 1)
    out["entry_after"] = first_entry_cycle(pay, t_a, end, pd, sd, entry_apr)
    return out


def slot_estimate(n_entry_after: int, n_iii_pool: int) -> dict[str, float]:
    """Rough 5-slot estimate with the Binance 8o / 8p ratios (registered): 1 / 8 ban events and 5 / 8 removed
    trades per unit with a no-slot entry after the announcement; 3 / 26 (iii) events per hot ended perp."""
    return {
        "ban_events": n_entry_after / 8.0,
        "removed_trades": n_entry_after * 5.0 / 8.0,
        "iii_events": n_iii_pool * 3.0 / 26.0,
    }
