"""The response contract shared by every tool.

Every tool returns an Envelope:

    {"ok": true, "data": {...}, "evidence": [...], "missing": [...], "warnings": [...], "as_of": "..."}

* `evidence` says where each number came from (provider, call, args, fetch time, cache hit).
* `missing` lists what could not be filled and why, instead of failing the whole call.
* A tool never raises to the client because one provider is down.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, NamedTuple

from pydantic import BaseModel, Field

from .errors import ProviderError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


class Evidence(BaseModel):
    provider: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    fetched_at: str = Field(default_factory=utcnow_iso)
    latency_ms: int | None = None
    cached: bool = False


class Missing(BaseModel):
    field: str
    provider: str
    reason: str
    detail: str = ""


class Fetched(NamedTuple):
    data: Any
    evidence: Evidence


class Builder:
    """Collects evidence/missing/warnings while a tool runs, then builds the Envelope dict."""

    def __init__(self) -> None:
        self.evidence: list[Evidence] = []
        self.missing: list[Missing] = []
        self.warnings: list[str] = []

    def add(self, fetched: Fetched | Evidence | None) -> Any:
        """Record evidence and return the data (if a Fetched was given)."""
        if fetched is None:
            return None
        if isinstance(fetched, Evidence):
            self.evidence.append(fetched)
            return None
        self.evidence.append(fetched.evidence)
        return fetched.data

    def miss(self, field: str, provider: str, reason: str, detail: str = "") -> None:
        self.missing.append(Missing(field=field, provider=provider, reason=reason, detail=detail[:200]))

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    async def guard(self, field: str, provider: str, awaitable: Awaitable[Fetched]) -> Any:
        """Await a provider call; on ProviderError record `missing` and return None."""
        try:
            return self.add(await awaitable)
        except ProviderError as e:
            self.miss(field, e.provider or provider, e.reason, e.detail)
            return None

    def build(self, data: Any = None, *, ok: bool | None = None) -> dict[str, Any]:
        if ok is None:
            ok = data is not None
        env = {
            "ok": ok,
            "data": data,
            "evidence": [e.model_dump() for e in self.evidence],
            "missing": [m.model_dump() for m in self.missing],
            "warnings": self.warnings,
            "as_of": utcnow_iso(),
        }
        return env
