"""Invariant 1: corrupt stored rows -> envelope, never an exception or garbage (item 328, open cases of item 288).

(1) A rule's `enabled` was read by the store as bool() of any stored value, so text "false", 2 or 0.5 (another writer;
SQLite INTEGER affinity keeps such text and numbers as they are) listed as an enabled rule. Now only a stored 0 / 1 is a
bool and rules_list / rules_disable skip any other value with a warning. The alerts job was never affected: it selects
`enabled = 1` in SQL.

(2) positions_list on a `tags` cell holding "[NaN]" / "[Infinity]" / "1e400": already skipped with a warning since item
296 (pinned here). A deeply nested array ("[[[...]]]") raised RecursionError out of the store's json.loads and failed the
whole tool; it now reads as no tags, like any unparseable tags text. Non-list JSON (5, {"a": 1}) is valid JSON and is
returned as stored.

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools._util import drop_corrupt_rows, first_nonfinite

NOW_TS = int(NOW.timestamp())


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
            text = json.loads(res.content[0].text)  # the text reply keeps Infinity / NaN, the structured one shows null
            assert first_nonfinite(text.get("data")) is None, str(text)[:300]
            data = res.structured_content if res.structured_content is not None else text
            return data["result"] if set(data) == {"result"} else data

        yield call


async def _rows(rt, sql: str, rows: list[tuple]) -> None:
    await rt.store.conn.executemany(sql, rows)
    await rt.store.conn.commit()


def _missing(env: dict, field: str, reason: str, detail: str = "") -> bool:
    return any(m["field"] == field and m["reason"] == reason and detail in m["detail"] for m in env["missing"])


# ---------- rules: `enabled` ----------

RULE_SQL = "INSERT INTO rules (id, name, enabled, expr, message, cooldown_seconds, created_ts, disabled_ts) VALUES (?,?,?,?,?,?,?,?)"
GOOD_RULE = (1, "good", 1, "1 > 0", "msg", 1800, NOW_TS - 60, None)
BAD_ENABLED = ("false", "true", "yes", "", 2, -1, 0.5, b"\x01", b"\xff\xfe")
BAD_IDS = ["str_false", "str_true", "str_yes", "str_empty", "two", "minus_one", "half", "blob", "bad_utf8"]


def _rule(**cols) -> tuple:
    names = ["id", "name", "enabled", "expr", "message", "cooldown_seconds", "created_ts", "disabled_ts"]
    row = dict(zip(names, GOOD_RULE, strict=True)) | cols
    return tuple(row[n] for n in names)


@pytest.mark.parametrize("value", BAD_ENABLED, ids=BAD_IDS)
async def test_rules_list_skips_a_rule_with_a_bad_enabled(rt, value):
    # Before item 328 the text / numbers listed as "enabled": true (bool() of a non-empty text / nonzero number), the empty
    # text as false; the store's bool() hid the blob from the blob check.
    await _rows(rt, RULE_SQL, [GOOD_RULE, _rule(id=2, name="bad", enabled=value)])
    async with _session(rt) as call:
        env = await call("rules_list")
        assert env["ok"] is True and [x["name"] for x in env["data"]["rules"]] == ["good"]
        assert any(w.startswith("1 stored rule rows skipped") and "non-0/1 flag enabled" in w for w in env["warnings"])
        env = await call("rules_list", enabled_only=True)  # SQL `enabled = 1` never matched these rows
        assert env["ok"] is True and [x["name"] for x in env["data"]["rules"]] == ["good"] and env["warnings"] == []


async def test_rules_list_only_bad_enabled_rules_is_not_ok(rt):
    await _rows(rt, RULE_SQL, [_rule(id=1, enabled="false"), _rule(id=2, name="b", enabled=2)])
    async with _session(rt) as call:
        env = await call("rules_list")
        assert env["ok"] is False and env["data"] == {"count": 0, "rules": []}
        assert _missing(env, "rules", "data_unavailable", "all 2 matching stored rules are malformed")


async def test_rules_list_valid_enabled_unchanged(rt):
    # Text "0" / "1" and the real 1.0 are stored as the integers 0 / 1 (INTEGER affinity): valid, not skipped.
    rows = [GOOD_RULE, _rule(id=2, name="off", enabled=0), _rule(id=3, name="t0", enabled="0"), _rule(id=4, name="t1", enabled="1")]
    await _rows(rt, RULE_SQL, [*rows, _rule(id=5, name="r1", enabled=1.0)])
    async with _session(rt) as call:
        env = await call("rules_list")
        assert env["ok"] is True and env["warnings"] == [] and env["data"]["count"] == 5
        assert {x["name"]: x["enabled"] for x in env["data"]["rules"]} == {"good": True, "off": False, "t0": False, "t1": True, "r1": True}
        env = await call("rules_list", enabled_only=True)
        assert sorted(x["name"] for x in env["data"]["rules"]) == ["good", "r1", "t1"]


@pytest.mark.parametrize("value", BAD_ENABLED, ids=BAD_IDS)
@pytest.mark.parametrize("enable", [False, True])
async def test_rules_disable_repairs_a_bad_enabled(rt, value, enable):
    # The switch writes 0 / 1 over the corrupt value, so the stored row is valid afterwards and is returned.
    await _rows(rt, RULE_SQL, [_rule(enabled=value)])
    async with _session(rt) as call:
        env = await call("rules_disable", id=1, enable=enable)
        assert env["ok"] is True and env["warnings"] == [] and env["data"]["enabled"] is enable
        stored = (await rt.store.conn.execute_fetchall("SELECT enabled, disabled_ts FROM rules WHERE id = 1"))[0]
        assert tuple(stored) == ((1, None) if enable else (0, NOW_TS))


async def test_rules_disable_row_whose_enabled_stays_bad_is_not_returned(rt, monkeypatch):
    # If the switch did not take (e.g. another writer overwrote it), the bad row is still left out of the reply.
    await _rows(rt, RULE_SQL, [_rule(enabled="false")])

    async def no_op(*_a, **_k):
        return 1

    monkeypatch.setattr(rt.store, "rule_set_enabled", no_op)
    async with _session(rt) as call:
        env = await call("rules_disable", id=1)
        assert env["ok"] is True and env["data"] == {"id": 1, "enabled": False}
        assert any("its stored row is malformed" in w for w in env["warnings"])


async def test_store_reads_enabled_strictly(rt):
    await _rows(rt, RULE_SQL, [GOOD_RULE, _rule(id=2, name="off", enabled=0), _rule(id=3, name="bad", enabled="false")])
    got = {r["name"]: r["enabled"] for r in await rt.store.rules_list()}
    assert got == {"good": True, "off": False, "bad": "false"}
    assert (await rt.store.rule_get(1))["enabled"] is True and (await rt.store.rule_get(3))["enabled"] == "false"


@pytest.mark.parametrize("value", [None, "0", "false", 1, 0.0])
def test_drop_corrupt_rows_flags_need_a_bool(value):
    # NULL cannot be stored (NOT NULL), so it is checked on the helper: anything but a bool in a flag cell drops the row.
    b = Builder()
    rows = [{"id": 1, "enabled": True}, {"id": 2, "enabled": False}, {"id": 3, "enabled": value}]
    assert [r["id"] for r in drop_corrupt_rows(b, rows, "rule", ("created_ts",), flags=("enabled",))] == [1, 2]
    msg = "1 stored rule rows skipped: a blob, or a non-numeric / non-finite created_ts, or a non-0/1 flag enabled; id [3]"
    assert b.build(None)["warnings"] == [msg]
    assert drop_corrupt_rows(Builder(), rows, "rule", ()) == rows  # without `flags` the cell is not checked (other tables)


# ---------- positions_list: tags ----------

POS_SQL = (
    "INSERT INTO positions (id, opened_ts, instrument, kind, side, size, entry, mark, delta, tags, status, note)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
)
GOOD_POS = (1, NOW_TS - 600, "BTC/USDT:USDT", "perp", "long", 1.0, 100.0, 101.0, 1.0, '["core"]', "open", None)
DEEP_TAGS = "[" * 100_000 + "]" * 100_000


def _pos(**cols) -> tuple:
    names = ["id", "opened_ts", "instrument", "kind", "side", "size", "entry", "mark", "delta", "tags", "status", "note"]
    row = dict(zip(names, GOOD_POS, strict=True)) | cols
    return tuple(row[n] for n in names)


@pytest.mark.parametrize("tags", ["[NaN]", '["a", Infinity]', "[-Infinity]", "1e400", '{"x": NaN}'])
async def test_positions_list_skips_non_finite_tags(rt, tags):
    # Already handled since item 296 (finite_json); pinned here for the open question of item 288.
    await _rows(rt, POS_SQL, [GOOD_POS, _pos(id=2, opened_ts=NOW_TS - 60, tags=tags)])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and [p["id"] for p in env["data"]["positions"]] == [1]
        assert any("NaN / Infinity in tags" in w and "[2]" in w for w in env["warnings"])


async def test_positions_list_deeply_nested_tags_reads_as_no_tags(rt):
    # Before item 328 json.loads raised RecursionError in the store and the whole tool failed.
    await _rows(rt, POS_SQL, [GOOD_POS, _pos(id=2, opened_ts=NOW_TS - 60, tags=DEEP_TAGS)])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(2, []), (1, ["core"])]
        env = await call("positions_close", id=2)
        assert env["ok"] is True and env["data"]["status"] == "closed" and env["data"]["tags"] == []


@pytest.mark.parametrize(("tags", "shown"), [("[oops", []), ("5", 5), ('{"a": 1}', {"a": 1}), ('"x"', "x"), ("null", None)])
async def test_positions_list_other_junk_tags_does_not_raise(rt, tags, shown):
    # Unparseable text reads as no tags (unchanged); valid non-list JSON is finite and returned as stored.
    await _rows(rt, POS_SQL, [_pos(tags=tags)])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and env["warnings"] == [] and env["data"]["positions"][0]["tags"] == shown


async def test_positions_list_valid_tags_unchanged(rt):
    await _rows(rt, POS_SQL, [GOOD_POS, _pos(id=2, opened_ts=NOW_TS - 60, tags='["hedge", "btc"]')])
    async with _session(rt) as call:
        env = await call("positions_list")
        assert env["ok"] is True and env["warnings"] == []
        assert [(p["id"], p["tags"]) for p in env["data"]["positions"]] == [(2, ["hedge", "btc"]), (1, ["core"])]
        env = await call("positions_list", tag="core")
        assert [p["id"] for p in env["data"]["positions"]] == [1]
