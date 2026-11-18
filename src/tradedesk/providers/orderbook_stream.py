"""Public order-book and trade streams for the book-walls collector (collector/book_walls.py): Binance USD-M and Bybit
linear perps. No key; read-only.

Endpoints (checked live 2026-11-18 from the server, samples in tests/fixtures/book_walls_live_2026-11-18.json):

* Binance USD-M diff depth ``<symbol>@depth@<speed>`` is served on the ``/public/`` path only; ``aggTrade`` on
  ``/market/`` only (the same combined-stream URL on the other path connects and stays silent for that stream). So
  Binance takes two connections, merged here into one row stream. The diffs must be sequenced against a REST snapshot
  (``GET /fapi/v1/depth``, ``providers.binance_futures.BinanceFutures.depth``; analytics.book_walls.DiffSequencer).
* Bybit v5 linear ``orderbook.<depth>.<symbol>`` (a ``snapshot`` then ``delta`` messages; ``u`` = 1 or a new snapshot
  resets the book) and ``publicTrade.<symbol>`` on one connection. Depth 1000 (pushed every 200 ms), not 200: on
  2026-11-18 the 200-level book reached only +-0.045 % (BTC) / +-0.12 % (ETH) from the mid, the 1000-level book
  +-0.3-0.4 % / +-0.9-1.0 %.

Rows (all prices / sizes floats, sizes in coins, ``ts`` = venue time in ms):

* ``{"kind": "depth", "symbol", "ts", "U", "u", "pu", "bids", "asks"}`` (Binance diff);
* ``{"kind": "snapshot" | "delta", "symbol", "ts", "u", "bids", "asks"}`` (Bybit);
* ``{"kind": "trade", "symbol", "ts", "price", "qty", "side"}``, ``side`` = taker side 'buy' | 'sell' (Binance
  ``m`` = buyer is the maker -> a taker sell; Bybit ``S`` is the taker side);
* ``{"kind": "_open" | "_drop", "feed"}`` connection life cycle (providers.liquidations._ws_consume ``lifecycle``).

Malformed messages are dropped and counted (collector.bad_rows).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..analytics.book_walls import levels_of
from ..collector.bad_rows import note_bad_rows
from . import liquidations as ws_mod
from .liquidations import StreamStats

log = logging.getLogger("tradedesk.collector")

BINANCE_DEPTH_WS = "wss://fstream.binance.com/public/stream?streams="
BINANCE_TRADE_WS = "wss://fstream.binance.com/market/stream?streams="
BYBIT_WS_URL = ws_mod.BYBIT_WS_URL
# 500 ms diffs, not 100 ms: the 100 ms feed carried ~13k levels a second for BTC + ETH (live trial 2026-11-18, most of
# them far outside the book we keep) and parsing them was the collector's largest CPU cost; a wall that matters to us
# lives for seconds.
BINANCE_DEPTH_SPEED = "500ms"
BYBIT_BOOK_DEPTH = 1000
QUEUE_MAX = 10_000  # rows waiting for the consumer (a REST snapshot fetch pauses it); then the feeds wait too


@dataclass
class StreamGroup:
    """The connections of one collector job, by feed name; ``system_health`` shows ``as_dict()`` in
    ``collector[].stream``."""

    feeds: dict[str, StreamStats] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {name: st.as_dict() for name, st in self.feeds.items()}


def binance_depth_url(symbols: list[str]) -> str:
    return BINANCE_DEPTH_WS + "/".join(f"{s.lower()}@depth@{BINANCE_DEPTH_SPEED}" for s in symbols)


def binance_trade_url(symbols: list[str]) -> str:
    return BINANCE_TRADE_WS + "/".join(f"{s.lower()}@aggTrade" for s in symbols)


def _bad() -> None:
    note_bad_rows(1)


def parse_binance_depth(msg: Any) -> dict[str, Any] | None:
    """A combined-stream ``depthUpdate`` -> depth row; None for anything else."""
    d = msg.get("data") if isinstance(msg, dict) else None
    if not isinstance(d, dict) or d.get("e") != "depthUpdate":
        return None
    bids, asks = levels_of(d.get("b")), levels_of(d.get("a"))
    try:
        row = {
            "kind": "depth",
            "symbol": str(d["s"]),
            "ts": int(d.get("T") or d["E"]),
            "U": int(d["U"]),
            "u": int(d["u"]),
            "pu": int(d["pu"]) if d.get("pu") is not None else None,
        }
    except (KeyError, TypeError, ValueError):
        row = None
    if row is None or bids is None or asks is None:
        _bad()
        return None
    return row | {"bids": bids, "asks": asks}


def parse_binance_agg_trade(msg: Any) -> dict[str, Any] | None:
    """A combined-stream ``aggTrade`` -> trade row; None for anything else."""
    d = msg.get("data") if isinstance(msg, dict) else None
    if not isinstance(d, dict) or d.get("e") != "aggTrade":
        return None
    try:
        row = {
            "kind": "trade",
            "symbol": str(d["s"]),
            "ts": int(d["T"]),
            "price": float(d["p"]),
            "qty": float(d["q"]),
            "side": "sell" if d["m"] is True else "buy" if d["m"] is False else None,
        }
    except (KeyError, TypeError, ValueError):
        row = None
    if row is None or row["side"] is None or not (row["price"] > 0 and row["qty"] > 0):
        _bad()
        return None
    return row


def parse_bybit(msg: Any) -> list[dict[str, Any]]:
    """A Bybit v5 linear ``orderbook.*`` or ``publicTrade.*`` message -> rows; [] for acks, pongs, other topics."""
    topic = msg.get("topic") if isinstance(msg, dict) else None
    if not isinstance(topic, str):
        return []
    data = msg.get("data")
    if topic.startswith("orderbook."):
        if not isinstance(data, dict):
            _bad()
            return []
        bids, asks = levels_of(data.get("b")), levels_of(data.get("a"))
        try:
            u, ts, symbol = int(data["u"]), int(msg.get("cts") or msg["ts"]), str(data["s"])
        except (KeyError, TypeError, ValueError):
            u = None
        if u is None or bids is None or asks is None or msg.get("type") not in ("snapshot", "delta"):
            _bad()
            return []
        kind = "snapshot" if msg.get("type") == "snapshot" or u == 1 else "delta"  # u = 1: snapshot after a restart (docs)
        return [{"kind": kind, "symbol": symbol, "ts": ts, "u": u, "bids": bids, "asks": asks}]
    if topic.startswith("publicTrade."):
        rows: list[dict[str, Any]] = []
        for t in data if isinstance(data, list) else []:
            try:
                row = {
                    "kind": "trade",
                    "symbol": str(t["s"]),
                    "ts": int(t["T"]),
                    "price": float(t["p"]),
                    "qty": float(t["v"]),
                    "side": {"Buy": "buy", "Sell": "sell"}.get(t["S"]),
                }
            except (KeyError, TypeError, ValueError):
                row = None
            if row is None or row["side"] is None or not (row["price"] > 0 and row["qty"] > 0):
                _bad()
                continue
            rows.append(row)
        return rows
    return []


async def merge_streams(primary: AsyncIterator[dict[str, Any]], *others: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Rows of several streams in arrival order. Ends when all have ended; re-raises the ``primary`` stream's error
    (no book = a failed run). The others' errors are not raised: wrap them in ``secondary_feed`` so they are logged and
    end with a ``_drop`` row."""
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_MAX)
    done = object()

    async def pump(gen: AsyncIterator[dict[str, Any]]) -> None:
        # ``done`` only when the feed ended by itself (or failed): a cancelled pump must not wait on a full queue that
        # nobody reads any more, or aclose() hangs (2026-11-18 review).
        try:
            async for row in gen:
                await queue.put(row)
        except asyncio.CancelledError:
            raise
        except BaseException:
            await queue.put(done)
            raise
        await queue.put(done)

    gens = (primary, *others)
    tasks = [asyncio.create_task(pump(g)) for g in gens]
    try:
        left = len(tasks)
        while left:
            item = await queue.get()
            if item is done:
                left -= 1
                continue
            yield item
        exc = tasks[0].exception()
        if exc is not None:
            raise exc
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t


