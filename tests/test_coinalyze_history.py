"""coinalyze_oi_history / coinalyze_funding_history / coinalyze_long_short_history behind MockTransport.

Response shapes come from DoubleTop's relay of Coinalyze captured 2026-10-18 (tests/fixtures/doubletop/coinalyze__*
_2026-10-18.json, BTCUSDT_PERP.A daily, 4 rows). They are real Coinalyze data, but NOT a direct live call with our
key: parameter names (`convert_to_usd` on OI) and the hourly window still need one live call by the owner.
Units are cross-checked against our own saved Binance responses of the same day (ours__*_2026-10-18.json).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp import Client

from tradedesk.analytics.coinalyze_series import FUNDING_PCT_TO_DECIMAL, long_short_series, ohlc_series
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIXDIR = Path(__file__).parent / "fixtures/doubletop"


def _load(name):
    return json.loads((FIXDIR / name).read_text())


OI = _load("coinalyze__open_interest_history_BTCUSDT_PERP.A_daily_2026-10-18.json")[0]["history"]
FUND = _load("coinalyze__funding_rate_history_BTCUSDT_PERP.A_daily_2026-10-18.json")[0]["history"]
LSR = _load("coinalyze__long_short_ratio_history_BTCUSDT_PERP.A_daily_2026-10-18.json")[0]["history"]
BN_FUND = _load("ours__binanceusdm_funding_history_BTCUSDT_2026-10-18.json")["data"]
BN_LSR = _load("ours__binance_globalLongShortAccountRatio_BTCUSDT_1d_2026-10-18.json")["data"]
SAMPLES = {"/v1/open-interest-history": OI, "/v1/funding-rate-history": FUND, "/v1/long-short-ratio-history": LSR}
SECRET = "k3y-SECRET-0123"
NOW = datetime(2026, 10, 18, 0, 0, tzinfo=UTC)
TOOLS = {
    "coinalyze_oi_history": "/v1/open-interest-history",
    "coinalyze_funding_history": "/v1/funding-rate-history",
    "coinalyze_long_short_history": "/v1/long-short-ratio-history",
}


async def call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


def make_server(handler=None, key=SECRET):
    seen: list[httpx.Request] = []

    def default(req):
        seen.append(req)
        return httpx.Response(200, json=[{"symbol": req.url.params["symbols"], "history": SAMPLES[req.url.path]}])

    def wrapped(req):
        seen.append(req)
        return handler(req)

    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, coinalyze_api_key=key),
        transports={"coinalyze": httpx.MockTransport(wrapped if handler else default)},
    )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv, seen


# ---- pure helpers -------------------------------------------------------------------------------------------


def test_ohlc_series_stats_on_oi_sample():
    s = ohlc_series(OI, "daily")
    closes = [h["c"] for h in OI]
    st = s["stats"]
    assert st["intervals"] == 4 and st["gaps"] == 0
    assert st["latest"] == pytest.approx(closes[-1]) and st["first"] == pytest.approx(closes[0])
    assert st["mean"] == pytest.approx(sum(closes) / 4)
    assert st["change_pct"] == pytest.approx((closes[-1] - closes[0]) / closes[0])
    assert st["min"] == pytest.approx(min(closes)) and st["max"] == pytest.approx(max(closes))
    assert s["rows"][0]["t"] == "2026-10-14T00:00:00+00:00" and s["rows"][0]["open"] == pytest.approx(92493.367)


def test_funding_percent_to_decimal_matches_binance_payments():
    """Coinalyze sends percent: close / 100 = the last Binance settlement of the UTC day, high = the largest."""
    s = ohlc_series(FUND, "daily", scale=FUNDING_PCT_TO_DECIMAL, with_pct_change=False)
    pay = [(p["timestamp"] // 1000, p["fundingRate"]) for p in BN_FUND]
    for r in s["rows"]:
        day = [v for t, v in pay if r["ts"] <= t < r["ts"] + 86_400]
        assert r["close"] == pytest.approx(day[-1], abs=1e-9)
        assert r["high"] == pytest.approx(max(day), abs=1e-9)
    assert s["stats"]["change_pct"] is None
    assert abs(s["stats"]["latest"]) < 0.001  # decimals per interval, not percent


def test_long_short_fractions_match_binance_account_ratio():
    s = long_short_series(LSR, "daily")
    bn = {int(x["timestamp"]) // 1000: x for x in BN_LSR}
    for r in s["rows"]:
        b = bn[r["ts"]]
        assert r["long_share"] == pytest.approx(float(b["longAccount"]), abs=1e-6)
        assert r["short_share"] == pytest.approx(float(b["shortAccount"]), abs=1e-6)
        assert r["ratio"] == pytest.approx(float(b["longShortRatio"]), abs=1e-6)
    assert s["stats"]["long_share_latest"] == pytest.approx(0.6331)
    assert s["stats"]["intervals"] == 4


T0 = 1_792_108_800  # 2026-10-16T00:00:00Z
# Timestamps an untrusted API row could carry that once made the tools raise (int(inf) -> OverflowError,
# fromtimestamp(1e14) -> ValueError/OverflowError/OSError depending on the platform) or are not plausible bar starts.
BAD_TS = [10**14, -(10**13), float("inf"), float("-inf"), float("nan"), 0, 4_102_444_800, "1e400"]


def test_series_edge_cases():
    assert ohlc_series([], "daily") is None and long_short_series([], "daily") is None
    bad = [{"t": "x"}, "junk", None, {"t": T0, "o": 1, "h": 1, "l": 1, "c": float("nan")}]
    assert ohlc_series(bad, "daily") is None
    ls_bad = [{"t": T0, "r": -1, "l": 50, "s": 50}, {"t": T0 + 1, "r": 1, "l": 150, "s": 50}]
    assert long_short_series(ls_bad, "daily") is None
    gap = ohlc_series([{"t": T0, "o": 1, "h": 1, "l": 1, "c": 1}, {"t": T0 + 3 * 86_400, "o": 2, "h": 2, "l": 2, "c": 2}], "daily")
    assert gap["stats"]["gaps"] == 1 and gap["stats"]["stdev"] is not None
    one = ohlc_series([{"t": T0, "o": 0, "h": 0, "l": 0, "c": 0}, {"t": T0, "o": 0, "h": 0, "l": 0, "c": 0}], "daily")
    assert one["stats"]["intervals"] == 1 and one["stats"]["zscore_latest"] is None and one["stats"]["change_pct"] is None


@pytest.mark.parametrize("t", BAD_TS)
def test_series_drop_bad_timestamps(t):
    good_ohlc = {"t": T0, "o": 1, "h": 1, "l": 1, "c": 1}
    good_ls = {"t": T0, "r": 1.5, "l": 60, "s": 40}
    assert ohlc_series([{**good_ohlc, "t": t}], "daily") is None
    assert long_short_series([{**good_ls, "t": t}], "daily") is None
    s = ohlc_series([{**good_ohlc, "t": t}, good_ohlc], "daily")
    assert s["stats"]["intervals"] == 1 and s["rows"][0]["t"] == "2026-10-16T00:00:00+00:00"
    assert long_short_series([{**good_ls, "t": t}, good_ls], "daily")["stats"]["intervals"] == 1


# ---- tools: happy paths -------------------------------------------------------------------------------------


async def test_oi_history_happy_and_key_not_leaked():
    """The OI sample was captured WITHOUT USD conversion (values are BTC, ~93k coins), so `unit == "USD"` here only
    checks our labelling of the request flag, not that Coinalyze honours `convert_to_usd` on this endpoint. After
    the owner's live call, save a USD sample and check its magnitude (~7.6e9 for BTC, like Binance
    sumOpenInterestValue in ours__binance_openInterestHist_BTCUSDT_1d_2026-10-18.json)."""
    srv, seen = make_server()
    env = await call(srv, "coinalyze_oi_history", symbol="btc", days=4)
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["symbol"] == "BTCUSDT_PERP.A" and d["unit"] == "USD" and len(d["rows"]) == 4
    req = seen[0]
    assert req.url.path == "/v1/open-interest-history" and req.headers["api_key"] == SECRET
    assert req.url.params["interval"] == "daily" and req.url.params["convert_to_usd"] == "true"
    assert int(req.url.params["to"]) == int(NOW.timestamp())
    assert int(req.url.params["from"]) == int(NOW.timestamp()) - 4 * 86_400
    assert SECRET not in json.dumps(env) and SECRET not in str(req.url)
    assert any("Binance only" in w for w in env["warnings"])
    assert env["evidence"][0]["provider"] == "coinalyze"


async def test_oi_history_base_units_and_hourly_cap():
    srv, seen = make_server()
    env = await call(srv, "coinalyze_oi_history", symbol="ETH", interval="hourly", days=90, in_usd=False)
    assert env["ok"] is True and env["data"]["unit"] == "ETH" and env["data"]["days"] == 30
    assert any("capped" in w for w in env["warnings"])
    assert seen[0].url.params["interval"] == "1hour" and seen[0].url.params["convert_to_usd"] == "false"


async def test_funding_history_happy_decimal_units():
    srv, seen = make_server()
    env = await call(srv, "coinalyze_funding_history", symbol="BTC", days=4)
    assert env["ok"] is True
    d = env["data"]
    assert d["rows"][-1]["close"] == pytest.approx(FUND[-1]["c"] / 100)
    assert d["unit"] == "decimal per funding interval"
    assert seen[0].url.path == "/v1/funding-rate-history" and "convert_to_usd" not in seen[0].url.params
    assert any("Binance only" in w and "not annualised" in w for w in env["warnings"])
    assert any("open/low may carry" in w and "not a carry basis" in w for w in env["warnings"])
    assert not any("hourly bar semantics" in w for w in env["warnings"])


async def test_funding_history_hourly_is_flagged_unverified():
    srv, _ = make_server()
    env = await call(srv, "coinalyze_funding_history", symbol="BTC", interval="hourly", days=4)
    assert env["ok"] is True
    assert any("hourly bar semantics are not verified" in w for w in env["warnings"])


async def test_long_short_history_happy_fractions():
    srv, seen = make_server()
    env = await call(srv, "coinalyze_long_short_history", symbol="BTC", days=4, margin="usdt")
    assert env["ok"] is True
    d = env["data"]
    assert d["rows"][0]["long_share"] == pytest.approx(0.5634) and d["rows"][0]["ratio"] == pytest.approx(1.2904)
    assert seen[0].url.path == "/v1/long-short-ratio-history"
    assert any("Binance only" in w for w in env["warnings"])


# ---- tools: failures ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_provider_failure_is_missing(tool):
    srv, seen = make_server(lambda req: httpx.Response(503, text="down"))
    env = await call(srv, tool, symbol="BTC")
    assert env["ok"] is False and env["data"] is None
    assert env["missing"][0]["provider"] == "coinalyze" and seen[0].url.path == TOOLS[tool]


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_bad_key_is_auth_missing(tool):
    srv, _ = make_server(lambda req: httpx.Response(401, text="invalid api key"))
    env = await call(srv, tool, symbol="BTC")
    assert env["ok"] is False and env["missing"][0]["reason"] == "auth"


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_missing_key_makes_no_request(tool):
    srv, seen = make_server(key=None)
    env = await call(srv, tool, symbol="BTC")
    assert env["ok"] is False and env["missing"][0]["reason"] == "config"
    assert "COINALYZE_API_KEY" in env["missing"][0]["detail"] and seen == []


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_empty_range_and_bad_shape(tool):
    srv, _ = make_server(lambda req: httpx.Response(200, json=[]))
    env = await call(srv, tool, symbol="BTC")
    assert env["ok"] is False and env["missing"][0]["reason"] == "data_unavailable"
    srv, _ = make_server(lambda req: httpx.Response(200, json={"error": "x"}))
    env = await call(srv, tool, symbol="BTC")
    assert env["ok"] is False and env["missing"][0]["reason"] == "backend_error"


BAD_TS_JSON = ["100000000000000", "-10000000000000", "Infinity", "-Infinity", "NaN", "1e400"]  # json.loads accepts all


@pytest.mark.parametrize("ts", BAD_TS_JSON)
@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_bad_timestamp_rows_return_envelope(tool, ts):
    """A row with an absurd `t` is dropped: only bad rows -> data_unavailable; mixed with a good row -> ok."""
    bad = f'{{"t": {ts}, "o": 1, "h": 1, "l": 60, "c": 1, "r": 1.5, "s": 40}}'
    good = f'{{"t": {T0}, "o": 1, "h": 1, "l": 60, "c": 1, "r": 1.5, "s": 40}}'

    def only_bad(req):
        return httpx.Response(200, content=f'[{{"symbol": "BTCUSDT_PERP.A", "history": [{bad}]}}]'.encode())

    def mixed(req):
        return httpx.Response(200, content=f'[{{"symbol": "BTCUSDT_PERP.A", "history": [{bad}, {good}]}}]'.encode())

    srv, _ = make_server(only_bad)
    env = await call(srv, tool, symbol="BTC", days=4)
    assert env["ok"] is False and env["data"] is None and env["missing"][0]["reason"] == "data_unavailable"
    srv, _ = make_server(mixed)
    env = await call(srv, tool, symbol="BTC", days=4)
    assert env["ok"] is True and len(env["data"]["rows"]) == 1


@pytest.mark.parametrize("tool", sorted(TOOLS))
async def test_invalid_symbol_makes_no_request(tool):
    srv, seen = make_server()
    env = await call(srv, tool, symbol="BTC/../x")
    assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_argument" and seen == []
