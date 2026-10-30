"""Step 1 for IDEA-20261028-premarket-perp-conversion-fade: one registered pass of H1-H3 (see the card's
"Попередня реєстрація"; parameters are fixed there and are not tuned here).

Stages (per-file JSON cache under --cache-dir, a rerun resumes):
  fetch     per converted pre-market event: Binance USD-M 1h klines (pre-market start .. conversion + 16 d) and
            funding rates; BTCUSDT 1h klines by month and BTC funding
  placebo   new Binance USDT perps without pre-market (events of new-perp-listing-drift, 2025-04-01 .. 2026-09-15):
            archive 1h klines (listing_drift cache read-only, missing months fetched from data.binance.vision) and
            funding rates
  analyze   registered tables (compact print; full rows to results.json)

Inputs (read-only): the step-0 inventory ~/.cache/tradedesk-agents/premarket_inventory/{inventory.json,
ann_convert_times.json, ann/} -- never its quarantine directory -- and ~/.cache/tradedesk-agents/listing_drift/.
Network only through the runtime providers (rt.exchanges.public_get -> Exchanges._call, rt.binance_vision).

    uv run python scripts/verify_premarket_fade.py fetch|placebo|analyze [--only SYM ...]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import resource
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.premarket_fade import (
    DAY_MS,
    HOUR_MS,
    Summary,
    bars_from_rows,
    beta_ols,
    bootstrap_diff_ci,
    entry_point,
    event_outcome,
    mark_h1,
    mark_h2,
    mark_h3,
    registered_t_conv,
    spearman,
    stop_trade,
    summarize,
    tercile_groups,
)
from tradedesk.core.errors import ProviderError
from tradedesk.server import build_server

HOME_CACHE = Path.home() / ".cache" / "tradedesk-agents"
INV = HOME_CACHE / "premarket_inventory"
LD = HOME_CACHE / "listing_drift"
SEEN = {"METUSDT", "CCUSDT", "MONUSDT"}
H7, H24 = 7 * DAY_MS, DAY_MS
WINDOW_AFTER = 16 * DAY_MS
PLACEBO_FROM = int(datetime(2025, 4, 1, tzinfo=UTC).timestamp() * 1000)
PLACEBO_TO = int(datetime(2026, 9, 16, tzinfo=UTC).timestamp() * 1000)
BTC_FROM = int(datetime(2025, 3, 1, tzinfo=UTC).timestamp() * 1000)
NOW_MS = int(datetime(2026, 10, 29, tzinfo=UTC).timestamp() * 1000)
SLIP_STRESS = 0.0015


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(p)


def ts(ms: int | None) -> str:
    return "-" if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def parse_utc(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp() * 1000)


def load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- events


def events() -> list[dict[str, Any]]:
    inv = [r for r in rd(INV / "inventory.json") if r.get("g1")]
    ann = rd(INV / "ann_convert_times.json")
    out = []
    for r in inv:
        sym = r["symbol"]
        announced = [parse_utc(s) for s in ann.get(sym, [])]
        idx = r["conv"] if r.get("conv_src") == "index_1m" else None
        t_conv, src = registered_t_conv(announced, idx, r.get("spot_first"))
        out.append(
            {
                "symbol": sym,
                "first": r["first"],
                "t_conv": t_conv,
                "src": src,
                "index": idx,
                "spot": r.get("spot_first"),
                "hodler": r.get("hodler") is not None,
                "pm_days": r.get("pm_days"),
                "strict": bool(r.get("g1") and r.get("g2") and r.get("g3_strict")),
                "seen": sym in SEEN,
            }
        )
    return sorted(out, key=lambda e: e["t_conv"])


async def klines_1h(rt: Any, sym: str, start: int, end: int) -> list[list[float]]:
    out: list[list[float]] = []
    t = start
    while t < end:
        q = {"symbol": sym, "interval": "1h", "startTime": t, "endTime": end, "limit": 1500}
        rows = (await rt.exchanges.public_get("binance", "fapiPublicGetKlines", q)).data
        if not rows:
            break
        out.extend([int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows)
        nxt = int(rows[-1][0]) + HOUR_MS
        if len(rows) < 1500 or nxt <= t:
            break
        t = nxt
    return out


async def funding(rt: Any, sym: str, start: int, end: int) -> list[list[float]]:
    out: list[list[float]] = []
    t = start
    for _ in range(20):
        q = {"symbol": sym, "startTime": t, "endTime": end, "limit": 1000}
        rows = (await rt.exchanges.public_get("binance", "fapiPublicGetFundingRate", q)).data
        if not rows:
            break
        out.extend([int(r["fundingTime"]), float(r["fundingRate"])] for r in rows)
        if len(rows) < 1000:
            break
        t = int(out[-1][0]) + 1
    return out


async def stage_fetch(cache: Path, only: list[str]) -> None:
    _, rt = build_server()
    t_start = time.time()
    try:
        y, m = 2025, 3
        while (y, m) <= (2026, 10):
            p = cache / "btc" / f"1h_{y:04d}-{m:02d}.json"
            if not p.exists():
                a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
                ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
                b = min(int(datetime(ny, nm, 1, tzinfo=UTC).timestamp() * 1000), NOW_MS)
                wr(p, await klines_1h(rt, "BTCUSDT", a, b - 1))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        p = cache / "btc" / "funding.json"
        if not p.exists():
            wr(p, await funding(rt, "BTCUSDT", BTC_FROM, NOW_MS))
        print(f"btc done {time.time() - t_start:.0f}s rss {rss_mb():.0f} MB", flush=True)
        evs = [e for e in events() if not only or e["symbol"] in only]
        for n, e in enumerate(evs, 1):
            p = cache / "ev" / f"{e['symbol']}.json"
            if p.exists():
                continue
            last = max(x for x in (e["t_conv"], e["index"], e["spot"]) if x is not None)
            end = min(last + WINDOW_AFTER, NOW_MS)
            start = e["first"] - DAY_MS
            rec = {"symbol": e["symbol"], "bars": await klines_1h(rt, e["symbol"], start, end)}
            rec["funding"] = await funding(rt, e["symbol"], start, end)
            wr(p, rec)
            print(
                f"  [{n}/{len(evs)}] {e['symbol']} bars={len(rec['bars'])} fund={len(rec['funding'])} "
                f"{time.time() - t_start:.0f}s rss {rss_mb():.0f} MB",
                flush=True,
            )
    finally:
        await rt.aclose()
    print("fetch done", flush=True)


# ---------------------------------------------------------------- placebo


def placebo_events() -> list[dict[str, Any]]:
    ld = load_script("verify_listing_drift")
    inv = load_script("verify_premarket_inventory")
    pm = {r["symbol"] for r in rd(INV / "inventory.json")}
    pm_assets = {s[:-4] for s in pm}
    raw, _ = ld.build_events(LD)
    hod = inv.flags_by_base(INV)["hodler"]
    out = []
    for e in raw:
        if not (PLACEBO_FROM <= e["t0"] < PLACEBO_TO) or e["symbol"] in pm or e["asset"] in pm_assets:
            continue
        base = e["symbol"][:-4]
        out.append({**e, "hodler": base in hod or e["asset"] in hod})
    return out


async def stage_placebo(cache: Path) -> None:
    _, rt = build_server()
    evs = placebo_events()
    print(f"placebo events: {len(evs)} (HODLer {sum(e['hodler'] for e in evs)})", flush=True)
    t_start = time.time()
    try:
        for n, e in enumerate(evs, 1):
            p = cache / "placebo" / f"{e['symbol']}.json"
            if p.exists():
                continue
            sym, t0 = e["symbol"], e["t0"]
            rows: list[list[float]] = []
            months = sorted({datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m") for t in (t0, t0 + WINDOW_AFTER)})
            for ym in months:
                y, m = map(int, ym.split("-"))
                src = LD / "k1h" / sym / f"{ym}.json"
                own = cache / "placebo_k1h" / sym / f"{ym}.json"
                if src.exists():
                    part = rd(src)
                elif own.exists():
                    part = rd(own)
                else:
                    try:
                        part = (await rt.binance_vision.um_klines_monthly(sym, "1h", y, m)).data
                    except ProviderError as err:
                        part = None
                        print(f"    {sym} {ym}: {err.reason}", flush=True)
                    wr(own, part)
                rows.extend(part or [])
            rows = [[float(x) for x in r[:6]] for r in rows if t0 - DAY_MS <= r[0] <= t0 + WINDOW_AFTER]
            try:
                fund = await funding(rt, sym, t0 - DAY_MS, t0 + WINDOW_AFTER)
                ferr = None
            except ProviderError as err:
                fund, ferr = [], err.reason
            wr(p, {**e, "bars": rows, "funding": fund, "funding_error": ferr})
            if n % 20 == 0 or n == len(evs):
                print(
                    f"  [{n}/{len(evs)}] {sym} bars={len(rows)} fund={len(fund)} {time.time() - t_start:.0f}s rss {rss_mb():.0f} MB",
                    flush=True,
                )
    finally:
        await rt.aclose()
    print("placebo done", flush=True)


# ---------------------------------------------------------------- analyze


def load_btc(cache: Path) -> tuple[dict[int, tuple[float, float, float, float]], list[tuple[int, float]]]:
    rows: list[list[float]] = []
    for p in sorted((cache / "btc").glob("1h_*.json")):
        rows.extend(rd(p))
    return bars_from_rows(rows), [(int(t), float(r)) for t, r in rd(cache / "btc" / "funding.json")]


def pct(x: float) -> str:
    return "nan" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:+.1f}"


def line(label: str, s: Summary) -> str:
    return (
        f"{label:<44} n={s.n:>3} mean {pct(s.mean):>6} med {pct(s.median):>6} sd {pct(s.sd):>5} "
        f"neg {100 * s.share_neg if s.n else float('nan'):>3.0f}% boot[{pct(s.boot_lo)};{pct(s.boot_hi)}] "
        f"t[{pct(s.t_lo)};{pct(s.t_hi)}]"
    )


def event_row(e: dict[str, Any], rec: dict[str, Any], btc: Any, fbtc: Any, t0: int) -> dict[str, Any] | None:
    bars = bars_from_rows(rec["bars"])
    fund = [(int(t), float(r)) for t, r in rec["funding"]]
    en = entry_point(bars, t0)
    if en is None:
        return None
    bpm, nobs = beta_ols(bars, btc, t0)
    row: dict[str, Any] = {"t_e": en.t_e, "p_pre": en.p_pre, "p0": en.p0, "g": en.gap, "beta_pm": bpm, "beta_n": nobs}
    for hname, h in (("24h", H24), ("7d", H7)):
        for bname, b in (("b1", 1.0), ("b0", 0.0), ("bpm", bpm)):
            o = event_outcome(bars, btc, fund, fbtc, en.t_e, en.p0, h, b)
            row[f"{hname}_{bname}"] = None if o is None else o.__dict__
        o = event_outcome(bars, btc, fund, fbtc, en.t_e, en.p0, h, 1.0, slip=SLIP_STRESS)
        row[f"{hname}_stress"] = None if o is None else o.net
    st = stop_trade(bars, btc, fund, fbtc, en.t_e, en.p0, H7, 1.0)
    row["stop"] = None if st is None else {"pnl": st.pnl, "stopped": st.stopped}
    for sname, sh in (("plus7", H7), ("minus7", -H7)):
        p0 = None
        t_e2 = en.t_e + sh
        b = bars.get(t_e2 - HOUR_MS)
        p0 = b[3] if b else None
        ok = sh > 0 or (e.get("pm_days") or 0) >= 8
        o = event_outcome(bars, btc, fund, fbtc, t_e2, p0, H7, 1.0) if p0 and ok else None
        row[sname] = None if o is None else o.net
    # descriptive only (look-ahead): P_pre vs mean close of the first 72 h after entry
    c72 = [bars[t][3] for t in range(en.t_e, en.t_e + 3 * DAY_MS, HOUR_MS) if t in bars]
    row["prem_3d"] = math.log(en.p_pre / statistics.fmean(c72)) if c72 else None
    return row


def net7(r: dict[str, Any], key: str = "7d_b1") -> float | None:
    v = r.get(key)
    return None if v is None else v["net"]


def stage_analyze(cache: Path) -> None:
    btc, fbtc = load_btc(cache)
    evs = events()
    rows = []
    for e in evs:
        p = cache / "ev" / f"{e['symbol']}.json"
        if not p.exists():
            print(f"missing {e['symbol']}")
            continue
        rec = rd(p)
        main = event_row(e, rec, btc, fbtc, e["t_conv"])
        r = {**e, "main": main}
        r["alt_index"] = event_row(e, rec, btc, fbtc, e["index"]) if e["index"] else None
        r["alt_spot"] = event_row(e, rec, btc, fbtc, e["spot"]) if e["spot"] else None
        rows.append(r)
    wr(cache / "results.json", rows)

    print("\nEVENTS (t_conv rule: announced > Binance spot open > index); returns in %, R7 net = beta 1, fees, funding")
    print(
        f"{'symbol':<11} {'t_conv':<16} src  pmD  H S seen  {'g':>6} {'R24n':>6} {'R7g':>6} {'R7n':>6} {'fund7':>6} "
        f"{'bpm':>4} {'stop':>6} {'+7d':>6}"
    )
    for r in rows:
        m = r["main"]
        if m is None:
            print(f"{r['symbol']:<11} no entry")
            continue
        g7 = m["7d_b1"]
        print(
            f"{r['symbol']:<11} {ts(r['t_conv']):<16} {r['src'][:4]:<4} {r['pm_days']:>4.0f} {'y' if r['hodler'] else '.'} "
            f"{'y' if r['strict'] else '.'} {'SEEN' if r['seen'] else '    '} {pct(m['g']):>6} {pct(net7(m, '24h_b1') or math.nan):>6} "
            f"{pct(g7['gross'] if g7 else math.nan):>6} {pct(g7['net'] if g7 else math.nan):>6} "
            f"{pct(-g7['f_tok'] if g7 else math.nan):>6} {m['beta_pm']:>4.1f} "
            f"{pct(m['stop']['pnl'] if m['stop'] else math.nan):>6} {pct(m['plus7'] if m['plus7'] is not None else math.nan):>6}"
        )

    def coll(sel: list[dict[str, Any]], f: Any) -> list[float]:
        out = []
        for r in sel:
            v = f(r)
            if v is not None:
                out.append(v)
        return out

    prim = [r for r in rows if not r["seen"] and r["main"]]
    allr = [r for r in rows if r["main"]]
    strict = [r for r in prim if r["strict"]]
    res: dict[str, Any] = {}
    print("\nH1 (R_7d, beta 1; net = short pays fees + funding)")
    s_g = summarize(coll(prim, lambda r: r["main"]["7d_b1"] and r["main"]["7d_b1"]["gross"]))
    s_n = summarize(coll(prim, lambda r: net7(r["main"])))
    print(line("primary 25 gross", s_g))
    print(line("primary 25 net", s_n))
    res["H1"] = mark_h1(s_g.mean, s_n)
    print(f"H1 mark: {res['H1']}")
    print("\nH2 (g = ln(P_0/P_pre))")
    s_gap = summarize(coll(prim, lambda r: r["main"]["g"]))
    print(line("primary 25 g", s_gap))
    print(line("all 28 g", summarize(coll(allr, lambda r: r["main"]["g"]))))
    res["H2"] = mark_h2(s_gap)
    print(f"H2 mark: {res['H2']}")

    print("\nH3 (terciles of premium P_pre/P_0 = -g; R_7d net)")
    h3 = [r for r in prim if net7(r["main"]) is not None]
    prem = [-r["main"]["g"] for r in h3]
    y = [net7(r["main"]) for r in h3]
    bot, top = tercile_groups(prem)
    yt, yb = [y[i] for i in top], [y[i] for i in bot]
    d = statistics.fmean(yt) - statistics.fmean(yb)
    lo, hi = bootstrap_diff_ci(yt, yb)
    rho = spearman(prem, y)
    res["H3"] = mark_h3(d, hi)
    print(f"top tercile {[h3[i]['symbol'][:-4] for i in top]} mean {pct(statistics.fmean(yt))}")
    print(f"bottom tercile {[h3[i]['symbol'][:-4] for i in bot]} mean {pct(statistics.fmean(yb))}")
    print(f"delta top-bottom {pct(d)} pp boot[{pct(lo)};{pct(hi)}]; spearman(premium, R7net) {rho:+.2f}; H3 mark: {res['H3']}")
    p3 = coll(prim, lambda r: r["main"]["prem_3d"])
    print(
        f"descriptive (look-ahead) ln(P_pre / mean close first 72 h): mean {pct(statistics.fmean(p3))}, "
        f"median {pct(statistics.median(p3))}, n={len(p3)}"
    )

    print("\nROBUSTNESS (R net unless stated; all registered variants)")
    variants = [
        ("all 28 R7 net", allr, lambda r: net7(r["main"])),
        ("strict G1&G2&G3s (no seen) R7 net", strict, lambda r: net7(r["main"])),
        ("primary R24 net", prim, lambda r: net7(r["main"], "24h_b1")),
        ("primary R24 gross", prim, lambda r: r["main"]["24h_b1"] and r["main"]["24h_b1"]["gross"]),
        ("primary R7 net beta 0", prim, lambda r: net7(r["main"], "7d_b0")),
        ("primary R7 net beta_pm", prim, lambda r: net7(r["main"], "7d_bpm")),
        ("primary R7 net cost stress (+2x0.15%)", prim, lambda r: r["main"]["7d_stress"]),
        ("primary t0 = index start R7 net", prim, lambda r: r["alt_index"] and net7(r["alt_index"])),
        ("primary t0 = Binance spot open R7 net", prim, lambda r: r["alt_spot"] and net7(r["alt_spot"])),
        ("primary HODLer R7 net", [r for r in prim if r["hodler"]], lambda r: net7(r["main"])),
        ("primary non-HODLer R7 net", [r for r in prim if not r["hodler"]], lambda r: net7(r["main"])),
        ("primary first half (by date) R7 net", prim[: len(prim) // 2], lambda r: net7(r["main"])),
        ("primary second half R7 net", prim[len(prim) // 2 :], lambda r: net7(r["main"])),
        ("primary stop+30% trade: -PnL (simple, net)", prim, lambda r: r["main"]["stop"] and -r["main"]["stop"]["pnl"]),
        ("placebo shift +7d (window 7-14 d) net", prim, lambda r: r["main"]["plus7"]),
        ("placebo shift -7d (pm >= 8 d) net", prim, lambda r: r["main"]["minus7"]),
    ]
    for label, sel, f in variants:
        print(line(label, summarize(coll(sel, f))))
    yy = sorted(coll(prim, lambda r: net7(r["main"])))
    print(line("primary without 2 lowest R7 net", summarize(yy[2:])))
    print(line("primary without 2 highest R7 net", summarize(yy[:-2])))
    contrib = sorted((net7(r["main"]), r["symbol"]) for r in prim if net7(r["main"]) is not None)
    print(f"lowest R7 net: {[(s, pct(v)) for v, s in contrib[:3]]}; highest: {[(s, pct(v)) for v, s in contrib[-3:]]}")
    stopped = sum(1 for r in prim if r["main"]["stop"] and r["main"]["stop"]["stopped"])
    print(
        f"stop +30% hit: {stopped}/{len(prim)}; funding paid by short over 7d (mean, pp): "
        f"{pct(statistics.fmean(coll(prim, lambda r: r['main']['7d_b1'] and -r['main']['7d_b1']['f_tok'])))}"
    )

    pp = cache / "placebo"
    plac: list[float] = []
    plac_h: list[float] = []
    nfe = 0
    if pp.exists():
        for p in sorted(pp.glob("*.json")):
            rec = rd(p)
            nfe += rec.get("funding_error") is not None
            bars = bars_from_rows(rec["bars"])
            t_first = int(rec["t0"])  # first 1h bar with volume (new-perp-listing-drift event time)
            if t_first not in bars:
                continue
            en_t = t_first + HOUR_MS
            o = event_outcome(bars, btc, [(int(t), float(r)) for t, r in rec["funding"]], fbtc, en_t, bars[t_first][3], H7, 1.0)
            if o is not None:
                (plac_h if rec["hodler"] else plac).append(o.net)
        print("\nPLACEBO (a): new Binance perps without pre-market, t0 = first 1h bar, R7 net beta 1")
        print(line("placebo non-HODLer", summarize(plac)))
        print(line("placebo HODLer (S41 tokens)", summarize(plac_h)))
        print(f"placebo funding errors: {nfe}")
        if plac:
            dl, dh = bootstrap_diff_ci(coll(prim, lambda r: net7(r["main"])), plac)
            dd = s_n.mean - statistics.fmean(plac)
            res["vs_placebo"] = dd
            print(f"primary - placebo(non-HODLer) {pct(dd)} pp boot[{pct(dl)};{pct(dh)}]")
            allp = plac + plac_h
            dl2, dh2 = bootstrap_diff_ci(coll(prim, lambda r: net7(r["main"])), allp)
            print(f"primary - placebo(all) {pct(s_n.mean - statistics.fmean(allp))} pp boot[{pct(dl2)};{pct(dh2)}]")

    # registered card verdict
    no_top2 = summarize(yy[2:])
    confirmed = res["H1"] == "✓" and s_g.mean <= -0.10 and no_top2.boot_hi < 0 and res.get("vs_placebo", 1) < 0
    h3_support = res["H3"] in ("✓", "≈") and d <= -0.05
    rejected = s_n.boot_lo >= -0.02 and not h3_support
    verdict = "confirmed" if confirmed else "rejected" if rejected else "inconclusive"
    print(
        f"\nREGISTERED VERDICT: {verdict} (H1 {res['H1']}, H2 {res['H2']}, H3 {res['H3']}; "
        f"H1 net boot lo {pct(s_n.boot_lo)}; forward-kill info: mean R7 net {pct(s_n.mean)} vs 0)"
    )
    print(f"peak RSS {rss_mb():.0f} MB")


def main() -> None:
    a = argparse.ArgumentParser()
    a.add_argument("stage", choices=["fetch", "placebo", "analyze"])
    a.add_argument("--cache-dir", type=Path, default=HOME_CACHE / "premarket_fade")
    a.add_argument("--only", nargs="*", default=[])
    a = a.parse_args()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    if a.stage == "fetch":
        asyncio.run(stage_fetch(a.cache_dir, a.only))
    elif a.stage == "placebo":
        asyncio.run(stage_placebo(a.cache_dir))
    else:
        stage_analyze(a.cache_dir)


if __name__ == "__main__":
    main()
