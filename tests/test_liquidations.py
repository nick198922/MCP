from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import ccxt
import pytest
from mcp import Client

from conftest import NOW, FakeEx, make_transports
from tradedesk.collector import scheduler as sched_mod
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.providers.liquidations import (
    okx_contract_specs,
    parse_binance_force_order,
    parse_bybit_liquidation,
    parse_okx_liquidation,
)
from tradedesk.runtime import Runtime
from tradedesk.server import build_server


async def _call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)
    if set(data) == {"result"}:
        data = data["result"]
    return data


# ---------- parser ----------


def _event(**overrides):
    base = {
        "e": "forceOrder",
        "E": 1_568_014_460_893,
        "o": {
            "s": "BTCUSDT",
            "S": "SELL",
            "o": "LIMIT",
            "f": "IOC",
            "q": "0.014",
            "p": "9910",
            "ap": "9910",
            "X": "FILLED",
            "l": "0.014",
            "z": "0.014",
            "T": 1_568_014_460_893,
        },
    }
    base["o"].update(overrides)
    return base


def test_parse_sell_is_long_liquidation():
    row = parse_binance_force_order(_event())
    assert row == {
        "ts": 1_568_014_460,
        "venue": "binanceusdm",
        "symbol": "BTCUSDT",
        "side": "sell",
        "qty": pytest.approx(0.014),
        "price": pytest.approx(9910.0),
        "notional_usd": pytest.approx(0.014 * 9910.0),
    }


def test_parse_buy_maps_to_short_liquidation():
    row = parse_binance_force_order(_event(S="BUY"))
    assert row["side"] == "buy"


WS = json.loads((Path(__file__).parent / "fixtures" / "liquidations_ws_2026-10-28.json").read_text())


def _bybit_rows() -> list[dict]:
    return [r for m in WS["bybit_messages"] for r in parse_bybit_liquidation(m)]


def _okx_rows(specs) -> list[dict]:
    return [r for m in WS["okx_messages"] for r in parse_okx_liquidation(m, specs)]


def test_parse_bybit_documented_example_buy_is_long_liquidation():
    # Bybit docs: S is the position side, "Buy" = a long got liquidated -> our taker-side "sell".
    (row,) = parse_bybit_liquidation(WS["documented_bybit"])
    assert row == {
        "ts": 1_739_502_302,
        "venue": "bybit",
        "symbol": "ROSEUSDT",
        "side": "buy",  # S=Sell: a short got liquidated
        "qty": pytest.approx(20_000.0),
        "price": pytest.approx(0.04499),
        "notional_usd": pytest.approx(20_000 * 0.04499),
    }
    flipped = json.loads(json.dumps(WS["documented_bybit"]).replace('"S": "Sell"', '"S": "Buy"'))
    assert parse_bybit_liquidation(flipped)[0]["side"] == "sell"


def test_parse_bybit_live_sides_match_bankruptcy_price():
    # Live 2026-10-28: a liquidated long's bankruptcy price sits below the mark, a short's above. All 33 rows agree with
    # the side we store (26 S=Buy -> "sell" below the mark, 7 S=Sell -> "buy" above); the old mapping got all wrong.
    rows = _bybit_rows()
    assert len(rows) == 33
    assert sum(r["side"] == "sell" for r in rows) == 26
    marks = {k: float(v) for k, v in WS["bybit_marks"].items()}
    for r in rows:
        assert (r["price"] < marks[r["symbol"]]) == (r["side"] == "sell"), r
    btc = next(r for r in rows if r["symbol"] == "BTCUSDT")
    assert btc["qty"] == pytest.approx(0.001) and btc["notional_usd"] == pytest.approx(81.1109)  # v is in coins


def test_parse_bybit_ignores_other_topics():
    assert parse_bybit_liquidation({"topic": "trade.BTCUSDT", "data": [{}]}) == []
    assert parse_bybit_liquidation({"op": "subscribe", "success": True}) == []
    assert (
        parse_bybit_liquidation({"topic": "allLiquidation.BTCUSDT", "data": [{"T": 1, "s": "BTCUSDT", "S": "?", "v": "1", "p": "1"}]}) == []
    )


