"""Pump fade analytics (IDEA-20261025-extreme-pump-fade-short): analytic references and point-in-time checks."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.funding_crowding import Bars, LegWeek, leg_week, run_start_map
from tradedesk.analytics.listing_drift import DAY_MS
from tradedesk.analytics.pump_fade import (
    btc_regime,
    capped_weights,
    first_breach,
    first_events,
    max_concurrent,
    placebo_members,
    pre_pump_beta,
    ret_n,
    short_trade,
    universe_member,
)


def bars_from(
    closes: dict[int, float], *, qv: float = 1e7, opens: dict[int, float] | None = None, highs: dict[int, float] | None = None
) -> Bars:
    rows = []
    for d, c in sorted(closes.items()):
        o = (opens or {}).get(d, c)
        h = (highs or {}).get(d, max(o, c))
        rows.append([d * DAY_MS, o, h, min(o, c), c, qv / c, qv])
    return Bars.from_rows(rows)


def leg(r: float, funding_long: float = 0.0, up: float = 0.0) -> LegWeek:
    return LegWeek("X", r, funding_long, up, 0.0, 0, False, 0, 0)


def test_ret_n_and_missing_bar():
    b = bars_from({0: 100.0, 7: 130.0})
    assert ret_n(b, 7) == pytest.approx(0.30)
    assert ret_n(b, 8) is None


def test_universe_member_reasons():
    closes = {d: 1.0 for d in range(200)}
    closes[150] = 1.5
    b = bars_from(closes)
    st = run_start_map(b)
    assert universe_member(b, 50, st)[2] == "young"
    r, mq, why = universe_member(b, 150, st)
    assert why == "ok" and r == pytest.approx(0.5) and mq == pytest.approx(1e7)
    thin = bars_from(closes, qv=1e6)
    assert universe_member(thin, 150, run_start_map(thin))[2] == "illiquid"
    assert universe_member(b, 250, st)[2] == "no_bar"


def test_first_events_cooldown():
    trig = [(10, 0.35), (11, 0.40), (20, 0.31), (24, 0.33), (30, 0.50), (40, 0.10), (45, 0.30)]
    assert first_events(trig, 0.30) == [10, 24, 45]
    assert first_events(trig, 0.45) == [30]


def test_first_events_point_in_time():
    trig = [(d, 0.3 + 0.01 * (d % 5)) for d in range(0, 100, 3)]
    full = first_events(trig, 0.30)
    for t in (20, 47, 80):
        assert first_events([x for x in trig if x[0] <= t], 0.30) == [d for d in full if d <= t]


def test_universe_and_beta_do_not_see_the_future():
    btc = {d: 100.0 * (1.0 + 0.01 * math.sin(d)) for d in range(300)}
    coin = {d: 50.0 * (1.0 + 0.02 * math.sin(d)) for d in range(300)}
    t = 200
    b0, c0 = bars_from(btc), bars_from(coin)
    m0 = universe_member(c0, t, run_start_map(c0))
    beta0 = pre_pump_beta(c0, b0, t)
    coin2 = dict(coin)
    for d in range(t - 6, 300):  # the pump window itself and everything after it changes
        coin2[d] = coin[d] * (3.0 if d > t else 1.4)
    c2 = bars_from(coin2)
    assert pre_pump_beta(c2, b0, t) == pytest.approx(beta0)
    coin3 = {d: (v * 5.0 if d > t else v) for d, v in coin.items()}
    c3 = bars_from(coin3)
    assert universe_member(c3, t, run_start_map(c3)) == pytest.approx(m0)


def test_pre_pump_beta_analytic_and_clip():
    btc = {0: 100.0}
    coin = {0: 10.0}
    for d in range(1, 200):
        rb = 0.02 * math.sin(1.7 * d)
        btc[d] = btc[d - 1] * (1 + rb)
        coin[d] = coin[d - 1] * (1 + 2.0 * rb)
    b, c = bars_from(btc), bars_from(coin)
    assert pre_pump_beta(c, b, 150) == pytest.approx(2.0)
    coin5 = {0: 10.0}
    for d in range(1, 200):
        coin5[d] = coin5[d - 1] * (1 + 5.0 * (btc[d] / btc[d - 1] - 1))
    assert pre_pump_beta(bars_from(coin5), b, 150) == 3.0
    assert pre_pump_beta(c, b, 60) is None  # fewer than 60 returns before t - 7


def test_short_trade_components():
    t = short_trade(leg(-0.10, funding_long=-0.004), leg(0.05, funding_long=-0.001), 1.5, cost_coin=0.0025, cost_btc=0.001)
    assert t.price_coin == pytest.approx(0.10)
    assert t.hedge == pytest.approx(0.075)
    assert t.funding_coin == pytest.approx(0.004)
    assert t.funding_hedge == pytest.approx(-0.0015)
    assert t.cost == pytest.approx(0.004)
    assert t.net == pytest.approx(0.10 + 0.075 + 0.004 - 0.0015 - 0.004)
    nh = short_trade(leg(0.20, funding_long=0.001), None, 1.5)
    assert nh.net == pytest.approx(-0.20 - 0.001 - 0.0025)
    ov = short_trade(leg(0.90), leg(0.0), 1.0, coin_r=1 / 3, coin_funding=0.0)
    assert ov.price_coin == pytest.approx(-1 / 3)


def test_first_breach_gap_fill():
    closes = {0: 100.0, 1: 110.0, 2: 150.0, 3: 160.0}
    b = bars_from(closes, opens={0: 100.0, 1: 100.0, 2: 140.0, 3: 150.0}, highs={1: 120.0, 2: 155.0, 3: 170.0})
    assert first_breach(b, 1, 3, 0.35) == (2, pytest.approx(0.40))  # gap open 140 > stop 135
    assert first_breach(b, 1, 3, 0.15) == (1, 0.15)
    assert first_breach(b, 1, 3, 0.80) is None


def test_placebo_members_percentiles():
    r7 = {f"S{i:02d}": float(i) for i in range(21)}
    assert placebo_members(r7) == ["S08", "S09", "S10", "S11", "S12"]
    assert placebo_members(r7, exclude=["S10"]) == ["S08", "S09", "S11", "S12"]
    assert placebo_members({k: v for k, v in list(r7.items())[:10]}) == []


def test_btc_regime():
    b = bars_from({0: 100.0, 30: 115.0, 60: 100.0, 90: 95.0})
    assert btc_regime(b, 30) == 1
    assert btc_regime(b, 60) == -1
    assert btc_regime(b, 90) == 0
    assert btc_regime(b, 45) is None


def test_capped_weights_and_concurrency():
    entries = [10] * 7 + [20]
    w = capped_weights(entries, 7)
    assert w[:7] == [pytest.approx(0.03 * 5 / 7)] * 7
    assert w[7] == pytest.approx(0.03)
    assert max_concurrent(entries, 7) == 7
    assert max_concurrent([1, 5, 8, 9], 7) == 3  # day 8: entries 5 and 8 (1 closed after day 7); day 9: 5, 8, 9


def test_leg_week_hold_matches_card():
    closes = {d: 100.0 + d for d in range(20)}
    b = bars_from(closes, opens={d: 100.0 + d - 0.5 for d in range(20)})
    lg = leg_week("X", b, [], 6, hold=7)  # signal t = 5 -> entry open(6), exit close(12)
    assert lg is not None and lg.exit == 12
    assert lg.r == pytest.approx(112.0 / 105.5 - 1)
