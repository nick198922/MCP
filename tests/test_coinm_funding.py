from __future__ import annotations

import math

import numpy as np
import pytest

from tradedesk.analytics.coinm_funding import (
    ANCHOR,
    BUCKET_MS,
    DAY_MS,
    HOUR_MS,
    MONDAY0_MS,
    WEEK_MS,
    Costs,
    Day,
    aligned,
    anchor_decomposition,
    ann,
    block_sign_flip_p,
    bucket_8h,
    bucket_end,
    carry_coinm,
    carry_usdtm,
    dropped_buckets,
    moving_block_bootstrap,
    week_start,
    weekly,
)

MON = MONDAY0_MS + 2900 * WEEK_MS  # a Monday 00:00 UTC (2025-08)
ZERO = Costs(spot=0.0, perp=0.0, slip=0.0, liq_fee=0.0)


def test_bucket_end_and_jitter() -> None:
    assert bucket_end(MON + 8 * HOUR_MS + 5) == MON + 8 * HOUR_MS
    assert bucket_end(MON + 4 * HOUR_MS) == MON + 8 * HOUR_MS
    assert bucket_end(MON) == MON


def test_bucket_8h_drops_sub_8h_and_offgrid() -> None:
    rows = [(MON + 8 * HOUR_MS + 11, 0.0001), (MON + 12 * HOUR_MS, 0.0002), (MON + 16 * HOUR_MS, 0.0003), (MON + 28 * HOUR_MS, 0.0004)]
    b = bucket_8h(rows)
    # 08:00 alone ok; 12:00 + 16:00 share the 16:00 bucket -> dropped; 04:00 next day off-grid alone -> dropped
    assert b == {MON + 8 * HOUR_MS: 0.0001}
    assert dropped_buckets(rows) == 2


def test_aligned_and_weekly() -> None:
    c = {MON + k * BUCKET_MS: 0.0002 for k in range(1, 22)}
    u = {MON + k * BUCKET_MS: 0.0001 for k in range(1, 22)}
    rows = [(t, fc - fu) for t, fc, fu in aligned(c, u)]
    w = weekly(rows)
    assert len(w) == 1 and w[0].start == MON and w[0].n == 21
    assert w[0].mean == pytest.approx(0.0001)
    assert ann(w[0].mean) == pytest.approx(0.1095)
    # the bucket ending Monday 00:00 belongs to the previous week
    assert week_start(MON) == MON - WEEK_MS
    assert weekly(rows[:14]) == []


def test_weekly_causal() -> None:
    rows = [(MON + k * BUCKET_MS, 0.0001 * (k % 3)) for k in range(1, 22 * 4)]
    base = weekly(rows)
    later = [(t, v + (1.0 if t > MON + 2 * WEEK_MS else 0.0)) for t, v in rows]
    changed = weekly(later)
    assert [w for w in changed if w.start < MON + 2 * WEEK_MS - DAY_MS] == [w for w in base if w.start < MON + 2 * WEEK_MS - DAY_MS]


def test_bootstrap_iid_se_and_constant() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 1.0, 400)
    r = moving_block_bootstrap(x, block=1, n_boot=4000)
    assert r["se"] == pytest.approx(1.0 / math.sqrt(400), rel=0.1)
    assert r["mde"] == pytest.approx(2.8 * r["se"])
    c = moving_block_bootstrap([0.5] * 50)
    assert c["lo"] == pytest.approx(0.5) and c["hi"] == pytest.approx(0.5) and c["se"] == pytest.approx(0.0, abs=1e-12)


def test_sign_flip_placebo() -> None:
    assert block_sign_flip_p([1.0] * 40, block=4, n_perm=2000) < 0.01
    rng = np.random.default_rng(2)
    assert block_sign_flip_p(rng.normal(0, 1, 200), block=4, n_perm=2000) > 0.05


def test_anchor_decomposition_sums_to_mean() -> None:
    rows = [(0, ANCHOR, ANCHOR), (1, 0.0003, ANCHOR), (2, ANCHOR, -0.0001), (3, 0.0002, 0.0004)]
    dec = anchor_decomposition(rows)
    assert dec["both_anchor"] == {"share": 0.25, "contrib": 0.0}
    assert dec["coinm_off"]["contrib"] == pytest.approx(0.0002 / 4)
    assert dec["usdtm_off"]["contrib"] == pytest.approx(0.0002 / 4)
    total = sum(v["contrib"] for v in dec.values())
    assert total == pytest.approx(np.mean([fc - fu for _, fc, fu in rows]))


def _days(prices: list[float], fc: float, fu: float, start: int = MON) -> list[Day]:
    return [Day(start + i * DAY_MS, p, p, p, (fc, fc, fc), (fu, fu, fu)) for i, p in enumerate(prices)]


def test_coinm_funding_usd_equals_rate_times_notional() -> None:
    days = _days([100.0, 200.0, 50.0], 0.0001, 0.0)
    r = carry_coinm(days, ZERO)
    assert r["funding"] == pytest.approx(9 * 0.0001)
    # coins held to week end are revalued at the last close of the week (all three days in one week)
    h = carry_coinm(days, ZERO, hold_coins_to_week_end=True)
    assert h["funding"] == pytest.approx(3 * 0.0001 * 50 * (1 / 100 + 1 / 200 + 1 / 50))


