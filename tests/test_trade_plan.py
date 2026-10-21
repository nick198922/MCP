from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from tradedesk.analytics.trade_plan import geometry, r_multiple, resolve_plan, summarize_plans, validate_levels
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.collector.trade_plan import collect_plan_resolve
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

H = 3600
TF = H * 1000
T0 = 1_791_590_400  # 2026-10-10 00:00 UTC
FEE = 0.0005


def bar(i: int, o: float, h: float, lo: float, c: float) -> list[float]:
    return [(T0 + i * H) * 1000, o, h, lo, c, 1.0]


def plan(side="long", etype="market", entry=100.0, stop=98.0, target=105.0, hours=10, created=T0 + 600) -> dict:
    return {
        "side": side,
        "entry_type": etype,
        "entry": entry,
        "stop": stop,
        "target": target,
        "created_ts": created,
        "expires_ts": T0 + hours * H,
    }


def resolve(p, bars, now=T0 + 24 * H):
    return resolve_plan(p, bars, tf_ms=TF, now_ts=now, fee=FEE)


FLAT = [bar(i, 100.0, 100.5, 99.5, 100.0) for i in range(0, 24)]


# ----------------------------------------------------------------------------- analytics


def test_validate_and_geometry():
    assert validate_levels("long", 100, 98, 105) is None and validate_levels("short", 100, 102, 95) is None
    assert "stop < entry < target" in validate_levels("long", 100, 101, 105)
    assert "target < entry < stop" in validate_levels("short", 100, 98, 95)
    assert validate_levels("flat", 100, 98, 105) is not None and validate_levels("long", 100, 0, 105) is not None
    g = geometry(100.0, 98.0, 105.0)
    assert g["rr"] == pytest.approx(2.5) and g["risk_pct"] == pytest.approx(0.02) and g["fee_r_round_trip"] == pytest.approx(0.05)
    assert r_multiple("short", 100, 102, 100, 95, fee=0.0) == pytest.approx(2.5)


def test_market_long_target_skips_creation_candle():
    bars = [bar(0, 100, 106, 99, 100), bar(1, 100, 101, 99.5, 100.5), bar(2, 100.5, 106, 100, 105.5)]
    res = resolve(plan(), bars)
    # candle 0 contains the creation time (00:10) and is ignored although it touched the target
    assert res["status"] == "resolved" and res["outcome"] == "target" and res["exit"] == 105.0
    assert res["filled_ts"] == T0 + 600 and res["fill"] == 100.0 and res["resolved_ts"] == T0 + 3 * H
    assert res["r_gross"] == pytest.approx(2.5)
    assert res["r_result"] == pytest.approx((5.0 - FEE * 205.0) / 2.0)


def test_stop_gap_exits_at_open_and_both_touched_is_stop():
    gap = resolve(plan(), [bar(1, 100, 100.5, 99.5, 100), bar(2, 97, 97.5, 96, 97)])
    assert gap["outcome"] == "stop" and gap["exit"] == 97.0 and gap["r_gross"] == pytest.approx(-1.5)
    both = resolve(plan(), [bar(1, 100, 106, 97, 101)])
    assert both["outcome"] == "stop" and both["exit"] == 98.0 and both["r_gross"] == pytest.approx(-1.0)


def test_limit_fill_candle_target_ignored_then_hit():
    p = plan(etype="limit", entry=99.0, stop=97.0, target=103.0)
    bars = [bar(1, 100, 100.5, 99.5, 100), bar(2, 100, 103.5, 98.8, 101), bar(3, 101, 103.2, 100.5, 103)]
    res = resolve(p, bars)
    assert res["filled_ts"] == T0 + 2 * H and res["fill"] == 99.0  # filled in candle 2, its target print does not count
    assert res["outcome"] == "target" and res["resolved_ts"] == T0 + 4 * H and res["r_gross"] == pytest.approx(2.0)


