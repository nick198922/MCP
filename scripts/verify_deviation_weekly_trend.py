"""IDEA-20261017-deviation-weekly-trend: a 4h sweep / deviation under a range whose wick reaches the daily EMA 100/200
band ("weekly trend"), then a buy limit at the range low. Groups T (touch), C (same events, no touch), P (a plain
test of the range low, no breach). Rules are fixed in the card's section "Попередня реєстрація". Stages:
  uv run python scripts/verify_deviation_weekly_trend.py --stage prereg
  uv run python scripts/verify_deviation_weekly_trend.py --stage live   --out ~/.cache/tradedesk-agents/deviation_weekly_trend/live.md
  uv run python scripts/verify_deviation_weekly_trend.py --stage count  --out ~/.cache/tradedesk-agents/deviation_weekly_trend/count.md
  uv run python scripts/verify_deviation_weekly_trend.py --stage report --out ~/.cache/tradedesk-agents/deviation_weekly_trend/report.md
  uv run python scripts/verify_deviation_weekly_trend.py --stage analyse --out ...
``live`` fetches ETHUSDT 1h daily archive files of October 2026 (rt.binance_vision, cached per day under --cache-dir)
and checks the author's example (and can write a test fixture with --fixture). ``count`` (stage 0) counts events per
group without any outcome and stores the T median stop in ATR (the placebo stop). ``report`` simulates and writes one
rows file per symbol to --cache-dir/rows/ (resumable), then analyses; ``analyse`` recomputes the statistics from them.
1h bars and the universe come from the step-1 cache (--s14-cache, filled from data.binance.vision via
rt.binance_vision); listings from the funding-crowding cache (--data-dir). No keys, no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (loaders, universe, period)
import verify_course_step3_s18 as s3  # noqa: E402  (membership intervals)
import verify_course_step7_basics as s7  # noqa: E402  (bootstrap summaries)

from tradedesk.analytics import markup  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars, atr, ema  # noqa: E402
from tradedesk.analytics.course_s18_trend import pos_from_seg, resample_partial, segments  # noqa: E402
from tradedesk.analytics.deviation_weekly_trend import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    Params,
    Setup,
    deviation_setups,
    last_daily_before,
    placebo_setups,
    trade_setups,
)
from tradedesk.analytics.liquidation_flush import without_top_days  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261017-deviation-weekly-trend.md"
MACRO_CSV = ROOT / "docs" / "verification" / "2026-10-15-us-macro-events.csv"
OUT: dict[str, Any] = {"fh": None}
CFG: dict[str, Any] = {"universe": "top30", "dir": Path("."), "fetch": False}
TOP_N_EXT = 50
emit = s7.emit
s7.OUT = OUT  # share the output file with the imported helpers
rss_mb = s7.rss_mb

START_MS = s1.ms("2020-08-01T00:00")
END_MS = s1.END_MS  # 2026-09-23
OOS_MS = s1.OOS_MS  # 2025-03-01
WARMUP = 100  # 4h bars inside a segment before a decision bar
# variants: the registered primary first; "card" = the card's literal detector (8 ATR, 1-2 closes)
VARIANTS = {
    "main": Params(w=60),
    "w30": Params(w=30),
    "w120": Params(w=120),
    "card": Params(w=60, width_atr=8.0, kmax=2),
}
VCODE = {k: i for i, k in enumerate(VARIANTS)}
GROUPS = {"T": 0, "C": 1, "P": 2, "deep": 3, "gap": 4, "band_high": 5, "na": 6}
GNAME = {v: k for k, v in GROUPS.items()}
SIM_GROUPS = ("T", "C", "P", "deep")
KIND = {"dev": 0, "sweep": 1, "placebo": 2}
EXIT = {"none": 0, "stop": 1, "stop_gap": 2, "target": 3, "time": 4, "data_end": 5}
TK = {"ema100_4h": 0, "mid": 1, "fixed": 2}
COLS = "sym side v grp kind s_ms r_ms day oos skipped has_path filled r_gross cost_r exit stop_pct stop_atr4 target_r tkind hold_h".split()
MIN_DATES_EXTEND, MIN_DATES = 250, 150
EFFECT = 0.10
FUND_8H = 0.0001
LIVE_DAYS = [f"2026-10-{d:02d}" for d in range(1, 17)]


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація")
    sec = t[a:]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- per-symbol context


class Ctx:
    """4h bars with their indicators and the daily context of one symbol, one side (long frame)."""

    def __init__(self, h1: Bars, side: int) -> None:
        seg = segments(h1.t)
        b4, s4 = resample_partial(h1, seg, 4, 4)
        d, sd = resample_partial(h1, seg, 24, 24)
        if side == -1:
            b4, d = b4.mirrored(), d.mirrored()
        self.path = h1 if side == 1 else h1.mirrored()
        self.b4, self.d = b4, d
        self.pos4 = pos_from_seg(s4)
        self.dpos = pos_from_seg(sd)
        self.atr4 = atr(b4, 14, self.pos4)
        self.e100_4 = ema(b4.c, 100, self.pos4)
        self.de100 = ema(d.c, 100, self.dpos)
        self.de200 = ema(d.c, 200, self.dpos)
        self.datr = atr(d, 14, self.dpos)
        self.d_e100 = last_daily_before(b4, d, self.de100)
        self.d_e200 = last_daily_before(b4, d, self.de200)
        self.d_atr = last_daily_before(b4, d, self.datr)
        self.close = b4.t + b4.bar_ms


def eligible(c: Ctx, st: Setup, iv: np.ndarray | None) -> bool:
    t = int(c.close[st.r])
    if not (START_MS <= t < END_MS) or c.pos4[st.r] < WARMUP:
        return False
    if not np.isfinite(c.d_e200[st.s]):
        return False
    return bool(s3.in_intervals(np.array([t]), iv)[0])


def symbol_setups(c: Ctx, iv: np.ndarray | None, p: Params, m_t: float | None) -> list[Setup]:
    out = [st for st in deviation_setups(c.b4, c.pos4, c.atr4, c.e100_4, c.d_e100, c.d_e200, c.d_atr, p) if eligible(c, st, iv)]
    if m_t is not None:
        out += [st for st in placebo_setups(c.b4, c.pos4, c.atr4, c.e100_4, m_t, p) if eligible(c, st, iv)]
    return out


def row_of(si: int, side: int, v: int, c: Ctx, st: Setup, tr: Any, skipped: bool) -> dict[str, float]:
    t = int(c.close[st.r])
    row = {
        "sym": si,
        "side": side,
        "v": v,
        "grp": GROUPS[st.group],
        "kind": KIND[st.kind],
        "s_ms": float(c.b4.t[st.s]),
        "r_ms": float(t),
        "day": t // DAY_MS,
        "oos": float(t >= OOS_MS),
        "skipped": float(skipped),
        "has_path": float(tr is not None),
        "filled": np.nan,
        "r_gross": np.nan,
        "cost_r": np.nan,
        "exit": np.nan,
        "stop_pct": st.risk / abs(st.lr),
        "stop_atr4": st.stop_atr4,
        "target_r": (st.target - st.lr) / st.risk,
        "tkind": TK[st.target_kind],
        "hold_h": np.nan,
    }
    if tr is not None:
        row["filled"] = float(tr.filled)
        row["exit"] = EXIT[tr.exit_kind]
        if tr.filled:
            row["r_gross"], row["cost_r"] = tr.r_gross, tr.cost_r
            row["hold_h"] = (tr.exit_ms - (tr.touch_ms + HOUR_MS)) / HOUR_MS
    return row


def run_symbol(si: int, h1: Bars, iv: np.ndarray | None, m_t: dict[str, float] | None, outcomes: bool) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for side in (1, -1):
        c = Ctx(h1, side)
        for name, p in VARIANTS.items():
            v = VCODE[name]
            mt = m_t.get(f"{name}:{side}") if m_t else None
            sts = symbol_setups(c, iv, p, mt)
            if not outcomes:
                rows += [row_of(si, side, v, c, st, None, False) for st in sts]
                continue
            for g in SIM_GROUPS:
                grp = [st for st in sts if st.group == g]
                for st, tr, sk in trade_setups(c.path, grp, c.close, p):
                    rows.append(row_of(si, side, v, c, st, tr, sk))
    return rows


def table(rows: list[dict[str, float]]) -> dict[str, np.ndarray]:
    return {k: np.asarray([r[k] for r in rows], dtype=np.float64) for k in COLS}


def universe(cache: Path) -> dict[str, list[str]]:
    """top30: the step-1 universe; top50: this card's extension (stage 0 power gate), built by --stage universe50."""
    if CFG["universe"] == "top30":
        return s1.members(cache)
    return s1.rd(CFG["dir"] / "universe_top50.json")


