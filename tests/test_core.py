import asyncio
import time

import httpx
import pytest

from tradedesk.core.cache import TTLCache
from tradedesk.core.envelope import Builder, Evidence, Fetched
from tradedesk.core.errors import ProviderError
from tradedesk.core.health import HealthRegistry, redact
from tradedesk.core.http import Http
from tradedesk.core.ratelimit import TokenBucket


def make_http(handler, **kw):
    health = HealthRegistry()
    kw.setdefault("backoff_base", 0.001)
    http = Http("test", "https://example.test/api", transport=httpx.MockTransport(handler), health=health, **kw)
    return http, health


async def test_single_flight_shares_one_fetch():
    cache = TTLCache()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return "v"

    res = await asyncio.gather(*[cache.get_or_fetch("k", 5, fetch) for _ in range(5)])
    assert calls == 1
    assert [r[0] for r in res] == ["v"] * 5
    assert sum(1 for r in res if r[1]) == 4  # four callers rode along
    assert (await cache.get_or_fetch("k", 5, fetch))[1] is True  # now served from cache


async def test_cache_propagates_errors_and_does_not_cache_them():
    cache = TTLCache()

    async def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        await asyncio.gather(*[cache.get_or_fetch("k", 5, boom) for _ in range(3)])
    assert cache.peek("k") == (False, None)


async def test_token_bucket_throttles():
    tb = TokenBucket(rate=50, burst=1)
    t0 = time.perf_counter()
    for _ in range(4):
        await tb.acquire()
    assert time.perf_counter() - t0 >= 0.05  # 3 waits of ~20ms


async def test_http_retries_then_succeeds_and_records_evidence():
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        if seen["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": 1})

    http, health = make_http(handler, retries=3)
    f = await http.get_json("/x", params={"a": 1, "api_key": "SECRET"}, tool="x")
    assert f.data == {"ok": 1} and seen["n"] == 3
    assert f.evidence.provider == "test" and f.evidence.cached is False
    assert "api_key" not in f.evidence.args and f.evidence.args == {"a": 1}
    assert health.snapshot()["providers"]["test"]["status"] == "ok"


@pytest.mark.parametrize(
    "status,reason",
    [(401, "auth"), (403, "auth"), (402, "provider_tier"), (404, "data_unavailable"), (400, "backend_error")],
)
async def test_http_error_classification_no_retry(status, reason):
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        return httpx.Response(status, text="nope")

    http, health = make_http(handler, retries=3)
    with pytest.raises(ProviderError) as ei:
        await http.get_json("/x")
    assert ei.value.reason == reason and seen["n"] == 1
    assert health.snapshot()["providers"]["test"]["last_reason"] == reason


async def test_http_persistent_429_becomes_rate_limited():
    http, health = make_http(lambda r: httpx.Response(429, text="slow down"), retries=2)
    with pytest.raises(ProviderError) as ei:
        await http.get_json("/x")
    assert ei.value.reason == "rate_limited"
    assert health.snapshot()["providers"]["test"]["status"] == "down"  # never succeeded


async def test_http_network_error_and_non_json():
    def boom(req):
        raise httpx.ConnectError("dns fail")

    http, _ = make_http(boom, retries=1)
    with pytest.raises(ProviderError) as ei:
        await http.get_json("/x")
    assert ei.value.reason == "network"

    http2, _ = make_http(lambda r: httpx.Response(200, text="<html>"), retries=0)
    with pytest.raises(ProviderError) as ei2:
        await http2.get_json("/x")
    assert ei2.value.reason == "backend_error"


async def test_http_cache_marks_second_call_cached_and_skips_network():
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        return httpx.Response(200, json={"n": seen["n"]})

    http, _ = make_http(handler)
    a = await http.get_json("/x", ttl=10)
    b = await http.get_json("/x", ttl=10)
    assert seen["n"] == 1 and a.evidence.cached is False and b.evidence.cached is True
    assert b.data == a.data
    http0, _ = make_http(handler, cache_scale=0.0)
    await http0.get_json("/x", ttl=10)
    await http0.get_json("/x", ttl=10)
    assert seen["n"] == 3  # cache_scale=0 disables caching


async def test_http_body_check_counts_one_error_scrubs_and_never_caches():
    seen = {"n": 0}

    def handler(req):
        seen["n"] += 1
        return httpx.Response(200, json={"err": "bad SeCrEt42 api_key=abc"} if seen["n"] <= 3 else {"v": 1})

    def check(body):
        return ("auth", f"body says {body['err']}") if "err" in body else None

    http, health = make_http(handler, secrets=("SeCrEt42",))
    for _ in range(3):
        with pytest.raises(ProviderError) as ei:
            await http.get_json("/x", ttl=10, check=check)
        assert ei.value.reason == "auth" and "SeCrEt42" not in ei.value.detail and "abc" not in ei.value.detail
    h = health.snapshot()["providers"]["test"]
    assert (h["calls"], h["failures"], h["status"]) == (3, 3, "down")  # an error only, never a success as well
    f = await http.get_json("/x", ttl=10, check=check)  # failures were not cached: the 4th call reaches the network
    assert f.data == {"v": 1} and seen["n"] == 4
    assert health.snapshot()["providers"]["test"]["status"] == "ok"


def test_health_degraded_then_down_then_recovers():
    h = HealthRegistry()
    h.ok("p", 100)
    h.error("p", "network", "x")
    assert h.snapshot()["providers"]["p"]["status"] == "degraded"
    for _ in range(3):
        h.error("p", "network", "x")
    assert h.snapshot()["providers"]["p"]["status"] == "down"
    h.ok("p", 50)
    assert h.snapshot()["providers"]["p"]["status"] == "ok"


def test_redact_hides_secrets():
    assert "SECRET" not in redact("GET /x?api_key=SECRET&b=1 token=SECRET2")


def test_redact_hides_telegram_bot_token():
    out = redact("POST https://api.telegram.org/bot123456789:AAH-secret_PART/sendMessage failed")
    assert "secret_PART" not in out and "123456789" not in out
    assert "/bot***/sendMessage" in out


def test_server_logging_keeps_request_urls_out(caplog):
    import logging

    from tradedesk.server import configure_logging

    configure_logging()
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot1:SECRET/sendMessage")
    assert "SECRET" not in caplog.text


async def test_builder_guard_degrades_gracefully():
    b = Builder()

    async def ok():
        return Fetched({"v": 1}, Evidence(provider="a", tool="t"))

    async def bad():
        raise ProviderError("b", "auth", "401")

    assert await b.guard("f1", "a", ok()) == {"v": 1}
    assert await b.guard("f2", "b", bad()) is None
    env = b.build({"x": 1})
    assert env["ok"] and len(env["evidence"]) == 1
    assert env["missing"] == [{"field": "f2", "provider": "b", "reason": "auth", "detail": "401"}]


def test_redact_hides_secrets_in_dict_and_header_reprs():
    from tradedesk.core.health import redact

    text = "{'api_key': 'K1', \"secret\": \"S1\"} X-BAPI-SIGN: abc access_token=T1 design: kept"
    out = redact(text, limit=500)
    assert not any(s in out for s in ("K1", "S1", "abc", "T1")) and "design: kept" in out
