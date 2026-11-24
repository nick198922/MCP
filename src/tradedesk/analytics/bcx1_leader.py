"""BCX-1, the intraday leader by residual 12 h return (IDEA-20261113-x-traders, item 5; spec «4б» BCX-1 of
LIB-20261114-x-btc_charlie). Our formalisation of the author's «spaghetti» rotation motive, tested both ways.

Cross-section on the hourly grid (row k = the 1h bar opening at ``t0 + k`` hours; its close is the decision time):

* ``lagged_beta``: OLS slope of the coin's 1h log returns on BTC's over ``window`` hourly returns that end ``lag`` bars
  before row k (the 12 h measuring window is not part of the fit), with at least ``min_obs`` pairs where both exist.
* ``residual_return``: e12 = r12(coin) - beta * r12(BTC), simple 12 h close-to-close returns.
* ``top_k_mask``: members of the row's universe with a finite e12 among the ``k`` largest of that row.
* ``leader_events``: the coin is in the top-k now, was in it at none of the previous ``lookback`` rows, has
  e12 >= ``min_e`` and BTC's 3 h return at that close is >= ``btc_floor``.
* ``momentum_loss_bar``: the reversal branch trigger after an event at coin bar j: the first bar k in
  j + n_low .. j + max_wait whose close is below the lows of the ``n_low`` bars before it while the highest high since
  bar j was set at least ``n_stale`` bars before k.
* ``placebo_bars``: random trigger bars of the same coin within +-``half`` bars with a hole-free path.

Trades (both branches) are held over hole-free 1h paths with ``xo4_vwap.vwap_exit_outcomes`` and an infinitely far
level (stop or the 12 h time exit only), entry at the open of the bar after the trigger, stop ``stop_atr`` x ATR(24)
of the trigger bar away from the entry. R is the live risk unit: a return on the trade's risk, not on capital.

Pure functions, no I/O. Row k uses grid rows up to k only, the reversal trigger coin bars up to k only, so nothing
before time t changes when data after t change.
"""

from __future__ import annotations

import numpy as np

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


def on_grid(t: np.ndarray, x: np.ndarray, t0: int, n: int) -> np.ndarray:
    """Values ``x`` at open times ``t`` placed on the hourly grid of ``n`` rows from ``t0`` (nan where no bar)."""
    out = np.full(n, np.nan)
    t = np.asarray(t, dtype=np.int64)
    k = (t - t0) // HOUR_MS
    ok = (k >= 0) & (k < n) & ((t - t0) % HOUR_MS == 0)
    out[k[ok]] = np.asarray(x, dtype=float)[ok]
    return out


def _log_ret(c: np.ndarray) -> np.ndarray:
    r = np.full(c.size, np.nan)
    if c.size > 1:
        with np.errstate(invalid="ignore", divide="ignore"):
            r[1:] = np.log(c[1:] / c[:-1])
    return r


def lagged_beta(c_coin: np.ndarray, c_btc: np.ndarray, *, window: int = 720, lag: int = 12, min_obs: int = 360) -> np.ndarray:
    """Per grid row k: OLS beta of the coin's hourly log returns on BTC's over rows k - lag - window + 1 .. k - lag
    (pairs where both returns are finite; nan with fewer than ``min_obs`` pairs or a flat BTC)."""
    x, y = _log_ret(np.asarray(c_btc, dtype=float)), _log_ret(np.asarray(c_coin, dtype=float))
    ok = np.isfinite(x) & np.isfinite(y)
    x0, y0 = np.where(ok, x, 0.0), np.where(ok, y, 0.0)
    cs = {
        k: np.concatenate([[0.0], np.cumsum(v)])
        for k, v in (("n", ok.astype(float)), ("x", x0), ("y", y0), ("xx", x0 * x0), ("xy", x0 * y0))
    }
    n_rows = x.size
    out = np.full(n_rows, np.nan)
    k = np.arange(n_rows)
    hi = k - lag + 1  # exclusive end in cumsum coordinates: rows .. k - lag
    lo = hi - window
    v = lo >= 0
    kk, a, b = k[v], lo[v], hi[v]
    n = cs["n"][b] - cs["n"][a]
    sx, sy = cs["x"][b] - cs["x"][a], cs["y"][b] - cs["y"][a]
    sxx, sxy = cs["xx"][b] - cs["xx"][a], cs["xy"][b] - cs["xy"][a]
    with np.errstate(invalid="ignore", divide="ignore"):
        varx = sxx - sx * sx / n
        beta = (sxy - sx * sy / n) / varx
    good = (n >= min_obs) & (varx > 1e-12)
    out[kk[good]] = beta[good]
    return out


def residual_return(c_coin: np.ndarray, c_btc: np.ndarray, beta: np.ndarray, n: int = 12) -> np.ndarray:
    """e_n = r_n(coin) - beta * r_n(BTC), simple close-to-close returns over ``n`` grid rows (nan where any is missing)."""
    c, b = np.asarray(c_coin, dtype=float), np.asarray(c_btc, dtype=float)
    out = np.full(c.size, np.nan)
    if c.size > n:
        with np.errstate(invalid="ignore", divide="ignore"):
            out[n:] = (c[n:] / c[:-n] - 1.0) - beta[n:] * (b[n:] / b[:-n] - 1.0)
    return out


