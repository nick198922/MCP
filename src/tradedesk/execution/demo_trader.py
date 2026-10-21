"""Demo trader (ADR 0007): trade the course setups on the Bybit Demo Trading account and keep the journal.

    uv run python -m tradedesk.execution.demo_trader [--db PATH] [--dry-run]   # hourly: sync, scan, select, enter
    uv run python -m tradedesk.execution.demo_trader --manage                  # every minute: break-even stops only

Hourly cycle:
1. Sync every trade of the book with the venue. An order whose answer was lost ('unknown') is looked up by its
   orderLinkId. A closed trade gets its P&L from Bybit's closed-pnl (fees included), R = P&L / planned risk, the
   journal row is closed and the owner gets a Telegram message. A position held longer than ``MAX_HOLD_MS`` of its
   timeframe is flattened with a reduce-only market order (outside the guard: it only reduces).
2. Break-even (also every minute): at +30 % ROE the stop moves to the entry plus both taker fees (owner 21.10).
3. Universe: today's top 100 USDT perpetuals by turnover. Scan the setups of ``analytics/setup_signals.py`` on the last
   closed 1d and 4h bars; a signal older than ``FRESH_MS`` is ignored, and each signal is decided once.
4. Each candidate is sized for $100 at the stop (fees included) from the live price, gets its leverage (tier, lowered
   for the liquidation) and a confidence score. Candidates that pass the guard and score at least ``MIN_SCORE`` are
   entered best first, at most 5 a UTC day (owner 21.10); the rest are recorded as skipped with the reason.
5. Every candidate, taken or not, is also written to ``trade_plan`` as a shadow plan (tag ``src:demo-shadow``, verdict
   'enter' if taken, else 'skip'), which the collector resolves on candles without the break-even rule: so selection
   and break-even can both be compared with the plain setup.

Kill-switch: the file ~/.config/tradedesk/demo.halt (any content) stops new entries; open positions keep their stop
and target. No key file = nothing to do (exit 0). Signals come from candles only: no text from any external source can
start an order (ADR 0005 p. 4). Nothing here can reach a real account (``bybit_demo`` refuses other hosts). Every step
is guarded separately and logged to the append-only ``exec_audit``; one failure never stops the cycle.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..analytics.course_s14_ema import Bars
from ..analytics.setup_signals import TF_MS, LiveSignal, Score, closed_bars, scan, score
from ..store import Store
from .bybit_demo import BybitDemo, load_keys
from .guard import (
    BE_ROE,
    DEFAULT,
    GuardConfig,
    Proposal,
    State,
    base_of,
    breakeven_stop,
    check,
    day_start_ms,
    open_risk_usd,
    plan_order,
    roe,
    universe,
)

VENUE = "bybit-demo"
HALT_FILE = Path.home() / ".config" / "tradedesk" / "demo.halt"
DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
TIMEFRAMES = ("1d", "4h")  # 1d first: its bars also feed the score of the 4h signals
SETUPS = ("S14", "S16", "S3")
FRESH_MS = 2 * 3_600_000  # the signal bar must have closed within the last 2 hours
MAX_HOLD_MS = {"4h": 7 * 86_400_000, "1d": 30 * 86_400_000}  # S14 registered hold: 7 days on 4h
MIN_SCORE = 2
UNKNOWN_GRACE_MS = 10 * 60_000  # an 'unknown' order the venue still does not know after this is rejected
SHADOW_EXCHANGE = "bybit"  # ccxt id the collector resolves the shadow plans on
HOUR_MS = 3_600_000

Notify = Callable[[str], Awaitable[None]]


def link_id(sig: LiveSignal, symbol: str) -> str:
    """Idempotency key of one signal (Bybit orderLinkId, max 36 chars)."""
    return f"td-{sig.setup}-{sig.side[0]}-{sig.timeframe}-{base_of(symbol)}-{sig.signal_ts // 1000}"[:36]


def _round(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10) if tick > 0 else x


def _fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.6g}" if abs(x) < 1000 else f"{x:,.0f}"


def _side_ua(side: str) -> str:
    return "лонг" if side == "long" else "шорт"


@dataclass
class Ctx:
    store: Store
    client: BybitDemo
    now: int
    notify: Notify
    cfg: GuardConfig = DEFAULT
    dry_run: bool = False
    out: dict[str, Any] = field(default_factory=dict)

    def bump(self, k: str, n: int = 1) -> None:
        self.out[k] = self.out.get(k, 0) + n

    async def audit(self, kind: str, **kw: Any) -> None:
        # a dry run marks its rows, so they never count as decided signals for a real run on the same DB
        await self.store.exec_audit_add(self.now, VENUE, f"dry_{kind}" if self.dry_run else kind, **kw)

    async def safe(self, what: str, fn: Callable[[], Awaitable[Any]], **kw: Any) -> Any:
        """Run one step; on any error count it, write it to the audit (secrets removed) and return None."""
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 - one step must never stop the cycle
            self.bump("errors")
            try:
                await self.audit("error", payload={"step": what, "error": self.client.safe_error(e)}, **kw)
            except Exception:  # noqa: BLE001, S110 - the audit itself failed; the counter still shows it
                pass
            return None


# ---------------------------------------------------------------- sync


async def _journal_open(ctx: Ctx, t: dict[str, Any], fill: float) -> int:
    return await ctx.store.journal_insert(
        opened_ts=(t["opened_ts"] or ctx.now) // 1000,
        symbol=t["symbol"],
        side=t["side"],
        size=t["qty"],
        entry=fill,
        stop=t["stop"],
        target=t["target"],
        thesis=t["comment"],
        tags=["demo-paper", "bybit-demo", f"setup:{t['setup']}", f"tf:{t['timeframe']}", "auto"],
    )


async def _resolve_unknown(ctx: Ctx, t: dict[str, Any]) -> str:
    """'unknown' (order answer lost or not yet recorded) → 'open' / 'rejected' / still 'unknown', by the link id."""
    m = await ctx.client.market(t["symbol"])
    o = await ctx.client.order_by_link(m.market_id, t["link_id"])
    if o is None:
        if ctx.now - (t["opened_ts"] or ctx.now) > UNKNOWN_GRACE_MS:
            await ctx.store.demo_trade_update(t["link_id"], status="rejected")
            await ctx.audit("rejected", link_id=t["link_id"], symbol=t["symbol"], setup=t["setup"], payload={"unknown": "no order"})
            return "rejected"
        return "unknown"
    st = str(o.get("orderStatus") or "")
    if st in ("Rejected", "Cancelled", "Deactivated") and float(o.get("cumExecQty") or 0) == 0:
        await ctx.store.demo_trade_update(t["link_id"], status="rejected", order_id=str(o.get("orderId") or ""))
        await ctx.audit("rejected", link_id=t["link_id"], symbol=t["symbol"], setup=t["setup"], payload={"orderStatus": st})
        return "rejected"
    if float(o.get("cumExecQty") or 0) <= 0:
        return "unknown"
    fill = float(o.get("avgPrice") or 0) or t["entry"]
    opened = int(o.get("updatedTime") or o.get("createdTime") or ctx.now)
    await ctx.store.demo_trade_update(t["link_id"], status="open", order_id=str(o.get("orderId") or ""), fill=fill, opened_ts=opened)
    if not t["journal_id"]:
        jid = await _journal_open(ctx, {**t, "opened_ts": opened}, fill)
        await ctx.store.demo_trade_update(t["link_id"], journal_id=jid)
    await ctx.audit("filled", link_id=t["link_id"], symbol=t["symbol"], setup=t["setup"], payload={"recovered": True, "fill": fill})
    return "open"


def _exit_reason(t: dict[str, Any], exit_px: float | None) -> str:
    if t["exit_reason"]:
        return t["exit_reason"]
    if exit_px is None:
        return "stop"
    levels = {"target": t["target"], "stop": t["stop"]}
    if t["be_stop"]:
        levels["breakeven"] = t["be_stop"]
    return min(levels, key=lambda k: abs(exit_px - levels[k]))


async def _sync_trade(ctx: Ctx, t: dict[str, Any], positions: dict[str, Any]) -> None:
    if t["status"] == "unknown":
        if await _resolve_unknown(ctx, t) != "open":
            return
        t = next(x for x in await ctx.store.demo_trade_list(("open",)) if x["link_id"] == t["link_id"])
    sym, lid = t["symbol"], t["link_id"]
    pos = positions.get(sym)
    if pos:
        if t["fill"] is None and pos.get("entryPrice"):
            await ctx.store.demo_trade_update(lid, fill=float(pos["entryPrice"]))
        if ctx.now - (t["opened_ts"] or ctx.now) > MAX_HOLD_MS.get(t["timeframe"], 7 * 86_400_000) and t["exit_reason"] != "time":
            await ctx.client.close_position(sym, t["side"], float(pos["contracts"]), "tx-" + lid[3:])
            await ctx.store.demo_trade_update(lid, exit_reason="time")
            await ctx.audit("sent", link_id=lid, symbol=sym, setup=t["setup"], payload={"time_exit": True})
            ctx.bump("time_exits")
        return
    m = await ctx.client.market(sym)
    opened = t["opened_ts"] or ctx.now
    rows = [r for r in await ctx.client.closed_pnl(m.market_id, opened - 60_000, ctx.now) if int(r.get("updatedTime") or 0) >= opened]
    if not rows:
        return  # the venue has not reported the close yet
    pnl = sum(float(r.get("closedPnl") or 0) for r in rows)
    exit_px = float(rows[0].get("avgExitPrice") or 0) or None
    reason = _exit_reason(t, exit_px)
    r_mult = pnl / t["risk_usd"] if t["risk_usd"] else 0.0
    await ctx.store.demo_trade_update(
        lid, status="closed", exit=exit_px, closed_ts=ctx.now, pnl_usd=pnl, r_multiple=r_mult, exit_reason=reason
    )
    await ctx.audit("closed", link_id=lid, symbol=sym, setup=t["setup"], payload={"pnl_usd": pnl, "exit": exit_px, "reason": reason})
    ctx.bump("closed")
    if t["journal_id"]:
        outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "break_even"
        await ctx.safe(
            "journal_close",
            lambda: ctx.store.journal_update_close(
                t["journal_id"],
                closed_ts=ctx.now // 1000,
                status="closed",
                exit_=exit_px,
                outcome=outcome,
                pnl=pnl,
                r_multiple=r_mult,
                note=f"bybit-demo {reason}",
            ),
            link_id=lid,
        )
    word = {"target": "ціль", "stop": "стоп", "breakeven": "беззбиток", "time": "вихід за часом"}.get(reason, reason)
    await ctx.safe(
        "notify",
        lambda: ctx.notify(
            f"{'✅' if pnl > 0 else '❌' if pnl < 0 else '➖'} Демо: закрито {t['setup']} {_side_ua(t['side'])} "
            f"{sym.split(':')[0]} ({t['timeframe']}) — {word}, P&L {pnl:+.2f} USD = {r_mult:+.2f}R "
            f"(вхід {_fmt(t['fill'] or t['entry'])}, вихід {_fmt(exit_px)})"
        ),
    )


# ---------------------------------------------------------------- break-even


async def manage(ctx: Ctx, positions: dict[str, Any]) -> None:
    """Owner's rule: at +30 % ROE move the stop to the entry plus both taker fees (once per trade)."""
    for t in await ctx.store.demo_trade_list(("open",)):
        pos = positions.get(t["symbol"])
        if t["be_ts"] or not pos or not pos.get("markPrice"):
            continue
        entry = float(t["fill"] or pos.get("entryPrice") or t["entry"])
        mark = float(pos["markPrice"])
        r = roe(t["side"], entry, mark, t["leverage"])
        if r < BE_ROE:
            continue
        await ctx.safe(
            "breakeven",
            lambda t=t, entry=entry, mark=mark, r=r: _move_to_breakeven(ctx, t, entry, mark, r),
            link_id=t["link_id"],
            symbol=t["symbol"],
        )


