"""Market makers, part A (IDEA-20261017-market-makers): does a sharp, persistent drop of order-book depth near the
price ("liquidity exit") without a price move predict worse returns / higher volatility over 1-7 days?

Rules and thresholds are fixed in the card's section "Реєстрація (частина A)". Data: daily ``bookDepth`` zips from
data.binance.vision via ``rt.binance_vision`` (USD-M perps, 30-s snapshots, from 2023-01), reduced on arrival to daily
features (one JSON per symbol-month under --cache-dir/depth; raw zips are never written to disk); daily klines and the
point-in-time universe inputs come from the funding-crowding cache (--data-dir), filled by the same provider.
No keys, no orders.

Stages (each resumable; a rerun skips cached files):
  uv run python scripts/verify_market_makers_depth.py --stage prereg
  uv run python scripts/verify_market_makers_depth.py --stage universe
  uv run python scripts/verify_market_makers_depth.py --stage fetch --limit 200      (timing sample)
  uv run python scripts/verify_market_makers_depth.py --stage fetch --shard 0/4 --out .../fetch0.log  (x4 processes;
      optionally --shard 0/2 --reverse and 1/2 --reverse walk the plan from the oldest month; cached months are skipped)
  uv run python scripts/verify_market_makers_depth.py --stage count                 (events only, no outcomes)
  uv run python scripts/verify_market_makers_depth.py --stage report --out .../report.md
  uv run python scripts/verify_market_makers_depth.py --stage report --event-months 2023-02:2023-12   (supplementary)
The registered analysis uses the newest contiguous block of fully fetched months (events from its second month).
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import hashlib
import json
import logging
import resource
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.liquidation_flush import (
    base_of,
    cluster_bootstrap_ci,
    cluster_bootstrap_p,
    holm,
    monthly_universe,
    without_top_days,
)
from tradedesk.analytics.listing_drift import symbol_eligible
from tradedesk.analytics.market_makers_depth import (
    SNAPSHOTS_PER_DAY,
    EventParams,
    depth_day_features,
    detect_events,
    forward_outcomes,
    match_features,
    nearest,
    normalized_depth,
)
from tradedesk.analytics.per_asset import asset_stats, heterogeneity, mde, permutation_q, persistence
from tradedesk.core.errors import ProviderError

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-market-makers.md"
DAY_MS = 86_400_000
TOP_N = 200
FIRST_UNI_MONTH, LAST_MONTH = "2023-01", "2026-09"
PRE_MONTHS = 1  # depth months fetched before a symbol's first member month (baseline + volume windows)
LAST_EVENT_DAY = "2026-09-22"  # 7 d of outcomes inside the cached klines (to 2026-09-30)
MIN_SNAP = SNAPSHOTS_PER_DAY // 2  # a depth day counts only with >= 12 h of snapshots
HORIZONS = (1, 3, 7)
K_BAL, K_RND = 3, 3
EXCL = 7  # placebo days are >= 7 d away from any event of the same symbol
COST_LEG = 0.002  # round trip per leg: taker 0.05 % x 2 + slippage 0.05 % x 2
MIN_EVENTS = 100
FALLBACK_RATIO = 0.6
OOS_FRAC = 0.25
SEED = 20261018
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


def day_of(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def months(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:])
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def shift_month(mo: str, k: int) -> str:
    y, m = int(mo[:4]), int(mo[5:]) - 1 + k
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def days_of(mo: str) -> list[str]:
    y, m = int(mo[:4]), int(mo[5:])
    return [f"{mo}-{d:02d}" for d in range(1, calendar.monthrange(y, m)[1] + 1)]


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Реєстрація (частина A)")
    b = t.index("## Критерій «вбити»")
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- universe


def eligible_syms(data_dir: Path) -> list[str]:
    syms = rd(data_dir / "symbols.json")
    info = rd(data_dir / "exchange_info.json")["info"]
    return [s for s in syms if symbol_eligible(s, (info.get(s) or {}).get("contractType"), (info.get(s) or {}).get("underlyingType"))]


def listing(data_dir: Path, s: str) -> list[str]:
    p = data_dir / "listing" / f"{s}.json"
    return (rd(p) if p.exists() else None) or []


def load_daily(data_dir: Path, s: str) -> dict[str, tuple[float, float]]:
    """{YYYY-MM-DD: (close, quote_volume)} from the cached monthly 1d klines (2021-10 .. 2026-09)."""
    out: dict[str, tuple[float, float]] = {}
    d = data_dir / "k1d" / s
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        for r in rd(p) or []:
            out[day_of(int(r[0]))] = (float(r[4]), float(r[6] if len(r) > 6 else r[5]))
    return out


def stage_universe(cache: Path, data_dir: Path) -> None:
    syms = eligible_syms(data_dir)
    emit(f"eligible USD-M COIN perps (incl. delisted): {len(syms)}")
    qv: dict[str, list[tuple[int, float]]] = {}
    for s in syms:
        rows = [(ms(f"{d}T00:00"), v[1]) for d, v in load_daily(data_dir, s).items() if d >= "2022-11-01"]
        if rows:
            qv[s] = rows
    uni: dict[str, list[str]] = {}
    for mo in months(FIRST_UNI_MONTH, LAST_MONTH):
        ranked = monthly_universe(qv, ms(f"{mo}-01T00:00"), top_n=TOP_N + 60)
        seen: set[str] = set()
        mem: list[str] = []
        for s in ranked:
            b = base_of(s)
            if b in seen:
                continue
            seen.add(b)
            mem.append(s)
            if len(mem) == TOP_N:
                break
        uni[mo] = mem
    wr(cache / "universe.json", uni)
    info = rd(data_dir / "exchange_info.json")["info"]
    allm = sorted({s for v in uni.values() for s in v})
    st = Counter((info.get(s) or {}).get("status", "absent") for v in uni.values() for s in v)
    jobs = fetch_plan(uni)
    emit(f"universe months {len(uni)}; sizes {min(map(len, uni.values()))}..{max(map(len, uni.values()))}; distinct members {len(allm)}")
    emit(f"member-months by today's status {dict(st)}")
    emit(f"depth symbol-months to fetch {len(jobs)}, symbol-days ~{sum(len(days_of(m)) for _, m in jobs)} (~0.45 MB per zip)")
    for mo in ("2023-01", "2024-06", "2026-09"):
        emit(f"  {mo}: {uni[mo][:12]} ... {uni[mo][-5:]}")


def fetch_plan(uni: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(symbol, month) pairs: member months plus PRE_MONTHS before each membership spell; newest months first."""
    need: set[tuple[str, str]] = set()
    for mo, mem in uni.items():
        for s in mem:
            for k in range(PRE_MONTHS + 1):
                m2 = shift_month(mo, -k)
                if m2 >= "2023-01":
                    need.add((s, m2))
    return sorted(need, key=lambda x: (x[1], x[0]), reverse=True)


