"""Live sample of The Graph gateway through the provider (rt.thegraph: httpx, `Authorization: Bearer`, its User-Agent).

Run on the server as tradedesk (the agents' sandbox has no network):
    uv run python scripts/thegraph_sample.py [--out PATH] [--entity NAME]

Requests (about six of the free 100k a month), all against the Polymarket main subgraph:
  introspect_fields  the query fields of the schema (to pick a real entity; `--entity` overrides the pick)
  introspect_entity  the fields of that entity's type (id type, scalar fields to select)
  meta               `_meta` (indexed block, deployment, hasIndexingErrors)
  page_1, page_2     `query_pages(page_size=2, max_pages=2)` of the entity, by `id_gt` cursor
  error_query        a deliberately bad query (an unknown field): is it HTTP 200 + `errors`, or HTTP 4xx?

Every raw answer (status, content type, decoded body) is recorded by a wrapping transport and written to
tests/fixtures/thegraph_live_<UTC date>.json (or --out), with the provider's outcome per request.

The key: GRAPH_API_KEY from the environment, else from ~/.config/tradedesk/onchain.env. It is never printed, and the
file is checked for it before writing (any echo is replaced by ***). Request bodies carry no key (it is a header).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tg_send import load_env  # noqa: E402

from tradedesk.config import Settings  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.providers import thegraph as graph_mod  # noqa: E402
from tradedesk.runtime import Runtime  # noqa: E402

ENV_FILE = Path.home() / ".config" / "tradedesk" / "onchain.env"
SUBGRAPH = graph_mod.POLYMARKET_SUBGRAPH_ID
# Preferred entities of the Polymarket main subgraph, first present wins (the schema is checked, not assumed).
CANDIDATES = ("conditions", "orderFilledEvents", "redemptions", "splits", "merges", "positions", "accounts", "markets")
TYPE_REF = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"
FIELDS_Q = "{ __schema { queryType { fields { name type { " + TYPE_REF + " } } } } }"
ENTITY_Q = "query($t: String!) { __type(name: $t) { name fields { name type { " + TYPE_REF + " } } } }"
BAD_Q = "{ tradedeskNoSuchField { id } }"
SCALAR_FIELDS = 4  # besides id


class Recorder(httpx.AsyncBaseTransport):
    """Passes requests to the real transport and keeps each raw answer (status, content type, body)."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.log: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self.inner.handle_async_request(request)
        try:
            content = await resp.aread()  # decoded (gzip etc.)
        finally:
            await resp.aclose()
        text = content.decode("utf-8", "replace")
        try:
            body: Any = json.loads(text)
        except ValueError:
            body = text[:2000]
        try:
            sent = json.loads(request.content or b"{}")
        except ValueError:
            sent = {}
        self.log.append(
            {
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type"),
                "request": {"path": request.url.path, "query": sent.get("query"), "variables": sent.get("variables")},
                "body": body,
            }
        )
        # the body is already decoded: drop the encoding/length headers so the client does not decode it again
        drop = {b"content-encoding", b"content-length", b"transfer-encoding"}
        hdrs = [(k, v) for k, v in resp.headers.raw if k.lower() not in drop]
        return httpx.Response(resp.status_code, headers=hdrs, content=content, request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def read_key() -> tuple[str, str]:
    key = (os.environ.get("GRAPH_API_KEY") or "").strip()
    if key:
        return key, "environment"
    return (load_env(ENV_FILE).get("GRAPH_API_KEY") or "").strip(), str(ENV_FILE).replace(str(Path.home()), "~")


def named(t: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """(innermost kind, name) of an introspection type reference: NON_NULL / LIST wrappers removed."""
    while t and t.get("name") is None and t.get("ofType"):
        t = t["ofType"]
    return (t or {}).get("kind"), (t or {}).get("name")


def pick_entity(fields: list[dict[str, Any]], forced: str | None) -> tuple[str, str] | None:
    """(query field, object type) of a list entity: --entity, else the first of CANDIDATES present."""
    lists = {}
    for f in fields:
        t, is_list = f.get("type"), False
        while t:
            is_list = is_list or t.get("kind") == "LIST"
            if t.get("name"):
                break
            t = t.get("ofType")
        kind, name = named(f.get("type"))
        if is_list and kind == "OBJECT" and name and not f["name"].startswith("_"):
            lists[f["name"]] = name
    for want in (forced,) if forced else CANDIDATES:
        if want in lists:
            return want, lists[want]
    return None


def outcome(e: ProviderError | None) -> str:
    return "ok" if e is None else f"{e.reason}: {e.detail}"


async def collect(key: str, forced: str | None) -> dict[str, Any]:
    """Send the requests; returns the document to save (raw answers keyed by label, outcomes)."""
    rec = Recorder()
    s = dataclasses.replace(Settings.from_env(), graph_api_key=key, db_path="", cache_scale=0.0)
    rt = Runtime(s, transports={"thegraph": rec})
    g = rt.thegraph
    labels: list[str] = []
    outcomes: dict[str, str] = {}
    entity = type_name = id_type = None

    async def step(label: str, coro: Any) -> Any:
        start = len(rec.log)
        err: ProviderError | None = None
        res = None
        try:
            res = await coro
        except ProviderError as e:
            err = e
        n = len(rec.log) - start
        names = [label] if n == 1 and label != "page" else [f"{label}_{i + 1}" for i in range(n)]
        labels.extend(names)
        for nm in names or [label]:
            outcomes[nm] = outcome(err)
        return res

    try:
        f = await step("introspect_fields", g.query(SUBGRAPH, FIELDS_Q))
        fields = (((f.data.get("__schema") or {}).get("queryType") or {}).get("fields") or []) if f else []
        picked = pick_entity(fields, forced)
        if picked:
            entity, type_name = picked
            t = await step("introspect_entity", g.query(SUBGRAPH, ENTITY_Q, {"t": type_name}))
            tfields = ((t.data.get("__type") or {}).get("fields") or []) if t else []
            scalars = [x["name"] for x in tfields if named(x.get("type"))[0] in ("SCALAR", "ENUM") and x["name"] != "id"]
            id_type = next((named(x.get("type"))[1] for x in tfields if x["name"] == "id"), None) or "ID"
            sel = " ".join(["id", *scalars[:SCALAR_FIELDS]])
            pages_q = (
                f"query($first: Int!, $lastId: {id_type}!) {{ {entity}(first: $first, orderBy: id, orderDirection: asc, "
                f"where: {{id_gt: $lastId}}) {{ {sel} }} }}"
            )
        else:
            names = sorted(x.get("name", "") for x in fields)[:40]
            print(f"no candidate entity in the schema (use --entity); query fields seen: {names}")
        await step("meta", g.meta(SUBGRAPH))
        if entity:
            await step("page", g.query_pages(SUBGRAPH, pages_q, entity, page_size=2, max_pages=2))
        await step("error_query", g.query(SUBGRAPH, BAD_Q))
    finally:
        await rt.aclose()

    doc: dict[str, Any] = {
        "_source": (
            f"LIVE, {datetime.now(UTC).isoformat(timespec='seconds')}, {graph_mod.BASE_URL}/subgraphs/id/{SUBGRAPH} "
            f"(Polymarket main subgraph) via scripts/thegraph_sample.py through rt.thegraph (httpx, Authorization: Bearer, "
            f"User-Agent {graph_mod.USER_AGENT}). Raw answers per request; the key is not in this file."
        ),
        "subgraph_id": SUBGRAPH,
        "entity": entity,
        "entity_type": type_name,
        "id_type": id_type,
        "outcomes": outcomes,
        "labels": labels,
    }
    for label, entry in zip(labels, rec.log, strict=False):
        doc[label] = entry
    return doc


def run(key: str, out: Path, forced: str | None) -> int:
    doc = asyncio.run(collect(key, forced))
    text = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    if key in text:
        text = text.replace(key, "***")
        print("note: an answer echoed the key; it was replaced by *** in the file")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    outcomes = doc["outcomes"]
    for label in doc["labels"]:
        print(f"{label:18} HTTP {doc[label]['status']}  {outcomes.get(label, '')[:150].replace(key, '***')}")
    meta = doc.get("meta", {}).get("body")
    if isinstance(meta, dict):
        block = (((meta.get("data") or {}).get("_meta") or {}).get("block") or {}).get("number")
        print(f"indexed block: {block}")
    print(f"entity: {doc['entity']} ({doc['entity_type']}, id {doc['id_type']})")
    print(f"saved {len(doc['labels'])} answers to {out}")
    return 0 if outcomes.get("meta") == "ok" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    today = datetime.now(UTC).date().isoformat()
    ap.add_argument("--out", type=Path, default=ROOT / "tests" / "fixtures" / f"thegraph_live_{today}.json")
    ap.add_argument("--entity", help="query field of a list entity to page (default: first of a preferred list present)")
    a = ap.parse_args()
    key, where = read_key()
    if not key:
        print(f"GRAPH_API_KEY not found (environment, {where}); store it with scripts/server/thegraph-key.sh")
        return 2
    problem = graph_mod.key_problem(key)
    if problem:
        print(problem)
        return 2
    print(f"key from {where}; subgraph {SUBGRAPH}")
    try:
        return run(key, a.out, a.entity)
    except Exception as e:  # never let a traceback carry the key to the terminal
        print(f"failed: {type(e).__name__}: {str(e).replace(key, '***')[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
