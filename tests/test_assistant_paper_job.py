from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import tradedesk.collector.assistant_paper as mod
from conftest import NOW, FakeEx, State, make_transports
from tradedesk.analytics.rec_method import decide, method_pick
from tradedesk.collector.assistant_paper import collect_assistant_paper, fetch_closes, state_path
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder, Evidence, Fetched
from tradedesk.runtime import Runtime

T40 = 40 / 365
SHORT_STRADDLE = [(1.0, True, -1.0, 0.5), (1.0, False, -1.0, 0.5)]


def _closes(n: int = 1200) -> list[float]:
    """Alternating +-2 % days with small noise: RV30 ~ 38 %, 40-day moves stay small, so selling premium wins in every window."""
    rnd = random.Random(7)
    out, px = [], 20_000.0
    for i in range(n):
        jump = rnd.choice((-0.12, 0.12)) if rnd.random() < 0.01 else 0.0  # rare shocks give the loss tail
        px *= 1.0 + (0.02 if i % 2 == 0 else -0.02) + rnd.gauss(0.0, 0.002) + jump
        out.append(px)
    return out


def _row(structure: str = "short_straddle", regime: str = "rv30_b1", expiry_ts: int = 2_000_000_000) -> dict[str, Any]:
    return {"structure": structure, "regime": regime, "expiry_ts": expiry_ts}


# ----------------------------------------------------------------------------- pure rules


EXP = 2_000_000_000


def _decide(pick="long_straddle", *, expiry=EXP, regime="rv30_b1", rv30=0.3, prev=None, event=None, last=None, seen=frozenset()):
    return decide(
        pick=pick,
        pick_expiry_ts=expiry,
        regime=regime,
        rv30=rv30,
        prev_regime=prev,
        event_key=event,
        last=last,
        now_ts=1_000,
        seen_events=set(seen),
    )


def test_decide_rules():
    d = _decide(None, last=_row())
    assert (d["action"], d["state"]) == ("none", "no_longer_actual")
    d = _decide(last=None)
    assert (d["action"], d["reason"], d["trigger"]) == ("open", "first", "chain")
    d = _decide(last=_row(expiry_ts=900))
    assert (d["action"], d["reason"], d["trigger"]) == ("open", "expiry", "chain")  # the previous trade expired
    # same structure and expiry: never a duplicate, whatever the regime or the event say
    d = _decide("long_straddle", regime="rv30_b2", rv30=0.5, event="fomc:2026-10-29", last=_row("long_straddle"))
    assert (d["action"], d["state"]) == ("hold", "actual")
    # another pick + confirmed regime change (RV30 2+ vol points past the 0.40 edge between b1 and b2)
    d = _decide("short_straddle", regime="rv30_b2", rv30=0.43, last=_row("long_straddle"))
    assert (d["action"], d["state"], d["reason"], d["trigger"]) == ("open", "changed", "regime", "regime")
    # the same structure on another expiry is another trade
    d = _decide("long_straddle", expiry=EXP - 1, event="fomc:2026-10-29", last=_row("long_straddle"))
    assert (d["action"], d["reason"], d["trigger"]) == ("open", "event", "event")
    d = _decide("short_straddle", event="fomc:2026-10-29", last=_row("long_straddle"), seen={"fomc:2026-10-29"})
    assert (d["action"], d["state"]) == ("hold", "changed")  # the event was already used; another pick alone opens nothing


