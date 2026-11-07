"""perps_funding_compare: a mark or index price that is not a finite number (standing card item 260, BACKLOG 6.52).

Item 257 left the venue out when its funding rate is NaN / inf, but the mark and index prices were passed through as the
venue sent them, so a JSON NaN / Infinity came out as a non-finite number in the answer. Now such a price is null and a
warning names the venue; the venue's funding row stays in the comparison (the rate itself is fine).
"""

from __future__ import annotations

import math

import pytest

from test_current_funding_nonfinite import _venues
from test_review_leftovers_nonfinite import HL, _clean_and_broken, _non_finite
from test_tools_provider_outage import as_envelope
from test_tools_wrong_type_fields import _failure
from tradedesk.core.envelope import Builder
from tradedesk.tools.perps import _finite_price, _finite_rate

CCXT = "ccxt:{}.fetch_funding_rate.{}"
DERIBIT = "http:www.deribit.com/api/v2/public/get_book_summary_by_currency.result[*].{}"

# (venue, mutated field, the row key it lands in, the label in the warning, which occurrence of the field)
CASES = [
    ("binanceusdm", CCXT.format("binanceusdm", "markPrice"), "mark", "binanceusdm BTC/USDT:USDT", "first"),
    ("bybit", CCXT.format("bybit", "indexPrice"), "index", "bybit BTC/USDT:USDT", "first"),
    ("hyperliquid", f"{HL}[*].markPx", "mark", "hyperliquid BTC", "first"),
    ("hyperliquid", f"{HL}[*].oraclePx", "index", "hyperliquid BTC", "first"),
    ("deribit", DERIBIT.format("mark_price"), "mark", "deribit BTC-PERPETUAL", "last"),  # the perpetual is the last row
    ("deribit", DERIBIT.format("estimated_delivery_price"), "index", "deribit BTC-PERPETUAL", "last"),
]


def _row(env: dict, venue: str) -> dict:
    return next(v for v in env["data"]["venues"] if v["venue"] == venue)


def _price_warnings(env: dict) -> list[str]:
    return [w for w in env["warnings"] if "price not a finite number" in w]


async def test_clean_answer_has_finite_prices_and_no_price_warning(tmp_path):
    clean, res = await _clean_and_broken(tmp_path, "perps_funding_compare", CCXT.format("bybit", "markPrice"), 1.0, "first")
    assert clean["ok"] is True and _non_finite(clean) == [] and _price_warnings(clean) == []
    for v in clean["data"]["venues"]:
        assert isinstance(v["mark"], float) and math.isfinite(v["mark"]) and v["mark"] > 0, v
    # a finite replacement price is passed through as a float and warns nothing
    env = as_envelope(res)
    assert _row(env, "bybit")["mark"] == 1.0 and _price_warnings(env) == []


@pytest.mark.parametrize(("venue", "field", "key", "label", "where"), CASES)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
async def test_a_non_finite_price_is_null_and_named(tmp_path, venue, field, key, label, where, value):
    clean, res = await _clean_and_broken(tmp_path, "perps_funding_compare", field, value, where)
    assert _failure(res) is None, _failure(res)
    env = as_envelope(res)
    assert env["ok"] is True and _non_finite(env) == []  # the NaN / inf reached the answer before
    assert _venues(env) == _venues(clean)  # the venue's funding row is kept
    assert _row(env, venue)[key] is None and _row(clean, venue)[key] is not None
    assert _row(env, venue)["annualized"] == _row(clean, venue)["annualized"]
    assert _price_warnings(env) == [f"{label}: {key} price not a finite number; shown as null"], env["warnings"]


def test_finite_price_helper():
    b = Builder()
    assert _finite_price(b, "x", "mark", None) is None  # absent (e.g. OKX bulk has no mark): no warning
    assert _finite_price(b, "x", "mark", "82760.5") == 82760.5 and _finite_price(b, "x", "mark", 5) == 5.0
    assert b.warnings == []
    # 10**400: a 300+-digit JSON integer becomes a Python int and float() of it raises OverflowError
    for bad in ("abc", "NaN", "Infinity", True, [], math.nan, 10**400):
        assert _finite_price(b, "okx BTC/USDT:USDT", "index", bad) is None
    assert b.warnings == ["okx BTC/USDT:USDT: index price not a finite number; shown as null"] * 7


def test_finite_rate_helper_overflow():
    b = Builder()
    assert _finite_rate(b, "x", "0.0001") == 0.0001 and b.warnings == []
    assert _finite_rate(b, "bybit BTC/USDT:USDT", 10**400) is None  # raised OverflowError before
    assert b.warnings == ["bybit BTC/USDT:USDT: current funding rate not a finite number; this venue is left out of the comparison"]
