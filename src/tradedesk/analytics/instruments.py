"""Deribit instrument-name parsing. Deribit options/futures expire at 08:00 UTC."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
_MONTH_NAMES = {v: k for k, v in _MONTHS.items()}
_EXP = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")
_EXPIRY_HOUR_UTC = 8


@dataclass(frozen=True)
class OptionName:
    raw: str
    product: str  # BTC, ETH, BTC_USDC ...
    expiry: datetime
    strike: float
    is_call: bool


def parse_expiry_token(token: str) -> datetime | None:
    m = _EXP.match(token.upper())
    if not m:
        return None
    day, mon, yy = int(m.group(1)), _MONTHS.get(m.group(2)), int(m.group(3))
    if mon is None:
        return None
    try:
        return datetime(2000 + yy, mon, day, _EXPIRY_HOUR_UTC, tzinfo=UTC)
    except ValueError:
        return None


def expiry_token(dt: datetime) -> str:
    """Deribit-style token without day padding, e.g. 26MAR27."""
    return f"{dt.day}{_MONTH_NAMES[dt.month]}{dt.year % 100:02d}"


def parse_option_name(name: str) -> OptionName | None:
    parts = name.split("-")
    if len(parts) != 4 or parts[3] not in ("C", "P"):
        return None
    expiry = parse_expiry_token(parts[1])
    if expiry is None:
        return None
    try:
        strike = float(parts[2].replace("d", "."))
    except ValueError:
        return None
    return OptionName(name, parts[0], expiry, strike, parts[3] == "C")


def parse_future_name(name: str) -> tuple[str, datetime | None] | None:
    """('BTC', expiry) for dated futures, ('BTC', None) for the perpetual."""
    parts = name.split("-")
    if len(parts) != 2:
        return None
    if parts[1] == "PERPETUAL":
        return parts[0], None
    expiry = parse_expiry_token(parts[1])
    return (parts[0], expiry) if expiry else None


def parse_user_expiry(text: str) -> date | None:
    """Accepts '26MAR27', '26mar27', '2027-03-26'."""
    text = text.strip()
    dt = parse_expiry_token(text)
    if dt:
        return dt.date()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