def test_decide_sees_all_standing_trades():
    chain = {**_row("short_straddle"), "trigger": "chain"}
    regime_trade = {**_row("short_strangle25"), "trigger": "regime"}
    # case 1: chain trade + newer regime trade; the pick equals the older chain trade -> no duplicate
    d = _decide("short_straddle", regime="rv30_b2", rv30=0.5, last=regime_trade, seen=set())
    assert d["action"] == "open"  # looking only at the newest row duplicated the chain trade
    d = decide(
        pick="short_straddle", pick_expiry_ts=EXP, regime="rv30_b2", rv30=0.5, prev_regime="rv30_b2", event_key=None,
        last=regime_trade, standing=[regime_trade, chain], now_ts=1_000, seen_events=set(),
    )  # fmt: skip
    assert (d["action"], d["state"]) == ("hold", "actual")
    # case 2: the 40d chain trade stands, the newer 14d regime trade expired -> not a second chain trade
    short = {**_row("long_straddle_14d", expiry_ts=900), "trigger": "regime"}
    d = decide(
        pick="long_strangle25", pick_expiry_ts=EXP + 5, regime="rv30_b1", rv30=0.3, prev_regime=None, event_key=None,
        last=short, standing=[short, chain], now_ts=1_000, seen_events=set(),
    )  # fmt: skip
    assert d["action"] == "hold"
    # only triggered trades stand -> the chain continues
    trig = {**_row("long_straddle_14d"), "trigger": "regime"}
    d = decide(
        pick="long_strangle25", pick_expiry_ts=EXP + 5, regime="rv30_b1", rv30=0.3, prev_regime=None, event_key=None,
        last=trig, standing=[trig], now_ts=1_000, seen_events=set(),
    )  # fmt: skip
    assert (d["action"], d["trigger"]) == ("open", "chain")


def test_regime_hysteresis():
    live = _row("long_straddle", regime="rv30_b1")
    # 0.405 is barely past the 0.40 edge: noise, no trade ...
    d = _decide("short_straddle", regime="rv30_b2", rv30=0.405, prev="rv30_b1", last=live)
    assert d["action"] == "hold"
    # ... but the same bucket on two decisions in a row confirms it
    d = _decide("short_straddle", regime="rv30_b2", rv30=0.405, prev="rv30_b2", last=live)
    assert (d["action"], d["trigger"]) == ("open", "regime")
    # downward: bucket 1 -> 0 needs RV30 <= 0.23
    assert _decide("short_straddle", regime="rv30_b0", rv30=0.24, last=live)["action"] == "hold"
    assert _decide("short_straddle", regime="rv30_b0", rv30=0.22, last=live)["action"] == "open"


def test_method_pick_returns_summary_unconverted():
    closes = _closes()
    res = method_pick(
        {"short_straddle": (SHORT_STRADDLE, T40), "long_straddle": ([(1.0, True, 1.0, 0.5), (1.0, False, 1.0, 0.5)], T40)}, closes
    )
    assert res["pick"] == "short_straddle" and res["regime"].startswith("rv30_b")
    s = res["summaries"]["short_straddle"]
    assert res["summary"] is s and s["mean"] > 0 and s["ci_lo"] > 0  # fractions of the entry forward, untouched
    assert res["summaries"]["long_straddle"]["mean"] < 0
    assert method_pick({"short_straddle": (SHORT_STRADDLE, T40)}, closes[:20])["pick"] is None  # too short for RV30


# ----------------------------------------------------------------------------- job


def _rt(tmp_path: Path, status: int = 200, **kw: Any) -> Runtime:
    state = State()
    state.deribit_status = status
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "p.sqlite"), assistant_paper_enabled=True, **kw),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    return rt


def _patch(monkeypatch: pytest.MonkeyPatch, closes: list[float] | None, event: str | None = None) -> None:
    async def fake_closes(rt: Runtime, b: Builder, now: datetime) -> list[float] | None:
        if closes is None:
            b.miss("ohlcv:history", "ccxt:binance", "backend_error", "venue down")
        return closes

    async def fake_event(rt: Runtime, b: Builder, now: datetime) -> str | None:
        return event

    monkeypatch.setattr(mod, "fetch_closes", fake_closes)
    monkeypatch.setattr(mod, "upcoming_event", fake_event)


