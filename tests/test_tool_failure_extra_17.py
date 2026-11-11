"""Invariant 1: the leftovers named after item 309 (standing card item 310, review live-float-guard-4 remark 3).

(a) `deribit_dvol` parsed every DVOL row with float(): a NaN close passed and turned mean / stdev / min / max into
    NaN; inf / 1e308 / a bool / an out-of-range close was kept, and a non-list `data` field raised out of the tool. A
    bad row is now dropped by the same check as in `deribit_vrp` (`_dvol_pct_row`) with a warning; all bad -> ok=false.
(b) `negrisk._jlist` caught only JSONDecodeError: a JSON integer of 4300+ digits (ValueError) or deep nesting
    (RecursionError) in a Gamma list field raised out of `classify_event`. Now an empty list, as for malformed JSON.
(c) `pm_threshold.outcome_yes` read `[true, false]` as "YES paid 1" (float(True) == 1.0). A bool entry is now junk.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from test_tool_failure_extra_16 import CLOSE_POISON, DEEP, HUGE_INT, ROW_POISON, TS_POISON, _env, _id, _poison_dvol, _rt
from tradedesk.analytics.negrisk import _jlist, classify_event
from tradedesk.analytics.pm_gap_report import gamma_outcome
from tradedesk.analytics.pm_threshold import outcome_yes
from tradedesk.runtime import Runtime
from tradedesk.tools.options import _dvol_pct_row, _dvol_row

# ----------------------------------------------------------------------------- (a) deribit_dvol rows

DVOL_ARGS = {"currency": "BTC", "hours_back": 168, "resolution": 3600}


async def _dvol(tmp_path, mode: tuple | None) -> dict:
    rt = _rt(tmp_path)
    if mode:
        _poison_dvol(rt, mode)
    return await _env(rt, "deribit_dvol", DVOL_ARGS)


@pytest.mark.parametrize(
    "mode",
    [("close", v) for v in CLOSE_POISON] + [("ts", v) for v in TS_POISON] + [("row", v) for v in ROW_POISON],
    ids=_id,
)
async def test_deribit_dvol_drops_a_poisoned_row_and_says_so(tmp_path, mode):
    ref = await _dvol(tmp_path, ("drop",))
    env = await _dvol(tmp_path, mode)  # a NaN close gave NaN mean / stdev / min / max before; no warning at all
    assert (env["ok"], env["data"], env["missing"]) == (ref["ok"], ref["data"], ref["missing"]), str(env)[:600]
    assert env["ok"] is True
    assert any(w.startswith("dvol: 1 malformed DVOL rows") for w in env["warnings"]), env["warnings"]
    assert not any(w.startswith("dvol:") for w in ref["warnings"])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "abc", None, 1e308], ids=_id)
async def test_deribit_dvol_all_rows_poisoned_is_not_ok(tmp_path, bad):
    env = await _dvol(tmp_path, ("all", bad))  # NaN / inf / True gave ok=true before
    assert env["ok"] is False and env["data"] is None
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [("dvol", "data_unavailable")]
    assert "no well-formed rows" in env["missing"][0]["detail"]
    assert any("malformed DVOL rows" in w for w in env["warnings"])


def _dvol_data(rt: Runtime, value: Any) -> None:
    orig = rt.deribit.volatility_index_data

    async def poisoned(*a, **k):
        got = await orig(*a, **k)
        data = copy.deepcopy(got.data)
        data["data"] = value
        return got._replace(data=data)

    rt.deribit.volatility_index_data = poisoned


@pytest.mark.parametrize("value", [5, 1.5, True], ids=_id)
async def test_deribit_dvol_non_list_data_is_empty(tmp_path, value):
    rt = _rt(tmp_path)
    _dvol_data(rt, value)
    env = await _env(rt, "deribit_dvol", DVOL_ARGS)  # TypeError (not iterable) raised out of the tool before
    assert env["ok"] is False
    assert [(m["field"], m["detail"]) for m in env["missing"]] == [("dvol", "empty series")]


async def test_deribit_dvol_clean_unchanged(tmp_path):
    env = await _dvol(tmp_path, None)
    d = env["data"]
    assert env["ok"] is True and not any(w.startswith("dvol:") for w in env["warnings"])
    assert d["count"] == 169 and d["change_7d_pts"] == pytest.approx(16.8)
    assert d["series"][0]["close"] == pytest.approx(45.3) and d["latest"]["value_pct"] == pytest.approx(62.1)


def test_dvol_pct_row():
    assert _dvol_pct_row([1_700_000_000_000, 1, 2, 0.5, 45.3]) == (1_700_000_000_000, 45.3)  # percent, untouched
    assert _dvol_row([1_700_000_000_000, 1, 2, 0.5, 45.0]) == (1_700_000_000_000, 0.45)
    for bad in CLOSE_POISON:
        assert _dvol_pct_row([1_700_000_000_000, 1, 2, 0.5, bad]) is None


# ----------------------------------------------------------------------------- (b) negrisk._jlist


@pytest.mark.parametrize("text", [HUGE_INT, DEEP], ids=_id)
def test_negrisk_jlist_garbage_is_empty(text):
    assert _jlist(text) == []  # HUGE_INT raised ValueError, DEEP RecursionError before


def test_negrisk_jlist_clean_unchanged():
    assert _jlist('["a", "b"]') == ["a", "b"] and _jlist([1]) == [1] and _jlist(None) == []
    assert _jlist("[1, 2") == [] and _jlist("{}") == []


def _event(**leg: Any) -> dict[str, Any]:
    base = {"clobTokenIds": '["t1", "t2"]', "groupItemTitle": "A"}
    return {
        "id": "1",
        "negRisk": True,
        "volume": 100_000,
        "markets": [{**base, **leg}, {**base, "clobTokenIds": '["t3", "t4"]', "groupItemTitle": "B"}],
    }


@pytest.mark.parametrize("text", [HUGE_INT, DEEP], ids=_id)
def test_classify_event_survives_an_undecodable_list(text):
    assert classify_event(_event(clobTokenIds=text), 0.0)["status"] == "leg_without_token"  # raised before
    assert classify_event(_event(closed=True, outcomePrices=text), 0.0)["status"] == "closed_leg_unclear"


def test_classify_event_clean_unchanged():
    assert classify_event(_event(), 0.0)["status"] == "ok"
    assert classify_event(_event(closed=True, outcomePrices='["0", "1"]'), 0.0)["status"] == "too_few_legs"


# ----------------------------------------------------------------------------- (c) outcome_yes bools

BOOL_PAIRS: list[Any] = [[True, False], [False, True], [True, 0], [1, False], "[true, false]", "[false, true]"]


@pytest.mark.parametrize("pair", BOOL_PAIRS, ids=_id)
def test_outcome_yes_bool_is_junk(pair):
    assert outcome_yes(pair) is None  # [true, false] read as "YES paid 1", [false, true] as "NO paid 1" before
    assert gamma_outcome({"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": pair}) is None


def test_outcome_yes_numbers_unchanged():
    assert outcome_yes([1, 0]) == 1 and outcome_yes(["0", "1"]) == 0 and outcome_yes('["1.0", "0.0"]') == 1
    assert gamma_outcome({"closed": True, "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]'}) == 1
