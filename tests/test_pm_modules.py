"""Analytic references for analytics/pm_modules.py (Polymarket modules H11, H7, H8)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics import pm_modules as pm
from tradedesk.analytics.pm_threshold import HOUR_S, digital_above


def _ts(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp())


# ----------------------------------------------------------------------------- H11


def test_ladder_sigma_recovers_lognormal_s() -> None:
    s0, s, m = 100_000.0, 0.02, -0.0002
    ks = [96_000.0, 98_000.0, 100_000.0, 102_000.0, 104_000.0]
    qs = [pm._N.cdf((math.log(s0 / k) + m) / s) for k in ks]
    fit = pm.ladder_sigma(ks, qs, s0)
    assert fit is not None
    assert fit["s"] == pytest.approx(s, rel=1e-9)
    assert fit["alpha"] == pytest.approx(m / s, rel=1e-9)
    assert fit["n"] == 3  # 96k / 104k have q outside [0.03, 0.97]
    wide = pm.ladder_sigma(ks, qs, s0, lo=0.01, hi=0.99)
    assert wide is not None and wide["n"] == 5 and wide["s"] == pytest.approx(s, rel=1e-9)


def test_ladder_sigma_filters_and_rejects() -> None:
    s0 = 100.0
    # only one strike inside [0.03, 0.97] -> None
    assert pm.ladder_sigma([90.0, 100.0, 110.0], [0.99, 0.5, 0.01], s0) is None
    # increasing q with strike (beta <= 0) -> None
    assert pm.ladder_sigma([95.0, 105.0], [0.3, 0.7], s0) is None
    assert pm.ladder_sigma([95.0, 105.0], [0.7, 0.3], s0, min_n=3) is None


def test_qlike_zero_at_truth_and_positive() -> None:
    assert pm.qlike(0.0004, 0.0004) == pytest.approx(0.0)
    assert pm.qlike(0.0004, 0.0002) == pytest.approx(2 - math.log(2) - 1)
    with pytest.raises(ValueError):
        pm.qlike(0.0, 1.0)


def test_fit_quality_ok_symmetric_sigma_band() -> None:
    v_ew = 0.0004
    assert pm.fit_quality_ok(v_ew, v_ew)
    assert pm.fit_quality_ok(9 * v_ew, v_ew)  # sigma exactly 3x: inclusive
    assert pm.fit_quality_ok(v_ew / 9, v_ew)
    assert not pm.fit_quality_ok(9.01 * v_ew, v_ew)
    assert not pm.fit_quality_ok(v_ew / 9.01, v_ew)
    assert not pm.fit_quality_ok(0.0, v_ew)
    assert pm.fit_quality_ok(3.9 * v_ew, v_ew, ratio=2.0) and not pm.fit_quality_ok(4.1 * v_ew, v_ew, ratio=2.0)


def test_projected_mde_and_n_for_mde() -> None:
    # SE scales as 1/sqrt(n): 4x the units halves the MDE
    assert pm.projected_mde(0.02, 100, 100) == pytest.approx(0.056)
    assert pm.projected_mde(0.02, 100, 400) == pytest.approx(0.028)
    assert pm.projected_mde(0.02, 100, 0) is None
    # analytic inverse: n = n_ref * (k * se / target)^2
    assert pm.n_for_mde(0.02, 100, 0.028) == 400
    assert pm.n_for_mde(0.0264, 737, 0.06) == math.ceil(737 * (2.8 * 0.0264 / 0.06) ** 2)
    n = pm.n_for_mde(0.0264, 737, 0.06)
    assert pm.projected_mde(0.0264, 737, n) <= 0.06 < pm.projected_mde(0.0264, 737, n - 1)
    with pytest.raises(ValueError):
        pm.n_for_mde(0.02, 100, 0.0)


def test_realized_var_window_and_coverage() -> None:
    t0 = _ts(2026, 1, 5, 4)
    closes = [(t0 + i * HOUR_S, 100.0 * math.exp(0.01 * (i % 2))) for i in range(-2, 14)]
    rv = pm.realized_var(closes, t0, t0 + 12 * HOUR_S)
    assert rv == pytest.approx(12 * 0.01**2)
    gap = [c for c in closes if c[0] != t0 + 5 * HOUR_S]
    assert pm.realized_var(gap, t0, t0 + 12 * HOUR_S) is None


# ----------------------------------------------------------------------------- H7


def test_is_weekend_bounds() -> None:
    assert not pm.is_weekend(_ts(2026, 10, 30, 20, 59))  # Fri
    assert pm.is_weekend(_ts(2026, 10, 30, 21))
    assert pm.is_weekend(_ts(2026, 10, 31, 12))  # Sat
    assert pm.is_weekend(_ts(2026, 11, 1, 20, 59))  # Sun
    assert not pm.is_weekend(_ts(2026, 11, 1, 21))


def test_profile_normalised_and_shape() -> None:
    start = _ts(2024, 1, 1)
    rets = []
    for i in range(24 * 7 * 8):
        t = start + i * HOUR_S
        h, wk = pm.cell_of(t)
        rets.append((t, (2.0 if h == 14 else 1.0) * (0.5 if wk else 1.0) * 0.001))
    prof = pm.intraday_profile(rets)
    week = sum(pm.weight_at(prof, start + i * HOUR_S) for i in range(168)) / 168
    assert week == pytest.approx(1.0)
    # ratio of |r| 2 -> variance ratio 4; weekend |r| x0.5 -> variance x0.25
    assert prof[(14, False)] / prof[(3, False)] == pytest.approx(4.0)
    assert prof[(3, True)] / prof[(3, False)] == pytest.approx(0.25)
    assert pm.window_weight({c: 1.0 for c in prof}, start, start + 12 * HOUR_S) == pytest.approx(12.0)


def test_ewma_flat_profile_matches_plain_and_ignores_future() -> None:
    t0 = _ts(2026, 3, 1)
    closes = [(t0 - (200 - i) * HOUR_S, 100.0 * math.exp(0.002 * math.sin(i))) for i in range(201)]
    flat = {(h, w): 1.0 for h in range(24) for w in (False, True)}
    a = pm.ewma_hourly_var(closes, t0)
    b = pm.ewma_hourly_var(closes, t0, profile=flat)
    assert a == pytest.approx(b)
    later = closes + [(t0 + HOUR_S, 500.0), (t0 + 2 * HOUR_S, 1.0)]
    assert pm.ewma_hourly_var(later, t0) == a
    # constant returns: EWMA equals r^2; a profile weight of 4 on every bar divides it by 4
    const = [(t0 - (100 - i) * HOUR_S, 100.0 * math.exp(0.01 * i)) for i in range(101)]
    assert pm.ewma_hourly_var(const, t0) == pytest.approx(1e-4)
    four = {c: 4.0 for c in flat}
    assert pm.ewma_hourly_var(const, t0, profile=four) == pytest.approx(0.25e-4)


def test_prob_from_var_matches_digital() -> None:
    v = 0.02**2
    assert pm.prob_from_var(100.0, 101.0, v) == pytest.approx(digital_above(100.0, 101.0, 0.02, 1.0))
    assert pm.prob_from_var(100.0, 99.0, 0.0) == 1.0


def test_brier_logloss() -> None:
    assert pm.brier(0.3, 1) == pytest.approx(0.49)
    assert pm.log_loss(0.5, 0) == pytest.approx(math.log(2))
    assert pm.log_loss(0.0, 1) == pytest.approx(-math.log(1e-4))


# ----------------------------------------------------------------------------- H8


def test_shift_delta_zero_and_first_order() -> None:
    f, k, iv, tau = 100_000.0, 101_000.0, 0.5, 1.0 / 365
    assert pm.shift_delta("above", f, k, iv, tau, 0.0) == 0.0
    g = 1e-4
    s = iv * math.sqrt(tau)
    d2 = (math.log(f / k) - 0.5 * s * s) / s
    analytic = math.exp(-0.5 * d2 * d2) / math.sqrt(2 * math.pi) * g / s
    assert pm.shift_delta("above", f, k, iv, tau, g) == pytest.approx(analytic, rel=1e-3)
    # ATM, 1 day, IV 50 %: 0.1 % shift ~ 1.5 p.p.
    assert pm.shift_delta("above", f, f, iv, tau, 0.001) == pytest.approx(0.0152, abs=0.001)
    # touch: higher forward -> 'up' more likely, 'down' less likely
    assert pm.shift_delta("touch", f, 105_000.0, iv, tau, 0.001, "up") > 0
    assert pm.shift_delta("touch", f, 95_000.0, iv, tau, 0.001, "down") < 0
    with pytest.raises(ValueError):
        pm.shift_delta("touch", f, k, iv, tau, 0.001)


# ----------------------------------------------------------------------------- statistics


def test_block_bootstrap_ratio() -> None:
    r = pm.block_bootstrap_ratio([(1.0, 1), (3.0, 1), (2.0, 2)], n_boot=2000, seed=1)
    assert r["mean"] == pytest.approx(1.5)
    assert 0 < r["lo"] <= r["mean"] <= r["hi"]
    assert r["p"] == 0.0
    z = pm.block_bootstrap_ratio([(1.0, 1), (-1.0, 1)], n_boot=2000, seed=1)
    assert z["mean"] == 0.0 and z["p"] > 0.5
    assert pm.block_bootstrap_ratio([])["mean"] is None
    lr = pm.block_bootstrap_logratio([(2.0, 1.0), (4.0, 2.0), (2.2, 1.0)], n_boot=2000, seed=1)
    assert lr["mean"] == pytest.approx(math.log(8.2 / 4.0))
    assert lr["lo"] > 0 and lr["p"] == 0.0


def test_cochran_q_and_holm() -> None:
    same = pm.cochran_q([0.1, 0.1, 0.1], [0.05, 0.05, 0.05])
    assert same["q"] == pytest.approx(0.0) and same["p"] == pytest.approx(1.0)
    diff = pm.cochran_q([0.0, 1.0], [0.1, 0.1])
    assert diff["q"] == pytest.approx(50.0)
    assert diff["p"] < 1e-6 and diff["i2"] == pytest.approx(49 / 50)
    # chi2(2) sf(x) = exp(-x/2)
    assert pm._chi2_sf(3.0, 2) == pytest.approx(math.exp(-1.5), rel=1e-9)
    h = pm.holm({"a": 0.01, "b": 0.03, "c": 0.04})
    assert h["a"]["reject"] and not h["b"]["reject"] and not h["c"]["reject"]
    h2 = pm.holm({"a": 0.01, "b": 0.02, "c": 0.04})
    assert all(x["reject"] for x in h2.values())
