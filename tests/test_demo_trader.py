"""execution/demo_trader.py and bybit_demo.py against a fake venue (no network): selection, entry, shadow plans, lost
order answers, closes, time exit, break-even, kill-switch, failures after the order."""

from __future__ import annotations

from types import SimpleNamespace

import ccxt
import numpy as np
import pytest

from tradedesk.analytics import setup_signals as ss
from tradedesk.execution import bybit_demo, demo_trader
from tradedesk.execution.bybit_demo import BybitDemo, DemoOnlyError, MarketInfo
from tradedesk.execution.guard import DEFAULT, TAKER_FEE
from tradedesk.store import Store

BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"
H4, D1 = ss.TF_MS["4h"], ss.TF_MS["1d"]
T0 = 1_792_000_000_000 // D1 * D1  # a UTC midnight
SECRET = "SECRET123KEY"


def _rising_rows(n: int, bar_ms: int, end_ms: int) -> list[list[float]]:
    c = np.linspace(80_000, 100_000, n)
    return [[end_ms - (n - k) * bar_ms, c[k], c[k] * 1.01, c[k] * 0.99, c[k], 1.0] for k in range(n)]


def _sig(setup="S14", side="long", tf="4h", entry=100_000.0, stop=98_000.0, target=106_000.0, ts=T0 - H4) -> ss.LiveSignal:
    return ss.LiveSignal(setup, side, tf, ts, entry, stop, target, f"{setup} коментар", {"atr14": 500.0})


class FakeVenue(BybitDemo):
    def __init__(self, now: int):
        super().__init__(SECRET, "SECRET456SEC", factory=lambda k, s: SimpleNamespace())
        self.now = now
        self.last = {BTC: 100_000.0, ETH: 4_000.0}
        self.pos: dict = {}
        self.orders: list = []
        self.by_link: dict = {}
        self.pnl_rows: list = []
        self.windows: list = []
        self.stops: list = []
        self.fail: BaseException | None = None
        self.place_lands = False  # with ``fail``: the order reached the venue anyway (lost answer)
        self.positions_fail = False

    async def aclose(self):
        pass

    async def positions(self):
        if self.positions_fail:
            raise ccxt.NetworkError("timeout")
        return dict(self.pos)

    async def market(self, symbol):
        return MarketInfo(symbol, symbol.split("/")[0] + "USDT", 0.001, 0.001, 0.1)

    async def last_price(self, symbol):
        return self.last[symbol]

    async def equity_usd(self):
        return 10_000.0

    async def turnovers(self):
        return [(BTC, 5e9), ("USDC/USDT:USDT", 9e9)]  # one coin: the patched scan gives every coin the same signal

    async def ohlcv(self, symbol, tf, limit=1000):
        return _rising_rows(300, ss.TF_MS[tf], self.now - 60_000)

    async def prepare(self, symbol, leverage):
        self.orders.append(("prepare", symbol, leverage))

    def _fill(self, symbol, side, qty, link_id):
        self.pos[symbol] = {"symbol": symbol, "contracts": qty, "entryPrice": self.last[symbol], "markPrice": self.last[symbol]}
        self.by_link[link_id] = {
            "orderStatus": "Filled",
            "cumExecQty": str(qty),
            "avgPrice": str(self.last[symbol]),
            "orderId": "o-" + link_id,
            "updatedTime": str(self.now),
        }

    async def place_bracket(self, symbol, side, qty, stop, target, link_id):
        self.orders.append(("bracket", symbol, side, qty, stop, target, link_id))
        if self.fail is not None:
            if self.place_lands:
                self._fill(symbol, side, qty, link_id)
            raise self.fail
        self._fill(symbol, side, qty, link_id)
        return {"id": "o-" + link_id}

    async def order_by_link(self, market_id, link_id):
        return self.by_link.get(link_id)

    async def set_stop(self, market_id, stop):
        self.stops.append((market_id, stop))
        return {}

    async def close_position(self, symbol, side, qty, link_id):
        self.orders.append(("close", symbol, qty, link_id))
        return {"id": "c-1"}

    async def closed_pnl(self, market_id, start_ms, end_ms):
        self.windows.append((start_ms, end_ms))
        assert end_ms - max(start_ms, end_ms - bybit_demo.CLOSED_PNL_WINDOW_MS) <= 7 * D1  # Bybit's limit
        lo = max(start_ms, end_ms - bybit_demo.CLOSED_PNL_WINDOW_MS)
        return [r for r in self.pnl_rows if lo <= int(r["updatedTime"]) <= end_ms]


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_trader, "HALT_FILE", tmp_path / "demo.halt")
    signals: dict = {"4h": [_sig()], "1d": []}
    monkeypatch.setattr(demo_trader, "scan", lambda bars, tf, setups: list(signals.get(tf, [])))
    store = Store(tmp_path / "db.sqlite")
    await store.init()
    msgs: list[str] = []

    async def notify(t):
        msgs.append(t)

    now = T0 + 60_000
    w = SimpleNamespace(store=store, venue=FakeVenue(now), now=now, msgs=msgs, notify=notify, tmp=tmp_path, signals=signals)

    async def run(at=None, **kw):
        at = at or w.now
        w.venue.now = at
        return await demo_trader.cycle(store, w.venue, at, notify, **kw)

    w.run = run
    yield w
    await store.aclose()


