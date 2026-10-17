"""Course setup S14 / H5 (IDEA-20261016-course-setups-crypto, step 1): a test of the trend EMA ribbon (13/21/34)
after an impulse, traded with the trend, and the "lost EMA" event H5.

Pure functions, no I/O. Bars are numpy arrays of open times (ms UTC, sorted, unique) and OHLC prices. Every
indicator is causal: the value at bar i uses bars <= i only, so a signal decided at the close of bar i cannot change
when bars after i change (``tests/test_course_s14_ema.py`` checks this). Trade outcomes look forward by design.

Longs are implemented once; a short is the same logic on negated prices (``Bars.mirrored``), which mirrors the
EMAs, the impulse, the zone, the stop and the target exactly. Fees are decimal fractions of price per side
(0.0007 = 0.05 % taker + 0.02 % slippage) converted to R with the trade's own stop distance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
TAKER = 0.0005
SLIPPAGE = 0.0002
MARKET_SIDE = TAKER + SLIPPAGE  # 0.07 % per market side, 0.14 % per round trip
MAX_GAP_MS = 48 * HOUR_MS  # a longer hole in the data restarts the indicators


@dataclass(frozen=True)
class Bars:
    t: np.ndarray  # open time, ms
    o: np.ndarray
    h: np.ndarray
    low: np.ndarray
    c: np.ndarray
    bar_ms: int

    @classmethod
    def from_rows(cls, rows: Any, bar_ms: int) -> Bars:
        a = np.asarray(rows, dtype=float)
        if a.size == 0:
            z = np.zeros(0)
            return cls(z.astype(np.int64), z, z, z, z, bar_ms)
        return cls(a[:, 0].astype(np.int64), a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy(), a[:, 4].copy(), bar_ms)

    def __len__(self) -> int:
        return int(self.t.size)

    def slice(self, i: int, j: int) -> Bars:
        return Bars(self.t[i:j], self.o[i:j], self.h[i:j], self.low[i:j], self.c[i:j], self.bar_ms)

    def mirrored(self) -> Bars:
        """Negated prices: a short setup becomes a long one."""
        return Bars(self.t, -self.o, -self.low, -self.h, -self.c, self.bar_ms)

    def close_time(self, i: int) -> int:
        return int(self.t[i]) + self.bar_ms


def resample(b: Bars, hours: int) -> Bars:
    """Aggregate 1h bars into ``hours``-hour bars aligned to 00:00 UTC. A bucket missing any hour is dropped."""
    step = hours * HOUR_MS
    if len(b) == 0:
        return Bars(b.t, b.o, b.h, b.low, b.c, step)
    key = b.t // step
    starts = np.flatnonzero(np.concatenate([[True], key[1:] != key[:-1]]))
    counts = np.diff(np.concatenate([starts, [key.size]]))
    ends = starts + counts - 1
    full = counts == hours
    s, e = starts[full], ends[full]
    h = np.maximum.reduceat(b.h, starts)[full]
    lo = np.minimum.reduceat(b.low, starts)[full]
    return Bars(key[s] * step, b.o[s], h, lo, b.c[e], step)


def segment_pos(t: np.ndarray, max_gap_ms: int = MAX_GAP_MS) -> np.ndarray:
    """Position of each bar inside its segment; a segment restarts after a gap between bar opens > ``max_gap_ms``."""
    n = t.size
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    brk = np.concatenate([[True], np.diff(t) > max_gap_ms])
    idx = np.arange(n)
    start = np.maximum.accumulate(np.where(brk, idx, 0))
    return idx - start


def ema(x: np.ndarray, n: int, pos: np.ndarray) -> np.ndarray:
    """EMA seeded with the simple mean of a segment's first ``n`` values (TradingView style); NaN before."""
    out = np.full(x.size, np.nan)
    k = 2.0 / (n + 1)
    prev = np.nan
    acc = 0.0
    for i in range(x.size):
        p = pos[i]
        if p == 0:
            acc = 0.0
            prev = np.nan
        if p < n:
            acc += x[i]
            if p == n - 1:
                prev = acc / n
                out[i] = prev
            continue
        prev = x[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(b: Bars, n: int, pos: np.ndarray) -> np.ndarray:
    """Wilder ATR; the first bar of a segment has no true range, the seed is the mean of the next ``n`` ranges."""
    out = np.full(len(b), np.nan)
    prev = np.nan
    acc = 0.0
    for i in range(len(b)):
        p = pos[i]
        if p == 0:
            acc = 0.0
            prev = np.nan
            continue
        pc = b.c[i - 1]
        tr = max(b.h[i] - b.low[i], abs(b.h[i] - pc), abs(b.low[i] - pc))
        if p <= n:
            acc += tr
            if p == n:
                prev = acc / n
                out[i] = prev
            continue
        prev = (prev * (n - 1) + tr) / n
        out[i] = prev
    return out


@dataclass(frozen=True)
class Params:
    fast: int = 13
    mid: int = 21
    slow: int = 34
    atr_n: int = 14
    trend_bars: int = 10  # closes in a row above the mid EMA
    impulse_bars: int = 20
    impulse_atr: float = 3.0
    stop_atr: float = 0.5
    min_target_r: float = 1.5
    fallback_r: float = 2.0
    warmup: int = 100
    hold_ms: int = 7 * DAY_MS
    max_gap_ms: int = MAX_GAP_MS


@dataclass(frozen=True)
class Ind:
    pos: np.ndarray
    e_fast: np.ndarray
    e_mid: np.ndarray
    e_slow: np.ndarray
    atr: np.ndarray
    above_run: np.ndarray  # closes in a row (this bar included) above the mid EMA of their own bar
    hi_idx: np.ndarray  # index of the highest high of bars i-impulse_bars .. i-1 (first on ties), -1 if undefined
    touch: np.ndarray  # low[i] <= e_fast[i-1]


def indicators(b: Bars, p: Params) -> Ind:
    pos = segment_pos(b.t, p.max_gap_ms)
    ef, em, es = ema(b.c, p.fast, pos), ema(b.c, p.mid, pos), ema(b.c, p.slow, pos)
    a = atr(b, p.atr_n, pos)
    above = np.zeros(len(b), dtype=np.int64)
    run = 0
    for i in range(len(b)):
        if pos[i] == 0:
            run = 0
        run = run + 1 if (np.isfinite(em[i]) and b.c[i] > em[i]) else 0
        above[i] = run
    hi = np.full(len(b), -1, dtype=np.int64)
    w = p.impulse_bars
    if len(b) > w:
        win = sliding_window_view(b.h, w)[:-1]  # row r = bars r .. r+w-1, used by bar r+w
        hi[w:] = np.argmax(win, axis=1) + np.arange(len(b) - w)
    touch = np.zeros(len(b), dtype=bool)
    if len(b) > 1:
        with np.errstate(invalid="ignore"):
            touch[1:] = b.low[1:] <= ef[:-1]
    return Ind(pos, ef, em, es, a, above, hi, touch)


def trend_ok(ind: Ind, k: int, p: Params) -> bool:
    """Trend at bar k: >= trend_bars closes in a row above the mid EMA and the mid EMA rising."""
    if k < 1 or ind.pos[k] < 1 or ind.above_run[k] < p.trend_bars:
        return False
    return bool(ind.e_mid[k] > ind.e_mid[k - 1])


@dataclass(frozen=True)
class Signal:
    idx: int  # signal bar; entry at its close
    kind: str  # touch | sweep | touch_no_trend | placebo
    hi_idx: int
    entry: float
    stop: float
    target: float
    high_ref: float  # the impulse high H
    target_kind: str  # high | fixed

    @property
    def risk(self) -> float:
        return self.entry - self.stop


def stop_target(b: Bars, ind: Ind, p: Params, i: int, high_ref: float) -> tuple[float, float, str] | None:
    """Stop 0.5 ATR(i) under the bar's low; target the impulse high if >= 1.5R away, otherwise 2R."""
    a = ind.atr[i]
    if not np.isfinite(a) or a <= 0:
        return None
    entry = float(b.c[i])
    stop = float(b.low[i]) - p.stop_atr * float(a)
    risk = entry - stop
    if risk <= 0:
        return None
    if high_ref - entry >= p.min_target_r * risk:
        return stop, float(high_ref), "high"
    return stop, entry + p.fallback_r * risk, "fixed"


def _impulse(b: Bars, ind: Ind, p: Params, i: int, atr_at: int) -> tuple[int, float] | None:
    k = int(ind.hi_idx[i])
    if k < 0 or not np.isfinite(ind.atr[atr_at]):
        return None
    high = float(b.h[k])
    low = float(b.low[i - p.impulse_bars : k + 1].min())
    if high - low < p.impulse_atr * float(ind.atr[atr_at]):
        return None
    return k, high


def _ready(ind: Ind, p: Params, i: int) -> bool:
    return bool(ind.pos[i] >= max(p.warmup, p.impulse_bars + 2) and np.isfinite(ind.e_slow[i - 1]) and np.isfinite(ind.atr[i]))


def find_signals(b: Bars, ind: Ind, p: Params, variant: str = "touch", lo: int = 0, hi: int | None = None) -> list[Signal]:
    """Long signals on bars lo .. hi-1. ``variant``: touch (main), touch_no_trend (placebo P2), sweep (S14 proper)."""
    hi = len(b) if hi is None else hi
    if variant not in ("touch", "touch_no_trend", "sweep"):
        raise ValueError(variant)
    out: list[Signal] = []
    if variant != "sweep":
        cs = np.cumsum(ind.touch)
        for i in range(max(lo, 1), hi):
            if not ind.touch[i] or not _ready(ind, p, i):
                continue
            c = float(b.c[i])
            if not c > ind.e_slow[i - 1]:
                continue
            if variant == "touch" and not trend_ok(ind, i - 1, p):
                continue
            imp = _impulse(b, ind, p, i, i - 1)
            if imp is None:
                continue
            k, high = imp
            if c >= high or cs[i - 1] - cs[k] != 0:  # not below the high, or not the first touch after it
                continue
            st = stop_target(b, ind, p, i, high)
            if st is not None:
                out.append(Signal(i, variant, k, c, st[0], st[1], high, st[2]))
        return out
    fr = fractal_lows(b.low)
    in_zone = np.zeros(len(b), dtype=bool)
    for f in fr:
        if f >= 1 and np.isfinite(ind.e_slow[f - 1]) and ind.e_slow[f - 1] <= b.low[f] <= ind.e_fast[f - 1]:
            in_zone[f] = True
    zf = np.flatnonzero(in_zone)
    used: set[int] = set()
    for i in range(max(lo, 1), hi):
        if not _ready(ind, p, i):
            continue
        k = int(ind.hi_idx[i])
        if k < 0 or k in used:
            continue
        c = float(b.c[i])
        if not c > ind.e_slow[i - 1] or not trend_ok(ind, k, p):
            continue
        q = int(np.searchsorted(zf, i - 3, side="right")) - 1  # last in-zone fractal with f + 2 <= i - 1
        if q < 0 or zf[q] <= k:
            continue
        f = int(zf[q])
        lf = float(b.low[f])
        if not (b.low[i] < lf < c) or float(b.low[f + 1 : i].min()) < lf:  # bar i is the first to sweep it
            continue
        imp = _impulse(b, ind, p, i, i - 1)
        if imp is None or c >= imp[1]:
            continue
        used.add(k)
        st = stop_target(b, ind, p, i, imp[1])
        if st is not None:
            out.append(Signal(i, "sweep", k, c, st[0], st[1], imp[1], st[2]))
    return out


def fractal_lows(low: np.ndarray, n: int = 2) -> np.ndarray:
    """Indices of strict fractal lows (lower than ``n`` bars on each side)."""
    m = low.size
    if m < 2 * n + 1:
        return np.zeros(0, dtype=np.int64)
    core = low[n : m - n]
    ok = np.ones(core.size, dtype=bool)
    for d in range(1, n + 1):
        ok &= low[n - d : m - n - d] > core
        ok &= low[n + d : m - n + d] > core
    return np.flatnonzero(ok) + n


def trend_episodes(ind: Ind, p: Params) -> np.ndarray:
    """Episode id per bar j whose previous bar is in trend (``trend_ok(j-1)``), -1 otherwise. An episode is a run of
    consecutive such bars."""
    n = ind.pos.size
    ep = np.full(n, -1, dtype=np.int64)
    cur, prev_in = -1, False
    for j in range(1, n):
        ok = trend_ok(ind, j - 1, p) and ind.pos[j] >= 1
        if ok:
            if not prev_in:
                cur += 1
            ep[j] = cur
        prev_in = ok
    return ep


def placebo_signal(b: Bars, ind: Ind, p: Params, j: int) -> Signal | None:
    """Entry at the close of an arbitrary bar j with the same stop and target rules (H = highest high of the
    previous impulse_bars bars)."""
    if j < p.impulse_bars or not _ready(ind, p, j):
        return None
    high = float(b.h[j - p.impulse_bars : j].max())
    st = stop_target(b, ind, p, j, high)
    if st is None:
        return None
    return Signal(j, "placebo", int(ind.hi_idx[j]), float(b.c[j]), st[0], st[1], high, st[2])


# ---------------------------------------------------------------- trade path


@dataclass(frozen=True)
class Trade:
    entry: float
    stop: float
    target: float
    exit: float
    exit_kind: str  # stop | stop_gap | target | time | data_end
    both_touched: bool
    r_gross: float
    cost_r: float
    mfe_r: float  # best excursion in R before the exit (bar highs), for exit-rule studies
    entry_ms: int
    exit_ms: int  # close time of the exit bar

    @property
    def r_net(self) -> float:
        return self.r_gross - self.cost_r

    @property
    def stop_pct(self) -> float:
        return (self.entry - self.stop) / abs(self.entry)


def simulate(
    path: Bars,
    entry_ms: int,
    entry: float,
    stop: float,
    target: float,
    hold_ms: int,
    fee_side: float = MARKET_SIDE,
    max_gap_ms: int = MAX_GAP_MS,
) -> Trade | None:
    """Long held from ``entry_ms`` (a bar close) over ``path`` bars opening in [entry_ms, entry_ms + hold_ms). The stop
    wins when a bar touches both; an open beyond the stop exits at the open; time exit at the last bar's close. If the
    data end before the horizon, or a hole longer than ``max_gap_ms`` opens in them (a delisting, a ticker reused for
    another token, a redenomination), the exit is the last close before it (``data_end``)."""
    i0 = int(np.searchsorted(path.t, entry_ms))
    i1 = int(np.searchsorted(path.t, entry_ms + hold_ms))
    if i1 <= i0 or int(path.t[i0]) - entry_ms > max_gap_ms:
        return None
    cut = False
    holes = np.flatnonzero(np.diff(path.t[i0:i1]) > max_gap_ms)
    if holes.size:
        i1, cut = i0 + int(holes[0]) + 1, True
    risk = entry - stop
    best = entry
    px, kind, both, j = float(path.c[i1 - 1]), ("data_end" if cut else "time"), False, i1 - 1
    for k in range(i0, i1):
        if path.o[k] <= stop:
            px, kind, j = float(path.o[k]), "stop_gap", k
            break
        if path.low[k] <= stop:
            px, kind, both, j = stop, "stop", bool(path.h[k] >= target), k
            break
        if path.h[k] >= target:
            px, kind, j = target, "target", k
            best = max(best, target)
            break
        best = max(best, float(path.h[k]))
    if kind == "time" and i1 == len(path) and int(path.t[i1 - 1]) + path.bar_ms < entry_ms + hold_ms:
        kind = "data_end"
    gross = (px - entry) / risk
    cost = fee_side * (abs(entry) + abs(px)) / risk
    return Trade(entry, stop, target, px, kind, both, gross, cost, (best - entry) / risk, entry_ms, int(path.t[j]) + path.bar_ms)


# ---------------------------------------------------------------- H5


def align_daily(b: Bars, daily: Bars, values: np.ndarray) -> np.ndarray:
    """For each bar of ``b``: ``values`` of the last daily bar closed at or before that bar's close; NaN if none."""
    close_b = b.t + b.bar_ms
    close_d = daily.t + daily.bar_ms
    k = np.searchsorted(close_d, close_b, side="right") - 1
    out = np.full(len(b), np.nan)
    ok = k >= 0
    out[ok] = values[k[ok]]
    return out


def forward_min_low(b: Bars, horizon_ms: int, max_gap_ms: int = MAX_GAP_MS) -> np.ndarray:
    """Lowest low of bars i+1 .. that open before close_i + horizon; NaN when the data end before the horizon or a
    hole longer than ``max_gap_ms`` lies inside the window."""
    n = len(b)
    out = np.full(n, np.nan)
    close = b.t + b.bar_ms
    end = np.searchsorted(b.t, close + horizon_ms)  # first bar opening at/after the horizon
    last_close = int(b.t[-1]) + b.bar_ms if n else 0
    hole = np.concatenate([[0], np.cumsum(np.diff(b.t) > max_gap_ms)]) if n else np.zeros(0)
    for i in range(n):
        lo, hi = i + 1, int(end[i])
        if close[i] + horizon_ms > last_close or hi <= lo or hole[hi - 1] != hole[i]:
            continue
        out[i] = float(b.low[lo:hi].min())
    return out


def h5_events(b: Bars, ind: Ind, min_run: int) -> np.ndarray:
    """Bars i whose close is below the mid EMA after >= ``min_run`` closes in a row above it."""
    n = len(b)
    if n < 2:
        return np.zeros(0, dtype=np.int64)
    with np.errstate(invalid="ignore"):
        below = b.c[1:] < ind.e_mid[1:]
    ok = below & (ind.above_run[:-1] >= min_run) & (ind.pos[1:] >= 1)
    return np.flatnonzero(ok) + 1


def matched_baseline(
    ev_dist: Sequence[float], ev_hit: Sequence[float], base_dist: np.ndarray, base_hit: np.ndarray, bins: int = 10
) -> dict[str, Any]:
    """Event hit rate against a baseline matched on distance: cut points are the event-distance deciles; the expected
    rate is the baseline hit rate per bin weighted by the events' bin counts. Returns rates and per-event expected."""
    ed = np.asarray(ev_dist, dtype=float)
    eh = np.asarray(ev_hit, dtype=float)
    if ed.size == 0:
        return {"n": 0}
    cuts = np.quantile(ed, np.linspace(0, 1, bins + 1)[1:-1])
    eb = np.searchsorted(cuts, ed, side="right")
    bb = np.searchsorted(cuts, base_dist, side="right")
    rate = np.full(bins, np.nan)
    for k in range(bins):
        m = bb == k
        if m.any():
            rate[k] = float(base_hit[m].mean())
    exp = rate[eb]
    ok = np.isfinite(exp)
    return {
        "n": int(ed.size),
        "n_matched": int(ok.sum()),
        "event_rate": float(eh[ok].mean()) if ok.any() else float("nan"),
        "base_rate": float(exp[ok].mean()) if ok.any() else float("nan"),
        "expected": exp,
        "bin_rates": rate,
        "cuts": cuts,
    }
