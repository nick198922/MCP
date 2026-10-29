"""Sieve batch 13 specs (stochastic_pop, anti_setup_raschke, bollinger_squeeze_126, ema_pullback_20_50,
momentum_pinball): registered parameters, constructed-series signals, long/short symmetry and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, atr, segment_pos
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("stochastic_pop", "anti_setup_raschke", "bollinger_squeeze_126", "ema_pullback_20_50", "momentum_pinball")
DAILY = tuple(s for s in SPECS if s != "momentum_pinball")
SYMMETRIC = ("stochastic_pop", "anti_setup_raschke", "ema_pullback_20_50")
K = 200.0


def load(name: str):
    return sieve_specs.load(name)


def make(c, bar_ms: int = DAY_MS, h=None, lo=None, o=None) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]]) if o is None else np.asarray(o, dtype=float)
    h = np.maximum(o, c) if h is None else np.asarray(h, dtype=float)
    lo = np.minimum(o, c) if lo is None else np.asarray(lo, dtype=float)
    return Bars(D0 + np.arange(c.size, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def mean_reverting(n: int, seed: int, bar_ms: int = DAY_MS) -> Bars:
    """AR(1) around 100 with trending stretches; stays far from zero so K - price is a valid mirror market."""
    rng = np.random.default_rng(seed)
    c = np.empty(n)
    x = 0.0
    for i in range(n):
        x = 0.8 * x + rng.normal(0.0, 2.0)
        c[i] = 100.0 + 25.0 * np.sin(2 * np.pi * i / 90.0) + x
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.6, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.6, n))
    return Bars(D0 + np.arange(n, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def flipped(b: Bars) -> Bars:
    """The same market seen upside down inside positive prices: price -> K - price."""
    return Bars(b.t, K - b.o, K - b.low, K - b.h, K - b.c, b.bar_ms)


def test_registered_parameters():
    assert load("stochastic_pop").PARAMS == {
        "n": 14,
        "k_smooth": 3,
        "hi": 80.0,
        "lo": 20.0,
        "quiet": 5,
        "adx_n": 14,
        "adx_min": 20.0,
        "rr": 2.0,
        "skip_bars": 3,
        "hold_bars": 20,
    }
    assert load("anti_setup_raschke").PARAMS == {
        "n": 14,
        "k_smooth": 3,
        "zone": 20.0,
        "ema_n": 20,
        "slope_lag": 5,
        "atr_cap": 2.0,
        "rr": 2.0,
        "hold_bars": 8,
    }
    assert load("bollinger_squeeze_126").PARAMS == {"n": 20, "k": 2.0, "lookback": 125, "window": 10, "rr": 2.0, "hold_bars": 20}
    assert load("ema_pullback_20_50").PARAMS == {"fast": 20, "slow": 50, "slope_lag": 5, "hold_bars": 20}
    assert load("momentum_pinball").PARAMS == {"rsi_n": 3, "roc_lag": 2, "oversold": 30.0, "overbought": 70.0}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
    assert load("momentum_pinball").TIMEFRAMES == (1,)
    assert all(load(n).TIMEFRAMES == (24,) for n in DAILY)


def test_bollinger_squeeze_long_and_short_levels():
    spec = load("bollinger_squeeze_126")
    rng = np.random.default_rng(3)
    noisy = 100.0 + np.cumsum(rng.normal(0.0, 2.0, 160))
    flat = np.full(30, noisy[-1])
    for sign, side in ((1.0, "long"), (-1.0, "short")):
        c = np.concatenate([noisy, flat, [noisy[-1] + sign * 12.0]])
        b = make(c, h=c + 0.01, lo=c - 0.01)
        j = len(c) - 1
        sigs = [s for s in spec.signals(b) if s.t_ms == b.close_time(j)]
        assert len(sigs) == 1 and sigs[0].side == side and sigs[0].valid()
        w = c[j - 19 : j + 1]
        mid = float(w.mean())
        assert sigs[0].entry == c[j] and sigs[0].stop == pytest.approx(mid)
        assert sigs[0].target == pytest.approx(c[j] + 2.0 * (c[j] - mid))
        assert sigs[0].hold_ms == 20 * DAY_MS


def test_bollinger_squeeze_needs_a_lower_bandwidth_than_126_bars():
    spec = load("bollinger_squeeze_126")
    rng = np.random.default_rng(4)
    c = 100.0 + np.cumsum(rng.normal(0.0, 2.0, 150))  # constantly wide bands: no bar is the lowest of 126
    c = c + 20.0 * np.sin(np.arange(150) / 2.0)
    b = make(c, h=c + 0.01, lo=c - 0.01)
    sigs = spec.signals(b)
    assert all(s.valid() for s in sigs)
    assert len(spec.signals(make(c[:140], h=c[:140] + 0.01, lo=c[:140] - 0.01))) <= len(sigs)


def hourly_days(daily_close, spike_day: int, spike: float) -> Bars:
    """Hourly bars: each day's hours sit at that day's close; hour 5 of ``spike_day`` closes ``spike`` above the prior day's close."""
    c = []
    for d, dc in enumerate(daily_close):
        row = [float(dc)] * 24
        if d == spike_day:
            row[5] = float(daily_close[d - 1]) + spike
        c.extend(row)
    c = np.asarray(c)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + 0.5
    lo = np.minimum(o, c) - 0.5
    return make(c, bar_ms=HOUR_MS, h=h, lo=lo, o=o)


