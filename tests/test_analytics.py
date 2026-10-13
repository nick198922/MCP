import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.analytics import derivatives, odds, orderbook, probability
from tradedesk.analytics.instruments import (
    expiry_token,
    parse_future_name,
    parse_option_name,
    parse_user_expiry,
)
from tradedesk.analytics.options import (
    OptionRow,
    Smile,
    atm_iv,
    black76_greeks,
    black76_price,
    build_chain,
    build_smile,
    gex_flip_shift,
    gex_profile,
    group_by_expiry,
    max_pain,
    ncdf,
    net_gex,
    put_call_ratios,
    rr_fly,
    year_fraction,
)

FIX = json.loads((Path(__file__).parent / "fixtures" / "deribit_options_sample.json").read_text())
UTC = UTC


# ---------- instruments ----------
def test_parse_option_expiry_matches_deribit_timestamp():
    p = parse_option_name("BTC-11OCT26-73000-C")
    assert p and p.is_call and p.strike == 73000
    assert int(p.expiry.timestamp() * 1000) == FIX["instrument_BTC-11OCT26-73000-C"]["expiration_timestamp"]


def test_parse_variants():
    assert parse_option_name("BTC-5NOV26-90000-P").expiry.day == 5
    assert parse_option_name("XRP_USDC-27MAR26-0d5-C").strike == 0.5
    assert parse_option_name("BTC-PERPETUAL") is None
    assert parse_future_name("BTC-PERPETUAL") == ("BTC", None)
    assert parse_future_name("BTC-27NOV26")[1].month == 11
    assert expiry_token(datetime(2027, 3, 26, 8, tzinfo=UTC)) == "26MAR27"
    assert parse_user_expiry("26mar27") == parse_user_expiry("2027-03-26")


# ---------- Black-76 vs Deribit's own numbers (real data) ----------
def test_black76_reproduces_deribit_ticker_greeks():
    t = FIX["ticker_BTC-26MAR27-85000-P"]
    now = datetime.fromtimestamp(t["timestamp"] / 1000, UTC)
    expiry = parse_option_name(t["instrument_name"]).expiry
    T = year_fraction(expiry, now)
    g = black76_greeks(t["underlying_price"], 85000, T, t["mark_iv"] / 100, is_call=False)
    d = t["greeks"]
    assert g["delta"] == pytest.approx(d["delta"], abs=0.002)
    assert g["gamma"] == pytest.approx(d["gamma"], abs=4e-6)
    assert g["vega"] == pytest.approx(d["vega"], rel=0.01)
    assert g["theta"] == pytest.approx(d["theta"], rel=0.02)


def test_black76_price_matches_deribit_mark_in_underlying_units():
    t = FIX["ticker_BTC-26MAR27-85000-P"]
    now = datetime.fromtimestamp(t["timestamp"] / 1000, UTC)
    T = year_fraction(parse_option_name(t["instrument_name"]).expiry, now)
    F = t["underlying_price"]
    usd = black76_price(F, 85000, T, t["mark_iv"] / 100, is_call=False)
    assert usd / F == pytest.approx(t["mark_price"], abs=3e-4)


def test_put_call_parity_and_limits():
    F, K, T, s = 100.0, 95.0, 0.5, 0.4
    c, p = black76_price(F, K, T, s, True), black76_price(F, K, T, s, False)
    assert c - p == pytest.approx(F - K, abs=1e-9)
    assert black76_price(100, 90, 0, 0.4, True) == 10
    assert black76_greeks(100, 90, 0, 0.4, True)["gamma"] == 0


def test_build_chain_on_real_rows():
    now = datetime.fromtimestamp(1791637668, UTC)
    rows = build_chain(FIX["result"], now)
    assert len(rows) == 5
    r = next(x for x in rows if x.instrument == "BTC-26MAR27-85000-P")
    assert r.iv == pytest.approx(0.3819)
    assert r.delta == pytest.approx(-0.4535, abs=0.002)
    assert r.mark_price_usd == pytest.approx(0.10446553 * 82743.71)
    assert not r.is_call
    assert len(group_by_expiry(rows)) == 4


