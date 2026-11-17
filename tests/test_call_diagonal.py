"""Rolled call diagonal (LIB-20261115-datahawks-btc-diagonal, D2): synthetic flat-smile surfaces, analytic P&L."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.call_diagonal import (
    DEFAULTS,
    delivery_cost,
    front_month_diff,
    pick_long,
    plan_entry,
    run_position,
)
from tradedesk.analytics.option_ideas import fee
from tradedesk.analytics.options import black76_price
from tradedesk.analytics.trend_spread import DAY_MS, DELIVERY_CAP, DELIVERY_RATE

H = 3_600_000
S0 = 40_000.0


def ms(y: int, m: int, d: int, h: int = 0) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp() * 1000)


ENTRY = ms(2024, 1, 6)  # Saturday
FRIDAYS = [ms(2024, 1, 12, 8) + 7 * k * DAY_MS for k in range(8)] + [ms(2024, 3, 29, 8)]
LONG_EXP = ms(2024, 2, 16, 8)  # 40.96 d from the entry surface
EXIT_DAY = ms(2024, 2, 9)  # long DTE 6.96 < 8


def surface(day: int, spot: float = S0, iv: float = 0.5, front_iv: float | None = None) -> dict:
    t = day + 9 * H
    exps = {}
    for e in FRIDAYS:
        if e <= t:
            continue
        v = front_iv if front_iv is not None and e - t <= 8 * DAY_MS else iv
        exps[e] = {"F": spot, "T": (e - t) / (365 * DAY_MS), "strikes": {float(k): {"mark_iv": v} for k in range(30_000, 61_000, 1_000)}}
    return {"day": day, "t_ms": t, "spot": spot, "expiries": exps}


def world(spot: float = S0) -> dict[int, dict]:
    return {ENTRY + k * DAY_MS: surface(ENTRY + k * DAY_MS, spot) for k in range(0, 60)}


def P(**kw):
    return {**DEFAULTS, "hs": 0.0, **kw}


def flat_delivery(price: float):
    return lambda e: price


def test_pick_long_smallest_strike_at_or_above_forward():
    s = surface(ENTRY, spot=40_400.0)
    lg, why = pick_long(s, P())
    assert why == ""
    assert lg["exp"] == LONG_EXP and lg["K"] == 41_000.0
    # forward far from any fresh strike -> rejected by the band
    s2 = surface(ENTRY)
    for x in s2["expiries"].values():
        x["strikes"] = {k: v for k, v in x["strikes"].items() if k < 40_000 or k > 45_000}
    assert pick_long(s2, P())[0] is None


def test_grid_variant_prices_listed_strike_from_the_smile():
    s = surface(ENTRY, spot=40_400.0)
    for x in s["expiries"].values():  # fresh marks only at 39k and 42k (42k at a higher IV)
        x["strikes"] = {39_000.0: {"mark_iv": 0.50}, 42_000.0: {"mark_iv": 0.53}}
    # fresh-only rule: the next fresh strike 42k is 1 600 above F > 3 % of spot -> skipped (the coverage loss)
    assert pick_long(s, P()) == (None, "long: no fresh strike")
    lg, why = pick_long(s, P(grid=1000.0))
    assert why == "" and lg["K"] == 41_000.0
    w = (math.log(41_000 / 40_400) - math.log(39_000 / 40_400)) / (math.log(42_000 / 40_400) - math.log(39_000 / 40_400))
    assert lg["iv"] == pytest.approx(0.50 + 0.03 * w, abs=1e-12)
    # no fresh strike above the grid strike -> not priced (no extrapolation)
    for x in s["expiries"].values():
        x["strikes"] = {39_000.0: {"mark_iv": 0.5}, 40_000.0: {"mark_iv": 0.5}}
    assert pick_long(s, P(grid=1000.0))[0] is None


def test_naked_call_pnl_is_black76_entry_and_exit():
    days = world()
    res, why = run_position(days, flat_delivery(S0), ENTRY, 0.0, P())
    assert why == ""
    t0, t1 = ENTRY + 9 * H, EXIT_DAY + 9 * H
    c0 = black76_price(S0, 40_000.0, (LONG_EXP - t0) / (365 * DAY_MS), 0.5, True)
    c1 = black76_price(S0, 40_000.0, (LONG_EXP - t1) / (365 * DAY_MS), 0.5, True)
    debit = c0 + fee(S0, c0)
    assert res["debit"] == pytest.approx(debit, rel=1e-9)
    assert res["pnl"] == pytest.approx(c1 - fee(S0, c1) - debit, rel=1e-9)
    assert res["r"] == pytest.approx(res["pnl"] / debit, rel=1e-12)
    assert res["exit_ms"] == t1 and res["rolls"] == 0


def test_diagonal_otm_shorts_add_their_credits():
    days = world()
    naked, _ = run_position(days, flat_delivery(S0), ENTRY, 0.0, P())
    diag, why = run_position(days, flat_delivery(S0), ENTRY, 1.0, P())
    assert why == ""
    # first short sold at entry, rolled on 12.01, 19.01, 26.01, 02.02; long closed on 09.02
    assert diag["rolls"] == 4 and diag["skipped"] == 0 and diag["itm"] == 0
    assert diag["exit_ms"] == naked["exit_ms"]
    assert diag["pnl"] == pytest.approx(naked["pnl"] + diag["credits"], rel=1e-9)
    plan, _ = plan_entry(days[ENTRY], 1.0, P())
    assert diag["debit"] == pytest.approx(naked["debit"] - plan["credit0"], rel=1e-9)
    assert 0 < plan["credit0"] < diag["credits"]
    half, _ = run_position(days, flat_delivery(S0), ENTRY, 0.5, P())
    assert half["credits"] == pytest.approx(diag["credits"] / 2, rel=1e-9)


def test_itm_short_pays_intrinsic_and_delivery_fee():
    assert delivery_cost(43_000.0, 42_000.0) == 0.0
    v = 50_000.0 - 43_000.0
    assert delivery_cost(43_000.0, 50_000.0) == pytest.approx(v + min(DELIVERY_RATE * 50_000.0, DELIVERY_CAP * v))
    days = world()
    first_short = ms(2024, 1, 12, 8)
    plan, _ = plan_entry(days[ENTRY], 1.0, P())
    K = plan["short"]["K"]
    base, _ = run_position(days, flat_delivery(S0), ENTRY, 1.0, P())
    hit, _ = run_position(days, lambda e: 50_000.0 if e == first_short else S0, ENTRY, 1.0, P())
    assert hit["itm"] == 1
    assert hit["pnl"] == pytest.approx(base["pnl"] - delivery_cost(K, 50_000.0), rel=1e-9)


def test_results_do_not_depend_on_data_after_exit():
    days = world()
    before, _ = run_position(days, flat_delivery(S0), ENTRY, 1.0, P(hs=0.015))
    later = dict(days)
    for d in list(later):
        if d > EXIT_DAY:
            later[d] = surface(d, spot=90_000.0, iv=2.0)
    after, _ = run_position(later, lambda e: 90_000.0 if e > EXIT_DAY + 9 * H else S0, ENTRY, 1.0, P(hs=0.015))
    assert after == before
    # the entry plan uses the entry surface only
    assert plan_entry(days[ENTRY], 1.0, P()) == plan_entry(later[ENTRY], 1.0, P())


def test_skipped_roll_waits_a_week_and_missing_surface_is_reported():
    days = world()
    # no fresh strike above the long strike on the 12.01 surface -> roll skipped, next check 19.01
    d = ms(2024, 1, 12)
    s = surface(d)
    for e, x in s["expiries"].items():
        if e < LONG_EXP:
            x["strikes"] = {k: v for k, v in x["strikes"].items() if k < 40_000}
    days[d] = s
    res, _ = run_position(days, flat_delivery(S0), ENTRY, 1.0, P())
    assert res["skipped"] == 1 and res["rolls"] == 3
    gone = {k: v for k, v in world().items() if not EXIT_DAY <= k <= EXIT_DAY + 2 * DAY_MS}
    assert run_position(gone, flat_delivery(S0), ENTRY, 1.0, P()) == (None, "no check surface")


def test_front_month_diff_measures_front_minus_month():
    s = surface(ENTRY, front_iv=0.62)
    out = front_month_diff(s)
    assert out["state"] == "measured"
    assert out["diff"] == pytest.approx(0.12, abs=1e-12)
