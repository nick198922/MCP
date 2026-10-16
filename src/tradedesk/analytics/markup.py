"""Chart markup: the levels and zones the DoubleTop course draws by hand, computed from OHLCV.

Everything here is pure math on bars ``{ts, open, high, low, close, volume}`` ordered oldest-first (``ts`` in ms,
UTC). Definitions follow the course (docs/knowledge/smc.md, indicators.md):

* swings are 3-candle fractals (the middle high above both neighbours' highs, lows mirror);
* a break of structure (BOS) needs a candle CLOSE beyond the swing; a wick beyond it with the close back inside is a
  sweep ("fake BOS", liquidity taken) — the swing is then spent either way;
* the course's structure uses structural swings only (``swing_structure``): in an uptrend the HL is the pullback low a
  new HH was made from, and the trend changes only on a close below it; every-fractal breaks (``structure``) are the
  internal structure (substructure);
* FVG/imbalance: candle 3's low above candle 1's high (bullish) or candle 3's high below candle 1's low (bearish);
  balanced at its midpoint (0.5) or fully filled;
* order block: a candle engulfed by the next candle of the opposite colour; the course's validity checks are reported
  as flags (impulsive engulf, FVG after it, BOS after it, in discount/premium);
* equal highs/lows: two untaken swings within a small tolerance — a liquidity pool;
* premium/discount and OTE (0.618-0.786) on the leg of the latest BOS;
* EMAs: Trend 13/21/34 on the chart timeframe, "Daily" EMA 100/200 on 4h, "Weekly" EMA 100/200 on 1d;
* anchored VWAP (day/week/month/quarter/year) with a 1-sigma band, volume profile POC/VAH/VAL;
* Asian session range and whether the Frankfurt/London open swept it (setup S25).

These are descriptive levels, not signals: our tests of mechanical SMC rules found no edge after costs.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

OTE_LEVELS = (0.5, 0.618, 0.705, 0.786)


# ---------- moving averages and oscillators ----------


def ema(values: list[float], n: int) -> list[float | None]:
    """Exponential moving average seeded with the simple average of the first ``n`` values (TradingView-style)."""
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def last_ema(values: list[float], n: int) -> float | None:
    series = ema(values, n)
    return series[-1] if series else None


def atr(bars: list[dict[str, Any]], n: int = 14) -> float | None:
    """Wilder's average true range of the last ``n`` bars."""
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, lo, pc = float(bars[i]["high"]), float(bars[i]["low"]), float(bars[i - 1]["close"])
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    value = sum(trs[:n]) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Wilder's RSI (0-100)."""
    if len(closes) < n + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g, avg_l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, lo in zip(gains[n:], losses[n:], strict=True):
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + lo) / n
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def ema_levels(price: float, tf_closes: list[float], h4_closes: list[float] | None, d1_closes: list[float] | None) -> dict[str, Any]:
    """The course's three EMA groups and where the price stands against them."""

    def group(closes: list[float] | None, periods: tuple[int, ...]) -> dict[str, Any] | None:
        if not closes:
            return None
        vals = {f"ema{p}": last_ema(closes, p) for p in periods}
        known = [v for v in vals.values() if v is not None]
        if not known:
            return None
        above = sum(1 for v in known if price > v)
        side = "above_all" if above == len(known) else "below_all" if above == 0 else "inside"
        return {**vals, "price_vs": side}

    return {
        "trend_13_21_34": group(tf_closes, (13, 21, 34)),
        "daily_4h_100_200": group(h4_closes, (100, 200)),
        "weekly_1d_100_200": group(d1_closes, (100, 200)),
    }


# ---------- VWAP and volume profile ----------


def anchor_start(ts_ms: int, anchor: str) -> datetime:
    """UTC start of the day/week (Monday)/month/quarter/year containing ``ts_ms``."""
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if anchor == "day":
        return day
    if anchor == "week":
        return day - timedelta(days=day.weekday())
    if anchor == "month":
        return day.replace(day=1)
    if anchor == "quarter":
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)
    if anchor == "year":
        return day.replace(month=1, day=1)
    raise ValueError(f"unknown anchor '{anchor}'")