def test_limit_fill_candle_stop_counts():
    p = plan(etype="limit", entry=99.0, stop=97.0, target=103.0)
    res = resolve(p, [bar(1, 100, 100.5, 99.5, 100), bar(2, 100, 104, 96.5, 98)])
    assert res["outcome"] == "stop" and res["exit"] == 97.0 and res["r_gross"] == pytest.approx(-1.0)


def test_stop_entry_gap_fills_worse_then_expires_at_close():
    p = plan(etype="stop", entry=102.0, stop=99.0, target=110.0, hours=4)
    bars = [bar(1, 100, 101, 99.5, 100.5), bar(2, 103, 104, 102.5, 103.5), bar(3, 103.5, 104, 102, 102.5), bar(4, 102.5, 109, 102, 108)]
    res = resolve(p, bars)
    # filled at the open 103 (gap through 102); window = candles closing by 04:00, so candle 4 (04:00-05:00) is outside
    assert res["fill"] == 103.0 and res["outcome"] == "expired" and res["exit"] == 102.5
    assert res["resolved_ts"] == T0 + 4 * H and res["r_gross"] == pytest.approx(-0.5 / 3.0)
    assert res["r_result"] == pytest.approx((-0.5 - FEE * 205.5) / 3.0)


def test_short_limit_target_and_short_market_stop():
    p = plan(side="short", etype="limit", entry=100.0, stop=102.0, target=95.0)
    bars = [bar(1, 99, 99.5, 98.5, 99), bar(2, 99, 100.2, 98.8, 99.5), bar(3, 99.5, 99.8, 94.9, 95.5)]
    res = resolve(p, bars)
    assert res["filled_ts"] == T0 + 2 * H and res["outcome"] == "target" and res["r_gross"] == pytest.approx(2.5)
    assert res["r_result"] == pytest.approx((5.0 - FEE * 195.0) / 2.0)
    s = resolve(plan(side="short", entry=100.0, stop=102.0, target=95.0), [bar(1, 100, 102.1, 99, 101)])
    assert s["outcome"] == "stop" and s["exit"] == 102.0 and s["r_gross"] == pytest.approx(-1.0)


def test_expired_waits_for_the_last_candle_and_ignores_later_ones():
    p = plan(hours=5)
    bars = [bar(i, 100, 100.5, 99.5, 100 + i * 0.1) for i in range(1, 4)]  # candle 4 (04:00-05:00) missing
    pending = resolve(p, bars)
    assert pending["status"] == "open" and pending["fill"] == 100.0
    bars += [bar(4, 100.3, 100.6, 99.9, 100.4), bar(5, 100.4, 106, 100, 105)]  # candle 5 hits the target after the horizon
    res = resolve(p, bars)
    assert res["outcome"] == "expired" and res["exit"] == 100.4 and res["resolved_ts"] == T0 + 5 * H
    assert res["r_gross"] == pytest.approx(0.2)


def test_not_filled_and_open_before_horizon():
    p = plan(etype="limit", entry=95.0, stop=93.0, target=99.0, hours=6)
    res = resolve(p, FLAT)
    assert res["outcome"] == "not_filled" and res["r_result"] is None and res["fill"] is None and res["resolved_ts"] == T0 + 6 * H
    early = resolve(p, FLAT, now=T0 + 3 * H)
    assert early["status"] == "open" and early["outcome"] is None
    # the incomplete candle at `now` is not used: a touch inside it waits for the candle to close
    m = resolve(plan(), [bar(1, 100, 100.5, 99.5, 100), bar(2, 100, 106, 99.5, 105)], now=T0 + 2 * H + 1800)
    assert m["status"] == "open"


