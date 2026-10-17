"""Course basics (IDEA-20261016-course-setups-crypto, step 7): BOS by body vs by wick, equal highs/lows as a magnet,
retracement depth after a BOS (OTE), "the first test is the best", confluence at a level, and a deviation under a
range against a one-bar sweep.

Pure functions, no I/O. Everything is written for the long side ("long frame"); the bearish side is the same logic on
negated prices (``Bars.mirrored``). Bars are ``course_s14_ema.Bars`` (open times in ms UTC, sorted); ``pos`` is the
position of each bar inside its data segment (``course_s18_trend.pos_from_seg``), so swings and windows never cross a
hole in the data. Every detector is causal: an event decided at the close of bar t uses bars <= t only
(``tests/test_course_basics.py`` checks that events before t do not change when the bars after t change; the only
exception is ``range_events(..., lookahead_busy=True)``, kept to reproduce the registered step-7 run). Outcomes
(races, depth paths, trades) look forward by design.

Swings are strict 3-candle fractals (as ``markup.fractals``): a high above both neighbours' highs; it is known after
the close of the next bar, i.e. usable on bars >= f + 2 (as ``markup.structure``). Prices are in quote currency;
R is the return on a trade's planned risk, not on capital; fees are decimal fractions of price per side.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, MARKET_SIDE, MAX_GAP_MS, Bars

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "MARKET_SIDE",
    "BosEvent",
    "Leg",
    "Race",
    "RangeEvent",
    "bar_shapes",
    "bos_events",
    "depth_path",
    "eq_pools",
    "fractal_mask",
    "fractal_n_lows",
    "level_tests",
    "null_depth",
    "ote_legs",
    "race",
    "range_events",
    "trade_r",
    "untaken_fractal_lows_near",
]


# ---------------------------------------------------------------- swings


def fractal_mask(h: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """mask[f]: bar f is a strict 3-candle fractal high (bars f-1 .. f+1 in one segment). For lows pass -low."""
    n = h.size
    out = np.zeros(n, dtype=bool)
    if n < 3:
        return out
    f = np.arange(1, n - 1)
    ok = (pos[f + 1] >= 2) & (h[f] > h[f - 1]) & (h[f] > h[f + 1])
    out[f[ok]] = True
    return out


def fractal_n_lows(low: np.ndarray, pos: np.ndarray, n: int = 5) -> np.ndarray:
    """mask[f]: low f strictly below the n lows on each side, bars f-n .. f+n in one segment (known after f + n)."""
    m = low.size
    out = np.zeros(m, dtype=bool)
    for f in range(n, m - n):
        if pos[f + n] < 2 * n:
            continue
        w = low[f - n : f + n + 1]
        if low[f] < np.min(np.delete(w, n)):
            out[f] = True
    return out


# ---------------------------------------------------------------- races and trades


@dataclass(frozen=True)
class Race:
    outcome: int  # 1: up reached first; 0: down first (a bar touching both counts as down); -1: neither
    exit_px: float  # up, down (or the open if it gapped through down), else the last close; NaN without bars
    exit_ms: int


def race(path: Bars, start_ms: int, up: float, down: float, horizon_ms: int, max_gap_ms: int = MAX_GAP_MS) -> Race | None:
    """Which of ``up`` / ``down`` the path reaches first on bars opening in [start, start + horizon). A hole longer
    than ``max_gap_ms`` cuts the horizon. None when the path has no bar near ``start_ms``."""
    n = len(path)
    i0 = int(np.searchsorted(path.t, start_ms))
    if i0 >= n or int(path.t[i0]) - start_ms > max_gap_ms:
        return None
    i1 = int(np.searchsorted(path.t, start_ms + horizon_ms))
    if i1 > i0 + 1:
        holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
        if holes.size:
            i1 = i0 + int(holes[0]) + 1
    if i1 <= i0:
        return Race(-1, float("nan"), start_ms)
    lo = path.low[i0:i1] <= down
    hi = path.h[i0:i1] >= up
    d = int(np.argmax(lo)) if lo.any() else -1
    u = int(np.argmax(hi)) if hi.any() else -1
    if d >= 0 and (u < 0 or d <= u):
        j = i0 + d
        px = float(path.o[j]) if path.o[j] <= down else down
        return Race(0, px, int(path.t[j]) + path.bar_ms)
    if u >= 0:
        j = i0 + u
        return Race(1, up, int(path.t[j]) + path.bar_ms)
    j = i1 - 1
    return Race(-1, float(path.c[j]), int(path.t[j]) + path.bar_ms)


def trade_r(entry: float, stop: float, exit_px: float, fee_side: float = MARKET_SIDE) -> tuple[float, float]:
    """(R gross, cost in R) of a long entered at ``entry`` with planned risk entry - stop; market fees both sides."""
    risk = entry - stop
    if risk <= 0:
        raise ValueError("stop must be below the entry")
    return (exit_px - entry) / risk, fee_side * (abs(entry) + abs(exit_px)) / risk


# ---------------------------------------------------------------- H1: BOS by body vs by wick


@dataclass(frozen=True)
class BosEvent:
    t: int  # the bar whose high first exceeds the reference swing high (decision at its close)
    ref: int  # the reference fractal high
    level: float
    body: bool  # close above the level (BOS) vs wick only (fake BOS)
    opp: float  # the latest confirmed, still untaken fractal low (nan if none)
    opp_idx: int


def bos_events(b: Bars, pos: np.ndarray) -> list[BosEvent]:
    """First exits above the latest confirmed fractal high (a newer fractal replaces the older reference). The
    opposite swing is the latest confirmed fractal low not taken by any bar from its confirmation through t."""
    n = len(b)
    fh = fractal_mask(b.h, pos)
    fl = fractal_mask(-b.low, pos)
    out: list[BosEvent] = []
    ref = -1
    ref_taken = True
    lo_ref = -1
    lo_taken = True
    for t in range(n):
        if pos[t] == 0:
            ref, ref_taken, lo_ref, lo_taken = -1, True, -1, True
        if pos[t] >= 3:
            f = t - 2
            if fh[f]:
                ref, ref_taken = f, False
            if fl[f]:
                lo_ref, lo_taken = f, False
        if lo_ref >= 0 and not lo_taken and b.low[t] < b.low[lo_ref]:
            lo_taken = True
        if ref >= 0 and not ref_taken and b.h[t] > b.h[ref]:
            ref_taken = True
            lvl = float(b.h[ref])
            ok = lo_ref >= 0 and not lo_taken
            out.append(BosEvent(t, ref, lvl, bool(b.c[t] > lvl), float(b.low[lo_ref]) if ok else float("nan"), lo_ref if ok else -1))
    return out


# ---------------------------------------------------------------- H2: equal highs


def eq_pools(
    b: Bars, pos: np.ndarray, atr: np.ndarray, tol: float, lookback: int = 100
) -> tuple[list[tuple[int, int, int, float]], list[tuple[int, int, float]]]:
    """Equal-high pools and single swings at the confirmation bar t = f2 + 2 of each fractal high f2.

    Untaken fractal highs are tracked as in ``markup.structure`` (a bar whose high exceeds a swing removes it); f2 is
    added at t unless bar t already exceeds it. A pool: the latest untaken f1 with f2 - lookback <= f1 < f2 and
    h[f1] - h[f2] <= tol * atr[t] (h[f1] >= h[f2] because f1 survived f2); level L = h[f1]. Otherwise f2 is a single
    swing (level h[f2]). Returns (pools [(t, f1, f2, L)], singles [(t, f2, L)])."""
    n = len(b)
    fh = fractal_mask(b.h, pos)
    active: list[int] = []
    pools: list[tuple[int, int, int, float]] = []
    singles: list[tuple[int, int, float]] = []
    for t in range(n):
        if pos[t] == 0:
            active = []
        h = b.h[t]
        if active:
            active = [f for f in active if not h > b.h[f]]
        if pos[t] >= 3 and fh[t - 2]:
            f2 = t - 2
            if h > b.h[f2]:
                continue
            a = float(atr[t])
            partner = -1
            if np.isfinite(a) and a > 0:
                for f1 in reversed(active):
                    if f1 < f2 - lookback:
                        break
                    if b.h[f1] - b.h[f2] <= tol * a:
                        partner = f1
                        break
                if partner >= 0:
                    pools.append((t, partner, f2, float(b.h[partner])))
                else:
                    singles.append((t, f2, float(b.h[f2])))
            active.append(f2)
    return pools, singles


# ---------------------------------------------------------------- H3: OTE legs and depth


@dataclass(frozen=True)
class Leg:
    t: int  # BOS bar
    q: int  # decision bar: the bar after the leg's high fractal f_h (the high is confirmed at its close)
    hi: float
    lo: float
    d0: float  # retracement depth already seen on bar q


def ote_legs(b: Bars, pos: np.ndarray, atr: np.ndarray, events: list[BosEvent], min_atr: float = 2.0, max_ahead: int = 30) -> list[Leg]:
    """For each body BOS (ref f, bar t): Lo = min low f .. t; f_h = the first fractal high with index >= t (within
    ``max_ahead`` bars, same segment); Hi = max high t .. f_h; q = f_h + 1. Kept if Hi - Lo >= min_atr * atr[q] and
    low[q] > Lo."""
    n = len(b)
    fh = fractal_mask(b.h, pos)
    out: list[Leg] = []
    for e in events:
        if not e.body:
            continue
        t = e.t
        fhi = -1
        for k in range(t, min(n - 1, t + max_ahead)):
            if pos[k + 1] != pos[t] + (k + 1 - t):
                break
            if fh[k]:
                fhi = k
                break
        if fhi < 0:
            continue
        q = fhi + 1
        lo = float(b.low[e.ref : t + 1].min())
        hi = float(b.h[t : fhi + 1].max())
        a = float(atr[q])
        if not (np.isfinite(a) and a > 0) or hi - lo < min_atr * a or not b.low[q] > lo:
            continue
        out.append(Leg(t, q, hi, lo, float((hi - b.low[q]) / (hi - lo))))
    return out


def depth_path(
    path: Bars, start_ms: int, hi: float, lo: float, d0: float, horizon_ms: int, max_gap_ms: int = MAX_GAP_MS
) -> tuple[str, float] | None:
    """Walk the path from ``start_ms`` until a high above ``hi`` ("up"), a low below ``lo`` ("break", depth 1; a bar
    doing both counts as a break) or the horizon ("timeout"). Returns (outcome, max depth), depth = (hi - low) /
    (hi - lo) including the resolving bar's low and the starting depth ``d0``."""
    n = len(path)
    i0 = int(np.searchsorted(path.t, start_ms))
    if i0 >= n or int(path.t[i0]) - start_ms > max_gap_ms:
        return None
    i1 = int(np.searchsorted(path.t, start_ms + horizon_ms))
    if i1 > i0 + 1:
        holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
        if holes.size:
            i1 = i0 + int(holes[0]) + 1
    return _depth(path.h[i0:i1], path.low[i0:i1], hi, lo, d0)


