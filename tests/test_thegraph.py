"""The Graph provider (providers/thegraph.py) behind MockTransport, on hand-built synthetic answers (no live sample yet).

Once the owner has run `scripts/thegraph_sample.py` on the server, the happy-path and error tests switch to
`tests/fixtures/thegraph_live_<date>.json` (same labels: meta, page_1, page_2, error_query; each with status and body).
"""

import gzip
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.core.errors import ProviderError
from tradedesk.core.health import HealthRegistry
from tradedesk.core.http import Http
from tradedesk.providers.thegraph import POLYMARKET_SUBGRAPH_ID, TheGraph, headers
from tradedesk.runtime import Runtime

FIX = json.loads((Path(__file__).parent / "fixtures/thegraph_synthetic_2026-10-28.json").read_text())
SECRET = "gRaPhKeY0123456789abcdefSECRET"
PNL_ID = "7nQSyntheticPnzSubgraphXd1111111111111111111"  # base58: no 0, O, I, l
PAGES_Q = (
    "query($first: Int!, $lastId: String!) { positions(first: $first, orderBy: id, orderDirection: asc, "
    "where: {id_gt: $lastId}) { id amount } }"
)


def make_rt(handler, *, key=SECRET, cache_scale=0.0, pnl=None, retries=0):
    seen: list[httpx.Request] = []

    def wrapped(req):
        seen.append(req)
        return handler(req)

    s = Settings(http_retries=retries, cache_scale=cache_scale, graph_api_key=key, graph_pnl_subgraph_id=pnl, db_path="")
    return Runtime(s, transports={"thegraph": httpx.MockTransport(wrapped)}), seen


def answer(body, status=200):
    return lambda req: httpx.Response(status, json=body)


async def test_query_happy_path_sends_bearer_header_and_explicit_user_agent():
    rt, seen = make_rt(answer(FIX["meta"]))
    f = await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert f.data["_meta"]["block"]["number"] == 78000000
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == f"https://gateway.thegraph.com/api/subgraphs/id/{POLYMARKET_SUBGRAPH_ID}"
    assert SECRET not in str(req.url)
    assert req.headers["authorization"] == f"Bearer {SECRET}"
    ua = req.headers["user-agent"]
    assert ua.startswith("tradedesk-mcp/") and "python" not in ua.lower()  # Cloudflare 1010 refuses Python-urllib
    body = json.loads(req.content)
    assert "_meta" in body["query"] and body["variables"] == {}
    ev = f.evidence
    assert ev.provider == "thegraph" and ev.tool == "graphql" and ev.cached is False
    assert ev.args["subgraph_id"] == POLYMARKET_SUBGRAPH_ID
    assert rt.thegraph.queries_today() == 1


async def test_polymarket_helper_passes_variables():
    rt, seen = make_rt(answer(FIX["page_2"]))
    f = await rt.thegraph.polymarket("query($n: Int!) { positions(first: $n) { id amount } }", {"n": 1})
    assert f.data["positions"][0]["id"] == "0x03"
    assert seen[0].url.path.endswith(POLYMARKET_SUBGRAPH_ID)
    assert json.loads(seen[0].content)["variables"] == {"n": 1}
    assert f.evidence.args["variables"] == {"n": 1}


@pytest.mark.parametrize(
    ("sample", "reason"),
    [("error_query", "backend_error"), ("error_partial", "backend_error"), ("error_auth", "auth")],
)
async def test_graphql_errors_payload_is_a_failure_and_not_cached(sample, reason):
    rt, seen = make_rt(answer(FIX[sample]), cache_scale=1.0)
    for _ in range(2):
        with pytest.raises(ProviderError) as e:
            await rt.thegraph.polymarket("{ foo }")
        assert e.value.provider == "thegraph" and e.value.reason == reason
        assert e.value.detail.startswith("GraphQL error: ")
    assert len(seen) == 2  # an error answer (HTTP 200) is never served from cache
    h = rt.health.snapshot()["providers"]["thegraph"]
    assert h["last_reason"] == reason and h["failures"] == 2 and h["calls"] == 2  # one call each, not ok + error


