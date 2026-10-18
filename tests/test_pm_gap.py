"""Polymarket <-> Deribit options gap: pure analytics against analytic references, the scan, the job and the tool."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime

import httpx
import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.analytics import probability
from tradedesk.analytics.options import Smile, build_chain, build_smile, group_by_expiry
from tradedesk.analytics.pm_gap import (
    YEAR_S,
    classify_open_market,
    gap_summary,
    model_probability,
    net_edge,
    path_extremes,
    pick_expiry,
    top_of_book,
    touch_state,
    weekly_window,
)
from tradedesk.analytics.pm_threshold import digital_above, settle_ts
from tradedesk.analytics.pm_touch import day_window
from tradedesk.collector.pm_gap import collect_pm_gap, scan_gaps
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

NOW_TS = int(NOW.timestamp())
# a Deribit expiry exists on 2026-10-30 08:00 UTC; the markets below end before it
END_ABOVE = settle_ts(date(2026, 10, 29))  # 12:00 ET = 16:00 UTC
START_TOUCH, END_TOUCH = day_window(date(2026, 10, 29))  # 00:00 ET to 00:00 ET = 04:00 UTC to 04:00 UTC
NOW_IN = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)  # inside the touch window
NOW_IN_TS = int(NOW_IN.timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


STANDARD_RULES = (
    "This market resolves via the Binance 1 minute candle for BTC/USDT 12:00 in the ET timezone (noon). "
    'The "Close" price higher than the price specified in the title resolves Yes.'
)
UP_RULES = (
    "This market resolves via the Binance 1 minute candle for Bitcoin (BTC/USDT), specifically the highest price reached "
    'between 12:00 AM ET and 11:59 PM ET on the date. The "High" price equal to or greater than the price specified in the title resolves Yes.'
)
DOWN_RULES = UP_RULES.replace('"High" price equal to or greater', '"Low" price equal to or lower').replace("highest", "lowest")


def _market(mid: str, question: str, yes: str, no: str, **extra) -> dict:
    return {
        "id": mid,
        "question": question,
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.2","0.8"]',
        "clobTokenIds": json.dumps([yes, no]),
        "endDate": iso(END_TOUCH),
        "active": True,
        "closed": False,
        "volumeNum": 12_345.0,
        "description": STANDARD_RULES,
        **extra,
    }


def _touch(mid: str, verb: str, strike: int, rules: str) -> dict:
    return _market(mid, f"Will Bitcoin {verb} ${strike:,} on October 29?", "Y" + mid, "N" + mid, description=rules)


def _events() -> list[dict]:
    return [
        {
            "id": "1",
            "slug": "bitcoin-above-on-october-29",
            "markets": [_market("m1", "Will the price of Bitcoin be above $90,000 on October 29?", "Y1", "N1", endDate=iso(END_ABOVE))],
        },
        {
            "id": "2",
            "slug": "what-price-will-bitcoin-hit-on-october-29",
            "markets": [
                _touch("m2", "reach", 95_000, "touch rules"),  # nonstandard text, thin ask
                _touch("m2b", "dip to", 70_000, DOWN_RULES),
                _touch("m2c", "reach", 80_000, UP_RULES),  # touched in the window (path high 82 000)
                _touch("m2d", "dip to", 81_500, DOWN_RULES),  # touched in the window (path low 81 000)
                _touch("m2w", "reach", 82_500, UP_RULES),  # not touched, but below the forward: wrong side
                _touch("m2e", "dip to", 75_000, DOWN_RULES),
            ],
        },
        {
            "id": "3",
            "slug": "ethereum-above-on-october-29",
            "markets": [
                _market(
                    "m3",
                    "Will the price of Ethereum be above $3,000 on October 29?",
                    "Y3",
                    "N3",
                    endDate=iso(END_ABOVE),
                    feesEnabled=True,
                    feeSchedule={"rate": 0.07, "exponent": 1},
                    description=STANDARD_RULES.replace("BTC/USDT", "ETH/USDT"),
                )
            ],
        },
        {"id": "4", "slug": "solana-above-on-october-29", "markets": [_market("m4", "Will Solana be above $200?", "Y4", "N4")]},
        {
            "id": "5",
            "slug": "bitcoin-above-on-december-31",
            "markets": [
                _market(
                    "m5",
                    "Will the price of Bitcoin be above $90,000 on December 31?",
                    "Y5",
                    "N5",
                    endDate=iso(settle_ts(date(2026, 12, 31))),
                )
            ],
        },
    ]


def _book(token: str, bids, asks) -> dict:
    return {
        "asset_id": token,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
    }


# unsorted on purpose: the API does not document the order of levels
BOOKS = {
    "Y1": _book("Y1", [(0.20, 500), (0.21, 1000), (0.10, 9)], [(0.25, 900), (0.24, 1000)]),
    "N1": _book("N1", [(0.74, 800), (0.75, 600)], [(0.79, 1000), (0.80, 100)]),
    "Ym2": _book("Ym2", [(0.05, 100)], [(0.07, 10)]),  # thin: 0.7 USD at the best ask
    "Nm2": _book("Nm2", [(0.92, 100)], [(0.94, 10)]),
    "Y3": _book("Y3", [(0.30, 500)], [(0.34, 1000)]),
    "N3": _book("N3", [(0.65, 500)], [(0.69, 1000)]),
}
for _m, _y in (("m2b", 0.02), ("m2c", 0.95), ("m2d", 0.95), ("m2w", 0.30), ("m2e", 0.10)):
    BOOKS["Y" + _m] = _book("Y" + _m, [(_y - 0.01, 20_000)], [(_y, 20_000)])
    BOOKS["N" + _m] = _book("N" + _m, [(1 - _y - 0.01, 20_000)], [(1 - _y + 0.01, 20_000)])


def _bars(start: int, now: int, high: float = 82_000.0, low: float = 81_000.0, since_ms: int = 0) -> list[list[float]]:
    return [[t * 1000, 81_500.0, high, low, 81_800.0, 1.0] for t in range(start, now + 1, 3600) if t * 1000 >= since_ms]


def path_ex(bars=None, fail: bool = False):
    """FakeEx whose Binance BTC/USDT 1h bars are the given ones (or fail)."""

    class PathEx(FakeEx):
        async def fetch_ohlcv(self, symbol, timeframe="1h", limit=100, since=None, params=None):
            if symbol == "BTC/USDT" and timeframe == "1h" and since is not None:
                if fail:
                    import ccxt

                    raise ccxt.NetworkError("binance down")
                return [b for b in (bars or []) if b[0] >= since][:limit]
            return await super().fetch_ohlcv(symbol, timeframe, limit, since, params)

    return PathEx


def _transports(state, gamma_status: int = 200, clob_status: int = 200, events=None, gamma=None) -> dict:
    def default_gamma(req: httpx.Request) -> httpx.Response:
        if gamma_status != 200:
            return httpx.Response(gamma_status, text="down")
        assert req.url.path == "/events/keyset"
        return httpx.Response(200, json={"events": events if events is not None else _events(), "next_cursor": None})

    def clob(req: httpx.Request) -> httpx.Response:
        if clob_status != 200:
            return httpx.Response(clob_status, text="down")
        assert req.url.path == "/books"
        wanted = [x["token_id"] for x in json.loads(req.content)]
        return httpx.Response(200, json=[BOOKS[t] for t in wanted if t in BOOKS])

    tr = make_transports(state)
    tr["polymarket:gamma"] = httpx.MockTransport(gamma or default_gamma)
    tr["polymarket:clob"] = httpx.MockTransport(clob)
    return tr


async def _rt(state, now: datetime = NOW, ex=FakeEx, **kw) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=":memory:", collector_enabled=True, **kw.pop("settings", {})),
        transports=_transports(state, **kw),
        exchange_factory=ex,
    )
    rt.now = lambda: now
    await rt.store.init()
    return rt


async def _call(rt: Runtime, **args) -> dict:
    srv, _ = build_server(rt)
    async with Client(srv) as c:
        res = await c.call_tool("polymarket_options_gap", args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


# ----------------------------------------------------------------------------- pure functions


def test_top_of_book_finds_best_levels_whatever_the_order():
    t = top_of_book(
        {
            "bids": [{"price": "0.2", "size": "5"}, {"price": "0.4", "size": "10"}, {"price": "0.4", "size": "2"}],
            "asks": [{"price": "0.9", "size": "1"}, {"price": "0.6", "size": "50"}, {"price": "bad", "size": "1"}],
        }
    )
    assert t["bid"] == 0.4 and t["bid_size"] == 12 and t["bid_usd"] == pytest.approx(4.8)
    assert t["ask"] == 0.6 and t["ask_size"] == 50 and t["ask_usd"] == pytest.approx(30.0)
    assert top_of_book({}) == {"bid": None, "bid_size": None, "bid_usd": None, "ask": None, "ask_size": None, "ask_usd": None}


def test_net_edge_matches_hand_calculation():
    # 0.60 chance, ask 0.50, fee 0.07 * 0.5 * 0.5 = 0.0175 -> 0.60 - 0.50 - 0.0175
    assert net_edge(0.60, 0.50, 0.07, 1.0) == pytest.approx(0.0825)
    assert net_edge(0.60, 0.50, 0.0, 1.0) == pytest.approx(0.10)  # fees off
    assert net_edge(0.60, None, 0.07, 1.0) is None


def test_gap_summary_picks_the_better_side_and_filters_depth():
    yes = {"bid": 0.28, "ask": 0.32, "ask_usd": 320.0}
    no = {"bid": 0.67, "ask": 0.71, "ask_usd": 150.0}
    g = gap_summary(0.50, yes, no, 0.0, 1.0, 200.0)
    assert g["mid_yes"] == pytest.approx(0.30) and g["gap_mid"] == pytest.approx(0.20) and g["half_spread_yes"] == pytest.approx(0.02)
    assert g["edge_yes"] == pytest.approx(0.18) and g["edge_no"] == pytest.approx(0.50 - 0.71)
    assert g["side"] == "yes" and g["net_edge"] == pytest.approx(0.18) and g["depth_ok"]
    # model far below the market: NO is the better side, but only $150 stands at its ask
    g2 = gap_summary(0.05, yes, no, 0.0, 1.0, 200.0)
    assert g2["side"] == "no" and g2["net_edge"] == pytest.approx(0.95 - 0.71) and not g2["depth_ok"]
    assert (
        gap_summary(0.5, {"bid": None, "ask": None, "ask_usd": None}, {"bid": None, "ask": None, "ask_usd": None}, 0, 1)["net_edge"] is None
    )


def test_model_probability_flat_smile_against_closed_forms():
    F, sigma, K = 100.0, 0.5, 120.0
    sm = Smile(F, 99.0, [80.0, 100.0, 120.0, 140.0], [sigma] * 4)  # T is replaced by the market's own horizon
    now, end = 1_000_000, 1_000_000 + 30 * 86_400
    tau = 30 / 365
    above = model_probability(sm, "above", K, now, end)
    # flat smile: -dC/dK = N(d2); same driftless lognormal as pm_threshold.digital_above
    assert above["prob"] == pytest.approx(digital_above(F, K, sigma, tau), abs=1e-9)
    assert above["covered"] and above["tau_days"] == pytest.approx(30.0)
    touch = model_probability(sm, "touch", K, now, end, "up")
    s, mu, b = sigma * math.sqrt(tau), -0.5 * sigma**2, math.log(K / F)
    ncdf = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))  # noqa: E731
    ref = ncdf((-b + mu * tau) / s) + math.exp(2 * mu * b / sigma**2) * ncdf((-b - mu * tau) / s)
    assert touch["prob"] == pytest.approx(ref, abs=1e-12) and touch["prob"] == pytest.approx(probability.prob_touch(F, K, sigma, tau))
    assert touch["prob"] > above["prob"] and touch["note"] == "ok"  # touching is at least as likely as finishing above
    # a "dip" below the forward uses the lower barrier; a nearer strike is more likely than a farther one
    near = model_probability(sm, "touch", 90.0, now, end, "down")["prob"]
    far = model_probability(sm, "touch", 80.0, now, end, "down")["prob"]
    assert near == pytest.approx(probability.prob_touch(F, 90.0, sigma, tau)) and near > far > 0
    assert model_probability(sm, "touch", 200.0, now, end, "up")["covered"] is False
    # direction decides the side: a "reach" below the forward and a "dip" above it have no honest number
    assert model_probability(sm, "touch", 90.0, now, end, "up") == {
        "prob": None,
        "iv": pytest.approx(sigma),
        "covered": True,
        "tau_days": pytest.approx(30.0),
        "note": "wrong_side",
    }
    assert model_probability(sm, "touch", 110.0, now, end, "down")["prob"] is None
    assert model_probability(sm, "above", K, now, now - 1) is None
    with pytest.raises(ValueError):
        model_probability(sm, "other", K, now, end)
    with pytest.raises(ValueError):
        model_probability(sm, "touch", K, now, end)  # touch needs a direction


def test_touch_state_and_path_extremes():
    start = 1_000_000
    hour = lambda i, h, lo: [(start + i * 3600) * 1000, 100.0, h, lo, 100.0, 1.0]  # noqa: E731
    bars = [hour(2, 103.0, 99.0), hour(0, 101.0, 98.0), hour(1, 104.0, 100.0), [(start - 3600) * 1000, 100.0, 500.0, 1.0, 100.0, 1.0]]
    ext = path_extremes(bars, start)  # the bar before the window is ignored, order does not matter
    assert (ext["high"], ext["low"], ext["last"]) == (104.0, 98.0, 100.0)
    late = path_extremes([hour(5, 104.0, 98.0)], start)  # the window start is not covered: no partial guess
    assert late["high"] is None and late["low"] is None
    assert path_extremes([], start)["high"] is None
    now = start + 3600
    assert touch_state("up", 104.0, now, start, 104.0, 98.0) == "touched"
    assert touch_state("up", 104.5, now, start, 104.0, 98.0) == "open"
    assert touch_state("down", 98.0, now, start, 104.0, 98.0) == "touched"
    assert touch_state("down", 97.0, now, start, 104.0, 98.0) == "open"
    assert touch_state("up", 1.0, start - 1, start, None, None) == "pre_window"  # before the window nothing is known
    assert touch_state("up", 1.0, now, start, None, 98.0) == "no_path"
    assert touch_state("down", 99.0, now, start, None, 98.0) == "touched"  # only the relevant extreme is needed


def test_pick_expiry_takes_the_first_expiry_after_the_end_within_the_gap():
    exps = [datetime(2026, 10, 30, 8, tzinfo=UTC), datetime(2026, 11, 27, 8, tzinfo=UTC)]
    end = int(datetime(2026, 10, 30, tzinfo=UTC).timestamp())
    assert pick_expiry(exps, end) == exps[0]
    assert pick_expiry(exps, end + 9 * 3600) is None  # past 08:00 the next one is 27 Nov: far beyond the 36 h gap
    assert pick_expiry(exps, end + 9 * 3600, max_gap_hours=1e5) == exps[1]
    assert pick_expiry(exps, end - 40 * 3600) is None  # nearest expiry is 48 h later
    assert pick_expiry([], end) is None


def test_classify_open_market_statuses():
    ev = _events()
    ok = classify_open_market(ev[0], ev[0]["markets"][0], NOW_TS, 30)
    assert ok["status"] == "ok" and ok["asset"] == "BTC" and ok["kind"] == "above" and ok["strike"] == 90_000
    assert ok["rules_standard"] and ok["end_ts"] == END_ABOVE and ok["yes_token"] == "Y1" and ok["fee_rate"] == 0.0
    assert ok["window_start"] is None and len(ok["rules_hash"]) == 16
    up = classify_open_market(ev[1], ev[1]["markets"][0], NOW_TS, 30)
    dn = classify_open_market(ev[1], ev[1]["markets"][1], NOW_TS, 30)
    assert (up["kind"], up["direction"], up["strike"], up["rules_standard"]) == ("touch", "up", 95_000, False)
    assert (dn["direction"], dn["strike"], dn["rules_standard"]) == ("down", 70_000, True)
    assert up["window_start"] == START_TOUCH and up["end_ts"] == END_TOUCH
    eth = classify_open_market(ev[2], ev[2]["markets"][0], NOW_TS, 30)
    assert eth["asset"] == "ETH" and eth["fee_rate"] == 0.07 and eth["rules_standard"]
    assert classify_open_market(ev[3], ev[3]["markets"][0], NOW_TS, 30)["status"] == "asset"
    assert classify_open_market(ev[4], ev[4]["markets"][0], NOW_TS, 30)["status"] == "window"
    assert classify_open_market(ev[0], ev[0]["markets"][0], NOW_TS, 14)["status"] == "window"  # 19 days > 14
    assert classify_open_market({"slug": "will-x-happen"}, ev[0]["markets"][0], NOW_TS, 30)["status"] == "slug"
    assert classify_open_market(ev[0], {**ev[0]["markets"][0], "closed": True}, NOW_TS, 30)["status"] == "closed"
    assert classify_open_market(ev[0], {**ev[0]["markets"][0], "outcomePrices": '["1","0"]'}, NOW_TS, 30)["status"] == "closed"
    assert classify_open_market(ev[0], {**ev[0]["markets"][0], "outcomes": '["A","B","C"]'}, NOW_TS, 30)["status"] == "tokens"
    assert classify_open_market(ev[0], {**ev[0]["markets"][0], "question": "Will it be above soon?"}, NOW_TS, 30)["status"] == "strike"
    assert (
        classify_open_market(ev[1], {**ev[1]["markets"][0], "question": "Will Bitcoin hover at $95,000?"}, NOW_TS, 30)["status"]
        == "direction"
    )
    assert classify_open_market(ev[0], {**ev[0]["markets"][0], "endDate": None}, NOW_TS, 30)["status"] == "end"


def test_classify_checks_the_resolution_time_like_the_research_classifiers():
    ev = _events()
    above, touch = ev[0], ev[1]
    m_above, m_touch = above["markets"][0], touch["markets"][0]

    def status(event, market, **over):
        return classify_open_market(event, {**market, **over}, NOW_TS, 30)["status"]

    # threshold: endDate must be 12:00 ET of the date in the slug
    assert status(above, m_above, endDate=iso(END_ABOVE + 3600)) == "end_mismatch"
    assert status(above, m_above, endDate=iso(settle_ts(date(2026, 10, 28)))) == "slug_date"  # another day than the slug
    assert status({**above, "slug": "bitcoin-above-on-october-29-2025"}, m_above) == "slug_date"
    # daily touch: endDate must be 00:00 ET after the slug date, the question must name the same day
    assert status(touch, m_touch, endDate=iso(END_TOUCH + 3600)) == "end_mismatch"
    assert status(touch, m_touch, endDate=iso(day_window(date(2026, 10, 30))[1])) == "slug_date"
    assert status(touch, m_touch, question="Will Bitcoin reach $95,000 on October 28?") == "slug_date"
    # a DST-switch day is 25 h long (2026-11-01): the 24 h window would be wrong
    dst = {"id": "d", "slug": "what-price-will-bitcoin-hit-on-november-1"}
    s, e = day_window(date(2026, 11, 1))
    assert e - s == 25 * 3600
    dm = _touch("d", "reach", 95_000, UP_RULES) | {"question": "Will Bitcoin reach $95,000 on November 1?"}
    good = classify_open_market(dst, dm | {"endDate": iso(e)}, NOW_TS, 30)
    assert good["status"] == "ok" and good["window_start"] == s
    assert classify_open_market(dst, dm | {"endDate": iso(s + 24 * 3600)}, NOW_TS, 30)["status"] == "end_mismatch"


def test_weekly_touch_window_comes_from_the_slug_and_is_checked_against_the_end():
    first, last = date(2026, 10, 26), date(2026, 11, 1)
    start, _ = day_window(first)
    end = day_window(last)[1]
    assert weekly_window("what-price-will-bitcoin-hit-october-26-november-1", end) == (start, end)
    assert weekly_window("what-price-will-bitcoin-hit-october-26-november-1-2026", end) == (start, end)
    assert weekly_window("what-price-will-bitcoin-hit-october-26-november-1", end + 3600) is None  # end is not 00:00 ET
    assert weekly_window("what-price-will-bitcoin-hit-october-26-november-8", end) is None  # slug's last day is not the end
    assert weekly_window("what-price-will-bitcoin-hit-october-1-november-1", end) is None  # more than 7 days
    assert weekly_window("what-price-will-bitcoin-hit-on-october-26", end) is None
    ev = {"id": "w", "slug": "what-price-will-bitcoin-hit-october-26-november-1"}
    m = _touch("w", "reach", 95_000, UP_RULES) | {"endDate": iso(end)}
    c = classify_open_market(ev, m, NOW_TS, 30)
    assert c["status"] == "ok" and c["window_start"] == start and c["end_ts"] == end and c["rules_standard"]
    assert classify_open_market(ev, m | {"endDate": iso(end + 3600)}, NOW_TS, 30)["status"] == "end_mismatch"
    assert classify_open_market(ev, m | {"description": "other rules"}, NOW_TS, 30)["rules_standard"] is False


# ----------------------------------------------------------------------------- scan, job, tool


async def test_scan_reads_markets_books_and_options(state):
    rt = await _rt(state)
    try:
        b = Builder()
        rows = {r["market_id"]: r for r in await scan_gaps(rt, b, max_days=30)}
        assert set(rows) == {"m1", "m2", "m2b", "m2c", "m2d", "m2w", "m2e", "m3"}  # SOL and the December market are left out
        assert not b.missing
        m1 = rows["m1"]
        assert (m1["yes_bid"], m1["yes_ask"], m1["yes_ask_usd"]) == (0.21, 0.24, pytest.approx(240.0))
        assert (m1["no_bid"], m1["no_ask"], m1["no_ask_usd"]) == (0.75, 0.79, pytest.approx(790.0))
        assert m1["hours_to_end"] == pytest.approx((END_ABOVE - NOW_TS) / 3600)
        assert m1["model_expiry"] == "2026-10-30T08:00:00+00:00" and m1["expiry_gap_hours"] == pytest.approx(16.0)
        assert 0.0 < m1["model_prob"] < 1.0 and m1["model_covered"] == 1 and m1["rules_standard"] == 1 and m1["model_note"] == "ok"
        assert m1["spot"] == 82_750.0 and m1["dvol"] == pytest.approx(0.45, abs=0.02)  # fake DVOL drifts up from 45
        assert rows["m3"]["fee_rate"] == 0.07 and rows["m2"]["rules_standard"] == 0 and rows["m2b"]["rules_standard"] == 1
        assert "markets left out" in b.warnings[-1] and "asset=1" in b.warnings[-1] and "window=1" in b.warnings[-1]
        assert "multi-strikes=" in b.warnings[0] and "hit-price=" in b.warnings[0]
        # 19 days before the windows open the start price is unknown: no number, not a made-up one
        for mid in ("m2", "m2b", "m2c", "m2d", "m2w", "m2e"):
            assert rows[mid]["model_prob"] is None and rows[mid]["model_note"] == "pre_window"
            assert rows[mid]["window_start"] == START_TOUCH and rows[mid]["path_high"] is None
    finally:
        await rt.aclose()


async def test_scan_touch_markets_inside_the_window_use_the_binance_path(state):
    ex = path_ex(_bars(START_TOUCH, NOW_IN_TS))
    rt = await _rt(state, now=NOW_IN, ex=ex)
    try:
        b = Builder()
        rows = {r["market_id"]: r for r in await scan_gaps(rt, b, max_days=30)}
        assert not b.missing
        # already reached inside the window: the event has happened, probability 1 (not a touch from the other side)
        for mid in ("m2c", "m2d"):
            assert rows[mid]["model_prob"] == 1.0 and rows[mid]["model_note"] == "touched" and rows[mid]["model_covered"] is None
        assert (rows["m2c"]["path_high"], rows["m2c"]["path_low"], rows["m2c"]["binance_last"]) == (82_000.0, 81_000.0, 81_800.0)
        # a "reach" strike below the forward that was never reached has no honest number
        raw = (await rt.deribit.book_summary("BTC", "option")).data
        by_exp = group_by_expiry(build_chain(raw, NOW_IN))
        sm = build_smile(by_exp[pick_expiry(by_exp, END_TOUCH)])
        assert sm.forward > 82_500
        assert rows["m2w"]["model_prob"] is None and rows["m2w"]["model_note"] == "wrong_side"
        # open "dip" markets: touch probability from the current price, the nearer strike is the likelier one
        tau = (END_TOUCH - NOW_IN_TS) / YEAR_S
        for mid, k in (("m2b", 70_000), ("m2e", 75_000)):
            iv = rows[mid]["model_iv"]
            assert rows[mid]["model_note"] == "ok" and rows[mid]["model_prob"] == pytest.approx(
                probability.prob_touch(sm.forward, k, iv, tau)
            )
        assert rows["m2e"]["model_prob"] > rows["m2b"]["model_prob"] > 0
        assert rows["m2"]["model_prob"] is not None and rows["m2"]["model_prob"] < 1.0  # reach 95 000: open and far
        assert rows["m1"]["model_prob"] is not None and rows["m1"]["window_start"] is None  # threshold markets need no path
    finally:
        await rt.aclose()


async def test_scan_without_binance_candles_leaves_open_touch_markets_unpriced(state):
    for ex in (path_ex(fail=True), path_ex([])):
        rt = await _rt(state, now=NOW_IN, ex=ex)
        try:
            b = Builder()
            rows = {r["market_id"]: r for r in await scan_gaps(rt, b, max_days=30)}
            assert all(rows[m]["model_prob"] is None and rows[m]["model_note"] == "no_path" for m in ("m2", "m2b", "m2c", "m2w"))
            assert rows["m1"]["model_prob"] is not None  # the threshold markets do not depend on the path
        finally:
            await rt.aclose()
    rt = await _rt(state, now=NOW_IN, ex=path_ex(fail=True))
    try:
        b = Builder()
        await scan_gaps(rt, b, max_days=30)
        assert any(m.provider == "ccxt:binance" for m in b.missing)
    finally:
        await rt.aclose()


async def test_tool_lists_gaps_after_costs_and_depth_filter(state):
    rt = await _rt(state)
    try:
        env = await _call(rt, max_days=30, min_net_edge=-1, min_depth_usd=200)
        assert env["ok"] is True and any("RISK-NEUTRAL" in w for w in env["warnings"])
        assert any("USDT" in w and "basis" in w for w in env["warnings"])
        d = env["data"]
        ids = {m["market_id"] for m in d["markets"]}
        assert "m2" not in ids  # $0.7 at the best ask (and a nonstandard text)
        assert d["with_model_probability"] == 2 and d["without_model_probability"] == 6  # six touch markets before their window
        m1 = next(m for m in d["markets"] if m["market_id"] == "m1")
        p = m1["model_prob"]
        # the better side's edge is p - ask - fee (no fees on this market), the other side's is the complement
        assert m1["edge_yes"] == pytest.approx(p - 0.24, abs=2e-4) and m1["edge_no"] == pytest.approx(1 - p - 0.79, abs=2e-4)
        assert m1["net_edge"] == pytest.approx(max(m1["edge_yes"], m1["edge_no"]))
        assert m1["gap_mid"] == pytest.approx(p - 0.225, abs=2e-4) and m1["half_spread_yes"] == pytest.approx(0.015)
        # fees: the ETH market (rate 0.07) is charged 0.07 * ask * (1 - ask)
        full = (await _call(rt, max_days=30, min_net_edge=-1, min_depth_usd=200, include_nonstandard=True))["data"]
        m3 = next(m for m in full["markets"] if m["market_id"] == "m3")  # ETH 3000 lies outside the quoted strikes
        assert m3["model_covered"] is False and "m3" not in ids
        assert m3["edge_yes"] == pytest.approx(m3["model_prob"] - 0.34 - 0.07 * 0.34 * 0.66, abs=2e-4)
        nets = [m["net_edge"] for m in d["markets"]]
        assert nets == sorted(nets, reverse=True)
        eth = await _call(rt, asset="ETH", max_days=30, min_net_edge=-1, include_nonstandard=True)
        assert {m["market_id"] for m in eth["data"]["markets"]} == {"m3"}
        strict = await _call(rt, max_days=30, min_net_edge=0.9)
        assert strict["ok"] and strict["data"]["markets"] == []
    finally:
        await rt.aclose()


async def test_tool_touched_levels_are_not_shown_as_a_no_edge_and_unpriced_ones_are_left_out(state):
    rt = await _rt(state, now=NOW_IN, ex=path_ex(_bars(START_TOUCH, NOW_IN_TS)))
    try:
        env = await _call(rt, max_days=30, min_net_edge=-1, include_nonstandard=True)
        by_id = {m["market_id"]: m for m in env["data"]["markets"]}
        assert "m2w" not in by_id  # wrong side of the forward: no probability
        for mid in ("m2c", "m2d"):
            x = by_id[mid]
            assert x["model_prob"] == 1.0 and x["model_note"] == "touched" and x["side"] == "yes"
            assert x["edge_no"] < 0 and x["net_edge"] == x["edge_yes"] == pytest.approx(1 - 0.95, abs=2e-4)
        assert by_id["m2c"]["path_high"] == 82_000.0 and by_id["m2c"]["binance_last"] == 81_800.0
        assert env["data"]["without_model_probability"] == 1  # m2w
        assert any("touched" in w for w in env["warnings"])
    finally:
        await rt.aclose()


async def test_tool_ranks_only_standard_covered_markets_unless_asked(state):
    rt = await _rt(state, now=NOW_IN, ex=path_ex(_bars(START_TOUCH, NOW_IN_TS)))
    try:
        default = (await _call(rt, max_days=30, min_net_edge=-1, min_depth_usd=0))["data"]
        listed = default["markets"]
        assert listed and all(m["rules_standard"] and m["model_covered"] is not False for m in listed)
        assert default["excluded_nonstandard_or_uncovered"] >= 2  # m2 (text) and the dips below the quoted strikes
        assert "m2" not in {m["market_id"] for m in listed}
        everything = (await _call(rt, max_days=30, min_net_edge=-1, min_depth_usd=0, include_nonstandard=True))["data"]
        assert everything["excluded_nonstandard_or_uncovered"] == 0
        assert {"m2", "m2b"} <= {m["market_id"] for m in everything["markets"]}
        assert len(everything["markets"]) > len(listed)
    finally:
        await rt.aclose()


async def test_tool_reports_polymarket_failure_without_raising(state):
    rt = await _rt(state, gamma_status=502)
    try:
        env = await _call(rt, max_days=30)
        assert env["ok"] is False and env["data"] is None
        assert any(m["provider"] == "polymarket:gamma" for m in env["missing"])
    finally:
        await rt.aclose()
    rt = await _rt(state, clob_status=502)
    try:
        env = await _call(rt, max_days=30)
        assert env["ok"] is False and any(m["provider"] == "polymarket:clob" for m in env["missing"])
    finally:
        await rt.aclose()


async def test_tool_keeps_polymarket_rows_when_deribit_is_down(state):
    state.deribit_status = 503
    rt = await _rt(state)
    try:
        env = await _call(rt, max_days=30)
        assert env["ok"] is True and env["data"]["markets"] == [] and env["data"]["with_model_probability"] == 0
        assert any(m["provider"] == "deribit" for m in env["missing"])
    finally:
        await rt.aclose()


def _gamma_by_tag(per_tag: dict[str, list[dict]]):
    def gamma(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"events": per_tag.get(req.url.params.get("tag_slug"), []), "next_cursor": None})

    return gamma


async def test_empty_gamma_answers_are_a_failure_not_a_quiet_zero(state):
    # both tags empty (a wrong tag slug gives an empty list, not an error): missing, ok=false, the job fails
    rt = await _rt(state, gamma=_gamma_by_tag({}))
    try:
        b = Builder()
        assert await scan_gaps(rt, b) == []
        fields = {m.field for m in b.missing}
        assert {"events:multi-strikes", "events:hit-price", "events"} <= fields
        with pytest.raises(RuntimeError, match="no rows"):
            await collect_pm_gap(rt)
        assert await rt.store.table_count("pm_gap_snapshot") == 0
        env = await _call(rt, max_days=30)
        assert env["ok"] is False and env["data"] is None
    finally:
        await rt.aclose()
    # only one tag empty: the other tag's rows are kept and the dead tag is reported
    evs = _events()
    rt = await _rt(state, gamma=_gamma_by_tag({"multi-strikes": [evs[0], evs[2]]}))
    try:
        b = Builder()
        rows = await scan_gaps(rt, b, max_days=30)
        assert {r["market_id"] for r in rows} == {"m1", "m3"}
        assert [m.field for m in b.missing] == ["events:hit-price"]
        assert "multi-strikes=2/2" in b.warnings[0] and "hit-price=0/0" in b.warnings[0]
        env = await _call(rt, max_days=30)
        assert env["ok"] is True and any(m["field"] == "events:hit-price" for m in env["missing"])
    finally:
        await rt.aclose()
    # events exist but none is a BTC/ETH price market: also a failure (the slug patterns no longer match)
    rt = await _rt(
        state, gamma=_gamma_by_tag({"multi-strikes": [_events()[3]], "hit-price": [{"id": "9", "slug": "who-wins", "markets": []}]})
    )
    try:
        b = Builder()
        assert await scan_gaps(rt, b) == []
        assert [m.field for m in b.missing] == ["events"]
    finally:
        await rt.aclose()


async def test_gamma_page_cap_keeps_what_was_found_and_reports_the_rest(state, monkeypatch):
    import tradedesk.collector.pm_gap as mod

    monkeypatch.setattr(mod, "MAX_EVENT_PAGES", 2)
    pages = {"n": 0}

    def gamma(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("tag_slug") != "multi-strikes":
            return httpx.Response(200, json={"events": [], "next_cursor": None})
        pages["n"] += 1
        return httpx.Response(200, json={"events": [_events()[0]], "next_cursor": f"c{pages['n']}"})  # never ends

    rt = await _rt(state, gamma=gamma)
    try:
        b = Builder()
        rows = await scan_gaps(rt, b, max_days=30)
        assert pages["n"] == 2 and [r["market_id"] for r in rows] == ["m1"]
        assert any(m.field == "events:multi-strikes" and "not finished" in m.detail for m in b.missing)
        env = await _call(rt, max_days=30)
        assert env["ok"] is True and any(m["field"] == "events:multi-strikes" for m in env["missing"])
    finally:
        await rt.aclose()


async def test_job_stores_snapshot_idempotently_and_rules_only_when_they_change(state, monkeypatch):
    events = _events()
    rt = await _rt(state, events=events)
    try:
        import tradedesk.collector.pm_gap as mod

        real = mod.scan_gaps

        async def wide(rt_, b, *, max_days=14.0):
            return await real(rt_, b, max_days=30.0)

        monkeypatch.setattr(mod, "scan_gaps", wide)
        assert await collect_pm_gap(rt) == 8
        assert await collect_pm_gap(rt) == 8  # same instant: replaced, not duplicated
        assert await rt.store.table_count("pm_gap_snapshot") == 8
        cur = await rt.store.conn.execute("SELECT * FROM pm_gap_snapshot WHERE market_id = 'm1'")
        row = dict(await cur.fetchone())
        assert (
            row["ts"] == NOW_TS
            and row["asset"] == "BTC"
            and row["rules"] == STANDARD_RULES
            and row["question"].startswith("Will the price")
        )
        assert row["model_prob"] is not None and row["yes_ask"] == 0.24 and row["model_note"] == "ok" and row["window_start"] is None
        h1 = row["rules_hash"]
        assert h1 and len(h1) == 16
        cur = await rt.store.conn.execute("SELECT model_prob, model_note, window_start FROM pm_gap_snapshot WHERE market_id = 'm2c'")
        assert tuple(await cur.fetchone()) == (None, "pre_window", START_TOUCH)
        # an hour later, text unchanged: the hash is kept in the row, the text is not stored again
        rt.now = lambda: datetime.fromtimestamp(NOW_TS + 3600, UTC)
        assert await collect_pm_gap(rt) == 8
        # two hours later Polymarket appended a clarification: the new text is stored with its new hash
        events[0]["markets"][0]["description"] = STANDARD_RULES + " Additional context: clarification."
        rt.now = lambda: datetime.fromtimestamp(NOW_TS + 7200, UTC)
        assert await collect_pm_gap(rt) == 8
        cur = await rt.store.conn.execute("SELECT ts, rules, rules_hash FROM pm_gap_snapshot WHERE market_id = 'm1' ORDER BY ts")
        got = [tuple(r) for r in await cur.fetchall()]
        assert got[0] == (NOW_TS, STANDARD_RULES, h1)
        assert got[1] == (NOW_TS + 3600, None, h1)
        assert got[2][1] == STANDARD_RULES + " Additional context: clarification." and got[2][2] != h1
    finally:
        await rt.aclose()


async def test_job_stores_the_binance_path_of_touch_markets(state, monkeypatch):
    rt = await _rt(state, now=NOW_IN, ex=path_ex(_bars(START_TOUCH, NOW_IN_TS)))
    try:
        import tradedesk.collector.pm_gap as mod

        real = mod.scan_gaps

        async def wide(rt_, b, *, max_days=14.0):
            return await real(rt_, b, max_days=30.0)

        monkeypatch.setattr(mod, "scan_gaps", wide)
        await collect_pm_gap(rt)
        cur = await rt.store.conn.execute(
            "SELECT market_id, model_prob, model_note, path_high, path_low, binance_last FROM pm_gap_snapshot "
            "WHERE market_id IN ('m2c', 'm2w', 'm1') ORDER BY market_id"
        )
        got = {r[0]: tuple(r)[1:] for r in await cur.fetchall()}
        assert got["m2c"] == (1.0, "touched", 82_000.0, 81_000.0, 81_800.0)
        assert got["m2w"] == (None, "wrong_side", 82_000.0, 81_000.0, 81_800.0)
        assert got["m1"][1] == "ok" and got["m1"][2:] == (None, None, None)
    finally:
        await rt.aclose()


async def test_job_fails_when_polymarket_is_down_and_is_scheduled_only_when_enabled(state):
    rt = await _rt(state, gamma_status=502)
    try:
        with pytest.raises(RuntimeError):
            await collect_pm_gap(rt)
        assert await rt.store.table_count("pm_gap_snapshot") == 0
        assert "pm_gap" not in [j.name for j in build_default_scheduler(rt).jobs]
    finally:
        await rt.aclose()
    on = await _rt(state, settings={"pm_gap_enabled": True})
    try:
        job = next(j for j in build_default_scheduler(on).jobs if j.name == "pm_gap")
        assert job.period_seconds == 3600
    finally:
        await on.aclose()
