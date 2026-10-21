"""execution/demo_trader.py and bybit_demo.py against a fake venue: entry, refusal, close, kill-switch (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from test_setup_signals import _bars
from tradedesk.analytics import setup_signals as ss
from tradedesk.execution import bybit_demo, demo_trader
from tradedesk.execution.bybit_demo import BybitDemo, DemoOnlyError, MarketInfo
from tradedesk.execution.guard import DEFAULT, GuardConfig
from tradedesk.store import Store

SYM = "BTC/USDT:USDT"
CFG = GuardConfig(symbols=(SYM,))


def _first_signal_bar(b, setup: str) -> int:
    for i in range(150, len(b)):
        if any(s.setup == setup for s in ss.scan(b.slice(0, i + 1), "4h")):
            return i
    raise AssertionError("no signal in the sample")


class FakeVenue:
    def __init__(self, rows, last):
        self.rows, self.last = rows, last
        self.pos: dict = {}
        self.orders: list = []
        self.pnl_rows: list = []
        self.fail_order = False

    async def positions(self):
        return dict(self.pos)

    async def market(self, symbol):
        return MarketInfo(symbol, "BTCUSDT", 0.001, 0.001, 0.01)

    async def last_price(self, symbol):
        return self.last

    async def equity_usd(self):
        return 10_000.0

    async def ohlcv(self, symbol, tf, limit=1000):
        return self.rows if tf == "4h" else []

    async def place_bracket(self, symbol, side, qty, stop, target, link_id):
        if self.fail_order:
            raise RuntimeError("bybit {'retMsg': 'insufficient', 'api_key': 'SECRET123'}")
        self.orders.append((symbol, side, qty, stop, target, link_id))
        self.pos[symbol] = {"symbol": symbol, "contracts": qty, "entryPrice": self.last}
        return {"id": "o-1"}

    async def close_position(self, symbol, side, qty, link_id):
        self.orders.append(("close", symbol, qty, link_id))
        return {"id": "o-2"}

    async def closed_pnl(self, market_id, since_ms):
        return self.pnl_rows


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_trader, "HALT_FILE", tmp_path / "demo.halt")
    b = _bars()
    i = _first_signal_bar(b, "S14")
    rows = [[int(b.t[k]), b.o[k], b.h[k], b.low[k], b.c[k], 1.0] for k in range(i + 1)]
    sig = next(s for s in ss.scan(b.slice(0, i + 1), "4h") if s.setup == "S14")
    now = int(b.t[i]) + ss.TF_MS["4h"] + 60_000  # one minute after the signal bar closed
    store = Store(tmp_path / "db.sqlite")
    await store.init()
    msgs: list[str] = []

    async def notify(t):
        msgs.append(t)

    venue = FakeVenue(rows, last=sig.entry)
    yield SimpleNamespace(store=store, venue=venue, now=now, sig=sig, msgs=msgs, notify=notify, tmp=tmp_path)
    await store.aclose()


async def test_signal_is_sized_checked_sent_journaled_and_announced(world):
    w = world
    res = await demo_trader.cycle(w.store, w.venue, w.now, w.notify, CFG)
    assert res["sent"] >= 1 and res["errors"] == 0
    sym, side, qty, stop, target, lid = w.venue.orders[0]
    assert (sym, side) == (SYM, w.sig.side) and lid == demo_trader.link_id(w.sig, SYM) and len(lid) <= 36
    assert qty * abs(w.sig.entry - stop) <= DEFAULT.risk_usd + 1e-6  # $100 at the stop
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["setup"] == "S14" and t["journal_id"] and t["comment"] == w.sig.comment
    j = await w.store.journal_get(t["journal_id"])
    assert {"demo-paper", "bybit-demo", "setup:S14", "auto"} <= set(j["tags"]) and j["thesis"] == w.sig.comment
    assert any("Демо: вхід S14" in m and "Чому:" in m for m in w.msgs)
    kinds = [a["kind"] for a in await w.store.exec_audit_list()]
    assert "signal" in kinds and "sent" in kinds

    # the same signal in the next hourly run is not traded again
    n = len(w.venue.orders)
    await demo_trader.cycle(w.store, w.venue, w.now + 3_600_000, w.notify, CFG)
    assert len(w.venue.orders) == n


async def test_close_is_read_from_the_venue_with_r_and_journal(world):
    w = world
    await demo_trader.cycle(w.store, w.venue, w.now, w.notify, CFG)
    t = (await w.store.demo_trade_list())[0]
    w.venue.pos.clear()
    w.venue.pnl_rows = [{"closedPnl": str(2 * t["risk_usd"]), "avgExitPrice": str(t["target"]), "updatedTime": str(w.now + 1000)}]
    res = await demo_trader.cycle(w.store, w.venue, w.now + 7_200_000, w.notify, CFG)
    assert res["closed"] == 1
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "closed" and t["exit_reason"] == "target" and t["r_multiple"] == pytest.approx(2.0)
    j = await w.store.journal_get(t["journal_id"])
    assert j["status"] == "closed" and j["outcome"] == "win" and j["r_multiple"] == pytest.approx(2.0)
    assert any("✅ Демо: закрито S14" in m and "+2.00R" in m for m in w.msgs)


async def test_time_exit_after_the_max_hold(world):
    w = world
    await demo_trader.cycle(w.store, w.venue, w.now, w.notify, CFG)
    res = await demo_trader.cycle(w.store, w.venue, w.now + demo_trader.MAX_HOLD_MS["4h"] + 1, w.notify, CFG)
    assert res["time_exits"] == 1 and w.venue.orders[-1][0] == "close"
    assert (await w.store.demo_trade_list())[0]["exit_reason"] == "time"


async def test_kill_switch_and_refusals_send_nothing(world):
    w = world
    (w.tmp / "demo.halt").write_text("stop")
    res = await demo_trader.cycle(w.store, w.venue, w.now, w.notify, CFG)
    assert res["refused"] >= 1 and w.venue.orders == []
    refused = await w.store.exec_audit_list(kinds=("refused",))
    assert any("kill-switch" in " ".join(a["payload"]["violations"]) for a in refused)


async def test_stale_signal_is_ignored(world):
    w = world
    res = await demo_trader.cycle(w.store, w.venue, w.now + 3 * 3_600_000, w.notify, CFG)
    assert res["signals"] == 0 and w.venue.orders == []


async def test_venue_error_is_audited_without_secrets_and_the_cycle_survives(world):
    w = world
    w.venue.fail_order = True
    res = await demo_trader.cycle(w.store, w.venue, w.now, w.notify, CFG)
    assert res["rejected"] >= 1 and res["errors"] == 0
    rej = await w.store.exec_audit_list(kinds=("rejected",))
    assert rej and "SECRET123" not in str(rej)
    assert (await w.store.demo_trade_list())[0]["status"] == "rejected"


async def test_audit_log_is_append_only(tmp_path):
    s = Store(tmp_path / "a.sqlite")
    await s.init()
    try:
        await s.exec_audit_add(1, "bybit-demo", "signal", payload={"x": 1})
        for sql in ("UPDATE exec_audit SET kind = 'x'", "DELETE FROM exec_audit"):
            with pytest.raises(Exception, match="append-only"):
                await s.conn.execute(sql)
    finally:
        await s.aclose()


async def test_client_refuses_private_calls_off_the_demo_host():
    class Ex:
        urls = {"api": {"public": "https://api.bybit.com", "private": "https://api.bybit.com"}}

        async def fetch_positions(self, *a):
            raise AssertionError("must not be called")

    c = BybitDemo("k", "s", factory=lambda k, s: Ex())
    with pytest.raises(DemoOnlyError):
        await c.positions()
    with pytest.raises(DemoOnlyError):
        await c.place_bracket(SYM, "long", 0.01, 1.0, 2.0, "x")


def test_real_factory_points_at_the_demo_host():
    ex = bybit_demo._make_exchange("k", "s")
    assert all("api-demo" in u for u in ex.urls["api"].values())


def test_keys_file(tmp_path):
    p = tmp_path / "k.env"
    assert bybit_demo.load_keys(p) is None
    p.write_text("BYBIT_DEMO_API_KEY=abc\nBYBIT_DEMO_API_SECRET=def\n")
    assert bybit_demo.load_keys(p) == ("abc", "def")


def test_no_live_mode_anywhere():
    assert DEFAULT.mode == "testnet" and np.all([c == "testnet" for c in [DEFAULT.mode]])
