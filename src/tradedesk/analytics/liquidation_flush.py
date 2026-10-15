"""Liquidation-flush overshoot study: event detection, point-in-time universe, 1m trade simulation, clustered stats.

Pure functions, no I/O. Time is always a parameter (epoch milliseconds, UTC). Returns are decimal fractions on
notional (0.01 = 1 %). Costs are round-trip fractions of notional (0.003 = taker 0.05 % x 2 + slippage 0.10 % x 2).

Event (pre-registered in docs/ideas/IDEA-20261013-liquidation-flush-overshoot.md):
  r_t = close_t / close_{t-1} - 1 with bar t-1 exactly one bar earlier;
  W = quote volumes of bars opened in [t - lookback, t) (strictly before t), at least ``min_lookback_bars`` of them;
  z_t = (QV_t - median(W)) / (1.4826 * MAD(W));
  event when |r_t| >= ret_threshold and z_t > z_threshold; r_t < 0 -> long (+1), r_t > 0 -> short (-1);
  after an accepted event, the same symbol's next one is accepted only ``cooldown_ms`` later (bar open to bar open).
"""

from __future__ import annotations

import math
import random
import statistics
import zlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

MIN_MS = 60_000
FIVE_MIN_MS = 5 * MIN_MS
HOUR_MS = 60 * MIN_MS
DAY_MS = 24 * HOUR_MS
MAD_SCALE = 1.4826

STABLE_OR_WRAPPED = frozenset(
    {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "USD1", "RLUSD", "PYUSD", "EUR", "AEUR"}
    | {"WBTC", "WBETH", "WETH", "STETH", "BTCDOM", "DEFI", "FOOTBALL", "BLUEBIRD"}
    | {"PAXG", "XAUT"}  # gold-backed tokens: TradFi exposure under a COIN label
)


# ---------------------------------------------------------------- event detection


def robust_z(window: Sequence[float] | np.ndarray, x: float) -> float | None:
    """(x - median) / (1.4826 * MAD) of ``window``; None when the window is empty or MAD is 0."""
    w = np.asarray(window, dtype=float)
    if w.size == 0:
        return None
    med = float(np.median(w))
    mad = float(np.median(np.abs(w - med)))
    if mad <= 0:
        return None
    return (x - med) / (MAD_SCALE * mad)


def detect_flush_events(
    ts: Sequence[int] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    qv: Sequence[float] | np.ndarray,
    *,
    low: Sequence[float] | np.ndarray | None = None,
    high: Sequence[float] | np.ndarray | None = None,
    ret_threshold: float = 0.04,
    z_threshold: float = 5.0,
    lookback_ms: int = 7 * DAY_MS,
    min_lookback_bars: int = 1500,
    cooldown_ms: int = 4 * HOUR_MS,
    bar_ms: int = FIVE_MIN_MS,
    eligible: Callable[[int], bool] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Flush events on one symbol's bars (sorted by open time). Uses only bars opened at or before each event bar.

    ``eligible(open_ms)`` restricts events (e.g. symbol in the point-in-time universe that month); ineligible
    candidates do not start a cooldown. ``start_ms``/``end_ms`` bound the event bar's open time ``[start, end)``."""
    t = np.asarray(ts, dtype=np.int64)
    c = np.asarray(close, dtype=float)
    v = np.asarray(qv, dtype=float)
    n = t.size
    if n < 2:
        return []
    r = np.full(n, np.nan)
    contiguous = (t[1:] - t[:-1]) == bar_ms
    with np.errstate(divide="ignore", invalid="ignore"):
        r[1:] = np.where(contiguous & (c[:-1] > 0), c[1:] / c[:-1] - 1.0, np.nan)
    cand = np.nonzero(np.abs(np.nan_to_num(r, nan=0.0)) >= ret_threshold)[0]
    out: list[dict[str, Any]] = []
    last_ts: int | None = None
    for i in cand:
        ti = int(t[i])
        if start_ms is not None and ti < start_ms:
            continue
        if end_ms is not None and ti >= end_ms:
            continue
        if last_ts is not None and ti - last_ts < cooldown_ms:
            continue
        if eligible is not None and not eligible(ti):
            continue
        lo = int(np.searchsorted(t, ti - lookback_ms, side="left"))
        if i - lo < min_lookback_bars:
            continue
        z = robust_z(v[lo:i], float(v[i]))
        if z is None or not z > z_threshold:
            continue
        ri = float(r[i])
        ev = {
            "ts": ti,
            "signal_ts": ti + bar_ms,
            "ret": ri,
            "z": z,
            "direction": 1 if ri < 0 else -1,
            "close": float(c[i]),
        }
        if low is not None:
            ev["low"] = float(np.asarray(low, dtype=float)[i])
        if high is not None:
            ev["high"] = float(np.asarray(high, dtype=float)[i])
        out.append(ev)
        last_ts = ti
    return out


# ---------------------------------------------------------------- point-in-time universe


def month_start(ms: int) -> int:
    d = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return int(datetime(d.year, d.month, 1, tzinfo=UTC).timestamp() * 1000)


def month_starts(start_ms: int, end_ms: int) -> list[int]:
    """UTC month starts m with start_ms <= m < end_ms (start_ms is floored to its month)."""
    out = []
    d = datetime.fromtimestamp(month_start(start_ms) / 1000, tz=UTC)
    while True:
        ms = int(d.timestamp() * 1000)
        if ms >= end_ms:
            return out
        out.append(ms)
        d = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=UTC)