def test_coinm_costs() -> None:
    c = Costs(spot=0.001, perp=0.0005, slip=0.0, liq_fee=0.0)
    r = carry_coinm(_days([100.0], 0.0, 0.0), c)
    assert r["costs"] == pytest.approx(2 * 0.0015)


def test_usdtm_flat_price_matches_rate() -> None:
    r = carry_usdtm(_days([100.0] * 10, 0.0, 0.0001), ZERO)
    assert r["funding"] == pytest.approx(30 * 0.0001)
    # separate 20 % margin: notional is 1/1.2 of capital
    r2 = carry_usdtm(_days([100.0] * 10, 0.0, 0.0001), ZERO, margin=0.2)
    assert r2["funding"] == pytest.approx(30 * 0.0001 / 1.2)
    assert r2["liquidations"] == 0


def test_usdtm_notional_drifts_then_rebalances() -> None:
    # price doubles on day 1 (Tuesday) and stays: funding on 2x notional until the Monday reset
    prices = [100.0] + [200.0] * 13
    r = carry_usdtm(_days(prices, 0.0, 0.0001), ZERO)
    # days 0: 1x; days 1..6: 2x; day 7 (Monday): funding at 2x then reset to equity (1 + funding so far); days 8..13
    before = 3 * 0.0001 * (1 + 6 * 2 + 2)
    expect = before + 3 * 0.0001 * 6 * (1 + before)
    assert r["funding"] == pytest.approx(expect, rel=1e-6)


def test_usdtm_liquidation_with_thin_margin() -> None:
    prices = [100.0, 130.0, 130.0]
    r = carry_usdtm(_days(prices, 0.0, 0.0), ZERO, margin=0.2)
    assert r["liquidations"] == 1
    r0 = carry_usdtm(_days(prices, 0.0, 0.0), ZERO, margin=0.0)
    assert r0["liquidations"] == 0


def _varied_days(prices: list[float], fc: list[float], fu: list[float], start: int = MON) -> list[Day]:
    return [Day(start + i * DAY_MS, p, p, p * 1.02, (a, a, a), (b, b, b)) for i, (p, a, b) in enumerate(zip(prices, fc, fu, strict=True))]


@pytest.mark.parametrize("k", [7, 8, 11, 15])
def test_carry_causal(k: int) -> None:
    # 21 days from a Monday: rebalancing Mondays at indices 7 and 14; the price moves enough to trigger them.
    # k = 8 and k = 15 change the day right after a Monday: day 8 is back at the entry price, so a Monday decision that
    # peeked at days[i + 1] would skip the rebalance on the base data but not on the changed data (x0.2) -> test fails.
    week1 = [100.0, 104.0, 111.0, 97.0, 92.0, 118.0, 121.0]
    week2 = [125.0, 100.0, 99.0, 103.0, 80.0, 84.0, 88.0]
    week3 = [130.0, 128.0, 95.0, 101.0, 70.0, 72.0, 110.0]
    prices = week1 + week2 + week3
    fc = [0.0001 * (1 + (i % 4)) for i in range(21)]
    fu = [0.00005 * (i % 5) - 0.00005 for i in range(21)]
    # everything from day k on differs: prices, highs and both funding streams
    prices2 = prices[:k] + [p * (3.0 if j % 2 else 0.2) for j, p in enumerate(prices[k:])]
    fc2 = fc[:k] + [-0.002] * (21 - k)
    fu2 = fu[:k] + [0.003] * (21 - k)
    base, changed = _varied_days(prices, fc, fu), _varied_days(prices2, fc2, fu2)
    assert base[:k] == changed[:k] and base[k:] != changed[k:]
    for margin in (0.0, 0.2):
        pa: list = []
        pb: list = []
        ra = carry_usdtm(base, Costs(), margin=margin, path=pa)
        rb = carry_usdtm(changed, Costs(), margin=margin, path=pb)
        assert len(pa) == len(pb) == 21
        # results up to day k-1 (funding, costs, coin quantity after the Monday decision, liquidations) are unchanged
        assert pa[:k] == pb[:k]
        # ... and match a run that never saw the later days at all
        pt: list = []
        carry_usdtm(base[:k], Costs(), margin=margin, path=pt)
        assert pt == pa[:k]
        # the later data really matters: the full-window results differ
        assert ra["net"] != pytest.approx(rb["net"])
        if margin == 0.0:
            assert any(pa[i][3] != pa[i - 1][3] for i in (7, 14)), "the scenario must trigger a Monday rebalance"
    for hold in (False, True):
        ca: list = []
        cb: list = []
        xa = carry_coinm(base, Costs(), hold_coins_to_week_end=hold, path=ca)
        xb = carry_coinm(changed, Costs(), hold_coins_to_week_end=hold, path=cb)
        assert ca[:k] == cb[:k]
        assert xa["net"] != pytest.approx(xb["net"])
