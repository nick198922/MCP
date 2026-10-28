"""Market-flow model test (IDEA-20261028-market-flow-reader, step 3): pure features, targets, models, metrics.

Hourly grid convention: index ``i`` is the bar ``[T0 + i h, T0 + (i + 1) h)``; the decision time of row ``i`` is the
bar's close ``T0 + (i + 1) h``. Every feature of row ``i`` uses bars ``<= i`` only (trailing windows include the
current, just-closed bar); targets look at bars ``> i``. Rates are decimals (0.10 = 10 %); funding is annualised.

Models are small and dependency-free (numpy): L2 logistic regression (Newton), ridge regression, and a histogram
gradient-boosting of depth-2 trees. No I/O; time enters as arrays.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

H_MS = 3_600_000
HOURS_PER_YEAR = 8760.0


# ---------------------------------------------------------------- rolling helpers (trailing, causal)


def rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Sum of ``x[i - w + 1 .. i]``; NaN for the first ``w - 1`` rows or when any value in the window is NaN."""
    x = np.asarray(x, dtype=float)
    n = x.size
    out = np.full(n, np.nan)
    if w > n:
        return out
    bad = np.concatenate([[0], np.cumsum(~np.isfinite(x))])
    c = np.concatenate([[0.0], np.cumsum(np.where(np.isfinite(x), x, 0.0))])
    s = c[w:] - c[:-w]
    nb = bad[w:] - bad[:-w]
    out[w - 1 :] = np.where(nb == 0, s, np.nan)
    return out