async def test_repeated_auth_errors_in_http_200_mark_provider_down():
    answers = [FIX["meta"]] + [FIX["error_auth"]] * 3
    rt, seen = make_rt(lambda req: httpx.Response(200, json=answers[len(seen) - 1]))
    await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    for _ in range(3):
        with pytest.raises(ProviderError) as e:
            await rt.thegraph.polymarket("{ x }")
        assert e.value.reason == "auth"
    h = rt.health.snapshot()["providers"]["thegraph"]
    assert (h["calls"], h["failures"], h["consecutive_failures"], h["status"]) == (4, 3, 3, "down")


async def test_successful_answer_is_cached():
    rt, seen = make_rt(answer(FIX["meta"]), cache_scale=1.0)
    a = await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    b = await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert len(seen) == 1 and a.evidence.cached is False and b.evidence.cached is True
    assert b.data == a.data and rt.thegraph.queries_today() == 1


@pytest.mark.parametrize(("status", "reason"), [(429, "rate_limited"), (500, "backend_error"), (402, "provider_tier")])
async def test_http_failure_maps_to_reason(status, reason):
    rt, seen = make_rt(answer({"message": "nope"}, status))
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == reason and len(seen) == 1


async def test_retries_at_most_once_even_if_settings_allow_more():
    rt, seen = make_rt(lambda req: httpx.Response(503, text="busy"), retries=5)
    rt.thegraph.http._backoff = 0.0
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == "backend_error" and len(seen) == 2


async def test_missing_key_is_config_error_without_request():
    rt, seen = make_rt(answer(FIX["meta"]), key=None)
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == "config" and "GRAPH_API_KEY" in e.value.detail and seen == []
    # a tool built on it records `missing` instead of raising
    b = Builder()
    assert await b.guard("graph", "thegraph", rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)) is None
    env = b.build(None)
    assert env["ok"] is False and env["missing"][0]["reason"] == "config"


@pytest.mark.parametrize("bad", ["", "../../api/other", "0OIl" * 10, "x" * 20])
async def test_malformed_subgraph_id_is_refused(bad):
    rt, seen = make_rt(answer(FIX["meta"]))
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.query(bad, "{ x }")
    assert e.value.reason == "config" and seen == []


async def test_pnl_subgraph_id_comes_from_settings():
    rt, seen = make_rt(answer(FIX["meta"]))
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.polymarket_pnl("{ x }")
    assert e.value.reason == "config" and seen == []
    rt2, seen2 = make_rt(answer(FIX["meta"]), pnl=PNL_ID)
    await rt2.thegraph.polymarket_pnl("{ _meta { block { number } } }")
    assert seen2[0].url.path == f"/api/subgraphs/id/{PNL_ID}"


async def test_survives_aclose_then_reuse():
    rt, seen = make_rt(answer(FIX["meta"]))
    await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    await rt.aclose()
    f = await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert f.data["_meta"]["hasIndexingErrors"] is False and len(seen) == 2
    await rt.aclose()


async def test_key_never_in_evidence_errors_or_health():
    def echo(req):  # a hostile/buggy gateway echoing the key back in both a URL-like and a bare form
        return httpx.Response(401, text=f"invalid key {SECRET} for /api/{SECRET}/subgraphs")

    rt, _ = make_rt(echo)
    b = Builder()
    await b.guard("graph", "thegraph", rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID))
    assert b.missing[0].reason == "auth"
    dumps = json.dumps(b.build(None)) + json.dumps(rt.health.snapshot())
    assert SECRET not in dumps and SECRET[:12] not in dumps

    rt2, _ = make_rt(answer({"errors": [{"message": f"auth error: key {SECRET} unknown"}]}))
    with pytest.raises(ProviderError) as e:
        await rt2.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == "auth" and SECRET not in e.value.detail
    assert SECRET not in json.dumps(rt2.health.snapshot())

    rt3, _ = make_rt(answer(FIX["meta"]))
    f = await rt3.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert SECRET not in f.evidence.model_dump_json()
    assert SECRET not in repr(rt3.settings)


@pytest.mark.parametrize("pad", [140, 150, 159])
async def test_key_cut_by_snippet_length_leaves_no_prefix(pad):
    rt, _ = make_rt(lambda req: httpx.Response(403, text="x" * pad + SECRET))
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == "auth" and SECRET[:8] not in e.value.detail


