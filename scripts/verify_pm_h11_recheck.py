"""H11 Polymarket re-run with a pre-registered fit-quality filter (IDEA-20261031-structural-edges-depth, item 8f).

The pre-registration is in the idea card ("Пункт 8f. Попередня реєстрація"). Only events with T >= 2026-10-07 UTC,
which the item-5 run (``verify_pm_modules.py``) never saw. Network only through the runtime providers (reuses the
stage functions of ``verify_pm_threshold.py``); per-file JSON cache under ``--cache-dir``
(default ``~/.cache/tradedesk-agents/pm_h11_recheck``). Hourly Binance klines before October come from the item-5
cache ``polymarket_threshold/klines`` (read only).

Usage:
    uv run python scripts/verify_pm_h11_recheck.py --stage list    # Gamma listing of closed daily events (new cache)
    uv run python scripts/verify_pm_h11_recheck.py --stage fetch   # /prices-history at t0 for the new markets only
    uv run python scripts/verify_pm_h11_recheck.py --stage spot    # Binance daily 1h klines for October 2026
    uv run python scripts/verify_pm_h11_recheck.py --stage stage0  # power gate: counts from forecasts only, no RV, no d
    uv run python scripts/verify_pm_h11_recheck.py --stage full    # refuses unless the stage0 gate passed
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import resource
import statistics
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tradedesk.analytics import pm_modules as pm
from tradedesk.analytics.pm_threshold import HOUR_S, quote_at, value_at
from tradedesk.core.errors import ProviderError

PMT = Path("~/.cache/tradedesk-agents/polymarket_threshold").expanduser()  # item-5 cache (read only)
DEFAULT_CACHE = Path("~/.cache/tradedesk-agents/pm_h11_recheck").expanduser()
CUT = int(datetime(2026, 10, 7, tzinfo=UTC).timestamp())  # T >= CUT (pre-registered)
SEED = 20261101
OFF_H = 12
# pre-registered power gate (item 8f): reference SE of the same filter in item 5 (q in [0.05, 0.95], sigma ratio <= 3)
SE_REF, N_REF, TARGET, MIN_WEEKS = 0.0264, 737, 0.06, 20
LO, HI, MIN_N, RATIO = 0.05, 0.95, 2, 3.0


def _load_vt():  # noqa: ANN202
    p = Path(__file__).with_name("verify_pm_threshold.py")
    spec = importlib.util.spec_from_file_location("verify_pm_threshold", p)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["verify_pm_threshold"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


vt = _load_vt()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def week_of(ts: int) -> str:
    y, w, _ = datetime.fromtimestamp(ts, UTC).isocalendar()
    return f"{y}-W{w:02d}"


def _amended(desc: str) -> str:
    """Amendment 1 (card, before any quote): the post-2026-10-06 text says the 1m candle "closing at 12:00"."""
    return desc.replace(" closing at 12:00 in the ET timezone", " 12:00 in the ET timezone")


def new_markets(cache: Path, why: Counter | None = None) -> list[dict[str, Any]]:
    """Markets of daily events with T >= CUT, classified by ``classify_market`` with amendment 1 applied."""
    out = []
    for asset in vt.ASSETS:
        p = cache / "events" / f"{asset}.json"
        if not p.exists():
            continue
        for ev in json.loads(p.read_text())["rows"]:
            if str(ev.get("endDate") or "") < "2026-10-07":
                continue
            ev2 = {**ev, "description": _amended(str(ev.get("description") or ""))}
            for m in ev.get("markets") or []:
                c = vt.classify_market(asset, ev2, {**m, "description": _amended(str(m.get("description") or ""))})
                if why is not None:
                    why[(asset, c["status"])] += 1
                if c["status"] == "ok" and int(c["T"]) >= CUT:
                    out.append(c)
    out.sort(key=lambda r: (r["T"], r["asset"], r["strike"]))
    return out


# ----------------------------------------------------------------------------- fetch


async def stage_fetch(cache: Path, limit: int) -> None:
    from tradedesk.server import build_server

    qdir = cache / "quotes_12h"
    qdir.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    why: Counter = Counter()
    ms = [m for m in new_markets(cache, why) if int(m["T"]) <= now]
    print(f"classification of markets with endDate >= 2026-10-07 (amendment 1): {sorted(why.items())}", flush=True)
    todo = [m for m in ms if vt.listed_by(m, vt.t0_of(m) - vt.WINDOW_S) and not (qdir / f"{m['market_id']}.json").exists()]
    if limit:
        todo = todo[:limit]
    print(f"new markets (T >= 2026-10-07, resolved): {len(ms)}; quotes to fetch: {len(todo)}", flush=True)
    _, rt = build_server()
    sem = asyncio.Semaphore(8)
    t_start = time.perf_counter()
    done = 0

    async def one(m: dict[str, Any]) -> None:
        nonlocal done
        t0 = vt.t0_of(m)
        async with sem:
            h = await vt._retry(lambda: rt.polymarket.price_history_range(m["yes_token"], t0 - vt.WINDOW_S, t0, 1), "hist")
        vt._write(qdir / f"{m['market_id']}.json", {"hist": h.data})
        done += 1
        if done == 40:
            el = time.perf_counter() - t_start
            print(f"  sample: 40 in {el:.1f}s -> estimate {el / 40 * len(todo) / 60:.1f} min", flush=True)
        if done % 200 == 0:
            print(f"  quotes {done}/{len(todo)} {time.perf_counter() - t_start:.0f}s rss {rss_mb():.0f} MB", flush=True)

    try:
        await asyncio.gather(*(one(m) for m in todo))
    finally:
        await rt.aclose()
    print(f"fetch done {done} in {time.perf_counter() - t_start:.0f}s rss {rss_mb():.0f} MB", flush=True)


async def stage_spot(cache: Path) -> None:
    from tradedesk.providers.binance_vision import parse_klines_zip
    from tradedesk.server import build_server

    kdir = cache / "klines"
    kdir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for _, sym, _ in vt.ASSETS.values():
        for d in range(1, 32):
            name = f"{sym}-1h-2026-10-{d:02d}"
            if not (PMT / "klines" / f"{name}.json").exists() and not (kdir / f"{name}.json").exists():
                jobs.append((kdir / f"{name}.json", f"/data/spot/daily/klines/{sym}/1h/{name}.zip"))
    print(f"klines to fetch: {len(jobs)}", flush=True)
    _, rt = build_server()
    missing = []
    try:
        for p, url in jobs:
            try:
                f = await vt._retry(lambda url=url: rt.binance_vision.http.get_bytes(url, tool="spot_klines"), url)
            except ProviderError as e:
                missing.append((p.name, str(e)[:60]))
                continue
            vt._write(p, parse_klines_zip(f.data))
    finally:
        await rt.aclose()
    print(f"spot done; missing {len(missing)}: {missing[:8]}", flush=True)


def load_spot_merged(cache: Path) -> dict[str, list[tuple[int, float]]]:
    """asset -> [(close_time_s, close)] from the item-5 klines plus the new October daily files."""
    out: dict[str, list[tuple[int, float]]] = {}
    for asset, (_, sym, _) in vt.ASSETS.items():
        rows: dict[int, float] = {}
        for d in (PMT / "klines", cache / "klines"):
            for p in sorted(d.glob(f"{sym}-1h-*.json")):
                for r in json.loads(p.read_text()):
                    rows[int(r[0]) // 1000] = float(r[4])
        out[asset] = [(k + HOUR_S, rows[k]) for k in sorted(rows)]
    return out


# ----------------------------------------------------------------------------- event table (forecasts only)


def old_max_t() -> int:
    """Latest T among item-5 markets that have a cached quote (an upper bound of the item-5 sample)."""
    markets, _ = vt.load_markets(PMT)
    ts = [int(m["T"]) for m in markets if (PMT / "quotes_12h" / f"{m['market_id']}.json").exists()]
    return max(ts) if ts else 0


def events(cache: Path, spot: dict[str, list[tuple[int, float]]]) -> list[dict[str, Any]]:
    """One record per new event: forecasts (sigma_PM fit, EWMA), bar coverage after t0. RV is NOT computed here."""
    qdir = cache / "quotes_12h"
    keys = {a: [k for k, _ in s] for a, s in spot.items()}
    ev: dict[tuple[str, int], dict[str, Any]] = {}
    for m in new_markets(cache):
        qp = qdir / f"{m['market_id']}.json"
        if not qp.exists():
            continue
        t0 = vt.t0_of(m)
        e = ev.setdefault((m["asset"], t0), {"asset": m["asset"], "t0": t0, "T": int(m["T"]), "mk": []})
        qa = quote_at(json.loads(qp.read_text())["hist"], t0)
        if qa:
            e["mk"].append((float(m["strike"]), float(qa[0])))
    out = []
    for (a, t0), e in sorted(ev.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        s, k = spot[a], keys[a]
        e["S0"] = value_at(s, t0)
        n_after = bisect_right(k, e["T"]) - bisect_right(k, t0)
        e["bars_ok"] = n_after == (e["T"] - t0) // HOUR_S and bisect_left(k, t0) < len(k) and k[bisect_left(k, t0)] == t0
        e["fit"] = (
            pm.ladder_sigma([x for x, _ in e["mk"]], [q for _, q in e["mk"]], e["S0"], lo=LO, hi=HI, min_n=MIN_N) if e["S0"] else None
        )
        v_raw = pm.ewma_hourly_var(s[bisect_left(k, t0 - 91 * 86400) : bisect_right(k, t0)], t0)
        e["v_ewma"] = v_raw * 12 if v_raw else None
        e["v_pm"] = e["fit"]["s"] ** 2 if e["fit"] else None
        e["F"] = bool(e["bars_ok"] and e["v_pm"] and e["v_ewma"] and pm.fit_quality_ok(e["v_pm"], e["v_ewma"], RATIO))
        out.append(e)
    return out


def gate(evs: list[dict[str, Any]]) -> dict[str, Any]:
    f = [e for e in evs if e["F"]]
    n_f = len(f)
    weeks = len({week_of(e["t0"]) for e in f})
    mde = pm.projected_mde(SE_REF, N_REF, n_f) if n_f else None
    n_need = pm.n_for_mde(SE_REF, N_REF, TARGET)
    last_t = max((e["T"] for e in evs if e["bars_ok"]), default=CUT)
    span_w = max((last_t - CUT) / (7 * 86400), 1e-9)
    rate = n_f / span_w
    eta = datetime.fromtimestamp(last_t, UTC) + timedelta(weeks=(n_need - n_f) / rate) if rate > 0 and n_f < n_need else None
    old_rate = N_REF / 60.0  # item 5: 737 events passing the same filter over ~60 ISO weeks
    eta_old = datetime.fromtimestamp(last_t, UTC) + timedelta(weeks=(n_need - n_f) / old_rate) if n_f < n_need else None
    return {
        "n_f": n_f,
        "weeks": weeks,
        "mde": mde,
        "n_need": n_need,
        "rate_per_week": rate,
        "span_weeks": span_w,
        "last_T": datetime.fromtimestamp(last_t, UTC).isoformat(),
        "eta": eta.date().isoformat() if eta else None,
        "eta_old_rate": eta_old.date().isoformat() if eta_old else None,
        "pass": bool(mde is not None and mde <= TARGET and weeks >= MIN_WEEKS),
    }


def stage0(cache: Path, out: Path) -> dict[str, Any]:
    spot = load_spot_merged(cache)
    omt = old_max_t()
    evs = events(cache, spot)
    new_min = min((e["T"] for e in evs), default=None)
    print(
        f"item-5 latest T with a quote: {datetime.fromtimestamp(omt, UTC).isoformat()}; "
        f"new earliest T: {datetime.fromtimestamp(new_min, UTC).isoformat() if new_min else None} "
        f"-> disjoint={new_min is not None and new_min > omt}",
        flush=True,
    )
    n_mk = len(new_markets(cache))
    why = Counter()
    for e in evs:
        if not e["bars_ok"]:
            why["no 12 bars after t0 / bar at t0"] += 1
        elif e["fit"] is None:
            why["< 2 strikes with q in [0.05, 0.95] (or beta <= 0)"] += 1
        elif not e["v_ewma"]:
            why["no EWMA"] += 1
        elif not e["F"]:
            why["sigma ratio outside [1/3, 3]"] += 1
        else:
            why["pass F"] += 1
    print(f"new markets {n_mk}; events with >= 1 cached quote {len(evs)}; rss {rss_mb():.0f} MB", flush=True)
    for k, v in why.most_common():
        print(f"  {k}: {v}", flush=True)
    per = Counter(e["asset"] for e in evs if e["F"])
    per_all = Counter(e["asset"] for e in evs)
    print("per asset (pass F / events): " + ", ".join(f"{a} {per[a]}/{per_all[a]}" for a in vt.ASSETS), flush=True)
    strikes = [len([q for _, q in e["mk"] if LO <= q <= HI]) for e in evs if e["bars_ok"]]
    if strikes:
        print(f"strikes with q in [0.05, 0.95] per event: median {statistics.median(strikes)}, max {max(strikes)}", flush=True)
    g = gate(evs)
    print(
        f"GATE: n_F={g['n_f']} weeks={g['weeks']} projected MDE={g['mde'] if g['mde'] is None else round(g['mde'], 3)} "
        f"(target {TARGET}, min weeks {MIN_WEEKS}) n_needed={g['n_need']} -> pass={g['pass']}",
        flush=True,
    )
    print(
        f"accrual: {g['rate_per_week']:.1f} F-events/week over {g['span_weeks']:.1f} weeks (last T {g['last_T'][:16]}); "
        f"ETA n_needed at this rate: {g['eta']}; at the item-5 rate ({N_REF / 60:.1f}/week): {g['eta_old_rate']}",
        flush=True,
    )
    res = {"gate": g, "why": dict(why), "per_asset_pass": dict(per), "per_asset_events": dict(per_all), "old_max_T": omt}
    out.write_text(json.dumps(res, indent=1))
    return res


def stage_full(cache: Path, out: Path, n_boot: int) -> None:
    spot = load_spot_merged(cache)
    evs = events(cache, spot)
    g = gate(evs)
    if not g["pass"]:
        print(f"REFUSED: the pre-registered power gate failed (n_F={g['n_f']}, weeks={g['weeks']}, MDE={g['mde']})", flush=True)
        return
    units = []  # (week, asset, d)
    hyb = []
    for e in evs:
        if not e["bars_ok"]:
            continue
        rv = pm.realized_var(spot[e["asset"]], e["t0"], e["T"])
        if not rv or rv <= 0:
            continue
        d = pm.qlike(rv, e["v_ewma"]) - pm.qlike(rv, e["v_pm"]) if e["F"] else None
        if d is not None:
            units.append((week_of(e["t0"]), e["asset"], d))
        hyb.append((week_of(e["t0"]), d or 0.0))

    def boot(us: list[tuple[str, float]]) -> dict[str, Any]:
        blocks: dict[str, list[float]] = defaultdict(list)
        for b, v in us:
            blocks[b].append(v)
        return pm.block_bootstrap_ratio([(sum(v), len(v)) for v in blocks.values()], n_boot=n_boot, seed=SEED)

    res: dict[str, Any] = {"gate": g, "primary": boot([(w, d) for w, _, d in units]), "hybrid": boot(hyb)}
    res["median_d"] = statistics.median(d for _, _, d in units)
    per = {a: boot([(w, d) for w, aa, d in units if aa == a]) for a in vt.ASSETS}
    ok = [a for a in per if per[a].get("se")]
    res["per_asset"] = per
    res["cochran"] = pm.cochran_q([per[a]["mean"] for a in ok], [per[a]["se"] for a in ok]) if len(ok) >= 2 else None
    res["btc_vs_rest"] = {"btc": per["bitcoin"], "rest": boot([(w, d) for w, a, d in units if a != "bitcoin"])}
    out.write_text(json.dumps(res, indent=1, default=str))
    p = res["primary"]
    print(
        f"PRIMARY d = {p['mean']:+.3f} [{p['lo']:+.3f}; {p['hi']:+.3f}] p={p['p']:.3f} n={len(units)} mde={2.8 * p['se']:.3f}", flush=True
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=("list", "fetch", "spot", "stage0", "full"))
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--n-boot", type=int, default=10_000)
    a = ap.parse_args()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    if a.stage == "list":
        asyncio.run(vt.stage_list(cache))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(cache, a.limit))
    elif a.stage == "spot":
        asyncio.run(stage_spot(cache))
    elif a.stage == "stage0":
        stage0(cache, cache / "stage0.json")
    else:
        stage_full(cache, cache / "full.json", a.n_boot)
    print(f"[{a.stage}] {time.perf_counter() - t:.0f}s peak rss {rss_mb():.0f} MB", flush=True)


if __name__ == "__main__":
    main()
