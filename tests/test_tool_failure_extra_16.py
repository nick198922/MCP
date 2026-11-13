"""Invariant 1: the rest of BACKLOG 6.53 "лишаються" after item 308 (standing card item 309).

(a) `pm_threshold._json_list` and `polymarket._maybe_json_list` caught only JSONDecodeError: a JSON integer of 4300+
    digits (ValueError) or deep nesting (RecursionError) in a Gamma list field raised. Now None, as for malformed JSON;
    `polymarket_market` / `polymarket_search` name such a field in `warnings`. On the same path `polymarket._f` raised
    OverflowError on a 300+-digit price (out of both tools) and passed NaN / inf prices into the reply; now None.
(b) `deribit_vrp`: a NaN DVOL close passed float() and turned the vols of the reply into NaN; a bool / out-of-range
    close or timestamp was kept. Such a row is now dropped with a warning; all rows bad -> ok=false.
(c) The six parsers of item 308 read JSON true / false in a numeric field as 1 / 0 (a `true` price as a payout of 1, a
    `true` size as one share). A bool is now junk like a string; no provider sends bools there (Bybit klines,
    alternative.me and Binance klines send strings, Polymarket trades and Hyperliquid send numbers or numeric strings).
"""

from __future__ import annotations

import copy
import itertools
import math
from typing import Any

import pytest

from conftest import NOW, FakeEx, State, make_transports
from test_tool_failure_extra_10 import _session
from tradedesk.analytics.demo_vs_backtest import DAY_MS, day_turnover
from tradedesk.analytics.pm_calibration import _yes_prints, resolved_outcome
from tradedesk.analytics.pm_dislocation import yes_prints
from tradedesk.analytics.pm_gap_report import binance_closes, gamma_outcome
from tradedesk.analytics.pm_threshold import _json_list, outcome_yes
from tradedesk.analytics.polymarket import _f, _maybe_json_list, normalise_market, undecodable_fields
from tradedesk.analytics.sentiment import _norm
from tradedesk.analytics.smart_money import bounded_num, finite_num, parse_portfolio, realized_pnl_windows
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.tools._util import first_nonfinite
from tradedesk.tools.options import _dvol_row

HUGE_INT = "[" + "1" + "0" * 5000 + ", 0]"  # json.loads: ValueError (Exceeds the limit (4300 digits))
DEEP = "[" * 100_000 + "]" * 100_000  # json.loads: RecursionError
D400 = "1" + "0" * 400
_DB = itertools.count()


def _id(v: Any) -> str:
    s = repr(v)
    return s if len(s) <= 24 else f"{type(v).__name__}{len(s)}"


def _rt(tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / f"extra16_{next(_DB)}.sqlite")),
        transports=make_transports(State()),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    return rt


async def _env(rt: Runtime, tool: str, args: dict) -> dict:
    """One call; `_session` fails on an error result or on NaN / Infinity in the reply text."""
    try:
        async with _session(rt) as call:
            env = await call(tool, **args)
    finally:
        await rt.aclose()
    assert first_nonfinite(env["data"]) is None
    return env


# ----------------------------------------------------------------------------- (a) Gamma list fields


@pytest.mark.parametrize("text", [HUGE_INT, DEEP, "[1, 2", "{}", '"x"'], ids=_id)
def test_json_lists_of_garbage_are_none(text):
    assert _json_list(text) is None  # HUGE_INT raised ValueError, DEEP RecursionError before
    assert _maybe_json_list(text) is None
    assert outcome_yes(text) is None
    assert gamma_outcome({"closed": True, "outcomes": text, "outcomePrices": '["1", "0"]'}) is None
    assert gamma_outcome({"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": text}) is None


def test_json_lists_clean_unchanged():
    assert _json_list('["Yes", "No"]') == ["Yes", "No"] == _maybe_json_list('["Yes", "No"]')
    assert _json_list([1]) == [1] == _maybe_json_list([1])
    assert outcome_yes('["1", "0"]') == 1 and outcome_yes('["0", "1"]') == 0


