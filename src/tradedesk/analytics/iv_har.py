"""Daily Deribit BTC ATM IV vs a HAR-RV forecast from 5-minute bars (IDEA-20261023-daily-iv-vs-intraday-vol-forecast).

Pure functions, no I/O; time is always a parameter. Conventions (see the card's pre-registration):
  * 5-minute bars are keyed by open time in seconds, value = close; a bar opened at ``t`` closes at ``t + bar_s``;
  * the return of bar ``t`` is ``ln(close[t] / close[t - bar_s])`` (the move over ``[t, t + bar_s]``), only when both
    bars exist; a window ``[lo, hi)`` uses bars with open time in ``[lo, hi - bar_s]``, i.e. information up to ``hi``;
  * realized variance (RV) of a window is the sum of squared returns scaled up for missing bars (decimal, not %);
  * vols are decimals (0.45 = 45 %), annualised with 365 days; option entry quotes come from
    ``event_straddle.entry_quote`` (prints at or before the entry time only).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .event_straddle import HOUR_MS, _leg_rows
from .put_skew import implied_vol_inverse, year_frac

BAR_S = 300
DAY_S = 86_400


# ----------------------------------------------------------------------------- realized variance and HAR


def bar_returns(closes: Mapping[int, float], lo_s: int, hi_s: int, *, bar_s: int = BAR_S) -> list[float]:
    """Log returns of the bars with open time in ``[lo_s, hi_s - bar_s]`` whose previous bar also exists."""
    out = []
    for t in range(lo_s, hi_s - bar_s + 1, bar_s):
        c, p = closes.get(t), closes.get(t - bar_s)
        if c and p:
            out.append(math.log(c / p))
    return out


def realized_var(closes: Mapping[int, float], lo_s: int, hi_s: int, *, bar_s: int = BAR_S, min_cov: float = 0.9) -> float | None:
    """Realized variance of ``[lo_s, hi_s)``: sum of squared bar returns x expected/actual bar count; None below ``min_cov``."""
    n_exp = (hi_s - lo_s) // bar_s
    rets = bar_returns(closes, lo_s, hi_s, bar_s=bar_s)
    if n_exp <= 0 or len(rets) < min_cov * n_exp:
        return None
    return sum(r * r for r in rets) * n_exp / len(rets)


def har_features(closes: Mapping[int, float], t0_s: int, *, bar_s: int = BAR_S, min_cov: float = 0.9) -> dict[str, float] | None:
    """Daily-scaled RV over the 1, 7 and 30 days before ``t0_s`` (bars closed by ``t0_s`` only); None if any window is thin."""
    out = {}
    for name, days in (("rv_d", 1), ("rv_w", 7), ("rv_m", 30)):
        v = realized_var(closes, t0_s - days * DAY_S, t0_s, bar_s=bar_s, min_cov=min_cov)
        if v is None or v <= 0:
            return None
        out[name] = v / days
    return out


def _har_row(f: Mapping[str, float]) -> list[float]:
    return [1.0, math.log(f["rv_d"]), math.log(f["rv_w"]), math.log(f["rv_m"])]


def har_fit(feats: Sequence[Mapping[str, float]], targets: Sequence[float]) -> dict[str, Any]:
    """Log-HAR by OLS: ln RV_target = b0 + bd ln RV_d + bw ln RV_w + bm ln RV_m; returns coefs, residual variance, r2, n."""
    X = np.array([_har_row(f) for f in feats], dtype=float)
    y = np.log(np.asarray(targets, dtype=float))
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ beta
    s2 = float(res @ res / (len(y) - X.shape[1]))
    r2 = 1.0 - float(res @ res) / float(((y - y.mean()) ** 2).sum())
    return {"coef": [float(b) for b in beta], "s2": s2, "r2": r2, "n": len(y)}


def har_forecast_var(fit: Mapping[str, Any], f: Mapping[str, float]) -> float:
    """Forecast of the target-window variance (decimal): exp(fitted + s2/2) (log-normal mean correction)."""
    return math.exp(sum(b * x for b, x in zip(fit["coef"], _har_row(f), strict=True)) + fit["s2"] / 2.0)


def annualize(var_window: float, window_h: float) -> float:
    """Annualised vol (decimal) from a window variance: sqrt(var * 365 * 24 / window_h)."""
    return math.sqrt(var_window * 365.0 * 24.0 / window_h)


# ----------------------------------------------------------------------------- ATM IV from prints


def atm_iv_prints(trades: Sequence[dict[str, Any]], q: Mapping[str, Any], *, window_h: float = 2.0) -> list[dict[str, Any]]:
    """Prints on the quote's ATM strike and expiry in ``[t0 - window_h, t0]``, time-sorted, with mark and trade IV (decimals).

    Mark IV is inverted from the print's ``mark_price`` on the forward ``ratio * index`` (as in ``entry_quote``).
    """
    t0, exp, K, ratio = q["t0"], q["expiry"], q["K"], q["ratio"]
    out = []
    for r in _leg_rows(trades, exp, t0 - int(window_h * HOUR_MS), t0):
        if r["K"] != K:
            continue
        t = r["t"]
        mark = None
        if t.get("mark_price"):
            mark = implied_vol_inverse(float(t["mark_price"]), ratio * float(t["index_price"]), K, year_frac(exp, r["ts"]), r["call"])
        tiv = float(t["iv"]) / 100.0 if t.get("iv") else None
        if mark is not None or tiv is not None:
            out.append({"ts": r["ts"], "call": r["call"], "mark_iv": mark, "trade_iv": tiv})
    return out


def iv_summary(prints: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Mean mark IV of all prints (primary), the last print's mark IV, mean trade IV, print count; None without mark IV."""
    marks = [p["mark_iv"] for p in prints if p["mark_iv"] is not None]
    if not marks:
        return None
    tivs = [p["trade_iv"] for p in prints if p["trade_iv"] is not None]
    last = [p for p in prints if p["mark_iv"] is not None][-1]
    return {
        "iv_mean": sum(marks) / len(marks),
        "iv_last": last["mark_iv"],
        "iv_trade_mean": sum(tivs) / len(tivs) if tivs else None,
        "n_iv": len(marks),
    }


