"""execution/demo_trader.py and bybit_demo.py against a fake venue (no network): selection, maker entries (re-quote,
market rest, missed), target limits, shadow plans, lost order answers, closes, time exit, break-even, kill-switch,
failures after the order."""

from __future__ import annotations

from types import SimpleNamespace

import ccxt
import numpy as np
import pytest

from tradedesk.analytics import setup_signals as ss
from tradedesk.execution import bybit_demo, demo_trader, orders
from tradedesk.execution.bybit_demo import CROSS, BybitDemo, DemoOnlyError, MarketInfo, RiskTier
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
        self.book = {BTC: (99_990.0, 100_010.0), ETH: (3_999.0, 4_001.0)}
        self.tp_fail: BaseException | None = None
        self.pos: dict = {}
        self.orders: list = []
        self.by_link: dict = {}
        self.pnl_rows: list = []
        self.windows: list = []
        self.stops: list = []
        self.fail: BaseException | None = None
        self.place_lands = False  # with ``fail``: the order reached the venue anyway (lost answer)
        self.positions_fail = False
        self.mode = CROSS
        self.mode_changes = 0
        self.prepare_fail: BaseException | None = None

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

    async def quote(self, symbol):
        return self.book[symbol]

    async def equity_usd(self):
        return 10_000.0

    async def turnovers(self):
        # one crypto coin: the patched scan gives every coin the same signal
        return [(BTC, 5e9, ""), ("USDC/USDT:USDT", 9e9, ""), ("NVDA/USDT:USDT", 8e9, "stock")]

    async def risk_tier(self, symbol):
        return RiskTier(0.005, 100.0, 2_000_000.0)

    async def margin_mode(self):
        return self.mode

    async def ensure_cross(self, change=True):
        was = self.mode
        if was != CROSS and change:
            self.mode, self.mode_changes = CROSS, self.mode_changes + 1
        return was

    async def ohlcv(self, symbol, tf, limit=1000):
        return _rising_rows(300, ss.TF_MS[tf], self.now - 60_000)

    async def prepare(self, symbol, leverage):
        self.orders.append(("prepare", symbol, leverage))
        if self.prepare_fail is not None:
            raise self.prepare_fail

    def _fill(self, symbol, side, qty, link_id, px=None, status="Filled"):
        px = px or self.last[symbol]
        old = self.pos.get(symbol)
        q0, e0 = (old["contracts"], old["entryPrice"]) if old else (0.0, 0.0)
        q = round(q0 + qty, 9)
        self.pos[symbol] = {
            "symbol": symbol, "contracts": q, "entryPrice": (q0 * e0 + qty * px) / q, "markPrice": px, "stopLossPrice": None,
            "maintenanceMargin": q * px * 0.005,
        }  # fmt: skip
        o = self.by_link.setdefault(link_id, {"orderId": "o-" + link_id, "qty": str(qty), "cumExecQty": "0"})
        o.update(orderStatus=status, cumExecQty=str(float(o["cumExecQty"]) + qty), avgPrice=str(px), updatedTime=str(self.now))

    def fill_limit(self, link_id, qty=None):
        """The resting limit trades (fully, or ``qty`` of it)."""
        o = self.by_link[link_id]
        rest = float(o["qty"]) - float(o["cumExecQty"])
        q = rest if qty is None else qty
        self._fill(o["symbol"], o["side"], q, link_id, float(o["price"]), "Filled" if q >= rest - 1e-12 else "PartiallyFilled")

    async def place_bracket(self, symbol, side, qty, stop, target, link_id, price=None):
        self.orders.append(("limit" if price else "bracket", symbol, side, qty, stop, target, link_id, price))
        if self.fail is not None:
            if self.place_lands:
                self._fill(symbol, side, qty, link_id, price)
            raise self.fail
        if price is None:
            self._fill(symbol, side, qty, link_id)
        else:
            self.by_link[link_id] = {
                "orderId": "o-" + link_id, "orderStatus": "New", "qty": str(qty), "cumExecQty": "0", "price": price,
                "symbol": symbol, "side": side,
            }  # fmt: skip
        return {"id": "o-" + link_id}

    async def place_reduce_limit(self, symbol, side, qty, price, link_id, post_only):
        self.orders.append(("reduce", symbol, side, qty, price, link_id, post_only))
        if self.tp_fail is not None and link_id.startswith("tp-"):
            raise self.tp_fail
        self.by_link[link_id] = {"orderId": "o-" + link_id, "orderStatus": "New", "qty": str(qty), "cumExecQty": "0", "price": price}
        return {"id": "o-" + link_id}

    async def amend(self, symbol, link_id, price, qty=None):
        self.orders.append(("amend", symbol, link_id, price, qty))
        self.by_link[link_id]["price"] = price
        if qty is not None:
            self.by_link[link_id]["qty"] = str(qty)
        return {}

    async def cancel(self, symbol, link_id):
        self.orders.append(("cancel", symbol, link_id))
        o = self.by_link.get(link_id)
        if not o or o["orderStatus"] not in ("New", "PartiallyFilled"):
            return False
        o["orderStatus"] = "Cancelled"
        return True

    async def set_take_profit(self, symbol, target):
        self.orders.append(("tp_market", symbol, target))
        return {}

    async def order_by_link(self, market_id, link_id):
        return self.by_link.get(link_id)

    async def set_stop(self, symbol, stop):
        self.stops.append((symbol, stop))
        if symbol in self.pos:
            self.pos[symbol]["stopLossPrice"] = stop
        return {}

    async def close_position(self, symbol, side, qty, link_id):
        self.orders.append(("close", symbol, qty, link_id))
        return {"id": "c-1"}

    def kinds(self, *names):
        return [o for o in self.orders if o[0] in names]

    async def closed_pnl(self, market_id, start_ms, end_ms):
        self.windows.append((start_ms, end_ms))
        assert end_ms - max(start_ms, end_ms - bybit_demo.CLOSED_PNL_WINDOW_MS) <= 7 * D1  # Bybit's limit
        lo = max(start_ms, end_ms - bybit_demo.CLOSED_PNL_WINDOW_MS)
        return [r for r in self.pnl_rows if lo <= int(r["updatedTime"]) <= end_ms]


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_trader, "HALT_FILE", tmp_path / "demo.halt")
    off = tmp_path / "demo-options.off"  # the futures tests run without the option leg (tests/test_demo_option_leg.py)
    off.write_text("off")
    monkeypatch.setattr(demo_trader, "OPTIONS_OFF_FILE", off)
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

    async def manage(at):
        w.venue.now = at
        return await demo_trader.manage_only(store, w.venue, at, notify)

    async def enter(at=None):
        """The hourly run places the limit, it fills, the minute job completes the entry."""
        at = at or w.now
        res = await run(at)
        w.venue.fill_limit((await store.demo_trade_list(("pending",)))[0]["link_id"])
        await manage(at + 60_000)
        return res

    w.run, w.manage, w.enter = run, manage, enter
    yield w
    await store.aclose()


