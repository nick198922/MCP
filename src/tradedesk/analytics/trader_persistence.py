"""Persistence of trader P&L across periods (IDEA-20261015-smart-money-persistence, H1 for Polymarket).

Pure functions, no I/O. Inputs are point-in-time position states of the Polymarket v1 P&L subgraph
(`UserPosition`: amount, avgPrice, realizedPnl, totalBought; shares and USDC are integers scaled by 1e6) and
resolution payouts known at the same block. Money out is USDC (float); IC and percentiles are plain fractions.

- `settled_value`: realized P&L plus the settled value of open positions on markets already resolved at the block
  (losing tokens are rarely redeemed, so `realizedPnl` alone never sees those losses).
- `pair_metrics`: rank IC (Spearman) between P&L in period M and M+1, and Q = mean mid-rank percentile in M+1 of the
  top decile by P&L in M, minus 0.5 (0 under no persistence).
- `rank_placebo`: the same means with the M+1 P&L permuted across traders within each pair.
- `mean_ci`, `mde`: t interval over pairs (cluster = pair) and the 80 % power / 5 % two-sided detectable effect.
- `period_of`, `bad_period`: which period a boundary's delta rows belong to, and which periods are excluded
  (length out of range or overlapping a data gap, with a tolerance for boundaries a few seconds late).
- `describe_pair`, `open_cost`: descriptive numbers (dollar outcome of the top decile, share of open cost without
  a known payout); not part of the pre-registered verdict.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .dated_basis import t_quantile

SCALE = 1_000_000  # shares and USDC in the subgraph are integers x 1e6


@dataclass(frozen=True)
class PosState:
    """One `UserPosition` at a block (raw subgraph integers)."""

    amount: int
    avg_price: int
    realized: int
    total_bought: int


def settled_value(states: Mapping[str, PosState], payouts: Mapping[str, float]) -> tuple[float, float]:
    """(settled, realized) cumulative P&L of one trader in USDC at a block.

    `states`: token id -> position at the block; `payouts`: token id -> payout per share (0..1) of tokens whose market
    was resolved at or before that block. settled = realized + sum over open positions on resolved tokens of
    amount * (payout - avgPrice)."""
    realized = 0
    settled_extra = 0.0
    for token, s in states.items():
        realized += s.realized
        if s.amount > 0:
            p = payouts.get(token)
            if p is not None:
                settled_extra += (s.amount / SCALE) * (p - s.avg_price / SCALE)
    r = realized / SCALE
    return r + settled_extra, r


def tokens_bought(before: Mapping[str, PosState], after: Mapping[str, PosState]) -> set[str]:
    """Distinct tokens whose cumulative `totalBought` rose between two blocks (proxy for buy trades).

    `after` may hold only the positions that changed (a delta); tokens missing from `before` count from 0."""
    out = set()
    for token, s in after.items():
        prev = before.get(token)
        if s.total_bought > (prev.total_bought if prev else 0):
            out.add(token)
    return out


def open_cost(states: Mapping[str, PosState], payouts: Mapping[str, float]) -> tuple[float, float]:
    """(cost of all open positions, cost of open positions without a known payout) in USDC: amount * avgPrice
    summed over positions with amount > 0. The second part is the P&L the settled measure cannot see yet."""
    total = 0.0
    unpaid = 0.0
    for token, s in states.items():
        if s.amount > 0:
            c = (s.amount / SCALE) * (s.avg_price / SCALE)
            total += c
            if token not in payouts:
                unpaid += c
    return total, unpaid


def period_of(k: int, idx: Sequence[int]) -> int | None:
    """Period p with idx[p] < k <= idx[p+1]: rows fetched at boundary k are the changes inside period p
    (between boundaries idx[p] and idx[p+1]); None if k is not inside any period."""
    for p in range(len(idx) - 1):
        if idx[p] < k <= idx[p + 1]:
            return p
    return None


def bad_period(
    start_ts: int, end_ts: int, *, min_days: float, max_days: float, gap_from: int, gap_to: int, tolerance_s: int = 3600
) -> bool:
    """True if a period [start_ts, end_ts] (UTC seconds) is too short / too long or overlaps the data gap
    (gap_from, gap_to). Boundaries sit a few seconds after the nominal time (first fill at or after it), so the
    overlap test has a tolerance: a month ending at 00:00:03 of the gap's first day does not touch the gap."""
    days = (end_ts - start_ts) / 86400
    return not (min_days <= days <= max_days) or (start_ts < gap_to - tolerance_s and end_ts > gap_from + tolerance_s)


