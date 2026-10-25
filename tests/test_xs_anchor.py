"""Anchor (52-week-high nearness) features: analytic expectations and point-in-time behaviour."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.funding_crowding import Bars
from tradedesk.analytics.xs_anchor import (
    age_days,
    bottom_set,
    features_at,
    hedged_returns,
    low_minus_middle,
    near_ath,
    near_high,
)


def make_bars(closes: dict[int, float]) -> Bars:
    return Bars(dict(closes), dict(closes), dict(closes), dict(closes), dict.fromkeys(closes, 1e7))


T = 1000  # a rank day


def test_near_high_peak_then_decline() -> None:
    # 400 days rising to 200 at day T-101, then a linear fall to 50 at day T-1
    c = {d: 100.0 + (d - (T - 500)) * 0.25 for d in range(T - 500, T - 100)}
    peak = c[T - 101]
    for i, d in enumerate(range(T - 100, T)):
        c[d] = peak - (peak - 50.0) * (i + 1) / 100
    b = make_bars(c)
    assert near_high(b, T) == pytest.approx(50.0 / peak)
    # at the peak's next day nearness is 1 (the last close is the high)
    assert near_high(b, T - 100) == pytest.approx(1.0)


def test_near_high_window_excludes_older_high() -> None:
    c = {d: 100.0 for d in range(T - 400, T)}
    c[T - 380] = 1000.0  # outside the 365-day window
    c[T - 200] = 125.0
    assert near_high(make_bars(c), T) == pytest.approx(100.0 / 125.0)
    assert near_ath(make_bars(c), T) == pytest.approx(0.1)


def test_near_high_min_days_and_recent_close() -> None:
    c = {d: 10.0 for d in range(T - 299, T)}  # 299 closes
    assert near_high(make_bars(c), T) is None
    c[T - 300] = 20.0
    assert near_high(make_bars(c), T) == pytest.approx(0.5)
    # last close older than tol (3 days) -> None
    c2 = {d: 10.0 for d in range(T - 365, T - 4)}
    assert near_high(make_bars(c2), T) is None
    assert near_high(make_bars(c2), T, min_days=300, tol=4) == pytest.approx(1.0)


def test_no_lookahead_truncation() -> None:
    # data after t-1 (including a new high on day t and later) must not change any feature at t
    c = {d: 50.0 + 30.0 * math.sin(d / 37.0) + d * 0.01 for d in range(T - 600, T + 200)}
    btc = {d: 100.0 + 10.0 * math.cos(d / 11.0) for d in range(T - 600, T + 200)}
    full, bfull = make_bars(c), make_bars(btc)
    cut = make_bars({d: v for d, v in c.items() if d < T})
    bcut = make_bars({d: v for d, v in btc.items() if d < T})
    f_full = features_at(full, bfull, T, T - 700)
    f_cut = features_at(cut, bcut, T, T - 700)
    assert all(v is not None for v in f_full.values())
    for k, v in f_full.items():
        assert f_cut[k] == pytest.approx(v), k
    # a huge spike on day T itself must not move NEAR365(T)
    spiked = dict(c)
    spiked[T] = 1e6
    assert near_high(make_bars(spiked), T) == pytest.approx(f_full["NEAR365"])


def test_features_values() -> None:
    days = range(T - 400, T)
    c = {d: 100.0 * 1.001 ** (d - T) for d in days}  # always rising: nearness 1
    f = features_at(make_bars(c), make_bars(c), T, T - 1000)
    assert f["NEAR365"] == pytest.approx(1.0)
    assert f["NEAR90"] == pytest.approx(1.0)
    assert f["ATH"] == pytest.approx(1.0)
    assert f["M90"] == pytest.approx(1.001**83 - 1.0)
    assert f["AGE"] == pytest.approx(math.log(1000))
    assert f["BETA90"] == pytest.approx(1.0)


def test_age_days() -> None:
    assert age_days(None, T) is None
    assert age_days(T, T) is None
    assert age_days(T - 30, T) == 30


def test_hedged_returns() -> None:
    assert hedged_returns([0.1, -0.05], [1.5, 0.0], 0.04) == pytest.approx([0.04, -0.05])
    with pytest.raises(ValueError):
        hedged_returns([0.1], [1.0, 2.0], 0.0)


def test_low_minus_middle() -> None:
    sig = list(range(10))  # Q1 = idx 0,1; Q3 = idx 4,5
    ret = [0.0] * 10
    ret[0], ret[1] = -0.02, -0.04
    ret[4], ret[5] = 0.01, 0.03
    assert low_minus_middle(sig, ret) == pytest.approx(-0.03 - 0.02)
    assert bottom_set(sig) == {0, 1}
    # reversed order of input does not change the answer
    assert low_minus_middle(sig[::-1], ret[::-1]) == pytest.approx(-0.05)
    with pytest.raises(ValueError):
        low_minus_middle([1.0, 2.0], [0.0, 0.0])