def anchored_vwap(bars: list[dict[str, Any]], anchor: str = "quarter") -> dict[str, Any] | None:
    """Volume-weighted typical price ((H+L+C)/3) since the start of the anchor period of the last bar, with a
    volume-weighted 1-sigma band. None when the period has no volume in ``bars``."""
    if not bars:
        return None
    start_ms = int(anchor_start(int(bars[-1]["ts"]), anchor).timestamp() * 1000)
    seg = [b for b in bars if int(b["ts"]) >= start_ms]
    vol = sum(float(b.get("volume") or 0.0) for b in seg)
    if vol <= 0:
        return None
    tps = [(float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0 for b in seg]
    vols = [float(b.get("volume") or 0.0) for b in seg]
    vwap = sum(tp * v for tp, v in zip(tps, vols, strict=True)) / vol
    var = sum(v * (tp - vwap) ** 2 for tp, v in zip(tps, vols, strict=True)) / vol
    sd = math.sqrt(var)
    return {
        "anchor": anchor,
        "start_ms": start_ms,
        "bars": len(seg),
        "vwap": vwap,
        "sd": sd,
        "upper_1sd": vwap + sd,
        "lower_1sd": vwap - sd,
    }


def volume_profile(bars: list[dict[str, Any]], bins: int = 40, value_area: float = 0.70) -> dict[str, Any] | None:
    """Fixed-range volume profile: each bar's volume spread evenly over its high-low range. Returns the point of
    control (bin centre with most volume) and the value area (~``value_area`` of volume, grown from the POC towards
    the larger neighbouring bin)."""
    if not bars:
        return None
    lo = min(float(b["low"]) for b in bars)
    hi = max(float(b["high"]) for b in bars)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    hist = [0.0] * bins
    for b in bars:
        v = float(b.get("volume") or 0.0)
        bl, bh = float(b["low"]), float(b["high"])
        if v <= 0:
            continue
        if bh <= bl:
            hist[min(int((bl - lo) / step), bins - 1)] += v
            continue
        for k in range(bins):
            a, z = lo + k * step, lo + (k + 1) * step
            overlap = min(bh, z) - max(bl, a)
            if overlap > 0:
                hist[k] += v * overlap / (bh - bl)
    total = sum(hist)
    if total <= 0:
        return None
    poc = max(range(bins), key=lambda k: hist[k])
    left, right, acc = poc, poc, hist[poc]
    while acc < value_area * total and (left > 0 or right < bins - 1):
        down = hist[left - 1] if left > 0 else -1.0
        up = hist[right + 1] if right < bins - 1 else -1.0
        if up >= down:
            right += 1
            acc += up
        else:
            left -= 1
            acc += down
    return {
        "poc": lo + (poc + 0.5) * step,
        "vah": lo + (right + 1) * step,
        "val": lo + left * step,
        "range_low": lo,
        "range_high": hi,
        "bins": bins,
    }


# ---------- swings, structure, liquidity ----------


def fractals(bars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """3-candle swing points: high strictly above both neighbours' highs; low strictly below both neighbours' lows."""
    highs, lows = [], []
    for i in range(1, len(bars) - 1):
        h, lo = float(bars[i]["high"]), float(bars[i]["low"])
        if h > float(bars[i - 1]["high"]) and h > float(bars[i + 1]["high"]):
            highs.append({"idx": i, "ts": bars[i]["ts"], "price": h})
        if lo < float(bars[i - 1]["low"]) and lo < float(bars[i + 1]["low"]):
            lows.append({"idx": i, "ts": bars[i]["ts"], "price": lo})
    return {"highs": highs, "lows": lows}


def structure(bars: list[dict[str, Any]], swings: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Walk the bars and resolve every swing once: a later CLOSE beyond it is a break of structure (``bos_up`` /
    ``bos_down``); a wick beyond it with the close back inside is a ``sweep_high`` / ``sweep_low`` (fake BOS). A swing
    is known from the close of the bar after it. Unresolved swings are still-standing liquidity."""
    pending_h = sorted(swings["highs"], key=lambda s: s["idx"])
    pending_l = sorted(swings["lows"], key=lambda s: s["idx"])
    active_h: list[dict[str, Any]] = []
    active_l: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for t, bar in enumerate(bars):
        while pending_h and pending_h[0]["idx"] + 2 <= t:
            active_h.append(pending_h.pop(0))
        while pending_l and pending_l[0]["idx"] + 2 <= t:
            active_l.append(pending_l.pop(0))
        h, lo, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
        # One event per bar and kind: the furthest swing closed beyond (BOS) and the furthest only wicked (sweep).
        taken_h = [s for s in active_h if h > s["price"]]
        for kind, group in (("bos_up", [s for s in taken_h if c > s["price"]]), ("sweep_high", [s for s in taken_h if c <= s["price"]])):
            if group:
                far = max(group, key=lambda s: s["price"])
                events.append(
                    {"type": kind, "idx": t, "ts": bar["ts"], "level": far["price"], "swing_idx": far["idx"], "swings": len(group)}
                )
        taken_l = [s for s in active_l if lo < s["price"]]
        for kind, group in (("bos_down", [s for s in taken_l if c < s["price"]]), ("sweep_low", [s for s in taken_l if c >= s["price"]])):
            if group:
                far = min(group, key=lambda s: s["price"])
                events.append(
                    {"type": kind, "idx": t, "ts": bar["ts"], "level": far["price"], "swing_idx": far["idx"], "swings": len(group)}
                )
        active_h = [s for s in active_h if s not in taken_h]
        active_l = [s for s in active_l if s not in taken_l]
    active_h.extend(pending_h)
    active_l.extend(pending_l)
    bos = [e for e in events if e["type"].startswith("bos")]
    trend = None if not bos else ("up" if bos[-1]["type"] == "bos_up" else "down")
    return {"events": events, "trend": trend, "last_bos": bos[-1] if bos else None, "untaken_highs": active_h, "untaken_lows": active_l}


def swing_structure(bars: list[dict[str, Any]], swings: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Market structure on structural swings, as the course draws it (docs/knowledge/market-structure.md).

    The first close beyond a known 3-candle swing sets the trend. In an uptrend the running high is the HH; once a
    swing low has formed after it (a pullback), a close above the HH is a continuation BOS and the lowest low of that
    pullback becomes the protected HL. A close below the protected HL is a change of character (``choch_down``): the
    trend turns down, the last HH becomes the protected high and the running low the LL. Wicks beyond the protected
    level or beyond the HH after a pullback with the close back inside are sweeps (fake BOS). A new high without a
    pullback only extends the HH. Downtrends mirror. A swing is known from the close of the bar after it."""
    highs = sorted(swings["highs"], key=lambda s: s["idx"])
    lows = sorted(swings["lows"], key=lambda s: s["idx"])
    hi_i = lo_i = 0
    last_hi: dict[str, Any] | None = None
    last_lo: dict[str, Any] | None = None
    trend: str | None = None
    prot: dict[str, Any] = {}
    ext: dict[str, Any] = {}
    pulled = False
    events: list[dict[str, Any]] = []

    def event(kind: str, t: int, level: float) -> None:
        events.append({"type": kind, "idx": t, "ts": bars[t]["ts"], "level": level})

    def extreme(lo: int, hi: int, side: str) -> dict[str, Any]:
        seg = range(lo, hi + 1)
        i = min(seg, key=lambda j: float(bars[j]["low"])) if side == "low" else max(seg, key=lambda j: float(bars[j]["high"]))
        return {"price": float(bars[i][side]), "idx": i, "ts": bars[i]["ts"]}

    for t, bar in enumerate(bars):
        while hi_i < len(highs) and highs[hi_i]["idx"] + 2 <= t:
            last_hi, hi_i = highs[hi_i], hi_i + 1
        while lo_i < len(lows) and lows[lo_i]["idx"] + 2 <= t:
            last_lo, lo_i = lows[lo_i], lo_i + 1
        h, lo, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if trend is None:
            if last_hi and c > last_hi["price"]:
                trend, prot, ext, pulled = "up", extreme(last_hi["idx"], t, "low"), {"price": h, "idx": t, "ts": bar["ts"]}, False
                event("bos_up", t, last_hi["price"])
            elif last_lo and c < last_lo["price"]:
                trend, prot, ext, pulled = "down", extreme(last_lo["idx"], t, "high"), {"price": lo, "idx": t, "ts": bar["ts"]}, False
                event("bos_down", t, last_lo["price"])
            continue
        up = trend == "up"
        # The protected level: a close beyond it changes the trend, a wick beyond it is a sweep.
        if (c < prot["price"]) if up else (c > prot["price"]):
            event("choch_down" if up else "choch_up", t, prot["price"])
            top = ext if not up or ext["price"] >= h else {"price": h, "idx": t, "ts": bar["ts"]}
            bottom = ext if up or ext["price"] <= lo else {"price": lo, "idx": t, "ts": bar["ts"]}
            trend = "down" if up else "up"
            prot, ext = (top, {"price": lo, "idx": t, "ts": bar["ts"]}) if up else (bottom, {"price": h, "idx": t, "ts": bar["ts"]})
            pulled = False
            continue
        if (lo < prot["price"]) if up else (h > prot["price"]):
            event("sweep_low" if up else "sweep_high", t, prot["price"])
        swing = last_lo if up else last_hi
        pulled = pulled or bool(swing and swing["idx"] > ext["idx"])
        beyond = h > ext["price"] if up else lo < ext["price"]
        if not beyond:
            continue
        if not pulled:
            ext = {"price": h if up else lo, "idx": t, "ts": bar["ts"]}
        elif (c > ext["price"]) if up else (c < ext["price"]):
            event("bos_up" if up else "bos_down", t, ext["price"])
            prot = extreme(ext["idx"] + 1, t, "low" if up else "high")
            ext, pulled = {"price": h if up else lo, "idx": t, "ts": bar["ts"]}, False
        else:
            event("sweep_high" if up else "sweep_low", t, ext["price"])
    breaks = [e for e in events if not e["type"].startswith("sweep")]
    labels = ("HL", "HH") if trend == "up" else ("LH", "LL")
    return {
        "trend": trend,
        "protected": {**prot, "label": labels[0]} if trend else None,
        "extreme": {**ext, "label": labels[1]} if trend else None,
        "in_pullback": pulled,
        "events": events,
        "last_break": breaks[-1] if breaks else None,
    }


def equal_levels(untaken: list[dict[str, Any]], tol: float) -> list[dict[str, Any]]:
    """Pairs (or clusters) of untaken swings within ``tol`` of each other: equal highs/lows, a liquidity pool."""
    pts = sorted(untaken, key=lambda s: s["price"])
    pools: list[dict[str, Any]] = []
    i = 0
    while i < len(pts):
        j = i
        while j + 1 < len(pts) and pts[j + 1]["price"] - pts[i]["price"] <= tol:
            j += 1
        if j > i:
            group = pts[i : j + 1]
            pools.append(
                {"low": group[0]["price"], "high": group[-1]["price"], "touches": len(group), "last_idx": max(g["idx"] for g in group)}
            )
        i = j + 1
    return pools


# ---------- imbalances and order blocks ----------


def fvgs(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every three-candle fair value gap with its later state: ``open`` (untouched), ``partial`` (entered, 0.5 not
    reached), ``balanced`` (0.5 reached), ``filled`` (traded through)."""
    out = []
    for i in range(2, len(bars)):
        h1, l1 = float(bars[i - 2]["high"]), float(bars[i - 2]["low"])
        h3, l3 = float(bars[i]["high"]), float(bars[i]["low"])
        if l3 > h1:
            gap = {"type": "bull", "bottom": h1, "top": l3}
        elif h3 < l1:
            gap = {"type": "bear", "bottom": h3, "top": l1}
        else:
            continue
        mid = (gap["top"] + gap["bottom"]) / 2
        state = "open"
        for b in bars[i + 1 :]:
            bl, bh = float(b["low"]), float(b["high"])
            if gap["type"] == "bull":
                if bl <= gap["bottom"]:
                    state = "filled"
                    break
                if bl <= mid:
                    state = "balanced"
                elif bl < gap["top"] and state == "open":
                    state = "partial"
            else:
                if bh >= gap["top"]:
                    state = "filled"
                    break
                if bh >= mid:
                    state = "balanced"
                elif bh > gap["bottom"] and state == "open":
                    state = "partial"
        out.append({**gap, "mid": mid, "idx": i - 1, "ts": bars[i - 1]["ts"], "state": state})
    return out


def order_blocks(
    bars: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    events: list[dict[str, Any]],
    atr_value: float | None,
    *,
    bos_window: int = 20,
) -> list[dict[str, Any]]:
    """Candle ``i`` engulfed by candle ``i+1`` of the opposite colour (close beyond candle i's open). Bullish OB: a
    bearish candle engulfed by a bullish one; the zone is the whole candle (wicks included), the body is the main
    reference. Flags (the course's validity rules): ``impulsive`` (engulfing body >= 1 ATR), ``fvg_after`` (a same-side
    FVG formed by candle i+1 or i+2), ``bos_after`` (a same-side close-BOS within ``bos_window`` bars). State: ``untested``,
    ``tested`` (price came back into the zone), ``broken`` (closed beyond the far side: now a breaker candidate). For a
    broken block, the course's breaker checks: ``break_impulsive`` (the breaking candle crossed the whole zone or its body
    is >= 1 ATR) and ``fvg_on_break`` (the break left an opposite-side FVG overlapping the zone)."""
    out = []
    fvg_by_mid = {(g["idx"], g["type"]) for g in gaps}
    for i in range(len(bars) - 1):
        o1, c1 = float(bars[i]["open"]), float(bars[i]["close"])
        o2, c2 = float(bars[i + 1]["open"]), float(bars[i + 1]["close"])
        if c1 < o1 and c2 > o2 and c2 > o1:
            side = "bull"
        elif c1 > o1 and c2 < o2 and c2 < o1:
            side = "bear"
        else:
            continue
        zone_lo, zone_hi = float(bars[i]["low"]), float(bars[i]["high"])
        body_lo, body_hi = min(o1, c1), max(o1, c1)
        impulsive = atr_value is not None and abs(c2 - o2) >= atr_value
        fvg_after = any((k, side) in fvg_by_mid for k in (i + 1, i + 2))
        bos_type = "bos_up" if side == "bull" else "bos_down"
        bos_after = any(e["type"] == bos_type and i + 1 <= e["idx"] <= i + bos_window for e in events)
        state, broke_at = "untested", None
        for k in range(i + 2, len(bars)):
            bl, bh, bc = float(bars[k]["low"]), float(bars[k]["high"]), float(bars[k]["close"])
            if side == "bull":
                if bc < zone_lo:
                    state, broke_at = "broken", k
                    break
                if bl <= zone_hi:
                    state = "tested"
            else:
                if bc > zone_hi:
                    state, broke_at = "broken", k
                    break
                if bh >= zone_lo:
                    state = "tested"
        break_impulsive = fvg_on_break = False
        if broke_at is not None:
            bo, bc = float(bars[broke_at]["open"]), float(bars[broke_at]["close"])
            crossed = bo >= zone_hi if side == "bull" else bo <= zone_lo
            break_impulsive = crossed or (atr_value is not None and abs(bc - bo) >= atr_value)
            other = "bear" if side == "bull" else "bull"
            fvg_on_break = any(
                g["type"] == other and broke_at - 1 <= g["idx"] <= broke_at + 1 and g["bottom"] < zone_hi and g["top"] > zone_lo
                for g in gaps
            )
        out.append(
            {
                "type": side,
                "idx": i,
                "ts": bars[i]["ts"],
                "zone_low": zone_lo,
                "zone_high": zone_hi,
                "body_low": body_lo,
                "body_high": body_hi,
                "impulsive": impulsive,
                "fvg_after": fvg_after,
                "bos_after": bos_after,
                "score": int(impulsive) + int(fvg_after) + int(bos_after),
                "state": state,
                "break_impulsive": break_impulsive,
                "fvg_on_break": fvg_on_break,
            }
        )
    return out


# ---------- dealing range, premium/discount, sessions ----------


def dealing_range(bars: list[dict[str, Any]], last_bos: dict[str, Any] | None) -> dict[str, Any] | None:
    """The leg of the latest close-BOS: for a bullish break, from the lowest low between the broken swing and the
    break up to the highest high since; bearish mirrors. Fibonacci 0 sits at the leg's end (high for a bullish leg),
    1 at its start; above 0.5 is premium, below is discount."""
    if not last_bos:
        return None
    k, s = int(last_bos["idx"]), int(last_bos["swing_idx"])
    if last_bos["type"] == "bos_up":
        low = min(float(b["low"]) for b in bars[s : k + 1])
        high = max(float(b["high"]) for b in bars[k:])
    else:
        high = max(float(b["high"]) for b in bars[s : k + 1])
        low = min(float(b["low"]) for b in bars[k:])
    return fib_leg(low, high, "up" if last_bos["type"] == "bos_up" else "down", float(bars[-1]["close"]))


def swing_dealing_range(bars: list[dict[str, Any]], swing: dict[str, Any]) -> dict[str, Any] | None:
    """The structural leg: from the protected HL to the HH in an uptrend (LH to LL in a downtrend)."""
    if not swing.get("trend"):
        return None
    a, b = float(swing["protected"]["price"]), float(swing["extreme"]["price"])
    return fib_leg(min(a, b), max(a, b), swing["trend"], float(bars[-1]["close"]))


def fib_leg(low: float, high: float, direction: str, price: float) -> dict[str, Any]:
    """Fibonacci on a leg: 0 at its end (the high of an up leg), 1 at its start; above 0.5 is premium."""
    levels = {str(f): high - f * (high - low) if direction == "up" else low + f * (high - low) for f in OTE_LEVELS}
    eq = (high + low) / 2
    zone = "premium" if price > eq else "discount" if price < eq else "equilibrium"
    ote_lo, ote_hi = sorted((levels["0.618"], levels["0.786"]))
    return {
        "direction": direction,
        "low": low,
        "high": high,
        "levels": levels,
        "price_zone": zone,
        "in_ote": ote_lo <= price <= ote_hi,
    }


def asia_session(bars: list[dict[str, Any]], now_ms: int) -> dict[str, Any] | None:
    """Asian range of the UTC day of ``now_ms`` (00:00 UTC to the Frankfurt open, 08:00 Europe/Berlin) and whether the
    hunt window (Frankfurt open to London open + 3 h, 08:00 Europe/London) swept either side (setup S25)."""
    day = datetime.fromtimestamp(now_ms / 1000, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    local_day = day.date()
    frankfurt = datetime(local_day.year, local_day.month, local_day.day, 8, tzinfo=ZoneInfo("Europe/Berlin")).astimezone(UTC)
    london = datetime(local_day.year, local_day.month, local_day.day, 8, tzinfo=ZoneInfo("Europe/London")).astimezone(UTC)
    a0, a1 = int(day.timestamp() * 1000), int(frankfurt.timestamp() * 1000)
    w1 = int((london + timedelta(hours=3)).timestamp() * 1000)
    asia = [b for b in bars if a0 <= int(b["ts"]) < a1]
    if not asia:
        return None
    hi = max(float(b["high"]) for b in asia)
    lo = min(float(b["low"]) for b in asia)
    hunt = [b for b in bars if a1 <= int(b["ts"]) < w1]
    swept_high = any(float(b["high"]) > hi for b in hunt)
    swept_low = any(float(b["low"]) < lo for b in hunt)
    return {
        "asia_high": hi,
        "asia_low": lo,
        "asia_from_ms": a0,
        "frankfurt_open_ms": a1,
        "london_open_ms": int(london.timestamp() * 1000),
        "hunt_until_ms": w1,
        "hunt_bars": len(hunt),
        "swept_high": swept_high,
        "swept_low": swept_low,
    }


def nearest(levels: list[dict[str, Any]], price: float, side: str, n: int = 5) -> list[dict[str, Any]]:
    """Up to ``n`` swings above (``side='above'``) or below the price, closest first."""
    pick = [s for s in levels if (s["price"] > price if side == "above" else s["price"] < price)]
    return sorted(pick, key=lambda s: abs(s["price"] - price))[:n]
