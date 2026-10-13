"""Volatility risk premium: implied vs subsequently realized volatility, and a delta-hedged short straddle.

Units: vols are decimals (0.45 = 45 %), annualised over 365 days (crypto trades every day).
Realized vol is zero-mean ``sqrt(365 * mean(r^2))`` over daily log returns, the variance-swap
convention. Option maths is Black-76 with r = 0 and the forward taken as spot.
"""

from __future__ import annotations

import bisect
import math
from typing import Any

from .carry import pearson
from .options import black76_greeks, black76_price

DAY_MS = 86_400_000


def realized_vol(closes: list[float]) -> float | None:
    """Annualised zero-mean realized vol from consecutive daily closes."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0]
    if not rets:
        return None
    return math.sqrt(365.0 * sum(x * x for x in rets) / len(rets))


def align_daily(iv: list[tuple[int, float]], prices: list[tuple[int, float]]) -> list[dict[str, float]]:
    """Join daily IV and close series on the same UTC day (ts floored to the day)."""
    px = {ts // DAY_MS: c for ts, c in prices}
    out = []
    for ts, v in sorted(iv):
        d = ts // DAY_MS
        if d in px and v > 0:
            out.append({"day": d, "iv": v, "close": px[d]})
    return out


def vrp_records(rows: list[dict[str, float]], *, horizon_days: int = 30, trailing_days: int = 30) -> list[dict[str, float]]:
    """Per day t: IV, trailing and forward realized vol, VRP (vol and variance) and the IV-minus-trailing spread.

    Requires consecutive days; gaps break the window and the day is skipped.
    """
    days = [int(r["day"]) for r in rows]
    closes = [r["close"] for r in rows]
    out = []
    for i, r in enumerate(rows):
        lo, hi = i - trailing_days, i + horizon_days
        if lo < 0 or hi >= len(rows):
            continue
        if days[hi] - days[lo] != hi - lo:
            continue
        rv_past = realized_vol(closes[lo : i + 1])
        rv_fwd = realized_vol(closes[i : hi + 1])
        if rv_past is None or rv_fwd is None:
            continue
        out.append(
            {
                "day": days[i],
                "iv": r["iv"],
                "rv_trailing": rv_past,
                "rv_forward": rv_fwd,
                "vrp_vol": r["iv"] - rv_fwd,
                "vrp_var": r["iv"] ** 2 - rv_fwd**2,
                "spread": r["iv"] - rv_past,
            }
        )
    return out


def short_straddle_hedged(
    rows: list[dict[str, float]],
    start: int,
    *,
    days: int = 30,
    entry_cost_vol: float = 0.01,
    option_fee: float = 0.0003,
    hedge_fee: float = 0.0005,
    hedge: bool = True,
) -> dict[str, float] | None:
    """Sell one ATM straddle at ``rows[start]`` close and IV, hedge delta at each daily close, settle at expiry.

    Premium is priced at ``iv - entry_cost_vol`` (half-spread paid on entry); ``option_fee`` is charged per
    option as a fraction of spot; ``hedge_fee`` on the traded hedge notional. The hedge ratio uses the
    day's IV (``rows[d]["iv"]``) and remaining time. Returns P&L as a fraction of entry spot.
    """
    end = start + days
    if end >= len(rows) or int(rows[end]["day"]) - int(rows[start]["day"]) != days:
        return None
    s0, iv0 = rows[start]["close"], rows[start]["iv"]
    k = s0
    t0 = days / 365.0
    sell_vol = max(iv0 - entry_cost_vol, 1e-4)
    premium = black76_price(s0, k, t0, sell_vol, True) + black76_price(s0, k, t0, sell_vol, False)
    pnl = premium - 2 * option_fee * s0
    position = 0.0
    hedge_pnl = 0.0
    costs = 0.0
    for d in range(start, end):
        s = rows[d]["close"]
        if hedge:
            t = (end - d) / 365.0
            g_c = black76_greeks(s, k, t, rows[d]["iv"], True)
            g_p = black76_greeks(s, k, t, rows[d]["iv"], False)
            target = g_c["delta"] + g_p["delta"]  # short straddle delta is -target, so hold +target underlying
            costs += hedge_fee * abs(target - position) * s
            position = target
        hedge_pnl += position * (rows[d + 1]["close"] - s)
    s_t = rows[end]["close"]
    if hedge:
        costs += hedge_fee * abs(position) * s_t
    payoff = abs(s_t - k)
    total = pnl - payoff + hedge_pnl - costs
    return {
        "day": rows[start]["day"],
        "iv": iv0,
        "premium": premium / s0,
        "payoff": payoff / s0,
        "hedge_pnl": hedge_pnl / s0,
        "costs": costs / s0 + 2 * option_fee,
        "pnl": total / s0,
        "move": s_t / s0 - 1.0,
    }


def stats(values: list[float], *, periods_per_year: float | None = None) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("nan")
    srt = sorted(values)
    out: dict[str, Any] = {
        "n": n,
        "mean": mean,
        "median": srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2,
        "sd": sd,
        "ci95": (mean - 1.96 * se, mean + 1.96 * se) if n > 1 else None,
        "share_positive": sum(1 for v in values if v > 0) / n,
        "min": srt[0],
        "max": srt[-1],
    }
    if periods_per_year and sd > 0:
        out["sharpe"] = mean / sd * math.sqrt(periods_per_year)
    cum, peak, dd = 0.0, 0.0, 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    out["max_drawdown"] = dd
    return out


def percentile_rank(values: list[float], x: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return (bisect.bisect_left(s, x) + bisect.bisect_right(s, x)) / 2 / len(s)


LEVEL_BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


def trailing_percentiles(rows: list[dict[str, float]], *, window: int = 365) -> dict[int, float]:
    """IV percentile of each day against the previous ``window`` days only (no lookahead)."""
    out: dict[int, float] = {}
    ivs = [r["iv"] for r in rows]
    for i in range(window, len(rows)):
        past = sorted(ivs[i - window : i])
        out[int(rows[i]["day"])] = bisect.bisect_left(past, ivs[i]) / window
    return out


def vrp_summary(
    rows: list[dict[str, float]],
    *,
    horizon_days: int = 30,
    entry_cost_vol: float = 0.02,
    option_fee: float = 0.0003,
    hedge_fee: float = 0.0005,
) -> dict[str, Any]:
    """Current IV/RV snapshot plus the history's realized premium and hedged short-straddle record."""
    if not rows:
        return {}
    closes = [r["close"] for r in rows]
    latest = rows[-1]
    rv7 = realized_vol(closes[-8:]) if len(closes) >= 8 else None
    rv30 = realized_vol(closes[-31:]) if len(closes) >= 31 else None
    ivs = [r["iv"] for r in rows]
    out: dict[str, Any] = {
        "current": {
            "day": latest["day"],
            "iv": latest["iv"],
            "rv_7d": rv7,
            "rv_30d": rv30,
            "spread_vs_rv30": latest["iv"] - rv30 if rv30 is not None else None,
            "iv_percentile": percentile_rank(ivs, latest["iv"]),
            "iv_min": min(ivs),
            "iv_max": max(ivs),
            "iv_mean": sum(ivs) / len(ivs),
        },
        "history_days": len(rows),
    }
    recs = vrp_records(rows, horizon_days=horizon_days, trailing_days=30)
    if recs:
        non_overlap = recs[::horizon_days]
        out["premium"] = {
            "mean_vrp_vol": sum(r["vrp_vol"] for r in recs) / len(recs),
            "share_iv_above_realized": sum(1 for r in recs if r["vrp_vol"] > 0) / len(recs),
            "mean_iv": sum(r["iv"] for r in recs) / len(recs),
            "mean_rv_forward": sum(r["rv_forward"] for r in recs) / len(recs),
            "non_overlapping": stats([r["vrp_vol"] for r in non_overlap]),
            "spread_correlation": pearson([(r["spread"], r["vrp_vol"]) for r in recs]),
        }
    kw = {"days": horizon_days, "entry_cost_vol": entry_cost_vol, "option_fee": option_fee, "hedge_fee": hedge_fee}
    trades = [t for i in range(len(rows)) if (t := short_straddle_hedged(rows, i, **kw)) is not None]
    pct = trailing_percentiles(rows, window=365)
    if pct:
        out["current"]["iv_percentile_365d"] = pct.get(int(latest["day"]))
        by_day = {int(t["day"]): t["pnl"] for t in trades}
        levels = []
        for lo, hi in LEVEL_BUCKETS:
            pnl = [v for d, v in by_day.items() if d in pct and lo <= pct[d] < hi]
            levels.append(
                {
                    "percentile_from": lo,
                    "percentile_to": min(hi, 1.0),
                    "n_starts": len(pnl),
                    "mean_pnl": sum(pnl) / len(pnl) if pnl else None,
                    "share_positive": sum(1 for v in pnl if v > 0) / len(pnl) if pnl else None,
                }
            )
        out["by_iv_level"] = levels
    if trades:
        last_day = trades[-1]["day"]
        recent = [t["pnl"] for t in trades if t["day"] > last_day - 365]
        out["hedged_short_straddle"] = {
            "all_starts": stats([t["pnl"] for t in trades]),
            "non_overlapping": stats([t["pnl"] for t in trades[::horizon_days]], periods_per_year=365 / horizon_days),
            "last_365d_mean": sum(recent) / len(recent) if recent else None,
            "mean_premium": sum(t["premium"] for t in trades) / len(trades),
            "worst": min(trades, key=lambda t: t["pnl"]),
        }
    return out
