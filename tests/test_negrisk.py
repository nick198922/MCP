"""Neg-risk set sums (IDEA-20261015-polymarket-negrisk-sum-scanner): analytic examples, no network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.negrisk import (
    FEE_LOWER,
    FEE_UPPER,
    annualized,
    category,
    classify_event,
    fee_per_share,
    is_placeholder,
    is_stale,
    last_trade_ts,
    leg_cost,
    set_edge,
    thin_edge,
    ts_spread_ms,
    utc_segment,
    walk,
)

FIX = Path(__file__).parent / "fixtures"


def book(asks, bids, ts=1_000):
    return {
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "timestamp": str(ts),
    }


def test_fee_bounds():
    # upper: 0.04 * 0.25 = 0.01 per share at p = 0.5; lower = p * upper = 0.005
    assert fee_per_share(0.5, 0.04, 1) == pytest.approx(0.01)
    assert fee_per_share(0.5, 0.04, 1, FEE_LOWER) == pytest.approx(0.005)
    # 15-min crypto schedule: 0.25 * 0.25^2 = 1.5625 % of 0.5 -> 0.0078125 per share for the lower (C*p) form
    assert fee_per_share(0.5, 0.25, 2, FEE_LOWER) == pytest.approx(0.0078125)
    assert fee_per_share(0.3, 0.0, 1) == 0.0
    assert fee_per_share(1.0, 0.04, 1) == 0.0


def test_walk_depth_and_insufficient():
    asks = [(0.31, 50), (0.30, 60), (0.35, 1000)]
    fills = walk(asks, 100, best_first="low")
    assert fills == [(0.30, 60), (0.31, 40)]
    assert walk(asks, 2000, best_first="low") is None
    bids = [(0.28, 30), (0.29, 80)]
    assert walk(bids, 100, best_first="high") == [(0.29, 80), (0.28, 20)]


def test_leg_cost_buy_avg_and_fee():
    b = book([(0.30, 60), (0.31, 40)], [])
    c = leg_cost(b, 100, "buy", fee_rate=0.04, fee_exp=1, fee_mode=FEE_UPPER)
    assert c is not None
    assert c["avg"] == pytest.approx((0.30 * 60 + 0.31 * 40) / 100)
    exp_fee = 60 * 0.04 * 0.30 * 0.70 + 40 * 0.04 * 0.31 * 0.69
    assert c["fee"] == pytest.approx(exp_fee)
    assert c["worst"] == 0.31


def test_leg_cost_sell_fee_on_no_price():
    b = book([], [(0.20, 100)])
    c = leg_cost(b, 100, "sell", fee_rate=0.04, fee_exp=1, fee_mode=FEE_LOWER)
    assert c is not None
    assert c["avg"] == pytest.approx(0.20)
    # NO price 0.80; lower bound = 0.8 * 0.04 * 0.8 * 0.2 per share
    assert c["fee"] == pytest.approx(100 * 0.8 * 0.04 * 0.16)


def test_set_edge_buy_arbitrage_analytic():
    # three legs, asks 0.30 / 0.30 / 0.35 for 100 shares -> S_ask = 0.95, gross 0.05 per set
    books = [book([(0.30, 100)], [(0.29, 100)]), book([(0.30, 100)], [(0.29, 100)]), book([(0.35, 100)], [(0.34, 100)])]
    r = set_edge(books, [(0.0, 1.0)] * 3, 100, "buy", buffer_per_leg=0.005)
    assert r["feasible"]
    assert r["s"] == pytest.approx(0.95)
    assert r["gross"] == pytest.approx(0.05)
    assert r["fee"] == 0.0
    assert r["net"] == pytest.approx(0.05 - 0.015)
    assert r["capital"] == pytest.approx(0.95)
    # with fees (upper bound, rate 0.04): 0.04 * (0.21 + 0.21 + 0.2275) = 0.0259
    r2 = set_edge(books, [(0.04, 1.0)] * 3, 100, "buy", buffer_per_leg=0.0)
    assert r2["fee"] == pytest.approx(0.04 * (0.21 + 0.21 + 0.35 * 0.65))
    assert r2["net"] == pytest.approx(0.05 - r2["fee"])
    # bids sum 0.92 -> selling the set loses 0.08
    r3 = set_edge(books, [(0.0, 1.0)] * 3, 100, "sell", buffer_per_leg=0.0)
    assert r3["s"] == pytest.approx(0.92)
    assert r3["gross"] == pytest.approx(-0.08)
    assert r3["capital"] == pytest.approx(3 - 0.92)


def test_set_edge_sell_and_infeasible():
    books = [book([(0.6, 10)], [(0.55, 500)]), book([(0.5, 10)], [(0.50, 500)])]
    r = set_edge(books, [(0.0, 1.0)] * 2, 500, "sell", buffer_per_leg=0.0)
    assert r["gross"] == pytest.approx(0.05)
    assert set_edge(books, [(0.0, 1.0)] * 2, 100, "buy")["feasible"] is False


def test_depth_makes_edge_vanish():
    # top of book sums to 0.90, but at 500 shares the second level of leg 2 costs 0.70
    books = [book([(0.40, 1000)], []), book([(0.50, 100), (0.70, 1000)], [])]
    r100 = set_edge(books, [(0.0, 1.0)] * 2, 100, "buy", buffer_per_leg=0.0)
    r500 = set_edge(books, [(0.0, 1.0)] * 2, 500, "buy", buffer_per_leg=0.0)
    assert r100["net"] == pytest.approx(0.10)
    assert r500["s"] == pytest.approx(0.40 + (0.5 * 100 + 0.7 * 400) / 500)
    assert r500["net"] < 0


def test_thin_edge():
    books = [book([(0.01, 1000)], []), book([(0.985, 1000)], [])]
    r = set_edge(books, [(0.0, 1.0)] * 2, 100, "buy", buffer_per_leg=0.0)
    assert r["net"] == pytest.approx(0.005)
    assert thin_edge(r) is True
    books2 = [book([(0.30, 1000)], []), book([(0.60, 1000)], [])]
    assert thin_edge(set_edge(books2, [(0.0, 1.0)] * 2, 100, "buy", buffer_per_leg=0.0)) is False


def test_annualized_and_spread():
    assert annualized(0.01, 0.99, 365) == pytest.approx(0.01 / 0.99)
    assert annualized(0.01, 0.99, 0) is None
    assert ts_spread_ms([{"timestamp": "1000"}, {"timestamp": "4500"}]) == 3500
    assert ts_spread_ms([{"timestamp": "1000"}, {}]) is None
    assert utc_segment(0) == 0 and utc_segment(9 * 3600) == 1 and utc_segment(17 * 3600) == 2


def test_last_trade_ignores_future_and_staleness():
    trades = [{"timestamp": 100}, {"timestamp": 200}]
    now = 250
    before = last_trade_ts(trades, now)
    # data after `now` must not change the result
    assert last_trade_ts(trades + [{"timestamp": 300}, {"timestamp": 10_000}], now) == before == 200
    assert is_stale(200, 200 + 24 * 3600) is False
    assert is_stale(200, 201 + 24 * 3600) is True
    assert is_stale(None, 0) is True


def test_placeholder_and_category():
    assert is_placeholder("Other")
    assert is_placeholder("Person B")
    assert is_placeholder("")
    assert is_placeholder(None)
    assert not is_placeholder("Bitcoin")
    assert not is_placeholder("Gavin Newsom")
    assert category({"tags": [{"slug": "politics"}, {"slug": "crypto"}]}) == "crypto"
    assert category({"tags": [{"slug": "nba"}]}) == "sports"
    assert category({"tags": []}) == "other"


def _ev(**kw):
    m = {
        "closed": False,
        "clobTokenIds": '["Y1","N1"]',
        "groupItemTitle": "Alice",
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.04, "exponent": 1},
        "conditionId": "0x1",
    }
    m2 = m | {"clobTokenIds": '["Y2","N2"]', "groupItemTitle": "Bob", "conditionId": "0x2"}
    ev = {
        "id": "1",
        "negRisk": True,
        "negRiskAugmented": False,
        "volume": 60_000,
        "active": True,
        "closed": False,
        "endDate": "2026-12-31T00:00:00Z",
        "markets": [m, m2],
        "tags": [{"slug": "crypto"}],
    }
    return ev | kw


def test_classify_event_rules():
    now = 1_790_000_000.0
    c = classify_event(_ev(), now)
    assert c["status"] == "ok" and c["group"] == "full" and c["category"] == "crypto"
    assert [leg["token"] for leg in c["legs"]] == ["Y1", "Y2"]
    assert c["legs"][0]["rate"] == 0.04
    assert classify_event(_ev(negRisk=False), now)["status"] == "not_negrisk"
    assert classify_event(_ev(volume=49_999), now)["status"] == "low_volume"
    assert classify_event(_ev(negRiskAugmented=True), now)["group"] == "fuzzy"
    ev = _ev()
    ev["markets"][1]["negRiskOther"] = True
    assert classify_event(ev, now)["flags"] == ["other"]
    ev = _ev()
    ev["markets"].append({"closed": True, "outcomePrices": '["0","1"]', "clobTokenIds": '["Y3","N3"]'})
    assert len(classify_event(ev, now)["legs"]) == 2
    ev = _ev()
    ev["markets"].append({"closed": True, "outcomePrices": '["0.5","0.5"]', "clobTokenIds": '["Y3","N3"]'})
    assert classify_event(ev, now)["status"] == "closed_leg_unclear"
    ev = _ev()
    ev["markets"][0]["feesEnabled"] = False
    assert classify_event(ev, now)["legs"][0]["rate"] == 0.0


def test_real_books_top_sum_matches_best_levels():
    """Real POST /books sample (4-leg neg-risk event): at 1 share the walk equals the sum of the best asks / bids."""
    books = json.loads((FIX / "polymarket_books_negrisk_2026-10-16.json").read_text())["books"]
    assert all(b["neg_risk"] is True for b in books)
    best_ask = sum(min(float(x["price"]) for x in b["asks"]) for b in books)
    best_bid = sum(max(float(x["price"]) for x in b["bids"]) for b in books)
    zero = [(0.0, 1.0)] * len(books)
    assert set_edge(books, zero, 1, "buy", buffer_per_leg=0)["s"] == pytest.approx(best_ask)
    assert set_edge(books, zero, 1, "sell", buffer_per_leg=0)["s"] == pytest.approx(best_bid)
    # deeper walks can only make the set more expensive to buy / cheaper to sell
    assert set_edge(books, zero, 500, "buy", buffer_per_leg=0)["s"] >= best_ask - 1e-12
    assert set_edge(books, zero, 500, "sell", buffer_per_leg=0)["s"] <= best_bid + 1e-12


async def test_provider_books_batch():
    from tradedesk.core.cache import TTLCache
    from tradedesk.core.http import Http
    from tradedesk.providers.polymarket import Polymarket

    sample = json.loads((FIX / "polymarket_books_negrisk_2026-10-16.json").read_text())["books"]
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=sample)

    def mk(name, url):
        return Http(name, url, cache=TTLCache(), transport=httpx.MockTransport(handler))

    pm = Polymarket(gamma=mk("g", "https://g"), clob=mk("c", "https://c"))
    toks = [b["asset_id"] for b in sample]
    f = await pm.books(toks)
    assert seen["method"] == "POST" and seen["body"] == [{"token_id": t} for t in toks]
    assert len(f.data) == len(sample)
    assert all("timestamp" in b and "asks" in b for b in f.data)
    await pm.aclose()
