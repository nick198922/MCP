"""Bitcoin network data from mempool.space answers (pure: no I/O, time passed in).

Units: fee rates in sat/vB (satoshis per virtual byte), amounts in satoshis and BTC (1 BTC = 100,000,000 sats), sizes in
vbytes (a block holds at most 4,000,000 weight units = 1,000,000 vB), hashrate in EH/s (1e18 hashes per second).

`valid_btc_address` checks a mainnet address (Base58Check P2PKH / P2SH, bech32 SegWit v0, bech32m Taproot and later
versions, BIP173 / BIP350) before any request: the address goes into a URL path, so nothing else may reach it.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from typing import Any

SATS_PER_BTC = 100_000_000
VBYTES_PER_BLOCK = 1_000_000  # 4M weight units / 4
HASHES_PER_EH = 1e18
# Lower edges (sat/vB) of the fee bands btc_mempool reports; the last band is everything under 1 sat/vB.
FEE_BANDS = (50.0, 20.0, 10.0, 5.0, 2.0, 1.0, 0.0)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3
_HASH_OK = re.compile(r"^[0-9a-f]{64}$")
_FEE_KEYS = (
    ("fastestFee", "fastest_sat_vb"),
    ("halfHourFee", "half_hour_sat_vb"),
    ("hourFee", "hour_sat_vb"),
    ("economyFee", "economy_sat_vb"),
    ("minimumFee", "minimum_sat_vb"),
)


# ---------------------------------------------------------------- address validation


def _b58check(addr: str) -> tuple[str | None, str | None]:
    if not 26 <= len(addr) <= 35:
        return None, "legacy address must be 26-35 characters"
    n = 0
    for ch in addr:
        i = _B58.find(ch)
        if i < 0:
            return None, "invalid Base58 character"
        n = n * 58 + i
    raw = b"\x00" * (len(addr) - len(addr.lstrip("1"))) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    if len(raw) != 25:
        return None, "legacy address does not decode to 25 bytes"
    payload, checksum = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        return None, "Base58Check checksum mismatch (typo?)"
    if payload[0] in (0x6F, 0xC4):
        return None, "testnet address; only Bitcoin mainnet is supported"
    if payload[0] not in (0x00, 0x05):
        return None, "not a Bitcoin mainnet P2PKH (1...) or P2SH (3...) address"
    return addr, None


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def _convertbits(data: list[int], frombits: int, tobits: int) -> list[int] | None:
    """BIP173 regrouping without padding; None when the leftover bits are not zero padding."""
    acc = bits = 0
    out: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for v in data:
        acc = ((acc << frombits) | v) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return out


def _segwit(addr: str) -> tuple[str | None, str | None]:
    """`addr` lower-case and starting with 'bc1'."""
    if not 14 <= len(addr) <= 90:
        return None, "bech32 address must be 14-90 characters"
    data = [_BECH32.find(c) for c in addr[3:]]
    if any(d < 0 for d in data):
        return None, "invalid bech32 character"
    hrp = [ord(c) >> 5 for c in "bc"] + [0] + [ord(c) & 31 for c in "bc"]
    const = _polymod(hrp + data)
    version = data[0]
    if version > 16:
        return None, "invalid witness version"
    if const != (_BECH32_CONST if version == 0 else _BECH32M_CONST):
        return None, "bech32 checksum mismatch (typo?)"
    prog = _convertbits(data[1:-6], 5, 8)
    if prog is None or not 2 <= len(prog) <= 40 or (version == 0 and len(prog) not in (20, 32)):
        return None, "invalid witness program length"
    return addr, None


def valid_btc_address(address: Any) -> tuple[str | None, str | None]:
    """(normalised address, None) for a valid Bitcoin mainnet address, else (None, why). Bech32 is returned lower-case
    (all-upper is valid, mixed case is not); Base58 is case-sensitive and returned as given (surrounding spaces cut)."""
    if not isinstance(address, str):
        return None, "address must be a string"
    a = address.strip()
    if not a:
        return None, "address is empty"
    if len(a) > 90:
        return None, "address is too long"
    low = a.lower()
    if low.startswith(("tb1", "bcrt1")):
        return None, "testnet / regtest address; only Bitcoin mainnet is supported"
    if low.startswith("bc1"):
        if a != low and a != a.upper():
            return None, "bech32 address mixes upper and lower case"
        return _segwit(low)
    if a[0] in "123mn":
        return _b58check(a)
    return None, "not a Bitcoin address (expected 1..., 3... or bc1...)"


# ---------------------------------------------------------------- number helpers


def _num(x: Any) -> float | None:
    """A finite non-negative JSON number as float, else None (bool, string, NaN, inf, beyond the float range)."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except OverflowError:
        return None
    return v if math.isfinite(v) and v >= 0 else None


