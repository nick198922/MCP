import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import (
    DAY_MS,
    HOUR_MS,
    MARKET_SIDE,
    Bars,
    Params,
    align_daily,
    atr,
    ema,
    find_signals,
    forward_min_low,
    fractal_lows,
    h5_events,
    indicators,
    matched_baseline,
    placebo_signal,
    resample,
    segment_pos,
    simulate,
    trend_episodes,
)

H4 = 4 * HOUR_MS
T0 = 1_704_067_200_000  # 2024-01-01 00:00 UTC


def bars(closes, *, step=H4, spread=0.5, lows=None, highs=None, t0=T0) -> Bars:
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + spread if highs is None else np.asarray(highs, dtype=float)
    lo = np.minimum(o, c) - spread if lows is None else np.asarray(lows, dtype=float)
    t = t0 + np.arange(c.size, dtype=np.int64) * step
    return Bars(t, o, h, lo, c, step)


def uptrend_with_touch(n_up=150, after=30):
    """Linear uptrend +1 per bar, then one bar dips into the EMA 13/34 zone and closes above EMA 34, then the trend
    resumes. Returns (bars, index of the dip bar)."""
    c = list(100.0 + np.arange(n_up))
    i = n_up
    c.append(c[-1] - 7.0)  # dip bar close: EMA13 lags ~6, EMA34 ~16.5 below the last close
    c.extend(c[-1] + 1.0 + np.arange(after))
    b = bars(c)
    lo = b.low.copy()
    lo[i] = c[i - 1] - 8.0  # low under EMA13 of the previous bar
    return Bars(b.t, b.o, b.h, lo, b.c, b.bar_ms), i


# ---------------------------------------------------------------- indicators


def test_ema_seed_and_recursion():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    out = ema(x, 2, segment_pos(np.arange(4) * H4))
    assert np.isnan(out[0])
    assert out[1] == pytest.approx(1.5)
    assert out[2] == pytest.approx(3 * 2 / 3 + 1.5 / 3)
    assert out[3] == pytest.approx(4 * 2 / 3 + out[2] / 3)


def test_ema_lag_on_linear_trend():
    x = 100.0 + np.arange(400, dtype=float)
    out = ema(x, 21, segment_pos(np.arange(400) * H4))
    assert x[-1] - out[-1] == pytest.approx(10.0, abs=1e-6)  # steady-state lag (n-1)/2 per unit slope


def test_atr_constant_range():
    b = bars([100.0] * 40, spread=1.0)  # every bar 99..101, flat closes -> TR = 2
    a = atr(b, 14, segment_pos(b.t))
    assert np.isnan(a[13]) and a[14] == pytest.approx(2.0) and a[-1] == pytest.approx(2.0)


def test_segment_restarts_after_long_gap():
    t = np.array([0, 1, 2, 3 + 11, 4 + 11, 5 + 11], dtype=np.int64) * H4  # opens 48 h apart -> not a break
    assert segment_pos(t).tolist() == [0, 1, 2, 3, 4, 5]
    t2 = np.array([0, 1, 2, 3 + 12, 4 + 12], dtype=np.int64) * H4  # opens 52 h apart -> new segment
    assert segment_pos(t2).tolist() == [0, 1, 2, 0, 1]


def test_resample_1h_to_4h_drops_incomplete_buckets():
    c = np.arange(1.0, 13.0)  # 12 hourly bars = three 4h buckets
    b = bars(c, step=HOUR_MS)
    keep = np.ones(12, dtype=bool)
    keep[9] = False  # third bucket misses an hour
    b = Bars(b.t[keep], b.o[keep], b.h[keep], b.low[keep], b.c[keep], HOUR_MS)
    r = resample(b, 4)
    assert len(r) == 2 and r.bar_ms == H4
    assert r.t.tolist() == [T0, T0 + H4]
    assert r.o[0] == b.o[0] and r.c[0] == 4.0 and r.h[0] == pytest.approx(4.5) and r.low[0] == pytest.approx(0.5)
    assert r.c[1] == 8.0


