"""Marking of the manual paper book of cross-venue perp-perp funding carry (local bookkeeping only, no orders).

A paper pair is long the perp on one venue and short it on another with the same coin quantity. Marking mirrors
``simulate_perp_spread_portfolio`` (funding at the leg price of the payment, price P&L, fees + slippage, stop at
``1 / leverage``) so paper results are comparable with docs/verification/2026-10-13-funding-carry-perp-perp.md.
The pairs are shown by ``paper_carry_status(strategy='manual')``. The tools that opened and closed them
(carry_paper_open / carry_paper_close) were removed from the MCP server on 2026-11-18 (the collector's engine, books A
and B, replaced the manual paper); pairs already in the store are still marked here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from ..analytics.carry import bar_gaps, carry_snapshot, pair_paper_mark, paper_summary
from ..collector.market_data import BAR_MS, bars_4h, funding_entries, parse_bar
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import STORE_ERRORS, clean_row_flags, clean_row_tags, finite_float, r, reject_bad_id, store_failed, store_ready
from .perps import MAX_FUNDING_RATE, TAKER_FEES

ENTRY_BAR_MS = 15 * 60_000  # the 4h bar containing the entry is checked on 15m bars from the entry on
PAPER_WARNING = (
    "Paper trade, local bookkeeping only: no order is sent. Fills are the last price plus modelled costs "
    "(taker fees + slippage per leg per side), not real fills. Expectation for this strategy is about zero or negative: the earlier "
    "+76 % APR backtest (docs/verification/2026-10-13-funding-carry-perp-perp.md) came from holes in the Bybit funding history; "
    "on the full history (docs/verification/2026-10-17-paper-carry-bench.md, Binance<->Bybit, 2024-10..2026-10) it is -8.7 % APR "
    "on notional at 2x per leg with stop (397 trades, 67 % losing, 130 stopped) and +2.9 % without the stop. Not "
    "modelled: per-symbol position limits, venues changing caps / intervals / leverage, two separate margins that need "
    "top-ups, real liquidation prices (the stop here is the backtest's: either leg's 4h bar high / low moving >= 1 / leverage "
    "from its entry, frozen at that bar). Closed pairs were mirrored into the journal as pp-paper-rule / pp-paper-offrule "
    "until 2026-11-18 (the closing tool was removed then); compare them with the backtest only after many closed trades."
)


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


async def _last_price(rt: Runtime, b: Builder, venue: str, sym: str) -> float | None:
    got = await b.guard(f"ticker:{venue}:{sym}", f"ccxt:{venue}", rt.exchanges.tickers(venue, [sym]))
    try:
        raw = ((got or {}).get(sym) or {}).get("last")
    except AttributeError:  # a ticker that is not a mapping
        raw = None
    try:
        # an "Infinity" last would mark the pair at an infinite price; a 300+-digit one would raise (OverflowError)
        px = finite_float(raw) if raw is not None and not isinstance(raw, bool) else 0.0
    except (TypeError, ValueError, OverflowError):
        b.warn(f"{venue} {sym}: last price in the ticker is not a finite number; not used")
        px = 0.0
    return px if px > 0 else None


def _plausible_entries(b: Builder, venue: str, sym: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`funding_entries(rows)` without rates beyond +-MAX_FUNDING_RATE per interval, with a warning: a finite 1e308 rate
    turned the pair's carry and P&L into NaN. `funding_entries` (collector/, shared with the feed job) drops such rows
    silently by default; here they are counted first so the answer says so."""
    entries = funding_entries(rows, max_rate=None)
    kept = [e for e in entries if abs(e["rate"]) <= MAX_FUNDING_RATE]
    if len(kept) < len(entries):
        b.warn(f"{venue} {sym}: {len(entries) - len(kept)} funding rows with a rate beyond +-{MAX_FUNDING_RATE:g} dropped")
    return kept


