"""Polymarket daily crypto markets after the observation moment (IDEA-20261029-pm-post-observation-settlement-lag).

Pure functions, no I/O. Units: prices are Polymarket share prices in USD (0..1), returns and rates are decimals
(0.003 = 0.3 %), times are unix seconds. The registration (gates K1-K3) lives in the idea card,
section "Попередня реєстрація"; this module only implements it.

* ``t_obs``: a market that resolves on the Binance 1-minute candle that OPENS at ``T`` (12:00 ET) is decided when the
  candle closes, ``t_obs = T + 60``.
* The winning side is derived from Binance klines only (never from Polymarket outcome prices).
* The entry price at ``t_obs + k`` minutes is the last ``/prices-history`` point inside a fixed window after ``t_obs``;
  points outside the window never change the result (no lookahead, no stale pre-observation quote).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from statistics import median
from typing import Any

CANDLE_S = 60
DAY_S = 86_400
PAY_WINDOW_S = 7 * DAY_S  # "winning side not paid $1 within 7 days" = non-payment (card, H1)
PAIRS = {"bitcoin": "BTC/USDT", "ethereum": "ETH/USDT", "solana": "SOL/USDT", "xrp": "XRP/USDT"}

# Registered thresholds (idea card "Критерій «вбити»" + triage); do not change without a labelled re-registration.
K1_MEDIAN_KILL = 0.993  # median winning-side price at t_obs+5 >= this -> kill
K2_RETURN_KILL = 0.003  # mean net return of entered events < this -> kill
K3_NONPAY_KILL = 0.01  # non-payment frequency > this -> kill
K_MIN_N = 100  # K1 and K3 need >= 100 events (K3 by the card; K1 aligned to it)
K2_MIN_N = 100  # K2 needs >= 100 entered events (review 2026-11-26 #5: at 30 one loss would decide the card)
ENTRY_MAX_ASK = 0.985  # forward plan: limit buy only at ask <= 0.985
HALF_SPREAD = 0.01  # kill gate is stated "при півспреду 1 цент"
APR_CAPITAL = 0.05  # opportunity cost of locked capital (card: ~5 % APR)
RESOLVED_TOL = 0.01  # outcomePrices (a, b) count as resolved if max >= 1 - tol and min <= tol
# Validity rules registered before any price was read (review 2026-11-26 #1, #4, #10).
OFFSET_FIX_MIN_MISMATCH = 3  # a candle-rule variant with >= 3 mismatches ...
FAR_FROM_STRIKE = 0.005  # a mismatch with |close / strike - 1| > 0.5 % cannot be a 1-minute shift: blocks the verdict
HIST_MISSING_MAX = 0.01  # > 1 % primary events without a history file: refuse to compute the gates
NO_PRICE_MAX = 0.10  # > 10 % primary events without a t_obs+5 price: K1 and K2 undecided
FROZEN_SHARE_MAX = 0.50  # >= 50 % of uncertain-before-T events with p5 == pre-T price: K1 and K2 undecided
FROZEN_MIN_N = 20  # the frozen-quote rule needs >= 20 such events, otherwise descriptive only


def t_obs_of(candle_open_ts: int) -> int:
    """The moment the 1-minute candle opening at ``candle_open_ts`` is final."""
    return int(candle_open_ts) + CANDLE_S


def close_at(rows: Iterable[Sequence[float]], open_ts: int) -> float | None:
    """Close of the kline row whose open time (ms, first column) equals ``open_ts`` seconds; None if absent."""
    want = int(open_ts) * 1000
    for r in rows:
        if int(r[0]) == want:
            c = float(r[4])
            return c if math.isfinite(c) and c > 0 else None
    return None


def winner_above(close: float | None, strike: float) -> int | None:
    """Index of the winning outcome of an "above" market (outcomes ["Yes", "No"]): 0 if close > strike, else 1."""
    if close is None:
        return None
    return 0 if close > strike else 1


def winner_updown(close_today: float | None, close_prev: float | None) -> int | None:
    """Index of the winning outcome of a daily "Up or Down" market (outcomes ["Up", "Down"]).

    Up (0) if today's 12:00 ET close is higher than the previous day's, Down (1) if lower; None on a tie (the market
    resolves 50-50, no side to buy) or a missing close."""
    if close_today is None or close_prev is None or close_today == close_prev:
        return None
    return 0 if close_today > close_prev else 1


_CANDLE = re.compile(r"Binance 1 minute candle for ([A-Z]+)/?USDT\b(.{0,24}?)(closing at )?(12:0[01]) in the ET timezone")


def candle_offset(description: str, asset: str, kind: str) -> int | None:
    """Offset (s) of the deciding candle's open from ``T``; see :func:`candle_variant`."""
    v = candle_variant(description, asset, kind)
    return None if v is None else v[0]


