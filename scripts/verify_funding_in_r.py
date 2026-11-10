#!/usr/bin/env python3
"""Funding over the hold in R, all demo setups (IDEA-20261030-demo-setups-depth, item «funding за час утримання в R»;
registration «Попередня реєстрація — funding за час утримання в R»). Accounting, not a signal.

    uv run python scripts/verify_funding_in_r.py --stage coverage [--sample N]   # counts and file coverage only
    uv run python scripts/verify_funding_in_r.py --stage fetch [--sample N]      # missing monthly funding files
    uv run python scripts/verify_funding_in_r.py --stage report                  # the registered table (next run)

Trades: the passports' verdict samples of the 9 demo setups, 2021-01 .. 2026-09 (the same rows as
``verify_regime_side.py``: ``--passport-cache``, S12 v4 from ``--s12-cache``). Funding: Binance USD-M settlements from
the data.binance.vision monthly archive, read from the ``funding_crowding`` cache (2021-10 .. 2026-09) and from this
script's own cache (``--cache-dir``, months the first one lacks), fetched only through ``rt.binance_vision``
(``core.http.Http``). Output also goes to ``<cache-dir>/<stage>.txt``. No keys, no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_s12_modules as m12s  # noqa: E402
import verify_setup_passport as vsp  # noqa: E402
from common_helpers import rss_mb  # noqa: E402

from tradedesk.analytics import funding_in_r as fr  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics.stop_management import TAKER  # noqa: E402

DEMO = ("S14", "S16", "S3", "S12", "S38", "S39", "S40", "S28", "S17")  # demo_trader.SETUPS (S12 = v4, S17 = 1d)
FILL_ENTRY = ("S28", "S17")  # limits on the demo: the position exists from the fill (t_fill), not the placement
THRESHOLD = 0.02  # |mean funding| in R that makes the funding line material (card item)
SEED = 20261111
N_BOOT = 2000
MONTH_PAD_MS = fr.MAX_GAP_MS  # coverage needs the settlement before the entry and the one after the exit
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    if OUT.get("mute") is not None:  # smoke of the report stage: run every line, show none of the values
        OUT["mute"] += 1
        return
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


# ---------------------------------------------------------------- trades


def setup_trades(setup: str, a: argparse.Namespace, qv: dict[str, Any], tops: dict[int, dict[int, set[str]]]) -> dict[str, np.ndarray]:
    """The setup's verdict-sample trades 2021+ (as ``verify_regime_side.setup_rows`` 'main'): demo selection, the
    demo's previous-day list, the passport's chain (S38 cap 2 per bar), filled limits only."""
    if setup == "S12":
        T, _, _, _ = m12s.load(a.s12_cache / "S12")
        floor, n_top, cap, filled = 5e6, vsp.TOP_N, None, None
    else:
        vsp.OUT["tf"] = "1d" if setup == "S17" else None
        T = vsp.load_rows(a.passport_cache / setup)
        floor, n_top, cap = vsp.SETUPS[setup][2], vsp.top_n_of(setup), vsp.BAR_CAP.get(setup)
        filled = T["filled"] if setup in vsp.LIMIT else None
    if n_top not in tops:
        tops[n_top] = vsp.top_n_days(qv, n_top)
    base = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    base &= np.nan_to_num(T["turn"].astype(float), nan=-1) >= floor
    base &= vsp.in_prev_day_top(T, tops[n_top])
    V = vsp.chain(T, base & (T["t_in"] >= vsp.PASSPORT_MS), cap)
    if filled is not None:
        V &= filled
    entry = np.array([int(x) for x in T["t_fill"][V]], dtype=np.int64) if setup in FILL_ENTRY else T["t_in"][V].astype(np.int64)
    out = {
        "sym": T["sym"][V].astype(str),
        "side": T["side"][V].astype(str),
        "t_in": T["t_in"][V].astype(np.int64),
        "entry": entry,
        "exit": T["B_x"][V].astype(np.int64),
        "stop": T["stop_pct"][V].astype(float),
        "B": T["B"][V].astype(float),
    }
    ok = np.isfinite(out["B"]) & (out["exit"] > out["entry"]) & (out["stop"] > 0)
    if not ok.all():
        emit(f"  {setup}: rows dropped (no R, exit <= entry or stop <= 0): {int((~ok).sum())}")
        out = {k: v[ok] for k, v in out.items()}
    return out


def load_trades(a: argparse.Namespace) -> dict[str, dict[str, np.ndarray]]:
    qv = vsp.s1.daily_qv(a.s1_cache, a.data_dir, vsp.s1.eligible_syms(a.data_dir))
    tops: dict[int, dict[int, set[str]]] = {}
    res = {}
    for s in DEMO:
        X = setup_trades(s, a, qv, tops)
        if a.sample or a.symbols:
            pick = a.symbols.split(",") if a.symbols else sorted(set(X["sym"].tolist()))[: a.sample]
            keep = np.isin(X["sym"], pick)
            X = {k: v[keep] for k, v in X.items()}
        res[s] = X
        emit(f"loaded {s}: n {X['sym'].size}, rss {rss_mb():.0f} MB")
    return res


# ---------------------------------------------------------------- funding files


def month_key(t_ms: int) -> str:
    d = datetime.fromtimestamp(t_ms / 1000, UTC)
    return f"{d.year:04d}-{d.month:02d}"


def months_between(lo_ms: int, hi_ms: int) -> list[str]:
    a, b = month_key(lo_ms), month_key(hi_ms)
    y, m = map(int, a.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def needed(trades: dict[str, dict[str, np.ndarray]]) -> dict[str, set[str]]:
    need: dict[str, set[str]] = defaultdict(set)
    for X in trades.values():
        for s, e, x in zip(X["sym"], X["entry"], X["exit"], strict=True):
            need[str(s)].update(months_between(int(e) - MONTH_PAD_MS, int(x) + MONTH_PAD_MS))
    return need


def fpaths(a: argparse.Namespace, sym: str, mo: str) -> tuple[Path, Path]:
    return a.cache_dir / "funding" / sym / f"{mo}.json", a.data_dir / "funding" / sym / f"{mo}.json"


def file_state(a: argparse.Namespace, sym: str, mo: str) -> str:
    """own | shared (file with rows), absent (cached 404 / null in both), missing (not fetched anywhere)."""
    seen_null = False
    for p, lab in zip(fpaths(a, sym, mo), ("own", "shared"), strict=True):
        if p.exists():
            if json.loads(p.read_text()):
                return lab
            seen_null = True
    return "absent" if seen_null else "missing"


def load_funding(a: argparse.Namespace, sym: str, months: set[str]) -> tuple[np.ndarray, np.ndarray]:
    rows: dict[int, float] = {}
    for mo in sorted(months):
        for p in fpaths(a, sym, mo):
            if p.exists():
                d = json.loads(p.read_text())
                if d:
                    for r in d:
                        rows[int(r[0])] = float(r[2])
                    break
    t = np.array(sorted(rows), dtype=np.int64)
    return t, np.array([rows[int(x)] for x in t], dtype=float)


# ---------------------------------------------------------------- stages


def stage_coverage(a: argparse.Namespace) -> None:
    """Counts and coverage only: no funding values, no means."""
    trades = load_trades(a)
    emit("\n## Вибірка (2021-01 … 2026-09, вибірки вердиктів паспортів; лімітки — від заповнення)")
    emit("| сетап | n | IS | OOS | лонги | шорти | медіана утримання, діб | p95 утримання, діб |\n|---|---|---|---|---|---|---|---|")
    for s, X in trades.items():
        oos = X["t_in"] >= vsp.OOS_MS
        hold = (X["exit"] - X["entry"]) / sp.DAY_MS
        q = np.quantile(hold, [0.5, 0.95]) if hold.size else [math.nan, math.nan]
        emit(
            f"| {s} | {X['sym'].size} | {int((~oos).sum())} | {int(oos.sum())} | {int((X['side'] == 'long').sum())} | "
            f"{int((X['side'] == 'short').sum())} | {q[0]:.2f} | {q[1]:.2f} |"
        )
    need = needed(trades)
    st: Counter[str] = Counter()
    per_sym: dict[str, Counter[str]] = {}
    for sym, mos in need.items():
        c = Counter(file_state(a, sym, mo) for mo in mos)
        per_sym[sym] = c
        st.update(c)
    emit(f"\nсимволів {len(need)}, місяце-файлів потрібно {sum(len(m) for m in need.values())}: {dict(st)}")
    by_month: Counter[str] = Counter()
    for sym, mos in need.items():
        for mo in mos:
            if file_state(a, sym, mo) == "missing":
                by_month[mo[:4]] += 1
    emit(f"не завантажено (missing) за роками: {dict(sorted(by_month.items()))}")
    # trade-level coverage from what is on disk now (series coverage, not values)
    emit("\n## Покриття угод рядом funding (за тим, що вже на диску)")
    emit("| сетап | n | покрито | частка | без жодного файлу |\n|---|---|---|---|---|")
    series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for s, X in trades.items():
        cov = np.zeros(X["sym"].size, dtype=bool)
        for i, (sym, e, x) in enumerate(zip(X["sym"], X["entry"], X["exit"], strict=True)):
            sym = str(sym)
            if sym not in series:
                series[sym] = load_funding(a, sym, need[sym])
            t, r = series[sym]
            cov[i] = fr.window(t, r, int(e), int(x)).covered
        none = np.array([series[str(sy)][0].size == 0 for sy in X["sym"]], dtype=bool)
        emit(f"| {s} | {cov.size} | {int(cov.sum())} | {cov.mean() if cov.size else math.nan:.3f} | {int(none.sum())} |")
    iv: Counter[int] = Counter()
    for t, _ in series.values():
        if t.size > 1:
            d = np.diff(fr.floor_minute(t)) // fr.HOUR_MS
            iv.update(int(x) for x in d[d <= 24])
    emit(f"\nінтервали між розрахунками, год (кількість): {dict(sorted(iv.items()))}")
    t, r = load_funding(a, "FTTUSDT", {"2022-11"})  # known number (funding_crowding report): 126 settlements, min -0.0250
    emit(f"звірка FTTUSDT 2022-11: {t.size} розрахунків, мін. ставка {r.min() if r.size else math.nan:.4f} (очікувано 126, -0.0250)")
    emit(f"rss {rss_mb():.0f} MB")


async def _fetch(a: argparse.Namespace, jobs: list[tuple[str, str]]) -> None:
    from verify_funding_crowding import Fetcher

    from tradedesk.server import build_server

    _, rt = build_server()
    fx = Fetcher(a.cache_dir)
    try:
        calls = []
        for sym, mo in jobs:
            y, m = map(int, mo.split("-"))

            async def go(s: str = sym, y: int = y, m: int = m) -> Any:
                return (await rt.binance_vision.um_funding_monthly(s, y, m)).data

            calls.append((fpaths(a, sym, mo)[0], go))
        await fx.many("funding", calls)
        emit(f"fetch errors: {dict(fx.errors)}")
    finally:
        await rt.aclose()


def stage_fetch(a: argparse.Namespace) -> None:
    trades = load_trades(a)
    need = needed(trades)
    jobs = sorted((sym, mo) for sym, mos in need.items() for mo in mos if file_state(a, sym, mo) == "missing")
    emit(f"місяце-файлів до завантаження: {len(jobs)} (символів {len({s for s, _ in jobs})})")
    if jobs:
        asyncio.run(_fetch(a, jobs))
    emit(f"rss {rss_mb():.0f} MB")


def fmt(r: dict[str, float]) -> str:
    if not r["n"]:
        return "0 | — | — | — | — | — | —"
    lo = "—" if math.isnan(r.get("lo", math.nan)) else f"{r['lo']:+.4f}"
    hi = "—" if math.isnan(r.get("hi", math.nan)) else f"{r['hi']:+.4f}"
    return f"{int(r['n'])} | {r['m']:+.4f} | [{lo}; {hi}] | {r['p5']:+.4f} | {r['p50']:+.4f} | {r['p95']:+.4f} | {r['paid']:.2f}"


def stat(v: np.ndarray, week: np.ndarray) -> dict[str, float]:
    r = fr.summarize(v)
    if v.size >= 2:
        c = sp.mean_ci(v, week, n_boot=N_BOOT, seed=SEED)
        r.update(lo=c["lo"], hi=c["hi"])
    return r


def stage_report(a: argparse.Namespace) -> None:
    """The registered table: funding in R per setup x side (run only after the registration commit)."""
    trades = load_trades(a)
    need = needed(trades)
    series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    rows: dict[str, dict[str, np.ndarray]] = {}
    for s, X in trades.items():
        n = X["sym"].size
        f, fpu, finc, tot, nset = (np.full(n, math.nan) for _ in range(5))
        cov = np.zeros(n, dtype=bool)
        for i in range(n):
            sym = str(X["sym"][i])
            if sym not in series:
                series[sym] = load_funding(a, sym, need[sym])
            t, r = series[sym]
            e, x, side, stop = int(X["entry"][i]), int(X["exit"][i]), str(X["side"][i]), float(X["stop"][i])
            w = fr.window(t, r, e, x)
            cov[i] = w.covered
            if not w.covered:
                continue
            tot[i], nset[i] = w.total, w.n
            f[i] = fr.funding_r(w.total, side, stop)
            fpu[i] = fr.funding_r_passport_unit(w.total, side, stop, TAKER)
            finc[i] = fr.funding_r(fr.window(t, r, e, x, inclusive=True).total, side, stop)
        rows[s] = {**X, "f": f, "fpu": fpu, "finc": finc, "tot": tot, "nset": nset, "cov": cov, "week": X["t_in"] // sp.WEEK_MS}
        emit(f"computed {s}: covered {int(cov.sum())}/{n}, rss {rss_mb():.0f} MB")
    if a.mute_values:
        OUT["mute"] = 0
    hdr = "| n | середнє | 95 % CI (тижні) | p5 | p50 | p95 | частка «платила» |"
    emit("\n## Основна таблиця: funding у R (+ отримано, − сплачено), покриті угоди 2021-01 … 2026-09")
    emit(f"| сетап | бік {hdr}\n|---|---|---|---|---|---|---|---|---|")
    verdict = {}
    for s, R in rows.items():
        c = R["cov"]
        for lab, m in (("обидва", c), ("лонги", c & (R["side"] == "long")), ("шорти", c & (R["side"] == "short"))):
            if m.sum() == 0:
                continue
            r = stat(R["f"][m], R["week"][m])
            emit(f"| {s} | {lab} | {fmt(r)} |")
            if lab == "обидва":
                verdict[s] = r["m"]
    emit("\n## Правило рішення (|середнє| ≥ 0.02R на рівні сетапу)")
    for s, m in verdict.items():
        emit(f"{s}: {m:+.4f} R -> {'ІСТОТНО' if fr.material(m, THRESHOLD) else 'неістотно'}")
    emit("\n## Чутливість (описово): одиниця R паспорта; межі включно; IS / OOS; роки; середні утримання й розрахунків")
    emit("| сетап | R паспорта | межі включно | IS | OOS | розрахунків на угоду | Σ ставок на угоду, % |\n|---|---|---|---|---|---|---|")
    for s, R in rows.items():
        c = R["cov"]
        oos = R["t_in"] >= vsp.OOS_MS
        emit(
            f"| {s} | {np.nanmean(R['fpu'][c]):+.4f} | {np.nanmean(R['finc'][c]):+.4f} | {np.nanmean(R['f'][c & ~oos]):+.4f} | "
            f"{np.nanmean(R['f'][c & oos]):+.4f} | {np.nanmean(R['nset'][c]):.1f} | {100 * np.nanmean(R['tot'][c]):+.4f} |"
        )
    emit("\nза роками (середнє f, обидва боки): ")
    for s, R in rows.items():
        yr = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in R["t_in"]])
        emit(f"  {s}: " + ", ".join(f"{y} {np.nanmean(R['f'][R['cov'] & (yr == y)]):+.4f}" for y in sorted(set(yr.tolist()))))
    emit("\n## Внесок монет: без топ-10 символів за |Σ f|; R паспорта без і з funding (контекст)")
    emit("| сетап | частка модуля Σ f у топ-10 | середнє без топ-10 | E[B] | E[B + f] | непокриті | межа (непокриті ±0.01 %/8 год) |")
    emit("|---|---|---|---|---|---|---|")
    for s, R in rows.items():
        c = R["cov"]
        agg: dict[str, float] = defaultdict(float)
        for sy, v in zip(R["sym"][c], R["f"][c], strict=True):
            agg[str(sy)] += float(v)
        top = sorted(agg, key=lambda k: -abs(agg[k]))[:10]
        share = sum(abs(agg[k]) for k in top) / max(1e-12, sum(abs(v) for v in agg.values()))
        rest = c & ~np.isin(R["sym"], top)
        unc = ~c
        flat = 0.0001 * (R["exit"][unc] - R["entry"][unc]) / (8 * fr.HOUR_MS) / R["stop"][unc]  # |flat| per uncovered trade
        n = c.size
        bound = (np.nansum(R["f"][c]) + np.array([-1, 1]) * flat.sum()) / n if n else [math.nan, math.nan]
        emit(
            f"| {s} | {share:.2f} | {np.nanmean(R['f'][rest]) if rest.any() else math.nan:+.4f} | {np.mean(R['B'][c]):+.4f} | "
            f"{np.mean(R['B'][c] + R['f'][c]):+.4f} | {int(unc.sum())} | [{bound[0]:+.4f}; {bound[1]:+.4f}] |"
        )
    if OUT.get("mute") is not None:
        k, OUT["mute"] = OUT["mute"], None
        emit(f"--mute-values: {k} report lines computed and not shown")
    emit(f"rss {rss_mb():.0f} MB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("coverage", "fetch", "report"), required=True)
    ap.add_argument("--sample", type=int, default=0, help="first N symbols of each setup (smoke)")
    ap.add_argument("--symbols", default="", help="comma-separated symbols only (smoke), e.g. BTCUSDT,ETHUSDT")
    ap.add_argument("--mute-values", action="store_true", help="report stage: compute, but print no value (smoke)")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_in_r")
    ap.add_argument("--passport-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s12-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/s12_modules")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    a = ap.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    name = a.stage + (f"_sample{a.sample}" if a.sample else "") + ("_symbols" if a.symbols else "")
    with (a.cache_dir / f"{name}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"verify_funding_in_r --stage {a.stage} --sample {a.sample} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        {"coverage": stage_coverage, "fetch": stage_fetch, "report": stage_report}[a.stage](a)
        emit(f"peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
