"""Pure analytics for Polymarket "What price will Bitcoin/Ethereum hit" touch markets.

Research support for IDEA-20261015-polymarket-touch-ladder-event-days (scripts/verify_pm_touch.py). No I/O; every time
is a unix-seconds parameter. Prices and probabilities are decimals (0.10 = 10 %), returns are fractions of capital.

Conventions
* A daily market "Will Bitcoin reach $K on <date>?" resolves YES iff any Binance <ASSET>/USDT 1-minute candle between
  12:00 AM and 11:59 PM ET of that date has High >= K ("dip to $K": Low <= K). The window is [start, end) with
  ``start`` = 00:00 ET of the date and ``end`` = 00:00 ET of the next day (DST-aware); a 1m candle belongs to the window
  iff its open time is in [start, end).
* ``q`` (YES price at decision time ``t0``) comes from :func:`tradedesk.analytics.pm_threshold.quote_at`, which only
  looks at points at least ``lag`` seconds before ``t0``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from .pm_threshold import ET, _iso_ts, _json_list, outcome_yes

ASSET_NAMES = {"bitcoin": ("Bitcoin", "BTC/USDT"), "ethereum": ("Ethereum", "ETH/USDT")}
MONTHS = {
    m: i
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1
    )
}
DAILY_SLUG = re.compile(r"^what-price-will-(bitcoin|ethereum)-hit-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$")
# weekly, e.g. "what-price-will-ethereum-hit-october-12-18[-2026]" or "...-hit-september-29-october-5[-2026]"
WEEKLY_SLUG = re.compile(r"^what-price-will-(bitcoin|ethereum)-hit-([a-z]+)-(\d{1,2})-(?:([a-z]+)-)?(\d{1,2})(?:-(\d{4}))?$")
_STRIKE = re.compile(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?(?![A-Za-z0-9])")
_Q_DATE = re.compile(r"\bon ([A-Z][a-z]+) (\d{1,2})\?")


# ----------------------------------------------------------------------------- calendar


def et_midnight(d: date) -> int:
    """00:00 America/New_York of date ``d`` as unix seconds (DST-aware)."""
    return int(datetime.combine(d, time(0, 0), tzinfo=ET).timestamp())


def day_window(d: date) -> tuple[int, int]:
    """[start, end) of the ET calendar day ``d``: 00:00 ET of d and of d + 1 (23 h / 25 h on DST switch days)."""
    return et_midnight(d), et_midnight(d + timedelta(days=1))


def et_time(d: date, hh: int, mm: int) -> int:
    return int(datetime.combine(d, time(hh, mm), tzinfo=ET).timestamp())


def et_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, ET).date()


# ----------------------------------------------------------------------------- market metadata (before any price)


def parse_strike(question: str) -> float | None:
    """Strike from "Will Bitcoin reach $91,000 on October 14?" ("$2,300", "$1.5", "$95k")."""
    m = _STRIKE.search(question or "")
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1.0)


def _norm(s: str) -> str:
    s = (s or "").replace("“", '"').replace("”", '"').replace("’", "'").replace("1-minute", "1 minute")
    return re.sub(r"\s+", " ", s)


def question_direction(question: str) -> str | None:
    q = (question or "").lower()
    if " reach " in q and " dip " not in q:
        return "up"
    if " dip to " in q and " reach " not in q:
        return "down"
    return None


def rules_direction(description: str, asset: str) -> str | None:
    """'up'/'down' if the text states the standard rule for the asset's Binance pair and the ET day window, else None."""
    name, pair = ASSET_NAMES.get(asset, ("", ""))
    d = _norm(description)
    if not pair or f"Binance 1 minute candle for {name} ({pair})" not in d or "between 12:00 AM ET and 11:59 PM ET" not in d:
        return None
    up = '"High" price equal to or greater than the price specified in the title' in d
    down = '"Low" price equal to or lower than the price specified in the title' in d
    if up == down:
        return None
    return "up" if up else "down"


def title_direction(group_item_title: str) -> str | None:
    t = (group_item_title or "").strip()
    if t.startswith("↑"):
        return "up"
    if t.startswith("↓"):
        return "down"
    return None


def parse_daily_slug(slug: str) -> tuple[str, int, int, int | None] | None:
    """(asset, month, day, year or None) of a daily touch event slug."""
    m = DAILY_SLUG.match(slug or "")
    if not m or m.group(2) not in MONTHS:
        return None
    return m.group(1), MONTHS[m.group(2)], int(m.group(3)), int(m.group(4)) if m.group(4) else None


