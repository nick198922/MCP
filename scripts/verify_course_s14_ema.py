"""Course setup S14 / H5 (IDEA-20261016-course-setups-crypto, step 1): trend EMA 13/21/34 test after an impulse,
with the trend, on Binance USD-M perps; point-in-time universe BTC, ETH + top-30 by quote volume (delisted included).

Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 1". Stages:
  uv run python scripts/verify_course_s14_ema.py --stage prereg   (SHA-256 of the registration section)
  uv run python scripts/verify_course_s14_ema.py --stage universe --out ~/.cache/tradedesk-agents/course_s14_ema/universe.log
  uv run python scripts/verify_course_s14_ema.py --stage fetch    --out ~/.cache/tradedesk-agents/course_s14_ema/fetch.log
  uv run python scripts/verify_course_s14_ema.py --stage count    --out ~/.cache/tradedesk-agents/course_s14_ema/count.md
  uv run python scripts/verify_course_s14_ema.py --stage report   --out ~/.cache/tradedesk-agents/course_s14_ema/report.md
``count`` (stage 0) counts signals without any trade outcome. Data: monthly kline zips from data.binance.vision via
rt.binance_vision (1d for the universe ranking, 1h for the bars), one JSON per symbol-month under --cache-dir; daily
klines from 2021-10 are read from the funding-crowding cache (--data-dir), which the same provider filled. No keys,
no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.course_s14_ema import (
    DAY_MS,
    HOUR_MS,
    Bars,
    Params,
    align_daily,
    ema,
    find_signals,
    forward_min_low,
    h5_events,
    indicators,
    matched_baseline,
    placebo_signal,
    resample,
    segment_pos,
    simulate,
    trend_episodes,
)
from tradedesk.analytics.liquidation_flush import (
    base_of,
    cluster_bootstrap_ci,
    cluster_bootstrap_means,
    cluster_bootstrap_p,
    holm,
    monthly_universe,
    without_top_days,
)
from tradedesk.analytics.listing_drift import symbol_eligible
from tradedesk.analytics.session_stop_hunt import cluster_bootstrap_diff
from tradedesk.core.errors import ProviderError

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
ALWAYS = ("BTCUSDT", "ETHUSDT")
TOP_N = 30
FIRST_MONTH, LAST_MONTH = "2020-01", "2026-09"
FIRST_UNI_MONTH = "2020-02"


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


START_MS = ms("2020-02-01T00:00")  # signal bar close (= entry) window
END_MS = ms("2026-09-23T00:00")
OOS_MS = ms("2025-03-01T00:00")
SEED = 20261017
P1_PER_SIGNAL = 3
H5_RUN = 120  # 4h bars = 20 days
H5_HORIZON = 30 * DAY_MS
FUND_8H = 0.0001  # funding stress: 0.01 % per 8 h against the long
MDE_GATE = 0.20
MIN_DATES = 200
MIN_EFFECT = 0.10
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


def month_of(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m")


def day_of(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def months(first: str = FIRST_MONTH, last: str = LAST_MONTH) -> list[str]:
    y, m = int(first[:4]), int(first[5:])
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 1")
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
    return [m for m in (rd(p) if p.exists() else []) or [] if FIRST_MONTH <= m <= LAST_MONTH]


async def fetch_jobs(jobs: list[tuple[str, str, str, Path]], label: str) -> None:
    """jobs: (symbol, interval, YYYY-MM, path). Rows kept: [open_ms, o, h, l, c, quote_volume]."""
    from tradedesk.server import build_server

    _, rt = build_server()
    sem = asyncio.Semaphore(8)
    errors: Counter[str] = Counter()
    done = 0
    t0 = time.time()
    emit(f"[{label}] {len(jobs)} files to fetch")

    async def one(s: str, iv: str, mo: str, p: Path) -> None:
        nonlocal done
        async with sem:
            try:
                f = await rt.binance_vision.um_klines_monthly(s, iv, int(mo[:4]), int(mo[5:]))
                rows: Any = [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in f.data]
            except ProviderError as e:
                if e.reason != "data_unavailable":
                    errors[e.reason] += 1
                    return  # transient: a rerun retries
                rows = None
        wr(p, rows)
        done += 1
        if done % 100 == 0:
            el = time.time() - t0
            emit(f"  [{label}] {done}/{len(jobs)} in {el:.0f}s, eta {el / done * (len(jobs) - done):.0f}s, rss {rss_mb():.0f} MB")

    try:
        await asyncio.gather(*[one(*j) for j in jobs])
    finally:
        await rt.aclose()
    emit(f"[{label}] done {done}/{len(jobs)} in {time.time() - t0:.0f}s; transient errors: {dict(errors)}; rss {rss_mb():.0f} MB")


def daily_qv(cache: Path, data_dir: Path, syms: list[str]) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = {}
    for s in syms:
        rows: list[tuple[int, float]] = []
        for mo in listing(data_dir, s):
            p = (cache / "k1d" / s / f"{mo}.json") if mo < "2021-10" else (data_dir / "k1d" / s / f"{mo}.json")
            if p.exists():
                for r in rd(p) or []:
                    rows.append((int(r[0]), float(r[6] if len(r) > 6 else r[5])))
        if rows:
            out[s] = rows
    return out


def stage_universe(cache: Path, data_dir: Path, limit: int | None) -> None:
    syms = eligible_syms(data_dir)
    emit(f"eligible USD-M COIN perps (incl. delisted): {len(syms)}")
    jobs = []
    for s in syms:
        for mo in listing(data_dir, s):
            if mo < "2021-10":
                p = cache / "k1d" / s / f"{mo}.json"
                if not p.exists():
                    jobs.append((s, "1d", mo, p))
    asyncio.run(fetch_jobs(jobs[:limit] if limit else jobs, "k1d"))
    if limit:
        return
    qv = daily_qv(cache, data_dir, syms)
    uni: dict[str, list[str]] = {}
    for mo in months(FIRST_UNI_MONTH, LAST_MONTH):
        ranked = monthly_universe(qv, ms(f"{mo}-01T00:00"), top_n=80)
        seen, mem = set(), []
        for s in ranked:
            b = base_of(s)
            if b in seen:
                continue
            seen.add(b)
            mem.append(s)
            if len(mem) == TOP_N:
                break
        for s in ALWAYS:
            if s not in mem:
                mem.append(s)
        uni[mo] = mem
    wr(cache / "universe.json", uni)
    info = rd(data_dir / "exchange_info.json")["info"]
    allm = sorted({s for v in uni.values() for s in v})
    st = Counter((info.get(s) or {}).get("status", "absent") for v in uni.values() for s in v)
    emit(f"universe months {len(uni)}; distinct members {len(allm)}; member-months by today's status {dict(st)}")
    emit(f"members not TRADING today: {[s for s in allm if (info.get(s) or {}).get('status') != 'TRADING']}")
    for mo in ("2020-02", "2022-06", "2024-01", "2026-09"):
        emit(f"  {mo}: {uni[mo]}")


# ---------------------------------------------------------------- 1h bars


def members(cache: Path) -> dict[str, list[str]]:
    return rd(cache / "universe.json")


def stage_fetch(cache: Path, data_dir: Path, limit: int | None) -> None:
    uni = members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    jobs = []
    for s in syms:
        for mo in listing(data_dir, s):
            p = cache / "k1h" / s / f"{mo}.json"
            if not p.exists():
                jobs.append((s, "1h", mo, p))
    emit(f"members {len(syms)}; 1h files missing {len(jobs)}")
    asyncio.run(fetch_jobs(jobs[:limit] if limit else jobs, "k1h"))


def load_1h(cache: Path, data_dir: Path, s: str) -> Bars:
    rows: list[list[float]] = []
    for mo in listing(data_dir, s):
        p = cache / "k1h" / s / f"{mo}.json"
        if p.exists():
            rows.extend(rd(p) or [])
    a = np.array(rows, dtype=float) if rows else np.zeros((0, 6))
    if a.size:
        a = a[np.argsort(a[:, 0], kind="stable")]
        a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
        a = a[(a[:, 5] > 0) & (a[:, 0] % HOUR_MS == 0)]  # zero-volume bars (settling contracts) count as missing
    return Bars.from_rows(a[:, :5] if a.size else a, HOUR_MS)


# ---------------------------------------------------------------- per-symbol run


class Membership:
    def __init__(self, uni: dict[str, list[str]]) -> None:
        self.by_month = {mo: set(v) for mo, v in uni.items()}

    def __call__(self, s: str, t_ms: int) -> bool:
        return s in self.by_month.get(month_of(t_ms), set())


def in_period(t_ms: int) -> bool:
    return START_MS <= t_ms < END_MS


def trade_row(base: dict[str, Any], tr: Any, sig: Any) -> dict[str, Any]:
    return {
        **base,
        "day": day_of(tr.entry_ms),
        "entry_ms": tr.entry_ms,
        "oos": tr.entry_ms >= OOS_MS,
        "r_gross": tr.r_gross,
        "cost_r": tr.cost_r,
        "r_net": tr.r_net,
        "exit_kind": tr.exit_kind,
        "both": tr.both_touched,
        "stop_pct": tr.stop_pct,
        "hold_h": (tr.exit_ms - tr.entry_ms) / HOUR_MS,
        "target_kind": sig.target_kind,
        "target_r": (sig.target - sig.entry) / sig.risk,
        "mfe_r": tr.mfe_r,
    }


def run_symbol(
    s: str, h1: Bars, member: Membership, p: Params, rng: np.random.Generator, outcomes: bool
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    h4 = resample(h1, 4)
    for tf, bars in (("4h", h4), ("1h", h1)):
        for side in ("long", "short"):
            b = bars if side == "long" else bars.mirrored()
            path = h1 if side == "long" else h1.mirrored()
            ind = indicators(b, p)
            ep = trend_episodes(ind, p) if outcomes else None
            ep_idx: dict[int, np.ndarray] = {}
            if ep is not None:
                order = np.argsort(ep, kind="stable")
                vals = ep[order]
                cuts = np.flatnonzero(np.diff(vals)) + 1
                for grp in np.split(order, cuts):
                    if grp.size and ep[grp[0]] >= 0:
                        ep_idx[int(ep[grp[0]])] = grp
            for variant in ("touch", "sweep", "touch_no_trend"):
                sigs = find_signals(b, ind, p, variant)
                busy_until = -1
                touch_set = {x.idx for x in find_signals(b, ind, p, "touch")} if variant == "touch_no_trend" else set()
                for sg in sigs:
                    t_in = b.close_time(sg.idx)
                    if not in_period(t_in):
                        continue
                    if not member(s, t_in):
                        why[f"{tf}:{side}:{variant}:not_member"] += 1
                        continue
                    if t_in < busy_until:
                        why[f"{tf}:{side}:{variant}:busy"] += 1
                        continue
                    base = {
                        "sym": s,
                        "tf": tf,
                        "side": side,
                        "variant": variant if variant != "touch_no_trend" else "p2",
                        "is_signal": sg.idx in touch_set,
                    }
                    if not outcomes:
                        why[f"{tf}:{side}:{variant}:ok"] += 1
                        rows.append(
                            {
                                **base,
                                "day": day_of(t_in),
                                "oos": t_in >= OOS_MS,
                                "stop_pct": sg.risk / abs(sg.entry),
                                "target_kind": sg.target_kind,
                            }
                        )
                        busy_until = t_in + p.hold_ms  # stage 0: no outcome, assume the full hold
                        continue
                    tr = simulate(path, t_in, sg.entry, sg.stop, sg.target, p.hold_ms)
                    if tr is None:
                        why[f"{tf}:{side}:{variant}:no_path"] += 1
                        continue
                    busy_until = tr.exit_ms
                    why[f"{tf}:{side}:{variant}:ok"] += 1
                    rows.append(trade_row(base, tr, sg))
                    if variant == "touch_no_trend" or ep is None:
                        continue
                    # placebo P1: random bars of the same trend episode, same stop and target rules
                    e = int(ep[sg.idx]) if variant == "touch" else int(ep[min(sg.hi_idx + 1, len(ep) - 1)])
                    cand = ep_idx.get(e)
                    if cand is None:
                        why[f"{tf}:{side}:{variant}:p1_no_episode"] += 1
                        continue
                    cand = np.array(
                        [j for j in cand if j != sg.idx and in_period(b.close_time(int(j))) and member(s, b.close_time(int(j)))]
                    )
                    if cand.size == 0:
                        why[f"{tf}:{side}:{variant}:p1_empty"] += 1
                        continue
                    for j in rng.choice(cand, size=min(P1_PER_SIGNAL, cand.size), replace=False):
                        ps = placebo_signal(b, ind, p, int(j))
                        if ps is None:
                            continue
                        pt = simulate(path, b.close_time(int(j)), ps.entry, ps.stop, ps.target, p.hold_ms)
                        if pt is not None:
                            rows.append(trade_row({**base, "variant": f"p1_{variant}", "is_signal": False}, pt, ps))
    return rows, why


def run_h5(
    s: str, h1: Bars, member: Membership, p: Params, min_run: int = H5_RUN
) -> tuple[list[dict[str, Any]], tuple[np.ndarray, np.ndarray]]:
    h4 = resample(h1, 4)
    d1 = resample(h1, 24)
    pos_d = segment_pos(d1.t, p.max_gap_ms)
    lvl_d = np.fmax(ema(d1.c, 100, pos_d), ema(d1.c, 200, pos_d))
    lvl_d[np.isnan(ema(d1.c, 200, pos_d))] = np.nan  # both EMAs must exist
    ind = indicators(h4, p)
    level = align_daily(h4, d1, lvl_d)
    fwd = forward_min_low(h4, H5_HORIZON)
    close_t = h4.t + h4.bar_ms
    ok = np.isfinite(level) & np.isfinite(fwd) & (h4.c > level)
    ok &= np.array([in_period(int(t)) and member(s, int(t)) for t in close_t])
    ev = set(int(i) for i in h5_events(h4, ind, min_run))
    rows = []
    for i in sorted(ev):
        if not ok[i]:
            continue
        rows.append(
            {
                "sym": s,
                "day": day_of(int(close_t[i])),
                "oos": int(close_t[i]) >= OOS_MS,
                "dist": float(level[i] / h4.c[i] - 1),
                "hit": float(fwd[i] <= level[i]),
            }
        )
    base = ok.copy()
    base[list(ev)] = False
    return rows, ((level[base] / h4.c[base] - 1), (fwd[base] <= level[base]).astype(float))


def sanity(cache: Path, data_dir: Path, syms: list[str]) -> None:
    emit("## Санітарні перевірки")
    b = load_1h(cache, data_dir, "BTCUSDT")
    d = resample(b, 24)
    k = int(np.searchsorted(d.t, ms("2024-12-31T00:00")))
    emit(
        f"- BTCUSDT: денне закриття 2024-12-31 з годинних барів = {d.c[k] if k < len(d) and d.t[k] == ms('2024-12-31T00:00') else 'нема'} (еталон з попередніх перевірок: 93548.9)"
    )
    gaps, jumps, firsts = [], [], []
    for s in syms:
        h = load_1h(cache, data_dir, s)
        if len(h) == 0:
            emit(f"- {s}: немає годинних даних")
            continue
        dt = np.diff(h.t)
        big = int(np.sum(dt > 48 * HOUR_MS))
        small = int(np.sum((dt > HOUR_MS) & (dt <= 48 * HOUR_MS)))
        gaps.append((s, small, big))
        r = np.abs(np.diff(np.log(h.c)))
        if r.size and r.max() > np.log(1.5):
            i = int(np.argmax(r))
            jumps.append(f"{s} {datetime.fromtimestamp(h.t[i + 1] / 1000, tz=UTC):%Y-%m-%d %H:%M} {h.c[i]:g}->{h.c[i + 1]:g}")
        firsts.append((s, day_of(int(h.t[0])), day_of(int(h.t[-1]))))
    emit(
        f"- символів з годинними даними: {len(firsts)}; дрібних пропусків (≤ 48 год) разом {sum(g[1] for g in gaps)}, розривів > 48 год {sum(g[2] for g in gaps)}"
    )
    emit(f"- найбільше дрібних пропусків: {sorted(gaps, key=lambda g: -g[1])[:5]}")
    emit(f"- розриви > 48 год: {[g for g in gaps if g[2]]}")
    emit(f"- годинні стрибки закриття > 50 %: {jumps or '—'}")
    # ticker collisions: several members on one asset (excluded by base_of, but list in case)
    by: dict[str, list[str]] = defaultdict(list)
    for s in syms:
        by[base_of(s)].append(s)
    emit(f"- активи з кількома символами серед членів: {[v for v in by.values() if len(v) > 1] or '—'}")
    emit(f"- пікова RSS {rss_mb():.0f} MB")


# ---------------------------------------------------------------- stats


def fmt_p(p: float | None) -> str:
    return "—" if p is None else f"{p:.4f}"


def fmt_ci(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def stats(rows: list[dict[str, Any]], key: str = "r_net", n_boot: int = 5000) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    v = [r[key] for r in rows]
    d = [r["day"] for r in rows]
    multi = len(set(d)) > 1
    bm = cluster_bootstrap_means(v, d, n_boot=n_boot) if multi else np.array([])
    return {
        "n": len(v),
        "days": len(set(d)),
        "win": sum(1 for x in v if x > 0) / len(v),
        "mean": statistics.fmean(v),
        "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))) if multi else None,
        "p": float((np.sum(bm <= 0) + 1) / (bm.size + 1)) if multi else None,
        "se": float(np.std(bm)) if multi else None,
        "sd": statistics.pstdev(v),
    }


HDR = "| вибірка | n | дат | частка R net>0 | E[R] gross | E[R] net | 95 % CI net | p (E>0) | SE |\n|---|---|---|---|---|---|---|---|---|"


def line(label: str, rows: list[dict[str, Any]], n_boot: int = 5000) -> str:
    if not rows:
        return f"| {label} | 0 | — | — | — | — | — | — | — |"
    n = stats(rows, "r_net", n_boot)
    g = statistics.fmean(r["r_gross"] for r in rows)
    p = "—" if n["p"] is None else f"{n['p']:.4f}"
    se = "—" if n["se"] is None else f"{n['se']:.3f}"
    return f"| {label} | {n['n']} | {n['days']} | {n['win']:.1%} | {g:+.3f} | {n['mean']:+.3f} | {fmt_ci(n['ci'])} | {p} | {se} |"


def sel(rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return [r for r in rows if all((r[k] in v) if isinstance(v, (set, tuple, list)) else r[k] == v for k, v in kw.items())]


def diff(a: list[dict[str, Any]], b: list[dict[str, Any]], n_boot: int = 5000) -> dict[str, Any]:
    if not a or not b:
        return {"diff": float("nan"), "ci": None, "p": None}
    return cluster_bootstrap_diff(
        [r["r_net"] for r in a], [r["day"] for r in a], [r["r_net"] for r in b], [r["day"] for r in b], n_boot=n_boot
    )


def q(xs: list[float], p: float) -> float:
    return float(np.quantile(np.asarray(xs), p)) if xs else float("nan")


# ---------------------------------------------------------------- stages


def collect(
    cache: Path, data_dir: Path, outcomes: bool, h5: bool
) -> tuple[list[dict[str, Any]], Counter[str], list[dict[str, Any]], list[np.ndarray]]:
    uni = members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    member = Membership(uni)
    p = Params()
    rng = np.random.default_rng(SEED)
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    h5rows: list[dict[str, Any]] = []
    base_d: list[np.ndarray] = []
    base_h: list[np.ndarray] = []
    t0 = time.time()
    for k, s in enumerate(syms):
        h1 = load_1h(cache, data_dir, s)
        if len(h1) < 500:
            why["symbol_too_short"] += 1
            continue
        r, w = run_symbol(s, h1, member, p, rng, outcomes)
        rows.extend(r)
        why.update(w)
        if h5:
            ev, (bd, bh) = run_h5(s, h1, member, p)
            h5rows.extend(ev)
            base_d.append(bd)
            base_h.append(bh)
        del h1
        if (k + 1) % 10 == 0 or k + 1 == len(syms):
            emit(f"  [run] {k + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rows {len(rows)}, rss {rss_mb():.0f} MB")
    bases = [np.concatenate(base_d) if base_d else np.zeros(0), np.concatenate(base_h) if base_h else np.zeros(0)]
    return rows, why, h5rows, bases


def stage_count(cache: Path, data_dir: Path) -> None:
    emit("# Крок 1 S14/H5, стадія 0: частота сигналів (без результатів угод)")
    uni = members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    sanity(cache, data_dir, syms)
    rows, why, _, _ = collect(cache, data_dir, outcomes=False, h5=False)
    emit("\n| tf | сторона | варіант | сигналів IS | дат IS | сигналів OOS | дат OOS | медіана стопу, % | частка цілі «хай» |")
    emit("|---|---|---|---|---|---|---|---|---|")
    for tf in ("4h", "1h"):
        for side in ("long", "short"):
            for v in ("touch", "sweep", "p2"):
                r = sel(rows, tf=tf, side=side, variant=v)
                i_, o_ = [x for x in r if not x["oos"]], [x for x in r if x["oos"]]
                sp = q([x["stop_pct"] for x in r], 0.5)
                hk = sum(1 for x in r if x["target_kind"] == "high") / max(1, len(r))
                emit(
                    f"| {tf} | {side} | {v} | {len(i_)} | {len({x['day'] for x in i_})} | {len(o_)} | {len({x['day'] for x in o_})} | {sp:.2%} | {hk:.0%} |"
                )
    emit(f"\n- відсів: {dict(sorted(why.items()))}")
    main = sel(rows, tf="4h", side="long", variant="touch")
    d_is = len({x["day"] for x in main if not x["oos"]})
    emit(f"- основна гілка (лонг 4h, дотик), різних дат IS: {d_is}; поріг реєстрації {MIN_DATES}")
    emit(
        f"- MDE при SD R net = 1.3 (припущення до результатів) і n_eff = дати IS: {2.8 * 1.3 / max(1, d_is) ** 0.5:.3f}R; ворота {MDE_GATE}R"
    )


def stage_report(cache: Path, data_dir: Path) -> None:
    uni = members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    emit("# Крок 1 S14/H5: результати")
    emit(
        f"Всесвіт: BTC, ETH + топ-{TOP_N} USD-M за обсягом, щомісяця ({len(uni)} міс., {len(syms)} різних символів); сигнали {day_of(START_MS)} … {day_of(END_MS)}; IS < {day_of(OOS_MS)} ≤ OOS."
    )
    emit("R — частки ризику (вхід − стоп); net — після 0.14 % за коло; це дохід на ризик угоди, не на капітал.\n")
    sanity(cache, data_dir, syms)
    rows, why, h5rows, (bd, bh) = collect(cache, data_dir, outcomes=True, h5=True)
    wr(cache / "trades.json", rows)
    emit(f"\n- відсів і лічильники: {dict(sorted(why.items()))}")

    def split(r: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return [x for x in r if not x["oos"]], [x for x in r if x["oos"]]

    main = sel(rows, tf="4h", side="long", variant="touch")
    m_is, m_oos = split(main)
    p1 = sel(rows, tf="4h", side="long", variant="p1_touch")
    p1_is, p1_oos = split(p1)
    p2 = sel(rows, tf="4h", side="long", variant="p2")
    p2c = [r for r in p2 if not r["is_signal"]]

    emit("\n## Основна гілка: лонг, 4h, дотик зони EMA 13/34")
    emit(HDR)
    emit(line("IS (основна метрика)", m_is, 10000))
    emit(line("OOS", m_oos, 10000))
    emit(line("весь період", main, 10000))
    emit(line("плацебо P1 IS", p1_is))
    emit(line("плацебо P1 OOS", p1_oos))
    emit(line("плацебо P2 (без тренду) IS, усі", split(p2)[0]))
    emit(line("плацебо P2 IS, без сигналів", split(p2c)[0]))
    emit(line("плацебо P2 OOS, без сигналів", split(p2c)[1]))
    s_is = stats(m_is, n_boot=10000)
    s_oos = stats(m_oos, n_boot=10000)
    d1_is, d1_oos = diff(m_is, p1_is, 10000), diff(m_oos, p1_oos, 10000)
    d2_is = diff(m_is, split(p2c)[0], 10000)
    emit("\n| порівняння | різниця E[R] net | 95 % CI | p (різниця>0) |\n|---|---|---|---|")
    for lab, d in (
        ("сигнал − P1, IS", d1_is),
        ("сигнал − P1, OOS", d1_oos),
        ("сигнал − P2 без сигналів, IS", d2_is),
        ("сигнал − P2 без сигналів, OOS", diff(m_oos, split(p2c)[1])),
    ):
        emit(f"| {lab} | {d['diff']:+.3f} | {fmt_ci(d['ci'])} | {fmt_p(d['p'])} |")

    # gates and verdict
    n_dates = s_is.get("days", 0)
    mde = 2.8 * (s_is.get("se") or float("inf"))
    emit("\n## Ворота й вердикт (за реєстрацією)")
    emit(
        f"- різних дат IS: {n_dates} (поріг {MIN_DATES}); MDE = 2.8·SE = {mde:.3f}R (ворота {MDE_GATE}R); SD R net {s_is.get('sd', float('nan')):.2f}"
    )
    p_raw = s_is.get("p")
    p_holm = holm([p_raw])[0] if p_raw is not None else None
    emit(
        f"- p основної метрики (однобічне, IS): сире {p_raw}; Holm (m = 1 завершений крок) {p_holm}; для довідки ×11 кроків: {min(1.0, 11 * p_raw) if p_raw is not None else None}"
    )
    if n_dates < MIN_DATES or mde > MDE_GATE:
        verdict = "inconclusive (замало даних або потужності)"
    elif s_is["ci"][0] <= 0 or d1_is["diff"] < MIN_EFFECT:
        verdict = "rejected"
    elif (
        s_is["mean"] >= MIN_EFFECT
        and d1_is["ci"] is not None
        and d1_is["ci"][0] > 0
        and p_holm is not None
        and p_holm < 0.05
        and s_oos.get("n", 0) > 0
        and s_oos["mean"] > 0
        and (s_oos.get("p") or 1) < 0.05
        and d1_oos["diff"] > 0
    ):
        verdict = "confirmed"
    else:
        verdict = "inconclusive (позитив в IS не пройшов Holm, OOS чи поріг проти плацебо)"
    emit(f"- **вердикт за правилами реєстрації: {verdict}**")

    # robustness on the main branch
    emit("\n## Стійкість основної гілки (весь період, net)")
    emit(HDR)
    emit(line("BTC+ETH", sel(main, sym=list(ALWAYS))))
    emit(line("альти", [r for r in main if r["sym"] not in ALWAYS]))
    for y in range(2020, 2027):
        emit(line(f"{y}", [r for r in main if r["day"].startswith(str(y))]))
    emit(line("ціль = хай імпульсу", sel(main, target_kind="high")))
    emit(line("ціль = 2R", sel(main, target_kind="fixed")))
    for k in (5, 10, 20):
        v, dd, _ = without_top_days([r["r_net"] for r in m_is], [r["day"] for r in m_is], k)
        emit(
            f"| IS без {k} найкращих дат | {len(v)} | {len(set(dd))} | — | — | {statistics.fmean(v):+.3f} | {fmt_ci(cluster_bootstrap_ci(v, dd, n_boot=5000))} | — | — |"
        )
    for mult in (0.5, 2.0, 3.0):
        v = [r["r_gross"] - mult * r["cost_r"] for r in m_is]
        emit(
            f"| IS, витрати ×{mult:g} | {len(v)} | — | — | — | {statistics.fmean(v):+.3f} | {fmt_ci(cluster_bootstrap_ci(v, [r['day'] for r in m_is], n_boot=5000))} | — | — |"
        )
    v = [r["r_net"] - FUND_8H * r["hold_h"] / 8 / r["stop_pct"] for r in m_is]
    emit(
        f"| IS, фандинг 0.01 %/8 год проти лонга | {len(v)} | — | — | — | {statistics.fmean(v):+.3f} | {fmt_ci(cluster_bootstrap_ci(v, [r['day'] for r in m_is], n_boot=5000))} | — | — |"
    )
    if main:
        st = [r["stop_pct"] for r in main]
        rn = [r["r_net"] for r in main]
        cr = [r["cost_r"] for r in main]
        emit(
            f"\n- ширина стопу, % ціни: p10 {q(st, 0.1):.2%}, медіана {q(st, 0.5):.2%}, p90 {q(st, 0.9):.2%}; витрати в R: медіана {q(cr, 0.5):.3f}, середнє {statistics.fmean(cr):.3f}"
        )
        emit(
            f"- розподіл R net: p5 {q(rn, 0.05):+.2f}, p25 {q(rn, 0.25):+.2f}, медіана {q(rn, 0.5):+.2f}, p75 {q(rn, 0.75):+.2f}, p95 {q(rn, 0.95):+.2f}"
        )
        emit(f"- виходи: {dict(Counter(r['exit_kind'] for r in main))}; бар зачепив і стоп, і ціль: {sum(r['both'] for r in main)}")
        emit(
            f"- ціль у R: медіана {q([r['target_r'] for r in main], 0.5):.2f}; утримання, год: медіана {q([r['hold_h'] for r in main], 0.5):.0f}"
        )
        emit(
            f"- дохід на номінал за угоду (net, середнє): {statistics.fmean(r['r_net'] * r['stop_pct'] for r in main):+.3%}; на капітал при ризику 1 % на угоду: {statistics.fmean(rn) * 0.01:+.3%} за угоду"
        )
        ev = sorted([(r["entry_ms"], 1) for r in main] + [(r["entry_ms"] + int(r["hold_h"] * HOUR_MS), -1) for r in main])
        cur = mx = 0
        for _, d in ev:
            cur += d
            mx = max(mx, cur)
        emit(
            f"- одночасно відкритих угод (макс): {mx}; угод на місяць у середньому: {len(main) / max(1, len({r['day'][:7] for r in main})):.1f}"
        )

    # secondary branches
    emit("\n## Другорядні гілки (сирі p; Holm усередині кроку — лише довідка)")
    emit(HDR)
    sec: list[tuple[str, float]] = []
    for tf in ("4h", "1h"):
        for side in ("long", "short"):
            for v in ("touch", "sweep"):
                if (tf, side, v) == ("4h", "long", "touch"):
                    continue
                r = sel(rows, tf=tf, side=side, variant=v)
                ri, ro = split(r)
                lab = f"{tf} {side} {'дотик' if v == 'touch' else 'свип фрактала'}"
                emit(line(f"{lab} IS", ri))
                emit(line(f"{lab} OOS", ro))
                pp = split(sel(rows, tf=tf, side=side, variant=f"p1_{v}"))
                emit(line(f"  P1 {lab} IS", pp[0]))
                d = diff(ri, pp[0])
                emit(f"|   {lab}: сигнал − P1 IS | різниця {d['diff']:+.3f} | CI {fmt_ci(d['ci'])} | p {fmt_p(d['p'])} | | | | | |")
                st_ = stats(ri)
                if st_.get("p") is not None:
                    sec.append((f"{lab} IS", st_["p"]))
    if sec:
        adj = holm([x[1] for x in sec])
        emit("\n| гілка | сире p (E[R] net > 0) | Holm усередині кроку |\n|---|---|---|")
        for (lab, pr), pa in zip(sec, adj, strict=True):
            emit(f"| {lab} | {pr:.4f} | {pa:.4f} |")

    emit("\n## H5: втрата 4h EMA21 після ≥ 20 днів над нею → дотик денної EMA 100/200 за 30 днів")
    h5_report(h5rows, bd, bh)
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def h5_report(h5rows: list[dict[str, Any]], bd: np.ndarray, bh: np.ndarray) -> None:
    for lab, rr in (("IS", [r for r in h5rows if not r["oos"]]), ("OOS", [r for r in h5rows if r["oos"]]), ("весь період", h5rows)):
        if not rr:
            emit(f"- {lab}: подій 0")
            continue
        mb = matched_baseline([r["dist"] for r in rr], [r["hit"] for r in rr], bd, bh)
        ex = mb["expected"]
        vals = [r["hit"] - e for r, e in zip(rr, ex, strict=True) if np.isfinite(e)]
        days = [r["day"] for r, e in zip(rr, ex, strict=True) if np.isfinite(e)]
        ci = cluster_bootstrap_ci(vals, days, n_boot=10000)
        pv = cluster_bootstrap_p(vals, days, n_boot=10000)
        emit(
            f"- {lab}: подій {mb['n']} (дат {len(set(days))}); частка влучань {mb['event_rate']:.1%}; база з тією ж відстанню {mb['base_rate']:.1%}; "
            f"різниця {statistics.fmean(vals):+.1%} CI {fmt_ci(ci)} p {pv}; медіана відстані до рівня {q([r['dist'] for r in rr], 0.5):+.1%}"
        )
    emit(f"- база: {bd.size} барів; загальна частка влучань {float(bh.mean()) if bh.size else float('nan'):.1%}")


def stage_h5x(cache: Path, data_dir: Path) -> None:
    """Exploratory: the registered H5 run (120 4h bars) never occurs; shorter runs, chosen after seeing that count."""
    uni = members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    member = Membership(uni)
    p = Params()
    runs = (60, 30)
    acc: dict[int, tuple[list[dict[str, Any]], list[np.ndarray], list[np.ndarray]]] = {k: ([], [], []) for k in runs}
    longest: list[tuple[int, str]] = []
    t0 = time.time()
    for k, s in enumerate(syms):
        h1 = load_1h(cache, data_dir, s)
        if len(h1) < 500:
            continue
        longest.append((int(indicators(resample(h1, 4), p).above_run.max()), s))
        for r in runs:
            ev, (bd, bh) = run_h5(s, h1, member, p, r)
            acc[r][0].extend(ev)
            acc[r][1].append(bd)
            acc[r][2].append(bh)
        if (k + 1) % 50 == 0 or k + 1 == len(syms):
            emit(f"  [h5x] {k + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    emit("# H5, дослідницький прогін (поріг серії обрано після нульової кількості подій за реєстрацією)")
    emit(f"- найдовші серії 4h-закриттів над EMA21 (барів): {sorted(longest, reverse=True)[:10]}")
    emit(f"- символів із серією ≥ 120 барів: {sum(1 for x, _ in longest if x >= 120)} з {len(longest)}")
    for r in runs:
        emit(f"\n## серія ≥ {r} барів ({r // 6} днів)")
        h5_report(acc[r][0], np.concatenate(acc[r][1]), np.concatenate(acc[r][2]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "universe", "fetch", "count", "report", "h5x"), required=True)
    ap.add_argument("--limit", type=int, help="fetch only this many files (timing sample)")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cache, data_dir = a.cache_dir.expanduser(), a.data_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "universe":
        stage_universe(cache, data_dir, a.limit)
    elif a.stage == "fetch":
        stage_fetch(cache, data_dir, a.limit)
    elif a.stage == "count":
        stage_count(cache, data_dir)
    elif a.stage == "h5x":
        stage_h5x(cache, data_dir)
    else:
        stage_report(cache, data_dir)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
