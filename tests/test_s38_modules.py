"""S38 modules (item 8 / 7b): analytic expectations and no-lookahead checks."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import s38_modules as m
from tradedesk.analytics.course_s14_ema import Bars, resample, segment_pos
from tradedesk.analytics.stop_management import Fees, risk_unit

H = m.HOUR_MS
T0 = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp() * 1000)  # a Monday 00:00 UTC
F = Fees()


def bars(rows: list[tuple[float, float, float, float]], step: int = H, t0: int = T0) -> Bars:
    return Bars.from_rows([[t0 + i * step, o, h, lo, c] for i, (o, h, lo, c) in enumerate(rows)], step)


def random_1h(n: int, seed: int = 1) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.005, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.005, n))
    return Bars.from_rows([[T0 + i * H, o[i], h[i], lo[i], c[i]] for i in range(n)], H)


def changed_after(b: Bars, t: int, seed: int = 7) -> Bars:
    """Same bars up to (not including) the bar opening at ``t``; later bars replaced by noise."""
    rng = np.random.default_rng(seed)
    k = int(np.searchsorted(b.t, t))
    f = np.ones(len(b))
    f[k:] = rng.uniform(0.5, 1.5, len(b) - k)
    w = np.where(np.arange(len(b)) >= k, 0.01, 0.0)
    return Bars(b.t, b.o * f, b.h * f * (1 + w), b.low * f * (1 - w), b.c * f, b.bar_ms)


# ---------------------------------------------------------------- features


def test_displacement() -> None:
    b = bars([(100, 101, 99, 100), (99, 99.5, 94, 94.5)], 4 * H)
    assert m.displacement(b, 1, level=98.0, atr_prev=2.0) is True  # 3.5 below (>= 1.0), close 0.5 of range 5.5 above low
    assert m.displacement(b, 1, level=95.0, atr_prev=2.0) is False  # only 0.5 below the level (< 1.0)
    c = bars([(100, 101, 99, 100), (99, 99.5, 94, 97)], 4 * H)
    assert m.displacement(c, 1, level=99.0, atr_prev=2.0) is False  # close 3 of 5.5 above the low: not in the lowest 25 %
    assert m.displacement(b, 1, level=98.0, atr_prev=math.nan) is None


def test_new_low() -> None:
    rows = [(100.0, 101.0, 99.0 - 0.01 * i, 100.0) for i in range(10)] + [(99, 99, 97.0, 98)]
    b = bars(rows, 4 * H)
    pos = segment_pos(b.t)
    assert m.new_low(b, pos, 10, n=10) is True
    assert m.new_low(b, pos, 10, n=11) is None  # not enough bars in the segment
    rows[10] = (99, 99, 99.0, 98)
    assert m.new_low(bars(rows, 4 * H), pos, 10, n=10) is False


def test_retest_quality() -> None:
    # breakout bar s = 0, retest j = 1: high 100.4 (<= 100 + 0.5*2), upper wick 100.4-99.2 = 1.2 >= body 0.4, close in the
    # lower third of 100.4..98.6
    b = bars([(101, 101, 97, 97.5), (99.2, 100.4, 98.6, 98.8)], 4 * H)
    q = m.retest_quality(b, 0, 1, level=100.0, atr_j=2.0)
    assert q == {"fast": True, "low_third": True, "wick": True, "shallow": True, "clean": True}
    q2 = m.retest_quality(b, 0, 1, level=99.0, atr_j=1.0)  # high above 99 + 0.5
    assert q2 is not None and not q2["shallow"] and not q2["clean"]
    b3 = bars([(101, 101, 97, 97.5), (97.5, 98, 97, 97.6), (97.6, 98, 97, 97.7), (99.2, 100.4, 98.6, 98.8)], 4 * H)
    q3 = m.retest_quality(b3, 0, 3, level=100.0, atr_j=2.0)
    assert q3 is not None and not q3["fast"] and not q3["clean"]
    assert m.retest_quality(b, 0, 1, level=100.0, atr_j=math.nan) is None


def test_pool_levels_and_take() -> None:
    # 40 days of flat 1h bars at 100 with a one-hour dip to 95 on day 35 (the previous day of t_in = day 36 00:00)
    n = 36 * 24
    rows = [(100.0, 100.5, 99.5, 100.0)] * n
    rows[35 * 24 - 10] = (100.0, 100.5, 95.0, 100.0)
    h1 = bars(rows)
    t_in = T0 + n * H
    b4 = resample(h1, 4)
    lv = m.pool_levels(h1, m.fractal_lows(b4), t_in, low_days=30)
    assert 95.0 in lv and 99.5 in lv  # day low (95), week low / 30-day low (95), month low absent (no full month)
    assert m.pool_on_path(lv, entry=97.0, target=90.0) == 95.0
    assert math.isnan(m.pool_on_path(lv, entry=94.0, target=90.0))
    assert m.take_price(95.0, 2.0, 97.0) == pytest.approx(95.2)
    assert math.isnan(m.take_price(95.0, 2.0, 95.1))


def test_measured_move() -> None:
    assert m.measured_move(100.0, 104.0, target=97.0) == 96.0  # 4 below the low is beyond the 2R target
    assert m.measured_move(100.0, 101.0, target=97.0) == 97.0  # a short measured move keeps the 2R target


def test_trail_levels() -> None:
    b = bars([(1, 2, 0.5, 1), (1, 2, 0.8, 1), (1, 2, 0.6, 1)], 4 * H)
    t, lv = m.trail_levels(b)
    assert list(lv) == [0.5, 0.6]
    assert list(t) == [T0 + 2 * 4 * H, T0 + 3 * 4 * H]


# ---------------------------------------------------------------- trade pieces (long space)


def test_fixed_half_and_mm_runner() -> None:
    # entry 100, stop 90: 1R = 110, 2R = 120; MM target 130
    p = bars([(100, 111, 99.5, 110.5), (110.5, 112, 100.0, 101), (101, 131, 100.5, 130)])
    half = m.fixed_half(p, T0, 100, 90, 110, 10 * H, F)
    ru = risk_unit(100, 90, F)
    assert half is not None and half.exit_kind == "target"
    assert half.r_net == pytest.approx((10 - F.maker * 100 - F.maker * 110) / ru)
    run = m.mm_runner(p, T0, 100, 90, 130, 10 * H, F)
    assert run is not None and run.exit_kind == "be"  # 1R touched in bar 0, break-even from bar 1, bar 1 dips to 100
    assert m.halves(half, run) == pytest.approx(0.5 * half.r_net + 0.5 * run.r_net)
    assert math.isnan(m.halves(half, None))


def test_trail_runner_tightens_after_1r() -> None:
    # 1h path; 4h trail levels: 95 known at bar 0's open (ignored before 1R), 112 known from bar 2
    p = bars([(100, 111, 99.5, 110.5), (110.5, 118, 109, 117), (117, 119, 113, 114), (114, 115, 111, 112)])
    trail_t = np.array([T0, T0 + 2 * H], dtype=np.int64)
    trail_lv = np.array([95.0, 112.0])
    out = m.trail_runner(p, T0, 100, 90, 10 * H, trail_t, trail_lv, F)
    ru = risk_unit(100, 90, F)
    assert out is not None and out.exit_kind == "trail" and out.exit == 112.0
    assert out.r_net == pytest.approx((12 - F.maker * 100 - F.taker_exit * 112) / ru)
    # without a 1R high the trail never applies and the base stop decides
    q = bars([(100, 105, 99.5, 104), (104, 105, 89, 89.5)])
    out2 = m.trail_runner(q, T0, 100, 90, 10 * H, trail_t, np.array([95.0, 104.0]), F)
    assert out2 is not None and out2.exit_kind == "stop" and out2.exit == 90


def test_trail_runner_time_exit() -> None:
    p = bars([(100, 111, 99.5, 110.5), (110.5, 112, 105, 111), (111, 113, 106, 112)])
    out = m.trail_runner(p, T0, 100, 90, 3 * H, np.zeros(0, dtype=np.int64), np.zeros(0), F)
    assert out is not None and out.exit_kind == "time" and out.exit == 112


# ---------------------------------------------------------------- no lookahead


def test_features_do_not_change_with_later_bars() -> None:
    h1 = random_1h(24 * 120)
    b4 = resample(h1, 4)
    pos = segment_pos(b4.t)
    s, j = 400, 402
    t_in = int(b4.t[j]) + b4.bar_ms
    h1x = changed_after(h1, t_in)
    b4x = resample(h1x, 4)
    assert np.array_equal(b4.c[: j + 1], b4x.c[: j + 1]) and not np.array_equal(b4.c[j + 1 :], b4x.c[j + 1 :])
    lvl = float(np.min(b4.low[s - 30 : s]))
    for b in (b4, b4x):
        assert m.displacement(b, s, lvl, 1.0) == m.displacement(b4, s, lvl, 1.0)
        assert m.new_low(b, pos, s) == m.new_low(b4, pos, s)
        assert m.retest_quality(b, s, j, lvl, 1.0) == m.retest_quality(b4, s, j, lvl, 1.0)
    assert m.pool_levels(h1x, m.fractal_lows(b4x), t_in) == m.pool_levels(h1, m.fractal_lows(b4), t_in)
    t1, l1 = m.trail_levels(b4)
    t2, l2 = m.trail_levels(b4x)
    known = t1 <= t_in
    assert np.array_equal(l1[known], l2[: known.sum()])


# ---------------------------------------------------------------- placebo


def test_draw_coins_deterministic_distinct() -> None:
    pool = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    a = m.draw_coins("X|4h|short|1", pool, 3)
    assert a == m.draw_coins("X|4h|short|1", list(reversed(pool)), 3)
    assert len(set(a)) == 3 and set(a) <= set(pool)
    assert m.draw_coins("k", ["AAA"], 3) == ["AAA"]


def test_placebo_levels() -> None:
    e, st, tg = m.placebo_levels(50.0, 0.04)
    assert (e, st, tg) == pytest.approx((50.0, 52.0, 46.0))
