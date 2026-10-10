"""Order-book metrics: spread, depth imbalance, and slippage for a given order size."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

Level = Sequence[float]  # [price, size_in_base, ...]  — some venues add order-count etc.


def book_stats(bids: list[Level], asks: list[Level], depth_pcts: tuple[float, ...] = (0.1, 0.5, 1.0)) -> dict[str, Any]:
    if not bids or not asks:
        return {"mid": None, "spread_bps": None, "depth": {}}
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    depth: dict[str, Any] = {}
    for pct in depth_pcts:
        lo, hi = mid * (1 - pct / 100), mid * (1 + pct / 100)
        bid_usd = sum(level[0] * level[1] for level in bids if level[0] >= lo)
        ask_usd = sum(level[0] * level[1] for level in asks if level[0] <= hi)
        tot = bid_usd + ask_usd
        depth[f"{pct}%"] = {
            "bid_usd": bid_usd,
            "ask_usd": ask_usd,
            "imbalance": (bid_usd - ask_usd) / tot if tot else None,  # +1 all bids, -1 all asks
        }
    return {
        "mid": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": (best_ask - best_bid) / mid * 1e4,
        "depth": depth,
    }


def walk_book(levels: list[Level], notional_usd: float, mid: float, side: str) -> dict[str, Any]:
    """Fill `notional_usd` against `levels` (asks for a buy, bids for a sell).

    Each level may carry extra fields past [price, size] (e.g. OKX adds order-count); only the
    first two are used.
    """
    remaining = notional_usd
    cost = 0.0
    qty = 0.0
    for level in levels:
        price, size = level[0], level[1]
        level_usd = price * size
        take = min(remaining, level_usd)
        qty += take / price
        cost += take
        remaining -= take
        if remaining <= 1e-9:
            break
    filled = notional_usd - max(remaining, 0.0)
    avg = cost / qty if qty else None
    slip = None
    if avg is not None:
        slip = ((avg - mid) / mid if side == "buy" else (mid - avg) / mid) * 1e4
    return {
        "side": side,
        "requested_usd": notional_usd,
        "filled_usd": filled,
        "unfilled_usd": max(remaining, 0.0),
        "avg_price": avg,
        "slippage_bps_vs_mid": slip,
    }
