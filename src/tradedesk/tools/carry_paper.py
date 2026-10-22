"""Paper tracker for cross-venue perp-perp funding carry (local bookkeeping only, no orders).

A paper pair is long the perp on one venue and short it on another with the same coin quantity. Marking mirrors
``simulate_perp_spread_portfolio`` (funding at the leg price of the payment, price P&L, fees + slippage, stop at
``1 / leverage``) so paper results are comparable with docs/verification/2026-10-13-funding-carry-perp-perp.md.
Closed pairs are mirrored into the ``journal`` as one synthetic pair index tagged ``pp-paper``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.carry import bar_gaps, carry_snapshot, pair_paper_mark, paper_summary, spread_snapshot
from ..analytics.journal import outcome_of
from ..analytics.journal import pnl as pnl_fn
from ..collector.market_data import BAR_MS, bars_4h, funding_entries
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r
from .journal import _store_guard
from .perps import TAKER_FEES, current_funding

MISMATCH_GAP = 0.20
PAPER_WARNING = (
    "Paper trade, local bookkeeping only: no order is sent. Fills are the last (or mark) price plus modelled costs "
    "(taker fees + slippage per leg per side), not real fills. Expectation for this strategy is about zero or negative: the earlier "
    "+76 % APR backtest (docs/verification/2026-10-13-funding-carry-perp-perp.md) came from holes in the Bybit funding history; "
    "on the full history (docs/verification/2026-10-17-paper-carry-bench.md, Binance<->Bybit, 2024-10..2026-10) it is -8.7 % APR "
    "on notional at 2x per leg with stop (397 trades, 67 % losing, 130 stopped) and +2.9 % without the stop. Not "
    "modelled: per-symbol position limits, venues changing caps / intervals / leverage, two separate margins that need "
    "top-ups, real liquidation prices (the stop here is the backtest's: either leg's 4h bar high / low moving >= 1 / leverage "
    "from its entry, frozen at that bar). Compare journal_stats(tag='pp-paper') with the backtest only after many closed trades."
)
Venue = Literal["binanceusdm", "bybit", "okx"]


def _symbol(base: str) -> str:
    return f"{base.strip().upper()}/USDT:USDT"


def _iso(ts: int | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


async def _last_price(rt: Runtime, b: Builder, venue: str, sym: str, fallback: float | None = None) -> float | None:
    got = await b.guard(f"ticker:{venue}:{sym}", f"ccxt:{venue}", rt.exchanges.tickers(venue, [sym]))
    try:
        px = float(((got or {}).get(sym) or {}).get("last") or 0.0)
    except (TypeError, ValueError):
        px = 0.0
    if px > 0:
        return px
    if fallback:
        b.warn(f"{venue} {sym}: last price unavailable, using the mark price from the funding feed instead")
    return fallback


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
        b.warn(f"{venue} {sym}: funding history incomplete (page limit or a later page failed); funding P&L may be understated")
    entries = funding_entries(hist["rows"])
    if not entries:
        return None
    return {"entries": entries, "bars": bars, "price": px}


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
    )
    return {**m, "long_now": lg["price"], "short_now": sg["price"], "stop_check": stop_check}


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


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # carry_paper_open writes to the LOCAL store only — no orders, no external effects
        description=(
            "Open a PAPER perp-perp funding-carry pair in the local store: long the perp on `long_venue`, short it on "
            "`short_venue` (binanceusdm / bybit / okx USDT perps), same coin quantity = notional_usd / long price. Entry "
            "prices are the last prices now; nothing is sent to any venue. Records the trailing and current funding spread "
            "(decimals, APR on notional, in the held direction) and the funding_spread_scan flags. Refuses (ok=false, "
            "nothing written) when the price gap between the venues exceeds `max_entry_gap` or looks like different tokens "
            "(> 20 %), as the backtest never opens those. If the pair is not `tradable_now` or opposes the scan's direction "
            "it is still recorded, with a warning. Stop distance = 1 / `leverage`. Close with carry_paper_close; track with "
            "carry_paper_status. See `warnings`: the backtest median trade was negative."
        ),
    )
    async def carry_paper_open(
        base: Annotated[str, Field(description="Base asset, e.g. WIF, BTC.")],
        long_venue: Annotated[Venue, Field(description="Venue where the paper long perp is held (the lower-funding side).")],
        short_venue: Annotated[Venue, Field(description="Venue where the paper short perp is held (the higher-funding side).")],
        notional_usd: Annotated[float, Field(gt=0, le=10_000_000, description="Notional per leg in USD.")] = 1000.0,
        leverage: Annotated[
            float, Field(ge=1, le=10, description="Isolated leverage per leg; the stop is 1 / leverage against a leg.")
        ] = 2.0,
        lookback_days: Annotated[float, Field(ge=1, le=30, description="Trailing window for the spread, days.")] = 7.0,
        exit_apr: Annotated[float, Field(ge=-1, le=50, description="Exit signal when the trailing spread falls to this APR.")] = 0.0,
        max_entry_gap: Annotated[float, Field(ge=0, le=0.2, description="Largest price gap between the legs, fraction.")] = 0.02,
        slippage: Annotated[float, Field(ge=0, le=0.01, description="Slippage per leg per side, fraction of notional.")] = 0.0005,
        note: Annotated[str | None, Field(description="Free-form note.")] = None,
        tags: Annotated[list[str] | None, Field(description="Free-form tags (journal gets them plus 'pp-paper').")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(PAPER_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        if long_venue == short_venue:
            b.miss("venues", "tradedesk", "config", "long_venue and short_venue must differ")
            return b.build(None, ok=False)
        await rt.store.init()
        base_u, sym = base.strip().upper(), _symbol(base)
        now = rt.now()
        now_ms = int(now.timestamp() * 1000)
        since_ms = now_ms - int(max(30.0, lookback_days) * 86_400_000)

        async def leg(v: str) -> dict[str, Any] | None:
            mk = await b.guard(f"perp_markets:{v}", f"ccxt:{v}", rt.exchanges.markets(v)) or {}
            cur = (await current_funding(rt, b, v, mk)).get(sym)
            hist = await b.guard(
                f"funding_history:{v}:{sym}", f"ccxt:{v}", rt.exchanges.funding_rate_history_range(v, sym, since_ms, now_ms)
            )
            px = await _last_price(rt, b, v, sym, fallback=(cur or {}).get("mark"))
            entries = funding_entries(hist["rows"]) if hist else []
            if cur is None or not entries or px is None:
                b.miss(
                    f"carry_paper_leg:{v}",
                    "tradedesk",
                    "data_unavailable",
                    f"{sym} on {v}: current funding, funding history or price missing",
                )
                return None
            if hist and not hist["complete"]:
                b.warn(f"{v} {sym}: funding history incomplete (page limit or a later page failed)")
            return {
                "entries": entries,
                "price": px,
                "rate": cur["rate"],
                "interval_hours": cur["interval_hours"],
                "mark": cur["mark"],
                "cap": cur["cap"],
            }

        lg, sg = await asyncio.gather(leg(long_venue), leg(short_venue))
        if lg is None or sg is None:
            return b.build(None, ok=False)
        snap = spread_snapshot(
            {long_venue: lg, short_venue: sg},
            now_ms=now_ms,
            lookback_days=lookback_days,
            entry_apr=0.10,
            exit_apr=exit_apr,
            fees=TAKER_FEES,
            slippage=slippage,
            max_entry_gap=max_entry_gap,
            mismatch_gap=MISMATCH_GAP,
        )
        pair = snap["pair"]
        if pair is None:
            b.miss("spread", "tradedesk", "data_unavailable", "not enough funding history on both venues to compute the trailing spread")
            return b.build(None, ok=False)
        gap = sg["price"] / lg["price"] - 1.0
        if abs(gap) > MISMATCH_GAP or "price_mismatch" in pair["flags"]:
            b.miss(
                "price_mismatch", "tradedesk", "refused", f"{base_u}: legs differ by {gap:.1%}; likely different tokens under one ticker"
            )
            return b.build(None, ok=False)
        if abs(gap) > max_entry_gap or "price_gap" in pair["flags"]:
            b.miss("price_gap", "tradedesk", "refused", f"{base_u}: legs differ by {gap:.2%} > max_entry_gap {max_entry_gap:.2%}")
            return b.build(None, ok=False)

        v = snap["venues"]
        trailing = v[short_venue]["trailing_apr"] - v[long_venue]["trailing_apr"]
        cs, cl = v[short_venue]["current_apr"], v[long_venue]["current_apr"]
        current = cs - cl if cs is not None and cl is not None else None
        same_dir = pair["short_venue"] == short_venue and pair["long_venue"] == long_venue
        flags = [f for f in pair["flags"] if f != "current_spread_reversed"]
        if current is not None and current < 0:
            flags.append("current_spread_reversed")
        tradable = same_dir and pair["tradable_now"]
        if not same_dir:
            flags.append("opposite_to_scan_direction")
            b.warn(
                f"requested long {long_venue} / short {short_venue} is opposite to the scan direction "
                f"(long {pair['long_venue']} / short {pair['short_venue']}); trailing spread in the held direction is {trailing:+.1%}"
            )
        if not tradable:
            b.warn(f"{base_u} is not tradable_now (status {pair['status']}, flags {flags or 'none'}); recorded anyway")
        dupes = [
            x["id"]
            for x in await rt.store.carry_paper_list(status="open")
            if x["base"] == base_u and x["long_venue"] == long_venue and x["short_venue"] == short_venue
        ]
        if dupes:
            b.warn(f"open paper pair(s) {dupes} already exist for {base_u} long {long_venue} / short {short_venue}; recording another")
        trade_id = await rt.store.carry_paper_insert(
            opened_ts=int(now.timestamp()),
            base=base_u,
            symbol=sym,
            long_venue=long_venue,
            short_venue=short_venue,
            qty=notional_usd / lg["price"],
            notional_usd=notional_usd,
            long_entry=lg["price"],
            short_entry=sg["price"],
            entry_trailing_spread_apr=trailing,
            entry_current_spread_apr=current,
            entry_flags=flags,
            entry_tradable=tradable,
            leverage=leverage,
            stop_move=1.0 / leverage,
            lookback_days=lookback_days,
            exit_apr=exit_apr,
            slippage=slippage,
            note=note,
            tags=tags,
        )
        return b.build(
            {
                "id": trade_id,
                "base": base_u,
                "symbol": sym,
                "long_venue": long_venue,
                "short_venue": short_venue,
                "notional_usd": notional_usd,
                "qty": r(notional_usd / lg["price"], 8),
                "long_entry": lg["price"],
                "short_entry": sg["price"],
                "price_gap": r(gap, 5),
                "trailing_spread_apr": r(trailing, 4),
                "current_spread_apr": r(current, 4),
                "tradable_now": tradable,
                "flags": flags,
                "leverage": leverage,
                "stop_move": r(1.0 / leverage, 4),
                "exit_apr": exit_apr,
                "roundtrip_cost": r(pair["roundtrip_cost"], 5),
            }
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Mark PAPER perp-perp carry pairs with live data (local store + public market data; nothing is traded). Per open "
            "pair: funding P&L (long leg pays, short leg receives, at each payment's leg price), price P&L, costs (entry + "
            "estimated exit fees and slippage), net P&L in USD and as a fraction of notional, held days, realized APR, the "
            "largest adverse move of each leg since entry, the trailing spread now, and `exit_signal` (null / 'spread_exit' "
            "when the trailing spread <= exit_apr / 'stop'). The stop is the backtest's: the first 4h bar where either leg's "
            "high or low is >= 1 / leverage away from that leg's entry; the result is then FROZEN at that bar (funding up to "
            "it, price P&L at its close, exit costs and the 1 % penalty) with `stop_at`, and later moves do not count. "
            "`stop_check` says whether bar coverage was complete. `id` marks a single pair. `summary_closed` covers closed "
            "pairs: n, mean / median return on notional, share losing, share stopped, mean held days, sums of funding / "
            "price / costs in USD, and the same under `entry_tradable` for pairs opened while tradable_now held (the "
            "backtest-comparable subset). A pair whose data cannot be fetched goes to `missing`; the others are still "
            "returned; ok=false when no open pair could be marked."
        ),
    )
    async def carry_paper_status(
        id: Annotated[int | None, Field(ge=1, description="Mark only this pair (any status).")] = None,
        include_closed: Annotated[bool, Field(description="Also list closed / cancelled pairs.")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(PAPER_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        now_ms = int(rt.now().timestamp() * 1000)
        if id is not None:
            one = await rt.store.carry_paper_get(id)
            if one is None:
                b.miss("carry_paper", "store", "data_unavailable", f"paper pair id={id} not found")
                return b.build(None, ok=False)
            rows = [one]
        else:
            rows = await rt.store.carry_paper_list(status="open")
        open_rows = [x for x in rows if x["status"] == "open"]
        sem = asyncio.Semaphore(3)

        async def one_mark(t: dict[str, Any]) -> dict[str, Any]:
            async with sem:
                m = await mark_trade(rt, b, t, now_ms)
            return _view(t, m)

        views = list(await asyncio.gather(*[one_mark(t) for t in open_rows]))
        closed_all = await rt.store.carry_paper_list(status="closed", limit=5000)
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

    @server.tool(
        annotations=READ_ONLY,  # carry_paper_close writes to the LOCAL store only — no orders
        description=(
            "Close a PAPER perp-perp carry pair at live last prices (local store only, nothing is traded). `reason` "
            "('signal' / 'stop' / 'manual') records your intent only. Whether the pair was stopped comes from the bar data "
            "(see carry_paper_status): a stopped pair is closed at its stop bar (funding, price P&L and exit costs frozen "
            "there, plus the 1 % of notional penalty) whatever `reason` says, and reason='stop' without a stop in the data "
            "adds no penalty (warning). The close is atomic: a repeated or concurrent close returns ok=false and writes "
            "nothing. Stores funding / price / cost P&L in USD and the return on notional, "
            "and mirrors the pair into the journal as symbol 'PP:<BASE>:<long_venue>/<short_venue>' with tags ['pp-paper', "
            "...] (side long, size = notional, entry 1.0, exit = 1 + return) so journal_stats(tag='pp-paper') reports it: "
            "journal PnL is in USD on the notional. `cancelled=true` drops the pair without a P&L or journal row. If live "
            "data is unavailable the pair stays open and ok=false."
        ),
    )
    async def carry_paper_close(
        id: Annotated[int, Field(ge=1, description="Pair id from carry_paper_open.")],
        reason: Annotated[Literal["signal", "stop", "manual"], Field(description="Why the pair is closed.")],
        cancelled: Annotated[bool, Field(description="Mark as cancelled instead of closing (no P&L, no journal row).")] = False,
        note: Annotated[str | None, Field(description="Post-mortem note.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(PAPER_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        t = await rt.store.carry_paper_get(id)
        if t is None:
            b.miss("carry_paper", "store", "data_unavailable", f"paper pair id={id} not found")
            return b.build(None, ok=False)
        if t["status"] != "open":
            b.miss("carry_paper", "store", "data_unavailable", f"paper pair id={id} is already {t['status']}")
            return b.build(None, ok=False)
        now = rt.now()
        ts = int(now.timestamp())
        already = ("carry_paper", "store", "data_unavailable", f"paper pair id={id} was closed by another call; nothing written")
        if cancelled:
            if not await rt.store.carry_paper_close(id, status="cancelled", closed_ts=ts, exit_reason=reason, note=note):
                b.miss(*already)
                return b.build(None, ok=False)
            return b.build(_closed_view(await rt.store.carry_paper_get(id)))
        m = await mark_trade(rt, b, t, ts * 1000)
        if m is None:
            return b.build(None, ok=False)
        # `stopped` and the penalty come from the data, never from `reason`; the result is frozen at the stop bar.
        stopped = bool(m["stopped"])
        if reason == "stop" and not stopped:
            b.warn(f"reason='stop' but no stop is found in the data for pair {id}: recorded as not stopped, no stop penalty")
        if stopped and reason != "stop":
            b.warn(f"pair {id} hit its stop at {_iso(m['stop_ts_ms'] // 1000)}: recorded as stopped (frozen there, penalty charged)")
        if m["stop_check"] != "complete":
            b.warn(f"pair {id}: bar coverage is {m['stop_check']}; a stop may have been missed")
        close_ts = m["stop_ts_ms"] // 1000 if stopped else ts
        ret, pnl_usd = m["return_on_notional"], m["pnl_usd"]
        symbol = f"PP:{t['base']}:{t['long_venue']}/{t['short_venue']}"
        thesis = (
            f"Paper perp-perp carry {t['base']}: long {t['long_venue']} {m['long_x']:.6g} (entry {t['long_entry']:.6g}) / short "
            f"{t['short_venue']} {m['short_x']:.6g} (entry {t['short_entry']:.6g}); notional {t['notional_usd']:g} USD, "
            f"funding {m['funding_pnl']:+.2f}, price {m['price_pnl']:+.2f}, costs {-m['costs']:+.2f} USD"
            f"{', stopped' if stopped else ''}; carry_paper id {id}. Journal entry 1.0 / exit 1 + return encodes the return on notional."
        )
        j_tags = ["pp-paper", *[x for x in (t.get("tags") or []) if x != "pp-paper"]]
        if not t.get("entry_tradable"):
            j_tags.append("pp-paper-offrule")  # opened outside the backtest's entry rule
        j_pnl = pnl_fn("long", float(t["notional_usd"]), 1.0, 1.0 + ret)
        won = await rt.store.carry_paper_close(
            id,
            status="closed",
            closed_ts=close_ts,
            stop_ts=close_ts if stopped else None,
            exit_reason=reason,
            long_exit=m["long_x"],
            short_exit=m["short_x"],
            funding_pnl=m["funding_pnl"],
            price_pnl=m["price_pnl"],
            costs=m["costs"],
            pnl_usd=pnl_usd,
            return_on_notional=ret,
            max_up=m["max_up"],
            max_down=m["max_down"],
            stopped=stopped,
            journal={
                "opened_ts": int(t["opened_ts"]),
                "symbol": symbol,
                "side": "long",
                "size": float(t["notional_usd"]),
                "entry": 1.0,
                "exit": 1.0 + ret,
                "thesis": thesis,
                "tags": j_tags,
                "outcome": outcome_of(j_pnl),
                "pnl": j_pnl,
            },
            note=note,
        )
        if not won:
            b.miss(*already)
            return b.build(None, ok=False)
        out = _closed_view(await rt.store.carry_paper_get(id))
        out["exit_signal_at_close"] = m["exit_signal"]
        return b.build(out)
