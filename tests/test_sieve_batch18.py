"""Sieve batch 18 specs (ut_bot_alerts, hlhb_rsi_ema_adx, volty_expan_close, tsi_zero_cross, hour_2100_high_vol):
registered parameters, constructed-series signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("ut_bot_alerts", "hlhb_rsi_ema_adx", "volty_expan_close", "tsi_zero_cross", "hour_2100_high_vol")


def load(name: str):
    return sieve_specs.load(name)


def walk(n: int, seed: int, vol: float, bar_ms: int) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 4, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 4, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def make(c, bar_ms: int = DAY_MS) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    return Bars(D0 + np.arange(c.size, dtype=np.int64) * bar_ms, o, np.maximum(o, c) + 0.2, np.minimum(o, c) - 0.2, c, bar_ms)


def test_registered_parameters():
    assert load("ut_bot_alerts").PARAMS == {"key": 1.0, "atr_n": 10, "hold_bars": 20}
    assert load("hlhb_rsi_ema_adx").PARAMS == {
        "rsi_n": 10, "fast": 5, "slow": 10, "adx_n": 14, "adx_min": 25.0, "stop_atr": 2.0, "hold_bars": 20,
    }  # fmt: skip
    assert load("volty_expan_close").PARAMS == {"length": 5, "num_atrs": 0.75, "stop_atr": 2.0, "hold_bars": 5}
    assert load("tsi_zero_cross").PARAMS == {"long_n": 25, "short_n": 13, "stop_atr": 2.0, "hold_bars": 20}
    assert load("hour_2100_high_vol").PARAMS == {
        "entry_hour": 21, "hold_hours": 2, "vol_hours": 720, "median_days": 365, "min_days": 300, "stop_atr": 2.0,
    }  # fmt: skip
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
    assert load("ut_bot_alerts").TIMEFRAMES == (4,) and load("hlhb_rsi_ema_adx").TIMEFRAMES == (4,)
    assert load("hour_2100_high_vol").TIMEFRAMES == (1,) and load("tsi_zero_cross").TIMEFRAMES == (24,)


def test_ut_bot_flips_both_ways():
    spec = load("ut_bot_alerts")
    h4 = 4 * HOUR_MS
    c = np.concatenate([np.linspace(100.0, 130.0, 60), np.linspace(130.0, 90.0, 60), np.linspace(90.0, 120.0, 60)])
    b = make(c + np.tile([0.0, 1.0], 90), h4)
    sigs = spec.signals(b)
    assert {s.side for s in sigs} == {"long", "short"} and all(s.valid() and s.target is None for s in sigs)
    assert all(s.hold_ms == 20 * h4 for s in sigs)
    # the stop is the trailing line one ATR10 from the entry, so it sits close to the entry
    assert all(abs(s.entry - s.stop) / s.entry < 0.1 for s in sigs)


def test_hlhb_needs_all_three_conditions():
    spec = load("hlhb_rsi_ema_adx")
    b = walk(6000, seed=3, vol=0.02, bar_ms=4 * HOUR_MS)
    sigs = spec.signals(b)
    assert sigs and all(s.valid() and s.hold_ms == 20 * 4 * HOUR_MS for s in sigs)
    assert spec.signals(b, adx_min=101.0) == []  # ADX never above 100 -> nothing
    assert spec.signals(make(np.full(300, 100.0), 4 * HOUR_MS)) == []


def test_volty_expansion_close_both_sides():
    spec = load("volty_expan_close")
    c = np.concatenate([100.0 + np.tile([0.0, 0.1], 20), [103.0], np.full(5, 103.0), [99.0]])
    b = make(c)
    sigs = spec.signals(b)
    up = [s for s in sigs if s.t_ms == b.close_time(40)]
    dn = [s for s in sigs if s.t_ms == b.close_time(46)]
    assert len(up) == 1 and up[0].side == "long" and up[0].valid() and up[0].hold_ms == 5 * DAY_MS
    assert len(dn) == 1 and dn[0].side == "short" and dn[0].valid()
    assert spec.signals(make(np.full(60, 100.0))) == []  # no movement, no breakout


def test_tsi_zero_cross_on_a_cycle():
    spec = load("tsi_zero_cross")
    t = np.arange(400)
    b = make(200.0 + 30.0 * np.sin(t / 15.0))
    sigs = spec.signals(b)
    assert {s.side for s in sigs} == {"long", "short"} and all(s.valid() for s in sigs)
    assert spec.signals(make(np.linspace(100.0, 160.0, 400))) == []  # a straight rise never crosses zero


def test_hour_2100_only_in_high_volatility_days():
    spec = load("hour_2100_high_vol")
    rng = np.random.default_rng(5)
    n = 24 * 520
    vol = np.concatenate([np.full(24 * 400, 0.004), np.full(n - 24 * 400, 0.012)])
    c = 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol)))
    o = np.concatenate([[100.0], c[:-1]])
    b = Bars(D0 + np.arange(n, dtype=np.int64) * HOUR_MS, o, np.maximum(o, c) * 1.001, np.minimum(o, c) * 0.999, c, HOUR_MS)
    sigs = spec.signals(b)
    assert sigs and all(s.valid() and s.side == "long" and s.hold_ms == 2 * HOUR_MS for s in sigs)
    assert all((s.t_ms // HOUR_MS) % 24 == 21 for s in sigs)
    assert min(s.t_ms for s in sigs) >= D0 + 330 * DAY_MS  # 30 days for the first volatility + 300 values for the median
    late = [s for s in sigs if s.t_ms >= D0 + 410 * DAY_MS]
    assert len(late) > 0.8 * (520 - 410)  # after the volatility jump almost every day is "high"
    assert spec.signals(walk(24 * 100, seed=1, vol=0.01, bar_ms=HOUR_MS)) == []  # history too short
    assert spec.signals(walk(100, seed=1, vol=0.01, bar_ms=DAY_MS)) == []  # not hourly bars


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    if name == "hour_2100_high_vol":
        b = walk(24 * 480, seed=11, vol=0.01, bar_ms=HOUR_MS)
    elif name in ("ut_bot_alerts", "hlhb_rsi_ema_adx"):
        b = walk(4000, seed=7, vol=0.02, bar_ms=4 * HOUR_MS)
    else:
        b = walk(2500, seed=7, vol=0.03, bar_ms=DAY_MS)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
