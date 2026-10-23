"""Analytic checks of the leveraged alt-basket benchmark (IDEA-20261023-alt-basket-leverage-benchmark)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.leverage_basket import (
    Panel,
    block_bootstrap_by_month,
    clopper_pearson,
    median,
    percentile_of,
    select_basket,
    share,
    simulate_window,
)


def flat_panel(n_days: int, n_syms: int, price: float = 100.0, fund: float = 0.0) -> Panel:
    c = np.full((n_days, n_syms), price)
    return Panel(c.copy(), c.copy(), c.copy(), c.copy(), np.full((n_days, n_syms), 1e6), np.full((n_days, n_syms), fund))


def run(p: Panel, basket: list[int], length: int, **kw: object) -> object:
    args: dict[str, object] = {"leverage": 1.0, "rebalance_every": length, "fee": 0.0, "slip": 0.0, "mm": 0.025, "mode": "close"}
    args.update(kw)
    return simulate_window(p, 0, length, lambda d: basket, **args)  # type: ignore[arg-type]


def test_flat_prices_cost_only() -> None:
    p = flat_panel(20, 2)
    r = run(p, [0, 1], 20, leverage=2.0, fee=0.0005, slip=0.0005)
    assert r.ret == pytest.approx(-4 * 0.001, abs=1e-12)  # 2 notional in, 2 out
    assert not r.liquidated and r.n_rebalance == 1


def test_doubling_unlevered() -> None:
    p = flat_panel(10, 1)
    for a in (p.open, p.high, p.low, p.close):
        a[5:, 0] = 200.0
    c = 0.001
    r = run(p, [0], 10, fee=c)
    assert r.ret == pytest.approx(1.0 - 3 * c, abs=1e-12)
    assert r.contrib[0] == pytest.approx(r.ret, abs=1e-12)


def test_funding_paid_by_long() -> None:
    p = flat_panel(30, 1, fund=0.0003)
    r = run(p, [0], 30)
    assert r.ret == pytest.approx(-30 * 0.0003, rel=1e-9, abs=1e-12)
    assert r.funding == pytest.approx(30 * 0.0003, rel=1e-9)
    p.fund[:] = np.nan
    r2 = run(p, [0], 30)
    assert r2.ret == pytest.approx(0.0, abs=1e-12) and r2.funding_holes == 30


def test_liquidation_close_vs_intraday() -> None:
    # L=3 cross: liquidation when the basket falls by x >= (1 - 3 mm) / (3 (1 - mm)) ~ 31.6 %
    p = flat_panel(20, 1)
    p.low[5, 0] = 65.0  # -35 % wick, closes flat
    ri = run(p, [0], 20, leverage=3.0, mode="intraday")
    rc = run(p, [0], 20, leverage=3.0, mode="close")
    assert ri.liquidated and ri.liq_day == 5 and ri.ret == -1.0 and ri.max_dd == 1.0
    assert not rc.liquidated and rc.ret == pytest.approx(0.0, abs=1e-12)
    p.low[5, 0] = 70.0  # -30 %: survives in both
    r = run(p, [0], 20, leverage=3.0, mode="intraday")
    assert not r.liquidated and r.max_dd == pytest.approx(0.9, abs=1e-12)  # equity 1 - 3 * 0.3
    p.close[8:, 0] = p.low[8:, 0] = p.open[8:, 0] = p.high[8:, 0] = 66.0
    r = run(p, [0], 20, leverage=3.0, mode="close")
    assert r.liquidated and r.liq_day == 8


def test_threshold_matches_formula() -> None:
    mm, lev = 0.025, 3.0
    x = (1 - lev * mm) / (lev * (1 - mm))  # 1 - L x = mm L (1 - x)
    for drop, liq in ((x - 1e-6, False), (x + 1e-6, True)):
        p = flat_panel(5, 1)
        p.low[2, 0] = 100.0 * (1 - drop)
        assert run(p, [0], 5, leverage=lev, mode="intraday").liquidated is liq


def test_rebalance_resets_weights() -> None:
    p = flat_panel(60, 2, price=1.0)
    for a in (p.open, p.high, p.low, p.close):
        a[1:, 0] = 2.0
        a[31:, 0] = 4.0
    rb = run(p, [0, 1], 60, rebalance_every=30)
    hold = run(p, [0, 1], 60, rebalance_every=60)
    assert rb.n_rebalance == 2 and hold.n_rebalance == 1
    assert rb.ret == pytest.approx(1.25, abs=1e-12)  # 1.5 at day 30, then half doubles: 2.25
    assert hold.ret == pytest.approx(1.5, abs=1e-12)  # 0.5 * 4 + 0.5


def test_delisting_closes_at_last_close() -> None:
    p = flat_panel(20, 2)
    for a in (p.open, p.high, p.low, p.close):
        a[3:, 0] = 150.0
        a[6:, 0] = np.nan  # leg 0 stops trading after day 5
    r = run(p, [0, 1], 20, fee=0.001)
    assert r.n_delist == 1
    # 0.5 at 100 -> 150 (+0.25), costs: entry 1.0, delist 0.75, exit 0.5
    assert r.ret == pytest.approx(0.25 - 0.001 * (1.0 + 0.75 + 0.5), abs=1e-12)


def test_no_lookahead() -> None:
    rng = np.random.default_rng(1)
    n, m = 120, 6
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, (n, m)), axis=0))
    p = Panel(c * 1.001, c * 1.03, c * 0.97, c, rng.uniform(1e6, 1e7, (n, m)), np.full((n, m), 1e-4))
    cand, grp = np.ones(m, bool), np.arange(m)

    def bas(pp: Panel):  # noqa: ANN202
        return lambda d: select_basket(pp.close, pp.qv, d, k=3, lookback=30, candidates=cand, group=grp)

    kw = {"leverage": 2.0, "rebalance_every": 30, "fee": 5e-4, "slip": 5e-4, "mm": 0.025, "mode": "intraday"}
    a = simulate_window(p, 30, 90, bas(p), **kw)  # type: ignore[arg-type]
    t = 70
    q = Panel(*(x.copy() for x in (p.open, p.high, p.low, p.close, p.qv, p.fund)))
    for x in (q.open, q.high, q.low, q.close):
        x[t:] *= 3.0
    q.qv[t:] = rng.uniform(0, 1e9, (n - t, m))
    b = simulate_window(q, 30, 90, bas(q), **kw)  # type: ignore[arg-type]
    k = t - 30  # equity offsets before day t
    assert np.allclose(a.equity[:k], b.equity[:k], rtol=0, atol=1e-12)
    assert a.equity[k + 1] != b.equity[k + 1]
    assert select_basket(p.close, p.qv, t, k=3, lookback=30, candidates=cand, group=grp) == select_basket(
        q.close, q.qv, t, k=3, lookback=30, candidates=cand, group=grp
    )


def test_select_basket_rules() -> None:
    n, m = 40, 5
    c = np.full((n, m), 1.0)
    qv = np.tile(np.array([5.0, 4.0, 3.0, 2.0, 1.0]), (n, 1))
    c[25, 0] = np.nan  # symbol 0 has a hole inside the lookback -> not continuous
    group = np.array([0, 1, 1, 2, 3])  # symbols 1 and 2 are the same asset
    cand = np.array([True, True, True, True, False])
    got = select_basket(c, qv, 35, k=3, lookback=30, candidates=cand, group=group)
    assert got == [1, 3]  # 0 broken run, 2 duplicate of 1, 4 not a candidate
    qv2 = qv.copy()
    qv2[35:, 3] = 1e9  # turnover on/after the decision day is ignored
    assert select_basket(c, qv2, 35, k=3, lookback=30, candidates=cand, group=group) == got
    assert select_basket(c, qv, 10, k=3, lookback=30, candidates=cand, group=group) == []


def test_bootstrap_and_stats() -> None:
    months = np.repeat(np.arange(24), 4)
    lo, hi = block_bootstrap_by_month(np.full(96, 0.3), months, median, block=8, n_boot=200)
    assert lo == pytest.approx(0.3) and hi == pytest.approx(0.3)
    x = (np.arange(96) < 48).astype(float)  # first half all ones: blocks make the CI wide
    lo, hi = block_bootstrap_by_month(x, months, share, block=8, n_boot=2000)
    assert lo < 0.5 < hi and hi - lo > 0.3
    assert percentile_of(2.0, [1.0, 2.0, 3.0, 4.0]) == pytest.approx(0.375)
    assert math.isnan(share(np.array([])))


def test_clopper_pearson() -> None:
    lo, hi = clopper_pearson(7, 7)
    assert lo == pytest.approx(0.025 ** (1 / 7), abs=1e-9) and hi == 1.0
    lo, hi = clopper_pearson(0, 7)
    assert lo == 0.0 and hi == pytest.approx(1 - 0.025 ** (1 / 7), abs=1e-9)
    lo, hi = clopper_pearson(5, 10)
    assert lo == pytest.approx(0.1871, abs=1e-4) and hi == pytest.approx(0.8129, abs=1e-4)


def test_bad_mode() -> None:
    with pytest.raises(ValueError):
        run(flat_panel(3, 1), [0], 3, mode="wick")
