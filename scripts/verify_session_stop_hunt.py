"""Session stop hunt (IDEA-20261016-session-stop-hunt, setup S25) on Binance USD-M perps, 5m, 2021-01 .. 2026-09.

Rules are fixed in the idea card; the pre-registration there fixes every detail the card leaves open. Stages:
  uv run python scripts/verify_session_stop_hunt.py --stage fetch  --out ~/.cache/tradedesk-agents/session_stop_hunt/fetch.log
  uv run python scripts/verify_session_stop_hunt.py --stage count  --out ~/.cache/tradedesk-agents/session_stop_hunt/stage0.md
  uv run python scripts/verify_session_stop_hunt.py --stage report --out ~/.cache/tradedesk-agents/session_stop_hunt/report.md
``count`` (stage 0) counts setups and trades per asset without computing any trade outcome. Data: monthly 5m kline
zips from data.binance.vision via rt.binance_vision, one JSON per symbol-month under --cache-dir. No keys, no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci, without_top_days
from tradedesk.analytics.session_stop_hunt import (
    BAR_MS,
    Bars,
    Session,
    Trade,
    cluster_bootstrap_diff,
    design_effect,
    find_setup,
    london_session,
    new_york_session,
    simulate,
    target_for,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

CORE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
EXTRA = ["BNBUSDT", "XRPUSDT", "DOGEUSDT"]
FIRST_DAY = date(2021, 1, 1)
LAST_DAY = date(2026, 9, 30)
SPLIT_DAY = date(2024, 1, 1)
RHO = 0.7  # cross-asset correlation assumed by the card's power section
PLACEBO_SHIFTS = (6, 12, 18)
COMBOS = [("E1", "T1"), ("E1", "T2"), ("E2", "T1"), ("E2", "T2")]
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    """Print and, with --out, append to the output file at once (a background run can be followed there)."""
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def months() -> list[tuple[int, int]]:
    out, y, m = [], 2020, 12  # December 2020 gives the first day's fractal lookback
    while (y, m) <= (2026, 10):  # October 2026 (if present) closes the +18 h placebo of the last day
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------------------------------------------------------------- fetch


async def fetch(cache: Path, syms: list[str]) -> None:
    _, rt = build_server()
    sem = asyncio.Semaphore(8)
    errors: Counter[str] = Counter()
    done = 0
    jobs = [(s, y, m) for s in syms for y, m in months() if not (cache / "k5m" / s / f"{y:04d}-{m:02d}.json").exists()]
    emit(f"[fetch] {len(syms) * len(months())} files, {len(jobs)} to fetch")
    t0 = time.time()

    async def one(s: str, y: int, m: int) -> None:
        nonlocal done
        p = cache / "k5m" / s / f"{y:04d}-{m:02d}.json"
        async with sem:
            try:
                rows: Any = [r[:5] for r in (await rt.binance_vision.um_klines_monthly(s, "5m", y, m)).data]
            except ProviderError as e:
                if e.reason != "data_unavailable":
                    errors[e.reason] += 1
                    return  # transient: a rerun retries
                rows = None
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, separators=(",", ":")))
        tmp.replace(p)
        done += 1
        if done % 20 == 0:
            el = time.time() - t0
            emit(f"  [fetch] {done}/{len(jobs)} in {el:.0f}s, eta {el / done * (len(jobs) - done):.0f}s, rss {rss_mb():.0f} MB")

    try:
        await asyncio.gather(*[one(*j) for j in jobs])
    finally:
        await rt.aclose()
    emit(f"[fetch] done in {time.time() - t0:.0f}s; transient errors: {dict(errors)}; rss {rss_mb():.0f} MB")


def load(cache: Path, sym: str) -> Bars:
    rows: list[list[float]] = []
    for y, m in months():
        p = cache / "k5m" / sym / f"{y:04d}-{m:02d}.json"
        if p.exists():
            rows.extend(json.loads(p.read_text()) or [])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 5))
    if a.size:
        a = a[np.argsort(a[:, 0], kind="stable")]
        a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
    return Bars.from_rows(a)


def days() -> list[date]:
    n = (LAST_DAY - FIRST_DAY).days + 1
    return [FIRST_DAY + timedelta(days=i) for i in range(n)]


# ---------------------------------------------------------------- per-asset run


def run_asset(bars: Bars, sym: str, anchor: str, sessions: list[Session], *, outcomes: bool) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Setups (and with ``outcomes`` the four trade variants) for one asset and one anchor."""
    reasons: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for s in sessions:
        st, b, why = find_setup(bars, s)
        reasons[why] += 1
        if st is None or b is None:
            continue
        for ek, tk in COMBOS:
            base = {"sym": sym, "anchor": anchor, "day": s.day, "side": "long" if st.side > 0 else "short", "entry": ek, "target": tk}
            if not outcomes:
                # stage 0: whether the variant would place a trade, decided from prices known at the BOS only
                lvl = st.entry if ek == "E1" else 0.5 * (st.manip_start + st.extreme)
                ok = target_for(tk, st, lvl) is not None
                reasons[f"{ek}{tk}:{'eligible' if ok else 't1_lt_1r'}"] += 1
                if ok and ek == "E1" and tk == "T1":
                    rows.append({**base, "stop_pct": (st.stop - st.entry) / abs(st.entry)})
                continue
            tr, why2 = simulate(st, b, ek, tk)
            reasons[f"{ek}{tk}:{why2}"] += 1
            if tr is None:
                continue
            rows.append({**base, **trade_row(tr)})
    return rows, reasons


