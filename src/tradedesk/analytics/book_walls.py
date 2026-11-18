"""Order-book walls: a local L2 book kept from diff updates, wall detection, the wall life cycle and its cause, and the
per-minute depth around the mid (pure: no I/O, every time is a venue timestamp in ms passed in).

Card: docs/ideas/IDEA-20261114-book-walls.md, item 1 (collector only; the December test reads the tables). A *wall* is
one price level of one side that is unusually large:

* inside ``BAND`` (+-2 %) of the mid and inside ``VIEW_MARGIN`` of the part of the book the feed shows (``Book.view``);
* size >= ``K_MEDIAN`` x the median size of the ``TOP_N`` levels of its side nearest the mid, and >= the coin minimum
  of the symbol (``MIN_QTY``).

Live 2026-11-18 the median of the top 50 levels is dust (BTC 0.003-0.03, ETH 2-6 coins), so in practice the coin
minimum sets the bar; the largest levels were 15-115 BTC and 600-5000 ETH. The constants are a first guess for a
descriptive calibration on the first day of data (card, item 1), not fitted values.

Life cycle: a wall opens when a level passes the bar; it stays open while its size stays >= ``HOLD_FRACTION`` x the bar
it opened at (so a wall that is half eaten or briefly trimmed is the same wall), and closes when it drops below that or
leaves the visible book. Trades on its price that hit it (taker sells on a bid wall, taker buys on an ask wall) are
summed from its opening until ``GRACE_MS`` after its close, because the trade and depth feeds are not in lockstep
(Binance sends them on two connections). The cause, decided after the grace period:

* ``consumed``: traded on the level >= ``CONSUMED_SHARE`` of its largest size;
* ``gap``: the feed broke while it was open (reconnect, sequence gap) and it was gone, or the break lasted longer than
  ``MAX_GAP_MS``, when the book came back: its fate is unknown;
* ``out_of_view``: its level left the part of the book the feed shows (price moved away); not a cancel;
* ``pulled_near``: removed while the mid was within ``NEAR_DIST`` of it (what a spoof looks like);
* ``withdrawn``: removed further away.

``withdrawn`` walls that lived less than ``MIN_LIFE_MS`` are not stored (requote flicker at our latency, not a
decision), only counted in the minute row (``short_walls``). Every other close is stored whatever its life, above all
a short ``pulled_near``: a spoof is pulled seconds after the price comes near, and these are the events of the card's
H1. A wall that saw a feed break (``gaps`` > 0) has ``traded`` as a lower bound.

Distances (``dist_*``) are decimal fractions of the mid (0.001 = 0.1 % = 10 bp), positive away from the mid; sizes and
depth are in coins of the base asset.
"""

from __future__ import annotations

import heapq
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

BAND = 0.02  # walls are looked for within +-2 % of the mid (card); the visible book is usually narrower (see Book.view)
VIEW_MARGIN = 0.8  # ... and within 80 % of the visible distance: levels at its edge blink in and out of view
# A full-book snapshot (Coinbase) is cut to this distance from its mid (near_mid): the whole BAND stays in reach
# (VIEW_MARGIN x 2.5 % = 2 %); the levels further away can never hold a wall.
FULL_BOOK_KEEP = BAND / VIEW_MARGIN
TOP_N = 50  # levels of a side nearest the mid whose median size is the yardstick
K_MEDIAN = 50.0  # a wall is >= this many times that median ...
# ... and >= this many coins (~$1.6 M / ~$1.3 M on 2026-11-18). A 3-minute live trial with 10 BTC / 300 ETH found 15-57
# walls open per symbol and ~1.4k stored a minute: that bar caught ordinary market-maker size, not walls.
MIN_QTY: dict[str, float] = {"BTC": 25.0, "ETH": 750.0}
HOLD_FRACTION = 0.5  # an open wall stays open while its size >= this share of the bar it opened at
CONSUMED_SHARE = 0.5  # traded on the level >= this share of the wall's largest size -> consumed (card)
NEAR_DIST = 0.001  # removed within 10 bp of the mid -> pulled_near (spoof-like), else withdrawn
MIN_LIFE_MS = 5_000  # shorter 'withdrawn' walls are counted, not stored (median life was 3 s in the trial: requotes)
GRACE_MS = 1_000  # trades up to this long after the close still count (depth and trades arrive on separate feeds)
MAX_GAP_MS = 120_000  # a wall not seen for longer (feed down) is closed as 'gap'
DEPTH_BANDS = (0.005, 0.01, 0.02)  # minute depth: coins within +-0.5 / 1 / 2 % of the mid
DEPTH_KEYS = ("05", "1", "2")
MINUTE_MS = 60_000

