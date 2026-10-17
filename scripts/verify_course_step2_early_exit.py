"""Course setups, step 2 (IDEA-20261016-course-setups-crypto): the early time exit and the breakeven stop as exit
modules on (a) the step-1 S14 trades, (b) the Session Stop Hunt crypto trades, (c) geometry-matched random entries.

Rules and thresholds are fixed in the card's section "Попередня реєстрація — крок 2". Stages:
  uv run python scripts/verify_course_step2_early_exit.py --stage prereg --out ~/.cache/tradedesk-agents/course_step2_early_exit/prereg.txt
  uv run python scripts/verify_course_step2_early_exit.py --stage report --out ~/.cache/tradedesk-agents/course_step2_early_exit/report.md
``--symbols N`` runs only the first N step-1 symbols and skips set (b) (a timing sample; no verdict).
No network: the 1h bars come from the step-1 cache (--s14-cache, filled from data.binance.vision via rt.binance_vision),
the 5m bars from the Session Stop Hunt cache (--ssh-cache). Per-trade results go to --cache-dir/results.json.
No keys, no orders.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_course_s14_ema as s1  # noqa: E402  (step-1 loaders, period and universe)
import verify_session_stop_hunt as ssh  # noqa: E402  (S25 loaders and day list)

from tradedesk.analytics.course_s14_ema import DAY_MS, HOUR_MS, Bars  # noqa: E402
from tradedesk.analytics.early_exit import BASE, ExitRule, pick_placebo_times, simulate_managed  # noqa: E402
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_means, holm, without_top_days  # noqa: E402
from tradedesk.analytics.session_stop_hunt import BAR_MS as M5  # noqa: E402
from tradedesk.analytics.session_stop_hunt import (  # noqa: E402
    cluster_bootstrap_diff,  # noqa: E402
    find_setup,
    london_session,
    target_for,
)
from tradedesk.analytics.session_stop_hunt import simulate as ssh_simulate  # noqa: E402

CARD = ROOT / "docs" / "ideas" / "IDEA-20261016-course-setups-crypto.md"
OUT: dict[str, Any] = {"fh": None}
SEED = 20261017
PG_PER_TRADE = 3
PG_WINDOW = 30 * DAY_MS
HOLD_S1 = 7 * DAY_MS
OOS_MS = s1.OOS_MS
STEP1_P = 0.985  # primary p of step 1 (Holm family of the card)
MIN_EFFECT = 0.05
MDE_GATE = 0.10
MIN_DATES = 200
SSH_SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
SSH_PG_FROM_H, SSH_PG_TO_H = 7, 12  # placebo entries between 07:00 and 12:00 UTC

# primary early-exit horizon per signal timeframe; grids are reported in full
CHECK_H = {"4h": 16, "1h": 24, "5m": 2}
GRID_H = {"4h": (8, 16, 24), "1h": (12, 24, 48), "5m": (1, 2, 3)}
GRID_X = (0.25, 0.5, 1.0)


def emit(line: str = "") -> None:
    print(line, flush=True)
    if OUT["fh"] is not None:
        OUT["fh"].write(line + "\n")
        OUT["fh"].flush()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def day_of(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def week_of(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def rules_for(tf: str, full: bool) -> dict[str, ExitRule]:
    """Rule key -> rule. ``full`` adds the sensitivity grid and the 1.5R breakeven."""
    T = CHECK_H[tf] * HOUR_MS
    out = {
        "base": BASE,
        "E": ExitRule(check_ms=T, min_mfe_r=0.5),
        "B": ExitRule(be_trigger_r=1.0),
        "EB": ExitRule(check_ms=T, min_mfe_r=0.5, be_trigger_r=1.0),
    }
    if full:
        out["B15"] = ExitRule(be_trigger_r=1.5)
        for h in GRID_H[tf]:
            for x in GRID_X:
                out[f"E{h}h_{x:g}"] = ExitRule(check_ms=h * HOUR_MS, min_mfe_r=x)
    return out


def run_rules(
    path: Bars, entry_ms: int, entry: float, stop: float, target: float, hold: int, rules: dict[str, ExitRule]
) -> dict[str, Any] | None:
    res: dict[str, Any] = {}
    for k, r in rules.items():
        m = simulate_managed(path, entry_ms, entry, stop, target, hold, r)
        if m is None:
            return None
        res[k] = m.r_net
        if k in ("base", "E", "B"):
            res[f"{k}_kind"] = m.exit_kind
            res[f"{k}_cost"] = m.cost_r
    return res


# ---------------------------------------------------------------- prereg


def stage_prereg() -> None:
    t = CARD.read_text()
    a = t.index("## Попередня реєстрація — крок 2")
    b = t.index("\n## ", a + 1)
    sec = t[a:b]
    emit(f"{datetime.now(UTC).isoformat(timespec='seconds')} sha256={hashlib.sha256(sec.encode()).hexdigest()} bytes={len(sec.encode())}")


# ---------------------------------------------------------------- set (a): step-1 trades


KEEP_VARIANTS = ("touch", "sweep", "p1_touch", "p1_sweep")


def load_step1_trades(path: Path) -> dict[str, list[dict[str, Any]]]:
    emit(f"- читаю {path} ({path.stat().st_size / 1e6:.0f} MB)")
    raw = json.loads(path.read_text())
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    n_all = len(raw)
    for r in raw:
        if r["variant"] not in KEEP_VARIANTS:
            continue
        by[r["sym"]].append(
            {k: r[k] for k in ("sym", "tf", "side", "variant", "entry_ms", "day", "oos", "stop_pct", "target_r", "r_net", "exit_kind")}
        )
    del raw
    gc.collect()
    emit(f"- рядків у trades.json кроку 1: {n_all}; взято (touch, sweep, p1_*): {sum(len(v) for v in by.values())}; rss {rss_mb():.0f} MB")
    return by


def member_intervals(uni: dict[str, list[str]]) -> dict[str, np.ndarray]:
    """symbol -> sorted array of [month_start, next_month_start) pairs (ms) where it is a member."""
    by: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for mo, mem in uni.items():
        a = s1.ms(f"{mo}-01T00:00")
        y, m = int(mo[:4]), int(mo[5:])
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        b = s1.ms(f"{y2:04d}-{m2:02d}-01T00:00")
        for s in mem:
            by[s].append((a, b))
    return {s: np.array(sorted(v), dtype=np.int64) for s, v in by.items()}


def in_intervals(t: np.ndarray, iv: np.ndarray) -> np.ndarray:
    if iv.size == 0:
        return np.zeros(t.size, dtype=bool)
    k = np.searchsorted(iv[:, 0], t, side="right") - 1
    ok = k >= 0
    out = np.zeros(t.size, dtype=bool)
    out[ok] = t[ok] < iv[k[ok], 1]
    return out


def candidate_entries(h1: Bars, tf: str, iv: np.ndarray) -> np.ndarray:
    """Entry times (bar closes) a placebo may use: every 1h close (1h) or every 4h-boundary close (4h), in the step-1
    signal period and in the universe of the entry month."""
    close = h1.t + HOUR_MS
    if tf == "4h":
        close = close[close % (4 * HOUR_MS) == 0]
    close = close[(close >= s1.START_MS) & (close < s1.END_MS)]
    return close[in_intervals(close, iv)]


def run_set_a(
    trades: dict[str, list[dict[str, Any]]], s14_cache: Path, data_dir: Path, uni: dict[str, list[str]], limit: int | None
) -> tuple[list[dict[str, Any]], Counter[str]]:
    ivs = member_intervals(uni)
    rng = np.random.default_rng(SEED)
    syms = sorted(trades)
    if limit:
        syms = syms[:limit]
    out: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    t0 = time.time()
    for n, s in enumerate(syms):
        h1 = s1.load_1h(s14_cache, data_dir, s)
        paths = {"long": h1, "short": h1.mirrored()}
        idx = {int(t): i for i, t in enumerate(h1.t)}
        cands = {tf: candidate_entries(h1, tf, ivs.get(s, np.zeros((0, 2), dtype=np.int64))) for tf in ("4h", "1h")}
        for r in trades[s]:
            p = paths[r["side"]]
            i = idx.get(r["entry_ms"] - HOUR_MS)
            if i is None:
                why["a:entry_bar_missing"] += 1
                continue
            entry = float(p.c[i])
            stop = entry - r["stop_pct"] * abs(entry)
            target = entry + r["target_r"] * (entry - stop)
            main = r["tf"] == "4h" and r["side"] == "long" and r["variant"] == "touch"
            rules = rules_for(r["tf"], full=main)
            if r["variant"].startswith("p1_"):
                rules = {k: rules[k] for k in ("base", "E", "B")}
            res = run_rules(p, r["entry_ms"], entry, stop, target, HOLD_S1, rules)
            if res is None:
                why["a:no_path"] += 1
                continue
            set_name = f"{r['tf']} {r['side']} {r['variant']}"
            row = {
                "set": set_name,
                "role": "p1" if r["variant"].startswith("p1_") else "signal",
                "sym": s,
                "day": r["day"],
                "oos": r["oos"],
            }
            row["step1_r_net"] = r["r_net"]
            row.update(res)
            out.append(row)
            if r["variant"].startswith("p1_"):
                continue
            # placebo PG: same symbol and side, same stop % and target R, random close within +-30 days
            for te in pick_placebo_times(cands[r["tf"]], r["entry_ms"], PG_WINDOW, PG_PER_TRADE, rng, exclude_ms=r["entry_ms"]):
                j = idx.get(int(te) - HOUR_MS)
                if j is None:
                    why["pg:entry_bar_missing"] += 1
                    continue
                e2 = float(p.c[j])
                st2 = e2 - r["stop_pct"] * abs(e2)
                res2 = run_rules(p, int(te), e2, st2, e2 + r["target_r"] * (e2 - st2), HOLD_S1, rules)
                if res2 is None:
                    why["pg:no_path"] += 1
                    continue
                d2 = day_of(int(te))
                out.append({"set": set_name, "role": "pg", "sym": s, "day": d2, "oos": int(te) >= OOS_MS, **res2})
        del h1, paths, idx
        if (n + 1) % 20 == 0 or n + 1 == len(syms):
            emit(f"  [a] {n + 1}/{len(syms)} symbols in {time.time() - t0:.0f}s, rows {len(out)}, rss {rss_mb():.0f} MB")
    return out, why


# ---------------------------------------------------------------- set (b): Session Stop Hunt


def neg(b: Any) -> Bars:
    """Short-space session bars -> long-space course bars (5m)."""
    return Bars(b.t, -b.o, -b.low, -b.h, -b.c, M5)


def run_set_b(ssh_cache: Path) -> tuple[list[dict[str, Any]], Counter[str]]:
    rng = np.random.default_rng(SEED + 1)
    out: list[dict[str, Any]] = []
    why: Counter[str] = Counter()
    rules = rules_for("5m", full=True)
    t0 = time.time()
    for s in SSH_SYMS:
        raw = ssh.load(ssh_cache, s)
        full_long = Bars(raw.t, raw.o, raw.h, raw.low, raw.c, M5)
        full_short = full_long.mirrored()
        idx = {int(t): i for i, t in enumerate(raw.t)}
        for d in ssh.days():
            sess = london_session(d)
            st, b, reason = find_setup(raw, sess)
            if st is None or b is None:
                continue
            tr, why2 = ssh_simulate(st, b, "E1", "T1")
            if tr is None:
                why[f"b:{why2}"] += 1
                continue
            tgt = target_for("T1", st, st.entry)
            assert tgt is not None
            lp = neg(b)
            entry_ms = int(b.t[st.bos_idx]) + M5
            hold = int(b.t[st.exit_idx]) + M5 - entry_ms
            res = run_rules(lp, entry_ms, -st.entry, -st.stop, -tgt, hold, rules)
            if res is None:
                why["b:no_path"] += 1
                continue
            side = "long" if st.side > 0 else "short"
            out.append(
                {
                    "set": "SSH E1xT1",
                    "role": "signal",
                    "sym": s,
                    "day": d.isoformat(),
                    "oos": entry_ms >= OOS_MS,
                    "step1_r_net": tr.r_net,
                    **res,
                }
            )
            # placebo PG: same date, random 5m close 07:00-12:00 UTC, same stop % and target R, exit 16:00 UTC
            d0 = s1.ms(f"{d.isoformat()}T00:00")
            exit_at = d0 + 16 * HOUR_MS
            cand = np.arange(d0 + SSH_PG_FROM_H * HOUR_MS, d0 + SSH_PG_TO_H * HOUR_MS + 1, M5, dtype=np.int64)
            stop_pct = tr.stop_pct
            target_r = (st.entry - tgt) / (st.stop - st.entry)
            p = full_long if side == "long" else full_short
            for te in pick_placebo_times(cand, int(cand.mean()), 6 * HOUR_MS, PG_PER_TRADE, rng, exclude_ms=entry_ms):
                j = idx.get(int(te) - M5)
                if j is None:
                    why["b:pg_entry_bar_missing"] += 1
                    continue
                e2 = float(p.c[j])
                st2 = e2 - stop_pct * abs(e2)
                res2 = run_rules(p, int(te), e2, st2, e2 + target_r * (e2 - st2), exit_at - int(te), rules)
                if res2 is None:
                    why["b:pg_no_path"] += 1
                    continue
                out.append({"set": "SSH E1xT1", "role": "pg", "sym": s, "day": d.isoformat(), "oos": int(te) >= OOS_MS, **res2})
        del raw, full_long, full_short, idx
        emit(f"  [b] {s} done in {time.time() - t0:.0f}s, rows {len(out)}, rss {rss_mb():.0f} MB")
    return out, why


# ---------------------------------------------------------------- statistics


def fmt_ci(ci: tuple[float, float] | None) -> str:
    return "—" if ci is None else f"[{ci[0]:+.3f}, {ci[1]:+.3f}]"


def boot(values: list[float], clusters: list[str], n_boot: int = 5000) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": float("nan"), "ci": None, "p": None, "se": None, "days": 0}
    out: dict[str, Any] = {"n": len(values), "mean": statistics.fmean(values), "days": len(set(clusters))}
    if len(set(clusters)) < 2:
        out.update(ci=None, p=None, se=None)
        return out
    bm = cluster_bootstrap_means(values, clusters, n_boot=n_boot)
    out.update(
        ci=(float(np.quantile(bm, 0.025)), float(np.quantile(bm, 0.975))),
        p=float((np.sum(bm <= 0) + 1) / (bm.size + 1)),
        se=float(np.std(bm)),
    )
    return out


def deltas(rows: list[dict[str, Any]], key: str, base: str = "base") -> tuple[list[float], list[str]]:
    return [r[key] - r[base] for r in rows], [r["day"] for r in rows]


def dd(sig: list[dict[str, Any]], plc: list[dict[str, Any]], key: str, n_boot: int = 5000) -> dict[str, Any]:
    a, ad = deltas(sig, key)
    b, bd = deltas(plc, key)
    if not a or not b:
        return {"diff": float("nan"), "ci": None, "p": None}
    return cluster_bootstrap_diff(a, ad, b, bd, n_boot=n_boot)


def sel(rows: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
    return [r for r in rows if all(r.get(k) == v for k, v in kw.items())]


def split(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return [r for r in rows if not r["oos"]], [r for r in rows if r["oos"]]


HDR = "| вибірка | n | дат | E[R] net база | E[R] net правило | Δ (правило − база) | 95 % CI Δ | p (Δ>0) | SE Δ |\n|---|---|---|---|---|---|---|---|---|"


def dline(label: str, rows: list[dict[str, Any]], key: str, n_boot: int = 5000) -> tuple[str, dict[str, Any]]:
    if not rows:
        return f"| {label} | 0 | — | — | — | — | — | — | — |", {"n": 0}
    v, d = deltas(rows, key)
    b = boot(v, d, n_boot)
    p = "—" if b["p"] is None else f"{b['p']:.4f}"
    se = "—" if b["se"] is None else f"{b['se']:.4f}"
    base = statistics.fmean(r["base"] for r in rows)
    rule = statistics.fmean(r[key] for r in rows)
    return f"| {label} | {b['n']} | {b['days']} | {base:+.3f} | {rule:+.3f} | {b['mean']:+.4f} | {fmt_ci(b['ci'])} | {p} | {se} |", b


def level_line(label: str, rows: list[dict[str, Any]], key: str, n_boot: int = 5000) -> str:
    if not rows:
        return f"| {label} | 0 | — | — | — | — |"
    b = boot([r[key] for r in rows], [r["day"] for r in rows], n_boot)
    p = "—" if b["p"] is None else f"{b['p']:.4f}"
    return f"| {label} | {b['n']} | {b['days']} | {b['mean']:+.3f} | {fmt_ci(b['ci'])} | {p} |"


# ---------------------------------------------------------------- report


def sanity_a(rows: list[dict[str, Any]], why: Counter[str]) -> None:
    emit("## Санітарні перевірки")
    sig = [r for r in rows if r["role"] in ("signal", "p1")]
    by: dict[str, list[float]] = defaultdict(list)
    for r in sig:
        by[r["set"]].append(abs(r["base"] - r["step1_r_net"]))
    emit("- (а) база, перерахована тут, проти `r_net` кроку 1 (та сама угода):")
    for k in sorted(by):
        v = by[k]
        emit(f"  - {k}: угод {len(v)}; розбіжність > 1e-6R: {sum(1 for x in v if x > 1e-6)}; макс. |різниця| {max(v):.2e}")
    emit(f"- (а) відсів: {dict(sorted(why.items()))}")


def report_block(title: str, rows: list[dict[str, Any]], set_name: str, keys: list[str], n_boot: int = 5000) -> dict[str, Any]:
    sig = sel(rows, set=set_name, role="signal")
    pg = sel(rows, set=set_name, role="pg")
    p1 = sel(rows, set=set_name.replace(" touch", " p1_touch").replace(" sweep", " p1_sweep"), role="p1")
    s_is, s_oos = split(sig)
    g_is, g_oos = split(pg)
    p_is, _ = split(p1)
    emit(f"\n### {title}")
    emit("| вибірка | n | дат | E[R] net | 95 % CI | p (E>0) |\n|---|---|---|---|---|---|")
    emit(level_line("сигнали IS, база", s_is, "base", n_boot))
    for k in keys:
        emit(level_line(f"сигнали IS, {k}", s_is, k, n_boot))
    emit(level_line("сигнали OOS, база", s_oos, "base", n_boot))
    for k in keys:
        emit(level_line(f"сигнали OOS, {k}", s_oos, k, n_boot))
    emit(level_line("плацебо PG IS, база", g_is, "base", n_boot))
    emit(HDR)
    stats: dict[str, Any] = {}
    for k in keys:
        ln, b = dline(f"сигнали IS, {k}", s_is, k, n_boot)
        emit(ln)
        stats[(k, "is")] = b
        ln, b = dline(f"сигнали OOS, {k}", s_oos, k, n_boot)
        emit(ln)
        stats[(k, "oos")] = b
        emit(dline(f"плацебо PG IS, {k}", g_is, k, n_boot)[0])
        emit(dline(f"плацебо PG OOS, {k}", g_oos, k, n_boot)[0])
        if p_is and k in p_is[0]:
            emit(dline(f"плацебо P1 кроку 1 IS, {k}", p_is, k, n_boot)[0])
    emit("\n| порівняння | ΔΔ = Δ сигналів − Δ плацебо | 95 % CI | p (ΔΔ>0) |\n|---|---|---|---|")
    for k in keys:
        for lab, a, b in (("PG, IS", s_is, g_is), ("PG, OOS", s_oos, g_oos), ("P1 кроку 1, IS", s_is, p_is)):
            if not b or k not in b[0]:
                continue
            d = dd(a, b, k, n_boot)
            stats[(k, "dd", lab)] = d
            p = "—" if d["p"] is None else f"{d['p']:.4f}"
            emit(f"| {k}: сигнали − {lab} | {d['diff']:+.4f} | {fmt_ci(d['ci'])} | {p} |")
    return stats


def verdict(st: dict[str, Any], key: str, p_adj: float | None) -> str:
    b, o = st[(key, "is")], st[(key, "oos")]
    d = st.get((key, "dd", "PG, IS"), {})
    if b.get("days", 0) < MIN_DATES or b.get("se") is None or 2.8 * b["se"] > MDE_GATE:
        return "inconclusive (замало дат або потужності)"
    if b["ci"][0] <= 0 or b["mean"] < MIN_EFFECT:
        return "rejected"
    if (
        p_adj is not None
        and p_adj < 0.05
        and d.get("ci") is not None
        and d["diff"] >= MIN_EFFECT
        and d["ci"][0] > 0
        and o.get("n", 0) > 0
        and o["mean"] > 0
        and (o.get("p") or 1) < 0.05
    ):
        return "confirmed"
    return "inconclusive (позитив в IS не пройшов Holm, плацебо чи OOS)"


def mechanics(rows: list[dict[str, Any]], key: str) -> None:
    trig = [r for r in rows if r[f"{key}_kind"] in ("early", "be", "be_gap")]
    if not rows:
        return
    emit(
        f"- {key}: спрацювало на {len(trig)} з {len(rows)} угод ({len(trig) / len(rows):.1%}); виходи з правилом {dict(Counter(r[f'{key}_kind'] for r in rows))}"
    )
    if trig:
        bk = Counter(r["base_kind"] for r in trig)
        emit(f"  - ці угоди в базі закінчились так: {dict(bk)}")
        emit(
            f"  - середнє R net цих угод: база {statistics.fmean(r['base'] for r in trig):+.3f}, з правилом {statistics.fmean(r[key] for r in trig):+.3f}; "
            f"середнє Δ на угоду, де спрацювало, {statistics.fmean(r[key] - r['base'] for r in trig):+.3f}"
        )
        saved = sum(1 for r in trig if r["base_kind"] in ("stop", "stop_gap"))
        cut = sum(1 for r in trig if r["base_kind"] == "target")
        emit(f"  - «врятовано від стопу» {saved}, «зрізано ціль» {cut}, решта (час / data_end) {len(trig) - saved - cut}")


def robustness(s_is: list[dict[str, Any]], key: str) -> None:
    emit(HDR)
    v, d = deltas(s_is, key)
    w = [week_of(x) for x in d]
    b = boot(v, w)
    emit(
        f"| IS, кластери за ISO-тижнем | {b['n']} | {b['days']} тижн. | — | — | {b['mean']:+.4f} | {fmt_ci(b['ci'])} | {b['p']:.4f} | {b['se']:.4f} |"
    )
    for k in (5, 10, 20):
        vv, dd_, _ = without_top_days(v, d, k)
        bb = boot(vv, dd_)
        emit(
            f"| IS без {k} найкращих дат (за сумою Δ) | {bb['n']} | {bb['days']} | — | — | {bb['mean']:+.4f} | {fmt_ci(bb['ci'])} | {bb['p']:.4f} | — |"
        )
    for mult in (0.5, 2.0, 3.0):
        # net = gross - cost; scaling costs changes Δ through the exit-cost difference only
        vv = [(r[key] - r["base"]) - (mult - 1) * (r.get(f"{key}_cost", 0.0) - r["base_cost"]) for r in s_is]
        bb = boot(vv, d)
        emit(f"| IS, витрати ×{mult:g} | {bb['n']} | — | — | — | {bb['mean']:+.4f} | {fmt_ci(bb['ci'])} | {bb['p']:.4f} | — |")
    for y in range(2020, 2026):
        rr = [r for r in s_is if r["day"].startswith(str(y))]
        if rr:
            emit(dline(f"IS {y}", rr, key)[0])
    emit(dline("IS BTC+ETH", [r for r in s_is if r["sym"] in ("BTCUSDT", "ETHUSDT")], key)[0])
    emit(dline("IS альти", [r for r in s_is if r["sym"] not in ("BTCUSDT", "ETHUSDT")], key)[0])


def stage_report(s14_cache: Path, data_dir: Path, ssh_cache: Path, cache: Path, limit: int | None) -> None:
    emit("# Крок 2 серії курсу: ранній вихід за часом і стоп у БУ — результати")
    emit(
        "R — частки ризику угоди (вхід − стоп); net — після 0.14 % за коло (ранній вихід і БУ — теж за ринком); дохід на ризик, не на капітал."
    )
    emit(
        f"IS — вхід до {day_of(OOS_MS)}, OOS — з цієї дати. Плацебо PG — до {PG_PER_TRADE} випадкових входів з тією самою геометрією, сід {SEED}.\n"
    )
    uni = s1.members(s14_cache)
    trades = load_step1_trades(s14_cache / "trades.json")
    rows, why = run_set_a(trades, s14_cache, data_dir, uni, limit)
    del trades
    gc.collect()
    rows_b: list[dict[str, Any]] = []
    why_b: Counter[str] = Counter()
    if not limit:
        rows_b, why_b = run_set_b(ssh_cache)
    allrows = rows + rows_b
    (cache / "results.json").write_text(json.dumps(allrows, separators=(",", ":")))
    sanity_a(rows, why)
    if rows_b:
        sig_b = sel(rows_b, role="signal")
        emit(
            f"- (б) SSH E1×T1 Лондон: угод {len(sig_b)} (звіт S25: 1915); середнє R net бази {statistics.fmean(r['base'] for r in sig_b):+.4f} (звіт S25: −0.214); "
            f"розбіжність з `session_stop_hunt.simulate` > 1e-6R: {sum(1 for r in sig_b if abs(r['base'] - r['step1_r_net']) > 1e-6)}; відсів {dict(why_b)}"
        )
        emit(f"- (б) плацебо PG: {len(sel(rows_b, role='pg'))} входів")

    main = "4h long touch"
    emit("\n## Основна метрика: набір (а), лонг 4h дотик, правило E (16 год, < +0.5R)")
    st = report_block("Лонг 4h дотик: E, B, E+B", rows, main, ["E", "B", "EB"], n_boot=10000)
    b = st[("E", "is")]
    mde = 2.8 * b["se"] if b.get("se") else float("inf")
    p_raw = b.get("p")
    p_adj = holm([STEP1_P, p_raw])[1] if p_raw is not None else None
    emit("\n## Ворота й вердикт (за реєстрацією)")
    emit(
        f"- різних дат IS: {b.get('days')} (поріг {MIN_DATES}); MDE = 2.8·SE(Δ) = {mde:.4f}R (ворота {MDE_GATE}R); мінімальний ефект {MIN_EFFECT}R"
    )
    emit(
        f"- p основної метрики (Δ > 0, однобічне, IS): сире {p_raw}; Holm (m = 2: крок 1 p = {STEP1_P}, крок 2) {p_adj}; ×11 для довідки {min(1.0, 11 * p_raw) if p_raw is not None else None}"
    )
    v_e = verdict(st, "E", p_adj)
    emit(f"- **вердикт H1 (ранній вихід, Good_Mood) за реєстрацією: {v_e}**")
    pb = st[("B", "is")].get("p")
    v_b = verdict(st, "B", pb)
    emit(f"- другорядне, H1 gmilnikov (БУ після +1R) за тими самими правилами, сире p {pb} (поза сім'єю Holm картки): {v_b}")
    s_is = split(sel(rows, set=main, role="signal"))[0]
    emit("\n### Чи стає набір прибутковим з правилом (рядок «перевага стратегії», не вердикт кроку)")
    emit("| вибірка | n | дат | E[R] net | 95 % CI | p (E>0) |\n|---|---|---|---|---|---|")
    for k in ("base", "E", "B", "EB"):
        emit(level_line(f"IS, {k}", s_is, k, 10000))
        emit(level_line(f"OOS, {k}", split(sel(rows, set=main, role="signal"))[1], k, 10000))

    emit("\n### Механіка правил (IS, сигнали)")
    mechanics(s_is, "E")
    mechanics(s_is, "B")
    g_is = split(sel(rows, set=main, role="pg"))[0]
    emit("- те саме на плацебо PG (IS):")
    mechanics(g_is, "E")

    emit("\n### Сітка чутливості (лонг 4h дотик; усі варіанти, жоден не обирався)")
    emit(HDR)
    g_rows = split(sel(rows, set=main, role="pg"))
    o_rows = split(sel(rows, set=main, role="signal"))[1]
    for h in GRID_H["4h"]:
        for x in GRID_X:
            k = f"E{h}h_{x:g}"
            emit(dline(f"сигнали IS, {k}", s_is, k)[0])
            emit(dline(f"сигнали OOS, {k}", o_rows, k)[0])
            emit(dline(f"PG IS, {k}", g_rows[0], k)[0])
    emit(dline("сигнали IS, B15 (БУ після +1.5R)", s_is, "B15")[0])
    emit(dline("сигнали OOS, B15", o_rows, "B15")[0])
    emit(dline("PG IS, B15", g_rows[0], "B15")[0])

    emit("\n### Стійкість основного правила E (лонг 4h дотик, IS)")
    robustness(s_is, "E")
    emit("\n### Стійкість варіанта B (лонг 4h дотик, IS)")
    robustness(s_is, "B")

    emit("\n## Другорядні набори (сирі p; Holm усередині кроку — лише довідка)")
    sec: list[tuple[str, float]] = []
    sets = sorted({r["set"] for r in rows if r["role"] == "signal"} - {main})
    if rows_b:
        sets.append("SSH E1xT1")
    for sname in sets:
        srows = rows_b if sname.startswith("SSH") else rows
        stt = report_block(
            f"{sname} (контроль T = {CHECK_H['5m' if sname.startswith('SSH') else sname.split()[0]]} год)", srows, sname, ["E", "B", "EB"]
        )
        for k in ("E", "B"):
            p = stt[(k, "is")].get("p")
            if p is not None:
                sec.append((f"{sname} {k}", p))
        if sname.startswith("SSH"):
            emit("\n#### SSH: сітка чутливості")
            emit(HDR)
            sb = split(sel(rows_b, role="signal"))
            gb = split(sel(rows_b, role="pg"))
            for h in GRID_H["5m"]:
                for x in GRID_X:
                    k = f"E{h}h_{x:g}"
                    emit(dline(f"SSH IS, {k}", sb[0], k)[0])
                    emit(dline(f"SSH OOS, {k}", sb[1], k)[0])
                    emit(dline(f"SSH PG IS, {k}", gb[0], k)[0])
            emit(dline("SSH IS, B15", sb[0], "B15")[0])
            emit("\n#### SSH: механіка")
            mechanics(sb[0], "E")
            mechanics(sb[0], "B")
    if sec:
        adj = holm([x[1] for x in sec])
        emit("\n| гілка | сире p (Δ > 0, IS) | Holm усередині кроку |\n|---|---|---|")
        for (lab, pr), pa in zip(sec, adj, strict=True):
            emit(f"| {lab} | {pr:.4f} | {pa:.4f} |")
    emit(f"\n(пікова RSS {rss_mb():.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=("prereg", "report"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_step2_early_exit")
    ap.add_argument("--s14-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/course_s14_ema")
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--ssh-cache", type=Path, default=Path.home() / ".cache/tradedesk-agents/session_stop_hunt")
    ap.add_argument("--symbols", type=int, help="timing sample: only the first N step-1 symbols, no set (b)")
    ap.add_argument("--out", type=Path, help="also write the printed lines to this file")
    a = ap.parse_args()
    cache = a.cache_dir.expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.out:
        a.out.expanduser().parent.mkdir(parents=True, exist_ok=True)
        OUT["fh"] = a.out.expanduser().open("a" if a.stage == "prereg" else "w")
    t0 = time.time()
    if a.stage == "prereg":
        stage_prereg()
    else:
        stage_report(a.s14_cache.expanduser(), a.data_dir.expanduser(), a.ssh_cache.expanduser(), cache, a.symbols)
    emit(f"\n[{a.stage}] finished in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
