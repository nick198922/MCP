"""Metric functions exposed to the rules DSL.

Each function hits the local SQLite store and returns a float or ``None``.
The DSL evaluator never raises on ``None`` — the rule simply becomes
``None`` / falsy and the alert does not fire. All lookups accept the
same ``MetricContext`` which closes over the ``Store`` and ``now_ts``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import Store


@dataclass
class MetricContext:
    store: "Store"
    now_ts: int

    def _since(self, days: float) -> int:
        return self.now_ts - int(days * 86_400)


async def _latest_from_series(series: list[dict[str, Any]], key: str) -> float | None:
    if not series:
        return None
    v = series[-1].get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _window_values(series: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in series:
        v = row.get(key)
        try:
            if v is not None:
                out.append(float(v))
        except (TypeError, ValueError):
            continue
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

    async def funding(venue: str, base: str) -> float | None:
        series = await ctx.store.funding_series(venue, base, 0, ctx.now_ts)
        return await _latest_from_series(series, "annualized")

    async def funding_mean(venue: str, base: str, days: float) -> float | None:
        series = await ctx.store.funding_series(venue, base, ctx._since(days), ctx.now_ts)
        return _mean(_window_values(series, "annualized"))

    async def funding_zscore(venue: str, base: str, days: float) -> float | None:
        series = await ctx.store.funding_series(venue, base, ctx._since(days), ctx.now_ts)
        xs = _window_values(series, "annualized")
        latest = xs[-1] if xs else None
        return _zscore(xs, latest)

    async def atm_iv(currency: str, expiry: str) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "atm_iv")

    async def atm_iv_mean(currency: str, expiry: str, days: float) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, ctx._since(days), ctx.now_ts)
        return _mean(_window_values(series, "atm_iv"))

    async def atm_iv_zscore(currency: str, expiry: str, days: float) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, ctx._since(days), ctx.now_ts)
        xs = _window_values(series, "atm_iv")
        latest = xs[-1] if xs else None
        return _zscore(xs, latest)

    async def rr25(currency: str, expiry: str) -> float | None:
        series = await ctx.store.atm_term_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "rr25")

    async def basis(currency: str, expiry: str) -> float | None:
        series = await ctx.store.basis_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "basis_pct")

    async def basis_annualized(currency: str, expiry: str) -> float | None:
        series = await ctx.store.basis_series(currency, expiry, 0, ctx.now_ts)
        return await _latest_from_series(series, "annualized_simple")

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
