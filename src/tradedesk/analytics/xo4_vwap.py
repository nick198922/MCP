"""XO-4, developed weekly VWAP as a pivot (IDEA-20261113-x-traders, item 5; spec «4б» XO-4 of LIB-20261113-x-trader_xo).

The level is the developing VWAP of the current period (ISO week from Monday 00:00 UTC; month from the 1st 00:00 UTC is
an exploratory variant), computed from 1h bars through each bar's close: VWAP = sum(tp * v) / sum(v) with the typical
price tp = (high + low + close) / 3 and the base volume v = quote volume / tp (the cache keeps quote volume only).

Trigger (one, both sides): at the close of a ``tf_hours`` bar (4h by default, aligned to 00:00 UTC), the close is on the
other side of the developing VWAP than the previous ``tf_hours`` close of the same period was ("loss": above -> below =
short; "acceptance back above": below -> above = long). The period must be at least ``warmup_h`` hours old at the
trigger close and >= ``MIN_COVER`` of its elapsed hours must have a bar. Entry: the trigger close. Stop: beyond the
extreme of the last two trigger blocks (2 x tf_hours 1h bars up to the trigger) plus ``buf_atr`` x ATR(14, 1h); stop
distance in [min_stop, max_stop] of the price, else no trade. The level L is the VWAP at the trigger close, then held
fixed (horizontal).

Exit (``vwap_exit_outcomes``), on hole-free 1h paths from the bar after the trigger: a bar opening at or beyond the stop
exits at the open, a bar touching the stop exits at the stop; otherwise, at the close of a ``tf_hours`` bar that is back
on the wrong side of L ("acceptance through" the level: close above L for a short) the trade exits at that close; else
at the close of the last bar of the hold. No price target. R is the live risk unit (entry-stop distance plus the taker
fee on both prices): a return on the trade's risk, not on capital. ``funding_r`` adds the perp funding of the hold,
``funding_covered`` flags trades whose hold the settlement history covers. ``overlaps_opposite`` flags trades that
coexist with an opposite trade on the same symbol; ``paired_placebo_diff`` is the per-trade real-minus-placebo R.

Pure functions, no I/O. The VWAP at bar j uses bars up to j only, a signal only bars up to its own close, so nothing
before time t changes when bars after t change.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.stop_management import Fees

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
MIN_COVER = 0.9  # share of the period's elapsed hours that must have a bar
WARMUP_H = 24  # the developing VWAP must be at least this many hours old at the trigger close
KIND_TIME, KIND_STOP, KIND_INVALID = 0, 1, 3


# ---------------------------------------------------------------- periods and levels


def period_start(t: np.ndarray, period: str) -> np.ndarray:
    """Open ms of the period containing each open ms ``t``: ``week`` = ISO week (Monday 00:00 UTC), ``month`` = the 1st
    00:00 UTC."""
    t = np.asarray(t, dtype=np.int64)
    if period == "week":
        d = t // DAY_MS
        return (((d + 3) // 7) * 7 - 3) * DAY_MS
    if period == "month":
        return t.astype("datetime64[ms]").astype("datetime64[M]").astype("datetime64[ms]").astype(np.int64)
    raise ValueError(f"unknown period {period!r}")


def _grouped_cumsum(x: np.ndarray, start: np.ndarray) -> np.ndarray:
    """Cumulative sum of ``x`` restarting wherever ``start`` (sorted group keys) changes."""
    cs = np.cumsum(x)
    if x.size == 0:
        return cs
    first = np.concatenate([[True], start[1:] != start[:-1]])
    base = np.where(first, cs - x, np.nan)
    idx = np.maximum.accumulate(np.where(first, np.arange(x.size), 0))
    return cs - base[idx]


@dataclass(frozen=True)
class Levels:
    level: np.ndarray  # developing level at each bar's close (nan where undefined)
    start: np.ndarray  # period open ms of each bar
    elapsed_h: np.ndarray  # hours from the period open to the bar's close
    cover_ok: np.ndarray  # >= MIN_COVER of the elapsed hours have a bar


def developing_vwap(t: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, qv: np.ndarray, period: str = "week") -> Levels:
    """Developing VWAP of the period through each 1h bar's close (see the module docstring)."""
    t = np.asarray(t, dtype=np.int64)
    st = period_start(t, period)
    tp = (h + low + c) / 3.0
    v = np.where(tp > 0, qv / np.where(tp > 0, tp, 1.0), 0.0)
    num = _grouped_cumsum(np.asarray(qv, dtype=float), st)
    den = _grouped_cumsum(v, st)
    lvl = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
    n = _grouped_cumsum(np.ones(t.size), st)
    el = (t + HOUR_MS - st) / HOUR_MS
    return Levels(lvl, st, el, n >= MIN_COVER * el)


