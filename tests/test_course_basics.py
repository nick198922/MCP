import numpy as np
import pytest

from tradedesk.analytics import markup
from tradedesk.analytics.course_basics import (
    HOUR_MS,
    MARKET_SIDE,
    Bars,
    bar_shapes,
    bos_events,
    depth_path,
    eq_pools,
    fractal_mask,
    fractal_n_lows,
    level_tests,
    null_depth,
    ote_legs,
    race,
    range_events,
    trade_r,
    untaken_fractal_lows_near,
)
from tradedesk.analytics.course_s14_ema import atr as atr_series
from tradedesk.analytics.course_s18_trend import pos_from_seg, segments

T0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
B4 = 4 * HOUR_MS


def walk(n: int, seed: int, bar_ms: int = B4) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.concatenate([[100.0], c[:-1]]) + rng.normal(0, 0.3, n)
    h = np.maximum(o, c) + rng.exponential(0.5, n)
    lo = np.minimum(o, c) - rng.exponential(0.5, n)
    t = T0 + np.arange(n, dtype=np.int64) * bar_ms
    return Bars(t, o, h, lo, c, bar_ms)


def bars(h, lo, c=None, o=None, bar_ms: int = B4) -> Bars:
    h = np.asarray(h, float)
    lo = np.asarray(lo, float)
    c = (h + lo) / 2 if c is None else np.asarray(c, float)
    o = c.copy() if o is None else np.asarray(o, float)
    t = T0 + np.arange(h.size, dtype=np.int64) * bar_ms
    return Bars(t, o, h, lo, c, bar_ms)


def pos_of(b: Bars) -> np.ndarray:
    return pos_from_seg(segments(b.t))


def as_rows(b: Bars) -> list[dict]:
    return [{"ts": int(b.t[i]), "open": b.o[i], "high": b.h[i], "low": b.low[i], "close": b.c[i]} for i in range(len(b))]


def perturb_after(b: Bars, cut: int, seed: int) -> Bars:
    """Same bars before ``cut``, a different random walk from ``cut`` on."""
    other = walk(len(b), seed)
    shift = b.c[cut - 1] - other.o[cut]
    cat = lambda x, y: np.concatenate([x[:cut], y[cut:] + shift])  # noqa: E731
    return Bars(b.t, cat(b.o, other.o), cat(b.h, other.h), cat(b.low, other.low), cat(b.c, other.c), b.bar_ms)


# ---------------------------------------------------------------- swings


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fractals_match_markup(seed):
    b = walk(400, seed)
    pos = pos_of(b)
    ref = markup.fractals(as_rows(b))
    assert list(np.flatnonzero(fractal_mask(b.h, pos))) == [s["idx"] for s in ref["highs"]]
    assert list(np.flatnonzero(fractal_mask(-b.low, pos))) == [s["idx"] for s in ref["lows"]]


def test_fractal_n_lows_strict():
    lo = np.array([5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 0, 5], float)
    b = bars(lo + 1, lo)
    m = fractal_n_lows(b.low, pos_of(b), 5)
    assert list(np.flatnonzero(m)) == [5]  # bar 11 has only one bar on its right


def test_fractals_do_not_cross_a_data_hole():
    b = walk(50, 4)
    t = b.t.copy()
    t[25:] += 100 * HOUR_MS  # hole between bars 24 and 25
    b2 = Bars(t, b.o, b.h, b.low, b.c, b.bar_ms)
    pos = pos_of(b2)
    m = fractal_mask(b2.h, pos)
    assert not m[24] and not m[25]


# ---------------------------------------------------------------- races


def hourly(o, h, lo, c) -> Bars:
    t = T0 + np.arange(len(c), dtype=np.int64) * HOUR_MS
    return Bars(t, np.asarray(o, float), np.asarray(h, float), np.asarray(lo, float), np.asarray(c, float), HOUR_MS)


