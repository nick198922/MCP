"""Implied move of a dated event from two adjacent option expiries (forward variance). Pure: no I/O, ``now`` passed in.

Total implied variance to an expiry is w = sigma^2 * T (ATM IV as a decimal, T = days / 365). Between two adjacent
expiries E1 < E2 the forward variance is (w2 - w1) / (T2 - T1) and the variance added over the window is w2 - w1.
sqrt(w2 - w1) is the 1-sigma log move (decimal) of the WHOLE window: the event plus the window's ordinary diffusion,
which is not removed. It is therefore an upper bound on the event's own move, even for a one-day window (one day of
ordinary crypto diffusion is itself a move of a few percent); for a one-day window it is exactly
forward_iv * sqrt(1/365). An event premium shows only when the forward IV exceeds the vol without the event (e.g. the
forward vol of a neighbouring window with no event). Risk-neutral, not a forecast.
A non-positive w2 - w1 is a calendar arbitrage in the marks: no square root is taken, the move is None.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .options import year_fraction


def forward_variance(iv1: float, t1: float, iv2: float, t2: float) -> float:
    """Annualised forward variance between two expiries: (iv2^2 t2 - iv1^2 t1) / (t2 - t1). Decimal IVs, T in years.

    May be negative (calendar arbitrage in the marks); the caller decides what to do then. Raises ValueError unless
    0 <= t1 < t2 and both IVs are finite and positive."""
    if not (math.isfinite(iv1) and math.isfinite(iv2) and iv1 > 0 and iv2 > 0):
        raise ValueError("IVs must be finite and positive decimals")
    if not (math.isfinite(t1) and math.isfinite(t2) and 0 <= t1 < t2):
        raise ValueError("need 0 <= t1 < t2")
    return (iv2 * iv2 * t2 - iv1 * iv1 * t1) / (t2 - t1)


def implied_event_move(expiries: Iterable[tuple[datetime, float]], event: datetime, now: datetime) -> dict[str, Any]:
    """Implied move of an event at ``event`` (aware UTC datetime) from the two adjacent expiries around it.

    ``expiries``: (expiry, ATM IV as a decimal) pairs in any order; an expiry at or before ``now`` or with an IV that is
    not a finite positive number is ignored. The event belongs to the window (E1, E2]: E1 is the last expiry strictly
    before the event, E2 the first at or after it. Returns ``{"ok": False, "reason": ...}`` when the event is not after
    ``now`` or not inside the listed expiries (no expiry before it, or none after it). Otherwise ``ok`` True and:
    ``before`` / ``after`` (expiry, T in years, iv), ``window_days`` (E2 - E1 in days), ``total_var_before`` /
    ``total_var_after`` (iv^2 * T), ``event_var`` (their difference), ``forward_var`` (``event_var`` / (T2 - T1),
    annualised), ``forward_iv`` (sqrt of it, None when not positive), ``event_move`` (sqrt(``event_var``), a 1-sigma
    log move as a decimal, None when not positive), ``event_share`` (``event_var`` / ``total_var_after``) and
    ``calendar_arbitrage`` (``event_var`` <= 0).
    """
    if event <= now:
        return {"ok": False, "reason": "the event time is not after now"}
    pts = sorted(
        (e, year_fraction(e, now), float(iv))
        for e, iv in expiries
        if e > now and isinstance(iv, (int, float)) and math.isfinite(iv) and iv > 0
    )
    if not pts:
        return {"ok": False, "reason": "no live expiry with an ATM IV"}
    after = next((i for i, p in enumerate(pts) if p[0] >= event), None)
    if after is None:
        return {"ok": False, "reason": f"the event is after the last listed expiry ({pts[-1][0].isoformat()})"}
    if after == 0:
        return {
            "ok": False,
            "reason": f"the event is at or before the first listed expiry ({pts[0][0].isoformat()}): no earlier expiry to difference against",
        }
    (e1, t1, v1), (e2, t2, v2) = pts[after - 1], pts[after]
    w1, w2 = v1 * v1 * t1, v2 * v2 * t2
    ev = w2 - w1
    fwd = forward_variance(v1, t1, v2, t2)
    return {
        "ok": True,
        "before": {"expiry": e1, "T": t1, "iv": v1},
        "after": {"expiry": e2, "T": t2, "iv": v2},
        "window_days": (e2 - e1).total_seconds() / 86400.0,
        "total_var_before": w1,
        "total_var_after": w2,
        "event_var": ev,
        "forward_var": fwd,
        "forward_iv": math.sqrt(fwd) if fwd > 0 else None,
        "event_move": math.sqrt(ev) if ev > 0 else None,
        "event_share": ev / w2 if w2 > 0 else None,
        "calendar_arbitrage": ev <= 0,
    }
