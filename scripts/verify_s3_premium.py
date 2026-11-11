"""S3: perp premium on the sweep candle as a weight for the score (IDEA-20261030-demo-setups-depth, item «S3, премія
перпа на свічці свипу»; registration «Попередня реєстрація — S3, премія перпа на свічці свипу»).

    uv run python scripts/verify_s3_premium.py --stage coverage [--symbols BTCUSDT,ETHUSDT]  # fetch + coverage, no R
    uv run python scripts/verify_s3_premium.py --stage features                              # feature per candidate, no R
    uv run python scripts/verify_s3_premium.py --stage run [--dry]                           # MDE80 gate, then the test

Network only in ``coverage``, through ``rt.binance_vision`` (data.binance.vision, USDT-M ``premiumIndexKlines`` 1h,
monthly files; the archive keeps delisted symbols). Per-file JSON cache under ``<cache-dir>``: ``listing/<SYM>.json``
(months in the archive), ``pi1h/<SYM>/<YYYY-MM>.json`` (rows ``[open_ms, high, low, close]``, ``null`` = 404),
``funding/BTCUSDT/<YYYY-MM>.json`` (the consistency check). Candidates and outcomes come from the S3 passport cache
(``<passport-cache>/S3/cands``, rule B, demo costs); 1h perp prices from ``course_s14_ema/k1h``. Every stage writes its
output to ``<cache-dir>/<stage>.txt``. R is per trade risk, not on capital.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any

import common_helpers as cmn
import numpy as np
import verify_setup_passport as vsp  # passport rows, masks, chain, loaders (read-only reuse)
from common_helpers import rss_mb

from tradedesk.analytics import premium_state as ps
from tradedesk.analytics import range_efficiency as rf
from tradedesk.analytics.stop_management import cochran_q
from tradedesk.core.errors import ProviderError

SETUP = "S3"
SEED = 20261030
N_BOOT = 10_000
N_BOOT_RHO = 2_000
MDE_GATE = 0.10  # registered: the interesting T1 - T3 difference, R (same family as the ER test)
HOME = Path.home() / ".cache/tradedesk-agents"
FEATS = ("z_ext", "z_close", "zr_ext", "lvl_ext")
SHIFTS_REG = (30, -30)  # registered placebo, days
SHIFTS_BAND = tuple(s * d for d in range(60, 361, 30) for s in (1, -1))  # descriptive null band, 22 shifts
SHIFT_KEYS = tuple(f"sh{d:+d}" for d in SHIFTS_REG + SHIFTS_BAND)
FIRST_NEED_MS = vsp.PASSPORT_MS - 32 * ps.DAY_MS
OUT: dict[str, Any] = {"fh": None}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def ym(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, UTC)
    return f"{d.year:04d}-{d.month:02d}"


def months_between(a: str, b: str) -> list[str]:
    y, m = map(int, a.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def cand_times(cands: Path, s: str) -> np.ndarray:
    """Entry times of the symbol's S3 candidates (the outcome columns of the file are not read)."""
    rows = json.loads((cands / f"{s}.json").read_text()).get("rows", [])
    return np.array([int(r["t_in"]) for r in rows], dtype=np.int64)


# ---------------------------------------------------------------- fetch (coverage stage only)


