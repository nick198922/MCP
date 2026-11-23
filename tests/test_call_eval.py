"""Pure helpers of the btc_charlie full run (card IDEA-20261113-x-traders, 3.3в): analytic references, synthetic bars."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics import call_eval as ce
from tradedesk.analytics.trade_replay import Bar
from tradedesk.analytics.trader_eval import cluster_bootstrap_ci, mean

H, D = ce.H_MS, ce.D_MS
T0 = 1_600_000_000_000 // H * H  # an hour boundary


def geometric(a: float, lo: int, hi: int, p0: float = 100.0) -> dict[int, Bar]:
    """Bars with open = p0 · e^(a·k), close = open · e^a (close of bar k = open of bar k + 1)."""
    out = {}
    for k, t in enumerate(range(lo, hi + 1, H)):
        o = p0 * math.exp(a * k)
        c = o * math.exp(a)
        out[t] = Bar(t, o, max(o, c), min(o, c), c)
    return out


def test_costs_are_the_registered_ones():
    assert ce.cost_of("binanceusdm:BTC/USDT:USDT") == pytest.approx(0.0015)
    assert ce.cost_of("binance:TOMO/USDT") == pytest.approx(0.0030)
    assert ce.cost_of("binance:REN/BTC") == pytest.approx(0.0030)
    assert ce.is_perp("mexc:ANSEM/USDT:USDT") and not ce.is_perp("binance:ADA/USDT")


def test_first_hour_after_skips_the_bar_containing_t0():
    assert ce.first_hour_after(T0) == T0 + H
    assert ce.first_hour_after(T0 + 59 * 60_000) == T0 + H


def test_hold_return_long_and_short_analytic():
    bars = geometric(0.001, T0, T0 + 400 * H)
    g = math.exp(0.001 * 168) - 1.0  # open of bar h to close of bar h + 167 h = 168 steps
    assert ce.hold_return(bars, "long", T0, hold_h=168, cost=0.0015) == pytest.approx(g - 0.0015)
    assert ce.hold_return(bars, "short", T0, hold_h=168, cost=0.0015) == pytest.approx(-g - 0.0015)
    assert ce.hold_return(bars, "long", T0 + 300 * H, hold_h=168, cost=0.0) is None  # out of data


def test_local_drift_of_a_constant_trend_equals_one_hold():
    bars = geometric(0.0005, T0 - 31 * D, T0 + 38 * D)
    g = math.exp(0.0005 * 168) - 1.0
    d, cov = ce.local_drift(bars, "long", T0, cost=0.003)
    assert d == pytest.approx(g - 0.003) and cov == pytest.approx(1.0)
    d_s, _ = ce.local_drift(bars, "short", T0, cost=0.003)
    assert d_s == pytest.approx(-g - 0.003)


def test_local_drift_coverage_and_skipped_hours():
    bars = geometric(0.0, T0 - 31 * D, T0 + 10 * D)  # no hold ends after t0 + 10 d
    d, cov = ce.local_drift(bars, "long", T0, cost=0.0)
    hours = 60 * 24 + 1
    usable = sum(1 for h in range(T0 - 30 * D, T0 + 30 * D + 1, H) if h + 167 * H <= T0 + 10 * D)
    assert cov == pytest.approx(usable / hours) and d == pytest.approx(0.0)


def test_pool_starts_respect_blocks_margin_and_market_bars():
    times = list(range(0, 100 * D, H))
    blocks = [(10 * D, 30 * D), (60 * D, 200 * D)]  # the second block is clipped by the market's last bar (100 d − 1 h)
    got = ce.pool_starts(times, blocks, 14 * D)
    assert got[0] == 10 * D and max(t for t in got if t < 50 * D) == 16 * D
    assert got[-1] == 100 * D - H - 14 * D
    assert not [t for t in got if 30 * D < t < 60 * D]
    a, b = ce.window_starts(got, 70 * D, 2 * D)
    assert got[a] == 68 * D and got[b - 1] == 72 * D


def test_short_row_rule_does_not_depend_on_the_outcome():
    last = T0 + 200 * H
    assert ce.short_row_evaluable(None, False, "no_fill", last, 168 * H)
    assert not ce.short_row_evaluable(None, False, "pending", last, 168 * H)
    assert ce.short_row_evaluable(T0 + 30 * H, True, "stop", last, 168 * H)
    assert not ce.short_row_evaluable(T0 + 40 * H, True, "stop", last, 168 * H)  # early stop, but window not in data
    assert not ce.short_row_evaluable(T0 + 40 * H, True, "target", last, 168 * H)


def test_breakeven_mean_is_analytic():
    x = ce.breakeven_mean(58, 0.02, 5)
    assert x == pytest.approx(58 * 0.02 / 5)
    assert mean([0.02] * 58 + [-x] * 5) == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ValueError):
        ce.breakeven_mean(10, 0.01, 0)


def test_breakeven_ci_low_equals_the_linear_search():
    vals = [0.05, 0.03, 0.04, 0.06, 0.02, 0.05, 0.01, 0.07]
    cl = [1, 1, 2, 3, 4, 5, 6, 7]
    extra = [2, 8]

    def ci(v, c):
        return cluster_bootstrap_ci(v, c, n_boot=400, seed=3)

    got = ce.breakeven_ci_low(vals, cl, extra, step=0.01, max_x=1.0, ci=ci)
    linear = next(i * 0.01 for i in range(101) if ci(vals + [-i * 0.01] * 2, cl + extra)[0] <= 0)
    assert got == pytest.approx(linear)
    assert ce.breakeven_ci_low([-0.01, 0.0, 0.01], [1, 2, 3], [4], ci=ci) == 0.0


def test_within_block_permutation_detects_label_effect_and_not_noise():
    blocks = [i // 4 for i in range(80)]
    labels = ["a", "b"] * 40
    strong = [1.0 if lab == "a" else 0.0 for lab in labels]
    assert ce.label_permutation_p_within(strong, labels, blocks, n=200, seed=1) < 0.01
    flat = [float(b) for b in blocks]  # value depends on the block only: within-block shuffles change nothing
    assert ce.label_permutation_p_within(flat, labels, blocks, n=200, seed=1) == pytest.approx(1.0)


def test_quantile_lower():
    assert ce.quantile([5, 1, 3, 2, 4, float("nan")], 0.10) == 1
    assert ce.quantile([float(i) for i in range(11)], 0.10) == 1.0
    assert math.isnan(ce.quantile([], 0.1))


def _unit(side: str, lo: float, hi: float, stop: float | None = None, target: float | None = None,
          *, cid: str = "BC-TEST", market: str = "binance:X/USDT"):  # fmt: skip
    import sys
    from dataclasses import replace
    from datetime import date
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import verify_btc_charlie_eval as bce

    from tradedesk.analytics.calls import Call

    rows, _ = bce.load_rows()
    lab = replace(rows[0].lab, t0_ms=T0 + 5 * 60_000)
    call = Call(cid, date(2020, 9, 13), "x", "X", side, lo, hi, target, stop)
    return bce, bce.Unit(bce.Row(call, "main", lab), lab.t0_ms, market, "ok")


def _series(bars: dict[int, Bar], market: str = "binance:X/USDT"):
    b = [bars[t] for t in sorted(bars)]
    return {market: (b, [x.t for x in b])}


def test_metric_of_a_market_entry_is_one_hold_net_of_spot_cost():
    a = 0.0001
    bars = geometric(a, T0 - 2 * D, T0 + 20 * D)
    bce, u = _unit("long", 100.0, 100.0)  # ref = open of the first hour after t0 = 100·e^(a·49), within 0.5 % → market
    tr = bce.run_unit(u, _series(bars))
    assert tr.status == "time" and tr.fill_t == T0 + H
    assert bce.value(tr) == pytest.approx(math.exp(168 * a) - 1.0 - 0.003)
    bce, us = _unit("short", 100.0, 100.0)
    assert bce.value(bce.run_unit(us, _series(bars))) == pytest.approx(-(math.exp(168 * a) - 1.0) - 0.003)


def test_unfilled_limit_is_zero_and_the_result_ignores_later_bars():
    bars = geometric(0.0001, T0 - 2 * D, T0 + 20 * D)
    bce, u = _unit("long", 90.0, 95.0)  # price above the zone and rising: the limit at 95 never fills
    tr = bce.run_unit(u, _series(bars))
    assert tr.status == "no_fill" and bce.value(tr) == 0.0
    bce, m = _unit("long", 100.0, 100.0)
    v1 = bce.value(bce.run_unit(m, _series(bars)))
    later = dict(bars)
    for t in range(T0 + 16 * D, T0 + 20 * D + 1, H):  # after fill (t0 + 1 h) + 168 h
        later[t] = Bar(t, 1.0, 500.0, 0.5, 1.5)
    assert bce.value(bce.run_unit(m, _series(later))) == v1


def test_local_drift_before_t_unchanged_by_later_data_outside_its_window():
    """A row's drift reads only [t0 − 30 d, t0 + 37 d]; changing bars after that leaves it unchanged."""
    bars = geometric(0.0003, T0 - 31 * D, T0 + 90 * D)
    d1 = ce.local_drift(bars, "long", T0, cost=0.0015)
    later = dict(bars)
    for t in range(T0 + 38 * D, T0 + 90 * D + 1, H):
        later[t] = Bar(t, 1.0, 2.0, 0.5, 1.5)
    assert ce.local_drift(later, "long", T0, cost=0.0015) == d1


