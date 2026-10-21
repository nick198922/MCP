"""Demo trader (ADR 0007): every hour, trade the course setups on the Bybit Demo Trading account and keep the journal.

    uv run python -m tradedesk.execution.demo_trader [--db PATH] [--dry-run]

One cycle:
1. Sync: open demo trades are checked against the venue; a closed one gets its P&L from Bybit's closed-pnl (fees
   included), R = P&L / planned risk, the journal row is closed and the owner gets a Telegram message. A position held
   longer than ``MAX_HOLD_MS`` of its timeframe is flattened with a reduce-only market order.
2. Scan: for each symbol of the allow-list and each timeframe, the setups of ``analytics/setup_signals.py`` on the last
   closed bar. A signal older than ``FRESH_MS`` (e.g. after downtime) is ignored.
3. For a fresh signal: size for $risk at the stop from the live price, round to the venue's steps, ``guard.check``; then
   either a refusal (audit row, no order) or a market order with the stop and target attached, a ``demo_trade`` row
   (written before the order), a journal row (tags ``demo-paper``, ``bybit-demo``, ``setup:<S>``, ``auto``) and a
   Telegram message with the setup, its indicators and levels.

Kill-switch: the file ~/.config/tradedesk/demo.halt (any content) stops new entries; open positions keep their attached
stop and target. No key file = nothing to do (exit 0). Signals come from candles only: no text from any external source
can start an order (ADR 0005 p. 4). Nothing here can reach a real account (``bybit_demo`` refuses other hosts).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..analytics.setup_signals import TF_MS, LiveSignal, closed_bars, scan
from ..store import Store
from .bybit_demo import BybitDemo, load_keys, safe_error
from .guard import DEFAULT, GuardConfig, Proposal, State, check, day_start_ms, open_risk_usd, size_for_risk

VENUE = "bybit-demo"
HALT_FILE = Path.home() / ".config" / "tradedesk" / "demo.halt"
DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
TIMEFRAMES = ("4h", "1d")
SETUPS = ("S14", "S16", "S3")
FRESH_MS = 2 * 3_600_000  # the signal bar must have closed within the last 2 hours
MAX_HOLD_MS = {"4h": 7 * 86_400_000, "1d": 30 * 86_400_000}  # S14 registered hold: 7 days on 4h
HOUR_MS = 3_600_000

Notify = Callable[[str], Awaitable[None]]


def link_id(sig: LiveSignal, symbol: str) -> str:
    """Idempotency key of one signal (Bybit orderLinkId, max 36 chars)."""
    base = symbol.split("/")[0]
    return f"td-{sig.setup}-{sig.side[0]}-{sig.timeframe}-{base}-{sig.signal_ts // 1000}"[:36]


def _round(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10) if tick > 0 else x


def _fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.6g}" if abs(x) < 1000 else f"{x:,.0f}"


async def _audit(store: Store, now: int, kind: str, **kw: Any) -> None:
    await store.exec_audit_add(now, VENUE, kind, **kw)


async def sync(store: Store, client: BybitDemo, now: int, notify: Notify) -> dict[str, int]:
    counts = {"closed": 0, "time_exits": 0}
    positions = await client.positions()
    for t in await store.demo_trade_list(("open", "unknown")):
        sym, lid = t["symbol"], t["link_id"]
        pos = positions.get(sym)
        if t["status"] == "unknown":
            await store.demo_trade_update(lid, status="open" if pos else "rejected")
            if not pos:
                continue
        if pos:
            if t["fill"] is None and pos.get("entryPrice"):
                await store.demo_trade_update(lid, fill=float(pos["entryPrice"]))
            opened = t["opened_ts"] or now
            if now - opened > MAX_HOLD_MS.get(t["timeframe"], 7 * 86_400_000) and t["exit_reason"] != "time":
                try:
                    await client.close_position(sym, t["side"], float(pos["contracts"]), lid[:33] + "-tx")
                    await store.demo_trade_update(lid, exit_reason="time")
                    await _audit(store, now, "sent", link_id=lid, symbol=sym, setup=t["setup"], payload={"time_exit": True})
                    counts["time_exits"] += 1
                except Exception as e:  # noqa: BLE001 - one symbol must not stop the cycle
                    await _audit(store, now, "error", link_id=lid, symbol=sym, payload={"time_exit": safe_error(e)})
            continue
        m = await client.market(sym)
        rows = [
            r
            for r in await client.closed_pnl(m.market_id, (t["opened_ts"] or now) - 60_000)
            if int(r.get("updatedTime") or 0) >= (t["opened_ts"] or 0)
        ]
        if not rows:
            continue  # the venue has not reported the close yet
        pnl = sum(float(r.get("closedPnl") or 0) for r in rows)
        exit_px = float(rows[0].get("avgExitPrice") or 0) or None
        reason = t["exit_reason"] or ("target" if exit_px is not None and abs(exit_px - t["target"]) < abs(exit_px - t["stop"]) else "stop")
        r_mult = pnl / t["risk_usd"] if t["risk_usd"] else None
        await store.demo_trade_update(lid, status="closed", exit=exit_px, closed_ts=now, pnl_usd=pnl, r_multiple=r_mult, exit_reason=reason)
        if t["journal_id"]:
            outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "break_even"
            await store.journal_update_close(
                t["journal_id"],
                closed_ts=now // 1000,
                status="closed",
                exit_=exit_px,
                outcome=outcome,
                pnl=pnl,
                r_multiple=r_mult,
                note=f"bybit-demo {reason}",
            )
        await _audit(
            store, now, "closed", link_id=lid, symbol=sym, setup=t["setup"], payload={"pnl_usd": pnl, "exit": exit_px, "reason": reason}
        )
        icon = "✅" if pnl > 0 else "❌"
        await notify(
            f"{icon} Демо: закрито {t['setup']} {t['side']} {sym.split(':')[0]} ({t['timeframe']}) — "
            f"{'ціль' if reason == 'target' else 'стоп' if reason == 'stop' else 'вихід за часом'}, "
            f"P&L {pnl:+.2f} USD = {r_mult:+.2f}R (вхід {_fmt(t['fill'] or t['entry'])}, вихід {_fmt(exit_px)})"
        )
        counts["closed"] += 1
    return counts


async def _state(store: Store, client: BybitDemo, now: int) -> State:
    positions = await client.positions()
    trades = await store.demo_trade_list(limit=5000)
    open_rows = [t for t in trades if t["status"] in ("open", "unknown", "sent")]
    closed_today = [t for t in trades if t["status"] == "closed" and (t["closed_ts"] or 0) >= day_start_ms(now)]
    day_loss = -sum(min(0.0, t["pnl_usd"] or 0.0) for t in closed_today) + open_risk_usd(
        [(t["qty"], t["fill"] or t["entry"], t["stop"]) for t in open_rows]
    )
    sent = await store.exec_audit_list(now - HOUR_MS, ("sent",))
    return State(
        now_ms=now,
        halted=HALT_FILE.exists(),
        open_symbols=tuple(sorted(set(positions) | {t["symbol"] for t in open_rows})),
        orders_last_hour=len(sent),
        day_loss_usd=day_loss,
        equity_usd=await client.equity_usd(),
        sent_link_ids=tuple(t["link_id"] for t in trades),
    )


async def enter(
    store: Store, client: BybitDemo, sig: LiveSignal, symbol: str, state: State, now: int, notify: Notify, cfg: GuardConfig, dry_run: bool
) -> tuple[State, str]:
    m = await client.market(symbol)
    last = await client.last_price(symbol)
    stop, target = _round(sig.stop, m.tick), _round(sig.target, m.tick)
    qty = size_for_risk(last, stop, cfg.risk_usd, m.lot_step, m.min_qty)
    lid = link_id(sig, symbol)
    p = Proposal(symbol, sig.side, last, stop, target, qty, sig.setup, lid)
    payload = {"signal": {**sig.__dict__}, "proposal": p.__dict__}
    await _audit(store, now, "signal", link_id=lid, symbol=symbol, setup=sig.setup, payload=payload)
    bad = check(p, state, cfg)
    if bad:
        await _audit(store, now, "refused", link_id=lid, symbol=symbol, setup=sig.setup, payload={"violations": bad})
        return state, "refused"
    if dry_run:
        return state, "dry_run"
    risk = qty * abs(last - stop)
    await store.demo_trade_insert(
        {
            "link_id": lid,
            "venue": VENUE,
            "symbol": symbol,
            "side": sig.side,
            "setup": sig.setup,
            "timeframe": sig.timeframe,
            "signal_ts": sig.signal_ts,
            "entry": last,
            "stop": stop,
            "target": target,
            "qty": qty,
            "risk_usd": risk,
            "comment": sig.comment,
            "status": "unknown",
        }
    )  # written before the order: a crash between the two leaves 'unknown', resolved by the next sync
    try:
        order = await client.place_bracket(symbol, sig.side, qty, stop, target, lid)
    except Exception as e:  # noqa: BLE001
        await store.demo_trade_update(lid, status="rejected")
        await _audit(store, now, "rejected", link_id=lid, symbol=symbol, setup=sig.setup, payload={"error": safe_error(e)})
        return state, "rejected"
    jid = await store.journal_insert(
        opened_ts=now // 1000,
        symbol=symbol,
        side=sig.side,
        size=qty,
        entry=last,
        stop=stop,
        target=target,
        thesis=sig.comment,
        tags=["demo-paper", "bybit-demo", f"setup:{sig.setup}", f"tf:{sig.timeframe}", "auto"],
    )
    await store.demo_trade_update(lid, status="open", order_id=str(order.get("id") or ""), opened_ts=now, journal_id=jid)
    await _audit(store, now, "sent", link_id=lid, symbol=symbol, setup=sig.setup, payload={"order_id": order.get("id"), "qty": qty})
    rr = abs(target - last) / abs(last - stop)
    await notify(
        f"🧪 Демо: вхід {sig.setup} {'лонг' if sig.side == 'long' else 'шорт'} {symbol.split(':')[0]} ({sig.timeframe})\n"
        f"Вхід ≈ {_fmt(last)}, стоп {_fmt(stop)}, ціль {_fmt(target)}, R:R {rr:.1f}, ризик {risk:.0f} USD, обсяг {qty:g}\n"
        f"Чому: {sig.comment}"
    )
    new_state = State(
        **{
            **state.__dict__,
            "open_symbols": (*state.open_symbols, symbol),
            "orders_last_hour": state.orders_last_hour + 1,
            "day_loss_usd": state.day_loss_usd + risk,
            "sent_link_ids": (*state.sent_link_ids, lid),
        }
    )
    return new_state, "sent"


async def cycle(
    store: Store, client: BybitDemo, now: int, notify: Notify, cfg: GuardConfig = DEFAULT, dry_run: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {"signals": 0, "sent": 0, "refused": 0, "rejected": 0, "errors": 0, "dry_run": 0}
    out.update(await sync(store, client, now, notify))
    state = await _state(store, client, now)
    out["halted"] = state.halted
    for symbol in cfg.symbols:
        for tf in TIMEFRAMES:
            try:
                bars = closed_bars(await client.ohlcv(symbol, tf), tf, now)
                sigs = [s for s in scan(bars, tf, SETUPS) if s.signal_ts + TF_MS[tf] >= now - FRESH_MS]
            except Exception as e:  # noqa: BLE001
                out["errors"] += 1
                await _audit(store, now, "error", symbol=symbol, payload={"scan": tf, "error": safe_error(e)})
                continue
            for sig in sigs:
                out["signals"] += 1
                if link_id(sig, symbol) in state.sent_link_ids:
                    continue  # already traded (or refused) in an earlier run
                try:
                    state, res = await enter(store, client, sig, symbol, state, now, notify, cfg, dry_run)
                    out[res] += 1
                except Exception as e:  # noqa: BLE001
                    out["errors"] += 1
                    await _audit(store, now, "error", symbol=symbol, setup=sig.setup, payload={"enter": safe_error(e)})
    return out


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


async def amain(db: str, dry_run: bool) -> int:
    keys = load_keys()
    if keys is None:
        print("no demo key (~/.config/tradedesk/bybit-demo.env): nothing to do")
        return 0
    store, client = Store(db), BybitDemo(*keys)
    await store.init()
    try:
        res = await cycle(store, client, int(time.time() * 1000), _telegram(), dry_run=dry_run)
        print(json.dumps(res, sort_keys=True))
        return 0
    finally:
        await client.aclose()
        await store.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("TRADEDESK_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true", help="scan, size and check, write the audit, but send no order")
    a = ap.parse_args(argv)
    return asyncio.run(amain(a.db, a.dry_run))


if __name__ == "__main__":
    sys.exit(main())
