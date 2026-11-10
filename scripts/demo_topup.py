"""One-off top-up of the Bybit demo account's play balance (owner 10.11, ADR 0007 addition 10.11: up to 25 positions,
the balance raised to about $50k so the margin checks do not become the new ceiling).

Reads the equity, asks Bybit's demo host for the difference to --target (at most --max per run) and writes a `funds`
row to `exec_audit`, so the jump in equity is never read as profit. Nothing happens when the equity is already at or
above the target. Demo host only (``BybitDemo`` refuses any other); it runs under the demo trader's lock. The key is
read by ``load_keys`` and never printed.

    python3 scripts/demo_topup.py [--target 50000] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from tradedesk.execution.bybit_demo import DEMO_FUNDS_MAX, BybitDemo, load_keys
from tradedesk.execution.demo_trader import DEFAULT_DB, VENUE
from tradedesk.store import Store

TARGET_USD = 50_000.0


def amount_needed(equity: float, target: float, cap: float = DEMO_FUNDS_MAX) -> int:
    """Whole USDT to add to reach the target, at most ``cap``; 0 when already there or the equity is unknown."""
    if equity <= 0 or equity >= target:
        return 0
    return int(min(target - equity, cap))


async def run(client: Any, store: Store, now_ms: int, target: float, dry_run: bool) -> dict[str, Any]:
    before = await client.equity_usd()
    add = amount_needed(before, target)
    rep: dict[str, Any] = {"equity_before": round(before, 2), "target": target, "add_usdt": add, "dry_run": dry_run}
    if not add or dry_run:
        return rep
    answer = await client.apply_demo_funds(add)
    rep["ret_code"] = (answer or {}).get("retCode")
    if str(rep["ret_code"]) != "0":
        rep["error"] = "the venue did not confirm the funds: nothing written to exec_audit"
        return rep
    await asyncio.sleep(3)  # the wallet shows the funds a moment later
    rep["equity_after"] = round(await client.equity_usd(), 2)
    await store.exec_audit_add(
        now_ms,
        VENUE,
        "funds",
        payload={"usdt": add, "equity_before": rep["equity_before"], "equity_after": rep["equity_after"], "why": "owner 10.11, ADR 0007"},
    )
    return rep


async def amain(db: str, keys: tuple[str, str], target: float, dry_run: bool) -> int:
    store, client = Store(db), BybitDemo(*keys)
    await store.init()
    try:
        rep = await run(client, store, int(time.time() * 1000), target, dry_run)
        code = 0
    except Exception as e:  # noqa: BLE001 - print the redacted error, never the key
        rep, code = {"error": client.safe_error(e)}, 2
    finally:
        await client.aclose()
        await store.aclose()
    print(json.dumps(rep, ensure_ascii=False, sort_keys=True))
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=os.environ.get("TRADEDESK_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--target", type=float, default=TARGET_USD, help=f"equity to reach, USD (at most {TARGET_USD:,.0f}, ADR 0007)")
    ap.add_argument("--dry-run", action="store_true", help="read the equity and print the amount; no request")
    a = ap.parse_args(argv)
    if a.target > TARGET_USD:
        print(f"--target above {TARGET_USD:,.0f} USD is outside ADR 0007 (addition 10.11)")
        return 2
    keys = load_keys()
    if keys is None:
        print("no demo key (~/.config/tradedesk/bybit-demo.env): nothing to do")
        return 0
    with open(Path(a.db).parent / "demo-trader.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # never at the same time as the demo trader's runs
        return asyncio.run(amain(a.db, keys, a.target, a.dry_run))


if __name__ == "__main__":
    sys.exit(main())
