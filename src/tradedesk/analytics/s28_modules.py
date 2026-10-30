"""S28 modules (IDEA-20261030-demo-setups-depth, item 8 / 7b; registration «Попередня реєстрація модулів S28»).

Pure functions, no I/O; prices in the long (or mirrored) space of the trade unless said otherwise:

* ``mirror_signals`` - H9: ``setup_signals.s28_all`` on mirrored bars (bearish valid order blocks), in the mirrored
  price space (a short in real prices is a long there).
* ``structural_stop`` - H7: the stop under the zone, min(low of the OB candle, low of the engulfing candle) - 0.1 ATR.
* ``confirm_trade`` - H6: the base limit's touch (fill bar k), then an entry at the close of the first 1h bar in
  k + 1 .. k + 6 that closes above the level and above the touch bar's high; stop under the lowest low since the touch.
* ``limit_distance`` / ``fvg_size`` - H5 / H3 features, from bars up to the signal bar only.
* ``spearman`` / ``spearman_ci`` / ``spearman_diff_ci`` - the rank correlation of H5 / H3 with a cluster bootstrap.
* ``hedged_r`` / ``basket_return`` - diagnostic D1: R minus the equal-weight basket's move over the trade in R.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from tradedesk.analytics import setup_signals as ss
from tradedesk.analytics.course_s14_ema import Bars
from tradedesk.analytics.setup_passport import _groups, limit_fill
from tradedesk.analytics.stop_management import Fees, Outcome, StopRule, risk_unit, simulate_stop_rule


def mirror_signals(b: Bars, timeframe: str) -> list[ss.LiveSignal]:
    """H9: S28 on the mirrored bars. Entry / stop / target / indicators are mirrored prices (negate for real ones);
    the detector is causal, so is this."""
    return ss.s28_all(b.mirrored(), timeframe)


def fvg_size(b: Bars, e: int) -> float:
    """H3: the gap of the bullish FVG that gave the order block of engulfing candle ``e`` its FVG flag (middle candle
    e: low[e + 1] - high[e - 1]; else middle e + 1: low[e + 2] - high[e]); NaN if neither is a gap (as
    ``course_fvg_ob.ob_zones``, segments are checked by the detector that produced the zone). Reads bars e - 1 .. e + 2."""
    n = len(b)
    if 1 <= e and e + 1 < n and b.low[e + 1] > b.h[e - 1]:
        return float(b.low[e + 1] - b.h[e - 1])
    if e + 2 < n and b.low[e + 2] > b.h[e]:
        return float(b.low[e + 2] - b.h[e])
    return math.nan


def limit_distance(close_j: float, level: float, atr_j: float) -> float:
    """H5: how far above the level the price closed at the placement, in ATR14 of the signal bar."""
    return (close_j - level) / atr_j if atr_j > 0 else math.nan


def structural_stop(ob_low: float, eng_low: float, atr_e: float, buf_atr: float = 0.1) -> float:
    """H7: the stop under the whole zone (the OB candle and the engulfing candle) with a ``buf_atr`` ATR buffer."""
    return min(ob_low, eng_low) - buf_atr * atr_e


@dataclass(frozen=True)
class Confirm:
    """H6 entry: touch bar k, confirmation bar m (indices into the 1h path), entry / stop / target."""

    k: int
    m: int
    entry: float
    stop: float
    target: float


def confirm_entry(
    path: Bars,
    k: int,
    level: float,
    atr_e: float,
    *,
    max_wait: int = 6,
    buf_atr: float = 0.25,
    target_r: float = 2.0,
    delay: int | None = None,
) -> Confirm | None:
    """H6 after a touch on 1h bar ``k``: the first bar m in k + 1 .. k + max_wait (contiguous 1h bars) closing above
    ``level`` and above high[k]; entry close[m], stop min(low[k .. m]) - buf_atr * atr_e, target ``target_r`` R.
    ``delay`` (the own placebo) forces m = k + delay without the close condition. Reads bars k .. m only."""
    n = len(path)
    if delay is not None:
        m = k + delay
        if delay < 1 or m >= n or int(path.t[m]) - int(path.t[k]) != delay * path.bar_ms:
            return None
    else:
        m = -1
        for q in range(k + 1, min(n, k + max_wait + 1)):
            if int(path.t[q]) - int(path.t[q - 1]) != path.bar_ms:
                break
            if path.c[q] > level and path.c[q] > path.h[k]:
                m = q
                break
        if m < 0:
            return None
    entry = float(path.c[m])
    stop = float(path.low[k : m + 1].min()) - buf_atr * atr_e
    if not stop < entry:
        return None
    return Confirm(k, m, entry, stop, entry + target_r * (entry - stop))


def confirm_trade(
    path: Bars,
    t0: int,
    t1: int,
    level: float,
    atr_e: float,
    hold_ms: int,
    rule: StopRule,
    fees: Fees,
    ok: Callable[[float, float, float], bool] | None = None,
    *,
    delay: int | None = None,
    max_wait: int = 6,
    buf_atr: float = 0.25,
) -> tuple[Outcome | None, Confirm | None, int]:
    """H6 as a trade: (outcome, confirmation, touch bar index). The touch is the base limit's fill bar (low strictly
    below the level in [t0, t1); -1 if none). Without a confirmation, or if ``ok(entry, stop, target)`` (the demo's
    guard) rejects it, there is no trade (None). The trade runs from the close of m, held ``hold_ms``."""
    k = limit_fill(path, t0, t1, level, True)
    if k < 0:
        return None, None, -1
    c = confirm_entry(path, k, level, atr_e, max_wait=max_wait, buf_atr=buf_atr, delay=delay)
    if c is None or (ok is not None and not ok(c.entry, c.stop, c.target)):
        return None, c, k
    t_in = int(path.t[c.m]) + path.bar_ms
    return simulate_stop_rule(path, t_in, c.entry, c.stop, c.target, hold_ms, rule, fees=fees), c, k


# ---------------------------------------------------------------- rank correlation


def ranks(x: Sequence[float]) -> np.ndarray:
    """Ranks 1..n with ties averaged."""
    v = np.asarray(x, dtype=float)
    r = np.empty(v.size)
    if v.size == 0:
        return r
    o = np.argsort(v, kind="mergesort")
    sv = v[o]
    brk = np.concatenate([[True], sv[1:] != sv[:-1]])
    starts = np.flatnonzero(brk)
    ends = np.concatenate([starts[1:], [v.size]]) - 1
    r[o] = ((starts + ends) / 2 + 1)[np.cumsum(brk) - 1]
    return r


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman's rank correlation (Pearson on tie-averaged ranks); NaN if fewer than 3 points or no spread."""
    a, b = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if a.size < 3:
        return math.nan
    ra, rb = ranks(a), ranks(b)
    ra, rb = ra - ra.mean(), rb - rb.mean()
    d = math.sqrt(float((ra * ra).sum() * (rb * rb).sum()))
    return float((ra * rb).sum() / d) if d > 0 else math.nan


