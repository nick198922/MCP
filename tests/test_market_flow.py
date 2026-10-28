"""market_flow (IDEA-20261028-market-flow-reader, step 1): analytics on analytic references, provider paging with
httpx.MockTransport, and the tool on real responses captured 2026-10-28 (tests/fixtures/market_flow_btc_1h_2026-10-28.json;
Coinalyze liquidations and Bybit funding are synthetic, see `_coinalyze` and `FlowEx`). No network."""

from __future__ import annotations

import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import httpx
import pytest

from conftest import FakeEx
from test_server import call
from tradedesk.analytics import market_flow as mf
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.core.http import Http
from tradedesk.providers.binance_spot import BinanceSpot
from tradedesk.providers.bybit import Bybit
from tradedesk.providers.coinbase import Coinbase
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

H = 3_600_000
FX = json.loads((Path(__file__).parent / "fixtures" / "market_flow_btc_1h_2026-10-28.json").read_text())
END = FX["end_ms"]


# ----------------------------------------------------------------------------- analytics


def _kl(t: int, close: float, qv: float, tbq: float) -> list:
    return [t, "1", "1", "1", str(close), "1", t + H - 1, str(qv), 10, "0.5", str(tbq), "0"]


def test_klines_delta_is_aggressor_buys_minus_sells_and_bad_rows_drop():
    rows = [_kl(0, 100, 100.0, 70.0), _kl(H, 101, 50.0, 10.0), _kl(2 * H, 1, 10.0, 11.0), [3 * H, "x"], None, _kl(4 * H, "nan", 1, 1)]
    out = mf.parse_binance_klines(rows)
    assert set(out) == {0, H}  # taker buy above volume, short row, None and NaN close dropped
    assert out[0]["delta"] == pytest.approx(40.0) and out[H]["delta"] == pytest.approx(-30.0)


def test_klines_delta_on_a_real_row():
    row = FX["binance_spot_klines"][-1]
    d = mf.parse_binance_klines([row])[int(row[0])]
    assert d["delta"] == pytest.approx(2 * float(row[10]) - float(row[7]))
    assert d["close"] == float(row[4])


def test_coinbase_candles_and_points_and_liquidations_parse():
    cb = mf.parse_coinbase_candles([[3600, 1, 2, 1.5, 1.8, 10], [7200, 1, 2, 1.5, -1, 10], ["x"], [10800, 1, 2, 1, float("inf"), 1]])
    assert cb == {3_600_000: 1.8}
    pts = mf.points(
        [{"timestamp": "20", "v": "2"}, {"timestamp": 10, "v": 1}, {"timestamp": 30, "v": 5}, {"v": 1}, {"timestamp": 40, "v": "x"}],
        "timestamp",
        "v",
        hi=4,
    )
    assert pts == [(10, 1.0), (20, 2.0)]
    liq = mf.liquidation_bars([{"t": 3600, "l": 5, "s": "2"}, {"t": 7200, "l": -1, "s": 0}, {"t": "bad"}])
    assert liq == {3_600_000: (5.0, 2.0)}


def test_asof_respects_staleness_and_funding_interval_is_the_median_spacing():
    pts = [(0, 1.0), (H, 2.0), (5 * H, 3.0)]
    assert mf.asof(pts, H, H) == 2.0 and mf.asof(pts, H + 1, H) == 2.0
    assert mf.asof(pts, 3 * H + 1, H) is None  # 2h+ old
    assert mf.asof(pts, -1, H) is None and mf.asof([], 0, H) is None
    assert mf.funding_interval_hours([(i * 8 * H, 1e-4) for i in range(5)]) == 8.0
    assert mf.funding_interval_hours([(0, 1e-4)]) is None
    assert mf.funding_interval_hours([(0, 1e-4), (48 * H, 1e-4)]) is None  # 48 h is not a funding interval


