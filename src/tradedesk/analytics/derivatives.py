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


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "stdev": None, "min": None, "max": None}
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return {"mean": mean, "stdev": None, "min": min(values), "max": max(values)}
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return {"mean": mean, "stdev": math.sqrt(var), "min": min(values), "max": max(values)}


def _percentile_rank(values: list[float], x: float) -> float | None:
    """Fraction of values strictly below x plus half the ties, scaled to [0, 1]."""
    if not values:
        return None
    below = sum(1 for v in values if v < x)
    ties = sum(1 for v in values if v == x)
    return (below + 0.5 * ties) / len(values)


def summarise_funding_history(
    entries: list[dict[str, Any]],
    *,
    interval_hours: float,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Stats over a funding-rate time series.

    `entries` are dicts with at least ``rate`` (per-interval, decimal) and ``ts_ms``.
    Returns mean/stdev/min/max over all, 7d and 30d windows; latest value with its z-score
    (vs the full window) and percentile rank; annualised carry (mean rate scaled to a year);
    and polarity flips (count of sign changes in the series).
    """
    rows = sorted(
        [{"ts_ms": int(e["ts_ms"]), "rate": float(e["rate"])} for e in entries if e.get("rate") is not None],
        key=lambda r: r["ts_ms"],
    )
    if not rows:
        return {"count": 0}

    rates = [r["rate"] for r in rows]
    stats_all = _stats(rates)
    latest = rows[-1]
    anchor_ms = now_ms if now_ms is not None else latest["ts_ms"]

    def window(days: float) -> list[float]:
        cutoff = anchor_ms - int(days * 86_400_000)
        return [r["rate"] for r in rows if r["ts_ms"] >= cutoff]

    flips = sum(1 for a, b in zip(rates, rates[1:], strict=False) if a == 0 or (a > 0) != (b > 0) and b != 0)
    scale = 8760.0 / interval_hours
    mean_rate = stats_all["mean"]
    carry_annualized = mean_rate * scale if mean_rate is not None else None

    z = None
    sd = stats_all["stdev"]
    if sd is not None and sd > 0:
        z = (latest["rate"] - stats_all["mean"]) / sd  # type: ignore[operator]

    return {
        "count": len(rows),
        "first_ts_ms": rows[0]["ts_ms"],
        "last_ts_ms": latest["ts_ms"],
        "latest_rate": latest["rate"],
        "latest_annualized": latest["rate"] * scale,
        "mean_rate": mean_rate,
        "stdev_rate": sd,
        "min_rate": stats_all["min"],
        "max_rate": stats_all["max"],
        "mean_7d": _stats(window(7))["mean"],
        "mean_30d": _stats(window(30))["mean"],
        "carry_annualized": carry_annualized,
        "z_score_latest": z,
        "percentile_latest": _percentile_rank(rates, latest["rate"]),
        "polarity_flips": flips,
        "interval_hours": interval_hours,
    }
