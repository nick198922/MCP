"""Sieve batch 5 specs (rsi14_failure_swing, stochastic_full_cross, parabolic_sar_flip, inside_bar_breakout,
london_breakout_asia_range): registered parameters, constructed signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, segment_pos
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("rsi14_failure_swing", "stochastic_full_cross", "parabolic_sar_flip", "inside_bar_breakout", "london_breakout_asia_range")


def load(name: str):
    return sieve_specs.load(name)


def random_walk(n: int, seed: int, bar_ms: int, vol: float) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def test_registered_parameters():
    assert load("rsi14_failure_swing").PARAMS == {"n": 14, "lo": 30.0, "stop_atr": 2.0, "hold_bars": 20}
    assert load("stochastic_full_cross").PARAMS == {"n": 14, "k_smooth": 3, "d_smooth": 3, "zone": 20.0, "stop_atr": 2.0, "hold_bars": 20}
    assert load("parabolic_sar_flip").PARAMS == {"af0": 0.02, "af_step": 0.02, "af_max": 0.20, "hold_bars": 20, "warmup": 50}
    assert load("inside_bar_breakout").PARAMS == {"hold_bars": 10}
    assert load("london_breakout_asia_range").PARAMS == {"range_end": 7, "entry_end": 10, "exit_hour": 12, "min_atr": 0.25, "max_atr": 0.80}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
        assert spec.TIMEFRAMES == ((1,) if name == "london_breakout_asia_range" else (24,))


def test_rsi_wilder_matches_hand_value():
    from tradedesk.analytics.sieve_specs.rsi14_failure_swing import rsi_wilder

    c = np.array([10.0, 11.0, 10.0, 12.0, 11.0])  # n=2: gains 1,0 losses 0,1 -> avg 0.5/0.5 -> RSI 50
    r = rsi_wilder(c, 2, segment_pos(D0 + np.arange(5, dtype=np.int64) * DAY_MS))
    assert np.isnan(r[1]) and r[2] == pytest.approx(50.0)
    # next: gain 2 -> ag (0.5 + 2) / 2 = 1.25, al 0.25 -> RSI 100 - 100 / 6
    assert r[3] == pytest.approx(100 - 100 / 6)


def test_inside_bar_breakout_long_and_short():
    spec = load("inside_bar_breakout")
    o = np.array([10.0, 10.0, 10.0, 10.0])
    h = np.array([11.0, 12.0, 11.5, 13.0])
    lo = np.array([9.0, 8.0, 9.0, 10.0])
    c = np.array([10.0, 10.0, 10.0, 12.5])
    b = Bars(D0 + np.arange(4, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)
    (s,) = spec.signals(b, **spec.PARAMS)  # mother bar 1 (8..12), inside bar 2, close 12.5 > 12
    assert (s.side, s.stop, s.t_ms) == ("long", 8.0, D0 + 4 * DAY_MS)
    c2 = c.copy()
    c2[3] = 7.5
    b2 = Bars(b.t, o, h, lo, c2, DAY_MS)
    (s2,) = spec.signals(b2, **spec.PARAMS)
    assert (s2.side, s2.stop) == ("short", 12.0)
    c3 = c.copy()
    c3[3] = 11.0  # no breakout
    assert spec.signals(Bars(b.t, o, h, lo, c3, DAY_MS), **spec.PARAMS) == []


def test_london_breakout_levels():
    spec = load("london_breakout_asia_range")
    n_days = 20
    m = 24 * n_days
    t = D0 + np.arange(m, dtype=np.int64) * HOUR_MS
    c = np.full(m, 100.0) + np.tile(np.sin(np.arange(24)), n_days)
    h, lo, o = c + 1.0, c - 1.0, c.copy()
    i = 24 * 19 + 8  # bar opening 08:00 of the last day closes far above the Asian high
    c[i] = 140.0
    h[i] = 141.0
    b = Bars(t, o, h, lo, c, HOUR_MS)
    sigs = spec.signals(b, **spec.PARAMS)
    day = [s for s in sigs if s.t_ms == int(t[i]) + HOUR_MS]
    if day:  # the ATR filter may veto the day; when it passes the geometry must hold
        (s,) = day
        assert s.side == "long" and s.entry == 140.0 and s.target == pytest.approx(140.0 + (140.0 - s.stop))
        assert s.t_ms + s.hold_ms == D0 + 19 * DAY_MS + 12 * HOUR_MS
    assert all(s.t_ms % DAY_MS in {h_ * HOUR_MS for h_ in (8, 9, 10)} for s in sigs)


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    if name == "london_breakout_asia_range":
        b = random_walk(24 * 200, seed=3, bar_ms=HOUR_MS, vol=0.01)
    else:
        b = random_walk(1500, seed=5, bar_ms=DAY_MS, vol=0.035)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 3 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