def test_premium_adjusted_is_raw_minus_trailing_median():
    n = mf.PREMIUM_MIN_OBS + 10
    grid = [i * H for i in range(n)]
    spot = {t: {"close": 100.0} for t in grid}
    coinbase = {t: 100.0 * (1.001 if i < mf.PREMIUM_MIN_OBS else 1.003) for i, t in enumerate(grid)}
    raw, adj = mf.premium_series(coinbase, spot, grid)
    assert raw[0] == pytest.approx(0.001) and raw[-1] == pytest.approx(0.003)
    assert all(a is None for a in adj[: mf.PREMIUM_MIN_OBS])  # fewer than 7 days before the bar
    assert adj[mf.PREMIUM_MIN_OBS] == pytest.approx(0.002)  # 0.003 - median of 168 x 0.001
    assert adj[-1] == pytest.approx(0.002)  # 10 values of 0.003 among 177: the median is still 0.001
    raw2, _ = mf.premium_series({}, spot, grid)
    assert all(v is None for v in raw2)


def _row(ts: int, **kw) -> dict:
    base = {f: None for f in (*mf._SUM_FIELDS, *mf._LEVEL_FIELDS)}
    return {"ts": ts, **base, **kw}


def test_hourly_rows_sum_oi_only_when_every_venue_has_it_and_liquidations_stop_at_last_published():
    grid = [0, H, 2 * H]
    spot = {t: {"close": 100.0, "quote_volume": 10.0, "taker_buy_quote": 6.0, "delta": 2.0} for t in grid}
    perp = {t: {"close": 101.0, "quote_volume": 20.0, "taker_buy_quote": 8.0, "delta": -4.0} for t in grid}
    oi = {"binance": [(H, 10.0), (2 * H, 11.0), (3 * H, 12.0)], "bybit": [(H, 5.0), (3 * H, 6.0)]}  # bybit misses 2H
    funding = {"binance": ([(0, 1e-4)], 8.0), "bybit": ([(0, 2e-4)], 8.0)}
    liq = {"binance": {0: (3.0, 1.0)}, "bybit": {H: (1.0, 1.0)}}
    ls = {"binance": [(H, 0.6)], "bybit": [(H, 0.7)]}
    rows = mf.hourly_rows(grid, spot=spot, perp=perp, oi=oi, funding=funding, liquidations=liq, long_share=ls, premium={}, premium_adj={})
    assert rows[0]["oi_coins"] == 15.0 and rows[0]["oi_usd"] == pytest.approx(15.0 * 101.0)
    assert rows[1]["oi_coins"] == pytest.approx(16.0)  # bybit's 1H snapshot is 1 h old at the 2H close: still used
    assert rows[2]["oi_coins"] == 18.0
    assert rows[0]["funding_annualized"] == pytest.approx((1e-4 + 2e-4) / 2 * 1095)
    assert rows[0]["liq_long"] == 3.0 and rows[0]["liq_short"] == 1.0  # bybit has 0 at 0 (before its last row)
    assert rows[1]["liq_long"] == 1.0  # binance's last row is at 0: its 1H is unknown, bybit's counts
    assert rows[2]["liq_long"] is None  # after every venue's last published hour
    assert rows[0]["long_share"] == pytest.approx(0.65) and rows[0]["price"] == 100.0


def test_hourly_rows_oi_total_is_none_when_a_venue_has_no_snapshot():
    grid = [0]
    oi = {"binance": [(H, 10.0)], "bybit": [(-10 * H, 5.0)]}  # bybit too old
    rows = mf.hourly_rows(grid, spot={}, perp={}, oi=oi, funding={}, liquidations={}, long_share={}, premium={}, premium_adj={})
    assert rows[0]["oi_coins"] is None and rows[0]["price"] is None and rows[0]["oi_usd"] is None


def test_resample_sums_flows_takes_last_level_and_drops_incomplete_buckets():
    rows = [_row(i * H, spot_delta=float(i), price=100.0 + i, oi_coins=10.0 + i) for i in range(10)]
    rows[5]["spot_delta"] = None
    out = mf.resample(rows, 4)
    assert [r["ts"] for r in out] == [0, 4 * H]  # hours 8-9 make an incomplete bucket
    assert out[0]["spot_delta"] == 0 + 1 + 2 + 3 and out[1]["spot_delta"] == 4 + 6 + 7
    assert out[0]["price"] == 103.0 and out[1]["oi_coins"] == 17.0
    mf.add_cumulative(out)
    assert out[1]["spot_cvd"] == 6 + 17 and out[0]["perp_cvd"] is None


