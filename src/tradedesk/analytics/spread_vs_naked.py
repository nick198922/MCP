"""Playbook P6 / P3 mechanism on history: is a vertical debit spread cheaper in VRP than the naked long option?

IDEA-20261114-options-playbook, item 4.9. The playbook says (P6, P3) that a debit spread "pays less VRP" than the
bare option it is built on. This module prices, on one entry surface, the P3 spread (bought |delta| 0.40, sold 0.15,
`trend_spread.plan_spread`) and the naked bought leg alone (same strike, same execution IV), holds both to expiry (or
the spread with the playbook exit) and returns the paired difference.

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Surfaces as in `put_skew.build_day`. Units: vols are
decimals, T in years, Black-76 with r = 0 on the surface forward, USD per 1 BTC per leg. ``r`` = P&L / debit (capital
at risk: a bought option's or a debit spread's margin on Bybit is its premium). Costs: Bybit taker fee per leg
(`option_ideas.fee`), delivery fee per ITM leg (`trend_spread.DELIVERY_*`); execution IV = mark +/- half-spread.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .calendar_p10 import clean_iv
from .option_ideas import fee
from .options import black76_price
from .put_skew import choose_expiry
from .trend_spread import DELIVERY_CAP, DELIVERY_RATE, MIN_IV, entry_debit, exec_ivs, managed, plan_spread, settle

ARB_RULES = ("replace", "skip", "none")


def clean_marks(snap: dict[str, Any], plan: dict[str, Any], *, rule: str = "replace") -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Apply the registered static-arbitrage rule (card 4.4 (a), `calendar_p10.clean_iv`) to both legs' marks.

    Returns (plan with the checked marks, {leg: status}); (None, statuses) when a leg's mark stays arbitrageable
    (the entry is skipped). ``rule="none"`` leaves the marks as they are (the P3 construction).
    """
    if rule not in ARB_RULES:
        raise ValueError(f"unknown arbitrage rule {rule!r}")
    if rule == "none":
        return plan, {"long": "unchecked", "short": "unchecked"}
    x = snap["expiries"][plan["expiry"]]
    out = dict(plan)
    status: dict[str, str] = {}
    for leg, key in (("long", "mark_long"), ("short", "mark_short")):
        iv, st = clean_iv(x, plan[f"K_{leg}"], rule=rule)
        status[leg] = st
        if iv is None:
            return None, status
        out[key] = iv
    return out, status


def naked_debit(plan: dict[str, Any], iv_long: float) -> float | None:
    """USD premium of the bought leg alone plus its Bybit taker fee; None if the IV is <= 1 %."""
    if iv_long <= MIN_IV:
        return None
    p = black76_price(plan["F"], plan["K_long"], plan["T"], iv_long, plan["is_call"])
    d = p + fee(plan["spot"], p)
    return d if d > 0 else None


def naked_settle(plan: dict[str, Any], debit: float, delivery_price: float) -> dict[str, Any]:
    """Hold the bought leg to expiry: intrinsic minus the delivery fee minus the debit; r = P&L / debit."""
    K = plan["K_long"]
    intrinsic = max(delivery_price - K, 0.0) if plan["is_call"] else max(K - delivery_price, 0.0)
    dfee = min(DELIVERY_RATE * delivery_price, DELIVERY_CAP * intrinsic) if intrinsic > 0 else 0.0
    pnl = intrinsic - dfee - debit
    return {"pnl": pnl, "r": pnl / debit, "pnl_spot": pnl / plan["spot"]}


def paired(plan: dict[str, Any], spread_res: dict[str, Any], naked_res: dict[str, Any]) -> dict[str, Any]:
    """Paired difference spread - naked: ``r`` in R (each on its own debit), ``pnl_spot`` in fractions of entry spot
    per 1 BTC (= the sold leg's P&L incl. its costs when both are held to expiry)."""
    return {
        "r": spread_res["r"] - naked_res["r"],
        "pnl_spot": (spread_res["pnl"] - naked_res["pnl"]) / plan["spot"],
        "r_spread": spread_res["r"],
        "r_naked": naked_res["r"],
        "exit": spread_res.get("exit", "expiry"),
    }


