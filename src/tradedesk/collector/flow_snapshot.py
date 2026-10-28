"""Hourly flow snapshot: order-book depth, OI, funding and long/short of BTC and ETH on Binance / Bybit / OKX / Hyperliquid.

Docs: docs/ideas/IDEA-20261028-market-flow-reader.md, step 2 (our own forward history of what has no free intraday past;
nothing here trades). Read-only public data through the existing providers only (`core.http.Http` and
`Exchanges._call`: rate limits, retries, health): ccxt order books, open interest and current funding (Binance spot /
USD-M, Bybit), OKX v5 through ``Exchanges.public_get`` (``books-full`` 5000 levels, instrument ``ctVal``, open interest
in coins, rubik long/short account ratio), Binance ``/futures/data`` and Bybit ``/v5/market/account-ratio`` (5-minute
long/short, the latest row), Hyperliquid ``l2Book`` at 4 and 3 significant figures and ``metaAndAssetCtxs``. Rows are
built by `analytics/flow_snapshot.py` into `flow_snapshot`: 2 bases x 7 (venue, market) = 14 rows an hour.

The key is (UTC hour, base, venue, market); ``fetched_ts`` is the time of the tick that wrote the stored row. The job
ticks more often than hourly (default 10 min) and fetches only the keys the current hour has no complete row for, so a
restart or a venue outage inside the hour is retried. A part that fails (book, oi, funding, funding_interval,
long_share) leaves its columns NULL and is named in ``missing``; such a partial row is stored, fetched again on the
next ticks of the hour and replaced whole by a later row with fewer missing parts (``funding_interval`` alone counts as
complete: 8 h assumed, a retry would not find it). A row with no data at all is not written. It never raises on a
provider failure.

Hyperliquid's ``l2Book`` is 20 aggregated levels a side, so its bands depend on the coin's price (live 2026-10-28):
BTC has only the 0.1 % and 1 % bands, ETH only the 0.5 % band; the others are NULL. Compare HL with other venues only
on the bands it fills. Liquidations are not in this table: the WebSocket collector (TRADEDESK_COLLECT_LIQUIDATIONS_ENABLED) stores
every event in `liquidations`, hourly sums are a read-time query. Off by default: TRADEDESK_COLLECT_FLOW_SNAPSHOT_ENABLED=1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..analytics import flow_snapshot as fs
from ..analytics.derivatives import parse_interval_hours
from ..analytics.hl_oi_cap import hour_ts
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

BOOK_LIMIT = {"binance": 5000, "binanceusdm": 1000, "bybit:spot": 200, "bybit:perp": 500}  # each venue's REST maximum
OKX_BOOK_SIZE = "5000"
DEFAULT_FUNDING_HOURS = 8.0  # Binance BTC/ETH when the venue lists no interval


async def _ccxt_book(rt: "Runtime", b: Builder, ex: str, symbol: str, limit: int) -> dict[str, Any] | None:
    ob = await b.guard("book", f"ccxt:{ex}", rt.exchanges.order_book(ex, symbol, limit))
    if ob is None:
        return None
    d = fs.depth_bands(fs.levels(ob.get("bids")), fs.levels(ob.get("asks"))) if isinstance(ob, dict) else fs.empty_depth()
    if d["mid"] is None:
        b.miss("book", f"ccxt:{ex}", "data_unavailable", "empty or crossed book")
        return None
    return d


def _okx_data(b: Builder, field: str, raw: Any) -> list[Any] | None:
    """``data`` of an OKX v5 answer (``{"code": "0", "data": [...]}``), else a miss."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or str(raw.get("code")) != "0" or not isinstance(raw.get("data"), list) or not raw["data"]:
        b.miss(field, "ccxt:okx", "backend_error", f"unexpected answer: {str(raw)[:80]}")
        return None
    return raw["data"]


