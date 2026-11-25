"""Delta absorption on the S3 signal (sweep) bar (IDEA-20261113-x-traders, item 5, batch 5.7; the «CVD / delta» and
«absorption» rows of Trader_XO's and KillaXBT's toolkits), measured as a module of the S3 range-edge sweep.

Taker data are Binance USD-M 1h klines with the aggressor columns (``binance_vision.parse_klines_taker_zip`` rows
``[open_ms, open, high, low, close, base_volume, quote_volume, taker_buy_base, taker_buy_quote]``). For a bar
``[t0, t1)`` built from its 1h rows:

* ``V`` = sum of ``base_volume``, ``B`` = sum of ``taker_buy_base`` (both in the base asset), taker-sell ``S = V - B``;
* normalised delta ``d = (B - S) / V = 2 B / V - 1`` in [-1, 1] (+1: every trade had a buying taker);
* oriented delta ``od = d`` for a long, ``-d`` for a short (the side the trade takes), so ``od < 0`` means the
  aggressors of the bar pushed against the trade: net taker selling on a bar that swept the range low and closed back
  above it (a short: net taker buying on a bar that swept the high and closed back below);
* excess oriented delta ``x = od - median(od of the same coin's bars 6-28 days earlier, same timeframe)``: the
  Binance alt taker-buy share sits below 0.5 (stage 0: median 0.486 on the S3 signal bars) and drifts by year, so the
  raw sign mostly measures that baseline, not the bar;
* the registered flag is **absorption**: ``x < 0`` (threshold 0), compared within quintiles of the oriented bar return
  (``stratified_delta``), so a flagged trade is one whose bar saw more aggression against the trade than the coin's
  usual for the same price outcome.

The bar's delta is defined only when every hour of the bar has a row and ``V > 0``, and each row has
``0 <= taker_buy_base <= base_volume``. Nothing here reads a row opening at or after ``t1`` (the entry time of the S3
trade is the close of the signal bar), so a feature before time t does not change when rows after t change.

Placebo: the same coin's delta on an earlier bar of the same timeframe, ``k`` bars back with ``k * bar`` in
[``PLACEBO_MIN_DAYS``, ``PLACEBO_MAX_DAYS``] days, one defined bar drawn per trade and set, deterministic by key; the
placebo excess (vs the same baseline) flags the observed number of trades in every stratum (``matched_flags``).

Pure functions, no I/O.
"""

from __future__ import annotations

import math
import zlib
from collections.abc import Sequence

import numpy as np

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
THRESHOLD = 0.0  # registered: absorption = excess oriented delta strictly below 0
PLACEBO_MIN_DAYS = 6
PLACEBO_MAX_DAYS = 28
MIN_BASELINE_SHARE = 0.5  # the coin's baseline needs >= half of its 6-28 d bars defined
# column indices of parse_klines_taker_zip rows
T_, O_, H_, L_, C_, V_, QV_, TB_, TBQ_ = range(9)


def side_sign(side: str) -> float:
    return 1.0 if side == "long" else -1.0


def _window(t: np.ndarray, t0: int, t1: int) -> slice:
    i0 = int(np.searchsorted(t, t0, side="left"))
    i1 = int(np.searchsorted(t, t1, side="left"))
    return slice(i0, i1)


def bar_delta(t: np.ndarray, base: np.ndarray, tbb: np.ndarray, t0: int, t1: int, hour_ms: int = HOUR_MS) -> float:
    """Normalised taker delta ``2 B / V - 1`` of the bar ``[t0, t1)`` from sorted 1h rows; nan unless every hour has
    exactly one row, ``V > 0`` and ``0 <= taker_buy_base <= base_volume`` on every row."""
    if t1 <= t0 or (t1 - t0) % hour_ms:
        return math.nan
    w = _window(t, t0, t1)
    need = (t1 - t0) // hour_ms
    tt = t[w]
    if tt.size != need or np.any(np.diff(tt) != hour_ms) or (tt.size and int(tt[0]) != t0):
        return math.nan
    v, b = base[w], tbb[w]
    if not (np.all(np.isfinite(v)) and np.all(np.isfinite(b))) or np.any(b < 0) or np.any(b > v * (1 + 1e-9)):
        return math.nan
    vs = float(v.sum())
    if vs <= 0:
        return math.nan
    return 2.0 * float(b.sum()) / vs - 1.0


