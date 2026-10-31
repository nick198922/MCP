"""Tests for analytics/dvol_filter.py (item 8e of IDEA-20261031-structural-edges-depth)."""

from __future__ import annotations

import statistics

import pytest

from tradedesk.analytics.dvol_filter import (
    block_bootstrap_mean,
    cvar,
    entry_percentiles,
    filter_rows,
    max_drawdown,
    mean_without_top,
    placebo_gains,
    placebo_p,
    ruin_gate,
)
from tradedesk.analytics.put_skew import DAY_MS

T0 = 1_600_000_000_000 // DAY_MS * DAY_MS


def _candles(values):
    return [(T0 + i * DAY_MS, float(v)) for i, v in enumerate(values)]


def test_entry_percentile_uses_previous_day_close_and_strictly_earlier_values():
    # Increasing DVOL: the value known on each entry day is above all earlier ones -> percentile 1.0.
    pct = entry_percentiles(_candles(range(1, 11)), window_days=365, min_obs=3)
    first_entry = T0 + DAY_MS  # candle stamped T0 is known on T0 + 1 day
    assert first_entry not in pct and first_entry + DAY_MS not in pct and first_entry + 2 * DAY_MS not in pct
    assert pct[first_entry + 3 * DAY_MS] == pytest.approx(1.0)
    assert T0 not in pct  # nothing is known on the day of the first candle


def test_entry_percentile_mid_rank_and_window():
    vals = [10, 30, 20, 20]
    pct = entry_percentiles(_candles(vals), window_days=365, min_obs=2)
    # Entry T0+3d knows 20 (candle T0+2d); earlier known values 10, 30 -> rank 1 of 2 = 0.5.
    assert pct[T0 + 3 * DAY_MS] == pytest.approx(0.5)
    # Entry T0+4d knows 20; earlier 10, 30, 20 -> (bisect_left 1 + bisect_right 2) / 2 / 3 = 0.5.
    assert pct[T0 + 4 * DAY_MS] == pytest.approx(0.5)
    # Window of 1 day keeps only the previous value: 20 vs [30] -> 0.0 with min_obs 1.
    short = entry_percentiles(_candles(vals), window_days=1, min_obs=1)
    assert short[T0 + 3 * DAY_MS] == pytest.approx(0.0)


def test_entry_percentile_no_lookahead():
    base = [50 + (i % 7) * 3 - (i % 5) for i in range(400)]
    pct_a = entry_percentiles(_candles(base), window_days=365, min_obs=180)
    changed = base[:300] + [999.0] * 100  # alter every candle from index 300 on
    pct_b = entry_percentiles(_candles(changed), window_days=365, min_obs=180)
    cutoff = T0 + 300 * DAY_MS  # first entry that knows a changed candle is T0 + 301 d
    before = {d: v for d, v in pct_a.items() if d <= cutoff}
    assert before and before == {d: v for d, v in pct_b.items() if d <= cutoff}
    assert pct_b[T0 + 301 * DAY_MS] == pytest.approx(1.0)


def test_filter_rows_gain_is_minus_pnl_of_skipped_weeks():
    pnl = {1: 0.02, 2: -0.03, 3: 0.01, 4: 0.05}
    pct = {1: 0.9, 2: 0.1, 3: 0.4, 5: 0.7}  # day 4 has no percentile, day 5 no pnl
    rows = filter_rows(pnl, pct, 0.4)
    assert [r["day"] for r in rows] == [1, 2, 3]
    assert [r["selected"] for r in rows] == [True, False, True]  # 0.4 is selected (>=)
    assert [r["gain"] for r in rows] == pytest.approx([0.0, 0.03, 0.0])
    assert sum(r["filtered"] for r in rows) - sum(r["pnl"] for r in rows) == pytest.approx(sum(r["gain"] for r in rows))


def test_block_bootstrap_mean_constant_and_sign():
    res = block_bootstrap_mean([0.01] * 40, block=4, n_boot=500, seed=1)
    assert res["mean"] == pytest.approx(0.01)
    assert res["ci95"] == pytest.approx((0.01, 0.01))
    assert res["p"] == 0.0
    sym = block_bootstrap_mean([0.03, -0.01, -0.02, 0.01, -0.03, 0.02] * 15, block=4, n_boot=2000, seed=1)
    assert sym["mean"] == pytest.approx(0.0, abs=1e-15)
    assert sym["ci95"][0] < 0 < sym["ci95"][1]
    assert sym["p"] > 0.5
    pos = block_bootstrap_mean([0.05, -0.01] * 40, block=4, n_boot=2000, seed=1)
    assert pos["ci95"][0] > 0 and pos["p"] == 0.0
    assert block_bootstrap_mean([0.1] * 5, block=4)["ci95"] is None


def test_placebo_gains_expected_value_and_extremes():
    pnls = [0.03, -0.02, 0.01, -0.04, 0.02, 0.0]
    n, k = len(pnls), 3
    draws = placebo_gains(pnls, k, n_draws=20_000, seed=3)
    # E[gain] = -(1 - k/n) * mean(pnl)
    assert statistics.mean(draws) == pytest.approx(-(1 - k / n) * statistics.mean(pnls), abs=5e-4)
    # Best possible selection keeps the 3 largest: gain = -(sum of 3 smallest) / n.
    best = -sum(sorted(pnls)[:3]) / n
    assert max(draws) == pytest.approx(best)
    assert placebo_gains(pnls, n, n_draws=10) == pytest.approx([0.0] * 10)
    assert placebo_p(best + 1.0, draws) == pytest.approx(1 / (1 + len(draws)))
    with pytest.raises(ValueError):
        placebo_gains(pnls, 7)


def test_tail_helpers():
    v = [0.01, -0.05, 0.02, -0.01, 0.03]
    assert cvar(v, 0.2) == pytest.approx(-0.05)
    assert cvar(v, 0.4) == pytest.approx(-0.03)
    assert mean_without_top(v, 2) == pytest.approx((0.01 - 0.05 - 0.01) / 3)
    assert max_drawdown([0.01, -0.05, 0.02, -0.01, 0.03]) == pytest.approx(0.05)


def test_ruin_gate_forms():
    assert ruin_gate(-0.1065, 0.001, 0.004) == {"strict": True, "mechanical": True}
    assert ruin_gate(-0.1065, 0.004, 0.012) == {"strict": False, "mechanical": True}
    assert ruin_gate(-0.07, 0.0, 0.001) == {"strict": False, "mechanical": False}
    assert ruin_gate(-0.09, 0.0, None)["strict"] is False
