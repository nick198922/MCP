"""Regimes of crypto <-> TradFi correlation (IDEA-20261017-macro, part B1). Pure functions, no I/O.

Conventions:
  * a "day" is a trading day of the US equity index (Nasdaq Composite); crypto is sampled at the NY close of that day
    (16:00 America/New_York, 13:00 on early-close days), so both legs of a pair cover the same window;
  * changes, never levels: log returns for prices, first differences for yields (decimals, 0.0412 = 4.12 %);
  * a change from day i to day i+1 is ``None`` when either end is missing (pairwise deletion);
  * Fed phase on a day uses only rate changes whose effective date is on or before that day (no lookahead).
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

NY = ZoneInfo("America/New_York")
HOUR_MS = 3_600_000

# ----------------------------------------------------------------------------- calendar / clock


def thanksgiving(year: int) -> date:
    """Fourth Thursday of November."""
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(days=21)


def us_early_close(d: date) -> bool:
    """Approximate NYSE/Nasdaq 13:00 ET early closes: day after Thanksgiving; Dec 24 and Jul 3 when Mon-Thu."""
    if d == thanksgiving(d.year) + timedelta(days=1):
        return True
    return (d.month, d.day) in ((12, 24), (7, 3)) and d.weekday() <= 3


def ny_time_ms(d: date, hh: int, mm: int = 0) -> int:
    """UTC epoch ms of local New York wall time ``hh:mm`` on ``d`` (DST-aware)."""
    return int(datetime.combine(d, time(hh, mm), tzinfo=NY).astimezone(UTC).timestamp() * 1000)


def ny_close_ms(d: date) -> int:
    """UTC epoch ms of the US cash close on ``d``: 16:00 ET, 13:00 ET on early-close days."""
    return ny_time_ms(d, 13 if us_early_close(d) else 16)


def price_at(closes_by_open_ms: Mapping[int, float], t_ms: int) -> float | None:
    """Price at an hour boundary ``t_ms``: close of the 1h bar that opened at ``t_ms - 1h``."""
    return closes_by_open_ms.get(t_ms - HOUR_MS)


# ----------------------------------------------------------------------------- parsers (pure)


def _num(s: Any) -> float | None:
    if s is None:
        return None
    t = str(s).replace("$", "").replace(",", "").strip()
    if not t or t.upper() in {"N/A", "NA", "--"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_nasdaq_historical(payload: Mapping[str, Any]) -> dict[date, tuple[float | None, float]]:
    """``api.nasdaq.com/api/quote/{SYM}/historical`` JSON -> {date: (open, close)} (rows without close dropped)."""
    out: dict[date, tuple[float | None, float]] = {}
    rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
    for r in rows:
        c = _num(r.get("close"))
        if c is None or c <= 0:
            continue
        m, d, y = (int(x) for x in str(r["date"]).split("/"))
        o = _num(r.get("open"))
        out[date(y, m, d)] = (o if o and o > 0 else None, c)
    return out


def parse_treasury_csv(text: str, columns: Sequence[str] = ("2 Yr", "10 Yr")) -> dict[date, tuple[float | None, ...]]:
    """US Treasury daily par yield curve CSV -> {date: (yield as decimal, ...)} for the given columns (percent / 100)."""
    out: dict[date, tuple[float | None, ...]] = {}
    for r in csv.DictReader(io.StringIO(text)):
        raw = r.get("Date")
        if not raw:
            continue
        m, d, y = (int(x) for x in raw.split("/"))
        vals = []
        for c in columns:
            v = _num(r.get(c))
            vals.append(None if v is None else v / 100.0)
        out[date(y, m, d)] = tuple(vals)
    return out


_MONTHS = {
    m: i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1
    )
}


def parse_fed_changes(html: str) -> list[tuple[date, float]]:
    """federalreserve.gov ``openmarket.htm`` -> sorted [(effective date, change in decimals)]; +0.0025 = +25 bp.

    Yearly tables follow an ``<h4>YYYY</h4>`` heading (years without changes are absent); rows are
    ``Month Day | Increase bp | Decrease bp | Level``. Dates are effective dates (the day after the decision).
    """
    out: list[tuple[date, float]] = []
    parts = re.split(r"<h4>\s*(\d{4})\s*</h4>", html)
    chunks = [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
    for yr, chunk in chunks:
        tb = chunk.find("<tbody")
        if tb < 0:
            continue
        body = chunk[tb : chunk.find("</tbody>", tb)]
        for row in re.findall(r"<tr>(.*?)</tr>", body, flags=re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S)]
            if len(cells) < 3:
                continue
            m = re.match(r"([A-Za-z]+)\s+(\d{1,2})", cells[0])
            if not m or m.group(1) not in _MONTHS:
                continue
            inc, dec = _num(cells[1]) or 0.0, _num(cells[2]) or 0.0
            if inc == 0 and dec == 0:
                continue
            out.append((date(yr, _MONTHS[m.group(1)], int(m.group(2))), (inc - dec) / 10_000.0))
    return sorted(out)


# ----------------------------------------------------------------------------- changes and statistics


def aligned_changes(days: Sequence[date], values: Mapping[date, float | None], *, kind: str = "log") -> list[float | None]:
    """Element i = change of the series from ``days[i]`` to ``days[i+1]`` (log return or difference)."""
    out: list[float | None] = []
    for a, b in zip(days[:-1], days[1:], strict=True):
        va, vb = values.get(a), values.get(b)
        if va is None or vb is None:
            out.append(None)
        elif kind == "log":
            out.append(math.log(vb / va) if va > 0 and vb > 0 else None)
        else:
            out.append(vb - va)
    return out


def complete_rows(*cols: Sequence[float | None]) -> np.ndarray:
    """Indices where every column is present (not None / NaN)."""
    n = len(cols[0])
    keep = [i for i in range(n) if all(c[i] is not None and not math.isnan(c[i]) for c in cols)]  # type: ignore[arg-type]
    return np.array(keep, dtype=int)


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    a, b = np.asarray(x, float), np.asarray(y, float)
    if len(a) < 3:
        return float("nan")
    a, b = a - a.mean(), b - b.mean()
    den = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    return float((a * b).sum()) / den if den > 0 else float("nan")


def beta(y: Sequence[float], x: Sequence[float]) -> float:
    """OLS slope of y on x (with intercept)."""
    a, b = np.asarray(x, float), np.asarray(y, float)
    if len(a) < 3:
        return float("nan")
    a = a - a.mean()
    den = float((a * a).sum())
    return float((a * (b - b.mean())).sum()) / den if den > 0 else float("nan")


def ols_r2(y: Sequence[float], xs: Sequence[Sequence[float]]) -> float:
    """R^2 of an OLS fit of y on the columns ``xs`` plus an intercept."""
    Y = np.asarray(y, float)
    if len(Y) <= len(xs) + 1:
        return float("nan")
    X = np.column_stack([np.ones(len(Y))] + [np.asarray(c, float) for c in xs])
    coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
    res = Y - X @ coef
    tss = float(((Y - Y.mean()) ** 2).sum())
    return 1.0 - float((res * res).sum()) / tss if tss > 0 else float("nan")


def rolling(
    cols: Sequence[Sequence[float | None]], stat: Callable[[list[np.ndarray]], float], *, window: int = 60, min_valid: int = 40
) -> list[float | None]:
    """Causal rolling statistic: value at i uses rows i-window+1..i only (rows with a missing column dropped)."""
    n = len(cols[0])
    out: list[float | None] = []
    for i in range(n):
        if i + 1 < window:
            out.append(None)
            continue
        idx = [j for j in range(i + 1 - window, i + 1) if all(c[j] is not None for c in cols)]
        if len(idx) < min_valid:
            out.append(None)
            continue
        out.append(stat([np.array([c[j] for j in idx], float) for c in cols]))
    return out


def rolling_corr(x: Sequence[float | None], y: Sequence[float | None], *, window: int = 60, min_valid: int = 40) -> list[float | None]:
    return rolling([x, y], lambda a: pearson(a[0], a[1]), window=window, min_valid=min_valid)


def moving_block_bootstrap(
    n: int, stat: Callable[[np.ndarray], float], *, block: int = 20, reps: int = 2000, seed: int = 20261018
) -> dict[str, Any]:
    """Point value ``stat(arange(n))`` and percentile 95 % CI from moving-block resamples of row indices."""
    point = stat(np.arange(n)) if n else float("nan")
    if n < max(3, block):
        return {"n": n, "stat": point, "ci95": None, "se": None}
    rng = np.random.default_rng(seed)
    k = -(-n // block)
    vals = []
    for _ in range(reps):
        starts = rng.integers(0, n - block + 1, size=k)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n]
        v = stat(idx)
        if not math.isnan(v):
            vals.append(v)
    if len(vals) < 10:
        return {"n": n, "stat": point, "ci95": None, "se": None}
    arr = np.sort(np.array(vals))
    return {
        "n": n,
        "stat": point,
        "ci95": (float(np.quantile(arr, 0.025)), float(np.quantile(arr, 0.975))),
        "se": float(arr.std(ddof=1)),
    }


# ----------------------------------------------------------------------------- Fed phases


def fed_phase(changes: Sequence[tuple[date, float]], day: date, *, window_days: int = 120) -> str:
    """'hike' / 'cut' if the latest change effective on or before ``day`` is a hike / cut no older than
    ``window_days``; otherwise 'pause'."""
    last: tuple[date, float] | None = None
    for d, ch in changes:
        if d <= day:
            last = (d, ch)
        else:
            break
    if last is None or (day - last[0]).days > window_days or last[1] == 0:
        return "pause"
    return "hike" if last[1] > 0 else "cut"


def episodes(labels: Sequence[str]) -> list[tuple[str, int, int]]:
    """Contiguous runs of equal labels: [(label, first index, last index)]."""
    out: list[tuple[str, int, int]] = []
    for i, lab in enumerate(labels):
        if out and out[-1][0] == lab and out[-1][2] == i - 1:
            out[-1] = (lab, out[-1][1], i)
        else:
            out.append((lab, i, i))
    return out


def h3_verdict(stat: float, lo: float, hi: float, episode_diffs: Sequence[float], *, min_episodes: int = 2) -> str:
    """Registered H3 rule (IDEA-20261017-macro B1) for d = beta_hike - beta_pause.

    ``episode_diffs``: beta_episode - beta_pause for every hike episode of >= 60 pairs.
    - '✓' only if lo > 0, every episode diff > 0 and there are >= ``min_episodes`` episodes;
    - '✗' if hi < 0;
    - '≈' if the signs agree: stat > 0 and every episode diff > 0 (at least one episode);
    - '◯' otherwise (CI spans 0 and the episode signs disagree: a case the rule does not grade).
    NaN bounds never pass a comparison, so a missing CI cannot produce ✓ or ✗.
    """
    eps = [float(e) for e in episode_diffs]
    signs_agree = bool(eps) and all(e > 0 for e in eps) and stat > 0
    if lo > 0 and signs_agree and len(eps) >= min_episodes:
        return "✓"
    if hi < 0:
        return "✗"
    if signs_agree:
        return "≈"
    return "◯"


# ----------------------------------------------------------------------------- lead-lag rule


def sign_rule(signal: Sequence[float], nxt: Sequence[float], *, cost: float = 0.0005) -> list[float]:
    """Net daily returns (on notional) of holding sign(signal[i]) over ``nxt[i]`` (the next window's log return),
    paying ``cost`` per unit of position change. Rows are consecutive; the first trade pays for opening."""
    out: list[float] = []
    prev = 0.0
    for s, r in zip(signal, nxt, strict=True):
        pos = 1.0 if s > 0 else (-1.0 if s < 0 else 0.0)
        out.append(pos * (math.exp(r) - 1.0) - cost * abs(pos - prev))
        prev = pos
    return out
