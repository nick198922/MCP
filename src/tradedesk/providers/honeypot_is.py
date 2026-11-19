"""honeypot.is public API (free, no key): buy/sell simulation of an EVM token, the second source of `contract_risk`.

`GET /v2/IsHoneypot?address=<token>&chainID=<id>` answers (live 2026-11-19, tests/fixtures/honeypot_is_2026-11-19.json)
`{"token": {...}, "summary": {"risk": "low" | "honeypot" | "unknown" | ..., "riskLevel": int, "flags": [...]},
"simulationSuccess": bool, "honeypotResult": {"isHoneypot": bool} | null, "simulationResult": {"buyTax": 0.3,
"sellTax": 0.3, "transferTax": 0, ...} | null, "flags": ["high_fail_rate", ...], ...}`. Taxes are PERCENT (FLOKI
0.3 = 0.3 %, GoPlus "0.003" for the same token); the analytics layer turns them into decimals.

Errors come as HTTP codes, so the generic `Http` cache and health work as usual: an unsupported chain is HTTP 400
"Invalid chain" (seen for 137 and 42161), an unknown token HTTP 404 "Token not found" (cached as an answer). Only
chains 1, 56 and 8453 answered live; `token_check` refuses the others without a request.
Limit: the answer carries `x-ratelimit-limit: 50` (window not documented); 0.5 req/s in Runtime stays below it even
for a one-minute window.
"""

from __future__ import annotations

from typing import Any

from ..core.envelope import Fetched
from ..core.errors import ProviderError
from ..core.http import Http

BASE_URL = "https://api.honeypot.is"
RATE_PER_SECOND = 0.5
TTL_SECONDS = 60.0
CHAINS = frozenset({"1", "56", "8453"})  # ethereum, bsc, base (live 2026-11-19; 137 and 42161 -> "Invalid chain")


def _body_check(body: Any) -> tuple[str, str] | None:
    if not isinstance(body, dict) or "simulationSuccess" not in body:
        return "backend_error", "unexpected response shape at /v2/IsHoneypot"
    return None


class HoneypotIs:
    name = "honeypot_is"

    def __init__(self, http: Http) -> None:
        self.http = http

    async def token_check(self, chain_id: str, address: str) -> Fetched:
        """Simulation result for one EVM token: Fetched(raw body dict, evidence). `address` must already be
        validated; it is sent lower-case. Unsupported chains raise `data_unavailable` without a request."""
        if chain_id not in CHAINS:
            raise ProviderError(self.name, "data_unavailable", f"honeypot.is does not cover chain id {chain_id} (only 1, 56, 8453)")
        addr = address.lower()
        return await self.http.get_json(
            "/v2/IsHoneypot",
            params={"address": addr, "chainID": chain_id},
            ttl=TTL_SECONDS,
            tool="is_honeypot",
            args={"chain_id": chain_id, "address": addr},
            check=_body_check,
        )
