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
from functools import partial
from typing import Any

from .carry_rules import (
    DECISION_DELAY_MS,
    carry_mark,
    carry_pnl_now,
    carry_side_cost,
    carry_step,
    carry_trade,
    spread_mark,
    spread_pnl_now,
    spread_side_cost,
    spread_step,
    spread_trade,
)
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


def carry_observers(ms: dict[str, Any], lookback_ms: int) -> tuple[Any, Any]:
    """Observation callbacks for ``carry_step`` over ``{symbol: _Market}``: the one place that reads bars and funding.

    ``mark_obs(sym, pos, at=t)`` and ``entry_obs(sym, at=t)``; used by the backtest and by the paper engine alike.
    """

    def mark_obs(sym: str, p: dict[str, Any], at: int) -> dict[str, Any]:
        m = ms[sym]
        return {
            "payments": [(ts_pay, rate, m.perp(ts_pay)) for ts_pay, rate in m.payments(p["last"], at)],
            "perp_high": m.perp_high(p["last_bar"], at),
            "spot": m.spot(at),
            "perp": m.perp(at),
            "apr": m.apr(at, lookback_ms),
        }

    def entry_obs(sym: str, at: int) -> tuple[float | None, float | None, float | None]:
        m = ms[sym]
        return m.apr(at, lookback_ms), m.spot(at), m.perp(at)

    return mark_obs, entry_obs


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
    start = (start // step + 1) * step + DECISION_DELAY_MS
    end = max(m.f_ts[-1] for m in ms.values())
    side_cost = carry_side_cost(spot_fee, perp_fee, slippage)
    w = 1.0 / slots
    open_pos: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity: list[tuple[int, float]] = []
    realized = 0.0
    t = start

    mark_obs_at, entry_obs_at = carry_observers(ms, lookback_ms)
    while t <= end:
        res = carry_step(
            open_pos,
            t,
            ms,
            partial(mark_obs_at, at=t),
            partial(entry_obs_at, at=t),
            slots=slots,
            entry_apr=entry_apr,
            exit_apr=exit_apr,
            side_cost=side_cost,
        )
        for tr in res["closed"]:
            realized += w * tr["pnl"]
            trades.append(tr)
        equity.append((t, realized + sum(w * carry_pnl_now(p) for p in open_pos.values())))
        t += step
    for p in list(open_pos.values()):
        o = mark_obs_at(p["symbol"], p, end)
        carry_mark(p, end, payments=o["payments"], perp_high=o["perp_high"], spot=o["spot"], perp=o["perp"])
        tr = carry_trade(p, p["last"], side_cost, at_end=True)
        realized += w * tr["pnl"]
        trades.append(tr)
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


def spread_observers(legs: dict[str, tuple[_Leg, _Leg]], lookback_ms: int) -> tuple[Any, Any]:
    """Observation callbacks for ``spread_step`` over ``{symbol: (_Leg A, _Leg B)}`` (see ``carry_observers``)."""

    def mark_obs(sym: str, p: dict[str, Any], at: int) -> dict[str, Any]:
        a, b = legs[sym]
        return {
            "pay_a": [(ts_pay, rate, a.price(ts_pay)) for ts_pay, rate in a.payments(p["last"], at)],
            "pay_b": [(ts_pay, rate, b.price(ts_pay)) for ts_pay, rate in b.payments(p["last"], at)],
            "ext_a": a.extremes(p["last_bar"], at),
            "ext_b": b.extremes(p["last_bar"], at),
            "pa": a.price(at),
            "pb": b.price(at),
            "ra": a.apr(at, lookback_ms),
            "rb": b.apr(at, lookback_ms),
        }

    def entry_obs(sym: str, at: int) -> tuple[float | None, float | None, float | None, float | None]:
        a, b = legs[sym]
        return a.apr(at, lookback_ms), b.apr(at, lookback_ms), a.price(at), b.price(at)

    return mark_obs, entry_obs


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
    start = (start // step + 1) * step + DECISION_DELAY_MS
    end = max(min(a.f_ts[-1], b.f_ts[-1]) for a, b in legs.values())
    side_cost = spread_side_cost(fee_a, fee_b, slippage)
    w = 1.0 / slots
    open_pos: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    equity: list[tuple[int, float]] = []
    realized = 0.0
    t = start

    mark_obs_at, entry_obs_at = spread_observers(legs, lookback_ms)
    while t <= end:
        res = spread_step(
            open_pos,
            t,
            legs,
            partial(mark_obs_at, at=t),
            partial(entry_obs_at, at=t),
            slots=slots,
            entry_apr=entry_apr,
            exit_apr=exit_apr,
            side_cost=side_cost,
            max_entry_gap=max_entry_gap,
            stop_move=stop_move,
            stop_penalty=stop_penalty,
        )
        for tr in res["closed"]:
            realized += w * tr["pnl"]
            trades.append(tr)
        equity.append((t, realized + sum(w * spread_pnl_now(p) for p in open_pos.values())))
        t += step
    for sym, p in list(open_pos.items()):
        o = mark_obs_at(sym, p, end)
        spread_mark(p, end, pay_a=o["pay_a"], pay_b=o["pay_b"], ext_a=o["ext_a"], ext_b=o["ext_b"], pa=o["pa"], pb=o["pb"])
        tr = spread_trade(p, end, side_cost, stop_penalty, at_end=True)
        realized += w * tr["pnl"]
        trades.append(tr)
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


def _bar_price(rows: list[tuple[int, int, dict[str, Any]]], t_ms: int, bar_ms: int, fallback: float) -> float:
    """Close of the latest ``(ts, duration, bar)`` row that closed at or before ``t_ms`` (60 s grace), like
    ``_Leg.price``; ``fallback`` when none closed within ``bar_ms`` before ``t_ms``."""
    best: float | None = None
    best_t = -1
    for ts, dur, b in rows:
        close_t = ts + dur
        if close_t <= t_ms + 60_000 and close_t > best_t:
            best, best_t = float(b["close"]), close_t
    if best is None or t_ms - best_t > bar_ms:
        return fallback
    return best


def _leg_rows(
    bars: list[dict[str, Any]], fine: list[dict[str, Any]] | None, opened_ms: int, now_ms: int, bar_ms: int, fine_ms: int
) -> tuple[list[tuple[int, int, dict[str, Any]]], bool]:
    """``(ts, duration, bar)`` rows of one leg and whether the entry bar was refined.

    The bar containing ``opened_ms`` would mix in up to ``bar_ms`` of prices from before the entry, so it is replaced
    by the ``fine`` bars (``fine_ms`` each) inside it whose close is after ``opened_ms``, provided they cover every
    fine slot from the entry to ``min(bar end, now_ms)`` that has closed. Otherwise (``fine`` None or with holes) the
    whole bar is kept: conservative, prices before the entry may then trigger the stop."""
    rows = [(int(b["ts"]), bar_ms, b) for b in bars]
    if fine is None:
        return rows, False
    start = next((ts for ts, _, _ in rows if ts <= opened_ms < ts + bar_ms), opened_ms - opened_ms % bar_ms)
    end = start + bar_ms
    inside = [b for b in fine if start <= int(b["ts"]) < end and int(b["ts"]) + fine_ms > opened_ms]
    have = {int(b["ts"]) // fine_ms for b in inside}
    if any(k not in have for k in range(opened_ms // fine_ms, min(end, now_ms) // fine_ms)):
        return rows, False
    return [x for x in rows if x[0] != start] + [(int(b["ts"]), fine_ms, b) for b in inside], True


def pair_paper_mark(
    *,
    opened_ms: int,
    now_ms: int,
    notional_usd: float,
    long_entry: float,
    short_entry: float,
    long_funding: list[dict[str, Any]],
    short_funding: list[dict[str, Any]],
    long_bars: list[dict[str, Any]],
    short_bars: list[dict[str, Any]],
    long_now: float,
    short_now: float,
    spread_apr: float | None,
    fee_long: float,
    fee_short: float,
    slippage: float = 0.0005,
    stop_move: float | None = None,
    stop_penalty: float = 0.01,
    exit_apr: float = 0.0,
    bar_ms: int = 4 * 3_600_000,
    long_entry_bars: list[dict[str, Any]] | None = None,
    short_entry_bars: list[dict[str, Any]] | None = None,
    entry_bar_ms: int = 15 * 60_000,
) -> dict[str, Any]:
    """Mark one paper perp-perp carry pair (long one venue, short another, same coin quantity) to ``now_ms``.

    Mirrors ``simulate_perp_spread_portfolio`` accounting, in USD on ``notional_usd`` (qty = notional / long_entry):
    the long leg pays ``rate x qty x price`` and the short leg receives it, per funding payment with
    ``opened_ms < ts <= end``, priced at the leg's last 4h bar close at or before the payment (entry price when
    no such bar). Price P&L = ``qty x ((long_x - long_entry) - (short_x - short_entry))``. Costs = entry side
    ``(fee_long + fee_short + 2 x slippage) x notional`` plus the same for the exit scaled by ``long_x / long_entry``
    (an estimate as if closed at ``end``), plus ``stop_penalty x notional`` when stopped.

    Stop, defined as ``mark()`` in the backtest does: ``max_up`` is the largest ``high / entry - 1`` over BOTH legs and
    ``max_down`` the largest ``1 - low / entry`` over both legs, from bars whose close is after ``opened_ms`` (the bar
    containing the entry included, conservative) and which opened at or before ``now_ms``. With
    ``long_entry_bars`` / ``short_entry_bars`` (``entry_bar_ms`` bars, e.g. 15m), the bar containing the entry is
    replaced by those finer bars from the entry on, so prices before the entry cannot trigger the stop (see
    ``_leg_rows``; ``long_entry_fine`` / ``short_entry_fine`` report whether that happened). The first bar (in time
    order) where either reaches ``stop_move`` (= 1 / leverage) is the stop: ``stop_ts`` = that bar's close (at most
    ``now_ms``), and funding, price P&L, exit costs and the penalty are frozen there (``end = stop_ts``, prices =
    the bars' closes at ``stop_ts``). Without a bar trigger the current prices are checked at ``now_ms``.
    Otherwise ``end = now_ms`` and ``spread_apr`` (trailing spread in the held direction) ``<= exit_apr`` gives
    ``exit_signal = "spread_exit"``. Returns are fractions of ``notional_usd``.
    """
    qty = notional_usd / long_entry
    long_rows, long_fine = _leg_rows(long_bars, long_entry_bars, opened_ms, now_ms, bar_ms, entry_bar_ms)
    short_rows, short_fine = _leg_rows(short_bars, short_entry_bars, opened_ms, now_ms, bar_ms, entry_bar_ms)
    legs = ((long_rows, long_entry, long_now), (short_rows, short_entry, short_now))
    max_up = max_down = 0.0
    stop_ts: int | None = None
    window = sorted(
        (ts, dur, float(b["high"]) / e - 1.0, 1.0 - float(b["low"]) / e)
        for rows, e, _ in legs
        for ts, dur, b in rows
        if ts + dur > opened_ms and ts <= now_ms
    )
    for ts, dur, up, down in window:
        max_up, max_down = max(max_up, up), max(max_down, down)
        if stop_move is not None and max(max_up, max_down) >= stop_move:
            stop_ts = min(ts + dur, now_ms)
            break
    if stop_ts is None:
        for _, e, px in legs:
            max_up, max_down = max(max_up, px / e - 1.0), max(max_down, 1.0 - px / e)
        if stop_move is not None and max(max_up, max_down) >= stop_move:
            stop_ts = now_ms
    end = stop_ts if stop_ts is not None else now_ms
    if stop_ts is not None and stop_ts < now_ms:
        long_x = _bar_price(long_rows, end, bar_ms, long_now)
        short_x = _bar_price(short_rows, end, bar_ms, short_now)
    else:
        long_x, short_x = long_now, short_now
    funding = 0.0
    n_payments = 0
    for pays, bars, entry, sign in ((long_funding, long_rows, long_entry, -1.0), (short_funding, short_rows, short_entry, 1.0)):
        for e in pays:
            ts = int(e["ts_ms"])
            if e.get("rate") is None or not opened_ms < ts <= end:
                continue
            funding += sign * float(e["rate"]) * qty * _bar_price(bars, ts, bar_ms, entry)
            n_payments += 1
    price_pnl = qty * ((long_x - long_entry) - (short_x - short_entry))
    side_cost = (fee_long + fee_short + 2 * slippage) * notional_usd
    costs = side_cost + side_cost * long_x / long_entry + (stop_penalty * notional_usd if stop_ts is not None else 0.0)
    pnl = funding + price_pnl - costs
    held_days = max(end - opened_ms, 0) / DAY_MS
    ret = pnl / notional_usd
    if stop_ts is not None:
        signal: str | None = "stop"
    elif spread_apr is not None and spread_apr <= exit_apr:
        signal = "spread_exit"
    else:
        signal = None
    return {
        "qty": qty,
        "funding_pnl": funding,
        "n_funding_payments": n_payments,
        "price_pnl": price_pnl,
        "costs": costs,
        "pnl_usd": pnl,
        "return_on_notional": ret,
        "held_days": held_days,
        "realized_apr": ret * 365.0 / held_days if held_days > 0 else None,
        "max_up": max_up,
        "max_down": max_down,
        "spread_apr": spread_apr,
        "exit_signal": signal,
        "stopped": stop_ts is not None,
        "stop_ts_ms": stop_ts,
        "long_x": long_x,
        "short_x": short_x,
        "long_entry_fine": long_fine,
        "short_entry_fine": short_fine,
    }


def bar_gaps(bars: list[dict[str, Any]], opened_ms: int, now_ms: int, bar_ms: int = 4 * 3_600_000) -> int:
    """Number of completed bars missing from the entry bar to the last bar closed by ``now_ms`` (0 = full coverage).

    The bar still in progress at ``now_ms`` is not expected: some venues return only closed bars, and counting it
    gave a false "incomplete" on every call. Grid slots are ``ts // bar_ms``."""
    first, last = opened_ms // bar_ms, now_ms // bar_ms - 1  # `last`: the latest slot whose bar closed by now_ms
    if last < first:
        return 0
    covered = {int(b["ts"]) // bar_ms for b in bars if first <= int(b["ts"]) // bar_ms <= last}
    return (last - first + 1) - len(covered)


def paper_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Stats over closed paper pairs (rows shaped like ``carry_paper``): fractions of notional, comparable with the backtest.

    Returns the stats of all closed pairs plus ``entry_tradable``: the same stats for the pairs that met the backtest's
    entry rule (``tradable_now`` in the held direction at open), the subset comparable with the backtest.
    """
    closed = [x for x in rows if x.get("status") == "closed" and x.get("return_on_notional") is not None]
    return {**_paper_stats(closed), "entry_tradable": _paper_stats([x for x in closed if x.get("entry_tradable")])}


def _paper_stats(closed: list[dict[str, Any]]) -> dict[str, Any]:
    if not closed:
        return {"n": 0}
    rets = sorted(float(x["return_on_notional"]) for x in closed)
    n = len(rets)
    median = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
    held = [
        (int(x["closed_ts"]) - int(x["opened_ts"])) / 86400
        for x in closed
        if x.get("closed_ts") is not None and x.get("opened_ts") is not None
    ]
    return {
        "n": n,
        "mean_return": sum(rets) / n,
        "median_return": median,
        "share_losing": sum(1 for v in rets if v < 0) / n,
        "share_stopped": sum(1 for x in closed if x.get("stopped")) / n,
        "mean_held_days": sum(held) / len(held) if held else None,
        "sum_funding_usd": sum(float(x.get("funding_pnl") or 0.0) for x in closed),
        "sum_price_usd": sum(float(x.get("price_pnl") or 0.0) for x in closed),
        "sum_costs_usd": sum(float(x.get("costs") or 0.0) for x in closed),
        "sum_pnl_usd": sum(float(x.get("pnl_usd") or 0.0) for x in closed),
    }