# ---------------------------------------------------------------- depth fetch


async def fetch_depth(
    cache: Path, data_dir: Path, limit: int | None, conc: int, shard: tuple[int, int] = (0, 1), reverse: bool = False
) -> None:
    from tradedesk.server import build_server

    uni = rd(cache / "universe.json")
    plan = [(s, m) for i, (s, m) in enumerate(fetch_plan(uni)) if i % shard[1] == shard[0]]
    plan = [(s, m) for s, m in plan if not (cache / "depth" / s / f"{m}.json").exists()]
    lst = {s: set(listing(data_dir, s)) for s in {s for s, _ in plan}}
    plan = [(s, m) for s, m in plan if not lst[s] or m in lst[s]]
    emit(f"symbol-months missing {len(plan)}; concurrency {conc}")
    _, rt = build_server()
    sem = asyncio.Semaphore(conc)
    errors: Counter[str] = Counter()
    stats = {"files": 0, "ok": 0, "bytes_rows": 0}
    t0 = time.time()
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    async def day(s: str, d: str) -> tuple[str, Any]:
        async with sem:
            try:
                f = await rt.binance_vision.um_book_depth_daily(s, d)
            except ProviderError as e:
                if e.reason == "data_unavailable":
                    return d, None
                errors[e.reason] += 1
                return d, "ERR"
            stats["files"] += 1
            stats["bytes_rows"] += int(f.data["ts"].size)
            feat = depth_day_features(f.data["ts"], f.data["pct"], f.data["notional"])
            stats["ok"] += 1
            return d, {k: (round(v, 2) if np.isfinite(v) else None) for k, v in feat.items()}

    done = 0
    try:
        for s, m in (plan[::-1] if reverse else plan)[: limit if limit else None]:
            if (cache / "depth" / s / f"{m}.json").exists():
                done += 1  # written meanwhile by a process walking the plan from the other end
                continue
            days = [d for d in days_of(m) if d < today]
            res = await asyncio.gather(*[day(s, d) for d in days])
            if any(v == "ERR" for _, v in res):
                continue  # transient: a rerun retries the month
            wr(cache / "depth" / s / f"{m}.json", dict(res))
            done += 1
            if done % 20 == 0 or done == len(plan):
                el = time.time() - t0
                emit(
                    f"  {done}/{len(plan)} symbol-months, {stats['files']} files in {el:.0f}s "
                    f"({stats['files'] / max(el, 1e-9):.1f}/s), eta {el / done * (len(plan) - done) / 3600:.1f} h, "
                    f"errors {dict(errors)}, rss {rss_mb():.0f} MB"
                )
    finally:
        await rt.aclose()
    emit(f"done {done} symbol-months, {stats['files']} files in {time.time() - t0:.0f}s; errors {dict(errors)}; rss {rss_mb():.0f} MB")