REASONS = ("consumed", "pulled_near", "withdrawn", "gap", "out_of_view")
EVENT_KEYS = (
    "venue", "symbol", "side", "price", "open_ms", "close_ms", "size_open", "size_max", "size_end", "threshold",
    "dist_open", "dist_min", "dist_close", "mid_close", "traded", "reason", "gaps",
)  # fmt: skip


def min_qty_for(symbol: str) -> float | None:
    """The coin minimum of a USDT- or USD-quoted symbol ('BTCUSDT', Coinbase 'BTC-USD' -> MIN_QTY['BTC']), None for a
    base without one."""
    base = symbol.upper().removesuffix("USDT").removesuffix("-USD")
    return MIN_QTY.get(base)


def near_mid(
    bids: list[tuple[float, float]], asks: list[tuple[float, float]], keep: float
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]] | None:
    """The levels of a full-book snapshot within ``keep`` (decimal) of its mid; None when there is no mid (a side empty
    or crossed), so the caller can refuse the snapshot instead of keeping the whole book. For a venue that sends the
    whole book (Coinbase: 13k / 25k levels for BTC-USD on 2026-11-18), so the per-update wall scan only walks the part
    of the book that can hold a wall."""
    pos = [p for p, q in bids if q > 0], [p for p, q in asks if q > 0]
    if not pos[0] or not pos[1] or max(pos[0]) >= min(pos[1]):
        return None
    mid = (max(pos[0]) + min(pos[1])) / 2
    lo, hi = mid * (1 - keep), mid * (1 + keep)
    return [lv for lv in bids if lv[0] >= lo], [lv for lv in asks if lv[0] <= hi]


class Book:
    """Local L2 book of one symbol: price -> size per side.

    ``bounded=True`` (Binance USD-M and spot; Coinbase, whose full-book snapshot is first cut by ``near_mid``): the
    book is a snapshot of the nearest levels plus every later diff. Diffs
    also report levels far outside the snapshot, but the levels there that did not change are unknown, so updates
    outside the snapshot's range are dropped and ``view`` stays the snapshot's deepest bid / ask. ``bounded=False``
    (Bybit ``orderbook.N``): the venue keeps the top N levels itself (a level leaving them is deleted), so ``view`` is
    the deepest level present; ``depth`` = N also trims each side to its N levels nearest the mid after a diff, in
    case the venue does not delete them (then the view would widen for the rest of the run).
    """

    def __init__(self, bounded: bool, depth: int | None = None) -> None:
        self.bounded = bounded
        self.depth = depth
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.bid_floor: float | None = None
        self.ask_cap: float | None = None
        self.ready = False

    def reset(self, bids: Iterable[tuple[float, float]], asks: Iterable[tuple[float, float]]) -> None:
        """Replace the book with a full snapshot."""
        self.bids = {p: q for p, q in bids if q > 0}
        self.asks = {p: q for p, q in asks if q > 0}
        self.bid_floor = min(self.bids) if self.bounded and self.bids else None
        self.ask_cap = max(self.asks) if self.bounded and self.asks else None
        self.ready = bool(self.bids) and bool(self.asks)

    def clear(self) -> None:
        """The feed broke: the book is unknown until the next snapshot."""
        self.bids, self.asks, self.bid_floor, self.ask_cap, self.ready = {}, {}, None, None, False

    def apply(self, bids: Iterable[tuple[float, float]], asks: Iterable[tuple[float, float]]) -> None:
        """Apply a diff: size 0 removes the level."""
        lo = self.bid_floor if self.bid_floor is not None else -math.inf
        hi = self.ask_cap if self.ask_cap is not None else math.inf
        book = self.bids
        for p, q in bids:
            if p >= lo:
                if q > 0:
                    book[p] = q
                else:
                    book.pop(p, None)
        book = self.asks
        for p, q in asks:
            if p <= hi:
                if q > 0:
                    book[p] = q
                else:
                    book.pop(p, None)
        n = self.depth
        if n is not None:
            if len(self.bids) > n:
                for p in heapq.nsmallest(len(self.bids) - n, self.bids):
                    del self.bids[p]
            if len(self.asks) > n:
                for p in heapq.nlargest(len(self.asks) - n, self.asks):
                    del self.asks[p]

    def best(self) -> tuple[float, float] | None:
        """(best bid, best ask), None when a side is empty or the book is crossed."""
        if not self.bids or not self.asks:
            return None
        b, a = max(self.bids), min(self.asks)
        return (b, a) if b < a else None

    def view(self) -> tuple[float, float] | None:
        """(deepest bid, deepest ask) the book is known down / up to; None when a side is empty."""
        if not self.bids or not self.asks:
            return None
        lo = self.bid_floor if self.bid_floor is not None else min(self.bids)
        hi = self.ask_cap if self.ask_cap is not None else max(self.asks)
        return lo, hi


