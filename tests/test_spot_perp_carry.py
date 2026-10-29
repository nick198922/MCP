import math

import pytest

from tradedesk.analytics import spot_perp_carry as spc

H = spc.HOUR_MS


def _flat(n: int, spot: float = 100.0, perp: float = 100.0, step: int = 4 * H) -> list[tuple[int, float, float]]:
    return [(k * step, spot, perp) for k in range(n + 1)]


def test_constant_base_rate_gives_analytic_apr():
    # 0.01 % / 8 h paid hourly = 0.0000125 per hour -> 10.95 % a year, no basis move, no costs
    n_h = 365 * 24
    funding = [((k + 1) * H, 0.0001 / 8) for k in range(n_h)]
    marks = _flat(n_h // 4)
    steps = spc.carry_steps(funding, marks, 0, n_h * H)
    s = spc.summarize(steps, 0, n_h * H, 12, round_trip_cost=0.0)
    assert s["funding_apr"] == pytest.approx(0.1095, abs=1e-9)
    assert s["net_apr"] == pytest.approx(0.1095, abs=1e-9)
    assert s["basis_apr"] == pytest.approx(0.0)
    assert s["block_mean"] == pytest.approx(0.1095, rel=1e-6)
    assert s["sd"] == pytest.approx(0.0, abs=1e-9)


def test_round_trip_cost_is_subtracted_once_per_window():
    n_h = 365 * 24
    marks = _flat(n_h // 4)
    steps = spc.carry_steps([], marks, 0, n_h * H)
    s = spc.summarize(steps, 0, n_h * H, 12, round_trip_cost=0.0023)
    assert s["net_apr"] == pytest.approx(-0.0023)
    assert s["block_mean"] == pytest.approx(-0.0023, rel=1e-4)  # blocks cover 182 or 183 4h steps


def test_basis_pnl_telescopes_to_minus_basis_change():
    # perp premium widens from 0 to +1 % while spot stays: the short perp loses 1 % of notional
    marks = [(0, 100.0, 100.0), (4 * H, 100.0, 100.5), (8 * H, 100.0, 101.0)]
    steps = spc.carry_steps([], marks, 0, 8 * H)
    assert sum(s["basis"] for s in steps) == pytest.approx(-0.01)
    path = [-0.005, -0.01]
    assert spc.max_drawdown(path) == pytest.approx(0.01)


def test_equal_coins_hedge_has_no_price_pnl_when_basis_is_constant():
    marks = [(0, 100.0, 100.0), (4 * H, 150.0, 150.0), (8 * H, 80.0, 80.0)]
    steps = spc.carry_steps([], marks, 0, 8 * H)
    assert all(s["basis"] == pytest.approx(0.0) for s in steps)


def test_rebalance_cost_on_price_move_beyond_band():
    marks = [(0, 100.0, 100.0), (4 * H, 110.0, 110.0), (8 * H, 130.0, 130.0), (12 * H, 140.0, 140.0)]
    steps = spc.carry_steps([], marks, 0, 12 * H, rebalance_band=0.2, rebalance_cost=0.002)
    assert steps[0]["cost"] == 0.0
    assert steps[1]["cost"] == pytest.approx((1 - 100 / 130) * 0.002)
    assert steps[2]["cost"] == 0.0  # reference reset to 130


def test_funding_assigned_to_half_open_intervals():
    marks = [(0, 1.0, 1.0), (4 * H, 1.0, 1.0), (8 * H, 1.0, 1.0)]
    funding = [(0, 9.0), (4 * H, 0.1), (5 * H, 0.2), (8 * H, 0.3), (9 * H, 5.0)]
    steps = spc.carry_steps(funding, marks, 0, 8 * H)
    assert [s["funding"] for s in steps] == pytest.approx([0.1, 0.5])


def test_results_before_t_do_not_depend_on_data_after_t():
    marks = [(k * 4 * H, 100.0 + k, 100.1 + k * 1.01) for k in range(60)]
    funding = [(k * H, 1e-5 * (k % 7 - 2)) for k in range(240)]
    cut = 30 * 4 * H
    base = spc.carry_steps(funding, marks, 0, 59 * 4 * H, rebalance_band=0.05, rebalance_cost=0.002)
    marks2 = [(t, s * (3.0 if t > cut else 1.0), p * (0.5 if t > cut else 1.0)) for t, s, p in marks]
    funding2 = [(t, r + (1.0 if t > cut else 0.0)) for t, r in funding]
    alt = spc.carry_steps(funding2, marks2, 0, 59 * 4 * H, rebalance_band=0.05, rebalance_cost=0.002)
    before = [s for s in base if s["ts"] <= cut]
    assert before == [s for s in alt if s["ts"] <= cut]
    assert base != alt


def test_coverage_and_align():
    assert spc.coverage([0, H, 3 * H, 3 * H + 5], 0, 4 * H, H) == pytest.approx(0.75)
    assert spc.align_marks([(0, 1.0), (1, 2.0)], [(1, 2.1), (2, 3.0)]) == [(1, 2.0, 2.1)]


def test_book_cost_walks_levels():
    asks = [(101.0, 1.0), (102.0, 10.0)]
    bids = [(99.0, 1.0), (98.0, 10.0)]
    # buying 203 USD: 1 coin at 101 + 1 coin at 102 -> VWAP 101.5 vs mid 100
    assert spc.book_cost(asks, 100.0, 203.0) == pytest.approx(0.015)
    assert spc.book_cost(asks, 100.0, 1e6) is None
    sl = spc.book_slippage(bids, asks, 50.0)
    assert sl["half_spread"] == pytest.approx(0.01)
    assert sl["avg"] == pytest.approx(0.01)


def test_mean_ci_mde_and_drop_top():
    m, (lo, hi) = spc.mean_ci([1.0, 2.0, 3.0])
    assert m == pytest.approx(2.0)
    assert hi - m == pytest.approx(4.303 * 1.0 / math.sqrt(3))
    assert spc.mde(4.0, 16) == pytest.approx(2.8)
    assert spc.drop_top([5.0, 1.0, 3.0, 4.0], 2) == [1.0, 3.0]


def test_block_bootstrap_contains_mean_and_is_deterministic():
    vals = [0.1, 0.05, 0.2, -0.1, 0.0, 0.15, 0.07, 0.12]
    a = spc.block_bootstrap_ci(vals)
    assert a == spc.block_bootstrap_ci(vals)
    assert a[0] < sum(vals) / len(vals) < a[1]


def test_cochran_q_homogeneous_vs_heterogeneous():
    hom = spc.cochran_q([0.05, 0.05, 0.05], [0.01, 0.01, 0.01])
    assert hom["q"] == pytest.approx(0.0)
    assert hom["i2"] == 0.0
    het = spc.cochran_q([0.0, 0.10, 0.20], [0.01, 0.01, 0.01])
    assert het["q"] == pytest.approx(200.0)  # (0.1 / 0.01)^2 * 2
    assert het["p"] < 1e-6
    assert het["i2"] == pytest.approx(0.99)  # (200 - 2) / 200
