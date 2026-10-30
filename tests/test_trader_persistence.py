import math

import numpy as np
import pytest

from tradedesk.analytics.trader_persistence import (
    PairMetrics,
    PosState,
    bad_period,
    describe_pair,
    drop_top_abs,
    make_pair,
    mde,
    mean_ci,
    open_cost,
    pair_metrics,
    period_of,
    period_pnl,
    power_gate,
    q_stat,
    rank_placebo,
    ranks,
    settled_value,
    spearman,
    stay_top,
    tokens_bought,
    weighted_mean,
)

M = 1_000_000


def test_settled_value_books_resolved_open_positions():
    s = {"a": PosState(amount=10 * M, avg_price=400_000, realized=2 * M, total_bought=10 * M)}
    assert settled_value(s, {"a": 1.0}) == pytest.approx((8.0, 2.0))  # 2 + 10 * (1 - 0.4)
    assert settled_value(s, {"a": 0.0}) == pytest.approx((-2.0, 2.0))  # unredeemed loser: 2 - 10 * 0.4
    assert settled_value(s, {}) == pytest.approx((2.0, 2.0))  # unresolved: realized only
    closed = {"a": PosState(amount=0, avg_price=400_000, realized=6 * M, total_bought=10 * M)}
    assert settled_value(closed, {"a": 1.0}) == pytest.approx((6.0, 6.0))  # redeemed: no double count


def test_redemption_moves_value_from_settled_to_realized_without_change():
    before = {"a": PosState(10 * M, 400_000, 0, 10 * M)}
    after = {"a": PosState(0, 400_000, 6 * M, 10 * M)}  # redeemed at payout 1: realized += 10 * 0.6
    assert settled_value(before, {"a": 1.0})[0] == pytest.approx(settled_value(after, {"a": 1.0})[0])


def test_tokens_bought_counts_new_and_increased():
    a = {"x": PosState(1, 1, 0, 5), "y": PosState(1, 1, 0, 5)}
    b = {"x": PosState(1, 1, 0, 5), "y": PosState(1, 1, 0, 7), "z": PosState(1, 1, 0, 1)}
    assert tokens_bought(a, b) == {"y", "z"}
    assert tokens_bought(a, {"y": PosState(0, 1, 3, 5)}) == set()  # a sale (delta row) is not a purchase


def test_open_cost_splits_unpaid():
    s = {
        "a": PosState(10 * M, 400_000, 0, 10 * M),  # resolved: cost 4
        "b": PosState(5 * M, 200_000, 0, 5 * M),  # unresolved: cost 1
        "c": PosState(0, 900_000, 1 * M, 3 * M),  # closed: no cost
    }
    assert open_cost(s, {"a": 1.0}) == pytest.approx((5.0, 1.0))
    assert open_cost(s, {"a": 0.0, "b": 1.0}) == pytest.approx((5.0, 0.0))


def test_period_of_boundary_rows_belong_to_the_period_they_close():
    idx = [0, 3, 7]  # boundaries of two periods: (0, 3] and (3, 7]
    assert period_of(0, idx) is None  # the starting snapshot is in no period
    assert [period_of(k, idx) for k in (1, 2, 3)] == [0, 0, 0]
    assert [period_of(k, idx) for k in (4, 7)] == [1, 1]
    assert period_of(8, idx) is None


def _utc(y: int, m: int, d: int, s: int = 0) -> int:
    from datetime import UTC, datetime

    return int(datetime(y, m, d, tzinfo=UTC).timestamp()) + s


def test_bad_period_gap_with_tolerance():
    gap = {"gap_from": _utc(2025, 10, 1), "gap_to": _utc(2025, 12, 1)}
    kw = {"min_days": 20, "max_days": 40, **gap}
    # September ends at 00:00:03 on 1 October: a few seconds late, it does not touch the gap
    assert bad_period(_utc(2025, 9, 1), _utc(2025, 10, 1, 3), **kw) is False
    assert bad_period(_utc(2025, 10, 1, 2), _utc(2025, 11, 1, 5), **kw) is True  # inside the gap
    assert bad_period(_utc(2025, 11, 1), _utc(2025, 12, 1, 4), **kw) is True  # ends at the gap's end, starts inside
    assert bad_period(_utc(2025, 12, 1, 4), _utc(2026, 1, 1), **kw) is False  # starts a few seconds after the gap
    # a period that reaches into the gap is excluded
    assert bad_period(_utc(2025, 9, 15), _utc(2025, 10, 15), **kw) is True
    # without the tolerance the late boundary would wrongly touch the gap (the bug of the first run)
    assert bad_period(_utc(2025, 9, 1), _utc(2025, 10, 1, 3), tolerance_s=0, **kw) is True
    # length out of range
    assert bad_period(_utc(2025, 5, 1), _utc(2025, 5, 8), **kw) is True
    no_gap = {"min_days": 6, "max_days": 8, "gap_from": 0, "gap_to": 0}
    assert bad_period(_utc(2025, 5, 5), _utc(2025, 5, 12), **no_gap) is False


