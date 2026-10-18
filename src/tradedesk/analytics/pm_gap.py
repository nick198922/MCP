"""Pure analytics for the Polymarket <-> Deribit options gap (docs/ideas/IDEA-20261017-polymarket-vs-options-forward.md).

No I/O; every time is a unix-seconds parameter. Prices and probabilities are decimals (0.05 = 5 %), IV is a decimal.

What is compared: the YES price of an open Polymarket BTC/ETH price market against the probability of the same event
from the Deribit smile. The options probability is RISK-NEUTRAL (what options price), not a real-world forecast; no
risk-premium discount is applied here and none is tuned: the gap is reported as-is and the premium is for the
pre-registration to describe (CLAUDE.md invariant 8). Texts that come from Polymarket (question, rules) are untrusted
data: they are parsed for numbers and kept as strings, never interpreted.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from . import probability
from .options import Smile
from .pm_threshold import _EVENT_SLUG, _iso_ts, _json_list, outcome_yes, settle_ts, standard_rules
from .pm_threshold import parse_strike as parse_above_strike
from .pm_touch import (
    _Q_DATE,
    ASSET_NAMES,
    DAILY_SLUG,
    MONTHS,
    WEEKLY_SLUG,
    _norm,
    day_window,
    et_date,
    et_midnight,
    fee_per_share,
    parse_daily_slug,
    question_direction,
    rules_direction,
)
from .pm_touch import parse_strike as parse_touch_strike

DAY_S = 86_400
YEAR_S = 365 * DAY_S
MAX_EXPIRY_GAP_HOURS = 36.0  # a Deribit expiry further than this after the market's end is not used
MAX_RULES_CHARS = 4000
TICKER = {"bitcoin": "BTC", "ethereum": "ETH"}


# ----------------------------------------------------------------------------- market discovery


def classify_open_market(event: dict[str, Any], market: dict[str, Any], now_ts: int, max_days: float = 14.0) -> dict[str, Any]:
    """Validate one Gamma market of an open BTC/ETH price event; ``status`` is ``ok`` or the first failed check.

    Checks: ``slug`` (not a daily threshold "<asset>-above-on-<date>" nor a daily/weekly touch "what-price-will-<asset>-hit-..."),
    ``asset`` (not BTC/ETH), ``closed`` (resolved, closed or not active), ``tokens`` (not exactly Yes/No), ``strike``,
    ``direction`` (touch: question says neither "reach" nor "dip to"), ``end`` (no end date), ``end_mismatch`` (threshold:
    endDate is not 12:00 ET of the date; touch: not 00:00 ET after the last day of the window), ``slug_date`` (the date in the
    event slug or in the question is another day than endDate), ``window`` (already ended or further than ``max_days``).
    The time checks mirror ``pm_threshold.classify_market`` and ``pm_touch.classify_touch_market``: the model's horizon and
    the Deribit expiry depend on ``end_ts``, so an unusual endDate must not pass silently. On ``ok`` the dict carries asset
    ("BTC"/"ETH"), kind ("above" | "touch"), direction ("above" | "up" | "down"), strike, end_ts, window_start (touch: start
    of the market's observation window, else None), yes_token, no_token, fee_rate, fee_exp, volume_usd, rules (text, capped),
    rules_hash (of the full text), rules_standard (the text matches the standard Binance-candle rule the model assumes).
    """
    slug = str(event.get("slug") or "")
    out: dict[str, Any] = {"market_id": str(market.get("id")), "event_slug": slug}
    kind: str
    name: str
    m = _EVENT_SLUG.match(slug)
    if m:
        kind, name = "above", m.group(1)
    else:
        t = DAILY_SLUG.match(slug) or WEEKLY_SLUG.match(slug)
        if not t:
            return {**out, "status": "slug"}
        kind, name = "touch", t.group(1)
    if name not in TICKER:
        return {**out, "status": "asset"}
    question = str(market.get("question") or "")
    if market.get("closed") or market.get("active") is False or outcome_yes(market.get("outcomePrices")) is not None:
        return {**out, "status": "closed"}
    outs, toks = _json_list(market.get("outcomes")), _json_list(market.get("clobTokenIds"))
    if not outs or not toks or len(toks) != 2 or [str(x).lower() for x in outs] != ["yes", "no"]:
        return {**out, "status": "tokens"}
    strike = parse_above_strike(question) if kind == "above" else parse_touch_strike(question)
    if strike is None or strike <= 0:
        return {**out, "status": "strike"}
    description = str(market.get("description") or event.get("description") or "")
    if kind == "above":
        direction, standard = "above", standard_rules(description, name)
    else:
        direction = question_direction(question)  # type: ignore[assignment]
        if direction is None:
            return {**out, "status": "direction"}
        rd = weekly_rules_direction(description, name) if WEEKLY_SLUG.match(slug) else rules_direction(description, name)
        standard = rd == direction
    end = _iso_ts(market.get("endDate") or event.get("endDate"))
    if end is None:
        return {**out, "status": "end"}
    window_start: int | None = None
    if kind == "above":
        d_end = et_date(end)
        if end != settle_ts(d_end):
            return {**out, "status": "end_mismatch"}
        year = m.group(4)
        if (MONTHS.get(m.group(2)), int(m.group(3))) != (d_end.month, d_end.day) or (year and int(year) != d_end.year):
            return {**out, "status": "slug_date"}
    else:
        daily = parse_daily_slug(slug) if DAILY_SLUG.match(slug) else None
        if daily is not None:
            d_last = et_date(end - 1)
            window_start, end_expected = day_window(d_last)
            if end != end_expected:
                return {**out, "status": "end_mismatch"}
            if (d_last.month, d_last.day) != (daily[1], daily[2]) or (daily[3] is not None and daily[3] != d_last.year):
                return {**out, "status": "slug_date"}
            qm = _Q_DATE.search(question)
            if qm and (MONTHS.get(qm.group(1).lower()), int(qm.group(2))) != (d_last.month, d_last.day):
                return {**out, "status": "slug_date"}
        else:
            win = weekly_window(slug, end) if WEEKLY_SLUG.match(slug) else None
            if win is None:
                return {**out, "status": "end_mismatch"}
            window_start = win[0]
    if not 0 < end - now_ts <= max_days * DAY_S:
        return {**out, "status": "window"}
    sched = market.get("feeSchedule") if isinstance(market.get("feeSchedule"), dict) else {}
    fees = bool(market.get("feesEnabled"))
    try:
        volume = float(market.get("volumeNum") or market.get("volume") or 0.0)
    except (TypeError, ValueError):
        volume = 0.0
    return {
        **out,
        "status": "ok",
        "asset": TICKER[name],
        "kind": kind,
        "direction": direction,
        "strike": strike,
        "question": question,
        "end_ts": end,
        "window_start": window_start,
        "yes_token": str(toks[0]),
        "no_token": str(toks[1]),
        "fee_rate": float(sched.get("rate") or 0.0) if fees else 0.0,
        "fee_exp": float(sched.get("exponent") or 1.0) if fees else 1.0,
        "volume_usd": volume,
        "rules": description[:MAX_RULES_CHARS],
        "rules_hash": hashlib.sha256(description.encode("utf-8")).hexdigest()[:16],
        "rules_standard": bool(standard),
    }


def weekly_rules_direction(description: str, asset: str) -> str | None:
    """'up'/'down' if a WEEKLY touch text states the Binance 1-minute-candle rule for the asset's pair with an ET window.

    The daily pattern ("between 12:00 AM ET and 11:59 PM ET") does not fit a week, so only the pair, the candle kind and the
    High/Low wording are required. The exact weekly wording has not been checked against a live text yet (see the HANDOFF).
    """
    name, pair = ASSET_NAMES.get(asset, ("", ""))
    d = _norm(description)
    if not pair or f"Binance 1 minute candle for {name} ({pair})" not in d or " ET" not in d:
        return None
    up = '"High" price equal to or greater than the price specified in the title' in d
    down = '"Low" price equal to or lower than the price specified in the title' in d
    if up == down:
        return None
    return "up" if up else "down"


def weekly_window(slug: str, end_ts: int) -> tuple[int, int] | None:
    """[start, end) of a weekly touch market from its slug ("...-hit-october-12-18[-2026]"), or None when the slug's last
    day is not the ET day before ``end_ts`` (= 00:00 ET after the last day), or the span is longer than 7 days."""
    m = WEEKLY_SLUG.match(slug)
    if not m:
        return None
    m1, d1, m2, d2, yr = m.group(2), int(m.group(3)), m.group(4) or m.group(2), int(m.group(5)), m.group(6)
    if m1 not in MONTHS or m2 not in MONTHS:
        return None
    last_et = et_date(end_ts - 1)
    year = int(yr) if yr else last_et.year
    try:
        last = date(year, MONTHS[m2], d2)
        first = date(year if MONTHS[m1] <= MONTHS[m2] else year - 1, MONTHS[m1], d1)
    except ValueError:
        return None
    if last != last_et or not 0 <= (last - first).days <= 7:
        return None
    start, end = et_midnight(first), et_midnight(last + timedelta(days=1))
    return (start, end) if end == end_ts else None


# ----------------------------------------------------------------------------- path of the underlying inside a touch window


def path_extremes(bars: Sequence[Sequence[float]], start_ts: int, max_lead_s: int = 3600) -> dict[str, float | None]:
    """High / Low / last close of Binance bars ``[open_ms, o, h, l, c, v]`` opened at or after ``start_ts``.

    The window must be covered from its start: when the first bar opens more than ``max_lead_s`` after ``start_ts``,
    the extremes are unknown (None) and not a partial guess. 1h bars give the same High/Low as the 1-minute candles the
    rules use, because the windows begin on a whole ET hour.
    """
    hi = lo = last = None
    first_open: float | None = None
    for r in bars:
        try:
            t, h, low, c = float(r[0]) / 1000.0, float(r[2]), float(r[3]), float(r[4])
        except (IndexError, TypeError, ValueError):
            continue
        if t < start_ts or not all(map(math.isfinite, (h, low, c))):
            continue
        first_open = t if first_open is None else min(first_open, t)
        hi = h if hi is None else max(hi, h)
        lo = low if lo is None else min(lo, low)
        last = c
    if first_open is None or first_open - start_ts > max_lead_s:
        return {"high": None, "low": None, "last": last}
    return {"high": hi, "low": lo, "last": last}


def touch_state(direction: str, strike: float, now_ts: int, window_start: int | None, high: float | None, low: float | None) -> str:
    """Where a touch market stands: ``pre_window`` (the window has not begun, so the start price is unknown), ``no_path``
    (no usable extremes), ``touched`` (the window's High reached a "reach" strike / Low reached a "dip" strike: resolved
    YES in substance) or ``open``."""
    if window_start is None or now_ts < window_start:
        return "pre_window"
    ext = high if direction == "up" else low
    if ext is None:
        return "no_path"
    return "touched" if (ext >= strike if direction == "up" else ext <= strike) else "open"


# ----------------------------------------------------------------------------- order book


def top_of_book(book: dict[str, Any]) -> dict[str, float | None]:
    """Best bid / ask with the size and USD value standing at that price, from a CLOB ``/book`` payload.

    The best level is found by price (highest bid, lowest ask), not by list position, because the API does not
    document the sort order. ``*_usd`` = size x price at the best level only (what a taker can hit at the best price).
    """

    def levels(rows: Any) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for r in rows if isinstance(rows, list) else []:
            try:
                p, s = float(r["price"]), float(r["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0.0 < p < 1.0 and s > 0 and math.isfinite(p) and math.isfinite(s):
                out.append((p, s))
        return out

    bids, asks = levels(book.get("bids")), levels(book.get("asks"))
    res: dict[str, float | None] = {"bid": None, "bid_size": None, "bid_usd": None, "ask": None, "ask_size": None, "ask_usd": None}
    if bids:
        p = max(x[0] for x in bids)
        s = sum(x[1] for x in bids if x[0] == p)
        res.update(bid=p, bid_size=s, bid_usd=p * s)
    if asks:
        p = min(x[0] for x in asks)
        s = sum(x[1] for x in asks if x[0] == p)
        res.update(ask=p, ask_size=s, ask_usd=p * s)
    return res


# ----------------------------------------------------------------------------- options side


def pick_expiry(expiries: Iterable[datetime], end_ts: int, max_gap_hours: float = MAX_EXPIRY_GAP_HOURS) -> datetime | None:
    """The first listed Deribit expiry at or after the market's end, if it is at most ``max_gap_hours`` later."""
    best: datetime | None = None
    for e in expiries:
        if e.timestamp() >= end_ts and (best is None or e < best):
            best = e
    if best is None or (best.timestamp() - end_ts) / 3600.0 > max_gap_hours:
        return None
    return best


def model_probability(
    smile: Smile, kind: str, strike: float, now_ts: int, end_ts: int, direction: str | None = None
) -> dict[str, Any] | None:
    """Risk-neutral probability of the market's event from a Deribit smile.

    The smile belongs to a Deribit expiry that is usually hours after the market's end. The IV curve and forward are
    kept, only the time to expiry is replaced by the market's own (end - now), so the horizon matches the market: this
    is a time alignment, not a fitted adjustment. ``kind='above'``: P(S_end > strike), Breeden-Litzenberger with the
    smile slope. ``kind='touch'`` (``direction`` 'up' = "reach", 'down' = "dip to" is required): P(price touches strike at
    any time before end), continuous monitoring, constant IV at the strike, from the CURRENT price. The caller must have
    established that the window is open and the level has not been touched yet (:func:`touch_state`); a strike on the
    wrong side of the forward (a "reach" at or below it, a "dip" at or above it) gives ``prob = None``, ``note =
    'wrong_side'``, not an invented number.
    Returns {"prob", "iv", "covered" (strike inside the quoted strikes), "tau_days", "note"} or None when the market has ended.
    """
    tau = (end_ts - now_ts) / YEAR_S
    if tau <= 0 or strike <= 0:
        return None
    sm = Smile(smile.forward, tau, [float(x) for x in smile.strikes], [float(x) for x in smile.ivs])
    iv = sm.iv(strike)
    info = {"iv": float(iv), "covered": sm.covers(strike), "tau_days": tau * 365.0}
    if kind == "above":
        return {"prob": float(probability.prob_above(sm, strike)), **info, "note": "ok"}
    if kind != "touch":
        raise ValueError(f"kind {kind!r}")
    if direction not in ("up", "down"):
        raise ValueError(f"direction {direction!r}")
    if (strike <= sm.forward) if direction == "up" else (strike >= sm.forward):
        return {"prob": None, **info, "note": "wrong_side"}
    return {"prob": float(probability.prob_touch(sm.forward, strike, iv, tau)), **info, "note": "ok"}


# ----------------------------------------------------------------------------- the gap


def net_edge(p_win: float, ask: float | None, fee_rate: float, fee_exp: float) -> float | None:
    """Expected profit per share of buying one outcome at ``ask`` and holding to resolution if it wins with ``p_win``:
    ``p_win - ask - fee``. ``ask`` already contains the half-spread (ask = mid + half-spread), the taker fee is the
    upper bound ``rate * (ask (1 - ask)) ** exponent`` of Polymarket's fee. None without an ask."""
    if ask is None:
        return None
    return p_win - ask - fee_per_share(ask, fee_rate, fee_exp)


def gap_summary(
    p_model: float,
    yes: dict[str, float | None],
    no: dict[str, float | None],
    fee_rate: float,
    fee_exp: float,
    min_depth_usd: float = 200.0,
) -> dict[str, Any]:
    """Gap between the market and the model for one market, from the two ``top_of_book`` dicts.

    * ``mid_yes`` = middle of the YES book; ``gap_mid`` = p_model - mid_yes (positive: YES is cheap against the model).
    * ``half_spread_yes`` = (ask - bid) / 2 of the YES book.
    * ``edge_yes`` / ``edge_no``: :func:`net_edge` of buying YES (win prob p_model) / NO (win prob 1 - p_model).
    * ``side`` = the side with the larger edge, ``net_edge`` its edge, ``ask_depth_usd`` the USD standing at its best ask,
      ``depth_ok`` = that depth >= ``min_depth_usd`` (the card's filter: thinner levels give paper prices that cannot be had).
    """
    bid, ask = yes.get("bid"), yes.get("ask")
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    e_yes = net_edge(p_model, ask, fee_rate, fee_exp)
    e_no = net_edge(1.0 - p_model, no.get("ask"), fee_rate, fee_exp)
    cands = [(e, s, tob) for e, s, tob in ((e_yes, "yes", yes), (e_no, "no", no)) if e is not None]
    side = net = depth = None
    if cands:
        net, side, tob = max(cands, key=lambda c: c[0])
        depth = tob.get("ask_usd")
    return {
        "mid_yes": mid,
        "gap_mid": None if mid is None else p_model - mid,
        "half_spread_yes": None if mid is None else (ask - bid) / 2.0,  # type: ignore[operator]
        "edge_yes": e_yes,
        "edge_no": e_no,
        "side": side,
        "net_edge": net,
        "ask_depth_usd": depth,
        "depth_ok": depth is not None and depth >= min_depth_usd,
    }
