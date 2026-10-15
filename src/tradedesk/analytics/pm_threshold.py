"""Pure analytics for Polymarket daily crypto threshold markets ("Will the price of Bitcoin be above $K on <date>?").

Research support for IDEA-20261015-polymarket-threshold-vs-vol-model (scripts/verify_pm_threshold.py). No I/O; every
time is a unix-seconds parameter. Prices and probabilities are decimals (0.05 = 5 %), returns are fractions of capital.

Conventions
* A market resolves YES iff the Binance <ASSET>/USDT 1-minute candle of 12:00 America/New_York on the title date has a
  "Close" strictly above the strike; that instant is ``T`` (16:00 UTC in EDT, 17:00 UTC in EST).
* ``q`` is the YES price (a quote snapshot from CLOB ``/prices-history``; not a trade price, and that it is
  the book midpoint is an unproven hypothesis) chosen by :func:`quote_at`, which only
  looks at points at least ``lag`` seconds before ``t0``.
* "Longshot" side of a contract: YES when ``q`` is low, NO when ``q`` is high; ``calib_contract`` maps both tails to
  the longshot side so one statistic (longshot won - longshot price) measures the favourite-longshot bias.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

HOUR_S = 3600
DAY_S = 86_400
YEAR_S = 365 * DAY_S
ET = ZoneInfo("America/New_York")

PAIRS = {"bitcoin": "BTC/USDT", "ethereum": "ETH/USDT", "solana": "SOL/USDT", "xrp": "XRP/USDT"}
_EVENT_SLUG = re.compile(r"^(bitcoin|ethereum|solana|xrp)-above-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$")
_STRIKE = re.compile(r"above \$([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?(?![A-Za-z])")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1
    )
}


# ----------------------------------------------------------------------------- market metadata


def _iso_ts(s: Any) -> int | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return int(d.timestamp())


def _json_list(x: Any) -> list[Any] | None:
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
        except json.JSONDecodeError:
            return None
        return v if isinstance(v, list) else None
    return None


def settle_ts(d: date) -> int:
    """12:00 America/New_York on date ``d`` as unix seconds (DST-aware)."""
    return int(datetime.combine(d, time(12, 0), tzinfo=ET).timestamp())


def parse_strike(question: str) -> float | None:
    """Strike from the question: "$66,000", "$112000", "$1.00" and the 2025 shorthand "$118K" (= 118 000)."""
    m = _STRIKE.search(question or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1.0)
    return v * mult


def standard_rules(description: str, asset: str) -> bool:
    """True if the market text states the standard rule: Binance <PAIR> 1m candle, 12:00 ET, Close strictly higher."""
    d = (description or "").replace("\n", " ").replace("“", '"').replace("”", '"')  # 2025 texts use curly quotes
    pair = PAIRS.get(asset)
    return bool(
        pair
        and f"Binance 1 minute candle for {pair} 12:00 in the ET timezone" in d
        and '"Close" price higher than the price specified in the title' in d
    )


def outcome_yes(outcome_prices: Any) -> int | None:
    """1 if YES paid 1, 0 if NO paid 1, None for anything else (unresolved, 50/50, malformed)."""
    v = _json_list(outcome_prices)
    if not v or len(v) != 2:
        return None
    try:
        a, b = float(v[0]), float(v[1])
    except (TypeError, ValueError):
        return None
    if a == 1.0 and b == 0.0:
        return 1
    if a == 0.0 and b == 1.0:
        return 0
    return None


def classify_market(asset: str, event: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    """Validate one Gamma market of a daily threshold event BEFORE looking at any price.

    ``status`` is ``ok`` or the first failed check: ``slug`` (event is not a daily "<asset>-above-on-<date>"),
    ``strike``, ``tokens``, ``rules`` (non-standard resolution text), ``end_mismatch`` (endDate is not 12:00 ET of the
    title date), ``slug_date`` (endDate is a different day than the event slug), ``unresolved`` (outcome prices not
    exactly 1/0).
    """
    out: dict[str, Any] = {"asset": asset, "market_id": str(market.get("id")), "event_slug": event.get("slug")}
    m = _EVENT_SLUG.match(str(event.get("slug") or ""))
    if not m or m.group(1) != asset:
        return {**out, "status": "slug"}
    strike = parse_strike(str(market.get("question") or ""))
    if strike is None or strike <= 0:
        return {**out, "status": "strike"}
    outs = _json_list(market.get("outcomes"))
    toks = _json_list(market.get("clobTokenIds"))
    if not outs or not toks or len(toks) != 2 or [str(x).lower() for x in outs] != ["yes", "no"]:
        return {**out, "status": "tokens"}
    if not standard_rules(str(market.get("description") or event.get("description") or ""), asset):
        return {**out, "status": "rules"}
    end = _iso_ts(market.get("endDate") or event.get("endDate"))
    if end is None:
        return {**out, "status": "end_mismatch"}
    d_end = datetime.fromtimestamp(end, ET).date()
    t_settle = settle_ts(d_end)
    if end != t_settle:
        return {**out, "status": "end_mismatch"}
    if (_MONTHS.get(m.group(2)), int(m.group(3))) != (d_end.month, d_end.day) or (m.group(4) and int(m.group(4)) != d_end.year):
        return {**out, "status": "slug_date"}  # market endDate disagrees with the date in the event title
    y = outcome_yes(market.get("outcomePrices"))
    if y is None:
        return {**out, "status": "unresolved"}
    sched = market.get("feeSchedule") if isinstance(market.get("feeSchedule"), dict) else {}
    fees = bool(market.get("feesEnabled"))
    return {
        **out,
        "status": "ok",
        "condition_id": market.get("conditionId"),
        "yes_token": str(toks[0]),
        "no_token": str(toks[1]),
        "strike": strike,
        "T": t_settle,
        "date": datetime.fromtimestamp(t_settle, ET).date().isoformat(),
        "start": _iso_ts(market.get("acceptingOrdersTimestamp") or market.get("startDate")),
        "outcome": y,
        "fee_rate": float(sched.get("rate") or 0.0) if fees else 0.0,
        "fee_exp": float(sched.get("exponent") or 1.0) if fees else 1.0,
        "fees_enabled": fees,
    }


# ----------------------------------------------------------------------------- quotes and trades (no lookahead)


def quote_at(points: Iterable[dict[str, Any]], t0: int, *, lag: int = 60, max_age: int = 900) -> tuple[float, int] | None:
    """YES price at ``t0`` from ``/prices-history`` points ``{"t", "p"}``: the last point with ``t <= t0 - lag``.

    ``lag`` guards against a point labelled by the start of its bucket (1-minute fidelity); points after ``t0 - lag``
    are ignored, so the result cannot change when later data changes. None when no point is younger than ``max_age``.
    """
    best: tuple[float, int] | None = None
    cut = t0 - lag
    for pt in points:
        try:
            t, p = int(pt["t"]), float(pt["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if t <= cut and (best is None or t >= best[1]):
            best = (p, t)
    if best is None or t0 - best[1] > max_age:
        return None
    return best


def last_trade_before(trades: Iterable[dict[str, Any]], t0: int, yes_token: str) -> dict[str, Any] | None:
    """Last trade print (either outcome token) with ``timestamp <= t0``, as YES-equivalent and NO-equivalent prices."""
    best: dict[str, Any] | None = None
    for tr in trades:
        try:
            ts, px = int(tr["timestamp"]), float(tr["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts > t0 or (best is not None and ts < best["t"]):
            continue
        yes_px = px if str(tr.get("asset")) == yes_token else 1.0 - px
        best = {"t": ts, "age": t0 - ts, "yes_px": yes_px, "no_px": 1.0 - yes_px}
    return best


def history_alignment(
    points: Sequence[dict[str, Any]], trades: Sequence[dict[str, Any]], yes_token: str, bucket: int = 60
) -> dict[str, Any]:
    """Diagnostics of what a ``/prices-history`` point is, from trade prints around it.

    For each point (t, p) with at least one trade before it: exact-match rates of p with the last trade <= t, with the
    last and first trade in (t, t + bucket], the share of p on half-cent steps (near 1 for the mid of a 1-cent-tick
    book with a 1-cent spread), and the mean absolute distance of p to the previous and to the next trade
    (YES-equivalent prices). The distances describe the series only; they are not a lookahead test, since trades
    print at the standing quote.
    """
    tr = sorted((int(x["timestamp"]), (float(x["price"]) if str(x.get("asset")) == yes_token else 1.0 - float(x["price"]))) for x in trades)
    n = m_prev = m_last_in = m_first_in = half = 0
    d_prev: list[float] = []
    d_next: list[float] = []
    j = 0
    for pt in sorted(points, key=lambda r: int(r["t"])):
        t, p = int(pt["t"]), float(pt["p"])
        while j < len(tr) and tr[j][0] <= t:
            j += 1
        if j == 0:
            continue
        n += 1
        prev = tr[j - 1][1]
        inb = [x for x in tr[j:] if x[0] <= t + bucket]
        m_prev += abs(prev - p) < 1e-9
        m_last_in += bool(inb) and abs(inb[-1][1] - p) < 1e-9
        m_first_in += bool(inb) and abs(inb[0][1] - p) < 1e-9
        c = round(p * 1000)
        half += c % 10 == 5
        d_prev.append(abs(p - prev))
        if j < len(tr):
            d_next.append(abs(p - tr[j][1]))
    return {
        "n": n,
        "match_prev_trade": m_prev / n if n else None,
        "match_last_in_bucket": m_last_in / n if n else None,
        "match_first_in_bucket": m_first_in / n if n else None,
        "half_cent_share": half / n if n else None,
        "mean_abs_to_prev": sum(d_prev) / len(d_prev) if d_prev else None,
        "mean_abs_to_next": sum(d_next) / len(d_next) if d_next else None,
    }


# ----------------------------------------------------------------------------- model


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def digital_above(spot: float, strike: float, sigma: float, tau_years: float) -> float:
    """Risk-neutral P(S_T > K) for a driftless lognormal (r = 0): N(d2), d2 = (ln(S/K) - sigma^2 tau / 2) / (sigma sqrt tau)."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if sigma <= 0 or tau_years <= 0:
        return 1.0 if spot > strike else 0.0
    s = sigma * math.sqrt(tau_years)
    return _ncdf((math.log(spot / strike) - 0.5 * s * s) / s)


