"""Part B of IDEA-20261017-market-makers: on-chain flows of labelled market-maker wallets (H3).

Stages (each prints a compact summary; raw responses go to the per-file cache under --cache-dir):
  prereg   SHA-256 of the card section «Попередня реєстрація (частина B)» (up to the results heading).
  probe    data availability only: Dune API access, label tables, counts of labelled MM / CEX addresses on
           Ethereum and the date each label was added (point-in-time quality); one Etherscan call. No prices,
           no transfers aggregated into events, no outcomes.
  stage0   registered event count, clusters and power gate (MDE from the placebo SD), no event means.

Keys: ETHERSCAN_API_KEY / DUNE_API_KEY are read from ~/.config/tradedesk/onchain.env; never printed, never cached
(Http strips key-like params from evidence; the key goes to Dune as a header).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from tg_send import load_env  # noqa: E402

from tradedesk.core.errors import ProviderError  # noqa: E402
from tradedesk.core.http import Http  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-market-makers.md"
PREREG_HEAD = "## Попередня реєстрація (частина B"
RESULTS_HEAD = "\n## Частина B: результати"  # a heading at line start, not the mention inside the section
ENV_FILE = Path.home() / ".config" / "tradedesk" / "onchain.env"
DEFAULT_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "market_makers_onchain"

# Market-maker firms named in the card and in the part C set (names as they appear in public label sets).
MM_NAMES = [
    "wintermute",
    "jump",
    "gsr",
    "dwf",
    "amber",
    "cumberland",
    "flow traders",
    "b2c2",
    "galaxy",
    "auros",
    "kronos",
    "gotbit",
    "web3port",
    "alameda",
    "fbg",
    "selini",
    "flowdesk",
    "keyrock",
    "presto",
    "qcp",
    "abraxas",
    "folkvang",
    "tokka",
    "efficient frontier",
    "nonco",
    "caladan",
    "hashed",
    "market maker",
    "market-maker",
]


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def keys() -> dict[str, str]:
    env = load_env(ENV_FILE)
    return {k: env.get(k, "") for k in ("ETHERSCAN_API_KEY", "DUNE_API_KEY")}


def scrub(text: str, secrets: dict[str, str]) -> str:
    for v in secrets.values():
        if v:
            text = text.replace(v, "***")
    return text


def prereg_hash() -> tuple[str, int]:
    text = CARD.read_text(encoding="utf-8")
    start = text.index(PREREG_HEAD)
    end = text.find(RESULTS_HEAD, start)
    sect = text[start : end if end >= 0 else len(text)].rstrip() + "\n"
    raw = sect.encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


class Dune:
    """Minimal Dune API client over core.http.Http: execute ad-hoc SQL, poll, read results; caches by SQL hash."""

    def __init__(self, key: str, cache: Path) -> None:
        self.http = Http("dune", "https://api.dune.com", rate=0.5, timeout=60.0, headers={"X-Dune-API-Key": key})
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)

    async def sql(self, name: str, sql: str, *, max_wait: float = 600.0) -> dict[str, Any]:
        h = hashlib.sha256(sql.encode()).hexdigest()[:12]
        f = self.cache / f"dune_{name}_{h}.json"
        if f.exists():
            return json.loads(f.read_text())
        t0 = time.time()
        ex = await self.http.post_json("/api/v1/sql/execute", json={"sql": sql})
        eid = ex.data.get("execution_id")
        state = ex.data.get("state")
        print(f"  dune {name}: execution {eid} {state}", flush=True)
        while True:
            st = await self.http.get_json(f"/api/v1/execution/{eid}/status")
            state = st.data.get("state")
            if state in ("QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"):
                break
            if time.time() - t0 > max_wait:
                raise ProviderError("dune", "timeout", f"execution {eid} still {state}")
            await asyncio.sleep(5)
        if state != "QUERY_STATE_COMPLETED":
            out = {"state": state, "error": st.data.get("error"), "rows": []}
        else:
            rows: list[dict[str, Any]] = []
            offset = 0
            while True:
                res = await self.http.get_json(f"/api/v1/execution/{eid}/results", params={"limit": 25000, "offset": offset})
                part = (res.data.get("result") or {}).get("rows") or []
                rows.extend(part)
                nxt = res.data.get("next_offset")
                if not nxt or not part:
                    break
                offset = int(nxt)
            out = {
                "state": state,
                "rows": rows,
                "credits": st.data.get("execution_cost_credits"),
                "seconds": round(time.time() - t0, 1),
            }
        f.write_text(json.dumps(out))
        return out


async def probe(cache: Path, secrets: dict[str, str]) -> None:
    print(f"keys present: etherscan={bool(secrets['ETHERSCAN_API_KEY'])} dune={bool(secrets['DUNE_API_KEY'])}")
    dune = Dune(secrets["DUNE_API_KEY"], cache)
    like = " OR ".join(f"lower(name) LIKE '%{n}%'" for n in MM_NAMES)
    queries = {
        "tables": (
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('labels','cex','cex_ethereum','cex_evms') ORDER BY 1,2"
        ),
        "mm_labels": (
            "SELECT name, category, label_type, source, model_name, count(*) AS n, "
            "min(created_at) AS first_added, max(created_at) AS last_added "
            "FROM labels.addresses WHERE blockchain = 'ethereum' AND (" + like + ") "
            "GROUP BY 1,2,3,4,5 ORDER BY n DESC LIMIT 300"
        ),
        "categories": (
            "SELECT category, count(*) AS n FROM labels.addresses WHERE blockchain = 'ethereum' GROUP BY 1 ORDER BY 2 DESC LIMIT 60"
        ),
        "cex": (
            "SELECT cex_name, count(*) AS n, min(first_used) AS first_used FROM cex.addresses "
            "WHERE blockchain = 'ethereum' GROUP BY 1 ORDER BY 2 DESC LIMIT 40"
        ),
    }
    for name, sql in queries.items():
        try:
            res = await dune.sql(name, sql)
        except ProviderError as e:
            print(f"[{name}] ProviderError {e.reason}: {scrub(str(e.detail), secrets)}")
            continue
        rows = res.get("rows") or []
        print(f"[{name}] state={res.get('state')} rows={len(rows)} credits={res.get('credits')} err={res.get('error')}")
        for r in rows[:40]:
            print("   ", json.dumps(r, default=str)[:220])
        print(f"   rss={rss_mb():.0f} MB")
    eth = Http("etherscan", "https://api.etherscan.io", rate=2.0, timeout=30.0)
    try:
        got = await eth.get_json(
            "/v2/api",
            params={
                "chainid": 1,
                "module": "account",
                "action": "tokentx",
                "address": "0xdbf5e9c5206d0db70a90108bf936da60221dc080",
                "page": 1,
                "offset": 5,
                "sort": "asc",
                "apikey": secrets["ETHERSCAN_API_KEY"],
            },
        )
        d = got.data
        print(f"[etherscan tokentx] status={d.get('status')} message={d.get('message')} n={len(d.get('result') or [])}")
        for r in (d.get("result") or [])[:3]:
            if isinstance(r, dict):
                print("   ", {k: r.get(k) for k in ("timeStamp", "from", "to", "tokenSymbol", "value", "tokenDecimal")})
        tag = await eth.get_json(  # nametag: answered 2026-10-25 "API Exclusive endpoint" (paid tier)
            "/v2/api",
            params={
                "chainid": 1,
                "module": "nametag",
                "action": "getaddresstag",
                "address": "0xdbf5e9c5206d0db70a90108bf936da60221dc080",
                "apikey": secrets["ETHERSCAN_API_KEY"],
            },
        )
        print(f"[etherscan nametag] {scrub(json.dumps(tag.data)[:300], secrets)}")
    except ProviderError as e:
        print(f"[etherscan] ProviderError {e.reason}: {scrub(str(e.detail), secrets)}")
    await eth.aclose()
    await dune.http.aclose()


async def diag(cache: Path, secrets: dict[str, str]) -> None:
    """Which Dune access paths the free key has: tiny ad-hoc SQL, results of an existing query, usage."""
    dune = Dune(secrets["DUNE_API_KEY"], cache)
    for label, call in (
        ("sql SELECT 1", lambda: dune.http.post_json("/api/v1/sql/execute", json={"sql": "SELECT 1 AS x"})),
        ("query/1/results limit=1", lambda: dune.http.get_json("/api/v1/query/1/results", params={"limit": 1})),
        ("usage", lambda: dune.http.post_json("/api/v1/usage", json={})),
        ("usage GET", lambda: dune.http.get_json("/api/v1/usage")),
    ):
        try:
            got = await call()
            print(f"[{label}] ok: {scrub(json.dumps(got.data, default=str)[:400], secrets)}")
        except ProviderError as e:
            print(f"[{label}] ProviderError {e.reason}: {scrub(str(e.detail), secrets)}")
    for qid in (1258228, 2030664, 3237721, 1747157, 4000000):
        try:
            got = await dune.http.get_json(f"/api/v1/query/{qid}/results", params={"limit": 1})
            print(f"[query/{qid}/results] ok rows={len((got.data.get('result') or {}).get('rows') or [])}")
        except ProviderError as e:
            print(f"[query/{qid}/results] ProviderError {e.reason}: {scrub(str(e.detail), secrets)[:120]}")
    await dune.http.aclose()
    gh = Http("github", "https://api.github.com", rate=1.0, timeout=30.0)
    for repo in ("brianleect/etherscan-labels", "dawsbot/eth-labels"):
        try:
            meta = await gh.get_json(f"/repos/{repo}")
            br = meta.data.get("default_branch")
            print(f"[{repo}] branch={br} pushed_at={meta.data.get('pushed_at')} size_kb={meta.data.get('size')}")
            tree = await gh.get_json(f"/repos/{repo}/git/trees/{br}", params={"recursive": 1})
            items = [t for t in tree.data.get("tree", []) if t.get("type") == "blob"]
            (cache / f"gh_tree_{repo.replace('/', '_')}.json").write_text(json.dumps(items))
            hits = [t for t in items if any(s in t["path"].lower() for s in ("market", "maker", "trading", "combined"))]
            print(f"   blobs={len(items)}; matching paths={len(hits)}")
            for t in hits[:25]:
                print(f"    {t['path']} {t.get('size')}")
            big = sorted(items, key=lambda t: -(t.get("size") or 0))[:8]
            for t in big:
                print(f"    [big] {t['path']} {t.get('size')}")
        except ProviderError as e:
            print(f"[{repo}] ProviderError {e.reason}: {e.detail[:160]}")
    await gh.aclose()


LABEL_FILES = {
    # snapshot of Etherscan labels scraped 2023-10 (repo frozen since 2023-10-01): point-in-time for later events
    "brianleect_2023": (
        "https://raw.githubusercontent.com",
        "/brianleect/etherscan-labels/main/data/etherscan/combined/combinedAccountLabels.json",
    ),
    # eth-labels.com dump, refreshed to 2026-09: current labels (look-ahead for older events)
    "dawsbot_2026": ("https://raw.githubusercontent.com", "/dawsbot/eth-labels/v1/data/csv/accounts.csv"),
}


async def fetch_labels(cache: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name, (base, path) in LABEL_FILES.items():
        f = cache / f"labels_{name}{Path(path).suffix}"
        if not f.exists():
            http = Http("github", base, rate=1.0, timeout=120.0)
            got = await http.get_bytes(path)
            f.write_bytes(got.data)
            await http.aclose()
        out[name] = f
    return out


def mm_firm(text: str) -> str | None:
    t = text.lower()
    for n in MM_NAMES:
        if n in t:
            return n
    return None


async def labels_stage(cache: Path) -> None:
    import csv
    import io
    from collections import Counter

    files = await fetch_labels(cache)
    b = json.loads(files["brianleect_2023"].read_text())
    print(f"brianleect_2023: {len(b)} addresses; example value: {json.dumps(next(iter(b.values())))[:200]}")
    old: dict[str, tuple[str, str]] = {}
    for addr, v in b.items():
        name = str(v.get("name", "")) if isinstance(v, dict) else str(v)
        labels = " ".join(v.get("labels", [])) if isinstance(v, dict) else ""
        firm = mm_firm(name) or mm_firm(labels)
        if firm:
            old[addr.lower()] = (firm, f"{name} | {labels}"[:90])
    rows = list(csv.DictReader(io.StringIO(files["dawsbot_2026"].read_text(encoding="utf-8", errors="replace"))))
    print(f"dawsbot_2026: {len(rows)} rows; columns {list(rows[0].keys()) if rows else []}")
    new: dict[str, tuple[str, str]] = {}
    for r in rows:
        if (r.get("chainId") or r.get("chain_id") or "1") not in ("1", "ethereum"):
            continue
        txt = " ".join(str(v) for k, v in r.items() if k and k.lower() in ("label", "nametag", "name_tag", "name"))
        firm = mm_firm(txt)
        if firm:
            new[(r.get("address") or "").lower()] = (firm, txt[:90])
    for tag, d in (("2023", old), ("2026", new)):
        c = Counter(f for f, _ in d.values())
        print(f"[{tag}] MM-name addresses: {len(d)}; by firm: {dict(c.most_common())}")
    both = set(old) & set(new)
    print(f"in both: {len(both)}; only 2026: {len(set(new) - set(old))}")
    sample = sorted(old.items())[:0] + sorted(new.items())
    (cache / "mm_label_matches.json").write_text(json.dumps({"old": old, "new": new}))
    for a, (f, t) in sample[:40]:
        print(f"   {a} {f:12s} {t}")
    print(f"rss={rss_mb():.0f} MB")


PERIOD = ("2023-10-01", "2026-09-26")  # event days; 3-day outcomes stay inside the klines cache (to 2026-09-30)
DATA_DIR = Path.home() / ".cache" / "tradedesk-agents" / "funding_crowding"  # Binance USD-M k1d / funding cache
PAGE = 1000  # Etherscan free tier returns at most 1000 rows per tokentx call (seen 2026-10-25)
# Crawl cache. tx2/: first full crawl (only rows to exchanges, raw page counts incl. refetched boundary blocks);
# tx3/: recrawl of the same blocks with next_page(), deduplicated counts and outgoing aggregates (fix round 25.10).
# tx_STALE_buggy_paging/: the very first crawl, where a 1000-row page was taken as the last one (do not use).
TX_DIR = "tx3"


def wallet_sets(cache: Path) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]], set[str]]:
    """(main MM wallets from the 2023-10 snapshot, sensitivity MM wallets from the 2026 dump, exchange addresses)."""
    import csv
    import io

    from tradedesk.analytics.mm_onchain import is_exchange_address, mm_firm

    b = json.loads((cache / "labels_brianleect_2023.json").read_text())
    main_w: dict[str, tuple[str, str]] = {}
    ex: set[str] = set()
    for addr, v in b.items():
        name = str(v.get("name", ""))
        labs = " ".join(v.get("labels", []))
        f = mm_firm(name)
        if f:
            main_w[addr.lower()] = (f, name)
        elif is_exchange_address(name, labs):
            ex.add(addr.lower())
    sens: dict[str, tuple[str, str]] = {}
    text = (cache / "labels_dawsbot_2026.csv").read_text(encoding="utf-8", errors="replace")
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get("chainId") or "") != "1":
            continue
        a, tag, lab = (r.get("address") or "").lower(), r.get("nameTag") or "", r.get("label") or ""
        f = mm_firm(tag)
        if f:
            sens[a] = (f, tag)
        elif is_exchange_address(tag, lab):
            ex.add(a)
    return main_w, sens, ex - set(main_w) - set(sens)


class Etherscan:
    def __init__(self, key: str) -> None:
        self.key = key
        self.http = Http("etherscan", "https://api.etherscan.io", rate=1.6, burst=1, timeout=60.0, retries=4)

    async def call(self, **params: Any) -> Any:
        got = await self.http.get_json("/v2/api", params={"chainid": 1, **params, "apikey": self.key})
        return got.data

    async def block_at(self, day: str) -> int:
        from datetime import datetime

        ts = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())
        d = await self.call(module="block", action="getblocknobytime", timestamp=ts, closest="before")
        return int(d["result"])


async def crawl(cache: Path, secrets: dict[str, str], max_pages: int, only: str | None) -> None:
    """Outgoing ERC-20 transfers of every MM wallet (main ∪ sensitivity) to exchange addresses, page by page.
    Each page is cached as soon as it arrives (the rows to exchanges, counts over all outgoing transfers by contract
    and by receiver, and the number of rows without the refetched boundary block), so a restart resumes."""
    from collections import Counter

    from tradedesk.analytics.mm_onchain import next_page

    main_w, sens, ex = wallet_sets(cache)
    wallets = dict(sens)
    wallets.update(main_w)
    print(f"wallets: main {len(main_w)}, sensitivity {len(sens)}, union {len(wallets)}; exchange addresses {len(ex)}")
    es = Etherscan(secrets["ETHERSCAN_API_KEY"])
    bf = cache / "blocks.json"
    if bf.exists():
        b0, b1 = json.loads(bf.read_text())
    else:
        b0, b1 = await es.block_at(PERIOD[0]), await es.block_at("2026-09-27")
        bf.write_text(json.dumps([b0, b1]))
    print(f"blocks {b0} .. {b1}")
    t_start = time.time()
    calls = 0
    for i, (addr, (firm, tag)) in enumerate(sorted(wallets.items(), key=lambda kv: kv[1])):
        if only and only.lower() not in (addr + firm).lower():
            continue
        wd = cache / TX_DIR / addr
        wd.mkdir(parents=True, exist_ok=True)
        done = wd / "done.json"
        if done.exists():
            continue
        start, pages, n_raw, n_keep = b0, 0, 0, 0
        truncated: list[int] = []
        prev = sorted(wd.glob("p_*.json"))
        if prev:
            last = json.loads(prev[-1].read_text())
            start, pages = last["next_start"], len(prev)
            truncated = [json.loads(p.read_text())["start"] for p in prev if json.loads(p.read_text()).get("truncated")]
        while pages < max_pages:
            try:
                d = await es.call(
                    module="account", action="tokentx", address=addr, startblock=start, endblock=b1,
                    page=1, offset=PAGE, sort="asc",
                )  # fmt: skip
            except ProviderError as e:
                print(f"  {addr} {firm}: ProviderError {e.reason}: {scrub(str(e.detail), secrets)[:120]}")
                break
            calls += 1
            res = d.get("result")
            if not isinstance(res, list) and "rate limit" in str(res).lower() and calls % 50 != 49:
                await asyncio.sleep(2.0)  # Etherscan answers 200 + NOTOK on its 3/s limit: retry the same page
                continue
            if not isinstance(res, list):
                print(f"  {addr} {firm}: {d.get('message')} {scrub(str(res), secrets)[:120]}")
                if "No transactions" in str(d.get("message")) or "No transactions" in str(res):
                    done.write_text(json.dumps({"pages": pages}))
                break
            # a full page may cut its last block in half: next_page drops that block and restarts from it
            st = next_page(res, start, b1, PAGE)
            out = [r for r in st.kept if str(r.get("from", "")).lower() == addr]
            keep = [
                [int(r["timeStamp"]), int(r["blockNumber"]), r["hash"], r["to"].lower(), r["contractAddress"].lower(),
                 (r.get("tokenSymbol") or "").upper(), r["value"], int(r.get("tokenDecimal") or 0)]
                for r in out
                if r.get("to", "").lower() in ex
            ]  # fmt: skip
            # aggregates over ALL outgoing transfers (registered collision rule; receiver counts for planning B2)
            by_contract = Counter(f"{(r.get('tokenSymbol') or '').upper()}|{r['contractAddress'].lower()}" for r in out)
            receivers = Counter(r["to"].lower() for r in out)
            (wd / f"p_{pages:05d}.json").write_text(
                json.dumps({
                    "start": start, "n_raw": len(res), "next_start": st.next_start, "n_unique": len(st.kept),
                    "n_out": len(out), "truncated": st.truncated, "rows": keep,
                    "out_by_contract": dict(by_contract), "receivers": dict(receivers),
                })
            )  # fmt: skip
            pages += 1
            n_raw += len(st.kept)
            n_keep += len(keep)
            if st.truncated:  # >= PAGE transfers inside one block: the rest of that block is lost
                print(f"  {addr}: block {start} has >= {PAGE} transfers; kept the first {PAGE}, moving on")
                truncated.append(start)
            if st.done:
                done.write_text(json.dumps({"pages": pages, "truncated_blocks": truncated}))
                break
            start = st.next_start
        print(
            f"[{i + 1}/{len(wallets)}] {firm:12s} {tag[:30]:30s} {addr[:10]} pages={pages} raw={n_raw} to_cex={n_keep} "
            f"done={done.exists()} calls={calls} t={time.time() - t_start:.0f}s rss={rss_mb():.0f}MB",
            flush=True,
        )
    await es.http.aclose()
    print("crawl finished", flush=True)


def load_bars(sym: str) -> dict[str, Any]:
    from tradedesk.analytics.mm_onchain import Bar, utc_day

    out: dict[str, Any] = {}
    d = DATA_DIR / "k1d" / sym
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        for r in json.loads(p.read_text()) or []:
            out[utc_day(int(r[0]) // 1000)] = Bar(float(r[1]), float(r[4]), float(r[6]))
    return out


def load_transfers(
    cache: Path, wallets: dict[str, tuple[str, str]], ex: set[str], tx_dir: str = TX_DIR
) -> tuple[list[tuple[str, Any]], dict[tuple[str, str], int]]:
    """([(firm, Transfer)] to exchange addresses in ``ex``, {(symbol, contract): outgoing transfers to anyone}) for
    the given wallets from the crawl cache (one page file at a time)."""
    from tradedesk.analytics.mm_onchain import Transfer

    out: list[tuple[str, Any]] = []
    by_contract: dict[tuple[str, str], int] = {}
    for addr, (firm, _) in wallets.items():
        for p in sorted((cache / tx_dir / addr).glob("p_*.json")):
            page = json.loads(p.read_text())
            for ts, _blk, _h, to, contract, sym, value, dec in page["rows"]:
                if to not in ex:
                    continue
                amt = int(value) / 10**dec if value.isdigit() else float("nan")
                out.append((firm, Transfer(ts, addr, to, contract, sym, amt)))
            for k, n in page.get("out_by_contract", {}).items():
                sym, _, c = k.partition("|")
                by_contract[(sym, c)] = by_contract.get((sym, c), 0) + n
    return out, by_contract


def wallet_counts(cache: Path, wallets: dict[str, tuple[str, str]], ex: set[str]) -> None:
    """Per wallet: pages, rows as returned (boundary blocks counted twice), rows without repeats, outgoing rows,
    distinct receivers, rows to exchanges; plus a row-by-row comparison of the tx2 and tx3 crawls."""
    from collections import Counter

    print(f"{'firm':12s} {'wallet':10s} {'pages':>5s} {'raw':>8s} {'unique':>8s} {'out':>8s} {'recv':>6s} {'to_cex':>6s}")
    tot = [0, 0, 0, 0, 0]
    for addr, (firm, _) in sorted(wallets.items(), key=lambda kv: kv[1]):
        pages = sorted((cache / TX_DIR / addr).glob("p_*.json"))
        raw = uniq = n_out = cex = 0
        recv: set[str] = set()
        for p in pages:
            d = json.loads(p.read_text())
            raw, uniq, n_out = raw + d["n_raw"], uniq + d["n_unique"], n_out + d["n_out"]
            cex += sum(1 for r in d["rows"] if r[3] in ex)
            recv.update(d.get("receivers", {}))
        old = Counter(
            tuple(r) for p in sorted((cache / "tx2" / addr).glob("p_*.json")) for r in json.loads(p.read_text())["rows"]
        )  # fmt: skip
        new = Counter(tuple(r) for p in pages for r in json.loads(p.read_text())["rows"])
        flag = "" if old == new else f"  tx2 vs tx3 rows differ: only tx2 {sum((old - new).values())}, only tx3 {sum((new - old).values())}"
        print(f"{firm:12s} {addr[:10]} {len(pages):5d} {raw:8d} {uniq:8d} {n_out:8d} {len(recv):6d} {cex:6d}{flag}")
        for i, v in enumerate((len(pages), raw, uniq, n_out, cex)):
            tot[i] += v
    print(f"{'total':12s} {'':10s} {tot[0]:5d} {tot[1]:8d} {tot[2]:8d} {tot[3]:8d} {'':6s} {tot[4]:6d}")


def stage0(cache: Path) -> None:
    import random
    import statistics
    from collections import Counter

    from tradedesk.analytics.mm_onchain import (
        EXCLUDED_SYMBOLS,
        daily_token_flows,
        find_events,
        mde,
        pick_contracts,
        pick_contracts_from_counts,
        shift,
        short_return,
    )

    main_w, sens, ex = wallet_sets(cache)
    pending = [a for a in {**sens, **main_w} if not (cache / TX_DIR / a / "done.json").exists()]
    print(f"crawl incomplete for {len(pending)} wallets: {[a[:10] for a in pending]}" if pending else "crawl complete")
    print(f"exchange addresses (is_exchange_address, both dumps): {len(ex)}")
    wallet_counts(cache, {**sens, **main_w}, ex)
    um = set(json.loads((DATA_DIR / "symbols.json").read_text()))
    bars_cache: dict[str, dict[str, Any]] = {}

    def perp_of(sym: str) -> tuple[str | None, float]:
        for name, scale in ((f"{sym}USDT", 1.0), (f"1000{sym}USDT", 1e-3), (f"1000000{sym}USDT", 1e-6)):
            if name in um:
                return name, scale
        return None, 1.0

    def run(label: str, wallets: dict[str, tuple[str, str]], min_share: float, min_usd: float, gate: bool) -> None:
        tr, out_counts = load_transfers(cache, wallets, ex)
        # registered collision rule: counts over all outgoing MM transfers; the first run had only exchange rows
        keep, dropped = pick_contracts_from_counts(out_counts)
        keep_cex, dropped_cex = pick_contracts(t for _, t in tr)
        flows = daily_token_flows((t for _, t in tr), keep)
        by_firm: dict[tuple[str, str], Counter[str]] = {}
        for firm, t in tr:
            if keep.get(t.symbol) == t.contract and t.symbol not in EXCLUDED_SYMBOLS:
                from tradedesk.analytics.mm_onchain import utc_day

                by_firm.setdefault((t.symbol, utc_day(t.ts)), Counter())[firm] += t.amount
        perp_flows: dict[tuple[str, str], float] = {}
        no_perp: Counter[str] = Counter()
        for (sym, day), amt in flows.items():
            perp, scale = perp_of(sym)
            if perp is None:
                no_perp[sym] += 1
                continue
            perp_flows[(perp, day)] = perp_flows.get((perp, day), 0.0) + amt * scale
            if perp not in bars_cache:
                bars_cache[perp] = load_bars(perp)
        evs = find_events(perp_flows, bars_cache, min_share=min_share, min_usd=min_usd, first_day=PERIOD[0], last_day=PERIOD[1])
        days = sorted({e.day for e in evs})
        print(
            f"\n[{label}] share>={min_share:.0%} usd>={min_usd / 1e3:.0f}k: transfers to CEX {len(tr)}, "
            f"token-days {len(flows)} ({len(perp_flows)} with perp), events n={len(evs)}, date clusters k={len(days)}"
        )
        if not gate:
            return
        print(f"  symbols without a Binance perp (token-days): {dict(no_perp.most_common(12))}")
        cex_syms = {t.symbol for _, t in tr}
        print(
            f"  collision rule (all outgoing): symbols with >1 contract {len(dropped)}; among symbols sent to exchanges: "
            f"{ {s: len(c) for s, c in sorted(dropped.items()) if s in cex_syms} }; exchange-rows-only rule: "
            f"{ {s: len(c) for s, c in sorted(dropped_cex.items())} }; kept contract differs: "
            f"{sorted(s for s in cex_syms if keep.get(s) != keep_cex.get(s))}"
        )
        n_flow_rows = sum(1 for _, t in tr if keep.get(t.symbol) == t.contract)
        print(f"  exchange rows on the kept contracts: {n_flow_rows} of {len(tr)}")
        print(f"  events by token: {dict(Counter(e.symbol for e in evs).most_common())}")
        print(f"  events by year: {dict(Counter(e.day[:4] for e in evs))}")
        for e in evs[:40]:
            firms = by_firm.get((e.symbol.removeprefix("1000").removesuffix("USDT"), e.day), Counter())
            print(f"   {e.day} {e.symbol:14s} usd={e.usd / 1e6:7.2f}M share={e.share:6.1%} firms={dict(firms)}")
        # power gate: SD of placebo 3-day short returns (no event outcomes are computed here)
        rng = random.Random(20261025)
        ev_days: dict[str, list[str]] = {}
        for e in evs:
            ev_days.setdefault(e.symbol, []).append(e.day)
        plac: list[float] = []
        for e in evs:
            bars = bars_cache[e.symbol]
            cand = [shift(e.day, -k) for k in range(7, 121)]
            cand = [c for c in cand if all(abs(_d(c) - _d(x)) > 7 for x in ev_days[e.symbol])]
            for c in rng.sample(cand, min(20, len(cand))):
                r = short_return(bars, c, 3)
                if r is not None:
                    plac.append(r)
        sd = statistics.pstdev(plac) if len(plac) > 1 else float("nan")
        m = mde(sd, len(days))
        ok = len(days) >= 30 and m <= 0.02
        print(f"  placebo 3d returns n={len(plac)} SD={sd:.4f}; MDE=2.8*SD/sqrt(k)={m:.4f} (gate: k>=30 and MDE<=0.020)")
        print(f"  GATE {'PASSED' if ok else 'FAILED'}")

    run("main: 2023-10 snapshot wallets", main_w, 0.02, 250_000, True)
    grid = [(s, u) for s in (0.01, 0.02, 0.05) for u in (100_000, 250_000, 1_000_000) if (s, u) != (0.02, 250_000)]
    for ms_, mu in grid:  # the full registered 3 x 3 grid (the first run had 4 of 9 / 2 of 9 cells)
        run("main grid (counts only)", main_w, ms_, mu, False)
    run("sensitivity: 2026 dump wallets", sens, 0.02, 250_000, True)
    for ms_, mu in grid:
        run("sensitivity grid (counts only)", sens, ms_, mu, False)
    no_wm = {a: v for a, v in sens.items() if v[0] != "Wintermute"}
    run("sensitivity without Wintermute (counts only)", no_wm, 0.02, 250_000, False)
    print(f"rss={rss_mb():.0f} MB")


def _d(day: str) -> int:
    from datetime import date

    return date.fromisoformat(day).toordinal()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prereg", "probe", "diag", "labels", "wallets", "crawl", "stage0"])
    ap.add_argument("--max-pages", type=int, default=10_000)
    ap.add_argument("--only", default=None)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    a = ap.parse_args()
    if a.stage == "prereg":
        h, n = prereg_hash()
        print(f"prereg sha256={h} bytes={n}")
        return
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "labels":
        asyncio.run(labels_stage(a.cache_dir))
        return
    if a.stage == "stage0":
        stage0(a.cache_dir)
        return
    if a.stage == "wallets":
        main_w, sens, ex = wallet_sets(a.cache_dir)
        print(f"exchange addresses: {len(ex)}")
        for tag, d in (("main(2023)", main_w), ("sens(2026)", sens)):
            print(f"{tag}: {len(d)}")
            for addr, (f, t) in sorted(d.items(), key=lambda kv: kv[1]):
                print(f"   {addr} {f:12s} {t[:60]}")
        return
    secrets = keys()
    if a.stage == "crawl":
        asyncio.run(crawl(a.cache_dir, secrets, a.max_pages, a.only))
        return
    asyncio.run((diag if a.stage == "diag" else probe)(a.cache_dir, secrets))


if __name__ == "__main__":
    main()
