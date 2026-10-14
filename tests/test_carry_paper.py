from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import ccxt
import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics.carry import DAY_MS, _Leg, pair_paper_mark, paper_summary, simulate_perp_spread_portfolio
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server
from tradedesk.store import SCHEMA_VERSION, Store

H4 = 4 * 3_600_000
H8 = 8 * 3_600_000


def _bars(price=100.0, start=-3 * H4, n=60, high=None, low=None):
    return [{"ts": start + i * H4, "close": price, "high": high or price, "low": low or price} for i in range(n)]


def _mark(**kw):
    base = dict(
        opened_ms=0,
        now_ms=3 * H8,
        notional_usd=1000.0,
        long_entry=100.0,
        short_entry=100.0,
        long_funding=[],
        short_funding=[],
        long_bars=_bars(),
        short_bars=_bars(),
        long_now=100.0,
        short_now=100.0,
        spread_apr=0.5,
        fee_long=0.0005,
        fee_short=0.00055,
        slippage=0.0005,
        stop_move=0.5,
    )
    base.update(kw)
    return pair_paper_mark(**base)


# ---------- pure analytics ----------


def test_funding_signs_long_pays_short_receives():
    # qty 10, price 100: long pays 0.0001 * 1000 = 0.1, short receives 0.0004 * 1000 = 0.4
    m = _mark(long_funding=[{"ts_ms": H8, "rate": 0.0001}], short_funding=[{"ts_ms": H8, "rate": 0.0004}])
    assert m["funding_pnl"] == pytest.approx(0.3)
    # negative rates flip it: the long leg receives 0.2, the short leg pays 0.1
    m = _mark(long_funding=[{"ts_ms": H8, "rate": -0.0002}], short_funding=[{"ts_ms": H8, "rate": -0.0001}])
    assert m["funding_pnl"] == pytest.approx(0.1)


def test_only_payments_after_open_and_up_to_now_count():
    rows = [{"ts_ms": ts, "rate": 0.001} for ts in (-H8, 0, H8, 2 * H8, 3 * H8, 3 * H8 + 1)]
    m = _mark(short_funding=rows)
    assert m["n_funding_payments"] == 3  # H8, 2*H8 and 3*H8 (== now) only
    assert m["funding_pnl"] == pytest.approx(3 * 0.001 * 10 * 100)


def test_funding_uses_leg_price_at_payment_and_entry_fallback():
    # Bars close at 110 from ts 0 on (bar closing at 4h, 8h, ...); the payment at 8h is priced at 110.
    bars = [{"ts": -H4, "close": 100.0, "high": 100.0, "low": 100.0}] + [
        {"ts": i * H4, "close": 110.0, "high": 110.0, "low": 110.0} for i in range(10)
    ]
    m = _mark(short_funding=[{"ts_ms": H8, "rate": 0.001}], short_bars=bars, short_now=110.0)
    assert m["funding_pnl"] == pytest.approx(0.001 * 10 * 110.0)
    # no bar close within 4h before the payment: entry price
    m = _mark(short_funding=[{"ts_ms": H8, "rate": 0.001}], short_bars=[])
    assert m["funding_pnl"] == pytest.approx(0.001 * 10 * 100.0)


def test_price_pnl_and_costs_with_stop_penalty():
    m = _mark(long_now=103.0, short_now=101.0)
    assert m["price_pnl"] == pytest.approx(10 * (3.0 - 1.0))
    side = (0.0005 + 0.00055 + 0.001) * 1000
    assert m["costs"] == pytest.approx(side + side * 1.03)
    assert m["return_on_notional"] == pytest.approx(m["pnl_usd"] / 1000) and not m["stopped"]
    # a stop always adds the 1 % penalty; prices are the bars' closes at the stop (100), not the current 103 / 101
    stopped = _mark(long_now=103.0, short_now=101.0, short_bars=_bars(high=160.0))
    assert stopped["stopped"] and stopped["price_pnl"] == 0.0
    assert stopped["costs"] == pytest.approx(2 * side + 10.0)