def classify_touch_market(asset: str, event: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    """Validate one Gamma market of a DAILY touch event before looking at any price.

    ``status`` is ``ok`` or the first failed check: ``slug``, ``strike``, ``tokens``, ``direction`` (question,
    rule text and ladder arrow disagree or are missing), ``rules`` (non-standard text), ``window`` (endDate is not
    00:00 ET after the slug date, or the question names another day), ``unresolved``.
    """
    out: dict[str, Any] = {"asset": asset, "market_id": str(market.get("id")), "event_slug": event.get("slug")}
    ps = parse_daily_slug(str(event.get("slug") or ""))
    if ps is None or ps[0] != asset:
        return {**out, "status": "slug"}
    question = str(market.get("question") or "")
    strike = parse_strike(question)
    if strike is None or strike <= 0:
        return {**out, "status": "strike"}
    outs = _json_list(market.get("outcomes"))
    toks = _json_list(market.get("clobTokenIds"))
    if not outs or not toks or len(toks) != 2 or [str(x).lower() for x in outs] != ["yes", "no"]:
        return {**out, "status": "tokens"}
    rd = rules_direction(str(market.get("description") or ""), asset)
    if rd is None:
        return {**out, "status": "rules"}
    qd, td = question_direction(question), title_direction(str(market.get("groupItemTitle") or ""))
    if qd is None or qd != rd or (td is not None and td != rd):
        return {**out, "status": "direction"}
    end = _iso_ts(market.get("endDate") or event.get("endDate"))
    if end is None:
        return {**out, "status": "window"}
    d = et_date(end - 1)
    start, end_expected = day_window(d)
    if end != end_expected or (d.month, d.day) != (ps[1], ps[2]) or (ps[3] is not None and ps[3] != d.year):
        return {**out, "status": "window"}
    qm = _Q_DATE.search(question)
    if qm and (MONTHS.get(qm.group(1).lower()), int(qm.group(2))) != (d.month, d.day):
        return {**out, "status": "window"}
    y = outcome_yes(market.get("outcomePrices"))
    if y is None:
        return {**out, "status": "unresolved"}
    sched = market.get("feeSchedule") if isinstance(market.get("feeSchedule"), dict) else {}
    fees = bool(market.get("feesEnabled"))
    return {
        **out,
        "status": "ok",
        "kind": "daily",
        "condition_id": market.get("conditionId"),
        "yes_token": str(toks[0]),
        "no_token": str(toks[1]),
        "strike": strike,
        "direction": rd,
        "date": d.isoformat(),
        "start": start,
        "end": end,
        "listed": _iso_ts(market.get("acceptingOrdersTimestamp") or market.get("startDate")),
        "outcome": y,
        "fee_rate": float(sched.get("rate") or 0.0) if fees else 0.0,
        "fee_exp": float(sched.get("exponent") or 1.0) if fees else 1.0,
        "fees_enabled": fees,
    }


# ----------------------------------------------------------------------------- Binance 1m path summaries (no lookahead)


def touched(direction: str, strike: float, high: float | None, low: float | None) -> bool | None:
    """Did a path with extremes (high, low) touch the level? None when the extremes are unknown."""
    if direction == "up":
        return None if high is None else high >= strike
    if direction == "down":
        return None if low is None else low <= strike
    raise ValueError(f"direction {direction!r}")


class DayPath:
    """Streaming summary of 1m candles for one window [start, end) and decision times ``t0s``.

    Per t0: ``S0`` = close of the candle opened at t0 - 60 (the last one closed by t0), and the High/Low of candles
    opened in [start, t0). Candles opened at or after t0 never change the per-t0 fields (no lookahead), only the full
    window extremes ``high``/``low``.
    """

    def __init__(self, start: int, end: int, t0s: Sequence[int]) -> None:
        self.start, self.end = start, end
        self.t0s = tuple(t0s)
        self.high: float | None = None
        self.low: float | None = None
        self.n = 0
        self.pre: dict[int, dict[str, float | None]] = {t: {"high": None, "low": None, "S0": None} for t in self.t0s}

    def add(self, open_s: int, high: float, low: float, close: float) -> None:
        if not self.start <= open_s < self.end:
            return
        self.n += 1
        self.high = high if self.high is None else max(self.high, high)
        self.low = low if self.low is None else min(self.low, low)
        for t0, p in self.pre.items():
            if open_s < t0:
                p["high"] = high if p["high"] is None else max(p["high"], high)
                p["low"] = low if p["low"] is None else min(p["low"], low)
                if open_s == t0 - 60:
                    p["S0"] = close

    def complete(self) -> bool:
        return self.n == (self.end - self.start) // 60

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "n": self.n,
            "high": self.high,
            "low": self.low,
            "pre": {str(k): v for k, v in self.pre.items()},
        }


