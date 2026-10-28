"""Hourly flow snapshot (IDEA-20261028-market-flow-reader, step 2): the pure row builder (analytics/flow_snapshot.py), the
table flow_snapshot and the collector job `flow_snapshot`, on live answers captured 2026-10-28 (see the fixture's
`_source`)."""

from __future__ import annotations

import copy
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ccxt
import httpx
import pytest

from conftest import State, make_transports
from tradedesk.analytics import flow_snapshot as fs
from tradedesk.collector.flow_snapshot import collect_flow_snapshot
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.features import allowed
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

FX = json.loads((Path(__file__).parent / "fixtures" / "flow_snapshot_live_2026-10-28.json").read_text())
NOW = datetime(2026, 10, 28, 17, 11, 12, tzinfo=UTC)
HOUR = int(datetime(2026, 10, 28, 17, tzinfo=UTC).timestamp())
KEYS = {(b, v, m) for b in ("BTC", "ETH") for v, m in fs.VENUE_MARKETS}


# ----------------------------------------------------------------------------- pure functions

BIDS = [(99.95, 1.0), (99.6, 2.0), (99.2, 3.0), (98.9, 4.0)]
ASKS = [(100.05, 1.0), (100.4, 2.0), (100.8, 3.0), (101.2, 4.0)]


def test_depth_bands_hand_book():
    d = fs.depth_bands(list(reversed(BIDS)), ASKS)  # unsorted input is sorted
    assert d["mid"] == pytest.approx(100.0)
    assert d["reach_bid"] == pytest.approx(0.011) and d["reach_ask"] == pytest.approx(0.012)
    assert d["bid_usd_10bp"] == pytest.approx(99.95) and d["ask_usd_10bp"] == pytest.approx(100.05)
    assert d["bid_usd_50bp"] == pytest.approx(99.95 + 2 * 99.6) and d["ask_usd_50bp"] == pytest.approx(100.05 + 2 * 100.4)
    assert d["bid_usd_100bp"] == pytest.approx(99.95 + 2 * 99.6 + 3 * 99.2)
    assert d["ask_usd_100bp"] == pytest.approx(100.05 + 2 * 100.4 + 3 * 100.8)


def test_depth_bands_null_beyond_reach_and_for_bad_books():
    d = fs.depth_bands(BIDS[:2], ASKS[:3])  # bids reach 0.4 %, asks 0.8 %
    assert d["bid_usd_10bp"] == pytest.approx(99.95) and d["bid_usd_50bp"] is None and d["bid_usd_100bp"] is None
    assert d["ask_usd_50bp"] == pytest.approx(300.85) and d["ask_usd_100bp"] is None  # never a partial sum
    assert fs.depth_bands([], ASKS) == fs.empty_depth()
    assert fs.depth_bands([(100.1, 1.0)], [(100.0, 1.0)]) == fs.empty_depth()  # crossed


def test_depth_bands_aggregated_steps():
    # step 0.1 on a mid of 100: a band needs >= 8 steps = 0.8 USD, so only the 1 % band (1 USD) is computed
    d = fs.depth_bands(BIDS, ASKS, step=0.1)
    assert d["bid_usd_10bp"] is None and d["bid_usd_50bp"] is None and d["bid_usd_100bp"] == pytest.approx(596.75)
    assert d["ask_usd_10bp"] is None and d["ask_usd_50bp"] is None and d["ask_usd_100bp"] == pytest.approx(603.25)
    assert all(fs.depth_bands(BIDS, ASKS, step=1.0)[k] is None for k in fs.DEPTH_KEYS[3:])
    # a reference mid moves the band edges: from 99.7 the 0.5 % bid band (>= 99.2015) holds two levels, not three
    m = fs.depth_bands(BIDS, ASKS, mid=99.7)
    assert m["mid"] == 99.7 and m["reach_bid"] == pytest.approx(1 - 98.9 / 99.7)
    assert m["bid_usd_50bp"] == pytest.approx(99.95 + 2 * 99.6)


def test_levels_parsing():
    assert fs.levels([["80498.4", "0.344", "2"], [1.0, 2.0], ["x", "1"], [5.0, 0], [5.0], None]) == [(80498.4, 0.344), (1.0, 2.0)]
    assert fs.levels([{"px": "2332", "sz": "1.5", "n": 3}, {"px": "1"}], size_mult=0.1) == [(2332.0, pytest.approx(0.15))]
    assert fs.levels(None) == [] and fs.levels({"bids": []}) == []


