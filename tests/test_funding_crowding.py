"""Funding crowding long/short: pure analytics with analytic references, point-in-time checks and a real archive sample."""

from __future__ import annotations

import math
import random
import statistics
from pathlib import Path

import pytest

from tradedesk.analytics.funding_crowding import (
    Bars,
    Candidate,
    LegWeek,
    Pick,
    Week,
    beta_scales,
    block_bootstrap_ci,
    fm_slope,
    leg_week,
    liquidated,
    liquidation_capped,
    median_turnover,
    newey_west_se,
    pick_baskets,
    random_pick,
    rank_pick,
    ranks,
    rebalance_cost,
    run_start,
    run_start_map,
    side_width,
    simulate,
    sma_regime,
    spearman,
    trailing_beta,
    trailing_funding,
    unit_cost,
    week_candidate,
)
from tradedesk.analytics.listing_drift import DAY_MS, HOUR_MS
from tradedesk.providers.binance_vision import parse_funding_zip, parse_klines_zip

FIX = Path(__file__).parent / "fixtures"
D0 = 19_000  # an arbitrary UTC day index


def mk_bars(
    days: range | list[int], price: float = 1.0, qv: float = 1e7, ohlc: dict[int, tuple[float, float, float, float]] | None = None
) -> Bars:
    rows = []
    for d in days:
        o, h, lo, c = (ohlc or {}).get(d, (price, price, price, price))
        rows.append([d * DAY_MS, o, h, lo, c, 1.0, qv])
    return Bars.from_rows(rows)


def settlements(start_ms: int, end_ms: int, every_h: int, rate: float) -> list[list[float]]:
    return [[float(t), float(every_h), rate] for t in range(start_ms, end_ms, every_h * HOUR_MS)]


# ---------------------------------------------------------------- universe


def test_run_start_resets_after_long_gap_and_matches_map():
    days = list(range(D0, D0 + 10)) + list(range(D0 + 20, D0 + 40))  # 9 missing days > 7: new run at D0 + 20
    b = mk_bars(days)
    assert run_start(b, D0 + 5) == D0
    assert run_start(b, D0 + 30) == D0 + 20
    assert run_start(b, D0 + 15) is None  # no bar that day
    short_gap = mk_bars(list(range(D0, D0 + 10)) + list(range(D0 + 15, D0 + 20)))  # 5 missing days: same run
    assert run_start(short_gap, D0 + 17) == D0
    for bb in (b, short_gap):
        m = run_start_map(bb)
        assert all(m[d] == run_start(bb, d) for d in bb.close)


def test_median_turnover_window_and_min_days():
    b = Bars.from_rows([[d * DAY_MS, 1, 1, 1, 1, 1, float(d - D0)] for d in range(D0, D0 + 40)])
    assert median_turnover(b, D0 + 40) == pytest.approx(statistics.median(range(10, 40)))  # days D0+10..D0+39
    assert median_turnover(b, D0 + 15) is None  # only 15 bars before


def test_trailing_funding_annualises_by_actual_interval_and_excludes_holes():
    t = (D0 + 30) * DAY_MS
    r8 = settlements(t - 20 * DAY_MS, t + 5 * DAY_MS, 8, 0.0001)
    assert trailing_funding(r8, t, 7) == pytest.approx(21 * 0.0001 / 7 * 365)  # 0.1095 / yr
    r1 = settlements(t - 20 * DAY_MS, t + 5 * DAY_MS, 1, 0.0001 / 8)  # same economics on a 1h interval
    assert trailing_funding(r1, t, 7) == pytest.approx(trailing_funding(r8, t, 7))
    # interval switch 8h -> 4h inside the window: still no hole, sum follows real settlements
    mixed = settlements(t - 20 * DAY_MS, t - 3 * DAY_MS, 8, 0.0001) + settlements(t - 3 * DAY_MS, t + DAY_MS, 4, 0.0001)
    assert trailing_funding(mixed, t, 7) == pytest.approx((4 * 3 + 6 * 3) * 0.0001 / 7 * 365)
    holed = [r for r in r8 if not (t - 4 * DAY_MS <= r[0] < t - 3 * DAY_MS)]
    assert trailing_funding(holed, t, 7) is None
    assert trailing_funding([r for r in r8 if r[0] < t - 2 * DAY_MS], t, 7) is None  # stops before t: hole at the end
    # the settlement at t itself is not in the signal
    spike = r8 + [[float(t), 8.0, 0.5]]
    assert trailing_funding(spike, t, 7) == pytest.approx(trailing_funding(r8, t, 7))


