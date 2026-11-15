"""Rolled call diagonal on daily option surfaces (LIB-20261115-datahawks-btc-diagonal, D2).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Works on the day surfaces of `put_skew.build_day`
(``snap["expiries"][expiry_ms] = {"F", "T", "strikes": {K: {"mark_iv", ...}}}``, only strikes with a fresh mark).
Units: vols are decimals (0.45 = 45 %), T in years = days / 365, Black-76 with r = 0 on each expiry's own forward,
prices and P&L in USD per 1 BTC of the long leg.

Structure: buy 1 call at the smallest fresh strike at or above the forward of the expiry nearest ``long_target`` days
(window ``long_lo``-``long_hi``); sell ``qty`` calls at ~``short_delta`` on the nearest Friday expiry ``short_lo``-
``short_hi`` days out, strike not below the long strike. Each time the short expires (cash-settled at the delivery
price), sell the next Friday ~``short_delta`` call while the long has at least ``min_long_days`` left; at the first check
surface (a short expiry, or a Friday when a roll was skipped) where it has less, close the long at the mark.
``qty = 0`` gives the naked long call with the same entry and exit days.

Costs (Bybit, as `calendar_p10`): bought at mark IV + ``hs``, sold at mark IV - ``hs``; taker fee per leg
(`option_ideas.fee`); delivery fee on an ITM short at settlement. R = P&L / initial debit (net premium paid plus
entry fees): the return on the capital at risk of the long; extra margin of the short is not counted.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from .calendar_p10 import atm_iv, clean_iv, month_expiry
from .option_condor import strike_for_delta
from .option_ideas import fee
from .options import black76_price
from .trend_spread import DAY_MS, DELIVERY_CAP, DELIVERY_RATE, MIN_IV

FRIDAY = 4

DEFAULTS: dict[str, Any] = {
    "long_lo": 30.0,
    "long_hi": 60.0,
    "long_target": 40.0,
    "short_lo": 4.0,
    "short_hi": 8.0,
    "short_hi_first": 11.0,  # first short only: if no Friday in [short_lo, short_hi], the one up to this many days
    "short_delta": 0.16,
    "min_long_days": 8.0,
    "band": 0.03,  # max distance of a chosen strike from its target, as a fraction of spot
    "hs": 0.015,
    "arb": "replace",
    "exit_slack": 2,  # days after a check day to look for a surface when the check day has none
    "grid": None,  # None: only strikes with a fresh mark; a number: listed-grid strikes priced from the smile
}


def _dte(exp: dict[str, Any]) -> float:
    return exp["T"] * 365.0


def is_friday(expiry_ms: int) -> bool:
    return datetime.fromtimestamp(expiry_ms / 1000, UTC).weekday() == FRIDAY


def long_expiry(snap: dict[str, Any], lo: float, hi: float, target: float) -> int | None:
    """Expiry with DTE in [lo, hi] nearest ``target`` days (ties: the earlier)."""
    ok = [(abs(_dte(x) - target), _dte(x), e) for e, x in snap["expiries"].items() if lo <= _dte(x) <= hi]
    return min(ok)[2] if ok else None


def short_expiry(snap: dict[str, Any], lo: float, hi: float, before_ms: int | None = None) -> int | None:
    """The nearest Friday expiry with DTE in [lo, hi] (and before ``before_ms`` if given)."""
    ok = [
        (_dte(x), e) for e, x in snap["expiries"].items() if lo <= _dte(x) <= hi and is_friday(e) and (before_ms is None or e < before_ms)
    ]
    return min(ok)[1] if ok else None


def call_price(exp: dict[str, Any], K: float, iv: float) -> float:
    return black76_price(exp["F"], K, exp["T"], max(iv, MIN_IV), True)


def grid_ok(x: dict[str, Any], K: float, tol: float) -> bool:
    """A grid strike can be priced from the smile: fresh strikes on both sides, the nearest within ``tol`` USD."""
    ks = list(x["strikes"])
    return bool(ks) and min(ks) <= K <= max(ks) and min(abs(k - K) for k in ks) <= tol + 1e-9


def pick_long(snap: dict[str, Any], P: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Long leg: (leg, reason). Smallest fresh strike >= the expiry's forward, within ``band`` x spot of it; with
    ``P["grid"]`` set: the smallest multiple of ``grid`` >= the forward, IV from the smile (`grid_ok`)."""
    e = long_expiry(snap, P["long_lo"], P["long_hi"], P["long_target"])
    if e is None:
        return None, "no long expiry"
    x = snap["expiries"][e]
    if P.get("grid"):
        K = math.ceil(x["F"] / P["grid"] - 1e-9) * P["grid"]
        if not grid_ok(x, K, P["band"] * snap["spot"]):
            return None, "long: no fresh strike"
        iv, st = clean_iv(x, K, rule=P["arb"])
        if iv is None:
            return None, "long: arbitrage in marks"
        return {"exp": e, "K": K, "iv": iv, "arb": st, "F": x["F"], "T": x["T"]}, ""
    cands = [k for k in x["strikes"] if k >= x["F"]]
    if not cands:
        return None, "long: no fresh strike"
    K = min(cands)
    if K - x["F"] > P["band"] * snap["spot"] + 1e-9:
        return None, "long: no fresh strike"
    iv, st = clean_iv(x, K, rule=P["arb"])
    if iv is None:
        return None, "long: arbitrage in marks"
    return {"exp": e, "K": K, "iv": iv, "arb": st, "F": x["F"], "T": x["T"]}, ""


