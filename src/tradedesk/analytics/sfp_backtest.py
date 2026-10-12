"""Walk-forward verification of the DoubleTop SFP mechanic.

At every closed bar ``t`` we run exactly what ``range_setup_scan`` would have
seen at that moment (trailing window ending at ``t``, nothing after it). When
the last bar of that window is a fresh sweep inside the latest range, we open
a bracket trade at the sweep close and walk forward bar by bar.

Conservative fills: a bar that touches both stop and target counts as a stop;
a bar that gaps through the stop exits at its open. Fees are charged as a
round-trip percentage of entry and converted to R.
"""

from __future__ import annotations

import math
from typing import Any

from .ranges import classify_ranges, detect_ranges, generate_setup

# 1× overshoot puts the stop exactly at the wick extreme, i.e. the same as "tight".
STOP_VARIANTS: dict[str, tuple[float, bool]] = {
    "tight": (0.0, True),
    "m2_2x": (2.0, False),
    "m2_3x": (3.0, False),
}


def simulate_bracket(
    bars: list[dict[str, Any]],
    start_idx: int,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    max_hold_bars: int,
) -> dict[str, Any] | None:
    """Walk bars from ``start_idx`` until stop, target or timeout. ``None`` if no bars follow."""
    end = min(start_idx + max_hold_bars, len(bars))
    if start_idx >= end:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    best = entry
    for k in range(start_idx, end):
        b = bars[k]
        op, hi, lo = float(b["open"]), float(b["high"]), float(b["low"])
        if direction == "long":
            best = max(best, hi)
            hit_stop, hit_tp = lo <= stop, hi >= target
            stop_fill = min(op, stop)
        else:
            best = min(best, lo)
            hit_stop, hit_tp = hi >= stop, lo <= target
            stop_fill = max(op, stop)
        if hit_stop:
            outcome, exit_price = "stop", stop_fill
        elif hit_tp:
            outcome, exit_price = "target", target
        else:
            continue
        return {
            "outcome": outcome,
            "both_in_bar": hit_stop and hit_tp,
            "exit_idx": k,
            "exit_price": exit_price,
            "bars_held": k - start_idx + 1,
            "r": sign * (exit_price - entry) / risk,
            "mfe_pct": sign * (best - entry) / entry * 100.0,
        }
    last = bars[end - 1]
    exit_price = float(last["close"])
    return {
        "outcome": "timeout",
        "both_in_bar": False,
        "exit_idx": end - 1,
        "exit_price": exit_price,
        "bars_held": end - start_idx,
        "r": sign * (exit_price - entry) / risk,
        "mfe_pct": sign * (best - entry) / entry * 100.0,
    }


def walk_forward_sfp(
    bars: list[dict[str, Any]],
    *,
    window: int = 300,
    min_bars: int = 20,
    max_width_pct: float = 15.0,
    swing_lookback: int = 3,
    sweep_wick_ratio: float = 0.5,
    stop_min_offset_pct: float = 0.1,
    max_hold_bars: int = 60,
    fee_pct: float = 0.1,
    one_position: bool = True,
) -> list[dict[str, Any]]:
    """Every fresh in-range sweep becomes a signal evaluated under all STOP_VARIANTS.

    With ``one_position`` new signals are skipped while the 2× trade is still open.
    """
    signals: list[dict[str, Any]] = []
    busy_until = -1
    n = len(bars)
    for t in range(window - 1, n - 1):
        if one_position and t <= busy_until:
            continue
        win = bars[t - window + 1 : t + 1]
        res = classify_ranges(
            win,
            min_bars=min_bars,
            max_width_pct=max_width_pct,
            swing_lookback=swing_lookback,
            sweep_wick_ratio=sweep_wick_ratio,
        )
        if not res["ranges"]:
            continue
        rng = res["ranges"][-1]
        in_rng = [s for s in res["sweeps"] if rng["start_idx"] <= s["idx"] <= rng["end_idx"]]
        if not in_rng or in_rng[-1]["idx"] != len(win) - 1:
            continue
        sweep = in_rng[-1]
        cls = res["classifications"][-1]
        variants: dict[str, Any] = {}
        for name, (mult, tight) in STOP_VARIANTS.items():
            setup = generate_setup(rng, sweep, stop_mult=mult, stop_min_offset_pct=stop_min_offset_pct, min_rr=0.0, use_tight_stop=tight)
            if setup is None:
                continue
            sim = simulate_bracket(bars, t + 1, setup["direction"], setup["entry"], setup["stop"], setup["tp1"], max_hold_bars)
            if sim is None:
                continue
            fee_r = fee_pct / setup["risk_pct_from_entry"] if setup["risk_pct_from_entry"] else 0.0
            variants[name] = {
                "entry": setup["entry"],
                "stop": setup["stop"],
                "tp1": setup["tp1"],
                "rr1": setup["rr1"],
                "risk_pct": setup["risk_pct_from_entry"],
                **sim,
                "r_net": sim["r"] - fee_r,
            }
        if "m2_2x" not in variants:
            continue
        base = variants["m2_2x"]
        busy_until = base["exit_idx"]
        signals.append(
            {
                "idx": t,
                "ts": bars[t].get("ts"),
                "side": sweep["side"],
                "direction": "short" if sweep["side"] == "high" else "long",
                "overshoot_pct": sweep["overshoot_pct"],
                "swept_level": sweep["swept_level"],
                "range_width_pct": rng["width_pct"],
                "classification": cls["type"],
                "context": cls["signals"]["context"],
                "same_side_in_last_5": sum(1 for s in in_rng[-5:] if s["side"] == sweep["side"]),
                "variants": variants,
            }
        )
    return signals


