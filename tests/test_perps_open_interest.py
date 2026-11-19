"""Item 404 (review docs/reviews/2026-11-19-tool-desc-audit-403.md, finding 1): behaviour of perps_open_interest on the
fake transports, so the units and sources of `oi_usd` named in its description are pinned by what the tool returns, not
only by the text (tests/test_tool_desc_audit.py pins the text).

(a) a ccxt venue with openInterestValue: oi_usd is that value, no ticker call, no estimate warning;
(b) a ccxt venue without it: oi_usd = openInterestAmount x ticker last, the venue named in `warnings`;
(c) Deribit: oi_base null, oi_usd = the PERPETUAL's USD open interest (not a dated future's);
(d) Hyperliquid: oi_base in coins, oi_usd from the asset context (coins x mark);
(e) one provider failing: an entry in `missing`, `ok` true with the remaining venues (invariant 1).
No network: conftest's MockTransports and FakeEx.
"""

from __future__ import annotations

import ccxt
import httpx
import pytest

from conftest import FUTURES, NOW, FakeEx, State, hl_payload, make_transports
from test_server import call
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

# Values chosen so that no product coincides with another number in the fakes (FakeEx last is 82 750).
AMOUNT = 1234.5
LAST = 50_000.0


def _server(state: State, factory=FakeEx, down: tuple[str, ...] = ()):
    tr = make_transports(state)
    for name in down:
        tr[name] = httpx.MockTransport(lambda req: httpx.Response(503, text="down"))
    rt = Runtime(Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0), transports=tr, exchange_factory=factory)
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


def _rows(env: dict) -> dict[str, dict]:
    return {v["venue"]: v for v in env["data"]["venues"]}


class _CountingEx(FakeEx):
    """FakeEx that counts fetch_ticker calls (class-wide: the runtime builds one instance per venue)."""

    ticker_calls: list[str] = []

    async def fetch_ticker(self, symbol):
        type(self).ticker_calls.append(self.id)
        return await super().fetch_ticker(symbol)


class _NoValueEx(FakeEx):
    """openInterestValue not reported (key absent); last price differs from FakeEx's so the product is unambiguous."""

    async def fetch_open_interest(self, symbol):
        return {"openInterestAmount": AMOUNT}

    async def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": LAST}


class _NoValueNoLastEx(_NoValueEx):
    async def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": None}


class _BybitOIDownEx(FakeEx):
    async def fetch_open_interest(self, symbol):
        if self.id == "bybit":
            raise ccxt.NetworkError("bybit down")
        return await super().fetch_open_interest(symbol)


async def test_a_reported_usd_value_is_used_as_is(state):
    _CountingEx.ticker_calls = []
    env = await call(_server(state, _CountingEx), "perps_open_interest", base="BTC", exchanges=["binanceusdm"])
    assert env["ok"] is True
    row = _rows(env)["binanceusdm"]
    assert row["oi_base"] == pytest.approx(1000.0)  # FakeEx openInterestAmount
    assert row["oi_usd"] == pytest.approx(120_000_000.0)  # FakeEx openInterestValue, not 1000 x 82 750
    assert _CountingEx.ticker_calls == []  # no ticker fetched when the venue reports a USD value
    assert not any("estimated" in w for w in env["warnings"])
    assert not any(w.startswith("binanceusdm:") for w in env["warnings"])


async def test_b_missing_usd_value_is_estimated_from_last_price(state):
    env = await call(_server(state, _NoValueEx), "perps_open_interest", base="BTC", exchanges=["binanceusdm"])
    assert env["ok"] is True
    row = _rows(env)["binanceusdm"]
    assert row["oi_base"] == pytest.approx(AMOUNT)
    assert row["oi_usd"] == pytest.approx(AMOUNT * LAST)
    est = [w for w in env["warnings"] if "estimated" in w]
    assert len(est) == 1 and est[0].startswith("binanceusdm:")
    # The estimate counts towards the total like a reported value.
    assert env["data"]["total_oi_usd"] == pytest.approx(sum(v["oi_usd"] for v in env["data"]["venues"] if v["oi_usd"]))
    assert row["share"] == pytest.approx(AMOUNT * LAST / env["data"]["total_oi_usd"], abs=1e-4)


async def test_b_no_usd_value_and_no_last_price_is_left_out_of_the_total(state):
    env = await call(_server(state, _NoValueNoLastEx), "perps_open_interest", base="BTC", exchanges=["binanceusdm"])
    assert env["ok"] is True
    row = _rows(env)["binanceusdm"]
    assert row["oi_base"] == pytest.approx(AMOUNT) and row["oi_usd"] is None and row["share"] is None
    assert any(w.startswith("binanceusdm: USD open interest not reported") for w in env["warnings"])
    assert not any("estimated" in w for w in env["warnings"])
    assert env["data"]["total_oi_usd"] == pytest.approx(sum(v["oi_usd"] for v in env["data"]["venues"] if v["oi_usd"]))


async def test_c_deribit_row_is_the_perpetual_in_usd(server):
    env = await call(server, "perps_open_interest", base="BTC")
    row = _rows(env)["deribit"]
    perp = next(x for x in FUTURES if x["instrument_name"] == "BTC-PERPETUAL")
    assert row["oi_base"] is None
    assert row["oi_usd"] == pytest.approx(perp["open_interest"])  # USD on Deribit's inverse perp
    dated = {x["open_interest"] for x in FUTURES if x["instrument_name"] != "BTC-PERPETUAL"}
    assert row["oi_usd"] not in dated


async def test_c_deribit_skipped_outside_btc_eth(server):
    env = await call(server, "perps_open_interest", base="DOGE")
    assert "deribit" not in _rows(env)
    assert not any(m["provider"] == "deribit" for m in env["missing"])


async def test_d_hyperliquid_coins_and_usd_from_context(server):
    env = await call(server, "perps_open_interest", base="btc")  # lower case is upper-cased
    assert env["data"]["base"] == "BTC"
    row = _rows(env)["hyperliquid"]
    ctx = next(c for c in hl_payload()[1] if c["openInterest"] == "1500")  # the BTC row of the fake
    assert row["oi_base"] == pytest.approx(float(ctx["openInterest"]))  # coins, as reported
    assert row["oi_usd"] == pytest.approx(float(ctx["openInterest"]) * float(ctx["markPx"]))  # coins x mark


async def test_e_one_http_provider_down(state):
    env = await call(_server(state, down=("hyperliquid",)), "perps_open_interest", base="BTC")
    assert env["ok"] is True
    assert "hyperliquid" not in _rows(env)
    assert {"binanceusdm", "bybit", "deribit"} <= set(_rows(env))
    assert any(m["provider"] == "hyperliquid" for m in env["missing"])
    assert env["data"]["total_oi_usd"] == pytest.approx(sum(v["oi_usd"] for v in env["data"]["venues"] if v["oi_usd"]))
    assert sum(v["share"] for v in env["data"]["venues"] if v["share"] is not None) == pytest.approx(1.0, abs=1e-3)
    assert any(w.startswith("partial:") and "hyperliquid" in w for w in env["warnings"])


async def test_e_one_ccxt_venue_down(state):
    env = await call(_server(state, _BybitOIDownEx), "perps_open_interest", base="BTC", exchanges=["binanceusdm", "bybit"])
    assert env["ok"] is True
    rows = _rows(env)
    assert "bybit" not in rows and {"binanceusdm", "hyperliquid", "deribit"} <= set(rows)
    assert any(m["provider"] == "ccxt:bybit" for m in env["missing"])
