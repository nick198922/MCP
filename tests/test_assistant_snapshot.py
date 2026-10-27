from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import ccxt
import pytest

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.assistant import (
    NEAR_MIN_DAYS,
    PAPER_MAX_AGE_HOURS,
    PAPER_PICK_MAX_LEN,
    is_last_friday,
    paper_rec_view,
    pick_expiries,
    pick_near_expiry,
)
from tradedesk.collector.assistant_snapshot import build_my_options as mod_build_my_options
from tradedesk.collector.assistant_snapshot import build_paper_rec as mod_build_paper_rec
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


def test_pick_near_expiry_skips_the_last_day_before_delivery():
    # Monday 10:00 UTC: Tuesday's daily is 22 h away (rule 8 bans buys under 24 h), Wednesday's 46 h -> Wednesday
    mon = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    assert pick_near_expiry(_listing(mon)) == "21OCT26"
    # 07:00 UTC: Tuesday's daily is 25 h away, exactly NEAR_MIN_DAYS (24 h + 1 h margin) -> taken; 1 min later it is not
    assert pick_near_expiry([{"expiry": "20OCT26", "days": 25 / 24}, {"expiry": "21OCT26", "days": 49 / 24}]) == "20OCT26"
    assert pick_near_expiry([{"expiry": "20OCT26", "days": 25 / 24 - 1 / 1440}, {"expiry": "21OCT26", "days": 2.0}]) == "21OCT26"
    assert NEAR_MIN_DAYS * 24 == pytest.approx(25.0)
    assert pick_near_expiry([]) is None and pick_near_expiry([{"expiry": "bad", "days": 3.0}, {"expiry": "1NOV26", "days": None}]) is None
    assert pick_near_expiry([{"expiry": "27NOV26", "days": 39.7}]) == "27NOV26"  # no daily listed: the next one at all


def test_snapshot_path_default_and_memory(tmp_path):
    assert snapshot_path(str(tmp_path / "x.sqlite"), "") == tmp_path.resolve() / "assistant" / "snapshot.json"
    assert snapshot_path(":memory:", "") is None
    assert snapshot_path(":memory:", "/tmp/s.json") == Path("/tmp/s.json")


def test_write_atomic_leaves_no_tmp_and_cleans_nan(tmp_path):
    p = tmp_path / "a" / "s.json"
    write_atomic(p, {"x": float("nan"), "y": [1.5, float("inf")]})
    assert json.loads(p.read_text()) == {"x": None, "y": [1.5, None]}
    assert [f.name for f in p.parent.iterdir()] == ["s.json"]


def _paper_state(**kw) -> dict:
    """The assistant_paper job's state file as collector.assistant_paper writes it (only the keys the view reads + extras)."""
    return {
        "schema": 1, "label": "hypothesis", "date": "2026-10-10", "asof_utc": "2026-10-10T09:00:00Z", "state": "actual",
        "action": "hold", "reason": "a standing paper trade is the method's pick", "pick": "short_strangle25",
        "regime": "mid", "rv30": 0.41, "event": None, "trigger": None, "trade_id": None, "standing_trade": 7,
    } | kw  # fmt: skip


