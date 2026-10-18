from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import httpx
import pytest
from mcp import Client

from conftest import NOW, FakeEx, State, make_transports
from tradedesk.analytics.rec_paper import entry_book, max_loss, open_paper, settle_paper, size_for_risk, summarize
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

F, T, IV = 100_000.0, 0.1, 0.5


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


ATM = F * (2 * _ncdf(IV * math.sqrt(T) / 2) - 1)  # analytic Black-76 ATM call (= put) price at r = 0
STRADDLE = [(F, True, 1.0, IV), (F, False, 1.0, IV)]


def test_atm_straddle_reference():
    book = entry_book(STRADDLE, F, F, T)
    assert book["premium"] == pytest.approx(2 * ATM, rel=1e-9)
    assert book["entry_fees"] == pytest.approx(2 * 0.0003 * F)  # fee cap (12.5 % of price) not binding


def test_max_loss_bounded_and_unbounded():
    book = entry_book(STRADDLE, F, F, T)
    loss, basis = max_loss(STRADDLE, book["premium"], book["entry_fees"], F)
    assert basis == "max_loss" and loss == pytest.approx(2 * ATM + 60.0)  # worst at the strike: premium + fees
    condor = [(110_000.0, True, -1.0, IV), (90_000.0, False, -1.0, IV), (120_000.0, True, 1.0, IV), (80_000.0, False, 1.0, IV)]
    cb = entry_book(condor, F, F, T)
    loss, basis = max_loss(condor, cb["premium"], cb["entry_fees"], F)
    # width - credit + entry fees + delivery fee of the in-the-money short call at the worst point (S = 120k)
    assert basis == "max_loss" and loss == pytest.approx(10_000.0 + cb["premium"] + cb["entry_fees"] + 0.00015 * 120_000.0)
    short = [(F, True, -1.0, IV), (F, False, -1.0, IV)]
    sb = entry_book(short, F, F, T)
    loss, basis = max_loss(short, sb["premium"], sb["entry_fees"], F)
    assert basis == "stress_up_30pct" and loss == pytest.approx(
        F + sb["premium"] + sb["entry_fees"]
    )  # the sold put at S = 0 dominates the +30 % call stress


def test_size_for_risk():
    assert size_for_risk(100.0, 291.0) == pytest.approx(0.3)  # 0.343 coins -> three 0.1 lots
    assert size_for_risk(100.0, 291.0, lot=0.01) == pytest.approx(0.34)
    assert size_for_risk(100.0, 291.0, lot=1.0) == 0.0
    assert size_for_risk(100.0, 0.0) == 0.0


def test_open_and_settle_long_straddle():
    rec = open_paper(STRADDLE, forward=F, spot=F, t_years=T, deposit_usd=10_000, risk_pct=1.0)
    assert rec["max_size_btc"] == 0.0 and rec["size_btc"] == 0.1 and rec["within_risk"] is False
    rec = open_paper(STRADDLE, forward=F, spot=F, t_years=T, deposit_usd=10_000_000, risk_pct=1.0)
    assert rec["within_risk"] and rec["size_btc"] == pytest.approx(math.floor(100_000 / (2 * ATM + 60.0) / 0.1 + 1e-9) * 0.1)
    legs = [[k, c, q, iv, p] for (k, c, q, iv), p in zip(STRADDLE, rec["leg_prices"], strict=True)]
    trade = {
        "legs": legs,
        "premium_per_btc": rec["premium"],
        "entry_fees_per_btc": rec["entry_fees"],
        "size_btc": rec["size_btc"],
        "risk_usd": rec["risk_usd"],
        "spot": F,
    }
    res = settle_paper(trade, 140_000.0)
    expect = 40_000.0 - 2 * ATM - 60.0 - 0.00015 * 140_000.0
    assert res["pnl_per_btc"] == pytest.approx(expect)
    assert res["pnl_usd"] == pytest.approx(expect * rec["size_btc"])
    assert res["pnl_pct_spot"] == pytest.approx(expect / F)
    assert res["r_worst_case"] == pytest.approx(res["pnl_usd"] / rec["risk_usd"])
    assert res["r_study"] == pytest.approx(expect / (2 * ATM))  # debit: risk is the premium
    flat = settle_paper(trade, F)  # no payoff: loses exactly the maximum loss, R = -1
    assert flat["r_worst_case"] == pytest.approx(-1.0)
    s = summarize([res, flat])
    assert s["n"] == 2 and s["win_share"] == 0.5 and s["mean_r_worst_case"] == pytest.approx((res["r_worst_case"] - 1.0) / 2)
    assert s["mean_r_study"] == pytest.approx((res["r_study"] + flat["r_study"]) / 2)
    assert summarize([]) == {"n": 0}


