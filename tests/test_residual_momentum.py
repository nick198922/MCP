"""Residual momentum: analytic references and point-in-time checks (IDEA-20261029-residual-momentum-short-leg)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.funding_crowding import Bars
from tradedesk.analytics.listing_drift import DAY_MS
from tradedesk.analytics.residual_momentum import (
    hedge_beta,
    inverse_vol_weights,
    ols_slope,
    placebo_shift_means,
    residual_momentum,
    staggered_book,
    weighted_partial_rank_ic,
)
from tradedesk.analytics.xs_factors import partial_rank_ic, rank_ic

T = 19_000 + (4 - 19_000 % 7) % 7  # a Monday
START = T - 200


def bars_from_returns(rets: dict[int, float], p0: float = 100.0) -> Bars:
    """Closes with close(d) / close(d - 1) - 1 = rets[d]; the first day START - 1 has close p0."""
    closes = {START - 1: p0}
    c = p0
    for d in sorted(rets):
        c *= 1.0 + rets[d]
        closes[d] = c
    rows = [[d * DAY_MS, v, v, v, v, 1.0, 1e7] for d, v in sorted(closes.items())]
    return Bars.from_rows(rows)


def make_pair(beta: float = 1.5, alpha: float = 0.001, seed: int = 1):
    """BTC returns and alt = beta * BTC + alpha + e, with e orthogonal to [1, BTC] on the estimation window [T-91; T-8]."""
    rng = np.random.default_rng(seed)
    days = list(range(START, T + 30))
    rb = rng.normal(0.0, 0.02, len(days))
    e = rng.normal(0.0, 0.03, len(days))
    est = np.array([T - 91 <= d <= T - 8 for d in days])
    x = np.column_stack([np.ones(est.sum()), rb[est]])
    coef, *_ = np.linalg.lstsq(x, e[est], rcond=None)
    e[est] = e[est] - x @ coef  # exactly orthogonal on the window
    ra = beta * rb + alpha + e
    btc = bars_from_returns(dict(zip(days, rb.tolist(), strict=True)))
    alt = bars_from_returns(dict(zip(days, ra.tolist(), strict=True)))
    return btc, alt, days, e, est


def test_ols_slope_reference():
    assert ols_slope([1.0, 2.0, 3.0, 4.0], [3.0, 5.0, 7.0, 9.0]) == pytest.approx(2.0)
    assert ols_slope([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert ols_slope([1.0, 2.0], [1.0, 2.0]) is None


def test_residual_momentum_recovers_beta_and_scaled_residual_sum():
    btc, alt, days, e, est = make_pair(beta=1.5, alpha=0.001)
    rm = residual_momentum(alt, btc, T)
    assert rm is not None
    assert rm.beta == pytest.approx(1.5, abs=1e-9)
    assert rm.n_est == 84 and rm.n_form == 28
    idx = {d: i for i, d in enumerate(days)}
    resid = [0.001 + e[idx[d]] for d in range(T - 91, T - 7)]
    sd = float(np.std(resid, ddof=1))
    assert rm.resid_sd == pytest.approx(sd, rel=1e-9)
    form = sum(0.001 + e[idx[d]] for d in range(T - 35, T - 7))
    assert rm.rm == pytest.approx(form / sd, rel=1e-8)
    rm56 = residual_momentum(alt, btc, T, form=56)
    assert rm56 is not None and rm56.n_form == 56
    assert rm56.rm == pytest.approx(sum(0.001 + e[idx[d]] for d in range(T - 63, T - 7)) / sd, rel=1e-8)


def test_pure_beta_coin_has_zero_signal_but_drift_counts():
    rng = np.random.default_rng(3)
    days = list(range(START, T))
    rb = rng.normal(0.0, 0.02, len(days))
    btc = bars_from_returns(dict(zip(days, rb.tolist(), strict=True)))
    noise = np.where(np.arange(len(days)) % 2 == 0, 0.01, -0.01)  # orthogonal-ish symmetric noise
    alt = bars_from_returns(dict(zip(days, (2.0 * rb + noise).tolist(), strict=True)))
    rm = residual_momentum(alt, btc, T)
    assert rm is not None
    assert abs(rm.rm) < 2.0  # 28 alternating +-1 % residuals sum to ~0
    up = bars_from_returns(dict(zip(days, (2.0 * rb + noise + 0.005).tolist(), strict=True)))
    rm_up = residual_momentum(up, btc, T)
    # a constant drift leaves beta and the residual SD unchanged and adds 28 * 0.5 % to the residual sum
    assert rm_up is not None and rm_up.beta == pytest.approx(rm.beta, abs=1e-9)
    assert rm_up.rm - rm.rm == pytest.approx(28 * 0.005 / rm.resid_sd, rel=1e-6)


def test_point_in_time_signal_ignores_last_week_and_future():
    btc, alt, days, _, _ = make_pair()
    base = residual_momentum(alt, btc, T)
    hb = hedge_beta(alt, btc, T)
    assert base is not None and hb is not None
    # change everything from day T-7 on (the skipped week and the future): RM must not move
    for d in range(T - 7, T + 30):
        alt.close[d] *= 3.0 if d % 2 else 0.2
        btc.close[d] *= 0.5
    after = residual_momentum(alt, btc, T)
    assert after is not None
    assert after.rm == pytest.approx(base.rm, rel=1e-12) and after.beta == pytest.approx(base.beta, rel=1e-12)
    # the hedge beta uses [T-91; T-1]: unchanged by days >= T, changed by the last week
    btc2, alt2, _, _, _ = make_pair()
    ref = hedge_beta(alt2, btc2, T)
    for d in range(T, T + 30):
        alt2.close[d] *= 5.0 if d % 2 else 0.3
    assert hedge_beta(alt2, btc2, T) == pytest.approx(ref, rel=1e-12)
    alt2.close[T - 3] *= 1.5
    assert hedge_beta(alt2, btc2, T) != pytest.approx(ref, rel=1e-6)


def test_hedge_beta_reference():
    btc, alt, _, _, _ = make_pair(beta=0.7)
    hb = hedge_beta(alt, btc, T)
    assert hb is not None and 0.4 < hb < 1.0  # [T-91; T-8] is exact 0.7; the last 7 days add noise
    assert hedge_beta(alt, btc, START + 30) is None  # fewer than 60 days


def test_thin_windows_give_none():
    btc, alt, _, _, _ = make_pair()
    for d in range(T - 35, T - 14):  # 21 of 28 formation days gone -> 7 left (< 20)
        del alt.close[d]
    assert residual_momentum(alt, btc, T) is None
    assert residual_momentum(alt, btc, T, min_form=5, min_est=40) is not None
    with pytest.raises(ValueError):
        residual_momentum(alt, btc, T, form=100)


def test_inverse_vol_weights():
    w = inverse_vol_weights([1.0, 2.0, 4.0])
    assert w.tolist() == pytest.approx([4 / 7, 2 / 7, 1 / 7])
    with pytest.raises(ValueError):
        inverse_vol_weights([1.0, 0.0])


def test_weighted_partial_ic_reduces_to_unweighted():
    rng = np.random.default_rng(5)
    s, r, c1, c2 = (rng.normal(size=60).tolist() for _ in range(4))
    eq = [1.0] * 60
    assert weighted_partial_rank_ic(s, r, [c1, c2], eq) == pytest.approx(partial_rank_ic(s, r, [c1, c2]), abs=1e-12)
    assert weighted_partial_rank_ic(s, r, [], eq) == pytest.approx(rank_ic(s, r), abs=1e-12)
    # weight concentrated on a perfectly ranked half dominates
    s2 = list(range(20))
    r2 = list(range(10)) + [9 - k for k in range(10)]  # first half concordant, second half reversed
    hi_first = [100.0] * 10 + [1.0] * 10
    assert weighted_partial_rank_ic(s2, r2, [], hi_first) > rank_ic(s2, r2)


def test_staggered_book():
    paths = [[0.01 * (i + 1) + 0.001 * k for k in range(3)] for i in range(5)]
    assert staggered_book(paths, 1) == pytest.approx([p[0] for p in paths])
    out = staggered_book(paths, 3)
    assert len(out) == 3
    # week 2: cohorts 2 (k=0), 1 (k=1), 0 (k=2)
    assert out[0] == pytest.approx((0.03 + 0.021 + 0.012) / 3)
    # changing a cohort's path after week j does not change weeks <= j
    paths2 = [list(p) for p in paths]
    paths2[4][0] = 9.9
    assert staggered_book(paths2, 3)[:2] == pytest.approx(out[:2])
    with pytest.raises(ValueError):
        staggered_book(paths, 0)


def test_placebo_shift_means():
    m = np.full((10, 4), 0.02)
    assert placebo_shift_means(m, 50).tolist() == pytest.approx([0.02] * 50)
    m2 = np.array([[np.nan, np.nan], [0.1, np.nan], [0.1, 0.3]])
    out = placebo_shift_means(m2, 200, seed=1)
    # week 0 never counts; week 1 counts only if column 0 drawn; week 2 always counts
    assert set(np.round(out, 10).tolist()) <= {0.1, 0.2, 0.3}
    assert math.isclose(placebo_shift_means(np.full((3, 1), np.nan), 2)[0], 0.0)