async def _move_to_breakeven(ctx: Ctx, t: dict[str, Any], entry: float, mark: float, r: float) -> None:
    m = await ctx.client.market(t["symbol"])
    be = _round(breakeven_stop(t["side"], entry), m.tick)
    if (t["side"] == "long" and be >= mark) or (t["side"] == "short" and be <= mark):
        return  # the price is already back through the break-even level
    await ctx.client.set_stop(m.market_id, be)
    await ctx.store.demo_trade_update(t["link_id"], be_ts=ctx.now, be_stop=be)
    await ctx.audit(
        "stop_moved", link_id=t["link_id"], symbol=t["symbol"], setup=t["setup"], payload={"be_stop": be, "roe": r, "mark": mark}
    )
    ctx.bump("breakeven")
    await ctx.safe(
        "notify",
        lambda: ctx.notify(
            f"🛡 Демо: {t['setup']} {_side_ua(t['side'])} {t['symbol'].split(':')[0]} — ROE {r:+.0%}, "
            f"стоп перенесено в беззбиток {_fmt(be)} (вхід {_fmt(entry)}, ціна {_fmt(mark)})"
        ),
    )


# ---------------------------------------------------------------- state, candidates, entries


async def _state(ctx: Ctx, positions: dict[str, Any], uni: tuple[str, ...], equity: float) -> State:
    trades = await ctx.store.demo_trade_list(limit=5000)
    open_rows = [t for t in trades if t["status"] in ("open", "unknown")]
    day0 = day_start_ms(ctx.now)
    seen_audit = await ctx.store.exec_audit_list(ctx.now - 3 * 86_400_000, ("refused", "skipped"), limit=5000)
    return State(
        now_ms=ctx.now,
        universe=uni,
        halted=HALT_FILE.exists(),
        open_symbols=tuple(sorted(set(positions) | {t["symbol"] for t in open_rows})),
        trades_today=sum(1 for t in trades if t["status"] in ("open", "unknown", "closed") and (t["opened_ts"] or 0) >= day0),
        orders_last_hour=len(await ctx.store.exec_audit_list(ctx.now - HOUR_MS, ("sent",))),
        open_risk_usd=open_risk_usd([(t["qty"], t["fill"] or t["entry"], t["be_stop"] or t["stop"]) for t in open_rows]),
        open_margin_usd=sum(t["qty"] * (t["fill"] or t["entry"]) / max(1, t["leverage"]) for t in open_rows),
        day_realised_loss_usd=-sum(
            min(0.0, t["pnl_usd"] or 0.0) for t in trades if t["status"] == "closed" and (t["closed_ts"] or 0) >= day0
        ),
        equity_usd=equity,
        seen_link_ids=tuple({t["link_id"] for t in trades} | {a["link_id"] for a in seen_audit if a["link_id"]}),
    )


