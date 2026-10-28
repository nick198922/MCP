"""Long-strangle management research: "sell the far strike after a move" on daily Deribit BTC surfaces.

Card: docs/ideas/IDEA-20261028-long-strangle-sell-far-strike.md. Pure functions, no I/O.

Inputs are normalised daily surfaces (one per day, time ordered)::

    {"day": ms, "t_ms": ms (09:00 UTC), "spot": float,
     "expiries": {expiry_ms: {"F": float, "strikes": {K: {"mark_iv": float, ...}}}}}

A strike is present only if it had a fresh (<= 6 h) mark, as built by ``put_skew.build_day``.
Units: vols and rates are decimals (0.45 = 45 %); option values are USD per 1 BTC notional,
``Black76(F, K, tau, sigma) * S / F`` (Deribit inverse options converted at the day's spot);
returns are fractions of a spot (0.01 = 1 % of spot), always on notional.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .options import black76_price
from .put_skew import iv_at_strike

DAY_MS = 86_400_000
YEAR_MS = 365.0 * DAY_MS


@dataclass(frozen=True)
class Params:
    dte: tuple[float, float] = (14.0, 21.0)  # entry series DTE band, days
    dte_mid: float = 17.5  # tie-break: series nearest to this DTE
    buy_off: float = 0.03  # bought legs at F_T * (1 -/+ buy_off)
    buy_band: float = 0.015  # tolerance on K / F_T
    trig: float = 0.03  # trigger: |S_D / S_T - 1| >= trig
    max_wait: int = 5  # trigger looked for on T+1 .. T+max_wait
    min_dte_d: float = 8.0  # days left to expiry at D
    sell_off: float = 0.08  # sold strike at S_T * (1 -/+ sell_off), same side as the move
    sell_band: float = 0.015  # tolerance on K / S_T
    far_off: float = 0.12  # variant (c): short end of the bought vertical
    cost_vp: float = 0.015  # cost per trade in vol points (decimal): buy at iv + c, sell at iv - c
    fee_btc: float = 0.0003  # Deribit option taker fee per trade, BTC per 1 BTC notional
    fee_cap: float = 0.125  # fee capped at this share of the option price


def tau_years(expiry_ms: int, t_ms: int) -> float:
    return max(expiry_ms - t_ms, 0) / YEAR_MS


def leg_value(F: float, K: float, tau: float, sigma: float, is_call: bool, spot: float) -> float:
    """USD value of one Deribit inverse option (1 BTC notional) at spot ``spot``."""
    return black76_price(F, K, tau, max(sigma, 1e-4), is_call) * spot / F


def fee_usd(value: float, spot: float, p: Params) -> float:
    """Taker fee in USD: min(fee_btc * spot, fee_cap * option value)."""
    return min(p.fee_btc * spot, p.fee_cap * value)


def payoff(K: float, s_exp: float, is_call: bool) -> float:
    return max(s_exp - K, 0.0) if is_call else max(K - s_exp, 0.0)


def pick_strike(strikes: Sequence[float], ref: float, target: float, band: float) -> float | None:
    """Strike nearest to ``ref * target`` with |K / ref - target| <= band (ties: lower K)."""
    best: tuple[float, float] | None = None
    for k in strikes:
        d = abs(k / ref - target)
        if d <= band + 1e-12 and (best is None or (d, k) < best):
            best = (d, k)
    return best[1] if best else None


def choose_series(expiries: dict[int, Any], t_ms: int, band: tuple[float, float], mid: float) -> int | None:
    """Expiry with DTE in ``band`` (days, inclusive) nearest to ``mid`` (ties: earlier)."""
    cands = [(abs((e - t_ms) / DAY_MS - mid), e) for e in expiries if band[0] <= (e - t_ms) / DAY_MS <= band[1]]
    return min(cands)[1] if cands else None


def find_trigger(spots: Sequence[float | None], i: int, max_wait: int, thr: float) -> tuple[int, str] | None:
    """First j in i+1 .. i+max_wait with |spots[j] / spots[i] - 1| >= thr -> (j, "up" | "down")."""
    s0 = spots[i]
    if not s0:
        return None
    for j in range(i + 1, min(i + max_wait, len(spots) - 1) + 1):
        s = spots[j]
        if not s:
            continue
        r = s / s0 - 1.0
        if r >= thr:
            return j, "up"
        if r <= -thr:
            return j, "down"
    return None


def build_episode(days: Sequence[dict[str, Any]], i: int, p: Params) -> dict[str, Any]:
    """One entry on day ``i``: bought strangle, trigger, sold strike and H2 ratios.

    Uses only days ``i`` .. D (no expiry data), so it is causal at D. ``status`` is ``ok`` or the
    first missing piece (``no_series``, ``no_put``, ``no_call``, ``no_trigger``, ``short_dte``,
    ``no_sell_mark``, ``no_series_d``).
    """
    dT = days[i]
    out: dict[str, Any] = {"i": i, "T": dT["day"], "status": "ok"}
    exp = choose_series(dT["expiries"], dT["t_ms"], p.dte, p.dte_mid)
    if exp is None:
        return {**out, "status": "no_series"}
    eT = dT["expiries"][exp]
    F_T, S_T, tau_T = eT["F"], dT["spot"], tau_years(exp, dT["t_ms"])
    kp = pick_strike(list(eT["strikes"]), F_T, 1.0 - p.buy_off, p.buy_band)
    kc = pick_strike(list(eT["strikes"]), F_T, 1.0 + p.buy_off, p.buy_band)
    out.update(exp=exp, S_T=S_T, F_T=F_T, tau_T=tau_T, K_p=kp, K_c=kc)
    if kp is None:
        return {**out, "status": "no_put"}
    if kc is None:
        return {**out, "status": "no_call"}
    out["iv_p"], out["iv_c"] = eT["strikes"][kp]["mark_iv"], eT["strikes"][kc]["mark_iv"]
    out["V_p"] = leg_value(F_T, kp, tau_T, out["iv_p"], False, S_T)
    out["V_c"] = leg_value(F_T, kc, tau_T, out["iv_c"], True, S_T)
    trg = find_trigger([d["spot"] for d in days], i, p.max_wait, p.trig)
    if trg is None:
        return {**out, "status": "no_trigger"}
    j, side = trg
    dD = days[j]
    is_call = side == "up"
    out.update(D=dD["day"], j=j, side=side, S_D=dD["spot"])
    if (exp - dD["t_ms"]) / DAY_MS < p.min_dte_d:
        return {**out, "status": "short_dte"}
    eD = dD["expiries"].get(exp)
    if eD is None:
        return {**out, "status": "no_series_d"}
    F_D, S_D, tau_D = eD["F"], dD["spot"], tau_years(exp, dD["t_ms"])
    tgt = 1.0 + p.sell_off if is_call else 1.0 - p.sell_off
    ks = pick_strike(list(eD["strikes"]), S_T, tgt, p.sell_band)
    out["F_D"] = F_D
    if ks is None:
        return {**out, "status": "no_sell_mark"}
    kb = kc if is_call else kp
    iv_b = out["iv_c"] if is_call else out["iv_p"]
    v_b = out["V_c"] if is_call else out["V_p"]
    iv_s = eD["strikes"][ks]["mark_iv"]
    iv_mod, _ = iv_at_strike(eT, ks * F_T / F_D)  # T surface at the same log-moneyness (sticky smile)
    iv_stk, _ = iv_at_strike(eT, ks)  # T surface at the same strike (sticky strike)
    v_s = leg_value(F_D, ks, tau_D, iv_s, is_call, S_D)
    v_mod = leg_value(F_D, ks, tau_D, iv_mod, is_call, S_D) if iv_mod else None
    v_stk = leg_value(F_D, ks, tau_D, iv_stk, is_call, S_D) if iv_stk else None
    v_s_net = leg_value(F_D, ks, tau_D, iv_s - p.cost_vp, is_call, S_D)
    v_b_net = leg_value(F_T, kb, tau_T, iv_b + p.cost_vp, is_call, S_T)
    out.update(
        K_b=kb,
        K_s=ks,
        iv_b=iv_b,
        iv_s=iv_s,
        iv_mod=iv_mod,
        iv_stk=iv_stk,
        V_s=v_s,
        R=v_s / v_b,
        R_mod=v_mod / v_b if v_mod is not None else None,
        R_stk=v_stk / v_b if v_stk is not None else None,
        R_net=v_s_net / v_b_net,
        R_pct=(v_s / S_D) / (v_b / S_T),
        m_s=ks / S_D,
        tau_D=tau_D,
    )
    out["dR"] = out["R"] - out["R_mod"] if out["R_mod"] is not None else None
    out["dR_stk"] = out["R"] - out["R_stk"] if out["R_stk"] is not None else None
    return out


def h1_pnl(ep: dict[str, Any], day_d: dict[str, Any], s_exp: float, p: Params) -> dict[str, float]:
    """Expiry P&L (fraction of S_T, 1 BTC per leg) of the four management variants of an ``ok`` episode.

    a: hold both legs; g: a + short K_s at D (F1ntech); b: a with the trigger leg sold at D;
    c: b + long vertical K_s / K_s2 bought at D. Costs: ``cost_vp`` per trade and taker fees.
    ``short`` is the short-leg P&L alone as a fraction of S_D (for the non-trigger control).
    """
    S_T, S_D, exp = ep["S_T"], ep["S_D"], ep["exp"]
    is_call = ep["side"] == "up"
    eD = day_d["expiries"][exp]
    F_D, tau_D, tau_T = eD["F"], tau_years(exp, day_d["t_ms"]), ep["tau_T"]

    def buy(F: float, K: float, tau: float, iv: float, call: bool, S: float) -> float:
        v = leg_value(F, K, tau, iv + p.cost_vp, call, S)
        return -v - fee_usd(v, S, p)

    def sell(F: float, K: float, tau: float, iv: float, call: bool, S: float) -> float:
        v = leg_value(F, K, tau, iv - p.cost_vp, call, S)
        return v - fee_usd(v, S, p)

    kp, kc, ks, kb = ep["K_p"], ep["K_c"], ep["K_s"], ep["K_b"]
    a = buy(ep["F_T"], kp, tau_T, ep["iv_p"], False, S_T) + buy(ep["F_T"], kc, tau_T, ep["iv_c"], True, S_T)
    a += payoff(kp, s_exp, False) + payoff(kc, s_exp, True)
    short = sell(F_D, ks, tau_D, ep["iv_s"], is_call, S_D) - payoff(ks, s_exp, is_call)
    iv_bD, _ = iv_at_strike(eD, kb)
    close_b = sell(F_D, kb, tau_D, iv_bD, is_call, S_D) - payoff(kb, s_exp, is_call)
    ks2 = S_T * (1.0 + p.far_off if is_call else 1.0 - p.far_off)
    iv_sD, _ = iv_at_strike(eD, ks)
    iv_s2, _ = iv_at_strike(eD, ks2)
    vert = buy(F_D, ks, tau_D, iv_sD, is_call, S_D) + payoff(ks, s_exp, is_call)
    vert += sell(F_D, ks2, tau_D, iv_s2, is_call, S_D) - payoff(ks2, s_exp, is_call)
    b = a + close_b
    return {"a": a / S_T, "g": (a + short) / S_T, "b": b / S_T, "c": (b + vert) / S_T, "short": short / S_D}


def nontrigger(spots: Sequence[float | None], j: int, lookback: int, thr: float) -> bool:
    """True if day j had no |S_j / S_{j-k} - 1| >= thr for k = 1 .. lookback (all lags must exist)."""
    if j - lookback < 0 or not spots[j]:
        return False
    for k in range(1, lookback + 1):
        s = spots[j - k]
        if not s or abs(spots[j] / s - 1.0) >= thr:
            return False
    return True


def control_short(
    day: dict[str, Any], side: str, m: float, s_exp_of: Callable[[int], float | None], p: Params, *, dte=(8.0, 18.0), mid=13.0
) -> float | None:
    """Short-leg P&L (fraction of the day's spot) of selling the ``side`` option at K ~ m * S on a day."""
    exp = choose_series(day["expiries"], day["t_ms"], dte, mid)
    if exp is None:
        return None
    e, S, is_call = day["expiries"][exp], day["spot"], side == "up"
    k = pick_strike(list(e["strikes"]), S, m, p.sell_band)
    s_exp = s_exp_of(exp)
    if k is None or s_exp is None:
        return None
    v = leg_value(e["F"], k, tau_years(exp, day["t_ms"]), e["strikes"][k]["mark_iv"] - p.cost_vp, is_call, S)
    return (v - fee_usd(v, S, p) - payoff(k, s_exp, is_call)) / S


def dedup(rows: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], Any], fields: Sequence[str]) -> list[dict[str, Any]]:
    """One row per key (time order of first appearance): mean of ``fields`` (None ignored), ``n_ep`` episodes."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = []
    for k, g in groups.items():
        row: dict[str, Any] = {"key": k, "n_ep": len(g), "first": g[0]}
        for f in fields:
            vals = [r[f] for r in g if r.get(f) is not None]
            row[f] = statistics.fmean(vals) if vals else None
        out.append(row)
    return out


def cvar(values: Sequence[float], q: float = 0.05) -> float:
    """Mean of the worst ceil(q * n) values (lower tail)."""
    v = sorted(values)
    k = max(1, math.ceil(q * len(v)))
    return statistics.fmean(v[:k])


def block_draws(
    values: Sequence[float], stat: Callable[[Sequence[float]], float], *, block: int, n_boot: int = 2000, seed: int = 7
) -> list[float]:
    """Moving-block bootstrap draws of ``stat`` over time-ordered ``values``."""
    n = len(values)
    if n == 0:
        return []
    b = max(1, min(block, n))
    rng = random.Random(seed)
    out = []
    for _ in range(n_boot):
        s: list[float] = []
        while len(s) < n:
            i = rng.randrange(0, n - b + 1)
            s.extend(values[i : i + b])
        out.append(stat(s[:n]))
    return out


def pct_ci(draws: Sequence[float]) -> tuple[float, float] | None:
    if len(draws) < 20:
        return None
    d = sorted(draws)
    return d[int(0.025 * len(d))], d[int(0.975 * len(d)) - 1]


def mde(sd: float, n: int) -> float | None:
    """Minimum detectable effect at 5 % two-sided, 80 % power: 2.8 * SD / sqrt(n)."""
    return 2.8 * sd / math.sqrt(n) if n > 0 else None


def h2_verdict(mean: float, ci: tuple[float, float] | None, *, coverage: float, n: int) -> str:
    """Pre-registered H2 verdict for one side (thresholds 0.90 / 0.75, gate 60 % coverage, n >= 30)."""
    if coverage < 0.60 or n < 30 or ci is None:
        return "◯"
    if mean >= 0.90 and ci[0] >= 0.75:
        return "✓"
    if ci[1] < 0.90:
        return "✗"
    return "≈"
