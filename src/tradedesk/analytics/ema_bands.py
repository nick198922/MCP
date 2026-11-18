"""Trader_XO's 12/25 EMA bands as a direction filter (IDEA-20261113-x-traders, item 5).

The author reads a fast / slow EMA pair ("bands", 12 and 25 periods) on weekly and daily closes and trades "with the
bands, not against" ("Weekly 12/25 EMA = Bull, never fade this"). Here a band's direction is the sign of
EMA12 - EMA25 on the closes of completed periods (UTC days, ISO weeks from Monday 00:00 UTC) built from 1h bars; the
state at a moment t is that of the last period closed by t. Pure functions: no I/O, time is a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .course_s14_ema import ema, segment_pos

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS
WEEK_OFFSET_MS = 3 * DAY_MS  # 1970-01-01 was a Thursday: (t + 3 days) // week puts the boundaries on Mondays 00:00 UTC
FAST, SLOW = 12, 25  # the author's pair (sometimes 12/21; 12/25 is the registered one)
MIN_FRAC = 0.9  # a period counts only with >= 90 % of its 1h bars


@dataclass(frozen=True)
class Band:
    """Direction of the band of every completed period: ``end`` close time (ms), ``dir`` +1 fast above slow, -1
    below, 0 undefined (warm-up, equality)."""

    end: np.ndarray
    dir: np.ndarray
    period_ms: int


def period_closes(
    t_open: np.ndarray, close: np.ndarray, bar_ms: int, period_ms: int, offset_ms: int = 0, min_frac: float = MIN_FRAC
) -> tuple[np.ndarray, np.ndarray]:
    """(start ms, close) of every period with at least ``min_frac`` of its bars; the close is that of the period's last
    bar. ``t_open`` must be sorted and unique. Periods are ``[k * period - offset, (k + 1) * period - offset)``."""
    t = np.asarray(t_open, dtype=np.int64)
    c = np.asarray(close, dtype=float)
    if t.size == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    key = (t + offset_ms) // period_ms
    starts = np.flatnonzero(np.concatenate([[True], key[1:] != key[:-1]]))
    counts = np.diff(np.concatenate([starts, [key.size]]))
    last = starts + counts - 1
    full = counts >= min_frac * (period_ms // bar_ms)
    return (key[starts] * period_ms - offset_ms)[full], c[last][full]


def band_series(
    t_open: np.ndarray,
    close: np.ndarray,
    bar_ms: int,
    period_ms: int,
    offset_ms: int = 0,
    fast: int = FAST,
    slow: int = SLOW,
    min_frac: float = MIN_FRAC,
) -> Band:
    """Band direction of every completed period. The EMAs (seeded with the mean of the first n closes, TradingView
    style) restart when two or more consecutive periods are missing (open-to-open gap > 2 periods); a single missing
    period is bridged."""
    start, cl = period_closes(t_open, close, bar_ms, period_ms, offset_ms, min_frac)
    pos = segment_pos(start, max_gap_ms=2 * period_ms)
    ef, es = ema(cl, fast, pos), ema(cl, slow, pos)
    ok = np.isfinite(ef) & np.isfinite(es)
    d = np.zeros(start.size, dtype=np.int64)
    d[ok] = np.sign(ef[ok] - es[ok]).astype(np.int64)
    return Band(start + period_ms, d, period_ms)


def daily_band(t_open: np.ndarray, close: np.ndarray, bar_ms: int = HOUR_MS) -> Band:
    return band_series(t_open, close, bar_ms, DAY_MS)


def weekly_band(t_open: np.ndarray, close: np.ndarray, bar_ms: int = HOUR_MS) -> Band:
    return band_series(t_open, close, bar_ms, WEEK_MS, WEEK_OFFSET_MS)


def states_at(band: Band, t_ms: np.ndarray, max_age_periods: int = 2) -> np.ndarray:
    """Direction of the last period closed by each ``t_ms`` (end <= t); 0 if none, or if that period ended more than
    ``max_age_periods`` periods before t (stale after a data gap)."""
    t = np.asarray(t_ms, dtype=np.int64)
    j = np.searchsorted(band.end, t, side="right") - 1
    out = np.zeros(t.size, dtype=np.int64)
    ok = j >= 0
    jj = j[ok]
    fresh = t[ok] - band.end[jj] <= max_age_periods * band.period_ms
    vals = np.where(fresh, band.dir[jj], 0)
    out[ok] = vals
    return out


def side_sign(side: object) -> int:
    """+1 for a long ("long" or +1), -1 for a short."""
    if isinstance(side, str):
        return 1 if side == "long" else -1
    return 1 if int(side) > 0 else -1


def alignment(side: object, weekly: int, daily: int) -> str:
    """Label of a trade against the bands: ``with`` both point the trade's way, ``against`` both point the other
    way, ``mixed`` they disagree, ``undef`` either is undefined."""
    if weekly == 0 or daily == 0:
        return "undef"
    want = side_sign(side)
    if weekly == want and daily == want:
        return "with"
    if weekly == -want and daily == -want:
        return "against"
    return "mixed"


def alignments(sides: np.ndarray, weekly: np.ndarray, daily: np.ndarray) -> np.ndarray:
    return np.array([alignment(s, int(w), int(d)) for s, w, d in zip(sides, weekly, daily, strict=True)])


# ---------------------------------------------------------------- stratified lift of a flagged subgroup
#
# The share of flagged trades differs a lot between sides and years, so a pooled mean(flagged) - mean(all) partly
# measures "shorts vs longs" or "2022 vs 2021". The stratified lift compares within strata (side x calendar year) and
# averages the within-stratum lifts with weights w_s:
#   "flag" (registered): w_s = n_a,s, the flagged trades of the stratum. Then the lift is the mean over the flagged
#          trades of (R - mean R of the trade's stratum): the pooled lift with each trade's baseline taken from its own
#          stratum instead of the whole sample.
#   "base": w_s = n_s, the base trades of the stratum (the average lift per base trade; noisier where few are flagged).

MIN_STRATUM = 2  # a stratum counts only with >= 2 flagged and >= 2 non-flagged base trades
WEIGHT = "flag"


def strata_codes(*keys: np.ndarray) -> np.ndarray:
    """Integer code 0..S-1 of each row's combination of ``keys`` (e.g. side and year)."""
    code = np.zeros(np.asarray(keys[0]).size, dtype=np.int64)
    for k in keys:
        _, inv = np.unique(np.asarray(k), return_inverse=True)
        code = code * (int(inv.max()) + 1 if inv.size else 1) + inv.reshape(-1)
    _, out = np.unique(code, return_inverse=True)
    return out.reshape(-1).astype(np.int64)


