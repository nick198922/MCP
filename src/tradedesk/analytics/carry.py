"""Funding carry: cash-and-carry simulation and funding persistence.

Units follow the project convention: funding rates, APRs, fees and returns are decimal
fractions of entry notional (0.10 = 10 %). Carry income is the sum of actual payments
over real time, so venues that change the funding interval (8h -> 4h -> 1h) are handled.

Cash-and-carry = long spot + short perp, equal quantity. The short perp receives
``rate x perp_price`` per unit each funding event (pays when the rate is negative).
Price P&L of the hedge is ``(spot_x - spot_e) - (perp_x - perp_e)``, i.e. minus the change
in the perp premium. Decisions are taken at funding timestamps using only events up to and
including that timestamp; a position opened at event k first earns funding at event k+1.
"""

from __future__ import annotations

import bisect
import math
from typing import Any

from .derivatives import detect_interval_hours

DAY_MS = 86_400_000
YEAR_MS = 365 * DAY_MS


def _prep(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int], list[float]]:
    ev = sorted((e for e in events if e.get("rate") is not None), key=lambda e: int(e["ts_ms"]))
    ts = [int(e["ts_ms"]) for e in ev]
    prefix = [0.0]
    for e in ev:
        prefix.append(prefix[-1] + float(e["rate"]))
    return ev, ts, prefix


def _window_sum(ts: list[int], prefix: list[float], k: int, lookback_ms: int) -> float:
    """Sum of rates for events in (ts[k] - lookback, ts[k]]."""
    lo = bisect.bisect_right(ts, ts[k] - lookback_ms)
    return prefix[k + 1] - prefix[lo]


def trailing_apr(events: list[dict[str, Any]], k: int, lookback_days: float) -> float:
    ev, ts, prefix = _prep(events)
    return _window_sum(ts, prefix, k, int(lookback_days * DAY_MS)) * 365.0 / lookback_days