async def _entry_bars(rt: Runtime, b: Builder, venue: str, sym: str, opened_ms: int, now_ms: int) -> list[dict[str, Any]] | None:
    """15m bars from the entry's 15m slot to the end of the 4h bar containing the entry (at most 16, one request
    under every venue's cap). None when the request fails: the whole 4h bar is then used (conservative)."""
    start = opened_ms - opened_ms % ENTRY_BAR_MS
    until = min(opened_ms - opened_ms % BAR_MS + BAR_MS - 1, now_ms)
    rows = await b.guard(f"ohlcv_15m:{venue}:{sym}", f"ccxt:{venue}", rt.exchanges.ohlcv(venue, sym, "15m", 20, start, until))
    if rows is None:
        return None
    return [bar for bar in (parse_bar(x) for x in (rows if isinstance(rows, list) else [])) if bar is not None]


async def _leg_data(rt: Runtime, b: Builder, venue: str, sym: str, opened_ms: int, now_ms: int, lookback_ms: int) -> dict[str, Any] | None:
    since = min(opened_ms, now_ms - lookback_ms) - 3_600_000
    hist = await b.guard(
        f"funding_history:{venue}:{sym}", f"ccxt:{venue}", rt.exchanges.funding_rate_history_range(venue, sym, since, now_ms)
    )
    bars = await bars_4h(rt, b, venue, sym, opened_ms - 2 * BAR_MS, now_ms)
    px = await _last_price(rt, b, venue, sym)
    if hist is None or bars is None or px is None:
        return None
    if not hist["complete"]:
        b.warn(
            f"{venue} {sym}: funding history incomplete (page limit, a later page failed, or rows with a malformed timestamp "
            "dropped); funding P&L may be understated"
        )
    entries = _plausible_entries(b, venue, sym, hist["rows"])
    if not entries:
        return None
    fine = await _entry_bars(rt, b, venue, sym, opened_ms, now_ms)
    return {"entries": entries, "bars": bars, "fine": fine, "price": px}


