"""NY Midnight Open (course setups, step 11 of IDEA-20261016-course-setups-crypto; H2 of LIB-20261016-ict-concepts).

Pure functions, no I/O. Bars are 5m bars (``session_stop_hunt.Bars``: open time ms UTC, o, h, low, c); missing bars
are simply absent. All session times are wall-clock times in America/New_York (DST through ``zoneinfo``).

(b) primary: the side at 09:30 NY is taken against a reference price (below the reference -> long, above -> short:
the ICT "discount / premium" reading) and the move to 16:00 NY is measured in daily ATR. The midnight open (00:00 NY)
is compared with the same rule on the 00:00 UTC open. (a) secondary: "Judas swing" frequency for an anchor A (window
[A, A + 9.5 h), day close at A + 16 h) against other anchors.

Causality: a day's record reads bars with open time up to its last time (16:00 NY, or A + 16 h for an anchor; at that
time only the open of the bar is used, i.e. the market price at that moment) and the ATR of complete NY days before
the day.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means
from tradedesk.analytics.session_stop_hunt import Bars

NEW_YORK = ZoneInfo("America/New_York")
MIN_MS = 60_000
BAR_MS = 5 * MIN_MS
HOUR_MS = 60 * MIN_MS
DAY_MS = 24 * HOUR_MS
BACK_MS = 30 * MIN_MS  # a missing bar at T falls back to the last close at most 30 min earlier
WINDOW_MS = 9 * HOUR_MS + 30 * MIN_MS  # Judas window: anchor -> anchor + 9.5 h (00:00 -> 09:30 NY)
CLOSE_MS = 16 * HOUR_MS  # day close: anchor + 16 h (16:00 NY)

# Named times of a NY day d: (label, day offset, hour, minute) in New York wall-clock time.
REF_HOURS_PREV = (19, 20, 21, 22, 23)
REF_HOURS_DAY = (0, 1, 2, 3, 4, 5, 6, 7, 8)
TIMES: tuple[tuple[str, int, int, int], ...] = (
    *((f"p{h:02d}", -1, h, 0) for h in REF_HOURS_PREV),
    *((f"d{h:02d}", 0, h, 0) for h in REF_HOURS_DAY),
    ("d0830", 0, 8, 30),
    ("d0930", 0, 9, 30),
    ("d1200", 0, 12, 0),
    ("d1600", 0, 16, 0),
)


def ny_ms(day: date, hour: int, minute: int = 0) -> int:
    """ms UTC of a New York wall-clock time on ``day`` (EST/EDT resolved by zoneinfo)."""
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK).timestamp() * 1000)


def utc_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)


def ny_date(t_ms: int) -> date:
    return datetime.fromtimestamp(t_ms / 1000, tz=NEW_YORK).date()


def price_at(b: Bars, t_ms: int, back_ms: int = BACK_MS) -> float:
    """Price at time T: the open of the bar opening at T; else the close of the last bar opening in [T - back, T);
    else NaN."""
    i = int(np.searchsorted(b.t, t_ms))
    if i < b.t.size and int(b.t[i]) == t_ms:
        return float(b.o[i])
    if i > 0 and int(b.t[i - 1]) >= t_ms - back_ms:
        return float(b.c[i - 1])
    return float("nan")


def _span(b: Bars, t0: int, t1: int) -> tuple[int, int]:
    return int(np.searchsorted(b.t, t0)), int(np.searchsorted(b.t, t1))


def coverage(b: Bars, t0: int, t1: int, bar_ms: int = BAR_MS) -> float:
    """Share of bar slots with open in [t0, t1) that have a bar."""
    slots = (t1 - t0) // bar_ms
    if slots <= 0:
        return 0.0
    i, j = _span(b, t0, t1)
    return (j - i) / slots


def extremes(b: Bars, t0: int, t1: int) -> tuple[float, float]:
    """(max high, min low) of bars opening in [t0, t1); NaN if none."""
    i, j = _span(b, t0, t1)
    if j <= i:
        return float("nan"), float("nan")
    return float(np.max(b.h[i:j])), float(np.min(b.low[i:j]))


def ny_days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=k) for k in range((last - first).days + 1)]


def daily_ny_bars(b: Bars, days: Sequence[date]) -> list[tuple[date, float, float, float, float]]:
    """(day, high, low, close, coverage) of each NY day 00:00 -> 24:00 New York time."""
    out = []
    for d in days:
        t0, t1 = ny_ms(d, 0), ny_ms(d + timedelta(days=1), 0)
        i, j = _span(b, t0, t1)
        cov = coverage(b, t0, t1)
        if j > i:
            out.append((d, float(np.max(b.h[i:j])), float(np.min(b.low[i:j])), float(b.c[j - 1]), cov))
        else:
            out.append((d, float("nan"), float("nan"), float("nan"), 0.0))
    return out


def causal_atr(daily: Sequence[tuple[date, float, float, float, float]], n: int = 14, min_cov: float = 0.5) -> dict[date, float]:
    """ATR of each day = simple mean true range of the ``n`` last valid NY days strictly before it (valid: coverage
    >= ``min_cov``; true range from the close of the previous valid day). NaN with fewer than ``n`` valid days."""
    out: dict[date, float] = {}
    trs: list[float] = []
    prev_c = float("nan")
    for d, h, lo, c, cov in sorted(daily, key=lambda x: x[0]):
        out[d] = float(np.mean(trs[-n:])) if len(trs) >= n else float("nan")
        if cov >= min_cov and math.isfinite(h):
            tr = h - lo if not math.isfinite(prev_c) else max(h - lo, abs(h - prev_c), abs(lo - prev_c))
            trs.append(tr)
            prev_c = c
    return out


def day_prices(b: Bars, day: date, min_cov: float = 0.8) -> tuple[dict[str, float] | None, str]:
    """Prices of one NY day at the named ``TIMES`` plus ``uo`` (00:00 UTC of the same calendar date). Rejected with a
    reason when coverage of [00:00 UTC d, 16:00 NY d) is below ``min_cov`` or a core price is missing."""
    t_uo = utc_midnight_ms(day)
    t_end = ny_ms(day, 16, 0)
    if coverage(b, min(t_uo, ny_ms(day, 0)), t_end) < min_cov:
        return None, "coverage"
    px: dict[str, float] = {"uo": price_at(b, t_uo)}
    for lab, off, hh, mm in TIMES:
        px[lab] = price_at(b, ny_ms(day + timedelta(days=off), hh, mm))
    for k in ("uo", "d00", "d0930", "d1600"):
        if not math.isfinite(px[k]):
            return None, f"missing_{k}"
    return px, "ok"


def thesis_move(ref: float, p_dec: float, p_exit: float, atr: float) -> tuple[int, float, float]:
    """Side s = -sign(p_dec - ref) (below the reference -> long), y = s (p_exit - p_dec) / atr, r = s (p_exit / p_dec
    - 1) gross on notional. s = 0 (and y = r = 0) when p_dec == ref; NaN for missing inputs."""
    vals = (ref, p_dec, p_exit, atr)
    if not all(math.isfinite(v) for v in vals) or atr <= 0 or p_dec == 0:
        return 0, float("nan"), float("nan")
    s = -1 if p_dec > ref else (1 if p_dec < ref else 0)
    return s, s * (p_exit - p_dec) / atr, s * (p_exit / p_dec - 1.0)


def judas(b: Bars, anchor_ms: int, atr: float, ks: Sequence[float] = (0.0,), min_cov: float = 0.8) -> tuple[dict[float, int] | None, str]:
    """Judas-swing flags for one anchor A: D = sign(P(A + 16 h) - P(A)); J_k = 1 if D > 0 and the window [A, A + 9.5 h)
    traded below P(A) - k ATR, or D < 0 and above P(A) + k ATR. None (with reason) on low coverage, a missing price
    or D = 0."""
    if not math.isfinite(atr) or atr <= 0:
        return None, "atr"
    if coverage(b, anchor_ms, anchor_ms + CLOSE_MS) < min_cov:
        return None, "coverage"
    p0, p1 = price_at(b, anchor_ms), price_at(b, anchor_ms + CLOSE_MS)
    if not (math.isfinite(p0) and math.isfinite(p1)):
        return None, "missing"
    if p1 == p0:
        return None, "flat_day"
    hi, lo = extremes(b, anchor_ms, anchor_ms + WINDOW_MS)
    up = p1 > p0
    return {k: int(lo < p0 - k * atr) if up else int(hi > p0 + k * atr) for k in ks}, "ok"


def paired_summary(values: Sequence[float], clusters: Sequence[Any], *, n_boot: int = 10_000, seed: int = 20261017) -> dict[str, Any]:
    """Mean of paired differences with a cluster bootstrap: 95 % percentile CI, one-sided p for mean > 0
    ((1 + #{m* <= 0}) / (1 + B)), se = SD of the bootstrap. NaN values are dropped."""
    v = [float(x) for x in values]
    keep = [i for i, x in enumerate(v) if math.isfinite(x)]
    v = [v[i] for i in keep]
    c = [clusters[i] for i in keep]
    if not v:
        return {"n": 0, "clusters": 0, "mean": float("nan"), "ci": None, "p": None, "p2": None, "se": float("nan")}
    m = float(np.mean(v))
    if len(set(c)) < 2:
        return {"n": len(v), "clusters": len(set(c)), "mean": m, "ci": None, "p": None, "p2": None, "se": float("nan")}
    bm = cluster_bootstrap_means(v, c, n_boot=n_boot, seed=seed)
    p = float((np.sum(bm <= 0) + 1) / (bm.size + 1))
    p_lo = float((np.sum(bm >= 0) + 1) / (bm.size + 1))
    return {
        "n": len(v),
        "clusters": len(set(c)),
        "mean": m,
        "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))),
        "p": p,
        "p2": min(1.0, 2 * min(p, p_lo)),
        "se": float(np.std(bm)),
    }


def nymo_verdict(
    *,
    n_days: int,
    mde: float,
    diff: float,
    ci_day: tuple[float, float] | None,
    ci_week: tuple[float, float] | None,
    p_holm: float | None,
    oos_diff: float,
    oos_p: float | None,
    trade_ci_is: tuple[float, float] | None,
    trade_oos_mean: float,
    min_days: int = 500,
    gate: float = 0.05,
    min_effect: float = 0.05,
) -> str:
    """Registered verdict of one primary asset (card section "Попередня реєстрація — крок 11")."""
    if n_days < min_days or not math.isfinite(mde) or mde > gate:
        return "inconclusive"
    if not math.isfinite(diff) or diff < min_effect or ci_day is None or ci_day[0] <= 0:
        return "rejected"
    ok = (
        ci_week is not None
        and ci_week[0] > 0
        and p_holm is not None
        and p_holm < 0.05
        and math.isfinite(oos_diff)
        and oos_diff > 0
        and oos_p is not None
        and oos_p < 0.05
        and trade_ci_is is not None
        and trade_ci_is[0] > 0
        and math.isfinite(trade_oos_mean)
        and trade_oos_mean > 0
    )
    return "confirmed" if ok else "inconclusive"
