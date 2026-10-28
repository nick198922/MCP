"""Hourly flow snapshot rows (pure: no I/O, time passed in).

Docs: docs/ideas/IDEA-20261028-market-flow-reader.md, step 2 (forward-only history of what has no free intraday past:
order-book depth, OI, funding and the long/short account ratio across venues). One row per (UTC hour, base, venue,
market); the collector `collector/flow_snapshot.py` does the I/O.

Order-book depth is the quote notional (USD, USDT read as USD) resting within 0.1 %, 0.5 % and 1 % of the book mid on
each side. Public REST books are truncated (live 2026-10-28, BTC: Binance spot 5000 levels reach ~0.9-1.3 %, USD-M 1000
levels ~0.15 %, Bybit spot 200 / linear 500 levels ~0.1-0.17 %, OKX books-full 5000 levels ~0.8-1.3 %), so a band the
fetched levels do not reach on that side is NULL, never a partial sum; ``reach_bid`` / ``reach_ask`` say how far the
book went. Hyperliquid answers 20 levels a side, aggregated to ``nSigFigs`` significant figures of the price (bids
rounded down, asks up): a band is computed only when it spans at least :data:`MIN_STEPS_PER_BAND` price steps, so the
partly-in-band bucket at the edge, which is left out, is at most about 1/8 of the band.

Units: depth USD; ``reach_*`` and the bands are decimal fractions of the mid (0.01 = 1 %); ``oi_coins`` base coins,
``oi_usd`` = coins x the row's book mid; ``funding_rate`` the venue's current (predicted) rate per interval, decimal;
``funding_annualized`` simple, rate x 8760 / interval hours; ``long_share`` fraction of accounts long (not notional).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from .derivatives import annualize_funding

ASSETS = ("BTC", "ETH")
VENUE_MARKETS = (
    ("binance", "spot"),
    ("binance", "perp"),
    ("bybit", "spot"),
    ("bybit", "perp"),
    ("okx", "spot"),
    ("okx", "perp"),
    ("hyperliquid", "perp"),
)
BANDS = (("10bp", 0.001), ("50bp", 0.005), ("100bp", 0.01))
MIN_STEPS_PER_BAND = 8
HL_SIG_FIGS = (4, 3)  # finest first: BTC ~$10 / $100 steps, ETH ~$1 / $10 at 2026-10 prices

DEPTH_KEYS = ("mid", "reach_bid", "reach_ask", *(f"{side}_usd_{label}" for label, _ in BANDS for side in ("bid", "ask")))
ROW_KEYS = (*DEPTH_KEYS, "oi_coins", "oi_usd", "funding_rate", "funding_hours", "funding_annualized", "long_share", "missing")


def fnum(v: Any) -> float | None:
    """A finite float (sign kept), else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def levels(rows: Any, size_mult: float = 1.0) -> list[tuple[float, float]]:
    """``[(price, size_in_base)]`` from venue levels: lists ``[price, size, ...]`` (ccxt, OKX raw strings) or Hyperliquid
    dicts ``{"px", "sz"}``. ``size_mult`` converts contracts to coins (OKX swaps: ``ctVal``). Bad or non-positive levels
    are skipped."""
    out: list[tuple[float, float]] = []
    for lv in rows if isinstance(rows, list) else []:
        if isinstance(lv, dict):
            p, s = fnum(lv.get("px")), fnum(lv.get("sz"))
        elif isinstance(lv, (list, tuple)) and len(lv) >= 2:
            p, s = fnum(lv[0]), fnum(lv[1])
        else:
            continue
        if p is not None and s is not None and p > 0 and s > 0:
            out.append((p, s * size_mult))
    return out


def empty_depth() -> dict[str, Any]:
    return dict.fromkeys(DEPTH_KEYS)


