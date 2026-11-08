"""S12 v4 audit modules (L: sweep of a 1h fractal low in the gap; A: hours accepted above the middle)."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics import s12_audit as au

H = au.HOUR_MS


def _t(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.int64) * H


def test_fractal_lows_strict_and_bounded() -> None:
    low = np.array([5.0, 4.0, 3.0, 4.0, 3.0, 3.0, 4.0, 2.0, 5.0])
    # k=2: 3 < 4, 3 < 4 -> fractal; k=4/5: equal lows -> not strict; k=7: 2 < 4, 2 < 5 -> fractal
    assert au.fractal_lows(_t(9), low, 0, 9).tolist() == [2, 7]
    # k + 1 must be before `end`: with end = 8 the bar k=7 is not confirmed
    assert au.fractal_lows(_t(9), low, 0, 8).tolist() == [2]
    assert au.fractal_lows(_t(9), low, 3, 9).tolist() == [7]


def test_fractal_needs_consecutive_hours() -> None:
    t = _t(5).copy()
    t[3:] += H  # hole between bars 2 and 3
    low = np.array([5.0, 4.0, 3.0, 4.0, 5.0])
    assert au.fractal_lows(t, low, 0, 5).size == 0


def test_sweep_level_highest_unswept_in_band() -> None:
    # gap: full 10, mid 12; fractals at 11 (k=2) and 10.5 (k=6); 13 (k=10) is above mid
    low = np.array([12, 11.5, 11, 11.8, 12.5, 11, 10.5, 11.2, 13.5, 14, 13, 14, 15], dtype=float)
    # k=2 (11) is swept by k=5 (11 <= 11) -> the nearest unswept is 10.5
    assert au.sweep_level(_t(13), low, 0, 13, 10.0, 12.0) == 10.5
    # the lower fractal first (10.5 at k=2), a higher one later (11 at k=6): both unswept, the nearest is 11
    low2 = np.array([12, 11.5, 10.5, 11.8, 12.5, 11.5, 11, 11.2, 13.5, 14, 13, 14, 15], dtype=float)
    assert au.sweep_level(_t(13), low2, 0, 13, 10.0, 12.0) == 11.0
    assert math.isnan(au.sweep_level(_t(13), low, 0, 13, 13.6, 14.0))


def test_sweep_flag() -> None:
    assert au.sweep_flag(10.5, 10.2, 10.8)
    assert not au.sweep_flag(10.5, 10.5, 10.8)  # no trade below
    assert not au.sweep_flag(10.5, 10.2, 10.4)  # closed below
    assert not au.sweep_flag(math.nan, 9.0, 12.0)


def test_sweep_level_ignores_data_at_and_after_end() -> None:
    rng = np.random.default_rng(7)
    low = 100 + np.cumsum(rng.normal(0, 1, 200))
    end = 120
    low[80] = float(np.min(low[40:end])) - 5.0  # a certain unswept fractal low inside the window
    full, mid = float(low[80]), float(np.max(low[40:end]))
    a = au.sweep_level(_t(200), low, 40, end, full, mid)
    low2 = low.copy()
    low2[end:] = rng.normal(0, 50, 80)  # anything at or after the touch bar's first hour
    b = au.sweep_level(_t(200), low2, 40, end, full, mid)
    assert math.isfinite(a)
    assert a == b


def test_acceptance_hours() -> None:
    assert au.acceptance_hours(np.array([1.0, 2.0, 3.0, 4.0]), 2.5) == 2
    assert au.acceptance_hours(np.array([3.0, 3.0, 3.0, 2.0]), 2.5) == 3
    assert au.acceptance_hours(np.array([2.5, 2.6, 2.7, 2.8]), 2.5) == 3  # equal is not above
    assert au.acceptance_hours(np.array([3.0, 3.0, 3.0]), 2.5) is None
    assert au.acceptance_hours(np.array([3.0, math.nan, 3.0, 3.0]), 2.5) is None


def test_mirrored_frame_counts_closes_below_mid_for_shorts() -> None:
    closes = np.array([99.0, 98.0, 101.0, 97.0])  # short: real mid 100, three closes below it
    assert au.acceptance_hours(-closes, -100.0) == 3