class DiffSequencer:
    """Binance diff-depth sequencing (docs "How to manage a local order book correctly"). USD-M: after a REST snapshot
    with ``lastUpdateId`` L, drop events with ``u`` < L; the first applied one must have ``U`` <= L <= ``u``; every
    later one must have ``pu`` equal to the previous event's ``u``. Spot (``spot=True``; its events carry no ``pu``):
    drop events with ``u`` <= L; the first applied one must have ``U`` <= L + 1 <= ``u``; every later one must have
    ``U`` equal to the previous event's ``u`` + 1. Anything else is a gap: take a new snapshot. In both, ``last`` is
    the id of the last update the book holds (a snapshot's L or an applied event's ``u``)."""

    def __init__(self, spot: bool = False) -> None:
        self.spot = spot
        self.last: int | None = None
        self.first = True

    @property
    def synced(self) -> bool:
        return self.last is not None

    def snapshot(self, last_update_id: int) -> None:
        self.last, self.first = int(last_update_id), True

    def reset(self) -> None:
        self.last, self.first = None, True

    def check(self, first_id: int, final_id: int, prev_final_id: int | None) -> str:
        """'apply' | 'skip' (already in the snapshot) | 'gap' | 'unsynced' (no snapshot yet)."""
        if self.last is None:
            return "unsynced"
        if self.first:
            nxt = self.last + 1 if self.spot else self.last  # the update id the first applied event must cover
            if final_id < nxt:
                return "skip"
            if first_id <= nxt <= final_id:
                self.last, self.first = final_id, False
                return "apply"
            return "gap"
        if (first_id == self.last + 1) if self.spot else (prev_final_id == self.last):
            self.last = final_id
            return "apply"
        return "gap"


def wall_threshold(top_sizes: Iterable[float], k: float, min_qty: float) -> float:
    """The bar for a wall: max(k x median of ``top_sizes``, ``min_qty``); ``min_qty`` alone without sizes."""
    sizes = list(top_sizes)
    return max(k * statistics.median(sizes), min_qty) if sizes else min_qty


def distance(side: str, price: float, mid: float) -> float:
    """Decimal distance of a level from the mid, positive on its own side of the book (a bid below the mid)."""
    return (mid - price) / mid if side == "bid" else (price - mid) / mid


def classify(*, traded: float, size_max: float, dist_close: float | None, technical: str | None) -> str:
    """The cause of a closed wall (module docstring): consumed first, then a technical close, then by distance."""
    if size_max > 0 and traded >= CONSUMED_SHARE * size_max:
        return "consumed"
    if technical is not None:
        return technical
    return "pulled_near" if dist_close is not None and dist_close <= NEAR_DIST else "withdrawn"


def depth_bands(book: Book, mid: float, bands: Iterable[float] = DEPTH_BANDS) -> dict[str, float]:
    """Coins resting within each band of the mid, per side (``bid_05`` ... ``ask_2``); only the visible book counts."""
    out: dict[str, float] = {}
    for band, key in zip(bands, DEPTH_KEYS, strict=True):
        lo, hi = mid * (1 - band), mid * (1 + band)
        out[f"bid_{key}"] = sum(q for p, q in book.bids.items() if p >= lo)
        out[f"ask_{key}"] = sum(q for p, q in book.asks.items() if p <= hi)
    return out


@dataclass
class Wall:
    side: str  # 'bid' | 'ask'
    price: float
    open_ms: int
    threshold: float
    size_open: float
    size_max: float
    last_seen_ms: int
    dist_open: float
    dist_min: float
    dist_last: float
    mid_last: float
    traded: float = 0.0
    gaps: int = 0
    gap_pending: bool = False
    close_ms: int | None = None
    size_end: float | None = None
    technical: str | None = None


