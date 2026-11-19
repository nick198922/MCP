"""btc_* tools (mempool.space) on a real sample captured 2026-11-19 (tests/fixtures/mempool_space_2026-11-19.json)
behind httpx.MockTransport. Expected numbers are read off the sample by hand."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from test_server import call
from tradedesk.analytics.bitcoin import address_summary, block_summary, fee_bands, mempool_summary, valid_btc_address
from tradedesk.config import Settings
from tradedesk.core.ratelimit import TokenBucket
from tradedesk.runtime import Runtime
from tradedesk.server import build_server

FIX = json.loads((Path(__file__).parent / "fixtures" / "mempool_space_2026-11-19.json").read_text())
ADDR = FIX["address"]["address"]  # bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh
BLOCK_TS = 1_795_070_079  # newest block of the sample, 2026-11-19 06:34:39 UTC
NOW = datetime.fromtimestamp(BLOCK_TS + 321, UTC)
SRC = "mempool"

ROUTES = {
    "/api/v1/fees/recommended": "fees_recommended",
    "/api/mempool": "mempool",
    "/api/blocks/tip/height": "tip_height",
    "/api/v1/blocks": "blocks",
    "/api/v1/mining/hashrate/3d": "hashrate_3d",
    f"/api/address/{ADDR}": "address",
}

# Mainnet vectors (BIP173 / BIP350 and well-known addresses); on 2026-11-19 mempool.space answered 200 for each valid one
# and HTTP 400 "Invalid Bitcoin address" for each invalid one, the same verdict as valid_btc_address.
VALID = [
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
    "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",
    "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",
    ADDR,
]
INVALID = [
    "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",  # Base58Check checksum
    "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLz",  # Base58Check checksum
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",  # bech32 checksum
    "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh",  # v0 with a bech32m checksum (BIP350)
    "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj1",  # '1' is not a bech32 data character
]


def serve(req: httpx.Request) -> httpx.Response:
    key = ROUTES.get(req.url.path)
    if key is None:
        return httpx.Response(404, text="not mocked")
    return httpx.Response(200, json=FIX[key])


def make_server(handler=serve):
    # The production bucket (1 req/s) would only pace the fakes in real time.
    with patch("tradedesk.core.http.TokenBucket", lambda rate, burst: TokenBucket(1_000.0, 1_000)):
        rt = Runtime(Settings(http_retries=0, http_retry_backoff=0.0, cache_scale=0.0), transports={SRC: httpx.MockTransport(handler)})
    rt.now = lambda: NOW
    srv, _ = build_server(rt)
    return srv


def _down(req: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="down")


def _evidence_ok(env, n: int) -> None:
    assert len(env["evidence"]) == n and {e["provider"] for e in env["evidence"]} == {SRC}


# ---------------------------------------------------------------- happy paths on the real sample


async def test_btc_fees_happy():
    env = await call(make_server(), "btc_fees")
    assert env["ok"] is True and env["missing"] == []
    assert env["data"] == {f"{k}_sat_vb": 1.0 for k in ("fastest", "half_hour", "hour", "economy", "minimum")}
    _evidence_ok(env, 1)
    assert any("not a confirmation guarantee" in w for w in env["warnings"])


async def test_btc_mempool_happy():
    env = await call(make_server(), "btc_mempool")
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["tx_count"] == 72_496 and d["vsize_vb"] == 31_672_781
    assert d["blocks_of_backlog"] == pytest.approx(31.672781)
    assert d["total_fee_sats"] == 3_881_601 and d["total_fee_btc"] == pytest.approx(0.03881601)
    assert d["avg_fee_rate_sat_vb"] == pytest.approx(3_881_601 / 31_672_781)
    # the histogram sums to the mempool vsize; on 2026-11-19 all of it paid under 2 sat/vB
    assert d["histogram_vsize_vb"] == 31_672_781
    bands = {b["min_sat_vb"]: b for b in d["fee_bands"]}
    assert bands[1.0]["vsize_vb"] == 164_755 and bands[1.0]["max_sat_vb"] == 2.0
    assert bands[0.0]["vsize_vb"] == 31_508_026 and bands[0.0]["share"] == pytest.approx(31_508_026 / 31_672_781)
    assert bands[50.0]["max_sat_vb"] is None and all(bands[e]["vsize_vb"] == 0 for e in (50.0, 20.0, 10.0, 5.0, 2.0))
    _evidence_ok(env, 1)


async def test_btc_latest_block_happy():
    env = await call(make_server(), "btc_latest_block")
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["height"] == 976_542 == d["tip_height"]
    assert d["hash"] == "00000000000000000001fbdd0ca10edda11103fa43a2595550bf61526422baf7"
    assert d["time_utc"] == "2026-11-19T06:34:39+00:00" and d["age_seconds"] == pytest.approx(321)
    assert (d["tx_count"], d["size_bytes"], d["weight_wu"]) == (3035, 1_613_193, 3_992_697)
    assert d["vsize_vb"] == pytest.approx(998_174.25) and d["fullness"] == pytest.approx(0.99817425)
    assert d["median_fee_sat_vb"] == pytest.approx(2.0009, abs=1e-4)
    assert d["min_fee_sat_vb"] == pytest.approx(0.3576, abs=1e-4) and d["max_fee_sat_vb"] == pytest.approx(201.9175, abs=1e-4)
    assert d["total_fees_sats"] == 2_461_979 and d["reward_sats"] == 314_961_979
    assert d["subsidy_btc"] == pytest.approx(3.125)  # the post-2024-halving subsidy: reward - fees
    assert d["pool"] == "Foundry USA" and d["difficulty"] == pytest.approx(134_823_837_323_272.7)
    _evidence_ok(env, 2)
    assert not any("lags the chain tip" in w for w in env["warnings"])


async def test_btc_difficulty_hashrate_happy():
    env = await call(make_server(), "btc_difficulty_hashrate")
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["current_hashrate_ehs"] == pytest.approx(999.306, abs=1e-3)
    assert d["current_difficulty"] == pytest.approx(134_823_837_323_272.7) and d["current_difficulty_t"] == pytest.approx(134.824, abs=1e-3)
    assert [r["date"] for r in d["daily_avg_hashrate"]] == ["2026-11-17", "2026-11-18", "2026-11-19"]
    assert [r["avg_hashrate_ehs"] for r in d["daily_avg_hashrate"]] == pytest.approx([1102.304, 1009.829, 1051.070], abs=1e-3)
    _evidence_ok(env, 1)


async def test_btc_address_happy():
    env = await call(make_server(), "btc_address", address=ADDR.upper())  # all-caps bech32 is valid, sent lower-case
    assert env["ok"] is True and env["missing"] == []
    d = env["data"]
    assert d["address"] == ADDR
    assert d["confirmed_balance_sats"] == 1_681_911_109 - 1_287_005_839 == 394_905_270
    assert d["confirmed_balance_btc"] == pytest.approx(3.9490527)
    assert d["balance_with_mempool_sats"] == 394_905_270
    assert d["confirmed"]["tx_count"] == 1159 and d["confirmed"]["funded_txo_count"] == 1202
    assert d["unconfirmed"]["net_sats"] == 0
    _evidence_ok(env, 1)
    assert env["evidence"][0]["args"] == {"address": ADDR}
    assert any("not a wallet" in w for w in env["warnings"])


# ---------------------------------------------------------------- provider failures


@pytest.mark.parametrize(
    ("tool", "args", "fields"),
    [
        ("btc_fees", {}, {"fees"}),
        ("btc_mempool", {}, {"mempool"}),
        ("btc_latest_block", {}, {"latest_block", "tip_height"}),
        ("btc_difficulty_hashrate", {}, {"hashrate"}),
        ("btc_address", {"address": ADDR}, {"address"}),
    ],
)
async def test_provider_failure_goes_to_missing(tool, args, fields):
    env = await call(make_server(_down), tool, **args)
    assert env["ok"] is False and env["data"] is None
    assert {m["field"] for m in env["missing"]} == fields
    assert all(m["provider"] == SRC and m["reason"] == "backend_error" for m in env["missing"])


async def test_latest_block_without_tip_height_is_partial():
    def tip_down(req):
        return _down(req) if req.url.path == "/api/blocks/tip/height" else serve(req)

    env = await call(make_server(tip_down), "btc_latest_block")
    assert env["ok"] is True and env["data"]["height"] == 976_542 and env["data"]["tip_height"] is None
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [("tip_height", "backend_error")]
    assert any("tip height unavailable" in w for w in env["warnings"])


async def test_latest_block_warns_when_the_list_lags_the_tip():
    def ahead(req):
        return httpx.Response(200, json=976_544) if req.url.path == "/api/blocks/tip/height" else serve(req)

    env = await call(make_server(ahead), "btc_latest_block")
    assert env["ok"] is True and env["data"]["tip_height"] == 976_544
    assert any("lags the chain tip by 2 block(s)" in w for w in env["warnings"])


@pytest.mark.parametrize(
    ("tool", "path", "body", "reason"),
    [
        ("btc_fees", "/api/v1/fees/recommended", [1, 2], "backend_error"),
        ("btc_fees", "/api/v1/fees/recommended", {"fastestFee": "fast"}, "data_unavailable"),
        ("btc_mempool", "/api/mempool", {"count": -1, "vsize": 5}, "data_unavailable"),
        ("btc_latest_block", "/api/v1/blocks", [], "data_unavailable"),
        ("btc_latest_block", "/api/v1/blocks", [{"height": "x"}], "data_unavailable"),
        ("btc_difficulty_hashrate", "/api/v1/mining/hashrate/3d", {"hashrates": "x"}, "data_unavailable"),
        ("btc_address", f"/api/address/{ADDR}", {"chain_stats": {"funded_txo_sum": True}}, "data_unavailable"),
    ],
)
async def test_unexpected_shapes_go_to_missing(tool, path, body, reason):
    def odd(req):
        return httpx.Response(200, json=body) if req.url.path == path else serve(req)

    env = await call(make_server(odd), tool, **({"address": ADDR} if tool == "btc_address" else {}))
    assert env["ok"] is False and env["data"] is None
    assert any(m["provider"] == SRC and m["reason"] == reason for m in env["missing"])


async def test_tip_height_of_a_wrong_type_is_a_backend_error():
    def odd(req):
        return httpx.Response(200, json=True) if req.url.path == "/api/blocks/tip/height" else serve(req)

    env = await call(make_server(odd), "btc_latest_block")
    assert env["ok"] is True and env["data"]["tip_height"] is None
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [("tip_height", "backend_error")]


# ---------------------------------------------------------------- address validation


@pytest.mark.parametrize("address", VALID)
def test_valid_addresses(address):
    assert valid_btc_address(address) == (address, None)
    assert valid_btc_address(f"  {address} ")[0] == address


@pytest.mark.parametrize(
    "address",
    [
        *INVALID,
        "bc1qW508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",  # mixed case
        "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",  # testnet (BIP173 vector)
        "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn",  # testnet P2PKH
        "0x" + "1" * 40,  # EVM
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfN0",  # '0' is not Base58
        "bc1q/../../api/v1/fees/recommended",
        "1" * 34,
        "",
        "bc1" + "q" * 100,
    ],
)
def test_invalid_addresses(address):
    addr, err = valid_btc_address(address)
    assert addr is None and err


def test_invalid_address_types():
    for x in (None, 5, ["bc1q"], b"1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"):
        assert valid_btc_address(x)[0] is None


async def test_invalid_address_makes_no_request():
    seen = []

    def counting(req):
        seen.append(req.url.path)
        return serve(req)

    env = await call(make_server(counting), "btc_address", address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5")
    assert env["ok"] is False and seen == []
    assert [(m["field"], m["reason"]) for m in env["missing"]] == [("address", "invalid_argument")]
    assert "checksum" in env["missing"][0]["detail"]


# ---------------------------------------------------------------- pure helpers on edge cases


def test_fee_bands_edges_and_bad_rows():
    rows = [[50, 10], [49.99, 20], [20.0, 30], [1.0, 40], [0.99, 50], ["5", 99], [5.0, -1], [float("inf"), 7], [3.0], "x"]
    bands = {b["min_sat_vb"]: b["vsize_vb"] for b in fee_bands(rows)}
    assert bands == {50.0: 10, 20.0: 50, 10.0: 0, 5.0: 0, 2.0: 0, 1.0: 40, 0.0: 50}
    assert all(b["share"] is None for b in fee_bands(None))


def test_mempool_and_address_summaries_reject_bad_numbers():
    assert mempool_summary({"count": True, "vsize": 1}) is None
    empty = mempool_summary({"count": 0, "vsize": 0, "total_fee": 0})
    assert empty["avg_fee_rate_sat_vb"] is None and empty["histogram_vsize_vb"] == 0
    assert address_summary({"chain_stats": {"funded_txo_sum": 10, "spent_txo_sum": 4}})["unconfirmed"] is None
    assert address_summary({"chain_stats": {"funded_txo_sum": 1e400, "spent_txo_sum": 0}}) is None


def test_block_summary_untrusted_fields():
    block = {**FIX["blocks"][0], "id": "../evil", "extras": {**FIX["blocks"][0]["extras"], "pool": {"name": "Pool\x00\n" + "A" * 200}}}
    d = block_summary(block, None, BLOCK_TS - 60)
    assert d["hash"] is None and d["pool"] == "Pool" + "A" * 60 and d["age_seconds"] == -60
    no_extras = block_summary({"height": 1, "timestamp": BLOCK_TS, "weight": 4000}, 2, BLOCK_TS)
    assert no_extras["vsize_vb"] == 1000 and no_extras["reward_btc"] is None and no_extras["pool"] is None
