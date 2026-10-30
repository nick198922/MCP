"""S39 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b; registration «Попередня реєстрація модулів S39»).

Pure functions, no I/O, real prices unless said otherwise:

* ``failed_long`` - the "failed long" detector: a parent long (signal bar r, its stop and target) whose stop is closed
  through within ``max_wait`` bars of the same segment before the parent's target is touched gives a short at that
  close (stop over the highest high since r plus ``stop_atr`` ATR14, target ``target_r`` R). With the S3 longs of
  ``setup_signals.range_all`` as parents it is exactly ``setup_signals.s39_all`` (H2 runs it on 1h / 2h bars, H8 with
  the S14 / S16 / S23 longs as parents).
* ``structural_stop`` - H3: the stop of an S39 short whose rule stop is too wide, moved to the broken range low.
* ``thesis_exit`` - H4: the base trade plus a market exit at the first signal-timeframe close back above a level.
* ``non_htf_edge`` - H7: the swept range low is not within 0.3 ATR of the previous day / ISO week / month low.
* ``permuted`` - the own placebos of H3 / H4: a value taken from another trade (a seeded permutation).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from tradedesk.analytics import s3_modules as mo
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.stop_management import MAX_GAP_MS, Fees, Outcome, StopRule, risk_unit


@dataclass(frozen=True)
class Parent:
    """A parent long: its signal bar index, stop and target (real prices), and the setup it comes from."""

    r: int
    stop: float
    target: float
    tag: str = "S3"


@dataclass(frozen=True)
class Fail:
    """A failed-long short: parent bar r, failure (entry) bar j, entry / stop / target (real prices), ATR14(j)."""

    r: int
    j: int
    entry: float
    stop: float
    target: float
    atr: float
    tag: str


def failed_long(
    b: Bars,
    pos: np.ndarray,
    atr: np.ndarray,
    parents: Sequence[Parent],
    *,
    max_wait: int = 6,
    stop_atr: float = 0.5,
    target_r: float = 2.0,
    used: set[int] | None = None,
) -> list[Fail]:
    """Shorts on the failure of each parent long, in parent order (as ``short_setups.failed_sweep`` in real prices):
    for bars j = r + 1 .. r + max_wait of the parent's segment, the first close below the parent's stop is the
    failure (one short per bar j; a bar already in ``used`` - updated in place - gives none); a high at or above the
    parent's target first ends the search. Causal: the short on bar j reads bars r .. j only."""
    taken: set[int] = set() if used is None else used
    n = len(b)
    out: list[Fail] = []
    for p in sorted(parents, key=lambda x: x.r):
        r = p.r
        if not (0 <= r < n):
            continue
        for j in range(r + 1, min(n, r + max_wait + 1)):
            if pos[j] != pos[r] + (j - r):
                break
            if b.c[j] < p.stop:
                aj = float(atr[j])
                if j not in taken and np.isfinite(aj) and aj > 0:
                    entry = float(b.c[j])
                    stop = float(b.h[r : j + 1].max()) + stop_atr * aj
                    risk = stop - entry
                    if risk > 0:
                        taken.add(j)
                        out.append(Fail(r, j, entry, stop, entry - target_r * risk, aj, p.tag))
                break
            if b.h[j] >= p.target:
                break
    out.sort(key=lambda x: (x.j, x.r))
    return out


def structural_stop(entry: float, base_stop: float, range_low: float, atr_j: float, buf_atr: float = 0.5) -> float:
    """H3: the short's stop at the broken range low plus ``buf_atr`` ATR, never further than the base stop."""
    return min(base_stop, range_low + buf_atr * atr_j)


def thesis_level(range_low: float, atr_j: float, k_atr: float = 0.25) -> float:
    """H4: a signal-timeframe close above this level cancels the 'failed sweep' thesis (back inside the range)."""
    return range_low + k_atr * atr_j


