"""Course setups, step 7 (IDEA-20261016-course-setups-crypto): the course's basic theses on crypto perps.

H1 basics (BOS by body vs by wick), H2 basics (equal highs / lows as a magnet), H3 basics (retracement depth after a
BOS: OTE vs a bootstrap random walk), H8 module0 (the first test of a level is the best), H9 module0 (confluence at a
level) and S16 / H2 module0 (a deviation under a range vs a one-bar sweep), plus H2 goodmood (a stop beyond the sweep
extreme). Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 7". Stages:
  uv run python scripts/verify_course_step7_basics.py --stage prereg
  uv run python scripts/verify_course_step7_basics.py --stage count  --out ~/.cache/tradedesk-agents/course_step7_basics/count.md
  uv run python scripts/verify_course_step7_basics.py --stage report --out ~/.cache/tradedesk-agents/course_step7_basics/report.md
  uv run python scripts/verify_course_step7_basics.py --stage analyse --out ~/.cache/tradedesk-agents/course_step7_basics/analyse.md
``count`` (stage 0) counts events without any outcome; ``report`` simulates and writes one rows file per symbol to
--cache-dir/rows/ (resumable), then analyses; ``analyse`` recomputes the statistics from those files. ``--symbols N``
runs the first N symbols only (timing). ``--tests`` limits the theses (e.g. ``h1,h2,h3``). After the critic review
(2026-10-17): ``--s16-rule causal`` reruns S16 with the causal episode rule (robustness row; use a separate
--cache-dir), ``--tests h8x`` is an exploratory H8 re-check (ATR at the arming bar, equal placebo wait). No network: the 1h bars and
the universe come from the step-1 cache (--s14-cache, filled from data.binance.vision via rt.binance_vision); listings
from the funding-crowding cache (--data-dir). No keys, no orders.
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
from tradedesk.analytics.course_basics import (  # noqa: E402
    bar_shapes,
    bos_events,
    depth_path,
    eq_pools,
    fractal_mask,
    fractal_n_lows,
    level_tests,
    null_depth,
    ote_legs,
    race,
    range_events,
    trade_r,
    untaken_fractal_lows_near,
)
from tradedesk.analytics.course_fvg_ob import DAY_MS, HOUR_MS, fvg_zones, limit_long  # noqa: E402
from tradedesk.analytics.course_s14_ema import Bars, align_daily, atr, ema  # noqa: E402
from tradedesk.analytics.course_s18_trend import pos_from_seg, resample_partial, segments  # noqa: E402
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means, holm, without_top_days  # noqa: E402
from tradedesk.analytics.session_stop_hunt import cluster_bootstrap_diff  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
# "registered": the episode rule of the registered run (looks ahead for sweeps inside a deviation, see range_events);
# "causal": the fixed rule (critic review 2026-10-17, finding 1), a robustness row, run into a separate --cache-dir
CFG: dict[str, Any] = {"s16_rule": "registered"}
SEED = 20261017
PER_EVENT = 3
WARMUP = 100
START_MS, OOS_MS = s1.START_MS, s1.OOS_MS
END_MS = {"1h": s1.END_MS, "4h": s1.END_MS, "1d": s1.ms("2026-08-20T00:00")}
TF_HOURS = {"1h": 1, "4h": 4, "1d": 24}
RAND_DAYS = {"1h": 30, "4h": 30, "1d": 60}
STEP_P = (0.985, 0.280, 0.448, 0.038, 0.822, 1.0)  # primary p of steps 1-6 (Holm family of the card)
N_PRIMARY = 6

# H1
H1_HORIZON = 60
# H2
EQ_TOL, EQ_TOL2, EQ_LOOK, EQ_N, EQ_N2, EQ_DTOL, EQ_WIN_DAYS = 0.25, 0.10, 100, 20, 50, 0.25, 60
# H3
OTE_MIN_ATR, OTE_HORIZON, OTE_SIMS, OTE_HIST, OTE_MIN_HIST = 2.0, 120, 20, 720, 500
OTE_ZONES = ((0.5, 0.79), (0.62, 0.79), (0.45, 0.55))
OTE_BINS = tuple((k / 10, (k + 1) / 10) for k in range(10))
OTE_X = (0.5, 0.62, 0.705, 0.79)
OTE_WAIT = OTE_HOLD = 60
OTE_STOP_BUF = 0.1
# H8 / H9
LV_N, LV_LIFE, LV_SEP, LV_HOLD, LV_PL_WAIT, CONF_BAND, CONF_LOOK = 5, 180, 1.0, 20, 60, 0.25, 100
# S16
RG_W, RG_WIDTH, RG_KMIN, RG_KMAX, RG_HORIZON = 30, 6.0, 3, 10, 20
STOP_M = (0.0, 0.5, 1.0)

# test codes
T = {
    "h1": 1,
    "h1_1h": 11,
    "h2": 2,
    "h2_t10": 21,
    "h3": 3,
    "h3_x50": 31,
    "h3_x62": 32,
    "h3_x705": 33,
    "h3_x79": 34,
    "h8": 8,
    "s16": 16,
    "s16_4h": 161,
    "gm": 17,
    "gm_4h": 171,
    "h8x": 81,  # exploratory re-check of H8 (critic review, finding 2): race scale = ATR at the arming bar
}
FCOLS = "test kind sym side day oos grp y y2 y3 y4 aux r_gross cost_r ra".split() + [f"h{k}" for k in range(10)]
ALL_TESTS = ("h1", "h2", "h3", "h8", "s16")


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 7")
    b = t.index("\n## ", a + 1)
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- rows


class Rows:
    def __init__(self) -> None:
        self.cur: dict[str, list[float]] = {c: [] for c in FCOLS}
        self.eid: list[int] = []
        self.next_eid = 0

    def new_eid(self) -> int:
        self.next_eid += 1
        return self.next_eid

    def add(self, eid: int, **kw: float) -> None:
        for c in FCOLS:
            self.cur[c].append(kw.get(c, np.nan))
        self.eid.append(eid)

    def table(self) -> dict[str, np.ndarray]:
        out = {c: np.asarray(v, dtype=np.float32) for c, v in self.cur.items()}
        out["eid"] = np.asarray(self.eid, dtype=np.int64)
        return out

    def __len__(self) -> int:
        return len(self.eid)


class TF:
    """One timeframe of one symbol: bars, segment positions, ATR, eligibility and close times."""

    def __init__(self, h1: Bars, seg: np.ndarray, tf: str, iv: np.ndarray | None) -> None:
        hrs = TF_HOURS[tf]
        if tf == "1h":
            self.b, segt = h1, seg
        else:
            self.b, segt = resample_partial(h1, seg, hrs, hrs)
        self.tf = tf
        self.bar_ms = hrs * HOUR_MS
        self.pos = pos_from_seg(segt)
        self.seg = segt
        self.atr = atr(self.b, 14, self.pos)
        self.close = self.b.t + self.bar_ms
        self.ok = (
            s3.in_intervals(self.close, iv)
            & (self.close >= START_MS)
            & (self.close < END_MS[tf])
            & (self.pos >= WARMUP)
            & np.isfinite(self.atr)
            & (self.atr > 0)
        )
        self.seg_lo = np.searchsorted(segt, segt, side="left")
        self.seg_hi = np.searchsorted(segt, segt, side="right")

    def __len__(self) -> int:
        return len(self.b)

    def cands(self, i: int, days: int) -> np.ndarray:
        win = days * DAY_MS
        lo = max(int(np.searchsorted(self.close, self.close[i] - win, side="left")), int(self.seg_lo[i]))
        hi = min(int(np.searchsorted(self.close, self.close[i] + win, side="right")), int(self.seg_hi[i]))
        c = np.flatnonzero(self.ok[lo:hi]) + lo
        return c[c != i]


def base(test: str, si: int, side: int, close_ms: int) -> dict[str, float]:
    return {"test": T[test], "sym": si, "side": side, "day": close_ms // DAY_MS, "oos": float(close_ms >= OOS_MS)}


def pick(rng: np.random.Generator, c: np.ndarray, k: int = PER_EVENT) -> list[int]:
    if c.size == 0:
        return []
    return [int(j) for j in rng.choice(c, size=min(k, c.size), replace=False)]


# ---------------------------------------------------------------- per-thesis runners (one side, long frame)


def run_h1(test: str, X: TF, B: Bars, P: Bars, evs: list, si: int, side: int, rng, rows: Rows, why: Counter, outcomes: bool) -> None:
    hz = H1_HORIZON * X.bar_ms
    for e in evs:
        t = e.t
        if not X.ok[t]:
            continue
        if not np.isfinite(e.opp):
            why[f"{test}:no_opposite"] += 1
            continue
        A = float(X.atr[t])
        c = float(B.c[t])
        up, dn = e.level + A, e.opp
        if c >= up:
            why[f"{test}:close_beyond_U_{'body' if e.body else 'wick'}"] += 1
            continue
        if c <= dn:
            why[f"{test}:close_below_D"] += 1
            continue
        bs = base(test, si, side, int(X.close[t]))
        grp = 1.0 if e.body else 0.0
        if not outcomes:
            rows.add(0, **bs, kind=0, grp=grp)
            continue
        r = race(P, int(X.close[t]), up, dn, hz)
        if r is None:
            why[f"{test}:no_path"] += 1
            continue
        eid = rows.new_eid()
        g, cst = trade_r(c, dn, r.exit_px) if np.isfinite(r.exit_px) else (np.nan, np.nan)
        rows.add(eid, **bs, kind=0, grp=grp, y=float(r.outcome == 1), y2=float(r.outcome == 0), aux=r.outcome, r_gross=g, cost_r=cst)
        du, dd = (up - c) / A, (c - dn) / A
        for j in pick(rng, X.cands(t, RAND_DAYS[X.tf])):
            aj, cj = float(X.atr[j]), float(B.c[j])
            uj, dj = cj + du * aj, cj - dd * aj
            rj = race(P, int(X.close[j]), uj, dj, hz)
            if rj is None:
                continue
            g, cst = trade_r(cj, dj, rj.exit_px) if np.isfinite(rj.exit_px) else (np.nan, np.nan)
            rows.add(eid, **bs, kind=1, grp=grp, y=float(rj.outcome == 1), y2=float(rj.outcome == 0), aux=rj.outcome, r_gross=g, cost_r=cst)


def touch(B: Bars, X: TF, t: int, level: float, n: int) -> float:
    if t + n >= len(B) or X.pos[t + n] != X.pos[t] + n:
        return np.nan
    return float(B.h[t + 1 : t + n + 1].max() >= level)


def run_h2(X: TF, B: Bars, si: int, side: int, rng, rows: Rows, why: Counter, outcomes: bool) -> None:
    for test, tol in (("h2", EQ_TOL), ("h2_t10", EQ_TOL2)):
        pools, singles = eq_pools(B, X.pos, X.atr, tol, EQ_LOOK)
        s_t = np.array([s[0] for s in singles], dtype=np.int64)
        s_d = np.array([(s[2] - B.c[s[0]]) / X.atr[s[0]] for s in singles]) if singles else np.zeros(0)
        s_l = np.array([s[2] for s in singles]) if singles else np.zeros(0)
        s_ok = X.ok[s_t] & (s_d > 0) if singles else np.zeros(0, dtype=bool)
        for t, _f1, _f2, lvl in pools:
            if not X.ok[t]:
                continue
            A = float(X.atr[t])
            d = (lvl - float(B.c[t])) / A
            if not d > 0:
                why[f"{test}:no_distance"] += 1
                continue
            y20, y50 = touch(B, X, t, lvl, EQ_N), touch(B, X, t, lvl, EQ_N2)
            if not np.isfinite(y20):
                why[f"{test}:short_tail"] += 1
                continue
            bs = base(test, si, side, int(X.close[t]))
            if not outcomes:
                rows.add(0, **bs, kind=0)
                continue
            eid = rows.new_eid()
            rows.add(eid, **bs, kind=0, y=y20, y2=y50, y3=d)
            # P_single: unpaired fractal highs at a similar distance, same segment, +-60 days
            if s_t.size:
                win = EQ_WIN_DAYS * DAY_MS
                m = s_ok & (np.abs(X.close[s_t] - X.close[t]) <= win) & (X.seg[s_t] == X.seg[t]) & (np.abs(s_d - d) <= EQ_DTOL) & (s_t != t)
                for k in pick(rng, np.flatnonzero(m)):
                    j = int(s_t[k])
                    z20, z50 = touch(B, X, j, float(s_l[k]), EQ_N), touch(B, X, j, float(s_l[k]), EQ_N2)
                    if np.isfinite(z20):
                        rows.add(eid, **bs, kind=1, y=z20, y2=z50, y3=float(s_d[k]))
            # P_rand: a random bar, a level at the same distance
            for j in pick(rng, X.cands(t, RAND_DAYS["4h"])):
                lj = float(B.c[j]) + d * float(X.atr[j])
                z20, z50 = touch(B, X, j, lj, EQ_N), touch(B, X, j, lj, EQ_N2)
                if np.isfinite(z20):
                    rows.add(eid, **bs, kind=2, y=z20, y2=z50, y3=d)


def run_h3(X: TF, B: Bars, P: Bars, evs: list, si: int, side: int, rng, rows: Rows, why: Counter, outcomes: bool) -> None:
    legs = ote_legs(B, X.pos, X.atr, evs, OTE_MIN_ATR)
    shapes_all = bar_shapes(P) if outcomes else None
    hz = OTE_HORIZON * X.bar_ms
    zones = OTE_ZONES + OTE_BINS
    for lg in legs:
        q = lg.q
        if not X.ok[q]:
            continue
        bs = base("h3", si, side, int(X.close[q]))
        if not outcomes:
            rows.add(0, **bs, kind=0)
            continue
        start = int(X.close[q])
        dp = depth_path(P, start, lg.hi, lg.lo, lg.d0, hz)
        if dp is None:
            why["h3:no_path"] += 1
            continue
        kind, d = dp
        i1 = int(np.searchsorted(P.t, start))
        i0 = max(0, i1 - OTE_HIST)
        sh = shapes_all[i0:i1]
        sh = sh[np.all(np.isfinite(sh), axis=1)]
        if sh.shape[0] < OTE_MIN_HIST:
            why["h3:short_history"] += 1
            continue
        nul = null_depth(sh, float(B.c[q]), lg.hi, lg.lo, lg.d0, OTE_HORIZON * 4, OTE_SIMS, rng, zones)
        eid = rows.new_eid()
        up = kind == "up"
        f = [float(up and a <= d < b) for a, b in OTE_ZONES]
        rows.add(eid, **bs, kind=0, y=f[0], y2=f[1], y3=f[2], y4=d, aux={"up": 1, "break": 0, "timeout": -1}[kind])
        rows.add(eid, **bs, kind=1, y=nul[0], y2=nul[1], y3=nul[2], **{f"h{k}": nul[3 + k] for k in range(10)})
        # limit trades at the retracement levels
        A = float(X.atr[q])
        c = float(B.c[q])
        stop = lg.lo - OTE_STOP_BUF * A
        for x in OTE_X:
            test = {0.5: "h3_x50", 0.62: "h3_x62", 0.705: "h3_x705", 0.79: "h3_x79"}[x]
            if lg.d0 >= x:
                why[f"{test}:touched_before"] += 1
                continue
            lvl = lg.hi - x * (lg.hi - lg.lo)
            tr = limit_long(P, start, OTE_WAIT * X.bar_ms, OTE_HOLD * X.bar_ms, lvl, stop, lg.hi)
            if tr is None:
                continue
            b2 = base(test, si, side, start)
            te = rows.new_eid()
            rows.add(te, **b2, kind=0, y=float(tr.filled), r_gross=tr.r_gross, cost_r=tr.cost_r)
            dl, ds, dt = (c - lvl) / A, (c - stop) / A, (lg.hi - c) / A
            for j in pick(rng, X.cands(q, RAND_DAYS["4h"])):
                aj, cj = float(X.atr[j]), float(B.c[j])
                tj = limit_long(P, int(X.close[j]), OTE_WAIT * X.bar_ms, OTE_HOLD * X.bar_ms, cj - dl * aj, cj - ds * aj, cj + dt * aj)
                if tj is not None:
                    rows.add(te, **b2, kind=1, y=float(tj.filled), r_gross=tj.r_gross, cost_r=tj.cost_r)


def run_h8(X: TF, B: Bars, P: Bars, D: Bars, dpos: np.ndarray, si: int, side: int, rng, rows: Rows, why: Counter, outcomes: bool) -> None:
    fn = fractal_n_lows(B.low, X.pos, LV_N)
    fl = fractal_mask(-B.low, X.pos)
    fz = fvg_zones(B, X.pos, X.atr)
    okc = np.concatenate([[0], np.cumsum(X.ok)])
    fz_form = np.array([z.form for z in fz], dtype=np.int64)
    with np.errstate(invalid="ignore"):
        e21, e200 = ema(B.c, 21, X.pos), ema(B.c, 200, X.pos)
        d100 = align_daily(B, D, ema(D.c, 100, dpos))
        d200 = align_daily(B, D, ema(D.c, 200, dpos))
    hold = LV_HOLD * X.bar_ms
    for f in np.flatnonzero(fn):
        f = int(f)
        conf = f + LV_N
        if conf >= len(B) or okc[min(len(B), f + LV_LIFE + 1)] == okc[conf]:
            continue  # no eligible arming bar possible
        A = float(X.atr[conf])
        if not (np.isfinite(A) and A > 0):
            continue
        L = float(B.low[f])
        for k, a, j in level_tests(B, X.pos, f, A, LV_N, LV_LIFE, LV_SEP):
            if not X.ok[a]:
                continue
            bs = base("h8", si, side, int(X.close[a]))
            aux = np.nan
            if k == 1:
                band = CONF_BAND * A
                fvg = False
                lo_i = int(np.searchsorted(fz_form, a - 200))
                hi_i = int(np.searchsorted(fz_form, a, side="right"))
                for z in fz[lo_i:hi_i]:
                    bot, top = z.levels["full"], z.levels["near"]
                    if bot < L + band and top > L - band and (z.form == a or B.low[z.form + 1 : a + 1].min() > bot):
                        fvg = True
                        break
                emas = [e21[a], e200[a], d100[a], d200[a]]
                near_ema = any(np.isfinite(v) and abs(v - L) <= band for v in emas)
                eql = untaken_fractal_lows_near(B, fl, a, L, band, CONF_LOOK, exclude=f)
                aux = float(fvg) + 2 * float(near_ema) + 4 * float(eql)
            if not outcomes:
                rows.add(0, **bs, kind=0, grp=float(k), aux=aux)
                continue
            start = int(X.close[a])
            wait = int(X.close[j]) - start
            tr = limit_long(P, start, wait, hold, L, L - A, L + 2 * A, react_px=A)
            if tr is None or not tr.filled:
                why[f"h8:k{min(k, 4)}_no_fill"] += 1
                continue
            eid = rows.new_eid()
            rows.add(eid, **bs, kind=0, grp=float(k), y=float(tr.react == 1), y2=tr.mfe_r, aux=aux, r_gross=tr.r_gross, cost_r=tr.cost_r)
            if k == 1:
                dist = (float(B.c[a]) - L) / A
                for jj in pick(rng, X.cands(a, RAND_DAYS["4h"])):
                    aj = float(X.atr[jj])
                    lj = float(B.c[jj]) - dist * aj
                    pt = limit_long(P, int(X.close[jj]), LV_PL_WAIT * X.bar_ms, hold, lj, lj - aj, lj + 2 * aj, react_px=aj)
                    if pt is not None:
                        rows.add(
                            eid,
                            **bs,
                            kind=1,
                            grp=1.0,
                            y=float(pt.react == 1) if pt.filled else np.nan,
                            y2=pt.mfe_r if pt.filled else np.nan,
                            y4=float(pt.filled),
                            r_gross=pt.r_gross,
                            cost_r=pt.cost_r,
                        )


def run_h8x(X: TF, B: Bars, P: Bars, si: int, side: int, rng, rows: Rows, why: Counter) -> None:
    """Exploratory (after the results; critic review finding 2). Same levels, arming and tests as ``run_h8``, but the
    reaction race, stop and target use A_arm = ATR at the arming bar instead of ATR[f + 5]. Event row (kind 0):
    y = reaction with A_arm, y2 = reaction with A_conf (the registered scale), r_gross / cost_r with A_arm, ra = net
    with A_conf. Test-1 placebos, both with the distance in A_arm units and the placebo bar's own ATR: kind 2 waits as
    long as the event's level could still live (bars a .. f + life), kind 3 waits the registered 60 bars."""
    fn = fractal_n_lows(B.low, X.pos, LV_N)
    okc = np.concatenate([[0], np.cumsum(X.ok)])
    hold = LV_HOLD * X.bar_ms
    for f in np.flatnonzero(fn):
        f = int(f)
        conf = f + LV_N
        if conf >= len(B) or okc[min(len(B), f + LV_LIFE + 1)] == okc[conf]:
            continue
        A = float(X.atr[conf])
        if not (np.isfinite(A) and A > 0):
            continue
        L = float(B.low[f])
        for k, a, j in level_tests(B, X.pos, f, A, LV_N, LV_LIFE, LV_SEP):
            if not X.ok[a]:
                continue
            Aa = float(X.atr[a])
            bs = base("h8x", si, side, int(X.close[a]))
            start = int(X.close[a])
            wait = int(X.close[j]) - start
            tr = limit_long(P, start, wait, hold, L, L - Aa, L + 2 * Aa, react_px=Aa)
            tc = limit_long(P, start, wait, hold, L, L - A, L + 2 * A, react_px=A)
            if tr is None or not tr.filled or tc is None or not tc.filled:
                why[f"h8x:k{min(k, 4)}_no_fill"] += 1
                continue
            eid = rows.new_eid()
            rows.add(
                eid,
                **bs,
                kind=0,
                grp=float(k),
                y=float(tr.react == 1),
                y2=float(tc.react == 1),
                y3=Aa / A,
                r_gross=tr.r_gross,
                cost_r=tr.cost_r,
                ra=tc.r_gross - tc.cost_r,
            )
            if k != 1:
                continue
            dist = (float(B.c[a]) - L) / Aa
            life_ms = (f + LV_LIFE - a) * X.bar_ms
            for jj in pick(rng, X.cands(a, RAND_DAYS["4h"])):
                aj = float(X.atr[jj])
                lj = float(B.c[jj]) - dist * aj
                for kind, w_ms in ((2, life_ms), (3, LV_PL_WAIT * X.bar_ms)):
                    pt = limit_long(P, int(X.close[jj]), w_ms, hold, lj, lj - aj, lj + 2 * aj, react_px=aj)
                    if pt is not None:
                        rows.add(
                            eid,
                            **bs,
                            kind=kind,
                            grp=1.0,
                            y=float(pt.react == 1) if pt.filled else np.nan,
                            y4=float(pt.filled),
                            r_gross=pt.r_gross,
                            cost_r=pt.cost_r,
                        )