def _write_paper_state(tmp_path: Path, payload: object) -> None:
    p = tmp_path / "assistant" / "paper_state.json"  # state_path(<tmp>/t.sqlite)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def test_paper_rec_view_keeps_known_fields_and_drops_old_or_odd_states():
    now = datetime(2026, 10, 11, 5, 0, tzinfo=UTC)  # the 08:00 Kyiv brief sees yesterday's 09:00 UTC decision
    data, why = paper_rec_view(_paper_state(extra="<x>", standing_trade=True), now)
    assert why is None and data == {
        "label": "hypothesis", "state": "actual", "action": "hold", "trigger": None, "pick": "short_strangle25",
        "asof_utc": "2026-10-10T09:00:00Z", "age_hours": 20.0, "trade_id": None, "standing_trade": None,
    }  # fmt: skip
    data, _ = paper_rec_view(_paper_state(state="changed", action="open", trigger="regime", pick="p" * 99, trade_id=9), now)
    assert data["trigger"] == "regime" and data["pick"] == "p" * PAPER_PICK_MAX_LEN and data["trade_id"] == 9
    data, _ = paper_rec_view(_paper_state(state="no_longer_actual", action="none", pick=None, trigger="bogus"), now)
    assert data["state"] == "no_longer_actual" and data["pick"] is None and data["trigger"] is None
    late = now + timedelta(hours=PAPER_MAX_AGE_HOURS - 20 + 1)
    assert paper_rec_view(_paper_state(), late)[0] is None and "seems stopped" in paper_rec_view(_paper_state(), late)[1]
    assert paper_rec_view(_paper_state(), now + timedelta(hours=PAPER_MAX_AGE_HOURS - 20))[0] is not None  # at the limit
    for bad in (None, [], "x"):
        assert paper_rec_view(bad, now) == (None, "no paper state file yet (collector job assistant_paper off or not run)")
    assert "unknown paper state" in paper_rec_view(_paper_state(state="<b>maybe</b>"), now)[1]
    assert "asof_utc" in paper_rec_view(_paper_state(asof_utc=None), now)[1]


def _my_options_state(**kw) -> dict:
    """The file scripts/bybit_options_positions.py writes (one long call; ticker values from the live 26.10 sample)."""
    return {
        "schema": 1, "source": "bybit", "asof_utc": "2026-10-10T12:50:00Z", "status": "ok", "detail": "",
        "positions": [{"symbol": "BTC-27NOV26-81000-C-USDT", "side": "Buy", "size": "0.1", "avgPrice": "3000",
                       "markPrice": "3572.95", "unrealisedPnl": "57.3", "positionValue": "357.3", "delta": "0.05", "theta": "-5"}],
        "tickers": {"BTC-27NOV26-81000-C-USDT": {"markPrice": "3572.95", "markIv": "0.365", "underlyingPrice": "81115.4",
                                                 "indexPrice": "80661.9"}},
        "errors": [],
    } | kw  # fmt: skip


def _write_my_options(tmp_path: Path, payload: object) -> None:
    p = tmp_path / "assistant" / "my_options.json"  # my_options_path(<tmp>/t.sqlite)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


async def test_snapshot_happy_path(state, tmp_path):
    _write_paper_state(tmp_path, _paper_state(asof_utc="2026-10-10T09:00:00Z"))
    _write_my_options(tmp_path, _my_options_state())
    rt = _rt(state, tmp_path)
    try:
        ok = await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    assert snap["asof_utc"] == "2026-10-10T13:07:00Z"
    # the fixture has no Friday 2..10 days out, so there is honestly no weekly expiry; its nearest is 30OCT26
    assert snap["expiries"] == {"week": None, "month": "27NOV26", "near": "30OCT26"}
    for name in (
        "tickers", "ohlc", "fng", "dvol", "vrp", "term", "skew",
        "funding_btc", "funding_eth", "events", "em_month", "chain", "chain_near",
    ):  # fmt: skip
        assert snap["sections"][name]["ok"], name
        assert snap["sections"][name]["asof_utc"] == "2026-10-10T13:07:00Z"
    assert snap["missing"] == ["em_week"]
    assert snap["sections"]["em_week"]["missing"][0]["reason"] == "data_unavailable"
    assert snap["stale"] == []
    assert snap["sections"]["chain"]["data"]["expiry"] == "27NOV26"
    near = snap["sections"]["chain_near"]["data"]  # the builder's nearer expiry, same shape as the month chain
    assert near["expiry"] == "30OCT26" and near["strikes"] and set(near) == set(snap["sections"]["chain"]["data"])
    # every term-structure expiry for the builder's list, same shape; the window widens with time to expiry
    by = snap["sections"]["chains"]["data"]["by_expiry"]
    assert snap["sections"]["chains"]["ok"] and list(by) == [x["expiry"] for x in snap["sections"]["term"]["data"]["expiries"]]
    assert by["27NOV26"] == snap["sections"]["chain"]["data"] and all(set(c) == set(near) for c in by.values())
    assert ok == len(snap["sections"]) - len(snap["missing"])
    assert "evidence" not in snap["sections"]["term"]  # envelopes are trimmed to data / missing / warnings
    paper = snap["sections"]["paper_rec"]  # from the local state file, no network
    assert paper["ok"] and paper["data"]["state"] == "actual" and paper["data"]["label"] == "hypothesis"
    assert paper["data"]["age_hours"] == pytest.approx(4.12, abs=0.01) and any("H1 rejected" in w for w in paper["warnings"])
    mine = snap["sections"]["my_options"]  # from the local positions file: no network, no key in this job
    assert mine["ok"] and mine["data"]["n_positions"] == 1 and mine["data"]["upnl_usd"] == 57.3
    assert mine["data"]["theta_usd_day"] < 0 and 0 < mine["data"]["positions"][0]["pop"] < 1
    assert any("risk-neutral" in w and "not advice" in w for w in mine["warnings"])


