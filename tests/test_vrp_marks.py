"""VRP on real marks: straddle plan, hedge path, hedged book, statistics and verdict rules (pure)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.options import black76_greeks, black76_price
from tradedesk.analytics.put_skew import DAY_MS, HOUR_MS, chain, run_book, trailing_percentile, year_frac
from tradedesk.analytics.vrp_marks import (
    block_bootstrap_means,
    bootstrap_summary,
    classify_primary,
    classify_secondary,
    hedge_path,
    hedged_book,
    holm,
    mde,
    plan_straddle,
    select_signal_days,
    straddle_legs,
)

D0 = 1_655_251_200_000  # 2022-06-15 00:00 UTC


def _days(spots, exp_ms, ivs=None, K=20_000.0, ratio=1.0, strikes=None):
    """One surface per day at 09:00 with a single expiry; ``ivs`` per day (default 0.6)."""
    out = {}
    for i, s in enumerate(spots):
        d = D0 + i * DAY_MS
        t = d + 9 * HOUR_MS
        T = year_frac(exp_ms, t)
        iv = ivs[i] if ivs else 0.6
        ks = strikes or [K]
        out[d] = {
            "day": d,
            "t_ms": t,
            "spot": float(s),
            "expiries": {
                exp_ms: {
                    "ratio": ratio,
                    "F": s * ratio,
                    "T": T,
                    "n_prints": 10,
                    "strikes": {
                        k: {"mark_iv": iv, "put_delta": black76_greeks(s * ratio, k, T, iv, False)["delta"], "n": 1, "n_put": 1} for k in ks
                    },
                }
            },
        }
    return out


def _entry(days, exp):
    x = days[D0]["expiries"][exp]
    return {"day": D0, "spot": days[D0]["spot"], "F": x["F"], "T": x["T"], "ratio": x["ratio"]}


def test_flat_spot_unhedged_straddle_keeps_the_premium():
    """Spot never moves and settles at the strike: P&L = premium exactly (no costs)."""
    exp = D0 + 5 * DAY_MS + 8 * HOUR_MS
    days = _days([20_000] * 5, exp)
    path = hedge_path(days, D0, exp, [20_000.0, 20_000.0])
    r = hedged_book(_entry(days, exp), straddle_legs(20_000.0, 0.6), path, exp, 20_000.0, option_fee=0, delivery_fee=0, hedge_fee=0)
    T0 = year_frac(exp, D0 + 9 * HOUR_MS)
    prem = 2 * black76_price(20_000, 20_000, T0, 0.6, True)
    assert r["pnl"] == pytest.approx(prem / 20_000, rel=1e-12)
    assert r["hedge_pnl"] == pytest.approx(0.0, abs=1e-12)  # ATM straddle delta ~0 and no moves
    assert r["hedge_days"] == 5


def test_unhedged_payoff_and_mtm_start_is_minus_spread():
    exp = D0 + 4 * DAY_MS + 8 * HOUR_MS
    days = _days([20_000] * 4, exp)
    path = hedge_path(days, D0, exp, [20_000.0, 20_000.0])
    r = hedged_book(_entry(days, exp), straddle_legs(20_000.0, 0.59), path, exp, 23_000.0, option_fee=0, delivery_fee=0, hedge=False)
    T0 = year_frac(exp, D0 + 9 * HOUR_MS)
    prem = 2 * black76_price(20_000, 20_000, T0, 0.59, True)
    assert r["pnl"] == pytest.approx((prem - 3_000) / 20_000, rel=1e-12)
    # first MTM point: sold 1 vp below the mark -> about -vega * 1 vp; worst MTM is the final loss here
    vega = 2 * black76_greeks(20_000, 20_000, T0, 0.6, True)["vega"]
    assert r["mtm_min"] == pytest.approx(r["pnl"], rel=1e-12)
    flat = hedged_book(_entry(days, exp), straddle_legs(20_000.0, 0.59), path, exp, 20_000.0, option_fee=0, delivery_fee=0, hedge=False)
    assert flat["mtm_min"] == pytest.approx(-vega / 20_000, rel=0.02)


def test_matches_run_book_with_marks_and_funding():
    """With the marks path the book reproduces put_skew.run_book (the put-skew v2 pipeline) exactly."""
    exp = D0 + 7 * DAY_MS + 8 * HOUR_MS
    spots = [20_000, 20_600, 19_800, 21_500, 21_000, 22_300, 21_900]
    ivs = [0.60, 0.62, 0.58, 0.66, 0.63, 0.70, 0.65]
    days = _days(spots, exp, ivs=ivs, ratio=1.001, strikes=[18_000.0, 20_000.0, 22_000.0])
    del days[D0 + 3 * DAY_MS]  # a missing day keeps the previous hedge
    funding = [(D0 + h * HOUR_MS, 0.00002 * ((h % 7) - 2)) for h in range(0, 24 * 9)]
    legs = straddle_legs(20_000.0, 0.59)
    rb = run_book(days, D0, exp, legs, 22_100.0, funding=funding)
    hb = hedged_book(_entry(days, exp), legs, hedge_path(days, D0, exp, [20_000.0, 20_000.0]), exp, 22_100.0, funding=funding)
    for k in ("pnl", "premium", "payoff", "hedge_pnl", "funding_pnl", "costs", "vega", "hedge_days", "stale_leg_days"):
        assert hb[k] == pytest.approx(rb[k], rel=1e-12, abs=1e-15), k
    assert hb["mtm_min"] <= hb["pnl"] + 1e-15


def test_fixed_vol_path_uses_given_vols():
    exp = D0 + 3 * DAY_MS + 8 * HOUR_MS
    days = _days([20_000, 21_000, 20_500], exp, ivs=[0.6, 0.9, 0.9])
    dv = {D0: 0.5, D0 + DAY_MS: 0.55}
    p = hedge_path(days, D0, exp, [20_000.0, 20_000.0], vol_by_day=dv)
    assert [pt[3] for pt in p] == [[0.5, 0.5], [0.55, 0.55], [None, None]]
    r = hedged_book(_entry(days, exp), straddle_legs(20_000.0, 0.5), p, exp, 20_000.0)
    assert r["stale_leg_days"] == 2  # the third day carries the previous vol forward


def test_no_lookahead_after_expiry_and_before_t():
    """A trade's result must not change when data after its expiry change; the path stops before expiry."""
    exp = D0 + 3 * DAY_MS + 8 * HOUR_MS
    days = _days([20_000, 20_400, 19_700, 30_000, 5_000, 1_000], exp)
    legs = straddle_legs(20_000.0, 0.59)
    a = hedged_book(_entry(days, exp), legs, hedge_path(days, D0, exp, [20_000.0] * 2), exp, 19_900.0)
    days2 = dict(days)
    days2[D0 + 3 * DAY_MS] = dict(days[D0 + 3 * DAY_MS], spot=99_999.0)  # 09:00 on expiry day is after 08:00
    days2[D0 + 4 * DAY_MS] = dict(days[D0 + 4 * DAY_MS], spot=1.0)
    del days2[D0 + 5 * DAY_MS]
    b = hedged_book(_entry(days2, exp), legs, hedge_path(days2, D0, exp, [20_000.0] * 2), exp, 19_900.0)
    assert a == b
    assert len(hedge_path(days, D0, exp, [20_000.0])) == 3
    # the trailing percentile used for the high-DVOL signal ignores later data
    s = [(D0 + i * DAY_MS, 0.4 + 0.01 * (i % 11)) for i in range(400)]
    p1 = trailing_percentile(s, window_ms=365 * DAY_MS, min_obs=180)
    s2 = s[:300] + [(t, 9.0) for t, _ in s[300:]]
    p2 = trailing_percentile(s2, window_ms=365 * DAY_MS, min_obs=180)
    assert all(p1[t] == p2[t] for t in p1 if t < D0 + 300 * DAY_MS)


