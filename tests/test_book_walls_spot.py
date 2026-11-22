"""Order-book walls collector, spot venues (IDEA-20261114-book-walls item 1, second slice): Binance spot and Coinbase
(Advanced Trade) parsers on live samples, spot diff sequencing on live ids, the Coinbase sequence check and frame plumbing
with fake sockets, and the two jobs with fake streams. No network."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import tradedesk.providers.liquidations as liq
import tradedesk.providers.orderbook_stream as obs
from test_book_walls import BTC_ASKS, BTC_BIDS, T0, _drain, _rt
from tradedesk.analytics import book_walls as bw
from tradedesk.analytics.book_walls import DiffSequencer
from tradedesk.collector.bad_rows import count_bad_rows
from tradedesk.collector.book_walls import BookWallsJob
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.envelope import Evidence, Fetched
from tradedesk.core.errors import ProviderError
from tradedesk.providers.liquidations import StreamStats

FX = json.loads((Path(__file__).parent / "fixtures" / "book_walls_spot_live_2026-11-18.json").read_text())
CB = FX["coinbase"]


def _copy(x):
    return json.loads(json.dumps(x))


# ---------- pure helpers ----------


def test_spot_diff_sequencing_on_live_ids():
    # Live 2026-11-18: the REST snapshot (L = ...832) falls inside the third diff; later diffs chain by U = u + 1.
    s = DiffSequencer(spot=True)
    s.snapshot(FX["binance_spot_rest_depth"]["lastUpdateId"])
    verdicts = [s.check(first, last, None) for first, last in FX["binance_spot_depth_ids"]]
    assert verdicts == ["skip", "skip", "apply", "apply", "apply", "apply", "apply"]
    assert s.last == FX["binance_spot_depth_ids"][-1][1]
    assert s.check(s.last + 2, s.last + 5, None) == "gap"  # an update missed


def test_spot_first_diff_may_start_right_after_the_snapshot():
    spot, perp = DiffSequencer(spot=True), DiffSequencer()
    spot.snapshot(100)
    perp.snapshot(100)
    assert spot.check(95, 100, None) == "skip"  # u <= L: already in the snapshot
    assert spot.check(101, 105, None) == "apply"  # U = L + 1: the usual case on spot ...
    assert perp.check(101, 105, 100) == "gap"  # ... which the USD-M rule (U <= L <= u) would call a gap
    assert spot.check(106, 110, None) == "apply" and spot.check(112, 115, None) == "gap"


def test_coinbase_symbols_minimums_and_the_full_book_cut():
    assert obs.coinbase_product("BTCUSDT") == "BTC-USD" and obs.coinbase_product("eth-usd") == "ETH-USD"
    assert bw.min_qty_for("BTC-USD") == bw.MIN_QTY["BTC"] and bw.min_qty_for("SOL-USD") is None
    assert bw.FULL_BOOK_KEEP == pytest.approx(0.025)
    bids = [(99.0, 1.0), (97.6, 1.0), (97.4, 1.0), (50.0, 9.0)]
    asks = [(101.0, 1.0), (102.4, 1.0), (102.6, 1.0), (1000.0, 9.0)]
    nb, na = bw.near_mid(bids, asks, 0.025)  # mid 100: keep 97.5 .. 102.5
    assert nb == [(99.0, 1.0), (97.6, 1.0)] and na == [(101.0, 1.0), (102.4, 1.0)]
    assert bw.near_mid([(101.0, 1.0)], asks, 0.025) is None  # crossed: no mid, the caller refuses the snapshot
    assert bw.near_mid([], asks, 0.025) is None
    assert bw.near_mid(bids, [(101.0, 0.0)], 0.025) is None  # only removed levels on a side: empty too


def test_iso_ms():
    assert obs.iso_ms("2026-11-18T19:13:06.400782747Z") == 1_795_029_186_400  # nanoseconds cut to ms
    assert obs.iso_ms("2026-11-18T19:13:05.9185Z") == 1_795_029_185_918
    assert obs.iso_ms("2026-11-18T19:13:05.5Z") == 1_795_029_185_500
    assert obs.iso_ms("2026-11-18T19:13:05Z") == 1_795_029_185_000
    for bad in ("2026-11-18T19:13:05", "2026-11-18 19:13:05Z", "2026-11-18T19:13:05.x1Z", None, 5):
        with pytest.raises((ValueError, TypeError)):
            obs.iso_ms(bad)


# ---------- parsers on the live samples ----------


def test_parse_binance_spot_live():
    d = obs.parse_binance_spot(FX["binance_spot_depth"][0])
    assert d["kind"] == "depth" and d["symbol"] == "ETHUSDT" and d["pu"] is None and d["ts"] == 1795029174015  # E: no T
    assert (d["U"], d["u"]) == (83554258017, 83554258341) and d["bids"][1] == (1761.03, 0.0) and d["asks"][0] == (1761.06, 4.9572)
    buy, sell = (obs.parse_binance_spot(m) for m in FX["binance_spot_agg_trade"])
    assert buy == {"kind": "trade", "symbol": "ETHUSDT", "ts": 1795029174489, "price": 1761.06, "qty": 0.7375, "side": "buy"}
    assert sell["side"] == "sell"  # m = true: the buyer was the maker
    assert obs.parse_binance_spot({"stream": "x", "data": {"e": "kline"}}) is None


def test_parse_coinbase_live():
    (snap,) = obs.parse_coinbase(CB["l2_data:snapshot"])
    assert snap["kind"] == "snapshot" and snap["symbol"] == "ETH-USD" and snap["u"] is None
    assert snap["ts"] == obs.iso_ms("2026-11-18T19:13:06.227597Z")  # the update's event time, not the send time
    assert snap["bids"][0] == (1758.9, 0.35903) and snap["asks"][0] == (1759.15, 0.85332484) and len(snap["asks"]) == 4
    (upd,) = obs.parse_coinbase(CB["l2_data:update"])
    assert upd["kind"] == "delta" and upd["bids"] == [] and upd["asks"][1] == (1759.19, 6.12142642)
    for quiet in ("subscriptions", "heartbeats", "market_trades:snapshot"):  # the trade snapshot predates the book
        assert obs.parse_coinbase(CB[quiet]) == []
    t1, t2 = obs.parse_coinbase(CB["market_trades:update"])
    # 'SELL' at 1759.15, the best offer of the snapshot: a resting sell was lifted, i.e. a taker buy
    assert t1 == {"kind": "trade", "symbol": "ETH-USD", "ts": obs.iso_ms("2026-11-18T19:13:06.978795Z"), "price": 1759.15,
                  "qty": 0.12457616, "side": "buy"}  # fmt: skip
    assert t2["side"] == "buy"


def test_parse_coinbase_drops_and_counts_malformed_messages():
    bad_side = _copy(CB["l2_data:update"])
    bad_side["events"][0]["updates"][0]["side"] = "ask"
    bad_qty = _copy(CB["l2_data:snapshot"])
    bad_qty["events"][0]["updates"][0]["new_quantity"] = "-1"
    bad_trade = _copy(CB["market_trades:update"])
    bad_trade["events"][0]["trades"][0]["side"] = "HOLD"
    no_events = {"channel": "l2_data", "sequence_num": 5}
    with count_bad_rows() as n:
        assert obs.parse_coinbase(bad_side) == [] and obs.parse_coinbase(bad_qty) == [] and obs.parse_coinbase(no_events) == []
        assert len(obs.parse_coinbase(bad_trade)) == 1  # the other trade is kept
    assert n.n == 4


def test_coinbase_sequence_check():
    seq = obs.CoinbaseSequence()
    assert [seq({"sequence_num": n}) for n in (0, 1, 2)] == [None, None, None]
    assert seq({"sequence_num": 4}) == "coinbase sequence_num 2 -> 4"
    assert seq({"sequence_num": 0}) is None and seq({"sequence_num": 1}) is None  # a new connection counts from 0
    assert seq({"no": "seq"}) is None and seq("pong") is None
    err = seq(CB["exchange_level2_error"])
    assert err.startswith("coinbase error: Failed to subscribe: level2, level3, and full channels now require authentication")


# ---------- stream plumbing (fake sockets, no network) ----------


class _RecWS:
    def __init__(self, frames, sent):
        self.frames, self.sent = list(frames), sent

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        if not self.frames:
            await asyncio.Event().wait()
        return json.dumps(self.frames.pop(0))


class _RecConnect:
    def __init__(self, script):
        self.script, self.sent, self.kw = list(script), [], []

    def __call__(self, url, **kw):
        self.kw.append(kw)
        return self._cm(self.script.pop(0) if self.script else [])

    @contextlib.asynccontextmanager
    async def _cm(self, frames):
        yield _RecWS(frames, self.sent)


def _seq(msg, n):
    m = _copy(msg)
    m["sequence_num"] = n
    return m


async def test_coinbase_sequence_gap_drops_the_connection_and_resubscribes():
    snap, upd = CB["l2_data:snapshot"], CB["l2_data:update"]
    fake = _RecConnect([[_seq(snap, 0), _seq(upd, 1), _seq(upd, 3), _seq(upd, 4)], [_seq(snap, 0)]])
    subs = [{"type": "subscribe", "channel": "level2"}, {"type": "subscribe", "channel": "heartbeats"}]
    st = StreamStats()
    rows = await _drain(
        liq._ws_consume(
            "wss://cb",
            subs,
            obs.parse_coinbase,
            max_seconds=0.3,
            stats=st,
            error_of=obs.CoinbaseSequence(),
            connect=fake,
            backoff_base=0.01,
            lifecycle="book",
            label="book coinbase:book",
        )  # fmt: skip
    )
    assert [r["kind"] for r in rows] == ["_open", "snapshot", "delta", "_drop", "_open", "snapshot"]
    assert "coinbase sequence_num 1 -> 3" in rows[3]["error"] and st.failures == 1 and st.reconnects == 1
    assert fake.sent == subs * 2  # every subscribe message, in order, on each connection


async def test_spot_streams_subscribe_and_name_their_feeds(monkeypatch):
    seen = []

    async def fake(url, subscribe, parser, **kw):
        seen.append((url, subscribe, parser, kw))
        yield {"kind": "_open", "feed": kw["lifecycle"]}

    monkeypatch.setattr(liq, "_ws_consume", fake)
    g = obs.StreamGroup()
    await _drain(obs.binance_spot_book_stream(["BTCUSDT", "ETHUSDT"], max_seconds=1, stats=g))
    url, sub, parser, kw = seen.pop()
    assert url == (
        "wss://stream.binance.com:9443/stream?streams=btcusdt@depth@1000ms/ethusdt@depth@1000ms/btcusdt@aggTrade/ethusdt@aggTrade"
    )
    assert sub is None and parser is obs.parse_binance_spot and kw["label"] == "book binance_spot:book" and kw["lifecycle"] == "book"
    await _drain(obs.coinbase_book_stream(["BTC-USD", "ETH-USD"], max_seconds=1, stats=g))
    url, sub, parser, kw = seen.pop()
    assert url == "wss://advanced-trade-ws.coinbase.com" and parser is obs.parse_coinbase and kw["label"] == "book coinbase:book"
    assert [(s.get("channel"), s.get("product_ids")) for s in sub] == [
        ("level2", ["BTC-USD", "ETH-USD"]), ("market_trades", ["BTC-USD", "ETH-USD"]), ("heartbeats", None),
    ]  # fmt: skip
    assert isinstance(kw["error_of"], obs.CoinbaseSequence) and kw["max_size"] == obs.COINBASE_MAX_FRAME > 4_200_000
    assert "heartbeat" not in kw  # Coinbase gets the heartbeats channel, not an application ping
    assert set(g.feeds) == {"book"}


async def test_ws_consume_passes_the_frame_cap_only_when_asked():
    big, plain = _RecConnect([[{"k": 1}]]), _RecConnect([[{"k": 1}]])
    await _drain(liq._ws_consume("wss://x", None, lambda ev: ev, max_seconds=0.05, connect=big, max_size=obs.COINBASE_MAX_FRAME))
    await _drain(liq._ws_consume("wss://x", None, lambda ev: ev, max_seconds=0.05, connect=plain))
    assert big.kw[0]["max_size"] == obs.COINBASE_MAX_FRAME and "max_size" not in plain.kw[0]  # liquidations: unchanged


async def test_binance_spot_depth_endpoint_on_the_live_sample():
    from tradedesk.core.cache import TTLCache  # noqa: PLC0415
    from tradedesk.core.http import Http  # noqa: PLC0415
    from tradedesk.providers.binance_spot import BinanceSpot  # noqa: PLC0415

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json=FX["binance_spot_rest_depth"])

    bs = BinanceSpot(Http("binance_spot", "https://api.binance.com", cache=TTLCache(), transport=httpx.MockTransport(handler)))
    f = await bs.depth("ETHUSDT")
    assert f.data["lastUpdateId"] == 83554258832 and seen["url"].endswith("/api/v3/depth?symbol=ETHUSDT&limit=1000")

    def broken(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -1121, "msg": "Invalid symbol."})

    bad = BinanceSpot(Http("binance_spot", "https://api.binance.com", cache=TTLCache(), transport=httpx.MockTransport(broken)))
    with pytest.raises(ProviderError):
        await bad.depth("NOPE")


# ---------- collector jobs (fake streams) ----------


def _fake_stream(monkeypatch, name: str, rows: list[dict]):
    async def fake(symbols, *, max_seconds, stats):
        for r in rows:
            yield r

    monkeypatch.setattr(obs, name, fake)


def _spot_diff(ts, first, last, bids=()):
    return {"kind": "depth", "symbol": "BTCUSDT", "ts": ts, "U": first, "u": last, "pu": None, "bids": list(bids), "asks": []}


async def test_binance_spot_job_seeds_from_the_spot_snapshot(state, tmp_path, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")
    wall = (62_950.0, 40.0)
    snap = {"lastUpdateId": 100, "bids": [[str(p), str(q)] for p, q in [*BTC_BIDS, wall]], "asks": [[str(p), str(q)] for p, q in BTC_ASKS]}
    calls = []

    async def spot_depth(symbol, limit=1000):
        calls.append(symbol)
        if len(calls) == 1:
            raise ProviderError("binance_spot", "network", "timed out")
        return Fetched(snap, Evidence(provider="binance_spot", tool="api/v3/depth"))

    async def perp_depth(symbol, limit=1000):
        raise AssertionError("the spot job must not read the USD-M book")

    rows = [
        {"kind": "_open", "feed": "book"},
        _spot_diff(T0, 90, 95),  # snapshot fails: retried 5 s later
        _spot_diff(T0 + 5_000, 101, 105),  # seeded (L = 100); U = L + 1 -> applied
        {"kind": "trade", "symbol": "BTCUSDT", "ts": T0 + 5_100, "price": 62_950.0, "qty": 30.0, "side": "sell"},
        _spot_diff(T0 + 6_000, 106, 107, bids=[(62_950.0, 0.0)]),
        _spot_diff(T0 + 8_000, 109, 110),  # U != 108: resync
        {"kind": "trade", "symbol": "BTCUSDT", "ts": T0 + 8_500, "price": 1.0, "qty": 1.0, "side": "buy"},  # grace over
    ]
    _fake_stream(monkeypatch, "binance_spot_book_stream", rows)
    rt = await _rt(state, tmp_path)
    monkeypatch.setattr(rt.binance_spot, "depth", spot_depth)
    monkeypatch.setattr(rt.binance_futures, "depth", perp_depth)
    job = BookWallsJob("binance_spot", ["BTCUSDT"])
    try:
        await job(rt)
        # failed at T0, seeded at T0 + 5 s; after the resync at T0 + 8 s the next fetch waits for T0 + 10 s
        assert calls == ["BTCUSDT"] * 2 and job.resyncs == 1 and "book walls binance_spot BTCUSDT: depth snapshot failed" in caplog.text
        (e,) = await rt.store.book_wall_events()
        assert (e["venue"], e["symbol"], e["reason"], e["traded"]) == ("binance", "BTCUSDT", "consumed", 30.0)
        assert set(job.stats.feeds) == {"book"}
    finally:
        await rt.aclose()


async def test_coinbase_job_cuts_the_full_book_and_stores_a_wall(state, tmp_path, monkeypatch):
    wall = (62_850.0, 40.0)  # 0.25 % below the mid 63005
    rows = [
        {"kind": "_open", "feed": "book"},
        {
            "kind": "snapshot",
            "symbol": "BTC-USD",
            "ts": T0,
            "u": None,
            "bids": [*BTC_BIDS, wall, (60_000.0, 100.0)],
            "asks": [*BTC_ASKS, (66_000.0, 5.0)],
        },  # fmt: skip
        {"kind": "trade", "symbol": "BTC-USD", "ts": T0 + 100, "price": 62_850.0, "qty": 3.0, "side": "sell"},
        {"kind": "delta", "symbol": "BTC-USD", "ts": T0 + 7_000, "u": None, "bids": [(62_850.0, 0.0)], "asks": []},
        {"kind": "delta", "symbol": "BTC-USD", "ts": T0 + 61_000, "u": None, "bids": [(59_000.0, 50.0)], "asks": [(63_010.0, 2.0)]},
    ]
    _fake_stream(monkeypatch, "coinbase_book_stream", rows)
    rt = await _rt(state, tmp_path)
    job = BookWallsJob("coinbase", ["BTCUSDT", "ETHUSDT", "DOGEUSDT"])
    assert job.symbols == ["BTC-USD", "ETH-USD"]  # products; no coin minimum for DOGE
    try:
        assert await job(rt) == 3  # 1 wall + 2 minute rows
        (e,) = await rt.store.book_wall_events()
        assert (e["venue"], e["symbol"], e["price"], e["reason"], e["traded"], e["gaps"]) == (
            "coinbase",
            "BTC-USD",
            62_850.0,
            "withdrawn",
            3.0,
            0,
        )
        book = job.books["BTC-USD"]
        assert 60_000.0 not in book.bids and 66_000.0 not in book.asks  # beyond FULL_BOOK_KEEP of the mid: cut
        assert 59_000.0 not in book.bids and book.asks[63_010.0] == 2.0  # updates outside the kept range are dropped
        assert job.resyncs == 0  # no u on Coinbase rows: no Bybit-style sequence check
        depth = await rt.store.book_depth_rows()
        assert depth[0]["venue"] == "coinbase" and depth[0]["bid_view"] == pytest.approx((63_005 - 62_710) / 63_005)
    finally:
        await rt.aclose()


async def test_coinbase_job_refuses_a_snapshot_without_a_mid(state, tmp_path, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")
    snap = {"kind": "snapshot", "symbol": "BTC-USD", "u": None}
    rows = [
        {"kind": "_open", "feed": "book"},
        snap | {"ts": T0, "bids": BTC_BIDS, "asks": BTC_ASKS},
        snap | {"ts": T0 + 1_000, "bids": [*BTC_BIDS, (63_100.0, 1.0)], "asks": [*BTC_ASKS, (50_000.0, 5.0)]},  # crossed
        {"kind": "delta", "symbol": "BTC-USD", "ts": T0 + 1_500, "u": None, "bids": [(62_995.0, 1.0)], "asks": []},
        snap | {"ts": T0 + 2_000, "bids": BTC_BIDS, "asks": [(63_010.0, 0.0)]},  # an empty side
        snap | {"ts": T0 + 3_000, "bids": BTC_BIDS, "asks": BTC_ASKS},  # a good one is taken again
    ]
    _fake_stream(monkeypatch, "coinbase_book_stream", rows)
    rt = await _rt(state, tmp_path)
    job = BookWallsJob("coinbase", ["BTCUSDT"])
    try:
        with count_bad_rows() as n:
            await job(rt)
        assert n.n == 2 and caplog.text.count("snapshot without a mid refused") == 2
        # the T0 minute row reset the count; the refused snapshot dropped a live book: one depth break, the empty one none
        assert job.trackers["BTC-USD"].gaps_since_row == 1
        book = job.books["BTC-USD"]
        assert book.ready and 50_000.0 not in book.asks and 62_995.0 not in book.bids  # the delta waited for a book
        assert len(book.bids) == len(BTC_BIDS) and len(book.asks) == len(BTC_ASKS)
    finally:
        await rt.aclose()


async def test_coinbase_job_run_fails_when_its_stream_is_down(state, tmp_path, monkeypatch):
    async def down(symbols, *, max_seconds, stats):
        raise TimeoutError("timed out during opening handshake")
        yield  # pragma: no cover

    monkeypatch.setattr(obs, "coinbase_book_stream", down)
    rt = await _rt(state, tmp_path)
    try:
        with pytest.raises(TimeoutError):  # the scheduler counts a failed run; no tool is involved
            await BookWallsJob("coinbase", ["BTCUSDT"])(rt)
    finally:
        await rt.aclose()


def test_all_four_venues_by_default_still_off():
    s = Settings()
    assert s.collect_book_walls_enabled is False and s.collect_book_walls_venues == "binance,bybit,binance_spot,coinbase"
    base = dataclasses.replace(Settings(), collector_enabled=True)
    rt = SimpleNamespace(settings=dataclasses.replace(base, collect_book_walls_enabled=True), store=object())
    jobs = [j for j in build_default_scheduler(rt).jobs if j.name.startswith("book_walls")]
    assert [j.name for j in jobs] == ["book_walls:binance", "book_walls:bybit", "book_walls:binance_spot", "book_walls:coinbase"]
    assert [set(j.metrics.stream.as_dict()) for j in jobs[2:]] == [{"book"}, {"book"}]
    with pytest.raises(ValueError):
        BookWallsJob("kraken", ["BTCUSDT"])
