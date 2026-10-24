"""IDEA-20261024-oi-confirmed-liquidation-flush: does a liquidation flush rebound when open interest really fell?

Stages (all network through ``rt.binance_vision`` on ``core.http.Http``; per-file JSON caches):
  fetch   klines (shared cache of the rejected card, extended back to 2021-12) + daily ``metrics`` for event days
  stage0  coverage, create_time semantics on the full event set, counts per basket and half-year, measured cluster
          SE / MDE of basket A and A - B, power gate. Prints NO mean returns.
  report  H1-H3 and controls; refuses to run unless stage0 passed the registered gate.

Registration: the card's «Попередня реєстрація». Returns are on trade notional, net of 0.30 % per round trip.

    uv run python scripts/verify_oi_liquidation_flush.py --cache-dir ~/.cache/tradedesk-agents/oi_liquidation_flush \
        --stage fetch --log ~/.cache/tradedesk-agents/oi_liquidation_flush/fetch.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_liquidation_flush as vlf  # noqa: E402  (shared fetch stages and caches of the rejected card)

from tradedesk.analytics.liquidation_flush import (  # noqa: E402
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    month_start,
    simulate_trade,
    summarise,
    without_top_days,
)
from tradedesk.analytics.macro_release import cochran_q  # noqa: E402
from tradedesk.analytics.oi_flush import (  # noqa: E402
    SEMANTICS_SWITCH_MS,
    archive_obs_times,
    basket,
    cluster_bootstrap_diff,
    cluster_se_diff,
    cluster_se_mean,
    effective_cluster_sd,
    mde,
    oi_change,
    units_consistent,
)
from tradedesk.server import build_server  # noqa: E402

START = int(datetime(2021, 12, 1, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
FRESH_END = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
COST = 0.003
ENTRY_DELAY = 5 * MIN_MS  # primary: >= 5 min after the OI observed at the bar end T
HOLD = 2 * HOUR_MS
LOOKBACK = 10 * MIN_MS  # OI(T) / OI(T - 10m): end of bar vs five minutes before it opened
SEED = 7
NBOOT = 10_000
GATE = {"mde_a": 0.008, "mde_diff": 0.012, "days_a": 300}
MASS_DAY = 10
# v1 (stage-0 diagnostic only, never used for a mean) treated every row as observed at create_time + 5 min; the
# archive switched semantics on 2024-03-04, so v2 uses oi_flush.archive_obs_times.
TABLE = "events_oi_v2.json"
KLINE_CACHE = Path("~/.cache/tradedesk-agents/liquidation_flush").expanduser()

# the rejected card's stages read these module globals at call time
vlf.START = START
vlf.END = END
vlf.DERIVED = "oi1"  # new derived files (universe / events) for the longer period; kline files are shared


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def half_year(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return f"{d.year}H{1 if d.month <= 6 else 2}"


def metric_days(e: dict[str, Any]) -> list[str]:
    # rows stamped t0 - 10m .. t0 + 5m cover both archive semantics (see oi_flush.archive_obs_times)
    return sorted({vlf.day_str(e["ts"] - LOOKBACK), vlf.day_str(e["ts"]), vlf.day_str(e["ts"] + 5 * MIN_MS)})


def control_days(controls: dict[tuple[str, int], list[int]], events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    need = set()
    for e in events:
        for c in controls[(e["symbol"], e["ts"])]:
            for d in vlf.days_for(c, c + ENTRY_DELAY + HOLD + MIN_MS):
                need.add((e["symbol"], d))
        for d in vlf.days_for(e["signal_ts"], e["signal_ts"] + ENTRY_DELAY + 4 * HOUR_MS + MIN_MS):
            need.add((e["symbol"], d))
    return need


# ---------------------------------------------------------------- fetch


async def fetch(kc: Path, cache: Path) -> None:
    _, rt = build_server()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        fx = vlf.Fetcher(rt, kc)
        symbols, info = await vlf.stage_meta(rt, kc)
        cands = vlf.candidate_symbols(symbols, info)
        print(f"eligible names {len(cands)}; rss {rss_mb():.0f} MB", flush=True)
        await vlf.stage_daily(fx, kc, cands)
        uni = vlf.build_universe(kc, cands)
        print(f"universe months {len(uni)}, members {len({s for v in uni.values() for s in v})}", flush=True)
        await vlf.stage_5m(fx, kc, uni)
        events = vlf.detect_cached(kc, uni)
        print(f"events {len(events)} on {len({e['day'] for e in events})} days; rss {rss_mb():.0f} MB", flush=True)
        mfx = vlf.Fetcher(rt, cache)
        jobs = []
        for s, d in sorted({(e["symbol"], d) for e in events for d in metric_days(e)}):

            async def go(s: str = s, d: str = d) -> Any:
                return (await rt.binance_vision.um_metrics_daily(s, d)).data

            jobs.append((cache / "metrics" / s / f"{d}.json", go))
        await mfx.many("metrics", jobs)
        controls = vlf.control_plan(events)
        need = vlf.needed_1m(events, controls) | control_days(controls, events)
        await vlf.stage_1m(fx, kc, need)
        print(f"FETCH COMPLETE rss {rss_mb():.0f} MB", flush=True)
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- per-event table


class Opens:
    """5m opens of universe members, one month in memory at a time (events are processed in time order)."""

    def __init__(self, kc: Path) -> None:
        self.kc = kc
        self.memo: dict[tuple[str, int], tuple[np.ndarray, np.ndarray]] = {}

    def get(self, sym: str, m: int) -> tuple[np.ndarray, np.ndarray]:
        k = (sym, m)
        if k not in self.memo:
            a = vlf.load_5m(self.kc, sym, [m])
            self.memo[k] = (a[:, 0].astype(np.int64), a[:, 1].copy()) if a.shape[0] else (np.zeros(0, np.int64), np.zeros(0))
        return self.memo[k]

    def at(self, sym: str, t: int) -> float | None:
        ts, op = self.get(sym, month_start(t))
        i = int(np.searchsorted(ts, t))
        return float(op[i]) if i < ts.size and int(ts[i]) == t and op[i] > 0 else None

    def ret(self, sym: str, t0: int, t1: int) -> float | None:
        a, b = self.at(sym, t0), self.at(sym, t1)
        return None if a is None or b is None else b / a - 1

    def trim(self, keep_from: int) -> None:
        self.memo = {k: v for k, v in self.memo.items() if k[1] >= keep_from}


def load_metrics(cache: Path, sym: str, days: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    rows: list[list[float]] = []
    files = missing = 0
    for d in days:
        p = cache / "metrics" / sym / f"{d}.json"
        data = json.loads(p.read_text()) if p.exists() else None
        files += 1
        if data is None:
            missing += 1
        rows.extend(data or [])
    if not rows:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0), files, missing
    a = np.array(sorted({int(r[0]): r for r in rows}.values()), dtype=float)
    return a[:, 0].astype(np.int64), a[:, 1], a[:, 2], files, missing


def build_table(kc: Path, cache: Path) -> list[dict[str, Any]]:
    out_p = cache / TABLE
    if out_p.exists():
        return json.loads(out_p.read_text())
    symbols, info = vlf.read_json(kc / "symbols.json"), vlf.read_json(kc / vlf.INFO_FILE)
    uni = vlf.build_universe(kc, vlf.candidate_symbols(symbols, info))
    events = vlf.detect_cached(kc, uni)
    controls = vlf.control_plan(events)
    per_day = Counter(e["day"] for e in events)
    opens = Opens(kc)
    memo: dict[tuple[str, str], dict[int, tuple[float, ...]]] = {}
    rows = []
    for n, e in enumerate(events):
        s, t0, T, d = e["symbol"], e["ts"], e["signal_ts"], e["direction"]
        r: dict[str, Any] = {k: e[k] for k in ("symbol", "ts", "signal_ts", "direction", "ret", "z", "day", "bar_qv", "close")}
        r["hy"] = half_year(t0)
        r["fresh"] = t0 < FRESH_END
        r["day_events"] = per_day[e["day"]]
        create, oi, val, nf, nmiss = load_metrics(cache, s, metric_days(e))
        r["metric_files_missing"] = nmiss
        obs = archive_obs_times(create)
        doi = oi_change(obs, oi, T, lookback_ms=LOOKBACK, asof=T)
        i = int(np.searchsorted(obs, T))  # the row observed at the bar end T
        has_row = i < obs.size and int(obs[i]) == T
        implied = float(val[i] / oi[i]) if has_row and oi[i] > 0 else None
        j = int(np.searchsorted(create, t0))  # the row stamped t0 (semantics diagnostic, independent of the switch)
        implied_t0 = float(val[j] / oi[j]) if j < create.size and int(create[j]) == t0 and oi[j] > 0 else None
        r["implied"] = implied
        if doi is None:
            r["status"] = "missing"
        elif not units_consistent(float(oi[i]), float(val[i]), e["close"]):
            r["status"] = "units"
        elif abs(doi) >= 0.5:
            r["status"] = "extreme"
        else:
            r["status"] = "ok"
        r["doi"] = doi
        r["basket"] = basket(doi) if r["status"] == "ok" else None
        m1 = vlf.load_1m(kc, s, vlf.days_for(T - 6 * MIN_MS, T + ENTRY_DELAY + 4 * HOUR_MS + MIN_MS), memo)
        # semantics: implied price of the row stamped t0 against 1m closes at T + k min
        for key, px in (("imp_err", implied), ("imp_err_t0", implied_t0)):
            errs = {}
            if px:
                for k in range(-5, 6):
                    b = m1.get(T + k * MIN_MS - MIN_MS)
                    if b:
                        errs[k] = abs(px / b[3] - 1)
            r[key] = errs
        sims = {
            "prim": simulate_trade(m1, T + ENTRY_DELAY, d, HOLD, cost=0.0),
            "stop3": simulate_trade(m1, T + ENTRY_DELAY, d, HOLD, cost=0.0, stop=0.03),
            "t60": simulate_trade(m1, T + MIN_MS, d, HOLD, cost=0.0),
            "h1": simulate_trade(m1, T + ENTRY_DELAY, d, HOUR_MS, cost=0.0),
            "h4": simulate_trade(m1, T + ENTRY_DELAY, d, 4 * HOUR_MS, cost=0.0),
        }
        r["g"] = {k: (None if v is None else v["gross"]) for k, v in sims.items()}
        r["mae"] = None if sims["prim"] is None else sims["prim"]["mae"]
        cg = []
        for c in controls[(s, t0)]:
            cm = vlf.load_1m(kc, s, vlf.days_for(c, c + ENTRY_DELAY + HOLD + MIN_MS), memo)
            res = simulate_trade(cm, c + ENTRY_DELAY, d, HOLD, cost=0.0)
            if res is not None:
                cg.append(res["gross"])
        r["control_g"] = cg
        if r["basket"] is not None:
            members = [x for x in uni.get(month_start(t0), []) if x != s]
            br = [w for x in members if (w := opens.ret(x, T + ENTRY_DELAY, T + ENTRY_DELAY + HOLD)) is not None]
            r["mkt"] = statistics.fmean(br) if br else None
        else:
            r["mkt"] = None
        rows.append(r)
        if len(memo) > 3000:
            memo.clear()
        opens.trim(month_start(t0) - 40 * DAY_MS)
        if (n + 1) % 500 == 0:
            print(f"  table {n + 1}/{len(events)}; rss {rss_mb():.0f} MB", flush=True)
    out_p.write_text(json.dumps(rows))
    return rows


# ---------------------------------------------------------------- stage 0 (no means)


def stage0(kc: Path, cache: Path) -> dict[str, Any]:
    rows = build_table(kc, cache)
    lines = ["# Стадія 0: покриття, семантика, кількість подій, виміряний SE і MDE (без середніх)", ""]
    st = Counter(r["status"] for r in rows)
    lines.append(f"- Подій (усі, 2021-12 … 2026-09): {len(rows)} у {len({r['day'] for r in rows})} днях")
    lines.append(f"- Статус ΔOI: {dict(st)} (missing: нема рядка t0 або t0−10 хв; units: value/OI ≠ close ±10 %; extreme: |ΔOI| ≥ 50 %)")
    mfiles = list((cache / "metrics").glob("*/*.json"))
    n_rows, n_null = [], 0
    for p in mfiles:
        data = json.loads(p.read_text())
        if data is None:
            n_null += 1
        else:
            n_rows.append(len(data))
    lines.append(
        f"- Файлів metrics: {len(mfiles)}, відсутніх в архіві (404): {n_null}; рядків на файл: медіана {statistics.median(n_rows) if n_rows else 0}, "
        f"повних (288): {sum(1 for x in n_rows if x == 288) / max(1, len(n_rows)):.1%}, < 280: {sum(1 for x in n_rows if x < 280)}"
    )
    lines += [
        "",
        "## Покриття ΔOI і кошики по півріччях (кількість подій)",
        "",
        "| Півріччя | подій | з ΔOI | A | B | проміжок | днів A |",
        "|---|---|---|---|---|---|---|",
    ]
    by_hy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_hy[r["hy"]].append(r)
    for hy in sorted(by_hy):
        g = by_hy[hy]
        ok = [r for r in g if r["status"] == "ok"]
        a = [r for r in ok if r["basket"] == "A"]
        b = [r for r in ok if r["basket"] == "B"]
        lines.append(f"| {hy} | {len(g)} | {len(ok)} | {len(a)} | {len(b)} | {len(ok) - len(a) - len(b)} | {len({r['day'] for r in a})} |")
    ok = [r for r in rows if r["status"] == "ok"]
    edges = [-1.0, -0.05, -0.03, -0.02, -0.01, -0.005, 0.0, 0.02, 1.0]
    cnt = Counter()
    for r in ok:
        for lo, hi in zip(edges, edges[1:], strict=False):
            if lo < r["doi"] <= hi:
                cnt[f"({lo:+.3f}; {hi:+.3f}]"] += 1
    lines += ["", "Розподіл ΔOI (лише кількості): " + ", ".join(f"{k}: {v}" for k, v in sorted(cnt.items()))]
    lines += [
        "",
        f"Лонг/шорт у A: {sum(1 for r in ok if r['basket'] == 'A' and r['direction'] > 0)}/{sum(1 for r in ok if r['basket'] == 'A' and r['direction'] < 0)}",
    ]
    # semantics on the full set: |value/OI ÷ 1m close at T + k − 1| for the row stamped t0 and the row observed at T
    lines += [
        "",
        "## Семантика create_time на всіх подіях з ΔOI: медіана |value/OI ÷ 1m close у T + k хв − 1|",
        "",
        "| рядок, період | n | " + " | ".join(f"k={k}" for k in range(-5, 6)) + " | найчастіший k (частка) |",
        "|---|---|" + "---|" * 12,
    ]
    best_obs: list[int] = []
    for key, lab in (("imp_err_t0", "мітка t0"), ("imp_err", "спостережений у T (v2)")):
        for plab, f in (
            ("до 2024-03-04", lambda r: r["ts"] < SEMANTICS_SWITCH_MS),
            ("з 2024-03-04", lambda r: r["ts"] >= SEMANTICS_SWITCH_MS),
        ):
            g = [{int(k): v for k, v in r[key].items()} for r in ok if f(r)]
            g = [ie for ie in g if len(ie) == 11]
            if not g:
                continue
            best = [min(ie, key=lambda k: ie[k]) for ie in g]
            if key == "imp_err":
                best_obs += best
            top, topn = Counter(best).most_common(1)[0]
            meds = [statistics.median(ie[k] for ie in g) for k in range(-5, 6)]
            lines.append(f"| {lab}, {plab} | {len(g)} | " + " | ".join(f"{m:.2%}" for m in meds) + f" | {top} ({topn / len(g):.0%}) |")
    lines.append(
        f"\nРядок, спостережений у T: найкращий k по всіх подіях {dict(sorted(Counter(best_obs).items()))}; медіана k = {statistics.median(best_obs) if best_obs else 'n/a'}"
    )
    t60_ok = bool(best_obs) and statistics.median(best_obs) <= 0
    lines.append(f"Чутливість T+60 с допустима (медіана k ≤ 0): {'так' if t60_ok else 'ні'}")
    # measured SE / MDE on the primary entry (net; cost does not change the SE)
    A = [r for r in ok if r["basket"] == "A" and r["g"]["prim"] is not None]
    B = [r for r in ok if r["basket"] == "B" and r["g"]["prim"] is not None]
    va, da = [r["g"]["prim"] - COST for r in A], [r["day"] for r in A]
    vb, db = [r["g"]["prim"] - COST for r in B], [r["day"] for r in B]
    se_a = cluster_se_mean(va, da)
    se_d = cluster_se_diff(va, da, vb, db)
    se_b = cluster_se_mean(vb, db)
    sa_stop = [r["g"]["stop3"] - COST for r in A if r["g"]["stop3"] is not None]
    se_stop = cluster_se_mean(sa_stop, [r["day"] for r in A if r["g"]["stop3"] is not None])
    nd_a = len(set(da))
    m_a, m_d = mde(se_a), mde(se_d)
    sd_event = statistics.pstdev(va) if len(va) > 1 else math.nan

    def power(effect: float) -> float:
        if not se_a:
            return math.nan
        return 0.5 * math.erfc(-(effect / se_a - 1.959964) / math.sqrt(2))

    gate = bool(m_a is not None and m_d is not None and m_a <= GATE["mde_a"] and m_d <= GATE["mde_diff"] and nd_a >= GATE["days_a"])
    lines += [
        "",
        "## Потужність (виміряно; основний вхід T+5 хв, 2 год, без стопу)",
        "",
        "| Величина | Значення | Ворота |",
        "|---|---|---|",
        f"| A: подій / днів | {len(A)} / {nd_a} | днів ≥ {GATE['days_a']} |",
        f"| B: подій / днів | {len(B)} / {len(set(db))} | |",
        f"| SD доходу на подію, A | {sd_event:.2%} | |",
        f"| SE середнього A (кластери-дні, CR1) | {se_a:.3%} | |" if se_a else "| SE A | n/a | |",
        f"| Ефективний SD на день-кластер A (SE × √G) | {effective_cluster_sd(se_a, nd_a):.2%} | картка припускала 6.7 % |" if se_a else "",
        f"| MDE(A) = 2.8 × SE | {m_a:.2%} | ≤ 0.80 % |" if m_a else "| MDE(A) | n/a | |",
        f"| SE B | {se_b:.3%} | |" if se_b else "",
        f"| SE(A − B), спільні дні | {se_d:.3%} | |" if se_d else "",
        f"| MDE(A − B) | {m_d:.2%} | ≤ 1.20 % |" if m_d else "| MDE(A−B) | n/a | |",
        f"| SE A зі стопом −3 % | {se_stop:.3%} | |" if se_stop else "",
        f"| Потужність H1 при істинному +0.5 % / +0.8 % | {power(0.005):.0%} / {power(0.008):.0%} | |",
        f"| **Ворота** | **{'пройдено' if gate else 'НЕ пройдено'}** | |",
    ]
    res = {
        "gate": gate,
        "mde_a": m_a,
        "mde_diff": m_d,
        "se_a": se_a,
        "days_a": nd_a,
        "n_a": len(A),
        "n_b": len(B),
        "t60_ok": t60_ok,
        "text": "\n".join(x for x in lines if x != ""),
    }
    (cache / "stage0.json").write_text(json.dumps(res))
    (cache / "stage0.md").write_text(res["text"])
    return res


# ---------------------------------------------------------------- report (only after the gate)


def pct(x: float | None) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.2f}%"


def row(label: str, vals: list[float], days: list[str]) -> tuple[str, dict[str, Any]]:
    s = summarise(vals, days, n_boot=NBOOT, seed=SEED)
    if not s["n"]:
        return f"| {label} | 0 | 0 | | | | | |", s
    ci = s["ci"] or (math.nan, math.nan)
    return (
        f"| {label} | {s['n']} | {s['days']} | {pct(s['mean'])} | {pct(s['median'])} | [{pct(ci[0])}; {pct(ci[1])}] | {s['p']:.3f} | {s['win']:.0%} |",
        s,
    )


HDR = "| Вибірка | n | днів | середнє | медіана | 95% CI (дні) | p (одност.) | частка > 0 |\n|---|---|---|---|---|---|---|---|"


def report(kc: Path, cache: Path) -> str:
    s0 = json.loads((cache / "stage0.json").read_text())
    if not s0["gate"]:
        return "Ворота потужності не пройдено: середні не рахуються (inconclusive)."
    rows = build_table(kc, cache)
    ok = [r for r in rows if r["status"] == "ok"]

    def sel(bk: str, key: str = "prim", cost: float = COST, filt=lambda r: True) -> tuple[list[float], list[str], list[dict[str, Any]]]:
        rr = [r for r in ok if r["basket"] == bk and r["g"][key] is not None and filt(r)]
        return [r["g"][key] - cost for r in rr], [r["day"] for r in rr], rr

    out = ["## Основна комірка: вхід T+5 хв, 2 год, нетто 0.30 %", "", HDR]
    va, da, ra = sel("A")
    vb, db, _ = sel("B")
    l1, s_a = row("A (ΔOI ≤ −2 %), без стопу", va, da)
    vs, ds, _ = sel("A", "stop3")
    l2, s_stop = row("A, стоп −3 %", vs, ds)
    l3, s_b = row("B (ΔOI > −0.5 %), без стопу", vb, db)
    l4, _ = row("B, стоп −3 %", *sel("B", "stop3")[:2])
    out += [l1, l2, l3, l4]
    diff = cluster_bootstrap_diff(va, da, vb, db, n_boot=NBOOT, seed=SEED)
    d_mean = statistics.fmean(va) - statistics.fmean(vb)
    d_ci = (float(np.quantile(diff, 0.025)), float(np.quantile(diff, 0.975)))
    out.append(f"\nA − B: {pct(d_mean)} [{pct(d_ci[0])}; {pct(d_ci[1])}] (спільний bootstrap днів, {diff.size} повторів)")
    # H3
    dates = sorted(r["ts"] for r in ra)
    split = dates[len(dates) // 2]
    h1 = [(v, d) for v, d, r in zip(va, da, ra, strict=True) if r["ts"] < split]
    h2 = [(v, d) for v, d, r in zip(va, da, ra, strict=True) if r["ts"] >= split]
    kv, kd, dropped = without_top_days(va, da, 3)
    single = [(v, d) for v, d, r in zip(va, da, ra, strict=True) if r["day_events"] < MASS_DAY]
    mass = [(v, d) for v, d, r in zip(va, da, ra, strict=True) if r["day_events"] >= MASS_DAY]
    out += ["", "## H3: стійкість (A, без стопу)", "", HDR]
    lh1, sh1 = row(f"перша половина (до {vlf.day_str(split)})", [v for v, _ in h1], [d for _, d in h1])
    lh2, sh2 = row("друга половина", [v for v, _ in h2], [d for _, d in h2])
    lk, sk = row(f"без топ-3 днів ({', '.join(dropped)})", kv, kd)
    ls, ss = row(f"дні з < {MASS_DAY} подій", [v for v, _ in single], [d for _, d in single])
    lm, _ = row(f"дні з ≥ {MASS_DAY} подій", [v for v, _ in mass], [d for _, d in mass])
    out += [lh1, lh2, lk, ls, lm]
    # controls and market
    out += ["", "## Контролі (A)", "", HDR]
    cv = [r["g"]["prim"] - statistics.fmean(r["control_g"]) for r in ra if r["control_g"]]
    cd = [r["day"] for r in ra if r["control_g"]]
    lc, s_c = row("подія − середнє 3 випадкових входів (брутто)", cv, cd)
    mv = [r["g"]["prim"] - COST - r["direction"] * r["mkt"] for r in ra if r["mkt"] is not None]
    md = [r["day"] for r in ra if r["mkt"] is not None]
    lmk, s_m = row("понад рівноважний кошик всесвіту (нетто)", mv, md)
    out += [lc, lmk]
    # verdict lines
    c = {
        "H1 середнє ≥ +0.5 %": s_a["mean"] >= 0.005,
        "H1 CI > 0": bool(s_a["ci"] and s_a["ci"][0] > 0),
        "H1 медіана ≥ +0.3 %": s_a["median"] >= 0.003,
        "H1-стоп CI > 0": bool(s_stop["ci"] and s_stop["ci"][0] > 0),
        "H2 A − B ≥ +0.7 п.п.": d_mean >= 0.007,
        "H2 CI > 0": d_ci[0] > 0,
        "H3 знак однаковий у половинах": (sh1.get("mean", 0) > 0) == (sh2.get("mean", 0) > 0) and sh1.get("mean", 0) != 0,
        "H3 без топ-3 днів > 0": sk.get("mean", -1) > 0,
        "H3 поодинокі дні ≥ 0": ss.get("mean", -1) >= 0,
        "понад кошик > 0 (описово, критерій (д))": s_m.get("mean", -1) > 0,
    }
    out += ["", "## Критерії", "", "| Критерій | Виконано |", "|---|---|"] + [f"| {k} | {'так' if v else 'ні'} |" for k, v in c.items()]
    # sensitivity (descriptive)
    out += ["", "## Чутливість (описово)", "", HDR]
    for thr in (-0.01, -0.03, -0.05):
        rr = [r for r in ok if r["doi"] <= thr and r["g"]["prim"] is not None]
        out.append(row(f"A з порогом ΔOI ≤ {thr:.0%}", [r["g"]["prim"] - COST for r in rr], [r["day"] for r in rr])[0])
    if s0["t60_ok"]:
        out.append(row("A, вхід T+60 с", *sel("A", "t60")[:2])[0])
        out.append(row("B, вхід T+60 с", *sel("B", "t60")[:2])[0])
    else:
        out.append("| A, вхід T+60 с | не звітується: стадія 0 не довела відсутність lookahead | | | | | | |")
    out.append(row("A, утримання 1 год", *sel("A", "h1")[:2])[0])
    out.append(row("A, утримання 4 год", *sel("A", "h4")[:2])[0])
    out.append(row("A, витрати 0.50 %", *sel("A", cost=0.005)[:2])[0])
    out.append(row("A, витрати 0.80 %", *sel("A", cost=0.008)[:2])[0])
    for lab, f in (("лонг", lambda r: r["direction"] > 0), ("шорт", lambda r: r["direction"] < 0)):
        out.append(row(f"A, {lab}", *sel("A", filt=f)[:2])[0])
        out.append(row(f"B, {lab}", *sel("B", filt=f)[:2])[0])
    for lab, f in (("свіжий 2021-12 … 2023-12", lambda r: r["fresh"]), ("відомий 2024-01 … 2026-09", lambda r: not r["fresh"])):
        out.append(row(f"A, {lab}", *sel("A", filt=f)[:2])[0])
        out.append(row(f"B, {lab}", *sel("B", filt=f)[:2])[0])
    # per-asset (5a)
    by_sym: dict[str, list[tuple[float, str, int]]] = defaultdict(list)
    for v, d, r in zip(va, da, ra, strict=True):
        by_sym[r["symbol"]].append((v, d, r["ts"]))
    tab, means, ses = [], [], []
    for sym, xs in sorted(by_sym.items(), key=lambda kv: -len(kv[1])):
        if len(xs) < 5:
            continue
        vals, ds_ = [x[0] for x in xs], [x[1] for x in xs]
        se = cluster_se_mean(vals, ds_)
        m = statistics.fmean(vals)
        if se and se > 0:
            means.append(m)
            ses.append(se)
        tab.append(f"| {sym} | {len(xs)} | {pct(m)} | " + (f"[{pct(m - 1.96 * se)}; {pct(m + 1.96 * se)}]" if se else "n/a") + " |")
    q = cochran_q(means, ses) if len(means) > 1 else None
    out += [
        "",
        f"## По активах (A, n ≥ 5; CI = середнє ± 1.96 кластерного SE): {len(tab)} символів",
        "",
        "| Символ | n | середнє | CI |",
        "|---|---|---|---|",
    ]
    out += tab[:40]
    if q:
        out.append(f"\nCochran Q = {q['Q']:.1f}, df = {q['df']}, p = {q['p']:.3f}, I² = {q['I2']:.0%}")
    first = defaultdict(list)
    for sym, xs in by_sym.items():
        for v, _, t in xs:
            if t < split:
                first[sym].append(v)
    ranked = sorted([(statistics.fmean(v), s) for s, v in first.items() if len(v) >= 3], reverse=True)
    top = {s for _, s in ranked[: max(1, len(ranked) // 4)]}
    t2 = [(v, d) for v, d, r in zip(va, da, ra, strict=True) if r["ts"] >= split and r["symbol"] in top]
    o2 = [(v, d) for v, d, r in zip(va, da, ra, strict=True) if r["ts"] >= split and r["symbol"] not in top and r["symbol"] in first]
    out += ["", f"Стійкість: топ-25 % символів за першою половиною ({len(top)} із {len(ranked)} з ≥ 3 подіями) у другій половині", "", HDR]
    out.append(row("топ-25 % (друга половина)", [v for v, _ in t2], [d for _, d in t2])[0])
    out.append(row("решта (друга половина)", [v for v, _ in o2], [d for _, d in o2])[0])
    # tail
    maes = sorted(r["mae"] for r in ra if r["mae"] is not None)
    if maes:
        qn = lambda p: maes[min(len(maes) - 1, int(p * len(maes)))]  # noqa: E731
        liq = ", ".join(f"{lv}x {sum(1 for m in maes if m <= -1 / lv) / len(maes):.1%}" for lv in (2, 3, 5, 10))
        out += [
            "",
            f"Хвіст A (без стопу, MAE за 2 год): медіана {pct(statistics.median(maes))}, p10 {pct(qn(0.10))}, p1 {pct(qn(0.01))}, мін {pct(maes[0])}; поріг ліквідації (≈ 1/плече): {liq}",
        ]
        out.append(f"Quote-об'єм бару події A: медіана {statistics.median(r['bar_qv'] for r in ra) / 1e6:.1f} млн USDT")
    return "\n".join(out)


def semantics_by_month(cache: Path) -> str:
    """Best-matching offset k of value/OI (row stamped t0) vs the 1m close at T + k, by month (no returns used)."""
    rows = json.loads((cache / TABLE).read_text())
    by_m: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        ie = {int(k): v for k, v in r["imp_err_t0"].items()}
        if r["status"] == "ok" and len(ie) == 11:
            by_m[vlf.day_str(r["ts"])[:7]][min(ie, key=lambda k: ie[k])] += 1
    out = ["| Місяць | n | k=−5 | k=0 | інше |", "|---|---|---|---|---|"]
    for m in sorted(by_m):
        c = by_m[m]
        n = sum(c.values())
        out.append(f"| {m} | {n} | {c[-5] / n:.0%} | {c[0] / n:.0%} | {(n - c[-5] - c[0]) / n:.0%} |")
    days = []
    for r in rows:
        ie = {int(k): v for k, v in r["imp_err_t0"].items()}
        if r["status"] == "ok" and len(ie) == 11 and "2024-02-20" <= r["day"] <= "2024-03-20":
            days.append(
                f"{r['day']} {vlf.day_str(r['ts'])} {datetime.fromtimestamp(r['ts'] / 1000, tz=UTC):%H:%M} k={min(ie, key=lambda k: ie[k])}"
            )
    out += ["", "Події навколо зміни (день, час t0, найкращий k):", *sorted(days)]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--kline-cache", default=str(KLINE_CACHE))
    ap.add_argument("--stage", choices=("fetch", "stage0", "semantics", "report"), default="fetch")
    ap.add_argument("--log", default="")
    a = ap.parse_args()
    if a.log:
        sys.stdout = open(a.log, "a", buffering=1)  # noqa: SIM115
        sys.stderr = sys.stdout
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cache, kc = Path(a.cache_dir).expanduser(), Path(a.kline_cache).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if a.stage == "fetch":
        asyncio.run(fetch(kc, cache))
    elif a.stage == "stage0":
        print(stage0(kc, cache)["text"])
    elif a.stage == "semantics":
        print(semantics_by_month(cache))
    else:
        text = report(kc, cache)
        (cache / "report.md").write_text(text)
        print(text)
    print(f"done in {time.time() - t0:.0f}s; peak rss {rss_mb():.0f} MB", flush=True)


if __name__ == "__main__":
    main()
