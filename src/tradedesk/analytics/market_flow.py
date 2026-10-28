"""Market flow reader (IDEA-20261028-market-flow-reader, step 1). Pure functions, no I/O; time comes in as data.

Everything is put on one grid of closed UTC hours (bar open times, ms) and resampled to 4h on request:

* CVD: per bar, aggressor buys minus sells in quote units = 2 * taker_buy_quote - quote_volume (Binance klines; the
  quote is USDT, read as USD); cumulative from the window start.
* OI: snapshots (base coins) as of each bar close, summed over venues; USD = coins x the bar's perp close.
* Funding: the last settled rate per venue as of the bar close, simple-annualised (rate x 8760 / interval hours),
  then the plain mean over venues.
* Liquidations: per-bar long / short USD (Coinalyze rows stamped with the interval start), summed over venues.
* Long share: fraction of accounts long as of the bar close, plain mean over venues.
* Coinbase premium: Coinbase USD close / Binance USDT close - 1 (decimal); adjusted = minus the median of the previous
  30 days of hourly premiums (removes the slow USDT/USD basis), needs at least 7 days of them.

Units: USD for CVD, OI and liquidations; decimals for funding, premium and changes (0.01 = 1 %); fractions in [0, 1]
for long share. Descriptive only: the single metrics did not predict direction in our tests (see the tool warnings).
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np

HOUR_MS = 3_600_000
TIMEFRAME_HOURS = {"1h": 1, "4h": 4}
PREMIUM_MEDIAN_HOURS = 30 * 24
PREMIUM_MIN_OBS = 7 * 24
LEVEL_MAX_AGE_MS = 2 * HOUR_MS  # an OI / long-short snapshot older than this at a bar close is not used
PRICE_FLAT = 0.001  # |price change| below 0.1 % reads as flat
OI_FLAT = 0.002  # |OI change| below 0.2 % reads as flat
FLOW_FLAT = 0.01  # |CVD| below 1 % of the traded quote volume reads as balanced
VENUES = ("binance", "bybit")
BASE_RE = re.compile(r"^[A-Z0-9]{1,15}$")
_ROW_ERRORS = (KeyError, TypeError, ValueError, OverflowError, IndexError)


def _num(x: Any) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("not finite")
    return v


def parse_venues(values: Sequence[Any] | None) -> tuple[list[str], list[str]]:
    """Requested venues -> (known names in request order without repeats, rejected values cut to 20 chars).
    None means both venues."""
    if values is None:
        return list(VENUES), []
    names: list[str] = []
    bad: list[str] = []
    for v in values:
        name = str(v).strip().lower()
        if name not in VENUES:
            bad.append(str(v)[:20])
        elif name not in names:
            names.append(name)
    return names, bad


def parse_binance_klines(rows: Iterable[Any]) -> dict[int, dict[str, float]]:
    """Binance spot / USD-M kline rows -> {open_ms: {close, quote_volume, taker_buy_quote, delta}}.

    Row: [open_ms, open, high, low, close, base_volume, close_ms, quote_volume, trades, taker_buy_base,
    taker_buy_quote, ignore]. delta = 2 * taker_buy_quote - quote_volume (aggressor buys minus sells, quote units).
    Malformed rows (missing fields, non-numbers, taker buy above the volume, negative volume) are dropped.
    """
    out: dict[int, dict[str, float]] = {}
    for r in rows or []:
        try:
            t = int(r[0])
            close = _num(r[4])
            qv = _num(r[7])
            tbq = _num(r[10])
        except _ROW_ERRORS:
            continue
        if close <= 0 or qv < 0 or tbq < 0 or tbq > qv * (1 + 1e-9):
            continue
        out[t] = {"close": close, "quote_volume": qv, "taker_buy_quote": tbq, "delta": 2.0 * tbq - qv}
    return out


def parse_coinbase_candles(rows: Iterable[Any]) -> dict[int, float]:
    """Coinbase Exchange candles [time_s, low, high, open, close, volume] -> {open_ms: close}; bad rows dropped."""
    out: dict[int, float] = {}
    for r in rows or []:
        try:
            t = int(r[0]) * 1000
            close = _num(r[4])
        except _ROW_ERRORS:
            continue
        if close > 0:
            out[t] = close
    return out


def points(
    rows: Iterable[Any], ts_field: str, value_field: str, *, lo: float | None = None, hi: float | None = None
) -> list[tuple[int, float]]:
    """Dict rows -> [(ts_ms, value)] sorted by time (a repeated ts keeps the last row). Values outside [lo, hi],
    non-numbers and rows without the fields are dropped."""
    out: dict[int, float] = {}
    for r in rows or []:
        try:
            t = int(r[ts_field])
            v = _num(r[value_field])
        except _ROW_ERRORS:
            continue
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            continue
        out[t] = v
    return sorted(out.items())


def liquidation_bars(rows: Iterable[Any]) -> dict[int, tuple[float, float]]:
    """Coinalyze liquidation rows {t: interval start in s, l: long, s: short} -> {open_ms: (long, short)}."""
    out: dict[int, tuple[float, float]] = {}
    for r in rows or []:
        try:
            t = int(r["t"]) * 1000
            lg = _num(r["l"])
            sh = _num(r["s"])
        except _ROW_ERRORS:
            continue
        if lg >= 0 and sh >= 0:
            out[t] = (lg, sh)
    return out


def asof(pts: Sequence[tuple[int, float]], t: int, max_age_ms: int) -> float | None:
    """Value of the last point at or before t, if it is at most max_age_ms old; else None."""
    if not pts:
        return None
    i = bisect_right([p[0] for p in pts], t) - 1
    if i < 0 or t - pts[i][0] > max_age_ms:
        return None
    return pts[i][1]


def funding_interval_hours(pts: Sequence[tuple[int, float]]) -> float | None:
    """Median spacing of settlements in hours (None with fewer than 2 points or outside 0.5..24 h)."""
    if len(pts) < 2:
        return None
    gaps = [(b[0] - a[0]) / HOUR_MS for a, b in zip(pts, pts[1:], strict=False) if b[0] > a[0]]
    if not gaps:
        return None
    h = float(np.median(gaps))
    return h if 0.5 <= h <= 24 else None


def premium_series(
    coinbase: Mapping[int, float], spot: Mapping[int, Mapping[str, float]], grid: Sequence[int]
) -> tuple[list[float | None], list[float | None]]:
    """(raw, adjusted) Coinbase premium per grid bar. raw = coinbase close / Binance spot close - 1; adjusted = raw
    minus the median of the raw values of the previous PREMIUM_MEDIAN_HOURS bars (the bar itself excluded), None with
    fewer than PREMIUM_MIN_OBS of them."""
    raw: list[float | None] = []
    for t in grid:
        cb = coinbase.get(t)
        sp = spot.get(t)
        raw.append(cb / sp["close"] - 1.0 if cb is not None and sp is not None and sp["close"] > 0 else None)
    arr = np.array([np.nan if v is None else v for v in raw], dtype=float)
    adj: list[float | None] = []
    for i, v in enumerate(raw):
        w = arr[max(0, i - PREMIUM_MEDIAN_HOURS) : i]
        w = w[np.isfinite(w)]
        adj.append(v - float(np.median(w)) if v is not None and w.size >= PREMIUM_MIN_OBS else None)
    return raw, adj


def hourly_rows(
    grid: Sequence[int],
    *,
    spot: Mapping[int, Mapping[str, float]],
    perp: Mapping[int, Mapping[str, float]],
    oi: Mapping[str, Sequence[tuple[int, float]]],
    funding: Mapping[str, tuple[Sequence[tuple[int, float]], float]],
    liquidations: Mapping[str, Mapping[int, tuple[float, float]]],
    long_share: Mapping[str, Sequence[tuple[int, float]]],
    premium: Mapping[int, float | None],
    premium_adj: Mapping[int, float | None],
) -> list[dict[str, Any]]:
    """One row per closed hour of the grid. Venue inputs hold only venues that returned data.

    A bar's OI total is None unless every OI venue has a snapshot at its close (a missing venue would fake a jump);
    funding and long share average the venues that have a value. Liquidations of an hour after a venue's last
    published row are None (not yet published), missing hours before it count as 0.
    """
    liq_last = {v: max(d) for v, d in liquidations.items() if d}
    out: list[dict[str, Any]] = []
    for t in grid:
        c = t + HOUR_MS
        s, p = spot.get(t), perp.get(t)
        price = s["close"] if s else (p["close"] if p else None)
        perp_close = p["close"] if p else (s["close"] if s else None)
        oi_vals = [asof(pts, c, LEVEL_MAX_AGE_MS) for pts in oi.values()]
        oi_known = [v for v in oi_vals if v is not None]
        oi_coins = sum(oi_known) if oi_vals and len(oi_known) == len(oi_vals) else None
        fund = [asof(pts, c, int(2 * hrs * HOUR_MS)) for pts, hrs in funding.values()]
        fund_ann = [f * 8760.0 / hrs for f, (_, hrs) in zip(fund, funding.values(), strict=True) if f is not None]
        shares = [v for v in (asof(pts, c, LEVEL_MAX_AGE_MS) for pts in long_share.values()) if v is not None]
        lg = sh = None
        for v, bars in liquidations.items():
            if v not in liq_last or t > liq_last[v]:
                continue
            a, b = bars.get(t, (0.0, 0.0))
            lg, sh = (lg or 0.0) + a, (sh or 0.0) + b
        out.append(
            {
                "ts": t,
                "price": price,
                "spot_delta": s["delta"] if s else None,
                "spot_volume": s["quote_volume"] if s else None,
                "perp_delta": p["delta"] if p else None,
                "perp_volume": p["quote_volume"] if p else None,
                "oi_coins": oi_coins,
                "oi_usd": oi_coins * perp_close if oi_coins is not None and perp_close is not None else None,
                "funding_annualized": sum(fund_ann) / len(fund_ann) if fund_ann else None,
                "liq_long": lg,
                "liq_short": sh,
                "long_share": sum(shares) / len(shares) if shares else None,
                "premium": premium.get(t),
                "premium_adj": premium_adj.get(t),
            }
        )
    return out


_SUM_FIELDS = ("spot_delta", "spot_volume", "perp_delta", "perp_volume", "liq_long", "liq_short")
_LEVEL_FIELDS = ("price", "oi_coins", "oi_usd", "funding_annualized", "long_share", "premium", "premium_adj")


def resample(rows: Sequence[Mapping[str, Any]], hours: int) -> list[dict[str, Any]]:
    """Hourly rows -> complete UTC-aligned buckets of `hours` (a bucket missing an hour is dropped). Flows sum (None
    when no hour has a value), levels take the bucket's last hour (its close)."""
    if hours == 1:
        return [dict(r) for r in rows]
    size = hours * HOUR_MS
    buckets: dict[int, list[Mapping[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(r["ts"] // size * size, []).append(r)
    out = []
    for b in sorted(buckets):
        members = sorted(buckets[b], key=lambda r: r["ts"])
        if len(members) != hours:
            continue
        row: dict[str, Any] = {"ts": b}
        for f in _SUM_FIELDS:
            vals = [m[f] for m in members if m[f] is not None]
            row[f] = sum(vals) if vals else None
        for f in _LEVEL_FIELDS:
            row[f] = members[-1][f]
        out.append(row)
    return out


def add_cumulative(rows: list[dict[str, Any]]) -> None:
    """Running spot / perp CVD (USD) from the window start; a bar without a delta keeps the previous level."""
    acc = {"spot": 0.0, "perp": 0.0}
    seen = {"spot": False, "perp": False}
    for r in rows:
        for k in ("spot", "perp"):
            d = r.get(f"{k}_delta")
            if d is not None:
                acc[k] += d
                seen[k] = True
            r[f"{k}_cvd"] = acc[k] if seen[k] else None


def _sum(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return sum(vals) if vals else None


def _mean(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return sum(vals) / len(vals) if vals else None


def _change(ref: Mapping[str, Any] | None, last: Mapping[str, Any], field: str, *, relative: bool) -> float | None:
    if ref is None or ref.get(field) is None or last.get(field) is None:
        return None
    if relative:
        return last[field] / ref[field] - 1.0 if ref[field] else None
    return last[field] - ref[field]


def window_summary(rows: Sequence[Mapping[str, Any]], hours: int) -> dict[str, Any] | None:
    """Flows summed over the last `hours` bars; level changes from the close of the bar before them to the last close.
    None when there are not `hours` + 1 rows."""
    if hours < 1 or len(rows) < hours + 1:
        return None
    ref, win, last = rows[-hours - 1], rows[-hours:], rows[-1]
    return {
        "hours": hours,
        "from_ts": win[0]["ts"],
        "price_change": _change(ref, last, "price", relative=True),
        "oi_change": _change(ref, last, "oi_coins", relative=True),
        "oi_usd_change": _change(ref, last, "oi_usd", relative=False),
        "spot_cvd": _sum(win, "spot_delta"),
        "spot_volume": _sum(win, "spot_volume"),
        "perp_cvd": _sum(win, "perp_delta"),
        "perp_volume": _sum(win, "perp_volume"),
        "liq_long": _sum(win, "liq_long"),
        "liq_short": _sum(win, "liq_short"),
        "funding_annualized_mean": _mean(win, "funding_annualized"),
        "long_share_change": _change(ref, last, "long_share", relative=False),
        "premium_adj_mean": _mean(win, "premium_adj"),
    }


def _dir(x: float | None, flat: float) -> str | None:
    if x is None:
        return None
    return "up" if x > flat else ("down" if x < -flat else "flat")


def _flow(cvd: float | None, volume: float | None) -> str | None:
    if cvd is None or not volume:
        return None
    share = cvd / volume
    return "buy" if share > FLOW_FLAT else ("sell" if share < -FLOW_FLAT else "balanced")


REGIME_UA = {
    "new_longs": "ріст на нових позиціях (плече)",
    "short_covering": "ріст на закритті шортів",
    "new_shorts": "падіння на нових шортах",
    "long_unwind": "падіння на закритті лонгів",
    "build_up": "набір позицій без руху ціни",
    "unwind": "закриття позицій без руху ціни",
    "oi_flat": "OI майже не змінився",
}
PATTERN_UA = {
    "leverage_without_spot": "ріст на плечі без спот-попиту (крихкий)",
    "flush": "очищення: падіння, OI вниз, ліквідують лонги",
    "shorts_into_spot_bid": "спот купує, ф'ючерси продають, funding ≤ 0 (шорти проти спот-попиту)",
}


def quadrant(s: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Descriptive reading of one window summary: price x OI regime, aggressor side of perps and spot, and the three
    combinations named in the card. Thresholds: PRICE_FLAT, OI_FLAT, FLOW_FLAT (share of traded volume)."""
    if s is None:
        return None
    price = _dir(s["price_change"], PRICE_FLAT)
    oi = _dir(s["oi_change"], OI_FLAT)
    perp = _flow(s["perp_cvd"], s["perp_volume"])
    spot = _flow(s["spot_cvd"], s["spot_volume"])
    regime = None
    if price is not None and oi is not None:
        if oi == "flat":
            regime = "oi_flat"
        elif price == "flat":
            regime = "build_up" if oi == "up" else "unwind"
        elif price == "up":
            regime = "new_longs" if oi == "up" else "short_covering"
        else:
            regime = "new_shorts" if oi == "up" else "long_unwind"
    patterns = []
    if price == "up" and oi == "up" and perp == "buy" and spot == "sell":
        patterns.append("leverage_without_spot")
    lg, sh = s["liq_long"], s["liq_short"]
    if price == "down" and oi == "down" and lg is not None and sh is not None and lg > sh:
        patterns.append("flush")
    fund = s["funding_annualized_mean"]
    if spot == "buy" and perp == "sell" and fund is not None and fund <= 0:
        patterns.append("shorts_into_spot_bid")
    return {
        "hours": s["hours"],
        "price": price,
        "oi": oi,
        "perp_flow": perp,
        "spot_flow": spot,
        "regime": regime,
        "regime_ua": REGIME_UA.get(regime) if regime else None,
        "patterns": patterns,
        "patterns_ua": [PATTERN_UA[p] for p in patterns],
    }


def usd_ua(x: float) -> str:
    """Signed USD in Ukrainian short form: +137 млн $, −1.2 млрд $, +850 тис $."""
    sign = "+" if x >= 0 else "−"
    a = abs(x)
    if a >= 1e9:
        return f"{sign}{a / 1e9:.2f} млрд $"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.0f} млн $"
    return f"{sign}{a / 1e3:.0f} тис $"


def pct_ua(x: float, nd: int = 1) -> str:
    sign = "+" if x >= 0 else "−"
    return f"{sign}{abs(x) * 100:.{nd}f} %"


_FLOW_UA = {"buy": "купують", "sell": "продають", "balanced": "збалансовані"}


def describe_ua(s: Mapping[str, Any] | None, q: Mapping[str, Any] | None) -> str | None:
    """One short descriptive Ukrainian sentence for a window, e.g. «За 24 год: ціна −1.2 %, OI −3.1 % (у монетах);
    ф'ючерси продають (−420 млн $), спот купує (+130 млн $) → падіння на закритті лонгів.» None without data."""
    if s is None or q is None:
        return None
    parts = []
    if s["price_change"] is not None:
        parts.append(f"ціна {pct_ua(s['price_change'])}")
    if s["oi_change"] is not None:
        parts.append(f"OI {pct_ua(s['oi_change'])} (у монетах)")
    flows = []
    if q["perp_flow"] is not None and s["perp_cvd"] is not None:
        flows.append(f"ф'ючерси {_FLOW_UA[q['perp_flow']]} ({usd_ua(s['perp_cvd'])})")
    if q["spot_flow"] is not None and s["spot_cvd"] is not None:
        verb = {"buy": "купує", "sell": "продає", "balanced": "збалансований"}[q["spot_flow"]]
        flows.append(f"спот {verb} ({usd_ua(s['spot_cvd'])})")
    if not parts and not flows:
        return None
    text = f"За {s['hours']} год: " + ", ".join(parts)
    if flows:
        text += ("; " if parts else "") + ", ".join(flows)
    tail = [q["regime_ua"]] if q["regime_ua"] else []
    tail += q["patterns_ua"]
    if tail:
        text += " → " + "; ".join(tail)
    return text + "."


def venue_latest(
    t_close: int,
    *,
    oi: Mapping[str, Sequence[tuple[int, float]]],
    funding: Mapping[str, tuple[Sequence[tuple[int, float]], float]],
    long_share: Mapping[str, Sequence[tuple[int, float]]],
    liquidations: Mapping[str, Mapping[int, tuple[float, float]]],
    since_ms: int,
) -> dict[str, dict[str, Any]]:
    """Per venue, as of the last bar close: OI coins, last settled funding (per interval and annualised) with the
    interval, long share, and liquidations summed over bars opened at or after since_ms."""
    out: dict[str, dict[str, Any]] = {}
    for v in VENUES:
        row: dict[str, Any] = {}
        if v in oi:
            row["oi_coins"] = asof(oi[v], t_close, LEVEL_MAX_AGE_MS)
        if v in funding:
            pts, hrs = funding[v]
            rate = asof(pts, t_close, int(2 * hrs * HOUR_MS))
            row.update(
                {
                    "funding_rate": rate,
                    "funding_interval_hours": hrs,
                    "funding_annualized": rate * 8760.0 / hrs if rate is not None else None,
                }
            )
        if v in long_share:
            row["long_share"] = asof(long_share[v], t_close, LEVEL_MAX_AGE_MS)
        if v in liquidations:
            bars = [x for t, x in liquidations[v].items() if since_ms <= t < t_close]
            row["liq_long"] = sum(x[0] for x in bars)
            row["liq_short"] = sum(x[1] for x in bars)
        if row:
            out[v] = row
    return out


def round_tree(x: Any) -> Any:
    """Round floats for output: |x| >= 1000 to 1 decimal place (USD, coins, prices), else to 6 decimal places."""
    if isinstance(x, float):
        if not math.isfinite(x):
            return None
        return round(x, 1) if abs(x) >= 1000 else round(x, 6)
    if isinstance(x, dict):
        return {k: round_tree(v) for k, v in x.items()}
    if isinstance(x, list):
        return [round_tree(v) for v in x]
    return x