async def test_job_books_once_a_day_and_only_on_triggers(tmp_path, monkeypatch):
    _patch(monkeypatch, _closes())
    rt = _rt(tmp_path)
    try:
        path = state_path(rt.settings.db_path)
        assert await collect_assistant_paper(rt) == 1
        st = json.loads(path.read_text())
        assert st["label"] == "hypothesis" and st["state"] == "actual" and st["reason"] == "first" and st["date"] == "2026-10-10"
        (row,) = await rt.store.rec_paper_list()
        assert row["source"] == "method" and row["regime"] == st["regime"] and row["structure"] == st["pick"]
        # stored exactly as the method returned them: fractions of the entry forward
        assert row["method_q05_pct"] == st["summary_fraction_of_forward"]["q05"]
        assert row["method_mean_pct"] == st["summary_fraction_of_forward"]["mean"]
        assert await collect_assistant_paper(rt) == 0  # same day: nothing, even when run again
        # next day, same regime, no event: the standing trade stays
        rt.now = lambda: NOW + timedelta(days=1)
        assert await collect_assistant_paper(rt) == 0
        assert len(await rt.store.rec_paper_list()) == 1
        assert json.loads(path.read_text())["state"] == "actual" and json.loads(path.read_text())["date"] == "2026-10-11"
        assert (
            row["trigger"] == "chain"
            and st["trigger"] == "chain"
            and st["open_method_risk_usd"] == pytest.approx(row["risk_usd"], abs=0.01)
        )
        # a new FOMC with the SAME pick: no duplicate of the standing trade, the event is marked used
        _patch(monkeypatch, _closes(), event="fomc:2026-10-29")
        rt.now = lambda: NOW + timedelta(days=2)
        assert await collect_assistant_paper(rt) == 0
        st = json.loads(path.read_text())
        assert st["state"] == "actual" and st["event"] == "fomc:2026-10-29" and "fomc:2026-10-29" in st["events_seen"]
        assert len(await rt.store.rec_paper_list()) == 1
    finally:
        await rt.aclose()


def _force(monkeypatch: pytest.MonkeyPatch, plan: dict[str, Any]) -> None:
    """Wrap the real method_pick: override the pick / regime / rv30 from ``plan`` (the rest stays real)."""
    real = mod.method_pick

    def wrapped(structures, closes):
        res = real(structures, closes)
        pick = plan.get("pick", res["pick"])
        res = {**res, "pick": pick, "summary": res["summaries"].get(pick) if pick else None}
        for k in ("regime", "rv30"):
            if k in plan:
                res[k] = plan[k]
        return res

    monkeypatch.setattr(mod, "method_pick", wrapped)


async def test_job_event_and_regime_triggers(tmp_path, monkeypatch):
    _patch(monkeypatch, _closes())
    plan: dict[str, Any] = {"pick": "short_straddle", "regime": "rv30_b1", "rv30": 0.30}
    _force(monkeypatch, plan)
    rt = _rt(tmp_path)
    path = state_path(rt.settings.db_path)

    async def day(n: int, *, event: str | None = None) -> int:
        _patch(monkeypatch, _closes(), event=event)
        rt.now = lambda: NOW + timedelta(days=n)
        return await collect_assistant_paper(rt)

    try:
        assert await day(0) == 1  # first: chain
        plan["pick"] = "short_strangle25"
        assert await day(1) == 0  # another pick alone opens nothing
        assert json.loads(path.read_text())["state"] == "changed"
        assert await day(2, event="fomc:2026-10-29") == 1  # a new FOMC and another pick: an event trade
        plan["pick"] = "long_strangle25"
        assert await day(3, event="fomc:2026-10-29") == 0  # another pick, but the same event is not used twice
        plan.update(regime="rv30_b2", rv30=0.405)  # barely past the 0.40 edge: noise
        assert await day(4) == 0
        plan.update(regime="rv30_b1", rv30=0.399)  # and back: nothing happened
        assert await day(5) == 0
        plan.update(regime="rv30_b2", rv30=0.405)
        assert await day(6) == 0  # first day in the new bucket
        assert await day(7) == 1  # held two decisions in a row: a regime trade
        rows = await rt.store.rec_paper_list()
        assert [x["trigger"] for x in reversed(rows)] == ["chain", "event", "regime"]
        assert all(x["source"] == "method" for x in rows)
        st = json.loads(path.read_text())
        assert st["trigger"] == "regime" and st["open_method_risk_usd"] == pytest.approx(sum(x["risk_usd"] for x in rows), abs=0.01)
    finally:
        await rt.aclose()


