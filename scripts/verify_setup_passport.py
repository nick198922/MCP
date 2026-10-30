#!/usr/bin/env python3
"""Setup passport (IDEA-20261030-demo-setups-depth, items 2-5): the demo setup's trades on history with the demo's
rules and costs, as the reference its live trades are compared with. Pre-registration: the card's section
«Попередня реєстрація паспорта».

    uv run python scripts/verify_setup_passport.py S3 --stage sanity        # coverage, units, known number, timing
    uv run python scripts/verify_setup_passport.py S3 --stage run           # per-symbol candidate files (4 workers)
    uv run python scripts/verify_setup_passport.py S3 --stage report        # tables (also <cache>/<setup>/report.txt)

No network: 1h Binance USD-M bars and the point-in-time top-30 universe from the ``course_s14_ema`` cache
(``--s1-cache``), listing / exchange info / daily candles from ``funding_crowding`` (``--data-dir``), BTC cycle-regime
inputs from ``cycle_regime`` (``--regime-cache``), all filled earlier through the runtime providers.

Every candidate signal is simulated once (rule B and Bx, stress costs, late entry) and written to
``<cache>/<setup>/cands/<symbol>.json``; the report applies the demo's selection, the turnover groups and the
one-trade-per-symbol chain afterwards, so every population comes from the same files.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (loaders, universe, listing)

from tradedesk.analytics import cycle_regime as cr  # noqa: E402
from tradedesk.analytics import cycle_regime_eval as cre  # noqa: E402
from tradedesk.analytics import setup_passport as sp  # noqa: E402
from tradedesk.analytics import setup_signals as ss  # noqa: E402
from tradedesk.analytics.course_s14_ema import HOUR_MS, Bars, resample  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.market_flow import points  # noqa: E402
from tradedesk.analytics.stop_management import Fees, StopRule, chi2_sf, cochran_q, simulate_stop_rule  # noqa: E402
from tradedesk.execution.orders import reward_risk_at  # noqa: E402  (read-only: the guard's R:R after fees)

DAY_MS = 24 * HOUR_MS
SEED = 20261030
N_BOOT = 10_000
START_MS = s1.ms("2020-02-01T00:00")  # candidates from here (the known-number check); the passport starts in 2021
PASSPORT_MS = s1.ms("2021-01-01T00:00")
OOS_MS = s1.ms("2025-03-01T00:00")
END = {"4h": s1.ms("2026-09-23T00:00"), "1d": s1.ms("2026-08-31T00:00")}
HOURS = {"4h": 4, "1d": 24}
HOLD = {"4h": 7 * DAY_MS, "1d": 30 * DAY_MS}  # demo_trader.MAX_HOLD_MS
MIN_RR, MAX_STOP, MIN_SCORE = 1.5, 0.10, 2  # guard.min_reward_risk, guard.max_stop_pct, demo_trader.MIN_SCORE
RULE_B, RULE_BX = StopRule("B", be_frac=0.5), StopRule("Bx", be_frac=0.5, be_same_bar=True)
F1, F15 = Fees(), Fees().scaled(1.5)
BTC = "BTCUSDT"
TOP_N = 100  # demo_trader scans guard.universe(size=100, min_turnover_usd=5e6): top-100 by turnover AND >= $5M


def _only(name: str, fn: Callable[..., list[ss.LiveSignal]]) -> Callable[[Bars, str], list[ss.LiveSignal]]:
    return lambda b, tf: [s for s in fn(b, tf) if s.setup == name]


# setup -> (full-history detector, timeframes, primary turnover floor in USD: the demo's scan list)
SETUPS: dict[str, tuple[Callable[[Bars, str], list[ss.LiveSignal]], tuple[str, ...], float]] = {
    "S3": (_only("S3", ss.range_all), ("4h", "1d"), 5e6),
    "S16": (_only("S16", ss.range_all), ("4h", "1d"), 1e6),
    "S38": (lambda b, tf: ss.s38_all(b, tf), ("4h",), 1e6),
    "S39": (lambda b, tf: ss.s39_all(b, tf), ("4h",), 1e6),
    "S40": (lambda b, tf: ss.s40_all(b, tf), ("4h",), 1e6),
}
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- data


def load_1h_qv(cache: Path, data_dir: Path, s: str) -> tuple[Bars, np.ndarray]:
    """``s1.load_1h`` keeping the quote volume (USD) of each bar."""
    rows: list[list[float]] = []
    for mo in s1.listing(data_dir, s):
        p = cache / "k1h" / s / f"{mo}.json"
        if p.exists():
            rows.extend(s1.rd(p) or [])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 6))
    if a.size:
        a = a[np.argsort(a[:, 0], kind="stable")]
        a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
        a = a[(a[:, 5] > 0) & (a[:, 0] % HOUR_MS == 0)]
    return Bars.from_rows(a[:, :5] if a.size else a, HOUR_MS), (a[:, 5].copy() if a.size else np.zeros(0))


def syms_with_1h(s1_cache: Path, data_dir: Path) -> list[str]:
    have = {p.name for p in (s1_cache / "k1h").iterdir() if p.is_dir()}
    return sorted(set(s1.eligible_syms(data_dir)) & have)


def regime_states(regime_cache: Path) -> dict[int, str | None]:
    spot = cr.parse_closes(json.loads((regime_cache / "spot_1d.json").read_text())["rows"])
    perp = cr.parse_closes(json.loads((regime_cache / "perp_1d.json").read_text())["rows"])
    fund_rows = json.loads((regime_cache / "funding.json").read_text())["rows"]
    funding = points(({"t": r[0], "r": r[1]} for r in fund_rows), "t", "r", lo=-0.1, hi=0.1)
    rows = cre.state_series(spot, perp, funding, START_MS - 2 * DAY_MS, END["4h"])
    return {int(r["t"]): r["state"] for r in rows}


def btc_daily_trend(s1_cache: Path, data_dir: Path) -> dict[str, list[float]]:
    h1, _ = load_1h_qv(s1_cache, data_dir, BTC)
    d = resample(h1, 24)
    return {"t": d.t.tolist(), "trend": sp.trend_series(d).tolist()}


# ---------------------------------------------------------------- per-symbol candidates


def _outcome(path: Bars, t_in: int, e: float, st: float, tg: float, hold: int, rule: StopRule, fees: Fees) -> Any:
    return simulate_stop_rule(path, t_in, e, st, tg, hold, rule, fees=fees)


def run_symbol(args: tuple[str, str, str, str, str]) -> dict[str, Any]:
    setup, s, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    s1c, dd, out = Path(s1_cache), Path(data_dir), Path(out_dir)
    det, tfs, _ = SETUPS[setup]
    member = s1.Membership(s1.members(s1c))
    btc = json.loads((out / "btc_trend.json").read_text())
    btc_t = np.asarray(btc["t"], dtype=np.int64) + DAY_MS  # close times
    btc_tr = np.asarray(btc["trend"], dtype=np.int64)
    states = {int(k): v for k, v in json.loads((out / "regimes.json").read_text()).items()}
    h1, qv = load_1h_qv(s1c, dd, s)
    daily = resample(h1, 24)
    d_tr = sp.trend_series(daily)
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for tf in tfs:
        tb = resample(h1, HOURS[tf])
        for sig in det(tb, tf):
            why[f"{tf}:signal"] += 1
            i = int(np.searchsorted(tb.t, sig.signal_ts))
            t_in = sig.signal_ts + tb.bar_ms
            if not (START_MS <= t_in < END[tf]):
                continue
            top = member(s, t_in)
            turn = sp.trailing_turnover(t_in, h1.t, qv)
            grp = sp.turnover_group(top, turn)
            if grp in (sp.LT1M, sp.UNKNOWN):
                why[f"{tf}:{grp}"] += 1
                continue
            sg = 1.0 if sig.side == "long" else -1.0
            e, st, tg = sg * sig.entry, sg * sig.stop, sg * sig.target
            if not (st < e < tg):
                why[f"{tf}:levels"] += 1
                continue
            path = h1 if sig.side == "long" else h1.mirrored()
            hold = HOLD[tf]
            b = _outcome(path, t_in, e, st, tg, hold, RULE_B, F1)
            if b is None:
                why[f"{tf}:no_path"] += 1
                continue
            bx = _outcome(path, t_in, e, st, tg, hold, RULE_BX, F1)
            b15 = _outcome(path, t_in, e, st, tg, hold, RULE_B, F15)
            bb = tb if sig.side == "long" else tb.mirrored()
            late = sp.late_entry(bb, i, st, tg)
            lb = lb15 = None
            if late is not None:
                lb = _outcome(path, late[0], late[1], st, tg, hold, RULE_B, F1)
                lb15 = _outcome(path, late[0], late[1], st, tg, hold, RULE_B, F15)
            j = int(np.searchsorted(btc_t, t_in, side="right")) - 1
            coin_trend = sp.trend_at(daily, d_tr, t_in)
            btc_trend = int(btc_tr[j]) if j >= 0 else 0
            rr = reward_risk_at(sig.side, sig.entry, sig.stop, sig.target)
            rows.append(
                {
                    "sym": s,
                    "tf": tf,
                    "side": sig.side,
                    "t_in": t_in,
                    "top30": top,
                    "turn": turn,
                    "grp": grp,
                    "rr": rr,
                    "stop_pct": abs(sig.entry - sig.stop) / abs(sig.entry),
                    "score": sp.score_points(sig.side, rr, coin_trend, btc_trend, s == BTC),
                    "regime": sp.state_before(t_in, states),
                    "B": b.r_net,
                    "B_k": b.exit_kind,
                    "B_x": b.exit_ms,
                    "Bx": bx.r_net if bx else None,
                    "B15": b15.r_net if b15 else None,
                    "L": lb.r_net if lb else None,
                    "L15": lb15.r_net if lb15 else None,
                    "cost": b.cost_r,
                }
            )
            why[f"{tf}:cand"] += 1
    (out / "cands").mkdir(parents=True, exist_ok=True)
    (out / "cands" / f"{s}.json").write_text(json.dumps({"rows": rows, "why": dict(why)}))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "bars": len(h1), "rss": rss_mb()}


def prepare(a: argparse.Namespace, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if not (out / "regimes.json").exists():
        t0 = time.time()
        st = regime_states(a.regime_cache)
        (out / "regimes.json").write_text(json.dumps({str(k): v for k, v in st.items()}))
        emit(f"regimes: {len(st)} days in {time.time() - t0:.0f}s; {dict(Counter(v or 'none' for v in st.values()))}")
    if not (out / "btc_trend.json").exists():
        (out / "btc_trend.json").write_text(json.dumps(btc_daily_trend(a.s1_cache, a.data_dir)))


def stage_run(a: argparse.Namespace, out: Path, syms: list[str]) -> None:
    prepare(a, out)
    jobs = [(a.setup, s, str(a.s1_cache), str(a.data_dir), str(out)) for s in syms if a.force or not (out / "cands" / f"{s}.json").exists()]
    emit(f"[run] {a.setup}: {len(jobs)} of {len(syms)} symbols to do, {a.workers} workers")
    t0 = time.time()
    tot = Counter()
    with Pool(a.workers, maxtasksperchild=20) as pool:
        for k, r in enumerate(pool.imap_unordered(run_symbol, jobs), 1):
            tot.update(r["why"])
            if k % 20 == 0 or k == len(jobs):
                el = time.time() - t0
                emit(
                    f"  [run] {k}/{len(jobs)} {el:.0f}s eta {el / k * (len(jobs) - k):.0f}s; last {r['sym']} n={r['n']} {r['sec']:.1f}s worker rss {r['rss']:.0f} MB; main rss {rss_mb():.0f} MB"
                )
    emit(f"[run] done in {time.time() - t0:.0f}s; {dict(sorted(tot.items()))}")


# ---------------------------------------------------------------- sanity


def stage_sanity(a: argparse.Namespace, out: Path, syms: list[str]) -> None:
    elig = s1.eligible_syms(a.data_dir)
    emit(f"eligible USD-M perps (no TradFi, incl. delisted): {len(elig)}; with 1h cache: {len(syms)}")
    have = set(syms)
    qv = s1.daily_qv(a.s1_cache, a.data_dir, elig)
    cnt: Counter[str] = Counter()
    for s, rows in qv.items():
        for t, v in rows:
            if not (PASSPORT_MS <= t < END["4h"]):
                continue
            g = "ge5m" if v >= 5e6 else "1to5m" if v >= 1e6 else None
            if g:
                cnt[f"{g}:all"] += 1
                cnt[f"{g}:have"] += int(s in have)
    for g in ("ge5m", "1to5m"):
        emit(
            f"coin-days 2021-01..2026-09 with daily turnover {g}: {cnt[g + ':all']}, with 1h cache {cnt[g + ':have']} ({cnt[g + ':have'] / max(1, cnt[g + ':all']):.1%})"
        )
    h1, q = load_1h_qv(a.s1_cache, a.data_dir, BTC)
    i = int(np.searchsorted(h1.t, s1.ms("2024-03-01T00:00")))
    emit(
        f"BTC 1h: {len(h1)} bars {iso(int(h1.t[0]))}..{iso(int(h1.t[-1]))}; 2024-03-01 00:00 close {h1.c[i]:.1f} (known ~62.4k); 24h quote turnover then ${sp.trailing_turnover(int(h1.t[i]), h1.t, q) / 1e9:.1f}B"
    )
    gaps = int(np.sum(np.diff(h1.t) > HOUR_MS))
    emit(f"BTC 1h gaps > 1h: {gaps}")
    prepare(a, out)
    t0 = time.time()
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        r = run_symbol((a.setup, s, str(a.s1_cache), str(a.data_dir), str(out)))
        emit(f"timing {s}: n={r['n']} {r['sec']:.1f}s rss {r['rss']:.0f} MB {r['why']}")
    emit(
        f"3 symbols in {time.time() - t0:.0f}s -> about {(time.time() - t0) / 3 * len(syms) / a.workers / 60:.0f} min for {len(syms)} on {a.workers} workers"
    )


# ---------------------------------------------------------------- report


def load_rows(out: Path) -> dict[str, np.ndarray]:
    rows: list[dict[str, Any]] = []
    for p in sorted((out / "cands").glob("*.json")):
        rows.extend(json.loads(p.read_text())["rows"])
    keys = rows[0].keys()
    T = {k: np.array([r[k] for r in rows], dtype=object) for k in keys}
    for k in ("t_in", "B_x"):
        T[k] = T[k].astype(np.int64)
    for k in ("B", "Bx", "B15", "L", "L15", "rr", "stop_pct", "cost"):
        T[k] = np.array([math.nan if x is None else float(x) for x in T[k]])
    T["turn"] = np.array([math.nan if x is None else float(x) for x in T["turn"]])
    T["score"] = T["score"].astype(np.int64)
    T["top30"] = T["top30"].astype(bool)
    T["week"] = T["t_in"] // sp.WEEK_MS
    T["year"] = np.array([datetime.fromtimestamp(t / 1000, UTC).year for t in T["t_in"]])
    T["order"] = np.array([(0 if tf == "4h" else 1) * 2 + (0 if sd == "long" else 1) for tf, sd in zip(T["tf"], T["side"], strict=True)])
    return T


def chain(T: dict[str, np.ndarray], m: np.ndarray) -> np.ndarray:
    idx = np.flatnonzero(m)
    keep = sp.busy_chain([str(x) for x in T["sym"][idx]], T["t_in"][idx], T["B_x"][idx], T["order"][idx])
    out = np.zeros(m.size, dtype=bool)
    out[idx[keep]] = True
    return out


def ci(T: dict[str, np.ndarray], m: np.ndarray, key: str = "B") -> dict[str, float]:
    v = T[key][m]
    ok = np.isfinite(v)
    return sp.mean_ci(v[ok], T["week"][m][ok], n_boot=N_BOOT, seed=SEED)


def fmt(r: dict[str, float]) -> str:
    if r["n"] == 0:
        return "| 0 | — | — | — |"
    lo = "—" if math.isnan(r["lo"]) else f"{r['lo']:+.3f}"
    hi = "—" if math.isnan(r["hi"]) else f"{r['hi']:+.3f}"
    p = "—" if math.isnan(r["p"]) else f"{r['p']:.3f}"
    return f"| {int(r['n'])} | {r['m']:+.3f} | [{lo}; {hi}] | {p} |"


def stage_report(a: argparse.Namespace, out: Path) -> None:
    T = load_rows(out)
    floor = SETUPS[a.setup][2]
    period = T["t_in"] >= PASSPORT_MS
    demo = (T["rr"] >= MIN_RR) & (T["stop_pct"] <= MAX_STOP) & (T["score"] >= MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= floor
    P = chain(T, period & demo & turn5)
    W = chain(T, period & demo)  # >= $1M (the candidates already are)
    A = chain(T, period & turn5)
    IS, OOS = T["t_in"] < OOS_MS, T["t_in"] >= OOS_MS
    elig = s1.eligible_syms(a.data_dir)
    qv = s1.daily_qv(a.s1_cache, a.data_dir, elig)
    top_days = top_n_days(qv, TOP_N)
    in_top = in_prev_day_top(T, top_days)
    # Correction after review (2026-10-30): the demo scans guard.universe(size=100, min_turnover_usd=5e6), i.e. the
    # top-100 by turnover AND >= $5M, not every coin >= $5M as the registration said.
    P100 = chain(T, period & demo & turn5 & in_top)
    emit(
        f"# Паспорт {a.setup}: кандидатів {T['B'].size}; основна вибірка (оборот ≥ ${floor / 1e6:g}M + відбір демо) {int(P.sum())}; ≥ $1M {int(W.sum())}; усі сигнали ≥ ${floor / 1e6:g}M {int(A.sum())}; топ-{TOP_N} і ≥ ${floor / 1e6:g}M + відбір демо {int(P100.sum())}"
    )
    emit(
        f"сигнали за відбором демо (≥ ${floor / 1e6:g}M, 2021+, до ланцюга): R:R≥1.5 {int((period & turn5 & (T['rr'] >= MIN_RR)).sum())}, + стоп≤10 % {int((period & turn5 & (T['rr'] >= MIN_RR) & (T['stop_pct'] <= MAX_STOP)).sum())}, + бал≥2 {int((period & turn5 & demo).sum())} з {int((period & turn5).sum())}"
    )
    if a.setup == "S3":  # known number: demo-stop-management 22.10, S3 4h top-30 2020-02.., rule B: -0.045R, n 10712
        kn = chain(T, (T["tf"] == "4h") & T["top30"])
        emit(
            "звірка з 2026-10-22-demo-stop-management (S3 4h топ-30, усі сигнали, з 2020-02; там −0.045 [−0.077; −0.013], n 10712, зайнятість спільна з S14/S16) "
            + fmt(ci(T, kn))
        )
    hdr = "| вибірка | n | E[R] net | 95 % CI (тижневі блоки) | p |\n|---|---|---|---|---|"
    emit("\n## Загалом (правило B; R на ризик угоди)\n" + hdr)
    for lab, m, k in (
        ("основна 2021+", P, "B"),
        ("основна IS (< 2025-03)", P & IS, "B"),
        ("**основна OOS (≥ 2025-03) — вердикт**", P & OOS, "B"),
        ("основна OOS, Bx (беззбиток і в барі тригера)", P & OOS, "Bx"),
        ("основна 2021+, Bx", P, "Bx"),
        ("усі сигнали без відбору демо, 2021+", A, "B"),
        ("усі сигнали без відбору демо, OOS", A & OOS, "B"),
        ("≥ $1M + відбір демо, 2021+", W, "B"),
        (f"топ-{TOP_N} і ≥ $5M + відбір демо (список демо), 2021+", P100, "B"),
        (f"топ-{TOP_N} і ≥ $5M, IS", P100 & IS, "B"),
        (f"**топ-{TOP_N} і ≥ $5M, OOS**", P100 & OOS, "B"),
        (f"топ-{TOP_N} і ≥ $5M, OOS, Bx", P100 & OOS, "Bx"),
    ):
        emit(f"| {lab} " + fmt(ci(T, m, k)))
    emit(
        f"угод топ-{TOP_N} за роками: "
        + ", ".join(f"{y} {int((P100 & (T['year'] == y)).sum())}" for y in sorted(set(T["year"][P100].tolist())))
    )
    ex = Counter(T["B_k"][P])
    emit(f"виходи (основна): {dict(ex)}; середні витрати {np.nanmean(T['cost'][P]):.3f}R; частка R>0 {np.mean(T['B'][P] > 0):.1%}")

    cells: list[tuple[str, str, np.ndarray, np.ndarray]] = []  # (table, label, sample mask, base)
    for y in sorted(set(T["year"][P].tolist())):
        cells.append(("рік", str(y), P & (T["year"] == y), P))
    for tf in SETUPS[a.setup][1]:
        cells.append(("таймфрейм", tf, P & (T["tf"] == tf), P))
    for sd in ("long", "short"):
        cells.append(("бік", sd, P & (T["side"] == sd), P))
    for g in (sp.TOP30, sp.GE5M, sp.M1TO5):
        cells.append(("оборот (≥ $1M, власний ланцюг)", g, W & (T["grp"] == g), W))
    for st in (*cr.STATES, "none"):
        cells.append(("режим BTC", st, P & (T["regime"] == st), P))
    res = [ci(T, m) for _, _, m, _ in cells]
    ps = [r["p"] if np.isfinite(r["p"]) else 1.0 for r in res]
    adj = holm(ps)
    cur = None
    for (tab, lab, m, _), r, h in zip(cells, res, adj, strict=True):
        if tab != cur:
            emit(
                f"\n## За: {tab} (Холм на всі {len(cells)} клітинок)\n| група | n | E[R] net | 95 % CI | p | p Холма | OOS: n | OOS E[R] [CI] |\n|---|---|---|---|---|---|---|---|"
            )
            cur = tab
        o = ci(T, m & OOS)
        os_ = f"{int(o['n'])} | {o['m']:+.3f} [{o['lo']:+.3f}; {o['hi']:+.3f}]" if o["n"] >= 2 else f"{int(o['n'])} | —"
        emit(f"| {lab} " + fmt(r) + f" {h:.3f} | {os_} |")
    for lab, mm in (("2021+", W), ("OOS", W & OOS)):
        d = sp.diff_ci(T["B"][mm], (T["grp"] == sp.M1TO5)[mm], (T["grp"] != sp.M1TO5)[mm], T["week"][mm], n_boot=N_BOOT, seed=SEED)
        emit(f"контраст 1to5m − (top30+ge5m), {lab}: {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p {d['p']:.3f}")

    emit("\n## Стрес (основна вибірка)\n" + hdr)
    late_ok = np.isfinite(T["L"])
    for lab, m, k in (
        ("база OOS", P & OOS, "B"),
        ("витрати ×1.5, 2021+", P, "B15"),
        ("витрати ×1.5, OOS", P & OOS, "B15"),
        ("вхід на свічку пізніше, 2021+", P, "L"),
        ("вхід на свічку пізніше, OOS", P & OOS, "L"),
        ("пізніше + ×1.5, OOS", P & OOS, "L15"),
        ("база OOS на тих самих угодах, що мають пізній вхід (відібрано за майбутнім)", P & OOS & late_ok, "B"),
    ):
        emit(f"| {lab} " + fmt(ci(T, m, k)))
    emit(f"без пізнього входу (ціна вже за стопом / ціллю або дірка): {int((P & ~late_ok).sum())} з {int(P.sum())}")
    emit(
        "\nціна запізнення (пізній − база), два знаменники; демо теж не входить, якщо ціна вже за стопом / ціллю\n"
        "| період | на сигнал (немає пізнього входу = 0R) | на угоду, де пізній вхід можливий | база пропущених: n, E[R] |\n|---|---|---|---|"
    )
    late0 = np.where(late_ok, T["L"], 0.0) - T["B"]
    for lab, m in (("2021+", P), ("OOS", P & OOS), (f"топ-{TOP_N}, 2021+", P100), (f"топ-{TOP_N}, OOS", P100 & OOS)):
        r = sp.mean_ci(late0[m], T["week"][m], n_boot=N_BOOT, seed=SEED)
        s = sp.mean_ci((T["L"] - T["B"])[m & late_ok], T["week"][m & late_ok], n_boot=N_BOOT, seed=SEED)
        nb = ci(T, m & ~late_ok)
        emit(
            f"| {lab} | {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] | {s['m']:+.3f} [{s['lo']:+.3f}; {s['hi']:+.3f}] | {int(nb['n'])}, {nb['m']:+.3f} |"
        )

    emit("\n## Смуга R для n = 30 (5-й / 50-й / 95-й перцентиль середнього R)")
    for lab, m in (
        (f"OOS топ-{TOP_N} (список демо, еталон пункту 6)", P100 & OOS),
        (f"2021+ топ-{TOP_N}", P100),
        (f"OOS топ-{TOP_N}, Bx", P100 & OOS),
        ("OOS основна", P & OOS),
        ("2021+ основна", P),
        ("OOS основна, Bx", P & OOS),
    ):
        k = "Bx" if "Bx" in lab else "B"
        idx = np.flatnonzero(m)
        idx = idx[np.argsort(T["t_in"][idx], kind="stable")]
        v = T[k][idx]
        v = v[np.isfinite(v)]
        b = sp.r_band(v, 30, reps=N_BOOT, seed=SEED)
        emit(
            f"{lab}: i.i.d. {b['iid_p5']:+.3f} / {b['iid_p50']:+.3f} / {b['iid_p95']:+.3f}; ковзні {int(b['windows'])} вікон {b['roll_p5']:+.3f} / {b['roll_p50']:+.3f} / {b['roll_p95']:+.3f}"
        )

    emit(
        "\n## Розвідувально (не зареєстровано, додано після перших результатів): за балом демо, основна вибірка\n| бал | n | E[R] net | 95 % CI | p | OOS: n | OOS E[R] [CI] |\n|---|---|---|---|---|---|---|"
    )
    for sc in sorted(set(T["score"][P].tolist())):
        m = P & (T["score"] == sc)
        o = ci(T, m & OOS)
        emit(f"| {sc} " + fmt(ci(T, m)) + f" {int(o['n'])} | {o['m']:+.3f} [{o['lo']:+.3f}; {o['hi']:+.3f}] |")

    emit("\n## Без найбільших внесків")
    for lab, m in (("OOS", P & OOS), ("2021+", P)):
        v = T["B"][m]
        top = np.argsort(-v)[:5]
        keep = np.ones(v.size, dtype=bool)
        keep[top] = False
        r = sp.mean_ci(v[keep], T["week"][m][keep], n_boot=N_BOOT, seed=SEED)
        emit(f"{lab} без 5 найкращих угод: {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]")
    per_asset(T, P, IS, OOS)
    coverage(a, qv, top_days, T, P)


def top_n_days(qv: dict[str, list[tuple[int, float]]], n: int) -> dict[int, set[str]]:
    """Per UTC day (open ms): the n eligible coins with the largest Binance daily quote volume that day."""
    by_day: dict[int, list[tuple[float, str]]] = defaultdict(list)
    for s, rows in qv.items():
        for t, v in rows:
            by_day[int(t)].append((v, s))
    return {t: {s for _, s in sorted(xs, reverse=True)[:n]} for t, xs in by_day.items()}


def in_prev_day_top(T: dict[str, np.ndarray], top_days: dict[int, set[str]]) -> np.ndarray:
    """Causal: the coin was in the top list of the last UTC day closed at or before t_in."""
    prev = (T["t_in"] // DAY_MS) * DAY_MS - DAY_MS
    return np.array([str(s) in top_days.get(int(d), ()) for d, s in zip(prev, T["sym"], strict=True)])


def coverage(
    a: argparse.Namespace, qv: dict[str, list[tuple[int, float]]], top_days: dict[int, set[str]], T: dict[str, np.ndarray], P: np.ndarray
) -> None:
    """Share of universe coin-days with >= 20 cached 1h bars, by period, for >= $5M and for top-N & >= $5M."""
    have = {p.name for p in (a.s1_cache / "k1h").iterdir() if p.is_dir()}
    cnt: Counter[str] = Counter()
    miss: Counter[str] = Counter()
    for s, rows in qv.items():
        hours: dict[int, int] = {}
        if s in have:
            h1, _ = load_1h_qv(a.s1_cache, a.data_dir, s)
            u, c = np.unique(h1.t // DAY_MS, return_counts=True)
            hours = dict(zip(u.tolist(), c.tolist(), strict=True))
        for t, v in rows:
            if not (PASSPORT_MS <= t < END["4h"]) or v < 5e6:
                continue
            ok = hours.get(int(t) // DAY_MS, 0) >= 20
            per = "OOS" if t >= OOS_MS else "IS"
            uni = ["ge5m"] + ([f"top{TOP_N}"] if s in top_days.get(int(t), ()) else [])
            for u_ in uni:
                for k in (per, s1.month_of(int(t))[:4]):
                    cnt[f"{u_}:{k}:all"] += 1
                    cnt[f"{u_}:{k}:ok"] += int(ok)
            if not ok and per == "OOS" and f"top{TOP_N}" in uni:
                miss[s] += 1
    emit(f"\n## Покриття 1h-кешем (монето-дні з ≥ 20 годинними свічками, 2021-01..2026-09); rss {rss_mb():.0f} MB")
    ks = ("IS", "OOS", "2021", "2022", "2023", "2024", "2025", "2026")
    emit("| всесвіт | " + " | ".join(ks) + " |\n|---|" + "---|" * len(ks))
    for u_, lab in (("ge5m", "усі ≥ $5M (зареєстрована)"), (f"top{TOP_N}", f"топ-{TOP_N} і ≥ $5M (список демо)")):
        cells = [f"{cnt[f'{u_}:{k}:ok'] / max(1, cnt[f'{u_}:{k}:all']):.1%} ({cnt[f'{u_}:{k}:all']})" for k in ks]
        emit(f"| {lab} | " + " | ".join(cells) + " |")
    emit(f"монети топ-{TOP_N} OOS без покриття (днів): " + ", ".join(f"{s} {n}" for s, n in miss.most_common(15)))
    for old, new in (("RNDRUSDT", "RENDERUSDT"), ("MATICUSDT", "POLUSDT")):
        emit(f"перейменування {old} -> {new}: 1h-кеш {old} {'є' if old in have else 'нема'}, {new} {'є' if new in have else 'нема'}")
    OOS = T["t_in"] >= OOS_MS
    is_syms = set(T["sym"][P & ~OOS].tolist())
    new = np.array([str(s) not in is_syms for s in T["sym"]])
    emit("OOS, монети з угодами в IS " + fmt(ci(T, P & OOS & ~new)) + " ; монети без угод в IS (нові) " + fmt(ci(T, P & OOS & new)))


def per_asset(T: dict[str, np.ndarray], P: np.ndarray, IS: np.ndarray, OOS: np.ndarray) -> None:
    emit("\n## За монетами (5a)")
    syms = sorted(set(T["sym"][P].tolist()))
    means, ses, rows = [], [], []
    for s in syms:
        v = T["B"][P & (T["sym"] == s)]
        if v.size >= 10:
            means.append(v.mean())
            ses.append(v.std(ddof=1) / math.sqrt(v.size))
            rows.append((s, v.size, v.mean()))
    q, df, i2 = cochran_q(np.array(means), np.array(ses))
    emit(f"монет з ≥ 10 угод: {len(rows)}; Cochran Q {q:.1f}, df {df}, p {chi2_sf(q, df):.3f}, I² {i2:.2f}")
    rows.sort(key=lambda x: x[2])
    emit("найгірші 5: " + ", ".join(f"{s} n={n} {m:+.2f}" for s, n, m in rows[:5]))
    emit("найкращі 5: " + ", ".join(f"{s} n={n} {m:+.2f}" for s, n, m in rows[-5:]))
    is_rank = []
    for s in syms:
        v = T["B"][P & IS & (T["sym"] == s)]
        if v.size >= 10:
            is_rank.append((v.mean(), s))
    is_rank.sort(reverse=True)
    top = {s for _, s in is_rank[:10]}
    ranked = {s for _, s in is_rank}
    mt = P & OOS & np.isin(T["sym"], list(top))
    mr = P & OOS & np.isin(T["sym"], list(ranked - top))
    d = sp.diff_ci(T["B"], mt, mr, T["week"], n_boot=N_BOOT, seed=SEED)
    emit(f"топ-10 монет за IS (з ≥ 10 угод IS, з {len(ranked)}): {sorted(top)}")
    emit(
        "OOS топ-10 "
        + fmt(ci(T, mt))
        + " ; решта "
        + fmt(ci(T, mr))
        + f" ; різниця {d['d']:+.3f} [{d['lo']:+.3f}; {d['hi']:+.3f}] p {d['p']:.3f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("setup", choices=sorted(SETUPS))
    ap.add_argument("--stage", choices=("sanity", "run", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/setup_passport")
    ap.add_argument("--s1-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--regime-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/cycle_regime")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = a.cache_dir / a.setup
    out.mkdir(parents=True, exist_ok=True)
    syms = syms_with_1h(a.s1_cache, a.data_dir)
    if a.limit:
        syms = syms[: a.limit]
    with (out / f"{a.stage}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"verify_setup_passport {a.setup} --stage {a.stage} at {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
        if a.stage == "sanity":
            stage_sanity(a, out, syms)
        elif a.stage == "run":
            stage_run(a, out, syms)
        else:
            stage_report(a, out)
        emit(f"peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
