from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import itertools
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import ccxt
import pytest
from mcp import Client

import tradedesk.providers.liquidations as liq
from conftest import NOW, FakeEx, make_transports, wait_until
from tradedesk.collector import scheduler as sched_mod
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.providers.liquidations import (
    StreamStats,
    backoff_delay,
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

    async def fake(url, subscribe, parser, *, max_seconds, **_):
        opened.append((url, subscribe))
        for msg in WS["okx_messages"] if "okx" in url else WS["bybit_messages"]:
            for row in parser(msg) or []:
                yield row

    monkeypatch.setattr(liq, "_ws_consume", fake)
    InstrumentsEx.down, InstrumentsEx.calls = False, []
    return opened


async def _job_rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "liq.sqlite")),
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


# ---------- stream reconnect (fake WebSocket, no network) ----------


class _FakeWS:
    """One connection: ``frames`` are returned by recv() in order (an exception is raised instead); then it is quiet."""

    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if not self.frames:
            await asyncio.Event().wait()  # quiet stream: blocks until the consumer's own timeout
        f = self.frames.pop(0)
        if isinstance(f, BaseException):
            raise f
        return f if isinstance(f, str) else json.dumps(f)


class _FakeConnect:
    """Stands in for ``websockets.connect``: each call takes the next step of ``script`` (an exception = failed
    handshake, a list = the frames of an open connection); after the script, ``default``."""

    def __init__(self, script, default=()):
        self.script, self.default, self.opened, self.kwargs = list(script), default, [], []

    def __call__(self, url, **kw):
        self.kwargs.append(kw)
        return self._cm(self.script.pop(0) if self.script else self.default)

    @contextlib.asynccontextmanager
    async def _cm(self, step):
        if isinstance(step, BaseException):
            raise step
        ws = _FakeWS(step)
        self.opened.append(ws)
        yield ws


async def _drain(gen) -> list[dict]:
    return [row async for row in gen]


def test_backoff_delay_doubles_with_jitter_and_cap():
    assert backoff_delay(0, 0.9) == 0.0
    assert backoff_delay(1, 0.0) == pytest.approx(0.5) and backoff_delay(1, 0.999) == pytest.approx(1.0, abs=1e-3)
    assert backoff_delay(4, 0.0) == pytest.approx(4.0)  # 1 x 2^3 x 0.5
    assert backoff_delay(10, 0.0) == pytest.approx(30.0) and backoff_delay(10, 0.999) <= 60.0  # capped at 60 s
    assert backoff_delay(10_000, 0.5) == pytest.approx(45.0)  # no overflow on a long outage


async def test_stream_reconnects_and_resubscribes_after_drops():
    # Live 2026-11-07: keepalive timeouts, handshake timeouts and resets. A drop no longer ends the run: the consumer
    # backs off, reconnects, sends the subscription again and keeps the rows of every connection.
    sub = {"op": "subscribe", "args": ["allLiquidation.BTCUSDT"]}
    ack = {"success": True, "ret_msg": "", "op": "subscribe"}
    fake = _FakeConnect(
        [
            [ack, WS["documented_bybit"], ConnectionResetError("Connection reset by peer")],
            TimeoutError("timed out during opening handshake"),
            [ack, WS["documented_bybit"]],
        ]
    )
    ticks = itertools.count(1_000)
    st = StreamStats(clock=lambda: float(next(ticks)))  # the collector passes rt.now
    rows = await _drain(
        liq._ws_consume(
            liq.BYBIT_WS_URL,
            sub,
            parse_bybit_liquidation,
            max_seconds=0.3,
            stats=st,
            error_of=liq._bybit_error,
            connect=fake,
            backoff_base=0.01,
        )
    )
    assert len(rows) == 2 and all(r["venue"] == "bybit" for r in rows)
    assert [json.loads(ws.sent[0]) for ws in fake.opened] == [sub, sub]  # resubscribed on the new connection
    # The last connection lived to the end of the window, so the backoff count starts over for the next run.
    assert (st.connects, st.reconnects, st.failures, st.consecutive_failures) == (2, 1, 2, 0)
    assert "opening handshake" in st.last_failure and st.last_failure_ts is not None
    assert st.connected is False and st.last_row_ts is not None and st.last_message_ts >= st.last_row_ts
    assert st.last_connect_ts > st.last_failure_ts  # stamps come from the injected clock only
    assert StreamStats().stamp() is None and "clock" not in st.as_dict()
    kw = fake.kwargs[0]
    assert (kw["open_timeout"], kw["ping_interval"], kw["ping_timeout"]) == (10.0, 20.0, 30.0)


