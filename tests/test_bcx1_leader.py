"""BCX-1 intraday leader: analytic expectations and causality (IDEA-20261113-x-traders, item 5)."""

from __future__ import annotations

import numpy as np
import pytest

from tradedesk.analytics import bcx1_leader as bx
from tradedesk.analytics import xo4_vwap as x4
from tradedesk.analytics.stop_management import Fees

H = bx.HOUR_MS


def test_on_grid_places_and_skips_off_grid() -> None:
    t = np.array([0, H, 3 * H, 3 * H + 5, 10 * H])
    g = bx.on_grid(t, np.array([1.0, 2.0, 3.0, 9.0, 4.0]), 0, 5)
    assert g[:4].tolist()[:2] == [1.0, 2.0] and np.isnan(g[2]) and g[3] == 3.0 and np.isnan(g[4])


def test_lagged_beta_exact_and_lag_is_causal() -> None:
    rng = np.random.default_rng(1)
    rb = rng.normal(0, 0.01, 200)
    btc = 100 * np.exp(np.cumsum(rb))
    coin = 5 * np.exp(np.cumsum(2.0 * rb))
    b = bx.lagged_beta(coin, btc, window=50, lag=5, min_obs=40)
    assert np.isnan(b[53]) and b[54] == pytest.approx(2.0, abs=1e-9)  # first window: rows 0 .. 49 (49 returns), row 54
    assert np.allclose(b[54:], 2.0, atol=1e-9)
    coin2 = coin.copy()
    coin2[100:] *= np.exp(np.cumsum(rng.normal(0, 0.05, 100)))
    b2 = bx.lagged_beta(coin2, btc, window=50, lag=5, min_obs=40)
    assert np.array_equal(b[:105], b2[:105], equal_nan=True)  # row k uses returns up to k - lag only
    assert not np.allclose(b[105:], b2[105:])


def test_lagged_beta_needs_min_obs() -> None:
    btc = np.linspace(100, 110, 80) * (1 + 0.01 * np.sin(np.arange(80)))
    coin = btc.copy()
    coin[10:40] = np.nan
    b = bx.lagged_beta(coin, btc, window=50, lag=0, min_obs=30)
    assert np.isnan(b[60])  # rows 11 .. 60: 20 finite pairs (41 .. 60)
    assert b[79] == pytest.approx(1.0, abs=1e-9)


def test_residual_return_analytic() -> None:
    btc = np.array([100.0] * 12 + [110.0])
    coin = np.array([1.0] * 12 + [1.25])
    e = bx.residual_return(coin, btc, np.full(13, 2.0), n=12)
    assert np.isnan(e[11]) and e[12] == pytest.approx(0.25 - 2.0 * 0.10)


def test_top_k_mask_respects_members_and_nans() -> None:
    e = np.array([[0.1, 0.5, 0.3, np.nan, 0.9]])
    mem = np.array([[True, True, True, True, False]])
    assert bx.top_k_mask(e, mem, k=2).tolist() == [[False, True, True, False, False]]


def _events_fixture() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    e = np.full((8, 3), 0.0)
    e[:, 0] = 0.01  # always top-1 but below min_e
    e[3, 1] = 0.08  # coin 1 enters the top-1 at row 3
    e[4, 1] = 0.09  # still top: not "first in lookback"
    e[7, 2] = 0.06  # coin 2 enters at row 7
    return e, np.ones_like(e, dtype=bool), np.zeros(8)


def test_leader_events_first_entry_threshold_and_btc_filter() -> None:
    e, mem, btc = _events_fixture()
    r, c = bx.leader_events(e, mem, btc, k=1, min_e=0.05, lookback=2)
    assert list(zip(r.tolist(), c.tolist(), strict=True)) == [(3, 1), (7, 2)]
    btc[7] = -0.03
    r, c = bx.leader_events(e, mem, btc, k=1, min_e=0.05, lookback=2)
    assert list(zip(r.tolist(), c.tolist(), strict=True)) == [(3, 1)]
    mem[3, 1] = False  # not in the universe that day: coin 0 is the leader at row 3, coin 1 first counts at row 4
    r, c = bx.leader_events(e, mem, np.zeros(8), k=1, min_e=0.05, lookback=2)
    assert (4, 1) in list(zip(r.tolist(), c.tolist(), strict=True))


