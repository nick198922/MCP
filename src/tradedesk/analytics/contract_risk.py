"""Token contract risk checks from a GoPlus token_security payload (pure: no I/O, time passed in).

Thresholds follow the DoubleTop course lesson «Інструменти для security» (docs/knowledge/defi.md, «Безпека
токена»): honeypot = never buy, buy/sell tax above 5 % = fail, closed source = fail, liquidity below 10 % of
market cap = fail; proxy, hidden owner, mintable, pausable, black/whitelist, owner-can-change-balance, tax
modifiable and not-renounced ownership are red flags. GoPlus's own extra flags are checked as well: cannot_buy
and selfdestruct as critical, transfer_tax with the same 5 % threshold, external_call, trading_cooldown,
anti_whale_modifiable, honeypot_with_same_creator (and is_airdrop_scam / fake_token when GoPlus returns them) as
red flags. The course gives no number for LP lock or holder concentration: LP_LOCKED_MIN, LP_LOCK_MIN_DAYS,
V2_LIQ_MIN and TOP10_MAX are our own judgement calls, not calibrated.

Every check is `pass`, `fail` or `unknown`. Missing or unparsable data is always `unknown`, never `pass`, and
the overall verdict is `pass` only when every check passed. A filter against -100 % outcomes, not a signal.

`cross_check` adds honeypot.is (a buy/sell simulation) as a second source for honeypot, cannot_sell_all, buy_tax
and sell_tax: the worse verdict wins, a disagreement is reported, and without a honeypot.is answer those four
checks cannot pass. TAX_GAP_MAX (2 percentage points) is our own threshold. honeypot.is's holder sell fail-rate
flags are a separate check of their own (holder_sell_fail_rate), with the source's own severity.

`assess_solana` reads GoPlus's Solana schema with the same thresholds and rules: the SPL token-program powers that
can block a sale or dilute holders (non-transferable, frozen by default, freeze / mint / close authority, permanent
delegate, transfer hook, transfer fee and the authorities that can change them) replace the EVM contract flags;
EVM-only checks are listed in `not_checked`, never invented. There is no second source and no sell simulation for
Solana, so its `honeypot` check is always unknown and the overall verdict is never pass.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .dex import clean_text, num

TAX_MAX = 0.05  # course: buy/sell tax <= 5 %
LIQ_MCAP_MIN = 0.10  # course: liquidity >= 10 % of market cap
LP_LOCKED_MIN = 0.95  # ours (the course says only "the more, the better")
LP_LOCK_MIN_DAYS = 30  # ours: a lock ending sooner than this counts as unlocked
V2_LIQ_MIN = 0.90  # ours: lp_locked can pass only if V2-style pools hold this share of the DEX liquidity
TOP10_MAX = 0.30  # ours (the course gives no number; top-N without clustering understates it)

EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")  # always used with fullmatch (`$` would let a trailing "\n" pass)
SOLANA_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")  # base58, fullmatch only
BURN_ADDRESSES = frozenset(
    {
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "0xdead000000000000000042069420694206942069",  # SHIB burn (GoPlus tag "Dead", live 2026-10-23)
    }
)
# GoPlus's own holder tags for burn addresses (matched only, never echoed). A tag counts only together with
# is_locked = 1: a vanity "0xdead..." prefix is cheap to generate, so the prefix alone proves nothing.
_BURN_TAGS = frozenset({"dead", "null address", "black hole", "burn"})
# GoPlus `dex[].liquidity_type` of pools whose LP is a fungible token (the one `lp_holders` describes); SushiSwap
# and other forks of Uniswap V2 are reported as "UniV2" too (live SHIB sample 2026-10-23).
_V2_TYPES = frozenset({"univ2"})
_NUMERIC = re.compile(r"\d+(\.\d+)?")

# DexScreener chain id -> GoPlus chain id (EVM only in this slice).
CHAINS: dict[str, str] = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "arbitrum": "42161",
    "polygon": "137",
    "optimism": "10",
    "avalanche": "43114",
}
SOLANA = "solana"  # GoPlus has a separate Solana endpoint and schema (assess_solana)
SUPPORTED = (*CHAINS, SOLANA)
_ALIASES = {"eth": "ethereum", "bnb": "bsc", "arb": "arbitrum", "matic": "polygon", "op": "optimism", "avax": "avalanche"}
_ALIASES |= {v: k for k, v in CHAINS.items()}  # numeric chain ids are accepted too
_ALIASES["sol"] = SOLANA


def resolve_chain(chain: str | None) -> tuple[str, str] | None:
    """('ethereum', '1') for 'ethereum', 'eth' or '1'; ('solana', 'solana') for 'solana' or 'sol'; None for
    unsupported chains."""
    c = (chain or "").strip().lower()
    c = _ALIASES.get(c, c)
    if c == SOLANA:
        return SOLANA, SOLANA
    return (c, CHAINS[c]) if c in CHAINS else None


def valid_evm_address(address: str | None) -> tuple[str | None, str | None]:
    """Lower-cased 0x + 40 hex address, or (None, error)."""
    a = (address or "").strip()
    if not EVM_ADDRESS.fullmatch(a):
        return None, f"invalid EVM token address: {clean_text(a, 50)!r} (expected 0x followed by 40 hex characters)"
    return a.lower(), None


def valid_solana_address(address: str | None) -> tuple[str | None, str | None]:
    """A base58 mint address (32-44 characters, case kept: base58 is case-sensitive), or (None, error)."""
    a = (address or "").strip()
    if not SOLANA_ADDRESS.fullmatch(a):
        return None, f"invalid Solana mint address: {clean_text(a, 50)!r} (expected 32-44 base58 characters)"
    return a, None


def _flag(v: Any) -> bool | None:
    """GoPlus 0/1 flag ("1", "0", 1, 0); anything else (empty string, missing) is unknown."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return {1: True, 0: False}.get(v)
    if isinstance(v, str):
        return {"1": True, "0": False}.get(v.strip())
    return None