async def test_network_error_text_is_scrubbed():
    def boom(req):
        raise httpx.ConnectError(f"cannot connect, header Bearer {SECRET}")

    rt, _ = make_rt(boom)
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == "network" and SECRET not in e.value.detail and "***" in e.value.detail


async def test_query_pages_walks_by_id_cursor():
    pages = [FIX["page_1"], FIX["page_2"]]
    rt, seen = make_rt(lambda req: httpx.Response(200, json=pages[len(seen) - 1]))
    f = await rt.thegraph.query_pages(POLYMARKET_SUBGRAPH_ID, PAGES_Q, "positions", page_size=2)
    assert [r["id"] for r in f.data["rows"]] == ["0x01", "0x02", "0x03"]
    assert f.data["pages"] == 2 and f.data["complete"] is True
    sent = [json.loads(r.content)["variables"] for r in seen]
    assert sent == [{"first": 2, "lastId": ""}, {"first": 2, "lastId": "0x02"}]
    assert f.evidence.args["pages"] == 2 and f.evidence.args["complete"] is True


async def test_query_pages_stops_at_max_pages_and_on_stuck_cursor():
    rt, _ = make_rt(answer(FIX["page_1"]))
    f = await rt.thegraph.query_pages(POLYMARKET_SUBGRAPH_ID, PAGES_Q, "positions", page_size=2, max_pages=1)
    assert f.data["complete"] is False and len(f.data["rows"]) == 2
    with pytest.raises(ProviderError) as e:  # same page again: the cursor would not move
        await rt.thegraph.query_pages(POLYMARKET_SUBGRAPH_ID, PAGES_Q, "positions", page_size=2, max_pages=5)
    assert e.value.reason == "backend_error"
    for kw in ({"page_size": 1001}, {"page_size": 0}):
        with pytest.raises(ProviderError) as e:
            await rt.thegraph.query_pages(POLYMARKET_SUBGRAPH_ID, PAGES_Q, "positions", **kw)
        assert e.value.reason == "config"
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.query_pages(POLYMARKET_SUBGRAPH_ID, "{ positions { id } }", "positions")
    assert e.value.reason == "config"
    h = rt.health.snapshot()["providers"]["thegraph"]
    assert h["failures"] == 0 and h["status"] == "ok"  # caller-shape refusals after a valid answer are not health errors


async def test_daily_budget_refuses_then_resets_next_utc_day():
    now = [datetime(2026, 10, 28, 23, 0, tzinfo=UTC)]
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=FIX["meta"])

    health = HealthRegistry()
    http = Http(
        "thegraph", "https://gateway.test/api", rate=1000, headers=headers(SECRET), health=health, transport=httpx.MockTransport(handler)
    )
    g = TheGraph(http, has_key=True, health=health, ttl=0, daily_budget=2, clock=lambda: now[0])
    await g.meta(POLYMARKET_SUBGRAPH_ID)
    await g.meta(POLYMARKET_SUBGRAPH_ID)
    with pytest.raises(ProviderError) as e:
        await g.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == "rate_limited" and "budget" in e.value.detail and len(seen) == 2
    h = health.snapshot()["providers"]["thegraph"]
    assert h["last_reason"] == "rate_limited" and "budget" in h["last_detail"] and h["failures"] == 1
    now[0] += timedelta(hours=2)
    assert g.queries_today() == 0
    await g.meta(POLYMARKET_SUBGRAPH_ID)
    assert len(seen) == 3 and g.queries_today() == 1
    await http.aclose()


def test_settings_read_key_and_pnl_id_from_env(monkeypatch):
    monkeypatch.setenv("GRAPH_API_KEY", SECRET)
    monkeypatch.setenv("TRADEDESK_GRAPH_PNL_SUBGRAPH_ID", PNL_ID)
    s = Settings.from_env()
    assert s.graph_api_key == SECRET and s.graph_pnl_subgraph_id == PNL_ID and SECRET not in repr(s)
    monkeypatch.delenv("GRAPH_API_KEY")
    assert Settings.from_env().graph_api_key is None
    monkeypatch.setenv("GRAPH_API_KEY", f"  {SECRET}\r")  # an env file saved with CRLF / a stray space
    assert Settings.from_env().graph_api_key == SECRET
    monkeypatch.setenv("GRAPH_API_KEY", " \r")
    assert Settings.from_env().graph_api_key is None


