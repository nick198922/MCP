"""Direct tests for ten analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 172).

They feed the carry A / B walks (observation callbacks), the funding-crowding basket, the token-unlock and strategy
sieve studies, the Hyperliquid smart-money journal and the collector report. References are hand-computed on
synthetic data in the comments.
"""

from __future__ import annotations

import zlib
from datetime import UTC, datetime

import numpy as np
import pytest

from tradedesk.analytics.carry import _Leg, _Market, carry_observers, spread_observers
from tradedesk.analytics.collector_health import GIB, MIB, fmt_bytes
from tradedesk.analytics.dated_basis import last_friday
from tradedesk.analytics.funding_crowding import LegWeek, basket_components, target_weights
from tradedesk.analytics.pm_modules import tau_of_hours
from tradedesk.analytics.smart_money import signed_notional
from tradedesk.analytics.stop_management import Fees
from tradedesk.analytics.strategy_sieve import SEED, funding_r, symbol_seed

H = 3_600_000
B = 4 * H  # bar length of the walks
YEAR_MS = 365 * 24 * H


def _funding(rates: list[float]) -> list[dict[str, float]]:
    # settlements at 8 h, 16 h, 24 h
    return [{"ts_ms": (i + 1) * 2 * B, "rate": r} for i, r in enumerate(rates)]


# ------------------------------------------------------------------ carry.carry_observers


def _market() -> _Market:
    # bar i opens at i*B and closes at (i+1)*B; spot close 100+i, perp close 101+i, perp high 102+i
    return _Market(
        {
            "funding": _funding([0.0001, 0.0002, 0.0003]),
            "spot": [{"ts": i * B, "close": 100 + i} for i in range(7)],
            "perp": [{"ts": i * B, "close": 101 + i, "high": 102 + i} for i in range(7)],
        },
        B,
    )


def test_carry_mark_obs_reads_payments_prices_high_and_apr() -> None:
    mark_obs, _ = carry_observers({"X": _market()}, lookback_ms=2 * B)
    at = 6 * B
    obs = mark_obs("X", {"last": 2 * B, "last_bar": 4 * B}, at)
    # payments strictly after 2B up to 6B: (4B, 0.0002) and (6B, 0.0003), each with the perp close at that time
    # (bar closing at 4B has index 3 -> 104; the one closing at 6B has index 5 -> 106)
    assert obs["payments"] == [(4 * B, 0.0002, 104.0), (6 * B, 0.0003, 106.0)]
    assert obs["perp_high"] == 107.0  # bars closing in (4B, 6B]: highs 106 and 107
    assert obs["spot"] == 105.0
    assert obs["perp"] == 106.0
    # APR over the last 8 h: funding in (4B, 6B] = 0.0003, annualised by 365 d / 8 h = 1095
    assert obs["apr"] == pytest.approx(0.0003 * 1095)


def test_carry_entry_obs_and_missing_data() -> None:
    mark_obs, entry_obs = carry_observers({"X": _market()}, lookback_ms=2 * B)
    apr, spot, perp = entry_obs("X", 6 * B)
    assert (spot, perp) == (105.0, 106.0)
    assert apr == pytest.approx(0.3285)
    # history starts at 2B: a lookback of 8 h is not covered at t = B -> no APR; the bar closing at B is still read
    apr, spot, perp = entry_obs("X", B)
    assert apr is None and (spot, perp) == (100.0, 101.0)
    # a price older than one bar is stale -> None; no bars in the window -> perp_high None
    obs = mark_obs("X", {"last": 6 * B, "last_bar": 7 * B}, 20 * B)
    assert obs["spot"] is None and obs["perp"] is None and obs["perp_high"] is None
    assert obs["payments"] == []


# ------------------------------------------------------------------ carry.spread_observers


def _legs() -> dict[str, tuple[_Leg, _Leg]]:
    def bars(base: float) -> list[dict[str, float]]:
        return [{"ts": i * B, "close": base + i, "high": base + i + 1, "low": base + i - 1} for i in range(7)]

    a = _Leg({"funding": _funding([0.0001, 0.0002, 0.0003]), "bars": bars(100)}, B)
    b = _Leg({"funding": _funding([0.0001, 0.0001, 0.0001]), "bars": bars(200)}, B)
    return {"X": (a, b)}


