"""Tests for analytics.iv_har (IDEA-20261023-daily-iv-vs-intraday-vol-forecast): analytic expectations, no network."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.iv_har import (
    BAR_S,
    DAY_S,
    annualize,
    atm_iv_prints,
    bar_returns,
    cluster_bootstrap_diff,
    har_features,
    har_fit,
    har_forecast_var,
    huber_cluster,
    iv_summary,
    ols_cluster,
    realized_var,
    shuffle_within,
    wing_strikes,
)
from tradedesk.analytics.put_skew import inverse_price, year_frac

T0 = int(datetime(2023, 5, 10, 9, tzinfo=UTC).timestamp())


def alternating(lo: int, hi: int, x: float = 0.001, p0: float = 30_000.0) -> dict[int, float]:
    """Closes keyed by bar open time whose consecutive log returns alternate +x, -x."""
    out, p, s = {}, p0, 1
    for t in range(lo, hi, BAR_S):
        p *= math.exp(s * x)
        s = -s
        out[t] = p
    return out


def test_bar_returns_constant_growth():
    closes = {t: 100.0 * math.exp(0.002 * i) for i, t in enumerate(range(0, 10 * BAR_S, BAR_S))}
    rets = bar_returns(closes, BAR_S, 10 * BAR_S)
    assert len(rets) == 9
    assert all(r == pytest.approx(0.002) for r in rets)


def test_realized_var_alternating_and_missing_bars_scaled():
    closes = alternating(T0 - 2 * DAY_S, T0 + DAY_S)
    rv = realized_var(closes, T0 - DAY_S, T0)
    assert rv == pytest.approx(288 * 0.001**2)
    # drop 5 % of bars: scaled back to the full window, still the same
    thin = {t: c for i, (t, c) in enumerate(sorted(closes.items())) if i % 40 != 7}
    assert realized_var(thin, T0 - DAY_S, T0) == pytest.approx(288 * 0.001**2)
    # drop half: below 90 % coverage -> None
    half = {t: c for i, (t, c) in enumerate(sorted(closes.items())) if i % 2}
    assert realized_var(half, T0 - DAY_S, T0) is None


def test_har_features_daily_scaled():
    closes = alternating(T0 - 31 * DAY_S, T0 + DAY_S, x=0.002)
    f = har_features(closes, T0)
    assert f is not None
    for k in ("rv_d", "rv_w", "rv_m"):
        assert f[k] == pytest.approx(288 * 0.002**2)


def test_har_features_no_lookahead():
    """Bars opened at or after t0 never change the features; the bar closing exactly at t0 does."""
    closes = alternating(T0 - 31 * DAY_S, T0 + DAY_S)
    base = har_features(closes, T0)
    future = dict(closes)
    for t in range(T0, T0 + DAY_S, BAR_S):
        future[t] = closes[t] * 3.0
    assert har_features(future, T0) == base
    last = dict(closes)
    last[T0 - BAR_S] *= 1.05  # bar 08:55-09:00 closes at 09:00: allowed and used
    assert har_features(last, T0)["rv_d"] != base["rv_d"]


def test_har_fit_recovers_coefficients():
    rng = random.Random(1)
    feats, ys = [], []
    for _ in range(200):
        f = {"rv_d": math.exp(rng.gauss(-7, 1)), "rv_w": math.exp(rng.gauss(-7, 1)), "rv_m": math.exp(rng.gauss(-7, 1))}
        feats.append(f)
        ys.append(math.exp(-1.0 + 0.4 * math.log(f["rv_d"]) + 0.3 * math.log(f["rv_w"]) + 0.2 * math.log(f["rv_m"])))
    fit = har_fit(feats, ys)
    assert fit["coef"] == pytest.approx([-1.0, 0.4, 0.3, 0.2], abs=1e-9)
    assert fit["s2"] == pytest.approx(0.0, abs=1e-12)
    assert har_forecast_var(fit, feats[0]) == pytest.approx(ys[0])


def test_annualize():
    assert annualize(0.0001, 24.0) == pytest.approx(math.sqrt(0.0365))
    assert annualize(0.0001 * 23 / 24, 23.0) == pytest.approx(math.sqrt(0.0365))


def _quote():
    day = int(datetime(2023, 5, 10, tzinfo=UTC).timestamp() * 1000)
    return {"t0": day + 9 * 3_600_000, "expiry": day + 32 * 3_600_000, "K": 27_500.0, "ratio": 1.0}


def _print(q, ts, K, call, iv, idx=27_500.0):
    T = year_frac(q["expiry"], ts)
    px = inverse_price(idx, K, T, iv, call)
    name = f"BTC-11MAY23-{int(K)}-{'C' if call else 'P'}"
    return {"instrument_name": name, "timestamp": ts, "index_price": idx, "mark_price": px, "price": px, "iv": iv * 100}


def test_atm_iv_prints_recovers_iv_and_ignores_after_t0():
    q = _quote()
    t0 = q["t0"]
    trades = [
        _print(q, t0 - 90 * 60_000, 27_500.0, True, 0.40),
        _print(q, t0 - 30 * 60_000, 27_500.0, False, 0.50),
        _print(q, t0 - 3 * 3_600_000, 27_500.0, True, 0.90),  # before the window
        _print(q, t0 - 10 * 60_000, 28_000.0, True, 0.70),  # other strike
    ]
    s = iv_summary(atm_iv_prints(trades, q))
    assert s["n_iv"] == 2
    assert s["iv_mean"] == pytest.approx(0.45, abs=1e-4)
    assert s["iv_last"] == pytest.approx(0.50, abs=1e-4)
    assert s["iv_trade_mean"] == pytest.approx(0.45)
    later = trades + [_print(q, t0 + 60_000, 27_500.0, True, 2.0)]
    assert iv_summary(atm_iv_prints(later, q)) == s
    assert iv_summary([]) is None


def test_wing_strikes():
    q = _quote()
    t0 = q["t0"]
    trades = [_print(q, t0 - 60_000, k, c, 0.5) for k, c in ((27_750.0, True), (28_000.0, True), (27_250.0, False), (29_000.0, True))]
    w = wing_strikes(trades, q)
    assert w["K_call"] == 27_750.0 and w["K_put"] == 27_250.0 and w["covered"]
    assert not wing_strikes(trades[:2], q)["covered"]


def test_ols_cluster_hand_computed_cr1():
    # y on [1, x], x = 0/1 inside each of 3 clusters of 2 rows.
    # beta = (1, 2); residuals (0, 0, 1, -1, -1, 1); cluster scores X_g' e_g = (0,0), (0,-1), (0,1)
    # meat = [[0,0],[0,2]]; (X'X)^-1 = [[1/3,-1/3],[-1/3,2/3]] -> bread.meat.bread = 2 * v v', v = (-1/3, 2/3)
    # CR1 factor G/(G-1) * (n-1)/(n-k) = 3/2 * 5/4 = 15/8 -> var = (5/12, 5/3); R^2 = 1 - 4/10.
    X = [[1.0, 0.0], [1.0, 1.0]] * 3
    y = [1.0, 3.0, 2.0, 2.0, 0.0, 4.0]
    cl = ["A", "A", "B", "B", "C", "C"]
    r = ols_cluster(y, X, cl)
    assert r["coef"] == pytest.approx([1.0, 2.0], abs=1e-12)
    assert r["se"][0] == pytest.approx(math.sqrt(5.0 / 12.0), rel=1e-12)
    assert r["se"][1] == pytest.approx(math.sqrt(5.0 / 3.0), rel=1e-12)
    assert r["p"][1] == pytest.approx(math.erfc(2.0 / math.sqrt(5.0 / 3.0) / math.sqrt(2.0)), rel=1e-12)
    assert r["r2"] == pytest.approx(0.6, abs=1e-12)
    assert (r["n"], r["clusters"]) == (6, 3)
    # uniform weights leave the WLS sandwich unchanged
    rw = ols_cluster(y, X, cl, w=[2.0] * 6)
    assert rw["se"] == pytest.approx(r["se"], rel=1e-12)


def test_ols_cluster_exact_and_noise():
    rng = random.Random(7)
    X, y, cl = [], [], []
    for i in range(400):
        a, b = rng.gauss(0, 1), rng.gauss(0, 1)
        X.append([1.0, a, b])
        y.append(0.5 + 1.0 * a + 0.0 * b + rng.gauss(0, 0.5))
        cl.append(str(i // 5))
    r = ols_cluster(y, X, cl)
    assert r["coef"][1] == pytest.approx(1.0, abs=0.1)
    assert r["p"][1] < 1e-6
    assert abs(r["coef"][2]) < 4 * r["se"][2]
    # homoskedastic iid: cluster SE close to the classical sigma/sqrt(n)
    assert r["se"][1] == pytest.approx(0.5 / math.sqrt(400), rel=0.35)


def test_huber_resists_outliers():
    rng = random.Random(3)
    X, y, cl = [], [], []
    for i in range(300):
        a = rng.gauss(0, 1)
        X.append([1.0, a])
        y.append(2.0 * a + rng.gauss(0, 0.1))
        cl.append(str(i))
    for i in range(10):
        y[i] = 50.0 * X[i][1] + 30.0
    ols = ols_cluster(y, X, cl)
    hub = huber_cluster(y, X, cl)
    assert abs(hub["coef"][1] - 2.0) < 0.1 < abs(ols["coef"][1] - 2.0)


def test_cluster_bootstrap_diff():
    rows = [(str(i), i % 3 == 0, 1.0 if i % 3 == 0 else 0.0) for i in range(90)]
    r = cluster_bootstrap_diff(rows, n_boot=500)
    assert r["diff"] == pytest.approx(1.0)
    assert r["ci95"][0] == pytest.approx(1.0) and r["ci95"][1] == pytest.approx(1.0)
    assert r["n_in"] == 30 and r["n_out"] == 60


def test_shuffle_within_keeps_block_multisets():
    blocks = ["a", "a", "b", "b", "b"]
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    out = shuffle_within(blocks, vals, random.Random(0))
    assert sorted(out[:2]) == [1.0, 2.0] and sorted(out[2:]) == [3.0, 4.0, 5.0]