# ---------------------------------------------------------------- panel


OUT_KEYS = ("r1", "r3", "r7", "rv1", "rv3", "rv7", "vq7")


class Panel:
    """Daily (symbol x day) arrays: close, quote volume, depth features (NaN unless >= MIN_SNAP snapshots), membership,
    and the per-day match features / forward outcomes (computed once, only where a symbol is a member with depth)."""

    def __init__(self, cache: Path, data_dir: Path, event_months: str | None = None) -> None:
        t0 = time.time()
        self.uni: dict[str, list[str]] = rd(cache / "universe.json")
        self.syms = sorted({s for v in self.uni.values() for s in v})
        d0 = datetime(2022, 11, 1, tzinfo=UTC)
        n = (datetime(2026, 10, 1, tzinfo=UTC) - d0).days
        self.days = [day_of(int(d0.timestamp() * 1000) + i * DAY_MS) for i in range(n)]
        self.di = {d: i for i, d in enumerate(self.days)}
        S, N = len(self.syms), n
        self.close = np.full((S, N), np.nan)
        self.qv = np.full((S, N), np.nan)
        self.dep = {k: np.full((S, N), np.nan) for k in ("d1", "d2", "d1_med")}
        self.nsnap = np.full((S, N), np.nan)
        self.member = np.zeros((S, N), dtype=bool)
        plan = fetch_plan(self.uni)
        lst = {s: set(listing(data_dir, s)) for s in self.syms}
        need = Counter(m for s, m in plan if not lst[s] or m in lst[s])
        have: Counter[str] = Counter()
        for si, s in enumerate(self.syms):
            for d, (c, q) in load_daily(data_dir, s).items():
                if d in self.di:
                    self.close[si, self.di[d]] = c
                    self.qv[si, self.di[d]] = q
            ddir = cache / "depth" / s
            for p in sorted(ddir.glob("*.json")) if ddir.exists() else []:
                have[p.stem] += 1
                for d, f in rd(p).items():
                    if d not in self.di:
                        continue
                    j = self.di[d]
                    if f is None:
                        continue
                    self.nsnap[si, j] = f["n_snap"]
                    if f["n_snap"] >= MIN_SNAP:
                        for k in self.dep:
                            self.dep[k][si, j] = f[k] if f[k] is not None else np.nan
        for mo, mem in self.uni.items():
            js = [self.di[d] for d in days_of(mo) if d in self.di]
            for s in mem:
                self.member[self.syms.index(s), js[0] : js[-1] + 1] = True
        self.need, self.have = need, have
        complete = [m for m in months(FIRST_UNI_MONTH, LAST_MONTH) if have[m] >= need[m]]
        run: list[str] = []
        for m in reversed(months(FIRST_UNI_MONTH, LAST_MONTH)):
            if m not in complete:
                break
            run.append(m)
        self.full_months = sorted(run)
        self.event_months = self.full_months[1:] if self.full_months and self.full_months[0] != FIRST_UNI_MONTH else self.full_months
        if event_months:  # supplementary run on another fully fetched block (not the registered slice)
            lo, hi = event_months.split(":")
            bad = [m for m in months(lo, hi) if m not in complete]
            if bad:
                raise SystemExit(f"months not fully fetched: {bad}")
            self.event_months = months(lo, hi)
        ev_days = {d for m in self.event_months for d in days_of(m) if d <= LAST_EVENT_DAY}
        self.ev_day_mask = np.array([d in ev_days for d in self.days])
        # match features and outcomes where a member has a valid depth day
        self.feat = np.full((S, N, 3), np.nan)
        self.out = np.full((S, N, len(OUT_KEYS)), np.nan)
        valid = self.member & np.isfinite(self.dep["d1"])
        for si in range(S):
            for j in np.flatnonzero(valid[si]):
                f = match_features(self.close[si], self.qv[si], int(j))
                if f is not None:
                    self.feat[si, j] = f
                o = forward_outcomes(self.close[si], self.qv[si], int(j), HORIZONS)
                self.out[si, j] = [o["r1"], o["r3"], o["r7"], o["rv1"], o["rv3"], o["rv7"], o["vq7"] if "vq7" in o else np.nan]
        self.pool_ok = valid & np.all(np.isfinite(self.feat), axis=2) & np.all(np.isfinite(self.out[:, :, :6]), axis=2)
        emit(f"panel {S} symbols x {N} days loaded in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")

    def norm(self, metric: str, normalize: bool) -> np.ndarray:
        d = self.dep[metric]
        if not normalize:
            return d.copy()
        return np.vstack([normalized_depth(d[i], self.qv[i]) for i in range(len(self.syms))])