def test_fractal_lows():
    low = np.array([5, 4, 3, 4, 5, 2, 6, 7, 1.0])
    assert fractal_lows(low).tolist() == [2, 5]  # the last bar (1.0) has no right side yet


# ---------------------------------------------------------------- signals


def test_touch_signal_on_constructed_uptrend():
    b, i = uptrend_with_touch()
    p = Params()
    ind = indicators(b, p)
    sig = find_signals(b, ind, p, "touch")
    assert [s.idx for s in sig] == [i]
    s = sig[0]
    assert s.entry == b.c[i]
    assert s.stop == pytest.approx(b.low[i] - 0.5 * ind.atr[i])
    assert s.high_ref == pytest.approx(b.h[i - 1])  # impulse high = last bar before the dip
    expected_target = s.high_ref if s.high_ref - s.entry >= 1.5 * s.risk else s.entry + 2 * s.risk
    assert s.target == pytest.approx(expected_target)
    # the same touch without the trend rule is found too (superset)
    assert i in [x.idx for x in find_signals(b, ind, p, "touch_no_trend")]


def test_no_signal_without_impulse():
    b, i = uptrend_with_touch()
    p = Params(impulse_atr=100.0)
    assert find_signals(b, indicators(b, p), p, "touch") == []


def test_close_below_slow_ema_is_not_a_signal():
    b, i = uptrend_with_touch()
    c = b.c.copy()
    c[i] = c[i - 1] - 30.0  # closes far under EMA 34
    b2 = Bars(b.t, b.o, b.h, np.minimum(b.low, c - 0.5), c, b.bar_ms)
    p = Params()
    assert i not in [s.idx for s in find_signals(b2, indicators(b2, p), p, "touch")]


def test_short_is_the_mirror_of_long():
    b, i = uptrend_with_touch()
    down = Bars(b.t, 1000 - b.o, 1000 - b.low, 1000 - b.h, 1000 - b.c, b.bar_ms)  # a downtrend with a rally bar
    p = Params()
    m = down.mirrored()
    sig = find_signals(m, indicators(m, p), p, "touch")
    assert [s.idx for s in sig] == [i]
    assert -sig[0].stop == pytest.approx(1000 - find_signals(b, indicators(b, p), p, "touch")[0].stop)


def test_sweep_variant_needs_a_fractal_in_the_zone():
    # uptrend, a 3-bar pullback that forms a fractal low inside the zone, a bounce, then a bar that sweeps that low
    c = list(100.0 + np.arange(150))
    top = c[-1]
    c += [top - 4.0, top - 6.0, top - 5.0, top - 4.5, top - 4.0, top - 5.5]
    lows = list(np.array(c) - 0.5)
    lows[0] = c[0] - 0.5
    b0 = bars(c)
    lo = b0.low.copy()
    f = 151
    p = Params()
    ind0 = indicators(b0, p)
    lo[f] = float(ind0.e_fast[f - 1]) - 0.5  # fractal low inside [EMA34, EMA13] of the previous bar
    for k in (f - 2, f - 1, f + 1, f + 2):
        lo[k] = max(lo[k], lo[f] + 0.3)
    sweep = len(c) - 1
    lo[sweep] = lo[f] - 0.4
    for k in range(f + 3, sweep):
        lo[k] = max(lo[k], lo[f] + 0.3)
    b = Bars(b0.t, b0.o, np.maximum(b0.h, lo), lo, b0.c, b0.bar_ms)
    ind = indicators(b, p)
    assert ind.e_slow[f - 1] <= b.low[f] <= ind.e_fast[f - 1]
    sig = find_signals(b, ind, p, "sweep")
    assert [s.idx for s in sig] == [sweep]
    assert sig[0].stop == pytest.approx(b.low[sweep] - 0.5 * ind.atr[sweep])


