"""Sieve batch 6 specs (aroon_trend_start, cci_100_break, vortex_cross, williams_r_scan, coppock_zero_cross):
registered parameters, hand-checked indicator values and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, segment_pos
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("aroon_trend_start", "cci_100_break", "vortex_cross", "williams_r_scan", "coppock_zero_cross")


def load(name: str):
    return sieve_specs.load(name)


def random_walk(n: int, seed: int, vol: float) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)


def test_registered_parameters():
    assert load("aroon_trend_start").PARAMS == {"n": 25, "level": 50.0, "stop_atr": 2.0, "hold_bars": 20}
    assert load("cci_100_break").PARAMS == {"n": 20, "level": 100.0, "stop_atr": 2.0, "hold_bars": 20}
    assert load("vortex_cross").PARAMS == {"n": 14, "stop_atr": 2.0, "hold_bars": 20}
    assert load("williams_r_scan").PARAMS["oversold"] == -80.0 and load("williams_r_scan").PARAMS["sma"] == 200
    assert load("coppock_zero_cross").PARAMS == {"roc_a": 14, "roc_b": 11, "wma": 10, "stop_atr": 2.0, "hold_bars": 20}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy" and spec.TIMEFRAMES == (24,)


def test_cci_hand_value():
    from tradedesk.analytics.sieve_specs.cci_100_break import cci

    c = np.array([1.0, 2.0, 3.0, 4.0])
    b = Bars(D0 + np.arange(4, dtype=np.int64) * DAY_MS, c, c, c, c, DAY_MS)  # TP = close
    x = cci(b, segment_pos(b.t), 4)
    # mean 2.5, mean deviation (1.5 + 0.5 + 0.5 + 1.5) / 4 = 1, CCI = 1.5 / 0.015 = 100
    assert np.isnan(x[2]) and x[3] == pytest.approx(100.0)


def test_coppock_hand_value():
    from tradedesk.analytics.sieve_specs.coppock_zero_cross import coppock

    c = np.array([100.0, 110.0, 121.0])
    k = coppock(c, segment_pos(D0 + np.arange(3, dtype=np.int64) * DAY_MS), 1, 1, 2)
    # s = 2 x ROC1: bar1 20, bar2 20 -> WMA(1, 2) = 20
    assert np.isnan(k[1]) and k[2] == pytest.approx(20.0)


def test_vortex_hand_value():
    from tradedesk.analytics.sieve_specs.vortex_cross import vortex

    h = np.array([10.0, 12.0, 13.0])
    lo = np.array([8.0, 9.0, 11.0])
    c = np.array([9.0, 11.0, 12.0])
    b = Bars(D0 + np.arange(3, dtype=np.int64) * DAY_MS, c, h, lo, c, DAY_MS)
    vp, vm = vortex(b, segment_pos(b.t), 2)
    # +VM: |12-8| + |13-9| = 8; -VM: |9-10| + |11-12| = 2; TR: max(3, 3, 0)=3 and max(2, 2, 0)=2 -> 5
    assert vp[2] == pytest.approx(8 / 5) and vm[2] == pytest.approx(2 / 5)


def test_aroon_fires_on_new_trend():
    spec = load("aroon_trend_start")
    c = np.concatenate([np.full(40, 100.0), 100.0 + np.arange(1, 41)])
    b = Bars(D0 + np.arange(80, dtype=np.int64) * DAY_MS, c, c + 0.5, c - 0.5, c, DAY_MS)
    sigs = spec.signals(b, **spec.PARAMS)
    assert any(s.side == "long" for s in sigs) and not any(s.side == "short" and s.t_ms > D0 + 40 * DAY_MS for s in sigs)


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    b = random_walk(2500 if name == "williams_r_scan" else 1500, seed=5, vol=0.035)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 3 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
