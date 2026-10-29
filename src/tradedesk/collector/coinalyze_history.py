"""Coinalyze daily history of BTC/ETH perps per venue into SQLite (table ``coinalyze_history``).

Series: open interest in USD (``convert_to_usd=true``), funding (decimal fraction per funding interval: Coinalyze sends
percent, divided by 100) and the long/short account ratio (ratio, long/short shares in [0, 1]), for ``BTCUSDT_PERP.A``
and ``ETHUSDT_PERP.A`` (Binance, the default) and, with ``TRADEDESK_COLLECT_COINALYZE_HISTORY_VENUES=binance,bybit``,
also ``BTCUSDT.6`` and ``ETHUSDT.6`` (Bybit USDT perps). The suffix is the exchange code in Coinalyze, NOT an
all-exchange aggregate (``.A`` = Binance, docs/reviews/2026-10-18-doubletop-parity-1b.md; ``.6`` = Bybit,
docs/reviews/2026-10-25-doubletop-parity-2.md, both by matching OI with the live venue). Only codes of that verified
whitelist (``EXCHANGE_CODES``) are asked; other venue names in the setting are ignored with a warning. Each venue is
stored under its own symbol, so the rows never mix: summing venues is left to the reader. For Bybit only OI was checked
against the venue; its funding and long/short rows are stored as Coinalyze sends them (same parsers and units as
Binance), not yet compared with Bybit's own history. Network only through ``rt.coinalyze`` (the shared ``Http``: rate
limit, retries, health); the API key lives in the request header only and is never logged or stored.

Only closed UTC days are stored, and a day is first asked :data:`SETTLE_SECONDS` after its close (02:00 UTC of the
next day), so a bar Coinalyze still finishes in the first minutes after midnight is not frozen half-built: the job
never re-reads a stored day. A repeated bar is rewritten in place (PRIMARY KEY (symbol, metric, ts)). Each run asks
at most :data:`CHUNK_DAYS` days per series, resuming the day after the last stored bar; with nothing stored it starts
``backfill_days`` ago (at least 1; never older than the retention window when one is set). Per venue 2 symbols x 3
metrics = at most 6 requests per run (12 with Bybit), all through the provider's 40/min limiter, and none once the
series is up to date (the next one goes out after 02:00 UTC). A venue switched on later backfills on its own: each
series resumes from its own last stored bar.

An empty answer is logged as a warning every time. When the empty window lies wholly in the past (it ends before the
last settled day) an in-memory cursor moves past it, so a range Coinalyze has no data for is not asked again until a
restart. An empty window that reaches the last settled day does NOT move the cursor: for BTC/ETH perps missing
recent days are a fault (wrong symbol, changed response shape), not the edge of history, so it is retried every run
and keeps warning.

Caveats for readers of the table: funding ``open``/``low`` of a day may carry the previous day's last rate (use
``close`` = the day's last settlement, ``high`` = its largest); the long/short row is the account snapshot at ``ts``
(inferred, not independently verified); OI in USD is Coinalyze's conversion. ``convert_to_usd`` has not yet been
checked with a live call of our key: an OI series whose MEDIAN close looks like coins (below :data:`MIN_OI_USD`) is
refused and lands in ``missing`` rather than being stored as USD; single bars below it in a USD series (a zero or a
gap filled by the aggregator) are dropped with a warning and the rest is stored.

Off by default: TRADEDESK_COLLECT_COINALYZE_HISTORY_ENABLED=1 (and the collector); without COINALYZE_API_KEY the job
logs one warning and does nothing.
"""

from __future__ import annotations

import logging
import statistics
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..analytics.coinalyze_series import FUNDING_PCT_TO_DECIMAL, long_short_series, ohlc_series
from ..analytics.liquidations_agg import aggregated_symbol, api_interval, exchange_symbol, parse_exchanges
from ..core.envelope import Builder, Fetched

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

DAY = 86_400
CHUNK_DAYS = 120  # per series per run; the tools read up to 365 daily bars in one request, so this is well inside
BASES = ("BTC", "ETH")
SYMBOLS = tuple(aggregated_symbol(b) or "" for b in BASES)  # BTCUSDT_PERP.A, ETHUSDT_PERP.A (Binance, the default)
METRICS = ("oi_usd", "funding", "long_short")
# Binance and Bybit BTC/ETH perp OI is billions of USD; in coins it is ~1e5 BTC / ~2e6 ETH (Bybit BTC ~6e4 BTC on
# 2026-10-25). A median close below this is not USD.
MIN_OI_USD = 1e7
# A closed day is first asked this long after 00:00 UTC, so Coinalyze has finished the bar (it is never re-read).
SETTLE_SECONDS = 2 * 3600


