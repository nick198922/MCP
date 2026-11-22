"""onchain_* tools (Etherscan API V2) on a DOCUMENTATION-DERIVED sample of raw bodies (tests/fixtures/
etherscan_v2_docs_2026-11-19.json: the shapes follow the Etherscan docs, the numbers are illustrative) behind
httpx.MockTransport, plus the LIVE envelopes of the MCP service from 2026-11-22 (tests/fixtures/
etherscan_v2_live_2026-11-22.json: live numbers, free-key chain coverage, output shape). The key is a test-only dummy;
the tests check it reaches the request but never the envelope."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from mcp import Client

from test_server import call
from tradedesk.analytics.evm import abi_string, hex_uint, scaled, source_summary, tx_row, uint_text, valid_evm_address
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.providers.etherscan import (
    CHAINS,
    FREE_CHAINS,
    FREE_SOURCE_CHAINS,
    SELECTORS,
    _body_check,
    _proxy_check,
    resolve_chain,
)
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = json.loads((Path(__file__).parent / "fixtures" / "etherscan_v2_docs_2026-11-19.json").read_text())
LIVE = json.loads((Path(__file__).parent / "fixtures" / "etherscan_v2_live_2026-11-22.json").read_text())
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


def make_server(handler=serve, key: str | None = KEY, cache_scale: float = 0.0, slept: list[float] | None = None):
    # The production bucket (2 req/s) would only pace the fakes in real time.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(
            Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=cache_scale, etherscan_api_key=key),
            transports={SRC: httpx.MockTransport(handler)},
        )
    rt.now = lambda: NOW
    pauses = slept if slept is not None else []

    async def no_sleep(seconds: float) -> None:  # the rate-limit retry pause, recorded instead of waited
        pauses.append(seconds)

    rt.etherscan.sleep = no_sleep
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
    assert any("does not look like an ERC-20 contract" in w for w in env["warnings"])


async def test_token_info_warns_not_erc20_when_symbol_and_decimals_revert_but_ok_is_unchanged():
    # name and totalSupply answer, symbol and decimals revert: partial data stays ok=true, with the not-ERC-20 warning.
    def two_revert(req):
        p = req.url.params
        if p.get("module") == "proxy" and p.get("data") in (SELECTORS["symbol"], SELECTORS["decimals"]):
            return httpx.Response(200, json=FIX["eth_call_reverted"])
        return serve(req)

    env = await call(make_server(two_revert), "onchain_token_info", chain="ethereum", address=USDT)
    assert env["ok"] is True and env["data"]["name"] == "Tether USD" and env["data"]["total_supply"] is None
    assert env["data"]["total_supply_raw"] == "96000000000000000"
    assert {(m["field"], m["reason"]) for m in env["missing"]} == {("symbol", "data_unavailable"), ("decimals", "data_unavailable")}
    assert any("does not look like an ERC-20 contract" in w for w in env["warnings"])


async def test_token_info_of_a_real_token_or_a_transient_failure_has_no_not_erc20_warning():
    env = await call(make_server(), "onchain_token_info", chain="ethereum", address=USDT)
    assert not any("ERC-20 contract" in w for w in env["warnings"])

    def node_error(req):  # every view fails upstream (not a revert): a failure, not evidence about the contract
        if req.url.params.get("module") == "proxy":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "header not found"}})
        return serve(req)

    env = await call(make_server(node_error), "onchain_token_info", chain="ethereum", address=USDT)
    assert env["ok"] is False and {m["reason"] for m in env["missing"]} == {"backend_error"}
    assert not any("ERC-20 contract" in w for w in env["warnings"])


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
    tries = 2 if reason == "rate_limited" else 1  # a rate limit is asked twice per call before it fails

    def flaky(req):
        if req.url.params.get("module") == "proxy":
            view = req.url.params.get("data")
            seen.append(view)
            if view == seen[0] and seen.count(view) <= tries:
                return httpx.Response(200, json=bad)
        return serve(req)

    srv = make_server(flaky, cache_scale=1.0)
    first = await call(srv, "onchain_balance", chain="ethereum", address=HOLDER, token=USDT)
    second = await call(srv, "onchain_balance", chain="ethereum", address=HOLDER, token=USDT)
    failed = {m["field"]: m["reason"] for m in first["missing"]}
    assert list(failed.values()) == [reason] and failed.keys() <= {"decimals", "symbol"}
    assert not any("does not look like an ERC-20" in w for w in first["warnings"])
    assert second["missing"] == [] and (second["data"]["decimals"], second["data"]["symbol"]) == (6, "USDT")
    assert len(seen) == 2 + tries  # the failed view was asked again; the good one came from the cache


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
    answers = [FIX["error_rate_limit"], FIX["error_rate_limit"], FIX["tokentx_empty"]]

    def scripted(req):
        seen.append(req.url)
        return httpx.Response(200, json=answers[min(len(seen), len(answers)) - 1])

    srv = make_server(scripted, cache_scale=1.0)
    envs = [await call(srv, "onchain_address_txs", chain="base", address=HOLDER, kind="erc20") for _ in range(3)]
    assert envs[0]["ok"] is False and envs[0]["missing"][0]["reason"] == "rate_limited"
    assert envs[1]["ok"] is True and envs[2]["ok"] is True and envs[2]["data"]["count"] == 0
    assert len(seen) == 3  # the NOTOK answer (asked twice) was asked again, the empty page came from the cache


async def test_rate_limit_in_a_200_body_is_retried_once_then_answers():
    seen, slept = [], []

    def scripted(req):
        seen.append(req.url)
        return httpx.Response(200, json=FIX["error_rate_limit"]) if len(seen) == 1 else serve(req)

    env = await call(make_server(scripted, slept=slept), "onchain_address_txs", chain="ethereum", address=HOLDER)
    assert env["ok"] is True and env["missing"] == [] and env["data"]["count"] == 2
    assert len(seen) == 2 and slept == [1.0]


async def test_rate_limit_on_each_eth_call_is_retried_once():
    seen, slept = [], []

    def once_per_view(req):
        if req.url.params.get("module") == "proxy":
            view = req.url.params.get("data")
            seen.append(view)
            if seen.count(view) == 1:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "Max rate limit reached"})
        return serve(req)

    env = await call(make_server(once_per_view, slept=slept), "onchain_token_info", chain="ethereum", address=USDT)
    assert env["ok"] is True and env["missing"] == [] and env["data"]["symbol"] == "USDT"
    assert len(seen) == 8 and slept == [1.0] * 4


async def test_rate_limit_twice_goes_to_missing_after_two_requests():
    seen, slept = [], []

    def limited(req):
        seen.append(req.url)
        return httpx.Response(200, json=FIX["error_rate_limit"])

    env = await call(make_server(limited, slept=slept), "onchain_balance", chain="ethereum", address=HOLDER)
    assert env["ok"] is False and env["data"] is None
    [m] = env["missing"]
    assert (m["field"], m["provider"], m["reason"]) == ("balance", SRC, "rate_limited")
    assert len(seen) == 2 and slept == [1.0]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx.Response(200, json=FIX["error_invalid_key"]), "auth"),
        (httpx.Response(200, json=FIX["error_chain_not_free"]), "provider_tier"),
        (httpx.Response(200, json={"status": "0", "message": "NOTOK", "result": "something new"}), "backend_error"),
        (httpx.Response(429, text="Too Many Requests"), "rate_limited"),  # HTTP 429: Http's own retries, not this one
    ],
)
async def test_other_failures_are_not_retried_by_the_provider(answer, reason):
    seen, slept = [], []

    def failing(req):
        seen.append(req.url)
        return answer

    env = await call(make_server(failing, slept=slept), "onchain_balance", chain="ethereum", address=HOLDER)
    assert env["ok"] is False and env["missing"][0]["reason"] == reason
    assert len(seen) == 1 and slept == []


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


# ---------------------------------------------------------------- live envelopes of 2026-11-22 (MCP service, free key)


def _word(n: int) -> str:
    return n.to_bytes(32, "big").hex()


def _abi_string(s: str) -> str:
    raw = s.encode()
    return "0x" + _word(32) + _word(len(raw)) + raw.hex().ljust(64 * max(1, -(-len(raw) // 32)), "0")


def _live_raw(views: dict[str, object], balances: dict[str, str]):
    """A fake Etherscan that answers with the raw bodies the live numbers imply (balances as returned, views ABI-encoded)."""

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.params
        if p.get("module") == "account" and p.get("action") in balances:
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": balances[p["action"]]})
        if p.get("module") == "proxy":
            view = next(v for v, sel in SELECTORS.items() if sel == p.get("data"))
            value = views[view]
            result = _abi_string(value) if isinstance(value, str) else "0x" + _word(int(value))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
        return httpx.Response(404, text="not mocked")

    return handler


COVERAGE_KEYS = (
    "_source",
    "chain_balance",
    "chain_source",
    "token_info_polygon",
    "txs_normal_arbitrum",
    "token_info_bsc_refused",
    "txs_normal_base_refused",
)


def _answered(env: dict, chain: str) -> bool:
    return env["ok"] is True and env["missing"] == [] and env["data"]["chain_id"] == CHAINS[chain][0]


def _refused(env: dict) -> bool:
    return env["ok"] is False and env["missing"] != [] and all(m["reason"] == "provider_tier" for m in env["missing"])


def test_live_free_key_chain_coverage_matches_the_code():
    # account/* and eth_call: FREE_CHAINS answer, the rest are refused as paid-plan chains.
    live = LIVE["chain_balance"]
    assert set(live) == set(CHAINS)
    for chain, entry in live.items():
        env = entry["envelope"]
        if chain in FREE_CHAINS:
            assert _answered(env, chain), chain
        else:
            assert _refused(env) and [m["reason"] for m in env["missing"]] == ["provider_tier"], chain
    # Not only the balance: token info (eth_call) on polygon and transactions (txlist) on arbitrum answer too.
    assert set(FREE_CHAINS) == {"ethereum", "polygon", "arbitrum"}
    info = LIVE["token_info_polygon"]["envelope"]
    assert _answered(info, "polygon") and (info["data"]["symbol"], info["data"]["decimals"]) == ("USDC", 6)
    assert [e["tool"] for e in info["evidence"]] == ["proxy/eth_call"] * 4
    txs = LIVE["txs_normal_arbitrum"]["envelope"]
    assert _answered(txs, "arbitrum") and txs["data"]["count"] > 0
    assert [e["tool"] for e in txs["evidence"]] == ["account/txlist"]
    # ... and the same endpoints are refused off FREE_CHAINS (one missing per eth_call view).
    assert "bsc" not in FREE_CHAINS and "base" not in FREE_CHAINS
    refused_info = LIVE["token_info_bsc_refused"]["envelope"]
    assert _refused(refused_info) and len(refused_info["missing"]) == 4
    assert _refused(LIVE["txs_normal_base_refused"]["envelope"])


def test_live_free_key_reads_contract_source_on_every_chain():
    live = LIVE["chain_source"]
    assert set(live) == set(CHAINS) == set(FREE_SOURCE_CHAINS)
    assert set(CHAINS) - set(FREE_CHAINS) == {"bsc", "optimism", "base", "avalanche"}  # the account / eth_call refusals
    for chain, entry in live.items():
        env = entry["envelope"]
        assert _answered(env, chain) and env["data"]["verified"] is True, chain
        assert [e["tool"] for e in env["evidence"]] == ["contract/getsourcecode"], chain
        assert all("apikey" not in e["args"] for e in env["evidence"]), chain


async def test_source_tool_is_not_described_as_refused_on_paid_chains():
    async with Client(make_server()) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
    source = tools["onchain_contract_source"]
    text = (source.description + " " + source.input_schema["properties"]["chain"]["description"]).lower()
    assert "refused" not in text and "provider_tier" not in text and "paid" not in text
    assert all(chain in text for chain in CHAINS)
    for name in ("onchain_balance", "onchain_address_txs", "onchain_token_info"):
        tool = tools[name]
        assert "provider_tier" in tool.description and "refused" in tool.description, name
        assert "provider_tier" in tool.input_schema["properties"]["chain"]["description"], name


def test_live_envelopes_carry_no_key_and_answered_on_ethereum():
    for name, entry in LIVE.items():
        if name in COVERAGE_KEYS:
            continue
        env = entry["envelope"]
        assert env["ok"] is True and env["missing"] == [], name
        assert all("apikey" not in e["args"] and e["args"]["chainid"] == 1 for e in env["evidence"]), name


async def test_live_numbers_round_trip_native_and_token_balance():
    native = LIVE["balance_native"]
    handler = _live_raw({}, {"balance": native["envelope"]["data"]["balance_raw"]})
    env = await call(make_server(handler), "onchain_balance", **native["args"])
    assert env["ok"] is True and env["data"] == native["envelope"]["data"]
    usdc = LIVE["balance_usdc"]
    d = usdc["envelope"]["data"]
    handler = _live_raw({"decimals": d["decimals"], "symbol": d["symbol"]}, {"tokenbalance": d["balance_raw"]})
    env = await call(make_server(handler), "onchain_balance", **usdc["args"])
    assert env["ok"] is True and env["data"] == d and env["data"]["balance"] == pytest.approx(52_363.119552)


@pytest.mark.parametrize("name", ["token_info_usdc", "token_info_weth"])
async def test_live_numbers_round_trip_token_info(name):
    live = LIVE[name]
    d = live["envelope"]["data"]
    views = {"name": d["name"], "symbol": d["symbol"], "decimals": d["decimals"], "totalSupply": int(d["total_supply_raw"])}
    env = await call(make_server(_live_raw(views, {})), "onchain_token_info", **live["args"])
    assert env["ok"] is True and env["data"] == d
    assert [e["tool"] for e in env["evidence"]] == ["proxy/eth_call"] * 4


async def test_live_output_shapes_match_the_tools_on_the_documented_bodies():
    # The live rows / summaries came out complete: the raw field names the parser reads are the ones Etherscan sends.
    normal = LIVE["txs_normal_weth"]["envelope"]["data"]["rows"]
    erc20 = LIVE["txs_erc20_usdc"]["envelope"]["data"]["rows"]
    assert normal and all(v is not None for r in normal for k, v in r.items() if k != "contract_created")
    assert erc20 and all(v is not None for r in erc20 for v in r.values())
    docs_normal = await call(make_server(), "onchain_address_txs", chain="ethereum", address=HOLDER)
    docs_erc20 = await call(make_server(), "onchain_address_txs", chain="ethereum", address=HOLDER, kind="erc20")
    assert set(normal[0]) == set(docs_normal["data"]["rows"][0]) and set(erc20[0]) == set(docs_erc20["data"]["rows"][0])
    src = LIVE["source_usdc"]["envelope"]["data"]
    docs_src = await call(make_server(), "onchain_contract_source", chain="ethereum", address=USDT)
    assert set(src) == set(docs_src["data"])
    assert (src["verified"], src["proxy"], src["contract_name"]) == (True, True, "FiatTokenProxy")
    assert src["implementation"] is not None and src["source_excerpt"].startswith("pragma solidity")
