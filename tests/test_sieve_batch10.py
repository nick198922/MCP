"""Sieve batch 10 specs (kama_cross, strat_212_reversal, connors_3day_hl, consecutive_high_close_short,
bb_rsi_overbought_short): registered parameters, constructed-series signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, segment_pos
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("kama_cross", "strat_212_reversal", "connors_3day_hl", "consecutive_high_close_short", "bb_rsi_overbought_short")


def load(name: str):
    return sieve_specs.load(name)


def make(c, bar_ms: int = DAY_MS, h=None, lo=None) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) if h is None else np.asarray(h, dtype=float)
    lo = np.minimum(o, c) if lo is None else np.asarray(lo, dtype=float)
    return Bars(D0 + np.arange(c.size, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def random_walk(n: int, seed: int, vol: float, bar_ms: int = DAY_MS) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def test_registered_parameters():
    assert load("kama_cross").PARAMS == {"er_n": 10, "fast": 2, "slow": 30, "stop_atr": 2.0, "hold_bars": 20, "warmup": 50}
    assert load("strat_212_reversal").PARAMS == {"hold_bars": 20}
    assert load("connors_3day_hl").PARAMS == {"trend_n": 200, "exit_n": 5, "stop_atr": 2.0, "hold_bars": 5}
    assert load("consecutive_high_close_short").PARAMS == {"threshold": 3, "stop_atr": 2.0, "hold_bars": 20}
    assert load("bb_rsi_overbought_short").PARAMS == {
        "bb_n": 20,
        "bb_k": 2.0,
        "bb_level": 1.0,
        "rsi_n": 7,
        "rsi_level": 65.0,
        "tp_pct": 0.0211,
        "stop_atr": 2.0,
        "hold_bars": 20,
    }
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
    assert load("bb_rsi_overbought_short").TIMEFRAMES == (1,)
    assert all(load(n).TIMEFRAMES == (24,) for n in SPECS if n != "bb_rsi_overbought_short")


def test_kama_seed_constant_and_cross_signals():
    mod = load("kama_cross")
    c = 100.0 + np.arange(30.0) ** 1.1
    pos = segment_pos(make(c).t)
    k = mod.kama(c, pos, 10, 2, 30)
    assert np.isnan(k[:9]).all() and k[9] == pytest.approx(c[:10].mean())
    flat = mod.kama(np.full(40, 7.0), segment_pos(make(np.full(40, 7.0)).t), 10, 2, 30)
    assert np.allclose(flat[9:], 7.0)
    i = np.arange(500)
    b = make(100.0 + 10.0 * np.sin(i / 15.0) + 0.02 * i)
    kk = mod.kama(b.c, segment_pos(b.t), 10, 2, 30)
    sigs = mod.signals(b)
    exp = {}
    for j in range(50, len(b)):
        if b.c[j - 1] <= kk[j - 1] and b.c[j] > kk[j]:
            exp[b.close_time(j)] = "long"
        elif b.c[j - 1] >= kk[j - 1] and b.c[j] < kk[j]:
            exp[b.close_time(j)] = "short"
    assert exp and {s.t_ms: s.side for s in sigs} == exp
    assert {s.side for s in sigs} == {"long", "short"}
    assert all(s.valid() and s.hold_ms == 20 * DAY_MS and s.target is None for s in sigs)
    assert mod.signals(make(c[:30])) == []  # inside the 50-bar warm-up


def test_strat_212_long_and_short_levels():
    spec = load("strat_212_reversal")
    # 2D (lower low, no higher high), inside bar, 2U (takes out the inside high only)
    long_b = make([95, 92, 91, 96], h=[100, 99, 95, 97], lo=[90, 85, 88, 89])
    (s,) = spec.signals(long_b)
    assert s.side == "long" and s.entry == 96 and s.stop == 88 and s.target == 99 and s.t_ms == long_b.close_time(3)
    assert s.hold_ms == 20 * DAY_MS and s.valid()
    # 2U, inside bar, 2D
    short_b = make([95, 100, 98, 93], h=[100, 105, 102, 101], lo=[90, 91, 94, 92])
    (s,) = spec.signals(short_b)
    assert s.side == "short" and s.entry == 93 and s.stop == 102 and s.target == 91 and s.valid()
    # no inside bar in the middle: nothing
    assert spec.signals(make([95, 92, 91, 96], h=[100, 99, 100, 97], lo=[90, 85, 88, 89])) == []


def test_connors_3day_hl_short_and_mirrored_long():
    spec = load("connors_3day_hl")
    c = np.concatenate([300.0 - 0.5 * np.arange(245), [150.0, 160.0, 165.0, 171.0, 178.0]])
    b = make(c, h=c + 1.0, lo=c - 1.0)
    sigs = spec.signals(b)
    assert {s.t_ms for s in sigs} == {b.close_time(248), b.close_time(249)}
    assert all(s.side == "short" and s.valid() and s.target is None and s.hold_ms == 5 * DAY_MS for s in sigs)
    cl = 600.0 - c  # mirror: uptrend below which the closes sit above SMA200, three lower highs and lows
    bl = make(cl, h=cl + 1.0, lo=cl - 1.0)
    longs = spec.signals(bl)
    assert {s.t_ms for s in longs} == {bl.close_time(248), bl.close_time(249)}
    assert all(s.side == "long" and s.valid() for s in longs)
    # not below SMA200 (an uptrend): no short
    up = 100.0 + np.arange(250.0)
    assert [s for s in spec.signals(make(up, h=up + 1.0, lo=up - 1.0)) if s.side == "short"] == []


def test_consecutive_close_above_high_counts_and_resets_on_low_break():
    spec = load("consecutive_high_close_short")
    # 20 flat bars, then closes above the previous high at 20, 21, 22 (count 3 -> short)
    c = [100.0] * 20 + [102.0, 103.0, 104.0, 104.2, 90.0, 92.0, 93.0]
    h = [101.0] * 20 + [102.5, 103.5, 104.5, 104.5, 90.5, 92.5, 93.5]
    lo = [99.0] * 20 + [100.0, 101.0, 103.0, 103.5, 89.0, 91.0, 92.0]
    b = make(c, h=h, lo=lo)
    sigs = spec.signals(b)
    # bar 23 neither closes above the prior high nor below the prior low: the count stays 3 and it fires again
    assert [s.t_ms for s in sigs] == [b.close_time(22), b.close_time(23)]
    assert all(s.side == "short" and s.stop > s.entry and s.target is None and s.hold_ms == 20 * DAY_MS for s in sigs)
    # bar 24 closes below the prior low: reset; bars 25, 26 give only a count of 2
    assert b.close_time(24) not in {s.t_ms for s in sigs}


def test_bb_rsi_short_on_joint_cross():
    spec = load("bb_rsi_overbought_short")
    c = np.array([99.9 if i % 2 == 0 else 100.1 for i in range(40)] + [101.0])
    b = make(c, bar_ms=HOUR_MS, h=c + 0.1, lo=c - 0.1)
    pos = segment_pos(b.t)
    pb = spec.percent_b(b.c, 20, 2.0, pos)
    rs = spec.rsi_wilder(b.c, 7, pos)
    assert pb[39] <= 1.0 < pb[40] and rs[39] <= 65.0 < rs[40]
    (s,) = spec.signals(b)
    assert s.side == "short" and s.t_ms == b.close_time(40) and s.entry == 101.0 and s.valid()
    assert s.target == pytest.approx(101.0 * (1 - 0.0211)) and s.stop > s.entry and s.hold_ms == 20 * HOUR_MS
    # a steady rise: RSI 100 throughout, no cross
    assert spec.rsi_wilder(np.arange(30.0) + 1, 7, segment_pos(make(np.arange(30.0) + 1).t))[-1] == 100.0


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    bar_ms = HOUR_MS if name == "bb_rsi_overbought_short" else DAY_MS
    b = random_walk(2500, seed=5, vol=0.045 if bar_ms == DAY_MS else 0.02, bar_ms=bar_ms)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
