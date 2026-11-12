from __future__ import annotations

import re

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


# Reasons a retry cannot fix (caller/config errors, missing data, auth, paid tier).
NO_RETRY_REASONS = frozenset({"config", "auth", "provider_tier", "data_unavailable"})


def is_permanent(e: BaseException) -> bool:
    """True for a `ProviderError` a retry cannot fix: a `NO_RETRY_REASONS` reason or any HTTP 4xx except 429.

    `Http` maps 404 -> data_unavailable, 401/403 -> auth, 402 -> provider_tier and other non-retryable 4xx
    (400, 422, ...) -> backend_error with detail "HTTP <code>: ..."; so check the reason, then the detail prefix.
    Any other exception (timeouts, 5xx, non-provider errors) is transient: False.
    """
    if not isinstance(e, ProviderError):
        return False
    if e.reason in NO_RETRY_REASONS:
        return True
    m = re.match(r"HTTP (\d{3})\b", e.detail or "")
    return bool(m) and 400 <= int(m.group(1)) < 500 and m.group(1) != "429"
