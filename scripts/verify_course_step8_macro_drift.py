"""Course setups, step 8 (IDEA-20261016-course-setups-crypto): S19 / H6 — price drift into a macro event.

H6: if BTC rose >= 3 % over the 3 days before a CPI or FOMC release, the return over the next 5 days is lower than
without such a rise. Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 8". Stages:
  uv run python scripts/verify_course_step8_macro_drift.py --stage prereg
  uv run python scripts/verify_course_step8_macro_drift.py --stage count  --out ~/.cache/tradedesk-agents/course_step8_macro_drift/count.md
  uv run python scripts/verify_course_step8_macro_drift.py --stage report --out ~/.cache/tradedesk-agents/course_step8_macro_drift/report.md
No downloads: hourly USD-M perp klines and the monthly universe come from the step-1 cache (--s14-cache, filled from
data.binance.vision through rt.binance_vision); listings from the funding-crowding cache (--data-dir). ``count``
(stage 0) uses only prices up to each release (pre-event moves, group sizes); ``report`` computes everything.
No keys, no orders.
"""

from __future__ import annotations

import argparse
import hashlib
import resource
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders and universe)

from tradedesk.analytics.course_macro_drift import (  # noqa: E402
    COST_RT,
    HOUR_MS,
    daily_vol,
    diff_draws,
    episodes,
    fade_net,
    group_diff,
    h6_verdict,
    mde,
    mean_boot,
    placebo_days,
    price_at,
    shifted_days,
    slope_boot,
)
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402
from tradedesk.analytics.macro_regimes import ny_time_ms  # noqa: E402
from tradedesk.analytics.macro_release import cochran_q, parse_events_csv, release_ms  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
EVENTS_CSV = ROOT / "docs" / "verification" / "2026-10-15-us-macro-events.csv"
OUT: dict[str, Any] = {"fh": None}

FIRST_DAY = date(2021, 1, 1)
END_MS = s1.ms("2026-10-01T00:00")  # end of the hourly cache (last bar closes here)
OOS_MS = s1.ms("2025-03-01T00:00")
PRE_H, POST_H, THR = 72, 120, 0.03
MIN_CLUSTERS, GATE, MIN_EFFECT = 20, 0.04, -0.02
STEP_P = (0.985, 0.280, 0.448, 0.038, 0.822, 1.0, 0.394)  # raw primary p of steps 1-7 (Holm family of the card)
SEED, N_BOOT = 20261017, 10_000
FUND_5D = 0.0015  # funding stress: 0.01 % per 8 h against the position over 5 days
MAX_GAP = 48 * HOUR_MS
GRID_THR, GRID_PRE, GRID_POST = (0.02, 0.03, 0.05), (48, 72, 120), (24, 72, 120, 240)


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def utc(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def pct(x: float | None, d: int = 2) -> str:
    return "—" if x is None or not np.isfinite(x) else f"{x:+.{d}%}"


def ci_s(ci: tuple[float, float] | None, d: int = 2) -> str:
    return "—" if ci is None else f"[{ci[0]:+.{d}%}; {ci[1]:+.{d}%}]"


def fp(p: float | None) -> str:
    return "—" if p is None else f"{p:.3f}"


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    raw = CARD.read_bytes()
    t = raw.decode()
    a = t.index("## Попередня реєстрація — крок 8")
    b = t.index("\n## ", a + 1)
    sec = t[a:b].encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()  # = git hash-object
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)} card_blob={blob}")


# ---------------------------------------------------------------- events and times


class Times:
    """A set of event (or placebo) times with labels."""

    def __init__(self, t0: list[int], day: list[date], kind: list[str]) -> None:
        self.t0 = np.array(t0, dtype=np.int64)
        self.day = day
        self.kind = np.array(kind)
        self.is_ = self.t0 < OOS_MS

    def __len__(self) -> int:
        return int(self.t0.size)


def load_events() -> tuple[list[Any], list[tuple[date, str]]]:
    evs = [e for e in parse_events_csv(EVENTS_CSV.read_text()) if e.status != "cancelled"]
    return evs, [(e.day, e.kind) for e in evs]


def event_times(kinds: tuple[str, ...], post_h: int = POST_H) -> Times:
    evs, _ = load_events()
    rows = sorted(
        (release_ms(e.day, e.kind), e.day, e.kind)
        for e in evs
        if e.kind in kinds and e.day >= FIRST_DAY and release_ms(e.day, e.kind) + post_h * HOUR_MS <= END_MS
    )
    return Times([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])


def placebo_times(hh: int, mm: int, post_h: int = POST_H) -> Times:
    _, all_ev = load_events()
    last = datetime.fromtimestamp((END_MS - post_h * HOUR_MS) / 1000, tz=UTC).date()
    days = [d for d in placebo_days(FIRST_DAY, last, all_ev) if ny_time_ms(d, hh, mm) + post_h * HOUR_MS <= END_MS]
    return Times([ny_time_ms(d, hh, mm) for d in days], days, ["placebo"] * len(days))