def build_top50(cache: Path, data_dir: Path, out_dir: Path) -> None:
    """Same ranking as step 1 (verify_course_s14_ema.stage_universe), 50 names a month instead of 30 (+ BTC, ETH)."""
    from tradedesk.analytics.liquidation_flush import base_of, monthly_universe

    syms = s1.eligible_syms(data_dir)
    qv = s1.daily_qv(cache, data_dir, syms)
    uni: dict[str, list[str]] = {}
    for mo in s1.months(s1.FIRST_UNI_MONTH, s1.LAST_MONTH):
        ranked = monthly_universe(qv, s1.ms(f"{mo}-01T00:00"), top_n=120)
        seen: set[str] = set()
        mem: list[str] = []
        for s in ranked:
            b = base_of(s)
            if b in seen:
                continue
            seen.add(b)
            mem.append(s)
            if len(mem) == TOP_N_EXT:
                break
        for s in s1.ALWAYS:
            if s not in mem:
                mem.append(s)
        uni[mo] = mem
    s1.wr(out_dir / "universe_top50.json", uni)
    old = s1.members(cache)
    same = all(set(old[mo]) <= set(uni[mo]) for mo in old)
    allm = sorted({s for v in uni.values() for s in v})
    emit(
        f"top-50: months {len(uni)}, distinct members {len(allm)} (top-30: {len({s for v in old.values() for s in v})}); top-30 ⊆ top-50 every month: {same}"
    )
    jobs = []
    for s in allm:
        for mo in s1.listing(data_dir, s):
            p = cache / "k1h" / s / f"{mo}.json"
            if not p.exists():
                jobs.append((s, "1h", mo, p))
    emit(f"1h files missing for the new members: {len(jobs)} (~{len(jobs) / 12 / 60:.0f} min at the step-1 rate)")
    if CFG["fetch"]:
        asyncio.run(s1.fetch_jobs(jobs, "k1h"))


