from __future__ import annotations

import math
import random
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.options import black76_price
from tradedesk.analytics.put_skew import FundingIndex, run_book, year_frac
from tradedesk.analytics.weekly_straddle import (
    DAY_MS,
    HOUR_MS,
    atm_plan,
    book_path,
    constant_maturity_atm_iv,
    deribit_im_estimate,
    friday_expiry,
    fridays,
    is_friday,
    mde,
    newey_west_mean,
    permutation_p,
    pre_entry_slope,
    realized_vol_path,
    term_slope,
    threshold_spread,
)

FRI = int(datetime(2024, 3, 1, tzinfo=UTC).timestamp() * 1000)  # a Friday
EXP = friday_expiry(FRI)


def _days(spots, ivs=None, K=60_000.0, exp=EXP, n_strikes=1):
    days = {}
    for i, s in enumerate(spots):
        d = FRI + i * DAY_MS
        t = d + 9 * HOUR_MS
        iv = ivs[i] if ivs else 0.5
        strikes = {K + 1000.0 * j: {"mark_iv": iv, "put_delta": -0.5, "n": 3, "n_put": 1, "sell_iv": iv - 0.01} for j in range(n_strikes)}
        days[d] = {"day": d, "t_ms": t, "spot": s, "expiries": {exp: {"ratio": 1.0, "F": s, "T": year_frac(exp, t), "strikes": strikes}}}
    return days


def _straddle(K=60_000.0, iv=0.5):
    return [(K, True, -1.0, iv), (K, False, -1.0, iv)]


def test_calendar_helpers():
    assert is_friday(FRI) and not is_friday(FRI + DAY_MS)
    fr = fridays(FRI - 3 * DAY_MS, FRI + 15 * DAY_MS)
    assert fr == [FRI, FRI + 7 * DAY_MS, FRI + 14 * DAY_MS]
    assert datetime.fromtimestamp(EXP / 1000, UTC) == datetime(2024, 3, 8, 8, tzinfo=UTC)
    assert friday_expiry(FRI, 2) - FRI == 14 * DAY_MS + 8 * HOUR_MS


def test_book_path_matches_run_book_and_decomposes():
    rng = random.Random(3)
    spots = [60_000.0]
    for _ in range(6):
        spots.append(spots[-1] * math.exp(rng.gauss(0, 0.03)))
    ivs = [0.5 + 0.02 * rng.random() for _ in spots]
    days = _days(spots, ivs)
    funding = FundingIndex([(FRI + h * HOUR_MS, 0.00001 * ((h % 5) - 1)) for h in range(1, 200)])
    legs = _straddle(iv=0.485)
    for hedge in (True, False):
        a = run_book(days, FRI, EXP, legs, 63_000.0, funding=funding, hedge=hedge)
        b = book_path(days, FRI, EXP, legs, 63_000.0, funding=funding, hedge=hedge)
        assert b["pnl"] == pytest.approx(a["pnl"], rel=1e-12)
        for k in ("premium", "payoff", "hedge_pnl", "funding_pnl", "costs", "pnl_btc_at_expiry"):
            assert b[k] == pytest.approx(a[k], rel=1e-12, abs=1e-15)
        assert b["entry"] + sum(x[2] for x in b["intervals"]) == pytest.approx(b["pnl"], abs=1e-12)
        assert len(b["intervals"]) == 7  # Fri..Thu hedge points -> 6 daily intervals + settlement
        assert b["min_mtm"] <= min(b["entry"], b["pnl"]) + 1e-15
    # entry component = (exec - mark) premium - option fees - first hedge trade cost (spot at entry, F = S)
    b = book_path(days, FRI, EXP, legs, 63_000.0, hedge=False, option_fee=0.0)
    T0 = year_frac(EXP, FRI + 9 * HOUR_MS)
    s0 = spots[0]
    exp_entry = 2 * (black76_price(s0, 60_000.0, T0, 0.485, True) - black76_price(s0, 60_000.0, T0, ivs[0], True)) / s0
    assert b["entry"] == pytest.approx(exp_entry, rel=1e-9)  # put = call at K = F


def test_book_path_flat_market_earns_premium_minus_costs():
    days = _days([60_000.0] * 7)
    r = book_path(days, FRI, EXP, _straddle(), 60_000.0, option_fee=0.0, delivery_fee=0.0, hedge_fee=0.0)
    T0 = year_frac(EXP, FRI + 9 * HOUR_MS)
    prem = 2 * black76_price(60_000.0, 60_000.0, T0, 0.5, True) / 60_000.0
    assert r["pnl"] == pytest.approx(prem, rel=1e-9)
    assert r["premium"] == pytest.approx(prem, rel=1e-9)
    # analytic ATM straddle ~ 0.8 * sigma * sqrt(T)
    assert prem == pytest.approx(0.8 * 0.5 * math.sqrt(T0), rel=0.01)


def test_book_path_ignores_data_after_expiry():
    """Results of a trade must not change when data after its expiry (or the next week's data) changes."""
    days = _days([60_000.0, 60_500.0, 59_000.0, 61_000.0, 62_000.0, 61_500.0, 60_800.0, 99_000.0, 1_000.0])
    a = book_path(days, FRI, EXP, _straddle(), 60_700.0)
    days2 = dict(days)
    days2[FRI + 7 * DAY_MS] = dict(days[FRI + 7 * DAY_MS], spot=5.0)
    del days2[FRI + 8 * DAY_MS]
    assert book_path(days2, FRI, EXP, _straddle(), 60_700.0) == a


