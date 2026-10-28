"""Sieve batch 8 specs (rsi4h_3commas_dca, cumulative_rsi2, faber_sma10m, trix_signal_cross, friday_effect):
registered parameters, constructed-series signals and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, Bars
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC (Wednesday)
H4_MS = 4 * 3_600_000
SPECS = ("rsi4h_3commas_dca", "cumulative_rsi2", "faber_sma10m", "trix_signal_cross", "friday_effect")


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


def ms(date: str) -> int:
    return int(np.datetime64(date, "ms").astype(np.int64))


def test_registered_parameters():
    assert load("rsi4h_3commas_dca").PARAMS == {"rsi_n": 14, "rsi_below": 28.0, "target": 0.03, "stop": 0.25, "hold_bars": 20}
    assert load("cumulative_rsi2").PARAMS == {
        "rsi_n": 2,
        "cum_n": 2,
        "below": 35.0,
        "trend_n": 200,
        "stop_atr": 2.0,
        "hold_bars": 20,
    }
    assert load("faber_sma10m").PARAMS == {"months": 10, "stop_atr": 2.0}
    assert load("trix_signal_cross").PARAMS == {"n": 15, "sig": 9, "stop_atr": 2.0, "hold_bars": 20}
    assert load("friday_effect").PARAMS == {"stop_atr": 2.0}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
    assert load("rsi4h_3commas_dca").TIMEFRAMES == (4,)
    for name in SPECS[1:]:
        assert load(name).TIMEFRAMES == (24,)


def test_rsi4h_oversold_gives_percent_target_and_stop():
    spec = load("rsi4h_3commas_dca")
    c = np.concatenate([np.full(20, 100.0) + np.tile([0.5, -0.5], 10), 100.0 - 2.0 * np.arange(1, 11)])
    sigs = spec.signals(make(c, bar_ms=H4_MS))
    assert sigs and all(s.side == "long" and s.valid() for s in sigs)
    for s in sigs:
        assert s.stop == pytest.approx(s.entry * 0.75) and s.target == pytest.approx(s.entry * 1.03)
        assert s.hold_ms == 20 * H4_MS
    # a steadily rising series never has RSI < 28
    assert spec.signals(make(100.0 + np.arange(60) * 0.5, bar_ms=H4_MS)) == []


def test_cumulative_rsi2_cross_below_35_above_sma200():
    spec = load("cumulative_rsi2")
    rise = 100.0 + 0.1 * np.arange(250)
    c = np.concatenate([rise, rise[-1] - 2.0 * np.arange(1, 6)])
    sigs = spec.signals(make(c))
    # cumulative RSI(2) is ~105 after the first drop bar and ~6 after the second: the cross is on the second drop bar
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].valid() and sigs[0].target is None
    assert sigs[0].t_ms == D0 + 252 * DAY_MS  # close time of bar 251 (second drop)
    assert sigs[0].entry == pytest.approx(c[251]) and sigs[0].hold_ms == 20 * DAY_MS
    # a crash that takes the close below its SMA200 is not traded
    crash = np.concatenate([rise, rise[-1] - 30.0 * np.arange(1, 4)])
    assert spec.signals(make(crash)) == []


def test_faber_month_end_signals():
    spec = load("faber_sma10m")
    rising = make(100.0 + 0.5 * np.arange(400))
    sigs = spec.signals(rising)
    # month ends Jan 2020 .. Jan 2021 = 13; the first with 10 month ends is 31 Oct 2020
    assert len(sigs) == 4 and all(s.side == "long" and s.valid() and s.target is None for s in sigs)
    assert sigs[0].t_ms == ms("2020-11-01") and sigs[0].hold_ms == ms("2020-12-01") - ms("2020-11-01")
    assert [s.t_ms for s in sigs][:2] == [ms("2020-11-01"), ms("2020-12-01")]
    assert spec.signals(make(300.0 - 0.5 * np.arange(400))) == []  # below SMA10m at every month end
    # a gap in mid-February splits the segment: Oct 2020 no longer has 10 month ends behind it in one segment
    keep = np.r_[0:45, 51:400]
    gapped = make(100.0 + 0.5 * np.arange(400)[keep], t=D0 + keep.astype(np.int64) * DAY_MS)
    ts = [s.t_ms for s in spec.signals(gapped)]
    assert ms("2020-11-01") not in ts and ts[0] == ms("2020-12-01")


def test_trix_cross_both_sides():
    spec = load("trix_signal_cross")
    i = np.arange(400)
    c = 100.0 + 10.0 * np.sin(i / 12.0) + 0.02 * i
    sigs = spec.signals(make(c))
    assert {s.side for s in sigs} == {"long", "short"} and all(s.valid() for s in sigs)
    for s in sigs:
        assert (s.stop < s.entry) == (s.side == "long") and s.hold_ms == 20 * DAY_MS and s.target is None
    # TRIX needs 3 * (n - 1) + 1 + sig bars: a shorter history has no signal
    assert spec.signals(make(c[:50])) == []


def test_friday_only_thursday_bars():
    spec = load("friday_effect")
    b = random_walk(120, seed=4, vol=0.03)
    sigs = spec.signals(b)
    assert sigs
    for s in sigs:
        assert s.side == "long" and s.valid() and s.hold_ms == DAY_MS and s.target is None
        assert (s.t_ms // DAY_MS + 3) % 7 == 4  # the signal bar closes at 00:00 UTC Friday
    assert len({s.t_ms for s in sigs}) == len(sigs)


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    bar_ms = H4_MS if name == "rsi4h_3commas_dca" else DAY_MS
    b = random_walk(2500, seed=5, vol=0.045 if bar_ms == DAY_MS else 0.02, bar_ms=bar_ms)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
