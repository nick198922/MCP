"""Direct tests for ten analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 127).

They feed carry A (delisting accounting, Bybit inventory), the demo execution report, the stop study and the assistant's
paper journal. References are hand-computed on synthetic data; edge cases follow each function.
"""

from __future__ import annotations

import math

import pytest

from tradedesk.analytics.carry import _Market
from tradedesk.analytics.carry_bybit_inventory import has_bar_in, last_active_day, max_apr
from tradedesk.analytics.carry_delisting import last_perp_close, last_spot_close
from tradedesk.analytics.coinm_funding import round_hour
from tradedesk.analytics.demo_execution import model_fee_usd
from tradedesk.analytics.mark_stop import stop_level_at_exit
from tradedesk.analytics.rec_paper import delivery_fees, leg_price
from tradedesk.analytics.stop_management import Fees, Outcome

H_MS = 3_600_000
DAY_MS = 86_400_000

# ------------------------------------------------------------------ demo_execution


def test_model_fee_usd_maker_in_maker_out_at_target() -> None:
    # 2 * (0.0002 * 100 + 0.0002 * 110) = 2 * 0.042
    assert model_fee_usd(2.0, 100.0, 110.0, "target") == pytest.approx(0.084)


@pytest.mark.parametrize("reason", ["stop", "breakeven", "time", None])
def test_model_fee_usd_taker_exit_otherwise(reason: str | None) -> None:
    # 2 * (0.0002 * 100 + 0.00055 * 95) = 2 * 0.07225
    assert model_fee_usd(2.0, 100.0, 95.0, reason) == pytest.approx(0.1445)


def test_model_fee_usd_uses_absolute_prices_and_zero_qty() -> None:
    # mirrored (negative) prices pay the same fee as the real ones
    assert model_fee_usd(1.0, -100.0, -95.0, "stop") == pytest.approx(model_fee_usd(1.0, 100.0, 95.0, "stop"))
    assert model_fee_usd(0.0, 100.0, 95.0, "stop") == 0.0


# ------------------------------------------------------------------------ mark_stop


def _outcome(kind: str) -> Outcome:
    return Outcome(99.0, kind, kind.startswith("be"), -1, -1.0, 0.1, 0.0, 0, H_MS)


@pytest.mark.parametrize("kind", ["be", "be_gap"])
def test_stop_level_at_exit_break_even(kind: str) -> None:
    # default be_offset = 2 * 0.00055 = 0.0011 -> 100 * 1.0011
    assert stop_level_at_exit(_outcome(kind), 100.0, 95.0) == pytest.approx(100.11)
    assert stop_level_at_exit(_outcome(kind), 100.0, 95.0, Fees(be_offset=0.002)) == pytest.approx(100.2)


@pytest.mark.parametrize("kind", ["stop", "stop_gap", "target", "time"])
def test_stop_level_at_exit_initial_stop(kind: str) -> None:
    assert stop_level_at_exit(_outcome(kind), 100.0, 95.0) == 95.0


def test_stop_level_at_exit_mirrored_short() -> None:
    # a short on negated prices: entry -100, offset on |entry| -> -100 + 0.11
    assert stop_level_at_exit(_outcome("be"), -100.0, -105.0) == pytest.approx(-99.89)


# ------------------------------------------------------------------------- rec_paper


def test_leg_price_atm_closed_form_and_sign_of_qty() -> None:
    # ATM Black-76: F * (2 N(v/2) - 1) = F * erf(v / (2 sqrt 2)), v = 0.5 * sqrt(0.25) = 0.25
    ref = 100_000.0 * math.erf(0.25 / (2.0 * math.sqrt(2.0)))
    assert ref == pytest.approx(9_947.6, abs=0.1)  # close to 0.4 * F * v = 10000
    assert leg_price(100_000.0, (100_000.0, True, 1.0, 0.5), 0.25) == pytest.approx(ref)
    assert leg_price(100_000.0, (100_000.0, False, 1.0, 0.5), 0.25) == pytest.approx(ref)
    # qty does not change the unit price (sold legs are signed later)
    assert leg_price(100_000.0, (100_000.0, True, -3.0, 0.5), 0.25) == pytest.approx(ref)


def test_leg_price_put_call_parity() -> None:
    c = leg_price(100_000.0, (90_000.0, True, 1.0, 0.6), 0.1)
    p = leg_price(100_000.0, (90_000.0, False, 1.0, 0.6), 0.1)
    assert c - p == pytest.approx(10_000.0)


def test_leg_price_intrinsic_at_expiry_or_zero_vol() -> None:
    assert leg_price(110.0, (100.0, True, 1.0, 0.5), 0.0) == 10.0
    assert leg_price(110.0, (100.0, False, 1.0, 0.5), 0.0) == 0.0
    assert leg_price(90.0, (100.0, False, 1.0, 0.0), 0.5) == 10.0


def test_delivery_fees_itm_legs_only_with_cap() -> None:
    s = 100_000.0  # uncapped fee 0.00015 * s = 15 USD per unit
    legs = [
        (90_000.0, True, 1.0, 0.5),  # payoff 10000, cap 1250 -> 15
        (95_000.0, False, -2.0, 0.5),  # out of the money -> 0
        (99_950.0, True, 2.0, 0.5),  # payoff 50, cap 6.25 -> 6.25 x 2
    ]
    assert delivery_fees(legs, s) == pytest.approx(15.0 + 12.5)


