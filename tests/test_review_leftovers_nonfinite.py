"""Review leftovers (standing card item 254), three fixes for data accuracy and invariant 1.

1. A funding rate that parses to NaN or +-inf (JSON `NaN` / `Infinity`, which httpx and ccxt hand out as floats, or the
   numeric string "Infinity" Hyperliquid would send) is dropped like a malformed one instead of poisoning the sums of
   perps_funding_history, hyperliquid_funding_history, funding_carry_scan, funding_spread_scan and the paper carry
   (review 2026-11-07-funding-entries-wrong-type, remark 1).
2. An infinite timestamp in a per-row parser (`int(inf)` raises OverflowError, which those try blocks did not catch)
   drops the row instead of escaping the tool (review 2026-11-06-wrong-type-fields, second round, remark 1).
3. range_detect / range_setup with source='live' read ccxt bars the way range_setup_scan does: a None volume (ccxt
   hands it out for some venues) counts as 0 instead of raising TypeError out of the tool, and a bar with a
   non-numeric price is dropped with a warning (review 2026-11-06-wrong-type-fields, remark 4 and its leftover 2(c)).
"""

from __future__ import annotations

import math
import re
from typing import Any

import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from test_tools_envelope_shape import ARGS
from test_tools_provider_outage import as_envelope
from test_tools_wrong_type_fields import Mutator, _failure, _runtime
from tradedesk.collector.market_data import funding_entries
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools._util import finite_float

HL = "http:api.hyperliquid.xyz/info[*]"
CCXT_HIST = "ccxt:{}.fetch_funding_rate_history[*].fundingRate"


def _non_finite(tree: Any, path: str = "") -> list[str]:
    """Paths of every NaN / inf float in an answer."""
    if isinstance(tree, float):
        return [] if math.isfinite(tree) else [path]
    if isinstance(tree, dict):
        return [p for k, v in tree.items() for p in _non_finite(v, f"{path}.{k}")]
    if isinstance(tree, list):
        return [p for i, v in enumerate(tree) for p in _non_finite(v, f"{path}[{i}]")]
    return []


async def _clean_and_broken(tmp_path, name: str, field: str, value: Any, where: str) -> tuple[dict, Any]:
    """The tool's clean answer and its answer with one occurrence of `field` set to `value` (the raw call result)."""
    m = Mutator()
    rt = _runtime(tmp_path, m)
    srv, _ = build_server(rt)
    args = ARGS.get(name, {})
    async with Client(srv) as c:
        await c.call_tool(name, args)  # warms the coin map and ccxt markets
        m.seen = {}
        clean = as_envelope(await c.call_tool(name, args))
        assert field in m.seen, f"{name} does not read {field}: {sorted(m.seen)[:20]}"
        m.seen, m.target = None, (field, value, where)
        broken = await c.call_tool(name, args)
    assert m.hits, f"{field} was not broken"
    return clean, broken


def test_finite_float():
    assert finite_float("-0.0002") == -0.0002 and finite_float(5) == 5.0
    for bad in (math.inf, -math.inf, math.nan, "Infinity", "-inf", "NaN"):
        with pytest.raises(ValueError):
            finite_float(bad)
    with pytest.raises(TypeError):
        finite_float(None)


def test_funding_entries_drops_a_non_finite_rate():
    rows = [
        {"timestamp": 1, "fundingRate": 0.0001},
        {"timestamp": 2, "fundingRate": math.nan},
        {"timestamp": 3, "fundingRate": math.inf},
        {"timestamp": 4, "fundingRate": "-Infinity"},
        {"timestamp": 5, "fundingRate": "-0.0002"},
        {"timestamp": math.inf, "fundingRate": 0.0003},  # int(inf): OverflowError, dropped by parsed_rows
    ]
    assert funding_entries(rows) == [{"ts_ms": 1, "rate": 0.0001}, {"ts_ms": 5, "rate": -0.0002}]


def _venue_count(env: dict, venue: str) -> int:
    return next(v["count"] for v in env["data"]["venues"] if v["venue"] == venue)


@pytest.mark.parametrize(
    ("venue", "field", "value", "where"),
    [
        ("binanceusdm", CCXT_HIST.format("binanceusdm"), math.nan, "last"),  # the latest rate
        ("bybit", CCXT_HIST.format("bybit"), -math.inf, "first"),
        ("hyperliquid", f"{HL}.fundingRate", math.inf, "last"),  # JSON Infinity
        ("hyperliquid", f"{HL}.fundingRate", "NaN", "second"),  # HL sends numeric strings
    ],
)
async def test_perps_funding_history_drops_a_non_finite_rate(tmp_path, venue, field, value, where):
    clean, res = await _clean_and_broken(tmp_path, "perps_funding_history", field, value, where)
    assert _failure(res) is None
    env = as_envelope(res)
    assert env["ok"] is True and _non_finite(env) == []
    assert _venue_count(env, venue) == _venue_count(clean, venue) - 1
    for other in {v["venue"] for v in clean["data"]["venues"]} - {venue}:
        assert _venue_count(env, other) == _venue_count(clean, other)


