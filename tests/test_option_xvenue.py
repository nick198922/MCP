"""Deribit <-> Bybit daily option quotes: pure crossing measure (analytics/option_xvenue.py), the table
option_xvenue_snapshot and the collector job `option_xvenue` (IDEA-20261023-bybit-deribit-daily-option-dislocation-forward)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import httpx
import pytest

from conftest import FakeEx, State, make_transports
from tradedesk.analytics.option_xvenue import (
    FEE_RATE,
    MIN_DEPTH,
    atm_strikes,
    bybit_quote,
    crossing,
    crossing_from_row,
    deribit_quote,
    parse_bybit_symbol,
    pick_daily_expiry,
)
from tradedesk.collector.option_xvenue import collect_option_xvenue, scan_option_xvenue
from tradedesk.collector.scheduler import _retention_sweep, build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.features import allowed
from tradedesk.runtime import Runtime

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


def _summary() -> list[dict]:
    """Book summary: the sample's 23OCT26 strikes plus far strikes and a next-day expiry the job must ignore."""
    fwd = SAMPLE["deribit_ticker"]["BTC-23OCT26-82000-C"]["underlying_price"]
    names = list(SAMPLE["deribit_ticker"])
    names += [f"BTC-23OCT26-{k}-{t}" for k in (78000, 79000, 80000, 84000, 85000) for t in "CP"]
    names += [f"BTC-24OCT26-{k}-{t}" for k in (81000, 82000) for t in "CP"]
    return [{"instrument_name": n, "underlying_price": fwd, "estimated_delivery_price": 81847.32} for n in names]


def _rt(tmp_path, *, deribit_down: bool = False, empty_summary: bool = False, bybit: str = "ok", **kw) -> tuple[Runtime, list]:
    calls: list = []

    def deribit(req: httpx.Request) -> httpx.Response:
        calls.append(("deribit", req.url.path.rsplit("/", 1)[-1], req.url.params.get("instrument_name")))
        if deribit_down:
            return httpx.Response(500, text="down")
        if req.url.path.endswith("get_book_summary_by_currency"):
            far = [{"instrument_name": "ETH-30OCT26-2400-C", "underlying_price": 2412.5}]  # ETH: no daily expiry in the fake
            res = ([] if empty_summary else _summary()) if req.url.params["currency"] == "BTC" else far
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": res})
        name = req.url.params.get("instrument_name")
        if req.url.path.endswith("/ticker") and name in SAMPLE["deribit_ticker"]:
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": SAMPLE["deribit_ticker"][name]})
        return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"code": 13020, "message": "instrument_not_found"}})

    class Ex(FakeEx):
        async def publicGetV5MarketTickers(self, params):  # noqa: N802 - ccxt implicit endpoint name
            calls.append(("bybit", params["category"], params["baseCoin"]))
            if bybit == "down":
                raise ccxt.NetworkError("bybit unreachable")
            body = json.loads(json.dumps(SAMPLE["bybit"]))
            if params["baseCoin"] != "BTC" or bybit == "no_daily":
                body["result"]["list"] = [r for r in body["result"]["list"] if params["baseCoin"] == "BTC" and "23OCT26" not in r["symbol"]]
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
        c = crossing_from_row(r)  # live sample: no crossing; Bybit's index 0.15 % above Deribit's
        assert c["best"] < 0 and c["index_gap"] == pytest.approx(0.001456, abs=1e-5) and c["dt_ms"] == 2205
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
        with pytest.raises(RuntimeError, match="option_xvenue: no rows"):  # the scheduler records it as a failed run
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
