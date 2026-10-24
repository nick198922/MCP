"""TradFi perps (Binance) around the reopen of the underlying market: calendar, signals and power (pure functions).

Used by ``scripts/verify_tradfi_offhours_reopen.py`` (IDEA-20261024-tradfi-perp-offhours-reopen). No I/O; every
time is passed in. Prices are as-of: a value "at t" is the close of the last 1m candle that ended at or before t,
so nothing after t is ever read. Returns are decimals (0.01 = 1 %).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
MIN_MS = 60_000

# NYSE full-day closures and 13:00 ET early closes (nyse.com holiday calendar for 2025-2026). Verified against the
# data in the runner (frozen implied index on a holiday); untrusted otherwise.
NYSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2025, 12, 25),
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
)
NYSE_EARLY_CLOSES: frozenset[date] = frozenset({date(2025, 12, 24), date(2026, 11, 27), date(2026, 12, 24)})


@dataclass(frozen=True)
class Session:
    day: date
    open_ms: int  # 09:30 ET in epoch ms
    close_ms: int  # 16:00 ET (13:00 on early-close days)
    early: bool


def et_ms(day: date, hh: int, mm: int) -> int:
    """Epoch ms of a wall-clock time in New York on ``day`` (DST handled by zoneinfo)."""
    return int(datetime.combine(day, time(hh, mm), tzinfo=ET).timestamp() * 1000)


def nyse_sessions(start: date, end: date) -> list[Session]:
    """Regular NYSE sessions with ``start <= day <= end`` (weekends and ``NYSE_HOLIDAYS`` skipped)."""
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in NYSE_HOLIDAYS:
            early = d in NYSE_EARLY_CLOSES
            out.append(Session(d, et_ms(d, 9, 30), et_ms(d, 13, 0) if early else et_ms(d, 16, 0), early))
        d += timedelta(days=1)
    return out


def price_at(ts: np.ndarray, close: np.ndarray, t_ms: int, *, max_stale_ms: int = 5 * MIN_MS) -> float | None:
    """Close of the last 1m candle that ended at or before ``t_ms`` (candle open <= t - 1 min).

    ``ts`` are candle open times (sorted). None if there is no such candle or it ended more than ``max_stale_ms``
    before ``t_ms`` (a gap in the data)."""
    i = int(np.searchsorted(ts, t_ms - MIN_MS, side="right")) - 1
    if i < 0:
        return None
    if t_ms - (int(ts[i]) + MIN_MS) > max_stale_ms:
        return None
    return float(close[i])


def volume_between(ts: np.ndarray, qvol: np.ndarray, t0: int, t1: int) -> float:
    """Quote volume of candles fully inside [t0, t1)."""
    a = int(np.searchsorted(ts, t0, side="left"))
    b = int(np.searchsorted(ts, t1 - MIN_MS, side="right"))
    return float(qvol[a:b].sum()) if b > a else 0.0


@dataclass(frozen=True)
class Night:
    day: date  # the session that reopens
    close_ms: int  # previous session close
    pre_ms: int  # signal / entry time (open - 5 min)
    exit_ms: int  # open + 60 min
    weekend: bool  # previous close was before a non-trading day


def nights(sessions: Sequence[Session], *, pre_min: int = 5, exit_min: int = 60) -> list[Night]:
    """Closed-market windows between consecutive sessions: previous close -> open - ``pre_min`` -> open + ``exit_min``."""
    out = []
    for a, b in zip(sessions, sessions[1:], strict=False):
        gap_days = (b.day - a.day).days
        out.append(Night(b.day, a.close_ms, b.open_ms - pre_min * MIN_MS, b.open_ms + exit_min * MIN_MS, gap_days > 1))
    return out


def night_record(ts: np.ndarray, close: np.ndarray, qvol: np.ndarray, n: Night) -> dict[str, float] | None:
    """Signal D = P(pre)/P(prev close) - 1, target R = P(exit)/P(pre) - 1, closed-hours quote volume.

    D uses nothing after ``pre_ms``; R is the only quantity that reads (pre, exit]."""
    p0 = price_at(ts, close, n.close_ms)
    p1 = price_at(ts, close, n.pre_ms)
    p2 = price_at(ts, close, n.exit_ms)
    if p0 is None or p1 is None or p2 is None or p0 <= 0 or p1 <= 0:
        return None
    return {"D": p1 / p0 - 1.0, "R": p2 / p1 - 1.0, "vol_closed": volume_between(ts, qvol, n.close_ms, n.pre_ms)}


def placebo_record(ts: np.ndarray, close: np.ndarray, s: Session) -> dict[str, float] | None:
    """Same construction inside regular hours: D = P(14:55)/P(11:00) - 1, R = P(16:00)/P(14:55) - 1 (65 min, as the
    main target). None on early-close days."""
    if s.early:
        return None
    p0 = price_at(ts, close, et_ms(s.day, 11, 0))
    p1 = price_at(ts, close, et_ms(s.day, 14, 55))
    p2 = price_at(ts, close, s.close_ms)
    if p0 is None or p1 is None or p2 is None or p0 <= 0 or p1 <= 0:
        return None
    return {"D": p1 / p0 - 1.0, "R": p2 / p1 - 1.0}


def center_by_date(rows: Iterable[tuple[date, str, float]]) -> dict[tuple[date, str], float]:
    """Subtract the cross-sectional mean of each date (dates with a single symbol drop out)."""
    by: dict[date, list[tuple[str, float]]] = {}
    for d, s, x in rows:
        by.setdefault(d, []).append((s, x))
    out = {}
    for d, xs in by.items():
        if len(xs) < 2:
            continue
        m = sum(x for _, x in xs) / len(xs)
        for s, x in xs:
            out[(d, s)] = x - m
    return out


# ---------------------------------------------------------------- power (step 0: no outcome means)


def mde(sd: float, n: int, *, z: float = 2.8) -> float | None:
    """Minimum detectable effect at 5 % two-sided, 80 % power: ``z * sd / sqrt(n)``."""
    return z * sd / math.sqrt(n) if n > 0 and sd >= 0 else None


def participation_ratio(eigenvalues: Sequence[float]) -> float:
    """Effective number of independent factors: (sum l)^2 / sum l^2 (1 = one factor, k = k equal factors)."""
    lam = np.clip(np.asarray(eigenvalues, dtype=float), 0.0, None)
    s2 = float((lam**2).sum())
    return float(lam.sum() ** 2 / s2) if s2 > 0 else 0.0


def pairwise_corr(mat: np.ndarray, *, min_periods: int = 30) -> np.ndarray:
    """Correlation matrix of the columns of ``mat`` (rows = dates) on pairwise-complete rows; NaN-padded matrix.

    Pairs with fewer than ``min_periods`` common rows get 0 off the diagonal (treated as unrelated)."""
    k = mat.shape[1]
    ok = ~np.isnan(mat)
    out = np.eye(k)
    for i in range(k):
        for j in range(i + 1, k):
            m = ok[:, i] & ok[:, j]
            if m.sum() < min_periods:
                continue
            x, y = mat[m, i], mat[m, j]
            sx, sy = x.std(), y.std()
            if sx > 0 and sy > 0:
                out[i, j] = out[j, i] = float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy))
    return out


def random_basket_sd(
    by_date: Mapping[date, Sequence[float]], *, frac: float = 0.2, reps: int = 200, seed: int = 20261024
) -> tuple[float, int]:
    """SD across nights of a long-short basket with RANDOM side assignment (no signal used).

    Each night: k = max(1, round(frac * m)) names long, k short, chosen at random; the night value is the mean of
    +R over the long names and -R over the short names (per-trade return). Returns (SD averaged over ``reps``
    random draws, number of nights with m >= 2k). This is the noise level the real basket will have, without
    looking at the signal or the mean."""
    rng = np.random.default_rng(seed)
    nights_ = [np.asarray(v, dtype=float) for v in by_date.values()]
    nights_ = [v for v in nights_ if len(v) >= 2 * max(1, round(frac * len(v)))]
    if len(nights_) < 3:
        return float("nan"), len(nights_)
    sds = []
    for _ in range(reps):
        vals = []
        for v in nights_:
            k = max(1, round(frac * len(v)))
            idx = rng.permutation(len(v))
            vals.append((v[idx[:k]].sum() - v[idx[k : 2 * k]].sum()) / (2 * k))
        sds.append(float(np.std(vals, ddof=1)))
    return float(np.mean(sds)), len(nights_)


WINDOWS = ("regular", "premarket", "afterhours", "overnight", "weekend", "holiday")


def window_label(t_ms: int) -> str:
    """US-equity window of one timestamp (ET via zoneinfo, so DST-safe).

    regular 09:30-close (close 16:00, 13:00 on early-close days) / premarket 04:00-09:30 / afterhours close-20:00 /
    overnight 20:00-04:00 on nights that start Sunday-Thursday / weekend Friday 20:00 - Sunday 20:00 / holiday all day
    on an NYSE-closed weekday. Conventions: the evening before a holiday (after 20:00) and Sunday after 20:00 before
    a Monday holiday are 'overnight' (the 24/5 overnight venues trade then)."""
    dt = datetime.fromtimestamp(t_ms / 1000, tz=ET)
    d, h = dt.date(), dt.hour + dt.minute / 60
    wd = d.weekday()
    if wd >= 5:
        return "overnight" if (wd == 6 and h >= 20) else "weekend"
    if d in NYSE_HOLIDAYS:
        return "holiday"
    close_h = 13.0 if d in NYSE_EARLY_CLOSES else 16.0
    if 9.5 <= h < close_h:
        return "regular"
    if 4 <= h < 9.5:
        return "premarket"
    if close_h <= h < 20:
        return "afterhours"
    return "weekend" if (wd == 4 and h >= 20) else "overnight"


def window_labels(t_ms: np.ndarray) -> np.ndarray:
    """Vectorised :func:`window_label` (object array of window names)."""
    out = np.empty(len(t_ms), dtype=object)
    for i, t in enumerate(t_ms):
        out[i] = window_label(int(t))
    return out


def premium_slope(
    k_ts: np.ndarray,
    k_close: np.ndarray,
    p_ts: np.ndarray,
    p_val: np.ndarray,
    *,
    step_ms: int = 5 * MIN_MS,
    min_n: int = 30,
) -> dict[str, tuple[float, int] | None]:
    """Through-origin slope of step changes of the premium index on step log changes of the perp price, by window.

    Premium = (impact price - index) / index (decimal). If the index is frozen while the perp moves, the premium
    moves ~1:1 with ln(price) (slope ~1); if the index is live it absorbs the move (slope ~0). Heuristic: noise of the
    last trade in the regressor biases the slope towards 0 (errors-in-variables).

    Inputs: 1m kline open times / closes (sorted) and premiumIndexKlines open times / closes (sorted), epoch ms.
    Uses the grid of premium times that are multiples of ``step_ms`` and also exist in the klines; only contiguous
    steps (exactly ``step_ms`` apart) with a non-zero price change count; the step is labelled by its end time.
    Returns {window: (slope rounded to 0.01, n_steps) or None if fewer than ``min_n`` steps}."""
    k_ts = np.asarray(k_ts, dtype=float)
    p_ts = np.asarray(p_ts, dtype=float)
    out: dict[str, tuple[float, int] | None] = dict.fromkeys(WINDOWS)
    if len(k_ts) == 0 or len(p_ts) == 0:
        return out
    grid = p_ts[(p_ts % step_ms) == 0]
    idx_k = np.searchsorted(k_ts, grid)
    ok = (idx_k < len(k_ts)) & (k_ts[np.minimum(idx_k, len(k_ts) - 1)] == grid)
    grid, ik = grid[ok], idx_k[ok]
    if len(grid) < 2:
        return out
    ip = np.searchsorted(p_ts, grid)
    dl = np.diff(np.log(np.asarray(k_close, dtype=float)[ik]))
    dp = np.diff(np.asarray(p_val, dtype=float)[ip])
    contiguous = np.diff(grid) == step_ms
    lab = window_labels(grid[1:])
    for w in WINDOWS:
        m = contiguous & (lab == w) & (dl != 0)
        if m.sum() < min_n:
            continue
        x, y = dl[m], dp[m]
        out[w] = (round(float((x * y).sum() / (x * x).sum()), 2), int(m.sum()))
    return out
