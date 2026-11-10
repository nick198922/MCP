"""Pure analytics for Polymarket market payloads."""

from __future__ import annotations

import json
import math
from typing import Any


def _f(x: Any) -> float | None:
    """A finite float, else None: absent, not a number, NaN / inf or an integer beyond the float range (float() of a
    300+-digit JSON integer raised OverflowError out of the Polymarket tools; a NaN price reached the reply)."""
    try:
        v = float(x) if x is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return v if v is not None and math.isfinite(v) else None


def _maybe_json_list(x: Any) -> list[Any] | None:
    """A list as is, or a JSON text of a list decoded; None otherwise. Text that is not valid JSON, holds an integer of
    4300+ digits (ValueError, not JSONDecodeError) or nests too deep (RecursionError) is None too, not an exception."""
    if x is None:
        return None
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            parsed = json.loads(x)
            return parsed if isinstance(parsed, list) else None
        except (ValueError, RecursionError):  # JSONDecodeError is a ValueError
            return None
    return None


LIST_FIELDS = ("outcomes", "outcomePrices", "clobTokenIds")


def undecodable_fields(raw: dict[str, Any]) -> list[str]:
    """Names of Gamma's list fields that are present but not a list or a JSON text of one (`normalise_market` reads
    them as absent), so a tool can say so in `warnings` instead of showing a market without outcomes silently."""
    return [k for k in LIST_FIELDS if raw.get(k) not in (None, "") and _maybe_json_list(raw.get(k)) is None]


def devig_multi(prices: list[float | None]) -> list[float | None] | None:
    """Normalise outcome prices to sum = 1 (removes vig / handles rounding).

    Returns None when the sum is non-positive. Yes/No binary markets usually sum
    to ~1.00 already; multi-outcome prediction markets can sum to 1.02–1.05.
    `None` entries (unknown prices) are left out of the sum and stay `None` at the
    same index, so the output lines up with the input position by position.
    """
    s = sum(p for p in prices if p is not None)
    if s <= 0:
        return None
    return [p / s if p is not None else None for p in prices]


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
    # Devig over the numeric prices only, keeping the original positions: an outcome
    # whose price is non-numeric gets price_devigged=None, and the others are scaled
    # by the sum of the numeric prices (with a price missing, that sum is not a full
    # book, so the devigged numbers are a best effort, not a distribution of all outcomes).
    devig = devig_multi(prices) if prices else None
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
