"""Deribit <-> Bybit daily option quotes: hourly snapshot of the nearest daily expiry, ATM +- 3 strikes, calls and puts,
plus a minute mode (ATM +- 2, trigger ATM +- 1) for 30 minutes after a crossing.

Docs: docs/ideas/IDEA-20261023-bybit-deribit-daily-option-dislocation-forward.md (forward card; measurement only, nothing
here trades). Read-only public data through the shared providers: the Deribit option book summary and per-instrument
tickers (`rt.deribit`, own rate limit) and the Bybit v5 option tickers through ccxt (`rt.exchanges.public_get`, i.e.
`Exchanges._call`). One row per (currency, strike, call/put) with both venues side by side, the venues' quote times
(`d_ts_ms`, `b_ts_ms`) and the local receive time of each leg (`d_recv_ms`, `b_recv_ms`: ``rt.now()`` right after the
response came back, unix ms; NULL when the leg failed or was served from the in-process cache), so the analysis can
keep only synchronous pairs by either measure. The crossing itself is computed at read time
(`analytics/option_xvenue.crossing_from_row`). Off by default: TRADEDESK_COLLECT_OPTION_XVENUE_ENABLED=1, which must
wait until the threshold is registered in its own commit (card condition 3).

Strikes come from Deribit (ATM +- 3 around the expiry's forward); a Bybit strike that Deribit does not list cannot cross
and is not stored. When the Deribit summary fails, the Bybit chain picks the expiry and strikes instead. A venue without
the chosen expiry (e.g. Bybit without daily options) leaves its columns NULL and lands in ``missing``.

Leg sync: the Bybit chain request goes out in the same ``asyncio.gather`` as the Deribit tickers (Bybit first, then the
Deribit tickers nearest the forward first, all under the Deribit rate limit), so the whole read takes ~0.4-0.6 s (live
23.10 04:40 UTC). Caveat seen live: Deribit's ticker ``timestamp`` (``d_ts_ms``) is the instrument's last update, not
the read time: 0.3-5.7 s BEFORE the request was sent at an RTT of ~30 ms (review probe 23.10 ~04:45 UTC, a quiet hour),
while Bybit's ``time`` (``b_ts_ms``) is its response time; so |b_ts_ms - d_ts_ms| also contains the age of an unchanged
Deribit quote, not only the read gap. ``d_recv_ms`` / ``b_recv_ms`` measure the read gap itself; which of the two is the
primary sync measure is the quant's call (the registered class A is still |b_ts_ms - d_ts_ms| <= 5 s).

Minute mode (card «Час життя»): the scheduler ticks :class:`OptionXvenueJob` every minute. A tick takes the hourly
snapshot when it is due; otherwise, for each currency whose minute window is open, a minute snapshot of the ATM +- 2
strikes; otherwise it reads nothing. Any snapshot with a crossing by the registered rule
(`analytics/option_xvenue.snapshot_crossings`: class A, ATM +- 1, adjusted measure) (re)opens that currency's window for
30 minutes. ATM +- 2 is for tracking only: an episode found on an outer ATM +- 1 strike stays in view when the forward
moves by about one strike step (a larger move within the window still loses it: censored, not "gone"). Every row has
``reg_atm`` (1 = in the snapshot's registered ATM +- 1 by `analytics/option_xvenue.registered_rows`, the same set the
trigger uses; else 0).
Rows carry ``mode`` ('hourly' | 'minute'): the registered frequency p uses only 'hourly'; the lifetime series is every
row (both modes) inside a window. The window state is rebuilt from the store after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..analytics.instruments import parse_option_name
from ..analytics.option_xvenue import (
    MINUTE_STRIKES_EACH_SIDE,
    MINUTE_TICK_SECONDS,
    MINUTE_WINDOW_SECONDS,
    STRIKES_EACH_SIDE,
    TAU_MAX_HOURS,
    TAU_MIN_HOURS,
    atm_strikes,
    bybit_quote,
    deribit_quote,
    extend_windows,
    num,
    parse_bybit_symbol,
    pick_daily_expiry,
    plan_tick,
    registered_rows,
    seed_schedule,
    snapshot_crossings,
)
from ..core.envelope import Builder, Fetched

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger("tradedesk.collector")

CURRENCIES = ("BTC", "ETH")
BYBIT = "bybit"
BYBIT_TICKERS = "publicGetV5MarketTickers"  # GET /v5/market/tickers?category=option&baseCoin=<COIN>
BYBIT_SETTLE = "USDT"  # symbol suffix kept (``BTC-23OCT26-82000-C-USDT``); others (no suffix = USDC) are skipped + warned
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


def _bybit_chain(body: Any, cur: str, b: Builder, settle: str = BYBIT_SETTLE) -> tuple[Chain, int | None]:
    """Ticker rows by expiry/strike/type and the response time (unix ms) of the Bybit v5 option tickers. Only symbols
    settled in ``settle`` are kept, so a strike listed in two settle currencies is never silently overwritten; any other
    suffix seen is reported once through ``b.warn``."""
    chain: Chain = {}
    if not isinstance(body, dict):
        return chain, None
    rows = (body.get("result") or {}).get("list") if isinstance(body.get("result"), dict) else None
    other: set[str] = set()
    for r in rows or []:
        p = parse_bybit_symbol(str(r.get("symbol", ""))) if isinstance(r, dict) else None
        if p is None or p["base"] != cur:
            continue
        if p["settle"] != settle:
            other.add(p["settle"] or "none (USDC)")
            continue
        chain.setdefault(p["expiry"], {}).setdefault(p["strike"], {})["C" if p["is_call"] else "P"] = r
    if other:
        msg = f"{cur}: Bybit option symbols with settle suffix {', '.join(sorted(other))} skipped (only {settle} is stored)"
        b.warn(msg)
        log.warning("option_xvenue: %s", msg)
    t = body.get("time")
    try:
        ts = int(t) if t is not None and not isinstance(t, bool) else None
    except (TypeError, ValueError):
        ts = None
    return chain, ts


async def _received(rt: "Runtime", call: Awaitable[Fetched]) -> Fetched:
    """``call`` with its data replaced by ``(data, recv_ms)``: the local receive time, ``rt.now()`` in unix ms right after
    the response came back (after any rate-limit wait and retry). None when the answer came from the in-process cache
    (its real receive time is unknown). Errors pass through to ``Builder.guard``."""
    f = await call
    recv = None if f.evidence.cached else int(rt.now().timestamp() * 1000)
    return Fetched((f.data, recv), f.evidence)


async def _scan_currency(rt: "Runtime", b: Builder, cur: str, now: datetime, each_side: int) -> list[dict[str, Any]]:
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
    names: list[tuple[float, str, str]] = []
    if expiry is not None and expiry in fwd:
        strikes = atm_strikes(d_chain[expiry], fwd[expiry], each_side)
        names = [(k, t, d_chain[expiry][k][t]) for k in strikes for t in ("C", "P") if t in d_chain[expiry][k]]
        names.sort(key=lambda x: abs(x[0] - fwd[expiry]))  # the registered ATM +- 1 first in the rate-limit queue
    # One gather: the Bybit chain leaves first, the Deribit tickers right behind it (Http's token bucket paces them).
    bybit_call = rt.exchanges.public_get(BYBIT, BYBIT_TICKERS, {"category": "option", "baseCoin": cur})
    bybit_req = b.guard(f"bybit_options:{cur}", f"ccxt:{BYBIT}", _received(rt, bybit_call))
    tickers = (b.guard(f"deribit_ticker:{n}", "deribit", _received(rt, rt.deribit.ticker(n))) for _, _, n in names)
    b_got, *got = await asyncio.gather(bybit_req, *tickers)
    d_quotes = {
        (k, t): (n, deribit_quote(g[0]), g[1]) for (k, t, n), g in zip(names, got, strict=True) if g is not None and isinstance(g[0], dict)
    }
    body, b_recv = b_got if b_got is not None else (None, None)
    b_chain, b_ts = _bybit_chain(body, cur, b)
    if summary is None:  # Deribit down: Bybit picks the expiry and the strikes
        expiry = pick_daily_expiry(b_chain, now)
        if expiry is None:
            if b_chain:
                b.warn(f"{cur}: no Bybit expiry {TAU_MIN_HOURS:g}-{TAU_MAX_HOURS:g} h away")
            return []
        u = [num(r.get("underlyingPrice")) for side in b_chain[expiry].values() for r in side.values()]
        center = next((x for x in u if x is not None), None)
        strikes = atm_strikes(b_chain[expiry], center, each_side) if center is not None else []
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
            name, dq, d_recv = d_quotes.get((k, t), (None, None, None))
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
            row["d_recv_ms"] = d_recv if dq else None
            row["b_recv_ms"] = b_recv if bq else None
            rows.append(row)
    reg = {id(r) for r in registered_rows(rows)}  # the trigger's ATM +- 1 (snapshot_crossings uses the same function)
    for r in rows:
        r["reg_atm"] = int(id(r) in reg)
    return rows


async def scan_option_xvenue(
    rt: "Runtime",
    b: Builder,
    now: datetime | None = None,
    *,
    currencies: tuple[str, ...] = CURRENCIES,
    each_side: int = STRIKES_EACH_SIDE,
) -> list[dict[str, Any]]:
    """One snapshot for ``currencies`` (see ``Store.OPTION_XVENUE_COLUMNS``), ATM +- ``each_side`` strikes. ``now``
    (default ``rt.now()``) is the snapshot time: ``tau_hours`` is measured from it, and the job stores it as the row
    ``ts``. Never raises on a provider failure: it lands in ``b.missing`` and the other venue / currency is still stored."""
    now = now or rt.now()
    rows: list[dict[str, Any]] = []
    for cur in currencies:
        rows += await _scan_currency(rt, b, cur, now, each_side)
    return rows


async def _store(rt: "Runtime", b: Builder, now: datetime, mode: str, rows: list[dict[str, Any]]) -> int:
    """Write one snapshot. Like `pm_gap`, it fails (so the scheduler records it) only when nothing was stored and a
    provider reported a problem; outside the 2-20 h window zero rows is normal."""
    assert rt.store is not None
    if not rows and b.missing:
        raise RuntimeError(f"option_xvenue ({mode}): no rows; " + "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:4]))
    if b.missing:
        log.warning("option_xvenue (%s): partial snapshot, missing: %s", mode, "; ".join(f"{m.field}: {m.reason}" for m in b.missing[:6]))
    return await rt.store.insert_option_xvenue(int(now.timestamp()), rows, mode=mode)


async def collect_option_xvenue(rt: "Runtime") -> int:
    """One hourly snapshot (ATM +- 3, both currencies, ``mode = 'hourly'``), regardless of the minute-mode schedule.
    Returns the rows written. The scheduler uses :class:`OptionXvenueJob` instead."""
    if rt.store is None:
        raise RuntimeError("option_xvenue: needs a SQLite store (set TRADEDESK_DB_PATH)")
    await rt.store.init()
    b = Builder()
    now = rt.now()  # one clock read: the row ts and the base of tau_hours agree
    return await _store(rt, b, now, "hourly", await scan_option_xvenue(rt, b, now))


class OptionXvenueJob:
    """Collector job ticked every :data:`MINUTE_TICK_SECONDS`: hourly snapshot when due, else minute snapshots of the
    currencies with an open window, else nothing (see the module docstring). One ``rt.now()`` read per tick is the row
    ``ts``, the base of ``tau_hours`` and the clock of the schedule. State: ``last_hourly`` and ``minute_until`` (per
    currency), seeded from the store on the first tick. Returns the rows written; raises only as ``_store`` does."""

    def __init__(
        self, hourly_seconds: float = 3600, window_seconds: float = MINUTE_WINDOW_SECONDS, tick_seconds: float = MINUTE_TICK_SECONDS
    ):
        self.hourly_seconds = hourly_seconds
        self.window_seconds = window_seconds
        self.tick_seconds = tick_seconds
        self.last_hourly: datetime | None = None
        self.minute_until: dict[str, datetime] = {}
        self._seeded = False

    async def __call__(self, rt: "Runtime") -> int:
        if rt.store is None:
            raise RuntimeError("option_xvenue: needs a SQLite store (set TRADEDESK_DB_PATH)")
        await rt.store.init()
        now = rt.now()
        if not self._seeded:
            since = int(now.timestamp() - max(self.hourly_seconds, self.window_seconds))
            self.last_hourly, self.minute_until = seed_schedule(
                await rt.store.option_xvenue_rows(since), window_seconds=self.window_seconds
            )
            self._seeded = True
        mode, currencies = plan_tick(
            now,
            self.last_hourly,
            self.minute_until,
            hourly_seconds=self.hourly_seconds,
            currencies=CURRENCIES,
            tick_seconds=self.tick_seconds,
        )
        if mode is None:
            return 0
        if mode == "hourly":
            self.last_hourly = now  # before the reads: a failed hourly snapshot is not retried every minute
        b = Builder()
        each_side = STRIKES_EACH_SIDE if mode == "hourly" else MINUTE_STRIKES_EACH_SIDE  # trigger: ATM +- 1 either way
        rows = await scan_option_xvenue(rt, b, now, currencies=currencies, each_side=each_side)
        hits = snapshot_crossings(rows)
        for cur in sorted({h[0] for h in hits}):
            if self.minute_until.get(cur, now) <= now:
                log.info(
                    "option_xvenue: %s crossing %s, minute mode for %g min",
                    cur,
                    [h[1:] for h in hits if h[0] == cur],
                    self.window_seconds / 60,
                )
        self.minute_until = extend_windows(self.minute_until, hits, now, self.window_seconds)
        return await _store(rt, b, now, mode, rows)