def _dec(v: Any) -> float | None:
    if isinstance(v, str) and not v.strip():
        return None
    return num(v)


def _addr(v: Any) -> str | None:
    """A payload address, stripped and lower-cased, only if it is a well-formed EVM address (else not echoed)."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s.lower() if EVM_ADDRESS.fullmatch(s) else None


def _end_time(v: Any) -> datetime | None:
    """A lock end time: ISO 8601 string (naive = UTC) or unix seconds/milliseconds; None when unparsable."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float) or (isinstance(v, str) and _NUMERIC.fullmatch(v.strip())):
        ts = float(v)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _check(name: str, verdict: str, severity: str, value: Any, threshold: str, detail: str) -> dict[str, Any]:
    return {"name": name, "verdict": verdict, "severity": severity, "value": value, "threshold": threshold, "detail": detail}


def _flags_check(name: str, token: dict[str, Any], fields: tuple[str, ...], severity: str, bad: str) -> dict[str, Any]:
    """Fail if any field is 1; pass only if every field is present and 0; otherwise unknown."""
    vals = {f: _flag(token.get(f)) for f in fields}
    if any(v is True for v in vals.values()):
        verdict, detail = "fail", bad
    elif all(v is False for v in vals.values()):
        verdict, detail = "pass", "not flagged"
    else:
        verdict, detail = "unknown", "flag missing or empty in the GoPlus answer"
    return _check(name, verdict, severity, vals if len(fields) > 1 else vals[fields[0]], "must be 0", detail)


def _tax_check(name: str, raw: Any) -> dict[str, Any]:
    tax = _dec(raw)
    if tax is None or tax < 0:
        return _check(name, "unknown", "red_flag", None, f"<= {TAX_MAX}", "tax not reported")
    verdict = "fail" if tax > TAX_MAX else "pass"
    return _check(name, verdict, "red_flag", round(tax, 6), f"<= {TAX_MAX}", "decimal (0.05 = 5 %)")


def _ownership_check(token: dict[str, Any]) -> dict[str, Any]:
    raw = token.get("owner_address")
    owner = _addr(raw)
    take_back = _flag(token.get("can_take_back_ownership"))
    thr = "owner is the zero/dead address and can_take_back_ownership = 0"
    if take_back is True:
        return _check("ownership_renounced", "fail", "red_flag", owner, thr, "ownership can be taken back")
    if owner is None:
        return _check(
            "ownership_renounced", "unknown", "red_flag", None, thr, "owner address not reported (no owner function or not detected)"
        )
    if owner not in BURN_ADDRESSES:
        return _check("ownership_renounced", "fail", "red_flag", owner, thr, "contract has an active owner")
    if take_back is None:
        return _check("ownership_renounced", "unknown", "red_flag", owner, thr, "can_take_back_ownership not reported")
    return _check("ownership_renounced", "pass", "red_flag", owner, thr, "renounced")


def _holders(raw: Any) -> list[dict[str, Any]]:
    return [h for h in raw if isinstance(h, dict)] if isinstance(raw, list) else []


def _is_burn(h: dict[str, Any]) -> bool:
    if _addr(h.get("address")) in BURN_ADDRESSES:
        return True
    tag = h.get("tag")
    return isinstance(tag, str) and tag.strip().lower() in _BURN_TAGS and _flag(h.get("is_locked")) is True


def _lock_state(h: dict[str, Any], until: datetime) -> str:
    """'locked' (burned, or locked past `until`), 'expiring' (some lock ends by `until`), 'unverified'
    (is_locked = 1 but no parsable locked_detail end times) or 'unlocked'."""
    if _is_burn(h):
        return "locked"
    if _flag(h.get("is_locked")) is not True:
        return "unlocked"
    ends = [_end_time(d.get("end_time")) for d in _holders(h.get("locked_detail"))]
    if not ends or any(e is None for e in ends):
        return "unverified"
    return "locked" if all(e > until for e in ends if e is not None) else "expiring"


def _v2_liquidity_share(token: dict[str, Any]) -> float | None:
    """Share of the token's DEX liquidity (GoPlus `dex`) held by the single largest V2-style pool; None without
    usable data. GoPlus `lp_holders` describes one LP token (assumed the largest V2 pair, not verified), so the
    lock says nothing about liquidity split over several pools. With over 100 pairs the total is understated
    upstream (summarise_pairs limit), which only errs towards fail/unknown."""
    rows = _holders(token.get("dex"))
    total = v2 = 0.0
    for d in rows:
        liq = _dec(d.get("liquidity"))
        if liq is None or liq < 0:
            return None
        typ = d.get("liquidity_type")
        total += liq
        if isinstance(typ, str) and typ.strip().lower() in _V2_TYPES:
            v2 = max(v2, liq)
    return v2 / total if total > 0 else None


