"""Course setups, step 4 (IDEA-20261016-course-setups-crypto): S12 + S28 — fair value gaps, order blocks with the
course's validity flags (impulsive / fvg_after / bos_after) and breakers (break_impulsive / fvg_on_break) as reaction
levels: a buy (sell) limit on the first test, stop 1 ATR beyond, target 2R, against a random level at the same distance.

Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 4". Stages:
  uv run python scripts/verify_course_step4_fvg_ob.py --stage prereg
  uv run python scripts/verify_course_step4_fvg_ob.py --stage count  --out ~/.cache/tradedesk-agents/course_step4_fvg_ob/count.md
  uv run python scripts/verify_course_step4_fvg_ob.py --stage report --out ~/.cache/tradedesk-agents/course_step4_fvg_ob/report.md
  uv run python scripts/verify_course_step4_fvg_ob.py --stage analyse --out ~/.cache/tradedesk-agents/course_step4_fvg_ob/analyse.md
``count`` (stage 0) counts eligible events without any trade outcome; ``analyse`` recomputes the statistics from the
saved rows.npz without simulating again. ``--symbols N`` runs the first N symbols only
(timing). No network: the 1h bars and the universe come from the step-1 cache (--s14-cache, filled from
data.binance.vision via rt.binance_vision); listings from the funding-crowding cache (--data-dir). Per-row results go
to --cache-dir/rows.npz. No keys, no orders.
"""

from __future__ import annotations

import argparse
import hashlib
import resource
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders, universe, period)
import verify_course_step3_s18 as s3  # noqa: E402  (membership intervals)

from tradedesk.analytics import markup  # noqa: E402
from tradedesk.analytics.course_fvg_ob import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    MARKET_SIDE,
    bos_up,
    breaker_zones,
    fvg_zones,
    limit_long,
    ob_zones,
    untouched,
)
from tradedesk.analytics.course_s14_ema import align_daily, atr, ema  # noqa: E402
from tradedesk.analytics.course_s18_trend import pos_from_seg, resample_partial, segments  # noqa: E402
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means, holm, without_top_days  # noqa: E402
from tradedesk.analytics.session_stop_hunt import cluster_bootstrap_diff  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
SEED = 20261017
PER_EVENT = 3
WARMUP = 100
WAIT_BARS = HOLD_BARS = 20
STOP_ATR, TARGET_R, FAR_BUF, PEN = 1.0, 2.0, 0.1, 0.05
RAND_WIN_MS, IMP_WIN_MS, IMP_TOL = 30 * DAY_MS, 60 * DAY_MS, 0.25
START_MS, OOS_MS = s1.START_MS, s1.OOS_MS
END_MS = {"4h": s1.END_MS, "1d": s1.ms("2026-08-20T00:00")}
TF_HOURS = {"4h": 4, "1d": 24}
STEP_P = (0.985, 0.280, 0.448)  # primary p of steps 1-3 (Holm family of the card)
MIN_DATES, MDE_GATE, MIN_EFFECT = 200, 0.10, 0.10

# (tf, zone kind, flag set, level, stop mode)
PRIMARY = {
    "A": ("4h", "fvg", "all", "mid", "atr"),
    "B": ("4h", "ob", "valid", "body", "atr"),
    "C": ("4h", "breaker", "valid", "body", "atr"),
}
VARIANTS: list[tuple[str, str, str, str, str]] = [
    PRIMARY["A"],
    ("4h", "fvg", "all", "near", "atr"),
    ("4h", "fvg", "all", "full", "atr"),
    ("4h", "fvg", "imp", "mid", "atr"),
    ("4h", "fvg", "trend", "mid", "atr"),
    ("4h", "fvg", "all", "mid", "far"),
    PRIMARY["B"],
    ("4h", "ob", "all", "body", "atr"),
    ("4h", "ob", "imp", "body", "atr"),
    ("4h", "ob", "fvg", "body", "atr"),
    ("4h", "ob", "bos", "body", "atr"),
    ("4h", "ob", "valid", "wick", "atr"),
    ("4h", "ob", "valid", "bmid", "atr"),
    ("4h", "ob", "valid", "body", "far"),
    PRIMARY["C"],
    ("4h", "breaker", "all", "body", "atr"),
    ("4h", "breaker", "bimp", "body", "atr"),
    ("4h", "breaker", "fob", "body", "atr"),
    ("4h", "breaker", "valid", "body", "far"),
    ("1d", "fvg", "all", "mid", "atr"),
    ("1d", "ob", "valid", "body", "atr"),
    ("1d", "breaker", "valid", "body", "atr"),
]
VID = {v: k for k, v in enumerate(VARIANTS)}
PRIMARY_IDS = {VID[v] for v in PRIMARY.values()} | {
    VID[("1d", "fvg", "all", "mid", "atr")],
    VID[("1d", "ob", "valid", "body", "atr")],
    VID[("1d", "breaker", "valid", "body", "atr")],
}
IMP_VARS = {VID[PRIMARY["A"]], VID[("1d", "fvg", "all", "mid", "atr")]}
OB_ALL = VID[("4h", "ob", "all", "body", "atr")]
KINDS = {"ev": 0, "rand": 1, "imp": 2}
EXITS = ["none", "stop", "stop_gap", "target", "time", "data_end"]
COLS = (
    "sym var kind side day oos eligible filled touch5 r_gross cost_r exit_kind fill exit risk mfe react valid_ok pen_filled pen_r_net arm_day"
).split()


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def vlabel(v: tuple[str, str, str, str, str]) -> str:
    tf, kind, fs, lvl, stop = v
    return f"{tf} {kind} F={fs} L={lvl}{' стоп=дальня межа' if stop == 'far' else ''}"


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 4")
    b = t.index("\n## ", a + 1)
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- per-symbol run


