"""paper_carry_status: read-only view of the forward paper engine's books (collector/carry_paper_engine.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.carry_report import closed_stats, cost_realism
from ..collector.carry_paper_engine import ON_TIME_MS, PARAMS
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r
from .journal import _store_guard

PAPER_CARRY_WARNING = (
    "Forward PAPER books, local bookkeeping only: no order is sent. Positions are opened and closed by the collector engine "
    "at the bar close after each 8h funding payment, with modelled costs; the recorded order book shows what execution would "
    "have cost. Marks are as of the last engine cycle, not live. Backtests: A (docs/verification/2026-10-12-funding-carry-alts.md, "
    "Binance, 2024-10..2026-10) about +6.5 % APR on notional, 40 % of trades losing, average trade 62 days, in 21 % of trades the "
    "perp rose >= 100 % while open (liquidation risk of the short perp, margin top-ups not modelled); B "
    "(docs/verification/2026-10-17-paper-carry-bench.md, full funding history) is expected at about zero or negative: -8.7 % APR "
    "on notional at 2x per leg with stop, +2.9 % without the stop (the earlier +76 % was an artefact of holes in Bybit funding "
    "history). Not modelled: position limits, venue rule changes, separate margins. At this "
    "sample size the paper P&L is descriptive only: it cannot separate the expected edge from zero (see the registration in "
    "docs/ideas/IDEA-20261017-forward-paper-carry.md)."
)
MARK_KEYS = (
    "funding_usd",
    "basis_usd",
    "price_usd",
    "costs_usd",
    "pnl_usd",
    "dist_to_liq_2x",
    "dist_to_stop",
    "trailing_apr",
    "held_spread_apr",
)


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def _rounded(d: dict[str, Any], nd: int = 5) -> dict[str, Any]:
    return {k: (r(v, nd) if isinstance(v, float) else _rounded(v, nd) if isinstance(v, dict) else v) for k, v in d.items()}


async def book_view(rt: Runtime, key: str, now_ms: int, include_closed: bool) -> dict[str, Any]:
    store = rt.store
    assert store is not None
    p = PARAMS[key]
    cycles = await store.cp_cycles(key, 1000)
    done = [c for c in cycles if c["status"] == "done"]
    last = done[0] if done else None
    marks: dict[str, dict[str, Any]] = {}
    for m in await store.cp_logs(key, "mark"):
        marks[m["trade_id"]] = {**m["payload"], "cycle_ts": m["cycle_ts"]}  # ordered by id: the latest wins
    open_rows = await store.cp_positions(key, "open")
    open_view = []
    for row in open_rows:
        st, mk = row["state"], marks.get(row["trade_id"])
        view = {
            "trade_id": row["trade_id"],
            "symbol": row["symbol"],
            "opened": _iso(row["opened_ts"]),
            "held_days": r((now_ms - row["opened_ts"]) / 86_400_000, 2),
            "entry_apr": r(st.get("entry_apr", st.get("entry_spread")), 4),
            "last_mark_at": _iso(mk["cycle_ts"]) if mk else None,
        }
        if key == "B":
            view["direction"] = "long A(binanceusdm) / short B(bybit)" if st["dir"] == 1 else "short A(binanceusdm) / long B(bybit)"
        if mk:
            view["mark"] = _rounded({k: mk[k] for k in MARK_KEYS if k in mk})
        open_view.append(view)
    closed_rows = await store.cp_positions(key, "closed", limit=5000)
    trades = [x["trade"] for x in closed_rows if x.get("trade")]
    sides = [s["payload"] for s in await store.cp_logs(key, "side")]
    unrealised = sum((marks.get(x["trade_id"]) or {}).get("pnl_usd", 0.0) for x in open_rows)
    realised = sum(t["pnl_usd"] for t in trades)
    notional = p.slot_notional * p.slots
    out: dict[str, Any] = {
        "variant": {
            "tag": p.tag,
            "slots": p.slots,
            "slot_notional_usd_per_leg": p.slot_notional,
            "entry_apr": p.entry_apr,
            "exit_apr": p.exit_apr,
            "lookback_days": p.lookback_days,
            "max_entry_gap": p.max_entry_gap,
            "stop_move": p.stop_move,
            "capital_per_notional": p.capital_factor,
        },
        "engine": {
            "cycles_done": len(done),
            "cycles_skipped": sum(1 for c in cycles if c["status"] == "skipped"),
            "last_cycle": _iso(last["cycle_ts"]) if last else None,
            "last_cycle_on_time": last["payload"].get("on_time") if last else None,
            "last_cycle_complete": last["payload"].get("complete") if last else None,
            "last_cycle_flags": len(last["payload"].get("flags") or []) if last else None,
            "on_time_share": r(sum(1 for c in done if c["payload"].get("on_time")) / len(done), 3) if done else None,
            "on_time_window_minutes": ON_TIME_MS // 60_000,
            "git_sha": last.get("git_sha") if last else None,
        },
        "open": open_view,
        "free_slots": p.slots - len(open_rows),
        "closed": _rounded(closed_stats(trades)),
        "pnl_usd": {
            "realised": r(realised, 2),
            "open_marked_at_last_cycle": r(unrealised, 2),
            "total": r(realised + unrealised, 2),
            "on_notional": r((realised + unrealised) / notional, 5),
            "on_capital": r((realised + unrealised) / (notional * p.capital_factor), 5),
            "portfolio_notional_usd": notional,
        },
        "cost_realism": _rounded(cost_realism(sides)),
    }
    if include_closed:
        out["closed_trades"] = [
            _rounded(
                {
                    k: t[k]
                    for k in ("symbol", "exit_reason", "days", "funding_usd", "price_usd", "costs_usd", "pnl_usd", "return_on_notional")
                }
            )
            | {"opened": _iso(t["open_ts"]), "closed": _iso(t["close_ts"])}
            for t in sorted(trades, key=lambda t: t["close_ts"], reverse=True)[:50]
        ]
    return out


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "State of the two forward PAPER carry books run by the collector engine (local store only; nothing is traded and no "
            "live data is fetched). Strategy A = cash-and-carry on Binance alts (long spot + short perp, 5 slots, enter when the "
            "trailing 7d funding APR >= 20 %, exit <= 0 %; tag cc-paper). Strategy B = Binance<->Bybit perp-perp carry (5 slots, "
            "trailing spread >= 10 %, price gap <= 2 %, 2x per leg, stop at 50 % against a leg + 1 % penalty; tag pp-paper). Slot = "
            "2000 USD per leg. Per strategy: the variant, engine health (last cycle, share of cycles run within 30 minutes of the "
            "funding payment, skipped cycles), open positions with their latest mark as of the LAST 8h cycle (funding, basis or "
            "price P&L, costs, distance to the 2x liquidation or the stop; not live), closed-trade stats (returns on notional and "
            "on capital as decimal fractions, 0.01 = 1 %; share losing; funding / price / costs in USD), total P&L in USD, on "
            "notional and on capital (A capital = 1.5 x notional, B = 1.0 x), and cost realism: mean actual cost per executed leg "
            "side (taker fee + |VWAP - mid| / mid from the recorded book) vs the modelled fee + slippage. Paper results are not "
            "live fills and say nothing about profit at this sample size; read them against the registered criteria. Not the "
            "same as carry_paper_status, which marks manually opened perp-perp pairs with live data."
        ),
    )
    async def paper_carry_status(
        strategy: Annotated[
            Literal["A", "B", "both"], Field(description="Which book: A (cash-and-carry), B (perp-perp) or both.")
        ] = "both",
        include_closed: Annotated[bool, Field(description="Also list the 50 most recent closed trades per strategy.")] = False,
    ) -> dict[str, Any]:
        b = Builder()
        b.warn(PAPER_CARRY_WARNING)
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        assert rt.store is not None
        await rt.store.init()
        now_ms = int(rt.now().timestamp() * 1000)
        out: dict[str, Any] = {}
        for key in ("A", "B") if strategy == "both" else (strategy,):
            try:
                out[key] = await book_view(rt, key, now_ms, include_closed)
            except Exception as e:  # noqa: BLE001 - a store error must not escape the tool
                b.miss(f"paper_carry:{key}", "store", "backend_error", f"{type(e).__name__}: {e}")
        return b.build(out or None, ok=bool(out))