async def test_stream_down_for_the_whole_window_fails_the_run_after_backing_off():
    fake = _FakeConnect([], default=TimeoutError("timed out during opening handshake"))
    st = StreamStats()
    with pytest.raises(TimeoutError, match="opening handshake"):
        await _drain(liq._ws_consume(liq.OKX_WS_URL, None, lambda ev: [], max_seconds=0.2, stats=st, connect=fake, backoff_base=0.02))
    # Backoff 0.02 x 2^(n-1) x [0.5, 1): a handful of attempts in 0.2 s, not a tight loop.
    assert 2 <= st.failures == len(fake.kwargs) <= 6
    assert st.connects == 0 and st.consecutive_failures == st.failures
    # The count carries over to the next run, whose first attempt waits (at most half the window) and still happens.
    fake.default = []
    assert await _drain(liq._ws_consume(liq.OKX_WS_URL, None, lambda ev: [], max_seconds=0.2, stats=st, connect=fake)) == []
    assert st.connects == 1 and st.reconnects == 1


async def test_stream_venue_error_message_counts_as_failure_and_reconnects():
    # A rejected Bybit subscription used to leave a silent connection for the whole window.
    bad = {"success": False, "ret_msg": "error:handler not found", "op": "subscribe"}
    fake = _FakeConnect([[bad], [{"success": True, "op": "subscribe"}, WS["documented_bybit"]]])
    st = StreamStats()
    rows = await _drain(
        liq._ws_consume(
            liq.BYBIT_WS_URL,
            {"op": "subscribe"},
            parse_bybit_liquidation,
            max_seconds=0.2,
            stats=st,
            error_of=liq._bybit_error,
            connect=fake,
            backoff_base=0.01,
        )
    )
    assert len(rows) == 1 and st.failures == 1 and st.reconnects == 1
    assert st.last_failure.startswith("StreamError: bybit subscribe: error:handler not found")
    assert liq._okx_error({"event": "error", "code": "60012", "msg": "Invalid request"}) == "okx error 60012: Invalid request"
    assert liq._okx_error(WS["okx_messages"][0]) is None  # the port notice is not an error


async def test_stream_sends_heartbeat_when_quiet_and_resets_backoff_once_stable():
    # OKX closes a connection that pushed nothing for 30 s unless it gets "ping"; its "pong" is not JSON.
    fake = _FakeConnect([ConnectionResetError("reset"), ["pong"]])
    st = StreamStats()
    rows = await _drain(
        liq._ws_consume(
            liq.OKX_WS_URL,
            {"op": "subscribe"},
            lambda ev: [],
            max_seconds=0.3,
            stats=st,
            heartbeat="ping",
            connect=fake,
            backoff_base=0.01,
            stable_seconds=0.05,
            heartbeat_seconds=0.03,
        )
    )
    ws = fake.opened[0]
    assert rows == [] and ws.sent[0] == '{"op": "subscribe"}' and ws.sent.count("ping") >= 3
    assert st.failures == 1 and st.consecutive_failures == 0  # the connection stayed up past stable_seconds


async def test_stream_up_to_the_end_of_a_short_window_resets_backoff():
    # Default window 60 s = STABLE_SECONDS: a connection never lives stable_seconds inside one run, so the count used
    # to stay >= 1 after the first failure and every later run began with a backoff pause and a phantom reconnect.
    fake = _FakeConnect([], default=[])
    st = StreamStats(consecutive_failures=3)
    for _ in range(2):
        await _drain(
            liq._ws_consume(
                liq.OKX_WS_URL, None, lambda ev: [], max_seconds=0.1, stats=st, connect=fake, backoff_base=0.01, stable_seconds=0.6
            )
        )
        assert st.consecutive_failures == 0
    assert (st.connects, st.reconnects, st.failures) == (2, 1, 0)  # only the first run followed a failure


async def test_stream_parser_bug_is_not_counted_as_a_dropped_link():
    def parser(ev):
        raise KeyError("bug")

    fake = _FakeConnect([[{"data": []}]])
    st = StreamStats()
    with pytest.raises(KeyError):
        await _drain(liq._ws_consume(liq.OKX_WS_URL, None, parser, max_seconds=0.2, stats=st, connect=fake))
    assert st.failures == 0 and st.connects == 1 and st.connected is False


# ---------- hourly connection journal (liquidation_stream_hour) ----------

H0 = 1_794_000_000 // 3600 * 3600  # a UTC hour start (2026-11)


class _Clock:
    """The injected clock (``rt.now`` in the collector), moved only by the fake stream below."""

    def __init__(self, t: float):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


