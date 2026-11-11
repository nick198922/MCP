"""Invariant 1: corrupt bars and rows from a live provider (item 305).

Every tool was called with a fake venue that answers one bar / book level / trade / funding row with None, text, NaN,
+-inf, 1e308 or a 400-digit integer in one field (ccxt fetch_ohlcv / fetch_order_book / fetch_trades /
fetch_funding_rate_history, Hyperliquid candleSnapshot / recentTrades, Binance daily klines). Found and fixed:

- ccxt OHLCV: market_ohlcv raised (log of an inf / negative close, float() of a 400-digit close, a null / text row)
  and passed NaN / Infinity through `rows`; chart_markup raised (inf low, 1e308 timestamp) and answered Infinity on a
  1e308 price or volume; deribit_vrp raised on an inf / 1e308 close and gave NaN on a NaN one;
  range_detect / market_regime (live) gave Infinity / NaN; funding_spread_scan gave range_7d = Infinity on an inf high;
  strategy_backtest_momentum and market_regime answered ok=true on all-NaN closes; range_setup_scan on all-bad bars too.
- hyperliquid_candles: NaN / Infinity in rows and summary, and raised on an infinite or negative close and on all-zero
  closes; hyperliquid_trades raised on a null row and summed a "NaN" price.
- market_orderbook: NaN / Infinity in mid and depth, raised on a 400-digit price or size.
- market_trades: raised on a NaN / inf timestamp, a 400-digit field or a null row; NaN / Infinity in the sums.
- funding history: a finite 1e308 rate raised out of perps_funding_history and gave Infinity / NaN in funding_carry_scan,
  funding_spread_scan and carry_paper_open / carry_paper_status.
- cycle_regime: two daily closes of 1e308 overflowed math.fsum (OverflowError out of the tool).

Such a row is now dropped with a warning; the answer equals the one without that row (all bad -> ok=false).
"""

from __future__ import annotations

import itertools
import json

import httpx
import pytest

from conftest import NOW, FakeEx, State, make_transports
from test_cycle_regime import SPOT, CycleEx, _futures_handler, _klines
from test_tool_failure_extra_10 import _session
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.tools._util import bar_row, first_nonfinite, hl_bar_row

NAN, INF, HUGE, BIG = float("nan"), float("inf"), 1e308, 10**400
SYM = "BTC/USDT:USDT"
_DB = itertools.count()


def _rt(tmp_path, ex: type = FakeEx, transports: dict | None = None) -> Runtime:
    """A runtime with its own store file: carry_paper_open writes a pair, the reference run must not see it."""
    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / f"extra14_{next(_DB)}.sqlite")),
        transports=transports or make_transports(State()),
        exchange_factory=ex,
    )
    rt.now = lambda: NOW
    return rt


async def _env(rt: Runtime, tool: str, args: dict) -> dict:
    """One call; `_session` fails the test on an error result or on NaN / Infinity in the reply text."""
    try:
        async with _session(rt) as call:
            env = await call(tool, **args)
    finally:
        await rt.aclose()
    assert first_nonfinite(env["data"]) is None
    return env


def _same(env: dict, ref: dict) -> None:
    assert (env["ok"], env["data"], env["missing"]) == (ref["ok"], ref["data"], ref["missing"]), str(env)[:800]


def _edit(rows: list, mode: tuple, col: dict, all_col: object) -> list:
    """Corrupt the middle row: ("drop",), ("row", v), ("cell", field, v) or ("all", v) (every row's `all_col`)."""
    if not rows or not mode:
        return rows
    i = len(rows) // 2
    kind, *rest = mode
    if kind == "drop":
        del rows[i]
    elif kind == "row":
        rows[i] = rest[0]
    elif kind == "cell":
        rows[i][col[rest[0]]] = rest[1]
    else:
        for x in rows:
            x[all_col] = rest[0]
    return rows


# ---------- ccxt OHLCV ----------

