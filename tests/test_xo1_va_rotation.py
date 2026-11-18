"""XO-1 value-area edge rotation: profiles, levels, signals, vectorised outcomes, placebo starts, causality."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import xo1_va_rotation as xo
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import Fees, StopRule, simulate_stop_rule

H = xo.HOUR_MS
D = xo.DAY_MS
FEES = Fees(maker=0.00075, taker_exit=0.00075)  # all sides taker 0.055 % + slippage 0.02 %


def test_tpo_profile_counts_touches() -> None:
    low = np.array([0.0, 2.0, 1.0])
    high = np.array([4.0, 4.0, 1.5])
    edges, cnt = xo.tpo_profile(low, high, n_rows=4)
    assert edges.tolist() == [0.0, 1.0, 2.0, 3.0, 4.0]
    # rows [0,1] [1,2] [2,3] [3,4]: bar 0 all four; bar 1 rows 1..3 (touches edge 2 of row 1); bar 2 rows 0..1
    assert cnt.tolist() == [2.0, 3.0, 2.0, 2.0]


def test_profile_levels_tpo_value_area() -> None:
    # 10 bars: 6 in [4, 5], 2 in [3, 4], 2 in [5, 6] -> 5 rows of width 1 over [1, 6]: one bar spans [1, 6]
    low = np.array([4.0] * 6 + [3.0] * 2 + [5.0] * 2 + [1.0])
    high = np.array([5.0] * 6 + [4.0] * 2 + [6.0] * 2 + [6.0])
    val, poc, vah = xo.profile_levels(low, high, n_rows=5)
    # counts: row [1,2] 1, [2,3] 1, [3,4] 1+2+6=9, [4,5] 6+2+2+1=11, [5,6] 2+6+1=9 -> POC row [4,5]; 70 % of 31 = 21.7:
    # 11 + 9 (up, tie -> above) = 20, + 9 = 29 -> VA [3, 6]
    assert poc == pytest.approx(4.5)
    assert (val, vah) == (pytest.approx(3.0), pytest.approx(6.0))


def test_value_area_sym_tie_rules() -> None:
    edges = np.arange(6, dtype=float)
    # POC: the largest rows are 0 and 3; row 3 (mid 3.5) is closer to the centre 2.5 than row 0 (mid 0.5)
    assert xo.value_area_sym(edges, np.array([5.0, 1.0, 2.0, 5.0, 2.0]), 0.5) == (2.0, 3.5, 5.0)
    # equal neighbours are added together: 4 + (3 + 3) = 10 >= 0.7 * 12
    assert xo.value_area_sym(edges, np.array([1.0, 3.0, 4.0, 3.0, 1.0]), 0.7) == (1.0, 2.5, 4.0)


def _flat_day(day: int, lo: float, hi: float, n: int = 24) -> list[list[float]]:
    return [[day + k * H, (lo + hi) / 2, hi, lo, (lo + hi) / 2] for k in range(n)]


def test_day_levels_uses_only_previous_days_and_coverage() -> None:
    d0 = 1_700_006_400_000 // D * D
    rows = _flat_day(d0, 99.0, 101.0) + _flat_day(d0 + D, 199.0, 201.0)
    a = np.array(rows)
    lv = xo.day_levels(a[:, 0], a[:, 3], a[:, 2], 1)
    assert int(lv.day[0]) == d0 + D
    # 48 equal rows of 1/24 over [99, 101]: POC row 23 (lower of the two middle rows), both sides grow together until
    # 35 rows >= 70 % of 48 -> rows 6 .. 40
    assert lv.poc[0] == pytest.approx(99 + 23.5 / 24)
    assert lv.val[0] == pytest.approx(99.25) and lv.vah[0] == pytest.approx(99 + 41 / 24)
    assert lv.val[1] == pytest.approx(199.25)  # day d0 + 2 from day d0 + 1
    short = xo.day_levels(a[:-5, 0], a[:-5, 3], a[:-5, 2], 1)  # 19 of 24 hours < 90 %
    assert np.isnan(short.val[1])
    comp = xo.day_levels(a[:, 0], a[:, 3], a[:, 2], 2)
    assert int(comp.day[0]) == d0 + 2 * D and comp.val[0] < 100 and comp.vah[0] > 200


def _rotation_series() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Day 0: balanced 90..110 (VA ~ inside); day 1: a dip under VAL at hour 3, a close back inside at hour 4."""
    d0 = 1_700_006_400_000 // D * D
    rows = []
    for k in range(24):
        lo, hi = (95.0, 105.0) if k % 4 else (90.0, 110.0)
        rows.append([d0 + k * H, 100.0, hi, lo, 100.0])
    path = [100.0, 99.0, 97.0, 95.0, 96.0, 97.0, 98.0, 99.0, 100.0, 101.0]
    for k, p in enumerate(path):
        lo = p - 0.5 if k != 3 else 93.0
        rows.append([d0 + D + k * H, p, p + 0.5, lo, p if k != 3 else 94.0])
    a = np.array(rows)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]


