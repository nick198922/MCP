"""Top-trader vs crowd long/short divergence on Binance USDT-M perps (idea card
docs/ideas/IDEA-20261024-toptrader-vs-crowd-divergence.md, section "Попередня реєстрація").

Pure functions, no I/O. Time is passed in: days are UTC day indices (``ms // DAY_MS``), a rank day ``t`` is a Monday
and its signal comes from the archive ``metrics`` row of Sunday ``t - 1`` observed no later than 23:55 UTC, i.e.
strictly before the open of ``t``. Ratios are long / short (dimensionless); returns are decimals (0.01 = 1 %).

Signal: x = ln(sum_toptrader_long_short_ratio), y = ln(count_long_short_ratio); each is z-scored against its own
trailing window of weekly (Sunday) values including the current one; D = z(x) - z(y). Positive D: top traders are
longer than usual relative to the crowd.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence

import numpy as np

from .oi_flush import SEMANTICS_SWITCH_MS, archive_obs_times
from .xs_factors import rank_z

DAY_MS = 86_400_000
MIN_MS = 60_000
# column positions in providers.binance_vision.parse_metrics_ratios_zip rows
COL = {"oi": 1, "oi_value": 2, "top_acc": 3, "top_pos": 4, "crowd": 5, "taker": 6}


def day_snapshot(
    rows: Sequence[Sequence[float]], day: int, *, margin_ms: int = 5 * MIN_MS, switch_ms: int = SEMANTICS_SWITCH_MS
) -> dict[str, float] | None:
    """Ratios of the last row of UTC ``day`` observed no later than ``day`` end minus ``margin_ms``.

    ``rows``: ``[create_time_ms, oi, oi_value, top_acc, top_pos, crowd, taker]`` of the day's archive file. Rows with
    a non-positive or NaN ratio among top_acc / top_pos / crowd are skipped. Returns {"obs_ms", "top_acc", "top_pos",
    "crowd"} or None."""
    if not rows:
        return None
    cutoff = (day + 1) * DAY_MS - margin_ms
    lo = day * DAY_MS
    create = np.array([r[0] for r in rows], dtype=np.int64)
    obs = archive_obs_times(create, switch_ms=switch_ms)
    for i in np.argsort(obs, kind="stable")[::-1]:
        if obs[i] > cutoff or obs[i] < lo:
            continue
        r = rows[int(i)]
        vals = {k: float(r[COL[k]]) for k in ("top_acc", "top_pos", "crowd")}
        if all(math.isfinite(v) and v > 0 for v in vals.values()):
            return {"obs_ms": float(obs[i]), **vals}
    return None


def trailing_z(values: Sequence[float | None], *, window: int = 13, min_obs: int = 10) -> list[float | None]:
    """z of each value against values ``i - window + 1 .. i`` (current included), SD with ddof 1.

    None where the current value is missing, fewer than ``min_obs`` values are present or the SD is 0. Causal: entry
    ``i`` depends on entries ``<= i`` only."""
    out: list[float | None] = []
    for i, v in enumerate(values):
        if v is None or not math.isfinite(v):
            out.append(None)
            continue
        win = [x for x in values[max(0, i - window + 1) : i + 1] if x is not None and math.isfinite(x)]
        if len(win) < max(min_obs, 2):
            out.append(None)
            continue
        sd = statistics.stdev(win)
        out.append((v - statistics.fmean(win)) / sd if sd > 0 else None)
    return out


def divergence(top: Sequence[float | None], crowd: Sequence[float | None], *, window: int = 13, min_obs: int = 10) -> list[float | None]:
    """D_i = z(ln top)_i - z(ln crowd)_i on a regular weekly grid (None = missing week). Ratios must be > 0."""
    if len(top) != len(crowd):
        raise ValueError("top and crowd must have the same length")
    lx = [math.log(v) if v is not None and v > 0 else None for v in top]
    ly = [math.log(v) if v is not None and v > 0 else None for v in crowd]
    zx = trailing_z(lx, window=window, min_obs=min_obs)
    zy = trailing_z(ly, window=window, min_obs=min_obs)
    return [a - b if a is not None and b is not None else None for a, b in zip(zx, zy, strict=True)]


def weekly_ic(signal: Sequence[float], returns: Sequence[float]) -> float:
    """Spearman IC (average ranks) of one cross-section; NaN with fewer than 3 names."""
    if len(signal) != len(returns):
        raise ValueError("length mismatch")
    if len(signal) < 3:
        return float("nan")
    return float(np.mean(rank_z(signal) * rank_z(returns)))


def placebo_within_asset(weeks: Sequence[Mapping[str, tuple[float, float]]], n_perm: int, *, seed: int = 7) -> np.ndarray:
    """Mean weekly IC when each asset's signal values are shuffled across the weeks where it is present.

    ``weeks``: per week {asset: (signal, return)}. The asset keeps its own signal distribution and its presence
    pattern; only the timing is destroyed. Returns ``n_perm`` mean ICs."""
    slots: dict[str, list[tuple[int, int]]] = {}
    sig: list[np.ndarray] = []
    zr: list[np.ndarray] = []
    names: list[list[str]] = []
    for w, wk in enumerate(weeks):
        ks = sorted(wk)
        names.append(ks)
        sig.append(np.array([wk[k][0] for k in ks], dtype=float))
        zr.append(rank_z([wk[k][1] for k in ks]))
        for j, k in enumerate(ks):
            slots.setdefault(k, []).append((w, j))
    rng = np.random.default_rng(seed)
    out = np.empty(n_perm)
    for p in range(n_perm):
        cur = [s.copy() for s in sig]
        for pos in slots.values():
            vals = [sig[w][j] for w, j in pos]
            perm = rng.permutation(len(vals))
            for (w, j), q in zip(pos, perm, strict=True):
                cur[w][j] = vals[q]
        ics = [float(np.mean(rank_z(c) * z)) for c, z in zip(cur, zr, strict=True) if c.size >= 3]
        out[p] = statistics.fmean(ics) if ics else float("nan")
    return out


def hit_rate(signal: Sequence[float], returns: Sequence[float]) -> tuple[float, int]:
    """Share of pairs with sign(signal) == sign(return) among pairs where both are non-zero; (share, n)."""
    pairs = [(s, r) for s, r in zip(signal, returns, strict=True) if s != 0 and r != 0]
    if not pairs:
        return float("nan"), 0
    return sum((s > 0) == (r > 0) for s, r in pairs) / len(pairs), len(pairs)


def gate(
    n_long_coins: int, ics: Sequence[float], se_nw: float, *, min_coins: int = 12, max_mde: float = 0.025, k: float = 2.8
) -> dict[str, float | bool]:
    """Step-0 power gate: SE = max(SD / sqrt(n), Newey-West SE); MDE = k * SE; pass if coins >= min and MDE <= max."""
    n = len(ics)
    sd = statistics.stdev(ics) if n > 1 else float("nan")
    se = max(sd / math.sqrt(n), se_nw) if n > 1 else float("nan")
    mde = k * se
    return {"n_weeks": n, "sd": sd, "se": se, "mde": mde, "pass": bool(n_long_coins >= min_coins and mde <= max_mde)}
