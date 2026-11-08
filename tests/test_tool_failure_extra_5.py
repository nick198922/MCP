"""Invariant 1 / 5: store failure, corrupt stored rows and extreme values -> envelope, never an exception (item 287,
BACKLOG 6.53).

Item 287 names events_add, events_list, events_seed_load, journal_add, journal_close and journal_stats. Each gets a
failing store query, rows another writer left with a string / blob / inf, and extreme arguments (1e308, "inf", "1e400",
a timestamp beyond SQLite INTEGER, a megabyte of text). Found on the way: journal_add raised OverflowError on
opened_ts = 10**20 and stored size / entry / stop "inf" as inf; journal_close raised ValueError / TypeError on a string
size / stop, failed its reply on a blob cell after it had written the close, stored a PnL that overflowed to inf and
raised on closed_ts = 10**20; journal_stats answered Infinity on sums of 1e308; events_list failed its reply on an
invalid UTF-8 blob and returned NaN / Infinity from a stored meta; events_add stored such a meta; events_seed_load
raised on a seed that is not {"events": [...]} and on a row with ts 1e308 / Infinity, and counted junk rows as
"already present". All of these now answer ok=false (or skip the row with a warning).

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
from tradedesk.tools import events as events_tools
from tradedesk.tools._util import first_nonfinite

NOW_TS = int(NOW.timestamp())
HUGE = 1e308  # finite, so the store keeps it; a sum of two or a product overflows
CORRUPT = ("abc", b"\x00\x01", b"\xff\xfe", float("inf"))


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

        async def call(name: str, **args) -> dict:
            res = await c.call_tool(name, args)
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


# ---------- journal_add ----------

ADD = {"symbol": "BTC/USDT:USDT", "side": "long", "size": 1.0, "entry": 100.0}


async def test_journal_add_insert_failure_goes_to_missing(rt, monkeypatch):
    _fails(rt, monkeypatch, "journal_insert")
    async with _session(rt) as call:
        _store_failed(await call("journal_add", **ADD))


@pytest.mark.parametrize("field", ["size", "entry", "stop", "target"])
@pytest.mark.parametrize("value", ["inf", "1e400"])
async def test_journal_add_non_finite_number_is_invalid(rt, field, value):
    # Both pass the gt=0 schema; before item 287 they were stored as inf and came back as Infinity.
    async with _session(rt) as call:
        env = await call("journal_add", **(ADD | {field: value}))
        assert env["ok"] is False and env["data"] is None and _missing(env, "input", "invalid_input", field)
        assert await rt.store.journal_list() == []


@pytest.mark.parametrize("ts", [10**20, -(10**20), NOW_TS * 1000])
async def test_journal_add_opened_ts_outside_datetime_is_invalid(rt, ts):
    # 10**20 raised OverflowError (SQLite INTEGER) before item 287; a millisecond value broke the weekly review.
    async with _session(rt) as call:
        env = await call("journal_add", **ADD, opened_ts=ts)
        assert env["ok"] is False and _missing(env, "opened_ts", "invalid_input", "outside the datetime range")
        assert await rt.store.journal_list() == []


async def test_journal_add_extreme_but_finite_values_are_stored(rt):
    thesis, tags = "x" * 1_000_000, [f"t{i}" for i in range(1000)]
    async with _session(rt) as call:
        env = await call("journal_add", **(ADD | {"size": HUGE, "entry": HUGE}), stop=1e-300, thesis=thesis, tags=tags)
        assert env["ok"] is True and env["data"]["size"] == HUGE and env["missing"] == []
        row = await rt.store.journal_get(env["data"]["id"])
        assert row["entry"] == HUGE and row["thesis"] == thesis and row["tags"] == tags


# ---------- journal_close ----------

OPEN_SQL = "INSERT INTO journal (id, opened_ts, symbol, side, size, entry, stop, thesis, status) VALUES (?,?,?,?,?,?,?,?,?)"
GOOD_OPEN = (1, NOW_TS - 600, "BTC/USDT:USDT", "long", 1.0, 100.0, 90.0, None, "open")


def _open(**cols) -> tuple:
    names = ["id", "opened_ts", "symbol", "side", "size", "entry", "stop", "thesis", "status"]
    row = dict(zip(names, GOOD_OPEN, strict=True)) | cols
    return tuple(row[n] for n in names)


async def test_journal_close_read_failure_goes_to_missing(rt, monkeypatch):
    _fails(rt, monkeypatch, "journal_get")
    async with _session(rt) as call:
        _store_failed(await call("journal_close", id=1, exit=101.0))


async def test_journal_close_write_failure_goes_to_missing(rt, monkeypatch):
    await _rows(rt, OPEN_SQL, [GOOD_OPEN])
    _fails(rt, monkeypatch, "journal_update_close")
    async with _session(rt) as call:
        _store_failed(await call("journal_close", id=1, exit=101.0))


@pytest.mark.parametrize("col", ["size", "entry", "stop"])
@pytest.mark.parametrize("bad", CORRUPT)
async def test_journal_close_leaves_a_corrupt_trade_open(rt, col, bad):
    # Before item 287 float() / the R arithmetic raised ValueError / TypeError on the string, inf gave an inf PnL.
    await _rows(rt, OPEN_SQL, [_open(**{col: bad})])
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=101.0)
        assert env["ok"] is False and env["data"] is None and _missing(env, "journal", "data_unavailable", "malformed")
        assert any(w.startswith("1 stored trade rows skipped") for w in env["warnings"])
        assert tuple((await rt.store.conn.execute_fetchall("SELECT status, pnl FROM journal WHERE id = 1"))[0]) == ("open", None)


async def test_journal_close_blob_text_cell_is_not_closed(rt):
    # An invalid UTF-8 blob in thesis failed the reply after the close had been written (the caller saw an error).
    await _rows(rt, OPEN_SQL, [_open(thesis=b"\xff\xfe")])
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=101.0)
        assert env["ok"] is False and _missing(env, "journal", "data_unavailable", "malformed")
        assert tuple((await rt.store.conn.execute_fetchall("SELECT status FROM journal WHERE id = 1"))[0]) == ("open",)


@pytest.mark.parametrize("value", ["inf", "1e400"])
async def test_journal_close_non_finite_exit_is_invalid(rt, value):
    await _rows(rt, OPEN_SQL, [GOOD_OPEN])
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=value)
        assert env["ok"] is False and _missing(env, "input", "invalid_input", "exit")
        assert (await rt.store.journal_get(1))["status"] == "open"


@pytest.mark.parametrize(
    ("trade", "exit_"),
    [({"size": HUGE, "entry": 1.0, "stop": None}, HUGE), ({"size": 1.0, "entry": 1e-300, "stop": 5e-301}, 1e10)],
    ids=["pnl", "r_multiple"],
)
async def test_journal_close_overflowing_result_is_not_written(rt, trade, exit_):
    # Before item 287 the inf PnL / R was stored (and journal_stats then skipped the trade as corrupt).
    await _rows(rt, OPEN_SQL, [_open(**trade)])
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=exit_)
        assert env["ok"] is False and env["data"] is None and _missing(env, "result", "invalid_input")
        assert (await rt.store.journal_get(1))["status"] == "open"


@pytest.mark.parametrize("ts", [10**20, -(10**20)])
async def test_journal_close_closed_ts_outside_datetime_is_invalid(rt, ts):
    await _rows(rt, OPEN_SQL, [GOOD_OPEN])
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=101.0, closed_ts=ts)
        assert env["ok"] is False and _missing(env, "closed_ts", "invalid_input")
        assert (await rt.store.journal_get(1))["status"] == "open"


async def test_journal_close_cancel_ignores_a_non_finite_exit(rt):
    # `exit` is ignored on cancel (documented), so "inf" there is not an error.
    await _rows(rt, OPEN_SQL, [GOOD_OPEN])
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit="inf", cancelled=True)
        assert env["ok"] is True and env["data"]["status"] == "cancelled" and env["data"]["exit"] is None


async def test_journal_close_valid_trade_unchanged(rt):
    await _rows(rt, OPEN_SQL, [GOOD_OPEN])
    async with _session(rt) as call:
        env = await call("journal_close", id=1, exit=110.0)
        d = env["data"]
        assert env["ok"] is True and env["warnings"] == [] and d["status"] == "closed" and d["outcome"] == "win"
        assert d["pnl"] == pytest.approx(10.0) and d["r_multiple"] == pytest.approx(1.0)


# ---------- journal_stats (failing read and string / blob / inf rows: test_tool_failure_extra_3.py) ----------

CLOSED_SQL = "INSERT INTO journal (opened_ts, symbol, side, size, entry, status, outcome, pnl, r_multiple) VALUES (?,?,?,?,?,?,?,?,?)"


@pytest.mark.parametrize("col", ["pnl", "r_multiple"])
async def test_journal_stats_overflowing_sums_are_not_ok(rt, col):
    # Each value is finite, so the corrupt-row filter keeps it; their sum is inf (Infinity in the reply before item 287).
    pnl, rm = (HUGE, 1.0) if col == "pnl" else (1.0, HUGE)
    await _rows(rt, CLOSED_SQL, [(NOW_TS - i, "BTC/USDT:USDT", "long", 1.0, 1.0, "closed", "win", pnl, rm) for i in range(3)])
    async with _session(rt) as call:
        env = await call("journal_stats")
        assert env["ok"] is False and env["data"] is None and _missing(env, "result", "invalid_input")


async def test_journal_stats_skips_a_blob_symbol(rt):
    await _rows(
        rt,
        CLOSED_SQL,
        [
            (NOW_TS - 2, "BTC/USDT:USDT", "long", 1.0, 1.0, "closed", "win", 4.0, 1.0),
            (NOW_TS - 1, b"\xff\xfe", "long", 1.0, 1.0, "closed", "loss", -1.0, -1.0),
        ],
    )
    async with _session(rt) as call:
        env = await call("journal_stats")
        assert env["ok"] is True and env["data"]["closed_trades"] == 1 and env["data"]["total_pnl"] == pytest.approx(4.0)
        assert any(w.startswith("1 stored closed trade rows skipped") for w in env["warnings"])


async def test_journal_stats_large_but_safe_values_unchanged(rt):
    await _rows(rt, CLOSED_SQL, [(NOW_TS - i, "BTC/USDT:USDT", "long", 1.0, 1.0, "closed", "win", 1e300, 2.0) for i in range(3)])
    async with _session(rt) as call:
        env = await call("journal_stats", tag="t" * 10_000)
        assert env["ok"] is True and env["data"]["closed_trades"] == 0  # the huge tag matches nothing
        env = await call("journal_stats")
        assert env["ok"] is True and env["data"]["total_pnl"] == pytest.approx(3e300) and env["warnings"] == []


# ---------- events_add ----------

EV = {"kind": "fomc", "label": "FOMC rate decision", "ts_ms": (NOW_TS + 86_400) * 1000}


async def test_events_add_insert_failure_goes_to_missing(rt, monkeypatch):
    _fails(rt, monkeypatch, "event_insert")
    async with _session(rt) as call:
        _store_failed(await call("events_add", **EV))


@pytest.mark.parametrize("meta", [{"x": float("inf")}, {"x": float("nan")}, {"a": [1.0, {"b": float("-inf")}]}])
async def test_events_add_non_finite_meta_is_invalid(rt, meta):
    # A JSON 1e400 / NaN / -Infinity inside meta parses to a non-finite float on the server (the in-memory Client would
    # send it as null, so the tool is called directly). Before item 287 it was stored and came back from events_list.
    res = await build_server(rt)[0].call_tool("events_add", {**EV, "meta": meta})
    env = json.loads(res.content[0].text)
    assert env["ok"] is False and env["data"] is None and _missing(env, "meta", "invalid_input")
    assert await rt.store.event_count() == 0


async def test_events_add_extreme_but_valid_input_is_stored(rt):
    # A string "inf" in meta is text, a 1e308 number is finite, the year 9999 still fits a datetime.
    meta = {"note": "inf", "big": HUGE, "blob": "y" * 1_000_000}
    async with _session(rt) as call:
        env = await call("events_add", kind="fomc", label="far", date="9999-12-31", time_utc="23:59", meta=meta)
        assert env["ok"] is True and env["data"]["inserted"] is True and env["data"]["ts"] == 253_402_300_740
        assert (await rt.store.events_list())[0]["meta"] == meta


# ---------- events_list ----------

EV_SQL = "INSERT INTO events (ts, kind, label, impact, meta) VALUES (?,?,?,?,?)"
GOOD_EV = (NOW_TS, "fomc", "good", "high", json.dumps({"src": "fed"}))


async def test_events_list_read_failure_goes_to_missing(rt, monkeypatch):
    _fails(rt, monkeypatch, "events_list")
    async with _session(rt) as call:
        _store_failed(await call("events_list"))


@pytest.mark.parametrize(
    "bad",
    [
        (NOW_TS + 1, "fomc", b"\xff\xfe", "high", None),
        (NOW_TS + 1, "fomc", "x", "high", b"\x00\x01"),
        ("abc", "fomc", "x", "high", None),
        (float("inf"), "fomc", "x", "high", None),
        (NOW_TS + 1, "fomc", "x", "high", "NaN"),
        (NOW_TS + 1, "fomc", "x", "high", '{"v": [1, Infinity]}'),
    ],
    ids=["blob_label", "blob_meta", "string_ts", "inf_ts", "nan_meta", "inf_in_meta"],
)
async def test_events_list_skips_a_corrupt_event(rt, bad):
    # Before item 287 the invalid UTF-8 blob failed the whole reply and the meta came back as NaN / Infinity.
    await _rows(rt, EV_SQL, [GOOD_EV, bad])
    async with _session(rt) as call:
        env = await call("events_list")
        assert env["ok"] is True and env["data"]["count"] == 1 and env["data"]["events"][0]["label"] == "good"
        assert any(w.startswith("1 stored event rows skipped") for w in env["warnings"])


async def test_events_list_only_corrupt_events_is_not_ok(rt):
    await _rows(rt, EV_SQL, [("abc", "fomc", "x", "high", None), (NOW_TS, "fomc", "y", "high", "NaN")])
    async with _session(rt) as call:
        env = await call("events_list")
        assert env["ok"] is False and env["data"] == {"count": 0, "events": []}
        assert _missing(env, "events", "data_unavailable", "all 2 matching stored events are malformed")


async def test_events_list_empty_and_valid_unchanged(rt):
    async with _session(rt) as call:
        env = await call("events_list")
        assert env["ok"] is True and env["data"] == {"count": 0, "events": []} and env["warnings"] == []
        await _rows(rt, EV_SQL, [GOOD_EV, (NOW_TS + 5, "cpi", "text meta", "low", "not json")])
        env = await call("events_list")
        assert env["ok"] is True and env["data"]["count"] == 2 and env["warnings"] == []
        assert [e["meta"] for e in env["data"]["events"]] == [{"src": "fed"}, "not json"]


# ---------- events_seed_load ----------


def _seed(monkeypatch, text: str | None) -> None:
    """Make the bundled seed read as `text` (None: opening it raises OSError)."""

    def open_(*_a, **_k):
        if text is None:
            raise OSError("seed unreadable")
        return io.StringIO(text)

    fake = SimpleNamespace(files=lambda _pkg: SimpleNamespace(joinpath=lambda _name: SimpleNamespace(open=open_)))
    monkeypatch.setattr(events_tools, "resources", fake)


SEED_EV = {"ts": NOW_TS, "kind": "fomc", "label": "FOMC", "impact": "high"}


@pytest.mark.parametrize("method", ["event_count", "events_insert_many"])
async def test_events_seed_load_store_failure_goes_to_missing(rt, monkeypatch, method):
    _fails(rt, monkeypatch, method)
    async with _session(rt) as call:
        _store_failed(await call("events_seed_load"))


@pytest.mark.parametrize(
    "text",
    [None, "not json", "[]", '{"events": 5}', '{"events": "abc"}', '{"events": []}', '{"_meta": {}}'],
    ids=["unreadable", "not_json", "list", "int_events", "str_events", "empty_events", "no_events"],
)
async def test_events_seed_load_unusable_seed_is_not_ok(rt, monkeypatch, text):
    # A list or an int / str `events` raised AttributeError / TypeError before item 287.
    _seed(monkeypatch, text)
    async with _session(rt) as call:
        env = await call("events_seed_load")
        assert env["ok"] is False and env["data"] is None and _missing(env, "seed", "data_unavailable")
        assert await rt.store.event_count() == 0


BAD_SEED_ROWS = [
    '{"ts": 1e308, "kind": "fomc", "label": "a"}',
    '{"ts": Infinity, "kind": "fomc", "label": "b"}',
    '{"ts": 1e400, "kind": "fomc", "label": "c"}',
    '{"ts": "abc", "kind": "fomc", "label": "d"}',
    '{"ts": 1800000000, "kind": 5, "label": "e"}',
    '{"ts": 1800000000, "kind": "fomc", "label": ""}',
    '{"ts": 1800000000, "kind": "fomc", "label": "f", "meta": {"x": NaN}}',
    '"junk"',
    "null",
]


async def test_events_seed_load_skips_malformed_rows(rt, monkeypatch):
    # ts 1e308 / Infinity raised OverflowError out of the insert; the other rows were counted as "already present".
    _seed(monkeypatch, '{"events": [' + json.dumps(SEED_EV) + ", " + ", ".join(BAD_SEED_ROWS) + "]}")
    async with _session(rt) as call:
        env = await call("events_seed_load")
        d = env["data"]
        assert env["ok"] is True and d["candidates"] == 1 and d["inserted"] == 1 and d["already_present"] == 0
        assert d["events_after"] == 1 and any(w.startswith(f"{len(BAD_SEED_ROWS)} seed rows skipped") for w in env["warnings"])


async def test_events_seed_load_only_malformed_rows_is_not_ok(rt, monkeypatch):
    _seed(monkeypatch, '{"events": [' + ", ".join(BAD_SEED_ROWS) + "]}")
    async with _session(rt) as call:
        env = await call("events_seed_load")
        assert env["ok"] is False and _missing(env, "seed", "data_unavailable", f"all {len(BAD_SEED_ROWS)} seed rows are malformed")
        assert await rt.store.event_count() == 0


async def test_events_seed_load_bundled_seed_loads_without_skips(rt):
    async with _session(rt) as call:
        env = await call("events_seed_load")
        d = env["data"]
        assert env["ok"] is True and d["inserted"] == d["candidates"] > 50 and d["already_present"] == 0
        assert env["warnings"] == ["2026 Fed dates in the seed are projected; verify against the official calendar"]
        again = await call("events_seed_load")
        assert again["data"]["inserted"] == 0 and again["data"]["already_present"] == d["candidates"]
