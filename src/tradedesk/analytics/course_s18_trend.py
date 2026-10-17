"""Course setup S18 / H4 (IDEA-20261016-course-setups-crypto, step 3): trend following after price reclaims all EMAs.

Signal: the first daily close above the daily EMA 100, the daily EMA 200 and the 4h EMA 200 after ``lookback`` days
that were not above all three; exit at the first daily close below the daily EMA 100 (variants: daily EMA 200, a 4h
close below the 4h EMA 200, a stop under the range low).

Pure functions, no I/O. Input is 1h bars (``course_s14_ema.Bars``). A hole longer than ``max_gap_ms`` between 1h bars
starts a new segment (indicators restart, a trade that meets the hole ends at the last close before it). Every
indicator is causal: the value at bar i uses bars <= i only (``tests/test_course_s18_trend.py`` checks that the
signals before t do not change when the data after t change). Trade outcomes look forward by design. Returns are
decimal fractions on notional without leverage; fees are fractions of each side's notional (0.0007 per side).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, MARKET_SIDE, MAX_GAP_MS, Bars, ema

__all__ = [
    "DAY_MS",
    "HOUR_MS",
    "MARKET_SIDE",
    "S18Ind",
    "Trade",
    "net_return",
    "pos_from_seg",
    "resample_partial",
    "s18_indicators",
    "segments",
    "signal_days",
    "simulate",
]


def segments(t: np.ndarray, max_gap_ms: int = MAX_GAP_MS) -> np.ndarray:
    """Segment id per bar: a gap between consecutive bar opens > ``max_gap_ms`` starts a new segment."""
    if t.size == 0:
        return np.zeros(0, dtype=np.int64)
    return np.concatenate([[0], np.cumsum(np.diff(t) > max_gap_ms)]).astype(np.int64)


def pos_from_seg(seg: np.ndarray) -> np.ndarray:
    """Position of each bar inside its segment."""
    n = seg.size
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    brk = np.concatenate([[True], seg[1:] != seg[:-1]])
    idx = np.arange(n)
    return idx - np.maximum.accumulate(np.where(brk, idx, 0))


def resample_partial(h1: Bars, seg: np.ndarray, hours: int, min_hours: int) -> tuple[Bars, np.ndarray]:
    """Aggregate 1h bars into ``hours``-hour UTC buckets inside segments. A bucket is kept when it has at least
    ``min_hours`` bars and its last hour; close = the last hour's close. Returns (bars, segment id per bar)."""
    step = hours * HOUR_MS
    if len(h1) == 0:
        z = np.zeros(0)
        return Bars(z.astype(np.int64), z, z, z, z, step), np.zeros(0, dtype=np.int64)
    key = h1.t // step
    new = np.concatenate([[True], (key[1:] != key[:-1]) | (seg[1:] != seg[:-1])])
    starts = np.flatnonzero(new)
    ends = np.concatenate([starts[1:], [key.size]]) - 1
    counts = ends - starts + 1
    last_ok = h1.t[ends] == key[ends] * step + (hours - 1) * HOUR_MS
    keep = (counts >= min_hours) & last_ok
    hi = np.maximum.reduceat(h1.h, starts)[keep]
    lo = np.minimum.reduceat(h1.low, starts)[keep]
    s, e = starts[keep], ends[keep]
    return Bars(key[s] * step, h1.o[s].copy(), hi, lo, h1.c[e].copy(), step), seg[s].copy()


@dataclass(frozen=True)
class S18Ind:
    d: Bars  # daily bars
    dseg: np.ndarray
    dpos: np.ndarray
    e100: np.ndarray  # daily EMA 100
    e200: np.ndarray  # daily EMA 200
    e4: np.ndarray  # 4h EMA 200 of the last 4h bar closed at or before the daily close (same segment)
    h4: Bars
    h4seg: np.ndarray
    h4e200: np.ndarray

    @property
    def defined(self) -> np.ndarray:
        return np.isfinite(self.e100) & np.isfinite(self.e200) & np.isfinite(self.e4)

    @property
    def above_all(self) -> np.ndarray:
        c = self.d.c
        with np.errstate(invalid="ignore"):
            return self.defined & (c > self.e100) & (c > self.e200) & (c > self.e4)

    @property
    def below_all(self) -> np.ndarray:
        c = self.d.c
        with np.errstate(invalid="ignore"):
            return self.defined & (c < self.e100) & (c < self.e200) & (c < self.e4)


