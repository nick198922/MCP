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
_ALIASES = {"eth": "ethereum", "bnb": "bsc", "arb": "arbitrum", "matic": "polygon", "op": "optimism", "avax": "avalanche"}
_ALIASES |= {v: k for k, v in CHAINS.items()}  # numeric chain ids are accepted too


def resolve_chain(chain: str | None) -> tuple[str, str] | None:
    """('ethereum', '1') for 'ethereum', 'eth' or '1'; None for unsupported chains."""
    c = (chain or "").strip().lower()
    c = _ALIASES.get(c, c)
    return (c, CHAINS[c]) if c in CHAINS else None


def valid_evm_address(address: str | None) -> tuple[str | None, str | None]:
    """Lower-cased 0x + 40 hex address, or (None, error)."""
    a = (address or "").strip()
    if not EVM_ADDRESS.fullmatch(a):
        return None, f"invalid EVM token address: {clean_text(a, 50)!r} (expected 0x followed by 40 hex characters)"
    return a.lower(), None


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


def _top10_check(token: dict[str, Any]) -> dict[str, Any]:
    thr = f"<= {TOP10_MAX} of non-burned supply (our threshold)"
    rows = _holders(token.get("holders"))
    if not rows:
        return _check("top10_holders", "unknown", "red_flag", None, thr, "no holder data")
    pools = {_addr(d.get("pair")) for d in _holders(token.get("dex"))} - {None}
    burned = 0.0
    shares = []
    skipped = 0
    for h in rows:
        share = _dec(h.get("percent"))
        if share is None or share < 0:
            skipped += 1
            continue
        if _is_burn(h):
            burned += share
        elif _addr(h.get("address")) not in pools:
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
        f"10 largest listed holders as a share of non-burned supply (burned {burned:.4f} excluded, DEX pools excluded); "
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

    failed = [c["name"] for c in checks if c["verdict"] == "fail"]
    unknown = [c["name"] for c in checks if c["verdict"] == "unknown"]
    critical = [c["name"] for c in checks if c["verdict"] == "fail" and c["severity"] == "critical"]
    overall = "fail" if failed else ("unknown" if unknown else "pass")
    holders = _dec(token.get("holder_count"))
    cex = token.get("is_in_cex")
    return {
        "token": {
            "name": clean_text(token.get("token_name"), 40),
            "symbol": clean_text(token.get("token_symbol"), 20),
        },
        "overall": overall,
        "critical_failed": critical,
        "failed": failed,
        "unknown": unknown,
        "passed_count": sum(1 for c in checks if c["verdict"] == "pass"),
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
