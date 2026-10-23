"""Relative strength through an alt-market crash: event detection and forward returns (pure, no I/O).

Used by ``scripts/verify_crash_leader.py`` (IDEA-20261023-crash-relative-strength). Arrays are dense
days × symbols panels (NaN = no bar); day indices are integers into those panels.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def compound_index(daily_ret: np.ndarray) -> np.ndarray:
    """Index level (start 1.0 before day 0) from daily returns; a NaN day counts as flat."""
    return np.cumprod(1 + np.nan_to_num(daily_ret, nan=0.0))


@dataclass(frozen=True)
class CrashEvent:
    day: int  # first close at or below the threshold
    peak: int  # day of the trailing max the drawdown is measured from
    drawdown: float  # index drawdown on ``day`` (negative)


def detect_events(
    index: np.ndarray, *, threshold: float, lookback: int = 90, rearm: float = 0.15, min_gap: int = 30, first_day: int = 0
) -> list[CrashEvent]:
    """Crash events: the index closes ≤ −``threshold`` below its max over the last ``lookback`` days (incl. today).

    After an event, the next one needs the drawdown to recover above −``rearm`` first and ``min_gap`` days to pass.
    Only past and current closes are used.
    """
    events: list[CrashEvent] = []
    armed = True
    for d in range(max(first_day, 0), len(index)):
        lo = max(0, d - lookback + 1)
        window = index[lo : d + 1]
        peak = lo + int(np.argmax(window))
        dd = index[d] / index[peak] - 1
        if not armed and dd > -rearm and (not events or d - events[-1].day >= min_gap):
            armed = True
        if armed and dd <= -threshold:
            events.append(CrashEvent(d, peak, float(dd)))
            armed = False
    return events


def hold_return(close: np.ndarray, fund: np.ndarray, j: int, start: int, horizon: int, *, gap: int = 3) -> float:
    """Long return of column ``j`` from the close of ``start`` to the close of ``start + horizon``, minus funding.

    A column without a bar ``gap`` days in a row is closed at its last close (delisting) and the money then sits
    in cash. Funding is the sum of the daily rates (a long pays a positive rate) over the days it was held.
    """
    p0 = close[start, j]
    if not np.isfinite(p0) or p0 <= 0:
        return float("nan")
    last, missing, paid = p0, 0, 0.0
    end = min(start + horizon, close.shape[0] - 1)
    for d in range(start + 1, end + 1):
        c = close[d, j]
        if np.isfinite(c):
            last, missing = c, 0
            f = fund[d, j]
            paid += 0.0 if not np.isfinite(f) else f
        else:
            missing += 1
            if missing >= gap:
                break
    return float(last / p0 - 1 - paid)


def terciles(scores: dict[int, float]) -> tuple[list[int], list[int]]:
    """(strong, weak): the top and bottom thirds of columns by score; ties broken by column index."""
    order = sorted(scores, key=lambda j: (-scores[j], j))
    k = len(order) // 3
    return order[:k], order[len(order) - k :]


def beta(y: np.ndarray, x: np.ndarray) -> float:
    """OLS slope of ``y`` on ``x`` over the days where both are finite."""
    m = np.isfinite(y) & np.isfinite(x)
    if m.sum() < 10 or np.var(x[m]) == 0:
        return float("nan")
    return float(np.cov(y[m], x[m])[0, 1] / np.var(x[m], ddof=1))
