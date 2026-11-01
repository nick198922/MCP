"""S17 modules (analytics/s17_modules.py): analytic expectations and causality."""

from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics import s17_modules as m
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import Fees, risk_unit

H = 3_600_000
T0 = 1_700_000_000_000 // H * H


def bars(rows: list[tuple[float, float, float, float]], bar_ms: int = H, t0: int = T0) -> Bars:
    a = np.array(rows, dtype=float)
    t = (t0 + bar_ms * np.arange(len(rows))).astype(np.int64)
    return Bars(t, a[:, 0], a[:, 1], a[:, 2], a[:, 3], bar_ms)


def test_trough_depth_uses_the_minimum_before_the_break() -> None:
    #        o    h    l    c
    rows = [
        (100, 101, 99, 100),  # 0
        (100, 106, 99, 105),  # 1 p: close 105
        (105, 105, 90, 91),  # 2 q: low 90
        (91, 92, 80, 85),  # 3 trough 80
        (85, 100, 84, 99),  # 4
        (99, 110, 98, 108),  # 5 k: break
    ]
    b = bars(rows)
    atr = np.full(len(rows), 5.0)
    d, lag = m.trough_depth(b, atr, 1, 2, 5)
    assert d == (105 - 80) / 5.0
    assert lag == 2  # p=1 -> trough at 3


def test_trough_depth_ignores_the_break_bar_and_later() -> None:
    rows = [(100, 101, 99, 100), (100, 106, 99, 105), (105, 105, 90, 91), (91, 99, 88, 98), (98, 110, 97, 108)]
    atr = np.full(len(rows), 5.0)
    base = m.trough_depth(bars(rows), atr, 1, 2, 4)
    changed = [*rows[:4], (98, 110, 1.0, 108), (108, 120, 0.5, 119)]  # break bar k=4 and a later bar dive
    assert m.trough_depth(bars(changed), np.full(len(changed), 5.0), 1, 2, 4) == base
    assert base == ((105 - 88) / 5.0, 2)


def test_trough_depth_nan_without_atr() -> None:
    rows = [(100, 101, 99, 100), (100, 106, 99, 105), (105, 105, 90, 91), (91, 99, 88, 98)]
    d, lag = m.trough_depth(bars(rows), np.array([5.0, 0.0, 5.0, 5.0]), 1, 2, 3)
    assert math.isnan(d) and lag == -1


def test_reclaim_entry_first_close_back_above_the_level() -> None:
    level, stop = 100.0, 90.0
    rows = [
        (101, 102, 99, 99.5),  # 0 fill bar (low < level), closes below
        (99.5, 100, 97, 98),  # 1
        (98, 101, 97.5, 100.5),  # 2 reclaim: close >= level
        (100.5, 103, 100, 102),  # 3
    ]
    assert m.reclaim_entry(bars(rows), 0, level, stop) == (2, 100.5)
    rows_same_bar = [(101, 102, 99, 100.0), *rows[1:]]  # the fill bar itself closes at the level
    assert m.reclaim_entry(bars(rows_same_bar), 0, level, stop) == (0, 100.0)


def test_reclaim_entry_fails_on_stop_or_after_24_hours() -> None:
    level, stop = 100.0, 90.0
    stop_first = [(101, 102, 99, 99.5), (99.5, 100, 89.9, 95), (95, 101, 94, 100.5)]
    assert m.reclaim_entry(bars(stop_first), 0, level, stop) is None
    wick_stop = [(101, 102, 89.0, 100.5)]  # touches the stop and closes back above: failed
    assert m.reclaim_entry(bars(wick_stop), 0, level, stop) is None
    slow = [(101, 102, 99, 99.5)] + [(99.5, 99.8, 98, 99)] * 23 + [(99, 101, 98.5, 100.5)]  # reclaim at hour 24
    assert m.reclaim_entry(bars(slow), 0, level, stop) is None
    slow_ok = [(101, 102, 99, 99.5)] + [(99.5, 99.8, 98, 99)] * 22 + [(99, 101, 98.5, 100.5)]  # hour 23: inside
    assert m.reclaim_entry(bars(slow_ok), 0, level, stop) == (23, 100.5)