def test_okx_contract_specs_from_live_instruments():
    specs = okx_contract_specs(WS["okx_instruments"]["data"])
    assert specs["BTC-USDT-SWAP"] == (pytest.approx(0.01), False)
    assert specs["ETH-USDT-SWAP"] == (pytest.approx(0.1), False)
    assert specs["DOS-USDT-SWAP"] == (pytest.approx(10.0), False)
    assert specs["BTC-USD-SWAP"] == (pytest.approx(100.0), True)  # inverse: 100 USD per contract
    # ctMult multiplies; value currency other than base (linear) / USD (inverse) or no ctVal -> left out.
    odd = [
        {"instId": "X-USDT-SWAP", "ctType": "linear", "ctVal": "2", "ctMult": "5", "ctValCcy": "X"},
        {"instId": "Y-USDT-SWAP", "ctType": "linear", "ctVal": "1", "ctMult": "1", "ctValCcy": "USDT"},
        {"instId": "Z-USD-SWAP", "ctType": "inverse", "ctVal": "10", "ctMult": "1", "ctValCcy": "Z"},
        {"instId": "W-USDT", "ctType": "", "ctVal": "", "ctMult": "", "ctValCcy": ""},
        "garbage",
    ]
    assert okx_contract_specs(odd) == {"X-USDT-SWAP": (10.0, False)}


def test_parse_okx_live_sizes_are_contracts_times_ct_val():
    specs = okx_contract_specs(WS["okx_instruments"]["data"])
    rows = _okx_rows(specs)
    assert len(rows) == 10
    eth = next(r for r in rows if r["symbol"] == "ETH-USDT-SWAP")
    # sz 288 contracts x 0.1 ETH at 2345.12: 28.8 ETH, ~$67.5k (the old parser stored 288 ETH, ~$675k).
    assert eth["qty"] == pytest.approx(28.8) and eth["notional_usd"] == pytest.approx(28.8 * 2345.12)
    big = max((r for r in rows if r["symbol"] == "BTC-USDT-SWAP"), key=lambda r: r["qty"])
    assert big["qty"] == pytest.approx(0.13) and big["notional_usd"] == pytest.approx(0.13 * 80930.5)
    # Side = order side; every live detail is in hedge mode and agrees with posSide (short -> buy, long -> sell). OKX's
    # bkPx sits inside the minute's mark range (live 2026-10-28), so unlike Bybit it cannot check the side by itself.
    details = [d for m in WS["okx_messages"] if "data" in m for i in m["data"] for d in i["details"]]
    assert {(d["posSide"], d["side"]) for d in details} == {("short", "buy"), ("long", "sell")}
    assert [r["side"] for r in rows] == [d["side"] for d in details]


def test_parse_okx_documented_example_and_inverse():
    event = WS["documented_okx"]
    assert parse_okx_liquidation(event, {}) == []  # unknown instrument: dropped, never stored in contracts
    (row,) = parse_okx_liquidation(event, {"IOST-USDT-SWAP": (1000.0, False)})
    assert row["side"] == "buy" and row["ts"] == 1_692_266_434
    assert row["qty"] == pytest.approx(13_000.0) and row["notional_usd"] == pytest.approx(13_000 * 0.007831)
    inv = {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [
            {"instId": "BTC-USD-SWAP", "details": [{"side": "sell", "posSide": "net", "sz": "5", "bkPx": "80000", "ts": "1793210146299"}]}
        ],
    }
    (row,) = parse_okx_liquidation(inv, {"BTC-USD-SWAP": (100.0, True)})
    assert row["notional_usd"] == pytest.approx(500.0) and row["qty"] == pytest.approx(500.0 / 80000)


def test_parse_okx_drops_bad_details():
    specs = {"BTC-USDT-SWAP": (0.01, False)}

    def ev(**d):
        base = {"side": "sell", "posSide": "long", "sz": "1", "bkPx": "80000", "ts": "1793210146299"}
        return {"arg": {"channel": "liquidation-orders"}, "data": [{"instId": "BTC-USDT-SWAP", "details": [{**base, **d}]}]}

    assert len(parse_okx_liquidation(ev(), specs)) == 1
    assert parse_okx_liquidation(ev(posSide="short"), specs) == []  # side and posSide disagree
    assert parse_okx_liquidation(ev(side=""), specs) == []
    assert parse_okx_liquidation(ev(sz="0"), specs) == []
    assert parse_okx_liquidation(ev(bkPx="x"), specs) == []
    assert parse_okx_liquidation({"arg": {"channel": "trades"}, "data": [{}]}, specs) == []
    assert parse_okx_liquidation(WS["okx_messages"][0], specs) == []  # port-8443 notice
    assert parse_okx_liquidation(WS["okx_messages"][1], specs) == []  # subscribe ack