def ltf_entry(
    bars: list[dict[str, Any]],
    start_idx: int,
    direction: str,
    level: float,
    stop: float,
    target: float,
    mode: str,
    wait_bars: int = 16,
) -> dict[str, Any]:
    """Find a lower-timeframe entry after a higher-timeframe sweep candle closed at bars[start_idx].ts.

    Modes (DoubleTop «Торговля SFP»: the sweep is read on 4h/12h, the entry taken on 15m):
    * ``htf_close`` — market entry at the open of the first lower-TF bar.
    * ``retest`` — resting limit at the swept level for ``wait_bars``. A bar that fills and
      also reaches the stop is a loss; a target touched before the fill cancels the order.
    * ``confirm`` — enter at the close of the first bar that closes beyond the previous bar's
      extreme in the trade direction while on the inside of the level. A stop or target
      touched first cancels.

    Returns ``{"entry_price", "exit_from", "stopped_in_fill_bar"}`` or ``{"skip": reason}``.
    """
    n = len(bars)
    if start_idx >= n:
        return {"skip": "no_bars"}
    short = direction == "short"
    end = min(start_idx + wait_bars, n)
    if mode == "htf_close":
        price = float(bars[start_idx]["open"])
        if (short and price >= stop) or (not short and price <= stop):
            return {"skip": "entry_beyond_stop"}
        if (short and price <= target) or (not short and price >= target):
            return {"skip": "entry_beyond_target"}
        return {"entry_price": price, "exit_from": start_idx, "stopped_in_fill_bar": False}
    if mode == "retest":
        for k in range(start_idx, end):
            b = bars[k]
            op, hi, lo = float(b["open"]), float(b["high"]), float(b["low"])
            filled = hi >= level if short else lo <= level
            if not filled:
                if (short and lo <= target) or (not short and hi >= target):
                    return {"skip": "target_before_fill"}
                continue
            price = max(op, level) if short else min(op, level)
            stopped = hi >= stop if short else lo <= stop
            return {"entry_price": price, "exit_from": k + 1, "stopped_in_fill_bar": stopped, "fill_idx": k}
        return {"skip": "no_fill"}
    if mode == "confirm":
        for k in range(max(start_idx, 1), end):
            b, prev = bars[k], bars[k - 1]
            hi, lo, cl = float(b["high"]), float(b["low"]), float(b["close"])
            if (short and hi >= stop) or (not short and lo <= stop):
                return {"skip": "stop_before_confirm"}
            if (short and lo <= target) or (not short and hi >= target):
                return {"skip": "target_before_confirm"}
            if short and cl < float(prev["low"]) and cl < level:
                return {"entry_price": cl, "exit_from": k + 1, "stopped_in_fill_bar": False}
            if not short and cl > float(prev["high"]) and cl > level:
                return {"entry_price": cl, "exit_from": k + 1, "stopped_in_fill_bar": False}
        return {"skip": "no_confirm"}
    raise ValueError(f"unknown mode {mode!r}")


def summarise_r(rs: list[float], outcomes: list[str]) -> dict[str, Any]:
    """Expectancy block: n, win rate (target hits), mean R with a normal-approx 95 % CI."""
    n = len(rs)
    if n == 0:
        return {"n": 0, "small_sample": True}
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else float("nan")
    gains = sum(x for x in rs if x > 0)
    losses = -sum(x for x in rs if x < 0)
    srt = sorted(rs)
    return {
        "n": n,
        "win_rate": sum(1 for o in outcomes if o == "target") / n,
        "stop_rate": sum(1 for o in outcomes if o == "stop") / n,
        "timeout_rate": sum(1 for o in outcomes if o == "timeout") / n,
        "avg_r": mean,
        "median_r": srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2,
        "ci95": (mean - 1.96 * se, mean + 1.96 * se) if n > 1 else None,
        "profit_factor": gains / losses if losses > 0 else None,
        "small_sample": n < 20,
    }


def summarise_signals(signals: list[dict[str, Any]], variant: str = "m2_2x", *, net: bool = True) -> dict[str, Any]:
    rows = [s["variants"][variant] for s in signals if variant in s["variants"]]
    key = "r_net" if net else "r"
    out = summarise_r([v[key] for v in rows], [v["outcome"] for v in rows])
    if rows:
        out["avg_rr1"] = sum(v["rr1"] for v in rows) / len(rows)
        out["breakeven_win_rate"] = sum(1 / (1 + v["rr1"]) for v in rows) / len(rows)
    return out


def range_time_share(bars: list[dict[str, Any]], *, min_bars: int = 20, max_width_pct: float = 10.0) -> float:
    """Share of bars covered by a detected range (the "70 % of the time" claim)."""
    if not bars:
        return 0.0
    covered = [False] * len(bars)
    for r in detect_ranges(bars, min_bars=min_bars, max_width_pct=max_width_pct):
        for i in range(r["start_idx"], r["end_idx"] + 1):
            covered[i] = True
    return sum(covered) / len(bars)