def simulate_cash_and_carry(
    events: list[dict[str, Any]],
    *,
    rule: str = "threshold",
    lookback_days: float = 7.0,
    entry_apr: float = 0.10,
    exit_apr: float = 0.0,
    spot_fee: float = 0.001,
    perp_fee: float = 0.0005,
    warmup_days: float | None = None,
) -> dict[str, Any]:
    """Walk funding events (``ts_ms``, ``rate``, ``spot``, ``perp``) under an entry/exit rule.

    ``rule="always"`` opens at the first decision point after warm-up and holds to the end.
    ``rule="threshold"`` opens when trailing APR >= ``entry_apr`` and closes when it drops to
    ``exit_apr`` or below. Fees are charged per side on both legs. An open position is marked
    and closed at the last event (``open_at_end``).
    """
    ev, ts, prefix = _prep(events)
    if len(ev) < 2:
        return {"n_events": len(ev), "trades": [], "stats": {}}
    lookback_ms = int(lookback_days * DAY_MS)
    start_ts = ts[0] + int((lookback_days if warmup_days is None else warmup_days) * DAY_MS)
    side_fee = spot_fee + perp_fee
    realized = 0.0
    pos: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    equity: list[tuple[int, float]] = []

    def close(k: int, at_end: bool) -> None:
        nonlocal realized, pos
        assert pos is not None
        e = ev[k]
        basis = (float(e["spot"]) - pos["spot_e"] - (float(e["perp"]) - pos["perp_e"])) / pos["spot_e"]
        fees = pos["fees"] + side_fee * float(e["spot"]) / pos["spot_e"]
        pnl = pos["funding"] + basis - fees
        realized += pnl
        trades.append(
            {
                "open_ts": pos["open_ts"],
                "close_ts": ts[k],
                "days": (ts[k] - pos["open_ts"]) / DAY_MS,
                "funding": pos["funding"],
                "basis": basis,
                "fees": fees,
                "pnl": pnl,
                "open_at_end": at_end,
            }
        )
        pos = None

    for k, e in enumerate(ev):
        if pos is not None:
            pos["funding"] += float(e["rate"]) * float(e["perp"]) / pos["spot_e"]
        if ts[k] >= start_ts and k < len(ev) - 1:
            apr = _window_sum(ts, prefix, k, lookback_ms) * 365.0 / lookback_days
            if rule == "always":
                want = True
            elif rule == "threshold":
                want = apr >= entry_apr if pos is None else apr > exit_apr
            else:
                raise ValueError(f"unknown rule {rule!r}")
            if pos is None and want:
                pos = {"open_ts": ts[k], "spot_e": float(e["spot"]), "perp_e": float(e["perp"]), "funding": 0.0, "fees": side_fee}
            elif pos is not None and not want:
                close(k, at_end=False)
        mtm = realized
        if pos is not None:
            basis_now = (float(e["spot"]) - pos["spot_e"] - (float(e["perp"]) - pos["perp_e"])) / pos["spot_e"]
            mtm += pos["funding"] + basis_now - pos["fees"]
        equity.append((ts[k], mtm))
    if pos is not None:
        close(len(ev) - 1, at_end=True)
        equity[-1] = (ts[-1], realized)

    span_ms = max(ts[-1] - start_ts, 1)
    years = span_ms / YEAR_MS
    in_market_ms = sum(t["close_ts"] - t["open_ts"] for t in trades)
    peak, max_dd = float("-inf"), 0.0
    for _, v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    return {
        "n_events": len(ev),
        "trades": trades,
        "equity": equity,
        "stats": {
            "years": years,
            "total_return": realized,
            "apr": realized / years if years > 0 else None,
            "apr_in_market": realized / (in_market_ms / YEAR_MS) if in_market_ms > 0 else None,
            "funding": sum(t["funding"] for t in trades),
            "fees": sum(t["fees"] for t in trades),
            "basis": sum(t["basis"] for t in trades),
            "n_trades": len(trades),
            "time_in_market": in_market_ms / span_ms,
            "max_drawdown": max_dd,
        },
    }


def funding_persistence(
    events: list[dict[str, Any]],
    *,
    lookback_days: float = 7.0,
    horizon_days: float = 7.0,
    step_days: float = 1.0,
) -> list[tuple[float, float]]:
    """Pairs of (trailing APR, next-horizon realized APR), sampled every ``step_days``."""
    ev, ts, prefix = _prep(events)
    if not ev:
        return []
    lb, hz, step = int(lookback_days * DAY_MS), int(horizon_days * DAY_MS), int(step_days * DAY_MS)
    out: list[tuple[float, float]] = []
    next_sample = ts[0] + lb
    for k in range(len(ev)):
        if ts[k] < next_sample:
            continue
        if ts[k] + hz > ts[-1]:
            break
        hi = bisect.bisect_right(ts, ts[k] + hz)
        past = _window_sum(ts, prefix, k, lb) * 365.0 / lookback_days
        future = (prefix[hi] - prefix[k + 1]) * 365.0 / horizon_days
        out.append((past, future))
        next_sample = ts[k] + step
    return out


