"""Forward watch of three Hyperliquid vaults (the owner's decision of 2026-10-24) to Telegram, once a month.

    python3 scripts/vault_watch_tg.py            # send to the group's agents topic
    python3 scripts/vault_watch_tg.py --print    # print the Telegram HTML, send nothing

The top vaults report (docs/verification/2026-10-23-top-vaults-profile.md) found no proven edge; Growi HF (the
smoothest curve) and Delta_01 / Jade Lotus Capital (the highest alpha t, fading) are watched forward from
START (2026-10-24 00:00 UTC) under the criteria registered in IDEA-20261015-smart-money-persistence («Волти»).
For each vault: the time-weighted return since START from the official ``vaultDetails`` series (the same
``twr_curve`` as the report, flows stripped), the largest fall since START, capital, and BTC 1x over the same days
(``candleSnapshot`` daily closes). Read-only public API, no keys; vault names and texts are untrusted data and are
HTML-escaped. A vault the API cannot serve gives a line with the reason, never an error. Every run appends its
numbers to LOG (one JSON line), so the record survives a vault that closes. Scheduled by
deploy/user-units/tradedesk-vault-watch.* (the 1st of each month, 10:00 Europe/Kyiv) while FLAG exists.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "research"))

import tg_send  # noqa: E402
import vault_profile as vp  # noqa: E402

START_MS = int(datetime(2026, 10, 24, tzinfo=UTC).timestamp() * 1000)
CHECKS = ("24.01.2027", "24.04.2027")  # the registered verdict dates (3 and 6 months)
# (name, address, beta to BTC from the report, its max drawdown in the report)
VAULTS = (
    ("Growi HF", "0x1e37a337ed460039d1b15bd3bc489de789768d5e", 0.25, 0.12),
    ("Delta_01", "0x3005fade4c0df5e1cd187d7062da359416f0eb8e", 0.61, 0.42),
    ("Jade Lotus Capital", "0xbc5bf88fd012612ba92c5bd96e183955801b7fdc", 1.07, 0.37),
)
LOG = Path("/var/lib/tradedesk-mcp/assistant/vault_watch.jsonl")
FLAG = Path("/var/lib/tradedesk-mcp/assistant/vault-watch.on")
CARD = "docs/ideas/IDEA-20261015-smart-money-persistence.md"


def since(details: dict, start_ms: int) -> dict:
    """Return, largest fall and capital since ``start_ms`` from one ``vaultDetails`` answer (allTime series)."""
    av, pnl = vp.series(details, "accountValueHistory"), vp.series(details, "pnlHistory")
    if len(pnl) < 2 or not av:
        return {"error": "немає історії"}
    nav = vp.twr_curve(av, pnl)
    after = [v for t, v in nav if t >= start_ms]
    base = vp.interp(nav, start_ms) if nav[0][0] <= start_ms else nav[0][1]
    path = [base, *after]
    if base <= 0:
        return {"error": "капітал волта обнулився до старту"}
    return {
        "ret": path[-1] / base - 1,
        "max_dd": vp.max_drawdown(path)[0],
        "tvl": av[-1][1],
        "points": len(after),
        "closed": bool(details.get("isClosed")),
    }


def btc_return(candles: list[dict], start_ms: int) -> float | None:
    """BTC close-to-close from the last daily candle that opened before ``start_ms`` to the newest one."""
    rows = sorted((int(c["t"]), float(c["c"])) for c in candles)
    before = [c for t, c in rows if t < start_ms]
    if not before or not rows:
        return None
    return rows[-1][1] / before[-1] - 1


def verdict(r: dict, beta: float, max_dd: float, btc: float | None) -> str:
    """The registered criterion, read off now (final only at the CHECKS dates)."""
    if "error" in r:
        return ""
    if r["closed"]:
        return "закрито"
    keeps = r["ret"] - beta * (btc or 0.0) > 0 and r["max_dd"] < max_dd
    return "тримає" if keeps else "не тримає"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f} %"


def build(rows: list[tuple[str, float, float, dict]], btc: float | None, now: datetime) -> str:
    days = (now.timestamp() * 1000 - START_MS) / vp.DAY_MS
    lines = [f"🏦 <b>Волти HL: спостереження</b>, {days:.0f} дн. з 24.10.2026", f"BTC 1x за цей час: {pct(btc)}", ""]
    for name, beta, max_dd, r in rows:
        if "error" in r:
            lines.append(f"• <b>{html.escape(name)}</b>: {html.escape(r['error'])}")
            continue
        alpha = r["ret"] - beta * (btc or 0.0)
        lines.append(
            f"• <b>{html.escape(name)}</b>: {pct(r['ret'])}, понад β·BTC {pct(alpha)}, просадка {r['max_dd'] * 100:.0f} % "
            f"(межа {max_dd * 100:.0f} %), капітал ${r['tvl'] / 1e6:.1f}M → {verdict(r, beta, max_dd, btc)}"
        )
    lines += ["", f"Вердикт за критерієм картки — {CHECKS[0]} і {CHECKS[1]}; до того лише проміжні цифри, не сигнал."]
    return "\n".join(lines)


def collect(post=vp._post, now_ms: int | None = None) -> tuple[list, float | None]:
    now_ms = now_ms or int(time.time() * 1000)
    rows = []
    for name, addr, beta, max_dd in VAULTS:
        try:
            r = since(post({"type": "vaultDetails", "vaultAddress": addr}), START_MS)
        except Exception as e:  # noqa: BLE001 — one vault's failure is a line, not a crash
            r = {"error": f"API: {type(e).__name__}"}
        rows.append((name, beta, max_dd, r))
    try:
        req = {"coin": "BTC", "interval": "1d", "startTime": START_MS - 3 * vp.DAY_MS, "endTime": now_ms}
        btc = btc_return(post({"type": "candleSnapshot", "req": req}), START_MS)
    except Exception:  # noqa: BLE001
        btc = None
    return rows, btc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the Telegram HTML, send nothing")
    ap.add_argument("--topic", default="agents", help="forum topic (default: agents)")
    a = ap.parse_args(argv)
    rows, btc = collect()
    now = datetime.now(UTC)
    text = build(rows, btc, now)
    if a.print_only:
        print(text)
        return 0
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now.isoformat(), "btc": btc, "vaults": {n: r for n, _, _, r in rows}}) + "\n")
    except OSError as e:
        print(f"log: {e}", file=sys.stderr)
    return 0 if tg_send.send(text, topic=a.topic, buttons=[("📋 Картка", f"https://github.com/nick198922/MCP/blob/main/{CARD}")]) else 1


if __name__ == "__main__":
    sys.exit(main())
