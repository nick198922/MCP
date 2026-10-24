"""Verify IDEA-20261024-spot-vs-perp-flow-divergence (pre-registration in the card).

Stages (run in order; every download is cached per file under --cache-dir, ``null`` marks a 404):
  probe     time a few archive files, save real fixtures (spot file with microsecond timestamps)
  meta      archive symbol listings + exchange info; 2021-07..12 perp/spot 1d klines of the candidates
  universe  ex-ante universe: BTC, ETH + next 30 by 2021-H2 perp quote turnover
  fetch     1d perp + spot klines (with taker columns) and funding, 2021-07..2026-09, for the universe
  stage0    sanity checks and the power gate (no means of returns are printed)
  full      H1..H3, robustness, per-asset (refuses to run unless stage0 passed)

Usage: uv run python scripts/verify_spot_perp_flow.py --stage stage0 --out ~/.cache/tradedesk-agents/spot_perp_flow/stage0.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics import per_asset as pa
from tradedesk.analytics.listing_drift import symbol_eligible
from tradedesk.analytics.spot_perp_flow import (
    DAY_MS,
    cluster_bootstrap_combo,
    cluster_se_combo,
    contrast,
    drop_top_clusters_combo,
    event_mask,
    expanding_bin_labels,
    funding_sum,
    imbalance,
    last_funding,
    ols_cluster_cov,
    shuffle_within_groups,
    symbol_features,
    trade_cost,
    week_of,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
RANK_MONTHS = [(2021, m) for m in range(7, 13)]
FIRST_MONTH, LAST_MONTH = (2021, 7), (2026, 9)
GRID0 = int(datetime(2021, 7, 1, tzinfo=UTC).timestamp() * 1000) // DAY_MS
GRID1 = int(datetime(2026, 9, 30, tzinfo=UTC).timestamp() * 1000) // DAY_MS
TEST0 = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000) // DAY_MS
TEST1 = int(datetime(2026, 9, 26, tzinfo=UTC).timestamp() * 1000) // DAY_MS
K_EVENT = 1.5
MIN_PRIOR = 150
GATE_MDE = 0.010
MAJORS = ("BTCUSDT", "ETHUSDT")
N_UNIVERSE = 32


class Tee:
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


def months(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    out, (y, m) = [], a
    while (y, m) <= b:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, tz=UTC).strftime("%Y-%m-%d")


class Fetcher:
    """Per-file JSON cache; ``null`` marks a file the source does not have (404)."""

    def __init__(self, cache: Path, workers: int = 10) -> None:
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
                            print(f"  error {path.name}: {e.reason}: {str(e.detail)[:160]}", flush=True)
                        return
                    await asyncio.sleep(5 * (k + 1))
        await asyncio.to_thread(wr, path, data)
        self.done += 1

    async def many(self, label: str, jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]]) -> None:
        todo = [(p, g) for p, g in jobs if not p.exists()]
        print(f"[{label}] {len(jobs)} files, {len(todo)} to fetch", flush=True)
        t0 = time.time()
        self.done, self.errors = 0, Counter()
        step = max(50, len(todo) // 20)

        async def run(p: Path, g: Callable[[], Awaitable[Any]]) -> None:
            await self.one(p, g)
            if self.done and self.done % step == 0:
                el = time.time() - t0
                print(
                    f"  [{label}] {self.done}/{len(todo)} {el:.0f}s eta {el / self.done * (len(todo) - self.done):.0f}s rss {rss_mb():.0f}MB",
                    flush=True,
                )

        await asyncio.gather(*[run(p, g) for p, g in todo])
        print(f"[{label}] done in {time.time() - t0:.0f}s; transient errors {dict(self.errors)}", flush=True)


def kpath(cache: Path, kind: str, sym: str, ym: tuple[int, int]) -> Path:
    return cache / f"k1d_{kind}" / sym / f"{ym[0]:04d}-{ym[1]:02d}.json"


def fpath(cache: Path, sym: str, ym: tuple[int, int]) -> Path:
    return cache / "funding" / sym / f"{ym[0]:04d}-{ym[1]:02d}.json"


def kjob(rt: Any, cache: Path, kind: str, sym: str, ym: tuple[int, int]) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    v = rt.binance_vision
    f = v.um_klines_taker_monthly if kind == "perp" else v.spot_klines_taker_monthly

    async def go() -> Any:
        return (await f(sym, "1d", ym[0], ym[1])).data

    return kpath(cache, kind, sym, ym), go


def fjob(rt: Any, cache: Path, sym: str, ym: tuple[int, int]) -> tuple[Path, Callable[[], Awaitable[Any]]]:
    async def go() -> Any:
        return (await rt.binance_vision.um_funding_monthly(sym, ym[0], ym[1])).data

    return fpath(cache, sym, ym), go


# ---------------------------------------------------------------- probe / meta / universe


async def stage_probe(cache: Path) -> None:
    _, rt = build_server()
    v = rt.binance_vision
    from tradedesk.providers.binance_vision import parse_klines_taker_zip

    try:
        paths = {
            "spot_btc_2025_03": "/data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2025-03.zip",
            "perp_btc_2025_03": "/data/futures/um/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2025-03.zip",
            "spot_eth_2022_01": "/data/spot/monthly/klines/ETHUSDT/1d/ETHUSDT-1d-2022-01.zip",
            "perp_eth_2022_01": "/data/futures/um/monthly/klines/ETHUSDT/1d/ETHUSDT-1d-2022-01.zip",
        }
        got = {}
        for k, p in paths.items():
            t = time.time()
            f = await v.http.get_bytes(p)
            rows = parse_klines_taker_zip(f.data)
            got[k] = rows
            print(f"OK {k:<18} {len(f.data):>6} B {len(rows)} rows {time.time() - t:.2f}s first {rows[0][:1] + rows[0][5:]}", flush=True)
            if k == "spot_btc_2025_03":
                (FIX / "binance_vision_BTCUSDT-1d-2025-03_spot_taker_2026-10-24.zip").write_bytes(f.data)
        for a, b in (("spot_btc_2025_03", "perp_btc_2025_03"), ("spot_eth_2022_01", "perp_eth_2022_01")):
            s = {int(r[0]): r for r in got[a]}
            pp = {int(r[0]): r for r in got[b]}
            common = sorted(set(s) & set(pp))
            ratio = statistics.median(pp[t][4] / s[t][4] for t in common)
            vol = statistics.median(s[t][5] / pp[t][5] for t in common)
            print(f"{a[5:8]}: common days {len(common)}; perp/spot close median {ratio:.5f}; spot/perp base volume median {vol:.3f}")
    finally:
        await rt.aclose()


async def stage_meta(cache: Path) -> None:
    _, rt = build_server()
    try:
        v = rt.binance_vision
        fx = Fetcher(cache, workers=6)

        async def ex_info() -> Any:
            mk = (await rt.exchanges.markets("binanceusdm")).data or {}
            info = {}
            for m in mk.values():
                i = (m or {}).get("info") or {}
                if i.get("symbol"):
                    info[i["symbol"]] = {
                        "underlyingType": i.get("underlyingType"),
                        "contractType": i.get("contractType"),
                        "status": i.get("status"),
                    }
            return {"fetched_at": int(time.time()), "info": info}

        async def um() -> Any:
            return (await v.um_symbols()).data

        async def spot() -> Any:
            return (await v.spot_symbols()).data

        await fx.many(
            "meta", [(cache / "um_symbols.json", um), (cache / "spot_symbols.json", spot), (cache / "exchange_info.json", ex_info)]
        )
        cands = candidates(cache)
        print(f"candidates (eligible perp with same-symbol spot): {len(cands)}", flush=True)
        await fx.many("perp 2021-12", [kjob(rt, cache, "perp", s, (2021, 12)) for s in cands])
        live = [s for s in cands if rd(kpath(cache, "perp", s, (2021, 12)))]
        print(f"candidates with perp data in 2021-12: {len(live)}", flush=True)
        jobs = [kjob(rt, cache, k, s, ym) for s in live for ym in RANK_MONTHS for k in ("perp", "spot")]
        await fx.many("rank months 2021-07..12", jobs)
    finally:
        await rt.aclose()


def candidates(cache: Path) -> list[str]:
    syms = rd(cache / "um_symbols.json")
    spot = set(rd(cache / "spot_symbols.json"))
    info = rd(cache / "exchange_info.json")["info"]
    out = []
    for s in syms:
        i = info.get(s) or {}
        if symbol_eligible(s, i.get("contractType"), i.get("underlyingType")) and s in spot:
            out.append(s)
    return sorted(out)


def stage_universe(cache: Path) -> None:
    syms = rd(cache / "um_symbols.json")
    info = rd(cache / "exchange_info.json")["info"]
    spot = set(rd(cache / "spot_symbols.json"))
    excl: Counter[str] = Counter()
    for s in syms:
        i = info.get(s) or {}
        if not symbol_eligible(s, i.get("contractType"), i.get("underlyingType")):
            excl["ineligible (tradfi/index/stable/dated/non-USDT)"] += 1
        elif s not in spot:
            excl["no same-symbol spot"] += 1
    cands = candidates(cache)
    rank = []
    for s in cands:
        if not kpath(cache, "perp", s, (2021, 12)).exists() or not rd(kpath(cache, "perp", s, (2021, 12))):
            continue
        ok = True
        qv = 0.0
        for ym in RANK_MONTHS:
            p = rd(kpath(cache, "perp", s, ym)) if kpath(cache, "perp", s, ym).exists() else None
            sp = rd(kpath(cache, "spot", s, ym)) if kpath(cache, "spot", s, ym).exists() else None
            if not p or not sp:
                ok = False
                break
            qv += sum(r[6] for r in p)
        if ok:
            rank.append((qv, s))
    # survivorship context: eligible perps (any spot) with data in 2021-12 vs those with a same-symbol spot
    hist22 = len([s for s in cands if kpath(cache, "perp", s, (2021, 12)).exists() and rd(kpath(cache, "perp", s, (2021, 12)))])
    no_spot_eligible = [
        s
        for s in syms
        if symbol_eligible(s, (info.get(s) or {}).get("contractType"), (info.get(s) or {}).get("underlyingType")) and s not in spot
    ]
    rank.sort(reverse=True)
    order = [s for _, s in rank]
    uni = list(MAJORS) + [s for s in order if s not in MAJORS][: N_UNIVERSE - len(MAJORS)]
    wr(cache / "universe.json", {"symbols": uni, "rank": [[s, q] for q, s in rank]})
    print(f"archive USDT-M symbols {len(syms)}; excluded {dict(excl)}")
    print(
        f"eligible perps without a same-symbol spot (all dates; e.g. 1000-prefixed): {len(no_spot_eligible)}: {no_spot_eligible[:12]} ..."
    )
    print(f"eligible perps with same-symbol spot and perp data in 2021-12: {hist22}; with full 2021-H2 perp+spot: {len(rank)}")
    print(f"universe ({len(uni)}): {' '.join(uni)}")
    tot = sum(q for q, _ in rank)
    print(f"universe share of 2021-H2 candidate perp turnover: {sum(q for q, s in rank if s in uni) / tot:.1%}")


async def stage_fetch(cache: Path) -> None:
    uni = rd(cache / "universe.json")["symbols"]
    _, rt = build_server()
    try:
        fx = Fetcher(cache, workers=10)
        ms = months(FIRST_MONTH, LAST_MONTH)
        jobs = [kjob(rt, cache, k, s, ym) for s in uni for ym in ms for k in ("perp", "spot")]
        jobs += [fjob(rt, cache, s, ym) for s in uni for ym in ms]
        await fx.many("klines+funding", jobs)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- panel


def load_market(cache: Path, kind: str, sym: str) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for ym in months(FIRST_MONTH, LAST_MONTH):
        p = kpath(cache, kind, sym, ym)
        rows = rd(p) if p.exists() else None
        for r in rows or []:
            out[int(r[0]) // DAY_MS] = r
    return out


def load_funding(cache: Path, sym: str) -> tuple[np.ndarray, np.ndarray]:
    rows: list[list[float]] = []
    for ym in months(FIRST_MONTH, LAST_MONTH):
        p = fpath(cache, sym, ym)
        rows += (rd(p) if p.exists() else None) or []
    rows.sort(key=lambda r: r[0])
    return np.array([r[0] for r in rows], dtype=float), np.array([r[2] for r in rows], dtype=float)


def build_panel(cache: Path) -> dict[str, Any]:
    """Per-symbol arrays on the day grid GRID0..GRID1 plus sanity counters."""
    uni = rd(cache / "universe.json")["symbols"]
    n = GRID1 - GRID0 + 1
    syms: dict[str, dict[str, np.ndarray]] = {}
    checks: dict[str, Any] = {}
    for s in uni:
        pk = load_market(cache, "perp", s)
        sk = load_market(cache, "spot", s)
        a = {k: np.full(n, np.nan) for k in ("o", "c", "v", "qv", "tb", "sv", "stb", "sc")}
        zero_vol = 0
        for d, r in pk.items():
            # zero-volume bars are trading halts / delisted periods with a stale flat price: treat as missing
            if GRID0 <= d <= GRID1:
                if r[5] <= 0:
                    zero_vol += 1
                    continue
                i = d - GRID0
                a["o"][i], a["c"][i], a["v"][i], a["qv"][i], a["tb"][i] = r[1], r[4], r[5], r[6], r[7]
        for d, r in sk.items():
            if GRID0 <= d <= GRID1 and r[5] > 0:
                i = d - GRID0
                a["sv"][i], a["stb"][i], a["sc"][i] = r[5], r[7], r[4]
        both = np.isfinite(a["c"]) & np.isfinite(a["sc"])
        ratio = float(np.median(a["c"][both] / a["sc"][both])) if both.any() else float("nan")
        bad_ts = sum(1 for d, r in list(pk.items()) + list(sk.items()) if int(r[0]) % DAY_MS)
        p_days = np.isfinite(a["c"])
        first = int(np.argmax(p_days)) if p_days.any() else 0
        last = n - 1 - int(np.argmax(p_days[::-1])) if p_days.any() else -1
        span = slice(first, last + 1)
        gaps_perp = int((~p_days[span]).sum())
        gaps_spot = int((~np.isfinite(a["sc"][span])).sum())
        volshare = float(np.nanmedian(a["sv"][both] / a["v"][both])) if both.any() else float("nan")
        a["i_perp"] = imbalance(a["v"], a["tb"])
        a["i_spot"] = imbalance(a["sv"], a["stb"])
        a["d"] = a["i_spot"] - a["i_perp"]
        # day-level ticker collision guard (e.g. a spot symbol re-used by a new token): drop the flow on such days
        with np.errstate(divide="ignore", invalid="ignore"):
            mism = both & (np.abs(a["c"] / a["sc"] - 1.0) > 0.05)
        a["d"][mism] = np.nan
        a["ft"], a["fr"] = load_funding(cache, s)
        checks[s] = {
            "ratio": ratio,
            "bad_ts": bad_ts,
            "gaps_perp": gaps_perp,
            "gaps_spot": gaps_spot,
            "last": ymd(GRID0 + last) if last >= 0 else None,
            "spot_vol_share": volshare,
            "funding_n": int(a["ft"].size),
            "mismatch_days": int(mism.sum()),
            "zero_vol_perp": zero_vol,
        }
        if math.isfinite(ratio) and 0.98 <= ratio <= 1.02:
            syms[s] = a
    btc = syms["BTCUSDT"]
    from tradedesk.analytics.spot_perp_flow import simple_returns

    btc_r = simple_returns(btc["c"])
    for s, a in syms.items():
        a.update(symbol_features(a["o"], a["c"], a["d"], None if s == "BTCUSDT" else btc_r))
    return {"syms": syms, "checks": checks}


def events(
    panel: dict[str, Any], *, k: float = K_EVENT, zkey: str = "z", zoverride: dict[str, np.ndarray] | None = None
) -> list[dict[str, Any]]:
    syms = panel["syms"]
    btc = syms["BTCUSDT"]
    out = []
    for s, a in syms.items():
        z = zoverride[s] if zoverride is not None else a[zkey]
        m = event_mask(a["r"], a["sd30"], k)
        for i in np.flatnonzero(m):
            day = GRID0 + int(i)
            if not (TEST0 <= day <= TEST1):
                continue
            if not (math.isfinite(z[i]) and math.isfinite(a["beta"][i]) and math.isfinite(a["f1"][i]) and math.isfinite(btc["f1"][i])):
                continue
            sg = 1.0 if a["r"][i] > 0 else -1.0
            hedged = s != "BTCUSDT"
            b = float(a["beta"][i]) if hedged else 0.0
            f1b, f3b = float(btc["f1"][i]), float(btc["f3"][i])
            y1 = sg * (a["f1"][i] - b * f1b)
            y3 = sg * (a["f3"][i] - b * f3b) if math.isfinite(a["f3"][i]) and math.isfinite(f3b) else float("nan")
            t1 = (day + 1) * DAY_MS
            fs1 = funding_sum(a["ft"], a["fr"], t1, (day + 2) * DAY_MS)
            fs3 = funding_sum(a["ft"], a["fr"], t1, (day + 4) * DAY_MS)
            fb1 = funding_sum(btc["ft"], btc["fr"], t1, (day + 2) * DAY_MS) if hedged else 0.0
            fb3 = funding_sum(btc["ft"], btc["fr"], t1, (day + 4) * DAY_MS) if hedged else 0.0
            lv = a["qv"][max(0, i - 30) : i]
            lv = lv[np.isfinite(lv)]
            lnvol = math.log(a["qv"][i] / lv.mean()) if lv.size >= 20 and a["qv"][i] > 0 else float("nan")
            fl = last_funding(a["ft"], a["fr"], t1)
            out.append(
                {
                    "s": s,
                    "day": day,
                    "wk": week_of(day),
                    "sg": sg,
                    "absr": abs(float(a["r"][i])) / float(a["sd30"][i]),
                    "dstar": sg * float(z[i]),
                    "beta": b,
                    "y1": float(y1),
                    "y3": float(y3),
                    "raw1": sg * float(a["f1"][i]),
                    "fd1": sg * (fs1 - b * fb1),
                    "fd3": sg * (fs3 - b * fb3),
                    "fm1": sg * fs1,
                    "c": trade_cost(b, is_major=s in MAJORS, hedged=hedged),
                    "c_unh": trade_cost(0.0, is_major=s in MAJORS, hedged=False),
                    "lnvol": lnvol,
                    "fsign": sg * (1.0 if fl > 0 else (-1.0 if fl < 0 else 0.0)) if math.isfinite(fl) else 0.0,
                }
            )
    out.sort(key=lambda e: (e["day"], e["s"]))
    return out


def label(evs: list[dict[str, Any]], nbins: int = 3) -> np.ndarray:
    return expanding_bin_labels([e["day"] for e in evs], [e["dstar"] for e in evs], nbins=nbins, min_prior=MIN_PRIOR)


def nets(evs: list[dict[str, Any]], lab: np.ndarray, top: int, *, h: int = 1, cost_mult: float = 1.0, unhedged: bool = False) -> np.ndarray:
    """Net trade result g: top bin with the move, bottom bin (0) against it; nan for the middle / burn-in."""
    out = np.full(len(evs), np.nan)
    for j, (e, b) in enumerate(zip(evs, lab, strict=True)):
        if b not in (0, top):
            continue
        y = e["raw1"] if unhedged else e[f"y{h}"]
        fd = e["fm1"] if unhedged else e[f"fd{h}"]  # unhedged: funding on the main leg only
        c = (e["c_unh"] if unhedged else e["c"]) * cost_mult
        p = 1.0 if b == top else -1.0
        out[j] = p * y - c - p * fd
    return out


# ---------------------------------------------------------------- stage 0


def stage0(cache: Path) -> dict[str, Any]:
    t0 = time.time()
    panel = build_panel(cache)
    ch = panel["checks"]
    print(f"panel built in {time.time() - t0:.0f}s rss {rss_mb():.0f}MB; symbols kept {len(panel['syms'])}/{len(ch)}")
    print(
        f"{'symbol':<12} {'perp/spot':>9} {'badTs':>5} {'gapP':>5} {'gapS':>5} {'mism':>5} {'zeroV':>5} {'spot/perp vol':>13} {'fund':>5} last"
    )
    for s, c in ch.items():
        print(
            f"{s:<12} {c['ratio']:>9.4f} {c['bad_ts']:>5} {c['gaps_perp']:>5} {c['gaps_spot']:>5} {c['mismatch_days']:>5} {c['zero_vol_perp']:>5} "
            f"{c['spot_vol_share']:>13.3f} {c['funding_n']:>5} {c['last']}"
        )
    vs = [c["spot_vol_share"] for c in ch.values() if math.isfinite(c["spot_vol_share"])]
    print(f"median over symbols of spot/perp base-volume share: {statistics.median(vs):.3f}")
    evs = events(panel)
    lab = label(evs)
    used = lab >= 0
    y = np.array([e["y1"] for e in evs])
    wk = [e["wk"] for e in evs]
    days = sorted({e["day"] for e, u in zip(evs, used, strict=True) if u})
    n_top, n_bot = int((lab == 2).sum()), int((lab == 0).sum())
    se = cluster_se_combo(y[used], lab[used], [w for w, u in zip(wk, used, strict=True) if u], {2: 1.0, 0: -1.0})
    sd_y = float(np.std(y[used], ddof=1))
    sd_alt = float(np.std([e["y1"] for e, u in zip(evs, used, strict=True) if u and e["s"] not in MAJORS], ddof=1))
    sd_maj = float(np.std([e["y1"] for e, u in zip(evs, used, strict=True) if u and e["s"] in MAJORS], ddof=1))
    mde = 2.8 * se["se"]
    first_used = min(days) if days else None
    res = {
        "n_events_all": len(evs),
        "n_burnin": int((~used).sum()),
        "n_events": int(used.sum()),
        "n_top": n_top,
        "n_mid": int((lab == 1).sum()),
        "n_bot": n_bot,
        "event_days": len(days),
        "event_weeks": se["clusters"],
        "first_day": ymd(first_used) if first_used else None,
        "sd_y1": sd_y,
        "sd_y1_alts": sd_alt,
        "sd_y1_majors": sd_maj,
        "se_cluster": se["se"],
        "se_iid": se["se_iid"],
        "deff": (se["se"] / se["se_iid"]) ** 2,
        "mde": mde,
        "gate_pass": bool(mde <= GATE_MDE),
        "event_rate": len(evs)
        / max(1, sum(int(np.isfinite(a["r"][TEST0 - GRID0 : TEST1 - GRID0 + 1]).sum()) for a in panel["syms"].values())),
    }
    print("\nSTAGE 0 (power gate; no means of returns computed for output)")
    for k, v in res.items():
        print(f"  {k:<14} {v:.5f}" if isinstance(v, float) else f"  {k:<14} {v}")
    print(
        f"  per-tercile n: top {n_top}, bottom {n_bot}; MDE {mde * 100:.3f} pp vs gate {GATE_MDE * 100:.1f} pp -> {'PASS' if res['gate_pass'] else 'FAIL'}"
    )
    wr(cache / "stage0.json", res)
    print(f"rss {rss_mb():.0f}MB")
    return res


# ---------------------------------------------------------------- full


def ci(
    values: np.ndarray,
    lab: np.ndarray,
    evs: list[dict[str, Any]],
    weights: dict[int, float],
    mask: np.ndarray | None = None,
    n_boot: int = 10_000,
) -> dict[str, Any]:
    m = np.isfinite(values) & np.isin(lab, list(weights))
    if mask is not None:
        m &= mask
    idx = np.flatnonzero(m)
    return cluster_bootstrap_combo(values[idx], lab[idx], [evs[i]["wk"] for i in idx], weights, n_boot=n_boot)


def fmt(r: dict[str, Any]) -> str:
    return f"{r['est'] * 100:+7.3f} pp  CI [{r['lo'] * 100:+.3f}; {r['hi'] * 100:+.3f}]  p(<=0) {r['p']:.4f}  n {r['n']}  weeks {r['clusters']}"


def full(cache: Path) -> None:
    s0 = rd(cache / "stage0.json")
    if not s0["gate_pass"]:
        print(f"stage 0 gate failed (MDE {s0['mde'] * 100:.3f} pp > {GATE_MDE * 100:.1f} pp): full test not run (pre-registered)")
        return
    panel = build_panel(cache)
    evs = events(panel)
    lab = label(evs)
    W = {2: 1.0, 0: 1.0}  # S_net = mean(g|top) + mean(g|bottom)
    G = {2: 1.0, 0: -1.0}  # gross S = mean(y|top) - mean(y|bottom)
    g = nets(evs, lab, 2)
    y1 = np.array([e["y1"] for e in evs])
    days = np.array([e["day"] for e in evs])
    used_days = sorted({int(d) for d, b in zip(days, lab, strict=True) if b >= 0})
    split = used_days[int(0.75 * len(used_days))]
    is_m, oos_m = days < split, days >= split
    print(f"IS/OOS split at {ymd(split)} (first 75 % of event dates after burn-in)")
    print("\nH1 (primary): S_net = mean(g|top) + mean(g|bottom), h=1, base costs")
    h1 = ci(g, lab, evs, W)
    print(f"  full  {fmt(h1)}")
    h1_is, h1_oos = ci(g, lab, evs, W, is_m), ci(g, lab, evs, W, oos_m)
    print(f"  IS    {fmt(h1_is)}")
    print(f"  OOS   {fmt(h1_oos)}")
    print(f"  gross S (y top - y bottom): {fmt(ci(y1, lab, evs, G))}")
    print(f"  gross S IS {fmt(ci(y1, lab, evs, G, is_m))}")
    print(f"  gross S OOS {fmt(ci(y1, lab, evs, G, oos_m))}")
    print("\nH2 (descriptive): legs separately (net g; gross directed y)")
    for b, nm in ((2, "top (spot-led, with move)"), (0, "bottom (perp-led, against move)")):
        print(f"  {nm:<32} net {fmt(ci(g, lab, evs, {b: 1.0}))}")
        print(f"  {'':<32} y   {fmt(ci(y1, lab, evs, {b: 1.0}))}")
    print(f"  middle tercile y: {fmt(ci(y1, lab, evs, {1: 1.0}))}")

    # H3 placebo: shuffle z across symbols within each day
    print("\nH3 placebo (a): z shuffled across symbols within day, 200 reps, gross S")
    syms = panel["syms"]
    rows = [(s, i) for s, a in syms.items() for i in np.flatnonzero(np.isfinite(a["z"]))]
    zvals = np.array([syms[s]["z"][i] for s, i in rows])
    zday = [int(i) for _, i in rows]
    rng = np.random.default_rng(20261024)
    true_s = ci(y1, lab, evs, G, n_boot=10)["est"]
    sims = []
    for rep in range(200):
        zs = shuffle_within_groups(zvals, zday, rng)
        zo = {s: np.full(GRID1 - GRID0 + 1, np.nan) for s in syms}
        for (s, i), v in zip(rows, zs, strict=True):
            zo[s][i] = v
        ev2 = events(panel, zoverride=zo)
        lb2 = label(ev2)
        y2 = np.array([e["y1"] for e in ev2])
        m = np.isin(lb2, [0, 2])
        sims.append(float(y2[m & (lb2 == 2)].mean() - y2[m & (lb2 == 0)].mean()))
        if rep % 50 == 49:
            print(f"  rep {rep + 1} rss {rss_mb():.0f}MB", flush=True)
    sims_a = np.array(sims)
    p_shuf = float((1 + (sims_a >= true_s).sum()) / (1 + sims_a.size))
    print(
        f"  actual gross S {true_s * 100:+.3f} pp; placebo mean {sims_a.mean() * 100:+.3f} pp sd {sims_a.std() * 100:.3f} pp; p {p_shuf:.4f}"
    )
    print("H3 placebo (b): z shifted by 30 / 60 days (same symbol), gross S")
    shift_res = {}
    for sh in (30, 60):
        zo = {s: np.r_[np.full(sh, np.nan), a["z"][:-sh]] for s, a in syms.items()}
        ev2 = events(panel, zoverride=zo)
        lb2 = label(ev2)
        y2 = np.array([e["y1"] for e in ev2])
        shift_res[sh] = ci(y2, lb2, ev2, G)
        print(f"  shift {sh}: {fmt(shift_res[sh])}")

    # controlled regression
    print("\nControl regression: y1 ~ 1 + top + bottom + |r|/SD30 + ln(vol/vol30) + sign(r) + sign(r)*sign(funding)")
    m = (lab >= 0) & np.array([math.isfinite(e["lnvol"]) for e in evs])
    idx = np.flatnonzero(m)
    X = np.column_stack(
        [
            np.ones(idx.size),
            (lab[idx] == 2).astype(float),
            (lab[idx] == 0).astype(float),
            [evs[i]["absr"] for i in idx],
            [evs[i]["lnvol"] for i in idx],
            [evs[i]["sg"] for i in idx],
            [evs[i]["fsign"] for i in idx],
        ]
    )
    fit = ols_cluster_cov(y1[idx], X, [evs[i]["wk"] for i in idx])
    names = ["const", "top", "bottom", "|r|/sd", "lnvol", "sign r", "sgn r*sgn f"]
    for j, nm in enumerate(names):
        print(f"  {nm:<12} {fit['coef'][j] * 100:+.4f} pp  se {math.sqrt(fit['cov'][j, j]) * 100:.4f}")
    reg = contrast(fit, [0, 1, -1, 0, 0, 0, 0])
    print(f"  top - bottom: {reg['est'] * 100:+.3f} pp se {reg['se'] * 100:.3f} p {reg['p']:.4f}  (n {fit['n']}, weeks {fit['clusters']})")
    X2 = np.column_stack([np.ones(idx.size), [evs[i]["dstar"] for i in idx], X[:, 3:]])
    fit2 = ols_cluster_cov(y1[idx], X2, [evs[i]["wk"] for i in idx])
    c2 = contrast(fit2, [0, 1, 0, 0, 0, 0])
    print(f"  continuous D*: {c2['est'] * 100:+.4f} pp per 1 z  se {c2['se'] * 100:.4f} p {c2['p']:.4f}")

    # robustness (descriptive)
    print("\nRobustness (descriptive, S_net unless noted)")
    print(f"  costs x2         {fmt(ci(nets(evs, lab, 2, cost_mult=2.0), lab, evs, W))}")
    print(f"  unhedged (raw)   {fmt(ci(nets(evs, lab, 2, unhedged=True), lab, evs, W))}")
    g3 = nets(evs, lab, 2, h=3)
    print(f"  h=3 (overlapping){fmt(ci(g3, lab, evs, W))}")
    sg = np.array([e["sg"] for e in evs])
    maj = np.array([e["s"] in MAJORS for e in evs])
    yr = np.array([int(ymd(e["day"])[:4]) for e in evs])
    ym_ = np.array([ymd(e["day"])[:7] for e in evs])
    for nm, mk in (
        ("up days", sg > 0),
        ("down days", sg < 0),
        ("BTC/ETH", maj),
        ("alts", ~maj),
        ("ex 2022", yr != 2022),
        ("ex 2022-11 (FTX)", ym_ != "2022-11"),
        ("ex 2025-10", ym_ != "2025-10"),
    ):
        print(f"  {nm:<17}{fmt(ci(g, lab, evs, W, mk))}")
    for yy in sorted(set(yr.tolist())):
        print(f"  year {yy:<12}{fmt(ci(g, lab, evs, W, yr == yy))}")
    for k in (5, 10):
        mm = np.isfinite(g)
        print(
            f"  without top {k} week-contributors: {drop_top_clusters_combo(g[mm], lab[mm], [evs[i]['wk'] for i in np.flatnonzero(mm)], W, k) * 100:+.3f} pp"
        )
    lab5 = label(evs, nbins=5)
    print(f"  quintiles (top-bottom) {fmt(ci(nets(evs, lab5, 4), lab5, evs, {4: 1.0, 0: 1.0}))}")
    print(f"  fixed full-sample terciles (lookahead in cuts){'':<1}", end="")
    ds = np.array([e["dstar"] for e in evs])
    q1, q2 = np.quantile(ds[lab >= 0], [1 / 3, 2 / 3])
    labf = np.where(lab < 0, -1, np.where(ds <= q1, 0, np.where(ds <= q2, 1, 2)))
    print(f" {fmt(ci(nets(evs, labf, 2), labf, evs, W))}")
    for kk in (1.0, 2.0):
        ev2 = events(panel, k=kk)
        lb2 = label(ev2)
        print(f"  threshold {kk} SD     {fmt(ci(nets(ev2, lb2, 2), lb2, ev2, W))}")
    # selection rule for the demo (condition 8): per day 1 top-tercile event with max D*, 1 bottom with min D*
    pick = np.zeros(len(evs), dtype=bool)
    byday: dict[int, list[int]] = defaultdict(list)
    for i, e in enumerate(evs):
        byday[e["day"]].append(i)
    for ii in byday.values():
        t = [i for i in ii if lab[i] == 2]
        b = [i for i in ii if lab[i] == 0]
        if t:
            pick[max(t, key=lambda i: evs[i]["dstar"])] = True
        if b:
            pick[min(b, key=lambda i: evs[i]["dstar"])] = True
    print(f"  demo rule top1/bottom1 per day {fmt(ci(g, lab, evs, W, pick))}")

    # baselines
    print("\nBaselines (h=1)")
    allm = lab >= 0
    cont = np.array([e["y1"] - e["c"] - e["fd1"] for e in evs])
    print(f"  all events, trade with move (net):    {fmt(ci(cont, np.where(allm, 1, -1), evs, {1: 1.0}))}")
    print(
        f"  all events, trade against move (net): {fmt(ci(-np.array([e['y1'] for e in evs]) - np.array([e['c'] for e in evs]) + np.array([e['fd1'] for e in evs]), np.where(allm, 1, -1), evs, {1: 1.0}))}"
    )
    print(f"  all events directed y (gross):        {fmt(ci(y1, np.where(allm, 1, -1), evs, {1: 1.0}))}")
    rnd = []
    btc = syms["BTCUSDT"]
    for s, a in syms.items():
        b = a["beta"] if s != "BTCUSDT" else np.zeros_like(a["beta"])
        yy = np.sign(a["r"]) * (a["f1"] - b * btc["f1"])
        for i in range(TEST0 - GRID0, TEST1 - GRID0 + 1):
            if math.isfinite(yy[i]) and a["r"][i] != 0:
                rnd.append((week_of(GRID0 + i), float(yy[i])))
    rv = np.array([v for _, v in rnd])
    rb = cluster_bootstrap_combo(rv, np.ones(rv.size, dtype=int), [w for w, _ in rnd], {1: 1.0}, n_boot=2000)
    print(f"  all symbol-days directed y (gross):   {fmt(rb)}")

    # per-asset (5a)
    print("\nPer-asset (net g of traded events, h=1)")
    tm = np.isfinite(g)
    ti = np.flatnonzero(tm)
    vals = g[ti]
    assets = [evs[i]["s"] for i in ti]
    clus = [evs[i]["wk"] for i in ti]
    for st in pa.asset_stats(vals, assets, min_n=1):
        lo, hi = st.ci if st.ci else (float("nan"), float("nan"))
        print(f"  {st.asset:<12} n {st.n:>4}  E[g] {st.mean * 100:+.3f} pp  CI [{lo * 100:+.3f}; {hi * 100:+.3f}] (iid)")
    q = pa.cluster_robust_q(vals, assets, clus, min_n=30)
    print(f"  Cochran Q (cluster-robust) {q['q']:.1f} df {q['df']} p {q['p']:.4f} I2 {q['i2']:.3f}")
    pq = pa.permutation_q(vals, assets, [evs[i]["day"] for i in ti], min_n=30, n_perm=2000)
    print(f"  permutation Q p {pq['p']:.4f}")
    pers = pa.persistence(vals, assets, clus, [bool(is_m[i]) for i in ti], min_n_is=20, top_frac=0.25)
    print(
        f"  persistence: {json.dumps({k: (round(v, 5) if isinstance(v, float) else v) for k, v in pers.items() if not isinstance(v, (list, dict))})}"
    )

    out = {
        "h1": h1,
        "h1_is": h1_is,
        "h1_oos": h1_oos,
        "placebo_shuffle_p": p_shuf,
        "placebo_shift": shift_res,
        "reg_top_minus_bottom": reg,
        "split": ymd(split),
    }
    wr(cache / "full.json", out)
    print(f"\nrss {rss_mb():.0f}MB")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["probe", "meta", "universe", "fetch", "stage0", "full"])
    ap.add_argument("--cache-dir", type=Path, default=Path("~/.cache/tradedesk-agents/spot_perp_flow"))
    ap.add_argument("--out", type=Path, default=None, help="also write the printed output to this file")
    a = ap.parse_args()
    a.cache_dir = a.cache_dir.expanduser()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    fh = None
    if a.out:
        a.out = a.out.expanduser()
        a.out.parent.mkdir(parents=True, exist_ok=True)
        fh = a.out.open("w")
        sys.stdout = Tee(sys.__stdout__, fh)  # type: ignore[assignment]
    try:
        if a.stage == "probe":
            asyncio.run(stage_probe(a.cache_dir))
        elif a.stage == "meta":
            asyncio.run(stage_meta(a.cache_dir))
        elif a.stage == "universe":
            stage_universe(a.cache_dir)
        elif a.stage == "fetch":
            asyncio.run(stage_fetch(a.cache_dir))
        elif a.stage == "stage0":
            stage0(a.cache_dir)
        else:
            full(a.cache_dir)
        print("DONE", flush=True)
    finally:
        if fh:
            sys.stdout = sys.__stdout__
            fh.close()


if __name__ == "__main__":
    main()
