"""Live fields that are not a finite number, third batch (standing card item 265, BACKLOG 6.53 "лишаються 6").

Before the fix: an infinite start or a 300+-digit turnover of a Bybit daily kline raised OverflowError out of
`demo_vs_backtest.day_turnover`; a 300+-digit Gamma outcome price raised out of `pm_calibration.resolved_outcome` and an
infinite one read as "paid 1"; an infinite time or 300+-digit price of a Polymarket print raised out of
`pm_calibration._yes_prints` / `pm_dislocation.yes_prints`, a NaN price (or a NaN / inf size) passed through; a
300+-digit or infinite open time of a Binance 1m bar raised out of `pm_gap_report.binance_closes` (the pm_gap job); an
infinite Fear & Greed value or time raised out of `sentiment_fear_greed`.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest
from mcp import Client

from test_tools_provider_outage import as_envelope
from test_tools_wrong_type_fields import _failure
from tradedesk.analytics.demo_vs_backtest import DAY_MS, day_turnover
from tradedesk.analytics.pm_calibration import _yes_prints, jump_proximity, resolved_outcome
from tradedesk.analytics.pm_dislocation import yes_prints
from tradedesk.analytics.pm_gap_report import binance_closes
from tradedesk.analytics.sentiment import _norm
from tradedesk.server import build_server

BIG = 10**400  # float() of it raises OverflowError
BAD = [BIG, math.nan, math.inf, -math.inf, None, "abc"]

# ----------------------------------------------------------------------------- demo_vs_backtest.day_turnover

DAY = 1_793_318_400_000  # 2026-10-30 00:00 UTC, ms
NOW = DAY + 2 * DAY_MS


def _kline(start: Any, turnover: Any = "1234.5") -> list[Any]:
    return [start, "1", "1", "1", "1", "10", turnover]


@pytest.mark.parametrize("bad", BAD)
def test_day_turnover_a_bad_turnover_is_unknown(bad):
    assert day_turnover([_kline(str(DAY), bad)], DAY, NOW) is None  # BIG raised OverflowError before


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan, None, "abc"])
def test_day_turnover_skips_a_row_with_a_bad_start(bad):
    assert day_turnover([_kline(bad), _kline(str(DAY))], DAY, NOW) == 1234.5  # int(inf) raised before


# ----------------------------------------------------------------------------- pm_calibration.resolved_outcome


@pytest.mark.parametrize(
    "prices",
    [[BIG, 0], [math.inf, 0], [1, -math.inf], [math.nan, 1], [None, 1], ["abc", 0], "[Infinity, 0]", '["1e400", "0"]'],
)
def test_resolved_outcome_a_bad_price_is_unresolved(prices):
    assert resolved_outcome(prices) is None  # BIG raised, inf / "1e400" read as "the first outcome paid 1"


def test_resolved_outcome_clean_prices_unchanged():
    assert resolved_outcome('["1", "0"]') == 1 and resolved_outcome('["0", "1"]') == 0 and resolved_outcome('["0.5", "0.5"]') is None


# ----------------------------------------------------------------------------- pm_calibration._yes_prints

GOOD_PRINTS = [{"timestamp": 900, "price": 0.3, "asset": "N"}, {"timestamp": 1000, "price": 0.8, "asset": "Y"}]


@pytest.mark.parametrize(
    "bad",
    [{"timestamp": 950, "price": v, "asset": "Y"} for v in BAD]
    + [{"timestamp": v, "price": 0.5, "asset": "Y"} for v in (math.inf, math.nan, None, "x")],
)
def test_calibration_yes_prints_skip_a_bad_print(bad):
    got = _yes_prints([*GOOD_PRINTS, bad], "Y")
    assert got == [(900, pytest.approx(0.7)), (1000, 0.8)]
    # the lookahead check over the same prints does not raise either and sees only the clean jump
    assert jump_proximity([{"t": 990, "p": 0.79}], [*GOOD_PRINTS, bad], "Y") == jump_proximity([{"t": 990, "p": 0.79}], GOOD_PRINTS, "Y")


# ----------------------------------------------------------------------------- pm_dislocation.yes_prints

ROW = {"timestamp": 900, "price": 0.4, "size": 10, "side": "BUY", "asset": "Y", "proxyWallet": "w"}


@pytest.mark.parametrize(
    ("field", "bad"),
    [("price", v) for v in BAD] + [("size", v) for v in BAD] + [("timestamp", v) for v in (math.inf, math.nan, None, "x")],
)
def test_dislocation_yes_prints_skip_a_bad_row(field, bad):
    got = yes_prints([ROW, {**ROW, "timestamp": 950, field: bad}], "Y")
    assert got == [{"t": 900, "p": 0.4, "usd": pytest.approx(4.0), "dir": 1, "wallet": "w"}]
    assert all(math.isfinite(x["usd"]) for x in got)  # a NaN / inf size used to give a NaN / inf notional


# ----------------------------------------------------------------------------- pm_gap_report.binance_closes

END = 1_793_376_000  # a 12:00 ET settle, s


def _bar(open_s: Any, close: Any) -> list[Any]:
    return [open_s, "1", "1", "1", close, "5"]


@pytest.mark.parametrize("bad", BAD)
def test_binance_closes_skip_a_bar_with_a_bad_open_time(bad):
    bars = [_bar(bad, "1"), _bar((END - 60) * 1000, "100.5"), _bar(END * 1000, "101.0")]
    assert binance_closes(bars, END) == (100.5, 101.0)  # BIG / inf raised OverflowError out of the pm_gap job before


@pytest.mark.parametrize("bad", BAD)
def test_binance_closes_a_bad_close_is_absent(bad):
    assert binance_closes([_bar((END - 60) * 1000, "100.5"), _bar(END * 1000, bad)], END) == (100.5, None)


# ----------------------------------------------------------------------------- sentiment: _norm and the tool


@pytest.mark.parametrize(("field", "bad"), [("value", v) for v in BAD] + [("timestamp", v) for v in BAD])
def test_fear_greed_norm_drops_a_bad_reading(field, bad):
    good = {"value": "40", "timestamp": "86400"}
    assert _norm([good, {"value": "50", "timestamp": "172800", field: bad}]) == [{"value": 40, "ts": 86400, "classification": "fear"}]


def _break_fng(rt, field: str, value: Any) -> None:
    orig = rt.alternative_me.fear_greed

    async def fear_greed(limit):
        got = await orig(limit)
        data = copy.deepcopy(got.data)
        data[0][field] = value  # the latest reading
        return got._replace(data=data)

    rt.alternative_me.fear_greed = fear_greed


@pytest.mark.parametrize(("field", "bad"), [("value", v) for v in BAD] + [("timestamp", v) for v in BAD])
async def test_sentiment_fear_greed_drops_a_bad_reading_and_says_so(rt, field, bad):
    _break_fng(rt, field, bad)
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        res = await c.call_tool("sentiment_fear_greed", {"days": 30})
    assert _failure(res) is None, _failure(res)  # inf raised OverflowError out of the tool before
    env = as_envelope(res)
    assert env["ok"] is True and env["data"]["count"] == 29
    assert env["data"]["latest"]["value"] == 30  # the broken latest reading is gone, the day before is the latest
    assert (
        "alternative_me: 1 fear & greed reading(s) with a value or time that is not a finite number (or a value outside 0..100) dropped"
        in env["warnings"]
    )


async def test_sentiment_fear_greed_clean_series_has_no_drop_warning(rt):
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        env = as_envelope(await c.call_tool("sentiment_fear_greed", {"days": 30}))
    assert env["ok"] is True and env["data"]["count"] == 30
    assert not any("fear & greed reading" in w for w in env["warnings"])