def find_events(P: Panel, D: np.ndarray, p: EventParams) -> list[tuple[int, int]]:
    out = []
    for si in range(len(P.syms)):
        for t in detect_events(D[si], P.close[si], p):
            if P.member[si, t] and P.ev_day_mask[t]:
                out.append((si, t))
    return out


def build_rows(P: Panel, events: list[tuple[int, int]], seed: int = SEED) -> dict[str, np.ndarray]:
    """Per event: outcomes, balanced-control means (same date) and random same-symbol placebo means."""
    S, N = P.close.shape
    excl = np.zeros((S, N), dtype=bool)
    for si, t in events:
        excl[si, max(0, t - EXCL) : t + EXCL + 1] = True
    pool = P.pool_ok & ~excl
    rng = np.random.default_rng(seed)
    rows: dict[str, list[Any]] = defaultdict(list)
    for si, t in events:
        o = P.out[si, t]
        f = P.feat[si, t]
        if not (np.all(np.isfinite(o[:6])) and np.all(np.isfinite(f))):
            rows["_dropped"].append((si, t))
            continue
        cand = np.flatnonzero(pool[:, t])
        cand = cand[cand != si]
        if cand.size < K_BAL:
            rows["_nocontrol"].append((si, t))
            continue
        pick = cand[nearest(f, P.feat[cand, t], K_BAL)]
        ctl = P.out[pick, t].mean(axis=0)
        own = np.flatnonzero(pool[si])
        rnd = (
            P.out[si, rng.choice(own, size=min(K_RND, own.size), replace=False)].mean(axis=0)
            if own.size
            else np.full(len(OUT_KEYS), np.nan)
        )
        rows["sym"].append(si)
        rows["t"].append(t)
        for k, key in enumerate(OUT_KEYS):
            rows[key].append(o[k])
            rows["c_" + key].append(ctl[k])
            rows["p_" + key].append(rnd[k])
        rows["dist"].append(float(np.sqrt((((P.feat[pick, t] - f) / np.array([1.0, 0.1, 0.5])) ** 2).sum(axis=1)).mean()))
    out = {k: np.asarray(v) for k, v in rows.items() if not k.startswith("_")}
    out["n_dropped"] = np.array([len(rows["_dropped"]), len(rows["_nocontrol"])])
    return out


def mean_ci(v: np.ndarray, cl: list[str], n_boot: int = 10_000) -> tuple[float, tuple[float, float] | None, float | None]:
    """Mean, date-cluster 95 % CI, one-sided p for mean < 0."""
    if v.size == 0:
        return float("nan"), None, None
    ci = cluster_bootstrap_ci(v.tolist(), cl, n_boot=n_boot)
    p = cluster_bootstrap_p((-v).tolist(), cl, n_boot=n_boot)
    return float(v.mean()), ci, p


def pp(x: float) -> str:
    return f"{x * 100:+.2f}" if np.isfinite(x) else "nan"


def ci_s(ci: tuple[float, float] | None) -> str:
    return f"[{ci[0] * 100:+.2f}; {ci[1] * 100:+.2f}]" if ci else "[n/a]"


# ---------------------------------------------------------------- count (no outcomes of events)


MAIN = EventParams()


def coverage(P: Panel) -> None:
    emit(
        f"months fully fetched (contiguous to {LAST_MONTH}): {P.full_months[0] if P.full_months else '-'} .. {P.full_months[-1] if P.full_months else '-'} ({len(P.full_months)})"
    )
    emit(f"event months: {P.event_months[0] if P.event_months else '-'} .. {P.event_months[-1] if P.event_months else '-'}")
    for y in ("2023", "2024", "2025", "2026"):
        ms_ = [m for m in months(FIRST_UNI_MONTH, LAST_MONTH) if m.startswith(y)]
        emit(f"  {y}: symbol-months cached {sum(P.have[m] for m in ms_)} / planned {sum(P.need[m] for m in ms_)}")
    mem = P.member
    for y in ("2023", "2024", "2025", "2026"):
        js = np.array([d.startswith(y) for d in P.days])
        m = mem[:, js]
        fetched = np.isfinite(P.nsnap[:, js]) & m
        ok = np.isfinite(P.dep["d1"][:, js]) & m
        emit(f"  {y}: member-days {int(m.sum())}, with a depth file {int(fetched.sum())}, valid (>= {MIN_SNAP} snapshots) {int(ok.sum())}")