def _strat_from_sums(sa: np.ndarray, na: np.ndarray, sb: np.ndarray, nb: np.ndarray, min_n: int, weight: str = WEIGHT) -> np.ndarray:
    """Stratified lift from per-stratum sums along the last axis (flagged a, base b with a a subset of b); NaN if no
    stratum qualifies."""
    if weight not in ("flag", "base"):
        raise ValueError(f"weight must be 'flag' or 'base', got {weight!r}")
    ok = (na >= min_n) & (nb - na >= min_n)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = np.where(ok, sa / np.where(ok, na, 1) - sb / np.where(ok, nb, 1), 0.0)
        w = np.where(ok, na if weight == "flag" else nb, 0.0)
        return np.where(w.sum(axis=-1) > 0, (w * d).sum(axis=-1) / np.maximum(w.sum(axis=-1), 1), np.nan)


def _prep(values: np.ndarray, flag: np.ndarray, base: np.ndarray, strata: np.ndarray):
    v = np.asarray(values, dtype=float)
    b = np.asarray(base, dtype=bool) & np.isfinite(v)
    a = np.asarray(flag, dtype=bool) & b
    s = np.asarray(strata, dtype=np.int64)
    return v[b], a[b], s[b]


def stratified_delta(
    values: np.ndarray, flag: np.ndarray, base: np.ndarray, strata: np.ndarray, min_n: int = MIN_STRATUM, weight: str = WEIGHT
) -> dict[str, float]:
    """Point stratified lift sum_s w_s (mean(R | flag, s) - mean(R | base, s)) / sum_s w_s over the qualifying strata
    (w_s see ``WEIGHT``); ``flag`` is taken within ``base``. Returns d, strata used / total, n flagged and n base
    (qualifying strata only)."""
    v, a, s = _prep(values, flag, base, strata)
    if v.size == 0:
        return {"d": float("nan"), "used": 0.0, "total": 0.0, "n_a": 0.0, "n": 0.0}
    S = int(s.max()) + 1
    sa, na = np.bincount(s, v * a, S), np.bincount(s, a.astype(float), S)
    sb, nb = np.bincount(s, v, S), np.bincount(s, minlength=S).astype(float)
    ok = (na >= min_n) & (nb - na >= min_n)
    return {
        "d": float(_strat_from_sums(sa, na, sb, nb, min_n, weight)),
        "used": float(ok.sum()),
        "total": float((nb > 0).sum()),
        "n_a": float(na[ok].sum()),
        "n": float(nb[ok].sum()),
    }


