"""Invariant 1 / 5: store failure, corrupt stored rows and extreme values -> envelope, never an exception (item 288,
BACKLOG 6.53).

Item 288 names positions_add, positions_close, rules_add, rules_disable, rules_list and rules_presets_load, plus
journal_list on an invalid UTF-8 blob (left open by item 287). Each gets a failing store query or write, rows another
writer left with a string / blob / inf, and extreme arguments (1e308, "inf", "1e400", a timestamp beyond SQLite INTEGER, a
megabyte of text). Found on the way: positions_add stored size / greeks "inf" as inf and raised OverflowError on
opened_ts = 10**20; positions_close raised on closed_ts = 10**20 and failed its reply on a blob cell after it had written
the close; rules_add raised RecursionError on a 100k-term expression (compile_expr); rules_list and rules_disable failed
their reply on a blob and returned Infinity from an inf cell; rules_presets_load raised on a seed that is not
{"presets": [...]} and on a preset with a cooldown "abc" / inf / 1e308, and returned NaN from the seed's _meta;
journal_list failed its reply on a blob and returned Infinity / NaN. All of these now answer ok=false (or skip the row
with a warning).

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls and store checks in
one session.
"""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.tools import rules as rules_tools
from tradedesk.tools._util import first_nonfinite

NOW_TS = int(NOW.timestamp())
HUGE = 1e308  # finite, so the store keeps it
CORRUPT = ("abc", b"\x00\x01", b"\xff\xfe", float("inf"))
LONG_EXPR = " + ".join(["1"] * 100_000) + " > 0"  # ~400 KB; overflowed the parser's stack in compile_expr


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

        async def call(tool: str, /, **args) -> dict:  # positional-only: rules_add has its own `name` argument
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


def _fails(rt, monkeypatch, method: str) -> None:
    async def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rt.store, method, boom)


def _missing(env: dict, field: str, reason: str, detail: str = "") -> bool:
    return any(m["field"] == field and m["reason"] == reason and detail in m["detail"] for m in env["missing"])


def _store_failed(env: dict) -> None:
    assert env["ok"] is False and env["data"] is None and _missing(env, "store", "backend_error", "disk I/O error")


async def _count(rt, table: str) -> int:
    return (await rt.store.conn.execute_fetchall(f"SELECT COUNT(*) FROM {table}"))[0][0]


# ---------- positions_add ----------

POS = {"instrument": "BTC-30OCT26-82000-C", "kind": "option", "side": "long", "size": 1.0}


async def test_positions_add_insert_failure_goes_to_missing(rt, monkeypatch):
    _fails(rt, monkeypatch, "position_insert")
    async with _session(rt) as call:
        _store_failed(await call("positions_add", **POS))


@pytest.mark.parametrize("field", ["size", "entry", "mark", "delta", "gamma", "vega", "theta"])
@pytest.mark.parametrize("value", ["inf", "1e400"])
async def test_positions_add_non_finite_number_is_invalid(rt, field, value):
    # Both pass the float schema; before item 288 they were stored as inf and came back as Infinity.
    async with _session(rt) as call:
        env = await call("positions_add", **(POS | {field: value}))
        assert env["ok"] is False and env["data"] is None and _missing(env, "input", "invalid_input", field)
        assert await _count(rt, "positions") == 0


async def test_positions_add_negative_infinite_greek_is_invalid(rt):
    async with _session(rt) as call:
        env = await call("positions_add", **POS, delta="-inf")
        assert env["ok"] is False and _missing(env, "input", "invalid_input", "delta")


@pytest.mark.parametrize("ts", [10**20, -(10**20), NOW_TS * 1000])
async def test_positions_add_opened_ts_outside_datetime_is_invalid(rt, ts):
    # 10**20 raised OverflowError (SQLite INTEGER) out of the tool before item 288.
    async with _session(rt) as call:
        env = await call("positions_add", **POS, opened_ts=ts)
        assert env["ok"] is False and _missing(env, "opened_ts", "invalid_input", "outside the datetime range")
        assert await _count(rt, "positions") == 0