def test_spread_mark_obs_reads_both_legs() -> None:
    mark_obs, _ = spread_observers(_legs(), lookback_ms=2 * B)
    obs = mark_obs("X", {"last": 2 * B, "last_bar": 4 * B}, 6 * B)
    assert obs["pay_a"] == [(4 * B, 0.0002, 103.0), (6 * B, 0.0003, 105.0)]
    assert obs["pay_b"] == [(4 * B, 0.0001, 203.0), (6 * B, 0.0001, 205.0)]
    # bars closing in (4B, 6B] have indices 4 and 5: A highs 105/106, lows 103/104
    assert obs["ext_a"] == (106.0, 103.0)
    assert obs["ext_b"] == (206.0, 203.0)
    assert (obs["pa"], obs["pb"]) == (105.0, 205.0)
    assert obs["ra"] == pytest.approx(0.0003 * 1095)
    assert obs["rb"] == pytest.approx(0.0001 * 1095)


def test_spread_entry_obs_and_missing_data() -> None:
    mark_obs, entry_obs = spread_observers(_legs(), lookback_ms=2 * B)
    ra, rb, pa, pb = entry_obs("X", 6 * B)
    assert (pa, pb) == (105.0, 205.0)
    assert ra == pytest.approx(0.3285) and rb == pytest.approx(0.1095)
    assert entry_obs("X", B)[:2] == (None, None)
    obs = mark_obs("X", {"last": 6 * B, "last_bar": 7 * B}, 20 * B)
    assert obs["ext_a"] is None and obs["ext_b"] is None and obs["pa"] is None and obs["pb"] is None
    assert obs["pay_a"] == [] and obs["pay_b"] == []


# ------------------------------------------------------------------ dated_basis.last_friday


@pytest.mark.parametrize(
    ("year", "month", "day"),
    [
        (2026, 3, 27),  # 31 Mar 2026 is a Tuesday
        (2026, 7, 31),  # the month ends on a Friday itself
        (2026, 11, 27),  # 30 Nov 2026 is a Monday
        (2026, 12, 25),  # 31 Dec 2026 is a Thursday; December rolls into the next year internally
        (2024, 2, 23),  # leap February: 29 Feb 2024 is a Thursday
    ],
)
def test_last_friday(year: int, month: int, day: int) -> None:
    d = last_friday(year, month)
    assert d == datetime(year, month, day, 8, tzinfo=UTC)
    assert d.weekday() == 4


# ------------------------------------------------------------------ funding_crowding.target_weights / basket_components


def test_target_weights_split_gross_notional_half_each_side() -> None:
    w = target_weights(["A", "B"], ["C"])
    assert w == {"A": 0.25, "B": 0.25, "C": -0.5}
    assert sum(abs(x) for x in w.values()) == pytest.approx(1.0)
    assert sum(w.values()) == pytest.approx(0.0)


def test_target_weights_scales_and_repeated_symbol() -> None:
    w = target_weights(["A", "B"], ["C"], long_scale=2.0, short_scale=0.5)
    assert w == {"A": 0.5, "B": 0.5, "C": -0.25}  # 0.5 * 2 / 2 per long, -0.5 * 0.5 / 1 for the short
    # a symbol listed twice on one side accumulates; the two sides of one symbol net out
    assert target_weights(["A", "A"], []) == {"A": 0.5}  # 2 x (0.5 / 2)
    assert target_weights(["A"], ["A"]) == {"A": 0.0}
    assert target_weights([], []) == {}


def _leg(symbol: str, r: float, funding_long: float) -> LegWeek:
    return LegWeek(symbol, r, funding_long, up=0.0, down=0.0, exit=0, truncated=False, n_settle=0, hold_gaps=0)


def test_basket_components_weight_price_and_funding() -> None:
    legs = {"A": _leg("A", 0.10, 0.001), "B": _leg("B", -0.04, -0.002), "C": _leg("C", 0.02, 0.003)}
    price, fund = basket_components({"A": 0.25, "B": 0.25, "C": -0.5}, legs)
    # price: 0.25*0.10 + 0.25*(-0.04) - 0.5*0.02 = 0.005; funding: 0.25*0.001 + 0.25*(-0.002) - 0.5*0.003 = -0.00175
    assert price == pytest.approx(0.005)
    assert fund == pytest.approx(-0.00175)
    assert basket_components({}, legs) == (0.0, 0.0)