def test_delivery_fees_at_strike_and_empty() -> None:
    assert delivery_fees([(100_000.0, True, 1.0, 0.5), (100_000.0, False, 1.0, 0.5)], 100_000.0) == 0.0
    assert delivery_fees([], 100_000.0) == 0.0


# ------------------------------------------------------------------- carry_delisting


def _bar(ts: int, close: float) -> dict:
    return {"ts": ts, "close": close, "high": close}


def test_last_closes_after_the_perp_vanishes() -> None:
    # 4h bars: perp closes at 4h, 8h and then disappears; spot keeps closing at 12h and 16h
    m = _Market(
        {
            "funding": [],
            "perp": [_bar(0, 20.0), _bar(4 * H_MS, 21.0)],
            "spot": [_bar(k * 4 * H_MS, 10.0 + k) for k in range(4)],
        },
        4 * H_MS,
    )
    assert last_perp_close(m, 16 * H_MS) == 21.0  # the perp stays at its last close
    assert last_spot_close(m, 16 * H_MS) == 13.0
    assert last_spot_close(m, 12 * H_MS - 1) == 11.0  # the 12h close is not known yet
    assert last_perp_close(m, 4 * H_MS) == 20.0  # close time is inclusive
    assert last_perp_close(m, 4 * H_MS - 1) is None
    assert last_spot_close(m, 0) is None


def test_last_closes_use_bar_length() -> None:
    m = _Market({"funding": [], "perp": [_bar(0, 5.0)], "spot": [_bar(0, 6.0)]}, H_MS)
    assert last_perp_close(m, H_MS) == 5.0
    assert last_spot_close(m, H_MS - 1) is None


# ------------------------------------------------------------- carry_bybit_inventory


def test_last_active_day() -> None:
    assert last_active_day({}) is None
    assert last_active_day({3 * DAY_MS: 1.0, DAY_MS: 2.0}) == 3 * DAY_MS


def test_has_bar_in_counts_the_day_containing_lo() -> None:
    days = {10 * DAY_MS: 1.0}
    assert has_bar_in(days, 10 * DAY_MS + 5 * H_MS, 12 * DAY_MS)  # lo inside day 10
    assert not has_bar_in(days, 11 * DAY_MS, 12 * DAY_MS)  # day 10 ended at lo
    assert has_bar_in(days, 0, 10 * DAY_MS)  # hi inclusive
    assert not has_bar_in(days, 0, 10 * DAY_MS - 1)
    assert not has_bar_in({}, 0, 12 * DAY_MS)


# 8h payments 0.0001 at 0 .. 20 days; the payment at day 10 pays 0.0008
PAY = [(k * 8 * H_MS, 0.0008 if k == 30 else 0.0001) for k in range(61)]
BASE_APR = 21 * 0.0001 * 365 / 7  # 21 payments in (t - 7d; t] -> 0.1095
BUMP_APR = (21 * 0.0001 + 0.0007) * 365 / 7  # windows holding the day-10 payment -> 0.146


def _days(lo: int, hi: int) -> dict[int, float]:
    return {d * DAY_MS: 1.0 for d in range(lo, hi + 1)}


def test_max_apr_reference_values() -> None:
    assert BASE_APR == pytest.approx(0.1095)
    assert BUMP_APR == pytest.approx(0.146)
    both = _days(0, 20)
    assert max_apr(PAY, 7 * DAY_MS, 20 * DAY_MS, both, both) == pytest.approx(BUMP_APR)
    # hi is exclusive: the cycle at day 10 00:00 is the first to hold the bump
    assert max_apr(PAY, 7 * DAY_MS, 10 * DAY_MS, both, both) == pytest.approx(BASE_APR)
    assert max_apr(PAY, 7 * DAY_MS, 10 * DAY_MS + 1, both, both) == pytest.approx(BUMP_APR)


def test_max_apr_needs_both_legs_that_day() -> None:
    # the spot trades only up to day 9: the bumped cycles (days 10..16) do not count
    assert max_apr(PAY, 7 * DAY_MS, 20 * DAY_MS, _days(0, 20), _days(0, 9)) == pytest.approx(BASE_APR)
    assert max_apr(PAY, 7 * DAY_MS, 20 * DAY_MS, _days(0, 20), {}) is None


def test_max_apr_needs_full_history() -> None:
    both = _days(0, 20)
    # every cycle before day 7 has less than 7 days of payments behind it
    assert max_apr(PAY, 0, 7 * DAY_MS, both, both) is None
    assert max_apr([], 7 * DAY_MS, 20 * DAY_MS, both, both) is None
    assert max_apr(PAY, 0, 7 * DAY_MS + 1, both, both) == pytest.approx(BASE_APR)  # day 7 00:00 is the first


# --------------------------------------------------------------------- coinm_funding


T_HOUR = 1_730_851_200_000  # 2024-11-06 00:00 UTC, a whole hour


def test_round_hour_removes_settlement_jitter() -> None:
    assert T_HOUR % H_MS == 0
    assert round_hour(T_HOUR + 5) == T_HOUR
    assert round_hour(T_HOUR - 3) == T_HOUR
    assert round_hour(T_HOUR + 29 * 60_000) == T_HOUR
    assert round_hour(T_HOUR + 31 * 60_000) == T_HOUR + H_MS
    assert round_hour(T_HOUR) == T_HOUR
