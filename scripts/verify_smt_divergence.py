"""Course setups, step 10 (IDEA-20261016-course-setups-crypto): SMT divergence as a sweep filter (H1 of
LIB-20261016-ict-concepts). Within sweeps of a swing low (high), is a sweep that the correlated asset does NOT confirm
(it holds a higher low) followed by a larger reversal than a sweep that both assets make?

Primary: BTCUSDT / ETHUSDT perps, 1h and 4h (co-primary), both directions of the pair, both sides, y12 = move
toward the reversal over 12 bars in ATR of the swept asset; Delta = E[y12 | SMT] - E[y12 | confirmed], day-clustered
bootstrap. Secondary: EURUSD / GBPUSD (Dukascopy 5m bid, already cached) on 1h and 15m, a variant grid, placebos.
Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 10". Stages:
  uv run python scripts/verify_smt_divergence.py --stage prereg --out ~/.cache/tradedesk-agents/smt_divergence/registration.txt
  uv run python scripts/verify_smt_divergence.py --stage count  --out ~/.cache/tradedesk-agents/smt_divergence/count.md
  uv run python scripts/verify_smt_divergence.py --stage report --out ~/.cache/tradedesk-agents/smt_divergence/report.md
  uv run python scripts/verify_smt_divergence.py --stage explore --out docs/verification/2026-10-20-smt-divergence.explore.raw.md
``explore`` is post-hoc (B window +-0 / +-1), labelled exploratory and kept out of the verdict.
Data (no new downloads): 1h perp klines of the step-1 cache (--s14-cache/k1h, filled from data.binance.vision through
rt.binance_vision), the funding-crowding listing (--data-dir), forex 5m CSVs (--fx-dir, written by fx_history.py).
``count`` (stage 0) counts events and runs the sanity checks without any outcome. No keys, no orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s14  # noqa: E402  (1h loader, month helpers)

from tradedesk.analytics.course_s1_spot import pick_matched  # noqa: E402
from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars, atr, resample, segment_pos  # noqa: E402
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means, holm  # noqa: E402
from tradedesk.analytics.markup import fractals  # noqa: E402
from tradedesk.analytics.smt_divergence import (  # noqa: E402
    cochran_q,
    depth_stratified_diff,
    forward_move,
    fractal_lows,
    joint_cluster_diff,
    joint_grid,
    resample_min,
    smt_verdict,
    sweep_events,
)

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
ms = s14.ms
CRYPTO_START = ms("2020-02-01T00:00")
FX_START = ms("2014-01-01T00:00")
END_MS = ms("2026-09-23T00:00")
OOS_MS = ms("2025-03-01T00:00")
SEED, N_BOOT, N_BOOT_SEC = 20261017, 10_000, 2_000
STEP_P = (0.985, 0.280, 0.448, 0.038, 0.822, 1.0, 0.394, 0.563, 0.278)  # raw primary p of steps 1-9 (Holm family)
MIN_DATES, GATE, MIN_EFFECT = 200, 0.20, 0.20
WARMUP = 100
HORIZONS = (4, 12, 24)
CRYPTO_RT = 0.0014  # 0.07 % per side
FX_RT_PIPS = {"EURUSD": 1.3, "GBPUSD": 1.6}
SHIFT_MS = 7 * DAY_MS
PRAND_K, PRAND_DAYS = 3, 30
M5 = 5 * 60_000
VARIANTS: dict[str, dict[str, Any]] = {
    "main": {},
    "any": {"mode": "any"},
    "after2": {"after": 2},
    "n2": {"n": 2},
    "shift7": {},
}
VAR_LABEL = {
    "main": "основне (свип, 3-свічковий фрактал, рішення на t)",
    "any": "будь-яке зняття (свип + пробій)",
    "after2": "вікно B до t+2, рішення на t+2",
    "n2": "5-свічковий фрактал",
    "shift7": "P_shift: B зсунуто на 7 діб",
}
# Exploratory (chosen after the registered run, not part of the report stage or its Holm family): narrower B window.
EXPLORE_VARIANTS: dict[str, dict[str, Any]] = {"lag0": {"lag": 0}, "lag1": {"lag": 1}, "main": {}}
EXPLORE_LABEL = {"lag0": "±0 (лише бар свінгу A)", "lag1": "±1", "main": "±2 (зареєстроване)"}


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def utc(t_ms: int) -> datetime:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC)


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 10")
    b = t.index("\n## ", a + 1)
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- data


@dataclass
class Series:
    fam: str  # crypto | fx
    tf: str
    a: str
    b: str
    A: Bars
    B: Bars
    max_gap: int
    start: int


def load_fx(fx_dir: Path, pair: str) -> Bars:
    """5m bid bars from fx_history.py's CSV; zero-volume (flat weekend) bars count as missing."""
    a = np.loadtxt(fx_dir / f"{pair}_5m.csv", delimiter=",", skiprows=1, dtype=float, ndmin=2)
    a = a[np.argsort(a[:, 0], kind="stable")]
    a = a[np.concatenate([[True], a[1:, 0] != a[:-1, 0]])]
    a = a[a[:, 5] > 0]
    return Bars.from_rows(a[:, :5], M5)


