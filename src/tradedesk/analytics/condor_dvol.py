"""Playbook rule P1' on history: iron condor entered when the DVOL 365-day percentile is high (IDEA-20261114-options-playbook, 4.2).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Works on the day surfaces of `put_skew.build_day`
(``snap["expiries"][expiry_ms] = {"F", "T", "ratio", "strikes": {K: {"mark_iv", "sell_iv", "buy_iv", ...}}}``).
Units: vols are decimals (0.45 = 45 %), T in years = days / 365, Black-76 with r = 0 on the surface forward,
prices and P&L in USD per condor of 1 BTC per leg. ``r`` = P&L / max loss, max loss = widest side - credit (credit
net of entry fees): the return on the capital the owner's risk rule sizes by. Bybit margins a sold option without
netting the bought wing, so the return on actual Bybit margin is lower than ``r``.

Construction mirrors the weekly generator (`option_ideas.iron_condor`): sold put / call at |delta| ~ ``short_delta``
on the OTM side of spot, bought wings ~ ``wing`` x spot beyond the sold strikes. On history every leg is the
fresh-mark strike nearest its target within ``band`` x spot (the rule of the earlier condor reports), a wing
strictly beyond its sold strike.

Costs as `trend_spread` (Bybit): taker fee per leg ``option_ideas.fee`` on entry and on an early exit; delivery
0.015 % of the delivery price capped at 12.5 % of intrinsic per ITM leg. Execution IVs = Deribit marks shifted by an
assumed half-spread (sold legs at mark - hs, bought at mark + hs; `option_condor.exec_legs`).
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

from .option_condor import LEG_IS_CALL, LEG_QTY, LEGS, strike_for_delta
from .option_ideas import fee
from .options import black76_greeks, black76_price
from .put_skew import iv_at_strike
from .trend_spread import DAY_MS, DELIVERY_CAP, DELIVERY_RATE, MIN_IV
from .vrp import trailing_percentiles


def entry_dvol_percentiles(candles: Sequence[tuple[int, float]], *, window: int = 365) -> dict[int, float]:
    """Entry day (00:00 UTC ms) -> 365-day percentile of the DVOL known at that day's 09:00 entry.

    Same statistic as `deribit_vrp` -> ``current.dvol_percentile_365d`` (`vrp.trailing_percentiles`: rank of the value
    among the previous ``window`` daily values, lower rank / window). The daily candle stamped ``d`` closes at 24:00,
    so entry day ``d`` uses the candle stamped ``d - 1 day``; later candles never enter (no look-ahead).
    """
    rows = [{"day": float(t), "iv": float(v)} for t, v in sorted(candles)]
    pct = trailing_percentiles(rows, window=window)
    return {(int(t) // DAY_MS) * DAY_MS + DAY_MS: p for t, p in pct.items()}


def wing_strike(exp: dict[str, Any], short_k: float, spot: float, wing: float, band: float, is_call: bool) -> float | None:
    """Fresh-mark strike strictly beyond ``short_k`` nearest ``short_k +/- wing * spot`` (ties: lower strike), if within
    ``band * spot`` of that target; else None."""
    target = short_k + wing * spot if is_call else short_k - wing * spot
    cands = [k for k in exp["strikes"] if (k > short_k if is_call else k < short_k)]
    if not cands:
        return None
    K = min(cands, key=lambda k: (abs(k - target), k))
    return K if abs(K - target) <= band * spot + 1e-9 else None


def _nearest_fresh_side(exp: dict[str, Any], target: float, spot: float, band: float, is_call: bool) -> float | None:
    """Fresh-mark strike nearest ``target`` on the OTM side of spot (above for calls, below for puts), within band."""
    cands = [k for k in exp["strikes"] if (k > spot if is_call else k < spot)]
    if not cands:
        return None
    K = min(cands, key=lambda k: (abs(k - target), k))
    return K if abs(K - target) <= band * spot + 1e-9 else None


def plan_p1(
    snap: dict[str, Any], expiry_ms: int, *, short_delta: float = 0.15, wing: float = 0.03, band: float = 0.015
) -> tuple[dict[str, Any] | None, str]:
    """(plan, reason) for the P1' condor on one day surface; reason is '' when all four legs are found.

    plan has the keys `option_condor.exec_legs` reads ("K", "mark", "sell", "buy" per leg) plus "day", "t_ms",
    "expiry", "spot", "F", "T", "width" (the wider side), "target" (strike targets) and "delta" (Black-76 at mark).
    """
    x = snap["expiries"].get(expiry_ms)
    if x is None or not x["strikes"]:
        return None, "no expiry"
    S = snap["spot"]
    t_sp = strike_for_delta(x, -short_delta, False)
    t_sc = strike_for_delta(x, short_delta, True)
    if t_sp is None or t_sc is None:
        return None, "no delta strike"
    K_sp = _nearest_fresh_side(x, t_sp, S, band, False)
    if K_sp is None:
        return None, "short put: no fresh strike"
    K_sc = _nearest_fresh_side(x, t_sc, S, band, True)
    if K_sc is None:
        return None, "short call: no fresh strike"
    K_lp = wing_strike(x, K_sp, S, wing, band, False)
    if K_lp is None:
        return None, "long put: no fresh strike"
    K_lc = wing_strike(x, K_sc, S, wing, band, True)
    if K_lc is None:
        return None, "long call: no fresh strike"
    K = {"lp": K_lp, "sp": K_sp, "sc": K_sc, "lc": K_lc}
    st = x["strikes"]
    F, T = x["F"], x["T"]
    mark = {k: st[K[k]]["mark_iv"] for k in LEGS}
    return {
        "day": snap["day"],
        "t_ms": snap.get("t_ms", snap["day"]),
        "expiry": expiry_ms,
        "spot": S,
        "F": F,
        "T": T,
        "K": K,
        "target": {"lp": K_sp - wing * S, "sp": t_sp, "sc": t_sc, "lc": K_sc + wing * S},
        "mark": mark,
        "sell": {k: st[K[k]].get("sell_iv") for k in LEGS},
        "buy": {k: st[K[k]].get("buy_iv") for k in LEGS},
        "delta": {k: black76_greeks(F, K[k], T, mark[k], LEG_IS_CALL[k])["delta"] for k in LEGS},
        "width": max(K_sp - K_lp, K_lc - K_sc),
    }, ""


def entry_credit(plan: dict[str, Any], legs: Sequence[tuple[float, bool, float, float]]) -> float | None:
    """Credit in USD after Bybit taker fees on all four legs; None if an IV <= 1 % or the credit is not in (0, width)."""
    if not legs or any(iv <= MIN_IV for *_, iv in legs):
        return None
    F, T, S = plan["F"], plan["T"], plan["spot"]
    c = 0.0
    for K, is_call, q, iv in legs:
        p = black76_price(F, K, T, iv, is_call)
        c += -q * p - fee(S, p)
    return c if 0.0 < c < plan["width"] else None


def _intrinsic(K: float, is_call: bool, price: float) -> float:
    return max(price - K, 0.0) if is_call else max(K - price, 0.0)


def condor_payoff(plan: dict[str, Any], price: float) -> float:
    """Condor value at expiry for the seller, USD: sum of qty x intrinsic (between -width and 0)."""
    return sum(LEG_QTY[k] * _intrinsic(plan["K"][k], LEG_IS_CALL[k], price) for k in LEGS)


def delivery_fees(plan: dict[str, Any], price: float) -> float:
    """Bybit delivery fee: min(0.015 % of the delivery price, 12.5 % of intrinsic) on every ITM leg, USD."""
    out = 0.0
    for k in LEGS:
        v = _intrinsic(plan["K"][k], LEG_IS_CALL[k], price)
        if v > 0:
            out += min(DELIVERY_RATE * price, DELIVERY_CAP * v)
    return out


def _result(plan: dict[str, Any], credit: float, pnl: float, exit_: str, exit_ms: int) -> dict[str, Any]:
    max_loss = plan["width"] - credit
    return {"pnl": pnl, "r": pnl / max_loss, "pnl_spot": pnl / plan["spot"], "max_loss": max_loss, "exit": exit_, "exit_ms": exit_ms}


def settle(plan: dict[str, Any], credit: float, delivery_price: float) -> dict[str, Any]:
    """Hold to expiry: P&L (USD), r = P&L / max loss, P&L as a fraction of entry spot (on notional of 1 BTC)."""
    pnl = credit + condor_payoff(plan, delivery_price) - delivery_fees(plan, delivery_price)
    return _result(plan, credit, pnl, "expiry", plan["expiry"])


def close_cost(plan: dict[str, Any], snap: dict[str, Any], hs: float = 0.015) -> float | None:
    """USD paid to close the condor on a later day surface: buy back the sold legs at mark + hs, sell the wings at
    mark - hs (marks interpolated on that day's smile), plus taker fees. None if the expiry is not on the surface."""
    x = snap["expiries"].get(plan["expiry"])
    if x is None or not x["strikes"] or x["T"] <= 0:
        return None
    F, T, S = x["F"], x["T"], snap["spot"]
    cost = 0.0
    for k in LEGS:
        iv = iv_at_strike(x, plan["K"][k])[0]
        if iv is None:
            return None
        sold = LEG_QTY[k] < 0
        p = black76_price(F, plan["K"][k], T, iv + hs if sold else max(iv - hs, MIN_IV), LEG_IS_CALL[k])
        cost += (p if sold else -p) + fee(S, p)
    return cost


def managed(
    days: Mapping[int, dict[str, Any]],
    plan: dict[str, Any],
    credit: float,
    delivery_price: float,
    *,
    tp: float = 0.80,
    breach: bool = True,
    hs: float = 0.015,
) -> dict[str, Any]:
    """Course exit: on each daily surface after entry and before expiry, close when ``tp`` of the credit is kept
    (credit - close cost >= tp x credit) or (``breach``) spot has reached a sold strike; else hold to expiry."""
    d = plan["day"] + DAY_MS
    while True:
        snap = days.get(d)
        if snap is not None and snap.get("t_ms", d) >= plan["expiry"]:
            break
        if snap is None and d >= plan["expiry"]:
            break
        if snap is not None:
            v = close_cost(plan, snap, hs)
            S = snap["spot"]
            hit = breach and (S <= plan["K"]["sp"] or S >= plan["K"]["sc"])
            if v is not None and (credit - v >= tp * credit or hit):
                return _result(plan, credit, credit - v, "tp" if credit - v >= tp * credit else "breach", snap.get("t_ms", d))
        d += DAY_MS
    return settle(plan, credit, delivery_price)


def cluster_bootstrap_diff(
    values: Sequence[float], clusters: Sequence[Any], in_a: Sequence[bool], *, n_boot: int = 10_000, seed: int = 20261114
) -> dict[str, Any]:
    """mean(A) - mean(B) with a cluster bootstrap that resamples whole clusters within each group.

    Every cluster must lie entirely in one group (a cluster in both raises ValueError). Returns diff, se, 95 %
    percentile CI and two-sided p = 2 min(P(<=0), P(>=0)); None-valued CI if a group is empty.
    """
    ga: dict[Any, list[float]] = {}
    gb: dict[Any, list[float]] = {}
    for v, c, a in zip(values, clusters, in_a, strict=True):
        (ga if a else gb).setdefault(c, []).append(v)
    if set(ga) & set(gb):
        raise ValueError("a cluster spans both groups")
    if not ga or not gb:
        return {"diff": float("nan"), "se": float("nan"), "ci": None, "p": float("nan"), "k_a": len(ga), "k_b": len(gb)}
    sa = [(sum(g), len(g)) for g in ga.values()]
    sb = [(sum(g), len(g)) for g in gb.values()]

    def mean_of(pairs: list[tuple[float, int]]) -> float:
        return sum(s for s, _ in pairs) / sum(n for _, n in pairs)

    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        da = [sa[rng.randrange(len(sa))] for _ in sa]
        db = [sb[rng.randrange(len(sb))] for _ in sb]
        draws.append(mean_of(da) - mean_of(db))
    draws.sort()
    m = sum(draws) / n_boot
    se = (sum((d - m) ** 2 for d in draws) / (n_boot - 1)) ** 0.5 if n_boot > 1 else 0.0
    le = sum(1 for d in draws if d <= 0) / n_boot
    ge = sum(1 for d in draws if d >= 0) / n_boot
    return {
        "diff": mean_of(sa) - mean_of(sb),
        "se": se,
        "ci": (draws[int(0.025 * (n_boot - 1))], draws[int(0.975 * (n_boot - 1))]),
        "p": min(1.0, 2.0 * min(le, ge)),
        "k_a": len(sa),
        "k_b": len(sb),
    }
