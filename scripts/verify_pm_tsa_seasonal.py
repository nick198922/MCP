"""Step 0 for IDEA-20261112-pm-tsa-daily-seasonal-nowcast: is the Polymarket TSA-passengers series still listed?

Stage ``gate`` (hard gate of the card): list every Polymarket event about TSA passenger counts through Gamma
(`/events/keyset` with ``tag_slug=tsa``, open and closed; `/public-search` for several phrasings, active and
closed; and every series those events belong to) and report whether any event ends, starts or was created after
the last known one (2026-07-05). Prints only listing metadata (dates, counts, volume): no prices, no outcomes.

Network only through the runtime providers (``rt.polymarket``); per-file JSON cache under ``--cache-dir``
(default ``~/.cache/tradedesk-agents/pm-tsa-daily-seasonal-nowcast``).

    uv run python scripts/verify_pm_tsa_seasonal.py gate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import resource
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm-tsa-daily-seasonal-nowcast").expanduser()
QUERIES = [
    "Number of TSA passengers",
    "TSA passengers",
    "TSA checkpoint",
    "TSA",
    "Thanksgiving TSA",
    "Christmas TSA",
    "air travel passengers",
]
TSA_RX = re.compile(r"\btsa\b", re.I)
LAST_KNOWN = "2026-07-05"


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _slim(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(ev.get("id")),
        "slug": ev.get("slug"),
        "title": ev.get("title"),
        "startDate": ev.get("startDate"),
        "creationDate": ev.get("creationDate") or ev.get("createdAt"),
        "endDate": ev.get("endDate"),
        "active": ev.get("active"),
        "closed": ev.get("closed"),
        "archived": ev.get("archived"),
        "volume": ev.get("volume"),
        "series": [{"id": str(s.get("id")), "slug": s.get("slug")} for s in ev.get("series") or []],
        "tags": [t.get("slug") for t in ev.get("tags") or [] if isinstance(t, dict)],
        "n_markets": len(ev.get("markets") or []),
    }


def _is_tsa(ev: dict[str, Any]) -> bool:
    return bool(TSA_RX.search(f"{ev.get('title') or ''} {ev.get('slug') or ''}".replace("-", " ")))


async def stage_gate(cache: Path) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    found: dict[str, dict[str, Any]] = {}
    sources: Counter[str] = Counter()
    try:
        for closed in ("false", "true"):
            f = await rt.polymarket.events_all({"tag_slug": "tsa", "closed": closed})
            rows = f.data["rows"]
            (cache / f"tag_tsa_{closed}.json").write_text(json.dumps([_slim(e) for e in rows]))
            print(f"  tag_slug=tsa closed={closed}: {len(rows)} events, complete={f.data['complete']}, rss {rss_mb():.0f} MB", flush=True)
            for e in rows:
                found.setdefault(str(e.get("id")), _slim(e))
                sources[f"tag:{closed}"] += 1
        for q in QUERIES:
            for status in ("active", "closed"):
                params = {"q": q, "limit_per_type": 50, "events_status": status}
                f = await rt.polymarket.gamma.get_json("/public-search", params=params, ttl=0, tool="public-search", args=params)
                evs = (f.data or {}).get("events") or [] if isinstance(f.data, dict) else []
                tsa = [e for e in evs if _is_tsa(e)]
                print(f"  search q={q!r} {status}: {len(evs)} events, tsa-matching {len(tsa)}", flush=True)
                for e in tsa:
                    found.setdefault(str(e.get("id")), _slim(e))
                    sources[f"search:{status}"] += 1
        series_ids = sorted({s["id"] for e in found.values() for s in e["series"]})
        for sid in series_ids:
            for closed in ("false", "true"):
                f = await rt.polymarket.events_all({"series_id": sid, "closed": closed})
                rows = f.data["rows"]
                print(f"  series {sid} closed={closed}: {len(rows)} events, complete={f.data['complete']}", flush=True)
                for e in rows:
                    found.setdefault(str(e.get("id")), _slim(e))
                    sources[f"series:{closed}"] += 1
    finally:
        await rt.polymarket.aclose()
    evs = [e for e in found.values() if _is_tsa(e)]
    (cache / "gate_events.json").write_text(json.dumps(evs, indent=1))
    print(f"\nTSA events total: {len(evs)} (non-TSA dropped: {len(found) - len(evs)}); sources {dict(sources)}")
    ends = sorted((e.get("endDate") or "")[:10] for e in evs)
    print(f"endDate range: {ends[0] if ends else None} .. {ends[-1] if ends else None}")
    by_month = Counter(x[:7] for x in ends)
    print("events by endDate month:", dict(sorted(by_month.items())))
    open_now = [e for e in evs if e.get("active") and not e.get("closed")]
    print(f"active & not closed: {len(open_now)}")
    late = [
        e
        for e in evs
        if (e.get("endDate") or "")[:10] > LAST_KNOWN
        or (e.get("startDate") or "")[:10] > LAST_KNOWN
        or (e.get("creationDate") or "")[:10] > LAST_KNOWN
    ]
    print(f"events with end/start/creation after {LAST_KNOWN}: {len(late)}")
    for e in sorted(late, key=lambda e: e.get("endDate") or "")[:30]:
        print(f"  {e['endDate']} start={e['startDate']} created={e['creationDate']} active={e['active']} closed={e['closed']} {e['slug']}")
    for e in sorted(evs, key=lambda e: e.get("endDate") or "")[-5:]:
        print(f"  last: {e['endDate']} closed={e['closed']} vol={e['volume']} {e['slug']} series={e['series']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["gate"])
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "gate":
        asyncio.run(stage_gate(a.cache_dir))


if __name__ == "__main__":
    main()
