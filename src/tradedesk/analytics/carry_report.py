"""Reporting over the paper engine's records (pure): closed-trade stats and cost realism."""

from __future__ import annotations

from typing import Any


def closed_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Stats over closed engine trade records (``return_on_notional`` / ``return_on_capital`` are decimal fractions)."""
    n = len(trades)
    if not n:
        return {"n": 0}
    rets = sorted(t["return_on_notional"] for t in trades)
    median = rets[n // 2] if n % 2 else (rets[n // 2 - 1] + rets[n // 2]) / 2
    return {
        "n": n,
        "mean_return_on_notional": sum(rets) / n,
        "median_return_on_notional": median,
        "mean_return_on_capital": sum(t["return_on_capital"] for t in trades) / n,
        "share_losing": sum(1 for t in trades if t["return_on_notional"] < 0) / n,
        "share_stopped": sum(1 for t in trades if t.get("stopped")) / n,
        "mean_days": sum(t["days"] for t in trades) / n,
        "funding_usd": sum(t["funding_usd"] for t in trades),
        "price_or_basis_usd": sum(t["price_usd"] for t in trades),
        "costs_usd": sum(t["costs_usd"] for t in trades),
        "pnl_usd": sum(t["pnl_usd"] for t in trades),
    }


def cost_realism(sides: list[dict[str, Any]]) -> dict[str, Any]:
    """Actual vs modelled cost per executed leg side (registered criterion (b); the unit is a leg side, not a trade).

    ``ratio`` = mean actual / mean modelled over sides with a usable book whose depth covered the slot notional;
    ``ratio_p90`` is the 90th percentile of the per-side actual / modelled ratio. Sides where the recorded depth did
    not cover the notional are reported separately under ``thin_book`` (their actual cost is on the available depth
    only and would understate the ratio).
    """
    total = len(sides)
    usable = [x for x in sides if x.get("actual_cost") is not None]
    if not usable:
        return {"sides": total, "with_book": 0}
    full = [x for x in usable if not x.get("depth_short")]
    thin = [x for x in usable if x.get("depth_short")]

    def block(xs: list[dict[str, Any]]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        actual = sum(x["actual_cost"] for x in xs) / len(xs)
        model = sum(x["model_cost"] for x in xs) / len(xs)
        per = sorted(x["actual_cost"] / x["model_cost"] for x in xs if x["model_cost"])
        return {
            "n": len(xs),
            "mean_actual_cost": actual,
            "mean_model_cost": model,
            "ratio": actual / model if model else None,
            "ratio_p90": per[min(int(0.9 * len(per)), len(per) - 1)] if per else None,
            "mean_spread_bps": sum(x["spread_bps"] for x in xs) / len(xs),
        }

    main = block(full)
    return {
        "sides": total,
        "with_book": len(usable),
        **{k: v for k, v in main.items() if k != "n"},
        "n_full_depth": main["n"],
        "share_depth_short": len(thin) / len(usable),
        "thin_book": block(thin),
    }