def test_sig_fig_step():
    assert fs.sig_fig_step(80475.0, 4) == pytest.approx(10.0) and fs.sig_fig_step(80475.0, 3) == pytest.approx(100.0)
    assert fs.sig_fig_step(2332.5, 4) == pytest.approx(1.0) and fs.sig_fig_step(2332.5, 3) == pytest.approx(10.0)
    assert fs.sig_fig_step(0.004989, 5) == pytest.approx(1e-7)


def _band(levels: list, mid: float, w: float, side: str) -> float:
    return sum(
        float(x["px"]) * float(x["sz"])
        for x in levels
        if (float(x["px"]) >= mid * (1 - w) if side == "bid" else float(x["px"]) <= mid * (1 + w))
    )


def test_hl_depth_on_the_live_books():
    for base, have, null in (("BTC", ("10bp", "100bp"), ("50bp",)), ("ETH", ("50bp",), ("10bp", "100bp"))):
        books = [(n, FX["hl_l2"][f"{base}|{n}"]) for n in fs.HL_SIG_FIGS]
        d = fs.hl_depth(books)
        fine, coarse = books[0][1]["levels"], books[1][1]["levels"]
        mid = (float(fine[0][0]["px"]) + float(fine[1][0]["px"])) / 2  # mid of the finer book
        assert d["mid"] == pytest.approx(mid)
        assert d["reach_bid"] == pytest.approx(1 - float(coarse[0][-1]["px"]) / mid)  # the farthest book
        for label in null:
            assert d[f"bid_usd_{label}"] is None and d[f"ask_usd_{label}"] is None
        for label in have:
            w = dict(fs.BANDS)[label]
            # the finest book that reaches the band with >= 8 steps: BTC 0.1 % from 4 sig figs ($10), 1 % from 3 ($100)
            src = fine if (base, label) != ("BTC", "100bp") else coarse
            assert d[f"bid_usd_{label}"] == pytest.approx(_band(src[0], mid, w, "bid"))
            assert d[f"ask_usd_{label}"] == pytest.approx(_band(src[1], mid, w, "ask"))
    assert fs.hl_depth([(4, {"levels": []}), (3, None)]) == fs.empty_depth()


def test_flow_row_units_and_missing():
    r = fs.flow_row("okx", "perp", depth={"mid": 80460.0}, oi_coins="31200.304", funding_rate=7.491e-05, funding_hours=8.0,
                    long_share=fs.long_share_from_ratio("1.9377643692460811"), missing=["oi", "book", "oi"])  # fmt: skip
    assert r["oi_usd"] == pytest.approx(31200.304 * 80460.0)
    assert r["funding_annualized"] == pytest.approx(7.491e-05 * 1095)  # 8760 / 8 h
    assert r["long_share"] == pytest.approx(1.9377643692460811 / 2.9377643692460811)
    assert r["missing"] == "book,oi" and r["bid_usd_10bp"] is None
    bad = fs.flow_row("binance", "perp", oi_coins=-1, funding_rate="nan", funding_hours=8.0, long_share=1.2)
    assert (bad["oi_coins"], bad["funding_rate"], bad["funding_hours"], bad["long_share"], bad["missing"]) == (None,) * 5
    assert not fs.has_data(bad) and fs.has_data(fs.flow_row("bybit", "spot", depth={"mid": 1.0}))
    assert fs.long_share_from_ratio(-1) is None and fs.long_share_from_ratio(None) is None


# ----------------------------------------------------------------------------- collector