async def test_positions_add_extreme_but_finite_values_are_stored(rt):
    note, tags = "x" * 1_000_000, [f"t{i}" for i in range(1000)]
    async with _session(rt) as call:
        env = await call("positions_add", **(POS | {"size": HUGE}), entry=HUGE, delta=-HUGE, gamma=1e-300, note=note, tags=tags)
        assert env["ok"] is True and env["data"]["size"] == HUGE and env["missing"] == []
        row = await rt.store.position_get(env["data"]["id"])
        assert row["delta"] == -HUGE and row["note"] == note and row["tags"] == tags


# ---------- positions_close ----------

POS_SQL = (
    "INSERT INTO positions (id, opened_ts, instrument, kind, side, size, entry, mark, delta, tags, status, note)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
)
GOOD_POS = (1, NOW_TS - 600, "BTC/USDT:USDT", "perp", "long", 1.0, 100.0, 101.0, 1.0, "[]", "open", None)


def _pos(**cols) -> tuple:
    names = ["id", "opened_ts", "instrument", "kind", "side", "size", "entry", "mark", "delta", "tags", "status", "note"]
    row = dict(zip(names, GOOD_POS, strict=True)) | cols
    return tuple(row[n] for n in names)


async def _pos_status(rt) -> str:
    return (await rt.store.conn.execute_fetchall("SELECT status FROM positions WHERE id = 1"))[0][0]


@pytest.mark.parametrize("method", ["position_get", "position_update_close"])
async def test_positions_close_store_failure_goes_to_missing(rt, monkeypatch, method):
    await _rows(rt, POS_SQL, [GOOD_POS])
    _fails(rt, monkeypatch, method)
    async with _session(rt) as call:
        _store_failed(await call("positions_close", id=1))


@pytest.mark.parametrize("ts", [10**20, -(10**20)])
async def test_positions_close_closed_ts_outside_datetime_is_invalid(rt, ts):
    # 10**20 raised OverflowError out of the update before item 288.
    await _rows(rt, POS_SQL, [GOOD_POS])
    async with _session(rt) as call:
        env = await call("positions_close", id=1, closed_ts=ts)
        assert env["ok"] is False and _missing(env, "closed_ts", "invalid_input")
        assert await _pos_status(rt) == "open"


@pytest.mark.parametrize(
    "cols",
    [{"size": b} for b in CORRUPT] + [{"delta": float("inf")}, {"instrument": b"\xff\xfe"}, {"note": b"\xff\xfe"}, {"tags": "[NaN]"}],
    ids=["size_str", "size_blob", "size_bad_utf8", "size_inf", "delta_inf", "instrument_blob", "note_blob", "tags_nan"],
)
async def test_positions_close_corrupt_position_is_closed_without_its_row(rt, cols):
    # Closing reads no stored number, so it goes ahead; before item 288 the blob failed the reply after the close had been
    # written (the caller saw an error) and inf came back as Infinity.
    await _rows(rt, POS_SQL, [_pos(**cols)])
    async with _session(rt) as call:
        env = await call("positions_close", id=1)
        assert env["ok"] is True and env["data"] == {"id": 1, "status": "closed", "closed_ts": NOW_TS}
        assert any("its stored row is malformed" in w for w in env["warnings"])
        assert await _pos_status(rt) == "closed"


async def test_positions_close_missing_and_already_closed(rt):
    await _rows(rt, POS_SQL, [_pos(status="closed")])
    async with _session(rt) as call:
        env = await call("positions_close", id=2)
        assert env["ok"] is False and _missing(env, "position", "data_unavailable", "not found")
        env = await call("positions_close", id=1)
        assert env["ok"] is False and _missing(env, "position", "data_unavailable", "already closed")


async def test_positions_close_valid_position_unchanged(rt):
    await _rows(rt, POS_SQL, [GOOD_POS])
    async with _session(rt) as call:
        env = await call("positions_close", id=1, note="n" * 1_000_000)
        d = env["data"]
        assert env["ok"] is True and env["warnings"] == [] and d["status"] == "closed" and d["closed_ts"] == NOW_TS
        assert d["size"] == 1.0 and len(d["note"]) == 1_000_000


