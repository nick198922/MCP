"""Put-skew premium research: daily option surfaces from Deribit trade prints and delta-hedged option books.

Pure functions, no I/O. Units and conventions:
  * vols are decimals (0.45 = 45 %); Deribit trade ``iv`` arrives in percent and is divided by 100 here;
  * Deribit BTC options are inverse: ``price_btc = Black76(F, K, T, sigma) / F`` (r = 0, F = the
    expiry forward). The forward of each print is solved from its own ``price`` and ``iv``;
  * T = (expiry 08:00 UTC - observation time) / 365 days;
  * P&L is USD-linear: the BTC premium is converted at the entry spot, the payoff is max(K - S_T, 0)
    (or max(S_T - K, 0)) USD at the delivery price, and the leg's USD value before expiry is
    ``Black76(F) * S / F``, whose spot delta equals the Black-76 forward delta. The hedge is a perp
    tracked by the index; perp funding (decimal per hour, longs pay when positive) is charged on it.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .instruments import parse_option_name
from .options import black76_greeks, black76_price

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
YEAR_MS = 365.0 * DAY_MS


def year_frac(expiry_ms: int, t_ms: int) -> float:
    return max(expiry_ms - t_ms, 0) / YEAR_MS


def inverse_price(F: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    """Deribit inverse option price in BTC."""
    return black76_price(F, K, T, sigma, is_call) / F


def implied_vol_inverse(price_btc: float, F: float, K: float, T: float, is_call: bool, lo: float = 0.005, hi: float = 6.0) -> float | None:
    """Black-76 vol that reproduces an inverse (BTC) option price; None if outside no-arbitrage bounds."""
    if T <= 0 or F <= 0 or price_btc <= 0:
        return None
    f_lo = inverse_price(F, K, T, lo, is_call) - price_btc
    f_hi = inverse_price(F, K, T, hi, is_call) - price_btc
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if inverse_price(F, K, T, mid, is_call) < price_btc:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


def implied_forward(price_btc: float, K: float, T: float, sigma: float, is_call: bool, spot: float) -> float | None:
    """Forward F for which ``Black76(F)/F`` equals the print's BTC price at its own IV (search 0.7-1.5 x spot).

    The inverse call price rises with F and the inverse put price falls with F, so the root is unique.
    """
    if T <= 0 or sigma <= 0 or price_btc <= 0 or spot <= 0:
        return None
    lo, hi = 0.7 * spot, 1.5 * spot
    sgn = 1.0 if is_call else -1.0
    g_lo = sgn * (inverse_price(lo, K, T, sigma, is_call) - price_btc)
    g_hi = sgn * (inverse_price(hi, K, T, sigma, is_call) - price_btc)
    if g_lo > 0 or g_hi < 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if sgn * (inverse_price(mid, K, T, sigma, is_call) - price_btc) < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6 * spot:
            break
    return 0.5 * (lo + hi)


def _interp(xs: Sequence[float], ys: Sequence[float], x: float, *, extrapolate: bool) -> float | None:
    if not xs:
        return None
    if x <= xs[0]:
        return ys[0] if extrapolate or x == xs[0] else None
    if x >= xs[-1]:
        return ys[-1] if extrapolate or x == xs[-1] else None
    i = bisect.bisect_right(xs, x)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


# Pre-registered v1 surface scheme (2 h window 08-10 UTC). Kept only to reproduce the v1 coverage
# report; it has known look-ahead/staleness flaws (prints after 09:00 used, window-median spot,
# oldest marks) that v2 fixes. See docs/reviews/2026-10-14-put-skew.md.
V1_BUILD: dict[str, Any] = {"cutoff": False, "spot_window_min": None, "mark_pick": "first", "max_mark_age_h": None}


def build_day(
    trades: list[dict[str, Any]],
    day_ms: int,
    *,
    ref_hour: float = 9.0,
    moneyness_band: float = 0.15,
    min_tenor_days: float = 1.0,
    max_forward_prints: int = 60,
    max_marks_per_strike: int = 15,
    cutoff: bool = True,
    spot_window_min: float | None = 15.0,
    mark_pick: str = "latest",
    max_mark_age_h: float | None = 6.0,
) -> dict[str, Any] | None:
    """One day's surface at the reference time ``t_ref = day + ref_hour`` from trade prints.

    Returns ``{"day", "t_ms", "spot", "expiries": {expiry_ms: {"ratio", "F", "T", "n_prints", "strikes": {K: {...}}}}}``.

    * ``cutoff``: prints after ``t_ref`` are dropped (no look-ahead).
    * spot: median ``index_price`` of the prints in the last ``spot_window_min`` minutes before t_ref,
      else the index of the latest print; ``None`` = median over all prints (v1).
    * ratio = median F/index over near-the-money prints (|ln(K/index)| <= ``moneyness_band``), F solved
      from each print's price and iv; with ``mark_pick="latest"`` the freshest ``max_forward_prints``
      within the age cap are used (all ages if none qualify), with ``"first"`` an even thinning (v1).
    * per strike: ``mark_iv`` = median of inverted mark prices of the freshest (``"latest"``) or first
      (``"first"``, v1) ``max_marks_per_strike`` prints no older than ``max_mark_age_h`` hours; a strike
      without a fresh print is left out (so callers interpolate from fresh strikes). Also ``mark_age_h``
      (age of the freshest print used), ``n``/``n_put`` (all prints / put prints in the window),
      ``sell_iv``/``buy_iv`` (median trade IV of taker-sell / taker-buy prints, i.e. bid / ask side) and
      ``put_delta`` at the mark IV on the reference forward and time.
    """
    t_ref = day_ms + int(ref_hour * HOUR_MS)
    rows = []
    for t in trades:
        on = parse_option_name(str(t.get("instrument_name") or ""))
        if on is None or on.product != "BTC":
            continue
        idx, ts = t.get("index_price"), t.get("timestamp")
        if not idx or not ts:
            continue
        if cutoff and int(ts) > t_ref:
            continue
        exp_ms = int(on.expiry.timestamp() * 1000)
        T = year_frac(exp_ms, int(ts))
        if T * 365 < min_tenor_days:
            continue
        rows.append((exp_ms, on.strike, on.is_call, float(idx), int(ts), T, t))
    if not rows:
        return None
    rows.sort(key=lambda r: r[4])
    if spot_window_min is None:
        spot = statistics.median(r[3] for r in rows)
    else:
        recent = [r[3] for r in rows if t_ref - spot_window_min * 60_000 <= r[4] <= t_ref]
        spot = statistics.median(recent) if recent else rows[-1][3]
    max_age_ms = max_mark_age_h * HOUR_MS if max_mark_age_h is not None else None

    def fresh(r: Any) -> bool:
        return max_age_ms is None or t_ref - r[4] <= max_age_ms

    by_exp: dict[int, list[Any]] = {}
    for r in rows:
        by_exp.setdefault(r[0], []).append(r)
    expiries: dict[int, dict[str, Any]] = {}
    for exp_ms, rs in by_exp.items():
        near = [r for r in rs if r[6].get("iv") and r[6].get("price") and abs(math.log(r[1] / r[3])) <= moneyness_band]
        if mark_pick == "first":
            use = _thin(near, max_forward_prints)
        else:
            pool = [r for r in near if fresh(r)] or near
            use = pool[-max_forward_prints:]
        ratios = []
        for _, K, is_call, idx, _, T, t in use:
            F = implied_forward(float(t["price"]), K, T, float(t["iv"]) / 100.0, is_call, idx)
            if F is not None:
                ratios.append(F / idx)
        if not ratios:
            continue
        ratio = statistics.median(ratios)
        T_ref = year_frac(exp_ms, t_ref)
        if T_ref <= 0:
            continue
        F_ref = ratio * spot
        by_k: dict[float, list[Any]] = {}
        for r in rs:
            by_k.setdefault(r[1], []).append(r)
        strikes: dict[float, dict[str, Any]] = {}
        for K, kr in by_k.items():
            cand = [r for r in kr if r[6].get("mark_price") and fresh(r)]
            cand = cand[:max_marks_per_strike] if mark_pick == "first" else cand[-max_marks_per_strike:]
            marks, ages = [], []
            for _, _, is_call, idx, ts, T, t in cand:
                v = implied_vol_inverse(float(t["mark_price"]), ratio * idx, K, T, is_call)
                if v is not None:
                    marks.append(v)
                    ages.append((t_ref - ts) / HOUR_MS)
            if not marks:
                continue
            miv = statistics.median(marks)
            sell = [float(r[6]["iv"]) / 100.0 for r in kr if r[6].get("iv") and r[6].get("direction") == "sell"]
            buy = [float(r[6]["iv"]) / 100.0 for r in kr if r[6].get("iv") and r[6].get("direction") != "sell"]
            strikes[K] = {
                "mark_iv": miv,
                "mark_age_h": min(ages),
                "n": len(kr),
                "n_put": sum(1 for r in kr if not r[2]),
                "sell_iv": statistics.median(sell) if sell else None,
                "buy_iv": statistics.median(buy) if buy else None,
                "put_delta": black76_greeks(F_ref, K, T_ref, miv, False)["delta"],
            }
        if strikes:
            expiries[exp_ms] = {"ratio": ratio, "F": F_ref, "T": T_ref, "n_prints": len(rs), "strikes": strikes}
    if not expiries:
        return None
    return {"day": day_ms, "t_ms": t_ref, "spot": spot, "expiries": expiries}


def _thin(xs: list[Any], k: int) -> list[Any]:
    if len(xs) <= k:
        return xs
    step = len(xs) / k
    return [xs[int(i * step)] for i in range(k)]


def iv_at_strike(exp: dict[str, Any], K: float) -> tuple[float | None, str]:
    """Mark IV at strike K: own mark if traded, else linear in ln(K/F) (flat beyond the traded range)."""
    st = exp["strikes"]
    if K in st:
        return st[K]["mark_iv"], "own"
    ks = sorted(st)
    if not ks:
        return None, "none"
    xs = [math.log(k / exp["F"]) for k in ks]
    return _interp(xs, [st[k]["mark_iv"] for k in ks], math.log(K / exp["F"]), extrapolate=True), "interp"


def iv_at_put_delta(exp: dict[str, Any], put_delta: float) -> float | None:
    """Mark IV at a put delta (e.g. -0.25; a 25-delta call is put delta -0.75), interpolated, no extrapolation."""
    pts = sorted((v["put_delta"], v["mark_iv"]) for v in exp["strikes"].values())
    return _interp([p[0] for p in pts], [p[1] for p in pts], put_delta, extrapolate=False)


def rr25(exp: dict[str, Any]) -> float | None:
    c, p = iv_at_put_delta(exp, -0.75), iv_at_put_delta(exp, -0.25)
    return c - p if c is not None and p is not None else None


EXPIRY_RULES = ("nearest", "calendar_monthly", "most_liquid_total", "most_liquid_puts")


def is_monthly_expiry(expiry_ms: int) -> bool:
    """Deribit monthly/quarterly expiry: the last Friday of its calendar month."""
    d = datetime.fromtimestamp(expiry_ms / 1000, UTC)
    return d.weekday() == 4 and (d + timedelta(days=7)).month != d.month


def wing_put_prints(x: dict[str, Any], delta_band: tuple[float, float]) -> int:
    return sum(v["n_put"] for v in x["strikes"].values() if delta_band[0] <= -v["put_delta"] <= delta_band[1])


def choose_expiry(
    snap: dict[str, Any],
    target_days: float,
    lo: float,
    hi: float,
    *,
    rule: str = "nearest",
    delta_band: tuple[float, float] = (0.20, 0.30),
) -> int | None:
    """Expiry with DTE in [lo, hi] (DTE from the reference time).

    ``nearest``: closest to ``target_days`` (ties: earlier). ``calendar_monthly``: the same but only
    last-Friday-of-month expiries (data-independent; the v2 primary rule). ``most_liquid_total``: most
    prints in the window. ``most_liquid_puts``: most put prints with |delta| in ``delta_band``
    (exploratory only: it selects on put demand, i.e. on the skew signal itself).
    """
    if rule not in EXPIRY_RULES:
        raise ValueError(f"unknown expiry rule {rule!r}")
    best = None
    for e, x in snap["expiries"].items():
        dte = x["T"] * 365
        if not lo <= dte <= hi:
            continue
        if rule == "calendar_monthly" and not is_monthly_expiry(e):
            continue
        if rule == "most_liquid_total":
            key: tuple[float, ...] = (-x.get("n_prints", 0), abs(dte - target_days), e)
        elif rule == "most_liquid_puts":
            key = (-wing_put_prints(x, delta_band), abs(dte - target_days), e)
        else:
            key = (abs(dte - target_days), e)
        if best is None or key < best[0]:
            best = (key, e)
    return best[1] if best else None


def plan_entry(
    snap: dict[str, Any],
    *,
    target_days: float = 30,
    dte_band: tuple[float, float] = (21, 45),
    target_delta: float = 0.25,
    delta_band: tuple[float, float] = (0.20, 0.30),
    min_wing_trades: int = 3,
    expiry_rule: str = "nearest",
) -> dict[str, Any] | None:
    """Pick expiry (``expiry_rule``, see `choose_expiry`), the traded 25-delta put strike and the ATM strike.

    ``covered`` is the pre-registered coverage condition: >= ``min_wing_trades`` put prints with
    |delta| in ``delta_band`` in the chosen expiry and an ATM strike with call delta in [0.40, 0.60].
    Strikes are those with a fresh mark (see `build_day`), so the 25-delta strike's mark obeys the age cap.
    """
    e = choose_expiry(snap, target_days, *dte_band, rule=expiry_rule, delta_band=delta_band)
    if e is None:
        return None
    x = snap["expiries"][e]
    st = x["strikes"]
    wing_trades = wing_put_prints(x, delta_band)
    cand = [(abs(-v["put_delta"] - target_delta), K) for K, v in st.items() if delta_band[0] <= -v["put_delta"] <= delta_band[1]]
    kp = min(cand)[1] if cand else None
    atm = min(((abs(K - x["F"]), K) for K in st), default=None)
    ka = atm[1] if atm else None
    atm_ok = ka is not None and 0.40 <= 1.0 + st[ka]["put_delta"] <= 0.60
    return {
        "day": snap["day"],
        "expiry": e,
        "dte": x["T"] * 365,
        "spot": snap["spot"],
        "F": x["F"],
        "T": x["T"],
        "kp": kp,
        "ka": ka if atm_ok else None,
        "kp_mark_age_h": st[kp].get("mark_age_h") if kp is not None else None,
        "ka_mark_age_h": st[ka].get("mark_age_h") if ka is not None else None,
        "wing_trades": wing_trades,
        "covered": wing_trades >= min_wing_trades and kp is not None and atm_ok,
        "rr25": rr25(x),
        "skew25": (iv_at_put_delta(x, -0.25) - st[ka]["mark_iv"]) if atm_ok and iv_at_put_delta(x, -0.25) is not None else None,
    }


def funding_sum(funding: Sequence[tuple[int, float]], t0: int, t1: int) -> float:
    """Sum of hourly funding records with timestamp in (t0, t1]. ``funding`` sorted by timestamp."""
    ts = [f[0] for f in funding]
    i, j = bisect.bisect_right(ts, t0), bisect.bisect_right(ts, t1)
    return sum(f[1] for f in funding[i:j])


def run_book(
    days: dict[int, dict[str, Any]],
    entry_day: int,
    expiry_ms: int,
    legs: list[tuple[float, bool, float, float]],
    delivery_price: float,
    *,
    funding: Sequence[tuple[int, float]] = (),
    option_fee: float = 0.0003,
    delivery_fee: float = 0.00015,
    fee_cap: float = 0.125,
    hedge_fee: float = 0.0005,
    hedge: bool = True,
) -> dict[str, Any] | None:
    """USD-linear P&L of an option book held to expiry with a daily perp delta hedge.

    ``legs`` = [(strike, is_call, qty, exec_iv)], qty < 0 = sold. Executed at ``exec_iv`` on the entry
    day's forward. Re-hedges at each later day's reference time (days missing from ``days`` keep the
    previous hedge) using each leg's mark IV (own strike, else interpolated, else the previous day's).
    Settles legs and the hedge at ``delivery_price``. Only data up to ``expiry_ms`` is read.
    Returns P&L as a fraction of entry spot plus diagnostics.
    """
    snap0 = days.get(entry_day)
    if snap0 is None or expiry_ms not in snap0["expiries"]:
        return None
    x0 = snap0["expiries"][expiry_ms]
    s0, f0, t0 = snap0["spot"], x0["F"], x0["T"]
    premium = 0.0
    fees = 0.0
    vega0 = 0.0
    for K, is_call, q, iv in legs:
        px_usd = inverse_price(f0, K, t0, iv, is_call) * s0
        premium += -q * px_usd
        fees += abs(q) * min(option_fee * s0, fee_cap * px_usd)
        vega0 += q * black76_greeks(f0, K, t0, iv, is_call)["vega"]
    last_iv = [iv_at_strike(x0, K)[0] or iv for K, _, _, iv in legs]
    pos, hedge_pnl, fund_pnl, hedge_cost = 0.0, 0.0, 0.0, 0.0
    stale, n_hedge = 0, 0
    d = entry_day
    last_ratio = x0["ratio"]
    path: list[tuple[int, float, float]] = []  # (t_ms, spot, position)
    while d + DAY_MS <= expiry_ms:
        snap = days.get(d)
        if snap is not None and snap["t_ms"] < expiry_ms:
            s = snap["spot"]
            x = snap["expiries"].get(expiry_ms)
            if x is not None:
                last_ratio = x["ratio"]
            F = last_ratio * s
            T = year_frac(expiry_ms, snap["t_ms"])
            delta = 0.0
            for i, (K, is_call, q, _) in enumerate(legs):
                v, how = iv_at_strike(x, K) if x is not None else (None, "none")
                if v is None:
                    stale += 1
                    v = last_iv[i]
                last_iv[i] = v
                delta += q * black76_greeks(F, K, T, v, is_call)["delta"]
            target = -delta if hedge else 0.0
            hedge_cost += hedge_fee * abs(target - pos) * s
            if path:
                pt, ps, pp = path[-1]
                hedge_pnl += pp * (s - ps)
                fund_pnl -= pp * ps * funding_sum(funding, pt, snap["t_ms"])
            pos = target
            path.append((snap["t_ms"], s, pos))
            n_hedge += 1
        d += DAY_MS
    if path:
        pt, ps, pp = path[-1]
        hedge_pnl += pp * (delivery_price - ps)
        fund_pnl -= pp * ps * funding_sum(funding, pt, expiry_ms)
        hedge_cost += hedge_fee * abs(pp) * delivery_price
    payoff = 0.0
    for K, is_call, q, _ in legs:
        intrinsic = max(delivery_price - K, 0.0) if is_call else max(K - delivery_price, 0.0)
        payoff += q * intrinsic
        if intrinsic > 0:
            fees += abs(q) * min(delivery_fee * delivery_price, fee_cap * intrinsic)
    total = premium + payoff + hedge_pnl + fund_pnl - hedge_cost - fees
    return {
        "day": entry_day,
        "expiry": expiry_ms,
        "spot": s0,
        "pnl": total / s0,
        "pnl_btc_at_expiry": total / delivery_price,  # BTC-denominated result per 1 BTC notional
        "premium": premium / s0,
        "payoff": payoff / s0,
        "hedge_pnl": hedge_pnl / s0,
        "funding_pnl": fund_pnl / s0,
        "costs": (hedge_cost + fees) / s0,
        "vega": vega0,  # USD per vol point of the whole book, signed
        "move": delivery_price / s0 - 1.0,
        "stale_leg_days": stale,
        "hedge_days": n_hedge,
    }


def dvol_known_at_entry(dvol: Sequence[tuple[int, float]]) -> dict[int, float]:
    """Map entry day -> DVOL known at that day's 09:00 UTC entry: the close of the previous daily candle.

    Deribit daily DVOL candles are stamped at 00:00 UTC of their day and close at 24:00, so the candle
    stamped ``d`` is only known after the day ends; for entry day ``d`` use the candle stamped ``d - 1 day``.
    """
    return {(int(t) // DAY_MS) * DAY_MS + DAY_MS: float(v) for t, v in dvol}


def trailing_percentile(series: Sequence[tuple[int, float]], *, window_ms: int, min_obs: int) -> dict[int, float]:
    """Percentile of each value among values strictly earlier and within ``window_ms`` (no lookahead)."""
    out: dict[int, float] = {}
    pts = sorted(series)
    for i, (t, v) in enumerate(pts):
        past = [w for s, w in pts[:i] if t - s <= window_ms]
        if len(past) >= min_obs:
            past.sort()
            out[t] = (bisect.bisect_left(past, v) + bisect.bisect_right(past, v)) / 2 / len(past)
    return out


def chain(entries: dict[int, dict[str, Any]], start_day: int, last_day: int) -> list[int]:
    """Non-overlapping entry days: enter at the first valid day >= start, re-enter on the expiry day."""
    out, d = [], start_day
    while d <= last_day:
        e = entries.get(d)
        if e is None:
            d += DAY_MS
            continue
        out.append(d)
        d = (int(e["expiry"]) // DAY_MS) * DAY_MS
    return out


def tercile_spread(rows: Sequence[dict[str, float]], *, y: str = "pnl", sort_key: str = "rr_pct", ctrl: str = "dvol_pct") -> float | None:
    """Mean over the two control halves (ctrl < 0.5, >= 0.5) of mean(y | sort_key < 1/3) - mean(y | sort_key >= 2/3)."""
    diffs = []
    for half in (lambda c: c < 0.5, lambda c: c >= 0.5):
        part = [r for r in rows if half(r[ctrl])]
        top = [r[y] for r in part if r[sort_key] < 1 / 3]
        bot = [r[y] for r in part if r[sort_key] >= 2 / 3]
        if not top or not bot:
            return None
        diffs.append(statistics.mean(top) - statistics.mean(bot))
    return statistics.mean(diffs)


def block_bootstrap_ci(
    rows: Sequence[Any], stat: Callable[[Sequence[Any]], float | None], *, block: int = 30, n_boot: int = 2000, seed: int = 7
) -> tuple[float, float] | None:
    """Moving-block bootstrap 95 % CI of ``stat`` over time-ordered ``rows``."""
    n = len(rows)
    if n < 2 * block:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        sample: list[Any] = []
        while len(sample) < n:
            i = rng.randrange(0, n - block + 1)
            sample.extend(rows[i : i + block])
        v = stat(sample[:n])
        if v is not None:
            vals.append(v)
    if len(vals) < n_boot // 2:
        return None
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]