def _p_two(b: np.ndarray) -> float:
    b = b[np.isfinite(b)]
    return float(min(1.0, 2 * min((np.sum(b <= 0) + 1) / (b.size + 1), (np.sum(b >= 0) + 1) / (b.size + 1))))


def spearman_ci(
    x: Sequence[float], y: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261030
) -> dict[str, float]:
    """Spearman's rho with a cluster-bootstrap 95 % CI, two-sided p (H0: rho 0) and bootstrap SE."""
    a, b = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    groups = _groups(np.asarray(clusters))
    res: dict[str, float] = {"n": float(a.size), "k": float(len(groups)), "m": spearman(a, b)}
    if a.size < 3 or len(groups) < 2:
        res.update(lo=math.nan, hi=math.nan, p=math.nan, se=math.nan)
        return res
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    for r in range(n_boot):
        idx = np.concatenate([groups[g] for g in rng.integers(0, len(groups), len(groups))])
        out[r] = spearman(a[idx], b[idx])
    ok = out[np.isfinite(out)]
    res.update(lo=float(np.quantile(ok, 0.025)), hi=float(np.quantile(ok, 0.975)), p=_p_two(ok), se=float(ok.std()))
    return res


def spearman_diff_ci(
    x1: Sequence[float],
    y1: Sequence[float],
    c1: Sequence[Any],
    x2: Sequence[float],
    y2: Sequence[float],
    c2: Sequence[Any],
    *,
    n_boot: int = 10_000,
    seed: int = 20261030,
) -> dict[str, float]:
    """rho(x1, y1) - rho(x2, y2), resampling the clusters of both samples together (a placebo shares its signal's
    cluster): difference, 95 % CI, two-sided p, SE."""
    a1, b1, a2, b2 = (np.asarray(v, dtype=float) for v in (x1, y1, x2, y2))
    k1, k2 = np.asarray(c1), np.asarray(c2)
    keys = np.unique(np.concatenate([k1.astype(object), k2.astype(object)]).astype(str))
    pos = {k: i for i, k in enumerate(keys)}
    g1 = np.array([pos[str(k)] for k in k1], dtype=np.int64)
    g2 = np.array([pos[str(k)] for k in k2], dtype=np.int64)
    by1, by2 = [np.flatnonzero(g1 == i) for i in range(keys.size)], [np.flatnonzero(g2 == i) for i in range(keys.size)]
    d0 = spearman(a1, b1) - spearman(a2, b2)
    res: dict[str, float] = {"n1": float(a1.size), "n2": float(a2.size), "d": d0}
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    for r in range(n_boot):
        pick = rng.integers(0, keys.size, keys.size)
        i1 = np.concatenate([by1[g] for g in pick])
        i2 = np.concatenate([by2[g] for g in pick])
        out[r] = spearman(a1[i1], b1[i1]) - spearman(a2[i2], b2[i2])
    ok = out[np.isfinite(out)]
    res.update(lo=float(np.quantile(ok, 0.025)), hi=float(np.quantile(ok, 0.975)), p=_p_two(ok), se=float(ok.std()))
    return res