class _TimedWS(_FakeWS):
    """Frames given as ``(seconds, frame)`` move the fake clock by ``seconds`` before recv() returns (or raises) them."""

    def __init__(self, frames, clock):
        super().__init__(frames)
        self.clock = clock

    async def recv(self):
        if self.frames and isinstance(self.frames[0], tuple):
            dt, frame = self.frames.pop(0)
            self.clock.t += dt
            self.frames.insert(0, frame)
        return await super().recv()


class _TimedConnect(_FakeConnect):
    """``(seconds, exc)`` = a handshake that fails after ``seconds`` of the fake clock; a list = a timed connection."""

    def __init__(self, script, clock):
        super().__init__(script)
        self.clock = clock

    @contextlib.asynccontextmanager
    async def _cm(self, step):
        if isinstance(step, tuple):
            self.clock.t += step[0]
            raise step[1]
        ws = _TimedWS(step, self.clock)
        self.opened.append(ws)
        yield ws


ACK = {"success": True, "ret_msg": "", "op": "subscribe"}


async def test_stream_hours_drop_mid_window_lands_in_its_hour():
    # Window from H0+50:00: up 500 s (2 frames, 1 row), reset at H0+58:20; the next handshake fails at H1+03:20; the
    # third connection opens then and gets one frame 600 s later before the window ends.
    clock = _Clock(H0 + 3000)
    fake = _TimedConnect(
        [
            [(100, ACK), (200, WS["documented_bybit"]), (200, ConnectionResetError("reset"))],
            (300, TimeoutError("timed out during opening handshake")),
            [(600, ACK)],
        ],
        clock,
    )
    st = StreamStats(clock=clock)
    rows = await _drain(
        liq._ws_consume(liq.BYBIT_WS_URL, None, parse_bybit_liquidation, max_seconds=0.3, stats=st, connect=fake, backoff_base=0.01)
    )
    assert len(rows) == 1 and (st.frames, st.rows, st.failures, st.reconnects) == (3, 1, 2, 1)
    hours = st.take_hours()
    h1 = H0 + 3600
    assert hours[H0] == {"connected_s": 500, "frames": 2, "rows": 1, "failures": 1, "reconnects": 0}
    # The drop: H1 is connected only from 03:20 to 13:20 (600 s of 3600 = 17 %), with the failed handshake in it.
    assert hours[h1] == {"connected_s": 600, "frames": 1, "rows": 0, "failures": 1, "reconnects": 1}
    assert st.take_hours() == {} and st.connected_since is None  # taken once; nothing left open
    assert "hours" not in st.as_dict() and st.as_dict()["frames"] == 3  # system_health gets totals, not the tally


async def test_stream_hours_without_clock_stay_empty():
    fake = _FakeConnect([[ACK, WS["documented_bybit"]]])
    st = StreamStats()
    await _drain(liq._ws_consume(liq.BYBIT_WS_URL, None, parse_bybit_liquidation, max_seconds=0.1, stats=st, connect=fake))
    assert (st.frames, st.rows) == (2, 1) and st.take_hours() == {}


def test_stream_hours_open_connection_is_credited_up_to_now_once():
    clock = _Clock(H0 + 3500)
    st = StreamStats(clock=clock, connected_since=clock.t)
    clock.t += 200
    assert st.take_hours() == {
        H0: {"connected_s": 100, "frames": 0, "rows": 0, "failures": 0, "reconnects": 0},
        H0 + 3600: {"connected_s": 100, "frames": 0, "rows": 0, "failures": 0, "reconnects": 0},
    }
    clock.t += 50
    st.close_segment()  # the rest of the connection, not counted twice
    assert st.take_hours()[H0 + 3600]["connected_s"] == 50


async def _journal(rt, exchange="bybit") -> dict[int, tuple]:
    cur = await rt.store.conn.execute(
        "SELECT hour_ts, connected_s, frames, rows, failures, reconnects FROM liquidation_stream_hour WHERE exchange = ?", (exchange,)
    )
    return {r[0]: tuple(r[1:]) for r in await cur.fetchall()}