def test_week_candidate_is_point_in_time():
    day = D0 + 60
    bars = mk_bars(range(D0, D0 + 80), qv=6e6)
    rec = settlements(D0 * DAY_MS, (D0 + 80) * DAY_MS, 8, 0.0002)
    c, why = week_candidate("XUSDT", bars, rec, day)
    assert why == "ok" and c is not None and c.age == 60 and c.median_qv == 6e6
    # change everything from t on (prices, volume, funding incl. the settlement at t): same candidate
    fut = Bars.from_rows(
        [[d * DAY_MS, 1, 1, 1, 1, 1, 6e6] for d in range(D0, day)] + [[d * DAY_MS, 9, 50, 0.1, 3, 1, 1.0] for d in range(day, D0 + 80)]
    )
    rec2 = [r if r[0] < day * DAY_MS - HOUR_MS else [r[0], r[1], -0.03] for r in rec]
    c2, _ = week_candidate("XUSDT", fut, rec2, day)
    assert c2 == c
    assert week_candidate("XUSDT", mk_bars(range(D0, D0 + 80), qv=4e6), rec, day)[1] == "illiquid"
    assert week_candidate("XUSDT", mk_bars(range(D0 + 40, D0 + 80)), rec, day)[1] == "young"
    holed = [r for r in rec if not (day * DAY_MS - 3 * DAY_MS <= r[0] < day * DAY_MS - 2 * DAY_MS)]
    assert week_candidate("XUSDT", bars, holed, day)[1] == "funding_gap"
    assert week_candidate("XUSDT", bars, rec, D0 + 90)[1] == "no_entry_bar"
    m = run_start_map(bars)
    assert week_candidate("XUSDT", bars, rec, day, starts=m)[0] == c


def test_side_width_and_pick():
    assert side_width(49) == 0 and side_width(50) == 5 and side_width(99) == 9 and side_width(150) == 10
    assert side_width(150, cap=20) == 15
    sig = {"A": 0.5, "B": -0.2, "C": 0.1, "D": -0.2, "E": 0.9}
    lo, hi = pick_baskets(sig, 2)
    assert lo == ["B", "D"] and hi == ["A", "E"]  # tie at -0.2 broken by name
    assert pick_baskets(sig, 3) == ([], [])  # sides would overlap


# ---------------------------------------------------------------- legs and basket


