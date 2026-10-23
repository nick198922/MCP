"""Option leg of the demo trader (owner 23.10, slice 2): pure selection and steps on the real Bybit demo samples, the
paired report, the store table, and the trader's entry / exit / close flow against a fake venue (no network)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import ccxt
import pytest

from test_demo_trader import BTC, FakeVenue, _sig
from test_trade_plan import _call, _rt
from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.demo_option_pairs import MIN_PAIRS, paired_report
from tradedesk.execution import demo_trader
from tradedesk.execution import option_leg as ol
from tradedesk.execution.bybit_demo import _option_instrument, _option_quote
from tradedesk.execution.guard import DAY_MS, DEFAULT, option_fee, size_option
from tradedesk.server import build_server
from tradedesk.store import SCHEMA_VERSION, Store

FIXTURES = Path(__file__).parent / "fixtures"
CHAIN = json.loads((FIXTURES / "bybit_demo_options_btc_chain_2026-10-23.json").read_text())
SMALL = json.loads((FIXTURES / "bybit_demo_options_btc_2026-10-23.json").read_text())
NOW = CHAIN["fetched_ms"]  # 2026-10-23 16:32 UTC, the chain sample's local clock
SMALL_NOW = 1_792_771_111_675  # the small sample's server time, 15:58 UTC
H4 = ss.TF_MS["4h"]
NOV6, NOV13, DEC25 = 1_793_952_000_000, 1_794_556_800_000, 1_798_185_600_000
CALL40 = "BTC-13NOV26-84000-C-USDT"  # |delta| 0.397 on the chain sample


def _chain(sample=CHAIN):
    insts = [_option_instrument(x) for x in sample["instruments_info"]["result"]["list"]]
    quotes = {q.symbol: q for q in (_option_quote(t) for t in sample["tickers"]["result"]["list"])}
    return insts, quotes


# ---------------------------------------------------------------- pure: expiry, strike, delta


def test_expiry_is_the_nearest_with_twice_the_hold_and_never_under_the_floor():
    insts, _ = _chain()
    four_h, one_d = demo_trader.MAX_HOLD_MS["4h"], demo_trader.MAX_HOLD_MS["1d"]
    assert ol.min_dte_days(four_h, guard=DEFAULT) == 14.0 and ol.min_dte_days(one_d, guard=DEFAULT) == 60.0
    assert ol.pick_expiry(insts, NOW, four_h, guard=DEFAULT) == NOV13  # 6NOV26 is 13.6 days out: too near for 14
    assert ol.pick_expiry(insts, NOW, one_d, guard=DEFAULT) == DEC25  # 62.6 days
    assert ol.pick_expiry(insts, NOW, 0, guard=DEFAULT) == NOV6  # 7-day minimum; 24OCT26 (0.6 days) is out
    loose = ol.OptionLegConfig(min_dte_days=0.0)
    assert ol.pick_expiry(insts, NOW, 0, loose, DEFAULT) == NOV6  # the guard's 4-day floor still holds
    assert ol.pick_expiry(insts, DEC25 - 10 * DAY_MS, four_h, guard=DEFAULT) is None


def test_small_sample_has_no_expiry_far_enough_for_a_4h_signal():
    insts, quotes = _chain(SMALL)  # 24OCT26 0.7, 30OCT26 6.7, 6NOV26 13.7 days
    assert ol.pick_expiry(insts, SMALL_NOW, demo_trader.MAX_HOLD_MS["4h"], guard=DEFAULT) is None
    assert ol.pick_expiry(insts, SMALL_NOW, 0, guard=DEFAULT) == NOV6
    pick, why = ol.pick_strike(insts, quotes, NOV6, True, SMALL_NOW)
    assert pick is None and "no call with |delta| 0.3-0.5" in why  # only far OTM calls (delta 0.03) in this sample


def test_black76_delta_matches_the_venue_on_the_real_chain():
    insts, quotes = _chain()
    venue = {t["symbol"]: float(t["delta"]) for t in CHAIN["tickers"]["result"]["list"]}
    checked = 0
    for i in insts:
        d = ol.leg_delta(i, quotes[i.symbol], NOW)
        if d is not None and ol.expiry_of(i) > NOW + DAY_MS:
            assert d == pytest.approx(venue[i.symbol], abs=2e-3), i.symbol
            checked += 1
    assert checked > 40


def test_strike_closest_to_40_delta_for_calls_and_puts():
    insts, quotes = _chain()
    call, why = ol.pick_strike(insts, quotes, NOV13, True, NOW)
    assert why == "" and call.inst.symbol == CALL40 and call.delta == pytest.approx(0.397, abs=2e-3)
    put, _ = ol.pick_strike(insts, quotes, NOV13, False, NOW)
    assert put.inst.symbol == "BTC-13NOV26-80000-P-USDT" and put.delta == pytest.approx(-0.377, abs=2e-3)  # 0.023 vs 0.032 off
    dec, _ = ol.pick_strike(insts, quotes, DEC25, True, NOW)
    assert dec.inst.symbol == "BTC-25DEC26-86000-C-USDT"


def test_wide_book_falls_to_the_next_closest_strike_then_refuses():
    insts, quotes = _chain()
    wide = dict(quotes)
    wide[CALL40] = replace(quotes[CALL40], bid=1500.0, ask=1845.0)  # 20 % of mid > 10 %
    pick, _ = ol.pick_strike(insts, wide, NOV13, True, NOW)
    assert pick.inst.symbol == "BTC-13NOV26-83000-C-USDT"  # |0.453 - 0.40| = 0.053 beats |0.343 - 0.40| = 0.057
    for s, q in quotes.items():
        if "13NOV26" in s and s.endswith("-C-USDT"):
            wide[s] = replace(q, bid=0.0)
    pick, why = ol.pick_strike(insts, wide, NOV13, True, NOW)
    assert pick is None and "none with a spread <= 10%" in why


def test_link_ids_fit_bybit_and_differ_by_role():
    lid = demo_trader.link_id(_sig(), BTC)
    links = {ol.leg_link(lid, p) for p in ("tdo-", "tdx-", "tdg-", "tdb-")}
    assert len(links) == 4 and all(len(ol.nth_link(x, 9)) <= 36 for x in links)
    assert ol.nth_link("a", 0) == "a" and ol.nth_link("a", 2) == "a-2"


def test_entry_and_close_prices_stay_inside_the_book():
    assert ol.entry_price(1835.0, 1845.0, 5.0) == 1840.0  # a tick above the bid
    assert ol.entry_price(1840.0, 1845.0, 5.0) == 1840.0  # one-tick book: join the bid, never the ask
    assert ol.entry_price(0.0, 1845.0, 5.0) == 0.0
    assert ol.close_price(1840.0, 1850.0, 5.0) == 1845.0 and ol.close_price(1840.0, 1845.0, 5.0) == 1845.0
    assert ol.close_price(1840.0, 0.0, 5.0) == 0.0


# ---------------------------------------------------------------- pure: entry, exits, close


def _entry(**kw):
    base = dict(
        order_px=1840.0, order_live=True, held=0.0, plan_qty=0.05, bid=1840.0, ask=1845.0, tick=5.0, index=81_645.0,
        age_ms=60_000, risk_usd=100.0, lot_step=0.01, min_qty=0.01, halted=False,
    )  # fmt: skip
    return ol.entry_leg_step(**{**base, **kw})


def test_entry_steps():
    assert _entry().action == "wait"  # our order is the bid
    s = _entry(bid=1850.0, ask=1860.0)
    assert (s.action, s.price, s.qty) == ("requote", 1855.0, 0.05)
    s = _entry(bid=2000.0, ask=2010.0)  # dearer: the size shrinks for the same $100
    assert s.qty == size_option(2005.0, 81_645.0, 100.0, 0.01, 0.01) == 0.04
    assert _entry(held=0.05).action == "complete"
    assert _entry(halted=True).reason.startswith("kill-switch")
    assert _entry(age_ms=10 * 60_000).action == "cancel"
    assert _entry(order_live=False).action == "replace"  # the venue cancelled the post-only order
    assert _entry(order_live=False, held=0.02).action == "cancel"  # partly filled: keep what we have
    assert _entry(bid=0.0).action == "wait"


def _exit(**kw):
    base = dict(is_call=True, target=85_000.0, extreme=84_000.0, now_ms=NOW, time_exit_ms=NOW + DAY_MS, expiry_ms=NOV13)
    return ol.exit_reason(**{**base, **kw})


def test_exit_reasons():
    assert _exit() is None
    assert _exit(extreme=85_000.0) == "target"
    assert _exit(is_call=False, target=80_000.0, extreme=79_990.0) == "target"
    assert _exit(is_call=False, target=80_000.0, extreme=84_000.0) is None
    assert _exit(now_ms=NOW + DAY_MS) == "time"
    assert _exit(now_ms=NOV13 - DAY_MS, time_exit_ms=NOV13) == "expiry"  # a day before expiry at the latest
    assert _exit(now_ms=NOV13 - DAY_MS - 1, time_exit_ms=NOV13) is None


def test_underlying_extreme_reads_bars_since_the_fill():
    bars = [[NOW - 180_000, 1, 90_000, 70_000, 1, 1], [NOW - 60_000, 1, 84_500, 81_000, 1, 1], [NOW, 1, 84_900, 81_500, 1, 1]]
    assert ol.underlying_extreme(True, bars, NOW - 90_000, 84_000) == 84_900  # the old bar is before the fill
    assert ol.underlying_extreme(False, bars, NOW - 90_000, 81_200) == 81_000
    assert ol.underlying_extreme(True, [], NOW, 0.0) == 0.0


def _close(**kw):
    base = dict(phase="maker", order_px=0.0, order_live=False, order_rejected=False, bid=2900.0, ask=2920.0, tick=5.0, age_ms=0)
    return ol.close_leg_step(**{**base, **kw})


def test_close_steps():
    assert (_close().action, _close().price) == ("place", 2915.0)
    assert _close(order_px=2915.0, order_live=True).action == "wait"
    assert (_close(order_px=2915.0, order_live=True, ask=2910.0).action, _close(order_px=2915.0, order_live=True, ask=2910.0).price) == (
        "requote",
        2905.0,
    )
    assert _close(order_px=2915.0, order_rejected=True).action == "fallback"
    assert _close(order_px=2915.0, order_live=True, age_ms=5 * 60_000).action == "to_bid"
    assert _close(order_px=2915.0, order_live=False).action == "to_bid"  # the maker close left the book
    b = _close(phase="bid")
    assert (b.action, b.price) == ("place", 2900.0)
    assert _close(phase="bid", order_px=2900.0, order_live=True, bid=2890.0).price == 2890.0
    assert _close(phase="bid", bid=0.0).action == "wait"


def test_fills_total_counts_trades_only():
    rows = [
        {"execType": "Trade", "execQty": "0.03", "execPrice": "1840", "execFee": "0.3"},
        {"execType": "Trade", "execQty": "0.02", "execPrice": "1845", "execFee": "0.2"},
        {"execType": "Funding", "execQty": "1", "execPrice": "1", "execFee": "9"},
    ]
    q, v, f = ol.fills_total(rows)
    assert q == pytest.approx(0.05) and v == pytest.approx(0.03 * 1840 + 0.02 * 1845) and f == pytest.approx(0.5)


# ---------------------------------------------------------------- paired report


def _pair(k, rf, ro, f_reason="target", o_reason="target", f_closed=1, o_closed=2, status="closed"):
    f = {"link_id": f"f{k}", "status": "closed", "r_multiple": rf, "exit_reason": f_reason, "closed_ts": f_closed}
    leg = {"pair_id": f"f{k}", "status": status, "r_multiple": ro, "exit_reason": o_reason, "closed_ts": o_closed}
    return f, leg


def test_paired_report_small_sample():
    rows = [_pair(0, -1.0, 0.8, "stop", "target"), _pair(1, -1.0, -1.0, "stop", "time"), _pair(2, 2.0, 1.0)]
    rows.append(_pair(3, None, None, status="open"))
    legs = [leg for _, leg in rows] + [{"pair_id": "nope", "status": "closed", "r_multiple": 1.0}]
    rep = paired_report([f for f, _ in rows], legs)
    assert rep["n_pairs"] == 3 and rep["open_pairs"] == 1 and rep["legs_without_futures_row"] == 1
    assert rep["mean_r_futures"] == pytest.approx(0.0) and rep["mean_r_option"] == pytest.approx(0.8 / 3)
    d = rep["diff_option_minus_futures"]
    assert d["n"] == 3 and d["mean"] == pytest.approx(0.8 / 3) and d["lo"] < d["mean"] < d["hi"]
    assert rep["futures_stopped"] == 2 and rep["option_target_after_futures_stop"] == 1
    assert rep["share_option_survived_to_target"] == 0.5 and rep["verdict"].startswith("замало даних")


def test_paired_report_verdicts_from_30_pairs():
    better = [_pair(k, -1.0, 0.5 + 0.01 * k) for k in range(MIN_PAIRS)]
    rep = paired_report([f for f, _ in better], [leg for _, leg in better])
    assert rep["n_pairs"] == MIN_PAIRS and rep["verdict"].startswith("опціон краще")
    mixed = [_pair(k, 0.0, (-1.0) ** k) for k in range(MIN_PAIRS)]
    assert paired_report([f for f, _ in mixed], [leg for _, leg in mixed])["verdict"].startswith("різниці не видно")
    empty = paired_report([], [])
    assert empty["n_pairs"] == 0 and empty["mean_r_option"] is None and empty["share_option_survived_to_target"] is None


# ---------------------------------------------------------------- store


async def test_store_round_trip(tmp_path):
    s = Store(tmp_path / "s.sqlite")
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 28
        row = {
            "link_id": "tdo-x", "pair_id": "td-x", "venue": "bybit-demo", "symbol": CALL40, "underlying": BTC, "side": "long",
            "is_call": 1, "strike": 84_000.0, "expiry_ms": NOV13, "tick": 5.0, "lot_step": 0.01, "min_qty": 0.01, "setup": "S14",
            "timeframe": "4h", "signal_ts": 1, "target": 85_000.0, "comment": "c", "status": "pending", "placed_ts": 2,
            "entry_order": "tdo-x", "entry_px": 1840.0, "qty": 0.05, "risk_usd": 93.2,
        }  # fmt: skip
        await s.demo_option_insert(row)
        assert await s.demo_option_update("tdo-x", status="closed", pnl_usd=5.0, r_multiple=0.05, exit_links='["tdx-x"]') == 1
        (got,) = await s.demo_option_list(("closed",))
        assert got["pair_id"] == "td-x" and got["r_multiple"] == 0.05 and got["entry_n"] == 0 and got["exit_links"] == '["tdx-x"]'
        assert await s.demo_option_list(("open",)) == []
        with pytest.raises(ValueError, match="not updatable"):
            await s.demo_option_update("tdo-x", pair_id="other")
    finally:
        await s.aclose()


# ---------------------------------------------------------------- the trader with a fake venue


class OptionVenue(FakeVenue):
    """FakeVenue (futures) plus the option calls, on the real chain sample."""

    def __init__(self, now: int):
        super().__init__(now)
        self.last = {BTC: 81_616.0}
        self.book = {BTC: (81_610.0, 81_620.0)}
        insts, quotes = _chain()
        self.insts, self.oq = insts, quotes
        self.oorders: dict = {}
        self.opos: dict = {}
        self.execs: dict = {}
        self.ocalls: list = []
        self.m1: list = []
        self.inst_fail: BaseException | None = None
        self.buy_fail: BaseException | None = None
        self.buy_lands = False
        self.close_refuse_post_only: BaseException | None = None

    async def ohlcv(self, symbol, tf, limit=1000):
        return list(self.m1) if tf == "1m" else await super().ohlcv(symbol, tf, limit)

    async def option_instruments(self, base):
        if self.inst_fail is not None:
            raise self.inst_fail
        return list(self.insts) if base == "BTC" else []

    async def option_quotes(self, base):
        return dict(self.oq) if base == "BTC" else {}

    async def option_quote(self, symbol):
        return self.oq[symbol]

    def _order(self, inst, qty, price, link, side):
        self.oorders[link] = {"symbol": inst.symbol, "orderLinkId": link, "orderStatus": "New", "qty": qty, "price": price,
                              "cumExecQty": "0", "side": side}  # fmt: skip

    async def place_option_buy(self, inst, qty, price, link):
        self.ocalls.append(("buy", inst.symbol, qty, price, link))
        if self.buy_fail is not None:
            if self.buy_lands:
                self._order(inst, qty, price, link, "Buy")
            raise self.buy_fail
        self._order(inst, qty, price, link, "Buy")
        return {"retCode": 0}

    async def amend_option(self, inst, link, price, qty=None):
        self.ocalls.append(("amend", inst.symbol, link, price, qty))
        self.oorders[link]["price"] = price
        if qty is not None:
            self.oorders[link]["qty"] = qty
        return {}

    async def cancel_option(self, symbol, link):
        self.ocalls.append(("cancel", symbol, link))
        o = self.oorders.get(link)
        if not o or o["orderStatus"] not in ("New", "PartiallyFilled"):
            return False
        o["orderStatus"] = "Cancelled"
        return True

    async def option_order_by_link(self, symbol, link):
        return self.oorders.get(link)

    async def option_positions(self, bases=("BTC", "ETH")):
        return {s: dict(p) for s, p in self.opos.items()}

    async def option_executions(self, symbol, link):
        return list(self.execs.get(link, []))

    async def close_option(self, inst, qty, price, link, post_only=True):
        self.ocalls.append(("close", inst.symbol, qty, price, link, post_only))
        if post_only and self.close_refuse_post_only is not None:
            raise self.close_refuse_post_only
        self._order(inst, qty, price, link, "Sell")
        return {"retCode": 0}

    def fill_option(self, link, fee=0.5, qty=None):
        o = self.oorders[link]
        q = float(o["qty"]) - float(o["cumExecQty"]) if qty is None else qty
        o["cumExecQty"] = str(float(o["cumExecQty"]) + q)
        o["orderStatus"] = "Filled" if float(o["cumExecQty"]) >= float(o["qty"]) - 1e-12 else "PartiallyFilled"
        o["avgPrice"] = str(o["price"])
        sym, px = o["symbol"], float(o["price"])
        p = self.opos.get(sym, {"symbol": sym, "size": "0", "avgPrice": "0"})
        size = float(p["size"]) + (q if o["side"] == "Buy" else -q)
        if o["side"] == "Buy":
            p["avgPrice"] = str((float(p["size"]) * float(p["avgPrice"]) + q * px) / size)
        if size > 1e-12:
            self.opos[sym] = {**p, "size": str(round(size, 9))}
        else:
            self.opos.pop(sym, None)
        self.execs.setdefault(link, []).append({"execType": "Trade", "execQty": str(q), "execPrice": str(px), "execFee": str(fee)})

    def okinds(self, *names):
        return [c for c in self.ocalls if c[0] in names]


@pytest.fixture
async def ow(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_trader, "HALT_FILE", tmp_path / "demo.halt")
    monkeypatch.setattr(demo_trader, "OPTIONS_OFF_FILE", tmp_path / "demo-options.off")
    bar = NOW // H4 * H4 - H4  # the 4h bar that closed 32 minutes before the sample
    signals: dict = {"4h": [_sig(entry=81_616.0, stop=80_000.0, target=85_000.0, ts=bar)], "1d": []}
    monkeypatch.setattr(demo_trader, "scan", lambda bars, tf, setups: list(signals.get(tf, [])))
    monkeypatch.setattr(demo_trader, "score", lambda *a, **k: ss.Score(3, ("x",)))
    store = Store(tmp_path / "db.sqlite")
    await store.init()
    msgs: list[str] = []

    async def notify(t):
        msgs.append(t)

    w = SimpleNamespace(store=store, venue=OptionVenue(NOW), now=NOW, msgs=msgs, tmp=tmp_path, signals=signals)

    async def run(at=None, **kw):
        w.venue.now = at or NOW
        return await demo_trader.cycle(store, w.venue, at or NOW, notify, **kw)

    async def manage(at):
        w.venue.now = at
        return await demo_trader.manage_only(store, w.venue, at, notify)

    async def leg():
        return (await store.demo_option_list())[0]

    w.run, w.manage, w.leg = run, manage, leg
    yield w
    await store.aclose()


async def test_long_signal_buys_the_40_delta_call_beside_the_futures(ow):
    w = ow
    res = await w.run()
    assert res["sent"] == 1 and res.get("option_sent") == 1 and res["errors"] == 0, res
    ((_, sym, qty, px, link),) = w.venue.okinds("buy")
    index = w.venue.oq[CALL40].index
    assert (sym, px, qty) == (CALL40, 1840.0, size_option(1840.0, index, 100.0, 0.01, 0.01)) and qty == 0.05
    t = (await w.store.demo_trade_list())[0]
    leg = await w.leg()
    assert link == leg["link_id"] == ol.leg_link(t["link_id"]) and leg["pair_id"] == t["link_id"] and leg["status"] == "pending"
    assert leg["risk_usd"] == pytest.approx(0.05 * (1840 + option_fee(1840.0, index))) and leg["risk_usd"] <= 100
    assert leg["delta"] == pytest.approx(0.397, abs=3e-3) and leg["dte_days"] == pytest.approx(20.6, abs=0.1)
    assert "не доведена перевага" in leg["comment"] and "VRP" in leg["comment"]
    sent = await w.store.exec_audit_list(kinds=("sent",))
    assert len(sent) == 2  # the option order counts toward the orders per hour
    assert any("Демо-опціон" in m and "84000-C" in m and "VRP" in m for m in w.msgs)
    state = await demo_trader.shared_state(w.store, w.venue, NOW, {}, 10_000.0)
    assert CALL40 in state.open_symbols and state.open_risk_usd >= leg["risk_usd"] and state.trades_today == 1


async def test_futures_stop_out_leaves_the_option_which_closes_at_the_target(ow):
    w = ow
    await w.run()
    t = (await w.store.demo_trade_list())[0]
    lid = (await w.leg())["link_id"]
    w.venue.fill_limit(t["link_id"])
    w.venue.fill_option(lid, fee=0.55)
    await w.manage(NOW + 60_000)
    leg = await w.leg()
    assert leg["status"] == "open" and leg["fill"] == 1840.0 and leg["qty"] == 0.05
    assert leg["fee_open"] == pytest.approx(0.55) and leg["fee_open_bound"] == pytest.approx(
        0.05 * option_fee(1840.0, w.venue.oq[CALL40].index)
    )
    assert leg["risk_usd"] == pytest.approx(0.05 * 1840 + 0.55)
    j = await w.store.journal_get(leg["journal_id"])
    assert {"demo-paper", "bybit-demo", "setup:S14", "auto", "option"} <= set(j["tags"]) and j["symbol"] == CALL40
    # the futures trade is stopped out; the option is not touched
    t = (await w.store.demo_trade_list())[0]
    w.venue.pos.clear()
    w.venue.pnl_rows = [{"closedPnl": str(-t["risk_usd"]), "avgExitPrice": str(t["stop"]), "updatedTime": str(NOW + 600_000)}]
    w.signals["4h"] = []
    await w.run(NOW + 3_600_000)
    assert (await w.store.demo_trade_list())[0]["exit_reason"] == "stop"
    assert (await w.leg())["status"] == "open" and not w.venue.okinds("close")
    # later the underlying reaches the setup's target: the option is sold at the ask minus a tick, reduce-only post-only
    at = NOW + 2 * 3_600_000
    w.venue.m1 = [[at - 60_000, 84_000, 85_100, 83_900, 85_000, 1]]
    w.venue.oq[CALL40] = replace(w.venue.oq[CALL40], bid=2900.0, ask=2920.0)
    await w.manage(at)
    leg = await w.leg()
    ((_, sym, qty, px, xlink, post_only),) = w.venue.okinds("close")
    assert leg["status"] == "closing" and leg["exit_reason"] == "target" and (sym, qty, px, post_only) == (CALL40, 0.05, 2915.0, True)
    w.venue.fill_option(xlink, fee=0.6)
    await w.manage(at + 60_000)
    leg = await w.leg()
    pnl = 0.05 * 2915 - 0.05 * 1840 - 0.55 - 0.6
    assert leg["status"] == "closed" and leg["pnl_usd"] == pytest.approx(pnl) and leg["exit"] == 2915.0
    assert leg["r_multiple"] == pytest.approx(pnl / (0.05 * 1840 + 0.55)) and leg["fee_close"] == pytest.approx(0.6)
    assert (await w.store.journal_get(leg["journal_id"]))["outcome"] == "win"
    rep = paired_report(await w.store.demo_trade_list(), await w.store.demo_option_list())
    assert rep["n_pairs"] == 1 and rep["option_target_after_futures_stop"] == 1 and rep["share_option_survived_to_target"] == 1.0
    assert rep["verdict"].startswith("замало даних")


async def test_entry_requotes_then_is_missed_after_ten_minutes(ow):
    w = ow
    await w.run()
    lid = (await w.leg())["link_id"]
    w.venue.oq[CALL40] = replace(w.venue.oq[CALL40], bid=1850.0, ask=1860.0)  # outbid
    await w.manage(NOW + 60_000)
    assert w.venue.okinds("amend") == [("amend", CALL40, lid, 1855.0, 0.05)] and (await w.leg())["entry_px"] == 1855.0
    await w.manage(NOW + ol.OPTION_LEG.entry_wait_ms)
    leg = await w.leg()
    assert ("cancel", CALL40, lid) in w.venue.ocalls and leg["status"] == "missed" and "not filled in 10 min" in leg["note"]
    assert await w.store.exec_audit_list(kinds=("missed",)) and any("купівлю не виконано" in m for m in w.msgs)


async def test_post_only_cancelled_by_the_venue_is_placed_again(ow):
    w = ow
    await w.run()
    lid = (await w.leg())["link_id"]
    w.venue.oorders[lid]["orderStatus"] = "Cancelled"
    await w.manage(NOW + 60_000)
    assert w.venue.okinds("buy")[-1][4] == lid + "-1" and (await w.leg())["entry_order"] == lid + "-1"
    w.venue.fill_option(lid + "-1", fee=0.4)
    await w.manage(NOW + 120_000)
    leg = await w.leg()
    assert leg["status"] == "open" and leg["fee_open"] == pytest.approx(0.4)  # the fee is read from every entry order


async def test_refused_post_only_close_falls_back_to_gtc_then_sells_at_the_bid(ow):
    w = ow
    await w.run()
    lid = (await w.leg())["link_id"]
    w.venue.fill_option(lid)
    await w.manage(NOW + 60_000)
    w.venue.close_refuse_post_only = ccxt.InvalidOrder("bybit reduceOnly with PostOnly not supported")
    w.venue.m1 = [[NOW + 60_000, 1, 85_500, 84_000, 85_000, 1]]
    at = NOW + 120_000
    await w.manage(at)
    closes = w.venue.okinds("close")
    assert [c[5] for c in closes] == [True, False] and closes[1][4].startswith("tdg-")
    fb = await w.store.exec_audit_list(kinds=("option_close_fallback",))
    assert fb and "not supported" in fb[0]["payload"]["error"] and (await w.leg())["exit_kind"] == "gtc_fallback"
    w.venue.oq[CALL40] = replace(w.venue.oq[CALL40], bid=1830.0, ask=1850.0)
    await w.manage(at + ol.OPTION_LEG.exit_wait_ms)  # not filled in 5 minutes: sell at the bid
    assert ("cancel", CALL40, closes[1][4]) in w.venue.ocalls
    last = w.venue.okinds("close")[-1]
    assert last[4].startswith("tdb-") and last[3] == 1830.0 and last[5] is False and (await w.leg())["exit_phase"] == "bid"
    w.venue.fill_option(last[4], fee=0.5)
    await w.manage(at + ol.OPTION_LEG.exit_wait_ms + 60_000)
    leg = await w.leg()
    assert leg["status"] == "closed" and leg["exit"] == 1830.0 and leg["pnl_usd"] == pytest.approx(0.05 * (1830 - 1840) - 1.0)


async def test_time_exit_closes_the_option(ow):
    w = ow
    await w.run()
    w.venue.fill_option((await w.leg())["link_id"])
    await w.manage(NOW + 60_000)
    w.signals["4h"] = []
    await w.manage(NOW + 60_000 + demo_trader.MAX_HOLD_MS["4h"])
    leg = await w.leg()
    assert leg["status"] == "closing" and leg["exit_reason"] == "time" and w.venue.okinds("close")


async def test_kill_switch_cancels_a_resting_entry_but_closes_still_run(ow):
    w = ow
    await w.run()
    lid = (await w.leg())["link_id"]
    (w.tmp / "demo.halt").write_text("stop")
    await w.manage(NOW + 60_000)
    leg = await w.leg()
    assert leg["status"] == "missed" and "kill-switch" in leg["note"]
    # an open leg is still closed under the kill-switch
    await w.store.demo_option_update(lid, status="open", opened_ts=NOW, fill=1840.0, qty=0.05)
    w.venue.opos[CALL40] = {"symbol": CALL40, "size": "0.05", "avgPrice": "1840"}
    w.venue.m1 = [[NOW + 60_000, 1, 85_500, 84_000, 85_000, 1]]
    await w.manage(NOW + 120_000)
    assert (await w.leg())["status"] == "closing" and w.venue.okinds("close")


async def test_leg_shares_the_signals_daily_slot(ow):
    w = ow
    for k in range(4):  # four trades at risk today: the futures entry takes the fifth slot, the option no extra one
        await w.store.demo_trade_insert(
            {
                "link_id": f"d{k}", "venue": "bybit-demo", "symbol": f"X{k}/USDT:USDT", "side": "long", "setup": "S3",
                "timeframe": "4h", "signal_ts": 1, "entry": 1.0, "stop": 0.99, "target": 1.05, "qty": 1.0, "leverage": 5,
                "risk_usd": 1.0, "comment": "-", "status": "open", "opened_ts": NOW // DAY_MS * DAY_MS + 1,
            }
        )  # fmt: skip
    res = await w.run()
    assert res["sent"] == 1 and res.get("option_sent") == 1, res


async def test_orders_per_hour_cap_refuses_the_leg(ow):
    w = ow
    for _ in range(4):
        await w.store.exec_audit_add(NOW - 60_000, demo_trader.VENUE, "sent", link_id="td-x")
    res = await w.run()
    assert res["sent"] == 1 and not w.venue.okinds("buy") and res.get("option_skipped") == 1
    skipped = await w.store.exec_audit_list(kinds=("option_skipped",))
    assert "5 orders in the last hour = max" in str(skipped[0]["payload"]["why"])


async def test_switch_off_file_skips_new_legs(ow):
    w = ow
    (w.tmp / "demo-options.off").write_text("off")
    res = await w.run()
    assert res["sent"] == 1 and not w.venue.okinds("buy") and await w.store.demo_option_list() == []
    assert "switched off" in (await w.store.exec_audit_list(kinds=("option_skipped",)))[0]["payload"]["why"]


async def test_dry_run_plans_the_leg_and_sends_nothing(ow):
    w = ow
    res = await w.run(dry_run=True)
    assert res.get("would_send_option") == 1 and not w.venue.okinds("buy")
    (plan,) = await w.store.exec_audit_list(kinds=("dry_option_plan",))
    assert plan["symbol"] == CALL40 and plan["payload"]["qty"] == 0.05


async def test_option_venue_failure_keeps_the_futures_entry(ow):
    w = ow
    w.venue.inst_fail = ccxt.NetworkError("option instruments down")
    res = await w.run()
    assert res["sent"] == 1 and res["errors"] == 1 and not w.venue.okinds("buy") and await w.store.demo_option_list() == []
    err = await w.store.exec_audit_list(kinds=("error",))
    assert err[0]["payload"]["step"] == "option_leg"


async def test_lost_option_answer_is_recovered_by_its_link_id(ow):
    w = ow
    w.venue.buy_fail, w.venue.buy_lands = ccxt.RequestTimeout("timeout"), True
    res = await w.run()
    assert res["sent"] == 1 and (await w.leg())["status"] == "unknown"
    w.venue.buy_fail = None
    await w.manage(NOW + 60_000)
    assert (await w.leg())["status"] == "pending"


async def test_option_refusal_is_recorded_without_secrets(ow):
    w = ow
    w.venue.buy_fail = ccxt.InvalidOrder("bybit SECRET123KEY refused")
    res = await w.run()
    assert res.get("option_rejected") == 1 and (await w.leg())["status"] == "rejected"
    assert "SECRET123KEY" not in str(await w.store.exec_audit_list(kinds=("rejected",)))


async def test_position_gone_without_our_close_is_flagged(ow):
    w = ow
    await w.run()
    lid = (await w.leg())["link_id"]
    w.venue.fill_option(lid)
    await w.manage(NOW + 60_000)
    w.venue.opos.clear()  # expired, delivered or closed by hand
    await w.manage(NOW + 120_000)
    leg = await w.leg()
    assert leg["status"] == "closed" and leg["exit_reason"] == "gone" and leg["pnl_usd"] is None
    assert any("P&L не записано" in m for m in w.msgs)


async def test_plan_review_shows_the_paired_comparison(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        rv = await _call(srv, "plan_review")
        pairs = rv["data"]["demo_option_pairs"]
        assert pairs["n_pairs"] == 0 and pairs["open_pairs"] == 0 and pairs["verdict"].startswith("замало даних")
        assert "not a claimed edge" in (await _tool_description(srv, "plan_review"))
    finally:
        await rt.aclose()


async def _tool_description(server, name: str) -> str:
    tools = await server.list_tools()
    return next(t.description for t in tools if t.name == name)
