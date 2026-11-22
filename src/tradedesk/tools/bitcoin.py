"""Bitcoin network tools on the public mempool.space API (no key): fees, mempool, latest block, difficulty / hashrate,
address overview. Network context, not trading signals."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..analytics import bitcoin as btc
from ..core.envelope import Builder
from ..runtime import Runtime
from ._util import READ_ONLY

SRC = "mempool"


def register(server: MCPServer, rt: Runtime) -> None:
    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Bitcoin fee-rate estimates from mempool.space (public API, no key), in sat/vB (satoshis per virtual byte; "
            "1 BTC = 100,000,000 sats): fastest (next block), half_hour (about 3 blocks), hour (about 6 blocks), "
            "economy and minimum. Fee for a transaction = rate x its vsize (a simple 1-input 2-output SegWit payment "
            "is about 140 vB). Caveats: mempool.space's projection of its own node's mempool, not a confirmation "
            "guarantee, and it moves within minutes; the recommended rates are whole sat/vB numbers while the mempool "
            "and recent blocks also hold transactions paying under 1 sat/vB (see btc_mempool). Cached ~30 s. "
            "Network context, not a trading signal."
        ),
    )
    async def btc_fees() -> dict[str, Any]:
        b = Builder()
        body = await b.guard("fees", SRC, rt.mempool.fees_recommended())
        if body is None:
            return b.build(None, ok=False)
        data = btc.fees_summary(body)
        if data is None:
            b.miss("fees", SRC, "data_unavailable", "no usable fee rates in /api/v1/fees/recommended")
            return b.build(None, ok=False)
        b.warn("Fee estimates, not a confirmation guarantee; they can change within minutes.")
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "State of the Bitcoin mempool (unconfirmed transactions) from mempool.space: tx_count, vsize_vb (virtual "
            "bytes), blocks_of_backlog = vsize / 1,000,000 vB (one block holds at most ~1,000,000 vB = 4M weight "
            "units), total_fee_sats / total_fee_btc, avg_fee_rate_sat_vb (total fee / vsize) and fee_bands: vsize and "
            "share of the mempool paying >= 50, 20-50, 10-20, 5-10, 2-5, 1-2 and under 1 sat/vB (from mempool.space's "
            "fee histogram). Caveats: fee_bands are approximate, because each histogram bin is counted in the band of "
            "its lower fee-rate edge (a bin spanning 1.95-2.3 sat/vB lands wholly in 1-2); one node's view (other nodes' mempools differ by policy and size limits); "
            "blocks_of_backlog is not a clearing time, because new transactions keep arriving and the low-fee tail can "
            "be evicted instead of mined. Cached ~30 s. Network context, not a trading signal."
        ),
    )
    async def btc_mempool() -> dict[str, Any]:
        b = Builder()
        body = await b.guard("mempool", SRC, rt.mempool.mempool())
        if body is None:
            return b.build(None, ok=False)
        data = btc.mempool_summary(body)
        if data is None:
            b.miss("mempool", SRC, "data_unavailable", "no usable count / vsize in /api/mempool")
            return b.build(None, ok=False)
        if data["histogram_vsize_vb"] == 0:
            b.warn("The fee histogram was empty or unusable: fee_bands are zero.")
        b.warn("Snapshot of mempool.space's node; blocks_of_backlog is a size, not a time to clear.")
        b.warn("fee_bands are approximate: each histogram bin is counted in the band of its lower fee-rate edge.")
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Latest Bitcoin block from mempool.space: height, hash, time (UTC; set by the miner and only loosely "
            "constrained, up to 2 hours ahead of network time, so age_seconds = now - block time is approximate and can "
            "even be negative), tip_height (chain tip, a separate request), "
            "tx_count, size_bytes, weight_wu, vsize_vb and fullness (vsize / 1,000,000 vB), difficulty, fee rates "
            "paid in the block in sat/vB (median, min, max), total fees and reward (subsidy + fees) in sats and BTC, "
            "subsidy_btc, and the mining pool as identified by mempool.space (untrusted third-party text, may be "
            "null or wrong). Cached ~15 s; a new block arrives about every 10 minutes on average, with wide variation."
        ),
    )
    async def btc_latest_block() -> dict[str, Any]:
        b = Builder()
        blocks, tip_height = await asyncio.gather(
            b.guard("latest_block", SRC, rt.mempool.blocks()),
            b.guard("tip_height", SRC, rt.mempool.tip_height()),
        )
        if blocks is None:
            return b.build(None, ok=False)
        data = btc.block_summary(blocks[0], tip_height, rt.now().timestamp())
        if data is None:
            b.miss("latest_block", SRC, "data_unavailable", "the newest block in /api/v1/blocks has no usable height / time")
            return b.build(None, ok=False)
        if tip_height is None:
            b.warn("Chain tip height unavailable; the block is the newest one mempool.space listed.")
        elif tip_height > data["height"]:
            b.warn(f"The block list lags the chain tip by {tip_height - data['height']} block(s); a newer block exists.")
        b.warn(
            "Block time is set by the miner (only loosely constrained), so age_seconds is approximate; the pool name is mempool.space's guess."
        )
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Bitcoin mining difficulty and hashrate from mempool.space (/api/v1/mining/hashrate/3d): current_difficulty "
            "(unitless; current_difficulty_t = in trillions), current_hashrate_ehs in EH/s (1 EH/s = 1e18 hashes per "
            "second) and the daily average hashrate for the last ~3 days (date UTC, EH/s). Caveats: hashrate is not "
            "measured, it is estimated from block times and difficulty, so it is noisy over days; difficulty retargets "
            "every 2016 blocks (~2 weeks). Cached ~10 min. Context for miners and network security, not a trading signal."
        ),
    )
    async def btc_difficulty_hashrate() -> dict[str, Any]:
        b = Builder()
        body = await b.guard("hashrate", SRC, rt.mempool.hashrate_3d())
        if body is None:
            return b.build(None, ok=False)
        data = btc.hashrate_summary(body)
        if data is None:
            b.miss("hashrate", SRC, "data_unavailable", "no usable hashrate / difficulty in /api/v1/mining/hashrate/3d")
            return b.build(None, ok=False)
        b.warn("Hashrate is an estimate from block times and difficulty, noisy over short windows.")
        return b.build(data)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Overview of one Bitcoin mainnet address from mempool.space: confirmed balance (sats and BTC; 1 BTC = "
            "100,000,000 sats), balance including unconfirmed mempool transactions, and for the confirmed chain and the "
            "mempool separately: funded / spent sums (sats), net, funded / spent output counts and tx_count. Accepts "
            "legacy (1...), P2SH (3...), SegWit bech32 (bc1q...) and Taproot (bc1p...) addresses; the checksum is "
            "verified before any request (testnet not supported). Caveats: one address is not a wallet or an entity "
            "(wallets use many addresses, change goes to new ones); no owner labels (exchange, miner, fund), so a "
            "balance or a move says nothing about who holds it. Cached ~60 s."
        ),
    )
    async def btc_address(
        address: Annotated[
            str, Field(description="Bitcoin mainnet address: 1... (P2PKH), 3... (P2SH), bc1q... (SegWit) or bc1p... (Taproot).")
        ],
    ) -> dict[str, Any]:
        b = Builder()
        addr, err = btc.valid_btc_address(address)
        if addr is None:
            b.miss("address", SRC, "invalid_argument", err or "invalid address")
            return b.build(None, ok=False)
        body = await b.guard("address", SRC, rt.mempool.address(addr))
        if body is None:
            return b.build(None, ok=False)
        data = btc.address_summary(body)
        if data is None:
            b.miss("address", SRC, "data_unavailable", "no usable chain_stats in /api/address")
            return b.build(None, ok=False)
        data = {"address": addr, **data}
        b.warn("One address is not a wallet or an entity; the owner is unknown (no labels).")
        return b.build(data)
