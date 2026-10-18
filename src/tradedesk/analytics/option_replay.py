"""Historical replay of option-structure payoffs: the assistant's recommendation method (IDEA-20261018-assistant).

The method being tested: take an option structure priced today (strikes as K/F, entry vols), replay its
expiry payoff over every past window of the same length that started in a similar regime, net of fees,
and rank structures by mean P&L per unit of risk. Pure functions, no I/O; time enters as indices into
a daily close series, so a caller decides what was known when.

Units and conventions:
  * vols are decimals (0.45 = 45 %), T in years = days / 365, Black-76 with r = 0;
  * a leg is ``(k_rel, is_call, qty, iv)``: strike as a fraction of the entry forward, qty > 0 bought,
    qty < 0 sold (per 1 unit of underlying notional);
  * premiums and P&L are fractions of the entry forward (0.01 = 1 % of F), on notional, not on capital;
  * a historical window starting at close index ``s`` with horizon ``h`` days uses the relative path
    ``closes[s + d] / closes[s]`` for d = 0..h as the forward path (zero drift between forward and spot);
  * realized vol is zero-mean ``sqrt(365 * mean(r^2))`` of daily log returns (`vrp.realized_vol`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from .options import black76_greeks, black76_price
from .vrp import realized_vol

Leg = tuple[float, bool, float, float]  # (K/F, is_call, qty, iv)


# ----------------------------------------------------------------------------- pricing and payoffs


def structure_premium(legs: Sequence[Leg], t_years: float) -> float:
    """Net premium paid (positive = debit) as a fraction of F, Black-76 at each leg's iv."""
    return sum(q * black76_price(1.0, k, t_years, iv, c) for k, c, q, iv in legs)


def payoff_rel(legs: Sequence[Leg], m: float) -> float:
    """Expiry payoff (fraction of entry F) when the underlying ends at ``m`` times the entry forward."""
    return sum(q * (max(m - k, 0.0) if c else max(k - m, 0.0)) for k, c, q, _ in legs)


def round_trip_fees(legs: Sequence[Leg], fee_per_leg: float = 0.0003) -> float:
    """Fees as a fraction of F: ``fee_per_leg`` per unit of each leg on entry and again on exit."""
    return 2.0 * fee_per_leg * sum(abs(q) for _, _, q, _ in legs)


def replay_unhedged(legs: Sequence[Leg], premium: float, m: float, *, fee_per_leg: float = 0.0003) -> float:
    """Held-to-expiry P&L of the structure on one historical window (fraction of F)."""
    return payoff_rel(legs, m) - premium - round_trip_fees(legs, fee_per_leg)


def replay_hedged(
    legs: Sequence[Leg],
    premium: float,
    path: Sequence[float],
    *,
    fee_per_leg: float = 0.0003,
    hedge_fee: float = 0.0005,
) -> float:
    """Held-to-expiry P&L with a daily delta hedge along ``path`` (relative forward, ``path[0]`` = 1).

    The structure lives ``len(path) - 1`` days. At each day d < h the hedge is reset to minus the book's
    Black-76 delta at forward ``path[d]``, time ``(h - d) / 365`` and each leg's own (constant) iv; the
    hedge pays ``hedge_fee`` on traded notional, including the final unwind at expiry.
    """
    h = len(path) - 1
    if h < 1:
        raise ValueError("path must span at least one day")
    pos, hedge_pnl, cost = 0.0, 0.0, 0.0
    for d in range(h):
        f, t = path[d], (h - d) / 365.0
        delta = sum(q * black76_greeks(f, k, t, iv, c)["delta"] for k, c, q, iv in legs)
        target = -delta
        cost += hedge_fee * abs(target - pos) * f
        pos = target
        hedge_pnl += pos * (path[d + 1] - f)
    cost += hedge_fee * abs(pos) * path[h]
    return payoff_rel(legs, path[h]) - premium - round_trip_fees(legs, fee_per_leg) + hedge_pnl - cost


# ----------------------------------------------------------------------------- windows and regimes


def rv_at(closes: Sequence[float], i: int, n: int = 30) -> float | None:
    """Trailing ``n``-day realized vol known at close ``i`` (uses closes i-n..i only)."""
    if i < n or i >= len(closes):
        return None
    return realized_vol(list(closes[i - n : i + 1]))


def window_starts(end: int, h: int, keep: Callable[[int], bool], *, first: int = 30) -> list[int]:
    """Start indices s >= ``first`` with ``s + h <= end`` (the window is complete by close ``end``) and ``keep(s)``."""
    return [s for s in range(first, end - h + 1) if keep(s)]


