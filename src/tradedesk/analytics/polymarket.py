"""Pure analytics for Polymarket market payloads."""

from __future__ import annotations

import json
from typing import Any


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _maybe_json_list(x: Any) -> list[Any] | None:
    if x is None:
        return None
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            parsed = json.loads(x)
            return parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            return None
    return None


def devig_multi(prices: list[float]) -> list[float] | None:
    """Normalise outcome prices to sum = 1 (removes vig / handles rounding).

    Returns None when the sum is non-positive. Yes/No binary markets usually sum
    to ~1.00 already; multi-outcome prediction markets can sum to 1.02–1.05.
    """
    s = sum(p for p in prices if p is not None)
    if s <= 0:
        return None
    return [p / s for p in prices]


def normalise_market(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Gamma `/markets` row to a stable shape.

    * `outcomes`, `outcomePrices` and `clobTokenIds` arrive as JSON-encoded strings
      in Gamma's payload; we decode them to real lists.
    * `volume` and `liquidity` can be either numeric or stringified; `*Num` suffix
      variants are preferred when present.
    """
    outcomes = _maybe_json_list(raw.get("outcomes"))
    prices_raw = _maybe_json_list(raw.get("outcomePrices"))
    tokens = _maybe_json_list(raw.get("clobTokenIds"))
    prices = [_f(p) for p in prices_raw] if prices_raw else None
    devig = devig_multi([p for p in prices if p is not None]) if prices else None
    n = max(len(outcomes or []), len(prices or []), len(tokens or []))
    rows: list[dict[str, Any]] = []
    for i in range(n):
        name = (outcomes or [None] * n)[i] if i < len(outcomes or []) else None
        price = (prices or [None] * n)[i] if i < len(prices or []) else None
        token = (tokens or [None] * n)[i] if i < len(tokens or []) else None
        rows.append(
            {
                "name": name,
                "price": price,
                "price_devigged": (devig[i] if devig and i < len(devig) else None),
                "token_id": token,
            }
        )

    return {
        "id": raw.get("id"),
        "slug": raw.get("slug"),
        "question": raw.get("question"),
        "end_date": raw.get("endDate") or raw.get("endDateIso"),
        "active": bool(raw.get("active")),
        "closed": bool(raw.get("closed")),
        "volume_usd": _f(raw.get("volumeNum") or raw.get("volume")),
        "liquidity_usd": _f(raw.get("liquidityNum") or raw.get("liquidity")),
        "outcomes": rows,
    }


def book_stats(book: dict[str, Any]) -> dict[str, Any]:
    """Best bid/ask/mid/spread from a CLOB `/book` payload.

    Polymarket's book has `bids` and `asks`, each a list of ``{price, size}``.
    Prices and sizes are strings in the API; we coerce them to float.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = _f(bids[0]["price"]) if bids and isinstance(bids[0], dict) else None
    best_ask = _f(asks[0]["price"]) if asks and isinstance(asks[0], dict) else None
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "bid_depth": sum(_f(b.get("size")) or 0 for b in bids if isinstance(b, dict)),
        "ask_depth": sum(_f(a.get("size")) or 0 for a in asks if isinstance(a, dict)),
    }
