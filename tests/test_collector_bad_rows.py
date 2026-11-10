"""Collector jobs on corrupt provider rows (item 306; invariant 1 and the quality of the forward data).

Each job that turns a provider answer into DB rows runs on its usual fake provider (the fixtures of its own test file)
with fields replaced by None, text, NaN, +-inf, 1e308, a 400-digit integer or (time fields) 10**20 ms. One run per bad
value corrupts every probed field at once, each in its own place, and is compared with a reference run on the clean
answer where those fields are NULL or those rows absent. The job must not raise, the corrupt field or row is left out,
the other rows are written as without it, and no NaN / inf reaches the DB. Found and fixed (before -> now):

- market_data (carry paper feed + carry_paper tools): `parse_bar` raised OverflowError on an inf time or a 400-digit
  field and kept NaN / inf / 1e308 prices and a 10**20 time; `funding_entries` kept a 1e308 rate and a 10**20 time.
- tradfi_spread: an inf time raised out of the job (no rows that minute), 1e308 / 10**20 times overflowed SQLite.
- coingecko_trending: a 400-digit number raised; 1e308 / 10**20 ids and ranks overflowed SQLite; 1e308 USD stored.
- hl_oi_cap: "NaN" / "Infinity" / 1e308 stored as inf (oi_usd = OI x mark), a 400-digit number raised.
- flow_snapshot: a 400-digit number lost the whole row; a 1e308 level crossed the book (no mid), 1e308 sizes / OI /
  funding gave inf depth, oi_usd and annualised funding; a text long/short ratio was NULL without `missing`.
- option_xvenue / option_legs: a NaN / inf quote time raised out of the job, 1e308 / 10**20 times overflowed SQLite,
  a 1e308 price / size / IV was stored; option_legs' open interest / volume raised on a 400-digit number.
- coinalyze_history: a 1e308 close overflowed the series stdev (OverflowError out of the job).
- radar_archive: one print with a text / NaN / None-row field raised out of build_surface and failed the run.
- assistant_snapshot (radar section): an inf DVOL / close time voided the section; NaN / inf / 1e308 values were used.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

import test_coinalyze_history_collector as czh
import test_coingecko_trending as cgt
import test_cycle_regime as tcr
import test_flow_snapshot as tfsnap
import test_hl_oi_cap as hoc
import test_option_legs as olg
import test_option_xvenue as oxv
import test_radar as trd
import test_tradfi_spread as tfs
from conftest import NOW as CONF_NOW
from conftest import FakeEx, State, make_transports
from tradedesk.collector.assistant_snapshot import build_radar
from tradedesk.collector.coinalyze_history import CoinalyzeHistoryJob
from tradedesk.collector.coingecko_trending import collect_coingecko_trending
from tradedesk.collector.cycle_regime_log import collect_cycle_regime_log
from tradedesk.collector.flow_snapshot import collect_flow_snapshot
from tradedesk.collector.hl_oi_cap import collect_hl_oi_cap
from tradedesk.collector.market_data import bars_4h, funding_entries, parse_bar
from tradedesk.collector.option_legs import collect_option_legs
from tradedesk.collector.option_xvenue import collect_option_xvenue
from tradedesk.collector.radar_archive import collect_radar_archive
from tradedesk.collector.tradfi_spread import TradfiSpreadJob
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder, Fetched
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.tools._util import first_nonfinite

NAN, INF, HUGE, BIG, FAR_MS = float("nan"), float("inf"), 1e308, 10**400, 10**20
BAD = {"none": None, "text": "abc", "nan": NAN, "inf": INF, "neg_inf": -INF, "1e308": HUGE, "big_int": BIG}
BAD_TS = {**BAD, "far_ms": FAR_MS}  # 10**20 is a time probe: as a value it is within some bounds (1e20)
_DROP = object()  # "the row absent": the reference runs


def _finite(rows: object) -> None:
    assert first_nonfinite(rows) is None, first_nonfinite(rows)


def _diff(got: dict, ref: dict) -> dict:
    """The differing columns of keyed rows, for the assertion message."""
    return {
        k: {c: (v, ref.get(k, {}).get(c)) for c, v in r.items() if v != ref.get(k, {}).get(c)} for k, r in got.items() if r != ref.get(k)
    }


_REFS: dict[tuple, object] = {}


async def _ref(key: tuple, run: Callable[[], Awaitable[Any]]) -> Any:
    """The reference run of a job (it does not depend on the bad value): once per test process, a deep copy each time."""
    if key not in _REFS:
        _REFS[key] = await run()
    return copy.deepcopy(_REFS[key])


@pytest.fixture(autouse=True)
def _nan_json(monkeypatch):
    """httpx refuses NaN / Infinity in ``json=``: the fakes send them as JSON tokens, as a broken venue would."""
    monkeypatch.setattr(httpx._content, "json_dumps", lambda obj, **kw: json.dumps(obj, **{**kw, "allow_nan": True}))


def _put(obj: object, path: tuple, value: object) -> None:
    """``obj[path[0]][path[1]]... = value``; ``_DROP`` deletes that item instead."""
    for k in path[:-1]:
        obj = obj[k]  # type: ignore[index]
    if value is _DROP:
        del obj[path[-1]]  # type: ignore[attr-defined]
    else:
        obj[path[-1]] = value  # type: ignore[index]


# ---------- market_data: ccxt 4h bars and funding rows (carry paper feed and the carry_paper tools) ----------

GOOD_BAR = [1791637200000, 82750.0, 82832.75, 82667.25, 82750.0, 10.0]


@pytest.mark.parametrize("col", [0, 2, 3, 4], ids=["ts", "high", "low", "close"])
@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
def test_parse_bar_rejects_a_corrupt_field(col, value):
    row = list(GOOD_BAR)
    row[col] = value
    assert parse_bar(row) is None  # inf time / 400 digits raised; NaN, inf, 1e308 and a 10**20 time passed


def test_parse_bar_keeps_a_good_row():
    assert parse_bar(GOOD_BAR) == {"ts": 1791637200000, "high": 82832.75, "low": 82667.25, "close": 82750.0}
    assert parse_bar(None) is None and parse_bar("abc") is None and parse_bar([1, 2]) is None


def _bar_ex(col: int, value: object) -> type:
    class BarEx(FakeEx):
        async def fetch_ohlcv(self, *a, **k):
            rows = [list(x) for x in await super().fetch_ohlcv(*a, **k)]
            i = len(rows) // 2
            if value is _DROP:
                del rows[i]
            elif col < 0:
                rows[i] = value
            else:
                rows[i][col] = value
            return rows

    return BarEx


async def _bars(ex: type) -> list:
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0), transports=make_transports(State()), exchange_factory=ex)
    try:
        now_ms = 1791637620000
        return await bars_4h(rt, Builder(), "binanceusdm", "BTC/USDT:USDT", now_ms - 30 * 4 * 3_600_000, now_ms)
    finally:
        await rt.aclose()


@pytest.mark.parametrize("col", [-1, 0, 4], ids=["row", "ts", "close"])
@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
async def test_bars_4h_drops_one_corrupt_bar(col, value):
    ref = await _ref(("bars", col), lambda: _bars(_bar_ex(col, _DROP)))
    got = await _bars(_bar_ex(col, value))
    assert got == ref and len(ref) >= 20
    _finite(got)


FUNDING = [{"timestamp": 1791600000000 + i * 28_800_000, "fundingRate": 0.0001 * (i + 1)} for i in range(5)]
FUNDING_CASES = {
    **{f"timestamp-{k}": ("timestamp", v) for k, v in BAD_TS.items()},
    **{f"rate-{k}": ("fundingRate", v) for k, v in {**BAD_TS, "200pct": 2.0, "neg_200pct": -2.0}.items()},
}


@pytest.mark.parametrize("case", FUNDING_CASES.values(), ids=FUNDING_CASES.keys())
def test_funding_entries_drop_one_corrupt_row(case, caplog):
    field, value = case
    rows = copy.deepcopy(FUNDING)
    rows[2][field] = value
    ref = [{"ts_ms": r["timestamp"], "rate": r["fundingRate"]} for i, r in enumerate(FUNDING) if i != 2]
    with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
        assert funding_entries(rows) == ref  # a 1e308 rate (inf carry) and a 10**20 time (SQLite overflow) passed
    if value is not None:  # None: a row without a rate or time is missing, not malformed
        assert "funding_entries: 1 of 5 funding row(s) dropped" in caplog.text


def test_funding_entries_without_a_rate_bound_for_the_tool_warning():
    # tools/carry_paper counts the rates beyond +-100 % itself (its warning), so it reads them unbounded
    assert funding_entries([dict(FUNDING[0], fundingRate=2.0)], max_rate=None) == [{"ts_ms": FUNDING[0]["timestamp"], "rate": 2.0}]


# ---------- tradfi_spread: Binance bookTicker / premiumIndex ----------

TRADFI = {  # (answer, symbol, field) -> stored column
    ("book_ticker", "SPYUSDT", "bidPrice"): "bid",
    ("book_ticker", "AAPLUSDT", "askPrice"): "ask",
    ("book_ticker", "XAUUSDT", "time"): "book_ms",
    ("premium_index", "SPYUSDT", "markPrice"): "mark_price",
    ("premium_index", "CLUSDT", "indexPrice"): "index_price",
    ("premium_index", "AAPLUSDT", "time"): "premium_ms",
}


async def _tradfi_rows(tmp_path, monkeypatch, name: str, sample: dict) -> dict:
    monkeypatch.setattr(tfs, "SAMPLE", sample)
    (tmp_path / name).mkdir()
    rt, _ = tfs._rt(tmp_path / name)
    try:
        assert await TradfiSpreadJob()(rt) == len(tfs.KEPT)
        rows = await rt.store.tradfi_spread_rows()
    finally:
        await rt.aclose()
    _finite(rows)
    return {r["symbol"]: r for r in rows}


@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
async def test_tradfi_spread_corrupt_fields_are_null(tmp_path, monkeypatch, value, caplog):
    clean = tfs.SAMPLE
    ref = await _ref(("tradfi",), lambda: _tradfi_rows(tmp_path, monkeypatch, "ref", clean))
    bad = copy.deepcopy(clean)
    for (part, sym, field), col in TRADFI.items():
        next(r for r in bad[part] if r.get("symbol") == sym)[field] = value
        ref[sym] = dict(ref[sym], **{col: None})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
        got = await _tradfi_rows(tmp_path, monkeypatch, "bad", bad)  # an inf time raised out of the job
    assert got == ref, _diff(got, ref)
    assert value is None or f"tradfi_spread: {len(TRADFI)} malformed field(s)" in caplog.text


# ---------- coingecko_trending ----------

CG = {  # (position, part, field) -> stored column (None: price_btc falls back to data.price_btc)
    (1, "item", "coin_id"): "coin_num",
    (2, "item", "market_cap_rank"): "market_cap_rank",
    (3, "item", "score"): "score",
    (4, "item", "price_btc"): None,
    (4, "data", "price"): "price_usd",
    (5, "data", "market_cap"): "market_cap_usd",
    (6, "data", "total_volume"): "volume_usd",
}


async def _cg_rows(tmp_path, name: str, body: dict) -> dict:
    (tmp_path / name).mkdir()
    rt, _ = cgt._rt(tmp_path / name, opts={"body": body})
    try:
        assert await collect_coingecko_trending(rt) == len(cgt.IDS)
        rows = await rt.store.coingecko_trending_rows()
    finally:
        await rt.aclose()
    _finite(rows)
    return {r["rank"]: r for r in rows}


@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
async def test_coingecko_trending_corrupt_fields_are_null(tmp_path, value, caplog):
    ref = await _ref(("coingecko",), lambda: _cg_rows(tmp_path, "ref", cgt.SAMPLE))
    bad = copy.deepcopy(cgt.SAMPLE)
    for (rank, part, field), col in CG.items():
        item = bad["coins"][rank - 1]["item"]
        (item if part == "item" else item["data"])[field] = value
        if col:
            ref[rank] = dict(ref[rank], **{col: None})
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
        got = await _cg_rows(tmp_path, "bad", bad)  # 400 digits raised; 1e308 / 10**20 ints overflowed SQLite
    assert got == ref, _diff(got, ref)
    assert value is None or "6 malformed numeric field(s) stored as NULL" in caplog.text


# ---------- hl_oi_cap: metaAndAssetCtxs (decimal strings) ----------

HL = {0: ("funding", ["funding"]), 1: ("markPx", ["mark", "oi_usd"]), 2: ("oraclePx", ["oracle"]), 4: ("dayNtlVlm", ["vol24h"])}
HL_OI = (1, "openInterest")  # ETH: mark and OI both corrupt, one oi_usd
HL_BAD = {**BAD, "nan_text": "NaN", "inf_text": "Infinity", "1e400_text": "1e400"}


async def _hl_rows(tmp_path, monkeypatch, name: str, sample: dict) -> dict:
    monkeypatch.setattr(hoc, "SAMPLE", sample)
    (tmp_path / name).mkdir()
    rt, _ = hoc._rt(tmp_path / name)
    try:
        assert await collect_hl_oi_cap(rt) == len(hoc.LISTED)
        rows = await rt.store.hl_oi_cap_rows()
    finally:
        await rt.aclose()
    _finite(rows)
    return {r["coin"]: r for r in rows}


@pytest.mark.parametrize("value", HL_BAD.values(), ids=HL_BAD.keys())
async def test_hl_oi_cap_corrupt_fields_are_null(tmp_path, monkeypatch, value, caplog):
    clean = hoc.SAMPLE
    ref = await _ref(("hl_oi_cap",), lambda: _hl_rows(tmp_path, monkeypatch, "ref", clean))
    bad = copy.deepcopy(clean)
    meta, ctxs = bad["meta_and_asset_ctxs"]
    for i, (field, cols) in [*HL.items(), (HL_OI[0], (HL_OI[1], ["oi_usd"]))]:
        ctxs[i][field] = value
        coin = meta["universe"][i]["name"]
        ref[coin] = dict(ref[coin], **dict.fromkeys(cols))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
        got = await _hl_rows(tmp_path, monkeypatch, "bad", bad)  # "NaN" / "Infinity" / 1e308 stored inf; 400 digits raised
    assert got == ref, _diff(got, ref)
    if value is not None and value != "abc" and value is not BIG:  # these three are None from the provider already
        assert "malformed field(s) (NaN / inf / beyond bounds) stored as NULL" in caplog.text


# ---------- flow_snapshot: ccxt / OKX / Hyperliquid books, OI, funding, long/short ----------

BOOK_LEVELS = {  # a book level: the row equals the one without that level
    ("ccxt_order_book", "binanceusdm|BTC/USDT:USDT", "bids", 3): 0,
    ("ccxt_order_book", "binanceusdm|ETH/USDT:USDT", "asks", 2): 1,
    ("hl_l2", "BTC|4", "levels", 0, 3): "px",
    ("hl_l2", "BTC|4", "levels", 1, 5): "sz",
    ("hl_l2", "ETH|3", "levels", 0, 2): "sz",
}
FLOW_FIELDS = {  # path -> (row key, the columns that turn NULL, the name added to `missing`)
    ("ccxt_open_interest", "binanceusdm|BTC/USDT:USDT", "openInterestAmount"): (("BTC", "binance", "perp"), ("oi_coins", "oi_usd"), "oi"),
    ("ccxt_funding_rate", "bybit|ETH/USDT:USDT", "fundingRate"):
        (("ETH", "bybit", "perp"), ("funding_rate", "funding_hours", "funding_annualized"), "funding"),
    ("binance_ls", "ETHUSDT", 0, "longAccount"): (("ETH", "binance", "perp"), ("long_share",), "long_share"),
    ("bybit_ls", "BTCUSDT", "result", "list", 0, "buyRatio"): (("BTC", "bybit", "perp"), ("long_share",), "long_share"),
    ("okx", "open-interest|ETH-USDT-SWAP", "data", 0, "oiCcy"): (("ETH", "okx", "perp"), ("oi_coins", "oi_usd"), "oi"),
    ("okx", "ls|BTC-USDT-SWAP", "data", 0, 1): (("BTC", "okx", "perp"), ("long_share",), "long_share"),
    ("hl_meta_and_asset_ctxs", 1, 0, "openInterest"): (("BTC", "hyperliquid", "perp"), ("oi_coins", "oi_usd"), None),
    ("hl_meta_and_asset_ctxs", 1, 1, "funding"):
        (("ETH", "hyperliquid", "perp"), ("funding_rate", "funding_hours", "funding_annualized"), "funding"),
}  # fmt: skip
FLOW_BAD = {**BAD, "nan_text": "NaN", "1e400_text": "1e400"}


async def _flow_rows(tmp_path, monkeypatch, name: str, fx: dict) -> dict:
    monkeypatch.setattr(tfsnap, "FX", fx)
    (tmp_path / name).mkdir()
    rt, _ = tfsnap._rt(tmp_path / name)
    try:
        assert await collect_flow_snapshot(rt) == 14
        rows = await rt.store.flow_snapshot_rows()
    finally:
        await rt.aclose()
    _finite(rows)
    return {(r["base"], r["venue"], r["market"]): r for r in rows}


@pytest.mark.parametrize("value", FLOW_BAD.values(), ids=FLOW_BAD.keys())
async def test_flow_snapshot_corrupt_fields(tmp_path, monkeypatch, value):
    clean = tfsnap.FX
    ref_fx, bad = copy.deepcopy(clean), copy.deepcopy(clean)
    for path, field in sorted(BOOK_LEVELS.items(), key=lambda x: -x[0][-1]):  # higher index first: deletes do not shift
        _put(ref_fx, path, _DROP)
        _put(bad, (*path, field), value)
    for path in FLOW_FIELDS:
        _put(bad, path, value)
    ref = await _ref(("flow",), lambda: _flow_rows(tmp_path, monkeypatch, "ref", ref_fx))
    for key, cols, miss in FLOW_FIELDS.values():
        names = {*(ref[key]["missing"] or "").split(","), *([miss] if miss else [])} - {""}
        ref[key] = dict(ref[key], **dict.fromkeys(cols), missing=",".join(sorted(names)) or None)
    got = await _flow_rows(tmp_path, monkeypatch, "bad", bad)
    assert got == ref, _diff(got, ref)


# ---------- option_xvenue: Deribit public/ticker and Bybit v5 option tickers ----------

XV_DERIBIT = {  # instrument -> (field, stored column)
    "BTC-23OCT26-82000-C": ("best_bid_price", "d_bid"),
    "BTC-23OCT26-82000-P": ("best_ask_amount", "d_ask_size"),
    "BTC-23OCT26-81500-C": ("mark_iv", "d_mark_iv"),
    "BTC-23OCT26-82500-P": ("index_price", "d_index"),
    "BTC-23OCT26-81000-C": ("timestamp", "d_ts_ms"),
}
XV_BYBIT = {  # symbol -> (field, stored column)
    "BTC-23OCT26-82000-C-USDT": ("bid1Price", "b_bid"),
    "BTC-23OCT26-82000-P-USDT": ("ask1Size", "b_ask_size"),
    "BTC-23OCT26-81500-P-USDT": ("markIv", "b_mark_iv"),
    "BTC-23OCT26-82500-C-USDT": ("indexPrice", "b_index"),
}


async def _xv_rows(tmp_path, monkeypatch, name: str, sample: dict) -> dict:
    monkeypatch.setattr(oxv, "SAMPLE", sample)
    (tmp_path / name).mkdir()
    rt, _ = oxv._rt(tmp_path / name)
    try:
        assert await collect_option_xvenue(rt) == 14
        rows = await rt.store.option_xvenue_rows()
    finally:
        await rt.aclose()
    _finite(rows)
    return {(r["strike"], r["opt"]): r for r in rows}


def _xv_key(name: str) -> tuple[float, str]:
    parts = name.split("-")
    return float(parts[2]), parts[3]


@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
@pytest.mark.parametrize("bybit_time", [False, True], ids=["legs", "bybit_time"])
async def test_option_xvenue_corrupt_fields_are_null(tmp_path, monkeypatch, value, bybit_time):
    clean = oxv.SAMPLE
    ref = await _ref(("xvenue",), lambda: _xv_rows(tmp_path, monkeypatch, "ref", clean))
    bad = copy.deepcopy(clean)
    if bybit_time:  # the response time: every Bybit leg of the snapshot
        bad["bybit"]["time"] = value
        ref = {k: dict(r, b_ts_ms=None) if r["b_symbol"] else r for k, r in ref.items()}
    else:
        for name, (field, col) in XV_DERIBIT.items():
            bad["deribit_ticker"][name][field] = value
            ref[_xv_key(name)] = dict(ref[_xv_key(name)], **{col: None})
        for sym, (field, col) in XV_BYBIT.items():
            next(r for r in bad["bybit"]["result"]["list"] if r["symbol"] == sym)[field] = value
            ref[_xv_key(sym)] = dict(ref[_xv_key(sym)], **{col: None})
    got = await _xv_rows(tmp_path, monkeypatch, "bad", bad)  # a NaN / inf time raised out of the job
    assert got == ref, _diff(got, ref)


# ---------- option_legs: Deribit public/ticker of the condor legs ----------

LEGS = {  # instrument (BTC legs of the fixture's book) -> (ticker field, the stored columns that turn NULL)
    "BTC-30OCT26-78000-P": ("timestamp", ["quote_ts_ms"]),
    "BTC-30OCT26-85000-C": ("best_bid_price", ["bid"]),
    "BTC-30OCT26-70000-P": ("best_ask_amount", ["ask_size"]),  # lp and lp15
    "BTC-30OCT26-83000-P": ("mark_iv", ["mark_iv", "delta"]),
    "BTC-30OCT26-83000-C": ("bid_iv", ["bid_iv"]),
    "BTC-30OCT26-80000-P": ("open_interest", ["open_interest"]),
    "BTC-30OCT26-88000-C": ("stats.volume", ["volume"]),
}


async def _leg_rows(tmp_path, name: str) -> dict:
    (tmp_path / name).mkdir()
    rt, _ = olg._rt(tmp_path / name)
    try:
        assert await collect_option_legs(rt) == 20
        rows = await rt.store.option_leg_rows()
    finally:
        await rt.aclose()
    _finite(rows)
    return {(r["currency"], r["role"]): r for r in rows}


@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
async def test_option_legs_corrupt_fields_are_null(tmp_path, monkeypatch, value):
    ref = await _ref(("legs",), lambda: _leg_rows(tmp_path, "ref"))
    ticker = olg._ticker

    def bad_ticker(row: dict) -> dict:
        t = ticker(row)
        field, _ = LEGS.get(row["instrument_name"], (None, None))
        if field == "stats.volume":
            t["stats"]["volume"] = value
        elif field:
            t[field] = value
        return t

    monkeypatch.setattr(olg, "_ticker", bad_ticker)
    got = await _leg_rows(tmp_path, "bad")  # a NaN / inf quote time raised out of the job
    hit = {name: [k for k, r in ref.items() if r["instrument"] == name] for name in LEGS}
    assert all(hit.values()), hit  # every probed instrument is a leg of the fixture's book
    for name, keys in hit.items():
        for k in keys:
            ref[k] = dict(ref[k], **dict.fromkeys(LEGS[name][1]))
    assert got == ref, _diff(got, ref)


# ---------- coinalyze_history: daily OI / funding / long-short bars ----------

CZ_FIELDS = {"time": {"oi_usd": "t", "funding": "t", "long_short": "t"}, "value": {"oi_usd": "c", "funding": "h", "long_short": "r"}}
CZ_CASES = {f"{kind}-{k}": (kind, v) for kind in CZ_FIELDS for k, v in (BAD_TS if kind == "time" else BAD).items()}


async def _cz_rows(tmp_path, name: str, kind: str, value: object) -> tuple[int, dict]:
    """The job on the Binance sample with the second-last BTC bar of every series corrupt in its ``kind`` field."""
    clean = czh.sample_handler()

    def handler(req: httpx.Request) -> httpx.Response:
        resp = clean(req)
        if req.url.params["symbols"] != "BTCUSDT_PERP.A":
            return resp
        body = resp.json()
        hist = body[0]["history"]
        _put(hist, (-2,) if value is _DROP else (-2, CZ_FIELDS[kind][czh.PATHS[req.url.path]]), value)
        return httpx.Response(200, content=json.dumps(body).encode())  # NaN / Infinity tokens on purpose

    (tmp_path / name).mkdir()
    rt, _ = czh.make_rt(tmp_path / name, handler, collect_coinalyze_history_backfill_days=10)
    try:
        written = await CoinalyzeHistoryJob()(rt)
        rows = {(s, m): await rt.store.coinalyze_history_rows(s, m) for s in ("BTCUSDT_PERP.A", "ETHUSDT_PERP.A") for m in czh.METRICS}
    finally:
        await rt.aclose()
    _finite(list(rows.values()))
    return written, rows


@pytest.mark.parametrize("case", CZ_CASES.values(), ids=CZ_CASES.keys())
async def test_coinalyze_history_drops_corrupt_bars(tmp_path, case, caplog):
    kind, value = case
    n_ref, ref = await _ref(("coinalyze", kind), lambda: _cz_rows(tmp_path, "ref", kind, _DROP))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
        n, got = await _cz_rows(tmp_path, "bad", kind, value)  # a 1e308 close overflowed the stdev out of the job
    assert (n, got) == (n_ref, ref) and n == 21
    assert caplog.text.count("dropped 1 malformed or repeated bar(s)") == 3  # BTC oi_usd, funding, long_short


async def test_clean_samples_log_no_malformed_rows(tmp_path, monkeypatch, caplog):
    # the dropped-row warnings must stay quiet on the real venue samples (else they are noise in the collector log)
    with caplog.at_level(logging.WARNING, logger="tradedesk.collector"):
        await _tradfi_rows(tmp_path, monkeypatch, "tradfi", tfs.SAMPLE)
        await _cg_rows(tmp_path, "cg", cgt.SAMPLE)
        await _hl_rows(tmp_path, monkeypatch, "hl", hoc.SAMPLE)
        (tmp_path / "cz").mkdir()
        rt, _ = czh.make_rt(tmp_path / "cz", czh.sample_handler(), collect_coinalyze_history_backfill_days=10)
        try:
            assert await CoinalyzeHistoryJob()(rt) > 0
        finally:
            await rt.aclose()
        assert funding_entries(FUNDING) == [{"ts_ms": r["timestamp"], "rate": r["fundingRate"]} for r in FUNDING]
    assert "malformed" not in caplog.text and "dropped" not in caplog.text, caplog.text


# ---------- radar_archive: Deribit option prints (history.deribit.com) ----------

RADAR_VICTIMS = {19: "timestamp", 3: "index_price", 7: "iv", 11: "price", 25: "mark_price", 30: "row"}  # print # -> field


def _radar_rt(tmp_path, name: str, value: object, fields: dict) -> Runtime:
    """test_radar's fake history.deribit.com (20 prints a page) with the prints ``fields`` of every window corrupt
    (#19 is the last of the first page: the paging cursor meets it). ``value`` _DROP: those prints absent;
    "next_day": their field is the window end + 1 day."""

    def history(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        asset, start, end = q["currency"], int(q["start_timestamp"]), int(q["end_timestamp"])
        full = trd._window_trades(asset, end)
        victims = {full[i]["trade_id"]: f for i, f in fields.items()}
        rows = []
        for t in full:
            if not start <= t["timestamp"] <= end:
                continue
            f = victims.get(t["trade_id"])
            if f is not None:
                if value is _DROP:
                    continue
                v = end + 86_400_000 if value == "next_day" else value  # a plausible time, a day after the window
                t = v if f == "row" else dict(t, **{f: v})
            rows.append(t)
        body = {"jsonrpc": "2.0", "result": {"trades": rows[:20], "has_more": len(rows) > 20}}
        return httpx.Response(200, content=json.dumps(body).encode())  # NaN / Infinity tokens on purpose

    tr = make_transports(State())
    tr["deribit_history"] = httpx.MockTransport(history)
    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / f"{name}.sqlite")), transports=tr, exchange_factory=FakeEx
    )
    rt.now = lambda: trd.NOW
    rt.deribit_history.http._limiter = TokenBucket(1_000.0, 1_000)
    return rt


async def _radar_rows(rt: Runtime) -> list:
    try:
        assert await collect_radar_archive(rt) == 14  # 7 days x BTC, ETH
        rows = await rt.store.radar_list("BTC") + await rt.store.radar_list("ETH")
    finally:
        await rt.aclose()
    _finite(rows)
    return rows


@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
async def test_radar_archive_drops_corrupt_prints(tmp_path, value):
    fields = RADAR_VICTIMS
    if value is None:  # None in iv / price / mark_price is an absent field: such a print still counts (n_prints, spot)
        fields = {i: f for i, f in RADAR_VICTIMS.items() if f not in ("iv", "price", "mark_price")}
    ref = await _ref(("radar", value is None), lambda: _radar_rows(_radar_rt(tmp_path, "ref", _DROP, fields)))
    got = await _radar_rows(_radar_rt(tmp_path, "bad", value, fields))  # one bad print raised out of build_surface
    assert got == ref


async def test_radar_archive_cursor_ignores_a_print_after_the_window(tmp_path):
    # print #5 (mid first page) timed a day after the window: as the paging cursor (max over the page) it jumped past
    # end_ms and the second page (prints #20..#35) was never fetched
    ref = await _radar_rows(_radar_rt(tmp_path, "ref", _DROP, {5: "timestamp"}))
    got = await _radar_rows(_radar_rt(tmp_path, "bad", "next_day", {5: "timestamp"}))
    assert got == ref


# ---------- cycle_regime_log: Binance daily klines and funding history ----------

KLINE_PATHS = {"/api/v3/klines": -3, "/fapi/v1/klines": -4, "/fapi/v1/premiumIndexKlines": -5}  # path -> corrupt row
CYCLE_KINDS = {"time": (0, "timestamp"), "value": (4, "fundingRate")}  # kline column, funding field


def _cycle_rt(tmp_path, name: str, kind: str, value: object) -> Runtime:
    """test_cycle_regime's real Binance responses of 28.10 with one spot, perp and premium kline and one funding row
    of the last week corrupt in their ``kind`` field (``value`` _DROP: those rows absent)."""
    col, ffield = CYCLE_KINDS[kind]

    def daily(req: httpx.Request) -> httpx.Response:
        resp = tcr.serve_daily(req)
        if resp is None:
            return httpx.Response(404, text="not mocked")
        target = tcr.FX_ROWS[req.url.path][KLINE_PATHS[req.url.path]][0]
        rows = [r for r in resp.json() if not (value is _DROP and r[0] == target)]
        for r in rows:
            if r[0] == target:
                r[col] = value
        return httpx.Response(200, content=json.dumps(rows).encode())  # NaN / Infinity tokens on purpose

    class Ex(tcr.FixtureEx):
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = await super().fetch_funding_rate_history(symbol, since, limit, params)
            target = int(tcr.FX["binance_funding_rate"][-9]["fundingTime"])
            hit = [x for x in rows if x["timestamp"] == target]
            return [x for x in rows if x not in hit] if value is _DROP else [dict(x, **{ffield: value}) if x in hit else x for x in rows]

    tr = {"binance_spot": httpx.MockTransport(daily), "binance_futures": httpx.MockTransport(daily)}
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=str(tmp_path / f"{name}.sqlite")), transports=tr, exchange_factory=Ex)
    rt.now = lambda: tcr.FX_NOW
    return rt


async def _cycle_rows(rt: Runtime) -> list:
    try:
        n = await collect_cycle_regime_log(rt)
        rows = await rt.store.cycle_regime_log_rows("BTC")
    finally:
        await rt.aclose()
    _finite(rows)
    assert n == len(rows)
    return [{k: v for k, v in r.items() if k != "created_ts"} for r in rows]


@pytest.mark.parametrize("kind", CYCLE_KINDS)
@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
async def test_cycle_regime_log_drops_corrupt_rows(tmp_path, kind, value):
    ref = await _ref(("cycle", kind), lambda: _cycle_rows(_cycle_rt(tmp_path, "ref", kind, _DROP)))
    got = await _cycle_rows(_cycle_rt(tmp_path, "bad", kind, value))  # no defect here: 305 bounded the closes
    assert got == ref and ref  # a missing spot day leaves the days whose windows need it undecided


# ---------- assistant_snapshot: the radar section's daily DVOL (Deribit) and closes (Binance) ----------


async def _radar_section(col: int, value: object) -> dict:
    """build_radar on the conftest fakes with the 10th-last daily DVOL row and the 12th-last daily close corrupt in
    column ``col`` (-1: the row itself; ``value`` _DROP: those rows absent)."""

    def edit(rows: list, i: int) -> list:
        rows = [list(x) for x in rows]
        _put(rows, (i,) if value is _DROP or col < 0 else (i, col), value)
        return rows

    class Ex(FakeEx):
        async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
            rows = await super().fetch_ohlcv(symbol, timeframe, limit, since, params)
            return edit(rows, -12) if timeframe == "1d" else rows

    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=""), transports=make_transports(State()), exchange_factory=Ex)
    rt.now = lambda: CONF_NOW
    dvol = rt.deribit.volatility_index_data

    async def bad_dvol(*a, **k):
        f = await dvol(*a, **k)
        return Fetched({**f.data, "data": edit(f.data["data"], -10)}, f.evidence)

    rt.deribit.volatility_index_data = bad_dvol
    try:
        sec = await build_radar(rt, {}, CONF_NOW)
    finally:
        await rt.aclose()
    assert sec["ok"], sec["missing"]
    _finite(sec["data"])
    return sec["data"]


@pytest.mark.parametrize("col", [0, 4, -1], ids=["time", "value", "row"])
@pytest.mark.parametrize("value", BAD_TS.values(), ids=BAD_TS.keys())
async def test_assistant_radar_section_drops_corrupt_rows(col, value):
    ref = await _ref(("radar_section", col), lambda: _radar_section(col, _DROP))
    got = await _radar_section(col, value)  # an inf time raised out of _radar_dvol: the whole section was lost
    assert got == ref
