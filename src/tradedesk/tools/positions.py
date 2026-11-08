"""Portfolio positions + risk scenarios.

Positions are manually added rows in the local SQLite store (future work:
sync read-only from an exchange). The risk tool aggregates net greeks and
runs a canonical set of spot/vol/time scenarios on top of them.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.portfolio import aggregate_greeks, scenario_pnl
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import (
    READ_ONLY,
    STORE_ERRORS,
    drop_corrupt_rows,
    finite_json,
    r,
    reject_bad_ts,
    reject_nonfinite,
    reject_nonfinite_result,
    store_failed,
    store_ready,
)

# Numeric columns of a stored position; another writer can leave a string, a blob or inf in any of them.
POSITION_NUMS = ("opened_ts", "closed_ts", "size", "entry", "mark", "delta", "gamma", "vega", "theta")

DEFAULT_SCENARIOS: list[dict[str, float]] = [
    {"d_spot_pct": -0.10, "d_vol_points": 0.0},
    {"d_spot_pct": -0.05, "d_vol_points": 0.0},
    {"d_spot_pct": 0.00, "d_vol_points": 0.05},
    {"d_spot_pct": 0.00, "d_vol_points": -0.05},
    {"d_spot_pct": 0.05, "d_vol_points": 0.0},
    {"d_spot_pct": 0.10, "d_vol_points": 0.0},
    {"d_spot_pct": -0.10, "d_vol_points": 0.05},  # crash-with-vol-up (classic)
]


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,  # writes to local store only
        description=(
            "Add a position to the local portfolio (manual). Per-contract greeks "
            "(delta/gamma/vega/theta) are optional but strongly recommended — without them the "
            "risk tool can't produce useful numbers. `side` = long/short; `size` is always > 0 and "
            "the sign comes from `side`. Greeks must be in a consistent set of units; Deribit's "
            "convention works well (delta in coin per $, vega in USD per 1 vol-point, theta in "
            "USD per day, gamma in coin per $^2)."
        ),
    )
    async def positions_add(
        instrument: Annotated[str, Field(description="e.g. BTC-30OCT26-82000-C or BTC/USDT:USDT.")],
        kind: Annotated[Literal["option", "future", "perp", "spot"], Field(description="Instrument type of the position.")],
        side: Annotated[Literal["long", "short"], Field(description="Direction; sets the sign applied to size and greeks.")],
        size: Annotated[
            float,
            Field(gt=0, description="Number of contracts / coins, always > 0 (sign comes from side); greeks are multiplied by it."),
        ],
        entry: Annotated[float | None, Field(gt=0, description="Entry price in quote currency.")] = None,
        mark: Annotated[float | None, Field(gt=0, description="Latest mark (USD per contract/coin).")] = None,
        delta: Annotated[float | None, Field(description="Per-contract delta.")] = None,
        gamma: Annotated[float | None, Field(description="Per-contract gamma (coin per $^2, Deribit convention).")] = None,
        vega: Annotated[float | None, Field(description="Per-contract vega (USD per 1 vol-point).")] = None,
        theta: Annotated[float | None, Field(description="Per-contract theta (USD per day).")] = None,
        tags: Annotated[list[str] | None, Field(description="Free-form tags for filtering.")] = None,
        note: Annotated[str | None, Field(description="Optional note.")] = None,
        opened_ts: Annotated[int | None, Field(description="Open timestamp (unix seconds). Default: now.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        # "inf" / "1e400" pass the float schema and were stored as inf (Infinity in every later read); opened_ts = 10**20
        # overflowed the SQLite INTEGER binding and raised out of the tool.
        nums = {"size": size, "entry": entry, "mark": mark, "delta": delta, "gamma": gamma, "vega": vega, "theta": theta}
        if (bad := reject_nonfinite(b, **nums) or reject_bad_ts(b, "opened_ts", opened_ts)) is not None:
            return bad
        if not await store_ready(b, rt, "positions"):
            return b.build(None, ok=False)
        try:
            ts = int(opened_ts) if opened_ts is not None else int(rt.now().timestamp())
            pid = await rt.store.position_insert(
                opened_ts=ts,
                instrument=instrument,
                kind=kind,
                side=side,
                size=size,
                entry=entry,
                mark=mark,
                delta=delta,
                gamma=gamma,
                vega=vega,
                theta=theta,
                tags=tags,
                note=note,
            )
            return b.build({"id": pid, "instrument": instrument, "side": side, "size": size, "kind": kind})
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "List tracked positions, newest-first. Filter by status (open / closed), instrument "
            "or tag (substring match on the stored JSON array)."
        ),
    )
    async def positions_list(
        status: Annotated[Literal["open", "closed"] | None, Field(description="Optional status filter.")] = None,
        instrument: Annotated[str | None, Field(description="Optional exact instrument filter.")] = None,
        tag: Annotated[str | None, Field(description="Optional single-tag filter.")] = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Maximum number of positions to return, newest first (1-1000).")] = 200,
    ) -> dict[str, Any]:
        b = Builder()
        if not await store_ready(b, rt, "positions"):
            return b.build(None, ok=False)
        try:
            rows = await rt.store.positions_list(status=status, instrument=instrument, tag=tag, limit=limit)
            rows = drop_corrupt_rows(b, rows, "position", POSITION_NUMS, required=("size",))
            return b.build({"count": len(rows), "positions": rows})
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,  # writes to local store only
        description=(
            "Mark a position closed in the local store. Does NOT place any exchange order — ordering "
            "lives behind the future ExecutionGuard (Phase 4). Pass an optional `note` and "
            "`closed_ts` (defaults to now)."
        ),
    )
    async def positions_close(
        id: Annotated[int, Field(ge=1, description="Position id from positions_add.")],
        closed_ts: Annotated[int | None, Field(description="Close timestamp (unix seconds). Default: now.")] = None,
        note: Annotated[str | None, Field(description="Optional close note.")] = None,
    ) -> dict[str, Any]:
        b = Builder()
        if (bad := reject_bad_ts(b, "closed_ts", closed_ts)) is not None:  # 10**20 raised OverflowError out of the update
            return bad
        if not await store_ready(b, rt, "positions"):
            return b.build(None, ok=False)
        try:
            pos = await rt.store.position_get(id)
            if pos is None:
                b.miss("position", "store", "data_unavailable", f"position id={id} not found")
                return b.build(None, ok=False)
            if pos["status"] != "open":
                b.miss("position", "store", "data_unavailable", f"position id={id} is already {pos['status']}")
                return b.build(None, ok=False)
            ts = int(closed_ts) if closed_ts is not None else int(rt.now().timestamp())
            await rt.store.position_update_close(id, closed_ts=ts, note=note)
            row = await rt.store.position_get(id)
            # Closing computes nothing from the stored numbers, so a corrupt position is still closed (it would otherwise
            # stay open for good); but a blob cell failed the reply after the close was written, and inf came back as
            # Infinity: the stored row is then left out of the reply.
            if row is not None and not (
                finite_json(row) and drop_corrupt_rows(Builder(), [row], "position", POSITION_NUMS, required=("size",))
            ):
                b.warn(
                    f"position id={id} was closed, but its stored row is malformed (a blob, or a non-numeric / non-finite "
                    f"{', '.join(POSITION_NUMS)}), so it is not returned"
                )
                return b.build({"id": id, "status": "closed", "closed_ts": ts})
            return b.build(row)
        except STORE_ERRORS as e:
            return store_failed(b, e)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Net greeks for the OPEN portfolio plus scenario PnL. Scenarios approximate "
            "PnL = Σ sign·size·(δ·dSpot + ½γ·dSpot² + vega·dVol + θ·days). The default scenario "
            "set covers ±5 % / ±10 % spot, ±5 vol-points and the classic crash-plus-vol scenario. "
            "`reference_spot` lets you override the spot used to convert dSpot_pct → dSpot (USD); "
            "when omitted, the first position's `mark` is used. Pass `scenarios` to replace the "
            "defaults with your own list."
        ),
    )
    async def positions_risk(
        instrument_filter: Annotated[str | None, Field(description="Only include positions matching this exact instrument.")] = None,
        tag: Annotated[str | None, Field(description="Only include positions carrying this tag.")] = None,
        reference_spot: Annotated[
            float | None, Field(gt=0, description="Spot to convert dSpot_pct → USD; defaults to the first mark.")
        ] = None,
        scenarios: Annotated[list[dict[str, float]] | None, Field(description="List of {d_spot_pct, d_vol_points, days?}.")] = None,
        days: Annotated[float, Field(ge=0, description="Scenario time (days) if a scenario does not supply its own.")] = 0.0,
    ) -> dict[str, Any]:
        b = Builder()
        # The float schema lets "inf" / 1e400 through; such a scenario or days gave NaN / Infinity PnL.
        if (bad := reject_nonfinite(b, reference_spot=reference_spot, scenarios=scenarios, days=days)) is not None:
            return bad
        if not await store_ready(b, rt, "positions"):
            return b.build(None, ok=False)
        try:
            rows = await rt.store.positions_list(status="open", instrument=instrument_filter, tag=tag, limit=1000)
            # A string / blob greek used to count as 0 silently and inf gave Infinity / NaN totals: such a position is
            # left out with a warning instead.
            stored = len(rows)
            rows = drop_corrupt_rows(b, rows, "open position", POSITION_NUMS, required=("size",))
            agg = aggregate_greeks(rows)
            totals = agg["totals"]
            if totals["count"] == 0:
                why = (
                    "no open positions matched the filter" if stored == len(rows) else f"all {stored} matching open positions are malformed"
                )
                b.miss("positions", "store", "data_unavailable", why)
                return b.build(None, ok=False)
            scen_list = scenarios or DEFAULT_SCENARIOS
            scen_out: list[dict[str, Any]] = []
            for s in scen_list:
                d_spot_pct = float(s.get("d_spot_pct", 0.0))
                d_vol_points = float(s.get("d_vol_points", 0.0))
                scen_days = float(s.get("days", days))
                res = scenario_pnl(
                    rows,
                    d_spot_pct=d_spot_pct,
                    d_vol_points=d_vol_points,
                    reference_spot=reference_spot,
                    days=scen_days,
                )
                scen_out.append(
                    {
                        "d_spot_pct": d_spot_pct,
                        "d_vol_points": d_vol_points,
                        "days": scen_days,
                        "pnl_usd": r(res["total_pnl"], 2),
                    }
                )
            nums = {
                "net_greeks": {
                    "count": totals["count"],
                    "delta": r(totals["delta"], 6),
                    "gamma": r(totals["gamma"], 8),
                    "vega": r(totals["vega"], 4),
                    "theta": r(totals["theta"], 4),
                    "notional_usd": r(totals["notional_usd"], 2),
                },
                "per_instrument": {k: {gk: r(gv, 6) for gk, gv in v.items()} for k, v in agg["per_instrument"].items()},
                "scenarios": scen_out,
            }
            # Finite but extreme stored greeks or scenario inputs can still overflow a sum or a product.
            if (bad := reject_nonfinite_result(b, nums)) is not None:
                return bad
            return b.build({"filters": {"instrument": instrument_filter, "tag": tag}, **nums, "reference_spot": reference_spot})
        except STORE_ERRORS as e:
            return store_failed(b, e)
