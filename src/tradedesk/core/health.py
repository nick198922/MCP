"""Per-provider health, updated on every call (no separate polling needed)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .envelope import utcnow_iso

_SECRET = re.compile(r"(?i)((?:api[_-]?key|token|secret|signature|passphrase)=)[^&\s\"']+")
# "key": "value" / 'key': 'value' / Key: value in reprs of dicts and headers (ccxt errors). "sign" only as a whole word,
# so "design: ..." stays; the other names may sit inside longer ones (apiSecret, clientSecret, accessToken).
_SECRET_KV = re.compile(r"(?i)((?:api[_-]?key|token|secret|signature|passphrase|(?<![a-z])sign)[\"']?\s*:\s*[\"']?)[^&\s\"',}]+")
# Bybit's sign error echoes the signed string, which starts with the timestamp and the API key.
_ORIGIN = re.compile(r"(?i)(origin_string\[)[^\]]*")
# Telegram puts the bot token in the URL path: /bot<id>:<secret>/sendMessage.
_BOT_TOKEN = re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+")


def redact(text: str, limit: int = 200) -> str:
    for rx in (_SECRET, _SECRET_KV, _ORIGIN, _BOT_TOKEN):
        text = rx.sub(r"\1***", text)
    return text[:limit]


@dataclass
class ProviderHealth:
    name: str
    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_ok_at: str | None = None
    last_error_at: str | None = None
    last_reason: str | None = None
    last_detail: str | None = None
    latency_ms_ewma: float | None = None

    @property
    def status(self) -> str:
        if self.calls == 0:
            return "unknown"
        if self.consecutive_failures == 0:
            return "ok"
        if self.consecutive_failures < 3 and self.last_ok_at is not None:
            return "degraded"
        return "down"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "calls": self.calls,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_ok_at": self.last_ok_at,
            "last_error_at": self.last_error_at,
            "last_reason": self.last_reason,
            "last_detail": self.last_detail,
            "latency_ms_ewma": None if self.latency_ms_ewma is None else round(self.latency_ms_ewma, 1),
        }


class HealthRegistry:
    def __init__(self) -> None:
        self._items: dict[str, ProviderHealth] = {}

    def _get(self, name: str) -> ProviderHealth:
        return self._items.setdefault(name, ProviderHealth(name))

    def register(self, name: str) -> None:
        self._get(name)

    def ok(self, name: str, latency_ms: float) -> None:
        h = self._get(name)
        h.calls += 1
        h.consecutive_failures = 0
        h.last_ok_at = utcnow_iso()
        h.latency_ms_ewma = latency_ms if h.latency_ms_ewma is None else 0.8 * h.latency_ms_ewma + 0.2 * latency_ms

    def error(self, name: str, reason: str, detail: str = "") -> None:
        h = self._get(name)
        h.calls += 1
        h.failures += 1
        h.consecutive_failures += 1
        h.last_error_at = utcnow_iso()
        h.last_reason = reason
        h.last_detail = redact(detail)

    def snapshot(self) -> dict[str, Any]:
        providers = {n: h.as_dict() for n, h in sorted(self._items.items())}
        counts: dict[str, int] = {}
        for p in providers.values():
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        return {"summary": counts, "providers": providers}
