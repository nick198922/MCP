"""Simple market-regime classifier: UP / DOWN / CHOP.

Uses two signals computed over the last ``lookback`` bars:

* **slope** — OLS fit of ``log(close)`` on bar index, in log-points per bar.
  Positive slope = up-trend, negative = down.
* **volatility** — stdev of per-bar log returns.

Trend-strength is ``|slope| * sqrt(N) / vol`` (a Sharpe-shaped number) — large
means the price moved monotonically; small means the trend is lost in noise.

Thresholds are deliberately simple and tunable:

* ``trend_strength >= trend_threshold`` → ``up`` if slope > 0 else ``down``
* otherwise ``chop``

Default ``trend_threshold`` is 0.5, which treats "about half a σ of signal per
window" as the boundary; crank it higher to only flag very clean trends. The
helper is pure, so callers can replay it against any OHLCV series.
"""

from __future__ import annotations

import math
from typing import Any


def _ols_slope(ys: list[float]) -> float:
    """Slope of a least-squares line y = a + b*x on x = 0..N-1."""
    n = len(ys)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(ys) / n
    num = 0.0
    den = 0.0
    for i, y in enumerate(ys):
        dx = i - x_mean
        num += dx * (y - y_mean)
        den += dx * dx
    return num / den if den > 0 else 0.0


def classify_regime(
    bars: list[dict[str, Any]],
    *,
    lookback: int = 100,
    trend_threshold: float = 0.5,
) -> dict[str, Any]:
    """Return the regime block over the trailing ``lookback`` bars.

    ``bars`` are dicts with ``close``; ordered oldest-first. When fewer than
    ``lookback`` bars are available we fall back to whatever is there (and
    report the effective sample).
    """
    if not bars:
        return {"regime": "unknown", "count": 0}
    rows = bars[-lookback:]
    closes = [float(b["close"]) for b in rows if b.get("close") is not None and float(b["close"]) > 0]
    n = len(closes)
    if n < 3:
        return {"regime": "unknown", "count": n}
    log_closes = [math.log(c) for c in closes]
    slope = _ols_slope(log_closes)
    rets = [log_closes[i + 1] - log_closes[i] for i in range(n - 1)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    vol = math.sqrt(var)
    trend_strength = (abs(slope) * math.sqrt(n) / vol) if vol > 0 else 0.0
    if trend_strength >= trend_threshold:
        regime = "up" if slope > 0 else "down"
    else:
        regime = "chop"
    return {
        "regime": regime,
        "count": n,
        "slope_per_bar_log": slope,
        "slope_pct_per_bar": (math.exp(slope) - 1.0) * 100.0,
        "stdev_bar_log": vol,
        "trend_strength": trend_strength,
        "trend_threshold": trend_threshold,
        "window_return_pct": (closes[-1] / closes[0] - 1.0) * 100.0,
        "first_ts": rows[0].get("ts"),
        "last_ts": rows[-1].get("ts"),
        "first_close": closes[0],
        "last_close": closes[-1],
    }