def walk(lo: int, hi: int, seed: int = 3, p0: float = 100.0, vol: float = 0.01) -> dict[int, Bar]:
    """A deterministic random walk of 1h bars (open = previous close), so limits, stops and targets get hit."""
    import random

    rnd, out, o = random.Random(seed), {}, p0
    for t in range(lo, hi + 1, H):
        c = o * math.exp(rnd.gauss(0.0, vol))
        out[t] = Bar(t, o, max(o, c) * (1 + vol / 4), min(o, c) * (1 - vol / 4), c)
        o = c
    return out


def _scramble(bars: dict[int, Bar], ts) -> dict[int, Bar]:
    out = dict(bars)
    for t in ts:
        out[t] = Bar(t, 1.0, 500.0, 0.5, 2.0)
    return out


def _entry_units(ref: float):
    """A market, a limit and a stop entry (plan_entry 'nearest' at ref), each with a stop and a target."""
    bce, m = _unit("long", ref, ref, stop=ref * 0.95, target=ref * 1.08, cid="BC-M")
    _, lim = _unit("long", ref * 0.96, ref * 0.98, stop=ref * 0.93, target=ref * 1.05, cid="BC-L")
    _, stp = _unit("long", ref * 1.02, ref * 1.03, stop=ref * 0.97, target=ref * 1.10, cid="BC-S")
    return bce, (m, lim, stp)


