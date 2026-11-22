"""Etherscan API V2 (one endpoint for many EVM chains, a free key: ETHERSCAN_API_KEY).

`GET https://api.etherscan.io/v2/api?chainid=<id>&module=<m>&action=<a>&...&apikey=<key>`. Shapes from the Etherscan
V2 documentation (docs.etherscan.io), checked live through the MCP service with the free key on 2026-11-22: all four
onchain_* tools answered on ethereum (balance, tokenbalance, txlist, tokentx, getsourcecode, eth_call). Free-key
coverage depends on the endpoint, not only the chain: `contract/getsourcecode` answered on all seven CHAINS, while
`account/*` and `proxy/eth_call` answered on ethereum, polygon and arbitrum only and were refused as a paid-plan chain on
bsc, optimism, base and avalanche (account/balance on all four, txlist on base, eth_call on bsc), see FREE_CHAINS and
FREE_SOURCE_CHAINS (tests/fixtures/etherscan_v2_live_2026-11-22.json; raw row shapes still in
etherscan_v2_docs_2026-11-19.json).
Without a key (2026-11-19) `account/balance` and `proxy/eth_call` both answered
`{"status": "0", "message": "NOTOK", "result": "Missing/Invalid API Key"}`. The envelope:
- module APIs answer `{"status": "1", "message": "OK", "result": ...}`; an error is HTTP 200 with `status` "0",
  `message` "NOTOK" and the reason as text in `result` (invalid key, the 3 requests/s limit, a paid-only endpoint or
  chain); an empty list is `status` "0" with `message` "No transactions found" and `result` [];
- `module=proxy` (eth_call) answers JSON-RPC: `{"jsonrpc": "2.0", "id": 1, "result": "0x..."}` or `{"error": {...}}`
  when the call reverts; on the request limit it answers in the module format above. Only a hex result or a revert
  error counts as an answer (`_proxy_check`); other node errors are failures, never cached.
Used here: `account/balance` (wei), `account/tokenbalance` (raw token units), `account/txlist` and `account/tokentx`
(paged: `page` x `offset` <= 10,000 rows), `contract/getsourcecode`, `proxy/eth_call` (name / symbol / decimals /
totalSupply of an ERC-20). Not used: token info (`token/tokeninfo`) and top holders (`token/topholders`,
`token/tokenholderlist`) are Etherscan API Pro (paid) endpoints.

The key: only from Settings (environment). It goes into the query string, so it is passed to `Http` as a secret (scrubbed
from every error detail) and never into `Evidence` (explicit args without it). Error text in `result` is third-party:
it is matched against known phrases and never echoed.
Free tier: 3 requests/s and 100,000 a day per key (Etherscan's published limits); the runtime keeps 2/s. The key is
shared with the research scripts, so the limit can still be hit: a request-limit answer in an HTTP 200 body is asked
once more after ~1 s (`Http` retries only HTTP 429/5xx); other status "0" answers are not repeated.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..analytics.evm import abi_string, hex_uint, uint_text, valid_evm_address
from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.etherscan.io"
PATH = "/v2/api"
RATE_PER_SECOND = 2.0  # free tier allows 3/s per key; the research scripts share the key
MAX_ROWS = 10_000  # Etherscan returns at most page x offset <= 10,000 rows of one listing
RATE_LIMIT_RETRY_S = 1.0  # one more try after this pause when the body says "rate limit" (at most two requests)
RATE_LIMIT_DETAIL = "Etherscan request limit reached (free key: 3 requests/s, 100k/day)"

# name -> (chain id, native coin). All stay mapped: the free key reads contract source on all of them, and a paid key
# would open the account / eth_call endpoints on the rest.
CHAINS: dict[str, tuple[int, str]] = {
    "ethereum": (1, "ETH"),
    "bsc": (56, "BNB"),
    "polygon": (137, "POL"),
    "arbitrum": (42161, "ETH"),
    "optimism": (10, "ETH"),
    "base": (8453, "ETH"),
    "avalanche": (43114, "AVAX"),
}
# Free-key coverage per endpoint, checked live 2026-11-22 through the MCP service. account/* and proxy/eth_call
# (onchain_balance, onchain_address_txs, onchain_token_info) answer on FREE_CHAINS only; the other CHAINS are refused as
# paid-plan chains (classified provider_tier; the raw text is never echoed, so not recorded).
FREE_CHAINS: tuple[str, ...] = ("ethereum", "polygon", "arbitrum")
# contract/getsourcecode (onchain_contract_source) answers on every chain with the free key.
FREE_SOURCE_CHAINS: tuple[str, ...] = tuple(CHAINS)
_ALIASES = {"eth": "ethereum", "bnb": "bsc", "matic": "polygon", "arb": "arbitrum", "op": "optimism", "avax": "avalanche"}
_ALIASES |= {str(cid): name for name, (cid, _) in CHAINS.items()}  # numeric chain ids too

# 4-byte selectors of the ERC-20 views read through eth_call.
SELECTORS = {"name": "0x06fdde03", "symbol": "0x95d89b41", "decimals": "0x313ce567", "totalSupply": "0x18160ddd"}

# Phrases of Etherscan's error text (lower-case) -> (reason, our own detail). First match wins.
_ERRORS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("rate limit",), "rate_limited", RATE_LIMIT_DETAIL),
    (("api key", "apikey"), "auth", "Etherscan rejected the API key"),
    (
        ("upgrade", "not supported for this chain", "api pro", "exclusive", "pro endpoint", "paid"),
        "provider_tier",
        "needs a paid Etherscan plan (the free key does not cover this endpoint or chain)",
    ),
    (("chainid",), "backend_error", "Etherscan V2 does not accept this chain id"),
    (("invalid address", "invalid contract address"), "backend_error", "Etherscan rejected the address"),
)


def resolve_chain(chain: Any) -> tuple[str, int, str] | None:
    """('ethereum', 1, 'ETH') for 'ethereum', 'eth' or '1'; None for a chain we do not map."""
    c = chain.strip().lower() if isinstance(chain, str) else ""
    c = _ALIASES.get(c, c)
    return (c, *CHAINS[c]) if c in CHAINS else None


def _classify(body: dict[str, Any]) -> tuple[str, str]:
    text = " ".join(str(body.get(k, "")) for k in ("message", "result")).lower()
    for phrases, reason, detail in _ERRORS:
        if any(p in text for p in phrases):
            return reason, detail
    return "backend_error", "Etherscan answered status 0 (NOTOK)"


def _body_check(body: Any) -> tuple[str, str] | None:
    """`Http` body check of module answers: status "1", or status "0" with an empty list ("No transactions found"); any
    other status "0" is a failure (counted once in health, never cached)."""
    if not isinstance(body, dict):
        return "backend_error", "unexpected response shape at /v2/api"
    status = body.get("status")
    if status == "1":
        return None
    if status == "0":
        if body.get("result") == [] and str(body.get("message", "")).lower().startswith("no "):
            return None  # "No transactions found" / "No records found": an empty answer, not an error
        return _classify(body)
    return "backend_error", "unexpected status at /v2/api"


_HEX = re.compile(r"0x[0-9a-fA-F]*")


def _proxy_check(body: Any) -> tuple[str, str] | None:
    """`Http` body check of proxy (JSON-RPC) answers. Only two answers pass (and may be cached): a hex `result`, or a
    JSON-RPC `error` whose message says the call reverted (handled by the caller as data_unavailable). Anything else
    (a module-style status "0", another node error such as "header not found", a non-hex `result` such as a rate-limit
    text) is a failure: counted in health and never cached, so a transient fault is not served as "not an ERC-20"."""
    if not isinstance(body, dict):
        return "backend_error", "unexpected response shape at /v2/api (proxy)"
    if "status" in body and body.get("status") != "1":
        return _classify(body)
    result, error = body.get("result"), body.get("error")
    if isinstance(result, str) and _HEX.fullmatch(result):
        return None
    if isinstance(error, dict) and "revert" in str(error.get("message", "")).lower():
        return None
    reason, detail = _classify({"result": result if isinstance(result, str) else "", "message": str(error)[:500]})
    if reason == "rate_limited":
        return reason, detail
    return "backend_error", "eth_call failed upstream"


class Etherscan:
    name = "etherscan"

    def __init__(
        self,
        http: Http,
        *,
        api_key: str | None,
        retry_delay: float = RATE_LIMIT_RETRY_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.http = http
        self._key = api_key
        self.has_key = bool(api_key)
        self.retry_delay, self.sleep = retry_delay, sleep  # injectable: tests never really wait

    def _need_key(self) -> None:
        if not self._key:
            raise ProviderError(
                self.name, "config", "ETHERSCAN_API_KEY is not set in the MCP service environment (free key at etherscan.io)"
            )

    async def _get(self, chain_id: int, module: str, action: str, ttl: float, proxy: bool = False, **params: Any) -> Fetched:
        self._need_key()
        args = {"chainid": chain_id, "module": module, "action": action, **params}

        async def fetch() -> Fetched:
            return await self.http.get_json(
                PATH,
                params={**args, "apikey": self._key},
                ttl=ttl,
                tool=f"{module}/{action}",
                args=args,  # never the key
                check=_proxy_check if proxy else _body_check,
            )

        try:
            return await fetch()
        except ProviderError as e:
            # Only the request-limit text of an HTTP 200 body (our own detail from _classify) earns one more try; an HTTP
            # 429 was already retried by Http, and other NOTOK answers (key, plan, address) would only fail again.
            if e.reason != "rate_limited" or e.detail != RATE_LIMIT_DETAIL:
                raise
        await self.sleep(self.retry_delay)
        return await fetch()

    def _uint(self, f: Fetched, what: str) -> Fetched:
        v = uint_text(f.data.get("result"))
        if v is None:
            raise ProviderError(self.name, "backend_error", f"{what}: result is not a non-negative integer")
        return Fetched(v, f.evidence)

    async def native_balance(self, chain_id: int, address: str) -> Fetched:
        """Balance in wei (int) at the latest block."""
        addr = self._address(address)
        f = await self._get(chain_id, "account", "balance", ttl=30, address=addr, tag="latest")
        return self._uint(f, "account/balance")

    async def token_balance(self, chain_id: int, token: str, address: str) -> Fetched:
        """ERC-20 balance in the token's smallest unit (int), not scaled by decimals."""
        tok, addr = self._address(token), self._address(address)
        f = await self._get(chain_id, "account", "tokenbalance", ttl=30, contractaddress=tok, address=addr, tag="latest")
        return self._uint(f, "account/tokenbalance")

    async def transactions(self, chain_id: int, address: str, kind: str, page: int, limit: int, token: str | None = None) -> Fetched:
        """Rows of `account/txlist` (kind 'normal') or `account/tokentx` (kind 'erc20'), newest first; [] when none."""
        addr = self._address(address)
        if kind not in ("normal", "erc20"):
            raise ProviderError(self.name, "unknown", f"unknown kind {kind!r}")
        if page < 1 or limit < 1 or page * limit > MAX_ROWS:
            raise ProviderError(self.name, "unknown", f"page x limit must be within 1..{MAX_ROWS}")
        extra = {"contractaddress": self._address(token)} if token else {}
        action = "txlist" if kind == "normal" else "tokentx"
        f = await self._get(chain_id, "account", action, ttl=30, address=addr, page=page, offset=limit, sort="desc", **extra)
        rows = f.data.get("result")
        if not isinstance(rows, list):
            raise ProviderError(self.name, "backend_error", f"account/{action}: result is not a list")
        return Fetched(rows, f.evidence)

    async def source_code(self, chain_id: int, address: str) -> Fetched:
        """The first item of `contract/getsourcecode` (a dict; unverified addresses have an empty SourceCode)."""
        addr = self._address(address)
        f = await self._get(chain_id, "contract", "getsourcecode", ttl=3600, address=addr)
        items = f.data.get("result")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise ProviderError(self.name, "backend_error", "contract/getsourcecode: result is not a list of objects")
        return Fetched(items[0], f.evidence)

    async def eth_call(self, chain_id: int, to: str, data: str, ttl: float) -> Fetched:
        """Hex result of a read-only call at the latest block; a revert or an empty result ("0x": no code, or no such
        function) is data_unavailable."""
        addr = self._address(to)
        f = await self._get(chain_id, "proxy", "eth_call", ttl=ttl, proxy=True, to=addr, data=data, tag="latest")
        result = f.data.get("result")
        if "error" in f.data or not isinstance(result, str) or result in ("0x", ""):
            raise ProviderError(self.name, "data_unavailable", f"eth_call {data} returned nothing (not an ERC-20 view, or reverted)")
        return Fetched(result, f.evidence)

    async def erc20_view(self, chain_id: int, token: str, view: str) -> Fetched:
        """One ERC-20 view: name / symbol (display text), decimals (0..255) or totalSupply (raw int)."""
        ttl = 60 if view == "totalSupply" else 3600
        f = await self.eth_call(chain_id, token, SELECTORS[view], ttl=ttl)
        value: Any
        if view in ("name", "symbol"):
            value = abi_string(f.data, 64 if view == "name" else 20)
        else:
            value = hex_uint(f.data)
            if view == "decimals" and value is not None and value > 255:
                value = None
        if value is None:
            raise ProviderError(self.name, "data_unavailable", f"eth_call {view}(): result could not be decoded")
        return Fetched(value, f.evidence)

    def _address(self, address: str | None) -> str:
        # Every address goes into the query string: re-checked here even though the tools validate it first.
        addr, err = valid_evm_address(address)
        if addr is None:
            raise ProviderError(self.name, "unknown", err or "invalid address")
        return addr