def test_race_outcomes():
    p = hourly([100, 100, 101, 102], [100.5, 101, 103, 104], [99.5, 99.8, 100.5, 101], [100, 101, 102, 103])
    assert race(p, T0, 102.5, 99.0, 10 * HOUR_MS) == race(p, T0, 102.5, 99.0, 10 * HOUR_MS)
    r = race(p, T0, 102.5, 99.0, 10 * HOUR_MS)
    assert r.outcome == 1 and r.exit_px == 102.5 and r.exit_ms == T0 + 3 * HOUR_MS
    r = race(p, T0, 110, 99.6, 10 * HOUR_MS)
    assert r.outcome == 0 and r.exit_px == 99.6
    r = race(p, T0, 110, 90, 2 * HOUR_MS)  # horizon: two bars, nothing reached -> last close
    assert r.outcome == -1 and r.exit_px == 101
    r = race(p, T0, 100.4, 99.6, 10 * HOUR_MS)  # first bar touches both -> down
    assert r.outcome == 0
    assert race(p, T0 + 200 * HOUR_MS, 110, 90, HOUR_MS) is None


def test_race_gap_through_down_exits_at_open():
    p = hourly([100, 95], [100.5, 96], [99.5, 94], [100, 95])
    r = race(p, T0, 110, 98, 5 * HOUR_MS)
    assert r.outcome == 0 and r.exit_px == 95


def test_trade_r():
    g, c = trade_r(100.0, 98.0, 104.0)
    assert g == pytest.approx(2.0)
    assert c == pytest.approx(MARKET_SIDE * 204.0 / 2.0)
    with pytest.raises(ValueError):
        trade_r(100.0, 101.0, 102.0)


# ---------------------------------------------------------------- H1


def test_bos_events_body_and_wick():
    #        0   1   2   3   4   5   6   7   8   9
    h = [10, 11, 13, 11, 10, 11, 14, 12, 11, 12.5]
    lo = [9, 9.5, 11, 9, 7, 8, 12, 10, 9.5, 11]
    c = [9.5, 10.5, 12, 10, 8, 10, 13.5, 11, 10, 11.5]
    b = bars(h, lo, c)
    ev = bos_events(b, pos_of(b))
    # fractal high 2 (13) confirmed at bar 4; bar 6 exceeds it with close 13.5 > 13: body BOS
    assert [(e.t, e.ref, e.level, e.body) for e in ev][0] == (6, 2, 13.0, True)
    # opposite: fractal low 4 (7) confirmed at bar 6, untaken
    assert ev[0].opp == 7.0 and ev[0].opp_idx == 4
    # fractal high 6 (14) confirmed at bar 8; no later bar exceeds it -> one event only
    assert len(ev) == 1


def test_bos_wick_only_and_taken_opposite():
    h = [10, 11, 13, 11, 10, 13.5, 12, 11]
    lo = [9, 9.5, 11, 8, 9, 6, 10, 9]
    c = [9.5, 10.5, 12, 10, 9.5, 12.5, 11, 10]
    b = bars(h, lo, c)
    ev = bos_events(b, pos_of(b))
    # bar 5 wicks above 13 and closes at 12.5: fake BOS; its low (6) takes the fractal low 3 (8) -> no opposite
    assert ev[0].t == 5 and not ev[0].body
    assert np.isnan(ev[0].opp) and ev[0].opp_idx == -1


# ---------------------------------------------------------------- H2


def test_eq_pools_and_singles():
    #      0  1   2   3  4   5     6  7  8   9  10
    h = [5, 6, 10, 7, 6, 9.9, 7, 6, 8, 6, 5]
    lo = [x - 2 for x in h]
    b = bars(h, lo)
    pos = pos_of(b)
    a = np.full(len(b), 1.0)
    pools, singles = eq_pools(b, pos, a, tol=0.25)
    # fractal 2 (10) is a single at t = 4; fractal 5 (9.9) pairs with it at t = 7 (|10 - 9.9| <= 0.25)
    assert singles[0] == (4, 2, 10.0)
    assert pools == [(7, 2, 5, 10.0)]
    # fractal 8 (8): 10 and 9.9 are too far -> single at t = 10
    assert singles[-1] == (10, 8, 8.0)
    pools2, _ = eq_pools(b, pos, a, tol=0.05)
    assert pools2 == []


def test_eq_pool_needs_untaken_first_high():
    h = [5, 6, 10, 7, 10.2, 7, 6, 9.9, 7, 6]
    b = bars(h, [x - 2 for x in h])
    pools, _ = eq_pools(b, pos_of(b), np.full(len(b), 1.0), tol=0.25)
    # 10 is taken by 10.2 (bar 4); 9.9 pairs with 10.2 (|0.3| > 0.25 -> no), so no pool
    assert pools == []


# ---------------------------------------------------------------- H3


