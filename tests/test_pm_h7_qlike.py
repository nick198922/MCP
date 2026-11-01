"""Item 8l (H7''): QLIKE of the flat vs profile variance forecasts on the paper's IV model - analytic references."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics import pm_h7_qlike as q
from tradedesk.analytics.pm_threshold import HOUR_S, YEAR_S

MON = int(datetime(2026, 10, 26, tzinfo=UTC).timestamp())  # Monday 00:00 UTC
FLAT = {(h, w): 1.0 for h in range(24) for w in (False, True)}


def test_whole_hours_drops_partial_hours() -> None:
    ts = MON + 9 * HOUR_S + 20 * 60
    assert q.whole_hours(ts, MON + 16 * HOUR_S) == (MON + 10 * HOUR_S, MON + 16 * HOUR_S)
    assert q.whole_hours(ts, MON + 16 * HOUR_S + 59 * 60) == (MON + 10 * HOUR_S, MON + 16 * HOUR_S)
    assert q.whole_hours(ts, MON + 10 * HOUR_S + 30 * 60) is None  # less than one whole hour
    assert q.whole_hours(MON + 10 * HOUR_S, MON + 11 * HOUR_S) == (MON + 10 * HOUR_S, MON + 11 * HOUR_S)


def test_window_var_scale_flat_profile_is_one() -> None:
    ts = MON + 9 * HOUR_S + 20 * 60
    s = q.window_var_scale(FLAT, ts, MON + 10 * HOUR_S, MON + 16 * HOUR_S, MON + 32 * HOUR_S)
    assert s == pytest.approx(1.0)


def test_window_var_scale_hand_example() -> None:
    # hour 10 on a weekday weighs 3, all else 1; ts 09:20, H = [10; 12), E = 14:00
    # W(H)/|H| = 4/2 = 2; W(ts, E) = 2/3 + 3 + 3 = 20/3 over 14/3 h = 10/7; s = 2 / (10/7) = 1.4
    prof = dict(FLAT)
    prof[(10, False)] = 3.0
    ts = MON + 9 * HOUR_S + 20 * 60
    s = q.window_var_scale(prof, ts, MON + 10 * HOUR_S, MON + 12 * HOUR_S, MON + 14 * HOUR_S)
    assert s == pytest.approx(1.4)


def test_window_var_scale_invalid() -> None:
    ts = MON + 9 * HOUR_S
    assert q.window_var_scale(FLAT, ts, MON + 10 * HOUR_S, MON + 12 * HOUR_S, MON + 11 * HOUR_S) is None  # E < h1
    assert q.window_var_scale(FLAT, ts, MON + 8 * HOUR_S, MON + 12 * HOUR_S, MON + 14 * HOUR_S) is None  # h0 < ts
    assert q.window_var_scale(FLAT, ts, MON + 12 * HOUR_S, MON + 12 * HOUR_S, MON + 14 * HOUR_S) is None


def test_flat_var_units() -> None:
    assert q.flat_var(0.5, 0, YEAR_S) == pytest.approx(0.25)
    assert q.flat_var(0.5, 0, 24 * HOUR_S) == pytest.approx(0.25 / 365)


def test_realized_var_and_missing_bar() -> None:
    rets = {MON: 0.01, MON + HOUR_S: -0.02}
    assert q.realized_var(rets, MON, MON + 2 * HOUR_S) == pytest.approx(0.0005)
    assert q.realized_var(rets, MON, MON + 3 * HOUR_S) is None


def test_realized_var_is_causal_in_its_window() -> None:
    rets = {MON + i * HOUR_S: 0.001 * (i + 1) for i in range(48)}
    before = q.realized_var(rets, MON, MON + 10 * HOUR_S)
    changed = dict(rets)
    for i in range(10, 48):
        changed[MON + i * HOUR_S] = 0.5  # data at or after the window end change
    changed.pop(MON + 40 * HOUR_S)
    assert q.realized_var(changed, MON, MON + 10 * HOUR_S) == before


@pytest.mark.parametrize(("y", "h", "s", "c"), [(1e-4, 2e-4, 1.3, 1.0), (3e-4, 1e-4, 0.8, 1.0), (2e-4, 2e-4, 1.1, 0.8)])
def test_qlike_diff_matches_losses(y: float, h: float, s: float, c: float) -> None:
    assert q.qlike_diff(y, h, s, c) == pytest.approx(q.qlike(c * h, y) - q.qlike(c * s * h, y))


def test_qlike_properties() -> None:
    assert q.qlike(2e-4, 2e-4) == pytest.approx(0.0)
    assert q.qlike(1e-4, 2e-4) > 0 and q.qlike(3e-4, 2e-4) > 0
    assert q.qlike_diff(5e-4, 2e-4, 1.0) == pytest.approx(0.0)
    assert q.qlike_diff(0.0, 2e-4, 1.25) == pytest.approx(-math.log(1.25))  # y = 0 is allowed


def test_expected_gain_by_linearity() -> None:
    # dL is linear in y: the mean over y values with mean s * h_f equals s - 1 - ln s
    h, s = 1e-4, 1.2
    ys = [0.3 * s * h, 1.7 * s * h]
    mean = sum(q.qlike_diff(y, h, s) for y in ys) / 2
    assert mean == pytest.approx(q.expected_gain(s))
    assert q.expected_gain(s) == pytest.approx(s - 1 - math.log(s))
    assert q.expected_diff_level(s, 1.0) == pytest.approx(q.expected_gain(s))
    assert q.expected_diff_level(s, 1.0, profile_true=False) < 0
    # a premium (q < 1) flips the sign for s > 1 even when the profile is right
    assert q.expected_diff_level(1.09, 0.8) < 0


def test_market_weights() -> None:
    w = q.market_weights(["a", "a", "b", "c", "c", "c"])
    assert sum(w) == pytest.approx(1.0)
    assert w[0] + w[1] == pytest.approx(1 / 3)
    assert sum(w[3:]) == pytest.approx(1 / 3)


def test_renormalize_cancels_first_order_level_term() -> None:
    scales = [1.2, 1.05, 0.9, 1.3]
    w = q.market_weights(["a", "a", "b", "c"])
    s2, m = q.renormalize(scales, w)
    assert q.weighted_mean(s2, w) == pytest.approx(1.0)
    assert m == pytest.approx(q.weighted_mean(scales, w))
    # with sum w (s' - 1) = 0 the weighted expectation does not depend on the level ratio
    e = [q.weighted_mean([q.expected_diff_level(s, qq) for s in s2], w) for qq in (0.7, 1.0, 1.4)]
    assert e[0] == pytest.approx(e[1]) and e[1] == pytest.approx(e[2])
    assert e[1] == pytest.approx(q.weighted_mean([-math.log(s) for s in s2], w))


def test_break_even_level() -> None:
    scales = [1.2, 1.05, 1.1]
    w = [0.5, 0.25, 0.25]
    qs = q.break_even_level(scales, w)
    assert qs is not None and 0 < qs < 1
    assert q.weighted_mean([q.expected_diff_level(s, qs) for s in scales], w) == pytest.approx(0.0, abs=1e-12)
    assert q.break_even_level([1.0, 1.0], [0.5, 0.5]) is None


def test_weeks_needed() -> None:
    assert q.weeks_needed(1.0, 0.28) == 100
    assert q.weeks_needed(0.0, 0.1) is None
    assert q.weeks_needed(1.0, 0.0) is None
