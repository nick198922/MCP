"""Bearer-token authentication for the streamable-http transport.

* ``parse_tokens`` turns a comma-separated string from env into a tuple of
  non-empty tokens (whitespace is trimmed; duplicates are kept but ignored).
* ``BearerAuthMiddleware`` is a plain ASGI middleware that fails every HTTP
  request with 401 unless the ``Authorization`` header is ``Bearer <token>`` and
  ``<token>`` matches one of the configured tokens (constant-time comparison).
  ``lifespan`` and ``websocket`` scopes pass through unchanged.

Comparison is done with :func:`hmac.compare_digest` so the response time does
not leak the first mismatching byte.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("tradedesk.auth")


def parse_tokens(raw: str) -> tuple[str, ...]:
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def _match(presented: str, allowed: tuple[str, ...]) -> bool:
    presented_b = presented.encode("utf-8")
    return any(hmac.compare_digest(presented_b, a.encode("utf-8")) for a in allowed)


Scope = dict[str, Any]
Receive = Callable[[], Awaitable[Any]]
Send = Callable[[Any], Awaitable[None]]


class BearerAuthMiddleware:
    """ASGI middleware. Empty ``tokens`` is a configuration error — callers must
    only wrap the app when auth is intentionally enabled."""

    def __init__(self, app: Any, tokens: tuple[str, ...], *, realm: str = "tradedesk") -> None:
        if not tokens:
            raise ValueError("BearerAuthMiddleware requires at least one token")
        self.app = app
        self.tokens = tokens
        self.realm = realm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        header = _authorization_header(scope)
        if not header or not header.lower().startswith("bearer "):
            await self._reject(send, "missing_bearer")
            return
        presented = header[len("bearer ") :].strip()
        if not presented or not _match(presented, self.tokens):
            await self._reject(send, "invalid_bearer")
            return
        await self.app(scope, receive, send)

    async def _reject(self, send: Send, reason: str) -> None:
        body = b'{"error":"unauthorized"}'
        log.info("auth reject: %s", reason)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", f'Bearer realm="{self.realm}"'.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


def _authorization_header(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            try:
                return value.decode("latin-1")
            except Exception:  # noqa: BLE001
                return None
    return None