def _window_rows(price_end: float, oi_end: float, spot: float, perp: float, liq=(None, None), fund=None) -> list[dict]:
    rows = [_row(0, price=100.0, oi_coins=1000.0, long_share=0.6)]
    for i in range(1, 5):
        last = i == 4
        rows.append(
            _row(
                i * H,
                price=price_end if last else 100.0,
                oi_coins=oi_end if last else 1000.0,
                spot_delta=spot / 4,
                spot_volume=100.0,
                perp_delta=perp / 4,
                perp_volume=1000.0,
                liq_long=None if liq[0] is None else liq[0] / 4,
                liq_short=None if liq[1] is None else liq[1] / 4,
                funding_annualized=fund,
                long_share=0.62 if last else 0.6,
            )
        )
    return rows


def test_window_summary_quadrant_and_sentence_new_longs_without_spot():
    s = mf.window_summary(_window_rows(102.0, 1050.0, spot=-40.0, perp=+200.0), 4)
    assert s["price_change"] == pytest.approx(0.02) and s["oi_change"] == pytest.approx(0.05)
    assert s["spot_cvd"] == pytest.approx(-40.0) and s["perp_volume"] == pytest.approx(4000.0)
    assert s["long_share_change"] == pytest.approx(0.02)
    q = mf.quadrant(s)
    assert (q["price"], q["oi"], q["perp_flow"], q["spot_flow"]) == ("up", "up", "buy", "sell")
    assert q["regime"] == "new_longs" and q["patterns"] == ["leverage_without_spot"]
    text = mf.describe_ua(s, q)
    assert text.startswith("За 4 год: ціна +2.0 %, OI +5.0 % (у монетах); ф'ючерси купують")
    assert "спот продає" in text and text.endswith("ріст на нових позиціях (плече); ріст на плечі без спот-попиту (крихкий).")
    assert mf.window_summary(_window_rows(102.0, 1050.0, 0, 0), 5) is None  # needs hours + 1 rows


def test_quadrant_flush_and_shorts_into_spot_bid_and_flat_bands():
    s = mf.window_summary(_window_rows(97.0, 950.0, spot=0.0, perp=-200.0, liq=(10.0, 2.0)), 4)
    q = mf.quadrant(s)
    assert q["regime"] == "long_unwind" and q["patterns"] == ["flush"] and q["spot_flow"] == "balanced"
    s = mf.window_summary(_window_rows(100.05, 1001.0, spot=+40.0, perp=-200.0, fund=-0.01), 4)
    q = mf.quadrant(s)
    assert q["price"] == "flat" and q["oi"] == "flat" and q["regime"] == "oi_flat"
    assert q["patterns"] == ["shorts_into_spot_bid"]
    s = mf.window_summary(_window_rows(100.05, 1010.0, spot=0.0, perp=0.0), 4)
    assert mf.quadrant(s)["regime"] == "build_up"
    assert mf.quadrant(None) is None and mf.describe_ua(None, None) is None


def test_formatting_and_rounding_and_venue_parsing():
    assert mf.usd_ua(137_000_000) == "+137 млн $" and mf.usd_ua(-1_234_000_000) == "−1.23 млрд $" and mf.usd_ua(-850_000) == "−850 тис $"
    assert mf.pct_ua(-0.0123) == "−1.2 %" and mf.pct_ua(0.0) == "+0.0 %"
    assert mf.round_tree({"a": [1234.5678, 0.123456789, float("nan")], "b": 3}) == {"a": [1234.6, 0.123457, None], "b": 3}
    assert mf.parse_venues(None) == (["binance", "bybit"], [])
    assert mf.parse_venues(["Bybit", "bybit", "okx"]) == (["bybit"], ["okx"])


# ----------------------------------------------------------------------------- providers (MockTransport)


def _http(name: str, handler, base: str = "https://x.test") -> Http:
    return Http(name, base, rate=1000, burst=1000, retries=0, transport=httpx.MockTransport(handler))