# ---------- rules_add ----------

RULE = {"name": "r1", "expr": 'funding("binanceusdm","BTC") > 0.30'}
RULE_SQL = "INSERT INTO rules (id, name, enabled, expr, message, cooldown_seconds, created_ts, disabled_ts) VALUES (?,?,?,?,?,?,?,?)"
GOOD_RULE = (1, "good", 1, "1 > 0", "msg", 1800, NOW_TS - 60, None)


def _rule(**cols) -> tuple:
    names = ["id", "name", "enabled", "expr", "message", "cooldown_seconds", "created_ts", "disabled_ts"]
    row = dict(zip(names, GOOD_RULE, strict=True)) | cols
    return tuple(row[n] for n in names)


@pytest.mark.parametrize("method", ["rules_list", "rule_insert"])
async def test_rules_add_store_failure_goes_to_missing(rt, monkeypatch, method):
    _fails(rt, monkeypatch, method)
    async with _session(rt) as call:
        _store_failed(await call("rules_add", **RULE))


@pytest.mark.parametrize("expr", [LONG_EXPR, "1\x00 > 0", "(" * 500 + "1" + ")" * 500], ids=["long_chain", "null_byte", "nested"])
async def test_rules_add_unparseable_expr_is_rejected(rt, expr):
    # The long chain raised RecursionError out of the tool before item 288 (a null byte raises ValueError on Python 3.11).
    async with _session(rt) as call:
        env = await call("rules_add", name="r1", expr=expr)
        assert env["ok"] is False and env["data"] is None and _missing(env, "expr", "data_unavailable")
        assert await _count(rt, "rules") == 0


async def test_rules_add_extreme_but_valid_input_is_stored(rt):
    # "1e400" in an expression is a literal the evaluator handles; a megabyte message and a 120-char name are valid.
    await _rows(rt, RULE_SQL, [_rule(id=5, name=b"\xff\xfe"), _rule(id=6, name="x", cooldown_seconds="abc")])
    async with _session(rt) as call:
        env = await call("rules_add", name="n" * 120, expr="1e400 > 1e308", message="m" * 1_000_000, cooldown_seconds=86_400)
        assert env["ok"] is True and env["data"]["enabled"] is True and env["missing"] == []
        assert (await rt.store.rule_get(env["data"]["id"]))["message"] == "m" * 1_000_000
        dup = await call("rules_add", name="x", expr="1 > 0")  # a corrupt stored row still counts for the name check
        assert dup["ok"] is False and _missing(dup, "name", "invalid_input", "id=6")


# ---------- rules_list ----------


async def test_rules_list_read_failure_goes_to_missing(rt, monkeypatch):
    _fails(rt, monkeypatch, "rules_list")
    async with _session(rt) as call:
        _store_failed(await call("rules_list"))


@pytest.mark.parametrize(
    "cols",
    [
        {"name": b"\xff\xfe"},
        {"expr": b"\xff\xfe"},
        {"message": b"\x00\x01"},
        {"cooldown_seconds": "abc"},
        {"cooldown_seconds": float("inf")},
        {"created_ts": b"\xff\xfe"},
        {"created_ts": float("-inf")},
        {"disabled_ts": "abc"},
    ],
    ids=["blob_name", "blob_expr", "blob_message", "str_cooldown", "inf_cooldown", "blob_created", "inf_created", "str_disabled"],
)
async def test_rules_list_skips_a_corrupt_rule(rt, cols):
    # Before item 288 the invalid UTF-8 blob failed the whole reply and inf came back as Infinity.
    await _rows(rt, RULE_SQL, [GOOD_RULE, _rule(**({"id": 2, "name": "bad"} | cols))])
    async with _session(rt) as call:
        env = await call("rules_list")
        assert env["ok"] is True and env["data"]["count"] == 1 and env["data"]["rules"][0]["name"] == "good"
        assert any(w.startswith("1 stored rule rows skipped") for w in env["warnings"])


