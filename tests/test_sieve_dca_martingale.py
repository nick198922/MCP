"""Spec ``dca_martingale`` (IDEA-20261024-dca-martingale-bots-sieve): the 24 h move, first-crossing signals on known
inputs, the three registered variants, causality."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import causality_violations, stamp_violations

T0 = 1_600_000_000_000 // HOUR_MS * HOUR_MS
spec = sieve_specs.load("dca_martingale")


def bars_from_close(c: np.ndarray, t: np.ndarray | None = None) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    t = T0 + np.arange(c.size, dtype=np.int64) * HOUR_MS if t is None else t
    return Bars(t, o, np.maximum(o, c), np.minimum(o, c), c, HOUR_MS)


def random_walk(n: int, seed: int, vol: float = 0.01) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(T0 + np.arange(n, dtype=np.int64) * HOUR_MS, o, h, lo, c, HOUR_MS)


def test_params_fixed_before_the_run():
    # the card's S-DCA-1 and the critic's clarification 5
    p = spec.PARAMS
    assert p == {"direction": 1, "move": 0.04, "lookback_h": 24, "target": 0.015, "stop": 0.12, "hold_h": 168}
    assert spec.TIMEFRAMES == (1,) and spec.MANAGEMENT == "strategy"


def test_move_is_the_24h_ratio():
    c = 100 * 1.001 ** np.arange(60)
    m = spec.move_24h(bars_from_close(c))
    assert np.isnan(m[:24]).all()
    assert m[24:] == pytest.approx(np.full(36, 1.001**24 - 1), rel=1e-12)


def test_move_needs_the_bar_exactly_24h_back():
    c = np.full(60, 100.0)
    t = T0 + np.arange(60, dtype=np.int64) * HOUR_MS
    t[30:] += HOUR_MS  # one missing hourly bar (not a segment break)
    c[40:] = 110.0
    m = spec.move_24h(bars_from_close(c, t))
    # bar k >= 30 opens at T0 + (k + 1) h; the hour T0 + 30 h is missing
    assert np.isnan(m[:24]).all() and np.isfinite(m[24:30]).all()
    assert m[30] == 0.0  # T0 + 31 h -> T0 + 7 h = index 7 (24 h back, not 24 bars back)
    assert m[47] == pytest.approx(0.1)  # T0 + 48 h -> T0 + 24 h = index 24 (price 100)
    assert np.isnan(m[53])  # T0 + 54 h -> T0 + 30 h: the missing bar
    assert m[54] == pytest.approx(0.1)  # T0 + 55 h -> T0 + 31 h = index 30 (price 100)


def test_a_gap_over_48h_breaks_the_window():
    c = np.full(100, 100.0)
    t = T0 + np.arange(100, dtype=np.int64) * HOUR_MS
    t[50:] += 49 * HOUR_MS
    m = spec.move_24h(bars_from_close(c, t))
    assert np.isnan(m[50:74]).all() and np.isfinite(m[74])


def dip_series() -> np.ndarray:
    """Flat at 100 for 30 h, then down 1 % per hour for 10 h, flat 40 h, back up to 100, flat, then another dip."""
    flat = np.full(30, 100.0)
    down = 100 * 0.99 ** np.arange(1, 11)
    low = np.full(40, down[-1])
    up = np.full(40, 100.0)
    return np.concatenate([flat, down, low, up, 100 * 0.99 ** np.arange(1, 11), np.full(10, 100 * 0.99**10)])


def test_long_fires_once_per_crossing():
    c = dip_series()
    b = bars_from_close(c)
    sigs = spec.signals(b, **spec.PARAMS)
    m = spec.move_24h(b)
    cross = [i for i in range(1, c.size) if np.isfinite(m[i - 1]) and m[i] <= -0.04 and not m[i - 1] <= -0.04]
    # first dip: 0.99^5 = 0.951 -> -4.9 % at the 5th falling bar (index 34); second dip similarly at index 124
    assert cross == [34, 124]
    assert [s.t_ms for s in sigs] == [b.close_time(i) for i in cross]
    s0 = sigs[0]
    assert s0.side == "long" and s0.entry == c[34]
    assert s0.target == pytest.approx(c[34] * 1.015) and s0.stop == pytest.approx(c[34] * 0.88)
    assert s0.hold_ms == 168 * HOUR_MS
    assert all(s.valid() for s in sigs) and stamp_violations(b, sigs) == []


def test_variants_stop_and_short_mirror():
    c = dip_series()
    b = bars_from_close(c)
    s2 = spec.signals(b, **{**spec.PARAMS, "stop": 0.06})
    assert [s.t_ms for s in s2] == [s.t_ms for s in spec.signals(b, **spec.PARAMS)]
    assert s2[0].stop == pytest.approx(c[34] * 0.94)
    # the mirror on this series fires only on the recovery (+10.6 % over 24 h at index 80)
    assert [s.t_ms for s in spec.signals(b, **{**spec.PARAMS, "direction": -1})] == [b.close_time(80)]
    # 200 - c turns each -x % dip from 100 into a +x % rise: the short fires at the same bars as the long
    inv = bars_from_close(200.0 - c)
    sh = spec.signals(inv, **{**spec.PARAMS, "direction": -1})
    assert [s.t_ms for s in sh] == [b.close_time(34), b.close_time(124)]
    e = 200.0 - c[34]
    assert sh[0].side == "short" and sh[0].stop == pytest.approx(e * 1.12) and sh[0].target == pytest.approx(e * 0.985)
    assert all(s.valid() for s in sh)


def test_bad_params_give_no_signal():
    b = bars_from_close(dip_series())
    assert spec.signals(b, **{**spec.PARAMS, "direction": 0}) == []
    assert spec.signals(b, **{**spec.PARAMS, "stop": 1.0}) == []


@pytest.mark.parametrize("over", [{}, {"stop": 0.06}, {"direction": -1}])
def test_causal(over):
    b = random_walk(24 * 200, seed=7)
    params = {**spec.PARAMS, **over}
    sigs = spec.signals(b, **params)
    assert sigs and all(s.valid() for s in sigs)
    m = len(b)
    cut = m * 2 // 3
    assert causality_violations(spec.signals, b, params, [m // 2, cut, m - 3], n_sample=40, n_last=10) == []
    # changing the bars after t does not change the signals decided by t
    edge = int(b.t[cut - 1]) + b.bar_ms
    k = 1.0 + np.linspace(0.0, -0.5, m - cut)
    changed = Bars(
        b.t,
        np.concatenate([b.o[:cut], b.o[cut:] * k]),
        np.concatenate([b.h[:cut], b.h[cut:] * k * 1.01]),
        np.concatenate([b.low[:cut], b.low[cut:] * k * 0.99]),
        np.concatenate([b.c[:cut], b.c[cut:] * k]),
        b.bar_ms,
    )
    before = sorted((s.t_ms, s.side, s.stop) for s in sigs if s.t_ms <= edge)
    after = sorted((s.t_ms, s.side, s.stop) for s in spec.signals(changed, **params) if s.t_ms <= edge)
    assert before == after
