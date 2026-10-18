import math

import numpy as np
import pytest

from tradedesk.analytics.risk_overlay import (
    block_indices,
    block_permute,
    bootstrap,
    card_status,
    cochran_q,
    cvar,
    donchian_fraction,
    donchian_state,
    drifted,
    ewma_vol,
    h1_verdict,
    holm,
    log_returns,
    max_drawdown,
    net_returns,
    period_returns,
    placebo_p,
    positions,
    rolling_vol,
    sharpe,
    sr_verdict,
    vol_target_signal,
)


def _pipeline(closes: np.ndarray, lag: int = 2) -> np.ndarray:
    r = log_returns(closes)
    s1 = vol_target_signal(rolling_vol(r, 60, min_n=30), ewma_vol(r, 0.94), cap=1.0)
    s2 = donchian_fraction(closes, (5, 10, 20)) * s1
    return positions(s2, lag)


def test_log_returns_and_ewma_constant_abs_return():
    c = np.exp(np.cumsum(np.r_[0.0, np.tile([0.02, -0.02], 50)]))
    r = log_returns(c)
    assert math.isnan(r[0])
    assert r[1] == pytest.approx(0.02)
    v = ewma_vol(r, 0.94, init_n=20)
    assert np.all(np.isnan(v[:20]))
    # |r| = 0.02 every day -> EWMA of r^2 stays 0.0004 -> annualised 0.02 * sqrt(365)
    assert v[20:] == pytest.approx(np.full(v.size - 20, 0.02 * math.sqrt(365)))


def test_ewma_recursion_one_step():
    r = np.r_[np.nan, np.full(20, 0.01), 0.05]
    v = ewma_vol(r, 0.9, init_n=20)
    expected = math.sqrt(365 * (0.9 * 0.0001 + 0.1 * 0.0025))
    assert v[21] == pytest.approx(expected)


def test_rolling_vol_alternating_returns():
    r = np.r_[np.nan, np.tile([0.01, -0.01], 100)]
    v = rolling_vol(r, 10, min_n=10)
    assert np.all(np.isnan(v[:10]))
    # 10 alternating values: mean 0, sample variance = 10 * 1e-4 / 9
    assert v[10] == pytest.approx(math.sqrt(1e-4 * 10 / 9 * 365))
    ex = rolling_vol(r, None, min_n=2)
    assert ex[200] == pytest.approx(math.sqrt(1e-4 * 200 / 199 * 365))


def test_vol_target_signal_clip_and_nan():
    s = vol_target_signal(0.6, np.array([0.3, 0.6, 1.2, np.nan, 0.0]), cap=1.0)
    assert s[:3] == pytest.approx([1.0, 1.0, 0.5])
    assert math.isnan(s[3]) and math.isnan(s[4])
    assert vol_target_signal(0.6, np.array([0.3]), cap=1.5)[0] == pytest.approx(1.5)


def test_donchian_states():
    up = np.arange(1.0, 31.0)
    st = donchian_state(up, 5)
    assert np.all(np.isnan(st[:5])) and np.all(st[5:] == 1.0)
    down = up[::-1]
    assert np.all(donchian_state(down, 5)[5:] == 0.0)
    # breakout up then flat inside the channel keeps the state, a break below the low resets it
    c = np.array([5, 5, 5, 5, 5, 6, 5.5, 5.5, 5.5, 5.5, 5.5, 4.0])
    st = donchian_state(c, 5)
    assert list(st[5:]) == [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0]
    f = donchian_fraction(up, (5, 10))
    assert math.isnan(f[7]) and f[10] == 1.0


def test_positions_lag():
    w = positions(np.array([0.1, 0.2, 0.3, 0.4]), 2)
    assert math.isnan(w[0]) and math.isnan(w[1])
    assert list(w[2:]) == [0.1, 0.2]


def test_no_lookahead_weights_before_t_unchanged():
    rng = np.random.default_rng(1)
    c = np.exp(np.cumsum(rng.normal(0, 0.03, 400)))
    base = _pipeline(c)
    t = 250
    c2 = c.copy()
    c2[t - 1 :] *= np.exp(rng.normal(0, 0.2, c.size - t + 1))  # data from close t-1 on changes
    alt = _pipeline(c2)
    # weight on day j uses closes up to j - 2, so days <= t are untouched
    np.testing.assert_array_equal(base[: t + 1], alt[: t + 1])
    assert not np.allclose(base[t + 1 :], alt[t + 1 :], equal_nan=True)


def test_net_returns_constant_full_weight_has_only_entry_cost():
    R = np.array([0.1, -0.05, 0.02])
    out = net_returns(np.ones(3), R, fee=0.001)
    assert out["ret"] == pytest.approx(R - np.array([0.001, 0.0, 0.0]))
    assert out["turnover"] == pytest.approx([1.0, 0.0, 0.0])


