"""Demo trader (ADR 0007): trade the course setups on the Bybit Demo Trading account and keep the journal.

    uv run python -m tradedesk.execution.demo_trader [--db PATH] [--dry-run]   # hourly: sync, scan, select, enter
    uv run python -m tradedesk.execution.demo_trader --manage                  # every minute: orders and break-even

Order types (owner 22.10, ``orders.py``): entries are post-only limits (maker) re-quoted each minute; the target is a
resting reduce-only limit; the stops stay stop-market; a time exit is a limit first and the market only when it does not
fill or the price nears the stop.

Hourly cycle:
1. Sync every trade of the book with the venue. An order whose answer was lost ('unknown') is looked up by its
   orderLinkId. A closed trade gets its P&L from Bybit's closed-pnl (fees included), R = P&L / risk of the filled size,
   the journal row is closed and the owner gets a Telegram message. A position held longer than ``MAX_HOLD_MS`` of its
   timeframe starts a time exit (reduce-only orders, outside the guard: they only reduce).
2. Orders and break-even (also every minute): a resting entry ('pending') is re-quoted, completed at the market or
   cancelled ('missed'); a complete entry gets its target limit; a time exit is re-quoted or sent to the market; half the
   way to the target the stop moves to the entry plus both taker fees (owner 22.10; was +30 % ROE).
3. Universe: today's top 100 crypto USDT perpetuals by turnover (no tokenised stocks, ETFs or commodities). Scan the setups of ``analytics/setup_signals.py`` on the last
   closed 1d and 4h bars; a signal older than ``FRESH_MS`` is ignored, and each signal is decided once.
4. Each candidate is sized for $100 at the stop (fees included) from the live price, gets its leverage (the owner's
   tier, capped by the venue's maximum) and a confidence score. The account is on cross margin (owner 21.10): the guard
   keeps the maintenance margin plus the loss at every stop within half of the equity, so stops come before liquidation. Candidates that pass the guard and score at least ``MIN_SCORE`` are
   entered best first, at most 5 still at risk a UTC day: a trade with its stop at break-even, or closed without a real
   loss, frees its slot (owner 21.10/22.10), at most 10 entries a day in all; the rest are recorded as skipped.
5. Every candidate, taken or not, is also written to ``trade_plan`` as a shadow plan (tag ``src:demo-shadow``, verdict
   'enter' if taken, else 'skip'), which the collector resolves on candles without the break-even rule: so selection
   and break-even can both be compared with the plain setup.

A dry run reads and scans only: no sync writes, no time exit, no stop move, no order; its audit rows are 'dry_*'.

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
from .bybit_demo import CROSS, BybitDemo, is_refusal, load_keys
from .guard import (
    BE_PATH,
    DEFAULT,
    TAKER_FEE,
    GuardConfig,
    Proposal,
    State,
    base_of,
    breakeven_stop,
    check,
    day_start_ms,
    open_risk_usd,
    path_to_target,
    plan_order,
    universe,
)
from .orders import ENTRY_WAIT_MS, entry_step, exit_step, maker_quote

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
BTC = "BTC/USDT:USDT"

Notify = Callable[[str], Awaitable[None]]


def link_id(sig: LiveSignal, symbol: str) -> str:
    """Idempotency key of one signal (Bybit orderLinkId, max 36 chars)."""
    return f"td-{sig.setup}-{sig.side[0]}-{sig.timeframe}-{base_of(symbol)}-{sig.signal_ts // 1000}"[:36]


def _link(lid: str, prefix: str) -> str:
    """Link id of a follow-up order of the trade ``lid``: 'em-' market rest of the entry, 'tp-' target limit, 'xl-' time
    exit limit, 'tx-' time exit at the market (same length as ``lid``, unique per trade)."""
    return prefix + lid[3:]


LIVE = ("New", "PartiallyFilled", "Untriggered")  # Bybit orderStatus of an order still working


def _risk(qty: float, fill: float, stop: float) -> float:
    return qty * (abs(fill - stop) + TAKER_FEE * (fill + stop))


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
    """'unknown' (order answer lost or not yet recorded) → 'pending' (the venue has it: the entry steps take over) /
    'rejected' / still 'unknown', by the link id."""
    m = await ctx.client.market(t["symbol"])
    o = await ctx.client.order_by_link(m.market_id, t["entry_order"] or t["link_id"])
    if o is None:
        if ctx.now - (t["opened_ts"] or ctx.now) > UNKNOWN_GRACE_MS:
            await ctx.store.demo_trade_update(t["link_id"], status="rejected")
            await ctx.audit("rejected", link_id=t["link_id"], symbol=t["symbol"], setup=t["setup"], payload={"unknown": "no order"})
            return "rejected"
        return "unknown"
    st = str(o.get("orderStatus") or "")
    if st == "Rejected" and float(o.get("cumExecQty") or 0) == 0:
        await ctx.store.demo_trade_update(t["link_id"], status="rejected", order_id=str(o.get("orderId") or ""))
        await ctx.audit("rejected", link_id=t["link_id"], symbol=t["symbol"], setup=t["setup"], payload={"orderStatus": st})
        return "rejected"
    await ctx.store.demo_trade_update(t["link_id"], status="pending", order_id=str(o.get("orderId") or ""))
    await ctx.audit("recovered", link_id=t["link_id"], symbol=t["symbol"], setup=t["setup"], payload={"orderStatus": st})
    return "pending"


# ---------------------------------------------------------------- entry orders


async def _progress_entry(ctx: Ctx, t: dict[str, Any]) -> None:
    """One step of a resting entry: wait, re-quote, complete at the market, or cancel (``orders.entry_step``)."""
    sym, lid = t["symbol"], t["link_id"]
    if (t["entry_order"] or lid) != lid:  # the market rest was sent (its answer lost): the venue's position is the trade
        await _complete_entry(ctx, t, t["entry_kind"] or "mixed")
        return
    m = await ctx.client.market(sym)
    o = await ctx.client.order_by_link(m.market_id, lid)
    if o is None:
        if ctx.now - (t["opened_ts"] or ctx.now) > UNKNOWN_GRACE_MS:  # the venue lost it: free the coin
            await ctx.store.demo_trade_update(lid, status="missed", note="the venue has no such order", closed_ts=ctx.now)
            await ctx.audit("missed", link_id=lid, symbol=sym, setup=t["setup"], payload={"why": "no order"})
        return  # else the venue does not show it yet; the next minute looks again
    st, cum = str(o.get("orderStatus") or ""), float(o.get("cumExecQty") or 0)
    if st == "Filled":
        await _complete_entry(ctx, t, "maker" if t["entry_px"] else "taker")
        return
    live = st in LIVE
    bid, ask = await ctx.client.quote(sym)
    step = entry_step(
        side=t["side"],
        planned=t["entry"],
        stop=t["stop"],
        target=t["target"],
        plan_qty=t["qty"],
        order_px=t["entry_px"] or t["entry"],
        order_live=live,
        filled=cum,
        bid=bid,
        ask=ask,
        age_ms=ctx.now - (t["opened_ts"] or ctx.now),
        risk_usd=ctx.cfg.risk_usd,
        lot_step=m.lot_step,
        min_qty=m.min_qty,
        min_reward_risk=ctx.cfg.min_reward_risk,
    )
    if step.action == "wait":
        return
    if step.action == "requote":
        px = _round(step.price, m.tick)
        await ctx.client.amend(sym, lid, px, step.qty)
        await ctx.store.demo_trade_update(lid, entry_px=px, qty=step.qty, risk_usd=_risk(step.qty, px, t["stop"]))
        await ctx.audit("requote", link_id=lid, symbol=sym, setup=t["setup"], payload={"price": px, "qty": step.qty})
        ctx.bump("requotes")
        return
    if live and not await ctx.client.cancel(sym, lid):
        return  # filled meanwhile: the next minute completes it
    if step.action == "market":
        em, kind = _link(lid, "em-"), "taker" if cum == 0 else "mixed"
        await ctx.store.demo_trade_update(lid, entry_order=em, entry_kind=kind, note=step.reason)  # before the order
        await ctx.client.place_bracket(sym, t["side"], step.qty, t["stop"], None, em)
        await ctx.audit("chased", link_id=lid, symbol=sym, setup=t["setup"], payload={"qty": step.qty, "filled": cum, "why": step.reason})
        await _complete_entry(ctx, t, kind)
        return
    if cum > 0:
        await ctx.store.demo_trade_update(lid, note=step.reason)
        await _complete_entry(ctx, t, "maker")
        return
    await ctx.store.demo_trade_update(lid, status="missed", note=step.reason, closed_ts=ctx.now)
    await ctx.audit("missed", link_id=lid, symbol=sym, setup=t["setup"], payload={"why": step.reason, "bid": bid, "ask": ask})
    ctx.bump("missed")
    await ctx.safe(
        "notify",
        lambda: ctx.notify(
            f"⏭ Демо: лімітний вхід {t['setup']} {_side_ua(t['side'])} {sym.split(':')[0]} не виконано, ордер знято — "
            f"{_why_ua(step.reason)} (план {_fmt(t['entry'])}, зараз {_fmt(bid)} / {_fmt(ask)})"
        ),
    )


def _why_ua(reason: str) -> str:
    if reason.startswith("price ran"):
        return "ціна пішла до цілі без нас, наздоганяти дорожче за план"
    if reason.startswith("price at the stop"):
        return "ціна дійшла до стопу раніше за вхід, ідея зламана"
    if reason.startswith("reward/risk"):
        return "по ринку R:R уже нижчий за поріг"
    return reason


async def _complete_entry(ctx: Ctx, t: dict[str, Any], kind: str) -> None:
    """The entry is done: size and price from the venue's position, journal row, resting target limit, message."""
    sym, lid = t["symbol"], t["link_id"]
    pos = (await ctx.client.positions()).get(sym)
    if not pos:
        await ctx.store.demo_trade_update(
            lid, status="missed", note=(t.get("note") or "") + "; no position after the fill", closed_ts=ctx.now
        )
        await ctx.audit("missed", link_id=lid, symbol=sym, setup=t["setup"], payload={"why": "no position"})
        return
    qty, fill = float(pos["contracts"]), float(pos.get("entryPrice") or t["entry_px"] or t["entry"])
    await ctx.store.demo_trade_update(
        lid, status="open", qty=qty, fill=fill, opened_ts=ctx.now, entry_kind=kind, risk_usd=_risk(qty, fill, t["stop"])
    )
    await ctx.audit("filled", link_id=lid, symbol=sym, setup=t["setup"], payload={"fill": fill, "qty": qty, "kind": kind})
    ctx.bump("filled")
    if not t["journal_id"]:
        jid = await ctx.safe("journal", lambda: _journal_open(ctx, {**t, "qty": qty, "opened_ts": ctx.now}, fill), link_id=lid)
        if jid:
            await ctx.safe("journal_link", lambda: ctx.store.demo_trade_update(lid, journal_id=jid), link_id=lid)
    target_kind = await ctx.safe("target", lambda: _place_target(ctx, t, qty), link_id=lid, symbol=sym)
    word = {"maker": "лімітом (мейкер)", "taker": "по ринку (тейкер)", "mixed": "частково лімітом, решта по ринку"}[kind]
    await ctx.safe(
        "notify",
        lambda: ctx.notify(
            f"✅ Демо: вхід {t['setup']} {_side_ua(t['side'])} {sym.split(':')[0]} виконано {word} по {_fmt(fill)}, "
            f"{qty:g} шт., ризик {_risk(qty, fill, t['stop']):.0f} USD. "
            f"Ціль {_fmt(t['target'])} — {'лімітка' if target_kind == 'limit' else 'по ринку'}, стоп {_fmt(t['stop'])} — по ринку"
        ),
    )


