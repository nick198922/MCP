"""Pooled event study of carry A entries after a delisting announcement (item 8p), without A's slot limit.

A *unit* is one delisting-announcement episode of a symbol: its first publication in the period, with later
publications of the same symbol within ``merge_ms`` of the unit's start merged into it. Its entry window is
``[publication, min(perp end, publication + window_ms, data end)]``; the hypothetical entry is the first trade the
frozen A walk opens on that symbol alone inside the window. Controls are symbols with no announcement within
``horizon_ms`` of the publication, entered by the same rule in ``[t_u, t_u + match_ms]``. ``delta = unit P&L - mean
control P&L``. Pure functions, no I/O; P&L are fractions of a slot's notional.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

DAY_MS = 86_400_000


def announcement_units(
    index: Mapping[str, Sequence[int]],
    start_ms: int,
    end_ms: int,
    *,
    merge_ms: int = 30 * DAY_MS,
    window_ms: int = 30 * DAY_MS,
    perp_end: Mapping[str, int] | None = None,
    data_end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Units ``{symbol, pub, cluster, n_ann, win_end, has_window}`` sorted by ``pub`` from ``{symbol: publication
    times}``. Only publications in ``[start_ms, end_ms]`` count; a publication more than ``merge_ms`` after the start
    of the symbol's current unit starts a new one. A unit's fields depend only on publications at or before its
    ``pub + merge_ms`` (later announcements never change an earlier unit's ``pub`` or window)."""
    pe = perp_end or {}
    out: list[dict[str, Any]] = []
    for sym, ts in index.items():
        cur: dict[str, Any] | None = None
        for t in sorted(int(x) for x in ts if start_ms <= int(x) <= end_ms):
            if cur is not None and t - cur["pub"] <= merge_ms:
                cur["n_ann"] += 1
                continue
            hi = t + window_ms
            if sym in pe:
                hi = min(hi, int(pe[sym]))
            if data_end_ms is not None:
                hi = min(hi, data_end_ms)
            cur = {"symbol": sym, "pub": t, "cluster": t, "n_ann": 1, "win_end": hi, "has_window": hi >= t}
            out.append(cur)
    return sorted(out, key=lambda u: (u["pub"], u["symbol"]))


def control_symbols(
    index: Mapping[str, Sequence[int]], symbols: Iterable[str], unit_symbol: str, pub_ms: int, horizon_ms: int = 180 * DAY_MS
) -> list[str]:
    """Symbols other than ``unit_symbol`` with no announcement in ``[pub - horizon, pub + horizon]``."""
    lo, hi = pub_ms - horizon_ms, pub_ms + horizon_ms
    return sorted(s for s in symbols if s != unit_symbol and not any(lo <= int(t) <= hi for t in index.get(s) or ()))


def first_trade_in(trades: Iterable[Mapping[str, Any]], lo_ms: int, hi_ms: int) -> Mapping[str, Any] | None:
    """Earliest trade with ``lo <= open_ts <= hi`` (None if there is none)."""
    best = None
    for t in trades:
        if lo_ms <= int(t["open_ts"]) <= hi_ms and (best is None or t["open_ts"] < best["open_ts"]):
            best = t
    return best


def unit_delta(unit_pnl: float, control_pnls: Sequence[float]) -> float | None:
    """``unit_pnl - mean(control_pnls)``; None without controls."""
    if not control_pnls:
        return None
    return unit_pnl - sum(control_pnls) / len(control_pnls)


def mde80(sd: float, n: int) -> float | None:
    """Minimum detectable mean at 80 % power, two-sided 5 %: ``2.8 * sd / sqrt(n)`` (None for n < 1)."""
    return None if n < 1 else 2.8 * sd / math.sqrt(n)


def cluster_bootstrap_mean(
    values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261102
) -> dict[str, Any]:
    """Mean of ``values`` with a 95 % percentile CI from resampling whole clusters with replacement (the replicate
    mean is over all units of the drawn clusters). ``p_ge0`` = share of replicates with mean >= 0 (one-sided p
    for 'mean < 0')."""
    if len(values) != len(clusters):
        raise ValueError("values and clusters differ in length")
    n = len(values)
    if n == 0:
        return {"n": 0, "k": 0, "mean": None, "ci": None, "p_ge0": None}
    groups: dict[Any, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(float(v))
    sums = [(sum(g), len(g)) for g in groups.values()]
    k = len(sums)
    rng = random.Random(seed)
    reps = []
    for _ in range(n_boot):
        s = m = 0.0
        for _ in range(k):
            a, b = sums[rng.randrange(k)]
            s += a
            m += b
        reps.append(s / m)
    reps.sort()
    lo, hi = reps[int(0.025 * (n_boot - 1))], reps[int(math.ceil(0.975 * (n_boot - 1)))]
    return {"n": n, "k": k, "mean": sum(values) / n, "ci": [lo, hi], "p_ge0": sum(1 for r in reps if r >= 0) / n_boot}


def top_contributor(values: Sequence[float], keys: Sequence[Any]) -> Any:
    """Key whose summed value contributes most in the direction of the overall mean (most negative sum if the
    mean is < 0, else most positive); None for empty input."""
    if not values:
        return None
    tot: dict[Any, float] = {}
    for v, k in zip(values, keys, strict=True):
        tot[k] = tot.get(k, 0.0) + float(v)
    sign = -1.0 if sum(values) < 0 else 1.0
    return max(tot, key=lambda k: sign * tot[k])
