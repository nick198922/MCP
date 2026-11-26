"""Bybit funding intervals in `current_funding` (standing card item 461).

Paper carry B had 9 incomplete cycles flagged `provider_missing` [`funding_intervals:bybit`] (reason data_unavailable).
`current_funding` asked for the venue's interval list as soon as *any* bulk row lacked an interval. ccxt's Bybit rows take
the interval from the market list loaded at the process start, so a perp listed after that comes back keyed by its raw id
("TJXUSDT") without one; that row is skipped anyway, and ccxt has no Bybit interval list (``has`` None, the base method
raises NotSupported), so the request could only fail. Now only a used row without an interval asks, Bybit's own
``info.fundingIntervalHour`` in the same ticker row fills the interval, and a venue without an interval list is not
asked: a used symbol left without one is named in a warning (interval_source "assumed").

The Bybit rows are a real sample (tests/fixtures/bybit_funding_rates_stale_markets_2026-11-25.json); the venue classes are
ccxt's own (their ``has`` and base methods), only the bulk call is replaced, so nothing here touches the network.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import ccxt
import ccxt.async_support as ca
import pytest

from tradedesk.core.envelope import Builder
from tradedesk.core.health import HealthRegistry
from tradedesk.providers.exchanges import Exchanges
from tradedesk.tools.perps import current_funding

SAMPLE = json.loads((Path(__file__).parent / "fixtures/bybit_funding_rates_stale_markets_2026-11-25.json").read_text())["rows"]


class FakeBybit(ca.bybit):
    """ccxt's Bybit (``has["fetchFundingIntervals"]`` None, no own ``fetch_funding_intervals``) with a canned bulk answer."""

    rows: dict = SAMPLE

    async def fetch_funding_rates(self, symbols=None, params=None):
        return copy.deepcopy(type(self).rows)


class FakeBinance(ca.binanceusdm):
    """ccxt's Binance USD-M: the bulk rows have no interval, fundingInfo (``fetch_funding_intervals``) lists them."""

    intervals_error: Exception | None = None

    async def fetch_funding_rates(self, symbols=None, params=None):
        return {s: {"symbol": s, "fundingRate": 0.0001, "interval": None, "markPrice": 1.0} for s in ("BTC/USDT:USDT", "WIF/USDT:USDT")}

    async def fetch_funding_intervals(self, symbols=None, params=None):
        if type(self).intervals_error is not None:
            raise type(self).intervals_error
        return {"WIF/USDT:USDT": {"symbol": "WIF/USDT:USDT", "interval": "4h", "info": {"adjustedFundingRateCap": "0.02"}}}


def _rt(**classes) -> SimpleNamespace:
    return SimpleNamespace(exchanges=Exchanges(HealthRegistry(), factory=lambda ex_id: classes[ex_id](), retries=0))


async def _run(rt: SimpleNamespace, venue: str) -> tuple[dict, Builder]:
    b = Builder()
    try:
        return await current_funding(rt, b, venue), b
    finally:
        await rt.exchanges.aclose()


def test_only_venues_with_an_interval_list_are_asked():
    ex = Exchanges(HealthRegistry(), factory=lambda ex_id: getattr(ca, ex_id)())
    assert ex.get("bybit").has.get("fetchFundingIntervals") is None  # ccxt says nothing: the base method raises
    assert ex.lists_funding_intervals("binanceusdm") is True
    assert ex.lists_funding_intervals("bybit") is False
    assert ex.lists_funding_intervals("okx") is False
    assert ex.lists_funding_intervals("no-such-venue") is False  # the factory raises ProviderError: still an answer


async def test_bybit_row_of_a_perp_missing_from_the_market_list_requests_nothing():
    cur, b = await _run(_rt(bybit=FakeBybit), "bybit")
    assert b.missing == [] and b.warnings == []  # before: missing funding_intervals:bybit (data_unavailable) + an 8h warning
    assert set(cur) == {"BTC/USDT:USDT", "SOL/USDT:USDT"}  # the raw-id row is not a used symbol
    assert all(x["interval_hours"] == 8.0 and x["interval_source"] == "venue" for x in cur.values())


