"""Cycle regime gauge (IDEA-20261028-btc-cycle-regime-gauge, step 1): analytics/cycle_regime.py and the cycle_regime tool."""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import httpx
import pytest

from conftest import FakeEx
from test_server import call
from tradedesk.analytics import cycle_regime as cr
from tradedesk.analytics.markup import rsi
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_futures import BinanceFutures
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

DAY = cr.DAY_MS
H = 3_600_000
NOW = datetime(2026, 10, 28, 14, 37, tzinfo=UTC)
END = int(NOW.timestamp() * 1000) // DAY * DAY  # 2026-10-28 00:00 UTC: the last closed day is 27.10
N = 560  # daily bars before END
FUNDING_RATE = 0.0000575  # per 8 h -> 0.0630 a year, the card's +6.3 %
BASIS = -0.00047  # the card's 7-day basis
PREMIUM = -0.0004

# The card's snapshot of 28.10 (Binance, Claude): spot 80 737, SMA365 77 407, 365-day high 113 644, basis7 -0.047 %,
# funding7 +6.3 %/yr, RSI14 44.8. The card does not give the 30-day low; the day was not at it (it concluded
# «накопичення»), so dist_30d_low is set to a plain positive 3 %.
CARD = {
    "price_to_sma": 80737 / 77407 - 1,
    "drawdown_365d": 80737 / 113644 - 1,
    "basis7": -0.00047,
    "funding7": 0.063,
    "rsi14": 44.8,
    "dist_30d_low": 0.03,
    "sma_slope_30d": -0.01,
}


# ------------------------------------------------------------------------------------------------ the rules alone


def test_card_sample_of_28_10_is_accumulation_and_growth_is_nearest():
    assert CARD["price_to_sma"] == pytest.approx(0.043, abs=5e-4) and CARD["drawdown_365d"] == pytest.approx(-0.29, abs=5e-3)
    assert cr.classify(CARD) == "accumulation"
    ev = cr.evaluate(CARD)
    assert ev["accumulation"]["met"] == 4 and ev["bottom"]["met"] == 1 and ev["overheat"]["met"] == 0
    trans = cr.transitions(CARD, "accumulation")
    assert [t["state"] for t in trans] == ["growth", "decline", "bottom", "overheat"]
    assert all(t["reaches"] for t in trans)
    near = trans[0]  # funding7 from 6.3 % to 8 % ends accumulation while the price stays above SMA365
    assert near["n_changes"] == 1 and near["changes"][0]["component"] == "funding7"
    assert near["changes"][0]["conditions"] == [{"op": ">=", "threshold": 0.08}] and near["changes"][0]["to"] == 0.08
    bottom = next(t for t in trans if t["state"] == "bottom")  # drawdown, RSI and funding must all move
    assert {r["component"] for r in bottom["changes"]} == {"drawdown_365d", "rsi14", "funding7"}
    line = cr.line_ua(CARD, "accumulation", near)
    # the tail says what ends the state, never which state comes next (the brief must not read like a forecast)
    assert line == "Режим циклу: накопичення (базис −0.05 %, funding 6 % < 11 %, +4 % над SMA365); зміниться, якщо: funding7 ≥ 8 %"
    assert not any(cr.STATE_UA[s] in line for s in cr.STATES if s != "accumulation")


def test_card_sample_at_its_30_day_low_or_funding_at_8_pct_is_growth():
    assert cr.classify(CARD | {"dist_30d_low": 0.0}) == "growth"  # the close equals the 30-day low: at the low
    assert cr.classify(CARD | {"funding7": 0.08}) == "growth"  # strict: 8 % is not < 8 %
    assert cr.classify(CARD | {"basis7": 0.0}) == "growth"  # strict: a zero basis is not < 0
    assert cr.classify(CARD | {"price_to_sma": 0.10}) == "accumulation"  # 10 % above SMA365 still counts
    assert cr.classify(CARD | {"price_to_sma": -0.10}) == "accumulation"
    assert cr.classify(CARD | {"price_to_sma": -0.1001}) == "decline"


