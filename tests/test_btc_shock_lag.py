import math

import numpy as np
import pytest

from tradedesk.analytics.btc_shock_lag import (
    FIVE_MIN_MS,
    HOUR_MS,
    adverse_excursion,
    beta_from_closes,
    beta_window,
    cluster_se,
    detect_shocks,
    directional_residual,
    lag_return,
    max_abs_log_jump,
    mde,
    placebo_times,
    post_entry_return,
    prev_window_std,
    tercile,
    winsorize,
)


def test_post_entry_return_exact_bars():
    o = np.arange(1.0, 41.0)  # o[k] = k + 1
    # event bar 10, entry at the open of bar 12 (5 min delay), exit 12 bars later at the open of bar 24
    assert post_entry_return(o, 10, 2, 12) == pytest.approx(25.0 / 13.0 - 1)
    assert post_entry_return(o, 10, 1, 12) == pytest.approx(24.0 / 12.0 - 1)
    assert math.isnan(post_entry_return(o, 30, 2, 12))  # exit off the array
    o2 = o.copy()
    o2[12] = np.nan
    assert math.isnan(post_entry_return(o2, 10, 2, 12))
    with pytest.raises(ValueError):
        post_entry_return(o, 10, 0, 12)  # the open of bar i is before the signal


def test_post_entry_return_reads_only_entry_and_exit():
    rng = np.random.default_rng(1)
    o = 100 + rng.random(60)
    base = post_entry_return(o, 20, 2, 12)
    o2 = o.copy()
    o2[:22] = rng.random(22) * 1e3  # everything up to and including the event bar and bar i+1
    o2[35:] = rng.random(25) * 1e3  # everything after the exit bar
    o2[23:34] = rng.random(11) * 1e3  # bars strictly inside the holding window
    assert post_entry_return(o2, 20, 2, 12) == base


def test_beta_window_excludes_event_bar_and_later():
    assert beta_window(100, 30) == slice(70, 100)
    with pytest.raises(ValueError):
        beta_window(10, 30)
    rng = np.random.default_rng(2)
    b = 100 * np.cumprod(1 + rng.normal(0, 0.001, 200))
    a = 50 * np.cumprod(1 + rng.normal(0, 0.002, 200))
    w = beta_window(150, 100)
    beta = beta_from_closes(b[w], a[w])
    b2, a2 = b.copy(), a.copy()
    b2[150:] *= 3.0  # the event bar and everything after it
    a2[150:] = rng.random(50)
    assert beta_from_closes(b2[w], a2[w]) == beta


def test_max_abs_log_jump_and_winsorize():
    c = np.array([1.0, 1.0, 2.0, np.nan, 2.0, 1.9])
    assert max_abs_log_jump(c, 0, 6) == pytest.approx(math.log(2.0))
    assert max_abs_log_jump(c, 2, 6) == pytest.approx(math.log(2.0 / 1.9))  # pairs across the NaN are skipped
    assert math.isnan(max_abs_log_jump(c, 3, 4))
    x = np.array([*range(1, 101), np.nan], dtype=float)
    w = winsorize(x, 0.01, 0.99)
    assert np.isnan(w[-1])
    assert w[0] == pytest.approx(np.quantile(x[:-1], 0.01)) and w[99] == pytest.approx(np.quantile(x[:-1], 0.99))
    assert w[50] == x[50]


def test_lag_return():
    r = lag_return(np.array([100.0, 101.0, 102.0, 103.0, np.nan, 110.0]), 3)
    assert np.isnan(r[:3]).all()
    assert r[3] == pytest.approx(0.03)
    assert np.isnan(r[4])
    assert r[5] == pytest.approx(110 / 102 - 1)


def test_prev_window_std_matches_numpy_and_excludes_current():
    rng = np.random.default_rng(1)
    x = rng.normal(size=200)
    x[50] = np.nan
    out = prev_window_std(x, 30, 20)
    for i in (40, 60, 120, 199):
        w = x[i - 30 : i]
        w = w[~np.isnan(w)]
        assert out[i] == pytest.approx(np.std(w, ddof=1), rel=1e-9)
    assert np.isnan(out[10])  # fewer than 20 values before bar 10


def test_prev_window_std_no_lookahead():
    rng = np.random.default_rng(2)
    x = rng.normal(size=300)
    a = prev_window_std(x, 50, 40)
    y = x.copy()
    y[150:] = rng.normal(size=150) * 100
    b = prev_window_std(y, 50, 40)
    np.testing.assert_array_equal(a[:151], b[:151])


def _grid(n):
    return np.arange(n, dtype=np.int64) * FIVE_MIN_MS


