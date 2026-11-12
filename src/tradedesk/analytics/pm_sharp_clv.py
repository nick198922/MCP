"""Polymarket football 1X2 vs a sharp closing line (IDEA-20261112-polymarket-sports-vs-sharp-book). Pure functions.

* ``sharp_probs`` — de-vigged probabilities of a home/draw/away decimal-odds triple (proportional or power method).
* ``p_group`` — the card's pre-registered bands of the sharp probability.
* ``quote_at`` — the last Polymarket ``/prices-history`` point strictly before a reference time, if fresh enough;
  points at or after the reference never matter (no look-ahead).
* ``bridge_delta`` — mean (p_alt - p_ref) per band of p_ref, to translate thresholds between two benchmarks
  (e.g. the average closing line ``AvgC*`` vs Pinnacle ``PSC*``) on matches that have both.
* ``norm_team`` / ``team_sim`` / ``resolve_teams`` — Polymarket -> football-data team names (overrides first).
* ``kickoff_utc`` — football-data ``Date`` + ``Time`` (UK local) to unix seconds.
* ``taker_net``, ``cluster_se``, ``drop_top_clusters`` — statistics of g = p_PM - p_sharp.

Probabilities are decimals (0.15 = 15 %), times are unix seconds.
"""

from __future__ import annotations

import math
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

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


# ----------------------------------------------------------------------------- team names and kick-off times

_DROP = frozenset(
    "fc cf afc ac as ss sc ssc club de calcio 1 rc ogc stade losc vfl vfb tsg sv fsv bv bsc rcd ud cd sd ca and the of "
    "us hellas 1899 1846 1907 1909 04 05 fk".split()
)


def norm_team(name: str) -> str:
    """ASCII lower-case team name without club-form tokens ("FC", "AC", years...); used for matching only."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    toks = [t for t in s.split() if t not in _DROP]
    return " ".join(toks) or s.strip()


def team_sim(a: str, b: str) -> float:
    """Similarity of two team names in [0, 1]; 1.0 when one normalised name contains the other."""
    x, y = norm_team(a), norm_team(b)
    if x and y and (x in y or y in x):
        return 1.0
    return SequenceMatcher(None, x, y).ratio()


def resolve_teams(
    pm_names: Iterable[str], fd_names: Iterable[str], overrides: Mapping[str, str], min_sim: float = 0.75
) -> dict[str, str | None]:
    """Map each Polymarket team name to one football-data name of the same league.

    An override (exact Polymarket name -> football-data name) wins; otherwise the most similar football-data name if its
    similarity is at least ``min_sim`` and it is unique (a tie between two candidates gives None); else None."""
    fd = sorted(set(fd_names))
    out: dict[str, str | None] = {}
    for name in sorted(set(pm_names)):
        if name in overrides:
            out[name] = overrides[name] if overrides[name] in fd else None
            continue
        scored = sorted(((team_sim(name, f), f) for f in fd), reverse=True)
        if not scored or scored[0][0] < min_sim or (len(scored) > 1 and scored[1][0] == scored[0][0]):
            out[name] = None
        else:
            out[name] = scored[0][1]
    return out


def kickoff_utc(date_s: str, time_s: str, tz: str = "Europe/London") -> int | None:
    """Unix seconds of a football-data kick-off: ``Date`` (dd/mm/yyyy or dd/mm/yy) and ``Time`` (HH:MM, UK local time)."""
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M"):
        try:
            d = datetime.strptime(f"{date_s.strip()} {time_s.strip()}", fmt)
        except ValueError:
            continue
        return int(d.replace(tzinfo=ZoneInfo(tz)).timestamp())
    return None


# ----------------------------------------------------------------------------- statistics of g


def taker_net(g: float, p: float, direction: int | None = None, fee_rate: float = 0.05) -> float:
    """Taker P&L per contract (decimal of notional 1): edge minus fee ``fee_rate * p * (1 - p)`` at trade price p.

    ``direction`` None: the card's registered |g|, an upper bound that is not reachable in practice: p is a
    book mid (a taker also pays half the spread) and the side is chosen from the sharp closing line at T,
    which is not known when trading before T;
    +1 / -1: the pre-set side (sell an overpriced outcome when g > 0 is expected, buy when g < 0), edge = direction * g."""
    edge = abs(g) if direction is None else direction * g
    return edge - fee_rate * p * (1.0 - p)


def cluster_se(values: Sequence[float], clusters: Sequence[Any]) -> float:
    """Cluster-robust standard error of the pooled mean (CR1 small-sample factor G / (G - 1)); nan with < 2 clusters."""
    if len(values) != len(clusters):
        raise ValueError("values and clusters differ in length")
    n = len(values)
    if n == 0:
        return math.nan
    m = sum(values) / n
    resid: dict[Any, float] = defaultdict(float)
    for v, c in zip(values, clusters, strict=True):
        resid[c] += v - m
    g = len(resid)
    if g < 2:
        return math.nan
    return math.sqrt(sum(r * r for r in resid.values()) * g / (g - 1)) / n


def drop_top_clusters(values: Sequence[float], clusters: Sequence[Any], frac: float) -> tuple[list[float], list[Any]]:
    """Remove the ``ceil(frac * G)`` clusters with the largest |sum of values| (their contribution to the pooled mean)."""
    sums: dict[Any, float] = defaultdict(float)
    for v, c in zip(values, clusters, strict=True):
        sums[c] += v
    k = math.ceil(frac * len(sums)) if sums else 0
    drop = {c for c, _ in sorted(sums.items(), key=lambda kv: (-abs(kv[1]), str(kv[0])))[:k]}
    kept = [(v, c) for v, c in zip(values, clusters, strict=True) if c not in drop]
    return [v for v, _ in kept], [c for _, c in kept]
