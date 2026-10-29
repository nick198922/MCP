"""Sieve batch 14 specs (range_fade_atr_zones, bollinger_reentry_fade, monthly_floor_pivot_cross,
coinrule_short_ema_cross, triple_ema_4_9_18): registered parameters, constructed-series signals, long/short symmetry
and causality (no look-ahead)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import causality_violations

D0 = 1_577_836_800_000  # 2020-01-01 00:00 UTC
SPECS = (
    "range_fade_atr_zones",
    "bollinger_reentry_fade",
    "monthly_floor_pivot_cross",
    "coinrule_short_ema_cross",
    "triple_ema_4_9_18",
)
SYMMETRIC = ("range_fade_atr_zones", "bollinger_reentry_fade", "triple_ema_4_9_18")
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
    """AR(1) around 100 with slow swings; stays far from zero so K - price is a valid mirror market."""
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
    """The same market upside down inside positive prices: price -> K - price."""
    return Bars(b.t, K - b.o, K - b.low, K - b.h, K - b.c, b.bar_ms)


def test_registered_parameters():
    assert load("range_fade_atr_zones").PARAMS == {
        "n": 20,
        "atr_n": 14,
        "zone": 0.25,
        "stop_atr": 0.5,
        "touches": 2,
        "hold_bars": 20,
    }
    assert load("bollinger_reentry_fade").PARAMS == {"n": 20, "k": 2.0, "atr_n": 14, "buffer_atr": 1.0, "hold_bars": 8}
    assert load("monthly_floor_pivot_cross").PARAMS == {"chop_window": 10, "chop_min": 3, "hold_bars": 15}
    assert load("coinrule_short_ema_cross").PARAMS == {"fast": 20, "slow": 50, "take_pct": 0.08, "stop_pct": 0.16, "hold_days": 20}
    assert load("triple_ema_4_9_18").PARAMS == {"fast": 4, "mid": 9, "slow": 18, "atr_n": 14, "stop_atr": 2.0, "hold_bars": 20}
    for name in SPECS:
        spec = load(name)
        assert spec.NAME == name and spec.MANAGEMENT == "strategy"
    assert load("coinrule_short_ema_cross").TIMEFRAMES == (1,)
    assert all(load(n).TIMEFRAMES == (24,) for n in SPECS if n != "coinrule_short_ema_cross")


def test_range_fade_long_levels():
    spec = load("range_fade_atr_zones")
    # a 20-bar box oscillating between 100 and 110 (two touches of each edge), then a dip into the bottom zone that closes back
    c = np.array([105, 110, 105, 100, 105, 110, 105, 100, 105, 108, 105, 103, 105, 107, 105, 104, 105, 106, 105, 105], dtype=float)
    h, lo = c + 0.5, c - 0.5
    h[c == 110], lo[c == 100] = 110.0, 100.0
    h[[4, 5]] = [105.5, 110.0]
    c = np.concatenate([c, [104.0]])
    h = np.concatenate([h, [105.0]])
    lo = np.concatenate([lo, [100.4]])  # low inside the bottom zone (<= 100 + 0.25 ATR), close back above it
    b = make(c, h=h, lo=lo)
    sigs = [s for s in spec.signals(b, atr_n=3) if s.t_ms == b.close_time(len(c) - 1)]
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side == "long" and s.valid() and s.entry == 104.0 and s.target == pytest.approx(105.0)
    assert s.stop < 100.0 and s.hold_ms == 20 * DAY_MS


def test_bollinger_reentry_needs_a_close_beyond_then_back_inside():
    spec = load("bollinger_reentry_fade")
    rng = np.random.default_rng(2)
    base = 100.0 + rng.normal(0.0, 1.0, 40)
    # stretch bar far below the band, then the next close back inside
    c = np.concatenate([base, [90.0, 99.0]])
    b = make(c, h=c + 0.5, lo=c - 0.5)
    j = len(c) - 1
    sigs = [s for s in spec.signals(b) if s.t_ms == b.close_time(j)]
    assert len(sigs) == 1 and sigs[0].side == "long" and sigs[0].valid()
    w = c[j - 19 : j + 1]
    assert sigs[0].entry == 99.0 and sigs[0].target == pytest.approx(float(w.mean()))
    assert sigs[0].stop < 89.5 and sigs[0].hold_ms == 8 * DAY_MS
    # without the stretch bar nothing fires on the last bar
    c2 = np.concatenate([base, [100.0, 99.0]])
    b2 = make(c2, h=c2 + 0.5, lo=c2 - 0.5)
    assert [s for s in spec.signals(b2) if s.t_ms == b2.close_time(len(c2) - 1)] == []


def month_days(year_month_starts: list[str]) -> np.ndarray:
    return np.arange(np.datetime64(year_month_starts[0], "D"), np.datetime64(year_month_starts[1], "D"), dtype="datetime64[D]").astype(
        np.int64
    )


def test_monthly_pivot_cross_long_and_short():
    spec = load("monthly_floor_pivot_cross")
    jan = month_days(["2020-01-01", "2020-02-01"])
    feb = month_days(["2020-02-01", "2020-03-01"])
    days = np.concatenate([jan, feb])
    # January: high 110, low 90, last close 100 -> P = 100, R1 = 110, S1 = 90
    c = np.full(days.size, 100.0)
    c[:31] = 100.0
    h, lo = c + 1.0, c - 1.0
    h[3], lo[5] = 110.0, 90.0
    # February: below the pivot (97), then a close above (101) on the 8th day: long; later a close below: short
    c[31:38] = 97.0
    c[38] = 101.0
    h[31:] = c[31:] + 1.0
    lo[31:] = c[31:] - 1.0
    b = Bars(days * DAY_MS, c.copy(), h, lo, c, DAY_MS)
    sigs = spec.signals(b)
    longs = [s for s in sigs if s.side == "long"]
    assert len(longs) == 1
    s = longs[0]
    assert s.t_ms == int(b.t[38]) + DAY_MS and s.entry == 101.0 and s.stop == 100.0 and s.target == pytest.approx(110.0)
    assert s.hold_ms == 15 * DAY_MS and s.valid()
    # a month that is not complete gives no levels
    cut = np.concatenate([np.arange(0, 15), np.arange(31, 60)])
    b2 = Bars(b.t[cut], b.o[cut], b.h[cut], b.low[cut], b.c[cut], DAY_MS)
    assert spec.signals(b2) == []
    # the short side exists on a fall through P
    c3 = c.copy()
    c3[31:38] = 103.0
    c3[38] = 99.0
    h3, lo3 = h.copy(), lo.copy()
    h3[31:], lo3[31:] = c3[31:] + 1.0, c3[31:] - 1.0
    b3 = Bars(days * DAY_MS, c3.copy(), h3, lo3, c3, DAY_MS)
    shorts = [s for s in spec.signals(b3) if s.side == "short"]
    assert shorts and shorts[0].target == pytest.approx(90.0) and shorts[0].stop == 100.0 and shorts[0].valid()


def test_coinrule_short_cross_levels():
    spec = load("coinrule_short_ema_cross")
    c = np.concatenate([np.linspace(100.0, 140.0, 120), np.linspace(140.0, 80.0, 120)])
    b = make(c, bar_ms=HOUR_MS)
    sigs = spec.signals(b)
    assert len(sigs) == 1 and sigs[0].side == "short"
    s = sigs[0]
    assert s.stop == pytest.approx(s.entry * 1.16) and s.target == pytest.approx(s.entry * 0.92)
    assert s.hold_ms == 20 * DAY_MS and s.valid()
    assert spec.signals(make(c, bar_ms=DAY_MS)) == []  # not 1h bars


def test_triple_ema_first_close_completing_the_order():
    spec = load("triple_ema_4_9_18")
    c = np.concatenate([np.linspace(130.0, 100.0, 60), np.linspace(100.0, 130.0, 40)])
    b = make(c)
    sigs = spec.signals(b)
    longs = [s for s in sigs if s.side == "long"]
    assert len(longs) == 1
    s = longs[0]
    i = [k for k in range(len(b)) if b.close_time(k) == s.t_ms][0]
    assert s.target is None and s.entry == b.c[i] and s.stop < s.entry and s.hold_ms == 20 * DAY_MS
    # the bar before it was not in the bullish order (one signal per episode)
    assert i > 60


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


@pytest.mark.parametrize("name", SPECS)
def test_causal(name):
    spec = load(name)
    if name == "coinrule_short_ema_cross":
        rng = np.random.default_rng(5)
        c = 100 * np.exp(np.cumsum(rng.normal(0.0, 0.012, 24 * 200)))
        o = np.concatenate([[100.0], c[:-1]])
        h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.004, c.size)))
        lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.004, c.size)))
        b = Bars(D0 + np.arange(c.size, dtype=np.int64) * HOUR_MS, o, h, lo, c, HOUR_MS)
    else:
        b = mean_reverting(2500, seed=11)
    params = dict(spec.PARAMS)
    sigs = spec.signals(b, **params)
    assert len(sigs) >= 1 and all(s.valid() for s in sigs)
    m = len(b)
    assert causality_violations(spec.signals, b, params, [m // 2, m * 2 // 3, int(m * 0.97)], n_sample=40, n_last=10) == []
