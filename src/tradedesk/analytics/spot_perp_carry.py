"""Same-venue cash-and-carry (long spot + short perp, equal coin quantity): pure path and block statistics.

No I/O. Time is data: every function takes timestamps (ms, UTC) and never reads a clock. Rates and returns are
decimal fractions of the position notional (0.01 = 1 %). Funding ``rate`` is the rate of one payment (Hyperliquid:
hourly, Binance: 8-hourly); a positive rate is paid by longs to shorts, so the short perp leg receives it.

Hedge price P&L per coin between two marks is ``dS - dF`` (long spot, short perp); it is expressed on the notional at
the earlier mark, ``(dS - dF) / S_prev``. Over the full holding it equals minus the change of the basis exactly only
when the spot price is constant (each step is normalised by its own ``S_prev``); otherwise it is an approximation.

Rebalancing: with equal coin quantities the delta is zero by construction, but the short leg's margin drifts with the
price. When the spot moved more than ``rebalance_band`` from the last rebalance price, part of the spot is sold (or
bought) and the short reduced (or increased) by the same coins to restore the margin split; the traded notional is
``|1 - P_ref / P|`` of the current notional, charged at ``rebalance_cost`` (all legs, one side each).
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from collections.abc import Sequence
from typing import Any

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
YEAR_MS = 365 * DAY_MS

# two-sided 95 % Student-t quantiles for df = 1..30
_T975 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
         2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]  # fmt: skip


def t975(df: int) -> float:
    if df < 1:
        return math.nan
    return _T975[df - 1] if df <= 30 else 1.96 + 2.4 / df


def coverage(ts: Sequence[int], start_ms: int, end_ms: int, step_ms: int) -> float:
    """Share of the ``step_ms`` slots in ``[start_ms, end_ms)`` holding at least one timestamp."""
    n = (end_ms - start_ms) // step_ms
    if n <= 0:
        return math.nan
    slots = {(t - start_ms) // step_ms for t in ts if start_ms <= t < end_ms}
    return len(slots) / n


def align_marks(spot: Sequence[tuple[int, float]], perp: Sequence[tuple[int, float]]) -> list[tuple[int, float, float]]:
    """Join spot and perp closes on identical timestamps: ``[(ts, spot, perp)]`` sorted by time."""
    p = dict(perp)
    return sorted((t, s, p[t]) for t, s in spot if t in p and s > 0 and p[t] > 0)


def book_cost(levels: Sequence[tuple[float, float]], mid: float, notional: float) -> float | None:
    """Cost of a market order of ``notional`` USD walking ``levels`` (best first): ``|VWAP / mid - 1|``.

    ``None`` if the book is too thin for the full notional."""
    if mid <= 0 or notional <= 0:
        return None
    left, coins, spent = notional, 0.0, 0.0
    for px, sz in levels:
        take = min(left, px * sz)
        coins += take / px
        spent += take
        left -= take
        if left <= 1e-9:
            return abs(spent / coins / mid - 1.0)
    return None


def book_slippage(bids: Sequence[tuple[float, float]], asks: Sequence[tuple[float, float]], notional: float) -> dict[str, Any]:
    """Half-spread and buy / sell / average cost (fractions of mid) for one order of ``notional`` USD per side."""
    if not bids or not asks:
        return {"half_spread": None, "buy": None, "sell": None, "avg": None}
    mid = (bids[0][0] + asks[0][0]) / 2.0
    buy, sell = book_cost(asks, mid, notional), book_cost(bids, mid, notional)
    avg = None if buy is None or sell is None else (buy + sell) / 2.0
    return {"half_spread": (asks[0][0] - bids[0][0]) / 2.0 / mid, "buy": buy, "sell": sell, "avg": avg}


def carry_steps(
    funding: Sequence[tuple[int, float]],
    marks: Sequence[tuple[int, float, float]],
    start_ms: int,
    end_ms: int,
    *,
    rebalance_band: float = 0.20,
    rebalance_cost: float = 0.0,
) -> list[dict[str, Any]]:
    """Per-interval P&L of an always-on long-spot / short-perp position held over ``[start_ms, end_ms]``.

    ``marks`` are ``(ts, spot, perp)`` sorted by time; the position opens at the first mark ``>= start_ms`` and
    closes at the last mark ``<= end_ms``. Step k covers ``(t_{k-1}, t_k]``: funding paid in that interval, hedge
    price P&L, and rebalancing cost decided at ``t_k`` from prices up to ``t_k`` only. Entry / exit costs are not
    included (see :func:`summarize`)."""
    ms = [m for m in marks if start_ms <= m[0] <= end_ms]
    if len(ms) < 2:
        return []
    f_ts = [t for t, _ in funding]
    f_cum = [0.0]
    for _, r in funding:
        f_cum.append(f_cum[-1] + r)
    out: list[dict[str, Any]] = []
    ref = ms[0][1]
    for (t0, s0, p0), (t1, s1, p1) in zip(ms, ms[1:], strict=False):
        i, j = bisect.bisect_right(f_ts, t0), bisect.bisect_right(f_ts, t1)
        fund = f_cum[j] - f_cum[i]
        basis = ((s1 - s0) - (p1 - p0)) / s0
        cost = 0.0
        move = abs(1.0 - ref / s1)
        if move > rebalance_band:
            cost = move * rebalance_cost
            ref = s1
        out.append({"ts": t1, "dt": t1 - t0, "funding": fund, "basis": basis, "cost": cost, "n_funding": j - i})
    return out


def blocks(steps: Sequence[dict[str, Any]], start_ms: int, block_ms: int, n_blocks: int) -> list[dict[str, Any]]:
    """Sum steps into ``n_blocks`` consecutive blocks of ``block_ms`` from ``start_ms`` (a step ending exactly on a
    boundary belongs to the earlier block)."""
    out = [{"i": i, "funding": 0.0, "basis": 0.0, "cost": 0.0, "dt": 0, "n_funding": 0} for i in range(n_blocks)]
    for s in steps:
        k = (s["ts"] - 1 - start_ms) // block_ms
        if 0 <= k < n_blocks:
            b = out[k]
            for key in ("funding", "basis", "cost", "dt", "n_funding"):
                b[key] += s[key]
    return out


def summarize(steps: Sequence[dict[str, Any]], start_ms: int, end_ms: int, n_blocks: int, round_trip_cost: float) -> dict[str, Any]:
    """Annualized totals and block statistics on notional.

    Block net = funding + basis - rebalancing - ``round_trip_cost / n_blocks`` (one entry and one exit amortized over
    the window), annualized by the time the block's steps cover. CI is the t-interval over block values."""
    block_ms = (end_ms - start_ms) // n_blocks
    bl = blocks(steps, start_ms, block_ms, n_blocks)
    vals = [(b["funding"] + b["basis"] - b["cost"] - round_trip_cost / n_blocks) * YEAR_MS / b["dt"] for b in bl if b["dt"] > 0]
    years = (end_ms - start_ms) / YEAR_MS
    fund = sum(s["funding"] for s in steps)
    basis = sum(s["basis"] for s in steps)
    rebal = sum(s["cost"] for s in steps)
    net = (fund + basis - rebal - round_trip_cost) / years
    cum, path = 0.0, []
    for s in steps:
        cum += s["basis"]
        path.append(cum)
    mean, ci = mean_ci(vals)
    return {
        "net_apr": net,
        "funding_apr": fund / years,
        "basis_apr": basis / years,
        "cost_apr": -(rebal + round_trip_cost) / years,
        "blocks": vals,
        "block_mean": mean,
        "ci": ci,
        "sd": statistics.stdev(vals) if len(vals) > 1 else math.nan,
        "basis_mdd": max_drawdown(path),
        "basis_min": min(path, default=0.0),
        "positive_blocks": sum(v > 0 for v in vals),
    }


