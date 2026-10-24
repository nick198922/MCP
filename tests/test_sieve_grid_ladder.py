"""Spec ``grid_ladder`` (IDEA-20261024-grid-short-vol-regime): analytic features, signals on a known input, causality."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import sieve_specs
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars
from tradedesk.analytics.strategy_sieve import causality_violations, stamp_violations

T0 = 1_600_000_000_000 // HOUR_MS * HOUR_MS
spec = sieve_specs.load("grid_ladder")


def bars_from_close(c: np.ndarray, t: np.ndarray | None = None) -> Bars:
    c = np.asarray(c, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    t = T0 + np.arange(c.size, dtype=np.int64) * HOUR_MS if t is None else t
    return Bars(t, o, np.maximum(o, c), np.minimum(o, c), c, HOUR_MS)


def zigzag(n: int) -> np.ndarray:
    """100, 101, 100, 101, ...: no net move, constant |return| ln(1.01)."""
    return np.where(np.arange(n) % 2 == 0, 100.0, 101.0)


def random_walk(n: int, seed: int, vol: float = 0.006) -> Bars:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0, vol, n)))
    o = np.concatenate([[100.0], c[:-1]])
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, vol / 2, n)))
    return Bars(T0 + np.arange(n, dtype=np.int64) * HOUR_MS, o, h, lo, c, HOUR_MS)


def test_params_fixed_before_the_run():
    # the critic's clarifications: 3 steps from the entry, 1h, 72 h hold, the card's filter thresholds
    assert spec.PARAMS["stop_steps"] == 3.0 and spec.PARAMS["target_steps"] == 1.0
    assert spec.PARAMS["er_max"] == 0.25 and spec.PARAMS["vol_ratio_max"] == 1.0
    assert spec.PARAMS["third"] == pytest.approx(1 / 3) and spec.PARAMS["hold_h"] == 72
    assert spec.TIMEFRAMES == (1,) and spec.MANAGEMENT == "strategy"


def test_features_on_zigzag_match_analytic_values():
    f = spec.features(bars_from_close(zigzag(1000)))
    i = 900
    a = math.log(1.01)
    assert f["er20"][i] == pytest.approx(0.0, abs=1e-12)  # 480 bars back is the same price
    assert f["sigma24h"][i] == pytest.approx(a * math.sqrt(24 / 23) * math.sqrt(24), rel=1e-9)
    assert f["vol_ratio"][i] == pytest.approx(math.sqrt((168 / 167) / (720 / 719)), rel=1e-9)
    assert f["loc"][i] in (0.0, 1.0)
    assert not f["warm"][719] and f["warm"][720]  # 720 returns need 721 closes


def trend(n: int) -> np.ndarray:
    """Every hour up, by 0.15 % and 0.05 % in turn: a one-way move with a non-zero return SD."""
    r = 0.001 + 0.0005 * np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    r[0] = 0.0
    return 100 * np.exp(np.cumsum(r))


def test_features_on_a_trend():
    f = spec.features(bars_from_close(trend(1000)))
    assert f["er20"][900] == pytest.approx(1.0, rel=1e-9)  # one-way move
    assert f["loc"][900] == pytest.approx(1.0)  # closes at the 20-day high
    assert f["vol_ratio"][900] == pytest.approx(math.sqrt((168 / 167) / (720 / 719)), rel=1e-6)


def test_unfiltered_signals_on_zigzag():
    b = bars_from_close(zigzag(800))
    sigs = spec.signals(b, **{**spec.PARAMS, "regime": 0})
    # from the first warm bar (720) every bar flips between the bottom (long) and the top (short) of the range
    assert len(sigs) == 80
    s0 = min(sigs, key=lambda s: s.t_ms)
    assert s0.t_ms == b.close_time(720) and s0.side == "long" and s0.entry == 100.0  # c[720] = 100 (even)
    step = 0.5 * math.log(1.01) * math.sqrt(24 / 23) * math.sqrt(24)
    assert s0.stop == pytest.approx(100 * (1 - 3 * step), rel=1e-12)
    assert s0.target == pytest.approx(100 * (1 + step), rel=1e-12)
    assert s0.hold_ms == 72 * HOUR_MS
    sh = min((s for s in sigs if s.side == "short"), key=lambda s: s.t_ms)
    assert sh.entry == 101.0 and sh.stop == pytest.approx(101 * (1 + 3 * step)) and sh.target == pytest.approx(101 * (1 - step))
    assert all(s.valid() for s in sigs) and stamp_violations(b, sigs) == []


def test_regime_filter_gates_the_same_events():
    b = bars_from_close(zigzag(800))
    # zigzag: ER20 = 0 but sigma7 / sigma30 = 1.0023 > 1.0 -> the card's filter blocks everything
    assert spec.signals(b, **spec.PARAMS) == []
    loose = spec.signals(b, **{**spec.PARAMS, "vol_ratio_max": 1.01})
    assert len(loose) == len(spec.signals(b, **{**spec.PARAMS, "regime": 0}))
    # a trend: ER20 = 1 -> no filtered signal, while the unfiltered rung shorts the top of the range
    tr = bars_from_close(trend(1000))
    assert spec.signals(tr, **{**spec.PARAMS, "vol_ratio_max": 10.0}) == []
    assert [s.side for s in spec.signals(tr, **{**spec.PARAMS, "regime": 0})] == ["short"]  # one transition only


def test_step_sigma_scales_the_distances():
    b = bars_from_close(zigzag(800))
    s5 = min(spec.signals(b, **{**spec.PARAMS, "regime": 0}), key=lambda s: s.t_ms)
    s10 = min(spec.signals(b, **{**spec.PARAMS, "regime": 0, "step_sigma": 1.0}), key=lambda s: s.t_ms)
    assert (s10.target - s10.entry) == pytest.approx(2 * (s5.target - s5.entry))
    assert (s10.entry - s10.stop) == pytest.approx(2 * (s5.entry - s5.stop))


def test_a_gap_restarts_the_windows():
    c = zigzag(1600)
    t = T0 + np.arange(1600, dtype=np.int64) * HOUR_MS
    t[800:] += 49 * HOUR_MS  # a hole longer than 48 h
    sigs = spec.signals(bars_from_close(c, t), **{**spec.PARAMS, "regime": 0})
    first_after = min(s.t_ms for s in sigs if s.t_ms > int(t[800]))
    assert first_after == int(t[800 + 720]) + HOUR_MS


@pytest.mark.parametrize("regime", [0, 1])
def test_causal(regime):
    b = random_walk(24 * 200, seed=11)
    params = {**spec.PARAMS, "regime": regime}
    sigs = spec.signals(b, **params)
    assert sigs and all(s.valid() for s in sigs)
    m = len(b)
    cut = m * 2 // 3
    assert causality_violations(spec.signals, b, params, [m // 2, cut, m - 3], n_sample=40, n_last=10) == []
    # changing the bars after t does not change the signals decided by t
    edge = int(b.t[cut - 1]) + b.bar_ms
    k = 1.0 + np.linspace(0.0, 0.5, m - cut)
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