def wing_strikes(
    trades: Sequence[dict[str, Any]], q: Mapping[str, Any], *, lo: float = 0.005, hi: float = 0.015, window_h: float = 2.0
) -> dict[str, Any]:
    """OTM call / put strikes of the quote's expiry with prints in the entry window at ``lo..hi`` from K (nearest to 1 %)."""
    t0, K = q["t0"], q["K"]
    rows = _leg_rows(trades, q["expiry"], t0 - int(window_h * HOUR_MS), t0)
    calls = {r["K"] for r in rows if r["call"] and lo <= r["K"] / K - 1.0 <= hi}
    puts = {r["K"] for r in rows if not r["call"] and lo <= 1.0 - r["K"] / K <= hi}
    kc = min(calls, key=lambda k: abs(k / K - 1.01)) if calls else None
    kp = min(puts, key=lambda k: abs(k / K - 0.99)) if puts else None
    return {"K_call": kc, "K_put": kp, "covered": kc is not None and kp is not None}


# ----------------------------------------------------------------------------- regressions


def ols_cluster(
    y: Sequence[float], X: Sequence[Sequence[float]], clusters: Sequence[str], *, w: Sequence[float] | None = None
) -> dict[str, Any]:
    """(Weighted) OLS with cluster-robust (CR1) standard errors and two-sided normal p-values."""
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    wa = np.ones(len(ya)) if w is None else np.asarray(w, dtype=float)
    Xw = Xa * wa[:, None]
    bread = np.linalg.inv(Xa.T @ Xw)
    beta = bread @ (Xw.T @ ya)
    res = ya - Xa @ beta
    n, k = Xa.shape
    idx: dict[str, list[int]] = {}
    for i, c in enumerate(clusters):
        idx.setdefault(c, []).append(i)
    meat = np.zeros((k, k))
    for rows in idx.values():
        s = Xw[rows].T @ res[rows]
        meat += np.outer(s, s)
    g = len(idx)
    corr = g / (g - 1) * (n - 1) / (n - k) if g > 1 and n > k else 1.0
    cov = corr * bread @ meat @ bread
    se = np.sqrt(np.diag(cov))
    p = [math.erfc(abs(b / s) / math.sqrt(2.0)) if s > 0 else float("nan") for b, s in zip(beta, se, strict=True)]
    r2 = 1.0 - float(res @ res) / float(((ya - ya.mean()) ** 2).sum())
    return {"coef": [float(b) for b in beta], "se": [float(s) for s in se], "p": p, "n": n, "clusters": g, "r2": r2}


