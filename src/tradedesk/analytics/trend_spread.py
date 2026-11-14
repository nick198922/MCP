"""Playbook rule P3 on history: 200-day trend -> vertical debit spread (IDEA-20261114-options-playbook, item 4.1).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Works on the day surfaces of `put_skew.build_day`
(``snap["expiries"][expiry_ms] = {"F", "T", "ratio", "strikes": {K: {"mark_iv", "sell_iv", "buy_iv", ...}}}``).
Units: vols are decimals (0.45 = 45 %), T in years = days / 365, Black-76 with r = 0 on the surface forward,
prices and P&L in USD per spread of 1 BTC per leg; ``r`` = P&L / debit (debit = capital at risk, fees included).

Costs follow Bybit options (``option_ideas.fee``): taker 0.03 % of spot capped at 7 % of the option price per leg
on entry and on an early exit; delivery 0.015 % of the delivery price capped at 12.5 % of intrinsic per ITM leg.
Bybit history does not exist, so execution IVs are Deribit marks shifted by an assumed half-spread ``hs``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

from .option_condor import nearest_fresh, strike_for_delta
from .option_ideas import fee, trend
from .options import black76_greeks, black76_price
from .put_skew import iv_at_strike

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
DELIVERY_RATE = 0.00015
DELIVERY_CAP = 0.125
MIN_IV = 0.01


def trend_at(day_stamps: Sequence[int], closes: Sequence[float], entry_ms: int, sma: int = 200, slope_days: int = 20) -> str:
    """`option_ideas.trend` on the daily closes known at ``entry_ms``.

    ``day_stamps`` are the bars' open times (day starts, ms); a bar is known once it closed, i.e. when
    ``stamp + DAY_MS <= entry_ms``. Later bars are ignored (no look-ahead).
    """
    known = [c for t, c in zip(day_stamps, closes, strict=True) if t + DAY_MS <= entry_ms]
    return trend(known, sma=sma, slope_days=slope_days)


def plan_spread(
    snap: dict[str, Any],
    expiry_ms: int,
    bearish: bool,
    *,
    long_delta: float = 0.40,
    short_delta: float = 0.15,
    band: float = 0.015,
) -> tuple[dict[str, Any] | None, str]:
    """(plan, reason) for a bear put spread (``bearish``) or a bull call spread on one day surface.

    Target strikes come from the day's smile (`strike_for_delta`); each leg is the fresh-mark strike nearest its
    target within ``band`` of spot (`nearest_fresh`), the sold leg strictly further OTM. reason is '' on success.
    """
    x = snap["expiries"].get(expiry_ms)
    if x is None or not x["strikes"]:
        return None, "no expiry"
    S = snap["spot"]
    is_call = not bearish
    sign = -1.0 if bearish else 1.0
    t_long = strike_for_delta(x, sign * long_delta, is_call)
    t_short = strike_for_delta(x, sign * short_delta, is_call)
    if t_long is None or t_short is None:
        return None, "no delta strike"
    k_long = nearest_fresh(x, t_long, S, band)
    if k_long is None:
        return None, "long leg: no fresh strike"
    k_short = nearest_fresh(x, t_short, S, band)
    if k_short is None:
        return None, "short leg: no fresh strike"
    if (bearish and not k_short < k_long) or (not bearish and not k_short > k_long):
        return None, "strike order"
    st = x["strikes"]
    F, T = x["F"], x["T"]
    m_long, m_short = st[k_long]["mark_iv"], st[k_short]["mark_iv"]
    return {
        "day": snap["day"],
        "t_ms": snap.get("t_ms", snap["day"]),
        "expiry": expiry_ms,
        "spot": S,
        "F": F,
        "T": T,
        "bearish": bearish,
        "is_call": is_call,
        "K_long": k_long,
        "K_short": k_short,
        "width": abs(k_long - k_short),
        "mark_long": m_long,
        "mark_short": m_short,
        "buy_long": st[k_long].get("buy_iv"),
        "sell_short": st[k_short].get("sell_iv"),
        "delta_long": black76_greeks(F, k_long, T, m_long, is_call)["delta"],
        "delta_short": black76_greeks(F, k_short, T, m_short, is_call)["delta"],
    }, ""


def exec_ivs(plan: dict[str, Any], model: str = "mark", hs: float = 0.015) -> tuple[float, float, int]:
    """(iv bought long leg, iv sold short leg, fallbacks). "mark": mark +/- ``hs``; "side": Deribit taker-buy IV for
    the bought leg and taker-sell IV for the sold one, falling back to mark +/- ``hs`` (counted)."""
    fb = 0
    iv_l = plan["mark_long"] + hs
    iv_s = plan["mark_short"] - hs
    if model == "side":
        if plan.get("buy_long") is not None:
            iv_l = plan["buy_long"]
        else:
            fb += 1
        if plan.get("sell_short") is not None:
            iv_s = plan["sell_short"]
        else:
            fb += 1
    elif model != "mark":
        raise ValueError(f"unknown model {model!r}")
    return iv_l, iv_s, fb


def entry_debit(plan: dict[str, Any], iv_long: float, iv_short: float) -> float | None:
    """Debit in USD incl. Bybit taker fees on both legs; None if an IV <= 1 % or the debit is not in (0, width)."""
    if iv_long <= MIN_IV or iv_short <= MIN_IV:
        return None
    F, T, S, c = plan["F"], plan["T"], plan["spot"], plan["is_call"]
    p_l = black76_price(F, plan["K_long"], T, iv_long, c)
    p_s = black76_price(F, plan["K_short"], T, iv_short, c)
    d = p_l - p_s + fee(S, p_l) + fee(S, p_s)
    return d if 0.0 < d < plan["width"] else None


def spread_payoff(plan: dict[str, Any], price: float) -> float:
    """Spread value at expiry, USD: long intrinsic minus short intrinsic (0 .. width)."""
    if plan["bearish"]:
        return max(plan["K_long"] - price, 0.0) - max(plan["K_short"] - price, 0.0)
    return max(price - plan["K_long"], 0.0) - max(price - plan["K_short"], 0.0)


def delivery_fees(plan: dict[str, Any], price: float) -> float:
    out = 0.0
    for K in (plan["K_long"], plan["K_short"]):
        intrinsic = max(K - price, 0.0) if plan["bearish"] else max(price - K, 0.0)
        if intrinsic > 0:
            out += min(DELIVERY_RATE * price, DELIVERY_CAP * intrinsic)
    return out


def settle(plan: dict[str, Any], debit: float, delivery_price: float) -> dict[str, Any]:
    """Hold to expiry: P&L (USD), r = P&L / debit, P&L as a fraction of entry spot."""
    pnl = spread_payoff(plan, delivery_price) - delivery_fees(plan, delivery_price) - debit
    return {"pnl": pnl, "r": pnl / debit, "pnl_spot": pnl / plan["spot"], "exit": "expiry", "exit_ms": plan["expiry"]}


def close_value(plan: dict[str, Any], snap: dict[str, Any], hs: float = 0.015) -> float | None:
    """USD received for closing the spread on a later day surface: sell the long leg at mark - hs, buy back the short
    at mark + hs (marks interpolated on that day's smile), minus taker fees. None if the expiry is not on the surface."""
    x = snap["expiries"].get(plan["expiry"])
    if x is None or not x["strikes"] or x["T"] <= 0:
        return None
    iv_l = iv_at_strike(x, plan["K_long"])[0]
    iv_s = iv_at_strike(x, plan["K_short"])[0]
    if iv_l is None or iv_s is None:
        return None
    F, T, S, c = x["F"], x["T"], snap["spot"], plan["is_call"]
    p_l = black76_price(F, plan["K_long"], T, max(iv_l - hs, MIN_IV), c)
    p_s = black76_price(F, plan["K_short"], T, iv_s + hs, c)
    return p_l - p_s - fee(S, p_l) - fee(S, p_s)


def managed(
    days: dict[int, dict[str, Any]],
    plan: dict[str, Any],
    debit: float,
    delivery_price: float,
    *,
    tp: float = 0.70,
    beyond_short: bool = True,
    hs: float = 0.015,
) -> dict[str, Any]:
    """Playbook exit: on each daily surface after entry and before expiry, close when the closing value gives
    >= ``tp`` of the max profit (width - debit) or (``beyond_short``) spot has passed the sold strike; else expiry."""
    target = tp * (plan["width"] - debit)
    d = plan["day"] + DAY_MS
    while True:
        snap = days.get(d)
        if snap is not None and snap.get("t_ms", d) >= plan["expiry"]:
            break
        if snap is None and d >= plan["expiry"]:
            break
        if snap is not None:
            v = close_value(plan, snap, hs)
            S = snap["spot"]
            passed = beyond_short and (S <= plan["K_short"] if plan["bearish"] else S >= plan["K_short"])
            if v is not None and (v - debit >= target or passed):
                pnl = v - debit
                why = "tp" if v - debit >= target else "beyond_short"
                return {"pnl": pnl, "r": pnl / debit, "pnl_spot": pnl / plan["spot"], "exit": why, "exit_ms": snap.get("t_ms", d)}
        d += DAY_MS
    return settle(plan, debit, delivery_price)


def episodes(labels: Sequence[tuple[int, str]], step_ms: int = 7 * DAY_MS) -> dict[int, int]:
    """Episode id per non-neutral entry: a new episode starts when the label changes, the previous entry was neutral,
    or the previous entry is more than ``step_ms`` earlier. ``labels`` = [(entry_ms, label)] in any order."""
    out: dict[int, int] = {}
    eid = -1
    prev_t: int | None = None
    prev_l = "neutral"
    for t, lab in sorted(labels):
        if lab != "neutral":
            if prev_l != lab or prev_t is None or t - prev_t > step_ms:
                eid += 1
            out[t] = eid
        prev_t, prev_l = t, lab
    return out


def holm_adjust(pvals: dict[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values: p_adj_(i) = max_{j <= i} min(1, (m - j + 1) p_(j))."""
    m = len(pvals)
    out: dict[str, float] = {}
    run = 0.0
    for i, (k, p) in enumerate(sorted(pvals.items(), key=lambda kv: kv[1])):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = run
    return out


def cluster_bootstrap(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261114) -> dict[str, Any]:
    """Mean with a cluster bootstrap: resample whole clusters with replacement, mean over the resampled trades.

    Returns n, n_clusters, mean, se (sd of the draws), 95 % percentile CI and two-sided p = 2 min(P(<=0), P(>=0)).
    """
    n = len(values)
    if n == 0:
        return {"n": 0, "n_clusters": 0, "mean": float("nan"), "se": float("nan"), "ci": None, "p": float("nan")}
    groups: dict[Any, list[float]] = {}
    for v, c in zip(values, clusters, strict=True):
        groups.setdefault(c, []).append(v)
    sums = [(sum(g), len(g)) for g in groups.values()]
    k = len(sums)
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        s = cnt = 0.0
        for _ in range(k):
            a, b = sums[rng.randrange(k)]
            s += a
            cnt += b
        draws.append(s / cnt)
    draws.sort()
    m = sum(draws) / n_boot
    se = math.sqrt(sum((d - m) ** 2 for d in draws) / (n_boot - 1)) if n_boot > 1 else 0.0
    le = sum(1 for d in draws if d <= 0) / n_boot
    ge = sum(1 for d in draws if d >= 0) / n_boot
    return {
        "n": n,
        "n_clusters": k,
        "mean": sum(values) / n,
        "se": se,
        "ci": (draws[int(0.025 * (n_boot - 1))], draws[int(0.975 * (n_boot - 1))]),
        "p": min(1.0, 2.0 * min(le, ge)),
    }