@dataclass
class Candidate:
    sig: LiveSignal
    p: Proposal
    score: Score
    structural: list[str]  # guard violations other than the daily / hourly caps


_CAPS = ("trades today", "orders in the last hour")


async def _candidates(ctx: Ctx, uni: tuple[str, ...], state: State) -> list[Candidate]:
    htf: dict[str, Bars] = {}
    out: list[Candidate] = []
    for symbol in uni:
        for tf in TIMEFRAMES:
            bars = await ctx.safe("ohlcv", lambda s=symbol, tf=tf: _bars(ctx, s, tf), symbol=symbol)
            if bars is None:
                continue
            if tf == "1d":
                htf[symbol] = bars
            for sig in scan(bars, tf, SETUPS):
                if sig.signal_ts + TF_MS[tf] < ctx.now - FRESH_MS or link_id(sig, symbol) in state.seen_link_ids:
                    continue
                c = await ctx.safe("candidate", lambda s=symbol, sig=sig: _candidate(ctx, s, sig, htf, state), symbol=symbol)
                if c is not None:
                    out.append(c)
    return out


async def _bars(ctx: Ctx, symbol: str, tf: str) -> Bars:
    return closed_bars(await ctx.client.ohlcv(symbol, tf), tf, ctx.now)


async def _candidate(ctx: Ctx, symbol: str, sig: LiveSignal, htf: dict[str, Bars], state: State) -> Candidate:
    ctx.bump("signals")
    m = await ctx.client.market(symbol)
    last = await ctx.client.last_price(symbol)
    p = plan_order(
        symbol,
        sig.side,
        last,
        _round(sig.stop, m.tick),
        _round(sig.target, m.tick),
        sig.setup,
        link_id(sig, symbol),
        m.lot_step,
        m.min_qty,
        ctx.cfg,
    )
    sc = score(sig, p.reward_risk, htf.get(symbol), htf.get("BTC/USDT:USDT"), base_of(symbol) == "BTC")
    structural = [v for v in check(p, replace(state, trades_today=0, orders_last_hour=0), ctx.cfg) if not any(c in v for c in _CAPS)]
    await ctx.audit(
        "signal",
        link_id=p.link_id,
        symbol=symbol,
        setup=sig.setup,
        payload={
            "signal": sig.__dict__,
            "proposal": {**p.__dict__, "risk_usd": p.risk_usd, "rr": p.reward_risk},
            "score": sc.points,
            "why": sc.reasons,
        },
    )
    return Candidate(sig, p, sc, structural)


