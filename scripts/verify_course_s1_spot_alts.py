"""Course setups, step 9 (IDEA-20261016-course-setups-crypto): S1 — spot alts near the "weekly support" (daily
SMA 100 / 200); H1-H3 of LIB-20261015-spot-entries.

H1 (primary): the first test of the SMA 100 by a strong alt (close >= 1.6 x SMA 100 within 120 days) in an uptrend,
held 60 days, against other alts in the same moving-average state on the same dates (P_reg). H2 (secondary): reclaim
of a lost triple support, 30 days. H3 (descriptive): alts relative to BTC while BTC is sideways after a drop.
Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 9". Stages:
  uv run python scripts/verify_course_s1_spot_alts.py --stage prereg --out ~/.cache/tradedesk-agents/course_step9_s1/registration.txt
  uv run python scripts/verify_course_s1_spot_alts.py --stage fetch  --out ~/.cache/tradedesk-agents/course_step9_s1/fetch.log
  uv run python scripts/verify_course_s1_spot_alts.py --stage count  --out ~/.cache/tradedesk-agents/course_step9_s1/count.md
  uv run python scripts/verify_course_s1_spot_alts.py --stage report --out ~/.cache/tradedesk-agents/course_step9_s1/report.md
Data: daily USD-M perp klines from data.binance.vision (rt.binance_vision): the step-1 cache (--s14-cache, < 2021-10),
the funding-crowding cache (--data-dir, >= 2021-10) and this step's cache (--cache-dir/k1d, files missing in both,
fetched by ``fetch``). ``count`` (stage 0) counts events without any return. No keys, no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s14  # noqa: E402  (eligible symbols, listings, month helpers)

from tradedesk.analytics.course_s1_spot import (  # noqa: E402
    DAY_MS,
    SIDE_COST,
    closes_below,
    event_minus_placebo,
    first_close_above,
    first_touch_events,
    forward_exit,
    h1_verdict,
    one_at_a_time,
    pick_matched,
    reclaim_events,
    seg_sma,
    segments,
    sideways_after_drop,
    state_onsets,
    uptrend_state,
    xs_median_rel,
)
from tradedesk.analytics.course_s13_drawdown import cluster_bootstrap_mean, shift_null  # noqa: E402
from tradedesk.analytics.liquidation_flush import base_of, cluster_bootstrap_means, holm, without_top_days  # noqa: E402
from tradedesk.analytics.per_asset import asset_stats, heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.session_stop_hunt import cluster_bootstrap_diff  # noqa: E402
from tradedesk.core.errors import ProviderError  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
ms = s14.ms
START_MS = ms("2020-02-01T00:00")  # entry = close of the event day, from here
DATA_END_MS = ms("2026-10-01T00:00")  # close of the last cached day
OOS_MS = ms("2025-03-01T00:00")
EXTRA_MONTHS = ("2019-09", "2019-10", "2019-11", "2019-12")
TOP_N = 50
SEED, N_BOOT = 20261017, 10_000
PER_EVENT, WINDOW_DAYS = 3, 15
STEP_P = (0.985, 0.280, 0.448, 0.038, 0.822, 1.0, 0.394, 0.563)  # raw primary p of steps 1-8 (Holm family)
MIN_DATES, GATE, MIN_EFFECT, MIN_OOS_DATES = 100, 0.10, 0.05, 20
H2_MIN_EFFECT = 0.03
SPOT_SIDE = 0.0012  # stress: spot taker 0.10 % + slippage 0.02 % per side
FUND_DAY = 0.0003  # stress: 0.01 % per 8 h against the long
PRIMARY = ("r1.6", 60)
H1_VARIANTS = [(f"r{r}", h) for r in ("1.4", "1.6", "2.0") for h in (30, 60, 90)] + [
    ("sma200", 60),
    ("down", 60),
    ("nofilter", 60),
    ("after_close", 60),
]
VAR_LABEL = {
    "r1.4": "сила ≥ 1.4",
    "r1.6": "сила ≥ 1.6",
    "r2.0": "сила ≥ 2.0",
    "sma200": "тест SMA200 (сила 1.6)",
    "down": "гілка SMA100 ≤ SMA200",
    "nofilter": "без фільтра сили (30 барів без дотику)",
    "after_close": "вхід після закриття над SMA100 (≤ 10 барів)",
}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def day_str(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, tz=UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 9")
    b = t.index("\n## ", a + 1)
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- files and fetch


def months_of(data_dir: Path, s: str) -> list[str]:
    mo = s14.listing(data_dir, s)
    return [*EXTRA_MONTHS, *mo] if mo and mo[0] == "2020-01" else mo


def kline_path(s14_cache: Path, data_dir: Path, own: Path, s: str, mo: str) -> Path | None:
    for p in (
        (s14_cache / "k1d" / s / f"{mo}.json") if mo < "2021-10" else (data_dir / "k1d" / s / f"{mo}.json"),
        own / "k1d" / s / f"{mo}.json",
    ):
        if p.exists():
            return p
    return None


def stage_fetch(s14_cache: Path, data_dir: Path, own: Path, limit: int | None) -> None:
    syms = s14.eligible_syms(data_dir)
    jobs = [(s, mo) for s in syms for mo in months_of(data_dir, s) if kline_path(s14_cache, data_dir, own, s, mo) is None]
    emit(
        f"eligible COIN perps {len(syms)}; daily files missing in the caches: {len(jobs)} (months 2019-09 … 2019-12 tried for symbols listed from 2020-01)"
    )
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        return

    async def run() -> None:
        from tradedesk.server import build_server

        _, rt = build_server()
        sem = asyncio.Semaphore(6)
        errors: Counter[str] = Counter()
        done, t0 = 0, time.time()

        async def one(s: str, mo: str) -> None:
            nonlocal done
            async with sem:
                try:
                    f = await rt.binance_vision.um_klines_monthly(s, "1d", int(mo[:4]), int(mo[5:]))
                    rows: Any = [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in f.data]
                except ProviderError as e:
                    if e.reason != "data_unavailable":
                        errors[e.reason] += 1
                        return
                    rows = None
            s14.wr(own / "k1d" / s / f"{mo}.json", rows)
            done += 1
            if done % 50 == 0 or done == len(jobs):
                emit(f"  [fetch] {done}/{len(jobs)} in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")

        try:
            await asyncio.gather(*[one(s, mo) for s, mo in jobs])
        finally:
            await rt.aclose()
        emit(f"[fetch] done {done}/{len(jobs)} in {time.time() - t0:.0f}s; transient errors {dict(errors)}")

    asyncio.run(run())


# ---------------------------------------------------------------- loading


@dataclass
class Sym:
    name: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    qv: np.ndarray
    seg: np.ndarray = field(init=False)
    s100: np.ndarray = field(init=False)
    s200: np.ndarray = field(init=False)
    day: np.ndarray = field(init=False)
    close_ms: np.ndarray = field(init=False)
    member: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self.seg = segments(self.t)
        self.s100 = seg_sma(self.c, self.seg, 100)
        self.s200 = seg_sma(self.c, self.seg, 200)
        self.day = (self.t // DAY_MS).astype(np.int64)
        self.close_ms = self.t + DAY_MS
        self.member = np.zeros(self.t.size, dtype=bool)


def load_sym(s14_cache: Path, data_dir: Path, own: Path, s: str) -> Sym | None:
    rows: list[list[float]] = []
    for mo in months_of(data_dir, s):
        p = kline_path(s14_cache, data_dir, own, s, mo)
        if p is None:
            continue
        for r in json.loads(p.read_text()) or []:
            rows.append([float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6] if len(r) > 6 else r[5])])
    if not rows:
        return None
    a = np.array(rows, dtype=float)
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
    a = a[(a[:, 5] > 0) & (a[:, 0] % DAY_MS == 0) & (a[:, 4] > 0)]  # zero-volume days count as missing
    if a.shape[0] == 0:
        return None
    return Sym(s, a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5])


def build_universe(syms: dict[str, Sym]) -> dict[str, list[str]]:
    """Monthly top-50 alts (not BTC) by quote volume over the 30 full days before the month (>= 20 days), one symbol
    per base asset (the larger volume); ties by symbol name (as liquidation_flush.monthly_universe)."""
    out: dict[str, list[str]] = {}
    for mo in s14.months("2020-02", "2026-09"):
        m_ms = ms(f"{mo}-01T00:00")
        lo = m_ms - 30 * DAY_MS
        ranked = []
        for s, x in syms.items():
            if s == "BTCUSDT":
                continue
            a, b = np.searchsorted(x.t, lo, "left"), np.searchsorted(x.t, m_ms, "left")
            if b - a >= 20:
                ranked.append((-float(x.qv[a:b].sum()), s))
        ranked.sort()
        seen: set[str] = set()
        mem: list[str] = []
        for _, s in ranked:
            bs = base_of(s)
            if bs in seen:
                continue
            seen.add(bs)
            mem.append(s)
            if len(mem) == TOP_N:
                break
        out[mo] = mem
    return out


def load_all(s14_cache: Path, data_dir: Path, own: Path, limit: int | None) -> tuple[dict[str, Sym], dict[str, list[str]]]:
    names = s14.eligible_syms(data_dir)
    if limit:
        names = sorted(set(names[:limit]) | {"BTCUSDT", "ETHUSDT"})
    t0 = time.time()
    syms: dict[str, Sym] = {}
    for n, s in enumerate(names):
        x = load_sym(s14_cache, data_dir, own, s)
        if x is not None:
            syms[s] = x
        if (n + 1) % 200 == 0:
            emit(f"  [load] {n + 1}/{len(names)} in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    uni_p = own / ("universe50.json" if not limit else "universe50_sample.json")
    uni = build_universe(syms)
    s14.wr(uni_p, uni)
    by_month = {mo: set(v) for mo, v in uni.items()}
    for s, x in syms.items():
        mon = [datetime.fromtimestamp(int(cm) / 1000, tz=UTC).strftime("%Y-%m") for cm in x.close_ms]
        x.member = np.array([s in by_month.get(m, ()) for m in mon], dtype=bool)
    emit(f"  [load] {len(syms)} symbols with daily bars in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    return syms, uni


# ---------------------------------------------------------------- per-symbol precomputation


@dataclass
class Fwd:
    j: np.ndarray
    data_end: np.ndarray
    gross: np.ndarray
    btc_ratio: np.ndarray


def fwd_table(x: Sym, h: int, btc_close: dict[int, float]) -> Fwd:
    j, de = forward_exit(x.t, x.seg, h)
    g = np.full(x.t.size, np.nan)
    br = np.full(x.t.size, np.nan)
    ok = j >= 0
    g[ok] = x.c[j[ok]] / x.c[ok] - 1.0
    for i in np.flatnonzero(ok):
        a, b = btc_close.get(int(x.day[i])), btc_close.get(int(x.day[j[i]]))
        if a and b:
            br[i] = b / a
    return Fwd(j, de, g, br)


def in_window(x: Sym, h: int) -> np.ndarray:
    return (x.close_ms >= START_MS) & (x.close_ms <= DATA_END_MS - h * DAY_MS)


@dataclass
class Pool:
    """Candidate (symbol, bar) rows for the placebo, sorted by day."""

    sym: np.ndarray
    bar: np.ndarray
    day: np.ndarray
    up: np.ndarray
    down: np.ndarray
    touch_up: np.ndarray
    names: list[str]
    names_arr: np.ndarray

    @classmethod
    def build(cls, syms: dict[str, Sym]) -> Pool:
        names = sorted(syms)
        cols: dict[str, list[np.ndarray]] = defaultdict(list)
        for k, s in enumerate(names):
            x = syms[s]
            m = x.member & (x.close_ms >= START_MS) & (x.close_ms <= DATA_END_MS - 30 * DAY_MS)
            idx = np.flatnonzero(m)
            up = uptrend_state(x.c, x.s100, x.s200, x.seg)
            dn = uptrend_state(x.c, x.s100, x.s200, x.seg, trend="down")
            with np.errstate(invalid="ignore"):
                tu = up & (x.lo <= x.s100)
            cols["sym"].append(np.full(idx.size, k))
            cols["bar"].append(idx)
            cols["day"].append(x.day[idx])
            cols["up"].append(up[idx])
            cols["down"].append(dn[idx])
            cols["touch_up"].append(tu[idx])
        cat = {k: np.concatenate(v) for k, v in cols.items()}
        o = np.argsort(cat["day"], kind="stable")
        return cls(**{k: v[o] for k, v in cat.items()}, names=names, names_arr=np.array(names, dtype=object))


# ---------------------------------------------------------------- events and rows


def h1_events(x: Sym, var: str) -> np.ndarray:
    if var.startswith("r"):
        return first_touch_events(x.c, x.lo, x.s100, x.s100, x.s200, x.seg, ratio=float(var[1:]))
    if var in ("sma200",):
        return first_touch_events(x.c, x.lo, x.s200, x.s100, x.s200, x.seg, ratio=1.6)
    if var == "down":
        return first_touch_events(x.c, x.lo, x.s100, x.s100, x.s200, x.seg, ratio=1.6, trend="down")
    if var == "nofilter":
        return first_touch_events(x.c, x.lo, x.s100, x.s100, x.s200, x.seg, ratio=None, quiet=30)
    if var == "after_close":
        ev = first_touch_events(x.c, x.lo, x.s100, x.s100, x.s200, x.seg, ratio=1.6)
        out = [first_close_above(x.c, x.s100, x.seg, int(t), within=10) for t in ev]
        return np.array(sorted({u for u in out if u >= 0}), dtype=np.int64)
    raise ValueError(var)


def trade(x: Sym, i: int, fw: Fwd) -> dict[str, Any] | None:
    j = int(fw.j[i])
    if j < 0:
        return None
    g = float(fw.gross[i])
    br = float(fw.btc_ratio[i])
    return {
        "sym": x.name,
        "bar_day": int(x.day[i]),
        "exit_ms": int(x.close_ms[j]),
        "entry_ms": int(x.close_ms[i]),
        "gross": g,
        "net": g - SIDE_COST * (2.0 + g),
        "rel": (1.0 + g) / br - 1.0 if math.isfinite(br) and br > 0 else float("nan"),
        "data_end": bool(fw.data_end[i]),
        "hold": (int(x.t[j]) - int(x.t[i])) / DAY_MS,
        "below200": closes_below(x.c, x.s200, x.seg, i, within=10),
    }


@dataclass
class Ctx:
    syms: dict[str, Sym]
    pool: Pool
    fwd: dict[tuple[str, int], Fwd]
    btc_up: dict[int, bool]
    rng: np.random.Generator


def add_placebo(
    ctx: Ctx,
    rows: list[dict[str, Any]],
    ev_row: dict[str, Any],
    ev_id: int,
    mask: np.ndarray,
    h: int,
    kind: str,
    var: str,
    ex_sym: str,
    ex_bar: int,
) -> None:
    p = ctx.pool
    k_ex = None
    lo = int(np.searchsorted(p.day, ev_row["bar_day"] - WINDOW_DAYS, "left"))
    hi = int(np.searchsorted(p.day, ev_row["bar_day"] + WINDOW_DAYS, "right"))
    hit = lo + np.flatnonzero((p.names_arr[p.sym[lo:hi]] == ex_sym) & (p.bar[lo:hi] == ex_bar)) if hi > lo else np.array([], dtype=np.int64)
    if hit.size:
        k_ex = int(hit[0])
    for k in pick_matched(ctx.rng, p.day, mask, ev_row["bar_day"], window=WINDOW_DAYS, k=PER_EVENT, exclude=k_ex):
        s = p.names[int(p.sym[k])]
        x = ctx.syms[s]
        tr = trade(x, int(p.bar[k]), ctx.fwd[(s, h)])
        if tr is None:
            continue
        rows.append(
            {**tr, "kind": kind, "var": var, "h": h, "ev": ev_id, "day": ev_row["day"], "month": ev_row["month"], "oos": ev_row["oos"]}
        )


def collect(ctx: Ctx, outcomes: bool) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    p = ctx.pool
    window_ok = {h: np.array([(DATA_END_MS - h * DAY_MS) >= (d + 1) * DAY_MS for d in p.day]) for h in (30, 60, 90)}
    ev_id = 0
    for s in sorted(ctx.syms):
        if s == "BTCUSDT":
            continue
        x = ctx.syms[s]
        for var, h in [*H1_VARIANTS, ("reclaim", 30)]:
            if var == "reclaim":
                ev = np.array([e[0] for e in reclaim_events(x.c, x.lo, x.seg)], dtype=np.int64)
            else:
                ev = h1_events(x, var)
            ok = in_window(x, h)
            fw = ctx.fwd[(s, h)]
            cand = [int(t) for t in ev if ok[t] and x.member[t]]
            why[f"{var}/{h}:raw"] += int(ev.size)
            why[f"{var}/{h}:not_member_or_period"] += int(ev.size) - len(cand)
            cand = [t for t in cand if fw.j[t] >= 0]
            if not cand:
                continue
            keep = one_at_a_time([int(x.close_ms[t]) for t in cand], [int(x.close_ms[fw.j[t]]) for t in cand])
            why[f"{var}/{h}:busy"] += int((~keep).sum())
            for t, kp in zip(cand, keep, strict=True):
                if not kp:
                    continue
                d = int(x.day[t])
                base = {
                    "sym": s,
                    "var": var,
                    "h": h,
                    "ev": ev_id,
                    "day": day_str(d),
                    "month": day_str(d)[:7],
                    "oos": int(x.close_ms[t]) >= OOS_MS,
                    "bar_day": d,
                    "btc_up": ctx.btc_up.get(d),
                    "dist200": float(x.c[t] / x.s200[t] - 1) if math.isfinite(x.s200[t]) else float("nan"),
                }
                if not outcomes:
                    rows.append({**base, "kind": "ev"})
                    ev_id += 1
                    continue
                tr = trade(x, t, fw)
                assert tr is not None
                ev_row = {**base, **tr, "kind": "ev"}
                rows.append(ev_row)
                wk = window_ok[h]
                if var == "reclaim":
                    add_placebo(ctx, rows, ev_row, ev_id, wk, h, "pany", var, s, t)
                elif var == "down":
                    add_placebo(ctx, rows, ev_row, ev_id, p.down & wk, h, "preg", var, s, t)
                else:
                    add_placebo(ctx, rows, ev_row, ev_id, p.up & wk, h, "preg", var, s, t)
                    if (var, h) == PRIMARY:
                        add_placebo(ctx, rows, ev_row, ev_id, p.touch_up & wk, h, "ptouch", var, s, t)
                ev_id += 1
    return rows, why


# ---------------------------------------------------------------- stats


def fmt_ci(ci: tuple[float, float] | None, pct: bool = True) -> str:
    if ci is None:
        return "—"
    return f"[{ci[0]:+.1%}; {ci[1]:+.1%}]" if pct else f"[{ci[0]:+.3f}; {ci[1]:+.3f}]"


def fp(p: float | None) -> str:
    return "—" if p is None else f"{p:.4f}"


def summ(rows: list[dict[str, Any]], key: str = "net", cl: str = "day", n_boot: int = 5000) -> dict[str, Any]:
    v = [r[key] for r in rows if math.isfinite(r[key])]
    c = [r[cl] for r in rows if math.isfinite(r[key])]
    if not v:
        return {"n": 0, "days": 0, "mean": float("nan"), "median": float("nan"), "ci": None, "p": None, "se": None, "win": float("nan")}
    multi = len(set(c)) > 1
    bm = cluster_bootstrap_means(v, c, n_boot=n_boot, seed=SEED) if multi else np.array([])
    return {
        "n": len(v),
        "days": len({r["day"] for r in rows}),
        "months": len({r["month"] for r in rows}),
        "mean": statistics.fmean(v),
        "median": statistics.median(v),
        "win": sum(1 for a in v if a > 0) / len(v),
        "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))) if multi else None,
        "p": float((np.sum(bm <= 0) + 1) / (bm.size + 1)) if multi else None,
        "se": float(np.std(bm)) if multi else None,
    }


def diff(a: list[dict[str, Any]], b: list[dict[str, Any]], cl: str = "day", key: str = "net", n_boot: int = 5000) -> dict[str, Any]:
    a = [r for r in a if math.isfinite(r[key])]
    b = [r for r in b if math.isfinite(r[key])]
    if not a or not b:
        return {"diff": float("nan"), "ci": None, "p": None, "se": float("nan")}
    out = cluster_bootstrap_diff([r[key] for r in a], [r[cl] for r in a], [r[key] for r in b], [r[cl] for r in b], n_boot=n_boot, seed=SEED)
    out["se"] = (out["ci"][1] - out["ci"][0]) / (2 * 1.959964) if out["ci"] is not None else float("nan")
    return out


def sel(rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return [r for r in rows if all(r.get(k) == v for k, v in kw.items())]


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [r for r in rows if not r["oos"]], [r for r in rows if r["oos"]]


HDR = "| вибірка | n | дат | міс. | частка net>0 | E[gross] | E[net] | медіана net | 95 % CI net (дати) | p (E>0) |\n|---|---|---|---|---|---|---|---|---|---|"


def line(label: str, rows: list[dict[str, Any]], key: str = "net", n_boot: int = 5000) -> str:
    st = summ(rows, key, "day", n_boot)
    if not st["n"]:
        return f"| {label} | 0 | — | — | — | — | — | — | — | — |"
    g = statistics.fmean(r["gross"] for r in rows)
    return f"| {label} | {st['n']} | {st['days']} | {st['months']} | {st['win']:.1%} | {g:+.1%} | {st['mean']:+.1%} | {st['median']:+.1%} | {fmt_ci(st['ci'])} | {fp(st['p'])} |"


def q(xs: list[float], p: float) -> float:
    xs = [a for a in xs if math.isfinite(a)]
    return float(np.quantile(np.asarray(xs), p)) if xs else float("nan")


# ---------------------------------------------------------------- sanity


def sanity(syms: dict[str, Sym], uni: dict[str, list[str]], data_dir: Path) -> None:
    emit("## Санітарні перевірки")
    b = syms["BTCUSDT"]
    k = int(np.searchsorted(b.t, ms("2024-12-31T00:00")))
    emit(
        f"- BTCUSDT перп: денне закриття 2024-12-31 = {b.c[k]:g} (еталон 93548.9); перший денний бар {day_str(int(b.day[0]))}; SMA200 з {day_str(int(b.day[np.flatnonzero(np.isfinite(b.s200))[0]]))}"
    )
    n_seg = sum(int(x.seg.max()) + 1 for x in syms.values())
    emit(f"- символів з денними барами: {len(syms)}; сегментів: {n_seg} (розрив > 2 діб — новий сегмент)")
    jumps = []
    for s, x in syms.items():
        if x.t.size < 2:
            continue
        r = x.c[1:] / x.c[:-1]
        same = x.seg[1:] == x.seg[:-1]
        for i in np.flatnonzero(same & ((r > 3) | (r < 1 / 3))):
            jumps.append(f"{s} {day_str(int(x.day[i + 1]))} ×{r[i]:.2f}")
    emit(f"- стрибки close/close[−1] > ×3 або < ×⅓ усередині сегмента ({len(jumps)}): {jumps[:40]}")
    allm = sorted({s for v in uni.values() for s in v})
    info = s14.rd(data_dir / "exchange_info.json")["info"]
    st = Counter((info.get(s) or {}).get("status", "absent") for v in uni.values() for s in v)
    emit(f"- всесвіт: {len(uni)} місяців, різних членів {len(allm)}; член-місяці за сьогоднішнім статусом {dict(st)}")
    for mo in ("2020-08", "2021-05", "2022-06", "2024-03", "2026-09"):
        emit(f"  - {mo} ({len(uni[mo])}): {', '.join(uni[mo][:50])}")
    cov = []
    for y in range(2020, 2027):
        mo = f"{y}-06" if y < 2026 else "2026-06"
        m_ms = ms(f"{mo}-01T00:00")
        n200 = 0
        for s in uni[mo]:
            x = syms.get(s)
            if x is None:
                continue
            i = int(np.searchsorted(x.t, m_ms)) - 1
            if 0 <= i < x.t.size and math.isfinite(x.s200[i]):
                n200 += 1
        cov.append(f"{mo}: {n200}/{len(uni[mo])}")
    emit(f"- членів всесвіту з визначеною SMA200 на початок місяця: {'; '.join(cov)}")
    emit(f"- пікова RSS {rss_mb():.0f} MB")


def make_ctx(syms: dict[str, Sym], with_fwd: bool) -> Ctx:
    b = syms["BTCUSDT"]
    btc_close = {int(d): float(c) for d, c in zip(b.day, b.c, strict=True)}
    btc_up = {int(d): bool(c > s2) for d, c, s2 in zip(b.day, b.c, b.s200, strict=True) if math.isfinite(s2)}
    pool = Pool.build({s: x for s, x in syms.items() if s != "BTCUSDT"})
    fwd: dict[tuple[str, int], Fwd] = {}
    if with_fwd:
        t0 = time.time()
        for s, x in syms.items():
            for h in (30, 60, 90):
                fwd[(s, h)] = fwd_table(x, h, btc_close)
        emit(f"  [fwd] forward exits for {len(syms)} symbols in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    else:
        for s, x in syms.items():
            for h in (30, 60, 90):
                j, de = forward_exit(x.t, x.seg, h)
                fwd[(s, h)] = Fwd(j, de, np.full(x.t.size, np.nan), np.full(x.t.size, np.nan))
    return Ctx(syms, pool, fwd, btc_up, np.random.default_rng(SEED))


# ---------------------------------------------------------------- H3


def h3_data(syms: dict[str, Sym], uni: dict[str, list[str]]) -> dict[str, Any]:
    b = syms["BTCUSDT"]
    st = sideways_after_drop(b.c, b.seg)
    on = state_onsets(st)
    jb, deb = forward_exit(b.t, b.seg, 30)
    alt_ratios: dict[int, list[float]] = defaultdict(list)
    for s, x in syms.items():
        if s == "BTCUSDT":
            continue
        j, de = forward_exit(x.t, x.seg, 30)
        for i in np.flatnonzero(x.member & (j >= 0) & ~de):
            alt_ratios[int(x.day[i])].append(float(x.c[j[i]] / x.c[i]))
    days, y, state, is_ev = [], [], [], []
    ev_days = {int(b.day[t]) for t in on}
    for i in range(b.t.size):
        cm = int(b.close_ms[i])
        if not (START_MS <= cm <= DATA_END_MS - 30 * DAY_MS) or jb[i] < 0 or deb[i]:
            continue
        d = int(b.day[i])
        yy = xs_median_rel(alt_ratios.get(d, []), float(b.c[jb[i]] / b.c[i]))
        if not math.isfinite(yy):
            continue
        days.append(d)
        y.append(yy)
        state.append(bool(st[i]))
        is_ev.append(d in ev_days)
    return {
        "days": np.array(days),
        "y": np.array(y),
        "state": np.array(state),
        "ev": np.array(is_ev),
        "onsets": [int(b.day[t]) for t in on],
    }


def h3_report(h3: dict[str, Any]) -> None:
    emit("\n## H3 (описова): альти відносно BTC, поки BTC у боковику після падіння")
    d, y, stt, ev = h3["days"], h3["y"], h3["state"], h3["ev"]
    emit(f"- початки епізодів стану BTC (усі, з даними): {[day_str(x) for x in h3['onsets']]}")
    emit(f"- днів з y: {d.size}; днів у стані: {int(stt.sum())}; подій з y: {int(ev.sum())}")
    for lab, m in (
        ("IS", d * DAY_MS + DAY_MS < OOS_MS),
        ("OOS", d * DAY_MS + DAY_MS >= OOS_MS),
        ("весь період", np.ones(d.size, dtype=bool)),
    ):
        yy, ee, ss = y[m], ev[m], stt[m]
        if yy.size == 0:
            continue
        pos = np.flatnonzero(ee)
        base = float(yy.mean())
        if pos.size:
            evm = float(yy[pos].mean())
            bc = cluster_bootstrap_mean(yy[pos], np.arange(pos.size))
            null = shift_null(yy, pos, 60) - base
            delta = evm - base
            ci = (delta - float(np.quantile(null, 0.975)), delta - float(np.quantile(null, 0.025))) if null.size else None
            p_lo = float((1 + np.sum(null <= delta)) / (1 + null.size)) if null.size else None
        else:
            evm, bc, delta, ci, p_lo = float("nan"), {"ci": None}, float("nan"), None, None
        sm = float(yy[ss].mean()) if ss.any() else float("nan")
        emit(
            f"- {lab}: подій {pos.size}; E[y | подія] {evm:+.1%} CI {fmt_ci(bc['ci'])}; медіана {float(np.median(yy[pos])) if pos.size else float('nan'):+.1%}; "
            f"E[y] усі доби {base:+.1%}; Δ3 {delta:+.1%} CI (зсув) {fmt_ci(ci)}, p (Δ3 < 0) {fp(p_lo)}; E[y | усі доби стану] {sm:+.1%} ({int(ss.sum())} діб)"
        )
        if lab == "IS" and pos.size:
            emit(f"  - події IS: {[(day_str(int(d[k])), round(float(yy[k]), 3)) for k in pos]}")
    emit("- позначка за реєстрацією: див. звіт (◯, якщо подій < 10)")


# ---------------------------------------------------------------- stages


def stage_count(s14_cache: Path, data_dir: Path, own: Path, limit: int | None) -> None:
    emit("# Крок 9 S1, стадія 0: кількість подій (без дохідностей)")
    syms, uni = load_all(s14_cache, data_dir, own, limit)
    sanity(syms, uni, data_dir)
    ctx = make_ctx(syms, False)
    rows, why = collect(ctx, False)
    emit("\n| варіант | h | подій IS | дат IS | міс. IS | подій OOS | дат OOS | символів |\n|---|---|---|---|---|---|---|---|")
    for var, h in [*H1_VARIANTS, ("reclaim", 30)]:
        r = sel(rows, var=var, h=h)
        i_, o_ = split(r)
        emit(
            f"| {VAR_LABEL.get(var, 'H2 reclaim')} | {h} | {len(i_)} | {len({x['day'] for x in i_})} | {len({x['month'] for x in i_})} | {len(o_)} | {len({x['day'] for x in o_})} | {len({x['sym'] for x in r})} |"
        )
    emit(f"\n- відсів: {dict(sorted(why.items()))}")
    prim = sel(rows, var=PRIMARY[0], h=PRIMARY[1])
    pi, _ = split(prim)
    emit(f"- основна H1: дат IS {len({x['day'] for x in pi})} (поріг {MIN_DATES}); місяців IS {len({x['month'] for x in pi})}")
    emit(f"- подій за роками (основна): {dict(sorted(Counter(x['day'][:4] for x in prim).items()))}")
    emit(
        f"- символів у основній: {len({x['sym'] for x in prim})}; подій на символ (макс): {max(Counter(x['sym'] for x in prim).values()) if prim else 0}"
    )
    b = syms["BTCUSDT"]
    st = sideways_after_drop(b.c, b.seg)
    on = [day_str(int(b.day[t])) for t in state_onsets(st) if START_MS <= int(b.close_ms[t]) <= DATA_END_MS - 30 * DAY_MS]
    emit(f"- H3: початків епізодів стану BTC у періоді: {len(on)} — {on}; діб у стані {int(st.sum())}")


def stage_report(s14_cache: Path, data_dir: Path, own: Path, limit: int | None) -> None:
    emit("# Крок 9 S1: спот-альти біля тижневої підтримки — результати")
    emit(
        f"Дані: денні свічки перпів Binance USD-M (замінник споту), з делістнутими; всесвіт — топ-{TOP_N} альтів за обсягом щомісяця; входи {day_str(START_MS // DAY_MS)} … кінець даних − h; IS < {day_str(OOS_MS // DAY_MS)} ≤ OOS. Дохідність — на номінал без плеча (= на капітал), net після 0.07 % на сторону.\n"
    )
    syms, uni = load_all(s14_cache, data_dir, own, limit)
    sanity(syms, uni, data_dir)
    ctx = make_ctx(syms, True)
    t0 = time.time()
    rows, why = collect(ctx, True)
    emit(f"  [collect] {len(rows)} rows in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    s14.wr(own / ("rows.json" if not limit else "rows_sample.json"), rows)
    emit(f"\n- відсів і лічильники: {dict(sorted(why.items()))}")

    var, h = PRIMARY
    ev = sel(rows, var=var, h=h, kind="ev")
    pr = sel(rows, var=var, h=h, kind="preg")
    pt = sel(rows, var=var, h=h, kind="ptouch")
    ev_is, ev_oos = split(ev)
    pr_is, pr_oos = split(pr)
    pt_is, pt_oos = split(pt)

    emit("\n## H1 (основна): перший тест SMA100 після сили ≥ 1.6, аптренд, 60 діб")
    emit(HDR)
    emit(line("події IS (основна)", ev_is, n_boot=N_BOOT))
    emit(line("події OOS", ev_oos, n_boot=N_BOOT))
    emit(line("події, весь період", ev, n_boot=N_BOOT))
    emit(line("P_reg IS", pr_is))
    emit(line("P_reg OOS", pr_oos))
    emit(line("P_touch IS", pt_is))
    emit(line("P_touch OOS", pt_oos))

    s_is = summ(ev_is, n_boot=N_BOOT)
    s_is_m = summ(ev_is, cl="month", n_boot=N_BOOT)
    s_oos = summ(ev_oos, n_boot=N_BOOT)
    d_day = diff(ev_is, pr_is, "day", n_boot=N_BOOT)
    d_mon = diff(ev_is, pr_is, "month", n_boot=N_BOOT)
    d_oos = diff(ev_oos, pr_oos, "day", n_boot=N_BOOT)
    emit("\n| порівняння | Δ E[net] | 95 % CI (дати) | p (>0) | 95 % CI (місяці) | p (місяці) |\n|---|---|---|---|---|---|")
    for lab, a, b_ in (
        ("подія − P_reg, IS (основне p)", ev_is, pr_is),
        ("подія − P_reg, OOS", ev_oos, pr_oos),
        ("подія − P_reg, весь період", ev, pr),
        ("подія − P_touch, IS", ev_is, pt_is),
        ("подія − P_touch, OOS", ev_oos, pt_oos),
    ):
        x1, x2 = diff(a, b_, "day", n_boot=N_BOOT), diff(a, b_, "month", n_boot=N_BOOT)
        emit(f"| {lab} | {x1['diff']:+.1%} | {fmt_ci(x1['ci'])} | {fp(x1['p'])} | {fmt_ci(x2['ci'])} | {fp(x2['p'])} |")
    emit(f"\n- E[net] подій IS, кластери за місяцем: CI {fmt_ci(s_is_m['ci'])} ({s_is_m.get('months', 0)} місяців)")
    rel_is = [{**r, "net": r["rel"]} for r in ev_is]
    rel_pr = [{**r, "net": r["rel"]} for r in pr_is]
    emit(
        f"- відносно BTC, IS: події {summ(rel_is)['mean']:+.1%}, P_reg {summ(rel_pr)['mean']:+.1%}; Δ {diff(rel_is, rel_pr)['diff']:+.1%} {fmt_ci(diff(rel_is, rel_pr)['ci'])}"
    )

    n_dates = s_is.get("days", 0)
    mde = 2.8 * d_mon["se"] if math.isfinite(d_mon["se"]) else float("inf")
    mde_day = 2.8 * d_day["se"] if math.isfinite(d_day["se"]) else float("inf")
    p_raw = d_day["p"]
    p_adj = holm([*STEP_P, p_raw])[-1] if p_raw is not None else None
    verdict = h1_verdict(
        n_dates=n_dates,
        mde=mde,
        diff=d_day["diff"],
        diff_ci_day=d_day["ci"],
        diff_ci_month=d_mon["ci"],
        mean_ci_day=s_is["ci"],
        mean_ci_month=s_is_m["ci"],
        p_holm=p_adj,
        oos_dates=s_oos.get("days", 0),
        oos_mean=s_oos.get("mean", float("nan")),
        oos_diff=d_oos["diff"],
    )
    emit("\n## Ворота й вердикт H1 (за реєстрацією)")
    emit(
        f"- різних дат подій IS: {n_dates} (поріг {MIN_DATES}); місяців IS {s_is.get('months', 0)}; OOS дат {s_oos.get('days', 0)} (поріг для confirmed {MIN_OOS_DATES})"
    )
    emit(f"- MDE = 2.8·SE(Δ, кластери за місяцем) = {mde:.3f} (ворота {GATE}); для довідки за датами {mde_day:.3f}")
    emit(
        f"- основне p (Δ > 0, IS, дати): сире {fp(p_raw)}; Holm (m = 9) {fp(p_adj)}; ×11 для довідки {fp(min(1.0, 11 * p_raw) if p_raw is not None else None)}"
    )
    emit(f"- **вердикт за правилами реєстрації: {verdict}**")

    # robustness
    emit("\n## Стійкість H1 (основна)")
    emit(HDR)
    for k in (5, 10, 20):
        v, dd, drop = without_top_days([r["net"] for r in ev_is], [r["day"] for r in ev_is], k)
        drop_set = set(drop)
        a = [r for r in ev_is if r["day"] not in drop_set]
        b_ = [r for r in pr_is if r["day"] not in drop_set]
        dx = diff(a, b_)
        emit(line(f"IS без {k} найкращих дат (Δ {dx['diff']:+.1%} {fmt_ci(dx['ci'])})", a))
    for lab, side in (("спот-тейкер 0.12 % на сторону", SPOT_SIDE), ("витрати ×3", 3 * SIDE_COST)):
        a = [{**r, "net": r["gross"] - side * (2 + r["gross"])} for r in ev_is]
        b_ = [{**r, "net": r["gross"] - side * (2 + r["gross"])} for r in pr_is]
        emit(line(f"IS, {lab} (Δ {diff(a, b_)['diff']:+.1%})", a))
    a = [{**r, "net": r["net"] - FUND_DAY * r["hold"]} for r in ev_is]
    emit(line("IS, фандинг 0.01 %/8 год проти лонга (перпом)", a))
    emit(line("IS без data_end", [r for r in ev_is if not r["data_end"]]))
    for y in range(2020, 2027):
        a = [r for r in ev if r["day"].startswith(str(y))]
        b_ = [r for r in pr if r["day"].startswith(str(y))]
        emit(line(f"події {y} (Δ {diff(a, b_)['diff']:+.1%})", a))
    for lab, flag in (("BTC вище SMA200", True), ("BTC нижче SMA200", False)):
        a = [r for r in ev_is if r["btc_up"] is flag]
        b_ = [r for r in pr_is if r["ev"] in {x["ev"] for x in a}]
        dx = diff(a, b_)
        emit(line(f"IS, {lab} (Δ {dx['diff']:+.1%} {fmt_ci(dx['ci'])})", a))
    half = sorted({r["day"] for r in ev_is})
    if half:
        mid = half[len(half) // 2]
        for lab, cond in (("перша половина IS", lambda r: r["day"] < mid), ("друга половина IS", lambda r: r["day"] >= mid)):
            a = [r for r in ev_is if cond(r)]
            b_ = [r for r in pr_is if cond(r)]
            dx = diff(a, b_)
            emit(line(f"{lab} (Δ {dx['diff']:+.1%} {fmt_ci(dx['ci'])})", a))
    emit(f"\n- медіани IS: події {q([r['net'] for r in ev_is], 0.5):+.1%}, P_reg {q([r['net'] for r in pr_is], 0.5):+.1%}")
    b10 = [r["below200"] for r in ev_is if r["below200"] is not None]
    p10 = [r["below200"] for r in pr_is if r["below200"] is not None]
    emit(
        f"- інвалідація (закриття під SMA200 за 10 барів): події IS {sum(b10) / max(1, len(b10)):.1%} (n {len(b10)}), P_reg IS {sum(p10) / max(1, len(p10)):.1%}; на вході close < SMA200: {sum(1 for r in ev_is if r['dist200'] < 0) / max(1, len(ev_is)):.1%}"
    )
    if ev:
        nets = [r["net"] for r in ev]
        emit(
            f"- розподіл net подій (весь період): p5 {q(nets, 0.05):+.1%}, p25 {q(nets, 0.25):+.1%}, медіана {q(nets, 0.5):+.1%}, p75 {q(nets, 0.75):+.1%}, p95 {q(nets, 0.95):+.1%}, макс {max(nets):+.0%}"
        )
        top = sorted(ev, key=lambda r: -r["net"])[:8]
        emit(f"- 8 найкращих подій: {[(r['sym'], r['day'], round(r['net'], 2)) for r in top]}")
        emit(f"- 8 найгірших подій: {[(r['sym'], r['day'], round(r['net'], 2)) for r in sorted(ev, key=lambda r: r['net'])[:8]]}")
        emit(f"- виходи data_end: {sum(r['data_end'] for r in ev)} з {len(ev)}")
        emit(f"- подій за місяцями (IS, топ-8): {Counter(r['month'] for r in ev_is).most_common(8)}")

    # per-asset (owner's decision 2026-10-17)
    emit("\n## Перевірка по монетах (H1 основна; x = net події − середнє її P_reg)")
    ev_all = ev
    idx = {r["ev"]: k for k, r in enumerate(ev_all)}
    x = event_minus_placebo([r["net"] for r in ev_all], [r["net"] for r in pr], [idx[r["ev"]] for r in pr])
    xr = [(r, float(v)) for r, v in zip(ev_all, x, strict=True) if math.isfinite(v)]
    xi = [(r, v) for r, v in xr if not r["oos"]]
    stats_ = asset_stats([v for _, v in xi], [r["sym"] for r, _ in xi], min_n=3)
    emit("| символ | n IS | E[x] | CI (норм.) |\n|---|---|---|---|")
    for st_ in sorted(stats_, key=lambda s_: -s_.mean):
        emit(f"| {st_.asset} | {st_.n} | {st_.mean:+.1%} | {fmt_ci(st_.ci)} |")
    het = heterogeneity([v for _, v in xi], [r["sym"] for r, _ in xi], min_n=3)
    pq = permutation_q([v for _, v in xi], [r["sym"] for r, _ in xi], [r["month"] for r, _ in xi], min_n=3, n_perm=2000)
    emit(
        f"\n- неоднорідність (символи з ≥ 3 подій IS): k {het['k']}, n {het['n']}, Q {het['q']:.1f}, df {het['df']}, p {fp(het['p'])}, I² {het['i2']:.0%}; tau {het['tau']:.3f}; перестановка міток усередині місяців: p {fp(pq['p'])} (Q нуля: середнє {pq['null_mean']:.1f}, p95 {pq['null_p95']:.1f})"
    )
    per = persistence(
        [v for _, v in xr], [r["sym"] for r, _ in xr], [r["day"] for r, _ in xr], [not r["oos"] for r, _ in xr], min_n_is=3, top_frac=0.25
    )
    emit(
        f"- сталість: {per['k']} символів з ≥ 3 подій IS, топ-чверть {per['top']}; IS топ {per['top_is_mean']:+.1%} / решта {per['rest_is_mean']:+.1%}; OOS топ {per['top_oos_mean']:+.1%} ({per['n_top_oos']} подій) / решта {per['rest_oos_mean']:+.1%} ({per['n_rest_oos']}); різниця {per['diff']:+.1%} {fmt_ci(per['diff_ci'])}, p {fp(per['diff_p'])}; rho {per['rho']:.2f} (n {per['rho_n']})"
    )

    # secondary grid and variants
    emit("\n## Сітка й варіанти H1 (усі; сирі p і Holm усередині кроку — лише довідка)")
    emit(
        "| варіант | h | n IS | дат IS | E[net] IS | CI IS | P_reg IS | Δ IS [CI дати] | p Δ | Δ [CI місяці] | n OOS | E[net] OOS | Δ OOS |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    sec: list[tuple[str, float]] = []
    res: dict[tuple[str, int], dict[str, Any]] = {}
    for v, hh in H1_VARIANTS:
        e_i, e_o = split(sel(rows, var=v, h=hh, kind="ev"))
        p_i, p_o = split(sel(rows, var=v, h=hh, kind="preg"))
        a, d1, dm, d2 = summ(e_i), diff(e_i, p_i), diff(e_i, p_i, "month"), diff(e_o, p_o)
        res[(v, hh)] = d1
        pm = statistics.fmean(r["net"] for r in p_i) if p_i else float("nan")
        om = statistics.fmean(r["net"] for r in e_o) if e_o else float("nan")
        lab = f"{VAR_LABEL[v]}{' (основна)' if (v, hh) == PRIMARY else ''}"
        emit(
            f"| {lab} | {hh} | {a['n']} | {a['days']} | {a['mean']:+.1%} | {fmt_ci(a['ci'])} | {pm:+.1%} | {d1['diff']:+.1%} {fmt_ci(d1['ci'])} | {fp(d1['p'])} | {dm['diff']:+.1%} {fmt_ci(dm['ci'])} | {len(e_o)} | {om:+.1%} | {d2['diff']:+.1%} |"
        )
        if (v, hh) != PRIMARY and d1["p"] is not None:
            sec.append((f"{v}/{hh}", d1["p"]))
    if sec:
        adj = holm([p_ for _, p_ in sec])
        emit("\n| варіант | сире p (Δ > 0) | Holm усередині кроку |\n|---|---|---|")
        for (v, pr_), pa in zip(sec, adj, strict=True):
            emit(f"| {v} | {pr_:.4f} | {pa:.4f} |")
    if ("nofilter", 60) in res:
        emit(f"\n- чи додає фільтр сили: Δ основна {res[PRIMARY]['diff']:+.1%} проти без фільтра {res[('nofilter', 60)]['diff']:+.1%}")
    if ("down", 60) in res:
        emit(f"- гілка SMA100 ≤ SMA200: Δ {res[('down', 60)]['diff']:+.1%} (теза: нижче основної {res[PRIMARY]['diff']:+.1%})")

    # H2
    emit("\n## H2 (другорядна): reclaim втраченої потрійної підтримки, 30 діб, проти P_any")
    e2 = sel(rows, var="reclaim", h=30, kind="ev")
    p2 = sel(rows, var="reclaim", h=30, kind="pany")
    e2i, e2o = split(e2)
    p2i, p2o = split(p2)
    emit(HDR)
    emit(line("reclaim IS", e2i, n_boot=N_BOOT))
    emit(line("reclaim OOS", e2o, n_boot=N_BOOT))
    emit(line("P_any IS", p2i))
    emit(line("P_any OOS", p2o))
    a_d, a_m, a_o = diff(e2i, p2i, n_boot=N_BOOT), diff(e2i, p2i, "month", n_boot=N_BOOT), diff(e2o, p2o, n_boot=N_BOOT)
    mde2 = 2.8 * a_m["se"] if math.isfinite(a_m["se"]) else float("inf")
    n2 = len({r["day"] for r in e2i})
    if n2 < MIN_DATES or mde2 > GATE:
        mark2 = "◯ (ворота)"
    elif a_d["diff"] < H2_MIN_EFFECT or a_d["ci"] is None or a_d["ci"][0] <= 0:
        mark2 = "✗"
    elif a_m["ci"] is not None and a_m["ci"][0] > 0 and a_o["diff"] > 0:
        mark2 = "≈ (кандидат на нову картку)"
    else:
        mark2 = "◯ (позитив в IS без місяців чи OOS)"
    emit(
        f"\n- Δ30 IS {a_d['diff']:+.1%} CI дати {fmt_ci(a_d['ci'])} p {fp(a_d['p'])}; CI місяці {fmt_ci(a_m['ci'])}; MDE (місяці) {mde2:.3f}; дат IS {n2}; OOS Δ30 {a_o['diff']:+.1%} {fmt_ci(a_o['ci'])}"
    )
    emit(f"- **позначка H2 за реєстрацією: {mark2}**")
    if e2:
        rel2 = [{**r, "net": r["rel"]} for r in e2i]
        relp = [{**r, "net": r["rel"]} for r in p2i]
        emit(f"- відносно BTC, IS: reclaim {summ(rel2)['mean']:+.1%}, P_any {summ(relp)['mean']:+.1%}")

    # H3
    h3_report(h3_data(syms, uni))
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step9_s1")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "fetch", "count", "report"), required=True)
    ap.add_argument("--symbols", type=int, help="first N eligible symbols only (timing sample; BTC and ETH always)")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    own = a.cache_dir.expanduser()
    own.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("a" if a.stage == "prereg" else "w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "fetch":
        stage_fetch(a.s14_cache.expanduser(), a.data_dir.expanduser(), own, a.symbols)
    elif a.stage == "count":
        stage_count(a.s14_cache.expanduser(), a.data_dir.expanduser(), own, a.symbols)
    else:
        stage_report(a.s14_cache.expanduser(), a.data_dir.expanduser(), own, a.symbols)
    if a.stage != "prereg":
        emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
