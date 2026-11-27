"""Polymarket monthly CPI bracket markets vs the Cleveland Fed inflation nowcast: step 0 feasibility only
(IDEA-20261028-pm-cpi-brackets-vs-nowcast-forward). No market price or model probability is compared with an
outcome here: the Polymarket stages count coverage only, the Cleveland stages check what is archived.

Network only through the runtime providers (``rt.polymarket``) and ``core.http.Http`` for public pages; per-file
cache under ``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm_cpi``). Page and API text is untrusted data:
only structure (keys, dates, counts, link paths) is extracted.

Usage (C = ~/.cache/tradedesk-agents/pm_cpi; the exact commands of the report are listed in it):
    uv run python scripts/verify_pm_cpi_step0.py --stage discover --out C/out_discover.txt  # public-search -> series
    uv run python scripts/verify_pm_cpi_step0.py --stage list --series 5 10007 10008 11905 12054 --out C/out_list.txt
    uv run python scripts/verify_pm_cpi_step0.py --stage cleveland --out C/out_cleveland.txt  # page, chart JSON files
    uv run python scripts/verify_pm_cpi_step0.py --stage anchors --out C/out_anchors.txt  # release days vs rule text
    uv run python scripts/verify_pm_cpi_step0.py --stage hist --out C/out_hist.txt      # T-7/T-3/T-1 coverage, counts
    uv run python scripts/verify_pm_cpi_step0.py --stage cfsum --out C/out_cfsum.txt    # nowcast points before release
    uv run python scripts/verify_pm_cpi_step0.py --stage moves --out C/out_moves.txt    # distinct values in T-7..T-1
    uv run python scripts/verify_pm_cpi_step0.py --stage vintage --out C/out_vintage.txt  # core nowcast vs today's CPI
    uv run python scripts/verify_pm_cpi_step0.py --stage avail --file nowcast_year.json --stamps 20240615 --out <file>
    uv run python scripts/verify_pm_cpi_step0.py --stage compare --file nowcast_year.json \
        --capture C/snapshots/20261126T1920Z/nowcast_year.json --against <later copy> --out C/out_compare.txt
    uv run python scripts/verify_pm_cpi_step0.py --stage snapshot --out <file>  # today's nowcast + active books
    uv run python scripts/verify_pm_cpi_step0.py --stage row --day 2026-12-03 --out <file>  # the collector job's row (DB read-only)
    uv run python scripts/verify_pm_cpi_step0.py --stage vcheck --capture <older copy> --against <newer copy> \
        [--step-target 2026-11] --out <file>  # V1 / V2 of the registration: last line 'V pass' or 'V fail: <reason>'
    uv run python scripts/verify_pm_cpi_step0.py --stage sigma --against <nowcast_year.json of the V2 row 11.12> \
        --before 2027-01-01 --out C/out_sigma.txt  # archive SD (targets released before --before; no PM prices)

Results (2026-11-26): docs/verification/2026-11-26-pm-cpi-step0.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import resource
import time
import zlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The chart parser lives in the package since item 480 (the collector job pm_cpi_snapshot uses the same code).
from tradedesk.analytics.pm_cpi import MONTHS, cf_daily, new_points_after

# Polymarket series of the US CPI bracket events (--stage discover, 26.11): `snapshot` falls back to them when the cache's
# events/*_open.json are gone (~/.cache is not backed up).
from tradedesk.collector.pm_cpi_snapshot import SERIES as DEFAULT_SERIES

DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_cpi").expanduser()
DEFAULT_DB = Path("/var/lib/tradedesk-mcp/tradedesk.sqlite")
# S of the registration п. 10: the older side of V1 (the snapshot of 26.11, backed up); the proof of
# scripts/pm_cpi_forward.py passes its files to --stage vcheck when it re-runs V1 (item 492), only when they are these
# bytes (sha256 read 27.11; the same in the ~/.cache copy of the snapshot): see v1_older_why
V1_OLDER = Path("~/.local/state/tradedesk-agents/pm_cpi/snapshots/20261126T1920Z").expanduser()
V1_OLDER_SHA256 = {
    "nowcast_year.json": "33758ce12635017dbcd562aa859a559d7eb909f9ec8dccb46fc54c247cf1addd",
    "nowcast_month.json": "da8d3f57994fe296304082d5cac0b0ac5cfff7e17265f65b9c2795a685821f36",
}
QUERIES = ("CPI", "inflation", "core CPI", "CPI year over year", "monthly inflation", "inflation US annual")
CPI_SLUG = re.compile(r"cpi|inflation", re.I)
UA = {"User-Agent": "Mozilla/5.0 (research script)"}
CF_BASE = "https://www.clevelandfed.org"
CF_PAGES = ("/indicators-and-data/inflation-nowcasting",)
# Chart data files referenced by the nowcasting page (verified in --stage cleveland, see report)
CF_FILES = (
    "/-/media/files/webcharts/inflationnowcasting/nowcast_month.json",
    "/-/media/files/webcharts/inflationnowcasting/nowcast_quarter.json",
    "/-/media/files/webcharts/inflationnowcasting/nowcast_year.json",
)
WAYBACK = "https://web.archive.org"

_OUT: Any = None


def say(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    if _OUT is not None:
        _OUT.write(line + "\n")
        _OUT.flush()


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


# ---------------------------------------------------------------------------------------------------- Polymarket


async def stage_discover(cache: Path) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    series: dict[str, dict[str, Any]] = {}
    loose: Counter = Counter()
    try:
        for q in QUERIES:
            for status in ("active", "closed"):
                params = {"q": q, "limit_per_type": 50, "events_status": status}
                f = await rt.polymarket.gamma.get_json("/public-search", params=params, ttl=0, tool="public-search", args=params)
                evs = (f.data or {}).get("events") or [] if isinstance(f.data, dict) else []
                for ev in evs:
                    slug = ev.get("slug") or ""
                    if not CPI_SLUG.search(slug):
                        continue
                    if not ev.get("series"):
                        loose[re.sub(r"\d+", "#", slug)] += 1
                    for s in ev.get("series") or []:
                        sid = str(s.get("id"))
                        row = series.setdefault(sid, {"slug": s.get("slug"), "title": s.get("title"), "n": 0, "ex": set()})
                        row["n"] += 1
                        if len(row["ex"]) < 3:
                            row["ex"].add(slug)
                say(f"  q={q!r} {status}: {len(evs)} events, rss {rss_mb():.0f} MB")
                await asyncio.sleep(0.3)
    finally:
        await rt.polymarket.aclose()
    out = {sid: {**r, "ex": sorted(r["ex"])} for sid, r in series.items()}
    (cache / "series.json").write_text(json.dumps({"series": out, "loose": dict(loose)}, indent=1))
    say("series with CPI/inflation event slugs:")
    for sid, r in sorted(out.items(), key=lambda kv: -kv[1]["n"]):
        say(f"  {sid:>8} {str(r['slug'])[:45]:45} n={r['n']:3} ex={r['ex'][:2]}")
    say("CPI/inflation events without a series (slug pattern: hits):")
    for k, v in loose.most_common(15):
        say(f"  {k[:80]}: {v}")


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
                (edir / f"{sid}_{'closed' if closed == 'true' else 'open'}.json").write_text(json.dumps(rows))
                say(f"  series {sid} closed={closed}: {len(rows)} events, complete={f.data['complete']}, rss {rss_mb():.0f} MB")
    finally:
        await rt.polymarket.aclose()


def _events(cache: Path, which: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for p in sorted((cache / "events").glob(f"*_{which}.json")):
        sid = p.stem.split("_")[0]
        out.extend((sid, ev) for ev in json.loads(p.read_text()))
    return out


def _desc(ev: dict[str, Any]) -> str:
    ms = ev.get("markets") or []
    return " ".join(((ms[0].get("description") if ms else None) or ev.get("description") or "").split())


def _kind(ev: dict[str, Any]) -> str:
    """Measure from the resolution text (slugs and series are inconsistent: a YoY event sits in the 'monthly' series)."""
    d = _desc(ev).lower()
    core = "excluding food and energy" in d or "less food and energy" in d
    if re.search(r"12[- ]month", d):
        per = "yoy"
    elif "one-month" in d or "month-to-month" in d or re.search(r"from \w+ to \w+ 20\d\d", d):
        per = "mom"
    else:
        per = "other"
    return ("core_" if core else "") + per


REL_RX = re.compile(r"scheduled to be released on (\w+ \d{1,2}, 20\d\d)")


def release_date(ev: dict[str, Any]) -> str | None:
    """BLS release day stated in the resolution text ('currently scheduled to be released on Month D, YYYY')."""
    m = REL_RX.search(_desc(ev))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


def summarize_events(cache: Path) -> None:
    rows = _events(cache, "closed")
    say(f"closed events: {len(rows)}")
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for sid, ev in rows:
        by.setdefault((sid, _kind(ev)), []).append(ev)
    rel_by_kind: dict[str, set[str]] = {}
    no_rel: Counter = Counter()
    cf_rel = _releases(cache)
    for (_, kind), evs in by.items():
        for e in evs:
            t, r = event_release(e, cf_rel)
            if r is not None and r >= "2024-01-01":
                rel_by_kind.setdefault(kind, set()).add(r)
            elif r is None and t is not None and t >= "2024":
                no_rel[kind] += 1
    say(
        "distinct BLS release days 2024+ per measure (all series; Cleveland release marker): "
        + str({k: len(v) for k, v in sorted(rel_by_kind.items())})
        + f"; events 2024+ whose target month had no release: {dict(no_rel)}"
    )
    for k, v in sorted(rel_by_kind.items()):
        say(f"  {k}: {sorted(v)[0] if v else '-'} .. {sorted(v)[-1] if v else '-'}")
    for (sid, kind), evs in sorted(by.items()):
        evs.sort(key=lambda e: e.get("endDate") or "")
        ends = [(e.get("endDate") or "")[:10] for e in evs]
        n24 = sum(1 for d in ends if d >= "2024-01-01")
        nm = sorted(len(e.get("markets") or []) for e in evs)
        vol = sorted(float(e.get("volume") or 0) for e in evs)
        say(
            f"  series {sid} kind {kind}: {len(evs)} events ({n24} ending 2024+), ends {ends[0]}..{ends[-1]};"
            f" brackets/event median {nm[len(nm) // 2]} [min {nm[0]}, max {nm[-1]}];"
            f" volume median ${vol[len(vol) // 2]:,.0f} p10 ${vol[len(vol) // 10]:,.0f}"
        )
        say(f"    slugs e.g.: {[e.get('slug') for e in evs[-3:]]}")
        e = evs[-1]
        ms = e.get("markets") or []
        say(f"    latest brackets: {[m.get('groupItemTitle') for m in ms][:14]}")
        d = (ms[0].get("description") if ms else None) or e.get("description") or ""
        say(f"    rule [{e.get('slug')}]: " + " ".join(d.split())[:900])
        fees = Counter((m.get("feesEnabled"), m.get("feeType")) for ev in evs for m in ev.get("markets") or [])
        say(f"    fees (feesEnabled, feeType): {fees.most_common(3)}")
    opens = _events(cache, "open")
    say(f"open events: {len(opens)}")
    for sid, ev in opens:
        ms = ev.get("markets") or []
        say(
            f"  {sid} {ev.get('slug')} end {ev.get('endDate')} markets {len(ms)} vol ${float(ev.get('volume') or 0):,.0f}"
            f" liq ${float(ev.get('liquidity') or 0):,.0f}"
        )


_MON_TOKENS = {
    **{n: i + 1 for i, n in enumerate(("january", "february", "march", "april", "may", "june", "july", "august"))},
    **{n: i + 9 for i, n in enumerate(("september", "october", "november", "december"))},
    **{n: i + 1 for i, n in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))},
    "sept": 9,
}


def target_month(slug: str, ref_day: str | None) -> str | None:
    """Reference month 'YYYY-M' of a CPI event. The month is the last month word of the slug ('september-inflation-
    annual', 'core-cpi-mom-november-2026', '...-from-nov-to-dec-...'); the year is the one whose CPI release (the
    month after the reference month) lies closest to `ref_day` (the event's endDate, ISO). The rule text is not
    used: its release date goes stale when BLS moves a release and has year typos ('January 13, 2025' for 2026)."""
    toks = [_MON_TOKENS[t] for t in (slug or "").lower().split("-") if t in _MON_TOKENS]
    if not toks or not ref_day:
        return None
    m = toks[-1]
    ry, rm = int(ref_day[:4]), int(ref_day[5:7])
    end_idx = ry * 12 + rm - 1
    y = min((ry - 1, ry, ry + 1), key=lambda yy: abs(end_idx - (yy * 12 + m)))  # release month index = y*12 + m
    return f"{y}-{m}"


def cf_release_days(charts: list[dict[str, Any]]) -> dict[str, str]:
    """{target 'YYYY-M': ISO day of the BLS release of that month's CPI} from the 'CPI <Mon>' vline of the target's
    chart in a Cleveland nowcast file (the day of the last nowcast point before the line: the release-day point is
    published at ~10:00 ET, after the 08:30 ET release). A target without the line had no release (October 2025)."""
    out = {}
    for ch in charts:
        target, _, vl = cf_daily(ch, "CPI Inflation")
        rel = vl.get(f"CPI {MONTHS[int(target.split('-')[1]) - 1]}")
        if rel is not None:
            out[target] = rel
    return out


def event_release(ev: dict[str, Any], releases: dict[str, str]) -> tuple[str | None, str | None]:
    """(target month, release day) of an event: target from the slug and endDate, release day from Cleveland's
    release markers; release None = the target month had no CPI release (or the target is unknown)."""
    t = target_month(ev.get("slug") or "", (ev.get("endDate") or "")[:10] or release_date(ev))
    return t, (releases.get(t) if t else None)


def anchor_ts(release_day: str) -> int:
    """BLS release moment: 08:30 New York time of the release day, as a Unix timestamp."""
    from zoneinfo import ZoneInfo

    return int(datetime.fromisoformat(release_day + "T08:30").replace(tzinfo=ZoneInfo("America/New_York")).timestamp())


def _releases(cache: Path) -> dict[str, str]:
    return cf_release_days(json.loads((cache / "cleveland" / "nowcast_year.json").read_text()))


def stage_anchors(cache: Path) -> None:
    """Every closed event: rule-text release day vs the Cleveland release marker, with endDate and closedTime as a
    control. Prints the events whose anchor differs from the rule text or that have no release."""
    rel = _releases(cache)
    c: Counter = Counter()
    for sid, ev in _events(cache, "closed"):
        t, r = event_release(ev, rel)
        rule = release_date(ev)
        if (r or rule or "") < "2024-01-01":
            continue
        closed = (ev.get("closedTime") or "")[:10]
        st = "no_target" if t is None else "no_release" if r is None else "same" if r == rule else "differs"
        c[st] += 1
        if st != "same":
            say(
                f"  {st:10} {sid:>5} {ev.get('slug')}: target {t} rule {rule} cleveland {r} end {(ev.get('endDate') or '')[:10]} closed {closed}"
            )
        elif closed and not (r <= closed <= (date_shift(r, 3))):
            c["closed_outside_0..3d"] += 1
            say(f"  closed?    {sid:>5} {ev.get('slug')}: release {r} closed {closed}")
    say(f"anchors (events with a release or rule day 2024+): {dict(c)}")


def date_shift(day: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def release_coverage(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per measure: distinct releases, and releases where every bracket of every event of that release (two events
    can share a release, e.g. '...-lower-brackets' and '...-higher-brackets') has a point before T-1/T-3/T-7."""
    by: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in rows:
        by.setdefault(r.get("kind") or "?", {}).setdefault(r["release"], []).extend(r["markets"])
    out = {}
    for k, rels in by.items():
        c = {"releases": len(rels)}
        for d in (7, 3, 1):
            c[f"T-{d}"] = sum(1 for ms in rels.values() if ms and all(m.get(f"t{d}") for m in ms))
        out[k] = c
    return out


async def stage_hist(cache: Path, fidelity: int, pace: float) -> None:
    """Per closed event since 2024: for every bracket, does /prices-history have a point within 6 h before
    T-7d, T-3d and T-1d of the anchor? Only point counts and timestamps are stored, never prices."""
    from tradedesk.server import build_server

    hdir = cache / "hist"
    hdir.mkdir(parents=True, exist_ok=True)
    rel = _releases(cache)
    rows = []
    for sid, ev in _events(cache, "closed"):
        t, r = event_release(ev, rel)
        if r is not None and r >= "2024-01-01":
            rows.append((sid, ev, t, r))
    say(f"events to check: {len(rows)}, brackets {sum(len(ev.get('markets') or []) for _, ev, _, _ in rows)}")
    _, rt = build_server()
    t0 = time.time()
    try:
        for i, (sid, ev, t, r) in enumerate(rows):
            p = hdir / f"{ev['id']}.json"
            a = anchor_ts(r)
            if p.exists() and json.loads(p.read_text()).get("anchor") == a:
                continue  # cached with the same anchor; a changed anchor is fetched again and overwritten
            res: dict[str, Any] = {
                "event_id": ev["id"],
                "series": sid,
                "slug": ev.get("slug"),
                "kind": _kind(ev),
                "target": t,
                "release": r,
                "rule_release": release_date(ev),
                "anchor": a,
                "markets": [],
            }
            for m in ev.get("markets") or []:
                toks = _jl(m.get("clobTokenIds"))
                rec: dict[str, Any] = {"id": m.get("id"), "title": m.get("groupItemTitle"), "start": _ts(m.get("startDate"))}
                if not toks or a is None:
                    rec["n"] = None
                    res["markets"].append(rec)
                    continue
                try:
                    h = await rt.polymarket.price_history_range(toks[0], a - 8 * 86400, a + 86400, fidelity)
                    ts = [int(x["t"]) for x in h.data if isinstance(x, dict) and "t" in x]
                    rec["n"] = len(ts)
                    for d in (7, 3, 1):
                        lo, hi = a - d * 86400 - 6 * 3600, a - d * 86400
                        rec[f"t{d}"] = any(lo <= t <= hi for t in ts)
                except Exception as e:  # recorded, not fatal
                    rec["n"] = None
                    rec["err"] = str(e)[:120]
                res["markets"].append(rec)
                await asyncio.sleep(pace)
            p.write_text(json.dumps(res))
            say(f"  {i + 1}/{len(rows)} {ev.get('slug')} {time.time() - t0:.0f}s rss {rss_mb():.0f} MB")
    finally:
        await rt.polymarket.aclose()
    summarize_hist(cache)


def current_hist_rows(cache: Path) -> tuple[list[dict[str, Any]], Counter]:
    """Cached hist files whose anchor equals the current Cleveland-anchored release (stale files of an older anchor
    and events without a release, e.g. October 2025, are counted and left out)."""
    rel = _releases(cache)
    want = {}
    for _, ev in _events(cache, "closed"):
        _, r = event_release(ev, rel)
        if r is not None and r >= "2024-01-01":
            want[str(ev["id"])] = anchor_ts(r)
    rows, skip = [], Counter()
    for p in sorted((cache / "hist").glob("*.json")):
        r = json.loads(p.read_text())
        a = want.get(str(r.get("event_id")))
        if a is None:
            skip["no_release_or_pre2024"] += 1
        elif r.get("anchor") != a:
            skip["stale_anchor"] += 1
        else:
            rows.append(r)
    skip["missing_file"] = len(set(want) - {str(r["event_id"]) for r in rows}) - skip["stale_anchor"]
    return rows, skip


def summarize_hist(cache: Path) -> None:
    rows, skip = current_hist_rows(cache)
    say(f"hist files used: {len(rows)}; left out: {dict(skip)}")
    for k, c in sorted(release_coverage(rows).items()):
        say(f"  {k}: distinct releases {c['releases']}, every bracket covered at T-7 / T-3 / T-1: {c['T-7']} / {c['T-3']} / {c['T-1']}")
    by: dict[str, Counter] = {}
    per_rel: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in rows:
        k = r.get("kind") or "?"
        c = by.setdefault(k, Counter())
        ms = r["markets"]
        per_rel.setdefault(k, {}).setdefault(r["release"], []).extend(ms)
        c["events"] += 1
        c["brackets"] += len(ms)
        c["err"] += sum(1 for m in ms if m.get("err"))
        c["no_points"] += sum(1 for m in ms if not m.get("n"))
        for d in (7, 3, 1):
            ok = sum(1 for m in ms if m.get(f"t{d}"))
            c[f"brk_T-{d}"] += ok
            c[f"ev_all_T-{d}"] += int(bool(ms) and ok == len(ms))
    say("per event (a point within 6 h before T-d; ev_all = every bracket of the event covered):")
    for k, c in sorted(by.items()):
        say(f"  {k}: {dict(c)}")
    for k, rels in sorted(per_rel.items()):
        for d in (7, 3, 1):
            miss = sorted(x for x, ms in rels.items() if not (ms and all(m.get(f"t{d}") for m in ms)))
            if miss:
                say(f"  {k}: releases without full T-{d} coverage: {miss}")
        say(f"  {k}: releases {sorted(rels)[0]} .. {sorted(rels)[-1]}")


# ---------------------------------------------------------------------------------------------------- Cleveland Fed


def _cf_series_summary(name: str, data: Any) -> dict[str, Any]:
    """Structure of a nowcast chart JSON: top-level type/keys, number of target periods, date span of the
    daily points of the first and last target. Values are not interpreted here."""
    info: dict[str, Any] = {"type": type(data).__name__}
    if isinstance(data, dict):
        info["keys"] = list(data)[:10]
    if isinstance(data, list):
        info["len"] = len(data)
        if data and isinstance(data[0], dict):
            info["item_keys"] = list(data[0])[:12]
    return info


async def stage_cleveland(cache: Path) -> None:
    from tradedesk.core.http import Http

    cdir = cache / "cleveland"
    cdir.mkdir(parents=True, exist_ok=True)
    http = Http("clevelandfed", CF_BASE, rate=0.5, timeout=30.0, retries=1, headers=UA)
    wb = Http("wayback", WAYBACK, rate=0.3, timeout=60.0, retries=2, headers=UA)
    try:
        for path in CF_PAGES:
            try:
                f = await http.get_bytes(path, tool="cf-page")
                body = bytes(f.data)
                (cdir / "page.html").write_bytes(body)
                txt = body.decode("utf-8", "replace")
                links = sorted(set(re.findall(r"[\"'(]([^\"'()\s]*\.(?:json|csv|xlsx|xls))[\"'?)]", txt, re.I)))
                media = sorted(set(re.findall(r"/-/media/[^\"'\s<>]+", txt)))[:30]
                dates = sorted(set(re.findall(r"20\d\d-\d\d-\d\d|\d\d/\d\d/20\d\d", txt)))
                say(f"page {path}: {len(body):,} B; data links {links[:20]}; dates {len(dates)} {dates[:3]}..{dates[-3:]}")
                say(f"  /-/media paths: {media}")
            except Exception as e:
                say(f"page {path}: FAIL {type(e).__name__}: {str(e)[:200]}")
        for path in CF_FILES:
            name = path.rsplit("/", 1)[1]
            try:
                f = await http.get_bytes(path, tool="cf-json")
                body = bytes(f.data)
                (cdir / name).write_bytes(body)
                say(f"file {name}: OK {len(body):,} B; head {body[:160]!r}")
            except Exception as e:
                say(f"file {name}: FAIL {type(e).__name__}: {str(e)[:200]}")
            # Wayback captures of the same file (distinct content only)
            params = {
                "url": "clevelandfed.org" + path,
                "output": "json",
                "fl": "timestamp,statuscode,digest,length",
                "collapse": "digest",
                "limit": "5000",
            }
            try:
                f = await wb.get_bytes("/cdx/search/cdx", params=params, tool="wayback-cdx")
                rows = json.loads(bytes(f.data) or b"[]")
                (cdir / f"cdx_{name}").write_text(json.dumps(rows))
                caps = rows[1:]
                yrs = Counter(r[0][:4] for r in caps)
                say(f"  wayback {name}: {len(caps)} distinct captures; by year {dict(sorted(yrs.items()))};")
                if caps:
                    say(f"    first {caps[0][0]} last {caps[-1][0]}; status {Counter(r[1] for r in caps).most_common(3)}")
            except Exception as e:
                say(f"  wayback {name}: FAIL {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(1.0)
    finally:
        await http.aclose()
        await wb.aclose()


async def stage_wayback_fetch(cache: Path, name: str, stamps: list[str]) -> None:
    """Fetch given Wayback captures (raw, `id_` suffix) of one Cleveland file into the cache. One attempt per
    capture (no retries): web.archive.org answered HTTP 429 to every request from the server on 26.11, and a 429
    is recorded, not worked around."""
    from tradedesk.core.http import Http

    cdir = cache / "cleveland" / "wayback"
    cdir.mkdir(parents=True, exist_ok=True)
    wb = Http("wayback", WAYBACK, rate=0.3, timeout=90.0, retries=0, headers=UA)
    path = next(p for p in CF_FILES if p.endswith(name))
    try:
        for st in stamps:
            p = cdir / f"{st}_{name}"
            if p.exists():
                continue
            try:
                f = await wb.get_bytes(f"/web/{st}id_/https://www.clevelandfed.org{path}", tool="wayback-raw")
                p.write_bytes(bytes(f.data))
                say(f"  capture {st}: {len(bytes(f.data)):,} B")
            except Exception as e:
                say(f"  capture {st}: FAIL {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(2.0)
    finally:
        await wb.aclose()


CF_DOCS = (
    "/-/media/project/clevelandfedtenant/clevelandfedsite/indicators-and-data/inflation-nowcasting/nowcasting_faqs.pdf",
    "/-/media/project/clevelandfedtenant/clevelandfedsite/indicators-and-data/inflation-nowcasting/nowcasting_users_guide.pdf",
    "/-/media/project/clevelandfedtenant/clevelandfedsite/indicators-and-data/inflation-nowcasting/"
    "inflation-nowcasting-model-methodological-approach-to-missing-october-and-november-2025-cpi-data.pdf",
)


def summarize_cf(cache: Path, fname: str, series: str, since: str) -> None:
    from datetime import date

    d = json.loads((cache / "cleveland" / fname).read_text())
    say(f"{fname} / {series!r}: {len(d)} target months, file stamp {d[-1]['chart'].get('_comment')}")
    cover = Counter()
    rows = []
    for ch in d:
        target, pts, vl = cf_daily(ch, series)
        ty, tm = (int(x) for x in target.split("-"))
        if (ty, tm) < tuple(int(x) for x in since.split("-")):
            continue
        lab = f"CPI {MONTHS[tm - 1]}"
        rel = vl.get(lab)
        if rel is None:
            rows.append((target, None, 0, len(pts)))
            cover["no_release_vline"] += 1
            continue
        r = date.fromisoformat(rel)
        # rel is the last point before the vline = the BLS release day (checked against the rule dates and
        # closedTime of the Polymarket events, --stage anchors)
        have = {(r - date.fromisoformat(p)).days for p, v in pts if v is not None and p <= rel}
        k = [x for x in range(0, 8) if x in have]
        rows.append((target, rel, len(k), len(pts)))
        cover["targets"] += 1
        for x in (1, 3, 5, 7):
            # a point x..x+2 calendar days before the release (x or a little further back, over weekends)
            cover[f"pt_at_lag{x}_to_{x + 2}d"] += int(any(x <= y <= x + 2 for y in have))
    say(f"  coverage: {dict(cover)}")
    say("  target  last-point-before-'CPI <mon>'-vline  #offsets in 0..7 with a value  #points")
    for t, rel, k, n in rows[-36:]:
        say(f"  {t:8} {rel or '-':12} {k:2} {n:4}")


async def stage_cfdocs(cache: Path) -> None:
    from tradedesk.core.http import Http

    cdir = cache / "cleveland"
    http = Http("clevelandfed", CF_BASE, rate=0.5, timeout=30.0, retries=1, headers=UA)
    try:
        for p in CF_DOCS:
            name = p.rsplit("/", 1)[1]
            try:
                f = await http.get_bytes(p, tool="cf-doc")
                (cdir / name).write_bytes(bytes(f.data))
                say(f"doc {name}: {len(bytes(f.data)):,} B")
            except Exception as e:
                say(f"doc {name}: FAIL {type(e).__name__}: {str(e)[:200]}")
    finally:
        await http.aclose()


async def stage_cdx(cache: Path, pause: float) -> None:
    from tradedesk.core.http import Http

    cdir = cache / "cleveland"
    wb = Http("wayback", WAYBACK, rate=0.1, timeout=90.0, retries=2, headers=UA)
    try:
        for path in CF_FILES:
            name = path.rsplit("/", 1)[1]
            params = {
                "url": "clevelandfed.org" + path,
                "matchType": "prefix",
                "output": "json",
                "fl": "timestamp,original,statuscode,digest",
            }
            try:
                f = await wb.get_bytes("/cdx/search/cdx", params=params, tool="wayback-cdx")
                rows = json.loads(bytes(f.data) or b"[]")
                (cdir / f"cdx_{name}").write_text(json.dumps(rows))
                caps = [r for r in rows[1:] if r[2] == "200"]
                dig = {r[3] for r in caps}
                yrs = Counter(r[0][:4] for r in caps)
                say(
                    f"wayback {name}: {len(rows) - 1} rows, {len(caps)} with 200, {len(dig)} distinct digests; by year {dict(sorted(yrs.items()))}"
                )
                if caps:
                    say(f"  first {caps[0][0]} last {caps[-1][0]}; e.g. {[r[0] for r in caps[-8:]]}")
            except Exception as e:
                say(f"wayback {name}: FAIL {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(pause)
    finally:
        await wb.aclose()


async def stage_avail(cache: Path, name: str, stamps: list[str], pause: float, query: str = "") -> None:
    """Wayback availability API (archive.org, not the CDX host): closest capture of one Cleveland file to each date;
    the raw capture is then fetched with the `id_` suffix and kept in the cache for `--stage compare`."""
    from tradedesk.core.http import Http

    path = next(p for p in CF_FILES if p.endswith(name)) + query
    wdir = cache / "cleveland" / "wayback"
    wdir.mkdir(parents=True, exist_ok=True)
    av = Http("archive-avail", "https://archive.org", rate=0.1, timeout=60.0, retries=1, headers=UA)
    wb = Http("wayback", WAYBACK, rate=0.1, timeout=120.0, retries=1, headers=UA)
    try:
        for st in stamps:
            try:
                f = await av.get_json("/wayback/available", params={"url": "www.clevelandfed.org" + path, "timestamp": st}, ttl=0)
                snap = ((f.data or {}).get("archived_snapshots") or {}).get("closest") or {}
                say(f"  avail {name} @{st}: {snap.get('timestamp')} status {snap.get('status')} available {snap.get('available')}")
                ts = snap.get("timestamp")
                if ts and snap.get("status") == "200" and not (wdir / f"{ts}_{name}").exists():
                    await asyncio.sleep(pause)
                    g = await wb.get_bytes(f"/web/{ts}id_/https://www.clevelandfed.org{path}", tool="wayback-raw")
                    (wdir / f"{ts}_{name}").write_bytes(bytes(g.data))
                    say(f"    fetched {len(bytes(g.data)):,} B")
            except Exception as e:
                say(f"  avail {name} @{st}: FAIL {type(e).__name__}: {str(e)[:160]}")
            await asyncio.sleep(pause)
    finally:
        await av.aclose()
        await wb.aclose()


VINTAGE_TARGETS = ("2024-3", "2024-9", "2025-3", "2025-8", "2026-3")


def _fred_mom(csv_text: str) -> dict[str, float]:
    """FRED CSV (DATE,value) of an index -> {YYYY-MM: month-over-month % change}. No change is given across a
    missing month (e.g. October 2025, never published), so a 2-month move is not mistaken for a monthly one."""
    lv: dict[str, float] = {}
    for line in csv_text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1] not in ("", "."):
            try:
                lv[parts[0][:7]] = float(parts[1])
            except ValueError:
                continue
    out = {}
    for ym, v in lv.items():
        prev = _prev_months(ym, 1)[0]
        if prev in lv:
            out[ym] = 100.0 * (v / lv[prev] - 1.0)
    return out


def _prev_months(target: str, n: int) -> list[str]:
    y, m = (int(x) for x in target.split("-"))
    out = []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return out


async def fetch_fred(p: Path, series: str) -> None:
    """Download today's vintage of a FRED series as CSV into ``p`` (core.http.Http, public graph endpoint)."""
    from tradedesk.core.http import Http

    fred = Http("fred", "https://fred.stlouisfed.org", rate=0.5, timeout=30.0, retries=1, headers=UA)
    try:
        f = await fred.get_bytes("/graph/fredgraph.csv", params={"id": series}, tool="fredgraph")
        p.write_bytes(bytes(f.data))  # noqa: ASYNC240 - one small file
    finally:
        await fred.aclose()


def stage_sigma(cache: Path, nowcast: Path, before: str, vintage: str | None = None) -> None:
    """Archive SD of the registration (card, «Попередня реєстрація» п. 4): headline YoY nowcast errors against the
    published CPI-U NSA 12-month change, from the nowcast file that passed V2 (``nowcast``) and today's CPIAUCNS
    (FRED, cached per day under <cache>/cleveland/fred/). Nowcast vs actual CPI only: no Polymarket price is read.
    ``vintage`` (YYYYMMDD) reads an already cached CSV instead of today's and never fetches."""
    from tradedesk.analytics.pm_cpi import archive_errors, sigma_archive, yoy_published

    if not nowcast.is_file():
        raise SystemExit(f"sigma: no nowcast file {nowcast} (see the 'exported' / 'NOT exported' lines of --stage row --export)")
    vdir = cache / "cleveland" / "fred"
    vdir.mkdir(parents=True, exist_ok=True)
    p = vdir / f"CPIAUCNS_{vintage or f'{datetime.now(UTC):%Y%m%d}'}.csv"
    if not p.exists():
        if vintage:
            raise SystemExit(f"sigma: no cached CPI vintage {p.name}")
        asyncio.run(fetch_fred(p, "CPIAUCNS"))
    levels = {}
    for line in p.read_text(encoding="utf-8").splitlines()[1:]:  # UTF-8, not the locale's (item 492, third review C, Т4)
        parts = line.split(",")
        if len(parts) >= 2 and parts[1] not in ("", "."):
            levels[parts[0][:7]] = parts[1]
    rows = archive_errors(json.loads(nowcast.read_text(encoding="utf-8")), yoy_published(levels), before)
    sd, n = sigma_archive(rows)
    for r in rows:
        say(f"  {r['target']:7} release {r['release']} point {r['day']} N {r['nowcast']} A {r['actual']} "
            f"err {r['error']} {r['why'] or ''}")  # fmt: skip
    used = [r["target"] for r in rows if r["error"] is not None]
    say(f"nowcast {nowcast} fred {p.name} before {before}: n {n} ({used[0] if used else '-'}..{used[-1] if used else '-'}), "
        f"left out {len(rows) - n}, sigma_A {sd if sd is None else round(sd, 4)}")  # fmt: skip


async def stage_vintage(cache: Path) -> None:
    """Is the archived daily nowcast as published? The FAQ defines the core CPI MoM nowcast as the 12-month moving
    average of past core CPI MoM inflation, so it can be recomputed from the core CPI index (CPILFESL) of two data
    vintages: the one available on the nowcast day and today's (seasonal factors are revised each February). Only
    today's vintage is reachable without a key, so this tells "not recomputed on today's data" and nothing more. Core
    CPI MoM changes only at a CPI release, so it cannot show whether daily headline points were overwritten: that
    needs two dated copies of the file (--stage compare --capture)."""
    from datetime import date, timedelta

    from tradedesk.core.http import Http

    vdir = cache / "cleveland" / "fred"
    vdir.mkdir(parents=True, exist_ok=True)
    fred = Http("fred", "https://fred.stlouisfed.org", rate=0.5, timeout=30.0, retries=1, headers=UA)
    month = {ch["chart"]["subcaption"]: ch for ch in json.loads((cache / "cleveland" / "nowcast_month.json").read_text())}

    async def get(http: Http, key: str, params: dict[str, str]) -> str | None:
        p = vdir / f"{key}.csv"
        if p.exists():
            return p.read_text()
        try:
            f = await http.get_bytes("/graph/fredgraph.csv", params=params, tool="fredgraph")
            txt = bytes(f.data).decode("utf-8", "replace")
            p.write_text(txt)
            return txt
        except Exception as e:
            say(f"  {key}: FAIL {type(e).__name__}: {str(e)[:160]}")
            return None

    try:
        cur = await get(fred, "CPILFESL_current", {"id": "CPILFESL"})  # today's vintage only
        cur_mom = _fred_mom(cur or "")
        say(f"current vintage: {len(cur_mom)} months, last {max(cur_mom) if cur_mom else '-'}")
        # Scan: stored core CPI MoM nowcast (point >= 3 days before the target's release) vs the same formula on
        # today's data. Recomputed history would match everywhere (~1e-6); as-published history differs where
        # the 12-month window was revised after the nowcast day (February seasonal-factor revisions).
        by_year: dict[str, Counter] = {}
        for tgt, ch in month.items():
            ty, tm = (int(x) for x in tgt.split("-"))
            if not (2014, 1) <= (ty, tm) <= (2025, 9):
                continue
            _, pts, vl = cf_daily(ch, "Core CPI Inflation")
            rel = vl.get(f"CPI {MONTHS[tm - 1]}")
            prev = _prev_months(tgt, 12)
            if rel is None or not all(m in cur_mom for m in prev):
                continue
            cut = (date.fromisoformat(rel) - timedelta(days=3)).isoformat()
            val = next((v for d, v in reversed(pts) if d <= cut and v is not None), None)
            if val is None:
                continue
            diff = abs(val - sum(cur_mom[m] for m in prev) / 12)
            c = by_year.setdefault(str(ty), Counter())
            c["n"] += 1
            c["match_1e-5"] += int(diff < 1e-5)
            c["diff_ge_1e-4"] += int(diff >= 1e-4)
        say("core CPI MoM nowcast vs 12-month average of today's CPILFESL (per target year):")
        for y, c in sorted(by_year.items()):
            say(f"  {y}: {dict(c)}")
        # The old-vintage side (ALFRED) is not available without a key: alfred.stlouisfed.org/graph/fredgraph.csv
        # answers 404 and fred.stlouisfed.org ignores `vintage_date` (26.11), so only today's side is printed.
        say("  target  nowcast-day  stored     12m-avg(today)  |stored-today|")
        for tgt in VINTAGE_TARGETS:
            ty, tm = (int(x) for x in tgt.split("-"))
            _, pts, vl = cf_daily(month[tgt], "Core CPI Inflation")
            rel = vl.get(f"CPI {MONTHS[tm - 1]}")
            if rel is None:
                continue
            # the nowcast point three calendar days or more before the release of the target month
            cut = (date.fromisoformat(rel) - timedelta(days=3)).isoformat()
            day, val = next(((d, v) for d, v in reversed(pts) if d <= cut and v is not None), (None, None))
            prev = _prev_months(tgt, 12)
            if day is None or not all(m in cur_mom for m in prev):
                continue
            a_cur = sum(cur_mom[m] for m in prev) / 12
            say(f"  {tgt:7} {day}  {val:.6f}  {a_cur:.6f}  {abs(val - a_cur):.2e}")
    finally:
        await fred.aclose()


def compare_points(
    old: list[dict[str, Any]], new: list[dict[str, Any]], series: str, targets: set[str] | None = None
) -> tuple[Counter, list[tuple[float, str, str]]]:
    """Every (target month, date) with a value of `series` in both copies of a Cleveland nowcast file: counts of
    pairs, exact matches (< 1e-9) and the differing points, largest first. Equal values on all shared points mean
    no past daily point was overwritten between the two copies."""
    nmap = {ch["chart"]["subcaption"]: ch for ch in new}
    c: Counter = Counter()
    diffs: list[tuple[float, str, str]] = []
    for ch in old:
        tgt = ch["chart"]["subcaption"]
        if targets is not None and tgt not in targets:
            continue
        if tgt not in nmap:
            c["target_missing_new"] += 1
            continue
        dn = dict(cf_daily(nmap[tgt], series)[1])
        for dte, v in cf_daily(ch, series)[1]:
            if v is None:
                continue
            w = dn.get(dte)
            if w is None:
                c["missing_new"] += 1
                continue
            c["pairs"] += 1
            if abs(w - v) < 1e-9:
                c["equal"] += 1
            else:
                diffs.append((abs(w - v), tgt, dte))
    diffs.sort(reverse=True)
    c["differ"] = len(diffs)
    return c, diffs


def compare_capture(old_path: Path, new_path: Path, series_list: tuple[str, ...], recent: int = 3) -> None:
    """Vintage check between two dated copies of one Cleveland file (a Wayback capture or an earlier snapshot as
    `old_path`, a later fetch as `new_path`): all shared points per series, and separately the `recent` newest
    target months of the old copy (the ones whose daily points matter for the next release)."""
    old = json.loads(old_path.read_text(encoding="utf-8"))
    new = json.loads(new_path.read_text(encoding="utf-8"))
    so, sn = old[-1]["chart"].get("_comment"), new[-1]["chart"].get("_comment")
    say(f"old {old_path} (stamp {so}) vs new {new_path} (stamp {sn})")
    if so == sn:
        say("  WARNING: same file stamp; equal values prove nothing about overwriting (no update in between)")
    rec = {ch["chart"]["subcaption"] for ch in old[-recent:]}
    for s in series_list:
        c, diffs = compare_points(old, new, s)
        cr, dr = compare_points(old, new, s, rec)
        say(f"  {s}: all targets {dict(c)}; largest {[(round(a, 4), t, d) for a, t, d in diffs[:5]]}")
        say(f"    newest {sorted(rec)}: {dict(cr)}; differing points {[(round(a, 4), t, d) for a, t, d in dr[:8]]}")
    say_new_points(old, new, series_list, recent)


def stage_vcheck(old_path: Path, new_path: Path, step_target: str | None = None) -> bool:
    """V1 / V2 of the registration (card п. 4) on one pair of stored copies of a nowcast file: the full comparison
    (compare_capture), then one verdict line from analytics.pm_cpi.vintage_why: 'V pass' or 'V fail: <reason>'. A
    missing or unreadable file is a fail, never a skipped check, and so is any exception of the comparison or of the
    check (review B 481, third round, finding 7): the last line is always 'V pass' or 'V fail: ...'."""
    try:  # UTF-8, not the locale's: a UnicodeDecodeError is a verdict of the data (item 492, third review C, Т4)
        old, new = json.loads(old_path.read_text(encoding="utf-8")), json.loads(new_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        say(f"V fail: cannot read a copy ({type(e).__name__}: {e})")
        return False
    try:
        from tradedesk.analytics.pm_cpi import vintage_why

        compare_capture(old_path, new_path, ("CPI Inflation", "Core CPI Inflation"))
        why = vintage_why(old, new, step_target)
    except Exception as e:  # noqa: BLE001 - an unexpected structure fails the check, it never drops the verdict line
        why = f"check raised {type(e).__name__}: {e}"
    say(f"{old_path.name} vs {new_path.name}" + (f", step target {step_target}" if step_target else ""))
    say("V pass" if why is None else f"V fail: {why}")
    return why is None


def v1_older_why() -> str | None:
    """Why the files of S (``V1_OLDER``) are not the registered ones: missing, unreadable or other bytes than
    ``V1_OLDER_SHA256`` (None: they are). The proof of scripts/pm_cpi_forward.py asks this before it re-runs V1: such
    an S gives no verdict there, the V1 line is «not recomputed» (item 492, review C, blocker 2)."""
    for name, want in V1_OLDER_SHA256.items():
        p = V1_OLDER / name
        try:
            got = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:
            return f"{p} unreadable ({type(e).__name__})"
        if got != want:
            return f"{p} has sha256 {got[:12]}…, not {want[:12]}…"
    return None


def say_new_points(old: Any, new: Any, series_list: tuple[str, ...], recent: int = 3) -> None:
    """Re-review of item 479, point 1: for the `recent` newest targets of the older copy, the newer copy's daily points
    after the older copy's stamp and how many distinct values they have (condition (b) of 03.12: headline 2026-11 got a
    new value after 25.11 = `changed` True)."""
    say(f"  new daily points after the old copy's stamp, {recent} newest targets of the old copy:")
    for s in series_list:
        for r in new_points_after(old, new, s, recent):
            if r["n"] is None:
                say(f"    {s:18} {r['target']:8} after {r['after']}: target not in the new copy")
            else:
                span = f" ({r['first']} .. {r['last']})" if r["n"] else ""
                say(
                    f"    {s:18} {r['target']:8} after {r['after']}: {r['n']} points{span}, distinct values {r['distinct']}, new value vs old last: {r['changed']}"
                )


def window_moves(pts: list[tuple[str, float | None]], release: str, lo: int = 7, hi: int = 1) -> tuple[int, int]:
    """(points with a value, distinct values) among the daily points dated from `lo` to `hi` calendar days before
    the release day. Distinct values > 1 = the series moves inside the T-7..T-1 window."""
    a, b = date_shift(release, -lo), date_shift(release, -hi)
    vals = [round(v, 6) for d, v in pts if v is not None and a <= d <= b]
    return len(vals), len(set(vals))


def summarize_moves(cache: Path, fname: str, since: str = "2024-1") -> None:
    """How often headline and core move inside T-7..T-1 of each release (why a core-only vintage test does not
    cover headline). Counts only; no value is compared with the CPI outcome."""
    d = json.loads((cache / "cleveland" / fname).read_text())
    rel = cf_release_days(d)
    sy, sm = (int(x) for x in since.split("-"))
    for s in ("CPI Inflation", "Core CPI Inflation"):
        dist, life = [], []
        for ch in d:
            t = ch["chart"]["subcaption"]
            ty, tm = (int(x) for x in t.split("-"))
            if (ty, tm) < (sy, sm) or t not in rel:
                continue
            pts = cf_daily(ch, s)[1]
            dist.append(window_moves(pts, rel[t])[1])
            life.append(window_moves(pts, rel[t], lo=400)[1])
        dist.sort()
        life.sort()
        say(
            f"{fname} {s!r}: {len(dist)} targets; distinct values in T-7..T-1: median {dist[len(dist) // 2]}"
            f" [min {dist[0]}, max {dist[-1]}], targets with 1 value {sum(1 for x in dist if x == 1)};"
            f" over the whole life to T-1: median {life[len(life) // 2]} [min {life[0]}, max {life[-1]}]"
        )


# ---------------------------------------------------------------------------------------------------- snapshot


def snapshot_series(cache: Path, given: list[str]) -> tuple[list[str], str | None]:
    """Series ids the snapshot lists: `--series` when given, else those of the cache's events/*_open.json, else
    DEFAULT_SERIES with a warning (re-review of item 479, point 6: an emptied cache must not give a snapshot without
    books in silence)."""
    if given:
        return list(given), None
    found = sorted({p.stem.split("_")[0] for p in (cache / "events").glob("*_open.json")})
    if found:
        return found, None
    return list(DEFAULT_SERIES), f"WARNING: no series in {cache / 'events'}/*_open.json; using {' '.join(DEFAULT_SERIES)}"


async def stage_snapshot(cache: Path, root: Path | None = None, series: list[str] | None = None) -> None:
    """Today's nowcast file and the books of every bracket of the open CPI events. `root` (default <cache>/snapshots):
    ~/.local/state/tradedesk-agents/pm_cpi/snapshots is covered by the server backups (~/.cache is not). Since item 480
    the collector job pm_cpi_snapshot writes the same data to the database daily. Data preservation only; never a
    source of a forward stage (registration п. 5: scripts/pm_cpi_forward.py reads the job's row alone)."""
    from tradedesk.core.http import Http
    from tradedesk.server import build_server

    sids, warn = snapshot_series(cache, series or [])
    if warn:
        say(warn)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%MZ")
    sdir = (root or cache / "snapshots") / stamp
    sdir.mkdir(parents=True, exist_ok=True)
    http = Http("clevelandfed", CF_BASE, rate=0.5, timeout=30.0, retries=1, headers=UA)
    try:
        # events are read from the cache's series list (events/*_open.json) even when the output goes elsewhere
        for path in (CF_FILES[0], CF_FILES[2]):
            name = path.rsplit("/", 1)[1]
            try:
                f = await http.get_bytes(path, tool="cf-json")
                (sdir / name).write_bytes(bytes(f.data))
                d = json.loads(bytes(f.data))
                tgt, pts, _ = cf_daily(d[-1], "CPI Inflation")
                last = [(x, v) for x, v in pts if v is not None][-1:]
                say(f"{name}: {len(bytes(f.data)):,} B, stamp {d[-1]['chart'].get('_comment')}, latest target {tgt}, CPI point {last}")
            except Exception as e:
                say(f"{name} FAIL {type(e).__name__}: {str(e)[:200]}")
    finally:
        await http.aclose()
    _, rt = build_server()
    try:
        opens = []
        for sid in sids:
            f = await rt.polymarket.events_all({"series_id": sid, "closed": "false"})
            opens.extend(f.data["rows"])
        (sdir / "events_open.json").write_text(json.dumps(opens))
        if not opens:
            say(f"WARNING: no open event in series {' '.join(sids)}: this snapshot has no books")
        for ev in opens:
            ms = [m for m in ev.get("markets") or [] if not m.get("closed")]
            toks = [(m.get("groupItemTitle"), _jl(m.get("clobTokenIds"))) for m in ms]
            toks = [(t, x[0]) for t, x in toks if x]
            if not toks:
                continue
            b = await rt.polymarket.books([x for _, x in toks])
            (sdir / f"books_{ev['id']}.json").write_text(json.dumps(b.data))
            bmap = {bk.get("asset_id"): bk for bk in b.data if isinstance(bk, dict)}
            say(f"event {ev.get('slug')} end {ev.get('endDate')} vol ${float(ev.get('volume') or 0):,.0f}")
            say("  bracket            bid    ask   spread  bid_sz$  ask_sz$")
            for title, tok in toks:
                bk = bmap.get(tok) or {}
                bids = sorted(((float(x["price"]), float(x["size"])) for x in bk.get("bids") or []), reverse=True)
                asks = sorted((float(x["price"]), float(x["size"])) for x in bk.get("asks") or [])
                bb, ba = (bids[0] if bids else (None, 0.0)), (asks[0] if asks else (None, 0.0))
                spr = f"{ba[0] - bb[0]:.3f}" if bb[0] is not None and ba[0] is not None else "  -  "
                say(
                    f"  {str(title)[:16]:16} {bb[0] if bb[0] is not None else '-':>6} {ba[0] if ba[0] is not None else '-':>6}"
                    f"  {spr:>6} {bb[1] * (bb[0] or 0):8.0f} {ba[1] * (ba[0] or 0):8.0f}"
                )
    finally:
        await rt.polymarket.aclose()
    say(f"snapshot saved to {sdir}")


# ---------------------------------------------------------------------------------------------------- job row


def read_rows(db: Path, day: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the pm_cpi_snapshot row of `day`, the latest earlier row with a nowcast copy), read-only: the database is opened
    with mode=ro, so this never writes the live collector database (no migration, no WAL switch). A file kept only as a
    reference (`cf_<kind>_ref`: same bytes as an earlier row's) is resolved into `cf_<kind>_z` when that row's sha256
    matches; `_<kind>_from` then names the day it came from."""
    import sqlite3

    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM pm_cpi_snapshot WHERE day = ?", (day,)).fetchone()
        prev = con.execute(
            "SELECT * FROM pm_cpi_snapshot WHERE day < ? AND (cf_year_z IS NOT NULL OR cf_year_ref IS NOT NULL) ORDER BY day DESC LIMIT 1",
            (day,),
        ).fetchone()
        out = [dict(r) if r else None for r in (row, prev)]
        for r in out:
            for kind in ("year", "month"):
                ref = (r or {}).get(f"cf_{kind}_ref")
                if r is None or r.get(f"cf_{kind}_z") is not None or not ref:
                    continue
                src = con.execute(f"SELECT cf_{kind}_z, cf_{kind}_sha FROM pm_cpi_snapshot WHERE day = ?", (ref,)).fetchone()
                if src is not None and src[0] is not None and src[1] == r.get(f"cf_{kind}_sha"):
                    r[f"cf_{kind}_z"], r[f"_{kind}_from"] = src[0], ref
    finally:
        con.close()
    return out[0], out[1]


def _hm(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts), UTC).strftime("%H:%M:%S UTC") if ts else "-"


def _hm_ms(ts_ms: Any) -> str:
    return datetime.fromtimestamp(int(ts_ms) / 1000, UTC).strftime("%H:%M:%S UTC") if ts_ms else "-"


def _charts_of(row: dict[str, Any], kind: str) -> Any:
    blob = row.get(f"cf_{kind}_z")
    if blob is None:
        return None
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8-sig"))
    except (zlib.error, UnicodeDecodeError, ValueError, RecursionError):
        return None  # kept as raw bytes because it did not parse (the row's missing says why)


def _say_brackets(brs: list[dict[str, Any]], early: bool = False) -> None:
    by: dict[str, list[dict[str, Any]]] = {}
    for b in brs:
        by.setdefault(str(b.get("event_slug")), []).append(b)
    for slug, bs in by.items():
        say(
            f"  event {slug} end {bs[0].get('end_date')}: {len(bs)} brackets, quoted {sum(1 for b in bs if b.get('bid') is not None and b.get('ask') is not None)}"
        )
        for b in bs:
            usd = f"{b.get('bid_usd') or 0:8.0f} {b.get('ask_usd') or 0:8.0f}" if b.get("book") else "  no book"
            sup = f"  superseded {_hm(b.get('superseded_ts'))}" if early else ""
            sup += f"  closed (seen {_hm(b.get('gone_ts'))}), not in the pair check" if b.get("gone_ts") else ""
            say(
                f"    {str(b.get('title'))[:16]:16} {b.get('bid') if b.get('bid') is not None else '-':>6} {b.get('ask') if b.get('ask') is not None else '-':>6} {usd}"
                f"  book {_hm_ms(b.get('book_ts_ms'))}  read {_hm(b.get('read_ts'))}{sup}"
            )


def stage_row(db: Path, day: str, export: Path | None = None) -> bool:
    """The collector's pm_cpi_snapshot row of one day (item 480): nowcast stamps and values, where each file's bytes are,
    the books of every bracket (venue book time and read time; the early set too when a re-read superseded it), the
    full-book blob's size, what is missing; then the vintage check against the latest earlier row (all shared points,
    and the new daily points after the earlier copy's stamp for its 3 newest targets). `export`: also write the stored files as
    <day>_nowcast_year.json / <day>_nowcast_month.json there (inputs for --stage compare). False = no row."""
    import sqlite3

    try:
        row, prev = read_rows(db, day)
    except sqlite3.Error as e:
        say(f"cannot read pm_cpi_snapshot in {db}: {type(e).__name__}: {e}")
        return False
    if row is None:
        say(f"NO ROW for {day} in {db}")
        return False
    r = row
    if export is not None:
        # first, before any descriptive line (item 492, review C N1, the mirror case: a row whose books do not read
        # must not stop the export); a stored blob that does not decompress is «NOT exported», as a file not stored
        export.mkdir(parents=True, exist_ok=True)
        for kind in ("year", "month"):
            # a file kept as a reference was resolved by read_rows: its bytes (from the source row, same sha256) are
            # written under this row's day, so the commands of the registration always name <day>_nowcast_<kind>.json
            p = export / f"{day}_nowcast_{kind}.json"
            if r.get(f"cf_{kind}_z") is not None:
                try:
                    data = zlib.decompress(r[f"cf_{kind}_z"])
                except zlib.error as e:
                    say(f"  NOT exported {p.name}: the stored bytes do not decompress ({type(e).__name__})")
                    continue
                p.write_bytes(data)
                src = r.get(f"_{kind}_from")
                say(f"  exported {p} (sha256 {r.get(f'cf_{kind}_sha')}" + (f", bytes of row {src})" if src else ")"))
            else:
                ref = r.get(f"cf_{kind}_ref")
                why = f"reference to {ref} not resolved (no such stored file with that sha256)" if ref else "no file in the row"
                say(f"  NOT exported {p.name}: {why}")
    say(
        f"row {day}: status {r['status']}, late {r['late']}, created {_hm(r['created_ts'])}, updated {_hm(r['updated_ts'])};"
        f" nowcast attempts {r['cf_tries']} (downloads {r['cf_fetches']}), books attempts {r['books_tries']}"
    )
    for kind in ("year", "month"):
        z, src, ref = r.get(f"cf_{kind}_z"), r.get(f"_{kind}_from"), r.get(f"cf_{kind}_ref")
        if src:
            where = f"same bytes as row {src}"
        elif z is not None:
            where = f"stored here ({len(z):,} B zlib)"
        else:
            where = f"reference to {ref} NOT resolved" if ref else "no file"
        say(
            f"  nowcast_{kind}.json: stamp {r.get(f'cf_stamp_{kind}')!r}, last daily point {r.get(f'cf_last_day_{kind}')},"
            f" first downloaded {_hm(r.get(f'cf_{kind}_ts'))}, {where}"
        )
    say(f"  fresh (stamp or last point = {day}): {r['cf_fresh']}; latest download {_hm(r.get('cf_fetch_ts'))}")
    for lab, pre in (("target", ""), ("previous", "prev_")):
        say(
            f"  {lab} {r[f'{pre}target'] if pre else r['target']}: YoY headline {r[f'{pre}yoy_headline_pct']} core {r[f'{pre}yoy_core_pct']};"
            f" MoM headline {r[f'{pre}mom_headline_pct']} core {r[f'{pre}mom_core_pct']} (percent)"
        )
    say(f"  MoM file's own target: {r.get('mom_target')}")
    books = json.loads(r["books"]) if r["books"] else {}
    brs = books.get("brackets") or []
    say(
        f"  books (main set): series listed {books.get('series_listed')}, events {r['n_events']}, brackets {r['n_brackets']},"
        f" with bid and ask {r['n_quoted']}, last read {_hm(r['books_ts'])}, paired with the nowcast {r.get('books_paired')}"
    )
    say(f"  listing per series: {books.get('counts')}")
    _say_brackets(brs)
    early = (json.loads(r["books_early"]) if r.get("books_early") else {}).get("brackets") or []
    if early:
        say(f"  books (early set, read before a nowcast file stored later): {len(early)} books")
        _say_brackets(early, early=True)
    if r.get("books_depth_z"):
        d = json.loads(zlib.decompress(r["books_depth_z"]))
        lv = sum(len(x.get("bids") or []) + len(x.get("asks") or []) for x in d.get("main", {}).values())
        say(
            f"  full books: {len(r['books_depth_z']):,} B zlib, main {len(d.get('main') or {})} books / {lv} levels,"
            f" early {len(d.get('early') or [])} books"
        )
    say(f"  missing (latest attempt): {r['missing'] or 'none'}")
    if prev is None:
        say("  no earlier row with a nowcast copy: nothing to compare")
        return True
    for kind in ("year", "month"):
        old, new = _charts_of(prev, kind), _charts_of(r, kind)
        if old is None or new is None:
            say(f"  vintage nowcast_{kind}: a copy is missing, not compared")
            continue
        if prev.get(f"cf_{kind}_sha") and prev.get(f"cf_{kind}_sha") == r.get(f"cf_{kind}_sha"):
            say(f"  vintage nowcast_{kind}: row {prev['day']} has the same bytes (sha256): no update in between")
            continue
        say(
            f"  vintage nowcast_{kind}: row {prev['day']} (stamp {prev.get(f'cf_stamp_{kind}')!r}) vs row {day} (stamp {r.get(f'cf_stamp_{kind}')!r})"
        )
        for s in ("CPI Inflation", "Core CPI Inflation"):
            c, diffs = compare_points(old, new, s)
            say(f"    {s}: all targets {dict(c)}; largest {[(round(a, 4), t, d) for a, t, d in diffs[:5]]}")
        say_new_points(old, new, ("CPI Inflation", "Core CPI Inflation"))
    return True


def main() -> None:
    global _OUT
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--stage",
        required=True,
        choices=[
            "discover",
            "list",
            "events",
            "hist",
            "histsum",
            "cleveland",
            "cdx",
            "avail",
            "vintage",
            "wayback",
            "compare",
            "moves",
            "anchors",
            "cfdocs",
            "cfsum",
            "snapshot",
            "row",
            "vcheck",
            "sigma",
        ],
    )
    ap.add_argument(
        "--before",
        help="sigma: ISO day; only targets whose release (the 'CPI <Mon>' line) is before it enter (registration: 2027-01-01)",
    )
    ap.add_argument(
        "--vintage", help="sigma: YYYYMMDD of a CPIAUCNS CSV already in <cache>/cleveland/fred/ (read, never fetched; item 492)"
    )
    ap.add_argument("--step-target", help="vcheck: target 'YYYY-M' whose headline must have moved after the older stamp (V1: 2026-11)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="row: collector database (opened read-only)")
    ap.add_argument("--day", help="row: UTC day YYYY-MM-DD (default: today)")
    ap.add_argument("--export", type=Path, help="row: write the stored nowcast files of the day into this directory")
    ap.add_argument("--capture", type=Path, help="compare: older copy of the nowcast file (Wayback capture or snapshot)")
    ap.add_argument("--against", type=Path, help="compare: newer copy (default: <cache>/cleveland/<--file>)")
    ap.add_argument("--snap-root", type=Path, help="snapshot: parent directory (default: <cache>/snapshots)")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--series", nargs="*", default=[])
    ap.add_argument("--file", default="nowcast_month.json")
    ap.add_argument("--stamps", nargs="*", default=[])
    ap.add_argument("--query", default="", help="query suffix of the archived URL, e.g. '?sc_lang=en'")
    ap.add_argument("--fidelity", type=int, default=60)
    ap.add_argument("--pace", type=float, default=0.25)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("a", encoding="utf-8") as fh:  # read back as UTF-8 by the proof (item 492, third review C, Т4)
        _OUT = fh
        say(f"# stage {a.stage} at {datetime.now(UTC).isoformat(timespec='seconds')}")
        if a.stage == "discover":
            asyncio.run(stage_discover(a.cache_dir))
        elif a.stage == "list":
            asyncio.run(stage_list(a.cache_dir, a.series))
            summarize_events(a.cache_dir)
        elif a.stage == "events":
            summarize_events(a.cache_dir)
        elif a.stage == "hist":
            asyncio.run(stage_hist(a.cache_dir, a.fidelity, a.pace))
        elif a.stage == "histsum":
            summarize_hist(a.cache_dir)
        elif a.stage == "cleveland":
            asyncio.run(stage_cleveland(a.cache_dir))
        elif a.stage == "wayback":
            asyncio.run(stage_wayback_fetch(a.cache_dir, a.file, a.stamps))
        elif a.stage == "avail":
            asyncio.run(stage_avail(a.cache_dir, a.file, a.stamps, 15.0, a.query))
        elif a.stage == "vintage":
            asyncio.run(stage_vintage(a.cache_dir))
        elif a.stage == "cdx":
            asyncio.run(stage_cdx(a.cache_dir, 20.0))
        elif a.stage == "cfdocs":
            asyncio.run(stage_cfdocs(a.cache_dir))
        elif a.stage == "cfsum":
            summarize_cf(a.cache_dir, "nowcast_year.json", "CPI Inflation", "2024-1")
            summarize_cf(a.cache_dir, "nowcast_month.json", "CPI Inflation", "2024-1")
        elif a.stage == "compare":
            new = a.against or a.cache_dir / "cleveland" / a.file
            caps = [a.capture] if a.capture else sorted((a.cache_dir / "cleveland" / "wayback").glob(f"*_{a.file}"))
            if not caps:
                say("no capture to compare (give --capture <file>)")
            for p in caps:
                compare_capture(p, new, ("CPI Inflation", "Core CPI Inflation"))
        elif a.stage == "vcheck":
            if not (a.capture and a.against):
                say("V fail: vcheck needs --capture <older copy> and --against <newer copy>")
                raise SystemExit(1)
            if not stage_vcheck(a.capture, a.against, a.step_target):
                raise SystemExit(1)
        elif a.stage == "moves":
            summarize_moves(a.cache_dir, "nowcast_year.json")
            summarize_moves(a.cache_dir, "nowcast_month.json")
        elif a.stage == "anchors":
            stage_anchors(a.cache_dir)
        elif a.stage == "snapshot":
            asyncio.run(stage_snapshot(a.cache_dir, a.snap_root, a.series))
        elif a.stage == "row":
            if not stage_row(a.db, a.day or datetime.now(UTC).date().isoformat(), a.export):
                raise SystemExit(1)
        elif a.stage == "sigma":
            if not (a.against and a.before):
                raise SystemExit("sigma needs --against <nowcast_year.json of row 11.12 (V2)> and --before 2027-01-01")
            if a.vintage is not None and not re.fullmatch(r"\d{8}", a.vintage):
                raise SystemExit("sigma: --vintage is YYYYMMDD")
            stage_sigma(a.cache_dir, a.against, a.before, a.vintage)


if __name__ == "__main__":
    main()
