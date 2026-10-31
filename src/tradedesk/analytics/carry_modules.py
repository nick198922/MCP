"""Strategy-A carry modules (card structural-edges-depth, item 3): rank functions, exit threshold, rotation with
hysteresis, spot-as-collateral capital model, and paired-difference statistics.

Pure functions, no I/O; time is a parameter. Rates and returns are decimal fractions (0.10 = 10 %). The frozen
paper engines (``carry_rules``) are reused unchanged: the walk below calls the same open / mark / trade primitives,
so with ``rank="apr7"``, ``exit_apr=0`` and ``rotation_gap=None`` it reproduces ``simulate_carry_portfolio``.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from tradedesk.analytics.carry import DAY_MS, YEAR_MS, _Market, carry_observers
from tradedesk.analytics.carry_costs import week_start
from tradedesk.analytics.carry_rules import (
    DECISION_DELAY_MS,
    carry_mark,
    carry_open,
    carry_pnl_now,
    carry_side_cost,
    carry_trade,
)

WEEKS_PER_YEAR = 52.1775
RANKS = ("apr7", "min7_30", "median30", "random")


# ---------- rank scores (H1) ----------


def apr_available(m: _Market, t: int, lookback_ms: int, min_ms: int) -> float | None:
    """Annualised funding over ``(t - W, t]`` with ``W = min(lookback, t - first payment)``; None if ``W < min_ms``.

    For a new listing this is the APR since listing, so a 30-day score exists once a 7-day one does."""
    if not m.f_ts:
        return None
    w = min(lookback_ms, t - m.f_ts[0])
    if w < min_ms:
        return None
    hi = bisect.bisect_right(m.f_ts, t)
    lo = bisect.bisect_right(m.f_ts, t - w)
    return (m.prefix[hi] - m.prefix[lo]) * YEAR_MS / w


def median_apr(m: _Market, t: int, lookback_ms: int, min_n: int = 3) -> float | None:
    """Median of per-payment annualised rates (rate x year / interval to the previous payment) in ``(t - lookback, t]``."""
    hi = bisect.bisect_right(m.f_ts, t)
    lo = max(bisect.bisect_right(m.f_ts, t - lookback_ms), 1)
    xs = [m.f_rate[i] * YEAR_MS / (m.f_ts[i] - m.f_ts[i - 1]) for i in range(lo, hi) if m.f_ts[i] > m.f_ts[i - 1]]
    return statistics.median(xs) if len(xs) >= min_n else None


def rank_score(kind: str, m: _Market, t: int, apr7: float, rng: random.Random | None = None) -> float | None:
    """Score that orders entry candidates (higher first). ``apr7`` is the trailing 7-day APR (the entry gate)."""
    if kind == "apr7":
        return apr7
    if kind == "min7_30":
        a30 = apr_available(m, t, 30 * DAY_MS, 7 * DAY_MS)
        return None if a30 is None else min(apr7, a30)
    if kind == "median30":
        return median_apr(m, t, 30 * DAY_MS)
    if kind == "random":
        if rng is None:
            raise ValueError("random rank needs rng")
        return rng.random()
    raise ValueError(f"unknown rank {kind!r}")


# ---------- the walk (H1, H7) ----------


def simulate_carry_modules(
    markets: Mapping[str, dict[str, Any]],
    *,
    slots: int = 5,
    entry_apr: float = 0.20,
    exit_apr: float = 0.0,
    rank: str = "apr7",
    rotation_gap: float | None = None,
    seed: int = 0,
    spot_fee: float = 0.001,
    perp_fee: float = 0.0005,
    slippage: float = 0.0005,
    step_hours: float = 8.0,
    bar_ms: int = 4 * 3_600_000,
) -> dict[str, Any]:
    """Strategy A (cash-and-carry, ``slots`` equal slots, 7-day trailing APR gate) with module switches.

    - ``rank``: order of entry candidates passing ``APR7 >= entry_apr`` ("apr7" = base; "min7_30" = min(APR7, APR30)
      with APR30 over the available history; "median30" = median of per-payment annualised rates over 30 days;
      "random" = placebo, a seeded random order among the same candidates). A candidate without a score is skipped.
    - ``exit_apr``: close when the trailing 7-day APR <= this (base 0).
    - ``rotation_gap``: when all slots are full, replace the held position with the lowest score by the best
      non-held candidate if ``score(candidate) - score(held) > rotation_gap`` (repeatedly, each symbol at most once
      per cycle). None = no rotation (base). Random rank never rotates.

    Same accounting as ``simulate_carry_portfolio``: per-side cost ``spot_fee + perp_fee + 2 x slippage``, returns as
    fractions of total notional (slot = 1 / slots), equity = realised + open marked (entry costs only).
    Trades carry ``reason`` ("exit", "rotation", "end").
    """
    if rank not in RANKS:
        raise ValueError(f"unknown rank {rank!r}")
    ms = {s: _Market(d, bar_ms) for s, d in markets.items() if d.get("funding") and d.get("spot") and d.get("perp")}
    if not ms:
        return {"trades": [], "equity": [], "stats": {}}
    lookback_ms = 7 * DAY_MS
    step = int(step_hours * 3_600_000)
    start = min(m.f_ts[0] for m in ms.values()) + lookback_ms
    start = (start // step + 1) * step + DECISION_DELAY_MS
    end = max(m.f_ts[-1] for m in ms.values())
    side_cost = carry_side_cost(spot_fee, perp_fee, slippage)
    w = 1.0 / slots
    rng = random.Random(seed)
    mark_obs, entry_obs = carry_observers(ms, lookback_ms)
    open_pos: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity: list[tuple[int, float]] = []
    realized = 0.0
    n_rot = 0
    t = start
    while t <= end:
        for sym in list(open_pos):
            p = open_pos[sym]
            o = mark_obs(sym, p, t)
            carry_mark(p, t, payments=o["payments"], perp_high=o["perp_high"], spot=o["spot"], perp=o["perp"])
            a = o["apr"]
            p["apr_now"] = a
            if a is not None and a <= exit_apr:
                tr = carry_trade(p, t, side_cost, at_end=False)
                realized += w * tr["pnl"]
                trades.append({**tr, "reason": "exit"})
                del open_pos[sym]
        cands: list[tuple[float, str, float, float, float]] = []
        for sym in ms:
            if sym in open_pos:
                continue
            a, sp, pp = entry_obs(sym, t)
            if a is None or a < entry_apr or not sp or not pp:
                continue
            sc = rank_score(rank, ms[sym], t, a, rng)
            if sc is not None:
                cands.append((sc, sym, a, sp, pp))
        cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
        free = slots - len(open_pos)
        for _sc, sym, a, sp, pp in cands[: max(free, 0)]:
            open_pos[sym] = carry_open(sym, t, a, sp, pp, side_cost)
        rest = cands[max(free, 0) :]
        if rotation_gap is not None and rank != "random" and rest and len(open_pos) >= slots:
            touched: set[str] = set()
            held: list[tuple[float, str]] = []
            for sym, p in open_pos.items():
                if p["open_ts"] == t or p.get("apr_now") is None:
                    continue
                sc = rank_score(rank, ms[sym], t, p["apr_now"])
                if sc is not None:
                    held.append((sc, sym))
            held.sort()
            for sc, sym, a, sp, pp in rest:
                if not held:
                    break
                h_sc, h_sym = held[0]
                if sc - h_sc <= rotation_gap or sym in touched:
                    break
                tr = carry_trade(open_pos.pop(h_sym), t, side_cost, at_end=False)
                realized += w * tr["pnl"]
                trades.append({**tr, "reason": "rotation"})
                open_pos[sym] = carry_open(sym, t, a, sp, pp, side_cost)
                touched.update((sym, h_sym))
                held.pop(0)
                n_rot += 1
        equity.append((t, realized + sum(w * carry_pnl_now(p) for p in open_pos.values())))
        t += step
    for p in list(open_pos.values()):
        o = mark_obs(p["symbol"], p, end)
        carry_mark(p, end, payments=o["payments"], perp_high=o["perp_high"], spot=o["spot"], perp=o["perp"])
        tr = carry_trade(p, p["last"], side_cost, at_end=True)
        realized += w * tr["pnl"]
        trades.append({**tr, "reason": "end"})
    if equity:
        equity[-1] = (equity[-1][0], realized)
    years = max(end - start, 1) / YEAR_MS
    occupied = sum(tr["close_ts"] - tr["open_ts"] for tr in trades) / (max(end - start, 1) * slots)
    return {
        "trades": trades,
        "equity": equity,
        "stats": {
            "years": years,
            "start_ms": start,
            "end_ms": end,
            "apr_on_notional": realized / years,
            "total_return": realized,
            "n_trades": len(trades),
            "n_rotations": n_rot,
            "slot_utilisation": occupied,
            "funding": w * math.fsum(tr["funding"] for tr in trades),
            "basis": w * math.fsum(tr["basis"] for tr in trades),
            "costs": w * math.fsum(tr["fees"] for tr in trades),
        },
    }


# ---------- capital model (H4) ----------


def collateral_buffer(h: float, *, im: float = 0.5, mmr: float = 0.05, move: float = 0.25, premium: float = 0.10) -> float:
    """Cash needed per unit of notional beside the spot leg when spot counts as perp collateral at ``1 - h``.

    Two conditions, both as fractions of entry notional: at entry, collateral covers the perp initial margin
    (``(1 - h) + B >= im``); after a perp move of ``+move`` with the spot lagging by ``premium`` (spot up
    ``move - premium``), equity ``(1 - h)(1 + move - premium) + B - move`` covers maintenance ``mmr (1 + move)``.
    """
    need_open = im - (1.0 - h)
    need_stress = move + mmr * (1.0 + move) - (1.0 - h) * (1.0 + move - premium)
    return max(0.0, need_open, need_stress)


def capital_factor(h: float | None, **kw: float) -> float:
    """Capital per unit of notional: spot (1) + buffer; ``h=None`` = the coin is not collateral -> 1 + im."""
    if h is None:
        return 1.0 + kw.get("im", 0.5)
    return 1.0 + collateral_buffer(h, **kw)


def tiered_haircut(tiers: Sequence[Mapping[str, Any]], qty: float) -> float | None:
    """Effective haircut ``1 - value / qty`` of ``qty`` coins under a tiered collateral table (Bybit
    ``collateralRatioList``: ``minQty``, ``maxQty`` ("" = no cap), ``collateralRatio``), valued piecewise like tax
    brackets. None when the table is empty or ``qty <= 0``."""
    if not tiers or qty <= 0:
        return None
    value = 0.0
    for tr in sorted(tiers, key=lambda x: float(x["minQty"] or 0)):
        lo = float(tr["minQty"] or 0)
        hi = float(tr["maxQty"]) if str(tr.get("maxQty", "")).strip() else math.inf
        if qty <= lo:
            break
        value += (min(qty, hi) - lo) * float(tr["collateralRatio"])
    return 1.0 - value / qty


# ---------- statistics ----------


def window_weekly(equity: Sequence[tuple[int, float]], from_ms: int, to_ms: int | None = None) -> list[tuple[int, float]]:
    """Calendar-week changes (Mon-Sun UTC) of the curve over ``[from_ms, to_ms)``, the first week measured from the
    last value before ``from_ms`` (0 if none). Uses only points before each week's end."""
    base = 0.0
    out: list[tuple[int, float]] = []
    cur_w: int | None = None
    last = 0.0
    for ts, v in equity:
        if ts < from_ms:
            base = v
            continue
        if to_ms is not None and ts >= to_ms:
            break
        wk = week_start(ts)
        if cur_w is None:
            cur_w, last = wk, v
            prev = base
            continue
        if wk != cur_w:
            out.append((cur_w, last - prev))
            prev = last
        cur_w, last = wk, v
    if cur_w is not None:
        out.append((cur_w, last - prev))
    return out