# ------------------------------------------------------------------ collector_health.fmt_bytes


def test_fmt_bytes_units() -> None:
    assert fmt_bytes(0) == "0 КБ"
    assert fmt_bytes(512 * 1024) == "512 КБ"
    assert fmt_bytes(820 * MIB) == "820 МБ"
    assert fmt_bytes(MIB) == "1 МБ"  # the boundary belongs to the larger unit
    assert fmt_bytes(1.5 * GIB) == "1.5 ГБ"
    assert fmt_bytes(12.4 * GIB) == "12.4 ГБ"
    assert fmt_bytes(GIB) == "1.0 ГБ"
    assert fmt_bytes(-3 * GIB) == "-3.0 ГБ"  # the sign is kept, the unit follows the magnitude


# ------------------------------------------------------------------ pm_modules.tau_of_hours


def test_tau_of_hours_is_years_of_365_days() -> None:
    assert tau_of_hours(0.0) == 0.0
    assert tau_of_hours(24 * 365) == pytest.approx(1.0)
    assert tau_of_hours(24) == pytest.approx(1 / 365)
    assert tau_of_hours(12) == pytest.approx(0.5 / 365)


# ------------------------------------------------------------------ smart_money.signed_notional


def test_signed_notional_long_short_and_degenerate_rows() -> None:
    assert signed_notional({"side": "long", "value_usd": 1500.0}) == 1500.0
    assert signed_notional({"side": "short", "value_usd": 1500.0}) == -1500.0
    assert signed_notional({"side": "short", "value_usd": -1500.0}) == -1500.0  # magnitude only; side gives the sign
    assert signed_notional({"side": "long", "value_usd": "250.5"}) == 250.5  # numeric strings from the API
    assert signed_notional({"side": "long", "value_usd": None}) == 0.0
    assert signed_notional({"side": "long"}) == 0.0
    assert signed_notional({"side": "flat", "value_usd": 10.0}) == 0.0
    assert signed_notional({"value_usd": 10.0}) == 0.0


# ------------------------------------------------------------------ strategy_sieve.symbol_seed / funding_r


def test_symbol_seed_is_deterministic_crc32_mix() -> None:
    # crc32("123456789") = 0xCBF43926 (the standard check value), so the seed is a closed-form constant
    assert zlib.crc32(b"123456789") == 0xCBF43926
    assert symbol_seed("123456789") == (SEED * 1_000_003 + 0xCBF43926) % 2**32
    assert symbol_seed("123456789", seed=0) == 0xCBF43926
    assert symbol_seed("BTCUSDT") == symbol_seed("BTCUSDT")
    assert symbol_seed("BTCUSDT") != symbol_seed("ETHUSDT")
    assert symbol_seed("BTCUSDT", seed=1) != symbol_seed("BTCUSDT", seed=2)
    assert 0 <= symbol_seed("ETHUSDT") < 2**32


def test_funding_r_actual_flat_and_none() -> None:
    fees = Fees(risk_taker=0.0)  # the risk unit is then entry - stop = 2 for entry 100 / stop 98
    t = np.array([8 * H, 16 * H, 24 * H], dtype=np.int64)
    rate = np.array([0.0001, 0.0002, -0.0001])
    # entry at 8 h, exit at 25 h: settlements strictly after the entry (16 h, 24 h) sum to 0.0001 per unit notional,
    # in R: 0.0001 * 100 / 2 = 0.005; a short receives it with the opposite sign
    r_long, src = funding_r((t, rate), 8 * H, 25 * H, "long", 100.0, 98.0, fees)
    assert src == "actual" and r_long == pytest.approx(0.005)
    r_short, src = funding_r((t, rate), 8 * H, 25 * H, "short", 100.0, 98.0, fees)
    assert src == "actual" and r_short == pytest.approx(-0.005)
    # history starting after the entry: stress 0.0001 per 8 h against the position, both sides (16 h -> 0.0002 -> 0.01 R)
    for side in ("long", "short"):
        r, src = funding_r((t, rate), 0, 16 * H, side, 100.0, 98.0, fees)
        assert src == "flat" and r == pytest.approx(0.01)
    assert funding_r(None, 0, 16 * H, "long", 100.0, 98.0, fees) == (0.0, "none")
