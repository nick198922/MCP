"""Pure analytics for Polymarket neg-risk events: is the sum of YES quotes across mutually exclusive outcomes off 1
by more than fees, at an executable depth? (IDEA-20261015-polymarket-negrisk-sum-scanner, step 0.)

No I/O; the snapshot time is a parameter. Prices are USD per share (a YES share pays 1 USD), rates are decimals.

* Buy the set: buy K YES shares of every leg by walking its ask book; one leg pays 1, so gross per set =
  ``1 - S_ask(K)`` with ``S_ask(K) = sum_i avg_ask_i(K)``.
* Sell the set without inventory: buy K NO shares of every leg at ``1 - bid`` (the NO ask mirrors the YES bid) and
  convert N NO into N - 1 USDC through the neg-risk adapter; gross per set = ``S_bid(K) - 1``.
* Fees: Polymarket taker fee from the market's ``feeSchedule``. Upper bound ``rate * (p(1-p))**exponent`` per share
  (the convention of the neighbouring reports) and the lower bound ``p * rate * (p(1-p))**exponent``; the exact
  formula has not been checked against Polymarket's documentation, hence both.
"""

from __future__ import annotations

import json
import re
from typing import Any

FEE_UPPER = "upper"
FEE_LOWER = "lower"
STALE_SECONDS = 24 * 3600

_PLACEHOLDER = re.compile(
    r"^\s*$|\bother\b|\bothers\b|none of|\banother\b|\bthe field\b|\bfield\b"
    r"|^(person|player|candidate|team|option|company|country|movie|song|artist|driver|club)\s+([a-z]|\d+)\s*$",
    re.IGNORECASE,
)