def test_run_unit_and_p3_ignore_bars_at_or_before_t0():
    """Review B 33в, finding 1(a): the replay starts at the first hour after t0, so the bar containing t0 (T0) and all
    earlier bars do not change run_unit (market, limit, stop entries) or the P3 row (same time, random side)."""
    bars = walk(T0 - 10 * D, T0 + 40 * D)
    bce, units = _entry_units(bars[T0 + H].open)
    S = _series(bars)
    kinds = [bce.unit_plan(u, bars[T0 + H].open).kind for u in units]
    assert kinds == ["market", "limit", "stop"]
    base = [bce.run_unit(u, S) for u in units]
    assert all(tr.filled for tr in base)  # the test is not vacuous: every order fills
    S2 = _series(_scramble(bars, range(T0 - 10 * D, T0 + 1, H)))  # the t0 bar (t0 = T0 + 5 min) and earlier
    assert [bce.run_unit(u, S2) for u in units] == base
    pools = {"binance:X/USDT": ce.pool_starts(S["binance:X/USDT"][1], [(T0 - 5 * D, T0 + 25 * D)], 14 * D)}
    p3, _ = bce.placebo(list(units), S, pools, "side", 2, 50)
    assert bce.placebo(list(units), S2, pools, "side", 2, 50)[0] == p3


def test_p1_p2_matrices_deterministic_and_blind_to_bars_after_the_pool():
    """Finding 1(b): P1/P2 read bars only up to max(pool) + 14 d; later bars do not change the matrix, while bars
    inside the pool do (the test can fail). A unit's row does not depend on the other units passed."""
    bars = walk(T0 - 10 * D, T0 + 60 * D)
    bce, units = _entry_units(bars[T0 + H].open)
    S = _series(bars)
    times = S["binance:X/USDT"][1]
    pools = {"binance:X/USDT": ce.pool_starts(times, [(T0 - 5 * D, T0 + 25 * D)], 14 * D)}
    edge = max(pools["binance:X/USDT"]) + 14 * D
    assert edge == T0 + 25 * D
    S_late = _series(_scramble(bars, range(edge + H, T0 + 60 * D + 1, H)))
    S_in = _series(_scramble(bars, range(T0 + 2 * D, T0 + 4 * D, H)))
    for mode, off in (("time_side", 0), ("time", 1)):
        m1, d1 = bce.placebo(list(units), S, pools, mode, off, 40)
        assert bce.placebo(list(units), S, pools, mode, off, 40) == (m1, d1)  # deterministic
        assert bce.placebo(list(units), S_late, pools, mode, off, 40)[0] == m1
        assert bce.placebo(list(units), S_in, pools, mode, off, 40)[0] != m1
        alone, _ = bce.placebo([units[1]], S, pools, mode, off, 40)
        assert alone[units[1].uid] == m1[units[1].uid]  # own generator per unit