def shift_times(ev: Times) -> Times:
    _, all_ev = load_events()
    main = [d for d, k in all_ev if k in ("cpi", "fomc")]
    rows = []
    for d, k in zip(ev.day, ev.kind, strict=True):
        for x, _s in shifted_days([d], main):
            hh, mm = (8, 30) if k != "fomc" else (14, 0)
            t = ny_time_ms(x, hh, mm)
            if t + POST_H * HOUR_MS <= END_MS and x >= FIRST_DAY:
                rows.append((t, x, k))
    rows.sort()
    return Times([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])


def month_cl(T: Times) -> np.ndarray:
    return np.array([d.year * 100 + d.month for d in T.day])


def week_cl(T: Times) -> np.ndarray:
    return np.array([d.isocalendar()[0] * 100 + d.isocalendar()[1] for d in T.day])


# ---------------------------------------------------------------- measurement


def gap_free(o: np.ndarray, a_ms: int, b_ms: int) -> bool:
    i0 = max(0, int(np.searchsorted(o, a_ms - HOUR_MS)) - 1)
    i1 = int(np.searchsorted(o, b_ms, side="right"))
    seg = o[i0 : i1 + 1]
    return seg.size >= 2 and int(np.diff(seg).max(initial=0)) <= MAX_GAP


def measure(
    o: np.ndarray, c: np.ndarray, T: Times, pre_h: int = PRE_H, post_h: int = POST_H, delay_h: int = 0, outcomes: bool = True
) -> dict[str, np.ndarray]:
    n = len(T)
    pre, post, vol = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for i, t0 in enumerate(T.t0.tolist()):
        end = t0 + (delay_h + post_h) * HOUR_MS if outcomes else t0
        if not gap_free(o, t0 - pre_h * HOUR_MS, end):
            continue
        pa, p0 = price_at(o, c, t0 - pre_h * HOUR_MS), price_at(o, c, t0)
        if pa is None or p0 is None:
            continue
        pre[i] = p0 / pa - 1.0
        v = daily_vol(o, c, t0)
        vol[i] = np.nan if v is None else v
        if outcomes:
            pe, pb = price_at(o, c, t0 + delay_h * HOUR_MS), price_at(o, c, end)
            if pe is not None and pb is not None:
                post[i] = pb / pe - 1.0
    return {"pre": pre, "post": post, "vol": vol}


def load(cache: Path, data_dir: Path, sym: str) -> tuple[np.ndarray, np.ndarray]:
    B = s1.load_1h(cache, data_dir, sym)
    return B.t.astype(np.int64), B.c


# ---------------------------------------------------------------- stage 0


def sanity(o: np.ndarray, c: np.ndarray) -> None:
    emit(f"- BTCUSDT 1h: {utc(int(o[0]))} … {utc(int(o[-1]))}, {o.size} барів; розривів > 1 год: {int(np.sum(np.diff(o) > HOUR_MS))}")
    emit(f"- закриття 2024-12-31 (ціна на 2025-01-01 00:00 UTC) = {price_at(o, c, s1.ms('2025-01-01T00:00'))} (еталон кроків 1–7: 93548.9)")
    emit(
        f"- t0 у UTC: CPI 2024-12-11 → {utc(release_ms(date(2024, 12, 11), 'cpi'))}; FOMC 2024-07-31 → {utc(release_ms(date(2024, 7, 31), 'fomc'))}; "
        f"CPI 2024-07-11 → {utc(release_ms(date(2024, 7, 11), 'cpi'))}; FOMC 2024-12-18 → {utc(release_ms(date(2024, 12, 18), 'fomc'))}"
    )


def counts_table(T: Times, m: dict[str, np.ndarray], label: str) -> None:
    ok = np.isfinite(m["pre"])
    cl = episodes(T.t0.tolist(), PRE_H * HOUR_MS, POST_H * HOUR_MS)
    emit(f"\n### {label}: {len(T)} моментів, з ціною {int(ok.sum())} (відкинуто {int((~ok).sum())})")
    emit("| період | n | епізодів | U: pre ≥ +3 % (епізодів) | pre ≤ −3 % | медіана pre |\n|---|---|---|---|---|---|")
    for per, mk in (("IS", T.is_), ("OOS", ~T.is_), ("усе", np.ones(len(T), bool))):
        s = ok & mk
        u = s & (m["pre"] >= THR)
        emit(
            f"| {per} | {int(s.sum())} | {np.unique(cl[s]).size} | {int(u.sum())} ({np.unique(cl[u]).size}) | {int((s & (m['pre'] <= -THR)).sum())} | "
            f"{pct(float(np.median(m['pre'][s])) if s.any() else None)} |"
        )


