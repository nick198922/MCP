"""Item 8n of IDEA-20261031-structural-edges-depth: carry A exit on a delisting announcement.

Stages (in order):
  gate     match Binance CMS delisting announcements (catalog 161, cached by `verify_news_events.py ann`) to the
           carry symbols (bench of item 2 + the 69 delisted candidates of item 8b); count (iii) events and symbols
           with an announcement, lead "announcement -> perp end" -> gate.json. No P&L is printed here.
  run      only if the gate passes (>= 10 events with an announcement): (ii), (iii), (iv) = (iii) + exit on the
           announcement, cost models 'main'/'stress', settlement proxies 'perp_last'/'spot'; paired per-event
           diffs; placebo of information-free early exits -> --out (default results.json)

No network: everything is read from the caches of items 8b (~/.cache/tradedesk-agents/carry_a_delisting/) and of
the news-events study (~/.cache/tradedesk-agents/news_events/). Returns on capital (A: capital = 1.5 x notional).
Announcement titles and bodies are untrusted data: only tickers are parsed from them.
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_carry_a_delisting as dl  # noqa: E402
import verify_carry_alts as alts  # noqa: E402
import verify_news_events as nev  # noqa: E402

from tradedesk.analytics.carry_announcement import (  # noqa: E402
    announcement_index,
    announcement_rule,
    carry_symbol_ids,
    first_announcement,
    paired_event_diffs,
    placebo_windows,
    window_rule,
)
from tradedesk.analytics.carry_costs import CostModel, risk_summary, weekly_changes  # noqa: E402
from tradedesk.analytics.carry_delisting import simulate_carry_delisting, vanish_events  # noqa: E402
from tradedesk.analytics.carry_modules import block_bootstrap, diff_curve, window_weekly  # noqa: E402
from tradedesk.analytics.news_events import classify_delisting  # noqa: E402

HOME = Path.home() / ".cache" / "tradedesk-agents"
DEFAULT_CACHE = HOME / "carry_a_announcement_exit"
NEWS = HOME / "news_events"
CAPITAL = dl.CAPITAL
H8 = dl.H8
GATE_MIN = 10
SETTLES = ("perp_last", "spot")
N_PLACEBO = 50
PLACEBO_SEED = 20261101
N_BOOT = 10_000

log, fmt, pct = dl.log, dl.fmt, dl.pct


def binance_rows() -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Binance delisting-catalog announcements as ``{ms, kind, tickers, title}`` (titles untrusted)."""
    rows: list[dict[str, Any]] = []
    cnt = {"articles": 0, "perp_delist": 0, "spot_delist": 0, "needs_body_missing": 0, "ignored": 0}
    for r in nev.load_ann(NEWS):
        if r["source"] != "binance" or r["catalog"] != "delisting":
            continue
        cnt["articles"] += 1
        kind, ticks = classify_delisting("binance", r["title"])
        if kind == "needs_body":
            p = nev.body_path(NEWS, r)
            if not p.exists():
                cnt["needs_body_missing"] += 1
                continue
            b = json.loads(p.read_text())
            kind, ticks = (
                ("perp_delist", b["perps"]) if ("Perpetual" in r["title"] or "Futures" in r["title"]) else ("spot_delist", b["pairs"])
            )
        if kind not in ("perp_delist", "spot_delist"):
            cnt["ignored"] += 1
            continue
        cnt[kind] += 1
        rows.append({"ms": r["ms"], "kind": kind, "tickers": ticks, "title": r["title"][:120]})
    rows.sort(key=lambda x: x["ms"])
    return rows, cnt


