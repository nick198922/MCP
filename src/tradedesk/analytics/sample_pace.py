"""Sample pace: how fast a live sample of trades grows (accounting, no returns).

Pure functions, no I/O; time is passed in as epoch milliseconds. Used by ``scripts/verify_s16_sample_pace.py``
(IDEA-20261030-demo-setups-depth, item «S16, темп вибірки»): weekly counts of passport trades with a t interval over
weeks, an exact Poisson interval for the demo's own rate, and the date / probability of reaching a sample threshold.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence

from tradedesk.analytics.trader_persistence import mean_ci

DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS

__all__ = [
    "DAY_MS",
    "WEEK_MS",
    "weekly_counts",
    "weekly_rate",
    "poisson_sf",
    "poisson_ci",
    "days_to_reach",
    "prob_reach",
    "reason_category",
    "signal_decisions",
]

# (category, regex on one guard / demo violation string), first match wins; texts of ``execution/guard.py`` and
# ``demo_trader._setup_cap`` / ``_bar_cap`` / ``_select_and_enter`` (IDEA-20261030-demo-setups-depth, «S40, S28, S17»).
_REASONS: tuple[tuple[str, str], ...] = (
    ("stop_wide", r"^stop .*from the entry >"),
    ("rr_low", r"^reward/risk "),
    ("score_low", r"^score -?\d+ < \d+"),
    ("at_level", r"already at the resting level"),
    ("levels", r"^levels:"),
    ("positions_max", r"open positions = max"),
    ("setup_slots", r"slots are taken|trades at risk today unknown"),
    ("bar_cap", r"cap \d+ per bar|entries on this .* bar"),
    ("day_caps", r"trades at risk today = max|entries today = max|orders in the last hour = max"),
    ("risk_caps", r"^open risk |^margin |^maintenance |^notional |^open size |^risk .* USD >|^realised loss today"),
    ("already_in", r"^already in "),
    ("decided", r"already decided"),
    ("universe", r"not in today's universe"),
    ("size_zero", r"^size is zero"),
    ("rank", r"^lower rank$"),
)


def reason_category(text: str) -> str:
    """Category of one violation string of the demo's guard (``other`` if no pattern matches)."""
    for cat, pat in _REASONS:
        if re.search(pat, text):
            return cat
    return "other"


def signal_decisions(rows: Sequence[dict], until_ms: int) -> dict[str, str]:
    """Final decision of each evaluated signal (``link_id``) from ``exec_audit`` rows with ``ts < until_ms``.

    Rows: dicts with ``ts`` (ms), ``kind`` and ``link_id``; ``payload`` a dict with ``violations`` (list of str) for the
    kinds ``refused`` / ``skipped``. A signal with a ``signal`` row gets: ``entered`` if any ``sent`` row exists;
    otherwise the category of the first violation of its first ``refused`` / ``skipped`` row (``decided`` dropped when
    the row has another violation); ``no_decision`` if it has neither. Rows at or after ``until_ms`` are ignored, so
    the decisions as of a time do not depend on later rows."""
    seen: dict[str, str] = {}
    first: dict[str, str] = {}
    sent: set[str] = set()
    for r in sorted((r for r in rows if int(r["ts"]) < until_ms), key=lambda r: int(r["ts"])):
        lid, kind = r.get("link_id"), r.get("kind")
        if not lid:
            continue
        if kind == "signal":
            seen.setdefault(lid, "no_decision")
        elif kind == "sent":
            sent.add(lid)
        elif kind in ("refused", "skipped") and lid not in first:
            vs = [str(v) for v in (r.get("payload") or {}).get("violations") or []]
            cats = [reason_category(v) for v in vs]
            keep = [c for c in cats if c != "decided"] or cats or ["other"]
            first[lid] = keep[0]
    return {lid: "entered" if lid in sent else first.get(lid, "no_decision") for lid in seen}


def weekly_counts(t_ms: Sequence[int], start_ms: int, n_weeks: int) -> list[int]:
    """Counts of events in the ``n_weeks`` 7-day bins starting at ``start_ms`` (empty weeks count 0). Events outside
    ``[start_ms, start_ms + n_weeks * WEEK_MS)`` are ignored, so a week's count depends only on events inside it."""
    if n_weeks < 0:
        raise ValueError("n_weeks must be >= 0")
    out = [0] * n_weeks
    for t in t_ms:
        k = (int(t) - start_ms) // WEEK_MS
        if 0 <= k < n_weeks:
            out[k] += 1
    return out


def weekly_rate(counts: Sequence[int]) -> dict[str, float | int | None]:
    """Mean events per week with a two-sided 95 % t interval over weeks (weeks = independent units)."""
    r = mean_ci([float(c) for c in counts])
    return {"weeks": len(counts), "total": int(sum(counts)), "mean": r["mean"], "lo": r["lo"], "hi": r["hi"], "sd": r["sd"]}


def poisson_sf(k: int, mu: float) -> float:
    """P(N >= k) for N ~ Poisson(mu)."""
    if k <= 0:
        return 1.0
    if mu <= 0:
        return 0.0
    term = math.exp(-mu)
    cdf = term
    for i in range(1, k):
        term *= mu / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def _solve(f: Callable[[float], float], lo: float, hi: float) -> float:
    """Root of an increasing f on [lo, hi] by bisection."""
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def poisson_ci(k: int, level: float = 0.95) -> tuple[float, float]:
    """Exact (Garwood) two-sided interval for the Poisson mean given ``k`` observed events."""
    if k < 0:
        raise ValueError("k must be >= 0")
    a = (1.0 - level) / 2.0
    hi_bound = 10.0 * k + 50.0
    lo = 0.0 if k == 0 else _solve(lambda m: poisson_sf(k, m) - a, 0.0, hi_bound)
    # upper: P(N <= k | mu) = a  <=>  P(N >= k + 1 | mu) = 1 - a
    hi = _solve(lambda m: poisson_sf(k + 1, m) - (1.0 - a), 0.0, hi_bound)
    return lo, hi


def days_to_reach(target: int, have: int, rate_per_day: float) -> float:
    """Expected days until ``have`` grows to ``target`` at a constant ``rate_per_day`` (inf if the rate is 0)."""
    need = target - have
    if need <= 0:
        return 0.0
    if rate_per_day <= 0:
        return math.inf
    return need / rate_per_day


def prob_reach(need: int, rate_per_day: float, days: float) -> float:
    """P(at least ``need`` new events within ``days``) for a Poisson process with ``rate_per_day``."""
    return poisson_sf(need, rate_per_day * max(days, 0.0))