def test_leg_week_price_funding_and_settlement_windows():
    day = D0 + 10
    ohlc = {
        day + k: (100.0 * (1 + 0.01 * k), 100.0 * (1 + 0.01 * k) * 1.05, 100.0 * (1 + 0.01 * k) * 0.9, 100.0 * (1 + 0.01 * (k + 1)))
        for k in range(7)
    }
    b = mk_bars(range(D0, D0 + 30), price=100.0, ohlc=ohlc)
    t = day * DAY_MS
    rec = settlements(t - 3 * DAY_MS, t + 10 * DAY_MS, 8, 0.001)
    lg = leg_week("X", b, rec, day)
    assert lg is not None and lg.r == pytest.approx(0.07) and not lg.truncated and lg.hold_gaps == 0
    # 21 settlements (t+8h .. t+7d); the one at t excluded, the one at t+7d included; marked at that day's close
    exp = -sum(0.001 * b.close[day + h // 24] / 100.0 for h in range(8, 7 * 24 + 1, 8))
    assert lg.n_settle == 21
    assert lg.funding_long == pytest.approx(exp)
    assert lg.up == pytest.approx(1.06 * 1.05 - 1) and lg.down == pytest.approx(0.1)


def test_leg_week_truncated_by_delisting():
    day = D0 + 10
    b = mk_bars(list(range(D0, day + 3)), price=2.0, ohlc={day + 2: (2.0, 2.0, 1.0, 1.0)})
    rec = settlements((day - 3) * DAY_MS, (day + 3) * DAY_MS, 8, -0.01)
    lg = leg_week("X", b, rec, day)
    assert lg is not None and lg.truncated and lg.exit == day + 2 and lg.r == pytest.approx(-0.5)
    assert lg.n_settle == 8  # t+8h .. t+3d (end of the exit day + 30 min)
    assert liquidated(lg, +1, 2.0) and not liquidated(lg, -1, 2.0)


def test_rebalance_cost_and_unit_cost():
    assert unit_cost(1e6) == pytest.approx(0.0005 + 0.0015)
    assert unit_cost(1e7) == pytest.approx(0.0015) and unit_cost(1e8) == pytest.approx(0.0010)
    assert unit_cost(1e7, fee=0.0002, slip_mult=2) == pytest.approx(0.0022)
    turn, cost = rebalance_cost({"A": 0.5, "B": -0.5}, {"A": 0.25, "C": -0.5}, {"A": 0.001, "B": 0.002, "C": 0.003}, 0.01)
    assert turn == pytest.approx(0.25 + 0.5 + 0.5)
    assert cost == pytest.approx(0.25 * 0.001 + 0.5 * 0.002 + 0.5 * 0.003)


def leg(s: str, r: float, f: float = 0.0) -> LegWeek:
    return LegWeek(s, r, f, max(r, 0.0), max(-r, 0.0), 0, False, 21, 0)


def cand(s: str, sig: float, mq: float = 1e8) -> Candidate:
    return Candidate(s, sig, mq, 100)


def test_simulate_two_weeks_analytic():
    # week 1: long L (+10 %, receives 1 %), short S (+20 %, short receives 2 %); week 2: same names, flat prices
    w1 = Week(D0, {"L": cand("L", -0.5), "S": cand("S", 0.9)}, {"L": leg("L", 0.10, 0.01), "S": leg("S", 0.20, -0.02)})
    w2 = Week(D0 + 7, {"L": cand("L", -0.5), "S": cand("S", 0.9)}, {"L": leg("L", 0.0), "S": leg("S", 0.0)})
    res = simulate([w1, w2], lambda wk: Pick(["L"], ["S"]))
    uc = unit_cost(1e8)
    r1, r2 = res
    assert r1.price == pytest.approx(0.5 * 0.10 - 0.5 * 0.20)
    assert r1.funding == pytest.approx(0.5 * 0.01 + 0.5 * 0.02)
    assert r1.turnover == pytest.approx(1.0) and r1.cost == pytest.approx(uc)
    # drifted: L 0.55, S -0.60 -> back to 0.5 / -0.5: turnover 0.05 + 0.10; then the final exit 0.5 + 0.5
    assert r2.turnover == pytest.approx(0.15 + 1.0) and r2.cost == pytest.approx(1.15 * uc)
    assert r1.net == pytest.approx(r1.price + r1.funding - r1.cost)
    assert r1.long_price == pytest.approx(0.10) and r1.short_price == pytest.approx(-0.20)
    assert r1.long_funding == pytest.approx(0.01) and r1.short_funding == pytest.approx(0.02)


def test_liquidation_capped_legs():
    legs = {"L": leg("L", -0.60, 0.05), "S": leg("S", 0.32, -0.01), "Q": leg("Q", 0.10)}
    wk = Week(D0, {s: cand(s, 0.0) for s in legs}, legs)
    w2 = liquidation_capped(wk, Pick(["L"], ["S"]), 2.0)  # threshold 1/2 - 2.5 % = 47.5 %
    assert w2.legs["L"].r == pytest.approx(-0.5) and w2.legs["L"].funding_long == 0.0
    assert w2.legs["S"] == legs["S"] and w2.legs["Q"] == legs["Q"]  # 32 % < 47.5 %; Q not picked
    w3 = liquidation_capped(wk, Pick(["L"], ["S"]), 3.0)  # 30.8 %: the short is liquidated, loses 1/3
    assert w3.legs["S"].r == pytest.approx(1 / 3)
    assert wk.legs["S"].r == 0.32  # input untouched


def test_rank_and_random_pick_shapes():
    uni = {f"S{i:02d}": cand(f"S{i:02d}", i / 100) for i in range(60)}
    wk = Week(D0, uni, {s: leg(s, 0.0) for s in uni})
    pk = rank_pick(wk)
    assert len(pk.longs) == 6 and pk.longs[0] == "S00" and pk.shorts[-1] == "S59"
    rp = random_pick(wk, random.Random(1))
    assert len(rp.longs) == len(rp.shorts) == 6 and not set(rp.longs) & set(rp.shorts)


def test_beta_scales_keep_gross_and_equalise_beta():
    ls, ss = beta_scales(1.0, 2.0)
    assert ls + ss == pytest.approx(2.0) and ls * 1.0 == pytest.approx(ss * 2.0)
    assert beta_scales(None, 1.0) == (1.0, 1.0) and beta_scales(-0.1, 1.0) == (1.0, 1.0)


# ---------------------------------------------------------------- statistics


def test_ranks_spearman_and_fm():
    assert ranks([3.0, 1.0, 3.0, 2.0]) == [3.5, 1.0, 3.5, 2.0]
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    sig = [0.3, -0.1, 0.5, 0.0, 0.2]
    rk = ranks(sig)
    mu, sd = statistics.fmean(rk), statistics.pstdev(rk)
    ret = [0.01 * (r - mu) / sd + 0.002 for r in rk]
    assert fm_slope(sig, ret) == pytest.approx(0.01)


def test_newey_west_reduces_to_iid_and_grows_with_autocorrelation():
    xs = [1.0, -1.0, 2.0, 0.0, -2.0, 1.5, -0.5, 0.3]
    n = len(xs)
    assert newey_west_se(xs, 0) == pytest.approx(statistics.pstdev(xs) / math.sqrt(n))
    trend = [float(i % 2 == 0) + i * 0.0 for i in range(40)]  # alternating: negative autocorrelation shrinks SE
    assert newey_west_se(trend, 4) < newey_west_se(trend, 0)
    runs = [1.0] * 10 + [-1.0] * 10 + [1.0] * 10 + [-1.0] * 10  # positive autocorrelation inflates SE
    assert newey_west_se(runs, 4) > newey_west_se(runs, 0)


def test_block_bootstrap_ci():
    assert block_bootstrap_ci([0.01] * 30, n_boot=200) == (pytest.approx(0.01), pytest.approx(0.01))
    rng = random.Random(3)
    xs = [rng.gauss(0.0, 1.0) for _ in range(400)]
    lo, hi = block_bootstrap_ci(xs, block=1, n_boot=2000, seed=5)
    se = statistics.stdev(xs) / math.sqrt(len(xs))
    assert lo < statistics.fmean(xs) < hi
    assert (hi - lo) == pytest.approx(2 * 1.96 * se, rel=0.15)


def test_trailing_beta_and_regime_use_past_only():
    px, c = {}, 100.0
    for d in range(D0, D0 + 300):
        c *= 1.01 if d % 2 else 0.99
        px[d] = c
    btc = Bars.from_rows([[d * DAY_MS, 1, 1, 1, px[d], 1, 1] for d in px])
    # alt = btc^2 / 100: daily return (1 + x)^2 - 1 = 2x + x^2 with x = +-1 % (x^2 constant): slope exactly 2
    alt = Bars.from_rows([[d * DAY_MS, 1, 1, 1, px[d] ** 2 / 100, 1, 1] for d in px])
    b = trailing_beta(alt, btc, D0 + 100)
    assert b == pytest.approx(2.0)
    later = Bars.from_rows([[d * DAY_MS, 1, 1, 1, alt.close[d] if d < D0 + 100 else 1.0, 1, 1] for d in range(D0, D0 + 300)])
    assert trailing_beta(later, btc, D0 + 100) == b
    up = Bars.from_rows([[d * DAY_MS, 1, 1, 1, float(d - D0 + 1), 1, 1] for d in range(D0, D0 + 300)])
    assert sma_regime(up, D0 + 250) == 1
    assert sma_regime(up, D0 + 100) is None  # fewer than 90 % of 200 closes


# ---------------------------------------------------------------- real archive sample (removed symbol)


def test_removed_symbol_archive_sample():
    # data.binance.vision, fetched 2026-10-15: RNDRUSDT (renamed to RENDER, absent from today's exchangeInfo), June 2024
    k = parse_klines_zip((FIX / "binance_vision_RNDRUSDT-1d-2024-06_2026-10-15.zip").read_bytes())
    f = parse_funding_zip((FIX / "binance_vision_RNDRUSDT-fundingRate-2024-06_2026-10-15.zip").read_bytes())
    assert len(k) == 30 and k[0][0] == 1_717_200_000_000 and k[0][1] == pytest.approx(10.1307)
    assert len(f) == 90 and {r[1] for r in f} == {8.0} and f[0][2] == pytest.approx(0.00019638)
    bars = Bars.from_rows(k)
    t = 1_717_200_000_000 + 9 * DAY_MS  # 2024-06-10 00:00 UTC (a Monday is fine for the arithmetic)
    sig = trailing_funding(f, t, 7)
    assert sig is not None
    inside = [r[2] for r in f if t - 7 * DAY_MS - HOUR_MS // 2 <= r[0] < t - HOUR_MS // 2]
    assert len(inside) == 21 and sig == pytest.approx(sum(inside) / 7 * 365)
    lg = leg_week("RNDRUSDT", bars, f, t // DAY_MS)
    assert lg is not None and lg.n_settle == 21 and lg.hold_gaps == 0
    assert lg.r == pytest.approx(bars.close[t // DAY_MS + 6] / bars.open[t // DAY_MS] - 1)
