"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    http_timeout: float = 10.0
    http_retries: int = 2
    # Multiplies every cache TTL. 0 disables caching (useful in tests / debugging).
    cache_scale: float = 1.0
    # Transport used by `tradedesk-mcp` (stdio for local, streamable-http for a server).
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    # Optional keys; a provider without its key reports `missing: config` instead of failing.
    coinalyze_api_key: str | None = None
    coingecko_api_key: str | None = None
    coingecko_plan: str = "demo"  # "demo" or "pro"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            http_timeout=_float("TRADEDESK_HTTP_TIMEOUT", 10.0),
            http_retries=int(_float("TRADEDESK_HTTP_RETRIES", 2)),
            cache_scale=_float("TRADEDESK_CACHE_SCALE", 1.0),
            transport=os.environ.get("TRADEDESK_TRANSPORT", "stdio"),
            host=os.environ.get("TRADEDESK_HOST", "127.0.0.1"),
            port=int(_float("TRADEDESK_PORT", 8000)),
            coinalyze_api_key=os.environ.get("COINALYZE_API_KEY") or None,
            coingecko_api_key=os.environ.get("COINGECKO_API_KEY") or None,
            coingecko_plan=os.environ.get("COINGECKO_PLAN", "demo").lower(),
        )
