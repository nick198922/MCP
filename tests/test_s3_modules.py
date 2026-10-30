"""S3 modules (item 7b): analytic expectations and no-lookahead checks."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics import s3_modules as m
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import Fees, StopRule, risk_unit, simulate_stop_rule

H = m.HOUR_MS
T0 = int(datetime(2025, 3, 3, tzinfo=UTC).timestamp() * 1000)  # a Monday
F = Fees()
RULE_B = StopRule("B", be_frac=0.5)


def bars(rows: list[tuple[float, float, float, float]], t0: int = T0) -> Bars:
    return Bars.from_rows([[t0 + i * H, o, h, lo, c] for i, (o, h, lo, c) in enumerate(rows)], H)


def test_partial_exit_runner_reaches_far() -> None:
    p = bars([(100, 111, 99.5, 110.5), (111, 121, 110, 120.5), (120, 121, 119, 120)])
    base = simulate_stop_rule(p, T0, 100, 90, 110, 10 * H, RULE_B, fees=F)
    assert base is not None and base.exit_kind == "target"
    r, run = m.partial_exit(p, T0, 100, 90, 110, 120, 10 * H, F, base)
    ru = risk_unit(100, 90, F)
    assert run is not None and run.exit_kind == "target"
    assert run.r_net == pytest.approx((20 - F.maker * 100 - F.maker * 120) / ru)
    assert r == pytest.approx(0.5 * base.r_net + 0.5 * run.r_net)


def test_partial_exit_runner_break_even_after_mid() -> None:
    p = bars([(100, 111, 99.5, 110.5), (110, 112, 100.0, 101), (101, 102, 100.5, 101)])
    base = simulate_stop_rule(p, T0, 100, 90, 110, 10 * H, RULE_B, fees=F)
    assert base is not None
    _, run = m.partial_exit(p, T0, 100, 90, 110, 120, 10 * H, F, base)
    assert run is not None and run.exit_kind == "be"
    be = 100 * (1 + F.be_offset)
    assert run.exit == pytest.approx(be)
    ru = risk_unit(100, 90, F)
    assert run.r_net == pytest.approx((be - 100 - F.maker * 100 - F.taker_exit * be) / ru)


def test_partial_exit_same_as_base_before_mid() -> None:
    # stopped before reaching mid: both halves lose the same
    p = bars([(100, 103, 95, 96), (96, 97, 89, 90)])
    base = simulate_stop_rule(p, T0, 100, 90, 110, 10 * H, RULE_B, fees=F)
    assert base is not None
    r, run = m.partial_exit(p, T0, 100, 90, 110, 120, 10 * H, F, base)
    assert run is not None and run.r_net == pytest.approx(base.r_net) and r == pytest.approx(base.r_net)


def test_partial_exit_bad_levels() -> None:
    p = bars([(100, 101, 99, 100)])
    base = simulate_stop_rule(p, T0, 100, 90, 110, 10 * H, RULE_B, fees=F)
    assert base is not None
    with pytest.raises(ValueError):
        m.partial_exit(p, T0, 100, 90, 110, 110, 10 * H, F, base)


def test_split_entry_filled_and_rescaled() -> None:
    # leg 2 limit at 95 fills in bar 0 (low 94 through 95, stop 90 not touched), then the target 110 in bar 2
    p = bars([(100, 101, 94, 96), (96, 104, 95.5, 103), (103, 111, 102, 110), (110, 111, 109, 110)])
    base = simulate_stop_rule(p, T0, 100, 90, 110, 10 * H, RULE_B, fees=F)
    assert base is not None and base.exit_kind == "target"
    lvl = m.split_level(100, 90.0 + 0.0, 0.5)
    assert lvl == pytest.approx(95)
    r, filled, r2 = m.split_entry(p, T0, 100, 89, 110, 95, 12 * H, 10 * H, F, base, RULE_B)
    assert filled
    leg = simulate_stop_rule(p, T0 + H, 95, 89, 110, 9 * H, RULE_B, fees=F)
    assert leg is not None and leg.exit_kind == "target"
    assert r2 == pytest.approx(leg.r_net * risk_unit(95, 89, F) / risk_unit(100, 89, F))
    assert r == pytest.approx(0.5 * base.r_net + 0.5 * r2)


def test_split_entry_unfilled_is_half_base() -> None:
    p = bars([(100, 103, 99, 102), (102, 111, 101, 110), (110, 111, 96, 97)])
    base = simulate_stop_rule(p, T0, 100, 90, 110, 10 * H, RULE_B, fees=F)
    assert base is not None and base.exit_kind == "target"
    # the dip to 96 after the base's exit bar must not fill (the limit is cancelled with the first half)
    r, filled, r2 = m.split_entry(p, T0, 100, 90, 110, 97, 12 * H, 10 * H, F, base, RULE_B)
    assert not filled and r2 == 0.0 and r == pytest.approx(0.5 * base.r_net)


def test_split_entry_fill_bar_hits_stop_is_loss() -> None:
    p = bars([(100, 101, 88, 89), (89, 90, 85, 86)])
    base = simulate_stop_rule(p, T0, 100, 90, 110, 10 * H, RULE_B, fees=F)
    assert base is not None and base.exit_kind.startswith("stop")
    r, filled, r2 = m.split_entry(p, T0, 100, 90, 110, 95, 12 * H, 10 * H, F, base, RULE_B)
    assert filled and r2 < 0


def test_edge_touches_counts_and_is_causal() -> None:
    low = np.full(40, 10.0)
    low[12], low[20], low[28] = 5.0, 5.1, 7.0  # two at the edge, one far
    s = 35
    assert m.edge_touches(low, s, 30, atr_prev=1.0) == 2
    assert m.edge_touches(low, s, 30, atr_prev=0.05) == 1
    later = low.copy()
    later[s:] = 0.1
    assert m.edge_touches(later, s, 30, atr_prev=1.0) == 2
    # a fractal whose right confirming bars include the sweep bar does not count
    low2 = low.copy()
    low2[s - 1] = 5.05
    assert m.edge_touches(low2, s, 30, atr_prev=1.0) == 2


def test_htf_lows_previous_periods_and_causal() -> None:
    start = int(datetime(2025, 1, 25, tzinfo=UTC).timestamp() * 1000)
    n = 24 * 40
    t = start + np.arange(n, dtype=np.int64) * H
    rng = np.random.default_rng(1)
    low = 100 + rng.normal(0, 1, n)
    ref = int(datetime(2025, 2, 11, 8, tzinfo=UTC).timestamp() * 1000)  # Tuesday
    lv = m.htf_lows(t, low, ref)
    d0 = int(datetime(2025, 2, 10, tzinfo=UTC).timestamp() * 1000)
    w0 = int(datetime(2025, 2, 3, tzinfo=UTC).timestamp() * 1000)
    m0 = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    m1 = int(datetime(2025, 2, 1, tzinfo=UTC).timestamp() * 1000)
    assert lv["day"] == pytest.approx(low[(t >= d0) & (t < d0 + 24 * H)].min())
    assert lv["week"] == pytest.approx(low[(t >= w0) & (t < w0 + 7 * 24 * H)].min())
    assert math.isnan(lv["month"])  # January: 7 of 31 days covered < 50 %
    assert m.htf_lows(t, low, ref, min_cover=0.2)["month"] == pytest.approx(low[(t >= m0) & (t < m1)].min())
    later = low.copy()
    later[t >= d0 + 24 * H] = 0.0
    assert m.htf_lows(t, later, ref) == lv
    hit, d = m.htf_match(lv["day"] + 0.2, lv, 1.0)
    assert hit and d <= 0.2 + 1e-12
    assert not m.htf_match(lv["day"] - 50, lv, 1.0)[0]


def test_htf_lows_monday_uses_previous_week() -> None:
    t = T0 - 14 * 24 * H + np.arange(15 * 24, dtype=np.int64) * H
    low = np.arange(t.size, dtype=float)
    lv = m.htf_lows(t, low, T0 + 4 * H)
    assert lv["week"] == pytest.approx(low[t == T0 - 7 * 24 * H][0])
    assert lv["day"] == pytest.approx(low[t == T0 - 24 * H][0])


def test_oi_change_and_staleness() -> None:
    obs = np.array([0, 300_000, 600_000, 3_600_000], dtype=np.int64)
    oi = np.array([100.0, 101.0, 102.0, 90.0])
    assert m.oi_change(obs, oi, 600_000, 3_600_000) == pytest.approx(90 / 102 - 1)
    assert math.isnan(m.oi_change(obs, oi, 600_000, 3_000_000))  # last obs 40 min old
    # a later observation does not change the value at t
    obs2 = np.append(obs, 3_700_000)
    oi2 = np.append(oi, 1.0)
    assert m.oi_change(obs2, oi2, 600_000, 3_600_000) == pytest.approx(90 / 102 - 1)


def test_quintiles_and_placebo() -> None:
    x = np.arange(100, dtype=float)
    e = m.quintile_edges(x)
    q = m.quintile_of(np.array([0.0, 50.0, 99.0, math.nan]), e)
    assert q.tolist() == [0, 2, 4, -1]
    v = np.ones(50)
    obs, pl = m.subgroup_placebo(v, np.arange(50) < 10, n_draw=50)
    assert obs == pytest.approx(0) and np.allclose(pl, 0)
    v2 = np.r_[np.ones(10), np.zeros(40)]
    obs2, pl2 = m.subgroup_placebo(v2, np.arange(50) < 10, n_draw=200)
    assert obs2 == pytest.approx(1 - 0.2) and np.mean(pl2 >= obs2) < 0.01


def test_unit_draw_and_one_sided() -> None:
    a = m.unit_draw("BTC|4h|long|1", "h9")
    assert 0 < a < 1 and a == m.unit_draw("BTC|4h|long|1", "h9") and a != m.unit_draw("BTC|4h|long|1", "h10")
    assert m.one_sided_p(0.1, 0.04) == pytest.approx(0.02)
    assert m.one_sided_p(-0.1, 0.04) == pytest.approx(0.98)
    assert m.weighted_mean(np.array([1.0, 3.0]), np.array([1.0, 3.0])) == pytest.approx(2.5)
