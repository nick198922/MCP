"""Current funding that is not a finite number (standing card item 257, review 2026-11-07-funding-entries-wrong-type).

The funding-history rows already drop NaN / +-inf (item 254). The current rate did not: `current_funding` (behind
funding_carry_scan and funding_spread_scan) let a NaN or an infinity through `json_num`, `hl_current_funding` took any
float, and perps_funding_compare ran `float()` on the venue's rate, so a NaN came out as the venue's APR and a
non-numeric string raised out of the tool. Now such a rate leaves the symbol (or the venue) out and a warning names it.
"""

from __future__ import annotations

import math

import pytest
from mcp import Client

from test_review_leftovers_nonfinite import HL, _clean_and_broken, _non_finite
from test_tools_provider_outage import as_envelope
from test_tools_wrong_type_fields import Mutator, _failure, _runtime
from tradedesk.server import build_server

COMPARE_CCXT = "ccxt:{}.fetch_funding_rate.fundingRate"
BULK = "ccxt:{}.fetch_funding_rates.{}.fundingRate"
DERIBIT = "http:www.deribit.com/api/v2/public/get_book_summary_by_currency.result[*].funding_8h"


def _venues(env: dict) -> set[str]:
    return {v["venue"] for v in (env["data"] or {}).get("venues", [])}


@pytest.mark.parametrize(
    ("venue", "field", "label"),
    [
        ("binanceusdm", COMPARE_CCXT.format("binanceusdm"), "binanceusdm BTC/USDT:USDT"),
        ("bybit", COMPARE_CCXT.format("bybit"), "bybit BTC/USDT:USDT"),
        ("hyperliquid", f"{HL}[*].funding", "hyperliquid BTC"),
        ("deribit", DERIBIT, "deribit BTC-PERPETUAL"),
    ],
)
@pytest.mark.parametrize("value", [math.nan, math.inf, "abc"])
async def test_perps_funding_compare_leaves_out_a_venue_with_a_non_finite_rate(tmp_path, venue, field, label, value):
    clean, res = await _clean_and_broken(tmp_path, "perps_funding_compare", field, value, "first")
    assert _failure(res) is None, _failure(res)  # a string rate raised ValueError out of the tool before
    env = as_envelope(res)
    if venue == "hyperliquid" and value == "abc":
        # the provider parses HL's strings and takes "abc" as an absent rate: the venue is left out without a warning
        assert _venues(env) == _venues(clean) - {venue}
        return
    if venue == "deribit" and value == "abc":
        assert _venues(env) == _venues(clean) - {venue}  # not a JSON number: skipped as before
        return
    assert env["ok"] is True and _non_finite(env) == []
    assert _venues(env) == _venues(clean) - {venue}
    assert any(w.startswith(f"{label}: current funding rate not a finite number") for w in env["warnings"]), env["warnings"]
    rest = [v["annualized"] for v in env["data"]["venues"]]
    assert rest == sorted(rest, reverse=True)  # a NaN no longer breaks the sort behind widest_spread


@pytest.mark.parametrize("value", [math.nan, -math.inf, "abc"])
async def test_funding_carry_scan_names_the_symbol_with_a_non_finite_current_rate(tmp_path, value):
    sym = "ETH/USDT:USDT"
    clean, res = await _clean_and_broken(tmp_path, "funding_carry_scan", BULK.format("binanceusdm", sym), value, "first")
    assert _failure(res) is None, _failure(res)
    env = as_envelope(res)
    assert env["ok"] is clean["ok"] is True and _non_finite(env) == []
    assert any(w == f"binanceusdm: current funding rate not a finite number for 1 symbols, left out: {sym}" for w in env["warnings"])
    assert env["data"]["rows"] == clean["data"]["rows"]  # ETH is not among the scanned candidates: the rest unchanged


@pytest.mark.parametrize("value", [math.nan, math.inf])
async def test_funding_spread_scan_names_the_symbol_with_a_non_finite_current_rate(tmp_path, value):
    sym = "SOL/USDT:USDT"
    clean, res = await _clean_and_broken(tmp_path, "funding_spread_scan", BULK.format("bybit", sym), value, "first")
    assert _failure(res) is None, _failure(res)
    env = as_envelope(res)
    assert env["ok"] is clean["ok"] and _non_finite(env) == []
    assert any(w == f"bybit: current funding rate not a finite number for 1 symbols, left out: {sym}" for w in env["warnings"])


@pytest.mark.parametrize("value", [math.inf, "NaN"])
async def test_hyperliquid_current_funding_names_the_coin(tmp_path, value):
    m = Mutator()
    srv, _ = build_server(_runtime(tmp_path, m))
    args = {"venues": ["binanceusdm", "hyperliquid"]}
    async with Client(srv) as c:
        await c.call_tool("funding_spread_scan", args)  # warms the coin map and ccxt markets
        m.target = (f"{HL}[*].funding", value, "first")
        res = await c.call_tool("funding_spread_scan", args)
    assert m.hits and _failure(res) is None, _failure(res)
    env = as_envelope(res)
    assert _non_finite(env) == []
    assert any(w.startswith("hyperliquid: current funding rate not a finite number for 1 symbols") for w in env["warnings"])
