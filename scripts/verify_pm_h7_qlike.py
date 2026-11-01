"""H7'' - weekend / hour-of-day profile on the paper's IV model, scored by QLIKE on realized variance (item 8l).

The pre-registration is in IDEA-20261031-structural-edges-depth, "Пункт 8l. Попередня реєстрація". Reads a read-only
``sqlite3 backup`` copy of the collector DB (``pm_gap_snapshot`` only; ``pm_gap_outcome`` is never read), the item-5 IS
profile (BTC 1h 2020-01 .. 2025-02) and, for the power replay, the BTC 1h cache 2025-03-01 .. 2026-10-12.

Stages:
    stage0  power gate from forecasts only plus a historical replay for the weekly SD; no test-period bars are loaded
    full    the registered test; refuses unless the stage-0 gate in <out-dir>/stage0.json passed; fetches Binance 1h
            spot BTC/ETH through the runtime providers (cached per day under <out-dir>/klines/)

Usage:
    uv run python scripts/verify_pm_h7_qlike.py --stage stage0 [--refresh-db] [--out FILE]
    uv run python scripts/verify_pm_h7_qlike.py --stage full [--n-boot 10000] [--out FILE]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import resource
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tradedesk.analytics import pm_h7_qlike as hq
from tradedesk.analytics import pm_modules as pm
from tradedesk.analytics.pm_threshold import DAY_S, HOUR_S

OUT = Path("~/.cache/tradedesk-agents/pm_h7_qlike").expanduser()
T_START = int(datetime(2026, 10, 20, tzinfo=UTC).timestamp())
W43_MON = datetime(2026, 10, 19, tzinfo=UTC)
REPLAY_LO = int(datetime(2025, 3, 1, tzinfo=UTC).timestamp())
REPLAY_HI = int(datetime(2026, 10, 13, tzinfo=UTC).timestamp())  # cache ends 2026-10-12
MIN_WEEKS = 20
SEED = 20261101
PLACEBO_DAYS = (1, 2, 3, 4, 5, 6)
C_GRID = (0.8, 1.0, 1.25)
WEEK_S = 7 * DAY_S
TRAIL_H = 168


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _load(name: str):  # noqa: ANN202
    p = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


VH = _load("verify_pm_h7_iv")  # copy_db, is_profile, week_of, weekend_window (item 8k)


def parse_expiry(s: str) -> int:
    e = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return int((e if e.tzinfo else e.replace(tzinfo=UTC)).timestamp())


def load_rows(db: Path, prof: dict) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    """Registered rows with their target window H and the item-8k multiplier s on H (forecasts only)."""
    cutoff = int(db.stat().st_mtime) - HOUR_S
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = (
        "SELECT ts, market_id, asset, kind, end_ts, model_iv, model_expiry, expiry_gap_hours FROM pm_gap_snapshot "
        "WHERE model_note = 'ok' AND model_covered = 1 AND asset IN ('BTC', 'ETH') AND model_iv > 0 "
        "AND model_expiry IS NOT NULL AND ts >= ? AND ts < end_ts AND end_ts <= ? ORDER BY market_id, ts"
    )
    rows, drop = [], defaultdict(int)
    for ts, mid, asset, kind, end, iv, mexp, gap in con.execute(q, (T_START, cutoff)):
        e = parse_expiry(mexp)
        if gap is not None and abs(e - (int(end) + float(gap) * HOUR_S)) > 60:
            drop["expiry mismatch"] += 1
        hw = hq.whole_hours(int(ts), int(end))
        if hw is None:
            drop["|H| < 1 h"] += 1
            continue
        s = hq.window_var_scale(prof, int(ts), hw[0], hw[1], e)
        if s is None:
            drop["no scale"] += 1
            continue
        rows.append(
            {
                "ts": int(ts),
                "mid": str(mid),
                "asset": str(asset),
                "kind": str(kind),
                "end": int(end),
                "h0": hw[0],
                "h1": hw[1],
                "e": e,
                "iv": float(iv),
                "s": s,
                "week": VH.week_of(int(end)),
            }
        )
    con.close()
    return rows, dict(drop), cutoff


def market_mean_stat(rows: list[dict[str, Any]], vals: list[float | None]) -> dict[str, list[float]]:
    """Per week: the list of market means of the row values (rows with None skipped)."""
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r, v in zip(rows, vals, strict=True):
        if v is not None:
            acc[(r["week"], r["mid"])].append(v)
    out: dict[str, list[float]] = defaultdict(list)
    for (w, _), v in acc.items():
        out[w].append(statistics.fmean(v))
    return out


def replay(rows: list[dict[str, Any]], s2: list[float]) -> dict[str, Any]:
    """Weekly statistic of dL on BTC history with the rows' geometry shifted back by whole weeks (registered SD)."""
    vm = _load("verify_pm_modules")
    rets = dict(vm.bar_returns(vm.load_btc_long(), REPLAY_LO, REPLAY_HI))
    n_h = (REPLAY_HI - REPLAY_LO) // HOUR_S
    pre_r2, pre_ok = [0.0] * (n_h + 1), [0] * (n_h + 1)
    for i in range(n_h):
        r = rets.get(REPLAY_LO + i * HOUR_S)
        pre_r2[i + 1] = pre_r2[i] + (r * r if r is not None else 0.0)
        pre_ok[i + 1] = pre_ok[i] + (r is not None)

    def seg(a: int, b: int) -> float | None:
        i, j = (a - REPLAY_LO) // HOUR_S, (b - REPLAY_LO) // HOUR_S
        if i < 0 or j > n_h or j <= i or pre_ok[j] - pre_ok[i] != j - i:
            return None
        return pre_r2[j] - pre_r2[i]

    max_h1, min_ts = max(r["h1"] for r in rows), min(r["ts"] for r in rows)
    k_lo = math.ceil((max_h1 - REPLAY_HI) / WEEK_S)
    k_hi = math.floor((min_ts - TRAIL_H * HOUR_S - HOUR_S - REPLAY_LO) / WEEK_S)
    vals_by_week: dict[str, list[float]] = defaultdict(list)
    all_rows: list[float] = []
    for k in range(k_lo, k_hi + 1):
        d = k * WEEK_S
        v: list[float | None] = []
        for r, s in zip(rows, s2, strict=True):
            t_floor = (r["ts"] - d) // HOUR_S * HOUR_S
            trail = seg(t_floor - TRAIL_H * HOUR_S, t_floor)
            y = seg(r["h0"] - d, r["h1"] - d)
            if trail is None or y is None or trail <= 0:
                v.append(None)
                continue
            h_f = trail / TRAIL_H * ((r["h1"] - r["h0"]) / HOUR_S)
            v.append(hq.qlike_diff(y, h_f, s))
        all_rows.extend(x for x in v if x is not None)
        for w, mm in market_mean_stat(rows, v).items():
            vals_by_week[w].append(statistics.fmean(mm))
    pooled = [x for v in vals_by_week.values() for x in v]
    return {
        "offsets": k_hi - k_lo + 1,
        "weeks_back": [k_lo, k_hi],
        "sd_week": statistics.stdev(pooled),
        "mean_week": statistics.fmean(pooled),
        "per_week": {w: {"n": len(v), "mean": statistics.fmean(v), "sd": statistics.stdev(v)} for w, v in vals_by_week.items()},
        "row_mean": statistics.fmean(all_rows),
        "rows_scored": len(all_rows),
    }


