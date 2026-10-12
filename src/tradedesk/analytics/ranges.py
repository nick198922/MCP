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


def classify_ranges(
    bars: list[dict[str, Any]],
    *,
    min_bars: int = 20,
    max_width_pct: float = 10.0,
    swing_lookback: int = 3,
    sweep_wick_ratio: float = 0.5,
    max_deviation_bars: int = 3,
) -> dict[str, Any]:
    """End-to-end: ranges + swings + sweeps + deviations within the latest range."""
    swings = swing_points(bars, lookback=swing_lookback)
    ranges = detect_ranges(bars, min_bars=min_bars, max_width_pct=max_width_pct)
    sweeps = detect_sweeps(bars, swings, wick_ratio=sweep_wick_ratio)
    deviations_per_range: list[list[dict[str, Any]]] = []
    for rng in ranges:
        deviations_per_range.append(detect_deviations(bars, rng, max_bars_back=max_deviation_bars))
    return {
        "n_bars": len(bars),
        "swings": swings,
        "ranges": ranges,
        "sweeps": sweeps,
        "deviations_per_range": deviations_per_range,
    }
