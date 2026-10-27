"""Option path of the demo trader (slice 1): bybit_demo option calls on the real Bybit demo sample and
scripts/demo_option_probe.py through the guard, against a fake ccxt exchange (no network, no order leaves the test)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import ccxt
import pytest

from tradedesk.execution import demo_trader
from tradedesk.execution.bybit_demo import BybitDemo, DemoOnlyError, OptionInstrument
from tradedesk.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import demo_option_probe as probe  # noqa: E402

FIX = json.loads((Path(__file__).parent / "fixtures" / "bybit_demo_options_btc_2026-10-23.json").read_text())
NOW = 1_792_771_111_675  # the sample's server time, 2026-10-23 15:58 UTC
OPT = "BTC-6NOV26-96000-C-USDT"
DEMO_URLS = {"api": {"public": "https://api-demo.{hostname}", "private": "https://api-demo.{hostname}"}}


class FakeEx:
    """Just enough of ccxt's bybit for the option calls: the sample for public data, an order book of our own orders."""

    def __init__(self, split_pages: bool = False):
        self.options = {"enableDemoTrading": True}
        self.hostname = "bybit.com"
        self.urls = DEMO_URLS
        self.calls: list[tuple[str, dict]] = []
        self.open: dict[str, dict] = {}
        self.done: dict[str, dict] = {}
        self.split_pages = split_pages
        self.create_error: BaseException | None = None
        self.create_lands = False  # with create_error: the order reached the venue anyway (lost answer)
        self.realtime_error: BaseException | None = None
        self.held = "BTC-6NOV26-80000-P-USDT"  # the option position the account holds

    async def close(self):
        pass

    def _rec(self, name: str, req: dict) -> None:
        self.calls.append((name, dict(req)))

    async def publicGetV5MarketInstrumentsInfo(self, req):
        self._rec("instruments", req)
        rows = FIX["instruments_info"]["result"]["list"]
        if not self.split_pages:
            return FIX["instruments_info"]
        if req.get("cursor") == "0,3":
            return {"result": {"list": rows[3:], "nextPageCursor": ""}}
        return {"result": {"list": rows[:3], "nextPageCursor": "0%2C3"}}

    async def publicGetV5MarketTickers(self, req):
        self._rec("tickers", req)
        rows = FIX["tickers"]["result"]["list"]
        if "symbol" in req:
            rows = [r for r in rows if r["symbol"] == req["symbol"]]
        return {"result": {"list": rows}}

    async def privatePostV5OrderCreate(self, req):
        self._rec("create", req)
        if self.create_error is not None:
            if self.create_lands:
                self.open[req["orderLinkId"]] = {"symbol": req["symbol"], "orderLinkId": req["orderLinkId"], "orderStatus": "New"}
            raise self.create_error
        self.open[req["orderLinkId"]] = {"symbol": req["symbol"], "orderLinkId": req["orderLinkId"], "orderStatus": "New"}
        return {"retCode": 0, "result": {"orderId": "o-1", "orderLinkId": req["orderLinkId"]}}

    async def privatePostV5OrderAmend(self, req):
        self._rec("amend", req)
        return {"retCode": 0, "result": {"orderLinkId": req["orderLinkId"]}}

    async def privatePostV5OrderCancel(self, req):
        self._rec("cancel", req)
        row = self.open.pop(req["orderLinkId"], None)
        if row is None:
            raise ccxt.OrderNotFound('bybit {"retCode":110001,"retMsg":"Order does not exist."}')
        self.done[req["orderLinkId"]] = {**row, "orderStatus": "Cancelled"}
        return {"retCode": 0, "result": {"orderLinkId": req["orderLinkId"]}}

    async def privateGetV5OrderRealtime(self, req):
        self._rec("realtime", req)
        if self.realtime_error is not None:
            raise self.realtime_error
        rows = [r for k, r in self.open.items() if req.get("orderLinkId") in (None, k)]
        return {"result": {"list": rows, "nextPageCursor": ""}}

    async def privateGetV5OrderHistory(self, req):
        self._rec("history", req)
        rows = [r for k, r in self.done.items() if req.get("orderLinkId") in (None, k)]
        return {"result": {"list": rows}}

    async def privateGetV5PositionList(self, req):
        self._rec("positions", req)
        rows = [{"symbol": self.held, "size": "0.5", "side": "Buy"}, {"symbol": "BTC-6NOV26-90000-C-USDT", "size": "0"}]
        return {"result": {"list": rows if req.get("baseCoin") == "BTC" else []}}

    async def privateGetV5AccountWalletBalance(self, req):
        return {"result": {"list": [{"totalEquity": "10000"}]}}

    async def fetch_positions(self, symbols, params):
        return []


def _client(ex: FakeEx | None = None) -> tuple[BybitDemo, FakeEx]:
    ex = ex or FakeEx()
    return BybitDemo("KEY123456", "SECRET123456", factory=lambda k, s: ex), ex


def _inst(symbol: str = OPT) -> OptionInstrument:
    return OptionInstrument(symbol, "BTC", "USDT", True, 96_000.0, 1_793_952_000_000, 5.0, 0.01, 0.01)


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    await s.init()
    yield s
    await s.aclose()


