"""Sieve batch 16 specs (gann_hilo_activator, pretty_good_oscillator, brock_vma_1_50_band, dual_thrust): registered
parameters, constructed-series signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("gann_hilo_activator", "pretty_good_oscillator", "brock_vma_1_50_band", "dual_thrust")


def load(name: str):
    return sieve_specs.load(name)


def make(c, bar_ms: int = DAY_MS, h=None, lo=None) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + 0.2 if h is None else np.asarray(h, dtype=float)
    lo = np.minimum(o, c) - 0.2 if lo is None else np.asarray(lo, dtype=float)
    return Bars(D0 + np.arange(c.size, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def walk(n: int, seed: int, bar_ms: int) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.03 if bar_ms == DAY_MS else 0.008, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.004, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.004, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def test_registered_parameters():
    assert load("gann_hilo_activator").PARAMS == {"length": 3, "hold_bars": 20}
    assert load("pretty_good_oscillator").PARAMS == {"n": 89, "level": 3.0, "stop_atr": 2.0, "hold_bars": 20}
    assert load("brock_vma_1_50_band").PARAMS == {"long_n": 50, "band": 0.01, "stop_atr": 2.0, "hold_bars": 10}
    assert load("dual_thrust").PARAMS == {"n_days": 4, "k1": 0.5, "k2": 0.5}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
    assert load("dual_thrust").TIMEFRAMES == (1,)
    assert all(load(n).TIMEFRAMES == (24,) for n in SPECS if n != "dual_thrust")


def test_gann_hilo_flip_uses_previous_bar_lines():
    spec = load("gann_hilo_activator")
    c = np.concatenate([np.linspace(120.0, 100.0, 15), np.linspace(100.0, 125.0, 15)])
    b = make(c)
    sigs = spec.signals(b)
    longs = [s for s in sigs if s.side == "long"]
    assert longs and all(s.valid() and s.target is None and s.hold_ms == 20 * DAY_MS for s in sigs)
    s = longs[0]
    i = [k for k in range(len(b)) if b.close_time(k) == s.t_ms][0]
    assert s.entry == b.c[i] and s.stop == pytest.approx(b.low[i - 3 : i].mean()) and b.c[i] > b.h[i - 3 : i].mean()


def test_pgo_long_after_a_surge_and_short_mirror():
    spec = load("pretty_good_oscillator")
    rng = np.random.default_rng(3)
    flat = 100.0 + rng.normal(0, 0.5, 120)
    up = make(np.concatenate([flat, [105.0, 112.0, 118.0]]))
    sl = [s for s in spec.signals(up) if s.side == "long"]
    assert len(sl) == 1 and sl[0].valid() and sl[0].stop < sl[0].entry
    dn = make(np.concatenate([flat, [95.0, 88.0, 82.0]]))
    ss = [s for s in spec.signals(dn) if s.side == "short"]
    assert len(ss) == 1 and ss[0].valid() and ss[0].stop > ss[0].entry


def test_brock_band_needs_one_percent():
    spec = load("brock_vma_1_50_band")
    base = np.full(60, 100.0)
    near = make(np.concatenate([base, [100.9]]))  # inside the 1 % band
    assert spec.signals(near) == []
    out = make(np.concatenate([base, [102.0]]))
    s = [x for x in spec.signals(out) if x.t_ms == out.close_time(60)]
    assert len(s) == 1 and s[0].side == "long" and s[0].hold_ms == 10 * DAY_MS and s[0].valid()
    dn = make(np.concatenate([base, [97.0]]))
    s2 = [x for x in spec.signals(dn) if x.t_ms == dn.close_time(60)]
    assert len(s2) == 1 and s2[0].side == "short" and s2[0].valid()


def test_dual_thrust_levels():
    spec = load("dual_thrust")
    n = 24 * 6
    c = np.full(n, 100.0)
    h, lo = c + 1.0, c - 1.0  # every day: HH 101, LL 99, HC 100, LC 100 -> range = max(1, 1) = 1
    # day 5 (index 4*24 .. ): open 100, upper 100.5; the 3rd hour closes at 101
    c[4 * 24 + 2] = 101.0
    h[4 * 24 + 2] = 101.5
    b = Bars(D0 + np.arange(n, dtype=np.int64) * HOUR_MS, np.concatenate([[100.0], c[:-1]]), h, lo, c, HOUR_MS)
    sigs = spec.signals(b)
    s = [x for x in sigs if x.t_ms == b.close_time(4 * 24 + 2)]
    assert len(s) == 1 and s[0].side == "long" and s[0].entry == 101.0 and s[0].stop == pytest.approx(99.5)
    assert s[0].hold_ms == 24 * HOUR_MS - 3 * HOUR_MS and s[0].valid()
    assert spec.signals(make(c[:100], bar_ms=DAY_MS)) == []  # not 1h bars
    # an incomplete previous day gives no levels
    cut = np.concatenate([np.arange(0, 3 * 24 + 5), np.arange(4 * 24, n)])
    b2 = Bars(b.t[cut], b.o[cut], b.h[cut], b.low[cut], b.c[cut], HOUR_MS)
    assert [x for x in spec.signals(b2) if x.t_ms == b.close_time(4 * 24 + 2)] == []


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    bar_ms = HOUR_MS if name == "dual_thrust" else DAY_MS
    b = walk(24 * 300 if bar_ms == HOUR_MS else 2500, seed=7, bar_ms=bar_ms)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
