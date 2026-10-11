from __future__ import annotations

import json

import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.journal import outcome_of, pnl, r_multiple, summary
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


# ---------- pure analytics ----------


def test_pnl_long_and_short():
    assert pnl("long", size=2, entry=100.0, exit_=110.0) == pytest.approx(20.0)
    assert pnl("short", size=2, entry=100.0, exit_=90.0) == pytest.approx(20.0)
    assert pnl("short", size=2, entry=100.0, exit_=110.0) == pytest.approx(-20.0)


def test_r_multiple_variants():
    # long: entry 100, stop 90 → risk 10 → exit 115 → +1.5R
    assert r_multiple("long", entry=100.0, stop=90.0, exit_=115.0) == pytest.approx(1.5)
    # short: entry 100, stop 110 → risk 10 → exit 85 → +1.5R
    assert r_multiple("short", entry=100.0, stop=110.0, exit_=85.0) == pytest.approx(1.5)
    # Missing stop → None
    assert r_multiple("long", entry=100.0, stop=None, exit_=115.0) is None
    # Incoherent stop above entry on long → None
    assert r_multiple("long", entry=100.0, stop=110.0, exit_=115.0) is None


def test_outcome_classification():
    assert outcome_of(100.0) == "win"
    assert outcome_of(-5.0) == "loss"
    assert outcome_of(0.0) == "break_even"
    assert outcome_of(0.0, cancelled=True) == "cancelled"


def test_summary_over_closed_trades():
    trades = [
        {"status": "closed", "pnl": 100.0, "outcome": "win", "r_multiple": 2.0},
        {"status": "closed", "pnl": -50.0, "outcome": "loss", "r_multiple": -1.0},
        {"status": "closed", "pnl": 25.0, "outcome": "win", "r_multiple": 0.5},
        {"status": "open", "pnl": None, "outcome": None, "r_multiple": None},  # ignored
    ]
    s = summary(trades)
    assert s["closed_trades"] == 3
    assert s["wins"] == 2 and s["losses"] == 1
    assert s["win_rate"] == pytest.approx(2 / 3)
    assert s["total_pnl"] == pytest.approx(75.0)
    assert s["expectancy_per_trade"] == pytest.approx(25.0)
    assert s["avg_r"] == pytest.approx(0.5)


def test_summary_empty():
    assert summary([]) == {"closed_trades": 0}


# ---------- MCP tool round-trip ----------


async def _rt(state, tmp_path) -> Runtime:
    """Use a real on-disk SQLite file — ``:memory:`` would reset between
    `async with Client(server)` blocks because the server lifespan runs
    ``rt.aclose()`` and the next client opens a brand-new empty database."""
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "journal.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_journal_add_list_close_and_stats(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        # add two trades
        a = await _call(
            srv, "journal_add", symbol="BTC/USDT:USDT", side="long", size=1.0, entry=80000.0, stop=78000.0, tags=["btc", "scalp"]
        )
        b = await _call(srv, "journal_add", symbol="ETH/USDT:USDT", side="short", size=10.0, entry=2500.0, stop=2600.0, tags=["eth"])
        assert a["data"]["id"] == 1 and b["data"]["id"] == 2

        listed = await _call(srv, "journal_list", status="open")
        assert listed["data"]["count"] == 2

        # close first (win: +2.5R, pnl +5000); close second (loss, pnl -500)
        close_a = await _call(srv, "journal_close", id=1, exit=85000.0)
        d_a = close_a["data"]
        assert d_a["status"] == "closed" and d_a["outcome"] == "win"
        assert d_a["pnl"] == pytest.approx(5000.0)
        assert d_a["r_multiple"] == pytest.approx(2.5)

        close_b = await _call(srv, "journal_close", id=2, exit=2550.0)
        d_b = close_b["data"]
        assert d_b["outcome"] == "loss"
        assert d_b["pnl"] == pytest.approx(-500.0)
        assert d_b["r_multiple"] == pytest.approx(-0.5)

        # stats
        stats = (await _call(srv, "journal_stats"))["data"]
        assert stats["closed_trades"] == 2
        assert stats["wins"] == 1 and stats["losses"] == 1
        assert stats["win_rate"] == 0.5
        assert stats["total_pnl"] == pytest.approx(4500.0)
        assert stats["avg_r"] == pytest.approx(1.0)

        # tag filter
        btc_only = (await _call(srv, "journal_list", tag="btc"))["data"]
        assert btc_only["count"] == 1 and btc_only["trades"][0]["id"] == 1
    finally:
        await rt.aclose()


async def test_journal_close_cancelled_path(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        a = await _call(srv, "journal_add", symbol="SOL/USDT:USDT", side="long", size=10.0, entry=180.0, stop=170.0)
        env = await _call(srv, "journal_close", id=a["data"]["id"], cancelled=True, note="skipped — missed entry")
        d = env["data"]
        assert d["status"] == "cancelled" and d["outcome"] == "cancelled"
        assert d["pnl"] is None and d["r_multiple"] is None
        # second close should fail
        again = await _call(srv, "journal_close", id=a["data"]["id"], exit=200.0)
        assert again["ok"] is False
    finally:
        await rt.aclose()


async def test_journal_close_requires_exit_unless_cancelled(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        a = await _call(srv, "journal_add", symbol="BTC", side="long", size=1.0, entry=80000.0, stop=78000.0)
        env = await _call(srv, "journal_close", id=a["data"]["id"])
        assert env["ok"] is False
    finally:
        await rt.aclose()
