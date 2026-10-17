"""DEX pair parsing and the "likely fake" heuristic (pure: no I/O).

DexScreener `/latest/dex/tokens/{addresses}` returns every pool that trades the token(s). Anyone can
deploy a token named "SOL" or "ETH" and seed a pool, so symbol alone proves nothing; the heuristic flags
the typical scam pattern: a major-asset ticker whose reported liquidity is large but whose traded volume
is a tiny fraction of it (real majors turn their liquidity over many times a day).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

# Tickers (upper-case) that scam tokens imitate. Wrapped variants included.
MAJOR_SYMBOLS = frozenset(
    {
        "BTC",
        "WBTC",
        "ETH",
        "WETH",
        "SOL",
        "WSOL",
        "BNB",
        "WBNB",
        "XRP",
        "USDT",
        "USDC",
        "DAI",
        "DOGE",
        "ADA",
        "AVAX",
        "TRX",
        "TON",
        "LINK",
        "MATIC",
        "POL",
        "SUI",
        "HYPE",
    }  # fmt: skip
)
# Canonical contracts of major assets per DexScreener chain id. A token at one of these addresses is the real
# asset and is never flagged, however thin its pool's turnover (quiet stable/WETH pools are normal). EVM
# addresses are compared lower-case; Solana base58 is case-sensitive.
CANONICAL_TOKENS: dict[str, frozenset[str]] = {
    "solana": frozenset(
        {
            "So11111111111111111111111111111111111111112",  # wSOL
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
            "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  # WBTC (Wormhole)
            "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",  # cbBTC
            "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # WETH (Wormhole)
        }
    ),
    "ethereum": frozenset(
        {
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
            "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
            "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC
            "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        }
    ),
    "base": frozenset(
        {
            "0x4200000000000000000000000000000000000006",  # WETH
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
        }
    ),
    "bsc": frozenset(
        {
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
            "0x55d398326f99059ff775485246999027b3197955",  # USDT
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
            "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH
            "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",  # BTCB
        }
    ),
    "arbitrum": frozenset(
        {
            "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
            "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC
            "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
            "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f",  # WBTC
        }
    ),
}  # fmt: skip


def is_canonical(chain: str, address: str) -> bool:
    a = address.strip()
    if a.lower().startswith("0x"):
        a = a.lower()
    return a in CANONICAL_TOKENS.get(chain.strip().lower(), frozenset())


# A major-ticker pool with at least this much liquidity is implausible when 24h volume / liquidity is below
# MAX_TURNOVER: genuine majors turn their pool liquidity over many times a day and even deep stable pools
# rarely sit under 1 %. The thresholds are a judgement call, not calibrated on a saved sample; canonical
# addresses (above) are exempt. Only non-canonical tokens wearing a major ticker can be flagged.
MIN_LIQUIDITY_USD = 1_000_000.0
MAX_TURNOVER = 0.01

# ASCII controls plus Unicode format characters (category Cf: zero-width, bidi overrides, BOM), which scam
# tokens use to make a symbol look like "SOL" without matching it.
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_ADDR_OK = re.compile(r"^[A-Za-z0-9_:-]{1,100}$")  # no '.': EVM hex and base58 never contain it, so '..' cannot pass
MAX_ADDRESSES = 30  # DexScreener limit per request


def clean_text(x: Any, n: int = 40) -> str:
    """Untrusted display text: no control/format characters, trimmed, length-capped."""
    if not isinstance(x, str):
        return ""
    stripped = "".join(ch for ch in _CTRL.sub("", x) if unicodedata.category(ch) != "Cf")
    return stripped.strip()[:n]


def num(x: Any) -> float | None:
    """DexScreener sends some numbers as strings; return a finite float or None."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def valid_addresses(addresses: list[str]) -> tuple[list[str], str | None]:
    """Normalise the address list (strip, de-duplicate keeping order). Returns (addresses, error)."""
    out: list[str] = []
    for a in addresses:
        a = (a or "").strip()
        if not a:
            continue
        if not _ADDR_OK.match(a):
            return [], f"invalid token address: {clean_text(a, 30)!r}"
        if a not in out:
            out.append(a)
    if not out:
        return [], "at least one token address is required"
    if len(out) > MAX_ADDRESSES:
        return [], f"at most {MAX_ADDRESSES} addresses per call"
    return out, None


def likely_fake(
    symbol: str,
    liquidity_usd: float | None,
    volume_24h_usd: float | None,
    *,
    chain: str = "",
    address: str = "",
) -> tuple[bool, str | None]:
    """Flag a token whose symbol imitates a major asset but whose pool liquidity/volume ratio is implausible.

    Tokens at a canonical address (`CANONICAL_TOKENS`) are never flagged.
    """
    sym = symbol.strip().upper()
    if sym not in MAJOR_SYMBOLS:
        return False, None
    if address and is_canonical(chain, address):
        return False, None
    if liquidity_usd is None or liquidity_usd < MIN_LIQUIDITY_USD:
        return False, None
    vol = volume_24h_usd or 0.0
    turnover = vol / liquidity_usd
    if turnover < MAX_TURNOVER:
        return True, (
            f"symbol {sym} matches a major asset but 24h volume is {turnover:.4%} of ${liquidity_usd:,.0f} liquidity "
            f"(< {MAX_TURNOVER:.0%}); verify the token address"
        )
    return False, None


