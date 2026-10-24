"""COIN-M vs USDT-M funding on Binance: fetch, step 0 (sanity + power gate), step 1 (difference), step 2 (net carry).

Idea card: docs/ideas/IDEA-20261025-coinm-vs-usdtm-funding-carry.md ("Попередня реєстрація").
Network only through the runtime providers (``rt.exchanges.public_get``); per-file JSON cache under --cache-dir:
    funding/<dapi|fapi>_<SYMBOL>.json   rows [[t_ms, rate], ...]
    mark/<dapi|fapi>_<SYMBOL>.json      daily mark klines [[open_ms, o, h, l, c], ...]
    ticker24_dapi.json                  current COIN-M 24 h tickers (capacity only)

    uv run python scripts/verify_coinm_funding.py fetch --cache-dir ~/.cache/tradedesk-agents/coinm_funding [--sample]
    uv run python scripts/verify_coinm_funding.py step0 --cache-dir ...
    uv run python scripts/verify_coinm_funding.py step1 --cache-dir ...
    uv run python scripts/verify_coinm_funding.py step2 --cache-dir ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import statistics
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tradedesk.analytics.coinm_funding import (
    ANCHOR,
    DAY_MS,
    HOUR_MS,
    WEEK_MS,
    Costs,
    Day,
    aligned,
    anchor_decomposition,
    ann,
    block_sign_flip_p,
    bucket_8h,
    carry_coinm,
    carry_usdtm,
    dropped_buckets,
    moving_block_bootstrap,
    round_hour,
    weekly,
)
from tradedesk.analytics.per_asset import cluster_robust_q, permutation_q, persistence
from tradedesk.core.errors import ProviderError


def ms(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


START = ms(2020, 8, 1)
END = ms(2026, 10, 25)
TRAIN_END = ms(2026, 4, 1)
MAIN = (ms(2023, 4, 1), TRAIN_END)
HOLD = (TRAIN_END, END)
LAST_YEAR = (END - 52 * WEEK_MS, END)  # filtered by week start, so the window holds the last 52 complete weeks
# currently listed COIN-M perps are taken from exchangeInfo; these were listed at some point (survivorship)
PAST = (
    "EOS EGLD THETA XTZ ATOM ALGO ICP FTM SAND MANA GALA MATIC AXS APE KNC ROSE OP ARB APT ENS GMT WIF ZEC DASH XMR "
    "VET ICX NEO QTUM ONT IOTA KSM COMP MKR SUSHI CRV RUNE GRT CHZ ENJ BAT ZIL WAVES OMG HBAR FLOW ONE KAVA INJ TIA SEI"
).split()
BIG = {"BTC", "ETH"}


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d")


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(p)


def sym(venue: str, a: str) -> str:
    return f"{a}USD_PERP" if venue == "dapi" else f"{a}USDT"


# ----------------------------------------------------------------------------- fetch


async def fetch_funding(rt: Any, cache: Path, venue: str, a: str) -> int:
    p = cache / "funding" / f"{venue}_{sym(venue, a)}.json"
    if p.exists():
        return -1
    ep = "dapiPublicGetFundingRate" if venue == "dapi" else "fapiPublicGetFundingRate"
    rows: list[list[float]] = []
    cur = START
    while cur < END:
        try:
            r = (await rt.exchanges.public_get("binance", ep, {"symbol": sym(venue, a), "startTime": cur, "limit": 1000})).data
        except ProviderError as ex:
            print(f"  {venue} {a}: {ex.reason}", flush=True)
            break
        await asyncio.sleep(0.6 if venue == "fapi" else 0.25)
        if not r:
            break
        rows += [[int(x["fundingTime"]), float(x["fundingRate"])] for x in r if x.get("fundingRate") not in (None, "")]
        cur = int(r[-1]["fundingTime"]) + 1
        if len(r) < 1000:
            break
    rows = [x for x in rows if x[0] < END]
    write_json(p, rows)
    return len(rows)


async def fetch_mark(rt: Any, cache: Path, venue: str, a: str, first_ms: int) -> int:
    p = cache / "mark" / f"{venue}_{sym(venue, a)}.json"
    if p.exists():
        return -1
    ep = "dapiPublicGetMarkPriceKlines" if venue == "dapi" else "fapiPublicGetMarkPriceKlines"
    out: list[list[float]] = []
    cur = first_ms - first_ms % DAY_MS
    while cur < END:
        params = {"symbol": sym(venue, a), "interval": "1d", "startTime": cur, "endTime": min(END, cur + 199 * DAY_MS), "limit": 200}
        try:
            r = (await rt.exchanges.public_get("binance", ep, params)).data
        except ProviderError as ex:
            print(f"  mark {venue} {a}: {ex.reason}", flush=True)
            break
        await asyncio.sleep(0.25)
        out += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in r]
        cur += 200 * DAY_MS
    dedup = {int(k[0]): k for k in out if k[0] < END - END % DAY_MS}
    write_json(p, [dedup[t] for t in sorted(dedup)])
    return len(dedup)


async def fetch(args: argparse.Namespace) -> None:
    from tradedesk.server import build_server

    cache: Path = args.cache_dir.expanduser()
    _, rt = build_server()
    t0 = time.perf_counter()
    try:
        info = (await rt.exchanges.public_get("binance", "dapiPublicGetExchangeInfo", {})).data
        live = sorted(s["symbol"][: -len("USD_PERP")] for s in info["symbols"] if s.get("contractType") == "PERPETUAL")
        assets = live + [a for a in PAST if a not in live]
        if args.sample:
            assets = assets[:2]
        print(f"assets to try: {len(assets)} ({len(live)} listed now)", flush=True)
        write_json(cache / "universe.json", {"live": live, "tried": assets})
        tick = cache / "ticker24_dapi.json"
        if not tick.exists():
            write_json(tick, (await rt.exchanges.public_get("binance", "dapiPublicGetTicker24hr", {})).data)
        for i, a in enumerate(assets):
            nd = await fetch_funding(rt, cache, "dapi", a)
            rows = json.loads((cache / "funding" / f"dapi_{sym('dapi', a)}.json").read_text())
            if not rows:
                print(f"  [{i + 1}/{len(assets)}] {a}: no COIN-M history", flush=True)
                continue
            nf = await fetch_funding(rt, cache, "fapi", a)
            first = int(rows[0][0])
            nm = await fetch_mark(rt, cache, "dapi", a, first)
            nu = await fetch_mark(rt, cache, "fapi", a, first)
            print(
                f"  [{i + 1}/{len(assets)}] {a}: dapi {nd} fapi {nf} mark {nm}/{nu} from {ymd(first)}; "
                f"{time.perf_counter() - t0:.0f}s rss {rss_mb():.0f} MB",
                flush=True,
            )
    finally:
        await rt.aclose()
    print(f"fetch done in {time.perf_counter() - t0:.0f}s", flush=True)


# ----------------------------------------------------------------------------- load


def load_rows(cache: Path, venue: str, a: str) -> list[tuple[int, float]]:
    p = cache / "funding" / f"{venue}_{sym(venue, a)}.json"
    return [(int(t), float(r)) for t, r in json.loads(p.read_text())] if p.exists() else []


def load_mark(cache: Path, venue: str, a: str) -> dict[int, list[float]]:
    p = cache / "mark" / f"{venue}_{sym(venue, a)}.json"
    return {int(k[0]): k for k in json.loads(p.read_text())} if p.exists() else {}


def assets_with_data(cache: Path) -> list[str]:
    u = json.loads((cache / "universe.json").read_text())["tried"]
    return [a for a in u if load_rows(cache, "dapi", a) and load_rows(cache, "fapi", a)]


def asset_weeks(cache: Path, a: str) -> tuple[list[Any], list[tuple[int, float, float]], dict[str, int]]:
    c, u = load_rows(cache, "dapi", a), load_rows(cache, "fapi", a)
    al = aligned(bucket_8h(c), bucket_8h(u))
    w = weekly([(t, fc - fu) for t, fc, fu in al])
    return w, al, {"n_c": len(c), "n_u": len(u), "drop_c": dropped_buckets(c), "drop_u": dropped_buckets(u), "aligned": len(al)}


def in_win(t: int, win: tuple[int, int]) -> bool:
    return win[0] <= t < win[1]


def pp(x: float) -> str:
    return f"{100 * x:+.2f}"


def boot_line(label: str, x: list[float], *, means: bool = True) -> str:
    """x are weekly means per 8 h; reported annualised in pp."""
    if len(x) < 2:
        return f"{label:<26} n={len(x)}"
    b = moving_block_bootstrap(x)
    head = f"{label:<26} n={b['n']:>4} SD/wk {1e4 * float(np.std(x, ddof=1)):.2f} bp  SE {100 * ann(b['se']):.2f} pp  MDE {100 * ann(b['mde']):.2f} pp"
    if not means:
        return head
    return head + f"  mean {pp(ann(b['mean']))} pp  CI [{pp(ann(b['lo']))}, {pp(ann(b['hi']))}]"


def combine(weeks_by_asset: dict[str, list[Any]], keep: set[str], win: tuple[int, int]) -> list[tuple[int, float]]:
    """Equal-weight weekly mean across the assets in ``keep`` present that week."""
    acc: dict[int, list[float]] = defaultdict(list)
    for a, ws in weeks_by_asset.items():
        if a in keep:
            for w in ws:
                if in_win(w.start, win):
                    acc[w.start].append(w.mean)
    return [(t, float(np.mean(v))) for t, v in sorted(acc.items())]


def btc_eth(weeks_by_asset: dict[str, list[Any]], win: tuple[int, int]) -> list[tuple[int, float]]:
    b = {w.start: w.mean for w in weeks_by_asset.get("BTC", []) if in_win(w.start, win)}
    e = {w.start: w.mean for w in weeks_by_asset.get("ETH", []) if in_win(w.start, win)}
    return [(t, (b[t] + e[t]) / 2) for t in sorted(b.keys() & e.keys())]


def load_all(cache: Path) -> tuple[list[str], dict[str, list[Any]], dict[str, list[tuple[int, float, float]]], dict[str, dict[str, int]]]:
    assets = assets_with_data(cache)
    wk, al, meta = {}, {}, {}
    for a in assets:
        wk[a], al[a], meta[a] = asset_weeks(cache, a)
    return assets, wk, al, meta


# ----------------------------------------------------------------------------- step 0


def step0(args: argparse.Namespace) -> None:
    cache: Path = args.cache_dir.expanduser()
    assets, wk, al, meta = load_all(cache)
    print(f"assets with both venues: {len(assets)}; rss {rss_mb():.0f} MB")
    print(
        f"{'asset':<7}{'from':<11}{'to':<11}{'n_c':>6}{'n_u':>6}{'drop_c':>7}{'drop_u':>7}{'algn':>6}{'wk':>5}{'wk3y':>5}{'px c/u':>8}{'anc_c':>7}{'anc_u':>7}  intervals_u(h)"
    )
    basket: set[str] = set()
    for a in assets:
        c, u = load_rows(cache, "dapi", a), load_rows(cache, "fapi", a)
        mc, mu = load_mark(cache, "dapi", a), load_mark(cache, "fapi", a)
        common = sorted(mc.keys() & mu.keys())
        ratio = statistics.median(mc[t][4] / mu[t][4] for t in common) if common else float("nan")
        iu = Counter(round((round_hour(u[i][0]) - round_hour(u[i - 1][0])) / HOUR_MS) for i in range(1, len(u)))
        ic = Counter(round((round_hour(c[i][0]) - round_hour(c[i - 1][0])) / HOUR_MS) for i in range(1, len(c)))
        n3 = sum(1 for w in wk[a] if in_win(w.start, MAIN))
        if n3 >= 52:
            basket.add(a)
        anc_c = np.mean([abs(fc - ANCHOR) < 1e-9 for _, fc, _ in al[a]]) if al[a] else float("nan")
        anc_u = np.mean([abs(fu - ANCHOR) < 1e-9 for _, _, fu in al[a]]) if al[a] else float("nan")
        m = meta[a]
        print(
            f"{a:<7}{ymd(c[0][0]):<11}{ymd(c[-1][0]):<11}{m['n_c']:>6}{m['n_u']:>6}{m['drop_c']:>7}{m['drop_u']:>7}{m['aligned']:>6}"
            f"{len(wk[a]):>5}{n3:>5}{ratio:>8.4f}{anc_c:>7.2f}{anc_u:>7.2f}  {dict(iu.most_common(3))} c:{dict(ic.most_common(2))}"
        )
    print(f"\nbasket (>= 52 weeks in main window {ymd(MAIN[0])}..{ymd(MAIN[1])}): {len(basket)} assets: {sorted(basket)}")
    write_json(cache / "basket.json", sorted(basket))
    print("\nPOWER (no means): SD of weekly d, block-bootstrap SE and MDE, annualised pp on notional")
    for win_name, win in (("main 3y", MAIN), ("hold-out", HOLD), ("last year", LAST_YEAR)):
        for a in ("BTC", "ETH"):
            print(boot_line(f"{a} {win_name}", [w.mean for w in wk.get(a, []) if in_win(w.start, win)], means=False))
        print(boot_line(f"BTC+ETH {win_name}", [v for _, v in btc_eth(wk, win)], means=False))
        print(boot_line(f"basket {win_name}", [v for _, v in combine(wk, basket, win)], means=False))
    be = moving_block_bootstrap([v for _, v in btc_eth(wk, MAIN)])
    bk = moving_block_bootstrap([v for _, v in combine(wk, basket, MAIN)])
    g1, g2 = ann(be["mde"]) <= 0.008, ann(bk["mde"]) <= 0.015 and len(basket) >= 8
    print(f"\nGATE H1 (MDE BTC+ETH main <= 0.80 pp): {100 * ann(be['mde']):.2f} pp -> {'PASS' if g1 else 'FAIL'}")
    print(
        f"GATE H2 (MDE basket main <= 1.50 pp, >= 8 assets): {100 * ann(bk['mde']):.2f} pp, {len(basket)} assets -> {'PASS' if g2 else 'FAIL'}"
    )
    # pipeline check vs the scout's look: last 499 settlements (mean of raw rates, no alignment), annualised
    for a in ("BTC", "ETH", "SOL", "XRP"):
        c, u = load_rows(cache, "dapi", a)[-499:], load_rows(cache, "fapi", a)[-499:]
        if c and u:
            print(
                f"scout replication {a}: last 499 raw settlements COIN-M - USDT-M = {pp(ann(np.mean([r for _, r in c]) - np.mean([r for _, r in u])))} pp/yr (scout: BTC +1.14 ETH +1.12 SOL +2.66 XRP +3.68)"
            )
    print(f"rss {rss_mb():.0f} MB")


# ----------------------------------------------------------------------------- step 1


def step1(args: argparse.Namespace) -> None:
    cache: Path = args.cache_dir.expanduser()
    assets, wk, al, _ = load_all(cache)
    basket = set(json.loads((cache / "basket.json").read_text()))
    full = (START, END)
    print("H1/H2: d = COIN-M - USDT-M, weekly means, annualised pp on notional; CI = moving-block bootstrap (4 wk)")
    for win_name, win in (
        ("main 3y", MAIN),
        ("hold-out", HOLD),
        ("last year", LAST_YEAR),
        ("full 2020-08..", full),
        ("train full", (START, TRAIN_END)),
    ):
        x_be = [v for _, v in btc_eth(wk, win)]
        x_bk = [v for _, v in combine(wk, basket, win)]
        x_all = [v for _, v in combine(wk, set(assets), win)]
        for a in ("BTC", "ETH"):
            print(boot_line(f"{a} {win_name}", [w.mean for w in wk.get(a, []) if in_win(w.start, win)]))
        print(boot_line(f"BTC+ETH {win_name}", x_be) + f"  placebo p {block_sign_flip_p(x_be):.4f}")
        print(boot_line(f"basket {win_name}", x_bk) + f"  placebo p {block_sign_flip_p(x_bk):.4f}")
        print(boot_line(f"all assets {win_name}", x_all))
    print("\nby calendar year (BTC+ETH | basket of all assets present that week)")
    for y in range(2020, 2027):
        win = (ms(y, 1, 1), min(ms(y + 1, 1, 1), END))
        print(
            boot_line(f"BTC+ETH {y}", [v for _, v in btc_eth(wk, win)])
            + " | "
            + boot_line(f"all {y}", [v for _, v in combine(wk, set(assets), win)]).split("  mean ")[-1]
        )
    print("\nquarters, basket (main-window basket assets), mean pp/yr:")
    qs = []
    q_rows = []
    for y in range(2020, 2027):
        for qn in range(4):
            win = (ms(y, 1 + 3 * qn, 1), ms(y + (qn == 3), 1 + 3 * ((qn + 1) % 4), 1))
            xb = [v for _, v in combine(wk, basket, win)]
            xe = [v for _, v in btc_eth(wk, win)]
            if xb:
                in_main = in_win(win[0], MAIN)
                qs.append((f"{y}Q{qn + 1}", ann(float(np.mean(xb))), ann(float(np.mean(xe))) if xe else float("nan"), in_main))
                q_rows.append(f"{y}Q{qn + 1}{'*' if in_main else ' '} bk {pp(qs[-1][1])} be {pp(qs[-1][2])}")
    for i in range(0, len(q_rows), 4):
        print("  " + " | ".join(q_rows[i : i + 4]))
    qm = [q for q in qs if q[3]]
    print(
        f"main-window quarters (*): basket positive {sum(q[1] > 0 for q in qm)}/{len(qm)}, BTC+ETH positive {sum(q[2] > 0 for q in qm)}/{len(qm)}"
    )
    print(
        f"all quarters: basket positive {sum(q[1] > 0 for q in qs)}/{len(qs)}, BTC+ETH positive {sum(q[2] > 0 for q in qs if q[2] == q[2])}/{sum(1 for q in qs if q[2] == q[2])}"
    )

    print("\nanchor decomposition (main window, pooled buckets): share, contribution to mean d in pp/yr")
    for name, keep in (("BTC+ETH", BIG), ("basket", basket)):
        rows = [r for a in keep for r in al[a] if in_win(r[0], MAIN)]
        dec = anchor_decomposition(rows)
        tot = sum(v["contrib"] for v in dec.values())
        print(f"  {name}: mean d {pp(ann(tot))}; " + "; ".join(f"{k} {v['share']:.2f} / {pp(ann(v['contrib']))}" for k, v in dec.items()))
        fc = Counter(round(r[1], 8) for r in rows).most_common(3)
        fu = Counter(round(r[2], 8) for r in rows).most_common(3)
        print(f"    modal rates COIN-M {fc}; USDT-M {fu}")
        lvl = [r for r in rows if r[1] > ANCHOR + 1e-9 and r[2] > ANCHOR + 1e-9]
        if lvl:
            print(
                f"    both above anchor: n {len(lvl)}, mean f_c {1e4 * np.mean([r[1] for r in lvl]):.3f} bp, f_u {1e4 * np.mean([r[2] for r in lvl]):.3f} bp, ratio {np.mean([r[1] for r in lvl]) / np.mean([r[2] for r in lvl]):.3f}"
            )

    print("\nPER-ASSET (main window): n weeks, mean, CI, MDE (pp/yr); training-to-hold-out persistence")
    vals, labs, clus, blk = [], [], [], []
    contrib = {}
    for a in sorted(assets):
        x = [w.mean for w in wk[a] if in_win(w.start, MAIN)]
        if len(x) >= 26:
            b = moving_block_bootstrap(x)
            contrib[a] = b["mean"]
            print(
                f"  {a:<6} n {b['n']:>4} mean {pp(ann(b['mean']))} CI [{pp(ann(b['lo']))}, {pp(ann(b['hi']))}] MDE {100 * ann(b['mde']):.2f}{'  basket' if a in basket else ''}"
            )
        for w in wk[a]:
            if w.start < END:
                vals.append(ann(w.mean))
                labs.append(a)
                clus.append(w.start)
                blk.append(w.start // (4 * WEEK_MS))
    m_mask = [in_win(c, MAIN) for c in clus]
    v_m = [v for v, k in zip(vals, m_mask, strict=True) if k]
    l_m = [v for v, k in zip(labs, m_mask, strict=True) if k]
    c_m = [v for v, k in zip(clus, m_mask, strict=True) if k]
    b_m = [v for v, k in zip(blk, m_mask, strict=True) if k]
    cq = cluster_robust_q(v_m, l_m, b_m, min_n=52)
    pq = permutation_q(v_m, l_m, c_m, min_n=52, n_perm=2000)
    print(
        f"  heterogeneity main: Cochran Q (cluster-robust, 4-wk) {cq['q']:.1f} df {cq['df']} p {cq['p']:.4g} I2 {cq['i2']:.2f}; permutation within weeks p {pq['p']:.4f}"
    )
    sel = [i for i, c in enumerate(clus) if c >= MAIN[0]]
    per = persistence(
        [vals[i] for i in sel],
        [labs[i] for i in sel],
        [clus[i] for i in sel],
        [clus[i] < TRAIN_END for i in sel],
        min_n_is=52,
        top_frac=0.25,
        min_n_oos_rho=10,
        n_boot=5000,
    )
    print(
        f"  persistence (IS {ymd(MAIN[0])}..{ymd(TRAIN_END)}, OOS hold-out, pp/yr): top {per['top']}; IS top {pp(per['top_is_mean'])} rest {pp(per['rest_is_mean'])}; "
        f"OOS top {pp(per['top_oos_mean'])} vs rest {pp(per['rest_oos_mean'])}; diff {pp(per['diff'])} CI {[pp(x) for x in per['diff_ci']] if per['diff_ci'] else None}; "
        f"p {per['diff_p']}; Spearman IS-OOS {per['rho']:.2f} (n {per['rho_n']})"
    )
    print("  IS ranking: " + ", ".join(f"{a} {pp(m)}" for a, _, m in per["is_ranked"]))
    top2 = sorted((a for a in contrib if a in basket), key=lambda a: -contrib[a])[:2]
    xb = [v for _, v in combine(wk, basket - set(top2), MAIN)]
    print(boot_line(f"basket w/o top2 {top2}", xb))
    topq = sorted((a for a in contrib if a in basket), key=lambda a: -contrib[a])[: max(1, len(basket) // 4)]
    print(boot_line("basket w/o top quarter", [v for _, v in combine(wk, basket - set(topq), MAIN)]) + f"  dropped {topq}")
    print(f"rss {rss_mb():.0f} MB")


# ----------------------------------------------------------------------------- step 2


def build_days(cache: Path, a: str) -> list[Day]:
    c, u = load_rows(cache, "dapi", a), load_rows(cache, "fapi", a)
    mc, mu = load_mark(cache, "dapi", a), load_mark(cache, "fapi", a)
    if not mc:  # delisted COIN-M symbols: dapi mark klines unavailable -> USDT-M mark as the coin price (labelled in step 2)
        mc = mu
    fc: dict[int, list[float]] = defaultdict(list)
    fu: dict[int, list[float]] = defaultdict(list)
    # a settlement at 00:00 accrued over the previous day: assign to the day of (t - 1 ms)
    for t, r in c:
        d = round_hour(t) - 1
        fc[d - d % DAY_MS].append(r)
    for t, r in u:
        d = round_hour(t) - 1
        fu[d - d % DAY_MS].append(r)
    days = []
    for t in sorted(mc.keys() & mu.keys()):
        if t in fc and t in fu:
            days.append(Day(t, mc[t][4], mu[t][4], mu[t][2], tuple(fc[t]), tuple(fu[t])))
    return days


def quarters(win: tuple[int, int]) -> list[tuple[int, int]]:
    out = []
    for y in range(2020, 2027):
        for qn in range(4):
            q = (ms(y, 1 + 3 * qn, 1), ms(y + (qn == 3), 1 + 3 * ((qn + 1) % 4), 1))
            if q[0] >= win[0] and q[0] < win[1]:
                out.append((q[0], min(q[1], win[1])))
    return out


def step2(args: argparse.Namespace) -> None:
    cache: Path = args.cache_dir.expanduser()
    basket = json.loads((cache / "basket.json").read_text())
    variants: dict[str, dict[str, Any]] = {
        "primary m=0": {"m": 0.0},
        "card m=0.2": {"m": 0.2},
        "m=0.1": {"m": 0.1},
        "m=0.33": {"m": 0.33},
        "m=0 hold coins to week end": {"m": 0.0, "hold": True},
        "m=0 slip x3": {"m": 0.0, "slip_x": 3},
        "m=0.2 slip x3": {"m": 0.2, "slip_x": 3},
        # exploratory (added after the first step-2 run): with coin collateral B is delta-neutral without any reset
        "EXPL m=0 no rebalance": {"m": 0.0, "band": 1e9},
    }
    liq_big: Counter = Counter()
    res: dict[str, dict[str, dict[int, float]]] = {v: defaultdict(dict) for v in variants}
    liq: Counter = Counter()
    a_minus_b_naive: dict[str, list[float]] = defaultdict(list)
    for a in basket:
        days = build_days(cache, a)
        slip = 0.0002 if a in BIG else 0.0005
        for qs, qe in quarters((MAIN[0], END)):
            dq = [d for d in days if qs <= d.t < qe]
            if len(dq) < 60:
                continue
            yrs = (len(dq)) / 365
            for name, v in variants.items():
                c = Costs(slip=slip * v.get("slip_x", 1))
                ra = carry_coinm(dq, c, hold_coins_to_week_end=v.get("hold", False))
                rb = carry_usdtm(dq, c, margin=v["m"], band=v.get("band", 0.05))
                liq[name] += rb["liquidations"]
                if a in BIG:
                    liq_big[(name, a)] += rb["liquidations"]
                res[name][a][qs] = (ra["net"] - rb["net"]) / yrs
                if name == "primary m=0":
                    a_minus_b_naive[a].append((ra["funding"] - rb["funding"]) / yrs)
                    res.setdefault("A net", defaultdict(dict))[a][qs] = ra["net"] / yrs
                    res.setdefault("B net", defaultdict(dict))[a][qs] = rb["net"] / yrs
    print("H3: A (coin collateral + short COIN-M) minus B (USDT-M leg), net of costs, per 1 USD capital, pp/yr")
    print(
        "    unit = asset x calendar quarter (13 wk hold, entry+exit each quarter); basket = mean over assets per quarter; CI = bootstrap over quarters"
    )
    for name in [*variants, "A net", "B net"]:
        for wn, win in (("main", MAIN), ("hold-out", HOLD)):
            qk = [q for q, _ in quarters(win)]
            per_q = [
                float(np.mean([res[name][a][q] for a in res[name] if q in res[name][a]]))
                for q in qk
                if any(q in res[name][a] for a in res[name])
            ]
            be_q = [
                float(np.mean([res[name][a][q] for a in BIG if q in res[name].get(a, {})]))
                for q in qk
                if any(q in res[name].get(a, {}) for a in BIG)
            ]
            if len(per_q) < 1:
                continue
            b = moving_block_bootstrap(per_q, block=1)
            e = (
                moving_block_bootstrap(be_q, block=1)
                if len(be_q) > 1
                else {"mean": float(np.mean(be_q)) if be_q else float("nan"), "lo": float("nan"), "hi": float("nan"), "mde": float("nan")}
            )
            print(
                f"  {name:<28} {wn:<8} quarters {len(per_q):>2}: basket {pp(b['mean'])} CI [{pp(b['lo'])}, {pp(b['hi'])}] MDE {100 * b['mde']:.2f}"
                f" | BTC+ETH {pp(e['mean'])} CI [{pp(e['lo'])}, {pp(e['hi'])}] MDE {100 * e['mde']:.2f}"
            )
    print(f"  liquidations of B over all asset-quarters: {dict(liq)}")
    print(f"  of which BTC/ETH: {{{', '.join(f'{k[0]}|{k[1]}: {v}' for k, v in liq_big.items() if v)}}}")
    n_aq = sum(len(v) for v in res["primary m=0"].values())
    print(f"  asset-quarters simulated: {n_aq}")
    print("\nper asset, primary m=0, main window (pp/yr): A-B net | gross funding A-B")
    for a in sorted(res["primary m=0"]):
        x = [v for q, v in res["primary m=0"][a].items() if in_win(q, MAIN)]
        if x:
            print(f"  {a:<6} q {len(x):>2} A-B {pp(float(np.mean(x)))}  gross {pp(float(np.mean(a_minus_b_naive[a][: len(x)])))}")
    tick = json.loads((cache / "ticker24_dapi.json").read_text())
    print("\ncapacity: COIN-M 24h volume now (USD notional = contracts x face; face 100 USD BTC, 10 USD others)")
    for t in sorted(tick, key=lambda t: -float(t.get("volume", 0)) * (100 if t["symbol"].startswith("BTCUSD_") else 10)):
        if t["symbol"].endswith("_PERP"):
            face = 100 if t["symbol"].startswith("BTCUSD_") else 10
            print(f"  {t['symbol']:<16} {float(t['volume']) * face / 1e6:>9.1f} M USD")
    print(f"rss {rss_mb():.0f} MB")


# ----------------------------------------------------------------------------- artefact check: funding from premium


PREM_ASSETS = ("BTC", "ETH", "XRP", "BCH", "TRX", "DOGE")
PREM_WIN = (ms(2025, 4, 1), TRAIN_END)  # training data only


async def fetch_premium(cache: Path) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    try:
        for a in PREM_ASSETS:
            for venue in ("dapi", "fapi"):
                p = cache / "premium" / f"{venue}_{sym(venue, a)}.json"
                if p.exists():
                    continue
                ep = "dapiPublicGetPremiumIndexKlines" if venue == "dapi" else "fapiPublicGetPremiumIndexKlines"
                out: dict[int, float] = {}
                cur = PREM_WIN[0]
                while cur < PREM_WIN[1]:
                    params = {
                        "symbol": sym(venue, a),
                        "interval": "1h",
                        "startTime": cur,
                        "endTime": min(PREM_WIN[1], cur + 60 * DAY_MS) - 1,
                        "limit": 1500,
                    }
                    r = (await rt.exchanges.public_get("binance", ep, params)).data
                    await asyncio.sleep(0.3)
                    out.update({int(k[0]): float(k[4]) for k in r})
                    cur += 60 * DAY_MS
                write_json(p, sorted(out.items()))
                print(f"  premium {venue} {a}: {len(out)} hours", flush=True)
    finally:
        await rt.aclose()


def premium_check(args: argparse.Namespace) -> None:
    """Rebuild each venue's 8 h funding as P + clamp(0.01 % - P, +-0.05 %), P = 1..8-weighted mean of the hourly premium
    closes in the bucket. Same formula fitting both venues -> the gap is in the premiums (books), not in the formula."""
    cache: Path = args.cache_dir.expanduser()
    asyncio.run(fetch_premium(cache))
    w = np.arange(1, 9, dtype=float)
    print(f"premium reconstruction {ymd(PREM_WIN[0])}..{ymd(PREM_WIN[1])} (hourly closes, weights 1..8), per 8 h in bp")
    print(
        f"{'asset':<6}{'venue':<6}{'n':>6}{'mean P':>8}{'mean f':>8}{'mean f^':>8}{'MAE':>7}{'corr':>7}{'at anchor':>10}{'f^ anchor':>10}{'P<-4bp':>8}"
    )
    for a in PREM_ASSETS:
        for venue in ("dapi", "fapi"):
            pr = dict((int(t), v) for t, v in json.loads((cache / "premium" / f"{venue}_{sym(venue, a)}.json").read_text()))
            f = bucket_8h(load_rows(cache, venue, a))
            got = []
            for b, rate in f.items():
                if not in_win(b, PREM_WIN):
                    continue
                hrs = [pr.get(b - (8 - k) * HOUR_MS) for k in range(8)]  # hourly klines opening b-8h .. b-1h
                if any(h is None for h in hrs):
                    continue
                pm = float(np.dot(w, hrs) / w.sum())
                fh = pm + min(max(ANCHOR - pm, -0.0005), 0.0005)
                got.append((pm, rate, fh))
            g = np.array(got)
            print(
                f"{a:<6}{venue:<6}{len(g):>6}{1e4 * g[:, 0].mean():>8.3f}{1e4 * g[:, 1].mean():>8.3f}{1e4 * g[:, 2].mean():>8.3f}"
                f"{1e4 * np.abs(g[:, 1] - g[:, 2]).mean():>7.3f}{np.corrcoef(g[:, 1], g[:, 2])[0, 1]:>7.3f}"
                f"{np.mean(np.abs(g[:, 1] - ANCHOR) < 1e-9):>10.2f}{np.mean(np.abs(g[:, 2] - ANCHOR) < 1e-9):>10.2f}{np.mean(g[:, 0] < -0.0004):>8.2f}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=("fetch", "step0", "step1", "step2", "premium"))
    ap.add_argument("--cache-dir", type=Path, default=Path("~/.cache/tradedesk-agents/coinm_funding"))
    ap.add_argument("--sample", action="store_true", help="fetch: only two assets (timing)")
    args = ap.parse_args()
    if args.stage == "fetch":
        asyncio.run(fetch(args))
    else:
        {"step0": step0, "step1": step1, "step2": step2, "premium": premium_check}[args.stage](args)


if __name__ == "__main__":
    main()
