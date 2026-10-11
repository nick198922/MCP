"""Trading journal: add / list / close trades and compute summary stats.

Journal lives in the local SQLite store (table ``journal``). PnL is reported
in quote-currency units (USD for linear perps / spot; for inverse crypto
options you'd convert outside). R-multiple needs an entry + stop.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.journal import outcome_of, r_multiple, summary
from ..analytics.journal import pnl as pnl_fn
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, r


def _store_guard(b: Builder, rt: Runtime) -> bool:
    if rt.store is None:
        b.miss("store", "tradedesk", "config", "db_path is empty; set TRADEDESK_DB_PATH to enable the journal")
        return False
    return True


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # journal_add writes to the LOCAL store only — no external effects
        description=(
            "Add an open trade to the local journal. Returns the new trade id. `side` is 'long' or "
            "'short'; `size` is in native units (contracts / coins). `stop` is optional but strongly "
            "recommended — without it R-multiple on close cannot be computed. `tags` is a free-form "
            "list of strings. All USD / price fields are decimals. Writes stay inside the local SQLite."
        ),
    )
    async def journal_add(
        symbol: Annotated[str, Field(description="Ticker / symbol as you track it, e.g. 'BTC/USDT:USDT'.")],
        side: Literal["long", "short"],
        size: Annotated[float, Field(gt=0)],
        entry: Annotated[float, Field(gt=0, description="Entry price in quote currency.")],
        stop: Annotated[float | None, Field(gt=0, description="Stop price; without it r_multiple on close is None.")] = None,
        target: Annotated[float | None, Field(gt=0, description="Target price.")] = None,
        thesis: Annotated[str | None, Field(description="Short note about why this trade.")] = None,
        tags: Annotated[list[str] | None, Field(description="Free-form tags; filter with journal_list.")] = None,
        opened_ts: Annotated[int | None, Field(description="Open timestamp (unix seconds). Defaults to now.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        ts = int(opened_ts) if opened_ts is not None else int(rt.now().timestamp())
        trade_id = await rt.store.journal_insert(
            opened_ts=ts, symbol=symbol, side=side, size=size, entry=entry, stop=stop, target=target, thesis=thesis, tags=tags
        )
        return b.build({"id": trade_id, "opened_ts": ts, "symbol": symbol, "side": side, "size": size, "entry": entry})

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "List journal trades. Filter by `status` (open / closed / cancelled), by `symbol`, by "
            "`tag` (substring match on the stored JSON array), newest first, up to `limit` rows."
        ),
    )
    async def journal_list(
        status: Annotated[Literal["open", "closed", "cancelled"] | None, Field(description="Optional status filter.")] = None,
        symbol: Annotated[str | None, Field(description="Optional symbol filter.")] = None,
        tag: Annotated[str | None, Field(description="Optional single-tag filter.")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        rows = await rt.store.journal_list(status=status, symbol=symbol, tag=tag, limit=limit)
        return b.build({"count": len(rows), "trades": rows})

    @server.tool(
        annotations=READ_ONLY,  # writes locally only
        description=(
            "Close an open journal trade. Pass `exit` (fill price) and optionally `closed_ts`/`note`. "
            "PnL = sign * size * (exit - entry); R = (exit - entry) / (entry - stop) for a long with "
            "stop below entry (symmetric for a short). Status becomes 'closed'; outcome is derived "
            "from PnL. Set `cancelled=true` to mark the trade as cancelled instead (no fill)."
        ),
    )
    async def journal_close(
        id: Annotated[int, Field(ge=1, description="Trade id from journal_add.")],
        exit: Annotated[float | None, Field(gt=0, description="Exit price. Required unless cancelled=true.")] = None,
        closed_ts: Annotated[int | None, Field(description="Close timestamp (unix seconds). Defaults to now.")] = None,
        cancelled: bool = False,
        note: Annotated[str | None, Field(description="Post-mortem note.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        trade = await rt.store.journal_get(id)
        if trade is None:
            b.miss("journal", "store", "data_unavailable", f"trade id={id} not found")
            return b.build(None, ok=False)
        if trade["status"] != "open":
            b.miss("journal", "store", "data_unavailable", f"trade id={id} is already {trade['status']}")
            return b.build(None, ok=False)

        ts = int(closed_ts) if closed_ts is not None else int(rt.now().timestamp())
        if cancelled:
            await rt.store.journal_update_close(
                id,
                closed_ts=ts,
                status="cancelled",
                exit_=None,
                outcome="cancelled",
                pnl=None,
                r_multiple=None,
                note=note,
            )
        else:
            if exit is None:
                b.miss("exit", "tradedesk", "data_unavailable", "exit price is required unless cancelled=true")
                return b.build(None, ok=False)
            p = pnl_fn(trade["side"], float(trade["size"]), float(trade["entry"]), float(exit))
            r_val = r_multiple(trade["side"], float(trade["entry"]), trade.get("stop"), float(exit))
            await rt.store.journal_update_close(
                id,
                closed_ts=ts,
                status="closed",
                exit_=float(exit),
                outcome=outcome_of(p),
                pnl=p,
                r_multiple=r_val,
                note=note,
            )
        return b.build(await rt.store.journal_get(id))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Journal statistics over closed trades (open and cancelled are ignored): win-rate, "
            "expectancy per trade (USD), total PnL (USD), avg win/loss PnL, and R-multiple stats "
            "(avg / median / min / max) when stops were recorded. Filter by `symbol` or `tag`."
        ),
    )
    async def journal_stats(
        symbol: Annotated[str | None, Field(description="Optional symbol filter.")] = None,
        tag: Annotated[str | None, Field(description="Optional tag filter.")] = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 500,
    ) -> dict[str, Any]:
        b = Builder()
        if not _store_guard(b, rt):
            return b.build(None, ok=False)
        await rt.store.init()
        rows = await rt.store.journal_list(status="closed", symbol=symbol, tag=tag, limit=limit)
        s = summary(rows)
        # Round USD / R to friendly precision for the envelope.
        rounded = {
            k: (r(v, 2) if isinstance(v, float) and k.endswith(("_pnl", "pnl")) else r(v, 4) if isinstance(v, float) else v)
            for k, v in s.items()
        }
        return b.build({"filters": {"symbol": symbol, "tag": tag}, **rounded})