def test_stop_freezes_funding_price_and_costs_at_the_stop_bar():
    # payments every 8h at 0.1 %; the short leg spikes +60 % inside the bar opening at 8h (closes at 12h)
    rows = [{"ts_ms": i * H8, "rate": 0.001} for i in range(1, 8)]
    sbars = _bars()
    sbars[3 + 2]["high"] = 160.0  # bar ts = 2 * H4
    for b in sbars[3 + 3 :]:
        b["close"] = 120.0  # price keeps rising after the stop; must not count
    m = _mark(now_ms=7 * H8, short_funding=rows, short_bars=sbars, short_now=120.0)
    assert m["stopped"] and m["exit_signal"] == "stop" and m["stop_ts_ms"] == 3 * H4
    assert m["n_funding_payments"] == 1  # only the payment at 8h is <= 12h
    assert m["funding_pnl"] == pytest.approx(0.001 * 10 * 100.0)
    assert m["price_pnl"] == 0.0 and m["held_days"] == pytest.approx(3 * H4 / DAY_MS)
    side = (0.0005 + 0.00055 + 0.001) * 1000
    assert m["costs"] == pytest.approx(2 * side + 10.0)
    # the same data without a stop would keep collecting and book the later price move
    free = _mark(now_ms=7 * H8, short_funding=rows, short_bars=sbars, short_now=120.0, stop_move=None)
    assert free["n_funding_payments"] == 7 and free["price_pnl"] == pytest.approx(-200.0) and not free["stopped"]
    # a stop caught only by the current price closes at now
    cur = _mark(short_now=160.0)
    assert cur["stopped"] and cur["stop_ts_ms"] == 3 * H8 and cur["price_pnl"] == pytest.approx(-600.0)


