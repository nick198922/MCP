"""Inventory helpers for pre-market perp contracts (IDEA-20261028-premarket-perp-conversion-fade, step 0).

Pure functions, no I/O; times are epoch milliseconds (UTC), volumes in quote currency (USDT ~ USD).

Conversion time: a Binance USD-M pre-market perp has no index price (there is no spot market to index); the
index-price klines of the symbol start when the contract is converted to a standard perpetual. The first 1m
index-price kline is therefore a data-based estimate of the conversion minute; the time stated in the conversion
announcement (untrusted text, timestamp only) is a cross-check when it exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class Conversion:
    t_ms: int | None  # estimated conversion time (first index print), None if not reconstructable
    source: str  # "index_1m" | "index_1d" | "announcement" | "none"
    ann_gap_h: float | None  # |data estimate - announcement time| in hours, None without both


def conversion_time(index_first_1m: int | None, index_first_1d: int | None, ann_ms: int | None) -> Conversion:
    """Conversion estimate: first 1m index kline, else first 1d index kline (day precision), else the announcement."""
    if index_first_1m is not None:
        t, src = index_first_1m, "index_1m"
    elif index_first_1d is not None:
        t, src = index_first_1d, "index_1d"
    elif ann_ms is not None:
        return Conversion(ann_ms, "announcement", None)
    else:
        return Conversion(None, "none", None)
    gap = abs(t - ann_ms) / HOUR_MS if ann_ms is not None else None
    return Conversion(t, src, gap)


def hour_precise(c: Conversion, tol_h: float = 1.0) -> bool:
    """G3 per event: the conversion hour comes from 1m data and, when an announced time exists, agrees within ``tol_h``."""
    if c.source != "index_1m":
        return False
    return c.ann_gap_h is None or c.ann_gap_h <= tol_h


def hour_confirmed(c: Conversion, spot_first_ms: int | None, tol_h: float = 1.0) -> bool:
    """Strict G3: the 1m index start is confirmed by a second source within ``tol_h``: the announced conversion time
    when there is one, otherwise the first spot candle of the same venue (spot listing = end of the pre-market)."""
    if c.source != "index_1m" or c.t_ms is None:
        return False
    if c.ann_gap_h is not None:
        return c.ann_gap_h <= tol_h
    return spot_first_ms is not None and abs(spot_first_ms - c.t_ms) / HOUR_MS <= tol_h


def premarket_volumes(daily: list[tuple[int, float]], t_first: int, t_conv: int) -> list[float]:
    """Quote volumes of the full UTC days of the pre-market: days that start at/after ``t_first`` (the partial listing
    day is dropped) and end at/before ``t_conv`` (the conversion day is dropped). Rows after ``t_conv`` are never read."""
    return [v for t, v in daily if t >= t_first and t + DAY_MS <= t_conv]


def median_or_none(xs: list[float]) -> float | None:
    return median(xs) if xs else None


def days_after(t_conv: int, last_bar_open_ms: int | None, now_ms: int) -> float:
    """Days of data after conversion: up to the open of the last daily bar + 1 day, capped by ``now_ms``."""
    if last_bar_open_ms is None or last_bar_open_ms + DAY_MS <= t_conv:
        return 0.0
    return max(0.0, (min(last_bar_open_ms + DAY_MS, now_ms) - t_conv) / DAY_MS)


def mde(sd: float, n: int, k: float = 2.8) -> float:
    """Minimum detectable mean effect (two-sided 5 %, 80 % power ~ k = 2.8) for ``n`` independent events."""
    return k * sd / math.sqrt(n) if n > 0 else math.inf
