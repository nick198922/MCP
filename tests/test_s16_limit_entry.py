"""S16 limit entry on the pullback to the broken edge: analytic expectations and causality."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import s16_limit_entry as le
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import Fees, StopRule

H = le.HOUR_MS
BAR4H = 4 * H
NOFEE = Fees(0.0, 0.0, 0.0, 0.0)
RULE = StopRule("A")
HOLD = 48 * H


def bars(rows: list[tuple[float, float, float, float]], n: int = 48) -> Bars:
    """1h bars from t = 0; ``rows`` first, then flat bars at the last close."""
    rows = list(rows)
    last = rows[-1][3]
    while len(rows) < n:
        rows.append((last, last + 0.5, last - 0.5, last))
    a = np.array(rows, dtype=float)
    t = np.arange(len(rows), dtype=np.int64) * H
    return Bars(t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], H)


FILL_THEN_TARGET = [
    (105, 106, 101, 102),  # h0: no fill (low above 100)
    (102, 103, 99, 101),  # h1: trades through 100, stays above the stop
    (101, 121, 100.5, 120),  # h2: the target 120
]


def test_fill_then_target_is_two_r() -> None:
    r, tk = le.retest_limit(bars(FILL_THEN_TARGET), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert tk == H
    assert r == pytest.approx(2.0)


def test_touch_without_trade_through_does_not_fill() -> None:
    rows = [(105, 106, 100.0, 102)] + [(102, 103, 100.5, 102)] * 15
    r, tk = le.retest_limit(bars(rows), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert (r, tk) == (None, -1)


def test_fill_after_the_window_is_unfilled() -> None:
    rows = [(105, 106, 101, 102)] * 12 + [(102, 103, 95, 101)]  # first trade-through at h12 = 3 bars of 4h
    r, tk = le.retest_limit(bars(rows), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert (r, tk) == (None, -1)
    r1, tk1 = le.retest_limit(bars(rows), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE, bars=4)
    assert tk1 == 12 * H and r1 is not None


def test_target_before_fill_cancels() -> None:
    rows = [(105, 120.5, 104, 110), (110, 111, 95, 99)]
    r, tk = le.retest_limit(bars(rows), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert (r, tk) == (None, -1)


def test_fill_hour_reaching_the_stop_is_a_full_loss() -> None:
    rows = [(105, 106, 89, 104), (104, 125, 103, 124)]
    r, tk = le.retest_limit(bars(rows), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert tk == 0
    assert r == pytest.approx(-1.0)


def test_invalid_geometry_is_unfilled() -> None:
    assert le.retest_limit(bars(FILL_THEN_TARGET), 0, BAR4H, 89.0, 90.0, 120.0, HOLD, RULE, NOFEE) == (None, -1)


def test_limit_result_ignores_bars_after_the_exit() -> None:
    b0 = bars(FILL_THEN_TARGET)
    b1 = bars(FILL_THEN_TARGET + [(120, 300, 1, 2)] * 5)
    assert le.retest_limit(b0, 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE) == le.retest_limit(
        b1, 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE
    )
    # unfilled: bars after the window (here a crash through the level) never change it
    quiet = [(105, 106, 101, 102)] * 12
    a = le.retest_limit(bars(quiet), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE)
    b = le.retest_limit(bars(quiet + [(102, 103, 50, 60)] * 3), 0, BAR4H, 100.0, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert a == b == (None, -1)


def test_delayed_entry_at_the_open() -> None:
    b = bars(FILL_THEN_TARGET)
    r = le.delayed_entry(b, 0, 2, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert r == pytest.approx((120 - 101) / (101 - 90))
    assert le.delayed_entry(b, 0, 0, 90.0, 120.0, HOLD, RULE, NOFEE) == pytest.approx((120 - 105) / (105 - 90))


def test_delayed_entry_cancelled_after_stop_or_target() -> None:
    stop_first = [(105, 106, 89, 95), (95, 96, 94, 95), (95, 96, 94, 95)]
    assert le.delayed_entry(bars(stop_first), 0, 2, 90.0, 120.0, HOLD, RULE, NOFEE) == 0.0
    target_first = [(105, 121, 104, 110), (110, 111, 109, 110), (110, 111, 109, 110)]
    assert le.delayed_entry(bars(target_first), 0, 2, 90.0, 120.0, HOLD, RULE, NOFEE) == 0.0


def test_delayed_entry_missing_bar_is_none() -> None:
    b = bars(FILL_THEN_TARGET)
    keep = np.arange(len(b)) != 2
    holed = Bars(b.t[keep], b.o[keep], b.h[keep], b.low[keep], b.c[keep], H)
    assert le.delayed_entry(holed, 0, 2, 90.0, 120.0, HOLD, RULE, NOFEE) is None


def test_delayed_entry_ignores_bars_after_the_exit() -> None:
    a = le.delayed_entry(bars(FILL_THEN_TARGET), 0, 2, 90.0, 120.0, HOLD, RULE, NOFEE)
    b = le.delayed_entry(bars(FILL_THEN_TARGET + [(120, 300, 1, 2)] * 5), 0, 2, 90.0, 120.0, HOLD, RULE, NOFEE)
    assert a == b


def test_placebo_signal_r() -> None:
    by = [None, None, 1.5, -1.0]
    assert le.placebo_signal_r(by, [0.1, 0.2], [0.0, 0.9], 0.0, [2]) == 0.0
    assert le.placebo_signal_r(by, [0.1, 0.2], [0.0, 0.9], 1.0, [2]) == pytest.approx(1.5)
    assert le.placebo_signal_r(by, [0.1, 0.9], [0.0, 0.0], 0.5, [2]) == pytest.approx(0.75)
    # delay quantiles of the pool [2, 3]: u < 0.5 -> 2, u >= 0.5 -> 3
    assert le.placebo_signal_r(by, [0.1, 0.1], [0.2, 0.7], 1.0, [3, 2]) == pytest.approx(0.25)
    # a draw without a bar is left out
    assert le.placebo_signal_r(by, [0.1, 0.1], [0.0, 0.0], 1.0, [0]) != le.placebo_signal_r(by, [0.1], [0.0], 1.0, [2])
    assert math.isnan(le.placebo_signal_r(by, [0.1], [0.0], 1.0, [0]))


def test_rescored_and_eligibility() -> None:
    assert le.rescored(3, 2.5, 3.2) == 4  # 2 trend points + 2 R:R points
    assert le.rescored(2, 1.2, 2.1) == 3
    assert le.rescored(2, 3.5, 1.6) == 0  # no trend points, both R:R points lost
    assert le.demo_eligible(1.5, 0.10, 2, 1.5, 0.10, 2)
    assert not le.demo_eligible(1.49, 0.05, 3, 1.5, 0.10, 2)
    assert not le.demo_eligible(2.0, 0.11, 3, 1.5, 0.10, 2)
    assert not le.demo_eligible(2.0, 0.05, 1, 1.5, 0.10, 2)


def test_cluster_se_singletons_is_the_classic_se() -> None:
    v = np.array([0.3, -1.0, 2.0, 0.5, -0.2])
    assert le.cluster_se(v, np.arange(v.size)) == pytest.approx(v.std(ddof=1) / math.sqrt(v.size))
    assert math.isnan(le.cluster_se(v, np.zeros(v.size)))


def test_cluster_se_two_clusters() -> None:
    # mean 1; cluster sums of deviations: (0 - 1) + (0 - 1) = -2 and (2 - 1) + (2 - 1) = 2; k = 2
    se = le.cluster_se([0.0, 0.0, 2.0, 2.0], ["a", "a", "b", "b"])
    assert se == pytest.approx(math.sqrt(2.0 * 8.0) / 4.0)


def test_mde80() -> None:
    assert le.mde80(0.1, 0.025) == pytest.approx(0.1 * (1.959964 + 0.841621), rel=1e-6)
    assert le.mde80(0.1, 0.05) == pytest.approx(0.1 * (1.644854 + 0.841621), rel=1e-6)
