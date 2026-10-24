"""COIN-M (inverse) vs USDT-M (linear) perpetual funding on the same asset and venue: alignment, weekly
aggregation, block bootstrap, anchor decomposition and a two-leg carry simulation on equal capital.

Pure functions, no I/O. Rates are decimals per settlement (0.0001 = 0.01 % per 8 h); times are epoch ms (UTC).
Idea card: docs/ideas/IDEA-20261025-coinm-vs-usdtm-funding-carry.md.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

HOUR_MS = 3_600_000
BUCKET_MS = 8 * HOUR_MS
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS
MONDAY0_MS = 4 * DAY_MS  # 1970-01-05 00:00 UTC, a Monday
PER_YEAR_8H = 3 * 365  # 8-hour settlements per year
ANCHOR = 0.0001  # Binance baseline interest component, 0.01 % per 8 h


def round_hour(t_ms: int) -> int:
    """Settlement times carry a few ms of jitter (e.g. ...00005); round to the nearest hour."""
    return int(round(t_ms / HOUR_MS)) * HOUR_MS


def bucket_end(t_ms: int) -> int:
    """End (00/08/16 UTC) of the 8-hour bucket holding a settlement at ``t_ms``; a settlement at 08:00 ends its own bucket."""
    h = round_hour(t_ms)
    return -(-h // BUCKET_MS) * BUCKET_MS


def bucket_8h(rows: Sequence[tuple[int, float]]) -> dict[int, float]:
    """Rates per 8-hour bucket, kept only when the bucket holds exactly one settlement and it lies on 00/08/16 UTC.

    Buckets with two or more settlements (interval < 8 h) or an off-grid settlement are dropped, so an interval
    change never mixes a 4-hour rate with an 8-hour one.
    """
    seen: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for t, r in rows:
        seen[bucket_end(t)].append((round_hour(t), float(r)))
    out: dict[int, float] = {}
    for b, lst in seen.items():
        if len(lst) == 1 and lst[0][0] == b:
            out[b] = lst[0][1]
    return out


def dropped_buckets(rows: Sequence[tuple[int, float]]) -> int:
    """Number of buckets that ``bucket_8h`` drops (several settlements or off-grid)."""
    seen: dict[int, list[int]] = defaultdict(list)
    for t, _ in rows:
        seen[bucket_end(t)].append(round_hour(t))
    return sum(1 for b, lst in seen.items() if not (len(lst) == 1 and lst[0] == b))


def aligned(coinm: dict[int, float], usdtm: dict[int, float]) -> list[tuple[int, float, float]]:
    """(bucket_end, f_coinm, f_usdtm) for buckets valid on both venues, time-ordered."""
    return [(b, coinm[b], usdtm[b]) for b in sorted(coinm.keys() & usdtm.keys())]


def week_start(t_ms: int) -> int:
    """Monday 00:00 UTC of the week holding the settlement at ``t_ms`` (a bucket ending Monday 00:00 is the prior week)."""
    x = t_ms - 1  # the bucket ending at Monday 00:00 accrued on Sunday
    return x - (x - MONDAY0_MS) % WEEK_MS


@dataclass(frozen=True)
class Week:
    start: int
    mean: float  # mean d per 8 h over the valid buckets
    n: int


def weekly(rows: Sequence[tuple[int, float]], *, min_n: int = 15) -> list[Week]:
    """Weekly means of (t, value) rows; a week counts with >= ``min_n`` valid buckets (21 is a full week)."""
    acc: dict[int, list[float]] = defaultdict(list)
    for t, v in rows:
        acc[week_start(t)].append(v)
    return [Week(w, float(np.mean(v)), len(v)) for w, v in sorted(acc.items()) if len(v) >= min_n]


def ann(x: float) -> float:
    """Mean per 8 h -> annual decimal on notional."""
    return x * PER_YEAR_8H


def moving_block_bootstrap(x: Sequence[float], *, block: int = 4, n_boot: int = 5000, seed: int = 20261025) -> dict[str, float]:
    """Circular moving-block bootstrap of the mean: mean, se, percentile 95 % CI, MDE = 2.8 * se, n."""
    a = np.asarray(x, dtype=float)
    n = a.size
    if n < 2:
        return {
            "n": n,
            "mean": float(a.mean()) if n else float("nan"),
            "se": float("nan"),
            "lo": float("nan"),
            "hi": float("nan"),
            "mde": float("nan"),
        }
    b = max(1, min(block, n))
    k = math.ceil(n / b)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(n_boot, k))
    idx = (starts[:, :, None] + np.arange(b)[None, None, :]) % n
    means = a[idx.reshape(n_boot, -1)[:, :n]].mean(axis=1)
    se = float(means.std(ddof=1))
    lo, hi = np.quantile(means, [0.025, 0.975])
    return {"n": n, "mean": float(a.mean()), "se": se, "lo": float(lo), "hi": float(hi), "mde": 2.8 * se}


def block_sign_flip_p(x: Sequence[float], *, block: int = 4, n_perm: int = 5000, seed: int = 20261025) -> float:
    """Placebo: two-sided p of the mean against random sign flips of consecutive blocks (venue labels swapped)."""
    a = np.asarray(x, dtype=float)
    n = a.size
    if n < 2:
        return float("nan")
    g = np.arange(n) // max(1, block)
    rng = np.random.default_rng(seed)
    signs = rng.choice([-1.0, 1.0], size=(n_perm, int(g.max()) + 1))[:, g]
    null = np.abs((signs * a).mean(axis=1))
    return float((1 + np.sum(null >= abs(a.mean()) - 1e-15)) / (1 + n_perm))


def anchor_decomposition(
    rows: Sequence[tuple[int, float, float]], *, anchor: float = ANCHOR, tol: float = 1e-9
) -> dict[str, dict[str, float]]:
    """Split mean d = f_c - f_u into buckets where both / only COIN-M / only USDT-M / neither sit off the anchor.

    ``share``: fraction of buckets; ``contrib``: that group's contribution to the overall mean d (sums to mean d).
    """
    n = len(rows)
    groups: dict[str, list[float]] = {"both_anchor": [], "coinm_off": [], "usdtm_off": [], "both_off": []}
    for _, fc, fu in rows:
        ca, ua = abs(fc - anchor) <= tol, abs(fu - anchor) <= tol
        key = "both_anchor" if ca and ua else "coinm_off" if ua else "usdtm_off" if ca else "both_off"
        groups[key].append(fc - fu)
    return {k: {"share": len(v) / n if n else float("nan"), "contrib": sum(v) / n if n else float("nan")} for k, v in groups.items()}


# ----------------------------------------------------------------------------- H3: two legs on equal capital


@dataclass(frozen=True)
class Costs:
    spot: float = 0.0010  # spot taker, on traded value
    perp: float = 0.0005  # USDT-M / COIN-M taker
    slip: float = 0.0002  # per leg per trade
    liq_fee: float = 0.005  # liquidation clearance, on notional

    @property
    def leg_spot(self) -> float:
        return self.spot + self.slip

    @property
    def leg_perp(self) -> float:
        return self.perp + self.slip


@dataclass(frozen=True)
class Day:
    t: int  # day open, ms
    close_c: float  # COIN-M daily mark close
    close_u: float  # USDT-M daily mark close
    high_u: float  # USDT-M daily mark high
    f_c: tuple[float, ...]  # COIN-M settlements during the day
    f_u: tuple[float, ...]  # USDT-M settlements during the day


def carry_coinm(
    days: Sequence[Day], costs: Costs, *, hold_coins_to_week_end: bool = False, path: list[tuple[int, float, float]] | None = None
) -> dict[str, float]:
    """Strategy A on 1 USD of capital: buy coin on spot, deposit to COIN-M, short N = 1 USD of the inverse perp.

    The inverse short with coin collateral worth N is USD-neutral without rebalancing. Funding is paid in coin
    (f * N / mark) and converted to USD at the day's COIN-M mark (default) or held until the week's last day.
    Returns funding, costs and net in USD per 1 USD capital over the window (not annualised). If ``path`` is given,
    one ``(t, cumulative funding, cumulative costs)`` tuple per day is appended to it (for causality checks).
    """
    if not days:
        return {"funding": 0.0, "costs": 0.0, "net": 0.0, "days": 0}
    n = 1.0
    fund = 0.0
    held_coin = 0.0
    trade_cost = 2 * (costs.leg_spot + costs.leg_perp) * n
    for i, d in enumerate(days):
        coins = sum(d.f_c) * n / d.close_c
        if hold_coins_to_week_end:
            held_coin += coins
            last = i == len(days) - 1 or week_start(days[i + 1].t + 1) != week_start(d.t + 1)
            if last:
                fund += held_coin * d.close_c
                trade_cost += costs.leg_spot * abs(held_coin * d.close_c)
                held_coin = 0.0
        else:
            fund += coins * d.close_c
            trade_cost += costs.leg_spot * abs(coins * d.close_c)
        if path is not None:
            path.append((d.t, fund, trade_cost))
    return {"funding": fund, "costs": trade_cost, "net": fund - trade_cost, "days": len(days), "liquidations": 0}


def carry_usdtm(
    days: Sequence[Day],
    costs: Costs,
    *,
    margin: float = 0.0,
    band: float = 0.05,
    maint: float = 0.01,
    path: list[tuple[int, float, float, float, int]] | None = None,
) -> dict[str, float]:
    """Strategy B on 1 USD of capital: spot coin + short USDT-M on the same coin quantity.

    ``margin`` = 0: the coin itself is USDT-M collateral (Multi-Assets mode), capital = notional. ``margin`` > 0:
    separate spot plus ``margin`` x notional of USDT in the futures wallet (capital = (1 + margin) x notional);
    liquidation is checked daily against the USDT-M mark high (maintenance ``maint`` of notional), then a clearance
    fee is charged and both legs are re-entered. Weekly (Mondays) the notional is reset to equity / (1 + margin) if it
    drifted by more than ``band`` (relative). Funding = f * Q * mark (day close). Uses only data up to each day.
    If ``path`` is given, one ``(t, cumulative funding, cumulative costs, coin quantity after the day, liquidations)``
    tuple per day is appended to it (for causality checks).
    """
    if not days:
        return {"funding": 0.0, "costs": 0.0, "net": 0.0, "days": 0, "liquidations": 0}
    equity = 1.0
    target = 1.0 / (1.0 + margin)
    p_ref = days[0].close_u
    q = target / p_ref
    m_cash = margin * target  # USDT in the futures wallet (only for margin > 0)
    fund = 0.0
    cost = 2 * (costs.leg_spot + costs.leg_perp) * target  # entry now, exit at the end (approx at entry notional)
    liq = 0
    for i, d in enumerate(days):
        p = d.close_u
        if margin > 0 and i > 0:
            worst = m_cash - q * (d.high_u - p_ref)
            if worst <= maint * q * d.high_u:
                liq += 1
                # forced close at the high: lose the clearance fee, re-enter both legs at the close
                equity_now = q * p + m_cash - q * (p - p_ref)
                pen = costs.liq_fee * q * d.high_u + 2 * (costs.leg_spot + costs.leg_perp) * q * p
                cost += pen
                equity_now -= pen
                target = equity_now / (1.0 + margin)
                q, p_ref, m_cash = target / p, p, margin * target
        f = sum(d.f_u) * q * p
        fund += f
        m_cash += f
        # equity: spot q*p plus futures wallet cash plus unrealised short pnl (hedged: changes only by funding/costs)
        m_now = m_cash - q * (p - p_ref)
        equity = q * p + m_now
        monday = (d.t - MONDAY0_MS) % WEEK_MS == 0
        if monday and i > 0:
            new_target = equity / (1.0 + margin)
            if abs(q * p / new_target - 1.0) > band:
                traded = abs(q * p - new_target)
                c = (costs.leg_spot + costs.leg_perp) * traded
                cost += c
                equity -= c
                new_target = equity / (1.0 + margin)
                q, p_ref, m_cash = new_target / p, p, margin * new_target
        if path is not None:
            path.append((d.t, fund, cost, q, liq))
    return {"funding": fund, "costs": cost, "net": fund - cost, "days": len(days), "liquidations": liq}
