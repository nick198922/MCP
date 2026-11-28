"""Coinalyze daily history of BTC/ETH perps per venue into SQLite (table ``coinalyze_history``).

Series: open interest in USD (``convert_to_usd=true``), funding (decimal fraction per funding interval: Coinalyze sends
percent, divided by 100), the long/short account ratio (ratio, long/short shares in [0, 1]) and, from 2026-11-28,
long and short liquidations in USD per day (metric ``liq_usd``, columns ``liq_long_usd`` / ``liq_short_usd``,
``convert_to_usd=true``; see the caveats below), for ``BTCUSDT_PERP.A``
and ``ETHUSDT_PERP.A`` (Binance, the default) and, with ``TRADEDESK_COLLECT_COINALYZE_HISTORY_VENUES=binance,bybit``,
also ``BTCUSDT.6`` and ``ETHUSDT.6`` (Bybit USDT perps). The suffix is the exchange code in Coinalyze, NOT an
all-exchange aggregate (``.A`` = Binance, docs/reviews/2026-10-18-doubletop-parity-1b.md; ``.6`` = Bybit,
docs/reviews/2026-10-25-doubletop-parity-2.md, both by matching OI with the live venue). Only codes of that verified
whitelist (``EXCHANGE_CODES``) are asked; other venue names in the setting are ignored with a warning. Each venue is
stored under its own symbol, so the rows never mix: summing venues is left to the reader. The three Bybit series of
BTC and ETH (daily bars) were reconciled with Bybit's own history on 2026-11-27
(docs/verification/2026-11-27-coinalyze-bybit-check.md): funding OHLC = first / max / min / last settlement of the
UTC day on all 1489 days (2022-10-30..2026-11-26); long/short = the venue's 00:00 UTC account snapshot of the bar's
day on the last 499 days only (from 2025-07-16; the venue returns 500 snapshots); OI USD close = the venue's OI
stamped at the next 00:00 UTC x the day's Bybit close on the last 401 days only (from 2025-10-22). Older long/short
and OI days were not checked against the venue. Network only through ``rt.coinalyze`` (the shared ``Http``: rate
limit, retries, health); the API key lives in the request header only and is never logged or stored.

Only closed UTC days are stored, and a day is first asked :data:`SETTLE_SECONDS` after its close (02:00 UTC of the
next day), so a bar Coinalyze still finishes in the first minutes after midnight is not frozen half-built: the job
never re-reads a stored day. A repeated bar is rewritten in place (PRIMARY KEY (symbol, metric, ts)). Each run asks
at most :data:`CHUNK_DAYS` days per series, resuming the day after the last stored bar; with nothing stored it starts
``backfill_days`` ago (at least 1; never older than the retention window when one is set). Per venue 2 symbols x 4
metrics = at most 8 requests per run (16 with Bybit), all through the provider's 40/min limiter, and none once the
series is up to date (the next one goes out after 02:00 UTC). A venue switched on later backfills on its own: each
series resumes from its own last stored bar; so does a metric added later (``liq_usd`` on a database that already
holds the other three backfills from ``backfill_days`` ago while they keep resuming from their own last bar).

An empty answer is logged as a warning every time. When the empty window lies wholly in the past (it ends before the
last settled day) an in-memory cursor moves past it, so a range Coinalyze has no data for is not asked again until a
restart. An empty window that reaches the last settled day does NOT move the cursor: for BTC/ETH perps missing
recent days are a fault (wrong symbol, changed response shape), not the edge of history, so it is retried every run
and keeps warning.

Caveats for readers of the table: funding ``open``/``high``/``low`` of a Binance day may carry the previous day's
last rate (BTC/ETH, 2022-10-30..2026-11-26: 6-7 % of days for open, 1-2 % for high and low, nearly all in 2026; not
seen on Bybit), so use ``close`` = the day's last settlement; the long/short row is the account snapshot at ``ts`` =
00:00 UTC, the START of the bar's day (checked on Bybit BTC/ETH over the last 499 days; Binance only 29 days, too few
to call), while the OI ``close`` is the end of the day: the two are 24 h apart; OI in USD is
Coinalyze's conversion. ``convert_to_usd`` is confirmed by the live table (2026-11-27: median closes 1.6e9-5.7e9
USD per series), and the guard stays: an OI series whose MEDIAN close looks like coins (below :data:`MIN_OI_USD`) is
refused and lands in ``missing`` rather than being stored as USD; single bars below it in a USD series (a zero or a
gap filled by the aggregator) are dropped with a warning and the rest is stored.

Liquidations (``liq_usd``): rows are parsed by the same ``summarise`` as the ``liquidations_history`` tool
(analytics/liquidations_agg.py); a row with a missing side, a negative, non-finite or absurd (above ``MAX_ABS``)
value or a bad time is dropped whole and counted like the other series (a repeated day keeps the last row). USD is
Coinalyze's conversion = coins x the day's close (the tool's check of 2026-10-25), not each liquidation at its own
price, so on crash days the USD sums are shifted against the coin counts (long liquidations of a crash day fill above
its close and are understated). The Binance and OKX streams send at most one liquidation per instrument per second
(docs/verification/2026-11-12-liquidation-stream-gaps.md), so exchange-reported liquidations can be lower bounds; how
Coinalyze builds its numbers was not checked. Read a row as one venue's reported flow, never a market-wide total;
venues stay separate rows (sum ``.A`` + ``.6`` per day yourself, only on days both have). A day with zero on both
sides is stored as zeros; a day the API omits is not stored and is NOT a zero day: read it as unknown. No unit guard
as for OI: a quiet day is legitimately small or zero, so a magnitude test would refuse real days; the USD unit rests
on ``convert_to_usd=true`` in the request. Not reconciled with the venues (Bybit / Binance) yet.

Off by default: TRADEDESK_COLLECT_COINALYZE_HISTORY_ENABLED=1 (and the collector); without COINALYZE_API_KEY the job
logs one warning and does nothing.
"""

