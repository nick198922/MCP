"""Invariant 1: journal_close on a stored `tags` cell that is not a JSON list of strings (item 354; left open by item 353,
docs/changes/2026-11-13-tags-close-journal.md).

Found: a tags value nested 300 levels deep failed the MCP serialiser ("Circular reference detected (depth exceeded)")
after the close was already written, so a repeat call said "already closed" and the caller never saw the PnL.

Now journal_close uses the same clean_tags as journal_list for the row it returns: any non-list or a list with non-strings
-> shown as its string items with a warning; NaN / Infinity in tags (or any cell that is not strict JSON) -> the close is
written and the reply carries what was written (id, status, closed_ts, exit, outcome, pnl, r_multiple) without the stored
row, with a warning. ok stays true in both cases. The reply text must stay strict JSON.

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_19 import NORMALISED, NORMALISED_IDS, _deep, _session
from test_tool_failure_extra_20 import NON_FINITE, NON_FINITE_IDS
from tradedesk.config import Settings
from tradedesk.runtime import Runtime

NOW_TS = int(NOW.timestamp())

JOURNAL_SQL = "INSERT INTO journal (id, opened_ts, symbol, side, size, entry, stop, tags, status) VALUES (?,?,?,?,?,?,?,?,?)"


def _trade(tags) -> tuple:
    return (1, NOW_TS - 600, "BTC/USDT:USDT", "long", 2.0, 100.0, 90.0, tags, "open")


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


async def _insert(rt, tags) -> None:
    await rt.store.conn.execute(JOURNAL_SQL, _trade(tags))
    await rt.store.conn.commit()


@pytest.mark.parametrize(("tags", "shown"), NORMALISED, ids=NORMALISED_IDS)
async def test_journal_close_normalises_non_string_tags(rt, tags, shown):
    await _insert(rt, tags)
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=110.0)
        assert env["ok"] is True and env["missing"] == []
        d = env["data"]
        assert (d["id"], d["status"], d["closed_ts"], d["outcome"], d["tags"]) == (1, "closed", NOW_TS, "win", shown)
        assert d["pnl"] == pytest.approx(20.0) and d["r_multiple"] == pytest.approx(1.0)
        assert env["warnings"] == ["trade id=1 had stored tags that were not a list of strings; shown as their string items"]
        env = await call("journal_close", id=1, exit=110.0)  # the close was written once
        assert env["ok"] is False and "already closed" in env["missing"][0]["detail"]
        env = await call("journal_list", status="closed")
        assert [(t["id"], t["tags"]) for t in env["data"]["trades"]] == [(1, shown)]


async def test_journal_close_cancel_normalises_deep_tags(rt):
    await _insert(rt, _deep(300))
    async with _session(rt) as call:
        env = await call("journal_close", id=1, cancelled=True)
        assert env["ok"] is True and env["missing"] == []
        assert (env["data"]["status"], env["data"]["outcome"], env["data"]["tags"]) == ("cancelled", "cancelled", [])
        assert len(env["warnings"]) == 1 and "not a list of strings" in env["warnings"][0]


@pytest.mark.parametrize("tags", NON_FINITE, ids=NON_FINITE_IDS)
async def test_journal_close_non_finite_tags_closes_without_the_row(rt, tags):
    await _insert(rt, tags)
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=95.0)
        assert env["ok"] is True and env["missing"] == []
        d = env["data"]
        assert (d["id"], d["status"], d["closed_ts"], d["exit"], d["outcome"]) == (1, "closed", NOW_TS, 95.0, "loss")
        assert d["pnl"] == pytest.approx(-10.0) and d["r_multiple"] == pytest.approx(-0.5) and "tags" not in d
        assert env["warnings"] == [
            "trade id=1 was closed, but its stored row is not strict JSON (NaN / Infinity in tags or another cell), so it is not returned"
        ]
        env = await call("journal_close", id=1, exit=95.0)  # closed for good, not left open
        assert env["ok"] is False and "already closed" in env["missing"][0]["detail"]


async def test_journal_close_cancel_non_finite_tags(rt):
    await _insert(rt, "[NaN]")
    async with _session(rt) as call:
        env = await call("journal_close", id=1, cancelled=True)
        assert env["ok"] is True
        assert env["data"] == {"id": 1, "closed_ts": NOW_TS, "status": "cancelled", "outcome": "cancelled"}


async def test_journal_close_infinite_target_closes_without_the_row(rt):
    # target is not used by the close, so inf there did not stop it, but came back as Infinity in the reply.
    await _insert(rt, '["btc"]')
    await rt.store.conn.execute("UPDATE journal SET target = ? WHERE id = 1", (float("inf"),))
    await rt.store.conn.commit()
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=110.0)
        assert env["ok"] is True and env["data"]["status"] == "closed" and "target" not in env["data"]
        assert len(env["warnings"]) == 1 and "not strict JSON" in env["warnings"][0]


async def test_journal_close_very_deep_tags_does_not_raise(rt):
    # 5000 levels: parsed by the store on 3.12+ (then normalised with a warning), RecursionError -> no tags on 3.11.
    await _insert(rt, _deep(5000))
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=110.0)
        assert env["ok"] is True and env["data"]["status"] == "closed" and env["data"]["tags"] == []


@pytest.mark.parametrize(("tags", "shown"), [('["core", "btc"]', ["core", "btc"]), (None, None), ("null", None), ("[]", [])])
async def test_journal_close_valid_tags_unchanged(rt, tags, shown):
    await _insert(rt, tags)
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=110.0, note="done")
        assert env["ok"] is True and env["warnings"] == [] and env["missing"] == []
        assert (env["data"]["status"], env["data"]["note"], env["data"]["tags"]) == ("closed", "done", shown)


async def test_journal_close_store_failure_goes_to_missing(rt, monkeypatch):
    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    await _insert(rt, _deep(300))
    monkeypatch.setattr(rt.store, "journal_update_close", boom)
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=110.0)
        assert env["ok"] is False and env["data"] is None
        assert [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]