def test_net_returns_drift_cost_half_weight():
    R = np.array([0.1, 0.0])
    out = net_returns(np.array([0.5, 0.5]), R, fee=0.01)
    wd = 0.5 * 1.1 / 1.05
    assert out["turnover"][1] == pytest.approx(wd - 0.5)
    assert out["ret"][0] == pytest.approx(0.05 - 0.005)
    assert out["ret"][1] == pytest.approx(-0.01 * (wd - 0.5))
    assert drifted(np.array(0.5), np.array(0.1)) == pytest.approx(wd)


def test_net_returns_perp_leg_pays_funding_and_perp_fee():
    out = net_returns(np.array([1.5]), np.array([0.0]), fee=0.001, perp_fee=0.0005, funding=np.array([0.0003]))
    assert out["ret"][0] == pytest.approx(-(0.001 * 1.0 + 0.0005 * 0.5 + 0.5 * 0.0003))


def test_net_returns_band_skips_small_moves():
    out = net_returns(np.array([0.5, 0.55, 0.8]), np.zeros(3), fee=0.001, band=0.1, w0=0.0)
    assert list(out["held"]) == pytest.approx([0.5, 0.5, 0.8])


def test_metrics():
    assert max_drawdown([0.1, -0.5, 0.2]) == pytest.approx(0.5)
    assert max_drawdown([0.1, 0.1]) == 0.0
    assert cvar(np.arange(20.0)) == pytest.approx(0.0)  # worst 1 of 20 -> -0
    assert cvar([-0.3, -0.1, 0.2, 0.5], q=0.5) == pytest.approx(0.2)
    x = np.tile([0.01, -0.005], 50)
    assert sharpe(x) == pytest.approx(x.mean() / x.std(ddof=1) * math.sqrt(365))
    k, v = period_returns([0.1, 0.1, -0.5, 0.2], ["a", "a", "b", "c"])
    assert k == ["a", "b", "c"]
    assert v == pytest.approx([0.21, -0.5, 0.2])


def test_bootstrap_and_block_helpers():
    rng = np.random.default_rng(0)
    idx = block_indices(23, 5, rng)
    assert idx.size == 23 and idx.max() < 23
    x = np.ones(100) * 0.5
    b = bootstrap(100, lambda i: float(x[i].mean()), block=10, reps=200)
    assert b["stat"] == pytest.approx(0.5) and b["ci95"] == pytest.approx((0.5, 0.5))
    assert b["p"] == pytest.approx(1 / 201)
    perm = block_permute(np.arange(10), 3, np.random.default_rng(2))
    assert sorted(perm.tolist()) == list(range(10))
    assert placebo_p(1.0, [0.0, 2.0, 0.5, 1.0]) == pytest.approx(3 / 5)


def test_holm_and_cochran():
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    q = cochran_q([0.2, 0.2], [0.1, 0.1])
    assert q["q"] == pytest.approx(0.0) and q["i2"] == 0.0 and q["pooled"] == pytest.approx(0.2)
    q2 = cochran_q([0.0, 0.3], [0.1, 0.1])
    assert q2["q"] == pytest.approx(4.5)  # 2 * (0.15 / 0.1)^2
    assert q2["i2"] == pytest.approx((4.5 - 1) / 4.5)


def test_verdict_rules():
    assert h1_verdict([True, True], [0.2, 0.1], [0.01, 0.02], [0.03, 0.01]) == "✓"
    assert h1_verdict([True, False], [0.2, 0.1], [0.01, 0.02], [0.03, 0.01]) == "≈"
    assert h1_verdict([True, True], [0.2, 0.1], [0.2, 0.02], [0.03, 0.01]) == "≈"
    assert h1_verdict([False, False], [0.2, 0.1], [0.01, 0.02], [0.03, 0.01]) == "✗"
    assert sr_verdict(0.2, (-0.1, 0.5), 0.1, 0.01, 0.5) == "◯"
    assert sr_verdict(0.2, (0.05, 0.5), 0.1, 0.01, 0.25) == "✓"
    assert sr_verdict(0.05, (-0.1, 0.2), 0.1, 0.01, 0.25) == "✗"
    assert sr_verdict(0.2, (-0.05, 0.4), 0.1, 0.01, 0.25) == "≈"
    kw = {"btc_placebo": [0.01, 0.01], "btc_oos": [0.1, 0.1], "btc_points": [0.1, 0.1], "eth_points": [0.1, 0.1]}
    assert card_status("✓", "≈", **kw) == "confirmed"
    assert card_status("≈", "✓", **kw) == "inconclusive"
    assert card_status("✗", "✓", **kw) == "rejected"
    assert card_status("✓", "✓", **{**kw, "eth_points": [-0.1, -0.1]}) == "rejected"
    assert card_status("✓", "✓", **{**kw, "btc_placebo": [0.2, 0.3]}) == "rejected"
