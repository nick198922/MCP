"""Pure helpers for the Etherscan V2 tools (tools/evm.py): EVM address check, decoding of eth_call results (uint256,
ABI string or bytes32), raw token amounts scaled by decimals, compact rows of an address's transactions and a summary of
a contract's verified source. No I/O.

Amounts stay exact as decimal strings in the smallest unit (`*_raw`, `*_wei`); the scaled float next to them is for
reading only (a float keeps ~15-16 significant digits). Every text field from the API (token names and symbols,
contract and function names, file names, source code) is untrusted third-party text: control and format characters are
dropped and the length is capped, but the content itself is not checked.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")  # always with fullmatch (`$` would let a trailing "\n" pass)
TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")
_HEX = re.compile(r"0x[0-9a-fA-F]*")
_DIGITS = re.compile(r"[0-9]{1,78}")  # uint256 has at most 78 decimal digits
MAX_UINT256 = 2**256 - 1
NATIVE_DECIMALS = 18  # ETH, BNB, POL and AVAX all use 18 decimals
NOT_VERIFIED_ABI = "Contract source code not verified"  # Etherscan's ABI field for an unverified address
MAX_SOURCE_FILES = 20


def valid_evm_address(address: Any) -> tuple[str | None, str | None]:
    """Lower-cased 0x + 40 hex address, or (None, error). The checksum case is not enforced (Etherscan ignores it)."""
    a = address.strip() if isinstance(address, str) else ""
    if not EVM_ADDRESS.fullmatch(a):
        return None, f"invalid EVM address: {display_text(a, 50) or ''!r} (expected 0x followed by 40 hex characters)"
    return a.lower(), None


def display_text(x: Any, n: int = 40) -> str | None:
    """Untrusted one-line display text: no control / format / private-use characters, whitespace collapsed, at most n
    characters; None when nothing is left."""
    if not isinstance(x, str):
        return None
    s = "".join(ch if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs") else " " for ch in x)
    s = " ".join(s.split())[:n]
    return s or None


def source_text(x: Any, n: int) -> str:
    """Untrusted multi-line text (source code): keeps newlines and tabs, drops other control / format characters,
    cut to n characters."""
    if not isinstance(x, str) or n <= 0:
        return ""
    return "".join(ch for ch in x[: n * 2] if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs"))[:n]


def uint_text(x: Any) -> int | None:
    """A non-negative integer from Etherscan's decimal strings ("40891626854930000000000"); None for anything else."""
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x if 0 <= x <= MAX_UINT256 else None
    if isinstance(x, str) and _DIGITS.fullmatch(x.strip()):
        v = int(x.strip())
        return v if v <= MAX_UINT256 else None
    return None


def hex_uint(x: Any) -> int | None:
    """A uint256 from an eth_call result ("0x" + 64 hex digits; shorter is accepted). "0x" (no code / no such function)
    and anything longer than one word is None."""
    if not isinstance(x, str) or not _HEX.fullmatch(x) or len(x) <= 2 or len(x) > 66:
        return None
    return int(x[2:], 16)


def abi_string(x: Any, n: int = 64) -> str | None:
    """A string returned by eth_call: the ABI-encoded `string` (offset, length, bytes) or a `bytes32` padded with zero
    bytes (older tokens such as MKR). Display text of at most n characters, or None when the result is not one of those."""
    if not isinstance(x, str) or not _HEX.fullmatch(x) or len(x) % 2:
        return None
    raw = bytes.fromhex(x[2:])
    if len(raw) == 32:
        data = raw.rstrip(b"\x00")
    elif len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset > len(raw) - 32:
            return None
        length = int.from_bytes(raw[offset : offset + 32], "big")
        if offset + 32 + length > len(raw):
            return None
        data = raw[offset + 32 : offset + 32 + length]
    else:
        return None
    return display_text(data.decode("utf-8", errors="replace"), n)


