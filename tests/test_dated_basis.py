"""Dated basis vs realized funding: calendar, price rules, funding sums, the contract record and statistics."""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.dated_basis import (
    DAY_MS,
    HOUR_MS,
    FundingSeries,
    bar_close_at,
    basis_ann,
    binance_usdm_name,
    block_bootstrap_diff,
    bootstrap_ci,
    contract_record,
    coverage,
    deribit_name,
    mde,
    mean_ci,
    quarterly_expiries,
    ratio_from_prints,
    t_quantile,
    tercile_groups,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.health import HealthRegistry
from tradedesk.core.http import Http
from tradedesk.providers.deribit import BASE_URL, Deribit
from tradedesk.providers.exchanges import Exchanges

FIX = Path(__file__).parent / "fixtures"


def ms(y: int, m: int, d: int, h: int = 0) -> int:
    return int(datetime(y, m, d, h, tzinfo=UTC).timestamp() * 1000)


# ----------------------------------------------------------------------------- calendar


def test_quarterly_expiries_are_last_fridays_at_8utc():
    exps = quarterly_expiries(ms(2022, 1, 1), ms(2022, 12, 31))
    assert [datetime.fromtimestamp(e / 1000, UTC).strftime("%Y-%m-%d %H") for e in exps] == [
        "2022-03-25 08",
        "2022-06-24 08",
        "2022-09-30 08",
        "2022-12-30 08",
    ]
    assert deribit_name("BTC", exps[-1]) == "BTC-30DEC22"
    assert binance_usdm_name("BTC", exps[-1]) == "BTCUSDT_221230"
    assert deribit_name("eth", quarterly_expiries(ms(2019, 6, 1), ms(2019, 6, 30))[0]) == "ETH-28JUN19"


# ----------------------------------------------------------------------------- prices


def _p(t: int, price: float, index: float) -> dict[str, float]:
    return {"timestamp": t, "price": price, "index_price": index}


def test_ratio_uses_only_prints_up_to_reference_time():
    t = ms(2022, 11, 1, 8)
    prints = [_p(t - 30 * 60_000, 101.0, 100.0), _p(t - 10 * 60_000, 102.0, 100.0), _p(t - 5 * 60_000, 103.0, 100.0)]
    r = ratio_from_prints(prints, t)
    assert r is not None and r["ratio"] == pytest.approx(1.02) and r["n"] == 3 and r["age_ms"] == 5 * 60_000
    # an absurd print after t (and one before the window) changes nothing
    noisy = [*prints, _p(t + 1, 500.0, 100.0), _p(t - 2 * HOUR_MS, 1.0, 100.0)]
    assert ratio_from_prints(noisy, t) == r
    # freshest max_n only
    assert ratio_from_prints(prints, t, max_n=1)["ratio"] == pytest.approx(1.03)
    assert ratio_from_prints(prints, t - 31 * 60_000 - HOUR_MS) is None


def test_bar_close_at_picks_bar_ending_at_reference():
    t = ms(2024, 1, 1, 8)
    bars = [[t - 2 * HOUR_MS, 1, 1, 1, "10.0"], [t - HOUR_MS, 1, 1, 1, "11.0"], [t, 1, 1, 1, "12.0"]]
    assert bar_close_at(bars, t) == 11.0
    assert bar_close_at(bars[:1], t) is None


def test_basis_ann_simple_and_log():
    assert basis_ann(1.01, 73) == pytest.approx(0.05)
    assert basis_ann(1.01, 73, log=True) == pytest.approx(math.log(1.01) * 5)


# ----------------------------------------------------------------------------- funding


def test_funding_series_sums_half_open_window_and_weights():
    t0 = ms(2023, 1, 1)
    fs = FundingSeries([(t0 + k * HOUR_MS, 0.0001, 100.0 + k) for k in range(0, 49)])
    assert fs.total(t0, t0 + 24 * HOUR_MS) == pytest.approx(24 * 0.0001)  # (t0, t0+24h]: k = 1..24
    assert fs.count(t0, t0 + 24 * HOUR_MS) == 24
    assert fs.weighted(t0, t0 + 2 * HOUR_MS, 100.0) == pytest.approx(0.0001 * (101 + 102) / 100)
    assert coverage(fs, t0, t0 + 24 * HOUR_MS, HOUR_MS) == pytest.approx(1.0)
    assert coverage(fs, t0, t0 + 96 * HOUR_MS, HOUR_MS) == pytest.approx(48 / 96)
    # mixed 8h -> 1h intervals: sums run over actual events
    mixed = FundingSeries([(t0 + 8 * HOUR_MS, 0.0008), *((t0 + 8 * HOUR_MS + k * HOUR_MS, 0.0001) for k in range(1, 9))])
    assert mixed.total(t0, t0 + 16 * HOUR_MS) == pytest.approx(0.0016)


# ----------------------------------------------------------------------------- contract record


def _flat_funding(t0: int, days: int, rate_1h: float, index: float = 100.0) -> FundingSeries:
    return FundingSeries([(t0 + k * HOUR_MS, rate_1h, index) for k in range(1, days * 24 + 1)])


def test_contract_record_analytic_values():
    e = ms(2023, 3, 31, 8)
    days = 73
    t0 = e - days * DAY_MS
    fs = _flat_funding(t0, days, 0.05 / 8760)  # 5 % a year, paid hourly
    r = contract_record(
        expiry_ms=e,
        days=days,
        fut_ratio=1.02,  # 2 % over 73 days = 10 % a year
        funding=fs,
        index0=100.0,
        perp_ratio0=1.0,
        perp_ratio_exit=1.0,
        index_exit=110.0,
        delivery=110.0,
        cal_cost=0.0,
        cc_cost=0.0,
        perp_cc_cost=0.0,
    )
    assert r["basis_ann"] == pytest.approx(0.10)
    assert r["funding_ann"] == pytest.approx(0.05, rel=1e-3)
    assert r["d"] == pytest.approx(0.05, rel=1e-3)
    # calendar: (102 - 110) + (110 - 100) - funding (flat index 100) = 2 - 1 % -> per I0
    assert r["calendar"] == pytest.approx(0.02 - 0.05 * days / 365, rel=1e-3)
    assert r["dated_cc"] == pytest.approx(0.02)
    assert r["perp_cc"] == pytest.approx(0.05 * days / 365, rel=1e-3)
    assert r["calendar_ann"] == pytest.approx(r["d"], rel=1e-3)  # zero premia and costs: calendar == d
    assert r["convergence_noise"] == 0.0


def test_calendar_decomposes_into_d_and_named_terms():
    e = ms(2024, 3, 29, 8)
    days = 60
    t0 = e - days * DAY_MS
    fs = FundingSeries([(t0 + k * HOUR_MS, 0.00002, 100.0 + k / 10) for k in range(1, days * 24 + 1)])
    r = contract_record(
        expiry_ms=e,
        days=days,
        fut_ratio=1.015,
        funding=fs,
        index0=100.0,
        perp_ratio0=1.001,
        perp_ratio_exit=1.0005,
        index_exit=244.0,
        delivery=243.5,
        cal_cost=0.00175,
    )
    rebuilt = r["d"] * days / 365 - r["perp_premium0"] + r["exit_gap"] - (r["funding_weighted"] - r["funding"]) - 0.00175
    assert r["calendar"] == pytest.approx(rebuilt, abs=1e-12)
    assert r["funding_weighted"] > r["funding"]  # rising index: a fixed coin quantity pays more USD funding


def test_contract_record_ignores_funding_outside_holding_period():
    e = ms(2023, 3, 31, 8)
    days = 60
    t0 = e - days * DAY_MS
    base = [(t0 + k * HOUR_MS, 0.0001, 100.0) for k in range(1, days * 24 + 1)]
    r1 = contract_record(expiry_ms=e, days=days, fut_ratio=1.01, funding=FundingSeries(base))
    # data after expiry (or before entry) changes: results stay identical
    extra = [*base, (e + HOUR_MS, 5.0, 1.0), (t0 - HOUR_MS, -5.0, 1.0), (t0, 7.0, 1.0)]
    r2 = contract_record(expiry_ms=e, days=days, fut_ratio=1.01, funding=FundingSeries(extra))
    # prefix sums: equal up to float roundoff
    assert r2["d"] == pytest.approx(r1["d"], abs=1e-9) and r2["funding"] == pytest.approx(r1["funding"], abs=1e-12)
    assert r1["calendar"] is None and r1["perp_cc"] is None and r1["dated_cc"] == pytest.approx(0.01 - 0.00275)


# ----------------------------------------------------------------------------- statistics


def test_t_quantile_matches_tables():
    assert t_quantile(0.975, 10) == pytest.approx(2.228, abs=2e-3)
    assert t_quantile(0.975, 28) == pytest.approx(2.048, abs=1e-3)
    assert t_quantile(0.80, 28) == pytest.approx(0.855, abs=1e-3)
    assert t_quantile(0.975, 10_000) == pytest.approx(1.960, abs=1e-3)


def test_mean_ci_and_mde():
    xs = [0.01, 0.02, 0.03, 0.04, 0.05]
    s = mean_ci(xs)
    half = t_quantile(0.975, 4) * statistics.stdev(xs) / math.sqrt(5)
    assert s["mean"] == pytest.approx(0.03) and s["ci"][0] == pytest.approx(0.03 - half)
    assert s["share_pos"] == 1.0
    assert mde(0.05, 29) == pytest.approx((t_quantile(0.975, 28) + t_quantile(0.8, 28)) * 0.05 / math.sqrt(29))
    assert mde(0.05, 2) == float("inf")
    lo, hi = bootstrap_ci(xs, reps=2000)
    assert lo < 0.03 < hi


def test_terciles_and_block_bootstrap():
    vals = list(range(9))
    assert tercile_groups(vals) == [0, 0, 0, -1, -1, -1, 1, 1, 1]
    days = list(range(300))
    xs = [1.0 if d % 3 == 0 else 0.0 for d in days]
    groups = [1 if d % 3 == 0 else 0 for d in days]
    res = block_bootstrap_diff(days, xs, groups, block_days=30, reps=200)
    assert res["diff"] == pytest.approx(1.0) and res["ci"] == (pytest.approx(1.0), pytest.approx(1.0))
    assert res["n_blocks"] == 10


# ----------------------------------------------------------------------------- providers


def test_deribit_tradingview_parses_fixture():
    body = json.loads((FIX / "deribit_tradingview_expired_2026-10-14.json").read_text())
    seen: dict[str, str] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        seen["path"] = req.url.path
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": body["result"]})

    dr = Deribit(Http("deribit", BASE_URL, transport=httpx.MockTransport(h), retries=0))
    f = asyncio.run(dr.tradingview_chart_data("BTC-30DEC22", ms(2022, 9, 1), ms(2022, 12, 31), "1D"))
    assert seen["path"].endswith("/public/get_tradingview_chart_data") and seen["instrument_name"] == "BTC-30DEC22"
    assert seen["resolution"] == "1D"
    assert f.data["close"][-2] == 16472.0 and f.data["status"] == "ok"


def test_deribit_tradingview_error_maps_to_provider_error():
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": 13020, "message": "instrument_not_found"}})

    dr = Deribit(Http("deribit", BASE_URL, transport=httpx.MockTransport(h), retries=0))
    with pytest.raises(ProviderError) as e:
        asyncio.run(dr.tradingview_chart_data("BTC-1JAN99", 0, 1))
    assert e.value.reason == "data_unavailable"


class _FakeBinance:
    def __init__(self, ex_id: str) -> None:
        self.calls: list[dict] = []
        self.blob = json.loads((FIX / "binance_delivery_klines_2026-10-14.json").read_text())

    async def dapiPublicGetContinuousKlines(self, params):  # noqa: N802 - ccxt implicit name
        self.calls.append(params)
        return self.blob["continuous_current_quarter"]

    async def privatePostOrder(self, params):  # noqa: N802 - must never be reachable
        raise AssertionError("private endpoint called")


def test_exchanges_public_get_only_public_endpoints():
    ex = Exchanges(HealthRegistry(), factory=_FakeBinance)
    f = asyncio.run(ex.public_get("binance", "dapiPublicGetContinuousKlines", {"pair": "BTCUSD", "contractType": "CURRENT_QUARTER"}))
    assert f.data[0][4] == "9298.6" and f.evidence.provider == "ccxt:binance"
    with pytest.raises(ProviderError) as e:
        asyncio.run(ex.public_get("binance", "privatePostOrder", {}))
    assert e.value.reason == "config"
    with pytest.raises(ProviderError) as e2:
        asyncio.run(ex.public_get("binance", "fapiPublicGetNoSuchThing", {}))
    assert e2.value.reason == "data_unavailable"