async def _okx_get(rt: "Runtime", b: Builder, field: str, endpoint: str, params: dict[str, Any], ttl: float = 0) -> list[Any] | None:
    return _okx_data(b, field, await b.guard(field, "ccxt:okx", rt.exchanges.public_get("okx", endpoint, params, ttl=ttl)))


async def _okx_book(rt: "Runtime", b: Builder, inst: str, size_mult: Any) -> dict[str, Any] | None:
    data = await _okx_get(rt, b, "book", "publicGetMarketBooksFull", {"instId": inst, "sz": OKX_BOOK_SIZE})
    if data is None or size_mult is None:
        return None
    book = data[0] if isinstance(data[0], dict) else {}
    d = fs.depth_bands(fs.levels(book.get("bids"), size_mult), fs.levels(book.get("asks"), size_mult))
    if d["mid"] is None:
        b.miss("book", "ccxt:okx", "data_unavailable", "empty or crossed book")
        return None
    return d


async def _funding(rt: "Runtime", b: Builder, ex: str, symbol: str) -> tuple[float | None, float | None]:
    """(current rate per interval, interval hours). Binance's ccxt answer has no interval: read it from
    ``fetch_funding_intervals``; if that is unknown too, 8 h is assumed and ``funding_interval`` is marked missing."""
    fr = await b.guard("funding", f"ccxt:{ex}", rt.exchanges.funding_rate(ex, symbol))
    rate = fs.fnum(fr.get("fundingRate")) if isinstance(fr, dict) else None
    if rate is None:
        if fr is not None:
            b.miss("funding", f"ccxt:{ex}", "data_unavailable", "no fundingRate")
        return None, None
    interval = fr.get("interval")
    if not interval:
        meta = await b.guard("funding_interval", f"ccxt:{ex}", rt.exchanges.funding_intervals(ex))
        entry = meta.get(symbol) if isinstance(meta, dict) else None
        interval = entry.get("interval") if isinstance(entry, dict) else None
        if not interval and meta is not None:
            b.miss("funding_interval", f"ccxt:{ex}", "data_unavailable", f"no interval listed, {DEFAULT_FUNDING_HOURS:g}h assumed")
    return rate, parse_interval_hours(interval, DEFAULT_FUNDING_HOURS)


async def _ccxt_oi(rt: "Runtime", b: Builder, ex: str, symbol: str) -> float | None:
    oi = await b.guard("oi", f"ccxt:{ex}", rt.exchanges.open_interest(ex, symbol))
    coins = fs.fnum(oi.get("openInterestAmount")) if isinstance(oi, dict) else None
    if oi is not None and coins is None:
        b.miss("oi", f"ccxt:{ex}", "data_unavailable", "no openInterestAmount")
    return coins


def _latest(b: Builder, field: str, provider: str, rows: Any, key: str) -> Any:
    """``key`` of the newest row (by ``timestamp``) that has it, else a miss."""
    if rows is None:
        return None
    cands = [r for r in rows if isinstance(r, dict) and r.get(key) is not None] if isinstance(rows, list) else []
    if not cands:
        b.miss(field, provider, "data_unavailable", f"no {key} in the answer")
        return None
    return max(cands, key=lambda r: fs.fnum(r.get("timestamp")) or 0)[key]


async def _binance(rt: "Runtime", base: str, market: str) -> dict[str, Any]:
    b = Builder()
    if market == "spot":
        return fs.flow_row("binance", "spot", depth=await _ccxt_book(rt, b, "binance", f"{base}/USDT", BOOK_LIMIT["binance"]),
                           missing=[m.field for m in b.missing])  # fmt: skip
    perp = f"{base}/USDT:USDT"
    depth, oi, (rate, hours), ls = await asyncio.gather(
        _ccxt_book(rt, b, "binanceusdm", perp, BOOK_LIMIT["binanceusdm"]),
        _ccxt_oi(rt, b, "binanceusdm", perp),
        _funding(rt, b, "binanceusdm", perp),
        b.guard("long_share", "binance_futures", rt.binance_futures.long_short_account_ratio(f"{base}USDT", "5m", 1)),
    )
    long_share = _latest(b, "long_share", "binance_futures", ls, "longAccount")
    return fs.flow_row("binance", "perp", depth=depth, oi_coins=oi, funding_rate=rate, funding_hours=hours, long_share=long_share,
                       missing=[m.field for m in b.missing])  # fmt: skip


