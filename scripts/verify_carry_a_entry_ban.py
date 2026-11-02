"""Item 8o of IDEA-20261031-structural-edges-depth: carry A entry ban after a delisting announcement, on the unseen
period 2022-01-01 ... 2024-10-16 (before the window of items 8b / 8g / 8n).

Stages (in order):
  universe  USDT-M perps of the Binance Vision listing (funding_crowding cache, with delisted symbols) that have a
            Binance USDT spot pair and were "hot" (trailing 7d APR >= 0.20) in the period -> candidates.json
  fetch     4h perp and spot klines of the hot symbols (rt.binance_vision; verify_carry_a_delisting.stage_fetch),
            one JSON file per month under --cache-dir/vision_v/
  bodies    texts of 2022 "needs_body" delisting announcements (verify_news_events.stage_bodies from 2022-01-01)
  gate      counts only (no P&L): (iii) events, events whose announcement precedes the entry, removed trades, lead
            distribution -> gate.json
  run       only if the gate passes: (iii) and (v) = (iii) + entry ban, cost models 'main'/'stress', settlement
            proxies 'perp_last'/'spot'; per-year diffs, per-event pairs, placebo of the entry ban -> --out
  bybit     descriptive Bybit inventory 2024-10-17 ... 2026-10-17 (no P&L)

The frozen engines are reused unchanged (carry_delisting.simulate_carry_delisting, carry_announcement rules).
Returns on capital (A: capital = 1.5 x notional). Pre-registration: the card, section "Пункт 8o".
Announcement titles and bodies are untrusted data: only tickers are parsed from them.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gc
import json
import random
import re
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_carry_a_announcement_exit as an  # noqa: E402
import verify_carry_a_delisting as dl  # noqa: E402
import verify_carry_alts as alts  # noqa: E402
import verify_news_events as nev  # noqa: E402

from tradedesk.analytics.carry_announcement import (  # noqa: E402
    announcement_index,
    announcement_rule,
    carry_symbol_ids,
    first_announcement,
    paired_event_diffs,
    placebo_ban_windows,
    window_rule,
)
from tradedesk.analytics.carry_costs import CostModel, risk_summary, weekly_changes  # noqa: E402
from tradedesk.analytics.carry_delisting import simulate_carry_delisting, vanish_events  # noqa: E402
from tradedesk.analytics.carry_modules import block_bootstrap, diff_curve, window_weekly  # noqa: E402
from tradedesk.analytics.news_events import classify_delisting  # noqa: E402


def ms(y: int, m: int, d: int) -> int:
    return int(dt.datetime(y, m, d, tzinfo=dt.UTC).timestamp() * 1000)


HOME = Path.home() / ".cache" / "tradedesk-agents"
DEFAULT_CACHE = HOME / "carry_a_entry_ban"
NEWS = HOME / "news_events"
START = ms(2022, 1, 1)
END = ms(2024, 10, 17) - 1
YEARS = ((2022, ms(2022, 1, 1), ms(2023, 1, 1)), (2023, ms(2023, 1, 1), ms(2024, 1, 1)), (2024, ms(2024, 1, 1), None))
NO_ANN_BEFORE = ms(2022, 2, 24)  # the CMS catalog starts 2022-02-17 (+ ~1 week lead)
STALE_MS = 90 * 86_400_000
GATE_MIN = 10
SD_8N = {"perp_last": 0.39, "spot": 0.14}  # per-event sd from the 8n MDE (30 % / 10.6 % at n = 13)
SETTLES = ("perp_last", "spot")
N_PLACEBO = 100
PLACEBO_SEED = 20261102
N_BOOT = 10_000
CAPITAL = dl.CAPITAL

log, fmt, pct = dl.log, dl.fmt, dl.pct


# ---------------------------------------------------------------- stage: universe


def stage_universe(cache: Path) -> None:
    spots = set(json.loads((dl.DEFAULT_CACHE / "spot_symbols.json").read_text()))
    info = json.loads((dl.CROWD / "exchange_info.json").read_text())["info"]
    syms = json.loads((dl.CROWD / "symbols.json").read_text())
    out: dict[str, Any] = {"window": [START, END], "hot": [], "not_hot": [], "no_spot": [], "no_funding_in_window": 0,
                           "not_measured": [], "excluded": []}  # fmt: skip
    for s in syms:
        if not s.endswith("USDT"):
            continue
        meta = info.get(s) or {}
        if meta.get("underlyingType") not in (None, "COIN") or str(meta.get("contractType", "PERPETUAL")) != "PERPETUAL":
            out["excluded"].append({"symbol": s, "why": f"{meta.get('underlyingType')}/{meta.get('contractType')}"})
            continue
        base = s[:-4]
        mult, spot_base = 1, base
        if (g := alts.MULT.match(base)) is not None:
            mult, spot_base = int(g.group(1)), g.group(2)
        if spot_base in alts.STABLES:
            continue
        rows = dl.archive_funding(s)
        fw = [{"ts_ms": int(r[0]), "rate": float(r[2])} for r in rows if START <= r[0] <= END]
        if not fw:
            out["no_funding_in_window"] += 1
            continue
        if f"{spot_base}USDT" not in spots:
            out["no_spot"].append(s)
            continue
        rec = {"symbol": s, "spot": f"{spot_base}USDT", "mult": mult, "first_ms": fw[0]["ts_ms"], "last_ms": int(rows[-1][0])}
        if alts.max_trailing_apr(fw) >= 0.20:
            out["hot"].append(rec)
        else:
            out["not_hot"].append(s)
    (cache / "candidates.json").write_text(json.dumps(out, indent=1))
    log(f"universe: hot {len(out['hot'])}, not hot {len(out['not_hot'])}, no spot pair {len(out['no_spot'])}, "
        f"no funding in window {out['no_funding_in_window']}, excluded {len(out['excluded'])}")  # fmt: skip
    print(f"jobs (month files): {len(dl.fetch_jobs(cache))}")


# ---------------------------------------------------------------- loading


_SUPP = (
    re.compile(r"^Binance Futures Will Delist (.+?) USDT-Margined Contracts?$"),  # "1000BTTC and YFII USDT-Margined Contracts"
    re.compile(r"^Binance Futures Will Delist USDT-Margined (.+?) Perpetual Contracts?$"),  # "USDT-Margined BLZ Perpetual Contract"
    re.compile(r"^Binance Will Delist ([A-Z0-9]{2,15}) Margin Pairs, USD.-M Futures Contracts"),  # FTT, 2022-11-13
)


def supplement_2022_rows() -> list[dict[str, Any]]:
    """Binance perp-delisting titles of 2022 that the shared parser (`classify_delisting`) reads as 'ignore' because they
    name the base token, not ``XYZUSDT`` (critic B of 8o, finding 1). Local to this script so that the 8n index stays
    byte-identical; tickers ending in BUSD are not USDT perps and are skipped. Titles are untrusted text."""
    out = []
    for r in nev.load_ann(NEWS):
        if r["source"] != "binance" or r["catalog"] != "delisting" or classify_delisting("binance", r["title"])[0] != "ignore":
            continue
        t = " ".join(r["title"].split())
        for rx in _SUPP:
            if (m := rx.match(t)) is not None:
                ticks = [
                    f"{x}USDT"
                    for x in re.split(r"\s*(?:,|\band\b)\s*", m.group(1))
                    if re.fullmatch(r"[A-Z0-9]{2,15}", x) and not x.endswith("BUSD")
                ]
                if ticks:
                    out.append({"ms": r["ms"], "kind": "perp_delist", "tickers": ticks, "title": t[:120]})
                break
    return sorted(out, key=lambda x: x["ms"])


def load_all(cache: Path, exclude: frozenset[str] = frozenset(), supplement: bool = False) -> dict[str, Any]:
    cal = json.loads(dl.CALIB.read_text())
    models = {k: CostModel(cal["models"][k]["fees"], cal["models"][k]["slippage"], cal["models"][k]["timing"]) for k in ("main", "stress")}
    full, not_measured, meta = dl.load_candidates(cache, START - 8 * 86_400_000, END, exclude=exclude)
    end = max(int(e["ts_ms"]) for d in full.values() for e in d["funding"])
    hot = {r["symbol"]: r for r in json.loads((cache / "candidates.json").read_text())["hot"]}
    ids = {n: carry_symbol_ids(n, hot[meta[n]["symbol"]]["spot"]) for n in full}
    rows, cnt = an.binance_rows()
    if supplement:
        supp = supplement_2022_rows()
        for r in supp:
            log(f"supplement {fmt(r['ms'])} {r['tickers']} | {r['title']}")
        rows = sorted([*rows, *supp], key=lambda x: x["ms"])
        cnt = {**cnt, "perp_delist": cnt["perp_delist"] + len(supp), "ignored": cnt["ignored"] - len(supp), "supplement_2022": len(supp)}
    index = announcement_index(rows, ids)
    pe = {s: e["perp_end"] for s, e in vanish_events(full, end, n_cycles=dl.N_MAIN).items()}
    log(f"loaded {len(full)} symbols with data, not measured {len(not_measured)}, end {fmt(end)}, perp ended {len(pe)}, "
        f"announced symbols {len(index)}; excluded {sorted(exclude) or '-'}")  # fmt: skip
    return {"models": models, "full": full, "not_measured": not_measured, "meta": meta, "end": end, "ids": ids,
            "rows": rows, "ann_counts": cnt, "index": index, "pe": pe}  # fmt: skip


def sim(d: dict[str, Any], model: CostModel, settle: str, entry_allowed: Any = None) -> dict[str, Any]:
    return simulate_carry_delisting(d["full"], perp_end=d["pe"], settle=settle, close_on_vanish=True, n_cycles=dl.N_MAIN,
                                    entry_allowed=entry_allowed, **an.kwargs(model, d["end"]))  # fmt: skip


def keyset(res: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    return {(t["symbol"], t["open_ts"]): t for t in res["trades"]}


# ---------------------------------------------------------------- stage: gate


def stage_gate(cache: Path, supplement: bool = False) -> dict[str, Any]:
    d = load_all(cache, supplement=supplement)
    pe, index = d["pe"], d["index"]
    rows = d["rows"]
    log(f"announcements: {d['ann_counts']}; first {fmt(rows[0]['ms'])}, last {fmt(rows[-1]['ms'])}")
    by_perp: dict[str, list[str]] = {}
    for n, x in d["ids"].items():
        by_perp.setdefault(x["perp"], []).append(n)
    coll = {k: v for k, v in by_perp.items() if len(v) > 1}
    _, ok = announcement_rule(index)
    main = d["models"]["main"]
    r3, rv = sim(d, main, "perp_last"), sim(d, main, "perp_last", ok)
    k3, kv = keyset(r3), keyset(rv)
    ev = []
    for t in r3["trades"]:
        if not t.get("perp_ended"):
            continue
        a = first_announcement(index, t["symbol"])
        a_end = a if (a is not None and a <= pe[t["symbol"]]) else None
        ev.append({"symbol": t["symbol"], "open": t["open_ts"], "close_iii": t["close_ts"], "perp_end": pe[t["symbol"]], "ann": a_end,
                   "banned": a is not None and a <= t["open_ts"], "in_v": (t["symbol"], t["open_ts"]) in kv,
                   "lead_to_end_d": None if a_end is None else (pe[t["symbol"]] - a_end) / 86_400_000,
                   "early": pe[t["symbol"]] < NO_ANN_BEFORE})  # fmt: skip
    removed = [k for k in k3 if k not in kv]
    added = [k for k in kv if k not in k3]
    stale = [k for k in removed if (a := first_announcement(index, k[0])) is not None and k[1] - a > STALE_MS]
    n_ban = sum(1 for r in ev if r["banned"])
    out = {"ann_counts": d["ann_counts"], "n_symbols": len(d["full"]), "n_not_measured": len(d["not_measured"]),
           "not_measured": d["not_measured"], "n_perp_ended": len(pe), "n_announced_symbols": len(index), "collisions": coll,
           "n_trades_iii": len(k3), "n_trades_v": len(kv), "removed": [list(k) for k in removed], "added": [list(k) for k in added],
           "stale_removed": [list(k) for k in stale], "events_iii": ev, "n_events_iii": len(ev), "n_ban": n_ban,
           "gate_min": GATE_MIN, "passed": n_ban >= GATE_MIN}  # fmt: skip
    (cache / ("gate_supplemented.json" if supplement else "gate.json")).write_text(json.dumps(out, indent=1, default=str))
    print(f"\n== gate (counts only, no P&L){' + 2022 title supplement' if supplement else ''} ==")
    print(f"symbols with data {len(d['full'])}, not measured {len(d['not_measured'])}: "
          + "; ".join(f"{x['symbol']} ({x['why'][:40]})" for x in d["not_measured"][:15]))  # fmt: skip
    print(f"ticker collisions across carry names: {coll or 'none'}")
    print(f"perp ended in period {len(pe)}; symbols with an announcement {len(index)}")
    print(
        f"(iii) trades {len(k3)}, (v) trades {len(kv)}: removed {len(removed)} (stale > 90 d after the announcement {len(stale)}), added {len(added)}"
    )
    na = [r for r in ev if r["ann"] is not None]
    print(f"(iii) events {len(ev)}: announced by perp end {len(na)}, of them before the entry (ban removes it) {n_ban}, "
          f"during holding {len(na) - n_ban}; no announcement {len(ev) - len(na)} (perp end before 2022-02-24: {sum(r['early'] for r in ev)})")  # fmt: skip
    ld = sorted(r["lead_to_end_d"] for r in na)
    if len(ld) > 1:
        q = statistics.quantiles(ld, n=4)
        print(
            f"lead announcement -> perp end, days: n {len(ld)}, min {ld[0]:.1f}, q25 {q[0]:.1f}, median {q[1]:.1f}, q75 {q[2]:.1f}, max {ld[-1]:.1f}"
        )
    print("| event | open | perp end | announcement | lead, d | banned | still in (v) |")
    for r in sorted(ev, key=lambda r: r["perp_end"]):
        le = "—" if r["lead_to_end_d"] is None else f"{r['lead_to_end_d']:.1f}"
        print(
            f"| {r['symbol'].split('/')[0]} | {fmt(r['open'])} | {fmt(r['perp_end'])} | {fmt(r['ann'])} | {le} | {'y' if r['banned'] else 'n'} | {'y' if r['in_v'] else 'n'} |"
        )
    for k in sorted(removed, key=lambda k: k[1]):
        a = first_announcement(index, k[0])
        t = k3[k]
        print(f"removed: {k[0].split('/')[0]} open {fmt(k[1])} close {fmt(t['close_ts'])} first ann {fmt(a)}"
              f"{' STALE' if k in stale else ''} perp_ended {bool(t.get('perp_ended'))}")  # fmt: skip
    if out["passed"]:
        print(f"gate: {n_ban} events removed by the ban >= {GATE_MIN} -> PASS")
    else:
        mde = {s: 2.8 * sd / max(n_ban, 1) ** 0.5 for s, sd in SD_8N.items()}
        print(
            f"gate: {n_ban} events removed by the ban < {GATE_MIN} -> FAIL (◯, no P&L); MDE per event perp_last {pct(mde['perp_last'])}, spot {pct(mde['spot'])}"
        )
    return out


# ---------------------------------------------------------------- stage: run


def diff(eq_a: list[tuple[int, float]], eq_b: list[tuple[int, float]], years: float) -> dict[str, Any]:
    dc = diff_curve(eq_a, eq_b)
    rk = risk_summary(dc, years, CAPITAL, n_boot=N_BOOT)
    bb = block_bootstrap([v for _, v in weekly_changes(dc)], n_boot=N_BOOT)
    out = {"apr_on_capital": rk["apr_on_capital"], "ci": rk["apr_on_capital_ci"], "p": bb["p"], "final": dc[-1][1] if dc else 0.0}
    for y, lo, hi in YEARS:
        b = block_bootstrap([v for _, v in window_weekly(dc, lo, hi)], n_boot=N_BOOT)
        out[str(y)] = {"weeks": b["weeks"], "apr": None if b["apr"] is None else b["apr"] / CAPITAL,
                       "ci": None if b["apr"] is None else [x / CAPITAL for x in b["ci"]]}  # fmt: skip
    return out


def stage_run(cache: Path, out_name: str, *, exclude: frozenset[str] = frozenset(), placebo: bool = True) -> None:
    gate = json.loads((cache / "gate.json").read_text()) if (cache / "gate.json").exists() else stage_gate(cache)
    if not gate["passed"]:
        print(f"gate failed: {gate['n_ban']} < {GATE_MIN}; no P&L run (◯)")
        return
    d = load_all(cache, exclude)
    pe, index = d["pe"], d["index"]
    _, ok = announcement_rule(index)
    out: dict[str, Any] = {"excluded": sorted(exclude), "models": {}}
    for mname, model in d["models"].items():
        for st in SETTLES:
            key = f"{mname}|{st}"
            r3, rv = sim(d, model, st), sim(d, model, st, ok)
            years = r3["stats"]["years"]
            mo: dict[str, Any] = {"years": years}
            mo["variants"] = {k: {**risk_summary(r["equity"], years, CAPITAL, n_boot=N_BOOT), "n_trades": len(r["trades"]),
                                  "n_perp_ended": sum(1 for t in r["trades"] if t.get("perp_ended"))} for k, r in (("iii", r3), ("v", rv))}  # fmt: skip
            mo["v_minus_iii"] = diff(rv["equity"], r3["equity"], years)
            ev_keys = [(t["symbol"], t["open_ts"]) for t in r3["trades"] if t.get("perp_ended")]
            pairs = paired_event_diffs(rv["trades"], r3["trades"], ev_keys)
            for p in pairs:
                a = first_announcement(index, p["symbol"])
                p["banned"] = a is not None and a <= p["open_ts"]
            mo["events"] = pairs
            k3, kv = keyset(r3), keyset(rv)
            mo["removed"] = [(k[0], k[1], k3[k]["pnl"], k3[k].get("perp_ended", False)) for k in k3 if k not in kv]
            mo["added"] = [(k[0], k[1], kv[k]["pnl"]) for k in kv if k not in k3]
            if mname == "main" and placebo:
                end = d["end"]
                leads = [min(pe.get(s, end), end) - ts[0] for s, ts in index.items() if s in d["full"] and min(pe.get(s, end), end) > ts[0]]
                traded = {t["symbol"] for t in r3["trades"]}
                spans = {s: (d["full"][s]["funding"][0]["ts_ms"], min(pe.get(s, end), end)) for s in traded if s not in index}
                rng = random.Random(PLACEBO_SEED)
                pl = []
                for i in range(N_PLACEBO):
                    win = placebo_ban_windows(spans, leads, len(leads), rng)
                    rp = sim(d, model, st, window_rule(win)[1])
                    dc = diff_curve(rp["equity"], r3["equity"])
                    kp = keyset(rp)
                    pl.append(
                        {"apr_on_capital": dc[-1][1] / years / CAPITAL if dc else 0.0, "n_removed": sum(1 for k in k3 if k not in kp)}
                    )
                    if i % 20 == 19:
                        log(f"{key} placebo {i + 1}/{N_PLACEBO}")
                mo["placebo"] = {"k": len(leads), "pool": len(spans), "draws": pl}
            out["models"][key] = mo
            log(f"{key}: done")
            del r3, rv
            gc.collect()
    (cache / out_name).write_text(json.dumps(out, indent=1, default=str))
    report(out)


def report(out: dict[str, Any]) -> None:
    print(f"\nreturns on capital = notional / {CAPITAL}; excluded: {', '.join(out['excluded']) or '-'}")
    for key, mo in out["models"].items():
        print(f"\n== {key} ==")
        print("| variant | trades | perp ended in trade | APR capital | 95% CI | max DD cap | worst week cap |")
        for k, v in mo["variants"].items():
            ci = v["apr_on_capital_ci"]
            print(f"| {k} | {v['n_trades']} | {v['n_perp_ended']} | {pct(v['apr_on_capital'], 2)} | [{pct(ci[0], 2)}; {pct(ci[1], 2)}] | "
                  f"{pct(v['max_dd_on_capital'], 2)} | {pct(v['worst_week_on_capital'], 2)} ({fmt(v['worst_week_start_ms'])[:10]}) |")  # fmt: skip
        x = mo["v_minus_iii"]
        ci = x["ci"]
        print(f"  (v)-(iii): {pct(x['apr_on_capital'], 2)} [{pct(ci[0], 2)}; {pct(ci[1], 2)}] p(one-sided) {x['p']:.3f} = p_holm (m=1)")
        print(
            "  per year: "
            + "; ".join(
                f"{y} {pct(x[str(y)]['apr'], 2)} [{pct((x[str(y)]['ci'] or [None])[0], 2)}; {pct((x[str(y)]['ci'] or [None, None])[1], 2)}] ({x[str(y)]['weeks']} w)"
                for y, _, _ in YEARS
            )
        )
        print(f"  trade nothing - (iii): {pct(-mo['variants']['iii']['apr_on_capital'], 2)}")
        ev = [e for e in mo["events"] if e["banned"]]
        ds = [e["diff"] for e in ev]
        print(
            f"  per-event (v)-(iii), banned events n {len(ds)}: positive {sum(1 for v in ds if v > 0)}, negative {sum(1 for v in ds if v < 0)}, zero {sum(1 for v in ds if v == 0)}"
        )
        if len(ds) > 1:
            sd = statistics.stdev(ds)
            print(
                f"  mean {pct(statistics.mean(ds), 2)} of slot notional, sd {pct(sd, 2)}, MDE(2.8 sd/sqrt n) {pct(2.8 * sd / len(ds) ** 0.5, 2)}"
            )
        for e in sorted(ev, key=lambda e: -e["diff"]):
            print(
                f"    {e['symbol'].split('/')[0]:<10} {fmt(e['open_ts'])[:10]} iii {pct(e['pnl_b'], 1)} v {pct(e['pnl_a'], 1)} diff {pct(e['diff'], 1)}"
            )
        ne = [r for r in mo["removed"] if not r[3]]
        print(
            f"  removed trades {len(mo['removed'])} (non-event {len(ne)}, sum P&L {pct(sum(r[2] for r in ne), 1)}); added {len(mo['added'])} (sum P&L {pct(sum(r[2] for r in mo['added']), 1)})"
        )
        if ds:
            top = max(ev, key=lambda e: e["diff"] if x["apr_on_capital"] >= 0 else -e["diff"])
            print(
                f"  top contributor: {top['symbol']} diff {pct(top['diff'], 1)} -> rerun with --exclude {top['symbol'].split('/')[0]}USDT"
            )
        if "placebo" in mo:
            pv = sorted(p["apr_on_capital"] for p in mo["placebo"]["draws"])
            act = x["final"] / mo["years"] / CAPITAL
            share = sum(1 for v in pv if v >= act) / len(pv)
            print(f"  placebo of the entry ban (k {mo['placebo']['k']} symbols from a pool of {mo['placebo']['pool']}, {len(pv)} draws): median {pct(statistics.median(pv), 2)}, "
                  f"q05 {pct(pv[int(0.05 * len(pv))], 2)}, q95 {pct(pv[int(0.95 * len(pv)) - 1], 2)}; actual {pct(act, 2)}; share >= actual {share:.2f} "
                  f"({'PASS' if share <= 0.05 else 'fail'}); removed trades median {statistics.median(p['n_removed'] for p in mo['placebo']['draws'])}")  # fmt: skip


# ---------------------------------------------------------------- stage: bybit (descriptive)


async def stage_bybit(cache: Path) -> None:
    from tradedesk.core.http import Http

    lo, hi = ms(2024, 10, 17), ms(2026, 10, 18)
    rows = [r for r in nev.load_ann(NEWS) if r["source"] == "bybit" and lo <= r["ms"] < hi]
    kinds: Counter[str] = Counter()
    perps: dict[str, int] = {}
    body_missing = 0
    for r in rows:
        kind, ticks = classify_delisting("bybit", r["title"])
        if kind == "needs_body":
            p = nev.body_path(NEWS, r)
            if not p.exists():
                body_missing += 1
                kinds["needs_body_missing"] += 1
                continue
            b = json.loads(p.read_text())
            kind, ticks = ("perp_delist", b["perps"]) if "Perpetual" in r["title"] else ("spot_delist", b["pairs"])
        kinds[kind] += 1
        if kind == "perp_delist":
            for t in ticks:
                if str(t).endswith("USDT"):
                    perps.setdefault(str(t), r["ms"])
    print(f"bybit delisting announcements 2024-10-17 … 2026-10-17: {len(rows)}; kinds {dict(kinds)}; linear USDT perps named {len(perps)}")
    probe = sorted(perps.items(), key=lambda kv: kv[1])[:3]
    http = Http("bybit_probe", "https://api.bybit.com", rate=1, burst=1, timeout=30, retries=2)
    res = []
    try:
        for sym, a in probe:
            k = await http.get_json(
                "/v5/market/kline",
                params={"category": "linear", "symbol": sym, "interval": "240", "start": a - 30 * 86_400_000, "limit": 200},
            )
            f = await http.get_json(
                "/v5/market/funding/history", params={"category": "linear", "symbol": sym, "endTime": a + 60 * 86_400_000, "limit": 200}
            )
            kl = (k.data.get("result") or {}).get("list") or []
            fl = (f.data.get("result") or {}).get("list") or []
            res.append({"symbol": sym, "ann": fmt(a), "kline_retCode": k.data.get("retCode"), "kline_rows": len(kl),
                        "funding_retCode": f.data.get("retCode"), "funding_rows": len(fl),
                        "funding_last": fmt(int(fl[0]["fundingRateTimestamp"])) if fl else None})  # fmt: skip
    finally:
        await http.aclose()
    for x in res:
        print(f"  probe {x}")
    (cache / "bybit.json").write_text(json.dumps({"n_ann": len(rows), "kinds": dict(kinds), "perps": perps, "probe": res}, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["universe", "fetch", "bodies", "gate", "run", "report", "bybit"])
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--limit", type=int, default=0, help="fetch: at most this many files (timing sample)")
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--exclude", default="", help="run: comma-separated perp symbols to drop (top-contributor rerun)")
    ap.add_argument("--no-placebo", action="store_true")
    ap.add_argument(
        "--supplement-2022", action="store_true", help="gate: add the 2022 Binance perp-delisting titles the shared parser misses"
    )
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "universe":
        stage_universe(cache)
    elif a.stage == "fetch":
        asyncio.run(dl.stage_fetch(cache, a.limit or None))
    elif a.stage == "bodies":
        nev.START = START
        asyncio.run(nev.stage_bodies(NEWS))
    elif a.stage == "gate":
        stage_gate(cache, supplement=a.supplement_2022)
    elif a.stage == "run":
        stage_run(cache, a.out, exclude=frozenset(s for s in a.exclude.split(",") if s), placebo=not a.no_placebo)
    elif a.stage == "report":
        report(json.loads((cache / a.out).read_text()))
    else:
        asyncio.run(stage_bybit(cache))


if __name__ == "__main__":
    main()