async def _place_target(ctx: Ctx, t: dict[str, Any], qty: float) -> str:
    """Resting reduce-only limit at the target (maker when it fills); refused → the venue's market take-profit."""
    tp = _link(t["link_id"], "tp-")
    try:
        await ctx.client.place_reduce_limit(t["symbol"], t["side"], qty, t["target"], tp, post_only=False)
    except Exception as e:  # noqa: BLE001 - a trade must never stay without a target
        await ctx.audit(
            "error", link_id=t["link_id"], symbol=t["symbol"], payload={"step": "target_limit", "error": ctx.client.safe_error(e)}
        )
        await ctx.client.set_take_profit(t["symbol"], t["target"])
        return "market"
    await ctx.store.demo_trade_update(t["link_id"], tp_link=tp)
    return "limit"


# ---------------------------------------------------------------- time exit


async def _progress_exit(ctx: Ctx, t: dict[str, Any], pos: dict[str, Any]) -> None:
    """One step of a time exit: a post-only limit at the best quote, the market when it does not fill in time or the
    price nears the stop (``orders.exit_step``)."""
    sym, lid = t["symbol"], t["link_id"]
    m = await ctx.client.market(sym)
    live = False
    if t["exit_order"]:
        o = await ctx.client.order_by_link(m.market_id, t["exit_order"])
        live = bool(o) and str(o.get("orderStatus") or "") in LIVE
    bid, ask = await ctx.client.quote(sym)
    step = exit_step(
        side=t["side"],
        entry=t["fill"] or t["entry"],
        stop=t["be_stop"] or t["stop"],
        order_px=t["exit_px"] or 0.0,
        order_live=live,
        bid=bid,
        ask=ask,
        age_ms=ctx.now - (t["exit_ts"] or ctx.now),
    )
    if step.action == "wait":
        return
    if not t["exit_ts"]:
        await ctx.store.demo_trade_update(lid, exit_reason="time", exit_ts=ctx.now)
        ctx.bump("time_exits")
        if t["tp_link"]:  # the target limit would add to the reduce-only size; the time exit replaces it
            await ctx.safe("cancel_target", lambda: ctx.client.cancel(sym, t["tp_link"]), link_id=lid)
    if step.action == "requote":
        px = _round(step.price, m.tick)
        if live:
            await ctx.client.amend(sym, t["exit_order"], px)
        else:
            xl = _link(lid, "xl-")
            await ctx.store.demo_trade_update(lid, exit_order=xl)
            await ctx.client.place_reduce_limit(sym, t["side"], float(pos["contracts"]), px, xl, post_only=True)
        await ctx.store.demo_trade_update(lid, exit_px=px)
        await ctx.audit("exit_limit", link_id=lid, symbol=sym, setup=t["setup"], payload={"price": px, "why": step.reason})
        return
    if live and not await ctx.client.cancel(sym, t["exit_order"]):
        return  # filled meanwhile
    qty = float(((await ctx.client.positions()).get(sym) or {}).get("contracts") or 0) if live else float(pos["contracts"])
    if qty <= 0:
        return
    await ctx.client.close_position(sym, t["side"], qty, _link(lid, "tx-"))
    await ctx.store.demo_trade_update(lid, exit_kind="taker", note=step.reason)
    await ctx.audit("sent", link_id=lid, symbol=sym, setup=t["setup"], payload={"time_exit": "market", "why": step.reason})


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
        await _resolve_unknown(ctx, t)
        return  # a 'pending' entry is taken over by the entry steps of ``manage``
    sym, lid = t["symbol"], t["link_id"]
    pos = positions.get(sym)
    if pos:
        if pos.get("entryPrice") and float(pos["entryPrice"]) != t["fill"]:
            await ctx.store.demo_trade_update(lid, fill=float(pos["entryPrice"]))  # the venue's average is the truth
        if ctx.now - (t["opened_ts"] or ctx.now) > MAX_HOLD_MS.get(t["timeframe"], 7 * 86_400_000) and not t["exit_ts"]:
            await _progress_exit(ctx, t, pos)
        return
    m = await ctx.client.market(sym)
    opened = t["opened_ts"] or ctx.now
    rows = [r for r in await ctx.client.closed_pnl(m.market_id, opened - 60_000, ctx.now) if int(r.get("updatedTime") or 0) >= opened]
    if not rows:
        return  # the venue has not reported the close yet
    pnl = sum(float(r.get("closedPnl") or 0) for r in rows)
    fee_rows = [r for r in rows if r.get("openFee") is not None or r.get("closeFee") is not None]
    fees = sum(float(r.get("openFee") or 0) + float(r.get("closeFee") or 0) for r in fee_rows) if fee_rows else None
    exit_px = float(rows[0].get("avgExitPrice") or 0) or None
    reason = _exit_reason(t, exit_px)
    kind = t["exit_kind"] or ("maker" if (reason == "target" and t["tp_link"]) or (reason == "time") else "taker")
    r_mult = pnl / t["risk_usd"] if t["risk_usd"] else 0.0
    await ctx.store.demo_trade_update(
        lid,
        status="closed",
        exit=exit_px,
        closed_ts=ctx.now,
        pnl_usd=pnl,
        r_multiple=r_mult,
        exit_reason=reason,
        exit_kind=kind,
        fees_usd=fees,
    )
    await ctx.audit(
        "closed", link_id=lid, symbol=sym, setup=t["setup"], payload={"pnl_usd": pnl, "exit": exit_px, "reason": reason, "fees": fees}
    )
    ctx.bump("closed")
    for left in (t["tp_link"], t["exit_order"]):  # a reduce-only order left on the book after the close
        if left:
            await ctx.safe("cancel_left", lambda left=left: ctx.client.cancel(sym, left), link_id=lid)
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
            f"(вхід {_fmt(t['fill'] or t['entry'])}, вихід {_fmt(exit_px)}"
            + (f"; комісії {fees:.2f} USD" if fees is not None else "")
            + ")"
        ),
    )


