"""SMT divergence as a sweep filter (course setups, step 10): analytic cases and causality."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, atr, segment_pos
from tradedesk.analytics.markup import fractals
from tradedesk.analytics.per_asset import chi2_sf
from tradedesk.analytics.smt_divergence import (
    cochran_q,
    depth_stratified_diff,
    forward_move,
    fractal_lows,
    joint_cluster_diff,
    joint_grid,
    resample_min,
    smt_verdict,
    sweep_events,
)


def bars(low: list[float], close: list[float] | None = None, t0: int = 0) -> Bars:
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float) if close is not None else lo + 0.5
    t = t0 + np.arange(lo.size, dtype=np.int64) * HOUR_MS
    return Bars(t, c.copy(), np.maximum(lo, c) + 0.5, lo, c, HOUR_MS)


A_LOW = [10, 9, 8, 9, 9.5, 9.6, 9.7, 7.9, 8.5, 9.0]
A_CLOSE = [10.5, 9.5, 8.5, 9.5, 10, 10, 10, 8.4, 9.0, 9.5]


def run(a: Bars, b: Bars, **kw: object) -> list:
    pos = segment_pos(a.t)
    return sweep_events(a, b, pos, np.ones(len(a)), warmup=0, **kw)  # type: ignore[arg-type]


def test_fractal_lows_three_candle_and_five_candle() -> None:
    low = np.array([5, 4, 3, 4, 5, 2, 6, 7, 1, 8])
    pos = np.arange(low.size)
    assert list(np.flatnonzero(fractal_lows(low, pos, 1))) == [2, 5, 8]
    assert list(np.flatnonzero(fractal_lows(low, pos, 2))) == [2, 5]  # 8 has only one bar on the right
    # a segment break right after a candidate cancels it
    pos2 = pos.copy()
    pos2[3:] = np.arange(low.size - 3)
    assert 2 not in np.flatnonzero(fractal_lows(low, pos2, 1))


def test_fractal_lows_match_markup_fractals() -> None:
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 1, 300))
    low = c - rng.uniform(0.1, 1, 300)
    high = c + rng.uniform(0.1, 1, 300)
    mine = set(np.flatnonzero(fractal_lows(low, np.arange(300), 1)).tolist())
    rows = [{"ts": i, "high": float(h), "low": float(lo)} for i, (h, lo) in enumerate(zip(high, low, strict=True))]
    ref = {f["idx"] for f in fractals(rows)["lows"]}
    assert mine == ref and len(mine) > 30


def test_smt_sweep_when_b_holds_higher_low() -> None:
    a = bars(A_LOW, A_CLOSE)
    b = bars([20, 19, 18, 18.5, 19, 19, 19, 18.2, 18.6, 19])
    ev = run(a, b)
    assert len(ev) == 1
    e = ev[0]
    assert (e.t, e.sweep, e.f) == (7, 7, 2)
    assert e.level_a == 8 and e.level_b == 18
    assert e.smt and e.close_back
    assert e.depth_atr == pytest.approx(0.1)


def test_confirmed_when_b_also_breaks_its_low() -> None:
    a = bars(A_LOW, A_CLOSE)
    b = bars([20, 19, 18, 18.5, 19, 19, 19, 17.9, 18.6, 19])
    (e,) = run(a, b)
    assert not e.smt
    # B leading: its lower low came before A's sweep -> still confirmed
    b2 = bars([20, 19, 18, 18.5, 19, 17.5, 19, 18.2, 18.6, 19])
    (e2,) = run(a, b2)
    assert not e2.smt


def test_b_level_uses_plus_minus_lag_window() -> None:
    a = bars(A_LOW, A_CLOSE)
    # B's own swing is one bar later (lag): its low 17.8 at bar 3 is inside f +- 2, so 18.0 at the sweep is a higher low
    b = bars([20, 19, 18.5, 17.8, 19, 19, 19, 18.0, 18.6, 19])
    (e,) = run(a, b)
    assert e.level_b == 17.8 and e.smt
    (e0,) = run(a, b, lag=0)
    assert e0.level_b == 18.5 and not e0.smt


def test_breakdown_only_in_any_mode() -> None:
    close = list(A_CLOSE)
    close[7] = 7.95  # closes below the swing low
    a = bars(A_LOW, close)
    b = bars([20, 19, 18, 18.5, 19, 19, 19, 18.2, 18.6, 19])
    assert run(a, b) == []
    (e,) = run(a, b, mode="any")
    assert not e.close_back and e.smt


def test_after_window_sees_b_lag() -> None:
    a = bars(A_LOW, A_CLOSE)
    b = bars([20, 19, 18, 18.5, 19, 19, 19, 18.2, 17.7, 19])  # B breaks one bar after A's sweep
    (e0,) = run(a, b)
    assert e0.smt and e0.t == 7
    (e2,) = run(a, b, after=2)
    assert not e2.smt and e2.t == 9 and e2.sweep == 7
    assert run(a, b, after=3) == []  # decision bar beyond the data


def test_swing_consumed_and_replaced() -> None:
    # two sweeps of two different swings; the second fractal replaces the first
    low = [10, 9, 8, 9, 9.5, 7.9, 9, 8.5, 9.2, 9.4, 8.4, 9]
    close = [10.5, 9.5, 8.5, 9.5, 10, 8.3, 9.5, 9, 9.7, 9.9, 8.6, 9.5]
    a = bars(low, close)
    b = bars([20.0] * len(low))
    ev = run(a, b)
    assert [(e.f, e.sweep) for e in ev] == [(2, 5), (7, 10)]


def test_mirrored_gives_bearish_sweep() -> None:
    """A high swept by A while B makes a lower high: the pattern on negated prices, found through ``mirrored``."""
    x, y = bars(A_LOW, A_CLOSE), bars([20, 19, 18, 18.5, 19, 19, 19, 18.2, 18.6, 19])
    neg_a = Bars(x.t, -x.o, -x.low, -x.h, -x.c, HOUR_MS)  # the pattern now sits on the highs
    neg_b = Bars(y.t, -y.o, -y.low, -y.h, -y.c, HOUR_MS)
    assert neg_a.h[2] == -8 and neg_a.h[7] == pytest.approx(-7.9)
    assert run(neg_a, neg_b) == []  # no sweep of a low in the bearish series
    (e,) = run(neg_a.mirrored(), neg_b.mirrored())
    assert (e.f, e.sweep, e.smt) == (2, 7, True)


def test_causal_events_do_not_change_with_future_bars() -> None:
    rng = np.random.default_rng(7)
    n = 1500
    ca = 100 + np.cumsum(rng.normal(0, 1, n))
    cb = 50 + 0.5 * np.cumsum(rng.normal(0, 1, n)) + 0.4 * (ca - 100)
    t = np.arange(n, dtype=np.int64) * HOUR_MS

    def mk(c: np.ndarray, r: np.random.Generator) -> Bars:
        lo = c - r.uniform(0.05, 1.0, c.size)
        hi = c + r.uniform(0.05, 1.0, c.size)
        return Bars(t, c.copy(), hi, lo, c.copy(), HOUR_MS)

    a, b = mk(ca, np.random.default_rng(1)), mk(cb, np.random.default_rng(2))
    cut = 900
    a2 = Bars(t, a.o.copy(), a.h.copy(), a.low.copy(), a.c.copy(), HOUR_MS)
    b2 = Bars(t, b.o.copy(), b.h.copy(), b.low.copy(), b.c.copy(), HOUR_MS)
    for arr in (a2.h, a2.low, a2.c, b2.h, b2.low, b2.c):
        arr[cut + 1 :] += rng.normal(0, 3, n - cut - 1)
    a2.h[cut + 1 :] = np.maximum(a2.h[cut + 1 :], a2.c[cut + 1 :])
    a2.low[cut + 1 :] = np.minimum(a2.low[cut + 1 :], a2.c[cut + 1 :])
    pos = segment_pos(t)
    for kw in ({}, {"after": 2}, {"mode": "any"}, {"n": 2}):
        e1 = [e for e in sweep_events(a, b, pos, atr(a, 14, pos), warmup=20, **kw) if e.t <= cut]  # type: ignore[arg-type]
        e2 = [e for e in sweep_events(a2, b2, pos, atr(a2, 14, pos), warmup=20, **kw) if e.t <= cut]  # type: ignore[arg-type]
        assert e1 == e2 and len(e1) > 20


def test_resample_min_and_joint_grid() -> None:
    step = 3 * HOUR_MS
    t = np.array([0, 1, 2, 3, 5, 6, 7, 8], dtype=np.int64) * HOUR_MS  # bucket 1 misses hour 4
    x = np.arange(8, dtype=float) + 1
    b = Bars(t, x, x + 1, x - 1, x + 0.5, HOUR_MS)
    r = resample_min(b, step, 2)
    assert list(r.t // HOUR_MS) == [0, 3, 6]
    assert r.o[1] == 4 and r.c[1] == 5.5 and r.h[1] == 6 and r.low[1] == 3
    assert list(resample_min(b, step, 3).t // HOUR_MS) == [0, 6]
    a = Bars(np.array([0, 1, 2, 4], dtype=np.int64) * HOUR_MS, *(np.arange(4.0) for _ in range(4)), HOUR_MS)
    c = Bars(np.array([1, 2, 3, 4], dtype=np.int64) * HOUR_MS, *(np.arange(4.0) + 10 for _ in range(4)), HOUR_MS)
    ja, jb = joint_grid(a, c)
    assert list(ja.t // HOUR_MS) == [1, 2, 4] and list(jb.c) == [10, 11, 13]
    ja, jb = joint_grid(a, c, shift_b_ms=HOUR_MS)  # A at T with B at T - 1h
    assert list(ja.t // HOUR_MS) == [2, 4] and list(jb.c) == [10, 12]


def test_forward_move_respects_segments() -> None:
    c = np.array([1.0, 2.0, 4.0, 8.0])
    pos = np.array([0, 1, 0, 1])
    assert forward_move(c, pos, 0, 1) == 1.0
    assert math.isnan(forward_move(c, pos, 1, 1))
    assert math.isnan(forward_move(c, pos, 2, 5))


def test_depth_stratified_diff() -> None:
    y = [1.0, 0.0, 3.0, 1.0, 5.0]
    smt = [True, False, True, False, True]
    depth = [0.1, 0.1, 0.9, 0.9, 0.9]
    out = depth_stratified_diff(y, smt, depth, [0.5])
    # bin 0: 1 - 0 = 1 (1 SMT); bin 1: mean(3, 5) - 1 = 3 (2 SMT) -> (1 + 6) / 3
    assert out["diff"] == pytest.approx(7 / 3) and out["bins"] == 2 and out["n_smt"] == 3


def test_joint_cluster_diff() -> None:
    a, b = [1.0, 2.0, 3.0, 4.0], [0.0, 0.0, 1.0, 1.0]
    cl = ["d1", "d1", "d2", "d2"]
    r = joint_cluster_diff(a, cl, b, cl, n_boot=2000, seed=1)
    assert r["diff"] == pytest.approx(2.0)
    assert r["ci"][0] >= 1.0 and r["ci"][1] <= 3.0 and r["p"] < 0.01 and r["se"] > 0
    # identical constant shift: no sampling noise in the difference
    r2 = joint_cluster_diff([2.0, 2.0, 2.0], ["x", "y", "z"], [1.0, 1.0, 1.0], ["x", "y", "z"], n_boot=500)
    assert r2["diff"] == pytest.approx(1.0) and r2["se"] == pytest.approx(0.0, abs=1e-12)
    assert joint_cluster_diff([], [], [1.0], ["x"])["ci"] is None


def test_cochran_q_analytic() -> None:
    r = cochran_q([0.0, 1.0], [0.5, 0.5])
    assert r["q"] == pytest.approx(2.0) and r["df"] == 1 and r["mean"] == pytest.approx(0.5)
    assert r["p"] == pytest.approx(chi2_sf(2.0, 1)) and r["p"] == pytest.approx(0.1573, abs=1e-4)
    assert r["i2"] == pytest.approx(0.5)
    assert cochran_q([1.0], [0.1])["p"] is None


def test_smt_verdict_rules() -> None:
    good = dict(
        n_dates=500,
        mde=0.15,
        diff=0.3,
        ci_day=(0.1, 0.5),
        ci_week=(0.05, 0.55),
        p_holm=0.01,
        oos_diff=0.2,
        oos_p=0.02,
        trade_ci_is=(0.001, 0.004),
        trade_oos_mean=0.002,
    )
    assert smt_verdict(**good) == "confirmed"  # type: ignore[arg-type]
    assert smt_verdict(**{**good, "n_dates": 199}) == "inconclusive"  # type: ignore[arg-type]
    assert smt_verdict(**{**good, "mde": 0.25}) == "inconclusive"  # type: ignore[arg-type]
    assert smt_verdict(**{**good, "diff": 0.15}) == "rejected"  # type: ignore[arg-type]
    assert smt_verdict(**{**good, "ci_day": (-0.01, 0.6)}) == "rejected"  # type: ignore[arg-type]
    assert smt_verdict(**{**good, "p_holm": 0.2}) == "inconclusive"  # type: ignore[arg-type]
    assert smt_verdict(**{**good, "trade_ci_is": (-0.001, 0.004)}) == "inconclusive"  # type: ignore[arg-type]
    assert smt_verdict(**{**good, "oos_p": 0.2}) == "inconclusive"  # type: ignore[arg-type]
