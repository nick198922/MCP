"""Setup passport (IDEA-20261030-demo-setups-depth): the reference distribution of a demo setup's trades on history,
with the demo's rules, against which its live trades are later compared.

Pure functions, no I/O, time is a parameter. The runner is ``scripts/verify_setup_passport.py``; the pre-registration
is in the card. Everything here is causal: a value attached to a trade entered at ``t`` uses data closed by ``t`` only
(``tests/test_setup_passport.py`` checks that data after ``t`` do not change it).

- ``trend_series``: ``setup_signals.trend_dir`` of every prefix of a daily series (the demo's daily trend of a coin);
- ``score_points``: the demo's confidence score (``setup_signals.score``) from precomputed trends;
- ``trailing_turnover``: quote turnover of the hours before a time (the 24h turnover the demo sees when it scans);
- ``turnover_group``: disjoint groups top-30 / >= $5M / $1-5M;
- ``state_before``: the state of the last UTC day closed by a time (the BTC cycle regime on the signal day);
- ``busy_chain``: one open trade per symbol (a later signal of a busy symbol is skipped);
- ``busy_chain_capped``: the same with at most ``cap`` entries per entry time (S38's 2 per signal bar on the demo);
- ``late_entry``: the entry one bar of the signal timeframe later (stress);
- ``limit_fill`` / ``limit_trade``: a resting limit (S12 v7, S28) filled only when the price trades through it (or
  touches it), pessimistic in the fill hour, then the trade under a stop rule;
- ``mean_ci`` / ``diff_ci``: mean with a cluster (weekly block) bootstrap CI and two-sided p, and a group contrast;
- ``r_band``: the 5th..95th percentile of the mean R of ``n`` trades (i.i.d. draws, and rolling consecutive windows).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .course_s14_ema import DAY_MS, Bars, ema, segment_pos
from .stop_management import Fees, Outcome, StopRule, simulate_stop_rule

WEEK_MS = 7 * DAY_MS
TOP30, GE5M, M1TO5, LT1M, UNKNOWN = "top30", "ge5m", "1to5m", "lt1m", "unknown"


def trend_series(d: Bars) -> np.ndarray:
    """+1 / -1 / 0 for every bar k of the daily series ``d``: ``setup_signals.trend_dir(d[: k + 1])`` (close vs EMA34
    and the slope of EMA21; 0 for the first 59 bars or without finite EMAs). The EMAs are recursive, so bar k uses bars
    <= k only."""
    n = len(d)
    out = np.zeros(n, dtype=np.int64)
    if n < 60:
        return out
    pos = segment_pos(d.t)
    e21, e34 = ema(d.c, 21, pos), ema(d.c, 34, pos)
    prev = np.concatenate([[np.nan], e21[:-1]])
    ok = np.isfinite(e21) & np.isfinite(prev) & np.isfinite(e34)
    up = ok & (d.c > e34) & (e21 > prev)
    dn = ok & (d.c < e34) & (e21 < prev)
    out[up] = 1
    out[dn] = -1
    out[:59] = 0
    return out


def trend_at(d: Bars, trend: np.ndarray, t_ms: int) -> int:
    """Trend of the last daily bar of ``d`` closed by ``t_ms`` (0 if none)."""
    j = int(np.searchsorted(d.t + d.bar_ms, t_ms, side="right")) - 1
    return int(trend[j]) if j >= 0 else 0


def score_points(side: str, rr_after_fees: float, coin_trend: int, btc_trend: int, is_btc: bool) -> int:
    """``setup_signals.score`` points: the coin's daily trend agrees, BTC's agrees (alts only), R:R >= 2, R:R >= 3."""
    want = 1 if side == "long" else -1
    pts = int(coin_trend == want)
    if not is_btc:
        pts += int(btc_trend == want)
    return pts + int(rr_after_fees >= 2) + int(rr_after_fees >= 3)


def trailing_turnover(t_ms: int, h_t: np.ndarray, h_qv: np.ndarray, hours: int = 24, min_bars: int = 20) -> float | None:
    """Sum of the quote volume of the 1h bars opening in [t_ms - hours h, t_ms) (``h_t`` sorted open times); None
    if fewer than ``min_bars`` of them exist."""
    i0 = int(np.searchsorted(h_t, t_ms - hours * 3_600_000, side="left"))
    i1 = int(np.searchsorted(h_t, t_ms, side="left"))
    if i1 - i0 < min_bars:
        return None
    return float(h_qv[i0:i1].sum())


def turnover_group(top30: bool, turnover_usd: float | None) -> str:
    if top30:
        return TOP30
    if turnover_usd is None or not math.isfinite(turnover_usd):
        return UNKNOWN
    if turnover_usd >= 5e6:
        return GE5M
    if turnover_usd >= 1e6:
        return M1TO5
    return LT1M


def state_before(t_ms: int, states: Mapping[int, str | None]) -> str:
    """State of the UTC day whose bar closes at or before ``t_ms`` last (keyed by the day's open ms); 'none' if that
    day has no decided state."""
    day_open = (t_ms // DAY_MS) * DAY_MS - DAY_MS
    s = states.get(day_open)
    return s if s else "none"


def busy_chain(sym: Sequence[str], t_in: np.ndarray, exit_ms: np.ndarray, order: np.ndarray) -> np.ndarray:
    """Mask of the trades kept with one open trade per symbol: candidates in (``t_in``, ``order``) order, a candidate
    entering before the exit of the symbol's last kept trade is skipped. Whether trade i is kept depends only on
    candidates entering at or before it."""
    n = len(sym)
    keep = np.zeros(n, dtype=bool)
    idx = sorted(range(n), key=lambda i: (int(t_in[i]), int(order[i])))
    busy: dict[str, int] = {}
    for i in idx:
        if int(t_in[i]) < busy.get(sym[i], -1):
            continue
        keep[i] = True
        busy[sym[i]] = int(exit_ms[i])
    return keep


def busy_chain_capped(sym: Sequence[str], t_in: np.ndarray, exit_ms: np.ndarray, rank: np.ndarray, cap: int) -> np.ndarray:
    """``busy_chain`` with at most ``cap`` trades entered at the same ``t_in`` (the demo's ``BAR_CAP``: entries per
    signal bar of a setup). Candidates of one entry time are taken in ``rank`` order (lower first: the demo's best
    first); a busy symbol is skipped without using the cap, a candidate over the cap does not occupy its symbol.
    Whether trade i is kept depends only on candidates entering at or before it."""
    n = len(sym)
    keep = np.zeros(n, dtype=bool)
    idx = sorted(range(n), key=lambda i: (int(t_in[i]), float(rank[i])))
    busy: dict[str, int] = {}
    taken: dict[int, int] = {}
    for i in idx:
        t = int(t_in[i])
        if t < busy.get(sym[i], -1) or taken.get(t, 0) >= cap:
            continue
        keep[i] = True
        taken[t] = taken.get(t, 0) + 1
        busy[sym[i]] = int(exit_ms[i])
    return keep


def late_entry(b: Bars, i: int, stop: float, target: float) -> tuple[int, float] | None:
    """(entry time, entry price) one bar after signal bar ``i`` of ``b`` (long price space): the close of bar i + 1,
    if it follows i without a hole and lies strictly between the stop and the target; else None (no trade)."""
    j = i + 1
    if j >= len(b) or int(b.t[j]) - int(b.t[i]) != b.bar_ms:
        return None
    c = float(b.c[j])
    if not (stop < c < target):
        return None
    return int(b.t[j]) + b.bar_ms, c


def limit_fill(path: Bars, t0: int, t1: int, level: float, through: bool = True, cancel_at: float | None = None) -> int:
    """Index of the first bar of ``path`` (long price space) opening in [t0, t1) that fills a resting buy limit at
    ``level``: its low trades strictly below the level (``through``) or reaches it (``through=False``, a touch); -1 if
    none. ``cancel_at`` (S23, ``sfp_backtest.ltf_entry`` "retest"): a bar that does not fill but whose high reaches
    it cancels the limit (-1); in the fill bar the fill comes first. Bars outside [t0, t1) are never read."""
    i0, i1 = int(np.searchsorted(path.t, t0)), int(np.searchsorted(path.t, t1))
    lows = path.low[i0:i1]
    hit = np.flatnonzero(lows < level if through else lows <= level)
    k = int(hit[0]) if hit.size else -1
    if cancel_at is not None:
        gone = np.flatnonzero(path.h[i0:i1] >= cancel_at)
        if gone.size and (k < 0 or int(gone[0]) < k):
            return -1
    return i0 + k if k >= 0 else -1


def limit_trade(
    path: Bars,
    t0: int,
    t1: int,
    level: float,
    stop: float,
    target: float,
    hold_ms: int,
    rule: StopRule,
    fees: Fees,
    through: bool = True,
    cancel_at_target: bool = False,
) -> tuple[Outcome | None, int]:
    """A buy limit resting at ``level`` from ``t0`` to ``t1`` (long price space, 1h ``path``), then the trade under
    ``rule``: (outcome, open time of the fill bar). (None, -1): never filled (or cancelled: with ``cancel_at_target``
    a bar before the fill that reaches ``target`` cancels the limit, ``limit_fill``); (None, t): filled without a path.
    Pessimistic in the fill hour (order inside the bar unknown): a fill bar that also reaches the stop is a loss at the
    stop; otherwise the trade runs from the next bar, held ``hold_ms`` from the fill hour, and a target or a break-even
    trigger inside the fill hour is ignored. Uses bars from ``t0`` up to the exit only."""
    k = limit_fill(path, t0, t1, level, through, target if cancel_at_target else None)
    if k < 0:
        return None, -1
    tk = int(path.t[k])
    if path.low[k] <= stop or k + 1 >= len(path):
        return simulate_stop_rule(path, tk, level, stop, target, hold_ms, rule, fees=fees), tk
    t_next = int(path.t[k + 1])
    return simulate_stop_rule(path, t_next, level, stop, target, hold_ms - (t_next - tk), rule, fees=fees), tk


def _boot(v: np.ndarray, groups: list[np.ndarray], n_boot: int, seed: int) -> np.ndarray:
    """Bootstrap means of ``v`` resampling the index groups (clusters) with replacement."""
    rng = np.random.default_rng(seed)
    sums = np.array([v[g].sum() for g in groups])
    cnts = np.array([g.size for g in groups], dtype=float)
    k = len(groups)
    out = np.empty(n_boot)
    for r in range(n_boot):
        pick = rng.integers(0, k, k)
        c = cnts[pick].sum()
        out[r] = sums[pick].sum() / c if c > 0 else math.nan
    return out


def _groups(clusters: np.ndarray) -> list[np.ndarray]:
    _, inv = np.unique(clusters, return_inverse=True)
    order = np.argsort(inv, kind="stable")
    cuts = np.flatnonzero(np.diff(inv[order])) + 1
    return np.split(order, cuts)


def mean_ci(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261030) -> dict[str, float]:
    """n, clusters, mean, 95 % CI and two-sided p (H0: mean 0) by a cluster bootstrap, bootstrap SE."""
    v = np.asarray(values, dtype=float)
    c = np.asarray(clusters)
    res: dict[str, float] = {"n": float(v.size), "k": float(np.unique(c).size) if v.size else 0.0}
    if v.size < 2 or res["k"] < 2:
        res.update(m=float(v.mean()) if v.size else math.nan, lo=math.nan, hi=math.nan, p=math.nan, se=math.nan)
        return res
    b = _boot(v, _groups(c), n_boot, seed)
    p = min(1.0, 2 * min((np.sum(b <= 0) + 1) / (b.size + 1), (np.sum(b >= 0) + 1) / (b.size + 1)))
    res.update(m=float(v.mean()), lo=float(np.quantile(b, 0.025)), hi=float(np.quantile(b, 0.975)), p=float(p), se=float(b.std()))
    return res


def diff_ci(
    values: Sequence[float],
    in_a: Sequence[bool],
    in_b: Sequence[bool],
    clusters: Sequence[Any],
    *,
    n_boot: int = 10_000,
    seed: int = 20261030,
) -> dict[str, float]:
    """mean(a) - mean(b) with a 95 % CI and two-sided p, resampling whole clusters (both groups together)."""
    v = np.asarray(values, dtype=float)
    a, bm = np.asarray(in_a, dtype=bool), np.asarray(in_b, dtype=bool)
    keep = a | bm
    v, a, bm, c = v[keep], a[keep], bm[keep], np.asarray(clusters)[keep]
    if a.sum() < 2 or bm.sum() < 2:
        return {"d": math.nan, "lo": math.nan, "hi": math.nan, "p": math.nan}
    groups = _groups(c)
    rng = np.random.default_rng(seed)
    sa = np.array([v[g][a[g]].sum() for g in groups])
    na = np.array([a[g].sum() for g in groups], dtype=float)
    sb = np.array([v[g][bm[g]].sum() for g in groups])
    nb = np.array([bm[g].sum() for g in groups], dtype=float)
    k = len(groups)
    out = np.empty(n_boot)
    for r in range(n_boot):
        p = rng.integers(0, k, k)
        x, y = na[p].sum(), nb[p].sum()
        out[r] = sa[p].sum() / x - sb[p].sum() / y if x > 0 and y > 0 else math.nan
    out = out[np.isfinite(out)]
    d = float(v[a].mean() - v[bm].mean())
    pv = min(1.0, 2 * min((np.sum(out <= 0) + 1) / (out.size + 1), (np.sum(out >= 0) + 1) / (out.size + 1)))
    return {"d": d, "lo": float(np.quantile(out, 0.025)), "hi": float(np.quantile(out, 0.975)), "p": float(pv)}


def r_band(values_in_time_order: Sequence[float], n: int = 30, *, reps: int = 10_000, seed: int = 20261030) -> dict[str, float]:
    """5th / 50th / 95th percentile of the mean R of ``n`` trades: ``iid_*`` from ``reps`` draws with replacement,
    ``roll_*`` over every window of ``n`` consecutive trades (the order given). NaN where there are fewer than n."""
    v = np.asarray(values_in_time_order, dtype=float)
    nan = math.nan
    if v.size < n:
        return {"iid_p5": nan, "iid_p50": nan, "iid_p95": nan, "roll_p5": nan, "roll_p50": nan, "roll_p95": nan, "windows": 0.0}
    rng = np.random.default_rng(seed)
    iid = v[rng.integers(0, v.size, (reps, n))].mean(axis=1)
    cs = np.concatenate([[0.0], np.cumsum(v)])
    roll = (cs[n:] - cs[:-n]) / n
    q = (0.05, 0.5, 0.95)
    a, b = np.quantile(iid, q), np.quantile(roll, q)
    return {
        "iid_p5": float(a[0]),
        "iid_p50": float(a[1]),
        "iid_p95": float(a[2]),
        "roll_p5": float(b[0]),
        "roll_p50": float(b[1]),
        "roll_p95": float(b[2]),
        "windows": float(roll.size),
    }