def candle_variant(description: str, asset: str, kind: str) -> tuple[int, str] | None:
    """(offset, label) of the deciding Binance 1m candle: offset (s) of its OPEN from the market's 12:00 ET ``T``,
    label = the rule-text variant ("1200", "closing_1201", "closing_1200"); None if the rule text is not one of the
    standard variants (checked on live texts 2026-11-26):

    * "candle for BTC/USDT 12:00 in the ET timezone" (2025 - 2026-10-06; "BTCUSDT" in early 2025): opens at T -> 0;
    * "candle for XRP/USDT closing at 12:01 in the ET timezone" (some up/down, 2026-10): opens at T -> 0;
    * "candle for BTC/USDT closing at 12:00 in the ET timezone" ("above" ladders from 2026-10-07): opens at T-60 -> -60.

    ``kind`` "above" also needs "Close" strictly higher than the title price, "updown" needs "Up if the previous
    close is lower than the final close". Chainlink / TWAP texts never match (no Binance candle)."""
    d = (description or "").replace("\n", " ").replace("“", '"').replace("”", '"')
    pair = PAIRS.get(asset)
    m = _CANDLE.search(d)
    if not pair or not m or m.group(1) != pair.split("/")[0]:
        return None
    if kind == "above":
        if '"Close" price higher than the price specified in the title' not in d:
            return None
    elif kind == "updown":
        if 'is lower than the final "Close" price' not in d:
            return None
    else:
        return None
    if m.group(3):  # "closing at HH:MM": the candle opens one minute earlier
        return (-60, "closing_1200") if m.group(4) == "12:00" else (0, "closing_1201")
    return (0, "1200") if m.group(4) == "12:00" else None


_MONTHS = {m: i for i, m in enumerate("january february march april may june july august september october november december".split(), 1)}
_SLUG_DATE = re.compile(r"-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$")


def slug_date(slug: str) -> tuple[int, int, int | None] | None:
    """(month, day, year or None) from an event slug ending in ``-on-<month>-<day>[-<yyyy>]``."""
    m = _SLUG_DATE.search(slug or "")
    if not m or m.group(1) not in _MONTHS:
        return None
    return _MONTHS[m.group(1)], int(m.group(2)), (int(m.group(3)) if m.group(3) else None)


def slug_date_ok(slug: str, d: date) -> bool:
    """True if the market's ET date ``d`` (from ``endDate``) is the date named in the event slug (and its year, if
    the slug has one). Catches markets attached to the wrong event (e.g. market 584188: ``endDate`` 2025-08-01 in
    event ``bitcoin-above-on-september-1``)."""
    s = slug_date(slug)
    if s is None:
        return False
    mo, dd, yy = s
    return (d.month, d.day) == (mo, dd) and (yy is None or yy == d.year)


def nearest_strikes(strikes: Sequence[float], close: float, k: int = 1) -> list[int]:
    """Indices of the ``k`` strikes closest to ``close`` (ties: lower strike first). The primary event of an "above"
    ladder is the nearest one: every strike is formally decided at ``t_obs``, but the nearest one is the least
    certain just before it."""
    order = sorted(range(len(strikes)), key=lambda i: (abs(strikes[i] - close), strikes[i]))
    return order[:k]


def price_in_window(points: Iterable[dict[str, Any]], t_from: int, t_to: int) -> tuple[float, int] | None:
    """Last ``/prices-history`` point ``{"t", "p"}`` with ``t_from <= t <= t_to``; None if there is none.

    Malformed points (missing, non-numeric, NaN / inf, out of [0, 1]) are skipped."""
    best: tuple[float, int] | None = None
    for pt in points:
        try:
            t, p = int(pt["t"]), float(pt["p"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(p) or not 0.0 <= p <= 1.0:
            continue
        if t_from <= t <= t_to and (best is None or t >= best[1]):
            best = (p, t)
    return best


def _json_list(x: Any) -> list[Any] | None:
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
        except (ValueError, RecursionError):
            return None
        return v if isinstance(v, list) else None
    return None


def _two_prices(outcome_prices: Any) -> tuple[float, float] | None:
    v = _json_list(outcome_prices)
    if not v or len(v) != 2 or any(isinstance(x, bool) for x in v):
        return None
    try:
        a, b = float(v[0]), float(v[1])
    except (TypeError, ValueError, OverflowError):
        return None
    return (a, b) if math.isfinite(a) and math.isfinite(b) else None


def resolved_index(outcome_prices: Any, tol: float = RESOLVED_TOL) -> int | None:
    """0 / 1 = which outcome Polymarket paid 1; None for anything else (unresolved, 50-50, malformed).

    Tolerant (registered 2026-11-26, review #14): ``max >= 1 - tol`` and ``min <= tol`` counts as resolved, so a
    rounding artefact like "0.9999" is not mistaken for a non-payment."""
    ab = _two_prices(outcome_prices)
    if ab is None:
        return None
    a, b = ab
    if a >= 1.0 - tol and b <= tol:
        return 0
    if b >= 1.0 - tol and a <= tol:
        return 1
    return None


def is_exact_binary(outcome_prices: Any) -> bool:
    """True if outcomePrices is exactly (1, 0) or (0, 1) (descriptive count of tolerant resolutions)."""
    return _two_prices(outcome_prices) in ((1.0, 0.0), (0.0, 1.0))


_TS = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}(?::?\d{2})?)?$")