def build_pair(snap: dict[str, Any], expiry_ms: int, bearish: bool, P: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Entry of one pair on the entry surface only (no outcome): plan, execution IVs, both debits. reason '' = ok."""
    plan, why = plan_spread(snap, expiry_ms, bearish, long_delta=P["long_delta"], short_delta=P["short_delta"], band=P["band"])
    if plan is None:
        return None, why
    plan, arb = clean_marks(snap, plan, rule=P.get("arb", "replace"))
    if plan is None:
        return None, "arbitrage in marks"
    iv_l, iv_s, fb = exec_ivs(plan, P["model"], P["hs"])
    sd = entry_debit(plan, iv_l, iv_s)
    if sd is None:
        return None, "spread debit outside (0, width)"
    nd = naked_debit(plan, iv_l)
    if nd is None:
        return None, "naked debit"
    return {"plan": plan, "debit_spread": sd, "debit_naked": nd, "fallbacks": fb, "arb": arb}, ""


def settle_pair(days: Mapping[int, dict[str, Any]], entry: dict[str, Any], delivery_price: float, P: Mapping[str, Any]) -> dict[str, Any]:
    """Outcome of a built pair: the naked leg to expiry; the spread to expiry or (``P["exit"] == "managed"``) with the
    playbook exit (`trend_spread.managed`: >= ``tp`` of max profit or spot past the sold strike, on later surfaces
    before expiry only)."""
    plan, sd = entry["plan"], entry["debit_spread"]
    if P["exit"] == "expiry":
        s = settle(plan, sd, delivery_price)
    else:
        s = managed(dict(days), plan, sd, delivery_price, tp=P["tp"], beyond_short=True, hs=P["hs"])
    n = naked_settle(plan, entry["debit_naked"], delivery_price)
    return paired(plan, s, n)


def pairs_for(
    days: Mapping[int, dict[str, Any]],
    delivery: Mapping[int, float],
    entries: Iterable[tuple[int, bool]],
    P: Mapping[str, Any],
    *,
    outcome: bool = True,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Pairs for (entry day, bearish) entries. ``days``: day start -> surface; ``delivery``: expiry ms -> delivery price.

    The expiry is `choose_expiry(snap, target, lo, hi, "nearest")` and must be <= ``P["last_expiry"]`` with a delivery
    price. ``outcome=False`` builds entries only (stage 0: no P&L). Each pair reads its entry surface, the surfaces
    before its expiry (managed exit only) and its delivery price, nothing later.
    """
    out: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for d, bearish in entries:
        snap = days.get(d)
        if snap is None:
            why["no surface"] += 1
            continue
        e = choose_expiry(snap, P["target"], P["lo"], P["hi"], rule="nearest")
        if e is None:
            why["no expiry in window"] += 1
            continue
        if e > P["last_expiry"]:
            why["expiry after cache end"] += 1
            continue
        dp = delivery.get(e)
        if dp is None:
            why["no delivery price"] += 1
            continue
        b, w = build_pair(snap, e, bearish, P)
        if b is None:
            why[w] += 1
            continue
        plan = b["plan"]
        rec = {
            "day": d,
            "expiry": e,
            "bearish": bearish,
            "spot": plan["spot"],
            "K_long": plan["K_long"],
            "K_short": plan["K_short"],
            "width": plan["width"],
            "dte": plan["T"] * 365,
            "delta_long": plan["delta_long"],
            "delta_short": plan["delta_short"],
            "debit_spread": b["debit_spread"],
            "debit_naked": b["debit_naked"],
            "fallbacks": b["fallbacks"],
            "arb_long": b["arb"]["long"],
            "arb_short": b["arb"]["short"],
        }
        if outcome:
            rec.update(settle_pair(days, b, dp, P))
        out.append(rec)
    return out, why


def p6_labels(v1: str, v2: str, v3: str) -> dict[str, str]:
    """Registered playbook labels (card 4.9) from the mechanical verdicts of H1 (R per capital, both sides), H2
    (USD per 1 BTC) and H3 (R per capital, P3 trend side)."""

    def lab(v: str, yes: str, no: str) -> str:
        return yes if v == "✓" else (no if v == "✗" else "insufficient")

    return {
        "capital": lab(v1, "spread loses less per capital", "spread not better per capital"),
        "usd": lab(v2, "sold leg earns after costs", "sold leg does not earn after costs"),
        "p3_side": lab(v3, "spread loses less on the trend side", "spread not better on the trend side"),
    }
