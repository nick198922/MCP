"""analytics.short_setups: detectors A (breakout + retest) and B (failed S3 sweep) on hand-built bars."""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.short_setups import breakout_retest, failed_sweep, placebo_levels, regime_at, regimes_at

H4 = 4 * 3_600_000


def bars(rows: list[tuple[float, float, float, float]]) -> Bars:
    return Bars.from_rows([[k * H4, o, h, lo, c] for k, (o, h, lo, c) in enumerate(rows)], H4)


def flat(n: int = 40) -> list[tuple[float, float, float, float]]:
    """A quiet range: high 102, low 99, closes alternating 100 / 101 (range middle 100.5)."""
    return [(100.0, 102.0, 99.0, 101.0 if k % 2 else 100.0) for k in range(n)]


def run_a(rows: list[tuple[float, float, float, float]], **kw: float) -> tuple[list, np.ndarray]:
    b = bars(rows)
    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    return breakout_retest(b, pos, a, **kw), a


def run_b(rows: list[tuple[float, float, float, float]]) -> tuple[list, np.ndarray]:
    b = bars(rows)
    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    return failed_sweep(b, pos, a), a


def test_a_breakout_and_retest_gives_a_long_at_the_retest_close() -> None:
    rows = flat() + [(101.0, 104.0, 100.5, 103.5), (103.5, 104.0, 102.3, 103.0)]
    sigs, a = run_a(rows)
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.kind, s.start, s.i, s.level) == ("A", 40, 41, 102.0)
    assert s.entry == 103.0
    assert s.stop == pytest.approx(102.3 - 0.5 * a[41])
    assert s.target == pytest.approx(103.0 + 2 * (103.0 - s.stop))
    assert math.isnan(s.alt_target)  # no bars before the range window


def test_a_retest_closing_back_inside_is_a_failed_breakout() -> None:
    rows = flat() + [(101.0, 104.0, 100.5, 103.5), (103.5, 104.0, 101.0, 101.8)]
    assert run_a(rows)[0] == []


def test_a_no_touch_within_the_wait_gives_nothing_and_a_late_touch_is_ignored() -> None:
    away = [(105.0, 106.0, 104.5, 105.5)] * 6
    late = [(105.0, 105.0, 102.1, 103.0)]  # touches at bar s + 7
    rows = flat() + [(101.0, 104.0, 100.5, 103.5)] + away + late
    assert run_a(rows)[0] == []
    rows_in = flat() + [(101.0, 104.0, 100.5, 103.5)] + away[:5] + late  # touch at s + 6: allowed
    sigs, _ = run_a(rows_in)
    assert [x.i for x in sigs] == [46]


def test_a_touch_tolerance_in_atr() -> None:
    # low 102.5 is 0.5 above the edge: a touch only with a tolerance >= 0.5 / ATR
    rows = flat() + [(101.0, 104.0, 100.5, 103.5), (103.5, 104.0, 102.5, 103.2)]
    sigs, a = run_a(rows)
    tol = 0.5 / a[41]
    assert (len(sigs) == 1) == (0.25 >= tol)
    assert len(run_a(rows, touch_atr=tol + 0.01)[0]) == 1
    assert run_a(rows, touch_atr=tol - 0.01)[0] == []


def test_a_short_is_the_mirror() -> None:
    # breakdown below 99, retest from below (high 98.6 >= 99 - 0.25 ATR), close 98.0 below the edge
    rows = flat() + [(100.0, 100.5, 97.0, 97.5), (97.5, 98.6, 97.2, 98.0)]
    b = bars(rows)
    m = b.mirrored()
    pos = segment_pos(m.t)
    a = atr(m, 14, pos)
    sigs = breakout_retest(m, pos, a)
    assert len(sigs) == 1
    s = sigs[0]
    assert -s.entry == 98.0 and -s.level == 99.0
    assert -s.stop == pytest.approx(98.6 + 0.5 * a[41])  # above the retest high
    assert -s.target == pytest.approx(98.0 - 2 * (-s.stop - 98.0))