def _lp_check(token: dict[str, Any], now: datetime) -> dict[str, Any]:
    """GoPlus lists only the largest LP holders of the V2-style LP token, so the unlisted rest may or may not be
    locked: pass if the listed share that is burned or locked for more than LP_LOCK_MIN_DAYS reaches the threshold
    and V2-style pools hold at least V2_LIQ_MIN of the DEX liquidity; fail if the threshold cannot be reached
    even with every unlisted or unverified share locked; otherwise unknown."""
    thr = (
        f">= {LP_LOCKED_MIN} of LP supply burned or locked for > {LP_LOCK_MIN_DAYS} days, "
        f"with V2-style pools >= {V2_LIQ_MIN} of DEX liquidity (our thresholds)"
    )
    rows = _holders(token.get("lp_holders"))
    if not rows:
        return _check("lp_locked", "unknown", "red_flag", None, thr, "no LP holder data (e.g. only V3/V4 pools)")
    if now.tzinfo is None:  # end times are aware; a naive clock is taken as UTC
        now = now.replace(tzinfo=UTC)
    until = now + timedelta(days=LP_LOCK_MIN_DAYS)
    locked = listed = unverified = expiring = 0.0
    for h in rows:
        share = _dec(h.get("percent"))
        if share is None or share < 0:
            continue
        listed += share
        state = _lock_state(h, until)
        if state == "locked":
            locked += share
        elif state == "unverified":
            unverified += share
        elif state == "expiring":
            expiring += share
    if listed > 1 + 1e-6:
        return _check("lp_locked", "unknown", "red_flag", None, thr, "LP holder shares sum above 1 (inconsistent data)")
    best_case = locked + unverified + max(0.0, 1.0 - listed)
    verdict = "pass" if locked >= LP_LOCKED_MIN else ("fail" if best_case < LP_LOCKED_MIN else "unknown")
    v2 = _v2_liquidity_share(token)
    v2_txt = "unknown" if v2 is None else f"{v2:.4f}"
    detail = (
        f"burned or locked (> {LP_LOCK_MIN_DAYS} days) share of the V2-style LP token among the {len(rows)} listed "
        f"LP holders (listed holders own {listed:.4f}; lock ending within {LP_LOCK_MIN_DAYS} days "
        f"{expiring:.4f}; locked without a readable end time {unverified:.4f}); largest V2-style pool holds "
        f"{v2_txt} of DEX liquidity"
    )
    if verdict == "pass" and (v2 is None or v2 < V2_LIQ_MIN):
        verdict = "unknown"
        detail += (
            "; liquidity split across several pools or mainly in V3/V4 (concentrated) positions, or pool liquidity "
            "not reported; GoPlus LP holders describe one LP token, so the lock of most liquidity is not visible"
        )
    return _check("lp_locked", verdict, "red_flag", round(locked, 6), thr, detail)


_TOP10_THR = f"<= {TOP10_MAX} of non-burned supply (our threshold)"


def _top10_check(token: dict[str, Any]) -> dict[str, Any]:
    rows = _holders(token.get("holders"))
    if not rows:
        return _check("top10_holders", "unknown", "red_flag", None, _TOP10_THR, "no holder data")
    pools = {_addr(d.get("pair")) for d in _holders(token.get("dex"))} - {None}
    return _top10_verdict([(_dec(h.get("percent")), _is_burn(h), _addr(h.get("address")) in pools) for h in rows], "DEX pools")


def _top10_verdict(rows: list[tuple[float | None, bool, bool]], pools_note: str) -> dict[str, Any]:
    """top10_holders from (share, is_burn, is_dex_pool) per listed holder; `pools_note` names what was excluded."""
    thr = _TOP10_THR
    burned = 0.0
    shares = []
    skipped = 0
    for share, burn, pool in rows:
        if share is None or share < 0:
            skipped += 1
            continue
        if burn:
            burned += share
        elif not pool:
            shares.append(share)
    if not shares:
        return _check(
            "top10_holders",
            "unknown",
            "red_flag",
            None,
            thr,
            "no listed holder with a usable share after excluding burn and DEX pool addresses",
        )
    live = 1.0 - min(burned, 1.0)
    if live <= 0:
        return _check("top10_holders", "unknown", "red_flag", None, thr, "listed burned share is 100 %")
    top = round(sum(sorted(shares, reverse=True)[:10]) / live, 6)
    detail = (
        f"10 largest listed holders as a share of non-burned supply (burned {burned:.4f} excluded, {pools_note} excluded); "
        "wallets are not clustered and exchange wallets are not separated"
    )
    if top > TOP10_MAX:
        verdict = "fail"  # skipped rows can only raise the true figure
    elif skipped:
        verdict = "unknown"
        detail += f"; {skipped} listed holder(s) without a usable share, so a pass cannot be confirmed"
    else:
        verdict = "pass"
    return _check("top10_holders", verdict, "red_flag", top, thr, detail)


def _liquidity_check(liquidity_usd: float | None, market_cap_usd: float | None) -> dict[str, Any]:
    thr = f">= {LIQ_MCAP_MIN}"
    if liquidity_usd is None or market_cap_usd is None or market_cap_usd <= 0:
        return _check("liquidity_to_mcap", "unknown", "red_flag", None, thr, "liquidity or market cap not available")
    ratio = liquidity_usd / market_cap_usd
    verdict = "pass" if ratio >= LIQ_MCAP_MIN else "fail"
    detail = (
        "DEX pool liquidity on this chain / market cap (DexScreener), likely-fake pairs excluded; "
        "tokens that trade mainly on CEX fail it by design"
    )
    return _check("liquidity_to_mcap", verdict, "red_flag", round(ratio, 6), thr, detail)