def _synthetic_klines(req: httpx.Request, first: int = 0, n: int = 5000, fail_after: int | None = None, calls: list | None = None):
    if calls is not None:
        calls.append(dict(req.url.params))
        if fail_after is not None and len(calls) > fail_after:
            return httpx.Response(503, text="down")
    p = req.url.params
    s, e, lim = int(p["startTime"]), int(p["endTime"]), int(p["limit"])
    rows = [_kl(first + i * H, 100, 10, 5) for i in range(n) if s <= first + i * H <= e][:lim]
    return httpx.Response(200, json=rows)


async def test_spot_klines_range_walks_pages_and_flags_incomplete():
    calls: list = []
    sp = BinanceSpot(_http("binance_spot", lambda r: _synthetic_klines(r, calls=calls)))
    f = await sp.klines_range("BTCUSDT", "1h", 0, 2500 * H)
    assert len(f.data["rows"]) == 2500 and f.data["complete"] and len(calls) == 3
    assert [int(r[0]) for r in f.data["rows"]] == [i * H for i in range(2500)] and f.evidence.args["pages"] == 3
    f = await sp.klines_range("BTCUSDT", "1h", 0, 2500 * H, max_pages=2)
    assert len(f.data["rows"]) == 2000 and not f.data["complete"]
    calls.clear()
    sp = BinanceSpot(_http("binance_spot", lambda r: _synthetic_klines(r, fail_after=1, calls=calls)))
    f = await sp.klines_range("BTCUSDT", "1h", 0, 2500 * H)
    assert len(f.data["rows"]) == 1000 and not f.data["complete"]  # second page failed: rows so far are kept
    sp = BinanceSpot(_http("binance_spot", lambda r: httpx.Response(503, text="down")))
    with pytest.raises(ProviderError):
        await sp.klines_range("BTCUSDT", "1h", 0, 10 * H)
    with pytest.raises(ProviderError):
        await sp.klines("BTCUSDT", "3h", 0, H)


async def test_paging_skips_malformed_rows():
    odd = json.dumps([{"t": 1}, "x", None, "INF", _kl(0, 100, 10, 5), _kl(H, 100, 10, 5)]).replace('"INF"', "[Infinity]")
    sp = BinanceSpot(_http("binance_spot", lambda r: httpx.Response(200, content=odd, headers={"content-type": "application/json"})))
    f = await sp.klines_range("BTCUSDT", "1h", 0, 2 * H)
    assert [int(r[0]) for r in f.data["rows"]] == [0, H]
    cb = Coinbase(_http("coinbase", lambda r: httpx.Response(200, json=[{"time": 0}, "x", [0, 1, 2, 1, 1.5, 1]])))
    f = await cb.candles_range("BTC-USD", 3600, 0, H)
    assert f.data["rows"] == [[0, 1, 2, 1, 1.5, 1]]


async def test_coinbase_candles_range_chunks_by_300_and_rejects_bad_product():
    calls: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        p = req.url.params
        s = int(datetime.fromisoformat(p["start"].replace("Z", "+00:00")).timestamp())
        e = int(datetime.fromisoformat(p["end"].replace("Z", "+00:00")).timestamp())
        rows = [[t, 1, 2, 1.5, 1000.0 + t / 3600, 3] for t in range(s, e + 1, 3600)]
        assert len(rows) <= 300
        return httpx.Response(200, json=rows[::-1])  # newest first, like Coinbase

    cb = Coinbase(_http("coinbase", handler))
    f = await cb.candles_range("BTC-USD", 3600, 0, 700 * H)
    assert len(calls) == 3 and f.data["complete"] and calls[0] == "/products/BTC-USD/candles"
    assert [r[0] for r in f.data["rows"]] == [i * 3600 for i in range(700)]
    calls.clear()
    with pytest.raises(ProviderError):
        await cb.candles_range("BTC/USD", 3600, 0, H)
    with pytest.raises(ProviderError):
        await cb.candles("BTC-USD", 7200, 0, H)
    assert calls == []  # nothing was requested