class Fetcher:
    """Per-file JSON cache; ``null`` marks a file the archive does not have (404). Transient errors stay uncached."""

    def __init__(self, cache: Path, workers: int) -> None:
        self.cache = cache
        self.sem = asyncio.Semaphore(workers)
        self.done = 0
        self.errors: Counter[str] = Counter()

    async def one(self, path: Path, go: Callable[[], Awaitable[Any]]) -> None:
        async with self.sem:
            data = None
            for k in range(4):
                try:
                    data = await go()
                    break
                except ProviderError as e:
                    if e.reason == "data_unavailable":
                        data = None
                        break
                    if k == 3:
                        self.errors[e.reason] += 1
                        if self.errors[e.reason] <= 3:
                            emit(f"  error {path.relative_to(self.cache)}: {e.reason}: {str(e.detail)[:200]}")
                        return
                    await asyncio.sleep(5 * (k + 1))
        await asyncio.to_thread(cmn.write_json, path, data)
        self.done += 1

    async def many(self, label: str, jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]]) -> None:
        todo = [(p, g) for p, g in jobs if not p.exists()]
        emit(f"[{label}] {len(jobs)} files, {len(todo)} to fetch")
        t0 = time.time()
        self.done = 0
        step = max(50, len(todo) // 20)

        async def run(p: Path, g: Callable[[], Awaitable[Any]]) -> None:
            await self.one(p, g)
            if self.done and self.done % step == 0:
                el = time.time() - t0
                emit(
                    f"  [{label}] {self.done}/{len(todo)} in {el:.0f}s, eta {el / self.done * (len(todo) - self.done):.0f}s; RSS {rss_mb():.0f} MB"
                )

        await asyncio.gather(*[run(p, g) for p, g in todo])
        emit(f"[{label}] done in {time.time() - t0:.0f}s; transient errors: {dict(self.errors)}")


async def fetch_all(a: argparse.Namespace, out: Path, syms: list[str], cands: Path) -> None:
    import logging

    from tradedesk.server import build_server

    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    _, rt = build_server()
    try:
        v = rt.binance_vision
        fx = Fetcher(out, workers=a.workers_net)
        jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]] = []
        for s in syms:

            async def lst(s: str = s) -> Any:
                keys = (await v.list_dir(f"data/futures/um/monthly/premiumIndexKlines/{s}/1h/")).data["keys"]
                return sorted({k.rsplit("-", 2)[-2] + "-" + k.rsplit("-", 1)[-1][:2] for k in keys if k.endswith(".zip")})

            jobs.append((out / "listing" / f"{s}.json", lst))
        await fx.many("listing premiumIndexKlines 1h", jobs)
        jobs = []
        for s in syms:
            need = needed_months(cands, s)
            lp = out / "listing" / f"{s}.json"
            have = set(cmn.read_json(lp) or []) if lp.exists() else set()
            for mo in need:
                if mo not in have:
                    continue
                y, m = map(int, mo.split("-"))

                async def go(s: str = s, y: int = y, m: int = m) -> Any:
                    rows = (await v.um_premium_index_monthly(s, "1h", y, m)).data
                    return [[int(r[0]), r[2], r[3], r[4]] for r in rows]  # open, high, low, close

                jobs.append((out / "pi1h" / s / f"{mo}.json", go))
        await fx.many("premiumIndexKlines 1h", jobs)
        jobs = []
        for mo in months_between("2023-01", "2023-12"):
            y, m = map(int, mo.split("-"))

            async def fr(y: int = y, m: int = m) -> Any:
                return (await v.um_funding_monthly("BTCUSDT", y, m)).data

            jobs.append((out / "funding" / "BTCUSDT" / f"{mo}.json", fr))
        await fx.many("BTCUSDT funding 2023 (consistency check)", jobs)
    finally:
        await rt.aclose()


def needed_months(cands: Path, s: str) -> list[str]:
    t = cand_times(cands, s)
    t = t[t >= vsp.PASSPORT_MS]
    if not t.size:
        return []
    return months_between(ym(max(int(t.min()) - 32 * ps.DAY_MS, FIRST_NEED_MS)), ym(int(t.max())))


