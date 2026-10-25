"""Stop triggered by the mark price instead of the last price (IDEA-20261025-mark-price-stop-trigger).

Pure functions, no I/O. A trade is a long in price space; a short is passed on negated prices (``Bars.mirrored``) for
both the last-price and the mark-price bars. The last-price branch is ``stop_management.simulate_stop_rule``; the
mark branch below is the same rule with one change: the stop (initial or moved) fires when the **mark** bar reaches it.
The target, the break-even trigger and the time exit stay on the last price, as in the stop study.

Mark-branch fill on a mark trigger in 1h bar k (pre-registered, never better than the last-price fill of that bar):
- mark gap (mark open at or beyond the stop): ``min(stop, last.o[k])``;
- intra-bar mark touch: ``stop`` when the last price opened the bar on the right side of the stop (the same fill as the
  last branch when both touch in this bar); otherwise ``min(last.o[k], last.c[k])`` (the last price was already beyond
  the stop at the open, the mark lags: it pays the worse of the bar's open and close).
With mark bars identical to the last bars the mark branch reproduces the last branch exactly.
A bar where the last price touches the stop but the mark does not is a *divergent* stop: the trade continues.
A mark bar missing on any bar the mark branch visits makes the trade unusable (``None``), for both branches.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import MAX_GAP_MS, Bars
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, risk_unit

STOP_KINDS = ("stop", "stop_gap", "be", "be_gap")


@dataclass(frozen=True)
class Aligned:
    """Mark bars aligned to the last-price bar index: ``ok[k]`` is False where no mark bar opens at ``last.t[k]``."""

    o: np.ndarray
    h: np.ndarray
    low: np.ndarray
    ok: np.ndarray


def align(last: Bars, mark: Bars) -> Aligned:
    n = len(last)
    o, h, lo = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    ok = np.zeros(n, dtype=bool)
    if n and len(mark):
        j = np.searchsorted(mark.t, last.t)
        jj = np.minimum(j, len(mark) - 1)
        hit = (j < len(mark)) & (mark.t[jj] == last.t)
        o[hit], h[hit], lo[hit] = mark.o[jj[hit]], mark.h[jj[hit]], mark.low[jj[hit]]
        ok = hit & np.isfinite(o) & np.isfinite(lo)
    return Aligned(o, h, lo, ok)


def simulate_mark_stop(
    last: Bars,
    mk: Aligned,
    entry_ms: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    fees: Fees = Fees(),
    max_gap_ms: int = MAX_GAP_MS,
    fill: str = "conservative",
) -> Outcome | None:
    """``simulate_stop_rule`` (break-even rules only, no trailing) with the stop fired by the mark bars ``mk``.

    ``fill``: ``conservative`` (pre-registered, see the module doc), ``stop`` (sensitivity: fill exactly at the stop on
    an intra-bar touch, ``min(stop, last.o)`` on a mark gap), ``worst`` (sensitivity: ``min(stop, last.low[k])``)."""
    if rule.trail:
        raise ValueError("the mark branch supports break-even rules only")
    if entry - stop <= 0 or len(last) == 0:
        return None
    ru = risk_unit(entry, stop, fees)
    i0 = int(np.searchsorted(last.t, entry_ms))
    i1 = int(np.searchsorted(last.t, entry_ms + hold_ms))
    if i1 <= i0 or int(last.t[i0]) - entry_ms > max_gap_ms:
        return None
    cut = False
    holes = np.flatnonzero(np.diff(last.t[i0:i1]) > max_gap_ms)
    if holes.size:
        i1, cut = i0 + int(holes[0]) + 1, True
    tgt = target if rule.use_target else np.inf
    trig = rule.trigger(entry, stop, target)
    be_stop = entry + fees.be_offset * abs(entry)
    cur, how = stop, "stop"
    armed = be_done = False
    move_ms = -1
    best = entry
    px, kind, j = float(last.c[i1 - 1]), ("data_end" if cut else "time"), i1 - 1
    for k in range(i0, i1):
        if not mk.ok[k]:
            return None
        tk = int(last.t[k])
        if armed and not be_done:
            be_done = True
            if be_stop > cur:
                cur, how = be_stop, "be"
                move_ms = tk if move_ms < 0 else move_ms
        if mk.o[k] <= cur:
            px, kind, j = min(cur, float(last.o[k])), f"{how}_gap", k
            break
        if mk.low[k] <= cur:
            if fill == "conservative":
                px = cur if last.o[k] > cur else min(float(last.o[k]), float(last.c[k]))
            elif fill == "stop":
                px = min(cur, float(last.o[k])) if last.o[k] <= cur else cur
            elif fill == "worst":
                px = min(cur, float(last.low[k]))
            else:
                raise ValueError(f"unknown fill {fill!r}")
            kind, j = how, k
            break
        if last.h[k] >= tgt:
            px, kind, j = float(tgt), "target", k
            best = max(best, float(tgt))
            break
        best = max(best, float(last.h[k]))
        if trig is not None and not armed and last.h[k] >= trig:
            armed = True
    if kind == "time" and i1 == len(last) and int(last.t[i1 - 1]) + last.bar_ms < entry_ms + hold_ms:
        kind = "data_end"
    fee_out = fees.maker if kind == "target" else fees.taker_exit
    gross = (px - entry) / ru
    cost = (fees.maker * abs(entry) + fee_out * abs(px)) / ru
    return Outcome(px, kind, move_ms >= 0, move_ms, gross, cost, (best - entry) / ru, entry_ms, int(last.t[j]) + last.bar_ms)


def stop_level_at_exit(o: Outcome, entry: float, stop: float, fees: Fees = Fees()) -> float:
    """The stop level in force at a stop exit of the last branch (initial stop or break-even), price space."""
    return entry + fees.be_offset * abs(entry) if o.exit_kind.startswith("be") else stop


def divergent(o: Outcome, last: Bars, mk: Aligned, entry: float, stop: float, fees: Fees = Fees()) -> bool | None:
    """True when the last branch stopped out in a bar where the mark bar stayed strictly above the stop in force
    (long space: mark low > stop; a short's mark high < stop in real prices). None: not a stop exit or no mark bar."""
    if o.exit_kind not in STOP_KINDS:
        return False
    k = int(np.searchsorted(last.t, o.exit_ms - last.bar_ms))
    if k >= len(last) or int(last.t[k]) != o.exit_ms - last.bar_ms or not mk.ok[k]:
        return None
    return bool(mk.low[k] > stop_level_at_exit(o, entry, stop, fees))


def shifted_mark(last: Bars, mark: Bars, shift_ms: int) -> Bars:
    """Placebo mark: at each last bar t, the last OHLC times the mark/last ratios of the bar at t + shift (same symbol,
    another day), so the mark-last spread keeps its size distribution but loses its timing. Bars without both a mark
    and a last bar at t + shift are dropped. Works on real (positive) prices; mirror afterwards for a short."""
    if len(last) == 0 or len(mark) == 0:
        return Bars.from_rows(np.zeros((0, 5)), last.bar_ms)
    a = align(last, mark)
    src = np.searchsorted(last.t, last.t + shift_ms)
    srcc = np.minimum(src, len(last) - 1)
    ok = (src < len(last)) & (last.t[srcc] == last.t + shift_ms) & a.ok[srcc]
    mark_c = np.full(len(last), np.nan)
    jm = np.searchsorted(mark.t, last.t)
    jmc = np.minimum(jm, len(mark) - 1)
    hitc = (jm < len(mark)) & (mark.t[jmc] == last.t)
    mark_c[hitc] = mark.c[jmc[hitc]]
    ok &= np.isfinite(mark_c[srcc])
    idx = np.flatnonzero(ok)
    s = srcc[idx]
    ro = a.o[s] / last.o[s]
    rh = a.h[s] / last.h[s]
    rl = a.low[s] / last.low[s]
    rc = mark_c[s] / last.c[s]
    o = last.o[idx] * ro
    c = last.c[idx] * rc
    h = np.maximum.reduce([last.h[idx] * rh, o, c])
    lo = np.minimum.reduce([last.low[idx] * rl, o, c])
    return Bars(last.t[idx].astype(np.int64), o, h, lo, c, last.bar_ms)


def mde_clustered(flags: np.ndarray, clusters: np.ndarray, mu: float, sd: float, z: float = 2.8) -> float:
    """Minimum detectable mean of a paired per-trade difference that is ``X ~ (mu, sd)`` on flagged trades and 0
    elsewhere, under a day-cluster sandwich variance: Var(mean) = sum_c [k_c sd^2 + (k_c mu - n_c q mu)^2] / N^2 with
    k_c flagged and n_c all trades of cluster c, q the pooled flagged share. Returns ``z`` times the standard error."""
    f = np.asarray(flags, dtype=float)
    n = f.size
    if n == 0:
        return float("nan")
    _, inv = np.unique(np.asarray(clusters), return_inverse=True)
    kc = np.bincount(inv, weights=f)
    nc = np.bincount(inv).astype(float)
    q = f.mean()
    var = float(np.sum(kc * sd**2 + (kc * mu - nc * q * mu) ** 2)) / n**2
    return z * var**0.5