async def test_bybit_open_interest_range_walks_200_interval_windows():
    calls: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.params
        calls.append((int(p["startTime"]), int(p["endTime"])))
        s, e = int(p["startTime"]), int(p["endTime"])
        rows = [{"openInterest": str(1000 + t // H), "timestamp": str(t)} for t in range(e // H * H, s - 1, -H)][: int(p["limit"])]
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": rows}})

    bb = Bybit(_http("bybit", handler))
    f = await bb.open_interest_range("BTCUSDT", "1h", 0, 336 * H)
    assert len(calls) == 2 and f.data["complete"] and len(f.data["rows"]) == 337
    assert [int(r["timestamp"]) for r in f.data["rows"]] == [i * H for i in range(337)]
    assert calls[0] == (137 * H, 336 * H) and calls[1] == (0, 137 * H - 1)


# ----------------------------------------------------------------------------- the tool on the captured responses


def _liq_rows(sym: str, s: int, e: int) -> list[dict]:
    k = 2.0 if sym.endswith(".6") else 1.0
    return [{"t": t, "l": k * 1000 * (t // 3600 % 5), "s": k * 500} for t in range(s // 3600 * 3600, e + 1, 3600)]


def _coinalyze(req: httpx.Request) -> httpx.Response:
    # Synthetic, in the documented shape of /liquidation-history (providers/coinalyze.py), not a capture.
    p = req.url.params
    sym = p["symbols"]
    return httpx.Response(200, json=[{"symbol": sym, "history": _liq_rows(sym, int(p["from"]), int(p["to"]))}])


def _klines_from(rows: list, req: httpx.Request) -> httpx.Response:
    p = req.url.params
    s, e, lim = int(p["startTime"]), int(p["endTime"]), int(p["limit"])
    return httpx.Response(200, json=[r for r in rows if s <= int(r[0]) <= e][:lim])


def _binance_spot(req: httpx.Request) -> httpx.Response:
    return _klines_from(FX["binance_spot_klines"], req) if req.url.path == "/api/v3/klines" else httpx.Response(404)


def _binance_futures(req: httpx.Request) -> httpx.Response:
    path, lim = req.url.path, int(req.url.params.get("limit", 0))
    if path == "/fapi/v1/klines":
        return _klines_from(FX["binance_fapi_klines"], req)
    if path.endswith("openInterestHist"):
        return httpx.Response(200, json=FX["binance_oi_hist"][-lim:])
    if path.endswith("globalLongShortAccountRatio"):
        return httpx.Response(200, json=FX["binance_ls_ratio"][-lim:])
    return httpx.Response(404, text="not mocked")


def _bybit(req: httpx.Request) -> httpx.Response:
    p = req.url.params
    if req.url.path.endswith("open-interest"):
        body = json.loads(json.dumps(FX["bybit_oi"]))
        rows = body["result"]["list"]
        if "startTime" in p:
            rows = [r for r in rows if int(p["startTime"]) <= int(r["timestamp"]) <= int(p["endTime"])]
        body["result"]["list"] = rows[: int(p["limit"])]
        return httpx.Response(200, json=body)
    if req.url.path.endswith("account-ratio"):
        body = json.loads(json.dumps(FX["bybit_ls_ratio"]))
        body["result"]["list"] = body["result"]["list"][: int(p["limit"])]
        return httpx.Response(200, json=body)
    return httpx.Response(404, text="not mocked")


def _coinbase(req: httpx.Request) -> httpx.Response:
    p = req.url.params
    s = int(datetime.fromisoformat(p["start"].replace("Z", "+00:00")).timestamp())
    e = int(datetime.fromisoformat(p["end"].replace("Z", "+00:00")).timestamp())
    return httpx.Response(200, json=[r for r in FX["coinbase_candles"] if s <= r[0] <= e][:300])


class FlowEx(FakeEx):
    """Funding history: Binance = the captured /fapi/v1/fundingRate rows; Bybit = synthetic 0.01 % every 8 h."""

    async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
        until = (params or {}).get("until", END)
        if self.id == "binanceusdm":
            rows = [{"timestamp": int(x["fundingTime"]), "fundingRate": float(x["fundingRate"])} for x in FX["binance_funding"]]
        else:
            rows = [{"timestamp": END - i * 8 * H, "fundingRate": 0.0001} for i in range(12)][::-1]
        return [r for r in rows if (since is None or r["timestamp"] >= since) and r["timestamp"] <= until][:limit]


def _flow_rt(**overrides) -> Runtime:
    handlers = {
        "binance_spot": _binance_spot,
        "binance_futures": _binance_futures,
        "bybit": _bybit,
        "coinbase": _coinbase,
        "coinalyze": _coinalyze,
        **overrides.pop("handlers", {}),
    }
    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, coinalyze_api_key="test-key"),
        transports={k: httpx.MockTransport(v) for k, v in handlers.items()},
        exchange_factory=overrides.pop("exchange_factory", FlowEx),
    )
    rt.now = lambda: datetime.fromtimestamp((END + 10 * 60_000) / 1000, UTC)
    return rt


def _spot(t: int) -> list:
    return next(r for r in FX["binance_spot_klines"] if int(r[0]) == t)


def _cb(t: int) -> float:
    return next(float(r[4]) for r in FX["coinbase_candles"] if r[0] * 1000 == t)


def _oi(t: int) -> float:
    a = next(float(r["sumOpenInterest"]) for r in FX["binance_oi_hist"] if int(r["timestamp"]) == t)
    b = next(float(r["openInterest"]) for r in FX["bybit_oi"]["result"]["list"] if int(r["timestamp"]) == t)
    return a + b


async def test_market_flow_happy_path_on_captured_responses():
    rt = _flow_rt()
    srv, _ = build_server(rt)
    env = await call(srv, "market_flow", base="btc", timeframe="1h", days=1)
    await rt.aclose()
    assert env["ok"] and env["missing"] == []
    d = env["data"]
    start = END - 24 * H
    assert d["base"] == "BTC" and d["venues"] == ["binance", "bybit"] and len(d["series"]) == 24
    assert d["series"][0]["ts"] == start and d["series"][-1]["ts"] == END - H
    last = d["series"][-1]
    sp = _spot(END - H)
    assert last["spot_delta"] == pytest.approx(2 * float(sp[10]) - float(sp[7]), abs=0.1)
    assert last["price"] == pytest.approx(float(sp[4]))
    assert last["oi_coins"] == pytest.approx(_oi(END), abs=0.1)  # snapshots at the bar close
    perp_last = next(r for r in FX["binance_fapi_klines"] if int(r[0]) == END - H)
    assert last["oi_usd"] == pytest.approx(_oi(END) * float(perp_last[4]), rel=1e-6)
    assert last["long_share"] == pytest.approx((0.6591 + 0.6464) / 2, abs=1e-6)
    prem = _cb(END - H) / float(sp[4]) - 1
    assert last["premium"] == pytest.approx(prem, abs=1e-6)
    prior = [_cb(t) / float(_spot(t)[4]) - 1 for t in range(END - 192 * H, END - H, H)]
    assert last["premium_adj"] == pytest.approx(prem - statistics.median(prior), abs=2e-6)
    assert all(r["premium_adj"] is not None for r in d["series"])  # 168+ hours of history before each bar
    # 24 h summary against the raw rows
    s24 = d["summary"]["24h"]
    perp_rows = [r for r in FX["binance_fapi_klines"] if start <= int(r[0]) < END]
    assert s24["perp_cvd"] == pytest.approx(sum(2 * float(r[10]) - float(r[7]) for r in perp_rows), abs=1.0)
    assert s24["price_change"] == pytest.approx(float(sp[4]) / float(_spot(start - H)[4]) - 1, abs=1e-6)
    assert s24["oi_change"] == pytest.approx(_oi(END) / _oi(start) - 1, abs=1e-6)
    liq = [x for sym in ("BTCUSDT_PERP.A", "BTCUSDT.6") for x in _liq_rows(sym, start // 1000, END // 1000 - 1)]
    assert s24["liq_long"] == pytest.approx(sum(x["l"] for x in liq)) and s24["liq_short"] == pytest.approx(24 * 1500)
    assert d["series"][-1]["perp_cvd"] == pytest.approx(s24["perp_cvd"], abs=1.0)  # cumulative over the 24 bars
    # funding: the last captured Binance settlement, 8 h interval
    bn = d["venues_latest"]["binance"]
    assert bn["funding_interval_hours"] == 8.0
    assert bn["funding_annualized"] == pytest.approx(float(FX["binance_funding"][-1]["fundingRate"]) * 1095, abs=1e-6)
    assert d["venues_latest"]["bybit"]["funding_annualized"] == pytest.approx(0.1095)
    # quadrant and sentence agree with the signs of the summary
    q = d["quadrant"]["24h"]
    assert q["price"] == ("up" if s24["price_change"] > 0.001 else "down" if s24["price_change"] < -0.001 else "flat")
    assert q["regime"] in mf.REGIME_UA and d["sentence_ua"].startswith("За 24 год: ціна ")
    assert d["sentence_4h_ua"].startswith("За 4 год: ")
    assert any("not a signal" in w and "taker-imbalance-8h-continuation" in w for w in env["warnings"])
    providers = {e["provider"] for e in env["evidence"]}
    assert {"binance_spot", "binance_futures", "bybit", "coinbase", "coinalyze", "ccxt:binanceusdm", "ccxt:bybit"} <= providers
    assert not any(isinstance(v, float) and not math.isfinite(v) for r in d["series"] for v in r.values())


async def test_market_flow_4h_buckets_and_summaries_only():
    rt = _flow_rt()
    srv, _ = build_server(rt)
    env = await call(srv, "market_flow", base="BTC", timeframe="4h", days=1, venues=["binance"])
    env2 = await call(srv, "market_flow", base="BTC", days=1, include_series=False)
    await rt.aclose()
    d = env["data"]
    series_end = END // (4 * H) * (4 * H)
    assert env["ok"] and len(d["series"]) == 6 and d["series"][-1]["ts"] == series_end - 4 * H
    b = d["series"][-1]
    hours = [_spot(t) for t in range(series_end - 4 * H, series_end, H)]
    assert b["spot_delta"] == pytest.approx(sum(2 * float(r[10]) - float(r[7]) for r in hours), abs=1.0)
    assert b["price"] == pytest.approx(float(hours[-1][4]))
    assert set(d["venues_latest"]) == {"binance"}
    assert not any(m["field"].endswith(":bybit") for m in env["missing"])  # bybit not asked, not fetched
    assert "series" not in env2["data"] and env2["data"]["summary"]["24h"] is not None


async def test_market_flow_provider_failures_land_in_missing():
    def down(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    class DownEx(FakeEx):
        async def fetch_funding_rate_history(self, symbol, since=None, limit=500, params=None):
            raise ccxt.NetworkError("venue down")

    names = ("binance_spot", "binance_futures", "bybit", "coinbase", "coinalyze")
    rt = _flow_rt(handlers={n: down for n in names}, exchange_factory=DownEx)
    srv, _ = build_server(rt)
    env = await call(srv, "market_flow", base="BTC", days=2)
    await rt.aclose()
    assert env["ok"] is False and env["data"] is None
    fields = {m["field"] for m in env["missing"]}
    assert {"spot_klines", "perp_klines", "premium:coinbase", "open_interest:binance", "open_interest:bybit"} <= fields
    assert {"funding:binance", "funding:bybit", "liquidations:binance", "liquidations:bybit", "long_short:bybit"} <= fields

    # only Coinbase down: everything else is returned, the premium is empty and named in missing
    rt = _flow_rt(handlers={"coinbase": down})
    srv, _ = build_server(rt)
    env = await call(srv, "market_flow", base="BTC", days=1)
    await rt.aclose()
    assert env["ok"] and [m["field"] for m in env["missing"]] == ["premium:coinbase"]
    assert all(r["premium"] is None and r["premium_adj"] is None for r in env["data"]["series"])
    assert env["data"]["series"][-1]["spot_delta"] is not None


async def test_market_flow_rejects_bad_arguments_without_requests():
    seen: list = []

    def spy(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(404)

    rt = _flow_rt(handlers={n: spy for n in ("binance_spot", "binance_futures", "bybit", "coinbase", "coinalyze")})
    srv, _ = build_server(rt)
    env = await call(srv, "market_flow", base="BTC", venues=["okx"])
    env2 = await call(srv, "market_flow", base="BTC/USDT")
    await rt.aclose()
    assert not env["ok"] and env["missing"][0]["field"] == "venues" and env["missing"][0]["reason"] == "invalid_argument"
    assert not env2["ok"] and env2["missing"][0]["field"] == "base"
    assert seen == []
