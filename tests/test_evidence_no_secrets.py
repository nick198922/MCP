"""API keys never reach Fetched.evidence, HealthRegistry or ProviderError (CLAUDE.md, "Security": nothing secret in evidence).

Tool envelopes and `system_health` end up in chat, Telegram and PRs, so a key sent in a header (Coinalyze, CoinGecko,
The Graph) must not come back through evidence, health details or error texts, even when the venue echoes it in an error
body or a network error message. Fake marker keys only; no environment, no disk, no network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tradedesk.config import Settings
from tradedesk.core.errors import ProviderError
from tradedesk.core.health import HealthRegistry
from tradedesk.core.http import Http
from tradedesk.runtime import Runtime

COINALYZE_KEY = "SECRET-MARKER-xyz-coinalyze"
COINGECKO_KEY = "SECRET-MARKER-xyz-coingecko"
GRAPH_KEY = "SECRET-MARKER-xyz-graph"
MARKER = "SECRET-MARKER-xyz"

# provider -> (header that must carry the key, expected header value, the key itself)
SENT = {
    "coinalyze": ("api_key", COINALYZE_KEY, COINALYZE_KEY),
    "coingecko": ("x-cg-demo-api-key", COINGECKO_KEY, COINGECKO_KEY),
    "thegraph": ("authorization", f"Bearer {GRAPH_KEY}", GRAPH_KEY),
}

# How a venue might echo the key back: a plain sentence, JSON, a header dump, the request URL, a bearer line.
ECHOES = [
    "Invalid API key {key}",
    '{{"error": "unauthorized", "key": "{key}"}}',
    "headers: {{'api_key': '{key}'}}",
    "GET https://x.test/v1/a?api_key={key}&b=1",
    "Authorization: Bearer {key} rejected",
    "{key}",
]

STATUSES = [400, 401, 402, 403, 404, 429, 500]


def _dump(obj: object) -> str:
    return json.dumps(obj, default=str)


def _runtime(handler) -> Runtime:
    settings = Settings(
        http_retries=1,
        http_retry_backoff=0.0,
        cache_scale=0.0,
        coinalyze_api_key=COINALYZE_KEY,
        coingecko_api_key=COINGECKO_KEY,
        graph_api_key=GRAPH_KEY,
    )
    transports = {name: httpx.MockTransport(lambda req, name=name: handler(name, req)) for name in SENT}
    return Runtime(settings, transports=transports)


def _require_key(name: str, req: httpx.Request) -> str:
    """The key must travel in its header, never in the URL; returns it so the handler can echo it."""
    header, value, key = SENT[name]
    assert req.headers.get(header) == value
    assert MARKER not in str(req.url)
    return key


def _assert_clean(rt: Runtime, *objs: object) -> None:
    assert MARKER not in _dump(rt.health.snapshot())
    for obj in objs:
        assert MARKER not in str(obj)
        assert MARKER not in repr(obj)


@pytest.mark.parametrize("name", sorted(SENT))
async def test_success_evidence_has_no_key(name: str) -> None:
    def handler(prov: str, req: httpx.Request) -> httpx.Response:
        _require_key(prov, req)
        return httpx.Response(200, json=[{"symbol": "BTCUSDT_PERP.A", "history": []}])

    rt = _runtime(handler)
    fetched = await getattr(rt, name).http.get_json("/a", params={"symbols": "BTCUSDT_PERP.A", "interval": "daily"}, tool="a")
    assert fetched.data
    _assert_clean(rt, fetched.evidence, fetched.evidence.model_dump_json())
    assert rt.health.snapshot()["providers"][name]["status"] == "ok"


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("echo", ECHOES)
@pytest.mark.parametrize("name", sorted(SENT))
async def test_http_error_echoing_key_is_scrubbed(name: str, echo: str, status: int) -> None:
    def handler(prov: str, req: httpx.Request) -> httpx.Response:
        key = _require_key(prov, req)
        return httpx.Response(status, text=echo.format(key=key))

    rt = _runtime(handler)
    with pytest.raises(ProviderError) as ei:
        await getattr(rt, name).http.get_json("/a", params={"b": 1})
    err = ei.value
    _assert_clean(rt, err, err.detail, err.args)
    assert rt.health.snapshot()["providers"][name]["last_reason"] == err.reason


@pytest.mark.parametrize("echo", ECHOES)
@pytest.mark.parametrize("name", sorted(SENT))
async def test_network_error_echoing_key_is_scrubbed(name: str, echo: str) -> None:
    def handler(prov: str, req: httpx.Request) -> httpx.Response:
        key = _require_key(prov, req)
        raise httpx.ConnectError(echo.format(key=key), request=req)

    rt = _runtime(handler)
    with pytest.raises(ProviderError) as ei:
        await getattr(rt, name).http.get_json("/a")
    assert ei.value.reason == "network"
    _assert_clean(rt, ei.value, ei.value.detail)


@pytest.mark.parametrize("name", sorted(SENT))
async def test_non_json_success_is_scrubbed(name: str) -> None:
    def handler(prov: str, req: httpx.Request) -> httpx.Response:
        key = _require_key(prov, req)
        return httpx.Response(200, text=f"<html>{key}</html>", headers={"content-type": f"text/html; key={key}"})

    rt = _runtime(handler)
    with pytest.raises(ProviderError) as ei:
        await getattr(rt, name).http.get_json("/a")
    assert ei.value.reason == "backend_error"
    _assert_clean(rt, ei.value, ei.value.detail)


async def test_key_in_query_params_stays_out_of_evidence_and_errors() -> None:
    """A provider that had to send a key as a query parameter: evidence drops it, an echoed URL is redacted."""
    key = "SECRET-MARKER-xyz-query"
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert req.url.params.get("apikey") == key
        if calls["n"] == 1:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(500, text=f"failed GET {req.url}")

    health = HealthRegistry()
    http = Http("q", "https://x.test", retries=0, backoff_base=0.0, health=health, transport=httpx.MockTransport(handler))
    fetched = await http.get_json("/a", params={"apikey": key, "symbol": "BTC"})
    assert fetched.evidence.args == {"symbol": "BTC"}
    assert MARKER not in fetched.evidence.model_dump_json()
    with pytest.raises(ProviderError) as ei:
        await http.get_json("/a", params={"apikey": key, "symbol": "ETH"})
    for obj in (ei.value, ei.value.detail):
        assert MARKER not in str(obj) and MARKER not in repr(obj)
    assert MARKER not in _dump(health.snapshot())


async def test_header_keys_are_scrubbed_without_explicit_secrets() -> None:
    """Http derives secrets from its own auth headers, so a provider wired without `secrets=` cannot leak an echo."""
    key = "SECRET-MARKER-xyz-header"

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad credentials {key}")

    for headers in ({"api_key": key}, {"X-Api-Key": key}, {"Authorization": f"Bearer {key}"}):
        health = HealthRegistry()
        http = Http("h", "https://x.test", retries=0, headers=headers, health=health, transport=httpx.MockTransport(handler))
        with pytest.raises(ProviderError) as ei:
            await http.get_json("/a")
        assert "***" in ei.value.detail
        assert MARKER not in str(ei.value) and MARKER not in _dump(health.snapshot())
        # the scrub is in the text only: the key is still sent
        assert http._client.headers.get(next(iter(headers))) == headers[next(iter(headers))]
        await http.aclose()
