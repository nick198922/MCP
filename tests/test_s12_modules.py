"""S12 v4 modules (analytics/s12_modules.py): analytic expectations and causality."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np

from tradedesk.analytics import s12_modules as m
from tradedesk.analytics.course_s14_ema import Bars

H = 3_600_000
T0 = 1_700_000_000_000 // (4 * H) * (4 * H)


def hours(rows: list[tuple[float, float, float, float]], t0: int = T0) -> Bars:
    a = np.array(rows, dtype=float)
    t = (t0 + H * np.arange(len(rows))).astype(np.int64)
    return Bars(t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], H)


def test_clv_and_gap_atr() -> None:
    assert m.clv(110.0, 100.0, 107.5) == 0.75
    assert math.isnan(m.clv(100.0, 100.0, 100.0))
    assert m.gap_atr(104.0, 101.0, 2.0) == 1.5
    assert math.isnan(m.gap_atr(104.0, 101.0, 0.0))


def test_edge_touch_counts_entries_into_the_gap_before_the_touch() -> None:
    low = np.array([90, 95, 100, 106, 104.5, 107, 103.9, 102.0])  # gap third candle i=2, near 105, touch j=7
    assert m.edge_touch(low, 2, 7, 105.0) == 2  # bars 4 (104.5) and 6 (103.9)
    assert m.edge_touch(low, 2, 3, 105.0) == 0
    low2 = low.copy()
    low2[7] = 50.0  # the touch bar itself is not counted
    assert m.edge_touch(low2, 2, 7, 105.0) == 2


def test_dry_ratio() -> None:
    qv = np.array([0, 10, 20, 30, 5, 5, 8.0])
    assert m.dry(qv, 3, 6) == (6.0 / 20.0)  # pullback bars 4..6 mean 6, gap bars 1..3 mean 20
    assert math.isnan(m.dry(qv, 1, 4))
    assert math.isnan(m.dry(np.zeros(7), 3, 6))


def test_bar_volume_sums_hours() -> None:
    h_t = (T0 + H * np.arange(9)).astype(np.int64)
    qv = np.arange(1.0, 10.0)
    bt = np.array([T0, T0 + 4 * H], dtype=np.int64)
    np.testing.assert_allclose(m.bar_volume(h_t, qv, bt, 4 * H), [1 + 2 + 3 + 4, 5 + 6 + 7 + 8])


# mid 100, stop 95, target 110
BASE = [(101.0, 102.0, 100.5, 101.5), (101.5, 101.8, 99.0, 99.5), (99.5, 100.8, 99.2, 100.6), (100.6, 104.0, 100.0, 103.5)]


def test_early_entry_first_close_above_mid_after_touch() -> None:
    p = hours(BASE)
    assert m.early_entry(p, T0, 4 * H, 100.0, 95.0, 110.0) == (2, 100.6)


def test_early_entry_none_when_only_last_hour_confirms_or_stop_touched() -> None:
    rows = [r for r in BASE]
    rows[2] = (99.5, 99.9, 99.2, 99.4)  # no close above the middle before the last hour: v4's close stays
    assert m.early_entry(hours(rows), T0, 4 * H, 100.0, 95.0, 110.0) is None
    rows = [r for r in BASE]
    rows[1] = (101.5, 101.8, 94.0, 99.5)  # the stop was touched before the confirmation
    assert m.early_entry(hours(rows), T0, 4 * H, 100.0, 95.0, 110.0) is None
    assert m.early_entry(hours(BASE[:3]), T0, 4 * H, 100.0, 95.0, 110.0) is None  # incomplete bar


def test_early_entry_is_causal() -> None:
    """The entry at hour m does not change when hours after m change."""
    rows = [r for r in BASE] + [(103.5, 105.0, 103.0, 104.0)] * 4
    got = m.early_entry(hours(rows), T0, 4 * H, 100.0, 95.0, 110.0)
    rows[3] = (100.6, 100.7, 80.0, 81.0)  # the rest of the touch bar crashes through the stop
    rows[4:] = [(81.0, 82.0, 70.0, 71.0)] * 4
    assert m.early_entry(hours(rows), T0, 4 * H, 100.0, 95.0, 110.0) == got == (2, 100.6)


MID, STOP, TGT, MIN_RR = 100.0, 95.0, 110.0, 1.5


def rr(e: float) -> float:
    return (TGT - e) / (e - STOP)


def h2_set_variants() -> list[tuple[Bars, np.ndarray, bool]]:
    """One touch bar (hours 0..3, early entry at hour 2, close 100.6, R:R 1.68) and hours after it, changed after m=2:
    the 4h close (hour 3) makes v4 pass the guard (R:R 1.73), fail it (R:R 0.76) or breach the stop (no v4 trade);
    later hours and their volumes change too. Returns (hours, hourly quote volume, v4 in V)."""
    out = []
    for last, later, vol_after in (
        ((100.6, 101.0, 100.2, 100.5), (100.5, 101.0, 100.0, 100.8), 1.0),  # v4 R:R 9.5/5.5 = 1.73: in V
        ((100.6, 104.0, 100.0, 103.5), (103.5, 109.0, 103.0, 108.0), 1e9),  # v4 R:R 6.5/8.5 = 0.76: not in V
        ((100.6, 100.7, 80.0, 81.0), (81.0, 82.0, 70.0, 71.0), 0.0),  # stop breached in the touch bar: no v4 trade
    ):
        rows = BASE[:3] + [last] + [later] * 4
        c4, low4 = last[3], min(r[2] for r in rows[:4])
        v4_in_v = low4 > STOP and c4 < TGT and rr(c4) >= MIN_RR
        qv = np.array([3e6, 3e6, 3e6] + [vol_after] * 5)
        out.append((hours(rows), qv, v4_in_v))
    return out


def gate_for(p: Bars, qv: np.ndarray, coin_free: bool) -> Callable[[int, float], bool]:
    """Demo list (volume of hours closed by t2 >= $5M), guard (R:R and stop distance at the entry) and free coin at t2:
    reads nothing after t2."""

    def passes_at(t2: int, e: float) -> bool:
        listed = float(qv[(p.t + H) <= t2].sum()) >= 5e6
        return m.h2_in_set(listed and rr(e) >= MIN_RR and (e - STOP) / e <= 0.10, coin_free)

    return passes_at


def test_h2_set_fixed_at_early_entry_does_not_read_the_4h_close() -> None:
    """Inclusion, t2 and entry of an H2 event do not change when hours after m change (incl. the 4h close that decides
    whether v4 is in V); the registered rule «(a) = v4 in V» does change, so this test fails on it."""
    variants = h2_set_variants()
    assert [v for _, _, v in variants] == [True, False, False]
    got = [m.h2_event(p, T0, 4 * H, MID, STOP, TGT, gate_for(p, qv, True)) for p, qv, _ in variants]
    assert got[0] == (2, T0 + 3 * H, 100.6)
    assert got == [got[0]] * len(got)
    registered = [g is not None and v for g, (_, _, v) in zip(got, variants, strict=True)]
    assert len(set(registered)) > 1  # the registered rule selects on the future
    for p, qv, _ in variants:  # a coin busy at t2 drops the event in every variant (fallback to v4, Δ = 0)
        assert m.h2_event(p, T0, 4 * H, MID, STOP, TGT, gate_for(p, qv, False)) is None


def test_h2_event_falls_back_when_the_early_entry_fails_the_guard() -> None:
    rows = list(BASE)
    rows[2] = (99.5, 102.5, 99.2, 102.0)  # entry 102.0: R:R 8/7 = 1.14 < 1.5 (above mid + 0.2 * (mid - stop) = 101)
    p = hours(rows)
    assert m.early_entry(p, T0, 4 * H, MID, STOP, TGT) == (2, 102.0)
    assert m.h2_event(p, T0, 4 * H, MID, STOP, TGT, gate_for(p, np.full(4, 3e6), True)) is None


def test_h2_delta() -> None:
    assert m.h2_delta(True, True, 0.5, -1.0) == 1.5
    assert m.h2_delta(True, False, 0.5, -1.0) == 0.5  # v4 would not trade: H2 adds the trade
    assert m.h2_delta(False, True, 0.5, -1.0) == 0.0  # fallback: the v4 trade itself, Δ = 0
    assert m.h2_in_set(True, True) and not m.h2_in_set(True, False) and not m.h2_in_set(False, True)


def test_entry_at_hour_placebo() -> None:
    p = hours(BASE)
    assert m.entry_at_hour(p, 1, 1, 95.0, 110.0) == 99.5
    assert m.entry_at_hour(p, 1, 2, 95.0, 110.0) == 100.6
    assert m.entry_at_hour(p, 1, 2, 99.1, 110.0) is None  # hour 1 low 99.0 touched this stop
    assert m.entry_at_hour(p, 2, 1, 95.0, 110.0) is None


def test_partial_spearman_removes_a_common_driver() -> None:
    rng = np.random.default_rng(1)
    z = rng.normal(size=4000)
    x = z + 0.1 * rng.normal(size=4000)
    y = z + 0.1 * rng.normal(size=4000)
    from tradedesk.analytics.s28_modules import spearman

    assert spearman(x, y) > 0.9
    assert abs(m.partial_spearman(x, y, z)) < 0.1
    assert math.isnan(m.partial_spearman([1, 2, 3], [1, 2, 3], [1, 2, 3]))