def test_momentum_pinball_long_after_oversold_day():
    spec = load("momentum_pinball")
    days = 200.0 - np.arange(16.0) ** 2  # x(d) = c(d) - c(d-2) falls faster every day: LBR/RSI(3) = 0
    day_bars = hourly_days(days, spike_day=12, spike=5.0)
    from tradedesk.analytics.course_s14_ema import resample

    rsi = spec.lbr_rsi(resample(day_bars, 24), 3, 2)
    assert np.isnan(rsi[:5]).all() and np.allclose(rsi[5:], 0.0)
    sigs = spec.signals(day_bars)
    assert len(sigs) == 1
    s = sigs[0]
    i = 12 * 24 + 5
    assert s.side == "long" and s.t_ms == day_bars.close_time(i) and s.entry == day_bars.c[i]
    assert s.stop == day_bars.low[12 * 24] and s.target is None and s.valid()
    assert s.t_ms + s.hold_ms == D0 + 14 * DAY_MS  # closed at 00:00 UTC of day D + 2
    # a rising run gives RSI 100 (> 70): no long, and no short either without a close below the first hour's low
    up = 100.0 + np.arange(16.0) ** 2
    assert spec.signals(hourly_days(up, spike_day=12, spike=5.0)) == []
    assert spec.signals(make(np.arange(10.0) + 1)) == []  # not 1h bars


@pytest.mark.parametrize("name", SYMMETRIC)
def test_long_short_symmetry(name):
    spec = load(name)
    b = mean_reverting(2500, seed=11)
    longs = {s.t_ms: s for s in spec.signals(b) if s.side == "long"}
    shorts = {s.t_ms: s for s in spec.signals(flipped(b)) if s.side == "short"}
    assert len(longs) >= 1 and longs.keys() == shorts.keys()
    for t, s in longs.items():
        m = shorts[t]
        assert m.entry == pytest.approx(K - s.entry) and m.stop == pytest.approx(K - s.stop)
        assert (m.target is None) == (s.target is None)
        if s.target is not None:
            assert m.target == pytest.approx(K - s.target)


def test_stochastic_pop_levels_on_the_signal_bar():
    spec = load("stochastic_pop")
    b = mean_reverting(2500, seed=11)
    sigs = spec.signals(b)
    assert {s.side for s in sigs} == {"long", "short"}
    idx = {b.close_time(i): i for i in range(len(b))}
    for s in sigs:
        i = idx[s.t_ms]
        assert s.entry == b.c[i] and s.hold_ms == 20 * DAY_MS and s.valid()
        if s.side == "long":
            assert s.stop == b.low[i] and s.target == pytest.approx(s.entry + 2.0 * (s.entry - s.stop))
        else:
            assert s.stop == b.h[i] and s.target == pytest.approx(s.entry - 2.0 * (s.stop - s.entry))
    k = spec.slow_k(
        make(np.arange(1.0, 40.0), h=np.arange(1.0, 40.0) + 0.5, lo=np.arange(1.0, 40.0) - 0.5),
        14,
        3,
        segment_pos(make(np.arange(1.0, 40.0)).t),
    )
    assert np.isnan(k[:15]).all() and k[-1] > 90.0


def test_anti_setup_risk_cap_and_levels():
    spec = load("anti_setup_raschke")
    b = mean_reverting(2500, seed=11)
    a = atr(b, 14, segment_pos(b.t))
    idx = {b.close_time(i): i for i in range(len(b))}
    sigs = spec.signals(b)
    assert {s.side for s in sigs} == {"long", "short"}
    for s in sigs:
        i = idx[s.t_ms]
        assert s.hold_ms == 8 * DAY_MS and s.valid() and abs(s.entry - s.stop) <= 2.0 * a[i] + 1e-9
        assert abs(s.target - s.entry) == pytest.approx(2.0 * abs(s.entry - s.stop))
        assert (s.stop <= b.low[i]) if s.side == "long" else (s.stop >= b.h[i])


def test_ema_pullback_stop_is_swing_extreme_above_slow_ema():
    spec = load("ema_pullback_20_50")
    b = mean_reverting(2500, seed=11)
    idx = {b.close_time(i): i for i in range(len(b))}
    sigs = spec.signals(b)
    assert {s.side for s in sigs} == {"long", "short"}
    for s in sigs:
        i = idx[s.t_ms]
        assert s.target is None and s.hold_ms == 20 * DAY_MS and s.valid()
        assert (s.stop <= b.low[i]) if s.side == "long" else (s.stop >= b.h[i])
        # the stop is a real low (high) of one of the last 60 bars, not an invented level
        near = b.low[i - 59 : i + 1] if s.side == "long" else b.h[i - 59 : i + 1]
        assert s.stop in near


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    if name == "momentum_pinball":
        rng = np.random.default_rng(5)
        c = 100 * np.exp(np.cumsum(rng.normal(0.0, 0.012, 24 * 150)))
        o = np.concatenate([[100.0], c[:-1]])
        h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.004, c.size)))
        lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.004, c.size)))
        b = Bars(D0 + np.arange(c.size, dtype=np.int64) * HOUR_MS, o, h, lo, c, HOUR_MS)
    elif name == "bollinger_squeeze_126":
        rng = np.random.default_rng(6)
        vol = np.where((np.arange(3000) // 150) % 2 == 0, 0.04, 0.004)  # quiet stretches follow noisy ones
        c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol)))
        o = np.concatenate([[100.0], c[:-1]])
        h = np.maximum(o, c) * 1.002
        lo = np.minimum(o, c) * 0.998
        b = Bars(D0 + np.arange(c.size, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)
    else:
        b = mean_reverting(2500, seed=11)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
