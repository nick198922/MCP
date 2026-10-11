from __future__ import annotations

import pytest

from tradedesk.http_auth import BearerAuthMiddleware, parse_tokens


def test_parse_tokens_strips_and_filters_empty():
    assert parse_tokens("") == ()
    assert parse_tokens(" , ,") == ()
    assert parse_tokens("abc, def ") == ("abc", "def")
    assert parse_tokens("single") == ("single",)


def test_middleware_rejects_empty_token_set():
    with pytest.raises(ValueError):
        BearerAuthMiddleware(lambda *a, **k: None, ())


async def _call(mw: BearerAuthMiddleware, headers: list[tuple[bytes, bytes]]) -> dict:
    sent: dict = {"status": None, "headers": {}, "body": b""}
    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": headers}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {name: value for name, value in message["headers"]}
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body") or b""

    await mw(scope, receive, send)
    return sent


class _OkApp:
    """Minimal ASGI app that always returns 200."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})


async def test_middleware_401_without_header():
    inner = _OkApp()
    mw = BearerAuthMiddleware(inner, ("s3cret",))
    out = await _call(mw, headers=[])
    assert out["status"] == 401
    assert out["headers"][b"www-authenticate"].startswith(b"Bearer ")
    assert inner.calls == 0


async def test_middleware_401_on_wrong_token():
    inner = _OkApp()
    mw = BearerAuthMiddleware(inner, ("s3cret",))
    out = await _call(mw, headers=[(b"authorization", b"Bearer nope")])
    assert out["status"] == 401
    assert inner.calls == 0


async def test_middleware_401_on_wrong_scheme():
    inner = _OkApp()
    mw = BearerAuthMiddleware(inner, ("s3cret",))
    out = await _call(mw, headers=[(b"authorization", b"Basic s3cret")])
    assert out["status"] == 401


async def test_middleware_passes_valid_token():
    inner = _OkApp()
    mw = BearerAuthMiddleware(inner, ("s3cret", "backup"))
    out = await _call(mw, headers=[(b"authorization", b"Bearer backup")])
    assert out["status"] == 200
    assert out["body"] == b"ok"
    assert inner.calls == 1


async def test_middleware_bearer_scheme_is_case_insensitive():
    inner = _OkApp()
    mw = BearerAuthMiddleware(inner, ("s3cret",))
    out = await _call(mw, headers=[(b"authorization", b"bearer s3cret")])
    assert out["status"] == 200


async def test_middleware_passes_non_http_scopes():
    inner = _OkApp()
    mw = BearerAuthMiddleware(inner, ("s3cret",))
    sent = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(msg):
        sent.append(msg)

    await mw({"type": "lifespan"}, receive, send)
    assert inner.calls == 1
