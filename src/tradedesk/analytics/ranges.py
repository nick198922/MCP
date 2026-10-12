"""Range / sweep / deviation detectors.

Pattern from the DoubleTop "Рейнджі" video: in a sideways market, price
oscillates between two horizontal boundaries. We detect those boundaries,
mark the swing points inside them, and flag two short-lived events:

* **sweep** — a candle whose wick pokes above a prior swing high (or below a
  swing low) but closes back on the original side. Interpreted as "liquidity
  grab" before a reversal.
* **deviation** — price closes beyond the range boundary but is pulled back
  into the range within a few bars. A failed breakout.

Everything is pure math on a list of OHLCV dicts
``{ts, open, high, low, close, volume}`` ordered oldest-first.
"""

from __future__ import annotations

from typing import Any


def swing_points(bars: list[dict[str, Any]], lookback: int = 3) -> dict[str, list[dict[str, Any]]]:
    """Local swing-high / swing-low pivots.

    A bar is a swing high if its ``high`` is >= every high in the ``lookback``
    bars on each side; swing low mirrors with ``low``. Edges of the series
    cannot be pivots (not enough context on one side).
    """
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    n = len(bars)
    if n < 2 * lookback + 1:
        return {"highs": highs, "lows": lows}
    for i in range(lookback, n - lookback):
        hi = float(bars[i]["high"])
        lo = float(bars[i]["low"])
        is_hi = all(hi >= float(bars[j]["high"]) for j in range(i - lookback, i + lookback + 1) if j != i)
        is_lo = all(lo <= float(bars[j]["low"]) for j in range(i - lookback, i + lookback + 1) if j != i)
        if is_hi:
            highs.append({"idx": i, "ts": bars[i].get("ts"), "price": hi})
        if is_lo:
            lows.append({"idx": i, "ts": bars[i].get("ts"), "price": lo})
    return {"highs": highs, "lows": lows}


def detect_ranges(
    bars: list[dict[str, Any]],
    *,
    min_bars: int = 20,
    max_width_pct: float = 10.0,
) -> list[dict[str, Any]]:
    """Rolling-window range detection.

    A window of >= ``min_bars`` consecutive bars qualifies as a range when
    ``(max(high) - min(low)) / mid <= max_width_pct / 100``. Overlapping or
    adjacent windows get merged into one maximal range so a long sideways
    stretch becomes a single entry.
    """
    n = len(bars)
    if n < min_bars:
        return []
    candidates: list[tuple[int, int]] = []
    i = 0
    while i <= n - min_bars:
        # Check the initial `min_bars` window first; only grow if it fits.
        first = bars[i : i + min_bars]
        hi0 = max(float(b["high"]) for b in first)
        lo0 = min(float(b["low"]) for b in first)
        mid0 = (hi0 + lo0) / 2.0
        if mid0 <= 0 or (hi0 - lo0) / mid0 * 100.0 > max_width_pct:
            i += 1
            continue
        j = i + min_bars
        while j < n:
            window = bars[i : j + 1]
            hi = max(float(b["high"]) for b in window)
            lo = min(float(b["low"]) for b in window)
            mid = (hi + lo) / 2.0
            if mid <= 0 or (hi - lo) / mid * 100.0 > max_width_pct:
                break
            j += 1
        candidates.append((i, j - 1))
        i = j
    # Merge touching / overlapping candidates.
    merged: list[tuple[int, int]] = []
    for a, b in candidates:
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    out: list[dict[str, Any]] = []
    for a, b in merged:
        window = bars[a : b + 1]
        hi = max(float(x["high"]) for x in window)
        lo = min(float(x["low"]) for x in window)
        mid = (hi + lo) / 2.0
        width_pct = (hi - lo) / mid * 100.0 if mid > 0 else 0.0
        out.append(
            {
                "start_idx": a,
                "end_idx": b,
                "start_ts": window[0].get("ts"),
                "end_ts": window[-1].get("ts"),
                "n_bars": b - a + 1,
                "top": hi,
                "bottom": lo,
                "mid": mid,
                "width_pct": width_pct,
            }
        )
    return out


