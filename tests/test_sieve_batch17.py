"""Sieve batch 17 specs (wyckoff_spring_utad, sr_break_retest, eod_sma50_breakout10, trend_sma200_cross): registered
parameters, constructed-series signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("wyckoff_spring_utad", "sr_break_retest", "eod_sma50_breakout10", "trend_sma200_cross")


def load(name: str):
    return sieve_specs.load(name)


def make(c, h=None, lo=None) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + 0.2 if h is None else np.asarray(h, dtype=float)
    lo = np.minimum(o, c) - 0.2 if lo is None else np.asarray(lo, dtype=float)
    return Bars(D0 + np.arange(c.size, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)


def walk(n: int, seed: int, vol: float = 0.03) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100.0 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.004, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.004, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * DAY_MS, o, h, lo, c, DAY_MS)


def test_registered_parameters():
    assert load("wyckoff_spring_utad").PARAMS == {
        "box_n": 20, "move_n": 60, "move_pct": 0.15, "max_height": 0.12, "stop_atr": 1.0, "hold_bars": 30,
    }  # fmt: skip
    assert load("sr_break_retest").PARAMS == {
        "window": 40, "band": 0.01, "min_swings": 2, "retest_bars": 8, "rr": 2.0, "hold_bars": 8,
    }  # fmt: skip
    assert load("eod_sma50_breakout10").PARAMS == {
        "sma_n": 50, "slope_bars": 10, "slope_pct": 0.01, "break_n": 10, "stop_atr": 2.0, "hold_bars": 20,
    }  # fmt: skip
    assert load("trend_sma200_cross").PARAMS == {"sma_n": 200, "stop_atr": 3.0, "hold_bars": 20}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy" and spec.TIMEFRAMES == (24,)


def test_wyckoff_spring_and_utad():
    spec = load("wyckoff_spring_utad")
    rng = np.random.default_rng(1)
    flat = 100.0 + rng.uniform(-0.5, 0.5, 20)
    c = np.concatenate([np.linspace(150.0, 100.0, 60), flat, [100.0]])
    h, lo = np.maximum(c, np.concatenate([[c[0]], c[:-1]])) + 0.2, np.minimum(c, np.concatenate([[c[0]], c[:-1]])) - 0.2
    lo[-1] = 97.0  # the spring bar: low under the box, close back inside
    b = make(c, h, lo)
    sigs = [s for s in spec.signals(b) if s.t_ms == b.close_time(len(c) - 1)]
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].valid() and sigs[0].hold_ms == 30 * DAY_MS
    hh, ll = b.h[-21:-1].max(), b.low[-21:-1].min()
    assert sigs[0].target == pytest.approx(hh + (hh - ll)) and sigs[0].entry == 100.0 and sigs[0].stop < 97.0
    # mirror: a rally of 50 % into a flat box, then an upthrust
    c2 = np.concatenate([np.linspace(100.0, 150.0, 60), 150.0 + rng.uniform(-0.7, 0.7, 20), [150.0]])
    prev = np.concatenate([[c2[0]], c2[:-1]])
    h2, lo2 = np.maximum(c2, prev) + 0.2, np.minimum(c2, prev) - 0.2
    h2[-1] = 155.0
    b2 = make(c2, h2, lo2)
    ss = [s for s in spec.signals(b2) if s.t_ms == b2.close_time(len(c2) - 1)]
    assert len(ss) == 1 and ss[0].side == "short" and ss[0].valid() and ss[0].stop > 155.0 and ss[0].target < 150.0
    # no prior 15 % move -> no spring
    c3 = np.concatenate([np.full(80, 100.0), [100.0]])
    h3, lo3 = c3 + 0.2, c3 - 0.2
    lo3[-1] = 97.0
    assert spec.signals(make(c3, h3, lo3)) == []


def _sr_series(n: int = 40):
    c = np.full(n, 90.0)
    h, lo = c + 0.2, c - 0.2
    h[5], h[12] = 100.0, 100.3  # two swing highs inside a 1 % band
    c[20], h[20], lo[20] = 102.0, 102.5, 101.0  # break: close above the band top
    c[21], h[21], lo[21] = 102.0, 102.4, 101.5
    c[22], h[22], lo[22] = 101.0, 101.5, 99.9  # retest: back into the band, closes above it
    return c, h, lo


def test_sr_break_retest_long_and_short_mirror():
    spec = load("sr_break_retest")
    c, h, lo = _sr_series()
    b = make(c, h, lo)
    sigs = [s for s in spec.signals(b) if s.t_ms == b.close_time(22)]
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].valid()
    s = sigs[0]
    assert s.entry == 101.0 and s.stop == pytest.approx(99.9) and s.target == pytest.approx(101.0 + 2 * 1.1)
    assert s.hold_ms == 8 * DAY_MS
    bm = make(200.0 - c, 200.0 - lo, 200.0 - h)
    sm = [x for x in spec.signals(bm) if x.t_ms == bm.close_time(22)]
    assert len(sm) == 1 and sm[0].side == "short" and sm[0].valid() and sm[0].stop == pytest.approx(200.0 - 99.9)
    # a retest bar that closes back inside the band cancels the setup (one retest only)
    c2, h2, lo2 = _sr_series()
    c2[22] = 100.1
    assert [x for x in spec.signals(make(c2, h2, lo2)) if x.side == "long"] == []
    # swing highs too far apart (more than 1 %) form no level
    c3, h3, lo3 = _sr_series()
    h3[12] = 102.0
    c3[20], h3[20], lo3[20] = 103.0, 103.5, 102.2
    c3[21], h3[21], lo3[21] = 103.0, 103.4, 102.5
    c3[22], h3[22], lo3[22] = 102.5, 102.6, 101.9
    assert [x for x in spec.signals(make(c3, h3, lo3)) if x.side == "long"] == []


def test_eod_breakout_long_and_short():
    spec = load("eod_sma50_breakout10")
    up = make(np.linspace(100.0, 140.0, 70))
    sl = [s for s in spec.signals(up) if s.side == "long"]
    assert sl and all(s.valid() and s.target is None and s.hold_ms == 20 * DAY_MS for s in sl)
    assert not [s for s in spec.signals(up) if s.side == "short"]
    dn = make(np.linspace(140.0, 100.0, 70))
    ss = [s for s in spec.signals(dn) if s.side == "short"]
    assert ss and all(s.valid() and s.stop > s.entry for s in ss)
    # flat average: no slope, no signal even with a breakout bar
    flat = np.concatenate([np.full(80, 100.0), [110.0]])
    assert spec.signals(make(flat)) == []


def test_sma200_cross_first_close_after_a_close_below():
    spec = load("trend_sma200_cross")
    c = np.concatenate([np.full(210, 100.0), np.full(3, 95.0), [105.0, 106.0]])
    b = make(c)
    sigs = spec.signals(b)
    longs = [s for s in sigs if s.side == "long"]
    assert len(longs) == 1 and longs[0].t_ms == b.close_time(213) and longs[0].valid()
    assert longs[0].target is None and longs[0].hold_ms == 20 * DAY_MS
    c2 = np.concatenate([np.full(210, 100.0), np.full(3, 105.0), [95.0, 94.0]])
    b2 = make(c2)
    shorts = [s for s in spec.signals(b2) if s.side == "short"]
    assert len(shorts) == 1 and shorts[0].t_ms == b2.close_time(213) and shorts[0].stop > shorts[0].entry
    assert spec.signals(make(c[:150])) == []  # not enough bars for the SMA200


def test_fifty_two_week_high_364():
    spec = load("fifty_two_week_high_364")
    assert spec.PARAMS == {"high_n": 364, "stop_n": 20, "rr": 2.0, "max_stop_pct": 0.08, "hold_bars": 20}
    assert spec.NAME == "fifty_two_week_high_364" and spec.TIMEFRAMES == (24,) and spec.MANAGEMENT == "strategy"
    c = np.concatenate([np.full(380, 100.0), [101.0, 102.0], np.full(3, 100.0)])
    b = make(c)
    sigs = spec.signals(b)
    assert len(sigs) == 1  # the second close above is not a fresh cross
    s = sigs[0]
    assert s.t_ms == b.close_time(380) and s.side == "long" and s.valid() and s.hold_ms == 20 * DAY_MS
    assert s.stop == pytest.approx(99.8) and s.target == pytest.approx(101.0 + 2 * (101.0 - 99.8))
    assert spec.signals(make(c[:300])) == []  # fewer than 364 bars of history
    wide = np.concatenate([np.full(380, 100.0), [120.0]])
    wl = make(wide, lo=np.concatenate([np.full(380, 80.0), [119.0]]))
    assert spec.signals(wl) == []  # stop farther than 8 % of the entry is skipped


@pytest.mark.parametrize("name", SPECS + ("fifty_two_week_high_364",))
def test_causal(name):
    spec = load(name)
    b = walk(2500, seed=7, vol=0.015 if name in ("wyckoff_spring_utad", "fifty_two_week_high_364") else 0.03)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
