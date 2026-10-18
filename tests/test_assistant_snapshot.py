from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.assistant import is_last_friday, pick_expiries
from tradedesk.collector.assistant_snapshot import collect_assistant_snapshot, snapshot_path, write_atomic
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime


def _rt(state, tmp_path: Path, **kw) -> Runtime:
    settings = Settings(
        http_retries=1,
        cache_scale=0.0,
        db_path=str(tmp_path / "t.sqlite"),
        assistant_snapshot_path=str(tmp_path / "out" / "snapshot.json"),
        **kw,
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    rt.upbit.http._limiter = TokenBucket(1000, 100)
    return rt


def test_is_last_friday():
    assert is_last_friday(date(2026, 11, 27))  # Friday, last of November
    assert not is_last_friday(date(2026, 11, 20))
    assert not is_last_friday(date(2026, 11, 26))  # Thursday


_MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _listing(now: datetime) -> list[dict]:
    """Deribit-like listing at `now`: 8 dailies, Friday weeklies, last-Friday monthlies and a quarterly."""
    days = {now.date() + timedelta(days=i) for i in range(0, 8)}
    d = now.date()
    while len(days) < 8 + 5:
        d += timedelta(days=1)
        if d.weekday() == 4:
            days.add(d)
    days |= {date(2026, 10, 30), date(2026, 11, 27), date(2026, 12, 25), date(2027, 3, 26)}
    rows = []
    for x in sorted(days):
        exp = datetime(x.year, x.month, x.day, 8, tzinfo=UTC)
        if exp > now:
            rows.append({"expiry": f"{x.day}{_MON[x.month - 1]}{x:%y}", "days": (exp - now).total_seconds() / 86400})
    return rows


def test_pick_expiries_realistic_listing():
    # Monday: the daily of Wednesday (2 days) is not "the week"; the Friday is
    mon = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    assert pick_expiries(_listing(mon)) == ("23OCT26", "27NOV26")
    # Thursday: Friday is 0.9 days away (too close), Saturday/Sunday dailies are not Fridays -> next Friday
    thu = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    assert pick_expiries(_listing(thu)) == ("30OCT26", "27NOV26")
    # Saturday: nearest Friday is 6 days away
    sat = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    assert pick_expiries(_listing(sat)) == ("30OCT26", "27NOV26")


def test_pick_expiries_edge_cases():
    # no Friday within 10 days: no weekly, but the month is still found
    rows = [
        {"expiry": "22OCT26", "days": 3.7},  # Thursday daily
        {"expiry": "30OCT26", "days": 19.8},  # Friday but too far
        {"expiry": "27NOV26", "days": 39.7},
    ]
    assert pick_expiries(rows) == (None, "27NOV26")
    # no last-Friday expiry far enough: fall back to the nearest far one
    assert pick_expiries([{"expiry": "20NOV26", "days": 32.0}]) == (None, "20NOV26")
    assert pick_expiries([]) == (None, None)
    assert pick_expiries([{"expiry": "bad", "days": None}]) == (None, None)


def test_snapshot_path_default_and_memory(tmp_path):
    assert snapshot_path(str(tmp_path / "x.sqlite"), "") == tmp_path.resolve() / "assistant" / "snapshot.json"
    assert snapshot_path(":memory:", "") is None
    assert snapshot_path(":memory:", "/tmp/s.json") == Path("/tmp/s.json")


def test_write_atomic_leaves_no_tmp_and_cleans_nan(tmp_path):
    p = tmp_path / "a" / "s.json"
    write_atomic(p, {"x": float("nan"), "y": [1.5, float("inf")]})
    assert json.loads(p.read_text()) == {"x": None, "y": [1.5, None]}
    assert [f.name for f in p.parent.iterdir()] == ["s.json"]


async def test_snapshot_happy_path(state, tmp_path):
    rt = _rt(state, tmp_path)
    try:
        ok = await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    assert snap["asof_utc"] == "2026-10-10T13:07:00Z"
    # the fixture has no Friday 2..10 days out, so there is honestly no weekly expiry
    assert snap["expiries"] == {"week": None, "month": "27NOV26"}
    for name in (
        "tickers", "ohlc", "fng", "dvol", "vrp", "term", "skew",
        "funding_btc", "funding_eth", "events", "upbit", "em_month", "chain",
    ):  # fmt: skip
        assert snap["sections"][name]["ok"], name
        assert snap["sections"][name]["asof_utc"] == "2026-10-10T13:07:00Z"
    assert snap["missing"] == ["em_week"]
    assert snap["sections"]["em_week"]["missing"][0]["reason"] == "data_unavailable"
    assert snap["stale"] == []
    assert snap["sections"]["chain"]["data"]["expiry"] == "27NOV26"
    assert ok == len(snap["sections"]) - len(snap["missing"])
    assert "evidence" not in snap["sections"]["term"]  # envelopes are trimmed to data / missing / warnings


async def test_snapshot_provider_failure_lands_in_missing(state, tmp_path):
    state.deribit_status = 500
    rt = _rt(state, tmp_path)
    try:
        ok = await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())  # the file is still written
    assert {"term", "skew", "dvol", "chain", "em_week", "em_month"} <= set(snap["missing"])
    assert snap["sections"]["chain"]["missing"]  # per-section reason
    assert snap["expiries"] == {"week": None, "month": None}
    assert snap["sections"]["tickers"]["ok"] and snap["sections"]["fng"]["ok"]
    assert ok >= 1