CATEGORY_TAGS: list[tuple[str, frozenset[str]]] = [
    (
        "crypto",
        frozenset(
            {"crypto", "bitcoin", "ethereum", "solana", "xrp", "crypto-prices", "cryptocurrency", "memecoins", "defi", "stablecoins"}
        ),
    ),
    (
        "sports",
        frozenset(
            {"sports", "nba", "nfl", "nhl", "mlb", "soccer", "football", "tennis", "golf", "f1", "ufc", "boxing", "mma", "cricket"}
            | {"epl", "premier-league", "champions-league", "la-liga", "serie-a", "bundesliga", "ncaa", "cbb", "cfb", "esports"}
        ),
    ),
    ("politics", frozenset({"politics", "elections", "geopolitics", "world-elections", "us-politics", "global-elections", "trump"})),
    ("economy", frozenset({"economy", "fed", "fed-rates", "finance", "business", "economics", "inflation", "stocks", "ipos"})),
]


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _jlist(x: Any) -> list[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except json.JSONDecodeError:
            return []
    return []


# ----------------------------------------------------------------------------- fees and book walking


def fee_per_share(price: float, rate: float, exponent: float, mode: str = FEE_UPPER) -> float:
    """Taker fee per share at `price`: upper bound ``rate (p(1-p))^e`` or lower bound ``p rate (p(1-p))^e``."""
    if rate <= 0 or price <= 0 or price >= 1:
        return 0.0
    base = rate * (price * (1.0 - price)) ** exponent
    return base if mode == FEE_UPPER else price * base


def levels(raw: Any) -> list[tuple[float, float]]:
    """CLOB book side (list of ``{"price": "0.42", "size": "100"}``) -> [(price, size)] with positive size."""
    out: list[tuple[float, float]] = []
    for lv in raw or []:
        if not isinstance(lv, dict):
            continue
        p, s = _f(lv.get("price")), _f(lv.get("size"))
        if p is not None and s is not None and s > 0 and 0 < p < 1:
            out.append((p, s))
    return out


def walk(book_side: list[tuple[float, float]], qty: float, *, best_first: str) -> list[tuple[float, float]] | None:
    """Fills ``[(price, size)]`` for `qty` shares taken from `book_side`, best price first (`best_first` = "low" for
    asks, "high" for bids). None when the side has less than `qty` shares in total."""
    if qty <= 0:
        return []
    side = sorted(book_side, key=lambda x: x[0], reverse=(best_first == "high"))
    fills: list[tuple[float, float]] = []
    left = qty
    for p, s in side:
        take = min(s, left)
        fills.append((p, take))
        left -= take
        if left <= 1e-9:
            return fills
    return None


def leg_cost(book: dict[str, Any], qty: float, side: str, *, fee_rate: float, fee_exp: float, fee_mode: str) -> dict[str, float] | None:
    """One leg of a set trade for `qty` shares.

    side = "buy": buy YES from the asks; `avg` is the mean YES price, `fee` is the fee on the YES fills.
    side = "sell": buy NO at 1 - bid from the YES bids; `avg` is the mean YES bid, `fee` is the fee on NO prices.
    Returns ``{"avg", "fee", "worst"}`` (fee in USD for `qty` shares, `worst` the last level touched) or None if the
    book is too thin."""
    if side == "buy":
        fills = walk(levels(book.get("asks")), qty, best_first="low")
    else:
        fills = walk(levels(book.get("bids")), qty, best_first="high")
    if fills is None:
        return None
    notional = sum(p * s for p, s in fills)
    fee_px = (lambda p: p) if side == "buy" else (lambda p: 1.0 - p)
    fee = sum(s * fee_per_share(fee_px(p), fee_rate, fee_exp, fee_mode) for p, s in fills)
    return {"avg": notional / qty, "fee": fee, "worst": fills[-1][0]}


def set_edge(
    books: list[dict[str, Any]],
    fees: list[tuple[float, float]],
    qty: float,
    side: str,
    *,
    fee_mode: str = FEE_UPPER,
    buffer_per_leg: float = 0.005,
) -> dict[str, Any]:
    """Edge per share-set (USD) of buying (`side`="buy") or selling (`side`="sell") the full set of N legs.

    `books[i]` is leg i's CLOB book, `fees[i]` = (rate, exponent) of leg i (rate 0 when fees are off).
    Returns sum `s` (S_ask or S_bid), `gross`, `fee` (per set), `net` = gross - fee - buffer * N, `capital` per set
    (S_ask for buy, N - S_bid for sell), the per-leg averages, and `feasible` = False when any leg lacks depth."""
    n = len(books)
    avgs: list[float] = []
    fee_total = 0.0
    for b, (rate, exp) in zip(books, fees, strict=True):
        c = leg_cost(b, qty, side, fee_rate=rate, fee_exp=exp, fee_mode=fee_mode)
        if c is None:
            return {"feasible": False, "n": n}
        avgs.append(c["avg"])
        fee_total += c["fee"]
    s = sum(avgs)
    gross = 1.0 - s if side == "buy" else s - 1.0
    fee = fee_total / qty
    net = gross - fee - buffer_per_leg * n
    capital = s if side == "buy" else n - s
    return {"feasible": True, "n": n, "s": s, "gross": gross, "fee": fee, "net": net, "capital": capital, "avgs": avgs}


def thin_edge(res: dict[str, Any], *, edge_price: float = 0.02) -> bool | None:
    """H4(a) as pre-registered: the edge "holds on a thin edge leg" when the cheapest leg's average price is
    <= `edge_price` and the net per set does not exceed that leg's price (doubling its price would erase the edge).
    None when the set is infeasible."""
    if not res.get("feasible"):
        return None
    cheapest = min(res["avgs"])
    return cheapest <= edge_price and res["net"] - cheapest <= 0


def annualized(net: float, capital: float, days_to_end: float) -> float | None:
    """Simple annualized return on capital: net / capital * 365 / days. None when not computable."""
    if capital <= 0 or days_to_end <= 0:
        return None
    return net / capital * 365.0 / days_to_end


# ----------------------------------------------------------------------------- snapshot helpers


def ts_spread_ms(books: list[dict[str, Any]]) -> float | None:
    """max - min of the books' `timestamp` (unix ms). None if any leg lacks a timestamp."""
    ts = [_f(b.get("timestamp")) for b in books]
    if not ts or any(t is None for t in ts):
        return None
    return max(ts) - min(ts)  # type: ignore[type-var,operator]


def last_trade_ts(trades: list[dict[str, Any]], now: float) -> float | None:
    """Latest trade `timestamp` (unix s) not after `now`; later trades are ignored (no look-ahead)."""
    best: float | None = None
    for t in trades:
        ts = _f(t.get("timestamp"))
        if ts is None or ts > now:
            continue
        if best is None or ts > best:
            best = ts
    return best


def is_stale(last_ts: float | None, now: float, window_s: float = STALE_SECONDS) -> bool:
    return last_ts is None or now - last_ts > window_s


# ----------------------------------------------------------------------------- event classification


def is_placeholder(title: Any) -> bool:
    return not isinstance(title, str) or bool(_PLACEHOLDER.search(title.strip()))


def category(event: dict[str, Any]) -> str:
    slugs = {str(t.get("slug") or "").lower() for t in event.get("tags") or [] if isinstance(t, dict)}
    for name, keys in CATEGORY_TAGS:
        if slugs & keys:
            return name
    return "other"


def _iso_to_ts(s: Any) -> float | None:
    from datetime import datetime

    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def classify_event(event: dict[str, Any], now: float, *, min_volume: float = 50_000.0) -> dict[str, Any]:
    """Pre-registered inclusion rules for one Gamma event (time `now` in unix s).

    Returns ``{"status": "ok" | <reason>, "group": "full" | "fuzzy", "category", "legs": [{token, title, rate, exp,
    condition_id}], "days_to_end", "flags": [...]}``. `group` "full" = H4(c) (not augmented, no Other, no
    placeholder titles); "fuzzy" = H4(b)."""
    out: dict[str, Any] = {"id": str(event.get("id")), "slug": event.get("slug"), "category": category(event), "flags": []}
    if event.get("negRisk") is not True:
        return out | {"status": "not_negrisk"}
    vol = _f(event.get("volume")) or 0.0
    out["volume"] = vol
    if vol < min_volume:
        return out | {"status": "low_volume"}
    if event.get("closed") is True or event.get("active") is False:
        return out | {"status": "inactive"}
    legs: list[dict[str, Any]] = []
    flags: list[str] = []
    for m in event.get("markets") or []:
        if not isinstance(m, dict):
            continue
        if m.get("closed") is True:
            px = [_f(x) for x in _jlist(m.get("outcomePrices"))]
            if len(px) == 2 and px[0] == 0.0 and px[1] == 1.0:
                continue  # resolved NO: contributes 0 to the set
            return out | {"status": "closed_leg_unclear"}
        toks = _jlist(m.get("clobTokenIds"))
        if not toks:
            return out | {"status": "leg_without_token"}
        sched = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
        fees_on = m.get("feesEnabled") is True
        rate = float(_f(sched.get("rate")) or 0.0) if fees_on else 0.0
        exp = float(_f(sched.get("exponent")) or 1.0) if fees_on else 1.0
        title = m.get("groupItemTitle")
        if m.get("negRiskOther") is True:
            flags.append("other")
        elif is_placeholder(title):
            flags.append("placeholder")
        legs.append({"token": str(toks[0]), "title": title, "rate": rate, "exp": exp, "condition_id": m.get("conditionId")})
    if len(legs) < 2:
        return out | {"status": "too_few_legs"}
    if event.get("negRiskAugmented") is True:
        flags.append("augmented")
    end = _iso_to_ts(event.get("endDate"))
    days = (end - now) / 86400.0 if end is not None else None
    group = "fuzzy" if flags else "full"
    return out | {"status": "ok", "group": group, "legs": legs, "days_to_end": days, "flags": sorted(set(flags))}


def utc_segment(ts: float) -> int:
    """0 for 00-08 UTC, 1 for 08-16, 2 for 16-24."""
    return int((ts % 86400) // (8 * 3600))
