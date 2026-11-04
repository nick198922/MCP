from __future__ import annotations

import math

import numpy as np

from tradedesk.analytics import regime_side as rs
from tradedesk.analytics import seller_exhaustion as se

D = se.DAY_MS
T0 = 1_600_000_000_000 // D * D


def _prices(logret: list[float], p0: float = 100.0) -> dict[int, float]:
    out = {T0: p0}
    p = p0
    for i, r in enumerate(logret, start=1):
        p *= math.exp(r)
        out[T0 + i * D] = p
    return out


def test_daily_series_index_and_gaps() -> None:
    s = se.daily_series([1.0, None, float("nan"), 4.0], T0)
    assert s == {T0: 1.0, T0 + 3 * D: 4.0}


def test_sec_constant_growth_is_zero() -> None:
    px = _prices([0.01] * 40)
    sh = {t: 80.0 for t in px}
    sec = se.sec_series(sh, px)
    assert min(sec) == T0 + 30 * D  # the first day with 31 closes
    assert all(abs(v) < 1e-12 for v in sec.values())


def test_sec_alternating_returns_analytic() -> None:
    a = 0.02
    px = _prices([a if i % 2 == 0 else -a for i in range(30)])
    sh = {T0 + 30 * D: 60.0}
    sec = se.sec_series(sh, px)
    # 15 x (+a) and 15 x (-a): mean 0, population sd a
    assert math.isclose(sec[T0 + 30 * D], 0.6 * a * math.sqrt(30), rel_tol=1e-9)


def test_sec_needs_31_consecutive_days() -> None:
    px = _prices([0.01] * 80)
    del px[T0 + 20 * D]
    sh = {t: 50.0 for t in px}
    sec = se.sec_series(sh, px)
    # day k needs closes k-30 .. k: before the gap only days >= 30 would qualify, and those up to 50 cross it
    assert sorted(sec) == [T0 + k * D for k in range(51, 81)]


def test_sec_no_lookahead() -> None:
    rng = np.random.default_rng(1)
    lr = list(rng.normal(0, 0.03, 80))
    px = _prices(lr)
    sh = {t: float(50 + 10 * math.sin(i)) for i, t in enumerate(sorted(px))}
    base = se.sec_series(sh, px)
    cut = T0 + 55 * D
    px2 = dict(px)
    sh2 = dict(sh)
    for t in px2:
        if t > cut:
            px2[t] *= 3.0
            sh2[t] = 5.0
    alt = se.sec_series(sh2, px2)
    for t, v in base.items():
        if t <= cut:
            assert alt[t] == v
    assert any(alt[t] != base[t] for t in base if t > cut)


def test_sec_sign_threshold_strict() -> None:
    s = se.sec_sign({1: 0.0499, 2: 0.05, 3: 0.2, 4: float("nan")})
    assert s == {1: 1, 2: 0, 3: 0, 4: 0}


def test_masks_and_sign_before() -> None:
    signs = {T0: 1, T0 + D: 0}
    sides = ["short", "short", "long", "long", "short"]
    t_in = [T0 + D, T0 + 2 * D + 5, T0 + D + 7, T0 + 2 * D, T0 + D]  # decisions in day T0 + D use day T0
    sg = [rs.sign_before(t, signs) for t in t_in]
    assert sg == [1, 0, 1, 0, 1]
    kept, drop = se.short_masks(sides, sg)
    assert kept.tolist() == [False, True, False, False, False]
    assert drop.tolist() == [True, False, False, False, True]
    rest, low = se.long_masks(sides, sg)
    assert rest.tolist() == [False, False, False, True, False]
    assert low.tolist() == [False, False, True, False, False]


def test_decision_before_day_close_does_not_see_that_day() -> None:
    # a decision one ms before day T0 + D closes must use day T0, not T0 + D
    signs = {T0: 0, T0 + D: 1}
    assert rs.sign_before(T0 + 2 * D - 1, signs) == 0
    assert rs.sign_before(T0 + 2 * D, signs) == 1