# ---------------------------------------------------------------- break-even


async def manage(ctx: Ctx, positions: dict[str, Any]) -> None:
    """Resting entries and time exits one step each; then the owner's rule: half the way to the target moves the stop to
    the entry plus both taker fees (once per trade)."""
    for t in await ctx.store.demo_trade_list(("pending",)):
        await ctx.safe("entry", lambda t=t: _progress_entry(ctx, t), link_id=t["link_id"], symbol=t["symbol"])
    for t in await ctx.store.demo_trade_list(("open",)):
        pos = positions.get(t["symbol"])
        if t["exit_ts"] and t["exit_kind"] != "taker" and pos:
            await ctx.safe("time_exit", lambda t=t, pos=pos: _progress_exit(ctx, t, pos), link_id=t["link_id"], symbol=t["symbol"])
    for t in await ctx.store.demo_trade_list(("open",)):
        pos = positions.get(t["symbol"])
        if t["be_ts"] or not pos or not pos.get("markPrice"):
            continue
        entry = float(t["fill"] or pos.get("entryPrice") or t["entry"])
        mark = float(pos["markPrice"])
        way = path_to_target(t["side"], entry, t["target"], mark)
        if way < BE_PATH:
            continue
        await ctx.safe(
            "breakeven",
            lambda t=t, pos=pos, entry=entry, mark=mark, way=way: _move_to_breakeven(ctx, t, pos, entry, mark, way),
            link_id=t["link_id"],
            symbol=t["symbol"],
        )


