"""Short iron condors on Deribit BTC options (IDEA-20261031-structural-edges-depth, item 7: modules H1, H4, H5).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Works on the day surfaces of `put_skew.build_day`
(``snap["expiries"][expiry_ms] = {"F", "T", "ratio", "strikes": {K: {"mark_iv", "sell_iv", "buy_iv", ...}}}``) and
settles with `weekly_straddle.book_path` (no hedge), so units are those of the earlier option reports:
  * vols are decimals (0.45 = 45 %), T in years = days / 365, Deribit expiries at 08:00 UTC, Black-76 with r = 0;
  * P&L is a fraction of entry spot per 1 BTC notional on every leg (0.01 = 1 % of spot), i.e. on notional.

Condor = short put (put delta -0.10), short call (call delta +0.10), long put at (1 - w) S and long call at
(1 + w) S, the wings at least w / 3 S beyond the short strikes. Every leg is the fresh-mark strike nearest its
target if within ``band`` of spot (the registered "nearest fresh strike within +-1.5 %" rule).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Any

from .options import black76_greeks
from .put_skew import iv_at_strike
from .weekly_straddle import book_path

LEGS = ("lp", "sp", "sc", "lc")  # long put, short put, short call, long call
LEG_IS_CALL = {"lp": False, "sp": False, "sc": True, "lc": True}
LEG_QTY = {"lp": 1.0, "sp": -1.0, "sc": -1.0, "lc": 1.0}  # + bought, - sold (BTC per leg)


def strike_for_delta(exp: dict[str, Any], delta: float, is_call: bool, *, iters: int = 80) -> float | None:
    """Strike whose Black-76 delta at the expiry's smile (``iv_at_strike``) equals ``delta``.

    ``delta`` is a call delta in (0, 1) for calls and a put delta in (-1, 0) for puts; both decrease in K, so
    the root is bracketed by bisection on ln K over [F e^-3, F e^3]. None if the smile is empty or unbracketed.
    """
    F, T = exp["F"], exp["T"]
    if T <= 0 or not exp["strikes"]:
        return None

    def f(lnk: float) -> float | None:
        K = math.exp(lnk)
        iv = iv_at_strike(exp, K)[0]
        if iv is None or iv <= 0:
            return None
        return black76_greeks(F, K, T, iv, is_call)["delta"] - delta

    lo, hi = math.log(F) - 3.0, math.log(F) + 3.0
    flo, fhi = f(lo), f(hi)
    if flo is None or fhi is None or flo < 0 or fhi > 0:
        return None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        fm = f(mid)
        if fm is None:
            return None
        if fm > 0:
            lo = mid
        else:
            hi = mid
    return math.exp(0.5 * (lo + hi))


def nearest_fresh(exp: dict[str, Any], target: float, spot: float, band: float) -> float | None:
    """The fresh-mark strike nearest ``target`` if |K - target| <= band * spot (ties: lower strike), else None."""
    st = exp["strikes"]
    if not st:
        return None
    K = min(st, key=lambda k: (abs(k - target), k))
    return K if abs(K - target) <= band * spot + 1e-9 else None


def plan_condor(
    snap: dict[str, Any], expiry_ms: int, *, short_delta: float = 0.10, wing: float = 0.15, band: float = 0.015
) -> tuple[dict[str, Any] | None, str]:
    """(plan, reason) for a condor on ``expiry_ms`` in one day surface; reason is '' when all four legs are found.

    plan = {"day", "expiry", "spot", "F", "T", "K": {leg: strike}, "target": {leg: K*}, "floor": {"lp", "lc"}
    (True where the w/3 width floor set the wing), "mark"/"sell"/"buy": {leg: IV or None}, "width": max side width}.
    """
    x = snap["expiries"].get(expiry_ms)
    if x is None or not x["strikes"]:
        return None, "no expiry"
    S = snap["spot"]
    t_sp = strike_for_delta(x, -short_delta, False)
    t_sc = strike_for_delta(x, short_delta, True)
    if t_sp is None or t_sc is None:
        return None, "no 10-delta strike"
    K_sp = nearest_fresh(x, t_sp, S, band)
    K_sc = nearest_fresh(x, t_sc, S, band)
    if K_sp is None:
        return None, "short put: no fresh strike"
    if K_sc is None:
        return None, "short call: no fresh strike"
    t_lp = min((1.0 - wing) * S, K_sp - wing / 3.0 * S)
    t_lc = max((1.0 + wing) * S, K_sc + wing / 3.0 * S)
    K_lp = nearest_fresh(x, t_lp, S, band)
    K_lc = nearest_fresh(x, t_lc, S, band)
    if K_lp is None:
        return None, "long put: no fresh strike"
    if K_lc is None:
        return None, "long call: no fresh strike"
    if not (K_lp < K_sp < x["F"] < K_sc < K_lc):
        return None, "strike order"
    K = {"lp": K_lp, "sp": K_sp, "sc": K_sc, "lc": K_lc}
    st = x["strikes"]
    return {
        "day": snap["day"],
        "expiry": expiry_ms,
        "spot": S,
        "F": x["F"],
        "T": x["T"],
        "K": K,
        "target": {"lp": t_lp, "sp": t_sp, "sc": t_sc, "lc": t_lc},
        "floor": {"lp": t_lp < (1.0 - wing) * S * (1.0 - 1e-9), "lc": t_lc > (1.0 + wing) * S * (1.0 + 1e-9)},
        "mark": {k: st[K[k]]["mark_iv"] for k in LEGS},
        "sell": {k: st[K[k]].get("sell_iv") for k in LEGS},
        "buy": {k: st[K[k]].get("buy_iv") for k in LEGS},
        "width": max(K_sp - K_lp, K_lc - K_sc),
    }, ""


def exec_legs(plan: dict[str, Any], model: str, hs: float = 0.015) -> tuple[list[tuple[float, bool, float, float]], int]:
    """Execution legs ``[(K, is_call, qty, iv)]`` and the number of side-model fallbacks.

    ``model``: "side" = sold legs at the taker-sell IV (bid side), bought legs at the taker-buy IV (ask side), a leg
    without that print falls back to mark -/+ ``hs`` (counted); "mark" = mark -/+ ``hs`` (sold / bought);
    "side_only" = like "side" but returns [] if any leg lacks its print.
    """
    out, fb = [], 0
    for k in LEGS:
        q = LEG_QTY[k]
        m = plan["mark"][k]
        if model == "mark":
            iv = m - hs if q < 0 else m + hs
        else:
            side = plan["sell"][k] if q < 0 else plan["buy"][k]
            if side is None:
                if model == "side_only":
                    return [], 0
                fb += 1
                iv = m - hs if q < 0 else m + hs
            else:
                iv = side
        out.append((plan["K"][k], LEG_IS_CALL[k], q, iv))
    return out, fb


def settle_condor(
    days: dict[int, dict[str, Any]],
    plan: dict[str, Any],
    legs: list[tuple[float, bool, float, float]],
    delivery_price: float,
    *,
    option_fee: float = 0.0003,
    delivery_fee: float = 0.00015,
) -> dict[str, Any] | None:
    """Hold-to-delivery P&L of the condor (no hedge, no funding) via `book_path`, plus ``max_loss`` = side width / S
    minus the net premium (fractions of entry spot); None if an execution IV is <= 1 %."""
    if not legs or any(iv <= 0.01 for *_, iv in legs):
        return None
    r = book_path(
        days,
        plan["day"],
        plan["expiry"],
        legs,
        delivery_price,
        option_fee=option_fee,
        delivery_fee=delivery_fee,
        hedge_fee=0.0,
        hedge=False,
    )
    if r is None:
        return None
    r["max_loss"] = plan["width"] / plan["spot"] - r["premium"]
    side_pnl = {"put": 0.0, "call": 0.0}
    for K, is_call, q, _ in legs:
        intrinsic = max(delivery_price - K, 0.0) if is_call else max(K - delivery_price, 0.0)
        side_pnl["call" if is_call else "put"] += q * intrinsic / plan["spot"]
    r["payoff_put"], r["payoff_call"] = side_pnl["put"], side_pnl["call"]
    return r


def dvol_size(p: float) -> float:
    """Registered H4 risk multiplier: 0.5 below the 40th DVOL percentile, 1.0 up to the 80th, 1.5 from the 80th."""
    return 0.5 if p < 0.4 else (1.0 if p < 0.8 else 1.5)


def scaled_minus_equal(pnls: Sequence[float], sizes: Sequence[float], s_bar: float) -> list[float]:
    """Per-week (s_t / s_bar - 1) * pnl_t: the scaled book (same average size as the equal one) minus the equal book."""
    return [(s / s_bar - 1.0) * p for p, s in zip(pnls, sizes, strict=True)]


def block_bootstrap_mean(values: Sequence[float], *, block: int = 4, n_boot: int = 10_000, seed: int = 20261031) -> dict[str, Any]:
    """Mean of a time-ordered series, moving-block bootstrap 95 % percentile CI and two-sided p = 2 min(P(<=0), P(>=0))."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": float("nan"), "sd": float("nan"), "ci": None, "p": float("nan")}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    b = max(1, min(block, n))
    starts = n - b + 1
    k = math.ceil(n / b)
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        s = 0.0
        cnt = 0
        for _ in range(k):
            i = rng.randrange(starts)
            take = min(b, n - cnt)
            s += sum(values[i : i + take])
            cnt += take
        draws.append(s / n)
    draws.sort()
    lo, hi = draws[int(0.025 * (n_boot - 1))], draws[int(0.975 * (n_boot - 1))]
    le = sum(1 for d in draws if d <= 0) / n_boot
    ge = sum(1 for d in draws if d >= 0) / n_boot
    return {"n": n, "mean": mean, "sd": sd, "ci": (lo, hi), "p": min(1.0, 2.0 * min(le, ge))}