async def test_collector_journals_hours_split_at_the_boundary_and_adds_on_rerun(state, tmp_path, monkeypatch):
    clock = _Clock(H0 + 3500)
    fake = _TimedConnect([[(200, ACK)], [(200, ACK), (0, WS["documented_bybit"])]], clock)
    consume = liq._ws_consume
    monkeypatch.setattr(liq, "_ws_consume", lambda *a, **kw: consume(*a, **kw, connect=fake))
    rt = await _job_rt(state, tmp_path)
    rt.settings = dataclasses.replace(
        rt.settings,
        collector_enabled=True,
        collect_liquidations_enabled=True,
        collect_liquidations_venues="bybit",
        collect_liquidations_window_seconds=0.15,
    )
    rt.now = lambda: datetime.fromtimestamp(clock.t, UTC)
    try:
        job = next(j for j in build_default_scheduler(rt).jobs if j.name == "liquidations:bybit")
        # Run 1: open at H0+58:20, one frame at H1+01:40, quiet to the end: 100 s in each hour.
        assert await job.fn(rt) == 0
        assert await _journal(rt) == {H0: (100.0, 0, 0, 0, 0), H0 + 3600: (100.0, 1, 0, 0, 0)}
        # Run 2 in the same hour: added to the stored row, not written over it.
        assert await job.fn(rt) == 1
        assert await _journal(rt) == {H0: (100.0, 0, 0, 0, 0), H0 + 3600: (300.0, 3, 1, 0, 0)}
        assert job.metrics.stream.hours == {}
    finally:
        await rt.aclose()


async def test_store_sums_every_counter_of_a_rewritten_hour(state, tmp_path):
    rt = await _job_rt(state, tmp_path)
    first = {"connected_s": 100.5, "frames": 3, "rows": 2, "failures": 1, "reconnects": 1}
    second = {"connected_s": 200.25, "frames": 5, "rows": 7, "failures": 2, "reconnects": 3}
    try:
        assert await rt.store.add_liquidation_stream_hours("bybit", {H0: first}) == 1
        assert await rt.store.add_liquidation_stream_hours("bybit", {H0: second, H0 + 3600: second}) == 2
        assert await rt.store.add_liquidation_stream_hours("bybit", {}) == 0
        journal = await _journal(rt)
        assert journal[H0][0] == pytest.approx(300.75) and journal[H0][1:] == (8, 9, 3, 4)  # summed, not overwritten
        assert journal[H0 + 3600][1:] == (5, 7, 2, 3)
        assert await _journal(rt, "okx") == {}  # keyed by exchange too
    finally:
        await rt.aclose()


