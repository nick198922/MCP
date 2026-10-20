"""Tests for analytics.funding_settlement (IDEA-20261020-funding-settlement-window)."""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.funding_settlement import (
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    band,
    cap_from_history,
    final_prediction,
    interval_start,
    predicted_funding,
    receiver_side,
    round_trip_cost,
    scale_mode,
    settlement_trade,
    slippage,
    trailing_median,
    window_return,
)

T = 1_750_000_000_000 // HOUR_MS * HOUR_MS  # a whole hour


def flat_premium(start: int, end: int, value: float) -> dict[int, float]:
    return {t: value for t in range(start, end + 1, MIN_MS)}


def test_interval_start_uses_previous_settlement_or_hours() -> None:
    assert interval_start(T, 4, T - 4 * HOUR_MS) == T - 4 * HOUR_MS
    assert interval_start(T, 4, T - HOUR_MS) == T - HOUR_MS  # interval changed: previous settlement wins
    assert interval_start(T, 4, T - 10 * HOUR_MS) == T - 4 * HOUR_MS  # too far back (gap)
    assert interval_start(T, 8, None) == T - 8 * HOUR_MS


def test_predicted_funding_flat_premium_and_clamp() -> None:
    start = T - 4 * HOUR_MS
    entry = T - 3 * MIN_MS
    # P = -1 %: I - P = 1.005 % clamped to +0.05 %  ->  -0.95 %
    f = predicted_funding(flat_premium(start, entry, -0.01), start, entry, 4)
    assert f == pytest.approx(-0.0095)
    # P = 0: I = 0.01 % * 4/8 = 0.005 %, inside the clamp -> F = I
    f0 = predicted_funding(flat_premium(start, entry, 0.0), start, entry, 4)
    assert f0 == pytest.approx(0.00005)
    # cap clips
    assert predicted_funding(flat_premium(start, entry, -0.01), start, entry, 4, cap=0.005) == pytest.approx(-0.005)


def test_predicted_funding_weights_later_minutes_more() -> None:
    start = T - HOUR_MS
    entry = start + MIN_MS  # two bars: weights 1 and 2
    prem = {start: 0.0, start + MIN_MS: -0.03}
    # weighted average (1*0 + 2*-0.03)/3 = -0.02; clamp +0.05 % -> -0.0195
    assert predicted_funding(prem, start, entry, 1) == pytest.approx(-0.0195)
    assert predicted_funding(prem, start, entry, 1, weighted=False) == pytest.approx(-0.015 + 0.0005)


def test_predicted_funding_coverage() -> None:
    start = T - HOUR_MS
    entry = T - 3 * MIN_MS
    prem = {start: -0.01}
    assert predicted_funding(prem, start, entry, 1) is None
    assert predicted_funding({}, start, entry, 1) is None


def test_predicted_funding_ignores_bars_at_or_after_entry_close() -> None:
    """No lookahead: changing any premium bar that opens after the entry bar leaves the signal unchanged."""
    start = T - 4 * HOUR_MS
    entry = T - 3 * MIN_MS
    prem = flat_premium(start, T + 10 * MIN_MS, -0.004)
    base = predicted_funding(prem, start, entry, 4)
    for t in range(entry + MIN_MS, T + 11 * MIN_MS, MIN_MS):
        prem2 = dict(prem)
        prem2[t] = 5.0
        assert predicted_funding(prem2, start, entry, 4) == base
    prem3 = dict(prem)
    prem3[entry] = 5.0  # the entry bar itself is included (its close is known at entry)
    assert predicted_funding(prem3, start, entry, 4) != base


def test_cap_from_history() -> None:
    hist = [(T - k * HOUR_MS, -0.02) for k in (1, 2)] + [(T - 3 * HOUR_MS, -0.01)]
    assert cap_from_history(hist, T) == pytest.approx(0.02)
    assert cap_from_history([(T - HOUR_MS, -0.02), (T - 2 * HOUR_MS, -0.01)], T) is None  # one hit only
    # settlements at or after T are not read
    assert cap_from_history([(T, -0.03), (T + HOUR_MS, -0.03), (T - HOUR_MS, -0.01)], T) is None
    # older than the window are not read
    assert cap_from_history([(T - 40 * DAY_MS, -0.02), (T - 41 * DAY_MS, -0.02)], T) is None


def bars_from_closes(closes: dict[int, float]) -> dict[int, tuple[float, float, float, float, float]]:
    return {t: (c, c * 1.001, c * 0.999, c, 1e5) for t, c in closes.items()}


