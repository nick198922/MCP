"""Extreme pump fade: short a mature Binance USDT-M perp after a >= +30 % week, BTC-beta hedged, hold 7 days.

Idea card: docs/ideas/IDEA-20261025-extreme-pump-fade-short.md (sections "Тріаж критика", "Попередня реєстрація").
Pure functions: src/tradedesk/analytics/pump_fade.py. No network: reads the per-file cache of
scripts/verify_funding_crowding.py (data.binance.vision daily klines + funding, delisted symbols included).

Stages:
    stage0   data checks, event counts, month clusters, ICC, SD, MDE -- no means; writes <out-dir>/stage0.json
    full     the registered test (H1-H4, robustness, exploratory grid, per-asset); refuses unless stage0 passed

    uv run python scripts/verify_pump_fade.py stage0 --cache-dir ~/.cache/tradedesk-agents/funding_crowding \
        --out-dir ~/.cache/tradedesk-agents/pump_fade --out ~/.cache/tradedesk-agents/pump_fade/stage0.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_funding_crowding as fc  # noqa: E402  (cache loaders: eligible / load_all / SymData)

from tradedesk.analytics.funding_crowding import LegWeek, leg_week, liquidated  # noqa: E402
from tradedesk.analytics.funding_spread import normalize_asset  # noqa: E402
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci, cluster_bootstrap_p  # noqa: E402
from tradedesk.analytics.listing_drift import DAY_MS, cr1_se, design_effect, holm, icc_anova, mde  # noqa: E402
from tradedesk.analytics.per_asset import asset_stats, heterogeneity, permutation_q, persistence  # noqa: E402
from tradedesk.analytics.pump_fade import (  # noqa: E402
    ShortTrade,
    btc_regime,
    capped_weights,
    first_breach,
    first_events,
    max_concurrent,
    placebo_members,
    pre_pump_beta,
    short_trade,
    universe_member,
)

CARD = Path(__file__).resolve().parent.parent / "docs" / "ideas" / "IDEA-20261025-extreme-pump-fade-short.md"


def dix(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp()) // 86400


FIRST, LAST = dix(2022, 1, 1), dix(2026, 9, 23)
END = dix(2026, 9, 30)  # last day in the cache
SPLIT = dix(2024, 1, 1)
BTC = fc.BTC
MAIN_THR, HOLD = 0.30, 7
GATE_MDE, GATE_N, GATE_MONTHS = 0.035, 150, 20
COST_COIN, COST_BTC = 0.0025, 0.0010
SEED = 20261025
NB = 10_000


def ymd(d: int) -> str:
    return fc.ymd(d)


def month(d: int) -> str:
    return ymd(d)[:7]


def rss() -> str:
    return f"rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"


def reg_hash() -> str:
    txt = CARD.read_text(encoding="utf-8")
    a = txt.index("## Попередня реєстрація")
    b = txt.find("**Крок 0, факт", a)
    b = b if b > 0 else txt.index("## Вердикт", a)
    return hashlib.sha256(txt[a:b].encode()).hexdigest()


# ---------------------------------------------------------------- panel


@dataclass
class Panel:
    data: dict[str, fc.SymData]
    uni: dict[int, dict[str, float]]  # signal day -> {symbol: r7} (deduplicated by asset)
    last_bar: dict[str, int]
    status: dict[str, str]
    reasons: Counter[str]


def build_panel(cache: Path) -> Panel:
    t0 = time.time()
    data = {s: d for s, d in fc.load_all(cache).items() if d.bars.close}  # symbols whose rows all had zero volume drop out
    print(f"loaded {len(data)} symbols in {time.time() - t0:.0f}s; {rss()}", flush=True)
    info = fc.rd(cache / "exchange_info.json")["info"]
    status = {s: (info.get(s) or {}).get("status") or "REMOVED" for s in data}
    last_bar = {s: max(d.bars.close) for s, d in data.items()}
    raw: dict[int, dict[str, tuple[float, float]]] = defaultdict(dict)
    reasons: Counter[str] = Counter()
    for s, d in data.items():
        if s == BTC:
            continue
        for day in d.bars.close:
            if FIRST <= day <= LAST:
                r, mq, why = universe_member(d.bars, day, d.starts)
                reasons[why] += 1
                if r is not None and mq is not None:
                    raw[day][s] = (r, mq)
    uni: dict[int, dict[str, float]] = {}
    for day, m in raw.items():
        best: dict[str, tuple[str, float]] = {}
        for s, (_, mq) in m.items():
            a = normalize_asset(s[:-4], "binanceusdm")[0]
            if a not in best or mq > best[a][1]:
                best[a] = (s, mq)
        reasons["dup_asset"] += len(m) - len(best)
        uni[day] = {s: m[s][0] for s, _ in best.values()}
    print(f"panel built in {time.time() - t0:.0f}s; signal days {len(uni)}; {rss()}", flush=True)
    return Panel(data, uni, last_bar, status, reasons)


@dataclass
class Event:
    sym: str
    day: int
    r7: float
    beta: float
    coin: LegWeek
    btc: LegWeek
    trade: ShortTrade
    delist: bool  # excluded from the registered cell (last bar within the holding week)

    @property
    def month(self) -> str:
        return month(self.day)

    @property
    def asset(self) -> str:
        return normalize_asset(self.sym[:-4], "binanceusdm")[0]


def legs(p: Panel, sym: str, day: int, hold: int) -> tuple[LegWeek, LegWeek] | None:
    d, b = p.data[sym], p.data[BTC]
    t_ms = (day + 1) * DAY_MS
    coin = leg_week(sym, d.bars, d.fslice(t_ms - 2 * DAY_MS, t_ms + (hold + 2) * DAY_MS), day + 1, hold=hold)
    if coin is None:
        return None
    h = coin.exit - day
    btc = leg_week(BTC, b.bars, b.fslice(t_ms - 2 * DAY_MS, t_ms + (h + 2) * DAY_MS), day + 1, hold=h)
    return None if btc is None else (coin, btc)


def make_events(p: Panel, thr: float, hold: int, *, cost_coin: float = COST_COIN, hedge: bool = True) -> tuple[list[Event], Counter[str]]:
    trig: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for day, m in p.uni.items():
        for s, r in m.items():
            if r >= thr:
                trig[s].append((day, r))
    out: list[Event] = []
    why: Counter[str] = Counter()
    btc = p.data[BTC].bars
    for s, tr in trig.items():
        r7 = dict(tr)
        for day in first_events(tr, thr):
            why["events_raw"] += 1
            delist = p.last_bar[s] < END and p.last_bar[s] <= day + hold
            beta = pre_pump_beta(p.data[s].bars, btc, day)
            if beta is None:
                why["no_beta"] += 1
                continue
            lg = legs(p, s, day, hold)
            if lg is None:
                why["no_entry"] += 1
                continue
            coin, bl = lg
            t = short_trade(coin, bl if hedge else None, beta, cost_coin=cost_coin, cost_btc=COST_BTC)
            out.append(Event(s, day, r7[day], beta, coin, bl, t, delist))
            why["delist_excluded" if delist else "kept"] += 1
    out.sort(key=lambda e: (e.day, e.sym))
    return out, why


def kept(evs: list[Event]) -> list[Event]:
    return [e for e in evs if not e.delist]


# ---------------------------------------------------------------- statistics


def power(vals: list[float], cl: list[str]) -> dict[str, float]:
    n = len(vals)
    sd = statistics.stdev(vals) if n > 1 else float("nan")
    icc = icc_anova(vals, cl)
    deff = design_effect(cl, icc)
    m_icc = mde(sd, n, deff)
    m_cr1 = 2.8 * cr1_se(vals, cl)
    return {
        "n": n,
        "months": len(set(cl)),
        "sd": sd,
        "icc": icc,
        "deff": deff,
        "mde_icc": m_icc,
        "mde_cr1": m_cr1,
        "mde": max(m_icc, m_cr1),
    }


def summ(vals: list[float], cl: list[str]) -> dict[str, Any]:
    n = len(vals)
    if n < 2 or len(set(cl)) < 2:
        return {"n": n, "mean": statistics.fmean(vals) if vals else float("nan"), "ci": None, "p": None, "months": len(set(cl))}
    ci = cluster_bootstrap_ci(vals, cl, n_boot=NB, seed=SEED)
    p = cluster_bootstrap_p(vals, cl, n_boot=NB, seed=SEED)
    return {"n": n, "mean": statistics.fmean(vals), "ci": ci, "p": p, "months": len(set(cl)), "median": statistics.median(vals)}


def pp(x: float | None) -> str:
    return "nan" if x is None or not math.isfinite(x) else f"{x * 100:+.2f}"


def line(label: str, s: dict[str, Any]) -> str:
    ci = s.get("ci")
    cis = f"[{pp(ci[0])}; {pp(ci[1])}]" if ci else "[-]"
    p = s.get("p")
    return (
        f"  {label:<44} n {s['n']:>5}  mo {s['months']:>3}  mean {pp(s['mean']):>7} pp  CI {cis:<20} p1 {p if p is None else round(p, 4)}"
    )


# ---------------------------------------------------------------- stage 0


def sanity(p: Panel) -> None:
    btc = p.data[BTC].bars
    print(f"check BTCUSDT close 2024-12-31 = {btc.close.get(dix(2024, 12, 31))} (listing-drift / funding-crowding reports: 93,548.9)")
    iv: Counter[float] = Counter()
    for d in p.data.values():
        for r in d.funding:
            iv[r[1]] += 1
    print(f"funding interval hours (settlement counts): {dict(sorted(iv.items()))}")
    # delisted symbols: last close vs the median close of the 30 days before it, and the last day's turnover
    rows = []
    for s, d in p.data.items():
        lb = p.last_bar[s]
        if lb >= END or s == BTC:
            continue
        prev = [d.bars.close[x] for x in range(lb - 30, lb) if x in d.bars.close]
        if len(prev) < 10:
            continue
        rows.append((d.bars.close[lb] / statistics.median(prev) - 1.0, s, ymd(lb), d.bars.qv.get(lb, 0.0)))
    rows.sort()
    q = [r[0] for r in rows]
    print(
        f"delisted symbols with >= 10 bars in the 30 days before the last bar: {len(rows)}; last close / median(30d) - 1: "
        f"p5 {pp(q[len(q) // 20])}, median {pp(statistics.median(q))}, p95 {pp(q[-len(q) // 20 - 1])} pp; "
        f"|move| > 50 %: {sum(abs(x) > 0.5 for x in q)}"
    )
    print("  extremes:", [(s, dd, f"{x:+.0%}", f"qv {v / 1e6:.2f}M") for x, s, dd, v in rows[:4] + rows[-4:]])
    st = Counter(p.status[s] for s in p.data)
    print(f"symbols by today's status: {dict(st)}")
    print(f"universe reasons (symbol-days in period): {dict(p.reasons)}")
    ns = [len(m) for m in p.uni.values()]
    print(f"universe size per day: min {min(ns)}, median {statistics.median(ns)}, max {max(ns)}")


def stage0(p: Panel, out_dir: Path) -> None:
    sanity(p)
    gate: dict[str, Any] = {}
    for thr in (0.30, 0.50, 0.80):
        evs, why = make_events(p, thr, HOLD)
        k = kept(evs)
        vals = [e.trade.net for e in k]
        cl = [e.month for e in k]
        pw = power(vals, cl)
        sizes = Counter(cl)
        by_year = Counter(ymd(e.day)[:4] for e in k)
        stat = Counter(p.status[e.sym] for e in k)
        holes = sum(e.coin.hold_gaps > 0 for e in k)
        trunc = sum(e.coin.truncated for e in k)
        tag = "MAIN" if thr == MAIN_THR else "exploratory"
        print(f"\n== threshold +{thr:.0%} ({tag}), hold {HOLD} d: {dict(why)}")
        print(f"  n {pw['n']}, months {pw['months']}, events per year {dict(sorted(by_year.items()))}")
        print(
            f"  month cluster size: max {max(sizes.values())}, median {statistics.median(sizes.values())}, sum m^2/n {sum(v * v for v in sizes.values()) / len(cl):.1f}"
        )
        print(f"  today's status of event symbols: {dict(stat)}; funding holes while held {holes}; truncated legs {trunc}")
        print(
            f"  SD {pw['sd'] * 100:.1f} pp, ICC {pw['icc']:.3f}, deff {pw['deff']:.2f}, MDE(ICC) {pw['mde_icc'] * 100:.2f} pp, MDE(CR1) {pw['mde_cr1'] * 100:.2f} pp"
        )
        if thr == MAIN_THR:
            ok = pw["mde"] <= GATE_MDE and pw["n"] >= GATE_N and pw["months"] >= GATE_MONTHS
            gate = {**pw, "passed": ok}
            print(
                f"  GATE: MDE {pw['mde'] * 100:.2f} pp <= {GATE_MDE * 100:.1f}, n >= {GATE_N}, months >= {GATE_MONTHS}: {'PASSED' if ok else 'FAILED'}"
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "registration_sha256": reg_hash(), "gate": gate}
    (out_dir / "stage0.json").write_text(json.dumps(rec, indent=1))
    print(f"\nstage0 written: {out_dir / 'stage0.json'}; registration sha256 {rec['registration_sha256'][:16]}; {rss()}")


# ---------------------------------------------------------------- full


def placebo_by_day(
    p: Panel, days: set[int], hold: int, cost_coin: float, events_by_day: dict[int, set[str]]
) -> dict[int, tuple[float, int]]:
    btc = p.data[BTC].bars
    out: dict[int, tuple[float, int]] = {}
    for day in sorted(days):
        mem = placebo_members(p.uni.get(day, {}), exclude=events_by_day.get(day, ()))
        vals = []
        for s in mem:
            if p.last_bar[s] < END and p.last_bar[s] <= day + hold:
                continue
            beta = pre_pump_beta(p.data[s].bars, btc, day)
            lg = legs(p, s, day, hold) if beta is not None else None
            if lg is None or beta is None:
                continue
            vals.append(short_trade(lg[0], lg[1], beta, cost_coin=cost_coin, cost_btc=COST_BTC).net)
        if vals:
            out[day] = (statistics.fmean(vals), len(vals))
    return out


def capped_net(p: Panel, e: Event, lev: float, hold: int) -> float:
    """Net with an isolated-margin liquidation at ``lev`` (trigger up >= 1/lev - 2.5 % mm; loss = margin)."""
    if not liquidated(e.coin, -1, lev):
        return e.trade.net
    b = first_breach(p.data[e.sym].bars, e.day + 1, e.coin.exit, 1.0 / lev - 0.025)
    ld = b[0] if b else e.coin.exit
    lg = legs(p, e.sym, e.day, ld - e.day)
    f = lg[0].funding_long if lg else e.coin.funding_long
    return short_trade(e.coin, e.btc, e.beta, coin_r=1.0 / lev, coin_funding=f, cost_coin=COST_COIN, cost_btc=COST_BTC).net


def stop_net(p: Panel, e: Event, level: float) -> tuple[float, bool]:
    b = first_breach(p.data[e.sym].bars, e.day + 1, e.coin.exit, level)
    if b is None:
        return e.trade.net, False
    lg = legs(p, e.sym, e.day, b[0] - e.day)
    f = lg[0].funding_long if lg else e.coin.funding_long
    return short_trade(e.coin, e.btc, e.beta, coin_r=b[1], coin_funding=f, cost_coin=COST_COIN, cost_btc=COST_BTC).net, True


def stage_full(p: Panel, out_dir: Path, detail: dict[str, Any]) -> None:
    s0 = json.loads((out_dir / "stage0.json").read_text())
    if not s0.get("gate", {}).get("passed"):
        print("stage0 gate did not pass: the registered test is not run (verdict inconclusive, forward).")
        return
    print(f"stage0 at {s0['at']}, registration sha256 {s0['registration_sha256'][:16]}, now {reg_hash()[:16]} (must match)")
    evs_all, why = make_events(p, MAIN_THR, HOLD)
    ev = kept(evs_all)
    cl = [e.month for e in ev]
    net = [e.trade.net for e in ev]
    print(f"\n== MAIN +30 %, hold 7, BTC-beta hedge, costs {COST_COIN:.2%} + beta x {COST_BTC:.2%}: {dict(why)}")
    print(f"period of signal days: {ymd(ev[0].day)} .. {ymd(ev[-1].day)}")
    res: dict[str, Any] = {}
    res["H1_net"] = summ(net, cl)
    res["price"] = summ([e.trade.price for e in ev], cl)
    res["price_coin"] = summ([e.trade.price_coin for e in ev], cl)
    res["hedge"] = summ([e.trade.hedge for e in ev], cl)
    res["funding"] = summ([e.trade.funding for e in ev], cl)
    res["funding_coin"] = summ([e.trade.funding_coin for e in ev], cl)
    res["cost"] = summ([e.trade.cost for e in ev], cl)
    for k, v in res.items():
        print(line(k, v))
    print(
        f"  beta: median {statistics.median(e.beta for e in ev):.2f}; clipped at 0: {sum(e.beta == 0 for e in ev)}, at 3: {sum(e.beta == 3 for e in ev)}"
    )
    qn = statistics.quantiles(net, n=20)
    print(
        f"  r7 at signal: median {pp(statistics.median(e.r7 for e in ev))} pp; net quantiles p5/p25/p50/p75/p95: {[pp(qn[i]) for i in (0, 4, 9, 14, 18)]}"
    )

    # H3 placebo
    days = {e.day for e in ev}
    by_day: dict[int, set[str]] = defaultdict(set)
    for e in evs_all:
        by_day[e.day].add(e.sym)
    plc = placebo_by_day(p, days, HOLD, COST_COIN, by_day)
    pe = [e for e in ev if e.day in plc]
    diff = [e.trade.net - plc[e.day][0] for e in pe]
    res["placebo"] = summ([plc[e.day][0] for e in pe], [e.month for e in pe])
    res["H3_diff"] = summ(diff, [e.month for e in pe])
    print(line("placebo (40-60 pct r7, same day)", res["placebo"]))
    print(line("H3 event - placebo", res["H3_diff"]))
    print(f"  placebo members per day: median {statistics.median(v[1] for v in plc.values())}; events without placebo {len(ev) - len(pe)}")

    # H4 tails
    up = [e.coin.up for e in ev]
    sh33, sh100 = sum(u >= 1 / 3 for u in up) / len(up), sum(u >= 1.0 for u in up) / len(up)
    liq3 = [capped_net(p, e, 3.0, HOLD) for e in ev]
    liq2 = [capped_net(p, e, 2.0, HOLD) for e in ev]
    res["H4_liq3"], res["H4_liq2"] = summ(liq3, cl), summ(liq2, cl)
    print(
        f"\nH4: share up >= +33 %: {sh33:.1%}; >= +100 %: {sh100:.1%}; liquidated at 3x: {sum(liquidated(e.coin, -1, 3.0) for e in ev)}, at 2x: {sum(liquidated(e.coin, -1, 2.0) for e in ev)}"
    )
    print(line("H4 net with 3x liquidation", res["H4_liq3"]))
    print(line("H4 net with 2x liquidation", res["H4_liq2"]))
    stops = [stop_net(p, e, 0.35) for e in ev]
    res["stop35"] = summ([s[0] for s in stops], cl)
    print(line(f"stop +35 % (hit {sum(s[1] for s in stops)})", res["stop35"]))
    print(
        f"  MAE (max up over hold): median {pp(statistics.median(up))}, p90 {pp(statistics.quantiles(up, n=10)[-1])}, max {pp(max(up))} pp"
    )
    worst = sorted(ev, key=lambda e: e.trade.net)[:5]
    print("  worst events:", [(e.sym, ymd(e.day), pp(e.trade.net), f"up {pp(e.coin.up)}") for e in worst])
    msum: dict[str, list[float]] = defaultdict(list)
    for e in ev:
        msum[e.month].append(e.trade.net)
    wm = sorted(msum.items(), key=lambda kv: sum(kv[1]))[:3]
    print("  worst months (sum of net over events, n, mean):", [(m, pp(sum(v)), len(v), pp(statistics.fmean(v))) for m, v in wm])
    wmm = sorted(msum.items(), key=lambda kv: statistics.fmean(kv[1]))[:3]
    print("  worst months by mean:", [(m, pp(statistics.fmean(v)), len(v)) for m, v in wmm])

    # robustness
    print("\n== robustness (descriptive)")
    halves = {"2022-2023": [e for e in ev if e.day < SPLIT], "2024-2026": [e for e in ev if e.day >= SPLIT]}
    for k, sub in halves.items():
        res[f"half_{k}"] = summ([e.trade.net for e in sub], [e.month for e in sub])
        print(line(f"half {k}", res[f"half_{k}"]))
    btc = p.data[BTC].bars
    for rg, lab in ((1, "BTC 30d > +10 %"), (0, "BTC 30d within +-10 %"), (-1, "BTC 30d < -10 %")):
        sub = [e for e in ev if btc_regime(btc, e.day) == rg]
        if len(sub) >= 2:
            print(line(lab, summ([e.trade.net for e in sub], [e.month for e in sub])))
    top = sorted(ev, key=lambda e: -e.trade.net)[:5]
    print("  top 5 events:", [(e.sym, ymd(e.day), pp(e.trade.net)) for e in top])
    rest = [e for e in ev if e not in top]
    res["no_top5"] = summ([e.trade.net for e in rest], [e.month for e in rest])
    print(line("without top 5 events", res["no_top5"]))
    for c in (0.005, 0.010):
        v = [e.trade.net - (c - COST_COIN) for e in ev]
        res[f"cost_{c}"] = summ(v, cl)
        print(line(f"cost stress coin {c:.2%} round trip", res[f"cost_{c}"]))
    nh = [e for e in ev if e.coin.hold_gaps == 0]
    print(line("without funding-hole events", summ([e.trade.net for e in nh], [e.month for e in nh])))
    dl = [e for e in evs_all if e.delist]
    print(line(f"with delisting-week events (+{len(dl)})", summ([e.trade.net for e in evs_all], [e.month for e in evs_all])))
    if dl:
        print("  delisting-week events:", [(e.sym, ymd(e.day), pp(e.trade.net)) for e in dl[:10]])
    d30 = [e for e in ev if not (p.last_bar[e.sym] < END and p.last_bar[e.sym] <= e.day + 30)]
    print(
        line(
            f"exploratory (added after results): no delisting within 30 d (-{len(ev) - len(d30)})",
            summ([e.trade.net for e in d30], [e.month for e in d30]),
        )
    )
    nf = [e for e in ev if e.trade.funding_coin < 0]
    print(
        f"  events where the short paid funding: {len(nf)} ({len(nf) / len(ev):.0%}); funding_coin quantiles p5/p50/p95: {pp(statistics.quantiles([e.trade.funding_coin for e in ev], n=20)[0])} / {pp(statistics.median(e.trade.funding_coin for e in ev))} / {pp(statistics.quantiles([e.trade.funding_coin for e in ev], n=20)[-1])} pp"
    )

    # capital
    ents = [e.day + 1 for e in ev]
    w = capped_weights(ents, HOLD)
    years = (ev[-1].day - ev[0].day + HOLD) / 365.25
    cap_ret = sum(wi * e.trade.net for wi, e in zip(w, ev, strict=True)) / years
    full_ret = sum(0.03 * e.trade.net for e in ev) / years
    mw: dict[str, float] = defaultdict(float)
    for wi, e in zip(w, ev, strict=True):
        mw[e.month] += wi * e.trade.net
    print(
        f"\ncapital: max concurrent {max_concurrent(ents, HOLD)}; events scaled by the 15 % cap {sum(x < 0.03 for x in w)}; "
        f"return on capital per year (simple sum, 3 %/event, cap 15 %): {pp(cap_ret)} % (uncapped {pp(full_ret)} %); worst month {pp(min(mw.values()))} %"
    )
    fw = [e.trade.net for wi, e in zip(w, ev, strict=True) if wi >= 0.03 - 1e-12]
    sc = [e.trade.net for wi, e in zip(w, ev, strict=True) if wi < 0.03 - 1e-12]
    print(
        f"  mean net of full-size (uncrowded) events {pp(statistics.fmean(fw))} pp (n {len(fw)}) vs scaled (crowded) {pp(statistics.fmean(sc))} pp (n {len(sc)}); capital-weighted mean {pp(sum(wi * e.trade.net for wi, e in zip(w, ev, strict=True)) / sum(w))} pp"
    )

    # exploratory grid
    print("\n== exploratory grid (Holm over one-sided p)")
    grid: list[tuple[str, dict[str, Any], list[float]]] = []
    for thr, hold, hedge, lab in (
        (0.50, 7, True, "+50 %, 7 d"),
        (0.80, 7, True, "+80 %, 7 d"),
        (0.30, 3, True, "+30 %, 3 d"),
        (0.30, 14, True, "+30 %, 14 d"),
        (0.30, 7, False, "+30 %, 7 d, no hedge"),
    ):
        g = kept(make_events(p, thr, hold, hedge=hedge)[0])
        v, c = [e.trade.net for e in g], [e.month for e in g]
        s = summ(v, c)
        s["price"] = statistics.fmean(e.trade.price for e in g)
        s["funding"] = statistics.fmean(e.trade.funding for e in g)
        s["sh33"] = sum(e.coin.up >= 1 / 3 for e in g) / len(g)
        grid.append((lab, s, v))
    adj = holm([s["p"] if s["p"] is not None else 1.0 for _, s, _ in grid])
    for (lab, s, _), a in zip(grid, adj, strict=True):
        print(line(lab, s) + f"  Holm {a:.4f}  price {pp(s['price'])} funding {pp(s['funding'])} share+33 {s['sh33']:.0%}")
        res[f"x_{lab}"] = {**s, "holm": a}

    # per-asset (5a)
    print("\n== per asset (5a)")
    vals, assets, cls = net, [e.asset for e in ev], cl
    st = sorted(asset_stats(vals, assets, min_n=3), key=lambda s: -s.n)
    print(
        f"  assets with events {len(set(assets))}; with >= 3 events {len(st)}; events per asset median {statistics.median(Counter(assets).values())}"
    )
    for s in st[:15]:
        print(f"    {s.asset:<10} n {s.n:>3}  mean {pp(s.mean):>7}  CI [{pp(s.ci[0]) if s.ci else '-'}; {pp(s.ci[1]) if s.ci else '-'}]")
    het = heterogeneity(vals, assets, min_n=3)
    pq = permutation_q(vals, assets, cls, min_n=3, n_perm=2000)
    print(
        f"  Cochran Q {het['q']:.1f} df {het['df']} p {het['p']}; I^2 {het['i2']:.2f}; tau {pp(het['tau'])} pp; permutation (within months) p {pq['p']}"
    )
    ism = [e.day < SPLIT for e in ev]
    per = persistence(vals, assets, cls, ism, min_n_is=2, top_frac=0.25)
    print(
        f"  persistence: k {per['k']} assets ranked in 2022-2023 (>= 2 events); top quarter IS {pp(per['top_is_mean'])} vs rest {pp(per['rest_is_mean'])}; "
        f"OOS 2024-2026 top {pp(per['top_oos_mean'])} (n {per['n_top_oos']}) vs rest {pp(per['rest_oos_mean'])} (n {per['n_rest_oos']}); "
        f"diff {pp(per['diff'])} CI {per['diff_ci']} p {per['diff_p']}; Spearman IS/OOS {per['rho']:.2f} (n {per['rho_n']})"
    )
    res["het"], res["perm_q"] = het, pq
    res["persist"] = {k: v for k, v in per.items() if k not in ("is_ranked",)}
    detail["results"] = res
    detail["events"] = [
        {
            "sym": e.sym,
            "t": ymd(e.day),
            "r7": e.r7,
            "beta": e.beta,
            "net": e.trade.net,
            "price": e.trade.price,
            "funding": e.trade.funding,
            "up": e.coin.up,
            "status": p.status[e.sym],
        }
        for e in ev
    ]
    print(f"\n{rss()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["stage0", "full"])
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "funding_crowding")
    ap.add_argument("--out-dir", type=Path, default=Path.home() / ".cache" / "tradedesk-agents" / "pump_fade")
    ap.add_argument("--out", type=Path, default=None, help="JSON file for the full stage's per-event detail and results")
    a = ap.parse_args()
    p = build_panel(a.cache_dir)
    if a.stage == "stage0":
        stage0(p, a.out_dir)
    else:
        detail: dict[str, Any] = {}
        stage_full(p, a.out_dir, detail)
        out = a.out or a.out_dir / "full.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(detail, indent=1, default=str))
        print(f"detail written: {out}")


if __name__ == "__main__":
    main()