@pytest.mark.parametrize("value", [math.inf, "Infinity", "nan"])
async def test_hyperliquid_funding_history_drops_a_non_finite_rate(tmp_path, value):
    clean, res = await _clean_and_broken(tmp_path, "hyperliquid_funding_history", f"{HL}.fundingRate", value, "last")
    env = as_envelope(res)
    assert env["ok"] is True and _non_finite(env) == []
    assert len(env["data"]["entries"]) == env["data"]["stats"]["count"] == clean["data"]["stats"]["count"] - 1


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("funding_carry_scan", CCXT_HIST.format("binanceusdm")),
        ("funding_spread_scan", CCXT_HIST.format("binanceusdm")),
        ("funding_spread_scan", CCXT_HIST.format("bybit")),
        ("carry_paper_open", CCXT_HIST.format("bybit")),
    ],
)
@pytest.mark.parametrize("value", [math.nan, math.inf])
async def test_funding_scans_and_paper_carry_drop_a_non_finite_rate(tmp_path, name, field, value):
    # Before the fix the NaN came out as null trailing APRs, funding_spread_scan paired a venue with itself and
    # carry_paper_open warned "trailing spread ... is +nan%". One payment fewer moves the numbers a little, but no
    # number may turn into null and no label (venue, status, flag) may change.
    clean, res = await _clean_and_broken(tmp_path, name, field, value, "last")
    assert _failure(res) is None
    env = as_envelope(res)
    assert env["ok"] is clean["ok"] is True and _non_finite(env) == []
    assert _labels(env["data"]) == _labels(clean["data"])
    assert not any(re.search(r"\b(nan|inf)\b", w) for w in env["warnings"]), env["warnings"]


def _labels(tree: Any, path: str = "") -> list[tuple[str, Any]]:
    """(path, value) of every null, bool and string leaf: what a non-finite number would turn into or flip."""
    if isinstance(tree, dict):
        return [p for k, v in tree.items() if k != "id" for p in _labels(v, f"{path}.{k}")]
    if isinstance(tree, list):
        return [p for i, v in enumerate(tree) for p in _labels(v, f"{path}[{i}]")]
    return [(path, tree)] if tree is None or isinstance(tree, bool | str) else []


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("hyperliquid_trades", f"{HL}.time"),
        ("hyperliquid_funding_history", f"{HL}.time"),
        ("hyperliquid_candles", f"{HL}.t"),
        ("deribit_largest_trades", "http:www.deribit.com/api/v2/public/get_last_trades_by_currency.result.trades[*].timestamp"),
        ("polymarket_history", "http:clob.polymarket.com/prices-history.history[*].t"),
    ],
)
@pytest.mark.parametrize("where", ["first", "last"])
async def test_an_infinite_timestamp_drops_the_row_instead_of_raising(tmp_path, name, field, where):
    clean, res = await _clean_and_broken(tmp_path, name, field, math.inf, where)  # JSON Infinity -> float inf
    assert _failure(res) is None, _failure(res)
    env = as_envelope(res)
    assert env["ok"] is clean["ok"] is True and _non_finite(env) == []


async def test_hyperliquid_trades_counts_the_dropped_row(tmp_path):
    clean, res = await _clean_and_broken(tmp_path, "hyperliquid_trades", f"{HL}.time", math.inf, "first")
    env = as_envelope(res)
    assert env["data"]["fetched"] == clean["data"]["fetched"]
    assert env["data"]["parsed"] == clean["data"]["parsed"] - 1


class _Bars(FakeEx):
    """FakeEx whose OHLCV rows get `volume` in place of the volume and, for the bar at `bad_close`, a close of "abc"."""

    volume: Any = None
    bad_close: int | None = None

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
        rows = await super().fetch_ohlcv(symbol, timeframe, limit, since, params)
        out = [[*x[:5], self.volume] for x in rows]
        if self.bad_close is not None:
            out[self.bad_close][4] = "abc"
        return out


def _bars_runtime(tmp_path, volume: Any, bad_close: int | None = None) -> Runtime:
    cls = type("Bars", (_Bars,), {"volume": volume, "bad_close": bad_close})
    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / f"bars_{volume}_{bad_close}.sqlite")),
        transports=make_transports(State()),
        exchange_factory=cls,
    )
    rt.now = lambda: NOW
    return rt


async def _call(rt: Runtime, name: str, args: dict) -> Any:
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        return await c.call_tool(name, args)


@pytest.mark.parametrize("name", ["range_detect", "range_setup"])
async def test_live_ranges_take_a_none_volume_as_zero(tmp_path, name):
    args = {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT", "source": "live"}
    res = await _call(_bars_runtime(tmp_path, None), name, args)
    assert _failure(res) is None, _failure(res)  # raised TypeError before
    env = as_envelope(res)
    zero = as_envelope(await _call(_bars_runtime(tmp_path, 0.0), name, args))
    assert (env["ok"], env["data"], env["missing"]) == (zero["ok"], zero["data"], zero["missing"])
    assert not any("malformed" in w for w in env["warnings"])


@pytest.mark.parametrize("name", ["range_detect", "range_setup"])
async def test_live_ranges_drop_a_bar_with_a_bad_price_and_say_so(tmp_path, name):
    args = {"venue": "binanceusdm", "symbol": "BTC/USDT:USDT", "source": "live"}
    res = await _call(_bars_runtime(tmp_path, 10.0, bad_close=5), name, args)
    assert _failure(res) is None, _failure(res)  # raised ValueError before
    env = as_envelope(res)
    assert any(w.startswith("1 malformed bars") for w in env["warnings"]), env["warnings"]
    if name == "range_detect":
        clean = as_envelope(await _call(_bars_runtime(tmp_path, 10.0), name, args))
        assert env["data"]["n_bars_evaluated"] == clean["data"]["n_bars_evaluated"] - 1


async def test_range_setup_scan_keeps_bars_with_a_none_volume(tmp_path):
    """Leftover 2(c) of the wrong-type review: a None volume on every bar is not `insufficient_bars`."""
    args = {"venue": "binanceusdm", "symbols": ["BTC/USDT:USDT"]}
    env = as_envelope(await _call(_bars_runtime(tmp_path, None), "range_setup_scan", args))
    zero = as_envelope(await _call(_bars_runtime(tmp_path, 0.0), "range_setup_scan", args))
    assert env["data"] == zero["data"]
    assert "insufficient_bars" not in str(env["data"])