async def _plans(store):
    cur = await store.conn.execute("SELECT verdict, tags, entry, stop, target FROM trade_plan ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


async def test_entry_is_a_maker_limit_then_journaled_with_a_target_limit(world):
    w = world
    res = await w.run()
    assert res["sent"] == 1 and res["errors"] == 0, res
    assert ("prepare", BTC, 20) in w.venue.orders  # BTC tier 20x
    _, sym, side, qty, stop, target, lid, px = w.venue.kinds("limit")[0]
    assert (sym, side, target, px) == (BTC, "long", None, 99_990.0) and len(lid) <= 36  # post-only at the bid, no market TP
    assert qty * (100_000 - stop + TAKER_FEE * (100_000 + stop)) <= DEFAULT.risk_usd  # $100 at the stop with fees
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "pending" and t["leverage"] == 20 and t["score"] >= 2 and t["plan_id"] and not t["journal_id"]
    plans = await _plans(w.store)
    assert len(plans) == 1 and plans[0]["verdict"] == "enter" and "demo:taken" in plans[0]["tags"]
    assert any("Демо: вхід S14 лонг BTC/USDT" in m and "Лімітний ордер (мейкер) по 99,990" in m and "Бали:" in m for m in w.msgs)
    res = await w.manage(w.now + 60_000)  # still the best bid: nothing to do
    assert w.venue.kinds("amend", "cancel", "reduce") == []
    w.venue.fill_limit(lid)
    res = await w.manage(w.now + 120_000)
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["fill"] == 99_990.0 and t["entry_kind"] == "maker" and t["journal_id"]
    assert t["risk_usd"] <= DEFAULT.risk_usd and t["tp_link"] == "tp-" + lid[3:]
    assert {"demo-paper", "bybit-demo", "setup:S14", "auto"} <= set((await w.store.journal_get(t["journal_id"]))["tags"])
    assert w.venue.kinds("reduce") == [("reduce", BTC, "long", qty, t["target"], "tp-" + lid[3:], False)]
    assert any("виконано лімітом (мейкер)" in m and "лімітка" in m for m in w.msgs)
    await w.run(w.now + 3_600_000)  # the next hourly run does not trade the same signal again
    assert len(w.venue.kinds("limit", "bracket")) == 1


async def test_quote_moving_away_is_followed_then_the_rest_goes_to_the_market(world):
    w = world
    await w.run()
    t = (await w.store.demo_trade_list())[0]
    w.venue.book[BTC] = (100_190.0, 100_210.0)  # +0.1R: re-quote at the new bid, size shrinks for the same $100
    await w.manage(w.now + 60_000)
    (_, _, lid, px, qty), *_ = w.venue.kinds("amend")
    assert (lid, px) == (t["link_id"], 100_190.0) and qty < t["qty"]
    assert (await w.store.demo_trade_list())[0]["entry_px"] == 100_190.0
    w.venue.fill_limit(lid, qty=qty / 2)  # half fills, the rest waits
    await w.manage(w.now + orders.ENTRY_WAIT_MS + 60_000)  # too long: cancel the rest, send it at the market
    assert ("cancel", BTC, lid) in w.venue.orders
    _, _, _, mqty, _, target, em, price = w.venue.kinds("bracket")[0]
    assert em == "em-" + lid[3:] and target is None and price is None and 0 < mqty < qty
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["entry_kind"] == "mixed" and t["risk_usd"] <= DEFAULT.risk_usd * 1.0001
    assert any("частково лімітом, решта по ринку" in m for m in w.msgs)


async def test_price_running_away_cancels_the_entry_as_missed(world):
    w = world
    await w.run()
    lid = (await w.store.demo_trade_list())[0]["link_id"]
    w.venue.book[BTC] = (100_800.0, 100_810.0)  # +0.4R toward the target
    await w.manage(w.now + 60_000)
    t = (await w.store.demo_trade_list())[0]
    assert ("cancel", BTC, lid) in w.venue.orders and not w.venue.kinds("bracket", "amend")
    assert t["status"] == "missed" and "ran" in t["note"] and not t["journal_id"]
    assert any("не виконано" in m and "ціна пішла до цілі" in m for m in w.msgs)
    assert await w.store.exec_audit_list(kinds=("missed",))


async def test_partial_fill_kept_when_the_price_runs_away(world):
    w = world
    await w.run()
    t = (await w.store.demo_trade_list())[0]
    w.venue.fill_limit(t["link_id"], qty=0.01)
    w.venue.book[BTC] = (100_800.0, 100_810.0)
    await w.manage(w.now + 60_000)
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["qty"] == pytest.approx(0.01) and t["entry_kind"] == "maker"
    assert t["risk_usd"] == pytest.approx(0.01 * (99_990 - t["stop"] + TAKER_FEE * (99_990 + t["stop"])))
    assert w.venue.kinds("reduce")[0][3] == pytest.approx(0.01)  # the target limit covers the filled size only


async def test_post_only_cancelled_by_the_venue_goes_to_the_market_when_still_near(world):
    w = world
    await w.run()
    lid = (await w.store.demo_trade_list())[0]["link_id"]
    w.venue.by_link[lid]["orderStatus"] = "Cancelled"  # the book moved: post-only would have taken
    await w.manage(w.now + 60_000)
    assert w.venue.kinds("bracket")[0][6] == "em-" + lid[3:]
    assert (await w.store.demo_trade_list())[0]["entry_kind"] == "taker"


async def test_refused_target_limit_falls_back_to_the_market_take_profit(world):
    w = world
    w.venue.tp_fail = ccxt.InvalidOrder("reduce-only refused")
    await w.enter()
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["tp_link"] is None and w.venue.kinds("tp_market") == [("tp_market", BTC, t["target"])]


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
                "pnl_usd": -1.0,  # five losses: the risk of the day is spent
            }
        )
    res = await w.run()
    assert res["sent"] == 0 and res.get("skipped") == 1
    skipped = await w.store.exec_audit_list(kinds=("skipped",))
    assert "trades at risk today" in " ".join(skipped[0]["payload"]["violations"])
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
    assert res["sent"] == 1 and len(w.venue.kinds("limit")) == 1
    assert (await w.store.demo_trade_list())[0]["target"] == 110_000.0  # the higher R:R signal won
    assert res.get("skipped") == 1  # the other one: already in BTC