@pytest.mark.parametrize("bad_key", [SECRET + "é", "keyік", SECRET + "\x00", "a\nb"])
async def test_unsendable_key_is_config_error_without_request_or_exception(bad_key):
    rt, seen = make_rt(answer(FIX["meta"]), key=bad_key)  # building the runtime must not raise either
    assert "authorization" not in {k.lower() for k in graph_headers_of(rt)}
    with pytest.raises(ProviderError) as e:
        await rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)
    assert e.value.reason == "config" and "non-ASCII" in e.value.detail and seen == []
    assert bad_key not in e.value.detail
    b = Builder()
    assert await b.guard("graph", "thegraph", rt.thegraph.meta(POLYMARKET_SUBGRAPH_ID)) is None
    assert b.build(None)["missing"][0]["reason"] == "config"
    await rt.aclose()


def graph_headers_of(rt):
    return rt.thegraph.http._client_args["headers"]


def test_sample_script_records_raw_answers_without_the_key(monkeypatch, tmp_path, capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import thegraph_sample as sample

    def nn(kind, name):
        return {"kind": "NON_NULL", "name": None, "ofType": {"kind": kind, "name": name, "ofType": None}}

    rows = [{"id": "0x01", "oracle": "0xaa"}, {"id": "0x02", "oracle": "0xbb"}, {"id": "0x03", "oracle": "0xcc"}]
    seen: list[httpx.Request] = []

    def gateway(req):
        seen.append(req)
        q, v = json.loads(req.content)["query"], json.loads(req.content)["variables"]
        if "__schema" in q:
            listed = {"kind": "NON_NULL", "name": None, "ofType": {"kind": "LIST", "name": None, "ofType": nn("OBJECT", "Condition")}}
            fields = [{"name": "condition", "type": {"kind": "OBJECT", "name": "Condition"}}, {"name": "conditions", "type": listed}]
            body = {"data": {"__schema": {"queryType": {"fields": fields}}}}
        elif "__type" in q:
            body = {
                "data": {
                    "__type": {
                        "name": "Condition",
                        "fields": [{"name": "id", "type": nn("SCALAR", "ID")}, {"name": "oracle", "type": nn("SCALAR", "Bytes")}],
                    }
                }
            }
        elif "_meta" in q:
            return httpx.Response(
                200,
                content=gzip.compress(json.dumps(FIX["meta"]).encode()),
                headers={"content-type": "application/json", "content-encoding": "gzip"},
            )
        elif "conditions" in q:
            after = [r for r in rows if r["id"] > v["lastId"]]
            body = {"data": {"conditions": after[: v["first"]]}}
        else:
            body = {"errors": [{"message": f"Type `Query` has no field `tradedeskNoSuchField` (key {SECRET})"}]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(sample.httpx, "AsyncHTTPTransport", lambda: httpx.MockTransport(gateway))
    out = tmp_path / "live.json"
    assert sample.run(SECRET, out, None) == 0
    text = out.read_text()
    assert SECRET not in text and SECRET not in capsys.readouterr().out
    doc = json.loads(text)
    assert [k for k in doc if k in ("introspect_fields", "introspect_entity", "meta", "page_1", "page_2", "error_query")] == [
        "introspect_fields",
        "introspect_entity",
        "meta",
        "page_1",
        "page_2",
        "error_query",
    ]
    assert doc["entity"] == "conditions" and doc["id_type"] == "ID"
    assert doc["meta"]["body"] == FIX["meta"] and doc["meta"]["status"] == 200  # gzip answer decoded once
    assert [r["id"] for r in doc["page_1"]["body"]["data"]["conditions"]] == ["0x01", "0x02"]
    assert doc["page_2"]["request"]["variables"] == {"first": 2, "lastId": "0x02"}
    assert "$lastId: ID!" in doc["page_1"]["request"]["query"]
    assert doc["outcomes"]["meta"] == "ok" and doc["outcomes"]["error_query"].startswith("backend_error: GraphQL error")
    assert all(r.headers["authorization"] == f"Bearer {SECRET}" for r in seen) and len(seen) == 6