def detect_sweeps(
    bars: list[dict[str, Any]],
    swings: dict[str, list[dict[str, Any]]],
    *,
    wick_ratio: float = 0.5,
    reversal_min_pct: float = 0.0,
) -> list[dict[str, Any]]:
    """Candles that poke past a prior swing level and close back on the
    original side (a "liquidity grab").

    ``wick_ratio`` is the min ratio of the piercing wick to the TOTAL range
    of the candle; higher = demand longer wicks. ``reversal_min_pct`` is an
    optional threshold on `(level - close) / close` (long sweep) — set > 0
    to require some immediate reversal strength.
    """
    out: list[dict[str, Any]] = []
    swing_highs = sorted(swings["highs"], key=lambda s: s["idx"])
    swing_lows = sorted(swings["lows"], key=lambda s: s["idx"])
    highs_prices = [s["price"] for s in swing_highs]
    highs_idx = [s["idx"] for s in swing_highs]
    lows_prices = [s["price"] for s in swing_lows]
    lows_idx = [s["idx"] for s in swing_lows]
    for i, b in enumerate(bars):
        hi = float(b["high"])
        lo = float(b["low"])
        op = float(b["open"])
        cl = float(b["close"])
        rng = hi - lo
        if rng <= 0:
            continue
        upper_wick = hi - max(op, cl)
        lower_wick = min(op, cl) - lo
        # Long sweep (sweep of a prior high and close back below).
        prior_highs = [p for p, idx in zip(highs_prices, highs_idx, strict=False) if idx < i]
        if prior_highs:
            last_high = prior_highs[-1]
            if hi > last_high and cl < last_high and upper_wick / rng >= wick_ratio:
                rev_pct = (last_high - cl) / cl * 100.0 if cl > 0 else 0.0
                if rev_pct >= reversal_min_pct * 100.0:
                    out.append(
                        {
                            "ts": b.get("ts"),
                            "idx": i,
                            "side": "high",
                            "swept_level": last_high,
                            "candle_high": hi,
                            "candle_close": cl,
                            "reversal_pct": rev_pct,
                        }
                    )
        prior_lows = [p for p, idx in zip(lows_prices, lows_idx, strict=False) if idx < i]
        if prior_lows:
            last_low = prior_lows[-1]
            if lo < last_low and cl > last_low and lower_wick / rng >= wick_ratio:
                rev_pct = (cl - last_low) / cl * 100.0 if cl > 0 else 0.0
                if rev_pct >= reversal_min_pct * 100.0:
                    out.append(
                        {
                            "ts": b.get("ts"),
                            "idx": i,
                            "side": "low",
                            "swept_level": last_low,
                            "candle_low": lo,
                            "candle_close": cl,
                            "reversal_pct": rev_pct,
                        }
                    )
    return out


def detect_deviations(
    bars: list[dict[str, Any]],
    rng: dict[str, Any],
    *,
    max_bars_back: int = 3,
) -> list[dict[str, Any]]:
    """Failed breakouts of a single range.

    A deviation is a bar whose ``close`` is above ``top`` (or below ``bottom``)
    but within ``max_bars_back`` the price returns inside the range (any bar's
    close back within [bottom, top]).
    """
    out: list[dict[str, Any]] = []
    top = float(rng["top"])
    bottom = float(rng["bottom"])
    start, end = int(rng["start_idx"]), int(rng["end_idx"])
    for i in range(start, min(end + 1, len(bars))):
        cl = float(bars[i]["close"])
        side: str | None = None
        if cl > top:
            side = "high"
        elif cl < bottom:
            side = "low"
        if side is None:
            continue
        back_in_range = False
        for j in range(i + 1, min(i + 1 + max_bars_back, len(bars))):
            cj = float(bars[j]["close"])
            if bottom <= cj <= top:
                back_in_range = True
                out.append(
                    {
                        "breakout_idx": i,
                        "breakout_ts": bars[i].get("ts"),
                        "side": side,
                        "breakout_close": cl,
                        "bars_back_in_range": j - i,
                        "returned_at_ts": bars[j].get("ts"),
                    }
                )
                break
        if not back_in_range:
            continue
    return out