def test_bottom_needs_all_four_and_takes_precedence_over_accumulation():
    both = {"price_to_sma": -0.05, "drawdown_365d": -0.35, "basis7": -0.0002, "funding7": 0.0, "rsi14": 35.0, "dist_30d_low": 0.01}
    ev = cr.evaluate(both)
    assert ev["bottom"]["holds"] and ev["accumulation"]["holds"]
    assert cr.classify(both) == "bottom"  # exact thresholds count: drawdown 35 %, funding 0, RSI 35
    assert cr.classify(both | {"rsi14": 35.1}) == "accumulation"
    assert cr.classify(both | {"funding7": 0.0001, "price_to_sma": -0.3}) == "decline"
    trans = cr.transitions(both, "bottom")
    acc = next(t for t in trans if t["state"] == "accumulation")
    assert acc["n_changes"] == 1 and acc["reaches"]  # one bottom condition must give way, but not the shared basis < 0
    assert acc["changes"][0]["component"] in {"drawdown_365d", "rsi14", "funding7"}


def test_overheat_is_any_three_of_four_with_inclusive_thresholds():
    hot = {"price_to_sma": 0.30, "drawdown_365d": -0.02, "basis7": 0.0004, "funding7": 0.20, "rsi14": 70.0, "dist_30d_low": 0.2}
    assert cr.classify(hot) == "overheat"  # 1.3 x SMA365, 20 %/yr and RSI 70 count; basis 0.04 % does not
    assert cr.evaluate(hot)["overheat"]["met"] == 3
    assert cr.classify(hot | {"basis7": 0.0005}) == "overheat"  # 0.05 % is not > 0.05 %, still 3 of 4
    assert cr.classify(hot | {"rsi14": 69.9}) == "growth"  # 2 of 4
    trans = cr.transitions(hot | {"rsi14": 69.9}, "growth")
    assert trans[0]["state"] == "overheat" and trans[0]["n_changes"] == 1


def test_undecided_without_funding_and_parse_closes():
    no_funding = CARD | {"funding7": None}
    assert cr.classify(no_funding) is None and cr.transitions(no_funding, None) == []
    assert cr.evaluate(no_funding)["accumulation"]["holds"] is None
    # deep drawdown + positive basis: bottom and accumulation are false whatever funding is, overheat needs funding
    assert cr.classify(no_funding | {"basis7": 0.0001, "price_to_sma": -0.3, "drawdown_365d": -0.5}) == "decline"
    assert cr.line_ua(no_funding, None, None) is None and cr.classify(None) is None
    rows = [[1, "1", "1", "1", "80000.5"], [2, "0", "0", "0", "-0.0004"], [3, "x", "x", "x", "nan"], ["bad"], None]
    assert cr.parse_closes(rows) == {1: 80000.5}
    assert cr.parse_closes(rows, positive=False) == {1: 80000.5, 2: -0.0004}


# ------------------------------------------------------------------------------------------- from daily series


def _close(j: int) -> float:
    """A year like the card's: a peak near 113k early in the 365-day window, a low near 58k, a recovery to ~80k."""
    if j < 195:
        x = 50000.0 + 300 * j
    elif j < 200:
        x = 113000.0 + 100 * (j - 195)
    elif j < 380:
        x = 113400.0 - 305 * (j - 200)
    elif j < N - 10:
        x = 58500.0 + 115 * (j - 380)
    else:
        x = 78000.0 + 300 * (j - (N - 10))
    return x + (250.0 if j % 2 else -250.0) * (j >= 380)


SPOT = {END - (N - j) * DAY: _close(j) for j in range(N)}
PERP = {t: c * (1 + BASIS) for t, c in SPOT.items() if t >= END - 120 * DAY}
PREM = {t: (0.0001 if t == END - 3 * DAY else PREMIUM) for t in SPOT if t >= END - 120 * DAY}
FUNDING = [(END - i * 8 * H + 3, FUNDING_RATE) for i in range(0, 3 * 120)]  # Binance stamps drift a few ms


