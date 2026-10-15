"""Alt downside beta regime basket: analytic references, point-in-time checks, inference helpers."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from tradedesk.analytics.alt_downside_beta import (
    BTC,
    active_net,
    below_sma,
    block_bootstrap_diff_ci,
    block_bootstrap_mean_ci,
    circular_shift_test,
    day_return,
    decompose,
    diff_means,
    episodes,
    hedge_pick,
    index_return,
    mean_beta,
    negative_week,
    ols_hac,
    path_mae,
    trailing_index_downside_beta,
    two_slope,
    universe_at,
)
from tradedesk.analytics.funding_crowding import Bars, Candidate, LegWeek, Week, WeekResult, run_start_map, simulate
from tradedesk.analytics.listing_drift import DAY_MS

D0 = 19_000


def bars_from_closes(closes: dict[int, float], qv: float = 1e7) -> Bars:
    return Bars.from_rows([[d * DAY_MS, c, c, c, c, 1.0, qv] for d, c in sorted(closes.items())])


def flat(days: range, price: float = 1.0, qv: float = 1e7) -> Bars:
    return bars_from_closes(dict.fromkeys(days, price), qv)


# ---------------------------------------------------------------- universe


def test_universe_rank_age_liquidity_and_dedupe() -> None:
    days = range(D0 - 200, D0 + 10)
    bars = {
        "AUSDT": flat(days, qv=9e7),
        "BUSDT": flat(days, qv=3e7),
        "CUSDT": flat(range(D0 - 40, D0 + 10), qv=5e8),  # too young (40 < 60)
        "DUSDT": flat(days, qv=1e6),  # illiquid
        "1000BUSDT": flat(days, qv=6e7),  # same asset as B, larger turnover: replaces it
        BTC: flat(days, qv=1e10),  # excluded
        "EUSDT": flat(range(D0 - 200, D0 - 5), qv=9e9),  # no bar on day - 1
    }
    asset = {s: s.removeprefix("1000")[:-4] for s in bars}
    starts = {s: run_start_map(b) for s, b in bars.items()}
    u = universe_at(bars, D0, n=30, asset_of=asset, starts=starts)
    assert [c.symbol for c in u] == ["AUSDT", "1000BUSDT"]
    assert u[0].median_qv == 9e7 and u[0].age == 200
    assert [c.symbol for c in universe_at(bars, D0, n=1, asset_of=asset, starts=starts)] == ["AUSDT"]


def test_universe_is_point_in_time() -> None:
    days = range(D0 - 200, D0 + 30)
    rng = random.Random(1)
    base = {f"S{i}USDT": {d: 1.0 + rng.random() for d in days} for i in range(8)}
    qv = {f"S{i}USDT": 1e7 * (i + 1) for i in range(8)}

    def build(perturb: bool) -> dict[str, Bars]:
        out = {}
        for s, cl in base.items():
            rows = []
            for d, c in cl.items():
                q = qv[s] * (100.0 if perturb and d >= D0 else 1.0)  # turnover from t on changes the ranking
                rows.append([d * DAY_MS, c, c, c, c * (3.0 if perturb and d >= D0 else 1.0), 1.0, q])
            out[s] = Bars.from_rows(rows)
        if perturb:
            out["NEWUSDT"] = flat(range(D0, D0 + 30), qv=1e12)  # listed at t
        return out

    a, b = build(False), build(True)
    sa = {s: run_start_map(x) for s, x in a.items()}
    sb = {s: run_start_map(x) for s, x in b.items()}
    ua = universe_at(a, D0, n=5, asset_of={}, starts=sa)
    ub = universe_at(b, D0, n=5, asset_of={}, starts=sb)
    assert [c.symbol for c in ua] == [c.symbol for c in ub] == ["S7USDT", "S6USDT", "S5USDT", "S4USDT", "S3USDT"]
    # daily index returns before t do not depend on data from t on
    for d in range(D0 - 30, D0):
        assert index_return([c.symbol for c in ua], a, d) == index_return([c.symbol for c in ub], b, d)
    assert below_sma(a["S0USDT"], D0) == below_sma(b["S0USDT"], D0) is not None
    assert trailing_index_downside_beta(["S1USDT", "S2USDT"], a, a["S0USDT"], D0) == trailing_index_downside_beta(
        ["S1USDT", "S2USDT"], b, b["S0USDT"], D0
    )


def test_day_and_index_return() -> None:
    b = bars_from_closes({D0 - 1: 100.0, D0: 110.0})
    c = bars_from_closes({D0 - 1: 50.0, D0: 45.0})
    e = bars_from_closes({D0: 1.0})  # no previous close: skipped
    assert day_return(b, D0) == pytest.approx(0.10)
    assert day_return(b, D0 - 1) is None
    m, k = index_return(["B", "C", "E"], {"B": b, "C": c, "E": e}, D0)
    assert m == pytest.approx(0.0) and k == 2


# ---------------------------------------------------------------- regimes


def test_below_sma_and_negative_week() -> None:
    cl = {d: 100.0 for d in range(D0 - 60, D0 - 1)}
    cl[D0 - 1] = 90.0
    btc = bars_from_closes(cl)
    assert below_sma(btc, D0, 50) is True
    cl[D0 - 1] = 110.0
    assert below_sma(bars_from_closes(cl), D0, 50) is False
    assert below_sma(bars_from_closes({D0 - 1: 1.0}), D0, 50) is None
    assert negative_week(btc, D0) is True
    assert negative_week(bars_from_closes(cl), D0) is False
    assert negative_week(bars_from_closes({D0 - 1: 1.0}), D0) is None


def test_episodes() -> None:
    assert episodes([True, True, False, True, False, False, True, True, True]) == [2, 1, 3]
    assert episodes([False, False]) == []


# ---------------------------------------------------------------- regressions


def test_two_slope_recovers_known_slopes() -> None:
    rng = random.Random(3)
    x = [rng.gauss(0, 0.03) for _ in range(4000)]
    y = [0.001 + 1.5 * min(v, 0) + 1.0 * max(v, 0) + rng.gauss(0, 0.005) for v in x]
    f = two_slope(y, x, lag=5)
    assert f.b_dn == pytest.approx(1.5, abs=0.03)
    assert f.b_up == pytest.approx(1.0, abs=0.03)
    assert f.delta == pytest.approx(0.5, abs=0.05)
    assert f.a == pytest.approx(0.001, abs=0.0005)
    lo, hi = f.ci()
    assert lo < 0.5 < hi
    # exact (noise-free) fit: slopes exact, SEs ~ 0
    y0 = [2.0 * min(v, 0) + 0.5 * max(v, 0) for v in x]
    g = two_slope(y0, x, lag=5)
    assert g.delta == pytest.approx(1.5, abs=1e-9) and g.se_delta < 1e-9


def test_two_slope_dimson_sums_lags() -> None:
    rng = random.Random(5)
    x = [rng.gauss(0, 0.03) for _ in range(3001)]
    y = [1.2 * min(x[i], 0) + 0.9 * max(x[i], 0) + 0.3 * min(x[i - 1], 0) + 0.1 * max(x[i - 1], 0) for i in range(1, 3001)]
    f = two_slope(y, x[1:], x_prev=x[:-1])
    assert f.b_dn == pytest.approx(1.5, abs=1e-9)
    assert f.b_up == pytest.approx(1.0, abs=1e-9)


def test_ols_hac_lag0_matches_white_and_slope() -> None:
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.1, 5.9, 8.2, 9.8]
    coef, cov = ols_hac(ys, [[1.0] * 5, xs], 0)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    slope = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True)) / sum((a - mx) ** 2 for a in xs)
    assert coef[1] == pytest.approx(slope)
    assert cov.shape == (2, 2) and cov[1, 1] > 0


def test_trailing_index_downside_beta() -> None:
    rng = random.Random(9)
    r = {d: rng.gauss(0, 0.03) for d in range(D0 - 70, D0 + 5)}
    btc_cl, alt_cl = {D0 - 71: 100.0}, {D0 - 71: 100.0}
    for d in range(D0 - 70, D0 + 5):
        btc_cl[d] = btc_cl[d - 1] * (1 + r[d])
        alt_cl[d] = alt_cl[d - 1] * (1 + 1.6 * min(r[d], 0) + 1.1 * max(r[d], 0))
    bars = {"A": bars_from_closes(alt_cl)}
    assert trailing_index_downside_beta(["A"], bars, bars_from_closes(btc_cl), D0) == pytest.approx(1.6, abs=1e-9)


# ---------------------------------------------------------------- basket


def test_hedge_pick_weights_through_simulate() -> None:
    members = [f"A{i}" for i in range(20)]
    pk = hedge_pick(members, 1.2)
    assert pk.longs == [BTC] and pk.long_scale == pytest.approx(2.4) and pk.short_scale == 2.0
    assert hedge_pick(members[:19], 1.2).longs == []
    assert hedge_pick(members, None).longs == [] and hedge_pick(members, -0.1).longs == []
    # one week: every alt -10 %, BTC -5 %, beta 1.2 -> price +0.10 - 1.2 * 0.05 = +0.04 per unit of alt notional
    legs = {s: LegWeek(s, -0.10, 0.0, 0.0, 0.1, D0 + 6, False, 0, 0) for s in members}
    legs[BTC] = LegWeek(BTC, -0.05, -0.001, 0.0, 0.05, D0 + 6, False, 0, 0)
    uni = {s: Candidate(s, 0.0, 1e9, 100) for s in [*members, BTC]}
    res = simulate([Week(D0, uni, legs)], lambda wk: pk, fee=0.0, slip_mult=0.0)
    assert res[0].price == pytest.approx(0.04)
    assert res[0].funding == pytest.approx(1.2 * -0.001)  # long BTC pays positive funding
    assert res[0].turnover == pytest.approx(1.0 + 1.2 + 0.9 + 1.2 * 0.95)  # entry plus final exit at drifted weights


def test_active_net_moves_closing_cost_into_the_episode() -> None:
    def wr(day: int, n: int, price: float, cost: float) -> WeekResult:
        return WeekResult(day, 30, n, price, 0.0, cost, 0.0, 0.0, 0.0, 0.0, 0.0)

    res = [wr(0, 0, 0.0, 0.0), wr(7, 1, 0.02, 0.002), wr(14, 1, -0.01, 0.0005), wr(21, 0, 0.0, 0.0021), wr(28, 0, 0.0, 0.0)]
    out = active_net(res)
    assert [d for d, _ in out] == [7, 14]
    assert out[0][1] == pytest.approx(0.018)
    assert out[1][1] == pytest.approx(-0.01 - 0.0005 - 0.0021)


def test_mean_beta() -> None:
    assert mean_beta([1.0, None, 2.0]) == pytest.approx(1.5)
    assert mean_beta([None]) is None


def test_decompose_is_exact() -> None:
    rng = random.Random(11)
    rb = [rng.gauss(-0.005, 0.05) for _ in range(150)]
    ra = [0.002 + 1.4 * min(v, 0) + 1.1 * max(v, 0) + rng.gauss(0, 0.01) for v in rb]
    beta = [1.0 + 0.2 * rng.random() for _ in rb]
    h = decompose(ra, rb, beta)
    assert h.intercept + h.convexity + h.drift == pytest.approx(h.price, abs=1e-12)
    assert h.price == pytest.approx(statistics.fmean(-a + b * x for a, x, b in zip(ra, rb, beta, strict=True)))
    assert h.drift_share == (h.drift / h.price if h.price > 0 else None)


def test_path_mae() -> None:
    a = Bars.from_rows([[d * DAY_MS, 10.0, 10.0, 10.0, c, 1.0, 1.0] for d, c in ((D0, 12.0), (D0 + 1, 9.0))])
    btc = Bars.from_rows([[d * DAY_MS, 100.0, 100.0, 100.0, c, 1.0, 1.0] for d, c in ((D0, 110.0), (D0 + 1, 100.0))])
    # day 0: short alt -20 %, long BTC beta 1 * +10 % -> -10 %; day 1: +10 % + 0 -> worst -10 %
    assert path_mae(["A"], {"A": a}, btc, 1.0, D0, hold=2) == pytest.approx(-0.10)
    assert path_mae(["A"], {"A": a}, btc, 1.0, D0 + 5, hold=2) is None


# ---------------------------------------------------------------- inference


def test_diff_and_circular_shift() -> None:
    xs = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    fl = [True, True, False, False, False, False]
    assert diff_means(xs, fl) == pytest.approx(1.0)
    assert diff_means(xs, [True] * 6) is None
    obs, p, stats = circular_shift_test(xs, fl)
    assert obs == pytest.approx(1.0)
    assert len(stats) == 5 and p == pytest.approx(1 / 6)  # only the identity reaches the observed value


def test_block_bootstrap_cis() -> None:
    rng = random.Random(2)
    xs = [rng.gauss(0.5, 1.0) for _ in range(400)]
    lo, hi = block_bootstrap_mean_ci(xs, block=5, n_boot=2000)
    assert lo < statistics.fmean(xs) < hi and hi - lo == pytest.approx(2 * 1.96 / math.sqrt(400), rel=0.3)
    fl = [i % 10 < 5 for i in range(400)]
    ys = [x + (1.0 if f else 0.0) for x, f in zip(xs, fl, strict=True)]
    dl, dh = block_bootstrap_diff_ci(ys, fl, block=10, n_boot=2000)
    assert dl < diff_means(ys, fl) < dh and dl > 0.5