def test_depth_path_outcomes():
    p = hourly([10, 9, 8, 9, 10], [10, 9.5, 9, 10, 11.5], [9, 8, 7, 8.5, 9.5], [9, 8, 8.5, 9.5, 11])
    kind, d = depth_path(p, T0, 11.0, 5.0, 0.1, 10 * HOUR_MS)
    assert kind == "up" and d == pytest.approx((11 - 7) / 6)
    kind, d = depth_path(p, T0, 11.0, 7.5, 0.1, 10 * HOUR_MS)
    assert kind == "break" and d == 1.0
    kind, d = depth_path(p, T0, 20.0, 5.0, 0.1, 2 * HOUR_MS)
    assert kind == "timeout" and d == pytest.approx((20 - 8) / 15)


def test_ote_legs_from_bos():
    #    0   1   2   3   4   5    6   7   8   9   10
    h = [10, 11, 13, 11, 10, 11, 14, 16, 15, 14, 14]
    lo = [9, 9.5, 11, 9, 7, 8, 12, 14, 13, 13, 12]
    c = [9.5, 10.5, 12, 10, 8, 10, 13.5, 15.5, 14, 13.5, 13]
    b = bars(h, lo, c)
    pos = pos_of(b)
    ev = bos_events(b, pos)
    legs = ote_legs(b, pos, np.full(len(b), 1.0), ev, min_atr=2.0)
    assert len(legs) == 1
    lg = legs[0]
    # BOS at 6 over fractal 2; low of 2..6 = 7; first fractal high >= 6 is 7 (16); q = 8
    assert (lg.t, lg.q, lg.hi, lg.lo) == (6, 8, 16.0, 7.0)
    assert lg.d0 == pytest.approx((16 - 13) / 9)


def test_null_depth_symmetric_walk():
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, 2000)
    shapes = np.column_stack([np.abs(r) * 0.0 + 0.002, -np.abs(r) * 0.0 - 0.002, r])
    # start in the middle: hi = 110, lo = 90 (log-symmetric enough), depth zones cover everything below 1
    out = null_depth(shapes, 100.0, 110.0, 90.0, 0.5, 20000, 400, np.random.default_rng(1), ((0.0, 1.0), (0.5, 0.79)))
    assert 0.40 < out[0] < 0.60  # P(new high before the break) ~ 1/2
    assert out[1] <= out[0]


def test_bar_shapes_mirrored_frame():
    b = bars([11, 12], [9, 10], [10.5, 11], [10, 10.5])
    s, m = bar_shapes(b), bar_shapes(b.mirrored())
    assert s[0] == pytest.approx([np.log(1.1), np.log(0.9), np.log(1.05)])
    assert m[0] == pytest.approx([np.log(0.9), np.log(1.1), np.log(1.05)])


# ---------------------------------------------------------------- H8 / H9


def test_level_tests_sequence():
    # n = 2 fractal low at 2 (L = 5); separation 1 ATR (= 1): highs >= 6
    #     0  1  2  3  4  5    6  7    8    9  10
    lo = [8, 7, 5, 7, 8, 6, 4.9, 7, 7.5, 5.0, 4]
    h = [x + 1.5 for x in lo]
    c = [x + 1.0 for x in lo]
    c[10] = 4.5  # closes below 5: the level dies
    b = bars(h, lo, c)
    tests = level_tests(b, pos_of(b), 2, 1.0, n=2, life=50)
    # armed at bar 4 (>= f + n, a high >= 6 after f) -> test 1 at bar 6 (low 4.9);
    # armed again at bar 7 (high 8.5 after the test) -> test 2 at bar 9; then armed at... bar 10 closes below: dead
    assert tests == [(1, 4, 6), (2, 7, 9)]


def test_level_tests_not_armed_without_separation():
    lo = [8, 7, 5, 5.5, 5.6, 5.2, 5.1, 7, 5.0]
    h = [x + 0.3 for x in lo]  # highs never reach 6 before bar 7 (7.3)
    b = bars(h, lo, [x + 0.2 for x in lo])
    tests = level_tests(b, pos_of(b), 2, 1.0, n=2, life=50)
    assert tests == [(1, 7, 8)]


def test_untaken_fractal_lows_near():
    lo = np.array([9, 8, 6, 8, 9, 7, 6.1, 7, 9, 10], float)
    b = bars(lo + 1, lo)
    fl = fractal_mask(-b.low, pos_of(b))
    assert untaken_fractal_lows_near(b, fl, 9, 6.0, 0.25, 100, exclude=2)  # fractal 6 (6.1) within 0.25 of 6
    assert not untaken_fractal_lows_near(b, fl, 9, 6.0, 0.05, 100, exclude=2)


