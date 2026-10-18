"""Cross-sectional factor IC panel: analytic references and point-in-time checks (IDEA-20261018-xs-factor-ic-panel)."""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from tradedesk.analytics.funding_crowding import Bars, leg_week, spearman
from tradedesk.analytics.listing_drift import DAY_MS
from tradedesk.analytics.xs_factors import (
    beta_ivol,
    close_near,
    daily_returns,
    holm_z,
    ic_products,
    max_return,
    partial_rank_ic,
    placebo_mean_ics,
    rank_ic,
    rank_z,
    residualize,
    rolling_means,
    signals_at,
    window_return,
    without_top,
)

T = 19_000 + (4 - 19_000 % 7) % 7  # a Monday (day 0 = Thursday 1970-01-01; Monday = 4 mod 7)


def bars_from_closes(closes: dict[int, float], qv: float = 1e7) -> Bars:
    rows = []
    for d, c in sorted(closes.items()):
        o = closes.get(d - 1, c)
        rows.append([d * DAY_MS, o, max(o, c), min(o, c), c, 1.0, qv])
    return Bars.from_rows(rows)


def geometric(days: range, growth: float, p0: float = 100.0) -> dict[int, float]:
    return {d: p0 * (1.0 + growth) ** (d - days.start) for d in days}


def test_rank_day_is_monday():
    assert T % 7 == 4


def test_window_returns_analytic():
    b = bars_from_closes(geometric(range(T - 60, T + 10), 0.01))
    assert window_return(b, T, 8, 1) == pytest.approx(1.01**7 - 1)  # R7
    assert window_return(b, T, 29, 8) == pytest.approx(1.01**21 - 1)  # M28 skip 7 = 21 days of return
    with pytest.raises(ValueError):
        window_return(b, T, 5, 0)


def test_close_near_never_looks_forward():
    b = bars_from_closes({T - 10: 1.0, T - 1: 2.0})
    assert close_near(b, T - 5) is None  # nearest earlier bar is 5 days back (> tol 3); the later bar is not used
    assert close_near(b, T - 8) == 1.0
    assert close_near(b, T - 1) == 2.0


def test_max_and_daily_returns():
    cl = {d: 100.0 for d in range(T - 40, T)}
    cl[T - 5] = 120.0  # +20 % on T-5, -16.7 % on T-4
    b = bars_from_closes(cl)
    r = daily_returns(b, T)
    assert len(r) == 30 and min(r) == T - 30 and max(r) == T - 1
    assert max_return(b, T) == pytest.approx(0.2)
    assert max_return(bars_from_closes({d: 1.0 for d in range(T - 10, T)}), T) is None  # < 20 returns


def test_beta_ivol_exact_and_noise():
    rng = random.Random(1)
    btc_cl, sym_cl = {T - 41: 100.0}, {T - 41: 10.0}
    eps = {}
    for d in range(T - 40, T + 5):
        rb = rng.gauss(0, 0.03)
        eps[d] = rng.gauss(0, 0.02)
        btc_cl[d] = btc_cl[d - 1] * (1 + rb)
        sym_cl[d] = sym_cl[d - 1] * (1 + 0.001 + 1.5 * rb + eps[d])
    btc, sym = bars_from_closes(btc_cl), bars_from_closes(sym_cl)
    b, iv = beta_ivol(sym, btc, T)
    assert b == pytest.approx(1.5, abs=0.25)
    e = [eps[d] for d in range(T - 30, T)]
    assert iv == pytest.approx(np.std(e, ddof=1), rel=0.2)
    b0, iv0 = beta_ivol(btc, btc, T)
    assert b0 == pytest.approx(1.0) and iv0 == pytest.approx(0.0, abs=1e-12)


