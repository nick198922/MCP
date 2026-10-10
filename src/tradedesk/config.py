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
    # Local SQLite history (Phase 2). Empty string disables the store entirely.
    db_path: str = "data/tradedesk.sqlite"
    collector_enabled: bool = False
    # Collection cadences in seconds.
    collect_atm_term_seconds: int = 300
    collect_funding_seconds: int = 3600
    collect_basis_seconds: int = 300

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
            db_path=os.environ.get("TRADEDESK_DB_PATH", "data/tradedesk.sqlite"),
            collector_enabled=os.environ.get("TRADEDESK_COLLECTOR_ENABLED", "").lower() in ("1", "true", "yes"),
            collect_atm_term_seconds=int(_float("TRADEDESK_COLLECT_ATM_TERM_SECONDS", 300)),
            collect_funding_seconds=int(_float("TRADEDESK_COLLECT_FUNDING_SECONDS", 3600)),
            collect_basis_seconds=int(_float("TRADEDESK_COLLECT_BASIS_SECONDS", 300)),
        )