# ---------- smile on a synthetic chain with a known smile ----------
def synth_chain(F=100_000.0, days=30, base_iv=0.40, skew=-0.25, curv=1.2):
    now = datetime(2026, 10, 10, 8, tzinfo=UTC)
    expiry = datetime(2026, 11, 9, 8, tzinfo=UTC)
    rows = []
    for k in range(60_000, 140_001, 5_000):
        x = math.log(k / F)
        iv = base_iv + skew * x + curv * x * x
        for is_call in (True, False):
            rows.append(
                OptionRow(
                    instrument=f"BTC-9NOV26-{k}-{'C' if is_call else 'P'}",
                    expiry=expiry,
                    strike=float(k),
                    is_call=is_call,
                    iv=iv,
                    forward=F,
                    index=F,
                    T=year_fraction(expiry, now),
                    mark_price_native=None,
                    mark_price_usd=None,
                    open_interest=10.0,
                    volume=1.0,
                )
            )
    return rows, iv_fn(F, base_iv, skew, curv)


def iv_fn(F, base, skew, curv):
    return lambda k: base + skew * math.log(k / F) + curv * math.log(k / F) ** 2


def test_smile_atm_and_skew_signs():
    rows, f = synth_chain()
    sm = build_smile(rows)
    assert sm is not None
    assert atm_iv(sm) == pytest.approx(0.40, abs=0.003)
    out = rr_fly(sm)
    assert out["rr"] < 0  # negative skew: 25d puts richer than calls
    assert out["fly"] > 0  # positive curvature
    assert out["iv_put"] > out["iv_call"]


def test_smile_needs_three_strikes_and_no_extrapolation():
    rows, _ = synth_chain()
    assert build_smile([r for r in rows if r.strike in (95_000.0, 100_000.0)]) is None
    sm = build_smile(rows)
    assert sm.iv_at_call_delta(0.0001) is None  # outside quoted delta range -> None, not a guess


# ---------- max pain, ratios, GEX ----------
def _row(k, call, oi, F=100.0, iv=0.5, T=0.1):
    e = datetime(2026, 12, 1, 8, tzinfo=UTC)
    return OptionRow("x", e, float(k), call, iv, F, F, T, None, None, float(oi), 0.0)


def test_max_pain_simple_case():
    rows = [_row(90, True, 5), _row(100, True, 10), _row(100, False, 10), _row(110, False, 5)]
    mp = max_pain(rows)
    assert mp["max_pain"] == 100
    assert mp["payout_usd"] == pytest.approx(5 * 10 + 5 * 10)  # call90 ITM by 10, put110 ITM by 10
    assert max_pain([]) is None


def test_put_call_ratio():
    rows = [_row(100, True, 10), _row(100, False, 15)]
    assert put_call_ratios(rows)["pc_oi"] == pytest.approx(1.5)


def test_gex_signs_and_flip():
    assert net_gex([_row(100, True, 10)]) > 0
    assert net_gex([_row(100, False, 10)]) < 0
    rows = [_row(110, True, 100), _row(90, False, 100)]
    prof = gex_profile(rows)
    flip = gex_flip_shift(prof)
    assert flip is not None and 0.95 < flip < 1.05
    assert net_gex(rows, 1.1) > 0 > net_gex(rows, 0.9)


# ---------- probabilities ----------
def test_prob_above_flat_smile_equals_nd2():
    F, T = 100.0, 0.25
    sm = Smile(F, T, [80.0, 90.0, 100.0, 110.0, 120.0], [0.4] * 5)
    from tradedesk.analytics.options import d1d2

    assert probability.prob_above(sm, 105.0) == pytest.approx(ncdf(d1d2(F, 105.0, T, 0.4)[1]), abs=1e-6)


def test_prob_above_skew_correction_raises_probability():
    F, T = 100.0, 0.25
    flat = Smile(F, T, [80.0, 90.0, 100.0, 110.0, 120.0], [0.4] * 5)
    skewed = Smile(F, T, [80.0, 90.0, 100.0, 110.0, 120.0], [0.5, 0.45, 0.4, 0.37, 0.35])
    assert probability.prob_above(skewed, 100.0) > probability.prob_above(flat, 100.0)


@pytest.mark.parametrize("barrier", [115.0, 130.0, 88.0, 70.0])
def test_touch_probability_matches_monte_carlo(barrier):
    F, s, T = 100.0, 0.6, 0.25
    a = probability.prob_touch(F, barrier, s, T)
    m = probability.mc_touch(F, barrier, s, T)
    assert a == pytest.approx(m, abs=0.015)


