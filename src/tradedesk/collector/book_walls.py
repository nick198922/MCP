"""Order-book walls collector (IDEA-20261114-book-walls item 1): one job per venue holds the book and trade streams of
BTC / ETH perps for a run window, keeps a local L2 book per symbol and stores closed walls (``book_wall_event``) and one
depth row per symbol and minute (``book_depth_minute``); the analysis is pure (analytics/book_walls.py).

* ``book_walls:binance``: USD-M depth diffs + aggTrades (two connections, providers/orderbook_stream.py). The book is
  seeded with a REST snapshot of 1000 levels (``BinanceFutures.depth``) and re-seeded every ``RESNAPSHOT_MS``, because
  levels outside the snapshot stay unknown and the visible range (+-0.2 % for BTC on 2026-11-18) drifts off with the
  price. A sequence gap (``pu`` != previous ``u``) clears the book until the next snapshot; snapshots are fetched at
  most once per ``SNAPSHOT_RETRY_MS`` per symbol, and a re-seed older than the book is discarded.
* ``book_walls:bybit``: linear ``orderbook.1000`` + ``publicTrade`` on one connection; the venue sends the snapshot. A
  break in the delta ``u`` sequence ends the run early, so the next run reconnects and gets a new snapshot.

The job object (books, open walls, connection counters) lives as long as the process: a run ends every
``collect_book_walls_window_seconds`` and the next one reconnects, which the walls see as a feed break (``gaps``).
Breaks are logged (``tradedesk.collector``), counted per connection in ``system_health`` (``collector[].stream``),
per hour in ``book_stream_hour`` and per minute in ``book_depth_minute.gaps``. Walls still open when the process stops
are lost. No key, read-only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..analytics.book_walls import Book, DiffSequencer, WallTracker, levels_of, min_qty_for, walls_summary
from ..core.errors import ProviderError
from .bad_rows import note_bad_rows

if TYPE_CHECKING:  # avoid import cycle
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

VENUE_ID = {"binance": "binanceusdm", "bybit": "bybit"}  # ccxt ids, as in the liquidations table
VENUES = tuple(VENUE_ID)
SNAPSHOT_LIMIT = 1000  # Binance REST depth levels per side (weight 20)
RESNAPSHOT_MS = 60_000  # re-seed the Binance book every minute: 2 symbols x weight 20 = 40 of the 2400 a minute
# At most one snapshot fetch per symbol this often, whatever the outcome: after a sequence break a snapshot that is
# always older than the stream would otherwise be fetched on every 500 ms diff (weight 20 each, 2400 a minute per IP,
# shared with every Binance collector; 2026-11-18 review).
SNAPSHOT_RETRY_MS = 5_000
FLUSH_EVENTS = 100


class BookWallsJob:
    """Callable collector job for one venue (``await job(rt)`` = one run window)."""

    def __init__(self, venue: str, symbols: list[str], *, clock: Callable[[], float] | None = None) -> None:
        from ..providers.liquidations import StreamStats  # noqa: PLC0415
        from ..providers.orderbook_stream import StreamGroup  # noqa: PLC0415

        if venue not in VENUE_ID:
            raise ValueError(f"book walls: unknown venue {venue!r}")
        self.venue = venue
        self.symbols = [s for s in symbols if min_qty_for(s) is not None]
        skipped = [s for s in symbols if s not in self.symbols]
        if skipped:
            log.warning("book walls %s: no coin minimum for %s (analytics/book_walls.MIN_QTY), skipped", venue, ",".join(skipped))
        self.stats = StreamGroup()
        for feed in ("depth", "trades") if venue == "binance" else ("book",):
            self.stats.feeds[feed] = StreamStats(clock=clock)
        from ..providers.orderbook_stream import BYBIT_BOOK_DEPTH  # noqa: PLC0415

        bounded = venue == "binance"
        self.books = {s: Book(bounded=bounded, depth=None if bounded else BYBIT_BOOK_DEPTH) for s in self.symbols}
        self.seq = {s: DiffSequencer() for s in self.symbols}
        self.trackers = {s: WallTracker(VENUE_ID[venue], s, min_qty_for(s) or 0.0) for s in self.symbols}
        self.snapshot_ms: dict[str, int | None] = dict.fromkeys(self.symbols)
        self.retry_ms: dict[str, int] = dict.fromkeys(self.symbols, 0)
        self.last_u: dict[str, int | None] = dict.fromkeys(self.symbols)
        self.resyncs = 0
        self.stale_snapshots = 0  # Binance re-seeds discarded as older than the book
        self.restart = False

    # ---- one run ----

    def _stream(self, window: float):
        from ..providers import orderbook_stream as obs  # noqa: PLC0415

        if self.venue == "binance":
            return obs.binance_book_stream(self.symbols, max_seconds=window, stats=self.stats)
        return obs.bybit_book_stream(self.symbols, max_seconds=window, stats=self.stats)

    async def __call__(self, rt: "Runtime") -> int:
        if not self.symbols:
            return 0
        assert rt.store is not None
        events: list[dict[str, Any]] = []
        depth: list[dict[str, Any]] = []
        written = 0
        stored: list[dict[str, Any]] = []
        self.restart = False
        stream = self._stream(float(rt.settings.collect_book_walls_window_seconds))
        try:
            async for row in stream:
                ev, dr = await self._handle(rt, row)
                events += ev
                if dr is not None:
                    depth.append(dr)
                if depth or len(events) >= FLUSH_EVENTS:  # a minute row is due: write what we have
                    stored += events
                    written += await self._flush(rt, events, depth)
                    events, depth = [], []
                if self.restart:
                    break
        finally:
            await stream.aclose()
            stored += events
            written += await self._flush(rt, events, depth)
            await self._journal(rt)
        if stored:
            log.info("book walls %s: %d walls stored %s", self.venue, len(stored), walls_summary(stored))
        return written

    async def _flush(self, rt: "Runtime", events: list[dict[str, Any]], depth: list[dict[str, Any]]) -> int:
        assert rt.store is not None
        return await rt.store.insert_book_wall_events(events) + await rt.store.insert_book_depth(depth)

    async def _journal(self, rt: "Runtime") -> None:
        """Hourly connection tallies of each feed into ``book_stream_hour``; kept for the next run if the write fails."""
        for feed, st in self.stats.feeds.items():
            hours = st.take_hours()
            if not hours or rt.store is None:
                continue
            try:
                await rt.store.add_book_stream_hours(f"{self.venue}:{feed}", hours)
            except Exception as e:  # noqa: BLE001
                st.put_back_hours(hours)
                log.warning("book stream journal %s:%s not written: %s", self.venue, feed, e)

    # ---- rows ----

    async def _handle(self, rt: "Runtime", row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        kind = row.get("kind")
        if kind in ("_open", "_drop"):
            self._feed_event(str(row.get("feed")), up=kind == "_open")
            return [], None
        sym = row.get("symbol")
        tracker = self.trackers.get(sym)  # type: ignore[arg-type]
        if tracker is None:
            return [], None
        if kind == "trade":
            return tracker.on_trade(row["ts"], row["price"], row["qty"], row["side"]), None
        book = self.books[sym]
        if kind == "depth":
            return await self._binance_depth(rt, sym, row, book, tracker)
        if kind == "snapshot":
            book.reset(row["bids"], row["asks"])
            self.last_u[sym] = row["u"]
            return tracker.on_book(row["ts"], book)
        if kind == "delta":
            if not book.ready:
                return [], None
            prev = self.last_u[sym]
            self.last_u[sym] = row["u"]
            if prev is not None and row["u"] != prev + 1:
                self._resync(sym, f"bybit update id {prev} -> {row['u']}")
                self.restart = True  # Bybit sends a snapshot only on (re)subscribe: end the run, the next one reconnects
                return [], None
            book.apply(row["bids"], row["asks"])
            return tracker.on_book(row["ts"], book)
        return [], None

    def _feed_event(self, feed: str, *, up: bool) -> None:
        """A connection opened or dropped: the book (depth feeds) is unknown until the next snapshot; trades may be missed."""
        for sym in self.symbols:
            if feed in ("depth", "book"):
                self.books[sym].clear()
                self.seq[sym].reset()
                self.last_u[sym] = None
                self.snapshot_ms[sym] = None
                self.trackers[sym].depth_gap()
            if feed in ("trades", "book"):
                self.trackers[sym].trades_feed(up)

    def _resync(self, sym: str, why: str) -> None:
        self.resyncs += 1
        self.books[sym].clear()
        self.seq[sym].reset()
        self.snapshot_ms[sym] = None
        self.trackers[sym].depth_gap()
        log.warning("book walls %s %s: book resync (%s), %d so far", self.venue, sym, why, self.resyncs)

    async def _binance_depth(
        self, rt: "Runtime", sym: str, row: dict[str, Any], book: Book, tracker: WallTracker
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        ts, seq = int(row["ts"]), self.seq[sym]
        last = self.snapshot_ms[sym]
        due = not seq.synced or (last is not None and ts - last >= RESNAPSHOT_MS)
        if due and ts >= self.retry_ms[sym]:
            await self._snapshot(rt, sym, ts)
        verdict = seq.check(row["U"], row["u"], row["pu"])
        if verdict == "apply":
            book.apply(row["bids"], row["asks"])
            return tracker.on_book(ts, book)
        if verdict == "gap":
            self._resync(sym, f"diff U={row['U']} pu={row['pu']} after {seq.last}")
        return [], None

    async def _snapshot(self, rt: "Runtime", sym: str, ts: int) -> None:
        """Seed (or re-seed) the book from REST, at most once per SNAPSHOT_RETRY_MS per symbol. A failure keeps the old
        book; so does a re-seed older than the update the book already holds (it would read as a sequence gap)."""
        self.retry_ms[sym] = ts + SNAPSHOT_RETRY_MS
        try:
            d = (await rt.binance_futures.depth(sym, SNAPSHOT_LIMIT)).data
            bids, asks, last_id = levels_of(d.get("bids")), levels_of(d.get("asks")), int(d["lastUpdateId"])
        except (ProviderError, KeyError, TypeError, ValueError) as e:
            log.warning("book walls binance %s: depth snapshot failed: %s", sym, e)
            return
        if bids is None or asks is None:
            note_bad_rows(1)
            log.warning("book walls binance %s: malformed depth snapshot dropped", sym)
            return
        seq = self.seq[sym]
        if seq.last is not None and last_id <= seq.last:
            # A re-seed not newer than the book: an older one is discarded (the next re-seed is due in RESNAPSHOT_MS),
            # one at exactly the last applied update replaces the levels and keeps the sequence going.
            if last_id < seq.last:
                self.snapshot_ms[sym] = ts
                self.stale_snapshots += 1
                log.info("book walls binance %s: snapshot %d older than the book (%d), kept the book", sym, last_id, seq.last)
                return
            self.books[sym].reset(bids, asks)
            self.snapshot_ms[sym] = ts
            return
        self.books[sym].reset(bids, asks)
        seq.snapshot(last_id)
        self.snapshot_ms[sym] = ts