def test_summary_by_verdict_and_tag():
    rows = [
        {"status": "resolved", "verdict": "enter", "outcome": "target", "r_result": 2.0, "tags": ["idea:x"]},
        {"status": "resolved", "verdict": "enter", "outcome": "stop", "r_result": -1.0, "tags": ["idea:x", "src:own"]},
        {"status": "resolved", "verdict": "enter", "outcome": "expired", "r_result": 0.5, "tags": []},
        {"status": "resolved", "verdict": "enter", "outcome": "not_filled", "r_result": None, "tags": ["idea:x"]},
        {"status": "resolved", "verdict": "skip", "outcome": "stop", "r_result": -1.0, "tags": ["idea:x"]},
        {"status": "open", "verdict": "enter", "outcome": None, "r_result": None, "tags": ["idea:x"]},
    ]
    s = summarize_plans(rows)
    e = s["by_verdict"]["enter"]
    assert e["n"] == 4 and e["n_filled"] == 3 and e["outcomes"] == {"target": 1, "stop": 1, "expired": 1, "not_filled": 1}
    assert e["hit_rate"] == pytest.approx(1 / 3) and e["win_rate"] == pytest.approx(2 / 3)
    assert e["mean_r"] == pytest.approx(0.5) and e["median_r"] == pytest.approx(0.5)
    h = 4.303 * 1.5 / math.sqrt(3)  # t(0.975, df 2) * sd / sqrt(n)
    assert e["ci95_mean_r"] == pytest.approx([0.5 - h, 0.5 + h])
    assert s["by_verdict"]["wait"]["n"] == 0 and s["by_verdict"]["wait"]["mean_r"] is None
    assert s["by_verdict"]["skip"]["mean_r"] == pytest.approx(-1.0) and s["by_verdict"]["skip"]["ci95_mean_r"] is None
    assert set(s["by_tag"]) == {"idea:x", "src:own"}
    assert s["by_tag"]["idea:x"]["enter"]["n"] == 3 and s["by_tag"]["idea:x"]["skip"]["n"] == 1
    assert "skip" not in s["by_tag"]["src:own"]


# ----------------------------------------------------------------------------- tools

CREATED = datetime(2026, 10, 10, 5, 30, tzinfo=UTC)


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


def _rt(tmp_path, **settings) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "plans.sqlite"), **settings),
        transports=make_transports(State()),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: CREATED
    return rt


def _ts(h: float) -> int:
    return int(CREATED.timestamp() + h * H)