def test_find_signals_long_rotation_from_val() -> None:
    t, o, h, low, c = _rotation_series()
    lv = xo.day_levels(t, low, h, 1)
    val, poc = float(lv.val[0]), float(lv.poc[0])
    assert 93.0 < val < 96.0 < poc  # the dip pierces VAL and hour 4 closes back inside below the POC
    atr = np.full(t.size, 1.0)
    sig = xo.find_signals(t, h, low, c, atr, lv, k_probe=3, buf_atr=0.5, min_stop=0.0, max_stop=1.0)
    longs = [s for s in sig if s.side == 1]
    assert len(longs) == 1
    s = longs[0]
    assert s.i == 24 + 4 and s.t_in == int(t[s.i]) + H
    assert s.entry == 96.0 and s.stop == pytest.approx(93.0 - 0.5) and s.target == pytest.approx(poc)
    # stop-distance filter skips the side for the day instead of searching a later bar
    assert not [s for s in xo.find_signals(t, h, low, c, atr, lv, min_stop=0.2) if s.side == 1]


def test_signals_and_levels_do_not_look_ahead() -> None:
    t, o, h, low, c = _rotation_series()
    atr = xo.atr_sma(h, low, c, 14)
    lv = xo.day_levels(t, low, h, 1)
    base = xo.find_signals(t, h, low, c, atr, lv, min_stop=0.0, max_stop=1.0)
    cut = 24 + 5  # change every bar after the signal bar's close
    h2, l2, c2 = h.copy(), low.copy(), c.copy()
    h2[cut:] *= 1.3
    l2[cut:] *= 0.7
    c2[cut:] *= 1.1
    atr2 = xo.atr_sma(h2, l2, c2, 14)
    lv2 = xo.day_levels(t, l2, h2, 1)
    later = xo.find_signals(t, h2, l2, c2, atr2, lv2, min_stop=0.0, max_stop=1.0)
    early = [s for s in base if s.t_in <= int(t[cut])]
    assert early and early == [s for s in later if s.t_in <= int(t[cut])]
    assert np.array_equal(lv.val[:1], lv2.val[:1]) and np.allclose(atr[:cut], atr2[:cut], equal_nan=True)


def test_atr_sma_analytic() -> None:
    h = np.array([2.0, 3.0, 4.0, 5.0])
    low = np.array([1.0, 2.0, 3.0, 4.0])
    c = np.array([1.5, 2.5, 3.5, 4.5])
    a = xo.atr_sma(h, low, c, 2)
    # TR: bar1 max(1, 1.5, 0.5) = 1.5, bar2 1.5, bar3 1.5
    assert np.isnan(a[:2]).all() and a[2] == pytest.approx(1.5) and a[3] == pytest.approx(1.5)


@pytest.mark.parametrize("side", [1, -1])
def test_barrier_outcomes_match_simulate_stop_rule(side: int) -> None:
    rng = np.random.default_rng(7)
    n = 400
    t = np.arange(n, dtype=np.int64) * H + 1_700_000_000_000 // H * H
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.concatenate([[100.0], c[:-1]]) * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.004, n)))
    bars = Bars(t, o, h, low, c, H)
    path = bars if side == 1 else bars.mirrored()
    n_hold = 24
    i0 = np.arange(20, 360, 7)
    e = c[i0 - 1]
    st = e * (1 - side * rng.uniform(0.005, 0.03, i0.size))
    tg = e * (1 + side * rng.uniform(0.005, 0.04, i0.size))
    r, ex, kind = xo.barrier_outcomes(o, h, low, c, i0, np.full(i0.size, side), e, st, tg, n_hold, FEES)
    for k, i in enumerate(i0):
        ref = simulate_stop_rule(path, int(t[i]), side * e[k], side * st[k], side * tg[k], n_hold * H, StopRule("A"), fees=FEES)
        assert ref is not None
        assert r[k] == pytest.approx(ref.r_net, abs=1e-9)
        assert int(t[ex[k]]) + H == ref.exit_ms
        assert {0: "time", 1: "stop", 2: "target"}[int(kind[k])] == ref.exit_kind.replace("_gap", "")


def test_valid_starts_and_placebo_window() -> None:
    t = np.array([0, 1, 2, 3, 5, 6, 7, 8, 9], dtype=np.int64) * H
    v = xo.valid_starts(t, 3)
    assert v.tolist() == [0, 1, 4, 5, 6]  # windows over the hole 3 -> 5 are out
    rng = np.random.default_rng(1)
    p = xo.placebo_starts(v, np.array([1, 6]), 1, 500, rng)
    assert set(p[0].tolist()) == {0, 1} and set(p[1].tolist()) == {5, 6}
    none = xo.placebo_starts(np.array([0], dtype=np.int64), np.array([50]), 2, 3, rng)
    assert none.tolist() == [[-1, -1, -1]]


def test_non_overlapping_and_balance() -> None:
    keep = xo.non_overlapping(np.array([0, 5, 12, 11]), np.array([10, 15, 20, 30]))
    assert keep.tolist() == [True, False, False, True]  # entry order 0, 5, 11, 12: the trade at 11 blocks 12
    lv = xo.Levels(np.array([0, D, 2 * D, 4 * D]), np.array([0.0, 1.0, 5.0, 5.0]), np.full(4, 2.0), np.array([4.0, 3.0, 6.0, 6.0]))
    assert xo.balance_flags(lv) == {D: True, 2 * D: False}  # day 4D follows a gap: no flag
