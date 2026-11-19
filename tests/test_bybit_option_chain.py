"""bybit_option_chain (standing card IDEA-20261102, item of 19.11): the Bybit option chain on a sample of the v5 option
tickers (tests/fixtures/bybit_option_chain_btc_2026-10-23.json, Bybit demo venue, rows unchanged). Fakes only, no
network: the ccxt exchange is a FakeEx whose implicit endpoint returns the sample."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import pytest

from conftest import FakeEx, State, make_transports
from test_server import call
from tradedesk.analytics.bybit_options import expiry_view, leg, parse_chain, select_expiries
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "bybit_option_chain_btc_2026-10-23.json").read_text(encoding="utf-8"))
FX_NOW = datetime.fromtimestamp(SAMPLE["time"] / 1000, UTC)  # 2026-10-23 16:32:18 UTC, the venue's response time
ROWS = SAMPLE["result"]["list"]


def _row(symbol: str) -> dict:
    return next(r for r in ROWS if r["symbol"] == symbol)


def chain_rt(mode: str = "ok", calls: list | None = None) -> Runtime:
    """A Runtime whose Bybit answers the sample (BTC; ETH gets an empty list). ``mode``: "down" raises a network error,
    "usdc_dup" appends every row again as a USDC-settled symbol (no suffix) with a different bid."""

    class Ex(FakeEx):
        async def publicGetV5MarketTickers(self, params):  # noqa: N802 - ccxt implicit endpoint name
            if calls is not None:
                calls.append(dict(params))
            if mode == "down":
                raise ccxt.NetworkError("bybit unreachable")
            body = copy.deepcopy(SAMPLE)
            if params.get("baseCoin") != "BTC":
                body["result"]["list"] = []
            if mode == "usdc_dup":
                body["result"]["list"] += [dict(r, symbol=r["symbol"].removesuffix("-USDT"), bid1Price="5") for r in ROWS]
            return body

    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=""),
        transports=make_transports(State()),
        exchange_factory=Ex,
    )
    rt.now = lambda: FX_NOW
    return rt


async def _call(mode: str = "ok", calls: list | None = None, **args) -> dict:
    rt = chain_rt(mode, calls)
    srv, _ = build_server(rt)
    try:
        return await call(srv, "bybit_option_chain", **args)
    finally:
        await rt.aclose()


async def test_default_is_nearest_expiries_around_atm_with_evidence():
    calls: list = []
    env = await _call(calls=calls)
    assert env["ok"] is True and env["missing"] == [] and env["warnings"] == []
    assert calls == [{"category": "option", "baseCoin": "BTC"}]
    ev = env["evidence"]
    assert len(ev) == 1 and ev[0]["provider"] == "ccxt:bybit" and ev[0]["args"]["endpoint"] == "publicGetV5MarketTickers"
    d = env["data"]
    assert (d["currency"], d["settle"], d["quote_time_ms"]) == ("BTC", "USDT", SAMPLE["time"])
    assert d["index"] == pytest.approx(81644.78, abs=0.01)
    assert (d["expiries_listed"], d["expiries_in_window"]) == (4, 4)
    assert [e["expiry"] for e in d["expiries"]] == ["24OCT26", "6NOV26", "13NOV26"]  # max_expiries=3, nearest first
    e = d["expiries"][1]
    # 6NOV26 08:00 UTC is 13 days 15:27:42 after the sample: 13.644 days
    assert e["days"] == pytest.approx(13.64, abs=0.01) and e["underlying"] == pytest.approx(81821.7)
    assert e["atm_strike"] == 82000.0 and e["n_strikes"] == 12
    assert [s["strike"] for s in e["strikes"]] == [79000.0, 80000.0, 81000.0, 82000.0, 83000.0, 84000.0, 85000.0]  # ATM +- 3
    put = next(s for s in e["strikes"] if s["strike"] == 80000.0)["put"]
    # BTC-6NOV26-80000-P-USDT: bid 1485, ask 1490, bid IV 0.3655, ask IV 0.3664, mark IV 0.366, OI 40.6
    assert (put["bid"], put["ask"], put["mid"]) == (1485.0, 1490.0, 1487.5)
    assert put["spread_pct"] == pytest.approx(5 / 1487.5 * 100, abs=0.01)
    assert put["spread_iv"] == pytest.approx(0.0009, abs=1e-4)
    assert (put["mark_iv"], put["oi"]) == (0.366, 40.6)
    assert set(put) == {"bid", "ask", "mid", "spread_pct", "spread_iv", "mark_iv", "oi"}  # compact: no detail fields
    atm = next(s for s in e["strikes"] if s["strike"] == 82000.0)
    ivs = [float(_row(f"BTC-6NOV26-82000-{t}-USDT")["markIv"]) for t in "CP"]
    assert e["atm_iv"] == pytest.approx(sum(ivs) / 2, abs=1e-4) and atm["call"]["mark_iv"] == pytest.approx(ivs[0])
    oi_c = sum(float(r["openInterest"]) for r in ROWS if r["symbol"].startswith("BTC-6NOV26-") and "-C-" in r["symbol"])
    assert e["oi_calls"] == pytest.approx(oi_c, abs=0.01)  # over all strikes of the expiry, not just those shown


async def test_include_details_returns_every_expiry_and_strike():
    env = await _call(include_details=True)
    d = env["data"]
    assert env["ok"] and [e["expiry"] for e in d["expiries"]] == ["24OCT26", "6NOV26", "13NOV26", "25DEC26"]
    assert sum(len(e["strikes"]) for e in d["expiries"]) == sum(e["n_strikes"] for e in d["expiries"]) == 7 + 12 + 13 + 12
    legs = [x for e in d["expiries"] for s in e["strikes"] for x in (s["call"], s["put"]) if x is not None]
    assert len(legs) == len(ROWS)
    c = next(s for s in d["expiries"][1]["strikes"] if s["strike"] == 80000.0)["call"]
    assert c["symbol"] == "BTC-6NOV26-80000-C-USDT" and (c["bid_size"], c["ask_size"]) == (1.76, 2.77)
    assert (c["bid_iv"], c["ask_iv"]) == (0.3644, 0.3653) and c["delta"] == pytest.approx(float(_row(c["symbol"])["delta"]), abs=1e-4)


async def test_one_expiry_and_window():
    one = await _call(expiry="2026-11-13", strikes_each_side=1)
    assert one["ok"] and [e["expiry"] for e in one["data"]["expiries"]] == ["13NOV26"]
    assert len(one["data"]["expiries"][0]["strikes"]) == 3
    tenor = await _call(min_days=7, max_days=30)
    assert [e["expiry"] for e in tenor["data"]["expiries"]] == ["6NOV26", "13NOV26"] and tenor["data"]["expiries_in_window"] == 2
    none = await _call(min_days=100)
    assert none["ok"] and none["data"]["expiries"] == [] and "listed: 24OCT26" in none["warnings"][0]


async def test_unknown_expiry_is_missing_not_raised():
    env = await _call(expiry="1JAN30")
    assert env["ok"] is False and env["missing"][0]["field"] == "expiry"
    assert "Available: 24OCT26, 6NOV26, 13NOV26, 25DEC26" in env["missing"][0]["detail"]


async def test_provider_failure_goes_to_missing():
    env = await _call("down")
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["provider"], m["reason"]) for m in env["missing"]] == [("bybit_options:BTC", "ccxt:bybit", "network")]


async def test_empty_chain_is_missing():
    env = await _call(currency="ETH")
    assert env["ok"] is False and env["missing"][0]["reason"] == "data_unavailable" and env["evidence"]


async def test_usdc_rows_are_skipped_with_a_warning():
    plain, dup = await _call(), await _call("usdc_dup")
    assert dup["data"] == plain["data"]  # the USDC duplicates (bid 5) never replace a USDT quote
    assert dup["warnings"] == ["BTC: Bybit option symbols with settle suffix none (USDC) skipped (only USDT is shown)"]


# ---------- analytics (pure) ----------


def test_leg_without_bid_has_no_mid_spread_or_iv_spread():
    x = leg(
        {"symbol": "BTC-23OCT26-85500-C-USDT", "bid1Price": "0", "bid1Iv": "0", "ask1Price": "5", "ask1Iv": "0.7508", "markIv": "0.4844"}
    )
    assert (x["bid"], x["ask"], x["mid"], x["spread_pct"], x["spread_iv"]) == (None, 5.0, None, None, None)
    assert x["mark_iv"] == 0.4844 and x["oi"] is None
    y = leg({"bid1Price": "15935", "bid1Iv": "0", "ask1Price": "16095", "ask1Iv": "3.8311", "openInterest": "0"})
    assert y["mid"] == 16015.0 and y["spread_iv"] is None and y["oi"] == 0.0  # Bybit IV "0" = none; OI 0 is a value


def test_parse_chain_skips_other_coins_settles_and_bad_strikes():
    rows = [
        {"symbol": "BTC-6NOV26-80000-C-USDT"},
        {"symbol": "ETH-6NOV26-3000-C-USDT"},
        {"symbol": "BTC-6NOV26-80000-C"},
        {"symbol": "BTC-6NOV26-inf-C-USDT"},
        {"symbol": "garbage"},
        "not a dict",
    ]
    chain, other = parse_chain(rows, "BTC")
    exp = datetime(2026, 11, 6, 8, tzinfo=UTC)
    assert list(chain) == [exp] and list(chain[exp]) == [80000.0] and other == ["none (USDC)"]
    assert parse_chain(None, "BTC") == ({}, [])
    assert select_expiries([exp], FX_NOW, 0, 13.5) == [] and select_expiries([exp], FX_NOW, 13.6, 13.7) == [exp]


def test_expiry_view_without_underlying_centres_on_the_median_strike():
    strikes = {float(k): {"C": {"bid1Price": "10", "ask1Price": "12"}} for k in (1, 2, 3, 4, 5)}
    v = expiry_view(strikes, datetime(2026, 11, 6, 8, tzinfo=UTC), FX_NOW, 1)
    assert v["underlying"] is None and v["atm_strike"] is None and v["atm_iv"] is None
    assert [s["strike"] for s in v["strikes"]] == [2.0, 3.0, 4.0] and v["strikes"][0]["put"] is None