def test_components_from_daily_series_match_plain_formulas():
    out = cr.cycle_regime(SPOT, PERP, FUNDING, PREM, NOW, history_days=90)
    closes = [SPOT[t] for t in sorted(SPOT)]
    c = out["components"]
    sma = statistics.fmean(closes[-365:])
    assert out["date"] == "2026-10-27" and out["close_utc"] == "2026-10-28T00:00:00+00:00"
    assert c["close"] == closes[-1] and c["sma365"] == pytest.approx(sma, rel=1e-12)
    assert c["price_to_sma"] == pytest.approx(closes[-1] / sma - 1, abs=1e-12)
    assert c["sma_slope_30d"] == pytest.approx(sma / statistics.fmean(closes[-395:-30]) - 1, abs=1e-12)
    assert c["drawdown_365d"] == pytest.approx(closes[-1] / max(closes[-365:]) - 1, abs=1e-12)
    assert c["dist_30d_low"] == pytest.approx(closes[-1] / min(closes[-30:-1]) - 1, abs=1e-12)
    assert c["rsi14"] == pytest.approx(rsi(closes), abs=1e-9)
    assert c["basis7"] == pytest.approx(BASIS, abs=1e-12) and c["basis_days"] == 7
    assert c["basis_last"] == pytest.approx(BASIS, abs=1e-12)
    # 21 settlements in (close - 7 d, close]: the one stamped at 00:00:00.003 of the closing day counts
    assert c["funding_settlements"] == 21 and c["funding_interval_hours"] == pytest.approx(8.0)
    assert c["funding7"] == pytest.approx(FUNDING_RATE * 1095, abs=1e-12)
    assert c["premium7"] == pytest.approx((6 * PREMIUM + 0.0001) / 7, abs=1e-12) and c["premium_negative_days"] == 6
    assert "найближче" not in out["line_ua"] and "зміниться, якщо: " in out["line_ua"]
    # like the card: a few % above SMA365, about -29 % from the year's high, negative basis, low funding
    assert 0 < c["price_to_sma"] < 0.10 and -0.35 < c["drawdown_365d"] < -0.25
    assert out["state"] == "accumulation" and out["state_ua"] == "накопичення" and out["undecided_by"] == []
    assert out["trend"] == "growth" and out["trend_confirmed"] is False  # above SMA365 while SMA365 still falls
    assert out["closest"] == out["transitions"][0] and out["line_ua"].startswith("Режим циклу: накопичення (базис −0.05 %")
    hist = out["history"]
    assert len(hist) == 90 and hist[-1] == {"date": "2026-10-27", "state": "accumulation", "close": closes[-1]}
    assert hist[0]["date"] == "2026-07-30" and {h["state"] for h in hist} <= set(cr.STATES)
    # history entry k is the same computation as if `now` were k days earlier
    back = cr.cycle_regime(SPOT, PERP, FUNDING, PREM, datetime.fromtimestamp((END - 40 * DAY) / 1000, UTC))
    assert hist[-41]["state"] == back["state"] and hist[-41]["date"] == back["date"]


def test_missing_last_bar_or_short_history_gives_no_state():
    spot = dict(SPOT)
    del spot[END - DAY]
    out = cr.cycle_regime(spot, PERP, FUNDING, PREM, NOW, history_days=2)
    assert out["state"] is None and out["components"] is None and out["undecided_by"] == ["close"]
    assert out["history"][-1]["state"] is None and out["history"][0]["state"] is not None
    short = {t: c for t, c in SPOT.items() if t >= END - 300 * DAY}  # under 365 closes: no SMA365, no drawdown
    out = cr.cycle_regime(short, PERP, FUNDING, PREM, NOW)
    assert out["state"] is None and out["components"]["sma365"] is None and out["trend"] is None
    assert "price_to_sma" in out["undecided_by"] and out["history"] == []


# ------------------------------------------------------------------------------------------------------ the tool