async def test_rules_list_only_corrupt_rules_is_not_ok(rt):
    await _rows(rt, RULE_SQL, [_rule(id=1, expr=b"\xff\xfe"), _rule(id=2, name="b", cooldown_seconds=float("inf"))])
    async with _session(rt) as call:
        env = await call("rules_list")
        assert env["ok"] is False and env["data"] == {"count": 0, "rules": []}
        assert _missing(env, "rules", "data_unavailable", "all 2 matching stored rules are malformed")


async def test_rules_list_empty_and_valid_unchanged(rt):
    async with _session(rt) as call:
        env = await call("rules_list")
        assert env["ok"] is True and env["data"] == {"count": 0, "rules": []} and env["warnings"] == []
        await _rows(rt, RULE_SQL, [GOOD_RULE, _rule(id=2, name="off", enabled=0, created_ts=HUGE, disabled_ts=NOW_TS)])
        env = await call("rules_list")
        assert env["ok"] is True and env["data"]["count"] == 2 and env["warnings"] == []
        assert [x["enabled"] for x in env["data"]["rules"]] == [True, False]
        env = await call("rules_list", enabled_only=True)
        assert env["data"]["count"] == 1 and env["data"]["rules"][0]["name"] == "good"


# ---------- rules_disable ----------


@pytest.mark.parametrize(("method", "args"), [("rule_get", {}), ("rule_set_enabled", {}), ("rule_delete", {"delete": True})])
async def test_rules_disable_store_failure_goes_to_missing(rt, monkeypatch, method, args):
    await _rows(rt, RULE_SQL, [GOOD_RULE])
    _fails(rt, monkeypatch, method)
    async with _session(rt) as call:
        _store_failed(await call("rules_disable", id=1, **args))


@pytest.mark.parametrize(
    "cols",
    [{"expr": b"\xff\xfe"}, {"message": b"\x00\x01"}, {"cooldown_seconds": float("inf")}, {"created_ts": "abc"}],
    ids=["blob_expr", "blob_message", "inf_cooldown", "str_created"],
)
async def test_rules_disable_corrupt_rule_is_switched_without_its_row(rt, cols):
    # Before item 288 the blob failed the reply after the switch had been written and inf came back as Infinity.
    await _rows(rt, RULE_SQL, [_rule(**cols)])
    async with _session(rt) as call:
        env = await call("rules_disable", id=1)
        assert env["ok"] is True and env["data"] == {"id": 1, "enabled": False}
        assert any("its stored row is malformed" in w for w in env["warnings"])
        assert tuple((await rt.store.conn.execute_fetchall("SELECT enabled, disabled_ts FROM rules WHERE id = 1"))[0]) == (0, NOW_TS)
        env = await call("rules_disable", id=1, delete=True)
        assert env["ok"] is True and env["data"] == {"id": 1, "deleted": True} and await _count(rt, "rules") == 0


async def test_rules_disable_missing_and_valid_unchanged(rt):
    await _rows(rt, RULE_SQL, [GOOD_RULE])
    async with _session(rt) as call:
        env = await call("rules_disable", id=2)
        assert env["ok"] is False and _missing(env, "rule", "data_unavailable", "not found")
        env = await call("rules_disable", id=1)
        assert env["ok"] is True and env["warnings"] == [] and env["data"]["enabled"] is False and env["data"]["disabled_ts"] == NOW_TS
        env = await call("rules_disable", id=1, enable=True)
        assert env["ok"] is True and env["data"]["enabled"] is True and env["data"]["disabled_ts"] is None


# ---------- rules_presets_load ----------


def _seed(monkeypatch, text: str | None) -> None:
    """Make the bundled presets read as `text` (None: opening it raises OSError)."""

    def open_(*_a, **_k):
        if text is None:
            raise OSError("seed unreadable")
        return io.StringIO(text)

    fake = SimpleNamespace(files=lambda _pkg: SimpleNamespace(joinpath=lambda _name: SimpleNamespace(open=open_)))
    monkeypatch.setattr(rules_tools, "resources", fake)


SEED_RULE = {"name": "p_ok", "expr": "1 > 0", "message": "m", "cooldown_seconds": 3600, "enabled": True}


