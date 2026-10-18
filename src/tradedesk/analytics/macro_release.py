"""Crypto reaction to US macro releases (CPI, NFP, FOMC) in minutes to hours (IDEA-20261017-macro, part B2).

Pure functions, no I/O. Conventions:
  * prices are closes of fixed bars keyed by bar open time (epoch ms); the price at a boundary ``t`` is the close of the
    bar that opened at ``t - bar`` (it ended at ``t``), so the price at the release time is the last pre-release price;
  * release times: CPI and NFP 08:30 America/New_York, FOMC statement 14:00 (DST-aware);
  * log returns and volatilities are decimals (0.01 = 1 %); "normal" volatility at ``t0`` uses only prices up to ``t0``.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from .macro_regimes import ny_time_ms

MIN_MS = 60_000
BAR_MS = 5 * MIN_MS
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
RELEASE_CLOCK = {"cpi": (8, 30), "nfp": (8, 30), "fomc": (14, 0)}
HORIZONS = {"5m": 5 * MIN_MS, "30m": 30 * MIN_MS, "1h": HOUR_MS, "2h": 2 * HOUR_MS, "4h": 4 * HOUR_MS, "24h": DAY_MS}

# ----------------------------------------------------------------------------- events


@dataclass(frozen=True)
class Event:
    day: date
    kind: str
    status: str
    verification: str
    original: date | None = None  # originally scheduled date of a delayed release (from the CSV note)


_SCHEDULED = re.compile(r"scheduled (\d{4}-\d{2}-\d{2})")


def parse_events_csv(text: str) -> list[Event]:
    """``docs/verification/2026-10-15-us-macro-events.csv`` -> events (all kinds and statuses, in file order)."""
    out: list[Event] = []
    for r in csv.DictReader(io.StringIO(text)):
        if not r.get("date"):
            continue
        m = _SCHEDULED.search(r.get("note") or "")
        orig = date.fromisoformat(m.group(1)) if m else None
        out.append(
            Event(date.fromisoformat(r["date"]), r["kind"].strip(), r["status"].strip(), (r.get("verification") or "").strip(), orig)
        )
    return out


def release_ms(day: date, kind: str) -> int:
    """UTC epoch ms of the release on ``day`` (08:30 ET for CPI/NFP, 14:00 ET for FOMC)."""
    hh, mm = RELEASE_CLOCK[kind]
    return ny_time_ms(day, hh, mm)


def placebo_days(trading_days: Iterable[date], excluded: Iterable[date]) -> list[date]:
    """Trading days with no excluded date on the day itself, the day before or the day after."""
    ex = set(excluded)
    one = timedelta(days=1)
    return [d for d in sorted(trading_days) if d not in ex and d - one not in ex and d + one not in ex]


# ----------------------------------------------------------------------------- official calendars (parsers)

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _month(s: str) -> int | None:
    return _MONTHS.get(s.strip()[:3].lower())


def parse_fomc_calendar(html: str) -> dict[int, list[date]]:
    """federalreserve.gov ``fomccalendars.htm`` -> {year: [decision dates]} (last day of each scheduled meeting).

    Each year is a panel headed ``YYYY FOMC Meetings``; a meeting row has a month cell
    (``fomc-meeting__month``; "Apr/May" for meetings spanning two months) and a date cell (``fomc-meeting__date``,
    e.g. "27-28", "30-1", "15-16*"). Unscheduled meetings and notation votes ("(unscheduled)", "(notation vote)")
    are skipped: their statements are not at 14:00 on a known date.
    """
    out: dict[int, list[date]] = {}
    parts = re.split(r"(\d{4})\s+FOMC\s+Meetings", html)
    for i in range(1, len(parts) - 1, 2):
        yr = int(parts[i])
        chunk = parts[i + 1]
        months = re.findall(r"fomc-meeting__month[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)<", chunk)
        dates = re.findall(r"fomc-meeting__date[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)<", chunk)
        ds: list[date] = []
        for mo, dd in zip(months, dates, strict=False):
            txt = dd.strip()
            if "unscheduled" in txt.lower() or "notation" in txt.lower():
                continue
            nums = re.findall(r"\d{1,2}", txt)
            if not nums:
                continue
            mnames = [x for x in re.split(r"[/\-]", mo.strip()) if _month(x)]
            if not mnames:
                continue
            last_month = _month(mnames[-1])
            assert last_month is not None
            ds.append(date(yr, last_month, int(nums[-1])))
        if ds:
            out[yr] = sorted(set(out.get(yr, []) + ds))
    return out


_BLS_KIND = {"cpi": re.compile(r"Consumer Price Index", re.I), "nfp": re.compile(r"Employment Situation", re.I)}


def parse_bls_schedule(html: str, year: int) -> dict[str, list[date]]:
    """BLS yearly release schedule page -> {'cpi': [...], 'nfp': [...]} (release dates).

    The page is a sequence of release rows; each row text contains a weekday-and-date (e.g. "Friday, January 08, 2021")
    and the release name. Only "Consumer Price Index" (not "...Average Price Data" or "Real Earnings" rows) and
    "Employment Situation" rows are kept."""
    out: dict[str, list[date]] = {"cpi": [], "nfp": []}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I)
    for row in rows:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row)).strip()
        m = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})", text
        )
        if not m or int(m.group(3)) != year:
            continue
        d = date(year, _month(m.group(1)) or 1, int(m.group(2)))
        for kind, pat in _BLS_KIND.items():
            if pat.search(text) and not re.search(r"Average Price|Real Earnings|State|Metropolitan", text, re.I):
                out[kind].append(d)
    return {k: sorted(set(v)) for k, v in out.items()}


# ----------------------------------------------------------------------------- measurements


def price_at(closes: Mapping[int, float], t_ms: int, bar_ms: int = BAR_MS) -> float | None:
    """Close of the bar that ended at ``t_ms``."""
    return closes.get(t_ms - bar_ms)


def log_ret(closes: Mapping[int, float], a_ms: int, b_ms: int, bar_ms: int = BAR_MS) -> float | None:
    pa, pb = price_at(closes, a_ms, bar_ms), price_at(closes, b_ms, bar_ms)
    if pa is None or pb is None or pa <= 0 or pb <= 0:
        return None
    return math.log(pb / pa)


def normal_vol(closes: Mapping[int, float], t0_ms: int, *, hours: int = 720, min_share: float = 0.9, bar_ms: int = BAR_MS) -> float | None:
    """Root mean square of hourly log returns on the grid ``t0 - j h`` (j = 1..hours); only prices up to ``t0``.

    ``None`` when fewer than ``min_share`` of the hourly returns are available or the RMS is zero."""
    rs = []
    for j in range(1, hours + 1):
        r = log_ret(closes, t0_ms - j * HOUR_MS, t0_ms - (j - 1) * HOUR_MS, bar_ms)
        if r is not None:
            rs.append(r)
    if len(rs) < min_share * hours:
        return None
    v = math.sqrt(sum(r * r for r in rs) / len(rs))
    return v if v > 0 else None


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _cont(closes: Mapping[int, float], t0: int, h1: int, h2: int, sig: float) -> tuple[int, float, float] | None:
    """(s, Y, z) with s = sign of the move t0 -> t0+h1, Y = log return t0+h1 -> t0+h2, z = s*Y / (sig*sqrt(hours))."""
    r1 = log_ret(closes, t0, t0 + h1)
    y = log_ret(closes, t0 + h1, t0 + h2)
    if r1 is None or y is None:
        return None
    s = _sign(r1)
    if s == 0:
        return None
    return s, y, s * y / (sig * math.sqrt((h2 - h1) / HOUR_MS))


def event_window(closes: Mapping[int, float], t0_ms: int) -> dict[str, Any] | None:
    """All registered measurements around one release (or one placebo time) ``t0_ms``.

    Keys: ``t0, p0, sigma, sigma7``; ``R{k}`` log return t0 -> t0+k for k in 5m, 30m, 1h, 2h, 4h, 24h; ``Rs{k}`` the
    same scaled by ``sigma * sqrt(k in hours)``; ``s`` = sign R1h, ``Y`` = log return t0+1h -> t0+24h, ``C`` = s*Y,
    ``z`` = C / (sigma sqrt 23); robustness variants ``z_30m_24h, z_2h_24h, z_1h_4h, z_1h_8h, z7``.
    ``None`` if the pre-release price, the 24 h price or the normal volatility is missing."""
    p0 = price_at(closes, t0_ms)
    sig = normal_vol(closes, t0_ms)
    if p0 is None or sig is None or price_at(closes, t0_ms + DAY_MS) is None:
        return None
    sig7 = normal_vol(closes, t0_ms, hours=168)
    w: dict[str, Any] = {"t0": t0_ms, "p0": p0, "sigma": sig, "sigma7": sig7}
    for k, dt in HORIZONS.items():
        r = log_ret(closes, t0_ms, t0_ms + dt)
        w[f"R{k}"] = r
        w[f"Rs{k}"] = None if r is None else r / (sig * math.sqrt(dt / HOUR_MS))
    c = _cont(closes, t0_ms, HOUR_MS, DAY_MS, sig)
    if c is None:
        w.update(s=0 if w["R1h"] == 0 else None, Y=None, C=None, z=None, z7=None)
    else:
        s, y, z = c
        w.update(s=s, Y=y, C=s * y, z=z, z7=None if sig7 is None else s * y / (sig7 * math.sqrt(23)))
    for key, h1, h2 in (
        ("z_30m_24h", 30 * MIN_MS, DAY_MS),
        ("z_2h_24h", 2 * HOUR_MS, DAY_MS),
        ("z_1h_4h", HOUR_MS, 4 * HOUR_MS),
        ("z_1h_8h", HOUR_MS, 8 * HOUR_MS),
    ):
        c2 = _cont(closes, t0_ms, h1, h2, sig)
        w[key] = None if c2 is None else c2[2]
    return w


def rule_pnl(signal: float, y: float, *, cost: float = 0.0005) -> float:
    """Net return on notional of holding sign(signal) over a window with log return ``y``; ``cost`` per side."""
    pos = _sign(signal)
    return pos * (math.exp(y) - 1.0) - (2 * cost if pos else 0.0)


# ----------------------------------------------------------------------------- statistics


def mde(sd: float, n: int) -> float:
    """Minimum detectable mean (80 % power, 5 % two-sided): 2.8 * SE."""
    return 2.8 * sd / math.sqrt(n) if n > 1 else float("nan")


def _quantiles(vals: list[float]) -> tuple[float, float] | None:
    v = [x for x in vals if not math.isnan(x)]
    if len(v) < 10:
        return None
    a = np.sort(np.array(v))
    return float(np.quantile(a, 0.025)), float(np.quantile(a, 0.975))


def bootstrap_mean(x: Sequence[float] | np.ndarray, *, reps: int = 2000, seed: int = 20261018) -> dict[str, Any]:
    """Mean and iid percentile 95 % CI."""
    a = np.asarray(x, float)
    n = len(a)
    if n < 3:
        return {"n": n, "stat": float(a.mean()) if n else float("nan"), "ci95": None}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(reps, n))
    vals = a[idx].mean(axis=1).tolist()
    return {"n": n, "stat": float(a.mean()), "ci95": _quantiles(vals)}


def bootstrap_two(
    a: Sequence[float] | np.ndarray,
    b: Sequence[float] | np.ndarray,
    stat: Callable[[np.ndarray, np.ndarray], float],
    *,
    reps: int = 2000,
    seed: int = 20261018,
) -> dict[str, Any]:
    """Statistic of two independent samples and a percentile 95 % CI, resampling each sample separately."""
    x, y = np.asarray(a, float), np.asarray(b, float)
    if len(x) < 3 or len(y) < 3:
        return {"stat": float("nan"), "ci95": None}
    rng = np.random.default_rng(seed)
    vals = [stat(x[rng.integers(0, len(x), len(x))], y[rng.integers(0, len(y), len(y))]) for _ in range(reps)]
    return {"stat": stat(x, y), "ci95": _quantiles(vals)}


def _gammaincc(s: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(s, x) (series / continued fraction, Numerical Recipes)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(s)
    if x < s + 1:
        ap, tot, d = s, 1.0 / s, 1.0 / s
        for _ in range(500):
            ap += 1
            d *= x / ap
            tot += d
            if abs(d) < abs(tot) * 1e-14:
                break
        return 1.0 - tot * math.exp(-x + s * math.log(x) - gln)
    b = x + 1 - s
    c = 1e300
    d = 1 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - s)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        h *= d * c
        if abs(d * c - 1) < 1e-14:
            break
    return math.exp(-x + s * math.log(x) - gln) * h


def chi2_sf(x: float, df: int) -> float:
    return _gammaincc(df / 2.0, x / 2.0)


def cochran_q(means: Sequence[float], ses: Sequence[float]) -> dict[str, float]:
    """Cochran's Q for heterogeneity of group means with standard errors ``ses``; I^2 = max(0, (Q - df) / Q)."""
    w = np.array([1.0 / (s * s) for s in ses])
    m = np.array(means, float)
    mbar = float((w * m).sum() / w.sum())
    q = float((w * (m - mbar) ** 2).sum())
    df = len(m) - 1
    return {
        "Q": q,
        "df": df,
        "p": chi2_sf(q, df) if df > 0 else float("nan"),
        "I2": max(0.0, (q - df) / q) if q > 0 else 0.0,
        "pooled": mbar,
    }