def collect(cache: Path, data_dir: Path, out_dir: Path | None, outcomes: bool, limit: int | None, m_t: dict[str, float] | None):
    uni = universe(cache)
    syms = sorted({s for v in uni.values() for s in v})
    if limit:
        syms = syms[:limit]
    ivs = s3.member_intervals(uni)
    tables = []
    t0 = time.time()
    for n, s in enumerate(syms):
        f = out_dir / f"rows_{CFG['universe']}" / f"{s}.npz" if out_dir is not None and outcomes else None
        if f is not None and f.exists():
            with np.load(f) as z:
                tables.append({k: z[k] for k in z.files})
            continue
        h1 = s1.load_1h(cache, data_dir, s)
        rows = run_symbol(n, h1, ivs.get(s), m_t, outcomes) if len(h1) >= 500 else []
        tb = table(rows)
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(f, **tb)
        tables.append(tb)
        del h1
        if (n + 1) % 10 == 0 or n + 1 == len(syms):
            emit(
                f"  [run] {n + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rows {sum(x['sym'].size for x in tables)}, rss {rss_mb():.0f} MB"
            )
    R = {k: np.concatenate([x[k] for x in tables]) if tables else np.zeros(0) for k in COLS}
    return R, syms


# ---------------------------------------------------------------- sanity and the live example


def sanity(cache: Path, data_dir: Path) -> None:
    emit("## Санітарні перевірки")
    h1 = s1.load_1h(cache, data_dir, "BTCUSDT")
    c = Ctx(h1, 1)
    k = int(np.searchsorted(c.b4.t, s1.ms("2024-12-31T20:00")))
    emit(f"- BTCUSDT 4h-закриття 2024-12-31 20:00–24:00 = {c.b4.c[k]:g} (еталон серії: 93548.9)")
    j = len(c.d) - 1
    ref = markup.ema([float(x) for x in c.d.c], 200)[-1]
    emit(
        f"- BTCUSDT денна EMA200 на {s1.day_of(int(c.d.t[j]))}: course_s14_ema {c.de200[j]:.1f}, markup.ema {ref:.1f} (мають збігатись при одному сегменті; сегментів 1D {int(c.dpos.size - np.count_nonzero(c.dpos))})"
    )
    emit(f"- BTCUSDT медіана ATR14(1D)/ціна {np.nanmedian(c.datr / c.d.c):.2%}, ATR14(4h)/ціна {np.nanmedian(c.atr4 / c.b4.c):.2%}")
    first = int(np.flatnonzero(np.isfinite(c.d_e200))[0])
    emit(f"- BTCUSDT перший 4h-бар із денною EMA200: {s1.day_of(int(c.b4.t[first]))}")
    emit(f"- пікова RSS {rss_mb():.0f} MB")


