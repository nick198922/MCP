"""Polymarket weekly pop-culture #1 markets (Spotify, Netflix, box office): steps 0-1 feasibility only
(IDEA-20261112-pm-chart-nowcast-popculture). No outcome metrics are computed here.

Network only through the runtime providers (``rt.polymarket``) and ``core.http.Http`` for public chart pages;
per-file JSON cache under ``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm-chart-nowcast-popculture``).

Usage:
    uv run python scripts/verify_pm_chart_nowcast.py --stage discover   # public-search -> series ids
    uv run python scripts/verify_pm_chart_nowcast.py --stage list       # closed events per series -> events/<series>.json
    uv run python scripts/verify_pm_chart_nowcast.py --stage hist       # /prices-history coverage (fidelity 10 min)
    uv run python scripts/verify_pm_chart_nowcast.py --stage sources    # free daily data pages: reachability, depth
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import resource
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm-chart-nowcast-popculture").expanduser()
QUERIES = (
    "song on spotify this week",
    "song on us spotify this week",
    "global netflix show this week",
    "us netflix show this week",
    "netflix show this week",
    "box office this weekend",
    "spotify",
    "netflix",
    "box office",
)
# Series of interest are matched on the event slug (kind -> regex).
KINDS = {
    "spotify_us": re.compile(r"song-on-us-spotify"),
    "spotify_global": re.compile(r"song-on-(global-)?spotify"),
    "netflix_us": re.compile(r"us-netflix-(show|series|movie)"),
    "netflix_global": re.compile(r"global-netflix-(show|series|movie)"),
    "box_office": re.compile(r"box-office-this-weekend|box-office-weekend"),
}


# Gamma series found by --stage discover (2026-11-12); the first five are the card's universe, the rest are context.
SERIES_KIND = {
    "10710": "spotify_global",  # 1-spotify-song
    "10770": "spotify_us",  # top-us-spotify-song
    "10440": "netflix_global",  # top-netflix-show-week
    "10772": "netflix_us",  # 1-us-netflix-show
    "11210": "box_office",  # highest-grossing-movie-weekly
    "10441": "ctx_netflix_global_movie",
    "10775": "ctx_netflix_us_movie",
    "46": "ctx_box_office_openings",
}


def kind_of(slug: str) -> str | None:
    for k, rx in KINDS.items():
        if rx.search(slug or ""):
            return k
    return None


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _ts(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _jl(x: Any) -> list[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            v = json.loads(x)
            return v if isinstance(v, list) else []
        except ValueError:
            return []
    return []


async def stage_discover(cache: Path) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    series: dict[str, dict[str, Any]] = {}
    try:
        for q in QUERIES:
            for closed in (False, True):
                params = {"q": q, "limit_per_type": 50, "events_status": "closed" if closed else "active"}
                f = await rt.polymarket.gamma.get_json("/public-search", params=params, ttl=0, tool="public-search", args=params)
                evs = (f.data or {}).get("events") or [] if isinstance(f.data, dict) else []
                for ev in evs:
                    k = kind_of(ev.get("slug", ""))
                    for s in ev.get("series") or []:
                        sid = str(s.get("id"))
                        row = series.setdefault(sid, {"slug": s.get("slug"), "title": s.get("title"), "kinds": Counter(), "n": 0})
                        row["kinds"][k] += 1
                        row["n"] += 1
                    if k and not ev.get("series"):
                        series.setdefault("none:" + k, {"slug": None, "title": None, "kinds": Counter(), "n": 0, "ex": ev.get("slug")})
                print(f"  q={q!r} closed={closed}: {len(evs)} events, rss {rss_mb():.0f} MB", flush=True)
    finally:
        await rt.polymarket.aclose()
    out = {sid: {**r, "kinds": dict(r["kinds"])} for sid, r in series.items()}
    (cache / "series.json").write_text(json.dumps(out, indent=1, default=str))
    for sid, r in sorted(out.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{sid:>8} {str(r['slug'])[:55]:55} n={r['n']:3} kinds={r['kinds']}")


async def stage_list(cache: Path, series_ids: list[str]) -> None:
    from tradedesk.server import build_server

    edir = cache / "events"
    edir.mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    try:
        for sid in series_ids:
            for closed in ("true", "false"):
                f = await rt.polymarket.events_all({"series_id": sid, "closed": closed})
                rows = f.data["rows"]
                p = edir / f"{sid}_{'closed' if closed == 'true' else 'open'}.json"
                p.write_text(json.dumps(rows))
                print(
                    f"  series {sid} closed={closed}: {len(rows)} events, complete={f.data['complete']}, rss {rss_mb():.0f} MB", flush=True
                )
    finally:
        await rt.polymarket.aclose()


def _closed_events(cache: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted((cache / "events").glob("*_closed.json")):
        for ev in json.loads(p.read_text()):
            sid = p.stem.split("_")[0]
            k = SERIES_KIND.get(sid) or kind_of(ev.get("slug", ""))
            out.append({"series": sid, "kind": k, "ev": ev})
    return out


def summarize_events(cache: Path) -> None:
    rows = _closed_events(cache)
    by = Counter((r["series"], r["kind"]) for r in rows)
    print("closed events by (series, kind):")
    for k, v in sorted(by.items(), key=str):
        print(f"  {k}: {v}")
    for (sid, kind), _ in sorted(by.items(), key=str):
        evs = [r["ev"] for r in rows if r["series"] == sid and r["kind"] == kind]
        ends = sorted(e.get("endDate") or "" for e in evs)
        nm = [len(e.get("markets") or []) for e in evs]
        vol = sorted(float(e.get("volume") or 0) for e in evs)
        print(
            f"  {sid}/{kind}: first end {ends[0][:10]} last end {ends[-1][:10]}; markets/event median {sorted(nm)[len(nm) // 2]};"
            f" volume median ${vol[len(vol) // 2]:,.0f}, p10 ${vol[len(vol) // 10]:,.0f}"
        )
        fees = Counter()
        for e in evs:
            for m in e.get("markets") or []:
                fees[(m.get("feesEnabled"), json.dumps(m.get("feeSchedule"), sort_keys=True), m.get("feeType"))] += 1
        print(f"    fees (feesEnabled, feeSchedule, feeType): {fees.most_common(4)}")
        # settlement rule of the latest and the earliest event (first market), truncated
        for e in (evs[0], evs[-1]) if len(evs) > 1 else evs:
            ms = e.get("markets") or []
            d = (ms[0].get("description") if ms else None) or e.get("description") or ""
            print(
                f"    rule [{e.get('slug')}] resolutionSource={e.get('resolutionSource') or (ms[0].get('resolutionSource') if ms else '')!r}"
            )
            print("      " + " ".join(d.split())[:700])


async def stage_hist(cache: Path, fidelity: int, pace: float) -> None:
    from tradedesk.server import build_server

    hdir = cache / "hist"
    hdir.mkdir(parents=True, exist_ok=True)
    rows = [r for r in _closed_events(cache) if r["kind"] and not r["kind"].startswith("ctx_")]
    print(f"events to check: {len(rows)}", flush=True)
    _, rt = build_server()
    t_start = time.time()
    try:
        for i, r in enumerate(rows):
            ev = r["ev"]
            p = hdir / f"{ev['id']}.json"
            if p.exists():
                continue
            ms = ev.get("markets") or []
            start = _ts(ev.get("startDate") or ev.get("creationDate")) or 0
            end = _ts(ev.get("closedTime") or ev.get("endDate")) or int(time.time())
            # one token per market (YES of every listed outcome), points counted only: no prices are kept
            res = {"event_id": ev["id"], "slug": ev.get("slug"), "kind": r["kind"], "n_markets": len(ms), "markets": []}
            # coverage sample: the resolved-YES market plus the 3 largest by volume (not all ~20 outcomes)
            won = [m for m in ms if (_jl(m.get("outcomePrices")) or ["0"])[0] in ("1", 1, "1.0")]
            top = sorted(ms, key=lambda m: -float(m.get("volumeNum") or m.get("volume") or 0))[:3]
            sample = list({m.get("id"): m for m in won + top}.values())
            res["n_won"] = len(won)
            res["won_ids"] = [m.get("id") for m in won]
            res["first_mkt_start"] = min((_ts(m.get("startDate")) or 0 for m in ms), default=0)
            res["end"] = end
            for m in sample:
                toks = _jl(m.get("clobTokenIds"))
                if not toks:
                    res["markets"].append({"id": m.get("id"), "n_points": None})
                    continue
                mend = _ts(m.get("closedTime")) or end
                try:
                    h = await rt.polymarket.price_history_range(toks[0], start - 86400, mend + 3600, fidelity)
                    pts = h.data
                    res["markets"].append(
                        {
                            "id": m.get("id"),
                            "n_points": len(pts),
                            "first_t": pts[0]["t"] if pts else None,
                            "last_t": pts[-1]["t"] if pts else None,
                        }
                    )
                except Exception as e:  # recorded, not fatal
                    res["markets"].append({"id": m.get("id"), "n_points": None, "err": str(e)[:120]})
                await asyncio.sleep(pace)
            p.write_text(json.dumps(res))
            if i % 20 == 0:
                print(f"  {i + 1}/{len(rows)} {time.time() - t_start:.0f}s rss {rss_mb():.0f} MB", flush=True)
    finally:
        await rt.polymarket.aclose()
    print("hist done", flush=True)


def summarize_hist(cache: Path, min_points: int) -> None:
    rows = [json.loads(p.read_text()) for p in sorted((cache / "hist").glob("*.json"))]
    by: dict[str, Counter] = {}
    for r in rows:
        c = by.setdefault(r["kind"], Counter())
        c["events"] += 1
        ok = [m for m in r["markets"] if (m.get("n_points") or 0) >= min_points]
        c["events_with_hist_all_markets"] += int(len(ok) == len(r["markets"]) and r["markets"] != [])
        c["events_with_hist_any"] += int(bool(ok))
        c["one_winner"] += int(r.get("n_won") == 1)
        okw = [m for m in ok if m["id"] in (r.get("won_ids") or [])]
        c["winner_hist"] += int(bool(okw))
        # winner history spanning at least the last 3 days before the end (the card's entry points are 1-3 days out)
        c["winner_hist_3d"] += int(any(m["first_t"] and m["first_t"] <= r["end"] - 3 * 86400 for m in okw))
        c["winner_pts_sum"] += sum(m["n_points"] for m in okw)
        c["markets"] += len(r["markets"])
        c["markets_with_hist"] += len(ok)
        c["errors"] += sum(1 for m in r["markets"] if m.get("err"))
    tot = Counter()
    for k, c in sorted(by.items()):
        print(f"  {k}: {dict(c)}")
        tot.update(c)
    print(f"  TOTAL: {dict(tot)} (min_points={min_points})")


async def stage_sources(cache: Path) -> None:
    """Reachability and back-history depth of free daily data pages. Page text is untrusted: only status, size and
    a few structural markers (dates, table rows) are extracted and printed."""
    from tradedesk.core.http import Http

    sdir = cache / "sources"
    sdir.mkdir(parents=True, exist_ok=True)
    ua = {"User-Agent": "Mozilla/5.0 (research script)"}
    checks = [
        # (name, base, path)
        ("kworb_global_daily", "https://kworb.net", "/spotify/country/global_daily.html"),
        ("kworb_us_daily", "https://kworb.net", "/spotify/country/us_daily.html"),
        ("kworb_global_weekly", "https://kworb.net", "/spotify/country/global_weekly.html"),
        ("kworb_global_weekly_totals", "https://kworb.net", "/spotify/country/global_weekly_totals.html"),
        ("kworb_song_example", "https://kworb.net", "/spotify/track/0VjIjW4GlUZAMYd2vXMi3b.html"),
        ("spotify_charts_api", "https://charts-spotify-com-service.spotify.com", "/public/v0/charts"),
        ("bom_daily_2026", "https://www.boxofficemojo.com", "/daily/2026/?view=year"),
        ("bom_date_example", "https://www.boxofficemojo.com", "/date/2026-05-15/"),
        ("bom_weekend_2026", "https://www.boxofficemojo.com", "/weekend/2026W20/"),
        ("the_numbers_daily", "https://www.the-numbers.com", "/box-office-chart/daily/2026/05/15"),
        ("flixpatrol_top10_today", "https://flixpatrol.com", "/top10/netflix/world/"),
        ("flixpatrol_top10_date", "https://flixpatrol.com", "/top10/netflix/world/2026-05-12/"),
        ("flixpatrol_us_date", "https://flixpatrol.com", "/top10/netflix/united-states/2026-05-12/"),
        ("netflix_top10_tudum", "https://www.netflix.com", "/tudum/top10"),
        ("netflix_top10_xlsx", "https://www.netflix.com", "/tudum/top10/data/all-weeks-global.xlsx"),
        ("netflix_top10_countries_xlsx", "https://www.netflix.com", "/tudum/top10/data/all-weeks-countries.xlsx"),
        ("wayback_kworb_cdx", "https://web.archive.org", "/cdx/search/cdx"),
    ]
    clients: dict[str, Http] = {}
    try:
        for name, base, path in checks:
            http = clients.setdefault(base, Http("charts-" + base.split("//")[1], base, rate=0.5, timeout=20.0, retries=1, headers=ua))
            params = None
            if name == "wayback_kworb_cdx":
                params = {"url": "kworb.net/spotify/country/global_daily.html", "output": "json", "from": "2026", "limit": "400"}
            try:
                f = await http.get_bytes(path, params=params, tool=name)
                body = f.data if isinstance(f.data, bytes | bytearray) else str(f.data).encode()
                (sdir / f"{name}.bin").write_bytes(body)
                txt = body[:2_000_000].decode("utf-8", "replace")
                dates = sorted(set(re.findall(r"20\d\d[-/]\d\d[-/]\d\d", txt)))
                print(
                    f"  {name}: OK {len(body):,} B; <tr> {txt.count('<tr')}; dates {len(dates)}"
                    f" [{dates[0] if dates else '-'} .. {dates[-1] if dates else '-'}]",
                    flush=True,
                )
            except Exception as e:
                print(f"  {name}: FAIL {type(e).__name__}: {str(e)[:160]}", flush=True)
            await asyncio.sleep(1.5)
    finally:
        for h in clients.values():
            await h.aclose()


async def stage_depth(cache: Path) -> None:
    """Second probe: can past daily states be read without an account, and is a first-publication archive available?"""
    from tradedesk.core.http import Http

    sdir = cache / "sources"
    ua = {"User-Agent": "Mozilla/5.0 (research script)"}
    checks = [
        (
            "spotify_public_daily_dated",
            "https://charts-spotify-com-service.spotify.com",
            "/public/v0/charts/regional-global-daily/2026-05-10",
            None,
        ),
        (
            "spotify_auth_daily_dated",
            "https://charts-spotify-com-service.spotify.com",
            "/auth/v0/charts/regional-global-daily/2026-05-10",
            None,
        ),
        (
            "spotify_public_weekly_dated",
            "https://charts-spotify-com-service.spotify.com",
            "/public/v0/charts/regional-global-weekly/2026-05-07",
            None,
        ),
        ("kworb_global_daily_archive", "https://kworb.net", "/spotify/country/global_daily_2026-05-10.html", None),
        (
            "wayback_kworb_cdx",
            "https://web.archive.org",
            "/cdx/search/cdx",
            {
                "url": "kworb.net/spotify/country/global_daily.html",
                "output": "json",
                "from": "20251201",
                "to": "20261110",
                "fl": "timestamp,statuscode",
                "collapse": "timestamp:8",
            },
        ),
        (
            "wayback_kworb_us_cdx",
            "https://web.archive.org",
            "/cdx/search/cdx",
            {
                "url": "kworb.net/spotify/country/us_daily.html",
                "output": "json",
                "from": "20251201",
                "to": "20261110",
                "fl": "timestamp,statuscode",
                "collapse": "timestamp:8",
            },
        ),
        ("bom_date_2026_02_06", "https://www.boxofficemojo.com", "/date/2026-02-06/", None),
        ("the_numbers_daily_2026_02_06", "https://www.the-numbers.com", "/box-office-chart/daily/2026/02/06", None),
    ]
    clients: dict[str, Http] = {}
    try:
        for name, base, path, params in checks:
            http = clients.setdefault(base, Http("charts-" + base.split("//")[1], base, rate=0.2, timeout=30.0, retries=1, headers=ua))
            try:
                f = await http.get_bytes(path, params=params, tool=name)
                body = f.data if isinstance(f.data, bytes | bytearray) else str(f.data).encode()
                (sdir / f"{name}.bin").write_bytes(body)
                txt = body[:3_000_000].decode("utf-8", "replace")
                extra = ""
                if name.startswith("wayback"):
                    rows = json.loads(txt or "[]")[1:]
                    days = sorted({r[0][:8] for r in rows if r[1] == "200"})
                    extra = f"; snapshot days (200) {len(days)} [{days[0] if days else '-'} .. {days[-1] if days else '-'}]"
                print(f"  {name}: OK {len(body):,} B; <tr> {txt.count('<tr')}{extra}; head {' '.join(txt[:120].split())!r}", flush=True)
            except Exception as e:
                print(f"  {name}: FAIL {type(e).__name__}: {str(e)[:140]}", flush=True)
            await asyncio.sleep(5.0)
    finally:
        for h in clients.values():
            await h.aclose()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["discover", "list", "events", "hist", "histsum", "sources", "depth"])
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--series", nargs="*", default=[])
    ap.add_argument("--fidelity", type=int, default=10)
    ap.add_argument("--pace", type=float, default=0.15)
    ap.add_argument("--min-points", type=int, default=10)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "discover":
        asyncio.run(stage_discover(a.cache_dir))
    elif a.stage == "list":
        asyncio.run(stage_list(a.cache_dir, a.series))
        summarize_events(a.cache_dir)
    elif a.stage == "events":
        summarize_events(a.cache_dir)
    elif a.stage == "hist":
        asyncio.run(stage_hist(a.cache_dir, a.fidelity, a.pace))
        summarize_hist(a.cache_dir, a.min_points)
    elif a.stage == "histsum":
        summarize_hist(a.cache_dir, a.min_points)
    elif a.stage == "sources":
        asyncio.run(stage_sources(a.cache_dir))
    elif a.stage == "depth":
        asyncio.run(stage_depth(a.cache_dir))


if __name__ == "__main__":
    main()
