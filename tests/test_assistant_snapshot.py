from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import ccxt
import pytest

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.assistant import is_last_friday, pick_expiries
from tradedesk.collector.assistant_snapshot import collect_assistant_snapshot, snapshot_path, write_atomic
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.runtime import Runtime


def _rt(state, tmp_path: Path, exchange_factory=FakeEx, **kw) -> Runtime:
    settings = Settings(
        http_retries=1,
        cache_scale=0.0,
        db_path=str(tmp_path / "t.sqlite"),
        assistant_snapshot_path=str(tmp_path / "out" / "snapshot.json"),
        **kw,
    )
    rt = Runtime(settings, transports=make_transports(state), exchange_factory=exchange_factory)
    rt.now = lambda: NOW
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
        "funding_btc", "funding_eth", "events", "em_month", "chain",
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


INPUTS = Path(__file__).parent / "fixtures" / "radar_snapshot_inputs_2026-10-22.json"


async def _seed_archive(rt: Runtime) -> None:
    """The real BTC rows of 18.10 and 11.10, relabelled as 09.10 and 02.10 (NOW is 10.10 13:07, before 20:00 UTC)."""
    rows = json.loads(INPUTS.read_text())["rows"]["BTC"]
    await rt.store.init()
    for src, day in (("2026-10-18", "2026-10-09"), ("2026-10-11", "2026-10-02")):
        ts = int(datetime.strptime(day, "%Y-%m-%d").replace(hour=19, tzinfo=UTC).timestamp())
        assert await rt.store.radar_insert(dict(rows[src], day=day, ts=ts, asset="BTC", created_ts=0)) == 1


async def test_snapshot_radar_section_from_archive(state, tmp_path):
    rt = _rt(state, tmp_path)
    try:
        await _seed_archive(rt)
        await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    sec = snap["sections"]["radar"]
    assert sec["ok"] and sec["missing"] == [] and "radar" not in snap["missing"] and sec["asof_utc"] == snap["asof_utc"]
    assert any("not confirmed as an edge" in w for w in sec["warnings"]) and any("premium-adjusted" in w for w in sec["warnings"])
    btc, eth = sec["data"]["assets"]["BTC"], sec["data"]["assets"]["ETH"]
    assert btc["missing"] == []
    assert btc["surface"]["now_day"] == "2026-10-09" and btc["surface"]["week_day"] == "2026-10-02"
    assert btc["price"]["now"] == pytest.approx(81_093.3) and btc["price"]["change"] == pytest.approx(-0.0296, abs=0.0001)
    assert btc["atm"]["now"]["30"] == pytest.approx(0.3704, abs=0.0001) and btc["skew"]["now"]["put_skew_25"] == pytest.approx(
        0.0265, abs=0.0001
    )
    assert btc["iv_rank"]["now"]["rank"] == pytest.approx(1.0)  # the fake DVOL drifts up every day
    assert btc["iv_vs_rv"]["rv30"] > 0 and btc["iv_vs_rv"]["premium_rv30"] == pytest.approx(
        btc["iv_vs_rv"]["iv30"] - btc["iv_vs_rv"]["rv30"]
    )
    assert any(w["hot"] for w in btc["forward_vol"]["windows"])
    # ETH has no archive rows: its surface fields are null with a reason, the rest is there
    assert eth["price"]["now"] is None and eth["atm"]["now"] is None and eth["iv_rank"]["now"] is not None
    assert [m["field"] for m in eth["missing"]] == ["surface"]


async def test_snapshot_radar_provider_failure_lands_in_missing(state, tmp_path):
    state.deribit_status = 500
    rt = _rt(state, tmp_path)
    try:
        await _seed_archive(rt)
        await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    sec = json.loads((tmp_path / "out" / "snapshot.json").read_text())["sections"]["radar"]
    assert sec["ok"]  # the archive and the Binance closes still give data
    assert {(m["field"], m["provider"]) for m in sec["missing"]} >= {("radar:dvol:BTC", "deribit"), ("radar:dvol:ETH", "deribit")}
    btc = sec["data"]["assets"]["BTC"]
    assert btc["iv_rank"] == {"now": None, "week": None, "change": None} and btc["price"]["now"] is not None
    assert {"field": "iv_rank", "reason": "no daily DVOL"} in btc["missing"]


class _DeadOhlcvEx(FakeEx):
    async def fetch_ohlcv(self, *a, **k):
        raise ccxt.NetworkError("binance down")


async def test_snapshot_network_down_archive_alone_keeps_previous_file(state, tmp_path, monkeypatch):
    """No network (every tool call, Deribit and Binance fail) but a seeded archive: the radar section has data from
    SQLite alone, which must not count as a successful snapshot (the old file stays, the job fails)."""
    import tradedesk.collector.assistant_snapshot as mod

    async def dead(client, tool, args):
        return mod._failed("tradedesk", "error", "x")

    monkeypatch.setattr(mod, "_call", dead)
    state.deribit_status = 500
    out = tmp_path / "out" / "snapshot.json"
    out.parent.mkdir()
    out.write_text('{"old": true}')
    rt = _rt(state, tmp_path, exchange_factory=_DeadOhlcvEx)
    try:
        await _seed_archive(rt)
        radar = await mod.build_radar(rt, {}, NOW)
        btc = radar["data"]["assets"]["BTC"]
        assert radar["ok"] and btc["price"]["now"] is not None  # the archive alone makes the radar section ok
        assert btc["iv_rank"]["now"] is None and btc["iv_vs_rv"]["rv30"] is None  # nothing from the network
        with pytest.raises(RuntimeError, match="every network section failed"):
            await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    assert json.loads(out.read_text()) == {"old": True}


async def test_snapshot_radar_error_never_fails_the_job(state, tmp_path, monkeypatch):
    import tradedesk.collector.assistant_snapshot as mod

    def boom(*a, **k):
        raise ValueError("bad row")

    monkeypatch.setattr(mod, "radar_view", boom)
    rt = _rt(state, tmp_path)
    try:
        await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    sec = snap["sections"]["radar"]
    assert not sec["ok"] and sec["data"] is None and "radar: ValueError: bad row" in sec["missing"][0]["detail"]
    assert "radar" in snap["missing"] and snap["sections"]["tickers"]["ok"]


async def test_build_radar_without_store_and_calendar(state):
    from tradedesk.collector.assistant_snapshot import build_radar

    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports(state), exchange_factory=FakeEx)
    rt.now = lambda: NOW
    try:
        sec = await build_radar(rt, {}, NOW)  # no events section: the seed alone tags the windows
    finally:
        await rt.aclose()
    assert sec["ok"] and {m["field"] for m in sec["missing"]} == {"radar:archive:BTC", "radar:archive:ETH"}
    assert all(m["reason"] == "data_unavailable" for m in sec["missing"])


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