class Rows:
    def __init__(self) -> None:
        self.chunks: list[dict[str, np.ndarray]] = []
        self.cur: dict[str, list[float]] = {c: [] for c in COLS}

    def add(self, **kw: float) -> None:
        for c in COLS:
            self.cur[c].append(kw.get(c, np.nan))

    def flush(self) -> None:
        if self.cur["sym"]:
            self.chunks.append({c: np.asarray(v, dtype=np.float32) for c, v in self.cur.items()})
        self.cur = {c: [] for c in COLS}

    def table(self) -> dict[str, np.ndarray]:
        self.flush()
        if not self.chunks:
            return {c: np.zeros(0) for c in COLS}
        out = {}
        for c in COLS:  # column by column, freeing the chunks as we go
            out[c] = np.concatenate([ch.pop(c) for ch in self.chunks])
        self.chunks = []
        return out

    def __len__(self) -> int:
        return sum(ch["sym"].size for ch in self.chunks) + len(self.cur["sym"])


def trade_fields(tr: Any, start_ms: int, bar_ms: int) -> dict[str, float]:
    if tr is None:
        return {"eligible": 0.0}
    out = {"eligible": 1.0, "filled": float(tr.filled), "risk": tr.risk}
    if tr.filled:
        out.update(
            touch5=float(tr.touch_ms < start_ms + 5 * bar_ms),
            r_gross=tr.r_gross,
            cost_r=tr.cost_r,
            exit_kind=float(EXITS.index(tr.exit_kind)),
            fill=tr.fill,
            exit=tr.exit,
            mfe=tr.mfe_r,
            react=float(tr.react),
        )
    else:
        out["touch5"] = 0.0
    return out


