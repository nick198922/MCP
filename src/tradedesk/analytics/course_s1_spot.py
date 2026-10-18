"""Course setups, step 9 (IDEA-20261016-course-setups-crypto): S1 — spot alts near the "weekly support" (daily
SMA 100 / SMA 200), hypotheses H1-H3 of LIB-20261015-spot-entries.

Pure functions, no I/O; time enters only through the bar arrays. Conventions:
  * one symbol = arrays over its *available* daily bars (UTC days keyed by open time, epoch ms, sorted); missing days
    are simply absent. ``seg`` labels segments: a gap of more than ``max_gap_days`` between neighbouring bars starts a
    new segment (a new token on the same ticker, a redenomination, a delisting); indicators restart in each segment;
  * every signal on bar ``t`` (decision at the close of ``t``) uses bars ``<= t`` only; outcomes use bars after ``t``;
  * returns are simple decimals on notional (0.05 = 5 %); spot without leverage, so on notional = on capital;
  * costs per side as a decimal (0.0007 = taker 0.05 % + slippage 0.02 %): net = gross - cost * (1 + exit / entry).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

DAY_MS = 86_400_000
SIDE_COST = 0.0007  # taker 0.05 % + slippage 0.02 % per side (0.14 % round trip)

# ----------------------------------------------------------------------------- segments and indicators


def segments(open_ms: np.ndarray, *, max_gap_days: int = 2) -> np.ndarray:
    """Segment id per bar: a new segment starts when the open-to-open gap exceeds ``max_gap_days`` days."""
    t = np.asarray(open_ms, dtype=np.int64)
    if t.size == 0:
        return np.zeros(0, dtype=np.int64)
    return np.concatenate([[0], np.cumsum(np.diff(t) > max_gap_days * DAY_MS)]).astype(np.int64)


def seg_start(seg: np.ndarray) -> np.ndarray:
    """Index of the first bar of each bar's segment."""
    s = np.asarray(seg)
    if s.size == 0:
        return np.zeros(0, dtype=np.int64)
    first = np.concatenate([[True], s[1:] != s[:-1]])
    return np.maximum.accumulate(np.where(first, np.arange(s.size), 0)).astype(np.int64)


def seg_end(seg: np.ndarray) -> np.ndarray:
    """Index of the last bar of each bar's segment."""
    s = np.asarray(seg)
    if s.size == 0:
        return np.zeros(0, dtype=np.int64)
    last = np.concatenate([s[1:] != s[:-1], [True]])
    idx = np.where(last, np.arange(s.size), s.size)
    return np.minimum.accumulate(idx[::-1])[::-1].astype(np.int64)