async def fetch_live(cache_dir: Path, sym: str) -> None:
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    _, rt = build_server()
    try:
        for day in LIVE_DAYS:
            p = cache_dir / "live" / sym / f"{day}.json"
            if p.exists():
                continue
            try:
                f = await rt.binance_vision.um_klines_daily(sym, "1h", day)
                rows: Any = [[r[0], r[1], r[2], r[3], r[4], r[6]] for r in f.data]
            except ProviderError as e:
                emit(f"  {day}: {e.reason}")
                continue
            s1.wr(p, rows)
            emit(f"  {day}: {len(rows)} bars")
            await asyncio.sleep(0.2)
    finally:
        await rt.aclose()


def load_live(cache: Path, data_dir: Path, cache_dir: Path, sym: str) -> Bars:
    base = s1.load_1h(cache, data_dir, sym)
    rows: list[list[float]] = []
    for day in LIVE_DAYS:
        p = cache_dir / "live" / sym / f"{day}.json"
        if p.exists():
            rows.extend(s1.rd(p) or [])
    if not rows:
        return base
    a = np.array(rows, dtype=float)
    a = a[(a[:, 5] > 0) & (a[:, 0] % HOUR_MS == 0)]
    a = a[a[:, 0] > base.t[-1]]
    t = np.concatenate([base.t, a[:, 0].astype(np.int64)])
    return Bars(t, *(np.concatenate([getattr(base, k), a[:, i]]) for i, k in ((1, "o"), (2, "h"), (3, "low"), (4, "c"))), HOUR_MS)


def stage_live(cache: Path, data_dir: Path, cache_dir: Path, fixture: Path | None) -> None:
    emit("# Живий приклад автора: ETHUSDT 4h, 15–16.10.2026 (лоу 2318, денні EMA100/200 2357/2311, рейндж 2411–2666)")
    asyncio.run(fetch_live(cache_dir, "ETHUSDT"))
    h1 = load_live(cache, data_dir, cache_dir, "ETHUSDT")
    emit(f"- 1h-барів {len(h1)}, останній {datetime.fromtimestamp(h1.t[-1] / 1000, tz=UTC):%Y-%m-%d %H:%M} UTC")
    c = Ctx(h1, 1)
    lo = s1.ms("2026-10-10T00:00")
    for name, p in VARIANTS.items():
        for st in deviation_setups(c.b4, c.pos4, c.atr4, c.e100_4, c.d_e100, c.d_e200, c.d_atr, p):
            if c.b4.t[st.s] < lo:
                continue
            emit(
                f"- {name} (w={p.w}, ширина ≤ {p.width_atr:g} ATR, 1–{p.kmax} закриття): {st.kind} s={datetime.fromtimestamp(c.b4.t[st.s] / 1000, tz=UTC):%m-%d %H:%M} r={datetime.fromtimestamp(c.b4.t[st.r] / 1000, tz=UTC):%m-%d %H:%M}"
                f" lr {st.lr:.2f} tr {st.tr:.2f} mid {st.mid:.2f} low {st.ld:.2f}; денні EMA100 {st.e100:.1f} EMA200 {st.e200:.1f} ATR {st.atr_d:.1f}"
                f" → смуга [{min(st.e100, st.e200) - 0.5 * st.atr_d:.1f}; {max(st.e100, st.e200) + 0.25 * st.atr_d:.1f}]; **група {st.group}**;"
                f" стоп {st.stop:.2f} ({st.stop_atr4:.2f} ATR4h, {st.risk / st.lr:.2%}), ціль {st.target:.2f} ({st.target_kind}, {(st.target - st.lr) / st.risk:.2f}R)"
            )
    k = int(np.searchsorted(c.d.t, s1.ms("2026-10-14T00:00")))
    for j in range(k, min(k + 3, len(c.d))):
        emit(
            f"- 1D {s1.day_of(int(c.d.t[j]))}: o {c.d.o[j]:.2f} h {c.d.h[j]:.2f} l {c.d.low[j]:.2f} c {c.d.c[j]:.2f}; EMA100 {c.de100[j]:.1f} EMA200 {c.de200[j]:.1f} ATR14 {c.datr[j]:.1f}"
        )
    if fixture is not None:
        d0, h0 = s1.ms("2024-01-01T00:00"), s1.ms("2026-07-01T00:00")
        md, m4 = c.d.t >= d0, c.b4.t >= h0
        fx = {
            "_source": "data.binance.vision USD-M ETHUSDT 1h (monthly to 2026-09, daily 2026-10-01..16), aggregated to full UTC 1D and 4h buckets; fetched 2026-10-17 by scripts/verify_deviation_weekly_trend.py --stage live",
            "daily": [[int(c.d.t[i]), *(round(float(x[i]), 2) for x in (c.d.o, c.d.h, c.d.low, c.d.c))] for i in np.flatnonzero(md)],
            "h4": [[int(c.b4.t[i]), *(round(float(x[i]), 2) for x in (c.b4.o, c.b4.h, c.b4.low, c.b4.c))] for i in np.flatnonzero(m4)],
        }
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(fx, separators=(",", ":")) + "\n")
        emit(f"- фікстуру записано: {fixture} ({len(fx['daily'])} 1D, {len(fx['h4'])} 4h)")