def oriented(d: float, side: str) -> float:
    return side_sign(side) * d


def absorption(od: float, threshold: float = THRESHOLD) -> bool:
    """The registered flag: oriented delta strictly below the threshold (False for nan; definedness is separate)."""
    return bool(np.isfinite(od) and od < threshold)


def placebo_k_range(bar_ms: int, min_days: int = PLACEBO_MIN_DAYS, max_days: int = PLACEBO_MAX_DAYS) -> tuple[int, int]:
    """Smallest and largest shift k (in bars) with ``k * bar_ms`` in [min_days, max_days] days."""
    return math.ceil(min_days * DAY_MS / bar_ms), (max_days * DAY_MS) // bar_ms


def candidate_deltas(
    t: np.ndarray, base: np.ndarray, tbb: np.ndarray, signal_ts: int, bar_ms: int, side: str, hour_ms: int = HOUR_MS
) -> np.ndarray:
    """Oriented deltas of the placebo bars ``[signal_ts - k bar, signal_ts - (k - 1) bar)`` for k in
    ``placebo_k_range(bar_ms)`` (nan where undefined); all of them closed before the signal bar opens."""
    k0, k1 = placebo_k_range(bar_ms)
    sg = side_sign(side)
    out = np.full(k1 - k0 + 1, math.nan)
    for j, k in enumerate(range(k0, k1 + 1)):
        a = signal_ts - k * bar_ms
        out[j] = sg * bar_delta(t, base, tbb, a, a + bar_ms, hour_ms)
    return out


def placebo_pick(cands: Sequence[np.ndarray], keys: Sequence[str], n_sets: int, seed: int) -> np.ndarray:
    """n x ``n_sets`` placebo oriented deltas: per trade and set one finite candidate drawn uniformly, deterministic by
    key and seed; a row of nan where a trade has no finite candidate."""
    out = np.full((len(keys), n_sets), math.nan)
    for i, (c, k) in enumerate(zip(cands, keys, strict=True)):
        fin = np.asarray(c, dtype=float)
        fin = fin[np.isfinite(fin)]
        if fin.size == 0:
            continue
        rng = np.random.default_rng([seed, zlib.crc32(k.encode())])
        out[i] = fin[rng.integers(0, fin.size, n_sets)]
    return out


def matched_flags(x: np.ndarray, strata: np.ndarray, flag: np.ndarray) -> np.ndarray:
    """Placebo flags with the observed flag count in every stratum: in stratum s the ``#flag_s`` trades with the lowest
    ``x`` (nan last, ties by position) are flagged, so a placebo set differs from the observed flag only in which
    trades it picks, not in how many per stratum."""
    x = np.asarray(x, dtype=float)
    s = np.asarray(strata)
    f = np.asarray(flag, dtype=bool)
    out = np.zeros(x.size, dtype=bool)
    for g in np.unique(s[s >= 0]):
        idx = np.flatnonzero(s == g)
        k = int(f[idx].sum())
        if k:
            xs = np.where(np.isfinite(x[idx]), x[idx], np.inf)
            out[idx[np.argsort(xs, kind="stable")[:k]]] = True
    return out


def excess_delta(od: float, cands: np.ndarray, min_share: float = MIN_BASELINE_SHARE) -> float:
    """The registered feature: ``od`` minus the median of the coin's oriented deltas on its bars 6-28 days earlier
    (``candidate_deltas``, past bars only); nan unless at least ``min_share`` of those bars are defined."""
    c = np.asarray(cands, dtype=float)
    fin = c[np.isfinite(c)]
    if not np.isfinite(od) or c.size == 0 or fin.size < min_share * c.size:
        return math.nan
    return float(od - np.median(fin))


def extreme_hour_delta(
    t: np.ndarray, low_or: np.ndarray, base: np.ndarray, tbb: np.ndarray, t0: int, t1: int, side: str, hour_ms: int = HOUR_MS
) -> float:
    """Oriented delta of the 1h row of ``[t0, t1)`` with the lowest oriented low (the sweep hour; the first on ties).
    ``low_or`` is the low for a long and minus the high for a short."""
    w = _window(t, t0, t1)
    if t[w].size == 0:
        return math.nan
    j = w.start + int(np.argmin(low_or[w]))
    return side_sign(side) * bar_delta(t, base, tbb, int(t[j]), int(t[j]) + hour_ms, hour_ms)