def liquidity_from_pairs(pairs: list[dict[str, Any]], address: str) -> tuple[float | None, float | None, int]:
    """(total liquidity USD, market cap USD, likely-fake pairs excluded) over parsed DexScreener pairs where
    `address` is the base token.

    Pairs flagged `likely_fake` (fake-ticker pools) are left out of both numbers: their liquidity and price are
    not real. Market cap is the largest one reported (the same token, so they should agree); None when nothing
    is reported.
    """
    a = address.lower()
    mine = [p for p in pairs if (p.get("base_token") or {}).get("address", "").lower() == a]
    real = [p for p in mine if not p.get("likely_fake")]
    liqs = [p["liquidity_usd"] for p in real if p.get("liquidity_usd") is not None]
    caps = [p["market_cap_usd"] for p in real if p.get("market_cap_usd") is not None]
    return (sum(liqs) if liqs else None), (max(caps) if caps else None), len(mine) - len(real)


def _fake_token_value(v: Any) -> Any:
    """GoPlus `fake_token` is an object {"true_token_address": ..., "value": 1} (or a bare flag)."""
    return v.get("value") if isinstance(v, dict) else v


def _summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [c["name"] for c in checks if c["verdict"] == "fail"]
    unknown = [c["name"] for c in checks if c["verdict"] == "unknown"]
    return {
        "overall": "fail" if failed else ("unknown" if unknown else "pass"),
        "critical_failed": [c["name"] for c in checks if c["verdict"] == "fail" and c["severity"] == "critical"],
        "failed": failed,
        "unknown": unknown,
        "passed_count": sum(1 for c in checks if c["verdict"] == "pass"),
    }


def assess(
    token: dict[str, Any], *, now: datetime, liquidity_usd: float | None = None, market_cap_usd: float | None = None
) -> dict[str, Any]:
    """Per-check verdicts and an overall verdict for one GoPlus token_security entry; `now` dates the LP locks."""
    checks = [
        _flags_check("honeypot", token, ("is_honeypot",), "critical", "GoPlus flags the token as a honeypot"),
        _flags_check("cannot_sell_all", token, ("cannot_sell_all",), "critical", "holders cannot sell their whole balance"),
        _flags_check("cannot_buy", token, ("cannot_buy",), "critical", "the token cannot be bought"),
        _flags_check("selfdestruct", token, ("selfdestruct",), "critical", "the contract can self-destruct"),
        _tax_check("buy_tax", token.get("buy_tax")),
        _tax_check("sell_tax", token.get("sell_tax")),
        _tax_check("transfer_tax", token.get("transfer_tax")),
        _flags_check(
            "tax_modifiable",
            token,
            ("slippage_modifiable", "personal_slippage_modifiable"),
            "red_flag",
            "the owner can change the tax (even a 0 % tax can be raised)",
        ),
    ]
    src = _flag(token.get("is_open_source"))
    src_verdict = {True: "pass", False: "fail", None: "unknown"}[src]
    checks.append(_check("open_source", src_verdict, "critical", src, "must be 1", "verified source code"))
    for name, field, bad in (
        ("proxy", "is_proxy", "upgradeable proxy: the logic can be replaced"),
        ("hidden_owner", "hidden_owner", "hidden owner"),
        ("mintable", "is_mintable", "new tokens can be minted"),
        ("pausable", "transfer_pausable", "transfers can be paused"),
        ("blacklist", "is_blacklisted", "addresses can be blacklisted"),
        ("whitelist", "is_whitelisted", "trading can be limited to a whitelist"),
        ("owner_change_balance", "owner_change_balance", "the owner can change balances"),
        ("external_call", "external_call", "the contract calls external contracts, so its behaviour can change"),
        ("trading_cooldown", "trading_cooldown", "trades are limited by a cooldown"),
        ("anti_whale_modifiable", "anti_whale_modifiable", "the owner can change the max trade / max holding limits"),
        ("same_creator_honeypot", "honeypot_with_same_creator", "the creator has deployed honeypots before"),
    ):
        checks.append(_flags_check(name, token, (field,), "red_flag", bad))
    # GoPlus returns these only for some tokens: checked when present (absent is not counted as unknown).
    if "is_airdrop_scam" in token:
        checks.append(_flags_check("airdrop_scam", token, ("is_airdrop_scam",), "red_flag", "GoPlus flags an airdrop scam"))
    if "fake_token" in token:
        fake = {"fake_token": _fake_token_value(token.get("fake_token"))}
        checks.append(_flags_check("fake_token", fake, ("fake_token",), "red_flag", "GoPlus flags a fake copy of another token"))
    checks += [
        _ownership_check(token),
        _lp_check(token, now),
        _top10_check(token),
        _liquidity_check(liquidity_usd, market_cap_usd),
    ]

    holders = _dec(token.get("holder_count"))
    cex = token.get("is_in_cex")
    return {
        "token": {
            "name": clean_text(token.get("token_name"), 40),
            "symbol": clean_text(token.get("token_symbol"), 20),
        },
        **_summary(checks),
        "checks": checks,
        "info": {
            "holder_count": None if holders is None else int(holders),
            "owner_address": _addr(token.get("owner_address")),
            "creator_address": _addr(token.get("creator_address")),
            "creator_share": _dec(token.get("creator_percent")),
            "owner_share": _dec(token.get("owner_percent")),
            "in_cex": _flag(cex.get("listed")) if isinstance(cex, dict) else None,
            "liquidity_usd": None if liquidity_usd is None else round(liquidity_usd, 2),
            "market_cap_usd": None if market_cap_usd is None else round(market_cap_usd, 2),
        },
    }


# ---------- Solana: GoPlus solana/token_security ----------