def parse_pair(raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """One DexScreener pair -> flat dict with numeric fields. Percent changes become decimals (0.05 = +5 %)."""
    if not isinstance(raw, dict):
        return None
    base = raw.get("baseToken") if isinstance(raw.get("baseToken"), dict) else {}
    quote = raw.get("quoteToken") if isinstance(raw.get("quoteToken"), dict) else {}
    liq = raw.get("liquidity") if isinstance(raw.get("liquidity"), dict) else {}
    vol = raw.get("volume") if isinstance(raw.get("volume"), dict) else {}
    chg = raw.get("priceChange") if isinstance(raw.get("priceChange"), dict) else {}
    txn = raw.get("txns") if isinstance(raw.get("txns"), dict) else {}
    t24 = txn.get("h24") if isinstance(txn.get("h24"), dict) else {}

    symbol = clean_text(base.get("symbol"), 20)
    liquidity_usd = num(liq.get("usd"))
    volume_24h = num(vol.get("h24"))
    chain_id = clean_text(raw.get("chainId"), 30)
    base_addr = clean_text(base.get("address"), 100)
    quote_addr = clean_text(quote.get("address"), 100)
    quote_symbol = clean_text(quote.get("symbol"), 20)
    # Either side can be the impostor (e.g. a pool X / fake "SOL"); checked against canonical addresses.
    fake, reason = likely_fake(symbol, liquidity_usd, volume_24h, chain=chain_id, address=base_addr)
    if not fake:
        fake, reason = likely_fake(quote_symbol, liquidity_usd, volume_24h, chain=chain_id, address=quote_addr)
        if fake:
            reason = f"quote token: {reason}"
    pct24 = num(chg.get("h24"))
    created_ms = num(raw.get("pairCreatedAt"))
    created = None
    age_days = None
    if created_ms is not None and created_ms > 0:
        created_dt = datetime.fromtimestamp(created_ms / 1000, tz=UTC)
        created = created_dt.isoformat(timespec="seconds")
        if now is not None:
            age_days = round(max((now - created_dt).total_seconds(), 0.0) / 86400, 2)
    return {
        "chain": clean_text(raw.get("chainId"), 30),
        "dex": clean_text(raw.get("dexId"), 30),
        "pair_address": clean_text(raw.get("pairAddress"), 100),
        "base_token": {"address": clean_text(base.get("address"), 100), "symbol": symbol, "name": clean_text(base.get("name"))},
        "quote_token": {
            "address": clean_text(quote.get("address"), 100),
            "symbol": clean_text(quote.get("symbol"), 20),
            "name": clean_text(quote.get("name")),
        },
        "price_usd": num(raw.get("priceUsd")),
        "price_native": num(raw.get("priceNative")),
        "liquidity_usd": liquidity_usd,
        "volume_24h_usd": volume_24h,
        "volume_6h_usd": num(vol.get("h6")),
        "volume_1h_usd": num(vol.get("h1")),
        "price_change_24h": None if pct24 is None else round(pct24 / 100.0, 6),
        "txns_24h_buys": t24.get("buys") if isinstance(t24.get("buys"), int) else None,
        "txns_24h_sells": t24.get("sells") if isinstance(t24.get("sells"), int) else None,
        "fdv_usd": num(raw.get("fdv")),
        "market_cap_usd": num(raw.get("marketCap")),
        "pair_created_at": created,
        "age_days": age_days,
        "url": clean_text(raw.get("url"), 200),
        "likely_fake": fake,
        "likely_fake_reason": reason,
    }


def summarise_pairs(rows: list[Any], *, chain: str | None = None, limit: int = 20, now: datetime | None = None) -> dict[str, Any]:
    """Parse, filter by chain id (case-insensitive), sort by liquidity (unknown last), cap at `limit`."""
    pairs = [p for p in (parse_pair(r, now) for r in rows) if p is not None]
    if chain:
        c = chain.strip().lower()
        pairs = [p for p in pairs if p["chain"].lower() == c]
    pairs.sort(key=lambda p: (p["liquidity_usd"] is None, -(p["liquidity_usd"] or 0.0)))
    total = len(pairs)
    return {
        "pairs_total": total,
        "pairs_returned": min(total, limit),
        "likely_fake_count": sum(1 for p in pairs if p["likely_fake"]),
        "pairs": pairs[:limit],
    }
