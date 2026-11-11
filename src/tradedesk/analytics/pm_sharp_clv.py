"""Polymarket football 1X2 vs a sharp closing line (IDEA-20261112-polymarket-sports-vs-sharp-book). Pure functions.

* ``sharp_probs`` — de-vigged probabilities of a home/draw/away decimal-odds triple (proportional or power method).
* ``p_group`` — the card's pre-registered bands of the sharp probability.
* ``quote_at`` — the last Polymarket ``/prices-history`` point strictly before a reference time, if fresh enough;
  points at or after the reference never matter (no look-ahead).
* ``bridge_delta`` — mean (p_alt - p_ref) per band of p_ref, to translate thresholds between two benchmarks
  (e.g. the average closing line ``AvgC*`` vs Pinnacle ``PSC*``) on matches that have both.

Probabilities are decimals (0.15 = 15 %), times are unix seconds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .odds import devig_power, devig_proportional

BANDS = ("<=0.15", "0.15-0.40", "0.40-0.60", ">=0.60")


def sharp_probs(odds: Sequence[float | None], method: str = "proportional") -> tuple[float, float, float] | None:
    """De-vigged (home, draw, away) probabilities; None unless all three decimal odds are finite and > 1."""
    if len(odds) != 3:
        return None
    clean: list[float] = []
    for o in odds:
        if o is None or not isinstance(o, int | float) or not (1.0 < float(o) < 1e6):
            return None
        clean.append(float(o))
    if method == "proportional":
        p = devig_proportional(clean)
    elif method == "power":
        p = devig_power(clean)
    else:
        raise ValueError(f"unknown de-vig method {method!r}")
    return (p[0], p[1], p[2])


def p_group(p: float) -> str:
    """Band of a sharp probability: <=0.15, (0.15, 0.40), [0.40, 0.60), >=0.60."""
    if p <= 0.15:
        return BANDS[0]
    if p < 0.40:
        return BANDS[1]
    if p < 0.60:
        return BANDS[2]
    return BANDS[3]


def quote_at(points: Iterable[Mapping[str, Any]], t_ref: int, max_age_s: int) -> tuple[float, int] | None:
    """(price, t) of the last point with t < t_ref, provided t_ref - t <= max_age_s; else None.

    `points` are ``{"t": unix_s, "p": price}`` rows in any order; malformed rows are skipped."""
    best: tuple[int, float] | None = None
    for row in points:
        try:
            t, p = int(row["t"]), float(row["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if t >= t_ref or not (0.0 <= p <= 1.0):
            continue
        if best is None or t > best[0]:
            best = (t, p)
    if best is None or t_ref - best[0] > max_age_s:
        return None
    return best[1], best[0]


def bridge_delta(pairs: Iterable[tuple[float, float]]) -> dict[str, dict[str, float]]:
    """Per band of p_ref: n and mean(p_alt - p_ref) over (p_ref, p_alt) pairs of the same outcome."""
    acc: dict[str, list[float]] = defaultdict(list)
    for p_ref, p_alt in pairs:
        acc[p_group(p_ref)].append(p_alt - p_ref)
    return {b: {"n": float(len(acc[b])), "mean": sum(acc[b]) / len(acc[b])} for b in BANDS if acc[b]}