def non_overlapping(starts: Sequence[int], h: int, offset: int = 0) -> list[int]:
    """Greedy chain of starts at least ``h`` apart, beginning at the first start >= ``starts[0] + offset``."""
    out: list[int] = []
    if not starts:
        return out
    nxt = starts[0] + offset
    for s in starts:
        if s >= nxt:
            out.append(s)
            nxt = s + h
    return out


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (q in [0, 1]) of a non-empty sequence."""
    xs = sorted(values)
    if not xs:
        raise ValueError("empty")
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mean_ci(values: Sequence[float]) -> tuple[float, float, float] | None:
    """Mean and normal 95 % CI (mean +- 1.96 SE), as the prototype reports for independent windows."""
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))
    se = sd / math.sqrt(n)
    return m, m - 1.96 * se, m + 1.96 * se


def summarize_replay(all_pnl: Sequence[float], indep_pnl: Sequence[float]) -> dict[str, Any]:
    """The prototype's summary: mean / win share / 5 % quantile over all windows, CI on independent ones.

    ``score`` = mean / |5 % quantile| (mean P&L per unit of tail risk) is the method's ranking key.
    """
    if not all_pnl:
        return {"n": 0, "n_indep": len(indep_pnl)}
    mean = sum(all_pnl) / len(all_pnl)
    q05 = quantile(all_pnl, 0.05)
    ci = mean_ci(indep_pnl)
    return {
        "n": len(all_pnl),
        "n_indep": len(indep_pnl),
        "mean": mean,
        "win": sum(1 for v in all_pnl if v > 0) / len(all_pnl),
        "q05": q05,
        "mean_indep": ci[0] if ci else None,
        "ci_lo": ci[1] if ci else None,
        "ci_hi": ci[2] if ci else None,
        "score": mean / abs(q05) if q05 < 0 else (math.inf if mean > 0 else 0.0),
    }


def replay_structure(
    legs: Sequence[Leg],
    closes: Sequence[float],
    h: int,
    starts: Sequence[int],
    *,
    t_years: float | None = None,
    fee_per_leg: float = 0.0003,
    hedged: bool = False,
    hedge_fee: float = 0.0005,
    premium: float | None = None,
) -> dict[int, float]:
    """P&L of one structure (priced now) on each historical window ``closes[s .. s + h]``, keyed by start.

    ``premium`` (fraction of F) overrides the Black-76 premium at the legs' ivs, e.g. with quoted marks.
    """
    t = h / 365.0 if t_years is None else t_years
    prem = structure_premium(legs, t) if premium is None else premium
    out: dict[int, float] = {}
    for s in starts:
        if s + h >= len(closes) or closes[s] <= 0:
            continue
        if hedged:
            path = [closes[s + d] / closes[s] for d in range(h + 1)]
            out[s] = replay_hedged(legs, prem, path, fee_per_leg=fee_per_leg, hedge_fee=hedge_fee)
        else:
            out[s] = replay_unhedged(legs, prem, closes[s + h] / closes[s], fee_per_leg=fee_per_leg)
    return out


def method_summary(
    legs: Sequence[Leg],
    closes: Sequence[float],
    h: int,
    starts: Sequence[int],
    *,
    t_years: float | None = None,
    fee_per_leg: float = 0.0003,
    hedged: bool = False,
    premium: float | None = None,
) -> dict[str, Any]:
    """Replay over ``starts`` and summarize (independent windows: greedy chain, offset 0)."""
    res = replay_structure(legs, closes, h, starts, t_years=t_years, fee_per_leg=fee_per_leg, hedged=hedged, premium=premium)
    keys = sorted(res)
    indep = non_overlapping(keys, h)
    return summarize_replay([res[s] for s in keys], [res[s] for s in indep])


def recommend(summaries: dict[str, dict[str, Any]], *, require_ci: bool = True) -> str | None:
    """The method's pick: highest ``score`` among structures with mean > 0 (and CI lower bound > 0)."""
    best: tuple[float, str] | None = None
    for name, s in summaries.items():
        if not s.get("n") or s["mean"] <= 0:
            continue
        if require_ci and (s.get("ci_lo") is None or s["ci_lo"] <= 0):
            continue
        key = (s["score"], name)
        if best is None or key > best:
            best = key
    return best[1] if best else None


# ----------------------------------------------------------------------------- structures from a surface


