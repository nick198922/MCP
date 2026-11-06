"""Direct tests for ten analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 179).

They feed the Polymarket gap collector and its paper forward (`pm_touch.question_direction`, checked on the live
`pm_gap_snapshot` sample, and the cluster bootstrap of `liquidation_flush` that `pm_gap_paper` uses), the research
reports (`funding_crowding.mde_nw`, `usde_funding.ols_residuals`, `holders_revenue.simple_return`,
`trader_persistence.top_index`) and the numbers of the markup, flow and strategy tools (`markup.ema_levels`,
`markup.fib_leg`, `market_flow.venue_latest`, `strategy.build_funding_carry`). References are hand-computed in the
comments; no output of the code under test is reused as an expected value.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from tradedesk.analytics.funding_crowding import mde_nw
from tradedesk.analytics.holders_revenue import simple_return
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means
from tradedesk.analytics.market_flow import venue_latest
from tradedesk.analytics.markup import ema_levels, fib_leg
from tradedesk.analytics.pm_touch import question_direction
from tradedesk.analytics.strategy import build_funding_carry, payoff_curve
from tradedesk.analytics.trader_persistence import top_index
from tradedesk.analytics.usde_funding import ols_residuals

HOUR = 3_600_000
PM_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pm_gap_snapshot_2026-10-21.json"

# ------------------------------------------------------------------ pm_touch.question_direction


def test_question_direction_matches_the_live_pm_gap_sample() -> None:
    # 86 rows of the live pm_gap_snapshot table (2026-10-21): the collector stored `direction` from this very function
    # for touch markets ("Will Bitcoin reach $X ..." / "Will Ethereum dip to $X ..."); "above" markets carry no direction
    d = json.loads(PM_FIXTURE.read_text())
    rows = [dict(zip(d["columns"], r, strict=True)) for r in d["rows"]]
    touch = [r for r in rows if r["kind"] == "touch"]
    above = [r for r in rows if r["kind"] == "above"]
    assert len(touch) == 27 and len(above) == 59
    for r in touch:
        assert question_direction(r["question"]) == r["direction"], r["question"]
    assert {r["direction"] for r in touch} == {"up", "down"}
    for r in above:
        assert question_direction(r["question"]) is None, r["question"]


def test_question_direction_rejects_ambiguous_or_empty_questions() -> None:
    assert question_direction("WILL BITCOIN REACH $95K ON OCTOBER 14?") == "up"  # case-insensitive
    assert question_direction("Will Ethereum reach $3,000 or dip to $2,000 in October?") is None  # both words
    assert question_direction("Will Bitcoin dip below $80,000?") is None  # "dip" without "to"
    assert question_direction("Will Bitcoin hit $100,000?") is None
    assert question_direction("") is None
    assert question_direction(None) is None  # type: ignore[arg-type]


# ------------------------------------------------------------------ liquidation_flush.cluster_bootstrap_means


def test_cluster_bootstrap_means_resamples_whole_clusters_into_a_pooled_mean() -> None:
    # day A: three values summing to 3 (count 3); day B: one value 4 (count 1). Two clusters drawn with replacement:
    # AA -> 6/6 = 1, AB or BA -> 7/4 = 1.75, BB -> 8/2 = 4, with probabilities 1/4, 1/2, 1/4
    vals, days = [0.5, 1.0, 1.5, 4.0], ["A", "A", "A", "B"]
    b = cluster_bootstrap_means(vals, days, n_boot=4000, seed=7)
    assert b.shape == (4000,)
    assert set(np.round(b, 12).tolist()) <= {1.0, 1.75, 4.0}
    assert np.mean(np.isclose(b, 1.0)) == pytest.approx(0.25, abs=0.03)
    assert np.mean(np.isclose(b, 1.75)) == pytest.approx(0.50, abs=0.03)
    assert np.mean(np.isclose(b, 4.0)) == pytest.approx(0.25, abs=0.03)
    # a ratio estimator: the bootstrap mean is near E = 0.25 + 0.875 + 1 = 2.125, not the pooled sample mean 7/4
    assert float(b.mean()) == pytest.approx(2.125, abs=0.05)
    # reproducible by seed
    assert np.array_equal(b, cluster_bootstrap_means(vals, days, n_boot=4000, seed=7))


def test_cluster_bootstrap_means_degenerate_inputs() -> None:
    # one cluster: every resample is that cluster, so every mean is the pooled mean (1 + 2 + 6) / 3 = 3
    one = cluster_bootstrap_means([1.0, 2.0, 6.0], [5, 5, 5], n_boot=50)
    assert one == pytest.approx(np.full(50, 3.0), rel=1e-12)
    assert cluster_bootstrap_means([], [], n_boot=50).size == 0
    with pytest.raises(ValueError):
        cluster_bootstrap_means([1.0, 2.0], ["A"], n_boot=10)  # misaligned clusters


# ------------------------------------------------------------------ funding_crowding.mde_nw


def test_mde_nw_is_z_times_the_newey_west_se() -> None:
    xs = [1.0, -1.0, 1.0, -1.0]
    # mean 0, gamma0 = 4/4 = 1; lag 0: SE = sqrt(1/4) = 0.5, MDE = 2.8 * 0.5 = 1.4
    assert mde_nw(xs, 0) == pytest.approx(1.4, rel=1e-12)
    # lag 1: gamma1 = (-1 - 1 - 1)/4 = -0.75, Bartlett weight 2 (1 - 1/2) = 1 -> S = 0.25, SE = sqrt(0.25/4) = 0.25
    assert mde_nw(xs, 1) == pytest.approx(0.7, rel=1e-12)
    assert mde_nw(xs, 1, z=1.96) == pytest.approx(0.49, rel=1e-12)
    assert mde_nw([0.3] * 10) == pytest.approx(0.0, abs=1e-15)  # constant series
    assert math.isnan(mde_nw([0.1]))  # n < 2


# ------------------------------------------------------------------ usde_funding.ols_residuals


def test_ols_residuals_hand_fit() -> None:
    # y = [1, 2, 4] on x = [0, 1, 2]: slope Sxy/Sxx = 3/2, intercept 7/3 - 3/2 = 5/6, fitted 5/6, 7/3, 23/6
    r = ols_residuals([1.0, 2.0, 4.0], [[0.0, 1.0, 2.0]])
    assert r == pytest.approx([1 / 6, -1 / 3, 1 / 6], abs=1e-12)
    # no controls: deviations from the mean 7/3
    assert ols_residuals([1.0, 2.0, 4.0], []) == pytest.approx([-4 / 3, -1 / 3, 5 / 3], abs=1e-12)


def test_ols_residuals_exact_fit_and_orthogonality() -> None:
    x1 = [0.0, 1.0, 2.0, 3.0, 4.0]
    x2 = [1.0, 0.0, 2.0, 5.0, 3.0]
    y = [2 + 3 * a - 0.5 * b for a, b in zip(x1, x2, strict=True)]
    assert ols_residuals(y, [x1, x2]) == pytest.approx([0.0] * 5, abs=1e-10)
    noisy = [v + e for v, e in zip(y, [0.3, -0.1, 0.2, -0.4, 0.1], strict=True)]
    r = ols_residuals(noisy, [x1, x2])
    # residuals are orthogonal to the constant and to every control
    assert float(r.sum()) == pytest.approx(0.0, abs=1e-10)
    assert float(r @ np.array(x1)) == pytest.approx(0.0, abs=1e-10)
    assert float(r @ np.array(x2)) == pytest.approx(0.0, abs=1e-10)


# ------------------------------------------------------------------ holders_revenue.simple_return


def test_simple_return_between_two_daily_closes() -> None:
    closes = {10: 100.0, 11: 110.0, 12: 0.0, 13: -5.0}
    assert simple_return(closes, 10, 11) == pytest.approx(0.10, rel=1e-12)
    assert simple_return(closes, 11, 10) == pytest.approx(-1 / 11, rel=1e-12)
    assert simple_return(closes, 10, 99) is None  # missing day
    assert simple_return(closes, 99, 10) is None
    assert simple_return(closes, 12, 10) is None  # zero entry price
    assert simple_return(closes, 13, 10) is None  # negative entry price
    # a zero exit close also gives None, not -100 % (`not pb`); daily closes are never 0, see docs/changes
    assert simple_return(closes, 10, 12) is None


# ------------------------------------------------------------------ trader_persistence.top_index


def test_top_index_takes_the_top_share_with_stable_ties() -> None:
    x = [0.3, 0.9, 0.5, 0.9, 0.1]
    # k = round(0.4 * 5) = 2: both 0.9 values, in input order
    assert top_index(x, 0.4).tolist() == [1, 3]
    # k = max(1, round(0.01 * 5)) = 1
    assert top_index(x, 0.01).tolist() == [1]
    # everybody, descending, ties by input order
    assert top_index(x, 1.0).tolist() == [1, 3, 2, 0, 4]
    # k = round(0.6 * 5) = 3
    assert top_index(x, 0.6).tolist() == [1, 3, 2]


# ------------------------------------------------------------------ markup.ema_levels / fib_leg


def test_ema_levels_groups_and_price_side() -> None:
    closes = [100.0] * 13 + [113.0] * 8
    # EMA13: seed SMA 100 at bar 13, then 8 bars of 113 with k = 2/14 = 1/7 -> 113 - 13 (6/7)^8
    e13 = 113.0 - 13.0 * (6 / 7) ** 8  # 109.2175...
    # EMA21: only the seed, SMA of 21 bars = (1300 + 904) / 21
    e21 = 2204.0 / 21.0  # 104.952...
    out = ema_levels(107.0, closes, None, [])
    tf = out["trend_13_21_34"]
    assert tf["ema13"] == pytest.approx(e13, rel=1e-12)
    assert tf["ema21"] == pytest.approx(e21, rel=1e-12)
    assert tf["ema34"] is None  # fewer than 34 closes
    assert tf["price_vs"] == "inside"
    assert out["daily_4h_100_200"] is None and out["weekly_1d_100_200"] is None
    assert ema_levels(110.0, closes, None, None)["trend_13_21_34"]["price_vs"] == "above_all"
    assert ema_levels(104.0, closes, None, None)["trend_13_21_34"]["price_vs"] == "below_all"
    # 150 four-hour closes: EMA100 known, EMA200 not; a group with no known EMA at all is None
    h4 = [50.0] * 150
    grp = ema_levels(60.0, closes, h4, [70.0] * 50)
    assert grp["daily_4h_100_200"]["ema100"] == pytest.approx(50.0, rel=1e-12)
    assert grp["daily_4h_100_200"]["ema200"] is None and grp["daily_4h_100_200"]["price_vs"] == "above_all"
    assert grp["weekly_1d_100_200"] is None


def test_fib_leg_levels_zone_and_ote() -> None:
    up = fib_leg(100.0, 200.0, "up", 130.0)
    # up leg: 0 at the high, level f = 200 - 100 f
    assert up["levels"] == pytest.approx({"0.5": 150.0, "0.618": 138.2, "0.705": 129.5, "0.786": 121.4}, rel=1e-12)
    assert up["price_zone"] == "discount" and up["in_ote"] is True  # 121.4 <= 130 <= 138.2
    assert up["low"] == 100.0 and up["high"] == 200.0 and up["direction"] == "up"
    assert fib_leg(100.0, 200.0, "up", 140.0)["in_ote"] is False
    down = fib_leg(100.0, 200.0, "down", 175.0)
    # down leg: 0 at the low, level f = 100 + 100 f
    assert down["levels"] == pytest.approx({"0.5": 150.0, "0.618": 161.8, "0.705": 170.5, "0.786": 178.6}, rel=1e-12)
    assert down["price_zone"] == "premium" and down["in_ote"] is True
    assert fib_leg(100.0, 200.0, "down", 150.0)["price_zone"] == "equilibrium"


# ------------------------------------------------------------------ market_flow.venue_latest


def test_venue_latest_per_venue_levels_funding_and_liquidations() -> None:
    t_close = 10 * HOUR
    out = venue_latest(
        t_close,
        oi={"binance": [(8 * HOUR, 1000.0), (9 * HOUR, 1100.0)], "bybit": [(7 * HOUR, 500.0)]},
        funding={"binance": ([(8 * HOUR, 0.0001)], 8.0), "bybit": ([(0, 0.0002)], 1.0)},
        long_share={"binance": [(10 * HOUR, 0.55)], "okx": [(10 * HOUR, 0.6)]},
        liquidations={"binance": {5 * HOUR: (50.0, 50.0), 8 * HOUR: (1.0, 2.0), 9 * HOUR: (3.0, 4.0), 10 * HOUR: (100.0, 100.0)}},
        since_ms=8 * HOUR,
    )
    assert set(out) == {"binance", "bybit"}  # okx is not a flow venue
    b, y = out["binance"], out["bybit"]
    assert b["oi_coins"] == 1100.0  # latest point, 1 h old (limit 2 h)
    assert y["oi_coins"] is None  # 3 h old
    # funding 0.01 % per 8 h: annualised 0.0001 * 8760 / 8 = 0.1095; a point is valid for two intervals (16 h)
    assert b["funding_rate"] == 0.0001 and b["funding_interval_hours"] == 8.0
    assert b["funding_annualized"] == pytest.approx(0.1095, rel=1e-12)
    # bybit hourly funding 10 h old > 2 intervals: no rate, no annualised value
    assert y["funding_rate"] is None and y["funding_annualized"] is None and y["funding_interval_hours"] == 1.0
    assert b["long_share"] == 0.55  # a point exactly at the close counts
    # bars opened in [since, t_close): 8 h and 9 h only
    assert b["liq_long"] == 4.0 and b["liq_short"] == 6.0
    assert "long_share" not in y and "liq_long" not in y
    assert venue_latest(t_close, oi={}, funding={}, long_share={}, liquidations={}, since_ms=0) == {}


# ------------------------------------------------------------------ strategy.build_funding_carry


def test_build_funding_carry_legs_lock_the_mark_spread() -> None:
    legs = build_funding_carry(long_mark=100.0, short_mark=101.0, size=2.0)
    assert legs == [
        {"kind": "linear", "side": "long", "size": 2.0, "entry": 100.0},
        {"kind": "linear", "side": "short", "size": 2.0, "entry": 101.0},
    ]
    # price PnL is flat in S: 2 (S - 100) - 2 (S - 101) = 2 (101 - 100) = 2, funding not included
    assert payoff_curve(legs, [50.0, 100.0, 150.0]) == [2.0, 2.0, 2.0]
    assert build_funding_carry(long_mark=1.0, short_mark=1.0)[0]["size"] == 1.0
