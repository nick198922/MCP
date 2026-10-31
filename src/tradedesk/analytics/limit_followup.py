"""Follow-up of a filled resting limit (IDEA-20261030-demo-setups-depth, item 10d; registration «Попередня
реєстрація 10d»): exit at the close of the 6th 1h bar after the fill bar unless the price confirmed.

Pure functions, no I/O; prices in the long (or mirrored) space of the trade:

* ``confirm_bar`` - the first 1h bar m opening in [t_k + 1 h, t_k + ``max_wait`` h] whose close is above the level
  and above the fill bar's high (the S28 H6 confirmation, without a new choice of parameters).
* ``followup_trade`` - the base limit trade (``setup_passport.limit_trade``) and the same trade with the follow-up:
  without a confirmation, a trade still open after the close of the window's last bar exits there (time exit, taker).
* ``placebo_contrast`` - per-trade values whose mean is the module's gain minus the expected gain of the
  same-frequency placebo (the same number of 6th-hour exits drawn at random among the trades open at that time).

The decision at the window's end uses bars up to that close only (``tests/test_limit_followup.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .course_s14_ema import Bars
from .setup_passport import limit_trade
from .stop_management import Fees, Outcome, StopRule, simulate_stop_rule


def confirm_bar(path: Bars, k: int, level: float, max_wait: int = 6) -> int:
    """Index of the first bar opening in [t_k + 1 bar, t_k + ``max_wait`` bars] with close > ``level`` and close >
    high[k]; -1 if none. Reads bars k .. the window's end only."""
    tk = int(path.t[k])
    i0 = int(np.searchsorted(path.t, tk + path.bar_ms))
    i1 = int(np.searchsorted(path.t, tk + (max_wait + 1) * path.bar_ms))
    c = path.c[i0:i1]
    hit = np.flatnonzero((c > level) & (c > path.h[k]))
    return i0 + int(hit[0]) if hit.size else -1


@dataclass(frozen=True)
class FollowUp:
    """A filled limit: the base outcome, the module's outcome, and how the module decided."""

    k: int  # fill bar index in the path
    base: Outcome
    mod: Outcome
    m: int  # confirmation bar index, -1 if none in the window
    open_at_end: bool  # the base trade was still open after the close of the window's last bar
    early: bool  # the module exited at the window's end (open_at_end and no confirmation)
    trunc: Outcome  # the trade held only to the window's end (= base when no window)


def followup_trade(
    path: Bars,
    t0: int,
    t1: int,
    level: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    fees: Fees,
    max_wait: int = 6,
) -> FollowUp | None:
    """The base limit trade of ``setup_passport.limit_trade`` (fill when a 1h low trades strictly below ``level``
    in [t0, t1), pessimistic fill hour) and the follow-up module: if no bar of k + 1 .. k + ``max_wait`` confirms
    (``confirm_bar``) and the trade is still open after the close of the last of them, it exits at that close by a
    taker time exit; otherwise the module trade is the base trade. None if never filled or without a path.

    A fill bar that also reaches the stop, or a fill without bars in the window, leaves the module equal to the base."""
    base, tk = limit_trade(path, t0, t1, level, stop, target, hold_ms, rule, fees)
    if base is None:
        return None
    k = int(np.searchsorted(path.t, tk))
    t_end = tk + (max_wait + 1) * path.bar_ms
    if path.low[k] <= stop or k + 1 >= len(path) or int(path.t[k + 1]) >= t_end:
        return FollowUp(k, base, base, -1, False, False, base)
    t_next = int(path.t[k + 1])
    trunc = simulate_stop_rule(path, t_next, level, stop, target, t_end - t_next, rule, fees=fees)
    if trunc is None:
        return FollowUp(k, base, base, -1, False, False, base)
    open_at_end = trunc.exit_kind == "time"
    m = confirm_bar(path, k, level, max_wait)
    early = open_at_end and m < 0
    return FollowUp(k, base, trunc if early else base, m, open_at_end, early, trunc)


def placebo_contrast(d: np.ndarray, early: np.ndarray, open_at_end: np.ndarray) -> np.ndarray:
    """Per-trade X_i = d_i * (early_i - q), d = R held to the window's end - R base, q = share of early exits among
    the trades open at the window's end. mean(X) = mean(d * early) - q * mean(d * open): the module's gain minus the
    expected gain of exiting the same number of open trades, drawn at random, at the window's end."""
    d = np.asarray(d, dtype=float)
    e = np.asarray(early, dtype=bool)
    o = np.asarray(open_at_end, dtype=bool)
    q = float(e.sum()) / float(o.sum()) if o.any() else 0.0
    return np.where(o, d * (e.astype(float) - q), 0.0)
