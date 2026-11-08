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
from ._util import (
    READ_ONLY,
    STORE_ERRORS,
    drop_corrupt_rows,
    finite_json,
    r,
    reject_nonfinite,
    reject_nonfinite_result,
    store_failed,
    store_ready,
)
from ._util import reject_bad_ts as _bad_ts

# Numeric columns of a stored trade; another writer can leave a string, a blob or inf in any of them.
JOURNAL_NUMS = ("opened_ts", "closed_ts", "size", "entry", "stop", "target", "exit", "pnl", "r_multiple")


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
        side: Annotated[Literal["long", "short"], Field(description="Trade direction; sets the sign of PnL on close.")],
        size: Annotated[
            float, Field(gt=0, description="Position size in native units (contracts / coins), not USD; PnL = size * price move.")
        ],
        entry: Annotated[float, Field(gt=0, description="Entry price in quote currency.")],
        stop: Annotated[float | None, Field(gt=0, description="Stop price; without it r_multiple on close is None.")] = None,
        target: Annotated[float | None, Field(gt=0, description="Target price.")] = None,
        thesis: Annotated[str | None, Field(description="Short note about why this trade.")] = None,
        tags: Annotated[list[str] | None, Field(description="Free-form tags; filter with journal_list.")] = None,
        opened_ts: Annotated[int | None, Field(description="Open timestamp (unix seconds). Defaults to now.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        # "inf" / "1e400" pass the gt=0 schema and were stored as inf (Infinity in the reply and in every later read).
        bad = reject_nonfinite(b, size=size, entry=entry, stop=stop, target=target) or _bad_ts(b, "opened_ts", opened_ts)
        if bad is not None:
            return bad
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            ts = int(opened_ts) if opened_ts is not None else int(rt.now().timestamp())
            trade_id = await rt.store.journal_insert(
                opened_ts=ts, symbol=symbol, side=side, size=size, entry=entry, stop=stop, target=target, thesis=thesis, tags=tags
            )
            return b.build({"id": trade_id, "opened_ts": ts, "symbol": symbol, "side": side, "size": size, "entry": entry})
        except STORE_ERRORS as e:
            return store_failed(b, e)

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
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum number of trades to return, latest opened first (1-500).")] = 100,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            rows = await rt.store.journal_list(status=status, symbol=symbol, tag=tag, limit=limit)
            # An invalid UTF-8 blob in any cell failed the whole reply, and inf / NaN (a numeric cell, or `tags` text such
            # as "[NaN]") came back as Infinity / NaN: such a trade is skipped with a warning (another writer).
            stored = len(rows)
            rows = drop_corrupt_rows(b, rows, "trade", JOURNAL_NUMS, required=("size", "entry"))
            nonfinite = [t.get("id") for t in rows if not finite_json(t)]
            if nonfinite:
                b.warn(f"{len(nonfinite)} stored trade rows skipped: NaN / Infinity in tags; id {nonfinite[:10]}")
                rows = [t for t in rows if finite_json(t)]
            if stored and not rows:  # all malformed must not read as "no trades"
                b.miss("journal", "store", "data_unavailable", f"all {stored} matching stored trades are malformed")
                return b.build({"count": 0, "trades": []}, ok=False)
            return b.build({"count": len(rows), "trades": rows})
        except STORE_ERRORS as e:
            return store_failed(b, e)

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
        cancelled: Annotated[
            bool, Field(description="Mark the trade as cancelled (never filled): no exit, PnL or R; `exit` is ignored.")
        ] = False,
        note: Annotated[str | None, Field(description="Post-mortem note.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        bad = (None if cancelled else reject_nonfinite(b, exit=exit)) or _bad_ts(b, "closed_ts", closed_ts)
        if bad is not None:
            return bad
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            trade = await rt.store.journal_get(id)
            if trade is None:
                b.miss("journal", "store", "data_unavailable", f"trade id={id} not found")
                return b.build(None, ok=False)
            if trade["status"] != "open":
                b.miss("journal", "store", "data_unavailable", f"trade id={id} is already {trade['status']}")
                return b.build(None, ok=False)
            # A string / blob / inf in size, entry or stop (another writer) raised in float() or the PnL, and a blob in any
            # cell failed the reply after the close was already written: such a trade is left untouched.
            if not drop_corrupt_rows(b, [trade], "trade", ("size", "entry", "stop"), required=("size", "entry")):
                b.miss("journal", "store", "data_unavailable", f"trade id={id} is malformed; it was not closed")
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
                # Finite but extreme prices / sizes (1e308) overflow the PnL: not written, so journal_stats is not poisoned.
                bad = reject_nonfinite_result(b, {"pnl": p, "r_multiple": r_val})
                if bad is not None:
                    return bad
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
        except STORE_ERRORS as e:
            return store_failed(b, e)

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
        limit: Annotated[int, Field(ge=1, le=5000, description="Use at most this many closed trades, latest opened first (1-5000).")] = 500,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "the journal"):
            return b.build(None, ok=False)
        try:
            rows = await rt.store.journal_list(status="closed", symbol=symbol, tag=tag, limit=limit)
            # A string / blob in pnl or r_multiple raised ValueError in summary(), inf gave Infinity stats: such a trade is
            # skipped with a warning (another writer, or a pnl that overflowed at close).
            stored = len(rows)
            rows = drop_corrupt_rows(b, rows, "closed trade", ("pnl", "r_multiple"))
            if stored and not rows:  # all malformed must not read as "no closed trades"
                b.miss("journal", "store", "data_unavailable", f"all {stored} matching closed trades are malformed")
                return b.build(None, ok=False)
            s = summary(rows)
            # Round USD / R to friendly precision for the envelope.
            rounded = {
                k: (r(v, 2) if isinstance(v, float) and k.endswith(("_pnl", "pnl")) else r(v, 4) if isinstance(v, float) else v)
                for k, v in s.items()
            }
            # Finite stored values near the float limit (1e308) overflow the sums: ok=false instead of Infinity / NaN.
            bad = reject_nonfinite_result(b, rounded)
            if bad is not None:
                return bad
            return b.build({"filters": {"symbol": symbol, "tag": tag}, **rounded})
        except STORE_ERRORS as e:
            return store_failed(b, e)
