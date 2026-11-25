"""30-day registered analysis: Deribit <-> Bybit daily option crossing (forward measurement, nothing trades).

Card: docs/ideas/IDEA-20261023-bybit-deribit-daily-option-dislocation-forward.md («Реєстрація (до збору)»,
«Реєстрація 2 (до збору)», both committed 23.10.2026 before collection). Reads the collector table
``option_xvenue_snapshot`` strictly read-only (SQLite URI mode=ro) and never touches rows at or after the decision
cut-off = first stored row + 30 days (fixed below before any crossing was computed; the 90-day sample may still be
needed, so later days stay unseen). Pure maths: analytics/option_xvenue.py (registered measure) and
analytics/option_xvenue_verdict.py (counts, CIs, verdict, episodes, journal).

    uv run python scripts/verify_option_xvenue.py --out ~/.cache/tradedesk-agents/bybit-deribit-daily-option-dislocation-forward
"""

from __future__ import annotations

import argparse
import json
import resource
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.option_xvenue import FEE_RATE, REG_THRESHOLD, num, registered_rows
from tradedesk.analytics.option_xvenue_verdict import (
    BOOT_REPS,
    BOOT_SEED,
    BYBIT_FEE_CAP,
    MAX_ABS_GAP,
    SIDES,
    abs_dt_ms,
    build_episodes,
    decision_cutoff,
    decision_rows,
    frequency,
    index_gap,
    km_median,
    leg_gap_bounds,
    paper_trade,
    persistence,
    row_measure,
    snapshot_leg_gaps,
    snapshot_outcome,
    snapshot_outcomes,
    sync_class,
    verdict_30d,
)

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DECISION_DAYS = 30  # registration: «За 30 днів ... від першого рядка після ввімкнення»; cut-off = first ts + 30 x 24 h
LOT_STEP = {"BTC": 0.01, "ETH": 0.1}
EXIT_MAX_TAU_H = 3.5  # the last snapshot of an expiry is due at tau ~2.4-3.4 h; later than that = exit snapshot missing
REG_GROSS = REG_THRESHOLD + 2 * FEE_RATE  # gross crossing needed for net >= threshold at the primary fees (descriptive)

A = lambda r: sync_class(r, "quote") == "A"  # noqa: E731
VARIANTS: dict[str, tuple[Any, dict[str, Any], str]] = {
    # name: (eligible row, row_measure kwargs, strike set)
    "main": (A, {}, "reg"),
    "fwd_g": (A, {"gap_source": "underlying"}, "reg"),
    "fee_b_0.06%": (A, {"fee_bybit": 0.0006}, "reg"),
    "fee_b_0.10%": (A, {"fee_bybit": 0.0010}, "reg"),
    "cap7 (blocker ii)": (A, {"bybit_cap": BYBIT_FEE_CAP}, "reg"),
    "depth_0.5": (A, {"min_depth": 0.5}, "reg"),
    "no_depth (descr)": (A, {"min_depth": 0.0}, "reg"),
    "sync<=1s": (lambda r: abs_dt_ms(r, "quote") is not None and abs_dt_ms(r, "quote") <= 1000, {}, "reg"),
    "class_B": (lambda r: sync_class(r, "quote") == "B", {}, "reg"),
    "A_r (blocker i)": (lambda r: sync_class(r, "recv") == "A", {}, "reg"),
    "A_r<=1s": (lambda r: abs_dt_ms(r, "recv") is not None and abs_dt_ms(r, "recv") <= 1000, {}, "reg"),
    "raw (descr)": (A, {"gap_source": "none"}, "reg"),
    "ATM+-3 (descr)": (A, {}, "all"),
}


def q(xs: list[float], p: float) -> float | None:
    return float(np.percentile(xs, p)) if xs else None


def pct(x: float | None, nd: int = 3) -> str:
    return "—" if x is None else f"{100 * x:.{nd}f}%"


def ci(f: dict[str, Any]) -> str:
    b, w = f["boot"], f["wilson_days"]
    return ("boot " + (f"[{pct(b[0])}; {pct(b[1])}]" if b else "—")) + (" wilson(n=days) " + (f"[{pct(w[0])}; {pct(w[1])}]" if w else "—"))


