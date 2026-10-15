"""Stage-0 probe for IDEA-20261015-token-unlock-short-drift: which free sources expose dated unlock schedules.

Prints status, shape and a short preview for each candidate endpoint and stores raw bodies in the cache dir.
Network only through ``core.http.Http`` (rate limit, retries). No keys are used.

Usage:
    uv run python scripts/probe_token_unlocks.py --cache-dir ~/.cache/tradedesk-agents/token_unlock/probe
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from tradedesk.core.http import Http

CANDIDATES: list[tuple[str, str, dict[str, Any]]] = [
    ("https://api.llama.fi", "/emissions", {}),
    ("https://api.llama.fi", "/emission/arbitrum", {}),
    ("https://api.llama.fi", "/api/emissions", {}),
    ("https://defillama-datasets.llama.fi", "/emissions/arbitrum", {}),
    ("https://api.coingecko.com/api/v3", "/coins/arbitrum/market_chart", {"vs_currency": "usd", "days": "max"}),
    ("https://api.coingecko.com/api/v3", "/coins/arbitrum/market_chart", {"vs_currency": "usd", "days": "365"}),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", type=Path, required=True)
    ap.add_argument("--extra", nargs="*", default=[], help="extra base|path pairs")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    cands = list(CANDIDATES)
    for e in a.extra:
        base, path = e.split("|", 1)
        params: dict[str, Any] = {}
        if "?" in path:
            path, q = path.split("?", 1)
            params = dict(kv.split("=", 1) for kv in q.split("&"))
        cands.append((base, path, params))
    for base, path, params in cands:
        h = Http("probe", base, rate=0.5, burst=1, timeout=60, retries=0)
        tag = (base.split("//")[1].split(".")[0] + path + "_" + "_".join(f"{k}{v}" for k, v in params.items())).replace("/", "_")
        try:
            f = await h.get_json(path, params=params)
            s = json.dumps(f.data)
            (a.cache_dir / f"{tag}.json").write_text(s)
            print(f"OK  {base}{path} {params} type={type(f.data).__name__} bytes={len(s)} :: {s[:400]}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"ERR {base}{path} {params} {type(e).__name__}: {str(e)[:300]}", flush=True)
        finally:
            await h.aclose()
        await asyncio.sleep(2.5)


if __name__ == "__main__":
    asyncio.run(main())
