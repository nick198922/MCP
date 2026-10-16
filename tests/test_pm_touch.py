"""Tests for analytics.pm_touch (Polymarket "What price will … hit" touch markets)."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tradedesk.analytics.pm_threshold import quote_at
from tradedesk.analytics.pm_touch import (
    DayPath,
    buy_return,
    classify_touch_market,
    day_window,
    fee_per_share,
    jump_trade_offsets,
    max_losing_streak,
    parse_daily_slug,
    parse_strike,
    pick_strangle,
    rules_direction,
    strangle_return,
    touch_prob,
    touched,
)

FIX = Path(__file__).parent / "fixtures" / "polymarket_touch_event_2026-10-16.json"


def _event() -> dict:
    return json.loads(FIX.read_text())


def test_day_window_is_et_calendar_day_with_dst():
    s, e = day_window(date(2026, 10, 14))
    assert datetime.fromtimestamp(s, UTC) == datetime(2026, 10, 14, 4, 0, tzinfo=UTC)
    assert e - s == 86_400
    s, e = day_window(date(2026, 3, 8))  # spring forward: 23-hour day
    assert e - s == 23 * 3600
    s, e = day_window(date(2026, 11, 1))  # fall back: 25-hour day
    assert e - s == 25 * 3600


def test_parse_strike_and_slug():
    assert parse_strike("Will Bitcoin reach $91,000 on October 14?") == 91_000
    assert parse_strike("Will Ethereum dip to $2,300 on October 14?") == 2_300
    assert parse_strike("Will Bitcoin reach $95k on May 1?") == 95_000
    assert parse_strike("Will Bitcoin reach 120500 on August 15?") is None  # 2025 format without "$": excluded
    assert parse_daily_slug("what-price-will-bitcoin-hit-on-october-14-2026") == ("bitcoin", 10, 14, 2026)
    assert parse_daily_slug("what-price-will-ethereum-hit-on-march-9") == ("ethereum", 3, 9, None)
    assert parse_daily_slug("what-price-will-ethereum-hit-october-12-18") is None


def test_classify_real_sample_up_and_down():
    ev = _event()
    out = {m["groupItemTitle"]: classify_touch_market("bitcoin", ev, m) for m in ev["markets"]}
    up, dn = out["↑ 86,000"], out["↓ 81,000"]
    assert up["status"] == dn["status"] == "ok"
    assert (up["direction"], up["strike"], dn["direction"], dn["strike"]) == ("up", 86_000, "down", 81_000)
    assert up["date"] == "2026-10-14" and up["start"] == day_window(date(2026, 10, 14))[0]
    assert dn["outcome"] == 1  # the post: BTC touched 81 000 on 2026-10-14
    assert up["fee_rate"] == 0.07 and up["fee_exp"] == 1.0
    # wrong asset -> slug check fails before anything else
    assert classify_touch_market("ethereum", ev, ev["markets"][0])["status"] == "slug"


def test_classify_rejections():
    ev = _event()
    m = dict(ev["markets"][0])
    assert classify_touch_market("bitcoin", ev, {**m, "groupItemTitle": "↓ 86,000"})["status"] == "direction"
    assert classify_touch_market("bitcoin", ev, {**m, "question": "Will Bitcoin dip to $86,000 on October 14?"})["status"] == "direction"
    assert classify_touch_market("bitcoin", ev, {**m, "description": m["description"].replace("BTC/USDT", "ETH/USDT")})["status"] == "rules"
    assert classify_touch_market("bitcoin", ev, {**m, "endDate": "2026-10-15T05:00:00Z"})["status"] == "window"
    assert classify_touch_market("bitcoin", ev, {**m, "question": "Will Bitcoin reach $86,000 on October 15?"})["status"] == "window"
    assert classify_touch_market("bitcoin", ev, {**m, "outcomePrices": '["0.5", "0.5"]'})["status"] == "unresolved"


def test_rules_direction_texts():
    ev = _event()
    texts = {m["groupItemTitle"]: m["description"] for m in ev["markets"]}
    assert rules_direction(texts["↑ 86,000"], "bitcoin") == "up"
    assert rules_direction(texts["↓ 81,000"], "bitcoin") == "down"
    assert rules_direction(texts["↑ 86,000"], "ethereum") is None


def test_daypath_no_lookahead_and_extremes():
    start = day_window(date(2026, 10, 14))[0]
    t0 = start + 30 * 60

    def build(late_high: float) -> DayPath:
        dp = DayPath(start, start + 86_400, [t0])
        dp.add(start - 60, 999.0, 1.0, 50.0)  # previous day: ignored
        for i in range(30):
            dp.add(start + 60 * i, 101.0 + i * 0.1, 99.0, 100.0 + i)
        dp.add(t0, late_high, 98.0, 100.0)  # opens at t0: not part of the pre-t0 summary
        return dp

    a, b = build(110.0), build(500.0)
    assert a.pre[t0] == b.pre[t0]
    assert a.pre[t0]["S0"] == 129.0  # close of the candle opened at t0 - 60
    assert a.pre[t0]["high"] == pytest.approx(103.9) and a.pre[t0]["low"] == 99.0
    assert (a.high, b.high, a.low) == (110.0, 500.0, 98.0)
    assert a.n == 31 and not a.complete()
    assert touched("up", 105.0, a.pre[t0]["high"], a.pre[t0]["low"]) is False
    assert touched("up", 105.0, a.high, a.low) is True
    assert touched("down", 98.0, a.high, a.low) is True
    assert touched("down", 98.0, None, None) is None


def test_quote_at_ignores_points_after_cutoff_for_touch_t0():
    t0 = day_window(date(2026, 10, 14))[0] + 1800
    pts = [{"t": t0 - 600, "p": 0.11}, {"t": t0 - 60, "p": 0.12}]
    base = quote_at(pts, t0)
    assert base == (0.12, t0 - 60)
    assert quote_at([*pts, {"t": t0 - 59, "p": 0.9}, {"t": t0 + 600, "p": 0.0}], t0) == base
    assert quote_at([*pts, {"t": t0 - 30, "p": 0.9}], t0, lag=120) == (0.11, t0 - 600)


def test_touch_prob_analytic():
    # 2 (1 - N(0.03 / 0.021 = 1.4286)) = 2 * 0.07656 ~ 0.1531 for |ln K/S| = 0.03 and sigma sqrt(tau) = 0.021
    sigma = 0.021 / math.sqrt(1 / 365)
    assert touch_prob(100.0, 100.0 * math.exp(0.03), sigma, 1 / 365) == pytest.approx(0.1531, abs=2e-4)
    assert touch_prob(100.0, 100.0 * math.exp(-0.03), sigma, 1 / 365) == pytest.approx(0.1531, abs=2e-4)  # symmetric in log
    assert touch_prob(100.0, 100.0, sigma, 1 / 365) == 1.0
    assert touch_prob(100.0, 120.0, 0.0, 1 / 365) == 0.0


def test_buy_return_on_capital_with_fee_and_print():
    x = buy_return(0.10, 1, half_spread=0.01, fee_rate=0.07, fee_exp=1.0)
    fee = 0.07 * 0.11 * 0.89
    assert x["entry"] == pytest.approx(0.11) and x["fee"] == pytest.approx(fee)
    assert x["ret"] == pytest.approx(1 / (0.11 + fee) - 1)
    assert buy_return(0.10, 0, half_spread=0.01)["ret"] == pytest.approx(-1.0)
    assert buy_return(0.10, 1, half_spread=0.01, last_print=0.13)["entry"] == pytest.approx(0.13)
    assert fee_per_share(0.5, 0.25, 2.0) == pytest.approx(0.25 * 0.0625)
    assert fee_per_share(0.5, 0.0, 1.0) == 0.0


def test_pick_strangle_uses_only_q_direction_strike():
    cs = [
        {"direction": "up", "strike": 86_000, "q": 0.10, "outcome": 0},
        {"direction": "up", "strike": 85_000, "q": 0.16, "outcome": 1},  # outside [0.07, 0.14]
        {"direction": "up", "strike": 87_000, "q": 0.08, "outcome": 0},
        {"direction": "down", "strike": 81_000, "q": 0.09, "outcome": 1},
        {"direction": "down", "strike": 82_000, "q": 0.11, "outcome": 1},  # tie |q - 0.10| -> nearer strike (82k)
    ]
    legs = pick_strangle(cs)
    assert legs is not None
    assert legs["up"]["strike"] == 86_000 and legs["down"]["strike"] == 82_000
    flipped = [{**c, "outcome": 1 - c["outcome"]} for c in cs]
    legs2 = pick_strangle(flipped)
    assert {k: v["strike"] for k, v in legs2.items()} == {k: v["strike"] for k, v in legs.items()}
    assert pick_strangle([c for c in cs if c["direction"] == "up"]) is None


def test_strangle_return_one_leg_pays():
    legs = {
        "up": {"q": 0.10, "outcome": 0, "fee_rate": 0.0, "fee_exp": 1.0},
        "down": {"q": 0.10, "outcome": 1, "fee_rate": 0.0, "fee_exp": 1.0},
    }
    # $1 per leg at 0.11: one leg pays 1/0.11 = 9.09 -> (9.09 - 2) / 2
    assert strangle_return(legs) == pytest.approx((1 / 0.11 - 2) / 2)
    both = {k: {**v, "outcome": 1} for k, v in legs.items()}
    assert strangle_return(both) == pytest.approx((2 / 0.11 - 2) / 2)
    none = {k: {**v, "outcome": 0} for k, v in legs.items()}
    assert strangle_return(none) == pytest.approx(-1.0)


def test_max_losing_streak():
    assert max_losing_streak([-1, -1, 2, -1, -1, -1, 0.5]) == 3
    assert max_losing_streak([]) == 0


def test_jump_trade_offsets_sign():
    yes = "Y"
    pts = [{"t": 0, "p": 0.10}, {"t": 60, "p": 0.10}, {"t": 120, "p": 0.20}, {"t": 180, "p": 0.20}]
    # trade at 150 s pushes the price to 0.20; the point labelled 120 already shows it -> offset +30 (label = bucket start)
    trades = [{"timestamp": 10, "price": 0.10, "asset": yes}, {"timestamp": 150, "price": 0.20, "asset": yes}]
    assert jump_trade_offsets(pts, trades, yes) == [30]
    # NO-token print at 0.80 is a YES-equivalent 0.20; trade at 100 s precedes the label -> offset -20
    trades2 = [{"timestamp": 100, "price": 0.80, "asset": "N"}]
    assert jump_trade_offsets(pts, trades2, yes) == [-20]
    assert jump_trade_offsets(pts, [], yes) == []