def stage_count(cache: Path, data_dir: Path) -> None:
    emit("# Крок 8 (S19 / H6), стадія 0: події й рух до них (без дохідностей після)")
    o, c = load(cache, data_dir, "BTCUSDT")
    emit("\n## Санітарні перевірки")
    sanity(o, c)
    ev = event_times(("cpi", "fomc"))
    by = Counter((d.year, k) for d, k in zip(ev.day, ev.kind, strict=True))
    emit(
        "- подій CPI / FOMC за роками: " + "; ".join(f"{y}: {by[(y, 'cpi')]} / {by[(y, 'fomc')]}" for y in sorted({d.year for d in ev.day}))
    )
    m = measure(o, c, ev, outcomes=False)
    counts_table(ev, m, "BTC, CPI + FOMC")
    for k in ("cpi", "fomc"):
        T = event_times((k,))
        counts_table(T, measure(o, c, T, outcomes=False), f"BTC, лише {k.upper()}")
    P = placebo_times(8, 30)
    counts_table(P, measure(o, c, P, outcomes=False), "BTC, плацебо P_day 08:30 ET")
    S = shift_times(ev)
    counts_table(S, measure(o, c, S, outcomes=False), "BTC, плацебо P_shift (±14 / ±21 доба)")
    oe, ce = load(cache, data_dir, "ETHUSDT")
    counts_table(ev, measure(oe, ce, ev, outcomes=False), "ETH, CPI + FOMC")
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


# ---------------------------------------------------------------- report helpers


def gd_line(lab: str, r: dict[str, Any], p_key: str = "p_neg") -> str:
    return (
        f"| {lab} | {r['n_in']} ({r['clusters_in']}) | {r['n_out']} | {pct(r['stat'])} | {ci_s(r.get('ci'))} | {fp(r.get(p_key))} | "
        f"{pct(mde(r['se']) if np.isfinite(r.get('se', np.nan)) else None)} |"
    )


GD_HDR = "| вибірка | n у групі (епізодів) | n решти | Δ | 95 % CI | p однобічне | MDE |\n|---|---|---|---|---|---|---|"


def run_gd(
    m: dict[str, np.ndarray], T: Times, mask: np.ndarray, cl: np.ndarray, grp: np.ndarray, seed: int = SEED, n_boot: int = N_BOOT
) -> dict[str, Any]:
    s = mask & np.isfinite(m["pre"]) & np.isfinite(m["post"])
    return group_diff(m["post"][s], grp[s], cl[s], n_boot=n_boot, seed=seed)


def ev_clusters(T: Times, pre_h: int = PRE_H, post_h: int = POST_H) -> np.ndarray:
    return episodes(T.t0.tolist(), pre_h * HOUR_MS, post_h * HOUR_MS)


def means_line(m: dict[str, np.ndarray], mask: np.ndarray, grp: np.ndarray) -> str:
    s = mask & np.isfinite(m["post"]) & np.isfinite(m["pre"])
    a, b = m["post"][s & grp], m["post"][s & ~grp]
    return (
        f"середнє post: у групі {pct(float(a.mean()) if a.size else None)} (медіана {pct(float(np.median(a)) if a.size else None)}, "
        f"частка > 0 {np.mean(a > 0) if a.size else float('nan'):.0%}); решта {pct(float(b.mean()) if b.size else None)} "
        f"(медіана {pct(float(np.median(b)) if b.size else None)}, частка > 0 {np.mean(b > 0) if b.size else float('nan'):.0%})"
    )


def dd_line(lab: str, e: dict[str, Any], p: dict[str, Any], p_key: str = "p_neg") -> str:
    d = diff_draws(e["draws"], p["draws"], e["stat"], p["stat"])
    return f"| {lab} | {pct(e['stat'])} | {pct(p['stat'])} | {pct(d['stat'])} | {ci_s(d['ci'])} | {fp(d.get(p_key))} |"


DD_HDR = "| порівняння | Δ подій | Δ плацебо | DD = різниця | 95 % CI DD | p однобічне |\n|---|---|---|---|---|---|"


# ---------------------------------------------------------------- report


