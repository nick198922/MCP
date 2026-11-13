"""Invariant 1: free-form JSON cells nested too deep for the MCP serialiser (item 358; found by the trial run of item 357).

Found: `events.meta` / `alerts.payload` nested 300 levels deep parsed fine but failed the MCP serialiser ("Circular
reference detected (depth exceeded)", is_error=true) of events_list / alerts_recent, which return the parsed JSON as is.
paper_carry_status did the same with a deep `cp_cycle.payload` (engine.new_entries_stopped), mark payload (open[].mark)
or closed `trade` (closed_trades). Deeper than json.loads can parse (~10000 levels), meta / payload raised RecursionError
out of the store: events_list / event_study / alerts_recent errored and every alerts tick failed; one radar_surface
row with such a smile (or invalid JSON) hid the whole radar archive from the snapshot.

Now: a cell deeper than MAX_CELL_DEPTH (64) levels is shown as null (cp payloads read as {}, a closed trade left out)
with a warning naming the row; shallower cells are unchanged. RecursionError in the store reads like invalid JSON.

The in-memory store is reopened empty on every MCP connection, so each test makes all its calls in one session.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from conftest import NOW, FakeEx, make_transports
from test_tool_failure_extra_19 import _deep, _session
from tradedesk.analytics.radar import radar_view
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.tools._util import MAX_CELL_DEPTH, json_too_deep

NOW_TS = int(NOW.timestamp())
NOW_MS = NOW_TS * 1000
TOO_DEEP = 20_000  # beyond what json.loads parses (RecursionError)


@pytest.fixture
async def rt(state):
    rt = Runtime(Settings(http_retries=0, cache_scale=0.0, db_path=":memory:"), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    await rt.store.init()
    yield rt
    await rt.aclose()


DEEP = [_deep(300), _deep(600), '{"a": ' * 300 + "1" + "}" * 300, '{"x": 1, "y": ' + _deep(MAX_CELL_DEPTH) + "}"]
DEEP_IDS = ["deep300", "deep600", "deep_obj300", "deep_value_65"]
SHALLOW = ['{"rate": 0.25, "src": ["fed", {"a": [1, 2]}]}', _deep(MAX_CELL_DEPTH), "[]", "5", '"text"', "null"]
SHALLOW_IDS = ["object", "deep_64", "empty_list", "number", "string", "null"]


def test_json_too_deep_counts_container_levels():
    assert not json_too_deep(5) and not json_too_deep("x") and not json_too_deep(None)
    assert not json_too_deep([], limit=1) and json_too_deep([[]], limit=1)
    assert not json_too_deep({"a": [1, {"b": 2}]}, limit=3) and json_too_deep({"a": [1, {"b": {}}]}, limit=3)
    assert json_too_deep(json.loads(_deep(MAX_CELL_DEPTH + 1))) and not json_too_deep(json.loads(_deep(MAX_CELL_DEPTH)))
    value: list = []
    for _ in range(4999):  # built by a loop: json.loads would hit the 3.11 recursion limit
        value = [value]
    assert json_too_deep(value)  # iterative: no RecursionError


# ---------- events_list ----------


async def _event(rt, ts: int, label: str, meta: str | None) -> int:
    cur = await rt.store.conn.execute(
        "INSERT INTO events (ts, kind, label, impact, meta) VALUES (?, 'fomc', ?, 'high', ?)", (ts, label, meta)
    )
    await rt.store.conn.commit()
    return int(cur.lastrowid)


@pytest.mark.parametrize("meta", DEEP, ids=DEEP_IDS)
async def test_events_list_shows_deep_meta_as_null(rt, meta):
    async with _session(rt) as call:
        good = await _event(rt, NOW_TS - 7200, "good", '{"rate": 0.25}')
        bad = await _event(rt, NOW_TS - 3600, "bad", meta)
        env = await call("events_list")
        assert env["ok"] is True and env["missing"] == [] and env["data"]["count"] == 2
        assert {e["id"]: e["meta"] for e in env["data"]["events"]} == {good: {"rate": 0.25}, bad: None}
        assert any(
            f"1 stored event rows had meta nested deeper than {MAX_CELL_DEPTH} levels" in w and f"[{bad}]" in w for w in env["warnings"]
        )


@pytest.mark.parametrize("meta", SHALLOW, ids=SHALLOW_IDS)
async def test_events_list_keeps_shallow_meta(rt, meta):
    async with _session(rt) as call:
        await _event(rt, NOW_TS - 3600, "ok", meta)
        env = await call("events_list")
        assert env["ok"] is True and env["data"]["events"][0]["meta"] == json.loads(meta)
        assert not any("nested deeper" in w for w in env["warnings"])


async def test_events_tools_survive_meta_too_deep_to_parse(rt):
    async with _session(rt) as call:
        eid = await _event(rt, NOW_TS - 3600, "huge", _deep(TOO_DEEP))
        env = await call("events_list")
        assert env["ok"] is True and env["data"]["events"][0]["id"] == eid  # unparsed text, as invalid JSON was
        env = await call("event_study", event_kind="fomc", venue="binance", symbol="BTC/USDT", timeframe="1h")
        assert env["ok"] is False and [m["field"] for m in env["missing"]] == ["ohlcv_hist"]  # no RecursionError


async def test_events_list_store_failure(rt, monkeypatch):
    async with _session(rt) as call:

        async def boom(**_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rt.store, "events_list", boom)
        env = await call("events_list")
        assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]


# ---------- alerts_recent ----------


async def _alert(rt, ts: int, payload: str, rule_id: str = "r1") -> int:
    cur = await rt.store.conn.execute(
        "INSERT INTO alerts (rule_id, ts, payload, channel, delivered) VALUES (?, ?, ?, 'log', 1)", (rule_id, ts, payload)
    )
    await rt.store.conn.commit()
    return int(cur.lastrowid)


@pytest.mark.parametrize("payload", DEEP, ids=DEEP_IDS)
async def test_alerts_recent_shows_deep_payload_as_null(rt, payload):
    async with _session(rt) as call:
        good = await _alert(rt, NOW_TS - 7200, '{"metric": "funding", "value": 0.001}')
        bad = await _alert(rt, NOW_TS - 3600, payload)
        env = await call("alerts_recent")
        assert env["ok"] is True and env["missing"] == [] and env["data"]["count"] == 2
        assert {a["id"]: a["payload"] for a in env["data"]["alerts"]} == {good: {"metric": "funding", "value": 0.001}, bad: None}
        assert any(
            f"1 stored alert rows had a payload nested deeper than {MAX_CELL_DEPTH}" in w and f"[{bad}]" in w for w in env["warnings"]
        )


@pytest.mark.parametrize("payload", SHALLOW, ids=SHALLOW_IDS)
async def test_alerts_recent_keeps_shallow_payload(rt, payload):
    async with _session(rt) as call:
        await _alert(rt, NOW_TS - 3600, payload)
        env = await call("alerts_recent")
        assert env["ok"] is True and env["data"]["alerts"][0]["payload"] == json.loads(payload)
        assert not any("nested deeper" in w for w in env["warnings"])


async def test_alerts_survive_payload_too_deep_to_parse(rt):
    async with _session(rt) as call:
        aid = await _alert(rt, NOW_TS - 3600, _deep(TOO_DEEP), rule_id="health:deribit")
        env = await call("alerts_recent")
        assert env["ok"] is True and env["data"]["alerts"][0]["id"] == aid
        # the alerts job reads the health state from these rows on every tick until it has one
        assert await rt.store.last_alerts_with_prefix("health:") == [{"rule_id": "health:deribit", "ts": NOW_TS - 3600, "payload": None}]


async def test_alerts_recent_store_failure(rt, monkeypatch):
    async with _session(rt) as call:

        async def boom(**_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rt.store, "recent_alerts", boom)
        env = await call("alerts_recent")
        assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("store", "backend_error")]


# ---------- paper_carry_status (cp_* payloads) ----------

CYCLE_TS = NOW_MS - 3_600_000
TRADE_OPEN = "A:WIF/USDT:USDT:1"
TRADE_CLOSED = "A:PEPE/USDT:USDT:1"
TRADE = {
    "symbol": "1000PEPE/USDT:USDT",
    "exit_reason": "signal",
    "days": 10.0,
    "funding_usd": 12.0,
    "price_usd": -1.0,
    "costs_usd": 4.0,
    "pnl_usd": 7.0,
    "return_on_notional": 0.0035,
    "return_on_capital": 0.0023,
    "open_ts": CYCLE_TS - 10 * 86_400_000,
    "close_ts": CYCLE_TS,
}


async def _book(rt, *, cycle: str, mark: str, trade: str, side: str) -> int:
    c = rt.store.conn
    await c.execute("INSERT INTO cp_cycle (strategy, cycle_ts, status, payload) VALUES ('A', ?, 'done', ?)", (CYCLE_TS, cycle))
    await c.execute(
        "INSERT INTO cp_position (trade_id, strategy, symbol, status, opened_ts, slot_notional, state) VALUES (?, 'A', ?, 'open', ?, 2000, ?)",
        (TRADE_OPEN, "WIF/USDT:USDT", CYCLE_TS - 86_400_000, '{"entry_apr": 0.3, "fees": 0.001}'),
    )
    await c.execute(
        "INSERT INTO cp_position (trade_id, strategy, symbol, status, opened_ts, closed_ts, slot_notional, state, trade)"
        " VALUES (?, 'A', ?, 'closed', ?, ?, 2000, '{}', ?)",
        (TRADE_CLOSED, "1000PEPE/USDT:USDT", TRADE["open_ts"], CYCLE_TS, trade),
    )
    await c.execute(
        "INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES ('A', ?, 'side', ?, ?)", (CYCLE_TS, TRADE_OPEN, side)
    )
    cur = await c.execute(
        "INSERT INTO cp_log (strategy, cycle_ts, kind, trade_id, payload) VALUES ('A', ?, 'mark', ?, ?)", (CYCLE_TS, TRADE_OPEN, mark)
    )
    await c.commit()
    return int(cur.lastrowid)


GOOD_CYCLE = '{"on_time": true, "complete": true, "flags": [], "entries_stopped": "kill (b)"}'
GOOD_MARK = '{"pnl_usd": 3.5, "funding_usd": 4.0, "costs_usd": 0.5}'
GOOD_SIDE = '{"actual_cost": 0.002, "model_cost": 0.001, "spread_bps": 2.0}'
DEEP300 = _deep(300)


async def test_paper_carry_status_valid_cells_unchanged(rt):
    async with _session(rt) as call:
        await _book(rt, cycle=GOOD_CYCLE, mark=GOOD_MARK, trade=json.dumps(TRADE), side=GOOD_SIDE)
        env = await call("paper_carry_status", strategy="A", include_closed=True)
        a = env["data"]["A"]
        assert env["ok"] is True and env["missing"] == [] and not any("nested deeper" in w for w in env["warnings"])
        assert a["engine"]["new_entries_stopped"] == "kill (b)" and a["engine"]["last_cycle_on_time"] is True
        assert a["open"][0]["mark"] == {"pnl_usd": 3.5, "funding_usd": 4.0, "costs_usd": 0.5}
        assert a["closed"]["n"] == 1 and a["closed_trades"][0]["symbol"] == "1000PEPE/USDT:USDT"
        assert a["cost_realism"]["with_book"] == 1


async def test_paper_carry_status_with_deep_cp_cells(rt):
    # Each of these reached the reply raw (cycle payload, mark, closed trade) and failed the MCP serialiser.
    async with _session(rt) as call:
        mark_id = await _book(
            rt,
            cycle='{"on_time": true, "entries_stopped": ' + DEEP300 + "}",
            mark='{"pnl_usd": 3.5, "funding_usd": ' + DEEP300 + "}",
            trade=json.dumps(TRADE)[:-1] + ', "note": ' + DEEP300 + "}",
            side=DEEP300,
        )
        env = await call("paper_carry_status", strategy="A", include_closed=True)
        a = env["data"]["A"]
        assert env["ok"] is True and env["missing"] == []
        assert a["engine"]["cycles_done"] == 1 and a["engine"]["new_entries_stopped"] is None and a["engine"]["last_cycle_on_time"] is None
        assert a["open"][0]["mark"] == {} and a["closed"] == {"n": 0} and a["closed_trades"] == []
        assert a["cost_realism"] == {"sides": 1, "with_book": 0}
        warns = [w for w in env["warnings"] if "nested deeper" in w]
        assert len(warns) == 4
        assert any("1 stored cycle rows had payload" in w and f"cycle_ts [{CYCLE_TS}]" in w for w in warns)
        assert any("1 stored mark rows had payload" in w and f"id [{mark_id}]" in w for w in warns)
        assert any("1 stored closed position rows had trade" in w and "left out" in w and TRADE_CLOSED in w for w in warns)
        assert any("1 stored side rows had payload" in w for w in warns)


async def test_paper_carry_status_store_failure(rt, monkeypatch):
    async with _session(rt) as call:

        async def boom(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(rt.store, "cp_cycles", boom)
        env = await call("paper_carry_status", strategy="A")
        assert env["ok"] is False and [(m["field"], m["reason"]) for m in env["missing"]] == [("paper_carry:A", "backend_error")]


# ---------- radar archive (assistant snapshot; not an MCP tool) ----------

RADAR_SQL = (
    "INSERT INTO radar_surface (day, asset, ts, spot, atm_7d, atm_30d, atm_90d, atm_180d, put_skew_25, rr_25, smile, expiries,"
    " n_prints, method, created_ts) VALUES (?, 'BTC', ?, 100000, 0.5, 0.5, 0.5, 0.5, 0.02, -0.01, ?, ?, 100, 'm', ?)"
)


async def test_radar_list_survives_bad_cells_and_view_stays_shallow(rt):
    day = [(NOW - timedelta(days=k)).strftime("%Y-%m-%d") for k in (3, 2, 1)]
    cells = [(_deep(TOO_DEEP), "[]"), ("not json", DEEP300), (DEEP300, DEEP300)]
    for i, (smile, expiries) in enumerate(cells):
        await rt.store.conn.execute(RADAR_SQL, (day[i], NOW_TS - (3 - i) * 86_400, smile, expiries, NOW_TS))
    await rt.store.conn.commit()
    rows = await rt.store.radar_list("BTC")  # raised RecursionError / ValueError: the whole archive was lost
    assert [(r["day"], r["smile"], r["expiries"]) for r in rows[:2]] == [(day[0], None, []), (day[1], None, json.loads(DEEP300))]
    view = radar_view("BTC", rows, [], [], [], NOW)  # smile / expiries are read as numbers only
    assert view["surface"]["now_day"] == day[2] and not json_too_deep(view, limit=20)
