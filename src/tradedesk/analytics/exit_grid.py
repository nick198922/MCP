"""Exit grid for the demo setups (IDEA-20261114-exit-grid): the same entries and stops, other exits. Pure functions,
no I/O; time is a parameter.

The trade is a long in price space (a short is passed on negated prices, ``Bars.mirrored``), held over 1h ``path``
bars from ``entry_ms`` as ``stop_management.simulate_stop_rule``: a bar opening at or beyond the current stop exits
at the open (gap), a bar touching the stop exits at the stop and the stop wins when the same bar also touches a
target; a trigger (break-even, the first half of E, the activation of F) moves the stop from the next bar. The time
exit is the close of the last bar opening before entry + hold; a hole longer than ``max_gap_ms`` ends the trade at
the last close before it.

Variants (registration of the card): A / B / C a target at 1.5R / 2R / 3R and D a structural target floored at 1R,
all with the demo's break-even (``stop_management`` rules, simulated there); E half at 1R and half at 3R with the
second half's stop at break-even after 1R (``simulate_split``); F a 3 x ATR(14) trailing stop activated after 1R,
no target (``simulate_atr_trail``). 1R in prices is ``entry - stop``; results are in the live risk unit
(``stop_management.risk_unit``), i.e. returns on the trade's risk, not on capital or notional.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import MAX_GAP_MS, Bars
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, risk_unit, simulate_stop_rule

RULE_NO_BE = StopRule("noBE")


def r_target(entry: float, stop: float, k: float) -> float:
    """Target ``k`` R above the entry, 1R = entry - stop in prices (long price space)."""
    return entry + k * (entry - stop)


def floored_target(entry: float, stop: float, level: float, floor_r: float = 1.0) -> float:
    """Structural target ``level`` but never closer than ``floor_r`` R to the entry (long price space); a level that
    is not finite falls back to the floor."""
    floor = r_target(entry, stop, floor_r)
    return max(float(level), floor) if math.isfinite(level) else floor


def window_high(path: Bars, t_from: int, t_to: int) -> float:
    """Highest high of the bars of ``path`` opening in [t_from, t_to) (NaN if none). Bars opening at or after
    ``t_to`` are never read, so a level taken up to the hour before the fill does not see the fill hour or later."""
    i0, i1 = int(np.searchsorted(path.t, t_from)), int(np.searchsorted(path.t, t_to))
    return float(np.max(path.h[i0:i1])) if i1 > i0 else math.nan


def limit_start(path: Bars, fill_idx: int, stop: float, hold_ms: int) -> tuple[int, int]:
    """(start time, hold) of a filled resting limit as ``setup_passport.limit_trade``: a fill bar that also touches
    the stop starts the trade in the fill hour (a loss at the stop for every exit variant); otherwise the trade runs
    from the next bar, held ``hold_ms`` from the fill hour."""
    tk = int(path.t[fill_idx])
    if path.low[fill_idx] <= stop or fill_idx + 1 >= len(path):
        return tk, hold_ms
    t_next = int(path.t[fill_idx + 1])
    return t_next, hold_ms - (t_next - tk)


@dataclass(frozen=True)
class SplitOutcome:
    first: Outcome  # half closed at the near target, no break-even
    second: Outcome  # half aimed at the far target, stop at break-even after the near target

    @property
    def r_net(self) -> float:
        return 0.5 * self.first.r_net + 0.5 * self.second.r_net

    @property
    def cost_r(self) -> float:
        return 0.5 * self.first.cost_r + 0.5 * self.second.cost_r

    @property
    def exit_kind(self) -> str:
        return f"{self.first.exit_kind}+{self.second.exit_kind}"


def simulate_split(
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    hold_ms: int,
    fees: Fees = Fees(),
    near_r: float = 1.0,
    far_r: float = 3.0,
    max_gap_ms: int = MAX_GAP_MS,
) -> SplitOutcome | None:
    """Variant E: two halves with the same entry, stop and risk unit. The first half's target is entry + near_r R
    (maker), no break-even; the second half's target is entry + far_r R, its stop moves to break-even (entry plus both
    taker fees, ``Fees.be_offset``) from the bar after the first bar whose high reaches entry + near_r R. A bar that
    touches both the stop and the near target closes both halves at the stop (the stop wins). Each half's costs are its
    own, so the total is 0.5 * R1 + 0.5 * R2 in the full trade's risk unit. None without a path or a positive risk."""
    one = simulate_stop_rule(
        path, entry_ms, entry, stop, r_target(entry, stop, near_r), hold_ms, RULE_NO_BE, fees=fees, max_gap_ms=max_gap_ms
    )
    if one is None:
        return None
    rule = StopRule("E2", be_r=near_r)
    two = simulate_stop_rule(path, entry_ms, entry, stop, r_target(entry, stop, far_r), hold_ms, rule, fees=fees, max_gap_ms=max_gap_ms)
    if two is None:  # same path and risk as the first half: cannot happen, kept for the type checker
        return None
    return SplitOutcome(one, two)


