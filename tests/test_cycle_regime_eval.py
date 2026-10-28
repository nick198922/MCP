"""Step 3 of IDEA-20261028-btc-cycle-regime-gauge: the pure history-test helpers in analytics/cycle_regime_eval.py."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import cycle_regime as cr
from tradedesk.analytics import cycle_regime_eval as ev

D = cr.DAY_MS
T0 = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp() * 1000)


def _synthetic(n: int = 900, shock_after: int | None = None):
    """Spot with a slow cycle, perp 0.05 % under spot, 8-hourly funding; after `shock_after` days everything changes."""
    spot, perp, funding = {}, {}, []
    for i in range(n):
        t = T0 + i * D
        c = 10_000 * math.exp(0.6 * math.sin(i / 120.0) + 0.0005 * i)
        if shock_after is not None and i > shock_after:
            c *= 3.0
        spot[t] = c
        perp[t] = c * (1 - 0.0005 if shock_after is None or i <= shock_after else 1.01)
        for k in range(3):
            rate = 0.00002 * math.sin(i / 50.0)
            if shock_after is not None and i > shock_after:
                rate = 0.001
            funding.append((t + D + k * 8 * 3_600_000 - 2 * 8 * 3_600_000, rate))
    return spot, perp, funding


def test_forward_returns_analytic():
    spot = {T0 + i * D: 100 * 1.01**i for i in range(100)}
    r = ev.forward_returns(spot, 30, T0 + 99 * D)
    assert r[T0] == pytest.approx(1.01**30 - 1)
    assert T0 + 69 * D in r and T0 + 70 * D not in r  # the bar 30 days later must exist
    del spot[T0 + 40 * D]
    r2 = ev.forward_returns(spot, 30, T0 + 99 * D)
    assert T0 + 10 * D not in r2  # missing bar -> no return, never a neighbour's
    r3 = ev.forward_returns(spot, 30, T0 + 50 * D)  # sample end bounds the exit day
    assert max(r3) == T0 + 20 * D


def test_episodes_merge_gap():
    days = [T0 + i * D for i in range(20)]
    flags = [False] * 20
    for i in (1, 2, 3, 6, 15):
        flags[i] = True
    # gap between day 3 and day 6 is 2 empty days; day 15 is 8 empty days after day 6
    assert ev.episodes(days, flags, 2) == [(days[1], days[6], 4), (days[15], days[15], 1)]
    assert ev.episodes(days, flags, 1) == [(days[1], days[3], 3), (days[6], days[6], 1), (days[15], days[15], 1)]
    assert ev.episodes(days, flags, 8) == [(days[1], days[15], 5)]
    assert ev.episodes(days, [False] * 20, 30) == []


def test_masked_diff():
    y = [1.0, 2.0, 3.0, 6.0]
    m = [False, False, True, True]
    assert ev.masked_diff(y, m) == pytest.approx(4.5 - 3.0)
    assert ev.masked_diff(y, m, vs_rest=True) == pytest.approx(4.5 - 1.5)
    assert ev.masked_diff(y, [False] * 4) is None
    assert ev.masked_diff(y, [True] * 4, vs_rest=True) is None


def test_month_block_bootstrap_constant_and_bounds():
    days = [T0 + i * D for i in range(366)]
    months = ev.month_index(days)
    assert months[0] == 0 and months[-1] == 11  # 2020 is a leap year: 366 days = 2020-01-01 .. 2020-12-31
    mask = np.zeros(366, dtype=bool)
    mask[40:70] = True
    flat = ev.month_block_bootstrap(np.full(366, 0.05), mask, months, block=2, reps=500)
    assert flat["lo"] == pytest.approx(0.0, abs=1e-12) and flat["hi"] == pytest.approx(0.0, abs=1e-12)
    assert 0 < flat["share_empty"] < 1  # the mask lives in ~2 of 13 months: some resamples miss it
    ind = ev.month_block_bootstrap(mask.astype(float), mask, months, block=2, reps=500)
    assert 0 < ind["lo"] <= ind["hi"] < 1  # y = 1 on the mask: stat = 1 - share of mask days in the resample


def test_shift_placebo_unique_alignment_gets_minimal_p():
    n = 400
    y = np.zeros(n)
    y[100:120] = 1.0
    mask = np.zeros(n, dtype=bool)
    mask[100:120] = True
    res = ev.shift_placebo(y, mask, k_min=30, direction=1)
    assert res["n_shifts"] == n - 60 + 1
    assert res["p"] == pytest.approx(1 / (1 + res["n_shifts"]))
    neg = ev.shift_placebo(-y, mask, k_min=30, direction=-1)
    assert neg["p"] == pytest.approx(res["p"])
    wrong = ev.shift_placebo(y, mask, k_min=30, direction=-1)
    assert wrong["p"] == pytest.approx(1.0)


def test_trend_residual_zero_mean_per_side():
    y = np.array([0.1, 0.3, -0.2, 0.0, 0.5])
    above = np.array([True, True, False, False, True])
    r = ev.trend_residual(y, above)
    assert r[above].mean() == pytest.approx(0.0, abs=1e-15)
    assert r[~above].mean() == pytest.approx(0.0, abs=1e-15)
    assert r[0] == pytest.approx(0.1 - 0.3)


def test_mde_and_t_quantiles():
    assert ev.mde(0.18, 15) == pytest.approx(2.8 * 0.18 / math.sqrt(15))
    assert ev.mde(0.18, 0) is None
    for df, t in ((3, 3.182), (5, 2.571), (10, 2.228), (12, 2.179), (20, 2.086), (30, 2.042), (120, 1.980)):
        assert ev.t975(df) == pytest.approx(t, abs=0.005)
    ci = ev.t_ci([1.0, 2.0, 3.0])
    assert ci["mean"] == pytest.approx(2.0) and ci["sd"] == pytest.approx(1.0)
    assert ci["hi"] - ci["mean"] == pytest.approx(4.303 / math.sqrt(3), abs=1e-3)
    assert ev.t_ci([0.5])["lo"] is None


def test_state_series_matches_the_tool_function():
    spot, perp, funding = _synthetic()
    t = T0 + 800 * D
    rows = ev.state_series(spot, perp, funding, T0 + 790 * D, t)
    assert [r["t"] for r in rows] == [T0 + k * D for k in range(790, 801)]
    for r in rows:
        now = datetime.fromtimestamp((r["t"] + D) / 1000, UTC)
        tool = cr.cycle_regime(spot, perp, funding, {}, now)
        assert r["state"] == tool["state"]
        assert r["close"] == tool["components"]["close"]
        assert r["funding7"] == pytest.approx(tool["components"]["funding7"])
        assert r["basis7"] == pytest.approx(tool["components"]["basis7"])
    assert all(r["state"] is not None for r in rows)
    assert all(r["basis7"] == pytest.approx(-0.0005) for r in rows)


def test_states_before_t_do_not_change_when_later_data_changes():
    base = _synthetic()
    shocked = _synthetic(shock_after=820)
    a = ev.state_series(*base, T0 + 700 * D, T0 + 820 * D)
    b = ev.state_series(*shocked, T0 + 700 * D, T0 + 820 * D)
    assert a == b
    later_a = ev.state_series(*base, T0 + 830 * D, T0 + 830 * D)
    later_b = ev.state_series(*shocked, T0 + 830 * D, T0 + 830 * D)
    assert later_a != later_b  # the shock is visible once it is in the past
