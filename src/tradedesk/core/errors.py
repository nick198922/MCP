from __future__ import annotations

# Reason classes (superset of the ones DOUBLETOP used), so a failure is always machine-readable.
REASONS = (
    "config",  # key/URL missing
    "auth",  # 401/403
    "network",  # DNS, timeout, connection reset
    "rate_limited",  # 429 after retries
    "provider_tier",  # 402 / feature needs a paid plan
    "data_unavailable",  # 404, unknown symbol, empty result
    "backend_error",  # 5xx, malformed response
    "unknown",
)


class ProviderError(Exception):
    """A data source failed. Tools catch this and degrade gracefully instead of crashing."""

    def __init__(self, provider: str, reason: str, detail: str = "") -> None:
        if reason not in REASONS:
            reason = "unknown"
        super().__init__(f"{provider}: {reason}: {detail}")
        self.provider = provider
        self.reason = reason
        self.detail = detail