async def test_close_reads_a_7_day_window_and_closes_journal_with_r(world):
    w = world
    await w.enter()
    t = (await w.store.demo_trade_list())[0]
    w.venue.pos.clear()
    w.venue.by_link[t["tp_link"]]["orderStatus"] = "Filled"
    later = w.now + 9 * D1  # more than 7 days later: the window must still be legal and contain the close
    w.venue.pnl_rows = [
        {
            "closedPnl": str(2 * t["risk_usd"]),
            "avgExitPrice": str(t["target"]),
            "updatedTime": str(later - 3_600_000),
            "openFee": "1.0",
            "closeFee": "1.1",
        }
    ]
    w.signals["4h"] = []
    res = await w.run(later)
    assert res["closed"] == 1
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "closed" and t["exit_reason"] == "target" and t["r_multiple"] == pytest.approx(2.0)
    assert t["exit_kind"] == "maker" and t["fees_usd"] == pytest.approx(2.1)
    assert (await w.store.journal_get(t["journal_id"]))["outcome"] == "win"
    assert any("✅ Демо: закрито S14" in m and "+2.00R" in m and "комісії 2.10 USD" in m for m in w.msgs)


async def test_stop_close_cancels_the_target_limit_left_on_the_book(world):
    w = world
    await w.enter()
    t = (await w.store.demo_trade_list())[0]
    w.venue.pos.clear()
    w.venue.pnl_rows = [{"closedPnl": str(-t["risk_usd"]), "avgExitPrice": str(t["stop"]), "updatedTime": str(w.now + 600_000)}]
    w.signals["4h"] = []
    await w.run(w.now + 3_600_000)
    t = (await w.store.demo_trade_list())[0]
    assert t["exit_reason"] == "stop" and t["exit_kind"] == "taker" and t["fees_usd"] is None
    assert ("cancel", BTC, t["tp_link"]) in w.venue.orders