def oriented_body(o_first: float, c_last: float, side: str) -> float:
    """Oriented return of the signal bar, ``sign * (close / open - 1)``."""
    if not (o_first > 0 and np.isfinite(c_last)):
        return math.nan
    return side_sign(side) * (c_last / o_first - 1.0)


def stratified_delta_ci(
    values: np.ndarray, flag: np.ndarray, strata: np.ndarray, clusters: np.ndarray, n_boot: int = 10_000, seed: int = 20261125
) -> dict[str, float]:
    """``stratified_delta`` with a cluster bootstrap (whole clusters resampled, stratum means re-estimated in every
    replicate): point ``d``, percentile 95 % ``lo`` / ``hi``, ``se`` (SD of the replicates) and the two-sided ``p``
    (2 x the smaller of ``(1 + #replicates <= 0) / (1 + B)`` and ``(1 + #replicates >= 0) / (1 + B)``, capped at 1;
    so p is never 0: with B = 10 000 the smallest value is about 2e-4)."""
    v = np.asarray(values, dtype=float)
    f = np.asarray(flag, dtype=bool)
    s = np.asarray(strata).astype(np.int64)
    ok = (s >= 0) & np.isfinite(v)
    v, f, s, c = v[ok], f[ok], s[ok], np.asarray(clusters)[ok]
    d = stratified_delta(v, f, s)
    if not np.isfinite(d):
        return {"d": math.nan, "lo": math.nan, "hi": math.nan, "se": math.nan, "p": math.nan}
    uc, ci = np.unique(c, return_inverse=True)
    k = int(s.max()) + 1
    # per cluster and stratum: sum and count of all values, sum and count of flagged values
    cell = ci * k + s
    m = uc.size * k
    sa, na = np.bincount(cell, v, m).reshape(uc.size, k), np.bincount(cell, None, m).reshape(uc.size, k)
    sf, nf = np.bincount(cell, v * f, m).reshape(uc.size, k), np.bincount(cell, f.astype(float), m).reshape(uc.size, k)
    rng = np.random.default_rng(seed)
    reps = np.full(n_boot, math.nan)
    for b in range(n_boot):
        w = np.bincount(rng.integers(0, uc.size, uc.size), minlength=uc.size).astype(float)
        SA, NA, SF, NF = w @ sa, w @ na, w @ sf, w @ nf
        good = NA > 0
        tot = NF[good].sum()
        if tot > 0:
            reps[b] = (SF[good].sum() - (NF[good] * SA[good] / NA[good]).sum()) / tot
    r = reps[np.isfinite(reps)]
    lo, hi = np.quantile(r, [0.025, 0.975])
    p = min(1.0, 2 * min((1 + np.sum(r <= 0)) / (1 + r.size), (1 + np.sum(r >= 0)) / (1 + r.size)))
    return {"d": d, "lo": float(lo), "hi": float(hi), "se": float(r.std(ddof=1)), "p": float(p)}


def covariate_adjusted(
    values: np.ndarray, covars: np.ndarray, strata: np.ndarray, groups: np.ndarray
) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    """Condition (13) of registration 5.7: ``values`` minus the linear part explained by ``covars`` (n x k) within
    strata. Per group g (the timeframe), values and covariates are demeaned within each stratum and the demeaned
    values regressed on the demeaned covariates without intercept (Frisch-Waugh: the same slopes as OLS with stratum
    dummies); the result is ``value - covars_demeaned @ beta_g``, so stratum means are unchanged and
    ``stratified_delta`` of it is the flagged-minus-stratum gap net of the linear covariate imbalance. Rows with a
    stratum < 0 or a non-finite value / covariate are nan. Returns (adjusted values, {g: beta_g})."""
    v = np.asarray(values, dtype=float)
    X = np.asarray(covars, dtype=float).reshape(v.size, -1)
    s = np.asarray(strata)
    g_ = np.asarray(groups)
    ok = (s >= 0) & np.isfinite(v) & np.all(np.isfinite(X), axis=1)
    out = np.full(v.size, math.nan)
    betas: dict[int, np.ndarray] = {}
    for g in np.unique(g_[ok]):
        mg = ok & (g_ == g)
        vd = np.zeros(v.size)
        Xd = np.zeros_like(X)
        for c in np.unique(s[mg]):
            m = mg & (s == c)
            vd[m] = v[m] - v[m].mean()
            Xd[m] = X[m] - X[m].mean(axis=0)
        beta, *_ = np.linalg.lstsq(Xd[mg], vd[mg], rcond=None)
        betas[int(g)] = beta
        out[mg] = v[mg] - Xd[mg] @ beta
    return out, betas