def s18_indicators(h1: Bars, max_gap_ms: int = MAX_GAP_MS) -> S18Ind:
    seg = segments(h1.t, max_gap_ms)
    d, dseg = resample_partial(h1, seg, 24, 20)
    h4, h4seg = resample_partial(h1, seg, 4, 3)
    dpos = pos_from_seg(dseg)
    e100 = ema(d.c, 100, dpos)
    e200 = ema(d.c, 200, dpos)
    h4e = ema(h4.c, 200, pos_from_seg(h4seg))
    # align: last 4h bar of the same segment with close <= the daily close
    k = np.searchsorted(h4.t + h4.bar_ms, d.t + d.bar_ms, side="right") - 1
    e4 = np.full(len(d), np.nan)
    ok = k >= 0
    kk = np.where(ok, k, 0)
    ok &= h4seg[kk] == dseg if len(h4) else np.zeros(len(d), dtype=bool)
    e4[ok] = h4e[kk[ok]]
    return S18Ind(d, dseg, dpos, e100, e200, e4, h4, h4seg, h4e)


def signal_days(ind: S18Ind, lookback: int, strict: bool = False) -> np.ndarray:
    """Daily indices d with a close above all three EMAs whose previous ``lookback`` daily bars (same segment) all had
    the three EMAs defined and were not above all three (``strict``: below all three)."""
    n = len(ind.d)
    if n <= lookback:
        return np.zeros(0, dtype=np.int64)
    good = ind.below_all if strict else (ind.defined & ~ind.above_all)
    bad = np.concatenate([[0], np.cumsum(~good)])  # bad[i] = number of bad bars among 0 .. i-1
    idx = np.arange(lookback, n)
    win_bad = bad[idx] - bad[idx - lookback]
    ok = ind.above_all[idx] & (ind.dpos[idx] >= lookback) & (win_bad == 0)
    return idx[ok]


@dataclass(frozen=True)
class Trade:
    entry_ms: int  # close time of the entry bar
    exit_ms: int  # close time of the exit bar
    entry: float
    exit: float
    kind: str  # ema | stop | stop_gap | time | data_end | open_end
    gross: float
    cost: float

    @property
    def net(self) -> float:
        return self.gross - self.cost

    @property
    def hold_days(self) -> float:
        return (self.exit_ms - self.entry_ms) / DAY_MS


def net_return(entry: float, exit_px: float, fee_side: float = MARKET_SIDE) -> tuple[float, float]:
    """(gross, cost) of a long on notional: gross = exit/entry - 1, cost = fee_side * (1 + exit/entry)."""
    r = exit_px / entry
    return r - 1.0, fee_side * (1.0 + r)


def simulate(
    path: Bars,
    path_seg: np.ndarray,
    entry_ms: int,
    entry: float,
    seg_id: int,
    *,
    exit_flag: np.ndarray | None = None,
    stop: float | None = None,
    until_ms: int | None = None,
    end_ms: int | None = None,
    fee_side: float = MARKET_SIDE,
) -> Trade | None:
    """Long from ``entry_ms`` (a bar close) over ``path`` bars opening at or after it, inside segment ``seg_id``.

    Exits, in order within a bar: open <= stop (at the open), low <= stop (at the stop), ``exit_flag[k]`` true (at the
    close of k), close time of k >= ``until_ms`` (at the close, a time exit). When the segment ends (a hole) the exit
    is the last close (``data_end``); when the data end, ``open_end`` if the last close is within 2 days of ``end_ms``
    (the end of the data set), otherwise ``data_end`` (a delisting). None when no path bar follows the entry."""
    i0 = int(np.searchsorted(path.t, entry_ms))
    n = len(path)
    if i0 >= n or path_seg[i0] != seg_id:
        return None
    px, kind, j = None, None, i0
    k = i0
    while k < n and path_seg[k] == seg_id:
        j = k
        if stop is not None and path.o[k] <= stop:
            px, kind = float(path.o[k]), "stop_gap"
            break
        if stop is not None and path.low[k] <= stop:
            px, kind = float(stop), "stop"
            break
        if exit_flag is not None and bool(exit_flag[k]):
            px, kind = float(path.c[k]), "ema"
            break
        if until_ms is not None and int(path.t[k]) + path.bar_ms >= until_ms:
            px, kind = float(path.c[k]), "time"
            break
        k += 1
    if px is None:
        px = float(path.c[j])
        last_close = int(path.t[j]) + path.bar_ms
        kind = "open_end" if (k >= n and end_ms is not None and last_close >= end_ms - 2 * DAY_MS) else "data_end"
    gross, cost = net_return(entry, px, fee_side)
    return Trade(entry_ms, int(path.t[j]) + path.bar_ms, entry, px, str(kind), gross, cost)
