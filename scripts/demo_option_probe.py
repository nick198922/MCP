"""One-shot check of the demo trader's option path (owner 23.10, slice 1): guard -> client -> Bybit Demo -> cancel.

Picks a far out-of-the-money BTC call (strike >= 1.10 x index) with at least 5 days to expiry (the guard's 4-day floor
plus a day of margin) whose book passes the guard's spread filter, plans ONE post-only limit buy of the venue's minimum
quantity at about half the bid (so it rests and does not fill), and runs it through ``guard.check_option`` with the
same account state the futures entries see (kill-switch, daily caps, orders per hour, open risk).

Default is a dry run: it reads the venue and the trader's DB and prints the plan and the guard's verdict; nothing is
sent and no audit row is written. With ``--place`` it writes the decision to the append-only ``exec_audit``, places the order,
confirms it among the open orders, cancels it and confirms the cancel. Only ``api-demo.bybit.com`` (play money) is
reachable: ``BybitDemo`` refuses any other host.

    uv run python scripts/demo_option_probe.py            # dry run: the plan and the guard's verdict
    uv run python scripts/demo_option_probe.py --place    # one resting order, then cancelled
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from tradedesk.execution.bybit_demo import BybitDemo, OptionInstrument, OptionQuote, is_refusal, load_keys
from tradedesk.execution.demo_trader import DEFAULT_DB, LIVE, VENUE, shared_state
from tradedesk.execution.guard import DAY_MS, DEFAULT, GuardConfig, check_option, plan_option
from tradedesk.store import Store

BASE = "BTC"
MIN_MONEYNESS = 1.10  # strike / index of a "far OTM" call
DTE_MARGIN_DAYS = 1.0  # above the guard's floor, so a slow run never lands on the edge
MIN_BID_TICKS = 4  # the bid must be at least 4 ticks, so half of it stays a tick or more below the bid
SETUP = "probe"


def pick_far_otm_call(
    instruments: Sequence[OptionInstrument], quotes: dict[str, OptionQuote], now_ms: int, cfg: GuardConfig = DEFAULT
) -> tuple[OptionInstrument, OptionQuote] | None:
    """The nearest expiry's farthest out-of-the-money call that passes the spread filter and has a bid of at least
    ``MIN_BID_TICKS`` ticks, at least ``option_min_dte_days + DTE_MARGIN_DAYS`` days out; None when none does."""
    ok = []
    for i in instruments:
        q = quotes.get(i.symbol)
        if q is None or not i.is_call or i.base != BASE or q.index <= 0:
            continue
        if (i.expiry_ms - now_ms) / DAY_MS < cfg.option_min_dte_days + DTE_MARGIN_DAYS or i.strike < MIN_MONEYNESS * q.index:
            continue
        if q.bid < MIN_BID_TICKS * i.tick or q.ask <= q.bid or (q.ask - q.bid) / ((q.ask + q.bid) / 2) > cfg.option_max_spread:
            continue
        ok.append((i.expiry_ms, -i.strike, i, q))
    if not ok:
        return None
    _, _, i, q = min(ok, key=lambda x: (x[0], x[1]))
    return i, q


def probe_price(bid: float, tick: float) -> float:
    """About half the bid, on the tick, at least one tick: a buy limit that rests well below the book."""
    return max(tick, math.floor(bid * 0.5 / tick + 1e-9) * tick)


def probe_link_id(now_ms: int) -> str:
    return f"tdo-probe-{now_ms // 1000}"


async def run(
    client: BybitDemo,
    store: Store,
    now_ms: int,
    place: bool,
    cfg: GuardConfig = DEFAULT,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[int, dict[str, Any]]:
    """(exit code, report). 0: dry run passed the guard, or the order was seen open and cancelled; 1: no candidate or
    the guard refused; 2: something after the order went wrong (see the report; an order may still be open)."""
    rep: dict[str, Any] = {"mode": "place" if place else "dry-run", "now_ms": now_ms}
    insts = await client.option_instruments(BASE)
    quotes = await client.option_quotes(BASE)
    picked = pick_far_otm_call(insts, quotes, now_ms, cfg)
    if picked is None:
        rep["result"] = f"no far-OTM {BASE} call with >= {cfg.option_min_dte_days + DTE_MARGIN_DAYS:g} DTE passes the filters"
        return 1, rep
    inst, q = picked
    lid = probe_link_id(now_ms)
    p = plan_option(
        inst.symbol, probe_price(q.bid, inst.tick), inst.expiry_ms, q.bid, q.ask, q.index, SETUP, lid, inst.lot_step, inst.min_qty, cfg
    )
    p = replace(p, qty=min(p.qty, inst.min_qty))  # one order of the venue's minimum size
    positions = await client.positions()
    opts = await client.option_positions()
    equity = await client.equity_usd()
    state = await shared_state(store, client, now_ms, positions, equity)
    state = replace(state, open_symbols=tuple(sorted({*state.open_symbols, *opts})))
    violations = check_option(p, state, cfg)
    rep["plan"] = {
        **asdict(p),
        "dte_days": round((inst.expiry_ms - now_ms) / DAY_MS, 2),
        "strike": inst.strike,
        "mark": q.mark,
        "mark_iv": q.mark_iv,
        "premium_usd": round(p.premium_usd, 4),
        "risk_usd": round(p.risk_usd, 4),
        "spread": round(p.spread, 4),
    }
    rep["state"] = {k: getattr(state, k) for k in ("halted", "trades_today", "entries_today", "orders_last_hour", "equity_usd")}
    rep["violations"] = violations
    if violations:
        if place:
            await store.exec_audit_add(
                now_ms, VENUE, "refused", link_id=lid, symbol=p.symbol, setup=SETUP, payload={"violations": violations}
            )
        rep["result"] = "refused by the guard"
        return 1, rep
    if not place:
        rep["result"] = "dry run: the guard passes; nothing sent (add --place to send it)"
        return 0, rep
    return await _place_and_cancel(client, store, inst, p.qty, p.price, lid, now_ms, rep, sleep)


async def _place_and_cancel(
    client: BybitDemo,
    store: Store,
    inst: OptionInstrument,
    qty: float,
    price: float,
    lid: str,
    now_ms: int,
    rep: dict[str, Any],
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[int, dict[str, Any]]:
    async def audit(kind: str, payload: dict[str, Any]) -> None:
        await store.exec_audit_add(now_ms, VENUE, kind, link_id=lid, symbol=inst.symbol, setup=SETUP, payload=payload)

    await audit("sent", {"option": "buy", "qty": qty, "price": price, "probe": True})
    try:
        rep["order"] = await client.place_option_buy(inst, qty, price, lid)
    except Exception as e:  # noqa: BLE001 - a refusal is final; anything else may have reached the venue
        rep["order_error"] = client.safe_error(e)
        if is_refusal(e):
            await audit("rejected", {"error": rep["order_error"]})
            rep["result"] = "the venue refused the order"
            return 2, rep
    try:
        seen = None
        for _ in range(5):
            seen = await client.option_order_by_link(inst.symbol, lid)
            if seen is not None:
                break
            await sleep(1.0)
        rep["status_after_place"] = (seen or {}).get("orderStatus")
        rep["in_open_orders"] = any(o.get("orderLinkId") == lid for o in await client.option_open_orders(inst.base))
        rep["cancelled"] = await client.cancel_option(inst.symbol, lid)
    finally:
        if "cancelled" not in rep:  # a read failed after the order: still try to take it off the book
            try:
                rep["cancelled_after_error"] = await client.cancel_option(inst.symbol, lid)
            except Exception as e:  # noqa: BLE001 - reported; the exception above propagates
                rep["cancel_error"] = client.safe_error(e)
            print(json.dumps(rep, indent=1, sort_keys=True, ensure_ascii=False, default=str))
    after = await client.option_order_by_link(inst.symbol, lid)
    rep["status_after_cancel"] = (after or {}).get("orderStatus")
    await audit("cancelled", {k: rep[k] for k in ("status_after_place", "in_open_orders", "cancelled", "status_after_cancel")})
    ok = rep["status_after_place"] in LIVE and rep["in_open_orders"] and rep["cancelled"] and rep["status_after_cancel"] == "Cancelled"
    rep["result"] = (
        "ok: rested, seen among the open orders, cancelled" if ok else "CHECK: see the statuses (an order may be open or filled)"
    )
    return (0 if ok else 2), rep


async def amain(db: str, keys: tuple[str, str], place: bool) -> int:
    store, client = Store(db), BybitDemo(*keys)
    await store.init()
    try:
        code, rep = await run(client, store, int(time.time() * 1000), place)
    except Exception as e:  # noqa: BLE001 - print the redacted error, never the key
        code, rep = 2, {"error": client.safe_error(e)}
    finally:
        await client.aclose()
        await store.aclose()
    print(json.dumps(rep, indent=1, sort_keys=True, ensure_ascii=False, default=str))
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("TRADEDESK_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--place", action="store_true", help="really place the one resting order on the demo account, then cancel it")
    a = ap.parse_args(argv)
    keys = load_keys()
    if keys is None:
        print("no demo key (~/.config/tradedesk/bybit-demo.env): nothing to do")
        return 0
    with open(Path(a.db).parent / "demo-trader.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # never at the same time as the demo trader's runs
        return asyncio.run(amain(a.db, keys, a.place))


if __name__ == "__main__":
    sys.exit(main())