async def _move_to_breakeven(ctx: Ctx, t: dict[str, Any], pos: dict[str, Any], entry: float, mark: float, way: float) -> None:
    m = await ctx.client.market(t["symbol"])
    be = breakeven_stop(t["side"], entry, m.tick)
    sign = 1 if t["side"] == "long" else -1
    if sign * (mark - be) <= 0:
        return  # the price is already back through the break-even level
    r_now = sign * (mark - entry) / abs(entry - t["stop"]) if entry != t["stop"] else 0.0
    current = float(pos.get("stopLossPrice") or 0)
    if not (current and sign * (current - be) >= 0):  # a stop already at break-even (an earlier move) is only recorded
        await ctx.client.set_stop(t["symbol"], be)
    await ctx.store.demo_trade_update(t["link_id"], be_ts=ctx.now, be_stop=be)
    await ctx.audit(
        "stop_moved",
        link_id=t["link_id"],
        symbol=t["symbol"],
        setup=t["setup"],
        payload={"be_stop": be, "path": way, "r": r_now, "mark": mark},
    )
    ctx.bump("breakeven")
    await ctx.safe(
        "notify",
        lambda: ctx.notify(
            f"🛡 Демо: {t['setup']} {_side_ua(t['side'])} {t['symbol'].split(':')[0]} — пройдено {way * 100:.0f} % шляху до цілі "
            f"({r_now:+.1f}R), "
            f"стоп перенесено в беззбиток {_fmt(be)} (вхід {_fmt(entry)}, ціна {_fmt(mark)})"
        ),
    )


