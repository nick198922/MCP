"""Paper trades of the assistant's option recommendations (IDEA-20261018-assistant, "Журнал").

Pure functions, no I/O. A recommendation is a structure of option legs on one Deribit expiry. It is booked as a
paper trade at the mark +- 1 vol pt (`option_replay.build_legs`), sized from the owner's deposit and risk
percentage, and settled at the expiry delivery price.

Units: prices and P&L in USD for 1 BTC of underlying per leg unit (``qty = +-1``); vols decimals; Black-76 with
r = 0 on the entry forward; T in years = days / 365. Deribit option fees: 0.03 % of the underlying per leg
(capped at 12.5 % of the option price) on entry, 0.015 % (same cap, of the payoff) on delivery of an in-the-money leg.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from .options import black76_price

Leg = tuple[float, bool, float, float]  # (strike USD, is_call, qty, exec_iv)

OPTION_FEE = 0.0003
DELIVERY_FEE = 0.00015
FEE_CAP = 0.125
STRESS_UP = 0.30  # undefined-risk structures: loss is measured at forward * (1 + STRESS_UP)


def leg_price(forward: float, leg: Leg, t_years: float) -> float:
    """Black-76 price of one unit of the leg in USD (positive, regardless of the sign of qty)."""
    k, is_call, _, iv = leg
    return black76_price(forward, k, t_years, iv, is_call)


def payoff(legs: Sequence[Leg], s: float) -> float:
    """Expiry payoff in USD (signed: sold legs subtract) when the underlying settles at ``s``."""
    return sum(q * (max(s - k, 0.0) if c else max(k - s, 0.0)) for k, c, q, _ in legs)


def entry_book(legs: Sequence[Leg], forward: float, spot: float, t_years: float) -> dict[str, Any]:
    """Premium (debit > 0), entry fees and per-leg entry prices for 1 unit of the structure."""
    prices = [leg_price(forward, lg, t_years) for lg in legs]
    premium = sum(lg[2] * p for lg, p in zip(legs, prices, strict=True))
    fees = sum(min(OPTION_FEE * spot, FEE_CAP * p) * abs(lg[2]) for lg, p in zip(legs, prices, strict=True))
    return {"leg_prices": prices, "premium": premium, "entry_fees": fees}


def delivery_fees(legs: Sequence[Leg], s: float) -> float:
    """Deribit delivery fee per unit of the structure when it settles at ``s`` (in-the-money legs only)."""
    total = 0.0
    for k, c, q, _ in legs:
        p = max(s - k, 0.0) if c else max(k - s, 0.0)
        if p > 0:
            total += min(DELIVERY_FEE * s, FEE_CAP * p) * abs(q)
    return total


def max_loss(legs: Sequence[Leg], premium: float, entry_fees: float, forward: float) -> tuple[float, str]:
    """Worst loss per unit (>= 0) and its basis, entry and delivery fees included.

    The payoff is piecewise linear, so for a structure whose upside is bounded the worst loss sits at 0 or at a
    strike ("max_loss"). A net short-call structure has unbounded loss; it is measured at forward * 1.3 as the
    larger of that and every strike point ("stress_up_30pct"), which is a stress, not a bound.
    """
    pts = [0.0, *sorted({lg[0] for lg in legs})]
    net_calls = sum(q for _, c, q, _ in legs if c)
    basis = "max_loss"
    if net_calls < -1e-12:
        pts.append(forward * (1.0 + STRESS_UP))
        basis = "stress_up_30pct"
    worst = max(-(payoff(legs, s) - premium - entry_fees - delivery_fees(legs, s)) for s in pts)
    return max(worst, 0.0), basis


def size_for_risk(risk_usd: float, loss_per_btc: float, lot: float = 0.1) -> float:
    """Largest size in BTC (multiple of ``lot``) whose worst loss fits ``risk_usd``; 0.0 when even one lot does not."""
    if loss_per_btc <= 0 or lot <= 0:
        return 0.0
    return math.floor(risk_usd / loss_per_btc / lot + 1e-9) * lot


def open_paper(
    legs: Sequence[Leg],
    *,
    forward: float,
    spot: float,
    t_years: float,
    deposit_usd: float,
    risk_pct: float,
    size_btc: float | None = None,
    lot: float = 0.1,
) -> dict[str, Any]:
    """Entry record of a paper trade. ``risk_pct`` is in percent (1.0 = 1 % of the deposit).

    Size = the largest lot multiple whose worst loss fits ``deposit * risk_pct / 100`` unless ``size_btc`` is
    given. When one lot already exceeds the risk budget the size is one lot and ``within_risk`` is False.
    """
    book = entry_book(legs, forward, spot, t_years)
    loss, basis = max_loss(legs, book["premium"], book["entry_fees"], forward)
    risk_usd = deposit_usd * risk_pct / 100.0
    max_size = size_for_risk(risk_usd, loss, lot)
    size = size_btc if size_btc is not None else (max_size if max_size > 0 else lot)
    return {
        **book,
        "max_loss_per_btc": loss,
        "risk_basis": basis,
        "risk_budget_usd": risk_usd,
        "max_size_btc": max_size,
        "size_btc": size,
        "risk_usd": loss * size,
        "within_risk": loss * size <= risk_usd + 1e-9,
    }


def settle_paper(trade: dict[str, Any], delivery_price: float) -> dict[str, Any]:
    """Settle an opened paper trade at the delivery price.

    ``trade`` needs ``legs`` (list of ``[strike, is_call, qty, exec_iv, entry_price]``), ``premium_per_btc``,
    ``entry_fees_per_btc``, ``size_btc``, ``risk_usd`` and ``spot``; optional ``method_q05_pct`` (the method's 5 %
    quantile of the P&L as a fraction of the entry forward, exactly as `option_replay.method_summary` returns it). Returns the USD P&L for the trade size, the P&L per 1 BTC of
    structure as a fraction of the entry spot (``pnl_pct_spot``, the main metric, same as in the 2026-10-18 study),
    ``r_worst_case`` = P&L / the trade's worst-loss risk in USD (NOT the study's R) and ``r_study`` = P&L per unit /
    the study's risk: the premium for a debit, |q05| * spot for a credit (spot stands in for the forward, as in the study; None when it is a credit and q05 unknown).
    """
    legs: list[Leg] = [(float(k), bool(c), float(q), float(iv)) for k, c, q, iv, *_ in trade["legs"]]
    gross = payoff(legs, delivery_price)
    dfees = delivery_fees(legs, delivery_price)
    per_btc = gross - trade["premium_per_btc"] - trade["entry_fees_per_btc"] - dfees
    size = float(trade["size_btc"])
    pnl = per_btc * size
    risk = float(trade["risk_usd"])
    premium = float(trade["premium_per_btc"])
    q05 = trade.get("method_q05_pct")
    if premium > 0:
        study_risk: float | None = premium
    elif q05 is not None:
        study_risk = abs(float(q05)) * float(trade["spot"])
    else:
        study_risk = None
    return {
        "payoff_per_btc": gross,
        "delivery_fees_per_btc": dfees,
        "pnl_per_btc": per_btc,
        "pnl_usd": pnl,
        "pnl_pct_spot": per_btc / float(trade["spot"]),
        "r_worst_case": pnl / risk if risk > 0 else None,
        "r_study": per_btc / study_risk if study_risk and study_risk > 0 else None,
    }


def summarize(settled: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate settled paper trades: n, win share, mean % of spot (main), mean R (two definitions), total USD."""
    n = len(settled)
    if n == 0:
        return {"n": 0}
    pct = [float(t["pnl_pct_spot"]) for t in settled]
    rw = [float(t["r_worst_case"]) for t in settled if t.get("r_worst_case") is not None]
    rs = [float(t["r_study"]) for t in settled if t.get("r_study") is not None]
    return {
        "n": n,
        "win_share": sum(1 for t in settled if float(t["pnl_usd"]) > 0) / n,
        "mean_pnl_pct_spot": sum(pct) / n,
        "mean_r_worst_case": sum(rw) / len(rw) if rw else None,
        "mean_r_study": sum(rs) / len(rs) if rs else None,
        "n_r_study": len(rs),
        "sum_pnl_usd": sum(float(t["pnl_usd"]) for t in settled),
    }