def strike_by_delta(exp: dict[str, Any], target: float, is_call: bool, band: tuple[float, float]) -> tuple[float, float] | None:
    """(strike, mark iv) of the fresh-marked strike whose |delta| is closest to ``target`` within ``band``.

    ``exp`` is one expiry of a `put_skew.build_day` surface (strikes carry ``mark_iv`` and ``put_delta``
    at the mark). Call delta = 1 + put delta. Calls are taken above the forward, puts below.
    """
    best = None
    for k, v in exp["strikes"].items():
        if is_call and k < exp["F"] or not is_call and k > exp["F"]:
            continue
        d = 1.0 + v["put_delta"] if is_call else -v["put_delta"]
        if not band[0] <= d <= band[1]:
            continue
        key = (abs(d - target), k)
        if best is None or key < best[0]:
            best = (key, (float(k), float(v["mark_iv"])))
    return best[1] if best else None


def atm_strike(exp: dict[str, Any], band: tuple[float, float] = (0.40, 0.60)) -> tuple[float, float] | None:
    """(strike, mark iv) of the fresh strike nearest the forward if its call delta is within ``band``."""
    st = exp["strikes"]
    if not st:
        return None
    k = min(st, key=lambda K: (abs(K - exp["F"]), K))
    cd = 1.0 + st[k]["put_delta"]
    return (float(k), float(st[k]["mark_iv"])) if band[0] <= cd <= band[1] else None


STRUCTURES = {
    # name: [(strike role, is_call, qty)] ; roles: atm, c25, p25, c10, p10
    "long_straddle": [("atm", True, 1.0), ("atm", False, 1.0)],
    "short_straddle": [("atm", True, -1.0), ("atm", False, -1.0)],
    "long_strangle25": [("c25", True, 1.0), ("p25", False, 1.0)],
    "short_strangle25": [("c25", True, -1.0), ("p25", False, -1.0)],
    "iron_condor": [("c25", True, -1.0), ("p25", False, -1.0), ("c10", True, 1.0), ("p10", False, 1.0)],
    "long_strangle10": [("c10", True, 1.0), ("p10", False, 1.0)],
    "reverse_iron_fly": [("atm", True, 1.0), ("atm", False, 1.0), ("c25", True, -1.0), ("p25", False, -1.0)],
    "call_debit_spread": [("atm", True, 1.0), ("c25", True, -1.0)],
    "put_debit_spread": [("atm", False, 1.0), ("p25", False, -1.0)],
}


def strike_roles(exp: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Strikes and mark ivs by role (atm, c25, p25, c10, p10); a role is missing when no fresh strike fits."""
    out: dict[str, tuple[float, float]] = {}
    a = atm_strike(exp)
    if a:
        out["atm"] = a
    for role, tgt, c, band in (
        ("c25", 0.25, True, (0.18, 0.32)),
        ("p25", 0.25, False, (0.18, 0.32)),
        ("c10", 0.10, True, (0.05, 0.15)),
        ("p10", 0.10, False, (0.05, 0.15)),
    ):
        x = strike_by_delta(exp, tgt, c, band)
        if x:
            out[role] = x
    return out


def build_legs(
    name: str, roles: dict[str, tuple[float, float]], forward: float, *, spread_vol: float = 0.01
) -> tuple[list[tuple[float, bool, float, float]], list[Leg]] | None:
    """Absolute legs ``(K, is_call, qty, exec_iv)`` and relative legs ``(K/F, ...)`` for a named structure.

    Bought legs execute at mark + ``spread_vol``, sold legs at mark - ``spread_vol`` (floored at 1 vol pt).
    Returns None if a role is missing or two roles collapse onto the same strike where they must differ.
    """
    spec = STRUCTURES[name]
    need = {r for r, _, _ in spec}
    if not need <= roles.keys():
        return None
    ks = [roles[r][0] for r in sorted(need)]
    if len(set(ks)) != len(ks):
        return None
    absl, rel = [], []
    for role, c, q in spec:
        k, iv = roles[role]
        ex = max(iv + spread_vol if q > 0 else iv - spread_vol, 0.01)
        absl.append((k, c, q, ex))
        rel.append((k / forward, c, q, ex))
    return absl, rel


# ----------------------------------------------------------------------------- statistics


def spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Spearman rank correlation (average ranks for ties)."""
    n = len(pairs)
    if n < 3:
        return None

    def ranks(xs: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: xs[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0
            for t in range(i, j + 1):
                r[order[t]] = avg
            i = j + 1
        return r

    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)
