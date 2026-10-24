"""Binance TradFi perps: best bid/ask, mark and index of every US-equity/ETF and commodity perp, once a minute.

Docs: docs/ideas/IDEA-20261024-tradfi-perp-offhours-reopen.md (card `inconclusive`; this is cost collection for the early
kill (b), not a trading hypothesis, nothing here trades). Read-only public data through the shared Binance USD-M
provider (`rt.binance_futures`, own rate limit): per tick two all-symbol requests without ``symbol`` -
``GET /fapi/v1/ticker/bookTicker`` (weight 5) and ``GET /fapi/v1/premiumIndex`` (weight 10) - in one ``asyncio.gather``;
the symbol list comes from ``GET /fapi/v1/exchangeInfo`` once a day (`analytics/tradfi_spread.is_tradfi_perp`:
TRADIFI_PERPETUAL, underlying EQUITY or COMMODITY, USDT-quoted; live 24.10: 194 symbols). Depth is NOT collected: the
``bookDepth`` archive on data.binance.vision has it for these symbols; only best bid/ask is missing there.

One row per (ts, symbol): ``ts`` is one ``rt.now()`` read per tick (unix seconds); ``book_ms`` / ``premium_ms`` are the
venue's own times of each answer. If one request fails, its columns stay NULL and the other is still stored (``missing``
logged); the job fails (the scheduler records it) only when nothing could be stored. The spread and the cost per round
trip are computed at read time (`analytics/tradfi_spread.py`). Off by default: TRADEDESK_COLLECT_TRADFI_SPREAD_ENABLED=1.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..analytics.option_xvenue import num
from ..analytics.tradfi_spread import is_tradfi_perp
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

PROVIDER = "binance_futures"
SYMBOLS_REFRESH_SECONDS = 86_400  # exchangeInfo once a day (a failed refresh is retried on the next tick)


def tradfi_symbols(exchange_info: Any) -> frozenset[str]:
    """Symbols of the TradFi perps kept (see :func:`analytics.tradfi_spread.is_tradfi_perp`) in an ``exchangeInfo``."""
    syms = exchange_info.get("symbols") if isinstance(exchange_info, dict) else None
    return frozenset(str(s["symbol"]) for s in syms or [] if isinstance(s, dict) and s.get("symbol") and is_tradfi_perp(s))


def _ms(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _by_symbol(rows: Any, keep: frozenset[str]) -> dict[str, dict[str, Any]]:
    return {str(r["symbol"]): r for r in rows if isinstance(r, dict) and r.get("symbol") in keep} if isinstance(rows, list) else {}


def build_rows(symbols: Iterable[str], book: Any, premium: Any) -> list[dict[str, Any]]:
    """Rows of one snapshot from the raw bookTicker / premiumIndex lists (either may be None after a failure). A symbol
    absent from both answers is skipped; an empty or zero side is stored as NULL."""
    keep = frozenset(symbols)
    bk, pm = _by_symbol(book, keep), _by_symbol(premium, keep)
    rows = []
    for s in sorted(keep & (bk.keys() | pm.keys())):
        b, p = bk.get(s, {}), pm.get(s, {})
        rows.append(
            {
                "symbol": s,
                "bid": num(b.get("bidPrice")),
                "ask": num(b.get("askPrice")),
                "mark_price": num(p.get("markPrice")),
                "index_price": num(p.get("indexPrice")),
                "book_ms": _ms(b.get("time")),
                "premium_ms": _ms(p.get("time")),
            }
        )
    return rows


async def scan_tradfi_spread(rt: "Runtime", b: Builder, symbols: Iterable[str]) -> list[dict[str, Any]]:
    """One snapshot of ``symbols`` (both all-symbol requests in one gather). Never raises on a provider failure: it
    lands in ``b.missing`` and the other request's columns are still returned."""
    book, premium = await asyncio.gather(
        b.guard("binance_book_ticker", PROVIDER, rt.binance_futures.book_tickers()),
        b.guard("binance_premium_index", PROVIDER, rt.binance_futures.premium_index()),
    )
    return build_rows(symbols, book, premium)


class TradfiSpreadJob:
    """Collector job: one snapshot per call (see the module docstring). State: the TradFi symbol list and when it was
    last refreshed from exchangeInfo. Returns the rows written; raises ``RuntimeError`` (caught and counted by the
    scheduler) only when nothing could be stored."""

    def __init__(self, refresh_seconds: float = SYMBOLS_REFRESH_SECONDS):
        self.refresh_seconds = refresh_seconds
        self.symbols: frozenset[str] = frozenset()
        self.symbols_at: datetime | None = None

    async def _refresh(self, rt: "Runtime", b: Builder, now: datetime) -> None:
        if self.symbols_at is not None and (now - self.symbols_at).total_seconds() < self.refresh_seconds:
            return
        info = await b.guard("binance_exchange_info", PROVIDER, rt.binance_futures.exchange_info())
        if info is None:
            return  # keep the previous list (if any); retried on the next tick
        syms = tradfi_symbols(info)
        if not syms:  # a reshaped answer must not wipe the list
            b.miss("binance_exchange_info", PROVIDER, "data_unavailable", "exchangeInfo lists no TradFi perp")
            return
        if self.symbols and syms != self.symbols:
            log.info("tradfi_spread: symbols +%s -%s", sorted(syms - self.symbols), sorted(self.symbols - syms))
        self.symbols, self.symbols_at = syms, now

    async def __call__(self, rt: "Runtime") -> int:
        if rt.store is None:
            raise RuntimeError("tradfi_spread: needs a SQLite store (set TRADEDESK_DB_PATH)")
        await rt.store.init()
        now = rt.now()  # one clock read: the row ts and the symbol-list schedule
        b = Builder()
        await self._refresh(rt, b, now)
        rows = await scan_tradfi_spread(rt, b, self.symbols) if self.symbols else []
        if not rows:  # like option_xvenue / pm_gap: a full outage is a failed run in the scheduler's metrics
            why = "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:4]) or "no TradFi perp in the answers"
            raise RuntimeError(f"tradfi_spread: no rows; {why}")
        if b.missing:
            log.warning("tradfi_spread: partial snapshot, missing: %s", "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:4]))
        return await rt.store.insert_tradfi_spread(int(now.timestamp()), rows)
