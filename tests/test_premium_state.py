from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics import premium_state as ps

H = ps.HOUR_MS
T0 = 1_700_000_000_000 // H * H  # an hour boundary


def _hours(n: int, start: int = T0) -> np.ndarray:
    return start + H * np.arange(n, dtype=np.int64)


def test_wick_hour_long_takes_lowest_low_and_short_highest_high() -> None:
    t = _hours(8)
    low = np.array([5.0, 4.0, 3.0, 3.0, 6.0, 1.0, 9.0, 9.0])
    high = np.array([9.0, 9.5, 8.0, 9.5, 7.0, 8.0, 9.0, 9.0])
    assert ps.wick_hour(t, low, high, T0, 4, "long") == 2  # first of the tied 3.0
    assert ps.wick_hour(t, low, high, T0, 4, "short") == 1  # first of the tied 9.5
    assert ps.wick_hour(t, low, high, T0 + 4 * H, 4, "long") == 5


def test_wick_hour_needs_every_hour_of_the_candle() -> None:
    t = np.delete(_hours(8), 2)
    x = np.ones(t.size)
    assert ps.wick_hour(t, x, x, T0, 4, "long") == -1  # hour 2 missing
    assert ps.wick_hour(t, x, x, T0 + 3 * H, 4, "long") == 2  # hours 3..6 present
    assert ps.wick_hour(t, x, x, T0 + 5 * H, 4, "long") == -1  # runs past the end
    with pytest.raises(ValueError):
        ps.wick_hour(_hours(4), np.ones(4), np.ones(4), T0, 4, "flat")


def test_trailing_z_known_value_and_strictly_prior_window() -> None:
    n = 720
    t = _hours(n + 1)
    v = np.where(np.arange(n + 1) % 2 == 0, 1.0, -1.0)  # mean 0, population SD 1 over the 720 prior bars
    v[n] = 3.0
    assert ps.trailing_z(t, v, int(t[n]), min_n=720) == pytest.approx(3.0)
    v2 = v.copy()
    v2[n] = 100.0  # the bar itself is not part of its own window
    assert ps.trailing_z(t, v2, int(t[n]), min_n=720) == pytest.approx(100.0)


def test_trailing_z_undefined_cases() -> None:
    t = _hours(100)
    v = np.arange(100, dtype=float)
    assert math.isnan(ps.trailing_z(t, v, int(t[99]), min_n=100))  # only 99 earlier bars
    assert ps.trailing_z(t, v, int(t[99]), min_n=99) == pytest.approx((99 - 49.0) / np.arange(99).std())
    assert math.isnan(ps.trailing_z(t, v, int(t[99]) + H, min_n=1))  # bar missing
    assert math.isnan(ps.trailing_z(t, np.zeros(100), int(t[99]), min_n=1))  # zero spread
    v3 = v.copy()
    v3[:50] = np.nan
    assert math.isnan(ps.trailing_z(t, v3, int(t[99]), min_n=60))  # 49 finite earlier values


def test_trailing_z_window_drops_older_bars() -> None:
    t = _hours(30)
    v = np.zeros(30)
    v[:10] = 1000.0  # older than the 19-hour window of bar 29
    v[10:29] = np.where(np.arange(19) % 2 == 0, 1.0, -1.0)
    z = ps.trailing_z(t, v, int(t[29]), window_ms=19 * H, min_n=19)
    w = v[10:29]
    assert z == pytest.approx((0.0 - w.mean()) / w.std())


def test_trailing_z_robust_uses_median_and_mad() -> None:
    t = _hours(6)
    v = np.array([1.0, 2.0, 3.0, 4.0, 100.0, 10.0])  # prior: median 3, |dev| 2,1,0,1,97 -> MAD 1
    assert ps.trailing_z(t, v, int(t[5]), min_n=5, robust=True) == pytest.approx((10.0 - 3.0) / ps.MAD_SCALE)


def _series(n_days: int = 40) -> tuple[tuple[np.ndarray, ...], tuple[np.ndarray, ...]]:
    n = n_days * 24
    t = _hours(n)
    rng = np.random.default_rng(7)
    c = 100 + rng.normal(0, 1, n).cumsum()
    price = (t, c - 1.0, c + 1.0)
    pc = rng.normal(0.0001, 0.0002, n)
    prem = (t, pc + 0.0003, pc - 0.0003, pc)
    return price, prem