def run_s16(test: str, gm: str, X: TF, B: Bars, P: Bars, si: int, side: int, rng, rows: Rows, why: Counter, outcomes: bool) -> None:
    hz = RG_HORIZON * X.bar_ms
    for e in range_events(B, X.pos, X.atr, RG_W, RG_WIDTH, RG_KMIN, RG_KMAX, lookahead_busy=CFG["s16_rule"] == "registered"):
        r = e.r
        if not X.ok[r]:
            continue
        A = float(X.atr[r])
        c = float(B.c[r])
        grp = 1.0 if e.kind == "dev" else 0.0
        bs = base(test, si, side, int(X.close[r]))
        if not outcomes:
            rows.add(0, **bs, kind=0, grp=grp)
            continue
        start = int(X.close[r])
        stops = [e.ld - m * A for m in STOP_M]
        if not stops[0] < c:
            why[f"{test}:no_risk"] += 1
            continue
        rr = [race(P, start, e.mid, s_, hz) for s_ in stops]
        if any(x is None for x in rr):
            why[f"{test}:no_path"] += 1
            continue
        eid = rows.new_eid()
        g0, c0 = trade_r(c, stops[0], rr[0].exit_px) if np.isfinite(rr[0].exit_px) else (np.nan, np.nan)
        g5, c5 = trade_r(c, stops[1], rr[1].exit_px) if np.isfinite(rr[1].exit_px) else (np.nan, np.nan)
        rows.add(
            eid,
            **bs,
            kind=0,
            grp=grp,
            y=float(rr[0].outcome == 1),
            y2=float(rr[1].outcome == 1),
            y3=float(rr[2].outcome == 1),
            r_gross=g0,
            cost_r=c0,
            ra=g5 - c5,
        )
        du = (e.mid - c) / A
        dds = [(c - s_) / A for s_ in stops]
        for j in pick(rng, X.cands(r, RAND_DAYS[X.tf])):
            aj, cj = float(X.atr[j]), float(B.c[j])
            pr = [race(P, int(X.close[j]), cj + du * aj, cj - dd * aj, hz) for dd in dds]
            if any(x is None for x in pr):
                continue
            g0, c0 = trade_r(cj, cj - dds[0] * aj, pr[0].exit_px) if np.isfinite(pr[0].exit_px) else (np.nan, np.nan)
            g5, c5 = trade_r(cj, cj - dds[1] * aj, pr[1].exit_px) if np.isfinite(pr[1].exit_px) else (np.nan, np.nan)
            rows.add(
                eid,
                **bs,
                kind=1,
                grp=grp,
                y=float(pr[0].outcome == 1),
                y2=float(pr[1].outcome == 1),
                y3=float(pr[2].outcome == 1),
                r_gross=g0,
                cost_r=c0,
                ra=g5 - c5,
            )
        # H2 goodmood on sweeps: stop beyond the extreme, target 2R
        if e.kind == "sweep":
            b2 = base(gm, si, side, start)
            ge = rows.new_eid()
            for mi, s_ in enumerate(stops):
                rk = race(P, start, c + 2 * (c - s_), s_, hz)
                if rk is None or not np.isfinite(rk.exit_px):
                    continue
                g, cst = trade_r(c, s_, rk.exit_px)
                rows.add(ge, **b2, kind=0, grp=float(mi), y=float(rk.outcome == 0), r_gross=g, cost_r=cst)


