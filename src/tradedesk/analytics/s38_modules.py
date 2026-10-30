"""S38 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b): pure helpers for the registered modules of the S38 short
(4h range breakdown + retest): H10 (half at 1R, the rest to the measured move or under a structural trail), H1
(displacement on the breakout bar), H4 (breakout to a new 180-bar low), H5 (half taken just above the nearest liquidity
pool on the way to the 2R target) and H6 (a "clean" retest bar), plus the cross-coin placebo draw. Registration: the
card's section «Попередня реєстрація модулів S38».

Features (H1, H4, H6, H5 levels) are computed on real (short-side) prices and read only bars that close at or before
the entry time ``t_in`` (the close of the retest bar j; H1 / H4 read bars <= the breakout bar s only). The trade
simulations work in the long price space on mirrored bars, as ``simulate_stop_rule`` does. No I/O; times are ms UTC.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence

import numpy as np

from tradedesk.analytics.course_s14_ema import MAX_GAP_MS, Bars
from tradedesk.analytics.s3_modules import htf_lows
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, risk_unit, simulate_stop_rule

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
NO_BE = StopRule("fixed")


# ---------------------------------------------------------------- features (real prices, short side)


def displacement(b: Bars, s: int, level: float, atr_prev: float, below_atr: float = 0.5, close_frac: float = 0.25) -> bool | None:
    """H1: the breakout bar ``s`` closed at least ``below_atr`` ATR below the broken ``level`` and in the lowest
    ``close_frac`` of its own range. ``atr_prev`` is ATR14 of bar s - 1. Reads bar s only. None without an ATR."""
    if not (np.isfinite(atr_prev) and atr_prev > 0):
        return None
    c, lo, hi = float(b.c[s]), float(b.low[s]), float(b.h[s])
    return bool(level - c >= below_atr * atr_prev and c - lo <= close_frac * (hi - lo))


def new_low(b: Bars, pos: np.ndarray, s: int, n: int = 180) -> bool | None:
    """H4: the low of bar ``s`` is below the lowest low of the ``n`` bars before it (same segment). None if the
    segment has fewer than ``n`` bars before s. Reads bars s - n .. s."""
    if int(pos[s]) < n:
        return None
    return bool(float(b.low[s]) < float(np.min(b.low[s - n : s])))


def retest_quality(b: Bars, s: int, j: int, level: float, atr_j: float, max_bars: int = 2, wick_atr: float = 0.5) -> dict[str, bool] | None:
    """H6 parts of the retest bar ``j`` (short): fast (j - s <= max_bars), close in the lower third of its range,
    upper wick >= body, high <= level + wick_atr * ATR(j); ``clean`` = all four. Reads bars s and j only."""
    if not (np.isfinite(atr_j) and atr_j > 0):
        return None
    o, h, lo, c = float(b.o[j]), float(b.h[j]), float(b.low[j]), float(b.c[j])
    parts = {
        "fast": j - s <= max_bars,
        "low_third": (c - lo) * 3.0 <= (h - lo),
        "wick": h - max(o, c) >= abs(c - o),
        "shallow": h <= level + wick_atr * atr_j,
    }
    parts["clean"] = all(parts.values())
    return parts


def fractal_lows(b: Bars, k: int = 2) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Strict fractal lows of ``b`` (lower than ``k`` bars on each side): (pivot open ms, confirmation close ms =
    close of bar f + k, low)."""
    low = b.low
    m = low.size
    if m < 2 * k + 1:
        z = np.zeros(0, dtype=np.int64)
        return z, z, np.zeros(0)
    core = low[k : m - k]
    ok = np.ones(core.size, dtype=bool)
    for d in range(1, k + 1):
        ok &= low[k - d : m - k - d] > core
        ok &= low[k + d : m - k + d] > core
    f = np.flatnonzero(ok) + k
    return b.t[f].astype(np.int64), (b.t[f + k] + b.bar_ms).astype(np.int64), low[f].astype(float)


def pool_levels(
    h1: Bars,
    fractals: tuple[np.ndarray, np.ndarray, np.ndarray],
    t_in: int,
    low_days: int = 90,
    swing_days: int = 30,
    min_cover: float = 0.5,
) -> list[float]:
    """H5 liquidity levels below price known at ``t_in`` (real prices): lows of the previous closed UTC day, ISO week
    and calendar month (``s3_modules.htf_lows``), the lowest 1h low of the ``low_days`` days before ``t_in`` (with at
    least ``min_cover`` of the hours present) and the fractal lows (``fractal_lows`` of the 4h bars) confirmed by
    ``t_in`` whose pivot opened in the last ``swing_days`` days. Reads 1h bars opening before ``t_in`` only."""
    out = [v for v in htf_lows(h1.t, h1.low, t_in).values() if np.isfinite(v)]
    a = t_in - low_days * DAY_MS
    i0, i1 = int(np.searchsorted(h1.t, a)), int(np.searchsorted(h1.t, t_in - h1.bar_ms, side="right"))
    if i1 > i0 and (i1 - i0) >= min_cover * low_days * 24:
        out.append(float(np.min(h1.low[i0:i1])))
    piv, conf, lv = fractals
    m = (conf <= t_in) & (piv >= t_in - swing_days * DAY_MS)
    out.extend(float(x) for x in lv[m])
    return out


def pool_on_path(levels: Sequence[float], entry: float, target: float) -> float:
    """Nearest level strictly between the short's 2R ``target`` and its ``entry`` (the highest such), NaN if none."""
    inside = [v for v in levels if target < v < entry]
    return max(inside) if inside else math.nan


