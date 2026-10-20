from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.types import ToolAnnotations

from ..analytics.instruments import expiry_token, parse_user_expiry

# All Phase 0-1 tools only read public market data.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


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