def stage_count(cache: Path, data_dir: Path) -> None:
    P = Panel(cache, data_dir)
    coverage(P)
    D = P.norm("d1", True)
    for ratio in (0.5, FALLBACK_RATIO):
        ev = find_events(P, D, EventParams(ratio=ratio))
        yrs = Counter(P.days[t][:4] for _, t in ev)
        emit(
            f"exit events ratio {ratio}: n={len(ev)}, dates={len({t for _, t in ev})}, symbols={len({s for s, _ in ev})}, by year {dict(sorted(yrs.items()))}"
        )
    ev = find_events(P, D, MAIN)
    # MDE from placebo days only: random member days vs their balanced controls (no event outcomes used)
    rng = np.random.default_rng(SEED + 1)
    ok = np.argwhere(P.pool_ok & P.ev_day_mask[None, :])
    if len(ok) == 0:
        emit("no valid event days yet")
        return
    take = ok[rng.choice(len(ok), size=min(3000, len(ok)), replace=False)]
    pseudo = [(int(s), int(t)) for s, t in take]
    R = build_rows(P, pseudo, seed=SEED + 2)
    d3 = R["r3"] - R["c_r3"]
    sd = float(np.std(d3, ddof=1))
    n = len(ev)
    emit(f"placebo-day sd of dr3 (vs balanced controls): {sd * 100:.2f} pp from {d3.size} random days")
    if n:
        emit(f"MDE (one-sided 5 %, power 80 %) at n={n}: {mde(sd / np.sqrt(n)) * 100:.2f} pp (iid; date clustering makes it larger)")
        # A date-clustered design effect estimated on these random days came out 1.00 (ICC ~ 0, 2026-10-18);
        # the realised clustered SE on events was 1.5x the iid one (event rows are more dispersed than
        # random days). Treat this MDE as a lower bound; the binding MDE is 2.49 x the clustered SE in `report`.
    emit(f"gate: n >= {MIN_EVENTS}: {'pass' if n >= MIN_EVENTS else 'FAIL -> fallback ratio ' + str(FALLBACK_RATIO)}")
    emit(f"rss {rss_mb():.0f} MB")


# ---------------------------------------------------------------- report


def family(R: dict[str, np.ndarray], P: Panel, keys: tuple[str, ...], kind: str, ctl: str = "c_") -> list[tuple[str, float, Any, Any]]:
    cl = [P.days[t] for t in R["t"]]
    res = []
    for k in keys:
        if kind == "r":
            v = R[k] - R[ctl + k]
        else:
            v = np.log(R[k] / R[ctl + k])
        fin = np.isfinite(v)
        res.append((k, *mean_ci(v[fin], [c for c, f in zip(cl, fin, strict=True) if f])))
    return res


def print_family(name: str, res: list[tuple[str, float, Any, Any]], greater: bool = False) -> list[float]:
    ps = [(1 - p if greater else p) if p is not None else 1.0 for _, _, _, p in res]
    adj = holm(ps)
    for (k, m, ci, _), p, a in zip(res, ps, adj, strict=True):
        emit(f"  {name} {k}: mean {pp(m)} pp, CI95 {ci_s(ci)}, p({'>' if greater else '<'}0) {p:.4f}, Holm {a:.4f}")
    return adj


def config_line(P: Panel, label: str, D: np.ndarray, p: EventParams) -> dict[str, np.ndarray]:
    ev = find_events(P, D, p)
    R = build_rows(P, ev)
    if R.get("t") is None or R["t"].size == 0:
        emit(f"| {label} | 0 | | | | |")
        return R
    cl = [P.days[t] for t in R["t"]]
    d3 = R["r3"] - R["c_r3"]
    m, ci, pv = mean_ci(d3, cl, n_boot=4000)
    v3 = np.log(R["rv3"] / R["c_rv3"])
    mv, civ, _ = mean_ci(v3, cl, n_boot=4000)
    emit(
        f"| {label} | {d3.size} | {len(set(cl))} | {pp(m)} {ci_s(ci)} | {pv:.3f} | {mv:+.3f} [{civ[0]:+.3f}; {civ[1]:+.3f}] |"
        if civ
        else f"| {label} | {d3.size} | {len(set(cl))} | {pp(m)} | | |"
    )
    return R