def load_all(exclude: frozenset[str] = frozenset()) -> dict[str, Any]:
    start, end, uni = dl.bench_window()
    cal = json.loads(dl.CALIB.read_text())
    models = {k: CostModel(cal["models"][k]["fees"], cal["models"][k]["slippage"], cal["models"][k]["timing"]) for k in ("main", "stress")}
    bench = alts.load_markets(dl.BENCH, dl.DAYS, uni)  # type: ignore[arg-type]
    end = max(int(e["ts_ms"]) for d in bench.values() for e in d["funding"])  # the frozen simulator's own end
    bar_lo = min(b["ts"] for d in bench.values() for b in d["perp"][:1])
    bar_hi = max(b["ts"] for d in bench.values() for b in d["perp"][-1:])
    cand, _nm, meta = dl.load_candidates(dl.DEFAULT_CACHE, bar_lo, bar_hi, exclude=exclude)
    full = {**bench, **cand}
    pe = {s: e["perp_end"] for s, e in vanish_events(full, end, n_cycles=dl.N_MAIN).items()}
    hot = {r["symbol"]: r for r in json.loads((dl.DEFAULT_CACHE / "candidates.json").read_text())["hot"]}
    ids = {n: carry_symbol_ids(n, uni[n][0]) for n in bench}
    ids.update({n: carry_symbol_ids(n, hot[meta[n]["symbol"]]["spot"]) for n in cand})
    rows, cnt = binance_rows()
    index = announcement_index(rows, ids)
    return {"start": start, "end": end, "models": models, "bench": bench, "cand": cand, "full": full, "pe": pe, "ids": ids,
            "rows": rows, "ann_counts": cnt, "index": index}  # fmt: skip


def kwargs(model: CostModel, end: int) -> dict[str, Any]:
    return dict(slots=5, lookback_days=7, entry_apr=0.20, exit_apr=0.0, spot_fee=model.fees["spot_binance"], perp_fee=model.fees["perp_binance"], slippage=model.sim_slippage(dl.LEGS), end_ms=end)  # fmt: skip


def hours(ms: float) -> float:
    return ms / 3_600_000


