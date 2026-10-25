"""Cross-sectional anchoring features of USDT-M perps: nearness to the trailing 365-day high (George-Hwang style).

Pure functions (no I/O). Time is passed in as UTC day indices (``ms // DAY_MS``); the rank day ``t`` is a Monday and a
feature uses only bars of days ``< t`` (the close of day ``t - 1`` is the price at ``t`` 00:00 UTC). Nearness is a ratio
in (0, 1] (1 = the last close is the window high); returns are decimals (0.01 = 1 %); age is in days.
Idea card: docs/ideas/IDEA-20261025-xs-anchor-nearness-high-short.md (section "Попередня реєстрація").

The universe, the weekly leg and the rank statistics are reused from ``funding_crowding``, ``xs_factors`` and
``xs_volume`` unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from .funding_crowding import Bars
from .xs_factors import beta_ivol, close_near, rank_z, window_return

FEATURES = ("NEAR365", "NEAR180", "NEAR90", "ATH", "M90", "AGE", "BETA90")
# (window, minimum number of closes inside it)
NEAR_WINDOWS = {"NEAR365": (365, 300), "NEAR180": (180, 150), "NEAR90": (90, 75)}


def near_high(bars: Bars, t: int, *, window: int = 365, min_days: int = 300, tol: int = 3) -> float | None:
    """``C(t - 1) / max(C_d for d in [t - window, t - 1])``.

    ``C(t - 1)`` is the close of day ``t - 1`` or of the nearest earlier day within ``tol`` days. None with fewer than
    ``min_days`` closes in the window, without a recent close, or with a non-positive high."""
    last = close_near(bars, t - 1, tol=tol)
    vals = [bars.close[d] for d in range(t - window, t) if d in bars.close]
    if last is None or len(vals) < min_days:
        return None
    hi = max(vals)
    return last / hi if hi > 0 else None


def near_ath(bars: Bars, t: int, *, min_days: int = 300, tol: int = 3) -> float | None:
    """``C(t - 1) / max(all closes of days < t)`` (the high since the first bar in ``bars``; the caller says where the
    history starts). None with fewer than ``min_days`` closes before ``t`` or without a recent close."""
    last = close_near(bars, t - 1, tol=tol)
    vals = [c for d, c in bars.close.items() if d < t]
    if last is None or len(vals) < min_days:
        return None
    hi = max(vals)
    return last / hi if hi > 0 else None


def age_days(first_day: int | None, t: int) -> int | None:
    """Days from the first known listing day to ``t``; None if unknown or not listed before ``t``."""
    if first_day is None or first_day >= t:
        return None
    return t - first_day


def features_at(bars: Bars, btc: Bars, t: int, first_day: int | None) -> dict[str, float | None]:
    """All anchor features at rank day ``t`` (days < t only).

    NEAR365 / NEAR180 / NEAR90: ``near_high`` over 365 / 180 / 90 days (>= 300 / 150 / 75 closes); ATH: ``near_ath``;
    M90: ``C(t - 8) / C(t - 91) - 1`` (90-day momentum skipping the last week, like M28); AGE: ln(days since the first
    listing day); BETA90: OLS beta on BTC daily returns over 90 days (>= 60 common days)."""
    out: dict[str, float | None] = {k: near_high(bars, t, window=w, min_days=m) for k, (w, m) in NEAR_WINDOWS.items()}
    out["ATH"] = near_ath(bars, t)
    out["M90"] = window_return(bars, t, 91, 8)
    a = age_days(first_day, t)
    out["AGE"] = math.log(a) if a is not None else None
    bi = beta_ivol(bars, btc, t, window=90, min_days=60)
    out["BETA90"] = bi[0] if bi is not None else None
    return out


def hedged_returns(returns: Sequence[float], betas: Sequence[float], r_btc: float) -> list[float]:
    """``r_i - beta_i * r_BTC``: the weekly return of a coin with a BTC position of ``-beta`` per unit (gross of costs)."""
    if len(returns) != len(betas):
        raise ValueError("returns and betas need the same length")
    return [r - b * r_btc for r, b in zip(returns, betas, strict=True)]


def low_minus_middle(signal: Sequence[float], returns: Sequence[float], *, q: int = 5) -> float:
    """Mean return of the lowest ``1/q`` of the signal minus the mean return of the middle ``1/q`` (equal weights).

    With ``q = 5`` this is Q1 - Q3: the side a short of the bottom quintile trades, against the median group. Ties are
    ordered by average rank, then by input order."""
    n = len(signal)
    if len(returns) != n or n < q:
        raise ValueError("need equal lengths and at least q values")
    order = np.argsort(rank_z(signal), kind="stable")
    k = n // q
    r = np.asarray(returns, dtype=float)
    mid0 = (n - k) // 2
    return float(r[order[:k]].mean() - r[order[mid0 : mid0 + k]].mean())


def bottom_set(signal: Sequence[float], *, q: int = 5) -> set[int]:
    """Indices of the lowest ``1/q`` of the signal (stable on ties)."""
    order = np.argsort(rank_z(signal), kind="stable")
    return {int(i) for i in order[: len(signal) // q]}