def load_prem(out: Path, s: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rows: list[list[float]] = []
    d = out / "pi1h" / s
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        rows.extend(cmn.read_json(p) or [])
    if not rows:
        z = np.zeros(0)
        return z.astype(np.int64), z, z, z
    arr = np.array(rows, dtype=float)
    arr = arr[np.argsort(arr[:, 0], kind="stable")]
    arr = arr[np.concatenate([[True], arr[1:, 0] != arr[:-1, 0]])]
    arr = arr[arr[:, 0] % ps.HOUR_MS == 0]
    return arr[:, 0].astype(np.int64), arr[:, 1].copy(), arr[:, 2].copy(), arr[:, 3].copy()


# ---------------------------------------------------------------- stages


def stage_coverage(a: argparse.Namespace, out: Path) -> None:
    cands = Path(a.passport_cache) / SETUP / "cands"
    all_syms = sorted(p.stem for p in cands.glob("*.json"))
    syms = a.symbols.split(",") if a.symbols else all_syms
    emit(f"S3 candidate symbols {len(all_syms)}; this run fetches {len(syms)}")
    t0 = time.monotonic()
    asyncio.run(fetch_all(a, out, syms, cands))
    emit(f"fetch wall time {time.monotonic() - t0:.0f} s; peak RSS {rss_mb():.0f} MB")
    if a.symbols:
        emit("sample run: coverage tables need the full symbol list")
        return

    # ---- symbols and months (no outcome)
    listed = {s: (cmn.read_json(out / "listing" / f"{s}.json") or []) for s in syms if (out / "listing" / f"{s}.json").exists()}
    no_list = [s for s in syms if not listed.get(s)]
    need_tot = got = null = 0
    first_year: Counter[str] = Counter()
    for s in syms:
        need = needed_months(cands, s)
        need_tot += len(need)
        for mo in need:
            p = out / "pi1h" / s / f"{mo}.json"
            if p.exists():
                got += 1 if cmn.read_json(p) else 0
                null += 0 if cmn.read_json(p) else 1
        if listed.get(s):
            first_year[listed[s][0][:4]] += 1
    emit(f"symbols with any premiumIndexKlines 1h month in the archive: {len(syms) - len(no_list)} of {len(syms)}; none: {no_list[:20]}")
    emit(f"first archive month by year: {sorted(first_year.items())}")
    emit(
        f"needed symbol-months (2020-12 .. last candidate, per symbol): {need_tot}; with data {got}; 404 {null}; not listed {need_tot - got - null}"
    )

    # ---- hourly gaps, units, OHLC sanity, per-year premium quantiles
    gaps = tot = bad = 0
    by_year: dict[str, list[np.ndarray]] = {}
    for s in syms:
        t, h, lo, c = load_prem(out, s)
        if not t.size:
            continue
        mo = np.array([ym(int(x)) for x in t[:: 24 * 7]])  # weekly sample of months for the span
        span = (int(t[-1]) - int(t[0])) // ps.HOUR_MS + 1
        tot += t.size
        gaps += span - t.size
        bad += int(np.sum((h < c - 1e-12) | (lo > c + 1e-12)))
        yrs = np.array([x[:4] for x in mo])
        for y in np.unique(yrs):
            by_year.setdefault(str(y), []).append(c[:: 24 * 7][yrs == y])
    emit(
        f"premium hours {tot}; hours missing inside each symbol's span {gaps} ({gaps / max(1, tot + gaps):.2%}); close outside [low, high] {bad}"
    )
    emit("premium close (weekly sample, all symbols) p1 / p50 / p99 by year (decimal; 0.0001 = 0.01 %):")
    for y in sorted(by_year):
        v = np.concatenate(by_year[y])
        q = np.quantile(v, [0.01, 0.5, 0.99])
        emit(f"  {y}: n {v.size}, {q[0]:+.5f} / {q[1]:+.5f} / {q[2]:+.5f}")

    # ---- known number: BTCUSDT funding 2023 from the hourly premium
    t, h, lo, c = load_prem(out, "BTCUSDT")
    fr: list[list[float]] = []
    for p in sorted((out / "funding" / "BTCUSDT").glob("*.json")):
        fr.extend(cmn.read_json(p) or [])
    pred, act = [], []
    for ct, _ih, rate in fr:
        end = int(ct) // ps.HOUR_MS * ps.HOUR_MS
        k = np.searchsorted(t, np.arange(end - 8 * ps.HOUR_MS, end, ps.HOUR_MS))
        if np.all(k < t.size) and np.array_equal(t[np.minimum(k, t.size - 1)], np.arange(end - 8 * ps.HOUR_MS, end, ps.HOUR_MS)):
            w = np.arange(1, 9, dtype=float)
            pred.append(ps.implied_funding(float(np.sum(w * c[k]) / w.sum())))
            act.append(float(rate))
    pr, ac = np.array(pred), np.array(act)
    off = np.abs(ac - 0.0001) > 1e-7
    emit(
        f"known number (Binance funding = P + clamp(0.01 % - P, ±0.05 %), P ≈ weighted mean of 8 hourly premium closes), BTCUSDT 2023: "
        f"settlements {ac.size}, |pred - actual| < 0.002 %: {np.mean(np.abs(pr - ac) < 2e-5):.1%}; "
        f"settlements off 0.01 %: {int(off.sum())}, corr there {np.corrcoef(pr[off], ac[off])[0, 1] if off.sum() > 2 else math.nan:+.3f}"
    )

    # ---- main sample rows with the sweep month covered (proxy; the exact count is in --stage features)
    M = passport_masks(a)
    T, main = M["T"], M["main"]
    emit(f"main sample (known 21 235 / OOS 7 006): 2021+ {int(main.sum())}, OOS {int((main & M['OOS']).sum())}")
    cov = np.array(
        [
            bool(m)
            and (out / "pi1h" / str(s) / f"{ym(int(ti) - 1)}.json").exists()
            and bool(cmn.read_json(out / "pi1h" / str(s) / f"{ym(int(ti) - 1)}.json"))
            for s, ti, m in zip(T["sym"], T["t_in"], main, strict=True)
        ]
    )
    emit("main sample by year: rows / sweep month covered / symbols / symbols covered")
    for y in range(2021, 2027):
        m = main & (T["year"] == y)
        emit(
            f"  {y}: {int(m.sum())} / {int((m & cov).sum())} ({(m & cov).sum() / max(1, m.sum()):.1%}) / "
            f"{np.unique(T['sym'][m]).size} / {np.unique(T['sym'][m & cov]).size}"
        )
    for nm, mm in (("IS", main & M["IS"]), ("OOS", main & M["OOS"])):
        emit(f"  {nm}: {int(mm.sum())} rows, covered {int((mm & cov).sum())} ({(mm & cov).sum() / max(1, mm.sum()):.1%})")
    emit(f"coverage: no outcome (R) computed; peak RSS {rss_mb():.0f} MB")


def symbol_features(args: tuple[str, str, str, str, str]) -> dict[str, Any]:
    """All features of every S3 candidate row of one symbol, in the row order of its cands file (no outcome read)."""
    s, cands_dir, s1_cache, data_dir, out_dir = args
    t0 = time.time()
    rows = json.loads((Path(cands_dir) / f"{s}.json").read_text()).get("rows", [])
    h1, _ = vsp.load_1h_qv(Path(s1_cache), Path(data_dir), s)
    price = (h1.t, h1.low, h1.h)
    prem = load_prem(Path(out_dir), s)
    res: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    for r in rows:
        t_in, tf, side = int(r["t_in"]), r["tf"], r["side"]
        rec: dict[str, Any] = {"t_in": t_in, "tf": tf, "side": side}
        if t_in < vsp.PASSPORT_MS:
            res.append({**rec, **dict.fromkeys(FEATS + SHIFT_KEYS), "why": "pre2021"})
            continue
        nh = vsp.HOURS[tf]
        start = t_in - nh * ps.HOUR_MS
        w = ps.wick_hour(h1.t, h1.low, h1.h, start, nh, side)
        f = {
            "z_ext": ps.sweep_premium(price, prem, start, nh, side),
            "z_close": ps.sweep_premium(price, prem, start, nh, side, stat="close"),
            "zr_ext": ps.sweep_premium(price, prem, start, nh, side, mode="robust"),
            "lvl_ext": ps.sweep_premium(price, prem, start, nh, side, mode="level"),
        }
        for d, k in zip(SHIFTS_REG + SHIFTS_BAND, SHIFT_KEYS, strict=True):
            f[k] = ps.sweep_premium(price, prem, start, nh, side, shift_ms=d * ps.DAY_MS)
        if w < 0:
            rec["why"] = "no_wick_hour"
        elif not math.isfinite(f["lvl_ext"]):
            rec["why"] = "no_premium_bar"
        elif not math.isfinite(f["z_ext"]):
            rec["why"] = "short_prior"
        else:
            rec["why"] = "ok"
        why[rec["why"]] += 1
        res.append({**rec, **{k: (v if math.isfinite(v) else None) for k, v in f.items()}})
    (Path(out_dir) / "features").mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "features" / f"{s}.json").write_text(json.dumps({"rows": res}))
    return {"sym": s, "n": len(rows), "why": dict(why), "sec": time.time() - t0, "rss": rss_mb()}