def test_describe_pair_dollars_and_bottom_q():
    x = list(range(20))  # top decile = traders 18, 19; bottom decile = 0, 1
    y = [0.0] * 18 + [100.0, 0.0]
    d = describe_pair(make_pair("m", dict(enumerate(x)), dict(enumerate(y)), range(20)))
    assert d["n"] == 20
    assert d["top_mean_next"] == pytest.approx(50.0)
    assert d["top_median_next"] == pytest.approx(50.0)
    assert d["top_zero_share"] == pytest.approx(0.5)
    assert d["rest_mean_next"] == pytest.approx(0.0)
    assert d["n_active_next"] == 1 and math.isnan(d["q_active"])  # fewer than 10 active
    assert d["q_bottom"] == pytest.approx(-0.025)  # ties at 0: mid-rank 10 of 20 -> 0.475
    assert weighted_mean([1.0, 3.0], [1.0, 3.0]) == pytest.approx(2.5)


def test_period_pnl_and_no_lookahead():
    vals = [{"u": 0.0}, {"u": 5.0, "v": 1.0}, {"u": 4.0, "v": 3.0}, {"u": 10.0}]
    p = period_pnl(vals)
    assert p[0] == {"u": 5.0, "v": 1.0}
    assert p[1] == {"u": -1.0, "v": 2.0}
    changed = [*vals[:3], {"u": -100.0, "w": 9.0}]
    assert period_pnl(changed)[:2] == p[:2]  # data after the boundary t=2 does not move earlier periods


def test_ranks_and_spearman():
    assert ranks([3.0, 1.0, 1.0, 2.0]).tolist() == [4.0, 1.5, 1.5, 3.0]
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4], [1, 1, 2, 2]) == pytest.approx(1 / math.sqrt(1.25))
    assert spearman([1, 2, 3], [5, 5, 5]) is None


def test_q_and_stay_top_extremes():
    x = list(range(10))
    assert q_stat(x, x) == pytest.approx(0.45)  # top-1 lands at mid-rank 10 -> 0.95
    assert q_stat(x, x[::-1]) == pytest.approx(-0.45)
    assert stay_top(x, x) == 1.0
    assert stay_top(x, x[::-1]) == 0.0


def test_make_pair_and_drop_top():
    p = make_pair("m", {"b": 5.0, "a": -100.0, "c": 1.0}, {"a": 1.0}, ["c", "a", "b"])
    assert p.traders == ("a", "b", "c")
    assert p.y.tolist() == [1.0, 0.0, 0.0]  # absent in M+1 -> 0
    d = drop_top_abs(p, 0.01)
    assert d.traders == ("b", "c")


def test_mean_ci_and_mde():
    v = [0.1 * i for i in range(11)]
    r = mean_ci(v)
    assert r["mean"] == pytest.approx(0.5)
    sd = np.std(v, ddof=1)
    assert r["hi"] - r["lo"] == pytest.approx(2 * 2.228 * sd / math.sqrt(11), rel=2e-3)
    assert mde(0.1, 4) == pytest.approx(0.14)
    assert mde(None, 4) is None


def test_placebo_detects_persistence_and_centres_on_zero():
    rng = np.random.default_rng(1)
    pairs = []
    for k in range(4):
        x = rng.normal(size=300)
        pairs.append(make_pair(str(k), dict(enumerate(x)), dict(enumerate(x + 0.5 * rng.normal(size=300))), range(300)))
    r = rank_placebo(pairs, n_perm=200, seed=3)
    assert r["obs_ic"] > 0.8
    assert r["p_ic"] == pytest.approx(1 / 201)
    assert r["p_q"] == pytest.approx(1 / 201)
    assert abs(r["null_ic_mean"]) < 0.02
    assert abs(r["null_q_mean"]) < 0.03
    m = pair_metrics(pairs[0])
    assert m.n == 300 and m.ic == pytest.approx(spearman(pairs[0].x, pairs[0].y))


def test_power_gate():
    ok = [PairMetrics(str(i), 500, 0.05 + 0.01 * (i % 2), 0.02, 0.1) for i in range(10)]
    g = power_gate(ok, min_pairs=8, min_median_n=300, mde_ic=0.05, mde_q=0.03)
    assert g["passed"] is True
    noisy = [PairMetrics(str(i), 500, 0.3 * (-1) ** i, 0.02, 0.1) for i in range(10)]
    g = power_gate(noisy, min_pairs=8, min_median_n=300, mde_ic=0.05, mde_q=0.03)
    assert g["passed"] is False and g["checks"]["mde_ic"] is False
    few = power_gate(ok[:5], min_pairs=8, min_median_n=300, mde_ic=0.05, mde_q=0.03)
    assert few["checks"]["pairs"] is False