def test_sweep_premium_long_is_z_of_premium_low_and_short_is_minus_z_of_high() -> None:
    price, prem = _series()
    start = int(price[0][35 * 24])
    wl = ps.wick_hour(price[0], price[1], price[2], start, 4, "long")
    ws = ps.wick_hour(price[0], price[1], price[2], start, 4, "short")
    zl = ps.trailing_z(prem[0], prem[2], int(price[0][wl]))
    zs = ps.trailing_z(prem[0], prem[1], int(price[0][ws]))
    assert ps.sweep_premium(price, prem, start, 4, "long") == pytest.approx(zl)
    assert ps.sweep_premium(price, prem, start, 4, "short") == pytest.approx(-zs)
    assert ps.sweep_premium(price, prem, start, 4, "short", mode="level") == pytest.approx(-prem[1][ws])
    assert ps.sweep_premium(price, prem, start, 4, "long", stat="close", mode="level") == pytest.approx(prem[3][wl])


def test_sweep_premium_sign_of_a_perp_discount_on_a_long_sweep() -> None:
    price, prem = _series()
    start = int(price[0][35 * 24])
    w = ps.wick_hour(price[0], price[1], price[2], start, 4, "long")
    low = prem[2].copy()
    low[w] = -0.01  # perp pushed 1 % below spot in the wick hour
    assert ps.sweep_premium(price, (prem[0], prem[1], low, prem[3]), start, 4, "long") < -10
    high = prem[1].copy()
    ws = ps.wick_hour(price[0], price[1], price[2], start, 4, "short")
    high[ws] = 0.01  # perp pushed 1 % above spot on a short sweep -> also a low signed value
    assert ps.sweep_premium(price, (prem[0], high, prem[2], prem[3]), start, 4, "short") < -10


def test_sweep_premium_shift_reads_the_shifted_hour() -> None:
    price, prem = _series()
    start = int(price[0][35 * 24])
    w = ps.wick_hour(price[0], price[1], price[2], start, 4, "long")
    sh = -3 * ps.DAY_MS
    want = ps.trailing_z(prem[0], prem[2], int(price[0][w]) + sh, min_n=24 * 2)
    got = ps.sweep_premium(price, prem, start, 4, "long", shift_ms=sh, min_n=24 * 2)
    assert got == pytest.approx(want)
    assert math.isnan(ps.sweep_premium(price, prem, start, 4, "long", shift_ms=10 * ps.DAY_MS))  # past the data


def test_sweep_premium_does_not_depend_on_data_after_the_entry() -> None:
    price, prem = _series()
    start = int(price[0][35 * 24])
    entry = start + 4 * H
    base = {
        (s, st, md): ps.sweep_premium(price, prem, start, 4, s, stat=st, mode=md)
        for s in ("long", "short")
        for st in ("extreme", "close")
        for md in ("z", "robust", "level")
    }
    after = price[0] >= entry
    rng = np.random.default_rng(1)
    p2 = (price[0], np.where(after, price[1] - 50 * rng.random(after.size), price[1]), np.where(after, price[2] + 50, price[2]))
    q2 = tuple([prem[0]] + [np.where(after, x * -30 + 0.05, x) for x in prem[1:]])
    for (s, st, md), v in base.items():
        assert ps.sweep_premium(p2, q2, start, 4, s, stat=st, mode=md) == pytest.approx(v), (s, st, md)
    assert all(math.isfinite(v) for v in base.values())


def test_sweep_premium_rejects_unknown_options() -> None:
    price, prem = _series()
    start = int(price[0][35 * 24])
    with pytest.raises(ValueError):
        ps.sweep_premium(price, prem, start, 4, "long", stat="mean")
    with pytest.raises(ValueError):
        ps.sweep_premium(price, prem, start, 4, "long", mode="pct")


def test_implied_funding_clamp() -> None:
    assert ps.implied_funding(0.0002) == pytest.approx(0.0001)  # inside the clamp band -> the interest rate
    assert ps.implied_funding(0.0010) == pytest.approx(0.0010 - 0.0005)
    assert ps.implied_funding(-0.0010) == pytest.approx(-0.0010 + 0.0005)