def huber_cluster(
    y: Sequence[float], X: Sequence[Sequence[float]], clusters: Sequence[str], *, k: float = 1.345, iters: int = 100
) -> dict[str, Any]:
    """Huber M-estimate by IRLS (scale = MAD / 0.6745, tuning ``k``) with the cluster sandwich of the final weighted fit."""
    Xa = np.asarray(X, dtype=float)
    ya = np.asarray(y, dtype=float)
    beta, *_ = np.linalg.lstsq(Xa, ya, rcond=None)
    w = np.ones(len(ya))
    for _ in range(iters):
        res = ya - Xa @ beta
        scale = float(np.median(np.abs(res - np.median(res)))) / 0.6745 or 1.0
        u = np.abs(res) / scale
        w = np.where(u <= k, 1.0, k / np.maximum(u, 1e-12))
        new = np.linalg.solve(Xa.T @ (Xa * w[:, None]), Xa.T @ (w * ya))
        if np.max(np.abs(new - beta)) < 1e-10:
            beta = new
            break
        beta = new
    return ols_cluster(y, X, clusters, w=list(w))


# ----------------------------------------------------------------------------- main slice helpers


def quantile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (numpy 'linear' rule)."""
    return float(np.quantile(np.asarray(xs, dtype=float), q))


def cluster_bootstrap_diff(rows: Sequence[tuple[str, bool, float]], *, n_boot: int = 10_000, seed: int = 20261023) -> dict[str, Any]:
    """Mean(in) - mean(out) of ``(cluster, in_group, value)`` rows with a cluster bootstrap 95 % CI (percentile)."""
    a = [v for _, g, v in rows if g]
    b = [v for _, g, v in rows if not g]
    if not a or not b:
        return {"diff": None, "ci95": None, "n_in": len(a), "n_out": len(b)}
    groups: dict[str, list[float]] = {}
    for c, g, v in rows:
        s = groups.setdefault(c, [0.0, 0.0, 0.0, 0.0])
        if g:
            s[0] += v
            s[1] += 1
        else:
            s[2] += v
            s[3] += 1
    sums = list(groups.values())
    rng = random.Random(seed)
    m = len(sums)
    diffs = []
    for _ in range(n_boot):
        acc = [0.0, 0.0, 0.0, 0.0]
        for _ in range(m):
            s = sums[rng.randrange(m)]
            for j in range(4):
                acc[j] += s[j]
        if acc[1] and acc[3]:
            diffs.append(acc[0] / acc[1] - acc[2] / acc[3])
    diffs.sort()
    nb = len(diffs)
    return {
        "diff": sum(a) / len(a) - sum(b) / len(b),
        "ci95": (diffs[int(0.025 * nb)], diffs[int(0.975 * nb) - 1]),
        "n_in": len(a),
        "n_out": len(b),
        "clusters": m,
    }


def shuffle_within(blocks: Sequence[str], values: Sequence[float], rng: random.Random) -> list[float]:
    """Permute ``values`` within each block label (e.g. calendar month), keeping positions of the blocks."""
    pos: dict[str, list[int]] = {}
    for i, b in enumerate(blocks):
        pos.setdefault(b, []).append(i)
    out = list(values)
    for idx in pos.values():
        vals = [values[i] for i in idx]
        rng.shuffle(vals)
        for i, v in zip(idx, vals, strict=True):
            out[i] = v
    return out
