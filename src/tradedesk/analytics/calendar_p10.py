"""Playbook rule P10 on history: front-month IV inversion before FOMC / CPI -> calendar / diagonal
(IDEA-20261114-options-playbook, item 4.4).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Works on the day surfaces of `put_skew.build_day`
(``snap["expiries"][expiry_ms] = {"F", "T", "strikes": {K: {"mark_iv", "sell_iv", "buy_iv", ...}}}``; only strikes with
a fresh mark are present). Units: vols are decimals (0.45 = 45 %), T in years = days / 365, Black-76 with r = 0 on
each expiry's own forward, prices and P&L in USD per 1 BTC per leg (times the leg quantity).

* The condition is `option_ideas.check_front_inversion` on a surface: an FOMC / CPI event within ``FRONT_MAX_DAYS``
  of the reference time, the front = first expiry after the event with DTE <= ``FRONT_MAX_DAYS``, the month = the
  expiry in ``MONTH_BAND`` nearest ``MONTH_DAYS``; ATM IV = smile at the forward (as ``deribit_skew_term(view="atm")``).
* Static-arbitrage check of every mark that prices a leg (card 4.4 (a)): call prices at the fresh strikes around K
  must be monotone, with verticals no wider than the strike gap, and convex on every consecutive triple holding K.
* R = P&L / debit (debit = net premium paid plus entry fees): the return on the capital at risk of a long calendar.
  Bybit margins the sold straddle separately, so the return on actual margin is lower.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .option_condor import strike_for_delta
from .option_ideas import EVENT_KINDS, FRONT_INVERSION, FRONT_MAX_DAYS, MONTH_BAND, MONTH_DAYS, fee
from .options import black76_price
from .put_skew import iv_at_strike
from .trend_spread import DAY_MS, DELIVERY_CAP, DELIVERY_RATE, MIN_IV

ARB_TOL = 1e-6  # tolerance of the static-arbitrage inequalities, as a fraction of the forward
NY = ZoneInfo("America/New_York")


# ----------------------------------------------------------------------------- condition


def event_times_ms(rows: Iterable[Mapping[str, str]], kinds: Sequence[str] = EVENT_KINDS) -> list[int]:
    """UTC epoch ms of the macro events (``date``, ``kind``, ``time_et``, ``status`` rows of the events CSV) of the
    given kinds, cancelled ones excluded; ET -> UTC with daylight saving (America/New_York). Sorted, unique."""
    out = set()
    for r in rows:
        if r.get("kind") not in kinds or r.get("status") == "cancelled":
            continue
        hh, mm = (int(x) for x in str(r.get("time_et") or "08:30").split(":"))
        d = datetime.fromisoformat(str(r["date"])).replace(hour=hh, minute=mm, tzinfo=NY)
        out.add(int(d.timestamp() * 1000))
    return sorted(out)


def atm_iv(exp: dict[str, Any]) -> float | None:
    """Smile at the forward (linear in ln K/F between fresh strikes, flat beyond): ``deribit_skew_term(view="atm")`` -> atm_iv."""
    return iv_at_strike(exp, exp["F"])[0]


def month_expiry(snap: dict[str, Any], band: tuple[float, float] = MONTH_BAND, target: float = MONTH_DAYS) -> int | None:
    """Expiry with DTE in ``band`` nearest ``target`` days (ties: the earlier), as `option_ideas._month`."""
    ok = [(abs(x["T"] * 365 - target), x["T"], e) for e, x in snap["expiries"].items() if band[0] <= x["T"] * 365 <= band[1]]
    return min(ok)[2] if ok else None


def inversion_at(
    snap: dict[str, Any],
    events_ms: Sequence[int],
    *,
    front_max: float = FRONT_MAX_DAYS,
    threshold: float = FRONT_INVERSION,
    band: tuple[float, float] = MONTH_BAND,
    target: float = MONTH_DAYS,
) -> dict[str, Any]:
    """The P10 condition at the surface time ``snap["t_ms"]``.

    ``state``: "no_event" (no event in [t, t + front_max)), "no_month", "no_front" (no expiry after the event within
    ``front_max``), "no_atm" or "measured"; ``met`` is True / False when measured, False without an event or front,
    None when the month or an ATM IV is missing (not measured). Only events at or after t and only this surface enter.
    """
    t = snap["t_ms"]
    evs = [e for e in events_ms if 0 <= e - t < front_max * DAY_MS]
    out: dict[str, Any] = {"t_ms": t, "event_ms": min(evs) if evs else None, "front": None, "month": None, "front_atm": None}
    out.update({"month_atm": None, "diff": None, "met": False, "state": "no_event"})
    if not evs:
        return out
    m = month_expiry(snap, band, target)
    if m is None:
        return {**out, "met": None, "state": "no_month"}
    ev_days = (min(evs) - t) / DAY_MS
    rows = sorted((x["T"] * 365, e) for e, x in snap["expiries"].items())
    front = next((e for dte, e in rows if ev_days < dte <= front_max), None)
    if front is None:
        return {**out, "month": m, "state": "no_front"}
    fa, ma = atm_iv(snap["expiries"][front]), atm_iv(snap["expiries"][m])
    out.update({"front": front, "month": m, "front_atm": fa, "month_atm": ma})
    if fa is None or ma is None:
        return {**out, "met": None, "state": "no_atm"}
    diff = fa - ma
    return {**out, "diff": diff, "met": diff >= threshold, "state": "measured"}


# ----------------------------------------------------------------------------- static arbitrage of marks


def _call(exp: dict[str, Any], K: float, iv: float) -> float:
    return black76_price(exp["F"], K, exp["T"], iv, True)


def arb_check(exp: dict[str, Any], K: float, iv: float) -> dict[str, Any]:
    """Static no-arbitrage of the call price at K (IV ``iv``) against the fresh strikes of its expiry (r = 0).

    Pairs (K-, K) and (K, K+) with the nearest fresh strikes: monotone (C non-increasing in strike) and vertical <=
    strike gap (slope >= -1: digital <= 1). Every consecutive triple of fresh strikes that holds K
    ((K--, K-, K), (K-, K, K+), (K, K+, K++)): convex. Puts at the same IV and forward give the same conditions
    (parity). ``sides`` = number of neighbours of K (0-2); ``violations`` lists the failed conditions.
    """
    ks = sorted(k for k in exp["strikes"] if k != K)
    below = [k for k in ks if k < K]
    above = [k for k in ks if k > K]
    tol = ARB_TOL * exp["F"]
    price = {k: _call(exp, k, exp["strikes"][k]["mark_iv"]) for k in (below[-2:] + above[:2])}
    price[K] = _call(exp, K, iv)
    chain = below[-2:] + [K] + above[:2]
    viol: list[str] = []
    for a, b in zip(chain, chain[1:], strict=False):
        if K not in (a, b):
            continue
        if price[b] > price[a] + tol:
            viol.append(f"monotone {a:g}/{b:g}")
        if price[a] - price[b] > (b - a) + tol:
            viol.append(f"vertical {a:g}/{b:g}")
    for a, b, c in zip(chain, chain[1:], chain[2:], strict=False):
        lam = (c - b) / (c - a)
        if price[b] > lam * price[a] + (1.0 - lam) * price[c] + tol:
            viol.append(f"convexity {a:g}/{b:g}/{c:g}")
    return {"ok": not viol, "sides": int(bool(below)) + int(bool(above)), "violations": viol}


def _interp_neighbours(exp: dict[str, Any], K: float) -> float | None:
    below = [k for k in exp["strikes"] if k < K]
    above = [k for k in exp["strikes"] if k > K]
    if not below or not above:
        return None
    a, b = max(below), min(above)
    xa, xb, x = (math.log(k / exp["F"]) for k in (a, b, K))
    w = (x - xa) / (xb - xa)
    return (1.0 - w) * exp["strikes"][a]["mark_iv"] + w * exp["strikes"][b]["mark_iv"]


def clean_iv(exp: dict[str, Any], K: float, *, rule: str = "replace", at_exit: bool = False) -> tuple[float | None, str]:
    """(IV, status) of the mark at K after the registered static-arbitrage rule (card 4.4 (a)).

    The mark is K's own fresh mark, else `iv_at_strike` (interpolated). Status: "ok" (checked on both sides),
    "one_sided" (a neighbour on one side only: convexity at K not checkable), "replaced" (violation; IV replaced by the
    ln K/F interpolation between the neighbours and re-checked clean; only with ``rule="replace"``), "skip" (entry:
    the violation stays or cannot be repaired -> IV None, the day is skipped), "raw" (exit: the violation stays; the raw
    mark is kept and flagged, because skipping on exit data would select on the outcome).
    """
    iv = iv_at_strike(exp, K)[0]
    if iv is None:
        return None, "skip"
    chk = arb_check(exp, K, iv)
    if chk["ok"]:
        return iv, ("ok" if chk["sides"] == 2 else "one_sided")
    if rule == "replace":
        iv2 = _interp_neighbours(exp, K)
        if iv2 is not None and arb_check(exp, K, iv2)["ok"]:
            return iv2, "replaced"
    elif rule != "skip":
        raise ValueError(f"unknown arbitrage rule {rule!r}")
    return (iv, "raw") if at_exit else (None, "skip")


def variance_ok(iv_front: float, t_front: float, iv_month: float, t_month: float) -> bool:
    """Calendar no-arbitrage at one strike: total variance does not fall with maturity."""
    return iv_month * iv_month * t_month >= iv_front * iv_front * t_front


# ----------------------------------------------------------------------------- construction


def _leg(exp_key: str, exp: dict[str, Any], K: float, call: bool, qty: float, iv: float, status: str) -> dict[str, Any]:
    st = exp["strikes"].get(K, {})
    return {"exp": exp_key, "K": K, "call": call, "qty": qty, "mark": iv, "sell": st.get("sell_iv"), "buy": st.get("buy_iv"), "arb": status}


def _base(snap: dict[str, Any], inv: dict[str, Any]) -> dict[str, Any]:
    fx, mx = snap["expiries"][inv["front"]], snap["expiries"][inv["month"]]
    return {
        "day": snap["day"],
        "t_ms": snap["t_ms"],
        "spot": snap["spot"],
        "event_ms": inv["event_ms"],
        "diff": inv["diff"],
        "expiry": {"f": inv["front"], "m": inv["month"]},
        "F": {"f": fx["F"], "m": mx["F"]},
        "T": {"f": fx["T"], "m": mx["T"]},
    }


def plan_calendar(
    snap: dict[str, Any], inv: dict[str, Any], *, band: float = 0.015, month_interp: bool = False, arb_rule: str = "replace"
) -> tuple[dict[str, Any] | None, str]:
    """H1: sell 1 front straddle, buy 1 month straddle at the same K. K = the strike nearest the front forward among
    strikes fresh in both expiries (front only if ``month_interp``), within ``band`` x spot. (plan, reason)."""
    if inv.get("front") is None or inv.get("month") is None:
        return None, "condition not measured"
    fx, mx = snap["expiries"][inv["front"]], snap["expiries"][inv["month"]]
    S = snap["spot"]
    cands = [k for k in fx["strikes"] if month_interp or k in mx["strikes"]]
    if not cands:
        return None, "no common fresh strike"
    K = min(cands, key=lambda k: (abs(k - fx["F"]), k))
    if abs(K - fx["F"]) > band * S + 1e-9:
        return None, "no common fresh strike"
    iv_f, st_f = clean_iv(fx, K, rule=arb_rule)
    iv_m, st_m = clean_iv(mx, K, rule=arb_rule)
    if iv_f is None or iv_m is None:
        return None, "arbitrage in marks"
    if not variance_ok(iv_f, fx["T"], iv_m, mx["T"]):
        return None, "calendar variance"
    legs = [
        _leg("m", mx, K, True, 1.0, iv_m, st_m),
        _leg("m", mx, K, False, 1.0, iv_m, st_m),
        _leg("f", fx, K, True, -1.0, iv_f, st_f),
        _leg("f", fx, K, False, -1.0, iv_f, st_f),
    ]
    return {**_base(snap, inv), "kind": "calendar", "K": K, "legs": legs}, ""


def _nearest(exp: dict[str, Any], target: float, ok: Any, tol: float) -> float | None:
    cands = [k for k in exp["strikes"] if ok(k)]
    if not cands:
        return None
    K = min(cands, key=lambda k: (abs(k - target), k))
    return K if abs(K - target) <= tol + 1e-9 else None


def plan_diagonal(
    snap: dict[str, Any], inv: dict[str, Any], *, delta: float = 0.25, qty: float = 0.5, band: float = 0.015, arb_rule: str = "replace"
) -> tuple[dict[str, Any] | None, str]:
    """H3: buy the month |delta| strangle, sell ``qty`` front strangle at strikes no closer to spot than the bought ones."""
    if inv.get("front") is None or inv.get("month") is None:
        return None, "condition not measured"
    fx, mx = snap["expiries"][inv["front"]], snap["expiries"][inv["month"]]
    S, tol = snap["spot"], band * snap["spot"]
    tc, tp = strike_for_delta(mx, delta, True), strike_for_delta(mx, -delta, False)
    if tc is None or tp is None:
        return None, "no delta strike"
    K_mc = _nearest(mx, tc, lambda k: k > S, tol)
    K_mp = _nearest(mx, tp, lambda k: k < S, tol)
    if K_mc is None or K_mp is None:
        return None, "month: no fresh strike"
    K_fc = _nearest(fx, K_mc, lambda k: k >= K_mc, tol)
    K_fp = _nearest(fx, K_mp, lambda k: k <= K_mp, tol)
    if K_fc is None or K_fp is None:
        return None, "front: no fresh strike"
    ivs = {}
    for key, exp, K in (("mc", mx, K_mc), ("mp", mx, K_mp), ("fc", fx, K_fc), ("fp", fx, K_fp)):
        ivs[key] = clean_iv(exp, K, rule=arb_rule)
        if ivs[key][0] is None:
            return None, "arbitrage in marks"
    legs = [
        _leg("m", mx, K_mc, True, 1.0, *ivs["mc"]),
        _leg("m", mx, K_mp, False, 1.0, *ivs["mp"]),
        _leg("f", fx, K_fc, True, -qty, *ivs["fc"]),
        _leg("f", fx, K_fp, False, -qty, *ivs["fp"]),
    ]
    return {**_base(snap, inv), "kind": "diagonal", "K": {"mc": K_mc, "mp": K_mp, "fc": K_fc, "fp": K_fp}, "legs": legs}, ""


# ----------------------------------------------------------------------------- prices and P&L


def exec_ivs(plan: dict[str, Any], model: str = "mark", hs: float = 0.015) -> tuple[list[float], int]:
    """Entry IV per leg: bought at mark + hs, sold at mark - hs; ``model="side"``: the median taker-buy / taker-sell
    print IV of the strike when there is one (else mark +/- hs). Returns (ivs, legs that fell back to the mark)."""
    out, fb = [], 0
    for leg in plan["legs"]:
        side = leg["buy"] if leg["qty"] > 0 else leg["sell"]
        if model == "side" and side is not None:
            out.append(side)
        else:
            fb += model == "side"
            out.append(leg["mark"] + hs if leg["qty"] > 0 else leg["mark"] - hs)
    return out, fb


def entry_cost(plan: dict[str, Any], ivs: Sequence[float]) -> float | None:
    """Debit in USD: sum of qty x price plus |qty| x Bybit taker fee per leg. None if an IV <= 1 % or debit <= 0."""
    if any(iv <= MIN_IV for iv in ivs):
        return None
    S, c = plan["spot"], 0.0
    for leg, iv in zip(plan["legs"], ivs, strict=True):
        p = black76_price(plan["F"][leg["exp"]], leg["K"], plan["T"][leg["exp"]], iv, leg["call"])
        c += leg["qty"] * p + abs(leg["qty"]) * fee(S, p)
    return c if c > 0 else None


def _intrinsic(K: float, call: bool, price: float) -> float:
    return max(price - K, 0.0) if call else max(K - price, 0.0)


def settle_at_front(
    plan: dict[str, Any],
    debit: float,
    delivery_price: float,
    exit_snap: dict[str, Any] | None,
    *,
    hs: float = 0.015,
    arb_rule: str = "replace",
) -> tuple[dict[str, Any] | None, str]:
    """Front legs settle at ``delivery_price`` (qty x intrinsic, Bybit delivery fee on every ITM leg); month legs are
    sold on ``exit_snap`` (a surface at or after the front expiry) at mark - hs with a taker fee. (result, reason)."""
    if exit_snap is None:
        return None, "no exit surface"
    if exit_snap["t_ms"] < plan["expiry"]["f"]:
        raise ValueError("exit surface before the front expiry")
    mx = exit_snap["expiries"].get(plan["expiry"]["m"])
    if mx is None or mx["T"] <= 0:
        return None, "no exit surface"
    pnl, flags, S = -debit, [], exit_snap["spot"]
    for leg in plan["legs"]:
        if leg["exp"] == "f":
            v = _intrinsic(leg["K"], leg["call"], delivery_price)
            pnl += leg["qty"] * v - (abs(leg["qty"]) * min(DELIVERY_RATE * delivery_price, DELIVERY_CAP * v) if v > 0 else 0.0)
            continue
        iv, st = clean_iv(mx, leg["K"], rule=arb_rule, at_exit=True)
        flags.append(st)
        if iv is None:
            return None, "no exit mark"
        sold = leg["qty"] > 0  # a bought month leg is sold on exit
        p = black76_price(mx["F"], leg["K"], mx["T"], max(iv - hs, MIN_IV) if sold else iv + hs, leg["call"])
        pnl += leg["qty"] * p - abs(leg["qty"]) * fee(S, p)
    return {
        "day": plan["day"],
        "pnl": pnl,
        "r": pnl / debit,
        "pnl_spot": pnl / plan["spot"],
        "debit": debit,
        "exit_ms": exit_snap["t_ms"],
        "exit_arb": flags,
        "exit": "front expiry",
    }, ""


# ----------------------------------------------------------------------------- batch statistics and verdict


def batch_raw_p(raw: Mapping[str, float], degenerate: Iterable[str]) -> dict[str, float]:
    """Raw p of the item-4 batch for Holm (card 4.4 (b)): a hypothesis whose sample failed the tail gate counts as 1.0."""
    deg = set(degenerate)
    unknown = deg - set(raw)
    if unknown:
        raise ValueError(f"degenerate keys not in the batch: {sorted(unknown)}")
    return {k: (1.0 if k in deg else float(v)) for k, v in raw.items()}


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
    """Registered verdict (card 4.4 (в)), text and code identical.

    ✓: lo > 0, Holm p < 0.05, OOS mean > 0 and mean under the cost stress > 0. ✗: hi < 0, or mean <= 0 and hi < edge.
    ≈: neither, with mean, IS and OOS means > 0 (the CI holds 0, or lo > 0 but Holm or the cost stress fails).
    ◯: otherwise. A ✓ or ≈ whose tail gate or coverage gate fails is ◯.
    """
    if lo > 0 and holm_p < 0.05 and oos_mean > 0 and stress_cost_mean > 0:
        v = "✓"
    elif hi < 0 or (mean <= 0 and hi < edge):
        v = "✗"
    elif mean > 0 and is_mean > 0 and oos_mean > 0:
        v = "≈"
    else:
        v = "◯"
    if v in ("✓", "≈") and not (tail_ok and coverage_ok):
        return "◯"
    return v
