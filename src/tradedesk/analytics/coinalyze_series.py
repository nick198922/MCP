"""Pure helpers for Coinalyze OI, funding and long/short histories (Binance symbols, suffix '.A'). No I/O.

* `/open-interest-history` and `/funding-rate-history` rows: `{"t": unix_s, "o", "h", "l", "c"}`, `t` = interval
  START. OI is in base-asset coins, or USD when the request set `convert_to_usd=true`.
* Funding is in PERCENT per funding interval (0.01 = 0.01 % = 1e-4); `FUNDING_PCT_TO_DECIMAL` turns it into our
  decimal fraction. Checked 2026-10-18 against Binance USDT-M payments (docs/reviews/2026-10-18-doubletop-parity-1b.md):
  the daily `c` is the last settlement of the UTC day, `h` the largest settlement in the bar.
* `/long-short-ratio-history` rows: `{"t", "r": long/short, "l": % of accounts long, "s": % short}` (l + s = 100;
  matches Binance `globalLongShortAccountRatio`, accounts, not notional). We return l and s as fractions in [0, 1].
"""

from __future__ import annotations

import math
from typing import Any

from .liquidations_agg import INTERVALS, ROW_ERRORS, parse_ts

FUNDING_PCT_TO_DECIMAL = 0.01
LS_PCT_TO_FRACTION = 0.01


def _num(x: Any) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("non-finite")
    return v


def _stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    mean = sum(values) / n
    stdev = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n >= 2 else None
    latest = values[-1]
    return {
        "latest": latest,
        "mean": mean,
        "stdev": stdev,
        "min": min(values),
        "max": max(values),
        "zscore_latest": ((latest - mean) / stdev) if stdev else None,
    }


def _gaps(rows: list[dict[str, Any]], step: int) -> int:
    return sum(1 for a, b in zip(rows, rows[1:], strict=False) if b["ts"] - a["ts"] > step)


def _dedupe_sorted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ts = {r["ts"]: r for r in rows}  # a repeated timestamp keeps the last row
    return [by_ts[t] for t in sorted(by_ts)]


def ohlc_series(history: list[dict[str, Any]], interval: str, *, scale: float = 1.0, with_pct_change: bool = True) -> dict[str, Any] | None:
    """OHLC rows -> {rows, stats}; values multiplied by `scale`; malformed rows (non-finite values, timestamps that
    are not numbers or fall outside 2009..2100) dropped; None if nothing is left.

    Stats are over the closes: latest, mean, stdev (sample), min, max, z-score of the latest close, change =
    last close - first close, change_pct = change / |first close| (None when disabled or the first close is 0).
    """
    step = INTERVALS[interval]
    rows: list[dict[str, Any]] = []
    for h in history or []:
        try:
            t, iso = parse_ts(h["t"])
            o, hi, lo, c = (_num(h[k]) * scale for k in ("o", "h", "l", "c"))
        except ROW_ERRORS:
            continue
        rows.append({"t": iso, "ts": t, "open": o, "high": hi, "low": lo, "close": c})
    rows = _dedupe_sorted(rows)
    if not rows:
        return None
    closes = [r["close"] for r in rows]
    stats = _stats(closes)
    change = closes[-1] - closes[0]
    stats.update(
        {
            "first": closes[0],
            "change": change,
            "change_pct": (change / abs(closes[0])) if with_pct_change and closes[0] != 0 else None,
            "intervals": len(rows),
            "gaps": _gaps(rows, step),
        }
    )
    return {"rows": rows, "stats": stats}


def long_short_series(history: list[dict[str, Any]], interval: str) -> dict[str, Any] | None:
    """Long/short rows -> {rows, stats}: ratio = longs / shorts (accounts), long_share and short_share in [0, 1].

    Rows with negative values, shares outside [0, 100] % or a malformed timestamp (see ohlc_series) are dropped. Stats: over the ratio (latest, mean,
    stdev, min, max, z-score of the latest) plus the latest and mean long_share.
    """
    step = INTERVALS[interval]
    rows: list[dict[str, Any]] = []
    for h in history or []:
        try:
            t, iso = parse_ts(h["t"])
            r = _num(h["r"])
            lg = _num(h["l"]) * LS_PCT_TO_FRACTION
            sh = _num(h["s"]) * LS_PCT_TO_FRACTION
        except ROW_ERRORS:
            continue
        if r < 0 or not (0 <= lg <= 1) or not (0 <= sh <= 1):
            continue
        rows.append({"t": iso, "ts": t, "ratio": r, "long_share": lg, "short_share": sh})
    rows = _dedupe_sorted(rows)
    if not rows:
        return None
    stats = _stats([r["ratio"] for r in rows])
    shares = [r["long_share"] for r in rows]
    stats.update(
        {
            "long_share_latest": shares[-1],
            "long_share_mean": sum(shares) / len(shares),
            "intervals": len(rows),
            "gaps": _gaps(rows, step),
        }
    )
    return {"rows": rows, "stats": stats}