def gamma_ts(s: Any) -> int | None:
    """Gamma timestamps: "2025-11-20T17:00:00Z", "2025-11-20 17:15:23+00" (closedTime), with or without fraction."""
    if not isinstance(s, str):
        return None
    m = _TS.match(s.strip())
    if not m:
        return None
    tz = m.group(3) or "Z"
    if tz == "Z":
        tz = "+00:00"
    elif len(tz) == 3:
        tz += ":00"
    elif ":" not in tz:
        tz = tz[:3] + ":" + tz[3:]
    try:
        d = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}{tz}")
    except ValueError:
        return None
    return int(d.astimezone(UTC).timestamp())


def settlement(market: dict[str, Any], t_obs: int, winner: int, time_field: str = "closedTime") -> dict[str, Any]:
    """Settlement facts of one market for the Binance-derived ``winner`` index.

    ``paid`` = Polymarket paid the Binance winner 1 (at any time); ``paid_7d`` = paid and the settlement time
    (``time_field``: ``closedTime`` primary, ``umaEndDate`` sensitivity) is within 7 days of ``t_obs``; ``nonpay`` =
    not ``paid_7d`` (the card's non-payment: wrong side, 50-50, unresolved or late). ``disputed`` = "disputed"
    appears in ``umaResolutionStatuses`` (descriptive)."""
    res = resolved_index(market.get("outcomePrices"))
    closed = gamma_ts(market.get(time_field))
    statuses = [str(x).lower() for x in (_json_list(market.get("umaResolutionStatuses")) or [])]
    paid = res is not None and res == winner
    lag = None if closed is None else closed - t_obs
    paid_7d = paid and lag is not None and lag <= PAY_WINDOW_S
    return {
        "resolved": res,
        "paid": paid,
        "paid_7d": paid_7d,
        "nonpay": not paid_7d,
        "mismatch": res is not None and res != winner,
        "lag_s": lag,
        "disputed": "disputed" in statuses,
        "n_proposals": statuses.count("proposed"),
    }


def taker_fee(price: float, rate: float, exponent: float) -> float:
    """Polymarket taker fee per share, ``rate * (p (1 - p)) ** exponent`` (docs checked 2026-10-16, analytics.negrisk)."""
    if rate <= 0:
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def entry_return(
    price: float,
    *,
    paid: bool,
    hold_s: float,
    half_spread: float = HALF_SPREAD,
    fee_rate: float = 0.0,
    fee_exp: float = 1.0,
    apr: float = APR_CAPITAL,
    max_ask: float = ENTRY_MAX_ASK,
) -> float | None:
    """Net return on the money paid for one winning-side share bought at ``price + half_spread`` (plus taker fee),
    held to settlement; payout 1 if ``paid`` else 0, minus ``apr`` for ``hold_s`` seconds. None = no entry (the ask
    proxy exceeds ``max_ask``)."""
    ask = price + half_spread
    if ask > max_ask:
        return None
    cost = ask + taker_fee(ask, fee_rate, fee_exp)
    return (1.0 if paid else 0.0) / cost - 1.0 - apr * max(0.0, hold_s) / (365 * DAY_S)


def median_cluster_ci(rows: Sequence[tuple[str, float]], *, n_boot: int = 2000, seed: int = 7) -> dict[str, float | None]:
    """Median of values and a percentile 95 % CI that resamples clusters (first element, e.g. UTC day) with replacement."""
    import numpy as np

    if not rows:
        return {"median": None, "lo": None, "hi": None, "n": 0}
    by: dict[str, list[float]] = {}
    for k, v in rows:
        by.setdefault(k, []).append(v)
    keys = sorted(by)
    rng = np.random.default_rng(seed)
    meds = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(keys), len(keys))
        vals = [v for i in pick for v in by[keys[i]]]
        meds.append(float(np.median(vals)))
    lo, hi = np.percentile(meds, [2.5, 97.5])
    return {"median": float(median(v for _, v in rows)), "lo": float(lo), "hi": float(hi), "n": len(rows)}


