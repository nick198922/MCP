"""Pure portfolio-level helpers.

A ``position`` is a dict with at least ``side`` (``long`` | ``short``), ``size``
(> 0) and per-contract ``delta`` / ``gamma`` / ``vega`` / ``theta``. ``mark`` is
in quote currency per contract and used by notional-value aggregation. The
sign convention: longs contribute greeks as-is; shorts flip the sign.

Greeks must be in a consistent set of units (we recommend Deribit-style:
delta in coin/\\$, vega in \\$ per 1 vol-point, theta in \\$ per day,
gamma in coin/\\$^2) — the aggregator does not try to translate.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _sign(side: str) -> int:
    return 1 if side == "long" else -1 if side == "short" else 0


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def aggregate_greeks(positions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Net delta / gamma / vega / theta plus notional_usd across positions.

    ``size`` is required; a missing greek is treated as 0 so partial data still
    gives you a sensible sum. Returns per-greek totals and a per-instrument
    breakdown (dict keyed by instrument).
    """
    totals = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "notional_usd": 0.0, "count": 0}
    per_instrument: dict[str, dict[str, float]] = {}
    for p in positions:
        side = p.get("side")
        sign = _sign(side)
        if sign == 0:
            continue
        size = _f(p.get("size")) or 0.0
        if size <= 0:
            continue
        totals["count"] += 1
        mult = sign * size
        d = _f(p.get("delta")) or 0.0
        g = _f(p.get("gamma")) or 0.0
        v = _f(p.get("vega")) or 0.0
        t = _f(p.get("theta")) or 0.0
        mark = _f(p.get("mark")) or 0.0
        totals["delta"] += d * mult
        totals["gamma"] += g * mult
        totals["vega"] += v * mult
        totals["theta"] += t * mult
        totals["notional_usd"] += mark * size  # size (always positive) times mark USD
        row = per_instrument.setdefault(
            p.get("instrument") or "<unknown>", {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "size_signed": 0.0}
        )
        row["delta"] += d * mult
        row["gamma"] += g * mult
        row["vega"] += v * mult
        row["theta"] += t * mult
        row["size_signed"] += mult
    return {"totals": totals, "per_instrument": per_instrument}


def scenario_pnl(
    positions: Iterable[dict[str, Any]],
    *,
    d_spot_pct: float = 0.0,
    d_vol_points: float = 0.0,
    reference_spot: float | None = None,
    days: float = 0.0,
) -> dict[str, Any]:
    """Second-order PnL approximation at the net-greeks level.

    PnL = Σ_i sign_i · size_i · (delta_i · dSpot + ½ · gamma_i · dSpot²
          + vega_i · d_vol_points + theta_i · days)

    * ``d_spot_pct`` is the move in percent (0.10 = +10 %). We convert to an
      absolute USD move using ``reference_spot`` (the first position's mark if
      not given). This is an approximation; it does NOT re-price option legs.
    * ``d_vol_points`` is the IV shift in vol-points (vega-compatible units).
    * ``days`` optionally decays positions via their theta.
    """
    rows = list(positions)
    if reference_spot is None:
        marks = [_f(p.get("mark")) for p in rows if _f(p.get("mark"))]
        reference_spot = marks[0] if marks else None
    d_spot = (reference_spot or 0.0) * d_spot_pct

    total = 0.0
    per_leg: list[dict[str, Any]] = []
    for p in rows:
        sign = _sign(p.get("side"))
        if sign == 0:
            continue
        size = _f(p.get("size")) or 0.0
        if size <= 0:
            continue
        d = _f(p.get("delta")) or 0.0
        g = _f(p.get("gamma")) or 0.0
        v = _f(p.get("vega")) or 0.0
        th = _f(p.get("theta")) or 0.0
        pnl = sign * size * (d * d_spot + 0.5 * g * d_spot * d_spot + v * d_vol_points + th * days)
        total += pnl
        per_leg.append({"instrument": p.get("instrument"), "pnl": pnl})
    return {
        "d_spot_pct": d_spot_pct,
        "d_spot_abs": d_spot,
        "d_vol_points": d_vol_points,
        "days": days,
        "reference_spot": reference_spot,
        "total_pnl": total,
        "per_leg": per_leg,
    }
