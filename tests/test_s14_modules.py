"""S14 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): analytic values, the detector's structure and no
look-ahead."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import s3_modules as m3
from tradedesk.analytics import s14_modules as m
from tradedesk.analytics.course_s14_ema import Bars, ema, find_signals, fractal_lows, indicators
from tradedesk.analytics.setup_signals import S14_PARAMS, s14_all
from tradedesk.analytics.stop_management import Fees, StopRule, simulate_stop_rule

H = 3_600_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % (24 * H))  # a UTC midnight
ZERO = Fees(0.0, 0.0, 0.0, 0.0)
RULE_B = StopRule("B", be_frac=0.5)
NO_BE = StopRule("none")


def bars(rows: list[tuple[float, float, float, float]], t0: int = T0, bar_ms: int = H) -> Bars:
    a = np.array([[t0 + i * bar_ms, o, h, lo, c] for i, (o, h, lo, c) in enumerate(rows)], dtype=float)
    return Bars.from_rows(a, bar_ms)


def trending(n: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Up legs with pullbacks: produces S14 sweeps."""
    rng = np.random.default_rng(seed)
    steps = []
    while len(steps) < n:
        steps += list(rng.normal(0.5, 1.0, int(rng.integers(20, 30))))
        steps += list(rng.normal(-0.3, 1.0, int(rng.integers(6, 12))))
    c = 100 + np.cumsum(np.array(steps[:n]))
    o = np.r_[c[0], c[:-1]]
    hi = np.maximum(o, c) + rng.uniform(0.05, 0.8, n)
    lo = np.minimum(o, c) - rng.uniform(0.05, 0.8, n)
    return o, hi, lo, c


# ---------------------------------------------------------------- analytic values


def test_channel_breakout():
    h = np.array([5.0, 7.0, 6.0, 8.0, 7.5])
    pos = np.arange(5)
    assert m.channel_breakout(h, pos, 3, 3) is True  # 8 > max(7, 6, 5)
    assert m.channel_breakout(h, pos, 4, 3) is False  # 7.5 < 8
    assert m.channel_breakout(h, pos, 2, 3) is None  # fewer than 3 bars before
    assert m.channel_breakout(h, np.array([0, 1, 2, 0, 1]), 4, 3) is None  # segment restarted


def test_whole_pullback():
    low = np.array([10.0, 9.0, 8.5, 9.2, 8.0, 8.7, 7.9])
    assert m.whole_pullback(low, 0, 4, 6) is True  # 8.0 is the lowest of bars 0 .. 5
    assert m.whole_pullback(low, 0, 2, 6) is False  # 8.0 at bar 4 is lower than 8.5
    assert m.whole_pullback(np.array([9.0, 8.0, 8.0, 8.5]), 0, 2, 3) is True  # ties count
    with pytest.raises(ValueError):
        m.whole_pullback(low, 3, 2, 6)


def test_zone_distance_and_zone():
    assert m.sweep_zone(95.0, 98.0, 2.0) == (94.0, 99.0)
    assert m.zone_distance([96.0], 94.0, 99.0, 2.0) == 0.0
    assert m.zone_distance([100.0, 90.0], 94.0, 99.0, 2.0) == pytest.approx(0.5)
    assert m.zone_distance([math.nan, 103.0], 94.0, 99.0, 2.0) == pytest.approx(2.0)
    assert math.isnan(m.zone_distance([math.nan], 94.0, 99.0, 2.0))
    assert math.isnan(m.zone_distance([96.0], 94.0, 99.0, 0.0))


def test_quantile_hours():
    d = np.array([5, 1, 3, 2, 4])
    assert m.quantile_hours(d, 0.0) == 1
    assert m.quantile_hours(d, 0.5) == 3
    assert m.quantile_hours(d, 0.999) == 5
    with pytest.raises(ValueError):
        m.quantile_hours(np.array([]), 0.5)


# ---------------------------------------------------------------- H10 runner


