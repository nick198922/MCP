"""Live fields that are not a finite number, second batch (standing card item 263, BACKLOG 6.53 "Лишаються 11").

Before the fix: a 300+-digit `markPrice` in the bulk funding rows raised OverflowError out of the funding scans and
carry_paper_open, a NaN / inf one came out as the mark; the same integer (or an infinite open time) in a Binance daily
bar raised out of the forecast_log job's RV30 model; NaN / inf levels of the Hyperliquid book went into the mid and
the impact walk, and a 300+-digit one raised; a NaN price in Polymarket points / prints came out as the quote; an
infinite or 300+-digit expiry row of the radar archive raised out of the assistant snapshot's radar view.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from test_current_funding_nonfinite import BULK
from test_forecast_log import RV_EXPECTED, DailyEx, _rt
from test_radar import AT_1810, _inputs, _seed_events
from test_review_leftovers_nonfinite import _clean_and_broken, _non_finite
from test_server import call
from test_tools_provider_outage import as_envelope
from test_tools_wrong_type_fields import _failure
from tradedesk.analytics.pm_threshold import last_trade_before, quote_at
from tradedesk.analytics.radar import forward_vols, radar_view
from tradedesk.collector.forecast_log import collect_forecast_log
from tradedesk.core.envelope import Builder
from tradedesk.server import build_server
from tradedesk.tools.forecast_log import _rv30

BIG = 10**400  # float() of it raises OverflowError
MARK = BULK.replace(".fundingRate", ".markPrice")

# ----------------------------------------------------------------------------- perps.current_funding: markPrice


@pytest.mark.parametrize("value", [BIG, math.nan, math.inf])
async def test_funding_spread_scan_names_the_symbol_with_a_non_finite_mark(tmp_path, value):
    sym = "SOL/USDT:USDT"
    clean, res = await _clean_and_broken(tmp_path, "funding_spread_scan", MARK.format("bybit", sym), value, "first")
    assert _failure(res) is None, _failure(res)  # BIG raised OverflowError out of the tool before
    env = as_envelope(res)
    assert env["ok"] is clean["ok"] is True and _non_finite(env) == []
    assert f"bybit: mark price not a finite number for 1 symbols, shown as null: {sym}" in env["warnings"], env["warnings"]
    for row in env["data"]["rows"]:
        if row["symbol"] == sym and "bybit" in row["venues"]:
            assert row["venues"]["bybit"]["mark"] is None


@pytest.mark.parametrize("value", [None, "abc"])
async def test_funding_spread_scan_takes_an_absent_or_text_mark_as_absent(tmp_path, value):
    # not a JSON number: shown as absent without a warning, as before (ccxt sends null when the venue omits it)
    sym = "SOL/USDT:USDT"
    clean, res = await _clean_and_broken(tmp_path, "funding_spread_scan", MARK.format("bybit", sym), value, "first")
    assert _failure(res) is None, _failure(res)
    env = as_envelope(res)
    assert env["ok"] is True and not any("mark price" in w for w in env["warnings"])


# ----------------------------------------------------------------------------- forecast_log._rv30: daily closes


class _BadBarEx(DailyEx):
    """DailyEx whose oldest daily bar gets `bad` at index `col` (0: open time, 4: close)."""

    bad: Any = None
    col = 4

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
        rows = await super().fetch_ohlcv(symbol, timeframe, limit, since, params)
        if timeframe == "1d" and self.id == "binance":
            rows[0][self.col] = self.bad
        return rows


@pytest.mark.parametrize(
    ("col", "bad"), [(4, BIG), (4, math.nan), (4, math.inf), (4, None), (4, "abc"), (0, math.inf), (0, math.nan), (0, "abc")]
)
async def test_rv30_drops_a_bar_with_a_bad_field_and_says_so(tmp_path, col, bad):
    ex = type("BadBar", (_BadBarEx,), {"bad": bad, "col": col})
    rt, _ = _rt(tmp_path, ex=ex)
    DailyEx.down = False
    try:
        b = Builder()
        rv = await _rv30(rt, b, "BTC")  # BIG / an infinite open time raised OverflowError here before
        # the oldest of the 32 completed bars is dropped: 31 closes are still the 30 returns the model needs
        assert rv == pytest.approx(RV_EXPECTED)
        assert b.warnings == ["rv30:BTC: ccxt:binance BTC/USDT 1d: 1 bar(s) with a time or close that is not a finite number dropped"]
        assert await collect_forecast_log(rt) == 8  # the job records the day instead of raising
        assert all(r["rv30"] == pytest.approx(RV_EXPECTED) for r in await rt.store.forecast_list())
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------- hyperliquid_book: levels


def _break_book(rt, side: int, field: str, value: Any) -> None:
    orig = rt.hyperliquid.l2_book

    async def l2_book(coin, n_sig_figs=None):
        got = await orig(coin, n_sig_figs)
        data = copy.deepcopy(got.data)
        data["levels"][side][0][field] = value  # the best level of the side
        return got._replace(data=data)

    rt.hyperliquid.l2_book = l2_book


@pytest.mark.parametrize("field", ["px", "sz", "n"])
@pytest.mark.parametrize("value", [BIG, math.nan, math.inf, "NaN", "abc", None])
async def test_hyperliquid_book_drops_a_level_with_a_bad_field(rt, field, value):
    _break_book(rt, 1, field, value)
    srv, _ = build_server(rt)
    env = await call(srv, "hyperliquid_book", coin="BTC", notional_usd=200_000)
    if field == "n" and value is None:  # an absent order count reads as 0, as before
        assert env["data"]["levels_loaded"] == [20, 20]
        return
    assert env["ok"] is True and _non_finite(env) == []
    assert env["data"]["levels_loaded"] == [20, 19]
    assert env["data"]["asks_top"][0][0] == 82752.0  # the broken best ask is gone, the next one is the best
    assert "hyperliquid BTC book: 1 ask level(s) with a price, size or count that is not a finite number dropped" in env["warnings"]


async def test_hyperliquid_book_a_side_that_is_not_a_list(rt):
    orig = rt.hyperliquid.l2_book

    async def l2_book(coin, n_sig_figs=None):
        got = await orig(coin, n_sig_figs)
        return got._replace(data={**got.data, "levels": [got.data["levels"][0], 5]})

    rt.hyperliquid.l2_book = l2_book
    srv, _ = build_server(rt)
    env = await call(srv, "hyperliquid_book", coin="BTC")  # iterating 5 raised TypeError before
    assert env["ok"] is False and any(m["field"] == "book" and m["reason"] == "data_unavailable" for m in env["missing"])


# ----------------------------------------------------------------------------- pm_threshold: quote_at, last_trade_before


@pytest.mark.parametrize(
    "bad",
    [
        {"t": 940, "p": math.nan},
        {"t": 940, "p": math.inf},
        {"t": 940, "p": BIG},
        {"t": math.inf, "p": 0.5},
        {"t": 940, "p": None},
        {"t": 940, "p": "x"},
    ],
)
def test_quote_at_skips_a_bad_point(bad):
    points = [{"t": 900, "p": 0.41}, bad]
    assert quote_at(points, 1000) == (0.41, 900)  # the newer bad point used to be the quote (NaN) or raise


@pytest.mark.parametrize(
    "bad",
    [
        {"timestamp": 950, "price": math.nan, "asset": "Y"},
        {"timestamp": 950, "price": -math.inf, "asset": "Y"},
        {"timestamp": 950, "price": BIG, "asset": "Y"},
        {"timestamp": math.inf, "price": 0.5, "asset": "Y"},
        {"timestamp": 950, "price": None, "asset": "Y"},
        {"timestamp": 950, "price": "x", "asset": "Y"},
    ],
)
def test_last_trade_before_skips_a_bad_print(bad):
    trades = [{"timestamp": 900, "price": 0.3, "asset": "N"}, bad]
    got = last_trade_before(trades, 1000, "Y")
    assert got == {"t": 900, "age": 100, "yes_px": pytest.approx(0.7), "no_px": pytest.approx(0.3)}


# ----------------------------------------------------------------------------- radar.forward_vols: archive expiries

T0, T1 = 1_793_347_200, 1_793_952_000  # 30.10, 06.11 08:00 UTC


@pytest.mark.parametrize(
    "bad",
    [
        [BIG, 15.0, 1.0, 0.45],
        [math.inf, 15.0, 1.0, 0.45],
        [math.nan, 15.0, 1.0, 0.45],
        ["x", 15.0, 1.0, 0.45],
        [T0 + 1, BIG, 1.0, 0.45],
        [T0 + 1, math.inf, 1.0, 0.45],
        [T0 + 1, None, 1.0, 0.45],
        None,
    ],
)
def test_forward_vols_drops_a_bad_expiry_row_and_reports_it(bad):
    good = [[T0, 10.0, 1.0, 0.4], [T1, 20.0, 1.0, 0.5]]
    missing: list[tuple[str, str]] = []
    got = forward_vols([*good, bad], [], miss=lambda f, r: missing.append((f, r)))
    assert got == forward_vols(good, [])
    assert missing == [("forward_vol", "1 expiry row(s) of the radar archive with a time or days that is not a finite number dropped")]


def test_forward_vols_a_300_digit_iv_is_an_absent_iv():
    # _num used to raise OverflowError on it; now it is no IV, like a null one, and not counted as dropped
    missing: list[Any] = []
    got = forward_vols([[T0, 10.0, 1.0, 0.4], [T1, 20.0, 1.0, 0.5], [T1 + 1, 30.0, 1.0, BIG]], [], miss=lambda *a: missing.append(a))
    assert len(got["windows"]) == 1 and missing == []


def test_radar_view_reports_a_bad_archive_expiry_and_keeps_the_rest():
    rows, dvol, closes = _inputs("BTC", AT_1810)
    events = _seed_events(AT_1810)
    clean = radar_view("BTC", rows, dvol, closes, events, AT_1810)
    broken = copy.deepcopy(rows)
    now_row = next(r for r in broken if r["day"] == "2026-10-18")
    now_row["expiries"].append([math.inf, 400.0, 1.0, 0.5])
    v = radar_view("BTC", broken, dvol, closes, events, AT_1810)  # int(inf) raised OverflowError before
    assert v["forward_vol"] == clean["forward_vol"]
    assert v["missing"] == [
        {"field": "forward_vol", "reason": "1 expiry row(s) of the radar archive with a time or days that is not a finite number dropped"}
    ]
