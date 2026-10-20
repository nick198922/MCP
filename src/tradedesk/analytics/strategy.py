"""Pure helpers for multi-leg option / linear strategy payoffs.

Legs are plain dicts (easy to feed from an MCP call):

* ``kind``: ``"call"`` | ``"put"`` | ``"linear"`` — call/put = option, linear =
  perp/future/spot.
* ``side``: ``"long"`` | ``"short"``.
* ``size``: contracts (always positive; sign comes from ``side``).
* ``strike``: option strike (ignored for ``linear``).
* ``entry_premium``: option premium paid/received at open, in USD per contract.
* ``entry``: entry price for ``linear`` legs, in USD.

``payoff_curve`` returns per-S PnL at the terminal date (option intrinsic value
+ linear P&L) net of entry costs; ``aggregate_greeks`` sums Black-76 greeks for
option legs at the current forward; ``prob_of_profit`` integrates the risk-
neutral log-normal of the forward against the payoff region.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from .options import black76_greeks, black76_price, ncdf


def _sign(side: str) -> int:
    return 1 if side == "long" else -1 if side == "short" else 0


def _leg_payoff_at(leg: dict[str, Any], s: float) -> float:
    kind = leg.get("kind")
    sign = _sign(leg.get("side", ""))
    size = float(leg.get("size") or 0.0)
    if sign == 0 or size <= 0:
        return 0.0
    if kind == "call":
        strike = float(leg["strike"])
        premium = float(leg.get("entry_premium") or 0.0)
        intrinsic = max(s - strike, 0.0)
        return sign * size * (intrinsic - premium)
    if kind == "put":
        strike = float(leg["strike"])
        premium = float(leg.get("entry_premium") or 0.0)
        intrinsic = max(strike - s, 0.0)
        return sign * size * (intrinsic - premium)
    if kind == "linear":
        entry = float(leg.get("entry") or 0.0)
        return sign * size * (s - entry)
    return 0.0


def payoff_curve(legs: Iterable[dict[str, Any]], prices: list[float]) -> list[float]:
    """Portfolio PnL at the terminal date on each S in ``prices``."""
    pnl: list[float] = []
    for s in prices:
        total = 0.0
        for leg in legs:
            total += _leg_payoff_at(leg, s)
        pnl.append(total)
    return pnl


def payoff_extremes(legs: list[dict[str, Any]]) -> dict[str, float | None]:
    """Exact max profit and max loss of the terminal payoff over S in [0, inf).

    The payoff is piecewise linear with kinks at the strikes, so its extremes sit at S = 0, at a strike, or at
    infinity: beyond the last strike the slope is the net long call (+ linear) size; a positive slope makes the
    profit unbounded, a negative one the loss. ``None`` = unbounded. Unlike ``summarise`` (max/min on a ±30 %
    grid), this does not mistake a grid edge for the maximum of a naked leg.
    """
    kinks = [0.0] + sorted({float(leg["strike"]) for leg in legs if leg.get("kind") in ("call", "put")})
    values = [sum(_leg_payoff_at(leg, s) for leg in legs) for s in kinks]
    slope = 0.0
    for leg in legs:
        if leg.get("kind") in ("call", "linear") and float(leg.get("size") or 0.0) > 0:
            slope += _sign(leg.get("side", "")) * float(leg["size"])
    eps = 1e-12
    return {
        "max_profit": None if slope > eps else max(values),
        "max_loss": None if slope < -eps else min(values),
    }


def breakevens(prices: list[float], pnl: list[float]) -> list[float]:
    """Linear-interpolated zero crossings of the payoff curve."""
    out: list[float] = []
    for i in range(1, len(prices)):
        p0, p1 = pnl[i - 1], pnl[i]
        if p0 == 0 and p1 != 0:
            out.append(prices[i - 1])
            continue
        if (p0 < 0 < p1) or (p0 > 0 > p1):
            s0, s1 = prices[i - 1], prices[i]
            # linear interpolation to the x-axis
            s_be = s0 - p0 * (s1 - s0) / (p1 - p0)
            out.append(s_be)
    return out


def aggregate_greeks(legs: Iterable[dict[str, Any]], *, forward: float, days: float, iv: float) -> dict[str, float]:
    """Portfolio Black-76 greeks at the current forward.

    Linear legs contribute +/- size to delta (sign from ``side``) and nothing
    to gamma/vega/theta. Option legs are priced at ``(F, days, iv)``.
    """
    T = max(days, 1e-9) / 365.0
    totals = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    for leg in legs:
        sign = _sign(leg.get("side", ""))
        size = float(leg.get("size") or 0.0)
        if sign == 0 or size <= 0:
            continue
        kind = leg.get("kind")
        if kind == "linear":
            totals["delta"] += sign * size
            continue
        is_call = kind == "call"
        strike = float(leg.get("strike") or 0.0)
        if strike <= 0:
            continue
        g = black76_greeks(forward, strike, T, iv, is_call=is_call)
        totals["delta"] += sign * size * g["delta"]
        totals["gamma"] += sign * size * g["gamma"]
        totals["vega"] += sign * size * g["vega"]
        totals["theta"] += sign * size * g["theta"]
    return totals


def mtm_value(legs: Iterable[dict[str, Any]], *, forward: float, days: float, iv: float) -> float:
    """Current mark-to-market value of the portfolio (net of entry cost).

    Option legs are valued with Black-76; linear legs = (F - entry) * sign * size.
    """
    T = max(days, 1e-9) / 365.0
    value = 0.0
    for leg in legs:
        sign = _sign(leg.get("side", ""))
        size = float(leg.get("size") or 0.0)
        if sign == 0 or size <= 0:
            continue
        kind = leg.get("kind")
        if kind == "linear":
            entry = float(leg.get("entry") or 0.0)
            value += sign * size * (forward - entry)
            continue
        is_call = kind == "call"
        strike = float(leg.get("strike") or 0.0)
        if strike <= 0:
            continue
        price = black76_price(forward, strike, T, iv, is_call=is_call)
        premium = float(leg.get("entry_premium") or 0.0)
        value += sign * size * (price - premium)
    return value


def _lognormal_cdf(F: float, S: float, T: float, sigma: float) -> float:
    """P(S_T <= S) under the risk-neutral log-normal with r=0, drift from F."""
    if S <= 0 or F <= 0 or T <= 0 or sigma <= 0:
        return 0.0 if S <= 0 else 1.0
    z = (math.log(S / F) + 0.5 * sigma * sigma * T) / (sigma * math.sqrt(T))
    # Note: standard form uses −½σ²T; r=0 forward model with F as martingale → use (ln S/F)/(σ√T) + 0.5σ√T
    return ncdf(z)


def prob_of_profit(
    legs: list[dict[str, Any]],
    prices: list[float],
    pnl: list[float],
    *,
    forward: float,
    days: float,
    iv: float,
) -> float | None:
    """Risk-neutral probability that the terminal payoff is strictly positive.

    Uses the breakeven crossings as segment boundaries, samples each segment's
    mid-point via the leg payoff formulas to decide polarity (not the grid,
    so we are not fooled by coarse grids), and integrates the log-normal CDF
    of the forward over the profit segments.
    """
    if not prices or len(prices) != len(pnl):
        return None
    T = max(days, 1e-9) / 365.0
    bes = breakevens(prices, pnl)
    boundaries = [-math.inf] + bes + [math.inf]
    prob = 0.0
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        if math.isinf(lo) and math.isinf(hi):
            s_mid = forward
        elif math.isinf(lo):
            s_mid = max(0.5 * hi, 0.0)
        elif math.isinf(hi):
            s_mid = 2.0 * lo
        else:
            s_mid = 0.5 * (lo + hi)
        total = sum(_leg_payoff_at(leg, s_mid) for leg in legs)
        if total <= 0:
            continue
        cdf_hi = 1.0 if math.isinf(hi) else _lognormal_cdf(forward, hi, T, iv)
        cdf_lo = 0.0 if math.isinf(lo) else _lognormal_cdf(forward, lo, T, iv)
        prob += max(0.0, cdf_hi - cdf_lo)
    return min(max(prob, 0.0), 1.0)


def _opp(side: str) -> str:
    return "short" if side == "long" else "long"


def build_straddle(
    *, strike: float, call_premium: float, put_premium: float, side: str = "long", size: float = 1.0
) -> list[dict[str, Any]]:
    """Call + put at the same strike — long = long vol, short = short vol."""
    return [
        {"kind": "call", "side": side, "size": size, "strike": strike, "entry_premium": call_premium},
        {"kind": "put", "side": side, "size": size, "strike": strike, "entry_premium": put_premium},
    ]


def build_strangle(
    *, call_strike: float, put_strike: float, call_premium: float, put_premium: float, side: str = "long", size: float = 1.0
) -> list[dict[str, Any]]:
    """OTM call + OTM put — wider breakevens than a straddle, cheaper premium."""
    return [
        {"kind": "call", "side": side, "size": size, "strike": call_strike, "entry_premium": call_premium},
        {"kind": "put", "side": side, "size": size, "strike": put_strike, "entry_premium": put_premium},
    ]


def build_risk_reversal(
    *,
    call_strike: float,
    put_strike: float,
    call_premium: float,
    put_premium: float,
    direction: str = "bullish",
    size: float = 1.0,
) -> list[dict[str, Any]]:
    """Risk reversal: bullish = long call + short put; bearish = long put + short call.

    Net premium ≈ call_premium − put_premium (bullish); can be a credit when
    puts trade richer than calls (downside skew).
    """
    if direction == "bullish":
        call_side, put_side = "long", "short"
    elif direction == "bearish":
        call_side, put_side = "short", "long"
    else:
        raise ValueError("direction must be 'bullish' or 'bearish'")
    return [
        {"kind": "call", "side": call_side, "size": size, "strike": call_strike, "entry_premium": call_premium},
        {"kind": "put", "side": put_side, "size": size, "strike": put_strike, "entry_premium": put_premium},
    ]


def build_iron_condor(
    *,
    long_put_strike: float,
    short_put_strike: float,
    short_call_strike: float,
    long_call_strike: float,
    long_put_premium: float,
    short_put_premium: float,
    short_call_premium: float,
    long_call_premium: float,
    size: float = 1.0,
) -> list[dict[str, Any]]:
    """Short strangle protected by further-OTM wings. Net credit; max profit
    when S stays between the short strikes, max loss capped by the wings.
    Requires: long_put < short_put < short_call < long_call."""
    if not (long_put_strike < short_put_strike < short_call_strike < long_call_strike):
        raise ValueError("strikes must satisfy long_put < short_put < short_call < long_call")
    return [
        {"kind": "put", "side": "long", "size": size, "strike": long_put_strike, "entry_premium": long_put_premium},
        {"kind": "put", "side": "short", "size": size, "strike": short_put_strike, "entry_premium": short_put_premium},
        {"kind": "call", "side": "short", "size": size, "strike": short_call_strike, "entry_premium": short_call_premium},
        {"kind": "call", "side": "long", "size": size, "strike": long_call_strike, "entry_premium": long_call_premium},
    ]


def build_cash_and_carry(*, spot_price: float, futures_price: float, size: float = 1.0) -> list[dict[str, Any]]:
    """Long spot + short dated future. Earns the basis as the future converges
    toward spot at expiry. Returns two ``linear`` legs."""
    return [
        {"kind": "linear", "side": "long", "size": size, "entry": spot_price},
        {"kind": "linear", "side": "short", "size": size, "entry": futures_price},
    ]


def build_funding_carry(*, long_mark: float, short_mark: float, size: float = 1.0) -> list[dict[str, Any]]:
    """Long perp on the cheap venue + short perp on the expensive venue.

    Both legs are ``linear`` at their own marks. The strategy earns the perp
    premium spread + the funding differential (which is NOT captured by this
    payoff — pair with ``funding_zscore`` / ``perps_basis_compare``)."""
    return [
        {"kind": "linear", "side": "long", "size": size, "entry": long_mark},
        {"kind": "linear", "side": "short", "size": size, "entry": short_mark},
    ]


def scenario_matrix(
    legs: list[dict[str, Any]],
    *,
    initial_forward: float,
    horizons: list[dict[str, float]],
    prices: list[float],
) -> list[dict[str, Any]]:
    """Multi-horizon mark-to-market grid for a strategy.

    ``horizons`` is a list of ``{"days": D, "iv": σ}``. For each horizon and
    each ``S`` in ``prices`` we revalue every leg with Black-76 at ``(S, D, σ)``
    minus its entry (linear legs: ``(S - entry) * sign * size``), so cells are
    actual MTM, not payoff-at-expiry. The ``greeks`` block is computed at
    ``forward=initial_forward`` and the horizon's ``(D, σ)`` so you can see how
    δ/γ/vega/θ drift through time without picking a new forward each row.

    This does NOT assume a vol surface — one flat IV per horizon; feed in a
    different value per row to approximate a term structure or a vol shock.
    """
    out: list[dict[str, Any]] = []
    for h in horizons:
        days = float(h["days"])
        iv = float(h["iv"])
        pnls: list[float] = []
        for s in prices:
            pnls.append(mtm_value(legs, forward=float(s), days=days, iv=iv))
        greeks = aggregate_greeks(legs, forward=float(initial_forward), days=days, iv=iv)
        out.append({"days": days, "iv": iv, "prices": list(prices), "pnl": pnls, "greeks": greeks})
    return out


def summarise(
    legs: list[dict[str, Any]],
    *,
    forward: float,
    days: float,
    iv: float,
    width_pct: float = 0.3,
    grid: int = 201,
) -> dict[str, Any]:
    """One-stop summary used by the MCP tool. Returns payoff grid, greeks,
    MTM, breakevens, max profit/loss on the grid and risk-neutral PoP."""
    if grid < 3:
        grid = 3
    lo = forward * (1.0 - max(width_pct, 0.0))
    hi = forward * (1.0 + max(width_pct, 0.0))
    if lo <= 0:
        lo = forward * 0.01
    prices = [lo + (hi - lo) * i / (grid - 1) for i in range(grid)]
    pnl = payoff_curve(legs, prices)
    bes = breakevens(prices, pnl)
    pop = prob_of_profit(legs, prices, pnl, forward=forward, days=days, iv=iv)
    greeks = aggregate_greeks(legs, forward=forward, days=days, iv=iv)
    mtm = mtm_value(legs, forward=forward, days=days, iv=iv)
    return {
        "forward": forward,
        "days": days,
        "iv": iv,
        "width_pct": width_pct,
        "grid": grid,
        "prices": prices,
        "pnl": pnl,
        "breakevens": bes,
        "max_profit": max(pnl),
        "max_loss": min(pnl),
        "pop": pop,
        "greeks": greeks,
        "mtm_usd": mtm,
    }