def verdict(
    mde_prior: float,
    all_conditions: bool,
    d: float,
    p1: float,
    mde: float,
    hi_week: float,
    gate: float = 0.25,
    interest: float = 0.05,
) -> str:
    """The verdict of registration 5.7 (amended before the run), checked in this order, so exactly one applies:

    1. ◯ (gate): the a-priori MDE80 is not finite or above ``gate``; no means are computed;
    2. ✓: all registered conditions hold;
    3. ≈: Δs > 0 and one-sided p1 < 0.05, but not every condition holds;
    4. ◯ (power): the actual MDE80 or the week CI is not finite, or the actual MDE80 > ``interest`` and the upper
       bound of the week 95 % CI of Δs is >= ``interest`` (an effect of the size of interest is not excluded);
    5. ✗: everything else (the CI excludes +interest, or the test had the power to see it), including a significantly
       negative Δs."""
    if not (np.isfinite(mde_prior) and mde_prior <= gate):
        return "◯"
    if all_conditions:
        return "✓"
    if np.isfinite(d) and np.isfinite(p1) and d > 0 and p1 < 0.05:
        return "≈"
    if not (np.isfinite(mde) and np.isfinite(hi_week)):
        return "◯"
    if mde > interest and hi_week >= interest:
        return "◯"
    return "✗"


def stratified_delta(values: np.ndarray, flag: np.ndarray, strata: np.ndarray) -> float:
    """mean over flagged trades of ``value - mean(value | its stratum)`` (strata < 0 dropped); nan if none flagged."""
    resid = stratified_resid(values, strata)
    sel = np.asarray(flag, dtype=bool) & (np.asarray(strata) >= 0)
    return float(resid[sel].mean()) if sel.any() else math.nan


def stratified_var_factor(flag: np.ndarray, strata: np.ndarray) -> float:
    """Variance of ``stratified_delta`` relative to the plain flagged-vs-all Δ under one common per-trade variance:
    ``sum_s w_s^2 (1/nf_s - 1/n_s) / (1/nf - 1/n)`` with ``w_s = nf_s / nf`` (strata < 0 dropped); 1 when the flag
    has the same share in equal strata, either side of 1 otherwise. Multiplies the a-priori variance, so the MDE
    scales by its square root."""
    f = np.asarray(flag, dtype=bool)
    s = np.asarray(strata)
    ok = s >= 0
    f, s = f[ok], s[ok]
    nf, n = int(f.sum()), f.size
    if nf == 0 or nf == n:
        return math.nan
    acc = 0.0
    for g in np.unique(s):
        m = s == g
        nfs, ns = int(f[m].sum()), int(m.sum())
        if nfs:
            acc += (nfs / nf) ** 2 * (1 / nfs - 1 / ns)
    return acc / (1 / nf - 1 / n)


def stratified_resid(values: np.ndarray, strata: np.ndarray) -> np.ndarray:
    """``value - mean(value | stratum)`` per trade (nan where the stratum is < 0)."""
    v = np.asarray(values, dtype=float)
    s = np.asarray(strata)
    out = np.full(v.size, math.nan)
    for g in np.unique(s[s >= 0]):
        m = s == g
        out[m] = v[m] - v[m].mean()
    return out


def taker_vwap_inside(rows: np.ndarray, rel_tol: float = 1e-6) -> np.ndarray:
    """Sanity per row: the taker-buy VWAP ``taker_buy_quote / taker_buy_base`` lies in [low, high] (True where the row
    has no taker buys)."""
    r = np.asarray(rows, dtype=float)
    tb, tq = r[:, TB_], r[:, TBQ_]
    with np.errstate(divide="ignore", invalid="ignore"):
        vw = np.where(tb > 0, tq / tb, math.nan)
    ok = (vw >= r[:, L_] * (1 - rel_tol)) & (vw <= r[:, H_] * (1 + rel_tol))
    return np.where(tb > 0, ok, True)