def run_symbol(si: int, s: str, h1: Bars, iv, rng, outcomes: bool, tests: set[str], rows: Rows, why: Counter) -> None:
    seg = segments(h1.t)
    X4 = TF(h1, seg, "4h", iv)
    if len(X4) < WARMUP + 30 or not X4.ok.any():
        why["symbol_no_eligible_4h"] += 1
        return
    XD = TF(h1, seg, "1d", iv) if "s16" in tests or "h8" in tests else None
    X1 = TF(h1, seg, "1h", iv) if "h1" in tests else None
    for side in (1, -1):
        P = h1 if side == 1 else h1.mirrored()
        B4 = X4.b if side == 1 else X4.b.mirrored()
        evs = bos_events(B4, X4.pos) if ("h1" in tests or "h3" in tests) else []
        if "h1" in tests:
            run_h1("h1", X4, B4, P, evs, si, side, rng, rows, why, outcomes)
        if "h1" in tests and X1 is not None:
            B1 = X1.b if side == 1 else X1.b.mirrored()
            run_h1("h1_1h", X1, B1, P, bos_events(B1, X1.pos), si, side, rng, rows, why, outcomes)
        if "h2" in tests:
            run_h2(X4, B4, si, side, rng, rows, why, outcomes)
        if "h3" in tests:
            run_h3(X4, B4, P, evs, si, side, rng, rows, why, outcomes)
        if "h8" in tests and XD is not None:
            BD = XD.b if side == 1 else XD.b.mirrored()
            run_h8(X4, B4, P, BD, XD.pos, si, side, rng, rows, why, outcomes)
        if "h8x" in tests and outcomes:
            run_h8x(X4, B4, P, si, side, rng, rows, why)
        if "s16" in tests and XD is not None:
            BD = XD.b if side == 1 else XD.b.mirrored()
            run_s16("s16", "gm", XD, BD, P, si, side, rng, rows, why, outcomes)
            run_s16("s16_4h", "gm_4h", X4, B4, P, si, side, rng, rows, why, outcomes)


