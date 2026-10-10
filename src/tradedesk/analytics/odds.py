"""Odds <-> probability helpers (bookmaker maths): margin and de-vigging."""

from __future__ import annotations


def decimal_to_prob(odds: float) -> float:
    if odds <= 1.0:
        raise ValueError("decimal odds must be > 1")
    return 1.0 / odds


def overround(odds: list[float]) -> float:
    """Booksum minus 1: the bookmaker margin in fraction (0.05 = 5 %)."""
    return sum(decimal_to_prob(o) for o in odds) - 1.0


def devig_proportional(odds: list[float]) -> list[float]:
    p = [decimal_to_prob(o) for o in odds]
    s = sum(p)
    return [x / s for x in p]


def devig_power(odds: list[float], tol: float = 1e-12) -> list[float]:
    """Power method: find k such that sum(p_i ** k) == 1 (shrinks long shots harder)."""
    p = [decimal_to_prob(o) for o in odds]
    if sum(p) <= 1.0:
        return p
    lo, hi = 1.0, 1.0
    while sum(x**hi for x in p) > 1.0:
        hi *= 2.0
        if hi > 1e6:
            raise ValueError("power de-vig did not converge")
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if sum(x**mid for x in p) > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    k = (lo + hi) / 2.0
    return [x**k for x in p]


def devig(odds: list[float], method: str = "proportional") -> dict:
    if method == "proportional":
        probs = devig_proportional(odds)
    elif method == "power":
        probs = devig_power(odds)
    else:
        raise ValueError("method must be 'proportional' or 'power'")
    return {
        "method": method,
        "margin": overround(odds),
        "implied_raw": [decimal_to_prob(o) for o in odds],
        "fair_probs": probs,
        "fair_odds": [1.0 / x for x in probs],
    }