def test_leader_events_before_t_do_not_change_with_later_data() -> None:
    rng = np.random.default_rng(7)
    e = rng.normal(0, 0.04, (300, 20))
    mem = rng.random((300, 20)) > 0.2
    btc = rng.normal(0, 0.01, 300)
    r1, c1 = bx.leader_events(e, mem, btc, k=3, lookback=24)
    e2, mem2, btc2 = e.copy(), mem.copy(), btc.copy()
    e2[150:] = rng.normal(0, 0.04, (150, 20))
    mem2[150:] = True
    btc2[150:] = -1.0
    r2, c2 = bx.leader_events(e2, mem2, btc2, k=3, lookback=24)
    a = set(zip(r1[r1 < 150].tolist(), c1[r1 < 150].tolist(), strict=True))
    b = set(zip(r2[r2 < 150].tolist(), c2[r2 < 150].tolist(), strict=True))
    assert a == b and len(a) > 0
    assert (r2 >= 150).sum() == 0  # BTC filter blocks every later row


def test_momentum_loss_bar() -> None:
    t = np.arange(12) * H
    h = np.array([10, 11, 12, 13, 12.5, 12.4, 12.3, 12.05, 11, 10.5, 10, 9.5], dtype=float)
    low = h - 0.5
    c = h - 0.2
    # peak at bar 3; bars 3 .. 7 close above the lows of their 3 previous bars;
    # bar 8: close 10.8 < min(low[5:8]) = 11.55 and peak 3 <= 8 - 3 -> trigger 8
    assert bx.momentum_loss_bar(t, h, low, c, 0) == 8
    assert bx.momentum_loss_bar(t, h, low, c, 0, max_wait=7) == -1
    h2 = h.copy()
    h2[7] = 14.0  # a new high at bar 7: not stale until bar 10
    assert bx.momentum_loss_bar(t, h2, h2 - 0.5, c, 0) == 10
    t3 = t.copy()
    t3[5:] += H  # a hole before the trigger
    assert bx.momentum_loss_bar(t3, h, low, c, 0) == -1


def test_full_path_and_placebo_bars() -> None:
    t = np.concatenate([np.arange(100), np.arange(105, 200)]) * H
    assert bx.full_path(t, 0, 12) and not bx.full_path(t, 95, 12) and not bx.full_path(t, 194, 12)
    pb = bx.placebo_bars(t, np.array([50, 150]), 12, 200, 30, np.random.default_rng(3))
    assert pb.shape == (2, 200) and (pb >= 0).all()
    for row, jj in zip(pb, (50, 150), strict=True):
        assert all(abs(t[i] - t[jj]) <= 30 * H and bx.full_path(t, int(i), 12) for i in row)


def test_icc_oneway_bounds() -> None:
    v = np.repeat([1.0, 2.0, 3.0, 4.0], 5)
    assert bx.icc_oneway(v, np.repeat(np.arange(4), 5)) == pytest.approx(1.0)
    rng = np.random.default_rng(5)
    assert abs(bx.icc_oneway(rng.normal(size=4000), np.repeat(np.arange(400), 10))) < 0.05


def test_time_exit_through_xo4_outcomes() -> None:
    """Infinite level: stop or the 12 h time exit only; R on the live risk unit, both fees paid."""
    n = 14
    t = np.arange(n) * H
    o = np.full(n, 100.0)
    c = np.full(n, 100.0)
    c[12] = 103.0  # close of the 12th hold bar (bars 1 .. 12)
    h, low = np.maximum(o, c) + 0.1, np.minimum(o, c) - 0.1
    fees = Fees(maker=0.00075, taker_exit=0.00075)
    side = np.array([1])
    r, k, kind = x4.vwap_exit_outcomes(t, o, h, low, c, np.array([1]), side, np.array([100.0]), np.array([98.0]), -side * np.inf, 12, fees)
    ru = 2.0 + fees.risk_taker * 198.0
    assert k[0] == 12 and kind[0] == x4.KIND_TIME
    assert r[0] == pytest.approx((3.0 - 0.00075 * 100 - 0.00075 * 103) / ru)
