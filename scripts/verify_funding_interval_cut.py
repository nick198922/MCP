"""Binance funding-interval cuts (8h -> 4h -> 1h) as a stress marker: step 0 (event list, causality, power gate).

Idea card: docs/ideas/IDEA-20261025-funding-interval-cut-stress.md ("Попередня реєстрація"). No network: reads the
per-file caches written by scripts/verify_funding_crowding.py (data.binance.vision archive):
    <cache>/funding/<SYM>/<YYYY-MM>.json   rows [calc_time_ms, interval_h, rate]
    <cache>/k1d/<SYM>/<YYYY-MM>.json       daily klines [open_ms, o, h, l, c, vol, quote_vol]
    <cache>/listing/<SYM>.json             archive months of the symbol (first month = listing month)

Stage ``step0`` prints counts, SDs and the MDE gate only. It never prints a mean return (the gate is decided first),
and ``--out`` holds only summary statistics and pre-entry per-event fields (``PRE_ENTRY_FIELDS``), no forward returns.

    uv run python scripts/verify_funding_interval_cut.py step0 \
        --cache-dir ~/.cache/tradedesk-agents/funding_crowding --out ~/.cache/tradedesk-agents/funding_interval_cut/step0.json
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from tradedesk.analytics.funding_interval_cut import (
    DAY_MS,
    Candidate,
    classify,
    dedup_changes,
    design_effect,
    detect_interval_changes,
    funding_pnl,
    iso_week,
    match_controls,
    sd,
    side_for,
)
from tradedesk.analytics.oi_flush import cluster_se_mean, mde

ARCHIVE_START = "2021-10"
COST = 2 * 0.00055 + 0.004  # taker x2 + 0.4 % round-trip slippage, on notional
GATE_PP = 0.035
K_CTRL, MIN_CTRL, MAX_DB3 = 5, 3, 0.05
GROUPS = ("H1", "H2", "H3a", "H3b")
# per-event fields that step 0 may write to disk: all known at or before entry (no forward returns)
PRE_ENTRY_FIELDS = frozenset(
    {
        "sym",
        "group",
        "t_label",
        "t_known",
        "h_prev",
        "h_new",
        "rate",
        "side",
        "day",
        "b1",
        "b3",
        "pre72_clean",
        "n_ctrl",
        "ctrl_db3",
        "nocap_db3",
        "week",
        "week_any",
    }
)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ymd(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def load_months(d: Path) -> list[list[float]]:
    rows: list[list[float]] = []
    if not d.is_dir():
        return rows
    for f in sorted(os.listdir(d)):
        rows += json.loads((d / f).read_text()) or []
    rows.sort(key=lambda r: r[0])
    return rows


def listing_ms(cache: Path, sym: str, first_row: float | None) -> int | None:
    """Listing time for the age filter: ``None`` if listed before the archive window (no age limit)."""
    p = cache / "listing" / f"{sym}.json"
    months = json.loads(p.read_text()) if p.exists() else None
    if months and min(months) < ARCHIVE_START:
        return None
    return None if first_row is None else int(first_row)


def step0(args: argparse.Namespace) -> None:
    cache: Path = args.cache_dir.expanduser()
    syms = sorted(s for s in os.listdir(cache / "funding") if s.endswith("USDT"))
    if args.sample:
        syms = syms[: args.sample]
    t0 = time.time()
    raw_count: Counter = Counter()
    lag_rows: Counter = Counter()
    trans: Counter = Counter()
    events: list[dict] = []
    changes_by_sym: dict[str, list[int]] = {}
    first_day: dict[str, int] = {}
    n_rows = 0
    for k, s in enumerate(syms):
        rows = load_months(cache / "funding" / s)
        n_rows += len(rows)
        if not rows:
            continue
        ch = detect_interval_changes(rows)
        changes_by_sym[s] = [c.t_known for c in ch]
        for c in ch:
            raw_count[c.kind] += 1
            if c.kind == "cut":
                trans[(c.h_prev, c.h_new)] += 1
                lag_rows["same row" if c.t_known == c.t_label else "next row"] += 1
        kept = dedup_changes(ch, first_row_ms=listing_ms(cache, s, rows[0][0]))
        for c in kept:
            g = classify(c)
            # funding received over the 72 h daily-bar hold: entry at the close of the UTC day containing t_known
            d0 = c.t_known - c.t_known % DAY_MS
            entry, exit_ = d0 + DAY_MS, d0 + 4 * DAY_MS
            events.append(
                {
                    "sym": s,
                    "group": g,
                    "t_label": c.t_label,
                    "t_known": c.t_known,
                    "h_prev": c.h_prev,
                    "h_new": c.h_new,
                    "rate": c.trigger_rate,
                    "side": side_for(c),
                    "day": d0,
                    "fund72": funding_pnl(rows, entry, exit_, side_for(c)),
                }
            )
        if (k + 1) % 100 == 0:
            print(f"  funding {k + 1}/{len(syms)} symbols, {n_rows} rows, {len(events)} kept, rss {rss_mb():.0f} MB", flush=True)
    print(f"funding: {len(syms)} symbols, {n_rows} rows in {time.time() - t0:.0f}s; rss {rss_mb():.0f} MB")
    print(f"confirmed changes (raw): {dict(raw_count)}; cut transitions {dict(sorted(trans.items()))}")
    print(f"cut known on: {dict(lag_rows)} (next row = label ahead of the settlement schedule)")
    kept_n = Counter(e["group"] for e in events)
    print(f"after dedup (14 d) and listing age (14 d): {dict(sorted(kept_n.items()))}")
    cuts = [e for e in events if e["group"] in ("H1", "H2", "H3a")]
    ar = sorted(abs(e["rate"]) for e in cuts)
    if ar:
        q = [ar[int(p * (len(ar) - 1))] for p in (0.1, 0.25, 0.5, 0.75, 0.9)]
        print("  |trigger rate| of deduped cuts p10/25/50/75/90: " + " ".join(f"{x:.4%}" for x in q))
    by_year = Counter((ymd(e["t_known"])[:4], e["group"]) for e in events)
    print("  by year: " + "; ".join(f"{y} {g}={n}" for (y, g), n in sorted(by_year.items())))

    # daily closes: one symbol at a time; keep only the days needed (event days D-3 .. D+3) per symbol
    need_days = sorted({e["day"] for e in events})
    offs = (-3, -1, 0, 3)
    # D-4 only for the clean pre-event window (close D-1 vs close D-4, entirely before the event day)
    want = {d + o * DAY_MS for d in need_days for o in (*offs, -4)}
    closes: dict[str, dict[int, float]] = {}
    for k, s in enumerate(syms):
        kl = load_months(cache / "k1d" / s)
        if not kl:
            continue
        first_day[s] = int(kl[0][0])
        closes[s] = {int(r[0]): float(r[4]) for r in kl if int(r[0]) in want}
        if (k + 1) % 200 == 0:
            print(f"  k1d {k + 1}/{len(syms)}, rss {rss_mb():.0f} MB", flush=True)

    def rets(s: str, d: int) -> tuple[float, float, float] | None:
        c = closes.get(s)
        if not c:
            return None
        v = [c.get(d + o * DAY_MS) for o in offs]
        if any(x is None or x <= 0 for x in v):
            return None
        cm3, cm1, c0, cp3 = v
        return c0 / cm1 - 1.0, c0 / cm3 - 1.0, cp3 / c0 - 1.0

    def eligible(s: str, d: int) -> bool:
        if first_day.get(s) is None or d - first_day[s] < 14 * DAY_MS:
            return False
        return not any(d - 14 * DAY_MS <= t <= d + 4 * DAY_MS for t in changes_by_sym.get(s, ()))

    pools: dict[int, list[Candidate]] = defaultdict(list)
    for d in need_days:
        for s in closes:
            if eligible(s, d):
                r = rets(s, d)
                if r is not None:
                    pools[d].append(Candidate(s, r[0], r[1], r[2]))
    print(
        f"control pools built for {len(need_days)} days; median pool {statistics.median(len(p) for p in pools.values()) if pools else 0}; rss {rss_mb():.0f} MB"
    )

    drop: Counter = Counter()
    for e in events:
        r = rets(e["sym"], e["day"])
        if r is None:
            drop[(e["group"], "no price window")] += 1
            continue
        b1, b3, f3 = r
        own = closes.get(e["sym"], {})
        cm1, cm4 = own.get(e["day"] - DAY_MS), own.get(e["day"] - 4 * DAY_MS)
        if cm1 and cm4:
            e["pre72_clean"] = cm1 / cm4 - 1.0
        pool = [c for c in pools[e["day"]] if c.key != e["sym"]]
        ctrl = match_controls(b1, b3, pool, k=K_CTRL, max_db3=MAX_DB3)
        e["b1"], e["b3"], e["ret72"] = b1, b3, e["side"] * f3
        e["trade72"] = e["side"] * f3 + e["fund72"] - COST
        e["week_any"] = iso_week(e["day"])
        # gate sensitivity only (exploratory, not the registered control): 5 nearest without the 5 pp cap
        nc = match_controls(b1, b3, pool, k=K_CTRL, max_db3=float("inf"))
        if nc:
            e["d72_nocap"] = e["trade72"] - statistics.fmean(e["side"] * c.fwd for c in nc)
            e["nocap_db3"] = statistics.fmean(abs(c.b3 - b3) for c in nc)
        e["n_ctrl"] = len(ctrl)
        if len(ctrl) < MIN_CTRL:
            drop[(e["group"], f"< {MIN_CTRL} matched controls")] += 1
            continue
        e["ctrl72"] = statistics.fmean(e["side"] * c.fwd for c in ctrl)
        e["ctrl_db3"] = statistics.fmean(abs(c.b3 - b3) for c in ctrl)
        e["d72"] = e["trade72"] - e["ctrl72"]
        e["week"] = iso_week(e["day"])
    print(f"dropped: {dict(sorted(drop.items()))}")

    # 1h coverage for step 1 (course_s14_ema/k1h): month file of the entry present?
    k1h = Path(args.k1h_dir).expanduser()
    cov = Counter()
    for e in events:
        if e["group"] == "H1" and "d72" in e:
            cov[(k1h / e["sym"] / f"{ymd(e['t_known'])[:7]}.json").exists()] += 1
    print(f"H1 events with a 1h kline month in {k1h}: {cov.get(True, 0)} of {sum(cov.values())}")

    out = {"gate_pp": GATE_PP, "cost": COST, "groups": {}}
    print("\nstep 0 power table (72 h, daily bars, matched-drawdown control; NO means shown)")
    print(
        f"{'group':5} {'n':>4} {'weeks':>5} {'days':>4} {'maxwk':>5} {'SD trade':>9} {'SD d':>7} {'SE_wk':>7} {'deff':>5} {'n_eff':>6} {'MDE_wk':>7} {'MDE_day':>7} {'SD pre24':>8} {'|Δb3|':>6}"
    )
    for g in GROUPS:
        sub = [e for e in events if e["group"] == g and "d72" in e]
        if len(sub) < 2:
            print(f"{g:5} {len(sub):>4}  (too few)")
            continue
        d = [e["d72"] for e in sub]
        wk = [e["week"] for e in sub]
        dy = [e["day"] for e in sub]
        se_w, se_d = cluster_se_mean(d, wk), cluster_se_mean(d, dy)
        sdd = sd(d)
        deff = design_effect(se_w, sdd, len(d))
        m_w, m_d = mde(se_w), mde(se_d)
        row = {
            "n": len(sub),
            "weeks": len(set(wk)),
            "days": len(set(dy)),
            "max_per_week": max(Counter(wk).values()),
            "sd_trade72": sd([e["trade72"] for e in sub]),
            "sd_d72": sdd,
            "sd_ctrl72": sd([e["ctrl72"] for e in sub]),
            "se_week": se_w,
            "se_day": se_d,
            "deff_week": deff,
            "n_eff_week": len(d) / deff if deff else None,
            "mde_week": m_w,
            "mde_day": m_d,
            "sd_pre24": sd([e["b1"] for e in sub]),
            "mean_abs_db3_ctrl": statistics.fmean(e["ctrl_db3"] for e in sub),
        }
        out["groups"][g] = row
        print(
            f"{g:5} {row['n']:>4} {row['weeks']:>5} {row['days']:>4} {row['max_per_week']:>5} {row['sd_trade72']:>9.2%} {sdd:>7.2%} "
            f"{se_w:>7.2%} {deff:>5.2f} {row['n_eff_week']:>6.0f} {m_w:>7.2%} {m_d:>7.2%} {row['sd_pre24']:>8.2%} {row['mean_abs_db3_ctrl']:>6.2%}"
        )
    h1 = out["groups"].get("H1", {})
    gate = h1.get("mde_week")
    out["gate_pass"] = gate is not None and gate <= GATE_PP
    print(
        f"\nGATE H1: MDE (week clusters, matched control) = {gate:.2%} vs threshold {GATE_PP:.1%} -> {'PASS' if out['gate_pass'] else 'FAIL'}"
    )
    # robustness of the gate itself (still no means): winsorised SD at 1/99 % and with 3 controls
    if h1:
        sub = [e for e in events if e["group"] == "H1" and "d72" in e]
        d = sorted(e["d72"] for e in sub)
        lo, hi = d[int(0.01 * (len(d) - 1))], d[int(0.99 * (len(d) - 1))]
        dw = [min(max(e["d72"], lo), hi) for e in sub]
        mw = mde(cluster_se_mean(dw, [e["week"] for e in sub]))
        print(f"  info: MDE with d winsorised at p1/p99 = {mw:.2%}; SD of 72 h trade alone = {h1['sd_trade72']:.2%} (card guessed 18 %)")
        out["mde_week_winsor"] = mw
        ex = sorted(sub, key=lambda e: abs(e["d72"]), reverse=True)[:5]
        print(
            "  info: 5 largest |d| events (sym, date, rate; values withheld): "
            + ", ".join(f"{e['sym']} {ymd(e['t_known'])} {e['rate']:+.4f}" for e in ex)
        )
        # exploratory gate sensitivity (designs not registered; MDE only, no means): does any design pass?
        allp = [e for e in events if e["group"] == "H1" and "trade72" in e]
        sens = {}
        for lab, key, rows_ in (
            ("all H1, 5 nearest controls without the 5 pp cap", "d72_nocap", [e for e in allp if "d72_nocap" in e]),
            ("all H1, trade vs zero (no control)", "trade72", allp),
            ("registered sample, trade vs zero (no control)", "trade72", sub),
        ):
            v = [e[key] for e in rows_]
            m = mde(cluster_se_mean(v, [e["week_any"] for e in rows_]))
            sens[lab] = {"n": len(v), "weeks": len({e["week_any"] for e in rows_}), "sd": sd(v), "mde_week": m}
            print(f"  sensitivity: {lab}: n={len(v)} weeks={sens[lab]['weeks']} SD {sd(v):.2%} MDE_wk {m:.2%}")
        nc = [e["nocap_db3"] for e in allp if "nocap_db3" in e]
        b3s = sorted(e["b3"] for e in allp)
        print(
            f"  info: H1 trailing 72 h return p10/50/90 {b3s[len(b3s) // 10]:+.1%} {statistics.median(b3s):+.1%} {b3s[9 * len(b3s) // 10]:+.1%}; "
            f"uncapped control mean |Δb3| {statistics.fmean(nc):.1%}, share > 5 pp {sum(x > MAX_DB3 for x in nc) / len(nc):.0%}"
        )
        print("    (window ends at the close of the event day D: includes the hours after t_known)")
        # clean pre-event window: close D-1 vs close D-4, ends before the UTC day of the event
        h1all = [e for e in events if e["group"] == "H1"]
        pc = sorted(e["pre72_clean"] for e in h1all if "pre72_clean" in e)
        hrs = sorted((e["t_known"] - e["day"]) / 3_600_000 for e in h1all)

        def qq(xs: list[float], p: float) -> float:
            return xs[int(p * (len(xs) - 1))]

        if pc:
            print(
                f"  info: H1 clean pre-event 72 h return (close D-1 / close D-4) n={len(pc)} p10/50/90 "
                f"{qq(pc, 0.1):+.1%} {statistics.median(pc):+.1%} {qq(pc, 0.9):+.1%}; share > 0 {sum(x > 0 for x in pc) / len(pc):.0%}"
            )
            print(f"  info: t_known hours into the UTC event day p10/50/90 {qq(hrs, 0.1):.1f} {qq(hrs, 0.5):.1f} {qq(hrs, 0.9):.1f}")
            out["pre72_clean"] = {
                "n": len(pc),
                "p10": qq(pc, 0.1),
                "p50": statistics.median(pc),
                "p90": qq(pc, 0.9),
                "share_pos": sum(x > 0 for x in pc) / len(pc),
            }
        out["gate_sensitivity"] = sens

    # sanity: FTTUSDT 2022-11 funding (known number from verify_funding_crowding.py)
    ftt = (
        json.loads((cache / "funding" / "FTTUSDT" / "2022-11.json").read_text())
        if (cache / "funding" / "FTTUSDT" / "2022-11.json").exists()
        else []
    )
    if ftt:
        print(f"sanity FTTUSDT 2022-11: {len(ftt)} settlements, min rate {min(r[2] for r in ftt):.4f} (expected 126, -0.0250)")
    print(f"done in {time.time() - t0:.0f}s, peak rss {rss_mb():.0f} MB")

    if args.out:
        o = Path(args.out).expanduser()
        o.parent.mkdir(parents=True, exist_ok=True)
        # step 0 stays blind: only pre-entry fields per event (no fund72 / ret72 / trade72 / ctrl72 / d72)
        out["events"] = [{k: v for k, v in e.items() if k in PRE_ENTRY_FIELDS} for e in events]
        o.write_text(json.dumps(out, default=str))
        print(f"wrote {o}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["step0"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--k1h-dir", default=str(Path.home() / ".cache/tradedesk-agents/course_s14_ema/k1h"))
    ap.add_argument("--out", default=None, help="JSON output (summary + pre-entry per-event fields only)")
    ap.add_argument("--sample", type=int, default=0, help="first N symbols only (timing)")
    args = ap.parse_args()
    {"step0": step0}[args.stage](args)


if __name__ == "__main__":
    sys.exit(main())