def load_features(cache: Path, cands: Path) -> dict[str, np.ndarray]:
    """Feature arrays aligned with ``vsp.load_rows`` (same sorted file order, same row order; keys checked)."""
    keys_all = FEATS + SHIFT_KEYS
    vals: dict[str, list[float]] = {k: [] for k in keys_all}
    keys: list[str] = []
    why: list[str] = []
    for p in sorted(cands.glob("*.json")):
        fp = cache / "features" / p.name
        rows = json.loads(fp.read_text())["rows"] if fp.exists() else None
        n_src = len(json.loads(p.read_text()).get("rows", []))
        if rows is None or len(rows) != n_src:
            raise SystemExit(f"features missing or misaligned for {p.stem}: run --stage features")
        for r in rows:
            keys.append(f"{r['t_in']}|{r['tf']}|{r['side']}")
            why.append(r["why"])
            for k in keys_all:
                vals[k].append(math.nan if r[k] is None else float(r[k]))
    out: dict[str, np.ndarray] = {k: np.asarray(v, dtype=float) for k, v in vals.items()}
    out["_keys"] = np.array(keys, dtype=object)
    out["_why"] = np.array(why, dtype=object)
    return out


def passport_masks(a: argparse.Namespace) -> dict[str, Any]:
    """The S3 passport rows, the registered main sample (as in the ER test) and month clusters (no outcome)."""
    T = vsp.load_rows(Path(a.passport_cache) / SETUP)
    floor = vsp.SETUPS[SETUP][2]
    period = T["t_in"] >= vsp.PASSPORT_MS
    sel = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= floor
    main = vsp.chain(T, period & sel & turn5)
    oos = T["t_in"] >= vsp.OOS_MS
    month = np.array([(lambda d: d.year * 12 + d.month - 1)(datetime.fromtimestamp(t / 1000, UTC)) for t in T["t_in"]])
    return {"T": T, "main": main, "OOS": oos, "IS": ~oos, "month": month, "sel": sel, "period": period, "turn5": turn5}


