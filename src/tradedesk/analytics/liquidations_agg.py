"""Pure helpers for liquidation history (Binance symbols) (Coinalyze `/liquidation-history`). No I/O.

Coinalyze rows are `{"t": unix_s, "l": long_liquidations, "s": short_liquidations}`; units are base-asset
contracts/coins, or USD when the request set `convert_to_usd=true`. A long liquidation is a forced SELL of a long
position; a short liquidation is a forced BUY.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

INTERVALS = {"daily": 86_400, "hourly": 3_600}
_API_INTERVAL = {"daily": "daily", "hourly": "1hour"}
_BASE_OK = re.compile(r"^[A-Z0-9]{1,15}$")


def api_interval(interval: str) -> str:
    if interval not in _API_INTERVAL:
        raise ValueError(f"interval must be one of {sorted(_API_INTERVAL)}")
    return _API_INTERVAL[interval]


def aggregated_symbol(base: str, margin: str = "usdt") -> str | None:
    """Coinalyze perpetual symbol on Binance (the '.A' suffix is the exchange code of Binance, NOT an all-exchange
    aggregate; checked 2026-10-18 against Binance OI/funding/long-short), e.g. BTC -> BTCUSDT_PERP.A. None if invalid."""
    b = (base or "").strip().upper()
    m = (margin or "").strip().upper()
    if not _BASE_OK.match(b) or m not in ("USDT", "USD"):
        return None
    return f"{b}{m}_PERP.A"


def summarise(history: list[dict[str, Any]], interval: str, *, limit: int | None = None) -> dict[str, Any] | None:
    """Rows -> {rows, totals, peak}. Rows are sorted by time, malformed ones dropped; `limit` keeps the latest N.

    long_share is long / (long + short) in [0, 1] (None when both are 0); net = long - short.
    """
    step = INTERVALS[interval]
    rows: list[dict[str, Any]] = []
    for h in history or []:
        try:
            t = int(h["t"])
            lg = float(h["l"])
            sh = float(h["s"])
        except (KeyError, TypeError, ValueError):
            continue
        if lg < 0 or sh < 0:
            continue
        tot = lg + sh
        rows.append(
            {
                "t": datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds"),
                "ts": t,
                "long": lg,
                "short": sh,
                "total": tot,
                "net": lg - sh,
                "long_share": (lg / tot) if tot > 0 else None,
            }
        )
    rows.sort(key=lambda r: r["ts"])
    if not rows:
        return None
    if limit is not None and limit > 0:
        rows = rows[-limit:]
    long_sum = sum(r["long"] for r in rows)
    short_sum = sum(r["short"] for r in rows)
    peak = max(rows, key=lambda r: r["total"])
    gaps = sum(1 for a, b in zip(rows, rows[1:], strict=False) if b["ts"] - a["ts"] > step)
    return {
        "rows": rows,
        "totals": {
            "long": long_sum,
            "short": short_sum,
            "total": long_sum + short_sum,
            "net": long_sum - short_sum,
            "long_share": (long_sum / (long_sum + short_sum)) if (long_sum + short_sum) > 0 else None,
            "intervals": len(rows),
            "gaps": gaps,
        },
        "peak": {"t": peak["t"], "total": peak["total"], "long": peak["long"], "short": peak["short"]},
    }
