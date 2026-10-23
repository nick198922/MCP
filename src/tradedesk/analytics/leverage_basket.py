"""Leveraged long alt-basket benchmark: equal-weight top-k by trailing turnover, cross margin, periodic rebalance.

A calibration benchmark, not a trading signal: "what track record can leverage times the market alone explain".
Pure functions (no I/O). Time is passed in as integer day indices into dense ``days x symbols`` matrices
(NaN = no bar that day). Returns are decimals on account capital (0.10 = 10 %), funding rates are decimals per
settlement summed per day (positive: longs pay shorts).
Idea card: docs/ideas/IDEA-20261023-alt-basket-leverage-benchmark.md.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Panel:
    """Dense daily panel: rows are consecutive UTC days, columns are symbols; NaN where the symbol has no bar.

    ``fund`` is the sum of the day's funding rates (NaN when no settlement was recorded)."""

    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    qv: np.ndarray
    fund: np.ndarray


def select_basket(
    close: np.ndarray, qv: np.ndarray, day: int, *, k: int, lookback: int, candidates: np.ndarray, group: np.ndarray
) -> list[int]:
    """Top-``k`` columns by turnover summed over days ``day - lookback .. day - 1`` (strictly before ``day``).

    A column qualifies when ``candidates`` is true and it has a bar on every one of those days (a continuous run:
    drops fresh listings and reused tickers). Columns sharing a ``group`` id (the same underlying asset under two
    tickers) contribute only the one with the larger turnover. Ties are broken by column index."""
    if day - lookback < 0:
        return []
    win_c = close[day - lookback : day]
    full = np.isfinite(win_c).all(axis=0) & candidates
    score = np.where(full, np.nansum(qv[day - lookback : day], axis=0), -np.inf)
    order = sorted((i for i in np.flatnonzero(full)), key=lambda i: (-score[i], i))
    out: list[int] = []
    seen: set[int] = set()
    for i in order:
        g = int(group[i])
        if g in seen:
            continue
        seen.add(g)
        out.append(int(i))
        if len(out) == k:
            break
    return out


@dataclass
class WindowResult:
    ret: float  # on account capital, after costs; -1.0 when liquidated
    max_dd: float  # 0..1; 1.0 when liquidated
    liquidated: bool
    liq_day: int | None  # offset from the window start
    n_delist: int  # legs closed because their bars stopped
    n_rebalance: int
    fees: float  # fees + slippage paid, in units of initial capital
    funding: float  # funding paid by the longs (negative = received), in units of initial capital
    funding_holes: int  # held leg-days without a recorded funding settlement (charged 0)
    equity: list[float] = field(default_factory=list)  # close equity per day
    contrib: dict[int, float] = field(default_factory=dict)  # P&L per column, units of initial capital