def aligned(a: argparse.Namespace, out: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    cands = Path(a.passport_cache) / SETUP / "cands"
    M = passport_masks(a)
    F = load_features(out, cands)
    T = M["T"]
    keys = np.array([f"{x}|{y}|{z}" for x, y, z in zip(T["t_in"], T["tf"], T["side"], strict=True)], dtype=object)
    if not np.array_equal(keys, F["_keys"]):
        raise SystemExit("features misaligned with passport rows")
    return M, F


def cuts_by_tf(x: np.ndarray, T: dict[str, np.ndarray], is_m: np.ndarray) -> tuple[np.ndarray, np.ndarray, dict[str, tuple[float, float]]]:
    lo = np.full(x.size, math.nan)
    hi = np.full(x.size, math.nan)
    cuts = {}
    for tf in ("4h", "1d"):
        c = rf.tertile_cuts(x[is_m & (T["tf"] == tf)])
        cuts[tf] = (round(c[0], 4), round(c[1], 4))
        lo[T["tf"] == tf], hi[T["tf"] == tf] = c
    return lo, hi, cuts


def stage_features(a: argparse.Namespace, out: Path) -> None:
    cands = Path(a.passport_cache) / SETUP / "cands"
    syms = sorted(p.stem for p in cands.glob("*.json"))
    jobs = [(s, str(cands), a.s1_cache, a.data_dir, str(out)) for s in syms if a.force or not (out / "features" / f"{s}.json").exists()]
    emit(f"features: {len(jobs)} of {len(syms)} symbols to compute")
    t0 = time.monotonic()
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(symbol_features, jobs), 1):
            if k % 25 == 0 or k == len(jobs):
                emit(
                    f"  {k}/{len(jobs)} {r['sym']} {time.monotonic() - t0:.0f} s; worker peak RSS {r['rss']:.0f} MB; parent {rss_mb():.0f} MB"
                )
    M, F = aligned(a, out)
    T, main, IS, OOS, month = M["T"], M["main"], M["IS"], M["OOS"], M["month"]
    x = F["z_ext"]
    emit(f"alignment with passport rows: ok ({x.size} rows)")
    emit("main sample: defined z_ext and reasons, by year (no outcome)")
    for y in range(2021, 2027):
        m = main & (T["year"] == y)
        emit(f"  {y}: n {int(m.sum())}, defined {int((m & np.isfinite(x)).sum())}; {dict(Counter(F['_why'][m].tolist()))}")
    for nm, mm in (("IS", main & IS), ("OOS", main & OOS)):
        emit(f"  {nm}: n {int(mm.sum())}, defined {int((mm & np.isfinite(x)).sum())}")
    emit("feature distribution, main 2021+ (p5/25/50/75/95):")
    for f in FEATS:
        for tf in ("4h", "1d"):
            v = F[f][main & (T["tf"] == tf)]
            v = v[np.isfinite(v)]
            emit(f"  {f} {tf}: n {v.size}; " + " / ".join(f"{q:+.4f}" for q in np.quantile(v, [0.05, 0.25, 0.5, 0.75, 0.95])))
    lo, hi, cuts = cuts_by_tf(x, T, main & IS)
    k = rf.tertile_codes(x, lo, hi)
    emit(f"IS tertile cuts of z_ext (registered, applied to OOS): {cuts}")
    emit(f"OOS T1 {int((main & OOS & (k == 1)).sum())} / T2 {int((main & OOS & (k == 2)).sum())} / T3 {int((main & OOS & (k == 3)).sum())}")
    both = [
        mo
        for mo in np.unique(month[main & OOS])
        if ((main & OOS & (month == mo) & (k == 1)).any() and (main & OOS & (month == mo) & (k == 3)).any())
    ]
    emit(f"OOS months {np.unique(month[main & OOS]).size}, with both T1 and T3 {len(both)}")
    emit("Spearman of z_ext with non-outcome columns, main 2021+:")
    for c in ("stop_pct", "rr", "score"):
        emit(f"  z_ext ~ {c}: {rf.spearman(x[main], np.asarray(T[c][main], dtype=float)):+.3f}")
    emit("  z_ext ~ " + ", ".join(f"{f} {rf.spearman(x[main], F[f][main]):+.3f}" for f in FEATS[1:]))
    erp = Path(a.er_cache) / "features"
    if erp.exists():
        import verify_s3_range_efficiency as ver

        er = ver.load_features(Path(a.er_cache), cands)["er30"]
        emit(f"  z_ext ~ ER30 (the sibling feature): {rf.spearman(x[main], er[main]):+.3f}")
    for d in SHIFTS_REG:
        xs = F[f"sh{d:+d}"]
        emit(
            f"  placebo shift {d:+d} d: defined in main OOS {int((main & OOS & np.isfinite(xs)).sum())}; z_ext ~ shifted {rf.spearman(x[main], xs[main]):+.3f}"
        )
    emit(f"features: no outcome (R) computed; {time.monotonic() - t0:.0f} s; peak RSS {rss_mb():.0f} MB")


