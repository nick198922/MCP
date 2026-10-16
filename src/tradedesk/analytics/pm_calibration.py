"""Pure analytics for the Polymarket YES-calibration study (all categories).

Research support for IDEA-20261015-polymarket-yes-calibration-all-categories (scripts/verify_pm_yes_calibration.py).
No I/O; every time is a unix-seconds parameter. Prices and probabilities are decimals (0.45 = 45 %), returns are
fractions of capital.

Conventions (pre-registered in the idea card)
* ``T`` is the scheduled time of the market: ``gameStartTime`` for sports/esports markets that have it (there
  ``endDate`` is the game day + 7 or 14 days, not a resolution time), otherwise ``endDate``; ``t0 = T - h``. For
  sports this makes ``q`` a pre-game quote.
* A market enters the sample only on what was known at ``t0``: it accepted orders by ``t0 - 2 h``, it was not closed at
  ``t0`` (``closedTime > t0``) and a YES quote exists at ``t0``. When it closed after ``t0`` does not matter, so early
  YES resolutions after ``t0`` are not dropped (selection by outcome).
* ``q`` is the YES quote snapshot from CLOB ``/prices-history`` taken by :func:`tradedesk.analytics.pm_threshold.quote_at`
  (last point at least 60 s before ``t0``); it is not a trade price.
* Clusters are events (markets of one Gamma event are dependent); statistics are equal-weight means of event means.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

HOUR_S = 3600
DAY_S = 86_400

# Automated price ladders and up/down series (correlated through one underlying): out of the primary universe.
AUTOMATED_TAGS = frozenset(
    {"up-or-down", "crypto-prices", "multi-strikes", "stock-prices", "stocks", "equities", "indicies", "hit-price", "5M", "15M", "1H", "4h"}
)
AUTOMATED_RECURRENCE = frozenset({"5m", "15m", "hourly", "4h"})

# Category from event tags, first match wins (order is part of the pre-registration).
CATEGORY_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("esports", frozenset({"esports", "counter-strike-2", "dota-2", "league-of-legends", "valorant", "rocket-league"})),
    (
        "sports",
        frozenset({"sports", "games", "soccer", "basketball", "nba", "nfl", "nhl", "mlb", "tennis", "cricket", "ufc", "golf", "f1"}),
    ),
    ("crypto", frozenset({"crypto", "bitcoin", "ethereum", "solana", "xrp", "ripple"})),
    ("weather", frozenset({"weather", "climate"})),
    ("economics", frozenset({"economy", "finance", "fed", "business", "macro", "inflation", "fed-rates", "economic-policy"})),
    (
        "politics",
        frozenset({"politics", "elections", "trump", "geopolitics", "world", "ukraine", "israel", "middle-east", "global-elections"}),
    ),
    ("culture", frozenset({"pop-culture", "culture", "movies", "music", "mention-markets", "awards", "tweet-markets", "celebrities"})),
    ("tech", frozenset({"tech", "ai", "big-tech", "science"})),
)

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1
    )
}
_SLUG_ISO = re.compile(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?!\d)")
_SLUG_MD = re.compile(r"(?:^|-)(" + "|".join(_MONTHS) + r")-(\d{1,2})(?:st|nd|rd|th)?(?:-(20\d\d))?(?=-|$)")


# ----------------------------------------------------------------------------- metadata (before any price)


def iso_ts(s: Any) -> int | None:
    """Unix seconds from Gamma timestamps ("2025-12-04T00:00:00Z", "2025-12-05 20:37:01+00"); None if unparsable."""
    if not isinstance(s, str) or not s:
        return None
    t = s.strip().replace("Z", "+00:00").replace(" ", "T")
    if re.search(r"[+-]\d\d$", t):
        t += ":00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return int(d.timestamp())


def json_list(x: Any) -> list[Any] | None:
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
        except json.JSONDecodeError:
            return None
        return v if isinstance(v, list) else None
    return None


def resolved_outcome(outcome_prices: Any, tol: float = 1e-3) -> int | None:
    """1 if the first outcome paid 1, 0 if the second did, None for 50/50, unresolved or malformed."""
    v = json_list(outcome_prices)
    if not v or len(v) != 2:
        return None
    try:
        a, b = float(v[0]), float(v[1])
    except (TypeError, ValueError):
        return None
    if a >= 1 - tol and b <= tol:
        return 1
    if a <= tol and b >= 1 - tol:
        return 0
    return None


def category_of(tags: Iterable[str]) -> str:
    ts = {str(t).lower() for t in tags}
    for name, keys in CATEGORY_RULES:
        if ts & {k.lower() for k in keys}:
            return name
    return "other"


def is_automated(tags: Iterable[str], recurrences: Iterable[Any]) -> bool:
    """Crypto/equity price ladders, up/down series and sub-daily recurring series (correlated units)."""
    return bool(set(tags) & AUTOMATED_TAGS) or any(str(r) in AUTOMATED_RECURRENCE for r in recurrences)


def slug_dates(slug: str) -> list[tuple[int | None, int, int]]:
    """Dates written in a slug as (year or None, month, day): "...-2025-11-03", "...-on-june-30", "...-june-30-2025"."""
    s = (slug or "").lower()
    out: list[tuple[int | None, int, int]] = []
    for y, mo, d in _SLUG_ISO.findall(s):
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            out.append((int(y), int(mo), int(d)))
    for mo, d, y in _SLUG_MD.findall(s):
        if 1 <= int(d) <= 31:
            out.append((int(y) if y else None, _MONTHS[mo], int(d)))
    return out


def end_matches_slug(slug: str, end_ts: int, *, before_days: int = 1, after_days: int = 2) -> bool | None:
    """Does ``endDate`` agree with a date written in the slug? None if the slug has no date.

    True if the UTC date of ``end_ts`` lies in [d - before_days, d + after_days] for at least one slug date ``d``
    (a missing year matches the end year or the one before/after). Guards against an ``endDate`` rewritten after the
    fact: a rewritten end makes ``t0 = T - h`` carry information about the outcome.
    """
    ds = slug_dates(slug)
    if not ds:
        return None
    e = datetime.fromtimestamp(end_ts, UTC).date()
    for y, mo, d in ds:
        for yy in [y] if y else [e.year - 1, e.year, e.year + 1]:
            try:
                c = date(yy, mo, d)
            except ValueError:
                continue
            if c - timedelta(days=before_days) <= e <= c + timedelta(days=after_days):
                return True
    return False


def classify_market(event: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    """Validate one Gamma market BEFORE looking at any price.

    ``status`` is ``ok`` or the first failed check: ``automated`` (price ladder / up-down / sub-daily series),
    ``tokens`` (not exactly two CLOB tokens or no order book), ``unresolved`` (50/50, cancelled, not 1/0),
    ``no_end``, ``slug_date`` (the anchor ``T`` — gameStartTime, else endDate — disagrees with a date in the market or
    event slug), ``no_times`` (no start or closed time). ``yes_no`` tells whether the outcomes are literally ["Yes", "No"] (the primary H1 universe); other
    binaries (teams, Over/Under) keep the first outcome as "YES" and are descriptive only.
    """
    tags = [str(t) for t in event.get("tags") or []]
    recs = [s.get("recurrence") for s in event.get("series") or [] if isinstance(s, dict)]
    out: dict[str, Any] = {"market_id": str(market.get("id")), "event_id": str(event.get("id")), "event_slug": event.get("slug")}
    if is_automated(tags, recs):
        return {**out, "status": "automated"}
    outs = json_list(market.get("outcomes"))
    toks = json_list(market.get("clobTokenIds"))
    if not outs or not toks or len(outs) != 2 or len(toks) != 2 or market.get("enableOrderBook") is False:
        return {**out, "status": "tokens"}
    y = resolved_outcome(market.get("outcomePrices"))
    if y is None:
        return {**out, "status": "unresolved"}
    category = category_of(tags)
    # gameStartTime only anchors sports/esports; on other markets Gamma sometimes carries an unrelated value
    game = iso_ts(market.get("gameStartTime")) if category in ("sports", "esports") else None
    end = game if game is not None else iso_ts(market.get("endDate") or event.get("endDate"))
    if end is None:
        return {**out, "status": "no_end"}
    slug_check = [end_matches_slug(str(s or ""), end) for s in (market.get("slug"), event.get("slug"))]
    if False in slug_check:
        return {**out, "status": "slug_date"}
    start = iso_ts(market.get("acceptingOrdersTimestamp") or market.get("startDate") or market.get("createdAt"))
    closed = iso_ts(market.get("closedTime") or event.get("closedTime"))
    if start is None or closed is None:
        return {**out, "status": "no_times"}
    sched = market.get("feeSchedule") if isinstance(market.get("feeSchedule"), dict) else {}
    fees = bool(market.get("feesEnabled"))
    series = [str(s.get("slug")) for s in event.get("series") or [] if isinstance(s, dict) and s.get("slug")]
    return {
        **out,
        "status": "ok",
        "condition_id": market.get("conditionId"),
        "yes_token": str(toks[0]),
        "no_token": str(toks[1]),
        "yes_no": [str(x).lower() for x in outs] == ["yes", "no"],
        "outcome": y,
        "T": end,
        "anchor": "game_start" if game is not None else "end_date",
        "slug_dated": True in slug_check,
        "start": start,
        "closed": closed,
        "category": category,
        "series": series[0] if series else None,
        "neg_risk": bool(event.get("enableNegRisk") or market.get("negRisk")),
        "n_event_markets": len(event.get("markets") or []),
        "volume": float(market.get("volumeNum") or 0.0),
        "fee_rate": float(sched.get("rate") or 0.0) if fees else 0.0,
        "fee_exp": float(sched.get("exponent") or 1.0) if fees else 1.0,
        "fees_enabled": fees,
        "year": datetime.fromtimestamp(end, UTC).year,
    }


def eligible_at(m: dict[str, Any], t0: int, *, listed_lead: int = 2 * HOUR_S) -> bool:
    """Known at ``t0``: accepting orders since ``t0 - listed_lead`` and not closed yet (``closed > t0``)."""
    return int(m["start"]) <= t0 - listed_lead and int(m["closed"]) > t0


# ----------------------------------------------------------------------------- statistics


def basket(q: float, lo: float = 0.40, hi: float = 0.60) -> bool:
    return lo <= q <= hi


def cluster_means(rows: Iterable[tuple[str, float]]) -> list[float]:
    """Equal-weight mean per cluster key (one value per event), in key order."""
    acc: dict[str, list[float]] = defaultdict(list)
    for k, v in rows:
        acc[k].append(v)
    return [sum(v) / len(v) for _, v in sorted(acc.items())]


def devig_event(quotes: dict[str, float]) -> dict[str, float] | None:
    """Normalise the YES quotes of all still-open options of one event to sum 1 (overround removed).

    None if the sum is not positive. Callers must pass every option open at ``t0`` (missing quotes -> skip the event).
    """
    s = sum(quotes.values())
    if s <= 0:
        return None
    return {k: v / s for k, v in quotes.items()}


def no_return(
    q: float,
    outcome: int,
    *,
    half_spread: float = 0.01,
    last_no_px: float | None = None,
    fee_rate: float = 0.0,
    fee_exp: float = 1.0,
) -> dict[str, float]:
    """Paper purchase of NO at ``t0`` held to resolution: return on capital (entry + taker fee).

    entry = max(1 - q + half_spread, last NO-equivalent print), clipped to [0.001, 0.999]; fee per share is the upper
    bound ``fee_rate * (entry * (1 - entry)) ** fee_exp``. Payout 1 if NO wins.
    """
    entry = 1.0 - q + half_spread
    if last_no_px is not None:
        entry = max(entry, last_no_px)
    entry = min(max(entry, 0.001), 0.999)
    fee = fee_rate * (entry * (1.0 - entry)) ** fee_exp if fee_rate > 0 else 0.0
    cap = entry + fee
    return {"entry": entry, "fee": fee, "ret": (float(1 - outcome) - cap) / cap}


def bootstrap_ci(values: Sequence[float], *, n_boot: int = 10_000, seed: int = 7) -> dict[str, float | None]:
    """Mean and percentile 95 % CI of the mean by resampling cluster values (one value per event) with replacement."""
    n = len(values)
    if n == 0:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    x = np.asarray(values, dtype=float)
    m = float(x.mean())
    if n < 2:
        return {"mean": m, "lo": None, "hi": None, "n": n}
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    step = max(1, 2_000_000 // n)
    for i in range(0, n_boot, step):
        k = min(step, n_boot - i)
        means[i : i + k] = x[rng.integers(0, n, size=(k, n))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"mean": m, "lo": float(lo), "hi": float(hi), "n": n}


def cluster_bootstrap_ci(rows: Iterable[tuple[str, str, float]], *, n_boot: int = 10_000, seed: int = 7) -> dict[str, float | None]:
    """Two-stage cluster bootstrap: the estimate stays the equal-weight mean of unit (event) means, the CI resamples
    outer clusters (e.g. calendar dates) with replacement and keeps every unit of a drawn cluster.

    ``rows`` are (outer key, unit key, value). A unit is assigned to the outer key of its first row in sorted order.
    Each replicate is sum of unit means over drawn clusters / number of units drawn, so with one unit per cluster the
    result equals :func:`bootstrap_ci` on the unit means (same seed and draws).
    """
    acc: dict[str, list[float]] = defaultdict(list)
    outer_of: dict[str, str] = {}
    for o, u, v in sorted(rows, key=lambda r: (r[1], r[0])):
        acc[u].append(v)
        outer_of.setdefault(u, o)
    if not acc:
        return {"mean": None, "lo": None, "hi": None, "n": 0, "n_clusters": 0}
    s: dict[str, float] = defaultdict(float)
    c: dict[str, int] = defaultdict(int)
    for u, vs in sorted(acc.items()):
        s[outer_of[u]] += sum(vs) / len(vs)
        c[outer_of[u]] += 1
    keys = sorted(s)
    S = np.asarray([s[k] for k in keys], dtype=float)
    N = np.asarray([c[k] for k in keys], dtype=float)
    k_cl = len(keys)
    m = float(S.sum() / N.sum())
    if k_cl < 2:
        return {"mean": m, "lo": None, "hi": None, "n": int(N.sum()), "n_clusters": k_cl}
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    step = max(1, 2_000_000 // k_cl)
    for i in range(0, n_boot, step):
        k = min(step, n_boot - i)
        idx = rng.integers(0, k_cl, size=(k, k_cl))
        means[i : i + k] = S[idx].sum(axis=1) / N[idx].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"mean": m, "lo": float(lo), "hi": float(hi), "n": int(N.sum()), "n_clusters": k_cl}


def _yes_prints(trades: Iterable[dict[str, Any]], yes_token: str) -> list[tuple[int, float]]:
    out = []
    for x in trades:
        try:
            t, px = int(x["timestamp"]), float(x["price"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((t, px if str(x.get("asset")) == yes_token else 1.0 - px))
    return sorted(out)


def lag_profile(
    points: Sequence[dict[str, Any]], trades: Iterable[dict[str, Any]], yes_token: str, ks: Sequence[int]
) -> dict[int, list[float]]:
    """Timestamp check of ``/prices-history``: for each shift k (s), |p - last YES-equivalent print with ts <= t + k|
    over points (t, p) that have such a print. A series stamped late (lookahead) fits best at k > 0."""
    tr = _yes_prints(trades, yes_token)
    out: dict[int, list[float]] = {k: [] for k in ks}
    for pt in points:
        t, p = int(pt["t"]), float(pt["p"])
        for k in ks:
            last = None
            for ts, px in tr:
                if ts > t + k:
                    break
                last = px
            if last is not None:
                out[k].append(abs(p - last))
    return out


def jump_proximity(
    points: Sequence[dict[str, Any]],
    trades: Iterable[dict[str, Any]],
    yes_token: str,
    *,
    min_jump: float = 0.02,
    windows: Sequence[tuple[str, int, int]] = (
        ("before_0_60", -60, 0),
        ("before_60_120", -120, -60),
        ("before_120_300", -300, -120),
        ("after_0_60", 0, 60),
    ),
) -> dict[str, list[bool]]:
    """Around each trade-price jump >= ``min_jump`` (consecutive YES-equivalent prints), whether a point in the window
    [tj + a, tj + b) relative to the jump print is closer to the new price than to the old one. Points before a jump
    that are mostly closer to the new price would indicate lookahead in the point timestamps."""
    tr = _yes_prints(trades, yes_token)
    out: dict[str, list[bool]] = {name: [] for name, _, _ in windows}
    for (_, old), (tj, new) in zip(tr, tr[1:], strict=False):
        if abs(new - old) < min_jump:
            continue
        for pt in points:
            t, p = int(pt["t"]), float(pt["p"])
            for name, a, b in windows:
                if tj + a <= t < tj + b:
                    out[name].append(abs(p - new) < abs(p - old))
    return out


def mde(sd_value: float, n: int, k: float = 2.8) -> float:
    """Minimum detectable effect at alpha 5 % two-sided, power 80 %: k * sd / sqrt(n)."""
    return k * sd_value / math.sqrt(n) if n > 0 else math.inf


def stratified_event_sample(strata: dict[str, str], frac: float, seed: int = 7) -> set[str]:
    """Same fraction of events from every stratum (e.g. "category|YYYY-MM"), deterministic for a seed.

    Within a stratum events are ordered by a seeded hash of their id and the first round(frac * n) are kept (at least
    one per non-empty stratum when frac > 0). Depends only on ids and strata, never on prices or outcomes.
    """
    if frac >= 1.0:
        return set(strata)
    by: dict[str, list[str]] = defaultdict(list)
    for eid, s in strata.items():
        by[s].append(eid)
    out: set[str] = set()
    for _, ids in sorted(by.items()):
        ids.sort(key=lambda e: hashlib.sha256(f"{seed}:{e}".encode()).hexdigest())
        k = max(1, round(frac * len(ids))) if frac > 0 else 0
        out.update(ids[:k])
    return out


def reliability(rows: Sequence[tuple[float, int]], edges: Sequence[float]) -> list[dict[str, Any]]:
    """Calibration table: per [a, b) bucket (last bucket closed) n, mean q, YES frequency and gap = mean q - freq."""
    out = []
    for i, (a, b) in enumerate(zip(edges, edges[1:], strict=False)):
        last = i == len(edges) - 2
        sel = [(q, y) for q, y in rows if a <= q < b or (last and q == b)]
        if not sel:
            out.append({"lo": a, "hi": b, "n": 0, "mean_q": None, "freq": None, "gap": None})
            continue
        mq = sum(q for q, _ in sel) / len(sel)
        fy = sum(y for _, y in sel) / len(sel)
        out.append({"lo": a, "hi": b, "n": len(sel), "mean_q": mq, "freq": fy, "gap": mq - fy})
    return out