# ----------------------------------------------------------------------------- model (descriptive)


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def touch_prob(spot: float, strike: float, sigma: float, tau_years: float) -> float:
    """Driftless continuous-monitoring one-touch probability: 2 (1 - N(|ln(K/S)| / (sigma sqrt tau))), capped at 1."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if sigma <= 0 or tau_years <= 0:
        return 1.0 if spot == strike else 0.0
    return min(1.0, 2.0 * (1.0 - _ncdf(abs(math.log(strike / spot)) / (sigma * math.sqrt(tau_years)))))


# ----------------------------------------------------------------------------- trading economics


def fee_per_share(price: float, rate: float, exponent: float) -> float:
    """Upper bound of Polymarket's taker fee per share: rate * (p (1 - p)) ** exponent (0 when rate is 0)."""
    return rate * (price * (1.0 - price)) ** exponent if rate > 0 else 0.0


def buy_return(
    price: float, won: int, *, half_spread: float = 0.01, last_print: float | None = None, fee_rate: float = 0.0, fee_exp: float = 1.0
) -> dict[str, float]:
    """Paper purchase of one outcome quoted at ``price`` held to resolution; return on capital (entry + fee).

    entry = max(price + half_spread, last print of the same outcome), clipped to [0.001, 0.999].
    For YES pass (q, outcome, last YES print); for NO pass (1 - q, 1 - outcome, last NO print).
    """
    entry = price + half_spread
    if last_print is not None:
        entry = max(entry, last_print)
    entry = min(max(entry, 0.001), 0.999)
    fee = fee_per_share(entry, fee_rate, fee_exp)
    cap = entry + fee
    return {"entry": entry, "fee": fee, "ret": (float(won) - cap) / cap}


def pick_strangle(
    contracts: Iterable[dict[str, Any]], *, lo: float = 0.07, hi: float = 0.14, target: float = 0.10
) -> dict[str, dict] | None:
    """One up and one down contract with q closest to ``target`` inside [lo, hi]; ties -> the strike nearer to spot.

    Returns {"up": c, "down": c} or None if either side has no candidate. Uses only q, direction and strike.
    """
    best: dict[str, dict] = {}
    for c in contracts:
        q = c.get("q")
        if q is None or not lo <= q <= hi or c.get("direction") not in ("up", "down"):
            continue
        k = c["direction"]
        key = (round(abs(q - target), 9), c["strike"] if k == "up" else -c["strike"])
        if k not in best or key < best[k]["_key"]:
            best[k] = {**c, "_key": key}
    if "up" not in best or "down" not in best:
        return None
    return {k: {kk: vv for kk, vv in v.items() if kk != "_key"} for k, v in best.items()}


def strangle_return(
    legs: dict[str, dict[str, Any]], *, half_spread: float = 0.01, fee_all: float | None = None, stake: float = 1.0
) -> float:
    """Return on capital of buying ``stake`` USD of YES in each leg (payout - capital) / capital."""
    pay = 0.0
    for c in legs.values():
        lt = c.get("last_yes_px")
        rate, exp = (fee_all, 1.0) if fee_all is not None else (c.get("fee_rate", 0.0), c.get("fee_exp", 1.0))
        x = buy_return(c["q"], c["outcome"], half_spread=half_spread, last_print=lt, fee_rate=rate, fee_exp=exp)
        shares = stake / (x["entry"] + x["fee"])
        pay += shares * c["outcome"]
    cap = stake * len(legs)
    return (pay - cap) / cap


def max_losing_streak(values: Sequence[float]) -> int:
    best = cur = 0
    for v in values:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


# ----------------------------------------------------------------------------- /prices-history label semantics


def jump_trade_offsets(
    points: Sequence[dict[str, Any]], trades: Sequence[dict[str, Any]], yes_token: str, *, min_jump: float = 0.02, window: int = 180
) -> list[int]:
    """For each jump |p_t - p_prev| >= min_jump in consecutive points, the offset (trade ts - t) of the earliest trade in
    [t - window, t + window] whose YES-equivalent price moved away from p_prev in the jump's direction by at least
    half the jump. Positive offsets mean the point labelled t already reflects a later trade (label = bucket start).
    """
    tr = sorted((int(x["timestamp"]), float(x["price"]) if str(x.get("asset")) == yes_token else 1.0 - float(x["price"])) for x in trades)
    pts = sorted((int(p["t"]), float(p["p"])) for p in points)
    out: list[int] = []
    for (_, p0), (t, p1) in zip(pts, pts[1:], strict=False):
        jump = p1 - p0
        if abs(jump) < min_jump:
            continue
        sgn = 1.0 if jump > 0 else -1.0
        for ts, px in tr:
            if ts < t - window:
                continue
            if ts > t + window:
                break
            if sgn * (px - p0) >= abs(jump) / 2:
                out.append(ts - t)
                break
    return out