def one_sided(m: float, p_two: float) -> float:
    """One-sided p for H1: effect > 0 from a two-sided bootstrap p."""
    if not math.isfinite(p_two):
        return math.nan
    return p_two / 2 if m > 0 else 1 - p_two / 2


# ---------------------------------------------------------------- D1 (diagnostic)


def close_at(t: np.ndarray, c: np.ndarray, ms: int) -> float:
    """Close of the 1h bar that ends exactly at ``ms`` (opens at ms - 1h); NaN if that bar is missing."""
    i = int(np.searchsorted(t, ms - 3_600_000))
    return float(c[i]) if i < t.size and int(t[i]) == ms - 3_600_000 else math.nan


def basket_return(closes: Mapping[str, tuple[np.ndarray, np.ndarray]], members: Sequence[str], t0: int, t1: int) -> tuple[float, int]:
    """Equal-weight mean of c(t1) / c(t0) - 1 over ``members`` that have a 1h close ending at both t0 and t1 (members
    fixed before t0 by the caller); (NaN, 0) without any."""
    rets = []
    for s in members:
        tc = closes.get(s)
        if tc is None:
            continue
        a, b = close_at(tc[0], tc[1], t0), close_at(tc[0], tc[1], t1)
        if math.isfinite(a) and math.isfinite(b) and a > 0:
            rets.append(b / a - 1)
    return (float(np.mean(rets)), len(rets)) if rets else (math.nan, 0)


def hedged_r(r_net: float, basket_ret: float, entry: float, stop: float, side: str, fees: Fees = Fees()) -> float:
    """D1: R minus a beta-1 basket position over the trade, in the trade's R (``entry`` / ``stop`` real prices). A long
    loses the basket's rise from its R, a short adds it."""
    sg = 1.0 if side == "long" else -1.0
    ru = risk_unit(sg * entry, sg * stop, fees)
    return r_net - sg * basket_ret * abs(entry) / ru
