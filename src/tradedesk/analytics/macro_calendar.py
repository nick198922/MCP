"""Pure macro-calendar builder: ForexFactory weekly rows + the bundled FOMC/CPI/NFP seed.

No I/O; `now` is a parameter. Every text field in a feed row is untrusted third-party data, so it is
stripped of control characters and truncated before it leaves this module.

Rules
- Keep only impact == High. USD events are all kept (listed kinds get a specific `kind`, the rest are
  `usd_other`); non-USD events are kept only when they are a central-bank decision of ECB (EUR),
  BoJ (JPY) or BoE (GBP).
- Times are normalised to UTC (feed dates carry a UTC offset) and also given in Europe/Kyiv.
- The seed fills dates the feed does not cover. Inside the span the loaded feed rows cover (whole UTC
  days from the first to the last row) the feed is authoritative and seed events are dropped.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHECK_WINDOW_HOURS = 48
GRACE_HOURS = 2  # events released up to this long ago are still listed (the market is digesting them)

_USD_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bcore pce\b", re.I), "core_pce"),
    (re.compile(r"\bpce\b", re.I), "pce"),
    (re.compile(r"\bcpi\b", re.I), "cpi"),
    (re.compile(r"\bppi\b", re.I), "ppi"),
    (re.compile(r"\badp\b", re.I), "adp"),
    (re.compile(r"non-?farm", re.I), "nfp"),
    (re.compile(r"\bgdp\b", re.I), "gdp"),
    (re.compile(r"retail sales", re.I), "retail_sales"),
    (re.compile(r"\bism\b", re.I), "ism"),
    (re.compile(r"(unemployment|jobless) claims", re.I), "jobless_claims"),
    (re.compile(r"fomc.*minutes", re.I), "fomc_minutes"),
    (re.compile(r"(fed chair|fomc member|fed governor).*speaks|testimony|monetary policy report", re.I), "fed_speech"),
    (re.compile(r"fomc (statement|press conference|economic projections)|federal funds rate", re.I), "fomc"),
]

_CB_RULES: dict[str, tuple[re.Pattern[str], str]] = {
    "EUR": (
        re.compile(r"main refinancing rate|deposit facility rate|ecb press conference|monetary policy statement", re.I),
        "ecb_decision",
    ),
    "JPY": (re.compile(r"boj policy rate|boj press conference|monetary policy statement", re.I), "boj_decision"),
    "GBP": (re.compile(r"official bank rate|mpc.*votes|monetary policy (summary|report)", re.I), "boe_decision"),
}


def _kyiv_tz() -> tzinfo:
    try:
        return ZoneInfo("Europe/Kyiv")
    except ZoneInfoNotFoundError:  # older tzdata
        return ZoneInfo("Europe/Kiev")


def clean_text(value: Any, limit: int) -> str:
    """Untrusted text: drop control characters, collapse whitespace, truncate."""
    s = "" if value is None else str(value)
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    return " ".join(s.split())[:limit]


def classify(title: str, country: str) -> str | None:
    """Event kind for a high-impact row, or None when the row is out of scope."""
    country = country.upper()
    if country == "USD":
        for pat, kind in _USD_RULES:
            if pat.search(title):
                return kind
        return "usd_other"
    rule = _CB_RULES.get(country)
    if rule and rule[0].search(title):
        return rule[1]
    return None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # an offset is part of the documented format; refuse to guess
        return None
    return dt.astimezone(UTC)


def _event(ts: datetime, now: datetime, kyiv: tzinfo, **fields: Any) -> dict[str, Any]:
    return {
        "ts_utc": ts.isoformat(),
        "ts_kyiv": ts.astimezone(kyiv).isoformat(),
        "hours_ahead": round((ts - now).total_seconds() / 3600, 2),
        **fields,
    }


def build_macro_calendar(
    feed_rows: list[dict[str, Any]],
    seed_events: list[dict[str, Any]],
    now: datetime,
    horizon_days: int,
) -> dict[str, Any]:
    """Merge feed rows (possibly empty when the feed is down) with the seed into one sorted list."""
    kyiv = _kyiv_tz()
    end = now + timedelta(days=horizon_days)
    start = now - timedelta(hours=GRACE_HOURS)
    warnings: list[str] = []

    parsed: list[tuple[datetime, dict[str, Any]]] = []
    skipped = 0
    for row in feed_rows:
        ts = _parse_dt(row.get("date"))
        if ts is None:
            skipped += 1
            continue
        parsed.append((ts, row))

    cover_from = cover_to = None
    if parsed:
        stamps = [t for t, _ in parsed]
        cover_from = min(stamps).replace(hour=0, minute=0, second=0, microsecond=0)
        cover_to = max(stamps).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    events: list[dict[str, Any]] = []
    for ts, row in parsed:
        if str(row.get("impact", "")).strip().lower() != "high" or not (start <= ts <= end):
            continue
        country = clean_text(row.get("country"), 8).upper()
        title = clean_text(row.get("title"), 120)
        kind = classify(title, country)
        if kind is None:
            continue
        events.append(
            _event(
                ts,
                now,
                kyiv,
                kind=kind,
                title=title,
                country=country,
                impact="high",
                forecast=clean_text(row.get("forecast"), 40) or None,
                previous=clean_text(row.get("previous"), 40) or None,
                source="forexfactory",
            )
        )

    seed_added = seed_superseded = 0
    last_seed: datetime | None = None
    for s in seed_events:
        try:
            ts = datetime.fromtimestamp(int(s["ts"]), tz=UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        last_seed = ts if last_seed is None or ts > last_seed else last_seed
        if str(s.get("impact", "high")).lower() != "high" or not (start <= ts <= end):
            continue
        if cover_from is not None and cover_to is not None and cover_from <= ts < cover_to:
            seed_superseded += 1
            continue
        events.append(
            _event(
                ts,
                now,
                kyiv,
                kind=clean_text(s.get("kind"), 24).lower() or "other",
                title=clean_text(s.get("label"), 120),
                country="USD",
                impact="high",
                forecast=None,
                previous=None,
                source="seed",
            )
        )
        seed_added += 1

    events.sort(key=lambda e: (e["ts_utc"], e["title"]))

    if seed_added:
        warnings.append("seed events (FOMC/CPI/NFP) have approximate times and no forecast; confirm against the official calendars")
    if last_seed is not None and end > last_seed:
        warnings.append(f"the seed ends {last_seed.date().isoformat()}; later dates are only listed while the feed covers them")
    if skipped:
        warnings.append(f"{skipped} feed row(s) skipped: unparsable or timezone-less date")

    # recently released events (hours_ahead < 0, inside the grace window) count for the flag; next_event
    # prefers the first upcoming event and falls back to the latest recent one
    within = [e for e in events if e["hours_ahead"] <= CHECK_WINDOW_HOURS]
    upcoming = [e for e in events if e["hours_ahead"] >= 0]
    return {
        "events": events,
        "high_impact_within_48h": bool(within),
        "next_event": upcoming[0] if upcoming else (events[-1] if events else None),
        "all_feed_rows_unparsable": bool(skipped and not parsed),
        "feed": {
            "rows": len(parsed),
            "covers_from": cover_from.isoformat() if cover_from else None,
            "covers_to": cover_to.isoformat() if cover_to else None,
        },
        "seed_added": seed_added,
        "seed_superseded": seed_superseded,
        "warnings": warnings,
    }