def pearson(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None


def carry_snapshot(
    entries: list[dict[str, Any]],
    *,
    now_ms: int,
    lookback_days: float = 7.0,
    entry_apr: float = 0.10,
    exit_apr: float = 0.0,
    roundtrip_fee: float = 0.003,
) -> dict[str, Any]:
    """Trailing realized funding APR for a short perp and where it stands vs entry/exit thresholds.

    APRs are sums of actual payments over real time. When history is shorter than the window
    (new listing, venue cap) the APR is scaled over the covered span and ``coverage_days`` says so.
    ``breakeven_days`` is how long the trailing APR needs to persist to pay ``roundtrip_fee``.
    """
    rows = sorted((e for e in entries if e.get("rate") is not None and int(e["ts_ms"]) <= now_ms), key=lambda e: int(e["ts_ms"]))
    if not rows:
        return {"count": 0}
    first = int(rows[0]["ts_ms"])
    interval_days = detect_interval_hours([{"ts_ms": int(e["ts_ms"])} for e in rows]) / 24.0
    coverage_days = (now_ms - first) / DAY_MS + interval_days

    def window(days: float) -> tuple[float | None, list[float]]:
        span = min(days, coverage_days)
        if span <= 0:
            return None, []
        vals = [float(e["rate"]) for e in rows if int(e["ts_ms"]) > now_ms - days * DAY_MS]
        return sum(vals) * 365.0 / span, vals

    apr, vals = window(lookback_days)
    apr_30d, _ = window(30.0)
    if apr is None:
        status = None
    elif apr >= entry_apr:
        status = "above_entry"
    elif apr <= exit_apr:
        status = "below_exit"
    else:
        status = "between"
    return {
        "count": len(rows),
        "coverage_days": coverage_days,
        "trailing_apr": apr,
        "trailing_30d_apr": apr_30d if coverage_days >= 30 else None,
        "positive_share": sum(1 for v in vals if v > 0) / len(vals) if vals else None,
        "status": status,
        "breakeven_days": roundtrip_fee / (apr / 365.0) if apr and apr > 0 else None,
    }


class _Market:
    """Per-symbol lookups for the portfolio walk: funding prefix sums and bar arrays."""

    def __init__(self, data: dict[str, Any], bar_ms: int) -> None:
        f = sorted(data["funding"], key=lambda e: int(e["ts_ms"]))
        self.f_ts = [int(e["ts_ms"]) for e in f]
        self.f_rate = [float(e["rate"]) for e in f]
        self.prefix = [0.0]
        for x in self.f_rate:
            self.prefix.append(self.prefix[-1] + x)
        self.bar_ms = bar_ms
        self.s_close_t = [int(b["ts"]) + bar_ms for b in data["spot"]]
        self.s_close = [float(b["close"]) for b in data["spot"]]
        self.p_close_t = [int(b["ts"]) + bar_ms for b in data["perp"]]
        self.p_close = [float(b["close"]) for b in data["perp"]]
        self.p_high = [float(b["high"]) for b in data["perp"]]

    def apr(self, t: int, lookback_ms: int) -> float | None:
        if not self.f_ts or self.f_ts[0] > t - lookback_ms:
            return None
        hi = bisect.bisect_right(self.f_ts, t)
        lo = bisect.bisect_right(self.f_ts, t - lookback_ms)
        return (self.prefix[hi] - self.prefix[lo]) * YEAR_MS / lookback_ms

    def _at(self, times: list[int], values: list[float], t: int) -> float | None:
        i = bisect.bisect_right(times, t + 60_000) - 1
        if i < 0 or t - times[i] > self.bar_ms:
            return None
        return values[i]

    def spot(self, t: int) -> float | None:
        return self._at(self.s_close_t, self.s_close, t)

    def perp(self, t: int) -> float | None:
        return self._at(self.p_close_t, self.p_close, t)

    def payments(self, after: int, upto: int) -> list[tuple[int, float]]:
        lo, hi = bisect.bisect_right(self.f_ts, after), bisect.bisect_right(self.f_ts, upto)
        return list(zip(self.f_ts[lo:hi], self.f_rate[lo:hi], strict=True))

    def perp_high(self, after: int, upto: int) -> float | None:
        lo, hi = bisect.bisect_right(self.p_close_t, after), bisect.bisect_right(self.p_close_t, upto)
        return max(self.p_high[lo:hi]) if hi > lo else None


def simulate_carry_portfolio(
    markets: dict[str, dict[str, Any]],
    *,
    slots: int = 5,
    lookback_days: float = 7.0,
    entry_apr: float = 0.20,
    exit_apr: float = 0.0,
    spot_fee: float = 0.001,
    perp_fee: float = 0.0005,
    slippage: float = 0.0005,
    step_hours: float = 8.0,
    bar_ms: int = 4 * 3_600_000,
) -> dict[str, Any]:
    """Cash-and-carry across many symbols with ``slots`` equal capital slots.

    ``markets[sym] = {"funding": [{ts_ms, rate}], "spot": [{ts, close}], "perp": [{ts, close, high}]}``,
    bars of length ``bar_ms`` keyed by open time. Every ``step_hours`` (UTC-aligned) the walk
    accrues actual funding payments on open positions, closes those whose trailing APR fell to
    ``exit_apr``, then fills free slots with the highest trailing APR >= ``entry_apr``. Costs per
    side: ``spot_fee + perp_fee + 2 x slippage``. Returns are fractions of total capital (slot
    notional = 1 / slots, no compounding). Per trade it records ``max_runup`` (highest perp high
    vs perp entry, the short leg's liquidation risk) and ``max_premium_rise`` (worst widening of
    perp/spot over entry at bar closes).
    """
    ms = {s: _Market(d, bar_ms) for s, d in markets.items() if d.get("funding") and d.get("spot") and d.get("perp")}
    if not ms:
        return {"trades": [], "equity": [], "stats": {}}
    lookback_ms = int(lookback_days * DAY_MS)
    step = int(step_hours * 3_600_000)
    start = min(m.f_ts[0] for m in ms.values()) + lookback_ms
    # Decisions 5 minutes after each UTC-aligned funding time, so payments stamped a few ms late are settled.
    start = (start // step + 1) * step + 300_000
    end = max(m.f_ts[-1] for m in ms.values())
    side_cost = spot_fee + perp_fee + 2 * slippage
    w = 1.0 / slots
    open_pos: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity: list[tuple[int, float]] = []
    realized = 0.0

    def mark(sym: str, p: dict[str, Any], t: int) -> None:
        m = ms[sym]
        for ts_pay, rate in m.payments(p["last"], t):
            price = m.perp(ts_pay) or p["perp_last"]
            p["funding"] += rate * price / p["spot_e"]
        p["last"] = t
        hi = m.perp_high(p["last_bar"], t)
        if hi is not None:
            p["max_runup"] = max(p["max_runup"], hi / p["perp_e"] - 1.0)
            p["last_bar"] = t
        s, q = m.spot(t), m.perp(t)
        if s and q:
            p["spot_last"], p["perp_last"] = s, q
            p["max_premium_rise"] = max(p["max_premium_rise"], q / s - p["perp_e"] / p["spot_e"])

    def basis(p: dict[str, Any]) -> float:
        return (p["spot_last"] - p["spot_e"] - (p["perp_last"] - p["perp_e"])) / p["spot_e"]

    t = start
    while t <= end:
        for sym in list(open_pos):
            p = open_pos[sym]
            mark(sym, p, t)
            a = ms[sym].apr(t, lookback_ms)
            if a is not None and a <= exit_apr:
                fees = p["fees"] + side_cost * p["spot_last"] / p["spot_e"]
                pnl = p["funding"] + basis(p) - fees
                realized += w * pnl
                trades.append(
                    {
                        **{k: p[k] for k in ("symbol", "open_ts", "entry_apr", "funding", "max_runup", "max_premium_rise")},
                        "close_ts": t,
                        "basis": basis(p),
                        "fees": fees,
                        "pnl": pnl,
                        "open_at_end": False,
                    }
                )
                del open_pos[sym]
        free = slots - len(open_pos)
        if free > 0:
            ranked = []
            for sym, m in ms.items():
                if sym in open_pos:
                    continue
                a = m.apr(t, lookback_ms)
                if a is not None and a >= entry_apr and m.spot(t) and m.perp(t):
                    ranked.append((a, sym))
            for a, sym in sorted(ranked, reverse=True)[:free]:
                m = ms[sym]
                s, q = m.spot(t), m.perp(t)
                open_pos[sym] = {
                    "symbol": sym,
                    "open_ts": t,
                    "entry_apr": a,
                    "spot_e": s,
                    "perp_e": q,
                    "spot_last": s,
                    "perp_last": q,
                    "funding": 0.0,
                    "fees": side_cost,
                    "last": t,
                    "last_bar": t,
                    "max_runup": 0.0,
                    "max_premium_rise": 0.0,
                }
        equity.append((t, realized + sum(w * (p["funding"] + basis(p) - p["fees"]) for p in open_pos.values())))
        t += step
    for p in list(open_pos.values()):
        mark(p["symbol"], p, end)
        fees = p["fees"] + side_cost * p["spot_last"] / p["spot_e"]
        pnl = p["funding"] + basis(p) - fees
        realized += w * pnl
        trades.append(
            {
                **{k: p[k] for k in ("symbol", "open_ts", "entry_apr", "funding", "max_runup", "max_premium_rise")},
                "close_ts": p["last"],
                "basis": basis(p),
                "fees": fees,
                "pnl": pnl,
                "open_at_end": True,
            }
        )
    if equity:
        equity[-1] = (equity[-1][0], realized)
    years = max(end - start, 1) / YEAR_MS
    peak, max_dd = float("-inf"), 0.0
    for _, v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    occupied = sum(tr["close_ts"] - tr["open_ts"] for tr in trades) / (max(end - start, 1) * slots)
    return {
        "trades": trades,
        "equity": equity,
        "stats": {
            "years": years,
            "apr_on_capital": realized / years,
            "total_return": realized,
            "n_trades": len(trades),
            "slot_utilisation": occupied,
            "max_drawdown": max_dd,
            "funding": w * sum(tr["funding"] for tr in trades),
            "basis": w * sum(tr["basis"] for tr in trades),
            "costs": w * sum(tr["fees"] for tr in trades),
        },
    }


class _Leg:
    """One perp on one venue: funding prefix sums and bar lookups (close, high, low)."""

    def __init__(self, data: dict[str, Any], bar_ms: int) -> None:
        f = sorted(data["funding"], key=lambda e: int(e["ts_ms"]))
        self.f_ts = [int(e["ts_ms"]) for e in f]
        self.f_rate = [float(e["rate"]) for e in f]
        self.prefix = [0.0]
        for x in self.f_rate:
            self.prefix.append(self.prefix[-1] + x)
        self.bar_ms = bar_ms
        self.t = [int(b["ts"]) + bar_ms for b in data["bars"]]
        self.close = [float(b["close"]) for b in data["bars"]]
        self.high = [float(b["high"]) for b in data["bars"]]
        self.low = [float(b["low"]) for b in data["bars"]]

    def apr(self, t: int, lookback_ms: int) -> float | None:
        if not self.f_ts or self.f_ts[0] > t - lookback_ms:
            return None
        hi = bisect.bisect_right(self.f_ts, t)
        lo = bisect.bisect_right(self.f_ts, t - lookback_ms)
        return (self.prefix[hi] - self.prefix[lo]) * YEAR_MS / lookback_ms

    def price(self, t: int) -> float | None:
        i = bisect.bisect_right(self.t, t + 60_000) - 1
        if i < 0 or t - self.t[i] > self.bar_ms:
            return None
        return self.close[i]

    def payments(self, after: int, upto: int) -> list[tuple[int, float]]:
        lo, hi = bisect.bisect_right(self.f_ts, after), bisect.bisect_right(self.f_ts, upto)
        return list(zip(self.f_ts[lo:hi], self.f_rate[lo:hi], strict=True))

    def extremes(self, after: int, upto: int) -> tuple[float, float] | None:
        lo, hi = bisect.bisect_right(self.t, after), bisect.bisect_right(self.t, upto)
        return (max(self.high[lo:hi]), min(self.low[lo:hi])) if hi > lo else None


def simulate_perp_spread_portfolio(
    markets: dict[str, dict[str, Any]],
    *,
    slots: int = 5,
    lookback_days: float = 7.0,
    entry_apr: float = 0.10,
    exit_apr: float = 0.0,
    fee_a: float = 0.0005,
    fee_b: float = 0.00055,
    slippage: float = 0.0005,
    max_entry_gap: float | None = None,
    stop_move: float | None = None,
    stop_penalty: float = 0.01,
    step_hours: float = 8.0,
    bar_ms: int = 4 * 3_600_000,
) -> dict[str, Any]:
    """Cross-venue funding carry: short the perp on the venue with higher trailing funding, long the other.

    ``markets[sym] = {"a": {"funding": [{ts_ms, rate}], "bars": [{ts, close, high, low}]}, "b": {...}}``.
    Every ``step_hours`` the walk accrues actual payments on both legs, closes positions whose trailing
    spread in the held direction fell to ``exit_apr``, then fills free slots with the widest trailing
    spreads >= ``entry_apr``. Costs per side: ``fee_a + fee_b + 2 x slippage``. Returns are fractions of
    portfolio notional (slot notional = 1 / slots, priced at leg A's entry, no compounding). With
    ``max_entry_gap`` a pair is not opened while its two perp prices differ by more than that fraction. With
    ``stop_move`` a position is closed at the step where either leg has moved that far against itself (a
    stand-in for liquidating an isolated leg at leverage 1 / stop_move), paying ``stop_penalty`` of notional. Per trade:
    ``max_up`` / ``max_down`` (largest price move against the short / long leg) and ``max_gap`` (largest
    change in the A/B price ratio at bar closes, the cross-venue divergence risk).
    """
    legs = {
        s: (_Leg(d["a"], bar_ms), _Leg(d["b"], bar_ms))
        for s, d in markets.items()
        if d.get("a", {}).get("funding") and d.get("b", {}).get("funding") and d["a"].get("bars") and d["b"].get("bars")
    }
    if not legs:
        return {"trades": [], "equity": [], "stats": {}}
    lookback_ms = int(lookback_days * DAY_MS)
    step = int(step_hours * 3_600_000)
    start = min(max(a.f_ts[0], b.f_ts[0]) for a, b in legs.values()) + lookback_ms
    start = (start // step + 1) * step + 300_000
    end = max(min(a.f_ts[-1], b.f_ts[-1]) for a, b in legs.values())
    side_cost = fee_a + fee_b + 2 * slippage
    w = 1.0 / slots
    open_pos: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity: list[tuple[int, float]] = []
    realized = 0.0

    def mark(sym: str, p: dict[str, Any], t: int) -> None:
        a, b = legs[sym]
        # dir = +1: long A / short B; dir = -1: short A / long B.
        for ts_pay, rate in a.payments(p["last"], t):
            p["funding"] -= p["dir"] * rate * (a.price(ts_pay) or p["a_last"]) / p["a_e"]
        for ts_pay, rate in b.payments(p["last"], t):
            p["funding"] += p["dir"] * rate * (b.price(ts_pay) or p["b_last"]) / p["a_e"]
        p["last"] = t
        for leg, e_key in ((a, "a_e"), (b, "b_e")):
            ex = leg.extremes(p["last_bar"], t)
            if ex:
                p["max_up"] = max(p["max_up"], ex[0] / p[e_key] - 1.0)
                p["max_down"] = max(p["max_down"], 1.0 - ex[1] / p[e_key])
        p["last_bar"] = t
        pa, pb = a.price(t), b.price(t)
        if pa and pb:
            p["a_last"], p["b_last"] = pa, pb
            p["max_gap"] = max(p["max_gap"], abs(pa / pb - p["a_e"] / p["b_e"]))

    def price_pnl(p: dict[str, Any]) -> float:
        return p["dir"] * ((p["a_last"] - p["a_e"]) - (p["b_last"] - p["b_e"])) / p["a_e"]

    def close(sym: str, p: dict[str, Any], t: int, at_end: bool, stopped: bool = False) -> None:
        nonlocal realized
        fees = p["fees"] + side_cost * p["a_last"] / p["a_e"] + (stop_penalty if stopped else 0.0)
        pnl = p["funding"] + price_pnl(p) - fees
        realized += w * pnl
        trades.append(
            {
                **{k: p[k] for k in ("symbol", "open_ts", "dir", "entry_spread", "funding", "max_up", "max_down", "max_gap")},
                "close_ts": t,
                "price_pnl": price_pnl(p),
                "fees": fees,
                "pnl": pnl,
                "open_at_end": at_end,
                "stopped": stopped,
            }
        )

    t = start
    while t <= end:
        for sym in list(open_pos):
            p = open_pos[sym]
            mark(sym, p, t)
            if stop_move is not None and max(p["max_up"], p["max_down"]) >= stop_move:
                close(sym, p, t, at_end=False, stopped=True)
                del open_pos[sym]
                continue
            a, b = legs[sym]
            ra, rb = a.apr(t, lookback_ms), b.apr(t, lookback_ms)
            if ra is not None and rb is not None and p["dir"] * (rb - ra) <= exit_apr:
                close(sym, p, t, at_end=False)
                del open_pos[sym]
        free = slots - len(open_pos)
        if free > 0:
            ranked = []
            for sym, (a, b) in legs.items():
                if sym in open_pos:
                    continue
                ra, rb = a.apr(t, lookback_ms), b.apr(t, lookback_ms)
                if ra is None or rb is None or abs(rb - ra) < entry_apr:
                    continue
                pa, pb = a.price(t), b.price(t)
                if pa and pb and (max_entry_gap is None or abs(pa / pb - 1.0) <= max_entry_gap):
                    ranked.append((abs(rb - ra), sym, 1 if rb > ra else -1))
            for spread, sym, d in sorted(ranked, reverse=True)[:free]:
                a, b = legs[sym]
                pa, pb = a.price(t), b.price(t)
                open_pos[sym] = {
                    "symbol": sym, "open_ts": t, "dir": d, "entry_spread": spread, "a_e": pa, "b_e": pb, "a_last": pa, "b_last": pb,
                    "funding": 0.0, "fees": side_cost, "last": t, "last_bar": t, "max_up": 0.0, "max_down": 0.0, "max_gap": 0.0,
                }  # fmt: skip
        equity.append((t, realized + sum(w * (p["funding"] + price_pnl(p) - p["fees"]) for p in open_pos.values())))
        t += step
    for sym, p in list(open_pos.items()):
        mark(sym, p, end)
        close(sym, p, end, at_end=True)
    if equity:
        equity[-1] = (equity[-1][0], realized)
    years = max(end - start, 1) / YEAR_MS
    peak, max_dd = float("-inf"), 0.0
    for _, v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    return {
        "trades": trades,
        "equity": equity,
        "stats": {
            "years": years,
            "apr_on_notional": realized / years,
            "total_return": realized,
            "n_trades": len(trades),
            "slot_utilisation": sum(tr["close_ts"] - tr["open_ts"] for tr in trades) / (max(end - start, 1) * slots),
            "max_drawdown": max_dd,
            "funding": w * sum(tr["funding"] for tr in trades),
            "price_pnl": w * sum(tr["price_pnl"] for tr in trades),
            "costs": w * sum(tr["fees"] for tr in trades),
            "n_stopped": sum(1 for tr in trades if tr.get("stopped")),
        },
    }


def spread_snapshot(
    legs: dict[str, dict[str, Any]],
    *,
    now_ms: int,
    lookback_days: float = 7.0,
    entry_apr: float = 0.10,
    exit_apr: float = 0.0,
    fees: dict[str, float] | None = None,
    slippage: float = 0.0005,
    max_entry_gap: float = 0.02,
    mismatch_gap: float = 0.20,
) -> dict[str, Any]:
    """Best cross-venue funding pair for one base: short where trailing funding is highest, long where lowest.

    ``legs[venue] = {"entries": [{ts_ms, rate}], "rate", "interval_hours", "mark", "cap"}`` (current fields
    optional). Spreads are trailing realized APRs (sums of actual payments). ``roundtrip_cost`` is both legs'
    taker fees plus ``slippage`` per leg, paid on entry and exit. Flags:
    ``price_mismatch`` (marks differ by more than ``mismatch_gap``: likely different instruments under one
    ticker), ``price_gap`` (more than ``max_entry_gap``), ``current_spread_reversed`` (current funding now favours
    the other direction), ``funding_at_cap`` (a leg's current rate within 5 % of its cap), ``short_history``,
    ``interval_mismatch``. ``tradable_now`` = above entry and none of the price/reversal flags.
    """
    fees = fees or {}
    venues: dict[str, dict[str, Any]] = {}
    for v, leg in legs.items():
        snap = carry_snapshot(leg.get("entries") or [], now_ms=now_ms, lookback_days=lookback_days)
        rate, hours = leg.get("rate"), leg.get("interval_hours")
        venues[v] = {
            "trailing_apr": snap.get("trailing_apr"),
            "trailing_30d_apr": snap.get("trailing_30d_apr"),
            "coverage_days": snap.get("coverage_days"),
            "current_rate": rate,
            "interval_hours": hours,
            "current_apr": rate * 8760.0 / hours if rate is not None and hours else None,
            "mark": leg.get("mark"),
            "cap": leg.get("cap"),
        }
    valid = {v: x for v, x in venues.items() if x["trailing_apr"] is not None}
    if len(valid) < 2:
        return {"venues": venues, "pair": None}
    short_v = max(valid, key=lambda v: valid[v]["trailing_apr"])
    long_v = min(valid, key=lambda v: valid[v]["trailing_apr"])
    s, lg = valid[short_v], valid[long_v]
    spread = s["trailing_apr"] - lg["trailing_apr"]
    current = s["current_apr"] - lg["current_apr"] if s["current_apr"] is not None and lg["current_apr"] is not None else None
    roundtrip = 2 * (fees.get(short_v, 0.0005) + fees.get(long_v, 0.0005) + 2 * slippage)
    gap = s["mark"] / lg["mark"] - 1.0 if s["mark"] and lg["mark"] else None
    flags = []
    if gap is not None and abs(gap) > mismatch_gap:
        flags.append("price_mismatch")
    elif gap is not None and abs(gap) > max_entry_gap:
        flags.append("price_gap")
    if current is not None and current < 0:
        flags.append("current_spread_reversed")
    if any(x["cap"] and x["current_rate"] is not None and abs(x["current_rate"]) >= 0.95 * x["cap"] for x in (s, lg)):
        flags.append("funding_at_cap")
    if any((x["coverage_days"] or 0) < lookback_days for x in (s, lg)):
        flags.append("short_history")
    if s["interval_hours"] and lg["interval_hours"] and s["interval_hours"] != lg["interval_hours"]:
        flags.append("interval_mismatch")
    if spread >= entry_apr:
        status = "above_entry"
    elif spread <= exit_apr:
        status = "below_exit"
    else:
        status = "between"
    blocking = {"price_mismatch", "price_gap", "current_spread_reversed"}
    return {
        "venues": venues,
        "pair": {
            "short_venue": short_v,
            "long_venue": long_v,
            "trailing_spread_apr": spread,
            "current_spread_apr": current,
            "status": status,
            "roundtrip_cost": roundtrip,
            "breakeven_days": roundtrip / (spread / 365.0) if spread > 0 else None,
            "price_gap": gap,
            "flags": flags,
            "tradable_now": status == "above_entry" and not blocking.intersection(flags),
        },
    }