def depth_bands(
    bids: list[tuple[float, float]], asks: list[tuple[float, float]], step: float = 0.0, mid: float | None = None
) -> dict[str, Any]:
    """Mid, reach and the USD depth of each band (see the module docstring). ``step`` = the price step of an aggregated
    book (0 for a raw one); ``mid`` = a reference mid for the band edges (default: this book's best bid/ask midpoint).
    An empty side or a crossed book gives all None."""
    bids = sorted(bids, key=lambda x: -x[0])
    asks = sorted(asks, key=lambda x: x[0])
    out = empty_depth()
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return out
    mid = mid if mid is not None and mid > 0 else (bids[0][0] + asks[0][0]) / 2.0
    reach_bid, reach_ask = 1.0 - bids[-1][0] / mid, asks[-1][0] / mid - 1.0
    out.update(mid=mid, reach_bid=reach_bid, reach_ask=reach_ask)
    for label, w in BANDS:
        if step > 0 and w * mid < MIN_STEPS_PER_BAND * step:
            continue
        if reach_bid >= w:
            lo = mid * (1.0 - w)
            out[f"bid_usd_{label}"] = sum(p * s for p, s in bids if p >= lo)
        if reach_ask >= w:
            hi = mid * (1.0 + w)
            out[f"ask_usd_{label}"] = sum(p * s for p, s in asks if p <= hi)
    return out


def merge_depth(parts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Several books of one market, finest first (Hyperliquid at 4 then 3 significant figures): the mid of the first
    book that has one, each band from the first book that has it, reach = the farthest any book went."""
    out = empty_depth()
    for d in parts:
        if out["mid"] is None and d.get("mid") is not None:
            out["mid"] = d["mid"]
        for k in ("reach_bid", "reach_ask"):
            if d.get(k) is not None and (out[k] is None or d[k] > out[k]):
                out[k] = d[k]
        for k in DEPTH_KEYS[3:]:
            if out[k] is None and d.get(k) is not None:
                out[k] = d[k]
    return out


def sig_fig_step(price: float, n_sig_figs: int) -> float:
    """Price step of a book aggregated to ``n_sig_figs`` significant figures (80475 at 4 -> 10, 2332 at 3 -> 10)."""
    return 10.0 ** (math.floor(math.log10(price)) - n_sig_figs + 1)


def hl_depth(books: Iterable[tuple[int, Any]]) -> dict[str, Any]:
    """Depth of Hyperliquid ``l2Book`` answers ``[(n_sig_figs, raw)]``, finest first. Every book is measured from the
    mid of the finest valid one (an aggregated book's own mid is off by up to half a step). A malformed answer is
    skipped."""
    parts: list[dict[str, Any]] = []
    ref: float | None = None
    for nsf, raw in books:
        try:
            bids, asks = levels(raw["levels"][0]), levels(raw["levels"][1])
        except (KeyError, IndexError, TypeError):
            continue
        if bids and asks:
            d = depth_bands(bids, asks, sig_fig_step(max(p for p, _ in asks), nsf), mid=ref)
            ref = ref if ref is not None else d["mid"]
            parts.append(d)
    return merge_depth(parts)


def long_share_from_ratio(ratio: Any) -> float | None:
    """Fraction of accounts long from a longs/shorts account ratio (OKX rubik): r / (1 + r)."""
    r = fnum(ratio)
    return None if r is None or r < 0 else r / (1.0 + r)


def share(v: Any) -> float | None:
    x = fnum(v)
    return x if x is not None and 0.0 <= x <= 1.0 else None


def flow_row(
    venue: str,
    market: str,
    *,
    depth: Mapping[str, Any] | None = None,
    oi_coins: Any = None,
    funding_rate: Any = None,
    funding_hours: float | None = None,
    long_share: Any = None,
    missing: Iterable[str] = (),
) -> dict[str, Any]:
    """One table row (without ``ts`` / ``base`` / ``fetched_ts``). ``missing`` = names of the parts that failed
    (``book``, ``oi``, ``funding``, ``funding_interval``, ``long_share``), stored comma-separated or NULL."""
    row: dict[str, Any] = {"venue": venue, "market": market, **empty_depth(), **(depth or {})}
    oi = fnum(oi_coins)
    row["oi_coins"] = oi if oi is not None and oi >= 0 else None
    row["oi_usd"] = row["oi_coins"] * row["mid"] if row["oi_coins"] is not None and row["mid"] is not None else None
    rate = fnum(funding_rate)
    hours = funding_hours if rate is not None and funding_hours and funding_hours > 0 else None
    row["funding_rate"], row["funding_hours"] = (rate, hours) if hours else (None, None)
    row["funding_annualized"] = annualize_funding(rate, hours) if rate is not None and hours else None
    row["long_share"] = share(long_share)
    names = sorted(set(missing))
    row["missing"] = ",".join(names) if names else None
    return row


def has_data(row: Mapping[str, Any]) -> bool:
    """A row worth storing: at least the book mid or one perp field."""
    return any(row.get(k) is not None for k in ("mid", "oi_coins", "funding_rate", "long_share"))