def stage_gate(cache: Path) -> dict[str, Any]:
    d = load_all()
    pe, index, cand, bench = d["pe"], d["index"], d["cand"], d["bench"]
    rows = d["rows"]
    log(f"announcements: {d['ann_counts']}; first {fmt(rows[0]['ms'])}, last {fmt(rows[-1]['ms'])}")
    log(f"bench {len(bench)}, candidates {len(cand)}, perp ended in window {len(pe)}; symbols with any announcement {len(index)}")
    # sanity: ticker collisions (same token, two carry names) and announcements matched to bench symbols
    by_perp: dict[str, list[str]] = {}
    for n, x in d["ids"].items():
        by_perp.setdefault(x["perp"], []).append(n)
    coll = {k: v for k, v in by_perp.items() if len(v) > 1}
    sym_rows = []
    for n in sorted(set(cand) | set(pe) | set(index)):
        a = first_announcement(index, n)
        e = pe.get(n)
        sym_rows.append({
            "symbol": n, "candidate": n in cand, "perp_end": e, "first_ann": a, "n_ann": len(index.get(n, [])),
            "lead_h": None if (a is None or e is None) else hours(e - a),
        })  # fmt: skip
    ended_cand = [r for r in sym_rows if r["candidate"]]
    with_ann = [r for r in ended_cand if r["lead_h"] is not None and r["lead_h"] >= 0]
    after_end = [r for r in ended_cand if r["lead_h"] is not None and r["lead_h"] < 0]
    bench_ann = [r for r in sym_rows if not r["candidate"] and r["first_ann"] is not None]
    # (iii) events: positions whose perp ended during the holding window (main model, registered perp_last proxy)
    kw = kwargs(d["models"]["main"], d["end"])
    r3 = simulate_carry_delisting(d["full"], perp_end=pe, close_on_vanish=True, n_cycles=dl.N_MAIN, **kw)
    ev = [t for t in r3["trades"] if t.get("perp_ended")]
    ev_rows = []
    for t in ev:
        a = first_announcement(index, t["symbol"], upto=pe[t["symbol"]])
        ev_rows.append({
            "symbol": t["symbol"], "open": t["open_ts"], "close_iii": t["close_ts"], "perp_end": pe[t["symbol"]], "ann": a,
            "ann_before_open": a is not None and a <= t["open_ts"],
            "lead_to_end_h": None if a is None else hours(pe[t["symbol"]] - a),
            "lead_to_close_iii_h": None if a is None else hours(t["close_ts"] - a),
        })  # fmt: skip
    n_ann = sum(1 for r in ev_rows if r["ann"] is not None)  # announced while open (exit) or before the open (no entry)
    leads = sorted(r["lead_to_end_h"] for r in ev_rows if r["ann"] is not None)
    out = {
        "ann_counts": d["ann_counts"], "n_bench": len(bench), "n_candidates": len(cand), "n_perp_ended": len(pe),
        "collisions": coll, "symbols": sym_rows, "n_cand_with_ann_before_end": len(with_ann), "n_cand_ann_after_end": len(after_end),
        "bench_with_ann": [(r["symbol"], r["first_ann"], r["perp_end"]) for r in bench_ann],
        "events_iii": ev_rows, "n_events_iii": len(ev_rows), "n_events_with_ann": n_ann, "gate_min": GATE_MIN, "passed": n_ann >= GATE_MIN,
    }  # fmt: skip
    (cache / "gate.json").write_text(json.dumps(out, indent=1, default=str))
    print("\n== gate ==")
    print(f"ticker collisions across carry names: {coll or 'none'}")
    print(
        f"candidates {len(cand)}: announcement before perp end {len(with_ann)}, only after perp end {len(after_end)}, none {len(cand) - len(with_ann) - len(after_end)}"
    )
    print(
        f"bench symbols with an announcement: {len(bench_ann)}: "
        + ", ".join(f"{s.split('/')[0]} {fmt(a)[:10]} end {fmt(e)[:10]}" for s, a, e in bench_ann)
    )
    ld = sorted(r["lead_h"] / 24 for r in with_ann)
    if ld:
        q = statistics.quantiles(ld, n=4) if len(ld) > 1 else [ld[0]] * 3
        print(
            f"lead announcement -> perp end, days (candidates): n {len(ld)}, min {ld[0]:.1f}, q25 {q[0]:.1f}, median {q[1]:.1f}, q75 {q[2]:.1f}, max {ld[-1]:.1f}"
        )
    print(
        f"(iii) events: {len(ev_rows)}; with an announcement by the perp end: {n_ann} (of them before the open: {sum(r['ann_before_open'] for r in ev_rows)})"
    )
    print("| event | open | perp end | announcement | lead to end, d | lead to (iii) close, d |")
    for r in ev_rows:
        le = "—" if r["lead_to_end_h"] is None else f"{r['lead_to_end_h'] / 24:.1f}"
        lc = "—" if r["lead_to_close_iii_h"] is None else f"{r['lead_to_close_iii_h'] / 24:.1f}"
        print(f"| {r['symbol'].split('/')[0]} | {fmt(r['open'])} | {fmt(r['perp_end'])} | {fmt(r['ann'])} | {le} | {lc} |")
    print(f"gate: {n_ann} events with an announcement vs minimum {GATE_MIN} -> {'PASS' if out['passed'] else 'FAIL (◯, no P&L run)'}")
    if leads:
        print(f"leads to perp end, h: {[round(x) for x in leads]}")
    return out


def diff(eq_a: list[tuple[int, float]], eq_b: list[tuple[int, float]], years: float) -> dict[str, Any]:
    dc = diff_curve(eq_a, eq_b)
    rk = risk_summary(dc, years, CAPITAL, n_boot=N_BOOT)
    bb = block_bootstrap([v for _, v in weekly_changes(dc)], n_boot=N_BOOT)
    out = {"apr_on_capital": rk["apr_on_capital"], "ci": rk["apr_on_capital_ci"], "p": bb["p"]}
    for k, lo, hi in (("is", 0, dl.SPLIT_MS), ("oos", dl.SPLIT_MS, None)):
        b = block_bootstrap([v for _, v in window_weekly(dc, lo, hi)], n_boot=N_BOOT)
        out[k] = None if b["apr"] is None else b["apr"] / CAPITAL
    return out