def stage_report(cache: Path, data_dir: Path, event_months: str | None = None) -> None:
    emit(f"# market makers part A: book-depth liquidity exit, run {datetime.now(UTC).isoformat(timespec='seconds')}")
    if event_months:
        emit(f"SUPPLEMENTARY (not the registered slice): events restricted to months {event_months}")
    P = Panel(cache, data_dir, event_months)
    coverage(P)
    sanity(P)
    D = P.norm("d1", True)
    ev = find_events(P, D, MAIN)
    ratio_used = MAIN.ratio
    if len(ev) < MIN_EVENTS:
        emit(f"gate: n={len(ev)} < {MIN_EVENTS} -> registered fallback ratio {FALLBACK_RATIO}")
        ratio_used = FALLBACK_RATIO
        ev = find_events(P, D, EventParams(ratio=FALLBACK_RATIO))
    main_p = EventParams(ratio=ratio_used)
    R = build_rows(P, ev)
    n = R["t"].size
    cl = [P.days[t] for t in R["t"]]
    emit("")
    emit(f"## H1 main: ratio {ratio_used}, run 2, z_max 1, cooldown 14, metric d1 / 30d volume")
    emit(
        f"events {len(ev)}; analysed {n} on {len(set(cl))} dates, {len(set(R['sym'].tolist()))} symbols; dropped (no outcome / no controls) {R['n_dropped'].tolist()}"
    )
    emit(
        f"mean distance to balanced controls (scaled): {R['dist'].mean():.3f}; event raw r3 mean {pp(R['r3'].mean())} pp, controls {pp(R['c_r3'].mean())} pp"
    )
    emit("vs balanced placebo (same date, matched on 2d z, 7d return, volume ratio):")
    adj_r = print_family("dr", family(R, P, ("r1", "r3", "r7"), "r"))
    print_family("dlogRV", family(R, P, ("rv1", "rv3", "rv7"), "v"), greater=True)
    emit("vs random placebo (same symbol, random days):")
    print_family("dr", family(R, P, ("r1", "r3", "r7"), "r", ctl="p_"))
    print_family("dlogRV", family(R, P, ("rv1", "rv3", "rv7"), "v", ctl="p_"), greater=True)
    d3 = R["r3"] - R["c_r3"]
    m3, ci3, _ = mean_ci(d3, cl)
    order = np.argsort(R["t"], kind="stable")
    cut = R["t"][order[int(np.floor(n * (1 - OOS_FRAC)))]] if n else 0
    oos = R["t"] >= cut
    mo, cio, _ = mean_ci(d3[oos], [c for c, o in zip(cl, oos, strict=True) if o])
    mi, cii, _ = mean_ci(d3[~oos], [c for c, o in zip(cl, oos, strict=True) if not o])
    emit(f"IS (< {P.days[cut]}): n={int((~oos).sum())} dr3 {pp(mi)} {ci_s(cii)}; OOS: n={int(oos.sum())} dr3 {pp(mo)} {ci_s(cio)}")
    emit("economics on notional of the short leg (3 d hold):")
    for mult in (1, 2):
        emit(
            f"  costs x{mult}: hedged pair (short event, long controls) {pp(-m3 - 2 * COST_LEG * mult)} pp; "
            f"naked short {pp(-float(R['r3'].mean()) - COST_LEG * mult)} pp (funding not included)"
        )
    ok = bool(ci3 and ci3[1] < 0 and adj_r[1] < 0.05 and mo < 0 and -m3 - 2 * COST_LEG > 0)
    kill = bool(ci3 and ci3[0] <= 0 <= ci3[1] and m3 > -0.01)
    emit(f"registered decision: confirm={ok}; kill={kill}")

    emit("")
    emit("## robustness: without top dates, by year")
    for k in (1, 5, 10):
        v, dd, drop = without_top_days((-d3).tolist(), cl, k)
        mm, cc, _ = mean_ci(-np.asarray(v), dd, n_boot=4000)
        emit(f"  without the {k} dates contributing most to a negative dr3: dr3 {pp(mm)} {ci_s(cc)} (dropped {drop[:5]})")
    for y in ("2023", "2024", "2025", "2026"):
        sel = np.array([c.startswith(y) for c in cl])
        if sel.sum():
            mm, cc, _ = mean_ci(d3[sel], [c for c, s in zip(cl, sel, strict=True) if s], n_boot=4000)
            emit(f"  {y}: n={int(sel.sum())} dr3 {pp(mm)} {ci_s(cc)}")
    stat = rd(data_dir / "exchange_info.json")["info"]
    alive = np.array([(stat.get(P.syms[s]) or {}).get("status") == "TRADING" for s in R["sym"]])
    for lab, sel in (("trading today", alive), ("delisted/settling today", ~alive)):
        if sel.sum():
            mm, cc, _ = mean_ci(d3[sel], [c for c, s in zip(cl, sel, strict=True) if s], n_boot=4000)
            emit(f"  {lab}: n={int(sel.sum())} dr3 {pp(mm)} {ci_s(cc)}")

    emit("")
    emit("## sensitivity grid (all variants; dr3 vs balanced placebo, CI95 date-cluster, one-sided p<0; dlogRV3)")
    emit("| variant | n | dates | dr3 pp [CI] | p | dlogRV3 [CI] |")
    emit("|---|---|---|---|---|---|")
    for ratio in (0.4, 0.5, 0.6):
        for run_ in (2, 3):
            for z in (0.5, 1.0, float("inf")):
                config_line(P, f"d1/vol ratio {ratio} run {run_} z {z}", D, EventParams(ratio=ratio, run=run_, z_max=z))
    for metric, norm_, lab in (("d2", True, "d2/vol"), ("d1_med", True, "d1 median/vol"), ("d1", False, "d1 raw USD")):
        config_line(P, f"{lab} ratio {ratio_used} run 2 z 1", P.norm(metric, norm_), main_p)

    emit("")
    emit("## per asset (5a), main config, dr3")
    assets = [base_of(P.syms[s]) for s in R["sym"]]
    st = sorted(asset_stats(d3.tolist(), assets, min_n=3), key=lambda s: s.mean)
    emit(f"assets with >= 3 events: {len(st)} (of {len(set(assets))}); events per asset max {max(Counter(assets).values())}")
    emit("| asset | n | dr3 pp | CI95 (normal) |")
    emit("|---|---|---|---|")
    for s in st:
        emit(f"| {s.asset} | {s.n} | {pp(s.mean)} | {ci_s(s.ci)} |")
    h = heterogeneity(d3.tolist(), assets, min_n=3)
    pq = permutation_q(d3.tolist(), assets, cl, min_n=3, n_perm=2000)
    emit(f"Cochran Q (pooled var) {h['q']:.2f} df {h['df']} p {h['p']} I2 {h['i2']:.2f}; tau {h['tau']:.4f}")
    emit(f"permutation within dates: Q {pq['q']:.2f}, null mean {pq['null_mean']:.2f}, p95 {pq['null_p95']:.2f}, p {pq['p']}")
    is_mask = ~oos
    ps_ = persistence((-d3).tolist(), assets, cl, is_mask.tolist(), min_n_is=2, top_frac=0.25, n_boot=4000)
    emit(
        f"persistence (value = -dr3, short edge): ranked {ps_['k']} assets, top {len(ps_['top'])}: IS top {pp(ps_['top_is_mean'])} vs rest {pp(ps_['rest_is_mean'])}; "
        f"OOS top {pp(ps_['top_oos_mean'])} (n={ps_['n_top_oos']}) vs rest {pp(ps_['rest_oos_mean'])} (n={ps_['n_rest_oos']}); "
        f"diff {pp(ps_['diff'])} {ci_s(ps_['diff_ci'])}; Spearman IS/OOS {ps_['rho']:.2f} (n={ps_['rho_n']})"
    )

    emit("")
    emit("## H2 (secondary): liquidity entry, D > 2 x median two days, |move| < 1 sigma")
    ev2 = find_events(P, D, EventParams(ratio=2.0, direction=1))
    R2 = build_rows(P, ev2)
    if R2.get("t") is not None and R2["t"].size:
        cl2 = [P.days[t] for t in R2["t"]]
        res = []
        for k in ("r7", "vq7"):
            v = R2[k] - R2["c_" + k]
            fin = np.isfinite(v)
            m, ci, p = mean_ci(v[fin], [c for c, f in zip(cl2, fin, strict=True) if f])
            res.append((k, m, ci, p))
        emit(f"events {len(ev2)}; analysed {R2['t'].size}")
        print_family("d", res, greater=True)
    delist_descriptive(P, D, data_dir)


