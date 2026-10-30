#!/usr/bin/env python3
"""Item 10b of IDEA-20261030-demo-setups-depth: the failure of an S23 long (QM sweep with the narrow M2 stop) traded
as an S39-style short, on data the finding (S39 modules, H8, 4h 2021-2026) has not seen: 2020 on 2h / 4h / 8h / 12h /
1d and 2021-01 .. 2025-02 on 2h / 8h / 12h / 1d (1h is in TFS but not in the default --tfs: too slow, see main()).
Registration: the card's section «Попередня реєстрація 10b».

    uv run python scripts/verify_s39_s23_fail_short.py --stage sanity    # timing of 3 coins, counts and checks, no R
    uv run python scripts/verify_s39_s23_fail_short.py --stage run       # per-symbol rows (local caches, 4 workers)
    uv run python scripts/verify_s39_s23_fail_short.py --stage power     # n, weeks, SE and MDE only (no means)
    uv run python scripts/verify_s39_s23_fail_short.py --stage placebo   # random coin of the same bar, same geometry
    uv run python scripts/verify_s39_s23_fail_short.py --stage report    # tables (also <cache>/report.txt)

No network: 1h bars from the S14 cache, BTC trend from the S39 passport cache.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s38_modules as v38  # noqa: E402
import verify_s39_modules as v39  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402

from tradedesk.analytics import s38_modules as sm  # noqa: E402
from tradedesk.analytics import s39_modules as m9  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

DAY_MS = 24 * HOUR_MS
TFS = {"1h": 1, "2h": 2, "4h": 4, "8h": 8, "12h": 12, "1d": 24}
Y2021 = vsp.PASSPORT_MS
CUT = {tf: (Y2021 if tf == "4h" else vsp.OOS_MS) for tf in TFS}  # 4h 2021+ is the finding's own data
MDE_K = 2.80  # z(0.975) + z(0.80): the strictest step of Holm over 2, 80 % power
K_COINS = 3
emit = vsp.emit


def run_symbol(args: tuple[str, str, str, str, str, tuple[str, ...]]) -> dict[str, Any]:
    s, s1_cache, data_dir, pp_dir, out_dir, tfs = args
    t0 = time.time()
    s1c, dd, pp = Path(s1_cache), Path(data_dir), Path(pp_dir)
    member = vsp.s1.Membership(vsp.s1.members(s1c))
    btc = json.loads((pp / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + DAY_MS
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    h1, qv = vsp.load_1h_qv(s1c, dd, s)
    path = h1.mirrored()
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    timing: dict[str, float] = {}

    def row(kind: str, tf: str, t_in: int, e: float, st: float, tg: float, dpar: float) -> dict[str, Any] | None:
        if not (vsp.START_MS <= t_in < CUT[tf]) or not (tg < e < st):
            return None
        top = member(s, t_in)
        turn = sp.trailing_turnover(t_in, h1.t, qv)
        grp = sp.turnover_group(top, turn)
        if grp in (sp.LT1M, sp.UNKNOWN):
            why[f"{tf}:{kind}:{grp}"] += 1
            return None
        b = v39.short(path, t_in, e, st, tg, vsp.F1)
        if b is None:
            why[f"{tf}:{kind}:no_path"] += 1
            return None
        b15 = v39.short(path, t_in, e, st, tg, vsp.F15)
        j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
        rr = reward_risk_at("short", e, st, tg)
        why[f"{tf}:{kind}:cand"] += 1
        return {
            "k": f"{s}|{tf}|{kind}|{t_in}",
            "sym": s,
            "tf": tf,
            "kind": kind,
            "t_in": t_in,
            "turn": turn,
            "grp": grp,
            "rr": rr,
            "stop_pct": (st - e) / e,
            "score": sp.score_points("short", rr, sp.trend_at(daily, d_tr, t_in), int(btc_tr[j]) if j >= 0 else 0, s == vsp.BTC),
            "B": b.r_net,
            "B_x": b.exit_ms,
            "B15": None if b15 is None else b15.r_net,
            "cost": b.cost_r,
            "pct": m9.price_return(b.r_net, -e, -st),
            "dpar": dpar,
        }

    for tf in tfs:
        tt = time.time()
        hours = TFS[tf]
        h1c = h1.slice(0, int(np.searchsorted(h1.t, CUT[tf])))  # causal detectors: bars after the cut are not needed
        tb = h1c if hours == 1 else resample(h1c, hours)
        if len(tb) < ss.S23_WINDOW + 10:
            continue
        pos = segment_pos(tb.t)
        a_arr = atr(tb, 14, pos)
        used: set[int] = set()
        p3 = v39.parents(tb, "S3")
        for f in m9.failed_long(tb, pos, a_arr, p3, used=used):
            r = row("base", tf, int(tb.t[f.j]) + tb.bar_ms, f.entry, f.stop, f.target, 0.5)
            if r:
                rows.append(r)
        ts = time.time()
        p23 = v39.parents(tb, "S23")  # s23_all gates on the label "4h" only; the detector itself is bar-agnostic
        timing[f"{tf}_s23"] = time.time() - ts
        why[f"{tf}:s23_long_parents"] += len(p23)
        pstop = {p.r: p.stop for p in p23}
        for f in m9.failed_long(tb, pos, a_arr, p23, used=used):
            ar = float(a_arr[f.r])
            dpar = (float(tb.low[f.r]) - pstop[f.r]) / ar if ar > 0 else math.nan
            r = row("S23F", tf, int(tb.t[f.j]) + tb.bar_ms, f.entry, f.stop, f.target, dpar)
            if r:
                rows.append(r)
        timing[tf] = time.time() - tt
    out = Path(out_dir) / "rows"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why)}, separators=(",", ":")))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "timing": timing, "rss": vsp.rss_mb()}


def load(out: Path) -> dict[str, np.ndarray]:
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for p in sorted((out / "rows").glob("*.json")):
        d = json.loads(p.read_text())
        rows.extend(d["rows"])
        why.update(d["why"])
    T: dict[str, np.ndarray] = {}
    for k in sorted({k for r in rows for k in r}):
        col = [r.get(k) for r in rows]
        if k in ("k", "sym", "tf", "kind", "grp"):
            T[k] = np.array(["" if x is None else str(x) for x in col])
        elif k in ("t_in", "B_x"):
            T[k] = np.array(col, dtype=np.int64)
        else:
            T[k] = np.array([math.nan if x is None else float(x) for x in col])
    T["score"] = T["score"].astype(np.int64)
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    T["order"] = (T["kind"] != "base").astype(np.int64)
    T["_why"] = np.array([json.dumps(dict(why))])
    return T


def samples(a: argparse.Namespace, T: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], dict[int, set[str]]]:
    """Per timeframe: the S39 chain (one trade per coin) on base + S23F with the demo filter, >= $1M, prev-day top-250;
    "added" = S23F trades left in the chain (as H8). IND = added on 2020 (all TFs) and 2021-01..2025-02 (not 4h)."""
    demo = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn = np.nan_to_num(T["turn"], nan=-1) >= 1e6
    elig = vsp.s1.eligible_syms(a.data_dir)
    top = vsp.top_n_days(vsp.s1.daily_qv(a.s1_cache, a.data_dir, elig), 250)
    ok = demo & turn & vsp.in_prev_day_top(T, top)
    S: dict[str, np.ndarray] = {}
    add = np.zeros(T["B"].size, dtype=bool)
    base = np.zeros_like(add)
    for tf in TFS:
        for m_per in (T["t_in"] < Y2021, T["t_in"] >= Y2021):
            ch = vsp.chain(T, ok & (T["tf"] == tf) & m_per)
            add |= ch & (T["kind"] == "S23F")
            base |= ch & (T["kind"] == "base")
    S["add"], S["base"] = add, base
    S["y20"] = T["t_in"] < Y2021
    S["IND"] = add
    return S, top


def ov4h(a: argparse.Namespace, T: dict[str, np.ndarray]) -> np.ndarray:
    """Rows within +-24 h of a 4h S23-failure candidate of the same coin from the S39 modules cache (the finding's own
    signals, any year): the overlap with what was seen."""
    seen: dict[str, list[int]] = defaultdict(list)
    for p in (a.s39_cache / "rows").glob("*.json"):
        for r in json.loads(p.read_text())["rows"]:
            if r["kind"] == "H8" and r.get("tag") == "S23":
                seen[r["sym"]].append(int(r["t_in"]))
    srt = {s: np.sort(np.array(v, dtype=np.int64)) for s, v in seen.items()}
    out = np.zeros(T["B"].size, dtype=bool)
    for i, (s, t) in enumerate(zip(T["sym"], T["t_in"], strict=True)):
        arr = srt.get(str(s))
        if arr is not None and arr.size:
            k = int(np.searchsorted(arr, t - DAY_MS))
            out[i] = k < arr.size and arr[k] <= t + DAY_MS
    return out


def stage_sanity(a: argparse.Namespace, out: Path) -> None:
    t0 = time.time()
    s39_rows = {}
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / "S39"), str(out / "sanity"), tuple(TFS)))
        rows = json.loads((out / "sanity" / "rows" / f"{s}.json").read_text())["rows"]
        cnt = Counter((x["tf"], x["kind"]) for x in rows)
        emit(f"timing {s}: {r['sec']:.1f}s ({', '.join(f'{k} {v:.1f}s' for k, v in r['timing'].items())}); rss {r['rss']:.0f} MB")
        emit(f"  rows {dict(sorted(cnt.items()))}")
        emit(f"  S23 long parents {[(k, v) for k, v in r['why'].items() if 'parents' in k]}")
        # 4h 2020 check against the S39 modules cache: same base trades; S23F there are tagged among S14/S16/S23
        p = a.s39_cache / "rows" / f"{s}.json"
        if p.exists():
            old = json.loads(p.read_text())["rows"]
            ob = {int(x["t_in"]) for x in old if x["kind"] == "base" and x["t_in"] < Y2021}
            o23 = {int(x["t_in"]) for x in old if x["kind"] == "H8" and x.get("tag") == "S23" and x["t_in"] < Y2021}
            nb = {int(x["t_in"]) for x in rows if x["tf"] == "4h" and x["kind"] == "base"}
            n23 = {int(x["t_in"]) for x in rows if x["tf"] == "4h" and x["kind"] == "S23F"}
            s39_rows[s] = (len(ob ^ nb), len(o23 - n23))
            emit(
                f"  4h 2020 vs S39-modules cache: base {len(nb)} / {len(ob)} (sym. diff {len(ob ^ nb)}); S23-tagged H8 {len(o23)}, not in ours {len(o23 - n23)}; ours {len(n23)}"
            )
    n = len(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * n / a.workers / 60:.0f} min for {n} on {a.workers} workers"
    )


def stage_run(a: argparse.Namespace, out: Path) -> None:
    syms = vsp.syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    jobs = [
        (s, str(a.s1_cache), str(a.data_dir), str(a.passport_cache / "S39"), str(out), tuple(a.tfs.split(",")))
        for s in syms
        if a.force or not (out / "rows" / f"{s}.json").exists()
    ]
    v38.pool_run(run_symbol, jobs, a.workers, "run")


def se_only(T: dict[str, np.ndarray], m: np.ndarray) -> dict[str, float]:
    """Bootstrap SE of the mean of R on ``m`` from centred values: the mean itself is never returned."""
    v = T["B"][m]
    ok = np.isfinite(v)
    r = sp.mean_ci(v[ok] - v[ok].mean(), T["week"][m][ok], n_boot=vsp.N_BOOT, seed=vsp.SEED) if ok.sum() >= 2 else {"se": math.nan}
    return {"n": int(ok.sum()), "coins": len(set(T["sym"][m])), "weeks": len(set(T["week"][m])), "se": float(r["se"])}


def stage_power(a: argparse.Namespace, out: Path) -> None:
    T = load(out)
    S, _ = samples(a, T)
    ov = ov4h(a, T)
    emit(f"# power (no means): rows {T['B'].size}; MDE80 = {MDE_K}·SE (one-sided 0.025: Holm over 2, strictest step)")
    cells = [("IND (усе)", S["IND"]), ("IND без ±24h 4h-S23", S["IND"] & ~ov), ("2020, усі ТФ", S["IND"] & S["y20"])]
    cells += [("2021-01..2025-02, не 4h", S["IND"] & ~S["y20"])]
    cells += [(f"  {tf}", S["IND"] & (T["tf"] == tf)) for tf in TFS]
    for name, m in cells:
        r = se_only(T, m)
        emit(f"{name:28s} n {r['n']:5d}, монет {r['coins']:4d}, тижнів {r['weeks']:4d}, SE {r['se']:.3f}, MDE80 {MDE_K * r['se']:.3f}")


def stage_placebo(a: argparse.Namespace, out: Path) -> None:
    T = load(out)
    S, top = samples(a, T)
    have = set(vsp.syms_with_1h(a.s1_cache, a.data_dir))
    by: dict[str, list[list[Any]]] = defaultdict(list)
    for i in np.flatnonzero(S["IND"] | S["base"]):
        key, sym, t = str(T["k"][i]), str(T["sym"][i]), int(T["t_in"][i])
        prev = (t // DAY_MS) * DAY_MS - DAY_MS
        pool = sorted((top.get(prev, set()) & have) - {sym})
        for kk, c in enumerate(sm.draw_coins(key, pool, K_COINS)):
            by[c].append(["x", key, kk, t, float(T["stop_pct"][i]), math.nan])
    emit(f"[placebo] jobs {sum(len(v) for v in by.values())}, coins {len(by)}")
    jobs = [(c, str(a.s1_cache), str(a.data_dir), str(out), v) for c, v in sorted(by.items())]
    v38.pool_run(v39.placebo_symbol, jobs, a.workers, "placebo")


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T = load(out)
    S, _ = samples(a, T)
    ov = ov4h(a, T)
    B, IND = T["B"], S["IND"]
    pl: dict[str, list[float]] = defaultdict(list)
    for p in sorted((out / "placebo").glob("*.json")):
        for r in json.loads(p.read_text()):
            if r["m"] == "x" and r["B"] is not None and np.isfinite(r["B"]):
                pl[r["k"]].append(r["B"])
    PB = np.array([np.mean(pl[k]) if pl.get(k) else math.nan for k in T["k"]])
    X = B - PB
    paired, fmtd = v38.paired, v38.fmtd
    y20 = S["y20"]
    years_ind = ((Y2021 - vsp.START_MS) + (vsp.OOS_MS - Y2021)) / (365.25 * DAY_MS)
    emit(f"# 10b S23 failure as short; rows {T['B'].size} {dict(Counter(zip(T['tf'], T['kind'], strict=True)))}")
    emit(f"why: {json.loads(str(T['_why'][0]))}")
    r1, r2 = paired(T, B, IND), paired(T, X, IND)
    p_adj = holm([r1["p1"], r2["p1"]])
    emit(
        f"T1 E[R] net IND {fmtd(r1)} n {int(r1['n'])}, тижнів {int(r1['k'])}, p₁ {r1['p1']:.4f}, Холм {p_adj[0]:.4f}, MDE80 {MDE_K * r1['se']:.3f}"
    )
    emit(
        f"T2 X = R − плацебо IND {fmtd(r2)} n {int(r2['n'])}, p₁ {r2['p1']:.4f}, Холм {p_adj[1]:.4f}; плацебо E[R] {np.nanmean(PB[IND]):+.3f}"
    )
    e15 = paired(T, T["B15"], IND)
    emit(
        f"×1.5 {fmtd(e15)}; угод / рік {IND.sum() / years_ind:.0f}, сума R / рік {np.nansum(B[IND]) / years_ind:+.1f} (×1.5 {np.nansum(T['B15'][IND]) / years_ind:+.1f}); витрати {np.nanmean(T['cost'][IND]):.3f}R; % ціни {np.nanmean(T['pct'][IND]):+.3f}"
    )
    cells = [
        ("2020 (усі ТФ)", IND & y20),
        ("2021-01..2025-02 (не 4h)", IND & ~y20),
        ("без ±24h 4h-S23", IND & ~ov),
    ]
    cells += [(f"ТФ {tf}", IND & (T["tf"] == tf)) for tf in TFS]
    cells += [(f"рік {y}", IND & (T["year"] == y)) for y in range(2020, 2026)]
    no5, top5 = v38.drop_top(T, B, IND)
    cells += [(f"без топ-5 монет ({', '.join(top5)})", no5)]
    cells += [("$1-5M", IND & (T["grp"] == "1to5m"))]
    emit("| клітинка | n | E[R] | X | ×1.5 |")
    for name, m in cells:
        if m.sum() < 2:
            emit(f"| {name} | {int(m.sum())} | — | — | — |")
            continue
        emit(f"| {name} | {int(m.sum())} | {fmtd(paired(T, B, m))} | {np.nanmean(X[m]):+.3f} | {np.nanmean(T['B15'][m]):+.3f} |")
    # mechanism (descriptive): the S39 base (S3 parent, stop 0.5 ATR beyond the sweep) on the same TFs / periods
    base = S["base"]
    emit(f"S39 база (батько S3) на тих самих ТФ / періодах: {fmtd(paired(T, B, base))} n {int(base.sum())}; X {np.nanmean(X[base]):+.3f}")
    emit(f"S23F − база (тиждень-кластери): {fmtd(v38.sub_delta(T, B, IND, IND | base))} (Δ до об'єднання)")
    d = T["dpar"]
    med = float(np.nanmedian(d[IND]))
    for nm, m in (("вузький (dpar ≤ медіани IND)", IND & (d <= med)), ("ширший", IND & (d > med))):
        emit(f"  стоп батька {nm}: {fmtd(paired(T, B, m))} n {int(m.sum())}")
    emit(f"  медіана dpar (IND) {med:.3f} ATR; S3-батько 0.5; частка dpar < 0.5: {np.nanmean(d[IND] < 0.5):.2f}")
    late = IND & (T["year"] >= 2023)
    early = IND & (T["year"] < 2023)
    v38.per_asset(T, B, early, late, "E[R] (ранг 2020–2022 → 2023–2025-02)")
    v38.per_asset(T, X, early, late, "X (ранг 2020–2022 → 2023–2025-02)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("sanity", "run", "power", "placebo", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/s39_s23_fail_short")
    ap.add_argument("--s39-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/s39_modules")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    # 1h is left out: s23_all on 1h costs 140-185 s per long-history coin (about 6 h for all on 4 workers, sanity
    # 2026-10-31), and the S39 modules report flags its maker-at-close entry as the weakest on 1h
    ap.add_argument("--tfs", default="2h,4h,8h,12h,1d")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.stage}.txt").open("w") as fh:
        vsp.OUT["fh"] = fh
        {"sanity": stage_sanity, "run": stage_run, "power": stage_power, "placebo": stage_placebo, "report": stage_report}[a.stage](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