def _depth(h: np.ndarray, low: np.ndarray, hi: float, lo: float, d0: float) -> tuple[str, float]:
    rng = hi - lo
    brk = low < lo
    up = h > hi
    kb = int(np.argmax(brk)) if brk.any() else -1
    ku = int(np.argmax(up)) if up.any() else -1
    if kb >= 0 and (ku < 0 or kb <= ku):
        return "break", 1.0
    end = ku if ku >= 0 else h.size - 1
    d = d0 if end < 0 else max(d0, float((hi - low[: end + 1].min()) / rng))
    return ("up" if ku >= 0 else "timeout"), d


def bar_shapes(b: Bars) -> np.ndarray:
    """Per bar (log(h/o), log(low/o), log(c/o)) in the bar's own frame (also valid on mirrored, negative prices)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.column_stack([np.log(b.h / b.o), np.log(b.low / b.o), np.log(b.c / b.o)])


def null_depth(
    shapes: np.ndarray,
    start_px: float,
    hi: float,
    lo: float,
    d0: float,
    n_steps: int,
    n_sims: int,
    rng: np.random.Generator,
    zones: tuple[tuple[float, float], ...],
) -> np.ndarray:
    """Bootstrap random walk: bars drawn with replacement from ``shapes`` (close drift removed), each opening at the
    previous close, from ``start_px``. Returns, per zone [a, b), the share of simulations whose pullback ended in the
    zone and then made a new high (outcome "up" with a <= depth < b), as in ``depth_path``."""
    sh = shapes[np.all(np.isfinite(shapes), axis=1)]
    lc = sh[:, 2] - sh[:, 2].mean()
    idx = rng.integers(0, sh.shape[0], size=(n_sims, n_steps))
    cum = np.cumsum(lc[idx], axis=1)
    o = start_px * np.exp(np.concatenate([np.zeros((n_sims, 1)), cum[:, :-1]], axis=1))
    h = o * np.exp(sh[idx, 0])
    low = o * np.exp(sh[idx, 1])
    out = np.zeros(len(zones))
    for s in range(n_sims):
        kind, d = _depth(h[s], low[s], hi, lo, d0)
        if kind == "up":
            for z, (a, b_) in enumerate(zones):
                if a <= d < b_:
                    out[z] += 1
    return out / n_sims


# ---------------------------------------------------------------- H8 / H9: tests of a level


def level_tests(
    b: Bars, pos: np.ndarray, f: int, atr_f: float, n: int = 5, life: int = 180, sep: float = 1.0
) -> list[tuple[int, int, int]]:
    """Tests of the support at low[f] (an n-fractal low, known after bar f + n). The level lives until bar f + life or
    the first close below it. It is armed on the first bar a >= f + n, after f (test 1) or after the previous test
    bar, with a high >= L + sep * atr_f; the next bar with low <= L is the test. Returns [(k, arm bar, test bar)]."""
    m = len(b)
    level = float(b.low[f])
    end = min(m - 1, f + life)
    out: list[tuple[int, int, int]] = []
    k = 0
    a = -1  # arming bar of the resting limit (-1: not armed)
    seen_sep = False  # a high >= L + sep * atr after f or after the previous test bar
    for j in range(f + 1, end + 1):
        if pos[j] != pos[f] + (j - f):
            break
        if a >= 0 and b.low[j] <= level:
            k += 1
            out.append((k, a, j))
            a, seen_sep = -1, False
        elif a < 0:
            if b.h[j] >= level + sep * atr_f:
                seen_sep = True
            if seen_sep and j >= f + n and b.c[j] >= level:
                a = j  # the limit rests from this bar's close
        if b.c[j] < level:
            break
    return out


def untaken_fractal_lows_near(b: Bars, fl: np.ndarray, a: int, level: float, band: float, lookback: int, exclude: int) -> bool:
    """An untaken confirmed 3-candle fractal low g (g + 2 <= a, a - lookback <= g, g != exclude) with
    |low[g] - level| <= band; untaken: no low below it on bars g + 1 .. a."""
    for g in range(max(1, a - lookback), a - 1):
        if g == exclude or not fl[g]:
            continue
        if abs(b.low[g] - level) <= band and b.low[g + 1 : a + 1].min() >= b.low[g]:
            return True
    return False


# ---------------------------------------------------------------- S16: deviation vs sweep


@dataclass(frozen=True)
class RangeEvent:
    kind: str  # "dev" | "sweep"
    s: int  # first bar below the range (dev) or the sweep bar
    r: int  # decision bar (first close back in the range)
    lr: float
    tr: float
    mid: float
    ld: float  # lowest low s .. r


def range_events(
    b: Bars,
    pos: np.ndarray,
    atr: np.ndarray,
    w: int = 30,
    width_atr: float = 6.0,
    kmin: int = 3,
    kmax: int = 10,
    *,
    lookahead_busy: bool = False,
) -> list[RangeEvent]:
    """Deviations (k in kmin .. kmax closes below the low of the w-bar range before s, then the first close back at or
    above it at r = s + k) and one-bar sweeps (low below the range low, close back at or above it). The range must be
    no wider than ``width_atr`` * atr[s - 1]; the event needs close[r] < the range middle.

    One deviation per episode, decided causally: candidates are taken in the order of their decision bar r, and a
    deviation whose start s is at or before the decision bar of the last kept deviation is dropped (whatever its middle
    test). Sweeps are decided on their own bar and are never dropped, except a sweep on the very bar where a kept
    deviation is decided (that bar belongs to the deviation). So an event decided at bar t depends on bars <= t only.

    ``lookahead_busy=True`` reproduces the rule of the registered step-7 run (kept only to reproduce it): after a
    deviation is found at s, bars s .. r start no event at all. Whether a sweep on a bar j in (s, r] is kept then
    depends on bars after j (the later close back in the range), i.e. it looks ahead."""
    if lookahead_busy:
        return _range_events_registered(b, pos, atr, w, width_atr, kmin, kmax)
    n = len(b)
    cands: list[tuple[int, int, int, RangeEvent | None]] = []  # (r, 0 dev | 1 sweep, s, event or None if close >= mid)
    for s in range(w, n):
        if pos[s] < w:
            continue
        a = float(atr[s - 1])
        if not (np.isfinite(a) and a > 0):
            continue
        lr = float(b.low[s - w : s].min())
        tr = float(b.h[s - w : s].max())
        if tr - lr > width_atr * a:
            continue
        mid = (tr + lr) / 2
        if b.low[s] < lr and b.c[s] >= lr:
            if b.c[s] < mid:
                cands.append((s, 1, s, RangeEvent("sweep", s, s, lr, tr, mid, float(b.low[s]))))
            continue
        if not b.c[s] < lr:
            continue
        k = 1
        r = -1
        while s + k < n and k <= kmax and pos[s + k] == pos[s] + k:
            if b.c[s + k] >= lr:
                r = s + k
                break
            k += 1
        if r < 0 or k < kmin:
            continue
        ev = RangeEvent("dev", s, r, lr, tr, mid, float(b.low[s : r + 1].min())) if b.c[r] < mid else None
        cands.append((r, 0, s, ev))
    cands.sort(key=lambda x: (x[0], x[1], x[2]))
    out: list[RangeEvent] = []
    last_dev_r = -1
    for r, kind, s, ev in cands:
        if kind == 0:
            if s <= last_dev_r:
                continue
            last_dev_r = r
        elif s == last_dev_r:
            continue
        if ev is not None:
            out.append(ev)
    return out


def _range_events_registered(b: Bars, pos: np.ndarray, atr: np.ndarray, w: int, width_atr: float, kmin: int, kmax: int) -> list[RangeEvent]:
    n = len(b)
    out: list[RangeEvent] = []
    busy = -1  # bars s .. r of the last deviation start no new event (looks ahead for bars s + 1 .. r)
    for s in range(w, n):
        if pos[s] < w or s <= busy:
            continue
        a = float(atr[s - 1])
        if not (np.isfinite(a) and a > 0):
            continue
        lr = float(b.low[s - w : s].min())
        tr = float(b.h[s - w : s].max())
        if tr - lr > width_atr * a:
            continue
        mid = (tr + lr) / 2
        if b.low[s] < lr and b.c[s] >= lr:
            if b.c[s] < mid:
                out.append(RangeEvent("sweep", s, s, lr, tr, mid, float(b.low[s])))
            continue
        if not b.c[s] < lr:
            continue
        k = 1
        r = -1
        while s + k < n and k <= kmax and pos[s + k] == pos[s] + k:
            if b.c[s + k] >= lr:
                r = s + k
                break
            k += 1
        if r < 0 or k < kmin:
            continue
        busy = r
        if b.c[r] < mid:
            out.append(RangeEvent("dev", s, r, lr, tr, mid, float(b.low[s : r + 1].min())))
    return out