# ----------------------------------------------------------------------------- registered verdict rules


def h1_verdict(claim: float, lo: float, hi: float) -> str:
    """Registered H1 rule for a claimed volatility ratio ``claim`` against the CI [lo, hi] of our ratio."""
    if lo <= claim <= hi:
        return "✓"
    if (lo > 1 and claim > 1) or (hi < 1 and claim < 1):
        return "≈"
    return "✗"


def continuation_verdict(
    ev_ci: tuple[float, float] | None, diff_ci: tuple[float, float] | None, halves: Sequence[float], mde_value: float, gate: float
) -> str:
    """Registered H3 rule (B2). ``ev_ci``: CI of the event mean of z; ``diff_ci``: CI of event minus placebo;
    ``halves``: means of z in the two halves of the period."""
    if ev_ci is None:
        return "◯"
    lo, hi = ev_ci
    if lo > 0 or hi < 0:
        sg = 1 if lo > 0 else -1
        word = "продовження" if sg > 0 else "відкат"
        diff_ok = diff_ci is not None and (diff_ci[0] > 0 if sg > 0 else diff_ci[1] < 0)
        halves_ok = bool(halves) and all(h * sg > 0 for h in halves)
        if diff_ok and halves_ok:
            return f"✓ {word}"
        why = "плацебо-різниця включає 0" if not diff_ok else "половини розходяться"
        return f"≈ {word} ({why})"
    return "✗ (не виявлено)" if mde_value <= gate else "◯ (ворота потужності)"


def rule_verdict(ci95: tuple[float, float] | None, halves: Sequence[float], mde_value: float, gate: float) -> str:
    """Registered rule for H4a / H5: ✓ if the CI lower bound > 0 and both halves > 0; ◯ if MDE > gate; else ✗."""
    if ci95 is not None and ci95[0] > 0 and bool(halves) and all(h > 0 for h in halves):
        return "✓"
    if not (mde_value <= gate):
        return "◯ (ворота потужності)"
    return "✗ (переваги не виявлено)"
