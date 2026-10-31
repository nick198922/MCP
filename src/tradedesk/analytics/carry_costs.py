"""Carry execution-cost calibration from recorded paper leg sides (pure: no I/O, time is a parameter).

A recorded leg side (``collector/carry_paper_engine.py`` -> ``cp_log`` kind ``side``) carries the order book walked
on the leg notional. Its cost, a fraction of leg notional, decomposes exactly into

    fee + half_spread + impact  (= the registered "actual cost": fee + |VWAP - mid| / mid)

plus a signed ``timing`` term: the adverse gap between the model's fill price (a 4h bar close, used by the backtest
and the paper engine) and the book mid ("+" hurts). ``CostModel`` turns per-leg-type slippage (half spread +
impact) into the single ``slippage`` parameter of ``simulate_carry_portfolio`` / ``simulate_perp_spread_portfolio``.
Rates are decimals (0.0005 = 0.05 %). Risk helpers work on an equity curve of ``(ts_ms, value)``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .orderbook import walk_book

WEEK_MS = 7 * 86_400_000
# Monday 1970-01-05 00:00 UTC: calendar weeks (Mon-Sun UTC) start at multiples of WEEK_MS from here.
MONDAY_EPOCH_MS = 4 * 86_400_000
TIMING_SANITY = 0.10  # |timing| above 10 % = unit mismatch (contract multiplier, wrong reference), excluded from timing


def leg_type(venue: str, leg: str) -> str:
    """Leg type used for calibration: 'spot_binance', 'perp_binance' or 'perp_bybit'."""
    if leg == "spot":
        return f"spot_{venue}"
    v = "binance" if venue.startswith("binance") else venue
    return f"perp_{v}"


def decompose_side(side: dict[str, Any], *, notional_usd: float | None = None) -> dict[str, Any] | None:
    """Split one recorded leg side into fee, half spread, depth impact and timing (fractions of leg notional).

    ``side`` is a ``cp_log`` side payload with ``book`` ({bids, asks} levels), ``side`` ('buy' | 'sell'), ``fee``,
    ``model_cost`` and ``model_price``. With ``notional_usd`` the book is re-walked on that notional (capacity check).
    Returns None when the book is missing or empty.
    """
    book = side.get("book") or {}
    bids = [[float(x[0]), float(x[1])] for x in book.get("bids") or []]
    asks = [[float(x[0]), float(x[1])] for x in book.get("asks") or []]
    if not bids or not asks:
        return None
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2.0
    n = float(notional_usd if notional_usd is not None else side.get("notional_usd") or 0.0)
    buy = side["side"] == "buy"
    w = walk_book(asks if buy else bids, n, mid, "buy" if buy else "sell")
    vwap = w["avg_price"]
    if vwap is None or mid <= 0:
        return None
    half = (ask - bid) / 2.0 / mid
    slip = abs(vwap - mid) / mid
    fee = float(side["fee"])
    mp = side.get("model_price")
    timing = None
    if mp:
        t = (mid - mp) / mp if buy else (mp - mid) / mp
        timing = t if abs(t) <= TIMING_SANITY else None
    return {
        "leg_type": leg_type(str(side.get("venue")), str(side.get("leg"))),
        "event": side.get("event"),
        "fee": fee,
        "half_spread": half,
        "impact": slip - half,
        "slippage": slip,
        "actual_cost": fee + slip,
        "model_cost": float(side.get("model_cost") if side.get("model_cost") is not None else fee + 0.0005),
        "timing": timing,
        "unfilled_usd": w["unfilled_usd"],
        "depth_short": w["unfilled_usd"] > 1e-6,
    }


def cluster_bootstrap_mean(
    values: Sequence[float], clusters: Sequence[str], *, n_boot: int = 10_000, seed: int = 20261031, alpha: float = 0.05
) -> dict[str, Any]:
    """Mean of ``values`` with a percentile CI from resampling whole clusters with replacement.

    Each bootstrap draw takes as many clusters as there are, and the mean is over the pooled observations of the
    drawn clusters (weights = cluster sizes). Returns n, number of clusters, mean, lo, hi (None when empty).
    """
    if not values:
        return {"n": 0, "clusters": 0, "mean": None, "lo": None, "hi": None}
    groups: dict[str, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(float(v))
    keys = sorted(groups)
    sums = [math.fsum(groups[k]) for k in keys]
    counts = [len(groups[k]) for k in keys]
    mean = math.fsum(sums) / sum(counts)
    rng = random.Random(seed)
    k = len(keys)
    boots = []
    for _ in range(n_boot):
        idx = [rng.randrange(k) for _ in range(k)]
        boots.append(math.fsum(sums[i] for i in idx) / sum(counts[i] for i in idx))
    boots.sort()
    return {"n": len(values), "clusters": k, "mean": mean, "lo": _pct(boots, alpha / 2), "hi": _pct(boots, 1 - alpha / 2)}


def _pct(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (numpy default) of a sorted sequence."""
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> dict[str, Any]:
    """Cochran's Q and I^2 of group means with standard errors (groups with se <= 0 are skipped)."""
    pts = [(m, s) for m, s in zip(means, ses, strict=True) if s and s > 0]
    if len(pts) < 2:
        return {"k": len(pts), "q": None, "df": None, "i2": None}
    w = [1.0 / s**2 for _, s in pts]
    pooled = math.fsum(wi * m for wi, (m, _) in zip(w, pts, strict=True)) / math.fsum(w)
    q = math.fsum(wi * (m - pooled) ** 2 for wi, (m, _) in zip(w, pts, strict=True))
    df = len(pts) - 1
    return {"k": len(pts), "q": q, "df": df, "i2": max(0.0, (q - df) / q) if q > 0 else 0.0, "pooled": pooled}