# ---------------------------------------------------------------- stage 0


def stage_count(cache: Path, data_dir: Path, out_dir: Path, limit: int | None) -> None:
    emit("# IDEA-20261017-deviation-weekly-trend, стадія 0: частота подій (без результатів)")
    sanity(cache, data_dir)
    R, syms = collect(cache, data_dir, None, False, limit, None)
    emit(f"\nсимволів {len(syms)}; подій (без плацебо) {R['sym'].size}")
    emit(
        "\n| варіант | сторона | група | подій IS | дат IS | подій OOS | дат OOS | медіана стопу, ATR4h | медіана стопу, % |\n|---|---|---|---|---|---|---|---|---|"
    )
    m_t: dict[str, float] = {}
    for name in VARIANTS:
        for side in (1, -1):
            for g in ("T", "C", "deep", "gap", "band_high", "na"):
                m = (R["v"] == VCODE[name]) & (R["side"] == side) & (R["grp"] == GROUPS[g])
                i_, o_ = m & (R["oos"] == 0), m & (R["oos"] == 1)
                emit(
                    f"| {name} | {'лонг' if side == 1 else 'шорт'} | {g} | {int(i_.sum())} | {np.unique(R['day'][i_]).size} | {int(o_.sum())} | {np.unique(R['day'][o_]).size}"
                    f" | {np.median(R['stop_atr4'][i_]) if i_.any() else float('nan'):.2f} | {np.median(R['stop_pct'][i_]) * 100 if i_.any() else float('nan'):.2f} |"
                )
                if g == "T" and i_.any():
                    m_t[f"{name}:{side}"] = float(np.median(R["stop_atr4"][i_]))
    t60 = (R["v"] == VCODE["main"]) & (R["side"] == 1) & (R["grp"] == 0)
    dates = np.unique(R["day"][t60]).size
    emit(
        f"\n- ворота потужності: дат T (основний варіант, лонги, IS+OOS) = {dates}; поріг розширення {MIN_DATES_EXTEND}, поріг inconclusive {MIN_DATES}"
    )
    emit(f"- з них sweep / dev: {int((t60 & (R['kind'] == 1)).sum())} / {int((t60 & (R['kind'] == 0)).sum())}")
    if limit is None:
        p = out_dir / f"m_t_{CFG['universe']}.json"
        s1.wr(p, m_t)
        emit(f"- медіана стопу T в ATR4h (стоп плацебо, IS) записано в {p}: {m_t}")


# ---------------------------------------------------------------- report