async def test_failed_section_carries_previous_good_one_as_stale(state, tmp_path):
    rt = _rt(state, tmp_path)
    try:
        await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    first = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    state.deribit_status = 500
    rt = _rt(state, tmp_path)
    rt.now = lambda: NOW + timedelta(minutes=10)
    try:
        await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    assert snap["asof_utc"] == "2026-10-10T13:17:00Z"
    for name in ("term", "skew", "chain", "em_month"):
        sec = snap["sections"][name]
        assert sec["stale"] is True and sec["ok"], name
        assert sec["asof_utc"] == "2026-10-10T13:07:00Z"
        assert sec["data"] == first["sections"][name]["data"]
        assert sec["missing"]  # why the refresh failed
        assert name in snap["stale"] and name not in snap["missing"]
    assert snap["expiries"] == {"week": None, "month": "27NOV26"}
    assert snap["sections"]["tickers"].get("stale") is None
    assert snap["sections"]["tickers"]["asof_utc"] == "2026-10-10T13:17:00Z"


async def test_carried_section_older_than_limit_is_dropped(state, tmp_path):
    rt = _rt(state, tmp_path)
    try:
        await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    state.deribit_status = 500
    rt = _rt(state, tmp_path)
    rt.now = lambda: NOW + timedelta(hours=7)
    try:
        await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    assert snap["stale"] == []
    assert "term" in snap["missing"] and not snap["sections"]["term"]["ok"]


async def test_call_reports_tool_error_reason():
    from mcp import Client
    from mcp.server.mcpserver import MCPServer

    import tradedesk.collector.assistant_snapshot as mod

    async with Client(MCPServer("t")) as c:
        sec = await mod._call(c, "no_such_tool", {})
    assert not sec["ok"]
    assert "no_such_tool" in sec["missing"][0]["detail"]
    assert "JSONDecodeError" not in sec["missing"][0]["detail"]


async def test_snapshot_all_failed_keeps_previous_file(state, tmp_path, monkeypatch):
    import tradedesk.collector.assistant_snapshot as mod

    async def dead(client, tool, args):
        return mod._failed("tradedesk", "error", "x")

    monkeypatch.setattr(mod, "_call", dead)
    out = tmp_path / "out" / "snapshot.json"
    out.parent.mkdir()
    out.write_text('{"old": true}')
    rt = _rt(state, tmp_path)
    try:
        with pytest.raises(RuntimeError):
            await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    assert json.loads(out.read_text()) == {"old": True}


async def test_job_off_by_default_and_on_by_switch(state, tmp_path):
    rt = _rt(state, tmp_path, collector_enabled=True)
    rt2 = _rt(state, tmp_path, collector_enabled=True, assistant_snapshot_enabled=True)
    try:
        assert rt.store is not None and rt2.store is not None
        assert "assistant_snapshot" not in [j.name for j in build_default_scheduler(rt).jobs]
        jobs = {j.name: j for j in build_default_scheduler(rt2).jobs}
        assert jobs["assistant_snapshot"].period_seconds == 600
    finally:
        await rt.aclose()
        await rt2.aclose()