async def _shadow(ctx: Ctx, c: Candidate, taken: bool) -> int | None:
    """The same levels as a plan the collector resolves on candles (no break-even): selection and BE are compared."""
    if ctx.dry_run or any("levels" in v or "stop " in v for v in c.structural):
        return None
    return await ctx.store.plan_insert(
        created_ts=ctx.now // 1000,
        exchange=SHADOW_EXCHANGE,
        symbol=c.p.symbol,
        instrument="perp",
        side=c.p.side,
        entry_type="market",
        entry=c.p.entry,
        stop=c.p.stop,
        target=c.p.target,
        expires_ts=(ctx.now + MAX_HOLD_MS[c.sig.timeframe]) // 1000,
        verdict="enter" if taken else "skip",
        thesis=c.sig.comment,
        tags=[
            "src:demo-shadow",
            f"setup:{c.sig.setup}",
            f"tf:{c.sig.timeframe}",
            "demo:taken" if taken else "demo:skipped",
            f"score:{c.score.points}",
        ],
    )


async def _enter(ctx: Ctx, c: Candidate, state: State) -> State:
    p, sig = c.p, c.sig
    await ctx.client.prepare(p.symbol, p.leverage)
    await ctx.store.demo_trade_insert(
        {
            "link_id": p.link_id,
            "venue": VENUE,
            "symbol": p.symbol,
            "side": p.side,
            "setup": sig.setup,
            "timeframe": sig.timeframe,
            "signal_ts": sig.signal_ts,
            "entry": p.entry,
            "stop": p.stop,
            "target": p.target,
            "qty": p.qty,
            "leverage": p.leverage,
            "risk_usd": p.risk_usd,
            "score": c.score.points,
            "score_reasons": json.dumps(c.score.reasons, ensure_ascii=False),
            "comment": sig.comment,
            "status": "unknown",
            "opened_ts": ctx.now,
        }
    )  # written before the order: if the answer is lost, the next sync finds the order by its link id
    taken = replace(
        state,
        open_symbols=(*state.open_symbols, p.symbol),
        trades_today=state.trades_today + 1,
        orders_last_hour=state.orders_last_hour + 1,
        open_risk_usd=state.open_risk_usd + p.risk_usd,
        open_margin_usd=state.open_margin_usd + p.margin,
        seen_link_ids=(*state.seen_link_ids, p.link_id),
    )
    try:
        order = await ctx.client.place_bracket(p.symbol, p.side, p.qty, p.stop, p.target, p.link_id)
    except Exception as e:  # noqa: BLE001
        from .bybit_demo import is_refusal  # noqa: PLC0415

        if is_refusal(e):
            await ctx.store.demo_trade_update(p.link_id, status="rejected")
            await ctx.audit("rejected", link_id=p.link_id, symbol=p.symbol, setup=sig.setup, payload={"error": ctx.client.safe_error(e)})
            ctx.bump("rejected")
            return replace(state, seen_link_ids=(*state.seen_link_ids, p.link_id))
        await ctx.audit(
            "error", link_id=p.link_id, symbol=p.symbol, setup=sig.setup, payload={"order": ctx.client.safe_error(e), "state": "unknown"}
        )
        ctx.bump("unknown")
        return taken  # the order may exist: count it as taken until the next sync resolves it
    ctx.bump("sent")
    lid = p.link_id
    await ctx.safe(
        "audit_sent",
        lambda: ctx.audit(
            "sent",
            link_id=lid,
            symbol=p.symbol,
            setup=sig.setup,
            payload={"order_id": order.get("id"), "qty": p.qty, "leverage": p.leverage},
        ),
    )
    await ctx.safe("trade_open", lambda: ctx.store.demo_trade_update(lid, status="open", order_id=str(order.get("id") or "")), link_id=lid)

    async def journal() -> None:
        t = next(x for x in await ctx.store.demo_trade_list(("open", "unknown")) if x["link_id"] == lid)
        await ctx.store.demo_trade_update(lid, journal_id=await _journal_open(ctx, t, p.entry))

    await ctx.safe("journal", journal, link_id=lid)
    plan_id = await ctx.safe("shadow", lambda: _shadow(ctx, c, True), link_id=lid)
    if plan_id:
        await ctx.safe("plan_link", lambda: ctx.store.demo_trade_update(lid, plan_id=plan_id), link_id=lid)
    why = "; ".join(c.score.reasons) or "—"
    await ctx.safe(
        "notify",
        lambda: ctx.notify(
            f"🧪 Демо: вхід {sig.setup} {_side_ua(p.side)} {p.symbol.split(':')[0]} ({sig.timeframe}), бал {c.score.points}\n"
            f"Вхід ≈ {_fmt(p.entry)}, стоп {_fmt(p.stop)}, ціль {_fmt(p.target)}, R:R {p.reward_risk:.1f} після комісій, "
            f"ризик {p.risk_usd:.0f} USD, плече {p.leverage}x, позиція {p.notional:,.0f} USD (застава {p.margin:,.0f})\n"
            f"Чому: {sig.comment}\nБали: {why}"
        ),
    )
    return taken