def trade_row(t: Trade) -> dict[str, Any]:
    return {
        "r_gross": t.r_gross,
        "cost_r": t.cost_r,
        "r_net": t.r_net,
        "exit_kind": t.exit_kind,
        "both": t.both_touched,
        "stop_pct": t.stop_pct,
        "hold_bars": t.exit_idx - t.entry_idx,
    }


def anchors() -> dict[str, list[Session]]:
    ds = days()
    out = {"london": [london_session(d) for d in ds]}
    for sh in PLACEBO_SHIFTS:
        out[f"placebo+{sh}h"] = [london_session(d, sh) for d in ds]
    out["ny"] = [new_york_session(d) for d in ds]
    return out


# ---------------------------------------------------------------- sanity


def sanity(cache: Path, syms: list[str]) -> None:
    emit("## Санітарні перевірки")
    for s in syms:
        b = load(cache, s)
        if b.t.size == 0:
            emit(f"- {s}: немає даних")
            continue
        dt = np.diff(b.t)
        gaps = int(np.sum(dt != BAR_MS))
        big = sorted(((int(d) // BAR_MS - 1, int(b.t[i])) for i, d in enumerate(dt) if d != BAR_MS), reverse=True)[:3]
        first = datetime.fromtimestamp(b.t[0] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
        last = datetime.fromtimestamp(b.t[-1] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
        bad = int(np.sum((b.h < b.low) | (b.h < np.maximum(b.o, b.c)) | (b.low > np.minimum(b.o, b.c))))
        big_s = ", ".join(f"{n} бар з {datetime.fromtimestamp(t / 1000, tz=UTC):%Y-%m-%d %H:%M}" for n, t in big)
        emit(f"- {s}: {b.t.size} барів {first} … {last}; розривів {gaps} (найбільші: {big_s or '—'}); некоректних OHLC {bad}")
        for probe in ("2021-01-01T00:00", "2024-01-01T00:00", "2026-09-30T23:55"):
            ts = int(datetime.fromisoformat(probe).replace(tzinfo=UTC).timestamp() * 1000)
            i = int(np.searchsorted(b.t, ts))
            if i < b.t.size and b.t[i] == ts:
                emit(f"  - {probe} UTC: open {b.o[i]:g}, close {b.c[i]:g}")
        del b
    emit(f"- пікова RSS {rss_mb():.0f} MB")


# ---------------------------------------------------------------- stats helpers


def fmt_ci(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def summary(rows: list[dict[str, Any]], key: str = "r_net", n_boot: int = 5000) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    v = [r[key] for r in rows]
    d = [r["day"] for r in rows]
    return {
        "n": len(v),
        "days": len(set(d)),
        "win": sum(1 for r in rows if r["r_gross"] > 0) / len(v),
        "mean": statistics.fmean(v),
        "ci": cluster_bootstrap_ci(v, d, n_boot=n_boot) if len(set(d)) > 1 else None,
    }


def line(label: str, rows: list[dict[str, Any]], n_boot: int = 5000) -> str:
    g = summary(rows, "r_gross", n_boot)
    if g["n"] == 0:
        return f"| {label} | 0 | — | — | — | — | — |"
    n = summary(rows, "r_net", n_boot)
    return f"| {label} | {n['n']} | {n['win']:.1%} | {g['mean']:+.3f} | {fmt_ci(g['ci'])} | {n['mean']:+.3f} | {fmt_ci(n['ci'])} |"


HDR = "| вибірка | n | частка R_gross>0 | E[R] gross | 95 % CI gross | E[R] net | 95 % CI net |\n|---|---|---|---|---|---|---|"


def q(xs: list[float], p: float) -> float:
    return float(np.quantile(np.asarray(xs), p)) if xs else float("nan")


# ---------------------------------------------------------------- stages


def stage_count(cache: Path, syms: list[str]) -> None:
    emit("# Стадія 0: частота подій (без результатів угод)")
    sanity(cache, syms)
    sess = anchors()["london"]
    per_day: dict[str, int] = defaultdict(int)
    total = 0
    emit(
        "\n| актив | днів | gap | no_sweep | both_first | no_fractal | no_bos_60m | new_extreme_after_window | сетапів (BOS) | E1×T1 угод | T1<1R |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    stops: list[float] = []
    for s in syms:
        b = load(cache, s)
        rows, rs = run_asset(b, s, "london", sess, outcomes=False)
        del b
        e1t1 = [r for r in rows if r["entry"] == "E1"]
        for r in e1t1:
            per_day[r["day"]] += 1
            stops.append(r["stop_pct"])
        total += len(e1t1)
        emit(
            f"| {s} | {len(sess)} | {rs['gap']} | {rs['no_sweep']} | {rs['both_first']} | {rs['no_fractal']} | {rs['no_bos_60m']} | "
            f"{rs['new_extreme_after_window']} | {rs['ok']} | {len(e1t1)} | {rs['E1T1:t1_lt_1r']} |"
        )
        emit(f"  (rss {rss_mb():.0f} MB)")
    sizes = list(per_day.values())
    deff = design_effect(sizes, RHO)
    emit(
        f"\n- угод E1×T1 разом: {total}; днів з угодою: {len(sizes)}; середня кількість угод на такий день {total / max(1, len(sizes)):.2f}"
    )
    emit(f"- ефект дизайну 1+(m−1)·ρ, ρ={RHO} (з картки), m зважене: {deff:.2f}; ефективне n ≈ {total / deff:.0f}")
    if stops:
        emit(f"- ширина стопу E1, % ціни: p10 {q(stops, 0.1):.2%}, медіана {q(stops, 0.5):.2%}, p90 {q(stops, 0.9):.2%}")


def collect(cache: Path, syms: list[str]) -> tuple[list[dict[str, Any]], dict[tuple[str, str], Counter[str]]]:
    rows: list[dict[str, Any]] = []
    reasons: dict[tuple[str, str], Counter[str]] = {}
    an = anchors()
    for s in syms:
        t0 = time.time()
        b = load(cache, s)
        for name, sess in an.items():
            r, rs = run_asset(b, s, name, sess, outcomes=True)
            rows.extend(r)
            reasons[(s, name)] = rs
        del b
        emit(f"  [run] {s} in {time.time() - t0:.0f}s, rows {len(rows)}, rss {rss_mb():.0f} MB")
    return rows, reasons


def sel(rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return [r for r in rows if all((r[k] in v) if isinstance(v, (set, tuple, list)) else r[k] == v for k, v in kw.items())]


def stage_report(cache: Path, syms: list[str]) -> None:
    rows, reasons = collect(cache, syms)
    (cache / "trades.json").write_text(json.dumps(rows))
    lon = sel(rows, anchor="london")
    main = sel(lon, entry="E1", target="T1")
    emit("# Session stop hunt: результати")
    emit(f"Активи: {', '.join(syms)}; дні {FIRST_DAY} … {LAST_DAY}; R у частках ризику (стоп − вхід), net = після витрат.")

    emit("\n## Основна комбінація E1 × T1 (Лондон)")
    emit(HDR)
    emit(line("усі активи", main, 10000))
    emit(line("BTC+ETH+SOL (список картки)", sel(main, sym=CORE)))
    for s in syms:
        emit(line(s, sel(main, sym=s)))
    emit(line("2021–2023", [r for r in main if r["day"] < SPLIT_DAY.isoformat()]))
    emit(line("2024–2026", [r for r in main if r["day"] >= SPLIT_DAY.isoformat()]))
    emit(line("лонг", sel(main, side="long")))
    emit(line("шорт", sel(main, side="short")))
    for k in (5, 10, 20):
        v, d, _ = without_top_days([r["r_net"] for r in main], [r["day"] for r in main], k)
        ci = cluster_bootstrap_ci(v, d, n_boot=5000)
        emit(f"| без {k} найкращих днів (net) | {len(v)} | — | — | — | {statistics.fmean(v):+.3f} | {fmt_ci(ci)} |")
    for mult in (0.5, 1.5, 2.0):
        v = [r["r_gross"] - mult * r["cost_r"] for r in main]
        ci = cluster_bootstrap_ci(v, [r["day"] for r in main], n_boot=5000)
        emit(f"| витрати ×{mult:g} | {len(v)} | — | — | — | {statistics.fmean(v):+.3f} | {fmt_ci(ci)} |")

    if main:
        st = [r["stop_pct"] for r in main]
        rn = [r["r_net"] for r in main]
        cr = [r["cost_r"] for r in main]
        ek = Counter(r["exit_kind"] for r in main)
        emit(
            f"\n- ширина стопу, % ціни: p10 {q(st, 0.1):.2%}, p25 {q(st, 0.25):.2%}, медіана {q(st, 0.5):.2%}, p75 {q(st, 0.75):.2%}, p90 {q(st, 0.9):.2%}"
        )
        emit(f"- витрати в R: медіана {q(cr, 0.5):.3f}, середнє {statistics.fmean(cr):.3f}, p90 {q(cr, 0.9):.3f}")
        emit(
            f"- розподіл R net: p5 {q(rn, 0.05):+.2f}, p25 {q(rn, 0.25):+.2f}, медіана {q(rn, 0.5):+.2f}, p75 {q(rn, 0.75):+.2f}, p95 {q(rn, 0.95):+.2f}, SD {statistics.pstdev(rn):.2f}"
        )
        emit(f"- виходи: {dict(ek)}")
        both = sum(1 for r in main if r["both"])
        emit(f"- свічки, що зачепили і стоп, і ціль (рахуються стопом): {both} з {len(main)} = {both / len(main):.1%}")
        per_day = Counter(r["day"] for r in main)
        deff = design_effect(list(per_day.values()), RHO)
        sd = statistics.pstdev(rn)
        emit(f"- ефективне n (ρ={RHO}): {len(main) / deff:.0f}; MDE ≈ 2.8·SD/√n_eff = {2.8 * sd / (len(main) / deff) ** 0.5:.3f}R")
        bins = [-10, -1.5, -1.0, -0.5, 0, 0.5, 1, 2, 3, 100]
        h, _ = np.histogram(rn, bins=bins)
        emit("- гістограма R net: " + ", ".join(f"[{bins[i]:g}, {bins[i + 1]:g}): {int(h[i])}" for i in range(len(h))))

    emit("\n## Усі комбінації входу й цілі (Лондон, для стійкості)")
    emit(HDR)
    for ek, tk in COMBOS:
        sub = sel(lon, entry=ek, target=tk)
        emit(line(f"{ek}×{tk}", sub))
        for s in syms:
            emit(line(f"  {ek}×{tk} {s}", sel(sub, sym=s)))
        emit(line(f"  {ek}×{tk} 2021–2023", [r for r in sub if r["day"] < SPLIT_DAY.isoformat()]))
        emit(line(f"  {ek}×{tk} 2024–2026", [r for r in sub if r["day"] >= SPLIT_DAY.isoformat()]))
        emit(line(f"  {ek}×{tk} лонг", sel(sub, side="long")))
        emit(line(f"  {ek}×{tk} шорт", sel(sub, side="short")))
        both = sum(1 for r in sub if r["both"])
        emit(f"|  {ek}×{tk}: обидва торкання {both}/{len(sub)} | | | | | | |")

    emit("\n## Лондон проти плацебо (E1 × T1, net)")
    emit(HDR)
    emit(line("Лондон", main))
    pl_all: list[dict[str, Any]] = []
    for sh in PLACEBO_SHIFTS:
        p = sel(rows, anchor=f"placebo+{sh}h", entry="E1", target="T1")
        pl_all.extend(p)
        emit(line(f"плацебо +{sh} год", p))
    emit(line("плацебо разом", pl_all))
    emit("\n| порівняння | різниця E[R] net | 95 % CI | p (різниця>0) |\n|---|---|---|---|")
    d = cluster_bootstrap_diff(
        [r["r_net"] for r in main], [r["day"] for r in main], [r["r_net"] for r in pl_all], [r["day"] for r in pl_all]
    )
    emit(f"| Лондон − середнє плацебо | {d['diff']:+.3f} | {fmt_ci(d['ci'])} | {d['p']:.3f} |")
    mc, pc = sel(main, sym=CORE), sel(pl_all, sym=CORE)
    if mc and pc:
        d = cluster_bootstrap_diff([r["r_net"] for r in mc], [r["day"] for r in mc], [r["r_net"] for r in pc], [r["day"] for r in pc])
        emit(f"| Лондон − плацебо, лише BTC+ETH+SOL | {d['diff']:+.3f} | {fmt_ci(d['ci'])} | {d['p']:.3f} |")
    for sh in PLACEBO_SHIFTS:
        p = sel(rows, anchor=f"placebo+{sh}h", entry="E1", target="T1")
        d = cluster_bootstrap_diff([r["r_net"] for r in main], [r["day"] for r in main], [r["r_net"] for r in p], [r["day"] for r in p])
        emit(f"| Лондон − плацебо +{sh} год | {d['diff']:+.3f} | {fmt_ci(d['ci'])} | {d['p']:.3f} |")
    # placebo per combo, for robustness
    emit("\n| комбінація | Лондон net | плацебо разом net | різниця | 95 % CI |\n|---|---|---|---|---|")
    for ek, tk in COMBOS:
        a = sel(lon, entry=ek, target=tk)
        p = [r for r in rows if r["anchor"].startswith("placebo") and r["entry"] == ek and r["target"] == tk]
        if a and p:
            d = cluster_bootstrap_diff(
                [r["r_net"] for r in a], [r["day"] for r in a], [r["r_net"] for r in p], [r["day"] for r in p], n_boot=5000
            )
            emit(
                f"| {ek}×{tk} | {statistics.fmean(r['r_net'] for r in a):+.3f} | {statistics.fmean(r['r_net'] for r in p):+.3f} | {d['diff']:+.3f} | {fmt_ci(d['ci'])} |"
            )

    emit("\n## H1b: лондонський рейндж, свип на відкритті Нью-Йорка (другорядна, лише звіт)")
    emit(HDR)
    ny = sel(rows, anchor="ny")
    for ek, tk in COMBOS:
        sub = sel(ny, entry=ek, target=tk)
        emit(line(f"{ek}×{tk}", sub))
    sub = sel(ny, entry="E1", target="T1")
    for s in syms:
        emit(line(f"  E1×T1 {s}", sel(sub, sym=s)))
    emit(line("  E1×T1 2021–2023", [r for r in sub if r["day"] < SPLIT_DAY.isoformat()]))
    emit(line("  E1×T1 2024–2026", [r for r in sub if r["day"] >= SPLIT_DAY.isoformat()]))

    emit("\n## Лічильники відсіву (Лондон)")
    emit(
        "| актив | "
        + " | ".join(
            [
                "gap",
                "no_sweep",
                "both_first",
                "no_fractal",
                "no_bos_60m",
                "new_extreme_after_window",
                "ok",
                "E1T1 t1_lt_1r",
                "E2T1 e2_unfilled",
            ]
        )
        + " |"
    )
    emit("|---" * 10 + "|")
    for s in syms:
        rs = reasons[(s, "london")]
        vals = [
            rs[k]
            for k in (
                "gap",
                "no_sweep",
                "both_first",
                "no_fractal",
                "no_bos_60m",
                "new_extreme_after_window",
                "ok",
                "E1T1:t1_lt_1r",
                "E2T1:e2_unfilled",
            )
        ]
        emit(f"| {s} | " + " | ".join(str(v) for v in vals) + " |")
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/session_stop_hunt")
    ap.add_argument("--stage", choices=("fetch", "count", "report"), required=True)
    ap.add_argument("--symbols", default=",".join(CORE), help="comma-separated Binance USD-M symbols")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    if a.stage == "fetch":
        asyncio.run(fetch(cache, syms))
    elif a.stage == "count":
        stage_count(cache, syms)
    else:
        stage_report(cache, syms)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")


if __name__ == "__main__":
    sys.exit(main())
