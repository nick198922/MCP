"""Market regime as the allowed side of a setup's trades (IDEA-20261030-demo-setups-depth, item «режим ринку задає
дозволену сторону»; registration «Попередня реєстрація — режим ринку задає сторону»).

Pure functions, no I/O; time is a parameter (ms).

* `sma_sign`: +1 / -1 / 0 for every BTC daily bar: close >= / < the mean of the last ``n`` closes (that bar included),
  0 without ``n`` consecutive calendar days;
* `cycle_sign`: the `cycle_regime` state as an allowed side (growth / accumulation -> +1, decline -> -1, else 0);
* `sign_before`: the sign of the last UTC day closed at or before ``t_ms`` (the day whose bar closes last by then);
* `split`: kept / dropped masks of trades under a sign (0 = the trade is not filtered and is in neither mask);
* `delta_boot`: Delta = mean R kept - mean R dropped per group (setup), pooled with equal weights over groups or over
  trades, with a cluster bootstrap that resamples the same clusters for every group;
* `random_drop` / `shift_signs`: the two placebos (same share dropped per stratum; a circular shift of the day signs);
* `mde80`: the minimum detectable effect at 80 % power for a two-sided test at ``alpha``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import NormalDist
from typing import Any

import numpy as np

DAY_MS = 86_400_000
LONG_STATES = ("growth", "accumulation")
SHORT_STATES = ("decline",)


def sma_sign(closes: Mapping[int, float], n: int) -> dict[int, int]:
    """{day open ms: +1 if close >= SMA_n, -1 if below, 0 without n consecutive daily closes ending at that day}. The
    SMA of day k uses the closes of days k-n+1 .. k only."""
    opens = sorted(closes)
    out: dict[int, int] = {}
    run = 0  # consecutive days ending at i
    csum = np.cumsum([0.0] + [float(closes[t]) for t in opens])
    for i, t in enumerate(opens):
        run = run + 1 if i > 0 and t - opens[i - 1] == DAY_MS else 1
        if run < n:
            out[t] = 0
            continue
        sma = (csum[i + 1] - csum[i + 1 - n]) / n
        out[t] = 1 if float(closes[t]) >= sma else -1
    return out


def cycle_sign(state: str | None) -> int:
    """Allowed side of a `cycle_regime` state: growth / accumulation -> +1 (longs), decline -> -1 (shorts); bottom,
    overheat and an undecided day -> 0 (not filtered, descriptive)."""
    if state in LONG_STATES:
        return 1
    if state in SHORT_STATES:
        return -1
    return 0


def sign_before(t_ms: int, signs: Mapping[int, int]) -> int:
    """Sign of the UTC day whose daily bar closes at or before ``t_ms`` last (keyed by the day's open ms; the same day
    as `setup_passport.state_before`); 0 if that day is missing."""
    day_open = (int(t_ms) // DAY_MS) * DAY_MS - DAY_MS
    return int(signs.get(day_open, 0))


def side_sign(side: str) -> int:
    return 1 if side == "long" else -1


def split(sides: Sequence[str], regime: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    """(kept, dropped): a trade is kept when its side agrees with the regime's sign, dropped when it opposes it; a 0
    sign leaves the trade in neither mask (the filter does not act)."""
    sd = np.array([side_sign(s) for s in sides], dtype=np.int64)
    rg = np.asarray(regime, dtype=np.int64)
    return (rg != 0) & (sd == rg), (rg != 0) & (sd == -rg)


def _cells(group: np.ndarray, cluster: np.ndarray, kept: np.ndarray, dropped: np.ndarray, r: np.ndarray, ng: int, nc: int) -> np.ndarray:
    """Sums [4, ng, nc]: sum R kept, n kept, sum R dropped, n dropped per (group, cluster)."""
    out = np.zeros((4, ng, nc))
    for j, (m, v) in enumerate(((kept, r), (kept, np.ones_like(r)), (dropped, r), (dropped, np.ones_like(r)))):
        np.add.at(out[j], (group[m], cluster[m]), v[m])
    return out


def _delta(cells: np.ndarray, w: np.ndarray, pooled: str) -> tuple[np.ndarray, np.ndarray]:
    """Per-group Delta [B, ng] and pooled Delta [B] for cluster weights ``w`` [B, nc]."""
    s = np.einsum("bc,kgc->kbg", w, cells)  # [4, B, ng]
    with np.errstate(invalid="ignore", divide="ignore"):
        per = s[0] / s[1] - s[2] / s[3]
        if pooled == "trades":
            tot = s.sum(axis=2)
            pool = tot[0] / tot[1] - tot[2] / tot[3]
        else:
            pool = np.nanmean(per, axis=1) if per.size else np.full(w.shape[0], math.nan)
    return per, pool


def delta_boot(
    group: Sequence[int],
    cluster: Sequence[int],
    kept: np.ndarray,
    dropped: np.ndarray,
    r: Sequence[float],
    *,
    pooled: str = "groups",
    n_boot: int = 4000,
    seed: int = 20261104,
    center: bool = False,
) -> dict[str, Any]:
    """Delta = mean R kept - mean R dropped per group and pooled (``pooled`` = 'groups': equal weight per group with
    both sides present; 'trades': all trades together), with a cluster bootstrap (the same resampled clusters for
    every group, so cross-group dependence inside a cluster is kept). ``center=True`` demeans R inside each (group,
    kept / dropped) cell first: the point Delta is then 0 by construction and only the bootstrap SE is informative (the
    MDE before any Delta is looked at). Returns m, lo, hi, se, p (two-sided, H0: Delta = 0) for the pool and per group."""
    g = np.asarray(group, dtype=np.int64)
    c = np.asarray(cluster)
    v = np.asarray(r, dtype=float).copy()
    kept, dropped = np.asarray(kept, bool), np.asarray(dropped, bool)
    _, ci = np.unique(c, return_inverse=True)
    ng, nc = int(g.max()) + 1 if g.size else 0, int(ci.max()) + 1 if ci.size else 0
    if center:
        for m in (kept, dropped):
            for k in range(ng):
                mk = m & (g == k)
                if mk.any():
                    v[mk] = v[mk] - v[mk].mean()
    cells = _cells(g, ci, kept, dropped, v, ng, nc)
    per0, pool0 = _delta(cells, np.ones((1, nc)), pooled)
    rng = np.random.default_rng(seed)
    w = rng.multinomial(nc, np.full(nc, 1.0 / nc), size=n_boot).astype(float) if nc else np.zeros((n_boot, 0))
    per_b, pool_b = _delta(cells, w, pooled)

    def summ(point: float, b: np.ndarray) -> dict[str, float]:
        b = b[np.isfinite(b)]
        if b.size < 2 or not math.isfinite(point):
            return {"m": point, "lo": math.nan, "hi": math.nan, "se": math.nan, "p": math.nan}
        p = min(1.0, 2 * min((np.sum(b <= 0) + 1) / (b.size + 1), (np.sum(b >= 0) + 1) / (b.size + 1)))
        return {"m": point, "lo": float(np.quantile(b, 0.025)), "hi": float(np.quantile(b, 0.975)), "se": float(b.std()), "p": float(p)}

    n_k = cells[1].sum(axis=1)
    n_d = cells[3].sum(axis=1)
    return {
        "pool": summ(float(pool0[0]), pool_b) | {"n_k": float(n_k.sum()), "n_d": float(n_d.sum()), "k": float(nc)},
        "per": [summ(float(per0[0, k]), per_b[:, k]) | {"n_k": float(n_k[k]), "n_d": float(n_d[k])} for k in range(ng)],
    }


def random_drop(strata: Sequence[Any], dropped: np.ndarray, eligible: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Placebo dropped mask: inside each stratum, as many randomly chosen eligible trades as the filter drops there
    (eligible = kept or dropped by the filter); the rest of the eligible trades are the placebo's kept."""
    st = np.asarray(strata)
    out = np.zeros(st.size, dtype=bool)
    el = np.asarray(eligible, bool)
    for s in np.unique(st[el]):
        idx = np.flatnonzero(el & (st == s))
        k = int(np.sum(np.asarray(dropped, bool)[idx]))
        if k:
            out[rng.choice(idx, size=k, replace=False)] = True
    return out


def shift_signs(signs: Mapping[int, int], shift_days: int) -> dict[int, int]:
    """Circular shift of the day signs by ``shift_days`` over the days that have a non-zero sign (keeps the regime's
    persistence and share; breaks its timing). Days with sign 0 stay 0."""
    days = sorted(t for t, s in signs.items() if s != 0)
    vals = [signs[t] for t in days]
    k = shift_days % len(days) if days else 0
    rot = vals[-k:] + vals[:-k] if k else vals
    out = {t: 0 for t in signs}
    out.update(dict(zip(days, rot, strict=True)))
    return out


def mde80(se: float, alpha: float = 0.05) -> float:
    """Minimum detectable effect at 80 % power, two-sided test at ``alpha``: (z_{1-alpha/2} + z_{0.8}) * se."""
    nd = NormalDist()
    return (nd.inv_cdf(1 - alpha / 2) + nd.inv_cdf(0.8)) * se