async def _select_and_enter(ctx: Ctx, cands: list[Candidate], state: State) -> None:
    ok = [c for c in cands if not c.structural and c.score.points >= MIN_SCORE]
    ok.sort(key=lambda c: (-c.score.points, -c.p.reward_risk))
    chosen: set[str] = set()
    for c in ok:
        if c.p.symbol in state.open_symbols:
            continue  # a better signal on the same coin was just taken
        bad = check(c.p, state, ctx.cfg)
        if bad:
            continue
        if ctx.dry_run:
            ctx.bump("would_send")
            chosen.add(c.p.link_id)
            state = replace(state, trades_today=state.trades_today + 1, open_symbols=(*state.open_symbols, c.p.symbol))
            continue
        state = await _enter(ctx, c, state)
        chosen.add(c.p.link_id)
    for c in cands:
        if c.p.link_id in chosen:
            continue
        if c.structural:
            reason = c.structural
            kind = "refused"
        elif c.score.points < MIN_SCORE:
            reason, kind = [f"score {c.score.points} < {MIN_SCORE}"], "skipped"
        else:
            reason, kind = check(c.p, state, ctx.cfg) or ["lower rank"], "skipped"
        ctx.bump(kind)
        await ctx.safe(
            "audit_skip",
            lambda c=c, kind=kind, reason=reason: ctx.audit(
                kind, link_id=c.p.link_id, symbol=c.p.symbol, setup=c.sig.setup, payload={"violations": reason, "score": c.score.points}
            ),
        )
        await ctx.safe("shadow", lambda c=c: _shadow(ctx, c, False), link_id=c.p.link_id)