def test_plan_straddle_picks_nearest_expiry_and_atm():
    e_near = D0 + 31 * DAY_MS + 8 * HOUR_MS
    e_far = D0 + 45 * DAY_MS + 8 * HOUR_MS
    a = _days([20_000], e_near, strikes=[19_000.0, 20_000.0, 21_000.0], ratio=1.004)[D0]
    b = _days([20_000], e_far, strikes=[19_000.0, 20_000.0, 21_000.0])[D0]
    snap = dict(a, expiries={**a["expiries"], **b["expiries"]})
    p = plan_straddle(snap)
    assert p["expiry"] == e_near and p["k"] == 20_000.0 and p["covered"]
    assert p["dte"] == pytest.approx(31 - 1 / 24)
    assert plan_straddle(snap, dte_band=(40, 56))["expiry"] == e_far
    assert plan_straddle(snap, dte_band=(5, 10)) is None
    far_otm = _days([20_000], e_near, strikes=[30_000.0])[D0]
    assert plan_straddle(far_otm)["covered"] is False


def test_signal_selection_and_chain():
    entries = {D0 + i * DAY_MS: {"expiry": D0 + (i + 10) * DAY_MS + 8 * HOUR_MS} for i in range(60)}
    pct = {D0 + i * DAY_MS: (0.9 if i in (3, 4, 20, 40) else 0.5) for i in range(60)}
    sel = select_signal_days(entries, pct, 0.80)
    assert sorted(sel) == [D0 + i * DAY_MS for i in (3, 4, 20, 40)]
    assert chain(sel, D0, D0 + 59 * DAY_MS) == [D0 + 3 * DAY_MS, D0 + 20 * DAY_MS, D0 + 40 * DAY_MS]