async def test_time_exit_rests_a_limit_first_then_goes_to_the_market(world):
    w = world
    await w.enter()
    w.signals["4h"] = []
    t0 = w.now + demo_trader.MAX_HOLD_MS["4h"] + 120_000  # the hold counts from the fill (a minute after the run)
    res = await w.run(t0)
    lid = (await w.store.demo_trade_list())[0]["link_id"]
    assert res["time_exits"] == 1 and ("cancel", BTC, "tp-" + lid[3:]) in w.venue.orders  # the target limit goes
    assert w.venue.kinds("reduce")[-1][4:] == (100_010.0, "xl-" + lid[3:], True)  # a long sells at the ask, post-only
    assert not w.venue.kinds("close")
    w.venue.book[BTC] = (99_900.0, 99_920.0)
    await w.manage(t0 + 60_000)  # the ask fell: re-quote
    assert w.venue.kinds("amend")[-1][2:4] == ("xl-" + lid[3:], 99_920.0)
    await w.manage(t0 + orders.EXIT_WAIT_MS)  # not filled in time: market
    close = w.venue.kinds("close")
    assert len(close) == 1 and close[0][3] == "tx-" + lid[3:]
    t = (await w.store.demo_trade_list())[0]
    assert t["exit_reason"] == "time" and t["exit_kind"] == "taker"
    await w.manage(t0 + orders.EXIT_WAIT_MS + 60_000)
    assert len(w.venue.kinds("close")) == 1  # sent once


async def test_time_exit_near_the_stop_goes_to_the_market_at_once(world):
    w = world
    await w.enter()
    w.signals["4h"] = []
    t = (await w.store.demo_trade_list())[0]
    w.venue.book[BTC] = (t["stop"] + 100, t["stop"] + 120)  # within 0.25R of the stop
    await w.run(w.now + demo_trader.MAX_HOLD_MS["4h"] + 120_000)
    assert len(w.venue.kinds("close")) == 1 and not [o for o in w.venue.kinds("reduce") if o[5].startswith("xl-")]


async def test_lost_answer_keeps_unknown_and_the_next_sync_recovers_the_fill(world):
    w = world
    w.venue.fail, w.venue.place_lands = ccxt.RequestTimeout("timeout"), True  # the limit landed and filled
    res = await w.run()
    assert res.get("unknown") == 1 and (await w.store.demo_trade_list())[0]["status"] == "unknown"
    w.venue.fail = None
    w.signals["4h"] = [_sig(tf="4h", ts=T0 - H4)]  # same signal again: must not be re-entered
    await w.run(w.now + 3_600_000)
    t = (await w.store.demo_trade_list())[0]
    assert t["status"] == "open" and t["fill"] == 99_990.0 and t["journal_id"] and t["entry_kind"] == "maker"
    assert len(w.venue.kinds("limit", "bracket")) == 1


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
    assert len(w.venue.kinds("limit")) == 1 and res["errors"] >= 1
    t = (await w.store.demo_trade_list(("pending",)))[0]
    assert t["symbol"] == BTC
    w.venue.fill_limit(t["link_id"])
    res = await demo_trader.manage_only(w.store, w.venue, w.now + 60_000, bad_notify)
    t = (await w.store.demo_trade_list(("open",)))[0]
    assert res["errors"] >= 2 and t["tp_link"] and not t["journal_id"]  # the journal failed, the target is still there


async def test_breakeven_half_the_way_to_the_target_once(world):
    w = world
    await w.enter()
    w.signals["4h"] = []
    w.venue.pos[BTC]["markPrice"] = 102_000.0  # a third of the way from 99 990 to 106 000 (+40 % ROE at 20x): not yet
    res = await demo_trader.manage_only(w.store, w.venue, w.now + 60_000, w.notify)
    assert res["breakeven"] == 0 and not w.venue.stops
    w.venue.pos[BTC]["markPrice"] = 103_100.0  # 52 % of the way
    res = await demo_trader.manage_only(w.store, w.venue, w.now + 120_000, w.notify)
    assert res["breakeven"] == 1 and w.venue.stops == [(BTC, pytest.approx(99_990 * (1 + 2 * TAKER_FEE), abs=0.1))]
    assert w.venue.stops[0][1] >= 99_990 * (1 + 2 * TAKER_FEE) - 1e-6  # rounded away from the entry
    await demo_trader.manage_only(w.store, w.venue, w.now + 180_000, w.notify)
    assert len(w.venue.stops) == 1  # once per trade
    assert any("беззбиток" in m and "пройдено 52 % шляху до цілі (+1.6R)" in m for m in w.msgs)
    t = (await w.store.demo_trade_list())[0]
    w.venue.pos.clear()
    w.venue.pnl_rows = [{"closedPnl": "-0.5", "avgExitPrice": str(t["be_stop"]), "updatedTime": str(w.now + 600_000)}]
    await w.run(w.now + 3_600_000)
    assert (await w.store.demo_trade_list())[0]["exit_reason"] == "breakeven"