def top_k_mask(e: np.ndarray, member: np.ndarray, k: int = 3) -> np.ndarray:
    """Rows x coins: True for the universe members with a finite value among the ``k`` largest of their row."""
    x = np.where(member & np.isfinite(e), e, -np.inf)
    if x.shape[1] <= k:
        return np.isfinite(x)
    kth = np.partition(x, x.shape[1] - k, axis=1)[:, x.shape[1] - k]
    return (x >= kth[:, None]) & np.isfinite(x)


def leader_events(
    e: np.ndarray,
    member: np.ndarray,
    btc_r3: np.ndarray,
    *,
    k: int = 3,
    min_e: float = 0.05,
    lookback: int = 24,
    btc_floor: float = -0.02,
) -> tuple[np.ndarray, np.ndarray]:
    """(rows, columns) of the events (see the module docstring); rows before ``lookback`` never fire."""
    top = top_k_mask(e, member, k)
    cs = np.concatenate([np.zeros((1, top.shape[1]), dtype=np.int32), np.cumsum(top, axis=0, dtype=np.int32)])
    rows = np.arange(top.shape[0])
    prior = np.zeros_like(top)
    v = rows >= lookback
    prior[v] = (cs[rows[v]] - cs[rows[v] - lookback]) > 0  # any top-k in rows r - lookback .. r - 1
    with np.errstate(invalid="ignore"):
        btc_ok = np.asarray(btc_r3, dtype=float) >= btc_floor
        sig = top & ~prior & (e >= min_e) & btc_ok[:, None] & v[:, None]
    r, c = np.nonzero(sig)
    return r.astype(np.int64), c.astype(np.int64)


def momentum_loss_bar(
    t: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, j: int, *, max_wait: int = 24, n_low: int = 3, n_stale: int = 3
) -> int:
    """Coin bar index of the reversal trigger after the event bar ``j`` (consecutive hours only), or -1."""
    t = np.asarray(t, dtype=np.int64)
    end = min(j + max_wait, t.size - 1)
    for kk in range(j + n_low, end + 1):
        if t[kk] - t[j] != (kk - j) * HOUR_MS:
            return -1  # a hole in the bars: the leader cannot be watched continuously
        peak = j + int(np.argmax(h[j : kk + 1]))
        if c[kk] < float(np.min(low[kk - n_low : kk])) and peak <= kk - n_stale:
            return kk
    return -1


def full_path(t: np.ndarray, j: int, n_hold: int) -> bool:
    """Bars j + 1 .. j + n_hold exist and are consecutive hours after bar j."""
    t = np.asarray(t, dtype=np.int64)
    return j + n_hold < t.size and t[j + n_hold] - t[j] == n_hold * HOUR_MS


def placebo_bars(t: np.ndarray, j: np.ndarray, n_hold: int, n_sets: int, half: int, rng: np.random.Generator) -> np.ndarray:
    """(len(j), n_sets) random trigger bars of the same coin, uniform over the bars within +-``half`` hours of each
    real trigger that have a full hole-free path of ``n_hold`` bars (-1 where none exists)."""
    t = np.asarray(t, dtype=np.int64)
    i = np.arange(max(0, t.size - n_hold), dtype=np.int64)
    cand = i[t[i + n_hold] - t[i] == n_hold * HOUR_MS] if i.size else i
    out = np.full((len(j), n_sets), -1, dtype=np.int64)
    for r, jj in enumerate(np.asarray(j, dtype=np.int64)):
        a = np.searchsorted(t[cand], t[jj] - half * HOUR_MS, side="left")
        b = np.searchsorted(t[cand], t[jj] + half * HOUR_MS, side="right")
        if b > a:
            out[r] = cand[a + rng.integers(0, b - a, n_sets)]
    return out


def icc_oneway(values: np.ndarray, clusters: np.ndarray) -> float:
    """One-way ANOVA intraclass correlation of ``values`` within ``clusters`` (unequal sizes: n0 of Searle)."""
    v = np.asarray(values, dtype=float)
    _, c = np.unique(np.asarray(clusters), return_inverse=True)
    g = int(c.max()) + 1 if c.size else 0
    if g < 2 or v.size <= g:
        return float("nan")
    n_i = np.bincount(c, minlength=g).astype(float)
    m_i = np.bincount(c, weights=v, minlength=g) / n_i
    m = v.mean()
    msb = float(np.sum(n_i * (m_i - m) ** 2) / (g - 1))
    msw = float(np.sum((v - m_i[c]) ** 2) / (v.size - g))
    n0 = (v.size - np.sum(n_i**2) / v.size) / (g - 1)
    den = msb + (n0 - 1) * msw
    return float((msb - msw) / den) if den > 0 else float("nan")