# ---------------------------------------------------------------- state, candidates, entries


async def _state(ctx: Ctx, positions: dict[str, Any], uni: tuple[str, ...], equity: float) -> State:
    trades = await ctx.store.demo_trade_list(limit=5000)
    open_rows = [t for t in trades if t["status"] in ("open", "unknown", "pending")]
    day0 = day_start_ms(ctx.now)
    today = [t for t in trades if t["status"] in ("open", "unknown", "pending", "closed") and (t["opened_ts"] or 0) >= day0]
    seen_audit = await ctx.store.exec_audit_list(ctx.now - 3 * 86_400_000, ("refused", "skipped"), limit=5000)
    return State(
        now_ms=ctx.now,
        universe=uni,
        halted=HALT_FILE.exists(),
        open_symbols=tuple(sorted(set(positions) | {t["symbol"] for t in open_rows})),
        # owner 22.10: the daily cap counts risk, not entries: a trade whose stop is at break-even (or that closed
        # without a real loss) frees its slot; a missed limit entry never took one
        trades_today=sum(1 for t in today if _at_risk(t)),
        entries_today=len(today),
        orders_last_hour=len(await ctx.store.exec_audit_list(ctx.now - HOUR_MS, ("sent",))),
        open_risk_usd=open_risk_usd([(t["qty"], t["fill"] or t["entry"], t["be_stop"] or t["stop"]) for t in open_rows]),
        open_margin_usd=sum(t["qty"] * (t["fill"] or t["entry"]) / max(1, t["leverage"]) for t in open_rows),
        open_maint_usd=sum(_maint(positions.get(t["symbol"]), t) for t in open_rows),
        open_notional_usd=sum(t["qty"] * (t["fill"] or t["entry"]) for t in open_rows),
        day_realised_loss_usd=-sum(
            min(0.0, t["pnl_usd"] or 0.0) for t in trades if t["status"] == "closed" and (t["closed_ts"] or 0) >= day0
        ),
        equity_usd=equity,
        seen_link_ids=tuple({t["link_id"] for t in trades} | {a["link_id"] for a in seen_audit if a["link_id"]}),
    )


