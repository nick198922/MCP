"""Stage 0 of IDEA-20261023-supply-from-unlock-schedules: DefiLlama unlock schedules as a free supply-growth history.

Data only (no prices, no returns). Criteria D1-D5 are pre-registered in the idea card.

    uv run python scripts/verify_unlock_schedules.py probe --offline   # replay from the cache, no request
    uv run python scripts/verify_unlock_schedules.py summarize         # cache-only summary quoted in the report

``probe`` reads the terms and documentation (DefiLlama terms page, docs, the free/pro API
lists of api-docs), the GitHub status of the adapters repository and its public copies (metadata only) and the
Wayback Machine indexes (CDX, metadata only). It downloads no schedule. The 2026-10-23 run stopped at D1: the terms
(Effective Date 24 June 2025, clause 8) forbid copying the materials, automated access to the Site and programmatic
access to any API other than the official public one, and unlock schedules are only in the paid Pro API
(see ``docs/verification/2026-10-23-unlock-schedules-stage0.md`` and Q-20261023-2). Fetch modes for D2-D5 are to be
written only after the owner's answer.

Failed responses are now cached as ``<name>.gz.failed`` markers. The 2026-10-23 run predates this, so its three
failures (``/terms-of-service`` 403, two GitHub 404) have no marker: without ``--offline`` the two GitHub requests
would be sent again (``/terms-of-service`` is no longer in the list). Use ``--offline`` to reproduce the report.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html
import json
import re
import resource
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.core.http import Http

UA = {"User-Agent": "tradedesk-research/0.1 (personal research; low rate)"}


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%H:%M:%S} rss={rss_mb():.0f}MB] {msg}", flush=True)


def wr_bytes(p: Path, b: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(gzip.compress(b))
    tmp.replace(p)


def rd_bytes(p: Path) -> bytes:
    return gzip.decompress(p.read_bytes())


def fail_marker(p: Path) -> Path:
    return p.with_suffix(p.suffix + ".failed")


async def cached_bytes(
    cache: Path, name: str, http: Http, path: str, params: dict[str, Any] | None = None, *, offline: bool = False
) -> bytes | None:
    """Return the cached response; failures are cached too (``<name>.failed``) so a replay sends no request."""
    p = cache / name
    if p.exists():
        return rd_bytes(p)
    fm = fail_marker(p)
    if fm.exists():
        log(f"FAIL(cached) {http.provider} {path} {params or ''}: {fm.read_text()[:160]}")
        return None
    if offline:
        log(f"MISS(offline) {http.provider} {path} {params or ''}: not in cache, no request sent")
        return None
    t0 = time.monotonic()
    try:
        b = (await http.get_bytes(path, params=params)).data
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__} {str(e)[:160]}"
        fm.parent.mkdir(parents=True, exist_ok=True)
        fm.write_text(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {msg}")
        log(f"FAIL {http.provider} {path} {params or ''}: {msg}")
        return None
    wr_bytes(p, b)
    log(f"got {http.provider} {path} {params or ''}: {len(b) / 1e6:.3f} MB in {time.monotonic() - t0:.1f}s")
    return b


async def probe(cache: Path, offline: bool = False) -> None:
    web = Http("defillama_web", "https://defillama.com", rate=0.5, burst=1, timeout=60, retries=1, headers=UA)
    docs = Http("defillama_docs", "https://docs.llama.fi", rate=0.5, burst=1, timeout=60, retries=1, headers=UA)
    apid = Http("defillama_apidocs", "https://api-docs.defillama.com", rate=0.5, burst=1, timeout=60, retries=1, headers=UA)
    gh = Http("github_api", "https://api.github.com", rate=0.5, burst=1, timeout=60, retries=1, headers=UA)
    wb = Http("wayback", "https://web.archive.org", rate=0.25, burst=1, timeout=90, retries=2, headers=UA)
    reqs: list[tuple[str, Http, str, dict[str, Any] | None]] = [
        # The terms define "Site" as https://defillama.com/ and forbid automated access to it (clause 8). The 2026-10-23
        # run sent two requests there (/terms and /terms-of-service, the latter Cloudflare 403); /terms-of-service is
        # removed and /terms is served from the cache. Read a service's terms before any scripted request to its domain.
        ("terms_web", web, "/terms", None),
        ("docs_root", docs, "/", None),
        ("apidocs_root", apid, "/", None),
        ("gh_repo", gh, "/repos/DefiLlama/emissions-adapters", None),
        ("apidocs_llms_free", apid, "/llms-free.txt", None),
        ("apidocs_llms_pro", apid, "/llms-pro.txt", None),
        ("gh_copy_omni", gh, "/repos/Omni-Chain-Protocols/emissions-adapters", None),
        ("gh_copy_define101", gh, "/repos/Define101/emissions-adapters", None),
        ("gh_copy_0xnirmal", gh, "/repos/0xnirmal/emissions-adapters", None),
        ("gh_server_repo", gh, "/repos/DefiLlama/defillama-server", None),
        ("gh_search", gh, "/search/repositories", {"q": "emissions-adapters", "per_page": 50}),
        ("gh_search2", gh, "/search/repositories", {"q": "emissions adapters defillama", "per_page": 50}),
        ("gh_code_search", gh, "/search/repositories", {"q": "org:DefiLlama emissions", "per_page": 50}),
        (
            "wb_ds_arbitrum",
            wb,
            "/cdx/search/cdx",
            {"url": "defillama-datasets.llama.fi/emissions/arbitrum", "output": "json", "fl": "timestamp,statuscode,length"},
        ),
        (
            "wb_ds_prefix",
            wb,
            "/cdx/search/cdx",
            {
                "url": "defillama-datasets.llama.fi/emissions/",
                "matchType": "prefix",
                "output": "json",
                "fl": "timestamp,original,statuscode,length",
                "collapse": "urlkey",
                "limit": 2000,
            },
        ),
        (
            "wb_unlocks_prefix",
            wb,
            "/cdx/search/cdx",
            {
                "url": "defillama.com/unlocks/",
                "matchType": "prefix",
                "output": "json",
                "fl": "timestamp,original,statuscode,length",
                "collapse": "urlkey",
                "limit": 3000,
            },
        ),
    ]
    try:
        for name, h, path, params in reqs:
            b = await cached_bytes(cache, f"probe/{name}.gz", h, path, params, offline=offline)
            if b is None:
                print(f"ERR {name}")
                continue
            txt = b.decode("utf-8", "replace")
            print(f"OK  {name} bytes={len(b)} :: {txt[:300]!r}")
    finally:
        for h in (web, docs, apid, gh, wb):
            await h.aclose()


def _cached_text(cache: Path, name: str) -> str | None:
    p = cache / "probe" / f"{name}.gz"
    return rd_bytes(p).decode("utf-8", "replace") if p.exists() else None


def summarize(cache: Path) -> None:
    """Cache-only summary used in the report (no network): api-docs grep, adapter copies, Wayback counts, clause 8."""
    for name in ("apidocs_llms_free", "apidocs_llms_pro"):
        txt = _cached_text(cache, name)
        if txt is None:
            print(f"{name}: not cached")
            continue
        hits = [ln.strip() for ln in txt.splitlines() if re.search(r"emission|unlock", ln, re.I)]
        print(f"{name}: {len(hits)} lines matching emission|unlock")
        for ln in hits[:12]:
            print(f"    {ln[:160]}")
    srch = _cached_text(cache, "gh_search")
    if srch is not None:
        js = json.loads(srch)
        names = [it["full_name"] for it in js.get("items", []) if it.get("name") == "emissions-adapters"]
        print(f"gh_search: total_count={js.get('total_count')} named emissions-adapters={len(names)} {names}")
    for name in ("gh_copy_omni", "gh_copy_define101", "gh_copy_0xnirmal"):
        txt = _cached_text(cache, name)
        if txt is None:
            print(f"{name}: not cached")
            continue
        r = json.loads(txt)
        print(
            f"{r.get('full_name')}: license={r.get('license')} fork={r.get('fork')} "
            f"created={str(r.get('created_at'))[:10]} pushed={str(r.get('pushed_at'))[:10]} branch={r.get('default_branch')}"
        )
    for name in ("wb_ds_arbitrum", "wb_ds_prefix", "wb_unlocks_prefix"):
        txt = _cached_text(cache, name)
        if txt is None:
            print(f"{name}: not cached")
            continue
        rows = json.loads(txt) if txt.strip() else []
        rows = rows[1:] if rows else rows  # header row
        if name != "wb_unlocks_prefix":
            print(f"{name}: {len(rows)} rows {rows[:5]}")
            continue
        ok = [r for r in rows if r[2] == "200" and "?" not in r[1]]
        root = [r for r in ok if r[1].rstrip("/").lower().endswith("defillama.com/unlocks")]
        ts = sorted(r[0] for r in ok)
        print(
            f"{name}: {len(rows)} distinct URLs; status 200 without query: {len(ok)} "
            f"(of which root /unlocks: {len(root)}, token pages: {len(ok) - len(root)}); "
            f"first captures {ts[0][:8] if ts else '-'}..{ts[-1][:8] if ts else '-'} (collapsed by urlkey)"
        )
    terms = _cached_text(cache, "terms_web")
    if terms is not None:
        plain = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", terms)))
        m = re.search(r"Effective Date:?\s*(\d{1,2} \w+ \d{4})", plain)
        print(f"terms_web: Effective Date {m.group(1) if m else '?'}")
        i = plain.find("you agree not to")
        j = plain.find("9. THIRD-PARTY", i)
        print(f"terms_web clause 8: {plain[i : j if j > i else i + 1100].strip() if i >= 0 else 'not found'}")


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("mode", choices=["probe", "summarize"])
    a.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/unlock_schedules")
    a.add_argument("--offline", action="store_true", help="probe: read the cache only, send no request")
    args = a.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "probe":
        asyncio.run(probe(args.cache_dir, offline=args.offline))
    else:
        summarize(args.cache_dir)


if __name__ == "__main__":
    main()
