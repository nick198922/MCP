from __future__ import annotations

import json

from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store import Store


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


# ---------- store level ----------


async def test_event_insert_is_idempotent():
    s = Store(":memory:")
    await s.init()
    rid = await s.event_insert(ts=1700000000, kind="fomc", label="Nov 2023 FOMC", impact="high")
    assert rid > 0
    # Same (ts, kind, label) — INSERT OR IGNORE → id=0
    rid2 = await s.event_insert(ts=1700000000, kind="fomc", label="Nov 2023 FOMC", impact="high")
    assert rid2 == 0
    assert await s.event_count() == 1
    await s.aclose()


async def test_events_list_filters():
    s = Store(":memory:")
    await s.init()
    await s.events_insert_many(
        [
            {"ts": 1_700_000_000, "kind": "fomc", "label": "A", "impact": "high"},
            {"ts": 1_700_500_000, "kind": "cpi", "label": "B", "impact": "high"},
            {"ts": 1_701_000_000, "kind": "fomc", "label": "C", "impact": "medium"},
        ]
    )
    fomc = await s.events_list(kind="fomc")
    assert [e["label"] for e in fomc] == ["A", "C"]
    high = await s.events_list(impact="high")
    assert [e["label"] for e in high] == ["A", "B"]
    windowed = await s.events_list(since_ts=1_700_000_001, until_ts=1_700_900_000)
    assert [e["label"] for e in windowed] == ["B"]
    await s.aclose()


# ---------- MCP tools ----------


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "events.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_events_add_and_list_mcp(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "events_add", kind="fomc", label="Test FOMC", impact="high", date="2026-12-16", time_utc="19:00")
        assert env["data"]["inserted"] is True
        dupe = await _call(srv, "events_add", kind="fomc", label="Test FOMC", impact="high", date="2026-12-16", time_utc="19:00")
        assert dupe["data"]["inserted"] is False  # dedup by UNIQUE(ts, kind, label)

        lst = await _call(srv, "events_list", kind="fomc", since_date="2026-01-01", until_date="2027-01-01")
        assert lst["data"]["count"] == 1 and lst["data"]["events"][0]["label"] == "Test FOMC"
    finally:
        await rt.aclose()


async def test_events_add_requires_timestamp_source(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "events_add", kind="fomc", label="no ts")
        assert env["ok"] is False
    finally:
        await rt.aclose()


async def test_events_seed_load_installs_bundled_dataset(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "events_seed_load")
        d = env["data"]
        assert d["candidates"] >= 24  # 2024 FOMC × 8 + CPI × 12 + NFP × 12 at minimum
        assert d["inserted"] == d["candidates"]  # fresh DB
        # second call: no new rows but tool still returns ok
        again = await _call(srv, "events_seed_load")
        assert again["data"]["inserted"] == 0
        assert again["data"]["already_present"] == d["candidates"]
        assert d["meta"]["generated_at"] == "2026-10-22"

        el = await _call(srv, "events_list", kind="election", since_date="2026-11-01", until_date="2026-11-30")
        assert el["data"]["count"] == 1
        row = el["data"]["events"][0]
        assert row["ts"] == 1_793_707_200 and row["impact"] == "high"  # 2026-11-03 12:00 UTC, nominal
        assert "midterm" in row["label"]
    finally:
        await rt.aclose()