def _at_risk(t: dict[str, Any]) -> bool:
    """Does this trade still take one of the day's slots? Open: until its stop is at break-even. Closed: if it lost more
    than a tenth of its risk (a break-even exit loses only slippage)."""
    if t["status"] == "closed":
        return (t["pnl_usd"] or 0.0) < -0.1 * (t["risk_usd"] or 0.0)
    return not t["be_ts"]


def _maint(pos: dict[str, Any] | None, t: dict[str, Any]) -> float:
    """Maintenance margin of an open trade: the venue's figure, else a cautious 10 % of the notional."""
    mm = (pos or {}).get("maintenanceMargin")
    return float(mm) if mm else 0.10 * t["qty"] * (t["fill"] or t["entry"])


@dataclass
class Candidate:
    sig: LiveSignal
    p: Proposal
    score: Score
    structural: list[str]  # guard violations other than the daily / hourly caps


_CAPS = ("at risk today", "entries today", "orders in the last hour")


async def _candidates(ctx: Ctx, uni: tuple[str, ...], state: State) -> list[Candidate]:
    htf: dict[str, Bars] = {}
    out: list[Candidate] = []
    btc = await ctx.safe("ohlcv", lambda: _bars(ctx, BTC, "1d"), symbol=BTC)  # the BTC-trend point of every alt
    if btc is not None:
        htf[BTC] = btc
    for symbol in uni:
        for tf in TIMEFRAMES:
            bars = htf.get(symbol) if tf == "1d" and symbol == BTC else None
            bars = bars or await ctx.safe("ohlcv", lambda s=symbol, tf=tf: _bars(ctx, s, tf), symbol=symbol)
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
    tier = await ctx.client.risk_tier(symbol)
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
        mmr=tier.mmr,
        venue_max_leverage=tier.max_leverage,
        tier_limit_usd=tier.limit_usd,
        equity_usd=state.equity_usd,
    )
    sc = score(sig, p.reward_risk, htf.get(symbol), htf.get(BTC), base_of(symbol) == "BTC")
    free = replace(state, trades_today=0, entries_today=0, orders_last_hour=0)
    structural = [v for v in check(p, free, ctx.cfg) if not any(c in v for c in _CAPS)]
    await ctx.audit(
        "signal",
        link_id=p.link_id,
        symbol=symbol,
        setup=sig.setup,
        payload={
            "signal": sig.__dict__,
            "proposal": {**p.__dict__, "risk_usd": p.risk_usd, "rr": p.reward_risk, "maintenance": p.maintenance},
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


async def _insert_trade(ctx: Ctx, c: Candidate, px: float) -> None:
    p, sig = c.p, c.sig
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
            "opened_ts": ctx.now,  # the placement; set again to the fill time when the entry completes
            "entry_order": p.link_id,
            "entry_px": px,
        }
    )  # written before the order: if the answer is lost, the next sync finds the order by its link id