def test_reclaim_entry_does_not_read_bars_after_the_entry() -> None:
    level, stop = 100.0, 90.0
    head = [(101, 102, 99, 99.5), (99.5, 101, 98, 100.5)]
    a = m.reclaim_entry(bars(head + [(100, 100, 50, 60)] * 5), 0, level, stop)
    b = m.reclaim_entry(bars(head + [(100, 200, 99, 190)] * 5), 0, level, stop)
    assert a == b == (1, 100.5)


def test_taker_entry_cost() -> None:
    f = Fees()
    c = m.taker_entry_cost(100.0, 90.0, f)
    assert math.isclose(c, (f.taker_exit - f.maker) * 100.0 / risk_unit(100.0, 90.0, f))
    assert c > 0
    assert math.isclose(
        m.taker_entry_cost(100.0, 90.0, f.scaled(1.5)), 1.5 * (f.taker_exit - f.maker) * 100.0 / risk_unit(100.0, 90.0, f), rel_tol=1e-12
    )


def test_target_before_fill_reads_only_bars_before_the_fill() -> None:
    rows = [(100, 101, 99, 100), (100, 125, 99, 120), (119, 119.5, 95, 96), (96, 200, 90, 150)]
    b = bars(rows)
    assert m.target_before_fill(b, int(b.t[0]), int(b.t[2]), 120.0)
    assert not m.target_before_fill(b, int(b.t[0]), int(b.t[1]), 120.0)
    assert not m.target_before_fill(b, int(b.t[2]), int(b.t[3]), 120.0)  # bar 3 is the fill bar: not read


def test_first_reach_window() -> None:
    rows = [(100, 101, 99, 100), (100, 119, 99, 110), (110, 121, 95, 96), (96, 200, 90, 150)]
    b = bars(rows)
    assert m.first_reach(b, int(b.t[0]), int(b.t[3]) + H, 120.0) == int(b.t[2])
    assert m.first_reach(b, int(b.t[0]), int(b.t[2]), 120.0) == -1  # bar 2 opens at t1: outside
    changed = bars([*rows[:2], (110, 119, 95, 96), (96, 120, 90, 150)])
    assert m.first_reach(changed, int(b.t[0]), int(b.t[2]), 120.0) == -1  # later bars do not matter


def test_placebo_parent_is_unique_when_two_pobs_share_a_key() -> None:
    # One break bar breaks two POBs: same key sym|tf|side|t0, levels 100 and 95, both stops at 90.
    key = f"XUSDT|1d|long|{T0}"
    parents = [(key, abs(100 - 90) / 100), (key, abs(95 - 90) / 95), (f"YUSDT|1d|long|{T0}", 0.10)]
    pid = [m.parent_uid(k, x) for k, x in parents]
    assert len(set(pid)) == 3

    def child(entry: float, stop: float, c_sig: float, c_r: float) -> str:  # the runner's placebo geometry
        e_r, st_r = c_r * entry / c_sig, c_r * stop / c_sig
        return m.parent_uid(key, abs(e_r - st_r) / abs(e_r))

    kids = [child(100, 90, 101.0, 138.7), child(95, 90, 101.0, 73.1), child(100, 90, 101.0, 0.0731)]
    assert m.link_parents(pid, [True, True, True], kids).tolist() == [0, 1, 0]
    # only the chained (eligible) sibling is a parent: the other sibling's placebo gets none (the key-only rule linked it)
    assert m.link_parents(pid, [False, True, True], kids).tolist() == [-1, 1, -1]
    assert m.link_parents(pid, [True, True, True], [m.parent_uid("ZUSDT|1d|long|1", 0.1)]).tolist() == [-1]
    # two eligible parents with the same id: the link is ambiguous and dropped; a non-eligible twin does not count
    twins = [pid[0], pid[0], pid[1]]
    assert m.link_parents(twins, [True, True, True], kids).tolist() == [-1, 2, -1]
    assert m.link_parents(twins, [False, True, True], kids).tolist() == [1, 2, 1]


def test_cluster_weights_and_drawdown() -> None:
    w = m.cluster_weights(["long", "long", "short", "long"], [1, 1, 1, 2])
    assert np.allclose(w, [1 / math.sqrt(2), 1 / math.sqrt(2), 1.0, 1.0])
    assert m.max_drawdown([1.0, -2.0, 0.5, -1.0, 3.0]) == 2.5
    assert m.max_drawdown([-1.0, -1.0]) == 2.0
    assert m.max_drawdown([]) == 0.0
