"""Shared rule selection and accounting for the two carry strategies (pure: no I/O, time is a parameter).

Strategy A, cash-and-carry on alts: long spot + short perp on one venue.
Strategy B, perp-perp carry: long the perp where funding is lower, short it where it is higher.

``simulate_carry_portfolio`` / ``simulate_perp_spread_portfolio`` (the backtests in ``carry.py``) and the live
paper engine (``collector/carry_paper_engine.py``) both call ``carry_step`` / ``spread_step`` with their own
observation callbacks, so the decisions and the accounting of a trade are one code path. All returns are
fractions of slot notional (decimals); multiply by the slot notional for USD.

An observation callback returns, per symbol, what a decision at time ``t`` is allowed to know. ``None`` values
mean "data not available" and are treated exactly as the backtest treats a missing bar.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .orderbook import book_stats, walk_book

FUNDING_STEP_MS = 8 * 3_600_000
# Decisions are taken 5 minutes after each UTC-aligned funding time, so payments stamped a few ms late are settled.
DECISION_DELAY_MS = 300_000


def cycle_grid(now_ms: int) -> int:
    """Latest scheduled funding time (00/08/16 UTC) whose decision time (+5 min) has passed at ``now_ms``."""
    return (now_ms - DECISION_DELAY_MS) // FUNDING_STEP_MS * FUNDING_STEP_MS


def funding_gap_issue(
    f_ts: list[int], last: int, t: int, cycle_ts: int, interval_hours: float | None, *, slack_ms: int = 60_000
) -> str | None:
    """Why the funding history cannot be trusted to settle a held position over ``(last, t]``; None when it can.

    Settling advances the position's ``last`` to ``t``, so a payment missing now would be lost for good. Expected: the
    payment scheduled at ``cycle_ts`` is present, and consecutive payments from the last one at or before ``last``
    are no more than 1.5 intervals apart (the interval defaults to 8 h).
    """
    allowed = 1.5 * (interval_hours or 8.0) * 3_600_000
    prior = [x for x in f_ts if x <= last]
    seq = ([prior[-1]] if prior else [last]) + [x for x in f_ts if last < x <= t]
    if seq[-1] < cycle_ts - slack_ms:
        return "scheduled_payment_missing"
    if any(b - a > allowed for a, b in zip(seq, seq[1:], strict=False)):
        return "payment_gap"
    return None


def leg_execution(
    book: dict[str, Any] | None,
    side: str,
    notional_usd: float,
    *,
    fee: float,
    model_slippage: float,
    cycle_ts: int,
    max_levels: int = 50,
) -> dict[str, Any]:
    """Execution reality of one leg side against a recorded order book (``side`` is "buy" or "sell").

    ``actual_cost`` = taker fee + |VWAP - mid| / mid, where VWAP is a walk of the book on ``notional_usd``;
    ``model_cost`` = fee + ``model_slippage`` (the backtest's per-side, per-leg cost). When the recorded depth
    does not cover the notional, ``unfilled_usd`` > 0 and the cost is on the available depth only.
    """
    bids = [[float(x[0]), float(x[1])] for x in ((book or {}).get("bids") or [])][:max_levels]
    asks = [[float(x[0]), float(x[1])] for x in ((book or {}).get("asks") or [])][:max_levels]
    model_cost = fee + model_slippage
    out: dict[str, Any] = {
        "side": side,
        "notional_usd": notional_usd,
        "fee": fee,
        "model_slippage": model_slippage,
        "model_cost": model_cost,
    }
    if not bids or not asks:
        return {**out, "book_available": False, "actual_cost": None}
    st = book_stats(bids, asks)
    mid = st["mid"]
    walk = walk_book(asks if side == "buy" else bids, notional_usd, mid, side)
    vwap = walk["avg_price"]
    ts = (book or {}).get("timestamp")
    return {
        **out,
        "book_available": True,
        "bid": st["best_bid"],
        "ask": st["best_ask"],
        "mid": mid,
        "spread_bps": st["spread_bps"],
        "depth_usd": {k: {"bid": v["bid_usd"], "ask": v["ask_usd"]} for k, v in st["depth"].items()},
        "vwap": vwap,
        "unfilled_usd": walk["unfilled_usd"],
        "depth_short": walk["unfilled_usd"] > 1e-6,
        "actual_cost": None if vwap is None else fee + abs(vwap - mid) / mid,
        "book": {"bids": bids, "asks": asks},
        "book_ts_ms": ts,
        "book_latency_ms": None if ts is None else int(ts) - cycle_ts,
    }


# ---------- A: cash-and-carry ----------


def carry_side_cost(spot_fee: float, perp_fee: float, slippage: float) -> float:
    """Cost of one side (open or close) of both legs, fraction of notional: fees + slippage on each of the two legs."""
    return spot_fee + perp_fee + 2 * slippage


def carry_open(sym: str, t: int, apr: float, spot: float, perp: float, side_cost: float) -> dict[str, Any]:
    return {
        "symbol": sym,
        "open_ts": t,
        "entry_apr": apr,
        "spot_e": spot,
        "perp_e": perp,
        "spot_last": spot,
        "perp_last": perp,
        "funding": 0.0,
        "fees": side_cost,
        "last": t,
        "last_bar": t,
        "max_runup": 0.0,
        "max_premium_rise": 0.0,
    }


def carry_mark(
    p: dict[str, Any],
    t: int,
    *,
    payments: Iterable[tuple[int, float, float | None]],
    perp_high: float | None,
    spot: float | None,
    perp: float | None,
) -> None:
    """Accrue funding payments ``(ts, rate, perp price at ts or None)`` since ``p["last"]`` and update the extremes.

    A short perp receives ``rate x price`` per unit; a missing payment price falls back to the last known perp price.
    """
    for _ts, rate, price in payments:
        p["funding"] += rate * (price or p["perp_last"]) / p["spot_e"]
    p["last"] = t
    if perp_high is not None:
        p["max_runup"] = max(p["max_runup"], perp_high / p["perp_e"] - 1.0)
        p["last_bar"] = t
    if spot and perp:
        p["spot_last"], p["perp_last"] = spot, perp
        p["max_premium_rise"] = max(p["max_premium_rise"], perp / spot - p["perp_e"] / p["spot_e"])


def carry_basis(p: dict[str, Any]) -> float:
    return (p["spot_last"] - p["spot_e"] - (p["perp_last"] - p["perp_e"])) / p["spot_e"]


def carry_trade(p: dict[str, Any], t: int, side_cost: float, at_end: bool) -> dict[str, Any]:
    """Closed-trade record of position ``p`` closed at ``t`` (exit costs scale with the spot price move)."""
    fees = p["fees"] + side_cost * p["spot_last"] / p["spot_e"]
    basis = carry_basis(p)
    return {
        **{k: p[k] for k in ("symbol", "open_ts", "entry_apr", "funding", "max_runup", "max_premium_rise")},
        "close_ts": t,
        "basis": basis,
        "fees": fees,
        "pnl": p["funding"] + basis - fees,
        "open_at_end": at_end,
    }


def carry_pnl_now(p: dict[str, Any]) -> float:
    """Open-position P&L marked at the last prices, entry costs only (the equity-curve convention)."""
    return p["funding"] + carry_basis(p) - p["fees"]


def carry_exit_due(apr: float | None, exit_apr: float) -> bool:
    return apr is not None and apr <= exit_apr


def carry_rank(candidates: Iterable[tuple[str, float | None, float | None, float | None]], entry_apr: float) -> list[tuple[float, str]]:
    """Entry candidates ``(symbol, trailing APR, spot, perp)`` -> ``[(apr, symbol)]``, highest APR first.

    Needs APR >= ``entry_apr`` and both prices. Ties break by symbol descending, as the backtest always did.
    """
    ranked = [(a, s) for s, a, sp, pp in candidates if a is not None and a >= entry_apr and sp and pp]
    return sorted(ranked, reverse=True)


def carry_step(
    open_pos: dict[str, dict[str, Any]],
    t: int,
    universe: Iterable[str],
    mark_obs: Callable[[str, dict[str, Any]], dict[str, Any]],
    entry_obs: Callable[[str], tuple[float | None, float | None, float | None]],
    *,
    slots: int,
    entry_apr: float,
    exit_apr: float,
    side_cost: float,
) -> dict[str, Any]:
    """One decision cycle of strategy A at ``t``. Mutates ``open_pos`` (symbol -> position) and returns the outcome.

    ``mark_obs(sym, pos)`` -> ``{"payments", "perp_high", "spot", "perp", "apr"}`` for an open position;
    ``entry_obs(sym)`` -> ``(trailing APR, spot, perp)`` for a symbol that may be opened.
    Result: ``closed`` (trade records), ``opened`` (symbols), ``ranked`` ([(apr, symbol)] before slot cut),
    ``free_slots`` (before filling) and ``decisions`` (one dict per open/close/hold with the reason).
    """
    decisions: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for sym in list(open_pos):
        p = open_pos[sym]
        o = mark_obs(sym, p)
        carry_mark(p, t, payments=o["payments"], perp_high=o["perp_high"], spot=o["spot"], perp=o["perp"])
        a = o["apr"]
        if carry_exit_due(a, exit_apr):
            closed.append(carry_trade(p, t, side_cost, at_end=False))
            del open_pos[sym]
            decisions.append({"symbol": sym, "action": "close", "reason": "apr<=exit", "apr": a})
        else:
            decisions.append({"symbol": sym, "action": "hold", "reason": "apr>exit" if a is not None else "apr_unavailable", "apr": a})
    free = slots - len(open_pos)
    ranked: list[tuple[float, str]] = []
    opened: list[str] = []
    if free > 0:
        obs = {s: entry_obs(s) for s in universe if s not in open_pos}
        ranked = carry_rank(((s, *v) for s, v in obs.items()), entry_apr)
        for a, sym in ranked[:free]:
            _, spot, perp = obs[sym]
            open_pos[sym] = carry_open(sym, t, a, spot, perp, side_cost)  # type: ignore[arg-type]
            opened.append(sym)
            decisions.append({"symbol": sym, "action": "open", "reason": "apr>=entry", "apr": a})
    return {"closed": closed, "opened": opened, "ranked": ranked, "free_slots": max(free, 0), "decisions": decisions}


# ---------- B: perp-perp spread ----------


def spread_side_cost(fee_a: float, fee_b: float, slippage: float) -> float:
    return fee_a + fee_b + 2 * slippage


def spread_open(sym: str, t: int, d: int, spread: float, pa: float, pb: float, side_cost: float) -> dict[str, Any]:
    """``d`` = +1: long A / short B; -1: short A / long B. Slot notional is priced at leg A's entry."""
    return {
        "symbol": sym, "open_ts": t, "dir": d, "entry_spread": spread, "a_e": pa, "b_e": pb, "a_last": pa, "b_last": pb,
        "funding": 0.0, "fees": side_cost, "last": t, "last_bar": t, "max_up": 0.0, "max_down": 0.0, "max_gap": 0.0,
    }  # fmt: skip


def spread_mark(
    p: dict[str, Any],
    t: int,
    *,
    pay_a: Iterable[tuple[int, float, float | None]],
    pay_b: Iterable[tuple[int, float, float | None]],
    ext_a: tuple[float, float] | None,
    ext_b: tuple[float, float] | None,
    pa: float | None,
    pb: float | None,
) -> None:
    """Accrue payments ``(ts, rate, price at ts or None)`` of both legs and update extremes (``ext`` = (high, low))."""
    for _ts, rate, price in pay_a:
        p["funding"] -= p["dir"] * rate * (price or p["a_last"]) / p["a_e"]
    for _ts, rate, price in pay_b:
        p["funding"] += p["dir"] * rate * (price or p["b_last"]) / p["a_e"]
    p["last"] = t
    for ex, e_key in ((ext_a, "a_e"), (ext_b, "b_e")):
        if ex:
            p["max_up"] = max(p["max_up"], ex[0] / p[e_key] - 1.0)
            p["max_down"] = max(p["max_down"], 1.0 - ex[1] / p[e_key])
    p["last_bar"] = t
    if pa and pb:
        p["a_last"], p["b_last"] = pa, pb
        p["max_gap"] = max(p["max_gap"], abs(pa / pb - p["a_e"] / p["b_e"]))


def spread_price_pnl(p: dict[str, Any]) -> float:
    return p["dir"] * ((p["a_last"] - p["a_e"]) - (p["b_last"] - p["b_e"])) / p["a_e"]


def spread_trade(p: dict[str, Any], t: int, side_cost: float, stop_penalty: float, at_end: bool, stopped: bool = False) -> dict[str, Any]:
    fees = p["fees"] + side_cost * p["a_last"] / p["a_e"] + (stop_penalty if stopped else 0.0)
    price_pnl = spread_price_pnl(p)
    return {
        **{k: p[k] for k in ("symbol", "open_ts", "dir", "entry_spread", "funding", "max_up", "max_down", "max_gap")},
        "close_ts": t,
        "price_pnl": price_pnl,
        "fees": fees,
        "pnl": p["funding"] + price_pnl - fees,
        "open_at_end": at_end,
        "stopped": stopped,
    }


def spread_pnl_now(p: dict[str, Any]) -> float:
    return p["funding"] + spread_price_pnl(p) - p["fees"]


def spread_stop_hit(p: dict[str, Any], stop_move: float | None) -> bool:
    return stop_move is not None and max(p["max_up"], p["max_down"]) >= stop_move


def spread_exit_due(p: dict[str, Any], ra: float | None, rb: float | None, exit_apr: float) -> bool:
    return ra is not None and rb is not None and p["dir"] * (rb - ra) <= exit_apr


def spread_rank(
    candidates: Iterable[tuple[str, float | None, float | None, float | None, float | None]],
    entry_apr: float,
    max_entry_gap: float | None,
) -> list[tuple[float, str, int]]:
    """Entry candidates ``(symbol, APR_a, APR_b, price_a, price_b)`` -> ``[(spread, symbol, direction)]``, widest first.

    ``|APR_b - APR_a| >= entry_apr``; the pair is skipped while its prices differ by more than ``max_entry_gap``.
    Direction +1 (long A / short B) when B's funding is higher.
    """
    ranked = []
    for sym, ra, rb, pa, pb in candidates:
        if ra is None or rb is None or abs(rb - ra) < entry_apr:
            continue
        if pa and pb and (max_entry_gap is None or abs(pa / pb - 1.0) <= max_entry_gap):
            ranked.append((abs(rb - ra), sym, 1 if rb > ra else -1))
    return sorted(ranked, reverse=True)


def spread_step(
    open_pos: dict[str, dict[str, Any]],
    t: int,
    universe: Iterable[str],
    mark_obs: Callable[[str, dict[str, Any]], dict[str, Any]],
    entry_obs: Callable[[str], tuple[float | None, float | None, float | None, float | None]],
    *,
    slots: int,
    entry_apr: float,
    exit_apr: float,
    side_cost: float,
    max_entry_gap: float | None,
    stop_move: float | None,
    stop_penalty: float,
) -> dict[str, Any]:
    """One decision cycle of strategy B at ``t``; same contract as ``carry_step``.

    ``mark_obs(sym, pos)`` -> ``{"pay_a", "pay_b", "ext_a", "ext_b", "pa", "pb", "ra", "rb"}``;
    ``entry_obs(sym)`` -> ``(APR_a, APR_b, price_a, price_b)``.
    """
    decisions: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for sym in list(open_pos):
        p = open_pos[sym]
        o = mark_obs(sym, p)
        spread_mark(p, t, pay_a=o["pay_a"], pay_b=o["pay_b"], ext_a=o["ext_a"], ext_b=o["ext_b"], pa=o["pa"], pb=o["pb"])
        if spread_stop_hit(p, stop_move):
            closed.append(spread_trade(p, t, side_cost, stop_penalty, at_end=False, stopped=True))
            del open_pos[sym]
            decisions.append({"symbol": sym, "action": "close", "reason": "stop", "spread": None})
            continue
        held = None if o["ra"] is None or o["rb"] is None else p["dir"] * (o["rb"] - o["ra"])
        if spread_exit_due(p, o["ra"], o["rb"], exit_apr):
            closed.append(spread_trade(p, t, side_cost, stop_penalty, at_end=False))
            del open_pos[sym]
            decisions.append({"symbol": sym, "action": "close", "reason": "spread<=exit", "spread": held})
        else:
            decisions.append(
                {"symbol": sym, "action": "hold", "reason": "spread>exit" if held is not None else "apr_unavailable", "spread": held}
            )
    free = slots - len(open_pos)
    ranked: list[tuple[float, str, int]] = []
    opened: list[str] = []
    if free > 0:
        obs = {s: entry_obs(s) for s in universe if s not in open_pos}
        ranked = spread_rank(((s, *v) for s, v in obs.items()), entry_apr, max_entry_gap)
        for spread, sym, d in ranked[:free]:
            _, _, pa, pb = obs[sym]
            open_pos[sym] = spread_open(sym, t, d, spread, pa, pb, side_cost)  # type: ignore[arg-type]
            opened.append(sym)
            decisions.append({"symbol": sym, "action": "open", "reason": "spread>=entry", "spread": spread, "dir": d})
    return {"closed": closed, "opened": opened, "ranked": ranked, "free_slots": max(free, 0), "decisions": decisions}
