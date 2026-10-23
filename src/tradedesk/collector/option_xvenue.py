"""Deribit <-> Bybit daily option quotes: hourly snapshot of the nearest daily expiry, ATM +- 3 strikes, calls and puts.

Docs: docs/ideas/IDEA-20261023-bybit-deribit-daily-option-dislocation-forward.md (forward card; measurement only, nothing
here trades). Read-only public data through the shared providers: the Deribit option book summary and per-instrument
tickers (`rt.deribit`, own rate limit) and the Bybit v5 option tickers through ccxt (`rt.exchanges.public_get`, i.e.
`Exchanges._call`). One row per (currency, strike, call/put) with both venues side by side and the read time of each leg
(`d_ts_ms`, `b_ts_ms`), so the analysis can keep only synchronous pairs. The crossing itself is computed at read time
(`analytics/option_xvenue.crossing_from_row`). Off by default: TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED=1, which must
wait until the threshold is registered in its own commit (card condition 3).

Strikes come from Deribit (ATM +- 3 around the expiry's forward); a Bybit strike that Deribit does not list cannot cross
and is not stored. When the Deribit summary fails, the Bybit chain picks the expiry and strikes instead. A venue without
the chosen expiry (e.g. Bybit without daily options) leaves its columns NULL and lands in ``missing``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..analytics.instruments import parse_option_name
from ..analytics.option_xvenue import (
    TAU_MAX_HOURS,
    TAU_MIN_HOURS,
    atm_strikes,
    bybit_quote,
    deribit_quote,
    num,
    parse_bybit_symbol,
    pick_daily_expiry,
)
from ..core.envelope import Builder

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

CURRENCIES = ("BTC", "ETH")
BYBIT = "bybit"
BYBIT_TICKERS = "publicGetV5MarketTickers"  # GET /v5/market/tickers?category=option&baseCoin=<COIN>
QUOTE_KEYS = ("bid", "ask", "bid_size", "ask_size", "bid_iv", "ask_iv", "mark_iv", "index", "underlying", "ts_ms")

Chain = dict[datetime, dict[float, dict[str, Any]]]  # expiry -> strike -> {"C": x, "P": x}


def _deribit_chain(summary: Any, cur: str) -> tuple[Chain, dict[datetime, float]]:
    """Instrument names by expiry/strike/type and the forward (underlying_price) of each expiry, from the book summary."""
    chain: Chain = {}
    fwd: dict[datetime, float] = {}
    for s in summary if isinstance(summary, list) else []:
        p = parse_option_name(str(s.get("instrument_name", ""))) if isinstance(s, dict) else None
        if p is None or p.product != cur:
            continue
        chain.setdefault(p.expiry, {}).setdefault(p.strike, {})["C" if p.is_call else "P"] = p.raw
        f = num(s.get("underlying_price")) or num(s.get("estimated_delivery_price"))
        if f is not None:
            fwd.setdefault(p.expiry, f)
    return chain, fwd


def _bybit_chain(body: Any, cur: str) -> tuple[Chain, int | None]:
    """Ticker rows by expiry/strike/type and the response time (unix ms) of the Bybit v5 option tickers."""
    chain: Chain = {}
    if not isinstance(body, dict):
        return chain, None
    rows = (body.get("result") or {}).get("list") if isinstance(body.get("result"), dict) else None
    for r in rows or []:
        p = parse_bybit_symbol(str(r.get("symbol", ""))) if isinstance(r, dict) else None
        if p is None or p["base"] != cur:
            continue
        chain.setdefault(p["expiry"], {}).setdefault(p["strike"], {})["C" if p["is_call"] else "P"] = r
    t = body.get("time")
    try:
        ts = int(t) if t is not None and not isinstance(t, bool) else None
    except (TypeError, ValueError):
        ts = None
    return chain, ts


async def _scan_currency(rt: "Runtime", b: Builder, cur: str, now: datetime) -> list[dict[str, Any]]:
    summary = await b.guard(f"deribit_options:{cur}", "deribit", rt.deribit.book_summary(cur, "option"))
    d_chain, fwd = _deribit_chain(summary, cur)
    if summary is not None and not d_chain:  # an empty or reshaped answer must not pass for "outside the window"
        b.miss(f"deribit_options:{cur}", "deribit", "data_unavailable", "book summary has no parsable option")
        summary = None
    expiry = pick_daily_expiry(d_chain, now)
    if summary is not None and expiry is None:
        b.warn(f"{cur}: no Deribit expiry {TAU_MIN_HOURS:g}-{TAU_MAX_HOURS:g} h away (normal between 06:00 and 10:00 UTC)")
        return []
    strikes: list[float] = []
    d_quotes: dict[tuple[float, str], tuple[str, dict[str, Any]]] = {}
    if expiry is not None:
        strikes = atm_strikes(d_chain[expiry], fwd[expiry]) if expiry in fwd else []
        names = [(k, t, d_chain[expiry][k][t]) for k in strikes for t in ("C", "P") if t in d_chain[expiry][k]]
        got = await asyncio.gather(*(b.guard(f"deribit_ticker:{n}", "deribit", rt.deribit.ticker(n)) for _, _, n in names))
        d_quotes = {(k, t): (n, deribit_quote(g)) for (k, t, n), g in zip(names, got, strict=True) if isinstance(g, dict)}
    # Bybit right after the Deribit tickers: the two legs as close in time as the APIs allow (both read times stored).
    body = await b.guard(
        f"bybit_options:{cur}", f"ccxt:{BYBIT}", rt.exchanges.public_get(BYBIT, BYBIT_TICKERS, {"category": "option", "baseCoin": cur})
    )
    b_chain, b_ts = _bybit_chain(body, cur)
    if summary is None:  # Deribit down: Bybit picks the expiry and the strikes
        expiry = pick_daily_expiry(b_chain, now)
        if expiry is None:
            if b_chain:
                b.warn(f"{cur}: no Bybit expiry {TAU_MIN_HOURS:g}-{TAU_MAX_HOURS:g} h away")
            return []
        u = [num(r.get("underlyingPrice")) for side in b_chain[expiry].values() for r in side.values()]
        center = next((x for x in u if x is not None), None)
        strikes = atm_strikes(b_chain[expiry], center) if center is not None else []
    if body is not None and expiry not in b_chain:
        b.miss(f"bybit_expiry:{cur}", f"ccxt:{BYBIT}", "data_unavailable", f"Bybit lists no {cur} option expiring {expiry.isoformat()}")
    if not strikes:
        b.miss(f"atm:{cur}", "deribit" if summary is not None else f"ccxt:{BYBIT}", "data_unavailable", "no forward to centre the strikes")
        return []
    b_side = b_chain.get(expiry, {})
    tau = (expiry - now).total_seconds() / 3600.0
    rows: list[dict[str, Any]] = []
    for k in strikes:
        for t in ("C", "P"):
            name, dq = d_quotes.get((k, t), (None, None))
            br = b_side.get(k, {}).get(t)
            if dq is None and br is None:
                continue
            bq = bybit_quote(br, b_ts) if br is not None else None
            row: dict[str, Any] = {
                "currency": cur,
                "expiry_ts": int(expiry.timestamp()),
                "tau_hours": tau,
                "strike": k,
                "opt": t,
                "d_instrument": name,
                "b_symbol": br.get("symbol") if br is not None else None,
            }
            for key in QUOTE_KEYS:
                row[f"d_{key}"] = dq[key] if dq else None
                row[f"b_{key}"] = bq[key] if bq else None
            rows.append(row)
    return rows


async def scan_option_xvenue(rt: "Runtime", b: Builder) -> list[dict[str, Any]]:
    """One snapshot for every currency (see ``Store.OPTION_XVENUE_COLUMNS``). Never raises on a provider failure: it
    lands in ``b.missing`` and the other venue / currency is still stored."""
    now = rt.now()
    rows: list[dict[str, Any]] = []
    for cur in CURRENCIES:
        rows += await _scan_currency(rt, b, cur, now)
    return rows


async def collect_option_xvenue(rt: "Runtime") -> int:
    """Collector job: store one snapshot. Returns the rows written. Like `pm_gap`, it fails (so the scheduler records it)
    only when nothing was stored and a provider reported a problem; outside the 2-20 h window zero rows is normal."""
    if rt.store is None:
        raise RuntimeError("option_xvenue: needs a SQLite store (set TRADEDESK_DB_PATH)")
    await rt.store.init()
    b = Builder()
    rows = await scan_option_xvenue(rt, b)
    if not rows and b.missing:
        raise RuntimeError("option_xvenue: no rows; " + "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:4]))
    if b.missing:
        log.warning("option_xvenue: partial snapshot, missing: %s", "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:6]))
    return await rt.store.insert_option_xvenue(int(rt.now().timestamp()), rows)