def mean_ci(vals: Sequence[float]) -> tuple[float, tuple[float, float]]:
    n = len(vals)
    if n < 2:
        m = vals[0] if vals else math.nan
        return m, (math.nan, math.nan)
    m = statistics.fmean(vals)
    h = t975(n - 1) * statistics.stdev(vals) / math.sqrt(n)
    return m, (m - h, m + h)


def block_bootstrap_ci(vals: Sequence[float], block: int = 3, n_boot: int = 4000, seed: int = 7) -> tuple[float, float]:
    """95 % percentile CI of the mean by a circular moving-block bootstrap (keeps short autocorrelation)."""
    n = len(vals)
    if n < 2:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        xs: list[float] = []
        while len(xs) < n:
            i = rng.randrange(n)
            xs.extend(vals[(i + k) % n] for k in range(block))
        means.append(statistics.fmean(xs[:n]))
    means.sort()
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


def max_drawdown(path: Sequence[float]) -> float:
    """Largest peak-to-trough fall of a cumulative series that starts at 0 (positive number)."""
    peak, mdd = 0.0, 0.0
    for x in path:
        peak = max(peak, x)
        mdd = max(mdd, peak - x)
    return mdd


def drop_top(vals: Sequence[float], k: int) -> list[float]:
    return sorted(vals)[: max(0, len(vals) - k)]


def mde(sd: float, n: int) -> float:
    """Minimum detectable mean at 5 % two-sided, 80 % power: 2.8 * sd / sqrt(n)."""
    return 2.8 * sd / math.sqrt(n) if n > 0 else math.nan


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> dict[str, float]:
    """Cochran's Q heterogeneity of per-asset means with standard errors; p from the Wilson-Hilferty chi-square
    approximation."""
    w = [1.0 / (s * s) for s in ses if s > 0]
    m = [x for x, s in zip(means, ses, strict=True) if s > 0]
    k = len(m)
    if k < 2:
        return {"q": math.nan, "df": 0, "i2": math.nan, "p": math.nan}
    pooled = sum(wi * mi for wi, mi in zip(w, m, strict=True)) / sum(w)
    q = sum(wi * (mi - pooled) ** 2 for wi, mi in zip(w, m, strict=True))
    df = k - 1
    i2 = max(0.0, (q - df) / q) if q > 0 else 0.0
    z = ((q / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return {"q": q, "df": df, "i2": i2, "p": 0.5 * math.erfc(z / math.sqrt(2)), "pooled": pooled}
