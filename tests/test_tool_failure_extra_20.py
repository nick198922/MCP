"""Invariant 1: positions_close and journal_list on a stored `tags` cell that is not a JSON list of strings (item 353;
left open by item 352, docs/changes/2026-11-13-positions-list-tags.md).

Found: a tags value nested 300 levels deep parsed fine in the store but failed the MCP serialiser ("Circular reference
detected (depth exceeded)"): in positions_close after the close was already written, in journal_list for the whole
list. Non-list JSON (5, {}, "x") and lists with non-strings came back as stored.

Now both tools use the same clean_tags as positions_list: NaN / Infinity anywhere in tags -> the trade is skipped
(journal_list) or the closed row is left out of the reply (positions_close), with a warning, as before; any other non-list
or a list with non-strings -> shown as its string items with a warning. The reply text must stay strict JSON.

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_19 import NORMALISED, NORMALISED_IDS, _deep, _session
from tradedesk.config import Settings
from tradedesk.runtime import Runtime

NOW_TS = int(NOW.timestamp())

NON_FINITE = ["[NaN]", "[-Infinity]", "1e400", '{"x": NaN}', '["a", {"b": [Infinity]}]', "[" * 300 + "NaN" + "]" * 300]
NON_FINITE_IDS = ["nan", "minus_inf", "overflow", "obj_nan", "deep_obj_inf", "deep300_nan"]


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


# ---------- positions_close ----------

POS_SQL = "INSERT INTO positions (id, opened_ts, instrument, kind, side, size, mark, tags, status) VALUES (?,?,?,?,?,?,?,?,?)"


def _pos(tags) -> tuple:
    return (1, NOW_TS - 600, "BTC/USDT:USDT", "perp", "long", 1.0, 100.0, tags, "open")


@pytest.mark.parametrize(("tags", "shown"), NORMALISED, ids=NORMALISED_IDS)
async def test_positions_close_normalises_non_string_tags(rt, tags, shown):
    await _rows(rt, POS_SQL, [_pos(tags)])
    async with _session(rt) as call:
        env = await call("positions_close", id=1)
        assert env["ok"] is True and env["missing"] == []
        assert (env["data"]["id"], env["data"]["status"], env["data"]["closed_ts"], env["data"]["tags"]) == (1, "closed", NOW_TS, shown)
        assert env["warnings"] == ["position id=1 had stored tags that were not a list of strings; shown as their string items"]
        env = await call("positions_list", status="closed")  # the close was written once
        assert [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(1, shown)]


@pytest.mark.parametrize("tags", NON_FINITE, ids=NON_FINITE_IDS)
async def test_positions_close_non_finite_tags_closes_without_the_row(rt, tags):
    await _rows(rt, POS_SQL, [_pos(tags)])
    async with _session(rt) as call:
        env = await call("positions_close", id=1)
        assert env["ok"] is True and env["data"] == {"id": 1, "status": "closed", "closed_ts": NOW_TS}
        assert any("its stored row is malformed" in w and "NaN / Infinity in tags" in w for w in env["warnings"])
        env = await call("positions_close", id=1)  # closed for good, not left open
        assert env["ok"] is False and "already closed" in env["missing"][0]["detail"]


async def test_positions_close_very_deep_tags_does_not_raise(rt):
    # 5000 levels: parsed by the store on 3.12+ (then normalised with a warning), RecursionError -> no tags on 3.11.
    await _rows(rt, POS_SQL, [_pos(_deep(5000))])
    async with _session(rt) as call:
        env = await call("positions_close", id=1)
        assert env["ok"] is True and env["data"]["status"] == "closed" and env["data"]["tags"] == []


@pytest.mark.parametrize(("tags", "shown"), [('["core", "btc"]', ["core", "btc"]), (None, None), ("null", None), ("[]", [])])
async def test_positions_close_valid_tags_unchanged(rt, tags, shown):
    await _rows(rt, POS_SQL, [_pos(tags)])
    async with _session(rt) as call:
        env = await call("positions_close", id=1, note="done")
        assert env["ok"] is True and env["warnings"] == [] and env["missing"] == []
        assert (env["data"]["status"], env["data"]["note"], env["data"]["tags"]) == ("closed", "done", shown)


async def test_positions_close_store_failure_goes_to_missing(rt, monkeypatch):
    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    await _rows(rt, POS_SQL, [_pos(_deep(300))])
    monkeypatch.setattr(rt.store, "position_update_close", boom)
    async with _session(rt) as call:
        env = await call("positions_close", id=1)
        assert env["ok"] is False and env["data"] is None
        assert [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]


# ---------- journal_list ----------

JOURNAL_SQL = "INSERT INTO journal (id, opened_ts, symbol, side, size, entry, tags, status) VALUES (?,?,?,?,?,?,?,?)"
GOOD_TRADE = (1, NOW_TS - 600, "BTC/USDT:USDT", "long", 1.0, 100.0, '["btc"]', "open")


def _trade(tags, id: int = 2) -> tuple:
    return (id, NOW_TS - 60, "ETH/USDT:USDT", "short", 2.0, 50.0, tags, "open")


@pytest.mark.parametrize(("tags", "shown"), NORMALISED, ids=NORMALISED_IDS)
async def test_journal_list_normalises_non_string_tags(rt, tags, shown):
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE, _trade(tags)])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is True and env["data"]["count"] == 2 and env["missing"] == []
        assert [(t["id"], t["tags"]) for t in env["data"]["trades"]] == [(2, shown), (1, ["btc"])]
        assert env["warnings"] == ["1 stored trade rows had tags that were not a list of strings; shown as their string items; id [2]"]


@pytest.mark.parametrize("tags", NON_FINITE, ids=NON_FINITE_IDS)
async def test_journal_list_skips_non_finite_tags(rt, tags):
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE, _trade(tags)])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is True and [(t["id"], t["tags"]) for t in env["data"]["trades"]] == [(1, ["btc"])]
        assert env["warnings"] == ["1 stored trade rows skipped: NaN / Infinity in tags; id [2]"]


async def test_journal_list_very_deep_tags_does_not_raise(rt):
    # 5000 levels: parsed by the store on 3.12+ (then normalised with a warning), RecursionError -> no tags on 3.11.
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE, _trade(_deep(5000))])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is True and [(t["id"], t["tags"]) for t in env["data"]["trades"]] == [(2, []), (1, ["btc"])]


async def test_journal_list_only_normalised_rows_is_ok(rt):
    # Trades whose tags were only reshaped are still shown, so the reply is ok; the LIKE filter matches the bare string.
    await _rows(rt, JOURNAL_SQL, [_trade("5", id=1), _trade('"hedge"')])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is True and sorted((t["id"], t["tags"]) for t in env["data"]["trades"]) == [(1, []), (2, ["hedge"])]
        env = await call("journal_list", tag="hedge")
        assert env["ok"] is True and [(t["id"], t["tags"]) for t in env["data"]["trades"]] == [(2, ["hedge"])]


async def test_journal_list_only_non_finite_tags_is_not_ok(rt):
    await _rows(rt, JOURNAL_SQL, [_trade("[NaN]", id=1), _trade(_deep(300).replace("[]", "[NaN]"))])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is False and env["data"] == {"count": 0, "trades": []}
        assert [(m["field"], m["reason"]) for m in env["missing"]] == [("journal", "data_unavailable")]


async def test_journal_list_valid_and_wide_tags_unchanged(rt):
    wide = [f"t{i}" for i in range(20_000)]
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE, _trade(json.dumps(wide)), _trade(None, id=3)])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is True and env["warnings"] == [] and env["missing"] == []
        assert sorted((t["id"], t["tags"]) for t in env["data"]["trades"] if t["id"] != 3) == [(1, ["btc"]), (2, wide)]
        assert [t["tags"] for t in env["data"]["trades"] if t["id"] == 3] == [None]


async def test_journal_list_store_failure_goes_to_missing(rt, monkeypatch):
    async def boom(**_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.store, "journal_list", boom)
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is False and env["data"] is None
        assert [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]
