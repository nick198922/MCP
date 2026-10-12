"""Fibonacci retracement calculator.

Pure math only. Pattern from the DoubleTop FOMC write-up: given a reference
price ``P0`` and a reaction extreme ``L`` (low in a drop, high in a rally),
project the standard retracement ladder.

Formula is the same in both directions:
    ``price_at(level) = L + (P0 - L) * level``

* Drop (P0 > L): ladder prices sit between L (level 0) and P0 (level 1.0).
* Rally (P0 < L): symmetric; levels count back down toward P0.

Historical hit-rate for these levels needs a windowed walk through
``ohlcv_hist`` — kept out of this helper so the math stays pure; wire it in a
separate tool when the time comes.
"""

from __future__ import annotations

from typing import Any

DEFAULT_LEVELS: tuple[float, ...] = (0.236, 0.382, 0.5, 0.618, 0.786)


def fib_levels(p0: float, l_point: float, levels: list[float] | None = None) -> list[dict[str, Any]]:
    """Return the ladder as a list of ``{level, price}``.

    ``p0`` is the pre-reaction reference (decision price). ``l_point`` is the
    reaction extreme (lowest low in a drop, highest high in a rally).
    """
    chosen = list(levels or DEFAULT_LEVELS)
    rows: list[dict[str, Any]] = []
    for lv in chosen:
        price = l_point + (p0 - l_point) * float(lv)
        rows.append({"level": float(lv), "price": price})
    return rows


def summarise_retracement(
    p0: float,
    l_point: float,
    *,
    levels: list[float] | None = None,
    min_drop_pct: float = 2.0,
) -> dict[str, Any]:
    """Full ladder + metadata.

    Direction is derived from the sign of ``P0 - L``. ``applicable`` is True
    when the drop/rally is at least ``min_drop_pct`` percent — a cheap guard
    against taking a ladder off noise.
    """
    if p0 <= 0 or l_point <= 0:
        return {"applicable": False, "reason": "non-positive prices"}
    diff = p0 - l_point
    direction = "drop" if diff > 0 else "rally" if diff < 0 else "flat"
    pct = (diff / p0) * 100.0 if p0 > 0 else 0.0
    applicable = direction != "flat" and abs(pct) >= min_drop_pct
    ladder = fib_levels(p0, l_point, levels)
    return {
        "p0": p0,
        "l": l_point,
        "diff_abs": diff,
        "diff_pct": pct,
        "direction": direction,
        "applicable": applicable,
        "min_drop_pct": min_drop_pct,
        "ladder": ladder,
    }