def fd(r: dict[str, float]) -> str:
    return f"{r['est']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] (n {r.get('n_a', r.get('n', ''))}/{r.get('n_b', '')}, p1 {r.get('p', math.nan):.3f})"


def delta(y: np.ndarray, k: np.ndarray, m: np.ndarray, cl: np.ndarray, n_boot: int = N_BOOT) -> dict[str, float]:
    ok = m & np.isfinite(y)
    return rf.boot_diff(y[ok], (k == 1)[ok], (k == 3)[ok], cl[ok], n_boot=n_boot, seed=SEED)


def shifted_delta(F: dict[str, np.ndarray], key: str, T: dict[str, np.ndarray], B: np.ndarray, M: dict[str, Any]) -> dict[str, float]:
    xs = F[key]
    lo, hi, _ = cuts_by_tf(xs, T, M["main"] & M["IS"])
    return delta(B, rf.tertile_codes(xs, lo, hi), M["main"] & M["OOS"], M["month"], n_boot=1_000)


def stage_run(a: argparse.Namespace, out: Path) -> None:
    M, F = aligned(a, out)
    T, main, IS, OOS, month = M["T"], M["main"], M["IS"], M["OOS"], M["month"]
    if a.dry:  # code check without the outcome: R columns replaced by seeded noise, the gate is not applied
        rng = np.random.default_rng(1)
        for col in ("B", "B15", "Bx"):
            T[col] = rng.normal(0.0, 1.0, T[col].size)
        emit("DRY RUN: B / B15 / Bx are random noise, nothing below is a result")
    B = T["B"]
    x = F["z_ext"]
    emit(f"main sample: 2021+ {int(main.sum())}, IS {int((main & IS).sum())}, OOS {int((main & OOS).sum())} (known 21 235 / 7 006)")
    emit(f"z_ext defined: IS {int((main & IS & np.isfinite(x)).sum())}, OOS {int((main & OOS & np.isfinite(x)).sum())}")
    lo, hi, cuts = cuts_by_tf(x, T, main & IS)
    k = rf.tertile_codes(x, lo, hi)
    emit(
        f"z_ext IS cuts: {cuts}; OOS T1 {int((main & OOS & (k == 1)).sum())}, T2 {int((main & OOS & (k == 2)).sum())}, T3 {int((main & OOS & (k == 3)).sum())}"
    )

    emit("\n== Gate MDE80 (T1 - T3, cluster-robust SE) ==")
    gate = {}
    for nm, m, cl in (("OOS, months (gate)", main & OOS, month), ("IS, months", main & IS, month), ("OOS, weeks", main & OOS, T["week"])):
        ok = m & np.isfinite(B)
        se = rf.cluster_se_diff(B[ok], (k == 1)[ok], (k == 3)[ok], cl[ok])
        gate[nm] = rf.mde80(se)
        emit(f"  {nm}: clusters {np.unique(cl[ok & ((k == 1) | (k == 3))]).size}, SE {se:.4f}, MDE80 {rf.mde80(se):.4f} R")
    if gate["OOS, months (gate)"] > MDE_GATE and not a.dry:
        emit(f"\nGATE FAILED: MDE80 {gate['OOS, months (gate)']:.4f} > {MDE_GATE} R -> verdict ◯ (unmeasurable); no means computed")
        return
    emit(f"gate passed (or dry): MDE80 {gate['OOS, months (gate)']:.4f} vs {MDE_GATE} R")

    emit("\n== H1: Δ = E[B|T1] - E[B|T3] (bootstrap over months, 10 000) ==")
    h1 = delta(B, k, main & OOS, month)
    h1_is = delta(B, k, main & IS, month)
    emit(f"  OOS (primary): {fd(h1)}; Holm on 2 with ER30 (ER stopped at its gate, no p): p1 x2 {min(1.0, 2 * h1['p']):.3f}")
    emit(f"  IS:            {fd(h1_is)}")
    rho = {}
    for nm, m in (("IS", main & IS), ("OOS", main & OOS)):
        mm = m & np.isfinite(x) & np.isfinite(B)
        p = rf.within_percentile(x[mm], T["tf"][mm])
        rho[nm] = rf.boot_spearman(p, B[mm], month[mm], n_boot=N_BOOT_RHO, seed=SEED)
        emit(f"  Spearman ρ(z pct within tf, B) {nm}: {rho[nm]['est']:+.4f} [{rho[nm]['lo']:+.4f}; {rho[nm]['hi']:+.4f}] n {rho[nm]['n']}")
    emit("  E[B] by tertile (bootstrap over months):")
    for nm, m in (("IS", main & IS), ("OOS", main & OOS)):
        cells = []
        for t in (1, 2, 3):
            mm = m & (k == t) & np.isfinite(B)
            r = rf.boot_mean(B[mm], month[mm], n_boot=N_BOOT, seed=SEED)
            cells.append(f"T{t} {r['est']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}] n {r['n']}")
        emit(f"    {nm}: " + " | ".join(cells))

    emit("\n== Placebo: premium series shifted (wick hour from the real candle; IS cuts recomputed per shift) ==")
    reg = {key: shifted_delta(F, key, T, B, M) for key in (f"sh{d:+d}" for d in SHIFTS_REG)}
    for key, r in reg.items():
        emit(f"  {key} d (registered): Δ OOS {fd(r)}")
    pl_pass = all(h1["est"] > r["est"] for r in reg.values())
    emit(f"  placebo passed (Δ real > both registered shifts): {pl_pass}")
    band = [shifted_delta(F, f"sh{d:+d}", T, B, M)["est"] for d in SHIFTS_REG + SHIFTS_BAND]
    emit(
        f"  null band ±30 … ±360 d ({len(band)} shifts): share with Δ >= real {np.mean(np.array(band) >= h1['est']):.3f}; p5/p50/p95 "
        + " / ".join(f"{q:+.3f}" for q in np.quantile(band, [0.05, 0.5, 0.95]))
    )

    if h1["est"] > 0 and h1["lo"] > 0 and h1_is["est"] > 0 and pl_pass:
        verdict = "✓ weight useful"
    elif h1["hi"] < 0:
        verdict = "✗ opposite sign"
    elif h1["hi"] < MDE_GATE:
        verdict = "✗ useful effect excluded (CI upper < 0.10R)"
    elif h1["est"] > 0 and h1_is["est"] > 0 and rho["IS"]["est"] < 0 and rho["OOS"]["est"] < 0:
        verdict = "≈ direction right, not significant"
    else:
        verdict = "◯ inconclusive"
    emit(f"\nVERDICT (registered rule): {verdict}")

    emit("\n== Descriptive: other feature variants (same pipeline, IS cuts per tf) ==")
    for f in FEATS[1:]:
        lo_f, hi_f, _ = cuts_by_tf(F[f], T, main & IS)
        kf = rf.tertile_codes(F[f], lo_f, hi_f)
        emit(f"  {f}: OOS {fd(delta(B, kf, main & OOS, month))} | IS {fd(delta(B, kf, main & IS, month))}")
    emit("\n== Descriptive: costs and stop rule ==")
    for col in ("B15", "Bx"):
        emit(f"  {col}: OOS {fd(delta(T[col], k, main & OOS, month))} | IS {fd(delta(T[col], k, main & IS, month))}")
    emit("\n== Descriptive: splits ==")
    for nm, m in (("4h", T["tf"] == "4h"), ("1d", T["tf"] == "1d"), ("long", T["side"] == "long"), ("short", T["side"] == "short")):
        emit(f"  {nm}: OOS {fd(delta(B, k, main & OOS & m, month))} | IS {fd(delta(B, k, main & IS & m, month))}")
    for y in range(2021, 2027):
        emit(f"  {y}: {fd(delta(B, k, main & (T['year'] == y), month))}")
    emit("\n== Descriptive: without the 5 coins with the largest contribution to Δ (OOS) ==")
    mm = main & OOS & np.isfinite(B)
    n1, n3 = max(1, int((mm & (k == 1)).sum())), max(1, int((mm & (k == 3)).sum()))
    contrib: dict[str, float] = {}
    for s, yv, kk in zip(T["sym"][mm], B[mm], k[mm], strict=True):
        if kk in (1, 3):
            contrib[str(s)] = contrib.get(str(s), 0.0) + (yv / n1 if kk == 1 else -yv / n3)
    top5 = sorted(contrib, key=lambda s: -contrib[s])[:5]
    emit(
        f"  top-5 {[(s, round(contrib[s], 4)) for s in top5]}; without them: {fd(delta(B, k, main & OOS & ~np.isin(T['sym'], top5), month))}"
    )
    for col in ("stop_pct", "rr"):
        lo_s, hi_s, _ = cuts_by_tf(T[col].astype(float), T, main & IS)
        ks = rf.tertile_codes(T[col].astype(float), lo_s, hi_s)
        ests, ws = [], []
        for t in (1, 2, 3):
            r = delta(B, k, main & OOS & (ks == t), month)
            ests.append(r["est"])
            ws.append(r["n_a"] + r["n_b"])
            emit(f"  inside {col} tertile {t}: OOS {fd(r)}")
        emit(f"  weighted mean of within-{col} Δ (OOS): {np.average(ests, weights=ws):+.4f}")
    emit("\n== 5a: per coin ==")
    per_asset(T, B, k, main, IS, OOS, month)
    emit(f"\npeak RSS {rss_mb():.0f} MB")


