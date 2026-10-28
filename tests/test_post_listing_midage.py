"""Post-listing mid-age decay: window, turnover filter, hedged short leg and the mean-free power gate."""

from __future__ import annotations

import math
import statistics

import pytest

from tradedesk.analytics.listing_drift import DAY_MS, DailyBars
from tradedesk.analytics.post_listing_midage import (
    Gate,
    median_turnover,
    power_gate,
    relative_short,
    window_days,
)


def bars(days: dict[int, tuple[float, float, float]], qv: float = 1e6) -> DailyBars:
    """days: day -> (open, high, close); low = min(open, close)."""
    return DailyBars.from_rows([[d * DAY_MS, o, h, min(o, c), c, 1.0, qv] for d, (o, h, c) in days.items()])


def flat(d0: int, d1: int, px: float) -> dict[int, tuple[float, float, float]]:
    return {d: (px, px, px) for d in range(d0, d1 + 1)}


def test_window_days_age_30_to_90():
    assert window_days(100) == (130, 189)  # 60 daily bars: open of age 30 .. close of age 89
    assert window_days(100, entry_age=150) == (250, 309)


def test_median_turnover_strictly_before_entry_and_min_days():
    qv = {d: float(d) for d in range(0, 40)}
    assert median_turnover(qv, 30) == pytest.approx(statistics.median(range(0, 30)))
    assert median_turnover(qv, 15) is None  # only 15 days before day 15
    assert median_turnover(qv, 15, min_days=15) == pytest.approx(7.0)


def test_median_turnover_ignores_data_from_entry_on():
    qv = {d: 1.0 for d in range(0, 30)}
    a = median_turnover(qv, 30)
    qv.update({d: 1e12 for d in range(30, 60)})
    assert median_turnover(qv, 30) == a == 1.0


def test_relative_short_analytic():
    coin = bars({**flat(10, 14, 100.0), 14: (100.0, 100.0, 80.0)})
    m = {"A": bars({**flat(10, 14, 50.0), 14: (50.0, 50.0, 55.0)}), "B": bars({**flat(10, 14, 10.0), 14: (10.0, 10.0, 13.0)})}
    r = relative_short(coin, ["A", "B"], m, 10, 5)
    assert r is not None
    assert r.short_pnl == pytest.approx(0.20)
    assert r.basket_ret == pytest.approx(0.20)  # mean of +10 % and +30 %
    assert r.rel == pytest.approx(0.40)
    assert (r.entry, r.exit, r.liquidated, r.truncated) == (10, 14, False, False)


def test_relative_short_liquidation_1x_and_2x_closes_hedge_same_day():
    coin = bars({10: (100.0, 100.0, 100.0), 11: (100.0, 160.0, 150.0), 12: (150.0, 200.0, 190.0), 13: (190.0, 190.0, 190.0)})
    m = {"A": bars({10: (10.0, 10.0, 10.0), 11: (10.0, 10.0, 11.0), 12: (11.0, 12.0, 12.0), 13: (12.0, 12.0, 12.0)})}
    one = relative_short(coin, ["A"], m, 10, 4)  # 1x: liquidated when high >= 197.5 (day 12)
    assert one is not None and one.liquidated and one.exit == 12
    assert one.short_pnl == pytest.approx(-1.0)
    assert one.basket_ret == pytest.approx(0.20)
    two = relative_short(coin, ["A"], m, 10, 4, leverage=2.0)  # 2x: high >= 147.5 on day 11, loses the margin
    assert two is not None and two.liquidated and two.exit == 11
    assert two.short_pnl == pytest.approx(-0.5)
    assert two.basket_ret == pytest.approx(0.10)


def test_relative_short_unchanged_by_data_after_exit():
    base = {**flat(10, 14, 100.0), 14: (100.0, 100.0, 90.0)}
    mb = flat(10, 14, 20.0)
    r1 = relative_short(bars(base), ["A"], {"A": bars(mb)}, 10, 5)
    later = {d: (999.0, 5000.0, 1.0) for d in range(15, 40)}
    r2 = relative_short(bars({**base, **later}), ["A"], {"A": bars({**mb, **later})}, 10, 5)
    assert r1 == r2


def test_relative_short_none_without_entry_or_basket():
    coin = bars(flat(11, 14, 100.0))
    assert relative_short(coin, ["A"], {"A": bars(flat(10, 14, 1.0))}, 10, 5) is None
    coin = bars(flat(10, 14, 100.0))
    assert relative_short(coin, ["A"], {"A": bars(flat(11, 14, 1.0))}, 10, 5) is None


def test_power_gate_independent_clusters():
    # one observation per cluster: ICC is not identified -> 0, deff 1, MDE = 2.8 * sd / sqrt(n)
    vals = [(-1.0) ** i * 0.5 for i in range(200)]
    g = power_gate(vals, list(range(200)))
    sd = statistics.stdev(vals)
    assert g.n == 200 and g.clusters == 200
    assert g.deff == pytest.approx(1.0)
    assert g.n_eff == pytest.approx(200.0)
    assert g.mde_icc == pytest.approx(2.8 * sd / math.sqrt(200))
    assert not hasattr(g, "mean")


def test_power_gate_full_clustering_inflates_mde():
    # 10 clusters of 20 identical values: ICC 1, deff = 20, n_eff = 10
    vals = [float(c) for c in range(10) for _ in range(20)]
    cl = [c for c in range(10) for _ in range(20)]
    g = power_gate(vals, cl)
    assert g.icc == pytest.approx(1.0)
    assert g.deff == pytest.approx(20.0)
    assert g.n_eff == pytest.approx(10.0)
    assert g.mde_icc == pytest.approx(2.8 * statistics.stdev(vals) / math.sqrt(10))


def test_gate_pass_rule():
    mk = lambda n, a, b: Gate(n, 50, 0.4, 0.0, 1.0, n, a, b, 150, 0.06)  # noqa: E731
    assert mk(150, 0.06, 0.05).passed
    assert not mk(149, 0.05, 0.05).passed
    assert not mk(300, 0.061, 0.05).passed
    assert not mk(300, 0.05, 0.07).passed  # conservative: both MDE estimates must clear the bar
