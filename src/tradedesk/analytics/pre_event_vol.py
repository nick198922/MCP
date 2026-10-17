"""Pre-event implied-vol ramp on Deribit BTC DVOL (IDEA-20261017-pre-event-vol-ramp).

Pure functions, no I/O; time is always a parameter. Conventions (see the card's pre-registration):
  * DVOL is a decimal (0.45 = 45 %); one vol point = 0.01; results stay decimals, reports multiply by 100;
  * Deribit daily DVOL candles are stamped at 00:00 UTC of their day and close at 24:00 UTC, so the close of
    the candle stamped ``d`` is the DVOL level at ``d + 1 day 00:00 UTC``;
  * the primary window of an event day ``T0`` is ``close(T0 - 4) -> close(T0 - 1)``, i.e. ``T0 - 3 00:00 UTC ->
    T0 00:00 UTC`` (fully before every FOMC / CPI / NFP release);
  * controls are pseudo-event days ``T0 +- 7k`` (same weekday) whose window holds no event (+-1 day) and no US
    federal holiday or 24.12-01.01 day.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def day_of(ms: int) -> int:
    return (int(ms) // DAY_MS) * DAY_MS


def ms_of(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def date_of(ms: int) -> date:
    return datetime.fromtimestamp(int(ms) / 1000, UTC).date()


# ----------------------------------------------------------------------------- DVOL windows


def daily_closes(rows: Iterable[Sequence[float]]) -> dict[int, float]:
    """``[[candle_ts_ms, close], ...]`` -> {candle day (00:00 UTC ms): close}."""
    return {day_of(int(t)): float(v) for t, v in rows}


def window_change(closes: Mapping[int, float], t0_day: int, *, start_back: int = 4, end_back: int = 1) -> float | None:
    """``close(T0 - end_back) - close(T0 - start_back)`` (defaults: T0-3 00:00 -> T0 00:00 UTC); None if a close is missing.

    Reads only candles that close at or before ``T0 - end_back + 1 day``: later data cannot change the result.
    """
    a = closes.get(t0_day - start_back * DAY_MS)
    b = closes.get(t0_day - end_back * DAY_MS)
    if a is None or b is None:
        return None
    return b - a


def level_profile(closes: Mapping[int, float], t0_day: int, offsets: Sequence[int], *, base_back: int = 6) -> dict[int, float | None]:
    """DVOL at ``T0 + k`` 00:00 UTC minus DVOL at ``T0 - base_back + 1`` 00:00 UTC, for each k in ``offsets``.

    Level at ``T0 + k`` 00:00 = close of the candle stamped ``T0 + k - 1``.
    """
    base = closes.get(t0_day - base_back * DAY_MS)
    out: dict[int, float | None] = {}
    for k in offsets:
        v = closes.get(t0_day + (k - 1) * DAY_MS)
        out[k] = None if base is None or v is None else v - base
    return out


def hourly_level(hourly: Mapping[int, float], t_ms: int) -> float | None:
    """DVOL level at ``t_ms`` from hourly closes keyed by CLOSE time (ms) = candle open + 1 h.

    This is the convention of the research cache written by ``scripts/verify_pm_threshold.py`` (key = moment the
    hourly close is known); the close of the daily candle stamped ``d`` equals the hourly value keyed ``d + 24 h``.
    """
    return hourly.get(int(t_ms))


def hourly_window(hourly: Mapping[int, float], start_ms: int, end_ms: int) -> dict[str, Any]:
    """Hourly change level(end) - level(start) with a completeness count of hourly closes keyed in [start, end]."""
    expected = (end_ms - start_ms) // HOUR_MS + 1
    present = sum(1 for t in range(start_ms, end_ms + HOUR_MS, HOUR_MS) if t in hourly)
    a, b = hourly_level(hourly, start_ms), hourly_level(hourly, end_ms)
    return {"change": None if a is None or b is None else b - a, "hours": present, "expected": expected, "complete": present == expected}


# ----------------------------------------------------------------------------- events, holidays, controls


def merge_events(events: Iterable[tuple[int, str]], *, gap_days: int = 3) -> list[dict[str, Any]]:
    """Chain events whose dates are within ``gap_days`` of the previous one into one window.

    Returns time-sorted ``{"day": earliest day ms, "kind": single kind or 'multi', "kinds": sorted kinds, "days": [...]}``.
    The window of a merged record ends before its first release.
    """
    pts = sorted({(day_of(d), k) for d, k in events})
    out: list[dict[str, Any]] = []
    for d, k in pts:
        if out and d - out[-1]["days"][-1] <= gap_days * DAY_MS:
            g = out[-1]
            if d not in g["days"]:
                g["days"].append(d)
            g["kinds"] = sorted(set(g["kinds"]) | {k})
        else:
            out.append({"day": d, "days": [d], "kinds": [k]})
    for g in out:
        g["kind"] = g["kinds"][0] if len(g["kinds"]) == 1 else "multi"
    return out


def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, weekday: int) -> date:
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def us_federal_holidays(year: int) -> set[date]:
    """US federal holidays (observed dates) of a year; Juneteenth from 2021."""
    h = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # MLK
        _nth_weekday(year, 2, 0, 3),  # Presidents
        _last_weekday(year, 5, 0),  # Memorial
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor
        _nth_weekday(year, 10, 0, 2),  # Columbus
        _observed(date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2021:
        h.add(_observed(date(year, 6, 19)))
    # New Year of the next year observed on Dec 31 of this year
    nxt = _observed(date(year + 1, 1, 1))
    if nxt.year == year:
        h.add(nxt)
    return h


def is_quiet_excluded(d: date) -> bool:
    """True for a US federal holiday (observed) or a day in 24.12-01.01."""
    if (d.month == 12 and d.day >= 24) or (d.month == 1 and d.day == 1):
        return True
    return d in us_federal_holidays(d.year)


def control_ok(
    tc_day: int,
    event_days: set[int],
    has_data: Callable[[int], bool],
    *,
    window_back: int = 3,
    pad_days: int = 1,
) -> bool:
    """A pseudo-event day is a valid control if its window ``[Tc - window_back, Tc]`` has data, no event within
    ``pad_days`` of any window day and no holiday / year-end day."""
    if not has_data(tc_day):
        return False
    for k in range(-window_back - pad_days, pad_days + 1):
        if tc_day + k * DAY_MS in event_days:
            return False
    return not any(is_quiet_excluded(date_of(tc_day - k * DAY_MS)) for k in range(window_back + 1))


def pick_controls(t0_day: int, ok: Callable[[int], bool], *, n: int = 6, max_weeks: int = 8) -> list[int]:
    """Up to ``n`` valid same-weekday pseudo-event days nearest to ``t0_day`` (|k| weeks, k <= max_weeks; ties: earlier first)."""
    out: list[int] = []
    for k in range(1, max_weeks + 1):
        for c in (t0_day - k * 7 * DAY_MS, t0_day + k * 7 * DAY_MS):
            if ok(c):
                out.append(c)
                if len(out) == n:
                    return out
    return out


def event_minus_control(event_value: float | None, control_values: Sequence[float | None], *, min_controls: int = 3) -> float | None:
    """Event change minus the mean change of its controls; None if the event value is missing or < ``min_controls`` controls."""
    cv = [v for v in control_values if v is not None]
    if event_value is None or len(cv) < min_controls:
        return None
    return event_value - statistics.fmean(cv)


# ----------------------------------------------------------------------------- gates and verdicts


def precision_gate(
    dx: Sequence[float], *, hw_max: float = 0.010, mde_max: float = 0.015, z_ci: float = 1.96, z_mde: float = 2.8
) -> dict[str, Any]:
    """Pre-registered gate on the actual SD (no mean): half-width = z_ci*SD/sqrt(n) <= hw_max and MDE = z_mde*SD/sqrt(n) <= mde_max."""
    n = len(dx)
    if n < 2:
        return {"n": n, "sd": float("nan"), "half_width": float("inf"), "mde": float("inf"), "passed": False}
    sd = statistics.stdev(dx)
    hw, mde = z_ci * sd / math.sqrt(n), z_mde * sd / math.sqrt(n)
    return {"n": n, "sd": sd, "half_width": hw, "mde": mde, "passed": hw <= hw_max and mde <= mde_max}


def thesis_verdict(mean: float, ci: tuple[float, float], *, null_max: float = 0.005) -> str:
    """Course thesis 'IV rises before the event': ✓ if CI low > 0; ✗ if CI high < null_max; else ≈ (mean > 0) or ◯."""
    lo, hi = ci
    if lo > 0:
        return "✓"
    if hi < null_max:
        return "✗"
    return "≈" if mean > 0 else "◯"


def trading_verdict(
    mean: float,
    ci: tuple[float, float],
    *,
    threshold: float = 0.015,
    sign_fomc: float | None,
    sign_cpi: float | None,
    mean_drop_top3: float,
    mean_late: float | None,
    drop_top_min: float = 0.005,
) -> dict[str, Any]:
    """H1 trading rule: pass iff mean >= threshold and CI low > 0 and no kill condition fires.

    Kill conditions: FOMC and CPI means of different sign; mean without the top-3 events < ``drop_top_min``;
    the late sub-period (2023-2026) mean not positive (effect only in 2021-2022 gives no trading conclusion).
    """
    reasons = []
    if mean < threshold:
        reasons.append("mean_below_threshold")
    if ci[0] <= 0:
        reasons.append("ci_low_not_positive")
    if sign_fomc is not None and sign_cpi is not None and (sign_fomc > 0) != (sign_cpi > 0):
        reasons.append("fomc_cpi_sign_differs")
    if mean_drop_top3 < drop_top_min:
        reasons.append("drop_top3_below_min")
    if mean_late is None or mean_late <= 0:
        reasons.append("late_period_not_positive")
    return {"passed": not reasons, "reasons": reasons}


def straddle_vega_frac(days: float = 30.0) -> float:
    """ATM straddle vega per 1 vol point as a fraction of spot: 2 * phi(0) * sqrt(T) / 100 (Black-76, K = F)."""
    return 2.0 * math.sqrt(days / 365.0) / math.sqrt(2.0 * math.pi) / 100.0
