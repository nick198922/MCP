"""Pure helpers for liquidation history (Coinalyze `/liquidation-history`, per-exchange symbols). No I/O.

Coinalyze rows are `{"t": unix_s, "l": long_liquidations, "s": short_liquidations}`; units are base-asset
contracts/coins, or USD when the request set `convert_to_usd=true`. A long liquidation is a forced SELL of a long
position; a short liquidation is a forced BUY.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

INTERVALS = {"daily": 86_400, "hourly": 3_600}
_API_INTERVAL = {"daily": "daily", "hourly": "1hour"}
_BASE_OK = re.compile(r"^[A-Z0-9]{1,15}$")
# Coinalyze exchange codes (symbol suffix), each verified by matching Coinalyze OI with the venue's live OI:
# '.A' = Binance (docs/reviews/2026-10-18-doubletop-parity-1b.md, 2026-10-25), '.6' = Bybit (BTCUSDT.6, 2026-10-25,
# docs/reviews/2026-10-25-doubletop-parity-2.md). Only these are accepted; nothing else reaches the URL.
EXCHANGE_CODES = {"binance": ".A", "bybit": ".6"}
_EXCHANGE_ALIASES = {"binance": "binance", ".a": "binance", "a": "binance", "bybit": "bybit", ".6": "bybit", "6": "bybit"}
TS_MIN = 1_230_768_000  # 2009-01-01T00:00:00Z: no crypto derivatives data before this
TS_MAX = 4_102_444_800  # 2100-01-01T00:00:00Z
# Everything a malformed untrusted row can raise while being parsed (int(inf) -> OverflowError, fromtimestamp -> OSError).
ROW_ERRORS = (KeyError, TypeError, ValueError, OverflowError, OSError)


def parse_ts(x: Any) -> tuple[int, str]:
    """Untrusted unix-seconds value -> (ts, ISO-8601 UTC). Raises one of ROW_ERRORS when it is not a number, not
    finite, or outside [2009, 2100), so callers drop the row instead of failing."""
    t = int(x)
    if not TS_MIN <= t < TS_MAX:
        raise ValueError("timestamp out of range")
    return t, datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


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


def parse_exchanges(values: Sequence[Any] | None) -> tuple[list[str], list[str]]:
    """Requested exchanges -> (known exchange names in request order without repeats, rejected values).

    Accepts names ('binance', 'bybit') or Coinalyze codes ('.A', '.6', with or without the dot), case-insensitive;
    None means ['binance']. Rejected values are returned truncated to 20 characters (for the error message only).
    """
    if values is None:
        return ["binance"], []
    names: list[str] = []
    bad: list[str] = []
    for v in values:
        name = _EXCHANGE_ALIASES.get(str(v).strip().lower())
        if name is None:
            bad.append(str(v)[:20])
        elif name not in names:
            names.append(name)
    return names, bad


def exchange_symbol(base: str, margin: str, exchange: str) -> str | None:
    """Coinalyze perpetual symbol of `base` on a known exchange, or None if the ticker is invalid or the pair is not
    verified. Binance: BTCUSDT_PERP.A / BTCUSD_PERP.A; Bybit: BTCUSDT.6 (USDT-margined only; the coin-margined Bybit
    symbol format is not verified, so it is refused)."""
    if exchange == "binance":
        return aggregated_symbol(base, margin)
    if exchange == "bybit":
        b = (base or "").strip().upper()
        m = (margin or "").strip().upper()
        if not _BASE_OK.match(b) or m != "USDT":
            return None
        return f"{b}USDT.6"
    return None


def summarise(history: list[dict[str, Any]], interval: str, *, limit: int | None = None) -> dict[str, Any] | None:
    """Rows -> {rows, totals, peak}. Rows are sorted by time, malformed ones dropped; `limit` keeps the latest N.

    long_share is long / (long + short) in [0, 1] (None when both are 0); net = long - short.
    """
    step = INTERVALS[interval]
    rows: list[dict[str, Any]] = []
    for h in history or []:
        try:
            t, iso = parse_ts(h["t"])
            lg = float(h["l"])
            sh = float(h["s"])
        except ROW_ERRORS:
            continue
        if lg < 0 or sh < 0:
            continue
        tot = lg + sh
        rows.append(
            {
                "t": iso,
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