async def mark_trade(rt: Runtime, b: Builder, t: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
    """Mark a stored paper pair with live data; None (and a ``missing`` entry) when any leg's data is unavailable."""
    sym, lv, sv = t["symbol"], t["long_venue"], t["short_venue"]
    opened_ms = int(t["opened_ts"]) * 1000
    lookback_ms = int(float(t["lookback_days"]) * 86_400_000)
    lg, sg = await asyncio.gather(
        _leg_data(rt, b, lv, sym, opened_ms, now_ms, lookback_ms), _leg_data(rt, b, sv, sym, opened_ms, now_ms, lookback_ms)
    )
    if lg is None or sg is None:
        b.miss(f"carry_paper:{t['id']}", "tradedesk", "data_unavailable", f"live data for {sym} on {lv}/{sv} is incomplete")
        return None
    stop_check = "complete"
    for venue, leg in ((lv, lg), (sv, sg)):
        missing = bar_gaps(leg["bars"], opened_ms, now_ms, BAR_MS)
        if not leg["bars"]:
            stop_check = "current_price_only"
            b.warn(f"carry_paper {t['id']}: no 4h bars from {venue}; the stop is checked against the current price only")
        elif missing:
            stop_check = "incomplete" if stop_check == "complete" else stop_check
            b.warn(f"carry_paper {t['id']}: {missing} 4h bar(s) missing on {venue} between open and now; stop detection may be incomplete")
    now_l = carry_snapshot(lg["entries"], now_ms=now_ms, lookback_days=float(t["lookback_days"]))
    now_s = carry_snapshot(sg["entries"], now_ms=now_ms, lookback_days=float(t["lookback_days"]))
    tl, ts_ = now_l.get("trailing_apr"), now_s.get("trailing_apr")
    spread = ts_ - tl if tl is not None and ts_ is not None else None
    m = pair_paper_mark(
        opened_ms=opened_ms,
        now_ms=now_ms,
        notional_usd=float(t["notional_usd"]),
        long_entry=float(t["long_entry"]),
        short_entry=float(t["short_entry"]),
        long_funding=lg["entries"],
        short_funding=sg["entries"],
        long_bars=lg["bars"],
        short_bars=sg["bars"],
        long_now=lg["price"],
        short_now=sg["price"],
        spread_apr=spread,
        fee_long=TAKER_FEES.get(lv, 0.0005),
        fee_short=TAKER_FEES.get(sv, 0.0005),
        slippage=float(t.get("slippage") if t.get("slippage") is not None else 0.0005),
        stop_move=float(t["stop_move"]),
        exit_apr=float(t["exit_apr"]),
        long_entry_bars=lg["fine"],
        short_entry_bars=sg["fine"],
        entry_bar_ms=ENTRY_BAR_MS,
    )
    coarse = [v for v, k in ((lv, "long_entry_fine"), (sv, "short_entry_fine")) if not m[k]]
    if coarse:
        b.warn(
            f"carry_paper {t['id']}: 15m bars of the entry bar unavailable or incomplete on {', '.join(coarse)}; the whole 4h "
            "entry bar is used, so prices from before the entry may trigger the stop (conservative)"
        )
    return {**m, "long_now": lg["price"], "short_now": sg["price"], "stop_check": stop_check}


def _clean_pair_tags(b: Builder, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`rows` with `tags` as a list of strings, in place, so no pair is hidden (item 356): tags 300 levels deep failed the
    MCP serialiser after the former carry_paper_close had written the close and its journal mirror, a number raised TypeError in the
    close. Anything that is not a list of strings is shown as its string items, NaN / Infinity in tags as []; warnings.
    `entry_flags` is normalised the same way (item 357)."""
    nonfinite, fixed = clean_row_tags(rows)
    for t in rows:
        if t.get("id") in nonfinite:
            t["tags"] = []
    if fixed:
        b.warn(f"stored paper pair id {fixed[:10]} had tags that were not a list of strings; shown as their string items")
    if nonfinite:
        b.warn(f"stored paper pair id {nonfinite[:10]} had NaN / Infinity in tags; shown as no tags")
    # entry_flags the same way (300 levels deep failed the reply of the manual book status / carry_paper_close, item 357).
    nonfinite, fixed = clean_row_flags(rows)
    for t in rows:
        if t.get("id") in nonfinite:
            t["entry_flags"] = []
    if fixed:
        b.warn(f"stored paper pair id {fixed[:10]} had entry_flags that were not a list of strings; shown as their string items")
    if nonfinite:
        b.warn(f"stored paper pair id {nonfinite[:10]} had NaN / Infinity in entry_flags; shown as no flags")
    return rows


def _view(t: dict[str, Any], m: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": t["id"],
        "status": t["status"],
        "base": t["base"],
        "long_venue": t["long_venue"],
        "short_venue": t["short_venue"],
        "opened": _iso(t["opened_ts"]),
        "notional_usd": t["notional_usd"],
        "qty": r(t["qty"], 8),
        "long_entry": t["long_entry"],
        "short_entry": t["short_entry"],
        "leverage": t["leverage"],
        "stop_move": r(t["stop_move"], 4),
        "exit_apr": t["exit_apr"],
        "entry_trailing_spread_apr": r(t.get("entry_trailing_spread_apr"), 4),
        "entry_flags": t.get("entry_flags") or [],
        "tags": t.get("tags") or [],
    }
    if m is not None:
        out["mark"] = {
            "long_now": m["long_now"],
            "short_now": m["short_now"],
            "held_days": r(m["held_days"], 2),
            "funding_pnl_usd": r(m["funding_pnl"], 2),
            "price_pnl_usd": r(m["price_pnl"], 2),
            "costs_usd": r(m["costs"], 2),
            "pnl_usd": r(m["pnl_usd"], 2),
            "return_on_notional": r(m["return_on_notional"], 5),
            "realized_apr": r(m["realized_apr"], 3),
            "n_funding_payments": m["n_funding_payments"],
            "max_up": r(m["max_up"], 4),
            "max_down": r(m["max_down"], 4),
            "trailing_spread_apr": r(m["spread_apr"], 4),
            "stopped": m["stopped"],
            "stop_at": None if m["stop_ts_ms"] is None else _iso(m["stop_ts_ms"] // 1000),
            "long_price_used": m["long_x"],
            "short_price_used": m["short_x"],
            "stop_check": m["stop_check"],
        }
        out["exit_signal"] = m["exit_signal"]
    return out


def _closed_view(t: dict[str, Any]) -> dict[str, Any]:
    out = _view(t, None)
    out.update(
        {
            "closed": _iso(t.get("closed_ts")),
            "stop_at": _iso(t.get("stop_ts")),
            "entry_tradable": t.get("entry_tradable"),
            "exit_reason": t.get("exit_reason"),
            "stopped": t.get("stopped"),
            "long_exit": t.get("long_exit"),
            "short_exit": t.get("short_exit"),
            "funding_pnl_usd": r(t.get("funding_pnl"), 2),
            "price_pnl_usd": r(t.get("price_pnl"), 2),
            "costs_usd": r(t.get("costs"), 2),
            "pnl_usd": r(t.get("pnl_usd"), 2),
            "return_on_notional": r(t.get("return_on_notional"), 5),
            "journal_id": t.get("journal_id"),
        }
    )
    return out


async def manual_book_status(rt: Runtime, id: int | None, include_closed: bool) -> dict[str, Any]:
    """Envelope of paper_carry_status(strategy='manual'): the pairs opened with the former carry_paper_open tool (removed
    2026-11-18), marked with live data.
    The body of the former carry_paper_status tool (merged into paper_carry_status 2026-11-17), unchanged."""
    b = Builder()
    b.warn(PAPER_WARNING)
    if (bad := reject_bad_id(b, id=id)) is not None:  # 10**20 raised OverflowError out of the lookup
        return bad
    if not await store_ready(b, rt, "the journal"):
        return b.build(None, ok=False)
    try:
        now_ms = int(rt.now().timestamp() * 1000)
        if id is not None:
            one = await rt.store.carry_paper_get(id)
            if one is None:
                b.miss("carry_paper", "store", "data_unavailable", f"paper pair id={id} not found")
                return b.build(None, ok=False)
            rows = [one]
        else:
            rows = await rt.store.carry_paper_list(status="open")
        rows = _clean_pair_tags(b, rows)
        open_rows = [x for x in rows if x["status"] == "open"]
        sem = asyncio.Semaphore(3)

        async def one_mark(t: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                m = await mark_trade(rt, b, t, now_ms)
            return _view(t, m)

        views = list(await asyncio.gather(*[one_mark(t) for t in open_rows]))
        closed_all = await rt.store.carry_paper_list(status="closed", limit=5000)
        if include_closed and id is None:  # only these closed rows are shown
            _clean_pair_tags(b, closed_all[:200])
        data: dict[str, Any] = {"open": views, "summary_closed": paper_summary(closed_all)}
        if id is not None and rows[0]["status"] != "open":
            data["trade"] = _closed_view(rows[0])
        if include_closed and id is None:
            data["closed"] = [_closed_view(t) for t in closed_all[:200]]
        for s in (data["summary_closed"], data["summary_closed"]["entry_tradable"]):
            for k in ("mean_return", "median_return", "share_losing", "share_stopped"):
                if k in s:
                    s[k] = r(s[k], 5)
            for k in ("mean_held_days",):
                if k in s:
                    s[k] = r(s[k], 2)
            for k in ("sum_funding_usd", "sum_price_usd", "sum_costs_usd", "sum_pnl_usd"):
                if k in s:
                    s[k] = r(s[k], 2)
        ok = not open_rows or any("mark" in v for v in views) or (include_closed and bool(closed_all))
        return b.build(data, ok=ok)
    except STORE_ERRORS as e:
        return store_failed(b, e)