def simulate_window(
    p: Panel,
    start: int,
    length: int,
    basket_on: Callable[[int], Sequence[int]],
    *,
    leverage: float,
    rebalance_every: int,
    fee: float,
    slip: float,
    mm: float,
    mode: str,
    delist_gap: int = 3,
) -> WindowResult:
    """Long equal-weight basket under cross margin, one window of ``length`` days starting at day ``start``.

    On a rebalance day (offset 0, ``rebalance_every``, ...) every leg of ``basket_on(day)`` is set to
    ``leverage * equity / len(basket)`` at the day's open (the previous close when the open is missing); other legs
    are closed. Each day: funding on the notional at the last close, then the margin check
    (``mode='intraday'``: all legs at their daily low at once, an upper bound on risk; ``'close'``: at the close, a
    lower bound): liquidated when equity <= ``mm`` x gross notional, the window then returns -100 %. A leg without a
    bar for ``delist_gap`` days in a row is closed at its last close. ``fee`` and ``slip`` are charged on every
    traded notional, including the final close-out. Uses only rows ``<= start + length - 1`` (and the
    ``basket_on`` lookback)."""
    if mode not in ("intraday", "close"):
        raise ValueError(f"mode must be 'intraday' or 'close', got {mode!r}")
    cost_rate = fee + slip
    cash = 1.0
    q: dict[int, float] = {}
    last: dict[int, float] = {}
    miss: dict[int, int] = {}
    contrib: dict[int, float] = {}
    fees = funding = 0.0
    holes = n_delist = n_reb = 0
    peak, max_dd = 1.0, 0.0
    equity: list[float] = []

    def trade(i: int, new_q: float, px: float) -> None:
        nonlocal cash, fees
        dq = new_q - q.get(i, 0.0)
        c = cost_rate * abs(dq) * px
        cash -= dq * px + c
        fees += c
        contrib[i] = contrib.get(i, 0.0) - c
        if new_q == 0.0:
            q.pop(i, None)
            miss.pop(i, None)
        else:
            q[i] = new_q
            last[i] = px
            miss.setdefault(i, 0)

    for off in range(length):
        d = start + off
        if off % rebalance_every == 0:
            px_open: dict[int, float] = {}
            for i in q:
                o = p.open[d, i]
                px_open[i] = float(o) if math.isfinite(o) else last[i]
                contrib[i] = contrib.get(i, 0.0) + q[i] * (px_open[i] - last[i])
                last[i] = px_open[i]
            eq = cash + sum(q[i] * px_open[i] for i in q)
            basket = list(basket_on(d))
            targets: dict[int, tuple[float, float]] = {}
            for i in basket:
                o = p.open[d, i]
                pe = float(o) if math.isfinite(o) else float(p.close[d - 1, i])
                targets[i] = (leverage * eq / len(basket) / pe, pe)
            for i in list(q):
                if i not in targets:
                    trade(i, 0.0, px_open[i])
            for i, (tq, pe) in targets.items():
                if i in q:  # held leg: re-mark to the entry price used for the trade
                    contrib[i] = contrib.get(i, 0.0) + q[i] * (pe - last[i])
                    last[i] = pe
                trade(i, tq, pe)
            n_reb += 1
        for i in q:
            r = p.fund[d, i]
            if math.isfinite(r):
                c = q[i] * last[i] * float(r)
                cash -= c
                funding += c
                contrib[i] = contrib.get(i, 0.0) - c
            else:
                holes += 1
        lo_px = {i: (float(p.low[d, i]) if math.isfinite(p.low[d, i]) else last[i]) for i in q}
        cl_px = {i: (float(p.close[d, i]) if math.isfinite(p.close[d, i]) else last[i]) for i in q}
        g_lo = sum(q[i] * lo_px[i] for i in q)
        g_cl = sum(q[i] * cl_px[i] for i in q)
        e_lo, e_cl = cash + g_lo, cash + g_cl
        e_chk, g_chk = (e_lo, g_lo) if mode == "intraday" else (e_cl, g_cl)
        if e_chk <= mm * g_chk:
            for i in q:
                contrib[i] = contrib.get(i, 0.0) + q[i] * (cl_px[i] - last[i])
            return WindowResult(-1.0, 1.0, True, off, n_delist, n_reb, fees, funding, holes, equity, contrib)
        if mode == "intraday":
            max_dd = max(max_dd, 1.0 - e_lo / peak)
            peak = max(peak, e_cl)
        else:
            peak = max(peak, e_cl)
            max_dd = max(max_dd, 1.0 - e_cl / peak)
        for i in list(q):
            contrib[i] = contrib.get(i, 0.0) + q[i] * (cl_px[i] - last[i])
            last[i] = cl_px[i]
            miss[i] = 0 if math.isfinite(p.close[d, i]) else miss.get(i, 0) + 1
            if miss[i] >= delist_gap:
                trade(i, 0.0, last[i])
                n_delist += 1
        equity.append(cash + sum(q[i] * last[i] for i in q))
    for i in list(q):
        trade(i, 0.0, last[i])
    final = cash
    if final <= 0.0:
        return WindowResult(-1.0, 1.0, True, length - 1, n_delist, n_reb, fees, funding, holes, equity, contrib)
    max_dd = max(max_dd, 1.0 - final / peak)
    return WindowResult(final - 1.0, max_dd, False, None, n_delist, n_reb, fees, funding, holes, equity, contrib)


def block_bootstrap_by_month(
    values: Sequence[float],
    months: Sequence[int],
    stat: Callable[[np.ndarray], float],
    *,
    block: int = 8,
    n_boot: int = 5000,
    seed: int = 7,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile CI of ``stat`` by a circular block bootstrap over calendar months.

    ``months[j]`` is the month id of observation ``j`` (e.g. of a window start). Blocks of ``block`` consecutive
    months (wrapping around) are drawn until as many months as in the sample are collected; all observations of the
    drawn months enter the replicate. With overlapping windows the block should be at least the window length."""
    v = np.asarray(values, dtype=float)
    m = np.asarray(months)
    uniq = np.unique(m)
    n_m = len(uniq)
    idx_by = [np.flatnonzero(m == u) for u in uniq]
    rng = np.random.default_rng(seed)
    b = max(1, min(block, n_m))
    reps = np.empty(n_boot)
    n_blocks = -(-n_m // b)
    for r in range(n_boot):
        starts = rng.integers(0, n_m, size=n_blocks)
        ms = np.concatenate([(s + np.arange(b)) % n_m for s in starts])[:n_m]
        reps[r] = stat(v[np.concatenate([idx_by[j] for j in ms])])
    lo, hi = np.quantile(reps, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def share(x: np.ndarray) -> float:
    """Mean of a 0/1 array (a frequency)."""
    return float(np.mean(x)) if len(x) else float("nan")


def median(x: np.ndarray) -> float:
    return float(np.median(x)) if len(x) else float("nan")


def clopper_pearson(k: int, n: int, *, alpha: float = 0.05) -> tuple[float, float]:
    """Exact two-sided binomial CI for ``k`` successes in ``n`` independent trials (bisection on the binomial CDF)."""

    def cdf(x: int, p: float) -> float:
        return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(x + 1))

    def solve(f: Callable[[float], float], target: float) -> float:
        lo, hi = 0.0, 1.0  # f decreasing in p
        for _ in range(80):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) > target else (lo, mid)
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(lambda p: cdf(k - 1, p), 1 - alpha / 2)
    upper = 1.0 if k == n else solve(lambda p: cdf(k, p), alpha / 2)
    return lower, upper


def percentile_of(x: float, xs: Sequence[float]) -> float:
    """Share of ``xs`` strictly below ``x`` plus half the ties (mid-rank percentile, 0..1)."""
    a = np.asarray(xs, dtype=float)
    return float((np.sum(a < x) + 0.5 * np.sum(a == x)) / len(a)) if len(a) else float("nan")