async def _bybit(rt: "Runtime", base: str, market: str) -> dict[str, Any]:
    b = Builder()
    if market == "spot":
        return fs.flow_row("bybit", "spot", depth=await _ccxt_book(rt, b, "bybit", f"{base}/USDT", BOOK_LIMIT["bybit:spot"]),
                           missing=[m.field for m in b.missing])  # fmt: skip
    perp = f"{base}/USDT:USDT"
    depth, oi, (rate, hours), ls = await asyncio.gather(
        _ccxt_book(rt, b, "bybit", perp, BOOK_LIMIT["bybit:perp"]),
        _ccxt_oi(rt, b, "bybit", perp),
        _funding(rt, b, "bybit", perp),
        b.guard("long_share", "bybit", rt.bybit.long_short_account_ratio(f"{base}USDT", "5min", 1)),
    )
    long_share = _latest(b, "long_share", "bybit", ls, "buyRatio")
    return fs.flow_row("bybit", "perp", depth=depth, oi_coins=oi, funding_rate=rate, funding_hours=hours, long_share=long_share,
                       missing=[m.field for m in b.missing])  # fmt: skip


async def _okx(rt: "Runtime", base: str, market: str) -> dict[str, Any]:
    b = Builder()
    if market == "spot":
        return fs.flow_row("okx", "spot", depth=await _okx_book(rt, b, f"{base}-USDT", 1.0), missing=[m.field for m in b.missing])
    inst = f"{base}-USDT-SWAP"

    async def book() -> dict[str, Any] | None:
        # Swap book sizes are contracts: x ctVal (coins per contract, BTC 0.01 / ETH 0.1 live 2026-10-28).
        ins = await _okx_get(rt, b, "book", "publicGetPublicInstruments", {"instType": "SWAP", "instId": inst}, ttl=3600)
        ct = fs.fnum(ins[0].get("ctVal")) if ins and isinstance(ins[0], dict) else None
        if ins is not None and (ct is None or ct <= 0 or ins[0].get("ctValCcy") != base):
            b.miss("book", "ccxt:okx", "data_unavailable", "no contract value in coins for the swap")
            return None
        return await _okx_book(rt, b, inst, ct) if ct else None

    async def oi() -> float | None:
        data = await _okx_get(rt, b, "oi", "publicGetPublicOpenInterest", {"instType": "SWAP", "instId": inst})
        coins = fs.fnum(data[0].get("oiCcy")) if data and isinstance(data[0], dict) else None
        if data is not None and coins is None:
            b.miss("oi", "ccxt:okx", "data_unavailable", "no oiCcy")
        return coins

    async def long_share() -> float | None:
        params = {"instId": inst, "period": "5m", "limit": "1"}
        data = await _okx_get(rt, b, "long_share", "publicGetRubikStatContractsLongShortAccountRatioContract", params)
        row = data[0] if data else None
        got = fs.long_share_from_ratio(row[1]) if isinstance(row, list) and len(row) >= 2 else None
        if data is not None and got is None:
            b.miss("long_share", "ccxt:okx", "data_unavailable", "no long/short ratio")
        return got

    depth, oi_coins, (rate, hours), ls = await asyncio.gather(book(), oi(), _funding(rt, b, "okx", f"{base}/USDT:USDT"), long_share())
    return fs.flow_row("okx", "perp", depth=depth, oi_coins=oi_coins, funding_rate=rate, funding_hours=hours, long_share=ls,
                       missing=[m.field for m in b.missing])  # fmt: skip


