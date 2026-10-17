"""Course setups, step 5 (IDEA-20261016-course-setups-crypto): S17 — the retest of an impulsively broken POB (the
last candle before a >= 10 % impulse, later closed through), traded with a resting limit at the POB for 90 days, stop
behind the POB, target 2R, against a random level at the same distance.

Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 5". Stages:
  uv run python scripts/verify_course_step5_s17.py --stage prereg
  uv run python scripts/verify_course_step5_s17.py --stage count  --out ~/.cache/tradedesk-agents/course_step5_s17/count.md
  uv run python scripts/verify_course_step5_s17.py --stage report --out ~/.cache/tradedesk-agents/course_step5_s17/report.md
  uv run python scripts/verify_course_step5_s17.py --stage analyse --out ~/.cache/tradedesk-agents/course_step5_s17/analyse.md
``count`` (stage 0) counts eligible events without any trade outcome; ``analyse`` recomputes the statistics from the
saved rows.npz. ``--symbols N`` runs the first N symbols only (timing). No network: the 1h bars and the universe come
from the step-1 cache (--s14-cache, filled from data.binance.vision via rt.binance_vision); listings from the
funding-crowding cache (--data-dir). Per-row results go to --cache-dir/rows.npz. No keys, no orders.
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
from typing import Any, NamedTuple

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders, universe)
import verify_course_step3_s18 as s3  # noqa: E402  (membership intervals)
import verify_course_step4_fvg_ob as s4  # noqa: E402  (bootstrap summaries)

from tradedesk.analytics.course_fvg_ob import DAY_MS, HOUR_MS, MARKET_SIDE, limit_long  # noqa: E402
from tradedesk.analytics.course_pob import pob_events, pob_stop  # noqa: E402
from tradedesk.analytics.course_s14_ema import atr, ema  # noqa: E402
from tradedesk.analytics.course_s18_trend import pos_from_seg, resample_partial, segments  # noqa: E402
from tradedesk.analytics.liquidation_flush import holm, without_top_days  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
SEED = 20261017
PER_EVENT = 3
WARMUP = 100
LOOK, ATR_MULT, IMP_ATR = 5, 3.0, 1.0
BREAK_DAYS, HOLD_BARS, PEN = 180, 20, 0.05
RAND_WIN_MS = 60 * DAY_MS
START_MS, OOS_MS = s1.START_MS, s1.OOS_MS
END_MS = s1.ms("2026-06-01T00:00")
TF_HOURS = {"1d": 24, "12h": 12, "4h": 4}
STEP_P = (0.985, 0.280, 0.448, 0.038)  # raw primary p of steps 1-4 (Holm family of the card)
STEP4_ADJ = 0.154  # step 4's Holm-adjusted p (reference variant of the family only)
MIN_DATES, MDE_GATE, MIN_EFFECT, OOS_MIN_DATES = 100, 0.20, 0.10, 20


class Var(NamedTuple):
    tf: str
    drop: float = 0.10
    level: str = "wick"
    stop: str = "pob"
    target: float = 2.0
    brk: str = "imp"  # imp | non | any
    confirm: bool = False
    wait_days: int = 90
    ema: bool = False


PRIMARY = {"1D": Var("1d"), "12h": Var("12h")}
VARIANTS: list[Var] = []
for _tf in ("1d", "12h"):
    VARIANTS += [
        Var(_tf),
        Var(_tf, drop=0.07),
        Var(_tf, drop=0.15),
        Var(_tf, level="body"),
        Var(_tf, level="mid"),
        Var(_tf, stop="atr"),
        Var(_tf, target=3.0),
        Var(_tf, brk="non"),
        Var(_tf, brk="any"),
        Var(_tf, confirm=True),
        Var(_tf, wait_days=30),
        Var(_tf, ema=True),
    ]
VARIANTS += [Var("4h"), Var("4h", brk="non")]
VID = {v: k for k, v in enumerate(VARIANTS)}
PRIMARY_IDS = {VID[v] for v in PRIMARY.values()}
EXITS = ["none", "stop", "stop_gap", "target", "time", "data_end"]
COLS = "sym var kind side day oos filled r_gross cost_r exit_kind fill exit risk wait_h pen_filled pen_r_net arm_day".split()


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def vlabel(v: Var) -> str:
    base = Var(v.tf)
    diff = [f"{k}={getattr(v, k)}" for k in Var._fields[1:] if getattr(v, k) != getattr(base, k)]
    return f"{v.tf} " + (", ".join(diff) if diff else "основні правила")


def day_str(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    raw = CARD.read_bytes()
    t = raw.decode()
    a = t.index("## Попередня реєстрація — крок 5")
    b = t.index("\n## ", a + 1)
    sec = t[a:b].encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()  # = git hash-object
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)} card_blob={blob}")


# ---------------------------------------------------------------- per-symbol run


def trade_fields(tr: Any, start_ms: int) -> dict[str, float]:
    out = {"filled": float(tr.filled), "risk": tr.risk}
    if tr.filled:
        out.update(
            r_gross=tr.r_gross,
            cost_r=tr.cost_r,
            exit_kind=float(EXITS.index(tr.exit_kind)),
            fill=tr.fill,
            exit=tr.exit,
            wait_h=(tr.touch_ms - start_ms) / HOUR_MS,
        )
    return out


def run_symbol(si: int, h1: Any, iv: np.ndarray | None, rng: np.random.Generator, outcomes: bool, rows: s4.Rows, why: Counter[str]) -> None:
    seg = segments(h1.t)
    for tf, hrs in TF_HOURS.items():
        bar_ms = hrs * HOUR_MS
        bt, segt = resample_partial(h1, seg, hrs, hrs)
        if len(bt) < WARMUP + 30:
            continue
        post = pos_from_seg(segt)
        at = atr(bt, 14, post)
        close = bt.t + bar_ms
        ok = s3.in_intervals(close, iv) & (close >= START_MS) & (close < END_MS) & (post >= WARMUP) & np.isfinite(at)
        seg_lo = np.searchsorted(segt, segt, side="left")
        seg_hi = np.searchsorted(segt, segt, side="right")
        vars_tf = [(k, v) for k, v in enumerate(VARIANTS) if v.tf == tf]
        brk_bars = BREAK_DAYS * 24 // hrs
        for side in (1, -1):
            B = bt if side == 1 else bt.mirrored()
            P = h1 if side == 1 else h1.mirrored()
            with np.errstate(invalid="ignore"):
                e21 = ema(B.c, 21, post)
            evs = {
                d: pob_events(B, post, at, drop=d, look=LOOK, atr_mult=ATR_MULT, break_bars=brk_bars, imp_atr=IMP_ATR, warmup=WARMUP)
                for d in sorted({v.drop for _, v in vars_tf})
            }
            for d, ev in evs.items():
                if d == 0.10:
                    why[f"{tf}:pob"] += len(ev)
                    why[f"{tf}:pob_broken"] += sum(e.k > 0 for e in ev)
                    why[f"{tf}:pob_broken_imp"] += sum(e.k > 0 and e.impulsive for e in ev)
            for vid, v in vars_tf:
                for e in evs[v.drop]:
                    if e.k < 0:
                        continue
                    if (v.brk == "imp" and not e.impulsive) or (v.brk == "non" and e.impulsive):
                        continue
                    arm = e.k
                    level = e.levels[v.level]
                    if v.confirm:
                        if not e.confirm:
                            why[f"{vid}:no_confirm"] += 1
                            continue
                        arm = e.k + 1
                        if not B.low[arm] > level:
                            why[f"{vid}:touched_before_arm"] += 1
                            continue
                    if arm >= len(B) or not ok[arm] or post[e.k] < WARMUP:
                        why[f"{vid}:not_eligible"] += 1
                        continue
                    A = float(at[e.k])
                    stop = pob_stop(level, e.low, A, v.stop)
                    if v.ema and not (stop <= e21[e.k] <= level + 0.5 * A):
                        why[f"{vid}:ema_far"] += 1
                        continue
                    dist = (float(B.c[arm]) - level) / A
                    if dist < 0:
                        why[f"{vid}:price_inside"] += 1
                        continue
                    rmult = (level - stop) / A
                    start = int(close[arm])
                    day = start // DAY_MS
                    base = {"sym": si, "var": vid, "side": side, "day": day, "oos": float(start >= OOS_MS), "arm_day": day}
                    if not outcomes:
                        why[f"{vid}:ok"] += 1
                        rows.add(**base, kind=0)
                        continue
                    wait, hold = v.wait_days * DAY_MS, HOLD_BARS * bar_ms
                    tr = limit_long(P, start, wait, hold, level, stop, level + v.target * (level - stop))
                    if tr is None:
                        why[f"{vid}:no_path"] += 1
                        continue
                    why[f"{vid}:ok"] += 1
                    extra: dict[str, float] = {}
                    if vid in PRIMARY_IDS:
                        tp = limit_long(P, start, wait, hold, level, stop, level + v.target * (level - stop), pen=PEN * A)
                        extra["pen_filled"] = float(bool(tp and tp.filled))
                        extra["pen_r_net"] = tp.r_net if tp is not None and tp.filled else np.nan
                    rows.add(**base, kind=0, **trade_fields(tr, start), **extra)
                    # placebo P_rand: random bars of the same segment within 60 days, same distance and stop in ATR
                    lo = max(int(np.searchsorted(close, start - RAND_WIN_MS, side="left")), int(seg_lo[arm]))
                    hi = min(int(np.searchsorted(close, start + RAND_WIN_MS, side="right")), int(seg_hi[arm]))
                    cand = np.flatnonzero(ok[lo:hi]) + lo
                    cand = cand[cand != arm]
                    if cand.size == 0:
                        why[f"{vid}:rand_empty"] += 1
                        continue
                    for j in rng.choice(cand, size=min(PER_EVENT, cand.size), replace=False):
                        j = int(j)
                        aj = float(at[j])
                        lj = float(B.c[j]) - dist * aj
                        sj = lj - rmult * aj
                        ps = int(close[j])
                        pt = limit_long(P, ps, wait, hold, lj, sj, lj + v.target * (lj - sj))
                        if pt is None:
                            continue
                        pextra: dict[str, float] = {}
                        if vid in PRIMARY_IDS:
                            pp = limit_long(P, ps, wait, hold, lj, sj, lj + v.target * (lj - sj), pen=PEN * aj)
                            pextra["pen_filled"] = float(bool(pp and pp.filled))
                            pextra["pen_r_net"] = pp.r_net if pp is not None and pp.filled else np.nan
                        rows.add(**{**base, "arm_day": ps // DAY_MS}, kind=1, **trade_fields(pt, ps), **pextra)


class Rows(s4.Rows):
    def __init__(self) -> None:
        self.chunks: list[dict[str, np.ndarray]] = []
        self.cur: dict[str, list[float]] = {c: [] for c in COLS}

    def add(self, **kw: float) -> None:
        for c in COLS:
            self.cur[c].append(kw.get(c, np.nan))

    def flush(self) -> None:
        if self.cur["sym"]:
            self.chunks.append({c: np.asarray(v, dtype=np.float64) for c, v in self.cur.items()})
        self.cur = {c: [] for c in COLS}

    def table(self) -> dict[str, np.ndarray]:
        self.flush()
        if not self.chunks:
            return {c: np.zeros(0) for c in COLS}
        out = {c: np.concatenate([ch.pop(c) for ch in self.chunks]) for c in COLS}
        self.chunks = []
        return out


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
        run_symbol(n, h1, ivs.get(s), rng, outcomes, rows, why)
        rows.flush()
        del h1
        if (n + 1) % 20 == 0 or n + 1 == len(syms):
            emit(f"  [run] {n + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rows {len(rows)}, rss {rss_mb():.0f} MB")
    return rows.table(), why, syms


# ---------------------------------------------------------------- sanity


def sanity(cache: Path, data_dir: Path, outcomes: bool) -> None:
    emit("## Санітарні перевірки")
    for s, tf, a, b in (("BTCUSDT", "1d", "2023-07-15", "2023-09-15"), ("ETHUSDT", "12h", "2024-07-20", "2024-08-20")):
        h1 = s1.load_1h(cache, data_dir, s)
        seg = segments(h1.t)
        hrs = TF_HOURS[tf]
        bt, segt = resample_partial(h1, seg, hrs, hrs)
        post = pos_from_seg(segt)
        at = atr(bt, 14, post)
        if s == "BTCUSDT":
            k = int(np.searchsorted(bt.t, s1.ms("2024-12-31T00:00")))
            emit(
                f"- BTCUSDT 1D: закриття 2024-12-31 = {bt.c[k]:g} (еталон кроків 1–4: 93548.9); медіана ATR(1D)/ціна {np.nanmedian(at / bt.c):.2%}"
            )
        lo_ms, hi_ms = s1.ms(f"{a}T00:00"), s1.ms(f"{b}T00:00")
        for side in (1, -1):
            B = bt if side == 1 else bt.mirrored()
            for e in pob_events(B, post, at, drop=0.10, look=LOOK, atr_mult=ATR_MULT, break_bars=BREAK_DAYS * 24 // hrs, warmup=WARMUP):
                if not lo_ms <= bt.t[e.p] < hi_ms:
                    continue
                sgn = "лонг-рамка (POB перед падінням)" if side == 1 else "шорт-рамка (POB перед ростом)"
                brk = f"пробій {day_str(int(bt.t[e.k]))}, імпульсний {e.impulsive}" if e.k > 0 else "пробою за 180 діб немає"
                line = f"- {s} {tf}, {sgn}: POB {day_str(int(bt.t[e.p]))} (рівень {abs(e.high):g}), імпульс {e.drop:.1%} до {day_str(int(bt.t[e.q]))}; {brk}"
                if outcomes and e.k > 0:
                    P = h1 if side == 1 else h1.mirrored()
                    A = float(at[e.k])
                    st = pob_stop(e.high, e.low, A)
                    tr = limit_long(
                        P, int(bt.t[e.k]) + hrs * HOUR_MS, 90 * DAY_MS, HOLD_BARS * hrs * HOUR_MS, e.high, st, e.high + 2 * (e.high - st)
                    )
                    if tr is not None:
                        line += f"; ретест: {'виконано ' + day_str(tr.touch_ms) + f', вихід {tr.exit_kind}, R net {tr.r_net:+.2f}' if tr.filled else 'за 90 діб не було'}"
                emit(line)
    emit(f"- пікова RSS {rss_mb():.0f} MB")


# ---------------------------------------------------------------- stats


def month_of(day: np.ndarray) -> np.ndarray:
    return (day.astype(np.int64) * DAY_MS).astype("datetime64[ms]").astype("datetime64[M]").astype(np.int64)


def block(
    R: dict[str, np.ndarray], net: np.ndarray, vid: int, oos: float | None, n_boot: int = 5000, mask: np.ndarray | None = None
) -> dict[str, Any]:
    m = (R["var"] == vid) if mask is None else (R["var"] == vid) & mask
    if oos is not None:
        m &= R["oos"] == oos
    ev, pr = m & (R["kind"] == 0), m & (R["kind"] == 1)
    evf, prf = ev & (R["filled"] == 1), pr & (R["filled"] == 1)
    per_e = np.where(R["filled"][ev] == 1, net[ev], 0.0)
    per_p = np.where(R["filled"][pr] == 1, net[pr], 0.0)
    return {
        "n_ev": int(ev.sum()),
        "ev_days": int(np.unique(R["day"][ev]).size),
        "fill_e": float(R["filled"][ev].mean()) if ev.any() else float("nan"),
        "fill_p": float(R["filled"][pr].mean()) if pr.any() else float("nan"),
        "s": s4.summ(net[evf], R["day"][evf], n_boot),
        "sp": s4.summ(net[prf], R["day"][prf], 1000),
        "d": s4.bdiff(net[evf], R["day"][evf], net[prf], R["day"][prf], n_boot),
        "per_e": float(per_e.mean()) if per_e.size else float("nan"),
        "per_p": float(per_p.mean()) if per_p.size else float("nan"),
        "per_d": s4.bdiff(per_e, R["day"][ev], per_p, R["day"][pr], min(n_boot, 2000)),
        "gross_e": float(R["r_gross"][evf].mean()) if evf.any() else float("nan"),
        "ev": ev,
        "pr": pr,
        "evf": evf,
        "prf": prf,
    }


GRID_HDR = (
    "| варіант | подій | дат подій | виконано (подія / P_rand) | n угод | дат | E[R] gross | E[R] net [CI] | P_rand E[R] net | Δ [CI] | p Δ | E[R] на подію (невиконані = 0): подія / P_rand | OOS: угод, E[R] net, Δ |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def grid_line(label: str, b: dict[str, Any], bo: dict[str, Any]) -> str:
    s, d = b["s"], b["d"]
    p = "—" if d["p"] is None else f"{d['p']:.4f}"
    return (
        f"| {label} | {b['n_ev']} | {b['ev_days']} | {b['fill_e']:.1%} / {b['fill_p']:.1%} | {s['n']} | {s['days']} | {b['gross_e']:+.3f} | "
        f"{s['mean']:+.3f} {s4.ci_str(s['ci'])} | {b['sp']['mean']:+.3f} | {d['diff']:+.3f} {s4.ci_str(d['ci'])} | {p} | "
        f"{b['per_e']:+.3f} / {b['per_p']:+.3f} | {bo['s']['n']}, {bo['s']['mean']:+.3f}, {bo['d']['diff']:+.3f} |"
    )


def verdict_for(R: dict[str, np.ndarray], net: np.ndarray, vid: int, p_adj: float | None) -> tuple[str, dict[str, Any]]:
    b = block(R, net, vid, 0.0, 10000)
    bo = block(R, net, vid, 1.0, 10000)
    evf, prf = b["evf"], b["prf"]
    mo_s = s4.summ(net[evf], month_of(R["day"][evf]), 10000)
    mo_d = s4.bdiff(net[evf], month_of(R["day"][evf]), net[prf], month_of(R["day"][prf]), 10000)
    s, d = b["s"], b["d"]
    mde = 2.8 * (d["se"] if d["se"] is not None else float("inf"))
    info = {"b": b, "bo": bo, "mo_s": mo_s, "mo_d": mo_d, "mde": mde}
    if s["days"] < MIN_DATES or mde > MDE_GATE:
        return "inconclusive (замало дат або потужності)", info
    if s["ci"] is None or s["ci"][0] <= 0 or d["diff"] < MIN_EFFECT:
        return "rejected", info
    if (
        d["ci"] is not None
        and d["ci"][0] > 0
        and mo_s["ci"] is not None
        and mo_s["ci"][0] > 0
        and mo_d["ci"] is not None
        and mo_d["ci"][0] > 0
        and p_adj is not None
        and p_adj < 0.05
        and bo["s"]["days"] >= OOS_MIN_DATES
        and bo["s"]["mean"] > 0
        and bo["s"]["p"] is not None
        and bo["s"]["p"] < 0.05
        and bo["d"]["diff"] > 0
    ):
        return "confirmed", info
    return "inconclusive (позитив в IS не пройшов CI за місяцями, Holm чи OOS)", info


def analyse(R: dict[str, np.ndarray], syms: list[str]) -> None:
    net = R["r_gross"] - R["cost_r"]
    raw_p: dict[str, float | None] = {}
    for key, v in PRIMARY.items():
        raw_p[key] = block(R, net, VID[v], 0.0, 10000)["d"]["p"]
    ps = [p for p in raw_p.values() if p is not None]
    p_step = min(1.0, 2 * min(ps)) if ps else None
    p_adj = holm([*STEP_P, p_step])[4] if p_step is not None else None
    p_adj_ref = holm([*STEP_P[:3], STEP4_ADJ, p_step])[4] if p_step is not None else None
    emit("\n## Основні метрики (1D і 12h, обидва боки, IS) і ворота")
    emit(
        f"- сирі p (Δ > 0): 1D {raw_p['1D']}, 12h {raw_p['12h']}; p кроку = min(1, 2·min) = {p_step}; "
        f"Holm (m = 5: 0.985, 0.280, 0.448, 0.038, крок 5) = {p_adj}; з 0.154 замість 0.038 — {p_adj_ref}; ×11 — {min(1.0, 11 * p_step) if p_step is not None else None}"
    )
    verdicts = {}
    for key, v in PRIMARY.items():
        vd, info = verdict_for(R, net, VID[v], p_adj)
        verdicts[key] = vd
        b, bo = info["b"], info["bo"]
        s, d = b["s"], b["d"]
        emit(f"\n### {key}: POB перед імпульсом ≥ 10 %, імпульсний пробій, ліміт на high POB, 90 діб, стоп за POB, ціль 2R")
        emit(
            f"- подій IS {b['n_ev']} на {b['ev_days']} датах; виконано {s['n']} ({b['fill_e']:.1%}; P_rand {b['fill_p']:.1%}), дат з виконаними {s['days']} (поріг {MIN_DATES})"
        )
        emit(
            f"- E[R] gross {b['gross_e']:+.3f}; **E[R] net {s['mean']:+.3f} {s4.ci_str(s['ci'])}**, p {s['p']}; за місяцями {s4.ci_str(info['mo_s']['ci'])}"
        )
        emit(
            f"- P_rand: E[R] net {b['sp']['mean']:+.3f} (n {b['sp']['n']}); **Δ {d['diff']:+.3f} {s4.ci_str(d['ci'])}**, p {d['p']}; за місяцями {s4.ci_str(info['mo_d']['ci'])}; MDE = 2.8·SE = {info['mde']:.3f} (ворота {MDE_GATE})"
        )
        emit(
            f"- на подію (невиконані = 0R): {b['per_e']:+.3f} проти {b['per_p']:+.3f}; Δ {b['per_d']['diff']:+.3f} {s4.ci_str(b['per_d']['ci'])}"
        )
        emit(
            f"- OOS: подій {bo['n_ev']}, угод {bo['s']['n']}, дат {bo['s']['days']}; E[R] net {bo['s']['mean']:+.3f} {s4.ci_str(bo['s']['ci'])}, p {bo['s']['p']}; Δ {bo['d']['diff']:+.3f} {s4.ci_str(bo['d']['ci'])}"
        )
        emit(f"- **вердикт {key} за реєстрацією: {vd}**")
    if any(v == "confirmed" for v in verdicts.values()):
        step = "confirmed"
    elif all(v == "rejected" for v in verdicts.values()):
        step = "rejected"
    else:
        step = "inconclusive"
    emit(f"\n- **вердикт кроку за реєстрацією: {step}** ({verdicts})")
    emit(f"- Holm усередині кроку (1D, 12h): {[round(x, 4) for x in holm(ps)] if ps else []}")

    emit("\n## Сітка й варіанти (усі; IS, крім останньої колонки)")
    emit(GRID_HDR)
    sec_p: list[tuple[str, float]] = []
    for vid, v in enumerate(VARIANTS):
        b, bo = block(R, net, vid, 0.0), block(R, net, vid, 1.0)
        tag = " **(основна)**" if vid in PRIMARY_IDS else ""
        emit(grid_line(vlabel(v) + tag, b, bo))
        if vid not in PRIMARY_IDS and b["d"]["p"] is not None:
            sec_p.append((vlabel(v), b["d"]["p"]))
    emit("\n| варіант | сире p (Δ > 0) | Holm усередині кроку (другорядні) |\n|---|---|---|")
    for (lab, pr), pa in zip(sec_p, holm([p for _, p in sec_p]), strict=True):
        emit(f"| {lab} | {pr:.4f} | {pa:.4f} |")

    emit("\n## H3 «імпульсний пробій кращий»: імпульсні − неімпульсні пробої (E[R] net виконаних)")
    for tf in ("1d", "12h", "4h"):
        for oos in (0.0, 1.0):
            a = block(R, net, VID[Var(tf)], oos, 2000)
            n_ = block(R, net, VID[Var(tf, brk="non")], oos, 2000)
            dd = s4.bdiff(net[a["evf"]], R["day"][a["evf"]], net[n_["evf"]], R["day"][n_["evf"]], 5000)
            emit(
                f"- {tf} {'OOS' if oos else 'IS'}: імпульсні {a['s']['mean']:+.3f} (n {a['s']['n']}), неімпульсні {n_['s']['mean']:+.3f} (n {n_['s']['n']}); Δ {dd['diff']:+.3f} {s4.ci_str(dd['ci'])}, p {dd['p']}"
            )

    emit("\n## Стійкість основних метрик (IS, якщо не сказано інше)")
    always = {i for i, s in enumerate(syms) if s in s1.ALWAYS}
    btc_eth = np.isin(R["sym"], list(always))
    years = (R["day"].astype(np.int64) * DAY_MS).astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970
    for key, v in PRIMARY.items():
        vid = VID[v]
        emit(f"\n### {key}")
        emit("| зріз | n угод | дат | E[R] net [CI] | P_rand | Δ [CI] |\n|---|---|---|---|---|---|")

        def cut(lab: str, mask: np.ndarray, oos: float | None = 0.0, vid: int = vid) -> None:
            b = block(R, net, vid, oos, 2000, mask=mask)
            emit(
                f"| {lab} | {b['s']['n']} | {b['s']['days']} | {b['s']['mean']:+.3f} {s4.ci_str(b['s']['ci'])} | {b['sp']['mean']:+.3f} | {b['d']['diff']:+.3f} {s4.ci_str(b['d']['ci'])} |"
            )

        cut("лонги (POB перед падінням)", R["side"] == 1)
        cut("шорти (POB перед ростом)", R["side"] == -1)
        cut("BTC+ETH", btc_eth)
        cut("альти", ~btc_eth)
        cut("лонги OOS", R["side"] == 1, 1.0)
        cut("шорти OOS", R["side"] == -1, 1.0)
        for y in range(2020, 2027):
            cut(f"рік пробою {y} (IS і OOS разом)", years == y, None)
        b = block(R, net, vid, 0.0, 2000)
        evf, prf = b["evf"], b["prf"]
        for k in (5, 10, 20):
            vals, dd_, _ = without_top_days(net[evf].tolist(), R["day"][evf].tolist(), k)
            st = s4.summ(np.asarray(vals), np.asarray(dd_), 2000)
            emit(f"| без {k} найкращих дат | {st['n']} | {st['days']} | {st['mean']:+.3f} {s4.ci_str(st['ci'])} | — | — |")
        for lab, f in (("витрати ×2", 2.0), ("витрати ×3", 3.0), ("витрати ×0.5", 0.5)):
            e_ = R["r_gross"][evf] - f * R["cost_r"][evf]
            p_ = R["r_gross"][prf] - f * R["cost_r"][prf]
            st = s4.summ(e_, R["day"][evf], 2000)
            dd = s4.bdiff(e_, R["day"][evf], p_, R["day"][prf], 2000)
            emit(
                f"| {lab} | {st['n']} | {st['days']} | {st['mean']:+.3f} {s4.ci_str(st['ci'])} | {p_.mean():+.3f} | {dd['diff']:+.3f} {s4.ci_str(dd['ci'])} |"
            )
        tk_e = R["r_gross"][evf] - MARKET_SIDE * (np.abs(R["fill"][evf]) + np.abs(R["exit"][evf])) / R["risk"][evf]
        tk_p = R["r_gross"][prf] - MARKET_SIDE * (np.abs(R["fill"][prf]) + np.abs(R["exit"][prf])) / R["risk"][prf]
        st = s4.summ(tk_e, R["day"][evf], 2000)
        dd = s4.bdiff(tk_e, R["day"][evf], tk_p, R["day"][prf], 2000)
        emit(
            f"| усе тейкером 0.07 % | {st['n']} | {st['days']} | {st['mean']:+.3f} {s4.ci_str(st['ci'])} | {tk_p.mean():+.3f} | {dd['diff']:+.3f} {s4.ci_str(dd['ci'])} |"
        )
        pe = b["ev"] & (R["pen_filled"] == 1)
        pp = b["pr"] & (R["pen_filled"] == 1)
        st = s4.summ(R["pen_r_net"][pe], R["day"][pe], 2000)
        dd = s4.bdiff(R["pen_r_net"][pe], R["day"][pe], R["pen_r_net"][pp], R["day"][pp], 2000)
        emit(
            f"| виконання лише при проколі ≥ {PEN:g}·ATR (частка {np.nanmean(R['pen_filled'][b['ev']]):.1%} / {np.nanmean(R['pen_filled'][b['pr']]):.1%}) | {st['n']} | {st['days']} | {st['mean']:+.3f} {s4.ci_str(st['ci'])} | {np.nanmean(R['pen_r_net'][pp]):+.3f} | {dd['diff']:+.3f} {s4.ci_str(dd['ci'])} |"
        )
        ex = Counter(EXITS[int(x)] for x in R["exit_kind"][evf])
        exp_ = Counter(EXITS[int(x)] for x in R["exit_kind"][prf])
        tot, totp = max(1, sum(ex.values())), max(1, sum(exp_.values()))
        emit(f"\n- виходи (подія / P_rand): {', '.join(f'{k} {ex[k] / tot:.1%} / {exp_[k] / totp:.1%}' for k in EXITS[1:])}")
        wq = np.quantile(R["wait_h"][evf] / 24, [0.25, 0.5, 0.75]) if evf.any() else [np.nan] * 3
        wp = np.quantile(R["wait_h"][prf] / 24, [0.25, 0.5, 0.75]) if prf.any() else [np.nan] * 3
        emit(
            f"- діб до виконання, квартилі (подія / P_rand): {wq[0]:.1f} / {wq[1]:.1f} / {wq[2]:.1f} проти {wp[0]:.1f} / {wp[1]:.1f} / {wp[2]:.1f}"
        )
        emit(
            f"- медіана ризику (L − стоп) / L: {np.median(R['risk'][evf] / np.abs(R['fill'][evf])):.2%} (P_rand {np.median(R['risk'][prf] / np.abs(R['fill'][prf])):.2%}); середні витрати {R['cost_r'][evf].mean():.3f}R"
        )
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


# ---------------------------------------------------------------- stages


def stage_count(cache: Path, data_dir: Path, limit: int | None) -> None:
    emit("# Крок 5 (S17, POB), стадія 0: частота допущених подій (без результатів угод)")
    sanity(cache, data_dir, False)
    R, why, _ = collect(cache, data_dir, False, limit)
    emit("\n- воронка (X = 10 %, усі символи й дати, обидва боки): " + ", ".join(f"{k} {why[k]}" for k in sorted(why) if ":pob" in k))
    emit("\n| варіант | подій IS | дат IS | подій OOS | дат OOS | лонг / шорт |\n|---|---|---|---|---|---|")
    for vid, v in enumerate(VARIANTS):
        m = R["var"] == vid
        i_, o_ = m & (R["oos"] == 0), m & (R["oos"] == 1)
        tag = " (основна)" if vid in PRIMARY_IDS else ""
        emit(
            f"| {vlabel(v)}{tag} | {int(i_.sum())} | {np.unique(R['day'][i_]).size} | {int(o_.sum())} | {np.unique(R['day'][o_]).size} | {int((m & (R['side'] == 1)).sum())} / {int((m & (R['side'] == -1)).sum())} |"
        )
    emit(f"\n- відсів: {dict(sorted((k, n) for k, n in why.items() if ':pob' not in k))}")


def stage_report(cache: Path, data_dir: Path, limit: int | None, out_dir: Path) -> None:
    emit("# Крок 5 (S17): ретест імпульсно пробитого POB — результати")
    emit(
        f"Всесвіт і дані — кроку 1; бари пробою {s1.day_of(START_MS)} … {s1.day_of(END_MS)}; IS < {s1.day_of(OOS_MS)} ≤ OOS. "
        f"POB — свічка з найвищим закриттям за {LOOK} барів до імпульсу ≥ 10 % і ≥ {ATR_MULT:g} ATR; пробій — перше закриття за high POB за {BREAK_DAYS} діб. "
        f"Ліміт на high POB чекає 90 діб; стоп за POB, ціль 2R, утримання {HOLD_BARS} барів. R — на ризик угоди, не на капітал; net після мейкера 0.02 % на вході й цілі та 0.07 % на ринкових виходах.\n"
    )
    sanity(cache, data_dir, True)
    R, why, syms = collect(cache, data_dir, True, limit)
    np.savez_compressed(out_dir / "rows.npz", **R)
    emit(f"\n- рядків {R['sym'].size}; відсів і лічильники: {dict(sorted(why.items()))}")
    analyse(R, syms)


def stage_analyse(cache: Path, out_dir: Path) -> None:
    emit("# Крок 5 (S17): статистика з rows.npz зареєстрованого прогону (без повторної симуляції)")
    with np.load(out_dir / "rows.npz") as z:
        R = {c: z[c] for c in COLS}
    uni = s1.members(cache)
    analyse(R, sorted({s for v in uni.values() for s in v}))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step5_s17")
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
