"""H1 for Polymarket (IDEA-20261015-smart-money-persistence): do top traders by P&L in month M stay in the upper part
in month M+1? History from The Graph Polymarket subgraphs, point-in-time via `block: {number: N}`.

Run on the server as tradedesk (the key is read by code from ~/.config/tradedesk/onchain.env, never printed):
    uv run python scripts/verify_smart_money_polymarket_h1.py probe     # schema of the subgraphs (a few queries)

Every answer is written to a per-file JSON cache under ~/.cache/tradedesk-agents/smart-money-pm-h1/ (--cache-dir);
a cached file is never fetched again. Network only through rt.thegraph (core.http.Http).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import resource
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from thegraph_sample import read_key  # noqa: E402

from tradedesk.config import Settings  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.providers import thegraph as graph_mod  # noqa: E402
from tradedesk.runtime import Runtime  # noqa: E402

CACHE = Path.home() / ".cache" / "tradedesk-agents" / "smart-money-pm-h1"
MAIN = graph_mod.POLYMARKET_SUBGRAPH_ID
NETWORK = "DZz4kDTdmzWLWsV373w2bSmoar3umKKH9y82SUKr5qmp"  # The Graph Network (Arbitrum) subgraph: subgraph metadata
TYPE_REF = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class Client:
    """rt.thegraph with a per-file JSON cache (one file per (subgraph, query, variables))."""

    def __init__(self, cache: Path) -> None:
        key, where = read_key()
        if not key:
            sys.exit(f"GRAPH_API_KEY not found ({where})")
        self._key = key
        s = dataclasses.replace(Settings.from_env(), graph_api_key=key, db_path="", cache_scale=0.0, http_timeout=90.0)
        self.rt = Runtime(s)
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        self.sent = 0
        self.hits = 0

    def _path(self, sub: str, query: str, variables: dict[str, Any]) -> Path:
        h = hashlib.sha256(json.dumps([sub, " ".join(query.split()), variables], sort_keys=True).encode()).hexdigest()[:24]
        return self.cache / f"q_{h}.json"

    async def q(self, sub: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        variables = dict(variables or {})
        p = self._path(sub, query, variables)
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text())["data"]
        for attempt in range(4):  # the gateway sometimes routes to an indexer without the block ("bad indexers")
            try:
                f = await self.rt.thegraph.query(sub, query, variables)
                break
            except ProviderError as e:
                if attempt == 3 or e.reason not in ("backend_error", "network"):
                    raise
                print(f"  retry {attempt + 1} after {e.reason}", flush=True)
                await asyncio.sleep(15 * (attempt + 1))
        self.sent += 1
        text = json.dumps({"sub": sub, "query": " ".join(query.split()), "variables": variables, "data": f.data})
        if self._key in text:  # never store an echo of the key
            text = text.replace(self._key, "***")
        p.write_text(text)
        return f.data

    async def aclose(self) -> None:
        await self.rt.aclose()

    def scrub(self, s: str) -> str:
        return s.replace(self._key, "***")


def flat_type(t: dict[str, Any] | None) -> str:
    out = ""
    while t:
        if t.get("kind") == "LIST":
            out += "["
        if t.get("name"):
            return out + t["name"]
        t = t.get("ofType")
    return out


async def probe(c: Client) -> None:
    types = ["Position", "Split", "Merge", "Redemption", "FixedProductMarketMaker", "NegRiskConversion", "Condition"]
    body = " ".join(f'{t.lower()}: __type(name: "{t}") {{ name fields {{ name type {{ {TYPE_REF} }} }} }}' for t in types)
    try:
        d = await c.q(MAIN, "{ " + body + " }")
        for t in types:
            ty = d.get(t.lower()) or {}
            fields = ", ".join(f"{f['name']}:{flat_type(f['type'])}" for f in ty.get("fields") or [])
            print(f"main.{t}: {fields}")
    except ProviderError as e:
        print("main introspection failed:", c.scrub(f"{e.reason}: {e.detail}"))
    try:
        d = await c.q(
            MAIN, "{ positions(first: 3) { id } redemptions(first: 2, orderBy: timestamp, orderDirection: asc) { id timestamp } }"
        )
        print("main sample:", json.dumps(d)[:600])
    except ProviderError as e:
        print("main sample failed:", c.scrub(f"{e.reason}: {e.detail}"))
    q_net = (
        '{ subgraphMetas(first: 40, where: {displayName_contains_nocase: "polymarket"}) '
        "{ id displayName description subgraph { id active currentSignalledTokens "
        "currentVersion { subgraphDeployment { ipfsHash stakedTokens } } } } }"
    )
    try:
        d = await c.q(NETWORK, q_net)
        for m in d.get("subgraphMetas") or []:
            sg = m.get("subgraph") or {}
            dep = ((sg.get("currentVersion") or {}).get("subgraphDeployment") or {}).get("ipfsHash")
            print(f"net: {m.get('displayName')!r:50} subgraph={sg.get('id')} active={sg.get('active')} dep={dep}")
    except ProviderError as e:
        print("network search failed:", c.scrub(f"{e.reason}: {e.detail}"))


PNL_CANDIDATES = {
    "pnl": "6c58N5U4MtQE2Y8njfVrrAfRykzfqajMGeTMEvMmskVz",  # 'Polymarket Profit and Loss' (active)
    "v2pnl": "6iGihLdrPygidk3h61e4Bnbr2yREof5STEguPuenhSfT",  # 'polymarket-v2-pnl'
    "pnl2": "EtXYMmR6ZVMkR1ndhfb5kZSee2T8hHg72FzPH5CPkypa",  # 'Polymarket PnL'
}


async def probe_pnl(c: Client) -> None:
    fq = "{ _meta { block { number timestamp } hasIndexingErrors } __schema { queryType { fields { name type { " + TYPE_REF + " } } } } }"
    for label, sub in PNL_CANDIDATES.items():
        try:
            d = await c.q(sub, fq)
        except ProviderError as e:
            print(f"{label}: failed {c.scrub(e.reason + ': ' + e.detail)[:200]}")
            continue
        meta = d.get("_meta") or {}
        ents = [
            flat_type(f["type"]).strip("[")
            for f in d["__schema"]["queryType"]["fields"]
            if flat_type(f["type"]).startswith("[") and not f["name"].startswith("_")
        ]
        print(f"{label} {sub}: meta={meta} entities={ents}")
        body = " ".join(f'{t.lower()}: __type(name: "{t}") {{ fields {{ name type {{ {TYPE_REF} }} }} }}' for t in ents[:8])
        try:
            d2 = await c.q(sub, "{ " + body + " }")
            for t in ents[:8]:
                fields = ", ".join(f"{f['name']}:{flat_type(f['type'])}" for f in (d2.get(t.lower()) or {}).get("fields") or [])
                print(f"  {t}: {fields}")
        except ProviderError as e:
            print(f"  types failed {c.scrub(e.detail)[:200]}")


V2PNL = PNL_CANDIDATES["v2pnl"]
ACCT_FIELDS = "id tradesQuantity collateralVolume scaledCollateralVolume realizedPnl lastTradedTimestamp"


async def probe_hist(c: Client) -> None:
    """Block timestamps back in time and whether v2pnl has accounts there; one page of accounts for scales."""
    now = (await c.q(V2PNL, "{ _meta { block { number timestamp } } }"))["_meta"]["block"]
    n0 = int(now["number"])
    print("now", now)
    for k in (1, 2, 4, 6, 9, 12, 18, 24, 36):
        n = n0 - k * 1_300_000
        qq = "query($b: Int!) { _meta(block: {number: $b}) { block { number timestamp } } accounts(first: 3, block: {number: $b}) { id } }"
        try:
            d = await c.q(V2PNL, qq, {"b": n})
            print(k, d["_meta"]["block"], len(d["accounts"]))
        except ProviderError as e:
            print(k, n, "failed", c.scrub(e.detail)[:160])
    d = await c.q(V2PNL, "{ accounts(first: 8, orderBy: collateralVolume, orderDirection: desc) { " + ACCT_FIELDS + " } }")
    for r in d["accounts"]:
        print(r)
    d = await c.q(V2PNL, '{ accounts(first: 1000, where: {id_lt: "0x0100"}) { id tradesQuantity } }')
    print("accounts with id < 0x0100:", len(d["accounts"]))


AVAIL_SUBS = {
    "v2pnl": V2PNL,
    "pnl": PNL_CANDIDATES["pnl"],
    "pnl2": PNL_CANDIDATES["pnl2"],
    "orderbook": "7fu2DWYK93ePfzB24c2wrP94S3x4LGHUrQxphhoEypyY",
    "v2orderbook": "B9mm21DKCex8ka4g8cteQU4NQqtviwmcTjQAYLbzQ1eR",
}


async def probe_avail(c: Client) -> None:
    """How far back can each subgraph answer a `block: {number}` (time-travel) query?"""
    n0 = 96_401_091  # head seen by probe_hist (2026-10-29 ~20:51 UTC)
    qq = "query($b: Int!) { _meta(block: {number: $b}) { block { number timestamp } } }"
    for label, sub in AVAIL_SUBS.items():
        out = []
        for back in (20_000, 100_000, 300_000, 1_000_000, 3_000_000):
            try:
                d = await c.q(sub, qq, {"b": n0 - back})
                out.append(f"-{back}: ok ts={d['_meta']['block']['timestamp']}")
            except ProviderError as e:
                det = c.scrub(e.detail)
                kind = "missing" if "missing block" in det else "startBlock" if "startBlock" in det else det[:90]
                out.append(f"-{back}: {kind}")
        print(label, " | ".join(out))
    for label in ("orderbook", "v2orderbook"):
        fq = "{ __schema { queryType { fields { name type { " + TYPE_REF + " } } } } }"
        d = await c.q(AVAIL_SUBS[label], fq)
        ents = [flat_type(f["type"]).strip("[") for f in d["__schema"]["queryType"]["fields"] if flat_type(f["type"]).startswith("[")]
        print(label, "entities:", [e for e in ents if not e.startswith("_")])


async def probe_v1(c: Client) -> None:
    ob, v2ob, pnl = AVAIL_SUBS["orderbook"], AVAIL_SUBS["v2orderbook"], AVAIL_SUBS["pnl"]
    types = ["Account", "OrderFilledEvent", "MarketData", "Orderbook"]
    body = " ".join(f'{t.lower()}: __type(name: "{t}") {{ fields {{ name type {{ {TYPE_REF} }} }} }}' for t in types)
    d = await c.q(ob, "{ " + body + " }")
    for t in types:
        print(f"ob.{t}:", ", ".join(f"{f['name']}:{flat_type(f['type'])}" for f in (d.get(t.lower()) or {}).get("fields") or []))
    last = "{ orderFilledEvents(first: 1, orderBy: timestamp, orderDirection: desc) { timestamp } }"
    for label, sub in (("v1 ob", ob), ("v2 ob", v2ob)):
        print(label, "last fill:", (await c.q(sub, last))["orderFilledEvents"])
    try:
        d = await c.q(pnl, "{ userPositions(first: 5) { id user tokenId amount avgPrice realizedPnl totalBought } }")
        for r in d["userPositions"]:
            print("pnl.up", r)
        u = d["userPositions"][0]["user"]
        d = await c.q(pnl, "query($u: String!) { userPositions(first: 1000, where: {user: $u}) { id realizedPnl } }", {"u": u})
        print("positions of first user:", len(d["userPositions"]))
    except ProviderError as e:
        print("pnl sample failed", c.scrub(e.detail)[:200])
    for b in await month_blocks(c, "2023-01", "2026-05"):
        print("boundary", b)


def month_starts(first: str, last: str) -> list[int]:
    """UTC epoch seconds of the 1st of each month from `first` to `last` (YYYY-MM), inclusive."""
    from datetime import UTC, datetime

    y, m = map(int, first.split("-"))
    ly, lm = map(int, last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        out.append(int(datetime(y, m, 1, tzinfo=UTC).timestamp()))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def month_blocks(c: Client, first: str, last: str) -> list[dict[str, Any]]:
    """Block of the first v1 order fill at or after each month start (no time travel: immutable events)."""
    q = (
        "query($t: BigInt!) { orderFilledEvents(first: 1, orderBy: timestamp, orderDirection: asc, "
        "where: {timestamp_gte: $t}) { timestamp blockNumber } }"
    )
    out = []
    for t in month_starts(first, last):
        rows = (await c.q(AVAIL_SUBS["orderbook"], q, {"t": str(t)}))["orderFilledEvents"]
        out.append(
            {"month_start": t, "ts": int(rows[0]["timestamp"]) if rows else None, "block": int(rows[0]["blockNumber"]) if rows else None}
        )
    return out


# ----------------------------------------------------------------------------- fetch (pre-registered design)

PNL = PNL_CANDIDATES["pnl"]
OB = AVAIL_SUBS["orderbook"]
POS_FIELDS = "id user tokenId amount avgPrice realizedPnl totalBought"
COND_FIELDS = "id positionIds payoutNumerators payoutDenominator"


def week_starts(first: str, last: str) -> list[int]:
    """Mondays 00:00 UTC from `first` to `last` (YYYY-MM-DD), inclusive."""
    from datetime import UTC, datetime, timedelta

    d = datetime.fromisoformat(first).replace(tzinfo=UTC)
    end = datetime.fromisoformat(last).replace(tzinfo=UTC)
    out = []
    while d <= end:
        out.append(int(d.timestamp()))
        d += timedelta(days=7)
    return out


async def boundaries(c: Client) -> list[dict[str, Any]]:
    """Month starts 2025-05..2026-04, Mondays 2025-05-05..2026-04-27, and the last v1 fill (end); sorted by block."""
    q = (
        "query($t: BigInt!) { orderFilledEvents(first: 1, orderBy: timestamp, orderDirection: asc, "
        "where: {timestamp_gte: $t}) { timestamp blockNumber } }"
    )
    out: dict[int, dict[str, Any]] = {}
    for kind, ts in [("month", t) for t in month_starts("2025-05", "2026-04")] + [
        ("week", t) for t in week_starts("2025-05-05", "2026-04-27")
    ]:
        rows = (await c.q(OB, q, {"t": str(ts)}))["orderFilledEvents"]
        b = int(rows[0]["blockNumber"])
        e = out.setdefault(b, {"block": b, "ts": int(rows[0]["timestamp"]), "kinds": set()})
        e["kinds"].add(kind)
    last = (await c.q(OB, "{ orderFilledEvents(first: 1, orderBy: timestamp, orderDirection: desc) { timestamp blockNumber } }"))[
        "orderFilledEvents"
    ][0]
    b = int(last["blockNumber"])
    out.setdefault(b, {"block": b, "ts": int(last["timestamp"]), "kinds": set()})["kinds"].update({"month", "week", "end"})
    res = sorted(out.values(), key=lambda e: e["block"])
    for e in res:
        e["kinds"] = sorted(e["kinds"])
    return res


async def walk(c: Client, sub: str, query: str, entity: str, variables: dict[str, Any], tag: str, max_pages: int) -> list[str]:
    """Page by id cursor; every page is a cache file. Returns the cache file names in order."""
    files: list[str] = []
    last = ""
    size = 1000
    for _ in range(max_pages):
        v = {**variables, "first": size, "lastId": last}
        try:
            d = await c.q(sub, query, v)
        except ProviderError as e:  # a heavy historical page can fail on every indexer: walk on with small pages
            if size == 200 or e.reason != "backend_error":
                raise
            print(f"  {tag}: page failed at size {size}, continuing with 200-row pages", flush=True)
            size = 200
            continue
        files.append(c._path(sub, query, v).name)
        rows = d[entity]
        if len(files) % 25 == 0:
            print(f"  {tag}: page {len(files)} sent {c.sent} hits {c.hits} RSS {rss_mb():.0f} MB", flush=True)
        if len(rows) < size:
            return files
        last = rows[-1]["id"]
    raise SystemExit(f"{tag}: more than {max_pages} pages; stop (pre-registered narrowing rule)")


def pos_query(delta: bool) -> str:
    chg = ", _change_block: {number_gte: $since}" if delta else ""
    since = ", $since: Int!" if delta else ""
    return (
        f"query($first: Int!, $lastId: ID!, $b: Int!, $hi: ID!{since}) {{ userPositions(first: $first, orderBy: id, "
        f"orderDirection: asc, block: {{number: $b}}, where: {{id_gt: $lastId, id_lt: $hi{chg}}}) {{ {POS_FIELDS} }} }}"
    )


def cond_query(delta: bool) -> str:
    chg = ", _change_block: {number_gte: $since}" if delta else ""
    since = ", $since: Int!" if delta else ""
    return (
        f"query($first: Int!, $lastId: ID!, $b: Int!{since}) {{ conditions(first: $first, orderBy: id, orderDirection: asc, "
        f'block: {{number: $b}}, where: {{id_gt: $lastId, payoutDenominator_gt: "0"{chg}}}) {{ {COND_FIELDS} }} }}'
    )


async def fetch(c: Client, a: argparse.Namespace) -> None:
    import time

    t0 = time.time()
    bnd = await boundaries(c)
    (c.cache / "boundaries.json").write_text(json.dumps(bnd))
    print(f"boundaries: {len(bnd)} (first {bnd[0]}, last {bnd[-1]}); sent {c.sent}", flush=True)
    stop = len(bnd) if a.upto is None else min(a.upto, len(bnd))
    ks = served(bnd)
    prev_of = {k: (ks[j - 1] if j else None) for j, k in enumerate(ks)}
    # deltas are independent per boundary, so a second process may walk them from the end (--reverse)
    for k in reversed([k for k in ks if k < stop]) if a.reverse else [k for k in ks if k < stop]:
        prev = prev_of[k]
        mf = c.cache / manifest_name(a.hi, k, prev)
        if mf.exists():
            continue
        since = {} if prev is None else {"since": bnd[prev]["block"] + 1}
        for b in range(bnd[k]["block"], bnd[k]["block"] + 4):  # a block no indexer serves: take the next one (~2 s later)
            try:
                pf = await walk(c, PNL, pos_query(k > 0), "userPositions", {"b": b, "hi": a.hi, **since}, f"pos[{k}]", a.max_pages)
                break
            except ProviderError as e:
                print(f"  pos[{k}] at block {b} failed ({e.reason}); trying block {b + 1}", flush=True)
        else:
            raise SystemExit(f"boundary {k}: no indexer serves blocks {bnd[k]['block']}..+3")
        cf = await walk(c, PNL, cond_query(k > 0), "conditions", {"b": b, **since}, f"cond[{k}]", a.max_pages)
        mf.write_text(json.dumps({"k": k, "block": b, "positions": pf, "conditions": cf}))
        print(
            f"boundary {k}/{len(bnd)} block {b}: pos pages {len(pf)}, cond pages {len(cf)}; sent {c.sent}, {time.time() - t0:.0f} s, RSS {rss_mb():.0f} MB",
            flush=True,
        )
    print("FETCH DONE", flush=True)


async def check_change(c: Client, a: argparse.Namespace) -> None:
    """Sanity: full snapshots of a small id range at two boundaries, diffed locally, against the union of the
    `_change_block` deltas fetched for the boundaries in between (same id range)."""
    bnd = json.loads((c.cache / "boundaries.json").read_text())
    hi = "0x0008"
    for k0, k1 in ((15, 21), (26, 28), (28, 34), (34, 36), (40, 42)):
        snaps = []
        for k in (k0, k1):
            files = await walk(c, PNL, pos_query(False), "userPositions", {"b": bnd[k]["block"], "hi": hi}, f"chk[{k}]", 50)
            rows = {}
            for f in files:
                for r in json.loads((c.cache / f).read_text())["data"]["userPositions"]:
                    rows[r["id"]] = (r["amount"], r["realizedPnl"], r["totalBought"])
            snaps.append(rows)
        changed = {i for i, v in snaps[1].items() if snaps[0].get(i) != v}
        delta = set()
        for k in range(k0 + 1, k1 + 1):
            man = json.loads((c.cache / f"manifest_0x02_{k}.json").read_text())
            for f in man["positions"]:
                for r in json.loads((c.cache / f).read_text())["data"]["userPositions"]:
                    if r["id"] < hi:
                        delta.add(r["id"])
        print(
            f"k {k0}->{k1}: positions {len(snaps[0])}->{len(snaps[1])}, changed by snapshot diff {len(changed)}, "
            f"in deltas {len(delta)}, changed but missing from deltas {len(changed - delta)}"
        )


# ----------------------------------------------------------------------------- analysis (cache only, no network)


UNSERVED = {85_459_182}  # 2026-04-13 week boundary: no indexer serves it or the next 3 blocks (30.10.2026)


def served(bnd: list[dict[str, Any]]) -> list[int]:
    """Boundary indices that are fetched; a dropped boundary merges its window into the next one."""
    return [k for k, e in enumerate(bnd) if e["block"] not in UNSERVED]


def manifest_name(hi: str, k: int, prev: int | None) -> str:
    return f"manifest_{hi}_{k}.json" if prev is None or prev == k - 1 else f"manifest_{hi}_{k}_from{prev}.json"


def _ts(day: str) -> int:
    from datetime import UTC, datetime

    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())


PNL_GAP = ("2025-10-27", "2025-12-01")  # P&L subgraph nearly frozen (card, data-quality amendment)


def unpaid_split(k: int, ts: int, pos: dict[str, dict[str, Any]], payout: dict[str, float], tok_last: dict[str, int]) -> dict[str, Any]:
    """Open-position cost (amount * avgPrice, USDC) at boundary k, the part without a known payout, and that part
    split by the last boundary at which any sampled position in the token changed (cache only)."""
    from tradedesk.analytics import trader_persistence as tp

    g0, g1 = _ts(PNL_GAP[0]), _ts(PNL_GAP[1])
    buckets = {
        "before the gap (< 2025-10-27)": lambda x: x < g0,
        "inside the gap": lambda x: g0 <= x < g1,
        "after the gap, > 30 d before": lambda x: g1 <= x < ts - 30 * 86400,
        "last 30 days": lambda x: x >= max(g1, ts - 30 * 86400),
    }
    by_last = {name: {"cost": 0.0, "tokens": set()} for name in buckets}
    open_ = unpaid = stale = 0.0
    n_open = n_unpaid = 0
    for book in pos.values():
        tot, unp = tp.open_cost(book, payout)
        open_ += tot
        unpaid += unp
        if unp == 0.0:
            n_open += sum(1 for s in book.values() if s.amount > 0)
            continue
        for t, s in book.items():
            if s.amount <= 0:
                continue
            n_open += 1
            if t in payout:
                continue
            n_unpaid += 1
            c = (s.amount / tp.SCALE) * (s.avg_price / tp.SCALE)
            last = tok_last.get(t, 0)
            if last < ts - 60 * 86400:
                stale += c
            for name, f in buckets.items():
                if f(last):
                    by_last[name]["cost"] += c
                    by_last[name]["tokens"].add(t)
                    break
    toks = set().union(*(v["tokens"] for v in by_last.values()))
    return {
        "k": k,
        "open": open_,
        "unpaid": unpaid,
        "stale": stale,
        "n_open": n_open,
        "n_unpaid": n_unpaid,
        "tok_unpaid": len(toks),
        "by_last": {name: {"cost": v["cost"], "tokens": len(v["tokens"])} for name, v in by_last.items()},
    }


def analyze(cache: Path, hi: str, n_perm: int) -> dict[str, Any]:
    from collections import defaultdict
    from datetime import UTC, datetime

    from tradedesk.analytics import trader_persistence as tp

    bnd = json.loads((cache / "boundaries.json").read_text())
    ks = served(bnd)
    month_idx = [k for k in ks if "month" in bnd[k]["kinds"]]
    week_idx = [k for k in ks if "week" in bnd[k]["kinds"]]
    pos: dict[str, dict[str, tp.PosState]] = {}
    payout: dict[str, float] = {}
    bought_m: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    bought_w: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    settled: dict[int, dict[str, float]] = {}  # by original boundary index
    realized: dict[int, dict[str, float]] = {}
    tok_last: dict[str, int] = {}  # token -> ts of the last boundary at which any sampled position in it changed
    unpaid_rows: list[dict[str, Any]] = []
    n_rows = 0
    adj_abs = 0.0
    per_k: list[int] = []
    for j, k in enumerate(ks):
        per_k.append(n_rows)
        man = json.loads((cache / manifest_name(hi, k, ks[j - 1] if j else None)).read_text())
        pm, pw = tp.period_of(k, month_idx), tp.period_of(k, week_idx)
        for f in man["positions"]:
            delta: dict[str, dict[str, tp.PosState]] = defaultdict(dict)
            for r in json.loads((cache / f).read_text())["data"]["userPositions"]:
                n_rows += 1
                u, t = r["user"], r["tokenId"]
                delta[u][t] = tp.PosState(int(r["amount"]), int(r["avgPrice"]), int(r["realizedPnl"]), int(r["totalBought"]))
                tok_last[t] = bnd[k]["ts"]
            for u, new in delta.items():
                book = pos.setdefault(u, {})
                if k > 0:  # k = 0 is the full starting snapshot: no purchases inside a period
                    toks = tp.tokens_bought(book, new)
                    if toks and pm is not None:
                        bought_m[pm][u] |= toks
                    if toks and pw is not None:
                        bought_w[pw][u] |= toks
                book.update(new)
        for f in man["conditions"]:
            for cnd in json.loads((cache / f).read_text())["data"]["conditions"]:
                den = int(cnd["payoutDenominator"])
                if den > 0:
                    for tid, num in zip(cnd["positionIds"], cnd["payoutNumerators"], strict=False):
                        payout[str(tid)] = int(num) / den
        sv, rv = {}, {}
        for u, book in pos.items():
            sv[u], rv[u] = tp.settled_value(book, payout)
        settled[k] = sv
        realized[k] = rv
        if k == ks[-1]:
            adj_abs = sum(abs(sv[u] - rv[u]) for u in sv)
        if k in month_idx:
            unpaid_rows.append(unpaid_split(k, bnd[k]["ts"], pos, payout, tok_last))
    per_k = [b - a for a, b in zip(per_k, [*per_k[1:], n_rows], strict=False)]
    print("position rows per boundary (k=0 is the full snapshot):", " ".join(str(x) for x in per_k))
    print(
        f"rows {n_rows}, users {len(pos)}, positions {sum(len(b) for b in pos.values())}, resolved tokens {len(payout)}, RSS {rss_mb():.0f} MB"
    )
    print(
        f"at the end: sum |settled - realized| = ${adj_abs:,.0f}, sum |realized| = ${sum(abs(v) for v in realized[ks[-1]].values()):,.0f}"
    )

    def label(k: int) -> str:
        return datetime.fromtimestamp(bnd[k]["ts"], UTC).strftime("%Y-%m-%d")

    print("\n== open-position cost without a known payout, at month boundaries (descriptive; registered to report) ==")
    print("  causes cannot be fully separated from the cache: only RESOLVED conditions were fetched, so 'condition exists")
    print("  but unresolved' and 'token in no condition' look the same. Split by the token's last sampled activity instead:")
    print("  'stale' = no sampled position in the token changed in the 60 days before the boundary.")
    print(f"  {'boundary':10} {'open $M':>8} {'unpaid $M':>9} {'share':>6} {'stale $M':>8} {'stale sh':>8}")
    for r in unpaid_rows:
        print(
            f"  {label(r['k']):10} {r['open'] / 1e6:8.2f} {r['unpaid'] / 1e6:9.2f} {r['unpaid'] / r['open']:6.3f} "
            f"{r['stale'] / 1e6:8.2f} {r['stale'] / r['open']:8.3f}"
        )
    end = unpaid_rows[-1]
    print(
        f"  at the end ({label(end['k'])}): unpaid ${end['unpaid'] / 1e6:.2f}M of ${end['open'] / 1e6:.2f}M "
        f"({end['unpaid'] / end['open']:.3f}); positions unpaid {end['n_unpaid']} of {end['n_open']}; "
        f"tokens unpaid {end['tok_unpaid']}"
    )
    for name, v in end["by_last"].items():
        print(f"    token last active {name:28}: ${v['cost'] / 1e6:6.2f}M ({v['cost'] / end['open']:.3f} of open), tokens {v['tokens']}")

    def bad_period(p: int, idx: list[int], min_days: float, max_days: float, gap_from: int, gap_to: int) -> bool:
        # 1 h tolerance inside tp.bad_period: September (ending 2025-10-01 00:00:03) does not touch October
        # (bug fixed 30.10 after the first run)
        return tp.bad_period(
            bnd[idx[p]]["ts"], bnd[idx[p + 1]]["ts"], min_days=min_days, max_days=max_days, gap_from=gap_from, gap_to=gap_to
        )

    def build(
        values: dict[int, dict[str, float]],
        idx: list[int],
        bought: dict,
        min_tokens: int,
        min_days: float,
        users=None,
        *,
        gap: str = "main",
    ) -> list[tp.Pair]:
        # data-quality amendment (card, before analysis): P&L subgraph nearly frozen 2025-10-27..2025-12-01
        gaps = {
            "main": (_ts("2025-10-01"), _ts("2025-12-01")),  # monthly: drop pairs touching Oct or Nov 2025
            "dec": (_ts("2025-10-01"), _ts("2026-01-01")),  # T = 7 variant: also drop Dec -> Jan
            "week": (_ts("2025-10-27"), _ts("2025-12-01")),
            "none": (0, 0),
        }
        g0, g1 = gaps[gap]
        max_days = 8.0 if min_days < 10 else 40.0
        per = tp.period_pnl([values[k] for k in idx])
        pairs = []
        for p in range(len(per) - 1):
            if bad_period(p, idx, min_days, max_days, g0, g1) or bad_period(p + 1, idx, min_days, max_days, g0, g1):
                continue
            if gap == "dec" and label(idx[p]) == "2025-12-01":
                continue
            elig = [u for u, toks in bought[p].items() if len(toks) >= min_tokens and (users is None or users(u))]
            pairs.append(tp.make_pair(f"{label(idx[p])}->{label(idx[p + 1])}", per[p], per[p + 1], elig))
        return pairs

    def summary(pairs: list[tp.Pair], tag: str, gate_ic: float, gate_q: float, perm: int = 0, show: bool = False) -> dict[str, Any]:
        ms = [tp.pair_metrics(p) for p in pairs]
        if show:
            print(f"  {'pair':24} {'n':>5} {'IC':>7} {'Q':>7} {'stay10':>6}")
            for m in ms:
                ic = f"{m.ic:+.3f}" if m.ic is not None else "  n/a"
                print(f"  {m.label:24} {m.n:5d} {ic:>7} {m.q:+.3f} {m.stay:6.2f}")
        gate = tp.power_gate(ms, min_pairs=8, min_median_n=300, mde_ic=gate_ic, mde_q=gate_q)
        ci_ic = tp.mean_ci([m.ic for m in ms if m.ic is not None])
        ci_q = tp.mean_ci([m.q for m in ms])
        out = {"tag": tag, "gate": gate, "ic": ci_ic, "q": ci_q, "pairs": [m.__dict__ for m in ms]}
        mi = gate["mde_ic"]
        mq = gate["mde_q"]
        print(
            f"{tag:34} T={gate['T']:2d} medN={gate['median_n']:6.0f} gate={'PASS' if gate['passed'] else 'FAIL'} "
            f"MDE_IC={mi if mi is None else round(mi, 3)} MDE_Q={mq if mq is None else round(mq, 3)} | "
            f"IC {fmt(ci_ic)} | Q {fmt(ci_q)}"
        )
        if perm:
            pl = tp.rank_placebo(pairs, n_perm=perm, seed=7)
            out["placebo"] = pl
            print(
                f"{'':34} placebo: p_IC={pl['p_ic']:.4f} p_Q={pl['p_q']:.4f} null mean IC={pl['null_ic_mean']:+.4f} Q={pl['null_q_mean']:+.4f}"
            )
        return out

    res: dict[str, Any] = {}
    print("\n== PRIMARY: monthly, settled P&L, >= 5 tokens bought in M ==")
    prim = build(settled, month_idx, bought_m, 5, 20)
    res["primary"] = summary(prim, "primary", 0.05, 0.03, perm=n_perm, show=True)
    res["descriptive"] = describe(prim)
    print("\n== robustness (monthly) ==")
    res["no_top1"] = summary([tp.drop_top_abs(p, 0.01) for p in prim], "without top 1% |P&L_M|", 0.05, 0.03, perm=n_perm)
    res["no_last"] = summary(prim[:-1], "without last pair (Apr 1-28)", 0.05, 0.03)
    res["all11"] = summary(build(settled, month_idx, bought_m, 5, 20, gap="none"), "all 11 pairs (gap months kept)", 0.05, 0.03, show=True)
    res["t7"] = summary(build(settled, month_idx, bought_m, 5, 20, gap="dec"), "T=7 (also without Dec->Jan)", 0.05, 0.03)
    res["realized"] = summary(build(realized, month_idx, bought_m, 5, 20), "realized P&L (secondary)", 0.05, 0.03, perm=n_perm, show=True)
    for mt in (1, 3, 10, 20):
        res[f"min{mt}"] = summary(build(settled, month_idx, bought_m, mt, 20), f"min tokens {mt} (grid, exploratory)", 0.05, 0.03)
    for pre in ("0x00", "0x01"):
        res[pre] = summary(
            build(settled, month_idx, bought_m, 5, 20, users=lambda u, pre=pre: u.startswith(pre)), f"prefix {pre}", 0.05, 0.03
        )
    print("\n== SECONDARY H1w: weekly, settled, >= 3 tokens bought in the week ==")
    wk = build(settled, week_idx, bought_w, 3, 6, gap="week")
    res["weekly"] = summary(wk, "weekly", 0.03, 0.03, perm=min(n_perm, 500))
    res["weekly_no_top1"] = summary([tp.drop_top_abs(p, 0.01) for p in wk], "weekly w/o top 1% (exploratory)", 0.03, 0.03)
    res["unpaid"] = unpaid_rows
    return res


def describe(pairs: list[Any]) -> dict[str, Any]:
    """Descriptive numbers of the primary pairs (not pre-registered; asked for by critic B, 30.10.2026)."""
    from tradedesk.analytics import trader_persistence as tp

    print("\n== DESCRIPTIVE (not pre-registered): top decile by P&L_M, outcome in M+1 (USDC) ==")
    print(f"  {'pair':24} {'n':>5} {'top mean':>9} {'top med':>8} {'rest mean':>9} {'top =0':>6} {'Q act':>7} {'Q bottom':>8}")
    ds = []
    for p in pairs:
        d = tp.describe_pair(p)
        ds.append({"label": p.label, **d})
        print(
            f"  {p.label:24} {d['n']:5d} {d['top_mean_next']:+9.0f} {d['top_median_next']:+8.0f} {d['rest_mean_next']:+9.0f} "
            f"{d['top_zero_share']:6.2f} {d['q_active']:+7.3f} {d['q_bottom']:+8.3f}"
        )
    out: dict[str, Any] = {"pairs": ds}
    for key in ("top_mean_next", "q_active", "q_bottom"):
        ci = tp.mean_ci([d[key] for d in ds])
        out[key] = ci
        pos = sum(1 for d in ds if d[key] > 0)
        print(f"  {key:14} mean over pairs {fmt(ci)} | > 0 in {pos}/{len(ds)}")
    w = [len(p.x) ** 0.5 for p in pairs]
    ms = [tp.pair_metrics(p) for p in pairs]
    out["sqrt_n_weighted"] = {
        "ic": tp.weighted_mean([m.ic for m in ms], w),
        "q": tp.weighted_mean([m.q for m in ms], w),
        "top_mean_next": tp.weighted_mean([d["top_mean_next"] for d in ds], w),
    }
    sw = out["sqrt_n_weighted"]
    print(
        f"  sqrt(n)-weighted means (pairs unweighted in the registered stats): IC {sw['ic']:+.3f} Q {sw['q']:+.3f} top mean ${sw['top_mean_next']:+,.0f}"
    )
    return out


def fmt(ci: dict[str, Any]) -> str:
    if ci.get("mean") is None:
        return "n/a"
    if ci.get("lo") is None:
        return f"{ci['mean']:+.3f}"
    return f"{ci['mean']:+.3f} [{ci['lo']:+.3f}; {ci['hi']:+.3f}] sd {ci['sd']:.3f}"


async def amain(a: argparse.Namespace) -> int:
    if a.cmd == "analyze":
        res = analyze(a.cache_dir, a.hi, a.perm)
        (a.cache_dir / f"results_{a.hi}.json").write_text(json.dumps(res, default=str, indent=1))
        print(f"full results: {a.cache_dir / f'results_{a.hi}.json'}; peak RSS {rss_mb():.0f} MB")
        return 0
    c = Client(a.cache_dir)
    try:
        if a.cmd == "probe":
            await probe(c)
        elif a.cmd == "probe_pnl":
            await probe_pnl(c)
        elif a.cmd == "probe_hist":
            await probe_hist(c)
        elif a.cmd == "probe_avail":
            await probe_avail(c)
        elif a.cmd == "probe_v1":
            await probe_v1(c)
        elif a.cmd == "fetch":
            await fetch(c, a)
        elif a.cmd == "check_change":
            await check_change(c, a)
    finally:
        await c.aclose()
    print(f"queries sent {c.sent}, cache hits {c.hits}, peak RSS {rss_mb():.0f} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["probe", "probe_pnl", "probe_hist", "probe_avail", "probe_v1", "fetch", "analyze", "check_change"])
    ap.add_argument("--cache-dir", type=Path, default=CACHE)
    ap.add_argument("--hi", default="0x02", help="sample = user ids below this (0x02: prefixes 0x00, 0x01)")
    ap.add_argument("--upto", type=int, default=None, help="fetch only the first N boundaries (timing sample)")
    ap.add_argument("--max-pages", type=int, default=1500)
    ap.add_argument("--perm", type=int, default=2000)
    ap.add_argument("--reverse", action="store_true", help="fetch boundaries from the last one back")
    return asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
