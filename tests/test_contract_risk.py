"""contract_risk: GoPlus token_security (live samples 2026-10-23) and DexScreener behind MockTransport."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from mcp import Client

from tradedesk.analytics import contract_risk as cr
from tradedesk.config import Settings
from tradedesk.core import errors as cr_errors
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = Path(__file__).parent / "fixtures"
SHIB_BODY = json.loads((FIX / "goplus_token_security_shib_2026-10-23.json").read_text())["response"]
EMPTY_BODY = json.loads((FIX / "goplus_token_security_empty_2026-10-23.json").read_text())["response"]
SHIB = "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce"
SHIB_TOKEN = SHIB_BODY["result"][SHIB]
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
ZERO = "0x0000000000000000000000000000000000000000"

# Synthetic DexScreener pairs in the documented shape (the real sample in test_onchain is a Solana token).
DEX_PAIRS = {
    "pairs": [
        {
            "chainId": "ethereum",
            "dexId": "uniswap",
            "pairAddress": "0x811beed0119b4afce20d2583eb608c6f7af1954f",
            "baseToken": {"address": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE", "name": "SHIBA INU", "symbol": "SHIB"},
            "quoteToken": {"address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "name": "Wrapped Ether", "symbol": "WETH"},
            "liquidity": {"usd": 3_000_000.0},
            "marketCap": 5_000_000_000,
        },
        {
            "chainId": "ethereum",
            "dexId": "uniswap",
            "pairAddress": "0x2f62f2b4c5fcd7570a709dec05d68ea19c82a9ec",
            "baseToken": {"address": SHIB, "name": "SHIBA INU", "symbol": "SHIB"},
            "quoteToken": {"address": "0xdac17f958d2ee523a2206206994597c13d831ec7", "name": "Tether", "symbol": "USDT"},
            "liquidity": {"usd": 1_000_000.0},
            "marketCap": 5_000_000_000,
        },
        {  # another chain: ignored
            "chainId": "bsc",
            "baseToken": {"address": SHIB, "symbol": "SHIB"},
            "quoteToken": {"address": "0x55d398326f99059ff775485246999027b3197955", "symbol": "USDT"},
            "liquidity": {"usd": 9_000_000.0},
            "marketCap": 5_000_000_000,
        },
    ]
}


YEAR_LOCK = [{"amount": "1.0", "end_time": "2027-10-23T00:00:00+00:00", "opt_time": "2026-01-01T00:00:00+00:00"}]
V2_POOL = "0x" + "7" * 40


def assess(token, **kw):
    return cr.assess(token, now=NOW, **kw)


def clean_token(**over):
    """Every check passes: renounced, LP locked for a year in the only (V2) pool, spread holders, zero taxes."""
    t = {
        "token_name": "Clean",
        "token_symbol": "CLN",
        "is_honeypot": "0",
        "cannot_sell_all": "0",
        "cannot_buy": "0",
        "selfdestruct": "0",
        "external_call": "0",
        "trading_cooldown": "0",
        "anti_whale_modifiable": "0",
        "honeypot_with_same_creator": "0",
        "buy_tax": "0",
        "sell_tax": "0.01",
        "transfer_tax": "0",
        "slippage_modifiable": "0",
        "personal_slippage_modifiable": "0",
        "is_open_source": "1",
        "is_proxy": "0",
        "hidden_owner": "0",
        "is_mintable": "0",
        "transfer_pausable": "0",
        "is_blacklisted": "0",
        "is_whitelisted": "0",
        "owner_change_balance": "0",
        "owner_address": ZERO,
        "can_take_back_ownership": "0",
        "lp_holders": [{"address": "0x" + "1" * 40, "percent": "0.99", "is_locked": 1, "locked_detail": YEAR_LOCK}],
        "dex": [{"liquidity_type": "UniV2", "name": "UniswapV2", "liquidity": "250000", "pair": V2_POOL}],
        "holders": [{"address": "0x" + f"{i:040x}"[-40:].replace("0", "a"), "percent": "0.02", "is_locked": 0} for i in range(10)],
    }
    t.update(over)
    return t


def by_name(res):
    return {c["name"]: c for c in res["checks"]}


# ---------- pure analytics ----------


def test_shib_live_sample_verdicts():
    res = assess(SHIB_TOKEN, liquidity_usd=4_000_000.0, market_cap_usd=5_000_000_000.0)
    c = by_name(res)
    assert res["token"] == {"name": "SHIBA INU", "symbol": "SHIB"}
    for name in ("honeypot", "cannot_sell_all", "buy_tax", "sell_tax", "tax_modifiable", "open_source", "proxy", "mintable"):
        assert c[name]["verdict"] == "pass", name
    assert c["buy_tax"]["value"] == 0.0
    # No owner_address in the live answer: unknown, never pass.
    assert c["ownership_renounced"]["verdict"] == "unknown"
    # 10 of 244 LP holders listed (0.714 of LP supply), none locked: even if the rest were locked, < 0.95.
    assert c["lp_locked"]["verdict"] == "fail" and c["lp_locked"]["value"] == 0.0
    # Top holder is the SHIB burn address (41 %): excluded, the other 9 are measured against non-burned supply.
    assert c["top10_holders"]["value"] == pytest.approx(0.231729 / (1 - 0.410444), abs=1e-4)
    assert c["top10_holders"]["verdict"] == "fail"
    assert c["liquidity_to_mcap"]["value"] == pytest.approx(0.0008) and c["liquidity_to_mcap"]["verdict"] == "fail"
    assert res["overall"] == "fail" and res["critical_failed"] == []
    assert res["info"]["holder_count"] == 1705298 and res["info"]["in_cex"] is True
    assert res["info"]["owner_address"] is None


def test_missing_data_is_unknown_never_pass():
    res = assess({})
    assert res["passed_count"] == 0 and res["failed"] == []
    assert res["overall"] == "unknown" and len(res["unknown"]) == len(res["checks"]) == 24
    # Empty strings (GoPlus "could not tell") are unknown too.
    res = assess(clean_token(is_honeypot="", buy_tax="", personal_slippage_modifiable=""))
    c = by_name(res)
    assert c["honeypot"]["verdict"] == c["buy_tax"]["verdict"] == c["tax_modifiable"]["verdict"] == "unknown"


def test_clean_token_passes_only_with_liquidity_data():
    assert assess(clean_token())["overall"] == "unknown"  # liquidity_to_mcap unknown
    res = assess(clean_token(), liquidity_usd=200_000.0, market_cap_usd=1_000_000.0)
    assert res["overall"] == "pass" and res["passed_count"] == 24 and res["unknown"] == []


def test_honeypot_and_tax_thresholds():
    res = assess(clean_token(is_honeypot="1", sell_tax="0.99"))
    c = by_name(res)
    assert c["honeypot"]["verdict"] == "fail" and c["honeypot"]["severity"] == "critical"
    assert c["sell_tax"]["verdict"] == "fail" and c["sell_tax"]["value"] == 0.99
    assert res["critical_failed"] == ["honeypot"] and res["overall"] == "fail"
    assert by_name(assess(clean_token(buy_tax="0.05")))["buy_tax"]["verdict"] == "pass"
    assert by_name(assess(clean_token(buy_tax="0.0501")))["buy_tax"]["verdict"] == "fail"
    c = by_name(assess(clean_token(is_open_source="0", personal_slippage_modifiable="1")))
    assert c["open_source"]["verdict"] == "fail" and c["tax_modifiable"]["verdict"] == "fail"


def test_ownership():
    c = by_name(assess(clean_token()))
    assert c["ownership_renounced"]["verdict"] == "pass"
    c = by_name(assess(clean_token(can_take_back_ownership="1")))
    assert c["ownership_renounced"]["verdict"] == "fail"
    c = by_name(assess(clean_token(owner_address="0x" + "ab" * 20)))
    assert c["ownership_renounced"]["verdict"] == "fail" and c["ownership_renounced"]["value"] == "0x" + "ab" * 20
    c = by_name(assess(clean_token(can_take_back_ownership="")))
    assert c["ownership_renounced"]["verdict"] == "unknown"
    # A non-address string from the API is never echoed.
    res = assess(clean_token(owner_address="<script>alert(1)</script>"))
    assert by_name(res)["ownership_renounced"]["verdict"] == "unknown" and res["info"]["owner_address"] is None


def test_lp_lock_uses_unlisted_remainder():
    one = "0x" + "1" * 40
    two = "0x" + "2" * 40
    # Listed locked share reaches the threshold: pass.
    assert by_name(assess(clean_token()))["lp_locked"]["verdict"] == "pass"
    # Half locked, half unlisted: the rest could be locked -> unknown.
    lp = [{"address": one, "percent": "0.5", "is_locked": 1, "locked_detail": YEAR_LOCK}]
    assert by_name(assess(clean_token(lp_holders=lp)))["lp_locked"]["verdict"] == "unknown"
    # Everything listed, half unlocked -> fail.
    lp = [
        {"address": one, "percent": "0.5", "is_locked": 1, "locked_detail": YEAR_LOCK},
        {"address": two, "percent": "0.5", "is_locked": 0},
    ]
    c = by_name(assess(clean_token(lp_holders=lp)))["lp_locked"]
    assert c["verdict"] == "fail" and c["value"] == 0.5
    # LP sent to the zero address counts as burned.
    lp = [{"address": ZERO, "percent": "0.96", "is_locked": 0}]
    assert by_name(assess(clean_token(lp_holders=lp)))["lp_locked"]["verdict"] == "pass"
    assert by_name(assess(clean_token(lp_holders=[])))["lp_locked"]["verdict"] == "unknown"


def test_top10_burn_detection_is_not_fooled_by_vanity_prefix():
    whale = "0xdead00000000000000000000000000000000beef"  # vanity prefix, not a burn address
    holders = [{"address": whale, "tag": "", "percent": "0.5", "is_locked": 0}]
    c = by_name(assess(clean_token(holders=holders)))["top10_holders"]
    assert c["verdict"] == "fail" and c["value"] == 0.5
    # GoPlus "Dead" tag counts only with is_locked = 1.
    holders = [{"address": whale, "tag": "Dead", "percent": "0.5", "is_locked": 0}]
    assert by_name(assess(clean_token(holders=holders)))["top10_holders"]["verdict"] == "fail"
    holders = [{"address": whale, "tag": "Dead", "percent": "0.5", "is_locked": 1}, {"address": "0x" + "3" * 40, "percent": "0.1"}]
    c = by_name(assess(clean_token(holders=holders)))["top10_holders"]
    assert c["verdict"] == "pass" and c["value"] == pytest.approx(0.2)
    # DEX pool contracts are not holders' concentration.
    pool = "0x" + "4" * 40
    holders = [{"address": pool, "percent": "0.6"}, {"address": "0x" + "5" * 40, "percent": "0.1"}]
    c = by_name(assess(clean_token(holders=holders, dex=[{"pair": pool}])))["top10_holders"]
    assert c["verdict"] == "pass" and c["value"] == pytest.approx(0.1)


def test_untrusted_text_is_cleaned():
    res = assess(clean_token(token_name="Ev‮il\x00" + "x" * 200, token_symbol="S​OL"))
    assert res["token"]["symbol"] == "SOL"
    assert "‮" not in res["token"]["name"] and "\x00" not in res["token"]["name"] and len(res["token"]["name"]) == 40


def test_liquidity_from_pairs_and_check():
    from tradedesk.analytics.dex import summarise_pairs

    pairs = summarise_pairs(DEX_PAIRS["pairs"], chain="ethereum", limit=100)["pairs"]
    liq, mcap, n_fake = cr.liquidity_from_pairs(pairs, SHIB)
    assert liq == pytest.approx(4_000_000.0) and mcap == pytest.approx(5e9) and n_fake == 0
    assert cr.liquidity_from_pairs(pairs, "0x" + "9" * 40) == (None, None, 0)
    c = by_name(assess(clean_token(), liquidity_usd=99.0, market_cap_usd=1000.0))["liquidity_to_mcap"]
    assert c["verdict"] == "fail" and c["value"] == 0.099
    c = by_name(assess(clean_token(), liquidity_usd=100.0, market_cap_usd=0.0))["liquidity_to_mcap"]
    assert c["verdict"] == "unknown"


TKN = "0x" + "c" * 40
FAKE_USDT_PAIR = {  # TKN / impostor "USDT" at a non-canonical address: big liquidity, no volume
    "chainId": "ethereum",
    "pairAddress": "0x" + "d" * 40,
    "baseToken": {"address": TKN, "symbol": "TKN"},
    "quoteToken": {"address": "0x" + "e" * 40, "symbol": "USDT"},
    "liquidity": {"usd": 5_000_000.0},
    "volume": {"h24": 10.0},
    "marketCap": 10_000_000,
}


def test_fake_pairs_do_not_count_as_liquidity():
    from tradedesk.analytics.dex import summarise_pairs

    pairs = summarise_pairs([FAKE_USDT_PAIR], chain="ethereum", limit=100)["pairs"]
    assert pairs[0]["likely_fake"] is True
    liq, mcap, n_fake = cr.liquidity_from_pairs(pairs, TKN)
    assert (liq, mcap, n_fake) == (None, None, 1)
    assert by_name(assess(clean_token(), liquidity_usd=liq, market_cap_usd=mcap))["liquidity_to_mcap"]["verdict"] == "unknown"
    # A small real pool next to the fake one: measured on the real pool only -> fail, not pass.
    real = {
        "chainId": "ethereum",
        "pairAddress": "0x" + "f" * 40,
        "baseToken": {"address": TKN, "symbol": "TKN"},
        "quoteToken": {"address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "symbol": "WETH"},
        "liquidity": {"usd": 50_000.0},
        "volume": {"h24": 20_000.0},
        "marketCap": 10_000_000,
    }
    pairs = summarise_pairs([FAKE_USDT_PAIR, real], chain="ethereum", limit=100)["pairs"]
    liq, mcap, n_fake = cr.liquidity_from_pairs(pairs, TKN)
    assert liq == pytest.approx(50_000.0) and n_fake == 1
    c = by_name(assess(clean_token(), liquidity_usd=liq, market_cap_usd=mcap))["liquidity_to_mcap"]
    assert c["verdict"] == "fail" and c["value"] == pytest.approx(0.005)


def test_top10_without_usable_shares_is_unknown():
    c = by_name(assess(clean_token(holders=[{"address": "0x" + "3" * 40, "percent": ""}])))["top10_holders"]
    assert c["verdict"] == "unknown" and c["value"] is None
    # Only a DEX pool listed.
    holders = [{"address": V2_POOL, "percent": "0.6"}]
    assert by_name(assess(clean_token(holders=holders)))["top10_holders"]["verdict"] == "unknown"
    # Some shares unparsable: a low figure cannot pass ...
    holders = [{"address": "0x" + "3" * 40, "percent": "0.1"}, {"address": "0x" + "5" * 40, "percent": "n/a"}]
    c = by_name(assess(clean_token(holders=holders)))["top10_holders"]
    assert c["verdict"] == "unknown" and "1 listed holder" in c["detail"]
    # ... but a high one still fails (skipped rows can only raise it).
    holders = [{"address": "0x" + "3" * 40, "percent": "0.4"}, {"address": "0x" + "5" * 40, "percent": None}]
    assert by_name(assess(clean_token(holders=holders)))["top10_holders"]["verdict"] == "fail"


def test_lp_lock_needs_v2_pools_to_hold_the_liquidity():
    # Small V2 pool fully locked, big V3 pool unlocked: the lock covers almost nothing -> unknown, not pass.
    dex = [
        {"liquidity_type": "UniV2", "liquidity": "1000", "pair": V2_POOL},
        {"liquidity_type": "UniV3", "liquidity": "900000", "pair": "0x" + "8" * 40},
    ]
    res = assess(clean_token(dex=dex), liquidity_usd=901_000.0, market_cap_usd=1_000_000.0)
    c = by_name(res)["lp_locked"]
    assert c["verdict"] == "unknown" and "V3/V4" in c["detail"] and res["overall"] == "unknown"
    # No `dex` list: the V2 share is unknown -> unknown.
    assert by_name(assess(clean_token(dex=None)))["lp_locked"]["verdict"] == "unknown"
    # Unparsable pool liquidity -> unknown.
    dex = [{"liquidity_type": "UniV2", "liquidity": "", "pair": V2_POOL}]
    assert by_name(assess(clean_token(dex=dex)))["lp_locked"]["verdict"] == "unknown"
    # V2 at >= 90 % of liquidity: pass.
    dex = [
        {"liquidity_type": "UniV2", "liquidity": "950000", "pair": V2_POOL},
        {"liquidity_type": "UniV4", "liquidity": "50000", "pair": "0x" + "8" * 64},
    ]
    assert by_name(assess(clean_token(dex=dex)))["lp_locked"]["verdict"] == "pass"
    # Live SHIB sample: V2 pools hold most liquidity, its LP is unlocked -> still fail.
    c = by_name(assess(SHIB_TOKEN))["lp_locked"]
    assert c["verdict"] == "fail" and "V2-style pools hold 0.8" in c["detail"]


def test_lp_lock_expiry():
    one = "0x" + "1" * 40

    def lp(detail):
        row = {"address": one, "percent": "1.0", "is_locked": 1}
        if detail is not ...:
            row["locked_detail"] = detail
        return [row]

    tomorrow = [{"amount": "1", "end_time": "2026-10-24T00:00:00+00:00"}]
    c = by_name(assess(clean_token(lp_holders=lp(tomorrow))))["lp_locked"]
    assert c["verdict"] == "fail" and c["value"] == 0.0
    # 20 days out is inside our 30-day horizon: still fail.
    soon = [{"end_time": "2026-11-12T00:00:00Z"}]
    assert by_name(assess(clean_token(lp_holders=lp(soon))))["lp_locked"]["verdict"] == "fail"
    assert by_name(assess(clean_token(lp_holders=lp(YEAR_LOCK))))["lp_locked"]["verdict"] == "pass"
    # Unix seconds, and one of two locks ending tomorrow -> fail.
    year_ts = int(datetime(2027, 10, 23, tzinfo=UTC).timestamp())
    assert by_name(assess(clean_token(lp_holders=lp([{"end_time": year_ts}]))))["lp_locked"]["verdict"] == "pass"
    assert by_name(assess(clean_token(lp_holders=lp(YEAR_LOCK + tomorrow))))["lp_locked"]["verdict"] == "fail"
    # Locked without a readable end time: unknown, never pass.
    for detail in (..., [], None, [{"end_time": ""}], [{"end_time": "soon"}], [{"amount": "1"}]):
        assert by_name(assess(clean_token(lp_holders=lp(detail))))["lp_locked"]["verdict"] == "unknown", detail
    # A naive clock is taken as UTC instead of raising.
    res = cr.assess(clean_token(), now=datetime(2026, 10, 23, 12, 0))
    assert by_name(res)["lp_locked"]["verdict"] == "pass"


def test_goplus_extra_flags():
    res = assess(clean_token(transfer_tax="0.5"))
    c = by_name(res)["transfer_tax"]
    assert c["verdict"] == "fail" and c["value"] == 0.5 and res["overall"] == "fail"
    assert by_name(assess(clean_token(transfer_tax="0.05")))["transfer_tax"]["verdict"] == "pass"
    res = assess(clean_token(cannot_buy="1", selfdestruct="1"))
    assert res["critical_failed"] == ["cannot_buy", "selfdestruct"]
    for field, name in (
        ("external_call", "external_call"),
        ("trading_cooldown", "trading_cooldown"),
        ("anti_whale_modifiable", "anti_whale_modifiable"),
        ("honeypot_with_same_creator", "same_creator_honeypot"),
    ):
        res = assess(clean_token(**{field: "1"}), liquidity_usd=200_000.0, market_cap_usd=1_000_000.0)
        assert res["failed"] == [name] and by_name(res)[name]["severity"] == "red_flag", field
        assert by_name(assess(clean_token(**{field: ""})))[name]["verdict"] == "unknown"
    # Optional flags: absent -> no check; present -> checked.
    assert "airdrop_scam" not in by_name(assess(clean_token()))
    assert by_name(assess(clean_token(is_airdrop_scam="1")))["airdrop_scam"]["verdict"] == "fail"
    fake = {"true_token_address": "0x" + "9" * 40, "value": 1}
    assert by_name(assess(clean_token(fake_token=fake)))["fake_token"]["verdict"] == "fail"
    assert by_name(assess(clean_token(fake_token={"value": 0})))["fake_token"]["verdict"] == "pass"


def test_addr_is_stripped():
    assert cr._addr(f" {ZERO}\n") == ZERO
    assert cr._addr(ZERO.upper().replace("0X", "0x") + "\n") == ZERO
    assert cr._addr(ZERO + "\nx") is None
    res = assess(clean_token(owner_address=f"  {ZERO} "))
    assert by_name(res)["ownership_renounced"]["verdict"] == "pass" and res["info"]["owner_address"] == ZERO
    # A burn address with surrounding whitespace is still recognised.
    holders = [{"address": f"{ZERO} ", "percent": "0.5"}, {"address": "0x" + "3" * 40, "percent": "0.1"}]
    c = by_name(assess(clean_token(holders=holders)))["top10_holders"]
    assert c["value"] == pytest.approx(0.2)


def test_address_and_chain_validation():
    assert cr.valid_evm_address("  0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE ") == (SHIB, None)
    for bad in ("", "0x123", "95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce", "0x" + "g" * 40, "0x" + "a" * 41, "../x"):
        addr, err = cr.valid_evm_address(bad)
        assert addr is None and err
    assert cr.resolve_chain("ETH") == ("ethereum", "1")
    assert cr.resolve_chain("56") == ("bsc", "56")
    assert cr.resolve_chain("base") == ("base", "8453")
    assert cr.resolve_chain("solana") is None and cr.resolve_chain(None) is None


# ---------- MCP tool ----------


async def call(server, name, **args):
    async with Client(server) as c:
        res = await c.call_tool(name, args)
    data = res.structured_content
    if data is None:
        data = json.loads(res.content[0].text)
    return data["result"] if set(data) == {"result"} else data


def make_server(goplus_handler=None, dex_handler=None, seen=None):
    def default_goplus(req):
        if seen is not None:
            seen.append(req)
        return httpx.Response(200, json=copy.deepcopy(SHIB_BODY))

    tr = {
        "goplus": httpx.MockTransport(goplus_handler or default_goplus),
        "dexscreener": httpx.MockTransport(dex_handler or (lambda req: httpx.Response(200, json=DEX_PAIRS))),
    }
    rt = Runtime(Settings(http_retries=1, cache_scale=0.0), transports=tr)
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


async def test_contract_risk_happy():
    seen = []
    env = await call(make_server(seen=seen), "contract_risk", chain="eth", address="0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE")
    assert env["ok"] is True and env["missing"] == []
    assert seen[0].url.path == "/api/v1/token_security/1"
    assert seen[0].url.params["contract_addresses"] == SHIB
    d = env["data"]
    assert d["chain"] == "ethereum" and d["address"] == SHIB and d["overall"] == "fail"
    c = by_name(d)
    assert c["liquidity_to_mcap"]["value"] == pytest.approx(0.0008)  # bsc pair ignored
    assert d["info"]["liquidity_usd"] == 4_000_000.0
    assert {e["provider"] for e in env["evidence"]} == {"goplus", "dexscreener"}
    assert any("Heuristic" in w for w in env["warnings"])
    assert any("unknown is not pass" in w for w in env["warnings"])


async def test_contract_risk_goplus_failure():
    env = await call(
        make_server(goplus_handler=lambda req: httpx.Response(503, text="down")), "contract_risk", chain="ethereum", address=SHIB
    )
    assert env["ok"] is False and env["data"] is None
    assert any(m["provider"] == "goplus" for m in env["missing"])


async def test_contract_risk_dexscreener_failure_keeps_goplus_checks():
    env = await call(make_server(dex_handler=lambda req: httpx.Response(503, text="down")), "contract_risk", chain="ethereum", address=SHIB)
    assert env["ok"] is True
    assert by_name(env["data"])["liquidity_to_mcap"]["verdict"] == "unknown"
    assert [m["provider"] for m in env["missing"]] == ["dexscreener"]


async def test_contract_risk_empty_result_and_error_codes():
    srv = make_server(goplus_handler=lambda req: httpx.Response(200, json=EMPTY_BODY))
    env = await call(srv, "contract_risk", chain="ethereum", address=SHIB)
    assert env["ok"] is False and env["missing"][0]["reason"] == "data_unavailable"
    limited = {"code": 4029, "message": "too many requests <b>", "result": {}}
    env = await call(
        make_server(goplus_handler=lambda req: httpx.Response(200, json=limited)), "contract_risk", chain="ethereum", address=SHIB
    )
    assert env["ok"] is False and env["missing"][0]["reason"] == "rate_limited"
    assert "<b>" not in env["missing"][0]["detail"]  # API message is not echoed
    partial = copy.deepcopy(SHIB_BODY) | {"code": 2}
    env = await call(
        make_server(goplus_handler=lambda req: httpx.Response(200, json=partial)), "contract_risk", chain="ethereum", address=SHIB
    )
    assert env["ok"] is True and any("partial" in w for w in env["warnings"])


async def test_goplus_error_codes_are_not_cached_and_hit_health():
    calls = []
    limited = {"code": 4029, "message": "too many requests", "result": {}}

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=limited if len(calls) == 1 else copy.deepcopy(SHIB_BODY))

    rt = Runtime(Settings(http_retries=0, cache_scale=1.0), transports={"goplus": httpx.MockTransport(handler)})
    try:
        with pytest.raises(cr_errors.ProviderError) as e:
            await rt.goplus.token_security("1", SHIB)
        assert e.value.reason == "rate_limited"
        h = rt.health.snapshot()["providers"]["goplus"]
        assert h["status"] != "ok" and h["last_reason"] == "rate_limited"
        # The 4029 answer was not cached: the next call goes to the network and succeeds ...
        f = await rt.goplus.token_security("1", SHIB)
        assert f.data["code"] == 1 and len(calls) == 2 and not f.evidence.cached
        # ... and the successful answer is cached.
        f = await rt.goplus.token_security("1", SHIB)
        assert len(calls) == 2 and f.evidence.cached
    finally:
        await rt.aclose()


async def test_contract_risk_warns_about_fake_pairs():
    env = await call(
        make_server(
            dex_handler=lambda req: httpx.Response(
                200, json={"pairs": [FAKE_USDT_PAIR | {"baseToken": {"address": SHIB, "symbol": "SHIB"}}]}
            )
        ),
        "contract_risk",
        chain="ethereum",
        address=SHIB,
    )
    assert env["ok"] is True
    assert by_name(env["data"])["liquidity_to_mcap"]["verdict"] == "unknown"
    assert any("fake-ticker" in w for w in env["warnings"])


async def test_contract_risk_invalid_arguments_make_no_request():
    seen = []
    srv = make_server(seen=seen)
    env = await call(srv, "contract_risk", chain="ethereum", address="0xnot-an-address")
    assert env["ok"] is False and env["missing"][0]["reason"] == "invalid_argument"
    env = await call(srv, "contract_risk", chain="solana", address=SHIB)
    assert env["ok"] is False and env["missing"][0]["field"] == "chain"
    assert seen == []