def stage0(db: Path, out: Path) -> dict[str, Any]:
    t0 = time.perf_counter()
    prof = VH.is_profile()
    rows, drop, cutoff = load_rows(db, prof)
    w = hq.market_weights([r["mid"] for r in rows])
    s_raw = [r["s"] for r in rows]
    s2, m = hq.renormalize(s_raw, w)
    markets = {r["mid"] for r in rows}
    weeks = sorted({r["week"] for r in rows})
    target = sum(wi * hq.expected_gain(s) for wi, s in zip(w, s2, strict=True))
    print(
        f"rows {len(rows)} (cutoff end <= {datetime.fromtimestamp(cutoff, UTC):%Y-%m-%d %H:%M}), markets {len(markets)}, "
        f"weeks {len(weeks)} {weeks}; dropped {drop}; peak rss {rss_mb():.0f} MB",
        flush=True,
    )
    per = defaultdict(set)
    for r in rows:
        per[f"{r['asset']}-{r['kind']}"].add(r["mid"])
    print("per asset/kind markets: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(per.items())), flush=True)
    hrs = [(r["h1"] - r["h0"]) / HOUR_S for r in rows]
    print(
        f"|H| hours: median {statistics.median(hrs):.0f}, p5 {sorted(hrs)[len(hrs) // 20]:.0f}, p95 {sorted(hrs)[19 * len(hrs) // 20]:.0f}",
        flush=True,
    )
    for lab, sel in (("no weekend", False), ("weekend", True)):
        ss = sorted(s for r, s in zip(rows, s2, strict=True) if VH.weekend_window(r["h0"], r["h1"]) is sel)
        if ss:
            print(
                f"  s' {lab}: rows {len(ss)} p5 {ss[len(ss) // 20]:.3f} median {ss[len(ss) // 2]:.3f} p95 {ss[19 * len(ss) // 20]:.3f}",
                flush=True,
            )
    qstar = hq.break_even_level(s_raw, w)
    lvl = {c: sum(wi * hq.expected_diff_level(s, c) for wi, s in zip(w, s_raw, strict=True)) for c in C_GRID}
    print(
        f"renorm m = {m:.4f}; [descr., unnormalised s] break-even level ratio q* = {qstar:.4f}; E[dL | profile right, q]: "
        + ", ".join(f"q={c} {v:+.5f}" for c, v in lvl.items()),
        flush=True,
    )
    rp = replay(rows, s2)
    n_w = len(weeks)
    mde = 2.8 * rp["sd_week"] / math.sqrt(n_w)
    need = hq.weeks_needed(rp["sd_week"], target)
    n_weeks_gate = max(MIN_WEEKS, need or 10**6)
    eta = W43_MON + timedelta(weeks=n_weeks_gate)
    ok = n_w >= MIN_WEEKS and mde <= target
    print(
        f"replay: {rp['offsets']} weekly offsets (weeks back {rp['weeks_back']}), rows scored {rp['rows_scored']}, "
        f"weekly stat SD {rp['sd_week']:.5f}, mean {rp['mean_week']:+.5f} [descr.], row mean {rp['row_mean']:+.5f}",
        flush=True,
    )
    for wk, v in sorted(rp["per_week"].items()):
        print(f"  replay composition {wk}: n {v['n']} mean {v['mean']:+.5f} sd {v['sd']:.5f}", flush=True)
    print(
        f"GATE: weeks {n_w} (min {MIN_WEEKS}), markets {len(markets)}, target D* = {target:.6f}, SD_week = {rp['sd_week']:.5f}, "
        f"MDE({n_w} wk) = {mde:.5f} -> pass={ok}; weeks needed for MDE <= D*: {need}; ETA (W43 + max(20, N*)) {eta:%Y-%m-%d}",
        flush=True,
    )
    res = {
        "rows": len(rows),
        "markets": len(markets),
        "weeks": weeks,
        "dropped": drop,
        "renorm_m": m,
        "q_star": qstar,
        "level_grid": lvl,
        "replay": rp,
        "gate": {"pass": ok, "target": target, "sd_week": rp["sd_week"], "mde": mde, "weeks_needed": need, "eta": f"{eta:%Y-%m-%d}"},
        "seconds": round(time.perf_counter() - t0, 1),
        "peak_rss_mb": round(rss_mb()),
    }
    out.write_text(json.dumps(res, indent=1, default=str))
    print(f"stage0 {res['seconds']}s peak rss {res['peak_rss_mb']} MB -> {out}", flush=True)
    return res


async def fetch_returns(out: Path, sym: str, lo: int, hi: int) -> dict[int, float]:
    """Binance spot 1h bars [lo - 1 h; hi) through the runtime providers, cached per UTC day; returns bar open -> r."""
    from tradedesk.server import build_server

    kdir = out / "klines"
    kdir.mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    closes: dict[int, float] = {}
    try:
        day = (lo - HOUR_S) // DAY_S * DAY_S
        while day < hi:
            f = kdir / f"{sym.replace('/', '')}-1h-{datetime.fromtimestamp(day, UTC):%Y-%m-%d}.json"
            if f.exists():
                data = json.loads(f.read_text())
            else:
                got = await rt.exchanges.ohlcv("binance", sym, "1h", 30, since_ms=day * 1000, until_ms=(day + DAY_S) * 1000 - 1)
                data = [b for b in (got.data or []) if day * 1000 <= b[0] < (day + DAY_S) * 1000]
                if day + DAY_S <= time.time() - HOUR_S:  # complete days only are cached
                    f.write_text(json.dumps(data))
                await asyncio.sleep(0.2)
            for b in data:
                closes[int(b[0]) // 1000] = float(b[4])
            print(f"  {sym} {datetime.fromtimestamp(day, UTC):%Y-%m-%d}: {len(data)} bars, peak rss {rss_mb():.0f} MB", flush=True)
            day += DAY_S
    finally:
        await rt.aclose()
    return {
        t: math.log(closes[t] / closes[t - HOUR_S]) for t in closes if t - HOUR_S in closes and closes[t] > 0 and closes[t - HOUR_S] > 0
    }


def boot(units: list[tuple[str, float]], n_boot: int) -> dict[str, Any]:
    blocks: dict[str, list[float]] = defaultdict(list)
    for b, v in units:
        blocks[b].append(v)
    return pm.block_bootstrap_ratio([(sum(v), len(v)) for v in blocks.values()], n_boot=n_boot, seed=SEED)


def fmt(r: dict[str, Any], k: float = 1e3) -> str:
    if r.get("mean") is None:
        return "—"
    return f"{r['mean'] * k:+.3f} [{r['lo'] * k:+.3f}; {r['hi'] * k:+.3f}] p={r['p']:.3f} n={r['n']} blocks={r['n_blocks']}"


def stage_full(db: Path, gate_file: Path, out: Path, n_boot: int) -> None:
    if not gate_file.exists() or not json.loads(gate_file.read_text())["gate"]["pass"]:
        raise SystemExit("stage-0 gate not passed (or not run): the registered test is not computed (item 8l waits)")
    prof = VH.is_profile()
    rows, drop, _ = load_rows(db, prof)
    lo, hi = min(r["h0"] for r in rows), max(r["h1"] for r in rows)
    rets = {a: asyncio.run(fetch_returns(gate_file.parent, f"{a}/USDT", lo, hi)) for a in ("BTC", "ETH")}
    ys = [hq.realized_var(rets[r["asset"]], r["h0"], r["h1"]) for r in rows]
    keep = [i for i, y in enumerate(ys) if y is not None]
    print(f"rows {len(rows)}, with complete bars {len(keep)}; dropped {drop}", flush=True)
    rows, ys = [rows[i] for i in keep], [ys[i] for i in keep]
    w = hq.market_weights([r["mid"] for r in rows])
    dl: dict[int, list[float]] = {}
    for kd in (0, *PLACEBO_DAYS):
        sk = [r["s"] if kd == 0 else hq.window_var_scale(prof, r["ts"], r["h0"], r["h1"], r["e"], 24 * kd) for r in rows]
        s2, _ = hq.renormalize(sk, w)  # type: ignore[arg-type]
        dl[kd] = [hq.qlike_diff(y, hq.flat_var(r["iv"], r["h0"], r["h1"]), s) for r, y, s in zip(rows, ys, s2, strict=True)]
    mk: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        m = mk.setdefault(r["mid"], {"asset": r["asset"], "kind": r["kind"], "week": r["week"], "wk": False, "v": defaultdict(list)})
        m["wk"] = m["wk"] or VH.weekend_window(r["h0"], r["h1"])
        for kd in dl:
            m["v"][kd].append(dl[kd][i])
    for m in mk.values():
        m["d"] = {kd: statistics.fmean(v) for kd, v in m["v"].items()}
        del m["v"]
    ml = list(mk.values())
    res: dict[str, Any] = {"n_markets": len(ml), "n_rows": len(rows)}
    res["primary"] = boot([(m["week"], m["d"][0]) for m in ml], n_boot)
    print(f"PRIMARY dQLIKE x1e3 (flat - profile, s' renormalised, week blocks): {fmt(res['primary'])}", flush=True)
    for lab, sel in (("weekend window", True), ("no weekend", False)):
        print(f"  {lab}: {fmt(boot([(m['week'], m['d'][0]) for m in ml if m['wk'] is sel], n_boot))}", flush=True)
    for kind in ("above", "touch"):
        print(f"  {kind}: {fmt(boot([(m['week'], m['d'][0]) for m in ml if m['kind'] == kind], n_boot))}", flush=True)
    res["placebo"] = {kd: statistics.fmean(m["d"][kd] for m in ml) for kd in PLACEBO_DAYS}
    print("  placebo k*24h x1e3: " + ", ".join(f"k={k} {v * 1e3:+.3f}" for k, v in res["placebo"].items()), flush=True)
    for c in C_GRID:  # descriptive, level-dependent
        v = [hq.qlike_diff(y, hq.flat_var(r["iv"], r["h0"], r["h1"]), r["s"], c) for r, y in zip(rows, ys, strict=True)]
        print(f"  [descr.] unnormalised s, c={c}: row-weighted {hq.weighted_mean(v, w) * 1e3:+.3f}", flush=True)
    weeks = sorted({m["week"] for m in ml})
    mid_w = weeks[len(weeks) // 2]
    res["halves"] = {}
    for lab, cond in (("1st", lambda x: x < mid_w), ("2nd", lambda x: x >= mid_w)):
        res["halves"][lab] = boot([(m["week"], m["d"][0]) for m in ml if cond(m["week"])], n_boot)
        print(f"  half {lab} (split at {mid_w}): {fmt(res['halves'][lab])}", flush=True)
    res["per_asset"] = {a: boot([(m["week"], m["d"][0]) for m in ml if m["asset"] == a], n_boot) for a in ("BTC", "ETH")}
    for a, r in res["per_asset"].items():
        print(f"  {a}: {fmt(r)}", flush=True)
    ok = [r for r in res["per_asset"].values() if r.get("se")]
    if len(ok) == 2:
        print(f"  cochran {pm.cochran_q([r['mean'] for r in ok], [r['se'] for r in ok])}", flush=True)
    out.write_text(json.dumps(res, indent=1, default=str))
    print(f"full done, peak rss {rss_mb():.0f} MB -> {out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("stage0", "full"), default="stage0")
    ap.add_argument("--cache-dir", default=str(OUT))
    ap.add_argument("--out", default=None, help="result JSON (default <cache-dir>/<stage>.json)")
    ap.add_argument("--refresh-db", action="store_true")
    ap.add_argument("--n-boot", type=int, default=10_000)
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    db = cache / "tradedesk_copy.sqlite"
    if a.refresh_db or not db.exists():
        t = time.perf_counter()
        VH.copy_db(db)
        print(f"db copy {time.perf_counter() - t:.0f}s -> {db}", flush=True)
    out = Path(a.out) if a.out else cache / f"{a.stage}.json"
    if a.stage == "stage0":
        stage0(db, out)
    else:
        stage_full(db, cache / "stage0.json", out, a.n_boot)


if __name__ == "__main__":
    main()
