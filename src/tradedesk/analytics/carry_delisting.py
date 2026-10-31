"""Strategy A (cash-and-carry) when the short perp leg is delisted: accounting and an exit rule.

The frozen backtest (``carry.simulate_carry_portfolio``) only updates a position's prices when both legs have a bar,
so once the perp disappears it freezes the whole position and never sees the spot move that follows. This module
walks the same frozen decision cycle (``carry_rules.carry_step``) with two additions:

* ``perp_end`` (accounting, known after the fact): after the perp's last data point the perp leg stays at its last
  4h close (a proxy of the exchange settlement price) while the spot leg keeps being marked, until the frozen exit
  rule closes the position or the spot data ends;
* ``close_on_vanish`` (a causal rule): at a cycle where the open position has had ``n_cycles`` consecutive 8 h
  cycles with no funding payment and no perp bar close, and the spot has a price, close it (spot at the bar close,
  perp at its last close before the cycle).

With neither addition the walk reproduces ``simulate_carry_portfolio`` trade for trade. Pure functions, no I/O;
returns are fractions of portfolio notional (slot = 1 / slots), as in the frozen simulator.
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

from .carry import DAY_MS, YEAR_MS, _Market, carry_observers
from .carry_rules import (
    DECISION_DELAY_MS,
    carry_mark,
    carry_pnl_now,
    carry_side_cost,
    carry_step,
    carry_trade,
)

STEP_MS = 8 * 3_600_000


def missing_cycles(f_ts: Sequence[int], p_close_t: Sequence[int], t: int, *, step_ms: int = STEP_MS, max_n: int = 10) -> int:
    """Consecutive cycles ``(t - k*step, t - (k-1)*step]``, k = 1, 2, ..., with no funding payment and no perp bar
    close, counted back from ``t`` (at most ``max_n``). Uses only data at or before ``t``."""
    n = 0
    for k in range(1, max_n + 1):
        lo, hi = t - k * step_ms, t - (k - 1) * step_ms
        if bisect.bisect_right(f_ts, hi) > bisect.bisect_right(f_ts, lo):
            break
        if bisect.bisect_right(p_close_t, hi) > bisect.bisect_right(p_close_t, lo):
            break
        n += 1
    return n


def last_perp_close(m: _Market, t: int) -> float | None:
    """Last perp 4h close at or before ``t`` (the settlement-price proxy after the leg disappears)."""
    i = bisect.bisect_right(m.p_close_t, t) - 1
    return m.p_close[i] if i >= 0 else None


def last_spot_close(m: _Market, t: int) -> float | None:
    """Last spot 4h close at or before ``t`` (an index-price proxy for the exchange settlement)."""
    i = bisect.bisect_right(m.s_close_t, t) - 1
    return m.s_close[i] if i >= 0 else None


def settle_price(m: _Market, t_end: int, settle: str) -> float | None:
    """Perp-leg settlement proxy at the leg's last data point ``t_end``: 'perp_last' (last perp close, registered)
    or 'spot' (last spot close, the exchange settles at an index average)."""
    if settle == "perp_last":
        return last_perp_close(m, t_end)
    if settle == "spot":
        return last_spot_close(m, t_end)
    raise ValueError(f"unknown settle {settle!r}")


def perp_end_ms(data: Mapping[str, Any], bar_ms: int = 4 * 3_600_000) -> int | None:
    """Last data point of the perp leg: max(last funding payment, close of the last perp bar)."""
    f = [int(e["ts_ms"]) for e in data.get("funding") or []]
    p = [int(b["ts"]) + bar_ms for b in data.get("perp") or []]
    if not f and not p:
        return None
    return max(f + p)


def vanish_events(
    markets: Mapping[str, Mapping[str, Any]], end_ms: int, *, n_cycles: int = 2, step_ms: int = STEP_MS, bar_ms: int = 4 * 3_600_000
) -> dict[str, dict[str, Any]]:
    """Symbols whose perp ends for good at least ``n_cycles`` cycles before ``end_ms``: ``{sym: {perp_end,
    spot_after}}`` where ``spot_after`` says whether the spot still has a bar closing ``n_cycles`` cycles later."""
    out: dict[str, dict[str, Any]] = {}
    for sym, d in markets.items():
        pe = perp_end_ms(d, bar_ms)
        if pe is None or pe > end_ms - n_cycles * step_ms:
            continue
        s_last = max((int(b["ts"]) + bar_ms for b in d.get("spot") or []), default=0)
        out[sym] = {"perp_end": pe, "spot_after": s_last >= pe + n_cycles * step_ms}
    return out


def interval_change_before(funding_rows: Sequence[Sequence[float]], end_ms: int, window_days: float = 30.0) -> dict[str, Any]:
    """Funding-interval changes (archive rows ``[ts_ms, interval_hours, rate]``) within ``window_days`` before
    ``end_ms``: ``{changed, from_h, to_h, at_ms}`` of the last change (``changed`` False if none)."""
    lo = end_ms - int(window_days * DAY_MS)
    rows = sorted((int(r[0]), float(r[1])) for r in funding_rows if r[1])
    last: dict[str, Any] = {"changed": False, "from_h": None, "to_h": None, "at_ms": None}
    for (_, a), (t, b) in zip(rows, rows[1:], strict=False):
        if lo <= t <= end_ms and a != b:
            last = {"changed": True, "from_h": a, "to_h": b, "at_ms": t}
    return last


def simulate_carry_delisting(
    markets: dict[str, dict[str, Any]],
    *,
    perp_end: Mapping[str, int] | None = None,
    close_on_vanish: bool = False,
    n_cycles: int = 2,
    settle: str = "perp_last",
    slots: int = 5,
    lookback_days: float = 7.0,
    entry_apr: float = 0.20,
    exit_apr: float = 0.0,
    spot_fee: float = 0.001,
    perp_fee: float = 0.0005,
    slippage: float = 0.0005,
    step_hours: float = 8.0,
    bar_ms: int = 4 * 3_600_000,
    end_ms: int | None = None,
) -> dict[str, Any]:
    """``simulate_carry_portfolio`` with perp-leg delisting accounting (``perp_end``) and the optional exit rule.

    ``settle`` picks the perp-leg settlement proxy once the leg is gone ('perp_last' or 'spot', see ``settle_price``).
    Trades carry ``reason`` ('apr<=exit', 'leg_vanished' or 'end') and ``perp_ended`` (perp end inside the holding
    window, from ``perp_end``). ``end_ms`` overrides the walk's end (default: last funding payment of any symbol).
    """
    pe = dict(perp_end or {})
    ms = {s: _Market(d, bar_ms) for s, d in markets.items() if d.get("funding") and d.get("spot") and d.get("perp")}
    if not ms:
        return {"trades": [], "equity": [], "stats": {}, "rule_closes": []}
    lookback_ms = int(lookback_days * DAY_MS)
    step = int(step_hours * 3_600_000)
    start = min(m.f_ts[0] for m in ms.values()) + lookback_ms
    start = (start // step + 1) * step + DECISION_DELAY_MS
    end = end_ms if end_ms is not None else max(m.f_ts[-1] for m in ms.values())
    side_cost = carry_side_cost(spot_fee, perp_fee, slippage)
    w = 1.0 / slots
    base_mark, base_entry = carry_observers(ms, lookback_ms)

    def mark_obs(sym: str, p: dict[str, Any], at: int) -> dict[str, Any]:
        o = base_mark(sym, p, at)
        e = pe.get(sym)
        if e is not None and at > e and o["perp"] is None:
            o["perp"] = settle_price(ms[sym], e, settle)
        return o

    def rule_perp(m: _Market, t: int) -> float | None:
        i = bisect.bisect_right(m.p_close_t, t) - 1
        return settle_price(m, m.p_close_t[i], settle) if i >= 0 else None

    def tag(tr: dict[str, Any], reason: str) -> dict[str, Any]:
        e = pe.get(tr["symbol"])
        tr["reason"] = reason
        tr["perp_ended"] = e is not None and tr["open_ts"] <= e < tr["close_ts"]
        return tr

    open_pos: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    rule_closes: list[dict[str, Any]] = []
    equity: list[tuple[int, float]] = []
    realized = 0.0
    t = start
    while t <= end:
        if close_on_vanish:
            for sym in list(open_pos):
                m = ms[sym]
                k = missing_cycles(m.f_ts, m.p_close_t, t, step_ms=step, max_n=n_cycles)
                spot = m.spot(t)
                if k < n_cycles or not spot:
                    continue
                p = open_pos.pop(sym)
                o = base_mark(sym, p, t)
                carry_mark(p, t, payments=o["payments"], perp_high=o["perp_high"], spot=spot, perp=rule_perp(m, t))
                tr = tag(carry_trade(p, t, side_cost, at_end=False), "leg_vanished")
                realized += w * tr["pnl"]
                trades.append(tr)
                rule_closes.append({"symbol": sym, "t": t, "open_ts": tr["open_ts"]})
        res = carry_step(
            open_pos,
            t,
            ms,
            partial(mark_obs, at=t),
            partial(base_entry, at=t),
            slots=slots,
            entry_apr=entry_apr,
            exit_apr=exit_apr,
            side_cost=side_cost,
        )
        for tr in res["closed"]:
            realized += w * tr["pnl"]
            trades.append(tag(tr, "apr<=exit"))
        equity.append((t, realized + sum(w * carry_pnl_now(p) for p in open_pos.values())))
        t += step
    for p in list(open_pos.values()):
        o = mark_obs(p["symbol"], p, end)
        carry_mark(p, end, payments=o["payments"], perp_high=o["perp_high"], spot=o["spot"], perp=o["perp"])
        tr = tag(carry_trade(p, p["last"], side_cost, at_end=True), "end")
        realized += w * tr["pnl"]
        trades.append(tr)
    if equity:
        equity[-1] = (equity[-1][0], realized)
    years = max(end - start, 1) / YEAR_MS
    return {
        "trades": trades,
        "equity": equity,
        "rule_closes": rule_closes,
        "stats": {
            "years": years,
            "apr_on_capital": realized / years,
            "total_return": realized,
            "n_trades": len(trades),
            "funding": w * sum(tr["funding"] for tr in trades),
            "basis": w * sum(tr["basis"] for tr in trades),
            "costs": w * sum(tr["fees"] for tr in trades),
        },
    }