# ---------------------------------------------------------------- S16


def test_range_events_deviation_and_sweep():
    n = 45
    lo = np.full(n, 99.0)
    h = np.full(n, 101.0)
    c = np.full(n, 100.0)
    # bars 30-33: four closes below 99, then 34 back at 99.5 -> deviation, ld = 97
    lo[30:34], h[30:34], c[30:34] = 97.0, 99.0, 98.0
    lo[34], h[34], c[34] = 98.5, 100.0, 99.5
    # bar 40: wick to 98.5, close 99.2 -> sweep of the 30-bar low (97 from the deviation is inside the window)
    b = bars(h, lo, c)
    atr = np.full(n, 1.0)
    ev = range_events(b, pos_of(b), atr, w=30, width_atr=6.0, kmin=3, kmax=10)
    dev = [e for e in ev if e.kind == "dev"]
    assert len(dev) == 1 and (dev[0].s, dev[0].r, dev[0].lr, dev[0].ld) == (30, 34, 99.0, 97.0)
    assert dev[0].mid == pytest.approx(100.0)
    # a narrow width limit kills it
    assert range_events(b, pos_of(b), atr, w=30, width_atr=1.5) == []


def test_range_sweep():
    n = 40
    lo, h, c = np.full(n, 99.0), np.full(n, 101.0), np.full(n, 100.0)
    lo[35], c[35] = 98.0, 99.5
    b = bars(h, lo, c)
    ev = range_events(b, pos_of(b), np.full(n, 1.0))
    assert [(e.kind, e.r, e.ld) for e in ev] == [("sweep", 35, 98.0)]


def deviation_with_inner_sweep(n: int = 50) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lo, h, c = np.full(n, 99.0), np.full(n, 101.0), np.full(n, 100.0)
    # deviation from bar 30: closes 98 < 99 on bars 30, 31, 33; bar 32 wicks to 96.5 (below the 30-bar low 97 that
    # now includes bars 30-31) and closes 97.5 >= 97: a sweep by the registered definition, inside the episode
    lo[30:34], h[30:34], c[30:34] = 97.0, 99.0, 98.0
    lo[32], c[32] = 96.5, 97.5
    lo[33] = 97.5
    lo[34], h[34], c[34] = 98.5, 100.0, 99.5  # first close back in the range: deviation s = 30, r = 34
    return lo, h, c


def test_range_sweep_inside_deviation_is_kept():
    lo, h, c = deviation_with_inner_sweep()
    b = bars(h, lo, c)
    a = np.full(len(b), 1.0)
    ev = range_events(b, pos_of(b), a)
    assert [(e.kind, e.s, e.r) for e in ev] == [("sweep", 32, 32), ("dev", 30, 34)]
    sw = ev[0]
    assert (sw.lr, sw.ld, sw.mid) == (97.0, 96.5, 99.0)
    # the registered rule drops the sweep because the deviation later closes back in the range
    old = range_events(b, pos_of(b), a, lookahead_busy=True)
    assert [(e.kind, e.s, e.r) for e in old] == [("dev", 30, 34)]


def test_range_sweep_does_not_depend_on_later_bars():
    lo, h, c = deviation_with_inner_sweep()
    lo2, h2, c2 = lo.copy(), h.copy(), c.copy()
    # after the sweep (bar 32) the price stays below the range for the rest of the data: no deviation ever resolves
    lo2[33:], h2[33:], c2[33:] = 97.5, 98.5, 98.0
    b, b2 = bars(h, lo, c), bars(h2, lo2, c2)
    a = np.full(len(b), 1.0)
    for flag in (False, True):
        e1 = [e for e in range_events(b, pos_of(b), a, lookahead_busy=flag) if e.r <= 32]
        e2 = [e for e in range_events(b2, pos_of(b2), a, lookahead_busy=flag) if e.r <= 32]
        assert (e1 == e2) is (not flag)  # causal: identical; registered rule: the sweep at 32 appears only in b2


