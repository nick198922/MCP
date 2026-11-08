"""Direct tests for the last two analytics helpers that had no mention in tests/ (BACKLOG 6.9, card item 214).

Both live in `analytics/dex.py` and feed the read-only `dex_pairs` tool: `is_canonical` (is a token address one of the
known contracts of a major asset on that chain) and `parse_pair` (one DexScreener pair -> flat dict plus the
"likely fake" flag). References are the real DexScreener sample from tests/fixtures/ (Fartcoin pools, 2026-10-17), the
published contract addresses (wSOL / USDC mint on Solana, WETH on Ethereum and Base) and hand-computed values (percent ->
decimal, epoch ms -> UTC, age in days, turnover = volume / liquidity); no output of the code under test is reused as an
expected value.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradedesk.analytics.dex import is_canonical, parse_pair

FIX = Path(__file__).parent / "fixtures"
DEX = json.loads((FIX / "dexscreener_tokens_fartcoin_2026-10-17.json").read_text())
FART = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
WSOL = "So11111111111111111111111111111111111111112"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PYUSD_SOL = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"
WETH_ETH_CHECKSUM = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
WETH_BASE = "0x4200000000000000000000000000000000000006"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def pair(base_sym: str, base_addr: str, quote_sym: str, quote_addr: str, liq, vol, chain: str = "solana") -> dict:
    return {
        "chainId": chain,
        "dexId": "raydium",
        "baseToken": {"address": base_addr, "symbol": base_sym, "name": base_sym},
        "quoteToken": {"address": quote_addr, "symbol": quote_sym, "name": quote_sym},
        "liquidity": {"usd": liq},
        "volume": {"h24": vol},
    }


# --- is_canonical ---------------------------------------------------------------------------------------------------


def test_is_canonical_solana_is_case_sensitive():
    assert is_canonical("solana", WSOL) is True
    assert is_canonical("solana", USDC_SOL) is True
    # base58 is case-sensitive: a different spelling is a different mint.
    assert is_canonical("solana", WSOL.lower()) is False
    assert is_canonical("solana", USDC_SOL.upper()) is False


def test_is_canonical_evm_ignores_case_and_whitespace():
    # The checksummed (mixed-case) WETH address as Etherscan prints it, plus a 0X prefix and padding.
    assert is_canonical("ethereum", WETH_ETH_CHECKSUM) is True
    assert is_canonical(" Ethereum ", f"  {WETH_ETH_CHECKSUM}\n") is True
    assert is_canonical("ethereum", "0X" + WETH_ETH_CHECKSUM[2:]) is True
    assert is_canonical("base", WETH_BASE) is True


def test_is_canonical_is_per_chain():
    # Ethereum WETH is not a canonical contract on Base / Arbitrum, and Base WETH is not one on Ethereum.
    assert is_canonical("base", WETH_ETH_CHECKSUM) is False
    assert is_canonical("arbitrum", WETH_ETH_CHECKSUM) is False
    assert is_canonical("ethereum", WETH_BASE) is False
    # Unknown chain id and empty input: never canonical.
    assert is_canonical("polygon", WETH_ETH_CHECKSUM) is False
    assert is_canonical("", WSOL) is False
    assert is_canonical("solana", "") is False


def test_is_canonical_on_real_sample_addresses():
    """In the saved Fartcoin pools the SOL / USDC quotes are canonical; Fartcoin itself and PYUSD are not listed."""
    quotes = {(p["chainId"], p["quoteToken"]["address"]) for p in DEX["pairs"]}
    assert quotes == {("solana", WSOL), ("solana", USDC_SOL), ("solana", PYUSD_SOL)}
    assert is_canonical("solana", WSOL) and is_canonical("solana", USDC_SOL)
    assert not is_canonical("solana", PYUSD_SOL)
    assert all(not is_canonical(p["chainId"], p["baseToken"]["address"]) for p in DEX["pairs"])


# --- parse_pair -----------------------------------------------------------------------------------------------------


def test_parse_pair_real_sample_fields():
    """First pool of the real sample (Raydium Fartcoin / SOL); every expected number is read off the raw JSON."""
    raw = DEX["pairs"][0]
    p = parse_pair(raw, NOW)
    assert p is not None
    assert p["chain"] == "solana" and p["dex"] == "raydium"
    assert p["pair_address"] == "Bzc9NZfMqkXR6fz1DBph7BDf9BroyEf6pnzESP7v5iiw"
    assert p["base_token"] == {"address": FART, "symbol": "Fartcoin", "name": "Fartcoin"}  # trailing space trimmed
    assert p["quote_token"] == {"address": WSOL, "symbol": "SOL", "name": "Wrapped SOL"}
    # String prices become floats.
    assert p["price_usd"] == pytest.approx(0.1497) and p["price_native"] == pytest.approx(0.001439)
    assert p["liquidity_usd"] == pytest.approx(7615594.18)
    assert p["volume_24h_usd"] == pytest.approx(1349283.57)
    assert p["volume_6h_usd"] == pytest.approx(58500.18) and p["volume_1h_usd"] == pytest.approx(5188.25)
    # priceChange.h24 = -2.57 (percent) -> -0.0257 (decimal).
    assert p["price_change_24h"] == pytest.approx(-0.0257, abs=1e-9)
    assert (p["txns_24h_buys"], p["txns_24h_sells"]) == (8885, 8349)
    assert p["fdv_usd"] == pytest.approx(149726199) and p["market_cap_usd"] == pytest.approx(149726199)
    # pairCreatedAt 1729231787000 ms = 2024-10-18 06:09:47 UTC; to 2026-11-07 12:00 UTC: 365 + 365 + 20 days and
    # 5 h 50 min 13 s = 750 + 21013 / 86400 = 750.2432 days -> 750.24.
    assert p["pair_created_at"] == "2024-10-18T06:09:47+00:00"
    assert p["age_days"] == pytest.approx(750.24, abs=1e-9)
    assert p["url"] == raw["url"]
    # Turnover 1349283.57 / 7615594.18 = 17.7 % and the SOL quote is the canonical wSOL mint: not fake.
    assert p["likely_fake"] is False and p["likely_fake_reason"] is None


def test_parse_pair_real_sample_none_flagged():
    """No real Fartcoin pool is flagged; the only major-ticker side (SOL / USDC quote) sits at canonical mints."""
    parsed = [parse_pair(r, NOW) for r in DEX["pairs"]]
    assert all(p is not None and p["likely_fake"] is False for p in parsed)
    assert all(-0.03 < p["price_change_24h"] < 0 for p in parsed)  # all -1.58 .. -2.57 % in the sample
    assert all(p["age_days"] > 300 for p in parsed)


def test_parse_pair_without_now_keeps_created_but_no_age():
    p = parse_pair(DEX["pairs"][0])
    assert p["pair_created_at"] == "2024-10-18T06:09:47+00:00" and p["age_days"] is None
    # Creation in the future (clock skew) clamps the age to zero instead of going negative.
    early = datetime(2024, 10, 17, tzinfo=UTC)
    assert parse_pair(DEX["pairs"][0], early)["age_days"] == 0.0
    # Zero, negative or missing timestamps give neither.
    for created in (0, -5, None):
        raw = copy.deepcopy(DEX["pairs"][0])
        raw["pairCreatedAt"] = created
        q = parse_pair(raw, NOW)
        assert q["pair_created_at"] is None and q["age_days"] is None


def test_parse_pair_quote_side_impostor():
    """Real Fartcoin base, quote ticker SOL at a non-canonical mint: 2,000,000 liquidity, 10,000 volume -> 0.5 %."""
    raw = pair("Fartcoin", FART, "SOL", "5Sqq3NWD98JkcirdDBKRxZFsoZ8f1qqpFp6DSZkoTsHd", 2_000_000, 10_000)
    p = parse_pair(raw, NOW)
    assert p["likely_fake"] is True
    assert p["likely_fake_reason"].startswith("quote token: symbol SOL")
    assert "0.5000%" in p["likely_fake_reason"] and "$2,000,000" in p["likely_fake_reason"]


def test_parse_pair_base_side_impostor_has_no_prefix():
    raw = pair("SOL", "5Sqq3NWD98JkcirdDBKRxZFsoZ8f1qqpFp6DSZkoTsHd", "USDC", USDC_SOL, 2_314_314_671.08, 347_776.17)
    p = parse_pair(raw, NOW)
    # 347776.17 / 2314314671.08 = 0.01503 % of liquidity.
    assert p["likely_fake"] is True
    assert p["likely_fake_reason"].startswith("symbol SOL matches a major asset but 24h volume is 0.0150%")


def test_parse_pair_canonical_tokens_are_exempt_and_chain_case_ignored():
    # Quiet WETH / USDC pool on Ethereum (0.02 % turnover): both sides canonical, never flagged; chain id upper-case.
    raw = pair("WETH", WETH_ETH_CHECKSUM, "USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 5_000_000, 1_000, "Ethereum")
    p = parse_pair(raw, NOW)
    assert p["likely_fake"] is False and p["likely_fake_reason"] is None
    # The same WETH address on another chain is not canonical there, so the thin pool is flagged.
    raw["chainId"] = "base"
    assert parse_pair(raw, NOW)["likely_fake"] is True


def test_parse_pair_thresholds_are_strict():
    imp = "5Sqq3NWD98JkcirdDBKRxZFsoZ8f1qqpFp6DSZkoTsHd"
    # Liquidity exactly 1,000,000 is eligible; turnover 9,999 / 1,000,000 = 0.9999 % < 1 % -> flagged.
    assert parse_pair(pair("SOL", imp, "USDC", USDC_SOL, 1_000_000, 9_999))["likely_fake"] is True
    # Turnover exactly 1 % is not below the threshold.
    assert parse_pair(pair("SOL", imp, "USDC", USDC_SOL, 1_000_000, 10_000))["likely_fake"] is False
    # Liquidity just under 1,000,000 or unknown: never flagged.
    assert parse_pair(pair("SOL", imp, "USDC", USDC_SOL, 999_999.99, 0))["likely_fake"] is False
    assert parse_pair(pair("SOL", imp, "USDC", USDC_SOL, None, 0))["likely_fake"] is False
    # Unknown volume counts as zero turnover.
    assert parse_pair(pair("SOL", imp, "USDC", USDC_SOL, 2_000_000, None))["likely_fake"] is True


def test_parse_pair_zero_width_symbol_is_cleaned_then_checked():
    # "S<ZWSP>OL" looks like SOL; after stripping the format character it is checked as SOL and flagged.
    p = parse_pair(pair("S​OL\x07", "5Sqq3NWD98JkcirdDBKRxZFsoZ8f1qqpFp6DSZkoTsHd", "USDC", USDC_SOL, 3_000_000, 0))
    assert p["base_token"]["symbol"] == "SOL" and p["likely_fake"] is True


def test_parse_pair_malformed_input():
    for bad in (None, [], "pair", 42):
        assert parse_pair(bad, NOW) is None  # type: ignore[arg-type]
    # Wrong-typed sub-objects and non-finite / non-numeric values degrade to None, never raise.
    raw = {
        "chainId": "solana",
        "baseToken": "SOL",
        "quoteToken": None,
        "liquidity": [1, 2],
        "volume": {"h24": "NaN", "h6": "inf", "h1": "abc"},
        "priceChange": {"h24": "2.5"},
        "txns": {"h24": {"buys": "5", "sells": 7.0}},
        "priceUsd": "1e400",
        "pairCreatedAt": "1729231787000",
    }
    p = parse_pair(raw, NOW)
    assert p["base_token"] == {"address": "", "symbol": "", "name": ""}
    assert p["liquidity_usd"] is None and p["price_usd"] is None
    assert p["volume_24h_usd"] is None and p["volume_6h_usd"] is None and p["volume_1h_usd"] is None
    assert p["price_change_24h"] == pytest.approx(0.025)  # "2.5" percent as a string -> 0.025
    assert p["txns_24h_buys"] is None and p["txns_24h_sells"] is None  # only ints are counts
    assert p["pair_created_at"] == "2024-10-18T06:09:47+00:00"  # numeric string timestamp accepted
    assert p["likely_fake"] is False