def _int(x: Any) -> int | None:
    """A non-negative JSON integer (an integral float counts), else None. Capped at 2**63 so sums stay sane."""
    if isinstance(x, float) and math.isfinite(x) and x.is_integer():
        x = int(x)
    if not isinstance(x, int) or isinstance(x, bool) or not 0 <= x < 2**63:
        return None
    return x


def _btc(sats: int | None) -> float | None:
    return None if sats is None else sats / SATS_PER_BTC


def _text(x: Any, limit: int = 64) -> str | None:
    """Third-party text (pool name) cut to `limit` printable characters; None when empty or not a string."""
    if not isinstance(x, str):
        return None
    s = "".join(ch for ch in x if ch.isprintable())[:limit].strip()
    return s or None


def _iso(ts: int) -> str | None:
    try:
        return datetime.fromtimestamp(ts, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------- summaries


def fees_summary(body: Any) -> dict[str, Any] | None:
    """/api/v1/fees/recommended -> the five rates in sat/vB; None when none is a usable number."""
    if not isinstance(body, dict):
        return None
    out = {name: _num(body.get(key)) for key, name in _FEE_KEYS}
    return out if any(v is not None for v in out.values()) else None


def fee_bands(histogram: Any) -> list[dict[str, Any]]:
    """mempool.space `fee_histogram` ([[fee rate sat/vB, vsize vB], ...]) summed into FEE_BANDS; malformed rows skipped."""
    sums = [0] * len(FEE_BANDS)
    for row in histogram if isinstance(histogram, list) else []:
        if not isinstance(row, list) or len(row) != 2:
            continue
        rate, vsize = _num(row[0]), _int(row[1])
        if rate is None or vsize is None:
            continue
        sums[next(i for i, edge in enumerate(FEE_BANDS) if rate >= edge)] += vsize
    total = sum(sums)
    return [
        {
            "min_sat_vb": edge,
            "max_sat_vb": FEE_BANDS[i - 1] if i else None,
            "vsize_vb": v,
            "share": v / total if total else None,
        }
        for i, (edge, v) in enumerate(zip(FEE_BANDS, sums, strict=True))
    ]


def mempool_summary(body: Any) -> dict[str, Any] | None:
    """/api/mempool -> backlog size, fees and fee bands; None without a usable count and vsize."""
    if not isinstance(body, dict):
        return None
    count, vsize, fee = _int(body.get("count")), _int(body.get("vsize")), _int(body.get("total_fee"))
    if count is None or vsize is None:
        return None
    bands = fee_bands(body.get("fee_histogram"))
    return {
        "tx_count": count,
        "vsize_vb": vsize,
        "blocks_of_backlog": vsize / VBYTES_PER_BLOCK,
        "total_fee_sats": fee,
        "total_fee_btc": _btc(fee),
        "avg_fee_rate_sat_vb": fee / vsize if fee is not None and vsize > 0 else None,
        "fee_bands": bands,
        "histogram_vsize_vb": sum(b["vsize_vb"] for b in bands),
    }


def block_summary(block: Any, tip_height: int | None, now_ts: float) -> dict[str, Any] | None:
    """One /api/v1/blocks entry -> the latest-block fields; None without a usable height and timestamp."""
    if not isinstance(block, dict):
        return None
    height, ts = _int(block.get("height")), _int(block.get("timestamp"))
    if height is None or ts is None or _iso(ts) is None:
        return None
    ex = block.get("extras") if isinstance(block.get("extras"), dict) else {}
    weight = _int(block.get("weight"))
    vsize = _num(ex.get("virtualSize"))
    if vsize is None and weight is not None:
        vsize = weight / 4
    fee_range = ex.get("feeRange")
    rates = [v for v in (_num(x) for x in fee_range) if v is not None] if isinstance(fee_range, list) else []
    total_fees, reward = _int(ex.get("totalFees")), _int(ex.get("reward"))
    subsidy = reward - total_fees if reward is not None and total_fees is not None and reward >= total_fees else None
    pool = ex.get("pool")
    block_hash = block.get("id")
    return {
        "height": height,
        "hash": block_hash if isinstance(block_hash, str) and _HASH_OK.match(block_hash) else None,
        "timestamp": ts,
        "time_utc": _iso(ts),
        "age_seconds": now_ts - ts,
        "tip_height": tip_height,
        "tx_count": _int(block.get("tx_count")),
        "size_bytes": _int(block.get("size")),
        "weight_wu": weight,
        "vsize_vb": vsize,
        "fullness": vsize / VBYTES_PER_BLOCK if vsize is not None else None,
        "difficulty": _num(block.get("difficulty")),
        "median_fee_sat_vb": _num(ex.get("medianFee")),
        "min_fee_sat_vb": min(rates) if rates else None,
        "max_fee_sat_vb": max(rates) if rates else None,
        "total_fees_sats": total_fees,
        "total_fees_btc": _btc(total_fees),
        "reward_sats": reward,
        "reward_btc": _btc(reward),
        "subsidy_btc": _btc(subsidy),
        "pool": _text(pool.get("name")) if isinstance(pool, dict) else None,
    }


def hashrate_summary(body: Any) -> dict[str, Any] | None:
    """/api/v1/mining/hashrate/3d -> current difficulty, current hashrate and daily averages in EH/s."""
    if not isinstance(body, dict):
        return None
    cur_h, cur_d = _num(body.get("currentHashrate")), _num(body.get("currentDifficulty"))
    daily = []
    rows = body.get("hashrates")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        ts, h = _int(row.get("timestamp")), _num(row.get("avgHashrate"))
        if ts is None or h is None or _iso(ts) is None:
            continue
        daily.append({"date": _iso(ts)[:10], "avg_hashrate_ehs": h / HASHES_PER_EH})
    if cur_h is None and cur_d is None and not daily:
        return None
    return {
        "current_hashrate_ehs": cur_h / HASHES_PER_EH if cur_h is not None else None,
        "current_difficulty": cur_d,
        "current_difficulty_t": cur_d / 1e12 if cur_d is not None else None,
        "daily_avg_hashrate": daily,
    }


def _txo_side(stats: Any) -> dict[str, Any] | None:
    if not isinstance(stats, dict):
        return None
    funded, spent = _int(stats.get("funded_txo_sum")), _int(stats.get("spent_txo_sum"))
    if funded is None or spent is None:
        return None
    return {
        "funded_sats": funded,
        "spent_sats": spent,
        "net_sats": funded - spent,
        "net_btc": (funded - spent) / SATS_PER_BTC,
        "funded_txo_count": _int(stats.get("funded_txo_count")),
        "spent_txo_count": _int(stats.get("spent_txo_count")),
        "tx_count": _int(stats.get("tx_count")),
    }


def address_summary(body: Any) -> dict[str, Any] | None:
    """/api/address/{address} -> confirmed totals, unconfirmed (mempool) deltas and the balance in sats and BTC; None
    without usable confirmed stats."""
    if not isinstance(body, dict):
        return None
    confirmed = _txo_side(body.get("chain_stats"))
    if confirmed is None:
        return None
    unconfirmed = _txo_side(body.get("mempool_stats"))
    balance = confirmed["net_sats"] + (unconfirmed["net_sats"] if unconfirmed else 0)
    return {
        "confirmed_balance_sats": confirmed["net_sats"],
        "confirmed_balance_btc": confirmed["net_btc"],
        "balance_with_mempool_sats": balance,
        "balance_with_mempool_btc": balance / SATS_PER_BTC,
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
    }
