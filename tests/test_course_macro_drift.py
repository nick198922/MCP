"""Step 8 of the course-setups card (S19 / H6): pure analytics, analytic expectations, no network."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from tradedesk.analytics.course_macro_drift import (
    COST_RT,
    DAY_MS,
    HOUR_MS,
    daily_vol,
    diff_draws,
    episodes,
    fade_net,
    group_diff,
    h6_verdict,
    mean_boot,
    placebo_days,
    pre_return,
    price_at,
    shifted_days,
    slope_boot,
    window_returns,
)


def bars(n: int, start: int = 0, f=lambda i: 100.0 + i) -> tuple[np.ndarray, np.ndarray]:
    o = start + HOUR_MS * np.arange(n, dtype=np.int64)
    return o, np.array([f(i) for i in range(n)], float)


def test_price_at_uses_last_closed_bar_only() -> None:
    o, c = bars(10)  # bar i opens at i h, closes at (i + 1) h with price 100 + i
    assert price_at(o, c, 5 * HOUR_MS) == 104.0  # bar 4 closed at 5 h
    assert price_at(o, c, 5 * HOUR_MS + 30 * 60_000) == 104.0  # 5:30 -> bar 5 has not closed yet
    assert price_at(o, c, 0) is None  # nothing closed yet
    assert price_at(o, c, 10 * HOUR_MS) == 109.0
    assert price_at(o, c, 13 * HOUR_MS) == 109.0  # last bar ended 3 h earlier: still allowed
    assert price_at(o, c, 13 * HOUR_MS + 1) is None  # older than 3 h


def test_price_at_skips_missing_bars_within_max_age() -> None:
    o, c = bars(10)
    keep = np.array([i not in (4, 5) for i in range(10)])
    o, c = o[keep], c[keep]
    assert price_at(o, c, 6 * HOUR_MS) == 103.0  # bars 4 and 5 missing -> bar 3 (ended 4 h, 2 h ago)
    assert price_at(o, c, 6 * HOUR_MS, max_age_ms=HOUR_MS) is None


def test_window_returns_analytic() -> None:
    o, c = bars(300, f=lambda i: 100.0 * 1.001**i)
    t0 = 100 * HOUR_MS
    pre, post = window_returns(o, c, t0, pre_h=72, post_h=120)
    assert pre == pytest.approx(1.001**72 - 1, rel=1e-12)
    assert post == pytest.approx(1.001**120 - 1, rel=1e-12)
    _, post_d = window_returns(o, c, t0, pre_h=72, post_h=120, delay_h=1)
    assert post_d == pytest.approx(1.001**120 - 1, rel=1e-12)
    assert window_returns(o, c, 250 * HOUR_MS, post_h=120) is None  # past the end of data
    assert pre_return(o, c, t0) == pytest.approx(pre)


def test_pre_event_values_do_not_depend_on_later_data() -> None:
    o, c = bars(400, f=lambda i: 100.0 + 5 * math.sin(i / 7.0))
    t0 = 200 * HOUR_MS + 30 * 60_000  # 08:30-like time inside a bar
    pre1, vol1 = pre_return(o, c, t0), daily_vol(o, c, t0, days=5)
    c2 = c.copy()
    c2[o >= t0 - HOUR_MS] *= 3.0  # change every bar that has not closed by t0 (and everything after)
    assert pre_return(o, c2, t0) == pre1
    assert daily_vol(o, c2, t0, days=5) == vol1
    _, post1 = window_returns(o, c, t0)
    _, post2 = window_returns(o, c2, t0)
    assert post1 != post2  # the outcome does use later prices


def test_daily_vol_constant_growth_is_zero() -> None:
    o, c = bars(24 * 40, f=lambda i: 100.0 * math.exp(0.001 * i))
    v = daily_vol(o, c, 24 * 35 * HOUR_MS, days=30)
    assert v == pytest.approx(0.0, abs=1e-12)
    assert daily_vol(o, c, 24 * 10 * HOUR_MS, days=30) is None  # not enough history


def test_episodes_chain_overlapping_windows() -> None:
    t = [0, 4 * DAY_MS, 30 * DAY_MS, 9 * DAY_MS, 100 * DAY_MS]
    cl = episodes(t, 3 * DAY_MS, 5 * DAY_MS)
    # 0 -> [-3, 5]; 4 -> [1, 9] overlaps; 9 -> [6, 14] overlaps 4; 30 and 100 separate
    assert cl[0] == cl[1] == cl[3]
    assert len({cl[0], cl[2], cl[4]}) == 3


def test_placebo_days_exclusions() -> None:
    ev = [(date(2024, 1, 10), "cpi"), (date(2024, 1, 31), "fomc"), (date(2024, 1, 19), "nfp")]
    days = placebo_days(date(2024, 1, 1), date(2024, 2, 9), ev)
    assert all(d.weekday() < 5 for d in days)
    assert date(2024, 1, 15) not in days  # within 5 days of CPI 01-10
    assert date(2024, 1, 16) in days
    assert date(2024, 1, 18) not in days and date(2024, 1, 22) in days  # NFP ±1 (01-20/21 weekend)
    assert date(2024, 1, 26) not in days and date(2024, 2, 5) not in days and date(2024, 2, 6) in days


def test_shifted_days() -> None:
    ev = [date(2024, 1, 10), date(2024, 1, 31)]
    out = shifted_days([date(2024, 1, 10)], ev, shifts=(14, 21))
    assert (date(2024, 1, 24), 14) in out  # 7 days from the 01-31 event: clear of ±5
    assert (date(2024, 1, 31), 21) not in out  # lands on an event
    out2 = shifted_days([date(2024, 1, 10)], ev, shifts=(14,), clear=7)
    assert out2 == []


def test_group_diff_exact_and_planted() -> None:
    x = [1.0, 2.0, 3.0, 10.0]
    g = [False, False, False, True]
    r = group_diff(x, g, [0, 1, 2, 3], n_boot=500)
    assert r["stat"] == pytest.approx(8.0)
    assert r["n_in"] == 1 and r["n_out"] == 3 and r["clusters_in"] == 1
    rng = np.random.default_rng(1)
    n = 400
    gg = rng.random(n) < 0.3
    xx = rng.normal(0, 0.05, n) - 0.04 * gg
    res = group_diff(xx, gg, np.arange(n), n_boot=2000)
    assert res["ci"][1] < 0 and res["p_neg"] < 0.01
    null = group_diff(rng.normal(0, 0.05, n), gg, np.arange(n), n_boot=2000)
    assert null["ci"][0] < 0 < null["ci"][1]
    assert mde_ok(null["se"], 0.05 * math.sqrt(1 / gg.sum() + 1 / (~gg).sum()))


def mde_ok(se: float, analytic: float) -> bool:
    return abs(se / analytic - 1) < 0.15


def test_group_diff_clusters_move_together() -> None:
    # two clusters, each with one in- and one out-of-group value: the bootstrap only ever sees whole clusters
    r = group_diff([1.0, 0.0, 3.0, 0.0], [True, False, True, False], ["a", "a", "b", "b"], n_boot=500)
    assert r["stat"] == pytest.approx(2.0)
    assert set(np.round(np.unique(r["draws"]), 9)) <= {1.0, 2.0, 3.0}


def test_group_diff_empty_group() -> None:
    r = group_diff([1.0, 2.0], [False, False], [0, 1])
    assert math.isnan(r["stat"]) and r["ci"] is None


def test_mean_boot_and_slope() -> None:
    m = mean_boot([1.0, 2.0, 3.0, 4.0], [0, 0, 1, 1], n_boot=500)
    assert m["stat"] == 2.5 and m["clusters"] == 2
    assert set(np.round(np.unique(m["draws"]), 9)) <= {1.5, 2.5, 3.5}
    x = np.linspace(-0.1, 0.1, 50)
    s = slope_boot(x, -0.5 * x, np.arange(50), n_boot=500)
    assert s["stat"] == pytest.approx(-0.5)
    assert s["ci"][0] == pytest.approx(-0.5) and s["ci"][1] == pytest.approx(-0.5)


def test_diff_draws() -> None:
    a = np.full(100, 1.0)
    b = np.full(100, 3.0)
    d = diff_draws(a, b, 1.0, 3.0)
    assert d["stat"] == -2.0 and d["ci"] == (-2.0, -2.0) and d["p_neg"] == pytest.approx(1 / 101)


def test_fade_net() -> None:
    pre = [0.05, -0.04, 0.01, 0.03]
    post = [-0.02, 0.03, 0.10, 0.01]
    f = fade_net(pre, post, 0.03)
    assert f[0] == pytest.approx(0.02 - COST_RT)
    assert f[1] == pytest.approx(0.03 - COST_RT)
    assert math.isnan(f[2])
    assert f[3] == pytest.approx(-0.01 - COST_RT)
    up = fade_net(pre, post, 0.03, side=1)
    assert math.isnan(up[1]) and up[0] == pytest.approx(f[0])
    dn = fade_net(pre, post, 0.03, side=-1)
    assert math.isnan(dn[0]) and dn[1] == pytest.approx(f[1])


def test_h6_verdict_rules() -> None:
    base = dict(
        delta=-0.05,
        ci=(-0.08, -0.02),
        clusters_u=25,
        mde_value=0.03,
        dd=-0.04,
        dd_ci=(-0.07, -0.01),
        p_adj=0.01,
        oos_n_u=6,
        oos_delta=-0.01,
    )
    assert h6_verdict(**base) == "confirmed"
    assert h6_verdict(**{**base, "clusters_u": 19}) == "inconclusive"
    assert h6_verdict(**{**base, "mde_value": 0.05}) == "inconclusive"
    assert h6_verdict(**{**base, "delta": -0.01}) == "rejected"
    assert h6_verdict(**{**base, "ci": (-0.08, 0.001)}) == "rejected"
    assert h6_verdict(**{**base, "dd_ci": (-0.07, 0.01)}) == "inconclusive"
    assert h6_verdict(**{**base, "p_adj": 0.2}) == "inconclusive"
    assert h6_verdict(**{**base, "oos_delta": 0.01}) == "inconclusive"
    assert h6_verdict(**{**base, "oos_n_u": 4}) == "inconclusive"
