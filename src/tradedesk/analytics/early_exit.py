"""Exit modules for an already-decided trade (IDEA-20261016-course-setups-crypto, step 2): an early time exit
("leave at market after T if the best excursion is below +x R") and a breakeven stop after an internal target.

Pure functions, no I/O. The trade is a long in price space; a short is passed on negated prices (``Bars.mirrored``).
The path is walked bar by bar from the entry (a bar close). Every decision at bar k uses bars <= k only: the early
exit is decided at the close of the last bar opening before entry + T, the breakeven stop becomes active from the bar
after the one whose high crossed the trigger (the order inside a bar is unknown, so the trigger bar keeps the old
stop). With the default ``ExitRule()`` the result equals ``course_s14_ema.simulate`` exactly (tests check this).

Fees are decimal fractions of price per side (0.0007 = 0.05 % taker + 0.02 % slippage), converted to R with the
trade's own stop distance. R is a return on the trade's risk, not on capital.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import MARKET_SIDE, MAX_GAP_MS, Bars


@dataclass(frozen=True)
class ExitRule:
    check_ms: int | None = None  # early exit checked at entry + check_ms; None = no early exit
    min_mfe_r: float = 0.5  # leave if the best high so far is below entry + min_mfe_r * R
    be_trigger_r: float | None = None  # stop to the entry price after a bar high >= entry + be_trigger_r * R

    @property
    def label(self) -> str:
        parts = []
        if self.check_ms is not None:
            parts.append(f"E{self.check_ms / 3_600_000:g}h@{self.min_mfe_r:g}R")
        if self.be_trigger_r is not None:
            parts.append(f"BE@{self.be_trigger_r:g}R")
        return "+".join(parts) or "base"


BASE = ExitRule()


@dataclass(frozen=True)
class Managed:
    entry: float
    stop: float
    target: float
    exit: float
    exit_kind: str  # stop | stop_gap | target | time | data_end | early | be | be_gap
    both_touched: bool
    r_gross: float
    cost_r: float
    mfe_r: float  # best excursion in R before the exit (bar highs)
    entry_ms: int
    exit_ms: int  # close time of the exit bar

    @property
    def r_net(self) -> float:
        return self.r_gross - self.cost_r


def simulate_managed(
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: ExitRule = BASE,
    fee_in: float = MARKET_SIDE,
    fee_out: float = MARKET_SIDE,
    max_gap_ms: int = MAX_GAP_MS,
) -> Managed | None:
    """Long held from ``entry_ms`` over ``path`` bars opening in [entry_ms, entry_ms + hold_ms). The stop (or the
    breakeven stop) wins when a bar touches it and the target; an open beyond it exits at the open; the time exit is
    the last bar's close; a data end or a hole longer than ``max_gap_ms`` ends the trade at the last close before it
    (``data_end``). ``rule`` adds the early exit and/or the breakeven stop."""
    risk = entry - stop
    if risk <= 0 or len(path) == 0:
        return None
    i0 = int(np.searchsorted(path.t, entry_ms))
    i1 = int(np.searchsorted(path.t, entry_ms + hold_ms))
    if i1 <= i0 or int(path.t[i0]) - entry_ms > max_gap_ms:
        return None
    cut = False
    holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
    if holes.size:
        i1, cut = i0 + int(holes[0]) + 1, True
    chk = -1
    if rule.check_ms is not None:
        chk = int(np.searchsorted(path.t, entry_ms + rule.check_ms)) - 1  # last bar opening before entry + T
        ends_early = chk == i1 - 1 and int(path.t[chk]) + path.bar_ms < entry_ms + rule.check_ms
        if chk < i0 or chk >= i1 or ends_early:
            chk = -1  # no bar in the window, or the trade's data end before the check
    keep = entry + rule.min_mfe_r * risk
    be_level = entry + rule.be_trigger_r * risk if rule.be_trigger_r is not None else None
    cur_stop, be_on = stop, False
    best = entry
    px, kind, both, j = float(path.c[i1 - 1]), ("data_end" if cut else "time"), False, i1 - 1
    for k in range(i0, i1):
        if path.o[k] <= cur_stop:
            px, kind, j = float(path.o[k]), ("be_gap" if be_on else "stop_gap"), k
            break
        if path.low[k] <= cur_stop:
            px, kind, both, j = cur_stop, ("be" if be_on else "stop"), bool(path.h[k] >= target), k
            break
        if path.h[k] >= target:
            px, kind, j = target, "target", k
            best = max(best, target)
            break
        best = max(best, float(path.h[k]))
        if be_level is not None and not be_on and path.h[k] >= be_level:
            be_on, cur_stop = True, max(cur_stop, entry)  # active from the next bar
        if k == chk and best < keep:
            px, kind, j = float(path.c[k]), "early", k
            break
    if kind == "time" and i1 == len(path) and int(path.t[i1 - 1]) + path.bar_ms < entry_ms + hold_ms:
        kind = "data_end"
    gross = (px - entry) / risk
    cost = (fee_in * abs(entry) + fee_out * abs(px)) / risk
    return Managed(entry, stop, target, px, kind, both, gross, cost, (best - entry) / risk, entry_ms, int(path.t[j]) + path.bar_ms)


def pick_placebo_times(
    candidates: np.ndarray, center_ms: int, window_ms: int, k: int, rng: np.random.Generator, exclude_ms: int | None = None
) -> np.ndarray:
    """Up to ``k`` distinct entry times drawn uniformly from sorted ``candidates`` within ``center_ms`` +- ``window_ms``,
    never ``exclude_ms`` (the signal's own entry)."""
    c = np.asarray(candidates, dtype=np.int64)
    lo = int(np.searchsorted(c, center_ms - window_ms, side="left"))
    hi = int(np.searchsorted(c, center_ms + window_ms, side="right"))
    pool = c[lo:hi]
    if exclude_ms is not None:
        pool = pool[pool != exclude_ms]
    if pool.size == 0:
        return pool
    return np.sort(rng.choice(pool, size=min(k, pool.size), replace=False))
