#!/usr/bin/env python3
"""IDEA-20261022-demo-stop-management: stop management rules for the demo trader's setups on history.

Same entries for every variant (S14 / S16 / S3, long and short, 4h and 1d, the live stop and target rules, one
position per symbol decided by the baseline exit), paths on 1h bars, variants from ``analytics.stop_management``.
No network: the 1h bars and the point-in-time universe come from the step-1 cache (``--s1-cache``); delisting and
exchange info from the funding-crowding cache (``--data-dir``), both filled earlier through ``rt.binance_vision``.

Stages: ``sanity`` (known number of step 1, units, gaps, a 3-symbol timing), ``count`` (signals only, no outcomes),
``run`` (per-symbol trade files into ``--cache-dir``/trades, 4 worker processes), ``report`` (tables; the full output
also goes to ``--cache-dir``/report.txt).
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders, universe, period)

from tradedesk.analytics.course_basics import range_events  # noqa: E402
from tradedesk.analytics.course_s14_ema import (  # noqa: E402
    HOUR_MS,
    Bars,
    Params,
    atr,
    find_signals,
    indicators,
    resample,
    segment_pos,
    simulate,
)
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means, holm  # noqa: E402
from tradedesk.analytics.stop_management import (  # noqa: E402
    StopRule,
    Swings,
    chi2_sf,
    cochran_q,
    max_drawdown,
    simulate_stop_rule,
    swing_stops,
)
from tradedesk.execution.guard import tier_leverage  # noqa: E402  (read-only constant lookup)

SEED = 20261022
N_BOOT = 10_000
DAY_MS = 24 * HOUR_MS
P = Params()
TFS = (("4h", 4, 7 * DAY_MS, s1.END_MS), ("1d", 24, 30 * DAY_MS, s1.ms("2026-08-20T00:00")))
START_MS, OOS_MS = s1.START_MS, s1.OOS_MS
SETUP_ORDER = {"S14": 0, "S16": 1, "S3": 2}
TF_ORDER = {"4h": 0, "1d": 1}
SIDE_ORDER = {"long": 0, "short": 1}
BUFS = (0.5, 0.25, 1.0)


def rules(lev: int) -> dict[str, StopRule]:
    return {
        "A": StopRule("A"),
        "B": StopRule("B", be_frac=0.5),
        "C": StopRule("C", trail=True),
        "D": StopRule("D", be_r=1.0),
        "E": StopRule("E", be_move=0.30 / lev),
        "C2": StopRule("C2", trail=True, use_target=False),
        "B40": StopRule("B40", be_frac=0.4),
        "B60": StopRule("B60", be_frac=0.6),
        "C025": StopRule("C025", trail=True),
        "C100": StopRule("C100", trail=True),
        # exploratory (added after the first results): break-even also inside the trigger bar, the optimistic bound
        "Bx": StopRule("Bx", be_frac=0.5, be_same_bar=True),
    }


VARIANTS = tuple(rules(5))
SW_BUF = {"C": 0.5, "C2": 0.5, "C025": 0.25, "C100": 1.0}
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def lev_of(s: str) -> int:
    base = s[:-4] if s.endswith("USDT") else s
    return tier_leverage(f"{base}/USDT:USDT")


# ---------------------------------------------------------------- signals


def signals(bb: Bars) -> list[tuple[str, int, float, float, float]]:
    """(setup, signal bar index, entry, stop, target) of the live rules on one (possibly mirrored) bar series."""
    out: list[tuple[str, int, float, float, float]] = []
    if len(bb) >= P.warmup + 2:
        ind = indicators(bb, P)
        for sg in find_signals(bb, ind, P, variant="sweep"):
            out.append(("S14", sg.idx, sg.entry, sg.stop, sg.target))
    pos = segment_pos(bb.t)
    a = atr(bb, 14, pos)
    for ev in range_events(bb, pos, a, w=30, width_atr=6.0):
        ar = float(a[ev.r])
        if not (np.isfinite(ar) and ar > 0):
            continue
        out.append(("S16" if ev.kind == "dev" else "S3", ev.r, float(bb.c[ev.r]), ev.ld - 0.5 * ar, ev.tr if ev.kind == "dev" else ev.mid))
    return out


def run_symbol(args: tuple[str, str, str, str, bool]) -> dict[str, Any]:
    s, s1_cache, data_dir, out_dir, outcomes = args
    t0 = time.time()
    s1c, dd = Path(s1_cache), Path(data_dir)
    member = s1.Membership(s1.members(s1c))
    h1 = s1.load_1h(s1c, dd, s)
    lev = lev_of(s)
    R = rules(lev)
    cands = []
    swings: dict[tuple[str, str], dict[float, Swings]] = {}
    check: list[float] = []  # step-1 known number: S14 4h long sweep, per-stream busy, market fees
    for tf, hours, hold, end in TFS:
        tb = resample(h1, hours)
        for side in ("long", "short"):
            bb = tb if side == "long" else tb.mirrored()
            a = atr(bb, 14, segment_pos(bb.t))
            swings[(tf, side)] = {k: swing_stops(bb, a, k=2, buf_atr=k) for k in BUFS}
            path = h1 if side == "long" else h1.mirrored()
            sigs = signals(bb)
            if outcomes and tf == "4h" and side == "long":
                busy = -1
                for setup, i, e, st, tg in sigs:
                    t_in = bb.close_time(i)
                    if setup != "S14" or not s1.in_period(t_in) or not member(s, t_in) or t_in < busy:
                        continue
                    tr = simulate(path, t_in, e, st, tg, P.hold_ms)
                    if tr is None:
                        continue
                    busy = tr.exit_ms
                    if t_in < OOS_MS:
                        check.append(tr.r_net)
            for setup, i, e, st, tg in sigs:
                t_in = bb.close_time(i)
                if not (START_MS <= t_in < end) or not member(s, t_in) or not (st < e < tg):
                    continue
                cands.append((t_in, SETUP_ORDER[setup], TF_ORDER[tf], SIDE_ORDER[side], setup, tf, side, e, st, tg, hold))
    cands.sort(key=lambda x: x[:4])
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    busy = -1
    for t_in, _, _, _, setup, tf, side, e, st, tg, hold in cands:
        why[f"{setup}:{tf}:cand"] += 1
        if not outcomes:
            continue
        if t_in < busy:
            why[f"{setup}:{tf}:busy"] += 1
            continue
        path = h1 if side == "long" else h1.mirrored()
        base = simulate_stop_rule(path, t_in, e, st, tg, hold, R["A"])
        if base is None:
            why[f"{setup}:{tf}:no_path"] += 1
            continue
        busy = base.exit_ms
        real = (lambda x: x) if side == "long" else (lambda x: -x)
        row: dict[str, Any] = {
            "sym": s,
            "setup": setup,
            "tf": tf,
            "side": side,
            "entry_ms": t_in,
            "day": t_in // DAY_MS,
            "oos": t_in >= OOS_MS,
            "lev": lev,
            "stop_pct": (e - st) / abs(e),
            "target_r": (tg - e) / (e - st),
            "entry": real(e),
        }
        for v, rule in R.items():
            o = base if v == "A" else simulate_stop_rule(path, t_in, e, st, tg, hold, rule, swings=swings[(tf, side)][SW_BUF.get(v, 0.5)])
            row[f"{v}_r"] = o.r_net
            row[f"{v}_c"] = o.cost_r
            row[f"{v}_k"] = o.exit_kind
            row[f"{v}_m"] = o.moved
            row[f"{v}_x"] = o.exit_ms
        rows.append(row)
        why[f"{setup}:{tf}:trade"] += 1
    if outcomes:
        (Path(out_dir) / "trades").mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "trades" / f"{s}.json").write_text(json.dumps({"rows": rows, "check": check, "why": dict(why)}))
    gaps = int(np.sum(np.diff(h1.t) > HOUR_MS)) if len(h1) else 0
    return {"sym": s, "n": len(rows), "why": dict(why), "check": check, "sec": time.time() - t0, "bars": len(h1), "gaps": gaps, "rss": rss_mb()}


def all_syms(s1_cache: Path) -> list[str]:
    return sorted({s for v in s1.members(s1_cache).values() for s in v})


# ---------------------------------------------------------------- stats


def boot(v: np.ndarray, day: np.ndarray) -> np.ndarray:
    return cluster_bootstrap_means(v.tolist(), day.tolist(), n_boot=N_BOOT, seed=SEED)


def summ(v: np.ndarray, day: np.ndarray) -> dict[str, Any]:
    if v.size < 2:
        return {"n": int(v.size), "d": int(np.unique(day).size), "m": float(v.mean()) if v.size else math.nan, "lo": math.nan, "hi": math.nan, "p": math.nan}
    b = boot(v, day)
    p2 = min(1.0, 2 * min((np.sum(b <= 0) + 1) / (b.size + 1), (np.sum(b >= 0) + 1) / (b.size + 1)))
    return {
        "n": int(v.size),
        "d": int(np.unique(day).size),
        "m": float(v.mean()),
        "lo": float(np.quantile(b, 0.025)),
        "hi": float(np.quantile(b, 0.975)),
        "p": float(p2),
        "se": float(b.std()),
    }


def fs(x: dict[str, Any]) -> str:
    return f"{x['m']:+.3f} [{x['lo']:+.3f}; {x['hi']:+.3f}]"


def load(cache: Path) -> tuple[dict[str, np.ndarray], list[float]]:
    cols: dict[str, list[Any]] = {}
    check: list[float] = []
    for p in sorted((cache / "trades").glob("*.json")):
        d = json.loads(p.read_text())
        check += d["check"]
        for r in d["rows"]:
            for k, v in r.items():
                cols.setdefault(k, []).append(v)
    return {k: np.asarray(v) for k, v in cols.items()}, check


def variant_table(T: dict[str, np.ndarray], m: np.ndarray, title: str, vs: tuple[str, ...] = VARIANTS[:6]) -> None:
    emit(f"\n### {title}")
    emit("| варіант | n | дат | E[R] net [95 % CI] | виграшів | переміщено | медіана холду, год | макс. просадка, R |")
    emit("|---|---|---|---|---|---|---|---|")
    day = T["day"][m]
    for v in vs:
        r = T[f"{v}_r"][m]
        x = summ(r, day)
        hold = (T[f"{v}_x"][m] - T["entry_ms"][m]) / HOUR_MS
        order = np.argsort(T[f"{v}_x"][m], kind="stable")
        emit(
            f"| {v} | {x['n']} | {x['d']} | {fs(x)} | {np.mean(r > 0):.1%} | {np.mean(T[f'{v}_m'][m]):.1%} | {np.median(hold):.0f} | {max_drawdown(r[order]):.1f} |"
        )


def diff_rows(T: dict[str, np.ndarray], m: np.ndarray, pairs: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    out = {}
    for a, b in pairs:
        out[f"{a}-{b}"] = summ(T[f"{a}_r"][m] - T[f"{b}_r"][m], T["day"][m])
    return out


def diff_table(title: str, rows: list[tuple[str, dict[str, dict[str, Any]]]], keys: list[str]) -> None:
    emit(f"\n### {title}")
    emit("| зріз | n | дат | " + " | ".join(f"Δ {k} [CI]" for k in keys) + " |")
    emit("|---|---|---|" + "---|" * len(keys))
    for label, d in rows:
        x0 = d[keys[0]]
        emit(f"| {label} | {x0['n']} | {x0['d']} | " + " | ".join(fs(d[k]) for k in keys) + " |")


def stage_report(cache: Path) -> None:
    T, check = load(cache)
    n = T["sym"].size
    emit(f"trades {n}; symbols {np.unique(T['sym']).size}; dates {np.unique(T['day']).size}; rss {rss_mb():.0f} MB")
    c = np.asarray(check)
    emit(f"sanity step 1 (S14 4h long sweep IS, market fees): n={c.size} mean={c.mean():+.3f}R (step 1: n=508, -0.080R)")
    IS, OOS = ~T["oos"].astype(bool), T["oos"].astype(bool)
    emit("\n## Розподіл входів")
    for key in ("setup", "tf", "side"):
        emit(f"{key}: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(T[key].tolist()).items())))
    emit(f"IS {int(IS.sum())} / OOS {int(OOS.sum())}; mean stop {np.mean(T['stop_pct']):.2%}, median target {np.median(T['target_r']):.2f}R")

    # primary
    emit("\n## Основне порівняння (IS), сім'я Holm {C-B, B-A, C-A}")
    fam = diff_rows(T, IS, [("C", "B"), ("B", "A"), ("C", "A")])
    adj = holm([fam[k]["p"] for k in fam])
    emit("| порівняння | n | дат | ΔE[R] net [95 % CI] | p (двобічне) | Holm p | SD різниць | MDE 80 % |")
    emit("|---|---|---|---|---|---|---|---|")
    for (k, x), pa in zip(fam.items(), adj, strict=True):
        a, b = k.split("-")
        sd = float(np.std(T[f"{a}_r"][IS] - T[f"{b}_r"][IS]))
        emit(f"| {k} | {x['n']} | {x['d']} | {fs(x)} | {x['p']:.4f} | {pa:.4f} | {sd:.3f} | {2.8 * x['se']:.3f} |")
    oos_cb = summ(T["C_r"][OOS] - T["B_r"][OOS], T["day"][OOS])
    emit(f"OOS C-B: {fs(oos_cb)} n={oos_cb['n']} дат={oos_cb['d']}")
    cb = fam["C-B"]
    switch = cb["m"] >= 0.05 and cb["lo"] > 0 and adj[0] < 0.05 and oos_cb["m"] > 0
    emit(f"ПРАВИЛО РІШЕННЯ: перевести на C = {switch} (IS Δ={cb['m']:+.3f}, CI lo={cb['lo']:+.3f}, Holm p={adj[0]:.4f}, OOS Δ={oos_cb['m']:+.3f})")

    emit("\n## Вторинні різниці (без поправки)")
    sec = [("D", "A"), ("E", "A"), ("C2", "A"), ("C2", "C"), ("C", "B"), ("B", "A"), ("C", "A")]
    keys = [f"{a}-{b}" for a, b in sec]
    diff_table("IS / OOS / усе", [(lab, diff_rows(T, m, sec)) for lab, m in (("IS", IS), ("OOS", OOS), ("усе", IS | OOS))], keys)

    variant_table(T, IS, "Варіанти, IS")
    variant_table(T, OOS, "Варіанти, OOS")

    emit("\n## Угоди, де стоп перемістився: що вони зробили б за A (IS+OOS)")
    emit("| варіант | переміщено | A: стоп | A: ціль | A: час/кінець | E[R] за варіантом | E[R] за A |")
    emit("|---|---|---|---|---|---|---|")
    ak = T["A_k"]
    for v in ("B", "C", "D", "E", "C2"):
        mv = T[f"{v}_m"].astype(bool)
        k = ak[mv]
        st = np.isin(k, ["stop", "stop_gap"]).mean() if k.size else math.nan
        tg = (k == "target").mean() if k.size else math.nan
        emit(f"| {v} | {mv.sum()} ({mv.mean():.1%}) | {st:.1%} | {tg:.1%} | {1 - st - tg:.1%} | {T[f'{v}_r'][mv].mean():+.3f} | {T['A_r'][mv].mean():+.3f} |")

    emit("\n## Розрізи: Δ C-B і B-A (IS / OOS)")
    pairs = [("C", "B"), ("B", "A"), ("C", "A")]
    ks = ["C-B", "B-A", "C-A"]
    be = np.isin(T["sym"], ["BTCUSDT", "ETHUSDT"])
    years = (T["entry_ms"] // DAY_MS * DAY_MS).astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970
    groups: list[tuple[str, np.ndarray]] = [(f"setup {x}", T["setup"] == x) for x in ("S14", "S16", "S3")]
    groups += [(f"tf {x}", T["tf"] == x) for x in ("4h", "1d")]
    groups += [(f"{x}", T["side"] == x) for x in ("long", "short")]
    groups += [("BTC+ETH", be), ("альти", ~be)]
    for sp, m0 in (("IS", IS), ("OOS", OOS)):
        diff_table(f"Розрізи, {sp}", [(lab, diff_rows(T, m0 & g, pairs)) for lab, g in groups if (m0 & g).sum() >= 2], ks)
    diff_table("За роками", [(str(y), diff_rows(T, years == y, pairs)) for y in sorted(set(years.tolist())) if (years == y).sum() >= 2], ks)
    emit("\n### E[R] net за сетапом і таймфреймом (усе, A / B / C)")
    emit("| сетап | tf | n | A | B | C |\n|---|---|---|---|---|---|")
    for st in ("S14", "S16", "S3"):
        for tf in ("4h", "1d"):
            m = (T["setup"] == st) & (T["tf"] == tf)
            if m.sum() >= 2:
                emit(f"| {st} | {tf} | {int(m.sum())} | " + " | ".join(fs(summ(T[f"{v}_r"][m], T["day"][m])) for v in ("A", "B", "C")) + " |")

    emit("\n## Стійкість (IS / OOS)")
    for sp, m0 in (("IS", IS), ("OOS", OOS)):
        emit(f"\n### {sp}")
        emit("| рядок | значення [95 % CI] |\n|---|---|")
        for v in ("A", "B", "C", "D", "E", "C2"):
            emit(f"| витрати ×2: E[R] {v} | {fs(summ(T[f'{v}_r'][m0] - T[f'{v}_c'][m0], T['day'][m0]))} |")
        cb2 = (T["C_r"] - T["C_c"]) - (T["B_r"] - T["B_c"])
        emit(f"| витрати ×2: Δ C-B | {fs(summ(cb2[m0], T['day'][m0]))} |")
        for v in ("C025", "C100"):
            emit(f"| буфер трейлу {SW_BUF[v]:g} ATR: Δ {v}-B | {fs(summ(T[f'{v}_r'][m0] - T['B_r'][m0], T['day'][m0]))} |")
            emit(f"| буфер трейлу {SW_BUF[v]:g} ATR: E[R] {v} | {fs(summ(T[f'{v}_r'][m0], T['day'][m0]))} |")
        for v in ("B40", "B60"):
            emit(f"| тригер BE {v[1:]} %: Δ {v}-A | {fs(summ(T[f'{v}_r'][m0] - T['A_r'][m0], T['day'][m0]))} |")
            emit(f"| тригер BE {v[1:]} %: Δ C-{v} | {fs(summ(T['C_r'][m0] - T[f'{v}_r'][m0], T['day'][m0]))} |")
        d = T["C_r"][m0] - T["B_r"][m0]
        dd = T["day"][m0]
        tot: dict[int, float] = {}
        for x, y in zip(d.tolist(), dd.tolist(), strict=True):
            tot[y] = tot.get(y, 0.0) + x
        top = [k for k, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:10]]
        keep = ~np.isin(dd, top)
        emit(f"| без 10 найкращих дат: Δ C-B | {fs(summ(d[keep], dd[keep]))} |")
        for lab, a_, b_ in (("Bx-A", "Bx", "A"), ("C-Bx", "C", "Bx"), ("Bx-B", "Bx", "B")):
            emit(f"| дослідницьке, BE і в годину тригера: Δ {lab} | {fs(summ(T[f'{a_}_r'][m0] - T[f'{b_}_r'][m0], T['day'][m0]))} |")
        emit(f"| дослідницьке: E[R] Bx | {fs(summ(T['Bx_r'][m0], T['day'][m0]))} |")

    per_asset(T, IS, OOS)


def per_asset(T: dict[str, np.ndarray], IS: np.ndarray, OOS: np.ndarray) -> None:
    emit("\n## За активами (5a): Δ C-B")
    syms = sorted(set(T["sym"].tolist()))
    d = T["C_r"] - T["B_r"]
    means, ses, rows = [], [], []
    for s in syms:
        m = T["sym"] == s
        v = d[m]
        se = float(v.std(ddof=1) / math.sqrt(v.size)) if v.size >= 2 else math.nan
        means.append(float(v.mean()))
        ses.append(se)
        rows.append((s, int(m.sum()), float(v.mean()), se, float(d[m & IS].mean()) if (m & IS).any() else math.nan, int((m & IS).sum())))
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    pq = chi2_sf(q, df)
    emit(f"Cochran Q={q:.1f}, df={df}, p={pq:.3f}, I²={i2:.1%} (symbols with n>=2: {df + 1})")
    emit("| символ | n | Δ C-B | SE |\n|---|---|---|---|")
    for s, nn, mu, se, _, _ in sorted(rows, key=lambda r: -r[1])[:25]:
        emit(f"| {s} | {nn} | {mu:+.3f} | {se:.3f} |")
    emit("(25 символів з найбільшою кількістю угод; повна таблиця — у trades/)")
    ok = [r for r in rows if r[5] >= 20]
    ok.sort(key=lambda r: -r[4])
    top = {r[0] for r in ok[: max(1, len(ok) // 4)]}
    mt = np.isin(T["sym"], list(top))
    a = summ(d[OOS & mt], T["day"][OOS & mt])
    b = summ(d[OOS & ~mt], T["day"][OOS & ~mt])
    emit(f"Стійкість: верхня чверть за IS Δ C-B ({len(top)} символів з >=20 IS-угод): OOS {fs(a)} n={a['n']}; решта OOS {fs(b)} n={b['n']}")


# ---------------------------------------------------------------- stages


def stage_sanity(s1_cache: Path, data_dir: Path, out: Path) -> None:
    syms = all_syms(s1_cache)
    info = s1.rd(data_dir / "exchange_info.json")["info"]
    emit(f"members {len(syms)}; not TRADING today: {sum(1 for s in syms if (info.get(s) or {}).get('status') != 'TRADING')}")
    emit(f"tier leverage: BTCUSDT {lev_of('BTCUSDT')}, ETHUSDT {lev_of('ETHUSDT')}, SOLUSDT {lev_of('SOLUSDT')}, 1000PEPEUSDT {lev_of('1000PEPEUSDT')}")
    t0 = time.time()
    for s in ("BTCUSDT", "SOLUSDT", "DOGEUSDT"):
        r = run_symbol((s, str(s1_cache), str(data_dir), str(out / "sanity"), True))
        h1 = s1.load_1h(s1_cache, data_dir, s)
        emit(
            f"{s}: bars {r['bars']} ({datetime.fromtimestamp(h1.t[0] / 1000, UTC):%Y-%m-%d}..{datetime.fromtimestamp(h1.t[-1] / 1000, UTC):%Y-%m-%d}), "
            f"close {h1.c[-1]:.6g}, holes>1h {r['gaps']}, trades {r['n']}, {r['sec']:.1f}s, rss {r['rss']:.0f} MB; {r['why']}"
        )
    el = time.time() - t0
    emit(f"timing: {el:.0f}s for 3 symbols -> estimate {el / 3 * len(syms) / 4 / 60:.0f} min for {len(syms)} symbols on 4 workers")


def stage_count(s1_cache: Path, data_dir: Path, out: Path) -> None:
    tot: Counter[str] = Counter()
    for s in all_syms(s1_cache):
        tot.update(run_symbol((s, str(s1_cache), str(data_dir), str(out), False))["why"])
    for k, v in sorted(tot.items()):
        emit(f"{k}: {v}")


def stage_run(s1_cache: Path, data_dir: Path, out: Path, limit: int | None, workers: int) -> None:
    syms = all_syms(s1_cache)[: limit or None]
    jobs = [(s, str(s1_cache), str(data_dir), str(out), True) for s in syms]
    t0 = time.time()
    tot: Counter[str] = Counter()
    with Pool(workers) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            tot.update(r["why"])
            if k % 10 == 0 or k == len(jobs):
                emit(f"  {k}/{len(jobs)} symbols, {time.time() - t0:.0f}s, last {r['sym']} n={r['n']}, worker rss {r['rss']:.0f} MB")
    emit(f"run done: {dict(sorted(tot.items()))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("sanity", "count", "run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/demo_stop_management")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    with (a.cache_dir / f"{a.stage}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"# {a.stage} {datetime.now(UTC).isoformat(timespec='seconds')}")
        if a.stage == "sanity":
            stage_sanity(a.s1_cache, a.data_dir, a.cache_dir)
        elif a.stage == "count":
            stage_count(a.s1_cache, a.data_dir, a.cache_dir)
        elif a.stage == "run":
            stage_run(a.s1_cache, a.data_dir, a.cache_dir, a.limit, a.workers)
        else:
            stage_report(a.cache_dir)
        emit(f"# done, rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
