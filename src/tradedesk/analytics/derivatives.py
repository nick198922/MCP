"""Futures / perpetual maths: basis, funding annualisation."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any


def basis(mark: float, index: float, expiry: datetime, now: datetime) -> dict[str, Any]:
    days = max((expiry - now).total_seconds() / 86400.0, 0.0)
    pct = mark / index - 1.0
    out: dict[str, Any] = {
        "days_to_expiry": round(days, 3),
        "basis_abs": mark - index,
        "basis_pct": pct,
        "annualized_simple": None,
        "annualized_log": None,
    }
    if days > 0.05:  # avoid blowing up on contracts expiring within ~1 hour
        out["annualized_simple"] = pct * 365.0 / days
        out["annualized_log"] = math.log(mark / index) * 365.0 / days
    return out


_INTERVAL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hmd])\s*$", re.I)


def parse_interval_hours(value: Any, default: float = 8.0) -> float:
    """'8h' -> 8.0, '1h' -> 1.0, '30m' -> 0.5, 4 -> 4.0. Falls back to `default`."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str):
        m = _INTERVAL.match(value)
        if m:
            n, unit = float(m.group(1)), m.group(2).lower()
            return n * {"h": 1.0, "m": 1 / 60.0, "d": 24.0}[unit]
    return default


def annualize_funding(rate: float, interval_hours: float) -> float:
    """Simple (non-compounded) annualised funding rate as a fraction (0.10 = 10 %)."""
    return rate * (8760.0 / interval_hours)
