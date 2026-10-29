"""CoinGecko trending-list snapshot rows (pure: no I/O, time passed in).

Docs: docs/ideas/IDEA-20261028-coingecko-trending-attention-fade-forward.md, step 0. The free ``/search/trending``
endpoint returns only the current list (no history), so the history exists only as far as we snapshot it: one snapshot
per UTC hour, one row per listed coin with its 1-based position in the list (``rank``).

The response is parsed defensively. Documented item shape (``coins[i].item``): ``id`` (API id), ``coin_id`` (numeric),
``name``, ``symbol``, ``market_cap_rank``, ``price_btc``, ``score`` (0-based position) and ``data`` with ``price`` (USD),
``price_btc``, ``price_change_percentage_24h`` (``{"usd": percent, ...}``), ``market_cap`` and ``total_volume`` (USD as
display strings such as ``"$206,941,453"``). Any field may be missing, null or of another type: it then becomes NULL;
an item without a usable ``id`` is skipped (its rank stays empty). Names and symbols are untrusted display text:
control and format characters removed, length capped. Numbers must be finite; prices, caps and volumes non-negative.

Units: ``price_usd`` USD; ``price_btc`` BTC; ``change_24h`` decimal fraction (CoinGecko's USD percent / 100, so
-0.0407 = -4.07 %); ``market_cap_usd`` and ``volume_usd`` USD.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .dex import clean_text
from .hl_oi_cap import hour_ts

__all__ = ["MAX_ROWS", "hour_ts", "snapshot_rows"]

MAX_ROWS = 50  # the free list has 15 coins; a much longer list is a changed endpoint, not more signal
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")  # CoinGecko API ids: lowercase slugs such as "usd-coin"
_MONEY = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)$")


def _num(x: Any, *, nonneg: bool = True) -> float | None:
    """A finite float from a number or a plain numeric / ``"$1,234.5"`` string; anything else (HTML, bool, NaN) None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, int | float):
        v = float(x)
    elif isinstance(x, str):
        s = x.strip()
        neg = s.startswith("-")
        m = _MONEY.match(s[1:] if neg else s)
        if not m:
            return None
        v = float(m.group(1).replace(",", "")) * (-1.0 if neg else 1.0)
    else:
        return None
    if not math.isfinite(v) or (nonneg and v < 0):
        return None
    return v


def _int(x: Any) -> int | None:
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x) or x != int(x):
        return None
    return int(x)


def snapshot_rows(payload: Any) -> list[dict[str, Any]]:
    """Rows of one snapshot from the ``/search/trending`` object (``coins`` in list order).

    ``rank`` is the 1-based position in ``coins`` (the order CoinGecko shows); ``score`` is kept as given. A repeated
    ``id`` keeps its first (higher) position only. At most ``MAX_ROWS`` positions are read."""
    coins = payload.get("coins") if isinstance(payload, dict) else None
    if not isinstance(coins, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pos, entry in enumerate(coins[:MAX_ROWS], 1):
        item = entry.get("item") if isinstance(entry, dict) else None
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not _ID.match(cid) or cid in seen:
            continue
        seen.add(cid)
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        change = data.get("price_change_percentage_24h")
        change_usd = _num(change.get("usd"), nonneg=False) if isinstance(change, dict) else None
        price_btc = _num(item.get("price_btc"))
        rows.append(
            {
                "rank": pos,
                "coin_id": cid,
                "coin_num": _int(item.get("coin_id")),
                "symbol": clean_text(item.get("symbol"), 40) or None,
                "name": clean_text(item.get("name"), 100) or None,
                "market_cap_rank": _int(item.get("market_cap_rank")),
                "score": _int(item.get("score")),
                "price_usd": _num(data.get("price")),
                "price_btc": price_btc if price_btc is not None else _num(data.get("price_btc")),
                "change_24h": change_usd / 100.0 if change_usd is not None else None,
                "market_cap_usd": _num(data.get("market_cap")),
                "volume_usd": _num(data.get("total_volume")),
            }
        )
    return rows