async def _enter(ctx: Ctx, c: Candidate, state: State) -> State:
    p, sig = c.p, c.sig
    try:
        await ctx.client.prepare(p.symbol, p.leverage)
        m = await ctx.client.market(p.symbol)
        px = _round(maker_quote(p.side, *await ctx.client.quote(p.symbol)), m.tick)
        if not px > 0:
            raise ValueError(f"no quote for {p.symbol}")
        await _insert_trade(ctx, c, px)
    except Exception as e:  # noqa: BLE001 - nothing was sent: the signal is refused, the cycle goes on
        err = ctx.client.safe_error(e)
        ctx.bump("refused")
        await ctx.safe(
            "audit_refused",
            lambda: ctx.audit("refused", link_id=p.link_id, symbol=p.symbol, setup=sig.setup, payload={"violations": [err]}),
        )
        await ctx.safe("shadow", lambda: _shadow(ctx, c, False), link_id=p.link_id)
        return replace(state, seen_link_ids=(*state.seen_link_ids, p.link_id))
    taken = replace(
        state,
        open_symbols=(*state.open_symbols, p.symbol),
        trades_today=state.trades_today + 1,
        entries_today=state.entries_today + 1,
        orders_last_hour=state.orders_last_hour + 1,
        open_risk_usd=state.open_risk_usd + p.risk_usd,
        open_margin_usd=state.open_margin_usd + p.margin,
        open_maint_usd=state.open_maint_usd + p.maintenance,
        open_notional_usd=state.open_notional_usd + p.notional,
        seen_link_ids=(*state.seen_link_ids, p.link_id),
    )
    try:
        order = await ctx.client.place_bracket(p.symbol, p.side, p.qty, p.stop, None, p.link_id, price=px)
    except Exception as e:  # noqa: BLE001
        err = ctx.client.safe_error(e)
        if is_refusal(e):
            ctx.bump("rejected")
            await ctx.safe("trade_rejected", lambda: ctx.store.demo_trade_update(p.link_id, status="rejected"), link_id=p.link_id)
            await ctx.safe(
                "audit_rejected",
                lambda: ctx.audit("rejected", link_id=p.link_id, symbol=p.symbol, setup=sig.setup, payload={"error": err}),
            )
            return replace(state, seen_link_ids=(*state.seen_link_ids, p.link_id))
        ctx.bump("unknown")
        await ctx.safe(
            "audit_unknown",
            lambda: ctx.audit("error", link_id=p.link_id, symbol=p.symbol, setup=sig.setup, payload={"order": err, "state": "unknown"}),
        )
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
            payload={"order_id": order.get("id"), "qty": p.qty, "leverage": p.leverage, "limit": px},
        ),
    )
    await ctx.safe(
        "trade_pending", lambda: ctx.store.demo_trade_update(lid, status="pending", order_id=str(order.get("id") or "")), link_id=lid
    )  # the journal row comes with the fill (``_complete_entry``)
    plan_id = await ctx.safe("shadow", lambda: _shadow(ctx, c, True), link_id=lid)
    if plan_id:
        await ctx.safe("plan_link", lambda: ctx.store.demo_trade_update(lid, plan_id=plan_id), link_id=lid)
    why = "; ".join(c.score.reasons) or "—"
    await ctx.safe(
        "notify",
        lambda: ctx.notify(
            f"🧪 Демо: вхід {sig.setup} {_side_ua(p.side)} {p.symbol.split(':')[0]} ({sig.timeframe}), бал {c.score.points}\n"
            f"Лімітний ордер (мейкер) по {_fmt(px)}, чекаю до {ENTRY_WAIT_MS // 60_000} хв; ціна {_fmt(p.entry)}, стоп {_fmt(p.stop)}, ціль {_fmt(p.target)}, R:R {p.reward_risk:.1f} після комісій, "
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
            state = replace(
                state,
                trades_today=state.trades_today + 1,
                entries_today=state.entries_today + 1,
                open_symbols=(*state.open_symbols, c.p.symbol),
            )
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
    if not dry_run:  # a dry run only reads and scans: no sync writes, no time exit, no stop move
        for t in await store.demo_trade_list(("open", "unknown")):
            await ctx.safe("sync", lambda t=t: _sync_trade(ctx, t, positions), link_id=t["link_id"], symbol=t["symbol"])
        positions = await ctx.safe("positions", client.positions) or positions
        await manage(ctx, positions)
    equity = await ctx.safe("equity", client.equity_usd)
    turnovers = await ctx.safe("universe", client.turnovers)
    mode = await ctx.safe("margin_mode", lambda: client.ensure_cross(change=not dry_run))
    if not equity or not turnovers or mode is None:
        ctx.out["skipped_entries"] = "equity, universe or margin mode unreadable"
        return ctx.out
    if mode != CROSS:
        await ctx.safe("audit_margin", lambda: ctx.audit("margin_mode", payload={"was": mode, "now": "cross" if not dry_run else mode}))
    uni = universe(turnovers)
    state = await _state(ctx, positions, uni, equity)
    ctx.out.update(halted=state.halted, universe=len(uni), trades_today=state.trades_today, entries_today=state.entries_today)
    cands = await _candidates(ctx, uni, state)
    await _select_and_enter(ctx, cands, state)
    return ctx.out


async def manage_only(store: Store, client: BybitDemo, now: int, notify: Notify) -> dict[str, Any]:
    ctx = Ctx(store, client, now, notify, out={"breakeven": 0, "errors": 0})
    positions = await ctx.safe("positions", client.positions)
    if positions is not None:  # no position yet still leaves resting entries to manage
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