async def test_job_registered_candidate_set_and_14d_booking(tmp_path, monkeypatch):
    _patch(monkeypatch, _closes())
    seen: dict[str, Any] = {}
    real = mod.method_pick

    def spy(structures, closes):
        seen["t"] = {k: v[1] for k, v in structures.items()}
        res = real(structures, closes)
        pick = "long_straddle_14d"  # force the short-dated candidate
        return {**res, "pick": pick, "summary": res["summaries"][pick]}

    monkeypatch.setattr(mod, "method_pick", spy)
    rt = _rt(tmp_path)
    try:
        assert await collect_assistant_paper(rt) == 1
        # the registered set is 9 structures on ~40 d + 2 on ~14 d; the fake chain is too thin for one of the 40 d ones
        expected = {*mod.STRUCTURES, "long_straddle_14d", "long_strangle25_14d"}
        assert set(seen["t"]) <= expected and len(expected) == 11 and len(seen["t"]) >= 10
        assert "long_straddle_14d" in seen["t"]  # the fake 30OCT26 smile has no 25-delta strikes for the 14 d strangle
        assert seen["t"]["long_straddle_14d"] < 21 / 365 < 28 / 365 < seen["t"]["long_straddle"]
        (row,) = await rt.store.rec_paper_list()
        assert row["structure"] == "long_straddle"
        assert datetime.fromtimestamp(row["expiry_ts"], UTC).date().isoformat() == "2026-10-30"  # the ~14-day expiry, not the 40-day
        assert json.loads(state_path(rt.settings.db_path).read_text())["pick"] == "long_straddle_14d"
    finally:
        await rt.aclose()


async def test_job_lost_state_file_does_not_double_book(tmp_path, monkeypatch):
    _patch(monkeypatch, _closes())
    plan: dict[str, Any] = {"pick": "short_straddle"}
    _force(monkeypatch, plan)
    rt = _rt(tmp_path)
    path = state_path(rt.settings.db_path)
    try:
        assert await collect_assistant_paper(rt) == 1
        path.unlink()  # the state file is lost after the trade was written
        plan["pick"] = "short_strangle25"
        _patch(monkeypatch, _closes(), event="fomc:2026-10-29")  # would trigger a second trade today
        _force(monkeypatch, plan)
        assert await collect_assistant_paper(rt) == 0
        assert len(await rt.store.rec_paper_list()) == 1
        assert json.loads(path.read_text())["reason"] == "a method trade was already booked today"
    finally:
        await rt.aclose()