@pytest.mark.parametrize("field", ["outcomes", "outcomePrices", "clobTokenIds"])
@pytest.mark.parametrize("text", [HUGE_INT, DEEP], ids=_id)
def test_normalise_market_survives_an_undecodable_list(field, text):
    raw = {"id": "1", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.6", "0.4"]', "clobTokenIds": '["a", "b"]', field: text}
    m = normalise_market(raw)  # raised before
    assert len(m["outcomes"]) == 2 and undecodable_fields(raw) == [field]


@pytest.mark.parametrize("bad", [D400, "NaN", "Infinity", "-Infinity"], ids=_id)
def test_normalise_market_a_poisoned_price_is_none(bad):
    m = normalise_market({"outcomes": '["Yes", "No"]', "outcomePrices": f"[{bad}, 0.5]"})  # D400 raised OverflowError
    assert [o["price"] for o in m["outcomes"]] == [None, 0.5]
    assert m["outcomes"][1]["price_devigged"] == 1.0  # NaN before


def test_f_and_undecodable_fields_clean():
    assert _f("0.5") == 0.5 and _f(3) == 3.0 and _f(None) is None
    for x in (math.nan, math.inf, "inf", 10**400, "abc", [1]):
        assert _f(x) is None
    assert undecodable_fields({"outcomes": '["a"]', "outcomePrices": None, "clobTokenIds": ""}) == []
    assert undecodable_fields({"outcomes": "not json", "outcomePrices": 5}) == ["outcomes", "outcomePrices"]


def _poison_gamma(rt: Runtime, field: str, value: Any) -> None:
    market, search = rt.polymarket.market, rt.polymarket.search

    def edit(m: Any) -> Any:
        if isinstance(m, dict):
            m[field] = value
        return m

    async def poisoned_market(market_id):
        got = await market(market_id)
        return got._replace(data=edit(copy.deepcopy(got.data)))

    async def poisoned_search(*a, **k):
        got = await search(*a, **k)
        data = copy.deepcopy(got.data)
        if isinstance(data, list) and data:
            edit(data[0])
        return got._replace(data=data)

    rt.polymarket.market = poisoned_market
    rt.polymarket.search = poisoned_search


@pytest.mark.parametrize("tool", ["polymarket_market", "polymarket_search"])
@pytest.mark.parametrize("field", ["outcomes", "outcomePrices", "clobTokenIds"])
@pytest.mark.parametrize("text", [HUGE_INT, DEEP], ids=_id)
async def test_polymarket_tools_warn_on_an_undecodable_list(tmp_path, tool, field, text):
    rt = _rt(tmp_path)
    _poison_gamma(rt, field, text)
    args = {"market_id": "516700"} if tool == "polymarket_market" else {"query": "btc"}
    env = await _env(rt, tool, args)
    assert env["ok"] is True
    assert any(f"{field} not a decodable JSON list" in w for w in env["warnings"]), env["warnings"]


@pytest.mark.parametrize("tool", ["polymarket_market", "polymarket_search"])
@pytest.mark.parametrize("bad", [D400, "NaN", "Infinity"], ids=_id)
async def test_polymarket_tools_survive_a_poisoned_price(tmp_path, tool, bad):
    rt = _rt(tmp_path)
    _poison_gamma(rt, "outcomePrices", f"[{bad}, 0.40]")
    args = {"market_id": "516700"} if tool == "polymarket_market" else {"query": "btc"}
    env = await _env(rt, tool, args)  # D400 raised OverflowError, NaN / Infinity reached the reply before
    assert env["ok"] is True
    m = env["data"]["market"] if tool == "polymarket_market" else env["data"]["markets"][0]
    assert [o["price"] for o in m["outcomes"]] == [None, 0.4]
    assert any("no numeric price" in w for w in env["warnings"])
    assert not any("decodable" in w for w in env["warnings"])


async def test_polymarket_clean_market_has_no_new_warning(tmp_path):
    env = await _env(_rt(tmp_path), "polymarket_market", {"market_id": "516700"})
    assert env["ok"] is True and not any("decodable" in w for w in env["warnings"])


# ----------------------------------------------------------------------------- (b) deribit_vrp DVOL rows

VRP_ARGS = {"currency": "BTC", "history_days": 200}


def _poison_dvol(rt: Runtime, mode: tuple) -> None:
    """Corrupt the DVOL answer: ("drop",), ("row", v), ("close", v), ("ts", v) on the middle row, or ("all", v)."""
    orig = rt.deribit.volatility_index_data

    async def poisoned(*a, **k):
        got = await orig(*a, **k)
        data = copy.deepcopy(got.data)
        rows = data["data"]
        i = len(rows) // 2
        kind, *rest = mode
        if kind == "drop":
            del rows[i]
        elif kind == "row":
            rows[i] = rest[0]
        elif kind == "close":
            rows[i][4] = rest[0]
        elif kind == "ts":
            rows[i][0] = rest[0]
        else:
            for x in rows:
                x[4] = rest[0]
        return got._replace(data=data)

    rt.deribit.volatility_index_data = poisoned


async def _vrp(tmp_path, mode: tuple | None) -> dict:
    rt = _rt(tmp_path)
    if mode:
        _poison_dvol(rt, mode)
    return await _env(rt, "deribit_vrp", VRP_ARGS)


_VRP_DROP_REF: list[dict] = []  # one reply per process: fixed clock, fake transports and a fresh DB make it the same each time


async def _vrp_drop_ref(tmp_path) -> dict:
    """The reference reply with the middle DVOL row dropped, computed once per process (a full deribit_vrp, ~0.3 s)."""
    if not _VRP_DROP_REF:
        _VRP_DROP_REF.append(await _vrp(tmp_path, ("drop",)))
    return copy.deepcopy(_VRP_DROP_REF[0])


CLOSE_POISON: list[Any] = [math.nan, math.inf, -math.inf, 1e308, -1e308, 10**400, None, "abc", "45.0", True, False, 0, -5.0, 1001.0]
TS_POISON: list[Any] = [math.nan, math.inf, 1e308, 10**20, -1, True, None, "abc", 1.5]
ROW_POISON: list[Any] = [None, "abc", 5, {}, [], [1, 2, 3]]


@pytest.mark.parametrize(
    "mode",
    [("close", v) for v in CLOSE_POISON] + [("ts", v) for v in TS_POISON] + [("row", v) for v in ROW_POISON],
    ids=_id,
)
async def test_deribit_vrp_drops_a_poisoned_dvol_row_and_says_so(tmp_path, mode):
    ref = await _vrp_drop_ref(tmp_path)
    env = await _vrp(tmp_path, mode)  # a NaN close gave NaN vols in the reply before
    assert (env["ok"], env["data"], env["missing"]) == (ref["ok"], ref["data"], ref["missing"]), str(env)[:600]
    assert env["ok"] is True
    assert any(w.startswith("dvol: 1 malformed DVOL rows") for w in env["warnings"]), env["warnings"]
    assert not any(w.startswith("dvol:") for w in ref["warnings"])


@pytest.mark.parametrize("bad", [math.nan, math.inf, True, "abc", None], ids=_id)
async def test_deribit_vrp_all_dvol_rows_poisoned_is_not_ok(tmp_path, bad):
    env = await _vrp(tmp_path, ("all", bad))
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [("dvol", "data_unavailable")]
    assert "no well-formed rows" in env["missing"][0]["detail"]
    assert any("malformed DVOL rows" in w for w in env["warnings"])


async def test_deribit_vrp_clean_has_no_dvol_warning(tmp_path):
    env = await _vrp(tmp_path, None)
    assert env["ok"] is True and not any(w.startswith("dvol:") for w in env["warnings"])


def test_dvol_row():
    assert _dvol_row([1_700_000_000_000, 1, 2, 0.5, 45.0]) == (1_700_000_000_000, 0.45)
    assert _dvol_row([1.7e12, 1, 2, 0.5, 45]) == (1_700_000_000_000, 0.45)
    assert _dvol_row((0, 0, 0, 0, 1000.0)) == (0, 10.0)
    for bad in CLOSE_POISON:
        assert _dvol_row([1_700_000_000_000, 1, 2, 0.5, bad]) is None
    for bad in TS_POISON:
        assert _dvol_row([bad, 1, 2, 0.5, 45.0]) is None
    for bad in ROW_POISON:
        assert _dvol_row(bad) is None


# ----------------------------------------------------------------------------- (c) bool in the six parsers of item 308

DAY = 1_793_318_400_000
BOOLS = [True, False]


@pytest.mark.parametrize("flag", BOOLS)
def test_day_turnover_bool_is_junk(flag):
    now = DAY + 2 * DAY_MS
    assert day_turnover([[str(DAY), "1", "1", "1", "1", "10", flag]], DAY, now) is None  # True read as 1.0, False as 0.0
    assert day_turnover([[flag, "1", "1", "1", "1", "10", "9"], [str(DAY), "1", "1", "1", "1", "10", "5"]], DAY, now) == 5.0


@pytest.mark.parametrize("pair", [[True, False], [False, True], [True, 0], [1, False]], ids=_id)
def test_resolved_outcome_bool_is_unresolved(pair):
    assert resolved_outcome(pair) is None  # [true, false] read as "the first outcome paid 1" before
    assert resolved_outcome(str(pair).replace("True", "true").replace("False", "false")) is None


def test_resolved_outcome_numbers_unchanged():
    assert resolved_outcome([1, 0]) == 1 and resolved_outcome([0.0, 1.0]) == 0


GOOD_PRINTS = [{"timestamp": 900, "price": 0.3, "asset": "N"}, {"timestamp": 1000, "price": 0.8, "asset": "Y"}]


@pytest.mark.parametrize(("field", "flag"), [(f, v) for f in ("timestamp", "price") for v in BOOLS], ids=_id)
def test_calibration_yes_prints_bool_is_junk(field, flag):
    clean = _yes_prints(GOOD_PRINTS, "Y")
    assert _yes_prints([*GOOD_PRINTS, {"timestamp": 950, "price": 0.5, "asset": "Y", field: flag}], "Y") == clean


ROW = {"timestamp": 900, "price": 0.4, "size": 10, "side": "BUY", "asset": "Y", "proxyWallet": "w"}


@pytest.mark.parametrize(("field", "flag"), [(f, v) for f in ("timestamp", "price", "size") for v in BOOLS], ids=_id)
def test_dislocation_yes_prints_bool_is_junk(field, flag):
    assert yes_prints([ROW, {**ROW, "timestamp": 950, field: flag}], "Y") == yes_prints([ROW], "Y")  # true size = 1 share


@pytest.mark.parametrize(("pos", "flag"), [(p, v) for p in (0, 1) for v in BOOLS], ids=_id)
def test_binance_closes_bool_is_junk(pos, flag):
    end = 1_793_376_000
    good = [[(end - 60) * 1000, "1", "1", "1", "100.5", "5"]]
    bar = [end * 1000, "1", "1", "1", "101.0", "5"]
    bar[0 if pos == 0 else 4] = flag
    assert binance_closes([*good, bar], end) == (100.5, None)  # a true close was kept as 1.0


@pytest.mark.parametrize(("field", "flag"), [(f, v) for f in ("value", "timestamp") for v in BOOLS], ids=_id)
def test_fear_greed_norm_bool_is_junk(field, flag):
    good = {"value": "40", "timestamp": "86400"}
    assert _norm([good, {"value": "50", "timestamp": "172800", field: flag}]) == _norm([good])


def test_smart_money_bool_is_junk():
    for flag in BOOLS:
        assert finite_num(flag) is None and bounded_num(flag, -10, 10) is None
    assert finite_num("1.5") == 1.5 and finite_num(0) == 0.0


@pytest.mark.parametrize(("pos", "flag"), [(p, v) for p in (0, 1) for v in BOOLS], ids=_id)
def test_parse_portfolio_bool_point_and_vlm_are_dropped(pos, flag):
    point: list[Any] = [1_700_000_000_500, "5.0"]
    point[pos] = flag
    raw = [["week", {"pnlHistory": [[1_700_000_000_000, "2.0"], point], "accountValueHistory": [], "vlm": flag}]]
    dropped: list[str] = []
    p = parse_portfolio(raw, dropped)
    assert p["week"]["pnl"] == [(1_700_000_000_000, 2.0)] and p["week"]["vlm"] == 0.0
    assert sorted(dropped) == sorted([f"week.pnlHistory.{'time' if pos == 0 else 'value'}", "week.vlm"])


def test_parse_portfolio_absent_vlm_still_zero_silently():
    dropped: list[str] = []
    assert parse_portfolio([["day", {"vlm": None}], ["week", {"vlm": ""}]], dropped)["week"]["vlm"] == 0.0
    assert dropped == []


@pytest.mark.parametrize(("field", "flag"), [(f, v) for f in ("time", "closedPnl", "fee") for v in BOOLS], ids=_id)
def test_realized_pnl_windows_bool_fill_is_dropped(field, flag):
    good = [{"time": 5, "closedPnl": "2", "fee": "0.5"}, {"time": 15, "closedPnl": "4"}]
    dropped: list[str] = []
    wins = realized_pnl_windows([*good, {"time": 6, "closedPnl": "3", "fee": "0", field: flag}], [0, 10, 20], 0, dropped=dropped)
    assert wins == [pytest.approx(1.5), pytest.approx(4.0)]  # a true closedPnl added 1, a false fee read as 0
    assert dropped == [field]


def test_realized_pnl_windows_absent_fields_still_zero():
    dropped: list[str] = []
    assert realized_pnl_windows([{"time": 5}, {"time": 6, "closedPnl": "", "fee": None}], [0, 10], 0, dropped=dropped) == [0.0]
    assert dropped == []
