"""Playbook rule P5' on history: low IV Rank of DVOL -> buy a 60-90-day ATM straddle (H1 of the DataHawks radar LIB;
IDEA-20261114-options-playbook, item 4.5).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Works on the day surfaces of `put_skew.build_day`
(``snap["expiries"][expiry_ms] = {"F", "T", "strikes": {K: {"mark_iv", "sell_iv", "buy_iv", ...}}}``; only strikes with
a fresh mark are present). Units: vols and ranks are decimals (0.45 = 45 %), T in years = days / 365, Black-76 with
r = 0 on the expiry's forward, prices and P&L in USD per 1 BTC per leg.

* The condition is the radar's IV Rank (`radar.iv_rank`) of the last CLOSED daily DVOL candle at the entry time.
* A plan has the leg layout of `calendar_p10` (``legs`` with ``exp="m"``, ``F`` / ``T`` keyed by ``"m"``), so its
  entry prices and fees come from `calendar_p10.exec_ivs` / `entry_cost`, and an early exit from `settle_at_front`.
* R = P&L / debit (premium paid at the bought IV plus entry fees): the return on the capital at risk of a long straddle
  (on Bybit the margin of a bought option is its premium).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from .calendar_p10 import clean_iv, settle_at_front
from .calendar_p10 import verdict as _verdict
from .condor_25d import tail_gate
from .radar import iv_rank
from .trend_spread import DAY_MS, DELIVERY_CAP, DELIVERY_RATE

YEAR_MS = 365 * DAY_MS


# ----------------------------------------------------------------------------- condition


def iv_rank_at(dvol: Sequence[tuple[int, float]], t_ms: int, *, window_days: int = 365) -> tuple[dict[str, Any] | None, str | None]:
    """`radar.iv_rank` of the newest daily DVOL candle CLOSED at ``t_ms`` (the candle of day D closes at D + 1 00:00 UTC).

    ``dvol``: [(day start ms, DVOL close as a DECIMAL)]; ranks are scale-free, the value is passed to `iv_rank` in
    percent as Deribit publishes it. Returns (value, None) or (None, reason); value has ``rank`` and ``percentile``.
    """
    day = (t_ms - DAY_MS) // DAY_MS * DAY_MS
    return iv_rank([(t, v * 100.0) for t, v in dvol if t <= day], day, window_days)


def dvol_change(dvol_by_day: dict[int, float], t_ms: int, days: int) -> float | None:
    """DVOL close ``days`` days after the candle known at ``t_ms`` minus that candle (decimals); None if either is missing."""
    day = (t_ms - DAY_MS) // DAY_MS * DAY_MS
    a, b = dvol_by_day.get(day), dvol_by_day.get(day + days * DAY_MS)
    return None if a is None or b is None else b - a


# ----------------------------------------------------------------------------- clusters


def overlap_clusters(spans: Sequence[tuple[int, int]]) -> list[int]:
    """Cluster index per trade for (entry_ms, expiry_ms) spans sorted by entry: a trade joins the current cluster while
    its entry is earlier than the latest expiry in that cluster (holding periods chain), else it opens a new one.

    The cluster of a trade depends only on the trades before it, so appending later trades never changes earlier ones.
    """
    out: list[int] = []
    cid, end, prev = -1, None, None
    for entry, expiry in spans:
        if prev is not None and entry < prev:
            raise ValueError("spans must be sorted by entry")
        if end is None or entry >= end:
            cid += 1
            end = expiry
        else:
            end = max(end, expiry)
        out.append(cid)
        prev = entry
    return out


# ----------------------------------------------------------------------------- construction and P&L


def plan_straddle(
    snap: dict[str, Any], expiry: int, *, band: float = 0.015, arb_rule: str = "replace"
) -> tuple[dict[str, Any] | None, str]:
    """Long ATM straddle on ``expiry``: K = the fresh strike nearest the expiry forward within ``band`` x spot; call and
    put at K's mark after the static-arbitrage rule (`calendar_p10.clean_iv`). (plan, reason)."""
    x = snap["expiries"].get(expiry)
    if x is None or not x["strikes"]:
        return None, "no expiry in window"
    S = snap["spot"]
    K = min(x["strikes"], key=lambda k: (abs(k - x["F"]), k))
    if abs(K - x["F"]) > band * S + 1e-9:
        return None, "no strike near the forward"
    iv, st = clean_iv(x, K, rule=arb_rule)
    if iv is None:
        return None, "arbitrage in marks"
    q = x["strikes"][K]
    legs = [
        {"exp": "m", "K": K, "call": c, "qty": 1.0, "mark": iv, "sell": q.get("sell_iv"), "buy": q.get("buy_iv"), "arb": st}
        for c in (True, False)
    ]
    return {
        "day": snap["day"],
        "t_ms": snap["t_ms"],
        "spot": S,
        "K": K,
        "iv": iv,
        "expiry": {"m": expiry},
        "F": {"m": x["F"]},
        "T": {"m": x["T"]},
        "legs": legs,
    }, ""