def _kline(t: int, close: float) -> list:
    return [t, f"{close:.2f}", f"{close:.2f}", f"{close:.2f}", f"{close:.8f}", "1.0", t + DAY - 1, "1.0", 1, "0.5", "0.5", "0"]


def _klines(rows: dict[int, float], req: httpx.Request) -> httpx.Response:
    p = req.url.params
    s, e, lim = int(p["startTime"]), int(p["endTime"]), int(p["limit"])
    assert p["interval"] == "1d" and p["symbol"] == "BTCUSDT"
    return httpx.Response(200, json=[_kline(t, rows[t]) for t in sorted(rows) if s <= t <= e][:lim])


def _spot_handler(req: httpx.Request) -> httpx.Response:
    return _klines(SPOT, req) if req.url.path == "/api/v3/klines" else httpx.Response(404)


def _futures_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/fapi/v1/klines":
        return _klines(PERP, req)
    if req.url.path == "/fapi/v1/premiumIndexKlines":
        return _klines(PREM, req)
    return httpx.Response(404, text="not mocked")


class CycleEx(FakeEx):
    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        assert self.id == "binanceusdm" and symbol == "BTC/USDT:USDT"
        until = (params or {}).get("until", END + DAY)
        rows = [{"timestamp": t, "fundingRate": r} for t, r in sorted(FUNDING)]
        return [x for x in rows if (since is None or x["timestamp"] >= since) and x["timestamp"] <= until][:limit]


def _down(req: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="maintenance")


class DownEx(FakeEx):
    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        raise ccxt.NetworkError("venue down")


def _rt(spot=_spot_handler, futures=_futures_handler, ex=CycleEx) -> Runtime:
    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0),
        transports={"binance_spot": httpx.MockTransport(spot), "binance_futures": httpx.MockTransport(futures)},
        exchange_factory=ex,
    )
    rt.now = lambda: NOW
    return rt


async def test_cycle_regime_tool_happy_path():
    rt = _rt()
    srv, _ = build_server(rt)
    env = await call(srv, "cycle_regime")
    await rt.aclose()
    assert env["ok"] and env["missing"] == []
    d = env["data"]
    ref = cr.cycle_regime(SPOT, PERP, FUNDING, PREM, NOW, history_days=90)
    assert d["base"] == "BTC" and d["history_days"] == 90 and d["state"] == ref["state"] == "accumulation"
    assert d["components"]["basis7"] == pytest.approx(BASIS, abs=1e-6) and d["components"]["funding7"] == pytest.approx(0.063, abs=1e-4)
    assert d["components"]["premium7"] == pytest.approx(ref["components"]["premium7"], abs=1e-6)
    assert d["components"]["sma365"] == pytest.approx(ref["components"]["sma365"], abs=0.1)
    assert [h["state"] for h in d["history"]] == [h["state"] for h in ref["history"]] and len(d["history"]) == 90
    assert d["closest"]["state"] == ref["closest"]["state"] and d["line_ua"] == ref["line_ua"]
    assert d["rules"]["accumulation"]["holds"] is True
    assert any("not a signal" in w and "NOT backtested" in w and "risk-neutral" in w for w in env["warnings"])
    assert {e["provider"] for e in env["evidence"]} >= {"binance_spot", "binance_futures", "ccxt:binanceusdm"}


async def test_cycle_regime_tool_provider_failures_land_in_missing():
    rt = _rt(futures=_down, ex=DownEx)  # perp, premium index and funding down: components without them, no state
    srv, _ = build_server(rt)
    env = await call(srv, "cycle_regime", base="BTC", history_days=0)
    await rt.aclose()
    assert env["ok"] and {m["field"] for m in env["missing"]} == {"perp_klines", "premium_index", "funding"}
    d = env["data"]
    assert d["state"] is None and set(d["undecided_by"]) >= {"basis7", "funding7"} and d["line_ua"] is None
    assert d["components"]["sma365"] is not None and d["trend"] == "growth" and d["history"] == []
    assert any(w.startswith("State undecided") for w in env["warnings"])

    rt = _rt(spot=_down)  # no spot: no state at all, the envelope still comes back
    srv, _ = build_server(rt)
    env = await call(srv, "cycle_regime")
    await rt.aclose()
    assert env["ok"] is False and env["data"] is None and "spot_klines" in {m["field"] for m in env["missing"]}