async def cycle(
    store: Store, client: BybitDemo, now: int, notify: Notify, cfg: GuardConfig = DEFAULT, dry_run: bool = False
) -> dict[str, Any]:
    ctx = Ctx(store, client, now, notify, cfg, dry_run, {"signals": 0, "sent": 0, "errors": 0})
    positions = await ctx.safe("positions", client.positions)
    if positions is None:
        ctx.out["skipped_entries"] = "positions unreadable"
        return ctx.out
    for t in await store.demo_trade_list(("open", "unknown")):
        await ctx.safe("sync", lambda t=t: _sync_trade(ctx, t, positions), link_id=t["link_id"], symbol=t["symbol"])
    positions = await ctx.safe("positions", client.positions) or positions
    await manage(ctx, positions)
    equity = await ctx.safe("equity", client.equity_usd)
    turnovers = await ctx.safe("universe", client.turnovers)
    if not equity or not turnovers:
        ctx.out["skipped_entries"] = "equity or universe unreadable"
        return ctx.out
    uni = universe(turnovers)
    state = await _state(ctx, positions, uni, equity)
    ctx.out.update(halted=state.halted, universe=len(uni), trades_today=state.trades_today)
    cands = await _candidates(ctx, uni, state)
    await _select_and_enter(ctx, cands, state)
    return ctx.out


