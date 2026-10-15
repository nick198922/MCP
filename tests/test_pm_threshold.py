"""Polymarket daily threshold research: pure analytics and the research provider methods (no network)."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from tradedesk.analytics.pm_threshold import (
    bootstrap_mean,
    calib_contract,
    classify_market,
    day_baskets,
    digital_above,
    history_alignment,
    last_trade_before,
    mde,
    no_trade_return,
    outcome_yes,
    parse_strike,
    quote_at,
    realized_vol,
    sd,
    settle_ts,
    standard_rules,
    value_at,
)
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.polymarket import Polymarket

FIX = Path(__file__).parent / "fixtures"
EVENTS = json.loads((FIX / "polymarket_threshold_events_2026-10-15.json").read_text())["events"]
HIST = json.loads((FIX / "polymarket_prices_history_1m_2026-10-15.json").read_text())["history"]
TRADES = json.loads((FIX / "polymarket_trades_2026-10-15.json").read_text())["trades"]
BTC_EV = EVENTS[0]  # bitcoin-above-on-february-23 (closed): 64k YES, 66k NO
YES_66K = "97007471278837526715475019011816387959807240907821566815499578324068016651279"


# ----------------------------------------------------------------------------- metadata


def test_settle_ts_is_noon_new_york_with_dst():
    assert datetime.fromtimestamp(settle_ts(date(2026, 2, 23)), UTC).hour == 17  # EST
    assert datetime.fromtimestamp(settle_ts(date(2026, 10, 15)), UTC).hour == 16  # EDT


def test_parse_strike_formats():
    assert parse_strike("Will the price of Bitcoin be above $66,000 on February 23?") == 66_000
    assert parse_strike("Bitcoin above $112000 on August 13?") == 112_000
    assert parse_strike("Will the price of XRP be above $1.00 on October 14?") == 1.0
    assert parse_strike("no number") is None
    # 2025 shorthand: "$118K" means 118 000 (parsing it as 118 broke the Binance cross-check on 51 markets)
    assert parse_strike("Bitcoin above $118K on August 18?") == 118_000
    assert parse_strike("Will Bitcoin be above $1.2m on X?") == 1_200_000
    assert parse_strike("XRP above $3 on Kalends?") == 3.0


def test_standard_rules_accepts_curly_quotes_and_rejects_other_pair_or_rule():
    d = BTC_EV["description"]
    assert standard_rules(d, "bitcoin")
    assert standard_rules(d.replace('"Close"', "“Close”"), "bitcoin")
    assert not standard_rules(d, "ethereum")  # ETH market whose text cites BTC/USDT -> excluded
    assert not standard_rules(d.replace("for BTC/USDT 12:00", "for BTC/USDT closing at 12:00"), "bitcoin")


def test_outcome_yes():
    assert outcome_yes('["1", "0"]') == 1
    assert outcome_yes('["0", "1"]') == 0
    assert outcome_yes('["0.5", "0.5"]') is None
    assert outcome_yes(None) is None


def test_classify_real_sample():
    m64, m66 = BTC_EV["markets"]
    c = classify_market("bitcoin", BTC_EV, m64)
    assert c["status"] == "ok"
    assert c["strike"] == 64_000 and c["outcome"] == 1
    assert c["T"] == int(datetime(2026, 2, 23, 17, tzinfo=UTC).timestamp())
    assert c["fees_enabled"] is False and c["fee_rate"] == 0.0
    c66 = classify_market("bitcoin", BTC_EV, m66)
    assert c66["outcome"] == 0 and c66["yes_token"] == YES_66K


def test_classify_reads_fee_schedule():
    ev = EVENTS[1]  # xrp-above-on-october-14-2026, crypto_fees_v2
    c = classify_market("xrp", ev, ev["markets"][0])
    # Since Oct 2026 the text says "closing at 12:00": a different candle -> excluded before prices
    assert c["status"] == "rules"
    m = dict(ev["markets"][0])
    m["description"] = BTC_EV["description"].replace("BTC/USDT", "XRP/USDT")
    c = classify_market("xrp", ev, m)
    assert c["status"] == "ok" and c["fee_rate"] == pytest.approx(0.07) and c["fee_exp"] == 1.0


def test_classify_rejections():
    m = dict(BTC_EV["markets"][0])
    assert classify_market("bitcoin", {**BTC_EV, "slug": "bitcoin-above-on-april-1-2026-1pm-et"}, m)["status"] == "slug"
    assert classify_market("bitcoin", BTC_EV, {**m, "endDate": "2026-02-23T16:00:00Z"})["status"] == "end_mismatch"
    assert classify_market("bitcoin", BTC_EV, {**m, "outcomePrices": '["0.5", "0.5"]'})["status"] == "unresolved"
    # a market listed under "...-on-february-23" whose endDate is another day (seen once: september-1 -> 2025-08-01)
    assert classify_market("bitcoin", BTC_EV, {**m, "endDate": "2026-02-01T17:00:00Z"})["status"] == "slug_date"
    ev_y = {**BTC_EV, "slug": "bitcoin-above-on-february-23-2025"}
    assert classify_market("bitcoin", ev_y, m)["status"] == "slug_date"  # year in slug must match too


# ----------------------------------------------------------------------------- no lookahead


def test_quote_at_uses_last_point_before_cutoff():
    pts = [{"t": 1000, "p": 0.10}, {"t": 1060, "p": 0.11}, {"t": 1150, "p": 0.50}]
    assert quote_at(pts, 1200) == (0.11, 1060)  # 1150 > 1200 - 60
    assert quote_at(pts, 1200, max_age=60) is None  # 1060 is 140 s old


def test_quote_ignores_points_after_cutoff():
    t0 = HIST[-1]["t"]
    base = quote_at(HIST, t0)
    assert base is not None and base[1] <= t0 - 60
    future = [*HIST, {"t": t0 - 30, "p": 0.99}, {"t": t0 + 600, "p": 0.01}]
    assert quote_at(future, t0) == base
    changed = [dict(p) if p["t"] <= t0 - 60 else {"t": p["t"], "p": 1 - p["p"]} for p in HIST]
    assert quote_at(changed, t0) == base


def test_last_trade_before_ignores_future_and_converts_tokens():
    t0 = 1771829000
    lt = last_trade_before(TRADES, t0, YES_66K)
    assert lt is not None and lt["t"] <= t0 and lt["age"] == t0 - lt["t"]
    later = [*TRADES, {"timestamp": t0 + 1, "price": 0.99, "asset": YES_66K}]
    assert last_trade_before(later, t0, YES_66K) == lt
    no = last_trade_before([{"timestamp": 10, "price": 0.93, "asset": "NO"}], 20, YES_66K)
    assert no["yes_px"] == pytest.approx(0.07) and no["no_px"] == pytest.approx(0.93)


def test_history_alignment_on_real_sample_is_not_last_trade():
    al = history_alignment(HIST, TRADES, YES_66K)
    assert al["n"] > 20
    # 1m points are not copies of the previous trade nor of the last trade in the next minute
    assert al["match_prev_trade"] < 0.5 and al["match_last_in_bucket"] < 0.5
    assert al["half_cent_share"] > 0.2  # some half-cent values: a quote snapshot, not a trade print


def test_history_alignment_detects_a_last_trade_series():
    trades = [{"timestamp": 100 * i, "price": 0.1 + 0.01 * i, "asset": "Y"} for i in range(1, 20)]
    pts = [{"t": 100 * i + 5, "p": 0.1 + 0.01 * i} for i in range(1, 20)]
    al = history_alignment(pts, trades, "Y")
    assert al["match_prev_trade"] == 1.0


# ----------------------------------------------------------------------------- model


def test_digital_above_analytic():
    assert digital_above(100, 100, 0.5, 1.0) == pytest.approx(0.5 * math.erfc(0.25 / math.sqrt(2)), abs=1e-12)  # N(-0.25)
    assert digital_above(100, 100, 0.5, 1.0) == pytest.approx(0.40129, abs=1e-5)
    assert digital_above(200, 100, 0.2, 1 / 365) > 0.999999
    assert digital_above(100, 101, 0.0, 0.1) == 0.0
    lo, hi = digital_above(100, 105, 0.6, 0.5 / 365), digital_above(100, 95, 0.6, 0.5 / 365)
    assert lo < 0.5 < hi


def test_value_at_and_realized_vol_known_at_t0():
    s = [(10, 1.0), (20, 2.0), (30, 3.0)]
    assert value_at(s, 9) is None and value_at(s, 20) == 2.0 and value_at(s, 29) == 2.0
    r = 0.01
    closes = [(3600 * i, 100 * math.exp(r if i % 2 else 0.0)) for i in range(400)]
    t0 = 3600 * 300
    v = realized_vol(closes, t0, span_days=30, lookback_days=90)
    assert v == pytest.approx(r * math.sqrt(8760), rel=0.05)  # |log return| = r every bar
    wild = closes[:301] + [(3600 * i, 1e6) for i in range(301, 400)]
    assert realized_vol(wild, t0) == v  # data after t0 does not matter


# ----------------------------------------------------------------------------- test statistics


def test_calib_contract_maps_tails_to_longshot():
    lo = calib_contract(0.05, 0)
    assert lo["tail"] == "low" and lo["resid"] == pytest.approx(-0.05)
    hi = calib_contract(0.95, 1)
    assert hi["tail"] == "high" and hi["price"] == pytest.approx(0.05) and hi["resid"] == pytest.approx(-0.05)
    assert calib_contract(0.95, 0)["resid"] == pytest.approx(0.95)
    assert calib_contract(0.5, 1) is None and calib_contract(0.01, 0) is None


def test_no_trade_return_on_capital():
    win = no_trade_return(0.05, 0, half_spread=0.01)
    assert win["entry"] == pytest.approx(0.96) and win["ret"] == pytest.approx(1 / 0.96 - 1)
    assert no_trade_return(0.05, 1)["ret"] == pytest.approx(-1.0)
    assert no_trade_return(0.05, 0, last_no_px=0.97)["entry"] == pytest.approx(0.97)
    assert no_trade_return(0.05, 0, last_no_px=0.90)["entry"] == pytest.approx(0.96)
    fee = no_trade_return(0.05, 0, fee_rate=0.07, fee_exp=1.0)
    assert fee["fee"] == pytest.approx(0.07 * 0.96 * 0.04)
    assert fee["ret"] == pytest.approx(1 / (0.96 + 0.07 * 0.96 * 0.04) - 1)


def test_day_baskets_bootstrap_mde():
    d = day_baskets([("2026-01-02", 1.0), ("2026-01-01", 0.0), ("2026-01-01", 2.0)])
    assert d == [("2026-01-01", 1.0, 2), ("2026-01-02", 1.0, 1)]
    b = bootstrap_mean([0.3] * 50)
    assert b["mean"] == pytest.approx(0.3) and b["lo"] == pytest.approx(0.3) and b["hi"] == pytest.approx(0.3)
    b2 = bootstrap_mean([0.0, 1.0] * 100)
    assert b2["lo"] < 0.5 < b2["hi"]
    assert bootstrap_mean([0.0, 1.0] * 100) == b2  # seeded
    assert mde(0.2, 400) == pytest.approx(0.028)
    assert sd([1.0, 3.0]) == pytest.approx(math.sqrt(2))


# ----------------------------------------------------------------------------- provider (MockTransport, real samples)


def _pm(handler) -> Polymarket:  # noqa: ANN001
    t = httpx.MockTransport(handler)
    return Polymarket(
        gamma=Http("polymarket:gamma", "https://gamma.test", transport=t, retries=0),
        clob=Http("polymarket:clob", "https://clob.test", transport=t, retries=0),
        data=Http("polymarket:data", "https://data.test", transport=t, retries=0),
    )


async def test_events_all_walks_keyset_cursor_to_the_end():
    seen: list[str | None] = []

    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/events/keyset"
        cur = req.url.params.get("after_cursor")
        seen.append(cur)
        if cur is None:
            return httpx.Response(200, json={"events": [EVENTS[0]], "next_cursor": "c1"})
        return httpx.Response(200, json={"events": [EVENTS[1], EVENTS[0]]})  # duplicate id de-duplicated

    pm = _pm(h)
    f = await pm.events_all({"tag_slug": "multi-strikes", "closed": "true"})
    assert seen == [None, "c1"]
    assert f.data["complete"] is True and f.data["pages"] == 2 and len(f.data["rows"]) == 2
    await pm.aclose()


async def test_events_all_marks_incomplete_when_page_limit_hit():
    pm = _pm(lambda req: httpx.Response(200, json={"events": [EVENTS[0]], "next_cursor": "again"}))
    f = await pm.events_all({"series_id": "45"}, max_pages=3)
    assert f.data["complete"] is False and f.data["pages"] == 3
    await pm.aclose()


async def test_price_history_range_and_trades_params():
    calls: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path == "/prices-history":
            return httpx.Response(200, json={"history": HIST})
        if req.url.path == "/trades":
            return httpx.Response(200, json=TRADES[:5])
        return httpx.Response(404)

    pm = _pm(h)
    f = await pm.price_history_range(YES_66K, 1771828000, 1771830000, 1)
    assert len(f.data) == len(HIST)
    assert calls[-1].url.params["startTs"] == "1771828000" and calls[-1].url.params["fidelity"] == "1"
    t = await pm.trades("0xabc", limit=5, end=1771829000)
    assert len(t.data) == 5 and calls[-1].url.params["end"] == "1771829000" and "start" not in calls[-1].url.params
    with pytest.raises(ProviderError) as ei:
        await pm.trades("0xabc", offset=20_000)
    assert ei.value.reason == "config" and "20000" in ei.value.detail
    await pm.aclose()


async def test_trades_provider_failure_raises_provider_error():
    pm = _pm(lambda req: httpx.Response(502, text="down"))
    with pytest.raises(ProviderError) as ei:
        await pm.trades("0xabc", end=1)
    assert ei.value.reason == "backend_error"
    await pm.aclose()


def _verify_script():
    import importlib
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    return importlib.import_module("verify_pm_threshold")


@pytest.mark.parametrize(
    ("status", "permanent"),
    [(400, True), (404, True), (422, True), (403, True), (429, False), (500, False), (502, False)],
)
async def test_script_retry_classifies_http_errors_by_status(status, permanent):
    vs = _verify_script()
    pm = _pm(lambda req: httpx.Response(status, text="x"))
    with pytest.raises(ProviderError) as ei:
        await pm.trades("0xabc", end=1)
    assert vs._is_permanent(ei.value) is permanent
    await pm.aclose()


async def test_script_retry_does_not_repeat_permanent_errors():
    vs = _verify_script()
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        raise ProviderError("polymarket", "backend_error", "HTTP 400: bad offset")

    with pytest.raises(ProviderError):
        await vs._retry(boom, "t")
    assert calls == 1
    assert vs._is_permanent(ProviderError("polymarket", "config", "offset 20000 exceeds 10000")) is True
    assert vs._is_permanent(ProviderError("polymarket", "network", "timeout: ReadTimeout")) is False
