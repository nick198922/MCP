"""History test of the daily cycle regime (IDEA-20261028-btc-cycle-regime-gauge, step 3). Pure functions, no I/O.

The states come from `analytics.cycle_regime` unchanged (`_components` + `classify`, VERSION cycle-regime-v1): the state
of day t uses only data up to the close of t. On top of them:

* `state_series`: the state and the components of every daily close in a window;
* `forward_returns`: simple spot return close(t + h days) / close(t) - 1, by bar time (a missing bar gives no return);
* `episodes`: runs of state days, runs separated by at most `merge_gap_days` days merged;
* `masked_diff`: mean(y | mask) - mean(y) (or - mean(y | not mask) with `vs_rest`), the effect statistic;
* `month_block_bootstrap`: circular block bootstrap of calendar months for that statistic;
* `shift_placebo`: the same statistic with the mask shifted circularly by every k in [k_min, n - k_min];
* `trend_residual`: y minus the mean y of the days on the same side of SMA365 (what a state adds over the trend rule);
* `mde` / `t_ci`: power and a small-sample t interval on episode-level values.

Returns are decimals (0.04 = 4 %), on the notional of an unlevered spot position, before costs.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

from . import cycle_regime as cr

DAY_MS = cr.DAY_MS
KEEP = ("close", "price_to_sma", "sma_slope_30d", "drawdown_365d", "dist_30d_low", "rsi14", "basis7", "funding7")


def state_series(
    spot: Mapping[int, float],
    perp: Mapping[int, float],
    funding: Iterable[tuple[int, float]],
    start_open: int,
    end_open: int,
) -> list[dict[str, Any]]:
    """State and components of every spot daily bar with open time in [start_open, end_open], oldest first. Inputs as
    in `cycle_regime.cycle_regime` (closes keyed by bar open ms; funding (ts_ms, rate per interval))."""
    opens = sorted(spot)
    closes = [spot[t] for t in opens]
    fpts = sorted(funding)
    out: list[dict[str, Any]] = []
    for i, t in enumerate(opens):
        if t < start_open or t > end_open:
            continue
        comps = cr._components(opens, closes, i, perp, fpts, {})
        row = {k: comps.get(k) for k in KEEP}
        row["t"] = t
        row["state"] = cr.classify(comps)
        out.append(row)
    return out


def forward_returns(spot: Mapping[int, float], h_days: int, last_open: int) -> dict[int, float]:
    """{open_ms: close(t + h) / close(t) - 1} for every bar whose bar h days later exists and opens <= last_open."""
    out: dict[int, float] = {}
    for t, c in spot.items():
        t2 = t + h_days * DAY_MS
        if t2 <= last_open and t2 in spot and c > 0:
            out[t] = spot[t2] / c - 1.0
    return out


def episodes(days: Sequence[int], flags: Sequence[bool], merge_gap_days: int) -> list[tuple[int, int, int]]:
    """Episodes of a state: (first_day, last_day, n_state_days) over bar opens `days` (sorted ms). A new episode starts
    when more than `merge_gap_days` days passed since the last state day."""
    out: list[list[int]] = []
    for t, f in zip(days, flags, strict=True):
        if not f:
            continue
        if out and (t - out[-1][1]) <= (merge_gap_days + 1) * DAY_MS:
            out[-1][1] = t
            out[-1][2] += 1
        else:
            out.append([t, t, 1])
    return [(a, b, n) for a, b, n in out]


def masked_diff(y: Sequence[float] | np.ndarray, mask: Sequence[bool] | np.ndarray, *, vs_rest: bool = False) -> float | None:
    """mean(y over mask) - mean(y over all) (or over not-mask with `vs_rest`); None when a side is empty."""
    ya = np.asarray(y, dtype=float)
    m = np.asarray(mask, dtype=bool)
    if not m.any() or (vs_rest and m.all()) or ya.size == 0:
        return None
    base = ya[~m].mean() if vs_rest else ya.mean()
    return float(ya[m].mean() - base)


def month_index(days: Sequence[int]) -> np.ndarray:
    """0-based calendar month (UTC) of each bar open, counted from the first day's month."""
    ym = [(d.year * 12 + d.month - 1) for d in (datetime.fromtimestamp(t / 1000, UTC) for t in days)]
    first = ym[0] if ym else 0
    return np.asarray([x - first for x in ym], dtype=int)


