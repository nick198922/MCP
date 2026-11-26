"""Hyperliquid HIP-4 outcome markets: pure parsing of ``outcomeMeta`` and outcome order books.

Encoding (official asset-ID page, checked 2026-11-26): ``encoding = 10 * outcome + side`` (side 0 or 1); spot coin
``#<encoding>``, token name ``+<encoding>``, asset id ``100_000_000 + encoding``. Recurring protocol series carry a
``description`` such as ``class:priceBinary|underlying:BTC|expiry:20261127-0600|targetPrice:60789|period:1d``
(settles YES iff the interpolated HyperCore mark at ``expiry`` is >= ``targetPrice``). Prices are fractions of the
1-unit payout (0.62 = 62 %). No I/O here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

OUTCOME_ASSET_OFFSET = 100_000_000
HOUR_MS = 3_600_000


def encoding(outcome: int, side: int) -> int:
    if side not in (0, 1):
        raise ValueError("side must be 0 or 1")
    if outcome < 0:
        raise ValueError("outcome must be >= 0")
    return 10 * outcome + side


def outcome_coin(outcome: int, side: int = 0) -> str:
    """Coin name for info requests (``l2Book``, ``candleSnapshot``, ``recentTrades``): ``#<encoding>``."""
    return f"#{encoding(outcome, side)}"


def outcome_asset_id(outcome: int, side: int = 0) -> int:
    """Integer asset id for exchange actions: ``100_000_000 + encoding``."""
    return OUTCOME_ASSET_OFFSET + encoding(outcome, side)


def decode_coin(coin: str) -> tuple[int, int] | None:
    """``#479480`` -> ``(47948, 0)``; anything else -> None."""
    if not isinstance(coin, str) or not coin.startswith("#") or not coin[1:].isdigit():
        return None
    enc = int(coin[1:])
    side = enc % 10
    if side not in (0, 1):
        return None
    return enc // 10, side


def parse_description(desc: Any) -> dict[str, str]:
    """``a:b|c:d:e`` -> ``{"a": "b", "c": "d:e"}`` (split on the first colon); a segment without a colon maps to ""."""
    out: dict[str, str] = {}
    if not isinstance(desc, str):
        return out
    for seg in desc.split("|"):
        if not seg:
            continue
        k, _, v = seg.partition(":")
        out[k.strip()] = v.strip()
    return out


def parse_stamp(s: Any) -> int | None:
    """``20261127-0600`` (UTC) -> epoch ms; malformed -> None."""
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.strptime(s, "%Y%m%d-%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def recurring_series(meta: Any) -> list[dict[str, Any]]:
    """Protocol recurring outcomes (description has ``class:``) from an ``outcomeMeta`` answer, sorted by outcome id.

    Each row: outcome, cls, underlying, period, expiry_ms, target (priceBinary) or thresholds (priceBucket), coin_yes,
    coin_no. Deployer-venue (``template:*``) outcomes are not included.
    """
    rows: list[dict[str, Any]] = []
    outs = meta.get("outcomes") if isinstance(meta, dict) else None
    for o in outs or []:
        if not isinstance(o, dict) or not isinstance(o.get("outcome"), int):
            continue
        d = parse_description(o.get("description"))
        if "class" not in d:
            continue
        th = [v for v in (_num(t) for t in d.get("priceThresholds", "").split(",") if t) if v is not None]
        rows.append(
            {
                "outcome": o["outcome"],
                "cls": d["class"],
                "underlying": d.get("underlying"),
                "period": d.get("period"),
                "expiry_ms": parse_stamp(d.get("expiry")),
                "target": _num(d.get("targetPrice")),
                "thresholds": th,
                "coin_yes": outcome_coin(o["outcome"], 0),
                "coin_no": outcome_coin(o["outcome"], 1),
            }
        )
    return sorted(rows, key=lambda r: r["outcome"])


def series_inventory(meta: Any) -> dict[str, int]:
    """Count of listed outcomes by kind: ``recurring:<class>:<underlying>:<period>`` or ``<venue>:<template name>``."""
    inv: dict[str, int] = {}
    outs = meta.get("outcomes") if isinstance(meta, dict) else None
    for o in outs or []:
        if not isinstance(o, dict):
            continue
        d = parse_description(o.get("description"))
        if "class" in d:
            key = f"recurring:{d['class']}:{d.get('underlying')}:{d.get('period')}"
        else:
            key = f"{o.get('venue', '-')}:{o.get('name', '?')}"
        inv[key] = inv.get(key, 0) + 1
    return dict(sorted(inv.items()))


def _levels(side: Any) -> list[tuple[float, float]]:
    out = []
    for lv in side or []:
        if not isinstance(lv, dict):
            continue
        px, sz = _num(lv.get("px")), _num(lv.get("sz"))
        if px is not None and sz is not None and sz > 0:
            out.append((px, sz))
    return out


def book_summary(book: Any, band: float = 0.05) -> dict[str, Any]:
    """Descriptive summary of one ``l2Book`` answer for an outcome coin (prices are fractions of the 1-unit payout).

    ``depth_*_usd`` = sum(px * sz) over all returned levels; ``*_band_usd`` only over levels within ``band`` (0.05 =
    5 percentage points) of the mid. ``spread`` in price units (0.02 = 2 pp). Empty or one-sided book -> mid None.
    """
    lv = book.get("levels") if isinstance(book, dict) else None
    bids = _levels(lv[0]) if isinstance(lv, list) and len(lv) > 0 else []
    asks = _levels(lv[1]) if isinstance(lv, list) and len(lv) > 1 else []
    bb = max((p for p, _ in bids), default=None)
    ba = min((p for p, _ in asks), default=None)
    mid = (bb + ba) / 2 if bb is not None and ba is not None else None
    res: dict[str, Any] = {
        "time_ms": book.get("time") if isinstance(book, dict) else None,
        "n_bid_levels": len(bids),
        "n_ask_levels": len(asks),
        "best_bid": bb,
        "best_ask": ba,
        "mid": mid,
        "spread": (ba - bb) if mid is not None else None,
        "depth_bid_usd": sum(p * s for p, s in bids),
        "depth_ask_usd": sum(p * s for p, s in asks),
        "depth_bid_contracts": sum(s for _, s in bids),
        "depth_ask_contracts": sum(s for _, s in asks),
    }
    if mid is not None:
        res["bid_band_usd"] = sum(p * s for p, s in bids if p >= mid - band)
        res["ask_band_usd"] = sum(p * s for p, s in asks if p <= mid + band)
    else:
        res["bid_band_usd"] = res["ask_band_usd"] = None
    return res


def priced_in_window(times_ms: list[int], expiry_ms: int, lo_h: float = 6.0, hi_h: float = 1.0) -> bool:
    """True if any observation time falls in ``[expiry - lo_h hours, expiry - hi_h hours]``."""
    a, b = expiry_ms - lo_h * HOUR_MS, expiry_ms - hi_h * HOUR_MS
    return any(a <= t <= b for t in times_ms)