async def test_kill_switch_refuses_but_still_shadows(world):
    w = world
    (w.tmp / "demo.halt").write_text("stop")
    res = await w.run()
    assert res["sent"] == 0 and res.get("refused") == 1 and not w.venue.kinds("limit", "bracket")
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
    assert res.get("would_send") == 1 and not w.venue.kinds("limit", "bracket")
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


async def test_breakeven_already_on_the_venue_is_only_recorded(world):
    """The stop moved but the DB write failed last minute: the next run records it without a second venue call."""
    w = world
    await w.enter()
    w.venue.pos[BTC].update(markPrice=103_100.0, stopLossPrice=100_200.0)
    res = await demo_trader.manage_only(w.store, w.venue, w.now + 60_000, w.notify)
    assert res["breakeven"] == 1 and w.venue.stops == [] and (await w.store.demo_trade_list())[0]["be_ts"]


async def test_account_is_switched_to_cross_once_and_stocks_are_out(world):
    w = world
    w.venue.mode = "ISOLATED_MARGIN"
    res = await w.run()
    assert w.venue.mode_changes == 1 and res["universe"] == 1 and res["sent"] == 1
    assert (await w.store.exec_audit_list(kinds=("margin_mode",)))[0]["payload"]["was"] == "ISOLATED_MARGIN"
    await w.run(w.now + 3_600_000)
    assert w.venue.mode_changes == 1


async def test_failure_before_the_order_refuses_the_signal_once(world):
    w = world
    w.venue.prepare_fail = ccxt.ExchangeError("leverage not allowed")
    res = await w.run()
    assert res.get("refused") == 1 and res["sent"] == 0 and not w.venue.kinds("limit", "bracket")
    assert await w.store.demo_trade_list() == []
    w.venue.prepare_fail = None
    res = await w.run(w.now + 600_000)
    assert res["sent"] == 0  # one shot: the refused signal is not retried


async def test_dry_run_does_not_touch_open_trades(world):
    w = world
    await w.enter()
    w.venue.pos[BTC]["markPrice"] = 101_600.0
    w.signals["4h"] = []
    n = len(w.venue.orders)
    await w.run(w.now + demo_trader.MAX_HOLD_MS["4h"] + 1, dry_run=True)
    assert w.venue.orders[n:] == [] and w.venue.stops == []
    assert (await w.store.demo_trade_list())[0]["exit_reason"] is None


async def _today(store, k, **kw):
    row = {
        "link_id": f"d{k}", "venue": "bybit-demo", "symbol": f"X{k}/USDT:USDT", "side": "long", "setup": "S3", "timeframe": "4h",
        "signal_ts": T0, "entry": 1.0, "stop": 0.99, "target": 1.05, "qty": 1.0, "leverage": 5, "risk_usd": 1.0, "comment": "-",
        "status": "open", "opened_ts": T0 + 1,
    }  # fmt: skip
    await store.demo_trade_insert({**row, **kw})


async def test_break_even_frees_a_slot_of_the_daily_cap(world):
    """Owner 22.10: five entries today, two of them with the stop at break-even and one closed at break-even: three
    slots are free again; a loss keeps its slot."""
    w = world
    await _today(w.store, 0, be_ts=T0 + 2, be_stop=1.001)
    await _today(w.store, 1, be_ts=T0 + 2, be_stop=1.001)
    await _today(w.store, 2, status="closed", closed_ts=T0 + 3, pnl_usd=-0.05)  # break-even exit: slippage only
    await _today(w.store, 3, status="closed", closed_ts=T0 + 3, pnl_usd=-1.0)  # a stop: keeps its slot
    await _today(w.store, 4)
    res = await w.run()
    assert res["sent"] == 1 and res["trades_today"] == 2 and res["entries_today"] == 5


