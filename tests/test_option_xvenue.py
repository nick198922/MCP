"""Deribit <-> Bybit daily option quotes: pure crossing measure (analytics/option_xvenue.py), the table
option_xvenue_snapshot and the collector job `option_xvenue` (IDEA-20261023-bybit-deribit-daily-option-dislocation-forward)."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import ccxt
import httpx
import pytest

from conftest import FakeEx, State, make_transports
from tradedesk.analytics.option_xvenue import (
    FEE_RATE,
    MIN_DEPTH,
    MINUTE_WINDOW_SECONDS,
    REG_GAP_SOURCE,
    REG_STRIKES_EACH_SIDE,
    REG_SYNC_MS,
    REG_THRESHOLD,
    adjusted_crossing_from_row,
    atm_strikes,
    bybit_quote,
    crossing,
    crossing_from_row,
    deribit_quote,
    extend_windows,
    parse_bybit_symbol,
    pick_daily_expiry,
    plan_tick,
    registered_rows,
    seed_schedule,
    snapshot_crossings,
    sync_ok,
)
from tradedesk.collector.option_xvenue import OptionXvenueJob, _received, collect_option_xvenue, scan_option_xvenue
from tradedesk.collector.scheduler import _retention_sweep, build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder, Evidence, Fetched
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

FIXTURE = Path(__file__).parent / "fixtures" / "option_xvenue_btc_2026-10-23.json"
SAMPLE = json.loads(FIXTURE.read_text())
NOW = datetime(2026, 10, 23, 2, 58, 20, tzinfo=UTC)  # the fixture's read time
EXP = datetime(2026, 10, 23, 8, tzinfo=UTC)
ATM = [80500.0, 81000.0, 81500.0, 82000.0, 82500.0, 83000.0, 83500.0]  # Deribit forward 81856.69 -> 82000 +- 3 x 500

# ----------------------------------------------------------------------------- pure functions


def test_crossing_hand_computed():
    # spot 100 000, fee 0.03 % per venue -> 2 x 30 = 60 USD per 1 BTC
    c = crossing(
        d_bid=900.0, d_ask=1000.0, d_bid_size=1.0, d_ask_size=0.5,
        b_bid=1200.0, b_ask=1250.0, b_bid_size=0.2, b_ask_size=1.0, spot=100_000.0,
    )  # fmt: skip
    bd, db = c["b_over_d"], c["d_over_b"]
    assert bd["gross_usd"] == pytest.approx(200.0) and bd["net_usd"] == pytest.approx(140.0)
    assert bd["net_frac"] == pytest.approx(0.0014) and bd["depth"] == pytest.approx(0.2) and bd["depth_ok"]
    assert db["gross_usd"] == pytest.approx(-350.0) and db["net_frac"] == pytest.approx(-0.0041) and db["depth_ok"]
    assert c["best"] == pytest.approx(0.0014)
    assert FEE_RATE == 0.0003 and MIN_DEPTH == 0.1


def test_crossing_depth_filter_missing_quote_and_bad_spot():
    thin = crossing(
        d_bid=900.0, d_ask=1000.0, d_bid_size=1.0, d_ask_size=0.5,
        b_bid=1200.0, b_ask=1250.0, b_bid_size=0.05, b_ask_size=1.0, spot=100_000.0,
    )  # fmt: skip
    assert thin["b_over_d"]["depth_ok"] is False and thin["best"] == pytest.approx(-0.0041)  # only the other side counts
    one_side = crossing(
        d_bid=None, d_ask=1000.0, d_bid_size=None, d_ask_size=0.5,
        b_bid=1200.0, b_ask=None, b_bid_size=0.2, b_ask_size=None, spot=100_000.0, fee_rate=0.0,
    )  # fmt: skip
    assert one_side["d_over_b"] is None and one_side["b_over_d"]["net_frac"] == pytest.approx(0.002)
    with pytest.raises(ValueError):
        crossing(d_bid=1, d_ask=1, d_bid_size=1, d_ask_size=1, b_bid=1, b_ask=1, b_bid_size=1, b_ask_size=1, spot=0.0)


def test_crossing_from_row_converts_deribit_coin_prices():
    row = {
        "d_bid": 0.009, "d_ask": 0.010, "d_bid_size": 1.0, "d_ask_size": 0.5, "d_index": 100_000.0, "d_ts_ms": 1_000,
        "b_bid": 1200.0, "b_ask": 1250.0, "b_bid_size": 0.2, "b_ask_size": 1.0, "b_index": 100_150.0, "b_ts_ms": 1_800,
    }  # fmt: skip
    c = crossing_from_row(row)
    assert c["b_over_d"]["net_frac"] == pytest.approx(0.0014) and c["d_over_b"]["net_frac"] == pytest.approx(-0.0041)
    assert c["index_gap"] == pytest.approx(0.0015) and c["dt_ms"] == 800
    assert crossing_from_row(dict(row, d_index=None)) is None
    assert crossing_from_row(dict(row, b_index=None, b_ts_ms=None))["index_gap"] is None


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _call(F: float, K: float, T: float, s: float) -> float:
    d1 = (math.log(F / K) + 0.5 * s * s * T) / (s * math.sqrt(T))
    return F * _ncdf(d1) - K * _ncdf(d1 - s * math.sqrt(T))


def test_adjusted_crossing_shift_is_delta_times_gap():
    # ATM call, F = K = 100 000, tau 10 h, IV 40 %: d1 = sigma sqrt(T) / 2, delta = N(d1); gap +0.15 %
    row = {
        "d_bid": 0.009, "d_ask": 0.010, "d_bid_size": 1.0, "d_ask_size": 1.0, "d_index": 100_000.0,
        "d_underlying": 100_000.0, "d_mark_iv": 0.40, "d_ts_ms": 1_000, "strike": 100_000.0, "tau_hours": 10.0,
        "opt": "C", "b_bid": 1200.0, "b_ask": 1250.0, "b_bid_size": 1.0, "b_ask_size": 1.0, "b_index": 100_150.0,
        "b_ts_ms": 4_000,
    }  # fmt: skip
    delta = _ncdf(0.5 * 0.40 * math.sqrt(10.0 / 8760.0))
    a = adjusted_crossing_from_row(row)
    assert a["gap"] == pytest.approx(0.0015) and a["delta"] == pytest.approx(delta) and a["dt_ms"] == 3_000
    assert a["shift_usd"] == pytest.approx(-delta * 0.0015 * 100_000.0)  # ~ -75.4: Bybit call made cheaper
    assert a["b_over_d"]["gross_usd"] == pytest.approx(1200.0 + a["shift_usd"] - 1000.0)
    assert a["b_over_d"]["net_usd"] == pytest.approx(a["b_over_d"]["gross_usd"] - 60.0)  # 2 x 0.03 % x 100 000
    put = adjusted_crossing_from_row(dict(row, opt="P"))
    assert put["delta"] == pytest.approx(delta - 1.0) and put["shift_usd"] > 0  # Bybit put made dearer
    fees = adjusted_crossing_from_row(row, fee_deribit=0.0003, fee_bybit=0.0007)
    assert fees["b_over_d"]["net_usd"] == pytest.approx(a["b_over_d"]["gross_usd"] - 100.0)  # per-venue fees
    raw = adjusted_crossing_from_row(dict(row, d_mark_iv=None), gap_source="none")
    assert raw["shift_usd"] == 0.0 and raw["b_over_d"]["gross_usd"] == pytest.approx(200.0)
    assert raw["b_over_d"]["net_frac"] == pytest.approx(crossing_from_row(row)["b_over_d"]["net_frac"])


def test_adjusted_crossing_removes_a_pure_settlement_basis():
    # Bybit quoted exactly at its fair USDT price (1 + g) C(F, K / (1 + g)), Deribit at C(F, K), no spread, no fees:
    # the raw comparison shows a "crossing" of ~ g F delta, the adjusted one is ~ 0 (second order only).
    F, T, s, g = 100_000.0, 10.0 / 8760.0, 0.40, 0.0015
    for K in (99_000.0, 100_000.0, 101_000.0):
        d = _call(F, K, T, s)
        b = (1.0 + g) * _call(F, K / (1.0 + g), T, s)
        row = {
            "d_bid": d / F, "d_ask": d / F, "d_bid_size": 1.0, "d_ask_size": 1.0, "d_index": F, "d_underlying": F,
            "d_mark_iv": s, "strike": K, "tau_hours": 10.0, "opt": "C", "b_bid": b, "b_ask": b, "b_bid_size": 1.0,
            "b_ask_size": 1.0, "b_index": F * (1.0 + g),
        }  # fmt: skip
        raw = adjusted_crossing_from_row(row, gap_source="none", fee_deribit=0.0, fee_bybit=0.0)
        adj = adjusted_crossing_from_row(row, fee_deribit=0.0, fee_bybit=0.0)
        assert raw["b_over_d"]["gross_usd"] > 20.0  # the basis looks like a dislocation
        # gone up to second order: 0.5 g^2 K^2 C_KK = 0.5 g^2 K phi(d2) / (sigma sqrt T), ~ $3.3 at the money
        d2 = (math.log(F / K) - 0.5 * s * s * T) / (s * math.sqrt(T))
        second = 0.5 * g * g * K * math.exp(-0.5 * d2 * d2) / math.sqrt(2.0 * math.pi) / (s * math.sqrt(T))
        assert 1.0 < second < 4.0 and adj["b_over_d"]["gross_usd"] == pytest.approx(second, abs=0.3)


def test_adjusted_crossing_unmeasurable_rows():
    row = {"d_index": 100_000.0, "d_underlying": 100_100.0, "b_underlying": 100_250.0, "b_index": 100_150.0,
           "d_mark_iv": 0.4, "strike": 100_000.0, "tau_hours": 5.0, "opt": "C", "d_ask": 0.01, "b_bid": 1000.0}  # fmt: skip
    u = adjusted_crossing_from_row(row, gap_source="underlying")
    assert u["gap"] == pytest.approx(100_250.0 / 100_100.0 - 1.0) and u["b_over_d"]["gross_usd"] is not None
    assert adjusted_crossing_from_row(dict(row, d_index=None)) is None
    assert adjusted_crossing_from_row(dict(row, d_mark_iv=None)) is None  # no IV on either venue -> no delta
    assert adjusted_crossing_from_row(dict(row, d_mark_iv=None, b_mark_iv=0.5))["delta"] is not None  # Bybit IV fallback
    assert adjusted_crossing_from_row(dict(row, b_index=None)) is None
    assert adjusted_crossing_from_row(dict(row, opt="X")) is None
    with pytest.raises(ValueError):
        adjusted_crossing_from_row(row, gap_source="mid")
    assert (REG_THRESHOLD, REG_STRIKES_EACH_SIDE, REG_SYNC_MS, REG_GAP_SOURCE) == (0.0015, 1, 5_000, "index")


def test_parse_bybit_symbol():
    p = parse_bybit_symbol("BTC-23OCT26-82000-C-USDT")
    assert p == {"base": "BTC", "expiry": EXP, "strike": 82000.0, "is_call": True, "settle": "USDT"}
    q = parse_bybit_symbol("ETH-3NOV26-2400-P")
    assert q["settle"] is None and q["is_call"] is False and q["expiry"] == datetime(2026, 11, 3, 8, tzinfo=UTC)
    assert parse_bybit_symbol("BTCUSDT") is None and parse_bybit_symbol("BTC-23OCT26-82000-X-USDT") is None


def test_pick_daily_expiry_window():
    exps = [EXP, datetime(2026, 10, 24, 8, tzinfo=UTC), datetime(2026, 10, 30, 8, tzinfo=UTC)]
    assert pick_daily_expiry(exps, NOW) == EXP  # 5 h away
    assert pick_daily_expiry(exps, datetime(2026, 10, 23, 7, tzinfo=UTC)) is None  # 1 h and 25 h: both outside 2-20 h
    assert pick_daily_expiry(exps, datetime(2026, 10, 23, 12, tzinfo=UTC)) == exps[1]  # exactly 20 h: inside
    assert pick_daily_expiry([], NOW) is None


def test_atm_strikes():
    ks = [70_000.0 + 500 * i for i in range(40)]
    assert atm_strikes(ks, 81_856.69) == ATM
    assert atm_strikes(ks, 81_750.0) == [80_000.0 + 500 * i for i in range(7)]  # tie 81 500 / 82 000 -> the lower
    assert atm_strikes(ks, 60_000.0) == ks[:4] and atm_strikes([], 1.0) == []


def test_quotes_from_the_real_sample():
    d = deribit_quote(SAMPLE["deribit_ticker"]["BTC-23OCT26-82000-C"])
    assert d["bid"] == 0.0013 and d["ask"] == 0.0016 and d["bid_size"] == 2.1 and d["ask_size"] == 1.2
    assert d["bid_iv"] == pytest.approx(0.215) and d["mark_iv"] == pytest.approx(0.2399) and d["index"] == 81847.32
    row = next(r for r in SAMPLE["bybit"]["result"]["list"] if r["symbol"] == "BTC-23OCT26-82000-C-USDT")
    b = bybit_quote(row, 7)
    assert b["bid"] == 180.0 and b["ask"] == 185.0 and b["bid_size"] == 0.26 and b["bid_iv"] == 0.2385 and b["ts_ms"] == 7
    empty = deribit_quote(SAMPLE["deribit_ticker"]["BTC-23OCT26-83000-C"])  # no bid on Deribit: 0 -> None
    assert empty["bid"] is None and empty["bid_size"] is None and empty["ask"] is not None


# ----------------------------------------------------------------------------- collector job


def _summary(fwd_shift: float = 0.0) -> list[dict]:
    """Book summary: the sample's 23OCT26 strikes plus far strikes and a next-day expiry the job must ignore."""
    fwd = SAMPLE["deribit_ticker"]["BTC-23OCT26-82000-C"]["underlying_price"] + fwd_shift
    names = list(SAMPLE["deribit_ticker"])
    names += [f"BTC-23OCT26-{k}-{t}" for k in (78000, 79000, 80000, 84000, 85000) for t in "CP"]
    names += [f"BTC-24OCT26-{k}-{t}" for k in (81000, 82000) for t in "CP"]
    return [{"instrument_name": n, "underlying_price": fwd, "estimated_delivery_price": 81847.32} for n in names]


