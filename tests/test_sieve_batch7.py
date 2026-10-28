"""Sieve batch 7 specs (smash_day_reversal, mayer_multiple_fade, td_setup9, elder_impulse_green, halloween_nov_apr):
registered parameters, constructed-series signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, ema, segment_pos
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("smash_day_reversal", "mayer_multiple_fade", "td_setup9", "elder_impulse_green", "halloween_nov_apr")


def load(name: str):
    return sieve_specs.load(name)


def daily(c, h=None, lo=None, t0: int = D0) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) if h is None else np.asarray(h, dtype=float)
    lo = np.minimum(o, c) if lo is None else np.asarray(lo, dtype=float)
    return Bars(t0 + np.arange(c.size, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)


def random_walk(n: int, seed: int, vol: float) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)


def test_registered_parameters():
    assert load("smash_day_reversal").PARAMS == {"lookback": 8, "hold_bars": 20}
    assert load("mayer_multiple_fade").PARAMS == {"n": 200, "high": 2.4, "low": 0.8, "stop_atr": 2.0, "hold_bars": 20}
    assert load("td_setup9").PARAMS == {"stop_atr": 2.0, "hold_bars": 20}
    assert load("elder_impulse_green").PARAMS == {"ema_n": 13, "trend_n": 65, "stop_atr": 2.0, "hold_bars": 20}
    assert load("halloween_nov_apr").PARAMS == {"stop_atr": 2.0}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy" and spec.TIMEFRAMES == (24,)


def test_smash_day_long_and_short():
    spec = load("smash_day_reversal")
    base = np.full(20, 100.0)
    # bar 15: close 94 < previous low (99.5) and its low 93 < min of 8 lows; bar 16 closes 101 > smash high 100.5
    c = base.copy()
    c[15], c[16] = 94.0, 101.0
    h = c + 0.5
    lo = c - 0.5
    h[15], lo[15] = 100.5, 93.0
    sigs = [s for s in spec.signals(daily(c, h, lo)) if s.side == "long"]  # the bounce also makes a later short
    assert len(sigs) == 1
    assert sigs[0].entry == 101.0 and sigs[0].stop == 93.0 and sigs[0].valid()
    # the mirrored series gives one short with the stop at the smash-day high
    cm = 200.0 - c
    sh = [s for s in spec.signals(daily(cm, 200.0 - lo, 200.0 - h)) if s.side == "short"]
    assert len(sh) == 1 and sh[0].stop == pytest.approx(200.0 - 93.0) and sh[0].valid()


def test_mayer_fires_on_first_crossing():
    spec = load("mayer_multiple_fade")
    c = np.concatenate([np.full(250, 100.0), np.linspace(100, 400, 60), np.full(10, 400.0)])
    sigs = spec.signals(daily(c, c * 1.01, c * 0.99))
    shorts = [s for s in sigs if s.side == "short"]
    assert len(shorts) == 1 and shorts[0].stop > shorts[0].entry
    assert not any(s.side == "long" for s in sigs)


def test_td_setup9_completes_perfected_buy_setup():
    spec = load("td_setup9")
    # 8 flat-ish up bars then a steady fall: the flip bar f has close[f-1] > close[f-5]
    up = 100.0 + np.arange(1, 11)
    down = up[-1] - 1.5 * np.arange(1, 12)
    c = np.concatenate([np.full(15, 100.0), up, down])
    lo = c - 0.2
    sigs = [s for s in spec.signals(daily(c, c + 0.2, lo)) if s.side == "long"]
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    # a fall that never lasts nine bars gives no setup
    short_fall = np.concatenate([np.full(15, 100.0), up, up[-1] - np.array([1, 2, 3, 4, 5, 6, 7, 8.0]), np.full(10, 90.0)])
    assert not [s for s in spec.signals(daily(short_fall, short_fall + 0.2, short_fall - 0.2)) if s.side == "long"]


def test_elder_green_requires_bull_trend():
    spec = load("elder_impulse_green")
    rng = np.random.default_rng(3)
    c = 100.0 + np.cumsum(rng.normal(0.4, 1.0, 300))
    b = daily(c, c + 0.5, c - 0.5)
    e65 = ema(b.c, 65, segment_pos(b.t))
    sigs = spec.signals(b)
    assert sigs
    for s in sigs:
        i = int((s.t_ms - D0) // DAY_MS) - 1
        assert s.valid()
        assert (s.side == "long") == bool(b.c[i] > e65[i])  # long only above EMA65, short only below


def test_halloween_dates():
    spec = load("halloween_nov_apr")
    b = random_walk(800, seed=2, vol=0.02)  # 2020-01-01 .. 2022-03-11
    sigs = spec.signals(b)
    assert len(sigs) == 2 and all(s.side == "long" for s in sigs)
    nov_2020 = int(np.datetime64("2020-11-01", "ms").astype(np.int64))
    may_2021 = int(np.datetime64("2021-05-01", "ms").astype(np.int64))
    assert sigs[0].t_ms == nov_2020 and sigs[0].hold_ms == may_2021 - nov_2020


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    b = random_walk(2500, seed=5, vol=0.045)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
