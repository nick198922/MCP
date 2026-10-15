"""Weekly ATM straddle premium on Deribit BTC (IDEA-20261015-weekly-straddle-vrp-term-slope).

Pure functions, no I/O; time is a parameter (epoch ms, UTC). Builds on the day surfaces and the book
model of `put_skew` (same units):
  * vols are decimals (0.45 = 45 %), T in years = days / 365, Deribit expiries at 08:00 UTC;
  * option values are USD-linear: ``Black76(F) * S / F`` (inverse price converted at spot);
  * P&L is a fraction of entry spot per 1 BTC notional (0.01 = 1 % of spot), i.e. on notional, not capital.

`book_path` reproduces `put_skew.run_book` exactly and additionally splits the result into an entry
component (execution vs mark and option fees), mark-to-market intervals between the daily hedge points and
the settlement interval, so that e.g. the weekend's share of a weekly trade can be shown separately.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from typing import Any

from .options import black76_greeks
from .put_skew import FundingIndex, inverse_price, iv_at_strike, year_frac

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
WEEK_MS = 7 * DAY_MS


def is_friday(day_ms: int) -> bool:
    """True if the UTC day starting at ``day_ms`` is a Friday (1970-01-02 was a Friday)."""
    return (day_ms // DAY_MS - 1) % 7 == 0


def fridays(first_ms: int, last_ms: int) -> list[int]:
    """00:00 UTC of every Friday in [first_ms, last_ms]."""
    d = (first_ms // DAY_MS) * DAY_MS
    while not is_friday(d):
        d += DAY_MS
    out = []
    while d <= last_ms:
        out.append(d)
        d += WEEK_MS
    return out


def friday_expiry(entry_day_ms: int, weeks: int = 1) -> int:
    """The Friday 08:00 UTC expiry ``weeks`` weeks after the entry Friday."""
    return entry_day_ms + weeks * WEEK_MS + 8 * HOUR_MS


def atm_plan(snap: dict[str, Any], expiry_ms: int, *, call_delta_band: tuple[float, float] = (0.40, 0.60)) -> dict[str, Any] | None:
    """ATM strike of ``expiry_ms`` in a day surface: the fresh-mark strike nearest the forward.

    Returns None if the expiry is absent or has no strikes; ``covered`` is False if the strike's call delta
    (at its mark IV) is outside ``call_delta_band``.
    """
    x = snap["expiries"].get(expiry_ms)
    if x is None or not x["strikes"]:
        return None
    st = x["strikes"]
    K = min(st, key=lambda k: (abs(k - x["F"]), k))
    cd = 1.0 + st[K]["put_delta"]
    ks = sorted(st)
    i = ks.index(K)
    near = ks[max(0, i - 2) : i + 3]
    return {
        "day": snap["day"],
        "expiry": expiry_ms,
        "K": K,
        "F": x["F"],
        "T": x["T"],
        "spot": snap["spot"],
        "mark_iv": st[K]["mark_iv"],
        "sell_iv": st[K].get("sell_iv"),
        "call_delta": cd,
        "mark_age_h": st[K].get("mark_age_h"),
        "prints_near": sum(int(st[k].get("n", 0)) for k in near),
        "covered": call_delta_band[0] <= cd <= call_delta_band[1],
    }


def atm_iv(exp: dict[str, Any]) -> float | None:
    """Mark IV at K = F of one expiry (linear in ln K/F between fresh strikes, flat beyond)."""
    return iv_at_strike(exp, exp["F"])[0]


def constant_maturity_atm_iv(snap: dict[str, Any], target_days: float = 30.0) -> float | None:
    """ATM IV at a constant maturity: total variance sigma^2 T linear in T between the expiries around it."""
    pts = []
    for x in snap["expiries"].values():
        v = atm_iv(x)
        if v is not None and x["T"] > 0:
            pts.append((x["T"], v))
    pts.sort()
    tt = target_days / 365.0
    lo = [p for p in pts if p[0] <= tt]
    hi = [p for p in pts if p[0] >= tt]
    if not lo or not hi:
        return None
    (t0, v0), (t1, v1) = lo[-1], hi[0]
    if t1 == t0:
        return v0
    w = v0 * v0 * t0 + (v1 * v1 * t1 - v0 * v0 * t0) * (tt - t0) / (t1 - t0)
    return math.sqrt(w / tt) if w > 0 else None


def term_slope(snap: dict[str, Any], expiry_ms: int, target_days: float = 30.0) -> float | None:
    """ATM IV of ``expiry_ms`` over the constant-maturity ``target_days`` ATM IV in the same surface (> 1 = backwardation)."""
    x = snap["expiries"].get(expiry_ms)
    if x is None:
        return None
    a, b = atm_iv(x), constant_maturity_atm_iv(snap, target_days)
    return a / b if a is not None and b else None


def pre_entry_slope(days: dict[int, dict[str, Any]], entry_day: int, expiry_ms: int, target_days: float = 30.0) -> float | None:
    """H2 slope from the previous day's 09:00 surface (``entry_day - 1 day``), never the entry snapshot itself.

    Keeps the slope's IV marks disjoint from the entry-price marks (no errors-in-variables / lookahead).
    """
    snap = days.get(entry_day - DAY_MS)
    return term_slope(snap, expiry_ms, target_days) if snap is not None else None


def book_path(
    days: dict[int, dict[str, Any]],
    entry_day: int,
    expiry_ms: int,
    legs: list[tuple[float, bool, float, float]],
    delivery_price: float,
    *,
    funding: Sequence[tuple[int, float]] | FundingIndex = (),
    option_fee: float = 0.0003,
    delivery_fee: float = 0.00015,
    fee_cap: float = 0.125,
    hedge_fee: float = 0.0005,
    hedge: bool = True,
) -> dict[str, Any] | None:
    """`put_skew.run_book` with a mark-to-market decomposition (same arguments, same ``pnl``).

    Extra keys (fractions of entry spot): ``entry`` = premium at execution IV minus the book's value at the
    entry marks minus option fees (the spread and fees paid); ``intervals`` = [(t_start, t_end, pnl)] between
    consecutive hedge points and from the last one to expiry, each = book revaluation at the day's mark IV +
    hedge P&L + funding - hedge fees traded at ``t_end`` (settlement interval includes delivery fees);
    ``min_mtm`` = lowest cumulative P&L at the hedge points (entry included; daily points miss intraday lows);
    ``spots`` = [(t, spot)] of the hedge points plus (expiry, delivery price).
    """
    snap0 = days.get(entry_day)
    if snap0 is None or expiry_ms not in snap0["expiries"]:
        return None
    fidx = funding if isinstance(funding, FundingIndex) else FundingIndex(funding)
    x0 = snap0["expiries"][expiry_ms]
    s0, f0, t0 = snap0["spot"], x0["F"], x0["T"]
    premium = fees = vega0 = 0.0
    for K, is_call, q, iv in legs:
        px_usd = inverse_price(f0, K, t0, iv, is_call) * s0
        premium += -q * px_usd
        fees += abs(q) * min(option_fee * s0, fee_cap * px_usd)
        vega0 += q * black76_greeks(f0, K, t0, iv, is_call)["vega"]
    last_iv = [iv_at_strike(x0, K)[0] or iv for K, _, _, iv in legs]
    v_entry = sum(q * inverse_price(f0, K, t0, last_iv[i], c) * s0 for i, (K, c, q, _) in enumerate(legs))
    entry = premium + v_entry - fees
    pos = hedge_pnl = fund_pnl = hedge_cost = 0.0
    stale = n_hedge = 0
    d = entry_day
    last_ratio = x0["ratio"]
    path: list[tuple[int, float, float, float]] = []  # (t_ms, spot, position, book value)
    intervals: list[tuple[int, int, float]] = []
    cum, min_mtm = entry, entry
    while d + DAY_MS <= expiry_ms:
        snap = days.get(d)
        if snap is not None and snap["t_ms"] < expiry_ms:
            s = snap["spot"]
            x = snap["expiries"].get(expiry_ms)
            if x is not None:
                last_ratio = x["ratio"]
            F = last_ratio * s
            T = year_frac(expiry_ms, snap["t_ms"])
            delta = value = 0.0
            for i, (K, is_call, q, _) in enumerate(legs):
                v, _how = iv_at_strike(x, K) if x is not None else (None, "none")
                if v is None:
                    stale += 1
                    v = last_iv[i]
                last_iv[i] = v
                delta += q * black76_greeks(F, K, T, v, is_call)["delta"]
                value += q * inverse_price(F, K, T, v, is_call) * s
            target = -delta if hedge else 0.0
            c = hedge_fee * abs(target - pos) * s
            hedge_cost += c
            if path:
                pt, ps, pp, pv = path[-1]
                h = pp * (s - ps)
                fu = -pp * ps * fidx.sum(pt, snap["t_ms"])
                hedge_pnl += h
                fund_pnl += fu
                step = (value - pv) + h + fu - c
                intervals.append((pt, snap["t_ms"], step / s0))
                cum += step
                min_mtm = min(min_mtm, cum)
            else:
                cum -= c
                entry -= c
                min_mtm = min(min_mtm, cum)
            pos = target
            path.append((snap["t_ms"], s, pos, value))
            n_hedge += 1
        d += DAY_MS
    payoff = 0.0
    dfees = 0.0
    for K, is_call, q, _ in legs:
        intrinsic = max(delivery_price - K, 0.0) if is_call else max(K - delivery_price, 0.0)
        payoff += q * intrinsic
        if intrinsic > 0:
            dfees += abs(q) * min(delivery_fee * delivery_price, fee_cap * intrinsic)
    fees += dfees
    if path:
        pt, ps, pp, pv = path[-1]
        h = pp * (delivery_price - ps)
        fu = -pp * ps * fidx.sum(pt, expiry_ms)
        c = hedge_fee * abs(pp) * delivery_price
        hedge_pnl += h
        fund_pnl += fu
        hedge_cost += c
        intervals.append((pt, expiry_ms, ((payoff - pv) + h + fu - c - dfees) / s0))
    else:  # no hedge point before expiry: the whole trade is one interval from the entry marks
        intervals.append((snap0["t_ms"], expiry_ms, (payoff - v_entry - dfees) / s0))
    total = premium + payoff + hedge_pnl + fund_pnl - hedge_cost - fees
    return {
        "day": entry_day,
        "expiry": expiry_ms,
        "spot": s0,
        "pnl": total / s0,
        "pnl_btc_at_expiry": total / delivery_price,
        "premium": premium / s0,
        "payoff": payoff / s0,
        "hedge_pnl": hedge_pnl / s0,
        "funding_pnl": fund_pnl / s0,
        "costs": (hedge_cost + fees) / s0,
        "vega": vega0,
        "move": delivery_price / s0 - 1.0,
        "stale_leg_days": stale,
        "hedge_days": n_hedge,
        "entry": entry / s0,
        "intervals": intervals,
        "min_mtm": min(min_mtm, total) / s0,
        "spots": [(p[0], p[1]) for p in path] + [(expiry_ms, delivery_price)],
    }


def realized_vol_path(spots: Sequence[tuple[int, float]]) -> float | None:
    """Annualised zero-mean realized vol of log returns between consecutive (t_ms, price) points over their span."""
    if len(spots) < 2:
        return None
    span = (spots[-1][0] - spots[0][0]) / (365.0 * DAY_MS)
    if span <= 0:
        return None
    ss = sum(math.log(b[1] / a[1]) ** 2 for a, b in zip(spots, spots[1:], strict=False))
    return math.sqrt(ss / span)


def deribit_im_estimate(K: float, S: float, is_call: bool, mark_btc: float) -> float:
    """Rough initial margin of one short inverse option in BTC: max(0.15 - OTM/S, 0.10) + mark (not verified against Deribit's spec)."""
    otm = max(K - S, 0.0) if is_call else max(S - K, 0.0)
    return max(0.15 - otm / S, 0.10) + mark_btc


def newey_west_mean(values: Sequence[float], lag: int) -> dict[str, Any]:
    """Mean with a Newey-West (Bartlett) standard error and normal 95 % CI."""
    n = len(values)
    if n < 2:
        return {"n": n, "mean": values[0] if n else float("nan"), "se": None, "ci95": None}
    m = sum(values) / n
    e = [v - m for v in values]
    lrv = sum(x * x for x in e) / n
    for k in range(1, min(lag, n - 1) + 1):
        g = sum(e[t] * e[t - k] for t in range(k, n)) / n
        lrv += 2.0 * (1.0 - k / (lag + 1)) * g
    se = math.sqrt(max(lrv, 0.0) / n)
    return {"n": n, "mean": m, "se": se, "ci95": (m - 1.96 * se, m + 1.96 * se)}


def mde(sd: float, n: int, k: float = 2.8) -> float:
    """Minimum detectable effect of a mean (two-sided 5 %, 80 % power): k * SD / sqrt(n)."""
    return k * sd / math.sqrt(n) if n > 0 else float("inf")


def threshold_spread(
    rows: Sequence[dict[str, float]], *, thr: float = 1.03, y: str = "pnl", key: str = "slope", ctrl: str = "dvol_pct", min_n: int = 10
) -> float | None:
    """Mean over control halves (ctrl < 0.5, >= 0.5) of mean(y | key > thr) - mean(y | key <= thr); None if a cell has < min_n."""
    diffs = []
    for half in (lambda c: c < 0.5, lambda c: c >= 0.5):
        part = [r for r in rows if half(r[ctrl])]
        hi = [r[y] for r in part if r[key] > thr]
        lo = [r[y] for r in part if r[key] <= thr]
        if len(hi) < min_n or len(lo) < min_n:
            return None
        diffs.append(statistics.mean(hi) - statistics.mean(lo))
    return statistics.mean(diffs)


def permutation_p(
    rows: Sequence[dict[str, float]], *, thr: float = 1.03, n_perm: int = 5000, seed: int = 20261015, min_n: int = 10
) -> float | None:
    """Two-sided p of `threshold_spread`, permuting the slope labels within each control half."""
    obs = threshold_spread(rows, thr=thr, min_n=min_n)
    if obs is None:
        return None
    rng = random.Random(seed)
    halves = [[r for r in rows if r["dvol_pct"] < 0.5], [r for r in rows if r["dvol_pct"] >= 0.5]]
    hit = 0
    for _ in range(n_perm):
        perm: list[dict[str, float]] = []
        for part in halves:
            sl = [r["slope"] for r in part]
            rng.shuffle(sl)
            perm.extend({"pnl": r["pnl"], "dvol_pct": r["dvol_pct"], "slope": s} for r, s in zip(part, sl, strict=True))
        v = threshold_spread(perm, thr=thr, min_n=min_n)
        if v is not None and abs(v) >= abs(obs) - 1e-15:
            hit += 1
    return (hit + 1) / (n_perm + 1)