def _rt(
    tmp_path, *, deribit_down: bool = False, empty_summary: bool = False, bybit: str = "ok", opts: dict | None = None, **kw
) -> tuple[Runtime, list]:
    """Fake venues. ``opts`` (mutable, read on every request) lets a test change "bybit" / "deribit_down" / "fwd_shift"
    (USD added to Deribit's forward in the summary and the tickers) between ticks."""
    calls: list = []
    opts = opts if opts is not None else {}
    opts.setdefault("bybit", bybit)
    opts.setdefault("deribit_down", deribit_down)
    opts.setdefault("fwd_shift", 0.0)

    def deribit(req: httpx.Request) -> httpx.Response:
        calls.append(("deribit", req.url.path.rsplit("/", 1)[-1], req.url.params.get("instrument_name") or req.url.params.get("currency")))
        if opts["deribit_down"]:
            return httpx.Response(500, text="down")
        if req.url.path.endswith("get_book_summary_by_currency"):
            far = [{"instrument_name": "ETH-30OCT26-2400-C", "underlying_price": 2412.5}]  # ETH: no daily expiry in the fake
            res = ([] if empty_summary else _summary(opts["fwd_shift"])) if req.url.params["currency"] == "BTC" else far
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": res})
        name = req.url.params.get("instrument_name")
        if req.url.path.endswith("/ticker") and name in SAMPLE["deribit_ticker"]:
            t = SAMPLE["deribit_ticker"][name]
            t = dict(t, underlying_price=t["underlying_price"] + opts["fwd_shift"])
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": t})
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": 13020, "message": "instrument_not_found"}})

    class Ex(FakeEx):
        async def publicGetV5MarketTickers(self, params):  # noqa: N802 - ccxt implicit endpoint name
            calls.append(("bybit", params["category"], params["baseCoin"]))
            bybit = opts["bybit"]
            if bybit == "down":
                raise ccxt.NetworkError("bybit unreachable")
            body = json.loads(json.dumps(SAMPLE["bybit"]))
            if params["baseCoin"] != "BTC" or bybit == "no_daily":
                body["result"]["list"] = [r for r in body["result"]["list"] if params["baseCoin"] == "BTC" and "23OCT26" not in r["symbol"]]
            if bybit == "cross":  # Bybit bids 1000 USD for 1 BTC of the ATM call Deribit offers at ~131: a registered crossing
                for r in body["result"]["list"]:
                    if r["symbol"] == "BTC-23OCT26-82000-C-USDT":
                        r.update(bid1Price="1000", bid1Size="1")
            if bybit == "usdc_dup":  # the same strikes listed again as USDC-settled (no suffix), after the USDT rows
                dup = [dict(r, symbol=r["symbol"].removesuffix("-USDT"), bid1Price="1") for r in body["result"]["list"]]
                body["result"]["list"] += dup
            return body

    tr = make_transports(State())
    tr["deribit"] = httpx.MockTransport(deribit)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite"), **kw), transports=tr, exchange_factory=Ex)
    rt.now = lambda: NOW
    rt.deribit.http._limiter = TokenBucket(1_000.0, 1_000)
    rt.deribit.http._backoff = 0.0
    return rt, calls