class FlowEx:
    """ccxt double serving the fixture; ``down`` (class-level, set per test) = names of calls that fail."""

    down: set[str] = set()
    okx_swap_book: dict | None = None

    def __init__(self, ex_id: str) -> None:
        self.id = ex_id
        self.has = {"fetchOpenInterest": True, "fetchFundingIntervals": True}

    def _check(self, name: str) -> None:
        if name in self.down or f"{self.id}:{name}" in self.down:
            raise ccxt.NetworkError(f"{self.id} {name} down")

    async def fetch_order_book(self, symbol, limit=100):
        self._check("book")
        return copy.deepcopy(FX["ccxt_order_book"][f"{self.id}|{symbol}"])

    async def fetch_open_interest(self, symbol):
        self._check("oi")
        return dict(FX["ccxt_open_interest"][f"{self.id}|{symbol}"])

    async def fetch_funding_rate(self, symbol):
        self._check("funding")
        return dict(FX["ccxt_funding_rate"][f"{self.id}|{symbol}"])

    async def fetch_funding_intervals(self, symbols=None):
        self._check("funding_intervals")
        return copy.deepcopy(FX["funding_intervals_binanceusdm"])

    async def publicGetMarketBooksFull(self, params):  # noqa: N802 - ccxt implicit endpoint name
        self._check("books_full")
        if params["instId"].endswith("-SWAP") and self.okx_swap_book is not None:
            return copy.deepcopy(self.okx_swap_book)
        return copy.deepcopy(FX["okx"][f"books-full|{params['instId']}"])

    async def publicGetPublicInstruments(self, params):  # noqa: N802
        self._check("instruments")
        return copy.deepcopy(FX["okx"][f"instruments|{params['instId']}"])

    async def publicGetPublicOpenInterest(self, params):  # noqa: N802
        self._check("okx_oi")
        return copy.deepcopy(FX["okx"][f"open-interest|{params['instId']}"])

    async def publicGetRubikStatContractsLongShortAccountRatioContract(self, params):  # noqa: N802
        self._check("okx_ls")
        return copy.deepcopy(FX["okx"][f"ls|{params['instId']}"])

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_fake():
    FlowEx.down, FlowEx.okx_swap_book = set(), None
    yield
    FlowEx.down, FlowEx.okx_swap_book = set(), None


def _rt(tmp_path, *, http_down: set[str] | None = None, **kw) -> tuple[Runtime, list]:
    """Runtime on the fixture. ``http_down`` = Http providers answering 500 ('binance_futures', 'bybit', 'hyperliquid')."""
    calls: list = []
    down = http_down if http_down is not None else set()

    def binance_futures(req: httpx.Request) -> httpx.Response:
        calls.append(("binance_futures", req.url.path))
        if "binance_futures" in down:
            return httpx.Response(500, text="down")
        return httpx.Response(200, json=FX["binance_ls"][req.url.params["symbol"]])

    def bybit(req: httpx.Request) -> httpx.Response:
        calls.append(("bybit", req.url.path))
        if "bybit" in down:
            return httpx.Response(500, text="down")
        return httpx.Response(200, json=FX["bybit_ls"][req.url.params["symbol"]])

    def hl(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(("hyperliquid", body["type"], body.get("nSigFigs")))
        if "hyperliquid" in down:
            return httpx.Response(500, text="down")
        if body["type"] == "metaAndAssetCtxs":
            return httpx.Response(200, json=FX["hl_meta_and_asset_ctxs"])
        if body["type"] == "l2Book" and body.get("nSigFigs"):
            return httpx.Response(200, json=FX["hl_l2"][f"{body['coin']}|{body['nSigFigs']}"])
        return httpx.Response(422, text="unexpected")

    tr = make_transports(State())
    tr.update(binance_futures=httpx.MockTransport(binance_futures), bybit=httpx.MockTransport(bybit), hyperliquid=httpx.MockTransport(hl))
    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / "x.sqlite"), **kw), transports=tr, exchange_factory=FlowEx
    )
    rt.now = lambda: NOW
    return rt, calls


def _by(rows: list) -> dict:
    return {(r["base"], r["venue"], r["market"]): r for r in rows}