def _ols_slope(xs: list[float], ys: list[float]) -> float:
    """Slope of y = a + b*x via least-squares; 0 if under-constrained."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0
    xm = sum(xs) / n
    ym = sum(ys) / n
    num = sum((xs[i] - xm) * (ys[i] - ym) for i in range(n))
    den = sum((xs[i] - xm) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def _sma(bars: list[dict[str, Any]], end_idx: int, n: int) -> float | None:
    """Simple moving average over the n closes ending at end_idx (inclusive)."""
    if end_idx + 1 < n:
        return None
    window = bars[end_idx - n + 1 : end_idx + 1]
    return sum(float(b["close"]) for b in window) / n


def range_type_hint(
    bars: list[dict[str, Any]],
    rng: dict[str, Any],
    swings: dict[str, list[dict[str, Any]]],
    sweeps: list[dict[str, Any]],
    *,
    sweep_asymmetry_threshold: float = 0.65,
) -> dict[str, Any]:
    """Vote-based classification of a range as accumulation / distribution.

    Three criteria from DoubleTop «Аккумуляція / Дистрибуція»:
    * **context**: SMA100 vs SMA200 at the range's end — above → accumulation
      candidate, below → distribution.
    * **wedge**: slope of swing highs vs swing lows inside the range —
      expanding-up wedge (bottom flat, top rising) → distribution; expanding-
      down (top flat, bottom falling) → accumulation.
    * **sweep asymmetry**: share of upper sweeps inside the range — above
      threshold → distribution; below (1 − threshold) → accumulation.

    Each criterion casts one vote. If votes agree → that label with high
    confidence; split → "ambiguous" (the lecture's warning: ambiguous ranges
    that showed upper-sweep hunts were the ones that flushed in Oct 2025).
    """
    start_idx, end_idx = int(rng["start_idx"]), int(rng["end_idx"])
    reasons: list[str] = []
    votes = {"accumulation": 0, "distribution": 0}

    # 1) SMA context.
    sma100 = _sma(bars, end_idx, 100)
    sma200 = _sma(bars, end_idx, 200)
    context_label = None
    if sma100 is not None and sma200 is not None:
        if sma100 > sma200:
            context_label = "accumulation"
            votes["accumulation"] += 1
        elif sma100 < sma200:
            context_label = "distribution"
            votes["distribution"] += 1
        reasons.append(f"SMA100={sma100:.2f} vs SMA200={sma200:.2f} → {context_label or 'flat'}")
    else:
        reasons.append("SMA100/200 unavailable (need ≥200 bars before range end)")

    # 2) Wedge slopes on swings inside the range.
    highs_in = [s for s in swings["highs"] if start_idx <= s["idx"] <= end_idx]
    lows_in = [s for s in swings["lows"] if start_idx <= s["idx"] <= end_idx]
    top_slope = _ols_slope([float(s["idx"]) for s in highs_in], [float(s["price"]) for s in highs_in])
    bot_slope = _ols_slope([float(s["idx"]) for s in lows_in], [float(s["price"]) for s in lows_in])
    wedge_label = None
    # Normalise slopes by mid price for comparability.
    mid = float(rng["mid"])
    top_slope_pct = (top_slope / mid * 100.0) if mid > 0 else 0.0
    bot_slope_pct = (bot_slope / mid * 100.0) if mid > 0 else 0.0
    # Distribution wedge: top rising while bottom roughly flat (|top| > 2× |bottom|, top > 0).
    if abs(top_slope_pct) > abs(bot_slope_pct) * 2 and top_slope_pct > 0:
        wedge_label = "distribution"
        votes["distribution"] += 1
    elif abs(bot_slope_pct) > abs(top_slope_pct) * 2 and bot_slope_pct < 0:
        wedge_label = "accumulation"
        votes["accumulation"] += 1
    reasons.append(f"wedge top_slope={top_slope_pct:+.4f}%/bar bottom_slope={bot_slope_pct:+.4f}%/bar → {wedge_label or 'symmetric'}")

    # 3) Sweep asymmetry.
    sweeps_in = [s for s in sweeps if start_idx <= s["idx"] <= end_idx]
    hi_sweeps = sum(1 for s in sweeps_in if s["side"] == "high")
    lo_sweeps = sum(1 for s in sweeps_in if s["side"] == "low")
    total = hi_sweeps + lo_sweeps
    sweep_label = None
    if total > 0:
        hi_share = hi_sweeps / total
        if hi_share >= sweep_asymmetry_threshold:
            sweep_label = "distribution"
            votes["distribution"] += 1
        elif hi_share <= 1 - sweep_asymmetry_threshold:
            sweep_label = "accumulation"
            votes["accumulation"] += 1
        reasons.append(f"sweeps high={hi_sweeps} low={lo_sweeps} ({hi_share:.0%} up) → {sweep_label or 'mixed'}")
    else:
        reasons.append("no sweeps inside range")

    # Verdict.
    acc, dist = votes["accumulation"], votes["distribution"]
    if acc > dist and acc >= 2:
        verdict, confidence = "accumulation", acc / 3.0
    elif dist > acc and dist >= 2:
        verdict, confidence = "distribution", dist / 3.0
    else:
        verdict, confidence = "ambiguous", max(acc, dist) / 3.0
    return {
        "type": verdict,
        "confidence": confidence,
        "votes": votes,
        "signals": {
            "context": context_label,
            "wedge": wedge_label,
            "sweep_asymmetry": sweep_label,
        },
        "metrics": {
            "sma100": sma100,
            "sma200": sma200,
            "top_slope_pct_per_bar": top_slope_pct,
            "bottom_slope_pct_per_bar": bot_slope_pct,
            "high_sweeps": hi_sweeps,
            "low_sweeps": lo_sweeps,
        },
        "reasons": reasons,
    }


def generate_setup(
    rng: dict[str, Any],
    sweep: dict[str, Any],
    *,
    stop_mult: float = 2.0,
    stop_min_offset_pct: float = 0.1,
    min_rr: float = 2.0,
    use_tight_stop: bool = False,
) -> dict[str, Any]:
    """Translate a sweep into a tradable entry / stop / targets block.

    Follows the DoubleTop «Торговля SFP» rules:
    * **direction**: long after a low-side sweep, short after a high-side sweep.
    * **entry**: at the swept level — i.e. where price retraced back to after
      the sweep. Simple approximation; a real client watches 15m price action.
    * **stop**: swept extreme ± `stop_mult × sweep.reversal_pct` (M2). Falls
      back to `stop_min_offset_pct` so a tiny sweep still gives breathing room.
      When `use_tight_stop` is True (after 2-3 confirmed sweeps, rule M7) the
      multiplier collapses to just the min offset — effectively "stop right at
      the swept level".
    * **tp1 / tp2**: mid of the range (M3) and the opposite boundary.
    * **rr1 / rr2**: reward-to-risk for each target.
    * **tradable**: True when `rr1 >= min_rr` (M4).

    Returns ``None`` when the sweep direction is incoherent with the range
    (sweep high but swept_level below the range top, etc.) — belt-and-braces.
    """
    top, bottom = float(rng["top"]), float(rng["bottom"])
    mid = (top + bottom) / 2.0
    side = sweep.get("side")
    level = float(sweep.get("swept_level") or 0.0)
    rev = float(sweep.get("reversal_pct") or 0.0)
    offset_pct = max(stop_min_offset_pct, stop_mult * rev) if not use_tight_stop else stop_min_offset_pct
    if side == "high":
        entry = level
        stop = level * (1 + offset_pct / 100.0)
        tp1 = mid
        tp2 = bottom
        direction = "short"
    elif side == "low":
        entry = level
        stop = level * (1 - offset_pct / 100.0)
        tp1 = mid
        tp2 = top
        direction = "long"
    else:
        return None
    risk = abs(stop - entry)
    reward1 = abs(tp1 - entry)
    reward2 = abs(tp2 - entry)
    if risk <= 0:
        return None
    # Flip reward signs if the sweep sits on the wrong side of the mid (incoherent).
    if direction == "short" and entry <= mid:
        return None
    if direction == "long" and entry >= mid:
        return None
    rr1 = reward1 / risk
    rr2 = reward2 / risk
    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "stop_offset_pct": offset_pct,
        "tp1": tp1,
        "tp2": tp2,
        "risk_abs": risk,
        "risk_pct_from_entry": risk / entry * 100.0 if entry > 0 else None,
        "rr1": rr1,
        "rr2": rr2,
        "tradable": rr1 >= min_rr,
        "min_rr": min_rr,
        "sweep_ts": sweep.get("ts"),
    }


def classify_ranges(
    bars: list[dict[str, Any]],
    *,
    min_bars: int = 20,
    max_width_pct: float = 10.0,
    swing_lookback: int = 3,
    sweep_wick_ratio: float = 0.5,
    max_deviation_bars: int = 3,
    sweep_asymmetry_threshold: float = 0.65,
) -> dict[str, Any]:
    """End-to-end: ranges + swings + sweeps + deviations + per-range classification."""
    swings = swing_points(bars, lookback=swing_lookback)
    ranges = detect_ranges(bars, min_bars=min_bars, max_width_pct=max_width_pct)
    sweeps = detect_sweeps(bars, swings, wick_ratio=sweep_wick_ratio)
    deviations_per_range: list[list[dict[str, Any]]] = []
    classifications: list[dict[str, Any]] = []
    for rng in ranges:
        deviations_per_range.append(detect_deviations(bars, rng, max_bars_back=max_deviation_bars))
        classifications.append(range_type_hint(bars, rng, swings, sweeps, sweep_asymmetry_threshold=sweep_asymmetry_threshold))
    return {
        "n_bars": len(bars),
        "swings": swings,
        "ranges": ranges,
        "sweeps": sweeps,
        "deviations_per_range": deviations_per_range,
        "classifications": classifications,
    }