def test_r_study_credit_uses_method_q05():
    short = [(F, True, -1.0, IV), (F, False, -1.0, IV)]
    rec = open_paper(short, forward=F, spot=F, t_years=T, deposit_usd=10_000_000, risk_pct=1.0)
    legs = [[k, c, q, iv, p] for (k, c, q, iv), p in zip(short, rec["leg_prices"], strict=True)]
    trade = {
        "legs": legs,
        "premium_per_btc": rec["premium"],
        "entry_fees_per_btc": rec["entry_fees"],
        "size_btc": rec["size_btc"],
        "risk_usd": rec["risk_usd"],
        "spot": F,
    }
    assert rec["premium"] < 0
    assert settle_paper(trade, F)["r_study"] is None  # credit without q05
    res = settle_paper({**trade, "method_q05_pct": -0.10}, F)
    assert res["r_study"] == pytest.approx(res["pnl_per_btc"] / 10_000.0)


# ----------------------------------------------------------------------------- tool

DELIVERY = "2026-11-27"


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


def _rt(tmp_path, delivery_price: float | None = 60_000.0, status: int = 200) -> Runtime:
    state = State()
    state.deribit_status = status
    tr = make_transports(state)
    base = tr["deribit"].handler

    def deribit(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("get_delivery_prices") and status == 200:
            rows = [] if delivery_price is None else [{"date": DELIVERY, "delivery_price": delivery_price}]
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"data": rows, "records_total": len(rows)}})
        return base(req)

    tr["deribit"] = httpx.MockTransport(deribit)
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "rec.sqlite")), transports=tr, exchange_factory=FakeEx)
    rt.now = lambda: NOW
    return rt


async def test_open_settle_roundtrip(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="long_strangle25", deposit_usd=100_000)
        assert o["ok"] is True, o
        d = o["data"]
        assert d["id"] == 1 and d["label"] == "hypothesis" and d["status"] == "open"
        assert d["expiry"].startswith(DELIVERY) and len(d["legs"]) == 2
        assert d["premium_usd_per_btc"] > 0 and d["risk_basis"] == "max_loss" and d["within_risk"] is True
        assert d["size_btc"] > 0 and any("HYPOTHESIS" in w for w in o["warnings"])
        # not yet expired -> stays open
        s = await _call(srv, "assistant_paper_status")
        assert s["data"]["summary_settled"] == {"method": {"n": 0}, "manual": {"n": 0}} and len(s["data"]["open"]) == 1
        # after expiry: settled at the delivery price, own table only (the owner's journal stays empty)
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        s = await _call(srv, "assistant_paper_status")
        assert s["ok"] is True and s["data"]["open"] == []
        (t,) = s["data"]["settled"]
        assert t["delivery_price"] == 60_000.0 and t["pnl_usd"] is not None and t["r_worst_case"] is not None
        assert t["r_study"] is not None and t["source"] == "manual"  # a debit trade: risk is the premium
        assert s["data"]["summary_settled"]["manual"]["n"] == 1 and s["data"]["summary_settled"]["method"] == {"n": 0}
        legs = t["legs"]
        pay = sum(q * max(k - 60_000.0, 0.0) for k, q in ((x["strike"], x["qty"]) for x in legs if x["type"] == "put"))
        assert t["pnl_usd"] == pytest.approx(
            (pay - d["premium_usd_per_btc"] - d["entry_fees_usd_per_btc"]) * d["size_btc"]
            - min(0.00015 * 60_000, 0.125 * pay) * d["size_btc"],
            abs=0.5,
        )
        await rt.store.init()
        assert await rt.store.table_count("journal") == 0
        again = await _call(srv, "assistant_paper_status")
        assert again["data"]["summary_settled"]["manual"]["n"] == 1
    finally:
        await rt.aclose()