def test_range_nested_deviation_decided_first_wins():
    n = 50
    lo, h, c = np.full(n, 99.0), np.full(n, 101.0), np.full(n, 100.0)
    # outer deviation from bar 30 (closes 98.5 < 99); bar 31 closes 96.4 below the new 30-bar low 97 (bar 30): a nested
    # deviation start; it is back above 97 at bar 34 (k = 3), while the outer one is back above 99 only at bar 36
    lo[30], h[30], c[30] = 97.0, 99.0, 98.5
    lo[31:34], h[31:34], c[31:34] = 96.0, 97.0, 96.4
    lo[34:36], h[34:36], c[34:36] = 97.5, 98.8, 98.0
    lo[36], h[36], c[36] = 98.5, 99.6, 99.4
    b = bars(h, lo, c)
    a = np.full(n, 1.0)
    ev = range_events(b, pos_of(b), a)
    # the nested deviation is decided at bar 34, before the outer one (bar 36), which then lies in its episode
    assert [(e.kind, e.s, e.r) for e in ev] == [("dev", 31, 34)]
    old = range_events(b, pos_of(b), a, lookahead_busy=True)
    assert [(e.kind, e.s, e.r) for e in old] == [("dev", 30, 36)]


@pytest.mark.parametrize("seed", [21, 23])
def test_range_events_causal_at_every_cut(seed):
    rng = np.random.default_rng(seed)
    n = 1500
    x = np.zeros(n)
    for i in range(1, n):  # mean-reverting AR(1): narrow 30-bar ranges, many deviations and sweeps
        x[i] = 0.93 * x[i - 1] + rng.normal(0, 0.8)
    c = 100 + x
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) + rng.exponential(0.4, n)
    lo = np.minimum(o, c) - rng.exponential(0.4, n)
    b = bars(h, lo, c, o)
    pos = pos_of(b)
    a = atr_series(b, 14, pos)
    full = range_events(b, pos, a)
    assert sum(e.kind == "dev" for e in full) >= 5 and sum(e.kind == "sweep" for e in full) >= 20
    old_full = range_events(b, pos, a, lookahead_busy=True)
    old_bad = 0
    for cut in range(100, n, 5):
        part = range_events(b.slice(0, cut), pos[:cut], a[:cut])
        assert part == [e for e in full if e.r < cut]
        old = range_events(b.slice(0, cut), pos[:cut], a[:cut], lookahead_busy=True)
        old_bad += old != [e for e in old_full if e.r < cut]
    assert old_bad > 0  # the test does catch the registered (look-ahead) rule on this series


# ---------------------------------------------------------------- causality


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_events_before_t_do_not_depend_on_later_bars(seed):
    b = walk(900, seed)
    cut = 600
    b2 = perturb_after(b, cut, seed + 100)
    assert np.array_equal(b.h[:cut], b2.h[:cut])
    for x in (b, b2):
        assert np.all(x.h >= x.low)
    pos = pos_of(b)
    a1, a2 = atr_series(b, 14, pos), atr_series(b2, 14, pos)
    assert np.array_equal(a1[:cut], a2[:cut], equal_nan=True)

    def before(evs, key):
        return [repr(e) for e in evs if key(e) < cut]  # repr: nan fields compare equal

    e1, e2 = bos_events(b, pos), bos_events(b2, pos)
    assert before(e1, lambda e: e.t) == before(e2, lambda e: e.t)
    p1, s1 = eq_pools(b, pos, a1, 0.25)
    p2, s2 = eq_pools(b2, pos, a2, 0.25)
    assert before(p1, lambda e: e[0]) == before(p2, lambda e: e[0])
    assert before(s1, lambda e: e[0]) == before(s2, lambda e: e[0])
    l1, l2 = ote_legs(b, pos, a1, e1), ote_legs(b2, pos, a2, e2)
    assert before(l1, lambda e: e.q) == before(l2, lambda e: e.q)
    r1, r2 = range_events(b, pos, a1), range_events(b2, pos, a2)
    assert before(r1, lambda e: e.r) == before(r2, lambda e: e.r)
    fn1, fn2 = fractal_n_lows(b.low, pos), fractal_n_lows(b2.low, pos)
    assert np.array_equal(fn1[: cut - 5], fn2[: cut - 5])
    for f in np.flatnonzero(fn1[: cut - 5]):
        t1 = [x for x in level_tests(b, pos, int(f), float(a1[f + 5])) if x[2] < cut]
        t2 = [x for x in level_tests(b2, pos, int(f), float(a2[f + 5])) if x[2] < cut]
        assert t1 == t2
    assert len(e1) > 20 and len(p1) > 0 and len(l1) > 0