def pick_short(
    snap: dict[str, Any], k_floor: float, long_exp: int, P: Mapping[str, Any], *, first: bool
) -> tuple[dict[str, Any] | None, str]:
    """Short leg: (leg, reason). Friday expiry in [short_lo, short_hi] (the first short may use up to
    ``short_hi_first``), strike = the fresh strike nearest the ``short_delta`` call strike among strikes >= max(k_floor,
    forward), within ``band`` x spot of the delta strike."""
    e = short_expiry(snap, P["short_lo"], P["short_hi"], long_exp)
    if e is None and first:
        e = short_expiry(snap, P["short_hi"], P["short_hi_first"], long_exp)
    if e is None:
        return None, "no short expiry"
    x = snap["expiries"][e]
    tgt = strike_for_delta(x, P["short_delta"], True)
    if tgt is None:
        return None, "short: no delta strike"
    floor = max(k_floor, x["F"])
    if P.get("grid"):
        g = P["grid"]
        K = max(round(tgt / g) * g, math.ceil(floor / g - 1e-9) * g)
        if abs(K - tgt) > P["band"] * snap["spot"] + 1e-9 or not grid_ok(x, K, P["band"] * snap["spot"]):
            return None, "short: no fresh strike"
        iv, st = clean_iv(x, K, rule=P["arb"])
        if iv is None:
            return None, "short: arbitrage in marks"
        return {"exp": e, "K": K, "iv": iv, "arb": st, "F": x["F"], "T": x["T"]}, ""
    cands = [k for k in x["strikes"] if k >= floor]
    if not cands:
        return None, "short: no fresh strike"
    K = min(cands, key=lambda k: (abs(k - tgt), k))
    if abs(K - tgt) > P["band"] * snap["spot"] + 1e-9:
        return None, "short: no fresh strike"
    iv, st = clean_iv(x, K, rule=P["arb"])
    if iv is None:
        return None, "short: arbitrage in marks"
    return {"exp": e, "K": K, "iv": iv, "arb": st, "F": x["F"], "T": x["T"]}, ""