def test_signals_point_in_time():
    """Any change to bars of days >= t leaves every signal at t unchanged."""
    rng = random.Random(3)
    base = {d: 50.0 * math.exp(0.02 * rng.gauss(0, 1) + 0.001 * (d - T)) for d in range(T - 60, T + 14)}
    btc_c = {d: 1e4 * math.exp(0.01 * rng.gauss(0, 1)) for d in range(T - 60, T + 14)}
    s0 = signals_at(bars_from_closes(base), bars_from_closes(btc_c), T)
    assert s0 is not None
    fut = dict(base)
    for d in range(T, T + 14):
        fut[d] = base[d] * 10 ** rng.uniform(-1, 1)
    btc_f = dict(btc_c)
    for d in range(T, T + 14):
        btc_f[d] = btc_c[d] * 3.0
    s1 = signals_at(bars_from_closes(fut), bars_from_closes(btc_f), T)
    assert s1 == s0
    # and with the future removed entirely (delisting at t)
    cut = {d: v for d, v in base.items() if d < T}
    assert signals_at(bars_from_closes(cut), bars_from_closes({d: v for d, v in btc_c.items() if d < T}), T) == s0
    # a change before t does change it
    early = dict(base)
    early[T - 1] *= 2
    assert signals_at(bars_from_closes(early), bars_from_closes(btc_c), T) != s0


def test_delisted_inside_week_keeps_return_to_last_close():
    cl = {d: 10.0 for d in range(T - 40, T)}
    cl[T], cl[T + 1], cl[T + 2] = 9.0, 6.0, 5.0  # last bar T+2, then delisted
    b = bars_from_closes(cl)
    lg = leg_week("X", b, [], T)
    assert lg is not None and lg.truncated and lg.exit == T + 2
    assert lg.r == pytest.approx(5.0 / 10.0 - 1.0)  # open of T is the close of T-1


def test_rank_z_and_ic():
    z = rank_z([3.0, 1.0, 2.0, 2.0])
    assert z.mean() == pytest.approx(0.0) and z.std() == pytest.approx(1.0)
    assert z[1] < z[2] == z[3] < z[0]
    g = random.Random(5)
    xs = [g.random() for _ in range(50)]
    ys = [x**3 + 0.1 * random.Random(i).random() for i, x in enumerate(xs)]
    assert rank_ic(xs, ys) == pytest.approx(spearman(xs, ys))
    assert rank_ic(xs, [-x for x in xs]) == pytest.approx(-1.0)
    assert ic_products(xs, ys).mean() == pytest.approx(rank_ic(xs, ys))
    assert rank_z([1.0, 1.0]).tolist() == [0.0, 0.0]


def test_partial_ic_removes_control():
    rng = np.random.default_rng(0)
    c = rng.normal(size=400)
    s = c + 0.01 * rng.normal(size=400)  # signal is the control
    r = c + rng.normal(size=400)  # return driven by the control only
    assert rank_ic(s, r) > 0.4
    assert abs(partial_rank_ic(s, r, [c])) < 0.1
    assert partial_rank_ic(s, r, []) == pytest.approx(rank_ic(s, r))
    y = np.array([1.0, 2.0, 3.0, 4.0])
    assert np.allclose(residualize(y, [y]), 0.0)


def test_placebo_is_centered_with_known_spread():
    rng = np.random.default_rng(2)
    weeks = [(rng.normal(size=100).tolist(), rng.normal(size=100).tolist()) for _ in range(30)]
    m = placebo_mean_ics(weeks, 2000, seed=1)
    assert abs(m.mean()) < 0.003
    assert m.std() == pytest.approx(1 / math.sqrt(99 * 30), rel=0.1)  # SD of a shuffled IC is 1/sqrt(N-1) per week
    assert np.array_equal(m, placebo_mean_ics(weeks, 2000, seed=1))


def test_holm_z_and_helpers():
    assert holm_z(1) == pytest.approx(1.959964 + 0.841621, abs=1e-5)
    assert holm_z(4) == pytest.approx(2.497705 + 0.841621, abs=1e-5)
    assert without_top([1.0, 5.0, -3.0, 2.0], 1, +1) == [1.0, -3.0, 2.0]
    assert without_top([1.0, 5.0, -3.0, 2.0], 1, -1) == [1.0, 5.0, 2.0]
    assert rolling_means([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx([1.5, 2.5, 3.5])
    assert rolling_means([1.0], 2) == []
