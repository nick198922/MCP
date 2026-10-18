"""VRP on real Deribit marks: a delta-hedged short ATM straddle priced from trade-print marks.

Pure functions, no I/O (IDEA-20261017-vrp-real-marks, step 1). Builds on the daily surfaces of
`put_skew.build_day` (marks inverted from Deribit ``mark_price`` of trade prints, 09:00 UTC).

Units and conventions (same as `put_skew`):
  * vols are decimals (0.45 = 45 %); T = (expiry 08:00 UTC - observation) / 365 days;
  * P&L is USD-linear per 1 BTC of each option, as a fraction of the entry spot (0.01 = 1 % of spot);
    it is a return on notional, not on margin capital;
  * the hedge is a perp tracked by the Deribit index; funding (decimal per hour, longs pay when
    positive) is charged on it when a funding series is passed.

The hedge path is built separately from the book (`hedge_path`) so the same trade can be hedged with
daily marks or with another vol source (e.g. DVOL) for the decomposition of the marks-vs-DVOL gap.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from typing import Any

from .options import black76_greeks
from .put_skew import DAY_MS, FundingIndex, choose_expiry, inverse_price, iv_at_strike, year_frac

# (t_ms, spot, forward/spot ratio or None when the expiry has no prints that day, [iv or None per leg])
HedgePoint = tuple[int, float, float | None, list[float | None]]


def plan_straddle(
    snap: dict[str, Any],
    *,
    target_days: float = 30,
    dte_band: tuple[float, float] = (21, 56),
    expiry_rule: str = "nearest",
    call_delta_band: tuple[float, float] = (0.40, 0.60),
) -> dict[str, Any] | None:
    """Entry plan of a short ATM straddle from one day's surface.

    Expiry: `choose_expiry` (``nearest`` = the listed expiry closest to ``target_days`` with DTE in
    ``dte_band``). ATM strike: the strike with a fresh mark closest to the forward (ties: lower
    strike). ``covered`` = its call delta at the mark is in ``call_delta_band``.
    """
    e = choose_expiry(snap, target_days, *dte_band, rule=expiry_rule)
    if e is None:
        return None
    x = snap["expiries"][e]
    st = x["strikes"]
    if not st:
        return None
    k = min(st, key=lambda K: (abs(K - x["F"]), K))
    s = st[k]
    call_delta = 1.0 + s["put_delta"]
    return {
        "day": snap["day"],
        "expiry": e,
        "dte": x["T"] * 365,
        "spot": snap["spot"],
        "F": x["F"],
        "T": x["T"],
        "ratio": x["ratio"],
        "k": k,
        "mark_iv": s["mark_iv"],
        "mark_age_h": s.get("mark_age_h"),
        "sell_iv": s.get("sell_iv"),
        "buy_iv": s.get("buy_iv"),
        "call_delta": call_delta,
        "covered": call_delta_band[0] <= call_delta <= call_delta_band[1],
    }


def hedge_path(
    days: dict[int, dict[str, Any]],
    entry_day: int,
    expiry_ms: int,
    strikes: Sequence[float],
    *,
    vol_by_day: dict[int, float] | None = None,
) -> list[HedgePoint]:
    """Daily hedge points from ``entry_day`` up to (not including) the expiry.

    Reads only surfaces of days ``d`` with ``d + 1 day <= expiry`` and reference time before the expiry,
    so data after the expiry never enters. With ``vol_by_day=None`` the leg vols are the day's marks at
    each strike (own strike, else interpolated); otherwise every leg uses ``vol_by_day[d]`` (None if
    missing). Missing days are skipped (the book keeps the previous hedge).
    """
    out: list[HedgePoint] = []
    d = entry_day
    while d + DAY_MS <= expiry_ms:
        snap = days.get(d)
        if snap is not None and snap["t_ms"] < expiry_ms:
            x = snap["expiries"].get(expiry_ms)
            ratio = x["ratio"] if x is not None else None
            if vol_by_day is None:
                ivs: list[float | None] = [iv_at_strike(x, K)[0] if x is not None else None for K in strikes]
            else:
                v = vol_by_day.get(d)
                ivs = [v for _ in strikes]
            out.append((int(snap["t_ms"]), float(snap["spot"]), ratio, ivs))
        d += DAY_MS
    return out


def hedged_book(
    entry: dict[str, Any],
    legs: Sequence[tuple[float, bool, float, float]],
    path: Sequence[HedgePoint],
    expiry_ms: int,
    delivery_price: float,
    *,
    funding: Sequence[tuple[int, float]] | FundingIndex = (),
    option_fee: float = 0.0003,
    delivery_fee: float = 0.00015,
    fee_cap: float = 0.125,
    hedge_fee: float = 0.0005,
    hedge: bool = True,
) -> dict[str, Any]:
    """USD-linear P&L of an option book held to expiry with a perp delta hedge at each path point.

    ``entry`` = {"day", "spot", "F", "T", "ratio"} of the entry surface; ``legs`` = [(strike, is_call,
    qty, exec_iv)], qty < 0 = sold, executed at ``exec_iv`` on the entry forward. At each path point the
    hedge is reset to minus the book delta at that point's vols (a missing vol keeps the leg's previous
    vol, a missing ratio the previous ratio). Legs and hedge settle at ``delivery_price``.

    Besides the final P&L, returns ``mtm_min``: the worst mark-to-market of the whole position over the
    path (legs valued at the path vols, before exit costs), a fraction of entry spot.
    Same numbers as `put_skew.run_book` when ``path`` comes from `hedge_path` with marks.
    """
    fidx = funding if isinstance(funding, FundingIndex) else FundingIndex(funding)
    s0, f0, t0 = float(entry["spot"]), float(entry["F"]), float(entry["T"])
    premium, fees, vega0 = 0.0, 0.0, 0.0
    for K, is_call, q, iv in legs:
        px_usd = inverse_price(f0, K, t0, iv, is_call) * s0
        premium += -q * px_usd
        fees += abs(q) * min(option_fee * s0, fee_cap * px_usd)
        vega0 += q * black76_greeks(f0, K, t0, iv, is_call)["vega"]
    last_iv = [iv for _, _, _, iv in legs]
    last_ratio = float(entry["ratio"])
    pos, hedge_pnl, fund_pnl, hedge_cost = 0.0, 0.0, 0.0, 0.0
    stale, n_hedge = 0, 0
    prev: tuple[int, float, float] | None = None  # (t_ms, spot, position)
    mtm: list[float] = []
    for t_ms, s, ratio, ivs in path:
        if t_ms >= expiry_ms:
            break
        if ratio is not None:
            last_ratio = ratio
        F = last_ratio * s
        T = year_frac(expiry_ms, t_ms)
        delta, value = 0.0, 0.0
        for i, (K, is_call, q, _) in enumerate(legs):
            v = ivs[i] if i < len(ivs) else None
            if v is None:
                stale += 1
                v = last_iv[i]
            last_iv[i] = v
            delta += q * black76_greeks(F, K, T, v, is_call)["delta"]
            value += q * inverse_price(F, K, T, v, is_call) * s
        target = -delta if hedge else 0.0
        hedge_cost += hedge_fee * abs(target - pos) * s
        if prev is not None:
            pt, ps, pp = prev
            hedge_pnl += pp * (s - ps)
            fund_pnl -= pp * ps * fidx.sum(pt, t_ms)
        pos = target
        prev = (t_ms, s, pos)
        n_hedge += 1
        mtm.append(premium + value + hedge_pnl + fund_pnl - hedge_cost - fees)
    if prev is not None:
        pt, ps, pp = prev
        hedge_pnl += pp * (delivery_price - ps)
        fund_pnl -= pp * ps * fidx.sum(pt, expiry_ms)
        hedge_cost += hedge_fee * abs(pp) * delivery_price
    payoff = 0.0
    for K, is_call, q, _ in legs:
        intrinsic = max(delivery_price - K, 0.0) if is_call else max(K - delivery_price, 0.0)
        payoff += q * intrinsic
        if intrinsic > 0:
            fees += abs(q) * min(delivery_fee * delivery_price, fee_cap * intrinsic)
    total = premium + payoff + hedge_pnl + fund_pnl - hedge_cost - fees
    mtm.append(total)
    return {
        "day": entry["day"],
        "expiry": expiry_ms,
        "spot": s0,
        "pnl": total / s0,
        "premium": premium / s0,
        "payoff": payoff / s0,
        "hedge_pnl": hedge_pnl / s0,
        "funding_pnl": fund_pnl / s0,
        "costs": (hedge_cost + fees) / s0,
        "vega": vega0,
        "move": delivery_price / s0 - 1.0,
        "mtm_min": min(mtm) / s0,
        "stale_leg_days": stale,
        "hedge_days": n_hedge,
    }


def straddle_legs(k: float, exec_iv: float, qty: float = -1.0) -> list[tuple[float, bool, float, float]]:
    return [(k, True, qty, exec_iv), (k, False, qty, exec_iv)]


# ----------------------------------------------------------------------------- statistics


def block_bootstrap_means(values: Sequence[float], *, block: int = 3, n_boot: int = 10_000, seed: int = 20261018) -> list[float]:
    """Sorted means of moving-block bootstrap resamples of a time-ordered series (block length in items)."""
    n = len(values)
    if n == 0:
        return []
    b = max(1, min(block, n))
    rng = random.Random(seed)
    out = []
    for _ in range(n_boot):
        sample: list[float] = []
        while len(sample) < n:
            i = rng.randrange(0, n - b + 1)
            sample.extend(values[i : i + b])
        out.append(sum(sample[:n]) / n)
    out.sort()
    return out


def bootstrap_summary(values: Sequence[float], *, block: int = 3, n_boot: int = 10_000, seed: int = 20261018) -> dict[str, Any]:
    """Mean, 95 % percentile CI and two-sided p-value (2 x the smaller tail share beyond 0) of the mean."""
    n = len(values)
    if n < 2:
        return {"n": n, "mean": values[0] if n else None, "ci": None, "p": None}
    bs = block_bootstrap_means(values, block=block, n_boot=n_boot, seed=seed)
    m = len(bs)
    lo, hi = bs[int(0.025 * m)], bs[int(0.975 * m) - 1]
    below = sum(1 for v in bs if v <= 0) / m
    above = sum(1 for v in bs if v >= 0) / m
    return {"n": n, "mean": sum(values) / n, "ci": (lo, hi), "p": min(1.0, 2 * min(below, above))}


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        adj[i] = running
    return adj


def mde(sd: float, n: int, *, alpha: float = 0.05, power: float = 0.80) -> float:
    """Minimum detectable mean (two-sided test at ``alpha`` with ``power``) for n i.i.d. draws of SD ``sd``."""
    if n <= 0:
        return math.inf
    nd = statistics.NormalDist()
    return (nd.inv_cdf(1 - alpha / 2) + nd.inv_cdf(power)) * sd / math.sqrt(n)


def select_signal_days(entries: dict[int, dict[str, Any]], pct: dict[int, float], threshold: float) -> dict[int, dict[str, Any]]:
    """Entry days whose trailing percentile (known at entry) is >= ``threshold``."""
    return {d: e for d, e in entries.items() if d in pct and pct[d] >= threshold}


def classify_primary(
    mean: float, ci: tuple[float, float], p_adj: float, worst: float, *, min_effect: float = 0.005, ruin: float = -0.20
) -> str:
    """Pre-registered step-1 verdict for the unconditional short straddle.

    confirmed: CI lower > 0, Holm-adjusted p < 0.05, mean >= ``min_effect`` and the worst trade over all
    start days not below ``ruin``; rejected: CI upper < ``min_effect``, or the ruin gate fails on an
    otherwise positive result; else inconclusive.
    """
    if ci[1] < min_effect:
        return "rejected"
    if ci[0] > 0 and p_adj < 0.05 and mean >= min_effect:
        return "confirmed" if worst >= ruin else "rejected"
    return "inconclusive"


def classify_secondary(
    mean: float,
    ci: tuple[float, float],
    p_adj: float,
    worst: float,
    mde_value: float,
    *,
    mde_cap: float = 0.02,
    kill_upper: float = 0.01,
    ruin: float = -0.20,
) -> str:
    """Pre-registered verdict for the high-DVOL-quintile hypothesis (critic's triage 2026-10-18).

    The ruin gate (worst trade below ``ruin``) rejects the rule whatever the mean; then, if the MDE
    registered before the run exceeds ``mde_cap`` the result is descriptive only (``descriptive``, ◯);
    otherwise confirmed (Holm p < 0.05 and CI lower > 0), rejected (CI upper < ``kill_upper``) or
    inconclusive.
    """
    if worst < ruin:
        return "rejected"
    if mde_value > mde_cap:
        return "descriptive"
    if ci[0] > 0 and p_adj < 0.05:
        return "confirmed"
    if ci[1] < kill_upper:
        return "rejected"
    return "inconclusive"