def _tf(closes: list[tuple[int, float, float]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a = np.array(closes, dtype=float)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2]


def test_runner_exits_under_ema():
    # entry 100, stop 90, target 120 hit on bar 2; the 4-bar close at bar 3's close (T0 + 4h) is above its EMA, the
    # one at T0 + 8h (bar 7's close 125) is below its EMA 126: runner +2.5R, position (2 + 2.5) / 2
    rows = [(100, 105, 99, 104), (104, 112, 103, 110), (110, 121, 109, 119), (119, 124, 118, 123)]
    rows += [(123, 128, 122, 127), (127, 129, 125, 126), (126, 127, 124, 125), (125, 126, 124, 125)]
    rows += [(125, 140, 124, 139)] * 4
    p = bars(rows)
    base = simulate_stop_rule(p, T0, 100.0, 90.0, 120.0, 100 * H, RULE_B, fees=ZERO)
    assert base.exit_kind == "target" and base.r_net == pytest.approx(2.0)
    cms, cc, ce = _tf([(T0 + 4 * H, 123.0, 110.0), (T0 + 8 * H, 125.0, 126.0), (T0 + 12 * H, 139.0, 127.0)])
    rr, ex, kind = m.runner_after_target(p, T0, 100.0, 90.0, 100 * H, ZERO, base, cms, cc, ce)
    assert kind == "ema" and ex == T0 + 8 * H and rr == pytest.approx(2.5)
    assert m.half_and_runner(base.r_net, rr) == pytest.approx(2.25)


def test_runner_break_even_and_time():
    rows = [(100, 121, 99, 119), (119, 120, 99.5, 101), (101, 105, 100, 104)]
    p = bars(rows)
    base = simulate_stop_rule(p, T0, 100.0, 90.0, 120.0, 100 * H, RULE_B, fees=ZERO)
    assert base.exit_kind == "target"
    none = (np.zeros(0, dtype=np.int64), np.zeros(0), np.zeros(0))
    rr, _, kind = m.runner_after_target(p, T0, 100.0, 90.0, 100 * H, ZERO, base, *none)
    assert kind == "be" and rr == pytest.approx(0.0)
    q = bars([(100, 121, 99, 119), (119, 122, 118, 121), (121, 125, 120, 124)])
    bq = simulate_stop_rule(q, T0, 100.0, 90.0, 120.0, 3 * H, RULE_B, fees=ZERO)
    rr, ex, kind = m.runner_after_target(q, T0, 100.0, 90.0, 3 * H, ZERO, bq, *none)
    assert kind == "time" and ex == T0 + 3 * H and rr == pytest.approx(2.4)
    with pytest.raises(ValueError):
        b2 = simulate_stop_rule(p, T0, 100.0, 90.0, 130.0, 100 * H, RULE_B, fees=ZERO)
        m.runner_after_target(p, T0, 100.0, 90.0, 100 * H, ZERO, b2, *none)


def test_runner_curve_matches_hours():
    rng = np.random.default_rng(3)
    n = 120
    c = 100 + np.cumsum(rng.normal(0.25, 1.0, n))
    o = np.r_[100.0, c[:-1]]
    hi = np.maximum(o, c) + 0.5
    lo = np.minimum(o, c) - 0.5
    p = bars(list(zip(o, hi, lo, c, strict=True)))
    fees = Fees()
    base = simulate_stop_rule(p, T0, 100.0, 90.0, 104.0, 100 * H, NO_BE, fees=fees)
    assert base is not None and base.exit_kind == "target"
    curve = m.runner_curve(p, T0, 100.0, 90.0, 100 * H, fees, base)
    assert curve.size > 3
    for h in range(0, curve.size + 5):
        assert m.runner_at_hours(p, T0, 100.0, 90.0, 100 * H, fees, base, h) == pytest.approx(curve[min(h, curve.size - 1)])


# ---------------------------------------------------------------- detector structure (the passport's trades)


def _features(b: Bars) -> dict[int, tuple]:
    """(k, f, H7, H1, H2 distance) per long sweep signal, as verify_s14_modules computes them (1h bars here)."""
    ind = indicators(b, S14_PARAMS)
    e100, e200 = ema(b.c, 100, ind.pos), ema(b.c, 200, ind.pos)
    out = {}
    for x in find_signals(b, ind, S14_PARAMS, variant="sweep"):
        i, k = x.idx, x.hi_idx
        f = m.swept_fractal(b, ind, i, k)
        a = float(ind.atr[i])
        zlo, zhi = m.sweep_zone(float(b.low[i]), float(b.low[f]), a)
        lv = m3.htf_lows(b.t, b.low, int(b.t[i]))
        d2 = m.zone_distance([float(e100[i - 1]), float(e200[i - 1]), lv["day"], lv["week"]], zlo, zhi, a)
        out[i] = (k, f, m.channel_breakout(b.h, ind.pos, k, 90), m.whole_pullback(b.low, k, f, i), round(d2, 9))
    return out


def test_swept_fractal_is_the_detectors():
    o, hi, lo, c = trending(6000, 9)
    b = bars(list(zip(o, hi, lo, c, strict=True)))
    ind = indicators(b, S14_PARAMS)
    sigs = find_signals(b, ind, S14_PARAMS, variant="sweep")
    assert len(sigs) >= 5
    fr = fractal_lows(b.low)
    zf = [int(f) for f in fr if f >= 1 and np.isfinite(ind.e_slow[f - 1]) and ind.e_slow[f - 1] <= b.low[f] <= ind.e_fast[f - 1]]
    for x in sigs:
        f = m.swept_fractal(b, ind, x.idx, x.hi_idx)
        assert f == max(z for z in zf if z <= x.idx - 3)
        assert f > x.hi_idx and b.low[x.idx] < b.low[f] < x.entry and b.low[f + 1 : x.idx].min() >= b.low[f]


def test_reconstructed_signals_equal_s14_all():
    # the script keys find_signals on the oriented bars by bar index; their prices must be the passport's (s14_all),
    # so the base trade the script simulates is the passport's base trade
    o, hi, lo, c = trending(3000, 9)
    b = bars(list(zip(o, hi, lo, c, strict=True)), bar_ms=4 * H)
    live = s14_all(b, "4h")
    assert any(s.side == "long" for s in live)
    for side, bb in (("long", b), ("short", b.mirrored())):
        sg = 1.0 if side == "long" else -1.0
        mine = {x.idx: x for x in find_signals(bb, indicators(bb, S14_PARAMS), S14_PARAMS, variant="sweep")}
        for s in (s for s in live if s.side == side):
            x = mine[int(np.searchsorted(bb.t, s.signal_ts))]
            assert (x.entry, x.stop, x.target) == pytest.approx((sg * s.entry, sg * s.stop, sg * s.target))


# ---------------------------------------------------------------- no look-ahead


def test_no_lookahead_features():
    o, hi, lo, c = trending(6000, 9)
    base = _features(bars(list(zip(o, hi, lo, c, strict=True))))
    assert len(base) >= 5
    cut = sorted(base)[len(base) // 2]  # change every bar after this signal bar
    rng = np.random.default_rng(1)
    c2 = c.copy()
    c2[cut + 1 :] = c[cut + 1 :] * 0.95 + rng.normal(0, 2.0, c.size - cut - 1)
    o2 = np.r_[o[: cut + 1], c2[cut:-1]]
    hi2 = np.r_[hi[: cut + 1], np.maximum(o2, c2)[cut + 1 :] + 0.3]
    lo2 = np.r_[lo[: cut + 1], np.minimum(o2, c2)[cut + 1 :] - 0.3]
    moved = _features(bars(list(zip(o2, hi2, lo2, c2, strict=True))))
    before = {i: v for i, v in base.items() if i <= cut}
    assert before
    assert {i: v for i, v in moved.items() if i <= cut} == before
    assert {i: v for i, v in moved.items() if i > cut} != {i: v for i, v in base.items() if i > cut}  # the change bites


@pytest.mark.parametrize(("bar_ms", "i"), [(24 * H, 14), (4 * H, 14 * 6 + 5)])  # 1d bar of day 14; 4h bar 20:00
def test_no_lookahead_htf_levels_on_4h_1d(bar_ms: int, i: int):
    """PDL / PWL as verify_s14_modules._levels takes them for a 4h / 1d signal bar i on the 1h path (``ref`` = open
    of bar i): neither the sweep bar i nor anything after it enters; the closed day before the bar's day does."""
    n_days = 21
    rng = np.random.default_rng(5)
    lo = 100 + rng.uniform(0.0, 5.0, n_days * 24)

    def path(low: np.ndarray) -> Bars:
        return bars([(x + 1, x + 2, x, x + 1) for x in low])

    htf = bars([(1.0, 1.0, 1.0, 1.0)] * (n_days * 24 * H // bar_ms), bar_ms=bar_ms)
    ref = int(htf.t[i])
    day = 24 * H
    assert ref == T0 + 14 * day + (20 * H if bar_ms == 4 * H else 0)
    p0 = path(lo)
    base = m3.htf_lows(p0.t, p0.low, ref)
    in_prev_day = (p0.t >= T0 + 13 * day) & (p0.t < T0 + 14 * day)
    assert base["day"] == pytest.approx(float(lo[in_prev_day].min()))
    assert np.isfinite(base["week"])

    # the sweep bar i and everything after it: a far lower low changes neither PDL nor PWL
    lo_after = lo.copy()
    lo_after[p0.t >= ref] = 1.0
    p1 = path(lo_after)
    after = m3.htf_lows(p1.t, p1.low, ref)
    assert after["day"] == base["day"] and after["week"] == base["week"]
    assert after["day"] > 1.0

    # the open (not yet closed) current day before bar i does not enter either (4h: 00:00-20:00 of day 14)
    if bar_ms == 4 * H:
        lo_today = lo.copy()
        lo_today[(p0.t >= T0 + 14 * day) & (p0.t < ref)] = 1.0
        p2 = path(lo_today)
        assert m3.htf_lows(p2.t, p2.low, ref)["day"] == base["day"]

    # the previous closed day does enter (1d: it is bar i - 1)
    lo_prev = lo.copy()
    lo_prev[np.flatnonzero(in_prev_day)[7]] = 50.0
    p3 = path(lo_prev)
    assert m3.htf_lows(p3.t, p3.low, ref)["day"] == pytest.approx(50.0)
    if bar_ms == 24 * H:
        assert int(htf.t[i - 1]) == T0 + 13 * day


def test_no_lookahead_runner():
    # the runner's exit at time t does not depend on bars after t: change everything after its exit bar
    rng = np.random.default_rng(4)
    n = 200
    c = 100 + np.cumsum(rng.normal(0.2, 1.0, n))
    o = np.r_[100.0, c[:-1]]
    hi = np.maximum(o, c) + 0.4
    lo = np.minimum(o, c) - 0.4
    p = bars(list(zip(o, hi, lo, c, strict=True)))
    tb_t = p.t[3::4] + H  # 4h closes
    tb_c = c[3::4]
    tb_e = ema(tb_c, 21, np.arange(tb_c.size))
    base = simulate_stop_rule(p, T0, 100.0, 90.0, 103.0, 150 * H, NO_BE, fees=ZERO)
    assert base is not None and base.exit_kind == "target"
    rr, ex, kind = m.runner_after_target(p, T0, 100.0, 90.0, 150 * H, ZERO, base, tb_t, tb_c, tb_e)
    j = int(np.searchsorted(p.t, ex - H))
    assert j < n - 5
    c2, hi2, lo2 = c.copy(), hi.copy(), lo.copy()
    c2[j + 1 :] += 50.0
    hi2[j + 1 :] += 50.0
    lo2[j + 1 :] += 50.0
    o2 = np.r_[o[: j + 1], c2[j:-1]]
    q = bars(list(zip(o2, hi2, lo2, c2, strict=True)))
    tq_c = c2[3::4]
    tq_e = ema(tq_c, 21, np.arange(tq_c.size))
    bq = simulate_stop_rule(q, T0, 100.0, 90.0, 103.0, 150 * H, NO_BE, fees=ZERO)
    assert m.runner_after_target(q, T0, 100.0, 90.0, 150 * H, ZERO, bq, tb_t, tq_c, tq_e) == (pytest.approx(rr), ex, kind)
