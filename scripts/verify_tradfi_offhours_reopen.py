"""Verification runner for IDEA-20261024-tradfi-perp-offhours-reopen (Binance TradFi perps, off-hours -> reopen).

Step 0 (no outcome means): symbol / history inventory in data.binance.vision, frozen-index check, closed-hours
volume, nights / weekends per symbol, n_eff, SD and MDE with nights as cluster units; gates G1-G3.

Stages:
  meta    exchangeInfo (TradFi only) + archive listings (monthly / daily klines, premiumIndexKlines)
  fetch   1m klines (and 1m premiumIndexKlines) for every TradFi symbol into the per-file cache
  stage0  inventory, liquidity, power and gates; prints compact tables

Network only through the runtime providers (rt.exchanges, rt.binance_vision). Cache: --cache-dir (outside the repo).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import math
import resource
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.tradfi_offhours import (
    MIN_MS,
    NYSE_HOLIDAYS,
    WINDOWS,
    center_by_date,
    et_ms,
    mde,
    night_record,
    nights,
    nyse_sessions,
    pairwise_corr,
    participation_ratio,
    premium_slope,
    price_at,
    random_basket_sd,
    volume_between,
    window_labels,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

UM = "data/futures/um"
TODAY = date(2026, 10, 24)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def read_json(p: Path) -> Any:
    return json.loads(p.read_text())


class Fetcher:
    """Per-file JSON cache in front of the archive provider; ``null`` marks a file the archive does not have."""

    def __init__(self, workers: int = 8) -> None:
        self.sem = asyncio.Semaphore(workers)
        self.done = 0
        self.errors: Counter[str] = Counter()

    async def one(self, path: Path, go: Callable[[], Awaitable[Any]]) -> None:
        if await asyncio.to_thread(path.exists):
            return
        async with self.sem:
            try:
                data = await go()
            except ProviderError as e:
                if e.reason != "data_unavailable":
                    self.errors[e.reason] += 1
                    return  # transient: leave uncached so a rerun retries
                data = None
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        await asyncio.to_thread(tmp.write_text, json.dumps(data, separators=(",", ":")))
        await asyncio.to_thread(tmp.replace, path)
        self.done += 1

    async def many(self, label: str, jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]]) -> None:
        todo = [(p, g) for p, g in jobs if not p.exists()]
        print(f"[{label}] {len(jobs)} files, {len(todo)} to fetch", flush=True)
        t0 = time.time()
        self.done = 0
        step = max(20, len(todo) // 20)

        async def run(p: Path, g: Callable[[], Awaitable[Any]]) -> None:
            await self.one(p, g)
            if self.done and self.done % step == 0:
                el = time.time() - t0
                eta = el / self.done * (len(todo) - self.done)
                print(f"  [{label}] {self.done}/{len(todo)} in {el:.0f}s, eta {eta:.0f}s, rss {rss_mb():.0f} MB", flush=True)

        await asyncio.gather(*[run(p, g) for p, g in todo])
        print(f"[{label}] done in {time.time() - t0:.0f}s; transient errors: {dict(self.errors)}", flush=True)


# ---------------------------------------------------------------- meta


def _dirs(listing: dict[str, Any]) -> list[str]:
    return sorted({d.rstrip("/").rsplit("/", 1)[-1] for d in listing["dirs"]})


async def stage_meta(cache: Path) -> None:
    _, rt = build_server()
    try:
        ip = cache / "exchange_info_tradfi.json"
        if not ip.exists():
            mk = (await rt.exchanges.markets("binanceusdm")).data or {}
            info: dict[str, Any] = {}
            ctypes: Counter[str] = Counter()
            for m in mk.values():
                i = (m or {}).get("info") or {}
                if not i.get("symbol"):
                    continue
                ctypes[f"{i.get('contractType')}/{i.get('underlyingType')}"] += 1
                if str(i.get("contractType") or "").startswith("TRADIFI") or i.get("underlyingType") not in (None, "COIN"):
                    keep = ("symbol", "contractType", "underlyingType", "underlyingSubType", "onboardDate", "status", "quoteAsset")
                    info[i["symbol"]] = {k: i.get(k) for k in keep}
            ip.write_text(json.dumps({"fetched_at": int(time.time()), "contract_types": dict(ctypes), "info": info}))
        v = rt.binance_vision
        for name, prefix in (
            ("archive_monthly_klines.json", f"{UM}/monthly/klines/"),
            ("archive_daily_klines.json", f"{UM}/daily/klines/"),
            ("archive_monthly_premium.json", f"{UM}/monthly/premiumIndexKlines/"),
            ("archive_daily_premium.json", f"{UM}/daily/premiumIndexKlines/"),
        ):
            p = cache / name
            if not p.exists():
                f = await v.list_dir(prefix, max_pages=200)
                p.write_text(json.dumps(_dirs(f.data)))
                print(f"listed {prefix}: {len(read_json(p))} symbols", flush=True)
    finally:
        await rt.aclose()
    d = read_json(cache / "exchange_info_tradfi.json")
    print("contract/underlying types (all binanceusdm):", d["contract_types"])
    mon, day = set(read_json(cache / "archive_monthly_klines.json")), set(read_json(cache / "archive_daily_klines.json"))
    pm, pd = set(read_json(cache / "archive_monthly_premium.json")), set(read_json(cache / "archive_daily_premium.json"))
    print(f"{'symbol':<14}{'contract':<20}{'underlying':<12}{'subtype':<22}{'onboard':<12}{'mK':>3}{'dK':>3}{'mP':>3}{'dP':>3}")
    for s, i in sorted(d["info"].items(), key=lambda kv: kv[1].get("onboardDate") or 0):
        ob = datetime.fromtimestamp((i.get("onboardDate") or 0) / 1000, tz=UTC).strftime("%Y-%m-%d")
        sub = ",".join(i.get("underlyingSubType") or [])[:21]
        flags = [int(s in x) for x in (mon, day, pm, pd)]
        print(
            f"{s:<14}{str(i.get('contractType')):<20}{str(i.get('underlyingType')):<12}{sub:<22}{ob:<12}"
            + "".join(f"{f:>3}" for f in flags)
        )


# ---------------------------------------------------------------- fetch

LAST_FULL_MONTH = (2026, 9)
DAILY_FROM, DAILY_TO = date(2026, 10, 1), date(2026, 10, 23)
PREMIUM_SAMPLE = ("SPYUSDT", "QQQUSDT", "AAPLUSDT", "NVDAUSDT", "TSLAUSDT", "COINUSDT", "XAUUSDT", "CLUSDT")
PREMIUM_MONTHS = ((2026, 8), (2026, 9))


def universe(cache: Path) -> dict[str, dict[str, Any]]:
    """US equities/ETFs and commodities (USDT-quoted TradFi perps); other sessions (HK/KR/CN, FX, pre-IPO) excluded."""
    info = read_json(cache / "exchange_info_tradfi.json")["info"]
    return {
        s: i
        for s, i in info.items()
        if i.get("contractType") == "TRADIFI_PERPETUAL" and i.get("underlyingType") in ("EQUITY", "COMMODITY") and s.endswith("USDT")
    }


def months_from(onboard_ms: int) -> list[tuple[int, int]]:
    d = datetime.fromtimestamp(onboard_ms / 1000, tz=UTC)
    y, m = d.year, d.month
    out = []
    while (y, m) <= LAST_FULL_MONTH:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def compact(rows: list[list[float]], since_ms: int) -> list[list[float]]:
    """[ts, open, high, low, close, quote_volume] from onboard time on (older rows can be a recycled crypto ticker)."""
    return [[int(r[0]), r[1], r[2], r[3], r[4], round(r[6], 2)] for r in rows if r[0] >= since_ms]


def kline_path(cache: Path, sym: str, tag: str) -> Path:
    return cache / "k1m" / sym / f"{tag}.json"


async def stage_fetch(cache: Path, limit: int | None) -> None:
    uni = universe(cache)
    syms = sorted(uni)[:limit] if limit else sorted(uni)
    _, rt = build_server()
    v = rt.binance_vision
    fx = Fetcher(workers=8)
    try:
        jobs: list[tuple[Path, Callable[[], Awaitable[Any]]]] = []
        for s in syms:
            ob = int(uni[s].get("onboardDate") or 0)
            for y, m in months_from(ob):

                async def go_m(s: str = s, y: int = y, m: int = m, ob: int = ob) -> Any:
                    return compact((await v.um_klines_monthly(s, "1m", y, m)).data, ob)

                jobs.append((kline_path(cache, s, f"{y:04d}-{m:02d}"), go_m))
            d = max(DAILY_FROM, datetime.fromtimestamp(ob / 1000, tz=UTC).date())
            while d <= DAILY_TO:

                async def go_d(s: str = s, day: str = d.isoformat(), ob: int = ob) -> Any:
                    return compact((await v.um_klines_daily(s, "1m", day)).data, ob)

                jobs.append((kline_path(cache, s, d.isoformat()), go_d))
                d += timedelta(days=1)
        for s in PREMIUM_SAMPLE:
            if s not in syms:
                continue
            for y, m in PREMIUM_MONTHS:

                async def go_p(s: str = s, y: int = y, m: int = m) -> Any:
                    rows = (await v.um_premium_index_monthly(s, "1m", y, m)).data
                    return [[int(r[0]), r[4]] for r in rows]

                jobs.append((cache / "premium1m" / s / f"{y:04d}-{m:02d}.json", go_p))
        await fx.many("klines+premium", jobs)
    finally:
        await rt.aclose()
    print(f"peak rss {rss_mb():.0f} MB", flush=True)


# ---------------------------------------------------------------- stage 0 (no outcome means)

MIN_NIGHTS = 60  # G3
MIN_WEEKENDS = 20
MIN_DAILY_VOL = 1_000_000.0  # USDT per day, median over days since listing
G1_NEFF, G2_MDE = 250, 0.0010
G3_MIN_SYMBOLS = 10  # a quintile basket needs >= 2 names per side (set before stage0 was run)


def load_symbol(cache: Path, sym: str) -> np.ndarray:
    """All cached 1m rows of one symbol as an array [ts, open, high, low, close, qvol] (sorted, de-duplicated)."""
    rows: list[list[float]] = []
    for p in sorted((cache / "k1m" / sym).glob("*.json")):
        d = read_json(p)
        if d:
            rows += d
    if not rows:
        return np.empty((0, 6))
    a = np.asarray(rows, dtype=float)
    a = a[np.argsort(a[:, 0], kind="stable")]
    keep = np.concatenate([[True], np.diff(a[:, 0]) > 0])
    return a[keep]


def load_premium_slope(cache: Path, sym: str, a: np.ndarray) -> dict[str, Any]:
    """Premium-index slope by window (pure computation in ``analytics.tradfi_offhours.premium_slope``)."""
    rows: list[list[float]] = []
    for p in sorted((cache / "premium1m" / sym).glob("*.json")):
        rows += read_json(p) or []
    if not rows:
        return {}
    pr = np.asarray(rows, dtype=float)
    return premium_slope(a[:, 0], a[:, 4], pr[:, 0], pr[:, 1])


def stage0(cache: Path, out_dir: Path) -> None:
    uni = universe(cache)
    sess = nyse_sessions(date(2025, 12, 1), DAILY_TO)
    ngt = nights(sess)
    per: dict[str, dict[str, Any]] = {}
    rec: dict[str, list[tuple[date, float, float, float]]] = {}  # sym -> (day, D, R, vol_closed)
    zero_share: dict[str, list[float | None]] = {}
    prem: dict[str, Any] = {}
    t0 = time.time()
    for k, s in enumerate(sorted(uni)):
        a = load_symbol(cache, s)
        if len(a) == 0:
            per[s] = {"rows": 0}
            continue
        ts, close, qv = a[:, 0], a[:, 4], a[:, 5]
        jumps = int((np.abs(np.diff(np.log(close))) > 0.2).sum())
        days = (ts[-1] - ts[0]) / 86_400_000 + 1 / 1440
        day_idx = (ts // 86_400_000).astype(np.int64)
        dv = np.bincount(day_idx - day_idx[0], weights=qv)
        dv = dv[dv > 0] if (dv > 0).any() else dv
        info = {
            "type": uni[s]["underlyingType"],
            "first": datetime.fromtimestamp(ts[0] / 1000, tz=UTC).date().isoformat(),
            "rows": len(a),
            "days": round(days, 1),
            "gaps_min": int(round(days * 1440)) - len(a),
            "jumps20": jumps,
            "med_daily_vol": float(np.median(dv)),
        }
        if uni[s]["underlyingType"] == "EQUITY":
            rs = []
            for n in ngt:
                if n.close_ms < ts[0] or n.exit_ms > ts[-1] + MIN_MS:
                    continue
                r = night_record(ts, close, qv, n)
                if r:
                    rs.append((n.day, r["D"], r["R"], r["vol_closed"]))
            rec[s] = rs
            info["nights"] = len(rs)
            info["med_closed_vol"] = float(np.median([x[3] for x in rs])) if rs else 0.0
            if s in ("SPYUSDT", "QQQUSDT", "AAPLUSDT", "NVDAUSDT", "TSLAUSDT"):
                lab = window_labels(ts[-30 * 1440 :])
                qq = qv[-30 * 1440 :]
                zero_share[s] = [float((qq[lab == w] == 0).mean()) if (lab == w).any() else None for w in WINDOWS]
        else:
            wk = 0
            wvol = []
            fri = [d for d in (date(2025, 12, 5) + timedelta(days=7 * i) for i in range(50)) if d <= DAILY_TO]
            for f in fri:
                c_ms, o_ms = et_ms(f, 17, 0), et_ms(f + timedelta(days=2), 18, 0)
                if c_ms < ts[0] or o_ms > ts[-1]:
                    continue
                if price_at(ts, close, c_ms) and price_at(ts, close, o_ms - 10 * MIN_MS):
                    wk += 1
                    wvol.append(volume_between(ts, qv, c_ms, o_ms))
            info["weekends"] = wk
            info["med_weekend_vol"] = float(np.median(wvol)) if wvol else 0.0
        if s in PREMIUM_SAMPLE:
            prem[s] = load_premium_slope(cache, s, a)
        per[s] = info
        del a, ts, close, qv
        if k % 25 == 0:
            print(f"  [stage0] {k + 1}/{len(uni)} {time.time() - t0:.0f}s rss {rss_mb():.0f} MB", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stage0_per_symbol.json").write_text(json.dumps(per, indent=0, default=str))

    # ---- inventory
    eq = {s: i for s, i in per.items() if i.get("type") == "EQUITY"}
    cm = {s: i for s, i in per.items() if i.get("type") == "COMMODITY"}
    print(f"\nuniverse: {len(eq)} US equity/ETF perps, {len(cm)} commodity perps (TRADIFI_PERPETUAL, USDT)")
    print("ETF perps for market neutralisation:", {s: eq.get(s, {}).get("first") for s in ("SPYUSDT", "QQQUSDT", "IWMUSDT")})
    print("sanity: symbols with >20% 1m jumps:", {s: i["jumps20"] for s, i in per.items() if i.get("jumps20")})
    gaps = sorted(((i.get("gaps_min", 0) / max(1, i.get("days", 1) * 1440), s) for s, i in per.items() if i.get("rows")), reverse=True)[:5]
    print("largest missing-minute shares:", [(s, f"{g:.3%}") for g, s in gaps])
    print("\nzero-volume 1m share by window (last 30 days):", " ".join(WINDOWS))
    for s, z in zero_share.items():
        print(f"  {s:<10}", " ".join("  n/a " if v is None else f"{v:6.1%}" for v in z))
    print("\npremium-index slope on perp log change, 5-min steps (1 = index frozen, 0 = live), (slope, n):")
    for s, d in prem.items():
        print(f"  {s:<10}", {w: v for w, v in d.items()})

    print("\ncommodities (weekends Fri 17:00 ET -> Sun 18:00 ET):")
    for s, i in sorted(cm.items()):
        print(
            f"  {s:<11} first {i['first']} weekends {i['weekends']:>3} med daily vol ${i['med_daily_vol'] / 1e6:7.1f}M med weekend vol ${i['med_weekend_vol'] / 1e6:6.2f}M"
        )

    # ---- equity gates
    liq = {s for s, i in eq.items() if i.get("med_daily_vol", 0) >= MIN_DAILY_VOL}
    g3 = {s for s in liq if eq[s].get("nights", 0) >= MIN_NIGHTS}
    hist = Counter(min(eq[s].get("nights", 0) // 20 * 20, 160) for s in eq)
    print(f"\nequity nights-per-symbol histogram (bucket=20, 160=160+): {dict(sorted(hist.items()))}")
    print(f"liquid (median daily vol >= $1M): {len(liq)} of {len(eq)}; liquid with >= {MIN_NIGHTS} nights (G3): {len(g3)}")
    vols = sorted(((eq[s]["med_daily_vol"], eq[s]["med_closed_vol"], s) for s in eq if eq[s].get("nights")), reverse=True)
    print("median daily / closed-hours (16:00 -> 09:25 ET) volume, top 12 and quartiles, $M:")
    print("  ", ", ".join(f"{s[:-4]} {v / 1e6:.0f}/{c / 1e6:.1f}" for v, c, s in vols[:12]))
    dvs = np.array([v for v, _, _ in vols])
    cvs = np.array([c for _, c, _ in vols])
    print(f"   daily q25/q50/q75 {np.percentile(dvs, [25, 50, 75]) / 1e6}; closed q25/q50/q75 {np.percentile(cvs, [25, 50, 75]) / 1e6}")

    for label, members in (("G3 set (liquid, >= 60 nights)", sorted(g3)), ("liquid, any history", sorted(liq))):
        rows_r = [(d, s, R) for s in members for d, _, R, _ in rec[s]]
        cen = center_by_date(rows_r)
        by_date: dict[date, list[float]] = {}
        for (d, _s), x in cen.items():
            by_date.setdefault(d, []).append(x)
        by_date = {d: v for d, v in by_date.items() if len(v) >= 5}
        m_per = np.array([len(v) for v in by_date.values()])
        xs = np.array([x for v in by_date.values() for x in v])
        sd_r = float(np.std(xs, ddof=1)) if len(xs) > 2 else float("nan")
        # effective factors per night: participation ratio of the pairwise correlation matrix of centred R
        syms = sorted(members)
        days = sorted(by_date)
        mat = np.full((len(days), len(syms)), np.nan)
        di, si = {d: i for i, d in enumerate(days)}, {s: j for j, s in enumerate(syms)}
        for (d, s), x in cen.items():
            if d in di:
                mat[di[d], si[s]] = x
        pr = float("nan")
        if len(syms) >= 3:
            pr = participation_ratio(np.linalg.eigvalsh(pairwise_corr(mat, min_periods=30)))
        n_nights = len(by_date)
        n_eff = float(np.minimum(m_per, pr).sum()) if n_nights else 0.0
        sd_q, n_q = random_basket_sd(by_date, frac=0.2)
        sd_5, n_5 = random_basket_sd({d: v for d, v in by_date.items()}, frac=5 / max(1, int(np.median(m_per))) if n_nights else 0.2)
        print(
            f"\n[{label}] symbols {len(members)}, nights with >= 5 names {n_nights}, names/night median {np.median(m_per) if n_nights else 0:.0f}"
        )
        print(f"   SD of date-centred target R (09:25 -> 10:30 ET), pooled symbol-nights: {sd_r:.4%} (n {len(xs)})")
        print(
            f"   participation ratio of centred-R correlations (effective factors): {pr:.1f}; n_eff = sum_nights min(m, PR) = {n_eff:.0f}"
        )
        mde_slope = mde(sd_r, int(n_eff)) if n_eff else None
        print(f"   MDE on the slope-scale target (2.8 SD/sqrt n_eff): {mde_slope if mde_slope is None else f'{mde_slope:.4%}'}")
        print(f"   random quintile long-short, night SD {sd_q:.4%} over {n_q} nights -> MDE(nights) {mde(sd_q, n_q):.4%}")
        print(f"   random ~5-per-side long-short, night SD {sd_5:.4%} over {n_5} nights -> MDE(nights) {mde(sd_5, n_5):.4%}")
        if label.startswith("G3"):
            # exploratory (defined after seeing G2): does the noise level hinge on tails or on the least liquid names?
            lo, hi = np.percentile(xs, [1, 99])
            sd_w, n_w = random_basket_sd({d: list(np.clip(v, lo, hi)) for d, v in by_date.items()}, frac=0.2)
            print(
                f"   [exploratory] R winsorised at 1/99 pct ({lo:.2%}/{hi:.2%}): quintile night SD {sd_w:.4%} -> MDE {mde(sd_w, n_w):.4%}"
            )
            top = sorted(members, key=lambda s: -eq[s]["med_closed_vol"])[: len(members) // 2]
            cen_t = center_by_date([(d, s, R) for s in top for d, _, R, _ in rec[s]])
            bd_t: dict[date, list[float]] = {}
            for (d, _s), x in cen_t.items():
                bd_t.setdefault(d, []).append(x)
            sd_t, n_t = random_basket_sd({d: v for d, v in bd_t.items() if len(v) >= 5}, frac=0.2)
            print(
                f"   [exploratory] top half by closed-hours volume ({len(top)} names): quintile night SD {sd_t:.4%} over {n_t} nights -> MDE {mde(sd_t, n_t):.4%}"
            )
            need = int(math.ceil(n_q * (mde(sd_q, n_q) / G2_MDE) ** 2))
            print(f"   nights needed for MDE <= 0.10 % at this SD: {need} (have {n_q})")
            g1 = n_eff >= G1_NEFF
            g2 = mde(sd_q, n_q) is not None and mde(sd_q, n_q) <= G2_MDE
            g3ok = len(g3) >= G3_MIN_SYMBOLS
            print(
                f"\nGATES: G1 n_eff {n_eff:.0f} >= {G1_NEFF}: {g1}; G2 MDE(nights, quintile) {mde(sd_q, n_q):.4%} <= 0.10 %: {g2}; "
                f"G3 liquid symbols with >= {MIN_NIGHTS} nights: {len(g3)} >= {G3_MIN_SYMBOLS}: {g3ok}"
            )
    print(f"\npeak rss {rss_mb():.0f} MB")


def stage_calendar(cache: Path) -> None:
    """Calendar vs data: volume in the 5 minutes after the computed 09:30 ET open over the 5 minutes before it, per
    session (TSLA covers the DST switch of 8 March). A wrong open time (DST bug) shows as a ratio near 1."""
    a = load_symbol(cache, "TSLAUSDT")
    ts, qv = a[:, 0], a[:, 5]
    rows = []
    for s in nyse_sessions(date(2026, 2, 2), DAILY_TO):
        after = volume_between(ts, qv, s.open_ms, s.open_ms + 5 * MIN_MS)
        before = volume_between(ts, qv, s.open_ms - 5 * MIN_MS, s.open_ms)
        shifted = volume_between(ts, qv, s.open_ms + 60 * MIN_MS, s.open_ms + 65 * MIN_MS)  # if DST were ignored
        if before > 0:
            rows.append((s.day, after / before, shifted / before))
    for lab, lo, hi in (
        ("before DST (Feb 2 - Mar 6)", date(2026, 2, 2), date(2026, 3, 6)),
        ("after DST (Mar 9 - Oct 23)", date(2026, 3, 9), DAILY_TO),
    ):
        r = [x for x in rows if lo <= x[0] <= hi]
        print(
            f"TSLA {lab}: sessions {len(r)}, median vol ratio open+5m / open-5m {np.median([x[1] for x in r]):.1f}, (open+60..65m)/open-5m {np.median([x[2] for x in r]):.1f}"
        )
    hol = [
        volume_between(ts, qv, et_ms(d, 9, 30), et_ms(d, 9, 35)) / max(1.0, volume_between(ts, qv, et_ms(d, 9, 25), et_ms(d, 9, 30)))
        for d in sorted(NYSE_HOLIDAYS)
        if date(2026, 2, 2) <= d <= DAILY_TO
    ]
    print(f"TSLA NYSE holidays in sample: same ratio at 09:30 ET {[round(x, 1) for x in hol]}")


async def stage_probe_book(cache: Path) -> None:
    """Does the archive keep order-book data (bookDepth, bookTicker) for TradFi perps? Listing only, no outcomes."""
    _, rt = build_server()
    try:
        for kind in ("bookDepth", "bookTicker"):
            for s in ("SPYUSDT", "NVDAUSDT", "XAUUSDT"):
                f = await rt.binance_vision.list_dir(f"{UM}/daily/{kind}/{s}/", max_pages=5)
                keys = sorted(f.data["keys"])
                zips = [k.rsplit("/", 1)[-1] for k in keys if k.endswith(".zip")]
                print(
                    f"{kind:<10} {s:<9} files {len(zips):>4} first {zips[0] if zips else '-'} last {zips[-1] if zips else '-'}", flush=True
                )
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["meta", "fetch", "probe_book", "calendar", "stage0"])
    ap.add_argument("--cache-dir", type=Path, default=Path("~/.cache/tradedesk-agents/tradfi_offhours_reopen"))
    ap.add_argument("--limit", type=int, default=None, help="fetch only the first N symbols (timing sample)")
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "meta":
        asyncio.run(stage_meta(cache))
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(cache, a.limit))
    elif a.stage == "calendar":
        stage_calendar(cache)
    elif a.stage == "probe_book":
        asyncio.run(stage_probe_book(cache))
    elif a.stage == "stage0":
        out = cache / "out"
        out.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()

        class Tee(io.TextIOBase):
            def write(self, s: str) -> int:
                buf.write(s)
                return sys.__stdout__.write(s)

        with contextlib.redirect_stdout(Tee()):
            stage0(cache, out)
        (out / "stage0.txt").write_text(buf.getvalue())


if __name__ == "__main__":
    main()
