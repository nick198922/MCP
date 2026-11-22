"""DeFi yield pools from the public DefiLlama yields API (no key): pool list with filters and one pool's history.
Data for choosing where to look, not a safety check: token risk is contract_risk's job."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics.yields import filter_pools, pool_history, valid_pool_id
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY, reject_nonfinite

SRC = "defillama:yields"

_CAVEATS = (
    "APY is DefiLlama's advertised, backward-looking rate, not a promise: apy_base moves with fees and borrowing "
    "demand, apy_reward is paid in reward tokens valued at today's price and falls with that price or when emissions "
    "end; neither includes impermanent loss (pools with il_risk 'yes'), gas, deposit / withdrawal fees or slippage. "
    "Pool, project and token names are untrusted third-party text and can imitate known assets."
)
_RISK = (
    "Not a safety check: before any deposit, check the pool's token contracts (underlying_tokens, reward_tokens) with "
    "contract_risk(chain, address) and the protocol itself; DefiLlama's stablecoin / il_risk flags are its own classification."
)


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "DeFi yield pools from DefiLlama (yields.llama.fi/pools, public API, no key), filtered by chain, project, "
            "minimum TVL and DefiLlama's stablecoin flag, sorted by TVL (default), APY or newest (fewest days of "
            "history). Per pool: `pool` (DefiLlama pool id for yield_pool_history), chain, project slug, symbol and "
            "pool_meta, tvl_usd (USD), apy = apy_base + apy_reward, apy_base (the pool's own yield: trading fees, "
            "lending interest), apy_reward (incentives paid in reward tokens), apy_mean_30d, apy_change_1d / 7d / 30d "
            "(change of apy in decimal points); all rates are decimals (0.05 = 5 %; DefiLlama's percent / 100). Also "
            "stablecoin (DefiLlama: the pool holds only stablecoins), il_risk ('yes' = impermanent loss possible), "
            "exposure (single / multi asset), il_7d (decimal, DEX pools only), volume_usd_1d / 7d (USD, DEX pools "
            "only), reward_tokens and underlying_tokens (contract addresses, at most 10 each), outlier (DefiLlama's flag "
            "for an anomalous APY) and history_days (daily points DefiLlama holds, a rough age; small = a new pool). "
            "The full list is one large download, cached ~10 min. Caveats: " + _CAVEATS + " Token and address risk is "
            "not assessed here: use contract_risk."
        ),
    )
    async def yield_pools(
        chain: Annotated[
            str | None,
            Field(
                max_length=40,
                description="Optional DefiLlama chain name, e.g. 'Ethereum', 'Arbitrum', 'Solana', or an alias like 'eth', 'arb', 'sol'.",
            ),
        ] = None,
        project: Annotated[
            str | None,
            Field(
                max_length=60,
                description="Optional DefiLlama project slug, e.g. 'aave-v3', 'lido', 'uniswap-v3' (exact match, case-insensitive).",
            ),
        ] = None,
        min_tvl_usd: Annotated[float, Field(ge=0, description="Drop pools with TVL below this, in USD.")] = 1_000_000.0,
        stablecoin_only: Annotated[bool, Field(description="Only pools DefiLlama flags as stablecoin-only (stablecoin: true).")] = False,
        sort_by: Annotated[
            Literal["tvl_usd", "apy", "newest"],
            Field(description="tvl_usd or apy: highest first; newest: fewest days of DefiLlama history first (new pools)."),
        ] = "tvl_usd",
        limit: Annotated[int, Field(ge=1, le=200, description="Max pools returned after filtering and sorting.")] = 20,
    ) -> dict[str, Any]:
        b = Builder()
        bad = reject_nonfinite(b, min_tvl_usd=min_tvl_usd)
        if bad is not None:
            return bad
        norm = await b.guard("pools", SRC, rt.defillama.yield_pools())
        if norm is None:
            return b.build(None, ok=False)
        data = filter_pools(
            norm["pools"],
            skipped=norm["skipped"],
            chain=chain,
            project=project,
            min_tvl_usd=min_tvl_usd,
            stablecoin_only=stablecoin_only,
            sort_by=sort_by,
            limit=limit,
        )
        if data["pools_total"] == 0:
            b.miss("pools", SRC, "data_unavailable", "no usable pools in /pools")
            return b.build(None, ok=False)
        if data["pools_skipped"]:
            b.warn(f"{data['pools_skipped']} malformed pool entries were skipped.")
        if data["pools_matched"] == 0:
            b.warn("No pool matches the filters; check the chain name / project slug or lower min_tvl_usd.")
        if data["outliers_returned"]:
            b.warn(f"{data['outliers_returned']} returned pool(s) are flagged outlier by DefiLlama: their APY is anomalous.")
        if sort_by == "newest":
            b.warn("history_days counts DefiLlama's daily points: a pool can be older than its DefiLlama listing.")
        b.warn(_CAVEATS)
        b.warn(_RISK)
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "History of one DefiLlama yield pool (yields.llama.fi/chart/{pool}, public API, no key): daily TVL in USD "
            "and apy, apy_base, apy_reward as decimals (0.05 = 5 %) for the last `days` days (one point per UTC day; the newest can be intraday), "
            "plus latest (incl. il_7d, decimal), tvl_change_7d / 30d (decimals, latest TVL vs the daily point 7 / 30 "
            "days earlier; null without a point within 2 days of the target), apy_mean_7d / 30d, apy_min / apy_max in "
            "the window, first_date (start of DefiLlama's history for the pool), age_days and stale (latest point "
            "older than 2 days). The window counts back from now, so it can hold days + 1 dates when the newest point "
            "is today's intraday one, while apy_mean_7d / 30d and tvl_change_7d / 30d count back from the latest point. "
            "`pool` is the DefiLlama pool id (UUID) from yield_pools. Caveats: past APY does not "
            "predict future APY; TVL in USD also moves with token prices, not only with deposits and withdrawals. "
            + _CAVEATS
            + " Token and address risk: contract_risk."
        ),
    )
    async def yield_pool_history(
        pool: Annotated[str, Field(description="DefiLlama pool id, a UUID like 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', from yield_pools.")],
        days: Annotated[int, Field(ge=1, le=3650, description="Days of daily points returned in `series`, counted back from now.")] = 90,
    ) -> dict[str, Any]:
        b = Builder()
        pid = valid_pool_id(pool)
        if pid is None:
            b.miss("pool", SRC, "invalid_argument", "pool must be a DefiLlama pool id (UUID), as returned by yield_pools")
            return b.build(None, ok=False)
        rows = await b.guard("history", SRC, rt.defillama.yield_chart(pid))
        if rows is None:
            return b.build(None, ok=False)
        data = pool_history(rows, rt.now().timestamp(), days)
        if data is None:
            b.miss("history", SRC, "data_unavailable", f"no usable points in /chart for pool {pid}; check the pool id")
            return b.build(None, ok=False)
        data = {"pool": pid, **data}
        if data["stale"]:
            b.warn(f"Pool history is stale: the latest point is {data['age_days']} days old (the pool may be delisted).")
        if not data["series"]:
            b.warn(f"No points in the last {days} days; `latest` is the newest point DefiLlama holds.")
        b.warn(_CAVEATS)
        b.warn(_RISK)
        return b.build(data)