@dataclass(frozen=True)
class CostModel:
    """Per-leg-type taker fee and slippage (fractions of leg notional per executed side)."""

    fees: dict[str, float]
    slippage: dict[str, float]
    timing: float = 0.0  # extra cost per trade side (both legs together), when registered as significant

    def side_cost(self, legs: Iterable[str]) -> float:
        """Cost of one side (open or close) of a position whose legs are ``legs`` (leg types)."""
        return math.fsum(self.fees[x] + self.slippage[x] for x in legs) + self.timing

    def sim_slippage(self, legs: Sequence[str]) -> float:
        """The simulators' single ``slippage`` (applied to each of two legs) that gives the same side cost."""
        return (math.fsum(self.slippage[x] for x in legs) + self.timing) / len(legs)


OLD_MODEL = CostModel(
    fees={"spot_binance": 0.001, "perp_binance": 0.0005, "perp_bybit": 0.00055},
    slippage={"spot_binance": 0.0005, "perp_binance": 0.0005, "perp_bybit": 0.0005},
)


def calibrate(
    sides: Sequence[dict[str, Any]], *, as_of_ms: int, min_sides: int = 20, min_clusters: int = 8, n_boot: int = 10_000
) -> dict[str, Any]:
    """Per-leg-type slippage (half spread + impact) from decomposed sides recorded at or before ``as_of_ms``.

    Each item of ``sides``: ``{"cycle_ts", "symbol", "trade_id", "leg_type", "slippage", "timing", ...}``. Returns per
    leg type the clustered (by symbol) mean and 95 % CI, whether the sample meets the minimum, the max, and the
    registered main / stress slippage: main = mean; stress = CI upper bound, or the max when below the minimum.
    """
    use = [s for s in sides if s["cycle_ts"] <= as_of_ms]
    out: dict[str, Any] = {}
    for lt in sorted({s["leg_type"] for s in use}):
        xs = [s for s in use if s["leg_type"] == lt]
        ci = cluster_bootstrap_mean([s["slippage"] for s in xs], [s["symbol"] for s in xs], n_boot=n_boot)
        enough = ci["n"] >= min_sides and ci["clusters"] >= min_clusters
        mx = max(s["slippage"] for s in xs)
        out[lt] = {**ci, "enough": enough, "max": mx, "main": ci["mean"], "stress": ci["hi"] if enough else mx}
    return out


def week_start(ts_ms: int) -> int:
    """Start (Monday 00:00 UTC, ms) of the calendar week containing ``ts_ms``."""
    return (ts_ms - MONDAY_EPOCH_MS) // WEEK_MS * WEEK_MS + MONDAY_EPOCH_MS


def weekly_changes(equity: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """Change of the equity curve over each calendar week (Mon-Sun UTC): last value of the week minus the last value
    before it (the first week starts from 0, the curve's implicit start). Only points at or before each week's end
    are used, so a week's change never depends on later data."""
    out: list[tuple[int, float]] = []
    prev = 0.0
    cur_w: int | None = None
    last = 0.0
    for ts, v in equity:
        w = week_start(ts)
        if cur_w is not None and w != cur_w:
            out.append((cur_w, last - prev))
            prev = last
        cur_w, last = w, v
    if cur_w is not None:
        out.append((cur_w, last - prev))
    return out


def max_drawdown(equity: Sequence[tuple[int, float]]) -> float:
    """Largest peak-to-trough fall of the curve (same units as the curve; the start value 0 counts as a peak)."""
    peak, dd = 0.0, 0.0
    for _, v in equity:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd


def block_bootstrap_apr(
    weekly: Sequence[float], *, block: int = 4, n_boot: int = 10_000, seed: int = 20261031, alpha: float = 0.05
) -> dict[str, Any]:
    """Annualised mean of weekly changes with a moving-block bootstrap CI (blocks of ``block`` weeks)."""
    n = len(weekly)
    if n == 0:
        return {"weeks": 0, "apr": None, "lo": None, "hi": None}
    apr = math.fsum(weekly) / n * 52.1775
    b = max(1, min(block, n))
    starts = n - b + 1
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        draw: list[float] = []
        while len(draw) < n:
            i = rng.randrange(starts)
            draw.extend(weekly[i : i + b])
        boots.append(math.fsum(draw[:n]) / n * 52.1775)
    boots.sort()
    return {"weeks": n, "apr": apr, "lo": _pct(boots, alpha / 2), "hi": _pct(boots, 1 - alpha / 2)}


def risk_summary(equity: Sequence[tuple[int, float]], years: float, capital_factor: float, **boot: Any) -> dict[str, Any]:
    """APR, max drawdown, worst calendar week and the APR CI, on notional and on capital (curve / capital_factor).

    ``equity`` is the simulators' curve: fractions of portfolio notional, realised + open marked."""
    total = equity[-1][1] if equity else 0.0
    wk = weekly_changes(equity)
    ci = block_bootstrap_apr([v for _, v in wk], **boot)
    worst = min(wk, key=lambda x: x[1]) if wk else (None, 0.0)
    c = capital_factor
    return {
        "apr_on_notional": total / years if years > 0 else None,
        "apr_on_capital": total / years / c if years > 0 else None,
        "max_dd_on_capital": max_drawdown(equity) / c,
        "worst_week_on_capital": worst[1] / c,
        "worst_week_start_ms": worst[0],
        "weeks": len(wk),
        "apr_on_capital_ci": [None if ci["lo"] is None else ci["lo"] / c, None if ci["hi"] is None else ci["hi"] / c],
        "apr_on_capital_weekly_mean": None if ci["apr"] is None else ci["apr"] / c,
    }