def sanity(P: Panel) -> None:
    emit("")
    emit("## sanity")
    D = P.norm("d1", True)
    for s in ("BTCUSDT", "ETHUSDT", "BLURUSDT"):
        if s not in P.syms:
            continue
        i = P.syms.index(s)
        for y in ("2023", "2024", "2025", "2026"):
            js = np.array([d.startswith(y) for d in P.days])
            d = P.dep["d1"][i, js]
            if np.isfinite(d).any():
                emit(
                    f"  {s} {y}: median d1 (bid+ask within 1 %) ${np.nanmedian(d) / 1e6:.3f} M, d1/30d-volume {np.nanmedian(D[i, js]):.4f}"
                )
    dl = np.diff(np.log(D), axis=1)
    dr = np.diff(np.log(P.close), axis=1)
    raw = np.diff(np.log(P.dep["d1"]), axis=1)
    ok = np.isfinite(dl) & np.isfinite(dr) & P.member[:, 1:]
    emit(
        f"  pooled corr(daily dln D_norm, daily return) {np.corrcoef(dl[ok], dr[ok])[0, 1]:+.3f}; corr(|return|, dln D_norm) {np.corrcoef(np.abs(dr[ok]), dl[ok])[0, 1]:+.3f}"
    )
    ok2 = np.isfinite(raw) & np.isfinite(dr) & P.member[:, 1:]
    emit(f"  pooled corr(daily dln d1 raw, daily return) {np.corrcoef(raw[ok2], dr[ok2])[0, 1]:+.3f}")
    part = (P.nsnap > 0) & (P.nsnap < MIN_SNAP) & P.member
    emit(f"  partial depth days (< {MIN_SNAP} snapshots) among member-days: {int(part.sum())}")


