"""Options maths on top of Deribit data.

Conventions (kept identical everywhere):
* IV is a decimal (0.38 = 38 %), converted from Deribit's percent.
* Black-76 with r = 0 on the forward `underlying_price` of each expiry. This reproduces Deribit's
  own delta/gamma/vega/theta (checked against a live ticker in tests).
* vega is per 1 vol point (1 %), theta per calendar day, both in USD per 1 unit of underlying.
* Open interest is in underlying units (BTC for BTC options).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np

from .instruments import parse_option_name

SECONDS_PER_YEAR = 365.0 * 86400.0


def ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def year_fraction(expiry: datetime, now: datetime) -> float:
    return max((expiry - now).total_seconds(), 0.0) / SECONDS_PER_YEAR


def d1d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    return d1, d1 - v


def black76_price(F: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if is_call else max(K - F, 0.0)
    d1, d2 = d1d2(F, K, T, sigma)
    if is_call:
        return F * ncdf(d1) - K * ncdf(d2)
    return K * ncdf(-d2) - F * ncdf(-d1)


def black76_greeks(F: float, K: float, T: float, sigma: float, is_call: bool) -> dict[str, float]:
    if T <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    sqrt_t = math.sqrt(T)
    d1, _ = d1d2(F, K, T, sigma)
    pdf = npdf(d1)
    return {
        "delta": ncdf(d1) if is_call else ncdf(d1) - 1.0,
        "gamma": pdf / (F * sigma * sqrt_t),
        "vega": F * pdf * sqrt_t / 100.0,
        "theta": -F * pdf * sigma / (2.0 * sqrt_t) / 365.0,
    }


@dataclass
class OptionRow:
    instrument: str
    expiry: datetime
    strike: float
    is_call: bool
    iv: float | None
    forward: float
    index: float | None
    T: float
    mark_price_native: float | None
    mark_price_usd: float | None
    open_interest: float
    volume: float
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        d["type"] = "call" if self.is_call else "put"
        d.pop("is_call")
        return d


def build_chain(summaries: Iterable[dict[str, Any]], now: datetime) -> list[OptionRow]:
    """Normalise Deribit `get_book_summary_by_currency(kind=option)` rows and add greeks."""
    rows: list[OptionRow] = []
    for s in summaries:
        parsed = parse_option_name(s.get("instrument_name", ""))
        if parsed is None:
            continue
        fwd = s.get("underlying_price")
        if not fwd:
            continue
        index = s.get("estimated_delivery_price")
        mark_iv = s.get("mark_iv")
        iv = mark_iv / 100.0 if mark_iv and mark_iv > 0 else None
        T = year_fraction(parsed.expiry, now)
        mark = s.get("mark_price")
        inverse = s.get("quote_currency") == s.get("base_currency")
        mark_usd = (mark * (index or fwd) if inverse else mark) if mark is not None else None
        row = OptionRow(
            instrument=parsed.raw,
            expiry=parsed.expiry,
            strike=parsed.strike,
            is_call=parsed.is_call,
            iv=iv,
            forward=float(fwd),
            index=float(index) if index else None,
            T=T,
            mark_price_native=mark,
            mark_price_usd=mark_usd,
            open_interest=float(s.get("open_interest") or 0.0),
            volume=float(s.get("volume") or 0.0),
        )
        if iv and T > 0:
            g = black76_greeks(row.forward, row.strike, T, iv, row.is_call)
            row.delta, row.gamma, row.vega, row.theta = g["delta"], g["gamma"], g["vega"], g["theta"]
        rows.append(row)
    return rows


def group_by_expiry(rows: Iterable[OptionRow]) -> dict[datetime, list[OptionRow]]:
    out: dict[datetime, list[OptionRow]] = {}
    for r in rows:
        out.setdefault(r.expiry, []).append(r)
    return dict(sorted(out.items()))


class Smile:
    """IV as a function of strike for one expiry, built from OTM options (puts below F, calls above)."""

    def __init__(self, forward: float, T: float, strikes: list[float], ivs: list[float]) -> None:
        order = np.argsort(strikes)
        self.forward = forward
        self.T = T
        self.strikes = np.array(strikes, dtype=float)[order]
        self.ivs = np.array(ivs, dtype=float)[order]
        self._logm = np.log(self.strikes / forward)

    def iv(self, K: float) -> float:
        """Linear interpolation in log-moneyness, flat outside the quoted range."""
        return float(np.interp(math.log(K / self.forward), self._logm, self.ivs))

    def covers(self, K: float) -> bool:
        return bool(self.strikes[0] <= K <= self.strikes[-1])

    def iv_at_call_delta(self, target: float) -> float | None:
        """IV at a given call-equivalent delta N(d1) (0.25 -> 25-delta call, 0.75 -> 25-delta put)."""
        deltas = np.array([ncdf(d1d2(self.forward, k, self.T, v)[0]) for k, v in zip(self.strikes, self.ivs, strict=True)])
        order = np.argsort(deltas)
        d_sorted, v_sorted = deltas[order], self.ivs[order]
        keep = np.concatenate(([True], np.diff(d_sorted) > 1e-12))
        d_sorted, v_sorted = d_sorted[keep], v_sorted[keep]
        if len(d_sorted) < 2 or not (d_sorted[0] <= target <= d_sorted[-1]):
            return None
        return float(np.interp(target, d_sorted, v_sorted))


def build_smile(rows: list[OptionRow]) -> Smile | None:
    """Smile for rows of ONE expiry. Needs >= 3 strikes with an IV."""
    usable = [r for r in rows if r.iv]
    if not usable:
        return None
    F, T = usable[0].forward, usable[0].T
    if T <= 0:
        return None
    by_strike: dict[float, dict[bool, OptionRow]] = {}
    for r in usable:
        by_strike.setdefault(r.strike, {})[r.is_call] = r
    strikes: list[float] = []
    ivs: list[float] = []
    for k, sides in by_strike.items():
        pick = sides.get(k >= F) or next(iter(sides.values()))  # OTM side, else whatever exists
        strikes.append(k)
        ivs.append(pick.iv)  # type: ignore[arg-type]
    if len(strikes) < 3:
        return None
    return Smile(F, T, strikes, ivs)


def atm_iv(smile: Smile) -> float:
    return smile.iv(smile.forward)


def rr_fly(smile: Smile, delta: float = 0.25) -> dict[str, float | None]:
    ivc = smile.iv_at_call_delta(delta)
    ivp = smile.iv_at_call_delta(1.0 - delta)
    atm = atm_iv(smile)
    if ivc is None or ivp is None:
        return {"iv_call": ivc, "iv_put": ivp, "rr": None, "fly": None, "atm": atm}
    return {"iv_call": ivc, "iv_put": ivp, "rr": ivc - ivp, "fly": (ivc + ivp) / 2.0 - atm, "atm": atm}


def put_call_ratios(rows: Iterable[OptionRow]) -> dict[str, float | None]:
    oi_c = sum(r.open_interest for r in rows if r.is_call)
    oi_p = sum(r.open_interest for r in rows if not r.is_call)
    v_c = sum(r.volume for r in rows if r.is_call)
    v_p = sum(r.volume for r in rows if not r.is_call)
    return {
        "oi_calls": oi_c,
        "oi_puts": oi_p,
        "pc_oi": (oi_p / oi_c) if oi_c else None,
        "pc_volume": (v_p / v_c) if v_c else None,
    }


def max_pain(rows: list[OptionRow]) -> dict[str, Any] | None:
    """Settlement price that minimises total payout to option holders (USD)."""
    rows = [r for r in rows if r.open_interest > 0]
    if not rows:
        return None
    strikes = sorted({r.strike for r in rows})
    calls = [(r.strike, r.open_interest) for r in rows if r.is_call]
    puts = [(r.strike, r.open_interest) for r in rows if not r.is_call]
    curve = []
    for s in strikes:
        pay = sum(oi * max(s - k, 0.0) for k, oi in calls) + sum(oi * max(k - s, 0.0) for k, oi in puts)
        curve.append({"settle": s, "payout_usd": pay})
    best = min(curve, key=lambda c: c["payout_usd"])
    return {"max_pain": best["settle"], "payout_usd": best["payout_usd"], "curve": curve}


def net_gex(rows: Iterable[OptionRow], shift: float = 1.0) -> float:
    """Net gamma exposure in USD per 1 % spot move, under the common (heuristic!) assumption that
    dealers are long calls and short puts. A positive value implies dampening hedging flow."""
    total = 0.0
    for r in rows:
        if not r.iv or r.T <= 0 or r.open_interest <= 0:
            continue
        F = r.forward * shift
        g = black76_greeks(F, r.strike, r.T, r.iv, r.is_call)["gamma"]
        total += (1.0 if r.is_call else -1.0) * g * r.open_interest * F * F * 0.01
    return total


def gex_by_strike(rows: Iterable[OptionRow]) -> dict[float, float]:
    out: dict[float, float] = {}
    for r in rows:
        if not r.iv or r.T <= 0 or r.open_interest <= 0:
            continue
        g = black76_greeks(r.forward, r.strike, r.T, r.iv, r.is_call)["gamma"]
        out[r.strike] = out.get(r.strike, 0.0) + (1.0 if r.is_call else -1.0) * g * r.open_interest * r.forward**2 * 0.01
    return dict(sorted(out.items()))


def gex_profile(rows: list[OptionRow], span: float = 0.2, step: float = 0.01) -> list[tuple[float, float]]:
    """Net GEX as the spot is shifted by factors in [1-span, 1+span]."""
    n = int(round(span / step))
    shifts = [1.0 + i * step for i in range(-n, n + 1)]
    return [(s, net_gex(rows, s)) for s in shifts]


def atm_straddle(rows: list[OptionRow]) -> dict[str, Any] | None:
    """ATM straddle (strike nearest the forward) for one expiry. Returns None if the call or put mark is missing.

    `straddle_usd` is the market price of 1 call + 1 put at the ATM strike, in USD per unit of underlying.
    Under a risk-neutral lognormal with r=0, this equals `F * sigma * sqrt(T) * sqrt(2/pi)` and is also a direct
    estimate of E[|S_T - K|] (expected absolute move). `breakeven_lo/hi = F -+ straddle_usd` are the straddle's
    P/L breakevens, i.e. the move magnitude required to profit buying the straddle.
    """
    rows = [r for r in rows if r.T > 0]
    if not rows:
        return None
    F = rows[0].forward
    T = rows[0].T
    strikes = sorted({r.strike for r in rows})
    k_atm = min(strikes, key=lambda k: abs(math.log(k / F)))
    legs = {r.is_call: r for r in rows if r.strike == k_atm}
    call, put = legs.get(True), legs.get(False)
    if call is None or put is None or call.mark_price_usd is None or put.mark_price_usd is None:
        return None
    straddle = float(call.mark_price_usd + put.mark_price_usd)
    atm = (call.iv + put.iv) / 2.0 if (call.iv and put.iv) else (call.iv or put.iv)
    sigma_move_pct = atm * math.sqrt(T) if atm else None
    return {
        "forward": F,
        "atm_strike": k_atm,
        "T": T,
        "atm_iv": atm,
        "call_mark_usd": float(call.mark_price_usd),
        "put_mark_usd": float(put.mark_price_usd),
        "straddle_usd": straddle,
        "straddle_pct_of_forward": straddle / F,
        "breakeven_lo": F - straddle,
        "breakeven_hi": F + straddle,
        "sigma_range": (F * math.exp(-sigma_move_pct), F * math.exp(sigma_move_pct)) if sigma_move_pct else None,
        "sigma_move_pct": sigma_move_pct,
    }


def oi_by_strike(rows: Iterable[OptionRow]) -> list[dict[str, Any]]:
    """Per-strike OI aggregated across the given rows (call vs put, totals, p/c ratio, USD notional at the forward)."""
    agg: dict[float, dict[str, float]] = {}
    for r in rows:
        if r.open_interest <= 0:
            continue
        slot = agg.setdefault(r.strike, {"call_oi": 0.0, "put_oi": 0.0, "forward": r.forward})
        if r.is_call:
            slot["call_oi"] += r.open_interest
        else:
            slot["put_oi"] += r.open_interest
    out: list[dict[str, Any]] = []
    for k, v in sorted(agg.items()):
        total = v["call_oi"] + v["put_oi"]
        out.append(
            {
                "strike": k,
                "call_oi": v["call_oi"],
                "put_oi": v["put_oi"],
                "total_oi": total,
                "notional_usd": total * v["forward"],
                "pc_ratio": (v["put_oi"] / v["call_oi"]) if v["call_oi"] > 0 else None,
                "net_oi": v["call_oi"] - v["put_oi"],
            }
        )
    return out


def gex_flip_shift(profile: list[tuple[float, float]]) -> float | None:
    """Spot shift factor (relative to now) where net GEX crosses zero, nearest to 1.0."""
    best: float | None = None
    for (s0, g0), (s1, g1) in zip(profile, profile[1:], strict=False):
        if g0 == 0:
            cand = s0
        elif g0 * g1 < 0:
            cand = s0 + (s1 - s0) * (0 - g0) / (g1 - g0)
        else:
            continue
        if best is None or abs(cand - 1.0) < abs(best - 1.0):
            best = cand
    return best
