"""Course setups, step 6 (IDEA-20261016-course-setups-crypto): S13 — buying BTC after a drawdown of -10 / -15 / -30 %
from its running maximum; forward returns at 30 / 60 / 90 days against a random day (descriptive, not a strategy).

Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 6". Stages:
  uv run python scripts/verify_course_step6_s13.py --stage prereg
  uv run python scripts/verify_course_step6_s13.py --stage fetch  --out ~/.cache/tradedesk-agents/course_step6_s13/fetch.log
  uv run python scripts/verify_course_step6_s13.py --stage count  --out ~/.cache/tradedesk-agents/course_step6_s13/count.md
  uv run python scripts/verify_course_step6_s13.py --stage report --out ~/.cache/tradedesk-agents/course_step6_s13/report.md
``fetch`` downloads Binance spot daily klines (BTCUSDT, ETHUSDT, 2017-08 … 2026-09) from data.binance.vision through
``rt.binance_vision`` into one JSON file per month under --cache-dir/k1d (skips files already cached). ``count``
(stage 0) lists the event days without any forward return; ``report`` computes everything. The perp closes for the
sanity check come from the step-1 cache (--s14-cache). No keys, no orders.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tradedesk.analytics.course_s13_drawdown import (  # noqa: E402
    ath_cross_events,
    cell_stats,
    cluster_bootstrap_mean,
    drawdown,
    episodes,
    forward_return,
    local_cross_events,
    net_return,
    path_after,
    running_max,
    sma,
    state_days,
)
from tradedesk.analytics.liquidation_flush import holm  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
DAY_MS = 86_400_000
SYMS = ("BTCUSDT", "ETHUSDT")
FIRST_MONTH, LAST_MONTH = (2017, 8), (2026, 9)
COST = 0.0012  # spot taker 0.10 % + slippage 0.02 % per side
FUND_DAY = 0.0003  # perp-hold stress: 0.01 % per 8 h against the long
THETAS = (0.10, 0.15, 0.30)
HS = (30, 60, 90)
HS_DESC = (180, 365)
MIN_EFFECT = {30: 0.03, 60: 0.05, 90: 0.07}
PRIMARY = {"A": (0.10, 30), "B": (0.30, 90)}
MIN_EP, OOS_MIN_EV = 10, 2
MIN_SHIFT = 90
STEP_P = (0.985, 0.280, 0.448, 0.038, 0.822)  # raw primary p of steps 1-5 (Holm family of the card)
SEED = 20261017
N_BOOT = 10_000


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def dnum(s: str) -> int:
    return (date.fromisoformat(s) - date(1970, 1, 1)).days


def dstr(d: int) -> str:
    return date.fromordinal(date(1970, 1, 1).toordinal() + int(d)).isoformat()


START_D, OOS_D, LAST_D = dnum("2018-01-01"), dnum("2025-03-01"), dnum("2026-09-30")


def months() -> list[tuple[int, int]]:
    y, m = FIRST_MONTH
    out = []
    while (y, m) <= LAST_MONTH:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------------------------------------------------------------- prereg / fetch


def stage_prereg() -> None:
    raw = CARD.read_bytes()
    t = raw.decode()
    a = t.index("## Попередня реєстрація — крок 6")
    b = t.index("\n## ", a + 1)
    sec = t[a:b].encode()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()  # = git hash-object
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec).hexdigest()} bytes={len(sec)} card_blob={blob}")


async def stage_fetch(cache: Path) -> None:
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    jobs = [(s, y, m, cache / "k1d" / f"{s}-{y:04d}-{m:02d}.json") for s in SYMS for y, m in months()]
    todo = [j for j in jobs if not j[3].exists()]
    emit(f"spot 1d klines: {len(jobs)} files, {len(todo)} to fetch")
    (cache / "k1d").mkdir(parents=True, exist_ok=True)
    _, rt = build_server()
    t0 = time.perf_counter()
    try:
        for i, (s, y, m, p) in enumerate(todo):
            rows: list[list[float]] = []
            for k in range(5):
                try:
                    rows = (await rt.binance_vision.spot_klines_monthly(s, "1d", y, m)).data
                    break
                except ProviderError as e:
                    if e.reason == "data_unavailable":
                        emit(f"  {s} {y}-{m:02d}: not in archive")
                        break
                    emit(f"  retry {s} {y}-{m:02d} after {e.reason}: {str(e)[:100]}")
                    await asyncio.sleep(10 * (k + 1))
            else:
                raise RuntimeError(f"giving up on {s} {y}-{m:02d}")
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows))
            tmp.replace(p)
            if i == 1:
                el = time.perf_counter() - t0
                emit(f"  sample: 2 files in {el:.1f}s -> estimate {el / 2 * len(todo):.0f}s for all")
            if i % 20 == 0 or i == len(todo) - 1:
                emit(f"  {i + 1}/{len(todo)} {p.name} rows={len(rows)} {time.perf_counter() - t0:.0f}s rss {rss_mb():.0f} MB")
            await asyncio.sleep(0.2)
    finally:
        await rt.aclose()
    emit("fetch done")


# ---------------------------------------------------------------- data


class Series:
    def __init__(self, sym: str, d0: int, c: np.ndarray) -> None:
        self.sym, self.d0, self.c = sym, d0, c

    def idx(self, d: int) -> int:
        return d - self.d0

    def day(self, i: int) -> str:
        return dstr(self.d0 + i)


def load_spot(cache: Path, sym: str) -> Series:
    rows: dict[int, float] = {}
    for y, m in months():
        p = cache / "k1d" / f"{sym}-{y:04d}-{m:02d}.json"
        if not p.exists():
            continue
        for r in json.loads(p.read_text()):
            if int(r[0]) % DAY_MS == 0:
                rows[int(r[0]) // DAY_MS] = float(r[4])
    d0, d1 = min(rows), max(rows)
    c = np.full(d1 - d0 + 1, np.nan)
    for d, v in rows.items():
        c[d - d0] = v
    return Series(sym, d0, c)


def perp_daily(s14_cache: Path, sym: str) -> dict[int, float]:
    """Daily close of the USD-M perp = close of the 23:00 UTC hourly bar (step-1 cache)."""
    out: dict[int, float] = {}
    for p in sorted((s14_cache / "k1h" / sym).glob("*.json")):
        for r in json.loads(p.read_text()) or []:
            t = int(r[0])
            if t % DAY_MS == 23 * 3_600_000:
                out[t // DAY_MS] = float(r[4])
    return out


# ---------------------------------------------------------------- sanity and events


def sanity(S: Series, s14_cache: Path) -> None:
    c = S.c
    miss = np.flatnonzero(~np.isfinite(c))
    emit(
        f"- {S.sym} спот 1d: {S.day(0)} … {S.day(len(c) - 1)}, {np.isfinite(c).sum()} доб, пропущено {miss.size}"
        + (f" ({', '.join(S.day(int(i)) for i in miss[:10])}{' …' if miss.size > 10 else ''})" if miss.size else "")
    )
    pd_ = perp_daily(s14_cache, S.sym)
    common = [d for d in pd_ if 0 <= d - S.d0 < len(c) and np.isfinite(c[d - S.d0])]
    if common:
        rel = np.array([pd_[d] / c[d - S.d0] - 1 for d in common])
        k = int(np.argmax(np.abs(rel)))
        emit(
            f"  - спот проти перпу (закриття 23:00-години), {len(common)} спільних доб: медіана |різниці| {np.median(np.abs(rel)):.3%}, "
            f"95-й перцентиль {np.quantile(np.abs(rel), 0.95):.3%}, максимум {rel[k]:+.2%} ({dstr(common[k])})"
        )
    if S.sym == "BTCUSDT":
        i = S.idx(dnum("2024-12-31"))
        emit(f"  - закриття споту 2024-12-31 = {c[i]:g} (перп, еталон кроків 1–5: 93548.9; різниця {93548.9 / c[i] - 1:+.3%})")
    m = running_max(c)
    new_max = np.flatnonzero(np.isfinite(c) & (c >= m))
    # the last close of each run of new maxima before a >= 10 % drawdown — the ATH levels of the episodes
    emit(f"  - нових максимумів закриття: {new_max.size}; останній {S.day(int(new_max[-1]))} = {c[new_max[-1]]:g}")


def ath_events(S: Series, th: float) -> np.ndarray:
    return ath_cross_events(S.c, th, start=S.idx(START_D))


def event_lines(S: Series, th: float, ev: np.ndarray, outcomes: bool) -> None:
    c = S.c
    m = running_max(c)
    dd = drawdown(c, m)
    for t in ev:
        t = int(t)
        peak = int(np.flatnonzero(np.isfinite(c[: t + 1]) & (c[: t + 1] >= m[t]))[-1])
        line = f"| {S.day(t)} | {c[t]:g} | {S.day(peak)} {m[t]:g} | {dd[t]:+.1%} | {'OOS' if S.d0 + t >= OOS_D else 'IS'} |"
        if outcomes:
            for h in HS:
                g, _ = forward_return(c, h)
                line += f" {net_return(g, COST)[t]:+.1%} |" if np.isfinite(g[t]) else " — |"
            if t + 90 < len(c):
                mae, rec = path_after(c, t, 90, m[t])
                line += f" {mae:+.1%} | {'так' if rec else 'ні'} |"
            else:
                line += " — | — |"
            nxt = np.flatnonzero(np.isfinite(c[t:]) & (c[t:] >= m[t]))
            line += f" {int(nxt[0]) if nxt.size else '—'} |"
        emit(line)


def stage_count(cache: Path, s14_cache: Path) -> None:
    emit("# Крок 6 (S13), стадія 0: дати подій (без дохідностей)")
    emit("\n## Санітарні перевірки")
    series = {s: load_spot(cache, s) for s in SYMS}
    for S in series.values():
        sanity(S, s14_cache)
    for S in series.values():
        emit(f"\n## {S.sym}: перетини −θ від максимуму (один на епізод), з 2018-01-01")
        for th in THETAS:
            ev = ath_events(S, th)
            n_is = int(np.sum(S.d0 + ev < OOS_D))
            emit(
                f"\n### θ = {th:.0%}: подій {ev.size} (IS {n_is}, OOS {ev.size - n_is}); епізодів IS при h = 30 / 90: "
                + ", ".join(str(int(episodes(ev[S.d0 + ev < OOS_D], h).max() + 1) if n_is else 0) for h in (30, 90))
            )
            emit("| подія | закриття | максимум (дата, рівень) | просадка | період |\n|---|---|---|---|---|")
            event_lines(S, th, ev, False)
        for th in THETAS:
            lv = local_cross_events(S.c, th, start=S.idx(START_D))
            st = state_days(S.c, th, start=S.idx(START_D))
            emit(
                f"- θ = {th:.0%}: локальний максимум 90 діб — подій {lv.size} (IS {int(np.sum(S.d0 + lv < OOS_D))}); "
                f"стан «у просадці ≥ θ» — {st.size} доб (IS {int(np.sum(S.d0 + st < OOS_D))})"
            )
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


# ---------------------------------------------------------------- statistics


def masks(S: Series) -> dict[str, np.ndarray]:
    d = S.d0 + np.arange(len(S.c))
    return {"IS": (d >= START_D) & (d < OOS_D), "OOS": d >= OOS_D, "ALL": d >= START_D}


def nets(S: Series, cost: float = COST) -> dict[int, np.ndarray]:
    return {h: net_return(forward_return(S.c, h)[0], cost) for h in (*HS, *HS_DESC)}


def ci_s(ci: tuple[float, float] | None, pct: bool = True) -> str:
    if ci is None:
        return "—"
    return f"[{ci[0]:+.1%}; {ci[1]:+.1%}]" if pct else f"[{ci[0]:+.3f}; {ci[1]:+.3f}]"


def fmt_p(p: float | None) -> str:
    return "—" if p is None else f"{p:.3f}"


CELL_HDR = (
    "| ряд | θ | h | n | епізодів | E[net] подій [CI епізодів] | медіана / частка > 0 | база (усі дні) | Δ [CI P_shift] | p (P_shift) | MDE |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)


def cell_line(lab: str, th: float, h: int, st: dict[str, Any]) -> str:
    if st["n"] == 0:
        return f"| {lab} | {th:.0%} | {h} | 0 | 0 | — | — | {st['base']:+.1%} | — | — | — |"
    mde = "—" if st.get("mde") is None else f"{st['mde']:.1%}"
    return (
        f"| {lab} | {th:.0%} | {h} | {st['n']} | {st['n_ep']} | {st['mean']:+.1%} {ci_s(st['ci'])} | {st['median']:+.1%} / {st['win']:.0%} | "
        f"{st['base']:+.1%} | {st['delta']:+.1%} {ci_s(st.get('ci_delta'))} | {fmt_p(st.get('p'))} | {mde} |"
    )


def p_reg(S: Series, net: np.ndarray, ev: np.ndarray, elig: np.ndarray) -> dict[str, Any]:
    s200 = sma(S.c, 200)
    ok = elig & np.isfinite(net) & np.isfinite(s200)
    above = S.c > s200
    base = {True: float(net[ok & above].mean()), False: float(net[ok & ~above].mean())}
    e = ev[ok[ev]] if ev.size else ev
    if e.size == 0:
        return {"n": 0}
    diff = np.array([net[t] - base[bool(above[t])] for t in e])
    bs = cluster_bootstrap_mean(diff, episodes(e, 30), N_BOOT, SEED)
    return {"n": int(e.size), "n_above": int(above[e].sum()), "delta": float(diff.mean()), "ci": bs["ci"], "base": base}


def verdict(st: dict[str, Any], h: int, p_adj: float | None, oos: dict[str, Any], eth: dict[str, Any]) -> str:
    me = MIN_EFFECT[h]
    if st["n_ep"] < MIN_EP or st.get("mde") is None or st["mde"] > 2 * me:
        return f"inconclusive (епізодів {st['n_ep']} < {MIN_EP} або MDE {st.get('mde') or float('nan'):.3f} > {2 * me:.2f})"
    if st["delta"] < me or st["ci_delta"][0] <= 0:
        return "rejected"
    if (
        st["ci"] is not None
        and st["ci"][0] > 0
        and p_adj is not None
        and p_adj < 0.05
        and oos["n"] >= OOS_MIN_EV
        and oos["delta"] > 0
        and eth["n"] > 0
        and eth["delta"] > 0
    ):
        return "confirmed"
    return "inconclusive (позитив в IS не пройшов CI подій, Holm, OOS чи ETH)"


def stage_report(cache: Path, s14_cache: Path) -> None:
    emit("# Крок 6 (S13): купівля BTC після просадок від максимуму — результати")
    emit(
        f"Спот Binance, денні закриття; події з {dstr(START_D)}; IS < {dstr(OOS_D)} ≤ OOS; кінець даних {dstr(LAST_D)}. "
        f"Дохідність — на номінал (= на капітал, спот без плеча), net після {COST:.2%} на сторону. "
        f"База — середня дохідність усіх допустимих діб того самого періоду; p і CI Δ — циклічний зсув індикатора подій (≥ {MIN_SHIFT} діб)."
    )
    emit("\n## Санітарні перевірки")
    series = {s: load_spot(cache, s) for s in SYMS}
    for S in series.values():
        sanity(S, s14_cache)
    B, E = series["BTCUSDT"], series["ETHUSDT"]
    mB, mE = masks(B), masks(E)
    nB, nE = nets(B), nets(E)

    # ---- events
    for S in (B, E):
        emit(f"\n## {S.sym}: події (перетин −θ від максимуму, один на епізод) і результати")
        for th in THETAS:
            ev = ath_events(S, th)
            emit(f"\n### θ = {th:.0%}: {ev.size} подій")
            emit(
                "| подія | закриття | максимум (дата, рівень) | просадка | період | net 30 д | net 60 д | net 90 д | найгірше закриття за 90 д | повернення до максимуму за 90 д | діб до максимуму |\n"
                "|---|---|---|---|---|---|---|---|---|---|---|"
            )
            event_lines(S, th, ev, True)

    # ---- primary
    raw: dict[str, dict[str, Any]] = {}
    for key, (th, h) in PRIMARY.items():
        raw[key] = cell_stats(nB[h], ath_events(B, th), mB["IS"], h, min_shift=MIN_SHIFT, n_boot=N_BOOT, seed=SEED)
    ps = [raw[k]["p"] for k in PRIMARY if raw[k]["p"] is not None]
    p_step = min(1.0, 2 * min(ps)) if ps else None
    p_adj = holm([*STEP_P, p_step])[5] if p_step is not None else None
    emit("\n## Основні метрики (BTC, IS) і вердикт")
    emit(
        f"- сирі p (Δ > 0, P_shift): A {fmt_p(raw['A']['p'])}, B {fmt_p(raw['B']['p'])}; p кроку = min(1, 2·min) = {fmt_p(p_step)}; "
        f"Holm (m = 6: {', '.join(map(str, STEP_P))}, крок 6) = {fmt_p(p_adj)}; ×11 — {fmt_p(min(1.0, 11 * p_step) if p_step is not None else None)}"
    )
    verdicts = {}
    for key, (th, h) in PRIMARY.items():
        st = raw[key]
        oos = cell_stats(nB[h], ath_events(B, th), mB["OOS"], h, min_shift=30, n_boot=N_BOOT, seed=SEED)
        eth = cell_stats(nE[h], ath_events(E, th), mE["ALL"], h, min_shift=MIN_SHIFT, n_boot=N_BOOT, seed=SEED)
        vd = verdict(st, h, p_adj, oos, eth)
        verdicts[key] = vd
        emit(f"\n### {key}: θ = {th:.0%}, h = {h} діб")
        emit(CELL_HDR)
        emit(cell_line("BTC IS", th, h, st))
        emit(cell_line("BTC OOS", th, h, oos))
        emit(cell_line("ETH увесь період", th, h, eth))
        emit(
            f"- зсувів P_shift: {st.get('n_shift')}; мінімальний ефект {MIN_EFFECT[h]:+.0%}, ворота MDE ≤ {2 * MIN_EFFECT[h]:.0%}, епізодів ≥ {MIN_EP}"
        )
        pr = p_reg(B, nB[h], st["days"], mB["IS"])
        if pr["n"]:
            emit(
                f"- P_reg (база тієї самої сторони від SMA200): подій з SMA {pr['n']} (над SMA {pr['n_above']}); база над / під SMA "
                f"{pr['base'][True]:+.1%} / {pr['base'][False]:+.1%}; Δ_reg {pr['delta']:+.1%} {ci_s(pr['ci'])}"
            )
        emit(f"- **вердикт {key} за реєстрацією: {vd}**")
    if any(v == "confirmed" for v in verdicts.values()):
        step = "confirmed"
    elif all(v == "rejected" for v in verdicts.values()):
        step = "rejected"
    else:
        step = "inconclusive"
    emit(f"\n- **вердикт кроку за реєстрацією: {step}**")

    # ---- grids
    sec_p: list[tuple[str, float]] = []

    def grid(
        title: str, S: Series, nets_: dict[int, np.ndarray], m: dict[str, np.ndarray], evf: Any, hs: tuple[int, ...], collect: bool
    ) -> None:
        emit(f"\n## {title}")
        emit(CELL_HDR)
        for per in ("IS", "OOS", "ALL"):
            for th in THETAS:
                ev = evf(S, th)
                for h in hs:
                    st = cell_stats(nets_[h], ev, m[per], h, min_shift=MIN_SHIFT if per != "OOS" else 30, n_boot=2000, seed=SEED)
                    emit(cell_line(f"{S.sym[:3]} {per}", th, h, st))
                    if collect and per == "IS" and (th, h) not in PRIMARY.values() and st.get("p") is not None and h in HS:
                        sec_p.append((f"{title[:40]}: θ {th:.0%}, h {h}", st["p"]))

    grid("BTC: перетин від ATH, уся сітка", B, nB, mB, ath_events, (*HS, *HS_DESC), True)
    grid("ETH: перетин від ATH, уся сітка", E, nE, mE, ath_events, (*HS, *HS_DESC), False)
    grid("BTC: стан «у просадці ≥ θ» (усі такі доби)", B, nB, mB, lambda S, th: state_days(S.c, th, start=S.idx(START_D)), HS, True)
    grid(
        "BTC: перетин від локального максимуму 90 діб (re-arm −θ/2, 30 діб)",
        B,
        nB,
        mB,
        lambda S, th: local_cross_events(S.c, th, start=S.idx(START_D)),
        HS,
        True,
    )
    grid(
        "ETH: перетин від локального максимуму 90 діб",
        E,
        nE,
        mE,
        lambda S, th: local_cross_events(S.c, th, start=S.idx(START_D)),
        HS,
        False,
    )
    emit("\n| другорядна клітинка (IS) | сире p | Holm усередині кроку |\n|---|---|---|")
    for (lab, p), pa in zip(sec_p, holm([p for _, p in sec_p]), strict=True):
        emit(f"| {lab} | {p:.3f} | {pa:.3f} |")

    # ---- path after entry
    emit("\n## Шлях після входу (BTC, IS + OOS, h = 90): події проти всіх діб")
    emit(
        "| θ | n | найгірше закриття, медіана | частка «ще −10 %» | частка «ще −20 %» | повернення до максимуму за 90 д |\n|---|---|---|---|---|---|"
    )
    c = B.c
    m_all = running_max(c)
    base_days = np.flatnonzero(mB["ALL"] & np.isfinite(nB[90]))
    for lab, days in [(f"{th:.0%}", ath_events(B, th)) for th in THETAS] + [("усі доби", base_days)]:
        days = days[days + 90 < len(c)]
        pa = [path_after(c, int(t), 90, m_all[int(t)]) for t in days]
        mae = np.array([x[0] for x in pa])
        rec = np.array([x[1] for x in pa])
        if days.size:
            emit(
                f"| {lab} | {days.size} | {np.median(mae):+.1%} | {np.mean(mae <= -0.10):.0%} | {np.mean(mae <= -0.20):.0%} | {np.mean(rec):.0%} |"
            )

    # ---- robustness of the primaries
    emit("\n## Стійкість основних клітинок (BTC, IS)")
    g = {h: forward_return(c, h)[0] for h in HS}
    for key, (th, h) in PRIMARY.items():
        ev = ath_events(B, th)
        st = raw[key]
        emit(f"\n### {key}: θ = {th:.0%}, h = {h}")
        e_is = st["days"]
        if e_is.size:
            cl = episodes(e_is, h)
            loo = []
            for k in range(int(cl.max()) + 1):
                keep = e_is[cl != k]
                if keep.size:
                    loo.append((B.day(int(e_is[cl == k][0])), float(nB[h][keep].mean()) - st["base"]))
            if loo:
                lo, hi = min(loo, key=lambda x: x[1]), max(loo, key=lambda x: x[1])
                emit(f"- без одного епізоду: Δ від {lo[1]:+.1%} (без {lo[0]}) до {hi[1]:+.1%} (без {hi[0]})")
        st1 = cell_stats(nB[h], ev + 1, mB["IS"], h, min_shift=MIN_SHIFT, n_boot=2000, seed=SEED)
        emit(f"- вхід за закриттям t + 1: Δ {st1['delta']:+.1%}, p {fmt_p(st1.get('p'))}, E[net] {st1['mean']:+.1%}")
        n3 = net_return(g[h], 3 * COST)
        st3 = cell_stats(n3, ev, mB["IS"], h, min_shift=MIN_SHIFT, n_boot=2000, seed=SEED)
        emit(f"- витрати ×3: E[net] {st3['mean']:+.1%}, база {st3['base']:+.1%}, Δ {st3['delta']:+.1%}")
        nf = nB[h] - FUND_DAY * h
        stf = cell_stats(nf, ev, mB["IS"], h, min_shift=MIN_SHIFT, n_boot=2000, seed=SEED)
        emit(
            f"- тримати перпом (фандинг 0.01 % / 8 год проти лонга): E[net] {stf['mean']:+.1%}, база {stf['base']:+.1%}, Δ {stf['delta']:+.1%}"
        )
        yrs: dict[int, list[int]] = {}
        for t in e_is:
            yrs.setdefault(int(B.day(int(t))[:4]), []).append(int(t))
        d_all = B.d0 + np.arange(len(c))
        parts = []
        for y, ts in sorted(yrs.items()):
            ym = mB["IS"] & (d_all >= dnum(f"{y}-01-01")) & (d_all < dnum(f"{y + 1}-01-01")) & np.isfinite(nB[h])
            parts.append(f"{y}: {len(ts)} под., E[net] {np.mean(nB[h][ts]):+.1%} проти бази року {np.mean(nB[h][ym]):+.1%}")
        emit("- за роками: " + "; ".join(parts))
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step6_s13")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--stage", choices=("prereg", "fetch", "count", "report"), required=True)
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    elif a.stage == "fetch":
        asyncio.run(stage_fetch(cache))
    elif a.stage == "count":
        stage_count(cache, a.s14_cache.expanduser())
    else:
        stage_report(cache, a.s14_cache.expanduser())
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
