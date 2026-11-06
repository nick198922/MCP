from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from mcp.types import ToolAnnotations

from ..analytics.instruments import expiry_token, parse_user_expiry
from ..analytics.options import json_num as json_num  # re-exported: one rule for "a JSON number" in tools and analytics
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

# All Phase 0-1 tools only read public market data.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

# What a query on the local SQLite store raises when the file is locked, unreadable, on a full disk or gone.
STORE_ERRORS = (sqlite3.Error, OSError)

T = TypeVar("T")


async def store_ready(b: Builder, rt: Runtime, feature: str) -> bool:
    """True when the local store is usable. Otherwise records `missing: store` and returns False (invariant 1):
    reason `config` when it is switched off (empty db_path), `backend_error` when `Store.init()` raised."""
    if rt.store is None:
        b.miss("store", "tradedesk", "config", f"db_path is empty; set TRADEDESK_DB_PATH to enable {feature}")
        return False
    try:
        await rt.store.init()
    except Exception as e:  # noqa: BLE001 - an unusable store is reported in `missing`, never raised
        b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
        return False
    return True


def store_failed(b: Builder, e: BaseException) -> dict[str, Any]:
    """Envelope for a store query that raised one of STORE_ERRORS: `missing: store` (backend_error), ok=false."""
    b.miss("store", "tradedesk", "backend_error", f"{type(e).__name__}: {e}")
    return b.build(None, ok=False)


def parsed_rows(rows: Any, parse: Callable[[Any], T]) -> list[T]:
    """`parse` applied to every row of a venue answer. A row it cannot parse (a field that is null, a string or a list
    where a number belongs, or an infinity: TypeError / ValueError / KeyError / IndexError / OverflowError) is dropped,
    as rows with an absent field already are, so a wrong-type field never escapes the tool (invariant 1). Well-formed
    rows parse exactly as before."""
    out: list[T] = []
    for row in rows or []:
        try:
            out.append(parse(row))
        except (TypeError, ValueError, KeyError, IndexError, OverflowError):
            continue
    return out


def r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def find_expiry(by_expiry: dict[datetime, Any], text: str | None) -> tuple[datetime | None, str | None]:
    """Resolve a user-supplied expiry ('26MAR27' or '2027-03-26'). Returns (expiry, error_text)."""
    available = ", ".join(expiry_token(e) for e in by_expiry)
    if not text:
        return None, f"expiry is required. Available: {available}"
    day = parse_user_expiry(text)
    if day is None:
        return None, f"cannot parse expiry '{text}' (use e.g. 26MAR27 or 2027-03-26). Available: {available}"
    for e in by_expiry:
        if e.date() == day:
            return e, None
    return None, f"no listed expiry on {day.isoformat()}. Available: {available}"


def timeframe_seconds(tf: str) -> float:
    """Seconds in one ccxt timeframe: '1m', '4h', '1d', '1w' and '1M' (a month, taken as 30 days).

    Raises ValueError for anything else; tools catch it and report `missing: timeframe` instead of raising.
    """
    unit = tf[-1:]
    # ccxt spells minutes 'm' and months 'M'; decide the month before lower-casing.
    mult = 30 * 86400 if unit == "M" else {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit.lower())
    if mult is None or not tf[:-1].isdigit():
        raise ValueError(f"bad timeframe '{tf}'")
    return int(tf[:-1]) * mult