async def test_job_stores_both_legs_of_the_atm_strikes(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_option_xvenue(rt) == 14  # 7 strikes x C/P, BTC only (ETH: no daily expiry in the fake)
        rows = await rt.store.option_xvenue_rows()
        assert sorted({r["strike"] for r in rows}) == ATM and {r["opt"] for r in rows} == {"C", "P"}
        assert all(r["currency"] == "BTC" and r["expiry_ts"] == int(EXP.timestamp()) for r in rows)
        r = next(r for r in rows if r["strike"] == 82000.0 and r["opt"] == "C")
        assert r["d_instrument"] == "BTC-23OCT26-82000-C" and r["b_symbol"] == "BTC-23OCT26-82000-C-USDT"
        assert (r["d_bid"], r["d_ask"], r["d_bid_size"], r["d_ask_size"]) == (0.0013, 0.0016, 2.1, 1.2)
        assert (r["b_bid"], r["b_ask"], r["b_bid_size"], r["b_ask_size"]) == (180.0, 185.0, 0.26, 12.09)
        assert r["d_index"] == 81847.32 and r["b_index"] == pytest.approx(81966.494, abs=1e-3)
        assert r["d_ts_ms"] == 1792724299253 and r["b_ts_ms"] == SAMPLE["bybit"]["time"]
        assert r["tau_hours"] == pytest.approx(5.0278, abs=1e-3) and r["ts"] == int(NOW.timestamp())
        assert r["d_mark_iv"] == pytest.approx(0.2399) and r["b_mark_iv"] == pytest.approx(0.2549)
        assert {r["d_instrument"] for r in rows if r["reg_atm"] == 1} == ATM1 and sum(r["reg_atm"] for r in rows) == 6
        assert r["d_recv_ms"] == r["b_recv_ms"] == int(NOW.timestamp() * 1000)  # the fake clock does not move
        c = crossing_from_row(r)  # live sample: no crossing; Bybit's index 0.15 % above Deribit's
        assert c["best"] < 0 and c["index_gap"] == pytest.approx(0.001456, abs=1e-5) and c["dt_ms"] == 2205
        a = adjusted_crossing_from_row(r)  # registered measure: the call's basis (~ delta x gap x F) taken off Bybit
        assert 0.3 < a["delta"] < 0.45 and a["shift_usd"] < 0
        assert a["b_over_d"]["gross_usd"] == pytest.approx(c["b_over_d"]["gross_usd"] + a["shift_usd"])
        tickers = [x[2] for x in calls if x[1] == "ticker"]
        assert len(tickers) == 14 and all("23OCT26" in n for n in tickers)  # only the ATM +- 3 of the daily expiry
        assert [x for x in calls if x[0] == "bybit"] == [("bybit", "option", "BTC")]  # ETH: no expiry, Bybit not asked
    finally:
        await rt.aclose()


async def test_bybit_down_keeps_deribit_and_never_raises(tmp_path):
    rt, _ = _rt(tmp_path, bybit="down")
    try:
        b = Builder()
        rows = await scan_option_xvenue(rt, b)
        assert len(rows) == 14 and all(r["b_bid"] is None and r["d_index"] for r in rows)
        assert [m.field for m in b.missing] == ["bybit_options:BTC"] and b.missing[0].reason == "network"
        assert await collect_option_xvenue(rt) == 14  # partial snapshot is stored, job does not fail
    finally:
        await rt.aclose()


async def test_row_ts_and_tau_share_one_clock_read(tmp_path):
    rt, _ = _rt(tmp_path)
    ticks = iter(range(100))
    rt.now = lambda: NOW + timedelta(seconds=30 * next(ticks))  # every clock read 30 s later
    try:
        assert await collect_option_xvenue(rt) == 14
        rows = await rt.store.option_xvenue_rows()
        assert {r["ts"] for r in rows} == {int(NOW.timestamp())}  # the first read
        assert all(r["tau_hours"] == pytest.approx((r["expiry_ts"] - r["ts"]) / 3600.0, abs=1e-9) for r in rows)
    finally:
        await rt.aclose()


async def test_bybit_other_settle_is_skipped_with_a_warning(tmp_path):
    rt, _ = _rt(tmp_path, bybit="usdc_dup")
    try:
        b = Builder()
        rows = await scan_option_xvenue(rt, b)
        r = next(r for r in rows if r["strike"] == 82000.0 and r["opt"] == "C")
        assert r["b_symbol"] == "BTC-23OCT26-82000-C-USDT" and r["b_bid"] == 180.0  # USDT row kept, not overwritten
        assert all(r["b_symbol"] is None or r["b_symbol"].endswith("-USDT") for r in rows)
        want = "BTC: Bybit option symbols with settle suffix none (USDC) skipped (only USDT is stored)"
        assert [w for w in b.warnings if "settle" in w] == [want]
        assert not b.missing
    finally:
        await rt.aclose()


async def test_bybit_without_daily_expiry_is_missing_not_a_crash(tmp_path):
    rt, _ = _rt(tmp_path, bybit="no_daily")
    try:
        b = Builder()
        rows = await scan_option_xvenue(rt, b)
        assert len(rows) == 14 and all(r["b_symbol"] is None for r in rows)
        assert [m.field for m in b.missing] == ["bybit_expiry:BTC"] and b.missing[0].reason == "data_unavailable"
    finally:
        await rt.aclose()


async def test_deribit_down_falls_back_to_the_bybit_chain(tmp_path):
    rt, _ = _rt(tmp_path, deribit_down=True)
    try:
        b = Builder()
        rows = await scan_option_xvenue(rt, b)
        # Bybit underlying 81 986 on its 250-step grid -> 82 000 and three neighbours each side
        assert sorted({r["strike"] for r in rows}) == [81250.0, 81500.0, 81750.0, 82000.0, 82250.0, 82500.0, 82750.0]
        assert all(r["d_bid"] is None and r["d_instrument"] is None and r["b_ts_ms"] for r in rows)
        assert {m.field for m in b.missing} == {"deribit_options:BTC", "deribit_options:ETH"}
    finally:
        await rt.aclose()


async def test_empty_deribit_summary_is_missing_not_outside_the_window(tmp_path):
    rt, _ = _rt(tmp_path, empty_summary=True)
    try:
        b = Builder()
        rows = await scan_option_xvenue(rt, b)
        assert [(m.field, m.reason) for m in b.missing] == [("deribit_options:BTC", "data_unavailable")]
        assert len(rows) == 14 and all(r["d_instrument"] is None and r["b_symbol"] for r in rows)  # Bybit chain used
    finally:
        await rt.aclose()


async def test_both_venues_down_scan_never_raises_job_reports(tmp_path):
    rt, _ = _rt(tmp_path, deribit_down=True, bybit="down")
    try:
        b = Builder()
        assert await scan_option_xvenue(rt, b) == []
        assert {m.field for m in b.missing} == {"deribit_options:BTC", "deribit_options:ETH", "bybit_options:BTC", "bybit_options:ETH"}
        with pytest.raises(RuntimeError, match=r"option_xvenue \(hourly\): no rows"):  # the scheduler records it as a failed run
            await collect_option_xvenue(rt)
    finally:
        await rt.aclose()


async def test_outside_the_window_writes_nothing_quietly(tmp_path):
    rt, calls = _rt(tmp_path)
    rt.now = lambda: datetime(2026, 10, 23, 7, tzinfo=UTC)  # today's expiry 1 h away, tomorrow's 25 h
    try:
        assert await collect_option_xvenue(rt) == 0
        assert not [x for x in calls if x[0] == "bybit" or x[1] == "ticker"]
    finally:
        await rt.aclose()


async def test_retention_prunes_old_snapshots(tmp_path):
    rt, _ = _rt(tmp_path, retention_option_xvenue_days=30)
    try:
        await rt.store.init()
        old = int(NOW.timestamp()) - 31 * 86_400
        row = {"currency": "BTC", "expiry_ts": 0, "tau_hours": 5.0, "strike": 1.0, "opt": "C"}
        assert await rt.store.insert_option_xvenue(old, [row]) == 1
        assert await rt.store.insert_option_xvenue(int(NOW.timestamp()), [row]) == 1
        await _retention_sweep(rt)
        assert [r["ts"] for r in await rt.store.option_xvenue_rows()] == [int(NOW.timestamp())]
    finally:
        await rt.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    assert Settings().collect_option_xvenue_enabled is False and Settings().retention_option_xvenue_days == 365
    assert allowed("TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED") and allowed("TRADEDESK_COLLECT_OPTION_XVENUE_SECONDS")
    assert allowed("TRADEDESK_RETENTION_OPTION_XVENUE_DAYS")
    monkeypatch.setenv("TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED", "1")
    assert Settings.from_env().collect_option_xvenue_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_option_xvenue_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "option_xvenue" not in names(off) and "option_xvenue" in names(on)
    job = next(j for j in build_default_scheduler(on).jobs if j.name == "option_xvenue")
    assert job.period_seconds == 60 and isinstance(job.fn, OptionXvenueJob) and job.fn.hourly_seconds == 3600


# ----------------------------------------------------------------------------- minute mode and leg sync

ATM1 = {f"BTC-23OCT26-{k}-{t}" for k in (81500, 82000, 82500) for t in "CP"}  # registered ATM +- 1 around 81 856.69
ATM2 = {f"BTC-23OCT26-{k}-{t}" for k in (81000, 81500, 82000, 82500, 83000) for t in "CP"}  # minute-mode read


def _xrow(strike: float, *, dt_ms: int = 1_000, b_bid: float = 1300.0, **kw) -> dict:
    """A stored row, no index gap (shift 0): Bybit bid 1300 vs Deribit ask 1000 USD -> net 300 - 60 = +0.24 % of spot,
    a registered crossing on b_over_d; Bybit ask 1350 vs Deribit bid 900 -> no crossing on d_over_b."""
    row = {
        "ts": 1_000, "mode": "hourly", "currency": "BTC", "strike": strike, "opt": "C", "tau_hours": 10.0,
        "d_instrument": f"BTC-X-{strike:g}-C", "d_bid": 0.009, "d_ask": 0.0100, "d_bid_size": 1.0, "d_ask_size": 1.0,
        "d_index": 100_000.0, "d_underlying": 100_000.0, "d_mark_iv": 0.40, "d_ts_ms": 10_000,
        "b_bid": b_bid, "b_ask": 1350.0, "b_bid_size": 1.0, "b_ask_size": 1.0, "b_index": 100_000.0,
        "b_ts_ms": 10_000 + dt_ms,
    }  # fmt: skip
    return row | kw


def test_registered_rows_sync_class_and_snapshot_crossings():
    rows = [_xrow(k, b_bid=0.0) for k in (98_500.0, 99_000.0, 99_500.0, 100_000.0, 100_500.0, 101_000.0, 101_500.0)]
    assert sorted(r["strike"] for r in registered_rows(rows)) == [99_500.0, 100_000.0, 100_500.0]
    assert registered_rows([dict(r, d_underlying=None) for r in rows]) == []  # no Deribit forward -> nothing measurable
    assert snapshot_crossings(rows) == []  # Bybit bid 0 -> no quote -> no crossing
    assert sync_ok(_xrow(1.0, dt_ms=REG_SYNC_MS)) and not sync_ok(_xrow(1.0, dt_ms=-REG_SYNC_MS - 1))
    assert not sync_ok(_xrow(1.0, b_ts_ms=None))
    hit = [dict(r, b_bid=1300.0) if r["strike"] == 100_000.0 else r for r in rows]
    assert snapshot_crossings(hit) == [("BTC", 100_000.0, "C", "b_over_d")]
    far = [dict(r, b_bid=1300.0) if r["strike"] == 101_500.0 else r for r in rows]
    assert snapshot_crossings(far) == []  # ATM + 3: collected, but not the registered ATM +- 1
    slow = [dict(r, b_ts_ms=r["d_ts_ms"] + 5_001) for r in hit]
    assert snapshot_crossings(slow) == []  # class B (5-60 s) never triggers
    thin = [dict(r, b_bid_size=0.05) for r in hit]
    assert snapshot_crossings(thin) == []  # depth < 0.1 BTC on the crossing side
    small = [dict(r, b_bid=1200.0) for r in hit]  # net 200 - 60 = +0.14 % < 0.15 %
    assert snapshot_crossings(small) == []


def test_plan_tick_and_windows():
    t0 = NOW
    until = {"BTC": t0 + timedelta(minutes=30)}
    kw = {"hourly_seconds": 3600, "currencies": ("BTC", "ETH")}
    assert plan_tick(t0, None, {}, **kw) == ("hourly", ("BTC", "ETH"))  # first tick
    assert plan_tick(t0 + timedelta(seconds=3570), t0, {}, **kw) == ("hourly", ("BTC", "ETH"))  # half a tick of slack
    assert plan_tick(t0 + timedelta(seconds=3569), t0, {}, **kw) == (None, ())
    assert plan_tick(t0 + timedelta(minutes=29), t0, until, **kw) == ("minute", ("BTC",))
    assert plan_tick(t0 + timedelta(minutes=30), t0, until, **kw) == (None, ())  # window over
    assert plan_tick(t0 + timedelta(minutes=60), t0, until | {"ETH": t0 + timedelta(hours=2)}, **kw)[0] == "hourly"
    hit = [("BTC", 82000.0, "C", "b_over_d")]
    later = t0 + timedelta(minutes=20)
    assert extend_windows(until, hit, later) == {"BTC": later + timedelta(seconds=MINUTE_WINDOW_SECONDS)}  # re-armed
    assert extend_windows(until, [], later) == until and extend_windows({}, [("ETH", 1.0, "P", "d_over_b")], t0) == {
        "ETH": t0 + timedelta(minutes=30)
    }


def test_seed_schedule_from_stored_rows():
    t = int(NOW.timestamp())
    rows = [_xrow(k, ts=t, b_bid=0.0) for k in (99_500.0, 100_000.0, 100_500.0)]  # hourly, no crossing
    rows += [_xrow(100_000.0, ts=t + 600, mode="minute")]  # minute row with a crossing
    rows += [_xrow(100_000.0, ts=t + 660, mode="minute", b_bid=0.0)]  # gone a minute later
    last, until = seed_schedule(rows)
    assert last == NOW and until == {"BTC": NOW + timedelta(seconds=600 + MINUTE_WINDOW_SECONDS)}
    assert seed_schedule([]) == (None, {})


def _clock(rt) -> list:
    clock = [NOW]
    rt.now = lambda: clock[0]
    return clock


async def test_crossing_starts_minute_window_and_it_ends_after_30_min(tmp_path):
    opts = {"bybit": "cross"}
    rt, calls = _rt(tmp_path, opts=opts)
    clock, job = _clock(rt), OptionXvenueJob()
    try:
        assert await job(rt) == 14  # hourly: ATM +- 3, BTC (ETH has no daily expiry in the fake)
        assert job.minute_until == {"BTC": NOW + timedelta(minutes=30)}
        calls.clear()
        opts["bybit"] = "ok"  # the crossing is gone from here on: the window is not re-armed
        clock[0] = NOW + timedelta(seconds=61)
        assert await job(rt) == 10  # minute: ATM +- 2 of BTC only
        assert {x[2] for x in calls if x[1] == "ticker"} == ATM2 and len([x for x in calls if x[1] == "ticker"]) == 10
        assert [x[2] for x in calls if x[1] == "get_book_summary_by_currency"] == ["BTC"]  # ETH not read in BTC's window
        assert [x for x in calls if x[0] == "bybit"] == [("bybit", "option", "BTC")]
        clock[0] = NOW + timedelta(minutes=20)
        assert await job(rt) == 10 and job.minute_until == {"BTC": NOW + timedelta(minutes=30)}
        calls.clear()
        clock[0] = NOW + timedelta(minutes=30, seconds=1)
        assert await job(rt) == 0 and calls == []  # window over, hourly not due: nothing is read
        clock[0] = NOW + timedelta(minutes=60)
        assert await job(rt) == 14  # back to the hourly snapshot
        rows = await rt.store.option_xvenue_rows()
        modes = {(r["ts"] - int(NOW.timestamp()), r["mode"]) for r in rows}
        assert modes == {(0, "hourly"), (61, "minute"), (1200, "minute"), (3600, "hourly")}
        assert {r["strike"] for r in rows if r["mode"] == "minute"} == {81000.0, 81500.0, 82000.0, 82500.0, 83000.0}
        assert {r["strike"] for r in rows if r["mode"] == "minute" and r["reg_atm"] == 1} == {81500.0, 82000.0, 82500.0}
        first = [r for r in rows if r["ts"] == int(NOW.timestamp())]
        assert snapshot_crossings(first) == [("BTC", 82000.0, "C", "b_over_d")]  # the stored rows reproduce the trigger
    finally:
        await rt.aclose()


async def test_no_crossing_stays_hourly(tmp_path):
    rt, calls = _rt(tmp_path)
    clock, job = _clock(rt), OptionXvenueJob()
    try:
        assert await job(rt) == 14
        assert snapshot_crossings(await rt.store.option_xvenue_rows()) == [] and job.minute_until == {}
        calls.clear()
        for minutes in (1, 15, 59):
            clock[0] = NOW + timedelta(minutes=minutes)
            assert await job(rt) == 0
        assert calls == []
        clock[0] = NOW + timedelta(minutes=60)
        assert await job(rt) == 14
        assert {r["mode"] for r in await rt.store.option_xvenue_rows()} == {"hourly"}
    finally:
        await rt.aclose()


async def test_crossing_rearms_the_window_and_a_restart_resumes_it(tmp_path):
    rt, _ = _rt(tmp_path, bybit="cross")
    clock = _clock(rt)
    try:
        job = OptionXvenueJob()
        assert await job(rt) == 14
        clock[0] = NOW + timedelta(minutes=20)
        assert await job(rt) == 10 and job.minute_until == {"BTC": NOW + timedelta(minutes=50)}  # re-armed by a minute snapshot
        restarted = OptionXvenueJob()  # a new process: state from the store, no extra hourly snapshot
        clock[0] = NOW + timedelta(minutes=40)
        assert await restarted(rt) == 10
        assert restarted.last_hourly == NOW and restarted.minute_until == {"BTC": NOW + timedelta(minutes=70)}
    finally:
        await rt.aclose()


async def test_minute_mode_provider_failure_never_crashes_the_schedule(tmp_path):
    opts = {"bybit": "cross"}
    rt, _ = _rt(tmp_path, opts=opts)
    clock, job = _clock(rt), OptionXvenueJob()
    try:
        assert await job(rt) == 14
        opts["bybit"] = "down"
        clock[0] = NOW + timedelta(minutes=1)
        assert await job(rt) == 10  # Deribit legs stored, Bybit in missing; no crossing measurable, window unchanged
        assert job.minute_until == {"BTC": NOW + timedelta(minutes=30)}
        opts["deribit_down"] = True
        clock[0] = NOW + timedelta(minutes=2)
        with pytest.raises(RuntimeError, match=r"option_xvenue \(minute\): no rows"):  # recorded as a failed run
            await job(rt)
        opts.update(bybit="ok", deribit_down=False)
        clock[0] = NOW + timedelta(minutes=3)
        assert await job(rt) == 10  # the window survived the failure
        rows = await rt.store.option_xvenue_rows()
        down = [r for r in rows if r["ts"] == int((NOW + timedelta(minutes=1)).timestamp())]
        assert len(down) == 10 and all(r["mode"] == "minute" and r["b_symbol"] is None and r["d_instrument"] for r in down)
        assert all(r["b_recv_ms"] is None and r["d_recv_ms"] is not None for r in down)  # failed leg: no receive time
    finally:
        await rt.aclose()


async def test_bybit_leaves_in_the_same_gather_before_the_deribit_tickers(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_option_xvenue(rt) == 14
        order = [x for x in calls if x[0] == "bybit" or x[1] == "ticker"]
        assert order[0] == ("bybit", "option", "BTC")  # was after all 14 Deribit tickers
        assert {x[2] for x in order[1:7]} == ATM1  # the registered strikes first in the Deribit rate-limit queue
    finally:
        await rt.aclose()


async def test_minute_mode_keeps_the_episode_strike_when_the_forward_moves(tmp_path):
    opts = {"bybit": "cross"}
    rt, _ = _rt(tmp_path, opts=opts)
    clock, job = _clock(rt), OptionXvenueJob()
    try:
        assert await job(rt) == 14 and job.minute_until == {"BTC": NOW + timedelta(minutes=30)}  # episode at 82 000 C (ATM)
        opts["fwd_shift"] = -1000.0  # forward 80 856.69: ATM 81 000, the registered ATM +- 1 no longer holds 82 000
        assert 82_000.0 not in atm_strikes(ATM, 80_856.69, REG_STRIKES_EACH_SIDE)  # what an ATM +- 1 read would lose
        clock[0] = NOW + timedelta(minutes=5)
        assert await job(rt) == 10  # ATM +- 2: 80 000 .. 82 000 (80 000: Bybit only, Deribit lists no ticker in the fake)
        rows = [r for r in await rt.store.option_xvenue_rows() if r["mode"] == "minute"]
        assert sorted({r["strike"] for r in rows}) == [80_000.0, 80_500.0, 81_000.0, 81_500.0, 82_000.0]
        assert sorted({r["strike"] for r in rows if r["reg_atm"] == 1}) == [80_500.0, 81_000.0, 81_500.0]
        ep = next(r for r in rows if r["strike"] == 82_000.0 and r["opt"] == "C")
        assert ep["reg_atm"] == 0 and ep["d_instrument"] and ep["b_symbol"]  # still read, both legs: trackable
        assert adjusted_crossing_from_row(ep)["b_over_d"]["net_frac"] >= REG_THRESHOLD  # the episode is still there ...
        assert snapshot_crossings(rows) == []  # ... but the registered trigger (ATM +- 1) is unchanged: no re-arm
        assert job.minute_until == {"BTC": NOW + timedelta(minutes=30)}
    finally:
        await rt.aclose()


async def test_receive_time_of_each_leg_lies_inside_the_tick(tmp_path):
    rt, _ = _rt(tmp_path)
    ticks = iter(range(10_000))
    rt.now = lambda: NOW + timedelta(milliseconds=next(ticks))  # every clock read 1 ms later
    try:
        assert await collect_option_xvenue(rt) == 14
        end = int(rt.now().timestamp() * 1000)
        rows = await rt.store.option_xvenue_rows()
        start = int(NOW.timestamp() * 1000)
        assert {r["ts"] for r in rows} == {int(NOW.timestamp())}
        assert all(start < r["d_recv_ms"] < end and start < r["b_recv_ms"] < end for r in rows)
        assert len({r["d_recv_ms"] for r in rows}) == 14  # one Deribit request per instrument
        assert len({r["b_recv_ms"] for r in rows}) == 1  # one Bybit chain request for the currency
    finally:
        await rt.aclose()


async def test_receive_time_is_unknown_for_a_cached_answer():
    clock = SimpleNamespace(now=lambda: NOW)

    async def answer(cached: bool) -> Fetched:
        return Fetched({"x": 1}, Evidence(provider="deribit", tool="ticker", cached=cached))

    assert (await _received(clock, answer(False))).data == ({"x": 1}, int(NOW.timestamp() * 1000))
    assert (await _received(clock, answer(True))).data == ({"x": 1}, None)


async def test_migration_26_tags_old_rows_hourly_and_mode_is_stored(tmp_path):
    path = tmp_path / "v25.sqlite"
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= 25:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("INSERT INTO option_xvenue_snapshot (ts, currency, expiry_ts, tau_hours, strike, opt) VALUES (1, 'BTC', 9, 5.0, 1.0, 'C')")
    con.execute("PRAGMA user_version = 25")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 26
        assert [r["mode"] for r in await s.option_xvenue_rows()] == ["hourly"]
        row = {"currency": "BTC", "expiry_ts": 9, "tau_hours": 5.0, "strike": 1.0, "opt": "C"}
        assert await s.insert_option_xvenue(2, [row], mode="minute") == 1
        assert [(r["ts"], r["mode"]) for r in await s.option_xvenue_rows()] == [(1, "hourly"), (2, "minute")]
        old = (await s.option_xvenue_rows())[0]
        assert (old["reg_atm"], old["d_recv_ms"], old["b_recv_ms"]) == (None, None, None)  # unknown for earlier rows
        tagged = row | {"reg_atm": 1, "d_recv_ms": 1_792_724_300_100, "b_recv_ms": 1_792_724_300_350}
        assert await s.insert_option_xvenue(4, [tagged], mode="minute") == 1
        new = (await s.option_xvenue_rows(4))[0]
        assert (new["reg_atm"], new["d_recv_ms"], new["b_recv_ms"]) == (1, 1_792_724_300_100, 1_792_724_300_350)
        with pytest.raises(ValueError):
            await s.insert_option_xvenue(3, [row], mode="burst")
    finally:
        await s.aclose()