def cvar(values: Sequence[float], q: float = 0.05) -> float:
    """Mean of the worst ceil(q n) values (expected shortfall, same sign as the values)."""
    s = sorted(values)
    k = max(1, math.ceil(q * len(s)))
    return sum(s[:k]) / k


def ruin_gates(worst: float, ci_hi: float) -> dict[str, bool]:
    """Registered ruin gates (True = triggered = fail), fractions of spot on 1 BTC notional.

    (d) of weekly-straddle-vrp: worst week < -8 % while the CI upper bound of the mean < +0.5 %;
    (d') for capped structures: worst-week loss >= 16 x max(CI upper bound, 0) (16 = 8 % / 0.5 %).
    """
    return {"d": worst < -0.08 and ci_hi < 0.005, "d_prime": -worst >= 16.0 * max(ci_hi, 0.0)}


def capital_paths(
    returns: Sequence[float], *, horizon: int = 52, block: int = 4, n_paths: int = 10_000, seed: int = 20261031, dd_limit: float = 0.15
) -> dict[str, float]:
    """Block-bootstrap ``horizon``-week compounded paths of weekly returns on capital: P(max drawdown > dd_limit),
    median and 5th percentile of the horizon return."""
    n = len(returns)
    if n == 0:
        return {"p_dd": float("nan"), "median": float("nan"), "p05": float("nan")}
    b = max(1, min(block, n))
    rng = random.Random(seed)
    hits, finals = 0, []
    for _ in range(n_paths):
        eq, peak, mdd, cnt = 1.0, 1.0, 0.0, 0
        while cnt < horizon:
            i = rng.randrange(n - b + 1)
            for r in returns[i : i + min(b, horizon - cnt)]:
                eq *= 1.0 + r
                peak = max(peak, eq)
                mdd = max(mdd, 1.0 - eq / peak)
                cnt += 1
        hits += mdd > dd_limit
        finals.append(eq - 1.0)
    finals.sort()
    return {"p_dd": hits / n_paths, "median": finals[n_paths // 2], "p05": finals[int(0.05 * (n_paths - 1))]}


def permutation_band(
    pnls: Sequence[float], sizes: Sequence[float], s_bar: float, *, n_perm: int = 1000, seed: int = 20261031
) -> tuple[float, float]:
    """2.5 / 97.5 percentiles of mean((s_pi / s_bar - 1) pnl) over random permutations of the sizes between weeks."""
    rng = random.Random(seed)
    sz = list(sizes)
    vals = []
    for _ in range(n_perm):
        rng.shuffle(sz)
        d = scaled_minus_equal(pnls, sz, s_bar)
        vals.append(sum(d) / len(d))
    vals.sort()
    return vals[int(0.025 * (n_perm - 1))], vals[int(0.975 * (n_perm - 1))]
