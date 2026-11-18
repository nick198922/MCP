"""XO-1, value-area edge rotation (IDEA-20261113-x-traders, item 5; spec «4б» XO-1 of LIB-20261113-x-trader_xo).

A price profile of a window of whole UTC days before day D (the previous day, or a composite of the last W days) gives
VAL / POC / VAH. On day D a long fires at the close of the first 1h bar that closes back inside value below the POC
(VAL < close < POC) after a bar of the last ``k_probe`` bars of day D touched or pierced VAL (low <= VAL); the stop is
under the probe's low minus ``buf_atr`` x ATR(14, 1h), the target the POC. Shorts mirror at VAH. One long and one short
at most per symbol, day and profile: the first bar meeting the price conditions is the signal, and when its stop
distance is outside [min_stop, max_stop] the side is skipped for the day (no search for a better bar).

The profile is a TPO (time) profile by default: every 1h bar adds one count to every price row its [low, high] touches
(rows: the window's range in ``N_ROWS`` equal steps); the value area (70 % of the counts) grows from the POC without a
side bias (``value_area_sym``). A volume profile (quote volume spread over each bar's range) is the registered variant.

Outcomes: ``barrier_outcomes`` is a vectorised ``stop_management.simulate_stop_rule`` without a stop move (rule A) on
hole-free 1h paths: a bar opening at or beyond the stop exits at the open, a bar touching the stop exits at the stop and
the stop wins a bar that also touches the target; otherwise the close of the last bar of the hold. R is the live risk
unit (entry-stop distance plus the taker fee on both prices), so R is a return on the trade's risk, not on capital.

Pure functions, no I/O. Every level of day D uses only bars opening before D, every signal only bars up to its own
close, so nothing before time t changes when bars after t change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from tradedesk.analytics.s16_modules import VA_SHARE, volume_profile
from tradedesk.analytics.stop_management import Fees

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
N_ROWS = 48  # price rows of a profile over the window's high-low range
MIN_COVER = 0.9  # share of the window's hours that must have a bar, else no levels for the day
BALANCE_OVERLAP = 0.5  # balance: VA(D-1) and VA(D-2) overlap by >= half of the narrower one


# ---------------------------------------------------------------- profiles


def tpo_profile(low: np.ndarray, high: np.ndarray, n_rows: int = N_ROWS) -> tuple[np.ndarray, np.ndarray]:
    """Row edges over [min(low), max(high)] in ``n_rows`` equal steps and the TPO count per row: a bar counts once in
    every row whose closed interval [edge_k, edge_k+1] meets its [low, high]."""
    lo, hi = float(np.min(low)), float(np.max(high))
    if not hi > lo:
        return np.array([lo, hi]), np.array([float(low.size)])
    edges = np.linspace(lo, hi, n_rows + 1)
    touch = (high[:, None] >= edges[None, :-1]) & (low[:, None] <= edges[None, 1:])
    return edges, touch.sum(axis=0).astype(float)


def value_area_sym(edges: np.ndarray, cnt: np.ndarray, share: float = VA_SHARE) -> tuple[float, float, float]:
    """(VAL, POC price, VAH) without a side bias: the POC is the largest row closest to the middle of the profile
    (lower one on an exact tie); the area grows by the neighbouring row with the larger count, by both rows when they
    are equal, until ``share`` of the counts. Integer TPO counts tie often, so the tie rules matter."""
    tot = float(np.sum(cnt))
    if not tot > 0:
        return math.nan, math.nan, math.nan
    mids = 0.5 * (edges[:-1] + edges[1:])
    centre = 0.5 * (edges[0] + edges[-1])
    top = np.flatnonzero(cnt == np.max(cnt))
    p = int(top[np.argmin(np.abs(mids[top] - centre))])
    a = b = p
    acc = float(cnt[p])
    while acc < share * tot - 1e-12 * tot:
        up = float(cnt[b + 1]) if b + 1 < cnt.size else -1.0
        dn = float(cnt[a - 1]) if a - 1 >= 0 else -1.0
        if up < 0 and dn < 0:
            break
        if up >= dn and up >= 0:
            b += 1
            acc += up
        if dn >= up and dn >= 0:
            a -= 1
            acc += dn
    return float(edges[a]), float(mids[p]), float(edges[b + 1])


def profile_levels(low: np.ndarray, high: np.ndarray, vol: np.ndarray | None = None, n_rows: int = N_ROWS) -> tuple[float, float, float]:
    """(VAL, POC, VAH) of the bars: TPO when ``vol`` is None, else the volume profile on the same rows."""
    if low.size == 0:
        return math.nan, math.nan, math.nan
    if vol is None:
        edges, cnt = tpo_profile(low, high, n_rows)
    else:
        lo, hi = float(np.min(low)), float(np.max(high))
        if not hi > lo:
            return math.nan, math.nan, math.nan
        edges, cnt = volume_profile(low, high, vol, (hi - lo) / n_rows)
    return value_area_sym(edges, cnt)


@dataclass(frozen=True)
class Levels:
    day: np.ndarray  # open ms of day D (the levels are known from D 00:00 UTC)
    val: np.ndarray
    poc: np.ndarray
    vah: np.ndarray


def day_levels(
    t: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    window_days: int,
    *,
    vol: np.ndarray | None = None,
    n_rows: int = N_ROWS,
    min_cover: float = MIN_COVER,
) -> Levels:
    """Levels of every UTC day D from the bars opening in [D - window_days, D); NaN when fewer than ``min_cover`` of
    the window's hours have a bar. ``t`` are sorted 1h open times."""
    if t.size == 0:
        z = np.zeros(0)
        return Levels(np.zeros(0, dtype=np.int64), z, z, z)
    d0 = (int(t[0]) // DAY_MS + window_days) * DAY_MS
    d1 = (int(t[-1]) // DAY_MS + 1) * DAY_MS
    days = np.arange(d0, d1 + 1, DAY_MS, dtype=np.int64)
    out = np.full((days.size, 3), np.nan)
    need = min_cover * 24 * window_days
    a = np.searchsorted(t, days - window_days * DAY_MS)
    b = np.searchsorted(t, days)
    for k in range(days.size):
        i0, i1 = int(a[k]), int(b[k])
        if i1 - i0 < need:
            continue
        out[k] = profile_levels(low[i0:i1], high[i0:i1], None if vol is None else vol[i0:i1], n_rows)
    return Levels(days, out[:, 0], out[:, 1], out[:, 2])


def balance_flags(daily: Levels, overlap: float = BALANCE_OVERLAP) -> dict[int, bool]:
    """Day D -> True when the value areas of D-1 and D-2 (``daily`` = 1-day levels, VA of day D-1 stored at D) overlap
    by at least ``overlap`` of the narrower one (balance, «overlapping value»)."""
    out: dict[int, bool] = {}
    for k in range(1, daily.day.size):
        lo1, hi1, lo2, hi2 = daily.val[k], daily.vah[k], daily.val[k - 1], daily.vah[k - 1]
        if not all(np.isfinite([lo1, hi1, lo2, hi2])) or daily.day[k] - daily.day[k - 1] != DAY_MS:
            continue
        narrow = min(hi1 - lo1, hi2 - lo2)
        inter = min(hi1, hi2) - max(lo1, lo2)
        out[int(daily.day[k])] = bool(narrow > 0 and inter >= overlap * narrow)
    return out


# ---------------------------------------------------------------- signals


def atr_sma(h: np.ndarray, low: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    """Causal ATR: mean true range of the last ``n`` bars ending at each bar; NaN for bars 0 .. n - 1 (bar 0 has no
    previous close, so the first full window ends at bar n)."""
    tr = np.full(h.size, np.nan)
    if h.size > 1:
        pc = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - low[1:], np.abs(h[1:] - pc), np.abs(low[1:] - pc)])
    out = np.full(h.size, np.nan)
    if h.size > n:
        cs = np.concatenate([[0.0], np.cumsum(tr[1:])])  # cs[j] = sum tr[1..j]
        j = np.arange(n, h.size)
        out[n:] = (cs[j] - cs[j - n]) / n
    return out


@dataclass(frozen=True)
class Signal:
    i: int  # index of the signal bar (its close is the entry)
    t_in: int  # entry time = close of the signal bar
    side: int  # +1 long, -1 short
    entry: float
    stop: float
    target: float  # the POC
    target2: float  # the opposite edge of value (variant)
    day: int  # open ms of day D


def find_signals(
    t: np.ndarray,
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    atr: np.ndarray,
    lv: Levels,
    *,
    k_probe: int = 3,
    buf_atr: float = 0.5,
    min_stop: float = 0.005,
    max_stop: float = 0.10,
) -> list[Signal]:
    """Signals of every day with finite levels VAL < POC < VAH (see the module docstring)."""
    out: list[Signal] = []
    for k in range(lv.day.size):
        val, poc, vah = float(lv.val[k]), float(lv.poc[k]), float(lv.vah[k])
        if not (np.isfinite(val) and np.isfinite(vah) and val < poc < vah):
            continue
        d = int(lv.day[k])
        a, b = int(np.searchsorted(t, d)), int(np.searchsorted(t, d + DAY_MS))
        done = {1: False, -1: False}
        for j in range(a, b):
            w0 = max(a, j - k_probe + 1)
            for side in (1, -1):
                if done[side]:
                    continue
                if side == 1:
                    ext = float(np.min(low[w0 : j + 1]))
                    hit = ext <= val and val < c[j] < poc
                else:
                    ext = float(np.max(h[w0 : j + 1]))
                    hit = ext >= vah and poc < c[j] < vah
                if not hit:
                    continue
                done[side] = True
                e = float(c[j])
                st = ext - side * buf_atr * float(atr[j])
                dist = side * (e - st) / e
                if not (np.isfinite(dist) and min_stop <= dist <= max_stop):
                    continue
                out.append(Signal(j, int(t[j]) + HOUR_MS, side, e, st, poc, vah if side == 1 else val, d))
            if done[1] and done[-1]:
                break
    return out


# ---------------------------------------------------------------- outcomes


def valid_starts(t: np.ndarray, n_hold: int) -> np.ndarray:
    """Indices i whose ``n_hold`` bars i .. i + n_hold - 1 exist and are consecutive hours."""
    if t.size < n_hold:
        return np.zeros(0, dtype=np.int64)
    i = np.arange(t.size - n_hold + 1)
    return i[t[i + n_hold - 1] - t[i] == (n_hold - 1) * HOUR_MS].astype(np.int64)


def barrier_outcomes(
    o: np.ndarray,
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    i0: np.ndarray,
    side: np.ndarray,
    entry: np.ndarray,
    stop: np.ndarray,
    target: np.ndarray,
    n_hold: int,
    fees: Fees,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """R net, the exit bar index and the exit kind (0 time, 1 stop / gap, 2 target) of trades held over 1h bars
    i0 .. i0 + n_hold - 1 (the caller passes hole-free starts, ``valid_starts``). Shorts (side -1) run on negated
    prices. Same conventions and fees as ``simulate_stop_rule`` with rule A."""
    n = i0.size
    if n == 0:
        z = np.zeros(0)
        return z, np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    idx = i0[:, None] + np.arange(n_hold)[None, :]
    sg = side.astype(float)[:, None]
    lng = sg > 0
    mo = o[idx] * sg
    mh = np.where(lng, h[idx], -low[idx])
    ml = np.where(lng, low[idx], -h[idx])
    mc = c[idx] * sg
    e, st, tg = entry * side, stop * side, target * side
    gap = mo <= st[:, None]
    hs = ml <= st[:, None]
    ht = mh >= tg[:, None]
    ev = gap | hs | ht
    anyev = ev.any(axis=1)
    k = np.where(anyev, ev.argmax(axis=1), n_hold - 1)
    rows = np.arange(n)
    g, s_ = gap[rows, k], hs[rows, k]
    px = np.where(~anyev, mc[:, -1], np.where(g, mo[rows, k], np.where(s_, st, tg)))
    kind = np.where(~anyev, 0, np.where(g | s_, 1, 2)).astype(np.int64)
    ru = (e - st) + fees.risk_taker * (np.abs(e) + np.abs(st))
    fee_out = np.where(kind == 2, fees.maker, fees.taker_exit)
    r = (px - e) / ru - (fees.maker * np.abs(e) + fee_out * np.abs(px)) / ru
    return r, (i0 + k).astype(np.int64), kind


def placebo_starts(valid: np.ndarray, i0: np.ndarray, half_window: int, n_perm: int, rng: np.random.Generator) -> np.ndarray:
    """(n, n_perm) random start indices drawn uniformly from ``valid`` (sorted) within [i0 - half_window, i0 +
    half_window] of each trade; -1 where that window has no valid start."""
    lo = np.searchsorted(valid, i0 - half_window)
    hi = np.searchsorted(valid, i0 + half_window, side="right")
    cnt = hi - lo
    u = rng.random((i0.size, n_perm))
    pos = lo[:, None] + np.floor(u * np.maximum(cnt, 1)[:, None]).astype(np.int64)
    pos = np.minimum(pos, np.maximum(hi - 1, 0)[:, None])
    out = valid[np.clip(pos, 0, max(valid.size - 1, 0))] if valid.size else np.full(pos.shape, -1)
    out = np.where((cnt > 0)[:, None], out, -1)
    return out.astype(np.int64)


def non_overlapping(t_in: np.ndarray, t_exit: np.ndarray) -> np.ndarray:
    """Greedy one-position-at-a-time flag in entry order: a trade is taken when it enters at or after the exit of the
    last taken trade (one symbol, one profile)."""
    order = np.argsort(t_in, kind="stable")
    keep = np.zeros(t_in.size, dtype=bool)
    free = -1
    for j in order:
        if t_in[j] >= free:
            keep[j] = True
            free = int(t_exit[j])
    return keep