def period_open(t: np.ndarray, o: np.ndarray, period: str = "week") -> Levels:
    """Open of the period's first hour (exploratory variant «W/O»); nan for a period whose first hour has no bar."""
    t = np.asarray(t, dtype=np.int64)
    st = period_start(t, period)
    first = np.concatenate([[True], st[1:] != st[:-1]]) if t.size else np.zeros(0, dtype=bool)
    val = np.where(first & (t == st), o, np.nan)
    idx = np.maximum.accumulate(np.where(first, np.arange(t.size), 0)) if t.size else np.zeros(0, dtype=np.int64)
    lvl = val[idx] if t.size else np.zeros(0)
    n = _grouped_cumsum(np.ones(t.size), st)
    el = (t + HOUR_MS - st) / HOUR_MS
    return Levels(lvl, st, el, n >= MIN_COVER * el)


def atr_sma(h: np.ndarray, low: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    """Causal ATR: mean true range of the last ``n`` bars ending at each bar; nan for bars 0 .. n - 1."""
    tr = np.full(h.size, np.nan)
    if h.size > 1:
        pc = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - low[1:], np.abs(h[1:] - pc), np.abs(low[1:] - pc)])
    out = np.full(h.size, np.nan)
    if h.size > n:
        cs = np.concatenate([[0.0], np.cumsum(tr[1:])])
        j = np.arange(n, h.size)
        out[n:] = (cs[j] - cs[j - n]) / n
    return out


# ---------------------------------------------------------------- signals


@dataclass(frozen=True)
class Signal:
    i: int  # index of the 1h bar whose close is the trigger close (the entry)
    t_in: int  # entry time = that bar's close
    side: int  # +1 long (back above), -1 short (loss)
    entry: float
    stop: float
    level: float  # the level at the trigger close, then fixed
    period: int  # open ms of the level's period


def is_boundary(t: np.ndarray, tf_hours: int) -> np.ndarray:
    """1h bars whose close ends a ``tf_hours`` block aligned to 00:00 UTC."""
    return (np.asarray(t, dtype=np.int64) + HOUR_MS) % (tf_hours * HOUR_MS) == 0


def find_signals(
    t: np.ndarray,
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    atr: np.ndarray,
    lv: Levels,
    *,
    tf_hours: int = 4,
    warmup_h: float = WARMUP_H,
    buf_atr: float = 0.5,
    min_stop: float = 0.005,
    max_stop: float = 0.10,
) -> list[Signal]:
    """Cross-and-close signals of the developing level (see the module docstring)."""
    t = np.asarray(t, dtype=np.int64)
    step = tf_hours * HOUR_MS
    bidx = np.flatnonzero(is_boundary(t, tf_hours))
    pos = {int(t[j]): int(j) for j in bidx}
    out: list[Signal] = []
    for j in bidx:
        jp = pos.get(int(t[j]) - step)
        if jp is None or lv.start[jp] != lv.start[j]:
            continue
        if lv.elapsed_h[j] < warmup_h or not lv.cover_ok[j]:
            continue
        lj, lp = float(lv.level[j]), float(lv.level[jp])
        if not (np.isfinite(lj) and np.isfinite(lp)):
            continue
        prev, now = np.sign(c[jp] - lp), np.sign(c[j] - lj)
        if prev == 0 or now == 0 or prev == now:
            continue
        side = int(now)
        a = int(np.searchsorted(t, int(t[j]) - (2 * tf_hours - 1) * HOUR_MS))
        ext = float(np.min(low[a : j + 1])) if side == 1 else float(np.max(h[a : j + 1]))
        e = float(c[j])
        stp = ext - side * buf_atr * float(atr[j])
        dist = side * (e - stp) / e
        if not (np.isfinite(dist) and min_stop <= dist <= max_stop):
            continue
        out.append(Signal(int(j), int(t[j]) + HOUR_MS, side, e, stp, lj, int(lv.start[j])))
    return out


# ---------------------------------------------------------------- outcomes


def valid_starts(t: np.ndarray, n_hold: int) -> np.ndarray:
    """Indices i whose ``n_hold`` bars i .. i + n_hold - 1 exist and are consecutive hours."""
    if t.size < n_hold:
        return np.zeros(0, dtype=np.int64)
    i = np.arange(t.size - n_hold + 1)
    return i[t[i + n_hold - 1] - t[i] == (n_hold - 1) * HOUR_MS].astype(np.int64)


def risk_unit(entry: np.ndarray, stop: np.ndarray, fees: Fees) -> np.ndarray:
    return np.abs(entry - stop) + fees.risk_taker * (np.abs(entry) + np.abs(stop))