def simulate_atr_trail(
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    hold_ms: int,
    atr: float,
    fees: Fees = Fees(),
    mult: float = 3.0,
    act_r: float = 1.0,
    max_gap_ms: int = MAX_GAP_MS,
) -> Outcome | None:
    """Variant F: no target, no break-even. ``atr`` (the setup timeframe's ATR(14) at the signal bar) is fixed for the
    whole trade. The trail activates after the first 1h bar whose high reaches entry + act_r R; from the next bar the
    stop is max(current stop, HH - mult * atr), HH the highest high of the trade's bars closed before the current one,
    so the stop only tightens. A gap through the stop exits at the open, a touch at the stop; every stop exit and the
    time exit pay the taker fee plus slippage. None without a path, a positive risk or a positive finite ``atr``."""
    if entry - stop <= 0 or len(path) == 0 or not (math.isfinite(atr) and atr > 0):
        return None
    ru = risk_unit(entry, stop, fees)
    i0 = int(np.searchsorted(path.t, entry_ms))
    i1 = int(np.searchsorted(path.t, entry_ms + hold_ms))
    if i1 <= i0 or int(path.t[i0]) - entry_ms > max_gap_ms:
        return None
    cut = False
    holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
    if holes.size:
        i1, cut = i0 + int(holes[0]) + 1, True
    act = r_target(entry, stop, act_r)
    cur, how = stop, "stop"
    active = False
    hh = -math.inf
    move_ms = -1
    best = entry
    px, kind, j = float(path.c[i1 - 1]), ("data_end" if cut else "time"), i1 - 1
    for k in range(i0, i1):
        tk = int(path.t[k])
        if active:
            lvl = hh - mult * atr
            if lvl > cur:
                cur, how = lvl, "trail"
                move_ms = tk if move_ms < 0 else move_ms
        if path.o[k] <= cur:
            px, kind, j = float(path.o[k]), f"{how}_gap", k
            break
        if path.low[k] <= cur:
            px, kind, j = cur, how, k
            break
        hk = float(path.h[k])
        hh = max(hh, hk)
        best = max(best, hk)
        if not active and hk >= act:
            active = True
    if kind == "time" and i1 == len(path) and int(path.t[i1 - 1]) + path.bar_ms < entry_ms + hold_ms:
        kind = "data_end"
    gross = (px - entry) / ru
    cost = (fees.maker * abs(entry) + fees.taker_exit * abs(px)) / ru
    return Outcome(px, kind, move_ms >= 0, move_ms, gross, cost, (best - entry) / ru, entry_ms, int(path.t[j]) + path.bar_ms)


def mde80(se: float) -> float:
    """Minimum detectable effect at 80 % power and two-sided 5 % (2.8 standard errors)."""
    return 2.8 * se


def cap_per_bar(t_in: np.ndarray, rank: np.ndarray, cap: int) -> np.ndarray:
    """Mask keeping at most ``cap`` entries per signal bar (same ``t_in``), the lowest ``rank`` first (ties by
    position). No occupancy chain: the kept set does not depend on any exit."""
    keep = np.zeros(t_in.size, dtype=bool)
    order = np.lexsort((np.arange(t_in.size), rank, t_in))
    prev, cnt = None, 0
    for i in order:
        t = int(t_in[i])
        cnt = cnt + 1 if t == prev else 1
        prev = t
        keep[i] = cnt <= cap
    return keep