def random_trending(n=3000, seed=1):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.01, n) + 0.002 * np.sin(np.arange(n) / 150)
    c = 100 * np.exp(np.cumsum(r))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.006, n))
    lo = np.minimum(o, c) * (1 - rng.uniform(0, 0.006, n))
    return Bars(T0 + np.arange(n, dtype=np.int64) * H4, o, h, lo, c, H4)


@pytest.mark.parametrize("variant", ["touch", "touch_no_trend", "sweep"])
def test_signals_before_t_ignore_data_after_t(variant):
    b = random_trending()
    p = Params()
    cut = 2000
    full = find_signals(b, indicators(b, p), p, variant)
    rng = np.random.default_rng(7)
    noise = rng.uniform(0.5, 1.5, len(b) - cut)
    o, h, lo, c = (x.copy() for x in (b.o, b.h, b.low, b.c))
    c[cut:] *= noise
    o[cut:] *= noise
    h[cut:] = np.maximum(h[cut:] * noise, np.maximum(o[cut:], c[cut:]))
    lo[cut:] = np.minimum(lo[cut:] * noise, np.minimum(o[cut:], c[cut:]))
    changed = Bars(b.t, o, h, lo, c, b.bar_ms)
    alt = find_signals(changed, indicators(changed, p), p, variant)
    trunc = b.slice(0, cut)
    short = find_signals(trunc, indicators(trunc, p), p, variant)
    before = [s for s in full if s.idx < cut]
    assert before, "the synthetic series should give signals before the cut"
    assert [s for s in alt if s.idx < cut] == before
    assert short == before


def test_indicators_prefix_is_causal():
    b = random_trending(800)
    p = Params()
    full, part = indicators(b, p), indicators(b.slice(0, 500), p)
    for name in ("e_fast", "e_mid", "e_slow", "atr"):
        np.testing.assert_array_equal(getattr(full, name)[:500], getattr(part, name))
    np.testing.assert_array_equal(full.above_run[:500], part.above_run)
    np.testing.assert_array_equal(full.hi_idx[:500], part.hi_idx)


def test_trend_episodes_and_placebo():
    b, i = uptrend_with_touch()
    p = Params()
    ind = indicators(b, p)
    ep = trend_episodes(ind, p)
    assert ep[i] >= 0 and ep[i - 1] == ep[i]
    s = placebo_signal(b, ind, p, i - 5)
    assert s is not None and s.entry == b.c[i - 5] and s.stop < s.entry < s.target


# ---------------------------------------------------------------- trade path


def path(rows, start=T0):
    a = np.asarray(rows, dtype=float)
    return Bars(start + np.arange(len(rows), dtype=np.int64) * HOUR_MS, a[:, 0], a[:, 1], a[:, 2], a[:, 3], HOUR_MS)


def test_simulate_target_and_costs():
    p = path([(100, 100.5, 99.5, 100.2), (100.2, 102.1, 100.0, 101.8)])
    t = simulate(p, T0, 100.0, 99.0, 102.0, 7 * DAY_MS)
    assert t.exit_kind == "target" and t.r_gross == pytest.approx(2.0)
    assert t.cost_r == pytest.approx(MARKET_SIDE * (100 + 102) / 1.0)
    assert t.r_net == pytest.approx(2.0 - 0.1414)


def test_simulate_stop_wins_when_both_touched_and_gap():
    t = simulate(path([(100, 102.5, 98.5, 101)]), T0, 100.0, 99.0, 102.0, DAY_MS)
    assert t.exit_kind == "stop" and t.both_touched and t.r_gross == pytest.approx(-1.0)
    g = simulate(path([(100, 100.2, 99.6, 100), (98.0, 98.5, 97.5, 98.2)]), T0, 100.0, 99.0, 102.0, DAY_MS)
    assert g.exit_kind == "stop_gap" and g.r_gross == pytest.approx(-2.0)


