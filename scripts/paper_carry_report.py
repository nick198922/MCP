"""Weekly report of the forward paper carry books: reads a COPY of the collector DB, no network, writes a Markdown file.

    uv run python scripts/paper_carry_report.py [--db PATH] [--out PATH] [--now-ms 0] [--print] [--telegram]

Defaults: the collector DB /var/lib/tradedesk-mcp/tradedesk.sqlite; output ``--out``, else $TRADEDESK_PAPER_CARRY_REPORT,
else /var/lib/tradedesk-mcp/paper/carry_REPORT.md (outside git). ``--telegram`` sends the text to the group's ``agents``
topic through scripts/tg_send.py. The user timer tradedesk-paper-carry-report runs it on Sundays.

The script never writes to the given DB: it takes a consistent snapshot (sqlite3 backup, source opened read-only) into a
temporary file and works on that, so the schema migration cannot touch the collector's database. Everything comes from the
engine's own records (``cp_*`` tables) through ``book_view``, the same numbers as the MCP tool ``paper_carry_status``;
actual-vs-model costs come from ``analytics/carry_report.py``. This is bookkeeping, not a profit claim: the paper P&L at
this sample size cannot separate the expected edge from zero, and strategy B is expected at about zero or negative.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tradedesk.store import Store
from tradedesk.tools.paper_carry import book_view

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_OUT = "/var/lib/tradedesk-mcp/paper/carry_REPORT.md"
ENV_OUT = "TRADEDESK_PAPER_CARRY_REPORT"

NAMES = {"A": "A: cash-and-carry (Binance spot + perp), tag cc-paper", "B": "B: perp-perp Binance<->Bybit, tag pp-paper"}
EXPECTATION = {
    "A": "Backtest about +6.5 % APR on notional (docs/verification/2026-10-12-funding-carry-alts.md); small and fading.",
    "B": (
        "Expectation about zero or negative: on the full funding history the backtest is -8.7 % APR on notional (2x with stop) "
        "and +2.9 % without the stop (docs/verification/2026-10-17-paper-carry-bench.md). The earlier +76 % was an artefact of "
        "holes in the Bybit funding history."
    ),
}


def _pct(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:.{nd}f} %"


def _usd(x: Any) -> str:
    return "n/a" if x is None else f"{x:,.2f} USD"


def _num(x: Any, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


DEFER_SHARE_LIMIT = 0.20  # registration (4): above this share of deferred trades the (v) criterion is not assessable
DEFER_MIN_TRADES = 10


def deferred_line(deferred: int, total: int) -> str:
    """Trades held at least once for a missing funding payment, as a count and a share of all trades of the book."""
    share = deferred / total if total else None
    line = f"- Deferred trades (held for a missing funding payment): {deferred} of {total} ({_pct(share, 1)})"
    if total < DEFER_MIN_TRADES:
        return line + f"; fewer than {DEFER_MIN_TRADES} trades: criterion (v) not assessable (unverifiable)"
    if share is not None and share > DEFER_SHARE_LIMIT:
        return line + f"; above {DEFER_SHARE_LIMIT:.0%}: criterion (v) not assessable (unverifiable)"
    return line


def render_book(key: str, v: dict[str, Any], events: dict[str, int], deferred: tuple[int, int] = (0, 0)) -> list[str]:
    e, c, pnl, cost = v["engine"], v["closed"], v["pnl_usd"], v["cost_realism"]
    funding_open = sum((o.get("mark") or {}).get("funding_usd", 0.0) for o in v["open"])
    funding_closed = c.get("funding_usd", 0.0) if c.get("n") else 0.0
    out = [f"## {NAMES[key]}", "", f"Expectation: {EXPECTATION[key]}", ""]
    out += [
        f"- Engine: {e['cycles_done']} cycles done, {e['cycles_skipped']} skipped, last {e['last_cycle']}, "
        f"on time within {e['on_time_window_minutes']} min: {_pct(e['on_time_share'], 1)}, "
        f"last cycle complete: {e['last_cycle_complete']}, flags: {e['last_cycle_flags']}",
        f"- Events: {', '.join(f'{k} x{n}' for k, n in sorted(events.items())) or 'none'}",
        deferred_line(*deferred),
        f"- Open positions: {len(v['open'])} (free slots {v['free_slots']}); closed trades: {c.get('n', 0)}",
        f"- P&L (USD, portfolio notional {pnl['portfolio_notional_usd']:,.0f}): realised {_usd(pnl['realised'])}, "
        f"open marked at last cycle {_usd(pnl['open_marked_at_last_cycle'])}, total {_usd(pnl['total'])}; "
        f"on notional {_pct(pnl['on_notional'])}, on capital {_pct(pnl['on_capital'])} (cumulative since start, not annualised)",
        f"- Funding collected: closed {_usd(funding_closed)}, open (marked) {_usd(funding_open)}",
    ]
    if c.get("n"):
        out.append(
            f"- Closed trades: mean return on notional {_pct(c['mean_return_on_notional'])}, median "
            f"{_pct(c['median_return_on_notional'])}, losing {_pct(c['share_losing'], 0)}, stopped {_pct(c['share_stopped'], 0)}, "
            f"mean {_num(c['mean_days'], 1)} days; price/basis {_usd(c['price_or_basis_usd'])}, costs {_usd(c['costs_usd'])}"
        )
    out.append(
        f"- Costs, actual vs model per leg side: {cost.get('with_book', 0)} of {cost.get('sides', 0)} sides with a book; "
        f"mean actual {_pct(cost.get('mean_actual_cost'), 3)} vs model {_pct(cost.get('mean_model_cost'), 3)}, "
        f"ratio {_num(cost.get('ratio'))}, p90 {_num(cost.get('ratio_p90'))}, share with short depth {_pct(cost.get('share_depth_short'), 0)}"
    )
    if v["open"]:
        out += [
            "",
            "| Symbol | Opened | Days | Entry | Funding USD | Price/basis USD | Costs USD | P&L USD |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for o in v["open"]:
            m = o.get("mark") or {}
            pb = m.get("basis_usd", m.get("price_usd"))
            out.append(
                f"| {o['symbol']} | {o['opened']} | {_num(o['held_days'], 1)} | {_num(o['entry_apr'], 3)} | "
                f"{_num(m.get('funding_usd'))} | {_num(pb)} | {_num(m.get('costs_usd'))} | {_num(m.get('pnl_usd'))} |"
            )
    return out + [""]


def _snapshot(src: str, dst: str) -> None:
    """Consistent copy of ``src`` into ``dst`` (the source is opened read-only)."""
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        b = sqlite3.connect(dst)
        try:
            a.backup(b)
        finally:
            b.close()
    finally:
        a.close()


async def build_report(db: str, now_ms: int) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        copy = str(Path(tmp) / "copy.sqlite")
        await asyncio.to_thread(_snapshot, db, copy)
        return await _build(copy, now_ms)


async def _build(db: str, now_ms: int) -> str:
    store = Store(db)
    await store.init()
    try:
        rt = SimpleNamespace(store=store)
        lines = [
            "# Paper carry: weekly report",
            "",
            f"Generated {datetime.fromtimestamp(now_ms / 1000, UTC).isoformat(timespec='minutes')} from a snapshot of the collector DB"
            " (no network). Forward PAPER books: local bookkeeping, no order was ever sent, fills are modelled.",
            "",
            "Reading guide: at this sample size the paper P&L is descriptive only and cannot separate the expected edge from zero "
            "(registration: docs/ideas/IDEA-20261017-forward-paper-carry.md). Nothing here is a profit promise or a recommendation.",
            "",
        ]
        for key in ("A", "B"):
            v = await book_view(rt, key, now_ms, include_closed=False)  # type: ignore[arg-type]
            events: dict[str, int] = {}
            for ev in await store.cp_logs(key, "event"):
                t = (ev["payload"] or {}).get("type", "?")
                events[t] = events.get(t, 0) + 1
            deferred_ids = {
                ev["trade_id"] for ev in await store.cp_logs(key, "event") if (ev["payload"] or {}).get("type") == "funding_payment_missing"
            }
            total = len(await store.cp_positions(key))
            lines += render_book(key, v, events, (len(deferred_ids), total))
        return "\n".join(lines).rstrip() + "\n"
    finally:
        await store.aclose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector SQLite DB (opened read-only; a snapshot is used)")
    ap.add_argument("--out", default="", help=f"output Markdown (default ${ENV_OUT} or {DEFAULT_OUT})")
    ap.add_argument("--now-ms", type=int, default=0, help="report time in UTC ms (default: now)")
    ap.add_argument("--print", action="store_true", help="also print the text")
    ap.add_argument("--telegram", action="store_true", help="send the text to the Telegram group (topic agents)")
    a = ap.parse_args(argv)
    text = asyncio.run(build_report(a.db, a.now_ms or int(time.time() * 1000)))
    out = Path(a.out or os.environ.get(ENV_OUT) or DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    if a.print:
        print(text)
    if a.telegram:
        from tg_send import md_to_html, send  # noqa: PLC0415

        if not send(md_to_html(text), topic="agents", silent=True):
            return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