def settle_straddle(plan: dict[str, Any], debit: float, delivery_price: float) -> dict[str, Any]:
    """Hold to expiry: the ITM leg pays its intrinsic value at ``delivery_price`` less the Bybit delivery fee
    (rate x price, capped at a share of the intrinsic value)."""
    if debit <= 0:
        raise ValueError("debit must be > 0")
    v = abs(delivery_price - plan["K"])
    fee = min(DELIVERY_RATE * delivery_price, DELIVERY_CAP * v) if v > 0 else 0.0
    pnl = v - fee - debit
    return {
        "day": plan["day"],
        "pnl": pnl,
        "r": pnl / debit,
        "pnl_spot": pnl / plan["spot"],
        "debit": debit,
        "payoff": v,
        "expiry": plan["expiry"]["m"],
        "exit_ms": plan["expiry"]["m"],
        "exit_arb": [],
        "exit": "expiry",
    }


def sell_early(
    plan: dict[str, Any], debit: float, exit_snap: dict[str, Any], *, hs: float = 0.015, arb_rule: str = "replace"
) -> dict[str, Any] | None:
    """Sell both legs on ``exit_snap`` (before expiry) at mark - hs with taker fees (`calendar_p10.settle_at_front` with
    no front legs). None if the surface lacks the expiry or a mark."""
    p = {**plan, "expiry": {"f": exit_snap["t_ms"], "m": plan["expiry"]["m"]}}
    res, _ = settle_at_front(p, debit, 0.0, exit_snap, hs=hs, arb_rule=arb_rule)
    if res is None:
        return None
    return {**res, "expiry": plan["expiry"]["m"], "exit": "early"}


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def straddle_delta(F: float, K: float, T: float, sigma: float) -> float:
    """Black-76 delta of a call + put at K with respect to F (r = 0): 2 N(d1) - 1; the payoff's sign at T <= 0."""
    if T <= 0 or sigma <= 0:
        return 1.0 if F > K else (-1.0 if F < K else 0.0)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    return 2.0 * _ncdf(d1) - 1.0


def hedge_pnl(
    plan: dict[str, Any],
    path: Sequence[tuple[int, float]],
    delivery_price: float,
    *,
    fee_side: float,
    every_days: int = 1,
) -> dict[str, Any]:
    """Delta hedge of the long straddle in BTC (spot / perp) at the entry mark IV (constant).

    ``path``: [(t_ms, spot)] observed strictly after the entry and before the expiry, sorted. The hedge holds
    -Delta_F x F/S BTC, Delta_F = `straddle_delta` on F_i = S_i x F0 / S0 (entry basis), rebalanced at the entry and at
    path points at least ``every_days`` days after the previous rebalance; closed at ``delivery_price`` at expiry.
    Fees: ``fee_side`` x |traded BTC| x price at every trade. Funding is not modelled. Returns pnl, fees, rebalances.
    """
    t0, S0, e = plan["t_ms"], plan["spot"], plan["expiry"]["m"]
    basis = plan["F"]["m"] / S0
    K, iv = plan["K"], plan["iv"]

    def target(t: int, S: float) -> float:
        return -straddle_delta(S * basis, K, (e - t) / YEAR_MS, iv) * basis

    h = target(t0, S0)
    fees, pnl, n = fee_side * abs(h) * S0, 0.0, 1
    last_t, last_S = t0, S0
    for t, S in path:
        if not t0 < t < e:
            raise ValueError("path points must lie strictly between entry and expiry")
        if t < last_t:
            raise ValueError("path must be sorted")
        pnl += h * (S - last_S)
        last_S = S
        if t - last_t >= every_days * DAY_MS - 3_600_000:
            h2 = target(t, S)
            fees += fee_side * abs(h2 - h) * S
            h, last_t, n = h2, t, n + 1
    pnl += h * (delivery_price - last_S)
    fees += fee_side * abs(h) * delivery_price
    return {"pnl": pnl - fees, "gross": pnl, "fees": fees, "rebalances": n}


# ----------------------------------------------------------------------------- gates and verdict


def two_sided_tail_gate(values: Sequence[float], clusters: Sequence[Any], *, min_count: int = 5, max_p: float = 0.05) -> dict[str, Any]:
    """`condor_25d.tail_gate` on the losses (R <= 0) and, mirrored, on the wins (R >= 0): a long straddle's mean rests
    on rare large wins, so a sample with too few winning (or losing) clusters makes the bootstrap degenerate."""
    loss = tail_gate(values, clusters, min_losses=min_count, max_p_no_loss=max_p)
    win = tail_gate([-v for v in values], clusters, min_losses=min_count, max_p_no_loss=max_p)
    return {"ok": loss["ok"] and win["ok"], "n_loss": loss["n_loss"], "n_win": win["n_loss"], "loss": loss, "win": win}


def verdict(
    *,
    mean: float,
    lo: float,
    hi: float,
    holm_p: float,
    is_mean: float,
    oos_mean: float,
    stress_cost_mean: float,
    edge: float,
    tail_ok: bool,
    coverage_ok: bool,
) -> str:
    """Registered verdict (card 4.5): a failed coverage gate gives ◯ whatever the numbers say (also instead of ✗);
    otherwise `calendar_p10.verdict` (✓ / ≈ / ✗ / ◯, a ✓ or ≈ with a failed tail gate is ◯)."""
    if not coverage_ok:
        return "◯"
    return _verdict(
        mean=mean,
        lo=lo,
        hi=hi,
        holm_p=holm_p,
        is_mean=is_mean,
        oos_mean=oos_mean,
        stress_cost_mean=stress_cost_mean,
        edge=edge,
        tail_ok=tail_ok,
        coverage_ok=True,
    )