OHLCV_COL = {"ts": 0, "open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}


def _bar_ex(mode: tuple) -> type:
    class BarEx(FakeEx):
        async def fetch_ohlcv(self, *a, **k):
            return _edit([list(x) for x in await super().fetch_ohlcv(*a, **k)], mode, OHLCV_COL, 4)

    return BarEx


BAR_TOOLS = {
    "market_ohlcv": {"exchange": "binanceusdm", "symbol": SYM},
    "chart_markup": {"exchange": "binanceusdm", "symbol": SYM},
    "deribit_vrp": {},
    "range_detect": {"venue": "binanceusdm", "symbol": SYM, "source": "live"},
    "range_setup": {"venue": "binanceusdm", "symbol": SYM},
    "range_setup_scan": {"venue": "binanceusdm", "symbols": [SYM]},
    "market_regime": {"venue": "binanceusdm", "symbol": SYM, "source": "live"},
    "strategy_backtest_momentum": {"exchange": "binanceusdm", "symbol": SYM},
    "funding_spread_scan": {},
}
BAD_BARS = {
    **{f"close_{k}": ("cell", "close", v) for k, v in (("none", None), ("text", "abc"), ("nan", NAN), ("inf", INF))},
    **{f"close_{k}": ("cell", "close", v) for k, v in (("neg_inf", -INF), ("1e308", HUGE), ("big_int", BIG), ("negative", -5.0))},
    "high_inf": ("cell", "high", INF),
    "low_1e308": ("cell", "low", HUGE),
    "ts_nan": ("cell", "ts", NAN),
    "ts_1e308": ("cell", "ts", HUGE),
    "volume_nan": ("cell", "volume", NAN),
    "volume_1e308": ("cell", "volume", HUGE),
    "row_none": ("row", None),
    "row_text": ("row", "abc"),
}  # fmt: skip


@pytest.mark.parametrize("tool", BAR_TOOLS)
@pytest.mark.parametrize("mode", BAD_BARS.values(), ids=BAD_BARS.keys())
async def test_a_corrupt_live_bar_is_dropped_with_a_warning(tmp_path, tool, mode):
    ref = await _env(_rt(tmp_path, _bar_ex(("drop",))), tool, BAR_TOOLS[tool])
    env = await _env(_rt(tmp_path, _bar_ex(mode)), tool, BAR_TOOLS[tool])
    _same(env, ref)
    assert any("malformed" in w and "dropped" in w for w in env["warnings"]), env["warnings"]


@pytest.mark.parametrize("tool", BAR_TOOLS)
@pytest.mark.parametrize("value", ["abc", NAN, INF, HUGE], ids=["text", "nan", "inf", "1e308"])
async def test_all_live_bars_corrupt(tmp_path, tool, value):
    clean = await _env(_rt(tmp_path), tool, BAR_TOOLS[tool])
    env = await _env(_rt(tmp_path, _bar_ex(("all", value))), tool, BAR_TOOLS[tool])
    if tool == "funding_spread_scan":  # bars only feed the range_7d flag: the scan stands, without it
        ccxt_rows = [i for i, x in enumerate(clean["data"]["rows"]) if x["short_venue"] != "hyperliquid"]
        assert env["ok"] is clean["ok"] is True and ccxt_rows
        assert all(clean["data"]["rows"][i]["range_7d"] is not None and env["data"]["rows"][i]["range_7d"] is None for i in ccxt_rows)
        assert any("range_7d" in w for w in env["warnings"])
    else:  # before: ok=true on all-NaN closes (market_regime, strategy_backtest_momentum), NaN / Infinity, or raised
        # range_setup is ok=false on the clean FakeEx series too (no setup), so the check is on the corrupt answer only
        assert env["ok"] is False and env["missing"], str(env)[:600]
    if tool == "range_setup_scan":
        assert env["data"]["rejected"] == [{"symbol": SYM, "reason": "malformed_bars"}]


def test_bar_row_accepts_venue_rows_and_rejects_garbage():
    assert bar_row([1791637200000, 1, 2.0, 0.5, 1.5, None]) == [1791637200000, 1.0, 2.0, 0.5, 1.5, None]
    assert bar_row((1.7916372e12, 1.0, 2.0, 0.5, 1.5)) == [1791637200000, 1.0, 2.0, 0.5, 1.5]
    for bad in ([1, 1.0, 1.0, 1.0], [True, 1.0, 1.0, 1.0, 1.0], [-1, 1.0, 1.0, 1.0, 1.0], [2**62, 1.0, 1.0, 1.0, 1.0]):
        assert bar_row(bad) is None
    for v in (None, "1.0", True, 0.0, -1.0, NAN, INF, 1e13, BIG):
        assert bar_row([1, v, 1.0, 1.0, 1.0]) is None, v
    for v in ("1.0", -1.0, NAN, INF, 1e21, BIG):
        assert bar_row([1, 1.0, 1.0, 1.0, 1.0, v]) is None, v
    assert hl_bar_row({"t": 5, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10"}) == [5, 1.0, 2.0, 0.5, 1.5, 10.0]
    for k, v in (("c", "NaN"), ("c", "Infinity"), ("c", "-5"), ("o", "1e400"), ("v", BIG), ("t", "5"), ("h", None)):
        assert hl_bar_row({"t": 5, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", k: v}) is None, (k, v)


# ---------- Hyperliquid candles and trades (strings over HTTP) ----------


def _hl_transports(kind: str, mode: tuple, col: dict, all_col: str) -> dict:
    tr = make_transports(State())
    inner = tr["hyperliquid"].handler

    def handler(req: httpx.Request) -> httpx.Response:
        resp = inner(req)
        if json.loads(req.content).get("type") != kind:
            return resp
        rows = _edit(resp.json(), mode, col, all_col)
        return httpx.Response(200, content=json.dumps(rows).encode())  # NaN / Infinity tokens on purpose

    tr["hyperliquid"] = httpx.MockTransport(handler)
    return tr


HL_CANDLE_COL = {"ts": "t", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}
BAD_CANDLES = {
    **{f"close_{k}": ("cell", "close", v) for k, v in (("nan", "NaN"), ("inf", "Infinity"), ("neg_inf", -INF), ("negative", "-5"))},
    **{f"close_{k}": ("cell", "close", v) for k, v in (("zero", "0"), ("1e308", HUGE), ("text", "abc"), ("none", None))},
    "open_1e400": ("cell", "open", "1e400"),
    "high_big_int": ("cell", "high", BIG),
    "volume_nan": ("cell", "volume", NAN),
    "ts_inf": ("cell", "ts", INF),
    "row_none": ("row", None),
}  # fmt: skip
HL_ARGS = {"coin": "BTC", "hours_back": 24.0}


@pytest.mark.parametrize("mode", BAD_CANDLES.values(), ids=BAD_CANDLES.keys())
async def test_hyperliquid_candles_drop_a_corrupt_candle(tmp_path, mode):
    def tr(m: tuple) -> dict:
        return _hl_transports("candleSnapshot", m, HL_CANDLE_COL, "c")

    ref = await _env(_rt(tmp_path, transports=tr(("drop",))), "hyperliquid_candles", HL_ARGS)
    env = await _env(_rt(tmp_path, transports=tr(mode)), "hyperliquid_candles", HL_ARGS)
    _same(env, ref)
    assert any(w.startswith("1 malformed candles") for w in env["warnings"]), env["warnings"]


@pytest.mark.parametrize("value", ["0", "NaN", "-5", HUGE], ids=["zero", "nan", "negative", "1e308"])
async def test_hyperliquid_candles_all_corrupt_is_not_ok(tmp_path, value):
    tr = _hl_transports("candleSnapshot", ("all", value), HL_CANDLE_COL, "c")
    env = await _env(_rt(tmp_path, transports=tr), "hyperliquid_candles", HL_ARGS)  # all "0" raised IndexError before
    assert env["ok"] is False and [m["detail"] for m in env["missing"]] == ["no parsable candles"]


HL_TRADE_COL = {"price": "px", "amount": "sz"}


@pytest.mark.parametrize(
    "mode", [("cell", "price", "NaN"), ("cell", "amount", "Infinity"), ("cell", "price", "1e400"), ("row", None)], ids=str
)
async def test_hyperliquid_trades_drop_a_corrupt_print(tmp_path, mode):
    def tr(m: tuple) -> dict:
        return _hl_transports("recentTrades", m, HL_TRADE_COL, "px")

    ref = await _env(_rt(tmp_path, transports=tr(("drop",))), "hyperliquid_trades", {"coin": "BTC"})
    env = await _env(_rt(tmp_path, transports=tr(mode)), "hyperliquid_trades", {"coin": "BTC"})  # a null row raised
    assert env["ok"] is ref["ok"] is True and env["data"]["summary"] == ref["data"]["summary"]


async def test_hyperliquid_trades_all_corrupt_is_not_ok(tmp_path):
    env = await _env(
        _rt(tmp_path, transports=_hl_transports("recentTrades", ("all", "NaN"), HL_TRADE_COL, "px")), "hyperliquid_trades", {"coin": "BTC"}
    )
    assert env["ok"] is False and env["missing"][0]["detail"] == "no usable trades"


# ---------- ccxt order book and trades ----------


def _book_ex(mode: tuple) -> type:
    class BookEx(FakeEx):
        async def fetch_order_book(self, symbol, limit=100):
            book = await super().fetch_order_book(symbol, limit)
            return {**book, "bids": _edit([list(x) for x in book["bids"]], mode, {"price": 0, "size": 1}, 0)}

    return BookEx


BOOK_ARGS = {"exchange": "binanceusdm", "symbol": SYM, "depth": 20, "notional_usd": 1e6}
BAD_LEVELS = {
    **{f"price_{k}": ("cell", "price", v) for k, v in (("nan", NAN), ("inf", INF), ("1e308", HUGE), ("big_int", BIG), ("text", "abc"))},
    **{f"size_{k}": ("cell", "size", v) for k, v in (("nan", NAN), ("neg_inf", -INF), ("1e308", HUGE), ("big_int", BIG))},
    "row_none": ("row", None),
}  # fmt: skip


@pytest.mark.parametrize("mode", BAD_LEVELS.values(), ids=BAD_LEVELS.keys())
async def test_market_orderbook_drops_a_corrupt_level(tmp_path, mode):
    ref = await _env(_rt(tmp_path, _book_ex(("drop",))), "market_orderbook", BOOK_ARGS)
    env = await _env(_rt(tmp_path, _book_ex(mode)), "market_orderbook", BOOK_ARGS)
    _same(env, ref)
    assert any(w.startswith("1 malformed book levels") for w in env["warnings"]), env["warnings"]


@pytest.mark.parametrize("value", [None, NAN, HUGE], ids=["none", "nan", "1e308"])
async def test_market_orderbook_a_side_without_one_valid_level_is_not_ok(tmp_path, value):
    env = await _env(_rt(tmp_path, _book_ex(("all", value))), "market_orderbook", BOOK_ARGS)  # None: ok=true, mid null
    assert env["ok"] is False and env["missing"][0]["detail"] == "no valid level on one side of the book"


def _trades_ex(mode: tuple) -> type:
    class TradesEx(FakeEx):
        async def fetch_trades(self, symbol, since=None, limit=200):
            rows = [dict(x) for x in await super().fetch_trades(symbol, since, limit)]
            return _edit(rows, mode, {"price": "price", "amount": "amount", "ts": "timestamp", "cost": "cost"}, "price")

    return TradesEx


TRADES_ARGS = {"exchange": "binanceusdm", "symbol": SYM}


@pytest.mark.parametrize(
    "mode",
    [("cell", "price", NAN), ("cell", "price", BIG), ("cell", "amount", INF), ("cell", "amount", HUGE), ("row", None), ("row", "abc")],
    ids=str,
)
async def test_market_trades_drops_a_corrupt_print(tmp_path, mode):
    ref = await _env(_rt(tmp_path, _trades_ex(("drop",))), "market_trades", TRADES_ARGS)
    env = await _env(_rt(tmp_path, _trades_ex(mode)), "market_trades", TRADES_ARGS)
    assert env["ok"] is ref["ok"] is True and env["data"]["summary"] == ref["data"]["summary"]


@pytest.mark.parametrize("value", [NAN, INF, -INF, HUGE, BIG], ids=["nan", "inf", "neg_inf", "1e308", "big_int"])
async def test_market_trades_a_bad_timestamp_reads_as_absent(tmp_path, value):
    clean = await _env(_rt(tmp_path), "market_trades", TRADES_ARGS)
    env = await _env(_rt(tmp_path, _trades_ex(("cell", "ts", value))), "market_trades", TRADES_ARGS)  # NaN / inf raised
    s, c = env["data"]["summary"], clean["data"]["summary"]
    assert s["count"] == c["count"] and s["quote_volume"] == c["quote_volume"] and len(s["cvd_series"]) == len(c["cvd_series"]) - 1


@pytest.mark.parametrize("value", [NAN, HUGE], ids=["nan", "1e308"])
async def test_market_trades_all_corrupt_is_not_ok(tmp_path, value):
    env = await _env(_rt(tmp_path, _trades_ex(("all", value))), "market_trades", TRADES_ARGS)  # 1e308: ok=true, Infinity sums
    assert env["ok"] is False and env["missing"][0]["detail"].startswith("no usable trades")


# ---------- funding history rate of 1e308 ----------


def _funding_ex(mode: tuple) -> type:
    class FundingEx(FakeEx):
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            rows = [dict(x) for x in await super().fetch_funding_rate_history(symbol, since, limit, params)]
            return _edit(rows, mode, {"rate": "fundingRate"}, "fundingRate")

    return FundingEx


FUNDING_TOOLS = {
    "perps_funding_history": {"base": "BTC"},
    "funding_carry_scan": {},
    "funding_spread_scan": {},
    "carry_paper_open": {"base": "BTC", "long_venue": "binanceusdm", "short_venue": "bybit"},
}


@pytest.mark.parametrize("tool", FUNDING_TOOLS)
@pytest.mark.parametrize("value", [HUGE, -HUGE, 2.0], ids=["1e308", "neg_1e308", "200pct"])
async def test_a_funding_rate_beyond_100_pct_is_dropped(tmp_path, tool, value):
    # 1e308 raised out of perps_funding_history and gave Infinity / NaN in the scans and carry_paper_open.
    ref = await _env(_rt(tmp_path, _funding_ex(("drop",))), tool, FUNDING_TOOLS[tool])
    env = await _env(_rt(tmp_path, _funding_ex(("cell", "rate", value))), tool, FUNDING_TOOLS[tool])
    _same(env, ref)
    assert any("funding rows" in w and "dropped" in w for w in env["warnings"]), env["warnings"]


# ---------- cycle_regime: daily closes of 1e308 ----------


def _spot(closes: dict[int, float]):
    def handler(req: httpx.Request) -> httpx.Response:
        return _klines(closes, req) if req.url.path == "/api/v3/klines" else httpx.Response(404)

    return handler


async def test_cycle_regime_drops_closes_beyond_range(tmp_path):
    days = sorted(SPOT)[-40:-38]  # two closes of 1e308 overflowed math.fsum in SMA365 (OverflowError out of the tool)
    bad = {**SPOT, **dict.fromkeys(days, HUGE)}
    gone = {t: c for t, c in SPOT.items() if t not in days}

    def rt(spot: dict[int, float]) -> Runtime:
        tr = {"binance_spot": httpx.MockTransport(_spot(spot)), "binance_futures": httpx.MockTransport(_futures_handler)}
        return _rt(tmp_path, CycleEx, transports=tr)

    ref = await _env(rt(gone), "cycle_regime", {})
    env = await _env(rt(bad), "cycle_regime", {})
    _same(env, ref)
    assert env["ok"] is True and "spot_klines: 2 klines without a close that is a finite number in range dropped" in env["warnings"]