async def test_sources_summarised_separately_and_validation(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        m = await _call(
            srv,
            "assistant_paper_open",
            structure="long_strangle25",
            deposit_usd=1e6,
            source="method",
            method_mean_pct=0.01,
            method_q05_pct=-0.05,
            regime="calm",
            note="x",
        )
        assert m["ok"] and m["data"]["source"] == "method" and m["data"]["regime"] == "calm" and m["data"]["method_q05_pct"] == -0.05
        assert (await _call(srv, "assistant_paper_open", structure="long_straddle", deposit_usd=1e6))["ok"]
        bad = await _call(srv, "assistant_paper_open", structure="long_straddle", size_btc=0.15)
        assert bad["ok"] is False and bad["missing"][0]["field"] == "size_btc"
        eth = await _call(srv, "assistant_paper_open", structure="long_straddle", size_btc=0.1, currency="ETH")
        assert eth["ok"] is False  # ETH lot is 1
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        s = (await _call(srv, "assistant_paper_status"))["data"]["summary_settled"]
        assert s["method"]["n"] == 1 and s["manual"]["n"] == 1
    finally:
        await rt.aclose()


async def test_small_deposit_flags_risk(tmp_path):
    rt = _rt(tmp_path)
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="short_straddle", expiry="30OCT26")
        assert o["ok"] is True
        assert o["data"]["within_risk"] is False and o["data"]["size_btc"] == 0.1
        assert o["data"]["risk_basis"] == "stress_up_30pct"
        assert any("budget" in w for w in o["warnings"]) and any("unbounded" in w for w in o["warnings"])
        dup = await _call(srv, "assistant_paper_open", structure="short_straddle", expiry="30OCT26")
        assert any("already exist" in w for w in dup["warnings"])
    finally:
        await rt.aclose()


async def test_delivery_not_published_stays_open(tmp_path):
    rt = _rt(tmp_path, delivery_price=None)
    srv, _ = build_server(rt)
    try:
        await _call(srv, "assistant_paper_open", structure="long_straddle", deposit_usd=1e6)
        rt.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
        s = await _call(srv, "assistant_paper_status")
        assert len(s["data"]["open"]) == 1 and any(m["field"] == "rec_paper:1" for m in s["missing"])
    finally:
        await rt.aclose()


async def test_provider_failure_never_raises(tmp_path):
    rt = _rt(tmp_path, status=500)
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="long_straddle")
        assert o["ok"] is False and o["missing"]
        await rt.store.init()
        assert await rt.store.rec_paper_list() == []
        ok = _rt(tmp_path)  # same db path: book it with a healthy provider, then fail the settle
        s2, _ = build_server(ok)
        try:
            assert (await _call(s2, "assistant_paper_open", structure="long_straddle", deposit_usd=1e6))["ok"]
        finally:
            await ok.aclose()
        bad = _rt(tmp_path, status=500)
        s3, _ = build_server(bad)
        try:
            bad.now = lambda: datetime(2026, 11, 27, 9, tzinfo=UTC)
            st = await _call(s3, "assistant_paper_status")
            assert len(st["data"]["open"]) == 1 and st["missing"]
        finally:
            await bad.aclose()
    finally:
        await rt.aclose()


async def test_no_store_configured():
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0, db_path=""), transports=make_transports(State()), exchange_factory=FakeEx)
    srv, _ = build_server(rt)
    try:
        o = await _call(srv, "assistant_paper_open", structure="long_straddle")
        s = await _call(srv, "assistant_paper_status")
        assert o["ok"] is False and s["ok"] is False
    finally:
        await rt.aclose()
