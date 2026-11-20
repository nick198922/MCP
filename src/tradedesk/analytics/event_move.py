"""Implied move of a dated event from two adjacent option expiries (forward variance). Pure: no I/O, ``now`` passed in.

Total implied variance to an expiry is w = sigma^2 * T (ATM IV as a decimal, T = days / 365). Between two adjacent
expiries E1 < E2 the forward variance is (w2 - w1) / (T2 - T1) and the variance added over the window is w2 - w1.
sqrt(w2 - w1) is the 1-sigma log move (decimal) of the WHOLE window: the event plus the window's ordinary diffusion,
which is not removed. It is therefore an upper bound on the event's own move, even for a one-day window (one day of
ordinary crypto diffusion is itself a move of a few percent); for a one-day window it is exactly
forward_iv * sqrt(1/365). An event premium shows only when the forward IV exceeds the vol without the event (e.g. the
forward vol of a neighbouring window with no event). Risk-neutral, not a forecast.
A non-positive w2 - w1 is a calendar arbitrage in the marks: no square root is taken, the move is None.

The event's excess over the base diffusion (the way an earnings move is backed out of equity options): the base
forward variance is that of a neighbouring window with no event, (E0, E1] or (E2, E3] (``base_forward_iv``), and
excess = sqrt(max(w2 - w1 - base_forward_var * (T2 - T1), 0)) (``excess_event_move``). An estimate, not a forecast: it
assumes the neighbouring window's forward vol is the vol the event window would have without the event. It is not
clean when another event lies inside (E1, E2] (``events_in_event_window``) or when the base window is short
(``base_window_is_short``).
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


Point = tuple[datetime, float, float]  # (expiry, T in years, ATM IV as a decimal)


def _locate(expiries: Iterable[tuple[datetime, float]], event: datetime, now: datetime) -> tuple[list[Point], int] | str:
    """Live (expiry, T, iv) points sorted by expiry and the index of E2 (first expiry at or after ``event``), or the
    reason there is no window (E1, E2] around the event."""
    if event <= now:
        return "the event time is not after now"
    pts = sorted(
        (e, year_fraction(e, now), float(iv))
        for e, iv in expiries
        if e > now and isinstance(iv, (int, float)) and math.isfinite(iv) and iv > 0
    )
    if not pts:
        return "no live expiry with an ATM IV"
    after = next((i for i, p in enumerate(pts) if p[0] >= event), None)
    if after is None:
        return f"the event is after the last listed expiry ({pts[-1][0].isoformat()})"
    if after == 0:
        return f"the event is at or before the first listed expiry ({pts[0][0].isoformat()}): no earlier expiry to difference against"
    return pts, after


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
    loc = _locate(expiries, event, now)
    if isinstance(loc, str):
        return {"ok": False, "reason": loc}
    pts, after = loc
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


def base_forward_iv(
    expiries: Iterable[tuple[datetime, float]],
    event: datetime,
    now: datetime,
    other_events: Iterable[datetime] = (),
) -> dict[str, Any]:
    """Forward vol of a window next to the event's window (E1, E2] that holds no event: (E0, E1] or (E2, E3].

    ``expiries`` and ``event`` as in ``implied_event_move`` (same filtering, same window); ``other_events``: aware UTC
    datetimes of other dated events. A side is unusable when the neighbouring expiry is missing, when one of
    ``other_events`` falls inside its window (start, end], or when its forward variance is not positive (calendar
    arbitrage). Of the usable sides the one whose length is closest to the event window's is taken (a tie goes to the
    earlier window). Returns ``{"ok": False, "reason": ...}`` (the reasons of both sides, or why there is no event
    window) or ``ok`` True and: ``side`` ("before" / "after"), ``start`` / ``end`` (expiries), ``T_start`` / ``T_end``
    (years), ``window_days``, ``forward_var`` (annualised decimal), ``forward_iv`` (its square root) and ``rejected``
    (reasons for an unusable other side, empty when both were usable)."""
    loc = _locate(expiries, event, now)
    if isinstance(loc, str):
        return {"ok": False, "reason": loc}
    pts, after = loc
    others = list(other_events)
    event_days = (pts[after][0] - pts[after - 1][0]).total_seconds() / 86400.0
    usable: list[dict[str, Any]] = []
    rejected: list[str] = []
    for side, i, j in (("before", after - 2, after - 1), ("after", after, after + 1)):
        if i < 0 or j >= len(pts):
            rejected.append(f"{side}: no {'earlier' if side == 'before' else 'later'} expiry with an ATM IV")
            continue
        (s, ts, vs), (e, te, ve) = pts[i], pts[j]
        if te <= ts:  # the same expiry listed twice
            rejected.append(f"{side}: expiry {e.isoformat()} is listed twice")
            continue
        inside = sorted(x for x in others if s < x <= e)
        if inside:
            rejected.append(f"{side}: window ({s.isoformat()}, {e.isoformat()}] holds another event ({inside[0].isoformat()})")
            continue
        fv = forward_variance(vs, ts, ve, te)
        if fv <= 0:
            rejected.append(f"{side}: non-positive forward variance between {s.isoformat()} and {e.isoformat()}")
            continue
        days = (e - s).total_seconds() / 86400.0
        usable.append(
            {
                "side": side,
                "start": s,
                "end": e,
                "T_start": ts,
                "T_end": te,
                "window_days": days,
                "forward_var": fv,
                "forward_iv": math.sqrt(fv),
            }
        )
    if not usable:
        return {"ok": False, "reason": "; ".join(rejected)}
    best = min(usable, key=lambda u: abs(u["window_days"] - event_days))  # min keeps the first ("before") on a tie
    return {"ok": True, **best, "rejected": rejected}


def excess_event_move(event_var: float, base_forward_var: float, t1: float, t2: float) -> dict[str, Any]:
    """The event's own variance and 1-sigma move beyond the base diffusion of its window (E1, E2].

    ``event_var`` = iv2^2 T2 - iv1^2 T1 (from ``implied_event_move``), ``base_forward_var`` the annualised forward variance
    without the event (``base_forward_iv``), T in years. Returns ``base_var`` = base_forward_var * (t2 - t1),
    ``excess_var`` = event_var - base_var (may be negative), ``excess_move`` = sqrt(max(excess_var, 0)) (a 1-sigma log
    move as a decimal; 0.0 when the window's variance is not above the base) and ``below_base`` (excess_var <= 0).
    Raises ValueError unless every input is finite, base_forward_var > 0 and 0 <= t1 < t2."""
    if not all(math.isfinite(x) for x in (event_var, base_forward_var, t1, t2)):
        raise ValueError("inputs must be finite")
    if base_forward_var <= 0:
        raise ValueError("base_forward_var must be positive")
    if not 0 <= t1 < t2:
        raise ValueError("need 0 <= t1 < t2")
    base_var = base_forward_var * (t2 - t1)
    x = event_var - base_var
    return {"base_var": base_var, "excess_var": x, "excess_move": math.sqrt(x) if x > 0 else 0.0, "below_base": x <= 0}


def events_in_event_window(other_events: Iterable[datetime], event: datetime, start: datetime, end: datetime) -> list[datetime]:
    """``other_events`` that fall inside the event's own window (start, end] = (E1, E2], sorted; the event itself
    (equal to ``event``) is not counted. Their variance is in w2 - w1 too, so the excess is attributed to the main
    event although it is not that event's alone. Aware UTC datetimes; pure, no clock."""
    return sorted(x for x in other_events if start < x <= end and x != event)


SHORT_BASE_DAYS = 1.0  # a base window under one day is a difference of two nearly equal total variances
SHORT_BASE_SHARE = 0.5  # or under half the event window: its forward vol is scaled up over a much longer window


def base_window_is_short(base_days: float, event_days: float) -> bool:
    """True when the base window is shorter than ``SHORT_BASE_DAYS`` or than ``SHORT_BASE_SHARE`` of the event window
    (both in days): its forward variance is noisy and scaling it to the event window may bias the excess badly."""
    return base_days < SHORT_BASE_DAYS or base_days < SHORT_BASE_SHARE * event_days