async def _hyperliquid(rt: "Runtime", base: str, market: str) -> dict[str, Any]:
    b = Builder()
    books = await asyncio.gather(*(b.guard("book", "hyperliquid", rt.hyperliquid.l2_book(base, n)) for n in fs.HL_SIG_FIGS))
    ctx = await b.guard("oi", "hyperliquid", rt.hyperliquid.asset_contexts())
    depth = fs.hl_depth([(n, raw) for n, raw in zip(fs.HL_SIG_FIGS, books, strict=True) if raw is not None])
    if depth["mid"] is None and any(raw is not None for raw in books):
        b.miss("book", "hyperliquid", "data_unavailable", "empty or malformed l2Book")
    row = next((c for c in ctx or [] if isinstance(c, dict) and c.get("coin") == base), None)
    if ctx is not None and row is None:
        b.miss("oi", "hyperliquid", "data_unavailable", f"{base} not in metaAndAssetCtxs")
    oi = row.get("open_interest") if row else None
    rate = row.get("funding_1h") if row else None
    if row is not None and fs.fnum(rate) is None:
        b.miss("funding", "hyperliquid", "data_unavailable", f"no funding for {base} in metaAndAssetCtxs")
    return fs.flow_row("hyperliquid", "perp", depth=depth, oi_coins=oi, funding_rate=rate, funding_hours=1.0,
                       missing=[m.field for m in b.missing] + ([] if row else ["funding"]))  # fmt: skip


VENUE_FN = {"binance": _binance, "bybit": _bybit, "okx": _okx, "hyperliquid": _hyperliquid}


async def collect_flow_snapshot(rt: "Runtime") -> int:
    """Collector job. Returns the rows inserted or replaced (0: the hour is complete, or nothing new was fetched)."""
    if rt.store is None:
        log.warning("flow_snapshot: no SQLite store (set TRADEDESK_DB_PATH), nothing recorded")
        return 0
    await rt.store.init()
    now = rt.now()  # one clock read: the hour key and the fetch time
    ts = hour_ts(now)
    have = await rt.store.flow_snapshot_keys(ts)
    todo = [(base, v, m) for base in fs.ASSETS for v, m in fs.VENUE_MARKETS if (base, v, m) not in have]
    if not todo:
        return 0
    got = await asyncio.gather(*(VENUE_FN[v](rt, base, m) for base, v, m in todo), return_exceptions=True)
    rows: list[dict[str, Any]] = []
    failed: list[str] = []
    partial: list[str] = []
    for (base, v, m), r in zip(todo, got, strict=True):
        key = f"{base} {v} {m}"
        if isinstance(r, asyncio.CancelledError):
            raise r
        if isinstance(r, BaseException):  # a bug, not a provider failure: keep the other rows, report it
            failed.append(f"{key}: {type(r).__name__}: {r}"[:160])
        elif not fs.has_data(r):
            failed.append(f"{key}: {r.get('missing') or 'no data'}")
        else:
            rows.append({"base": base, **r})
            if r.get("missing"):
                partial.append(f"{key}: {r['missing']}")
    written = await rt.store.insert_flow_snapshot(ts, int(now.timestamp()), rows) if rows else 0
    if failed:
        log.warning("flow_snapshot: hour %d, %d rows not recorded (retried on the next tick): %s", ts, len(failed), "; ".join(failed)[:600])
    if partial:
        log.warning("flow_snapshot: hour %d, partial rows (NULL columns): %s", ts, "; ".join(partial)[:600])
    log.info(
        "flow_snapshot: hour %d, %d rows written, %d of %d keys complete",
        ts,
        written,
        len(await rt.store.flow_snapshot_keys(ts)) if written else len(have),
        len(fs.ASSETS) * len(fs.VENUE_MARKETS),
    )
    return written
