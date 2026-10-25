"""`exchanges` in liquidations_history / coinalyze_oi_history: whitelist, per-exchange symbols, sum across exchanges.

Samples: DoubleTop's relay of Coinalyze captured 2026-10-25 (tests/fixtures/doubletop/coinalyze__*_2026-10-25.json):
daily liquidations of BTCUSDT_PERP.A (Binance) and BTCUSDT.6 (Bybit) in coins, hourly OI of the same two symbols.
'.6' = Bybit was confirmed by matching Coinalyze OI with Bybit's live OI (docs/reviews/2026-10-25-doubletop-parity-2.md).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp import Client

from tradedesk.analytics.coinalyze_series import sum_histories
from tradedesk.analytics.liquidations_agg import exchange_symbol, parse_exchanges
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIXDIR = Path(__file__).parent / "fixtures/doubletop"
LIQ = {
    x["symbol"]: x["history"]
    for x in json.loads((FIXDIR / "coinalyze__liquidation_history_BTC-ETH-SOL.A_BTC.6_daily_2026-10-25.json").read_text())["response"]
}
OI = {
    x["symbol"]: x["history"]
    for x in json.loads((FIXDIR / "coinalyze__open_interest_history_BTC.A_BTC.6_1hour_2026-10-25.json").read_text())["response"]
}
SAMPLES = {"/v1/liquidation-history": LIQ, "/v1/open-interest-history": OI}
SECRET = "k3y-SECRET-0123"
NOW_DAILY = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)  # the seven sample days are 18..24.10
NOW_HOURLY = datetime(2026, 10, 25, 20, 23, tzinfo=UTC)  # capture time of the hourly OI sample
BN, BY = "BTCUSDT_PERP.A", "BTCUSDT.6"


async def call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


async def descriptions(server):
    async with Client(server) as c:
        return {t.name: t.description for t in (await c.list_tools()).tools}


def make_server(now, fail=frozenset(), history=None):
    """Serve the 2026-10-25 samples per requested symbol; symbols in `fail` answer 503."""
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        sym = req.url.params["symbols"]
        if sym in fail:
            return httpx.Response(503, text="down")
        src = history or SAMPLES[req.url.path]
        return httpx.Response(200, json=[{"symbol": sym, "history": src.get(sym, [])}])

    rt = Runtime(
        Settings(http_retries=0, cache_scale=0.0, coinalyze_api_key=SECRET), transports={"coinalyze": httpx.MockTransport(handler)}
    )
    rt.now = lambda: now
    srv, _ = build_server(rt)
    return srv, seen


# ---- pure helpers -------------------------------------------------------------------------------------------


def test_parse_exchanges_whitelist():
    assert parse_exchanges(None) == (["binance"], [])
    assert parse_exchanges(["Binance", ".6", "A", "bybit", " .a "]) == (["binance", "bybit"], [])
    assert parse_exchanges(["okx", "binance", "6&symbols=X" * 5]) == (["binance"], ["okx", ("6&symbols=X" * 5)[:20]])
    assert parse_exchanges([]) == ([], [])


def test_exchange_symbol():
    assert exchange_symbol("btc", "usdt", "binance") == BN
    assert exchange_symbol("ETH", "usd", "binance") == "ETHUSD_PERP.A"
    assert exchange_symbol("btc", "usdt", "bybit") == BY
    assert exchange_symbol("BTC", "usd", "bybit") is None  # coin-margined Bybit symbol format is not verified
    assert exchange_symbol("../x", "usdt", "bybit") is None and exchange_symbol("BTC", "usdt", "okx") is None


def test_sum_histories_sums_common_timestamps():
    rows, dropped = sum_histories([LIQ[BN], LIQ[BY]], ("l", "s"))
    assert dropped == 0 and len(rows) == 7
    assert rows[-1]["t"] == 1792800000
    assert rows[-1]["l"] == pytest.approx(722.567 + 259.845, abs=1e-6)
    assert rows[-1]["s"] == pytest.approx(81.310 + 17.911, abs=1e-6)
    t0 = 1792281600
    a = [{"t": t0, "l": 1, "s": 2}, {"t": t0 + 86400, "l": 3, "s": 4}, {"t": "x"}, "junk", None]
    b = [{"t": t0, "l": 10, "s": 20}, {"t": t0 + 2 * 86400, "l": 5, "s": 5}, {"t": t0 + 86400, "l": -1, "s": 0}]
    rows, dropped = sum_histories([a, b], ("l", "s"))
    assert rows == [{"t": t0, "l": 11.0, "s": 22.0}] and dropped == 2
    assert sum_histories([], ("l",)) == ([], 0)


# ---- tools ---------------------------------------------------------------------------------------------------


async def test_liquidations_sum_binance_and_bybit():
    srv, seen = make_server(NOW_DAILY)
    env = await call(srv, "liquidations_history", symbol="BTC", days=7, in_usd=False, exchanges=["binance", "bybit"])
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert sorted(r.url.params["symbols"] for r in seen) == [BY, BN]
    assert d["symbol"] == f"{BN},{BY}" and d["unit"] == "BTC"
    assert [(e["exchange"], e["code"], e["symbol"], e["intervals"]) for e in d["exchanges"]] == [
        ("binance", ".A", BN, 7),
        ("bybit", ".6", BY, 7),
    ]
    assert d["totals"]["long"] == pytest.approx(sum(h["l"] for h in LIQ[BN]) + sum(h["l"] for h in LIQ[BY]))
    assert d["rows"][-1]["long"] == pytest.approx(722.567 + 259.845, abs=1e-6)
    assert any("Sum of binance, bybit only" in w for w in env["warnings"])
    assert not any("Binance only" in w for w in env["warnings"])
    assert len(env["evidence"]) == 2 and SECRET not in json.dumps(env)


async def test_liquidations_default_is_binance_only_with_usd_note():
    srv, seen = make_server(NOW_DAILY)
    env = await call(srv, "liquidations_history", symbol="BTC", days=7)
    assert env["ok"] is True and [r.url.params["symbols"] for r in seen] == [BN]
    assert env["data"]["symbol"] == BN and [e["exchange"] for e in env["data"]["exchanges"]] == ["binance"]
    assert any("Binance only" in w for w in env["warnings"])
    assert any("close" in w and "liquidations_summary" in w for w in env["warnings"])


async def test_oi_sum_hourly_codes_accepted():
    srv, seen = make_server(NOW_HOURLY)
    env = await call(srv, "coinalyze_oi_history", symbol="BTC", interval="hourly", days=1, in_usd=False, exchanges=[".A", ".6"])
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert len(d["rows"]) == 3 and d["unit"] == "BTC"
    assert d["stats"]["latest"] == pytest.approx(93531.992 + 62749.196, abs=1e-6)
    assert d["rows"][0]["open"] == pytest.approx(93507.539 + 62782.174, abs=1e-6)
    assert {r.url.params["symbols"] for r in seen} == {BN, BY}
    assert not any("close of the interval" in w for w in env["warnings"])  # USD note only with in_usd


@pytest.mark.parametrize("tool", ["liquidations_history", "coinalyze_oi_history"])
async def test_one_exchange_down_is_missing_rest_summed(tool):
    now = NOW_DAILY if tool == "liquidations_history" else NOW_HOURLY
    srv, _ = make_server(now, fail={BY})
    env = await call(
        srv, tool, symbol="BTC", interval="daily" if tool == "liquidations_history" else "hourly", days=7, exchanges=["binance", "bybit"]
    )
    assert env["ok"] is True
    assert [(m["field"].split(".")[-1], m["provider"]) for m in env["missing"]] == [("bybit", "coinalyze")]
    assert [e["exchange"] for e in env["data"]["exchanges"]] == ["binance"] and env["data"]["symbol"] == BN
    assert any("partial sum: bybit" in w for w in env["warnings"])


@pytest.mark.parametrize("tool", ["liquidations_history", "coinalyze_oi_history"])
async def test_all_exchanges_down_returns_envelope(tool):
    srv, _ = make_server(NOW_DAILY, fail={BN, BY})
    env = await call(srv, tool, symbol="BTC", exchanges=["binance", "bybit"])
    assert env["ok"] is False and env["data"] is None
    assert len(env["missing"]) == 2 and all(m["provider"] == "coinalyze" for m in env["missing"])


@pytest.mark.parametrize("tool", ["liquidations_history", "coinalyze_oi_history"])
@pytest.mark.parametrize("bad", [["okx"], ["binance", "BTCUSDT.6&symbols=ETHUSDT.6"], []])
async def test_unknown_exchange_rejected_without_request(tool, bad):
    srv, seen = make_server(NOW_DAILY)
    env = await call(srv, tool, symbol="BTC", exchanges=bad)
    assert env["ok"] is False and seen == []
    m = env["missing"][0]
    assert m["field"] == "exchanges" and m["reason"] == "invalid_argument" and "allowed" in m["detail"]


async def test_bybit_coin_margined_refused_binance_kept():
    srv, seen = make_server(NOW_DAILY)
    env = await call(srv, "liquidations_history", symbol="BTC", days=7, margin="usd", exchanges=["bybit", "binance"])
    assert [r.url.params["symbols"] for r in seen] == ["BTCUSD_PERP.A"]
    assert env["missing"][0]["field"] == "liquidations.bybit" and env["missing"][0]["reason"] == "invalid_argument"
    srv, seen = make_server(NOW_DAILY)
    env = await call(srv, "liquidations_history", symbol="BTC", margin="usd", exchanges=["bybit"])
    assert env["ok"] is False and seen == []


async def test_interval_missing_on_one_exchange_is_left_out():
    t0 = 1792281600
    hist = {BN: [{"t": t0, "l": 1, "s": 1}, {"t": t0 + 86400, "l": 2, "s": 2}], BY: [{"t": t0 + 86400, "l": 3, "s": 3}]}
    srv, _ = make_server(NOW_DAILY, history=hist)
    env = await call(srv, "liquidations_history", symbol="BTC", days=7, exchanges=["binance", "bybit"])
    assert env["ok"] is True and len(env["data"]["rows"]) == 1
    assert env["data"]["rows"][0]["long"] == pytest.approx(5.0)
    assert any("1 interval(s) left out" in w for w in env["warnings"])


async def test_descriptions_state_usd_conversion_and_exchanges():
    srv, _ = make_server(NOW_DAILY)
    desc = await descriptions(srv)
    for tool in ("liquidations_history", "coinalyze_oi_history"):
        text = desc[tool]
        assert "coins x the close" in text and "liquidations_summary" in text and "crash days" in text
        assert "'.A'" in text and "'.6'" in text