from __future__ import annotations

import logging
import math
import statistics
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..analytics.coinalyze_series import FUNDING_PCT_TO_DECIMAL, MAX_ABS, long_short_series, ohlc_series
from ..analytics.liquidations_agg import aggregated_symbol, api_interval, exchange_symbol, parse_exchanges, summarise
from ..core.envelope import Builder, Fetched
from .bad_rows import note_bad_rows

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

DAY = 86_400
CHUNK_DAYS = 120  # per series per run; the tools read up to 365 daily bars in one request, so this is well inside
BASES = ("BTC", "ETH")
SYMBOLS = tuple(aggregated_symbol(b) or "" for b in BASES)  # BTCUSDT_PERP.A, ETHUSDT_PERP.A (Binance, the default)
METRICS = ("oi_usd", "funding", "long_short", "liq_usd")
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
    per funding interval (API percent / 100); ``oi_usd`` as sent; ``long_short`` ratio and shares in [0, 1];
    ``liq_usd`` long / short liquidations of the day, USD as sent. Malformed rows are dropped by the shared parsers
    (analytics/coinalyze_series.py, analytics/liquidations_agg.summarise)."""
    return [r for r in parse_history(metric, history) if start <= r["ts"] < end]


def parse_history(metric: str, history: Any) -> list[dict[str, Any]]:
    """:func:`history_rows` without the window filter: every usable row (malformed and repeated ones dropped)."""
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
    elif metric == "liq_usd":
        # The tool's parser (time range, both sides present, no negatives); it keeps NaN / inf / 1e308 and repeated
        # days, so those are dropped here as ohlc_series does (``MAX_ABS``, a repeated day keeps the last row).
        s = summarise(hist, "daily")
        by_ts = {
            r["ts"]: {"ts": r["ts"], "liq_long_usd": r["long"], "liq_short_usd": r["short"]}
            for r in (s or {}).get("rows", [])
            if all(math.isfinite(v) and v <= MAX_ABS for v in (r["long"], r["short"]))
        }
        rows = [by_ts[t] for t in sorted(by_ts)]
    else:
        raise ValueError(f"unknown metric {metric!r}")
    return rows


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
    if metric == "liq_usd":
        return rt.coinalyze.liquidation_history(symbol, iv, start, end - 1, convert_to_usd=True)
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
                parsed = parse_history(metric, hist)
                if isinstance(hist, list) and len(hist) > len(parsed):  # a NaN / text / 1e308 value or a bad time
                    note_bad_rows(len(hist) - len(parsed))
                    log.warning("coinalyze_history: %s %s dropped %d malformed or repeated bar(s)", symbol, metric, len(hist) - len(parsed))
                rows = [r for r in parsed if start <= r["ts"] < end]
                if metric == "oi_usd" and oi_not_usd(rows):
                    b.miss(
                        f"{metric}:{symbol}", "coinalyze", "data_unavailable", f"OI median close below {MIN_OI_USD:g}: not USD, not stored"
                    )
                    continue
                if metric == "oi_usd":
                    rows, dropped = split_bad_oi(rows)
                    if dropped:
                        note_bad_rows(len(dropped))
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