# ---------------------------------------------------------------- client


async def test_instruments_and_quotes_from_the_real_sample():
    c, ex = _client()
    insts = {i.symbol: i for i in await c.option_instruments("BTC")}
    assert len(insts) == 6 and insts[OPT] == _inst()
    assert not insts["BTC-6NOV26-80000-P-USDT"].is_call
    q = (await c.option_quotes("BTC"))[OPT]
    assert (q.bid, q.ask, q.mark_iv) == (65.0, 70.0, pytest.approx(float(FIX["tickers"]["result"]["list"][1]["markIv"])))
    assert q.index == pytest.approx(81_704.14346865) and q.bid_iv > 0 and q.ask_iv > q.bid_iv
    assert (await c.option_quotes("BTC"))["BTC-30OCT26-106000-C-USDT"].bid == 0.0  # empty side reads 0
    one = await c.option_quote(OPT)
    assert one == q
    assert all(req["category"] == "option" for _, req in ex.calls)
    with pytest.raises(ValueError, match="no ticker"):
        await c.option_quote("BTC-6NOV26-1-C-USDT")


async def test_instrument_pages_follow_the_cursor_unquoted():
    c, ex = _client(FakeEx(split_pages=True))
    assert len(await c.option_instruments("BTC")) == 6
    assert [req.get("cursor") for name, req in ex.calls if name == "instruments"] == [None, "0,3"]


async def test_buy_is_a_post_only_limit_on_tick_and_lot():
    c, ex = _client()
    await c.place_option_buy(_inst(), 0.019, 32.7, "tdo-x")
    name, req = ex.calls[-1]
    assert name == "create" and req == {
        "category": "option",
        "symbol": OPT,
        "side": "Buy",
        "orderType": "Limit",
        "qty": "0.01",  # floored to the lot step
        "price": "30",  # floored to the tick: never pays more than planned
        "timeInForce": "PostOnly",
        "orderLinkId": "tdo-x",
    }
    with pytest.raises(ValueError, match="minimum"):
        await c.place_option_buy(_inst(), 0.009, 30.0, "tdo-y")
    assert len([n for n, _ in ex.calls if n == "create"]) == 1


async def test_close_is_a_reduce_only_limit_sell():
    c, ex = _client()
    await c.close_option(_inst(), 0.5, 31.0, "tdo-c")
    req = ex.calls[-1][1]
    assert (req["side"], req["reduceOnly"], req["timeInForce"], req["price"], req["qty"]) == ("Sell", True, "PostOnly", "35", "0.5")
    await c.close_option(_inst(), 0.5, 31.0, "tdo-c2", post_only=False)
    assert ex.calls[-1][1]["timeInForce"] == "GTC"


async def test_amend_cancel_lookup_and_positions_by_link_id():
    c, ex = _client()
    await c.place_option_buy(_inst(), 0.01, 30.0, "tdo-a")
    await c.amend_option(_inst(), "tdo-a", 26.0, 0.02)
    assert ex.calls[-1] == ("amend", {"category": "option", "symbol": OPT, "orderLinkId": "tdo-a", "price": "25", "qty": "0.02"})
    assert (await c.option_order_by_link(OPT, "tdo-a"))["orderStatus"] == "New"
    assert [o["orderLinkId"] for o in await c.option_open_orders("BTC")] == ["tdo-a"]
    assert await c.cancel_option(OPT, "tdo-a") is True
    assert await c.cancel_option(OPT, "tdo-a") is False  # 110001: no longer open
    assert (await c.option_order_by_link(OPT, "tdo-a"))["orderStatus"] == "Cancelled"
    assert await c.option_order_by_link(OPT, "never") is None
    assert list(await c.option_positions()) == [ex.held]  # zero-size rows left out


@pytest.mark.parametrize(
    "ex",
    [
        SimpleNamespace(options={"enableDemoTrading": True}, hostname="bybit.com", urls={"api": {"public": "https://api.bybit.com"}}),
        SimpleNamespace(options={"enableDemoTrading": False}, hostname="bybit.com", urls=DEMO_URLS),
        SimpleNamespace(options={"enableDemoTrading": True}, hostname="evil.com", urls=DEMO_URLS),
    ],
)
async def test_every_option_call_refuses_off_the_demo_host(ex):
    c = BybitDemo("k", "s", factory=lambda k, s: ex)
    calls = (
        c.option_instruments("BTC"),
        c.option_quotes("BTC"),
        c.option_quote(OPT),
        c.place_option_buy(_inst(), 0.01, 30.0, "x"),
        c.amend_option(_inst(), "x", 30.0),
        c.cancel_option(OPT, "x"),
        c.option_open_orders("BTC"),
        c.option_order_by_link(OPT, "x"),
        c.option_positions(),
        c.close_option(_inst(), 0.01, 30.0, "x"),
    )
    for call in calls:
        with pytest.raises(DemoOnlyError):
            await call


# ---------------------------------------------------------------- probe