def coin_deltas(
    T: dict[str, np.ndarray], B: np.ndarray, k: np.ndarray, m: np.ndarray, min_n: int = 5
) -> dict[str, tuple[float, float, int]]:
    out = {}
    for s in np.unique(T["sym"][m]):
        mm = m & (T["sym"] == s) & np.isfinite(B)
        x1, x3 = B[mm & (k == 1)], B[mm & (k == 3)]
        if x1.size >= min_n and x3.size >= min_n:
            out[str(s)] = (
                float(x1.mean() - x3.mean()),
                math.sqrt(x1.var(ddof=1) / x1.size + x3.var(ddof=1) / x3.size),
                int(x1.size + x3.size),
            )
    return out


def per_asset(
    T: dict[str, np.ndarray], B: np.ndarray, k: np.ndarray, main: np.ndarray, IS: np.ndarray, OOS: np.ndarray, month: np.ndarray
) -> None:
    allc = coin_deltas(T, B, k, main)
    if len(allc) < 2:
        emit("  fewer than 2 coins with >= 5 trades in T1 and T3")
        return
    q, df, i2 = cochran_q(np.array([v[0] for v in allc.values()]), np.array([v[1] for v in allc.values()]))
    emit(f"  coins with >= 5 trades in T1 and T3 (2021+): {len(allc)}; Cochran Q {q:.1f} df {df}, I² {i2:.2f}")
    top = sorted(allc.items(), key=lambda kv: -kv[1][0])
    emit("  best 5 / worst 5 by Δ (2021+): " + ", ".join(f"{s} {v[0]:+.2f} (n {v[2]})" for s, v in top[:5] + top[-5:]))
    isc = coin_deltas(T, B, k, main & IS)
    lead = [s for s, _ in sorted(isc.items(), key=lambda kv: -kv[1][0])[:10]]
    inl = np.isin(T["sym"], lead)
    emit(f"  IS leaders ({len(isc)} coins eligible on IS): {lead}")
    emit(f"  OOS Δ leaders: {fd(delta(B, k, main & OOS & inl, month))}")
    emit(f"  OOS Δ rest:    {fd(delta(B, k, main & OOS & ~inl, month))}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("coverage", "features", "run"), required=True)
    ap.add_argument("--cache-dir", default=str(HOME / "s3-premium"))
    ap.add_argument("--passport-cache", default=str(HOME / "setup_passport"))
    ap.add_argument("--s1-cache", default=str(HOME / "course_s14_ema"))
    ap.add_argument("--data-dir", default=str(HOME / "funding_crowding"))
    ap.add_argument("--er-cache", default=str(HOME / "s3-range-efficiency"))
    ap.add_argument("--symbols", default="", help="coverage: fetch only these symbols (timing sample)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--workers-net", type=int, default=12)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true", help="run: replace R with seeded noise (code check, no result)")
    a = ap.parse_args()
    out = Path(a.cache_dir)
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{a.stage}{'-dry' if a.dry else ''}{'-sample' if a.symbols else ''}"
    with (out / f"{tag}.txt").open("w") as fh:
        OUT["fh"] = fh
        emit(f"# verify_s3_premium --stage {a.stage} at {datetime.now(UTC).isoformat(timespec='seconds')}")
        {"coverage": stage_coverage, "features": stage_features, "run": stage_run}[a.stage](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