async def test_upcoming_event_reads_calendar_and_degrades(monkeypatch):
    import tradedesk.tools.macro as macro
    from tradedesk.core.errors import ProviderError

    now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    rows = [
        {
            "title": "FOMC Statement",
            "country": "USD",
            "date": "2026-10-28T14:00:00-04:00",
            "impact": "High",
            "forecast": "",
            "previous": "",
        },
        {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-27T08:30:00-04:00", "impact": "High"},
        {"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High"},
    ]
    monkeypatch.setattr(macro, "_load_seed", lambda: [])

    class FF:
        def __init__(self, week: list[dict[str, Any]] | None):
            self.week = week

        async def this_week(self):
            if self.week is None:
                raise ProviderError("forexfactory", "backend_error", "down")
            return Fetched(self.week, Evidence(provider="forexfactory", tool="thisweek"))

        async def next_week(self):
            return Fetched([], Evidence(provider="forexfactory", tool="nextweek"))

    class Rt:
        def __init__(self, week):
            self.forexfactory = FF(week)

    b = Builder()
    # NFP (dropped) is ignored; FOMC is 2 days ahead (within 72 h); the CPI is far
    assert await mod.upcoming_event(Rt(rows), b, now) == "fomc:2026-10-28"  # type: ignore[arg-type]
    only_far = [rows[2]]
    assert await mod.upcoming_event(Rt(only_far), Builder(), now) is None  # type: ignore[arg-type]
    down = Builder()
    assert await mod.upcoming_event(Rt(None), down, now) is None  # type: ignore[arg-type]
    assert [m.field for m in down.missing] == ["calendar:thisweek"]  # recorded, not raised


async def test_job_waits_for_09_utc(tmp_path, monkeypatch):
    _patch(monkeypatch, _closes())
    rt = _rt(tmp_path)
    rt.now = lambda: datetime(2026, 10, 10, 8, 30, tzinfo=UTC)
    try:
        assert await collect_assistant_paper(rt) == 0
        assert not state_path(rt.settings.db_path).exists()
    finally:
        await rt.aclose()


async def test_job_no_pick_writes_state_without_trade(tmp_path, monkeypatch):
    _patch(monkeypatch, _closes())
    monkeypatch.setattr(mod, "method_pick", lambda s, c: {"pick": None, "summary": None, "regime": "rv30_b1", "rv30": 0.3, "summaries": {}})
    rt = _rt(tmp_path)
    try:
        assert await collect_assistant_paper(rt) == 0
        st = json.loads(state_path(rt.settings.db_path).read_text())
        assert st["state"] == "no_longer_actual" and st["action"] == "none" and st["pick"] is None
        assert await rt.store.rec_paper_list() == []
    finally:
        await rt.aclose()


async def test_job_provider_failure_raises_and_retries(tmp_path, monkeypatch):
    _patch(monkeypatch, None)  # history down
    rt = _rt(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="no data"):
            await collect_assistant_paper(rt)
        assert not state_path(rt.settings.db_path).exists() and await rt.store.rec_paper_list() == []
    finally:
        await rt.aclose()
    _patch(monkeypatch, _closes())
    bad = _rt(tmp_path, status=500)  # Deribit down
    try:
        with pytest.raises(RuntimeError, match="no data"):
            await collect_assistant_paper(bad)
        assert not state_path(bad.settings.db_path).exists() and await bad.store.rec_paper_list() == []
    finally:
        await bad.aclose()
    ok = _rt(tmp_path)  # the next period of a healthy provider books the same day
    try:
        assert await collect_assistant_paper(ok) == 1
    finally:
        await ok.aclose()


async def test_fetch_closes_paginates_and_drops_today():
    day = 86_400_000
    start = mod.HISTORY_START_MS
    now = datetime.fromtimestamp((start + 2300 * day) / 1000 + 3600, UTC)  # 2300 complete days, then a partial bar
    calls: list[int] = []

    class Ex:
        async def ohlcv(self, ex_id, symbol, tf, limit, since_ms=None, until_ms=None):
            calls.append(since_ms)
            ts = [since_ms + i * day for i in range(limit) if since_ms + i * day <= start + 2300 * day]
            return Fetched([[t, 1.0, 1.0, 1.0, 100.0 + (t - start) // day, 1.0] for t in ts], Evidence(provider="stub", tool="ohlcv"))

    class Rt:
        exchanges = Ex()

    b = Builder()
    closes = await fetch_closes(Rt(), b, now)  # type: ignore[arg-type]
    assert closes is not None and len(closes) == 2300 and calls == [start, start + 1000 * day, start + 2000 * day]
    assert closes[0] == 100.0 and closes[-1] == 100.0 + 2299


def test_job_off_by_default_and_on_by_switch(tmp_path):
    off = Runtime(
        Settings(collector_enabled=True, db_path=str(tmp_path / "a.sqlite")), transports=make_transports(State()), exchange_factory=FakeEx
    )
    on = Runtime(
        Settings(collector_enabled=True, assistant_paper_enabled=True, db_path=str(tmp_path / "b.sqlite")),
        transports=make_transports(State()),
        exchange_factory=FakeEx,
    )
    assert Settings().assistant_paper_enabled is False
    assert "assistant_paper" not in [j.name for j in build_default_scheduler(off).jobs]
    assert "assistant_paper" in [j.name for j in build_default_scheduler(on).jobs]