async def test_s12_rejected_by_the_backtest_is_scanned_and_traded_under_the_same_caps(world, monkeypatch):
    """BACKLOG 0.5 item "в": S12 joins the demo with its own setup tag; the guard and the daily caps apply as to the rest."""
    w = world
    asked: list = []

    def scan(bars, tf, setups):
        asked.append(tuple(setups))
        return list(w.signals.get(tf, []))

    monkeypatch.setattr(demo_trader, "scan", scan)
    assert demo_trader.SETUPS == ("S14", "S16", "S3", "S12", "S38", "S39", "S40")
    w.signals["4h"] = [_sig(setup="S12", target=104_500.0)]
    await w.enter()
    assert asked and all(a == demo_trader.SETUPS for a in asked)
    t = (await w.store.demo_trade_list())[0]
    assert t["setup"] == "S12" and t["status"] == "open" and t["link_id"].startswith("td-S12-l-4h-BTC-")
    assert "setup:S12" in (await w.store.journal_get(t["journal_id"]))["tags"]
    assert any("Демо: вхід S12 лонг BTC/USDT" in m for m in w.msgs)
    plans = await _plans(w.store)
    assert "setup:S12" in plans[0]["tags"] and plans[0]["verdict"] == "enter"
    # the daily cap: five trades at risk today, a new S12 signal is skipped like any other
    await w.store.conn.execute("DELETE FROM demo_trade")
    await w.store.conn.commit()
    for k in range(5):
        await _today(w.store, k)
    w.venue.pos.clear()
    w.signals["4h"] = [_sig(setup="S12", target=104_500.0, ts=T0)]
    res = await w.run(T0 + H4 + 60_000)
    assert res["sent"] == 0 and res.get("skipped") == 1
    skipped = await w.store.exec_audit_list(kinds=("skipped",))
    assert skipped[0]["setup"] == "S12" and "trades at risk today" in " ".join(skipped[0]["payload"]["violations"])


def _coins(w, n: int) -> list[str]:
    """``n`` crypto coins at the same price as BTC in the fake venue's universe (the patched scan gives each the same
    signals)."""
    coins = [f"C{k}/USDT:USDT" for k in range(n)]
    for s in coins:
        w.venue.last[s], w.venue.book[s] = 100_000.0, (99_990.0, 100_010.0)

    async def turnovers():
        return [(s, 1e9 - k, "") for k, s in enumerate(coins)]

    w.venue.turnovers = turnovers
    return coins


async def test_s12_cap_keeps_three_of_five_slots_for_the_other_setups(world):
    """Critic 25.10, owner 25.10 (Q-20261025-1: 2): S12 ranks first on every coin here, yet takes only 2 slots; the S3 signals of
    the other coins get the rest, and the skipped S12 signals say why."""
    w = world
    assert demo_trader.SETUP_SLOTS == {"S12": 2}
    _coins(w, 5)
    w.signals["4h"] = [_sig(setup="S12", target=106_000.0), _sig(setup="S3", target=104_000.0)]
    res = await w.run()
    sent = [o[6] for o in w.venue.kinds("limit")]
    assert res["sent"] == 5 and sorted(lid.split("-")[1] for lid in sent) == ["S12", "S12", "S3", "S3", "S3"]
    assert {lid.split("-")[4] for lid in sent if "-S12-" in lid} == {"C0", "C1"}  # the best two, then the cap
    skipped = {a["link_id"]: a["payload"]["violations"] for a in await w.store.exec_audit_list(kinds=("skipped",))}
    s12_skipped = [v for lid, v in skipped.items() if "-S12-" in lid]
    assert len(s12_skipped) == 3 and all(any("trades at risk today = S12 cap 2" in x for x in v) for v in s12_skipped)
    assert not any("S12 cap" in x for lid, v in skipped.items() if "-S3-" in lid for x in v)  # other setups: no cap


async def test_s12_cap_counts_todays_s12_slots_at_risk(world, monkeypatch):
    """Two S12 trades at risk today fill the cap; one at break-even does not count (the daily-slot rule); an unreadable
    book keeps S12 out rather than uncapped."""
    w = world
    await _today(w.store, 0, setup="S12")
    await _today(w.store, 1, setup="S12", status="closed", closed_ts=T0 + 3, pnl_usd=-1.0)  # a stop keeps its slot
    await _today(w.store, 2, setup="S12", be_ts=T0 + 2, be_stop=1.001)  # break-even: free
    _coins(w, 1)
    w.signals["4h"] = [_sig(setup="S12", target=106_000.0), _sig(setup="S3", target=104_000.0)]
    res = await w.run()
    assert res["sent"] == 1 and w.venue.kinds("limit")[0][6].startswith("td-S3-l-4h-C0-")
    (sk,) = await w.store.exec_audit_list(kinds=("skipped",))
    assert sk["setup"] == "S12" and any("2 S12 trades at risk today" in x for x in sk["payload"]["violations"])
    # one S12 slot frees (its stop moves to break-even): the next S12 signal is taken, then the cap holds again
    await w.store.conn.execute("UPDATE demo_trade SET be_ts = ?, be_stop = 1.001 WHERE link_id = 'd0'", (T0 + 5,))
    await w.store.conn.commit()
    before = len(w.venue.kinds("limit"))
    _coins(w, 3)
    w.signals["4h"] = [_sig(setup="S12", target=106_000.0, ts=T0)]
    res = await w.run(T0 + H4 + 60_000)
    new = [o[6] for o in w.venue.kinds("limit")[before:]]
    assert res["sent"] == 1 and len(new) == 1 and new[0].startswith("td-S12-l-4h-")

    async def broken(ctx):
        raise RuntimeError("db locked")

    monkeypatch.setattr(demo_trader, "_setup_slots", broken)
    before = len(w.venue.kinds("limit"))
    _coins(w, 3)
    w.signals["4h"] = [_sig(setup="S12", target=106_000.0, ts=T0 + H4), _sig(setup="S3", target=104_000.0, ts=T0 + H4)]
    res = await w.run(T0 + 2 * H4 + 60_000)
    new = [o[6] for o in w.venue.kinds("limit")[before:]]
    assert res["sent"] >= 1 and new and all(lid.startswith("td-S3-") for lid in new) and res["errors"] >= 1


