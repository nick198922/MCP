"""Analytic checks for analytics.usde_funding (IDEA-20261025-ethena-usde-supply-funding-regime)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics.usde_funding import (
    DAY_MS,
    WEEK_MS,
    adf_tstat,
    block_bootstrap_diff,
    expanding_tercile_top,
    forward_drawdown,
    h1_rows,
    h1_rows_4w,
    lag1_autocorr,
    mde_partial_corr,
    monday_anchors,
    partial_corr_nw,
    tercile_top,
    value_asof,
    weekly_panel,
    window_funding_apr,
)


def ms(*a: int) -> int:
    return int(datetime(*a, tzinfo=UTC).timestamp() * 1000)


def test_monday_anchors() -> None:
    out = monday_anchors(ms(2026, 10, 1), ms(2026, 10, 26))
    assert out == [ms(2026, 10, 5), ms(2026, 10, 12), ms(2026, 10, 19), ms(2026, 10, 26)]
    assert all(datetime.fromtimestamp(t / 1000, UTC).weekday() == 0 for t in out)
    assert monday_anchors(ms(2026, 10, 5), ms(2026, 10, 5)) == [ms(2026, 10, 5)]


def test_value_asof() -> None:
    t, v = [10, 20, 30], [1.0, 2.0, 3.0]
    assert value_asof(t, v, 9) is None
    assert value_asof(t, v, 20) == 2.0
    assert value_asof(t, v, 29) == 2.0
    assert value_asof(t, v, 99) == 3.0


def test_window_funding_apr_and_gaps() -> None:
    start = ms(2026, 10, 5)
    times = [start + i * 8 * 3_600_000 for i in range(21)]  # one week of 8h settlements
    rates = [0.0001] * 21
    hrs = [8.0] * 21
    apr = window_funding_apr(times, rates, hrs, start, start + WEEK_MS)
    assert apr == pytest.approx(0.0001 * 3 * 365)
    # a settlement exactly at the window end belongs to the next window
    assert window_funding_apr(times + [start + WEEK_MS], rates + [1.0], hrs + [8.0], start, start + WEEK_MS) == pytest.approx(apr)
    # dropping 3 of 21 settlements (< 90 % cover) rejects the window
    assert window_funding_apr(times[3:], rates[3:], hrs[3:], start, start + WEEK_MS) is None


def test_window_funding_is_causal() -> None:
    start = ms(2026, 10, 5)
    times = [start + i * 8 * 3_600_000 for i in range(42)]
    a = [0.0001] * 42
    b = a[:21] + [0.01] * 21  # change only data after the first week
    hrs = [8.0] * 42
    assert window_funding_apr(times, a, hrs, start, start + WEEK_MS) == window_funding_apr(times, b, hrs, start, start + WEEK_MS)
    assert value_asof(times, a, start + WEEK_MS - 1) == value_asof(times, b, start + WEEK_MS - 1)


def test_lag1_autocorr_ar1() -> None:
    rng = np.random.default_rng(1)
    e = rng.normal(size=20000)
    v = np.zeros_like(e)
    for i in range(1, len(e)):
        v[i] = 0.6 * v[i - 1] + e[i]
    assert lag1_autocorr(v) == pytest.approx(0.6, abs=0.03)


def test_adf_separates_random_walk_and_white_noise() -> None:
    rng = np.random.default_rng(2)
    e = rng.normal(size=400)
    assert adf_tstat(e) < -5
    assert adf_tstat(np.cumsum(e)) > -2.88


def test_mde_matches_fisher_formula() -> None:
    m, n_eff = mde_partial_corr(130, 3)
    assert n_eff == 130
    assert m == pytest.approx(math.tanh(2.80 / math.sqrt(124)))
    m2, n_eff2 = mde_partial_corr(130, 3, 0.5, 0.5)
    assert n_eff2 == pytest.approx(130 * 0.75 / 1.25)
    assert m2 > m
    # opposite-signed autocorrelations would inflate n; it is capped at n
    assert mde_partial_corr(130, 3, 0.5, -0.5)[1] == 130


def test_partial_corr_recovers_known_value() -> None:
    rng = np.random.default_rng(3)
    n = 20000
    c = rng.normal(size=n)
    x = 0.8 * c + rng.normal(size=n)
    y = -1.5 * c + rho_noise(rng, x, n)
    pc = partial_corr_nw(y, x, [c], lag=4)
    # given c, y = -0.5 * x_resid + e with var(x_resid) = 1, var(e) = 1 -> r = -0.5 / sqrt(0.25 + 1)
    assert pc.r == pytest.approx(-0.5 / math.sqrt(1.25), abs=0.02)
    assert pc.lo < pc.r < pc.hi
    assert sum(pc.contrib) / n == pytest.approx(pc.r)
    assert pc.n == n


def rho_noise(rng: np.random.Generator, x: np.ndarray, n: int) -> np.ndarray:
    return -0.5 * x + rng.normal(size=n)


def test_partial_corr_null_has_honest_se() -> None:
    rng = np.random.default_rng(4)
    n = 500
    pc = partial_corr_nw(rng.normal(size=n), rng.normal(size=n), [rng.normal(size=n)])
    assert abs(pc.r) < 3 * pc.se
    assert pc.se == pytest.approx(1 / math.sqrt(n), rel=0.25)


def test_block_bootstrap_diff() -> None:
    v = [1.0] * 30 + [0.0] * 60
    m = [True] * 30 + [False] * 60
    point, lo, hi = block_bootstrap_diff(v, m, block=5, n_boot=300)
    assert point == 1.0
    assert lo == pytest.approx(1.0) and hi == pytest.approx(1.0)


def test_forward_drawdown() -> None:
    t0 = ms(2026, 10, 5)
    times = [t0 + i * DAY_MS for i in range(10)]
    lows = [100, 95, 90, 99, 98, 97, 96, 50, 50, 50]
    assert forward_drawdown(times, lows, 100.0, t0, t0 + WEEK_MS) == pytest.approx(-0.10)
    assert forward_drawdown(times, lows, 100.0, t0 + 20 * DAY_MS, t0 + 27 * DAY_MS) is None


# ---------------------------------------------------------------- weekly design (lag / alignment)

T0 = ms(2026, 1, 5)  # a Monday
H8 = 8 * 3_600_000
NW = 14  # weeks in the synthetic panel


def f_week(k: int) -> float:
    """8h rate in week k: quadratic in k, so F_{k+1} - F_k = c (2k + 1) differs for every k (an index shift shows)."""
    return 1e-5 * k * k


def cl(d: int) -> float:
    """Daily close on day d of the synthetic bars (day 0 = T0 - 14 d); weekly returns differ week to week."""
    return 100.0 + d**1.5


def wk_ret(k: int) -> float:
    """log return of week k: close of the bar opening T_k + 6 d over that of the bar opening T_k - 1 d."""
    return math.log(cl(14 + 7 * k + 6) / cl(14 + 7 * k - 1))


def synth(usde_bump: tuple[int, float] | None = None, fund_from: int | None = None) -> dict:
    """Synthetic inputs: funding 8h-settled, USDe daily at 00:00 = day index (optionally bumped from a time on),
    daily bars with close = 100 * 1.01 ** day, OI = 1000 + week."""
    anchors = [T0 + k * WEEK_MS for k in range(NW)]
    ft = [T0 - 2 * WEEK_MS + j * H8 for j in range((NW + 2) * 21)]
    rates = [f_week((t - T0) // WEEK_MS) for t in ft]
    if fund_from is not None:  # perturb funding settled at or after fund_from only
        rates = [r + (0.003 if t >= fund_from else 0.0) for t, r in zip(ft, rates, strict=True)]
    hrs = [8.0] * len(ft)
    ut = [float(T0 - 2 * WEEK_MS + d * DAY_MS) for d in range((NW + 2) * 7)]
    uv = [1e3 * d for d in range(len(ut))]
    if usde_bump is not None:
        uv = [v + (usde_bump[1] if t >= usde_bump[0] else 0.0) for t, v in zip(ut, uv, strict=True)]
    bt = [float(T0 - 2 * WEEK_MS + d * DAY_MS) for d in range((NW + 2) * 7)]
    close = [cl(d) for d in range(len(bt))]
    return {
        "anchors": anchors,
        "now_ms": T0 + NW * WEEK_MS,
        "usde_t": ut,
        "usde_v": uv,
        "oi": [1000.0 + k for k in range(NW)],
        "funding": ((ft, rates, hrs), (ft, rates, hrs)),
        "funding_alt": ((ft, rates, hrs), (ft, rates, hrs)),
        "bar_t": bt,
        "bar_close": close,
        "bar_low": close,
    }


def panel(**kw) -> list[dict]:
    return weekly_panel(**synth(**kw))


def test_weekly_panel_known_values() -> None:
    w = panel()
    k = 5
    assert w[k]["t"] == T0 + k * WEEK_MS
    assert w[k]["f"] == pytest.approx(f_week(k) * 3 * 365)  # funding of [T_k, T_k + 7d)
    assert w[k]["f_btc"] == w[k]["f_eth"] == w[k]["f_byb"] == pytest.approx(w[k]["f"])
    # USDe stamped <= T_k - 2 d: day index 14 + 7k - 2 (series starts at T0 - 14 d)
    assert w[k]["usde"] == pytest.approx(1e3 * (14 + 7 * k - 2))
    assert w[k]["usde_nolag"] == pytest.approx(1e3 * (14 + 7 * k))
    assert w[k]["ret"] == pytest.approx(wk_ret(k))
    assert wk_ret(k) != pytest.approx(wk_ret(k - 1))


def test_weekly_panel_drops_incomplete_last_week_for_every_funding_field() -> None:
    kw = synth()
    # "now" falls inside the last week; its settlements so far would pass the 90 % cover gate (cf. 2026-10-19: 19/21)
    kw["now_ms"] = T0 + (NW - 1) * WEEK_MS + 6 * DAY_MS + 8 * 3_600_000
    t_last = [t for t in kw["funding"][0][0] if t < kw["now_ms"]]
    n = len(t_last)
    kw["funding"] = tuple((t_last, s[1][:n], s[2][:n]) for s in kw["funding"])
    kw["funding_alt"] = kw["funding"]
    w = weekly_panel(**kw)
    assert w[-2]["f"] is not None
    for key in ("f", "f_btc", "f_eth", "f_byb", "ret", "dd7"):
        assert w[-1][key] is None, key


def test_h1_rows_alignment_known_example() -> None:
    w = panel()
    r = h1_rows(w)
    i = 6
    j = r["t"].index(T0 + i * WEEK_MS)
    c = 1e-5 * 3 * 365
    assert r["y"][j] == pytest.approx(c * (2 * i + 1))  # F_{i+1} - F_i
    assert r["c1"][j] == pytest.approx(c * (2 * i - 1))  # F_i - F_{i-1}
    assert r["c2"][j] == pytest.approx(c * (2 * i - 3))  # F_{i-1} - F_{i-2}
    assert r["lvl"][j] == pytest.approx(c * i * i)
    assert r["x"][j] == pytest.approx(7e3 / (1000 + i + 1))  # one week of USDe growth / OI(T_{i+1})
    assert r["c3"][j] == pytest.approx(wk_ret(i))  # return over week i, known at T_{i+1}
    # rows start at i = 2 and the last usable i is NW - 2 (needs F_{i+1})
    assert r["t"][0] == T0 + 2 * WEEK_MS and r["t"][-1] == T0 + (NW - 2) * WEEK_MS


def test_h1_x_ignores_usde_stamped_after_the_lag_cutoff() -> None:
    i = 6
    cut = T0 + (i + 1) * WEEK_MS - 2 * DAY_MS  # USDe stamps <= T_{i+1} - 2 d enter x_i
    base = h1_rows(panel())
    late = h1_rows(panel(usde_bump=(cut + 1, 5e5)))
    on = h1_rows(panel(usde_bump=(cut, 5e5)))
    j = base["t"].index(T0 + i * WEEK_MS)
    assert late["x"][: j + 1] == base["x"][: j + 1]  # a stamp after the cutoff leaves x_i and earlier unchanged
    assert late["x"][j + 1] != base["x"][j + 1]  # ... and moves x_{i+1}
    assert on["x"][j] != base["x"][j]  # a stamp exactly at the cutoff is used by x_i
    for key in ("y", "c1", "c2", "c3"):
        assert late[key] == base[key]


def test_h1_funding_after_t_changes_only_the_target() -> None:
    i = 6
    t_next = T0 + (i + 1) * WEEK_MS
    base = h1_rows(panel())
    pert = h1_rows(panel(fund_from=t_next))
    j = base["t"].index(T0 + i * WEEK_MS)
    for key in ("t", "y", "x", "c1", "c2", "c3", "lvl"):
        assert pert[key][:j] == base[key][:j], key  # earlier rows untouched
    assert pert["y"][j] != base["y"][j]
    for key in ("x", "c1", "c2", "c3", "lvl"):
        assert pert[key][j] == base[key][j], key


def test_h1_rows_4w_alignment_and_causality() -> None:
    w = panel()
    r = h1_rows_4w(w)
    i = 8
    j = r["t"].index(T0 + i * WEEK_MS)
    c = 1e-5 * 3 * 365
    m = [sum(c * k * k for k in range(a, a + 4)) / 4 for a in (i - 7, i - 3, i + 1)]
    assert r["lvl"][j] == pytest.approx(m[1])
    assert r["y"][j] == pytest.approx(m[2] - m[1])
    assert r["c1"][j] == pytest.approx(m[1] - m[0])
    assert r["x"][j] == pytest.approx(28e3 / (1000 + i + 1))
    assert r["c3"][j] == pytest.approx(sum(wk_ret(k) for k in range(i - 3, i + 1)))
    pert = h1_rows_4w(panel(fund_from=T0 + (i + 1) * WEEK_MS, usde_bump=(T0 + (i + 1) * WEEK_MS - 2 * DAY_MS + 1, 5e5)))
    assert pert["y"][j] != r["y"][j]
    for key in ("x", "c1", "c3", "lvl"):
        assert pert[key][: j + 1] == r[key][: j + 1], key


def test_h1_placebo_shift_moves_x() -> None:
    w = panel()
    base, sh = h1_rows(w), h1_rows(w, shift=1)
    j = base["t"].index(T0 + 6 * WEEK_MS)
    k = sh["t"].index(T0 + 6 * WEEK_MS)
    assert sh["x"][k] == pytest.approx(base["x"][j + 1])


def test_terciles() -> None:
    v = list(range(9))
    assert tercile_top(v) == [False] * 6 + [True] * 3
    ex = expanding_tercile_top([float(i) for i in range(5)], min_hist=3)
    assert ex[:2] == [None, None]
    assert ex[2:] == [True, True, True]  # increasing series: each new point is the max so far
    # causal: changing a later value does not change earlier flags
    assert expanding_tercile_top([0.0, 1.0, 2.0, -9.0, 4.0], min_hist=3)[:3] == ex[:3]