def test_a_liquidity_target() -> None:
    early = [(110.0, 112.0, 109.0, 110.0)] + [(100.0, 104.0, 99.0, 100.5)] * 29  # bars 0..29: high 112
    rows = early + flat(30) + [(101.0, 104.0, 100.5, 103.5), (103.5, 104.0, 102.3, 103.0)]
    b = bars(rows)
    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    sigs = breakout_retest(b, pos, a)
    assert len(sigs) == 1 and sigs[0].i == 61
    assert sigs[0].alt_target == 112.0
    sigs_far = breakout_retest(b, pos, a, liq_min_r=100.0)
    assert math.isnan(sigs_far[0].alt_target)


def test_b_failed_s3_short_gives_a_long() -> None:
    # bar 40: sweep of the high (103 > 102), close back inside above the middle -> S3 short, stop 103 + 0.5 ATR
    rows = flat() + [(101.0, 103.0, 101.0, 101.5), (101.5, 106.0, 101.2, 105.8)]
    sigs, a = run_b(rows)
    assert 105.8 > 103.0 + 0.5 * a[40]
    assert len(sigs) == 1
    s = sigs[0]
    assert (s.kind, s.start, s.i) == ("B", 40, 41)
    assert s.level == pytest.approx(103.0 + 0.5 * a[40])
    assert s.entry == 105.8
    assert s.stop == pytest.approx(101.0 - 0.5 * a[41])
    assert s.target == pytest.approx(105.8 + 2 * (105.8 - s.stop))


def test_b_needs_a_close_beyond_the_stop_and_an_alive_s3_trade() -> None:
    wick = flat() + [(101.0, 103.0, 101.0, 101.5), (101.5, 106.0, 101.2, 102.0)]  # wick through the stop, no close
    assert run_b(wick)[0] == []
    hit = flat() + [(101.0, 103.0, 101.0, 101.5), (101.5, 101.8, 100.3, 101.0), (101.0, 106.0, 100.8, 105.8)]
    assert run_b(hit)[0] == []  # the S3 short reached the middle 100.5 first
    late = flat() + [(101.0, 103.0, 101.0, 101.5)] + [(101.5, 102.0, 101.0, 101.6)] * 6 + [(101.6, 106.0, 101.4, 105.8)]
    assert run_b(late)[0] == []  # beyond r + 6


def _walk(n: int, seed: int) -> list[tuple[float, float, float, float]]:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
    return list(zip(o.tolist(), h.tolist(), lo.tolist(), c.tolist(), strict=True))


@pytest.mark.parametrize("mirror", [False, True])
def test_signals_up_to_t_do_not_depend_on_later_bars(mirror: bool) -> None:
    base = _walk(1500, 1)
    other = base[:900] + _walk(600, 2)
    t = 899
    found = 0
    for fn in (breakout_retest, failed_sweep):
        res = []
        for rows in (base, other):
            b = bars(rows)
            b = b.mirrored() if mirror else b
            pos = segment_pos(b.t)
            # astuple + repr: alt_target is NaN for A, and NaN != NaN in dataclass __eq__ on Python 3.13
            res.append([repr(dataclasses.astuple(x)) for x in fn(b, pos, atr(b, 14, pos)) if x.i <= t])
        assert res[0] == res[1]
        found += len(res[0])
    assert found > 0


def test_regime_at_uses_the_last_closed_bar() -> None:
    close = np.array([10, 20, 30])
    trend = np.array([1, -1, 0])
    assert regime_at(close, trend, 5) == 0
    assert regime_at(close, trend, 20) == -1
    assert regime_at(close, trend, 29) == -1
    assert regime_at(close, trend, 99) == 0


def test_regimes_at_vectorised_is_causal() -> None:
    # regimes_at is what scripts/verify_short_setups.py uses (imported as ``regimes``)
    close = np.array([10, 20, 30, 40])
    trend = np.array([1, -1, 0, 1])
    t = np.array([5, 10, 19, 20, 29, 35])
    out = regimes_at(close, trend, t)
    assert out.dtype == np.int64
    assert out.tolist() == [0, 1, 1, -1, -1, 0]
    # values of bars closed after the last query time do not change the result
    changed = trend.copy()
    changed[3] = -1
    assert regimes_at(close, changed, t).tolist() == out.tolist()
    # scalar form agrees
    assert [regime_at(close, trend, int(x)) for x in t] == out.tolist()


def test_placebo_levels() -> None:
    assert placebo_levels(100.0, 2.0, 1.5) == (97.0, 106.0)
    assert placebo_levels(100.0, 2.0, 1.5, target_r=3.0) == (97.0, 109.0)