def base_of(symbol: str, quote: str = "USDT") -> str:
    b = symbol[: -len(quote)] if symbol.endswith(quote) else symbol
    for mult in ("1000000", "1000", "1M"):
        if b.startswith(mult) and len(b) > len(mult):
            return b[len(mult) :]
    return b


def symbol_eligible(symbol: str, underlying_type: str | None) -> bool:
    """USDT-margined linear perp of a crypto coin: not a dated future, not TradFi/index, not a stable or wrapped token.

    ``underlying_type`` comes from the venue's current exchange info (None for symbols no longer listed)."""
    if not symbol.endswith("USDT") or "_" in symbol or len(symbol) <= 4:
        return False
    if underlying_type is not None and underlying_type != "COIN":
        return False
    return base_of(symbol) not in STABLE_OR_WRAPPED


def monthly_universe(
    daily_qv: Mapping[str, Sequence[tuple[int, float]]],
    month_ms: int,
    *,
    top_n: int = 50,
    window_days: int = 30,
    min_days: int = 20,
) -> list[str]:
    """Top ``top_n`` symbols by quote volume summed over the ``window_days`` full UTC days before ``month_ms``.

    ``daily_qv[symbol]`` = [(day_open_ms, quote_volume), ...]. Only days strictly before ``month_ms`` count, so the
    universe of a month never depends on that month's data. Ties break by symbol name."""
    lo = month_ms - window_days * DAY_MS
    ranked = []
    for sym, rows in daily_qv.items():
        vals = [q for d, q in rows if lo <= d < month_ms]
        if len(vals) >= min_days:
            ranked.append((-sum(vals), sym))
    ranked.sort()
    return [s for _, s in ranked[:top_n]]


# ---------------------------------------------------------------- trade simulation on 1m bars


def _bar(m1: Mapping[int, Sequence[float]], ts: int) -> Sequence[float] | None:
    return m1.get(ts)


