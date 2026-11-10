"""Hyperliquid open-interest-cap snapshot rows (pure: no I/O, time passed in).

Docs: docs/ideas/IDEA-20261028-hl-oi-cap-state-forward.md, step 0. Hyperliquid caps the total open interest of each perp;
at the cap, orders that raise OI are rejected (closing is allowed). ``perpsAtOpenInterestCap`` returns only the current
list of capped tickers, so the history of the state exists only as far as we snapshot it. One row per perp and UTC hour:
the boolean state plus the context needed later (OI in USD, funding, mark, oracle, 24 h notional volume). The cap itself
is not published as a number, so OI relative to the cap is not computed.

Units: ``funding`` is Hyperliquid's hourly rate as a decimal fraction (0.0000125 = 0.00125 % per hour), as the venue
reports it; ``oi_usd`` = open interest in coins x mark price, USD; ``vol24h`` = ``dayNtlVlm``, USD notional.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

HOUR_S = 3600
MAX_VALUE = 1e15  # USD, price or hourly rate: far above any perp's OI or 24 h volume (BTC OI ~3.5e9 USD); 1e308 is garbage


def hour_ts(now: datetime) -> int:
    """Unix seconds of the start of the UTC hour containing ``now`` (the snapshot key: one snapshot per hour)."""
    t = int(now.timestamp())
    return t - t % HOUR_S


def _value(v: Any) -> float | None:
    """A finite number up to +-MAX_VALUE, else None: the venue's "NaN" / "Infinity" strings parse to non-finite floats,
    and a 1e308 open interest or mark gives an infinite ``oi_usd``; such a field is stored as NULL, the row is kept."""
    if not isinstance(v, int | float) or isinstance(v, bool):
        return None
    return float(v) if math.isfinite(v) and abs(v) <= MAX_VALUE else None


def snapshot_rows(capped: Iterable[str], contexts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows of one snapshot from the capped tickers and the parsed ``metaAndAssetCtxs`` rows
    (``providers.hyperliquid.parse_asset_contexts``, ideally with ``include_delisted=True``).

    Every listed perp gets a row with ``at_cap`` 1 or 0 (the uncapped ones are the controls of H2/H3). Delisted perps
    (``delisted`` true) get no row even when capped: live 2026-10-28 the endpoint listed 7 delisted perps with OI 0
    (CANTO, FTM, JELLY, ...), a permanent "at cap" that is not a market state. A capped ticker absent from the contexts
    keeps its state with NULL market fields rather than being dropped. Sorted by coin."""
    cap = {str(c) for c in capped if c}
    rows: dict[str, dict[str, Any]] = {}
    delisted: set[str] = set()
    for c in contexts:
        coin = c.get("coin") if isinstance(c, dict) else None
        if not coin or coin in rows or coin in delisted:
            continue
        if c.get("delisted"):
            delisted.add(coin)
            continue
        rows[coin] = {
            "coin": coin,
            "at_cap": int(coin in cap),
            "oi_usd": _value(c.get("open_interest_usd")),
            "funding": _value(c.get("funding_1h")),
            "mark": _value(c.get("mark_px")),
            "oracle": _value(c.get("oracle_px")),
            "vol24h": _value(c.get("day_volume_usd")),
        }
    for coin in cap - rows.keys() - delisted:
        rows[coin] = {"coin": coin, "at_cap": 1, "oi_usd": None, "funding": None, "mark": None, "oracle": None, "vol24h": None}
    return [rows[k] for k in sorted(rows)]