def vwap_exit_outcomes(
    t: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    i0: np.ndarray,
    side: np.ndarray,
    entry: np.ndarray,
    stop: np.ndarray,
    level: np.ndarray,
    n_hold: int,
    fees: Fees,
    tf_hours: int = 4,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """R net (without funding), the exit bar index and the exit kind (0 time, 1 stop / gap, 3 close through the level)
    of trades held over the hole-free 1h bars i0 .. i0 + n_hold - 1. Entry fee ``fees.maker`` (the runner passes the
    taker rate), every exit ``fees.taker_exit``. Shorts run on negated prices."""
    n = i0.size
    if n == 0:
        return np.zeros(0), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    idx = i0[:, None] + np.arange(n_hold)[None, :]
    sg = side.astype(float)[:, None]
    lng = sg > 0
    mo = o[idx] * sg
    ml = np.where(lng, low[idx], -h[idx])
    mc = c[idx] * sg
    e, st, lv = entry * side, stop * side, level * side
    gap = mo <= st[:, None]
    hs = ml <= st[:, None]
    inv = is_boundary(t[idx], tf_hours) & (mc < lv[:, None])
    ev = gap | hs | inv
    anyev = ev.any(axis=1)
    k = np.where(anyev, ev.argmax(axis=1), n_hold - 1)
    rows = np.arange(n)
    g, s_ = gap[rows, k], hs[rows, k]
    px = np.where(~anyev, mc[:, -1], np.where(g, mo[rows, k], np.where(s_, st, mc[rows, k])))
    kind = np.where(~anyev, KIND_TIME, np.where(g | s_, KIND_STOP, KIND_INVALID)).astype(np.int64)
    ru = risk_unit(entry, stop, fees)
    r = (px - e) / ru - (fees.maker * np.abs(e) + fees.taker_exit * np.abs(px)) / ru
    return r, (i0 + k).astype(np.int64), kind


def funding_r(
    t_in: np.ndarray, t_exit: np.ndarray, side: np.ndarray, entry: np.ndarray, ru: np.ndarray, f_t: np.ndarray, f_rate: np.ndarray
) -> np.ndarray:
    """Funding of the hold in R: settlements with t_in < time <= t_exit; a long pays a positive rate, a short receives
    it (rate x entry notional per unit, divided by the risk unit). No settlements known -> 0."""
    if f_t.size == 0 or t_in.size == 0:
        return np.zeros(t_in.size)
    cs = np.concatenate([[0.0], np.cumsum(f_rate)])
    a = np.searchsorted(f_t, t_in, side="right")
    b = np.searchsorted(f_t, t_exit, side="right")
    return -side * (cs[b] - cs[a]) * entry / ru


def funding_covered(t_in: np.ndarray, t_exit: np.ndarray, f_t: np.ndarray, max_gap_h: float = 8.0) -> np.ndarray:
    """Per trade: the settlement history covers the hold, i.e. the first known settlement is at or before the entry and
    the last at or after ``t_exit - max_gap_h`` hours. False for every trade when no settlement is known (funding 0)."""
    t_in = np.asarray(t_in, dtype=np.int64)
    t_exit = np.asarray(t_exit, dtype=np.int64)
    if f_t.size == 0:
        return np.zeros(t_in.size, dtype=bool)
    return (int(f_t[0]) <= t_in) & (int(f_t[-1]) >= t_exit - int(max_gap_h * HOUR_MS))


def overlaps_opposite(t_in: np.ndarray, t_exit: np.ndarray, side: np.ndarray) -> np.ndarray:
    """Per trade: a trade of the other side on the same symbol is open at the same time (the intervals
    [t_in, t_exit) intersect). Such pairs cannot coexist on a one-way account."""
    t_in = np.asarray(t_in, dtype=np.int64)
    t_exit = np.asarray(t_exit, dtype=np.int64)
    side = np.asarray(side)
    out = np.zeros(t_in.size, dtype=bool)
    for sd in (1, -1):
        me, op = np.flatnonzero(side == sd), np.flatnonzero(side == -sd)
        if me.size == 0 or op.size == 0:
            continue
        o = op[np.argsort(t_in[op], kind="stable")]
        pmax = np.maximum.accumulate(t_exit[o])  # latest exit among opposite trades entered so far
        k = np.searchsorted(t_in[o], t_exit[me], side="left")  # opposite trades entered strictly before my exit
        hit = k > 0
        res = np.zeros(me.size, dtype=bool)
        res[hit] = pmax[k[hit] - 1] > t_in[me][hit]
        out[me] = res
    return out


def paired_placebo_diff(real: np.ndarray, placebo: np.ndarray) -> np.ndarray:
    """Per trade: the real R minus the mean of its own placebo results (row of ``placebo``, nan sets skipped); nan
    where a trade has no finite placebo result. The mean of this difference, with a cluster CI, is the placebo test."""
    pl = np.asarray(placebo, dtype=float)
    if pl.ndim != 2 or pl.shape[0] != np.asarray(real).size:
        raise ValueError("placebo must be a (n_trades, n_sets) matrix")
    cnt = np.isfinite(pl).sum(axis=1)
    mean = np.where(cnt > 0, np.nansum(pl, axis=1) / np.maximum(cnt, 1), np.nan)
    return np.asarray(real, dtype=float) - mean


def boundary_starts(t: np.ndarray, n_hold: int, tf_hours: int = 4) -> np.ndarray:
    """Placebo start candidates: hole-free hold paths that start right after a ``tf_hours`` close (t % tf == 0)."""
    v = valid_starts(t, n_hold)
    return v[np.asarray(t, dtype=np.int64)[v] % (tf_hours * HOUR_MS) == 0]
