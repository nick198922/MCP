import math

import numpy as np

from tradedesk.analytics import vol_state as vs

H = vs.HOUR_MS


def _bars(n: int, r: float = 0.01, t0: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Closes with alternating +r / -r log returns (|r| constant)."""
    t = t0 + np.arange(n, dtype=np.int64) * H
    lr = np.where(np.arange(n) % 2 == 0, r, -r)
    lr[0] = 0.0
    return t, 100.0 * np.exp(np.cumsum(lr))


def test_log_returns_hole_gives_nan():
    t = np.array([0, H, 2 * H, 4 * H], dtype=np.int64)
    c = np.array([100.0, 110.0, 99.0, 120.0])
    r = vs.log_returns(t, c)
    assert math.isnan(r[0]) and math.isnan(r[3])
    assert math.isclose(r[1], math.log(1.1)) and math.isclose(r[2], math.log(0.9))


def test_realized_vol_constant_abs_return():
    t, c = _bars(400, 0.01)
    rv = vs.realized_vol(t, c, window=168, min_count=160)
    assert np.isnan(rv[:160]).all()  # returns exist from bar 1: bar 160 is the first with 160 of them
    assert math.isclose(rv[160], 0.01 * math.sqrt(8760), rel_tol=1e-9)
    assert np.allclose(rv[168:], 0.01 * math.sqrt(8760), rtol=1e-9)


def test_realized_vol_window_in_time_with_hole():
    t, c = _bars(400, 0.01)
    keep = np.ones(400, dtype=bool)
    keep[300:305] = False  # 5 missing hours -> 6 returns lost inside the window of bar 310
    rv = vs.realized_vol(t[keep], c[keep], window=168, min_count=163)
    k = int(np.flatnonzero(t[keep] == 310 * H)[0])
    assert math.isnan(rv[k])  # 168 - 6 = 162 < 163
    rv2 = vs.realized_vol(t[keep], c[keep], window=168, min_count=162)
    assert math.isclose(rv2[k], 0.01 * math.sqrt(8760), rel_tol=1e-9)


def test_trailing_percentile_monotone_and_ties():
    t = np.arange(50, dtype=np.int64) * H
    up = vs.trailing_percentile(t, np.arange(50.0), window_ms=10 * H, min_obs=10)
    assert np.isnan(up[:10]).all() and np.allclose(up[10:], 1.0)
    down = vs.trailing_percentile(t, -np.arange(50.0), window_ms=10 * H, min_obs=10)
    assert np.allclose(down[10:], 0.0)
    flat = vs.trailing_percentile(t, np.ones(50), window_ms=10 * H, min_obs=10)
    assert np.allclose(flat[10:], 0.5)
    x = np.arange(50.0)
    x[20] = 15.5  # earlier values 10..19 in the window: 6 below (10..15), 4 above
    p = vs.trailing_percentile(t, x, window_ms=10 * H, min_obs=10)
    assert math.isclose(p[20], 0.6)


def test_trailing_percentile_skips_undefined():
    t = np.arange(30, dtype=np.int64) * H
    x = np.arange(30.0)
    x[5:15] = np.nan
    p = vs.trailing_percentile(t, x, window_ms=20 * H, min_obs=10)
    assert p[20] == 1.0  # earlier defined in [0, 20): 0..4 and 15..19 = 10 values
    p11 = vs.trailing_percentile(t, x, window_ms=20 * H, min_obs=11)
    assert math.isnan(p11[20])
    assert math.isnan(p[10])  # x[10] itself undefined


def test_value_at_boundaries():
    t = np.array([0, H, 2 * H, 10 * H], dtype=np.int64)
    x = np.array([1.0, 2.0, 3.0, 4.0])
    q = np.array([H - 1, H, 2 * H - 1, 2 * H, 3 * H, 9 * H, 10 * H, 11 * H])
    v = vs.value_at(t, x, q, max_stale_ms=6 * H)
    assert math.isnan(v[0])  # nothing closed yet
    assert v[1] == 1.0 and v[2] == 1.0 and v[3] == 2.0 and v[4] == 3.0
    assert v[5] == 3.0  # last close 3H, exactly 6 h stale: still defined
    assert math.isnan(v[6])  # 7 h stale
    assert v[7] == 4.0


def test_causality_later_prices_do_not_change_past():
    rng = np.random.default_rng(7)
    n = 3000
    t = np.arange(n, dtype=np.int64) * H
    c = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    cut = 2000
    c2 = c.copy()
    c2[cut:] *= np.exp(rng.normal(0, 0.05, n - cut))  # change everything from bar `cut` on
    rv1 = vs.realized_vol(t, c)
    rv2 = vs.realized_vol(t, c2)
    assert np.array_equal(np.isnan(rv1[:cut]), np.isnan(rv2[:cut]))
    assert np.allclose(rv1[:cut], rv2[:cut], equal_nan=True)
    p1 = vs.trailing_percentile(t, rv1, window_ms=1000 * H, min_obs=500)
    p2 = vs.trailing_percentile(t, rv2, window_ms=1000 * H, min_obs=500)
    assert np.allclose(p1[:cut], p2[:cut], equal_nan=True)
    q = np.array([cut * H, cut * H - 1, 1500 * H])  # queries at or before the close of bar cut - 1
    assert np.allclose(vs.value_at(t, p1, q), vs.value_at(t, p2, q), equal_nan=True)
    assert not np.allclose(rv1[cut:], rv2[cut:])