async def test_snapshot_my_options_failure_lands_in_missing_and_is_never_carried(state, tmp_path):
    _write_my_options(tmp_path, _my_options_state())
    rt = _rt(state, tmp_path)
    try:
        await collect_assistant_snapshot(rt)
        assert "my_options" not in json.loads((tmp_path / "out" / "snapshot.json").read_text())["missing"]
        _write_my_options(tmp_path, _my_options_state(status="error", detail="position/list: every request failed"))
        await collect_assistant_snapshot(rt)  # the previous snapshot had good positions: they are not carried
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
    sec = snap["sections"]["my_options"]
    assert not sec["ok"] and sec["data"] is None and "my_options" in snap["missing"] and "my_options" not in snap["stale"]
    assert sec["missing"][0]["provider"] == "bybit" and "every request failed" in sec["missing"][0]["detail"]
    assert "the Bybit request failed" in sec["missing"][0]["detail"]
    (tmp_path / "assistant" / "my_options.json").unlink()
    assert "no Bybit positions file yet" in mod_build_my_options(str(tmp_path / "t.sqlite"), NOW)["missing"][0]["detail"]
    _write_my_options(tmp_path, _my_options_state(asof_utc="2026-10-10T08:00:00Z"))  # 5 h old: the timer stopped
    assert "seems stopped" in mod_build_my_options(str(tmp_path / "t.sqlite"), NOW)["missing"][0]["detail"]
    _write_my_options(tmp_path, _my_options_state(errors=["position/list SOL: NetworkError"]))
    sec = mod_build_my_options(str(tmp_path / "t.sqlite"), NOW)
    assert sec["ok"] and any("Partial Bybit fetch" in w and "SOL" in w for w in sec["warnings"])
    assert mod_build_my_options(":memory:", NOW)["missing"][0]["detail"].startswith("my_options: no SQLite store")


def test_build_my_options_never_raises(monkeypatch):
    import tradedesk.collector.assistant_snapshot as mod

    def boom(*a, **k):
        raise ValueError("odd")

    monkeypatch.setattr(mod, "my_options_view", boom)
    sec = mod.build_my_options("/tmp/x.sqlite", NOW)
    assert not sec["ok"] and sec["missing"][0]["reason"] == "error" and "ValueError: odd" in sec["missing"][0]["detail"]