def scaled(raw: int | None, decimals: int | None) -> float | None:
    """raw / 10**decimals as a float (for reading; the exact amount is the raw integer), or None."""
    if raw is None or decimals is None or not 0 <= decimals <= 255:
        return None
    try:
        v = float(Decimal(raw).scaleb(-decimals))
    except (InvalidOperation, OverflowError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ts_iso(x: Any) -> str | None:
    ts = uint_text(x)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _addr(x: Any) -> str | None:
    return x.lower() if isinstance(x, str) and EVM_ADDRESS.fullmatch(x) else None


def _direction(frm: str | None, to: str | None, me: str) -> str | None:
    if frm == me and to == me:
        return "self"
    if frm == me:
        return "out"
    if to == me:
        return "in"
    return None


def _flag(x: Any) -> bool | None:
    return {"1": True, "0": False}.get(x.strip()) if isinstance(x, str) else None


def tx_row(row: Any, me: str, kind: str) -> dict[str, Any] | None:
    """One row of Etherscan `account/txlist` (kind 'normal') or `account/tokentx` (kind 'erc20'); None for a non-object.
    A field of the wrong type becomes None, the row is kept."""
    if not isinstance(row, dict):
        return None
    frm, to = _addr(row.get("from")), _addr(row.get("to"))
    h = row.get("hash")
    out: dict[str, Any] = {
        "hash": h.lower() if isinstance(h, str) and TX_HASH.fullmatch(h) else None,
        "block": uint_text(row.get("blockNumber")),
        "time_utc": _ts_iso(row.get("timeStamp")),
        "from": frm,
        "to": to,
        "direction": _direction(frm, to, me),
    }
    value = uint_text(row.get("value"))
    if kind == "erc20":
        dec = uint_text(row.get("tokenDecimal"))
        dec = dec if dec is not None and dec <= 255 else None
        out |= {
            "token": _addr(row.get("contractAddress")),
            "token_symbol": display_text(row.get("tokenSymbol"), 20),
            "token_name": display_text(row.get("tokenName"), 64),
            "token_decimals": dec,
            "value_raw": None if value is None else str(value),
            "value": scaled(value, dec),
        }
        return out
    gas_used, gas_price = uint_text(row.get("gasUsed")), uint_text(row.get("gasPrice"))
    fee = gas_used * gas_price if gas_used is not None and gas_price is not None else None
    fn = row.get("functionName")
    method = display_text(fn.split("(", 1)[0], 40) if isinstance(fn, str) else None
    method_id = row.get("methodId")
    out |= {
        "value_wei": None if value is None else str(value),
        "value": scaled(value, NATIVE_DECIMALS),
        "fee": scaled(fee, NATIVE_DECIMALS),
        "failed": _flag(row.get("isError")),
        "method": method or (method_id if isinstance(method_id, str) and re.fullmatch(r"0x[0-9a-fA-F]{8}", method_id) else None),
        "contract_created": _addr(row.get("contractAddress")),
    }
    return out


def tx_rows(rows: Any, me: str, kind: str) -> list[dict[str, Any]]:
    return [r for r in (tx_row(x, me, kind) for x in (rows if isinstance(rows, list) else [])) if r is not None]


def _source_files(code: str) -> tuple[str, dict[str, Any] | None]:
    """(format, files): Etherscan's SourceCode is one flattened file, a JSON map of files, or the solc standard-JSON
    input wrapped in an extra pair of braces ("{{...}}")."""
    s = code.strip()
    if not s.startswith("{"):
        return "single_file", None
    text, fmt = (s[1:-1], "standard_json") if s.startswith("{{") and s.endswith("}}") else (s, "multi_file_json")
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return "single_file", None
    if not isinstance(parsed, dict):
        return "single_file", None
    files = parsed.get("sources") if fmt == "standard_json" else parsed
    return (fmt, files) if isinstance(files, dict) else ("single_file", None)


def _main_source(files: dict[str, Any], contract_name: str | None) -> str:
    """The content of the file named after the contract (Foo.sol for Foo), else of the first file."""
    first = ""
    for path, entry in files.items():
        content = entry.get("content") if isinstance(entry, dict) else None
        if not isinstance(content, str):
            continue
        if contract_name and isinstance(path, str) and path.rsplit("/", 1)[-1] == f"{contract_name}.sol":
            return content
        first = first or content
    return first


def source_summary(item: Any, source_chars: int = 0) -> dict[str, Any] | None:
    """Summary of one `contract/getsourcecode` result item; None when it is not an object. The source itself is not
    returned, only its size, its files (at most MAX_SOURCE_FILES names) and, when source_chars > 0, the first
    source_chars characters of the main file."""
    if not isinstance(item, dict):
        return None
    code = item.get("SourceCode")
    code = code if isinstance(code, str) else ""
    abi = item.get("ABI")
    verified = bool(code.strip()) and abi != NOT_VERIFIED_ABI
    name = display_text(item.get("ContractName"), 80)
    out: dict[str, Any] = {
        "verified": verified,
        "contract_name": name,
        "compiler": display_text(item.get("CompilerVersion"), 80),
        "compiler_type": display_text(item.get("CompilerType"), 40),
        "optimization": _flag(item.get("OptimizationUsed")),
        "runs": uint_text(item.get("Runs")),
        "evm_version": display_text(item.get("EVMVersion"), 40),
        "license": display_text(item.get("LicenseType"), 40),
        "proxy": _flag(item.get("Proxy")),
        "implementation": _addr(item.get("Implementation")),
        "source_format": None,
        "source_files_count": 0,
        "source_files": [],
        "source_chars_total": len(code),
        "source_excerpt": None,
    }
    if not verified:
        return out
    fmt, files = _source_files(code)
    out["source_format"] = fmt
    if files is None:
        out["source_files_count"] = 1
        main = code
    else:
        out["source_files_count"] = len(files)
        out["source_files"] = [t for t in (display_text(p, 120) for p in list(files)[:MAX_SOURCE_FILES]) if t]
        main = _main_source(files, name)
    if source_chars > 0:
        out["source_excerpt"] = source_text(main, source_chars)
    return out