def rolling_max(x: np.ndarray, w: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(x.size, np.nan)
    if w > x.size:
        return out
    v = np.lib.stride_tricks.sliding_window_view(x, w)
    out[w - 1 :] = v.max(axis=1)  # NaN propagates
    return out


def rolling_min(x: np.ndarray, w: int) -> np.ndarray:
    return -rolling_max(-np.asarray(x, dtype=float), w)


def rolling_median(x: np.ndarray, w: int, *, min_count: int | None = None, chunk: int = 4096) -> np.ndarray:
    """Median of finite values in ``x[i - w + 1 .. i]`` (NaN-ignoring), NaN when fewer than ``min_count`` (default w/2)."""
    x = np.asarray(x, dtype=float)
    n = x.size
    out = np.full(n, np.nan)
    if w > n:
        return out
    mc = w // 2 if min_count is None else min_count
    v = np.lib.stride_tricks.sliding_window_view(x, w)
    for a in range(0, v.shape[0], chunk):
        blk = v[a : a + chunk]
        cnt = np.isfinite(blk).sum(axis=1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN windows
            med = np.nanmedian(blk, axis=1)
        out[w - 1 + a : w - 1 + a + blk.shape[0]] = np.where(cnt >= mc, med, np.nan)
    return out


def lag_diff(x: np.ndarray, k: int) -> np.ndarray:
    """``x[i] - x[i - k]`` (NaN for i < k)."""
    x = np.asarray(x, dtype=float)
    out = np.full(x.size, np.nan)
    out[k:] = x[k:] - x[:-k]
    return out


def asof(obs_ms: np.ndarray, values: np.ndarray, query_ms: np.ndarray, *, max_age_ms: int | None = None) -> np.ndarray:
    """Last ``values`` with observation time ``<= query`` (``obs_ms`` sorted); NaN if none or older than ``max_age_ms``."""
    obs = np.asarray(obs_ms, dtype=np.int64)
    val = np.asarray(values, dtype=float)
    q = np.asarray(query_ms, dtype=np.int64)
    j = np.searchsorted(obs, q, side="right") - 1
    ok = j >= 0
    out = np.full(q.size, np.nan)
    out[ok] = val[j[ok]]
    if max_age_ms is not None:
        age = np.where(ok, q - obs[np.clip(j, 0, None)], np.iinfo(np.int64).max)
        out[age > max_age_ms] = np.nan
    return out


# ---------------------------------------------------------------- features


def taker_imbalance(quote_vol: np.ndarray, taker_buy_quote: np.ndarray, w: int) -> np.ndarray:
    """(buy - sell) / total of aggressor quote volume over the trailing ``w`` bars: (2 * taker_buy - vol) / vol."""
    v = rolling_sum(quote_vol, w)
    b = rolling_sum(taker_buy_quote, w)
    with np.errstate(all="ignore"):
        return np.where(v > 0, (2.0 * b - v) / v, np.nan)


def realized_vol(logret: np.ndarray, w: int) -> np.ndarray:
    """sqrt(sum r^2) over the trailing ``w`` bars (not annualised)."""
    return np.sqrt(rolling_sum(np.asarray(logret, dtype=float) ** 2, w))


def price_features(close: np.ndarray, high: np.ndarray, low: np.ndarray) -> dict[str, np.ndarray]:
    """Baseline P (7): log returns 1/4/24 h, log RV24, log RV168, position in and log width of the 7-day range."""
    lc = np.log(np.asarray(close, dtype=float))
    r1 = lag_diff(lc, 1)
    h168 = rolling_max(high, 168)
    l168 = rolling_min(low, 168)
    with np.errstate(all="ignore"):
        pos = (np.asarray(close) - l168) / (h168 - l168)
        return {
            "r1": r1,
            "r4": lag_diff(lc, 4),
            "r24": lag_diff(lc, 24),
            "lrv24": np.log(realized_vol(r1, 24)),
            "lrv168": np.log(realized_vol(r1, 168)),
            "range_pos": pos,
            "range_lw": np.log(h168 / l168),
        }


def har_features(close: np.ndarray) -> dict[str, np.ndarray]:
    """HAR-RV regressors in 24 h units: log RV of the last day, week and month (sqrt(mean r^2 * 24))."""
    r = lag_diff(np.log(np.asarray(close, dtype=float)), 1)
    r2 = r**2
    with np.errstate(all="ignore"):
        return {
            "har_d": np.log(np.sqrt(rolling_sum(r2, 24))),
            "har_w": np.log(np.sqrt(rolling_sum(r2, 168) / 168.0 * 24.0)),
            "har_m": np.log(np.sqrt(rolling_sum(r2, 720) / 720.0 * 24.0)),
        }


def funding_features(settle_ms: np.ndarray, rate: np.ndarray, interval_h: np.ndarray, decision_ms: np.ndarray) -> dict[str, np.ndarray]:
    """Last settled funding (annualised) and the mean annualised funding of settlements in the trailing 3 days."""
    t = np.asarray(settle_ms, dtype=np.int64)
    o = np.argsort(t, kind="stable")
    t = t[o]
    ih = np.asarray(interval_h, dtype=float)[o]
    ih = np.where(ih > 0, ih, 8.0)
    ann = np.asarray(rate, dtype=float)[o] * HOURS_PER_YEAR / ih
    last = asof(t, ann, decision_ms, max_age_ms=26 * H_MS)
    c = np.concatenate([[0.0], np.cumsum(ann)])
    q = np.asarray(decision_ms, dtype=np.int64)
    hi = np.searchsorted(t, q, side="right")
    lo = np.searchsorted(t, q - 72 * H_MS, side="right")
    cnt = hi - lo
    with np.errstate(all="ignore"):
        mean3 = np.where(cnt > 0, (c[hi] - c[lo]) / np.maximum(cnt, 1), np.nan)
    return {"fund_last": last, "fund_3d": mean3}


def coinbase_premium(cb_close: np.ndarray, spot_close: np.ndarray, *, med_w: int = 720, mean_w: int = 24) -> np.ndarray:
    """Mean over ``mean_w`` hours of (Coinbase / Binance spot - 1) minus its trailing ``med_w``-hour median."""
    with np.errstate(all="ignore"):
        p = np.asarray(cb_close, dtype=float) / np.asarray(spot_close, dtype=float) - 1.0
    dev = p - rolling_median(p, med_w)
    return rolling_sum(dev, mean_w) / mean_w


def flow_features(
    *,
    perp_qv: np.ndarray,
    perp_tbq: np.ndarray,
    spot_qv: np.ndarray,
    spot_tbq: np.ndarray,
    oi: np.ndarray,
    fund_last: np.ndarray,
    fund_3d: np.ndarray,
    premium: np.ndarray,
    r24: np.ndarray,
) -> dict[str, np.ndarray]:
    """Registered F (10): taker imbalances perp/spot 4/24 h, dlog OI 4/24 h, funding last/3d, Coinbase premium, r24*dOI24."""
    with np.errstate(all="ignore"):
        loi = np.log(np.asarray(oi, dtype=float))
    doi24 = lag_diff(loi, 24)
    return {
        "imb_perp4": taker_imbalance(perp_qv, perp_tbq, 4),
        "imb_perp24": taker_imbalance(perp_qv, perp_tbq, 24),
        "imb_spot4": taker_imbalance(spot_qv, spot_tbq, 4),
        "imb_spot24": taker_imbalance(spot_qv, spot_tbq, 24),
        "doi4": lag_diff(loi, 4),
        "doi24": doi24,
        "fund_last": np.asarray(fund_last, dtype=float),
        "fund_3d": np.asarray(fund_3d, dtype=float),
        "cb_prem": np.asarray(premium, dtype=float),
        "r24_x_doi24": np.asarray(r24, dtype=float) * doi24,
    }


VOL_ABS = ("imb_perp4", "imb_perp24", "imb_spot4", "imb_spot24", "doi24")


# ---------------------------------------------------------------- targets


def forward_logret(close: np.ndarray, h: int) -> np.ndarray:
    lc = np.log(np.asarray(close, dtype=float))
    out = np.full(lc.size, np.nan)
    out[:-h] = lc[h:] - lc[:-h]
    return out


def forward_log_rv(close: np.ndarray, h: int = 24) -> np.ndarray:
    """log sqrt(sum of squared hourly log returns of bars i+1 .. i+h)."""
    r = lag_diff(np.log(np.asarray(close, dtype=float)), 1)
    s = rolling_sum(r**2, h)  # s[j] = sum r[j-h+1..j]
    out = np.full(r.size, np.nan)
    out[:-h] = s[h:]
    with np.errstate(all="ignore"):
        return np.log(np.sqrt(out))


@dataclass(frozen=True)
class RangeEvent:
    i: int  # origin row (decision at its close)
    up: int  # 1 = high of the range crossed first, 0 = low
    resolve_i: int  # bar in which the exit happened
    width: float  # log(H / L)


def range_exit_events(
    high: np.ndarray,
    low: np.ndarray,
    origins: Sequence[int],
    *,
    lookback: int = 168,
    horizon: int = 168,
    width_window: int = 365,
    min_prior: int = 180,
) -> list[RangeEvent]:
    """Range-state origins and which side of the trailing ``lookback``-bar range is crossed first.

    An origin qualifies when log(H/L) of bars ``i - lookback + 1 .. i`` is <= the median width of earlier origins
    (the last ``width_window`` of them, at least ``min_prior``). The outcome scans bars ``i + 1 .. i + horizon``: a
    high above H -> up, a low below L -> down; both in one bar or neither -> no event. Origins must be sorted."""
    hi = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    n = hi.size
    widths: list[float] = []
    out: list[RangeEvent] = []
    for i in origins:
        if i - lookback + 1 < 0:
            continue
        H = np.max(hi[i - lookback + 1 : i + 1])
        L = np.min(lo[i - lookback + 1 : i + 1])
        if not (np.isfinite(H) and np.isfinite(L) and L > 0):
            continue
        w = float(np.log(H / L))
        prior = widths[-width_window:]
        widths.append(w)
        if len(prior) < min_prior or w > float(np.median(prior)):
            continue
        if i + horizon >= n:
            continue
        fh = hi[i + 1 : i + horizon + 1]
        fl = lo[i + 1 : i + horizon + 1]
        if not (np.all(np.isfinite(fh)) and np.all(np.isfinite(fl))):
            continue
        u = np.flatnonzero(fh > H)
        d = np.flatnonzero(fl < L)
        ju = u[0] if u.size else horizon
        jd = d[0] if d.size else horizon
        if ju == jd:
            continue
        out.append(RangeEvent(i=int(i), up=int(ju < jd), resolve_i=int(i + 1 + min(ju, jd)), width=w))
    return out


# ---------------------------------------------------------------- models


def standardize(train: np.ndarray, *others: np.ndarray) -> list[np.ndarray]:
    mu = train.mean(axis=0)
    sd = train.std(axis=0)
    sd = np.where(sd > 1e-12, sd, 1.0)
    return [(train - mu) / sd, *[(o - mu) / sd for o in others]]


def fit_logistic(X: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 50) -> np.ndarray:
    """L2 logistic regression by Newton; intercept (last weight) not penalised. Returns weights incl. intercept."""
    A = np.hstack([X, np.ones((X.shape[0], 1))])
    w = np.zeros(A.shape[1])
    pen = np.full(A.shape[1], lam)
    pen[-1] = 0.0
    for _ in range(iters):
        z = np.clip(A @ w, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = A.T @ (p - y) + pen * w
        Hm = (A * (p * (1 - p))[:, None]).T @ A + np.diag(pen) + 1e-9 * np.eye(A.shape[1])
        step = np.linalg.solve(Hm, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def predict_logistic(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    z = np.clip(X @ w[:-1] + w[-1], -30, 30)
    return 1.0 / (1.0 + np.exp(-z))


def fit_ridge(X: np.ndarray, y: np.ndarray, lam: float = 1.0) -> np.ndarray:
    A = np.hstack([X, np.ones((X.shape[0], 1))])
    pen = np.full(A.shape[1], lam)
    pen[-1] = 0.0
    return np.linalg.solve(A.T @ A + np.diag(pen), A.T @ y)


def predict_linear(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return X @ w[:-1] + w[-1]


def _bin_edges(X: np.ndarray, nb: int) -> list[np.ndarray]:
    qs = np.linspace(0, 1, nb + 1)[1:-1]
    return [np.unique(np.quantile(X[:, j], qs)) for j in range(X.shape[1])]


def _binize(X: np.ndarray, edges: list[np.ndarray]) -> np.ndarray:
    return np.stack([np.searchsorted(e, X[:, j], side="right") for j, e in enumerate(edges)], axis=1).astype(np.int64)


def _best_split(
    Xb: np.ndarray, g: np.ndarray, h: np.ndarray, idx: np.ndarray, nb: int, lam: float, min_leaf: int
) -> tuple[float, int, int]:
    """(gain, feature, bin) of the best split ``bin <= b`` -> left on rows ``idx``; gain 0 when none."""
    d = Xb.shape[1]
    if idx.size < 2 * min_leaf:
        return 0.0, -1, -1
    flat = (Xb[idx] + np.arange(d) * nb).ravel()
    G = np.bincount(flat, weights=np.repeat(g[idx], d), minlength=d * nb).reshape(d, nb)
    Hh = np.bincount(flat, weights=np.repeat(h[idx], d), minlength=d * nb).reshape(d, nb)
    C = np.bincount(flat, minlength=d * nb).reshape(d, nb)
    GL, HL, CL = np.cumsum(G, 1)[:, :-1], np.cumsum(Hh, 1)[:, :-1], np.cumsum(C, 1)[:, :-1]
    Gt, Ht, Ct = G.sum(1)[:, None], Hh.sum(1)[:, None], C.sum(1)[:, None]
    GR, HR, CR = Gt - GL, Ht - HL, Ct - CL
    gain = GL**2 / (HL + lam) + GR**2 / (HR + lam) - Gt**2 / (Ht + lam)
    gain = np.where((CL >= min_leaf) & (CR >= min_leaf), gain, -np.inf)
    k = int(np.argmax(gain))
    f, b = divmod(k, nb - 1)
    best = float(gain[f, b])
    return (best, f, b) if np.isfinite(best) and best > 0 else (0.0, -1, -1)


@dataclass
class GBModel:
    edges: list[np.ndarray]
    trees: list[tuple[int, int, int, int, int, int, np.ndarray]]  # (f0, b0, fL, bL, fR, bR, leaf values[4])
    base: float
    lr: float
    loss: str

    def raw(self, X: np.ndarray) -> np.ndarray:
        Xb = _binize(X, self.edges)
        out = np.full(X.shape[0], self.base)
        for f0, b0, fL, bL, fR, bR, v in self.trees:
            left = Xb[:, f0] <= b0
            leaf = np.where(
                left, np.where(Xb[:, max(fL, 0)] <= bL, 0, 1) if fL >= 0 else 0, np.where(Xb[:, max(fR, 0)] <= bR, 2, 3) if fR >= 0 else 2
            )
            out += self.lr * v[leaf]
        return out

    def predict(self, X: np.ndarray) -> np.ndarray:
        r = self.raw(X)
        return 1.0 / (1.0 + np.exp(-np.clip(r, -30, 30))) if self.loss == "logistic" else r


def fit_gb(
    X: np.ndarray, y: np.ndarray, *, loss: str, rounds: int = 150, lr: float = 0.05, nb: int = 32, lam: float = 1.0, min_leaf: int = 200
) -> GBModel:
    """Histogram gradient boosting with depth-2 trees (logistic or squared loss)."""
    edges = _bin_edges(X, nb)
    Xb = _binize(X, edges)
    nbins = nb  # searchsorted gives 0..len(edges) <= nb - 1
    if loss == "logistic":
        pbar = float(np.clip(y.mean(), 1e-6, 1 - 1e-6))
        base = float(np.log(pbar / (1 - pbar)))
    else:
        base = float(y.mean())
    F = np.full(y.size, base)
    trees = []
    all_idx = np.arange(y.size)
    for _ in range(rounds):
        if loss == "logistic":
            p = 1.0 / (1.0 + np.exp(-np.clip(F, -30, 30)))
            g, h = p - y, np.maximum(p * (1 - p), 1e-6)
        else:
            g, h = F - y, np.ones_like(y)
        gain, f0, b0 = _best_split(Xb, g, h, all_idx, nbins, lam, min_leaf)
        if f0 < 0:
            break
        left = Xb[:, f0] <= b0
        li, ri = all_idx[left], all_idx[~left]
        _, fL, bL = _best_split(Xb, g, h, li, nbins, lam, min_leaf)
        _, fR, bR = _best_split(Xb, g, h, ri, nbins, lam, min_leaf)
        leaf = np.where(left, 0, 2)
        if fL >= 0:
            leaf = np.where(left & (Xb[:, fL] > bL), 1, leaf)
        if fR >= 0:
            leaf = np.where(~left & (Xb[:, fR] > bR), 3, leaf)
        v = np.zeros(4)
        for k in range(4):
            m = leaf == k
            if m.any():
                v[k] = -g[m].sum() / (h[m].sum() + lam)
        F += lr * v[leaf]
        trees.append((f0, b0, fL, bL, fR, bR, v))
    return GBModel(edges, trees, base, lr, loss)


# ---------------------------------------------------------------- walk-forward


def month_starts(first_ms: int, last_ms: int) -> list[int]:
    """UTC month starts in [first_ms, last_ms]."""
    d = datetime.fromtimestamp(first_ms / 1000, UTC)
    y, m = d.year, d.month
    if d != datetime(y, m, 1, tzinfo=UTC):
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out = []
    while True:
        t = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
        if t > last_ms:
            return out
        out.append(t)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def walk_forward(
    X: np.ndarray,
    y: np.ndarray,
    t_ms: np.ndarray,
    resolve_ms: np.ndarray,
    test_months: Sequence[int],
    fit: Callable[[np.ndarray, np.ndarray], Any],
    predict: Callable[[Any, np.ndarray], np.ndarray],
    *,
    min_train: int = 50,
) -> np.ndarray:
    """OOS predictions: for each month start m (next = following start or +inf), train on rows resolved before m
    (standardised on that training set only), predict rows with ``m <= t < next``. NaN where not predicted."""
    pred = np.full(y.size, np.nan)
    ms = list(test_months)
    for k, m in enumerate(ms):
        nxt = ms[k + 1] if k + 1 < len(ms) else np.iinfo(np.int64).max
        tr = resolve_ms < m
        te = (t_ms >= m) & (t_ms < nxt)
        if tr.sum() < min_train or not te.any():
            continue
        Xtr, Xte = standardize(X[tr], X[te])
        model = fit(Xtr, y[tr])
        pred[te] = predict(model, Xte)
    return pred


# ---------------------------------------------------------------- metrics and inference


def auc(y: np.ndarray, s: np.ndarray) -> float:
    """ROC AUC with average ranks for ties (Mann-Whitney)."""
    y = np.asarray(y, dtype=float)
    s = np.asarray(s, dtype=float)
    n1 = y.sum()
    n0 = y.size - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    o = np.argsort(s, kind="mergesort")
    ss = s[o]
    ranks = np.empty(s.size)
    _, start, cnt = np.unique(ss, return_index=True, return_counts=True)  # average ranks over ties
    ranks[o] = np.repeat(start + (cnt + 1) / 2.0, cnt)
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def r2_oos(y: np.ndarray, pred: np.ndarray) -> float:
    y = np.asarray(y, dtype=float)
    sst = float(((y - y.mean()) ** 2).sum())
    return float(1.0 - ((y - pred) ** 2).sum() / sst) if sst > 0 else float("nan")


def accuracy(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((np.asarray(p) >= 0.5) == (np.asarray(y) == 1)))


def block_bootstrap_delta(
    groups: np.ndarray, stat: Callable[[np.ndarray], float], *, reps: int = 2000, seed: int = 20261028
) -> dict[str, float]:
    """Bootstrap over whole groups (e.g. ISO weeks) of a statistic of row indices. Returns the point value, the 95 %
    percentile CI, and the one-sided p = share of replicates <= 0."""
    g = np.asarray(groups)
    uniq, inv = np.unique(g, return_inverse=True)
    members = [np.flatnonzero(inv == k) for k in range(uniq.size)]
    rng = np.random.default_rng(seed)
    point = stat(np.arange(g.size))
    vals = np.empty(reps)
    for b in range(reps):
        pick = rng.integers(0, uniq.size, uniq.size)
        vals[b] = stat(np.concatenate([members[k] for k in pick]))
    lo, hi = np.nanpercentile(vals, [2.5, 97.5])
    return {"point": float(point), "lo": float(lo), "hi": float(hi), "p_le0": float(np.mean(vals <= 0)), "n_groups": int(uniq.size)}


def holm(pvals: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values (monotone, capped at 1)."""
    p = np.asarray(pvals, dtype=float)
    o = np.argsort(p)
    m = p.size
    adj = np.empty(m)
    run = 0.0
    for k, j in enumerate(o):
        run = max(run, (m - k) * p[j])
        adj[j] = min(1.0, run)
    return adj.tolist()


def circular_shift(a: np.ndarray, k: int) -> np.ndarray:
    """Rows shifted cyclically by ``k`` (placebo: breaks the time link of a feature block, keeps its distribution)."""
    return np.roll(np.asarray(a), k, axis=0)
