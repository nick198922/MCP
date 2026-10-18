"""Order-book liquidity exit (IDEA-20261017-market-makers, part A): daily book-depth features from Binance ``bookDepth``
snapshots, causal "liquidity exit / entry" events and forward outcomes.

Pure functions, no I/O. Daily series are aligned arrays indexed by UTC day (index t = day t; values of day t are known
at its close, 24:00 UTC). Missing days are NaN. Rates and returns are decimals (log returns; 0.01 = 1 %).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

SNAPSHOTS_PER_DAY = 2880  # one every 30 s


def depth_day_features(ts: np.ndarray, pct: np.ndarray, notional: np.ndarray, levels: Sequence[float] = (1.0, 2.0)) -> dict[str, float]:
    """Daily features of one symbol-day of book snapshots.

    ``ts`` snapshot time per row, ``pct`` signed level (-L bids, +L asks), ``notional`` cumulative USD within L %.
    Returns ``n_snap`` (snapshots with both sides at every requested level) and, per level L, ``bid{L}``, ``ask{L}``
    (means over those snapshots), ``d{L}`` = mean of bid + ask, ``d{L}_med`` = median of bid + ask (USD). Levels are
    formatted with ``g`` (1.0 -> "1", 0.2 -> "0.2")."""
    ts = np.asarray(ts)
    pct = np.asarray(pct, dtype=float)
    val = np.asarray(notional, dtype=float)
    out: dict[str, float] = {"n_snap": 0.0}
    if ts.size == 0:
        for L in levels:
            k = f"{L:g}"
            out.update({f"bid{k}": np.nan, f"ask{k}": np.nan, f"d{k}": np.nan, f"d{k}_med": np.nan})
        return out
    uniq, idx = np.unique(ts, return_inverse=True)
    n = uniq.size
    ok = np.ones(n, dtype=bool)
    sides: dict[tuple[float, int], np.ndarray] = {}
    for L in levels:
        for sgn in (-1, 1):
            m = np.isclose(pct, sgn * L)
            cnt = np.bincount(idx[m], minlength=n)
            s = np.bincount(idx[m], weights=val[m], minlength=n)
            ok &= cnt == 1
            sides[(L, sgn)] = s
    out["n_snap"] = float(ok.sum())
    for L in levels:
        k = f"{L:g}"
        if not ok.any():
            out.update({f"bid{k}": np.nan, f"ask{k}": np.nan, f"d{k}": np.nan, f"d{k}_med": np.nan})
            continue
        b, a = sides[(L, -1)][ok], sides[(L, 1)][ok]
        out[f"bid{k}"] = float(b.mean())
        out[f"ask{k}"] = float(a.mean())
        out[f"d{k}"] = float((b + a).mean())
        out[f"d{k}_med"] = float(np.median(b + a))
    return out


def trailing_mean(x: np.ndarray, window: int, min_count: int) -> np.ndarray:
    """out[t] = nanmean(x[t-window+1 .. t]) when at least ``min_count`` values are finite, else NaN (causal)."""
    x = np.asarray(x, dtype=float)
    fin = np.isfinite(x)
    cs = np.concatenate(([0.0], np.cumsum(np.where(fin, x, 0.0))))
    cn = np.concatenate(([0], np.cumsum(fin)))
    hi = np.arange(1, x.size + 1)
    lo = np.maximum(0, hi - window)
    cnt = cn[hi] - cn[lo]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (cs[hi] - cs[lo]) / cnt
    out[cnt < max(1, min_count)] = np.nan
    return out


def normalized_depth(depth: np.ndarray, qv: np.ndarray, *, vol_days: int = 30, min_days: int = 20) -> np.ndarray:
    """Depth divided by the trailing ``vol_days`` mean daily quote volume (days t-vol_days+1 .. t). Unitless:
    "USD resting within L % per USD traded per day". Causal: uses nothing after day t."""
    v = trailing_mean(qv, vol_days, min_days)
    d = np.asarray(depth, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = d / v
    out[~(v > 0)] = np.nan
    return out


@dataclass(frozen=True)
class EventParams:
    ratio: float = 0.5  # exit: norm < ratio * baseline; entry (direction=+1): norm > ratio * baseline
    run: int = 2  # consecutive days
    base_days: int = 30  # baseline median over the base_days days before the run
    min_base: int = 20
    vol_days: int = 30  # sigma of daily log returns over the vol_days days before the run
    z_max: float = 1.0  # |log return over the run| < z_max * sigma * sqrt(run)
    cooldown: int = 14  # no new event for the same symbol within cooldown days after one
    direction: int = -1  # -1 = liquidity exit (drop), +1 = liquidity entry (rise)


def _below(x: float, thr: float, direction: int) -> bool:
    return x < thr if direction < 0 else x > thr


def detect_events(norm: np.ndarray, close: np.ndarray, p: EventParams = EventParams()) -> list[int]:
    """Indices t (last day of the run) of liquidity events; entry is at the close of day t.

    Event at t when, with run days r = t-run+1 .. t:
      * baseline B = median of finite ``norm`` over the ``base_days`` days before r (>= ``min_base`` values);
      * every run day has finite norm beyond ``ratio * B`` (below for exits, above for entries);
      * onset: day r-1 is not beyond the threshold (finite or not);
      * |ln(close[t] / close[r-1])| < z_max * sigma * sqrt(run), sigma = sd of daily log returns over the
        ``vol_days`` days before r (>= ``min_base`` returns);
      * no event in the previous ``cooldown`` days.
    Only data up to day t is used, so an event at t never depends on later data."""
    norm = np.asarray(norm, dtype=float)
    c = np.asarray(close, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.full(c.size, np.nan)
        lr[1:] = np.log(c[1:] / c[:-1])
    events: list[int] = []
    last = -(10**9)
    for t in range(c.size):
        r = t - p.run + 1
        if r - 1 < 0 or t - last <= p.cooldown:
            continue
        base = norm[max(0, r - p.base_days) : r]
        base = base[np.isfinite(base)]
        if base.size < p.min_base:
            continue
        thr = p.ratio * float(np.median(base))
        run = norm[r : t + 1]
        if not np.all(np.isfinite(run)) or not all(_below(float(x), thr, p.direction) for x in run):
            continue
        if np.isfinite(norm[r - 1]) and _below(float(norm[r - 1]), thr, p.direction):
            continue
        rets = lr[max(1, r - p.vol_days) : r]
        rets = rets[np.isfinite(rets)]
        if rets.size < p.min_base or not (np.isfinite(c[t]) and np.isfinite(c[r - 1]) and c[r - 1] > 0):
            continue
        sigma = float(np.std(rets, ddof=1))
        move = abs(float(np.log(c[t] / c[r - 1])))
        if not move < p.z_max * sigma * np.sqrt(p.run):
            continue
        events.append(t)
        last = t
    return events


def forward_outcomes(close: np.ndarray, qv: np.ndarray, t: int, horizons: Sequence[int] = (1, 3, 7)) -> dict[str, float]:
    """From the close of day t: ``r{h}`` = ln(close[t+h] / close[t]); ``rv{h}`` = sqrt(mean of squared daily log returns
    over days t+1..t+h) (daily scale); ``vq{h}`` = ln(mean qv over t+1..t+h / mean qv over t-29..t). NaN if missing."""
    c = np.asarray(close, dtype=float)
    q = np.asarray(qv, dtype=float)
    out: dict[str, float] = {}
    past = q[max(0, t - 29) : t + 1]
    past_m = float(np.nanmean(past)) if np.isfinite(past).sum() >= 20 else np.nan
    for h in horizons:
        if t + h >= c.size or not np.all(np.isfinite(c[t : t + h + 1])) or np.any(c[t : t + h + 1] <= 0):
            out.update({f"r{h}": np.nan, f"rv{h}": np.nan, f"vq{h}": np.nan})
            continue
        seg = c[t : t + h + 1]
        d = np.diff(np.log(seg))
        out[f"r{h}"] = float(np.log(seg[-1] / seg[0]))
        out[f"rv{h}"] = float(np.sqrt(np.mean(d * d)))
        fq = q[t + 1 : t + h + 1]
        out[f"vq{h}"] = float(np.log(fq.mean() / past_m)) if past_m > 0 and np.all(np.isfinite(fq)) and fq.mean() > 0 else np.nan
    return out


def match_features(close: np.ndarray, qv: np.ndarray, t: int, run: int = 2, vol_days: int = 30) -> tuple[float, float, float] | None:
    """Pre-event state used by the balanced placebo, all known at the close of day t:
    (z of the run-day log return vs sigma * sqrt(run), 7-day log return, ln(mean qv over the run days / mean qv over
    the 30 days before the run)). None if any input is missing."""
    c = np.asarray(close, dtype=float)
    q = np.asarray(qv, dtype=float)
    r = t - run + 1
    if r - 1 - vol_days < 0 or t - 7 < 0:
        return None
    win = c[r - 1 - vol_days : t + 1]
    if not np.all(np.isfinite(win)) or np.any(win <= 0):
        return None
    lr = np.diff(np.log(c[r - 1 - vol_days : r]))
    sigma = float(np.std(lr, ddof=1))
    if not sigma > 0 or not (np.isfinite(c[t - 7]) and c[t - 7] > 0):
        return None
    z = float(np.log(c[t] / c[r - 1])) / (sigma * np.sqrt(run))
    r7 = float(np.log(c[t] / c[t - 7]))
    qr, qb = q[r : t + 1], q[r - vol_days : r]
    if not (np.all(np.isfinite(qr)) and np.isfinite(qb).sum() >= 20) or qr.mean() <= 0 or np.nanmean(qb) <= 0:
        return None
    return z, r7, float(np.log(qr.mean() / np.nanmean(qb)))


MATCH_SCALES = (1.0, 0.10, 0.50)  # fixed scales for (z, 7d log return, log volume ratio); set before the run


def nearest(target: Sequence[float], cands: np.ndarray, k: int, scales: Sequence[float] = MATCH_SCALES) -> np.ndarray:
    """Row indices of the ``k`` candidates nearest to ``target`` in scaled Euclidean distance (ties: lower index)."""
    cands = np.asarray(cands, dtype=float).reshape(-1, len(scales))
    if cands.shape[0] == 0:
        return np.empty(0, dtype=np.int64)
    d = (((cands - np.asarray(target, dtype=float)) / np.asarray(scales, dtype=float)) ** 2).sum(axis=1)
    return np.argsort(d, kind="stable")[:k]
