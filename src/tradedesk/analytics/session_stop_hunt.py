"""Session stop hunt (IDEA-20261016-session-stop-hunt, setup S25): Asian range swept at the Frankfurt/London open,
trade against the sweep after a 5m break of structure.

Pure functions, no I/O. Bars are numpy arrays of 5m open times (ms, sorted, unique) and OHLC prices. Every
decision about a day uses only bars that open before that day's time exit, so data after it cannot change the result.

The short side is implemented once; a long is the same logic on negated prices (high <-> -low), which mirrors the
range, the fractal, the stop buffer and the target exactly. Fees are decimal fractions of price per side
(0.0007 = 0.05 % taker + 0.02 % slippage) and are converted to R with the trade's own stop distance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

BAR_MS = 300_000
HOUR_MS = 3_600_000
BERLIN = ZoneInfo("Europe/Berlin")
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")

TAKER = 0.0005
SLIPPAGE = 0.0002
MAKER = 0.0002
MARKET_SIDE = TAKER + SLIPPAGE  # 0.07 % per market side
STOP_BUFFER = 0.0005  # stop 0.05 % beyond the sweep extreme
MAX_BOS_BARS = 12  # BOS close no later than 60 min after the sweep extreme
E2_VALID_BARS = 12  # the E2 limit lives 60 min after the BOS close
FRACTAL_N = 2  # 2 bars left and 2 right
FRACTAL_LOOKBACK = 288  # search the manipulation start at most one day back


@dataclass(frozen=True)
class Session:
    """Times (ms UTC) of one day's setup. Bars with open in [range_start, range_end) form the range, bars with open in
    [range_end, window_end) can sweep it, and an open trade closes at the close of the bar that ends at exit_at."""

    day: str
    range_start: int
    range_end: int
    window_end: int
    exit_at: int


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _local(day: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def london_session(day: date, shift_hours: int = 0) -> Session:
    """Asian range 00:00 UTC -> Frankfurt open (08:00 Europe/Berlin); hunt window -> London open (08:00 Europe/London)
    + 3 h; time exit 16:00 UTC. ``shift_hours`` moves every time by the same amount (placebo anchors)."""
    s = shift_hours * HOUR_MS
    d0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return Session(
        day=day.isoformat(),
        range_start=_ms(d0) + s,
        range_end=_ms(_local(day, 8, 0, BERLIN)) + s,
        window_end=_ms(_local(day, 8, 0, LONDON)) + 3 * HOUR_MS + s,
        exit_at=_ms(d0 + timedelta(hours=16)) + s,
    )


def new_york_session(day: date) -> Session:
    """H1b: London range 08:00 Europe/London -> 08:00 America/New_York; hunt window -> NYSE open (09:30 New York)
    + 3 h; time exit 16:00 New York."""
    return Session(
        day=day.isoformat(),
        range_start=_ms(_local(day, 8, 0, LONDON)),
        range_end=_ms(_local(day, 8, 0, NEW_YORK)),
        window_end=_ms(_local(day, 9, 30, NEW_YORK)) + 3 * HOUR_MS,
        exit_at=_ms(_local(day, 16, 0, NEW_YORK)),
    )


@dataclass(frozen=True)
class Bars:
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    low: np.ndarray
    c: np.ndarray

    @classmethod
    def from_rows(cls, rows: Any) -> Bars:
        a = np.asarray(rows, dtype=float)
        if a.size == 0:
            z = np.zeros(0)
            return cls(z.astype(np.int64), z, z, z, z)
        return cls(a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4])

    def slice(self, i: int, j: int) -> Bars:
        return Bars(self.t[i:j], self.o[i:j], self.h[i:j], self.low[i:j], self.c[i:j])

    def mirrored(self) -> Bars:
        """Negated prices: a long setup becomes a short one."""
        return Bars(self.t, -self.o, -self.low, -self.h, -self.c)


@dataclass(frozen=True)
class Setup:
    """A BOS-confirmed setup in short space (prices negated for a long). Indices are into the sliced day bars."""

    side: int  # -1 short (swept the high), +1 long (swept the low)
    range_high: float  # short space
    range_low: float
    sweep_idx: int
    extreme_idx: int
    extreme: float
    manip_idx: int
    manip_start: float
    bos_idx: int
    entry: float  # E1: BOS close
    stop: float
    exit_idx: int  # last bar (closes at exit_at)


def is_fractal_low(low: np.ndarray, k: int, n: int = FRACTAL_N) -> bool:
    """Strict fractal low: lower than the n bars on each side (needs bars k-n .. k+n)."""
    if k - n < 0 or k + n >= low.size:
        return False
    v = low[k]
    return bool(np.all(low[k - n : k] > v) and np.all(low[k + 1 : k + n + 1] > v))


def fractal_low_indices(low: np.ndarray, n: int = FRACTAL_N) -> np.ndarray:
    """Sorted indices of every strict fractal low (vectorised ``is_fractal_low``)."""
    m = low.size
    if m < 2 * n + 1:
        return np.zeros(0, dtype=np.int64)
    core = low[n : m - n]
    ok = np.ones(core.size, dtype=bool)
    for d in range(1, n + 1):
        ok &= low[n - d : m - n - d] > core
        ok &= low[n + d : m - n + d] > core
    return np.flatnonzero(ok) + n


def last_fractal_low(fractals: np.ndarray, before: int, confirmed_by: int, floor: int = 0, n: int = FRACTAL_N) -> int | None:
    """Last fractal index k < ``before`` whose right side is closed by bar ``confirmed_by`` (k + n <= it), k >= floor."""
    cap = min(before - 1, confirmed_by - n)
    p = int(np.searchsorted(fractals, cap, side="right")) - 1
    if p < 0 or fractals[p] < floor:
        return None
    return int(fractals[p])


def _short_setup(b: Bars, rs: int, re_: int, sweep: int, w_end: int, exit_idx: int, side: int) -> tuple[Setup | None, str]:
    """Short-space BOS search after the sweep bar ``sweep``. ``rs``/``re_`` bound the range, ``w_end`` is the first bar
    outside the hunt window. The extreme may still extend inside the window; a new extreme after it voids the setup."""
    H = float(b.h[rs:re_].max())
    L = float(b.low[rs:re_].min())
    X, ix = float(b.h[sweep]), sweep
    returned = False
    seen_fractal = False
    floor = max(0, rs - FRACTAL_LOOKBACK)
    fractals = fractal_low_indices(b.low)
    for j in range(sweep, exit_idx):  # a BOS on the exit bar itself could not be traded
        if b.h[j] > X:
            if j >= w_end:
                return None, "new_extreme_after_window"
            X, ix = float(b.h[j]), j
            returned = False
        if j - ix > MAX_BOS_BARS:
            return None, ("no_bos_60m" if seen_fractal else "no_fractal")
        if b.c[j] < H:
            returned = True
        m = last_fractal_low(fractals, ix, j, floor=floor)
        if m is None:
            continue
        seen_fractal = True
        M = float(b.low[m])
        if returned and b.c[j] < M:
            entry = float(b.c[j])
            stop = X + STOP_BUFFER * abs(X)
            return Setup(side, H, L, sweep, ix, X, m, M, j, entry, stop, exit_idx), "ok"
    return None, "no_bos_60m"


def find_setup(bars: Bars, s: Session) -> tuple[Setup | None, Bars | None, str]:
    """Detect the day's setup. Returns (setup in short space, the day bars in that space, reason).

    Reasons: ok, gap (a missing bar between range start and the time exit), no_sweep, both_first (the first bar out
    of the range breaks both sides), no_bos_60m, new_extreme_after_window, no_fractal."""
    i_rs = int(np.searchsorted(bars.t, s.range_start))
    i_ex = int(np.searchsorted(bars.t, s.exit_at))  # first bar at/after exit_at; exit bar is i_ex - 1
    expected = (s.exit_at - s.range_start) // BAR_MS
    if i_ex - i_rs != expected or i_rs >= bars.t.size or bars.t[i_rs] != s.range_start:
        return None, None, "gap"
    lo = max(0, i_rs - FRACTAL_LOOKBACK)
    day = bars.slice(lo, i_ex)  # nothing at or after the time exit
    rs = i_rs - lo
    re_ = rs + (s.range_end - s.range_start) // BAR_MS
    w_end = rs + (s.window_end - s.range_start) // BAR_MS
    exit_idx = day.t.size - 1
    H = float(day.h[rs:re_].max())
    L = float(day.low[rs:re_].min())
    for j in range(re_, w_end):
        up, dn = day.h[j] > H, day.low[j] < L
        if up and dn:
            return None, None, "both_first"
        if up or dn:
            side = -1 if up else 1
            space = day if up else day.mirrored()
            st, why = _short_setup(space, rs, re_, j, w_end, exit_idx, side)
            return st, (space if st else None), why
    return None, None, "no_sweep"


@dataclass(frozen=True)
class Trade:
    entry_kind: str  # E1 | E2
    target_kind: str  # T1 | T2
    entry: float  # short space
    stop: float
    target: float
    exit: float
    exit_kind: str  # stop | stop_gap | target | time
    both_touched: bool  # the exit bar touched both stop and target (counted as a stop)
    r_gross: float
    cost_r: float
    entry_idx: int
    exit_idx: int

    @property
    def r_net(self) -> float:
        return self.r_gross - self.cost_r

    @property
    def stop_pct(self) -> float:
        return (self.stop - self.entry) / abs(self.entry)


def walk(b: Bars, start: int, last: int, entry: float, stop: float, target: float) -> tuple[float, str, bool, int]:
    """Short position held from the close before ``start``; bars start..last. Stop first when a bar touches both; an
    open beyond the stop exits at the open; time exit at the close of ``last``."""
    for j in range(start, last + 1):
        if b.o[j] >= stop:
            return float(b.o[j]), "stop_gap", False, j
        if b.h[j] >= stop:
            return stop, "stop", bool(b.low[j] <= target), j
        if b.low[j] <= target:
            return target, "target", False, j
    return float(b.c[last]), "time", False, last


def _trade(ek: str, tk: str, b: Bars, start: int, last: int, entry: float, stop: float, target: float, fee_in: float) -> Trade:
    px, kind, both, j = walk(b, start, last, entry, stop, target)
    risk = stop - entry
    gross = (entry - px) / risk
    cost = (fee_in * abs(entry) + MARKET_SIDE * abs(px)) / risk
    return Trade(ek, tk, entry, stop, target, px, kind, both, gross, cost, start - 1, j)


def target_for(kind: str, st: Setup, entry: float) -> float | None:
    """T1: opposite range edge, None when it is closer than 1R; T2: fixed 2R."""
    risk = st.stop - entry
    if kind == "T2":
        return entry - 2 * risk
    return st.range_low if entry - st.range_low >= risk else None


def simulate(st: Setup, b: Bars, entry_kind: str, target_kind: str) -> tuple[Trade | None, str]:
    """One entry/target variant of a setup. Reasons: ok, t1_lt_1r, e2_unfilled, no_bars."""
    last = st.exit_idx
    if entry_kind == "E1":
        if st.bos_idx >= last:
            return None, "no_bars"
        tgt = target_for(target_kind, st, st.entry)
        if tgt is None:
            return None, "t1_lt_1r"
        return _trade("E1", target_kind, b, st.bos_idx + 1, last, st.entry, st.stop, tgt, MARKET_SIDE), "ok"
    level = 0.5 * (st.manip_start + st.extreme)
    tgt = target_for(target_kind, st, level)
    if tgt is None:
        return None, "t1_lt_1r"
    risk = st.stop - level
    for j in range(st.bos_idx + 1, min(st.bos_idx + E2_VALID_BARS, last) + 1):
        if b.h[j] < level:
            continue
        # filled at the limit price (never at a better open); on the fill bar only the stop counts, because the
        # order of the fill and a target touch inside the bar is unknown
        if b.h[j] >= st.stop or j == last:
            if b.h[j] >= st.stop:
                px, kind = (float(b.o[j]), "stop_gap") if b.o[j] >= st.stop else (st.stop, "stop")
            else:
                px, kind = float(b.c[j]), "time"
            cost = (MAKER * abs(level) + MARKET_SIDE * abs(px)) / risk
            return Trade("E2", target_kind, level, st.stop, tgt, px, kind, False, (level - px) / risk, cost, j, j), "ok"
        t = _trade("E2", target_kind, b, j + 1, last, level, st.stop, tgt, MAKER)
        return Trade("E2", target_kind, level, st.stop, tgt, t.exit, t.exit_kind, t.both_touched, t.r_gross, t.cost_r, j, t.exit_idx), "ok"
    return None, "e2_unfilled"


# ---------------------------------------------------------------- statistics


def design_effect(cluster_sizes: Sequence[int], rho: float) -> float:
    """Kish design effect 1 + (m - 1) * rho with m the size-weighted mean cluster size."""
    n = sum(cluster_sizes)
    if n == 0:
        return 1.0
    m = sum(k * k for k in cluster_sizes) / n
    return 1.0 + (m - 1.0) * rho


def cluster_bootstrap_diff(
    a: Sequence[float], a_cl: Sequence[Any], b: Sequence[float], b_cl: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261016
) -> dict[str, Any]:
    """mean(a) - mean(b) with a joint cluster bootstrap: the same resampled dates feed both means (a date's London
    and placebo trades stay together). Returns diff, 95 % percentile CI and one-sided p for diff > 0."""
    keys = sorted(set(a_cl) | set(b_cl))
    pos = {k: i for i, k in enumerate(keys)}
    sa, na, sb, nb = (np.zeros(len(keys)) for _ in range(4))
    for v, k in zip(a, a_cl, strict=True):
        sa[pos[k]] += v
        na[pos[k]] += 1
    for v, k in zip(b, b_cl, strict=True):
        sb[pos[k]] += v
        nb[pos[k]] += 1
    diff = sa.sum() / max(na.sum(), 1) - sb.sum() / max(nb.sum(), 1)
    if len(keys) < 2 or na.sum() == 0 or nb.sum() == 0:
        return {"diff": float(diff), "ci": None, "p": None}
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    chunk = max(1, 2_000_000 // len(keys))
    for lo in range(0, n_boot, chunk):
        hi = min(n_boot, lo + chunk)
        idx = rng.integers(0, len(keys), size=(hi - lo, len(keys)))
        out[lo:hi] = sa[idx].sum(1) / np.maximum(na[idx].sum(1), 1) - sb[idx].sum(1) / np.maximum(nb[idx].sum(1), 1)
    return {
        "diff": float(diff),
        "ci": (float(np.quantile(out, 0.025)), float(np.quantile(out, 0.975))),
        "p": float((np.sum(out <= 0) + 1) / (n_boot + 1)),
    }