def test_stop_and_exit_signals():
    # +60 % on the short leg's bar high vs stop 50 %
    m = _mark(short_bars=_bars(high=160.0))
    assert m["max_up"] == pytest.approx(0.6) and m["exit_signal"] == "stop"
    # long leg 55 % lower
    m = _mark(long_bars=_bars(low=45.0))
    assert m["max_down"] == pytest.approx(0.55) and m["exit_signal"] == "stop"
    # as in the backtest's mark(): max_up takes the highs of BOTH legs, so a spike up on the long leg's venue also stops
    m = _mark(long_bars=_bars(high=300.0))
    assert m["max_up"] == pytest.approx(2.0) and m["exit_signal"] == "stop"
    m = _mark(short_bars=_bars(low=40.0))
    assert m["max_down"] == pytest.approx(0.6) and m["exit_signal"] == "stop"
    # bars that closed before the entry are ignored; the bar containing the entry counts (conservative)
    m = _mark(opened_ms=H4, short_bars=_bars(start=-3 * H4, n=1, high=300.0) + _bars(start=H4, n=3))
    assert m["max_up"] == 0.0
    m = _mark(opened_ms=H4 // 2, short_bars=_bars(start=0, n=3, high=300.0))
    assert m["max_up"] == pytest.approx(2.0)
    # exit by spread, stop wins over it
    assert _mark(spread_apr=0.0)["exit_signal"] == "spread_exit"
    assert _mark(spread_apr=-0.1, exit_apr=-0.2)["exit_signal"] is None
    assert _mark(spread_apr=None)["exit_signal"] is None
    assert _mark(spread_apr=-1.0, short_bars=_bars(high=160.0))["exit_signal"] == "stop"
    # no stop configured
    assert _mark(stop_move=None, short_bars=_bars(high=160.0))["exit_signal"] is None


def test_held_days_and_realized_apr():
    m = _mark(now_ms=2 * DAY_MS, short_funding=[{"ts_ms": DAY_MS, "rate": 0.01}])
    assert m["held_days"] == pytest.approx(2.0)
    assert m["realized_apr"] == pytest.approx(m["return_on_notional"] * 365 / 2)
    assert _mark(now_ms=0)["realized_apr"] is None


def test_matches_backtest_accounting():
    """One trade of simulate_perp_spread_portfolio and pair_paper_mark give the same fractions."""

    def leg(rates, slope):
        funding = [{"ts_ms": i * H8, "rate": r} for i, r in enumerate(rates)]
        bars = [{"ts": i * H4, "close": 100.0 + slope * i, "high": 100.0 + slope * i, "low": 100.0 + slope * i} for i in range(62)]
        return {"funding": funding, "bars": bars}

    a, b = leg([0.0001] * 30, 0.1), leg([0.0006] * 30, 0.2)  # long A, short B; both drift up, B faster
    kw = dict(fee_a=0.0005, fee_b=0.00055, slippage=0.0005)
    res = simulate_perp_spread_portfolio({"X": {"a": a, "b": b}}, slots=1, lookback_days=1, entry_apr=0.1, **kw)
    (t,) = res["trades"]
    assert t["dir"] == 1 and not t["stopped"]
    la, lb = _Leg(a, H4), _Leg(b, H4)
    m = pair_paper_mark(
        opened_ms=t["open_ts"],
        now_ms=t["close_ts"],
        notional_usd=1000.0,
        long_entry=la.price(t["open_ts"]),
        short_entry=lb.price(t["open_ts"]),
        long_funding=a["funding"],
        short_funding=b["funding"],
        # the backtest only sees bars closed by the step; live marking also sees the bar in progress (a conservative extra)
        long_bars=[x for x in a["bars"] if x["ts"] + H4 <= t["close_ts"] + 60_000],
        short_bars=[x for x in b["bars"] if x["ts"] + H4 <= t["close_ts"] + 60_000],
        long_now=la.price(t["close_ts"]),
        short_now=lb.price(t["close_ts"]),
        spread_apr=1.0,
        fee_long=kw["fee_a"],
        fee_short=kw["fee_b"],
        slippage=kw["slippage"],
    )
    assert m["funding_pnl"] / 1000 == pytest.approx(t["funding"])
    assert m["price_pnl"] / 1000 == pytest.approx(t["price_pnl"])
    assert m["costs"] / 1000 == pytest.approx(t["fees"])
    assert m["return_on_notional"] == pytest.approx(t["pnl"])
    assert m["max_up"] == pytest.approx(t["max_up"])
    assert t["price_pnl"] != 0 and t["funding"] > 0


def test_paper_summary():
    rows = [
        {
            "status": "closed",
            "return_on_notional": r,
            "opened_ts": 0,
            "closed_ts": 86400 * d,
            "stopped": s,
            "funding_pnl": 1,
            "price_pnl": -1,
            "costs": 2,
            "pnl_usd": 10 * r,
            "entry_tradable": r > 0,
        }
        for r, d, s in ((-0.02, 2, True), (0.01, 4, False), (0.05, 6, False), (-0.01, 8, False))
    ] + [{"status": "open"}]
    s = paper_summary(rows)
    assert s["n"] == 4
    assert s["mean_return"] == pytest.approx(0.0075)
    assert s["median_return"] == pytest.approx(0.0)  # (-0.01 + 0.01) / 2
    assert s["share_losing"] == 0.5 and s["share_stopped"] == 0.25
    assert s["mean_held_days"] == pytest.approx(5.0)
    assert s["sum_funding_usd"] == 4 and s["sum_costs_usd"] == 8
    sub = s["entry_tradable"]  # only the two winners met the entry rule
    assert sub["n"] == 2 and sub["mean_return"] == pytest.approx(0.03) and sub["share_losing"] == 0.0 and sub["share_stopped"] == 0.0
    assert paper_summary([]) == {"n": 0, "entry_tradable": {"n": 0}}


# ---------- store ----------


async def test_store_migration_and_roundtrip():
    s = Store(":memory:")
    await s.init()
    try:
        assert SCHEMA_VERSION >= 9 and await s.version() == SCHEMA_VERSION
        assert await s.table_count("carry_paper") == 0
        tid = await s.carry_paper_insert(
            opened_ts=100, base="WIF", symbol="WIF/USDT:USDT", long_venue="bybit", short_venue="okx", qty=2.0, notional_usd=10.0,
            long_entry=5.0, short_entry=5.01, entry_trailing_spread_apr=0.3, entry_current_spread_apr=0.2, entry_flags=["x"],
            leverage=2.0, stop_move=0.5, lookback_days=7.0, exit_apr=0.0, slippage=0.0005, tags=["a"],
        )  # fmt: skip
        got = await s.carry_paper_get(tid)
        assert got["status"] == "open" and got["entry_flags"] == ["x"] and got["tags"] == ["a"]
        assert await s.carry_paper_close(tid, status="closed", closed_ts=200, exit_reason="stop", pnl_usd=-1.0, stopped=True) == 1
        assert await s.carry_paper_close(tid, status="closed", closed_ts=300, exit_reason="manual") == 0  # already closed
        got = await s.carry_paper_get(tid)
        assert got["stopped"] is True and got["closed_ts"] == 200
        assert len(await s.carry_paper_list(status="closed")) == 1 and await s.carry_paper_list(status="open") == []

        # claim + journal mirror in one go; a concurrent second claim writes nothing
        tid2 = await s.carry_paper_insert(
            opened_ts=100, base="WIF", symbol="WIF/USDT:USDT", long_venue="bybit", short_venue="okx", qty=2.0, notional_usd=10.0,
            long_entry=5.0, short_entry=5.01, leverage=2.0, stop_move=0.5, lookback_days=7.0, exit_apr=0.0, entry_tradable=True,
        )  # fmt: skip
        assert (await s.carry_paper_get(tid2))["entry_tradable"] is True
        journal = dict(opened_ts=100, symbol="PP:WIF:bybit/okx", side="long", size=10.0, entry=1.0, exit=1.01, thesis="t", tags=["pp-paper"], outcome="win", pnl=0.1)  # fmt: skip
        results = await asyncio.gather(
            *[s.carry_paper_close(tid2, status="closed", closed_ts=300, exit_reason="manual", journal=journal) for _ in range(2)]
        )
        assert sorted(results) == [0, 1]
        assert await s.table_count("journal") == 1
        assert (await s.carry_paper_get(tid2))["journal_id"] == 1
    finally:
        await s.aclose()


# ---------- tools ----------


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "paper.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


def _fake_rate(venue: str, i: int) -> float:
    base = {"binanceusdm": 0.0001, "bybit": 0.00008}[venue]
    return base + ((-1) ** i) * 0.00001 * (i % 7)


async def test_open_status_close_roundtrip(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        open_at = NOW - timedelta(days=3)
        rt.now = lambda: open_at
        o = await _call(srv, "carry_paper_open", base="btc", long_venue="binanceusdm", short_venue="bybit", notional_usd=1000, tags=["t1"])
        assert o["ok"] is True, o
        d = o["data"]
        assert d["id"] == 1 and d["base"] == "BTC" and d["qty"] == pytest.approx(1000 / 82750.0, rel=1e-6)
        assert d["stop_move"] == 0.5 and d["price_gap"] == 0
        assert d["tradable_now"] is False  # fake spread is far below the 10 % entry bar
        assert any("not tradable_now" in w for w in o["warnings"])
        assert any("-0.17" in w and "37 %" in w for w in o["warnings"])  # honest backtest caveat

        rt.now = lambda: NOW
        s = await _call(srv, "carry_paper_status")
        assert s["ok"] is True, s
        (tr,) = s["data"]["open"]
        mk = tr["mark"]
        # fake history is on the 8h grid ending 2026-10-10 12:00 UTC; payments in (open, NOW]; price falls back to entry
        end, step = 1791637200000, H8
        open_ms = int(open_at.timestamp()) * 1000
        idx = [i for i in range(90) if open_ms < end - (89 - i) * step <= int(NOW.timestamp() * 1000)]
        assert mk["n_funding_payments"] == 2 * len(idx) > 0
        expect = sum(_fake_rate("bybit", i) - _fake_rate("binanceusdm", i) for i in idx) * 1000.0
        assert mk["funding_pnl_usd"] == pytest.approx(expect, abs=0.01)
        assert mk["price_pnl_usd"] == 0
        assert mk["costs_usd"] == pytest.approx(2 * (0.0005 + 0.00055 + 0.001) * 1000, abs=0.01)
        assert tr["exit_signal"] in (None, "spread_exit")
        assert s["data"]["summary_closed"] == {"n": 0, "entry_tradable": {"n": 0}}
        assert mk["stopped"] is False and mk["stop_check"] == "complete" and tr["exit_signal"] != "stop"
        assert not any("missing" in w and "4h bar" in w for w in s["warnings"])

        c = await _call(srv, "carry_paper_close", id=1, reason="manual", note="done")
        assert c["ok"] is True, c
        cd = c["data"]
        assert cd["status"] == "closed" and cd["exit_reason"] == "manual" and cd["stopped"] is False
        assert cd["return_on_notional"] == pytest.approx(mk["return_on_notional"], abs=1e-4)
        again = await _call(srv, "carry_paper_close", id=1, reason="manual")
        assert again["ok"] is False

        stats = await _call(srv, "journal_stats", tag="pp-paper")
        assert stats["data"]["closed_trades"] == 1
        assert stats["data"]["total_pnl"] == pytest.approx(cd["pnl_usd"], abs=0.01)
        jl = await _call(srv, "journal_list", tag="pp-paper")
        (row,) = jl["data"]["trades"]
        assert row["symbol"] == "PP:BTC:binanceusdm/bybit" and row["side"] == "long" and "t1" in row["tags"]
        assert row["exit"] == pytest.approx(1 + cd["return_on_notional"], abs=1e-4)

        s2 = await _call(srv, "carry_paper_status", include_closed=True)
        assert s2["data"]["open"] == [] and s2["data"]["summary_closed"]["n"] == 1 and len(s2["data"]["closed"]) == 1
        # opened while not tradable_now: counted in `n` but not in the backtest-comparable subset, tagged off-rule
        assert s2["data"]["summary_closed"]["entry_tradable"]["n"] == 0 and "pp-paper-offrule" in row["tags"]
    finally:
        await rt.store.aclose()


async def test_close_stop_charges_penalty_and_cancel(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        first = await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit")
        assert first["ok"] and not any("already exist" in w for w in first["warnings"])
        second = await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit")
        assert second["ok"] and any("already exist" in w for w in second["warnings"])  # warned, still recorded
        # reason='stop' without a stop in the data invents no penalty and no stopped flag
        fake_stop = await _call(srv, "carry_paper_close", id=1, reason="stop")
        manual = await _call(srv, "carry_paper_close", id=2, reason="manual")
        assert fake_stop["data"]["stopped"] is False and fake_stop["data"]["exit_reason"] == "stop"
        assert any("no stop is found" in w for w in fake_stop["warnings"])
        assert fake_stop["data"]["costs_usd"] == pytest.approx(manual["data"]["costs_usd"], abs=0.01)
        assert (await _call(srv, "carry_paper_open", base="BTC", long_venue="bybit", short_venue="binanceusdm"))["ok"]
        canc = await _call(srv, "carry_paper_close", id=3, reason="manual", cancelled=True)
        assert canc["data"]["status"] == "cancelled"
        assert (await _call(srv, "journal_stats", tag="pp-paper"))["data"]["closed_trades"] == 2
    finally:
        await rt.store.aclose()


async def test_open_refuses_price_gap_and_provider_failure(state, tmp_path):
    class GapEx(FakeEx):
        async def fetch_tickers(self, symbols=None):
            out = await super().fetch_tickers(symbols)
            if self.id == "bybit":
                for v in out.values():
                    v["last"] = v["last"] * 1.05
            return out

    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "gap.sqlite")),
        transports=make_transports(state),
        exchange_factory=GapEx,
    )
    rt.now = lambda: NOW
    await rt.store.init()
    srv, _ = build_server(rt)
    try:
        gap = await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit")
        assert gap["ok"] is False and gap["data"] is None
        assert any(m["field"] == "price_gap" for m in gap["missing"])
        far = await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit", max_entry_gap=0.2)
        assert far["ok"] is True  # 5 % is inside a 20 % allowance
        await rt.store.init()
        rt2_before = len(await rt.store.carry_paper_list())

        mism = Runtime(
            Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "mm.sqlite")),
            transports=make_transports(state),
            exchange_factory=type("MM", (GapEx,), {"fetch_tickers": _mismatch_tickers}),
        )
        mism.now = lambda: NOW
        await mism.store.init()
        srv2, _ = build_server(mism)
        bad = await _call(srv2, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit", max_entry_gap=0.2)
        assert bad["ok"] is False and any(m["field"] == "price_mismatch" for m in bad["missing"])
        await mism.store.init()
        assert await mism.store.carry_paper_list() == []
        await mism.store.aclose()
        assert rt2_before == 1
    finally:
        await rt.store.aclose()


async def _mismatch_tickers(self, symbols=None):
    out = await FakeEx.fetch_tickers(self, symbols)
    if self.id == "bybit":
        for v in out.values():
            v["last"] = v["last"] * 3
    return out


async def test_open_provider_failure_writes_nothing(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "carry_paper_open", base="BTC", long_venue="okx", short_venue="bybit")
        assert env["ok"] is False and env["missing"]
        await rt.store.init()
        assert await rt.store.carry_paper_list() == []
        same = await _call(srv, "carry_paper_open", base="BTC", long_venue="bybit", short_venue="bybit")
        assert same["ok"] is False
    finally:
        await rt.store.aclose()


async def test_status_and_close_with_provider_failure(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        assert (await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit"))["ok"]
        await rt.store.init()
        await rt.store.carry_paper_insert(
            opened_ts=int(NOW.timestamp()) - 86400, base="BTC", symbol="BTC/USDT:USDT", long_venue="okx", short_venue="bybit", qty=0.01,
            notional_usd=1000.0, long_entry=82750.0, short_entry=82750.0, leverage=2.0, stop_move=0.5, lookback_days=7.0, exit_apr=0.0,
            slippage=0.0005,
        )  # fmt: skip
        s = await _call(srv, "carry_paper_status")
        assert len(s["data"]["open"]) == 2
        marked = [t for t in s["data"]["open"] if "mark" in t]
        assert [t["id"] for t in marked] == [1]
        assert any(m["field"] == "carry_paper:2" for m in s["missing"])

        c = await _call(srv, "carry_paper_close", id=2, reason="manual")
        assert c["ok"] is False and c["data"] is None
        await rt.store.init()
        assert (await rt.store.carry_paper_get(2))["status"] == "open"
        assert await rt.store.journal_list(tag="pp-paper") == []

        nf = await _call(srv, "carry_paper_status", id=99)
        assert nf["ok"] is False
    finally:
        await rt.store.aclose()


async def test_tools_without_store_report_config_miss(state):
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports(state), exchange_factory=FakeEx)
    srv, _ = build_server(rt)
    for name, args in (
        ("carry_paper_open", dict(base="BTC", long_venue="binanceusdm", short_venue="bybit")),
        ("carry_paper_status", {}),
        ("carry_paper_close", dict(id=1, reason="manual")),
    ):
        env = await _call(srv, name, **args)
        assert env["ok"] is False and env["missing"][0]["provider"] == "tradedesk"


# ---------- stop detection, pagination, atomic close through the tools ----------

SPIKE_TS = 1791590400000  # 2026-10-10 00:00 UTC on the 4h grid; the bar closes 04:00, 9 h before NOW


class SpikeEx(FakeEx):
    """4h bars honouring since / limit / until; bybit's bar at SPIKE_TS has a +60 % wick."""

    calls: list = []
    fail_second_bybit_page = False

    async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
        rows = await super().fetch_ohlcv(symbol, timeframe, limit, since, params)
        if timeframe == "4h" and since is not None:
            type(self).calls.append((self.id, since, limit, len(rows)))
            n_bybit = sum(1 for c in type(self).calls if c[0] == "bybit")
            if self.id == "bybit" and type(self).fail_second_bybit_page and n_bybit == 2:
                raise ccxt.RateLimitExceeded("bybit 429")
            for x in rows:
                if self.id == "bybit" and x[0] == SPIKE_TS:
                    x[2] = 82750.0 * 1.6
            if self.id == "binanceusdm":
                rows.insert(0, [since - 1, None, None, None, None, 0.0])  # malformed row must be skipped, not raise
        return rows


async def _rt_with(state, tmp_path, factory) -> Runtime:
    SpikeEx.calls, SpikeEx.fail_second_bybit_page = [], False
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "spike.sqlite")),
        transports=make_transports(state),
        exchange_factory=factory,
    )
    rt.now = lambda: NOW
    await rt.store.init()
    return rt