def venue_symbols(venues: str) -> tuple[list[str], list[str]]:
    """(Coinalyze symbols to collect, rejected venue values) of the comma-separated ``venues`` setting.

    Names or codes of the verified whitelist only ('binance' / '.A', 'bybit' / '.6', via ``parse_exchanges``), USDT
    perps of :data:`BASES`, in setting order without repeats. Anything else is rejected and never reaches the URL. When
    nothing valid is left (empty or all rejected), the default Binance symbols, so a typo does not stop the series
    already being collected.
    """
    names, bad = parse_exchanges([v for v in (venues or "").split(",") if v.strip()])
    symbols = [sym for ex in names for b in BASES if (sym := exchange_symbol(b, "usdt", ex))]
    return symbols or list(SYMBOLS), bad


def settled_end(now_ts: int) -> int:
    """Exclusive end (00:00 UTC, unix seconds) of the closed days the job may ask at ``now_ts``: today 00:00 UTC once
    :data:`SETTLE_SECONDS` have passed since it, else yesterday 00:00 UTC."""
    return (now_ts - SETTLE_SECONDS) // DAY * DAY


def plan_window(now_ts: int, last_ts: int | None, cursor: int | None, backfill_days: int, retention_days: int) -> tuple[int, int] | None:
    """[start, end) in unix seconds of the next request for one series, or None when it is up to date.

    ``end`` never passes :func:`settled_end` (only closed, settled days) and the window spans at most
    :data:`CHUNK_DAYS`. Start: the day after ``last_ts`` (the last stored bar), else ``backfill_days`` ago (clamped to
    at least 1, so a 0 or a negative value still collects new days), and never before ``cursor`` (the end of an empty
    window already walked). With ``retention_days`` > 0 the backfill starts no older than the retention window, so the
    backfill does not keep re-asking for days the sweep deletes; the first bar (ts = that floor, 00:00 UTC) is still up
    to a day older than the sweep's cutoff (now - retention) and is pruned on the next sweep, which is harmless (the
    series resumes from its last stored bar, not its first).
    """
    end = settled_end(now_ts)
    days = max(backfill_days, 1)
    if retention_days > 0:
        days = min(days, retention_days)
    floor = end - days * DAY
    start = max(last_ts + DAY if last_ts is not None else floor, floor)
    if cursor is not None:
        start = max(start, cursor)
    if start >= end:
        return None
    return start, min(start + CHUNK_DAYS * DAY, end)


def history_rows(metric: str, history: Any, start: int, end: int) -> list[dict[str, Any]]:
    """Raw Coinalyze ``history`` rows -> store rows of ``metric`` with ``start <= ts < end``. Units: ``funding`` decimal
    per funding interval (API percent / 100); ``oi_usd`` as sent; ``long_short`` ratio and shares in [0, 1]. Malformed
    rows are dropped by the shared parsers (analytics/coinalyze_series.py)."""
    hist = history if isinstance(history, list) else []
    if metric == "long_short":
        s = long_short_series(hist, "daily")
        rows = [
            {"ts": r["ts"], "ratio": r["ratio"], "long_share": r["long_share"], "short_share": r["short_share"]}
            for r in (s or {}).get("rows", [])
        ]
    elif metric in ("oi_usd", "funding"):
        scale = FUNDING_PCT_TO_DECIMAL if metric == "funding" else 1.0
        s = ohlc_series(hist, "daily", scale=scale, with_pct_change=False)
        rows = [
            {"ts": r["ts"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]} for r in (s or {}).get("rows", [])
        ]
    else:
        raise ValueError(f"unknown metric {metric!r}")
    return [r for r in rows if start <= r["ts"] < end]


def oi_not_usd(rows: list[dict[str, Any]]) -> bool:
    """True when the median close of an ``oi_usd`` series is below :data:`MIN_OI_USD` (looks like coins, not USD).
    The median, not any single bar, so one zero or missing bar does not refuse the whole series."""
    return bool(rows) and statistics.median(r["close"] for r in rows) < MIN_OI_USD