def simulate_trade(
    m1: Mapping[int, Sequence[float]],
    entry_ts: int,
    direction: int,
    horizon_ms: int,
    *,
    cost: float = 0.003,
    worst_entry: bool = False,
    stop: float | None = None,
    max_missing: int = 5,
) -> dict[str, Any] | None:
    """One trade on 1m bars ``m1[open_ms] = (open, high, low, close)``.

    Entry at time ``entry_ts`` = close of the 1m bar opened at ``entry_ts - 1m`` (``worst_entry``: that bar's high
    for a long, low for a short). Exit = close of the 1m bar closing at ``entry_ts + horizon_ms``. With ``stop``
    (fraction, e.g. 0.03) the first bar whose low (long) / high (short) crosses entry*(1 -/+ stop) exits at the stop
    level, or at that bar's open if it opened beyond it. Returns gross/net return on notional, MAE (<= 0) and MFE
    (>= 0), or None when the entry/exit bar is missing or more than ``max_missing`` path bars are missing."""
    eb = _bar(m1, entry_ts - MIN_MS)
    xb = _bar(m1, entry_ts + horizon_ms - MIN_MS)
    if eb is None or xb is None:
        return None
    d = 1 if direction > 0 else -1
    entry = (eb[1] if d > 0 else eb[2]) if worst_entry else eb[3]
    if not entry > 0:
        return None
    exit_px = xb[3]
    mae = mfe = 0.0
    missing = 0
    stopped = False
    level = entry * (1 - stop) if (stop and d > 0) else (entry * (1 + stop) if stop else None)
    for k in range(horizon_ms // MIN_MS):
        b = _bar(m1, entry_ts + k * MIN_MS)
        if b is None:
            missing += 1
            if missing > max_missing:
                return None
            continue
        o, h, lo = b[0], b[1], b[2]
        adverse = (lo / entry - 1) if d > 0 else -(h / entry - 1)
        favour = (h / entry - 1) if d > 0 else -(lo / entry - 1)
        if level is not None:
            hit = lo <= level if d > 0 else h >= level
            if hit:
                gapped = o <= level if d > 0 else o >= level
                exit_px = o if gapped else level
                stopped = True
                mae = min(mae, d * (exit_px / entry - 1))
                mfe = max(mfe, favour)
                break
        mae = min(mae, adverse)
        mfe = max(mfe, favour)
    gross = d * (exit_px / entry - 1)
    return {"entry": entry, "exit": exit_px, "gross": gross, "net": gross - cost, "mae": mae, "mfe": mfe, "stopped": stopped}


def held_extreme(m1: Mapping[int, Sequence[float]], start_ts: int, n_bars: int, direction: int, level: float) -> bool | None:
    """True if the ``n_bars`` 1m bars from ``start_ts`` did not trade beyond ``level`` against the trade (long: no low
    below it; short: no high above it). None if any bar is missing."""
    for k in range(n_bars):
        b = _bar(m1, start_ts + k * MIN_MS)
        if b is None:
            return None
        if (direction > 0 and b[2] < level) or (direction < 0 and b[1] > level):
            return False
    return True


def window_return(opens: Mapping[int, float], t0: int, t1: int) -> float | None:
    """open(t1) / open(t0) - 1 from a {bar_open_ms: open} map; None if either bar is missing."""
    a, b = opens.get(t0), opens.get(t1)
    if not a or b is None:
        return None
    return b / a - 1


# ---------------------------------------------------------------- matched controls


def control_times(
    signal_ts: int,
    key: str,
    *,
    k: int = 3,
    max_shift_days: int = 14,
    blocked: Callable[[int], bool] | None = None,
    seed: int = 20261014,
) -> list[int]:
    """``k`` control signal times at the same UTC minute of day, shifted by +-1..max_shift_days days, drawn without
    replacement with a seed derived from ``key`` and ``signal_ts`` (deterministic, independent of other events).
    Times for which ``blocked(t)`` is true (e.g. near an event of the same symbol) are skipped."""
    rng = random.Random(zlib.crc32(f"{key}|{signal_ts}".encode()) ^ seed)
    shifts = [s for s in range(-max_shift_days, max_shift_days + 1) if s != 0]
    rng.shuffle(shifts)
    out = []
    for s in shifts:
        t = signal_ts + s * DAY_MS
        if blocked is not None and blocked(t):
            continue
        out.append(t)
        if len(out) == k:
            break
    return sorted(out)


# ---------------------------------------------------------------- statistics


def _clusters(values: Sequence[float], clusters: Sequence[Any]) -> tuple[np.ndarray, np.ndarray]:
    sums: dict[Any, float] = {}
    counts: dict[Any, int] = {}
    for v, c in zip(values, clusters, strict=True):
        sums[c] = sums.get(c, 0.0) + v
        counts[c] = counts.get(c, 0) + 1
    keys = sorted(sums)
    return np.array([sums[k] for k in keys]), np.array([counts[k] for k in keys], dtype=float)


def cluster_bootstrap_means(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261014) -> np.ndarray:
    """Bootstrap distribution of the pooled mean, resampling whole clusters (days) with replacement."""
    s, c = _clusters(values, clusters)
    if s.size == 0:
        return np.array([])
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    chunk = max(1, 2_000_000 // max(1, s.size))
    for lo in range(0, n_boot, chunk):
        hi = min(n_boot, lo + chunk)
        idx = rng.integers(0, s.size, size=(hi - lo, s.size))
        out[lo:hi] = s[idx].sum(axis=1) / c[idx].sum(axis=1)
    return out


def cluster_bootstrap_ci(
    values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261014, alpha: float = 0.05
) -> tuple[float, float] | None:
    """Percentile CI of the mean with a cluster (day) bootstrap; None with fewer than 2 clusters."""
    if len(set(clusters)) < 2:
        return None
    b = cluster_bootstrap_means(values, clusters, n_boot=n_boot, seed=seed)
    return float(np.quantile(b, alpha / 2)), float(np.quantile(b, 1 - alpha / 2))


def cluster_bootstrap_p(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261014) -> float | None:
    """One-sided p-value for mean > 0: share of bootstrap means <= 0 (with a +1 correction)."""
    if len(set(clusters)) < 2:
        return None
    b = cluster_bootstrap_means(values, clusters, n_boot=n_boot, seed=seed)
    return float((np.sum(b <= 0) + 1) / (b.size + 1))


def holm(pvalues: Sequence[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (same order as input)."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adj = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adj[i] = running
    return adj


def without_top_days(values: Sequence[float], days: Sequence[Any], k: int) -> tuple[list[float], list[Any], list[Any]]:
    """Drop the ``k`` days with the largest summed value; returns (values, days, dropped days)."""
    tot: dict[Any, float] = {}
    for v, d in zip(values, days, strict=True):
        tot[d] = tot.get(d, 0.0) + v
    drop = [d for d, _ in sorted(tot.items(), key=lambda x: -x[1])[:k]]
    keep = [(v, d) for v, d in zip(values, days, strict=True) if d not in drop]
    return [v for v, _ in keep], [d for _, d in keep], drop


def summarise(values: Sequence[float], days: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261014) -> dict[str, Any]:
    """n, distinct days, mean, median, share > 0, day-clustered 95 % CI and one-sided p of the mean."""
    n = len(values)
    if n == 0:
        return {"n": 0, "days": 0}
    return {
        "n": n,
        "days": len(set(days)),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "win": sum(1 for v in values if v > 0) / n,
        "ci": cluster_bootstrap_ci(values, days, n_boot=n_boot, seed=seed),
        "p": cluster_bootstrap_p(values, days, n_boot=n_boot, seed=seed),
    }


def quantile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return math.nan
    return s[min(len(s) - 1, max(0, int(q * len(s))))]