@pytest.mark.parametrize("method", ["rules_list", "rule_insert"])
async def test_rules_presets_load_store_failure_goes_to_missing(rt, monkeypatch, method):
    _fails(rt, monkeypatch, method)
    async with _session(rt) as call:
        _store_failed(await call("rules_presets_load"))


@pytest.mark.parametrize(
    "text",
    [None, "not json", "[]", '{"presets": 5}', '{"presets": "abc"}', '{"presets": []}', '{"_meta": {}}'],
    ids=["unreadable", "not_json", "list", "int_presets", "str_presets", "empty_presets", "no_presets"],
)
async def test_rules_presets_load_unusable_seed_is_not_ok(rt, monkeypatch, text):
    # A list or an int / str `presets` raised AttributeError / TypeError before item 288; no presets read as ok=true.
    _seed(monkeypatch, text)
    async with _session(rt) as call:
        env = await call("rules_presets_load")
        assert env["ok"] is False and env["data"] is None and _missing(env, "presets", "data_unavailable")
        assert await _count(rt, "rules") == 0


BAD_PRESETS = [
    '{"name": "a", "expr": "1 > 0", "cooldown_seconds": "abc"}',
    '{"name": "b", "expr": "1 > 0", "cooldown_seconds": Infinity}',
    '{"name": "c", "expr": "1 > 0", "cooldown_seconds": 1e400}',
    '{"name": "d", "expr": "1 > 0", "cooldown_seconds": 1e308}',
    '{"name": "e", "expr": "1 > 0", "cooldown_seconds": 100000000000000000000}',
    '{"name": "f", "expr": "1 > 0", "cooldown_seconds": 10}',
    '{"name": "g", "expr": "1 > 0", "cooldown_seconds": true}',
    '{"name": "h", "expr": "1 > 0", "message": 5}',
    '{"name": "i", "expr": "1 > 0", "enabled": "no"}',
    '{"name": "", "expr": "1 > 0"}',
    '{"name": 7, "expr": "1 > 0"}',
    '"junk"',
    "null",
]


async def test_rules_presets_load_skips_malformed_presets(rt, monkeypatch):
    # The string / inf cooldown raised out of int() and 1e308 / 10**20 overflowed the insert before item 288; an
    # unparseable expression (here the 100k-term chain that raised RecursionError) is reported in `failed`.
    long_ = json.dumps({"name": "p_long", "expr": LONG_EXPR})
    meta = '{"updated": "x", "bad": NaN}'
    _seed(monkeypatch, '{"_meta": ' + meta + ', "presets": [' + json.dumps(SEED_RULE) + ", " + long_ + ", " + ", ".join(BAD_PRESETS) + "]}")
    async with _session(rt) as call:
        env = await call("rules_presets_load")
        d = env["data"]
        assert env["ok"] is True and d["candidates"] == 2 and d["inserted"] == 1 and d["already_present"] == []
        assert [f["name"] for f in d["failed"]] == ["p_long"] and d["meta"] is None
        assert any(w.startswith(f"{len(BAD_PRESETS)} seed presets skipped") for w in env["warnings"])
        assert any("_meta holds NaN or Infinity" in w for w in env["warnings"])
        assert [x["name"] for x in await rt.store.rules_list()] == ["p_ok"]


async def test_rules_presets_load_only_malformed_presets_is_not_ok(rt, monkeypatch):
    _seed(monkeypatch, '{"presets": [' + ", ".join(BAD_PRESETS) + "]}")
    async with _session(rt) as call:
        env = await call("rules_presets_load")
        assert env["ok"] is False and _missing(env, "presets", "data_unavailable", f"all {len(BAD_PRESETS)} seed presets are malformed")
        assert await _count(rt, "rules") == 0


async def test_rules_presets_load_skips_names_of_corrupt_stored_rules(rt):
    # A stored rule with a blob name or a string cooldown does not break the duplicate check.
    await _rows(rt, RULE_SQL, [_rule(id=50, name=b"\xff\xfe"), _rule(id=51, name="preset_eth_funding_hot", cooldown_seconds="abc")])
    async with _session(rt) as call:
        env = await call("rules_presets_load", only=["preset_eth_funding_hot", "preset_btc_funding_hot_binance", "x" * 1_000_000])
        d = env["data"]
        assert env["ok"] is True and d["candidates"] == 2 and d["inserted"] == 1 and d["already_present"] == ["preset_eth_funding_hot"]