async def test_collector_journals_a_failed_run_and_keeps_tallies_when_the_write_fails(state, tmp_path, caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")
    rt = await _job_rt(state, tmp_path)
    clock = _Clock(H0 + 600)
    st = StreamStats(clock=clock)

    async def down(_rt, stats):  # the venue refused every connection in the window
        stats.failures += 2
        stats.count("failures", clock.t)
        stats.count("failures", clock.t + 30)
        raise TimeoutError("timed out during opening handshake")

    try:
        with pytest.raises(TimeoutError):
            await sched_mod._liquidation_window(rt, exchange="okx", fn=down, stats=st)
        assert await _journal(rt, "okx") == {H0: (0.0, 0, 0, 2, 0)}

        async def ok(_rt, stats):
            stats.count("frames", clock.t)
            return 0

        async def broken(*_a, **_kw):
            raise sqlite3.OperationalError("database is locked")

        real = rt.store.add_liquidation_stream_hours
        rt.store.add_liquidation_stream_hours = broken
        assert await sched_mod._liquidation_window(rt, exchange="okx", fn=ok, stats=st) == 0  # the run's result stands
        assert st.hours[H0]["frames"] == 1 and "journal okx not written" in caplog.text
        rt.store.add_liquidation_stream_hours = real
        await sched_mod._liquidation_window(rt, exchange="okx", fn=ok, stats=st)
        assert await _journal(rt, "okx") == {H0: (0.0, 2, 0, 2, 0)}  # the kept tally is written by the next run
    finally:
        await rt.aclose()


def test_venue_error_text_is_clipped():
    long = "x" * 5000
    assert len(liq._bybit_error({"success": False, "op": "subscribe", "ret_msg": long})) <= liq.ERROR_TEXT_MAX
    assert len(liq._okx_error({"event": "error", "code": "1", "msg": long})) <= liq.ERROR_TEXT_MAX


async def test_bybit_and_okx_streams_pass_heartbeat_and_error_check(monkeypatch):
    seen = {}

    async def fake(url, subscribe, parser, **kw):
        seen[url] = kw
        for row in ():
            yield row

    monkeypatch.setattr(liq, "_ws_consume", fake)
    st = StreamStats()
    await _drain(liq.bybit_liquidation_stream(["BTCUSDT"], stats=st))
    await _drain(liq.okx_liquidation_stream({}, stats=st))
    await _drain(liq.binance_liquidation_stream(stats=st))
    assert json.loads(seen[liq.BYBIT_WS_URL]["heartbeat"]) == {"op": "ping"} and seen[liq.BYBIT_WS_URL]["error_of"] is liq._bybit_error
    assert seen[liq.OKX_WS_URL]["heartbeat"] == "ping" and seen[liq.OKX_WS_URL]["error_of"] is liq._okx_error
    assert all(kw["stats"] is st for kw in seen.values()) and "heartbeat" not in seen[liq.BINANCE_WS_URL]


async def test_liquidation_jobs_get_their_stream_stats_in_system_health(state, tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEDESK_COLLECT_LIQUIDATIONS_VENUES", raising=False)
    monkeypatch.setenv("TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED", "1")
    rt = Runtime(
        dataclasses.replace(Settings.from_env(), collector_enabled=True, db_path=str(tmp_path / "liq.sqlite")),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: NOW
    await rt.store.init()
    full = build_default_scheduler(rt)
    liq_jobs = [j for j in full.jobs if j.name.startswith("liquidations:")]
    assert len(liq_jobs) == 3 and len({id(j.metrics.stream) for j in liq_jobs}) == 3
    assert all(isinstance(j.metrics.stream, StreamStats) and j.fn.keywords["stats"] is j.metrics.stream for j in liq_jobs)
    assert all(j.metrics.stream.stamp() == NOW.timestamp() for j in liq_jobs)  # stamps follow rt.now
    # Only the bybit job is started (the real one would open a socket); its counters reach system_health.
    bybit = next(j for j in liq_jobs if j.name == "liquidations:bybit")
    bybit.metrics.stream.failures, bybit.metrics.stream.last_failure = 4, "ConnectionClosedError: keepalive ping timeout"

    async def quiet(_rt):
        return 0

    bybit.fn = quiet
    sched = sched_mod.Scheduler(rt, [bybit])
    monkeypatch.setattr("tradedesk.collector.build_default_scheduler", lambda _rt: sched)
    srv, _ = build_server(rt)
    try:
        data = await _call(srv, "system_health")
        rows = {j["name"]: j for j in data["data"]["collector"]}
        stream = rows["liquidations:bybit"]["stream"]
        assert stream["failures"] == 4 and "keepalive" in stream["last_failure"]
        assert {"connects", "reconnects", "consecutive_failures", "last_message_ts", "last_row_ts"} <= set(stream)
        assert "clock" not in stream
    finally:
        await sched.stop()
        await rt.aclose()


async def test_scheduler_survives_a_stray_cancelled_error(state, tmp_path):
    # A CancelledError raised inside a job (not a cancel of the job's task) used to end the loop for good, silently.
    rt = await _job_rt(state, tmp_path)
    outcomes: list = [asyncio.CancelledError("inner"), 3]

    async def job(_rt):
        out = outcomes.pop(0) if outcomes else 0
        if isinstance(out, BaseException):
            raise out
        return out

    sched = sched_mod.Scheduler(rt, [sched_mod.PeriodicJob(name="stray", period_seconds=0, fn=job)])
    try:
        await sched.start()
        await wait_until(lambda: sched.jobs[0].metrics.successes > 0)
        m = sched.jobs[0].metrics
        assert m.failures == 1 and m.last_failure == "CancelledError: inner" and m.stream is None
    finally:
        await sched.stop()  # a real cancel still stops it
        assert sched._tasks == []
        await rt.aclose()


@pytest.mark.parametrize("how", ["stop", "cancel"])
async def test_scheduler_cancel_while_a_job_is_running_exits_cleanly(state, tmp_path, how):
    # The cancel lands inside job.fn (a liquidation stream mid-window), not in the pause between runs.
    rt = await _job_rt(state, tmp_path)
    started = asyncio.Event()

    async def blocking(_rt):
        started.set()
        await asyncio.Event().wait()

    sched = sched_mod.Scheduler(rt, [sched_mod.PeriodicJob(name="blocking", period_seconds=0, fn=blocking)])
    try:
        await sched.start()
        await asyncio.wait_for(started.wait(), 5)
        if how == "stop":
            await asyncio.wait_for(sched.stop(), 5)
            assert sched._tasks == []
        else:  # a direct cancel of the task without _stop (event loop shutdown)
            task = sched._tasks[0]
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert task.cancelled()
        assert sched.jobs[0].metrics.failures == 0
    finally:
        await sched.stop()
        await rt.aclose()


# ---------- MCP summary tool ----------


async def _rt(state, tmp_path) -> Runtime:
    rt = Runtime(
        Settings(http_retries=1, http_retry_backoff=0.0, cache_scale=0.0, db_path=str(tmp_path / "liq.sqlite")),
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