async def secondary_feed(gen: AsyncIterator[dict[str, Any]], feed: str) -> AsyncIterator[dict[str, Any]]:
    """A feed whose failure must not end the run (trades next to the book): its error (a venue down for the whole window,
    or a bug such as a parser error, which ``_ws_consume`` does not count) is logged and ends the feed with a ``_drop``
    row, so the consumer marks the trade counts of open walls as a lower bound instead of losing them silently."""
    try:
        async for row in gen:
            yield row
    except Exception as e:  # noqa: BLE001
        log.warning("book stream %s ended early for this run: %s: %s", feed, type(e).__name__, e)
        yield {"kind": "_drop", "feed": feed, "error": f"{type(e).__name__}: {e}"}


async def binance_book_stream(symbols: list[str], *, max_seconds: float, stats: StreamGroup) -> AsyncIterator[dict[str, Any]]:
    """Depth diffs (``/public/``) and aggTrades (``/market/``) of the symbols for at most ``max_seconds``; the feeds'
    connection counters go to ``stats.feeds['depth']`` / ``['trades']``."""
    depth = stats.feeds.setdefault("depth", StreamStats())
    trades = stats.feeds.setdefault("trades", StreamStats())
    async for row in merge_streams(
        ws_mod._ws_consume(
            binance_depth_url(symbols),
            None,
            parse_binance_depth,
            max_seconds=max_seconds,
            stats=depth,
            lifecycle="depth",
            label="book binance:depth",
        ),
        secondary_feed(
            ws_mod._ws_consume(
                binance_trade_url(symbols),
                None,
                parse_binance_agg_trade,
                max_seconds=max_seconds,
                stats=trades,
                lifecycle="trades",
                label="book binance:trades",
            ),
            "trades",
        ),
    ):
        yield row


async def bybit_book_stream(
    symbols: list[str], *, max_seconds: float, stats: StreamGroup, depth: int = BYBIT_BOOK_DEPTH
) -> AsyncIterator[dict[str, Any]]:
    """``orderbook.<depth>`` and ``publicTrade`` of the symbols on one connection (feed name 'book')."""
    sub = {"op": "subscribe", "args": [f"orderbook.{depth}.{s}" for s in symbols] + [f"publicTrade.{s}" for s in symbols]}
    async for row in ws_mod._ws_consume(
        BYBIT_WS_URL,
        sub,
        parse_bybit,
        max_seconds=max_seconds,
        stats=stats.feeds.setdefault("book", StreamStats()),
        heartbeat=json.dumps({"op": "ping"}),
        error_of=ws_mod._bybit_error,
        lifecycle="book",
        label="book bybit:book",
    ):
        yield row