def split_bad_oi(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped) of a USD ``oi_usd`` series: a bar whose close is below :data:`MIN_OI_USD` is damaged (a zero or
    a gap the aggregator filled) and is dropped."""
    kept = [r for r in rows if r["close"] >= MIN_OI_USD]
    return kept, [r for r in rows if r["close"] < MIN_OI_USD]


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _fetch(rt: "Runtime", metric: str, symbol: str, start: int, end: int) -> "Awaitable[Fetched]":
    iv = api_interval("daily")
    if metric == "oi_usd":
        return rt.coinalyze.open_interest_history(symbol, iv, start, end - 1, convert_to_usd=True)
    if metric == "funding":
        return rt.coinalyze.funding_rate_history(symbol, iv, start, end - 1)
    return rt.coinalyze.long_short_ratio_history(symbol, iv, start, end - 1)


class CoinalyzeHistoryJob:
    """Collector job: one pass over the symbols of the configured venues (:func:`venue_symbols`) x :data:`METRICS`
    (see the module docstring). Returns the rows written. A provider failure lands in ``missing`` and the other series
    go on; the job raises (so the scheduler records ``last_error``) only when every request of the run failed."""

    def __init__(self) -> None:
        self.cursor: dict[tuple[str, str], int] = {}
        self._warned_no_key = False
        self._warned_venues = False

    async def __call__(self, rt: "Runtime") -> int:
        if rt.store is None:
            raise RuntimeError("coinalyze_history: needs a SQLite store (set TRADEDESK_DB_PATH)")
        if not rt.coinalyze.has_key:
            if not self._warned_no_key:
                log.warning("coinalyze_history: COINALYZE_API_KEY is not set, the job does nothing")
                self._warned_no_key = True
            return 0
        await rt.store.init()
        s = rt.settings
        symbols, bad = venue_symbols(s.collect_coinalyze_history_venues)
        if bad and not self._warned_venues:
            log.warning("coinalyze_history: venue(s) %s not in the verified whitelist (binance, bybit), ignored", bad)
            self._warned_venues = True
        now_ts = int(rt.now().timestamp())
        b = Builder()
        written = tried = 0
        for symbol in symbols:
            for metric in METRICS:
                key = (symbol, metric)
                last = await rt.store.coinalyze_history_last_ts(symbol, metric)
                w = plan_window(
                    now_ts, last, self.cursor.get(key), s.collect_coinalyze_history_backfill_days, s.retention_coinalyze_history_days
                )
                if w is None:
                    continue
                start, end = w
                tried += 1
                hist = await b.guard(f"{metric}:{symbol}", "coinalyze", _fetch(rt, metric, symbol, start, end))
                if hist is None:
                    continue
                rows = history_rows(metric, hist, start, end)
                if metric == "oi_usd" and oi_not_usd(rows):
                    b.miss(
                        f"{metric}:{symbol}", "coinalyze", "data_unavailable", f"OI median close below {MIN_OI_USD:g}: not USD, not stored"
                    )
                    continue
                if metric == "oi_usd":
                    rows, dropped = split_bad_oi(rows)
                    if dropped:
                        log.warning(
                            "coinalyze_history: %s oi_usd dropped %d damaged bar(s) with close below %g: %s",
                            symbol,
                            len(dropped),
                            MIN_OI_USD,
                            ", ".join(_day(r["ts"]) for r in dropped),
                        )
                if not rows:
                    if end < settled_end(now_ts):
                        # A window wholly in the past: walk past it (until a restart), but say so.
                        self.cursor[key] = end
                        log.warning("coinalyze_history: %s %s empty for [%s, %s), skipped", symbol, metric, _day(start), _day(end))
                    else:
                        # Recent days missing for BTC/ETH perps is a fault, not the edge of history: retry every run.
                        log.warning(
                            "coinalyze_history: %s %s empty for [%s, %s) up to the last settled day, retried next run",
                            symbol,
                            metric,
                            _day(start),
                            _day(end),
                        )
                    continue
                written += await rt.store.insert_coinalyze_history(symbol, metric, rows, now_ts)
        if b.missing:
            msg = "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:6])
            if not written and len(b.missing) >= tried:
                raise RuntimeError(f"coinalyze_history: all {tried} requests failed; {msg}")
            log.warning("coinalyze_history: partial run, missing: %s", msg)
        return written