def seg_sma(x: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    """Simple mean of the last ``n`` bars of the same segment (bar ``t`` included); NaN before ``n`` bars."""
    v = np.asarray(x, dtype=float)
    out = np.full(v.size, np.nan)
    if v.size == 0:
        return out
    cs = np.concatenate([[0.0], np.cumsum(v)])
    idx = np.arange(v.size)
    ok = idx - seg_start(seg) >= n - 1
    i = idx[ok]
    out[ok] = (cs[i + 1] - cs[i + 1 - n]) / n
    return out


def rolling_extreme(x: np.ndarray, seg: np.ndarray, n: int, *, how: str = "max") -> np.ndarray:
    """Max (or min) of the last ``n`` bars of the same segment, bar ``t`` included; NaN before ``n`` bars."""
    v = np.asarray(x, dtype=float)
    out = np.full(v.size, np.nan)
    if v.size < n:
        return out
    win = np.lib.stride_tricks.sliding_window_view(v, n)
    ext = win.max(axis=1) if how == "max" else win.min(axis=1)
    idx = np.arange(n - 1, v.size)
    ok = idx - seg_start(seg)[idx] >= n - 1
    out[idx[ok]] = ext[ok]
    return out


# ----------------------------------------------------------------------------- H1: first test of the support after strength


def first_touch_events(
    close: np.ndarray,
    low: np.ndarray,
    touch_ma: np.ndarray,
    sma100: np.ndarray,
    sma200: np.ndarray,
    seg: np.ndarray,
    *,
    ratio: float | None = 1.6,
    lookback: int = 120,
    trend: str | None = "up",
    quiet: int = 30,
) -> np.ndarray:
    """Bars ``t`` where a strong asset tests its moving-average support for the first time.

    * strength (``ratio`` set): some bar ``j`` in ``t - lookback … t - 1`` of the segment closed at
      ``close[j] / sma100[j] >= ratio``; ``k`` is the last such bar;
    * first test: no bar ``k + 1 … t - 1`` had ``low <= touch_ma``, and ``low[t] <= touch_ma[t]``;
    * without the strength filter (``ratio=None``): the first touch after ``quiet`` bars without one (``touch_ma``
      defined on bar ``t - quiet``);
    * ``trend="up"``: ``sma100[t] > sma200[t]`` and ``close[t-1] > sma200[t-1]``; ``"down"``: ``sma100[t] <= sma200[t]``;
      ``None``: no condition. ``sma200[t]`` must be defined in every case.
    """
    c = np.asarray(close, dtype=float)
    lo = np.asarray(low, dtype=float)
    ma = np.asarray(touch_ma, dtype=float)
    s1 = np.asarray(sma100, dtype=float)
    s2 = np.asarray(sma200, dtype=float)
    sg = np.asarray(seg)
    out: list[int] = []
    last_strong = last_touch = -(10**9)
    for t in range(c.size):
        if t == 0 or sg[t] != sg[t - 1]:
            last_strong = last_touch = -(10**9)
        touch = bool(np.isfinite(ma[t]) and lo[t] <= ma[t])
        if touch and np.isfinite(s1[t]) and np.isfinite(s2[t]):
            if ratio is not None:
                armed = last_strong >= t - lookback and last_touch <= last_strong
            else:
                armed = t - quiet >= 0 and sg[t - quiet] == sg[t] and np.isfinite(ma[t - quiet]) and last_touch < t - quiet
            if trend == "up":
                ok_trend = s1[t] > s2[t] and t >= 1 and sg[t - 1] == sg[t] and np.isfinite(s2[t - 1]) and c[t - 1] > s2[t - 1]
            elif trend == "down":
                ok_trend = s1[t] <= s2[t]
            else:
                ok_trend = True
            if armed and ok_trend:
                out.append(t)
        if touch:
            last_touch = t
        if ratio is not None and np.isfinite(s1[t]) and s1[t] > 0 and c[t] / s1[t] >= ratio:
            last_strong = t
    return np.asarray(out, dtype=np.int64)


def uptrend_state(close: np.ndarray, sma100: np.ndarray, sma200: np.ndarray, seg: np.ndarray, *, trend: str = "up") -> np.ndarray:
    """Bars in the same moving-average state as the events (placebo pool): ``"up"`` — ``sma100 > sma200`` and the
    previous close of the segment above ``sma200``; ``"down"`` — ``sma100 <= sma200``. ``sma200`` must be defined."""
    c = np.asarray(close, dtype=float)
    s1 = np.asarray(sma100, dtype=float)
    s2 = np.asarray(sma200, dtype=float)
    sg = np.asarray(seg)
    ok = np.isfinite(s1) & np.isfinite(s2)
    if trend == "down":
        return ok & (s1 <= s2)
    prev_ok = np.zeros(c.size, dtype=bool)
    if c.size > 1:
        with np.errstate(invalid="ignore"):
            prev_ok[1:] = (sg[1:] == sg[:-1]) & np.isfinite(s2[:-1]) & (c[:-1] > s2[:-1])
    with np.errstate(invalid="ignore"):
        return ok & (s1 > s2) & prev_ok


def first_close_above(close: np.ndarray, ma: np.ndarray, seg: np.ndarray, t: int, *, within: int = 10) -> int:
    """First bar ``u`` in ``t + 1 … t + within`` of the same segment with ``close[u] > ma[u]``; -1 if none."""
    for u in range(t + 1, min(len(close), t + within + 1)):
        if seg[u] != seg[t]:
            break
        if np.isfinite(ma[u]) and close[u] > ma[u]:
            return u
    return -1


def closes_below(close: np.ndarray, ma: np.ndarray, seg: np.ndarray, t: int, *, within: int = 10) -> bool | None:
    """Whether any close in ``t + 1 … t + within`` (same segment) is below ``ma``; None if no such bar exists."""
    seen = False
    for u in range(t + 1, min(len(close), t + within + 1)):
        if seg[u] != seg[t]:
            break
        seen = True
        if np.isfinite(ma[u]) and close[u] < ma[u]:
            return True
    return False if seen else None


# ----------------------------------------------------------------------------- holding and returns


def forward_exit(open_ms: np.ndarray, seg: np.ndarray, h_days: int, *, max_gap_days: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """Exit bar for an entry at the close of each bar, held ``h_days`` calendar days.

    The exit is the last bar of the same segment with ``open <= open[t] + h_days``; ``data_end`` marks exits whose bar
    opened more than ``max_gap_days`` before that target (the segment ended: delisting, ticker reuse, end of data).
    ``-1`` when no later bar exists in the segment."""
    t = np.asarray(open_ms, dtype=np.int64)
    n = t.size
    if n == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool)
    target = t + h_days * DAY_MS
    j = np.searchsorted(t, target, side="right") - 1
    j = np.minimum(j, seg_end(seg))
    idx = np.arange(n)
    j = np.where(j > idx, j, -1)
    data_end = np.zeros(n, dtype=bool)
    ok = j >= 0
    data_end[ok] = t[j[ok]] < target[ok] - max_gap_days * DAY_MS
    return j.astype(np.int64), data_end


def net_return(entry: np.ndarray | float, exit_: np.ndarray | float, *, cost: float = SIDE_COST) -> Any:
    """``exit / entry - 1 - cost * (1 + exit / entry)`` (simple, on notional)."""
    ratio = np.asarray(exit_, dtype=float) / np.asarray(entry, dtype=float)
    out = ratio - 1.0 - cost * (1.0 + ratio)
    return float(out) if np.ndim(out) == 0 else out


def one_at_a_time(entry_ms: Sequence[int], exit_ms: Sequence[int]) -> np.ndarray:
    """Mask of trades kept when a new trade on the same symbol is skipped while the previous kept one is open
    (inputs sorted by entry)."""
    keep = np.zeros(len(entry_ms), dtype=bool)
    busy = -(10**18)
    for i, (a, b) in enumerate(zip(entry_ms, exit_ms, strict=True)):
        if a >= busy:
            keep[i] = True
            busy = b
    return keep


# ----------------------------------------------------------------------------- H2: reclaim of a lost support level


def is_swing_low(low: np.ndarray, seg: np.ndarray, f: int, k: int = 3) -> bool:
    """Strict fractal low: ``low[f]`` below the ``k`` lows on each side, all ``2k + 1`` bars in one segment."""
    if f - k < 0 or f + k >= len(low):
        return False
    if seg[f - k] != seg[f] or seg[f + k] != seg[f]:
        return False
    v = low[f]
    return bool(np.all(low[f - k : f] > v) and np.all(low[f + 1 : f + k + 1] > v))


def reclaim_events(
    close: np.ndarray,
    low: np.ndarray,
    seg: np.ndarray,
    *,
    k: int = 3,
    tol: float = 0.03,
    touches: int = 3,
    lookback: int = 365,
    below: int = 5,
    max_wait: int = 180,
    life: int = 365,
) -> list[tuple[int, float, int]]:
    """Reclaims of a lost support level, causal: ``[(bar, level, swing bar), ...]`` sorted by bar, one per bar.

    * swing low ``f`` (strict ``k``-bar fractal) becomes known at the close of ``f + k``;
    * it makes a support level ``L = low[f]`` if at least ``touches - 1`` earlier swing lows ``g`` of the segment
      (``f - lookback <= g < f``) lie within ``tol`` of it (``|low[g] / L - 1| <= tol``);
    * the level counts closes from bar ``f + k + 1``; it is lost after ``below`` consecutive closes ``< L`` (the loss
      must start within ``life`` bars of the level becoming known);
    * the reclaim is the first close ``>= L`` after the loss, within ``max_wait`` bars of the start of the losing run;
      each level gives at most one event and then retires. Several levels reclaimed on one bar give one event (the
      highest level is reported).
    """
    c = np.asarray(close, dtype=float)
    lo = np.asarray(low, dtype=float)
    sg = np.asarray(seg)
    swings: list[int] = []
    levels: list[dict[str, Any]] = []
    out: dict[int, tuple[int, float, int]] = {}
    for t in range(c.size):
        if t > 0 and sg[t] != sg[t - 1]:
            swings, levels = [], []
        # 1) update the levels known before bar t with the close of t
        keep: list[dict[str, Any]] = []
        for lv in levels:
            L = lv["L"]
            if c[t] < L:
                lv["run"] += 1
                if lv["lost"] is None and lv["run"] >= below:
                    if t - lv["run"] + 1 - lv["known"] > life:
                        continue  # the loss started after the level's life: retire
                    lv["lost"] = t - lv["run"] + 1
            else:
                if lv["lost"] is not None:
                    prev = out.get(t)
                    if prev is None or L > prev[1]:
                        out[t] = (t, L, lv["f"])
                    continue  # reclaimed: retire
                lv["run"] = 0
            if lv["lost"] is not None and t - lv["lost"] > max_wait:
                continue
            if lv["lost"] is None and t - lv["known"] > life + below:
                continue
            keep.append(lv)
        levels = keep
        # 2) confirm the swing low f = t - k at the close of t (it starts counting closes from t + 1)
        f = t - k
        if is_swing_low(lo, sg, f, k):
            v = lo[f]
            near = sum(1 for g in swings if f - lookback <= g < f and abs(lo[g] / v - 1.0) <= tol)
            if near >= touches - 1:
                levels.append({"L": float(v), "f": f, "known": t, "run": 0, "lost": None})
            swings.append(f)
            swings = [g for g in swings if g >= t - lookback - k]
    return [out[t] for t in sorted(out)]


# ----------------------------------------------------------------------------- H3: alts while BTC is sideways after a drop


def sideways_after_drop(
    close: np.ndarray,
    seg: np.ndarray,
    *,
    drop: float = 0.15,
    max_window: int = 180,
    range_window: int = 30,
    width: float = 0.15,
) -> np.ndarray:
    """State per bar: close at least ``drop`` below the max close of the last ``max_window`` bars and the last
    ``range_window`` closes within a band ``max / min - 1 <= width`` (about +-7 % around the middle for 0.15)."""
    c = np.asarray(close, dtype=float)
    mx = rolling_extreme(c, seg, max_window, how="max")
    rmx = rolling_extreme(c, seg, range_window, how="max")
    rmn = rolling_extreme(c, seg, range_window, how="min")
    with np.errstate(invalid="ignore", divide="ignore"):
        st = (c / mx - 1.0 <= -drop) & (rmx / rmn - 1.0 <= width)
    return st & np.isfinite(mx) & np.isfinite(rmn)


def state_onsets(state: np.ndarray, *, quiet: int = 30) -> np.ndarray:
    """Bars where ``state`` is true and was false on each of the previous ``quiet`` bars (bars before the start of the
    array count as unknown, so the first ``quiet`` bars never start an episode)."""
    s = np.asarray(state, dtype=bool)
    out = [t for t in range(quiet, s.size) if s[t] and not s[t - quiet : t].any()]
    return np.asarray(out, dtype=np.int64)


def xs_median_rel(alt_ratio: Sequence[float], btc_ratio: float, *, min_n: int = 5) -> float:
    """Cross-sectional median of ``alt_ratio / btc_ratio - 1`` (exit/entry price ratios over the same window);
    NaN with fewer than ``min_n`` finite alts."""
    a = np.asarray(alt_ratio, dtype=float)
    a = a[np.isfinite(a)]
    if a.size < min_n or not math.isfinite(btc_ratio) or btc_ratio <= 0:
        return float("nan")
    return float(np.median(a / btc_ratio - 1.0))


# ----------------------------------------------------------------------------- placebo sampling and per-event contrasts


def pick_matched(
    rng: np.random.Generator,
    cand_day: np.ndarray,
    cand_ok: np.ndarray,
    event_day: int,
    *,
    window: int = 15,
    k: int = 3,
    exclude: int | None = None,
) -> np.ndarray:
    """Up to ``k`` random candidate indices (without replacement) with ``|cand_day - event_day| <= window`` and
    ``cand_ok``; ``cand_day`` sorted ascending (integer day numbers); ``exclude`` drops one candidate index."""
    lo = int(np.searchsorted(cand_day, event_day - window, side="left"))
    hi = int(np.searchsorted(cand_day, event_day + window, side="right"))
    idx = lo + np.flatnonzero(cand_ok[lo:hi])
    if exclude is not None:
        idx = idx[idx != exclude]
    if idx.size == 0:
        return idx.astype(np.int64)
    return np.sort(rng.choice(idx, size=min(k, idx.size), replace=False)).astype(np.int64)


def event_minus_placebo(ev: Sequence[float], pl: Sequence[float], pl_event: Sequence[int]) -> np.ndarray:
    """Per event: its value minus the mean of its placebo rows (``pl_event`` = event index of each placebo row);
    NaN for events without placebo rows."""
    e = np.asarray(ev, dtype=float)
    p = np.asarray(pl, dtype=float)
    pe = np.asarray(pl_event, dtype=np.int64)
    s = np.bincount(pe, weights=p, minlength=e.size) if pe.size else np.zeros(e.size)
    n = np.bincount(pe, minlength=e.size) if pe.size else np.zeros(e.size)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, e - s / np.where(n > 0, n, 1), np.nan)