def collect(
    cache: Path, data_dir: Path, out_dir: Path | None, outcomes: bool, limit: int | None, tests: set[str]
) -> tuple[dict[str, np.ndarray], Counter, list[str]]:
    uni = s1.members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    if limit:
        syms = syms[:limit]
    ivs = s3.member_intervals(uni)
    why: Counter = Counter()
    tables: list[dict[str, np.ndarray]] = []
    t0 = time.time()
    tag = "-".join(sorted(tests))
    for n, s in enumerate(syms):
        f = out_dir / "rows" / tag / f"{s}.npz" if (out_dir is not None and outcomes) else None
        if f is not None and f.exists():
            with np.load(f) as z:
                tables.append({c: z[c] for c in z.files})
            continue
        rng = np.random.default_rng([SEED, n])  # per-symbol stream: resumable and order-independent
        h1 = s1.load_1h(cache, data_dir, s)
        rows = Rows()
        if len(h1) < 500:
            why["symbol_too_short"] += 1
        else:
            run_symbol(n, s, h1, ivs.get(s), rng, outcomes, tests, rows, why)
        tb = rows.table()
        if f is not None:
            f.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(f, **tb)
        tables.append(tb)
        del h1
        if (n + 1) % 10 == 0 or n + 1 == len(syms):
            emit(
                f"  [run] {n + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rows {sum(x['eid'].size for x in tables)}, rss {rss_mb():.0f} MB"
            )
    R = {c: np.concatenate([x[c] for x in tables]) if tables else np.zeros(0) for c in [*FCOLS, "eid"]}
    # eids are per-symbol counters: make them global
    if tables:
        R["eid"] = np.concatenate([x["eid"] + (k << 32) for k, x in enumerate(tables)])
    return R, why, syms


# ---------------------------------------------------------------- statistics


def week_of(day: np.ndarray) -> np.ndarray:
    return (day.astype(np.int64) + 3) // 7


def ci_str(ci: tuple[float, float] | None, pct: bool = False) -> str:
    if ci is None:
        return "—"
    return f"[{ci[0] * 100:+.1f}; {ci[1] * 100:+.1f}]" if pct else f"[{ci[0]:+.3f}; {ci[1]:+.3f}]"


def summ(v: np.ndarray, cl: np.ndarray, n_boot: int = 10000) -> dict[str, Any]:
    ok = np.isfinite(v)
    v, cl = v[ok], cl[ok]
    if v.size == 0:
        return {"n": 0, "days": 0, "mean": float("nan"), "ci": None, "p": None, "se": None}
    multi = np.unique(cl).size > 1
    bm = cluster_bootstrap_means(v.tolist(), cl.tolist(), n_boot=n_boot, seed=SEED) if multi else np.array([])
    return {
        "n": int(v.size),
        "days": int(np.unique(cl).size),
        "mean": float(v.mean()),
        "ci": (float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))) if multi else None,
        "p": float((np.sum(bm <= 0) + 1) / (bm.size + 1)) if multi else None,
        "se": float(np.std(bm)) if multi else None,
    }


def bdiff(a: np.ndarray, acl: np.ndarray, b: np.ndarray, bcl: np.ndarray, n_boot: int = 10000) -> dict[str, Any]:
    ka, kb = np.isfinite(a), np.isfinite(b)
    a, acl, b, bcl = a[ka], acl[ka], b[kb], bcl[kb]
    if a.size == 0 or b.size == 0:
        return {"diff": float("nan"), "ci": None, "p": None, "se": None}
    out = cluster_bootstrap_diff(a.tolist(), acl.tolist(), b.tolist(), bcl.tolist(), n_boot=n_boot, seed=SEED)
    out["se"] = (out["ci"][1] - out["ci"][0]) / (2 * 1.96) if out["ci"] is not None else None
    return out