def macro_days() -> dict[str, set[int]]:
    out: dict[str, set[int]] = {"cpi": set(), "fomc": set(), "nfp": set()}
    with MACRO_CSV.open() as fh:
        for r in csv.DictReader(fh):
            if r["status"] == "cancelled":
                continue
            out.setdefault(r["kind"], set()).add(s1.ms(r["date"] + "T00:00") // DAY_MS)
    return out


def touches_days(s_ms: np.ndarray, r_ms: np.ndarray, days: set[int]) -> np.ndarray:
    """True where a UTC date of any 4h bar s .. r (opens s_ms .. r_ms - 4h) is in ``days``."""
    out = np.zeros(s_ms.size, dtype=bool)
    for i in range(s_ms.size):
        d0, d1 = int(s_ms[i]) // DAY_MS, (int(r_ms[i]) - 4 * HOUR_MS) // DAY_MS
        out[i] = any(d in days for d in range(d0, d1 + 1))
    return out


def stage_report(cache: Path, data_dir: Path, out_dir: Path, limit: int | None) -> None:
    emit("# IDEA-20261017-deviation-weekly-trend — результати")
    p = out_dir / f"m_t_{CFG['universe']}.json"
    if not p.exists():
        raise SystemExit(f"run --stage count first ({p.name}: the placebo stop)")
    m_t = s1.rd(p)
    emit(
        f"Бари рішення {s1.day_of(START_MS)} … {s1.day_of(END_MS)}; IS < {s1.day_of(OOS_MS)} ≤ OOS. Стоп плацебо (медіана T, ATR4h): {m_t}\n"
    )
    sanity(cache, data_dir)
    R, syms = collect(cache, data_dir, out_dir, True, limit, m_t)
    analyse(R, syms)


def stage_analyse(cache: Path, data_dir: Path, out_dir: Path) -> None:
    emit("# IDEA-20261017-deviation-weekly-trend — статистика з файлів rows/ (без повторної симуляції)")
    R, syms = collect(cache, data_dir, out_dir, True, None, None)
    analyse(R, syms)


def ci(c: tuple[float, float] | None) -> str:
    return s7.ci_str(c)


def grp_stats(R: dict[str, np.ndarray], m: np.ndarray, net: np.ndarray, n_boot: int = 10000) -> dict[str, Any]:
    ev = m & (R["skipped"] == 0) & (R["has_path"] == 1)
    f = ev & (R["filled"] == 1) & np.isfinite(net)
    s = s7.summ(net[f], R["day"][f], n_boot)
    g = s7.summ(R["r_gross"][f], R["day"][f], n_boot)
    return {
        "ev": int(ev.sum()),
        "skipped": int((m & (R["skipped"] == 1)).sum()),
        "fill": float(f.sum() / ev.sum()) if ev.any() else float("nan"),
        "f": f,
        "net": s,
        "gross": g,
    }


def line(lab: str, st: dict[str, Any]) -> str:
    s, g = st["net"], st["gross"]
    mde = 2.8 * s["se"] if s.get("se") is not None else float("nan")
    return f"| {lab} | {st['ev']} | {st['fill']:.1%} | {s['n']} | {s['days']} | {g['mean']:+.3f} | **{s['mean']:+.3f}** {ci(s['ci'])} | {mde:.3f} |"


HDR = "| вибірка | подій (без пропусків) | частка ліміту | угод | дат | E[R] gross | E[R] net [95 % CI] | MDE |\n|---|---|---|---|---|---|---|---|"


def diff_line(lab: str, R, a: np.ndarray, b: np.ndarray, net: np.ndarray, n_boot: int = 10000) -> dict[str, Any]:
    d = s7.bdiff(net[a], R["day"][a], net[b], R["day"][b], n_boot)
    emit(
        f"| {lab} | {int(a.sum())} / {int(b.sum())} | **{d['diff']:+.3f}** {ci(d['ci'])} | {d['p'] if d['p'] is None else round(d['p'], 4)} |"
    )
    return d


def analyse(R: dict[str, np.ndarray], syms: list[str]) -> None:
    net = R["r_gross"] - R["cost_r"]
    always = [i for i, s in enumerate(syms) if s in s1.ALWAYS]
    btc_eth = np.isin(R["sym"], always)
    years = (R["day"].astype(np.int64) * DAY_MS).astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970
    sel = {g: R["grp"] == GROUPS[g] for g in SIM_GROUPS}

    def base(v: str = "main", side: int = 1, oos: float | None = 0.0) -> np.ndarray:
        m = (R["v"] == VCODE[v]) & (R["side"] == side)
        return m if oos is None else m & (R["oos"] == oos)

    # ---- primary
    emit("\n## Основний тест (w = 60, ширина ≤ 10 ATR, 1–3 закриття, лонги)")
    res: dict[str, dict[str, Any]] = {}
    for per, oos in (("IS", 0.0), ("OOS", 1.0)):
        emit(f"\n### {per}\n{HDR}")
        for g in ("T", "C", "P", "deep"):
            res[f"{per}:{g}"] = st = grp_stats(R, base(oos=oos) & sel[g], net)
            emit(line(f"{g} {per}", st))
        emit("\n| різниця E[R] net | угод a / b | Δ [95 % CI] | p (Δ ≤ 0) |\n|---|---|---|---|")
        res[f"{per}:TC"] = diff_line(f"T − C ({per})", R, res[f"{per}:T"]["f"], res[f"{per}:C"]["f"], net)
        res[f"{per}:TP"] = diff_line(f"T − P ({per})", R, res[f"{per}:T"]["f"], res[f"{per}:P"]["f"], net)

    # ---- verdict by the registered rule
    t_is, tc_is = res["IS:T"]["net"], res["IS:TC"]
    t_oos, tc_oos = res["OOS:T"]["net"], res["OOS:TC"]
    dates_is = t_is["days"]
    a_ok = t_is["mean"] >= EFFECT and t_is["ci"] is not None and t_is["ci"][0] > 0
    b_ok = tc_is["diff"] >= EFFECT and tc_is["ci"] is not None and tc_is["ci"][0] > 0
    kill = (t_is["ci"] is None or t_is["ci"][0] <= 0) or tc_is["diff"] < EFFECT
    if dates_is < MIN_DATES:
        vd = f"inconclusive (дат набраних угод T в IS {dates_is} < {MIN_DATES})"
    elif a_ok and b_ok:
        vd = "confirmed" if (t_oos["mean"] > 0 and tc_oos["diff"] > 0) else "inconclusive (H1 в IS виконано, знак в OOS інший)"
    elif kill:
        vd = "rejected (CI E[R] net T включає 0 або T − C < +0.10R)"
    else:
        vd = "inconclusive (H1 в IS не виконано повністю, але критерій «вбити» не спрацював)"
    emit(f"\n- H1 (a) E[R] net T ≥ +{EFFECT} і CI > 0: {a_ok}; (b) T − C ≥ +{EFFECT} і CI > 0: {b_ok}; дат T в IS {dates_is}")
    emit(f"- OOS: E[R] net T {t_oos['mean']:+.3f}, T − C {tc_oos['diff']:+.3f}")
    emit(f"- **вердикт за реєстрацією: {vd}**")

    # ---- details of the primary
    emit("\n## Деталі основного тесту (w = 60, лонги, IS+OOS окремо)")
    for per in ("IS", "OOS"):
        for g in ("T", "C", "P"):
            f = res[f"{per}:{g}"]["f"]
            if not f.any():
                continue
            x = net[f]
            ex = Counter(int(v) for v in R["exit"][f])
            tk = Counter(int(v) for v in R["tkind"][f])
            q = np.quantile(x, [0.05, 0.25, 0.5, 0.75, 0.95])
            emit(
                f"- {g} {per}: R net квантилі 5/25/50/75/95 % {', '.join(f'{v:+.2f}' for v in q)}; SD {x.std():.2f}; частка > 0 {np.mean(x > 0):.1%};"
                f" виходи {{{', '.join(f'{k}: {ex.get(v, 0)}' for k, v in EXIT.items() if v)}}}; цілі {{{', '.join(f'{k}: {tk.get(v, 0)}' for k, v in TK.items())}}};"
                f" стоп: медіана {np.median(R['stop_pct'][f]) * 100:.2f} % ціни, {np.median(R['stop_atr4'][f]):.2f} ATR4h; ціль у середньому {np.mean(R['target_r'][f]):.2f}R;"
                f" витрати в середньому {np.mean(R['cost_r'][f]):.3f}R; утримання медіана {np.nanmedian(R['hold_h'][f]):.0f} год; пропущено (одна угода водночас) {res[f'{per}:{g}']['skipped']}"
            )

    # ---- robustness (registered report rows)
    emit("\n## Стійкість і зрізи (звіт, без вердикту)")
    emit(f"\n### Зрізи основного тесту (w = 60, лонги)\n{HDR}")
    for g in ("T", "C", "P"):
        for lab, m in (
            ("BTC+ETH", btc_eth),
            ("альти", ~btc_eth),
            *[(f"рік {y}", years == y) for y in range(2020, 2027)],
            ("свип", R["kind"] == 1),
            ("девіація 1–3 закриття", R["kind"] == 0),
        ):
            st = grp_stats(R, base(oos=None) & sel[g] & m, net, 2000)
            if st["net"]["n"]:
                emit(line(f"{g} · {lab}", st))
    emit("\n| різниця E[R] net | угод a / b | Δ [95 % CI] | p |\n|---|---|---|---|")
    for lab, m in (("BTC+ETH", btc_eth), ("альти", ~btc_eth)):
        for per, oos in (("IS", 0.0), ("OOS", 1.0)):
            a = grp_stats(R, base(oos=oos) & sel["T"] & m, net, 10)["f"]
            b = grp_stats(R, base(oos=oos) & sel["C"] & m, net, 10)["f"]
            diff_line(f"T − C · {lab} · {per}", R, a, b, net, 2000)

    emit("\n### Без найкращих дат, стрес витрат і funding (w = 60, лонги, IS)")
    emit("| вибірка | угод | дат | E[R] net [CI] |\n|---|---|---|---|")
    for g in ("T", "C"):
        f = res[f"IS:{g}"]["f"]
        for k in (5, 10):
            v, dd, _ = without_top_days(net[f].tolist(), R["day"][f].tolist(), k)
            s = s7.summ(np.asarray(v), np.asarray(dd), 2000)
            emit(f"| {g} без {k} найкращих дат | {s['n']} | {s['days']} | {s['mean']:+.3f} {ci(s['ci'])} |")
        s = s7.summ(net[f] - R["cost_r"][f], R["day"][f], 2000)
        emit(f"| {g}, витрати ×2 (0.28 % за коло) | {s['n']} | {s['days']} | {s['mean']:+.3f} {ci(s['ci'])} |")
        fund = FUND_8H * np.nan_to_num(R["hold_h"][f]) / 8 / R["stop_pct"][f]
        s = s7.summ(net[f] - fund, R["day"][f], 2000)
        emit(f"| {g}, funding 0.01 %/8 год проти позиції | {s['n']} | {s['days']} | {s['mean']:+.3f} {ci(s['ci'])} |")
    for g in ("T", "C"):
        f = res[f"IS:{g}"]["f"]
        s = s7.summ(net[f], s7.week_of(R["day"][f]), 2000)
        emit(f"| {g}, кластери за ISO-тижнем | {s['n']} | {s['days']} тиж. | {s['mean']:+.3f} {ci(s['ci'])} |")

    emit("\n### Варіанти детектора і шорти (звіт)")
    for name, p in VARIANTS.items():
        for side in (1, -1):
            if name == "main" and side == 1:
                continue
            emit(
                f"\n#### {name}: w = {p.w}, ширина ≤ {p.width_atr:g} ATR, 1–{p.kmax} закриття; {'лонги' if side == 1 else 'шорти (дзеркально)'}\n{HDR}"
            )
            sts = {}
            for per, oos in (("IS", 0.0), ("OOS", 1.0)):
                for g in ("T", "C", "P"):
                    sts[f"{per}:{g}"] = st = grp_stats(R, base(name, side, oos) & sel[g], net, 2000)
                    emit(line(f"{g} {per}", st))
            emit("\n| різниця | угод a / b | Δ [CI] | p |\n|---|---|---|---|")
            for per in ("IS", "OOS"):
                diff_line(f"T − C ({per})", R, sts[f"{per}:T"]["f"], sts[f"{per}:C"]["f"], net, 2000)
                diff_line(f"T − P ({per})", R, sts[f"{per}:T"]["f"], sts[f"{per}:P"]["f"], net, 2000)

    # ---- H2
    emit("\n## H2 (лише звіт): новинні дні CPI / FOMC (PPI в таблиці немає), w = 60, лонги, IS+OOS")
    md = macro_days()
    cf = touches_days(R["s_ms"], R["r_ms"], md["cpi"] | md["fomc"])
    nf = touches_days(R["s_ms"], R["r_ms"], md["nfp"])
    emit(HDR)
    for g in ("T", "C"):
        for lab, m in (("CPI/FOMC", cf), ("NFP", nf), ("решта (без CPI/FOMC/NFP)", ~cf & ~nf)):
            st = grp_stats(R, base(oos=None) & sel[g] & m, net, 2000)
            emit(line(f"{g} · {lab}", st))
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/deviation_weekly_trend")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "live", "universe50", "count", "report", "analyse"), required=True)
    ap.add_argument("--universe", choices=("top30", "top50"), default="top30", help="top50: the stage-0 power-gate extension")
    ap.add_argument("--fetch", action="store_true", help="universe50: also fetch the missing 1h files into --s14-cache/k1h")
    ap.add_argument("--symbols", type=int, help="run only the first N symbols (timing sample)")
    ap.add_argument("--fixture", type=Path, help="live: also write the ETH test fixture here")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    out_dir = a.cache_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    cache, data_dir = a.s14_cache.expanduser(), a.data_dir.expanduser()
    CFG.update(universe=a.universe, dir=out_dir, fetch=a.fetch)
    emit(f"(всесвіт {a.universe})")
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "universe50":
        build_top50(cache, data_dir, out_dir)
    elif a.stage == "live":
        stage_live(cache, data_dir, out_dir, a.fixture)
    elif a.stage == "count":
        stage_count(cache, data_dir, out_dir, a.symbols)
    elif a.stage == "analyse":
        stage_analyse(cache, data_dir, out_dir)
    else:
        stage_report(cache, data_dir, out_dir, a.symbols)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