def stratified_delta_ci(
    values: np.ndarray,
    flag: np.ndarray,
    base: np.ndarray,
    strata: np.ndarray,
    clusters: np.ndarray,
    *,
    n_boot: int = 10_000,
    seed: int = 20261118,
    min_n: int = MIN_STRATUM,
    weight: str = WEIGHT,
) -> dict[str, float]:
    """Stratified lift with a 95 % CI and two-sided p (H0: 0) by a cluster bootstrap: whole clusters (weeks) are
    resampled, all their strata together, and the stratified lift is recomputed in every replicate."""
    res = stratified_delta(values, flag, base, strata, min_n, weight)
    v, a, s = _prep(values, flag, base, strata)
    c = np.asarray(clusters)[np.asarray(base, dtype=bool) & np.isfinite(np.asarray(values, dtype=float))]
    if not np.isfinite(res["d"]) or np.unique(c).size < 2:
        res.update(lo=float("nan"), hi=float("nan"), p=float("nan"))
        return res
    _, k = np.unique(c, return_inverse=True)
    k = k.reshape(-1)
    K, S = int(k.max()) + 1, int(s.max()) + 1
    idx = k * S + s
    mats = [np.bincount(idx, w, K * S).reshape(K, S) for w in (v * a, a.astype(float), v, np.ones(v.size))]
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    step = 1000
    for i in range(0, n_boot, step):
        m = min(step, n_boot - i)
        cnt = np.stack([np.bincount(rng.integers(0, K, K), minlength=K) for _ in range(m)]).astype(float)
        sa, na, sb, nb = (cnt @ x for x in mats)
        out[i : i + m] = _strat_from_sums(sa, na, sb, nb, min_n, weight)
    out = out[np.isfinite(out)]
    p = min(1.0, 2 * min((np.sum(out <= 0) + 1) / (out.size + 1), (np.sum(out >= 0) + 1) / (out.size + 1)))
    res.update(lo=float(np.quantile(out, 0.025)), hi=float(np.quantile(out, 0.975)), p=float(p))
    return res


def stratified_placebo(
    values: np.ndarray,
    flag: np.ndarray,
    base: np.ndarray,
    strata: np.ndarray,
    *,
    n_rep: int = 2000,
    seed: int = 20261118,
    min_n: int = MIN_STRATUM,
    weight: str = WEIGHT,
) -> np.ndarray:
    """Stratified lifts of ``n_rep`` random subgroups that keep the flagged count of every stratum (drawn without
    replacement among the stratum's base trades): the null distribution of ``stratified_delta`` for a feature
    unrelated to R within strata."""
    v, a, s = _prep(values, flag, base, strata)
    if v.size == 0:
        return np.full(n_rep, np.nan)
    S = int(s.max()) + 1
    na = np.bincount(s, a.astype(float), S)
    sb, nb = np.bincount(s, v, S), np.bincount(s, minlength=S).astype(float)
    groups = [np.flatnonzero(s == j) for j in range(S)]
    rng = np.random.default_rng(seed)
    out = np.empty(n_rep)
    sa = np.zeros(S)
    for r in range(n_rep):
        for j, g in enumerate(groups):
            kj = int(na[j])
            sa[j] = v[g[rng.permutation(g.size)[:kj]]].sum() if 0 < kj < g.size else (v[g].sum() if kj else 0.0)
        out[r] = _strat_from_sums(sa, na, sb, nb, min_n, weight)
    return out


def stratified_prior_se(
    flag: np.ndarray, base: np.ndarray, strata: np.ndarray, sd: float = 1.0, min_n: int = MIN_STRATUM, weight: str = WEIGHT
) -> float:
    """A-priori SE of the stratified lift for independent trades with a common sd (no outcomes used):
    sqrt(sum_s (w_s / W)^2 sd^2 (1 / n_a,s - 1 / n_s)), w_s = n_a,s (``weight="flag"``) or n_s (``"base"``)."""
    b = np.asarray(base, dtype=bool)
    a = np.asarray(flag, dtype=bool)[b]
    s = np.asarray(strata, dtype=np.int64)[b]
    if s.size == 0:
        return float("nan")
    S = int(s.max()) + 1
    na, nb = np.bincount(s, a.astype(float), S), np.bincount(s, minlength=S).astype(float)
    ok = (na >= min_n) & (nb - na >= min_n)
    if not ok.any():
        return float("nan")
    w = (na if weight == "flag" else nb)[ok]
    w = w / w.sum()
    return float(sd * np.sqrt(np.sum(w**2 * (1 / na[ok] - 1 / nb[ok]))))