def delist_descriptive(P: Panel, D: np.ndarray, data_dir: Path) -> None:
    emit("")
    emit("## descriptive: depth before delisting (last daily candle before 2026-09-20)")
    info = rd(data_dir / "exchange_info.json")["info"]
    ratios = []
    for si, s in enumerate(P.syms):
        if (info.get(s) or {}).get("status") == "TRADING":
            continue
        fin = np.flatnonzero(np.isfinite(P.close[si]))
        if not fin.size or P.days[fin[-1]] >= "2026-09-20":
            continue
        L = int(fin[-1])
        a, b = D[si, max(0, L - 30) : L], D[si, max(0, L - 60) : max(0, L - 30)]
        if np.isfinite(a).sum() >= 10 and np.isfinite(b).sum() >= 10:
            ratios.append((s, P.days[L], float(np.nanmedian(a) / np.nanmedian(b))))
    if not ratios:
        emit("  no delisted symbol has depth in both windows (depth fetched only for top-200 member months)")
        return
    r = np.array([x[2] for x in ratios])
    emit(
        f"  n={r.size}: median ratio D(last 30 d) / D(30-60 d before) {np.median(r):.2f}; share < 0.5: {np.mean(r < 0.5):.2f}; share > 1: {np.mean(r > 1):.2f}"
    )
    rng = np.random.default_rng(SEED + 3)
    ref = []
    alive = [i for i, s in enumerate(P.syms) if (info.get(s) or {}).get("status") == "TRADING"]
    for _ in range(3000):
        si = int(rng.choice(alive))
        L = int(rng.integers(60, len(P.days)))
        if not P.member[si, L]:
            continue
        a, b = D[si, L - 30 : L], D[si, L - 60 : L - 30]
        if np.isfinite(a).sum() >= 10 and np.isfinite(b).sum() >= 10:
            ref.append(float(np.nanmedian(a) / np.nanmedian(b)))
    if ref:
        rr = np.array(ref)
        emit(
            f"  reference (random member days of trading symbols) n={rr.size}: median {np.median(rr):.2f}; share < 0.5: {np.mean(rr < 0.5):.2f}; share > 1: {np.mean(rr > 1):.2f}"
        )
    for s, d, x in sorted(ratios, key=lambda z: z[2])[:15]:
        emit(f"    {s} last candle {d}: ratio {x:.2f}")


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/market_makers_depth")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "universe", "fetch", "count", "report"), required=True)
    ap.add_argument("--limit", type=int, help="fetch only this many symbol-months (timing sample)")
    ap.add_argument("--conc", type=int, default=16)
    ap.add_argument("--shard", default="0/1", help="i/n: fetch every n-th symbol-month starting at i (parallel processes)")
    ap.add_argument("--event-months", help="YYYY-MM:YYYY-MM, supplementary report on another fully fetched block")
    ap.add_argument("--reverse", action="store_true", help="walk the plan oldest month first (a second process per shard)")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.open("a")
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "universe":
        stage_universe(cache, a.data_dir)
    elif a.stage == "fetch":
        i, n = (int(x) for x in a.shard.split("/"))
        asyncio.run(fetch_depth(cache, a.data_dir, a.limit, a.conc, (i, n), a.reverse))
    elif a.stage == "count":
        stage_count(cache, a.data_dir)
    elif a.stage == "report":
        stage_report(cache, a.data_dir, a.event_months)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
