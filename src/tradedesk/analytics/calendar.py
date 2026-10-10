"""Pure calendar helpers: Deribit expiry classification, funding payout schedule, macro filter."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from typing import Any


def _last_friday(year: int, month: int) -> int:
    _, last_day = calendar.monthrange(year, month)
    for d in range(last_day, last_day - 7, -1):
        if datetime(year, month, d).weekday() == 4:
            return d
    return last_day


def classify_expiry(exp: datetime, now: datetime) -> str:
    """Classify a Deribit-style expiry (always 08:00 UTC) relative to `now`.

    - quarterly: last Friday of {Mar, Jun, Sep, Dec}
    - monthly:   last Friday of any other month
    - weekly:    other Fridays at 08:00 UTC
    - daily:     any non-Friday or non-08:00 expiry within ~2 days
    """
    if exp.hour == 8 and exp.weekday() == 4:
        if exp.day == _last_friday(exp.year, exp.month):
            return "quarterly" if exp.month in (3, 6, 9, 12) else "monthly"
        return "weekly"
    return "daily"


def funding_schedule(now: datetime, hours_ahead: int) -> list[dict[str, Any]]:
    """Upcoming funding payout timestamps across venues.

    Binance USDⓈ-M / Bybit linear / OKX swap: every 8h at 00/08/16 UTC.
    Hyperliquid: every hour on the hour.
    Returns one entry per (venue, ts) sorted ascending, limited to `hours_ahead`.
    """
    out: list[dict[str, Any]] = []
    hourly = now.replace(minute=0, second=0, microsecond=0)
    base_8h = hourly.replace(hour=hourly.hour // 8 * 8)
    end = now + timedelta(hours=hours_ahead)

    for step in range(0, hours_ahead + 8, 8):
        ts = base_8h + timedelta(hours=step)
        if ts <= now or ts > end:
            continue
        for venue in ("binanceusdm", "bybit", "okx"):
            out.append({"venue": venue, "ts": ts.isoformat(), "interval_hours": 8})

    for h in range(1, min(hours_ahead, 24) + 1):
        ts = hourly + timedelta(hours=h)
        if ts > end:
            continue
        out.append({"venue": "hyperliquid", "ts": ts.isoformat(), "interval_hours": 1})

    out.sort(key=lambda r: (r["ts"], r["venue"]))
    return out


def filter_macro(events: list[dict[str, Any]], now: datetime, horizon_days: int) -> list[dict[str, Any]]:
    end = now + timedelta(days=horizon_days)
    out: list[dict[str, Any]] = []
    for e in events:
        try:
            d, t = e["date"], e.get("time_utc") or "00:00"
            dt = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except (KeyError, ValueError):
            continue
        if dt < now or dt > end:
            continue
        out.append({**e, "ts": dt.isoformat(), "days_ahead": round((dt - now).total_seconds() / 86400, 2)})
    out.sort(key=lambda r: r["ts"])
    return out