def test_main_and_combo_pools_do_not_overlap():
    """Finding 1(c): main and combo pools come from their own blocks; BTC's combo pool has no 2020–2022 time."""
    from datetime import date

    bce, _ = _unit("long", 1.0, 1.0)
    now = bce._day_ms(date(2026, 11, 23))
    main, combo = bce.pool_blocks("main", now), bce.pool_blocks("combo", now)
    assert all(hi_m < lo_c for _, hi_m in main for lo_c, _ in combo)
    lo_t, hi_t = bce._day_ms(date(2019, 11, 1)), bce._day_ms(date(2026, 10, 31))
    b = [Bar(t, 1.0, 1.0, 1.0, 1.0) for t in range(lo_t, hi_t + 1, H)]
    mk = "binanceusdm:BTC/USDT:USDT"
    S = {mk: (b, [x.t for x in b])}
    pm = bce.build_pools("main", [mk], S, now)[mk]
    pc = bce.build_pools("combo", [mk], S, now)[mk]
    assert pm and pc and not set(pm) & set(pc)
    assert min(pc) >= bce._day_ms(date(2025, 9, 1))
    assert max(pm) <= bce._day_ms(date(2022, 5, 31)) and min(pm) >= bce._day_ms(date(2019, 12, 2))


def test_strict_fill_plan_does_not_fill_on_a_touch():
    """Exploratory rule (review B 33в, finding 2): a long limit whose price the lows only touch fills under the
    registered rule (low <= limit) but not when the price must pass through it."""
    bars = {t: Bar(t, 20.0, 21.0, 19.0, 20.0) for t in range(T0 - D, T0 + 20 * D + 1, H)}
    for t in range(T0 + 3 * D, T0 + 3 * D + 4 * H, H):
        bars[t] = Bar(t, 17.0, 17.5, 16.0, 17.0)  # low exactly at the limit, four bars (as STORM 2020-05-28)
    bce, u = _unit("long", 15.0, 16.0)
    S = _series(bars)
    tr = bce.run_unit(u, S)
    assert tr.filled and tr.fill_t == T0 + 3 * D and tr.fill == pytest.approx(16.0)
    strict = bce.strict_fill_plan(u, S)
    assert strict.kind == "limit" and strict.entry < 16.0
    ts = bce.run_unit(u, S, plan=strict)
    assert ts.status == "no_fill" and bce.value(ts) == 0.0


def test_funding_only_on_filled_perps():
    """Finding 1(d): spot and */BTC rows are not charged funding even when rates are passed for their key; a Binance
    USD-M perp is charged the settlements in (fill, exit + 1 h]; a perp without a funding series is flagged."""
    bars = geometric(0.0001, T0 - 2 * D, T0 + 20 * D)
    for mk, charged in (("binance:X/USDT", False), ("binance:X/BTC", False), ("binanceusdm:X/USDT:USDT", True)):
        bce, u = _unit("long", 100.0, 100.0, market=mk)
        tr = bce.run_unit(u, _series(bars, mk))
        x = bce.value(tr)
        rates = {mk: [(t, 0.001) for t in range(T0, T0 + 20 * D, 8 * H)]}
        got, lacking = bce.with_funding(u, tr, x, rates)
        assert not lacking
        if charged:
            n = sum(tr.fill_t < t <= tr.exit_t + H for t, _ in rates[mk])
            assert n == 21 and got == pytest.approx(x - 0.001 * n)
        else:
            assert got == x
    bce, u = _unit("long", 100.0, 100.0, market="mexc:X/USDT:USDT")
    tr = bce.run_unit(u, _series(bars, "mexc:X/USDT:USDT"))
    assert bce.with_funding(u, tr, bce.value(tr), {}) == (bce.value(tr), True)
    bce, nf = _unit("long", 90.0, 95.0, market="binanceusdm:X/USDT:USDT")  # an unfilled limit: no funding, no flag
    tr = bce.run_unit(nf, _series(bars, "binanceusdm:X/USDT:USDT"))
    assert bce.with_funding(nf, tr, bce.value(tr), {}) == (0.0, False)
