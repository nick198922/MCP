"""Invariant 1: positions_list on a stored `tags` cell that is not a JSON list of strings (item 352; open since review
docs/reviews/2026-11-08-tool-failure-tests-6.md, point 5).

Found: a tags value nested only 300 levels deep (item 328 covered the depth where the store's json.loads raises
RecursionError) parsed fine but failed the MCP serialiser ("Circular reference detected (depth exceeded)"), so the
whole tool errored. Non-list JSON (5, {}, "x") and lists with non-strings were returned as stored, breaking the
list-of-strings shape without a word. And when every matching row was skipped the tool said ok=true with no positions,
unlike journal_list / rules_list / events_list.

Now: NaN / Infinity anywhere in tags -> row skipped with a warning (item 296, unchanged); any other non-list or a list
with non-strings -> shown as its string items with a warning; all matching rows malformed -> ok=false with `missing`.
The reply text must stay strict JSON (no NaN / Infinity literals).

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

NOW_TS = int(NOW.timestamp())


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


def _no_constants(name: str):
    raise AssertionError(f"non-strict JSON literal {name} in the reply")


@asynccontextmanager
async def _session(rt):
    async with Client(build_server(rt)[0]) as c:

        async def call(tool: str, /, **args) -> dict:
            res = await c.call_tool(tool, args)
            assert not res.is_error, res.content[0].text[:300] if res.content else ""
            text = json.loads(res.content[0].text, parse_constant=_no_constants)  # strict JSON: NaN / Infinity fail
            data = res.structured_content if res.structured_content is not None else text
            return data["result"] if set(data) == {"result"} else data

        yield call


POS_SQL = "INSERT INTO positions (id, opened_ts, instrument, kind, side, size, mark, tags, status) VALUES (?,?,?,?,?,?,?,?,?)"
GOOD_POS = (1, NOW_TS - 600, "BTC/USDT:USDT", "perp", "long", 1.0, 100.0, '["core"]', "open")


def _pos(**cols) -> tuple:
    names = ["id", "opened_ts", "instrument", "kind", "side", "size", "mark", "tags", "status"]
    row = dict(zip(names, GOOD_POS, strict=True)) | cols
    return tuple(row[n] for n in names)


async def _rows(rt, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(POS_SQL, rows)
    await rt.store.conn.commit()


def _deep(n: int) -> str:
    return "[" * n + "]" * n


NORMALISED = [
    ("{}", []),
    ('"x"', ["x"]),
    ("5", []),
    ("true", []),
    ('{"a": 1}', []),
    ('["a", 1, null, true, {"b": 2}, ["c"]]', ["a"]),
    ('[["core"]]', []),
    ("[" + "9" * 4000 + "]", []),  # an integer beyond the float range (4000 digits parse; 4300+ fail and read as no tags)
    (_deep(300), []),  # failed the MCP serialiser before item 352
    (_deep(600), []),
    ('{"a": ' * 300 + "1" + "}" * 300, []),
]
NORMALISED_IDS = [
    "empty_obj",
    "string",
    "number",
    "bool",
    "object",
    "mixed_list",
    "nested_list",
    "big_int",
    "deep300",
    "deep600",
    "deep_obj300",
]


@pytest.mark.parametrize(("tags", "shown"), NORMALISED, ids=NORMALISED_IDS)
async def test_positions_list_normalises_non_string_tags(rt, tags, shown):
    await _rows(rt, [GOOD_POS, _pos(id=2, opened_ts=NOW_TS - 60, tags=tags)])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and env["data"]["count"] == 2 and env["missing"] == []
        assert [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(2, shown), (1, ["core"])]
        assert any("1 stored position rows had tags that were not a list of strings" in w and "[2]" in w for w in env["warnings"])


@pytest.mark.parametrize(
    "tags",
    [
        "[NaN]",
        "[Infinity]",
        "[-Infinity]",
        "NaN",
        "1e400",
        '{"x": NaN}',
        "[[[NaN]]]",
        '["a", {"b": [Infinity]}]',
        "[" * 300 + "NaN" + "]" * 300,
    ],
    ids=["nan", "inf", "minus_inf", "bare_nan", "overflow", "obj_nan", "nested_nan", "deep_obj_inf", "deep300_nan"],
)
async def test_positions_list_skips_non_finite_tags(rt, tags):
    await _rows(rt, [GOOD_POS, _pos(id=2, opened_ts=NOW_TS - 60, tags=tags)])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(1, ["core"])]
        assert any(w.startswith("1 stored position rows skipped: NaN / Infinity in tags") and "[2]" in w for w in env["warnings"])


async def test_positions_list_very_deep_tags_does_not_raise(rt):
    # 5000 levels: parsed by the store on 3.12+ (then normalised with a warning), RecursionError -> no tags on 3.11.
    await _rows(rt, [GOOD_POS, _pos(id=2, opened_ts=NOW_TS - 60, tags=_deep(5000))])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(2, []), (1, ["core"])]


async def test_positions_list_only_malformed_rows_is_not_ok(rt):
    # One row with NaN tags, one with a non-numeric size (drop_corrupt_rows): nothing left, which is not "no positions".
    await _rows(rt, [_pos(tags="[NaN]"), _pos(id=2, size="abc")])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is False and env["data"] == {"count": 0, "positions": []}
        assert any(
            m["field"] == "positions" and m["reason"] == "data_unavailable" and "all 2 matching" in m["detail"] for m in env["missing"]
        )
        assert len(env["warnings"]) == 2


async def test_positions_list_only_normalised_rows_is_ok(rt):
    # Rows whose tags were only reshaped are still shown, so the reply is ok.
    await _rows(rt, [_pos(tags="5"), _pos(id=2, tags='"hedge"')])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and sorted((p["id"], p["tags"]) for p in env["data"]["positions"]) == [(1, []), (2, ["hedge"])]
        env = await call("positions_list", tag="hedge")  # the LIKE filter matches the bare string; shown as a list
        assert env["ok"] is True and [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(2, ["hedge"])]


async def test_positions_list_no_rows_is_ok(rt):
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and env["data"] == {"count": 0, "positions": []} and env["warnings"] == []


async def test_positions_list_valid_and_wide_tags_unchanged(rt):
    wide = [f"t{i}" for i in range(20_000)]
    await _rows(rt, [GOOD_POS, _pos(id=2, opened_ts=NOW_TS - 60, tags=json.dumps(wide)), _pos(id=3, opened_ts=NOW_TS - 30, tags=None)])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and env["warnings"] == [] and env["missing"] == []
        assert [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(3, None), (2, wide), (1, ["core"])]


async def test_positions_list_store_failure_goes_to_missing(rt, monkeypatch):
    async def boom(**_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.store, "positions_list", boom)
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is False and env["data"] is None
        assert [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]
