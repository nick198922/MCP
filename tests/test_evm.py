"""onchain_* tools (Etherscan API V2) on a DOCUMENTATION-DERIVED sample (tests/fixtures/etherscan_v2_docs_2026-11-19.json,
not a live capture: the shapes follow the Etherscan docs, the numbers are illustrative) behind httpx.MockTransport. The
key is a test-only dummy; the tests check it reaches the request but never the envelope."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from test_server import call
from tradedesk.analytics.evm import abi_string, hex_uint, scaled, source_summary, tx_row, uint_text, valid_evm_address
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.providers.etherscan import _body_check, _proxy_check, resolve_chain
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = json.loads((Path(__file__).parent / "fixtures" / "etherscan_v2_docs_2026-11-19.json").read_text())
HOLDER = FIX["holder"]
USDT = FIX["token"]
KEY = "test-dummy-etherscan-key"  # not a real key: the transport below is a fake
NOW = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
SRC = "etherscan"


def serve(req: httpx.Request) -> httpx.Response:
    if req.url.path != "/v2/api":
        return httpx.Response(404, text="not mocked")
    p = req.url.params
    module, action = p.get("module"), p.get("action")
    if module == "account" and action in ("balance", "tokenbalance", "txlist", "tokentx"):
        return httpx.Response(200, json=FIX[action])
    if module == "contract" and action == "getsourcecode":
        verified = p.get("address") == USDT
        return httpx.Response(200, json=FIX["getsourcecode_verified" if verified else "getsourcecode_unverified"])
    if module == "proxy" and action == "eth_call":
        body = FIX["eth_call"].get(p.get("data")) if p.get("to") == USDT else None  # a wallet has no code: reverts
        return httpx.Response(200, json=body if body is not None else FIX["eth_call_reverted"])
    return httpx.Response(404, text="not mocked")


def make_server(handler=serve, key: str | None = KEY, cache_scale: float = 0.0):
    # The production bucket (2 req/s) would only pace the fakes in real time.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(
            Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=cache_scale, etherscan_api_key=key),
            transports={SRC: httpx.MockTransport(handler)},
        )
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


def _down(req: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


def _no_key_anywhere(env) -> None:
    assert KEY not in json.dumps(env)
    assert all("apikey" not in e["args"] for e in env["evidence"])


# ---------------------------------------------------------------- happy paths


async def test_native_balance_happy():
    seen = []

    def spy(req):
        seen.append(req.url.params)
        return serve(req)

    env = await call(make_server(spy), "onchain_balance", chain="eth", address=HOLDER.upper().replace("0X", "0x"))
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert (d["chain"], d["chain_id"], d["address"], d["asset"], d["symbol"]) == ("ethereum", 1, HOLDER, "native", "ETH")
    assert d["balance_raw"] == "40891626854930000000000" and d["balance"] == pytest.approx(40891.62685493)
    assert seen[0]["chainid"] == "1" and seen[0]["apikey"] == KEY and seen[0]["address"] == HOLDER
    assert env["evidence"][0]["provider"] == SRC and env["evidence"][0]["tool"] == "account/balance"
    _no_key_anywhere(env)


async def test_token_balance_reads_decimals_and_symbol():
    env = await call(make_server(), "onchain_balance", chain="ethereum", address=HOLDER, token=USDT)
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert (d["asset"], d["token"], d["symbol"], d["decimals"]) == ("erc20", USDT, "USDT", 6)
    assert d["balance_raw"] == "1234567890123" and d["balance"] == pytest.approx(1_234_567.890123)
    assert {e["tool"] for e in env["evidence"]} == {"account/tokenbalance", "proxy/eth_call"} and len(env["evidence"]) == 3
    _no_key_anywhere(env)


async def test_token_balance_without_decimals_keeps_the_raw_amount():
    def no_views(req):
        return httpx.Response(200, json=FIX["eth_call_reverted"]) if req.url.params.get("module") == "proxy" else serve(req)

    env = await call(make_server(no_views), "onchain_balance", chain="ethereum", address=HOLDER, token=USDT)
    assert env["ok"] is True and env["data"]["balance_raw"] == "1234567890123" and env["data"]["balance"] is None
    assert {(m["field"], m["reason"]) for m in env["missing"]} == {("decimals", "data_unavailable"), ("symbol", "data_unavailable")}
    assert any("decimals could not be read" in w for w in env["warnings"])
    assert any("does not look like an ERC-20" in w for w in env["warnings"])


async def test_token_balance_of_a_real_token_has_no_not_erc20_warning():
    env = await call(make_server(), "onchain_balance", chain="ethereum", address=HOLDER, token=USDT)
    assert not any("ERC-20 contract" in w for w in env["warnings"])


async def test_normal_transactions_page():
    seen = []

    def spy(req):
        seen.append(req.url.params)
        return serve(req)

    env = await call(make_server(spy), "onchain_address_txs", chain="1", address=HOLDER, limit=2, page=3)
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert (d["kind"], d["page"], d["limit"], d["count"], d["has_more"], d["native"]) == ("normal", 3, 2, 2, True, "ETH")
    out, failed = d["rows"]
    assert out["direction"] == "out" and out["value_wei"] == "1500000000000000000" and out["value"] == pytest.approx(1.5)
    assert out["fee"] == pytest.approx(21000 * 20e9 / 1e18) and out["failed"] is False and out["method"] is None
    assert out["time_utc"] == "2026-11-19T06:34:39+00:00" and out["block"] == 21_234_567 and out["contract_created"] is None
    assert failed["direction"] == "in" and failed["failed"] is True and failed["method"] == "transfer" and failed["value"] == 0
    p = seen[0]
    assert (p["action"], p["page"], p["offset"], p["sort"]) == ("txlist", "3", "2", "desc") and "contractaddress" not in p
    _no_key_anywhere(env)


async def test_erc20_transfers_with_token_filter():
    seen = []

    def spy(req):
        seen.append(req.url.params)
        return serve(req)

    env = await call(make_server(spy), "onchain_address_txs", chain="ethereum", address=HOLDER, kind="erc20", token=USDT)
    assert env["ok"] is True
    d = env["data"]
    assert d["count"] == 1 and d["has_more"] is False and d["token"] == USDT
    row = d["rows"][0]
    assert (row["token"], row["token_symbol"], row["token_name"], row["token_decimals"]) == (USDT, "USDT", "Tether USD", 6)
    assert row["value_raw"] == "250000000000" and row["value"] == pytest.approx(250_000.0) and row["direction"] == "out"
    assert seen[0]["action"] == "tokentx" and seen[0]["contractaddress"] == USDT
    assert any("match tokens by address" in w for w in env["warnings"])


async def test_no_transactions_found_is_an_empty_page_not_an_error():
    def empty(req):
        return httpx.Response(200, json=FIX["tokentx_empty"])

    env = await call(make_server(empty), "onchain_address_txs", chain="base", address=HOLDER, kind="erc20")
    assert env["ok"] is True and env["missing"] == []
    assert env["data"]["count"] == 0 and env["data"]["rows"] == [] and env["data"]["chain_id"] == 8453
    assert any("No transactions on this page" in w for w in env["warnings"])


async def test_token_info_happy():
    env = await call(make_server(), "onchain_token_info", chain="ethereum", address=USDT)
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert (d["name"], d["symbol"], d["decimals"]) == ("Tether USD", "USDT", 6)
    assert d["total_supply_raw"] == "96000000000000000" and d["total_supply"] == pytest.approx(96e9)
    assert len(env["evidence"]) == 4
    _no_key_anywhere(env)


async def test_token_info_of_a_wallet_is_not_ok():
    env = await call(make_server(), "onchain_token_info", chain="ethereum", address=HOLDER)  # eth_call reverts on a wallet
    assert env["ok"] is False and env["data"] is None
    assert {m["field"] for m in env["missing"]} == {"name", "symbol", "decimals", "total_supply"}
    assert all(m["reason"] == "data_unavailable" for m in env["missing"])


async def test_contract_source_verified_without_and_with_excerpt():
    env = await call(make_server(), "onchain_contract_source", chain="ethereum", address=USDT)
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["verified"] is True and d["contract_name"] == "TetherToken" and d["compiler"] == "v0.4.18+commit.9cf6e910"
    assert (d["optimization"], d["runs"], d["proxy"], d["implementation"]) == (False, 200, False, None)
    assert (d["source_format"], d["source_files_count"], d["source_excerpt"]) == ("single_file", 1, None)
    assert d["source_chars_total"] == len(FIX["getsourcecode_verified"]["result"][0]["SourceCode"])
    env = await call(make_server(), "onchain_contract_source", chain="ethereum", address=USDT, source_chars=24)
    assert env["data"]["source_excerpt"] == "pragma solidity ^0.4.17;"
    assert any("untrusted text" in w for w in env["warnings"])


async def test_contract_source_unverified():
    env = await call(make_server(), "onchain_contract_source", chain="ethereum", address=HOLDER)
    assert env["ok"] is True and env["data"]["verified"] is False and env["data"]["contract_name"] is None
    assert any("not verified" in w for w in env["warnings"])


# ---------------------------------------------------------------- failures and refusals


@pytest.mark.parametrize(
    ("tool", "args", "fields"),
    [
        ("onchain_balance", {"chain": "ethereum", "address": HOLDER}, {"balance"}),
        ("onchain_balance", {"chain": "ethereum", "address": HOLDER, "token": USDT}, {"balance", "decimals", "symbol"}),
        ("onchain_address_txs", {"chain": "ethereum", "address": HOLDER}, {"transactions"}),
        ("onchain_token_info", {"chain": "ethereum", "address": USDT}, {"name", "symbol", "decimals", "total_supply"}),
        ("onchain_contract_source", {"chain": "ethereum", "address": USDT}, {"source"}),
    ],
)
async def test_provider_failure_goes_to_missing(tool, args, fields):
    env = await call(make_server(_down), tool, **args)
    assert env["ok"] is False and env["data"] is None
    assert {m["field"] for m in env["missing"]} == fields
    assert all(m["provider"] == SRC and m["reason"] == "backend_error" for m in env["missing"])
    _no_key_anywhere(env)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("error_invalid_key", "auth"),
        ("error_missing_key_live", "auth"),  # the real no-key answer, 2026-11-19
        ("error_rate_limit", "rate_limited"),
        ("error_chain_not_free", "provider_tier"),
        ("error_pro_endpoint", "provider_tier"),
    ],
)
async def test_status_0_answers_are_classified_and_never_echoed(body, reason):
    env = await call(make_server(lambda req: httpx.Response(200, json=FIX[body])), "onchain_address_txs", chain="bsc", address=HOLDER)
    assert env["ok"] is False
    [m] = env["missing"]
    assert (m["field"], m["provider"], m["reason"]) == ("transactions", SRC, reason)
    assert FIX[body]["result"] not in m["detail"]  # third-party text is matched, not echoed


async def test_rate_limit_on_eth_call_is_a_failure_not_a_revert():
    def limited(req):
        return httpx.Response(200, json=FIX["error_rate_limit"]) if req.url.params.get("module") == "proxy" else serve(req)

    env = await call(make_server(limited), "onchain_token_info", chain="ethereum", address=USDT)
    assert env["ok"] is False and {m["reason"] for m in env["missing"]} == {"rate_limited"}


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "header not found"}}, "backend_error"),
        ({"jsonrpc": "2.0", "id": 1, "result": "Max rate limit reached"}, "rate_limited"),
        ({"jsonrpc": "2.0", "id": 1}, "backend_error"),
    ],
)
async def test_transient_eth_call_failure_is_not_cached(bad, reason):
    # Caching ON (production scale): a node error must not be cached as "not an ERC-20" for the 1 h TTL.
    seen = []

    def flaky(req):
        if req.url.params.get("module") == "proxy":
            seen.append(req.url.params.get("data"))
            if len(seen) == 1:
                return httpx.Response(200, json=bad)
        return serve(req)

    srv = make_server(flaky, cache_scale=1.0)
    first = await call(srv, "onchain_balance", chain="ethereum", address=HOLDER, token=USDT)
    second = await call(srv, "onchain_balance", chain="ethereum", address=HOLDER, token=USDT)
    failed = {m["field"]: m["reason"] for m in first["missing"]}
    assert list(failed.values()) == [reason] and failed.keys() <= {"decimals", "symbol"}
    assert not any("does not look like an ERC-20" in w for w in first["warnings"])
    assert second["missing"] == [] and (second["data"]["decimals"], second["data"]["symbol"]) == (6, "USDT")
    assert len(seen) == 3  # the failed view was asked again; the good one came from the cache


async def test_revert_on_eth_call_is_still_data_unavailable_and_cached():
    seen = []

    def reverting(req):
        if req.url.params.get("module") == "proxy":
            seen.append(req.url.params.get("data"))
            return httpx.Response(200, json=FIX["eth_call_reverted"])
        return serve(req)

    srv = make_server(reverting, cache_scale=1.0)
    for _ in range(2):
        env = await call(srv, "onchain_token_info", chain="ethereum", address=USDT)
        assert env["ok"] is False and {m["reason"] for m in env["missing"]} == {"data_unavailable"}
    assert len(seen) == 4  # a revert is a real answer: cached, not re-asked


async def test_empty_page_is_cached_and_status_0_is_not():
    seen = []
    answers = [FIX["error_rate_limit"], FIX["tokentx_empty"]]

    def scripted(req):
        seen.append(req.url)
        return httpx.Response(200, json=answers[min(len(seen), len(answers)) - 1])

    srv = make_server(scripted, cache_scale=1.0)
    envs = [await call(srv, "onchain_address_txs", chain="base", address=HOLDER, kind="erc20") for _ in range(3)]
    assert envs[0]["ok"] is False and envs[0]["missing"][0]["reason"] == "rate_limited"
    assert envs[1]["ok"] is True and envs[2]["ok"] is True and envs[2]["data"]["count"] == 0
    assert len(seen) == 2  # the NOTOK answer was asked again, the empty page came from the cache


def test_proxy_check():
    assert _proxy_check({"jsonrpc": "2.0", "id": 1, "result": "0x"}) is None
    assert _proxy_check(FIX["eth_call"]["0x313ce567"]) is None
    assert _proxy_check(FIX["eth_call_reverted"]) is None
    assert _proxy_check({"error": {"message": "header not found"}}) == ("backend_error", "eth_call failed upstream")
    assert _proxy_check({"result": "Max rate limit reached"})[0] == "rate_limited"
    assert _proxy_check({"result": "0xzz"})[0] == "backend_error" and _proxy_check({"error": "revert"})[0] == "backend_error"
    assert _proxy_check(FIX["error_rate_limit"])[0] == "rate_limited" and _proxy_check([1])[0] == "backend_error"


async def test_key_echoed_in_an_error_body_is_scrubbed():
    def echo(req):
        return httpx.Response(400, text=f"bad request for apikey {req.url.params['apikey']}")

    env = await call(make_server(echo), "onchain_balance", chain="ethereum", address=HOLDER)
    assert env["ok"] is False and env["missing"][0]["reason"] == "backend_error"
    _no_key_anywhere(env)


@pytest.mark.parametrize("tool", ["onchain_balance", "onchain_address_txs", "onchain_token_info", "onchain_contract_source"])
async def test_without_a_key_no_request_and_missing_config(tool):
    seen = []

    def counting(req):
        seen.append(req.url)
        return serve(req)

    env = await call(make_server(counting, key=None), tool, chain="ethereum", address=HOLDER)
    assert env["ok"] is False and seen == []
    [m] = env["missing"]
    assert m["reason"] == "config" and "ETHERSCAN_API_KEY" in m["detail"]


@pytest.mark.parametrize(
    ("tool", "args", "field"),
    [
        ("onchain_balance", {"chain": "solana", "address": HOLDER}, "chain"),
        ("onchain_balance", {"chain": "ethereum", "address": "0x123"}, "address"),
        ("onchain_token_info", {"chain": "ethereum", "address": USDT[:20] + "\n" + USDT[20:]}, "address"),
        ("onchain_contract_source", {"chain": "ethereum", "address": "0x" + "g" * 40}, "address"),
        ("onchain_balance", {"chain": "ethereum", "address": HOLDER, "token": "USDT"}, "token"),
        ("onchain_address_txs", {"chain": "ethereum", "address": HOLDER, "kind": "erc20", "token": "USDT"}, "token"),
        ("onchain_address_txs", {"chain": "ethereum", "address": HOLDER, "kind": "normal", "token": USDT}, "token"),
        ("onchain_address_txs", {"chain": "ethereum", "address": HOLDER, "page": 101, "limit": 100}, "page"),
    ],
)
async def test_invalid_input_makes_no_request(tool, args, field):
    seen = []

    def counting(req):
        seen.append(req.url)
        return serve(req)

    env = await call(make_server(counting), tool, **args)
    assert env["ok"] is False and seen == []
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [(field, "invalid_argument")]


# ---------------------------------------------------------------- pure helpers


def test_resolve_chain_names_aliases_and_ids():
    assert resolve_chain("ETH") == ("ethereum", 1, "ETH")
    assert resolve_chain(" matic ") == ("polygon", 137, "POL")
    assert resolve_chain("42161") == ("arbitrum", 42161, "ETH")
    assert resolve_chain("op")[1] == 10 and resolve_chain("base")[1] == 8453 and resolve_chain("bnb")[1] == 56
    assert resolve_chain("solana") is None and resolve_chain(None) is None and resolve_chain(1) is None


def test_body_check():
    assert _body_check({"status": "1", "message": "OK", "result": "5"}) is None
    assert _body_check(FIX["tokentx_empty"]) is None
    assert _body_check({"status": "0", "message": "No transactions found", "result": "x"})[0] == "backend_error"
    assert _body_check({"status": "0", "message": "NOTOK", "result": "something new"}) == (
        "backend_error",
        "Etherscan answered status 0 (NOTOK)",
    )
    assert _body_check({"status": None, "result": "5"})[0] == "backend_error"
    assert _body_check([1])[0] == "backend_error"


def test_valid_evm_address():
    assert valid_evm_address(" " + USDT.upper().replace("0X", "0x") + " ") == (USDT, None)
    for bad in (None, 5, "", "0x" + "1" * 39, "0x" + "1" * 41, "1" * 42, "0x" + "1" * 20 + " " + "1" * 20):
        assert valid_evm_address(bad)[0] is None


def test_abi_decoding():
    eth_call = FIX["eth_call"]
    assert abi_string(eth_call["0x06fdde03"]["result"]) == "Tether USD" and abi_string(eth_call["0x95d89b41"]["result"]) == "USDT"
    assert abi_string("0x" + b"Maker".hex().ljust(64, "0")) == "Maker"  # bytes32 (MKR-style)
    assert abi_string("0x") is None and abi_string("0x123") is None and abi_string(5) is None
    assert abi_string("0x" + "00" * 31 + "40" + "00" * 32) is None  # offset past the end
    assert abi_string("0x" + "00" * 31 + "20" + "00" * 31 + "ff") is None  # length past the end
    evil = "0x" + (32).to_bytes(32, "big").hex() + (9).to_bytes(32, "big").hex() + "A\x1b[2J‮B".encode().hex().ljust(64, "0")
    assert abi_string(evil) == "A [2J B"  # control and bidi characters dropped
    assert hex_uint(eth_call["0x313ce567"]["result"]) == 6 and hex_uint("0x") is None and hex_uint("0x" + "f" * 65) is None


def test_numbers():
    assert uint_text("123") == 123 and uint_text(" 7 ") == 7 and uint_text(5) == 5
    for bad in (True, -1, "-1", "1.5", "1e3", "", None, [], "9" * 79, 2**256):
        assert uint_text(bad) is None
    assert scaled(1_500_000, 6) == 1.5 and scaled(None, 6) is None and scaled(1, None) is None and scaled(1, 300) is None
    assert scaled(2**256 - 1, 0) == pytest.approx(1.157920892373162e77)


def test_tx_row_survives_wrong_types():
    row = {**FIX["txlist"]["result"][0], "value": "abc", "timeStamp": [], "gasUsed": None, "hash": "0x12", "from": 5}
    r = tx_row(row, HOLDER, "normal")
    assert r["value_wei"] is None and r["value"] is None and r["time_utc"] is None and r["fee"] is None
    assert r["hash"] is None and r["from"] is None and r["direction"] is None
    assert tx_row("x", HOLDER, "normal") is None
    erc = tx_row({**FIX["tokentx"]["result"][0], "tokenDecimal": "999", "tokenSymbol": "US​DT\n"}, HOLDER, "erc20")
    assert erc["token_decimals"] is None and erc["value"] is None and erc["token_symbol"] == "US DT"


def test_source_summary_standard_json_and_files():
    sources = {f"contracts/F{i}.sol": {"content": f"// file {i}"} for i in range(25)}
    sources["contracts/Main.sol"] = {"content": "contract Main {}\n// tail"}
    item = {"SourceCode": "{" + json.dumps({"language": "Solidity", "sources": sources}) + "}", "ABI": "[]", "ContractName": "Main",
            "Proxy": "1", "Implementation": "0x" + "ab" * 20}  # fmt: skip
    s = source_summary(item, source_chars=16)
    assert s["verified"] is True and s["source_format"] == "standard_json" and s["source_files_count"] == 26
    assert len(s["source_files"]) == 20 and s["source_excerpt"] == "contract Main {}"
    assert s["proxy"] is True and s["implementation"] == "0x" + "ab" * 20
    multi = source_summary({"SourceCode": json.dumps({"A.sol": {"content": "a"}}), "ContractName": "B"}, 5)
    assert multi["source_format"] == "multi_file_json" and multi["source_excerpt"] == "a"  # no B.sol: the first file
    broken = source_summary({"SourceCode": "{{not json}}", "ContractName": "X"}, 0)
    assert broken["source_format"] == "single_file" and broken["source_files_count"] == 1
    assert source_summary(None) is None
    assert source_summary({"SourceCode": "x", "ABI": "Contract source code not verified"})["verified"] is False