async def test_plan_add_review_roundtrip(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        # FakeEx 1h candles rise 0.1 % an hour from 80080 at 05:00 (high +0.1 %, low -0.1 %) up to the 12:00 candle
        a = await _call(
            srv, "plan_add", exchange="binance", symbol="BTC/USDT:USDT", instrument="perp", side="long", entry_type="market",
            entry=80100.0, stop=79000.0, target=80500.0, expires_ts=_ts(24), verdict="enter", thesis="t",
            rules={"1": "pass", "5": "fail"}, tags=["src:own", "idea:demo"],
        )  # fmt: skip
        assert a["ok"] is True, a
        assert a["data"]["id"] == 1 and a["data"]["rr"] == pytest.approx(400 / 1100, abs=1e-3)
        assert a["data"]["risk_pct"] == pytest.approx(1100 / 80100, abs=1e-5)
        assert any("rule 5" in w for w in a["warnings"]) and any("failed rules ['5']" in w for w in a["warnings"])
        assert any("not proven edge" in w or "not a proven edge" in w for w in a["warnings"])
        b = await _call(
            srv, "plan_add", exchange="binance", symbol="BTC/USDT:USDT", instrument="perp", side="short", entry_type="limit",
            entry=81000.0, stop=82000.0, target=79000.0, expires_ts=_ts(3), verdict="skip", tags=["idea:demo"],
        )  # fmt: skip
        assert b["ok"] is True and b["data"]["id"] == 2
        rt.now = lambda: NOW  # 13:07: candles up to the 12:00 one are in the data
        rv = await _call(srv, "plan_review")
        assert rv["ok"] is True, rv
        assert sorted(rv["data"]["resolved_now"]) == [1, 2] and rv["data"]["open"] == []
        by = {p["id"]: p for p in rv["data"]["recent_resolved"]}
        assert by[1]["outcome"] == "target" and by[1]["exit"] == 80500.0 and by[1]["timeframe"] == "1h"
        assert by[1]["r_result"] == pytest.approx((400 - FEE * 160600) / 1100, abs=1e-3)
        assert by[2]["outcome"] == "not_filled" and by[2]["r_result"] is None
        summ = rv["data"]["summary"]
        assert summ["by_verdict"]["enter"]["n"] == 1 and summ["by_verdict"]["enter"]["hit_rate"] == 1.0
        assert summ["by_verdict"]["skip"]["outcomes"]["not_filled"] == 1
        assert summ["by_tag"]["idea:demo"]["enter"]["n"] == 1 and summ["by_tag"]["idea:demo"]["skip"]["n"] == 1
        lst = await _call(srv, "plan_list", verdict="skip")
        assert lst["data"]["count"] == 1 and lst["data"]["plans"][0]["id"] == 2
        assert (await _call(srv, "plan_list", tag="src:own"))["data"]["count"] == 1
        await rt.store.init()
        assert await rt.store.table_count("journal") == 0  # plans never write the owner's journal
        j = await _call(srv, "journal_add", symbol="BTC/USDT:USDT", side="long", size=0.1, entry=80100.0, stop=79000.0)
        ln = await _call(srv, "plan_link", id=1, journal_id=j["data"]["id"])
        assert ln["ok"] and ln["data"]["journal_id"] == j["data"]["id"]
        assert (await _call(srv, "plan_link", id=99, journal_id=1))["ok"] is False
        again = await _call(srv, "plan_review")
        assert again["data"]["resolved_now"] == [] and again["data"]["summary"]["by_verdict"]["enter"]["n"] == 1
    finally:
        await rt.aclose()


async def test_plan_add_validation(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    base = dict(exchange="binance", symbol="BTC/USDT:USDT", instrument="perp", side="long", entry_type="market", verdict="enter")
    try:
        cases = [
            dict(entry=100.0, stop=101.0, target=105.0, expires_ts=_ts(24)),  # stop on the wrong side
            dict(entry=100.0, stop=98.0, target=99.0, expires_ts=_ts(24)),  # target on the wrong side
            dict(entry=100.0, stop=98.0, target=105.0, expires_ts=_ts(1)),  # horizon too short
            dict(entry=100.0, stop=98.0, target=105.0, expires_ts=_ts(-5)),  # in the past
            dict(entry=100.0, stop=98.0, target=105.0, expires_ts=_ts(24), rules={"9": "pass"}),
            dict(entry=100.0, stop=98.0, target=105.0, expires_ts=_ts(24), rec_paper_id=1),  # perp with rec link
        ]
        for extra in cases:
            out = await _call(srv, "plan_add", **base, **extra)
            assert out["ok"] is False and out["missing"][0]["reason"] == "invalid_input", extra
        opt = await _call(
            srv, "plan_add", **{**base, "instrument": "option"}, entry=100.0, stop=98.0, target=105.0, expires_ts=_ts(24), rec_paper_id=7
        )
        assert opt["ok"] is False and opt["missing"][0]["field"] == "rec_paper_id"
        await rt.store.init()
        assert await rt.store.plan_list() == []
    finally:
        await rt.aclose()


async def test_option_plan_scored_by_rec_paper_or_underlying(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        await rt.store.init()
        rec_id = await rt.store.rec_paper_insert(
            opened_ts=_ts(0), currency="BTC", structure="long_straddle", expiry_ts=_ts(5), spot=80000.0, forward=80000.0,
            t_years=0.01, legs=[[80000.0, True, 1.0, 0.5, 900.0]], premium_per_btc=900.0, entry_fees_per_btc=24.0,
            max_loss_per_btc=950.0, risk_basis="max_loss", size_btc=0.1, deposit_usd=10000.0, risk_pct=1.0, risk_usd=95.0,
            within_risk=True,
        )  # fmt: skip
        common = dict(exchange="binance", symbol="BTC/USDT", instrument="option", side="long", entry_type="market", verdict="enter")
        linked = await _call(
            srv, "plan_add", **common, entry=80100.0, stop=79000.0, target=80500.0, expires_ts=_ts(24), rec_paper_id=rec_id
        )
        bare = await _call(srv, "plan_add", **common, entry=80100.0, stop=79000.0, target=80500.0, expires_ts=_ts(24))
        assert linked["ok"] and bare["ok"] and any("underlying" in w for w in bare["warnings"])
        rt.now = lambda: NOW
        rv = await _call(srv, "plan_review")
        assert rv["data"]["resolved_now"] == [bare["data"]["id"]]  # the linked one waits for its paper trade
        (res,) = rv["data"]["recent_resolved"]
        assert "underlying" in res["note"]
        await rt.store.init()
        await rt.store.rec_paper_settle(
            rec_id, settled_ts=_ts(5), delivery_price=81000.0, pnl_usd=7.6, pnl_pct_spot=0.001, r_worst_case=0.08, r_study=0.08
        )
        rv = await _call(srv, "plan_review")
        assert rv["data"]["resolved_now"] == [linked["data"]["id"]]
        got = {p["id"]: p for p in rv["data"]["recent_resolved"]}[linked["data"]["id"]]
        assert got["outcome"] == "expired" and got["exit"] == 81000.0 and got["r_result"] == pytest.approx(0.08)
    finally:
        await rt.aclose()


async def test_provider_failure_keeps_plan_open(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        a = await _call(
            srv, "plan_add", exchange="binance", symbol="DOWN/USDT", instrument="spot", side="long", entry_type="market",
            entry=100.0, stop=98.0, target=105.0, expires_ts=_ts(3), verdict="wait",
        )  # fmt: skip
        assert a["ok"] is True
        rt.now = lambda: NOW
        rv = await _call(srv, "plan_review")
        assert rv["data"]["resolved_now"] == [] and len(rv["data"]["open"]) == 1
        assert rv["missing"] and all(m["field"].startswith("ohlcv:binance:DOWN/USDT") for m in rv["missing"])
        with pytest.raises(RuntimeError, match="missing"):
            await collect_plan_resolve(rt)
        await rt.store.init()
        assert (await rt.store.plan_get(a["data"]["id"]))["status"] == "open"
    finally:
        await rt.aclose()


async def test_no_store_configured():
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports(State()), exchange_factory=FakeEx)
    srv, _ = build_server(rt)
    try:
        assert (await _call(srv, "plan_review"))["ok"] is False
        assert (await _call(srv, "plan_list"))["ok"] is False
    finally:
        await rt.aclose()


# ----------------------------------------------------------------------------- collector job


async def test_job_resolves_and_is_switched_by_flag(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        await _call(
            srv, "plan_add", exchange="binance", symbol="BTC/USDT:USDT", instrument="perp", side="long", entry_type="market",
            entry=80100.0, stop=79000.0, target=80500.0, expires_ts=_ts(24), verdict="enter",
        )  # fmt: skip
        rt.now = lambda: NOW
        assert await collect_plan_resolve(rt) == 1
        assert await collect_plan_resolve(rt) == 0
    finally:
        await rt.aclose()
    off = _rt(tmp_path, collector_enabled=True)
    on = _rt(tmp_path, collector_enabled=True, plan_resolve_enabled=True)
    assert Settings().plan_resolve_enabled is False
    assert "plan_resolve" not in [j.name for j in build_default_scheduler(off).jobs]
    assert "plan_resolve" in [j.name for j in build_default_scheduler(on).jobs]