async def _plans(store):
    cur = await store.conn.execute("SELECT verdict, tags, entry, stop, target FROM trade_plan ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


async def test_entry_is_sized_levered_journaled_shadowed_and_announced(world):
    w = world
    res = await w.run()
    assert res["sent"] == 1 and res["errors"] == 0, res
    assert ("prepare", BTC, 20) in w.venue.orders  # BTC tier 20x
    _, sym, side, qty, stop, target, lid = next(o for o in w.venue.orders if o[0] == "bracket")
    assert (sym, side) == (BTC, "long") and len(lid) <= 36
    assert qty * (100_000 - stop + TAKER_FEE * (100_000 + stop)) <= DEFAULT.risk_usd  # $100 at the stop with fees
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["leverage"] == 20 and t["score"] >= 2 and t["journal_id"] and t["plan_id"]
    j = await w.store.journal_get(t["journal_id"])
    assert {"demo-paper", "bybit-demo", "setup:S14", "auto"} <= set(j["tags"])
    plans = await _plans(w.store)
    assert len(plans) == 1 and plans[0]["verdict"] == "enter" and "demo:taken" in plans[0]["tags"]
    assert any("Демо: вхід S14 лонг BTC/USDT" in m and "плече 20x" in m and "Бали:" in m for m in w.msgs)
    n = len(w.venue.orders)
    await w.run(w.now + 3_600_000)  # the next hourly run does not trade the same signal again
    assert len([o for o in w.venue.orders[n:] if o[0] == "bracket"]) == 0


async def test_daily_cap_and_low_score_are_skipped_and_shadowed(world, monkeypatch):
    w = world
    for k in range(5):  # five trades already today
        await w.store.demo_trade_insert(
            {
                "link_id": f"old{k}",
                "venue": "bybit-demo",
                "symbol": f"X{k}/USDT:USDT",
                "side": "long",
                "setup": "S3",
                "timeframe": "4h",
                "signal_ts": T0,
                "entry": 1.0,
                "stop": 0.99,
                "target": 1.05,
                "qty": 1.0,
                "leverage": 5,
                "risk_usd": 1.0,
                "comment": "-",
                "status": "closed",
                "opened_ts": T0 + 1,
                "closed_ts": T0 + 2,
                "pnl_usd": 0.0,
            }
        )
    res = await w.run()
    assert res["sent"] == 0 and res.get("skipped") == 1
    skipped = await w.store.exec_audit_list(kinds=("skipped",))
    assert "trades today" in " ".join(skipped[0]["payload"]["violations"])
    assert (await _plans(w.store))[0]["verdict"] == "skip"

    monkeypatch.setattr(demo_trader, "score", lambda *a, **k: ss.Score(1, ("x",)))
    w.signals["4h"] = [_sig(ts=T0 - 2 * H4 + H4)]
    w.signals["4h"] = [_sig(setup="S16")]
    await w.store.conn.execute("DELETE FROM demo_trade")
    await w.store.conn.commit()
    res = await w.run()
    assert res["sent"] == 0 and res.get("skipped") == 1
    assert "score 1 < 2" in str((await w.store.exec_audit_list(kinds=("skipped",)))[0]["payload"])


async def test_best_scored_first_and_one_trade_per_coin(world, monkeypatch):
    w = world
    w.signals["4h"] = [_sig(setup="S3", target=104_500.0), _sig(setup="S14", target=110_000.0)]
    res = await w.run()
    brackets = [o for o in w.venue.orders if o[0] == "bracket"]
    assert res["sent"] == 1 and len(brackets) == 1 and brackets[0][5] == 110_000.0  # the higher R:R signal won
    assert res.get("skipped") == 1  # the other one: already in BTC


async def test_close_reads_a_7_day_window_and_closes_journal_with_r(world):
    w = world
    await w.run()
    t = (await w.store.demo_trade_list())[0]
    w.venue.pos.clear()
    later = w.now + 9 * D1  # more than 7 days later: the window must still be legal and contain the close
    w.venue.pnl_rows = [{"closedPnl": str(2 * t["risk_usd"]), "avgExitPrice": str(t["target"]), "updatedTime": str(later - 3_600_000)}]
    w.signals["4h"] = []
    res = await w.run(later)
    assert res["closed"] == 1
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "closed" and t["exit_reason"] == "target" and t["r_multiple"] == pytest.approx(2.0)
    assert (await w.store.journal_get(t["journal_id"]))["outcome"] == "win"
    assert any("✅ Демо: закрито S14" in m and "+2.00R" in m for m in w.msgs)


async def test_time_exit(world):
    w = world
    await w.run()
    w.signals["4h"] = []
    res = await w.run(w.now + demo_trader.MAX_HOLD_MS["4h"] + 1)
    assert res["time_exits"] == 1 and w.venue.orders[-1][0] == "close" and w.venue.orders[-1][3].startswith("tx-")
    assert (await w.store.demo_trade_list())[0]["exit_reason"] == "time"


async def test_lost_answer_keeps_unknown_and_the_next_sync_recovers_the_fill(world):
    w = world
    w.venue.fail, w.venue.place_lands = ccxt.RequestTimeout("timeout"), True
    res = await w.run()
    assert res.get("unknown") == 1 and (await w.store.demo_trade_list())[0]["status"] == "unknown"
    w.venue.fail = None
    w.signals["4h"] = [_sig(tf="4h", ts=T0 - H4)]  # same signal again: must not be re-entered
    await w.run(w.now + 3_600_000)
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["fill"] == 100_000.0 and t["journal_id"]
    assert len([o for o in w.venue.orders if o[0] == "bracket"]) == 1


async def test_unknown_order_the_venue_never_saw_becomes_rejected(world):
    w = world
    w.venue.fail = ccxt.RequestTimeout("timeout")
    await w.run()
    w.venue.fail = None
    w.signals["4h"] = []
    await w.run(w.now + demo_trader.UNKNOWN_GRACE_MS + 60_000)
    assert (await w.store.demo_trade_list())[0]["status"] == "rejected"


async def test_explicit_refusal_is_rejected_without_secrets(world):
    w = world
    w.venue.fail = ccxt.InsufficientFunds(f"bybit {{'retMsg': 'insufficient', 'api_key': '{SECRET}'}} origin_string[1{SECRET}x]")
    res = await w.run()
    assert res.get("rejected") == 1
    rej = await w.store.exec_audit_list(kinds=("rejected",))
    assert rej and SECRET not in str(rej)


async def test_failure_after_the_order_does_not_double_the_coin(world, monkeypatch):
    w = world
    w.signals["4h"] = [_sig()]
    w.signals["1d"] = [_sig(tf="1d", ts=T0 - D1, setup="S16")]

    async def broken(**kw):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(w.store, "journal_insert", broken)

    async def bad_notify(t):
        raise RuntimeError("telegram down")

    res = await demo_trader.cycle(w.store, w.venue, w.now, bad_notify)
    assert len([o for o in w.venue.orders if o[0] == "bracket"]) == 1 and res["errors"] >= 2
    assert (await w.store.demo_trade_list(("open",)))[0]["symbol"] == BTC


async def test_breakeven_at_30_percent_roe_once(world):
    w = world
    await w.run()
    w.signals["4h"] = []
    w.venue.pos[BTC]["markPrice"] = 101_000.0  # +1 % at 20x = +20 % ROE: not yet
    res = await demo_trader.manage_only(w.store, w.venue, w.now + 60_000, w.notify)
    assert res["breakeven"] == 0 and not w.venue.stops
    w.venue.pos[BTC]["markPrice"] = 101_600.0  # +1.6 % at 20x = +32 % ROE
    res = await demo_trader.manage_only(w.store, w.venue, w.now + 120_000, w.notify)
    assert res["breakeven"] == 1 and w.venue.stops == [("BTCUSDT", pytest.approx(100_000 * (1 + 2 * TAKER_FEE), abs=0.1))]
    await demo_trader.manage_only(w.store, w.venue, w.now + 180_000, w.notify)
    assert len(w.venue.stops) == 1  # once per trade
    assert any("беззбиток" in m for m in w.msgs)
    t = (await w.store.demo_trade_list())[0]
    w.venue.pos.clear()
    w.venue.pnl_rows = [{"closedPnl": "-0.5", "avgExitPrice": str(t["be_stop"]), "updatedTime": str(w.now + 600_000)}]
    await w.run(w.now + 3_600_000)
    assert (await w.store.demo_trade_list())[0]["exit_reason"] == "breakeven"


async def test_kill_switch_refuses_but_still_shadows(world):
    w = world
    (w.tmp / "demo.halt").write_text("stop")
    res = await w.run()
    assert res["sent"] == 0 and res.get("refused") == 1 and not [o for o in w.venue.orders if o[0] == "bracket"]
    assert "kill-switch" in str(await w.store.exec_audit_list(kinds=("refused",)))
    assert (await _plans(w.store))[0]["verdict"] == "skip"


async def test_unreadable_positions_skip_everything(world):
    w = world
    w.venue.positions_fail = True
    res = await w.run()
    assert res["errors"] == 1 and res["skipped_entries"] and not w.venue.orders


async def test_dry_run_sends_nothing_and_marks_its_audit(world):
    w = world
    res = await w.run(dry_run=True)
    assert res.get("would_send") == 1 and not [o for o in w.venue.orders if o[0] == "bracket"]
    kinds = {a["kind"] for a in await w.store.exec_audit_list()}
    assert kinds and all(k.startswith("dry_") for k in kinds) and await _plans(w.store) == []
    res = await w.run()  # a real run afterwards still takes the signal
    assert res["sent"] == 1


async def test_stale_signal_is_ignored(world):
    w = world
    res = await w.run(w.now + 3 * 3_600_000)
    assert res["signals"] == 0 and not w.venue.orders


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


@pytest.mark.parametrize(
    "ex",
    [
        SimpleNamespace(
            options={"enableDemoTrading": True},
            hostname="bybit.com",
            urls={"api": {"public": "https://api.bybit.com", "private": "https://api.bybit.com"}},
        ),
        SimpleNamespace(
            options={"enableDemoTrading": False}, hostname="bybit.com", urls={"api": {"private": "https://api-demo.{hostname}"}}
        ),
        SimpleNamespace(options={"enableDemoTrading": True}, hostname="evil.com", urls={"api": {"private": "https://api-demo.{hostname}"}}),
        SimpleNamespace(
            options={"enableDemoTrading": True},
            hostname="bybit.com",
            urls={"api": {"a": "https://api-demo.{hostname}", "b": "https://api.bybit.com"}},
        ),
    ],
)
async def test_client_refuses_private_calls_off_the_demo_host(ex):
    c = BybitDemo("k", "s", factory=lambda k, s: ex)
    for call in (c.positions(), c.place_bracket(BTC, "long", 0.01, 1.0, 2.0, "x"), c.set_stop("BTCUSDT", 1.0), c.equity_usd()):
        with pytest.raises(DemoOnlyError):
            await call


async def test_real_factory_points_only_at_the_demo_host():
    c = BybitDemo("k", "s")
    try:
        c._demo_only()  # no exception
    finally:
        await c.aclose()


def test_is_refusal_tells_venue_answers_from_network_errors():
    assert bybit_demo.is_refusal(ccxt.InsufficientFunds("x")) and bybit_demo.is_refusal(ccxt.InvalidOrder("x"))
    assert not bybit_demo.is_refusal(ccxt.RequestTimeout("x")) and not bybit_demo.is_refusal(ccxt.NetworkError("x"))
    assert not bybit_demo.is_refusal(RuntimeError("x"))


def test_keys_file(tmp_path):
    p = tmp_path / "k.env"
    assert bybit_demo.load_keys(p) is None
    p.write_text("BYBIT_DEMO_API_KEY=abc\nBYBIT_DEMO_API_SECRET=def\n")
    assert bybit_demo.load_keys(p) == ("abc", "def")