def thesis_exit(
    path: Bars,
    t_in: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    level: float,
    tf_ms: int,
    fees: Fees = Fees(),
    max_gap_ms: int = MAX_GAP_MS,
) -> Outcome | None:
    """H4 in the long (mirrored) space: ``simulate_stop_rule`` with ``rule`` (break-even trigger, target, hard stop,
    time exit, gap rule unchanged) plus a market exit at the close of the first 1h bar that ends a ``tf_ms`` bar
    (close time a multiple of ``tf_ms``) at or below ``level``. Inside a 1h bar the stop / target come first, then the
    close test. With ``level`` = -inf it equals ``simulate_stop_rule``. The decision at a close reads bars up to it."""
    if entry - stop <= 0 or len(path) == 0:
        return None
    ru = risk_unit(entry, stop, fees)
    i0 = int(np.searchsorted(path.t, t_in))
    i1 = int(np.searchsorted(path.t, t_in + hold_ms))
    if i1 <= i0 or int(path.t[i0]) - t_in > max_gap_ms:
        return None
    cut = False
    holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
    if holes.size:
        i1, cut = i0 + int(holes[0]) + 1, True
    trig = rule.trigger(entry, stop, target)
    be_stop = entry + fees.be_offset * abs(entry)
    cur, how, armed, be_done, move_ms, best = stop, "stop", False, False, -1, entry
    px, kind, j = float(path.c[i1 - 1]), ("data_end" if cut else "time"), i1 - 1
    for k in range(i0, i1):
        tk = int(path.t[k])
        if armed and not be_done:
            be_done = True
            if be_stop > cur:
                cur, how = be_stop, "be"
                move_ms = tk if move_ms < 0 else move_ms
        if path.o[k] <= cur:
            px, kind, j = float(path.o[k]), f"{how}_gap", k
            break
        if path.low[k] <= cur:
            px, kind, j = cur, how, k
            break
        if path.h[k] >= target:
            px, kind, j = float(target), "target", k
            best = max(best, float(target))
            break
        best = max(best, float(path.h[k]))
        if trig is not None and not armed and path.h[k] >= trig:
            armed = True
        if (tk + path.bar_ms) % tf_ms == 0 and path.c[k] <= level:
            px, kind, j = float(path.c[k]), "thesis", k
            break
    if kind == "time" and i1 == len(path) and int(path.t[i1 - 1]) + path.bar_ms < t_in + hold_ms:
        kind = "data_end"
    fee_out = fees.maker if kind == "target" else fees.taker_exit
    gross = (px - entry) / ru
    cost = (fees.maker * abs(entry) + fee_out * abs(px)) / ru
    return Outcome(px, kind, move_ms >= 0, move_ms, gross, cost, (best - entry) / ru, t_in, int(path.t[j]) + path.bar_ms)


def non_htf_edge(
    h_t: np.ndarray, h_low: np.ndarray, sweep_open_ms: int, range_low: float, atr_prev: float, tol_atr: float = 0.3
) -> bool | None:
    """H7: True if none of the previous closed UTC day / ISO week / calendar month lows (1h bars, periods closed at or
    before the open of the sweep bar) lies within ``tol_atr * atr_prev`` of the range low; None without levels."""
    lv = mo.htf_lows(h_t, h_low, sweep_open_ms)
    hit, dist = mo.htf_match(range_low, lv, atr_prev, tol_atr)
    return None if not np.isfinite(dist) else not hit


def permuted(values: Sequence[float], seed: int = 20261030) -> np.ndarray:
    """``values`` in a seeded random order: each trade gets the value of another (the own placebos of H3 / H4)."""
    v = np.asarray(values, dtype=float)
    return v[np.random.default_rng(seed).permutation(v.size)]


def price_return(r_net: float, entry: float, stop: float, fees: Fees = Fees()) -> float:
    """R of a trade expressed as a fraction of the entry price (the long-space risk unit over |entry|)."""
    return r_net * risk_unit(entry, stop, fees) / abs(entry)