async def test_snapshot_paper_rec_without_state_or_with_a_stale_one(state, tmp_path):
    rt = _rt(state, tmp_path)
    try:
        await collect_assistant_snapshot(rt)  # the job assistant_paper never ran: no state file
        snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())
        sec = snap["sections"]["paper_rec"]
        assert not sec["ok"] and sec["data"] is None and "paper_rec" in snap["missing"]
        assert sec["missing"][0]["reason"] == "data_unavailable" and "no paper state file" in sec["missing"][0]["detail"]
        _write_paper_state(tmp_path, _paper_state(asof_utc="2026-10-08T09:00:00Z"))  # two days old: the job stopped
        sec = mod_build_paper_rec(rt.settings.db_path, NOW)
        assert not sec["ok"] and "seems stopped" in sec["missing"][0]["detail"]
        (tmp_path / "assistant" / "paper_state.json").write_text("{broken", encoding="utf-8")
        assert not mod_build_paper_rec(rt.settings.db_path, NOW)["ok"]
    finally:
        await rt.aclose()
    assert mod_build_paper_rec(":memory:", NOW)["missing"][0]["detail"].startswith("paper_rec: no SQLite store")


def test_build_paper_rec_never_raises(monkeypatch):
    import tradedesk.collector.assistant_snapshot as mod

    def boom(*a, **k):
        raise ValueError("odd")

    monkeypatch.setattr(mod, "paper_rec_view", boom)
    sec = mod.build_paper_rec("/tmp/x.sqlite", NOW)
    assert not sec["ok"] and sec["missing"][0]["reason"] == "error" and "ValueError: odd" in sec["missing"][0]["detail"]


async def test_snapshot_provider_failure_lands_in_missing(state, tmp_path):
    state.deribit_status = 500
    rt = _rt(state, tmp_path)
    try:
        ok = await collect_assistant_snapshot(rt)
    finally:
        await rt.aclose()
    snap = json.loads((tmp_path / "out" / "snapshot.json").read_text())  # the file is still written
    assert {"term", "skew", "dvol", "chain", "chain_near", "em_week", "em_month"} <= set(snap["missing"])
    assert snap["sections"]["chain"]["missing"]  # per-section reason
    assert snap["sections"]["chain_near"]["missing"][0]["reason"] == "data_unavailable"
    assert snap["expiries"] == {"week": None, "month": None, "near": None}
    assert "chains" in snap["missing"] and snap["sections"]["chains"]["missing"][0]["reason"] == "data_unavailable"
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
    for name in ("term", "skew", "chain", "chain_near", "em_month"):
        sec = snap["sections"][name]
        assert sec["stale"] is True and sec["ok"], name
        assert sec["asof_utc"] == "2026-10-10T13:07:00Z"
        assert sec["data"] == first["sections"][name]["data"]
        assert sec["missing"]  # why the refresh failed
        assert name in snap["stale"] and name not in snap["missing"]
    assert snap["expiries"] == {"week": None, "month": "27NOV26", "near": "30OCT26"}
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
    assert [m["field"] for m in eth["missing"]] == ["surface", "year_skew"] and eth["year"]["skew"] is None
    # yearly chart series: a year of closes (one Binance request) and DVOL; the skew of the two archive rows
    y = btc["year"]
    assert len(y["price"]["values"]) == 365 and y["price"]["n"] == 365 and y["price"]["start"] == "2025-10-11"
    assert len(y["dvol"]["values"]) == 365 and y["dvol"]["values"][-1] > y["dvol"]["values"][0]  # the fake DVOL drifts up
    assert y["skew"]["start"] == "2026-10-02" and y["skew"]["n"] == 2 and len(y["skew"]["put_skew_25"]) == 8
    assert any("Yearly series" in w for w in sec["warnings"]) and eth["year"]["price"]["n"] == 365


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


def test_chain_width_widens_with_time_to_expiry():
    from tradedesk.collector.assistant_snapshot import chain_width_pct

    assert [chain_width_pct(d) for d in (0.5, 32, 60, 151, 333, 700)] == [20.0, 20.0, 24.0, 39.0, 57.0, 60.0]