def run_symbol(
    si: int, s: str, h1: Any, iv: np.ndarray | None, rng: np.random.Generator, outcomes: bool, rows: Rows, why: Counter[str]
) -> None:
    seg = segments(h1.t)
    d, dseg = resample_partial(h1, seg, 24, 24)
    dpos = pos_from_seg(dseg)
    with np.errstate(invalid="ignore"):
        e21 = ema(d.c, 21, dpos)
        d_up, d_dn = (d.c > e21).astype(float), (d.c < e21).astype(float)
    for tf in ("4h", "1d"):
        hrs = TF_HOURS[tf]
        bar_ms = hrs * HOUR_MS
        bt, segt = resample_partial(h1, seg, hrs, hrs)
        if len(bt) < WARMUP + 30:
            continue
        post = pos_from_seg(segt)
        at = atr(bt, 14, post)
        close = bt.t + bar_ms
        ok = s3.in_intervals(close, iv) & (close >= START_MS) & (close < END_MS[tf]) & (post >= WARMUP) & np.isfinite(at)
        seg_lo = np.searchsorted(segt, segt, side="left")
        seg_hi = np.searchsorted(segt, segt, side="right")
        vars_tf = [(k, v) for k, v in enumerate(VARIANTS) if v[0] == tf]
        htf = {1: align_daily(bt, d, d_up) > 0.5, -1: align_daily(bt, d, d_dn) > 0.5} if tf == "4h" else {1: None, -1: None}
        for side in (1, -1):
            B = bt if side == 1 else bt.mirrored()
            P = h1 if side == 1 else h1.mirrored()
            bos = bos_up(B, post)
            zones = fvg_zones(B, post, at, htf[side]) + ob_zones(B, post, at, bos) + breaker_zones(B, post, at)
            with np.errstate(invalid="ignore", divide="ignore"):
                imp_arr = np.full(len(B), np.nan)
                imp_arr[2:] = (B.c[1:-1] - B.o[1:-1]) / at[:-2]
                gap_arr = np.zeros(len(B), dtype=bool)
                gap_arr[2:] = B.low[2:] > B.h[:-2]
            for z in zones:
                for vid, (_, kind, fs, lvl, stop_mode) in vars_tf:
                    if z.kind != kind:
                        continue
                    arm = z.arms.get(fs)
                    if arm is None or arm >= len(B) or not ok[arm]:
                        continue
                    level = z.levels[lvl]
                    if not untouched(B, z.form, arm, level):
                        why[f"{vid}:touched_before_arm"] += 1
                        continue
                    A = z.atr
                    dist = (float(B.c[arm]) - level) / A
                    if dist < 0:
                        why[f"{vid}:price_inside_zone"] += 1
                        continue
                    stop = level - STOP_ATR * A if stop_mode == "atr" else z.far - FAR_BUF * A
                    if stop >= level:
                        why[f"{vid}:no_risk"] += 1
                        continue
                    rmult = (level - stop) / A
                    start = int(close[arm])
                    day = start // DAY_MS
                    oos = float(start >= OOS_MS)
                    base = {"sym": si, "var": vid, "side": side, "day": day, "oos": oos, "arm_day": day}
                    if not outcomes:
                        why[f"{vid}:ok"] += 1
                        rows.add(**base, kind=0, eligible=1.0)
                        continue
                    tr = limit_long(
                        P, start, WAIT_BARS * bar_ms, HOLD_BARS * bar_ms, level, stop, level + TARGET_R * (level - stop), react_px=A
                    )
                    if tr is None:
                        why[f"{vid}:no_path"] += 1
                        continue
                    why[f"{vid}:ok"] += 1
                    extra: dict[str, float] = {}
                    if vid == OB_ALL:
                        extra["valid_ok"] = float(
                            "valid" in z.arms and untouched(B, z.form, z.arms["valid"], level) and bool(ok[z.arms["valid"]])
                        )
                    if vid in PRIMARY_IDS:
                        tp = limit_long(
                            P, start, WAIT_BARS * bar_ms, HOLD_BARS * bar_ms, level, stop, level + TARGET_R * (level - stop), pen=PEN * A
                        )
                        extra["pen_filled"] = float(bool(tp and tp.filled))
                        extra["pen_r_net"] = tp.r_net if tp is not None and tp.filled else np.nan
                    rows.add(**base, kind=0, **trade_fields(tr, start, bar_ms), **extra)
                    # placebo P_rand: random bars of the same segment within 30 days, same distance in ATR
                    pools = [("rand", RAND_WIN_MS, None)]
                    if vid in IMP_VARS:
                        pools.append(("imp", IMP_WIN_MS, z.impulse))
                    for pk, win, imp in pools:
                        lo = max(int(np.searchsorted(close, start - win, side="left")), int(seg_lo[arm]))
                        hi = min(int(np.searchsorted(close, start + win, side="right")), int(seg_hi[arm]))
                        cand = np.flatnonzero(ok[lo:hi]) + lo
                        cand = cand[cand != arm]
                        if imp is not None:
                            if not np.isfinite(imp):
                                cand = cand[:0]
                            else:
                                with np.errstate(invalid="ignore"):
                                    cand = cand[(np.abs(imp_arr[cand] - imp) <= IMP_TOL) & ~gap_arr[cand]]
                        if cand.size == 0:
                            why[f"{vid}:{pk}_empty"] += 1
                            continue
                        for j in rng.choice(cand, size=min(PER_EVENT, cand.size), replace=False):
                            j = int(j)
                            aj = float(at[j])
                            lj = float(B.c[j]) - dist * aj
                            sj = lj - rmult * aj
                            ps = int(close[j])
                            pt = limit_long(P, ps, WAIT_BARS * bar_ms, HOLD_BARS * bar_ms, lj, sj, lj + TARGET_R * (lj - sj), react_px=aj)
                            if pt is None:
                                continue
                            pextra: dict[str, float] = {}
                            if vid in PRIMARY_IDS and pk == "rand":
                                pp = limit_long(
                                    P, ps, WAIT_BARS * bar_ms, HOLD_BARS * bar_ms, lj, sj, lj + TARGET_R * (lj - sj), pen=PEN * aj
                                )
                                pextra["pen_filled"] = float(bool(pp and pp.filled))
                                pextra["pen_r_net"] = pp.r_net if pp is not None and pp.filled else np.nan
                            rows.add(**{**base, "arm_day": ps // DAY_MS}, kind=KINDS[pk], **trade_fields(pt, ps, bar_ms), **pextra)


def collect(cache: Path, data_dir: Path, outcomes: bool, limit: int | None) -> tuple[dict[str, np.ndarray], Counter[str], list[str]]:
    uni = s1.members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    if limit:
        syms = syms[:limit]
    ivs = s3.member_intervals(uni)
    rng = np.random.default_rng(SEED)
    rows = Rows()
    why: Counter[str] = Counter()
    t0 = time.time()
    for n, s in enumerate(syms):
        h1 = s1.load_1h(cache, data_dir, s)
        if len(h1) < 500:
            why["symbol_too_short"] += 1
            continue
        run_symbol(n, s, h1, ivs.get(s), rng, outcomes, rows, why)
        rows.flush()
        del h1
        if (n + 1) % 10 == 0 or n + 1 == len(syms):
            emit(f"  [run] {n + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rows {len(rows)}, rss {rss_mb():.0f} MB")
    return rows.table(), why, syms


# ---------------------------------------------------------------- stats


def week_of(day: np.ndarray) -> np.ndarray:
    return (day + 3) // 7  # ISO weeks (1970-01-01 is a Thursday)


def ci_str(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"[{ci[0]:+.3f}; {ci[1]:+.3f}]"


def summ(v: np.ndarray, cl: np.ndarray, n_boot: int = 5000) -> dict[str, Any]:
    if v.size == 0:
        return {"n": 0, "days": 0, "mean": float("nan"), "ci": None, "p": None, "se": None}
    multi = np.unique(cl).size > 1
    bm = cluster_bootstrap_means(v.tolist(), cl.tolist(), n_boot=n_boot) if multi else np.array([])
    return {
        "n": int(v.size),
        "days": int(np.unique(cl).size),
        "mean": float(v.mean()),
        "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))) if multi else None,
        "p": float((np.sum(bm <= 0) + 1) / (bm.size + 1)) if multi else None,
        "se": float(np.std(bm)) if multi else None,
    }


def bdiff(a: np.ndarray, acl: np.ndarray, b: np.ndarray, bcl: np.ndarray, n_boot: int = 5000) -> dict[str, Any]:
    if a.size == 0 or b.size == 0:
        return {"diff": float("nan"), "ci": None, "p": None, "se": None}
    out = cluster_bootstrap_diff(a.tolist(), acl.tolist(), b.tolist(), bcl.tolist(), n_boot=n_boot)
    out["se"] = (out["ci"][1] - out["ci"][0]) / (2 * 1.96) if out["ci"] is not None else None
    return out


class View:
    def __init__(self, R: dict[str, np.ndarray]) -> None:
        self.R = R
        self.net = R["r_gross"] - R["cost_r"]

    def m(self, **kw: Any) -> np.ndarray:
        mask = np.ones(self.R["sym"].size, dtype=bool)
        for k, v in kw.items():
            mask &= self.R[k] == v
        return mask


def var_block(V: View, vid: int, oos: float | None, n_boot: int = 5000, ptype: int = 1, mask: np.ndarray | None = None) -> dict[str, Any]:
    R = V.R
    extra = np.ones(R["sym"].size, dtype=bool) if mask is None else mask
    per = {} if oos is None else {"oos": oos}
    ev = V.m(var=vid, kind=0, **per) & extra
    pr = V.m(var=vid, kind=ptype, **per) & extra
    evf, prf = ev & (R["filled"] == 1), pr & (R["filled"] == 1)
    s = summ(V.net[evf], R["day"][evf], n_boot)
    sp = summ(V.net[prf], R["day"][prf], 1000)
    dd = bdiff(V.net[evf], R["day"][evf], V.net[prf], R["day"][prf], n_boot)
    react_e = R["react"][evf]
    react_p = R["react"][prf]
    return {
        "n_ev": int(ev.sum()),
        "n_pr": int(pr.sum()),
        "fill_e": float(R["filled"][ev].mean()) if ev.any() else float("nan"),
        "fill_p": float(R["filled"][pr].mean()) if pr.any() else float("nan"),
        "t5_e": float(R["touch5"][ev].mean()) if ev.any() else float("nan"),
        "t5_p": float(R["touch5"][pr].mean()) if pr.any() else float("nan"),
        "s": s,
        "sp": sp,
        "d": dd,
        "gross_e": float(R["r_gross"][evf].mean()) if evf.any() else float("nan"),
        "react_e": float(np.mean(react_e == 1)) if react_e.size else float("nan"),
        "react_p": float(np.mean(react_p == 1)) if react_p.size else float("nan"),
        "mfe_e": float(np.median(R["mfe"][evf])) if evf.any() else float("nan"),
        "mfe_p": float(np.median(R["mfe"][prf])) if prf.any() else float("nan"),
        "evf": evf,
        "prf": prf,
        "ev": ev,
        "pr": pr,
    }


GRID_HDR = (
    "| варіант | подій | виконано (подія / P_rand) | дотик за 5 барів (подія / P_rand) | n угод | дат | E[R] gross | E[R] net [CI] | P_rand E[R] net | Δ [CI] | p Δ | +1 ATR раніше −1 ATR (подія / P_rand) | OOS E[R] net | OOS Δ |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def grid_line(label: str, b: dict[str, Any], bo: dict[str, Any]) -> str:
    s, d = b["s"], b["d"]
    p = "—" if d["p"] is None else f"{d['p']:.4f}"
    return (
        f"| {label} | {b['n_ev']} | {b['fill_e']:.1%} / {b['fill_p']:.1%} | {b['t5_e']:.1%} / {b['t5_p']:.1%} | {s['n']} | {s['days']} | {b['gross_e']:+.3f} | "
        f"{s['mean']:+.3f} {ci_str(s['ci'])} | {b['sp']['mean']:+.3f} | {d['diff']:+.3f} {ci_str(d['ci'])} | {p} | {b['react_e']:.1%} / {b['react_p']:.1%} | "
        f"{bo['s']['mean']:+.3f} | {bo['d']['diff']:+.3f} |"
    )


# ---------------------------------------------------------------- stages


def sanity(cache: Path, data_dir: Path) -> None:
    emit("## Санітарні перевірки")
    h1 = s1.load_1h(cache, data_dir, "BTCUSDT")
    seg = segments(h1.t)
    b4, s4 = resample_partial(h1, seg, 4, 4)
    p4 = pos_from_seg(s4)
    a4 = atr(b4, 14, p4)
    k = int(np.searchsorted(b4.t, s1.ms("2024-12-31T20:00")))
    emit(f"- BTCUSDT: 4h-закриття 2024-12-31 20:00–24:00 = {b4.c[k]:g} (еталон денного закриття з попередніх кроків: 93548.9)")
    emit(f"- BTCUSDT: 4h-барів {len(b4)}, сегментів {int(s4.max()) + 1}; медіана ATR(4h)/ціна {np.nanmedian(a4 / b4.c):.2%}")
    rows = [
        {"ts": int(b4.t[i]), "open": b4.o[i], "high": b4.h[i], "low": b4.low[i], "close": b4.c[i]} for i in range(len(b4) - 300, len(b4))
    ]
    ref = markup.fvgs(rows)
    sub = b4.slice(len(b4) - 300, len(b4))
    mine = fvg_zones(sub, p4[len(b4) - 300 :] - p4[len(b4) - 300], a4[len(b4) - 300 :]) + fvg_zones(
        sub.mirrored(), p4[len(b4) - 300 :] - p4[len(b4) - 300], a4[len(b4) - 300 :]
    )
    emit(
        f"- BTCUSDT, останні 300 4h-барів: FVG за markup.fvgs {len(ref)} (бичачих {sum(g['type'] == 'bull' for g in ref)}), за course_fvg_ob {len(mine)} — мають збігатися"
    )
    emit(
        f"- частка 4h-барів BTC, що закривають бичачий FVG: {np.mean(b4.low[2:] > b4.h[:-2]):.1%}, ведмежий: {np.mean(b4.h[2:] < b4.low[:-2]):.1%}"
    )
    emit(f"- пікова RSS {rss_mb():.0f} MB")


def stage_count(cache: Path, data_dir: Path, limit: int | None) -> None:
    emit("# Крок 4 (FVG / OB / брейкери), стадія 0: частота допущених подій (без результатів угод)")
    sanity(cache, data_dir)
    R, why, _ = collect(cache, data_dir, False, limit)
    emit("\n| варіант | подій IS | дат IS | подій OOS | дат OOS | лонг / шорт |\n|---|---|---|---|---|---|")
    for vid, v in enumerate(VARIANTS):
        m = R["var"] == vid
        i_, o_ = m & (R["oos"] == 0), m & (R["oos"] == 1)
        tag = " (основна)" if v in PRIMARY.values() else ""
        emit(
            f"| {vlabel(v)}{tag} | {int(i_.sum())} | {np.unique(R['day'][i_]).size} | {int(o_.sum())} | {np.unique(R['day'][o_]).size} | {int((m & (R['side'] == 1)).sum())} / {int((m & (R['side'] == -1)).sum())} |"
        )
    emit(f"\n- відсів: {dict(sorted(why.items()))}")


def verdict_for(V: View, key: str, vid: int, p_step_adj: float | None) -> tuple[str, dict[str, Any]]:
    R = V.R
    b = var_block(V, vid, 0.0, 10000)
    bo = var_block(V, vid, 1.0, 10000)
    evf, prf = b["evf"], b["prf"]
    wk_s = summ(V.net[evf], week_of(R["day"][evf]), 10000)
    wk_d = bdiff(V.net[evf], week_of(R["day"][evf]), V.net[prf], week_of(R["day"][prf]), 10000)
    s, d = b["s"], b["d"]
    mde = 2.8 * (d["se"] if d["se"] is not None else float("inf"))
    info = {"b": b, "bo": bo, "wk_s": wk_s, "wk_d": wk_d, "mde": mde}
    if s["days"] < MIN_DATES or mde > MDE_GATE:
        return "inconclusive (замало дат або потужності)", info
    if s["ci"] is None or s["ci"][0] <= 0 or d["diff"] < MIN_EFFECT:
        return "rejected", info
    if (
        d["ci"] is not None
        and d["ci"][0] > 0
        and wk_s["ci"] is not None
        and wk_s["ci"][0] > 0
        and wk_d["ci"] is not None
        and wk_d["ci"][0] > 0
        and p_step_adj is not None
        and p_step_adj < 0.05
        and bo["s"]["mean"] > 0
        and bo["s"]["p"] is not None
        and bo["s"]["p"] < 0.05
        and bo["d"]["diff"] > 0
    ):
        return "confirmed", info
    return "inconclusive (позитив в IS не пройшов CI за тижнями, Holm чи OOS)", info


def stage_report(cache: Path, data_dir: Path, limit: int | None, out_dir: Path) -> None:
    emit("# Крок 4 (S12 + S28): FVG, OB з критеріями курсу, брейкери — результати")
    emit(
        f"Всесвіт і дані — кроку 1; бари рішення {s1.day_of(START_MS)} … {s1.day_of(END_MS['4h'])} (4h), … {s1.day_of(END_MS['1d'])} (1D); IS < {s1.day_of(OOS_MS)} ≤ OOS. "
        f"Ліміт на рівні, чекає {WAIT_BARS} барів; стоп {STOP_ATR:g} ATR, ціль {TARGET_R:g}R, утримання {HOLD_BARS} барів. R — на ризик угоди, не на капітал; net після мейкера 0.02 % на вході й цілі та 0.07 % на ринкових виходах.\n"
    )
    sanity(cache, data_dir)
    R, why, syms = collect(cache, data_dir, True, limit)
    np.savez_compressed(out_dir / "rows.npz", **R)
    emit(f"\n- рядків {R['sym'].size}; відсів і лічильники: {dict(sorted(why.items()))}")
    analyse(R, syms)


def stage_analyse(cache: Path, out_dir: Path) -> None:
    """Statistics only, from the rows saved by ``report`` (no simulation)."""
    emit("# Крок 4 (S12 + S28): статистика з rows.npz зареєстрованого прогону (без повторної симуляції)")
    with np.load(out_dir / "rows.npz") as z:
        R = {c: z[c] for c in COLS}
    uni = s1.members(cache)
    analyse(R, sorted({s for v in uni.values() for s in v}))


def magnet(V: View) -> None:
    """H2 'magnet' and 'reaction' differences with CIs (registered secondaries)."""
    R = V.R
    emit("\n## H2 «магніт» і «реакція» проти P_rand (IS; різниця частот у п. п., CI — бутстреп за датами)")
    emit(
        "| варіант | дотик за 5 барів: подія / P_rand / Δ [CI] | дотик за 20 барів: подія / P_rand / Δ [CI] | +1 ATR раніше −1 ATR (виконані): подія / P_rand / Δ [CI] |\n|---|---|---|---|"
    )
    for v in (
        PRIMARY["A"],
        ("4h", "fvg", "all", "near", "atr"),
        ("4h", "fvg", "all", "full", "atr"),
        PRIMARY["B"],
        PRIMARY["C"],
        ("1d", "fvg", "all", "mid", "atr"),
    ):
        vid = VID[v]
        ev, pr = V.m(var=vid, kind=0, oos=0.0), V.m(var=vid, kind=1, oos=0.0)
        evf, prf = ev & (R["filled"] == 1), pr & (R["filled"] == 1)
        cells = []
        for col, a, b in (("touch5", ev, pr), ("filled", ev, pr), ("react", evf, prf)):
            xa = (R[col][a] == 1).astype(float)
            xb = (R[col][b] == 1).astype(float)
            d = bdiff(xa, R["day"][a], xb, R["day"][b], 5000)
            ci = "—" if d["ci"] is None else f"[{d['ci'][0] * 100:+.1f}; {d['ci'][1] * 100:+.1f}]"
            cells.append(f"{xa.mean():.1%} / {xb.mean():.1%} / {d['diff'] * 100:+.1f} {ci}")
        emit(f"| {vlabel(v)} | {' | '.join(cells)} |")


def analyse(R: dict[str, np.ndarray], syms: list[str]) -> None:
    V = View(R)

    # primary p and Holm
    raw_p = {}
    for key, v in PRIMARY.items():
        b = var_block(V, VID[v], 0.0, 10000)
        raw_p[key] = b["d"]["p"]
    ps = [p for p in raw_p.values() if p is not None]
    p_step = min(1.0, 3 * min(ps)) if ps else None
    p_adj = holm([*STEP_P, p_step])[3] if p_step is not None else None
    emit("\n## Основні метрики (4h, обидва боки, IS) і ворота")
    emit(
        f"- сирі p (Δ > 0): A {raw_p['A']}, B {raw_p['B']}, C {raw_p['C']}; p кроку = min(1, 3·min) = {p_step}; Holm (m = 4: 0.985, 0.280, 0.448, крок 4) = {p_adj}; ×11 для довідки {min(1.0, 11 * p_step) if p_step is not None else None}"
    )
    verdicts = {}
    names = {
        "A": "A — S12: усі FVG, рівень 0.5",
        "B": "B — S28: валідні OB (усі три прапорці), рівень — тіло",
        "C": "C — валідні брейкери (обидва прапорці), рівень — тіло",
    }
    for key, v in PRIMARY.items():
        vd, info = verdict_for(V, key, VID[v], p_adj)
        verdicts[key] = vd
        b, bo = info["b"], info["bo"]
        s, d = b["s"], b["d"]
        emit(f"\n### {names[key]}")
        emit(
            f"- подій IS {b['n_ev']}, виконано {s['n']} ({b['fill_e']:.1%}; P_rand {b['fill_p']:.1%}), дат {s['days']} (поріг {MIN_DATES}); дотик за 5 барів {b['t5_e']:.1%} проти {b['t5_p']:.1%}"
        )
        emit(
            f"- E[R] gross {b['gross_e']:+.3f}; **E[R] net {s['mean']:+.3f} {ci_str(s['ci'])}**, p {s['p']}; за тижнями {ci_str(info['wk_s']['ci'])}"
        )
        emit(
            f"- P_rand: E[R] net {b['sp']['mean']:+.3f} (n {b['sp']['n']}); **Δ {d['diff']:+.3f} {ci_str(d['ci'])}**, p {d['p']}; за тижнями {ci_str(info['wk_d']['ci'])}; MDE = 2.8·SE = {info['mde']:.3f} (ворота {MDE_GATE})"
        )
        emit(
            f"- реакція «+1 ATR раніше −1 ATR»: {b['react_e']:.1%} проти {b['react_p']:.1%}; медіана MFE {b['mfe_e']:.2f}R проти {b['mfe_p']:.2f}R"
        )
        emit(
            f"- OOS: подій {bo['n_ev']}, угод {bo['s']['n']}, дат {bo['s']['days']}; E[R] net {bo['s']['mean']:+.3f} {ci_str(bo['s']['ci'])}, p {bo['s']['p']}; Δ {bo['d']['diff']:+.3f} {ci_str(bo['d']['ci'])}"
        )
        emit(f"- **вердикт {key} за реєстрацією: {vd}**")
    if any(v == "confirmed" for v in verdicts.values()):
        step = "confirmed"
    elif all(v == "rejected" for v in verdicts.values()):
        step = "rejected"
    else:
        step = "inconclusive"
    emit(f"\n- **вердикт кроку за реєстрацією: {step}** ({verdicts})")
    adj_in = holm(ps) if ps else []
    emit(f"- Holm усередині кроку (A, B, C): {[round(x, 4) for x in adj_in]}")
    magnet(V)

    # grid
    emit("\n## Сітка й варіанти (усі; IS, крім двох останніх колонок)")
    emit(GRID_HDR)
    sec_p: list[tuple[str, float]] = []
    for vid, v in enumerate(VARIANTS):
        b, bo = var_block(V, vid, 0.0), var_block(V, vid, 1.0)
        tag = " **(основна)**" if v in PRIMARY.values() else ""
        emit(grid_line(vlabel(v) + tag, b, bo))
        if v not in PRIMARY.values() and b["d"]["p"] is not None:
            sec_p.append((vlabel(v), b["d"]["p"]))
    emit("\n| варіант | сире p (Δ > 0) | Holm усередині кроку (другорядні) |\n|---|---|---|")
    for (lab, pr), pa in zip(sec_p, holm([p for _, p in sec_p]), strict=True):
        emit(f"| {lab} | {pr:.4f} | {pa:.4f} |")

    # P_imp
    emit("\n## FVG проти «того самого імпульсу без гепу» (P_imp, другорядне)")
    emit(GRID_HDR)
    for v in (PRIMARY["A"], ("1d", "fvg", "all", "mid", "atr")):
        b, bo = var_block(V, VID[v], 0.0, ptype=2), var_block(V, VID[v], 1.0, ptype=2)
        emit(grid_line(vlabel(v) + " проти P_imp", b, bo))

    # H4 basics: valid vs rest
    emit("\n## H4 basics: валідні OB проти решти (4h, рівень — тіло)")
    for oos in (0.0, 1.0):
        bv = var_block(V, VID[PRIMARY["B"]], oos, 5000)
        rest = V.m(var=OB_ALL, kind=0, oos=oos) & (R["filled"] == 1) & (R["valid_ok"] == 0)
        dd = bdiff(V.net[bv["evf"]], R["day"][bv["evf"]], V.net[rest], R["day"][rest], 5000)
        sr = summ(V.net[rest], R["day"][rest])
        emit(
            f"- {'OOS' if oos else 'IS'}: валідні {bv['s']['mean']:+.3f} (n {bv['s']['n']}), решта {sr['mean']:+.3f} {ci_str(sr['ci'])} (n {sr['n']}); Δ {dd['diff']:+.3f} {ci_str(dd['ci'])}, p {dd['p']} (теза: ≥ +0.10R)"
        )

    # robustness of the primaries
    emit("\n## Стійкість основних метрик (IS, якщо не сказано інше)")
    always = {i for i, s in enumerate(syms) if s in s1.ALWAYS}
    btc_eth = np.isin(R["sym"], list(always))
    years = (R["day"].astype(np.int64) * DAY_MS).astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970
    for key, v in PRIMARY.items():
        vid = VID[v]
        emit(f"\n### {names[key]}")
        emit("| зріз | n угод | дат | E[R] net [CI] | P_rand | Δ [CI] |\n|---|---|---|---|---|---|")

        def cut(lab: str, mask: np.ndarray, oos: float | None = 0.0, vid: int = vid) -> None:
            b = var_block(V, vid, oos, 2000, mask=mask)
            emit(
                f"| {lab} | {b['s']['n']} | {b['s']['days']} | {b['s']['mean']:+.3f} {ci_str(b['s']['ci'])} | {b['sp']['mean']:+.3f} | {b['d']['diff']:+.3f} {ci_str(b['d']['ci'])} |"
            )

        cut("лонги", R["side"] == 1)
        cut("шорти", R["side"] == -1)
        cut("BTC+ETH", btc_eth)
        cut("альти", ~btc_eth)
        cut("лонги OOS", R["side"] == 1, 1.0)
        cut("шорти OOS", R["side"] == -1, 1.0)
        for y in range(2020, 2027):
            cut(f"рік рішення {y} (IS і OOS разом)", years == y, None)
        b = var_block(V, vid, 0.0, 2000)
        evf, prf = b["evf"], b["prf"]
        for k in (5, 10, 20):
            vals, dd_, _ = without_top_days(V.net[evf].tolist(), R["day"][evf].tolist(), k)
            st = summ(np.asarray(vals), np.asarray(dd_), 2000)
            emit(f"| без {k} найкращих дат | {st['n']} | {st['days']} | {st['mean']:+.3f} {ci_str(st['ci'])} | — | — |")
        for lab, f in (("витрати ×2", 2.0), ("витрати ×3", 3.0), ("витрати ×0.5", 0.5)):
            e_ = R["r_gross"][evf] - f * R["cost_r"][evf]
            p_ = R["r_gross"][prf] - f * R["cost_r"][prf]
            st = summ(e_, R["day"][evf], 2000)
            dd = bdiff(e_, R["day"][evf], p_, R["day"][prf], 2000)
            emit(
                f"| {lab} | {st['n']} | {st['days']} | {st['mean']:+.3f} {ci_str(st['ci'])} | {p_.mean():+.3f} | {dd['diff']:+.3f} {ci_str(dd['ci'])} |"
            )
        tk_e = R["r_gross"][evf] - MARKET_SIDE * (np.abs(R["fill"][evf]) + np.abs(R["exit"][evf])) / R["risk"][evf]
        tk_p = R["r_gross"][prf] - MARKET_SIDE * (np.abs(R["fill"][prf]) + np.abs(R["exit"][prf])) / R["risk"][prf]
        st = summ(tk_e, R["day"][evf], 2000)
        dd = bdiff(tk_e, R["day"][evf], tk_p, R["day"][prf], 2000)
        emit(
            f"| усе тейкером 0.07 % | {st['n']} | {st['days']} | {st['mean']:+.3f} {ci_str(st['ci'])} | {tk_p.mean():+.3f} | {dd['diff']:+.3f} {ci_str(dd['ci'])} |"
        )
        pe = b["ev"] & (R["pen_filled"] == 1)
        pp = b["pr"] & (R["pen_filled"] == 1)
        st = summ(R["pen_r_net"][pe], R["day"][pe], 2000)
        dd = bdiff(R["pen_r_net"][pe], R["day"][pe], R["pen_r_net"][pp], R["day"][pp], 2000)
        emit(
            f"| виконання лише при проколі ≥ {PEN:g}·ATR (частка {R['pen_filled'][b['ev']].mean():.1%} / {R['pen_filled'][b['pr']].mean():.1%}) | {st['n']} | {st['days']} | {st['mean']:+.3f} {ci_str(st['ci'])} | {np.nanmean(R['pen_r_net'][pp]):+.3f} | {dd['diff']:+.3f} {ci_str(dd['ci'])} |"
        )
        ex = Counter(EXITS[int(x)] for x in R["exit_kind"][evf])
        exp_ = Counter(EXITS[int(x)] for x in R["exit_kind"][prf])
        tot, totp = max(1, sum(ex.values())), max(1, sum(exp_.values()))
        emit(f"\n- виходи (подія / P_rand): {', '.join(f'{k} {ex[k] / tot:.1%} / {exp_[k] / totp:.1%}' for k in EXITS[1:])}")
        emit(
            f"- медіана ризику угоди (L − стоп) / L: {np.median(R['risk'][evf] / np.abs(R['fill'][evf])):.2%}; середні витрати {R['cost_r'][evf].mean():.3f}R"
        )
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step4_fvg_ob")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "count", "report", "analyse"), required=True)
    ap.add_argument("--symbols", type=int, help="run only the first N symbols (timing sample)")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    out_dir = a.cache_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "count":
        stage_count(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.symbols)
    elif a.stage == "analyse":
        stage_analyse(a.s14_cache.expanduser(), out_dir)
    else:
        stage_report(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.symbols, out_dir)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
