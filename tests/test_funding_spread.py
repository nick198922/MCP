"""Cross-venue funding spread: ticker matching, lookahead-free signal, two-leg trade P&L, statistics."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tradedesk.analytics.dated_basis import FundingSeries
from tradedesk.analytics.funding_spread import (
    DAY_MS,
    HOUR_MS,
    WEEK_MS,
    Leg,
    card_mde,
    cluster_bootstrap_ci,
    cluster_bootstrap_diff,
    entry_direction,
    match_tickers,
    normalize_asset,
    pooled_ar1,
    price_ratio,
    spread_signal,
    trade_pnl,
    trailing_turnover,
    walk_pair,
    week_of,
    weekly_spreads,
    window_covered,
)

FIX = Path(__file__).parent / "fixtures"
H4 = 4 * HOUR_MS
T0 = 1_700_006_400_000 // H4 * H4  # a 4h boundary in Nov 2023


def hourly(rate: float, start: int, end: int, step: int = HOUR_MS) -> FundingSeries:
    return FundingSeries([(t, rate) for t in range(start, end + 1, step)])


def flat_bars(price: float, start: int, end: int, bar: int = H4) -> list[dict[str, float]]:
    return [{"ts": t, "open": price, "high": price, "low": price, "close": price} for t in range(start, end, bar)]


# ----------------------------------------------------------------------------- tickers


def test_normalize_asset() -> None:
    assert normalize_asset("kPEPE", "hyperliquid") == ("PEPE", 1000)
    assert normalize_asset("BTC", "hyperliquid") == ("BTC", 1)
    assert normalize_asset("1000PEPE", "binanceusdm") == ("PEPE", 1000)
    assert normalize_asset("1000000MOG", "binanceusdm") == ("MOG", 1_000_000)
    assert normalize_asset("1MBABYDOGE", "binanceusdm") == ("BABYDOGE", 1_000_000)
    assert normalize_asset("10000LADYS", "bybit") == ("LADYS", 10000)
    assert normalize_asset("SHIB1000", "bybit") == ("SHIB", 1000)
    assert normalize_asset("1INCH", "binanceusdm") == ("1INCH", 1)


def test_match_tickers_excludes_ambiguous() -> None:
    a = {"kPEPE": ("PEPE", 1000), "BTC": ("BTC", 1), "X": ("X", 1)}
    b = {"1000PEPE": ("PEPE", 1000), "BTC": ("BTC", 1), "X": ("X", 1), "1000X": ("X", 1000)}
    pairs, amb = match_tickers(a, b)
    assert pairs == {"kPEPE": "1000PEPE", "BTC": "BTC"}
    assert amb == ["X"]


def test_price_ratio_uses_multipliers() -> None:
    hl = {d: 0.012 for d in range(30)}  # kPEPE quotes 1000 PEPE
    bn = {d: 0.012 for d in range(30)}  # 1000PEPE quotes 1000 PEPE
    assert price_ratio(hl, bn, 1000, 1000) == pytest.approx(1.0)
    spot = {d: 0.000012 for d in range(30)}  # 1 PEPE
    assert price_ratio(hl, spot, 1000, 1) == pytest.approx(1.0)
    assert price_ratio(hl, bn, 1000, 1000, min_points=40) is None


def test_hyperliquid_fixture_names_normalise() -> None:
    meta = json.loads((FIX / "hyperliquid_meta_sample_2026-10-14.json").read_text())
    names = {u["name"]: normalize_asset(u["name"], "hyperliquid") for u in meta["universe"]}
    assert names["kPEPE"] == ("PEPE", 1000)
    assert any(u.get("isDelisted") for u in meta["universe"])
    rows = json.loads((FIX / "hyperliquid_funding_history_kPEPE_2026-10-14.json").read_text())["rows"]
    fs = FundingSeries([(int(r["time"]) // 60_000 * 60_000, float(r["fundingRate"])) for r in rows])
    assert len(fs.ts) == 48
    assert all(b - a == HOUR_MS for a, b in zip(fs.ts, fs.ts[1:], strict=False))  # hourly payments (ms jitter snapped)
    assert 0.0000125 in fs.rate  # baseline 0.01 % per 8 h paid hourly
    assert all(abs(r) < 0.04 for r in fs.rate)  # HL caps funding at 4 % per hour


# ----------------------------------------------------------------------------- signal


def test_window_covered_detects_holes() -> None:
    fs = hourly(0.0001, T0, T0 + DAY_MS)
    assert window_covered(fs, T0, T0 + DAY_MS, 2 * HOUR_MS)
    holed = FundingSeries([(t, r) for t, r in zip(fs.ts, fs.rate, strict=True) if not T0 + 5 * HOUR_MS <= t <= T0 + 8 * HOUR_MS])
    assert not window_covered(holed, T0, T0 + DAY_MS, 2 * HOUR_MS)
    assert not window_covered(fs, T0 - DAY_MS, T0 + DAY_MS, 2 * HOUR_MS)  # starts late


def test_spread_signal_analytic() -> None:
    t = T0 + 10 * DAY_MS
    a = hourly(0.0001, T0, t + 5 * DAY_MS)  # 0.01 %/h
    b = hourly(0.0001, T0, t + 5 * DAY_MS, step=8 * HOUR_MS)  # 0.01 %/8h
    sig = spread_signal(a, b, t, gap_a_ms=2 * HOUR_MS, gap_b_ms=9 * HOUR_MS)
    assert sig is not None
    # (168 - 21) payments of 0.0001 over 7 days -> x 365 / 7
    assert sig["s"] == pytest.approx((168 - 21) * 0.0001 * 365 / 7, rel=1e-9)
    assert sig["s1"] == pytest.approx(sig["s"], rel=0.05) and sig["s2"] == pytest.approx(sig["s"], rel=0.05)
    assert entry_direction(sig, 0.25) == -1  # A pays more -> short A
    assert entry_direction(sig, 1.0) == 0


def test_signal_ignores_data_after_t() -> None:
    t = T0 + 10 * DAY_MS
    a = hourly(0.0002, T0, t + 5 * DAY_MS)
    b = hourly(0.0, T0, t + 5 * DAY_MS, step=8 * HOUR_MS)
    base = spread_signal(a, b, t, gap_a_ms=2 * HOUR_MS, gap_b_ms=9 * HOUR_MS)
    a2 = FundingSeries([(ts, r if ts <= t else -0.04) for ts, r in zip(a.ts, a.rate, strict=True)])
    b2 = FundingSeries([(ts, r if ts <= t else 0.03) for ts, r in zip(b.ts, b.rate, strict=True)])
    assert spread_signal(a2, b2, t, gap_a_ms=2 * HOUR_MS, gap_b_ms=9 * HOUR_MS) == base


def test_entry_needs_same_sign_halves() -> None:
    assert entry_direction({"s": 0.5, "s1": -0.1, "s2": 1.1}, 0.25) == 0
    assert entry_direction({"s": -0.5, "s1": -0.4, "s2": -0.6}, 0.25) == 1
    assert entry_direction(None, 0.25) == 0


def test_trailing_turnover_point_in_time() -> None:
    daily = [{"ts": T0 // DAY_MS * DAY_MS + i * DAY_MS, "close": 2.0, "volume": 1e6} for i in range(60)]
    t = daily[40]["ts"] + 3 * HOUR_MS  # day 40 not closed yet
    assert trailing_turnover(daily, t) == pytest.approx(30 * 2e6)
    assert trailing_turnover(daily, daily[5]["ts"]) == pytest.approx(5 * 2e6)
    changed = daily[:40] + [dict(d, volume=9e9) for d in daily[40:]]
    assert trailing_turnover(changed, t) == trailing_turnover(daily, t)


# ----------------------------------------------------------------------------- trades


def _legs(rate_a: float, rate_b: float, pa: float = 100.0, pb: float = 100.0) -> tuple[Leg, Leg]:
    s, e = T0 - 10 * DAY_MS, T0 + 30 * DAY_MS
    a = Leg(hourly(rate_a, s, e), flat_bars(pa, s, e), H4)
    b = Leg(hourly(rate_b, s, e, step=8 * HOUR_MS), flat_bars(pb, s, e), H4)
    return a, b


def test_trade_pnl_flat_prices_analytic() -> None:
    a, b = _legs(0.0002, 0.0001)
    tr = trade_pnl(a, b, T0, -1, cost=0.0059, stop_move=0.5)
    assert tr is not None
    assert tr["close_ts"] == T0 + WEEK_MS and not tr["stopped"]
    assert tr["funding_a"] == pytest.approx(168 * 0.0002)  # short A receives
    assert tr["funding_b"] == pytest.approx(-21 * 0.0001)  # long B pays
    assert tr["price"] == pytest.approx(0.0)
    assert tr["net"] == pytest.approx(168 * 0.0002 - 21 * 0.0001 - 0.0059)


def test_trade_pnl_hedged_move_and_stop() -> None:
    a, b = _legs(0.0, 0.0)
    # both legs rise 10 % after entry: hedged, price P&L 0
    for leg in (a, b):
        leg.close = [c * (1.1 if t > T0 else 1.0) for t, c in zip(leg.t, leg.close, strict=True)]
        leg.high = list(leg.close)
        leg.low = list(leg.close)
    tr = trade_pnl(a, b, T0, -1, cost=0.0, stop_move=0.5)
    assert tr is not None and tr["price"] == pytest.approx(0.0)
    # A spikes 60 % on the 3rd bar: short A is stopped there and pays the penalty
    spike = T0 + 3 * H4
    i = a.t.index(spike)
    a.high[i] = a.close[i] * 1.6 / 1.1
    tr = trade_pnl(a, b, T0, -1, cost=0.0, stop_move=0.5, stop_penalty=0.01)
    assert tr is not None and tr["stopped"] and tr["close_ts"] == spike
    assert tr["net"] == pytest.approx(-0.01)
    assert tr["max_adverse"] == pytest.approx(0.6)


def test_trade_pnl_ignores_nothing_before_entry_and_needs_prices() -> None:
    a, b = _legs(0.001, 0.0)
    assert trade_pnl(a, b, T0 + 3 * HOUR_MS, -1, cost=0.0) is None  # not a bar close
    tr = trade_pnl(a, b, T0, -1, cost=0.0)
    a.funding = FundingSeries([(t, r if t > T0 else 0.5) for t, r in zip(a.funding.ts, a.funding.rate, strict=True)])
    assert trade_pnl(a, b, T0, -1, cost=0.0) == tr  # payments at or before entry do not count


def test_walk_pair_non_overlapping_with_cooldown() -> None:
    a, b = _legs(0.0002, 0.0)
    trs = walk_pair(a, b, start_ms=T0 - 10 * DAY_MS, end_ms=T0 + 30 * DAY_MS, gap_a_ms=2 * HOUR_MS, gap_b_ms=9 * HOUR_MS, cost=0.0)
    assert len(trs) >= 2
    for x, y in zip(trs, trs[1:], strict=False):
        assert y["open_ts"] >= x["close_ts"] + DAY_MS
    assert all(t["dir"] == -1 for t in trs)
    # spread 0.0002 x 8760 = 175 % per year > 25 %; nothing changes when data after the last exit changes
    last = trs[-1]["close_ts"]
    a.funding = FundingSeries([(t, r if t <= last else -0.01) for t, r in zip(a.funding.ts, a.funding.rate, strict=True)])
    trs2 = walk_pair(a, b, start_ms=T0 - 10 * DAY_MS, end_ms=last, gap_a_ms=2 * HOUR_MS, gap_b_ms=9 * HOUR_MS, cost=0.0)
    assert [t["open_ts"] for t in trs2] == [t["open_ts"] for t in trs]


def test_walk_pair_respects_eligibility() -> None:
    a, b = _legs(0.0002, 0.0)
    trs = walk_pair(
        a, b, start_ms=T0, end_ms=T0 + 30 * DAY_MS, gap_a_ms=2 * HOUR_MS, gap_b_ms=9 * HOUR_MS, cost=0.0, eligible=lambda t: False
    )
    assert trs == []


# ----------------------------------------------------------------------------- statistics


def test_week_of_starts_monday() -> None:
    monday = 1_696_809_600_000  # 2023-10-09 00:00 UTC, Monday
    assert week_of(monday) == week_of(monday + WEEK_MS - 1) == week_of(monday - 1) + 1


def test_card_mde_and_cluster_bootstrap() -> None:
    assert card_mde(0.03, 100) == pytest.approx(2.8 * 0.003)
    assert math.isinf(card_mde(0.03, 0))
    ci = cluster_bootstrap_ci([0.01] * 10, list(range(10)))
    assert ci == pytest.approx((0.01, 0.01))
    xs = [0.02, -0.01, 0.03, 0.0, 0.01, 0.02]
    lo, hi = cluster_bootstrap_ci(xs, [1, 1, 2, 2, 3, 3])
    assert lo <= sum(xs) / len(xs) <= hi
    d = cluster_bootstrap_diff([0.02, 0.02], [1, 2], [0.01, 0.01], [1, 2])
    assert d["diff"] == pytest.approx(0.01) and d["ci"] == pytest.approx((0.01, 0.01))


def test_weekly_spreads_and_ar1() -> None:
    monday = 1_696_809_600_000
    w0 = week_of(monday)
    a = hourly(0.0001, monday - WEEK_MS, monday + 3 * WEEK_MS)
    b = hourly(0.0, monday - WEEK_MS, monday + 3 * WEEK_MS, step=8 * HOUR_MS)
    s = weekly_spreads(a, b, [w0, w0 + 1, w0 + 2, w0 + 5], gap_a_ms=2 * HOUR_MS, gap_b_ms=9 * HOUR_MS)
    assert set(s) == {w0, w0 + 1, w0 + 2}
    assert s[w0] == pytest.approx(168 * 0.0001 * 365 / 7)
    alt = [{i: (1.0 if i % 2 else -1.0) for i in range(20)}]
    r, n = pooled_ar1(alt)
    assert r == pytest.approx(-1.0) and n == 19
    trend = [{i: float(i) for i in range(10)}]
    assert pooled_ar1(trend, rank=True)[0] == pytest.approx(1.0)


def test_pooled_ar1_demean_removes_coin_level_effect() -> None:
    # two coins with constant opposite offsets and pure mean-reverting noise: pooling raw levels
    # looks persistent, demeaning per coin recovers the within-coin AR(1) of -1
    alt = {i: (1.0 if i % 2 else -1.0) for i in range(20)}
    hi = {i: 10.0 + v for i, v in alt.items()}
    lo = {i: -10.0 + v for i, v in alt.items()}
    raw, n = pooled_ar1([hi, lo])
    assert raw is not None and raw > 0.9 and n == 38
    dm, n2 = pooled_ar1([hi, lo], demean=True)
    assert dm == pytest.approx(-1.0) and n2 == 38
