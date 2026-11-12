"""Invariant 1: a deeply nested JSON cell in the store -> envelope, never an exception (item 355).

The journal / positions parser has caught RecursionError since item 328, but the carry_paper parser (`tags`,
`entry_flags`) and the generic JSON-column parser (trade_plan `rules` / `tags`, rec_paper `legs`, payload columns) did
not: "[[[...]]]" 100k levels deep (any writer other than our tools) raised RecursionError out of json.loads, so
carry_paper_status / carry_paper_close / carry_paper_open, plan_list, plan_review (and the hourly plan_resolve job)
failed as a whole. Now such a cell reads as unparseable text does: [] for carry_paper, None for the generic columns.

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store.db import _parse_carry_paper_row, _parse_json_cols
from tradedesk.tools._util import first_nonfinite

NOW_TS = int(NOW.timestamp())
DEEP = "[" * 100_000 + "]" * 100_000  # json.loads: RecursionError on every supported Python


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


@asynccontextmanager
async def _session(rt):
    async with Client(build_server(rt)[0]) as c:

        async def call(tool: str, /, **args) -> dict:
            res = await c.call_tool(tool, args)
            assert not res.is_error, res.content[0].text[:300] if res.content else ""
            text = json.loads(res.content[0].text)
            assert first_nonfinite(text.get("data")) is None, str(text)[:300]
            data = res.structured_content if res.structured_content is not None else text
            return data["result"] if set(data) == {"result"} else data

        yield call


# ---------- the store parsers ----------


def test_carry_paper_parser_reads_deep_nesting_as_empty():
    row = _parse_carry_paper_row({"id": 1, "tags": DEEP, "entry_flags": DEEP, "stopped": 1, "entry_tradable": 0})
    assert row == {"id": 1, "tags": [], "entry_flags": [], "stopped": True, "entry_tradable": False}


@pytest.mark.parametrize("cols", [("rules", "tags"), ("legs",), ("payload",)])
def test_json_cols_parser_reads_deep_nesting_as_none(cols):
    row = _parse_json_cols({"id": 1, **dict.fromkeys(cols, DEEP)}, cols)
    assert row == {"id": 1, **dict.fromkeys(cols)}


def test_parsers_unchanged_on_valid_and_junk_text():
    assert _parse_carry_paper_row({"tags": '["a"]', "entry_flags": "[oops"}) == {"tags": ["a"], "entry_flags": []}
    assert _parse_json_cols({"rules": '{"1": "pass"}', "tags": "[oops"}, ("rules", "tags")) == {"rules": {"1": "pass"}, "tags": None}


# ---------- trade plans ----------

PLAN = {
    "exchange": "binance",
    "symbol": "BTC/USDT:USDT",
    "instrument": "perp",
    "side": "long",
    "entry_type": "limit",
    "entry": 100.0,
    "stop": 90.0,
    "target": 130.0,
    "expires_ts": NOW_TS + 86_400,
    "verdict": "wait",
    "tags": ["src:own"],
}


@pytest.mark.parametrize("col", ["tags", "rules"])
async def test_plan_tools_survive_a_deeply_nested_json_cell(rt, col):
    # Before item 355 json.loads raised RecursionError out of the store and plan_list / plan_review failed as a whole.
    async with _session(rt) as call:
        good = (await call("plan_add", **PLAN))["data"]["id"]
        bad = (await call("plan_add", **PLAN))["data"]["id"]
        await rt.store.conn.execute(f"UPDATE trade_plan SET {col} = ? WHERE id = ?", (DEEP, bad))
        await rt.store.conn.commit()
        env = await call("plan_list")
        assert env["ok"] is True and env["data"]["count"] == 2
        got = {p["id"]: p[col] for p in env["data"]["plans"]}
        assert got[bad] is None and got[good] == (["src:own"] if col == "tags" else {})
        env = await call("plan_review")  # also what the hourly plan_resolve job runs (review_open)
        assert "data" in env and "warnings" in env


# ---------- carry_paper ----------


async def _pair(rt, *, status: str = "open") -> int:
    pid = await rt.store.carry_paper_insert(
        opened_ts=NOW_TS - 3600,
        base="WIF",
        symbol="WIF/USDT:USDT",
        long_venue="okx",
        short_venue="bybit",
        qty=10.0,
        notional_usd=1000.0,
        long_entry=100.0,
        short_entry=100.1,
        entry_trailing_spread_apr=0.2,
        entry_current_spread_apr=0.15,
        entry_flags=[],
        entry_tradable=True,
        leverage=2.0,
        stop_move=0.5,
        lookback_days=7.0,
        exit_apr=0.0,
        tags=["core"],
    )
    await rt.store.conn.execute("UPDATE carry_paper SET status = ? WHERE id = ?", (status, pid))
    await rt.store.conn.commit()
    return pid


@pytest.mark.parametrize("col", ["tags", "entry_flags"])
async def test_carry_paper_status_survives_a_deeply_nested_json_cell(rt, col):
    # Before item 355 carry_paper_get / carry_paper_list raised RecursionError and the tool failed as a whole.
    pid = await _pair(rt, status="cancelled")
    await rt.store.conn.execute(f"UPDATE carry_paper SET {col} = ? WHERE id = ?", (DEEP, pid))
    await rt.store.conn.commit()
    async with _session(rt) as call:
        env = await call("carry_paper_status", id=pid)
        assert env["ok"] is True and env["data"]["open"] == []
        assert env["data"]["trade"]["id"] == pid and env["data"]["trade"][col] == []
        env = await call("carry_paper_status", include_closed=True)
        assert env["ok"] is True and env["data"]["open"] == []


async def test_carry_paper_close_cancel_survives_deeply_nested_tags(rt):
    pid = await _pair(rt)
    await rt.store.conn.execute("UPDATE carry_paper SET tags = ? WHERE id = ?", (DEEP, pid))
    await rt.store.conn.commit()
    async with _session(rt) as call:
        env = await call("carry_paper_close", id=pid, reason="manual", cancelled=True)
        assert env["ok"] is True and env["data"]["status"] == "cancelled" and env["data"]["tags"] == []