def gate_decision(
    prices: Sequence[float],
    returns: Sequence[float],
    nonpay: Sequence[bool],
    force_undecided: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Registered kill gates. ``prices``: winning-side price at t_obs+5 of every priced primary event; ``returns``:
    net returns of entered events; ``nonpay``: one flag per primary event with a Binance winner;
    ``force_undecided``: gate -> reason, set by the registered validity rules (missing data, frozen quotes,
    unexplained mismatches); such a gate is "undecided" whatever its value (it can neither kill nor pass).

    Each gate is "kill", "pass" or "undecided" (too few events or a validity rule). The card is killed if any gate
    says "kill"; a retrospective can never confirm the idea (triage 2026-10-29)."""
    force = force_undecided or {}
    out: dict[str, Any] = {}
    specs = (
        ("K1", prices, K_MIN_N, lambda xs: median(xs), lambda v: v >= K1_MEDIAN_KILL),
        ("K2", returns, K2_MIN_N, lambda xs: sum(xs) / len(xs), lambda v: v < K2_RETURN_KILL),
        ("K3", [float(x) for x in nonpay], K_MIN_N, lambda xs: sum(xs) / len(xs), lambda v: v > K3_NONPAY_KILL),
    )
    for name, xs, min_n, stat, kills in specs:
        v = stat(xs) if xs else None
        if name in force:
            verdict, why = "undecided", force[name]
        elif len(xs) < min_n:
            verdict, why = "undecided", f"n {len(xs)} < {min_n}"
        else:
            verdict, why = ("kill" if kills(v) else "pass"), ""
        out[name] = {"value": v, "n": len(xs), "verdict": verdict, "why": why}
    verdicts = [out[k]["verdict"] for k in ("K1", "K2", "K3")]
    out["card"] = "rejected" if "kill" in verdicts else ("forward" if all(v == "pass" for v in verdicts) else "forward-undecided")
    return out


def offset_audit(mismatches: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Registered candle-offset rule (review 2026-11-26 #1). ``mismatches``: one dict per primary event whose
    Polymarket resolution disagrees with our Binance winner, with keys ``kind``, ``variant`` (rule-text label),
    ``by_minus`` / ``by_plus`` (the resolution agrees with the winner from the candle one minute earlier / later).

    Per (kind, variant): if there are >= 3 mismatches and MORE than half of them are explained by the same
    neighbouring candle, that variant's offset is wrong and is shifted by -60 / +60 s ("fix"); otherwise no fix.
    If both neighbours qualify (only possible when one event is explained by both), the one explaining more wins,
    ties -> no fix (the variant is ambiguous and stays as registered)."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in mismatches:
        g = groups.setdefault((r["kind"], r["variant"]), {"n": 0, "by_minus": 0, "by_plus": 0, "fix": 0})
        g["n"] += 1
        g["by_minus"] += bool(r.get("by_minus"))
        g["by_plus"] += bool(r.get("by_plus"))
    for g in groups.values():
        if g["n"] < OFFSET_FIX_MIN_MISMATCH:
            continue
        cand = [(g["by_minus"], -60), (g["by_plus"], 60)]
        cand = [(k, d) for k, d in cand if k * 2 > g["n"]]
        if len(cand) == 1 or (len(cand) == 2 and cand[0][0] != cand[1][0]):
            g["fix"] = max(cand)[1]
    return groups


def frozen_share(pairs: Sequence[tuple[float, float]], lo: float = 0.1, hi: float = 0.9) -> dict[str, Any]:
    """Frozen-quote check (review 2026-11-26 #10). ``pairs``: (last price before the deciding candle opens, price at
    t_obs+5) of the winning token. Among events still uncertain before ``T`` (pre price in [lo, hi]) the share whose
    t_obs+5 price equals the pre-``T`` price: a quote that did not move although the outcome became known."""
    sel = [(a, b) for a, b in pairs if lo <= a <= hi]
    same = sum(1 for a, b in sel if abs(a - b) < 1e-9)
    share = same / len(sel) if sel else None
    blocks = share is not None and len(sel) >= FROZEN_MIN_N and share >= FROZEN_SHARE_MAX
    return {"n": len(sel), "same": same, "share": share, "blocks": blocks}