def vstats(res: dict[str, Any], years: float) -> dict[str, Any]:
    rk = risk_summary(res["equity"], years, CAPITAL, n_boot=N_BOOT)
    rc = res.get("rule_closes", [])
    return {**rk, "n_trades": len(res["trades"]), "n_perp_ended": sum(1 for t in res["trades"] if t.get("perp_ended")),
            "n_ann_closes": sum(1 for x in rc if x.get("rule") == "announcement"), "n_vanish_closes": sum(1 for x in rc if x.get("rule") != "announcement")}  # fmt: skip


def stage_run(cache: Path, out_name: str, *, exclude: frozenset[str] = frozenset(), placebo: bool = True, explore: bool = False) -> None:
    gate = json.loads((cache / "gate.json").read_text()) if (cache / "gate.json").exists() else stage_gate(cache)
    if not gate["passed"]:
        print(f"gate failed: {gate['n_events_with_ann']} events with an announcement < {GATE_MIN}; no P&L run (◯)")
        return
    d = load_all(exclude)
    log(
        f"loaded: bench {len(d['bench'])}, candidates {len(d['cand'])} (excluded {sorted(exclude) or '-'}), placebo {placebo}, explore {explore}"
    )
    pe, index = d["pe"], d["index"]
    exit_now, entry_ok = announcement_rule(index)
    out: dict[str, Any] = {"gate": {k: gate[k] for k in ("n_events_iii", "n_events_with_ann")}, "models": {}}
    for mname, model in d["models"].items():
        kw = kwargs(model, d["end"])
        for st in SETTLES:
            key = f"{mname}|{st}"
            runs = {
                "ii": simulate_carry_delisting(d["full"], perp_end=pe, settle=st, **kw),
                "iii": simulate_carry_delisting(d["full"], perp_end=pe, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, **kw),
                "iv": simulate_carry_delisting(
                    d["full"],
                    perp_end=pe,
                    settle=st,
                    close_on_vanish=True,
                    n_cycles=dl.N_MAIN,
                    exit_now=exit_now,
                    entry_allowed=entry_ok,
                    **kw,
                ),
            }
            if explore:  # exploratory (added after the first run): which half of (iv) carries the difference
                v3 = dict(perp_end=pe, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, **kw)
                runs["iv_entry_only"] = simulate_carry_delisting(d["full"], entry_allowed=entry_ok, **v3)
                runs["iv_exit_only"] = simulate_carry_delisting(d["full"], exit_now=exit_now, **v3)
            years = runs["ii"]["stats"]["years"]
            mo: dict[str, Any] = {"variants": {k: vstats(r, years) for k, r in runs.items()}}
            mo["diffs"] = {"iv_minus_iii": diff(runs["iv"]["equity"], runs["iii"]["equity"], years),
                           "iv_minus_ii": diff(runs["iv"]["equity"], runs["ii"]["equity"], years)}  # fmt: skip
            for k in ("iv_entry_only", "iv_exit_only"):
                if k in runs:
                    mo["diffs"][f"{k}_minus_iii [exploratory]"] = diff(runs[k]["equity"], runs["iii"]["equity"], years)
            ev_keys = [(t["symbol"], t["open_ts"]) for t in runs["iii"]["trades"] if t.get("perp_ended")]
            pairs = paired_event_diffs(runs["iv"]["trades"], runs["iii"]["trades"], ev_keys)
            for p in pairs:
                p["announced"] = first_announcement(index, p["symbol"], upto=pe[p["symbol"]]) is not None
            mo["events"] = pairs
            if exclude and "excluded" not in out["gate"]:
                # the cached gate.json counts the full universe; with --exclude, count the events of this run instead
                out["gate"] = {"n_events_iii": len(ev_keys), "n_events_with_ann": sum(1 for p in pairs if p["announced"]),
                               "excluded": sorted(exclude), "counted_on": key}  # fmt: skip
            mo["ann_closes"] = [
                {"symbol": x["symbol"], "t": x["t"], "open_ts": x["open_ts"]}
                for x in runs["iv"]["rule_closes"]
                if x.get("rule") == "announcement"
            ]
            # trades changed by (iv) beyond the events (bench exits on spot delistings, blocked re-entries, path changes)
            k3 = {(t["symbol"], t["open_ts"]): t for t in runs["iii"]["trades"]}
            k4 = {(t["symbol"], t["open_ts"]): t for t in runs["iv"]["trades"]}
            mo["changed"] = {"only_iii": [(k[0], k[1], k3[k]["pnl"]) for k in k3 if k not in k4],
                             "only_iv": [(k[0], k[1], k4[k]["pnl"]) for k in k4 if k not in k3],
                             "diff_pnl": [(k[0], k[1], k4[k]["pnl"] - k3[k]["pnl"]) for k in k3 if k in k4 and abs(k4[k]["pnl"] - k3[k]["pnl"]) > 1e-12]}  # fmt: skip
            if mname == "main" and placebo:
                leads = [
                    int(r["lead_to_close_iii_h"] * 3_600_000)
                    for r in gate["events_iii"]
                    if r["ann"] is not None and not r["ann_before_open"] and r["symbol"] in pe  # drops --exclude'd symbols
                ]
                pool = [t for t in runs["iii"]["trades"] if not t.get("perp_ended") and t["symbol"] not in index]
                k = out["gate"]["n_events_with_ann"]
                rng = random.Random(PLACEBO_SEED)
                pl = []
                for i in range(N_PLACEBO):
                    win = placebo_windows(pool, leads, k, rng)
                    ex, ok = window_rule(win)
                    rp = simulate_carry_delisting(
                        d["full"], perp_end=pe, settle=st, close_on_vanish=True, n_cycles=dl.N_MAIN, exit_now=ex, entry_allowed=ok, **kw
                    )
                    dc = diff_curve(rp["equity"], runs["iii"]["equity"])
                    pl.append(
                        {"apr_on_capital": dc[-1][1] / years / CAPITAL if dc else 0.0, "n_windows": sum(len(v) for v in win.values())}
                    )
                    if i % 10 == 9:
                        log(f"{key} placebo {i + 1}/{N_PLACEBO}")
                mo["placebo"] = pl
            out["models"][key] = mo
            log(f"{key}: done")
            del runs
            gc.collect()
    (cache / out_name).write_text(json.dumps(out, indent=1, default=str))
    report(out)