async def test_job_writes_fourteen_rows_on_the_live_sample(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        assert await collect_flow_snapshot(rt) == 14
        rows = await rt.store.flow_snapshot_rows()
        by = _by(rows)
        assert set(by) == KEYS and {r["ts"] for r in rows} == {HOUR} and {r["fetched_ts"] for r in rows} == {int(NOW.timestamp())}
        assert all(r["missing"] is None for r in rows)
        for b in ("BTC", "ETH"):
            # perps: OI in coins, oi_usd = coins x mid, funding per interval and annualised
            bn = by[(b, "binance", "perp")]
            assert bn["oi_coins"] == FX["ccxt_open_interest"][f"binanceusdm|{b}/USDT:USDT"]["openInterestAmount"]
            assert bn["oi_usd"] == pytest.approx(bn["oi_coins"] * bn["mid"])
            rate = FX["ccxt_funding_rate"][f"binanceusdm|{b}/USDT:USDT"]["fundingRate"]
            assert (bn["funding_rate"], bn["funding_hours"]) == (rate, 8.0)  # interval from fetch_funding_intervals
            assert bn["funding_annualized"] == pytest.approx(rate * 1095)
            assert bn["long_share"] == float(FX["binance_ls"][f"{b}USDT"][0]["longAccount"])
            assert by[(b, "bybit", "perp")]["long_share"] == float(FX["bybit_ls"][f"{b}USDT"]["result"]["list"][0]["buyRatio"])
            ok = by[(b, "okx", "perp")]
            assert ok["oi_coins"] == pytest.approx(float(FX["okx"][f"open-interest|{b}-USDT-SWAP"]["data"][0]["oiCcy"]))
            r = float(FX["okx"][f"ls|{b}-USDT-SWAP"]["data"][0][1])
            assert ok["long_share"] == pytest.approx(r / (1 + r)) and ok["funding_hours"] == 8.0
            h = by[(b, "hyperliquid", "perp")]
            assert h["funding_hours"] == 1.0 and h["funding_annualized"] == pytest.approx(h["funding_rate"] * 8760)
            assert h["long_share"] is None and h["mid"] is not None
            # trimmed live books (25 levels): mid from the top of book, every band NULL (reach < 0.1 %)
            for v, m, key in (("binance", "spot", f"binance|{b}/USDT"), ("bybit", "perp", f"bybit|{b}/USDT:USDT")):
                row, ob = by[(b, v, m)], FX["ccxt_order_book"][key]
                assert row["mid"] == pytest.approx((ob["bids"][0][0] + ob["asks"][0][0]) / 2)
                assert row["reach_bid"] < 0.001 and all(row[k] is None for k in fs.DEPTH_KEYS[3:])
            assert by[(b, "binance", "spot")]["oi_coins"] is None and by[(b, "okx", "spot")]["funding_rate"] is None
        assert by[("BTC", "hyperliquid", "perp")]["bid_usd_100bp"] is not None  # aggregated HL books reach 1 %
        assert {c[2] for c in calls if c[:2] == ("hyperliquid", "l2Book")} == {4, 3}
        # a later tick in the same hour: no request, nothing written
        n = len(calls)
        rt.now = lambda: NOW + timedelta(minutes=10)
        assert await collect_flow_snapshot(rt) == 0 and len(calls) == n
        # the next hour: a new snapshot
        rt.now = lambda: NOW + timedelta(minutes=50)
        assert await collect_flow_snapshot(rt) == 14
        assert sorted({r["ts"] for r in await rt.store.flow_snapshot_rows(base="ETH")}) == [HOUR, HOUR + 3600]
    finally:
        await rt.aclose()


async def test_okx_swap_book_sizes_are_contracts(tmp_path):
    # Synthetic levels around the live top of book so the 0.1 % band is reached: sizes in contracts x ctVal 0.01 BTC.
    top = FX["okx"]["books-full|BTC-USDT-SWAP"]["data"][0]
    bid, ask = float(top["bids"][0][0]), float(top["asks"][0][0])
    mid = (bid + ask) / 2
    bids = [[str(bid - i * 20.0), "100", "3"] for i in range(10)]  # 100 contracts = 1 BTC a level, down to -0.22 %
    asks = [[str(ask + i * 20.0), "300", "5"] for i in range(10)]
    FlowEx.okx_swap_book = {"code": "0", "msg": "", "data": [{"bids": bids, "asks": asks, "ts": top["ts"]}]}
    rt, _ = _rt(tmp_path)
    try:
        await collect_flow_snapshot(rt)
        ok = _by(await rt.store.flow_snapshot_rows())[("BTC", "okx", "perp")]
        exp_bid = sum(float(p) * 1.0 for p, *_ in bids if float(p) >= mid * 0.999)
        exp_ask = sum(float(p) * 3.0 for p, *_ in asks if float(p) <= mid * 1.001)
        assert ok["bid_usd_10bp"] == pytest.approx(exp_bid) and ok["ask_usd_10bp"] == pytest.approx(exp_ask)
        assert ok["bid_usd_50bp"] is None and ok["mid"] == pytest.approx(mid)
    finally:
        await rt.aclose()


async def test_partial_failures_leave_null_columns_named_in_missing(tmp_path, caplog):
    FlowEx.down = {"okx:okx_oi", "funding_intervals", "bybit:book"}
    http_down = {"binance_futures"}
    rt, _ = _rt(tmp_path, http_down=http_down)
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_flow_snapshot(rt) == 12  # Bybit spot rows have no data: retried next tick
        by = _by(await rt.store.flow_snapshot_rows())
        assert ("BTC", "bybit", "spot") not in by and "BTC bybit spot: book" in caplog.text
        okp = by[("BTC", "okx", "perp")]
        assert okp["oi_coins"] is None and okp["oi_usd"] is None and okp["missing"] == "oi" and okp["mid"] is not None
        bn = by[("ETH", "binance", "perp")]
        assert bn["missing"] == "funding_interval,long_share" and bn["long_share"] is None
        assert bn["funding_hours"] == 8.0 and bn["funding_rate"] is not None  # 8 h assumed when the interval is unknown
        byp = by[("ETH", "bybit", "perp")]
        assert byp["missing"] == "book" and byp["mid"] is None and byp["oi_usd"] is None and byp["oi_coins"] is not None
        assert "partial rows" in caplog.text
        # the venues recover inside the hour: the 2 absent and 6 partial keys (Binance / Bybit / OKX perps) are fetched
        # again; the absent ones are inserted, the partial ones replaced whole, with the new fetch time
        FlowEx.down = set()
        http_down.clear()
        later = NOW + timedelta(minutes=10)
        rt.now = lambda: later
        assert await collect_flow_snapshot(rt) == 8
        by = _by(await rt.store.flow_snapshot_rows())
        assert set(by) == KEYS and all(r["missing"] is None for r in by.values())
        byp = by[("ETH", "bybit", "perp")]
        assert byp["mid"] is not None and byp["oi_usd"] is not None and byp["fetched_ts"] == int(later.timestamp())
        assert by[("BTC", "hyperliquid", "perp")]["fetched_ts"] == int(NOW.timestamp())  # complete row kept
        # every key complete: the next tick fetches nothing
        rt.now = lambda: NOW + timedelta(minutes=20)
        assert await collect_flow_snapshot(rt) == 0
    finally:
        await rt.aclose()


async def test_every_provider_down_records_nothing_and_never_raises(tmp_path, caplog):
    FlowEx.down = {"book", "oi", "funding", "funding_intervals", "books_full", "instruments", "okx_oi", "okx_ls"}
    rt, _ = _rt(tmp_path, http_down={"binance_futures", "bybit", "hyperliquid"})
    try:
        with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
            assert await collect_flow_snapshot(rt) == 0
        assert await rt.store.flow_snapshot_rows() == []
        assert "14 rows not recorded" in caplog.text
        FlowEx.down = set()
    finally:
        await rt.aclose()
    rt, _ = _rt(tmp_path)  # recovers on the next tick (same db)
    try:
        rt.now = lambda: NOW + timedelta(minutes=20)
        assert await collect_flow_snapshot(rt) == 14
    finally:
        await rt.aclose()


async def test_malformed_okx_answers_are_missing_not_errors(tmp_path):
    rt, _ = _rt(tmp_path)
    try:

        async def bad(params):
            return {"code": "51001", "msg": "Instrument ID does not exist", "data": []}

        orig = FlowEx.__init__

        def init(self, ex_id):
            orig(self, ex_id)
            if ex_id == "okx":
                self.publicGetPublicInstruments = bad
                self.publicGetRubikStatContractsLongShortAccountRatioContract = bad

        FlowEx.__init__ = init
        try:
            assert await collect_flow_snapshot(rt) == 14
        finally:
            FlowEx.__init__ = orig
        ok = _by(await rt.store.flow_snapshot_rows())[("ETH", "okx", "perp")]
        assert ok["missing"] == "book,long_share" and ok["mid"] is None and ok["oi_coins"] is not None
    finally:
        await rt.aclose()


async def test_no_store_never_raises(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        rt.store = None
        assert await collect_flow_snapshot(rt) == 0 and calls == []
    finally:
        await rt.aclose()


async def test_hl_l2_book_sends_n_sig_figs(tmp_path):
    rt, calls = _rt(tmp_path)
    try:
        raw = (await rt.hyperliquid.l2_book("BTC", 3)).data
        assert raw["levels"][0][0]["px"] == FX["hl_l2"]["BTC|3"]["levels"][0][0]["px"]
        assert ("hyperliquid", "l2Book", 3) in calls
    finally:
        await rt.aclose()


async def test_insert_replaces_only_by_a_more_complete_row_and_keys(tmp_path):
    s = Store(tmp_path / "x.sqlite")
    await s.init()
    try:
        row = {"base": "BTC", "venue": "okx", "market": "perp", "mid": 80460.0, "oi_coins": 31188.0, "missing": "book,long_share"}
        assert await s.insert_flow_snapshot(HOUR, HOUR + 60, [row, {"base": "BTC", "venue": "okx"}]) == 1  # no market: dropped
        assert await s.flow_snapshot_keys(HOUR) == set()  # a partial row is not done
        assert await s.insert_flow_snapshot(HOUR, HOUR + 600, [dict(row, mid=1.0)]) == 0  # as many gaps: first kept
        assert await s.insert_flow_snapshot(HOUR, HOUR + 600, [dict(row, mid=2.0, missing="oi,long_share")]) == 0
        better = dict(row, mid=80470.0, oi_coins=None, missing="oi")
        assert await s.insert_flow_snapshot(HOUR, HOUR + 900, [better]) == 1  # fewer gaps: replaced whole
        got = (await s.flow_snapshot_rows())[0]
        assert (got["mid"], got["oi_coins"], got["missing"], got["fetched_ts"]) == (80470.0, None, "oi", HOUR + 900)
        # funding_interval alone is complete (8 h assumed); a complete row is final
        done = dict(row, mid=80480.0, missing="funding_interval")
        assert await s.insert_flow_snapshot(HOUR, HOUR + 1200, [done]) == 1
        assert await s.flow_snapshot_keys(HOUR) == {("BTC", "okx", "perp")} and await s.flow_snapshot_keys(HOUR + 3600) == set()
        assert await s.insert_flow_snapshot(HOUR, HOUR + 1800, [dict(row, mid=9.0, missing=None)]) == 0
        got = (await s.flow_snapshot_rows())[0]
        assert (got["mid"], got["missing"], got["fetched_ts"], got["funding_rate"]) == (80480.0, "funding_interval", HOUR + 1200, None)
    finally:
        await s.aclose()


async def test_hl_coin_without_funding_names_funding_in_missing(tmp_path):
    rt, _ = _rt(tmp_path)
    try:
        ctxs = await rt.hyperliquid.asset_contexts()
        for c in ctxs.data:
            if c.get("coin") == "ETH":
                c["funding_1h"] = None

        async def ctx():
            return ctxs

        rt.hyperliquid.asset_contexts = ctx
        await collect_flow_snapshot(rt)
        by = _by(await rt.store.flow_snapshot_rows())
        eth, btc = by[("ETH", "hyperliquid", "perp")], by[("BTC", "hyperliquid", "perp")]
        assert eth["missing"] == "funding" and eth["funding_rate"] is None and eth["oi_coins"] is not None
        assert btc["missing"] is None and btc["funding_rate"] is not None
    finally:
        await rt.aclose()


def test_job_off_by_default_and_switch_allow_listed(tmp_path, monkeypatch):
    s = Settings()
    assert s.collect_flow_snapshot_enabled is False and s.collect_flow_snapshot_seconds == 600
    assert allowed("TRADEDESK_COLLECT_FLOW_SNAPSHOT_ENABLED") and allowed("TRADEDESK_COLLECT_FLOW_SNAPSHOT_SECONDS")
    assert allowed("TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED") and allowed("TRADEDESK_COLLECT_LIQUIDATIONS_WINDOW_SECONDS")
    monkeypatch.setenv("TRADEDESK_COLLECT_FLOW_SNAPSHOT_ENABLED", "1")
    assert Settings.from_env().collect_flow_snapshot_enabled is True
    off, _ = _rt(tmp_path, collector_enabled=True)
    on, _ = _rt(tmp_path, collector_enabled=True, collect_flow_snapshot_enabled=True)
    names = lambda rt: {j.name for j in build_default_scheduler(rt).jobs}  # noqa: E731
    assert "flow_snapshot" not in names(off) and "flow_snapshot" in names(on)
    job = next(j for j in build_default_scheduler(on).jobs if j.name == "flow_snapshot")
    assert job.period_seconds == 600 and job.fn is collect_flow_snapshot


async def test_migration_32_adds_the_table_to_a_v31_store(tmp_path):
    path = tmp_path / "v31.sqlite"
    con = sqlite3.connect(path)
    for v in sorted(MIGRATIONS):
        if v <= 31:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 31")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 32
        assert await s.insert_flow_snapshot(HOUR, HOUR + 1, [{"base": "ETH", "venue": "bybit", "market": "spot", "mid": 2333.0}]) == 1
        got = await s.flow_snapshot_rows()
        assert got == [{"ts": HOUR, "base": "ETH", "venue": "bybit", "market": "spot", **dict.fromkeys(s.FLOW_SNAPSHOT_COLUMNS),
                        "mid": 2333.0, "fetched_ts": HOUR + 1}]  # fmt: skip
    finally:
        await s.aclose()
