"""contract_risk on Solana: GoPlus solana/token_security (live samples 2026-11-19) and DexScreener behind MockTransport."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from test_contract_risk import by_name, call, make_server
from tradedesk.analytics import contract_risk as cr
from tradedesk.config import Settings
from tradedesk.core import errors as cr_errors
from tradedesk.runtime import Runtime

FIX = Path(__file__).parent / "fixtures"
LIVE = json.loads((FIX / "goplus_solana_token_security_2026-11-19.json").read_text())["responses"]
JUP = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
PYUSD = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
BERN = "CKfatsPMUf8SkiURsDXs7eK6GWb4Jsd6UDbs7twMCWxo"
WSOL = "So11111111111111111111111111111111111111112"
EMPTY = {"code": 1, "message": "ok", "result": {}}
POOL = "Poo1" + "1" * 40

# Synthetic DexScreener pairs in the documented shape (Solana chain id).
JUP_PAIRS = {
    "pairs": [
        {
            "chainId": "solana",
            "dexId": "raydium",
            "pairAddress": "C1MgLojNLWBKADvu9BHdtgzz1oZX4dZ5zGdGcgvvW8Wz",
            "baseToken": {"address": JUP, "name": "Jupiter", "symbol": "JUP"},
            "quoteToken": {"address": WSOL, "name": "Wrapped SOL", "symbol": "SOL"},
            "liquidity": {"usd": 600_000.0},
            "volume": {"h24": 900_000.0},
            "marketCap": 1_500_000_000,
        }
    ]
}


def token(name):
    body = LIVE[name]
    return next(iter(body["result"].values()))


def clean_sol(**over):
    """Every Solana check passes: no authorities, no fee or hook, LP 100 % burned, spread holders."""
    off = {"authority": [], "status": "0"}
    t = {
        "metadata": {"name": "Clean", "symbol": "CLN"},
        "non_transferable": "0",
        "default_account_state": "1",
        "transfer_hook": [],
        "transfer_fee": {},
        "total_supply": "1000000",
        "dex": [{"id": POOL, "tvl": "250000", "type": "Standard", "burn_percent": 100}],
        "holders": [{"account": f"Ho1der{i}" + "z" * 32, "percent": "0.02", "is_locked": 0, "tag": ""} for i in range(1, 10)],
    }
    for f in (
        "freezable",
        "mintable",
        "closable",
        "balance_mutable_authority",
        "transfer_fee_upgradable",
        "transfer_hook_upgradable",
        "default_account_state_upgradable",
    ):
        t[f] = dict(off)
    t.update(over)
    return t


def assess(t, **kw):
    return cr.assess_solana(t, **kw)


# ---------- pure analytics on the live samples ----------


def test_live_jup_classic_spl():
    res = assess(token("jup"), liquidity_usd=600_000.0, market_cap_usd=1_500_000_000.0)
    c = by_name(res)
    assert res["token"] == {"name": "Jupiter", "symbol": "JUP"}
    for name in (
        "non_transferable",
        "default_account_frozen",
        "freezable",
        "transfer_hook",
        "transfer_tax",
        "tax_modifiable",
        "mintable",
        "owner_change_balance",
        "closable",
        "upgradable",
    ):
        assert c[name]["verdict"] == "pass", name
    assert c["transfer_tax"]["value"] == 0.0  # `transfer_fee: {}` = no extension
    # Two wallets hold ~49 %: concentration fails.
    assert c["top10_holders"]["verdict"] == "fail" and c["top10_holders"]["value"] > 0.6
    # Most pool liquidity has no burn_percent and the rest is barely burned: even all-unknown-locked stays < 0.95.
    assert c["lp_locked"]["verdict"] == "fail"
    assert c["liquidity_to_mcap"]["verdict"] == "fail" and res["critical_failed"] == []
    assert res["overall"] == "fail" and len(res["checks"]) == 14
    assert c["honeypot"]["verdict"] == "unknown" and c["honeypot"]["severity"] == "critical"
    assert "honeypot" in res["unknown"]
    assert res["info"]["holder_count"] == 825754 and res["info"]["metadata_mutable"] is True
    assert set(res["not_checked"]) >= {"cannot_sell_all", "buy_tax", "sell_tax", "open_source", "ownership_renounced", "pausable"}
    assert "honeypot" not in res["not_checked"]
    assert "Pausable" in res["not_checked"]["pausable"] and "freezable" not in res["not_checked"]["pausable"]
    assert not set(res["not_checked"]) & set(c)  # nothing is both scored and listed as not checked


def test_live_pyusd_token2022_authorities_fail():
    c = by_name(assess(token("pyusd")))
    for name in ("freezable", "mintable", "closable", "owner_change_balance", "tax_modifiable", "upgradable"):
        assert c[name]["verdict"] == "fail" and c[name]["severity"] == "red_flag", name
    assert c["transfer_tax"]["verdict"] == "pass" and c["transfer_tax"]["value"] == 0.0  # fee_rate "0"
    assert c["non_transferable"]["verdict"] == c["default_account_frozen"]["verdict"] == "pass"


def test_live_bern_transfer_fee_takes_worse_rate():
    res = assess(token("bern"))
    c = by_name(res)
    # current 0.0269, scheduled 0.042 (basis points / 10 000): the worse one counts, both under 0.05.
    assert c["transfer_tax"]["verdict"] == "pass" and c["transfer_tax"]["value"] == pytest.approx(0.042)
    assert c["tax_modifiable"]["verdict"] == "fail"
    assert res["token"] == {"name": "", "symbol": ""}
    assert c["top10_holders"]["verdict"] == "fail" and c["top10_holders"]["value"] == pytest.approx(0.4584, abs=1e-4)


def test_live_wsol_zero_supply_is_unknown_not_pass():
    c = by_name(assess(token("wsol")))
    # GoPlus reports total_supply "0" and every share "0" for wSOL: a 0 % top-10 would be a false pass.
    assert c["top10_holders"]["verdict"] == "unknown" and c["top10_holders"]["value"] is None


# ---------- pure analytics: rules ----------


def test_missing_data_is_unknown_never_pass():
    res = assess({})
    assert res["overall"] == "unknown" and res["passed_count"] == 0 and res["failed"] == []
    assert len(res["unknown"]) == len(res["checks"]) == 14
    res = assess(clean_sol(freezable={"authority": []}, transfer_hook=None, default_account_state=""))
    c = by_name(res)
    assert c["freezable"]["verdict"] == c["transfer_hook"]["verdict"] == c["default_account_frozen"]["verdict"] == "unknown"


def test_clean_token_is_unknown_without_sell_simulation():
    res = assess(clean_sol())
    assert res["overall"] == "unknown" and set(res["unknown"]) == {"honeypot", "liquidity_to_mcap"}
    res = assess(clean_sol(), liquidity_usd=250_000.0, market_cap_usd=1_000_000.0)
    # Every GoPlus-backed check passes, but there is no buy/sell simulation: honeypot stays unknown.
    assert res["overall"] == "unknown" and res["passed_count"] == 13 and res["unknown"] == ["honeypot"]
    assert len(res["checks"]) == 14 and res["failed"] == []


@pytest.mark.parametrize(
    "case",
    [
        ("clean", clean_sol(), {"liquidity_usd": 250_000.0, "market_cap_usd": 1_000_000.0}),
        ("clean_no_liq", clean_sol(), {}),
        ("empty", {}, {}),
        *((name, token(name), {"liquidity_usd": 1e9, "market_cap_usd": 1e9}) for name in ("jup", "pyusd", "bern", "wsol")),
    ],
    ids=lambda case: case[0],
)
def test_solana_overall_is_never_pass(case):
    _, t, kw = case
    res = assess(t, **kw)
    assert res["overall"] != "pass"
    assert by_name(res)["honeypot"]["verdict"] == "unknown"


def test_sell_blocking_powers():
    res = assess(clean_sol(non_transferable="1", default_account_state="2"))
    assert res["critical_failed"] == ["non_transferable", "default_account_frozen"]
    assert by_name(res)["default_account_frozen"]["value"] == "frozen"
    c = by_name(assess(clean_sol(freezable={"authority": [{"address": "x"}], "status": "1"})))["freezable"]
    assert c["verdict"] == "fail" and c["severity"] == "red_flag" and "honeypot" in c["detail"]
    c = by_name(assess(clean_sol(transfer_hook=[{"program": "Hook"}])))["transfer_hook"]
    assert c["verdict"] == "fail" and c["value"] == 1
    c = by_name(assess(clean_sol(transfer_hook_upgradable={"status": "1"})))["upgradable"]
    assert c["verdict"] == "fail"


def test_transfer_fee_rules():
    def tax(tf):
        return by_name(assess(clean_sol(transfer_fee=tf)))["transfer_tax"]

    assert tax({})["verdict"] == "pass"
    assert tax(None)["verdict"] == "unknown"
    assert tax({"current_fee_rate": {"fee_rate": "0.05"}, "scheduled_fee_rate": []})["verdict"] == "pass"
    c = tax({"current_fee_rate": {"fee_rate": "0.01"}, "scheduled_fee_rate": [{"epoch": "900", "fee_rate": "0.069"}]})
    assert c["verdict"] == "fail" and c["value"] == 0.069
    # An unparsable rate cannot pass, but a known rate above the threshold still fails.
    assert tax({"current_fee_rate": {"fee_rate": ""}, "scheduled_fee_rate": []})["verdict"] == "unknown"
    assert tax({"current_fee_rate": {"fee_rate": "0.01"}, "scheduled_fee_rate": "?"})["verdict"] == "unknown"
    assert tax({"current_fee_rate": {"fee_rate": ""}, "scheduled_fee_rate": [{"fee_rate": "0.5"}]})["verdict"] == "fail"


def test_lp_lock_weighted_by_pool_liquidity():
    def lp(dex, liq=None):
        return by_name(assess(clean_sol(dex=dex), liquidity_usd=liq))["lp_locked"]

    p = POOL
    assert lp([{"id": p, "tvl": "1000", "burn_percent": 100}])["verdict"] == "pass"
    # burn_percent is percent (0-100): 96 % burned passes, 50 % fails.
    assert lp([{"id": p, "tvl": "1000", "burn_percent": 96}])["verdict"] == "pass"
    c = lp([{"id": p, "tvl": "1000", "burn_percent": 50}])
    assert c["verdict"] == "fail" and c["value"] == 0.5
    # A pool without burn_percent could be locked: unknown, not pass.
    assert lp([{"id": p, "tvl": "900", "burn_percent": 100}, {"id": p, "tvl": "100", "burn_percent": None}])["verdict"] == "unknown"
    # DexScreener sees more liquidity than the listed pools: the rest is unknown.
    assert lp([{"id": p, "tvl": "1000", "burn_percent": 100}], liq=2000.0)["verdict"] == "unknown"
    for dex in ([], None, [{"tvl": "", "burn_percent": 100}], [{"tvl": "10", "burn_percent": 150}], [{"tvl": "0", "burn_percent": 100}]):
        assert lp(dex)["verdict"] == "unknown", dex


def test_top10_excludes_pools_authorities_and_burns():
    raydium = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
    pool = POOL
    whale = "Wha1e" + "z" * 35
    holders = [
        {"account": raydium, "percent": "0.6"},
        {"account": pool, "percent": "0.2"},
        {"account": "1nc1nerator11111111111111111111111111111111", "percent": "0.5"},
        {"account": whale, "percent": "0.1"},
    ]
    c = by_name(assess(clean_sol(holders=holders)))["top10_holders"]
    assert c["verdict"] == "pass" and c["value"] == pytest.approx(0.2)  # 0.1 of the 0.5 not burned
    c = by_name(assess(clean_sol(holders=[{"account": whale, "percent": "0.4"}], total_supply="0")))["top10_holders"]
    assert c["verdict"] == "unknown"
    # Positive supply but every listed share "0" (or missing): a 0 % top-10 would be a false pass.
    zero = [{"account": whale, "percent": "0"}, {"account": "Ho1der" + "z" * 34, "percent": ""}]
    c = by_name(assess(clean_sol(holders=zero, total_supply="1000000")))["top10_holders"]
    assert c["verdict"] == "unknown" and c["value"] is None


def test_untrusted_text_is_cleaned():
    res = assess(clean_sol(metadata={"name": "Ev‮il\x00" + "x" * 200, "symbol": "S​OL"}))
    assert res["token"]["symbol"] == "SOL" and len(res["token"]["name"]) == 40 and "\x00" not in res["token"]["name"]


def test_address_and_chain_validation():
    assert cr.valid_solana_address(f"  {JUP}\n") == (JUP, None)  # case kept
    for bad in ("", "0x" + "a" * 40, "O" * 40, "l" * 40, "abc", "1" * 45, JUP + "\nx", "../" + JUP):
        addr, err = cr.valid_solana_address(bad)
        assert addr is None and err, bad
    assert cr.resolve_chain("solana") == cr.resolve_chain(" Sol ") == ("solana", "solana")
    assert "solana" in cr.SUPPORTED


# ---------- MCP tool ----------


def solana_server(goplus_body=None, goplus_status=200, seen=None, hp_seen=None, dex=None, dex_status=200):
    def goplus(req):
        if seen is not None:
            seen.append(req)
        if goplus_status != 200:
            return httpx.Response(goplus_status, text="down")
        return httpx.Response(200, json=copy.deepcopy(goplus_body if goplus_body is not None else LIVE["jup"]))

    def hp(req):
        if hp_seen is not None:
            hp_seen.append(req)
        return httpx.Response(500)

    def dexscreener(req):
        if dex_status != 200:
            return httpx.Response(dex_status, text="down")
        return httpx.Response(200, json=dex or JUP_PAIRS)

    return make_server(goplus_handler=goplus, dex_handler=dexscreener, hp_handler=hp)


async def test_contract_risk_solana_happy():
    seen, hp_seen = [], []
    env = await call(solana_server(seen=seen, hp_seen=hp_seen), "contract_risk", chain="sol", address=f" {JUP} ")
    assert env["ok"] is True
    assert seen[0].url.path == "/api/v1/solana/token_security"
    assert seen[0].url.params["contract_addresses"] == JUP  # case-sensitive base58 is sent unchanged
    assert hp_seen == []  # honeypot.is does not cover Solana: never called
    d = env["data"]
    assert d["chain"] == "solana" and d["address"] == JUP and d["overall"] == "fail"
    c = by_name(d)
    assert c["liquidity_to_mcap"]["value"] == pytest.approx(0.0004) and d["info"]["liquidity_usd"] == 600_000.0
    assert d["second_source"]["available"] is False and "not_checked" in d
    assert {e["provider"] for e in env["evidence"]} == {"goplus", "dexscreener"}
    assert [m["field"] for m in env["missing"]] == ["honeypot", "evm_only_checks"]
    assert any("no buy/sell simulation" in w and "cannot be pass" in w for w in env["warnings"])
    assert c["honeypot"]["verdict"] == "unknown" and "honeypot" in d["unknown"]


async def test_contract_risk_solana_goplus_failure():
    env = await call(solana_server(goplus_status=503), "contract_risk", chain="solana", address=JUP)
    assert env["ok"] is False and env["data"] is None
    assert any(m["provider"] == "goplus" for m in env["missing"])


async def test_contract_risk_solana_dexscreener_failure():
    # GoPlus answers, DexScreener is down: lp_locked uses the GoPlus pools alone, liquidity_to_mcap is unknown.
    clean = {"code": 1, "message": "OK", "result": {JUP: clean_sol()}}
    env = await call(solana_server(goplus_body=clean, dex_status=503), "contract_risk", chain="solana", address=JUP)
    assert env["ok"] is True
    d = env["data"]
    c = by_name(d)
    assert c["liquidity_to_mcap"]["verdict"] == "unknown" and d["info"]["liquidity_usd"] is None
    assert c["lp_locked"]["verdict"] == "pass"  # the one listed pool is 100 % burned
    assert d["overall"] == "unknown" and set(d["unknown"]) == {"honeypot", "liquidity_to_mcap"}
    assert any(m["provider"] == "dexscreener" for m in env["missing"])


@pytest.mark.parametrize(
    "body", [EMPTY, LIVE["bonk_7012"], {"code": 1, "result": {JUP: {}}}, {"code": 1, "result": {JUP.lower(): {"x": 1}}}]
)
async def test_contract_risk_solana_no_data_is_not_safe(body):
    env = await call(solana_server(goplus_body=body), "contract_risk", chain="solana", address=JUP)
    assert env["ok"] is False and env["data"] is None
    assert env["missing"][0]["provider"] == "goplus" and env["missing"][0]["reason"] == "data_unavailable"


@pytest.mark.parametrize(("dex", "expect"), [({"pairs": []}, "mistyped"), (None, "pool(s) for it on solana")])
async def test_contract_risk_solana_7012_explains_why(dex, expect):
    # Live 2026-11-20: the 7012 recorded for BONK came from a mistyped mint (no DexScreener pool either).
    env = await call(solana_server(goplus_body=LIVE["bonk_7012"], dex=dex), "contract_risk", chain="solana", address=JUP)
    assert env["ok"] is False and env["data"] is None
    hints = [w for w in env["warnings"] if "GoPlus has no data" in w]
    assert len(hints) == 1 and expect in hints[0]
    if dex is None:  # JUP pools listed: GoPlus does not cover the token; no second source on Solana
        assert "no second source for Solana" in hints[0] and "honeypot.is" not in hints[0]


async def test_contract_risk_solana_invalid_address_makes_no_request():
    seen = []
    env = await call(solana_server(seen=seen), "contract_risk", chain="solana", address="0OIl" * 10)
    assert env["ok"] is False and env["missing"][0]["field"] == "address" and seen == []


async def test_solana_7012_is_healthy_and_not_cached():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=copy.deepcopy(LIVE["bonk_7012"] if len(calls) == 1 else LIVE["jup"]))

    rt = Runtime(Settings(http_retries=0, cache_scale=1.0), transports={"goplus": httpx.MockTransport(handler)})
    try:
        with pytest.raises(cr_errors.ProviderError) as e:
            await rt.goplus.solana_token_security(JUP)
        assert e.value.reason == "data_unavailable"
        assert rt.health.snapshot()["providers"]["goplus"]["status"] == "ok"  # GoPlus answered; it just has no data
        f = await rt.goplus.solana_token_security(JUP)
        assert f.data["code"] == 1 and len(calls) == 2 and not f.evidence.cached
        f = await rt.goplus.solana_token_security(JUP)
        assert len(calls) == 2 and f.evidence.cached
        # A rate-limit code in an HTTP 200 body is still an error.
        rt2 = Runtime(
            Settings(http_retries=0),
            transports={"goplus": httpx.MockTransport(lambda req: httpx.Response(200, json={"code": 4029, "message": "x"}))},
        )
        try:
            with pytest.raises(cr_errors.ProviderError) as e:
                await rt2.goplus.solana_token_security(JUP)
            assert e.value.reason == "rate_limited"
        finally:
            await rt2.aclose()
    finally:
        await rt.aclose()