def diff_curve(eq_a: Sequence[tuple[int, float]], eq_b: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """``a - b`` on the timestamps both curves share (paired simulations on the same grid)."""
    vb = dict(eq_b)
    return [(t, v - vb[t]) for t, v in eq_a if t in vb]


def _q(xs: Sequence[float], q: float) -> float:
    i = q * (len(xs) - 1)
    lo = math.floor(i)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def block_bootstrap(
    weekly: Sequence[float], *, block: int = 4, n_boot: int = 10_000, seed: int = 20261031, alpha: float = 0.05
) -> dict[str, Any]:
    """Annualised mean of weekly values, moving-block bootstrap CI and one-sided p = share of draws <= 0."""
    n = len(weekly)
    if n == 0:
        return {"weeks": 0, "apr": None, "lo": None, "hi": None, "p": None}
    b = max(1, min(block, n))
    starts = n - b + 1
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        draw: list[float] = []
        while len(draw) < n:
            i = rng.randrange(starts)
            draw.extend(weekly[i : i + b])
        boots.append(math.fsum(draw[:n]) / n * WEEKS_PER_YEAR)
    boots.sort()
    return {
        "weeks": n,
        "apr": math.fsum(weekly) / n * WEEKS_PER_YEAR,
        "lo": _q(boots, alpha / 2),
        "hi": _q(boots, 1 - alpha / 2),
        "p": sum(1 for x in boots if x <= 0) / n_boot,
    }


def cluster_bootstrap_total(
    by_cluster: Mapping[str, float], years: float, *, n_boot: int = 10_000, seed: int = 20261031, alpha: float = 0.05
) -> dict[str, Any]:
    """Annualised total over clusters (symbols) with a cluster bootstrap CI and one-sided p = share of draws <= 0."""
    # Sorted by key: the draw sequence must not depend on insertion / set order (PYTHONHASHSEED).
    xs = [by_cluster[c] for c in sorted(by_cluster)]
    k = len(xs)
    if k == 0 or years <= 0:
        return {"clusters": k, "apr": None, "lo": None, "hi": None, "p": None}
    rng = random.Random(seed)
    boots = sorted(math.fsum(xs[rng.randrange(k)] for _ in range(k)) / years for _ in range(n_boot))
    return {
        "clusters": k,
        "apr": math.fsum(xs) / years,
        "lo": _q(boots, alpha / 2),
        "hi": _q(boots, 1 - alpha / 2),
        "p": sum(1 for x in boots if x <= 0) / n_boot,
    }


def holm(pvals: Mapping[str, float], alpha: float = 0.025) -> dict[str, dict[str, Any]]:
    """Holm step-down over one-sided p-values: adjusted p and pass at ``alpha`` (0.025 one-sided = 95 % two-sided)."""
    order = sorted(pvals, key=lambda k: pvals[k])
    m = len(order)
    out: dict[str, dict[str, Any]] = {}
    run = 0.0
    stop = False
    for i, k in enumerate(order):
        adj = min(1.0, max(run, (m - i) * pvals[k]))
        run = adj
        ok = not stop and pvals[k] <= alpha / (m - i)
        stop = stop or not ok
        out[k] = {"p": pvals[k], "p_holm": adj, "pass": ok}
    return out


def pnl_by_symbol(
    trades: Sequence[Mapping[str, Any]], weight: float, opened_from: int = 0, opened_to: int | None = None
) -> dict[str, float]:
    """Slot-weighted trade P&L per symbol for trades opened in ``[opened_from, opened_to)``."""
    out: dict[str, float] = {}
    for tr in trades:
        if tr["open_ts"] < opened_from or (opened_to is not None and tr["open_ts"] >= opened_to):
            continue
        out[tr["symbol"]] = out.get(tr["symbol"], 0.0) + weight * tr["pnl"]
    return out


def paired_symbol_diff(a: Mapping[str, float], b: Mapping[str, float]) -> dict[str, float]:
    return {s: a.get(s, 0.0) - b.get(s, 0.0) for s in sorted(set(a) | set(b))}


def apply_capital(equity: Sequence[tuple[int, float]], factor: Callable[[int], float] | float) -> list[tuple[int, float]]:
    """Curve on capital: each point divided by a constant capital factor (or ``factor(ts)``)."""
    if callable(factor):
        return [(t, v / factor(t)) for t, v in equity]
    return [(t, v / factor) for t, v in equity]