def value_at(series: Sequence[tuple[int, float]], t: int) -> float | None:
    """Last value whose timestamp (the moment the value became known) is <= t. ``series`` sorted ascending."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return series[lo - 1][1] if lo else None


def realized_vol(
    closes: Sequence[tuple[int, float]], t0: int, *, span_days: float = 30.0, lookback_days: int = 90, bar_s: int = HOUR_S
) -> float | None:
    """EWMA realised vol (annualised decimal) of bar log returns known at ``t0``.

    ``closes`` are (close_time_s, close) sorted ascending; only bars with close_time <= t0 inside ``lookback_days``
    are used. Decay lambda = 1 - 2 / (N + 1) with N = span in bars; annualisation by sqrt(bars per year).
    """
    lo_t = t0 - lookback_days * DAY_S
    pts = [(t, c) for t, c in closes if lo_t <= t <= t0 and c > 0]
    if len(pts) < 48:
        return None
    n_span = span_days * DAY_S / bar_s
    lam = 1.0 - 2.0 / (n_span + 1.0)
    var = None
    for (_, a), (_, b) in zip(pts, pts[1:], strict=False):
        r2 = math.log(b / a) ** 2
        var = r2 if var is None else lam * var + (1.0 - lam) * r2
    if var is None:
        return None
    return math.sqrt(var * YEAR_S / bar_s)


# ----------------------------------------------------------------------------- tests' building blocks


def calib_contract(q: float, outcome: int, lo: float = 0.02, hi: float = 0.15) -> dict[str, Any] | None:
    """Map a contract in either tail to its longshot side.

    Low tail: lo <= q <= hi (YES is the longshot). High tail: 1 - hi <= q <= 1 - lo (NO is the longshot).
    Returns {"tail", "price" (longshot price), "won" (0/1), "resid" = won - price} or None outside both tails.
    Under calibration E[resid] = 0; favourite-longshot bias -> E[resid] < 0.
    """
    if lo <= q <= hi:
        return {"tail": "low", "price": q, "won": outcome, "resid": outcome - q}
    if 1.0 - hi <= q <= 1.0 - lo:
        return {"tail": "high", "price": 1.0 - q, "won": 1 - outcome, "resid": (1 - outcome) - (1.0 - q)}
    return None


def no_trade_return(
    q: float, outcome: int, *, half_spread: float = 0.01, last_no_px: float | None = None, fee_rate: float = 0.0, fee_exp: float = 1.0
) -> dict[str, float]:
    """Paper purchase of NO at t0, held to resolution; return on capital (entry + fee).

    entry = max(1 - q + half_spread, last NO-equivalent print) capped at 0.999; taker fee per share is the upper bound
    ``fee_rate * (entry * (1 - entry)) ** fee_exp`` of Polymarket's ``C * p * rate * (p (1 - p)) ** exponent``.
    """
    entry = 1.0 - q + half_spread
    if last_no_px is not None:
        entry = max(entry, last_no_px)
    entry = min(max(entry, 0.001), 0.999)
    fee = fee_rate * (entry * (1.0 - entry)) ** fee_exp if fee_rate > 0 else 0.0
    cap = entry + fee
    payout = float(1 - outcome)
    return {"entry": entry, "fee": fee, "ret": (payout - cap) / cap}


def day_baskets(rows: Iterable[tuple[str, float]]) -> list[tuple[str, float, int]]:
    """Equal-weight mean per cluster key (calendar date): [(key, mean, n_contracts)] sorted by key."""
    acc: dict[str, list[float]] = defaultdict(list)
    for k, v in rows:
        acc[k].append(v)
    return [(k, sum(v) / len(v), len(v)) for k, v in sorted(acc.items())]


def bootstrap_mean(values: Sequence[float], *, n_boot: int = 10_000, seed: int = 7) -> dict[str, float | None]:
    """Mean and percentile 95 % CI by resampling cluster-level values (one value per day = cluster bootstrap)."""
    n = len(values)
    if n == 0:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    m = sum(values) / n
    if n < 2:
        return {"mean": m, "lo": None, "hi": None, "n": n}
    rng = random.Random(seed)
    bs = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return {"mean": m, "lo": bs[int(0.025 * n_boot)], "hi": bs[int(0.975 * n_boot) - 1], "n": n}


def sd(values: Sequence[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    return math.sqrt(sum((x - m) ** 2 for x in values) / (n - 1))


def mde(sd_value: float, n: int, k: float = 2.8) -> float:
    """Minimum detectable effect at alpha 5 % two-sided, power 80 %: k * sd / sqrt(n)."""
    return k * sd_value / math.sqrt(n) if n > 0 else math.inf
