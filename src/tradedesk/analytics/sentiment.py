"""Pure analytics for the Crypto Fear & Greed Index."""

from __future__ import annotations

from typing import Any

ZONES = (
    ("extreme_fear", 0, 25),
    ("fear", 25, 45),
    ("neutral", 45, 55),
    ("greed", 55, 75),
    ("extreme_greed", 75, 101),
)


def _zone(value: int) -> str:
    for name, lo, hi in ZONES:
        if lo <= value < hi:
            return name
    return "neutral"


def _norm(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse string fields and sort oldest-first."""
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            v = int(r["value"])
            ts = int(r["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= v <= 100:
            continue
        out.append({"value": v, "ts": ts, "classification": str(r.get("value_classification") or _zone(v)).lower().replace(" ", "_")})
    out.sort(key=lambda r: r["ts"])
    return out


def summarise_fear_greed(rows: list[dict[str, Any]], *, now_ts: int | None = None) -> dict[str, Any]:
    """Summarise a raw Fear & Greed series.

    ``rows`` are items as returned by alternative.me (``value`` and ``timestamp`` are strings).
    Returns the latest reading, its zone, mean over 7d and 30d, min/max over the whole series,
    change vs 7d/30d ago (points), and the share of days spent in each zone.
    """
    rows = _norm(rows)
    if not rows:
        return {"count": 0}
    latest = rows[-1]
    anchor = now_ts if now_ts is not None else latest["ts"]

    def window(days: float) -> list[int]:
        cutoff = anchor - int(days * 86_400)
        return [r["value"] for r in rows if r["ts"] >= cutoff]

    all_values = [r["value"] for r in rows]
    w7, w30 = window(7), window(30)

    def mean(xs: list[int]) -> float | None:
        return sum(xs) / len(xs) if xs else None

    def delta(days: float) -> int | None:
        cutoff = anchor - int(days * 86_400)
        past = [r["value"] for r in rows if r["ts"] <= cutoff]
        return latest["value"] - past[-1] if past else None

    zone_counts: dict[str, int] = {name: 0 for name, _, _ in ZONES}
    for v in all_values:
        zone_counts[_zone(v)] += 1
    zone_share = {k: round(c / len(all_values), 4) for k, c in zone_counts.items()}

    return {
        "count": len(rows),
        "latest": {
            "value": latest["value"],
            "classification": latest["classification"],
            "ts": latest["ts"],
            "zone": _zone(latest["value"]),
        },
        "mean_7d": mean(w7),
        "mean_30d": mean(w30),
        "min": min(all_values),
        "max": max(all_values),
        "change_7d": delta(7),
        "change_30d": delta(30),
        "zone_share": zone_share,
        "series": [{"ts": r["ts"], "value": r["value"], "zone": _zone(r["value"])} for r in rows],
    }
