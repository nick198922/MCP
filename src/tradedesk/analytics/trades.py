"""Recent-trades flow analytics: aggressor-signed volume, VWAP, CVD, large prints."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _f(x: Any) -> float | None:
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def summarise(trades: Iterable[Mapping[str, Any]], *, top_n: int = 5) -> dict[str, Any]:
    """Aggregate a ccxt-shaped trades list.

    Each trade must expose ``price``, ``amount`` (base units) and ``side`` ('buy'|'sell'),
    which in ccxt is the aggressor side (the taker). ``cost`` is used when present, else price*amount.
    All notionals returned here are in the quote currency the exchange reports (USD/USDT in practice).
    """
    rows: list[dict[str, Any]] = []
    for t in trades:
        price = _f(t.get("price"))
        amount = _f(t.get("amount"))
        if price is None or amount is None or amount <= 0 or price <= 0:
            continue
        side = str(t.get("side") or "").lower()
        if side not in ("buy", "sell"):
            continue
        cost = _f(t.get("cost"))
        if cost is None:
            cost = price * amount
        ts = t.get("timestamp")
        rows.append({"ts": int(ts) if isinstance(ts, (int, float)) else None, "side": side, "price": price, "amount": amount, "cost": cost})

    if not rows:
        return {"count": 0}

    buy = [r for r in rows if r["side"] == "buy"]
    sell = [r for r in rows if r["side"] == "sell"]
    buy_base = sum(r["amount"] for r in buy)
    sell_base = sum(r["amount"] for r in sell)
    buy_usd = sum(r["cost"] for r in buy)
    sell_usd = sum(r["cost"] for r in sell)
    tot_base = buy_base + sell_base
    tot_usd = buy_usd + sell_usd
    tot_cost = sum(r["cost"] for r in rows)
    tot_amount = sum(r["amount"] for r in rows)

    ts_vals = [r["ts"] for r in rows if r["ts"] is not None]
    first_ts = min(ts_vals) if ts_vals else None
    last_ts = max(ts_vals) if ts_vals else None

    cvd: list[list[int | float]] = []
    running = 0.0
    for r in rows:
        signed = r["cost"] if r["side"] == "buy" else -r["cost"]
        running += signed
        if r["ts"] is not None:
            cvd.append([r["ts"], running])

    biggest = sorted(rows, key=lambda r: r["cost"], reverse=True)[:top_n]
    top = [{"ts": b["ts"], "side": b["side"], "price": b["price"], "amount": b["amount"], "cost": b["cost"]} for b in biggest]

    return {
        "count": len(rows),
        "buy_count": len(buy),
        "sell_count": len(sell),
        "buy_base": buy_base,
        "sell_base": sell_base,
        "delta_base": buy_base - sell_base,
        "buy_notional": buy_usd,
        "sell_notional": sell_usd,
        "delta_notional": buy_usd - sell_usd,
        "imbalance": (buy_usd - sell_usd) / tot_usd if tot_usd else None,
        "vwap": tot_cost / tot_amount if tot_amount else None,
        "buy_vwap": buy_usd / buy_base if buy_base else None,
        "sell_vwap": sell_usd / sell_base if sell_base else None,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "duration_ms": (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else None,
        "top_trades": top,
        "cvd_end_notional": running,
        "cvd_series": cvd,
        "base_volume": tot_base,
        "quote_volume": tot_usd,
    }
