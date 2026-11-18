"""Order-book walls collector (IDEA-20261114-book-walls item 1): pure wall analytics on synthetic sequences, the stream
parsers on live samples, the stream plumbing with fake sockets, the store and the collector job with fake streams."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import tradedesk.providers.liquidations as liq
import tradedesk.providers.orderbook_stream as obs
from conftest import FakeEx, make_transports
from tradedesk.analytics import book_walls as bw
from tradedesk.analytics.book_walls import Book, DiffSequencer, WallTracker
from tradedesk.collector.bad_rows import count_bad_rows
from tradedesk.collector.book_walls import BookWallsJob
from tradedesk.collector.scheduler import build_default_scheduler
from tradedesk.config import Settings
from tradedesk.core.envelope import Evidence, Fetched
from tradedesk.core.errors import ProviderError
from tradedesk.features import allowed
from tradedesk.providers.liquidations import StreamStats
from tradedesk.runtime import Runtime
from tradedesk.store import SCHEMA_VERSION, Store
from tradedesk.store.schema import MIGRATIONS

FX = json.loads((Path(__file__).parent / "fixtures" / "book_walls_live_2026-11-18.json").read_text())
T0 = 1_795_012_200_000  # ms, a minute boundary (1_795_012_200 s = 29_916_870 minutes)


# ---------- synthetic book: mid 100, 20 levels of size 1 per side at x.x5 (no level on a band edge) ----------


def _levels(side: str, n: int = 20) -> list[tuple[float, float]]:
    return [(round(99.95 - 0.1 * i, 2), 1.0) if side == "bid" else (round(100.05 + 0.1 * i, 2), 1.0) for i in range(n)]


def _book(extra_bids=(), extra_asks=(), bounded=False) -> Book:
    b = Book(bounded=bounded)
    bids = dict(_levels("bid"))
    bids.update(dict(extra_bids))
    asks = dict(_levels("ask"))
    asks.update(dict(extra_asks))
    b.reset(bids.items(), asks.items())
    return b


def _tracker(**kw) -> WallTracker:
    return WallTracker("bybit", "BTCUSDT", 10.0, k=5.0, **kw)  # bar = max(5 x median 1.0, 10) = 10


def _run_out(t: WallTracker, book: Book, ts: int) -> list[dict]:
    """Book updates past the grace period, so every closed wall is decided."""
    ev, _ = t.on_book(ts + bw.GRACE_MS + 1, book)
    return ev


def test_book_reset_apply_best_and_view():
    b = _book()
    assert b.ready and b.best() == (99.95, 100.05) and b.view() == (98.05, 101.95)
    b.apply([(99.95, 0.0), (99.97, 2.0)], [(100.05, 3.0)])
    assert b.best() == (99.97, 100.05) and b.asks[100.05] == 3.0 and 99.95 not in b.bids
    b.apply([(100.10, 1.0)], [])  # crossed
    assert b.best() is None
    b.clear()
    assert not b.ready and b.view() is None


def test_bounded_book_drops_updates_outside_the_snapshot():
    b = _book(bounded=True)
    b.apply([(90.0, 5.0), (98.05, 0.0)], [(150.0, 5.0)])  # far diffs: their neighbours are unknown
    assert 90.0 not in b.bids and 150.0 not in b.asks and 98.05 not in b.bids
    assert b.view() == (98.05, 101.95)  # the snapshot's range, not the deepest level left


def test_diff_sequencer_follows_binance_rules():
    s = DiffSequencer()
    assert s.check(1, 2, None) == "unsynced"
    s.snapshot(100)
    assert s.check(90, 95, 89) == "skip"  # already in the snapshot
    assert s.check(96, 105, 95) == "apply"  # U <= 100 <= u
    assert s.check(106, 110, 105) == "apply"  # pu == previous u
    assert s.check(112, 115, 111) == "gap"
    s.snapshot(200)
    assert s.check(201, 210, 200) == "gap"  # first event after the snapshot starts past it: snapshot too old
    s.reset()
    assert not s.synced


def test_threshold_distance_classify_and_levels():
    assert bw.wall_threshold([1, 2, 3], 5.0, 4.0) == 10.0 and bw.wall_threshold([0.01] * 50, 50.0, 25.0) == 25.0
    assert bw.wall_threshold([], 5.0, 4.0) == 4.0
    assert bw.distance("bid", 99.0, 100.0) == pytest.approx(0.01) and bw.distance("ask", 101.0, 100.0) == pytest.approx(0.01)
    assert bw.classify(traded=6, size_max=10, dist_close=0.5, technical="gap") == "consumed"
    assert bw.classify(traded=4, size_max=10, dist_close=0.5, technical="gap") == "gap"
    assert bw.classify(traded=0, size_max=10, dist_close=0.0005, technical=None) == "pulled_near"
    assert bw.classify(traded=0, size_max=10, dist_close=0.002, technical=None) == "withdrawn"
    assert bw.levels_of([["1.5", "2"], [3, 0]]) == [(1.5, 2.0), (3.0, 0.0)]
    for bad in ([["nan", "1"]], [["1", "-1"]], [["1"]], [{"p": 1}], "x", [["1", "inf"]]):
        assert bw.levels_of(bad) is None
    assert bw.min_qty_for("BTCUSDT") == bw.MIN_QTY["BTC"] and bw.min_qty_for("SOLUSDT") is None


def test_withdrawn_wall_far_from_the_mid():
    t, book = _tracker(), _book(extra_bids=[(99.05, 20.0)])
    ev, row = t.on_book(T0, book)
    assert ev == [] and list(t.open) == [("bid", 99.05)] and row is not None and row["walls_bid"] == 1
    book.apply([(99.05, 0.0)], [])
    assert t.on_book(T0 + 6_000, book)[0] == []  # closed, waiting out the grace period
    (e,) = _run_out(t, book, T0 + 6_000)
    assert e["reason"] == "withdrawn" and e["open_ms"] == T0 and e["close_ms"] == T0 + 6_000
    assert (e["size_open"], e["size_max"], e["size_end"], e["threshold"], e["traded"], e["gaps"]) == (20.0, 20.0, 0.0, 10.0, 0.0, 0)
    assert e["dist_close"] == pytest.approx(0.0095) and e["mid_close"] == pytest.approx(100.0)
    assert set(e) == set(bw.EVENT_KEYS)


def test_pulled_near_when_the_mid_came_close():
    t, book = _tracker(), _book(extra_bids=[(99.55, 20.0)])
    t.on_book(T0, book)
    book.apply([(lv[0], 0.0) for lv in _levels("bid")[:4]], [(99.65, 1.0)] + [(p, 0.0) for p, _ in _levels("ask")[:3]])
    t.on_book(T0 + 3_000, book)  # mid now (99.55 + 99.65) / 2 = 99.60
    assert t.open[("bid", 99.55)].dist_min == pytest.approx(0.05 / 99.60)
    book.apply([(99.55, 0.0)], [])
    t.on_book(T0 + 6_000, book)
    (e,) = _run_out(t, book, T0 + 6_000)
    assert e["reason"] == "pulled_near" and e["dist_close"] <= bw.NEAR_DIST


def test_consumed_wall_counts_hits_on_its_side_and_late_trades():
    t, book = _tracker(), _book(extra_bids=[(99.55, 20.0)])
    t.on_book(T0, book)
    t.on_trade(T0 - 50, 99.55, 5.0, "sell")  # before the wall was seen: not its fill
    t.on_trade(T0 + 100, 99.55, 4.0, "buy")  # a taker buy cannot hit a bid
    t.on_trade(T0 + 200, 99.55, 6.0, "sell")
    t.on_trade(T0 + 300, 99.45, 6.0, "sell")  # another price
    book.apply([(99.55, 3.0)], [])  # 3 < 0.5 x 10: no longer a wall
    t.on_book(T0 + 1_000, book)
    t.on_trade(T0 + 1_500, 99.55, 5.0, "sell")  # the trade feed lags the depth feed: still counted
    (e,) = _run_out(t, book, T0 + 1_000)
    assert e["reason"] == "consumed" and e["traded"] == pytest.approx(11.0) and e["size_end"] == 3.0
    # Lived 1 s (< MIN_LIFE_MS) but was consumed: stored.


def test_a_trade_is_credited_once_when_a_wall_reopens_on_its_price():
    t, book = _tracker(), _book(extra_bids=[(99.55, 20.0)])
    t.on_book(T0, book)
    book.apply([(99.55, 2.0)], [])
    t.on_book(T0 + 6_000, book)  # closed (2 < 5), in its grace period
    book.apply([(99.55, 30.0)], [])
    t.on_book(T0 + 6_200, book)  # a new wall on the same price
    t.on_trade(T0 + 6_100, 99.55, 1.0, "sell")  # before the new one opened: the old one's
    t.on_trade(T0 + 6_300, 99.55, 4.0, "sell")  # the new one's only
    (old,) = _run_out(t, book, T0 + 6_000)
    assert old["traded"] == 1.0 and t.open[("bid", 99.55)].traded == 4.0


def test_hold_fraction_keeps_a_trimmed_wall_and_short_walls_are_only_counted():
    t, book = _tracker(), _book(extra_bids=[(99.05, 20.0)], extra_asks=[(101.05, 30.0)])
    t.on_book(T0, book)
    book.apply([(99.05, 6.0)], [(101.05, 0.0)])  # bid trimmed to 6 >= 5 stays; ask gone after 0.5 s
    t.on_book(T0 + 500, book)
    assert ("bid", 99.05) in t.open and ("ask", 101.05) not in t.open
    assert _run_out(t, book, T0 + 500) == []  # the short ask is not stored ...
    _, row = t.on_book(T0 + 60_000, book)
    assert row["short_walls"] == 1 and row["walls_bid"] == 1 and row["walls_ask"] == 0  # ... only counted
    assert t.on_book(T0 + 60_500, book)[1] is None  # one depth row per minute


def test_a_short_pulled_near_wall_is_stored():
    # The best bid is the wall (5 bp from the mid); pulled after 1 s, well under MIN_LIFE_MS: the spoof-like event of H1.
    t, book = _tracker(), _book(extra_bids=[(99.95, 20.0)])
    t.on_book(T0, book)
    book.apply([(99.95, 0.0)], [])
    t.on_book(T0 + 1_000, book)
    (e,) = _run_out(t, book, T0 + 1_000)
    assert e["reason"] == "pulled_near" and e["close_ms"] - e["open_ms"] == 1_000 < bw.MIN_LIFE_MS
    _, row = t.on_book(T0 + 60_000, book)
    assert row["short_walls"] == 0


def test_unbounded_book_trims_to_its_depth():
    b = Book(bounded=False, depth=3)
    b.reset([(99.0, 1.0), (98.0, 1.0), (97.0, 1.0)], [(101.0, 1.0), (102.0, 1.0)])
    b.apply([(99.5, 2.0), (96.0, 1.0)], [(100.5, 1.0), (103.0, 1.0)])
    assert sorted(b.bids) == [98.0, 99.0, 99.5] and sorted(b.asks) == [100.5, 101.0, 102.0] and b.view() == (98.0, 102.0)
    free = _book()
    free.apply([(90.0, 1.0)], [])
    assert 90.0 in free.bids  # no depth given: the venue's own deletes only


def test_walls_are_looked_for_inside_the_band_and_the_visible_book():
    # View reaches 1.95 % below the mid; walls only within 80 % of it (1.56 %): 98.55 yes, 98.25 no.
    t = _tracker()
    t.on_book(T0, _book(extra_bids=[(98.55, 50.0), (98.25, 50.0)]))
    assert list(t.open) == [("bid", 98.55)]
    wide = WallTracker("bybit", "BTCUSDT", 10.0, k=5.0, band=0.005)
    wide.on_book(T0, _book(extra_bids=[(98.55, 50.0), (99.65, 50.0)]))
    assert list(wide.open) == [("bid", 99.65)]


def test_out_of_view_when_the_level_leaves_the_venues_book():
    t, book = _tracker(), _book(extra_bids=[(98.55, 50.0)])
    t.on_book(T0, book)
    book.apply([(p, 0.0) for p, _ in _levels("bid") if p < 98.6] + [(98.55, 0.0)], [])  # venue trims its top-N
    t.on_book(T0 + 600, book)
    (e,) = _run_out(t, book, T0 + 600)
    assert e["reason"] == "out_of_view"  # kept even though it lived < MIN_LIFE_MS: not a cancel


def test_depth_gap_survivor_victim_and_long_gap():
    t, book = _tracker(), _book(extra_bids=[(99.05, 20.0), (99.25, 20.0)])
    t.on_book(T0, book)
    t.depth_gap()
    t.depth_gap()  # the _drop and the _open of one reconnect: one gap per wall
    survivor = _book(extra_bids=[(99.05, 20.0)])  # 99.25 vanished during the gap
    (e,), row = t.on_book(T0 + 60_000, survivor)  # closed at its last sighting, so already past the grace period
    assert e["reason"] == "gap" and e["price"] == 99.25 and e["close_ms"] == T0 and e["size_end"] is None and e["gaps"] == 1
    assert ("bid", 99.05) in t.open and t.open[("bid", 99.05)].gaps == 1 and not t.open[("bid", 99.05)].gap_pending
    assert row["gaps"] == 2  # break notices since the previous row
    t.depth_gap()
    (e2,) = _run_out(t, survivor, T0 + 60_000 + bw.MAX_GAP_MS)  # down longer than MAX_GAP_MS: closed though present
    assert e2["reason"] == "gap" and e2["price"] == 99.05 and e2["gaps"] == 2 and e2["close_ms"] == T0 + 60_000


def test_trade_feed_breaks_mark_traded_as_a_lower_bound():
    t, book = _tracker(), _book(extra_bids=[(99.05, 20.0)])
    t.trades_feed(True)  # first connect
    t.on_book(T0, book)
    assert t.open[("bid", 99.05)].gaps == 0
    t.trades_feed(False)
    t.trades_feed(True)  # one break: drop + reconnect
    assert t.open[("bid", 99.05)].gaps == 1
    t.trades_feed(False)
    book.apply([], [(101.05, 30.0)])
    t.on_book(T0 + 100, book)
    assert t.open[("ask", 101.05)].gaps == 1  # opened while trades were down
    t.trades_feed(True)
    t.trades_feed(True)  # a clean reconnect between runs is a break too
    assert t.open[("bid", 99.05)].gaps == 3


def test_depth_row_sums_the_visible_bands():
    t, book = _tracker(), _book(extra_bids=[(99.65, 20.0)])
    _, row = t.on_book(T0 + 12_345, book)
    assert row["ts"] == T0 // 1000 and row["mid"] == pytest.approx(100.0) and row["spread"] == pytest.approx(0.001)
    # 99.95..99.55 (99.65 holds 20) / 100.05..100.45; 99.95..99.05; the whole visible book
    assert row["bid_05"] == pytest.approx(24.0) and row["ask_05"] == pytest.approx(5.0)
    assert row["bid_1"] == pytest.approx(29.0) and row["bid_2"] == pytest.approx(39.0) and row["ask_2"] == pytest.approx(20.0)
    assert row["bid_view"] == pytest.approx(0.0195) and row["ask_view"] == pytest.approx(0.0195)


def test_tracker_ignores_a_book_that_is_not_ready():
    t = _tracker()
    assert t.on_book(T0, Book(bounded=True)) == ([], None)


# ---------- parsers on the live samples ----------


def test_parse_binance_depth_and_agg_trade_live():
    d = obs.parse_binance_depth(FX["binance_depth"][0])
    assert d["kind"] == "depth" and d["symbol"] == "ETHUSDT" and d["ts"] == 1795012197770
    assert (d["U"], d["u"], d["pu"]) == (12155391125705, 12155391131808, 12155391125622)
    assert d["bids"][1] == (1288.0, 179.204) and d["asks"][0] == (1793.01, 320.78)
    a, b = (obs.parse_binance_agg_trade(m) for m in FX["binance_agg_trade"])
    assert a == {"kind": "trade", "symbol": "ETHUSDT", "ts": 1795012205201, "price": 1792.41, "qty": 0.013, "side": "sell"}
    assert b["side"] == "buy"  # m = false: the buyer took liquidity
    assert obs.parse_binance_depth(FX["binance_agg_trade"][0]) is None and obs.parse_binance_agg_trade({"x": 1}) is None


def test_parse_bybit_live():
    snap, delta, ack, trade = FX["bybit"]
    (s,) = obs.parse_bybit(snap)
    assert s["kind"] == "snapshot" and s["symbol"] == "ETHUSDT" and s["u"] == 3053380 and s["asks"][0] == (1793.44, 39.06)
    (d,) = obs.parse_bybit(delta)
    assert d["kind"] == "delta" and d["u"] == 3053381 and d["bids"][0] == (1793.43, 0.0) and d["ts"] == 1795012211709
    assert obs.parse_bybit(ack) == []
    (t,) = obs.parse_bybit(trade)
    assert t == {"kind": "trade", "symbol": "ETHUSDT", "ts": 1795012213317, "price": 1793.92, "qty": 1.39, "side": "buy"}
    restart = json.loads(json.dumps(delta))
    restart["data"]["u"] = 1
    assert obs.parse_bybit(restart)[0]["kind"] == "snapshot"  # u = 1: the venue restarted, a full book


def test_parsers_drop_and_count_malformed_messages():
    bad_depth = json.loads(json.dumps(FX["binance_depth"][0]))
    bad_depth["data"]["b"] = [["x", "1"]]
    bad_trade = json.loads(json.dumps(FX["bybit"][3]))
    bad_trade["data"][0]["S"] = "Hold"
    bad_book = json.loads(json.dumps(FX["bybit"][0]))
    del bad_book["data"]["u"]
    with count_bad_rows() as n:
        assert obs.parse_binance_depth(bad_depth) is None
        assert obs.parse_bybit(bad_trade) == [] and obs.parse_bybit(bad_book) == []
        assert obs.parse_binance_agg_trade({"data": {"e": "aggTrade", "s": "X", "T": 1, "p": "1", "q": "1", "m": "yes"}}) is None
    assert n.n == 4


# ---------- stream plumbing (fake sockets, no network) ----------


class _FakeWS:
    def __init__(self, frames):
        self.frames, self.sent = list(frames), []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if not self.frames:
            await asyncio.Event().wait()
        f = self.frames.pop(0)
        if isinstance(f, BaseException):
            raise f
        return json.dumps(f)


class _FakeConnect:
    def __init__(self, script):
        self.script = list(script)

    def __call__(self, url, **kw):
        return self._cm(self.script.pop(0) if self.script else [])

    @contextlib.asynccontextmanager
    async def _cm(self, step):
        if isinstance(step, BaseException):
            raise step
        yield _FakeWS(step)


async def _drain(gen) -> list[dict]:
    return [row async for row in gen]


async def test_ws_consume_lifecycle_rows_mark_opens_and_drops():
    fake = _FakeConnect([[{"k": 1}, ConnectionResetError("reset")], [{"k": 2}]])
    st = StreamStats()
    rows = await _drain(
        liq._ws_consume("wss://x", None, lambda ev: ev, max_seconds=0.3, stats=st, connect=fake, backoff_base=0.01, lifecycle="book")
    )
    assert [r.get("kind", r.get("k")) for r in rows] == ["_open", 1, "_drop", "_open", 2]
    assert rows[2]["feed"] == "book" and "reset" in rows[2]["error"] and st.rows == 2  # markers are not rows
    plain = await _drain(liq._ws_consume("wss://x", None, lambda ev: ev, max_seconds=0.1, connect=_FakeConnect([[{"k": 3}]])))
    assert plain == [{"k": 3}]  # liquidations: unchanged


async def test_ws_consume_logs_failures_under_its_label(caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")

    def fake():
        return _FakeConnect([[{"k": 1}, ConnectionResetError("reset")], [{"k": 2}]])

    await _drain(liq._ws_consume("wss://x", None, lambda ev: ev, max_seconds=0.2, connect=fake(), backoff_base=0.01))
    assert "liquidation stream wss://x failed (1 in a row): ConnectionResetError: reset" in caplog.messages
    caplog.clear()
    await _drain(
        liq._ws_consume(
            "wss://x", None, lambda ev: ev, max_seconds=0.2, connect=fake(), backoff_base=0.01, lifecycle="book", label="book bybit:book"
        )
    )
    assert caplog.messages == ["book bybit:book stream wss://x failed (1 in a row): ConnectionResetError: reset"]
    assert "liquidation stream" not in caplog.text


async def test_merge_streams_aclose_does_not_hang_on_a_full_queue(monkeypatch):
    monkeypatch.setattr(obs, "QUEUE_MAX", 3)

    async def endless(tag):
        i = 0
        while True:
            i += 1
            yield (tag, i)

    merged = obs.merge_streams(endless("a"), endless("b"))
    assert (await anext(merged))[1] == 1
    await asyncio.sleep(0.05)  # the consumer stalls (a REST fetch, a slow write): both pumps block on the full queue
    await asyncio.wait_for(merged.aclose(), timeout=2)


async def test_secondary_feed_logs_its_error_and_ends_with_a_drop_row(caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")

    async def trades():
        yield {"kind": "_open", "feed": "trades"}
        raise KeyError("parser bug")

    rows = await _drain(obs.secondary_feed(trades(), "trades"))
    assert [r["kind"] for r in rows] == ["_open", "_drop"] and rows[1]["feed"] == "trades" and "KeyError" in rows[1]["error"]
    assert "book stream trades ended early" in caplog.text

    async def depth():
        yield {"kind": "_open", "feed": "depth"}

    async def broken_trades():
        raise ValueError("bad aggTrade")
        yield  # pragma: no cover

    got = await _drain(obs.merge_streams(depth(), obs.secondary_feed(broken_trades(), "trades")))
    assert sorted(r["feed"] for r in got) == ["depth", "trades"]  # the run goes on, the trades break is visible


async def test_merge_streams_raises_only_the_primary_error():
    async def gen(rows, exc=None):
        for r in rows:
            await asyncio.sleep(0)
            yield r
        if exc is not None:
            raise exc

    got = await _drain(obs.merge_streams(gen([1, 2]), gen([3], OSError("trades down"))))
    assert sorted(got) == [1, 2, 3]
    with pytest.raises(TimeoutError):
        await _drain(obs.merge_streams(gen([1], TimeoutError("depth down")), gen([2, 3])))


async def test_book_streams_subscribe_and_name_their_feeds(monkeypatch):
    seen = []

    async def fake(url, subscribe, parser, **kw):
        seen.append((url, subscribe, parser, kw))
        yield {"kind": "_open", "feed": kw["lifecycle"]}

    monkeypatch.setattr(liq, "_ws_consume", fake)
    g = obs.StreamGroup()
    rows = await _drain(obs.binance_book_stream(["BTCUSDT", "ETHUSDT"], max_seconds=1, stats=g))
    assert sorted(r["feed"] for r in rows) == ["depth", "trades"] and set(g.feeds) == {"depth", "trades"}
    urls = {kw["lifecycle"]: url for url, _, _, kw in seen}
    assert urls["depth"] == "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@500ms/ethusdt@depth@500ms"
    assert urls["trades"] == "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/ethusdt@aggTrade"
    assert sorted(kw["label"] for *_, kw in seen) == ["book binance:depth", "book binance:trades"]
    seen.clear()
    await _drain(obs.bybit_book_stream(["BTCUSDT"], max_seconds=1, stats=g))
    (url, sub, parser, kw) = seen[0]
    assert url == liq.BYBIT_WS_URL and sub["args"] == ["orderbook.1000.BTCUSDT", "publicTrade.BTCUSDT"]
    assert kw["label"] == "book bybit:book"  # not the liquidation log line (scripts/liquidation_gaps_vs_log.py)
    assert parser is obs.parse_bybit and kw["error_of"] is liq._bybit_error and json.loads(kw["heartbeat"]) == {"op": "ping"}
    assert set(g.as_dict()) == {"depth", "trades", "book"} and "clock" not in g.as_dict()["book"]


async def test_binance_depth_snapshot_endpoint_on_the_live_sample():
    from tradedesk.core.cache import TTLCache  # noqa: PLC0415
    from tradedesk.core.http import Http  # noqa: PLC0415
    from tradedesk.providers.binance_futures import BinanceFutures  # noqa: PLC0415

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json=FX["binance_rest_depth"])

    bf = BinanceFutures(Http("binance_futures", "https://fapi.binance.com", cache=TTLCache(), transport=httpx.MockTransport(handler)))
    f = await bf.depth("ETHUSDT")
    assert f.data["lastUpdateId"] == 12155391035403 and seen["url"].endswith("/fapi/v1/depth?symbol=ETHUSDT&limit=1000")

    def broken(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": -1121, "msg": "Invalid symbol."})

    bad = BinanceFutures(Http("binance_futures", "https://fapi.binance.com", cache=TTLCache(), transport=httpx.MockTransport(broken)))
    with pytest.raises(ProviderError):
        await bad.depth("NOPE")


# ---------- store ----------


async def _store(tmp_path) -> Store:
    s = Store(tmp_path / "walls.sqlite")
    await s.init()
    return s


def _event(**kw) -> dict:
    base = dict.fromkeys(bw.EVENT_KEYS, 1.0) | {"venue": "bybit", "symbol": "BTCUSDT", "side": "bid", "reason": "withdrawn"}
    return base | {"open_ms": T0, "close_ms": T0 + 6000, "gaps": 0, "size_end": None} | kw


async def test_store_writes_and_reads_walls_depth_and_stream_hours(tmp_path):
    s = await _store(tmp_path)
    try:
        assert await s.insert_book_wall_events([_event(), _event(venue="binanceusdm", close_ms=T0 + 9000)]) == 2
        assert await s.insert_book_wall_events([]) == 0
        got = await s.book_wall_events(venue="bybit")
        assert len(got) == 1 and got[0]["size_end"] is None and got[0]["reason"] == "withdrawn"
        assert [e["venue"] for e in await s.book_wall_events(since_ms=T0 + 7000)] == ["binanceusdm"]
        row = dict.fromkeys(Store.BOOK_DEPTH_COLUMNS, 0.0) | {"venue": "bybit", "symbol": "BTCUSDT", "ts": T0 // 1000, "mid": 100.0}
        await s.insert_book_depth([row])
        await s.insert_book_depth([row | {"mid": 101.0}])  # the same minute again: replaced
        (d,) = await s.book_depth_rows()
        assert d["mid"] == 101.0
        h = {T0 // 1000: {"connected_s": 10.0, "frames": 3, "rows": 2, "failures": 1, "reconnects": 0}}
        assert await s.add_book_stream_hours("bybit:book", h) == 1
        await s.add_book_stream_hours("bybit:book", h)
        cur = await s.conn.execute("SELECT connected_s, frames, failures FROM book_stream_hour WHERE stream = 'bybit:book'")
        assert tuple(await cur.fetchone()) == (20.0, 6, 2)  # summed
    finally:
        await s.aclose()


async def test_migration_41_adds_the_tables_to_a_v40_store(tmp_path):
    path = tmp_path / "v40.sqlite"
    con = sqlite3.connect(path)
    con.execute("PRAGMA synchronous=OFF")
    for v in sorted(MIGRATIONS):
        if v <= 40:
            for stmt in MIGRATIONS[v]:
                con.execute(stmt)
    con.execute("PRAGMA user_version = 40")
    con.commit()
    con.close()
    s = Store(path)
    await s.init()
    try:
        assert await s.version() == SCHEMA_VERSION >= 41
        assert await s.insert_book_wall_events([_event()]) == 1 and len(await s.book_wall_events()) == 1
    finally:
        await s.aclose()


# ---------- collector job (fake streams) ----------


async def _rt(state, tmp_path, **kw) -> Runtime:
    rt = Runtime(
        dataclasses.replace(Settings(), db_path=str(tmp_path / "bw.sqlite"), http_retries=0, cache_scale=0.0, **kw),
        transports=make_transports(state),
        exchange_factory=FakeEx,
    )
    rt.now = lambda: datetime.fromtimestamp(T0 / 1000, UTC)
    await rt.store.init()
    return rt


def _wire(levels):
    return [(float(p), float(q)) for p, q in levels]


# 63000 .. 62710: view 0.47 % below the mid; median size 0.1, so the bar is the 25 BTC minimum
BTC_BIDS = [(round(63_000 - 10 * i, 1), 0.1) for i in range(30)]
BTC_ASKS = [(round(63_010 + 10 * i, 1), 0.1) for i in range(30)]


def _fake_stream(monkeypatch, name: str, rows: list[dict]):
    async def fake(symbols, *, max_seconds, stats):
        for r in rows:
            yield r

    monkeypatch.setattr(obs, name, fake)


async def test_bybit_job_stores_a_wall_and_depth_rows(state, tmp_path, monkeypatch):
    wall = (62_850.0, 40.0)  # 40 BTC >= 25: a wall, 0.25 % below the mid
    rows = [
        {"kind": "_open", "feed": "book"},
        {"kind": "snapshot", "symbol": "BTCUSDT", "ts": T0, "u": 10, "bids": [*BTC_BIDS, wall], "asks": BTC_ASKS},
        {"kind": "trade", "symbol": "BTCUSDT", "ts": T0 + 100, "price": 62_850.0, "qty": 2.0, "side": "sell"},
        {"kind": "delta", "symbol": "BTCUSDT", "ts": T0 + 7_000, "u": 11, "bids": [(62_850.0, 0.0)], "asks": []},
        {"kind": "delta", "symbol": "BTCUSDT", "ts": T0 + 61_000, "u": 12, "bids": [], "asks": [(63_010.0, 2.0)]},
        {"kind": "trade", "symbol": "SOLUSDT", "ts": T0, "price": 1.0, "qty": 1.0, "side": "buy"},  # not tracked
    ]
    _fake_stream(monkeypatch, "bybit_book_stream", rows)
    rt = await _rt(state, tmp_path)
    job = BookWallsJob("bybit", ["BTCUSDT", "ETHUSDT", "DOGEUSDT"])
    assert job.symbols == ["BTCUSDT", "ETHUSDT"]  # no coin minimum for DOGE
    try:
        assert await job(rt) == 3  # 1 wall + 2 minute rows
        (e,) = await rt.store.book_wall_events()
        assert (e["venue"], e["side"], e["price"], e["reason"], e["traded"], e["gaps"]) == ("bybit", "bid", 62_850.0, "withdrawn", 2.0, 0)
        assert e["close_ms"] - e["open_ms"] == 7_000 and e["threshold"] == 25.0
        depth = await rt.store.book_depth_rows()
        assert [d["ts"] for d in depth] == [T0 // 1000, T0 // 1000 + 60] and depth[0]["walls_bid"] == 1
        assert depth[0]["gaps"] == 1  # the connect itself: the book before it is unknown
    finally:
        await rt.aclose()


async def test_bybit_job_ends_the_run_on_an_update_id_gap(state, tmp_path, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")
    rows = [
        {"kind": "_open", "feed": "book"},
        {"kind": "snapshot", "symbol": "BTCUSDT", "ts": T0, "u": 10, "bids": BTC_BIDS, "asks": BTC_ASKS},
        {"kind": "delta", "symbol": "BTCUSDT", "ts": T0 + 200, "u": 12, "bids": [], "asks": []},
        {"kind": "delta", "symbol": "BTCUSDT", "ts": T0 + 400, "u": 13, "bids": [], "asks": []},
    ]
    consumed = []

    async def fake(symbols, *, max_seconds, stats):
        for r in rows:
            consumed.append(r)
            yield r

    monkeypatch.setattr(obs, "bybit_book_stream", fake)
    rt = await _rt(state, tmp_path)
    job = BookWallsJob("bybit", ["BTCUSDT"])
    try:
        assert await job(rt) == 1  # the first minute row only
        assert len(consumed) == 3 and job.resyncs == 1 and not job.books["BTCUSDT"].ready
        assert "book resync (bybit update id 10 -> 12)" in caplog.text
    finally:
        await rt.aclose()


async def test_binance_job_seeds_from_rest_and_survives_a_failed_snapshot(state, tmp_path, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")
    snap = {
        "lastUpdateId": 100,
        "bids": [[str(p), str(q)] for p, q in [*BTC_BIDS, (62_950.0, 40.0)]],
        "asks": [[str(p), str(q)] for p, q in BTC_ASKS],
    }
    calls = []

    async def depth(symbol, limit=1000):
        calls.append(symbol)
        if len(calls) == 1:
            raise ProviderError("binance_futures", "network", "timed out")
        return Fetched(snap, Evidence(provider="binance_futures", tool="fapi/v1/depth"))

    def diff(ts, first, last, prev, bids=()):
        return {"kind": "depth", "symbol": "BTCUSDT", "ts": ts, "U": first, "u": last, "pu": prev, "bids": list(bids), "asks": []}

    rows = [
        {"kind": "_open", "feed": "depth"},
        {"kind": "_open", "feed": "trades"},
        diff(T0, 90, 95, 89),  # snapshot fetch fails: retried 5 s later
        diff(T0 + 1_000, 96, 99, 95),  # too early to retry: nothing applied
        diff(T0 + 5_000, 99, 101, 99),  # seeded (L = 100): U <= 100 <= u -> applied
        {"kind": "trade", "symbol": "BTCUSDT", "ts": T0 + 5_100, "price": 62_950.0, "qty": 30.0, "side": "sell"},
        diff(T0 + 6_000, 102, 103, 101, bids=[(62_950.0, 0.0)]),
        diff(T0 + 8_000, 105, 106, 104),  # pu != 103: resync
        diff(T0 + 9_000, 107, 108, 106),  # 4 s after the last fetch: no new one yet
        diff(T0 + 10_000, 109, 110, 108),  # re-seeded (L = 100 again): first event U > L -> gap again
        {"kind": "trade", "symbol": "BTCUSDT", "ts": T0 + 10_500, "price": 1.0, "qty": 1.0, "side": "buy"},  # grace over
    ]
    _fake_stream(monkeypatch, "binance_book_stream", rows)
    rt = await _rt(state, tmp_path)
    monkeypatch.setattr(rt.binance_futures, "depth", depth)
    job = BookWallsJob("binance", ["BTCUSDT"])
    try:
        await job(rt)
        assert calls == ["BTCUSDT"] * 3 and "depth snapshot failed" in caplog.text and job.resyncs == 2
        (e,) = await rt.store.book_wall_events()
        assert e["venue"] == "binanceusdm" and e["reason"] == "consumed" and e["traded"] == 30.0
        assert "pu=104" in caplog.text
    finally:
        await rt.aclose()


def _diff(ts, first, last, prev, bids=()):
    return {"kind": "depth", "symbol": "BTCUSDT", "ts": ts, "U": first, "u": last, "pu": prev, "bids": list(bids), "asks": []}


def _snap(last_id, extra_bids=()):
    return {
        "lastUpdateId": last_id,
        "bids": [[str(p), str(q)] for p, q in [*BTC_BIDS, *extra_bids]],
        "asks": [[str(p), str(q)] for p, q in BTC_ASKS],
    }


async def test_binance_snapshots_are_spaced_after_a_sequence_break(state, tmp_path, monkeypatch):
    # The REST snapshot is always older than the stream (L < U of every diff): each one ends in a gap. One fetch per
    # SNAPSHOT_RETRY_MS per symbol, not one per 500 ms diff.
    calls = []

    async def depth(symbol, limit=1000):
        calls.append(symbol)
        return Fetched(_snap(50), Evidence(provider="binance_futures", tool="fapi/v1/depth"))

    rows = [{"kind": "_open", "feed": "depth"}] + [_diff(T0 + 500 * i, 1000 + 10 * i, 1009 + 10 * i, 999 + 10 * i) for i in range(41)]
    _fake_stream(monkeypatch, "binance_book_stream", rows)
    rt = await _rt(state, tmp_path)
    monkeypatch.setattr(rt.binance_futures, "depth", depth)
    job = BookWallsJob("binance", ["BTCUSDT"])
    try:
        await job(rt)
        assert len(calls) == 5 and job.resyncs == 5  # at T0, +5 s, +10 s, +15 s, +20 s over 20 s of diffs
    finally:
        await rt.aclose()


async def test_binance_reseed_older_than_the_book_is_discarded(state, tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="tradedesk.collector")
    wall = (62_950.0, 40.0)
    snaps = [_snap(100, [wall]), _snap(150, [wall]), _snap(210, [wall])]
    calls = []

    async def depth(symbol, limit=1000):
        calls.append(symbol)
        return Fetched(snaps[len(calls) - 1], Evidence(provider="binance_futures", tool="fapi/v1/depth"))

    rows = [
        {"kind": "_open", "feed": "depth"},
        _diff(T0, 99, 101, 98),  # seeded (L = 100), applied
        _diff(T0 + 30_000, 102, 200, 101),
        _diff(T0 + 60_000, 201, 210, 200),  # re-seed due: L = 150 < 200 -> discarded, the diff goes on the old book
        _diff(T0 + 120_000, 211, 220, 210),  # re-seed due: L = 210 = the book's last update -> levels refreshed, in sequence
        _diff(T0 + 121_000, 221, 230, 220),
    ]
    _fake_stream(monkeypatch, "binance_book_stream", rows)
    rt = await _rt(state, tmp_path)
    monkeypatch.setattr(rt.binance_futures, "depth", depth)
    job = BookWallsJob("binance", ["BTCUSDT"])
    try:
        await job(rt)
        assert len(calls) == 3 and job.resyncs == 0 and job.stale_snapshots == 1 and job.seq["BTCUSDT"].last == 230
        w = job.trackers["BTCUSDT"].open[("bid", 62_950.0)]
        assert w.open_ms == T0 and w.gaps == 0 and job.books["BTCUSDT"].ready  # the open wall was not marked as a gap
        assert "snapshot 150 older than the book (200)" in caplog.text and await rt.store.book_wall_events() == []
    finally:
        await rt.aclose()


async def test_job_journals_stream_hours_and_keeps_them_when_the_write_fails(state, tmp_path, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="tradedesk.collector")
    _fake_stream(monkeypatch, "bybit_book_stream", [])
    rt = await _rt(state, tmp_path)
    job = BookWallsJob("bybit", ["BTCUSDT"], clock=lambda: 3600.0 * 500_000)
    hour = 3600 * 500_000
    try:
        job.stats.feeds["book"].count("failures", hour + 5)
        real = rt.store.add_book_stream_hours

        async def broken(*_a, **_kw):
            raise sqlite3.OperationalError("database is locked")

        rt.store.add_book_stream_hours = broken
        assert await job(rt) == 0
        assert "book stream journal bybit:book not written" in caplog.text and job.stats.feeds["book"].hours[hour]["failures"] == 1
        rt.store.add_book_stream_hours = real
        await job(rt)
        cur = await rt.store.conn.execute("SELECT stream, hour_ts, failures FROM book_stream_hour")
        assert [tuple(r) for r in await cur.fetchall()] == [("bybit:book", hour, 1)]
    finally:
        await rt.aclose()


async def test_job_run_fails_when_the_book_stream_is_down(state, tmp_path, monkeypatch):
    async def down(symbols, *, max_seconds, stats):
        raise TimeoutError("timed out during opening handshake")
        yield  # pragma: no cover

    monkeypatch.setattr(obs, "binance_book_stream", down)
    rt = await _rt(state, tmp_path)
    try:
        with pytest.raises(TimeoutError):
            await BookWallsJob("binance", ["ETHUSDT"])(rt)
    finally:
        await rt.aclose()


def test_job_is_off_by_default_and_switches_are_allow_listed(monkeypatch):
    s = Settings()
    assert s.collect_book_walls_enabled is False and s.collect_book_walls_venues == "binance,bybit"
    for name in ("ENABLED", "VENUES", "SYMBOLS", "WINDOW_SECONDS", "RESTART_SECONDS"):
        assert allowed(f"TRADEDESK_COLLECT_BOOK_WALLS_{name}")
    monkeypatch.setenv("TRADEDESK_COLLECT_BOOK_WALLS_ENABLED", "1")
    monkeypatch.setenv("TRADEDESK_COLLECT_BOOK_WALLS_VENUES", "bybit")
    env = Settings.from_env()
    assert env.collect_book_walls_enabled and env.collect_book_walls_venues == "bybit"
    base = dataclasses.replace(Settings(), collector_enabled=True)
    off = build_default_scheduler(SimpleNamespace(settings=base, store=object()))
    assert not any(j.name.startswith("book_walls") for j in off.jobs)
    rt = SimpleNamespace(settings=dataclasses.replace(base, collect_book_walls_enabled=True), store=object())
    jobs = [j for j in build_default_scheduler(rt).jobs if j.name.startswith("book_walls")]
    assert [j.name for j in jobs] == ["book_walls:binance", "book_walls:bybit"]
    assert all(isinstance(j.metrics.stream, obs.StreamGroup) and j.metrics.stream is j.fn.stats for j in jobs)
    assert all(j.period_seconds == 2 for j in jobs)
    assert set(jobs[0].metrics.stream.as_dict()) == {"depth", "trades"} and set(jobs[1].metrics.stream.as_dict()) == {"book"}