def test_parse_rejects_bad_events():
    assert parse_binance_force_order({"e": "trade"}) is None
    assert parse_binance_force_order({"e": "forceOrder", "o": {}}) is None
    # qty = 0
    bad = _event(l="0")
    bad["o"]["z"] = "0"
    bad["o"]["q"] = "0"
    assert parse_binance_force_order(bad) is None
    # unknown side
    assert parse_binance_force_order(_event(S="SIDE")) is None


# ---------- collector jobs ----------


def test_enabling_liquidations_without_venues_starts_all_three(state, tmp_path, monkeypatch):
    # Bybit (side) and OKX (contracts) parsers fixed 2026-10-28: a bare switch-on starts all three venues again.
    monkeypatch.delenv("TRADEDESK_COLLECT_LIQUIDATIONS_VENUES", raising=False)
    monkeypatch.setenv("TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED", "1")
    s = Settings.from_env()
    assert Settings().collect_liquidations_venues == "binance,bybit,okx" and s.collect_liquidations_venues == "binance,bybit,okx"
    rt = Runtime(
        dataclasses.replace(s, collector_enabled=True, db_path=str(tmp_path / "liq.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    sched = build_default_scheduler(rt)
    assert sched is not None
    assert sorted(j.name for j in sched.jobs if j.name.startswith("liquidations")) == [
        "liquidations:binance",
        "liquidations:bybit",
        "liquidations:okx",
    ]


class InstrumentsEx(FakeEx):
    """FakeEx serving the live OKX SWAP instruments; ``down`` makes the endpoint fail."""

    down = False
    calls: list = []

    async def publicGetPublicInstruments(self, params):  # noqa: N802 - ccxt implicit endpoint name
        InstrumentsEx.calls.append(dict(params))
        if self.down:
            raise ccxt.NetworkError("okx instruments down")
        return copy.deepcopy(WS["okx_instruments"])


@pytest.fixture
def ws_replay(monkeypatch):
    """Replace the WebSocket with the recorded messages of the venue; records (url, subscribe)."""
    import tradedesk.providers.liquidations as liq  # noqa: PLC0415

    opened: list = []

    async def fake(url, subscribe, parser, *, max_seconds):
        opened.append((url, subscribe))
        for msg in WS["okx_messages"] if "okx" in url else WS["bybit_messages"]:
            for row in parser(msg) or []:
                yield row

    monkeypatch.setattr(liq, "_ws_consume", fake)
    InstrumentsEx.down, InstrumentsEx.calls = False, []
    return opened


async def _job_rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "liq.sqlite")),
        transports=make_transports(state),
        exchange_factory=InstrumentsEx,
    )
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_okx_job_converts_contracts_with_instrument_specs(state, tmp_path, ws_replay):
    rt = await _job_rt(state, tmp_path)
    try:
        assert await sched_mod._collect_liquidations_okx(rt) == 10
        assert InstrumentsEx.calls == [{"instType": "SWAP"}]
        assert ws_replay == [
            ("wss://ws.okx.com:443/ws/v5/public", {"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]})
        ]
        rows = await rt.store.liquidations_in_window(since_ts=0, until_ts=2_000_000_000, venue="okx")
        eth = next(r for r in rows if r["symbol"] == "ETH-USDT-SWAP")
        assert eth["qty"] == pytest.approx(28.8) and eth["notional_usd"] == pytest.approx(28.8 * 2345.12)
        assert sum(r["notional_usd"] for r in rows) < 100_000  # the old contracts-as-coins parse gave ~$2.1M
    finally:
        await rt.aclose()


async def test_okx_job_fails_without_contract_values(state, tmp_path, ws_replay):
    # Instruments unreachable: the run fails (the scheduler logs it and retries) and no WebSocket is opened, so no
    # liquidation is stored with its size in contracts.
    rt = await _job_rt(state, tmp_path)
    InstrumentsEx.down = True
    try:
        with pytest.raises(ProviderError):
            await sched_mod._collect_liquidations_okx(rt)
        assert ws_replay == []
        assert await rt.store.liquidations_in_window(since_ts=0, until_ts=2_000_000_000) == []
    finally:
        await rt.aclose()


async def test_bybit_job_stores_flipped_sides(state, tmp_path, ws_replay):
    rt = await _job_rt(state, tmp_path)
    try:
        assert await sched_mod._collect_liquidations_bybit(rt) == 33
        assert ws_replay[0][1] == {
            "op": "subscribe",
            "args": ["allLiquidation.BTCUSDT", "allLiquidation.ETHUSDT", "allLiquidation.SOLUSDT"],
        }
        rows = await rt.store.liquidations_in_window(since_ts=0, until_ts=2_000_000_000, venue="bybit")
        assert sum(r["side"] == "sell" for r in rows) == 26  # S=Buy (long liquidated) -> "sell"
    finally:
        await rt.aclose()


# ---------- MCP summary tool ----------


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, cache_scale=0.0, db_path=str(tmp_path / "liq.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    assert rt.store is not None
    await rt.store.init()
    return rt


async def test_liquidations_summary_empty_window(state, tmp_path):
    rt = await _rt(state, tmp_path)
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "liquidations_summary", window_minutes=10)
        assert env["ok"] is True
        d = env["data"]
        assert d["count"] == 0 and d["total_notional_usd"] == 0.0
    finally:
        await rt.aclose()


async def test_liquidations_summary_aggregates(state, tmp_path):
    rt = await _rt(state, tmp_path)
    now_ts = int(NOW.timestamp())
    # Three liquidations in the trailing 10 minutes: two longs flushed, one short squeezed.
    await rt.store.insert_liquidations(
        [
            {
                "ts": now_ts - 60,
                "venue": "binanceusdm",
                "symbol": "BTCUSDT",
                "side": "sell",
                "qty": 1.0,
                "price": 60_000.0,
                "notional_usd": 60_000.0,
            },
            {
                "ts": now_ts - 120,
                "venue": "binanceusdm",
                "symbol": "BTCUSDT",
                "side": "sell",
                "qty": 2.0,
                "price": 60_000.0,
                "notional_usd": 120_000.0,
            },
            {
                "ts": now_ts - 90,
                "venue": "binanceusdm",
                "symbol": "ETHUSDT",
                "side": "buy",
                "qty": 10.0,
                "price": 3_000.0,
                "notional_usd": 30_000.0,
            },
        ]
    )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "liquidations_summary", window_minutes=10, top_n=5)
        d = env["data"]
        assert d["count"] == 3
        assert d["total_notional_usd"] == 210_000
        assert d["sell_notional_usd"] == 180_000
        assert d["buy_notional_usd"] == 30_000
        # imbalance = (buy - sell) / total = -150000 / 210000 ≈ -0.7143
        assert d["imbalance"] == pytest.approx(-0.7143, abs=1e-4)
        syms = {s["symbol"] for s in d["per_symbol"]}
        assert syms == {"BTCUSDT", "ETHUSDT"}
        # Top entry should be the biggest sell print.
        assert d["top"][0]["symbol"] == "BTCUSDT" and d["top"][0]["notional_usd"] == 120_000
        assert any("lower bound" in w for w in env["warnings"])  # Binance: one liquidation per symbol per second
    finally:
        await rt.aclose()


async def test_liquidations_summary_warns_okx_lower_bound(state, tmp_path):
    rt = await _rt(state, tmp_path)
    now_ts = int(NOW.timestamp())
    row = {
        "ts": now_ts - 30,
        "venue": "okx",
        "symbol": "ETH-USDT-SWAP",
        "side": "buy",
        "qty": 28.8,
        "price": 2345.12,
        "notional_usd": 67_539.5,
    }
    await rt.store.insert_liquidations([row])
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "liquidations_summary", window_minutes=10)
        assert env["data"]["count"] == 1 and env["data"]["buy_notional_usd"] == 67_540
        assert any("OKX" in w for w in env["warnings"]) and not any("Binance" in w for w in env["warnings"])
    finally:
        await rt.aclose()


async def test_liquidations_summary_filters_by_symbol(state, tmp_path):
    rt = await _rt(state, tmp_path)
    now_ts = int(NOW.timestamp())
    await rt.store.insert_liquidations(
        [
            {
                "ts": now_ts - 30,
                "venue": "binanceusdm",
                "symbol": "BTCUSDT",
                "side": "sell",
                "qty": 1.0,
                "price": 60_000.0,
                "notional_usd": 60_000.0,
            },
            {
                "ts": now_ts - 30,
                "venue": "binanceusdm",
                "symbol": "ETHUSDT",
                "side": "buy",
                "qty": 10.0,
                "price": 3_000.0,
                "notional_usd": 30_000.0,
            },
        ]
    )
    srv, _ = build_server(rt)
    try:
        env = await _call(srv, "liquidations_summary", window_minutes=10, symbol="BTCUSDT")
        d = env["data"]
        assert d["count"] == 1 and d["sell_notional_usd"] == 60_000
    finally:
        await rt.aclose()