def test_touch_bounds():
    assert probability.prob_touch(100, 100, 0.5, 0.1) == 1.0
    p_touch = probability.prob_touch(100, 120, 0.5, 0.25)
    from tradedesk.analytics.options import d1d2

    p_above_at_expiry = ncdf(d1d2(100, 120, 0.25, 0.5)[1])
    assert p_touch > p_above_at_expiry


# ---------- odds ----------
def test_odds_margin_and_devig():
    assert odds.overround([1.9, 1.9]) == pytest.approx(2 / 1.9 - 1)
    for method in ("proportional", "power"):
        out = odds.devig([1.8, 2.0, 5.0], method)
        assert sum(out["fair_probs"]) == pytest.approx(1.0, abs=1e-9)
        assert all(f > 1 for f in out["fair_odds"])
    prop = odds.devig_proportional([1.8, 2.0, 5.0])
    pw = odds.devig_power([1.8, 2.0, 5.0])
    assert pw[2] < prop[2]  # power method shrinks the long shot harder
    with pytest.raises(ValueError):
        odds.decimal_to_prob(1.0)


# ---------- derivatives ----------
def test_basis_and_funding():
    now = datetime(2026, 10, 10, 8, tzinfo=UTC)
    b = derivatives.basis(83355.29, 82757.52, datetime(2026, 11, 27, 8, tzinfo=UTC), now)
    assert b["days_to_expiry"] == 48
    assert b["basis_pct"] == pytest.approx(0.007223, rel=1e-3)
    assert b["annualized_simple"] == pytest.approx(b["basis_pct"] * 365 / 48)
    assert derivatives.basis(100, 99, now, now)["annualized_simple"] is None
    assert derivatives.parse_interval_hours("8h") == 8
    assert derivatives.parse_interval_hours("30m") == 0.5
    assert derivatives.parse_interval_hours(None) == 8
    assert derivatives.annualize_funding(0.0001, 8) == pytest.approx(0.1095)
    assert derivatives.annualize_funding(0.00001, 1) == pytest.approx(0.0876)


# ---------- order book ----------
def test_book_stats_and_walk():
    bids = [[99.0, 10], [98.0, 20]]
    asks = [[101.0, 5], [102.0, 50]]
    st = orderbook.book_stats(bids, asks)
    assert st["mid"] == 100 and st["spread_bps"] == pytest.approx(200)
    w = orderbook.walk_book(asks, 505 + 1020, mid=100, side="buy")  # 5@101 then 10@102
    assert w["unfilled_usd"] == pytest.approx(0)
    assert w["avg_price"] == pytest.approx((505 + 1020) / (5 + 10))
    big = orderbook.walk_book(asks, 1e6, mid=100, side="buy")
    assert big["unfilled_usd"] > 0


def test_book_tolerates_extra_level_columns():
    # OKX returns [price, size, 0, order_count] — tolerate anything with price,size as first two.
    bids = [[99.0, 10, 0, 3], [98.0, 20, 0, 7]]
    asks = [[101.0, 5, 0, 2], [102.0, 50, 0, 11]]
    st = orderbook.book_stats(bids, asks)
    assert st["mid"] == 100
    w = orderbook.walk_book(asks, 505, mid=100, side="buy")
    assert w["unfilled_usd"] == pytest.approx(0)


def test_funding_carry_is_robust_to_interval_change():
    from tradedesk.analytics.derivatives import summarise_funding_history

    h = 3_600_000
    r = 0.0001  # 0.01 % per 8h = 0.03 % a day
    eight = [{"ts_ms": i * 8 * h, "rate": r} for i in range(90)]  # 30 days at 8h
    t0 = eight[-1]["ts_ms"]
    hourly = [{"ts_ms": t0 + (i + 1) * h, "rate": r / 8} for i in range(7 * 24)]  # then 7 days at 1h, same daily income
    s = summarise_funding_history(eight + hourly)
    assert s["carry_annualized"] == pytest.approx(r * 3 * 365, rel=0.01)
    assert s["latest_annualized"] == pytest.approx(r / 8 * 8760, rel=1e-9)  # at the latest 1h interval
    plain = summarise_funding_history(eight)
    assert plain["carry_annualized"] == pytest.approx(r * 3 * 365, rel=1e-9)
