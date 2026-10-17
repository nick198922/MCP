"""Funding benchmark for criterion (v) of the forward paper carry (IDEA-20261017-forward-paper-carry). Pure: no I/O.

Per backtest trade: realized funding APR = funding collected (fraction of slot notional, as in ``carry_rules``) /
(days held / 365). Trades are bucketed by the trailing APR at entry (A: ``entry_apr``; B: ``entry_spread``); a bucket
with fewer than ``min_n`` trades is merged into its lower neighbour (the lowest bucket into its upper one). Each bucket
gets the empirical ``q_lo`` / ``q_hi`` percentiles (linear interpolation between order statistics, the "type 7"
estimator of R / numpy's default). A paper trade passes when its realized APR lies within its bucket's band.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

DAY_MS = 86_400_000
EDGES_A: tuple[float, ...] = (0.2, 0.3, 0.5, 1.0)
EDGES_B: tuple[float, ...] = (0.1, 0.3, 1.0, 3.0)


def realized_funding_apr(funding: float, open_ts: int, close_ts: int) -> float | None:
    """Funding (fraction of slot notional) annualised over the holding time; None for a zero/negative holding time."""
    days = (close_ts - open_ts) / DAY_MS
    if days <= 0:
        return None
    return funding / (days / 365.0)


def percentile(xs: Sequence[float], q: float) -> float:
    """Empirical percentile, linear interpolation between order statistics (numpy default, R type 7)."""
    if not xs:
        raise ValueError("percentile of an empty sample")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be within [0, 1]")
    s = sorted(xs)
    h = (len(s) - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (h - lo) * (s[hi] - s[lo])


def trade_samples(trades: Iterable[dict[str, Any]], entry_key: str, *, min_days: float = 0.0) -> list[dict[str, Any]]:
    """Backtest trades -> ``[{symbol, open_ts, close_ts, days, entry, realized_apr, open_at_end, stopped}]``.

    Keeps trades held at least ``min_days`` (the paper unit is a trade held >= 7 days); open-at-end trades are kept,
    as open paper trades marked at the checkpoint are.
    """
    out = []
    for t in trades:
        days = (int(t["close_ts"]) - int(t["open_ts"])) / DAY_MS
        apr = realized_funding_apr(float(t["funding"]), int(t["open_ts"]), int(t["close_ts"]))
        if apr is None or days < min_days:
            continue
        out.append(
            {
                "symbol": t["symbol"],
                "open_ts": int(t["open_ts"]),
                "close_ts": int(t["close_ts"]),
                "days": days,
                "entry": float(t[entry_key]),
                "realized_apr": apr,
                "open_at_end": bool(t.get("open_at_end")),
                "stopped": bool(t.get("stopped")),
            }
        )
    return out


def bucket_of(x: float, edges: Sequence[float]) -> int | None:
    """Index of the registered bucket ``[edges[i], edges[i+1])`` (last one open-ended); None below ``edges[0]``."""
    if x < edges[0]:
        return None
    k = 0
    for i, e in enumerate(edges):
        if x >= e:
            k = i
    return k


def merge_buckets(edges: Sequence[float], counts: Sequence[int], min_n: int) -> list[tuple[float, float | None]]:
    """Merge thin buckets: the highest bucket with < ``min_n`` goes into its lower neighbour (the lowest into its upper).

    Returns the final ``[(lo, hi or None for open-ended)]``. Stops when every bucket has ``min_n`` or one is left.
    """
    groups: list[list[Any]] = [[edges[i], edges[i + 1] if i + 1 < len(edges) else None, int(counts[i])] for i in range(len(edges))]
    while len(groups) > 1:
        thin = [i for i, g in enumerate(groups) if g[2] < min_n]
        if not thin:
            break
        i = thin[-1]
        j = i - 1 if i > 0 else 1
        lo, hi = min(i, j), max(i, j)
        groups[lo] = [groups[lo][0], groups[hi][1], groups[lo][2] + groups[hi][2]]
        del groups[hi]
    return [(g[0], g[1]) for g in groups]


def _in(x: float, lo: float, hi: float | None) -> bool:
    return x >= lo and (hi is None or x < hi)


def build_bench(
    samples: Sequence[dict[str, Any]],
    edges: Sequence[float],
    *,
    min_n: int = 10,
    q_lo: float = 0.05,
    q_hi: float = 0.95,
) -> dict[str, Any]:
    """Buckets with bands from samples ``{entry, realized_apr}`` (see ``trade_samples``)."""
    counts = [0] * len(edges)
    below = 0
    for s in samples:
        k = bucket_of(s["entry"], edges)
        if k is None:
            below += 1
        else:
            counts[k] += 1
    buckets = []
    for lo, hi in merge_buckets(edges, counts, min_n):
        xs = [s["realized_apr"] for s in samples if _in(s["entry"], lo, hi)]
        b: dict[str, Any] = {"entry_lo": lo, "entry_hi": hi, "n": len(xs), "thin": len(xs) < min_n}
        if xs:
            b.update(
                p05=percentile(xs, q_lo),
                p95=percentile(xs, q_hi),
                median=percentile(xs, 0.5),
                mean=sum(xs) / len(xs),
            )
        buckets.append(b)
    return {
        "registered_edges": list(edges),
        "registered_counts": counts,
        "below_lowest_edge": below,
        "min_n": min_n,
        "q_lo": q_lo,
        "q_hi": q_hi,
        "buckets": buckets,
    }


def ladder_summary(tiers: Iterable[dict[str, Any]], *, notional_usd: float, leverage: float) -> dict[str, Any]:
    """Risk-limit ladder of one symbol -> what criteria (g) and (ye) need for a leg of ``notional_usd`` at ``leverage``.

    ``tiers``: ``{limit_usd, mmr, max_leverage}`` (Bybit v5 ``riskLimitValue``, ``maintenanceMargin``, ``maxLeverage``).
    ``mmr``: maintenance margin rate of the lowest tier whose limit covers the notional (None if none does);
    ``max_position_usd``: largest tier limit that still allows ``leverage``; ``fits``: notional <= that limit.
    """
    ts = sorted(
        ({"limit_usd": float(t["limit_usd"]), "mmr": float(t["mmr"]), "max_leverage": float(t["max_leverage"])} for t in tiers),
        key=lambda t: t["limit_usd"],
    )
    covering = [t for t in ts if t["limit_usd"] >= notional_usd]
    allowed = [t["limit_usd"] for t in ts if t["max_leverage"] >= leverage]
    max_pos = max(allowed) if allowed else None
    return {
        "mmr": covering[0]["mmr"] if covering else None,
        "max_position_usd": max_pos,
        "fits": max_pos is not None and notional_usd <= max_pos,
        "n_tiers": len(ts),
    }


def band_check(bench: dict[str, Any], entry: float, realized_apr: float) -> dict[str, Any]:
    """Where a paper trade lands: its bucket (by entry APR) and whether its realized APR lies within [p05, p95]."""
    for b in bench["buckets"]:
        if _in(entry, b["entry_lo"], b["entry_hi"]) and "p05" in b:
            return {
                "bucket": [b["entry_lo"], b["entry_hi"]],
                "p05": b["p05"],
                "p95": b["p95"],
                "in_band": b["p05"] <= realized_apr <= b["p95"],
            }
    return {"bucket": None, "in_band": None}