def take_price(level: float, atr_j: float, entry: float, buf_atr: float = 0.1) -> float:
    """H5 partial take (short): ``buf_atr`` ATR above the pool level, NaN if that is not below the entry."""
    if not (np.isfinite(level) and np.isfinite(atr_j)):
        return math.nan
    p = level + buf_atr * atr_j
    return p if p < entry else math.nan


def measured_move(range_low: float, range_high: float, target: float) -> float:
    """H10-MM runner target (short, real price): the measured move below the broken low if it is beyond the 2R target,
    else the 2R target."""
    return min(target, range_low - (range_high - range_low))


def trail_levels(b4m: Bars) -> tuple[np.ndarray, np.ndarray]:
    """H10-TR in the long (mirrored) space: at the close of each 4h bar f >= 1, the stop level min(low[f - 1], low[f])
    (for the short: above the highs of the last two closed 4h bars). Returns (close ms of f, level)."""
    if len(b4m) < 2:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    lv = np.minimum(b4m.low[:-1], b4m.low[1:]).astype(float)
    return (b4m.t[1:] + b4m.bar_ms).astype(np.int64), lv


# ---------------------------------------------------------------- trade pieces (long space, mirrored for a short)


def fixed_half(path: Bars, t_in: int, entry: float, stop: float, target: float, hold_ms: int, fees: Fees) -> Outcome | None:
    """A half of the position with its own limit target, the base stop and no break-even (H10 at 1R, H5 at the pool).
    Same entry and stop as the base, so its R is in the base risk unit."""
    return simulate_stop_rule(path, t_in, entry, stop, target, hold_ms, NO_BE, fees=fees)


def mm_runner(path: Bars, t_in: int, entry: float, stop: float, far: float, hold_ms: int, fees: Fees) -> Outcome | None:
    """H10-MM rest: target ``far``, break-even (with fees, from the next bar) after a 1R high."""
    rule = StopRule("H10-MM", be_frac=(entry - stop) / (far - entry))
    return simulate_stop_rule(path, t_in, entry, stop, far, hold_ms, rule, fees=fees)


def trail_runner(
    path: Bars,
    t_in: int,
    entry: float,
    stop: float,
    hold_ms: int,
    trail_ms: np.ndarray,
    trail_lv: np.ndarray,
    fees: Fees,
    be_r: float = 1.0,
    max_gap_ms: int = MAX_GAP_MS,
) -> Outcome | None:
    """H10-TR rest (long space): no target; the base stop until a high >= entry + ``be_r`` R, then from the next 1h bar
    the break-even stop (entry plus both taker fees) and, at every bar, the latest trail level whose 4h bar closed at
    or before that bar's open (``trail_levels``) if it is higher (the stop only tightens). Exits by the stop or at the
    end of ``hold_ms`` (taker); the same gap rule as ``simulate_stop_rule``."""
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
    trig = entry + be_r * (entry - stop)
    be_stop = entry + fees.be_offset * abs(entry)
    cur, how, armed, live, move_ms, best = stop, "stop", False, False, -1, entry
    px, kind, j = float(path.c[i1 - 1]), ("data_end" if cut else "time"), i1 - 1
    for k in range(i0, i1):
        tk = int(path.t[k])
        if armed:
            if not live:
                live = True
                if be_stop > cur:
                    cur, how, move_ms = be_stop, "be", tk
            q = int(np.searchsorted(trail_ms, tk, side="right")) - 1
            if q >= 0 and trail_lv[q] > cur:
                cur, how = float(trail_lv[q]), "trail"
                move_ms = tk if move_ms < 0 else move_ms
        if path.o[k] <= cur:
            px, kind, j = float(path.o[k]), f"{how}_gap", k
            break
        if path.low[k] <= cur:
            px, kind, j = cur, how, k
            break
        best = max(best, float(path.h[k]))
        if not armed and path.h[k] >= trig:
            armed = True
    if kind == "time" and i1 == len(path) and int(path.t[i1 - 1]) + path.bar_ms < t_in + hold_ms:
        kind = "data_end"
    gross = (px - entry) / ru
    cost = (fees.maker * abs(entry) + fees.taker_exit * abs(px)) / ru
    return Outcome(px, kind, move_ms >= 0, move_ms, gross, cost, (best - entry) / ru, t_in, int(path.t[j]) + path.bar_ms)


def halves(first: Outcome | None, rest: Outcome | None) -> float:
    """R of a position split into two equal halves with the same entry and stop (NaN if a half has no path)."""
    if first is None or rest is None:
        return math.nan
    return 0.5 * first.r_net + 0.5 * rest.r_net


# ---------------------------------------------------------------- cross-coin placebo


def draw_coins(key: str, pool: Sequence[str], k: int) -> list[str]:
    """``k`` distinct coins from ``pool`` (sorted, without the signal's coin) by a deterministic hash of the trade key:
    stable across runs and Python versions."""
    cand = sorted(pool)
    out: list[str] = []
    salt = 0
    while len(out) < min(k, len(cand)):
        h = hashlib.sha256(f"plc|{key}|{salt}".encode()).digest()
        c = cand[int.from_bytes(h[:8], "big") % len(cand)]
        if c not in out:
            out.append(c)
        salt += 1
    return out


def placebo_levels(entry: float, stop_frac: float, target_r: float = 2.0) -> tuple[float, float, float]:
    """Short geometry moved to another coin (real prices): the same stop distance in % of the entry, target at
    ``target_r`` R. Returns (entry, stop, target)."""
    st = entry * (1.0 + stop_frac)
    return entry, st, entry - target_r * (st - entry)
