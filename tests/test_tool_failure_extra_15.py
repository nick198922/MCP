"""Invariant 1: the six live parsers of BACKLOG 6.53 "лишаються" on poisoned rows (standing card item 308).

Items 265 and 276 already catch OverflowError at every listed site (`demo_vs_backtest.day_turnover`,
`pm_calibration.resolved_outcome` / `_yes_prints`, `pm_dislocation.yes_prints`, `smart_money` points and fills,
`sentiment._norm`, `pm_gap_report.binance_closes`); their tests use None / text / NaN / +-inf / 10**400. This file adds
1e308 (finite, so it passed every `math.isfinite` check), 400-digit strings, 4300+-digit JSON integers and rows of the
wrong shape. Found and fixed:

- `day_turnover`: a dict kline raised KeyError; a 1e308 turnover read as the "ge5m" group.
- `resolved_outcome`: a 1e308 (or 5, or -3) outcome price read as "paid 1"; `json_list` raised ValueError on a JSON
  integer of 4300+ digits and RecursionError on deep nesting (out of `classify_market`).
- `_yes_prints`: a 1e308 price made the lookahead check's distances 1e308; a 1e308 / 400-digit time gave a 309-digit
  print time.
- `pm_dislocation.yes_prints`: a 1e308 size gave a 4e307 notional (two of them sum to inf); 1e308 time as above.
- `binance_closes`: a dict bar raised KeyError out of the pm_gap job; a 1e308 / negative close was kept as a close.
- `sentiment_fear_greed`: a 1e308 time became the "latest" reading with a 309-digit `ts`.
- `smart_money`: a 1e308 point value / closedPnl / fee / vlm passed (two of them sum to inf), a 1e308 time too.

Such a row is now skipped (a field named in `dropped` / `warnings` where the function has one); the rest is computed as
without it.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

import pytest
from mcp import Client

from test_tools_provider_outage import as_envelope
from test_tools_wrong_type_fields import _failure
from tradedesk.analytics.demo_vs_backtest import DAY_MS, day_turnover, turnover_group
from tradedesk.analytics.pm_calibration import _yes_prints, classify_market, json_list, lag_profile, resolved_outcome
from tradedesk.analytics.pm_dislocation import notional, yes_prints
from tradedesk.analytics.pm_gap_report import binance_check, binance_closes
from tradedesk.analytics.sentiment import _norm, summarise_fear_greed
from tradedesk.analytics.smart_money import bounded_num, parse_portfolio, realized_pnl_windows
from tradedesk.server import build_server

D400 = "1" + "0" * 400  # float() of it is inf, int() of it is a 401-digit integer
# Every poison of the card: None, text, NaN, +-inf, 1e308 (finite), 10**400 and its digit string, "1e400".
POISON: list[Any] = [None, "abc", math.nan, math.inf, -math.inf, 1e308, -1e308, "1e308", 10**400, D400, "1e400"]
BAD_ROWS: list[Any] = [None, "abc", 5, {}, {"0": 1, "4": 1}, [], [1]]


def _id(v: Any) -> str:
    """Short test id: a 400-digit value is named by its type and length."""
    s = repr(v)
    return s if len(s) <= 24 else f"{type(v).__name__}{len(s)}"


# ----------------------------------------------------------------------------- demo_vs_backtest.day_turnover

DAY = 1_793_318_400_000  # 2026-10-30 00:00 UTC, ms
NOW = DAY + 2 * DAY_MS


def _kline(start: Any, turnover: Any = "1234.5") -> list[Any]:
    return [start, "1", "1", "1", "1", "10", turnover]


@pytest.mark.parametrize("bad", POISON, ids=_id)
def test_day_turnover_a_poisoned_turnover_is_unknown(bad):
    got = day_turnover([_kline(str(DAY), bad)], DAY, NOW)
    assert got is None and turnover_group(got) == "unknown"  # 1e308 read as "ge5m" before


@pytest.mark.parametrize("bad", POISON, ids=_id)
def test_day_turnover_skips_a_poisoned_start(bad):
    assert day_turnover([_kline(bad, "9"), _kline(str(DAY))], DAY, NOW) == 1234.5


@pytest.mark.parametrize("row", BAD_ROWS, ids=_id)
def test_day_turnover_skips_a_row_of_the_wrong_shape(row):
    assert day_turnover([row, _kline(str(DAY))], DAY, NOW) == 1234.5  # a dict raised KeyError before


def test_day_turnover_clean_rows_unchanged():
    assert day_turnover([_kline(str(DAY - DAY_MS), "1"), _kline(str(DAY), "6e6")], DAY, NOW) == 6e6
    assert day_turnover([_kline(str(DAY), "0")], DAY, NOW) == 0.0


# ----------------------------------------------------------------------------- pm_calibration.resolved_outcome / json_list


@pytest.mark.parametrize("bad", POISON + [5, -3, "1.5"], ids=_id)
def test_resolved_outcome_a_poisoned_price_is_unresolved(bad):
    assert resolved_outcome([bad, 0]) is None  # 1e308 / 5 read as "the first outcome paid 1" before
    assert resolved_outcome([1, bad]) is None  # -3 / -1e308 read as "paid 1" too
    assert resolved_outcome(json.dumps([str(bad), "0"])) is None


@pytest.mark.parametrize("text", ["[" + "1" + "0" * 5000 + ", 0]", "[" * 100_000 + "]" * 100_000], ids=_id)
def test_json_list_huge_integer_or_deep_nesting_is_none(text):
    assert json_list(text) is None  # ValueError (4300+ digits) / RecursionError raised before
    assert resolved_outcome(text) is None


def test_classify_market_survives_a_poisoned_gamma_field():
    event = {"id": "e", "slug": "e", "tags": ["politics"]}
    market = {"id": "m", "outcomes": '["Yes", "No"]', "clobTokenIds": '["1", "2"]'}
    for field in ("outcomes", "clobTokenIds", "outcomePrices"):
        poisoned = {**market, "outcomePrices": '["1", "0"]', field: "[" + "9" * 5000 + "]"}
        assert classify_market(event, poisoned)["status"] in ("tokens", "unresolved")  # raised ValueError before


def test_resolved_outcome_clean_prices_unchanged():
    assert resolved_outcome('["1", "0"]') == 1 and resolved_outcome('["0", "1"]') == 0
    assert resolved_outcome('["0.9995", "0.0005"]') == 1  # within tol
    assert resolved_outcome('["0.5", "0.5"]') is None


# ----------------------------------------------------------------------------- pm_calibration._yes_prints

GOOD_PRINTS = [{"timestamp": 900, "price": 0.3, "asset": "N"}, {"timestamp": 1000, "price": 0.8, "asset": "Y"}]
CLEAN = [(900, pytest.approx(0.7)), (1000, 0.8)]


@pytest.mark.parametrize(("field", "bad"), [("price", v) for v in POISON] + [("timestamp", v) for v in POISON], ids=_id)
def test_calibration_yes_prints_skip_a_poisoned_print(field, bad):
    poisoned = {"timestamp": 950, "price": 0.5, "asset": "Y", field: bad}
    assert _yes_prints([*GOOD_PRINTS, poisoned], "Y") == CLEAN  # 1e308 price / time passed before
    points = [{"t": 1000, "p": 0.79}]
    assert lag_profile(points, [*GOOD_PRINTS, poisoned], "Y", [0, 60]) == lag_profile(points, GOOD_PRINTS, "Y", [0, 60])


@pytest.mark.parametrize("row", BAD_ROWS, ids=_id)
def test_calibration_yes_prints_skip_a_row_of_the_wrong_shape(row):
    assert _yes_prints([*GOOD_PRINTS, row], "Y") == CLEAN


def test_calibration_yes_prints_price_out_of_range_is_skipped():
    for px in (-0.1, 1.1):
        assert _yes_prints([*GOOD_PRINTS, {"timestamp": 950, "price": px, "asset": "Y"}], "Y") == CLEAN
    assert _yes_prints([*GOOD_PRINTS, {"timestamp": -1, "price": 0.5, "asset": "Y"}], "Y") == CLEAN


# ----------------------------------------------------------------------------- pm_dislocation.yes_prints

ROW = {"timestamp": 900, "price": 0.4, "size": 10, "side": "BUY", "asset": "Y", "proxyWallet": "w"}
ONE = [{"t": 900, "p": 0.4, "usd": pytest.approx(4.0), "dir": 1, "wallet": "w"}]


@pytest.mark.parametrize(("field", "bad"), [(f, v) for f in ("price", "size", "timestamp") for v in POISON], ids=_id)
def test_dislocation_yes_prints_skip_a_poisoned_row(field, bad):
    got = yes_prints([ROW, {**ROW, "timestamp": 950, field: bad}, {**ROW, "timestamp": 960, field: bad}], "Y")
    assert got == ONE  # a 1e308 size gave a 4e307 notional (two of them: inf), a 1e308 time a 309-digit t
    assert notional(got, 0, 10**4) == pytest.approx(4.0)


@pytest.mark.parametrize("row", BAD_ROWS, ids=_id)
def test_dislocation_yes_prints_skip_a_row_of_the_wrong_shape(row):
    assert yes_prints([ROW, row], "Y") == ONE


# ----------------------------------------------------------------------------- pm_gap_report.binance_closes

END = 1_793_376_000  # a 12:00 ET settle, s
GOOD_BARS = [[(END - 60) * 1000, "1", "1", "1", "100.5", "5"], [END * 1000, "1", "1", "1", "101.0", "5"]]


@pytest.mark.parametrize("bad", POISON + [0, -100.5, "-1"], ids=_id)
def test_binance_closes_a_poisoned_close_is_absent(bad):
    bars = [GOOD_BARS[0], [END * 1000, "1", "1", "1", bad, "5"]]
    got = binance_closes(bars, END)
    assert got == (100.5, None)  # a 1e308 / negative close was kept before
    assert binance_check(1, 100.0, *got) == "match"  # the clean bar still checks the result


@pytest.mark.parametrize("bad", POISON, ids=_id)
def test_binance_closes_skip_a_poisoned_open_time(bad):
    assert binance_closes([[bad, "1", "1", "1", "999", "5"], *GOOD_BARS], END) == (100.5, 101.0)


@pytest.mark.parametrize("row", BAD_ROWS, ids=_id)
def test_binance_closes_skip_a_bar_of_the_wrong_shape(row):
    assert binance_closes([row, *GOOD_BARS], END) == (100.5, 101.0)  # a dict raised KeyError out of the pm_gap job


# ----------------------------------------------------------------------------- sentiment: _norm and the tool


@pytest.mark.parametrize(("field", "bad"), [("value", v) for v in POISON] + [("timestamp", v) for v in POISON], ids=_id)
def test_fear_greed_norm_drops_a_poisoned_reading(field, bad):
    good = {"value": "40", "timestamp": "86400"}
    assert _norm([good, {"value": "50", "timestamp": "172800", field: bad}]) == [{"value": 40, "ts": 86400, "classification": "fear"}]


@pytest.mark.parametrize("row", BAD_ROWS, ids=_id)
def test_fear_greed_norm_drops_a_row_of_the_wrong_shape(row):
    assert summarise_fear_greed([{"value": "40", "timestamp": "86400"}, row])["count"] == 1


def _break_fng(rt, field: str, value: Any) -> None:
    orig = rt.alternative_me.fear_greed

    async def fear_greed(limit):
        got = await orig(limit)
        data = copy.deepcopy(got.data)
        data[0][field] = value  # the latest reading
        return got._replace(data=data)

    rt.alternative_me.fear_greed = fear_greed


@pytest.mark.parametrize(("field", "bad"), [("timestamp", v) for v in (1e308, "1e308", -1e308, D400)] + [("value", 1e308)], ids=_id)
async def test_sentiment_fear_greed_drops_a_poisoned_reading_and_says_so(rt, field, bad):
    _break_fng(rt, field, bad)
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        res = await c.call_tool("sentiment_fear_greed", {"days": 30})
    assert _failure(res) is None, _failure(res)
    env = as_envelope(res)
    assert env["ok"] is True and env["data"]["count"] == 29
    assert env["data"]["latest"]["value"] == 30  # a 1e308 time made the broken reading the latest before
    assert all(len(str(r["ts"])) <= 12 for r in env["data"]["series"])
    assert any("1 fear & greed reading(s)" in w and "1970..9999" in w for w in env["warnings"])


# ----------------------------------------------------------------------------- smart_money: portfolio points and fills


def test_bounded_num():
    assert bounded_num("1.5", 0, 2) == 1.5 and bounded_num(0, 0, 0) == 0.0
    for x in (*POISON, 3, -1):
        assert bounded_num(x, 0, 2) is None


@pytest.mark.parametrize(("pos", "bad"), [(0, v) for v in POISON] + [(1, v) for v in POISON], ids=_id)
def test_parse_portfolio_skips_a_poisoned_point(pos, bad):
    point = [1_700_000_000_500, "5.0"]
    point[pos] = bad
    raw = [["week", {"pnlHistory": [[1_700_000_000_000, "2.0"], point], "accountValueHistory": [[1_700_000_000_000, "9"]], "vlm": "7"}]]
    dropped: list[str] = []
    p = parse_portfolio(raw, dropped)
    assert p == {"week": {"pnl": [(1_700_000_000_000, 2.0)], "value": [(1_700_000_000_000, 9.0)], "vlm": 7.0}}  # 1e308 passed before
    assert dropped == [f"week.pnlHistory.{'time' if pos == 0 else 'value'}"]


@pytest.mark.parametrize("bad", [v for v in POISON if v is not None] + [-5], ids=_id)
def test_parse_portfolio_a_poisoned_vlm_reads_as_zero_and_is_named(bad):
    dropped: list[str] = []
    p = parse_portfolio([["week", {"pnlHistory": [], "accountValueHistory": [], "vlm": bad}]], dropped)
    assert p["week"]["vlm"] == 0.0 and dropped == ["week.vlm"]  # a 1e308 vlm passed before


@pytest.mark.parametrize(("field", "bad"), [(f, v) for f in ("time", "closedPnl", "fee") for v in POISON if v is not None], ids=_id)
def test_realized_pnl_windows_skip_a_poisoned_fill(field, bad):
    good = [{"time": 5, "closedPnl": "2", "fee": "0.5"}, {"time": 15, "closedPnl": "4"}]
    poisoned = {"time": 6, "closedPnl": "3", "fee": "0", field: bad}
    dropped: list[str] = []
    wins = realized_pnl_windows([*good, poisoned, dict(poisoned)], [0, 10, 20], 0, dropped=dropped)
    assert wins == [pytest.approx(1.5), pytest.approx(4.0)]  # two 1e308 closedPnl summed to inf before
    assert dropped == [field, field]