def period_pnl(values: Sequence[Mapping[str, float]]) -> list[dict[str, float]]:
    """P&L per period from cumulative values at boundaries 0..K: period k = value[k+1] - value[k] for every trader
    present at either boundary (absent = 0, i.e. no positions yet)."""
    out = []
    for a, b in zip(values, values[1:], strict=False):
        keys = set(a) | set(b)
        out.append({u: b.get(u, 0.0) - a.get(u, 0.0) for u in keys})
    return out


def ranks(x: Sequence[float]) -> np.ndarray:
    """Average ranks 1..n (ties share the mean rank)."""
    a = np.asarray(x, dtype=float)
    order = np.argsort(a, kind="mergesort")
    r = np.empty(len(a), dtype=float)
    sa = a[order]
    i = 0
    n = len(a)
    while i < n:
        j = i
        while j + 1 < n and sa[j + 1] == sa[i]:
            j += 1
        r[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Spearman rank correlation with average ranks; None when n < 3 or either side is constant."""
    if len(x) != len(y) or len(x) < 3:
        return None
    rx, ry = ranks(x), ranks(y)
    sx, sy = rx.std(), ry.std()
    if sx == 0 or sy == 0:
        return None
    return float(((rx - rx.mean()) * (ry - ry.mean())).mean() / (sx * sy))


def top_index(x: Sequence[float], top: float) -> np.ndarray:
    """Indices of the top `top` share by x (k = max(1, round(top * n))); ties broken by input order."""
    n = len(x)
    k = max(1, round(top * n))
    order = np.argsort(-np.asarray(x, dtype=float), kind="mergesort")
    return order[:k]


def q_stat(x: Sequence[float], y: Sequence[float], top: float = 0.1) -> float:
    """Mean mid-rank percentile ((rank - 0.5) / n) in y of the top `top` by x, minus 0.5."""
    n = len(y)
    pct = (ranks(y) - 0.5) / n
    return float(pct[top_index(x, top)].mean() - 0.5)


def stay_top(x: Sequence[float], y: Sequence[float], top: float = 0.1) -> float:
    """Share of the top `top` by x that is also in the top `top` by y."""
    a = set(top_index(x, top).tolist())
    b = set(top_index(y, top).tolist())
    return len(a & b) / len(a)


@dataclass(frozen=True)
class Pair:
    """One formation -> outcome pair: aligned arrays over eligible traders (sorted by trader id)."""

    label: str
    traders: tuple[str, ...]
    x: np.ndarray  # P&L in M (USDC)
    y: np.ndarray  # P&L in M+1 (USDC)


def make_pair(label: str, pnl_m: Mapping[str, float], pnl_next: Mapping[str, float], eligible: Iterable[str]) -> Pair:
    """Align eligible traders (sorted ids, so tie-breaks are deterministic); a trader absent in M+1 has P&L 0."""
    tr = tuple(sorted(set(eligible)))
    x = np.array([pnl_m.get(u, 0.0) for u in tr], dtype=float)
    y = np.array([pnl_next.get(u, 0.0) for u in tr], dtype=float)
    return Pair(label, tr, x, y)


def drop_top_abs(p: Pair, frac: float = 0.01) -> Pair:
    """The pair without the top `frac` of traders by |P&L in M| (k = max(1, round(frac * n)))."""
    k = max(1, round(frac * len(p.x)))
    drop = set(np.argsort(-np.abs(p.x), kind="mergesort")[:k].tolist())
    keep = [i for i in range(len(p.x)) if i not in drop]
    return Pair(p.label, tuple(p.traders[i] for i in keep), p.x[keep], p.y[keep])


@dataclass(frozen=True)
class PairMetrics:
    label: str
    n: int
    ic: float | None
    q: float
    stay: float


def pair_metrics(p: Pair, top: float = 0.1) -> PairMetrics:
    return PairMetrics(p.label, len(p.x), spearman(p.x, p.y), q_stat(p.x, p.y, top), stay_top(p.x, p.y, top))


def describe_pair(p: Pair, top: float = 0.1) -> dict[str, float | int]:
    """Descriptive (not pre-registered) numbers of one pair, in USDC and fractions:
    the top decile by P&L_M — mean / median P&L_{M+1}, share with P&L_{M+1} == 0; the rest — mean P&L_{M+1};
    Q among traders active in M+1 (P&L_{M+1} != 0, top decile re-ranked inside that subset);
    Q of the bottom decile by P&L_M (negative = losers stay low)."""
    idx = top_index(p.x, top)
    rest = np.ones(len(p.x), dtype=bool)
    rest[idx] = False
    act = p.y != 0
    n_act = int(act.sum())
    q_act = q_stat(p.x[act], p.y[act], top) if n_act >= 10 else float("nan")
    return {
        "n": len(p.x),
        "top_mean_next": float(p.y[idx].mean()),
        "top_median_next": float(np.median(p.y[idx])),
        "top_zero_share": float((p.y[idx] == 0).mean()),
        "rest_mean_next": float(p.y[rest].mean()) if rest.any() else float("nan"),
        "n_active_next": n_act,
        "q_active": q_act,
        "q_bottom": q_stat(-p.x, p.y, top),
    }


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    """Weighted mean (descriptive sensitivity, e.g. pairs weighted by sqrt(n))."""
    w = np.asarray(weights, dtype=float)
    return float((np.asarray(values, dtype=float) * w).sum() / w.sum())


def mean_ci(values: Sequence[float], level: float = 0.95) -> dict[str, float | int | None]:
    """Mean over periods with a two-sided t interval (df = T - 1); sd is the between-period sample SD."""
    v = [float(x) for x in values]
    t = len(v)
    if t == 0:
        return {"T": 0, "mean": None, "sd": None, "lo": None, "hi": None}
    m = statistics.fmean(v)
    if t < 2:
        return {"T": t, "mean": m, "sd": None, "lo": None, "hi": None}
    sd = statistics.stdev(v)
    h = t_quantile(0.5 + level / 2, t - 1) * sd / math.sqrt(t)
    return {"T": t, "mean": m, "sd": sd, "lo": m - h, "hi": m + h}


def mde(sd: float | None, t: int) -> float | None:
    """Minimum detectable mean effect, 5 % two-sided, 80 % power: 2.8 * sd / sqrt(T)."""
    if sd is None or t < 2:
        return None
    return 2.8 * sd / math.sqrt(t)


def rank_placebo(pairs: Sequence[Pair], n_perm: int = 2000, seed: int = 7, top: float = 0.1) -> dict[str, float]:
    """Permute the M+1 P&L across traders within each pair; one-sided p of the observed mean IC and mean Q
    (share of permutations with a mean >= observed, with the +1 correction) and the placebo means."""
    obs = [pair_metrics(p, top) for p in pairs]
    ics = [m.ic for m in obs if m.ic is not None]
    obs_ic = statistics.fmean(ics) if ics else float("nan")
    obs_q = statistics.fmean(m.q for m in obs)
    rng = np.random.default_rng(seed)
    # ranks once per pair: Spearman of permuted y equals Pearson of x-ranks with permuted y-ranks
    prepared = []
    for p in pairs:
        rx, ry = ranks(p.x), ranks(p.y)
        n = len(rx)
        idx = top_index(p.x, top)
        prepared.append((rx - rx.mean(), ry - ry.mean(), rx.std() * ry.std(), (ry - 0.5) / n, idx))
    null_ic = np.empty(n_perm)
    null_q = np.empty(n_perm)
    for b in range(n_perm):
        s_ic, n_ic, s_q = 0.0, 0, 0.0
        for cx, cy, den, pct, idx in prepared:
            perm = rng.permutation(len(cx))
            if den > 0:
                s_ic += float((cx * cy[perm]).mean() / den)
                n_ic += 1
            s_q += float(pct[perm][idx].mean() - 0.5)
        null_ic[b] = s_ic / n_ic if n_ic else float("nan")
        null_q[b] = s_q / len(prepared)
    return {
        "obs_ic": obs_ic,
        "obs_q": obs_q,
        "p_ic": float((1 + np.sum(null_ic >= obs_ic)) / (n_perm + 1)),
        "p_q": float((1 + np.sum(null_q >= obs_q)) / (n_perm + 1)),
        "null_ic_mean": float(np.nanmean(null_ic)),
        "null_q_mean": float(null_q.mean()),
        "null_ic_sd": float(np.nanstd(null_ic)),
    }


def power_gate(metrics: Sequence[PairMetrics], *, min_pairs: int, min_median_n: int, mde_ic: float, mde_q: float) -> dict[str, object]:
    """The pre-registered gate: enough pairs, enough traders per pair, and between-pair SDs small enough."""
    ics = [m.ic for m in metrics if m.ic is not None]
    qs = [m.q for m in metrics]
    t = len(metrics)
    med_n = statistics.median(m.n for m in metrics) if metrics else 0
    ci_ic, ci_q = mean_ci(ics), mean_ci(qs)
    m_ic, m_q = mde(ci_ic["sd"], len(ics)), mde(ci_q["sd"], t)  # type: ignore[arg-type]
    checks = {
        "pairs": t >= min_pairs,
        "median_n": med_n >= min_median_n,
        "mde_ic": m_ic is not None and m_ic <= mde_ic,
        "mde_q": m_q is not None and m_q <= mde_q,
    }
    return {"T": t, "median_n": med_n, "mde_ic": m_ic, "mde_q": m_q, "checks": checks, "passed": all(checks.values())}