async def test_bybit_interval_falls_back_to_the_ticker_field():
    class FakeSol(FakeBybit):
        rows = copy.deepcopy(SAMPLE)

    FakeSol.rows["SOL/USDT:USDT"]["interval"] = None
    FakeSol.rows["SOL/USDT:USDT"]["info"]["fundingIntervalHour"] = "4"
    cur, b = await _run(_rt(bybit=FakeSol), "bybit")
    assert b.missing == [] and b.warnings == []
    assert cur["SOL/USDT:USDT"]["interval_hours"] == 4.0 and cur["SOL/USDT:USDT"]["interval_source"] == "venue"
    assert cur["SOL/USDT:USDT"]["apr"] == pytest.approx(-7.83e-06 * 2190)


@pytest.mark.parametrize(("hour", "want"), [("1", 1.0), (2, 2.0), ("0.5", 0.5), ("24", 24.0)])
async def test_bybit_ticker_interval_is_taken_as_hours(hour, want):
    class FakeSol(FakeBybit):
        rows = copy.deepcopy(SAMPLE)

    FakeSol.rows["SOL/USDT:USDT"]["interval"] = None
    FakeSol.rows["SOL/USDT:USDT"]["info"]["fundingIntervalHour"] = hour
    cur, b = await _run(_rt(bybit=FakeSol), "bybit")
    assert (cur["SOL/USDT:USDT"]["interval_hours"], cur["SOL/USDT:USDT"]["interval_source"]) == (want, "venue")


@pytest.mark.parametrize("hour", [None, "", "0", "abc", "nan", True, "1e-05", "1e+08", 1e-05, "48"])
async def test_bybit_used_perp_without_any_interval_stays_visible(hour):
    class FakeSol(FakeBybit):
        rows = copy.deepcopy(SAMPLE)

    FakeSol.rows["SOL/USDT:USDT"]["interval"] = None
    FakeSol.rows["SOL/USDT:USDT"]["info"]["fundingIntervalHour"] = hour
    cur, b = await _run(_rt(bybit=FakeSol), "bybit")
    assert b.missing == []  # no request that cannot succeed
    sol = cur["SOL/USDT:USDT"]
    assert sol["interval_source"] == "assumed" and sol["interval_hours"] == 8.0
    assert b.warnings == ["bybit: funding interval not reported for 1 symbols, 8h shown (interval_source=assumed): SOL/USDT:USDT"]
    assert cur["BTC/USDT:USDT"]["interval_source"] == "venue"


async def test_binance_interval_list_is_still_read():
    cur, b = await _run(_rt(binanceusdm=FakeBinance), "binanceusdm")
    assert b.missing == [] and b.warnings == []
    wif = cur["WIF/USDT:USDT"]
    assert (wif["interval_hours"], wif["interval_source"], wif["cap"]) == (4.0, "venue", 0.02)
    assert (cur["BTC/USDT:USDT"]["interval_hours"], cur["BTC/USDT:USDT"]["interval_source"]) == (8.0, "default")


FAILURES = [(ccxt.NetworkError("fundingInfo down"), "network"), (ccxt.ExchangeError("bad"), "backend_error")]


@pytest.mark.parametrize(("error", "reason"), FAILURES)
async def test_binance_interval_list_failure_is_still_reported(error, reason):
    class FakeDown(FakeBinance):
        intervals_error = error

    cur, b = await _run(_rt(binanceusdm=FakeDown), "binanceusdm")
    assert [(m.field, m.provider, m.reason) for m in b.missing] == [("funding_intervals:binanceusdm", "ccxt:binanceusdm", reason)]
    assert b.warnings == ["binanceusdm: funding intervals unavailable; 8h shown (interval_source=assumed) where the venue omits it"]
    assert {x["interval_source"] for x in cur.values()} == {"assumed"}