async def test_rules_presets_load_bundled_seed_loads_without_skips(rt):
    async with _session(rt) as call:
        env = await call("rules_presets_load")
        d = env["data"]
        assert env["ok"] is True and d["inserted"] == d["candidates"] == 8 and d["failed"] == [] and env["warnings"] == []
        assert d["meta"]["updated"] == "2026-10-12"
        again = await call("rules_presets_load")
        assert again["data"]["inserted"] == 0 and len(again["data"]["already_present"]) == 8


# ---------- journal_list ----------

JOURNAL_SQL = "INSERT INTO journal (id, opened_ts, symbol, side, size, entry, thesis, tags, status, pnl) VALUES (?,?,?,?,?,?,?,?,?,?)"
GOOD_TRADE = (1, NOW_TS - 60, "BTC/USDT:USDT", "long", 1.0, 100.0, None, '["btc"]', "open", None)


def _trade(**cols) -> tuple:
    names = ["id", "opened_ts", "symbol", "side", "size", "entry", "thesis", "tags", "status", "pnl"]
    row = dict(zip(names, GOOD_TRADE, strict=True)) | cols
    return tuple(row[n] for n in names)


async def test_journal_list_read_failure_goes_to_missing(rt, monkeypatch):
    _fails(rt, monkeypatch, "journal_list")
    async with _session(rt) as call:
        _store_failed(await call("journal_list"))


@pytest.mark.parametrize(
    "cols",
    [
        {"symbol": b"\xff\xfe"},
        {"thesis": b"\xff\xfe"},
        {"tags": b"\xff\xfe"},
        {"size": "abc"},
        {"entry": b"\x00\x01"},
        {"pnl": float("inf")},
        {"opened_ts": float("-inf")},
        {"tags": "[NaN]"},
        {"tags": '{"x": Infinity}'},
    ],
    ids=["blob_symbol", "blob_thesis", "blob_tags", "str_size", "blob_entry", "inf_pnl", "inf_opened", "nan_tags", "inf_tags"],
)
async def test_journal_list_skips_a_corrupt_trade(rt, cols):
    # Before item 288 the invalid UTF-8 blob failed the whole reply and inf / NaN came back as Infinity / NaN.
    await _rows(rt, JOURNAL_SQL, [GOOD_TRADE, _trade(**({"id": 2, "opened_ts": NOW_TS - 120} | cols))])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is True and env["data"]["count"] == 1 and env["data"]["trades"][0]["id"] == 1
        assert any(w.startswith("1 stored trade rows skipped") for w in env["warnings"])


async def test_journal_list_only_corrupt_trades_is_not_ok(rt):
    await _rows(rt, JOURNAL_SQL, [_trade(symbol=b"\xff\xfe"), _trade(id=2, pnl=float("inf"))])
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is False and env["data"] == {"count": 0, "trades": []}
        assert _missing(env, "journal", "data_unavailable", "all 2 matching stored trades are malformed")


async def test_journal_list_empty_and_valid_unchanged(rt):
    async with _session(rt) as call:
        env = await call("journal_list")
        assert env["ok"] is True and env["data"] == {"count": 0, "trades": []} and env["warnings"] == []
        await _rows(
            rt,
            JOURNAL_SQL,
            [GOOD_TRADE, _trade(id=2, opened_ts=NOW_TS - 120, size=HUGE, pnl=-HUGE, thesis="t" * 1_000_000, tags="not json")],
        )
        env = await call("journal_list")
        assert env["ok"] is True and env["data"]["count"] == 2 and env["warnings"] == []
        assert [t["tags"] for t in env["data"]["trades"]] == [["btc"], []]
        env = await call("journal_list", tag="btc", symbol="s" * 100_000)
        assert env["ok"] is True and env["data"]["count"] == 0
