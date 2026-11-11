"""Metric functions exposed to the rules DSL.

Each function hits the local SQLite store and returns a float or ``None``.
The DSL evaluator never raises on ``None`` — the rule simply becomes
``None`` / falsy and the alert does not fire. All lookups accept the
same ``MetricContext`` which closes over the ``Store`` and ``now_ts``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import Store


@dataclass
class MetricContext:
    store: "Store"
    now_ts: int
    # Stored values a metric could not read as a number (text / blob left by another writer). The metric still returns
    # None / skips the row, but rules_test turns these notes into warnings and the alerts job logs them (item 300).
    notes: list[str] = field(default_factory=list)

    def _since(self, days: float) -> int:
        return self.now_ts - int(days * 86_400)


async def _latest_from_series(series: list[dict[str, Any]], key: str, notes: list[str] | None = None, label: str = "") -> float | None:
    if not series:
        return None
    v = series[-1].get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        if notes is not None:  # the stored text itself is not echoed: untrusted, and may be huge
            notes.append(f"{label}: the latest stored `{key}` is not a number ({type(v).__name__}); read as no value")
        return None


def _window_values(series: list[dict[str, Any]], key: str, notes: list[str] | None = None, label: str = "") -> list[float]:
    out: list[float] = []
    bad = 0
    last_bad = False
    for row in series:
        v = row.get(key)
        last_bad = False
        try:
            if v is not None:
                out.append(float(v))
        except (TypeError, ValueError):
            bad += 1
            last_bad = True
    if bad and notes is not None:
        last = " (the latest one among them)" if last_bad else ""
        notes.append(f"{label}: {bad} of {len(series)} stored `{key}` values in the window are not numbers and were skipped{last}")
    return out


def _mean(xs: list[float]) -> float | None:
    return (sum(xs) / len(xs)) if xs else None


def _zscore(xs: list[float], latest: float | None) -> float | None:
    if latest is None or len(xs) < 2:
        return None
    m = _mean(xs)
    if m is None:
        return None
    var = sum((v - m) ** 2 for v in xs) / (len(xs) - 1)
    sd = math.sqrt(var)
    return (latest - m) / sd if sd > 0 else None


def build_metrics(ctx: MetricContext) -> dict[str, Callable[..., Any]]:
    """Return the DSL call-table bound to this ``MetricContext``."""

    notes = ctx.notes

    async def funding(venue: str, base: str) -> float | None:
        series = await ctx.store.funding_series(venue, base, 0, ctx.now_ts)
        return await _latest_from_series(series, "annualized", notes, f"funding({venue!r}, {base!r})")

    async def funding_mean(venue: str, base: str, days: float) -> float | None:
        series = await ctx.store.funding_series(venue, base, ctx._since(days), ctx.now_ts)
        return _mean(_window_values(series, "annualized", notes, f"funding_mean({venue!r}, {base!r})"))

    async def funding_zscore(venue: str, base: str, days: float) -> float | None:
        series = await ctx.store.funding_series(venue, base, ctx._since(days), ctx.now_ts)
        xs = _window_values(series, "annualized", notes, f"funding_zscore({venue!r}, {base!r})")
        latest = xs[-1] if xs else None
        return _zscore(xs, latest)

    async def atm_iv(currency: str, expiry: str) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "atm_iv", notes, f"atm_iv({currency!r}, {expiry!r})")

    async def atm_iv_mean(currency: str, expiry: str, days: float) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, ctx._since(days), ctx.now_ts)
        return _mean(_window_values(series, "atm_iv", notes, f"atm_iv_mean({currency!r}, {expiry!r})"))

    async def atm_iv_zscore(currency: str, expiry: str, days: float) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, ctx._since(days), ctx.now_ts)
        xs = _window_values(series, "atm_iv", notes, f"atm_iv_zscore({currency!r}, {expiry!r})")
        latest = xs[-1] if xs else None
        return _zscore(xs, latest)

    async def rr25(currency: str, expiry: str) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "rr25", notes, f"rr25({currency!r}, {expiry!r})")

    async def basis(currency: str, expiry: str) -> float | None:
        series = await ctx.store.basis_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "basis_pct", notes, f"basis({currency!r}, {expiry!r})")

    async def basis_annualized(currency: str, expiry: str) -> float | None:
        series = await ctx.store.basis_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "annualized_simple", notes, f"basis_annualized({currency!r}, {expiry!r})")

    async def liquidations_total(window_minutes: float, venue: str | None = None, symbol: str | None = None) -> float | None:
        since = ctx.now_ts - int(window_minutes * 60)
        rows = await ctx.store.liquidations_in_window(since_ts=since, until_ts=ctx.now_ts, venue=venue, symbol=symbol)
        if not rows:
            return 0.0
        return sum(float(r["notional_usd"]) for r in rows)

    async def liquidations_imbalance(window_minutes: float, venue: str | None = None, symbol: str | None = None) -> float | None:
        since = ctx.now_ts - int(window_minutes * 60)
        rows = await ctx.store.liquidations_in_window(since_ts=since, until_ts=ctx.now_ts, venue=venue, symbol=symbol)
        if not rows:
            return None
        buys = sum(float(r["notional_usd"]) for r in rows if r["side"] == "buy")
        sells = sum(float(r["notional_usd"]) for r in rows if r["side"] == "sell")
        total = buys + sells
        return ((buys - sells) / total) if total else None

    return {
        "funding": funding,
        "funding_mean": funding_mean,
        "funding_zscore": funding_zscore,
        "atm_iv": atm_iv,
        "atm_iv_mean": atm_iv_mean,
        "atm_iv_zscore": atm_iv_zscore,
        "rr25": rr25,
        "basis": basis,
        "basis_annualized": basis_annualized,
        "liquidations_total": liquidations_total,
        "liquidations_imbalance": liquidations_imbalance,
        "abs": abs,
        "min": min,
        "max": max,
    }


# Public list of metric names (for `rules_test`/help output).
METRICS = (
    "funding(venue, base)",
    "funding_mean(venue, base, days)",
    "funding_zscore(venue, base, days)",
    "atm_iv(currency, expiry)",
    "atm_iv_mean(currency, expiry, days)",
    "atm_iv_zscore(currency, expiry, days)",
    "rr25(currency, expiry)",
    "basis(currency, expiry)",
    "basis_annualized(currency, expiry)",
    "liquidations_total(window_minutes, venue=None, symbol=None)",
    "liquidations_imbalance(window_minutes, venue=None, symbol=None)",
    "abs / min / max",
)
