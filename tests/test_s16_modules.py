"""S16 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): analytic values and no look-ahead."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import s16_modules as m
from tradedesk.analytics.course_basics import range_events
from tradedesk.analytics.course_s14_ema import Bars, atr, segment_pos
from tradedesk.analytics.stop_management import Fees, StopRule, simulate_stop_rule

H = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % H)
ZERO = Fees(0.0, 0.0, 0.0, 0.0)
RULE_B = StopRule("B", be_frac=0.5)


def bars(rows: list[tuple[float, float, float, float]], t0: int = T0) -> Bars:
    a = np.array([[t0 + i * H, o, h, lo, c] for i, (o, h, lo, c) in enumerate(rows)], dtype=float)
    return Bars.from_rows(a, H)


def test_half_exit_both_targets():
    # half 1 fills at 110 (+1R), half 2 reaches 120 (+2R): 1.5R
    p = bars([(100, 111, 99, 108), (108, 109, 101, 104), (104, 121, 103, 119)])
    r, h1, h2 = m.half_exit(p, T0, 100.0, 90.0, 110.0, 120.0, 10 * H, ZERO)
    assert h1.exit_kind == "target" and h2.exit_kind == "target"
    assert r == pytest.approx(1.5)


def test_half_exit_runner_break_even():
    # the middle is touched on bar 0, the runner's stop moves to the entry from bar 1 and is hit: 0.5 * 1 + 0.5 * 0
    p = bars([(100, 111, 99, 108), (108, 109, 99.5, 100), (100, 130, 99, 129)])
    r, _, h2 = m.half_exit(p, T0, 100.0, 90.0, 110.0, 120.0, 10 * H, ZERO)
    assert h2.exit_kind == "be"
    assert r == pytest.approx(0.5)
    with pytest.raises(ValueError):
        m.half_exit(p, T0, 100.0, 90.0, 125.0, 120.0, 10 * H, ZERO)


def test_time_exit_fires_without_progress():
    rows = [(100, 104, 98, 103), (103, 105, 101, 104), (104, 106, 102, 105), (105, 115, 104, 114), (114, 121, 113, 120)]
    p = bars(rows)
    base = simulate_stop_rule(p, T0, 100.0, 90.0, 120.0, 10 * H, RULE_B, fees=ZERO)
    assert base.exit_kind == "target"
    r, alive, fired = m.time_exit(p, T0, 100.0, 90.0, 120.0, 110.0, 3 * H, RULE_B, ZERO, base)
    assert alive and fired
    assert r == pytest.approx(0.5)  # close of the third bar 105: (105 - 100) / 10


def test_time_exit_keeps_base_after_progress_or_stop():
    rows = [(100, 104, 98, 103), (103, 110.5, 101, 104), (104, 106, 102, 105), (105, 121, 104, 120)]
    p = bars(rows)
    base = simulate_stop_rule(p, T0, 100.0, 90.0, 120.0, 10 * H, RULE_B, fees=ZERO)
    r, alive, fired = m.time_exit(p, T0, 100.0, 90.0, 120.0, 110.0, 3 * H, RULE_B, ZERO, base)
    assert alive and not fired and r == pytest.approx(base.r_net)
    q = bars([(100, 101, 89, 90), (90, 95, 88, 94), (94, 96, 93, 95)])
    bq = simulate_stop_rule(q, T0, 100.0, 90.0, 120.0, 10 * H, RULE_B, fees=ZERO)
    r, alive, fired = m.time_exit(q, T0, 100.0, 90.0, 120.0, 110.0, 3 * H, RULE_B, ZERO, bq)
    assert not alive and not fired and r == pytest.approx(-1.0)


def test_placebo_hours():
    assert m.placebo_hours(0.0, 48 * H) == 1
    assert m.placebo_hours(0.5, 48 * H) == 24
    assert m.placebo_hours(0.999, 48 * H) == 48


def test_volume_profile_and_value_area():
    edges, v = m.volume_profile(np.array([0.0, 1.0]), np.array([4.0, 2.0]), np.array([4.0, 10.0]), 1.0)
    assert edges.tolist() == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert v.tolist() == pytest.approx([1.0, 11.0, 1.0, 1.0])
    assert m.value_area(edges, v) == pytest.approx((1.0, 1.5, 2.0))  # the POC bin alone holds 11 / 14 >= 70 %
    e2 = np.arange(5, dtype=float)
    assert m.value_area(e2, np.array([1.0, 3.0, 5.0, 2.0])) == pytest.approx((1.0, 2.5, 3.0))  # adds the larger (below)
    assert m.value_area(e2, np.array([0.0, 2.0, 5.0, 2.0])) == pytest.approx((2.0, 2.5, 4.0))  # tie: above first
    edges, v = m.volume_profile(np.array([0.0, 2.5]), np.array([2.5, 2.5]), np.array([5.0, 1.0]), 1.0)
    assert edges.tolist() == [0.0, 1.0, 2.0, 2.5]  # last bin truncated at the top
    assert v.tolist() == pytest.approx([2.0, 2.0, 2.0])  # the flat bar falls into the top bin
    assert all(math.isnan(x) for x in m.value_area(edges, np.zeros(3)))


def test_strong_return_and_depth():
    assert m.strong_return(100.0, 106.0, 99.0, 105.5, 102.0, 4.0) == (True, True, True)
    assert m.strong_return(100.0, 106.0, 99.0, 101.5, 101.0, 4.0) == (False, False, False)
    assert m.strong_return(100.0, 106.0, 99.0, 105.5, 105.0, 4.0) == (True, True, False)
    assert m.depth_atr(100.0, 96.0, 2.0) == pytest.approx(2.0)
    assert math.isnan(m.depth_atr(100.0, 96.0, 0.0))


def test_no_lookahead():
    rng = np.random.default_rng(7)
    n = 400
    c = 100 + np.cumsum(rng.normal(0, 0.5, n))
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) + rng.uniform(0, 0.5, n)
    lo = np.minimum(o, c) - rng.uniform(0, 0.5, n)
    vol = rng.uniform(1, 10, n)
    p = bars(list(zip(o, hi, lo, c, strict=True)))
    t_in = T0 + 200 * H
    vah = m.range_vah(p, vol, T0 + 80 * H, T0 + 200 * H, 0.25)
    e = float(c[199])
    st, tg, mid = e - 5.0, e + 50.0, e + 25.0
    n_ms = 48 * H
    base = simulate_stop_rule(p, t_in, e, st, tg, 168 * H, RULE_B, fees=ZERO)
    r7 = m.time_exit(p, t_in, e, st, tg, mid, n_ms, RULE_B, ZERO, base)
    # change everything after the entry (H8 profile) and after the H7 deadline (its decision); H1 / H3: next test
    c2, hi2, lo2, vol2 = c.copy(), hi.copy(), lo.copy(), vol.copy()
    c2[200:] += 30.0
    hi2[200:] += 40.0
    lo2[200:] += 20.0
    vol2[200:] *= 100.0
    q = bars(list(zip(o, hi2, lo2, c2, strict=True)))
    assert m.range_vah(q, vol2, T0 + 80 * H, T0 + 200 * H, 0.25) == pytest.approx(vah)
    later = 248
    c3, hi3, lo3 = c.copy(), hi.copy(), lo.copy()
    c3[later:] -= 3.0
    hi3[later:] -= 3.0
    lo3[later:] -= 3.0
    q3 = bars(list(zip(np.r_[o[:later], c3[later - 1 : -1]], hi3, lo3, c3, strict=True)))
    b3 = simulate_stop_rule(q3, t_in, e, st, tg, 168 * H, RULE_B, fees=ZERO)
    r3 = m.time_exit(q3, t_in, e, st, tg, mid, n_ms, RULE_B, ZERO, b3)
    assert r7[1:] == r3[1:]
    if r7[2]:
        assert r3[0] == pytest.approx(r7[0])


def _event_features(b: Bars) -> dict[int, tuple]:
    """H1 (k), H3 conditions and depth for every deviation, keyed by its decision bar r (as in verify_s16_modules)."""
    pos = segment_pos(b.t)
    a = atr(b, 14, pos)
    out = {}
    for ev in range_events(b, pos, a, w=30, width_atr=6.0):
        if ev.kind != "dev":
            continue
        i = ev.r
        h3 = m.strong_return(float(b.o[i]), float(b.h[i]), float(b.low[i]), float(b.c[i]), float(ev.lr), float(a[i]))
        out[i] = (ev.s, ev.r - ev.s, ev.lr, ev.mid, h3, m.depth_atr(float(ev.lr), float(ev.ld), float(a[i])))
    return out


def test_no_lookahead_h1_h3():
    # mean-reverting closes make ranges and deviations; changing bars after a decision bar r must not change
    # the event decided at r, its k (H1), the H3 conditions of bar r or the depth
    rng = np.random.default_rng(11)
    n = 2400
    c = np.empty(n)
    c[0] = 100.0
    for j in range(1, n):
        c[j] = c[j - 1] + 0.3 * (100.0 - c[j - 1]) + rng.normal(0, 1.0)
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) + rng.uniform(0, 0.4, n)
    lo = np.minimum(o, c) - rng.uniform(0, 0.4, n)
    base = _event_features(bars(list(zip(o, hi, lo, c, strict=True))))
    assert len(base) >= 3
    cut = sorted(base)[len(base) // 2]  # change everything after this decision bar
    c2 = c.copy()
    c2[cut + 1 :] = c[cut + 1 :] * 0.9 + rng.normal(0, 3.0, n - cut - 1)
    o2 = np.r_[o[: cut + 1], c2[cut:-1]]
    hi2 = np.r_[hi[: cut + 1], np.maximum(o2, c2)[cut + 1 :] + 0.5]
    lo2 = np.r_[lo[: cut + 1], np.minimum(o2, c2)[cut + 1 :] - 0.5]
    moved = _event_features(bars(list(zip(o2, hi2, lo2, c2, strict=True))))
    before = {r: v for r, v in base.items() if r <= cut}
    assert before
    assert {r: v for r, v in moved.items() if r <= cut} == before
    assert {r: v for r, v in moved.items() if r > cut} != {r: v for r, v in base.items() if r > cut}  # the change bites
