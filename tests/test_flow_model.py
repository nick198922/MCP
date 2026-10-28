"""Tests for analytics.flow_model (IDEA-20261028-market-flow-reader, step 3)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import flow_model as fm


def test_rolling_sum_and_nan():
    x = np.array([1.0, 2.0, 3.0, np.nan, 5.0, 6.0])
    s = fm.rolling_sum(x, 2)
    assert np.isnan(s[0])
    assert s[1] == 3.0 and s[2] == 5.0
    assert np.isnan(s[3]) and np.isnan(s[4])
    assert s[5] == 11.0


def test_rolling_max_min_median():
    x = np.array([3.0, 1.0, 4.0, 1.0, 5.0])
    assert fm.rolling_max(x, 3)[2:].tolist() == [4.0, 4.0, 5.0]
    assert fm.rolling_min(x, 3)[2:].tolist() == [1.0, 1.0, 1.0]
    med = fm.rolling_median(x, 3, min_count=2)
    assert med[2:].tolist() == [3.0, 1.0, 4.0]


def test_taker_imbalance_analytic():
    qv = np.array([100.0, 100.0, 100.0])
    tb = np.array([75.0, 25.0, 50.0])
    imb = fm.taker_imbalance(qv, tb, 1)
    assert imb.tolist() == pytest.approx([0.5, -0.5, 0.0])
    assert fm.taker_imbalance(qv, tb, 2)[1] == pytest.approx(0.0)


def test_asof_and_max_age():
    obs = np.array([10, 20, 30])
    v = np.array([1.0, 2.0, 3.0])
    out = fm.asof(obs, v, np.array([5, 10, 25, 100]), max_age_ms=20)
    assert np.isnan(out[0]) and out[1] == 1.0 and out[2] == 2.0 and np.isnan(out[3])


def test_funding_features_annualised():
    h = fm.H_MS
    t = np.array([8, 16, 24, 32]) * h
    rate = np.array([0.0001, 0.0002, 0.0003, 0.0004])
    ih = np.array([8.0, 8.0, 8.0, 0.0])  # 0 -> assumed 8 h
    dec = np.array([7, 8, 24, 100]) * h
    f = fm.funding_features(t, rate, ih, dec)
    assert np.isnan(f["fund_last"][0])
    assert f["fund_last"][1] == pytest.approx(0.0001 * 1095)
    assert f["fund_last"][2] == pytest.approx(0.0003 * 1095)
    assert f["fund_3d"][2] == pytest.approx(0.0002 * 1095)  # mean of the three settlements in (−72h, 24h]
    assert np.isnan(f["fund_last"][3])  # last settlement 68 h old > 26 h
    f2 = fm.funding_features(t, rate, ih, np.array([40 * h]))
    assert f2["fund_last"][0] == pytest.approx(0.0004 * 1095)  # interval 0 -> 8 h assumed


def test_forward_targets():
    c = np.exp(np.array([0.0, 0.1, 0.3, 0.6]))
    fr = fm.forward_logret(c, 2)
    assert fr[0] == pytest.approx(0.3) and fr[1] == pytest.approx(0.5) and np.isnan(fr[2])
    lrv = fm.forward_log_rv(c, 2)
    assert lrv[0] == pytest.approx(math.log(math.sqrt(0.1**2 + 0.2**2)))
    assert lrv[1] == pytest.approx(math.log(math.sqrt(0.2**2 + 0.3**2)))
    assert np.isnan(lrv[2])


def test_coinbase_premium_constant_offset_is_zero():
    sp = np.full(100, 100.0)
    cb = np.full(100, 100.5)
    prem = fm.coinbase_premium(cb, sp, med_w=10, mean_w=5)
    assert np.nanmax(np.abs(prem)) < 1e-12
    assert np.isfinite(prem[-1])


def test_range_exit_events_sides():
    n = 60
    hi = np.full(n, 101.0)
    lo = np.full(n, 99.0)
    hi[45] = 105.0  # breaks up after origin 40
    ev = fm.range_exit_events(hi, lo, [10, 20, 30, 40], lookback=5, horizon=10, min_prior=2)
    assert [(e.i, e.up, e.resolve_i) for e in ev] == [(40, 1, 45)]  # 10, 20 lack prior widths; 30: no exit
    lo2 = lo.copy()
    lo2[45] = 90.0
    ev2 = fm.range_exit_events(hi, lo2, [10, 20, 30, 40], lookback=5, horizon=10, min_prior=2)
    assert ev2 == []  # both sides in one bar -> dropped


def test_auc_ties_and_perfect():
    y = np.array([0, 0, 1, 1])
    assert fm.auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert fm.auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    assert fm.auc(y, np.array([0.1, 0.4, 0.35, 0.8])) == pytest.approx(0.75)


def test_logistic_recovers_sign_and_ridge_ols():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(4000, 2))
    p = 1 / (1 + np.exp(-(1.5 * X[:, 0] - 0.0 * X[:, 1] + 0.3)))
    y = (rng.random(4000) < p).astype(float)
    w = fm.fit_logistic(X, y, lam=0.0)
    assert w[0] == pytest.approx(1.5, abs=0.15) and abs(w[1]) < 0.1 and w[2] == pytest.approx(0.3, abs=0.1)
    yr = 2.0 * X[:, 0] - 1.0 * X[:, 1] + 5.0
    wr = fm.fit_ridge(X, yr, lam=0.0)
    assert wr.tolist() == pytest.approx([2.0, -1.0, 5.0], abs=1e-9)


def test_gb_learns_interaction():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(6000, 3))
    y = ((X[:, 0] > 0) ^ (X[:, 1] > 0)).astype(float)  # XOR: invisible to a linear model
    m = fm.fit_gb(X, y, loss="logistic", rounds=60, lr=0.3, min_leaf=50)
    assert fm.auc(y, m.predict(X)) > 0.9
    wl = fm.fit_logistic(X, y)
    assert fm.auc(y, fm.predict_logistic(wl, X)) < 0.6


def test_walk_forward_uses_only_resolved_rows_and_past_is_stable():
    """Predictions for a month do not change when data after that month changes (no look-ahead)."""
    rng = np.random.default_rng(2)
    n = 400
    t = np.arange(n, dtype=np.int64) * 10
    res = t + 5
    X = rng.normal(size=(n, 2))
    y = (X[:, 0] + rng.normal(size=n) > 0).astype(float)
    months = [2000, 3000]
    fit, pr = (lambda A, b: fm.fit_logistic(A, b)), fm.predict_logistic
    p1 = fm.walk_forward(X, y, t, res, months, fit, pr)
    X2, y2 = X.copy(), y.copy()
    X2[300:] = rng.normal(size=(100, 2)) * 50
    y2[300:] = 1 - y2[300:]
    p2 = fm.walk_forward(X2, y2, t, res, months, fit, pr)
    first = (t >= 2000) & (t < 3000)
    assert np.all(np.isfinite(p1[first])) and np.allclose(p1[first], p2[first])
    assert np.all(np.isnan(p1[t < 2000]))


def test_features_causal():
    """Features at row i do not change when bars after i change."""
    rng = np.random.default_rng(3)
    n = 1000
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    h, lo = c * 1.002, c * 0.998
    qv = rng.uniform(50, 150, n)
    tb = qv * rng.uniform(0.3, 0.7, n)
    oi = rng.uniform(1e4, 2e4, n)
    i0 = 800

    def feats(c, h, lo, qv, tb, oi):
        p = fm.price_features(c, h, lo)
        f = fm.flow_features(
            perp_qv=qv,
            perp_tbq=tb,
            spot_qv=qv,
            spot_tbq=tb,
            oi=oi,
            fund_last=np.zeros(n),
            fund_3d=np.zeros(n),
            premium=fm.coinbase_premium(c * 1.001, c, med_w=50, mean_w=24),
            r24=p["r24"],
        )
        return np.hstack([np.stack(list(p.values()), 1), np.stack(list(f.values()), 1), np.stack(list(fm.har_features(c).values()), 1)])

    a = feats(c, h, lo, qv, tb, oi)
    c2, h2, l2, q2, t2, o2 = (x.copy() for x in (c, h, lo, qv, tb, oi))
    for x in (c2, h2, l2, q2, t2, o2):
        x[i0 + 1 :] *= 1.7
    b = feats(c2, h2, l2, q2, t2, o2)
    assert np.allclose(a[: i0 + 1], b[: i0 + 1], equal_nan=True)
    assert not np.allclose(a[i0 + 30 :], b[i0 + 30 :], equal_nan=True)


def test_block_bootstrap_and_holm():
    g = np.repeat(np.arange(20), 5)
    vals = np.ones(100)
    r = fm.block_bootstrap_delta(g, lambda ix: float(vals[ix].mean()), reps=200)
    assert r["point"] == 1.0 and r["lo"] == 1.0 and r["p_le0"] == 0.0 and r["n_groups"] == 20
    assert fm.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def test_month_starts_and_shift():
    from datetime import UTC, datetime

    a = int(datetime(2025, 1, 15, tzinfo=UTC).timestamp() * 1000)
    b = int(datetime(2025, 3, 1, tzinfo=UTC).timestamp() * 1000)
    ms = fm.month_starts(a, b)
    assert [datetime.fromtimestamp(x / 1000, UTC).month for x in ms] == [2, 3]
    assert fm.circular_shift(np.arange(5), 2).tolist() == [3, 4, 0, 1, 2]