# Burn sink for SPL tokens (Solana burns usually reduce supply instead, so they never show up as a holder).
SOL_BURN_ADDRESSES = frozenset({"1nc1nerator11111111111111111111111111111111"})
# Pool authorities that own the token vaults of many pools at once, so a pool's id never appears as the holder:
# Raydium AMM v4 and Raydium CPMM (both are top wSOL holders in the live sample of 2026-11-19).
SOL_DEX_AUTHORITIES = frozenset({"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"})
# SPL AccountState enum: 1 = Initialized, 2 = Frozen (GoPlus sends the number as a string; "1" live 2026-11-19).
_ACCOUNT_STATE = {"1": "initialized", "2": "frozen"}
# EVM checks without a field in the GoPlus Solana schema: left out of `checks` and listed here, never invented.
SOLANA_NOT_CHECKED: dict[str, str] = {
    **dict.fromkeys(
        ("cannot_sell_all", "cannot_buy", "holder_sell_fail_rate"),
        "no buy/sell simulation for Solana (honeypot.is covers ethereum, bsc and base only; GoPlus Solana has no "
        "such flag); the always-unknown honeypot check stands for them",
    ),
    **dict.fromkeys(
        ("buy_tax", "sell_tax"), "SPL tokens have no buy/sell tax; a Token-2022 transfer fee (transfer_tax) hits every transfer"
    ),
    **dict.fromkeys(
        ("open_source", "proxy", "selfdestruct", "external_call"),
        "SPL mints run on the shared token programs, there is no per-token contract code",
    ),
    **dict.fromkeys(
        ("hidden_owner", "ownership_renounced"), "Solana tokens have authorities instead of an owner (see the authority checks)"
    ),
    "pausable": "Token-2022 Pausable extension (a pause authority stops every transfer): no field in the GoPlus Solana schema, not checked",
    **dict.fromkeys(("blacklist", "whitelist"), "covered by freezable and default_account_frozen"),
    **dict.fromkeys(("trading_cooldown", "anti_whale_modifiable"), "no such field in the GoPlus Solana schema"),
    "same_creator_honeypot": "GoPlus Solana has no creator honeypot history; its per-address malicious_address flag "
    "(authorities, creators) is not read yet",
}
SOL_HONEYPOT_DETAIL = (
    "no buy/sell simulation for Solana (honeypot.is does not cover it, GoPlus Solana has no honeypot flag); "
    "token-program powers are checked separately, Token-2022 pause and pool-program blocks are not visible"
)