def _snap_multi(exp_ivs, spot=60_000.0, day=FRI, skew=0.01):
    t = day + 9 * HOUR_MS
    return {
        "day": day,
        "t_ms": t,
        "spot": spot,
        "expiries": {
            e: {
                "ratio": 1.0,
                "F": spot,
                "T": year_frac(e, t),
                "strikes": {
                    spot - 1000: {"mark_iv": iv + skew, "put_delta": -0.45, "n": 2},
                    spot + 1000: {"mark_iv": iv - skew, "put_delta": -0.55, "n": 4},
                },
            }
            for e, iv in exp_ivs.items()
        },
    }


def test_constant_maturity_and_slope():
    e7, e28, e35 = friday_expiry(FRI, 1), friday_expiry(FRI, 4), friday_expiry(FRI, 5)
    snap = _snap_multi({e7: 0.60, e28: 0.50, e35: 0.50}, skew=0.0)
    assert constant_maturity_atm_iv(snap, 30) == pytest.approx(0.50, rel=1e-9)
    assert term_slope(snap, e7) == pytest.approx(1.2, rel=1e-9)
    # total variance interpolation between 0.4 and 0.6
    snap = _snap_multi({e28: 0.40, e35: 0.60}, skew=0.0)
    t0, t1, tt = snap["expiries"][e28]["T"], snap["expiries"][e35]["T"], 30 / 365
    w = 0.16 * t0 + (0.36 * t1 - 0.16 * t0) * (tt - t0) / (t1 - t0)
    assert constant_maturity_atm_iv(snap, 30) == pytest.approx(math.sqrt(w / tt), rel=1e-9)
    assert constant_maturity_atm_iv(_snap_multi({e7: 0.5}), 30) is None
    assert term_slope(snap, e7) is None


def test_pre_entry_slope_uses_previous_day_snapshot_only():
    """H2 slope must come from the d-1 surface: changing the entry-day snapshot (or later days) cannot move it."""
    e7, e28, e35 = friday_expiry(FRI, 1), friday_expiry(FRI, 4), friday_expiry(FRI, 5)
    thu = FRI - DAY_MS
    days = {
        thu: _snap_multi({e7: 0.60, e28: 0.50, e35: 0.50}, day=thu, skew=0.0),
        FRI: _snap_multi({e7: 0.90, e28: 0.40, e35: 0.40}, day=FRI, skew=0.0),
    }
    a = pre_entry_slope(days, FRI, e7)
    assert a == pytest.approx(term_slope(days[thu], e7), rel=1e-12)
    assert a != pytest.approx(term_slope(days[FRI], e7), rel=1e-3)
    days2 = dict(days)
    days2[FRI] = _snap_multi({e7: 0.30, e28: 0.70, e35: 0.70}, day=FRI, skew=0.0)
    days2[FRI + DAY_MS] = _snap_multi({e7: 2.0, e28: 0.1, e35: 0.1}, day=FRI + DAY_MS, skew=0.0)
    assert pre_entry_slope(days2, FRI, e7) == a
    del days2[thu]
    assert pre_entry_slope(days2, FRI, e7) is None  # no fallback to the entry snapshot


def test_atm_plan_picks_nearest_strike_and_delta_band():
    snap = _snap_multi({EXP: 0.5}, spot=60_400.0)
    p = atm_plan(snap, EXP)
    assert p["K"] == 59_400.0  # |59400 - 60400| = |61400 - 60400|: tie -> lower strike
    assert p["covered"] and p["call_delta"] == pytest.approx(0.55)
    assert p["prints_near"] == 6
    assert atm_plan(snap, EXP + 7 * DAY_MS) is None
    snap["expiries"][EXP]["strikes"][59_400.0]["put_delta"] = -0.2
    assert not atm_plan(snap, EXP)["covered"]


def test_realized_vol_path_and_margin():
    spots = [(i * DAY_MS, 100.0 * math.exp(0.01 * (i % 2))) for i in range(8)]  # |r| = 0.01 every day
    assert realized_vol_path(spots) == pytest.approx(0.01 * math.sqrt(365), rel=1e-9)
    assert realized_vol_path(spots[:1]) is None
    assert deribit_im_estimate(60_000, 60_000, True, 0.02) == pytest.approx(0.17)
    assert deribit_im_estimate(70_000, 60_000, True, 0.001) == pytest.approx(0.101)  # floor 0.10
    assert deribit_im_estimate(58_000, 60_000, False, 0.0) == pytest.approx(0.15 - 2 / 60)


def test_newey_west_and_mde():
    x = [1.0, -1.0] * 50
    nw0 = newey_west_mean(x, 0)
    assert nw0["mean"] == 0.0 and nw0["se"] == pytest.approx(math.sqrt(1.0 / 100))
    # negative autocorrelation shrinks the long-run variance
    assert newey_west_mean(x, 1)["se"] < nw0["se"]
    y = [1.0] * 50 + [-1.0] * 50  # strong positive autocorrelation widens it
    assert newey_west_mean(y, 4)["se"] > newey_west_mean(y, 0)["se"]
    assert mde(2.0, 400) == pytest.approx(0.28)


def test_threshold_spread_and_permutation():
    rng = random.Random(1)
    rows = []
    for i in range(400):
        s = 0.9 + 0.2 * rng.random()
        rows.append({"slope": s, "dvol_pct": (i % 10) / 10, "pnl": (0.01 if s > 1.03 else 0.0) + rng.gauss(0, 0.002)})
    est = threshold_spread(rows)
    assert est == pytest.approx(0.01, abs=0.001)
    assert permutation_p(rows, n_perm=200) < 0.01
    null = [dict(r, pnl=rng.gauss(0, 0.002)) for r in rows]
    assert permutation_p(null, n_perm=200) > 0.01
    assert threshold_spread(rows[:15]) is None  # cells below min_n
