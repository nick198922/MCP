"""Event-day vs ordinary-day short daily ATM straddle on Deribit BTC (IDEA-20261014-event-day-straddle-premium).

Pure functions, no I/O; time is always a parameter. Conventions (see the card's pre-registration):
  * entry at ``t0 = day + entry_hour`` (09:00 UTC) on the daily expiry ``E = day + 1 day + 08:00 UTC``;
    only prints with timestamp in ``[t0 - window_h, t0]`` are read (no look-ahead);
  * Deribit options are inverse: ``price_btc = Black76(F, K, T, sigma) / F``; trade ``iv`` arrives in
    percent and is divided by 100; vols are decimals (0.45 = 45 %);
  * T = (expiry - observation time) / 365 days; r = 0 on the expiry forward;
  * results are fractions of entry spot S0 per 1 BTC notional (0.01 = 1 % of spot), USD-linear unless
    the name says ``inverse``.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from .instruments import parse_option_name
from .put_skew import implied_forward, implied_vol_inverse, inverse_price, year_frac

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def expiry_code(expiry_ms: int) -> str:
    """Deribit instrument prefix of an expiry, e.g. ``BTC-14JAN21-``."""
    d = datetime.fromtimestamp(expiry_ms / 1000, UTC)
    return f"BTC-{d.day}{MONTHS[d.month - 1]}{d:%y}-"


def _leg_rows(trades: Iterable[dict[str, Any]], expiry_ms: int, lo_ms: int, hi_ms: int) -> list[dict[str, Any]]:
    """Prints of one expiry with timestamp in [lo_ms, hi_ms], time-sorted, with parsed strike/side."""
    code = expiry_code(expiry_ms)
    out = []
    for t in trades:
        name = str(t.get("instrument_name") or "")
        ts = t.get("timestamp")
        if not name.startswith(code) or ts is None or not lo_ms <= int(ts) <= hi_ms:
            continue
        on = parse_option_name(name)
        if on is None or int(on.expiry.timestamp() * 1000) != expiry_ms or not t.get("index_price"):
            continue
        out.append({"K": on.strike, "call": on.is_call, "ts": int(ts), "t": t})
    out.sort(key=lambda r: r["ts"])
    return out


def entry_quote(
    trades: Sequence[dict[str, Any]],
    day_ms: int,
    *,
    entry_hour: float = 9.0,
    window_h: float = 2.0,
    spot_window_min: float = 15.0,
    max_prints: int = 5,
    max_strike_dist: float = 0.015,
    moneyness_band: float = 0.15,
    fee: float = 0.0003,
    fee_cap: float = 0.125,
    expiry_days: int = 1,
) -> dict[str, Any] | None:
    """Short ATM straddle entry quote at ``t0`` on the daily expiry ``day + expiry_days`` 08:00 UTC, from prints <= t0 only.

    Returns None if no print at all is usable for the spot; otherwise a dict with ``covered`` (bool) and,
    when covered: spot ``S0``, forward ``F``, ``T``, strike ``K``, per-leg mark IV (``iv_call``, ``iv_put``)
    and trade IV (``iv_call_trade``, ``iv_put_trade``), USD premium at mark IV (``premium``) and at trade
    IV (``premium_trade``), ``premium_btc``, empirical half-spreads (USD, sum of legs, None if a leg has no
    taker print of that side) for selling (``hs_sell``) and buying (``hs_buy``), exchange fees (USD), the
    ATM mark IV of the following daily expiry (``iv_next``) and volumes (BTC) for capacity.
    """
    t0 = day_ms + int(entry_hour * HOUR_MS)
    lo = t0 - int(window_h * HOUR_MS)
    exp = day_ms + expiry_days * DAY_MS + 8 * HOUR_MS
    # spot: median index of all BTC option prints in the last spot_window_min before t0, else latest print <= t0
    recent, last = [], None
    for t in trades:
        ts, idx = t.get("timestamp"), t.get("index_price")
        if ts is None or not idx or int(ts) > t0 or not str(t.get("instrument_name") or "").startswith("BTC-"):
            continue
        if t0 - spot_window_min * 60_000 <= int(ts):
            recent.append(float(idx))
        if last is None or int(ts) > last[0]:
            last = (int(ts), float(idx))
    if last is None:
        return None
    s0 = statistics.median(recent) if recent else last[1]
    rows = _leg_rows(trades, exp, lo, t0)
    T0 = year_frac(exp, t0)
    base: dict[str, Any] = {"day": day_ms, "t0": t0, "expiry": exp, "spot": s0, "T": T0, "n_prints": len(rows)}
    base["volume"] = sum(float(r["t"].get("amount") or 0.0) for r in rows)
    if not rows:
        return dict(base, covered=False, reason="no_prints")
    ratios = []
    for r in rows:
        t, idx = r["t"], float(r["t"]["index_price"])
        if t.get("price") and t.get("iv") and abs(math.log(r["K"] / idx)) <= moneyness_band:
            f = implied_forward(float(t["price"]), r["K"], year_frac(exp, r["ts"]), float(t["iv"]) / 100.0, r["call"], idx)
            if f is not None:
                ratios.append(f / idx)
    ratio = statistics.median(ratios) if ratios else 1.0
    F0 = ratio * s0
    base.update(ratio=ratio, F=F0)
    both = {r["K"] for r in rows if r["call"]} & {r["K"] for r in rows if not r["call"]}
    if not both:
        return dict(base, covered=False, reason="no_strike_with_both_legs")
    K = min(both, key=lambda k: (abs(k - F0), k))
    if abs(K / F0 - 1.0) > max_strike_dist:
        return dict(base, K=K, covered=False, reason="strike_too_far")
    legs: dict[bool, dict[str, Any]] = {}
    for is_call in (True, False):
        lr = [r for r in rows if r["K"] == K and r["call"] == is_call]
        marks, tivs = [], []
        for r in lr[-max_prints:]:
            t, idx = r["t"], float(r["t"]["index_price"])
            T = year_frac(exp, r["ts"])
            if t.get("mark_price"):
                v = implied_vol_inverse(float(t["mark_price"]), ratio * idx, K, T, is_call)
                if v is not None:
                    marks.append(v)
            if t.get("iv"):
                tivs.append(float(t["iv"]) / 100.0)
        if not marks:
            return dict(base, K=K, covered=False, reason="no_mark_iv")
        iv = statistics.median(marks)
        tiv = statistics.median(tivs) if tivs else iv
        px = inverse_price(F0, K, T0, iv, is_call)
        sells = [(float(r["t"]["mark_price"]) - float(r["t"]["price"])) * s0 for r in lr if _sided(r, "sell")]
        buys = [(float(r["t"]["price"]) - float(r["t"]["mark_price"])) * s0 for r in lr if _sided(r, "buy")]
        legs[is_call] = {
            "iv": iv,
            "iv_trade": tiv,
            "px_btc": px,
            "px_btc_trade": inverse_price(F0, K, T0, tiv, is_call),
            "hs_sell": statistics.median(sells) if sells else None,
            "hs_buy": statistics.median(buys) if buys else None,
            "age_h": (t0 - lr[-1]["ts"]) / HOUR_MS,
            "volume": sum(float(r["t"].get("amount") or 0.0) for r in lr),
        }
    c, p = legs[True], legs[False]
    prem_btc = c["px_btc"] + p["px_btc"]
    fees = sum(min(fee * s0, fee_cap * x["px_btc"] * s0) for x in (c, p))
    return dict(
        base,
        covered=True,
        reason="",
        K=K,
        iv_call=c["iv"],
        iv_put=p["iv"],
        iv_call_trade=c["iv_trade"],
        iv_put_trade=p["iv_trade"],
        premium_btc=prem_btc,
        premium=prem_btc * s0,
        premium_trade=(c["px_btc_trade"] + p["px_btc_trade"]) * s0,
        hs_sell=c["hs_sell"] + p["hs_sell"] if c["hs_sell"] is not None and p["hs_sell"] is not None else None,
        hs_buy=c["hs_buy"] + p["hs_buy"] if c["hs_buy"] is not None and p["hs_buy"] is not None else None,
        fees=fees,
        max_age_h=max(c["age_h"], p["age_h"]),
        volume_K=c["volume"] + p["volume"],
        iv_next=next_expiry_atm_iv(trades, exp + DAY_MS, lo, t0, F0, ratio, max_prints=max_prints),
    )


def _sided(r: dict[str, Any], side: str) -> bool:
    t = r["t"]
    return t.get("direction") == side and bool(t.get("price")) and bool(t.get("mark_price"))


def next_expiry_atm_iv(
    trades: Sequence[dict[str, Any]], expiry_ms: int, lo_ms: int, t0: int, F0: float, ratio: float, *, max_prints: int = 5
) -> float | None:
    """Mark IV at the traded strike nearest F0 of ``expiry_ms`` (both legs pooled, freshest prints in [lo, t0])."""
    rows = _leg_rows(trades, expiry_ms, lo_ms, t0)
    if not rows:
        return None
    K = min({r["K"] for r in rows}, key=lambda k: (abs(k - F0), k))
    if abs(K / F0 - 1.0) > 0.015:
        return None
    marks = []
    for r in [r for r in rows if r["K"] == K][-max_prints:]:
        t = r["t"]
        if t.get("mark_price"):
            v = implied_vol_inverse(float(t["mark_price"]), ratio * float(t["index_price"]), K, year_frac(expiry_ms, r["ts"]), r["call"])
            if v is not None:
                marks.append(v)
    return statistics.median(marks) if marks else None


def straddle_x(q: dict[str, Any], s_t: float, *, premium_key: str = "premium") -> float:
    """Short straddle result before costs, USD-linear: (premium - |S_T - K|) / S0."""
    return (q[premium_key] - abs(s_t - q["K"])) / q["spot"]


def straddle_x_inverse(q: dict[str, Any], s_t: float) -> float:
    """Short straddle result in BTC per 1 BTC notional (inverse accounting): premium_btc - |S_T - K| / S_T."""
    return q["premium_btc"] - abs(s_t - q["K"]) / s_t


def delivery_fee(K: float, s_t: float, *, fee: float = 0.00015, cap: float = 0.125) -> float:
    """Deribit delivery fee (USD) on the in-the-money leg of a straddle: min(fee * S_T, cap * intrinsic)."""
    intrinsic = abs(s_t - K)
    return min(fee * s_t, cap * intrinsic) if intrinsic > 0 else 0.0


def cost_frac(q: dict[str, Any], s_t: float, *, model: str, direction: int, fallback_hs: float | None = None) -> float:
    """Round-trip cost as a fraction of S0. ``model``: 'base' (0.15 %), 'stress' (0.30 %), 'empirical', 'zero'.

    Delivery fee is added for every model except 'zero'. 'empirical' = side half-spreads (sell side for a short,
    ``direction`` = +1; buy side for a long, -1; ``fallback_hs`` USD if the day has none) + exchange fees.
    """
    if model == "zero":
        return 0.0
    dfee = delivery_fee(q["K"], s_t) / q["spot"]
    if model == "base":
        return 0.0015 + dfee
    if model == "stress":
        return 0.0030 + dfee
    if model == "empirical":
        hs = q.get("hs_sell" if direction > 0 else "hs_buy")
        if hs is None:
            hs = fallback_hs if fallback_hs is not None else 0.00075 * q["spot"]
        return (hs + q["fees"]) / q["spot"] + dfee
    raise ValueError(f"unknown cost model {model!r}")


def mae_short(path: Sequence[tuple[int, float]], q: dict[str, Any]) -> float | None:
    """Worst intrinsic-only loss of the short straddle over index prints in (t0, expiry], fraction of S0.

    A lower bound of the mark-to-market loss (time value ignored). None without path data.
    """
    pts = [s for ts, s in path if q["t0"] < ts <= q["expiry"]]
    if not pts:
        return None
    worst = max(abs(s - q["K"]) for s in (min(pts), max(pts)))
    return (worst - q["premium"]) / q["spot"]


# ----------------------------------------------------------------------------- events and controls


def day_of(ms: int) -> int:
    return (int(ms) // DAY_MS) * DAY_MS


def group_events(events: Iterable[tuple[int, str]]) -> dict[int, str]:
    """day_ms -> kind; several events on one UTC date become one record of kind 'multi'."""
    by: dict[int, set[str]] = {}
    for ms, kind in events:
        by.setdefault(day_of(ms), set()).add(kind)
    return {d: (next(iter(k)) if len(k) == 1 else "multi") for d, k in by.items()}


def blocked_days(dates: Iterable[int], *, pad_days: int = 1) -> set[int]:
    """All days within ``pad_days`` of any given date (controls may not fall there)."""
    out: set[int] = set()
    for ms in dates:
        d = day_of(ms)
        for k in range(-pad_days, pad_days + 1):
            out.add(d + k * DAY_MS)
    return out


def pick_controls(day_ms: int, valid: set[int], blocked: set[int], *, n: int = 4, max_weeks: int = 8) -> list[int]:
    """Up to ``n`` same-weekday control days nearest to ``day_ms`` (|k| weeks, ties: earlier) that are valid and not blocked."""
    out = []
    for k in range(1, max_weeks + 1):
        for c in (day_ms - k * 7 * DAY_MS, day_ms + k * 7 * DAY_MS):
            if c in valid and c not in blocked:
                out.append(c)
                if len(out) == n:
                    return out
    return out


# ----------------------------------------------------------------------------- statistics


def iso_week(day_ms: int) -> str:
    y, w, _ = datetime.fromtimestamp(day_ms / 1000, UTC).isocalendar()
    return f"{y}-W{w:02d}"


def cluster_bootstrap(rows: Sequence[tuple[str, float]], *, n_boot: int = 10_000, seed: int = 20261015) -> dict[str, Any]:
    """Mean of values with a cluster (e.g. ISO-week) bootstrap: percentile 95 % CI and the bootstrap SE."""
    vals = [v for _, v in rows]
    if len(vals) < 2:
        return {"n": len(vals), "mean": statistics.mean(vals) if vals else None, "ci95": None, "se": None, "clusters": 0}
    groups: dict[str, list[float]] = {}
    for c, v in rows:
        groups.setdefault(c, []).append(v)
    keys = sorted(groups)
    sums = [(sum(groups[k]), len(groups[k])) for k in keys]
    rng = random.Random(seed)
    means = []
    m = len(keys)
    for _ in range(n_boot):
        s = c = 0.0
        for _ in range(m):
            a, b = sums[rng.randrange(m)]
            s += a
            c += b
        means.append(s / c)
    means.sort()
    return {
        "n": len(vals),
        "mean": statistics.mean(vals),
        "ci95": (means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]),
        "se": statistics.stdev(means),
        "clusters": m,
    }


def power_gate(dx: Sequence[float], *, mde_max: float = 0.004, n_min: int = 120, z: float = 2.8) -> dict[str, Any]:
    """Pre-registered power gate: MDE = z * SD / sqrt(n) (z = 2.8: 5 % two-sided, 80 % power); pass if MDE <= mde_max and n >= n_min."""
    n = len(dx)
    sd = statistics.stdev(dx) if n > 1 else float("nan")
    mde = z * sd / math.sqrt(n) if n > 1 else float("inf")
    return {"n": n, "sd": sd, "mde": mde, "passed": n >= n_min and mde <= mde_max}


def drop_top(rows: Sequence[tuple[str, float]], k: int = 3, *, direction: int | None = None) -> list[tuple[str, float]]:
    """Rows without the ``k`` largest contributors: largest ``direction * value`` if given, else largest |value|."""
    key = (lambda r: direction * r[1]) if direction else (lambda r: abs(r[1]))
    drop = set(sorted(range(len(rows)), key=lambda i: key(rows[i]), reverse=True)[:k])
    return [r for i, r in enumerate(rows) if i not in drop]