def test_probe_picks_a_far_otm_call_past_the_dte_floor():
    from tradedesk.execution.bybit_demo import _option_instrument, _option_quote  # noqa: PLC0415

    insts = [_option_instrument(x) for x in FIX["instruments_info"]["result"]["list"]]
    quotes = {q.symbol: q for q in (_option_quote(t) for t in FIX["tickers"]["result"]["list"])}
    inst, q = probe.pick_far_otm_call(insts, quotes, NOW)
    # 30OCT26 90000-C: spread 11.8 % > 10 %; 106000-C: no bid; 6NOV26 110000-C: spread 100 %; 24OCT26: 0.7 days
    assert inst.symbol == OPT and q.bid == 65.0
    assert probe.probe_price(q.bid, inst.tick) == 30.0 and probe.probe_price(7.0, 5.0) == 5.0
    assert probe.pick_far_otm_call(insts, quotes, 1_793_952_000_000 - 4 * 86_400_000) is None  # 4 days left < 4 + 1


async def test_probe_dry_run_prints_the_plan_and_sends_nothing(store):
    c, ex = _client()
    code, rep = await probe.run(c, store, NOW, place=False)
    assert code == 0 and rep["violations"] == [] and "dry run" in rep["result"]
    plan = rep["plan"]
    assert (plan["symbol"], plan["side"], plan["price"], plan["qty"]) == (OPT, "buy", 30.0, 0.01)
    assert plan["dte_days"] == pytest.approx(13.67, abs=0.01) and plan["risk_usd"] < 1.0
    assert not [n for n, _ in ex.calls if n in ("create", "cancel")]
    assert await store.exec_audit_list() == []


async def test_probe_places_sees_and_cancels_one_order(store):
    c, ex = _client()
    code, rep = await probe.run(c, store, NOW, place=True)
    assert code == 0, rep
    assert rep["status_after_place"] == "New" and rep["in_open_orders"] and rep["cancelled"] and rep["status_after_cancel"] == "Cancelled"
    creates = [req for n, req in ex.calls if n == "create"]
    assert len(creates) == 1 and creates[0]["side"] == "Buy" and creates[0]["timeInForce"] == "PostOnly" and ex.open == {}
    kinds = [a["kind"] for a in reversed(await store.exec_audit_list())]
    assert kinds == ["sent", "cancelled"]


async def test_probe_shares_the_futures_caps(store):
    for _ in range(demo_trader.DEFAULT.orders_per_hour):  # the hour's orders all spent by the futures trader
        await store.exec_audit_add(NOW - 60_000, demo_trader.VENUE, "sent", link_id="td-x")
    c, ex = _client()
    code, rep = await probe.run(c, store, NOW, place=True)
    assert code == 1 and any("orders in the last hour" in v for v in rep["violations"])
    assert not [n for n, _ in ex.calls if n == "create"]
    assert (await store.exec_audit_list(kinds=("refused",)))[0]["link_id"] == probe.probe_link_id(NOW)


async def test_probe_refuses_on_the_kill_switch_and_an_open_position(store, tmp_path, monkeypatch):
    halt = tmp_path / "demo.halt"
    halt.write_text("stop")
    monkeypatch.setattr(demo_trader, "HALT_FILE", halt)
    c, ex = _client()
    ex.held = OPT  # the account already holds the call the probe picks
    code, rep = await probe.run(c, store, NOW, place=False)
    assert code == 1 and any("kill-switch" in v for v in rep["violations"])
    assert any("already in" in v for v in rep["violations"])
    assert not [n for n, _ in ex.calls if n == "create"]


async def test_probe_without_a_candidate_does_nothing(store):
    c, ex = _client()
    code, rep = await probe.run(c, store, 1_793_952_000_000, place=True)  # at the last expiry: nothing has 5 days left
    assert code == 1 and "no far-OTM" in rep["result"] and not [n for n, _ in ex.calls if n == "create"]


async def test_probe_lost_answer_still_finds_and_cancels_the_order(store):
    c, ex = _client()
    ex.create_error, ex.create_lands = ccxt.RequestTimeout("timeout"), True
    code, rep = await probe.run(c, store, NOW, place=True, sleep=_no_sleep)
    assert code == 0 and "order_error" in rep and rep["cancelled"] and ex.open == {}


async def test_probe_venue_refusal_is_reported_without_secrets(store):
    c, ex = _client()
    ex.create_error = ccxt.InvalidOrder("bybit KEY123456 rejected")
    code, rep = await probe.run(c, store, NOW, place=True, sleep=_no_sleep)
    assert code == 2 and "KEY123456" not in rep["order_error"] and "cancel" not in [n for n, _ in ex.calls]
    assert [a["kind"] for a in reversed(await store.exec_audit_list())] == ["sent", "rejected"]


async def test_probe_read_failure_after_the_order_still_cancels(store):
    c, ex = _client()
    ex.realtime_error = ccxt.NetworkError("down")
    with pytest.raises(ccxt.NetworkError):
        await probe.run(c, store, NOW, place=True, sleep=_no_sleep)
    assert ex.open == {} and [n for n, _ in ex.calls].count("cancel") == 1


async def _no_sleep(_: float) -> None:
    return None