def test_simulate_time_exit_and_data_end():
    rows = [(100, 100.4, 99.6, 100.1)] * 30
    t = simulate(path(rows), T0, 100.0, 99.0, 102.0, 24 * HOUR_MS)
    assert t.exit_kind == "time" and t.exit_ms == T0 + 24 * HOUR_MS and t.r_gross == pytest.approx(0.1)
    assert t.mfe_r == pytest.approx(0.4)
    d = simulate(path(rows[:10]), T0, 100.0, 99.0, 102.0, 24 * HOUR_MS)
    assert d.exit_kind == "data_end"


def test_simulate_stops_at_a_long_hole():
    # flat bars, then a 3-day hole after which the ticker trades at another price level (reused symbol)
    rows = [(100, 100.4, 99.6, 100.1)] * 5 + [(500, 600, 400, 550)] * 5
    p = path(rows)
    t = np.concatenate([p.t[:5], p.t[5:] + 72 * HOUR_MS])
    p = Bars(t, p.o, p.h, p.low, p.c, HOUR_MS)
    tr = simulate(p, T0, 100.0, 99.0, 102.0, 7 * DAY_MS)
    assert tr.exit_kind == "data_end" and tr.exit == pytest.approx(100.1) and tr.exit_ms == T0 + 5 * HOUR_MS
    assert simulate(p, T0 + 5 * HOUR_MS, 100.0, 99.0, 102.0, 7 * DAY_MS) is None  # next bar opens > 48 h later
    assert forward_min_low(p, 10 * HOUR_MS)[0] == pytest.approx(99.6)  # window ends before the hole
    assert np.isnan(forward_min_low(p, 80 * HOUR_MS)[0])  # window spans the hole


def test_simulate_mirrored_short():
    # short at 100, stop 101, target 98; the price falls to 97.9
    p = path([(100, 100.3, 99.0, 99.2), (99.2, 99.4, 97.9, 98.1)])
    m = p.mirrored()
    t = simulate(m, T0, -100.0, -101.0, -98.0, DAY_MS)
    assert t.exit_kind == "target" and t.r_gross == pytest.approx(2.0)
    assert t.cost_r == pytest.approx(MARKET_SIDE * (100 + 98))


# ---------------------------------------------------------------- H5


def test_h5_events_after_long_run():
    c = list(100.0 + np.arange(200)) + [250.0]  # long run above EMA21, then a close far below it
    b = bars(c)
    p = Params()
    ind = indicators(b, p)
    assert h5_events(b, ind, 120).tolist() == [200]
    assert h5_events(b, ind, 500).tolist() == []


def test_align_daily_uses_closed_days_only():
    h4 = bars([1.0] * 12)  # two days of 4h bars
    d = Bars(np.array([T0, T0 + DAY_MS], dtype=np.int64), *(np.array([1.0, 2.0]),) * 4, DAY_MS)
    v = align_daily(h4, d, np.array([10.0, 20.0]))
    assert np.isnan(v[:5]).all()  # day 1 closes with the 6th 4h bar
    assert v[5] == 10.0 and v[6] == 10.0 and v[11] == 20.0


def test_forward_min_low():
    b = bars([5.0, 4.0, 3.0, 6.0, 7.0], spread=0.0, lows=[5, 4, 3, 6, 7])
    f = forward_min_low(b, 2 * H4)
    assert f[0] == 3.0 and f[1] == 3.0 and f[2] == 6.0 and np.isnan(f[3]) and np.isnan(f[4])


def test_matched_baseline_weights_by_event_bins():
    ev_d = [0.1, 0.1, 0.5, 0.5]
    ev_h = [1, 1, 0, 1]
    base_d = np.array([0.1, 0.1, 0.1, 0.1, 0.5, 0.5])
    base_h = np.array([1, 0, 0, 0, 1, 0], dtype=float)
    r = matched_baseline(ev_d, ev_h, base_d, base_h, bins=2)
    assert r["event_rate"] == pytest.approx(0.75)
    assert r["base_rate"] == pytest.approx((0.25 + 0.25 + 0.5 + 0.5) / 4)
