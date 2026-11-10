"""Funding over a trade's holding period, in R of the trade (accounting, not a signal).

The setup passports (``setup_passport``) and the demo-vs-backtest comparison measure R without funding, while the
demo's R is Bybit's closed P&L, which includes it. This module turns a venue's funding settlements into the funding
leg of one trade, in units of its risk.

Conventions (decimals, as everywhere): a settlement rate ``r`` is the fraction of notional a long pays a short at that
settlement (0.0001 = 0.01 %); a negative rate is paid by shorts. A settlement counts when its time (floored to the
minute: venue stamps carry a few ms of jitter) is strictly inside the holding interval (entry_ms, exit_ms): a
position opened at a settlement instant (an entry at a bar close that coincides with one) or closed at it (an exit at
a bar close) is not charged. The notional is taken at the entry price (the mark at each settlement is not used; the
error is of the order of the price change over the hold times the funding itself).

``funding_r`` is the P&L of the funding leg in R: positive = the trade received funding, negative = it paid.
Pure functions, no I/O; time is a parameter.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
MAX_GAP_MS = 9 * HOUR_MS  # the longest settlement interval seen on Binance USD-M is 8 h; one hour of slack


def floor_minute(t: np.ndarray) -> np.ndarray:
    t = np.asarray(t, dtype=np.int64)
    return t - t % MINUTE_MS


@dataclass(frozen=True)
class Window:
    """Funding settlements of one trade: ``total`` = sum of rates strictly inside (entry_ms, exit_ms), ``n`` their
    count, ``covered`` whether the series spans the whole hold without a hole longer than ``MAX_GAP_MS``."""

    total: float
    n: int
    covered: bool


def window(fund_t: np.ndarray, fund_rate: np.ndarray, entry_ms: int, exit_ms: int, inclusive: bool = False) -> Window:
    """Sum of settlement rates inside the hold (sorted ``fund_t`` in ms, ``fund_rate`` decimals per settlement).

    ``inclusive`` (sensitivity only) counts settlements at the entry or exit instant too. Coverage: the series must
    have a settlement at or before ``entry_ms`` and one at or after ``exit_ms`` (so the hold lies inside its span),
    and consecutive settlements from the last one at or before entry to the first one at or after exit must be at
    most ``MAX_GAP_MS`` apart. Reads nothing after the first settlement at or after ``exit_ms``."""
    t = floor_minute(fund_t)
    r = np.asarray(fund_rate, dtype=float)
    if exit_ms <= entry_ms:
        return Window(0.0, 0, bool(t.size) and int(t[0]) <= entry_ms and int(t[-1]) >= exit_ms)
    if inclusive:
        lo, hi = np.searchsorted(t, entry_ms, "left"), np.searchsorted(t, exit_ms, "right")
    else:
        lo, hi = np.searchsorted(t, entry_ms, "right"), np.searchsorted(t, exit_ms, "left")
    total = float(np.sum(r[lo:hi])) if hi > lo else 0.0
    a = int(np.searchsorted(t, entry_ms, "right")) - 1  # last settlement <= entry
    b = int(np.searchsorted(t, exit_ms, "left"))  # first settlement >= exit
    covered = a >= 0 and b < t.size and bool(np.all(np.diff(t[a : b + 1]) <= MAX_GAP_MS))
    return Window(total, int(max(0, hi - lo)), covered)


def side_sign(side: str) -> int:
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    return 1 if side == "long" else -1


def funding_r(total_rate: float, side: str, stop_frac: float) -> float:
    """Funding P&L in R (primary unit of the card item): ``-side * total_rate / stop_frac``, where ``stop_frac`` =
    |entry - stop| / |entry|. Positive = received."""
    if not stop_frac > 0:
        return math.nan
    return -side_sign(side) * total_rate / stop_frac


def risk_frac(stop_frac: float, side: str, risk_taker: float) -> float:
    """The passports' risk unit as a fraction of the entry price: |e - s| + risk_taker * (|e| + |s|) over |e|, i.e.
    stop_frac + risk_taker * (2 - side * stop_frac) (a long's stop is below the entry, a short's above)."""
    return stop_frac + risk_taker * (2 - side_sign(side) * stop_frac)


def funding_r_passport_unit(total_rate: float, side: str, stop_frac: float, risk_taker: float) -> float:
    """Funding P&L in the passports' R unit (with the taker-fee buffer in the denominator; slightly smaller)."""
    d = risk_frac(stop_frac, side, risk_taker)
    return -side_sign(side) * total_rate / d if d > 0 else math.nan


def summarize(values: Sequence[float]) -> dict[str, float]:
    """n, mean, 5th / 50th / 95th percentile, share of trades that paid (value < 0) of finite ``values``."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"n": 0.0, "m": math.nan, "p5": math.nan, "p50": math.nan, "p95": math.nan, "paid": math.nan}
    p5, p50, p95 = np.quantile(v, [0.05, 0.5, 0.95])
    return {"n": float(v.size), "m": float(v.mean()), "p5": float(p5), "p50": float(p50), "p95": float(p95), "paid": float(np.mean(v < 0))}


def material(mean_r: float, threshold: float = 0.02) -> bool:
    """The card's decision rule: |mean funding in R| >= ``threshold`` (0.02 R) makes the funding line material."""
    return bool(np.isfinite(mean_r)) and abs(mean_r) >= threshold
