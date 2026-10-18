"""Pendle fixed-yield maths and market summaries (pure: no I/O, `now` is passed in).

A Pendle PT (principal token) is redeemable 1:1 for the underlying at expiry, so buying it at price `p`
(in units of the accounting asset, i.e. what 1 PT redeems for at expiry, p < 1) locks in a fixed return. Pendle quotes it as an
ANNUALISED, COMPOUNDED rate over a 365-day year:

    apy = (1 / p) ** (365 / days) - 1          p = (1 + apy) ** (-days / 365)

All rates are decimals (0.05 = 5 %). `impliedApy` from the API is the market's own fixed rate for a PT bought
now and held to expiry.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

_YEAR_DAYS = 365.0
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤﻿]")


def pt_price_to_apy(price: float, days: float) -> float | None:
    """Fixed APY (compounded, 365-day year) from a PT price in underlying units. None when undefined."""
    if not (price > 0) or not (days > 0) or not math.isfinite(price) or not math.isfinite(days):
        return None
    try:
        return (1.0 / price) ** (_YEAR_DAYS / days) - 1.0
    except OverflowError:
        return None


def apy_to_pt_price(apy: float, days: float) -> float | None:
    """PT price in underlying units implied by a fixed APY and days to expiry. None when undefined."""
    if not (days > 0) or not math.isfinite(apy) or not math.isfinite(days) or apy <= -1.0:
        return None
    try:
        return (1.0 + apy) ** (-days / _YEAR_DAYS)
    except OverflowError:
        return None


def parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def days_to_expiry(expiry: Any, now: datetime) -> float | None:
    dt = parse_expiry(expiry)
    if dt is None:
        return None
    return (dt - now).total_seconds() / 86400.0


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _text(x: Any, limit: int = 60) -> str | None:
    """Untrusted API text: strip control/invisible characters, cap length."""
    if not isinstance(x, str):
        return None
    return _CTRL.sub("", x).strip()[:limit] or None


def _split_id(x: Any) -> tuple[int | None, str | None]:
    """Pendle ids look like '<chainId>-<0xaddress>'."""
    if not isinstance(x, str) or "-" not in x:
        return None, None
    chain, _, addr = x.partition("-")
    chain_id = int(chain) if chain.isascii() and chain.isdigit() else None
    return chain_id, (addr.lower() if ADDR_RE.match(addr) else None)


def _addr(x: Any) -> str | None:
    return x.lower() if isinstance(x, str) and ADDR_RE.match(x) else None


def summarise_market(m: Any, now: datetime) -> dict[str, Any] | None:
    """One `/core/v1/markets/all` entry -> flat dict with numeric fields; None when it is unusable."""
    if not isinstance(m, dict):
        return None
    det = m.get("details")
    det = det if isinstance(det, dict) else {}
    days = days_to_expiry(m.get("expiry"), now)
    implied = _num(det.get("impliedApy"))
    under = _num(det.get("underlyingApy"))
    chain_id, pt_addr = _split_id(m.get("pt"))
    raw_cats = m.get("categoryIds")
    cats = [c for c in (_text(c, 30) for c in (raw_cats if isinstance(raw_cats, list) else []) if isinstance(c, str)) if c]
    price = apy_to_pt_price(implied, days) if implied is not None and days is not None else None
    return {
        "name": _text(m.get("name")),
        "protocol": _text(m.get("protocol")),
        "chain_id": chain_id,
        "market": _addr(m.get("address")),
        "pt": pt_addr,
        "expiry": _text(m.get("expiry"), 30),
        "days_to_expiry": round(days, 3) if days is not None else None,
        "expired": days is not None and days <= 0,
        "implied_apy": implied,
        "underlying_apy": under,
        "implied_minus_underlying": (implied - under) if implied is not None and under is not None else None,
        "pt_price_implied": price,
        "pt_discount": (1.0 - price) if price is not None else None,
        "liquidity_usd": _num(det.get("liquidity")),
        "tvl_usd": _num(det.get("totalTvl")),
        "trading_volume_usd": _num(det.get("tradingVolume")),
        "categories": cats,
        "stable": "stables" in cats,
        "volatile": bool(m.get("isVolatile")),
    }


def summarise_all(markets: Any, now: datetime) -> tuple[list[dict[str, Any]], int]:
    """Summarise every entry; a malformed entry is skipped (never raises). Returns (rows, skipped)."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    for m in markets if isinstance(markets, list) else []:
        try:
            r = summarise_market(m, now)
        except Exception:
            r = None
        if r is None:
            skipped += 1
        else:
            rows.append(r)
    return rows, skipped


def scan_fixed_yields(
    markets: Any,
    now: datetime,
    *,
    min_days: float = 1.0,
    max_days: float = 1095.0,
    min_liquidity_usd: float = 0.0,
    min_apy: float | None = None,
    stable_only: bool = False,
    chain_ids: list[int] | None = None,
    sort_by: str = "implied_apy",
    limit: int = 30,
) -> dict[str, Any]:
    """Filter and rank Pendle markets by fixed yield. Expired and unparseable markets are always dropped."""
    rows, skipped = summarise_all(markets, now)
    total = len(rows)
    out = []
    for r in rows:
        d = r["days_to_expiry"]
        if r["implied_apy"] is None or d is None or r["expired"] or d < min_days or d > max_days:
            continue
        if (r["liquidity_usd"] or 0.0) < min_liquidity_usd:
            continue
        if min_apy is not None and r["implied_apy"] < min_apy:
            continue
        if stable_only and not r["stable"]:
            continue
        if chain_ids and r["chain_id"] not in chain_ids:
            continue
        out.append(r)
    key = sort_by if sort_by in {"implied_apy", "liquidity_usd", "days_to_expiry", "implied_minus_underlying"} else "implied_apy"
    out.sort(key=lambda r: (r[key] is None, -(r[key] or 0.0) if key != "days_to_expiry" else (r[key] or 0.0)))
    return {"markets_total": total, "markets_skipped": skipped, "markets_matched": len(out), "markets": out[:limit]}
