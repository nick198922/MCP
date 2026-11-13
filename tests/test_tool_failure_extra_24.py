"""Invariant 1: plan_* on a stored `rules` cell that is not a JSON object of strings, and carry_paper_* on a stored
`entry_flags` cell that is not a JSON list of strings (item 357; found by the trial run of item 356).

Found: rules / entry_flags nested 300 levels deep parsed fine but failed the MCP serialiser ("Circular reference detected
(depth exceeded)", is_error=true) of plan_list / plan_review / plan_link and carry_paper_status / carry_paper_close (the
close after it had written the close and its journal mirror).

Now: plans show `rules` as its string entries (anything else -> {}; warning), NaN / Infinity in rules skips the plan as
before. Carry pairs show `entry_flags` as its string items (warning); NaN / Infinity reads as [] with a warning.

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_19 import NORMALISED, NORMALISED_IDS, _deep, _session
from test_tool_failure_extra_23 import NAN_IDS, NAN_TAGS, NOW_TS, PLAN, RESOLVED, _open_pair, _set
from tradedesk.config import Settings
from tradedesk.runtime import Runtime


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


RULES_WARNING = "had rules that were not an object of strings"
FLAGS_WARNING = "had entry_flags that were not a list of strings"

RULES = [
    ('{"1": "pass", "2": 5, "3": ["x"], "4": {"a": "b"}, "5": null, "6": "warn"}', {"1": "pass", "6": "warn"}),
    ("[]", {}),
    ('["pass"]', {}),
    ('"pass"', {}),
    ("5", {}),
    ("true", {}),
    ("{" + '"1": ' + "9" * 4000 + "}", {}),
    (_deep(300), {}),
    (_deep(600), {}),
    ('{"1": ' + _deep(300) + "}", {}),
    ('{"a": ' * 300 + '"x"' + "}" * 300, {}),
]
RULES_IDS = ["mixed", "list", "list_str", "string", "number", "bool", "big_int", "deep300", "deep600", "deep_value300", "deep_obj300"]
NAN_RULES = ['{"1": NaN}', '{"1": "pass", "2": [Infinity]}', '{"1": ' + "[" * 300 + "NaN" + "]" * 300 + "}"]


# ---------- trade plans: rules ----------


@pytest.mark.parametrize(("rules", "shown"), RULES, ids=RULES_IDS)
async def test_plan_list_normalises_rules(rt, rules, shown):
    async with _session(rt) as call:
        good = (await call("plan_add", **PLAN, rules={"1": "pass"}))["data"]["id"]
        bad = (await call("plan_add", **PLAN))["data"]["id"]
        await _set(rt, "trade_plan", "rules", rules, bad)
        env = await call("plan_list")
        assert env["ok"] is True and env["missing"] == [] and env["data"]["count"] == 2
        assert {p["id"]: p["rules"] for p in env["data"]["plans"]} == {good: {"1": "pass"}, bad: shown}
        assert any(f"1 stored plan rows {RULES_WARNING}" in w and f"[{bad}]" in w for w in env["warnings"])


@pytest.mark.parametrize("rules", NAN_RULES, ids=["nan", "inf_in_list", "deep300_nan"])
async def test_plan_list_still_skips_non_finite_rules(rt, rules):
    async with _session(rt) as call:
        good = (await call("plan_add", **PLAN))["data"]["id"]
        bad = (await call("plan_add", **PLAN))["data"]["id"]
        await _set(rt, "trade_plan", "rules", rules, bad)
        env = await call("plan_list")
        assert env["ok"] is True and [p["id"] for p in env["data"]["plans"]] == [good]
        assert any("1 stored plan rows skipped" in w and f"[{bad}]" in w for w in env["warnings"])


@pytest.mark.parametrize(
    ("rules", "shown"),
    [("5", {}), (_deep(300), {}), ('{"1": ' + _deep(300) + ', "2": "fail"}', {"2": "fail"})],
    ids=["number", "deep300", "deep_value300"],
)
async def test_plan_review_with_bad_rules(rt, rules, shown):
    async with _session(rt) as call:
        ids = [(await call("plan_add", **PLAN))["data"]["id"] for _ in range(2)]
        for i in ids:
            await rt.store.conn.execute(RESOLVED, (NOW_TS - 60, NOW_TS - 600, i))
        await rt.store.conn.commit()
        await _set(rt, "trade_plan", "rules", rules, ids[1])
        env = await call("plan_review")
        assert env["missing"] == [] and env["data"]["summary"]["by_verdict"]["wait"]["n"] == 2
        assert {p["id"]: p["rules"] for p in env["data"]["recent_resolved"]} == {ids[0]: {}, ids[1]: shown}
        assert any(RULES_WARNING in w and f"[{ids[1]}]" in w for w in env["warnings"])


async def test_plan_link_returns_the_row_with_normalised_rules(rt):
    async with _session(rt) as call:
        pid = (await call("plan_add", **PLAN))["data"]["id"]
        jid = (await call("journal_add", symbol="BTC/USDT:USDT", side="long", size=1.0, entry=100.0, stop=90.0))["data"]["id"]
        await _set(rt, "trade_plan", "rules", _deep(300), pid)
        env = await call("plan_link", id=pid, journal_id=jid)
        assert env["ok"] is True and (env["data"]["id"], env["data"]["journal_id"], env["data"]["rules"]) == (pid, jid, {})


async def test_plan_rules_valid_unchanged_null_and_store_failure(rt, monkeypatch):
    async with _session(rt) as call:
        rules = {"1": "pass", "5": "fail", "8": "warn"}
        pid = (await call("plan_add", **PLAN, rules=rules))["data"]["id"]
        env = await call("plan_list")
        assert env["ok"] is True and env["data"]["plans"][0]["rules"] == rules
        assert not any("rules" in w for w in env["warnings"])
        await _set(rt, "trade_plan", "rules", "null", pid)
        env = await call("plan_list")
        assert env["data"]["plans"][0]["rules"] is None and not any("rules" in w for w in env["warnings"])

        async def boom(**_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rt.store, "plan_list", boom)
        env = await call("plan_review")
        assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]


# ---------- carry_paper: entry_flags ----------


@pytest.mark.parametrize(("flags", "shown"), NORMALISED, ids=NORMALISED_IDS)
async def test_carry_paper_status_normalises_entry_flags(rt, flags, shown):
    async with _session(rt) as call:
        pid = await _open_pair(call, rt)
        await _set(rt, "carry_paper", "entry_flags", flags, pid)
        env = await call("carry_paper_status")
        (pair,) = env["data"]["open"]
        assert env["ok"] is True and pair["id"] == pid and "mark" in pair and pair["entry_flags"] == shown
        assert any(f"paper pair id {[pid]} {FLAGS_WARNING}" in w for w in env["warnings"])


@pytest.mark.parametrize("flags", NAN_TAGS, ids=NAN_IDS)
async def test_carry_paper_status_non_finite_entry_flags_read_as_empty(rt, flags):
    async with _session(rt) as call:
        pid = await _open_pair(call, rt)
        await _set(rt, "carry_paper", "entry_flags", flags, pid)
        env = await call("carry_paper_status")
        assert env["ok"] is True and [(p["id"], p["entry_flags"]) for p in env["data"]["open"]] == [(pid, [])]
        assert any(f"paper pair id {[pid]} had NaN / Infinity in entry_flags" in w for w in env["warnings"])


@pytest.mark.parametrize(
    ("flags", "shown"),
    [("5", []), ('[1, "price_gap", ["y"]]', ["price_gap"]), (_deep(300), []), ("[NaN]", [])],
    ids=["number", "mixed", "deep300", "nan"],
)
async def test_carry_paper_close_with_bad_entry_flags_closes_once(rt, flags, shown):
    # Deep flags failed the reply after the close and its journal row were written.
    async with _session(rt) as call:
        pid = await _open_pair(call, rt)
        await _set(rt, "carry_paper", "entry_flags", flags, pid)
        env = await call("carry_paper_close", id=pid, reason="manual")
        assert env["ok"] is True and env["missing"] == []
        assert (env["data"]["status"], env["data"]["entry_flags"], env["data"]["tags"], env["data"]["journal_id"]) == (
            "closed",
            shown,
            ["t1"],
            1,
        )
        assert sum(f"paper pair id {[pid]}" in w and "entry_flags" in w for w in env["warnings"]) == 1
        env = await call("carry_paper_status", include_closed=True)
        assert env["ok"] is True and [(t["id"], t["entry_flags"]) for t in env["data"]["closed"]] == [(pid, shown)]
        env = await call("carry_paper_close", id=pid, reason="manual")  # closed once
        assert env["ok"] is False and "already closed" in env["missing"][0]["detail"]


async def test_carry_paper_cancel_and_single_status_with_deep_entry_flags(rt):
    async with _session(rt) as call:
        pid = await _open_pair(call, rt)
        await _set(rt, "carry_paper", "entry_flags", _deep(300), pid)
        env = await call("carry_paper_close", id=pid, reason="manual", cancelled=True)
        assert env["ok"] is True and (env["data"]["status"], env["data"]["entry_flags"]) == ("cancelled", [])
        env = await call("carry_paper_status", id=pid)
        assert env["ok"] is True and (env["data"]["trade"]["id"], env["data"]["trade"]["entry_flags"]) == (pid, [])


async def test_carry_paper_valid_entry_flags_unchanged_and_store_failure(rt, monkeypatch):
    async with _session(rt) as call:
        pid = await _open_pair(call, rt)
        await _set(rt, "carry_paper", "entry_flags", '["price_gap", "current_spread_reversed"]', pid)
        env = await call("carry_paper_status")
        assert env["data"]["open"][0]["entry_flags"] == ["price_gap", "current_spread_reversed"]
        assert not any("entry_flags" in w for w in env["warnings"])
        await _set(rt, "carry_paper", "entry_flags", "null", pid)
        env = await call("carry_paper_status")
        assert env["data"]["open"][0]["entry_flags"] == [] and not any("entry_flags" in w for w in env["warnings"])

        async def boom(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rt.store, "carry_paper_list", boom)
        env = await call("carry_paper_status")
        assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]