# ----------------------------------------------------------------------------- verdict (registration of step 9)


def h1_verdict(
    *,
    n_dates: int,
    mde: float,
    diff: float,
    diff_ci_day: tuple[float, float] | None,
    diff_ci_month: tuple[float, float] | None,
    mean_ci_day: tuple[float, float] | None,
    mean_ci_month: tuple[float, float] | None,
    p_holm: float | None,
    oos_dates: int,
    oos_mean: float,
    oos_diff: float,
    min_dates: int = 100,
    gate: float = 0.10,
    min_effect: float = 0.05,
    min_oos_dates: int = 20,
) -> str:
    """Verdict of the primary H1 metric by the step-9 registration: ``inconclusive`` without dates or power,
    ``rejected`` when the difference to the placebo is below the minimum effect or its date-clustered CI includes 0,
    ``confirmed`` only with every IS, Holm and OOS condition, otherwise ``inconclusive``."""
    if n_dates < min_dates or not math.isfinite(mde) or mde > gate:
        return "inconclusive"
    if not math.isfinite(diff) or diff < min_effect or diff_ci_day is None or diff_ci_day[0] <= 0:
        return "rejected"
    ok = (
        diff_ci_month is not None
        and diff_ci_month[0] > 0
        and mean_ci_day is not None
        and mean_ci_day[0] > 0
        and mean_ci_month is not None
        and mean_ci_month[0] > 0
        and p_holm is not None
        and p_holm < 0.05
        and oos_dates >= min_oos_dates
        and oos_mean > 0
        and oos_diff > 0
    )
    return "confirmed" if ok else "inconclusive"