async def manage_only(store: Store, client: BybitDemo, now: int, notify: Notify) -> dict[str, Any]:
    ctx = Ctx(store, client, now, notify, out={"breakeven": 0, "errors": 0})
    positions = await ctx.safe("positions", client.positions)
    if positions:
        await manage(ctx, positions)
    return ctx.out


def _telegram() -> Notify:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    try:
        from tg_send import md_to_html, send  # noqa: PLC0415
    except ImportError:
        send = None

    async def notify(text: str) -> None:
        print(text)
        if send is not None:
            await asyncio.to_thread(send, md_to_html(text), topic="demo")

    return notify


async def amain(db: str, keys: tuple[str, str], dry_run: bool, manage_mode: bool) -> int:
    store, client = Store(db), BybitDemo(*keys)
    await store.init()
    try:
        now = int(time.time() * 1000)
        res = await (
            manage_only(store, client, now, _telegram()) if manage_mode else cycle(store, client, now, _telegram(), dry_run=dry_run)
        )
        print(json.dumps(res, sort_keys=True, ensure_ascii=False))
        return 0
    finally:
        await client.aclose()
        await store.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("TRADEDESK_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true", help="scan, size, score and check, write the audit, send no order")
    ap.add_argument("--manage", action="store_true", help="only the break-even rule (the minute timer)")
    a = ap.parse_args(argv)
    keys = load_keys()
    if keys is None:
        print("no demo key (~/.config/tradedesk/bybit-demo.env): nothing to do")
        return 0
    with open(Path(a.db).parent / "demo-trader.lock", "w") as lock:
        try:
            # the minute job skips while the hourly one runs; the hourly one waits for a running minute job
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if a.manage else 0))
        except BlockingIOError:
            return 0
        return asyncio.run(amain(a.db, keys, a.dry_run, a.manage))


if __name__ == "__main__":
    sys.exit(main())