async def test_cycle_regime_tool_rejects_bad_base_without_requests():
    seen: list = []

    def spy(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(404)

    rt = _rt(spot=spy, futures=spy)
    srv, _ = build_server(rt)
    env = await call(srv, "cycle_regime", base="BTC/USDT")
    await rt.aclose()
    assert not env["ok"] and env["missing"][0]["field"] == "base" and env["missing"][0]["reason"] == "invalid_argument"
    assert seen == []


# ------------------------------------------------------------ real Binance responses of 28.10 (tests/fixtures)

# Captured from this server on 2026-10-28 ~19:58 UTC (the fixture's _captured_utc and _note): raw /api/v3/klines, /fapi/v1/klines and
# /fapi/v1/premiumIndexKlines daily rows and /fapi/v1/fundingRate rows, the windows the tool asks for (history_days=90).
FX = json.loads((Path(__file__).parent / "fixtures" / "cycle_regime_btc_1d_2026-10-28.json").read_text(encoding="utf-8"))
FX_NOW = datetime.fromisoformat(FX["_captured_utc"].replace("Z", "+00:00"))
FX_ROWS = {
    "/api/v3/klines": FX["binance_spot_klines_1d"],
    "/fapi/v1/klines": FX["binance_perp_klines_1d"],
    "/fapi/v1/premiumIndexKlines": FX["binance_premium_index_klines_1d"],
}


def serve_daily(req: httpx.Request, shift_ms: int = 0) -> httpx.Response | None:
    """The fixture's BTCUSDT daily rows with Binance's paging semantics (open time in [startTime, endTime], first
    `limit`), moved `shift_ms` earlier; None for any other request (the caller falls back to its own handler)."""
    rows, p = FX_ROWS.get(req.url.path), req.url.params
    if rows is None or p.get("interval") != "1d" or p.get("symbol") != "BTCUSDT":
        return None
    s, e, lim = int(p["startTime"]), int(p["endTime"]), int(p["limit"])
    moved = [[r[0] - shift_ms, *r[1:6], r[6] - shift_ms, *r[7:]] for r in rows]
    return httpx.Response(200, json=[r for r in moved if s <= r[0] <= e][:lim])


def _fx_handler(req: httpx.Request) -> httpx.Response:
    return serve_daily(req) or httpx.Response(404, text="not mocked")


class FixtureEx(FakeEx):
    """binanceusdm funding history from the fixture's raw /fapi/v1/fundingRate rows, in ccxt's shape."""

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        assert self.id == "binanceusdm" and symbol == "BTC/USDT:USDT"
        until = (params or {}).get("until")
        rows = [{"timestamp": int(r["fundingTime"]), "fundingRate": float(r["fundingRate"])} for r in FX["binance_funding_rate"]]
        return [x for x in rows if (since is None or x["timestamp"] >= since) and (until is None or x["timestamp"] <= until)][:limit]


async def test_cycle_regime_tool_on_real_binance_responses_of_28_10():
    rt = _rt(spot=_fx_handler, futures=_fx_handler, ex=FixtureEx)
    rt.now = lambda: FX_NOW
    srv, _ = build_server(rt)
    env = await call(srv, "cycle_regime", base="BTC", history_days=90)
    await rt.aclose()
    assert env["ok"] and env["missing"] == [] and not any("incomplete" in w for w in env["warnings"])
    d = env["data"]
    c = d["components"]
    assert d["date"] == "2026-10-27" and d["state"] == "accumulation" and d["undecided_by"] == []
    assert d["trend"] == "growth" and d["trend_confirmed"] is False  # above SMA365 while SMA365 still falls
    # the same numbers as the critic's live call of 28.10 (docs/reviews/2026-10-28-cycle-regime-code.md); the tool
    # rounds to 6 significant digits (round_tree)
    assert c["close"] == pytest.approx(80909.8, abs=0.1) and c["sma365"] == pytest.approx(77494.7, abs=0.1)
    assert c["price_to_sma"] == pytest.approx(0.0441, abs=1e-4) and c["sma_slope_30d"] == pytest.approx(-0.0325, abs=1e-4)
    assert c["high_365d"] == pytest.approx(112898.45, abs=0.1) and c["drawdown_365d"] == pytest.approx(-0.2833, abs=1e-4)
    assert c["dist_30d_low"] == pytest.approx(0.0095, abs=1e-4) and c["rsi14"] == pytest.approx(45.49, abs=0.01)
    assert c["basis7"] == pytest.approx(-0.000465, abs=1e-6) and c["basis_days"] == 7
    assert c["funding7"] == pytest.approx(0.0635, abs=1e-4) and c["funding_settlements"] == 21
    assert c["funding_interval_hours"] == pytest.approx(8.0)
    assert c["premium7"] == pytest.approx(-0.000435, abs=1e-6) and c["premium_negative_days"] == 7
    assert d["rules"]["accumulation"]["met"] == 4 and d["rules"]["bottom"]["met"] == 1
    # one step from the edge: closing at the 30-day low would end accumulation (the tool names the state, the line not)
    assert d["closest"]["state"] == "growth" and [r["component"] for r in d["closest"]["changes"]] == ["dist_30d_low"]
    assert d["line_ua"] == (
        "Режим циклу: накопичення (базис −0.05 %, funding 6 % < 11 %, +4 % над SMA365); зміниться, якщо: ціна на 30-денному мінімумі"
    )
    hist = d["history"]
    assert len(hist) == 90 and hist[-1]["state"] == "accumulation" and hist[0]["date"] == "2026-07-30"
    assert {s: sum(h["state"] == s for h in hist) for s in ("decline", "accumulation", "growth")} == {
        "decline": 35, "accumulation": 50, "growth": 5,
    }  # fmt: skip


async def test_premium_index_klines_provider_on_the_real_sample():
    seen: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.url.path, dict(req.url.params)))
        return serve_daily(req) or httpx.Response(404, text="not mocked")

    bf = BinanceFutures(
        Http("binance_futures", "https://fapi.test", rate=1000, burst=1000, retries=0, transport=httpx.MockTransport(handler))
    )
    end, start = FX["end_ms"], FX["short_start_ms"]
    f = await bf.premium_index_klines_range("BTCUSDT", "1d", start, end)
    assert f.data["complete"] and f.data["rows"] == FX["binance_premium_index_klines_1d"] and len(f.data["rows"]) == 98
    path, params = seen[0]
    assert path == "/fapi/v1/premiumIndexKlines" and len(seen) == 1  # 98 rows < 1500: one page
    assert params == {"symbol": "BTCUSDT", "interval": "1d", "startTime": str(start), "endTime": str(end - 1), "limit": "1500"}
    prem = cr.parse_closes(f.data["rows"], positive=False)  # signed decimals: negative closes must survive
    assert prem[end - DAY] == pytest.approx(-0.00050349, abs=1e-12) and sum(v < 0 for v in prem.values()) > 0
    assert cr.parse_closes(f.data["rows"]) != prem  # the default (positive only) would drop them
    with pytest.raises(ProviderError):
        await bf.premium_index_klines("BTCUSDT", "3h", start, end)  # not a Binance interval
    await bf.http.aclose()
    bf = BinanceFutures(
        Http("binance_futures", "https://fapi.test", rate=1000, burst=1000, retries=0, transport=httpx.MockTransport(_down))
    )
    with pytest.raises(ProviderError):
        await bf.premium_index_klines_range("BTCUSDT", "1d", start, end)
    await bf.http.aclose()