def load_series(s14_cache: Path, data_dir: Path, fx_dir: Path, with_fx: bool) -> list[Series]:
    t0 = time.time()
    btc = s14.load_1h(s14_cache, data_dir, "BTCUSDT")
    eth = s14.load_1h(s14_cache, data_dir, "ETHUSDT")
    out: list[Series] = []
    for tf, (x, y) in (("1h", (btc, eth)), ("4h", (resample(btc, 4), resample(eth, 4)))):
        out.append(Series("crypto", tf, "BTC", "ETH", x, y, 48 * HOUR_MS, CRYPTO_START))
        out.append(Series("crypto", tf, "ETH", "BTC", y, x, 48 * HOUR_MS, CRYPTO_START))
    emit(f"  [load] crypto 1h: BTC {len(btc)} bars, ETH {len(eth)} bars in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    if with_fx and (fx_dir / "EURUSD_5m.csv").exists() and (fx_dir / "GBPUSD_5m.csv").exists():
        eu, gb = load_fx(fx_dir, "EURUSD"), load_fx(fx_dir, "GBPUSD")
        for tf, step, mn in (("1h", HOUR_MS, 10), ("15m", 15 * 60_000, 2)):
            x, y = resample_min(eu, step, mn), resample_min(gb, step, mn)
            out.append(Series("fx", tf, "EURUSD", "GBPUSD", x, y, 72 * HOUR_MS, FX_START))
            out.append(Series("fx", tf, "GBPUSD", "EURUSD", y, x, 72 * HOUR_MS, FX_START))
        emit(f"  [load] fx 5m: EURUSD {len(eu)}, GBPUSD {len(gb)} non-flat bars in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    return out


# ---------------------------------------------------------------- events


def iso_week(d: datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def rt_cost(ser: Series, price: float) -> float:
    return CRYPTO_RT if ser.fam == "crypto" else FX_RT_PIPS[ser.a] * 1e-4 / price


def collect(ser: Series, var: str, outcomes: bool, rng: np.random.Generator | None, why: Counter[str]) -> list[dict[str, Any]]:
    kw = VARIANTS[var] if var in VARIANTS else EXPLORE_VARIANTS[var]
    A, B = joint_grid(ser.A, ser.B, SHIFT_MS if var == "shift7" else 0)
    pos = segment_pos(A.t, ser.max_gap)
    at = atr(A, 14, pos)  # true range is symmetric: the same ATR for the mirrored frame
    close_ms = A.t + A.bar_ms
    in_per = (close_ms >= ser.start) & (close_ms < END_MS)
    rows: list[dict[str, Any]] = []
    key = f"{ser.fam}/{ser.tf}/{var}"
    for side in ("bull", "bear"):
        a_, b_ = (A, B) if side == "bull" else (A.mirrored(), B.mirrored())
        evs = sweep_events(a_, b_, pos, at, warmup=WARMUP, **kw)
        why[f"{key}:raw"] += len(evs)
        ev_bars: list[int] = []
        for e in evs:
            cm = int(close_ms[e.t])
            if not in_per[e.t]:
                why[f"{key}:out_of_period"] += 1
                continue
            d = utc(cm)
            row: dict[str, Any] = {
                "fam": ser.fam,
                "tf": ser.tf,
                "var": var,
                "a": ser.a,
                "b": ser.b,
                "side": side,
                "kind": "ev",
                "t_ms": cm,
                "day": d.strftime("%Y-%m-%d"),
                "week": iso_week(d),
                "oos": cm >= OOS_MS,
                "smt": e.smt,
                "back": e.close_back,
                "depth": e.depth_atr,
            }
            if outcomes:
                px = abs(float(a_.c[e.t]))
                y12 = forward_move(a_.c, pos, e.t, 12)
                if not math.isfinite(y12):
                    why[f"{key}:horizon_cut"] += 1
                    continue
                for k in HORIZONS:
                    mv = forward_move(a_.c, pos, e.t, k)
                    row[f"y{k}"] = mv / float(at[e.t])
                    row[f"r{k}"] = mv / px
                    row[f"n{k}"] = mv / px - rt_cost(ser, px)
                rb = forward_move(b_.c, pos, e.t, 12)
                row["rel12"] = row["r12"] - rb / abs(float(b_.c[e.t])) if math.isfinite(rb) else float("nan")
                row["cost"] = rt_cost(ser, px)
            else:
                j = e.t + 12
                if j >= pos.size or pos[j] != pos[e.t] + 12:
                    why[f"{key}:horizon_cut"] += 1
                    continue
            row["ev"] = f"{key}/{ser.a}/{side}/{e.t}"
            rows.append(row)
            ev_bars.append(e.t)
        if outcomes and var == "main" and rng is not None:
            smt_rows = [r for r in rows if r["kind"] == "ev" and r["side"] == side and r["smt"]]
            rows.extend(prand_rows(ser, a_, pos, at, close_ms, in_per, side, smt_rows, rng))
    return rows


def prand_rows(
    ser: Series,
    a_: Bars,
    pos: np.ndarray,
    at: np.ndarray,
    close_ms: np.ndarray,
    in_per: np.ndarray,
    side: str,
    smt_rows: list[dict[str, Any]],
    rng: np.random.Generator,
) -> list[dict[str, Any]]:
    """P_rand: up to 3 random bars of the same asset, frame, pair and timeframe within +-30 days of each SMT event."""
    n = pos.size
    j = np.arange(n)
    j12 = np.minimum(j + 12, n - 1)
    ok = in_per & (pos >= WARMUP) & np.isfinite(at) & (j + 12 < n) & (pos[j12] == pos + 12)
    cand = np.flatnonzero(ok)
    cand_day = close_ms[cand] // DAY_MS
    out: list[dict[str, Any]] = []
    for r in smt_rows:
        t = int(r["ev"].rsplit("/", 1)[1])
        ex = int(np.searchsorted(cand, t))
        ex_i = ex if ex < cand.size and cand[ex] == t else None
        for k in pick_matched(
            rng, cand_day, np.ones(cand.size, dtype=bool), int(close_ms[t] // DAY_MS), window=PRAND_DAYS, k=PRAND_K, exclude=ex_i
        ):
            b = int(cand[k])
            mv = float(a_.c[b + 12] - a_.c[b])
            px = abs(float(a_.c[b]))
            out.append(
                {
                    **{x: r[x] for x in ("fam", "tf", "var", "a", "b", "side", "day", "week", "oos", "ev")},
                    "kind": "prand",
                    "y12": mv / float(at[b]),
                    "r12": mv / px,
                    "n12": mv / px - rt_cost(ser, px),
                }
            )
    return out


# ---------------------------------------------------------------- stats helpers


def fmt_ci(ci: tuple[float, float] | None, f: str = "+.3f") -> str:
    return "—" if ci is None else f"[{ci[0]:{f}}; {ci[1]:{f}}]"


def fp(p: float | None) -> str:
    return "—" if p is None else f"{p:.4f}"


def sel(rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return [r for r in rows if all(r.get(k) == v for k, v in kw.items())]


def fin(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [r for r in rows if math.isfinite(r.get(key, float("nan")))]


def dsum(s: list[dict[str, Any]], c: list[dict[str, Any]], key: str = "y12", cl: str = "day", n_boot: int = N_BOOT) -> dict[str, Any]:
    s, c = fin(s, key), fin(c, key)
    return joint_cluster_diff([r[key] for r in s], [r[cl] for r in s], [r[key] for r in c], [r[cl] for r in c], n_boot=n_boot, seed=SEED)


def msum(rows: list[dict[str, Any]], key: str, cl: str = "day", n_boot: int = N_BOOT) -> dict[str, Any]:
    rows = fin(rows, key)
    if not rows:
        return {"n": 0, "days": 0, "mean": float("nan"), "ci": None, "p": None}
    v = [r[key] for r in rows]
    c = [r[cl] for r in rows]
    bm = cluster_bootstrap_means(v, c, n_boot=n_boot, seed=SEED) if len(set(c)) > 1 else np.array([])
    return {
        "n": len(v),
        "days": len(set(c)),
        "mean": statistics.fmean(v),
        "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))) if bm.size else None,
        "p": float((np.sum(bm <= 0) + 1) / (bm.size + 1)) if bm.size else None,
    }


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [r for r in rows if not r["oos"]], [r for r in rows if r["oos"]]


def groups(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ev = [r for r in rows if r["kind"] == "ev"]
    return [r for r in ev if r["smt"]], [r for r in ev if not r["smt"]]


# ---------------------------------------------------------------- sanity


def sanity(series: list[Series]) -> None:
    emit("## Санітарні перевірки")
    by = {(s.fam, s.tf, s.a): s for s in series}
    b4 = by[("crypto", "4h", "BTC")].A
    k = int(np.searchsorted(b4.t, ms("2024-12-31T20:00")))
    emit(f"- BTCUSDT: 4h-закриття бару 2024-12-31 20:00 (= закриття доби) = {b4.c[k]:g} (еталон 93548.9)")
    e1 = by[("crypto", "1h", "ETH")].A
    for d in ("2021-05-12T00:00", "2022-06-18T00:00", "2024-03-11T00:00"):
        i = int(np.searchsorted(e1.t, ms(d)))
        emit(
            f"- ETHUSDT 1h-закриття {d}: {e1.c[i]:g}; BTC {by[('crypto', '1h', 'BTC')].A.c[int(np.searchsorted(by[('crypto', '1h', 'BTC')].A.t, ms(d)))]:g}"
        )
    for s in series:
        if s.a not in ("BTC", "EURUSD"):
            continue
        A, B = joint_grid(s.A, s.B)
        pos = segment_pos(A.t, s.max_gap)
        miss_a = 1 - len(A) / max(1, len(s.A))
        miss_b = 1 - len(B) / max(1, len(s.B))
        emit(
            f"- {s.fam} {s.tf} {s.a}/{s.b}: барів {len(s.A)} / {len(s.B)}, спільних {len(A)} (без пари {miss_a:.2%} / {miss_b:.2%}), сегментів {int((pos == 0).sum())}, {utc(int(A.t[0])):%Y-%m-%d} … {utc(int(A.t[-1])):%Y-%m-%d}"
        )
        la, lb = np.log(A.c), np.log(B.c)
        same = np.concatenate([[False], np.diff(A.t) == A.bar_ms])
        ra, rb = np.diff(la), np.diff(lb)
        m = same[1:]
        years = np.array([utc(int(t)).year for t in A.t[1:]])
        cors = []
        for y in sorted(set(years.tolist())):
            mm = m & (years == y)
            if mm.sum() > 100:
                cors.append(f"{y}: {np.corrcoef(ra[mm], rb[mm])[0, 1]:.2f}")
        emit(f"  - кореляція лог-дохідностей сусідніх барів за роками: {'; '.join(cors)}")
    if ("fx", "1h", "EURUSD") in by:
        eu = by[("fx", "1h", "EURUSD")].A
        gb = by[("fx", "1h", "GBPUSD")].A
        for d, ref in (("2014-01-02T12:00", "EURUSD ≈ 1.37"), ("2022-09-28T12:00", "EURUSD ≈ 0.96")):
            i = int(np.searchsorted(eu.t, ms(d)))
            emit(f"- EURUSD 1h на {d}: open {eu.o[i]:.5f} ({ref}, еталон стадії 0 Session Stop Hunt FX)")
        i = int(np.searchsorted(gb.t, ms("2016-06-27T12:00")))
        emit(f"- GBPUSD 1h на 2016-06-27T12:00: open {gb.o[i]:.5f} (≈ 1.32)")
    last = slice(len(b4) - 300, len(b4))
    gaps = int(np.sum(np.diff(b4.t[last]) > 4 * HOUR_MS))
    rows = [{"ts": int(t), "high": float(h), "low": float(lo)} for t, h, lo in zip(b4.t[last], b4.h[last], b4.low[last], strict=True)]
    ref = {f["idx"] for f in fractals(rows)["lows"]}
    mine = {int(i) for i in np.flatnonzero(fractal_lows(b4.low[last], np.arange(300), 1))}
    emit(f"- пропусків 4h-барів серед останніх 300: {gaps}")
    emit(f"- фрактальні лоу на останніх 300 4h-барах BTC: наші {len(mine)}, markup.fractals {len(ref)}, збіг {mine == ref}")
    emit(f"- пікова RSS {rss_mb():.0f} MB")


# ---------------------------------------------------------------- stages


def all_rows(series: list[Series], outcomes: bool, fam_vars: dict[str, tuple[str, ...]]) -> tuple[list[dict[str, Any]], Counter[str]]:
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    rng = np.random.default_rng(SEED)
    t0 = time.time()
    for s in series:
        for var in fam_vars[s.fam]:
            rows.extend(collect(s, var, outcomes, rng, why))
        emit(f"  [events] {s.fam} {s.tf} {s.a}/{s.b}: rows {len(rows)} in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    return rows, why


FAM_VARS = {"crypto": tuple(VARIANTS), "fx": ("main", "shift7")}


def stage_count(s14_cache: Path, data_dir: Path, fx_dir: Path) -> None:
    emit("# Крок 10 SMT, стадія 0: кількість подій (без результатів)")
    series = load_series(s14_cache, data_dir, fx_dir, True)
    sanity(series)
    rows, why = all_rows(series, False, FAM_VARS)
    emit(
        "\n| сім'я | ТФ | варіант | свипів IS | S IS | C IS | частка S | дат S IS | дат C IS | S OOS | C OOS | дат S OOS |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for fam in ("crypto", "fx"):
        for tf in ("1h", "4h", "15m"):
            for var in FAM_VARS[fam]:
                r = sel(rows, fam=fam, tf=tf, var=var)
                if not r:
                    continue
                i_, o_ = split(r)
                si, ci_ = [x for x in i_ if x["smt"]], [x for x in i_ if not x["smt"]]
                so, co = [x for x in o_ if x["smt"]], [x for x in o_ if not x["smt"]]
                emit(
                    f"| {fam} | {tf} | {var} | {len(i_)} | {len(si)} | {len(ci_)} | {len(si) / max(1, len(i_)):.1%} | {len({x['day'] for x in si})} | {len({x['day'] for x in ci_})} | {len(so)} | {len(co)} | {len({x['day'] for x in so})} |"
                )
    emit(f"\n- відсів: {dict(sorted(why.items()))}")
    emit("\n## Очікувана потужність (до результатів; SD y12 ≈ 3 ATR, без ефекту дизайну)")
    for tf in ("1h", "4h"):
        i_, _ = split(sel(rows, fam="crypto", tf=tf, var="main"))
        ns, nc = sum(x["smt"] for x in i_), sum(not x["smt"] for x in i_)
        if ns and nc:
            emit(
                f"- crypto {tf}: S {ns}, C {nc} → MDE ≈ 2.8·3·√(1/S + 1/C) = {2.8 * 3 * math.sqrt(1 / ns + 1 / nc):.3f} ATR (ворота {GATE})"
            )
    for tf in ("1h", "15m"):
        i_, _ = split(sel(rows, fam="fx", tf=tf, var="main"))
        ns, nc = sum(x["smt"] for x in i_), sum(not x["smt"] for x in i_)
        if ns and nc:
            emit(f"- fx {tf}: S {ns}, C {nc} → MDE ≈ {2.8 * 3 * math.sqrt(1 / ns + 1 / nc):.3f} ATR")
    per_year = Counter(x["day"][:4] for x in sel(rows, fam="crypto", tf="4h", var="main") if x["smt"])
    emit(f"- crypto 4h, події S за роками: {dict(sorted(per_year.items()))}")


def tf_block(rows: list[dict[str, Any]], fam: str, tf: str) -> dict[str, Any]:
    """Primary numbers of one family / timeframe (variant main)."""
    s, c = groups(sel(rows, fam=fam, tf=tf, var="main"))
    s_is, s_oos = split(s)
    c_is, c_oos = split(c)
    d = dsum(s_is, c_is)
    dw = dsum(s_is, c_is, cl="week")
    do = dsum(s_oos, c_oos)
    tr = msum(s_is, "n12")
    tro = msum(s_oos, "n12")
    return {
        "s_is": s_is,
        "c_is": c_is,
        "s_oos": s_oos,
        "c_oos": c_oos,
        "d": d,
        "dw": dw,
        "do": do,
        "tr": tr,
        "tro": tro,
        "dates": len({r["day"] for r in s_is}),
        "mde": 2.8 * d["se"] if math.isfinite(d["se"]) else float("inf"),
        "ys": msum(s_is, "y12"),
        "yc": msum(c_is, "y12"),
    }


def emit_block(lab: str, b: dict[str, Any]) -> None:
    emit(f"\n### {lab}")
    emit("| величина | IS | OOS |\n|---|---|---|")
    emit(f"| подій S / C | {len(b['s_is'])} / {len(b['c_is'])} | {len(b['s_oos'])} / {len(b['c_oos'])} |")
    emit(f"| дат S | {b['dates']} | {len({r['day'] for r in b['s_oos']})} |")
    ys_o, yc_o = msum(b["s_oos"], "y12", n_boot=N_BOOT_SEC), msum(b["c_oos"], "y12", n_boot=N_BOOT_SEC)
    emit(f"| E[y12 \\| S], ATR [CI дати] | {b['ys']['mean']:+.3f} {fmt_ci(b['ys']['ci'])} | {ys_o['mean']:+.3f} {fmt_ci(ys_o['ci'])} |")
    emit(f"| E[y12 \\| C], ATR [CI дати] | {b['yc']['mean']:+.3f} {fmt_ci(b['yc']['ci'])} | {yc_o['mean']:+.3f} {fmt_ci(yc_o['ci'])} |")
    emit(
        f"| **Δ = S − C**, ATR [CI дати] | **{b['d']['diff']:+.3f} {fmt_ci(b['d']['ci'])}**, p {fp(b['d']['p'])} | {b['do']['diff']:+.3f} {fmt_ci(b['do']['ci'])}, p {fp(b['do']['p'])} |"
    )
    emit(f"| Δ [CI ISO-тижні] | {fmt_ci(b['dw']['ci'])} | — |")
    emit(f"| SE, MDE = 2.8·SE | {b['d']['se']:.3f}, {b['mde']:.3f} | — |")
    emit(
        f"| угода S, E[net] k=12 (на номінал) [CI] | {b['tr']['mean']:+.3%} {fmt_ci(b['tr']['ci'], '+.3%')} | {b['tro']['mean']:+.3%} {fmt_ci(b['tro']['ci'], '+.3%')} |"
    )
    tc = msum(b["c_is"], "n12", n_boot=N_BOOT_SEC)
    emit(f"| угода C, E[net] k=12 | {tc['mean']:+.3%} {fmt_ci(tc['ci'], '+.3%')} | — |")


def stage_report(s14_cache: Path, data_dir: Path, fx_dir: Path, own: Path) -> None:
    emit("# Крок 10: SMT-дивергенція як фільтр свипу — результати")
    emit(
        f"Дані: BTCUSDT / ETHUSDT перпи Binance USD-M (1h, 4h з годинних), EURUSD / GBPUSD Dukascopy bid (1h, 15m з 5m); рішення {utc(CRYPTO_START):%Y-%m-%d} (крипта) / {utc(FX_START):%Y-%m-%d} (форекс) … {utc(END_MS):%Y-%m-%d}; IS < {utc(OOS_MS):%Y-%m-%d} ≤ OOS. y — рух у бік реверсії в ATR активу A; угода — на номінал, net після витрат кола (крипта 0.14 %, форекс 1.3 / 1.6 піпса).\n"
    )
    series = load_series(s14_cache, data_dir, fx_dir, True)
    sanity(series)
    rows, why = all_rows(series, True, FAM_VARS)
    emit(f"\n- відсів: {dict(sorted(why.items()))}")
    (own / "rows_crypto.json").write_text(json.dumps([r for r in rows if r["fam"] == "crypto"], separators=(",", ":")))

    # ---- primary
    emit("\n## Основне: крипта, BTC + ETH, обидва боки, k = 12")
    blocks = {tf: tf_block(rows, "crypto", tf) for tf in ("1h", "4h")}
    for tf, b in blocks.items():
        emit_block(f"крипта {tf}", b)
    p1, p4 = blocks["1h"]["d"]["p"], blocks["4h"]["d"]["p"]
    p_step = min(1.0, 2 * min(p1, p4)) if p1 is not None and p4 is not None else None
    p_adj = holm([*STEP_P, p_step])[-1] if p_step is not None else None
    emit("\n## Ворота, Holm і вердикт (за реєстрацією)")
    emit(
        f"- сирі p: 1h {fp(p1)}, 4h {fp(p4)}; p кроку = min(1, 2·min) = {fp(p_step)}; Holm (m = 10) {fp(p_adj)}; ×11 {fp(min(1.0, 11 * p_step) if p_step is not None else None)}"
    )
    verdicts = {}
    for tf, b in blocks.items():
        v = smt_verdict(
            n_dates=b["dates"],
            mde=b["mde"],
            diff=b["d"]["diff"],
            ci_day=b["d"]["ci"],
            ci_week=b["dw"]["ci"],
            p_holm=p_adj,
            oos_diff=b["do"]["diff"],
            oos_p=b["do"]["p"],
            trade_ci_is=b["tr"]["ci"],
            trade_oos_mean=b["tro"]["mean"],
        )
        verdicts[tf] = v
        emit(
            f"- {tf}: дат S IS {b['dates']} (поріг {MIN_DATES}), MDE {b['mde']:.3f} (ворота {GATE}), Δ {b['d']['diff']:+.3f} (мін. ефект {MIN_EFFECT}) → **{v}**"
        )
    vs = set(verdicts.values())
    step_v = "confirmed" if "confirmed" in vs else ("rejected" if vs == {"rejected"} else "inconclusive")
    emit(f"- **вердикт кроку: {step_v}**")

    # ---- controls
    emit("\n## Контролі й плацебо (крипта, k = 12, IS)")
    emit(
        "| ТФ | P_shift Δ [CI] | S − P_rand [CI] | E[y] P_rand | Δ у децилях глибини | відносний рух S − C (r_A − r_B) [CI] |\n|---|---|---|---|---|---|"
    )
    for tf in ("1h", "4h"):
        sh_s, sh_c = groups(sel(rows, fam="crypto", tf=tf, var="shift7"))
        dsh = dsum(split(sh_s)[0], split(sh_c)[0], n_boot=N_BOOT_SEC)
        main = sel(rows, fam="crypto", tf=tf, var="main")
        s_is = split(groups(main)[0])[0]
        pr_is = split([r for r in main if r["kind"] == "prand"])[0]
        dpr = dsum(s_is, pr_is, n_boot=N_BOOT_SEC)
        ev_is = split([r for r in main if r["kind"] == "ev"])[0]
        cuts = np.quantile([r["depth"] for r in ev_is], np.linspace(0.1, 0.9, 9)).tolist()
        dd = depth_stratified_diff([r["y12"] for r in ev_is], [r["smt"] for r in ev_is], [r["depth"] for r in ev_is], cuts)
        drel = dsum(s_is, split(groups(main)[1])[0], key="rel12", n_boot=N_BOOT_SEC)
        emit(
            f"| {tf} | {dsh['diff']:+.3f} {fmt_ci(dsh['ci'])} | {dpr['diff']:+.3f} {fmt_ci(dpr['ci'])} | {statistics.fmean(r['y12'] for r in pr_is):+.3f} | {dd['diff']:+.3f} ({dd['bins']} децилів) | {drel['diff']:+.3%} {fmt_ci(drel['ci'], '+.3%')} |"
        )
        sd_s = statistics.pstdev([r["y12"] for r in s_is])
        emit(
            f"  - {tf}: SD y12 у S {sd_s:.2f} ATR; медіана глибини S {statistics.median(r['depth'] for r in s_is):.3f}, C {statistics.median(r['depth'] for r in ev_is if not r['smt']):.3f} ATR"
        )

    # ---- grid
    emit("\n## Сітка (усі варіанти; сирі p і Holm усередині кроку — довідка)")
    emit(
        "| сім'я | ТФ | варіант | k | S / C IS | Δ IS [CI дати] | p | Δ OOS | E[y\\|S] IS | E[y\\|C] IS |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    sec: list[tuple[str, float]] = []
    for fam in ("crypto", "fx"):
        for tf in ("1h", "4h", "15m"):
            for var in FAM_VARS[fam]:
                rr = sel(rows, fam=fam, tf=tf, var=var)
                if not rr:
                    continue
                s, c = groups(rr)
                s_is, s_oos = split(s)
                c_is, c_oos = split(c)
                for k in HORIZONS:
                    key = f"y{k}"
                    d = dsum(s_is, c_is, key=key, n_boot=N_BOOT_SEC)
                    do = dsum(s_oos, c_oos, key=key, n_boot=N_BOOT_SEC)
                    a1, a2 = fin(s_is, key), fin(c_is, key)
                    emit(
                        f"| {fam} | {tf} | {var} | {k} | {len(a1)} / {len(a2)} | {d['diff']:+.3f} {fmt_ci(d['ci'])} | {fp(d['p'])} | {do['diff']:+.3f} | {statistics.fmean(r[key] for r in a1):+.3f} | {statistics.fmean(r[key] for r in a2):+.3f} |"
                    )
                    if not (fam == "crypto" and var == "main" and k == 12) and var != "shift7" and d["p"] is not None:
                        sec.append((f"{fam}/{tf}/{var}/k{k}", d["p"]))
    adj = holm([p for _, p in sec])
    emit("\n| рядок | сире p | Holm у кроці |\n|---|---|---|")
    for (lab, p), pa in sorted(zip(sec, adj, strict=True), key=lambda x: x[0][1])[:15]:
        emit(f"| {lab} | {p:.4f} | {pa:.4f} |")
    emit(f"(показано 15 найменших із {len(sec)})")

    # ---- trade
    emit("\n## Угода (вхід за закриттям свипу, вихід через k барів; на номінал, net; крипта, IS / OOS)")
    emit(
        "| ТФ | група | k | n IS | E[gross] IS | E[net] IS [CI] | E[net] ×2 | E[net] ×3 | E[net] OOS |\n|---|---|---|---|---|---|---|---|---|"
    )
    for tf in ("1h", "4h"):
        s, c = groups(sel(rows, fam="crypto", tf=tf, var="main"))
        for g, grp in (("S", s), ("C", c)):
            gi, go = split(grp)
            for k in HORIZONS:
                st = msum(gi, f"n{k}", n_boot=N_BOOT_SEC)
                gr = statistics.fmean(r[f"r{k}"] for r in fin(gi, f"r{k}"))
                x2 = statistics.fmean(r[f"r{k}"] - 2 * r["cost"] for r in fin(gi, f"r{k}"))
                x3 = statistics.fmean(r[f"r{k}"] - 3 * r["cost"] for r in fin(gi, f"r{k}"))
                oo = msum(go, f"n{k}", n_boot=200)
                emit(
                    f"| {tf} | {g} | {k} | {st['n']} | {gr:+.3%} | {st['mean']:+.3%} {fmt_ci(st['ci'], '+.3%')} | {x2:+.3%} | {x3:+.3%} | {oo['mean']:+.3%} |"
                )

    # ---- sides, assets, per-asset
    emit("\n## Боки й активи (крипта, основне, k = 12)")
    emit("| ТФ | зріз | S / C IS | Δ IS [CI] | SE | Δ OOS [CI] |\n|---|---|---|---|---|---|")
    per: dict[str, list[tuple[str, float, float, float]]] = defaultdict(list)
    for tf in ("1h", "4h"):
        main = sel(rows, fam="crypto", tf=tf, var="main")
        for lab, flt in (
            ("бичачі (свип лою)", {"side": "bull"}),
            ("ведмежі (свип хаю)", {"side": "bear"}),
            ("A = BTC", {"a": "BTC"}),
            ("A = ETH", {"a": "ETH"}),
        ):
            s, c = groups(sel(main, **flt))
            si, so = split(s)
            ci_, co = split(c)
            d, do = dsum(si, ci_, n_boot=N_BOOT_SEC), dsum(so, co, n_boot=N_BOOT_SEC)
            emit(
                f"| {tf} | {lab} | {len(si)} / {len(ci_)} | {d['diff']:+.3f} {fmt_ci(d['ci'])} | {d['se']:.3f} | {do['diff']:+.3f} {fmt_ci(do['ci'])} |"
            )
            if "a" in flt:
                per[tf].append((flt["a"], d["diff"], d["se"], do["diff"]))
    emit("\n## Перевірка по активах (рішення власника 2026-10-17)")
    for fam, tfs in (("crypto", ("1h", "4h")), ("fx", ("1h", "15m"))):
        for tf in tfs:
            if fam == "fx":
                main = sel(rows, fam="fx", tf=tf, var="main")
                if not main:
                    continue
                for a in ("EURUSD", "GBPUSD"):
                    s, c = groups(sel(main, a=a))
                    si, so = split(s)
                    ci_, co = split(c)
                    d, do = dsum(si, ci_, n_boot=N_BOOT_SEC), dsum(so, co, n_boot=N_BOOT_SEC)
                    per[f"fx{tf}"].append((a, d["diff"], d["se"], do["diff"]))
            key = tf if fam == "crypto" else f"fx{tf}"
            lst = per.get(key, [])
            if not lst:
                continue
            q = cochran_q([x[1] for x in lst], [x[2] for x in lst])
            emit(
                f"- {fam} {tf}: "
                + "; ".join(f"{a}: Δ IS {d:+.3f} (SE {se:.3f}), Δ OOS {o:+.3f}" for a, d, se, o in lst)
                + f" — Cochran Q {q['q']:.2f}, df {q['df']}, p {fp(q['p'])}, I² {q['i2']:.0%}"
            )
    emit("- сталість: активів по 2 на пару, топ-чверть не має сенсу; порівнюється знак Δ в IS і OOS по кожному активу (вище).")

    # ---- robustness
    emit("\n## Стійкість основної метрики (крипта, k = 12, IS)")
    for tf in ("1h", "4h"):
        b = blocks[tf]
        s_is, c_is = b["s_is"], b["c_is"]
        tot: dict[str, float] = defaultdict(float)
        for r in s_is:
            tot[r["day"]] += r["y12"]
        top = sorted(tot, key=lambda d: -tot[d])
        parts = []
        for k in (5, 10, 20):
            drop = set(top[:k])
            d = dsum([r for r in s_is if r["day"] not in drop], [r for r in c_is if r["day"] not in drop], n_boot=N_BOOT_SEC)
            parts.append(f"без {k} найкращих дат Δ {d['diff']:+.3f} {fmt_ci(d['ci'])}")
        emit(f"- {tf}: " + "; ".join(parts))
        yrs = []
        for y in range(2020, 2027):
            si = [r for r in b["s_is"] + b["s_oos"] if r["day"].startswith(str(y))]
            ci_ = [r for r in b["c_is"] + b["c_oos"] if r["day"].startswith(str(y))]
            if si and ci_:
                yrs.append(f"{y}: {dsum(si, ci_, n_boot=200)['diff']:+.3f} ({len(si)})")
        emit(f"  - за роками (Δ, n S): {'; '.join(yrs)}")
        days = sorted({r["day"] for r in s_is})
        mid = days[len(days) // 2]
        h1 = dsum([r for r in s_is if r["day"] < mid], [r for r in c_is if r["day"] < mid], n_boot=N_BOOT_SEC)
        h2 = dsum([r for r in s_is if r["day"] >= mid], [r for r in c_is if r["day"] >= mid], n_boot=N_BOOT_SEC)
        emit(f"  - половини IS (до / з {mid}): {h1['diff']:+.3f} {fmt_ci(h1['ci'])} / {h2['diff']:+.3f} {fmt_ci(h2['ci'])}")
        emit(f"  - частка S серед свипів IS: {len(s_is) / max(1, len(s_is) + len(c_is)):.1%}")

    # ---- fx
    emit("\n## Форекс (другорядне, поза сім'єю Holm)")
    for tf in ("1h", "15m"):
        if not sel(rows, fam="fx", tf=tf, var="main"):
            emit(f"- fx {tf}: немає даних")
            continue
        b = tf_block(rows, "fx", tf)
        emit_block(f"EURUSD + GBPUSD {tf}", b)
        if b["dates"] < MIN_DATES or b["mde"] > GATE:
            mark = "◯ (ворота)"
        elif b["d"]["diff"] < MIN_EFFECT or b["d"]["ci"] is None or b["d"]["ci"][0] <= 0:
            mark = "≈ (знак тези, CI > 0, нижче порогу)" if b["d"]["ci"] is not None and b["d"]["ci"][0] > 0 else "✗"
        elif b["do"]["diff"] > 0 and b["tr"]["ci"] is not None and b["tr"]["ci"][0] > 0:
            mark = "✓ (кандидат на нову картку)"
        else:
            mark = "≈ (Δ ≥ порогу, але без OOS чи прибуткової угоди)"
        emit(f"- **позначка fx {tf}: {mark}**")
        sh_s, sh_c = groups(sel(rows, fam="fx", tf=tf, var="shift7"))
        dsh = dsum(split(sh_s)[0], split(sh_c)[0], n_boot=N_BOOT_SEC)
        emit(f"- P_shift (B −7 діб): Δ {dsh['diff']:+.3f} {fmt_ci(dsh['ci'])}")
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def stage_explore(s14_cache: Path, data_dir: Path, fx_dir: Path) -> None:
    """Exploratory, after the registered run: the B window narrowed to +-0 / +-1 bars (same rules otherwise)."""
    emit("# Крок 10 SMT, дослідницьке (після прогону; на вердикт не впливає): вікно B ±0 / ±1 / ±2, крипта")
    emit(f"Бутстреп {N_BOOT_SEC} повторів, сід {SEED}, кластер — UTC-дата; IS < {utc(OOS_MS):%Y-%m-%d} ≤ OOS.\n")
    series = [s for s in load_series(s14_cache, data_dir, fx_dir, False) if s.fam == "crypto"]
    why: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    t0 = time.time()
    for s in series:
        for var in EXPLORE_VARIANTS:
            rows.extend(collect(s, var, True, None, why))
        emit(f"  [events] {s.tf} {s.a}/{s.b}: rows {len(rows)} in {time.time() - t0:.0f}s, rss {rss_mb():.0f} MB")
    emit(
        "\n| вікно B | ТФ | S / C IS | частка S | Δ k = 4 | Δ k = 12 [CI] | p | Δ k = 24 | Δ k = 12 OOS |\n|---|---|---|---|---|---|---|---|---|"
    )
    for var in EXPLORE_VARIANTS:
        for tf in ("1h", "4h"):
            s, c = groups(sel(rows, tf=tf, var=var))
            s_is, s_oos = split(s)
            c_is, c_oos = split(c)
            d = {k: dsum(s_is, c_is, key=f"y{k}", n_boot=N_BOOT_SEC) for k in HORIZONS}
            do = dsum(s_oos, c_oos, n_boot=N_BOOT_SEC)
            share = len(s_is) / max(1, len(s_is) + len(c_is))
            emit(
                f"| {EXPLORE_LABEL[var]} | {tf} | {len(s_is)} / {len(c_is)} | {share:.1%} | {d[4]['diff']:+.3f} | {d[12]['diff']:+.3f} {fmt_ci(d[12]['ci'])} | {fp(d[12]['p'])} | {d[24]['diff']:+.3f} | {do['diff']:+.3f} |"
            )
    emit(f"\n- відсів: {dict(sorted(why.items()))}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/smt_divergence")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--fx-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/fx")
    ap.add_argument("--stage", choices=("prereg", "count", "report", "explore"), required=True)
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
    elif a.stage == "count":
        stage_count(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.fx_dir.expanduser())
    elif a.stage == "explore":
        stage_explore(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.fx_dir.expanduser())
    else:
        stage_report(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.fx_dir.expanduser(), own)
    if a.stage != "prereg":
        emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
