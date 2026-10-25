#!/usr/bin/env python3
"""IDEA-20261025-mark-price-stop-trigger: the demo trader's stop fired by the Binance mark price instead of the last.

Same 15 363 trades as the stop study (``verify_demo_stop_management.py``, rule B = break-even at 50 % of the way to
the target), same 1h last-price bars (step-1 cache). The mark branch is ``analytics.mark_stop.simulate_mark_stop``.

Stages:
- ``plan``: symbol-months the trades are held in, and how many mark files are missing (no network, no R);
- ``fetch``: ``markPriceKlines`` 1h monthly through ``rt.binance_vision`` into ``--cache-dir``/mark1h/<sym>/<YYYY-MM>.json;
- ``count`` (step 0, the gate): divergent stops, mark-earlier stops, MDE under the card's assumptions; no R printed;
- ``run``: per-symbol paired rows (last branch, mark branch, fill sensitivities, placebo) into ``--cache-dir``/rows;
- ``report``: tables; the full output also goes to ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
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

import verify_course_s14_ema as s1  # noqa: E402

from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars  # noqa: E402
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means  # noqa: E402
from tradedesk.analytics.mark_stop import (  # noqa: E402
    STOP_KINDS,
    align,
    divergent,
    mde_clustered,
    shifted_mark,
    simulate_mark_stop,
)
from tradedesk.analytics.stop_management import StopRule, chi2_sf, cochran_q, simulate_stop_rule  # noqa: E402

SEED = 20261025
N_BOOT = 10_000
DAY_MS = 24 * HOUR_MS
HOLD = {"4h": 7 * DAY_MS, "1d": 30 * DAY_MS}
RULE_B = StopRule("B", be_frac=0.5)
OOS_MS = s1.OOS_MS
PLACEBO_SHIFT_MS = 7 * DAY_MS
KEYS = ("sym", "setup", "tf", "side", "entry_ms", "day", "oos", "stop_pct", "target_r", "entry", "B_k", "B_x")
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def month_of(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m")


def trades_of(trades_dir: Path, s: str) -> list[dict[str, Any]]:
    """Trade definitions only (no R columns are kept)."""
    d = json.loads((trades_dir / f"{s}.json").read_text())
    return [{k: r[k] for k in KEYS} for r in d["rows"]]


def symbols(trades_dir: Path) -> list[str]:
    return sorted(p.stem for p in trades_dir.glob("*.json"))


def held_months(r: dict[str, Any]) -> list[str]:
    a, b = r["entry_ms"], r["entry_ms"] + HOLD[r["tf"]]
    out, t = [], a
    while month_of(t) <= month_of(b):
        mo = month_of(t)
        if not out or out[-1] != mo:
            out.append(mo)
        t += DAY_MS
    if out[-1] != month_of(b):
        out.append(month_of(b))
    return out


def plan(trades_dir: Path, cache: Path) -> list[tuple[str, str, Path]]:
    jobs = []
    for s in symbols(trades_dir):
        mos = sorted({mo for r in trades_of(trades_dir, s) for mo in held_months(r)})
        for mo in mos:
            if mo > s1.LAST_MONTH:
                continue
            p = cache / "mark1h" / s / f"{mo}.json"
            if not p.exists():
                jobs.append((s, mo, p))
    return jobs


def load_mark(cache: Path, s: str) -> Bars:
    rows: list[list[float]] = []
    d = cache / "mark1h" / s
    if d.exists():
        for p in sorted(d.glob("*.json")):
            rows.extend(json.loads(p.read_text()) or [])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 5))
    if a.size:
        a = a[np.argsort(a[:, 0], kind="stable")]
        a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
        a = a[(a[:, 0] % HOUR_MS == 0) & (a[:, 1:5].min(axis=1) > 0)]
    return Bars.from_rows(a[:, :5] if a.size else a, HOUR_MS)


async def fetch_jobs(jobs: list[tuple[str, str, Path]]) -> None:
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    _, rt = build_server()
    sem = asyncio.Semaphore(8)
    errors: Counter[str] = Counter()
    done = 0
    t0 = time.time()
    emit(f"[mark1h] {len(jobs)} files to fetch")

    async def one(s: str, mo: str, p: Path) -> None:
        nonlocal done
        async with sem:
            try:
                f = await rt.binance_vision.um_mark_price_monthly(s, "1h", int(mo[:4]), int(mo[5:]))
                rows: Any = [[r[0], r[1], r[2], r[3], r[4]] for r in f.data]
            except ProviderError as e:
                if e.reason != "data_unavailable":
                    errors[e.reason] += 1
                    return
                rows = None
        s1.wr(p, rows)
        done += 1
        if done % 200 == 0:
            el = time.time() - t0
            emit(f"  [mark1h] {done}/{len(jobs)} in {el:.0f}s, eta {el / done * (len(jobs) - done):.0f}s, rss {rss_mb():.0f} MB")

    try:
        await asyncio.gather(*[one(*j) for j in jobs])
    finally:
        await rt.aclose()
    emit(f"[mark1h] done {done}/{len(jobs)} in {time.time() - t0:.0f}s; transient errors: {dict(errors)}; rss {rss_mb():.0f} MB")


# ---------------------------------------------------------------- per-symbol simulation


def path_prices(r: dict[str, Any]) -> tuple[float, float, float]:
    e = r["entry"] if r["side"] == "long" else -r["entry"]
    st = e - r["stop_pct"] * abs(e)
    tg = e + r["target_r"] * (e - st)
    return e, st, tg


def qv30(raw: np.ndarray, t_ms: int) -> float:
    """Quote volume of the 30 days before ``t_ms`` from raw 1h rows [open, o, h, l, c, qv]."""
    if raw.size == 0:
        return math.nan
    a, b = np.searchsorted(raw[:, 0], [t_ms - 30 * DAY_MS, t_ms])
    return float(raw[a:b, 5].sum())


def raw_1h(s1_cache: Path, data_dir: Path, s: str) -> np.ndarray:
    rows: list[list[float]] = []
    for mo in s1.listing(data_dir, s):
        p = s1_cache / "k1h" / s / f"{mo}.json"
        if p.exists():
            rows.extend(json.loads(p.read_text()) or [])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 6))
    return a[np.argsort(a[:, 0], kind="stable")] if a.size else a


def sim_symbol(args: tuple[str, str, str, str, str, bool]) -> dict[str, Any]:
    s, trades_dir, cache, s1_cache, data_dir, outcomes = args
    t0 = time.time()
    h1 = s1.load_1h(Path(s1_cache), Path(data_dir), s)
    mark = load_mark(Path(cache), s)
    plac = shifted_mark(h1, mark, PLACEBO_SHIFT_MS)
    raw = raw_1h(Path(s1_cache), Path(data_dir), s) if outcomes else np.zeros((0, 6))
    paths = {"long": h1, "short": h1.mirrored()}
    mks = {"long": align(h1, mark), "short": align(h1.mirrored(), mark.mirrored())}
    pks = {"long": align(h1, plac), "short": align(h1.mirrored(), plac.mirrored())}
    cnt: Counter[str] = Counter()
    rows = []
    for r in trades_of(Path(trades_dir), s):
        side, hold = r["side"], HOLD[r["tf"]]
        e, st, tg = path_prices(r)
        last = simulate_stop_rule(paths[side], r["entry_ms"], e, st, tg, hold, RULE_B)
        if last is None:
            cnt["no_last"] += 1
            continue
        same = last.exit_kind == r["B_k"] and last.exit_ms == r["B_x"]
        cnt["repro_ok" if same else "repro_bad"] += 1
        mo = simulate_mark_stop(paths[side], mks[side], r["entry_ms"], e, st, tg, hold, RULE_B)
        if mo is None:
            cnt["no_mark"] += 1
            continue
        dv = divergent(last, paths[side], mks[side], e, st)
        is_stop = last.exit_kind in STOP_KINDS
        earlier = mo.exit_ms < last.exit_ms and mo.exit_kind in STOP_KINDS
        differs = mo.exit_ms != last.exit_ms or mo.exit_kind != last.exit_kind or abs(mo.exit - last.exit) > 1e-12 * abs(e)
        late = bool(dv) and mo.exit_kind in STOP_KINDS and mo.exit_ms > last.exit_ms
        cnt["trades"] += 1
        cnt["stop_exits"] += int(is_stop)
        cnt["divergent"] += int(bool(dv))
        cnt["mark_earlier"] += int(earlier)
        cnt["differs"] += int(differs)
        row: dict[str, Any] = {
            "sym": s,
            "side": side,
            "tf": r["tf"],
            "setup": r["setup"],
            "day": r["day"],
            "oos": r["oos"],
            "stop_pct": r["stop_pct"],
            "last_k": last.exit_kind,
            "mark_k": mo.exit_kind,
            "div": bool(dv),
            "be_div": bool(dv) and last.exit_kind.startswith("be"),
            "earlier": earlier,
            "differs": differs,
            "late": late,
            "late_bars": int((mo.exit_ms - last.exit_ms) // HOUR_MS) if late else 0,
        }
        if outcomes:
            row["last_r"] = last.r_net
            row["mark_r"] = mo.r_net
            for f in ("stop", "worst"):
                o2 = simulate_mark_stop(paths[side], mks[side], r["entry_ms"], e, st, tg, hold, RULE_B, fill=f)
                row[f"mark_{f}_r"] = o2.r_net if o2 is not None else math.nan
            po = simulate_mark_stop(paths[side], pks[side], r["entry_ms"], e, st, tg, hold, RULE_B)
            row["plac_r"] = po.r_net if po is not None else math.nan
            row["qv30"] = qv30(raw, r["entry_ms"])
            for x2 in (2.0, 3.0):  # cost stress on both branches: every paid fee times x2
                from tradedesk.analytics.stop_management import Fees

                fz = Fees().scaled(x2)
                a2 = simulate_stop_rule(paths[side], r["entry_ms"], e, st, tg, hold, RULE_B, fees=fz)
                b2 = simulate_mark_stop(paths[side], mks[side], r["entry_ms"], e, st, tg, hold, RULE_B, fees=fz)
                row[f"d_cost{int(x2)}"] = (b2.r_net - a2.r_net) if (a2 is not None and b2 is not None) else math.nan
        rows.append(row)
    if outcomes:
        out = Path(cache) / "rows"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{s}.json").write_text(json.dumps(rows))
    ml = mks["long"]
    dev = np.abs(ml.o[ml.ok] / h1.o[ml.ok] - 1) if ml.ok.any() else np.zeros(1)
    return {
        "sym": s,
        "cnt": dict(cnt),
        "rows": [] if outcomes else rows,
        "sec": time.time() - t0,
        "rss": rss_mb(),
        "mark_bars": len(mark),
        "last_bars": len(h1),
        "dev_med": float(np.median(dev)),
        "dev_max": float(dev.max()),
        "wick_lo": float(np.median((ml.low[ml.ok] - h1.low[ml.ok]) / h1.o[ml.ok])) if ml.ok.any() else math.nan,
    }


# ---------------------------------------------------------------- stats


def summ(v: np.ndarray, day: np.ndarray) -> dict[str, Any]:
    ok = np.isfinite(v)
    v, day = v[ok], day[ok]
    if v.size < 2:
        return {
            "n": int(v.size),
            "d": int(np.unique(day).size),
            "m": float(v.mean()) if v.size else math.nan,
            "lo": math.nan,
            "hi": math.nan,
            "p": math.nan,
            "se": math.nan,
        }
    b = cluster_bootstrap_means(v.tolist(), day.tolist(), n_boot=N_BOOT, seed=SEED)
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
    return f"{x['m']:+.4f} [{x['lo']:+.4f}; {x['hi']:+.4f}]"


def row_line(label: str, d: np.ndarray, day: np.ndarray) -> str:
    x = summ(d, day)
    return f"| {label} | {x['n']} | {x['d']} | {fs(x)} | {x['p']:.3f} |"


def stage_count(trades_dir: Path, cache: Path, s1_cache: Path, data_dir: Path, workers: int) -> None:
    syms = symbols(trades_dir)
    jobs = [(s, str(trades_dir), str(cache), str(s1_cache), str(data_dir), False) for s in syms]
    tot: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    san: list[tuple[str, float, float, float, int, int]] = []
    with Pool(workers) as pool:
        for i, res in enumerate(pool.imap_unordered(sim_symbol, jobs), 1):
            tot.update(res["cnt"])
            rows += res["rows"]
            san.append((res["sym"], res["dev_med"], res["dev_max"], res["wick_lo"], res["mark_bars"], res["last_bars"]))
            if i % 50 == 0:
                emit(f"  {i}/{len(syms)} symbols, rss {rss_mb():.0f} MB")
    emit(f"counts: {dict(tot)}")
    san.sort(key=lambda x: -x[1])
    dm = np.array([x[1] for x in san])
    emit(
        f"sanity |mark open / last open - 1| per symbol: median of medians {np.median(dm):.5f}, worst 5: "
        + ", ".join(f"{x[0]} med {x[1]:.4f} max {x[2]:.3f}" for x in san[:5])
    )
    emit(
        "sanity (mark low - last low) / open, median of per-symbol medians (> 0: mark wicks less): "
        f"{np.nanmedian([x[3] for x in san]):+.5f}"
    )
    for s in ("BTCUSDT", "SOLUSDT", "DOGEUSDT"):
        for x in san:
            if x[0] == s:
                emit(f"  {s}: mark bars {x[4]}, last bars {x[5]}, dev med {x[1]:.5f}, max {x[2]:.4f}")
    T = {k: np.asarray([r[k] for r in rows]) for k in rows[0]}
    IS = ~T["oos"]
    emit("| вибірка | угод | дат | стоп-виходів last | розбіжних | з них БУ | mark раніше | будь-яка різниця | q (різниця) |")
    emit("|---|---|---|---|---|---|---|---|---|")
    for label, m in [
        ("усі", np.ones(IS.size, bool)),
        ("IS", IS),
        ("OOS", ~IS),
        ("IS лонги", IS & (T["side"] == "long")),
        ("IS шорти", IS & (T["side"] == "short")),
    ]:
        stops = np.isin(T["last_k"][m], STOP_KINDS).sum()
        emit(
            f"| {label} | {m.sum()} | {np.unique(T['day'][m]).size} | {stops} | {T['div'][m].sum()} | {T['be_div'][m].sum()} | "
            f"{T['earlier'][m].sum()} | {T['differs'][m].sum()} | {T['differs'][m].mean():.4f} |"
        )
    lb = T["late_bars"][T["late"]]
    emit(
        f"late mark-stops (divergent, mark stop fired later): {T['late'].sum()}; bars later p50 {np.median(lb) if lb.size else math.nan}, "
        f"p90 {np.quantile(lb, 0.9) if lb.size else math.nan}"
    )
    emit("MDE (z = 2.8, day clusters, IS, flags = any exit difference):")
    for mu, sd in ((1.0, 1.2), (0.5, 1.2), (1.0, 1.5)):
        emit(
            f"  mu {mu}, sd {sd}: MDE {mde_clustered(T['differs'][IS], T['day'][IS], mu, sd):.4f}R; "
            f"flags = divergent only: {mde_clustered(T['div'][IS], T['day'][IS], mu, sd):.4f}R"
        )
    emit(f"rss {rss_mb():.0f} MB")


def stage_run(trades_dir: Path, cache: Path, s1_cache: Path, data_dir: Path, workers: int, limit: int | None) -> None:
    syms = symbols(trades_dir)[: limit or None]
    jobs = [(s, str(trades_dir), str(cache), str(s1_cache), str(data_dir), True) for s in syms]
    t0 = time.time()
    tot: Counter[str] = Counter()
    with Pool(workers) as pool:
        for i, res in enumerate(pool.imap_unordered(sim_symbol, jobs), 1):
            tot.update(res["cnt"])
            if i % 50 == 0 or i == len(syms):
                emit(f"  {i}/{len(syms)} symbols in {time.time() - t0:.0f}s, worker rss {res['rss']:.0f} MB")
    emit(f"run done: {dict(tot)}")


def load_rows(cache: Path) -> dict[str, np.ndarray]:
    cols: dict[str, list[Any]] = {}
    for p in sorted((cache / "rows").glob("*.json")):
        for r in json.loads(p.read_text()):
            for k, v in r.items():
                cols.setdefault(k, []).append(v)
    return {k: np.asarray(v) for k, v in cols.items()}


def stage_report(cache: Path) -> None:
    T = load_rows(cache)
    n = T["sym"].size
    IS, OOS = ~T["oos"], T["oos"]
    d = T["mark_r"] - T["last_r"]
    day = T["day"]
    emit(f"paired trades {n}; symbols {np.unique(T['sym']).size}; IS {IS.sum()} / OOS {OOS.sum()}; rss {rss_mb():.0f} MB")
    emit("")
    emit("## H1 (primary): mark − last, E[R] net per trade")
    emit("| вибірка | n | дат | Δ E[R] [95 % CI] | p |")
    emit("|---|---|---|---|---|")
    emit(row_line("IS (primary)", d[IS], day[IS]))
    emit(row_line("OOS", d[OOS], day[OOS]))
    emit(row_line("усі", d, day))
    emit("")
    emit("## branches (E[R] net)")
    emit("| вибірка | last | mark |")
    emit("|---|---|---|")
    for lab, m in (("IS", IS), ("OOS", OOS)):
        emit(f"| {lab} | {fs(summ(T['last_r'][m], day[m]))} | {fs(summ(T['mark_r'][m], day[m]))} |")
    emit("")
    emit("## splits (descriptive; H2 = shorts, lower half of turnover)")
    # turnover half: 30-day quote volume at entry vs the median of trades in the same calendar month
    mon = (T["day"] * DAY_MS).astype(np.int64)
    mkey = np.array([month_of(int(t)) for t in mon])
    low = np.zeros(n, bool)
    for mo in np.unique(mkey):
        mm = mkey == mo
        q = T["qv30"][mm]
        low[mm] = q < np.nanmedian(q)
    emit("| вибірка | n | дат | Δ E[R] [95 % CI] | p |")
    emit("|---|---|---|---|---|")
    for lab, m in [
        ("IS лонги", IS & (T["side"] == "long")),
        ("IS шорти", IS & (T["side"] == "short")),
        ("IS верхня половина обігу", IS & ~low),
        ("IS нижня половина обігу", IS & low),
        ("IS H2: шорти, нижня половина", IS & low & (T["side"] == "short")),
        ("OOS H2: шорти, нижня половина", OOS & low & (T["side"] == "short")),
        ("IS 4h", IS & (T["tf"] == "4h")),
        ("IS 1d", IS & (T["tf"] == "1d")),
    ]:
        emit(row_line(lab, d[m], day[m]))
    emit("")
    emit("## divergent trades: what the gain is made of")
    for lab, m in (("розбіжні (last-стоп, mark ні)", T["div"]), ("mark раніше за last", T["earlier"]), ("пізній mark-стоп", T["late"])):
        x = d[m]
        if x.size:
            emit(
                f"{lab}: n {x.size}, mean {x.mean():+.3f}R, median {np.median(x):+.3f}, p10 {np.quantile(x, 0.1):+.3f}, "
                f"p90 {np.quantile(x, 0.9):+.3f}, min {x.min():+.3f}, max {x.max():+.3f}"
            )
    emit(f"mark-branch exits of divergent trades: {dict(Counter(T['mark_k'][T['div']].tolist()))}")
    emit(f"worst trade: last {T['last_r'].min():+.3f}R, mark {T['mark_r'].min():+.3f}R (kill if mark worse by > 1R)")
    emit("")
    emit("## robustness")
    emit("| варіант | n | дат | Δ E[R] [95 % CI] | p |")
    emit("|---|---|---|---|---|")
    for lab, col in (("fill = stop (sensitivity)", "mark_stop_r"), ("fill = worst last low (sensitivity)", "mark_worst_r")):
        dd = T[col] - T["last_r"]
        emit(row_line(f"IS {lab}", dd[IS], day[IS]))
        emit(row_line(f"OOS {lab}", dd[OOS], day[OOS]))
    # exploratory (added after the first results): the worst fill only where the mark stop fired after the last stop
    # (the lag risk), every other trade keeps the pre-registered fill; 'worst' above also penalises same-bar stops
    lw = np.where(T["late"], T["mark_worst_r"], T["mark_r"]) - T["last_r"]
    emit(row_line("IS [exploratory] worst fill лише на пізніх mark-стопах", lw[IS], day[IS]))
    emit(row_line("OOS [exploratory] worst fill лише на пізніх mark-стопах", lw[OOS], day[OOS]))
    emit(f"  worst trade under it: {np.nanmin(np.where(T['late'], T['mark_worst_r'], T['mark_r'])):+.3f}R")
    for k in (2, 3):
        emit(row_line(f"IS витрати ×{k}", T[f"d_cost{k}"][IS], day[IS]))
    pl = T["plac_r"] - T["last_r"]
    emit(row_line("IS плацебо (mark/last зсунуто на +7 діб)", pl[IS], day[IS]))
    emit(row_line("OOS плацебо", pl[OOS], day[OOS]))
    for yr in sorted({month_of(int(t))[:4] for t in mon}):
        m = np.array([k.startswith(yr) for k in mkey])
        emit(row_line(f"рік {yr}", d[m], day[m]))
    # without the top contributors: drop the 10 / 50 largest positive differences (IS)
    di = d[IS]
    order = np.argsort(-di)
    for k in (10, 50):
        keep = np.ones(di.size, bool)
        keep[order[:k]] = False
        emit(row_line(f"IS без топ-{k} угод", di[keep], day[IS][keep]))
    emit("")
    per_asset(T, d, IS, OOS)


def per_asset(T: dict[str, np.ndarray], d: np.ndarray, IS: np.ndarray, OOS: np.ndarray) -> None:
    emit("## per asset (IS)")
    syms = np.unique(T["sym"])
    means, ses, ns, names = [], [], [], []
    for s in syms:
        m = IS & (T["sym"] == s)
        if m.sum() < 20:
            continue
        x = d[m]
        means.append(x.mean())
        ses.append(max(x.std(ddof=1), 1e-9) / math.sqrt(x.size))
        ns.append(int(m.sum()))
        names.append(str(s))
    me, se = np.array(means), np.array(ses)
    nz = se > 1e-8
    q, df, i2 = cochran_q(me[nz], se[nz])
    emit(
        f"assets with >= 20 IS trades: {len(names)} ({int(nz.sum())} with a non-zero spread); Cochran Q {q:.1f}, df {df}, "
        f"p {chi2_sf(q, df):.3g}, I² {i2:.2f}"
    )
    top = np.argsort(-me)[:10]
    emit("top-10 IS assets by Δ: " + ", ".join(f"{names[i]} {me[i]:+.3f} (n {ns[i]})" for i in top))
    topset = {names[i] for i in top}
    in_top = np.isin(T["sym"], list(topset))
    emit(row_line("OOS top-10 IS assets", d[OOS & in_top], T["day"][OOS & in_top]))
    emit(row_line("OOS the rest", d[OOS & ~in_top], T["day"][OOS & ~in_top]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=("plan", "fetch", "count", "run", "report"))
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/mark_price_stop")
    ap.add_argument("--trades-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/demo_stop_management/trades")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, default=None, help="also write the output here (default: <cache-dir>/<stage>.txt)")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    out = a.out or a.cache_dir / f"{a.stage}.txt"
    with out.open("w") as fh:
        OUT["fh"] = fh
        emit(f"# verify_mark_stop {a.stage} {datetime.now(UTC).isoformat(timespec='seconds')}")
        if a.stage in ("plan", "fetch"):
            jobs = plan(a.trades_dir, a.cache_dir)
            emit(f"missing mark files: {len(jobs)}; symbols {len({j[0] for j in jobs})}")
            if a.stage == "fetch":
                asyncio.run(fetch_jobs(jobs[: a.limit] if a.limit else jobs))
        elif a.stage == "count":
            stage_count(a.trades_dir, a.cache_dir, a.s1_cache, a.data_dir, a.workers)
        elif a.stage == "run":
            stage_run(a.trades_dir, a.cache_dir, a.s1_cache, a.data_dir, a.workers, a.limit)
        else:
            stage_report(a.cache_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
