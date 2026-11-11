"""IDEA-20261112-polymarket-sports-vs-sharp-book, steps 0-1 (data reachability and the power gate). No g is computed.

Network only through ``core.http.Http`` (football-data.co.uk) and the runtime provider ``rt.polymarket`` (Gamma, CLOB).
Per-file JSON/CSV cache under ``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm-sports-sharp``).

Stages (run in order):
    uv run python scripts/verify_pm_sports_step0.py --stage fd     --out <file>   # football-data CSVs: PSC*/AvgC* fill
    uv run python scripts/verify_pm_sports_step0.py --stage probe  --out <file>   # Gamma /sports, sample soccer event shape
    uv run python scripts/verify_pm_sports_step0.py --stage gamma  --out <file>   # closed 1X2 events per league series
    uv run python scripts/verify_pm_sports_step0.py --stage hist   --out <file>   # /prices-history non-empty rate (sample)
    uv run python scripts/verify_pm_sports_step0.py --stage gate   --out <file>   # fuzzy match + n_eff + MDE

The ``hist`` stage records only point counts and timestamps near kick-off, never the price values, so nothing about
g = p_PM - p_sharp is seen before the pre-registration is committed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import random
import resource
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradedesk.core.http import Http  # noqa: E402

FD_URL = "https://www.football-data.co.uk"
FD_LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
FD_SEASONS = ("2122", "2223", "2324", "2425", "2526", "2627")
# Gamma /sports codes of the same five leagues (checked in the probe stage before use)
PM_LEAGUES = {"epl": "E0", "lal": "SP1", "bun": "D1", "sea": "I1", "fl1": "F1"}
HIST_SAMPLE_PER_LEAGUE = 16
SEED = 7
SD_G_PRIOR = (0.03, 0.04, 0.05)  # a-priori SD of g per observation (card: 3 pp; critic: may be 4-5 pp)
MDE_K = 2.8  # 80 % power, two-sided 5 %
N_EFF_GATE = 120


class Out:
    def __init__(self, path: str | None) -> None:
        self.lines: list[str] = []
        self.path = Path(path).expanduser() if path else None

    def __call__(self, s: str = "") -> None:
        print(s, flush=True)
        self.lines.append(s)

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(self.lines) + "\n")


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def iso_ts(s: Any) -> int | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip().replace(" ", "T", 1)
    if s.endswith("+00"):
        s += ":00"
    s = s.replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return int(d.timestamp())


# ----------------------------------------------------------------------------- football-data


def fd_rows(cache: Path, league: str, season: str) -> list[dict[str, str]] | None:
    p = cache / "fd" / f"{season}_{league}.csv"
    if not p.exists():
        return None
    txt = p.read_bytes().decode("utf-8-sig", errors="replace")
    return [r for r in csv.DictReader(io.StringIO(txt)) if (r.get("HomeTeam") or "").strip()]


def fnum(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


def fd_date(s: str) -> str | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def devig_prop(odds: tuple[float, float, float]) -> tuple[float, float, float]:
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return (inv[0] / s, inv[1] / s, inv[2] / s)


async def stage_fd(cache: Path, out: Out) -> None:
    http = Http("football_data", FD_URL, rate=1.0, burst=1, timeout=30, retries=1)
    # The host answers the CSV path with a redirect (an empty 3xx body passed as success on 2026-11-12); show it once,
    # then let the same Http client (limiter, retries, health) follow redirects.
    r = await http._client.request("GET", "/mmz4281/2526/E0.csv")
    out(f"diagnostic GET /mmz4281/2526/E0.csv: HTTP {r.status_code}, location {r.headers.get('location')}, bytes {len(r.content)}")
    await http.aclose()
    http._client_args["follow_redirects"] = True
    (cache / "fd").mkdir(parents=True, exist_ok=True)
    try:
        for season in FD_SEASONS:
            for lg in FD_LEAGUES:
                p = cache / "fd" / f"{season}_{lg}.csv"
                if not p.exists() or p.stat().st_size == 0:
                    t0 = time.monotonic()
                    try:
                        f = await http.get_bytes(f"/mmz4281/{season}/{lg}.csv", tool="fd-csv")
                    except Exception as e:  # noqa: BLE001
                        out(f"{season} {lg}: ERROR {type(e).__name__}: {str(e)[:160]}")
                        continue
                    p.write_bytes(f.data)
                    out(f"{season} {lg}: fetched {len(f.data)} bytes in {time.monotonic() - t0:.1f}s")
    finally:
        await http.aclose()
    out("")
    out("season league   n   PSC*  PS*  AvgC*  B365C*  MaxC*   first..last date   (fill = all three closing odds present)")
    for season in FD_SEASONS:
        for lg in FD_LEAGUES:
            rows = fd_rows(cache, lg, season)
            if not rows:
                out(f"{season} {lg:4s}  missing or empty")
                continue

            def fill(cols: tuple[str, str, str], rows: list[dict[str, str]] = rows) -> str:
                if cols[0] not in rows[0]:
                    return "  -  "
                k = sum(1 for r in rows if all(fnum(r.get(c)) for c in cols))
                return f"{k:4d}"

            ds = sorted(d for d in (fd_date(r.get("Date", "")) for r in rows) if d)
            out(
                f"{season} {lg:4s} {len(rows):4d}  {fill(('PSCH', 'PSCD', 'PSCA'))} {fill(('PSH', 'PSD', 'PSA'))}"
                f"  {fill(('AvgCH', 'AvgCD', 'AvgCA'))}  {fill(('B365CH', 'B365CD', 'B365CA'))}  {fill(('MaxCH', 'MaxCD', 'MaxCA'))}"
                f"   {ds[0] if ds else '-'}..{ds[-1] if ds else '-'}"
            )
    # overround of the closing Pinnacle triple (sanity: a sharp book should be ~2-3 %)
    ov = []
    for season in ("2425", "2526"):
        for lg in FD_LEAGUES:
            for r in fd_rows(cache, lg, season) or []:
                o = [fnum(r.get(c)) for c in ("PSCH", "PSCD", "PSCA")]
                if all(o):
                    ov.append(sum(1 / x for x in o) - 1)  # type: ignore[operator]
    if ov:
        out(f"PSC overround 2024-26: n {len(ov)}, median {statistics.median(ov):.4f}, p90 {sorted(ov)[int(0.9 * len(ov))]:.4f}")
    out(f"peak RSS {rss_mb():.0f} MB")


# ----------------------------------------------------------------------------- Polymarket


def build_rt():
    from tradedesk.server import build_server

    _, rt = build_server()
    return rt


async def stage_probe(cache: Path, out: Out) -> None:
    rt = build_rt()
    pm = rt.polymarket
    try:
        f = await pm.gamma.get_json("/sports", ttl=0, tool="sports")
        (cache / "sports.json").write_text(json.dumps(f.data))
        out(f"/sports: {len(f.data)} entries; codes: {' '.join(sorted(str(s.get('sport')) for s in f.data))}")
        for s in f.data:
            if s.get("sport") in PM_LEAGUES or s.get("sport") in ("ucl", "uel"):
                out(f"  {s.get('sport')}: series {s.get('series')} tags {s.get('tags')}")
        # the card's literal probe
        try:
            g = await pm.gamma.get_json("/events", params={"tag": "soccer", "closed": "false", "limit": 5}, ttl=0, tool="events")
            out(f"/events?tag=soccer&closed=false&limit=5: {len(g.data)} rows; titles: {[e.get('title') for e in g.data][:5]}")
        except Exception as e:  # noqa: BLE001
            out(f"/events?tag=soccer: ERROR {str(e)[:160]}")
        epl = next((s for s in f.data if s.get("sport") == "epl"), None)
        if epl:
            g = await pm.gamma.get_json("/events", params={"series_id": epl["series"], "closed": "true", "limit": 3}, ttl=0, tool="events")
            (cache / "probe_epl_events.json").write_text(json.dumps(g.data))
            for ev in g.data:
                out(f"  event {ev.get('id')} {ev.get('slug')} | {ev.get('title')} | start {ev.get('startTime') or ev.get('startDate')}")
                for m in ev.get("markets") or []:
                    out(
                        f"    m {m.get('id')} smt={m.get('sportsMarketType')} git={m.get('groupItemTitle')} "
                        f"gst={m.get('gameStartTime')} q={str(m.get('question'))[:60]}"
                    )
            if g.data:
                out(f"  event keys: {sorted(g.data[0].keys())}")
                ms = g.data[0].get("markets") or []
                if ms:
                    out(f"  market keys: {sorted(ms[0].keys())}")
    finally:
        await rt.aclose()
    out.save()


def compact_event(ev: dict[str, Any], league: str) -> dict[str, Any] | None:
    """Keep the 1X2 'match winner' triple: three Yes/No markets (home, draw, away), else None."""
    ms = [m for m in ev.get("markets") or [] if m.get("sportsMarketType") in (None, "moneyline")]
    if len(ms) != 3:
        return None
    draw = [m for m in ms if "draw" in str(m.get("groupItemTitle") or m.get("question") or "").lower()]
    if len(draw) != 1:
        return None
    teams = [m for m in ms if m is not draw[0]]
    title = str(ev.get("title") or "")
    sep = " vs. " if " vs. " in title else (" vs " if " vs " in title else None)
    if sep is None:
        return None
    home_name, away_name = (x.strip() for x in title.split(sep, 1))

    def tok(m: dict[str, Any]) -> str | None:
        try:
            ids = json.loads(m.get("clobTokenIds") or "[]")
        except (TypeError, ValueError):
            return None
        return ids[0] if ids else None

    def side(m: dict[str, Any]) -> str:
        t = str(m.get("groupItemTitle") or "")
        return (
            "home"
            if SequenceMatcher(None, t.lower(), home_name.lower()).ratio() >= SequenceMatcher(None, t.lower(), away_name.lower()).ratio()
            else "away"
        )

    sides = {side(m): m for m in teams}
    if set(sides) != {"home", "away"}:
        return None
    gst = iso_ts(ms[0].get("gameStartTime")) or iso_ts(ev.get("startTime"))
    return {
        "id": ev.get("id"),
        "slug": ev.get("slug"),
        "league": league,
        "home": home_name,
        "away": away_name,
        "T": gst,
        "tok": {"home": tok(sides["home"]), "draw": tok(draw[0]), "away": tok(sides["away"])},
        "volume": float(ev.get("volume") or 0),
        "closed": bool(ev.get("closed")),
    }


async def stage_gamma(cache: Path, out: Out) -> None:
    sports = json.loads((cache / "sports.json").read_text())
    rt = build_rt()
    pm = rt.polymarket
    try:
        for code, lg in PM_LEAGUES.items():
            p = cache / f"events_{code}.json"
            if p.exists():
                continue
            s = next((x for x in sports if x.get("sport") == code), None)
            if s is None:
                out(f"{code}: not in /sports")
                continue
            kept: list[dict[str, Any]] = []
            n_all = 0
            reasons: Counter = Counter()
            t0 = time.monotonic()
            complete = False
            async for evs, last in pm.iter_events({"series_id": s["series"], "closed": "true"}):
                for ev in evs:
                    n_all += 1
                    c = compact_event(ev, lg)
                    if c is None or not c["T"] or not all(c["tok"].values()):
                        reasons["not_1x2"] += 1
                        continue
                    kept.append(c)
                complete = last
            p.write_text(json.dumps({"events": kept, "n_all": n_all, "complete": complete, "reasons": dict(reasons)}))
            out(
                f"{code}: series {s['series']} closed events {n_all}, 1X2 kept {len(kept)}, complete {complete}, {time.monotonic() - t0:.0f}s, RSS {rss_mb():.0f} MB"
            )
    finally:
        await rt.aclose()
    for code in PM_LEAGUES:
        p = cache / f"events_{code}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        by_m = Counter(datetime.fromtimestamp(e["T"], UTC).strftime("%Y-%m") for e in d["events"])
        out(f"{code}: kept {len(d['events'])} of {d['n_all']}; by month {dict(sorted(by_m.items()))}")
    out.save()


async def stage_hist(cache: Path, out: Out) -> None:
    rng = random.Random(SEED)
    rt = build_rt()
    pm = rt.polymarket
    (cache / "hist").mkdir(exist_ok=True)
    rows = []
    try:
        for code in PM_LEAGUES:
            p = cache / f"events_{code}.json"
            if not p.exists():
                continue
            evs = json.loads(p.read_text())["events"]
            for ev in rng.sample(evs, min(HIST_SAMPLE_PER_LEAGUE, len(evs))):
                T = ev["T"]
                for side, tok in ev["tok"].items():
                    hp = cache / "hist" / f"{tok[-16:]}_{T}.json"
                    if hp.exists():
                        h = json.loads(hp.read_text())
                    else:
                        try:
                            f = await pm.price_history_range(tok, T - 3 * 3600, T + 600, 1)
                            h = f.data
                        except Exception as e:  # noqa: BLE001
                            out(f"  {ev['slug']} {side}: ERROR {str(e)[:120]}")
                            h = None
                        hp.write_text(json.dumps(h))
                    ts = sorted(int(x["t"]) for x in h or [])
                    pre = [t for t in ts if t <= T]
                    rows.append(
                        {
                            "code": code,
                            "T": T,
                            "side": side,
                            "n": len(ts),
                            "n_pre": len(pre),
                            "age_T": (T - pre[-1]) / 60 if pre else None,
                            "age_T10": (T - 600 - max(t for t in pre if t <= T - 600)) / 60 if any(t <= T - 600 for t in pre) else None,
                        }
                    )
            out(f"{code}: sampled, RSS {rss_mb():.0f} MB")
    finally:
        await rt.aclose()
    (cache / "hist_summary.json").write_text(json.dumps(rows))
    out("")
    out("league  tokens  non-empty  last<=T within 2 min  last<=T-10 within 2 min   median pts in [T-3h,T]   (no prices printed)")
    for code in [*PM_LEAGUES, "ALL"]:
        rr = [r for r in rows if code == "ALL" or r["code"] == code]
        if not rr:
            continue
        ne = sum(1 for r in rr if r["n"] > 0)
        a2 = sum(1 for r in rr if r["age_T"] is not None and r["age_T"] <= 2)
        a10 = sum(1 for r in rr if r["age_T10"] is not None and r["age_T10"] <= 2)
        med = statistics.median([r["n_pre"] for r in rr])
        out(
            f"{code:5s}  {len(rr):5d}  {ne:5d} ({ne / len(rr):.0%})  {a2:5d} ({a2 / len(rr):.0%})  {a10:5d} ({a10 / len(rr):.0%})  {med:6.0f}"
        )
    by_season: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        d = datetime.fromtimestamp(r["T"], UTC)
        k = f"{d.year}-{'H1' if d.month <= 6 else 'H2'}"
        by_season[k][0] += 1
        by_season[k][1] += int(r["n"] > 0 and r["age_T"] is not None and r["age_T"] <= 2)
    out("by half-year (tokens, usable at T within 2 min): " + ", ".join(f"{k} {v[1]}/{v[0]}" for k, v in sorted(by_season.items())))
    out.save()


# ----------------------------------------------------------------------------- gate

DROP = {"fc", "cf", "afc", "ac", "as", "ss", "sc", "ssc", "club", "de", "calcio", "1", "rc", "ogc", "stade", "losc", "vfl",
        "vfb", "tsg", "sv", "fsv", "bv", "bsc", "rcd", "ud", "cd", "sd", "ca", "real", "and", "the", "of", "us", "hellas",
        "1899", "1846", "1907", "1909", "04", "05", "fk", "hotspur", "albion", "wanderers", "united", "city", "town"}  # fmt: skip


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    toks = [t for t in s.split() if t not in DROP]
    return " ".join(toks) or s.strip()


def sim(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if a and b and (a in b or b in a):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def stage_gate(cache: Path, out: Out) -> None:
    # football-data by league, date (with closing odds)
    fd: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for season in FD_SEASONS:
        for lg in FD_LEAGUES:
            for r in fd_rows(cache, lg, season) or []:
                d = fd_date(r.get("Date", ""))
                if d:
                    fd[lg][d].append(r)
    out("PSC* (Pinnacle closing) fill in 2025-26 by month (filled/all):")
    for lg in FD_LEAGUES:
        by_m: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in fd_rows(cache, lg, "2526") or []:
            d = fd_date(r.get("Date", ""))
            if d:
                by_m[d[:7]][1] += 1
                by_m[d[:7]][0] += int(all(fnum(r.get(c)) for c in ("PSCH", "PSCD", "PSCA")))
        out(f"  {lg:4s} " + " ".join(f"{k[2:]}:{v[0]}/{v[1]}" for k, v in sorted(by_m.items())))
    hist = json.loads((cache / "hist_summary.json").read_text()) if (cache / "hist_summary.json").exists() else []
    usable_rate: dict[str, float] = {}
    for code in PM_LEAGUES:
        evs: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in hist:
            if r["code"] == code:
                evs[r["T"]].append(r)
        ok = [all(x["n"] > 0 and x["age_T"] is not None and x["age_T"] <= 2 for x in v) for v in evs.values()]
        usable_rate[code] = sum(ok) / len(ok) if ok else 0.0

    out("Fuzzy match Polymarket 1X2 events -> football-data (same league, date +-1 day; both team similarities >= 0.6)")
    out("bench   league  PM events  matched  with bench  usable-hist rate  -> n matches  obs p<=0.15 (clusters)  obs p>=0.60 (clusters)")
    unmatched: list[str] = []
    sub: dict[tuple[str, str, str], set] = defaultdict(set)
    for bench, cols in (("PSC", ("PSCH", "PSCD", "PSCA")), ("AvgC", ("AvgCH", "AvgCD", "AvgCA"))):
        tot = Counter()
        cl_lo_all: set = set()
        cl_hi_all: set = set()
        for code, lg in PM_LEAGUES.items():
            p = cache / f"events_{code}.json"
            if not p.exists():
                continue
            evs = json.loads(p.read_text())["events"]
            matched = with_b = 0
            lo = hi = 0
            cl_lo: set = set()
            cl_hi: set = set()
            for ev in evs:
                day = datetime.fromtimestamp(ev["T"], UTC).date()
                best, best_r = 0.0, None
                for dd in (-1, 0, 1):
                    for r in fd[lg].get((day + timedelta(days=dd)).isoformat(), []):
                        sh, sa = sim(ev["home"], r["HomeTeam"]), sim(ev["away"], r["AwayTeam"])
                        if min(sh, sa) >= 0.6 and sh + sa > best:
                            best, best_r = sh + sa, r
                if best_r is None:
                    if bench == "PSC" and len(unmatched) < 25:
                        unmatched.append(f"{code} {day} {ev['home']} vs {ev['away']}")
                    continue
                matched += 1
                o = tuple(fnum(best_r.get(c)) for c in cols)
                if not all(o):
                    continue
                with_b += 1
                ps = devig_prop(o)  # type: ignore[arg-type]
                cl = (lg, best_r["Date"])
                per = "pre-2026-02" if day.isoformat() < "2026-02-01" else "post-2026-02"
                for q in ps:
                    if q <= 0.15:
                        lo += 1
                        cl_lo.add(cl)
                        sub[(bench, "p<=0.15", per)].add(cl)
                    elif q >= 0.60:
                        hi += 1
                        cl_hi.add(cl)
                        sub[(bench, "p>=0.60", per)].add(cl)
            u = usable_rate.get(code, 0.0)
            out(
                f"{bench:5s}  {code:5s}  {len(evs):8d}  {matched:7d}  {with_b:9d}  {u:15.0%}  {with_b * u:12.0f}"
                f"  {lo * u:10.0f} ({len(cl_lo) * u:5.0f})  {hi * u:10.0f} ({len(cl_hi) * u:5.0f})"
            )
            tot.update({"ev": len(evs), "m": matched, "b": with_b, "n": with_b * u, "lo": lo * u, "hi": hi * u,
                        "clo": len(cl_lo) * u, "chi": len(cl_hi) * u})  # fmt: skip
            cl_lo_all |= cl_lo
            cl_hi_all |= cl_hi
        out(
            f"{bench:5s}  ALL    {tot['ev']:8d}  {tot['m']:7d}  {tot['b']:9d}  {'':15s}  {tot['n']:12.0f}"
            f"  {tot['lo']:10.0f} ({tot['clo']:5.0f})  {tot['hi']:10.0f} ({tot['chi']:5.0f})"
        )
        for grp, n_obs, n_cl in (("p<=0.15", tot["lo"], tot["clo"]), ("p>=0.60", tot["hi"], tot["chi"])):
            mde = ", ".join(
                f"SD {sd:.0%}: {MDE_K * sd / math.sqrt(n_cl) * 100:.2f} pp" if n_cl > 0 else f"SD {sd:.0%}: inf" for sd in SD_G_PRIOR
            )
            out(
                f"   {bench} {grp}: n_obs {n_obs:.0f}, n_eff (clusters league-day, rho=1 bound) {n_cl:.0f} "
                f"-> gate {'PASS' if n_cl >= N_EFF_GATE else 'FAIL'} (>= {N_EFF_GATE}); MDE {mde}"
            )
    out(
        "clusters (league-day) by subperiod, before the usable-history factor: "
        + ", ".join(f"{k[0]} {k[1]} {k[2]}: {len(v)}" for k, v in sorted(sub.items()))
    )
    out("unmatched examples (first 25): " + " | ".join(unmatched))
    out.save()


def stage_calib(cache: Path, out: Out) -> None:
    """Card step 3 / kill criterion: is the benchmark itself calibrated on outcomes in the H1 bands? No Polymarket data.

    Also the AvgC -> PSC bridge (mean p_AvgC - p_PSC per PSC band) on matches with both."""
    from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci
    from tradedesk.analytics.pm_sharp_clv import BANDS, bridge_delta, p_group, sharp_probs

    benches = {"PSC": ("PSCH", "PSCD", "PSCA"), "AvgC": ("AvgCH", "AvgCD", "AvgCA")}
    periods = {"2021-25": ("2122", "2223", "2324", "2425"), "2025-26": ("2526",), "2026-27": ("2627",)}
    out("Benchmark calibration: mean(outcome - p) per band, cluster bootstrap 95 % CI (cluster = league-date), in pp")
    out("period  bench method      band        n    bias pp   CI95")
    for per, seasons in periods.items():
        rows = [(lg, r) for s in seasons for lg in FD_LEAGUES for r in fd_rows(cache, lg, s) or [] if r.get("FTR") in ("H", "D", "A")]
        for bench, cols in benches.items():
            for method in ("proportional", "power"):
                res: dict[str, tuple[list[float], list[Any]]] = {b: ([], []) for b in BANDS}
                for lg, r in rows:
                    p = sharp_probs([fnum(r.get(c)) for c in cols], method)
                    if p is None:
                        continue
                    for q, side in zip(p, "HDA", strict=True):
                        v, c = res[p_group(q)]
                        v.append((1.0 if r["FTR"] == side else 0.0) - q)
                        c.append((lg, r["Date"]))
                for b in BANDS:
                    v, c = res[b]
                    if len(v) < 30:
                        continue
                    ci = cluster_bootstrap_ci(v, c, n_boot=2000, seed=SEED)
                    out(
                        f"{per}  {bench:5s} {method:12s} {b:10s} {len(v):6d}  {100 * sum(v) / len(v):+7.2f}  "
                        f"[{100 * ci[0]:+.2f}; {100 * ci[1]:+.2f}]"
                        if ci
                        else ""
                    )
        for method in ("proportional", "power"):
            pairs = []
            for _, r in rows:
                a = sharp_probs([fnum(r.get(c)) for c in benches["PSC"]], method)
                b = sharp_probs([fnum(r.get(c)) for c in benches["AvgC"]], method)
                if a and b:
                    pairs.extend(zip(a, b, strict=True))
            if pairs:
                d = bridge_delta(pairs)
                out(
                    f"bridge {per} {method}: mean(p_AvgC - p_PSC) pp by PSC band: "
                    + ", ".join(f"{k} {100 * v['mean']:+.2f} (n {v['n']:.0f})" for k, v in d.items())
                )
    out.save()


def stage_fixtures(cache: Path, out: Out) -> None:
    """Small real samples for tests/fixtures (trimmed; date and source in the test that reads them)."""
    fix = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    cols = ["Div", "Date", "Time", "HomeTeam", "AwayTeam", "FTR", "PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA", "AvgCH", "AvgCD", "AvgCA"]
    rows = fd_rows(cache, "E0", "2526") or []
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows[:6]:
        w.writerow(r)
    (fix / "football_data_2526_E0_head.csv").write_text(buf.getvalue())
    ev = json.loads((cache / "probe_epl_events.json").read_text())[0]
    keep_m = ("id", "question", "groupItemTitle", "gameStartTime", "clobTokenIds", "outcomes", "closed", "conditionId")
    small = {k: ev.get(k) for k in ("id", "slug", "title", "startTime", "closed", "seriesSlug")}
    small["markets"] = [{k: m.get(k) for k in keep_m} for m in ev["markets"]]
    (fix / "polymarket_gamma_epl_event_2526.json").write_text(json.dumps(small, indent=1) + "\n")
    out(f"fixtures written: {len(rows[:6])} football-data rows, event {small['slug']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("fd", "probe", "gamma", "hist", "gate", "calib", "fixtures"))
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/pm-sports-sharp")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    out = Out(a.out)
    if a.stage == "fd":
        asyncio.run(stage_fd(cache, out))
        out.save()
    elif a.stage == "probe":
        asyncio.run(stage_probe(cache, out))
    elif a.stage == "gamma":
        asyncio.run(stage_gamma(cache, out))
    elif a.stage == "hist":
        asyncio.run(stage_hist(cache, out))
    elif a.stage == "fixtures":
        stage_fixtures(cache, out)
    elif a.stage == "calib":
        stage_calib(cache, out)
    else:
        stage_gate(cache, out)


if __name__ == "__main__":
    main()