def test_settlement_trade_long_receives_negative_funding() -> None:
    closes = {T + k * MIN_MS: 100.0 for k in range(-5, 5)}
    closes[T + MIN_MS] = 99.0  # exit bar close
    bars = bars_from_closes(closes)
    tr = settlement_trade(bars, T, receiver_side(-0.005), -0.005)
    assert tr is not None
    assert tr.side == 1
    assert tr.price_ret == pytest.approx(-0.01)
    assert tr.funding == pytest.approx(0.005)
    assert tr.gross == pytest.approx(-0.005)
    # MAE from bars [T-2, T+1]: lows 99.9 ... 98.901 -> 98.901/100 - 1
    assert tr.mae == pytest.approx(99.0 * 0.999 / 100 - 1)


def test_settlement_trade_short_and_worse_prices() -> None:
    closes = {T + k * MIN_MS: 100.0 for k in range(-5, 5)}
    closes[T - 2 * MIN_MS] = 99.0  # next bar after entry: worse for a short (lower entry)
    closes[T + 2 * MIN_MS] = 101.0  # next bar after exit: worse for a short (higher exit)
    bars = bars_from_closes(closes)
    tr = settlement_trade(bars, T, -1, 0.004)
    assert tr is not None and tr.price_ret == pytest.approx(0.0) and tr.funding == pytest.approx(0.004)
    w = settlement_trade(bars, T, -1, 0.004, worse=True)
    assert w is not None
    assert w.p_in == 99.0 and w.p_out == 101.0
    assert w.price_ret == pytest.approx(-(101 / 99 - 1))


def test_settlement_trade_missing_bar_or_bad_side() -> None:
    bars = bars_from_closes({T - 3 * MIN_MS: 1.0})
    assert settlement_trade(bars, T, 1, -0.01) is None
    assert settlement_trade(bars_from_closes({T - 3 * MIN_MS: 1.0, T + MIN_MS: 1.0}), T, 0, 0.0) is None


def test_settlement_trade_ignores_bars_after_exit() -> None:
    closes = {T + k * MIN_MS: 100.0 for k in range(-5, 10)}
    bars = bars_from_closes(closes)
    base = settlement_trade(bars, T, 1, -0.01)
    bars2 = dict(bars)
    bars2[T + 2 * MIN_MS] = (1.0, 1.0, 1.0, 1.0, 1.0)  # after the exit bar
    assert settlement_trade(bars2, T, 1, -0.01) == base


def test_window_return_and_costs() -> None:
    bars = bars_from_closes({T: 100.0, T + MIN_MS: 102.0})
    assert window_return(bars, -1, T, T + MIN_MS) == pytest.approx(-0.02)
    assert window_return(bars, 1, T, T + 2 * MIN_MS) is None
    assert slippage(60e6) == 0.0005 and slippage(6e6) == 0.001 and slippage(None) == 0.002
    assert round_trip_cost(1e6) == pytest.approx(2 * (0.0005 + 0.002))


def test_trailing_median_reads_only_past_days() -> None:
    qv = {d: float(d) for d in range(0, 20)}
    m, n = trailing_median(qv, 10)
    assert (m, n) == (6.0, 7)
    qv2 = dict(qv)
    qv2[10] = 1e12
    qv2[15] = -1.0
    assert trailing_median(qv2, 10) == (m, n)


def test_scale_mode_and_final_prediction() -> None:
    scaled = [(4.0, -0.01, -0.005), (1.0, -0.008, -0.001), (4.0, 0.004, 0.002)]
    unscaled = [(4.0, -0.01, -0.0098), (1.0, -0.008, -0.0081), (4.0, 0.004, 0.0041)]
    assert scale_mode(scaled) == "scaled"
    assert scale_mode(unscaled) == "unscaled"
    assert scale_mode(scaled[:2]) is None  # too few pairs
    assert scale_mode([(8.0, -0.01, -0.01)] * 5) is None  # 8h settlements carry no information
    assert scale_mode([(4.0, -0.01, 0.005)] * 5) is None  # opposite signs are skipped
    assert final_prediction(-0.016, 1.0, "scaled", None) == pytest.approx(-0.002)
    assert final_prediction(-0.016, 1.0, "unscaled", 0.005) == pytest.approx(-0.005)
    assert final_prediction(-0.016, 8.0, "scaled", None) == pytest.approx(-0.016)


def test_band() -> None:
    assert band(-0.0035) == "-[0.30,1.00)"
    assert band(0.0012) == "+[0.10,0.20)"
    assert band(-0.02) == "->=1.00"
    assert band(0.0005) is None
    assert math.isclose(0.003, 0.003)