def _short(setup: str, ts: int = T0 - H4) -> ss.LiveSignal:
    """A short with R:R 5 (two score points from R:R alone; the fake candles rise, so no trend point)."""
    return _sig(setup=setup, side="short", stop=102_000.0, target=90_000.0, ts=ts)


async def test_short_setups_s38_s39_are_traded_with_their_tags_under_the_same_caps(world):
    """IDEA-20261024-short-setups-demo step 3: S38 and S39 enter as shorts with their own setup tag, journal and shadow
    plan; S39 has no per-bar cap, but its own pool (owner 25.10, Q-20261025-2): two S39 shorts fill the pool's two slots
    and the others are skipped by it."""
    w = world
    w.signals["4h"] = [_short("S39")]
    await w.enter()
    t = (await w.store.demo_trade_list())[0]
    assert t["setup"] == "S39" and t["side"] == "short" and t["status"] == "open" and t["link_id"].startswith("td-S39-s-4h-BTC-")
    assert "setup:S39" in (await w.store.journal_get(t["journal_id"]))["tags"]
    assert any("Демо: вхід S39 шорт BTC/USDT" in m for m in w.msgs)
    assert "setup:S39" in (await _plans(w.store))[0]["tags"]
    await w.store.conn.execute("DELETE FROM demo_trade")
    await w.store.conn.commit()
    w.venue.pos.clear()
    _coins(w, 6)
    w.signals["4h"] = [_short("S39", ts=T0)]
    res = await w.run(T0 + H4 + 60_000)
    sent = [o[6] for o in w.venue.kinds("limit")[1:]]
    assert res["sent"] == 2 and all(lid.startswith("td-S39-s-4h-") for lid in sent)
    skipped = await w.store.exec_audit_list(kinds=("skipped",))
    assert len(skipped) == 4
    for sk in skipped:
        assert sk["setup"] == "S39" and any("2 trades at risk today = max" in x for x in sk["payload"]["violations"])
        assert not any("per bar" in x for x in sk["payload"]["violations"])


async def test_pool_setups_have_two_slots_above_the_five_and_do_not_take_theirs(world, monkeypatch):
    """Owner 25.10 (Q-20261025-2, Q-20261025-3): with the day's five slots taken by S3, S39 and S40 (a long) still
    enter, two in all; the pool's trades do not count toward the five, so an S3 signal enters once one S3 slot frees;
    an unreadable book keeps the pool out and counts its trades toward the five."""
    w = world
    for k in range(5):
        await _today(w.store, k)  # S14 trades at risk today: the five are full
    _coins(w, 4)
    w.signals["4h"] = [_short("S39", ts=T0), _sig(setup="S40", target=106_000.0, ts=T0), _sig(setup="S3", target=104_000.0, ts=T0)]
    res = await w.run(T0 + H4 + 60_000)
    sent = [o[6] for o in w.venue.kinds("limit")]
    assert res["sent"] == 2 and {lid.split("-")[1] for lid in sent} <= {"S39", "S40"}
    reasons = {a["link_id"]: a["payload"]["violations"] for a in await w.store.exec_audit_list(kinds=("skipped",))}
    assert any(any("5 trades at risk today = max" in x for x in v) for lid, v in reasons.items() if "-S3-" in lid)
    assert any(any("2 trades at risk today = max" in x for x in v) for lid, v in reasons.items() if "-S40-" in lid or "-S39-" in lid)
    # one S3-day slot frees: an S3 signal enters although seven trades were at risk, the pool's two do not count
    await w.store.conn.execute("UPDATE demo_trade SET be_ts = ?, be_stop = 1.001 WHERE link_id = 'd0'", (T0 + 5,))
    await w.store.conn.commit()
    before = len(w.venue.kinds("limit"))
    _coins(w, 8)
    w.signals["4h"] = [_sig(setup="S3", target=104_000.0, ts=T0 + H4), _short("S39", ts=T0 + H4)]
    res = await w.run(T0 + 2 * H4 + 60_000)
    new = [o[6] for o in w.venue.kinds("limit")[before:]]
    assert res["sent"] == 1 and new[0].startswith("td-S3-")

    async def broken(ctx):
        raise RuntimeError("db locked")

    monkeypatch.setattr(demo_trader, "_setup_slots", broken)
    await w.store.conn.execute("UPDATE demo_trade SET be_ts = ?, be_stop = 1.001 WHERE link_id IN ('d1', 'd2')", (T0 + 5,))
    await w.store.conn.commit()
    before = len(w.venue.kinds("limit"))
    _coins(w, 12)
    w.signals["4h"] = [_short("S39", ts=T0 + 2 * H4), _sig(setup="S3", target=104_000.0, ts=T0 + 2 * H4)]
    res = await w.run(T0 + 3 * H4 + 60_000)
    new = [o[6] for o in w.venue.kinds("limit")[before:]]
    assert not any(lid.startswith("td-S39-") for lid in new) and res["errors"] >= 1


