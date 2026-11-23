"""KX-4, period-open and previous-week levels swept and reclaimed by the S3 sweep bar (IDEA-20261113-x-traders, item 5;
spec «4б» KX-4 of LIB-20261113-x-killaxbt), measured as a module of S3 H2 together with the XO-4 weekly VWAP.

Levels, all known at the open of the S3 signal (sweep) bar ``ref_ms`` and computed from 1h bars:

* ``YO`` / ``MO`` / ``WO`` - open of the first 1h bar of the calendar year / month / ISO week containing ``ref_ms``
  (nan if that exact bar is missing, e.g. a coin listed later in the period). A level whose period starts at the same
  instant as a longer one is dropped (``MO`` in January, ``WO`` in a week that starts on the 1st of a month), so one
  price never counts as two levels.
* ``PW`` - previous ISO week's low for a long, high for a short (``s3_modules.htf_lows`` on the oriented bars,
  >= 50 % of its hours present).
* ``VW`` - XO-4's developing weekly VWAP through the last 1h bar closed at or before ``ref_ms`` (typical price,
  base volume = quote volume / tp), defined when the week is >= ``WARMUP_H`` hours old and >= ``MIN_COVER`` of its
  elapsed hours have a bar.

Levels are oriented like the S3 bars (a short's prices negated), so one rule serves both sides: a level ``L`` is
**swept and reclaimed** by the signal bar when ``extreme < L < close`` (oriented: the bar traded beyond the level and
closed back on the entry side). Flags:

* ``K1`` (new levels) - the bar sweeps and reclaims ``YO`` or ``VW``;
* ``K2`` (confluence, the author's «key level») - the bar sweeps and reclaims some level that has another, differently
  named level within ``tol`` (relative, 0.005 = 0.5 %) of it.

The placebo shifts all levels of a trade by one common factor ``1 + u`` (|u| uniform in [0.5 %, 3 %], random sign;
confluence is invariant under it) and recomputes the flags on the same bars.

Pure functions, no I/O; nothing reads a bar opening at or after ``ref_ms``, so levels and flags before time t do not
change when bars after t change.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import numpy as np

from tradedesk.analytics.s3_modules import htf_lows
from tradedesk.analytics.xo4_vwap import MIN_COVER, WARMUP_H, period_start

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
LEVELS = ("PW", "YO", "MO", "WO", "VW")
NEW = ("YO", "VW")
CONF_TOL = 0.005
U_LO, U_HI = 0.005, 0.03


def year_start(ref_ms: int) -> int:
    """Open ms of the UTC calendar year containing ``ref_ms``."""
    d = datetime.fromtimestamp(ref_ms / 1000, UTC)
    return int(datetime(d.year, 1, 1, tzinfo=UTC).timestamp() * 1000)


def open_levels(t: np.ndarray, o: np.ndarray, ref_ms: int) -> dict[str, float]:
    """``YO`` / ``MO`` / ``WO`` (real prices) of the periods containing ``ref_ms`` (see the module docstring)."""
    t = np.asarray(t, dtype=np.int64)
    ys = year_start(ref_ms)
    ms = int(period_start(np.array([ref_ms], dtype=np.int64), "month")[0])
    ws = int(period_start(np.array([ref_ms], dtype=np.int64), "week")[0])

    def at(start: int) -> float:
        j = int(np.searchsorted(t, start))
        return float(o[j]) if j < t.size and int(t[j]) == start and start <= ref_ms else math.nan

    return {
        "YO": at(ys),
        "MO": math.nan if ms == ys else at(ms),
        "WO": math.nan if ws in (ms, ys) else at(ws),
    }


def weekly_vwap_at(t: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, qv: np.ndarray, ref_ms: int) -> float:
    """XO-4's developing ISO-week VWAP through the last 1h bar closing at or before ``ref_ms`` (nan if the week is
    younger than ``WARMUP_H`` hours or fewer than ``MIN_COVER`` of its elapsed hours have a bar)."""
    t = np.asarray(t, dtype=np.int64)
    ws = int(period_start(np.array([ref_ms], dtype=np.int64), "week")[0])
    i0 = int(np.searchsorted(t, ws))
    i1 = int(np.searchsorted(t, ref_ms - HOUR_MS, side="right"))  # bars opening <= ref - 1h close <= ref
    if i1 <= i0:
        return math.nan
    elapsed = (int(t[i1 - 1]) + HOUR_MS - ws) / HOUR_MS
    if elapsed < WARMUP_H or (i1 - i0) < MIN_COVER * elapsed:
        return math.nan
    tp = (h[i0:i1] + low[i0:i1] + c[i0:i1]) / 3.0
    q = np.asarray(qv[i0:i1], dtype=float)
    v = np.where(tp > 0, q / np.where(tp > 0, tp, 1.0), 0.0)
    den = float(v.sum())
    return float(q.sum()) / den if den > 0 else math.nan


def oriented_levels(
    t: np.ndarray, o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, qv: np.ndarray, ref_ms: int, side: str
) -> dict[str, float]:
    """All ``LEVELS`` at ``ref_ms`` in the oriented price space of ``side`` (a short's prices negated)."""
    sg = 1.0 if side == "long" else -1.0
    lo_o = low if side == "long" else -np.asarray(h)
    pw = htf_lows(t, lo_o, ref_ms)["week"]  # oriented already
    real = open_levels(t, o, ref_ms)
    real["VW"] = weekly_vwap_at(t, h, low, c, qv, ref_ms)
    out = {"PW": pw}
    out.update({k: sg * v for k, v in real.items()})
    return {k: out[k] for k in LEVELS}


def swept(ext: float, close: float, levels: Mapping[str, float]) -> dict[str, bool]:
    """Per level: ``ext < L < close`` (oriented; nan levels are never swept)."""
    return {k: bool(np.isfinite(v) and ext < v < close) for k, v in levels.items()}


def confluent(levels: Mapping[str, float], name: str, tol: float = CONF_TOL) -> bool:
    """Another, differently named finite level lies within ``tol`` (relative) of level ``name``."""
    v = levels.get(name, math.nan)
    if not np.isfinite(v) or v == 0:
        return False
    return any(k != name and np.isfinite(x) and abs(x - v) <= tol * abs(v) for k, x in levels.items())


def flags(ext: float, close: float, levels: Mapping[str, float], tol: float = CONF_TOL) -> tuple[bool, bool]:
    """(``K1``, ``K2``) of one signal (see the module docstring)."""
    sw = swept(ext, close, levels)
    k1 = any(sw.get(n, False) for n in NEW)
    k2 = any(sw[n] and confluent(levels, n, tol) for n in sw)
    return k1, k2


def edge_distance_atr(edge: float, levels: Mapping[str, float], names: Sequence[str], atr_prev: float) -> float:
    """Distance from the range edge to the nearest of ``names`` in ATR (the H2 rule, an exploratory variant); nan if
    no level or ATR."""
    ds = [abs(edge - levels[n]) for n in names if np.isfinite(levels.get(n, math.nan))]
    if not ds or not (np.isfinite(atr_prev) and atr_prev > 0):
        return math.nan
    return min(ds) / atr_prev


# ---------------------------------------------------------------- vectorised flags and the placebo


def flag_matrix(ext: np.ndarray, close: np.ndarray, lv: np.ndarray, tol: float = CONF_TOL) -> tuple[np.ndarray, np.ndarray]:
    """``flags`` for n signals at once: ``lv`` is n x len(LEVELS) (columns in ``LEVELS`` order, oriented, nan =
    undefined); returns boolean (K1, K2) arrays."""
    lv = np.asarray(lv, dtype=float)
    fin = np.isfinite(lv)
    with np.errstate(invalid="ignore"):
        sw = fin & (lv > ext[:, None]) & (lv < close[:, None])
        conf = np.zeros_like(sw)
        for i in range(lv.shape[1]):
            for j in range(lv.shape[1]):
                if i != j:
                    conf[:, i] |= fin[:, i] & fin[:, j] & (np.abs(lv[:, j] - lv[:, i]) <= tol * np.abs(lv[:, i]))
    new = [LEVELS.index(n) for n in NEW]
    return sw[:, new].any(axis=1), (sw & conf).any(axis=1)


def near_matrix(
    edge: np.ndarray, atr_prev: np.ndarray, lv: np.ndarray, tol_atr: float = 0.3, tol: float = CONF_TOL
) -> tuple[np.ndarray, np.ndarray]:
    """The H2 rule instead of the sweep: (K1, K2) with «swept and reclaimed» replaced by «within ``tol_atr`` x ATR of
    the S3 range edge» (n signals; ``lv`` as in ``flag_matrix``; nan ATR = never near)."""
    lv = np.asarray(lv, dtype=float)
    fin = np.isfinite(lv)
    a = np.asarray(atr_prev, dtype=float)
    with np.errstate(invalid="ignore"):
        near = fin & (np.abs(lv - np.asarray(edge, dtype=float)[:, None]) <= tol_atr * a[:, None]) & (a[:, None] > 0)
        conf = np.zeros_like(near)
        for i in range(lv.shape[1]):
            for j in range(lv.shape[1]):
                if i != j:
                    conf[:, i] |= fin[:, i] & fin[:, j] & (np.abs(lv[:, j] - lv[:, i]) <= tol * np.abs(lv[:, i]))
    new = [LEVELS.index(n) for n in NEW]
    return near[:, new].any(axis=1), (near & conf).any(axis=1)


def placebo_u(keys: Sequence[str], n_sets: int, seed: int, lo: float = U_LO, hi: float = U_HI) -> np.ndarray:
    """n x ``n_sets`` shift factors u (|u| uniform in [lo, hi], random sign), deterministic per key and seed."""
    out = np.empty((len(keys), n_sets))
    for i, k in enumerate(keys):
        rng = np.random.default_rng([seed, zlib.crc32(k.encode())])
        out[i] = rng.uniform(lo, hi, n_sets) * rng.choice((-1.0, 1.0), n_sets)
    return out


def placebo_flags(
    ext: np.ndarray, close: np.ndarray, lv: np.ndarray, u: np.ndarray, tol: float = CONF_TOL
) -> tuple[np.ndarray, np.ndarray]:
    """(K1, K2) as n x n_sets boolean arrays with every level of a signal scaled by ``1 + u[:, j]``."""
    n, m = u.shape
    k1 = np.zeros((n, m), dtype=bool)
    k2 = np.zeros((n, m), dtype=bool)
    for j in range(m):
        k1[:, j], k2[:, j] = flag_matrix(ext, close, lv * (1.0 + u[:, j : j + 1]), tol)
    return k1, k2


def subgroup_delta(values: np.ndarray, flag: np.ndarray) -> float:
    """mean(values | flag) - mean(values); nan if the flag is empty."""
    v = np.asarray(values, dtype=float)
    f = np.asarray(flag, dtype=bool)
    return float(v[f].mean() - v.mean()) if f.any() else math.nan


def placebo_p(observed: float, placebo: np.ndarray) -> float:
    """(1 + #{placebo >= observed}) / (1 + #finite placebo)."""
    p = np.asarray(placebo, dtype=float)
    p = p[np.isfinite(p)]
    if not np.isfinite(observed) or p.size == 0:
        return math.nan
    return float((1 + np.sum(p >= observed)) / (1 + p.size))