def _sol_addr(v: Any) -> str | None:
    """A payload base58 address, stripped, only if well-formed (else not echoed or matched)."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s if SOLANA_ADDRESS.fullmatch(s) else None


def _status(v: Any) -> Any:
    """`status` of a GoPlus Solana authority object {"authority": [...], "status": "0"/"1"}; None otherwise."""
    return v.get("status") if isinstance(v, dict) else None


def _sol_transfer_tax(token: dict[str, Any]) -> dict[str, Any]:
    """Token-2022 transfer fee as a decimal (GoPlus fee_rate = basis points / 10 000: BERN 0.0269, live 2026-11-19).
    The worse of the current and every scheduled rate counts (GoPlus `current_fee_rate` can be the older one once
    the scheduled epoch has passed). `{}` = no transfer-fee extension = 0."""
    thr = f"<= {TAX_MAX}"
    tf = token.get("transfer_fee")
    if not isinstance(tf, dict):
        return _check("transfer_tax", "unknown", "red_flag", None, thr, "transfer fee not reported")
    if not tf:
        return _check("transfer_tax", "pass", "red_flag", 0.0, thr, "no Token-2022 transfer-fee extension")
    cur = tf.get("current_fee_rate")
    rates = [_dec(cur.get("fee_rate")) if isinstance(cur, dict) else None]
    sched = tf.get("scheduled_fee_rate")
    if isinstance(sched, list):
        rates += [_dec(s.get("fee_rate")) if isinstance(s, dict) else None for s in sched]
    elif sched is not None:
        rates.append(None)
    known = [r for r in rates if r is not None and r >= 0]
    detail = "Token-2022 transfer fee on every transfer, decimal (0.05 = 5 %), worse of current and scheduled rates"
    if known and max(known) > TAX_MAX:
        return _check("transfer_tax", "fail", "red_flag", round(max(known), 6), thr, detail)
    if not known or len(known) < len(rates):
        return _check("transfer_tax", "unknown", "red_flag", None, thr, "transfer fee rate missing or unparsable")
    return _check("transfer_tax", "pass", "red_flag", round(max(known), 6), thr, detail)


def _sol_default_frozen(token: dict[str, Any]) -> dict[str, Any]:
    raw = token.get("default_account_state")
    key = raw.strip() if isinstance(raw, str) else (str(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None)
    state = _ACCOUNT_STATE.get(key or "")
    verdict = {"initialized": "pass", "frozen": "fail"}.get(state or "", "unknown")
    detail = {
        "pass": "new token accounts start usable",
        "fail": "new token accounts start frozen: a buyer cannot move or sell until the freeze authority thaws them",
        "unknown": "default account state missing or not recognised",
    }[verdict]
    return _check("default_account_frozen", verdict, "critical", state, "must be initialized (SPL AccountState 1)", detail)


def _sol_transfer_hook(token: dict[str, Any]) -> dict[str, Any]:
    raw = token.get("transfer_hook")
    thr = "no transfer hook program"
    if not isinstance(raw, list):
        return _check("transfer_hook", "unknown", "red_flag", None, thr, "transfer hook not reported")
    if raw:
        return _check("transfer_hook", "fail", "red_flag", len(raw), thr, "a custom program runs on every transfer and can block it")
    return _check("transfer_hook", "pass", "red_flag", 0, thr, "no transfer hook")


def _sol_lp_check(token: dict[str, Any], liquidity_usd: float | None) -> dict[str, Any]:
    """Liquidity-weighted burned/locked share of the pools GoPlus lists (`dex[].burn_percent`, percent 0-100, e.g.
    100 on a BERN pool live 2026-11-19; null = not reported). Liquidity outside the listed pools (DexScreener
    liquidity above the listed TVL) and pools without burn_percent count as unknown: pass needs the known locked
    share to reach LP_LOCKED_MIN, fail needs even the best case (all unknown liquidity locked) to stay below it."""
    thr = f">= {LP_LOCKED_MIN} of pool liquidity (TVL-weighted) burned or locked per GoPlus burn_percent (our threshold)"
    rows = _holders(token.get("dex"))
    if not rows:
        return _check("lp_locked", "unknown", "red_flag", None, thr, "no pool data")
    listed = locked = no_burn = 0.0
    for d in rows:
        tvl = _dec(d.get("tvl"))
        if tvl is None or tvl < 0:
            return _check("lp_locked", "unknown", "red_flag", None, thr, "pool TVL not reported")
        bp = _dec(d.get("burn_percent"))
        if bp is not None and not 0 <= bp <= 100:
            return _check("lp_locked", "unknown", "red_flag", None, thr, "burn_percent outside 0-100 (inconsistent data)")
        listed += tvl
        if bp is None:
            no_burn += tvl
        else:
            locked += tvl * bp / 100.0
    whole = max(listed, liquidity_usd or 0.0)
    if whole <= 0:
        return _check("lp_locked", "unknown", "red_flag", None, thr, "pools report no liquidity")
    share = locked / whole
    best = (whole - listed + locked + no_burn) / whole
    verdict = "pass" if share >= LP_LOCKED_MIN else ("fail" if best < LP_LOCKED_MIN else "unknown")
    detail = (
        f"burned or locked share of pool liquidity over the {len(rows)} pools GoPlus lists (TVL {listed:.2f} USD; "
        f"without burn_percent {no_burn / whole:.4f}; outside the listed pools {(whole - listed) / whole:.4f} of "
        f"{whole:.2f} USD); lock end times are not reported for Solana pools"
    )
    return _check("lp_locked", verdict, "red_flag", round(share, 6), thr, detail)


def _sol_top10_check(token: dict[str, Any]) -> dict[str, Any]:
    rows = _holders(token.get("holders"))
    if not rows:
        return _check("top10_holders", "unknown", "red_flag", None, _TOP10_THR, "no holder data")
    supply = _dec(token.get("total_supply"))
    if supply is None or supply <= 0:  # wSOL live 2026-11-19: total_supply "0" and every share "0"
        return _check("top10_holders", "unknown", "red_flag", None, _TOP10_THR, "total supply not reported, so holder shares are unusable")
    pools = ({_sol_addr(d.get("id")) for d in _holders(token.get("dex"))} - {None}) | SOL_DEX_AUTHORITIES

    def burn(h: dict[str, Any]) -> bool:
        if _sol_addr(h.get("account")) in SOL_BURN_ADDRESSES:
            return True
        tag = h.get("tag")
        return isinstance(tag, str) and tag.strip().lower() in _BURN_TAGS and _flag(h.get("is_locked")) is True

    entries = [(_dec(h.get("percent")), burn(h), _sol_addr(h.get("account")) in pools) for h in rows]
    if not any(s is not None and s > 0 for s, _, _ in entries):  # positive supply but every listed share 0 or missing
        return _check("top10_holders", "unknown", "red_flag", None, _TOP10_THR, "every listed holder share is zero or missing")
    return _top10_verdict(entries, "DEX pools and Raydium pool authorities")


def assess_solana(token: dict[str, Any], *, liquidity_usd: float | None = None, market_cap_usd: float | None = None) -> dict[str, Any]:
    """Per-check verdicts for one GoPlus solana/token_security entry, with the EVM thresholds and the same
    pass / fail / unknown rules (missing = unknown, never pass). There is no buy/sell simulation, so `honeypot` is
    always unknown (critical) and `overall` is never pass; the token-program powers that can block a sale
    (non-transferable, frozen by default, freeze authority, transfer hook, permanent delegate) are checked
    separately; EVM-only checks are listed in `not_checked`."""
    flat = {
        f: _status(token.get(f))
        for f in (
            "freezable",
            "mintable",
            "closable",
            "balance_mutable_authority",
            "transfer_fee_upgradable",
            "transfer_hook_upgradable",
            "default_account_state_upgradable",
        )
    } | {"non_transferable": token.get("non_transferable")}
    checks = [
        # Always unknown: without a sell simulation `overall` can never be pass on Solana.
        _check("honeypot", "unknown", "critical", None, "buy and sell simulation must succeed", SOL_HONEYPOT_DETAIL),
        _flags_check("non_transferable", flat, ("non_transferable",), "critical", "the token cannot be transferred, so it cannot be sold"),
        _sol_default_frozen(token),
        _flags_check(
            "freezable",
            flat,
            ("freezable",),
            "red_flag",
            "freeze authority set: holders' token accounts can be frozen, which blocks selling (the usual Solana honeypot)",
        ),
        _sol_transfer_hook(token),
        _sol_transfer_tax(token),
        _flags_check("tax_modifiable", flat, ("transfer_fee_upgradable",), "red_flag", "an authority can change the transfer fee"),
        _flags_check("mintable", flat, ("mintable",), "red_flag", "mint authority set: new tokens can be minted"),
        _flags_check(
            "owner_change_balance",
            flat,
            ("balance_mutable_authority",),
            "red_flag",
            "a permanent delegate can move or burn tokens from any account",
        ),
        _flags_check("closable", flat, ("closable",), "red_flag", "mint close authority set"),
        _flags_check(
            "upgradable",
            flat,
            ("transfer_hook_upgradable", "default_account_state_upgradable"),
            "red_flag",
            "an authority can set or change a transfer hook or make new token accounts frozen by default",
        ),
        _sol_lp_check(token, liquidity_usd),
        _sol_top10_check(token),
        _liquidity_check(liquidity_usd, market_cap_usd),
    ]
    meta = token.get("metadata") if isinstance(token.get("metadata"), dict) else {}
    holders = _dec(token.get("holder_count"))
    supply = _dec(token.get("total_supply"))
    meta_mut = token.get("metadata_mutable")
    return {
        "token": {"name": clean_text(meta.get("name"), 40), "symbol": clean_text(meta.get("symbol"), 20)},
        **_summary(checks),
        "checks": checks,
        "not_checked": dict(SOLANA_NOT_CHECKED),
        "info": {
            "holder_count": None if holders is None else int(holders),
            "total_supply": supply,
            "metadata_mutable": _flag(_status(meta_mut)),
            "goplus_trusted_token": _flag(token.get("trusted_token")),
            "liquidity_usd": None if liquidity_usd is None else round(liquidity_usd, 2),
            "market_cap_usd": None if market_cap_usd is None else round(market_cap_usd, 2),
        },
        "second_source": {"provider": "honeypot.is", "available": False, "disagreements": []},
    }


# ---------- second source: honeypot.is buy/sell simulation ----------

CROSS_CHECKED = ("honeypot", "cannot_sell_all", "buy_tax", "sell_tax")
TAX_GAP_MAX = 0.02  # ours: taxes from the two sources disagree when they differ by more than 2 percentage points
# honeypot.is flags that say many holders could not sell ("A (very) high amount of users cannot sell their tokens.",
# live 2026-11-19 for SafeMoon and EverGrow on BSC); matched only, never echoed beyond the flag name. They feed
# only holder_sell_fail_rate, not cannot_sell_all (a different property; see _fail_rate_check).
_SELL_FAIL_FLAGS = frozenset({"high_fail_rate", "medium_fail_rate"})
_RANK = {"pass": 0, "unknown": 1, "fail": 2}


def _flag_names(raw: Any, key: str | None) -> list[str] | None:
    """Flag names from a honeypot.is flags list: plain strings (top-level `flags`, key=None) or objects with a string
    `key` (`summary.flags[].flag`). None when it is not a list or any entry has another shape."""
    if not isinstance(raw, list):
        return None
    names = []
    for x in raw[:20]:
        name = x if key is None else (x.get(key) if isinstance(x, dict) else None)
        if not isinstance(name, str):
            return None
        names.append(clean_text(name, 40))
    return [n for n in names if n]


def parse_honeypot_is(body: Any) -> dict[str, Any]:
    """Normalised honeypot.is IsHoneypot answer. Taxes become decimals (the API sends percent: 0.3 = 0.3 %) and are
    read only after a successful simulation; anything missing or of the wrong type is None. Free text
    (honeypotReason, simulationError, flag descriptions) is dropped. `flags` joins the top-level list of strings and
    the names in `summary.flags[].flag`; when the top-level list is missing or of another shape it is None (no
    fail-rate pass), unless `summary.flags` names a fail-rate flag, which is then kept alone so it still fails."""
    b = body if isinstance(body, dict) else {}
    res = b.get("honeypotResult") if isinstance(b.get("honeypotResult"), dict) else {}
    sim = b.get("simulationResult") if isinstance(b.get("simulationResult"), dict) else {}
    summary = b.get("summary") if isinstance(b.get("summary"), dict) else {}
    ok = b.get("simulationSuccess")
    ok = ok if isinstance(ok, bool) else None
    is_hp = res.get("isHoneypot")
    level = summary.get("riskLevel")

    def tax(key: str) -> float | None:
        v = num(sim.get(key)) if ok is True else None
        return None if v is None or v < 0 else round(v / 100.0, 6)

    top = _flag_names(b.get("flags"), None)
    names = list(dict.fromkeys((top or []) + (_flag_names(summary.get("flags"), "flag") or [])))
    flags = names if top is not None else ([n for n in names if n in _SELL_FAIL_FLAGS] or None)
    return {
        "simulation_success": ok,
        "is_honeypot": is_hp if isinstance(is_hp, bool) else None,
        "buy_tax": tax("buyTax"),
        "sell_tax": tax("sellTax"),
        "risk": clean_text(summary.get("risk"), 20) or None,
        "risk_level": level if isinstance(level, int) and not isinstance(level, bool) else None,
        "flags": flags,
    }


def _second_verdicts(hp: dict[str, Any]) -> dict[str, tuple[str, Any, str]]:
    """honeypot.is (verdict, value, detail) for each CROSS_CHECKED name. A pass needs a successful simulation.
    cannot_sell_all rests only on the simulated sell (isHoneypot); the fail-rate flags are a separate check."""
    sim_ok = hp.get("simulation_success") is True
    is_hp = hp.get("is_honeypot")
    if is_hp is True:
        honeypot = ("fail", True, "honeypot.is buy/sell simulation: the sell failed (isHoneypot = true)")
    elif hp.get("risk") == "honeypot":
        honeypot = ("fail", True, "honeypot.is rates the token risk as 'honeypot'")
    elif is_hp is False and sim_ok:
        honeypot = ("pass", False, "honeypot.is buy/sell simulation: buy and sell went through")
    else:
        honeypot = ("unknown", is_hp, "honeypot.is simulation failed or gave no honeypot result")
    out = {"honeypot": honeypot, "cannot_sell_all": honeypot}
    for name in ("buy_tax", "sell_tax"):
        v = hp.get(name)
        if v is None:
            out[name] = ("unknown", None, "honeypot.is simulated tax not available")
        else:
            out[name] = ("fail" if v > TAX_MAX else "pass", v, "honeypot.is simulated tax, decimal (0.05 = 5 %)")
    return out


def _fail_rate_check(hp: dict[str, Any] | None) -> dict[str, Any]:
    """honeypot.is-only check of its holder sell fail-rate flags, with the source's own severity: high_fail_rate
    ("a very high amount of users cannot sell", critical there) fails as critical, medium_fail_rate ("a high
    amount", high there) fails as a red flag. Neither flag in a well-formed flag list after a successful simulation
    passes; anything else (no answer, failed simulation, missing or malformed flags) is unknown. A sample of holders
    whose simulated sell failed (blacklist, wallet or transaction limits, cooldown, contract holders, ...), not a
    contract property, so it is not compared with GoPlus."""
    thr = "no honeypot.is high_fail_rate / medium_fail_rate flag"
    raw = (hp or {}).get("flags")
    flags = {x for x in (raw if isinstance(raw, list) else ()) if isinstance(x, str)} & _SELL_FAIL_FLAGS
    if "high_fail_rate" in flags:
        return _check(
            "holder_sell_fail_rate",
            "fail",
            "critical",
            "high_fail_rate",
            thr,
            "honeypot.is: a very high share of sampled holders could not sell (its own severity: critical)",
        )
    if flags:
        return _check(
            "holder_sell_fail_rate",
            "fail",
            "red_flag",
            "medium_fail_rate",
            thr,
            "honeypot.is: a high share of sampled holders could not sell (its own severity: high); causes include "
            "blacklists, wallet or transaction limits and cooldowns, so check why before trading",
        )
    if hp is None:
        return _check("holder_sell_fail_rate", "unknown", "critical", None, thr, "honeypot.is gave no answer")
    if hp.get("simulation_success") is not True:
        return _check("holder_sell_fail_rate", "unknown", "critical", None, thr, "honeypot.is simulation failed")
    if not isinstance(raw, list):
        return _check("holder_sell_fail_rate", "unknown", "critical", None, thr, "honeypot.is flag list missing or malformed")
    return _check("holder_sell_fail_rate", "pass", "critical", None, thr, "honeypot.is: no fail-rate flag")


def _merge_check(check: dict[str, Any], second: tuple[str, Any, str] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The check with the worse verdict of the two sources (fail > unknown > pass) and the disagreement, if any.
    `value` and `detail` come from the source that decided the verdict (GoPlus on a tie or when honeypot.is gave no
    answer; for taxes `value` is the higher of the two). Without a second answer a GoPlus pass becomes unknown: one
    source alone never makes it green."""
    g, gv = check["verdict"], check["value"]
    h, hv, hd = second if second is not None else ("unknown", None, "honeypot.is gave no answer")
    by_second = second is not None and _RANK[h] > _RANK[g]
    merged = h if _RANK[h] > _RANK[g] else g
    value = hv if by_second else gv
    if check["name"] in ("buy_tax", "sell_tax"):
        known = [v for v in (gv, hv) if isinstance(v, float | int)]
        value = max(known) if known else None  # the worse (higher) tax
    if second is None:
        detail = f"GoPlus: {check['detail']}; honeypot.is gave no answer, so this rests on GoPlus alone and cannot pass"
    elif by_second:
        detail = f"verdict from honeypot.is: {hd}; GoPlus: {g} ({check['detail']}); the worse verdict of the two is kept"
    else:
        detail = f"verdict from GoPlus: {check['detail']}; honeypot.is: {h} ({hd}); the worse verdict of the two is kept"
    out = {
        **check,
        "verdict": merged,
        "value": value,
        "detail": detail,
        "sources": {"goplus": {"verdict": g, "value": gv}, "honeypot_is": {"verdict": h, "value": hv}},
    }
    split = {g, h} == {"pass", "fail"}
    if check["name"] in ("buy_tax", "sell_tax") and isinstance(gv, float | int) and isinstance(hv, float | int):
        split = split or abs(gv - hv) > TAX_GAP_MAX + 1e-9
    disagreement = {"check": check["name"], "goplus": [g, gv], "honeypot_is": [h, hv]} if split else None
    return out, disagreement


def cross_check(result: dict[str, Any], second: dict[str, Any] | None) -> dict[str, Any]:
    """A new `assess` result where honeypot, cannot_sell_all, buy_tax and sell_tax take the worse verdict of GoPlus
    and honeypot.is (`second` = parse_honeypot_is(...), or None when honeypot.is gave no answer). A honeypot.is pass
    never upgrades a GoPlus verdict. Adds the honeypot.is-only holder_sell_fail_rate check after cannot_sell_all
    and `second_source` with honeypot.is's own summary and the disagreements (pass vs fail, or taxes more than
    TAX_GAP_MAX apart)."""
    verdicts = _second_verdicts(second) if second is not None else {}
    checks, disagreements = [], []
    for c in result["checks"]:
        if c["name"] in CROSS_CHECKED:
            c, d = _merge_check(c, verdicts.get(c["name"]) if second is not None else None)
            if d is not None:
                disagreements.append(d)
        checks.append(c)
        if c["name"] == "cannot_sell_all":
            checks.append(_fail_rate_check(second))  # honeypot.is only, never a disagreement
    src: dict[str, Any] = {"provider": "honeypot.is", "available": second is not None, "disagreements": disagreements}
    if second is not None:
        src |= {k: second.get(k) for k in ("simulation_success", "risk", "risk_level", "flags")}
    return {**result, **_summary(checks), "checks": checks, "second_source": src}