async def test_s38_takes_at_most_two_shorts_per_4h_bar(world):
    """Owner 25.10: S38 ranks first on every coin here, yet only two of its shorts of one 4h bar enter; the other
    coins get their S3 signal, and the skipped S38 shorts say why."""
    w = world
    assert demo_trader.BAR_CAP == {"S38": 2}
    _coins(w, 5)
    w.signals["4h"] = [_short("S38"), _sig(setup="S3", target=104_000.0)]
    res = await w.run()
    sent = [o[6] for o in w.venue.kinds("limit")]
    assert res["sent"] == 5 and sorted(lid.split("-")[1] for lid in sent) == ["S3", "S3", "S3", "S38", "S38"]
    assert {lid.split("-")[4] for lid in sent if "-S38-" in lid} == {"C0", "C1"}  # the best two, then the cap
    skipped = {a["link_id"]: a["payload"]["violations"] for a in await w.store.exec_audit_list(kinds=("skipped",))}
    s38 = [v for lid, v in skipped.items() if "-S38-" in lid]
    assert len(s38) == 3 and all(any("2 S38 entries on this 4h bar = S38 cap 2 per bar" in x for x in v) for v in s38)
    assert not any("per bar" in x for lid, v in skipped.items() if "-S3-" in lid for x in v)  # other setups: no cap


async def test_s38_bar_cap_counts_the_book_and_fails_closed(world, monkeypatch):
    """Two S38 entries of a bar in the book close that bar for S38 (a break-even does not reopen it), the next bar is
    open again; an unreadable book keeps S38 out rather than uncapped, the other setups still trade."""
    w = world
    await _today(w.store, 0, setup="S38", side="short", signal_ts=T0 - H4, be_ts=T0 + 2, be_stop=0.999)
    await _today(w.store, 1, setup="S38", side="short", signal_ts=T0 - H4, status="closed", closed_ts=T0 + 3, pnl_usd=2.0)
    await _today(w.store, 2, setup="S38", side="short", signal_ts=T0 - H4, status="missed")  # never entered: not counted
    _coins(w, 2)
    w.signals["4h"] = [_short("S38")]
    res = await w.run()
    assert res["sent"] == 0 and res.get("skipped") == 2
    sk = await w.store.exec_audit_list(kinds=("skipped",))
    assert all(any("2 S38 entries on this 4h bar" in x for x in a["payload"]["violations"]) for a in sk)
    # the next 4h bar: a fresh cap of two
    _coins(w, 3)
    w.signals["4h"] = [_short("S38", ts=T0)]
    res = await w.run(T0 + H4 + 60_000)
    new = [o[6] for o in w.venue.kinds("limit")]
    assert res["sent"] == 2 and len(new) == 2 and all(lid.startswith("td-S38-s-4h-") for lid in new)

    async def broken(ctx):
        raise RuntimeError("db locked")

    monkeypatch.setattr(demo_trader, "_bar_entries", broken)
    before = len(w.venue.kinds("limit"))
    _coins(w, 4)
    w.signals["4h"] = [_short("S38", ts=T0 + H4), _sig(setup="S3", target=104_000.0, ts=T0 + H4)]
    res = await w.run(T0 + 2 * H4 + 60_000)
    new = [o[6] for o in w.venue.kinds("limit")[before:]]
    assert res["sent"] >= 1 and new and all(lid.startswith("td-S3-") for lid in new) and res["errors"] >= 1
    sk = [a for a in await w.store.exec_audit_list(kinds=("skipped",)) if a["link_id"].startswith("td-S38-") and "-C3-" in a["link_id"]]
    assert sk and any("book not read" in x for x in sk[0]["payload"]["violations"])


async def test_hard_ceiling_of_entries_per_day(world):
    w = world
    for k in range(10):
        await _today(w.store, k, be_ts=T0 + 2, be_stop=1.001)  # all protected, but ten entries already
    res = await w.run()
    assert res["sent"] == 0 and "10 entries today = max" in str(await w.store.exec_audit_list(kinds=("skipped",)))
