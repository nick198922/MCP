"""dex_pairs (DexScreener) and chain_metrics (DefiLlama): real samples from 2026-10-17 behind MockTransport."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp import Client

from tradedesk.analytics.chain_metrics import overview_summary, resolve_chain, tvl_summary
from tradedesk.analytics.dex import likely_fake, summarise_pairs, valid_addresses
from tradedesk.config import Settings
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = Path(__file__).parent / "fixtures"
DEX = json.loads((FIX / "dexscreener_tokens_fartcoin_2026-10-17.json").read_text())
LLAMA = json.loads((FIX / "defillama_solana_2026-10-17.json").read_text())
FART = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

FAKE_SOL = {
    "chainId": "solana",
    "dexId": "raydium",
    "pairAddress": "392Yvfy6iJB5GHkZEFoF5bYtzXZffKVFz4fdmi26vVkw",
    "baseToken": {"address": "5Sqq3NWD98JkcirdDBKRxZFsoZ8f1qqpFp6DSZkoTsHd", "name": "Solana", "symbol": "SOL"},
    "quoteToken": {"address": "So11111111111111111111111111111111111111112", "name": "Wrapped SOL", "symbol": "SOL"},
    "priceUsd": "110.21",
    "volume": {"h24": 347776.17},
    "liquidity": {"usd": 2314314671.08},
    "fdv": 2314591244,
}


async def call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


def make_server(dex_handler=None, llama_handler=None):
    def default_dex(req):
        body = copy.deepcopy(DEX)
        body["pairs"] = [FAKE_SOL, *body["pairs"]]
        return httpx.Response(200, json=body)

    def default_llama(req):
        p = req.url.path
        if p.startswith("/v2/historicalChainTvl/"):
            return httpx.Response(200, json=LLAMA["tvl"])
        if p.startswith("/overview/dexs/"):
            return httpx.Response(200, json=LLAMA["dexs"])
        if p.startswith("/overview/fees/"):
            return httpx.Response(200, json=LLAMA["fees"])
        return httpx.Response(404, text="not mocked")

    tr = {
        "dexscreener": httpx.MockTransport(dex_handler or default_dex),
        "defillama": httpx.MockTransport(llama_handler or default_llama),
    }
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr)
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


async def test_dex_pairs_happy():
    env = await call(make_server(), "dex_pairs", addresses=[FART], limit=5)
    assert env["ok"] is True
    d = env["data"]
    assert d["pairs_total"] == 9 and d["pairs_returned"] == 5
    liqs = [p["liquidity_usd"] for p in d["pairs"]]
    assert liqs == sorted(liqs, reverse=True)
    assert d["pairs"][0]["base_token"]["symbol"] == "SOL" and d["pairs"][0]["likely_fake"] is True
    assert d["likely_fake_count"] == 1
    real = d["pairs"][1]
    assert real["base_token"]["symbol"] == "Fartcoin"  # trailing space trimmed
    assert isinstance(real["price_usd"], float) and isinstance(real["liquidity_usd"], float)
    assert real["likely_fake"] is False and real["age_days"] > 300
    assert abs(real["price_change_24h"]) < 1  # decimal, not percent
    assert any("fake" in w for w in env["warnings"])
    assert env["evidence"][0]["provider"] == "dexscreener"


async def test_dex_pairs_chain_filter_and_empty():
    srv = make_server()
    env = await call(srv, "dex_pairs", addresses=[FART], chain="ethereum")
    assert env["ok"] is False and env["missing"][0]["reason"] == "data_unavailable"
    env = await call(srv, "dex_pairs", addresses=[FART], chain="Solana")
    assert env["ok"] is True and all(p["chain"] == "solana" for p in env["data"]["pairs"])


async def test_dex_pairs_invalid_address():
    env = await call(make_server(), "dex_pairs", addresses=["bad address/../x"])
    assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_argument"


async def test_dex_pairs_provider_failure():
    srv = make_server(dex_handler=lambda req: httpx.Response(503, text="down"))
    env = await call(srv, "dex_pairs", addresses=[FART])
    assert env["ok"] is False
    assert any(m["provider"] == "dexscreener" for m in env["missing"])


async def test_chain_metrics_happy():
    env = await call(make_server(), "chain_metrics", chain="sol")
    assert env["ok"] is True
    d = env["data"]
    assert d["chain"] == "Solana"
    assert d["tvl"]["usd"] == pytest.approx(5903214861, rel=1e-9)
    assert d["dex_volume_24h"]["usd_24h"] == pytest.approx(2168129752.12)
    # Exact values recomputed by hand from the saved payload (fixture 2026-10-17).
    assert d["tvl"]["change_7d"] == pytest.approx(-0.041617, abs=1e-6)
    assert d["tvl"]["change_30d"] == pytest.approx(0.025422, abs=1e-6)
    dv, fe = d["dex_volume_24h"], d["fees_24h"]
    assert dv["change_1d"] == pytest.approx(2168129752.12 / 2799858606.03 - 1, abs=1e-6)
    assert dv["change_7d"] == pytest.approx(0.096043, abs=1e-6)
    assert dv["change_30d"] == pytest.approx(-0.225829, abs=1e-6)
    assert dv["week_over_week"] == pytest.approx(0.012476, abs=1e-6)
    assert dv["usd_7d_sum"] == pytest.approx(14378281859.72)
    assert fe["change_1d"] == pytest.approx(12431928.33 / 12784889.89 - 1, abs=1e-6)
    assert fe["change_7d"] == pytest.approx(-0.106792, abs=1e-6)
    assert fe["week_over_week"] == pytest.approx(84185669.18 / 107734276.19 - 1, abs=1e-6)
    assert fe["usd_7d_sum"] == pytest.approx(84185669.18)
    assert {e["provider"] for e in env["evidence"]} == {"defillama"} and len(env["evidence"]) == 3


async def test_chain_metrics_partial_and_total_failure():
    def partial(req):
        if req.url.path.startswith("/overview/fees/"):
            return httpx.Response(500, text="boom")
        return make_server_default(req)

    def make_server_default(req):
        p = req.url.path
        if p.startswith("/v2/historicalChainTvl/"):
            return httpx.Response(200, json=LLAMA["tvl"])
        return httpx.Response(200, json=LLAMA["dexs"])

    env = await call(make_server(llama_handler=partial), "chain_metrics", chain="Solana")
    assert env["ok"] is True and env["data"]["fees_24h"] is None and env["data"]["tvl"] is not None
    assert any(m["field"] == "fees_24h" for m in env["missing"])

    env = await call(make_server(llama_handler=lambda req: httpx.Response(503, text="down")), "chain_metrics", chain="Solana")
    assert env["ok"] is False and len(env["missing"]) == 3


async def test_chain_metrics_invalid_chain():
    env = await call(make_server(), "chain_metrics", chain="../etc")
    assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_argument"


WSOL = "So11111111111111111111111111111111111111112"
USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def test_canonical_tokens_never_flagged():
    # Real wSOL / USDC with thin turnover: not fake. Same symbol at an unknown address: fake.
    assert likely_fake("SOL", 2.3e9, 3.5e5, chain="solana", address=WSOL)[0] is False
    assert likely_fake("SOL", 2.3e9, 3.5e5, chain="solana", address="5Sqq3NWD98JkcirdDBKRxZFsoZ8f1qqpFp6DSZkoTsHd")[0] is True
    assert likely_fake("USDC", 3e8, 2e6, chain="ethereum", address=USDC_ETH)[0] is False
    assert likely_fake("USDC", 3e8, 2e6, chain="ethereum", address=USDC_ETH.lower())[0] is False
    assert likely_fake("USDC", 3e8, 2e6, chain="ethereum", address="0x" + "ab" * 20)[0] is True
    assert likely_fake("USDC", 3e8, 2e6, chain="base", address=USDC_ETH)[0] is True  # right address, wrong chain
    # case-sensitive on Solana: a case-altered base58 string is a different token
    assert likely_fake("SOL", 2.3e9, 3.5e5, chain="solana", address=WSOL.lower())[0] is True


def _pair(base_sym, base_addr, quote_sym, quote_addr, liq=2.3e9, vol=3.5e5):
    return {
        "chainId": "solana",
        "baseToken": {"address": base_addr, "symbol": base_sym},
        "quoteToken": {"address": quote_addr, "symbol": quote_sym},
        "liquidity": {"usd": liq},
        "volume": {"h24": vol},
    }


def test_fake_checked_on_quote_side_and_real_pool_not_flagged():
    fake_addr = "5Sqq3NWD98JkcirdDBKRxZFsoZ8f1qqpFp6DSZkoTsHd"
    out = summarise_pairs(
        [
            _pair("SOL", WSOL, "USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),  # real, thin turnover
            _pair("FOO", "FooAddr1", "SOL", fake_addr, liq=5e6, vol=1e3),  # fake SOL as quote
        ]
    )
    real, fake = [p for p in out["pairs"] if p["base_token"]["symbol"] == "SOL"][0], out["pairs"][1]
    assert real["likely_fake"] is False
    assert fake["likely_fake"] is True and fake["likely_fake_reason"].startswith("quote token:")


def test_clean_text_strips_invisible_unicode():
    from tradedesk.analytics.dex import clean_text

    assert clean_text("S​O‮L﻿") == "SOL"
    assert likely_fake(clean_text("S​OL"), 2.3e9, 1.0, chain="solana", address="x")[0] is True


def test_dot_segments_rejected():
    assert valid_addresses([".."])[1] is not None
    assert valid_addresses(["a.b"])[1] is not None


def test_tvl_gap_and_staleness():
    day = 86400
    t0 = 1_700_000_000
    # 15-day hole covering the target (day 23): nearest earlier point is 14 days away -> change_7d is None
    rows = [{"date": t0 + i * day, "tvl": 100.0 + i} for i in range(0, 10)]
    rows += [{"date": t0 + i * day, "tvl": 100.0 + i} for i in range(25, 31)]
    s = tvl_summary(rows)
    assert s["change_7d"] is None
    # a reference exactly 7 days earlier is used; one 1.5 days older than target is still accepted
    full = [{"date": t0 + i * day, "tvl": 100.0 + i} for i in range(0, 31)]
    assert tvl_summary(full)["change_7d"] == pytest.approx(130.0 / 123.0 - 1, abs=1e-6)
    near = [r for r in full if r["date"] != t0 + 23 * day]  # drop the exact day; day 22 is 1 day older
    assert tvl_summary(near)["change_7d"] == pytest.approx(130.0 / 122.0 - 1, abs=1e-6)
    # staleness
    last = t0 + 30 * day
    assert tvl_summary(full, last + 3600)["stale"] is False
    st = tvl_summary(full, last + 5 * day)
    assert st["stale"] is True and st["age_days"] == 5.0


def test_likely_fake_rules():
    assert likely_fake("SOL", 2.3e9, 3.5e5)[0] is True
    assert likely_fake("sol ", 2.3e9, None)[0] is True
    assert likely_fake("SOL", 5e7, 1.2e8)[0] is False  # genuine turnover
    assert likely_fake("SOL", 5e5, 10.0)[0] is False  # below liquidity floor
    assert likely_fake("PEPE", 5e9, 1.0)[0] is False  # not a major ticker
    assert likely_fake("SOL", None, None)[0] is False


def test_summarise_pairs_robust_to_junk():
    out = summarise_pairs([None, 5, {"baseToken": "x", "liquidity": {"usd": "abc"}}, {"liquidity": {"usd": "10"}}])
    assert out["pairs_total"] == 2 and out["pairs"][0]["liquidity_usd"] == 10.0
    assert valid_addresses([" a ", "a", "b"]) == (["a", "b"], None)
    assert valid_addresses([])[1] is not None
    assert valid_addresses([str(i) for i in range(31)])[1] is not None


def test_chain_summaries_edge_cases():
    assert tvl_summary([]) is None
    assert overview_summary({}) is None
    assert resolve_chain("ETH") == "Ethereum" and resolve_chain("Mantle") == "Mantle"
    only = tvl_summary([{"date": 1000, "tvl": 5}])
    assert only["usd"] == 5 and only["change_7d"] is None
