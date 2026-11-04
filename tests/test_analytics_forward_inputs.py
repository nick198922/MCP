"""Direct tests of pure analytics functions that feed forward and tool numbers and had no direct test (third round of
the coverage audit, item 72 of the standing-improvements card, BACKLOG 6.9): per-strike GEX, the ATM straddle and
per-strike OI of an option chain (analytics/options.py), the funding-interval detector (analytics/derivatives.py) and
the g-shifted model probability of the v2-g Polymarket model (analytics/pm_gap_paper.py). Expected values are worked
out by hand in the comments (analytical references), with the normal CDF/PDF written out independently here."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from tradedesk.analytics.derivatives import detect_interval_hours
from tradedesk.analytics.options import OptionRow, atm_straddle, gex_by_strike, oi_by_strike
from tradedesk.analytics.pm_gap_paper import MODEL_V1, MODEL_V2G, g_shifted_prob, model_prob

H_MS = 3_600_000
EXPIRY = datetime(2026, 12, 25, 8, tzinfo=UTC)


def _n(x: float) -> float:
    """Standard normal CDF, independent of analytics.options.ncdf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi(x: float) -> float:
    """Standard normal PDF, independent of analytics.options.npdf."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _row(
    k: float,
    call: bool,
    oi: float = 0.0,
    *,
    iv: float | None = 0.5,
    F: float = 100.0,
    T: float = 0.25,
    mark: float | None = None,
) -> OptionRow:
    return OptionRow(f"BTC-25DEC26-{k:g}-{'C' if call else 'P'}", EXPIRY, float(k), call, iv, F, F, T, None, mark, float(oi), 0.0)


# ------------------------------------------------------------------------------------- options.gex_by_strike


def test_gex_by_strike_hand_values_and_sign_convention():
    # F = 100, sigma = 0.5, T = 0.25 -> s = sigma sqrt(T) = 0.25.
    # Call at K = 100 (ATM): d1 = s / 2 = 0.125; gamma = phi(0.125) / (F s) = 0.395838 / 25 = 0.0158335;
    #   GEX = +gamma x OI x F^2 x 1 % = 0.0158335 x 10 x 10000 x 0.01 = +15.8335 (dealers assumed long calls).
    # Put at K = 110: d1 = (ln(100/110) + s^2/2) / s = (-0.0953102 + 0.03125) / 0.25 = -0.256241;
    #   gamma = phi(-0.256241) / 25 = 0.0154423; GEX = -0.0154423 x 4 x 100 = -6.17692 (dealers assumed short puts).
    out = gex_by_strike([_row(110, False, 4), _row(100, True, 10)])
    assert list(out) == [100.0, 110.0]  # sorted by strike whatever the input order
    assert out[100.0] == pytest.approx(15.8335, rel=1e-4)
    assert out[110.0] == pytest.approx(-6.17692, rel=1e-4)
    # the same numbers from the textbook formula phi(d1) / (F sigma sqrt T) x OI x F^2 x 0.01
    assert out[100.0] == pytest.approx(_phi(0.125) / 25.0 * 10 * 100.0, rel=1e-12)
    d1 = (math.log(100 / 110) + 0.03125) / 0.25
    assert out[110.0] == pytest.approx(-_phi(d1) / 25.0 * 4 * 100.0, rel=1e-12)


def test_gex_by_strike_call_and_put_same_strike_and_oi_cancel():
    # Black-76 gamma is the same for a call and a put of one strike, so +call - put at equal OI nets to zero.
    out = gex_by_strike([_row(95, True, 7), _row(95, False, 7)])
    assert out[95.0] == pytest.approx(0.0, abs=1e-12)


def test_gex_by_strike_sums_rows_of_one_strike():
    # two call rows at K = 100 with OI 4 and 6 equal one row with OI 10 (gamma is linear in OI)
    two = gex_by_strike([_row(100, True, 4), _row(100, True, 6)])
    one = gex_by_strike([_row(100, True, 10)])
    assert two[100.0] == pytest.approx(one[100.0], rel=1e-12)


def test_gex_by_strike_skips_rows_without_iv_time_or_oi():
    rows = [
        _row(90, True, 5, iv=None),  # no IV
        _row(95, True, 5, iv=0.0),  # zero IV
        _row(100, True, 5, T=0.0),  # expired
        _row(105, True, 0),  # no open interest
        _row(110, False, -3),  # negative OI (bad data)
    ]
    assert gex_by_strike(rows) == {}
    assert gex_by_strike([]) == {}


# ------------------------------------------------------------------------------------- options.atm_straddle


def _chain() -> list[OptionRow]:
    return [
        _row(90, True, mark=12.0, iv=0.62),
        _row(90, False, mark=2.5, iv=0.64),
        _row(110, True, mark=3.0, iv=0.50),
        _row(110, False, mark=12.0, iv=0.60),
    ]


def test_atm_straddle_hand_values():
    st = atm_straddle(_chain())
    # F = 100 lies halfway between 90 and 110, but the ATM pick is by log distance:
    # |ln(110/100)| = 0.09531 < |ln(90/100)| = 0.10536 -> K = 110.
    assert st["atm_strike"] == 110.0
    assert st["forward"] == 100.0 and st["T"] == 0.25
    # straddle = call + put marks = 3 + 12 = 15 USD; 15 % of F; breakevens F -/+ 15 = 85 / 115
    assert st["call_mark_usd"] == 3.0 and st["put_mark_usd"] == 12.0
    assert st["straddle_usd"] == pytest.approx(15.0)
    assert st["straddle_pct_of_forward"] == pytest.approx(0.15)
    assert (st["breakeven_lo"], st["breakeven_hi"]) == pytest.approx((85.0, 115.0))
    # ATM IV = (0.50 + 0.60) / 2 = 0.55; 1 sigma move = 0.55 x sqrt(0.25) = 0.275;
    # range = 100 e^(-/+0.275) = 75.9572 / 131.6531
    assert st["atm_iv"] == pytest.approx(0.55)
    assert st["sigma_move_pct"] == pytest.approx(0.275)
    assert st["sigma_range"] == pytest.approx((75.9572, 131.6531), rel=1e-6)


def test_atm_straddle_ignores_expired_rows():
    # an expired pair exactly at the forward must not be picked: rows with T <= 0 are dropped first
    rows = [_row(100, True, mark=1.0, T=0.0), _row(100, False, mark=1.0, T=0.0), *_chain()]
    assert atm_straddle(rows)["atm_strike"] == 110.0


def test_atm_straddle_one_sided_iv_uses_the_available_leg():
    rows = [_row(100, True, mark=5.0, iv=None), _row(100, False, mark=5.0, iv=0.4)]
    st = atm_straddle(rows)
    assert st["atm_iv"] == pytest.approx(0.4)
    assert st["sigma_move_pct"] == pytest.approx(0.2)  # 0.4 x sqrt(0.25)


def test_atm_straddle_without_any_iv_keeps_prices_and_drops_sigma():
    st = atm_straddle([_row(100, True, mark=5.0, iv=None), _row(100, False, mark=4.0, iv=None)])
    assert st["straddle_usd"] == pytest.approx(9.0)
    assert st["atm_iv"] is None and st["sigma_move_pct"] is None and st["sigma_range"] is None


@pytest.mark.parametrize(
    "rows",
    [
        [],  # empty
        [_row(100, True, mark=5.0, T=0.0), _row(100, False, mark=5.0, T=0.0)],  # all expired
        [_row(100, True, mark=5.0)],  # no put at the ATM strike
        [_row(100, True, mark=5.0), _row(100, False, mark=None)],  # put mark missing
        [_row(100, True, mark=None), _row(100, False, mark=4.0)],  # call mark missing
    ],
)
def test_atm_straddle_none_when_a_leg_or_mark_is_missing(rows):
    assert atm_straddle(rows) is None


# ------------------------------------------------------------------------------------- options.oi_by_strike


def test_oi_by_strike_aggregates_calls_puts_and_ratios():
    rows = [
        _row(100, True, 10),
        _row(100, False, 15),
        _row(100, True, 5),  # e.g. another expiry, same strike: summed
        _row(90, False, 8),  # puts only -> p/c ratio undefined
        _row(110, True, 0),  # zero OI: strike absent
        _row(120, False, -1),  # negative OI: skipped
    ]
    out = oi_by_strike(rows)
    assert [r["strike"] for r in out] == [90.0, 100.0]
    k90, k100 = out
    # K = 90: call 0, put 8, total 8, notional 8 x F(100) = 800, p/c None, net 0 - 8 = -8
    assert k90 == {"strike": 90.0, "call_oi": 0.0, "put_oi": 8.0, "total_oi": 8.0, "notional_usd": 800.0, "pc_ratio": None, "net_oi": -8.0}
    # K = 100: call 10 + 5 = 15, put 15, total 30, notional 3000, p/c 15 / 15 = 1, net 0
    assert k100 == {
        "strike": 100.0,
        "call_oi": 15.0,
        "put_oi": 15.0,
        "total_oi": 30.0,
        "notional_usd": 3000.0,
        "pc_ratio": 1.0,
        "net_oi": 0.0,
    }


def test_oi_by_strike_notional_uses_each_rows_own_forward():
    # item 73 / BACKLOG 6.12: rows of two expiries at one strike, forwards 100 and 110.
    # notional = 10 x 100 + 5 x 110 + 15 x 100 = 1000 + 550 + 1500 = 3050 (not 30 x first forward 100 = 3000),
    # and the result does not depend on which expiry comes first.
    rows = [_row(100, True, 10, F=100.0), _row(100, True, 5, F=110.0), _row(100, False, 15, F=100.0)]
    for order in (rows, rows[::-1]):
        (k100,) = oi_by_strike(order)
        assert k100["total_oi"] == 30.0
        assert k100["notional_usd"] == pytest.approx(3050.0)


def test_oi_by_strike_empty_and_all_zero():
    assert oi_by_strike([]) == []
    assert oi_by_strike([_row(100, True, 0), _row(100, False, 0)]) == []


# ------------------------------------------------------------------------------------- derivatives.detect_interval_hours


def _ts(*hours: float) -> list[dict]:
    return [{"ts_ms": int(h * H_MS), "rate": 0.0001} for h in hours]


@pytest.mark.parametrize(("step_h", "expected"), [(8, 8.0), (4, 4.0), (1, 1.0)])
def test_detect_interval_hours_regular_cadence(step_h, expected):
    assert detect_interval_hours(_ts(*(i * step_h for i in range(6)))) == pytest.approx(expected)


def test_detect_interval_hours_median_resists_one_gap_and_ignores_order_and_duplicates():
    # sorted stamps 0, 8, 16, 16, 24, 25 h -> positive spacings 8, 8, 8, 1 -> median 8;
    # shuffled input and the duplicate stamp (zero spacing, dropped) do not matter
    entries = _ts(25, 0, 16, 8, 16, 24)
    assert detect_interval_hours(entries) == pytest.approx(8.0)


def test_detect_interval_hours_skips_missing_stamps_and_casts_strings():
    entries = [{"ts_ms": None}, {"rate": 0.1}, {"ts_ms": str(0)}, {"ts_ms": str(4 * H_MS)}, {"ts_ms": 8 * H_MS}]
    assert detect_interval_hours(entries) == pytest.approx(4.0)


def test_detect_interval_hours_returns_raw_median_not_rounded():
    # item 73 / BACKLOG 6.12: the docstring now says the median is returned as is. Rounding to {1, 2, 4, 8} h was
    # rejected because carry_snapshot / summarise_funding_history feed forward numbers through this value.
    assert detect_interval_hours(_ts(0, 7.5, 15, 22.5)) == pytest.approx(7.5)
    assert detect_interval_hours(_ts(0, 3, 6, 9)) == pytest.approx(3.0)


def test_detect_interval_hours_fallbacks():
    assert detect_interval_hours([]) == 8.0
    assert detect_interval_hours(_ts(0)) == 8.0  # one point: no spacing
    assert detect_interval_hours(_ts(0, 0, 0)) == 8.0  # only duplicate stamps
    assert detect_interval_hours(_ts(0), fallback=1.0) == 1.0
    # below 15 minutes the spacing is treated as noise: 10 min -> fallback
    assert detect_interval_hours(_ts(0, 1 / 6, 2 / 6, 3 / 6), fallback=4.0) == 4.0
    # exactly 15 minutes is accepted (boundary inclusive)
    assert detect_interval_hours(_ts(0, 0.25, 0.5, 0.75)) == pytest.approx(0.25)


# ------------------------------------------------------------------------------------- pm_gap_paper.g_shifted_prob


def test_g_shifted_prob_zero_gap_is_the_input_probability():
    # g = 0 returns p_model as is, even without the inputs a shift would need
    assert g_shifted_prob("above", None, 0.37, 0.5, 100.0, 105.0, 0.02, 0.0) == 0.37
    assert g_shifted_prob("touch", "up", 0.21, None, None, 105.0, 0.0, 0.0) == 0.21


def test_g_shifted_prob_zero_gap_v2g_equals_v1():
    # a snapshot where Binance equals the Deribit index (g = 0): the v2-g model probability is the registered v1 one
    row = {
        "model_prob": 0.37,
        "spot": 100.0,
        "binance_last": 100.0,
        "ts": 0,
        "end_ts": 7 * 86_400,
        "kind": "above",
        "strike": 105.0,
        "model_iv": 0.5,
        "direction": None,
    }
    assert model_prob(row, model=MODEL_V2G) == model_prob(row, model=MODEL_V1) == 0.37


def test_g_shifted_prob_above_small_gap_matches_first_order():
    # P(above) = N(d2), d2 = (ln(F/K) - s^2/2) / s; dP/dF = phi(d2) / (F s), so F -> F(1 + g) adds
    # ~ phi(d2) g / (sigma sqrt tau). F = 100000, K = 102000, sigma = 0.6, tau = 7/365:
    # s = 0.0830910, d2 = -0.279870, phi(d2) = 0.383613 -> delta ~ 0.383613 x 1e-4 / 0.0830910 = 4.6169e-4.
    F, K, sig, tau, g, p = 100_000.0, 102_000.0, 0.6, 7 / 365, 1e-4, 0.30
    s = sig * math.sqrt(tau)
    d2 = (math.log(F / K) - 0.5 * s * s) / s
    linear = _phi(d2) * g / s
    assert linear == pytest.approx(4.6169e-4, rel=1e-4)
    out = g_shifted_prob("above", None, p, sig, F, K, tau, g)
    # the smile-slope part of p_model is kept: the result is p + delta, not N(d2) at the new forward
    assert out - p == pytest.approx(linear, rel=1e-3)
    # and the exact flat-IV difference N(d2') - N(d2) at a larger gap
    g2 = 0.02
    d2_new = (math.log(F * (1 + g2) / K) - 0.5 * s * s) / s
    assert g_shifted_prob("above", None, p, sig, F, K, tau, g2) == pytest.approx(p + _n(d2_new) - _n(d2), rel=1e-12)
    # sign: a lower forward lowers P(above)
    assert g_shifted_prob("above", None, p, sig, F, K, tau, -g) < p


def test_g_shifted_prob_touch_shift_onto_the_barrier():
    # Driftless-forward touch probability (log-drift -sigma^2/2), s = sigma sqrt(tau), F = 100, sigma = 0.8, tau = 30/365:
    #   up to B > F, b = ln(B/F):   P = N(-b/s - s/2) + (F/B) N(-b/s + s/2)
    #   down to B < F, a = ln(F/B): P = N(-a/s + s/2) + (F/B) N(-a/s - s/2)
    # Shifting the forward onto the barrier makes the touch certain (P = 1), so the result is p_model + 1 - P(F).
    F, sig, tau = 100.0, 0.8, 30 / 365
    s = sig * math.sqrt(tau)
    b = math.log(110 / F)
    p_up = _n(-b / s - s / 2) + F / 110 * _n(-b / s + s / 2)  # = 0.645020
    a = math.log(F / 90)
    p_down = _n(-a / s + s / 2) + F / 90 * _n(-a / s - s / 2)  # = 0.679563
    assert p_up == pytest.approx(0.645020, rel=1e-5) and p_down == pytest.approx(0.679563, rel=1e-5)
    assert g_shifted_prob("touch", "up", 0.10, sig, F, 110.0, tau, 0.10) == pytest.approx(0.10 + 1 - p_up, rel=1e-9)
    assert g_shifted_prob("touch", "down", 0.10, sig, F, 90.0, tau, -0.10) == pytest.approx(0.10 + 1 - p_down, rel=1e-9)


def test_g_shifted_prob_clips_to_unit_interval():
    # p 0.95 + a large upward shift of an ATM 'above' (N(d2) moves by ~0.4) -> clipped to 1; mirror case -> 0
    assert g_shifted_prob("above", None, 0.95, 0.5, 100.0, 100.0, 0.02, 0.5) == 1.0
    assert g_shifted_prob("above", None, 0.05, 0.5, 100.0, 100.0, 0.02, -0.5) == 0.0


@pytest.mark.parametrize(
    ("kind", "direction", "iv", "forward", "strike", "tau", "g"),
    [
        ("above", None, None, 100.0, 105.0, 0.02, 0.01),  # no IV
        ("above", None, 0.0, 100.0, 105.0, 0.02, 0.01),  # zero IV
        ("above", None, 0.5, None, 105.0, 0.02, 0.01),  # no forward
        ("above", None, 0.5, 0.0, 105.0, 0.02, 0.01),  # zero forward
        ("above", None, 0.5, 100.0, 0.0, 0.02, 0.01),  # zero strike
        ("above", None, 0.5, 100.0, 105.0, 0.0, 0.01),  # no time left
        ("touch", None, 0.5, 100.0, 105.0, 0.02, 0.01),  # touch without a direction
        ("touch", "sideways", 0.5, 100.0, 105.0, 0.02, 0.01),  # unknown direction
        ("range", None, 0.5, 100.0, 105.0, 0.02, 0.01),  # unknown kind
        ("above", None, 0.5, 100.0, 105.0, 0.02, -1.5),  # shifted forward negative
        ("touch", "up", 0.5, 100.0, 105.0, 0.02, -1.5),  # same for touch
    ],
)
def test_g_shifted_prob_none_for_inputs_it_cannot_take(kind, direction, iv, forward, strike, tau, g):
    assert g_shifted_prob(kind, direction, 0.4, iv, forward, strike, tau, g) is None