def test_detect_shocks_first_bar_and_cooldown():
    n = 400
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.0005, n)))
    # step down of 3 % at bar 300 -> r15 crosses at bars 300..302; the first one is the event
    close[300:] *= 0.97
    # another 3 % step 10 bars later is inside the 2 h cooldown, one 30 bars later is outside it
    close[310:] *= 0.97
    close[340:] *= 1.03
    ev = detect_shocks(_grid(n), close, window=100, min_frac=0.8, cooldown_ms=2 * HOUR_MS)
    assert [e["i"] for e in ev] == [300, 340]
    assert ev[0]["sign"] == -1 and ev[1]["sign"] == 1
    assert ev[0]["r15"] == pytest.approx(close[300] / close[297] - 1)


def test_detect_shocks_threshold_both_conditions():
    n = 300
    close = np.full(n, 100.0)
    close += np.where(np.arange(n) % 2 == 0, 0.5, -0.5)  # noisy: r15 SD about 1 %
    close[250:] *= 1.015  # 1.5 % >= 1.2 % but not >= 4 sigma
    assert detect_shocks(_grid(n), close, window=100) == []


def test_detect_shocks_no_lookahead():
    n = 500
    rng = np.random.default_rng(4)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.0005, n)))
    close[200:] *= 0.97
    a = detect_shocks(_grid(n), close, window=100)
    b_close = close.copy()
    b_close[260:] *= np.exp(np.cumsum(rng.normal(0, 0.02, n - 260)))
    b = detect_shocks(_grid(n), b_close, window=100)
    assert [e for e in a if e["i"] < 260] == [e for e in b if e["i"] < 260]


def test_beta_exact_and_hourly_sampling():
    rng = np.random.default_rng(5)
    rb = rng.normal(0, 0.002, 600)
    btc = 100 * np.cumprod(1 + rb)
    alt = 10 * np.cumprod(1 + 2.0 * rb)
    assert beta_from_closes(btc, alt) == pytest.approx(2.0, rel=1e-9)
    # 1h sampling uses closes at -1, -13, ...: alt follows BTC with a 1-bar lag -> 5m beta ~ 0, 1h beta ~ 2
    alt_lag = 10 * np.cumprod(1 + 2.0 * np.concatenate([[0.0], rb[:-1]]))
    assert abs(beta_from_closes(btc, alt_lag)) < 0.3
    assert beta_from_closes(btc, alt_lag, step=12) == pytest.approx(2.0, abs=0.4)


def test_beta_missing_and_degenerate():
    assert beta_from_closes(np.array([1.0]), np.array([1.0])) is None
    assert beta_from_closes(np.ones(10), np.arange(1.0, 11.0)) is None
    b = np.array([100.0, 101, np.nan, 103, 104])
    a = np.array([10.0, 10.2, 10.3, np.nan, 10.5])
    assert beta_from_closes(b, a, min_count=3) is None


def test_directional_residual():
    assert directional_residual(-1, -0.02, -0.01, 1.5) == pytest.approx(0.005)
    assert directional_residual(1, 0.01, 0.01, 1.0) == pytest.approx(0.0)


def test_tercile():
    assert [tercile(r) for r in (1, 20, 21, 40, 41, 60)] == ["high", "high", "mid", "mid", "low", "low"]
    with pytest.raises(ValueError):
        tercile(61)


def test_cluster_se_iid_and_clustered():
    v = [1.0, -1.0, 1.0, -1.0]
    # one value per cluster: CR1 = sqrt(sum e^2 * G/(G-1)) / N = sqrt(4 * 4/3) / 4
    assert cluster_se(v, [1, 2, 3, 4]) == pytest.approx(math.sqrt(16 / 3) / 4)
    # duplicated values in the same cluster double the cluster sums
    assert cluster_se(v + v, [1, 2, 3, 4] * 2) == pytest.approx(math.sqrt(4 * 4 * 4 / 3) / 8)
    assert math.isnan(cluster_se([1.0, 2.0], ["a", "a"]))
    assert mde(0.001) == pytest.approx(0.0028)


def test_placebo_times_guard():
    evs = [0, 10 * HOUR_MS]
    got = placebo_times(0, evs, [HOUR_MS, 5 * HOUR_MS, 9 * HOUR_MS])
    assert got == [5 * HOUR_MS]


def test_adverse_excursion():
    assert adverse_excursion(-1, 100.0, np.array([101.0, 105.0]), np.array([90.0, 99.0])) == pytest.approx(0.05)
    assert adverse_excursion(1, 100.0, np.array([101.0, 105.0]), np.array([90.0, 99.0])) == pytest.approx(0.10)
    assert adverse_excursion(1, 100.0, np.array([np.nan]), np.array([np.nan])) is None