def test_bootstrap_holm_mde():
    assert block_bootstrap_means([0.01] * 10, n_boot=50) == [pytest.approx(0.01)] * 50
    s = bootstrap_summary([0.01, 0.02, 0.03, 0.015, 0.025, 0.02], block=2, n_boot=2000)
    assert s["p"] == 0.0 and s["ci"][0] > 0 and s["mean"] == pytest.approx(0.02)
    sym = bootstrap_summary([1.0, -1.0] * 20, block=2, n_boot=2000)
    assert sym["p"] > 0.5
    assert holm([0.01, 0.04]) == pytest.approx([0.02, 0.04])
    assert holm([0.04, 0.03]) == pytest.approx([0.06, 0.06])
    assert mde(1.0, 1) == pytest.approx(1.959964 + 0.841621, abs=1e-5)
    assert mde(0.037, 66) == pytest.approx(2.801585 * 0.037 / math.sqrt(66), rel=1e-6)


def test_verdict_rules():
    assert classify_primary(0.012, (0.004, 0.02), 0.01, -0.15) == "confirmed"
    assert classify_primary(0.012, (0.004, 0.02), 0.01, -0.25) == "rejected"  # ruin gate
    assert classify_primary(0.012, (0.004, 0.02), 0.06, -0.15) == "inconclusive"  # fails after Holm
    assert classify_primary(0.003, (0.001, 0.006), 0.01, -0.1) == "inconclusive"  # below +0.5 %
    assert classify_primary(-0.004, (-0.012, 0.004), 0.4, -0.1) == "rejected"
    assert classify_primary(0.0006, (-0.0083, 0.0095), 0.9, -0.1) == "inconclusive"
    assert classify_secondary(0.03, (0.01, 0.05), 0.01, -0.25, 0.01) == "rejected"
    assert classify_secondary(0.03, (0.01, 0.05), 0.01, -0.10, 0.03) == "descriptive"
    assert classify_secondary(0.03, (0.01, 0.05), 0.01, -0.10, 0.015) == "confirmed"
    assert classify_secondary(0.0, (-0.02, 0.008), 0.9, -0.10, 0.015) == "rejected"
    assert classify_secondary(0.01, (-0.01, 0.03), 0.3, -0.10, 0.015) == "inconclusive"
