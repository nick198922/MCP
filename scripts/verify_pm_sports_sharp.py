"""IDEA-20261112-polymarket-sports-vs-sharp-book, step 2: H1 exactly as pre-registered in the card.

Network only through ``core.http.Http`` (football-data.co.uk) and the runtime provider ``rt.polymarket`` (CLOB).
Cache ``--cache-dir`` (default ``~/.cache/tradedesk-agents/pm-sports-sharp``), shared with ``verify_pm_sports_step0.py``.

Stages (in order):
    uv run python scripts/verify_pm_sports_sharp.py --stage fd      --out <file>  # extra seasons for the calibration
    uv run python scripts/verify_pm_sports_sharp.py --stage aliases --out <file>  # team-name table, no prices
    uv run python scripts/verify_pm_sports_sharp.py --stage match   --out <file>  # matching counts, no prices
    uv run python scripts/verify_pm_sports_sharp.py --stage hist    --out <file>  # /prices-history of matched events
    uv run python scripts/verify_pm_sports_sharp.py --stage calib   --out <file>  # benchmark calibration (no Polymarket)
    uv run python scripts/verify_pm_sports_sharp.py --stage h1      --out <file>  # the registered test + robustness
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci, cluster_bootstrap_p, holm  # noqa: E402
from tradedesk.analytics.pm_sharp_clv import (  # noqa: E402
    BANDS,
    cluster_se,
    drop_top_clusters,
    kickoff_utc,
    p_group,
    quote_at,
    resolve_teams,
    sharp_probs,
    taker_net,
)
from tradedesk.analytics.risk_overlay import cochran_q  # noqa: E402
from tradedesk.core.http import Http  # noqa: E402

FD_URL = "https://www.football-data.co.uk"
FD_LEAGUES = ("E0", "SP1", "D1", "I1", "F1")
# Registration: every season up to 2024-25 with PSC* filled ("expected from 2019-20"); the fd stage lists the fill.
CALIB_SEASONS = ("1112", "1213", "1314", "1415", "1516", "1617", "1718", "1819", "1920", "2021", "2122", "2223", "2324", "2425")
PM_LEAGUES = {"epl": "E0", "lal": "SP1", "bun": "D1", "sea": "I1", "fl1": "F1"}
T_FIRST, T_LAST = "2025-08-15", "2026-11-08"  # registered universe (kick-off date, UTC), inclusive
B_FROM = "2026-02-01"
MAX_KICKOFF_DIFF_S = 600
MAX_AGE_S = 120
SENS_SHIFT_S = 600
SEED = 20261112
N_BOOT = 10_000
THR = {("A", "lo"): 0.015, ("A", "hi"): -0.010, ("B", "lo"): 0.0143, ("B", "hi"): -0.0106}
BENCH_COLS = {"PSC": ("PSCH", "PSCD", "PSCA"), "AvgC": ("AvgCH", "AvgCD", "AvgCA")}

# Polymarket name -> football-data name, per league; built from the `aliases` stage listing (names only, no prices).
# Every other name maps automatically (the full table is in the `aliases` output and the report). "Saint-Etienne" has no
# football-data row in E0..F1 2025-27 (not in Ligue 1 those seasons), so it stays unmatched.
OVERRIDES: dict[str, dict[str, str]] = {
    "E0": {
        "Manchester City": "Man City",
        "Manchester City FC": "Man City",
        "Manchester United": "Man United",
        "Manchester United FC": "Man United",
        "Wolverhampton Wanderers FC": "Wolves",
    },
    "SP1": {
        "Athletic Club": "Ath Bilbao",
        "Atletico Madrid": "Ath Madrid",
        "Club Atlético de Madrid": "Ath Madrid",
        "RC Deportivo A Coruña": "La Coruna",
        "RCD Espanyol de Barcelona": "Espanol",  # auto picked "Barcelona" (substring)
        "Real Racing Club": "Santander",
    },
    "D1": {"Borussia Mönchengladbach": "M'gladbach"},
    "I1": {"FC Internazionale Milano": "Inter"},
    "F1": {
        "Paris FC": "Paris FC",
        "Paris Saint-Germain FC": "Paris SG",  # auto picked "Paris FC"
        "Stade Rennais FC 1901": "Rennes",
    },
}


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


def fd_rows(cache: Path, league: str, season: str) -> list[dict[str, str]]:
    p = cache / "fd" / f"{season}_{league}.csv"
    if not p.exists() or p.stat().st_size == 0:
        return []
    txt = p.read_bytes().decode("utf-8-sig", errors="replace")
    return [r for r in csv.DictReader(io.StringIO(txt)) if (r.get("HomeTeam") or "").strip()]


def fnum(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v > 1.0 else None


def fd_date(s: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def bench_probs(r: dict[str, str], bench: str, method: str) -> tuple[float, float, float] | None:
    return sharp_probs([fnum(r.get(c)) for c in BENCH_COLS[bench]], method)


def load_events(cache: Path) -> list[dict[str, Any]]:
    evs = []
    for code, lg in PM_LEAGUES.items():
        for e in json.loads((cache / f"events_{code}.json").read_text())["events"]:
            d = datetime.fromtimestamp(e["T"], UTC).date().isoformat()
            if T_FIRST <= d <= T_LAST:
                e["code"] = code
                e["lg"] = lg
                evs.append(e)
    return evs


def fd_current(cache: Path) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for lg in FD_LEAGUES:
        out[lg] = fd_rows(cache, lg, "2526") + fd_rows(cache, lg, "2627")
    return out


def team_maps(evs: list[dict[str, Any]], fd: dict[str, list[dict[str, str]]]) -> dict[str, dict[str, str | None]]:
    maps = {}
    for lg in FD_LEAGUES:
        pm_names = {e[s] for e in evs if e["lg"] == lg for s in ("home", "away")}
        fd_names = {r[k].strip() for r in fd[lg] for k in ("HomeTeam", "AwayTeam")}
        maps[lg] = resolve_teams(pm_names, fd_names, OVERRIDES[lg])
    return maps


# ----------------------------------------------------------------------------- stages


async def stage_fd(cache: Path, out: Out) -> None:
    http = Http("football_data", FD_URL, rate=1.0, burst=1, timeout=30, retries=1)
    http._client_args["follow_redirects"] = True  # the host redirects www -> bare domain (step 0)
    (cache / "fd").mkdir(parents=True, exist_ok=True)
    try:
        for season in CALIB_SEASONS:
            for lg in FD_LEAGUES:
                p = cache / "fd" / f"{season}_{lg}.csv"
                if p.exists() and p.stat().st_size > 0:
                    continue
                try:
                    f = await http.get_bytes(f"/mmz4281/{season}/{lg}.csv", tool="fd-csv")
                except Exception as e:  # noqa: BLE001
                    out(f"{season} {lg}: ERROR {type(e).__name__}: {str(e)[:120]}")
                    continue
                p.write_bytes(f.data)
                out(f"{season} {lg}: fetched {len(f.data)} bytes")
    finally:
        await http.aclose()
    for season in CALIB_SEASONS:
        cells = []
        for lg in FD_LEAGUES:
            rows = fd_rows(cache, lg, season)
            k = sum(1 for r in rows if all(fnum(r.get(c)) for c in BENCH_COLS["PSC"]))
            cells.append(f"{lg} {k}/{len(rows)}")
        out(f"{season} PSC* filled: " + ", ".join(cells))
    out.save()


def stage_aliases(cache: Path, out: Out) -> None:
    """Every Polymarket team name with its football-data name (auto or override). Names only."""
    evs = load_events(cache)
    fd = fd_current(cache)
    maps = team_maps(evs, fd)
    for lg in FD_LEAGUES:
        fd_names = sorted({r[k].strip() for r in fd[lg] for k in ("HomeTeam", "AwayTeam")})
        used = {v for v in maps[lg].values() if v}
        out(f"== {lg}: {len(maps[lg])} PM names, {len(fd_names)} fd names")
        for pm, f in sorted(maps[lg].items()):
            tag = "OVR" if pm in OVERRIDES[lg] else "auto"
            out(f"  {pm!r:38s} -> {f!r}  [{tag}]")
        dup = [v for v, n in Counter(v for v in maps[lg].values() if v).items() if n > 1]
        out(f"  fd names used by >1 PM name: {dup}")
        out(f"  fd names unused: {sorted(set(fd_names) - used)}")
    out.save()


def match_events(cache: Path, out: Out) -> list[dict[str, Any]]:
    evs = load_events(cache)
    fd = fd_current(cache)
    maps = team_maps(evs, fd)
    idx: dict[tuple[str, frozenset], list[dict[str, str]]] = defaultdict(list)
    for lg, rows in fd.items():
        for r in rows:
            idx[(lg, frozenset((r["HomeTeam"].strip(), r["AwayTeam"].strip())))].append(r)
    reasons: Counter = Counter()
    unmatched: list[str] = []
    cand: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in evs:
        h, a = maps[e["lg"]].get(e["home"]), maps[e["lg"]].get(e["away"])
        if not h or not a:
            reasons["no_alias"] += 1
            unmatched.append(f"{e['code']} {e['home']} vs {e['away']}")
            continue
        day = datetime.fromtimestamp(e["T"], UTC).date()
        rows = [r for r in idx.get((e["lg"], frozenset((h, a))), []) if (d := fd_date(r["Date"])) and abs((d - day).days) <= 1]
        if not rows:
            reasons["no_fd_row_pm1day"] += 1
            unmatched.append(f"{e['code']} {day} {e['home']} vs {e['away']} (no fd row +-1 day)")
            continue
        r = rows[0]
        ko = kickoff_utc(r["Date"], r.get("Time", "") or "")
        if ko is None or abs(ko - e["T"]) > MAX_KICKOFF_DIFF_S:
            reasons["kickoff_diff_gt_10min"] += 1
            continue
        swapped = r["HomeTeam"].strip() != h
        team_side = {"home": "away" if swapped else "home", "away": "home" if swapped else "away", "draw": "draw"}
        reasons["swapped_orientation"] += int(swapped)
        key = (e["lg"], r["Date"], r["HomeTeam"], r["AwayTeam"])
        cand[key].append({**e, "fd": r, "fd_side": team_side, "kick_diff": ko - e["T"]})
    matched = []
    for _key, es in cand.items():
        es.sort(key=lambda x: -x["volume"])
        matched.append(es[0])
        reasons["duplicate_pm_event_dropped"] += len(es) - 1
    out(f"PM 1X2 events in {T_FIRST}..{T_LAST}: {len(evs)}; matched one-to-one: {len(matched)} ({len(matched) / len(evs):.1%})")
    out("reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    out(f"unmatched ({len(unmatched)}): " + " | ".join(unmatched[:40]))
    return matched


def period_of(e: dict[str, Any]) -> str | None:
    d = datetime.fromtimestamp(e["T"], UTC).date().isoformat()
    if bench_probs(e["fd"], "PSC", "power"):
        return "A"
    if d >= B_FROM and bench_probs(e["fd"], "AvgC", "power"):
        return "B"
    return None


def stage_match(cache: Path, out: Out) -> None:
    m = match_events(cache, out)
    per = Counter(period_of(e) for e in m)
    out(f"by subperiod: A (PSC) {per['A']}, B (AvgC, >= {B_FROM}) {per['B']}, neither {per[None]}")
    out(f"kick-off diff (fd - PM) s: {Counter(e['kick_diff'] for e in m).most_common(6)}")
    out.save()


def build_rt():
    from tradedesk.server import build_server

    _, rt = build_server()
    return rt


async def stage_hist(cache: Path, out: Out, limit: int | None) -> None:
    m = [e for e in match_events(cache, out) if period_of(e)]
    if limit:
        m = m[:limit]
    toks = [(e["T"], t) for e in m for t in e["tok"].values()]
    todo = [(T, t) for T, t in toks if not (cache / "hist" / f"{t[-16:]}_{T}.json").exists()]
    out(f"tokens {len(toks)}, to fetch {len(todo)}")
    rt = build_rt()
    pm = rt.polymarket
    t0 = time.monotonic()
    err = 0
    try:
        for i, (T, tok) in enumerate(todo, 1):
            hp = cache / "hist" / f"{tok[-16:]}_{T}.json"
            try:
                f = await pm.price_history_range(tok, T - 3 * 3600, T + 600, 1)
                hp.write_text(json.dumps(f.data))
            except Exception as e:  # noqa: BLE001
                err += 1
                if err <= 10:
                    out(f"  ERROR {tok[-16:]} {T}: {str(e)[:120]}")
            if i % 200 == 0 or i == len(todo):
                el = time.monotonic() - t0
                out(f"  {i}/{len(todo)} in {el:.0f}s ({i / el:.1f}/s), errors {err}, RSS {rss_mb():.0f} MB")
    finally:
        await rt.aclose()
    out("hist done")
    out.save()


def stage_calib(cache: Path, out: Out) -> None:
    out("Benchmark calibration, PSC (Pinnacle closing), mean(outcome - p) per band, pp; cluster = (league, date)")
    seasons = [s for s in CALIB_SEASONS if any(bench_probs(r, "PSC", "power") for lg in FD_LEAGUES for r in fd_rows(cache, lg, s))]
    out(f"seasons with PSC* (<= 2024-25): {seasons}")
    rows = [(lg, r) for s in seasons for lg in FD_LEAGUES for r in fd_rows(cache, lg, s) if r.get("FTR") in ("H", "D", "A")]
    for method in ("power", "proportional"):
        res: dict[str, tuple[list[float], list[Any]]] = {b: ([], []) for b in BANDS}
        for lg, r in rows:
            p = bench_probs(r, "PSC", method)
            if p is None:
                continue
            for q, side in zip(p, "HDA", strict=True):
                v, c = res[p_group(q)]
                v.append((1.0 if r["FTR"] == side else 0.0) - q)
                c.append((lg, r["Date"]))
        for b in BANDS:
            v, c = res[b]
            ci = cluster_bootstrap_ci(v, c, n_boot=N_BOOT, seed=SEED)
            kill = method == "power" and b in (BANDS[0], BANDS[3]) and abs(sum(v) / len(v)) >= 0.01
            out(
                f"  {method:12s} {b:10s} n {len(v):6d}  bias {100 * sum(v) / len(v):+6.2f}  CI [{100 * ci[0]:+.2f}; {100 * ci[1]:+.2f}]"
                f"{'  KILL(|bias|>=1pp)' if kill else ''}"
            )
    out.save()


# ----------------------------------------------------------------------------- H1


def observations(cache: Path, out: Out) -> list[dict[str, Any]]:
    m = [e for e in match_events(cache, out) if period_of(e)]
    obs: list[dict[str, Any]] = []
    miss = Counter()
    sums: dict[str, list[float]] = defaultdict(list)
    for e in m:
        per = period_of(e)
        r = e["fd"]
        hist = {}
        for side, tok in e["tok"].items():
            hp = cache / "hist" / f"{tok[-16:]}_{e['T']}.json"
            hist[side] = json.loads(hp.read_text()) if hp.exists() else None
        if any(h is None for h in hist.values()):
            miss["hist_not_fetched"] += 1
            continue
        qT = {s: quote_at(h or [], e["T"], MAX_AGE_S) for s, h in hist.items()}
        q10 = {s: quote_at(h or [], e["T"] - SENS_SHIFT_S, MAX_AGE_S) for s, h in hist.items()}
        sT = sum(q[0] for q in qT.values() if q) if all(qT.values()) else None
        s10 = sum(q[0] for q in q10.values() if q) if all(q10.values()) else None
        if sT is not None:
            sums[per].append(sT)
        bp = {(b, mth): bench_probs(r, b, mth) for b in BENCH_COLS for mth in ("power", "proportional")}
        for side in ("home", "draw", "away"):
            j = {"home": 0, "draw": 1, "away": 2}[e["fd_side"][side]]
            if qT[side] is None:
                miss["no_quote_T"] += 1
            team = None if side == "draw" else e[side]
            o = {
                "per": per,
                "lg": e["lg"],
                "cl": (e["lg"], r["Date"]),
                "T": e["T"],
                "side": side,
                "team": team,
                "vol": e["volume"],
                "pT": qT[side][0] if qT[side] else None,
                "p10": q10[side][0] if q10[side] else None,
                "sT": sT,
                "s10": s10,
            }
            for (b, mth), p in bp.items():
                o[f"{b}_{mth}"] = p[j] if p else None
            obs.append(o)
    out(f"matched events with a benchmark: {len(m)}; " + ", ".join(f"{k} {v}" for k, v in sorted(miss.items())))
    for per, v in sorted(sums.items()):
        q = statistics.quantiles(v, n=10)
        out(
            f"sum p_PM at T, subperiod {per}: n {len(v)}, mean {statistics.fmean(v):.4f}, p10 {q[0]:.4f}, p50 {statistics.median(v):.4f}, p90 {q[-1]:.4f}"
        )
    return obs


def g_set(obs, per, bench, method="power", price="pT", band=None, side_filter=None, normed=False):
    vals, cls, ps, rows = [], [], [], []
    for o in obs:
        if o["per"] != per or o[price] is None:
            continue
        ps_ = o[f"{bench}_{method}"]
        if ps_ is None:
            continue
        if band is not None and p_group(ps_) != band:
            continue
        if side_filter == "draw" and o["side"] != "draw":
            continue
        if side_filter == "nodraw" and o["side"] == "draw":
            continue
        q = o[price]
        if normed:
            s = o["sT" if price == "pT" else "s10"]
            if not s:
                continue
            q = q / s
        vals.append(q - ps_)
        cls.append(o["cl"])
        ps.append(o[price])
        rows.append(o)
    return vals, cls, ps, rows


def summ(vals, cls, sign=1):
    if len(vals) < 2 or len(set(cls)) < 2:
        return None
    ci = cluster_bootstrap_ci(vals, cls, n_boot=N_BOOT, seed=SEED)
    p = cluster_bootstrap_p([sign * v for v in vals], cls, n_boot=N_BOOT, seed=SEED)
    return {"n": len(vals), "G": len(set(cls)), "mean": sum(vals) / len(vals), "ci": ci, "p": p, "sd": statistics.pstdev(vals)}


def fmt(s) -> str:
    if s is None:
        return "n/a"
    return (
        f"n {s['n']:4d} G {s['G']:4d}  mean {100 * s['mean']:+6.2f} pp  CI [{100 * s['ci'][0]:+6.2f}; {100 * s['ci'][1]:+6.2f}]"
        f"  p1 {s['p']:.4f}  sd {100 * s['sd']:.2f}"
    )


def stage_h1(cache: Path, out: Out) -> None:
    obs = observations(cache, out)
    (cache / "h1_obs.json").write_text(json.dumps(obs))
    ends = {"lo": (BANDS[0], 1), "hi": (BANDS[3], -1)}
    bench_of = {"A": "PSC", "B": "AvgC"}

    out("\n== PRIMARY (registered): g = p_PM(T, last point < T within 2 min) - p_sharp(power); one-sided cluster bootstrap")
    prim: dict[tuple[str, str], Any] = {}
    for per in ("A", "B"):
        for end, (band, sign) in ends.items():
            v, c, _, _ = g_set(obs, per, bench_of[per], band=band)
            prim[(per, end)] = summ(v, c, sign)
    keys = list(prim)
    adj = holm([prim[k]["p"] if prim[k] else 1.0 for k in keys])
    passed = {}
    for k, pa in zip(keys, adj, strict=True):
        s = prim[k]
        thr = THR[k]
        ok_mag = s is not None and (s["mean"] >= thr if k[1] == "lo" else s["mean"] <= thr)
        passed[k] = bool(ok_mag and pa < 0.05)
        out(f"  {k[0]} {bench_of[k[0]]:4s} {k[1]}  {fmt(s)}  p_holm {pa:.4f}  thr {100 * thr:+.2f} -> {'PASS' if passed[k] else 'fail'}")
    a_lo, a_hi = prim[("A", "lo")], prim[("A", "hi")]
    reject = a_lo and a_hi and abs(a_lo["mean"]) < 0.01 and abs(a_hi["mean"]) < 0.01
    out(f"  rejection rule (A: |mean g| < 1.0 pp in both ends): {'MET' if reject else 'not met'}")

    out("\n== All bands, by side (primary prices/benchmarks), mean g pp")
    for per in ("A", "B"):
        for band in BANDS:
            for sf in (None, "nodraw", "draw"):
                v, c, _, _ = g_set(obs, per, bench_of[per], band=band, side_filter=sf)
                out(f"  {per} {band:10s} {sf or 'all':7s} {fmt(summ(v, c))}")
        v, c, _, _ = g_set(obs, per, bench_of[per])
        out(f"  {per} ALL bands  all     {fmt(summ(v, c))}")

    out("\n== Robustness (all reported; none decides)")
    variants = [
        ("T-10 min", {"price": "p10"}),
        ("proportional de-vig", {"method": "proportional"}),
        ("normalised p_PM / sum", {"normed": True}),
    ]
    for per in ("A", "B"):
        for end, (band, sign) in ends.items():
            for name, kw in variants:
                v, c, _, _ = g_set(obs, per, bench_of[per], band=band, **kw)
                out(f"  {per} {end} {name:24s} {fmt(summ(v, c, sign))}")
            v, c, ps, rows = g_set(obs, per, bench_of[per], band=band)
            dv, dc = drop_top_clusters(v, c, 0.05)
            out(f"  {per} {end} {'drop top 5% clusters':24s} {fmt(summ(dv, dc, sign))}")
            if rows:
                med = statistics.median(o["vol"] for o in rows)
                for lab, cond in (
                    ("volume >= median", lambda o, med=med: o["vol"] >= med),
                    ("volume < median", lambda o, med=med: o["vol"] < med),
                ):
                    vv = [x for x, o in zip(v, rows, strict=True) if cond(o)]
                    cc = [x for x, o in zip(c, rows, strict=True) if cond(o)]
                    out(f"  {per} {end} {lab:24s} {fmt(summ(vv, cc, sign))}")
                tk_abs = [taker_net(x, p) for x, p in zip(v, ps, strict=True)]
                tk_dir = [taker_net(x, p, direction=sign) for x, p in zip(v, ps, strict=True)]
                out(f"  {per} {end} {'taker |g|-fee (registered)':24s} {fmt(summ(tk_abs, c))}")
                out(f"  {per} {end} {'taker dir*g-fee':24s} {fmt(summ(tk_dir, c))}")
        if per == "A":
            for end, (band, sign) in ends.items():
                v, c, _, _ = g_set(obs, "A", "AvgC", band=band)
                out(f"  A {end} {'AvgC bench on A matches':24s} {fmt(summ(v, c, sign))}")

    out("\n== Per league (5a), primary; Cochran Q over league means with cluster-robust SE")
    for per in ("A", "B"):
        for end, (band, sign) in ends.items():
            means, ses = [], []
            for lg in FD_LEAGUES:
                v, c, _, rows = g_set(obs, per, bench_of[per], band=band)
                vv = [x for x, o in zip(v, rows, strict=True) if o["lg"] == lg]
                cc = [x for x, o in zip(c, rows, strict=True) if o["lg"] == lg]
                s = summ(vv, cc, sign)
                out(f"  {per} {end} {lg:4s} {fmt(s)}")
                if s:
                    means.append(s["mean"])
                    ses.append(cluster_se(vv, cc))
            q = cochran_q(means, ses)
            out(f"  {per} {end} Cochran Q {q['q']:.2f} df {q['df']} p {q['p']:.3f} I2 {q['i2']:.2f}")

    out("\n== Team persistence (5a, descriptive): teams ranked by mean g in the first half (by date), top quarter vs rest in the second")
    for end, (band, sign) in ends.items():
        rows = []
        for per in ("A", "B"):
            v, _, _, rr = g_set(obs, per, bench_of[per], band=band, side_filter="nodraw")
            rows += [(o["T"], o["team"], x, o["cl"]) for x, o in zip(v, rr, strict=True)]
        if len(rows) < 20:
            continue
        tmed = statistics.median(r[0] for r in rows)
        first = defaultdict(list)
        for t, team, x, _ in rows:
            if t < tmed:
                first[team].append(x)
        ranked = sorted((sign * statistics.fmean(x), team) for team, x in first.items() if len(x) >= 2)
        ranked.reverse()
        top = {team for _, team in ranked[: max(1, len(ranked) // 4)]}
        second = [(x, cl, team in top) for t, team, x, cl in rows if t >= tmed and team in first]
        for lab, flag in (("top quarter", True), ("rest", False)):
            vv = [x for x, _, f in second if f == flag]
            cc = [c for _, c, f in second if f == flag]
            out(f"  {end} {lab:12s} (ranked teams {len(ranked)}) second half: {fmt(summ(vv, cc))}")

    out("\n== Verdict inputs")
    out(f"  PASS map: {', '.join(f'{k[0]}-{k[1]} {v}' for k, v in passed.items())}")
    out(f"peak RSS {rss_mb():.0f} MB")
    out.save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["fd", "aliases", "match", "hist", "calib", "h1"])
    ap.add_argument("--cache-dir", default="~/.cache/tradedesk-agents/pm-sports-sharp")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=None, help="hist: only the first N matched events (smoke)")
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    out = Out(a.out)
    if a.stage == "fd":
        asyncio.run(stage_fd(cache, out))
    elif a.stage == "aliases":
        stage_aliases(cache, out)
    elif a.stage == "match":
        stage_match(cache, out)
    elif a.stage == "hist":
        asyncio.run(stage_hist(cache, out, a.limit))
    elif a.stage == "calib":
        stage_calib(cache, out)
    else:
        stage_h1(cache, out)


if __name__ == "__main__":
    main()