async def _call_many(server, calls):
    async with Client(server) as c:
        outs = await asyncio.gather(*[c.call_tool(n, a) for n, a in calls])
    return [o.structured_content if o.structured_content is not None else json.loads(o.content[0].text) for o in outs]


def _expected_funding_to(stop_ms: int, open_ms: int) -> float:
    end, step = 1791637200000, H8
    idx = [i for i in range(90) if open_ms < end - (89 - i) * step <= stop_ms]
    return sum(_fake_rate("bybit", i) - _fake_rate("binanceusdm", i) for i in idx) * 1000.0


async def test_status_detects_stop_through_paginated_bars_and_close_freezes_it(state, tmp_path):
    rt = await _rt_with(state, tmp_path, SpikeEx)
    srv, _ = build_server(rt)
    try:
        open_at = NOW - timedelta(days=20)  # 20 d of 4h bars = 120 > one 100-bar page
        rt.now = lambda: open_at
        assert (await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit"))["ok"]
        rt.now = lambda: NOW
        s = await _call(srv, "carry_paper_status")
        (tr,) = s["data"]["open"]
        mk = tr["mark"]
        for venue in ("binanceusdm", "bybit"):
            pages = [c for c in SpikeEx.calls if c[0] == venue]
            assert len(pages) >= 2 and all(c[2] == 100 and c[3] <= 100 for c in pages)  # paged, never above the cap
        assert tr["exit_signal"] == "stop" and mk["stopped"] is True and mk["stop_check"] == "complete"
        assert mk["stop_at"] == "2026-10-10T04:00:00+00:00"
        stop_ms = SPIKE_TS + 4 * 3_600_000
        open_ms = int(open_at.timestamp()) * 1000
        assert mk["funding_pnl_usd"] == pytest.approx(_expected_funding_to(stop_ms, open_ms), abs=0.01)
        side = (0.0005 + 0.00055 + 0.001) * 1000
        assert mk["costs_usd"] == pytest.approx(2 * side + 10.0, abs=0.01)  # exit costs and penalty frozen at the stop
        assert mk["price_pnl_usd"] == 0 and mk["held_days"] == pytest.approx((stop_ms - open_ms) / DAY_MS, abs=0.01)

        # closing by hand later still records the stop, the penalty and the frozen result
        c = await _call(srv, "carry_paper_close", id=1, reason="manual")
        assert c["ok"] is True, c
        d = c["data"]
        assert d["stopped"] is True and d["exit_reason"] == "manual" and d["stop_at"] == mk["stop_at"]
        assert d["closed"] == mk["stop_at"] and d["costs_usd"] == mk["costs_usd"]
        assert d["return_on_notional"] == pytest.approx(mk["return_on_notional"], abs=1e-4)
        assert any("hit its stop" in w for w in c["warnings"])
        s2 = await _call(srv, "carry_paper_status")
        assert s2["data"]["summary_closed"]["share_stopped"] == 1.0
    finally:
        await rt.store.aclose()


async def test_concurrent_close_writes_one_journal_row(state, tmp_path):
    rt = await _rt_with(state, tmp_path, SpikeEx)
    srv, _ = build_server(rt)
    try:
        assert (await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit"))["ok"]
        a, b = await _call_many(
            srv, [("carry_paper_close", dict(id=1, reason="manual")), ("carry_paper_close", dict(id=1, reason="manual"))]
        )
        oks = sorted([a["ok"], b["ok"]])
        assert oks == [False, True]
        loser = a if not a["ok"] else b
        assert loser["data"] is None and loser["missing"][0]["field"] == "carry_paper"
        jl = await _call(srv, "journal_list", tag="pp-paper")
        assert jl["data"]["count"] == 1
        assert (await _call(srv, "journal_stats", tag="pp-paper"))["data"]["closed_trades"] == 1
        # a repeated close afterwards is rejected too
        again = await _call(srv, "carry_paper_close", id=1, reason="manual")
        assert again["ok"] is False
        assert (await _call(srv, "journal_list", tag="pp-paper"))["data"]["count"] == 1
    finally:
        await rt.store.aclose()


async def test_bar_page_failure_warns_and_marks_stop_check_incomplete(state, tmp_path):
    rt = await _rt_with(state, tmp_path, SpikeEx)
    SpikeEx.fail_second_bybit_page = True
    srv, _ = build_server(rt)
    try:
        rt.now = lambda: NOW - timedelta(days=20)
        assert (await _call(srv, "carry_paper_open", base="BTC", long_venue="binanceusdm", short_venue="bybit"))["ok"]
        rt.now = lambda: NOW
        s = await _call(srv, "carry_paper_status")
        assert s["ok"] is True
        (tr,) = s["data"]["open"]
        assert tr["mark"]["stop_check"] == "incomplete"
        assert any("later page failed" in w for w in s["warnings"])
        assert any("bar(s) missing" in w for w in s["warnings"])
        assert any(m["field"].startswith("ohlcv:bybit") for m in s["missing"])
    finally:
        await rt.store.aclose()


async def test_status_is_not_ok_when_no_open_pair_can_be_marked(state, tmp_path):
    rt = await _rt_with(state, tmp_path, SpikeEx)
    srv, _ = build_server(rt)
    try:
        await rt.store.carry_paper_insert(
            opened_ts=int(NOW.timestamp()) - 86400, base="BTC", symbol="BTC/USDT:USDT", long_venue="okx", short_venue="bybit", qty=0.01,
            notional_usd=1000.0, long_entry=82750.0, short_entry=82750.0, leverage=2.0, stop_move=0.5, lookback_days=7.0, exit_apr=0.0,
        )  # fmt: skip
        for args in ({}, {"id": 1}):
            s = await _call(srv, "carry_paper_status", **args)
            assert s["ok"] is False and s["data"]["open"][0].get("mark") is None
            assert any(m["field"] == "carry_paper:1" for m in s["missing"])
    finally:
        await rt.store.aclose()


async def test_entry_tradable_subset_in_summary_and_journal_tags(state, tmp_path):
    rt = await _rt_with(state, tmp_path, SpikeEx)
    srv, _ = build_server(rt)
    try:
        for tradable in (True, False):
            await rt.store.carry_paper_insert(
                opened_ts=int(NOW.timestamp()) - 3600, base="BTC", symbol="BTC/USDT:USDT", long_venue="binanceusdm",
                short_venue="bybit", qty=0.01, notional_usd=1000.0, long_entry=82750.0, short_entry=82750.0, leverage=2.0,
                stop_move=0.5, lookback_days=7.0, exit_apr=0.0, entry_tradable=tradable,
            )  # fmt: skip
        for i in (1, 2):
            assert (await _call(srv, "carry_paper_close", id=i, reason="manual"))["ok"]
        s = await _call(srv, "carry_paper_status")
        assert s["data"]["summary_closed"]["n"] == 2 and s["data"]["summary_closed"]["entry_tradable"]["n"] == 1
        tags = [t["tags"] for t in (await _call(srv, "journal_list", tag="pp-paper"))["data"]["trades"]]
        assert sum("pp-paper-offrule" in t for t in tags) == 1
    finally:
        await rt.store.aclose()