class WallTracker:
    """Walls of one venue x symbol. Feed it every book state (``on_book``), every trade (``on_trade``) and every feed
    break (``depth_gap`` / ``trades_feed``); closed walls come back as event dicts (``EVENT_KEYS``) once their grace
    period is over, and ``on_book`` also returns one depth row at the first book of each new UTC minute."""

    def __init__(
        self,
        venue: str,
        symbol: str,
        min_qty: float,
        *,
        k: float = K_MEDIAN,
        band: float = BAND,
        top_n: int = TOP_N,
    ) -> None:
        self.venue, self.symbol, self.min_qty, self.k, self.band, self.top_n = venue, symbol, min_qty, k, band, top_n
        self.open: dict[tuple[str, float], Wall] = {}
        self.closing: list[Wall] = []
        self.trades_down = False
        self.minute: int | None = None  # start (ms) of the minute of the last depth row
        self.gaps_since_row = 0
        self.short_since_row = 0

    # ---- feed breaks ----

    def depth_gap(self) -> None:
        """The depth feed broke (reconnect, sequence gap): open walls wait for the next book to see if they survived."""
        self.gaps_since_row += 1
        for w in self.open.values():
            if not w.gap_pending:
                w.gap_pending, w.gaps = True, w.gaps + 1

    def trades_feed(self, up: bool) -> None:
        """The trade feed (re)connected (``up``) or dropped. Either way trades may have been missed, so the open walls'
        ``traded`` becomes a lower bound; walls opened while it is down start with a gap. (Not in the minute row's
        ``gaps``, which counts book breaks; trade-feed breaks are in the stream counters.)"""
        if not self.trades_down:
            for w in self.open.values():
                w.gaps += 1
        self.trades_down = not up

    # ---- trades ----

    def on_trade(self, ts_ms: int, price: float, qty: float, taker_side: str) -> list[dict[str, Any]]:
        """A trade (``taker_side`` 'buy' | 'sell'): added to the wall it hit (a bid wall is hit by sells)."""
        side = "bid" if taker_side == "sell" else "ask" if taker_side == "buy" else None
        if side is not None and qty > 0:
            w = self.open.get((side, price))
            if w is not None and ts_ms >= w.open_ms:
                w.traded += qty  # a wall reopened on the same price takes it, not the one closing there
            else:
                for c in self.closing:
                    if c.side == side and c.price == price and c.open_ms <= ts_ms <= (c.close_ms or 0) + GRACE_MS:
                        c.traded += qty
                        break
        return self._finalize(ts_ms)

    # ---- book ----

    def on_book(self, ts_ms: int, book: Book) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Re-evaluate the walls on the current book. Returns (closed wall events, depth row or None)."""
        events = self._finalize(ts_ms)
        best = book.best() if book.ready else None
        view = book.view() if best is not None else None
        if best is None or view is None:
            return events, None
        mid = (best[0] + best[1]) / 2
        for w in [w for w in self.open.values() if w.gap_pending]:
            levels = book.bids if w.side == "bid" else book.asks
            if ts_ms - w.last_seen_ms > MAX_GAP_MS or levels.get(w.price, 0.0) < HOLD_FRACTION * w.threshold:
                self._close(w, w.last_seen_ms, None, "gap")
            else:
                w.gap_pending = False
        for side in ("bid", "ask"):
            self._side(ts_ms, book, side, mid, view)
        return events + self._finalize(ts_ms), self._depth_row(ts_ms, book, mid, best, view)

    def _side(self, ts_ms: int, book: Book, side: str, mid: float, view: tuple[float, float]) -> None:
        levels = book.bids if side == "bid" else book.asks
        in_view = (lambda p: p >= view[0]) if side == "bid" else (lambda p: p <= view[1])
        for w in [w for (s, _), w in self.open.items() if s == side]:
            size = levels.get(w.price, 0.0)
            dist = distance(side, w.price, mid)
            w.dist_last, w.mid_last = dist, mid
            if not in_view(w.price):
                self._close(w, ts_ms, size, "out_of_view")
            elif size < HOLD_FRACTION * w.threshold:
                self._close(w, ts_ms, size, None)
            else:
                w.size_max, w.last_seen_ms, w.dist_min = max(w.size_max, size), ts_ms, min(w.dist_min, dist)
        candidates = [(p, q) for p, q in levels.items() if q >= self.min_qty and (side, p) not in self.open]
        if not candidates:
            return  # the bar is never below the coin minimum: skip the median
        top = heapq.nlargest(self.top_n, levels) if side == "bid" else heapq.nsmallest(self.top_n, levels)
        bar = wall_threshold((levels[p] for p in top), self.k, self.min_qty)
        reach = min(self.band, VIEW_MARGIN * distance(side, view[0] if side == "bid" else view[1], mid))
        for p, q in candidates:
            dist = distance(side, p, mid)
            if q < bar or dist > reach:
                continue
            self.open[(side, p)] = Wall(
                side=side, price=p, open_ms=ts_ms, threshold=bar, size_open=q, size_max=q, last_seen_ms=ts_ms,
                dist_open=dist, dist_min=dist, dist_last=dist, mid_last=mid, gaps=1 if self.trades_down else 0,
            )  # fmt: skip

    def _close(self, w: Wall, ts_ms: int, size_end: float | None, technical: str | None) -> None:
        self.open.pop((w.side, w.price), None)
        w.close_ms, w.size_end, w.technical = ts_ms, size_end, technical
        self.closing.append(w)

    def _finalize(self, now_ms: int) -> list[dict[str, Any]]:
        """Closed walls whose grace period is over, as events; short 'withdrawn' ones are only counted."""
        if not self.closing:
            return []
        out: list[dict[str, Any]] = []
        keep: list[Wall] = []
        for w in self.closing:
            if now_ms <= (w.close_ms or 0) + GRACE_MS:
                keep.append(w)
                continue
            reason = classify(traded=w.traded, size_max=w.size_max, dist_close=w.dist_last, technical=w.technical)
            if reason == "withdrawn" and (w.close_ms or 0) - w.open_ms < MIN_LIFE_MS:
                self.short_since_row += 1
                continue
            out.append(self.event(w, reason))
        self.closing = keep
        return out

    def event(self, w: Wall, reason: str) -> dict[str, Any]:
        return {
            "venue": self.venue, "symbol": self.symbol, "side": w.side, "price": w.price, "open_ms": w.open_ms,
            "close_ms": w.close_ms, "size_open": w.size_open, "size_max": w.size_max, "size_end": w.size_end,
            "threshold": w.threshold, "dist_open": w.dist_open, "dist_min": w.dist_min, "dist_close": w.dist_last,
            "mid_close": w.mid_last, "traded": w.traded, "reason": reason, "gaps": w.gaps,
        }  # fmt: skip

    def _depth_row(self, ts_ms: int, book: Book, mid: float, best: tuple[float, float], view: tuple[float, float]) -> dict[str, Any] | None:
        minute = ts_ms // MINUTE_MS * MINUTE_MS
        if self.minute is not None and minute <= self.minute:
            return None
        self.minute = minute
        row = {
            "venue": self.venue, "symbol": self.symbol, "ts": minute // 1000, "mid": mid,
            "spread": (best[1] - best[0]) / mid, **depth_bands(book, mid),
            "bid_view": max(0.0, (mid - view[0]) / mid), "ask_view": max(0.0, (view[1] - mid) / mid),
            "walls_bid": sum(1 for s, _ in self.open if s == "bid"), "walls_ask": sum(1 for s, _ in self.open if s == "ask"),
            "gaps": self.gaps_since_row, "short_walls": self.short_since_row,
        }  # fmt: skip
        self.gaps_since_row = self.short_since_row = 0
        return row


def levels_of(raw: Any) -> list[tuple[float, float]] | None:
    """``[[price, size], ...]`` (strings or numbers) -> floats; None when any entry is malformed or negative."""
    if not isinstance(raw, list):
        return None
    try:  # hot path: Binance diffs carry ~10k levels a second (live trial 2026-11-18)
        out = [(float(p), float(q)) for p, q in raw]
    except (TypeError, ValueError):
        return None
    for p, q in out:
        if not (0 < p < math.inf and 0 <= q < math.inf):  # NaN fails every comparison
            return None
    return out


def walls_summary(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count of stored wall events per reason (for logs and the calibration day)."""
    out = dict.fromkeys(REASONS, 0)
    for e in events:
        r = str(e.get("reason"))
        out[r] = out.get(r, 0) + 1
    return out