def paired_x(R: dict[str, np.ndarray], test: int, pk: int, col: str = "y", ev_mask: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """Per event: y(event) - mean y(its placebo rows of kind ``pk``); events without a finite placebo are dropped."""
    mt = R["test"] == test
    ev = mt & (R["kind"] == 0)
    if ev_mask is not None:
        ev &= ev_mask
    pl = mt & (R["kind"] == pk) & np.isfinite(R[col])
    pe, pv = R["eid"][pl], R[col][pl].astype(float)
    if pe.size:
        u, inv = np.unique(pe, return_inverse=True)
        s = np.bincount(inv, weights=pv)
        c = np.bincount(inv)
        means = s / c
    else:
        u, means = np.zeros(0, dtype=np.int64), np.zeros(0)
    ee = R["eid"][ev]
    k = np.searchsorted(u, ee)
    has = (k < u.size) & (u[np.minimum(k, max(u.size - 1, 0))] == ee) if u.size else np.zeros(ee.size, dtype=bool)
    idx = np.flatnonzero(ev)[has]
    yv = R[col][idx].astype(float)
    pm = means[k[has]]
    keep = np.isfinite(yv)
    idx, yv, pm = idx[keep], yv[keep], pm[keep]
    return {
        "idx": idx,
        "x": yv - pm,
        "y": yv,
        "yp": pm,
        "day": R["day"][idx].astype(np.int64),
        "oos": R["oos"][idx],
        "grp": R["grp"][idx],
        "n_ev": int(ev.sum()),
    }


def gate_verdict(
    d: dict[str, Any],
    dates: int,
    min_dates: int,
    gate: float,
    eff: float,
    wk: dict[str, Any],
    oos: dict[str, Any],
    p_adj: float | None,
    extra_ok: bool = True,
) -> tuple[str, str]:
    """Registered verdict and the protocol mark."""
    mde = 2.8 * d["se"] if d.get("se") is not None else float("inf")
    pos_ci = d["ci"] is not None and d["ci"][0] > 0
    if dates < min_dates or mde > gate:
        return f"inconclusive (ворота: дат {dates} / поріг {min_dates}, MDE {mde:.3f} / {gate})", "◯"
    if d["diff"] < eff or not pos_ci:
        mark = "≈" if pos_ci else "✗"
        return f"rejected (різниця {d['diff']:+.3f} {'<' if d['diff'] < eff else '≥'} {eff}; CI {ci_str(d['ci'])})", mark
    if (
        wk["ci"] is not None
        and wk["ci"][0] > 0
        and p_adj is not None
        and p_adj < 0.05
        and oos["diff"] > 0
        and oos["p"] is not None
        and oos["p"] < 0.05
        and extra_ok
    ):
        return "confirmed", "✓"
    return "inconclusive (позитив в IS не пройшов тижні, Holm чи OOS)", "≈"


def diff_block(
    x: np.ndarray, day: np.ndarray, g1: np.ndarray | None = None, g0: np.ndarray | None = None, n_boot: int = 10000
) -> dict[str, Any]:
    """Either mean(x) (g1 None) or mean(x[g1]) - mean(x[g0]) with a joint date bootstrap; also ISO-week clusters."""
    if g1 is None:
        s = summ(x, day, n_boot)
        w = summ(x, week_of(day), n_boot)
        return {"diff": s["mean"], "ci": s["ci"], "p": s["p"], "se": s["se"], "n": s["n"], "days": s["days"], "wk": {"ci": w["ci"]}}
    d = bdiff(x[g1], day[g1], x[g0], day[g0], n_boot)
    w = bdiff(x[g1], week_of(day[g1]), x[g0], week_of(day[g0]), n_boot)
    d.update(n=int(g1.sum()), n0=int(g0.sum()), days=int(np.unique(day[g1]).size), days0=int(np.unique(day[g0]).size), wk={"ci": w["ci"]})
    return d


def pp(v: float) -> str:
    return f"{v * 100:+.1f} п. п."


# ---------------------------------------------------------------- stage 0


def sanity(cache: Path, data_dir: Path) -> None:
    emit("## Санітарні перевірки")
    h1 = s1.load_1h(cache, data_dir, "BTCUSDT")
    seg = segments(h1.t)
    b4, s4 = resample_partial(h1, seg, 4, 4)
    p4 = pos_from_seg(s4)
    a4 = atr(b4, 14, p4)
    k = int(np.searchsorted(b4.t, s1.ms("2024-12-31T20:00")))
    emit(f"- BTCUSDT: 4h-закриття 2024-12-31 20:00–24:00 = {b4.c[k]:g} (еталон кроків 1–6: 93548.9)")
    emit(f"- BTCUSDT: 4h-барів {len(b4)}, сегментів {int(s4.max()) + 1}; медіана ATR(4h)/ціна {np.nanmedian(a4 / b4.c):.2%}")
    n0 = len(b4) - 300
    rows = [{"ts": int(b4.t[i]), "open": b4.o[i], "high": b4.h[i], "low": b4.low[i], "close": b4.c[i]} for i in range(n0, len(b4))]
    ref = markup.fractals(rows)
    sub = b4.slice(n0, len(b4))
    sp = p4[n0:] - p4[n0]
    mine_h = int(fractal_mask(sub.h, sp).sum())
    mine_l = int(fractal_mask(-sub.low, sp).sum())
    emit(
        f"- BTCUSDT, останні 300 4h-барів: фрактали markup {len(ref['highs'])} / {len(ref['lows'])}, course_basics {mine_h} / {mine_l} — мають збігатися"
    )
    st = markup.structure(rows, ref)
    ev = bos_events(sub, sp)
    emit(
        f"- там само: markup.structure — bos_up {sum(e['type'] == 'bos_up' for e in st['events'])}, sweep_high {sum(e['type'] == 'sweep_high' for e in st['events'])}; "
        f"course_basics (лише останній свінг) — тілом {sum(e.body for e in ev)}, тінню {sum(not e.body for e in ev)}"
    )
    emit(f"- пікова RSS {rss_mb():.0f} MB")


def stage_count(cache: Path, data_dir: Path, limit: int | None, tests: set[str]) -> None:
    emit("# Крок 7 (базові тези курсу), стадія 0: частота подій (без результатів)")
    sanity(cache, data_dir)
    R, why, _ = collect(cache, data_dir, None, False, limit, tests)
    emit("\n| тест / група | подій IS | дат IS | подій OOS | дат OOS |\n|---|---|---|---|---|")
    labels = []
    for name in ("h1", "h1_1h", "s16", "s16_4h"):
        for g, lab in ((1.0, "тіло / девіація"), (0.0, "тінь / свип")):
            labels.append((name, lab, (R["test"] == T[name]) & (R["grp"] == g)))
    for name in ("h2", "h2_t10", "h3"):
        labels.append((name, "", R["test"] == T[name]))
    for k in (1, 2, 3):
        labels.append(("h8", f"тест {k}", (R["test"] == T["h8"]) & (R["grp"] == k)))
    labels.append(("h8", "тест ≥ 4", (R["test"] == T["h8"]) & (R["grp"] >= 4)))
    t1 = (R["test"] == T["h8"]) & (R["grp"] == 1)
    cnt = np.zeros(R["aux"].size)
    for bit in (1, 2, 4):
        cnt += (np.nan_to_num(R["aux"]).astype(np.int64) & bit) > 0
    for c in (0, 1, 2, 3):
        labels.append(("h9", f"факторів {c}", t1 & (cnt == c)))
    for bit, lab in ((1, "FVG"), (2, "EMA"), (4, "EQL")):
        labels.append(("h9", f"фактор {lab}", t1 & ((np.nan_to_num(R["aux"]).astype(np.int64) & bit) > 0)))
    for name, lab, m in labels:
        i_, o_ = m & (R["oos"] == 0), m & (R["oos"] == 1)
        emit(f"| {name} {lab} | {int(i_.sum())} | {np.unique(R['day'][i_]).size} | {int(o_.sum())} | {np.unique(R['day'][o_]).size} |")
    emit(f"\n- відсів: {dict(sorted(why.items()))}")


# ---------------------------------------------------------------- report


def stage_report(cache: Path, data_dir: Path, limit: int | None, out_dir: Path, tests: set[str]) -> None:
    emit("# Крок 7 (базові тези курсу) — результати")
    emit(
        f"Всесвіт і дані — кроку 1; бари рішення {s1.day_of(START_MS)} … {s1.day_of(END_MS['4h'])} (1h, 4h), … {s1.day_of(END_MS['1d'])} (1D); IS < {s1.day_of(OOS_MS)} ≤ OOS. Тези: {sorted(tests)}.\n"
    )
    sanity(cache, data_dir)
    R, why, syms = collect(cache, data_dir, out_dir, True, limit, tests)
    emit(f"\n- рядків {R['eid'].size}; відсів і лічильники: {dict(sorted(why.items()))}")
    analyse(R, syms, tests)


def stage_analyse(cache: Path, out_dir: Path, tests: set[str], data_dir: Path) -> None:
    emit("# Крок 7: статистика з файлів rows/ зареєстрованого прогону (без повторної симуляції)")
    R, _, syms = collect(cache, data_dir, out_dir, True, None, tests)
    analyse(R, syms, tests)


def popcount3(aux: np.ndarray) -> np.ndarray:
    a = np.nan_to_num(aux, nan=-1).astype(np.int64)
    out = ((a & 1) > 0).astype(int) + ((a & 2) > 0) + ((a & 4) > 0)
    out[a < 0] = -1
    return out


def analyse(R: dict[str, np.ndarray], syms: list[str], tests: set[str]) -> None:
    always = {i for i, s in enumerate(syms) if s in s1.ALWAYS}
    btc_eth = np.isin(R["sym"], list(always))
    years = (R["day"].astype(np.int64) * DAY_MS).astype("datetime64[ms]").astype("datetime64[Y]").astype(int) + 1970
    net = R["r_gross"].astype(float) - R["cost_r"].astype(float)
    if "h8x" in tests:
        h8x_details(R, net, btc_eth, years)
        if tests == {"h8x"}:
            emit(f"\n(пікова RSS {rss_mb():.0f} MB)")
            return
    prim: dict[str, dict[str, Any]] = {}

    # ---- primaries (IS) with their OOS twins
    def h1_block(test: str, extra: np.ndarray | None = None, oos: float = 0.0) -> dict[str, Any]:
        X = paired_x(R, T[test], 1, ev_mask=extra)
        m = X["oos"] == oos
        g1, g0 = m & (X["grp"] == 1), m & (X["grp"] == 0)
        d = diff_block(X["x"], X["day"], g1, g0)
        d["X"], d["g1"], d["g0"] = X, g1, g0
        return d

    def mean_block(test: str, pk: int, col: str = "y", oos: float = 0.0, extra: np.ndarray | None = None) -> dict[str, Any]:
        X = paired_x(R, T[test], pk, col, ev_mask=extra)
        m = X["oos"] == oos
        d = diff_block(X["x"][m], X["day"][m])
        d["X"], d["m"] = X, m
        return d

    def grp_block(test: str, g1f, g0f, oos: float = 0.0, col: str = "y", extra: np.ndarray | None = None) -> dict[str, Any]:
        base_m = (R["test"] == T[test]) & (R["kind"] == 0) & (R["oos"] == oos)
        if extra is not None:
            base_m &= extra
        y = R[col].astype(float)
        g1 = base_m & g1f
        g0 = base_m & g0f
        d = diff_block(y, R["day"].astype(np.int64), g1, g0)
        d["g1m"], d["g0m"] = g1, g0
        return d

    k_r = R["grp"]
    cnt = popcount3(R["aux"])
    spec: dict[str, Any] = {}
    if "h1" in tests:
        spec["H1"] = ("h1", lambda oos: h1_block("h1", oos=oos), 200, 0.04, 0.05)
    if "h2" in tests:
        spec["H2"] = ("h2", lambda oos: mean_block("h2", 1, oos=oos), 200, 0.04, 0.05)
    if "h3" in tests:
        spec["H3"] = ("h3", lambda oos: mean_block("h3", 1, oos=oos), 200, 0.04, 0.05)
    if "h8" in tests:
        spec["H8"] = ("h8", lambda oos: grp_block("h8", k_r == 1, (k_r == 2) | (k_r == 3), oos), 200, 0.05, 0.05)
        spec["H9"] = ("h8", lambda oos: grp_block("h8", (k_r == 1) & (cnt >= 2), (k_r == 1) & (cnt == 0), oos), 200, 0.06, 0.05)
    if "s16" in tests:
        spec["S16"] = ("s16", lambda oos: h1_block("s16", oos=oos), 100, 0.10, 0.10)
    blocks = {k: (v[1](0.0), v[1](1.0)) for k, v in spec.items()}
    raw_p = {k: (b[0]["p"] if b[0]["p"] is not None else 1.0) for k, b in blocks.items()}
    all_p = [raw_p.get(k, 1.0) for k in ("H1", "H2", "H3", "H8", "H9", "S16")]
    p_step = min(1.0, N_PRIMARY * min(all_p))
    p_adj = holm([*STEP_P, p_step])[len(STEP_P)]
    emit("\n## Основні метрики (IS) і вердикти")
    emit(
        f"- сирі p (різниця > 0): {', '.join(f'{k} {v:.4g}' for k, v in raw_p.items())}; непрогнані — 1.0. p кроку = min(1, 6·min) = {p_step:.4g}; Holm (m = 7: {', '.join(map(str, STEP_P))}, крок 7) = {p_adj:.4g}; ×11 для довідки {min(1.0, 11 * p_step):.4g}"
    )
    names = {
        "H1": "H1 basics: D1 = x̄(BOS тілом) − x̄(тінню), x = 1[+1 ATR за рівнем раніше протилежного свінгу] − P_rand",
        "H2": "H2 basics: Δ_EQ = дотик EQH/EQL за 20 барів − окремий свінг на тій самій відстані (P_single)",
        "H3": "H3 basics: Δ_OTE = частка розворотів у [0.5; 0.79) з оновленням хаю − бутстреп-блукання",
        "H8": "H8 module0: Δ_H8 = реакція +1 ATR раніше −1 ATR на тесті 1 − на тестах 2–3",
        "H9": "H9 module0: Δ_H9 = реакція при ≥ 2 факторах − при 0 факторів (тест 1)",
        "S16": "S16 / H2 module0: Δ_S16 = x̄(девіація) − x̄(свип), x = 1[середина рейнджу раніше стопу під лоями] − P_rand",
    }
    verdicts: dict[str, tuple[str, str]] = {}
    for key, (_t, _f, min_d, gate, eff) in spec.items():
        d, do = blocks[key]
        dates = min(d.get("days", 0), d.get("days0", d.get("days", 0)))
        if key == "H9":
            dates = d.get("days", 0)
        if key == "S16":
            dates = d.get("days", 0)
        extra_ok = True
        if key == "S16":
            extra_ok = do.get("days", 0) >= 20
        vd = gate_verdict(d, dates, min_d, gate, eff, d["wk"], do, p_adj, extra_ok)
        verdicts[key] = vd
        prim[key] = d
        mde = 2.8 * d["se"] if d.get("se") is not None else float("nan")
        emit(f"\n### {names[key]}")
        n_line = f"n {d.get('n')} / {d.get('n0', '—')}, дат {d.get('days')} / {d.get('days0', '—')}"
        emit(
            f"- IS: {n_line}; **різниця {d['diff']:+.4f} {ci_str(d['ci'])}**, p {d['p']}; за тижнями {ci_str(d['wk']['ci'])}; MDE {mde:.4f} (ворота {gate}; ефект ≥ {eff}; дат ≥ {min_d})"
        )
        emit(
            f"- OOS: n {do.get('n')} / {do.get('n0', '—')}, дат {do.get('days')} / {do.get('days0', '—')}; різниця {do['diff']:+.4f} {ci_str(do['ci'])}, p {do['p']}"
        )
        emit(f"- **вердикт за реєстрацією: {vd[0]}** — позначка {vd[1]}")
    for key in ("H1", "H2", "H3", "H8", "H9", "S16"):
        if key not in spec:
            emit(f"\n### {key}: не прогнано (p = 1 у p кроку)")
    vs = [v[0] for v in verdicts.values()]
    if any(v == "confirmed" for v in vs):
        step = "confirmed"
    elif len(vs) == N_PRIMARY and all(v.startswith("rejected") for v in vs):
        step = "rejected"
    else:
        step = "inconclusive" + ("" if len(vs) == N_PRIMARY else " (прогнано не всі тези — крок відкритий)")
    emit(f"\n- **вердикт кроку за реєстрацією: {step}**")

    # ---- per-thesis details
    if "h1" in tests:
        h1_details(R, net, btc_eth, years)
    if "h2" in tests:
        h2_details(R, btc_eth, years)
    if "h3" in tests:
        h3_details(R, net, btc_eth, years)
    if "h8" in tests:
        h8_details(R, net, btc_eth, years, cnt)
    if "s16" in tests:
        s16_details(R, net, btc_eth, years)
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def robust_rows(
    label: str, x: np.ndarray, day: np.ndarray, g1: np.ndarray | None, g0: np.ndarray | None, masks: list[tuple[str, np.ndarray]]
) -> None:
    emit(f"\n#### Стійкість: {label}")
    emit("| зріз | n | дат | різниця [CI] |\n|---|---|---|---|")
    for lab, m in masks:
        if g1 is not None and g0 is not None:
            d = diff_block(x, day, g1 & m, g0 & m, 2000)
        else:
            d = diff_block(x[m], day[m], None, None, 2000)
        emit(f"| {lab} | {d.get('n')} | {d.get('days')} | {d['diff']:+.4f} {ci_str(d['ci'])} |")
    if g1 is None:
        for k in (5, 10, 20):
            v, dd, _ = without_top_days(x.tolist(), day.tolist(), k)
            s = summ(np.asarray(v), np.asarray(dd), 2000)
            emit(f"| без {k} найкращих дат | {s['n']} | {s['days']} | {s['mean']:+.4f} {ci_str(s['ci'])} |")


def std_masks(sel_sym: np.ndarray, sel_side: np.ndarray, sel_year: np.ndarray, btc_eth_idx: np.ndarray) -> list[tuple[str, np.ndarray]]:
    m = [("лонги", sel_side == 1), ("шорти", sel_side == -1), ("BTC+ETH", btc_eth_idx), ("альти", ~btc_eth_idx)]
    for y in range(2020, 2025):
        m.append((f"рік {y}", sel_year == y))
    m.append(("рік 2025 (до 03-01)", sel_year == 2025))
    return m


def h1_details(R, net, btc_eth, years) -> None:
    emit("\n## H1 basics — деталі")
    for test, lab in (("h1", "4h"), ("h1_1h", "1h")):
        for oos in (0.0, 1.0):
            X = paired_x(R, T[test], 1)
            m = X["oos"] == oos
            idx = X["idx"]
            rows = []
            for g, gl in ((1.0, "тілом (a)"), (0.0, "тінню (b)")):
                gm = m & (X["grp"] == g)
                s = summ(X["x"][gm], X["day"][gm], 2000)
                down = R["y2"][idx[gm]].mean() if gm.any() else np.nan
                tout = np.mean(R["aux"][idx[gm]] == -1) if gm.any() else np.nan
                rows.append(
                    f"| {lab} {'OOS' if oos else 'IS'} {gl} | {s['n']} | {s['days']} | {X['y'][gm].mean():.1%} | {X['yp'][gm].mean():.1%} | {s['mean'] * 100:+.1f} {ci_str(s['ci'], True)} | {down:.1%} | {tout:.1%} |"
                )
            if oos == 0.0 and test == "h1":
                emit(
                    "| вибірка | n | дат | частка «+1 ATR раніше» | P_rand | x̄ = подія − P_rand, п. п. [CI] | частка «протилежний свінг раніше» | тайм-аут |\n|---|---|---|---|---|---|---|---|"
                )
            for r in rows:
                emit(r)
            gm = m
            emit(
                f"| {lab} {'OOS' if oos else 'IS'} будь-який вихід | {int(gm.sum())} | {np.unique(X['day'][gm]).size} | {X['y'][gm].mean():.1%} | {X['yp'][gm].mean():.1%} | {X['x'][gm].mean() * 100:+.1f} | — | — |"
            )
    # 1h primary-style D1 and 2022+
    for lab, test, extra in (("1h", "h1_1h", None), ("4h, лише 2022+", "h1", R["day"] >= s1.ms("2022-01-01T00:00") // DAY_MS)):
        X = paired_x(R, T[test], 1, ev_mask=extra)
        m = X["oos"] == 0
        d = diff_block(X["x"], X["day"], m & (X["grp"] == 1), m & (X["grp"] == 0), 5000)
        emit(f"- D1 {lab} (IS): {d['diff']:+.4f} {ci_str(d['ci'])}, p {d['p']}, MDE {2.8 * d['se']:.4f}" if d["se"] else f"- D1 {lab}: —")
    # trade row for (a)
    emit(
        "\n**Угода для BOS тілом (лонг за закриттям, стоп — протилежний свінг, ціль +1 ATR за рівнем, ринок 0.07 % на сторону; R — на ризик):**"
    )
    for test, lab in (("h1", "4h"), ("h1_1h", "1h")):
        for oos in (0.0, 1.0):
            ev = (R["test"] == T[test]) & (R["kind"] == 0) & (R["grp"] == 1) & (R["oos"] == oos) & np.isfinite(net)
            pl = (R["test"] == T[test]) & (R["kind"] == 1) & (R["grp"] == 1) & (R["oos"] == oos) & np.isfinite(net)
            s = summ(net[ev], R["day"][ev].astype(np.int64), 2000)
            d = bdiff(net[ev], R["day"][ev].astype(np.int64), net[pl], R["day"][pl].astype(np.int64), 2000)
            gross = R["r_gross"][ev].mean() if ev.any() else np.nan
            x2 = R["r_gross"][ev] - 2 * R["cost_r"][ev]
            x3 = R["r_gross"][ev] - 3 * R["cost_r"][ev]
            emit(
                f"- {lab} {'OOS' if oos else 'IS'}: n {s['n']}, E[R] gross {gross:+.3f}, **net {s['mean']:+.3f} {ci_str(s['ci'])}**; P_rand net {net[pl].mean():+.3f}; Δ {d['diff']:+.3f} {ci_str(d['ci'])}; витрати ×2 {np.nanmean(x2):+.3f}, ×3 {np.nanmean(x3):+.3f}; медіана витрат {np.nanmedian(R['cost_r'][ev]):.3f}R"
            )
    X = paired_x(R, T["h1"], 1)
    m = X["oos"] == 0
    g1, g0 = m & (X["grp"] == 1), m & (X["grp"] == 0)
    idx = X["idx"]
    masks = std_masks(R["sym"][idx], R["side"][idx], years[idx], btc_eth[idx])
    robust_rows("D1 (4h, IS)", X["x"], X["day"], g1, g0, masks)
    robust_rows("x̄(a) — BOS тілом проти P_rand (4h, IS)", X["x"][g1], X["day"][g1], None, None, [(lab, mm[g1]) for lab, mm in masks])
    robust_rows("x̄(b) — тінню проти P_rand (4h, IS)", X["x"][g0], X["day"][g0], None, None, [(lab, mm[g0]) for lab, mm in masks])


def h2_details(R, btc_eth, years) -> None:
    emit("\n## H2 basics — деталі (частки дотику рівня; Δ — подія мінус середнє її контролів, п. п.)")
    emit(
        "| варіант | контроль | N барів | вибірка | n | дат | дотик пулу | контроль | Δ [CI] | p |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    for test, tl in (("h2", "τ 0.25"), ("h2_t10", "τ 0.10")):
        for pk, pl in ((1, "P_single"), (2, "P_rand")):
            for col, nb in (("y", 20), ("y2", 50)):
                for oos in (0.0, 1.0):
                    X = paired_x(R, T[test], pk, col)
                    m = X["oos"] == oos
                    s = summ(X["x"][m], X["day"][m], 2000)
                    emit(
                        f"| {tl} | {pl} | {nb} | {'OOS' if oos else 'IS'} | {s['n']} | {s['days']} | {X['y'][m].mean():.1%} | {X['yp'][m].mean():.1%} | {s['mean'] * 100:+.1f} {ci_str(s['ci'], True)} | {s['p']} |"
                    )
    X = paired_x(R, T["h2"], 1)
    m = X["oos"] == 0
    idx = X["idx"]
    dist = R["y3"][idx]
    emit(f"\n- медіана відстані пулу до ціни (основне, IS): {np.median(dist[m]):.2f} ATR")
    masks = std_masks(R["sym"][idx], R["side"][idx], years[idx], btc_eth[idx])
    for q_lo, q_hi in ((0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 99)):
        masks.append((f"відстань {q_lo}–{q_hi} ATR", (dist >= q_lo) & (dist < q_hi)))
    robust_rows("Δ_EQ (τ 0.25, 20 барів, P_single, IS)", X["x"][m], X["day"][m], None, None, [(lab, mm[m]) for lab, mm in masks])


def h3_details(R, net, btc_eth, years) -> None:
    emit("\n## H3 basics — деталі")
    ev = (R["test"] == T["h3"]) & (R["kind"] == 0)
    nl = (R["test"] == T["h3"]) & (R["kind"] == 1)
    for oos in (0.0, 1.0):
        e = ev & (R["oos"] == oos)
        n_ = nl & (R["oos"] == oos)
        aux = R["aux"][e]
        emit(
            f"\n**{'OOS' if oos else 'IS'}:** ніг {int(e.sum())}; оновлення хаю {np.mean(aux == 1):.1%}, злам лою {np.mean(aux == 0):.1%}, тайм-аут {np.mean(aux == -1):.1%}"
        )
        for col, zl in (("y", "[0.50; 0.79)"), ("y2", "[0.62; 0.79) OTE"), ("y3", "[0.45; 0.55) біля 0.5")):
            X = paired_x(R, T["h3"], 1, col)
            m = X["oos"] == oos
            s = summ(X["x"][m], X["day"][m], 2000)
            emit(
                f"- зона {zl}: реально {X['y'][m].mean():.1%}, нуль {X['yp'][m].mean():.1%}, Δ {s['mean'] * 100:+.1f} п. п. {ci_str(s['ci'], True)}, p {s['p']}"
            )
        d = R["y4"][e]
        upm = R["aux"][e] == 1
        real_bins = [np.mean(upm & (d >= a) & (d < b)) for a, b in OTE_BINS]
        null_bins = [float(np.nanmean(R[f"h{k}"][n_])) for k in range(10)]
        emit("| глибина D (оновлення після відкату) | реально | нуль | різниця, п. п. |\n|---|---|---|---|")
        for (a, b), rv, nv in zip(OTE_BINS, real_bins, null_bins, strict=True):
            emit(f"| [{a:.1f}; {b:.1f}) | {rv:.1%} | {nv:.1%} | {(rv - nv) * 100:+.1f} |")
    X = paired_x(R, T["h3"], 1)
    m = X["oos"] == 0
    idx = X["idx"]
    masks = std_masks(R["sym"][idx], R["side"][idx], years[idx], btc_eth[idx])
    robust_rows("Δ_OTE (IS)", X["x"][m], X["day"][m], None, None, [(lab, mm[m]) for lab, mm in masks])
    emit("\n**Угоди: ліміт на рівні відкату, стоп Lo − 0.1 ATR, ціль Hi (мейкер на вході й цілі, ринок на стопі й часі; R — на ризик):**")
    emit(
        "| рівень | вибірка | ніг | виконано (подія / P_rand) | n угод | дат | E[R] gross | E[R] net [CI] | P_rand net | Δ [CI] | p Δ | net витрати ×2 / ×3 |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for x in OTE_X:
        test = {0.5: "h3_x50", 0.62: "h3_x62", 0.705: "h3_x705", 0.79: "h3_x79"}[x]
        for oos in (0.0, 1.0):
            e = (R["test"] == T[test]) & (R["kind"] == 0) & (R["oos"] == oos)
            p_ = (R["test"] == T[test]) & (R["kind"] == 1) & (R["oos"] == oos)
            ef, pf = e & (R["y"] == 1) & np.isfinite(net), p_ & (R["y"] == 1) & np.isfinite(net)
            s = summ(net[ef], R["day"][ef].astype(np.int64), 2000)
            d = bdiff(net[ef], R["day"][ef].astype(np.int64), net[pf], R["day"][pf].astype(np.int64), 2000)
            x2 = np.nanmean(R["r_gross"][ef] - 2 * R["cost_r"][ef]) if ef.any() else np.nan
            x3 = np.nanmean(R["r_gross"][ef] - 3 * R["cost_r"][ef]) if ef.any() else np.nan
            emit(
                f"| {x} | {'OOS' if oos else 'IS'} | {int(e.sum())} | {R['y'][e].mean():.1%} / {R['y'][p_].mean():.1%} | {s['n']} | {s['days']} | {R['r_gross'][ef].mean():+.3f} | {s['mean']:+.3f} {ci_str(s['ci'])} | {net[pf].mean():+.3f} | {d['diff']:+.3f} {ci_str(d['ci'])} | {d['p']} | {x2:+.3f} / {x3:+.3f} |"
            )


def h8_details(R, net, btc_eth, years, cnt) -> None:
    emit("\n## H8 / H9 module0 — деталі (тести рівня, 4h; реакція = +1 ATR від рівня раніше −1 ATR)")
    ev = (R["test"] == T["h8"]) & (R["kind"] == 0)
    emit("| тест | вибірка | n | дат | реакція | медіана MFE (ATR) | E[R] net [CI] |\n|---|---|---|---|---|---|---|")
    for oos in (0.0, 1.0):
        for k, lab in ((1, "1"), (2, "2"), (3, "3"), (4, "≥ 4")):
            m = ev & (R["oos"] == oos) & ((R["grp"] == k) if k < 4 else (R["grp"] >= 4))
            s = summ(net[m], R["day"][m].astype(np.int64), 2000)
            emit(
                f"| {lab} | {'OOS' if oos else 'IS'} | {int(m.sum())} | {np.unique(R['day'][m]).size} | {R['y'][m].mean():.1%} | {np.nanmedian(R['y2'][m]) if m.any() else np.nan:.2f} | {s['mean']:+.3f} {ci_str(s['ci'])} |"
            )
    # test 1 vs P_rand (filled only)
    for oos in (0.0, 1.0):
        e = ev & (R["grp"] == 1) & (R["oos"] == oos)
        p_ = (R["test"] == T["h8"]) & (R["kind"] == 1) & (R["oos"] == oos) & (R["y4"] == 1)
        d = bdiff(R["y"][e].astype(float), R["day"][e].astype(np.int64), R["y"][p_].astype(float), R["day"][p_].astype(np.int64), 2000)
        dn = bdiff(net[e], R["day"][e].astype(np.int64), net[p_], R["day"][p_].astype(np.int64), 2000)
        emit(
            f"- тест 1 проти P_rand ({'OOS' if oos else 'IS'}): реакція {R['y'][e].mean():.1%} проти {R['y'][p_].mean():.1%}, Δ {pp(d['diff'])} {ci_str(d['ci'], True)}; E[R] net {net[e].mean():+.3f} проти {net[p_].mean():+.3f}, Δ {dn['diff']:+.3f} {ci_str(dn['ci'])}"
        )
    # Δ_H8 robustness
    m0 = ev & (R["oos"] == 0)
    g1, g0 = m0 & (R["grp"] == 1), m0 & ((R["grp"] == 2) | (R["grp"] == 3))
    y = R["y"].astype(float)
    day = R["day"].astype(np.int64)
    masks = std_masks(R["sym"], R["side"], years, btc_eth)
    robust_rows("Δ_H8 (IS)", y, day, g1, g0, masks)
    dn = bdiff(net[g1], day[g1], net[g0], day[g0], 2000)
    emit(f"- E[R] net тест 1 − тести 2–3 (IS): {dn['diff']:+.3f} {ci_str(dn['ci'])}")
    # H9
    emit("\n**H9 — фактори біля рівня (тест 1):**")
    emit("| група | вибірка | n | дат | реакція | Δ проти «0 факторів» [CI] | E[R] net |\n|---|---|---|---|---|---|---|")
    a = np.nan_to_num(R["aux"], nan=-1).astype(np.int64)
    t1 = ev & (R["grp"] == 1)
    for oos in (0.0, 1.0):
        base0 = t1 & (R["oos"] == oos) & (cnt == 0)
        groups = [(f"факторів {c}", t1 & (R["oos"] == oos) & (cnt == c)) for c in (0, 1, 2, 3)]
        groups.append(("факторів ≥ 2", t1 & (R["oos"] == oos) & (cnt >= 2)))
        for bit, lab in ((1, "FVG"), (2, "EMA"), (4, "EQL")):
            groups.append((f"лише {lab}", t1 & (R["oos"] == oos) & (a == bit)))
            groups.append((f"{lab} (будь-який збіг)", t1 & (R["oos"] == oos) & (a >= 0) & ((a & bit) > 0)))
        for lab, m in groups:
            d = bdiff(y[m], day[m], y[base0], day[base0], 2000)
            emit(
                f"| {lab} | {'OOS' if oos else 'IS'} | {int(m.sum())} | {np.unique(day[m]).size} | {y[m].mean():.1%} | {pp(d['diff'])} {ci_str(d['ci'], True)} | {np.nanmean(net[m]):+.3f} |"
            )
    m0 = t1 & (R["oos"] == 0)
    robust_rows("Δ_H9 (IS)", y, day, m0 & (cnt >= 2), m0 & (cnt == 0), masks)


def h8x_details(R, net, btc_eth, years) -> None:
    emit("\n## H8 — дослідницька перевірка масштабу й вікна очікування (після результатів; рев'ю критика, п. 2)")
    emit("A_arm = ATR(4h) на барі озброєння, A_conf = ATR[f+5] (зареєстрований масштаб). Реакція = +1·A від рівня раніше −1·A.")
    ev = (R["test"] == T["h8x"]) & (R["kind"] == 0)
    y, yc = R["y"].astype(float), R["y2"].astype(float)
    day = R["day"].astype(np.int64)
    emit(
        "| тест | вибірка | n | реакція (A_conf) | реакція (A_arm) | медіана A_arm / A_conf | E[R] net A_conf | E[R] net A_arm |\n|---|---|---|---|---|---|---|---|"
    )
    for oos in (0.0, 1.0):
        for k, lab in ((1, "1"), (2, "2"), (3, "3"), (4, "≥ 4")):
            m = ev & (R["oos"] == oos) & ((R["grp"] == k) if k < 4 else (R["grp"] >= 4))
            emit(
                f"| {lab} | {'OOS' if oos else 'IS'} | {int(m.sum())} | {yc[m].mean():.1%} | {y[m].mean():.1%} | {np.median(R['y3'][m]):.2f} | {np.nanmean(R['ra'][m]):+.3f} | {np.nanmean(net[m]):+.3f} |"
            )
    for oos in (0.0, 1.0):
        m0 = ev & (R["oos"] == oos)
        g1, g0 = m0 & (R["grp"] == 1), m0 & ((R["grp"] == 2) | (R["grp"] == 3))
        for col, lab in ((yc, "A_conf (має відтворити зареєстроване)"), (y, "A_arm")):
            d = diff_block(col, day, g1, g0, 5000)
            emit(f"- Δ_H8 тест 1 − тести 2–3, {lab}, {'OOS' if oos else 'IS'}: {pp(d['diff'])} {ci_str(d['ci'], True)}, p {d['p']:.4f}")
    for oos in (0.0, 1.0):
        e = ev & (R["grp"] == 1) & (R["oos"] == oos)
        for kind, lab in ((3, "чекає 60 барів (як зареєстровано)"), (2, "чекає до кінця життя рівня події (f + 180)")):
            pall = (R["test"] == T["h8x"]) & (R["kind"] == kind) & (R["oos"] == oos)
            p_ = pall & (R["y4"] == 1)
            d = bdiff(y[e], day[e], y[p_], day[p_], 5000)
            dg = bdiff(R["r_gross"][e].astype(float), day[e], R["r_gross"][p_].astype(float), day[p_], 5000)
            emit(
                f"- тест 1 (A_arm) проти P_rand, плацебо {lab}, {'OOS' if oos else 'IS'}: виконано плацебо {R['y4'][pall].mean():.1%}; "
                f"реакція {y[e].mean():.1%} проти {y[p_].mean():.1%}, Δ {pp(d['diff'])} {ci_str(d['ci'], True)}; "
                f"gross {np.nanmean(R['r_gross'][e]):+.3f} проти {np.nanmean(R['r_gross'][p_]):+.3f}, Δ {dg['diff']:+.3f} {ci_str(dg['ci'])}; "
                f"net {np.nanmean(net[e]):+.3f} проти {np.nanmean(net[p_]):+.3f}"
            )
    m0 = ev & (R["oos"] == 0)
    masks = std_masks(R["sym"], R["side"], years, btc_eth)
    robust_rows("Δ_H8 з A_arm (IS)", y, day, m0 & (R["grp"] == 1), m0 & ((R["grp"] == 2) | (R["grp"] == 3)), masks)


def s16_details(R, net, btc_eth, years) -> None:
    emit("\n## S16 / H2 module0 — деталі (вхід за закриттям повернення, ціль — середина рейнджу, ринок 0.07 % на сторону)")
    emit(
        "| TF | вибірка | група | n | дат | M раніше стопу (m = 0 / 0.5 / 1) | P_rand (m = 0) | x̄ (m = 0), п. п. [CI] | E[R] net m = 0 [CI] | P_rand | E[R] net m = 0.5 | P_rand |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for test, tl in (("s16", "1D"), ("s16_4h", "4h")):
        for oos in (0.0, 1.0):
            X = paired_x(R, T[test], 1)
            for g, gl in ((1.0, "девіація"), (0.0, "свип")):
                m = (X["oos"] == oos) & (X["grp"] == g)
                idx = X["idx"][m]
                s = summ(X["x"][m], X["day"][m], 2000)
                ev = (R["test"] == T[test]) & (R["kind"] == 0) & (R["oos"] == oos) & (R["grp"] == g)
                pl = (R["test"] == T[test]) & (R["kind"] == 1) & (R["oos"] == oos) & (R["grp"] == g)
                sn = summ(net[ev], R["day"][ev].astype(np.int64), 2000)
                emit(
                    f"| {tl} | {'OOS' if oos else 'IS'} | {gl} | {s['n']} | {s['days']} | {R['y'][idx].mean():.1%} / {R['y2'][idx].mean():.1%} / {R['y3'][idx].mean():.1%} | {X['yp'][m].mean():.1%} | {s['mean'] * 100:+.1f} {ci_str(s['ci'], True)} | {sn['mean']:+.3f} {ci_str(sn['ci'])} | {np.nanmean(net[pl]):+.3f} | {np.nanmean(R['ra'][ev]):+.3f} | {np.nanmean(R['ra'][pl]):+.3f} |"
                )
            m = X["oos"] == oos
            d = diff_block(X["x"], X["day"], m & (X["grp"] == 1), m & (X["grp"] == 0), 5000)
            emit(
                f"| {tl} | {'OOS' if oos else 'IS'} | **Δ девіація − свип** | {d.get('n')} / {d.get('n0')} | {d.get('days')} | — | — | {pp(d['diff'])} {ci_str(d['ci'], True)}, p {d['p']} | — | — | — | — |"
            )
    X = paired_x(R, T["s16"], 1)
    m = X["oos"] == 0
    idx = X["idx"]
    masks = std_masks(R["sym"][idx], R["side"][idx], years[idx], btc_eth[idx])
    robust_rows(
        "x̄ девіацій (1D, IS)",
        X["x"][m & (X["grp"] == 1)],
        X["day"][m & (X["grp"] == 1)],
        None,
        None,
        [(lab, mm[m & (X["grp"] == 1)]) for lab, mm in masks],
    )
    emit("\n**H2 goodmood — стоп за екстремумом свипу, ціль 2R (той самий R:R), свипи рейнджу:**")
    emit(
        "| TF | вибірка | стоп | n | дат | частка стопів | E[R] net [CI] | парна різниця з m = 0 [CI] | p |\n|---|---|---|---|---|---|---|---|---|"
    )
    for test, tl in (("gm", "1D"), ("gm_4h", "4h")):
        for oos in (0.0, 1.0):
            base_m = (R["test"] == T[test]) & (R["kind"] == 0) & (R["oos"] == oos)
            e0 = base_m & (R["grp"] == 0)
            r0 = dict(zip(R["eid"][e0].tolist(), net[e0].tolist(), strict=True))
            for mi, mv in enumerate(STOP_M):
                e = base_m & (R["grp"] == mi)
                s = summ(net[e], R["day"][e].astype(np.int64), 2000)
                if mi == 0:
                    emit(
                        f"| {tl} | {'OOS' if oos else 'IS'} | {mv} ATR | {s['n']} | {s['days']} | {R['y'][e].mean():.1%} | {s['mean']:+.3f} {ci_str(s['ci'])} | — | — |"
                    )
                    continue
                ids = R["eid"][e]
                keep = np.array([i in r0 for i in ids.tolist()])
                dv = net[e][keep] - np.array([r0[i] for i in ids[keep].tolist()])
                ds = summ(dv, R["day"][e][keep].astype(np.int64), 5000)
                emit(
                    f"| {tl} | {'OOS' if oos else 'IS'} | {mv} ATR | {s['n']} | {s['days']} | {R['y'][e].mean():.1%} | {s['mean']:+.3f} {ci_str(s['ci'])} | {ds['mean']:+.3f} {ci_str(ds['ci'])} | {ds['p']} |"
                )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step7_basics")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "count", "report", "analyse"), required=True)
    ap.add_argument("--symbols", type=int, help="run only the first N symbols (timing sample)")
    ap.add_argument(
        "--tests", default=",".join(ALL_TESTS), help="comma list of h1,h2,h3,h8,s16 (h8 also runs H9); h8x: exploratory H8 re-check"
    )
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    ap.add_argument(
        "--s16-rule",
        choices=("registered", "causal"),
        default="registered",
        help="S16 episode rule: the registered one (looks ahead for sweeps inside a deviation) or the causal fix; use a separate --cache-dir",
    )
    a = ap.parse_args()
    CFG["s16_rule"] = a.s16_rule
    tests = {x.strip() for x in a.tests.split(",") if x.strip()}
    bad = tests - set(ALL_TESTS) - {"h8x"}
    if bad:
        ap.error(f"unknown tests {bad}")
    out_dir = a.cache_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "count":
        stage_count(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.symbols, tests)
    elif a.stage == "analyse":
        stage_analyse(a.s14_cache.expanduser(), out_dir, tests, a.data_dir.expanduser())
    else:
        stage_report(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.symbols, out_dir, tests)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