def report(out: dict[str, Any]) -> None:
    g = out["gate"]
    scope = f" (this run, excluded {', '.join(g['excluded'])}; counted on {g['counted_on']})" if g.get("excluded") else ""
    print(
        f"\ngate: (iii) events {g['n_events_iii']}, with an announcement {g['n_events_with_ann']}{scope}; returns on capital = notional / {CAPITAL}"
    )
    for key, mo in out["models"].items():
        print(f"\n== {key} ==")
        print(
            "| variant | trades | perp ended in trade | ann. closes | vanish closes | APR capital | 95% CI | max DD cap | worst week cap |"
        )
        for k, v in mo["variants"].items():
            ci = v["apr_on_capital_ci"]
            print(f"| {k} | {v['n_trades']} | {v['n_perp_ended']} | {v['n_ann_closes']} | {v['n_vanish_closes']} | {pct(v['apr_on_capital'], 2)} | "
                  f"[{pct(ci[0], 2)}; {pct(ci[1], 2)}] | {pct(v['max_dd_on_capital'], 2)} | {pct(v['worst_week_on_capital'], 2)} ({fmt(v['worst_week_start_ms'])[:10]}) |")  # fmt: skip
        for k, v in mo["diffs"].items():
            ci = v["ci"]
            print(
                f"  {k}: {pct(v['apr_on_capital'], 2)} [{pct(ci[0], 2)}; {pct(ci[1], 2)}] p(one-sided) {v['p']:.3f} = p_holm (m=1); IS {pct(v['is'], 2)} OOS {pct(v['oos'], 2)}"
            )
        ev = mo["events"]
        ds = [e["diff"] for e in ev if e["announced"]]
        pos = sum(1 for x in ds if x > 0)
        print(
            f"  per-event (iv)-(iii), announced events n {len(ds)}: positive {pos}, negative {sum(1 for x in ds if x < 0)}, zero {len(ds) - pos - sum(1 for x in ds if x < 0)}"
        )
        if len(ds) > 1:
            m, sd = statistics.mean(ds), statistics.stdev(ds)
            print(
                f"  mean per event (trade P&L, fraction of slot notional) {pct(m, 2)}, sd {pct(sd, 2)}, MDE(2.8 sd/sqrt n) {pct(2.8 * sd / len(ds) ** 0.5, 2)}"
            )
        for e in sorted(ev, key=lambda e: e["diff"]):
            print(
                f"    {e['symbol'].split('/')[0]:<10} {fmt(e['open_ts'])[:10]} ann {'y' if e['announced'] else 'n'} iii {pct(e['pnl_b'], 1)} iv {pct(e['pnl_a'], 1)} diff {pct(e['diff'], 1)}"
            )
        ch = mo["changed"]
        print(
            f"  other changes: only in (iii) {len(ch['only_iii'])}, only in (iv) {len(ch['only_iv'])}, same trade different P&L {len(ch['diff_pnl'])}"
        )
        evk = {(e["symbol"], e["open_ts"]) for e in ev}
        ne3 = [x for x in ch["only_iii"] if (x[0], x[1]) not in evk]
        print(
            f"    non-event trades only in (iii): {len(ne3)}, sum P&L {pct(sum(x[2] for x in ne3), 1)}: {', '.join(x[0].split('/')[0] for x in ne3)}"
        )
        print(
            f"    only in (iv): sum P&L {pct(sum(x[2] for x in ch['only_iv']), 1)}: {', '.join(x[0].split('/')[0] for x in ch['only_iv'])}"
        )
        print(f"    same trade, P&L (iv)-(iii): {', '.join(f'{x[0].split(chr(47))[0]} {pct(x[2], 1)}' for x in ch['diff_pnl'])}")
        print(
            f"  announcement closes: {', '.join(f'{x[0].split(chr(47))[0]} {fmt(x[1])}' for x in ((c['symbol'], c['t']) for c in mo['ann_closes']))}"
        )
        if "placebo" in mo:
            pv = sorted(x["apr_on_capital"] for x in mo["placebo"])
            act = mo["diffs"]["iv_minus_iii"]["apr_on_capital"]
            rank = sum(1 for x in pv if x >= act) / len(pv)
            print(f"  placebo of the exit only, not of the entry ban (information-free early exits, {len(pv)} draws, mean windows {statistics.mean(x['n_windows'] for x in mo['placebo']):.1f}): "
                  f"median {pct(statistics.median(pv), 2)}, q05 {pct(pv[int(0.05 * len(pv))], 2)}, q95 {pct(pv[int(0.95 * len(pv)) - 1], 2)}; share >= actual {rank:.2f}")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["gate", "run", "report"])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--out", default="results.json", help="run/report: results file name in the cache dir")
    ap.add_argument("--exclude", default="", help="run (exploratory): comma-separated candidate perp symbols to drop, e.g. A2ZUSDT")
    ap.add_argument("--no-placebo", action="store_true", help="run: skip the placebo draws")
    ap.add_argument("--explore", action="store_true", help="run (exploratory): also entry-block-only and exit-only variants")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "gate":
        stage_gate(cache)
    elif a.stage == "run":
        ex = frozenset(s for s in a.exclude.split(",") if s)
        stage_run(cache, a.out, exclude=ex, placebo=not a.no_placebo, explore=a.explore)
    else:
        report(json.loads((cache / a.out).read_text()))


if __name__ == "__main__":
    main()
