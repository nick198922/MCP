"""Sieve batch 9 specs (connors_rsi_composite, kst_signal_cross, ao_saucer, engulfing_bar):
registered parameters, constructed-series signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars, segment_pos
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = ("connors_rsi_composite", "kst_signal_cross", "ao_saucer", "engulfing_bar")


def load(name: str):
    return sieve_specs.load(name)


def make(c, t=None, bar_ms: int = DAY_MS, h=None, lo=None) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) if h is None else np.asarray(h, dtype=float)
    lo = np.minimum(o, c) if lo is None else np.asarray(lo, dtype=float)
    t = D0 + np.arange(c.size, dtype=np.int64) * bar_ms if t is None else np.asarray(t, dtype=np.int64)
    return Bars(t, o, h, lo, c, bar_ms)


def random_walk(n: int, seed: int, vol: float, bar_ms: int = DAY_MS) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(D0 + np.arange(n, dtype=np.int64) * bar_ms, o, h, lo, c, bar_ms)


def test_registered_parameters():
    assert load("connors_rsi_composite").PARAMS == {
        "rsi_n": 3,
        "streak_n": 2,
        "rank_n": 100,
        "low": 10.0,
        "high": 90.0,
        "trend_n": 200,
        "stop_atr": 2.0,
        "hold_bars": 20,
    }
    assert load("kst_signal_cross").PARAMS == {"sig": 9, "stop_atr": 2.0, "hold_bars": 20}
    assert load("ao_saucer").PARAMS == {"fast": 5, "slow": 34, "stop_atr": 2.0, "hold_bars": 20}
    assert load("engulfing_bar").PARAMS == {"hold_bars": 20}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy" and spec.TIMEFRAMES == (24,)


def test_connors_rank_excludes_today_and_streak():
    mod = load("connors_rsi_composite")
    c = 100.0 * 1.01 ** np.arange(110)
    pos = segment_pos(make(c).t)
    streak = np.arange(110.0)  # an unbroken rise: streak 0, 1, 2, ...
    # today's change is the smallest of the window: rank 0
    c1 = c.copy()
    c1[-1] = c1[-2] * 1.005
    r1 = mod.rsi_wilder(c1, 3, pos)[-1]
    r2 = mod.rsi_wilder(streak, 2, pos)[-1]
    assert mod.connors_rsi(c1, pos, 3, 2, 100)[-1] == pytest.approx((r1 + r2 + 0.0) / 3.0)
    # today's change is the largest of the previous 100 changes: rank 100
    c2 = c.copy()
    c2[-1] = c2[-2] * 1.02
    r1 = mod.rsi_wilder(c2, 3, pos)[-1]
    assert mod.connors_rsi(c2, pos, 3, 2, 100)[-1] == pytest.approx((r1 + r2 + 100.0) / 3.0)


def test_connors_long_on_first_bar_of_condition_only():
    spec = load("connors_rsi_composite")
    rise = 100.0 + 0.5 * np.arange(250)
    c = np.concatenate([rise, rise[-1] - 3.0 * np.arange(1, 6)])
    b = make(c)
    sigs = spec.signals(b)
    crsi = spec.connors_rsi(b.c, segment_pos(b.t), 3, 2, 100)
    first = int(np.argmax(crsi[250:] <= 10.0)) + 250
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].valid() and sigs[0].target is None
    assert sigs[0].t_ms == b.close_time(first) and sigs[0].entry == pytest.approx(c[first])
    assert sigs[0].hold_ms == 20 * DAY_MS
    # a rise never gives an oversold ConnorsRSI
    assert spec.signals(make(100.0 + 0.5 * np.arange(300))) == []


def test_kst_cross_both_sides_and_segment_start():
    spec = load("kst_signal_cross")
    i = np.arange(500)
    c = 100.0 + 10.0 * np.sin(i / 15.0) + 0.02 * i
    sigs = spec.signals(make(c))
    assert {s.side for s in sigs} == {"long", "short"} and all(s.valid() for s in sigs)
    for s in sigs:
        assert (s.stop < s.entry) == (s.side == "long") and s.hold_ms == 20 * DAY_MS and s.target is None
    assert spec.signals(make(c[:50])) == []  # KST + SMA9 needs 30 + 15 - 1 + 9 bars
    # two 40-bar segments split by a gap: neither has enough history, NaN windows do not leak across
    keep = np.r_[0:40, 60:100]
    gapped = make(c[keep], t=D0 + keep.astype(np.int64) * DAY_MS)
    assert spec.signals(gapped) == []


def ao_reference(b: Bars) -> np.ndarray:
    mp = (b.h + b.low) / 2.0
    out = np.full(len(b), np.nan)
    for k in range(33, len(b)):
        out[k] = mp[k - 4 : k + 1].mean() - mp[k - 33 : k + 1].mean()
    return out


def test_ao_saucer_pattern_matches_independent_reading():
    spec = load("ao_saucer")
    i = np.arange(600)
    c = 100.0 + 0.05 * i + 6.0 * np.sin(i / 7.0) + 4.0 * np.sin(i / 2.3)
    b = make(c, h=c + 1.0, lo=c - 1.0)
    ao = ao_reference(b)
    sigs = spec.signals(b)
    exp = {}
    for k in range(36, len(b)):
        w = ao[k - 3 : k + 1]
        if np.all(w > 0) and w[1] < w[0] and w[2] < w[1] and w[3] > w[2]:
            exp[b.close_time(k)] = "long"
        elif np.all(w < 0) and w[1] > w[0] and w[2] > w[1] and w[3] < w[2]:
            exp[b.close_time(k)] = "short"
    assert exp and {s.t_ms: s.side for s in sigs} == exp
    assert {s.side for s in sigs} == {"long", "short"}
    assert all(s.valid() and s.hold_ms == 20 * DAY_MS and s.target is None for s in sigs)


def test_ao_saucer_needs_four_bars_on_one_side_of_zero():
    spec = load("ao_saucer")
    # a monotone rise: AO > 0 but never two falling bars
    assert spec.signals(make(100.0 + np.arange(120.0), h=100.0 + np.arange(120.0) + 1, lo=100.0 + np.arange(120.0) - 1)) == []


def test_engulfing_long_short_and_stop_at_bar_extreme():
    spec = load("engulfing_bar")
    long_b = make([100, 103], h=[101, 104], lo=[99, 98])
    (s,) = spec.signals(long_b)
    assert s.side == "long" and s.entry == 103 and s.stop == 98 and s.target is None and s.t_ms == long_b.close_time(1)
    short_b = make([100, 97], h=[101, 105], lo=[99, 96])
    (s,) = spec.signals(short_b)
    assert s.side == "short" and s.entry == 97 and s.stop == 105 and s.hold_ms == 20 * DAY_MS
    # inside bar / no sweep of the previous low: nothing
    assert spec.signals(make([100, 103], h=[101, 104], lo=[99, 100])) == []


def test_engulfing_requires_adjacent_bars():
    spec = load("engulfing_bar")
    t = D0 + np.array([0, 2 * DAY_MS], dtype=np.int64)
    assert spec.signals(make([100, 103], t=t, h=[101, 104], lo=[99, 98])) == []


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    b = random_walk(2500, seed=5, vol=0.045)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