def hour_block(ts: int) -> str:
    h = datetime.fromtimestamp(ts, UTC).hour
    return "12-17" if 12 <= h <= 17 else "18-23" if h >= 18 else "00-06"


def analyse(cur: str, rows: list[dict[str, Any]], cutoff: int, reps: int, out: list[str]) -> dict[str, Any]:
    P = out.append
    rows = decision_rows(rows, cutoff)  # second guard after the SQL `ts < cutoff`; tested in test_option_xvenue_verdict
    hourly = [r for r in rows if r["mode"] == "hourly"]
    snaps: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in hourly:
        snaps[r["ts"]].append(r)
    ts_sorted = sorted(snaps)
    reg: dict[int, list[dict[str, Any]]] = {t: list(registered_rows(snaps[t])) for t in ts_sorted}
    mismatch = sum(
        1
        for t in ts_sorted
        if {(r["strike"], r["opt"]) for r in reg[t]} != {(r["strike"], r["opt"]) for r in snaps[t] if r["reg_atm"] == 1}
    )
    res: dict[str, Any] = {"currency": cur}
    P(f"\n=== {cur} ===")
    P(
        f"hourly snapshots {len(ts_sorted)}, rows {len(hourly)}, minute rows {len(rows) - len(hourly)}; expiries {len({r['expiry_ts'] for r in hourly})}"
    )
    P(
        f"rows/snapshot {Counter(len(v) for v in snaps.values())}; registered rows/snapshot {Counter(len(v) for v in reg.values())}; registered set != stored reg_atm in {mismatch} snapshots"
    )

    # ---------- data quality (registered rows)
    rr = [r for t in ts_sorted for r in reg[t]]
    P(
        f"registered rows: {len(rr)}; sync class (quote) {dict(Counter(sync_class(r, 'quote') for r in rr))}; A_r class {dict(Counter(sync_class(r, 'recv') for r in rr))}"
    )
    d_cache = sum(1 for r in rr if r["d_ts_ms"] is not None and r["d_recv_ms"] is None)
    b_cache = sum(1 for r in rr if r["b_ts_ms"] is not None and r["b_recv_ms"] is None)
    d_noleg = sum(1 for r in rr if r["d_ts_ms"] is None)
    b_noleg = sum(1 for r in rr if r["b_ts_ms"] is None)
    P(
        f"recv NULL on registered rows (Deribit-leg rows by construction): Deribit cache {d_cache} / no leg {d_noleg}; Bybit cache {b_cache} / no leg {b_noleg}"
    )
    # leg completeness over ALL stored rows of each snapshot (a row may carry one venue's leg only)
    lg = {t: snapshot_leg_gaps(snaps[t]) for t in ts_sorted}
    gap_d = {t for t, g in lg.items() if g["d_missing"]}
    gap_b = {t for t, g in lg.items() if g["b_missing"]}
    gap_any = gap_d | gap_b
    gap_atm = {t for t, g in lg.items() if g["d_missing_atm"]}
    day = lambda t: datetime.fromtimestamp(t, UTC).strftime("%m-%d")  # noqa: E731
    P(
        f"missing Deribit legs (all rows): {sum(g['d_missing'] for g in lg.values())} rows in {len(gap_d)} snapshots; by #missing {dict(sorted(Counter(g['d_missing'] for g in lg.values() if g['d_missing']).items()))}; "
        f"by snapshot UTC date {dict(sorted(Counter(day(t) for t in gap_d).items()))}; with a missing Deribit leg in ATM+-1 around the Bybit fwd {len(gap_atm)}"
    )
    P(
        f"missing Bybit legs (all rows): {sum(g['b_missing'] for g in lg.values())} rows in {len(gap_b)} snapshots; by #missing {dict(sorted(Counter(g['b_missing'] for g in lg.values() if g['b_missing']).items()))}; "
        f"in registered ATM+-1 rows {b_noleg}; by snapshot UTC date {dict(sorted(Counter(day(t) for t in gap_b).items()))}"
    )
    res["leg_gaps"] = {
        "d_rows": sum(g["d_missing"] for g in lg.values()),
        "d_snapshots": len(gap_d),
        "b_rows": sum(g["b_missing"] for g in lg.values()),
        "b_snapshots": len(gap_b),
        "d_atm_snapshots": len(gap_atm),
    }
    gaps = [g for g in (index_gap(r) for r in rr) if g is not None]
    anom = sum(1 for g in gaps if abs(g) > MAX_ABS_GAP)
    fg = [num(r["b_underlying"]) / num(r["d_underlying"]) - 1 for r in rr if num(r["b_underlying"]) and num(r["d_underlying"])]
    P(
        f"index gap g: median {pct(q(gaps, 50))} p5 {pct(q(gaps, 5))} p95 {pct(q(gaps, 95))}; |g|>1% anomaly rows {anom}; forward gap median {pct(q(fg, 50))} p5 {pct(q(fg, 5))} p95 {pct(q(fg, 95))}"
    )
    miss = Counter()
    for r in rr:
        for k in ("d_bid", "d_ask", "b_bid", "b_ask"):
            miss[k] += r[k] is None
    P(f"missing touch quotes on registered rows: {dict(miss)}; unmeasurable (main measure) {sum(1 for r in rr if row_measure(r) is None)}")
    # snapshot coverage
    cov = Counter()
    for t in ts_sorted:
        cls = {sync_class(r, "quote") for r in reg[t] if row_measure(r) is not None}
        cov["A" if "A" in cls else "only_B" if "B" in cls else "only_C" if "C" in cls else "unmeasurable"] += 1
    P(f"snapshot coverage: {dict(cov)}")
    # Реєстрація 2 diagnostics
    age = [(r["d_recv_ms"] - r["d_ts_ms"]) / 1000 for r in rr if r["d_recv_ms"] is not None and r["d_ts_ms"] is not None]
    rg = [abs(r["b_recv_ms"] - r["d_recv_ms"]) / 1000 for r in rr if r["b_recv_ms"] is not None and r["d_recv_ms"] is not None]
    bclk = [(r["b_recv_ms"] - r["b_ts_ms"]) / 1000 for r in rr if r["b_recv_ms"] is not None and r["b_ts_ms"] is not None]
    qd = [abs_dt_ms(r) / 1000 for r in rr if abs_dt_ms(r) is not None]
    P(
        f"Deribit quote age d_recv-d_ts, s: median {q(age, 50):.2f} p90 {q(age, 90):.2f} p99 {q(age, 99):.2f}; >5s {sum(a > 5 for a in age) / len(age):.3f}; <-0.5s {sum(a < -0.5 for a in age)}"
        if age
        else "quote age: no data"
    )
    if rg:
        P(
            f"read gap |b_recv-d_recv|, s: median {q(rg, 50):.3f} p90 {q(rg, 90):.3f} p99 {q(rg, 99):.3f}; |b_ts-d_ts| s: median {q(qd, 50):.2f} p90 {q(qd, 90):.2f} p99 {q(qd, 99):.2f}"
        )
    if bclk:
        m = statistics.median(bclk)
        P(
            f"clock check median(b_recv-b_ts) {m:.3f} s -> flag {'CLOCK SKEW' if abs(m) > 1 else 'ok'}; negative Deribit age < -0.5 s flag: {'YES' if any(a < -0.5 for a in age) else 'no'}"
        )
    step = LOT_STEP[cur]
    sz = [x for r in hourly for x in (r["b_bid_size"], r["b_ask_size"]) if x is not None]
    lot_ok = sum(1 for x in sz if abs(x - round(x / step) * step) <= 1e-9) / len(sz) if sz else None
    P(
        f"Bybit size lot-step ({step}) multiple share: {lot_ok:.4f} of {len(sz)} sizes -> {'ok' if lot_ok is not None and lot_ok >= 0.99 else 'REVIEW unit'}"
    )
    res["quality"] = {
        "snapshots": len(ts_sorted),
        "reg_rows": len(rr),
        "coverage": dict(cov),
        "anomaly_rows": anom,
        "lot_ok": lot_ok,
        "reg_mismatch": mismatch,
    }

    # ---------- frequencies by variant
    P("\nvariant | n snapshots (denominator) | k | p | 95% CI")
    freqs: dict[str, dict[str, Any]] = {}
    hits_main: dict[int, list[tuple[dict[str, Any], str]]] = {}
    outc_main: list[tuple[int, Any, bool, bool]] = []
    for name, (elig, kw, sset) in VARIANTS.items():
        outc4 = snapshot_outcomes(rows, cutoff, elig, lambda r, kw=kw: row_measure(r, **kw), strikes=sset)
        if name == "main":
            outc_main = outc4
        f = frequency([(day, d, h) for _, day, d, h in outc4], reps=reps)
        freqs[name] = f
        P(f"{name:18s} | {f['n']:4d} | {f['k']:3d} | {pct(f['p'])} | {ci(f)} | days {f['days']}")
    # missing legs (added after the critic's review 25.11.2026, NOT registered): sensitivity and worst case
    P("missing-leg sensitivity (added after review, not registered):")
    for lab, aff in (
        ("missing Deribit leg (any strike)", gap_d),
        ("missing Deribit leg in ATM+-1 (Bybit fwd)", gap_atm),
        ("missing leg on either venue", gap_any),
    ):
        lb = leg_gap_bounds(outc_main, aff, reps=reps)
        w, wo = lb["worst"], lb["without"]
        P(
            f"  {lab}: {len(aff)} snapshots, {lb['affected_in_denom']} of them in the main denominator; "
            f"without them {wo['k']}/{wo['n']} = {pct(wo['p'])} {ci(wo)}; worst case (all of them crossings) {w['k']}/{w['n']} = {pct(w['p'])}"
        )
        res.setdefault("leg_gaps_sensitivity", {})[lab] = {
            "snapshots": len(aff),
            "in_denom": lb["affected_in_denom"],
            "without": [wo["k"], wo["n"]],
            "worst": [w["k"], w["n"]],
        }
    # halves (time split) for main
    half = ts_sorted[len(ts_sorted) // 2] if ts_sorted else 0
    for lab, sel in (("first half", lambda t: t < half), ("second half", lambda t: t >= half)):
        outc = [(reg[t][0]["expiry_ts"] if reg[t] else t, *snapshot_outcome(reg[t], A, row_measure)) for t in ts_sorted if sel(t)]
        f = frequency(outc, reps=reps)
        P(f"main {lab:11s} | {f['n']:4d} | {f['k']:3d} | {pct(f['p'])} | {ci(f)}")
    res["freq"] = {k: {kk: v[kk] for kk in ("n", "k", "p", "days", "boot", "wilson_days")} for k, v in freqs.items()}

    # ---------- row-level distributions (class A, registered, main measure)
    best, best_raw, hits_rows, spreads_b, spreads_d = [], [], [], [], []
    by = Counter()
    mids: dict[str, list[float]] = {"C": [], "P": []}
    a_rows = [r for r in rr if A(r)]
    for r in a_rows:
        m = row_measure(r)
        if m is None:
            continue
        mr = row_measure(r, gap_source="none")
        best.append(m["best"]) if m["best"] is not None else None
        if mr is not None and mr["best"] is not None:
            best_raw.append(mr["best"])
        s = r["d_index"]
        if None not in (r["b_bid"], r["b_ask"], r["d_bid"], r["d_ask"]):
            spreads_b.append((r["b_ask"] - r["b_bid"]) / s)
            spreads_d.append(r["d_ask"] - r["d_bid"])  # Deribit quotes in the coin: already a fraction of the index
            mids[r["opt"]].append(((r["b_bid"] + r["b_ask"]) / 2 + m["shift_usd"] - (r["d_bid"] + r["d_ask"]) / 2 * s) / s)
        for side in SIDES:
            v = m["sides"][side]
            if v is not None and v["hit"]:
                hits_rows.append((r, side, v))
                ks = sorted({x["strike"] for x in reg[r["ts"]]})
                by[("side", side)] += 1
                by[("opt", r["opt"])] += 1
                by[("strike_off", ks.index(r["strike"]) - 1 if len(ks) == 3 else "?")] += 1
                by[("hours", hour_block(r["ts"]))] += 1
                hits_main.setdefault(r["ts"], []).append((r, side))
    P(
        f"\nclass A registered measurable rows {len(best)} (with a depth-ok side); best net_frac after fees: median {pct(q(best, 50))} p90 {pct(q(best, 90))} p99 {pct(q(best, 99))} max {pct(max(best) if best else None)}; >= 0: {sum(b >= 0 for b in best)}"
    )
    P(f"raw (no g) best net_frac: median {pct(q(best_raw, 50))} p99 {pct(q(best_raw, 99))} max {pct(max(best_raw) if best_raw else None)}")
    P(
        f"Bybit spread, frac of index: median {pct(q(spreads_b, 50))} p90 {pct(q(spreads_b, 90))}; Deribit spread same unit: median {pct(q(spreads_d, 50))} p90 {pct(q(spreads_d, 90))}"
    )
    mc, mp = (statistics.fmean(mids[o]) if mids[o] else None for o in ("C", "P"))
    flag = mc is not None and mp is not None and (mc * mp < 0 and abs(mc - mp) > 0.0005 or abs(mc - mp) > 0.0005)
    P(
        f"adjustment check mean (B_adj_mid - D_mid)/spot: calls {pct(mc)} (n {len(mids['C'])}), puts {pct(mp)} (n {len(mids['P'])}) -> {'INCOMPLETE ADJUSTMENT' if flag else 'ok'}"
    )
    # descriptive execution facts (added after review; hourly REST snapshots, top of book, registered ATM +- 1, class A)
    full = [r for r in a_rows if None not in (r["b_bid"], r["b_ask"], r["d_bid"], r["d_ask"]) and row_measure(r) is not None]
    inside: dict[str, list[bool]] = {"C": [], "P": []}
    for r in full:
        bm = (r["b_bid"] + r["b_ask"]) / 2 + row_measure(r)["shift_usd"]
        inside[r["opt"]].append(r["d_bid"] * r["d_index"] <= bm <= r["d_ask"] * r["d_index"])
    dpx = [x for r in full for x in (r["d_bid"], r["d_ask"])]
    dbid = [r["d_bid"] for r in full]
    mult = lambda x, s: abs(x / s - round(x / s)) < 1e-6  # noqa: E731
    one_tick = sum(1 for r in full if abs(r["b_ask"] - r["b_bid"] - 5.0) < 1e-9) / max(1, len(full))
    gross0 = [m["best"] for r in full if (m := row_measure(r, fee_deribit=0.0, fee_bybit=0.0))["best"] is not None]
    P(
        f"adjusted Bybit mid inside the Deribit book: calls {statistics.fmean(inside['C']) if inside['C'] else float('nan'):.3f}, puts {statistics.fmean(inside['P']) if inside['P'] else float('nan'):.3f}; "
        f"needed gross {pct(REG_GROSS)} / median Deribit spread {pct(q(spreads_d, 50))} = {REG_GROSS / q(spreads_d, 50):.2f}x"
        if spreads_d
        else "mid-in-book: no data"
    )
    P(
        f"Deribit prices < 0.005 coin: {sum(x < 0.005 for x in dpx) / max(1, len(dpx)):.3f}; d_bid multiple of 0.0001 {sum(mult(x, 0.0001) for x in dbid) / max(1, len(dbid)):.3f}, "
        f"of 0.0005 {sum(mult(x, 0.0005) for x in dbid) / max(1, len(dbid)):.3f}; Bybit spread = 5 USDT {one_tick:.3f}; "
        f"median touch size Bybit bid/ask {q([r['b_bid_size'] for r in full if r['b_bid_size'] is not None], 50)}/{q([r['b_ask_size'] for r in full if r['b_ask_size'] is not None], 50)}, "
        f"Deribit {q([r['d_bid_size'] for r in full if r['d_bid_size'] is not None], 50)}/{q([r['d_ask_size'] for r in full if r['d_ask_size'] is not None], 50)}"
    )
    gross0n = [m["best"] for r in full if (m := row_measure(r, fee_deribit=0.0, fee_bybit=0.0, min_depth=0.0))["best"] is not None]
    P(
        f"registered class A, adjusted, zero fees: best gross max depth-ok {pct(max(gross0) if gross0 else None)} (>= 0.15%: {sum(x >= 0.0015 for x in gross0)}), "
        f"no depth filter {pct(max(gross0n) if gross0n else None)} (>= 0.15%: {sum(x >= 0.0015 for x in gross0n)})"
    )
    raw_all = []
    for r in hourly:
        m = row_measure(r, gap_source="none", fee_deribit=0.0, fee_bybit=0.0, min_depth=0.0)
        if m is not None:
            raw_all.extend((v["net_frac"], r) for v in m["sides"].values() if v is not None)
    big = [r for x, r in raw_all if x >= 0.0015]
    P(
        f"all stored rows, any class, raw (no g), zero fees, no depth: max gross {pct(max((x for x, _ in raw_all), default=None))}; sides >= 0.15% {len(big)} "
        f"in snapshots {sorted({datetime.fromtimestamp(r['ts'], UTC).strftime('%m-%d %H:%M') for r in big})} classes {dict(Counter(sync_class(r) for r in big))}"
    )
    nf = [v["net_frac"] for _, _, v in hits_rows]
    P(f"main crossing rows {len(hits_rows)}: net_frac median {pct(q(nf, 50))} p90 {pct(q(nf, 90))}; breakdown {dict(by)}")
    # A_r-not-A rows ("B only by age")
    ar_not_a = [r for r in rr if sync_class(r, "recv") == "A" and not A(r)]
    hr = lambda xs: (sum(1 for r in xs if (m := row_measure(r)) is not None and m["hit"]), sum(1 for r in xs if row_measure(r) is not None))  # noqa: E731
    h1, n1 = hr(ar_not_a)
    h2, n2 = hr(a_rows)
    P(
        f"rows A_r but not A ('B only by age'): {len(ar_not_a)} ({len(ar_not_a) / max(1, len(rr)):.3f} of registered); row hit rate {h1}/{n1} vs class A {h2}/{n2}"
    )

    # ---------- hourly persistence
    seq = [
        (t, reg[t][0]["expiry_ts"] if reg[t] else None, {(r["strike"], r["opt"], s) for r, s in hits_main.get(t, [])}) for t in ts_sorted
    ]
    tot, again = persistence(seq)
    P(f"persistence after 1 h: {again}/{tot}")

    # ---------- episodes (all rows, both modes; class A, measurable)
    obs: dict[tuple, list[tuple[int, bool, bool]]] = defaultdict(list)
    for r in rows:
        if not A(r):
            continue
        m = row_measure(r)
        if m is None:
            continue
        for side in SIDES:
            v = m["sides"][side]
            obs[(r["strike"], r["opt"], side)].append((r["ts"], bool(v and v["hit"]), r["reg_atm"] == 1))
    eps = [e for k in obs for e in build_episodes(sorted(obs[k]))]
    ev = [e for e in eps if e["event"]]
    P(
        f"episodes {len(eps)} (M {sum(e['kind'] == 'M' for e in eps)}, H {sum(e['kind'] == 'H' for e in eps)}), events {len(ev)}, censored {len(eps) - len(ev)}; "
        f"KM median (right end) {km_median([e['d_right'] for e in eps], [e['event'] for e in eps]) if eps else None} s; lower-bound median (left end) {statistics.median([e['d_left'] for e in eps]) if eps else None} s"
    )
    res["episodes"] = {"n": len(eps), "events": len(ev)}

    # ---------- paper journal (hourly, main rule)
    by_exp: dict[int, list[int]] = defaultdict(list)
    for t in ts_sorted:
        if snaps[t]:
            by_exp[snaps[t][0]["expiry_ts"]].append(t)
    trades: list[dict[str, Any]] = []
    for exp, tss in by_exp.items():
        tss.sort()
        last = tss[-1]
        exit_ok = snaps[last][0]["tau_hours"] <= EXIT_MAX_TAU_H
        seen: set = set()
        for t in tss[:-1]:
            for r, side in hits_main.get(t, []):
                key = (r["strike"], r["opt"], side)
                if key in seen:
                    continue
                seen.add(key)
                same = [x for tt in tss if t <= tt <= last for x in snaps[tt] if (x["strike"], x["opt"]) == (r["strike"], r["opt"])]
                max_b = max((x["b_ask"] for x in same if x["b_ask"] is not None), default=None)
                max_d = max((x["d_ask"] * x["d_index"] for x in same if x["d_ask"] is not None and x["d_index"]), default=None)
                ex = next((x for x in snaps[last] if (x["strike"], x["opt"]) == (r["strike"], r["opt"])), None) if exit_ok else None
                if exit_ok and ex is None:
                    ex = {"d_index": snaps[last][0]["d_index"], "b_index": snaps[last][0]["b_index"]}
                li_d = next((x["d_index"] for tt in reversed(tss) for x in snaps[tt] if x["d_index"]), r["d_index"])
                li_b = next((x["b_index"] for tt in reversed(tss) for x in snaps[tt] if x["b_index"]), r["b_index"] or r["d_index"])
                kw = {"max_ask_b": max_b, "max_ask_d_usd": max_d, "last_index_d": li_d, "last_index_b": li_b}
                tr = paper_trade(r, ex, side, **kw)
                tr_cap = paper_trade(r, ex, side, bybit_cap=BYBIT_FEE_CAP, **kw)
                m_in = row_measure(r)["sides"][side]
                m_out = row_measure(ex) if ex is not None and "strike" in ex else None
                sh_in = row_measure(r)["shift_usd"]
                basis = None if m_out is None else ((m_out["shift_usd"] - sh_in) if side == "b_over_d" else (sh_in - m_out["shift_usd"]))
                trades.append(
                    {
                        "day": exp,
                        "pnl": tr["pnl_usd"],
                        "pnl_frac": tr["pnl_frac"],
                        "pnl_cap": tr_cap["pnl_usd"],
                        "cons": tr["conservative"],
                        "entry_net_usd": m_in["net_frac"] * r["d_index"],
                        "basis_usd": basis,
                    }
                )
    if trades:
        pn = [t["pnl"] for t in trades]
        days = sorted({t["day"] for t in trades})
        grp = [[t["pnl"] for t in trades if t["day"] == d] for d in days]
        rng = np.random.default_rng(BOOT_SEED)
        bs = [np.mean([x for i in rng.integers(0, len(grp), len(grp)) for x in grp[i]]) for _ in range(reps)]
        P(
            f"paper journal: {len(trades)} trades over {len(days)} days; mean {statistics.fmean(pn):.1f} USD/BTC ({pct(statistics.fmean(t['pnl_frac'] for t in trades))} of index, notional); "
            f"day-boot CI [{np.percentile(bs, 2.5):.1f}; {np.percentile(bs, 97.5):.1f}]; with cap7 mean {statistics.fmean(t['pnl_cap'] for t in trades):.1f}; "
            f"conservative fills {sum(t['cons'] for t in trades)}; entry crossing mean {statistics.fmean(t['entry_net_usd'] for t in trades):.1f}; basis part mean "
            f"{statistics.fmean([t['basis_usd'] for t in trades if t['basis_usd'] is not None] or [float('nan')]):.1f}"
        )
    else:
        P("paper journal: 0 trades (no main crossing with a later snapshot in its expiry)")
    res["journal"] = {"n": len(trades), "mean_usd": statistics.fmean([t["pnl"] for t in trades]) if trades else None}

    # ---------- minute mode share
    minute = [r for r in rows if r["mode"] == "minute"]
    P(
        f"minute mode: {len({r['ts'] for r in minute})} minute ticks, {len(minute)} rows; rows by mode {dict(Counter(r['mode'] for r in rows))}"
    )

    if cur == "BTC":
        v = verdict_30d(freqs["main"], freqs["fee_b_0.06%"], freqs["fwd_g"], freqs["A_r (blocker i)"], freqs["cap7 (blocker ii)"])
        res["verdict"] = v
        P(f"\nVERDICT (30-day branch, BTC): status={v['status']} branch={v['branch']} reason: {v['reason']}")
        for b in v["blockers"]:
            P(f"  blocker {b['name']}: blocks={b['blocks']} ({b['note']})")
        P(f"  life: {len(eps)} episodes < 20 -> ◯" if len(eps) < 20 else f"  life: {len(eps)} episodes")
        P(f"  journal: {len(trades)} trades < 30 -> ◯" if len(trades) < 30 else f"  journal: {len(trades)} trades")
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", required=True, help="output dir (report.txt, result.json)")
    ap.add_argument("--reps", type=int, default=BOOT_REPS)
    a = ap.parse_args()
    out_dir = Path(a.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    first = con.execute("SELECT MIN(ts) FROM option_xvenue_snapshot").fetchone()[0]
    cutoff = decision_cutoff(first, DECISION_DAYS)
    lines: list[str] = []
    P = lines.append
    fmt = lambda t: datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")  # noqa: E731
    P(
        f"decision sample: first row {fmt(first)}, cut-off (exclusive) {fmt(cutoff)} = first + {DECISION_DAYS} d; rows at/after it are not read for crossings"
    )
    # integrity over the whole table: counts only, no prices
    tot = con.execute("SELECT COUNT(*), MAX(ts), COUNT(DISTINCT ts) FROM option_xvenue_snapshot").fetchone()
    P(f"table: {tot[0]} rows, {tot[2]} snapshot times, last {fmt(tot[1])}")
    modes = con.execute("SELECT mode, COUNT(*) FROM option_xvenue_snapshot GROUP BY 1").fetchall()
    P(f"whole table rows by mode (counts only, no prices; includes days after the cut-off): {dict(modes)}")
    per_day = con.execute(
        "SELECT expiry_ts, currency, COUNT(DISTINCT ts), COUNT(*), SUM(d_instrument IS NULL OR d_instrument = ''), "
        "SUM(b_symbol IS NULL OR b_symbol = '') FROM option_xvenue_snapshot WHERE ts < ? AND mode='hourly' GROUP BY 1,2 ORDER BY 1,2",
        (cutoff,),
    ).fetchall()
    # completeness by legs, not rows: a row stored with one venue's leg only still counts as a row
    short = [(fmt(e)[:10], c, n, k, dm, bm) for e, c, n, k, dm, bm in per_day if n != 18 or k != 252 or dm or bm]
    P(
        f"expiries in sample per currency: {Counter(c for _, c, *_ in per_day)}; expiries with != 18 snapshots, != 252 rows or a missing leg "
        f"(date, cur, snapshots, rows, rows w/o Deribit leg, rows w/o Bybit leg): {short}"
    )
    ts_all = [
        t for (t,) in con.execute("SELECT DISTINCT ts FROM option_xvenue_snapshot WHERE ts < ? AND mode='hourly' ORDER BY ts", (cutoff,))
    ]
    outages = [
        (fmt(x), fmt(y))
        for x, y in zip(ts_all, ts_all[1:], strict=False)
        if y - x > 1.6 * 3600 and not (datetime.fromtimestamp(x, UTC).hour == 5 and y - x < 7.6 * 3600)
    ]
    P(f"collector outages (gap > 1.6 h outside 06-12 UTC): {outages or 'none'}")
    try:
        size = con.execute("SELECT SUM(pgsize) FROM dbstat WHERE name = 'option_xvenue_snapshot'").fetchone()[0]
        P(f"table size (dbstat): {size / 1e6:.2f} MB for {tot[0]} rows ({size / tot[0]:.0f} B/row)")
    except sqlite3.Error as e:
        pc = con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
        P(f"table size: dbstat unavailable ({e}); whole DB {pc / 1e9:.2f} GB")
    rows = [dict(r) for r in con.execute("SELECT * FROM option_xvenue_snapshot WHERE ts < ? ORDER BY ts, currency, strike, opt", (cutoff,))]
    con.close()
    results = {"first_ts": first, "cutoff_ts": cutoff}
    for cur in ("BTC", "ETH"):
        results[cur] = analyse(cur, [r for r in rows if r["currency"] == cur], cutoff, a.reps, lines)
    P(f"\npeak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")
    text = "\n".join(lines)
    (out_dir / "report.txt").write_text(text + "\n")
    (out_dir / "result.json").write_text(json.dumps(results, indent=1, default=str))
    print(text)


if __name__ == "__main__":
    main()