def stage_report(cache: Path, data_dir: Path) -> None:
    emit("# Крок 8 (S19 / H6): рух у макроподію — результати")
    emit(
        f"BTCUSDT перп Binance (годинні свічки кешу кроку 1); події CPI і FOMC з `2026-10-15-us-macro-events.csv` без `cancelled`, з {FIRST_DAY}; "
        f"pre — {PRE_H} год до релізу, post — {POST_H} год від останньої ціни до релізу; U: pre ≥ +{THR:.0%}. IS: t0 < 2025-03-01 ≤ OOS. "
        "Дохідності — частки на номінал; Δ — різниця середніх post «U − решта»; CI — кластерний бутстреп за епізодами подій (плацебо — за місяцями), "
        f"{N_BOOT} повторів, сід {SEED}."
    )
    o, c = load(cache, data_dir, "BTCUSDT")
    emit("\n## Санітарні перевірки")
    sanity(o, c)

    ev = event_times(("cpi", "fomc"))
    cl = ev_clusters(ev)
    m = measure(o, c, ev)
    P = placebo_times(8, 30)
    mp = measure(o, c, P)
    pcl = month_cl(P)
    allm = np.ones(len(ev), bool)
    pall = np.ones(len(P), bool)
    miss = int(np.sum(~np.isfinite(m["post"])))
    emit(
        f"- подій CPI + FOMC: {len(ev)} (IS {int(ev.is_.sum())}, OOS {int((~ev.is_).sum())}); без ціни: {miss}; днів плацебо P_day: {len(P)} (IS {int(P.is_.sum())})"
    )

    # ---- events table
    emit("\n## Події (BTC): рух до релізу і 5 діб після")
    emit("| t0 (UTC) | вид | епізод | pre 72 год | post 120 год | група | період |\n|---|---|---|---|---|---|---|")
    for i in range(len(ev)):
        g = "U" if m["pre"][i] >= THR else ("D" if m["pre"][i] <= -THR else "")
        emit(
            f"| {utc(int(ev.t0[i]))} | {ev.kind[i]} | {cl[i]} | {pct(m['pre'][i])} | {pct(m['post'][i])} | {g} | {'IS' if ev.is_[i] else 'OOS'} |"
        )

    # ---- primary
    U = m["pre"] >= THR
    Up = mp["pre"] >= THR
    e_is = run_gd(m, ev, ev.is_, cl, U)
    p_is = run_gd(mp, P, P.is_, pcl, Up, seed=SEED + 1)
    dd = diff_draws(e_is["draws"], p_is["draws"], e_is["stat"], p_is["stat"])
    e_oos = run_gd(m, ev, ~ev.is_, cl, U)
    p_oos = run_gd(mp, P, ~P.is_, pcl, Up, seed=SEED + 1)
    e_all = run_gd(m, ev, allm, cl, U)
    p_all = run_gd(mp, P, pall, pcl, Up, seed=SEED + 1)
    p8 = e_is["p_neg"]
    p_adj = holm([*STEP_P, p8])[7] if p8 is not None else None
    mde_v = mde(e_is["se"])
    vd = h6_verdict(
        delta=e_is["stat"],
        ci=e_is["ci"],
        clusters_u=e_is["clusters_in"],
        mde_value=mde_v,
        dd=dd["stat"],
        dd_ci=dd["ci"],
        p_adj=p_adj,
        oos_n_u=e_oos["n_in"],
        oos_delta=e_oos["stat"],
    )
    emit("\n## Основна метрика (H6, BTC, CPI + FOMC): Δ_E = E[post | pre ≥ +3 %] − E[post | решта]")
    emit(GD_HDR)
    emit(gd_line("події IS", e_is))
    emit(gd_line("плацебо P_day IS (08:30 ET, дні без подій)", p_is))
    emit(gd_line("події OOS", e_oos))
    emit(gd_line("плацебо P_day OOS", p_oos))
    emit(gd_line("події, увесь період", e_all))
    emit(gd_line("плацебо P_day, увесь період", p_all))
    emit(f"- події IS: {means_line(m, ev.is_, U)}")
    emit(f"- плацебо IS: {means_line(mp, P.is_, Up)}")
    emit(f"- події OOS: {means_line(m, ~ev.is_, U)}")
    emit("\n" + DD_HDR)
    emit(dd_line("IS: події − плацебо", e_is, p_is))
    emit(dd_line("OOS: події − плацебо", e_oos, p_oos))
    emit(dd_line("увесь період", e_all, p_all))
    emit(
        f"\n- сире p (Δ_E < 0) = {fp(p8)}; Holm (m = 8: {', '.join(map(str, STEP_P))}, крок 8) = {fp(p_adj)}; ×11 — {fp(min(1.0, 11 * p8) if p8 is not None else None)}"
    )
    emit(
        f"- ворота: епізодів U в IS {e_is['clusters_in']} (поріг {MIN_CLUSTERS}); MDE = 2.8·SE = {pct(mde_v)} (поріг {GATE:.0%}); мінімальний ефект {MIN_EFFECT:+.0%}"
    )
    emit(f"- **вердикт кроку 8 за реєстрацією: {vd}**")

    # ---- S19 mirror and fade trade
    emit("\n## S19 дзеркально: падіння ≤ −3 % у подію — дно? Δ = E[post | pre ≤ −3 %] − E[post | решта] (теза > 0)")
    D, Dp = m["pre"] <= -THR, mp["pre"] <= -THR
    emit(GD_HDR)
    rows = []
    for lab, mk, pk in (("IS", ev.is_, P.is_), ("OOS", ~ev.is_, ~P.is_), ("усе", allm, pall)):
        a = run_gd(m, ev, mk, cl, D)
        b = run_gd(mp, P, pk, pcl, Dp, seed=SEED + 1)
        rows.append((lab, a, b))
        emit(gd_line(f"події {lab}", a, "p_pos"))
        emit(gd_line(f"плацебо {lab}", b, "p_pos"))
    emit("\n" + DD_HDR.replace("p однобічне", "p однобічне (DD > 0)"))
    for lab, a, b in rows:
        emit(dd_line(lab, a, b, "p_pos"))

    emit("\n## Угода S19 / fade: проти руху до події при |pre| ≥ 3 %, вхід P(t0), вихід через 120 год; net на номінал (без плеча)")
    emit("| вибірка | бік | n (епізодів) | E[net] | 95 % CI | p (> 0) | плацебо E[net] | різниця [CI] |\n|---|---|---|---|---|---|---|---|")
    for lab, mk, pk in (("IS", ev.is_, P.is_), ("OOS", ~ev.is_, ~P.is_), ("усе", allm, pall)):
        for side, sl in ((0, "обидва"), (1, "шорт після росту"), (-1, "лонг після падіння")):
            fe = fade_net(m["pre"], m["post"], THR, side=side)
            fpb = fade_net(mp["pre"], mp["post"], THR, side=side)
            se_ = mk & np.isfinite(fe)
            sp_ = pk & np.isfinite(fpb)
            a = mean_boot(fe[se_], cl[se_], n_boot=N_BOOT, seed=SEED)
            b = mean_boot(fpb[sp_], pcl[sp_], n_boot=N_BOOT, seed=SEED + 1)
            d = diff_draws(a["draws"], b["draws"], a["stat"], b["stat"])
            emit(
                f"| {lab} | {sl} | {a['n']} ({a.get('clusters', 0)}) | {pct(a['stat'])} | {ci_s(a.get('ci'))} | {fp(a.get('p_pos'))} | {pct(b['stat'])} | "
                f"{pct(d['stat'])} {ci_s(d['ci'])} |"
            )
    fe = fade_net(m["pre"], m["post"], THR)
    s_ = ev.is_ & np.isfinite(fe)
    gross = fe[s_] + COST_RT
    emit(
        f"- стрес IS (обидва боки): gross {pct(float(gross.mean()))}; витрати ×2 {pct(float((gross - 2 * COST_RT).mean()))}; ×3 {pct(float((gross - 3 * COST_RT).mean()))}; "
        f"фандинг −0.15 % за 5 діб {pct(float((fe[s_] - FUND_5D).mean()))}"
    )

    # ---- by kind
    emit("\n## За видами (H6, U: pre ≥ +3 %)")
    P14 = placebo_times(14, 0)
    mp14 = measure(o, c, P14)
    emit(GD_HDR)
    kind_rows = []
    for k, PP, mpp in (("cpi", P, mp), ("fomc", P14, mp14), ("nfp", P, mp)):
        T = event_times((k,))
        mk = measure(o, c, T)
        clk = ev_clusters(T)
        Uk = mk["pre"] >= THR
        for lab, msk, pmsk in (("IS", T.is_, PP.is_), ("OOS", ~T.is_, ~PP.is_)):
            a = run_gd(mk, T, msk, clk, Uk)
            b = run_gd(mpp, PP, pmsk, month_cl(PP), mpp["pre"] >= THR, seed=SEED + 1)
            emit(gd_line(f"{k.upper()} {lab}", a))
            kind_rows.append((f"{k.upper()} {lab}", a, b))
    emit("\n" + DD_HDR)
    for lab, a, b in kind_rows:
        emit(dd_line(lab, a, b))
    emit("(плацебо для FOMC — ті самі дні P_day о 14:00 ET; для CPI і NFP — о 08:30 ET; NFP поза H6, описово)")

    # ---- grid
    emit("\n## Сітка (IS, BTC, CPI + FOMC): поріг × вікно pre × горизонт post")
    emit(
        "| поріг | pre, год | post, год | n U (еп.) | Δ_E | 95 % CI | p (Δ_E < 0) | Δ_P | DD [CI] | p (DD < 0) |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    sec_p: list[tuple[str, float]] = []
    for thr in GRID_THR:
        for ph in GRID_PRE:
            for qh in GRID_POST:
                Tg = event_times(("cpi", "fomc"), post_h=qh)
                Pg = placebo_times(8, 30, post_h=qh)
                mg, mpg = measure(o, c, Tg, ph, qh), measure(o, c, Pg, ph, qh)
                clg = ev_clusters(Tg, ph, qh)
                a = run_gd(mg, Tg, Tg.is_, clg, mg["pre"] >= thr, n_boot=4000)
                b = run_gd(mpg, Pg, Pg.is_, month_cl(Pg), mpg["pre"] >= thr, seed=SEED + 1, n_boot=4000)
                d = diff_draws(a["draws"], b["draws"], a["stat"], b["stat"])
                prim = (thr, ph, qh) == (THR, PRE_H, POST_H)
                emit(
                    f"| {thr:.0%}{' (осн.)' if prim else ''} | {ph} | {qh} | {a['n_in']} ({a['clusters_in']}) | {pct(a['stat'])} | {ci_s(a.get('ci'))} | {fp(a.get('p_neg'))} | "
                    f"{pct(b['stat'])} | {pct(d['stat'])} {ci_s(d['ci'])} | {fp(d.get('p_neg'))} |"
                )
                if not prim and a.get("p_neg") is not None:
                    sec_p.append((f"{thr:.0%} / {ph} / {qh}", a["p_neg"]))
    adj = holm([p for _, p in sec_p])
    best = sorted(zip(sec_p, adj, strict=True), key=lambda x: x[0][1])[:5]
    emit(
        f"- Holm усередині кроку для {len(sec_p)} другорядних клітинок Δ_E: найменші сирі p — "
        + "; ".join(f"{lab}: {p:.3f} → {pa:.3f}" for (lab, p), pa in best)
    )

    # ---- continuous slope, vol threshold, shifted placebo
    emit("\n## Неперервно: нахил OLS post на pre (IS)")
    s_ = ev.is_ & np.isfinite(m["post"]) & np.isfinite(m["pre"])
    sp_ = P.is_ & np.isfinite(mp["post"]) & np.isfinite(mp["pre"])
    se_ = slope_boot(m["pre"][s_], m["post"][s_], cl[s_], n_boot=N_BOOT, seed=SEED)
    sp = slope_boot(mp["pre"][sp_], mp["post"][sp_], pcl[sp_], n_boot=N_BOOT, seed=SEED + 1)
    d = diff_draws(se_["draws"], sp["draws"], se_["stat"], sp["stat"])
    emit(
        f"- події: {se_['stat']:+.3f} [{se_['ci'][0]:+.3f}; {se_['ci'][1]:+.3f}]; плацебо: {sp['stat']:+.3f} [{sp['ci'][0]:+.3f}; {sp['ci'][1]:+.3f}]; "
        f"різниця {d['stat']:+.3f} [{d['ci'][0]:+.3f}; {d['ci'][1]:+.3f}], p (< 0) {fp(d['p_neg'])}"
    )
    emit("\n## Поріг у волатильності: U — pre ≥ 1.0·σ_30д·√3")
    Uv = np.isfinite(m["vol"]) & (m["pre"] >= m["vol"] * np.sqrt(3))
    Uvp = np.isfinite(mp["vol"]) & (mp["pre"] >= mp["vol"] * np.sqrt(3))
    emit(GD_HDR)
    a = run_gd(m, ev, ev.is_ & np.isfinite(m["vol"]), cl, Uv)
    b = run_gd(mp, P, P.is_ & np.isfinite(mp["vol"]), pcl, Uvp, seed=SEED + 1)
    emit(gd_line("події IS", a))
    emit(gd_line("плацебо IS", b))
    emit(gd_line("події OOS", run_gd(m, ev, ~ev.is_ & np.isfinite(m["vol"]), cl, Uv)))
    emit(DD_HDR)
    emit(dd_line("IS", a, b))
    emit("\n## Плацебо P_shift: ті самі події, зсунуті на ±14 / ±21 добу (якщо поруч немає CPI / FOMC)")
    S = shift_times(ev)
    ms_ = measure(o, c, S)
    b = run_gd(ms_, S, S.is_, month_cl(S), ms_["pre"] >= THR, seed=SEED + 2)
    emit(GD_HDR)
    emit(gd_line("P_shift IS", b))
    emit(DD_HDR)
    emit(dd_line("IS: події − P_shift", e_is, b))

    # ---- ETH replication
    emit("\n## Реплікація ETH (ті самі правила)")
    oe, ce = load(cache, data_dir, "ETHUSDT")
    me, mpe = measure(oe, ce, ev), measure(oe, ce, P)
    emit(GD_HDR)
    eth_rows = []
    for lab, mk, pk in (("IS", ev.is_, P.is_), ("OOS", ~ev.is_, ~P.is_)):
        a = run_gd(me, ev, mk, cl, me["pre"] >= THR)
        b = run_gd(mpe, P, pk, pcl, mpe["pre"] >= THR, seed=SEED + 1)
        emit(gd_line(f"ETH події {lab}", a))
        emit(gd_line(f"ETH плацебо {lab}", b))
        eth_rows.append((lab, a, b))
    emit(DD_HDR)
    for lab, a, b in eth_rows:
        emit(dd_line(f"ETH {lab}", a, b))

    # ---- robustness
    robustness(o, c, ev, cl, m, U, e_is)

    # ---- universe and per-asset check
    universe(cache, data_dir, ev, cl, P, pcl)
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def robustness(
    o: np.ndarray, c: np.ndarray, ev: Times, cl: np.ndarray, m: dict[str, np.ndarray], U: np.ndarray, e_is: dict[str, Any]
) -> None:
    emit("\n## Стійкість основної метрики (IS)")
    s = ev.is_ & np.isfinite(m["post"]) & np.isfinite(m["pre"])
    x, g, k = m["post"][s], U[s], cl[s]

    def delta(keep: np.ndarray) -> float:
        return float(x[keep & g].mean() - x[keep & ~g].mean()) if (keep & g).any() and (keep & ~g).any() else float("nan")

    keep = np.ones(x.size, bool)
    dropped = []
    for _ in range(5):
        cand = [(delta(keep & (k != q)), q) for q in np.unique(k[keep])]
        best = max(cand)  # removing this episode raises Δ the most (it supports the thesis most)
        keep &= k != best[1]
        dropped.append(best)
        if len(dropped) in (1, 3, 5):
            emit(f"- без {len(dropped)} найсприятливіших для тези епізодів: Δ_E {pct(best[0])}")
    emit(f"  (вилучені епізоди: {', '.join(str(q) for _, q in dropped)})")
    yrs = sorted({d.year for d in ev.day})
    parts = []
    for y in yrs:
        mk = np.array([d.year == y for d in ev.day])
        r = group_diff(m["post"][mk & np.isfinite(m["post"])], U[mk & np.isfinite(m["post"])], cl[mk & np.isfinite(m["post"])], n_boot=1000)
        parts.append(f"{y}: U {r['n_in']} / {r['n_out']}, Δ {pct(r['stat'])}")
    emit("- за роками (усі періоди): " + "; ".join(parts))
    t_is = ev.t0[ev.is_]
    med = int(np.median(t_is))
    for lab, mk in (("перша половина IS", ev.is_ & (ev.t0 <= med)), ("друга половина IS", ev.is_ & (ev.t0 > med))):
        r = run_gd(m, ev, mk, cl, U, n_boot=4000)
        emit(f"- {lab}: U {r['n_in']} / {r['n_out']}, Δ {pct(r['stat'])} {ci_s(r.get('ci'))}")
    m1 = measure(o, c, ev, delay_h=1)
    r = run_gd(m1, ev, ev.is_, cl, m1["pre"] >= THR)
    emit(f"- вхід на 1 год пізніше (після реакції): Δ {pct(r['stat'])} {ci_s(r.get('ci'))}, p {fp(r.get('p_neg'))}")
    r = run_gd(m, ev, ev.is_, week_cl(ev), U)
    emit(f"- кластери за ISO-тижнем: Δ {pct(r['stat'])} {ci_s(r.get('ci'))}, p {fp(r.get('p_neg'))}")
    emit(f"- медіани post: U {pct(float(np.median(x[g])))}, решта {pct(float(np.median(x[~g])))}; основний Δ_E {pct(e_is['stat'])}")


def universe(cache: Path, data_dir: Path, ev: Times, cl: np.ndarray, P: Times, pcl: np.ndarray) -> None:
    emit("\n## Всесвіт (point-in-time): BTC, ETH + топ-30 місяця t0, свої pre і post; кластери — епізоди подій")
    uni = s1.members(cache)
    syms = sorted({s for v in uni.values() for s in v})
    ev_m = [s1.month_of(int(t)) for t in ev.t0]
    p_m = [s1.month_of(int(t)) for t in P.t0]
    rows: list[tuple[str, int, float, float, bool]] = []  # sym, event index, pre, post, is
    prow: list[tuple[str, int, float, float, bool]] = []
    t0 = time.time()
    for j, s in enumerate(syms):
        ie = [i for i in range(len(ev)) if s in uni.get(ev_m[i], [])]
        ip = [i for i in range(len(P)) if s in uni.get(p_m[i], [])]
        if not ie and not ip:
            continue
        o, c = load(cache, data_dir, s)
        if o.size == 0:
            continue
        if ie:
            sub = Times([int(ev.t0[i]) for i in ie], [ev.day[i] for i in ie], [str(ev.kind[i]) for i in ie])
            mm = measure(o, c, sub)
            for q, i in enumerate(ie):
                if np.isfinite(mm["pre"][q]) and np.isfinite(mm["post"][q]):
                    rows.append((s, i, float(mm["pre"][q]), float(mm["post"][q]), bool(ev.is_[i])))
        if ip:
            sub = Times([int(P.t0[i]) for i in ip], [P.day[i] for i in ip], ["placebo"] * len(ip))
            mm = measure(o, c, sub)
            for q, i in enumerate(ip):
                if np.isfinite(mm["pre"][q]) and np.isfinite(mm["post"][q]):
                    prow.append((s, i, float(mm["pre"][q]), float(mm["post"][q]), bool(P.is_[i])))
        if j % 50 == 0:
            print(
                f"  [universe] {j + 1}/{len(syms)} {s} rows {len(rows)} / {len(prow)} {time.time() - t0:.0f}s rss {rss_mb():.0f} MB",
                flush=True,
            )
    sym = np.array([r[0] for r in rows])
    eidx = np.array([r[1] for r in rows])
    pre = np.array([r[2] for r in rows])
    post = np.array([r[3] for r in rows])
    isr = np.array([r[4] for r in rows])
    ecl = cl[eidx]
    ppre = np.array([r[2] for r in prow])
    ppost = np.array([r[3] for r in prow])
    pis = np.array([r[4] for r in prow])
    pcls = pcl[np.array([r[1] for r in prow])]
    emit(f"- символо-подій: {len(rows)} (символів {np.unique(sym).size}); символо-днів плацебо: {len(prow)}")
    emit(GD_HDR)
    res = {}
    for lab, mk, pk in (("IS", isr, pis), ("OOS", ~isr, ~pis)):
        a = group_diff(post[mk], pre[mk] >= THR, ecl[mk], n_boot=N_BOOT, seed=SEED)
        b = group_diff(ppost[pk], ppre[pk] >= THR, pcls[pk], n_boot=N_BOOT, seed=SEED + 1)
        res[lab] = (a, b)
        emit(gd_line(f"всесвіт, події {lab}", a))
        emit(gd_line(f"всесвіт, плацебо {lab}", b))
    emit(DD_HDR)
    for lab, (a, b) in res.items():
        emit(dd_line(f"всесвіт {lab}", a, b))
    alts = ~np.isin(sym, ["BTCUSDT", "ETHUSDT"])
    a = group_diff(post[isr & alts], pre[isr & alts] >= THR, ecl[isr & alts], n_boot=N_BOOT, seed=SEED)
    emit(f"- лише альти (без BTC, ETH), IS: Δ {pct(a['stat'])} {ci_s(a.get('ci'))}, U {a['n_in']} ({a['clusters_in']} еп.)")
    # a second, market-wide grouping: BTC's own pre classifies every symbol of the event
    btc_pre = {i: p for s_, i, p, _, _ in rows if s_ == "BTCUSDT"}
    gb = np.array([btc_pre.get(int(i), np.nan) >= THR for i in eidx])
    a = group_diff(post[isr], gb[isr], ecl[isr], n_boot=N_BOOT, seed=SEED)
    emit(f"- група за ростом BTC (а не самої монети), IS: Δ {pct(a['stat'])} {ci_s(a.get('ci'))}")

    emit("\n### Перевірка по монетах (IS; символи з ≥ 5 подій U і ≥ 5 решти)")
    emit("| символ | n U | n решти | Δ_E | 95 % CI | SE |\n|---|---|---|---|---|---|")
    per: list[tuple[str, float, float]] = []
    for s in sorted(set(sym.tolist())):
        mk = isr & (sym == s)
        g = pre[mk] >= THR
        if g.sum() < 5 or (~g).sum() < 5:
            continue
        r = group_diff(post[mk], g, ecl[mk], n_boot=2000, seed=SEED)
        if not np.isfinite(r["se"]) or r["se"] <= 0:
            continue
        per.append((s, r["stat"], r["se"]))
        emit(f"| {s} | {r['n_in']} | {r['n_out']} | {pct(r['stat'])} | {ci_s(r.get('ci'))} | {pct(r['se'])} |")
    if len(per) >= 2:
        q = cochran_q([p[1] for p in per], [p[2] for p in per])
        emit(
            f"- Cochran's Q = {q['Q']:.1f}, df {q['df']}, p {q['p']:.3f}, I² {q['I2']:.0%}; зважене середнє Δ {pct(q['pooled'])} "
            "(SE по монетах ігнорують кореляцію монет усередині однієї події, тож Q — орієнтир)"
        )
        ranked = sorted(per, key=lambda x: x[1])
        k = max(1, len(ranked) // 4)
        top = {p[0] for p in ranked[:k]}
        emit(f"- сталість: топ-чверть за найбільш від'ємним Δ_E в IS ({k}): {', '.join(sorted(top))}")
        for lab, sel in (("топ-чверть", np.isin(sym, list(top))), ("решта", ~np.isin(sym, list(top)) & np.isin(sym, [p[0] for p in per]))):
            mk = ~isr & sel
            r = group_diff(post[mk], pre[mk] >= THR, ecl[mk], n_boot=2000, seed=SEED)
            emit(f"  - {lab} в OOS: U {r['n_in']} / {r['n_out']}, Δ {pct(r['stat'])} {ci_s(r.get('ci'))}")
    else:
        emit("- менше двох символів з достатньою кількістю подій — неоднорідність не перевіряється")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step8_macro_drift")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--stage", choices=("prereg", "count", "report"), required=True)
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    a.cache_dir.expanduser().mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    s14, data_dir = a.s14_cache.expanduser(), a.data_dir.expanduser()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "count":
        stage_count(s14, data_dir)
    else:
        stage_report(s14, data_dir)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