def month_block_bootstrap(
    y: Sequence[float] | np.ndarray,
    mask: Sequence[bool] | np.ndarray,
    months: Sequence[int] | np.ndarray,
    *,
    block: int,
    reps: int = 5000,
    seed: int = 20261028,
    vs_rest: bool = False,
) -> dict[str, Any]:
    """Circular block bootstrap of calendar months for `masked_diff`: blocks of `block` consecutive months (wrapping
    around), as many as cover the sample's months, days kept whole inside a month. Resamples without a mask day
    (or, with `vs_rest`, without a non-mask day) are dropped and counted (`share_empty`)."""
    ya = np.asarray(y, dtype=float)
    m = np.asarray(mask, dtype=bool)
    mo = np.asarray(months, dtype=int)
    n_m = int(mo.max()) + 1 if mo.size else 0
    if n_m == 0:
        return {"lo": None, "hi": None, "se": None, "share_empty": 1.0, "reps": reps}
    n_all = np.bincount(mo, minlength=n_m).astype(float)
    s_all = np.bincount(mo, weights=ya, minlength=n_m)
    n_in = np.bincount(mo, weights=m.astype(float), minlength=n_m)
    s_in = np.bincount(mo, weights=np.where(m, ya, 0.0), minlength=n_m)
    rng = np.random.default_rng(seed)
    k = math.ceil(n_m / block)
    starts = rng.integers(0, n_m, size=(reps, k))
    pick = ((starts[:, :, None] + np.arange(block)[None, None, :]) % n_m).reshape(reps, -1)[:, :n_m]
    a_n, a_s = n_all[pick].sum(1), s_all[pick].sum(1)
    i_n, i_s = n_in[pick].sum(1), s_in[pick].sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        if vs_rest:
            o_n, o_s = a_n - i_n, a_s - i_s
            stat = i_s / i_n - o_s / o_n
            ok = (i_n > 0) & (o_n > 0)
        else:
            stat = i_s / i_n - a_s / a_n
            ok = i_n > 0
    good = stat[ok]
    if good.size < 2:
        return {"lo": None, "hi": None, "se": None, "share_empty": float(1 - ok.mean()), "reps": reps}
    return {
        "lo": float(np.quantile(good, 0.025)),
        "hi": float(np.quantile(good, 0.975)),
        "se": float(good.std(ddof=1)),
        "share_empty": float(1 - ok.mean()),
        "reps": reps,
    }


def shift_placebo(
    y: Sequence[float] | np.ndarray,
    mask: Sequence[bool] | np.ndarray,
    *,
    k_min: int,
    direction: int,
    vs_rest: bool = False,
) -> dict[str, Any]:
    """Placebo: `masked_diff` with the mask rolled circularly by every k in [k_min, n - k_min]. One-sided p in the
    hypothesis direction (+1: effect > 0, -1: effect < 0): (1 + #{placebo at least as extreme}) / (1 + #shifts)."""
    ya = np.asarray(y, dtype=float)
    m = np.asarray(mask, dtype=bool)
    obs = masked_diff(ya, m, vs_rest=vs_rest)
    n = ya.size
    ks = range(k_min, n - k_min + 1)
    vals = []
    for k in ks:
        v = masked_diff(ya, np.roll(m, k), vs_rest=vs_rest)
        if v is not None:
            vals.append(v)
    if obs is None or not vals:
        return {"obs": obs, "p": None, "n_shifts": len(vals), "placebo_mean": None}
    arr = np.asarray(vals)
    hit = int((direction * arr >= direction * obs - 1e-15).sum())
    return {"obs": obs, "p": (1 + hit) / (1 + arr.size), "n_shifts": int(arr.size), "placebo_mean": float(arr.mean())}


def trend_residual(y: Sequence[float] | np.ndarray, above: Sequence[bool] | np.ndarray) -> np.ndarray:
    """y minus the mean y of the days on the same side of SMA365 (`above` = price >= SMA365)."""
    ya = np.asarray(y, dtype=float)
    a = np.asarray(above, dtype=bool)
    out = ya.copy()
    for side in (True, False):
        sel = a == side
        if sel.any():
            out[sel] = ya[sel] - ya[sel].mean()
    return out


def mde(sd: float, n: int, k: float = 2.8) -> float | None:
    """Minimal detectable mean difference at 5 % two-sided, 80 % power: k x sd / sqrt(n)."""
    return None if n <= 0 else k * sd / math.sqrt(n)


def t975(df: int) -> float:
    """Two-sided 95 % Student t quantile: table for df 1-9, Cornish-Fisher expansion (error < 0.005) for df >= 10."""
    if df <= 0:
        return math.inf
    if df < 10:
        return (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262)[df - 1]
    z = 1.959964
    return (
        z
        + (z**3 + z) / (4 * df)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3)
    )


def t_ci(xs: Sequence[float]) -> dict[str, Any]:
    """Mean, SD and the 95 % t interval of a small sample (None bounds when n < 2)."""
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "lo": None, "hi": None}
    mu = math.fsum(xs) / n
    if n < 2:
        return {"n": 1, "mean": mu, "sd": None, "lo": None, "hi": None}
    sd = math.sqrt(math.fsum((x - mu) ** 2 for x in xs) / (n - 1))
    half = t975(n - 1) * sd / math.sqrt(n)
    return {"n": n, "mean": mu, "sd": sd, "lo": mu - half, "hi": mu + half}