def plan_entry(snap: dict[str, Any], qty: float, P: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Entry legs and debit from the entry surface alone. (plan, reason)."""
    lg, why = pick_long(snap, P)
    if lg is None:
        return None, why
    sh = None
    if qty > 0:
        sh, why = pick_short(snap, lg["K"], lg["exp"], P, first=True)
        if sh is None:
            return None, why
    S, hs = snap["spot"], P["hs"]
    pl = call_price(snap["expiries"][lg["exp"]], lg["K"], lg["iv"] + hs)
    debit = pl + fee(S, pl)
    credit = 0.0
    if sh is not None:
        ps = call_price(snap["expiries"][sh["exp"]], sh["K"], sh["iv"] - hs)
        credit = qty * (ps - fee(S, ps))
        debit -= credit
    if debit <= 0:
        return None, "debit <= 0"
    return {"day": snap["day"], "t_ms": snap["t_ms"], "spot": S, "qty": qty, "long": lg, "short": sh, "debit": debit, "credit0": credit}, ""


def delivery_cost(K: float, delivery_price: float) -> float:
    """Pay-out plus Bybit delivery fee of one short call settled at ``delivery_price`` (0 if OTM)."""
    v = max(delivery_price - K, 0.0)
    return v + min(DELIVERY_RATE * delivery_price, DELIVERY_CAP * v) if v > 0 else 0.0


def _surface_on_or_after(days: Mapping[int, dict[str, Any]], day: int, slack: int, not_after_ms: int) -> dict[str, Any] | None:
    for k in range(slack + 1):
        s = days.get(day + k * DAY_MS)
        if s is not None and s["t_ms"] < not_after_ms:
            return s
    return None


def run_position(
    days: Mapping[int, dict[str, Any]],
    delivery: Callable[[int], float | None],
    entry_day: int,
    qty: float,
    P: Mapping[str, Any] = DEFAULTS,
) -> tuple[dict[str, Any] | None, str]:
    """Full life of one position entered on the 09:00 surface of ``entry_day`` (UTC midnight ms). (result, reason).

    Every decision (strikes, roll or close) uses only the surface of its own day; settlements use the delivery
    price of the expiry. Result: P&L and R on the initial debit, rolls done / skipped, ITM shorts, exit time.
    """
    snap = days.get(entry_day)
    if snap is None:
        return None, "no surface"
    plan, why = plan_entry(snap, qty, P)
    if plan is None:
        return None, why
    lg, hs = plan["long"], P["hs"]
    cash = -plan["debit"]
    short = plan["short"]
    rolls = skipped = itm = 0
    credits = plan["credit0"]
    # first check day: the first short's expiry day, or (naked) the first Friday at least short_lo days out
    if short is not None:
        check = (short["exp"] // DAY_MS) * DAY_MS
    else:
        check = entry_day + DAY_MS
        while (
            datetime.fromtimestamp(check / 1000, UTC).weekday() != FRIDAY or check + 8 * 3_600_000 - snap["t_ms"] < P["short_lo"] * DAY_MS
        ):
            check += DAY_MS
    while True:
        if short is not None:
            dp = delivery(short["exp"])
            if dp is None:
                return None, "no delivery price"
            pay = delivery_cost(short["K"], dp)
            itm += pay > 0
            cash -= qty * pay
            short = None
        s = _surface_on_or_after(days, check, P["exit_slack"], lg["exp"])
        if s is None:
            return None, "no check surface"
        x = s["expiries"].get(lg["exp"])
        if x is None:
            return None, "no long mark at check"
        if _dte(x) < P["min_long_days"]:
            iv, st = clean_iv(x, lg["K"], rule=P["arb"], at_exit=True)
            if iv is None:
                return None, "no long mark at exit"
            p = call_price(x, lg["K"], iv - hs)
            cash += p - fee(s["spot"], p)
            return {
                "day": entry_day,
                "spot": plan["spot"],
                "qty": qty,
                "K_long": lg["K"],
                "long_exp": lg["exp"],
                "debit": plan["debit"],
                "pnl": cash,
                "r": cash / plan["debit"],
                "pnl_spot": cash / plan["spot"],
                "credits": credits,
                "rolls": rolls,
                "skipped": skipped,
                "itm": itm,
                "exit_ms": s["t_ms"],
                "exit_spot": s["spot"],
                "exit_arb": st,
            }, ""
        if qty > 0:
            sh, _ = pick_short(s, lg["K"], lg["exp"], P, first=False)
            if sh is not None:
                ps = call_price(s["expiries"][sh["exp"]], sh["K"], sh["iv"] - hs)
                c = qty * (ps - fee(s["spot"], ps))
                cash += c
                credits += c
                rolls += 1
                short = sh
                check = (sh["exp"] // DAY_MS) * DAY_MS
                continue
            skipped += 1
        check += 7 * DAY_MS


def front_month_diff(snap: dict[str, Any], P: Mapping[str, Any] = DEFAULTS) -> dict[str, Any]:
    """Front inversion without an event condition: ATM IV of the Friday expiry in [short_lo, short_hi] minus the
    ATM IV of the month expiry (`calendar_p10.month_expiry`: 21-45 d nearest 30 d). ``diff`` None when not measured."""
    f = short_expiry(snap, P["short_lo"], P["short_hi"])
    m = month_expiry(snap)
    if f is None or m is None:
        return {"front": f, "month": m, "diff": None, "state": "no_front" if f is None else "no_month"}
    fa, ma = atm_iv(snap["expiries"][f]), atm_iv(snap["expiries"][m])
    if fa is None or ma is None:
        return {"front": f, "month": m, "diff": None, "state": "no_atm"}
    return {"front": f, "month": m, "diff": fa - ma, "state": "measured"}
