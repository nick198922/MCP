"""Alt downside beta and the "short alts / long BTC" basket while BTC is below its 50-day SMA (Binance USDT-M perps).

Idea card: docs/ideas/IDEA-20261015-alt-downside-beta-regime-basket.md (sections "Тріаж критика", "Попередня реєстрація").

Data: the data.binance.vision archive already cached by scripts/verify_funding_crowding.py (daily klines and funding
settlements of 678 eligible USDT-M symbols incl. SETTLING and removed ones, 2021-10 .. 2026-09; that script's ``meta``
and ``fetch`` stages fill the same layout through ``rt.binance_vision``). This runner only reads that cache
(``--data-dir``) and writes its own files to ``--cache-dir``. No network, no keys, no orders.

Stages:
    prereg   store the SHA-256 of the card's pre-registration section with a UTC timestamp (before stage0)
    stage0   data checks, universe and regime counts, SD and MDE of H1 (delta of slopes) and H2 (regime-week net) --
             no means, no coefficients
    full     the registered test (H1, H2, H3, permutations, robustness, stress, exploratory grid with Holm)

    uv run python scripts/verify_alt_downside_beta.py <stage> [--data-dir ...] [--cache-dir ...]
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import logging
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradedesk.analytics.alt_downside_beta import (
    BTC,
    active_net,
    below_sma,
    block_bootstrap_diff_ci,
    block_bootstrap_mean_ci,
    circular_shift_test,
    day_return,
    decompose,
    episodes,
    hedge_pick,
    index_return,
    mean_beta,
    negative_week,
    path_mae,
    trailing_index_downside_beta,
    two_slope,
    universe_at,
)
from tradedesk.analytics.funding_crowding import (
    Bars,
    Candidate,
    LegWeek,
    Pick,
    Week,
    WeekResult,
    leg_week,
    median_turnover,
    newey_west_se,
    run_start_map,
    simulate,
    trailing_beta,
)
from tradedesk.analytics.funding_spread import normalize_asset
from tradedesk.analytics.listing_drift import DAY_MS, holm, one_sided_p, symbol_eligible

ROOT = Path(__file__).resolve().parent.parent
CARD = ROOT / "docs" / "ideas" / "IDEA-20261015-alt-downside-beta-regime-basket.md"
FIRST = int(datetime(2022, 1, 2, tzinfo=UTC).timestamp()) // 86400  # Sunday
LAST = int(datetime(2026, 9, 20, tzinfo=UTC).timestamp()) // 86400  # last Sunday; its week ends 2026-09-26
SPLIT_H1 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp()) // 86400
FIRST_MONTH, LAST_MONTH = "2021-10", "2026-09"
N_MAIN, N_MAX = 30, 50
GATE_H1 = 0.15
GATE_H2 = 0.005
NW_DAILY, NW_WEEKLY = 5, 4
SEED = 7


def ymd(day: int) -> str:
    return datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d")


def rd(p: Path) -> Any:
    return json.loads(p.read_text())


def wr(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(p)


def pct(x: float, nd: int = 3) -> str:
    return f"{x * 100:+.{nd}f}%"


# ---------------------------------------------------------------- data


@dataclass
class Sym:
    bars: Bars
    funding: list[list[float]]
    times: list[int]
    starts: dict[int, int]

    def fslice(self, lo_ms: int, hi_ms: int) -> list[list[float]]:
        return self.funding[bisect.bisect_left(self.times, lo_ms) : bisect.bisect_left(self.times, hi_ms)]


def eligible(data_dir: Path) -> list[str]:
    syms, info = rd(data_dir / "symbols.json"), rd(data_dir / "exchange_info.json")["info"]
    return [s for s in syms if symbol_eligible(s, (info.get(s) or {}).get("contractType"), (info.get(s) or {}).get("underlyingType"))]


def months_of(data_dir: Path, sym: str) -> list[str]:
    p = data_dir / "listing" / f"{sym}.json"
    ms = rd(p) if p.exists() else None
    return [m for m in ms or [] if FIRST_MONTH <= m <= LAST_MONTH]


def load(data_dir: Path) -> dict[str, Sym]:
    out: dict[str, Sym] = {}
    for s in sorted(set(eligible(data_dir)) | {BTC}):
        rows: list[list[float]] = []
        fund: list[list[float]] = []
        for mo in months_of(data_dir, s):
            kp, fp = data_dir / "k1d" / s / f"{mo}.json", data_dir / "funding" / s / f"{mo}.json"
            if kp.exists() and (k := rd(kp)):
                rows.extend(k)
            if fp.exists() and (f := rd(fp)):
                fund.extend(f)
        if not rows:
            continue
        b = Bars.from_rows(rows)
        fund.sort(key=lambda r: r[0])
        out[s] = Sym(b, fund, [int(r[0]) for r in fund], run_start_map(b))
    return out


@dataclass
class Panel:
    data: dict[str, Sym]
    btc: Bars
    weeks: list[int]
    uni: dict[int, list[Candidate]]  # top-N_MAX per Sunday, largest turnover first
    legs: dict[int, dict[str, LegWeek]]  # members (top-N_MAX) and BTC, open t -> close t+6
    btc_cand: dict[int, Candidate]
    betas: dict[tuple[int, str], float | None]


def asset_map(bars: dict[str, Bars]) -> dict[str, str]:
    """Symbol -> asset for the one-symbol-per-asset rule. Symbols that ``normalize_asset`` puts on one asset but whose
    price levels on a common day disagree with their contract multipliers by more than x3 are different tokens (stage 0
    found 1000000BOBUSDT vs BOBUSDT: ratio 2.4 instead of 1e6) and keep their own symbol as the asset key."""
    norm = {s: normalize_asset(s[:-4], "binanceusdm") for s in bars}
    by: dict[str, list[str]] = {}
    for s, (a, _) in norm.items():
        by.setdefault(a, []).append(s)
    out = {s: a for s, (a, _) in norm.items()}
    for ss in by.values():
        if len(ss) < 2:
            continue
        common = set.intersection(*(set(bars[s].close) for s in ss))
        if not common:
            continue
        d = max(common)
        per_unit = [bars[s].close[d] / norm[s][1] for s in ss]
        if max(per_unit) / min(per_unit) > 3.0:
            for s in ss:
                out[s] = s
    return out


def build_panel(data_dir: Path) -> Panel:
    t0 = time.time()
    data = load(data_dir)
    print(f"loaded {len(data)} symbols in {time.time() - t0:.0f}s", flush=True)
    bars = {s: d.bars for s, d in data.items()}
    starts = {s: d.starts for s, d in data.items()}
    asset = asset_map(bars)
    btc = bars[BTC]
    weeks = list(range(FIRST, LAST + 1, 7))
    uni: dict[int, list[Candidate]] = {}
    legs: dict[int, dict[str, LegWeek]] = {}
    btc_cand: dict[int, Candidate] = {}
    betas: dict[tuple[int, str], float | None] = {}
    for t in weeks:
        u = universe_at(bars, t, n=N_MAX, asset_of=asset, starts=starts)
        uni[t] = u
        lg: dict[str, LegWeek] = {}
        t_ms = t * DAY_MS
        for s in [c.symbol for c in u] + [BTC]:
            d = data[s]
            x = leg_week(s, d.bars, d.fslice(t_ms - 2 * DAY_MS, t_ms + 9 * DAY_MS), t)
            if x is not None:
                lg[s] = x
        legs[t] = lg
        btc_cand[t] = Candidate(BTC, 0.0, median_turnover(btc, t) or 0.0, 9999)
        for c in u:
            betas[(t, c.symbol)] = trailing_beta(bars[c.symbol], btc, t)
    print(f"panel built in {time.time() - t0:.0f}s", flush=True)
    return Panel(data, btc, weeks, uni, legs, btc_cand, betas)


def members(p: Panel, t: int, n: int) -> list[str]:
    return [c.symbol for c in p.uni[t][:n]]


# ---------------------------------------------------------------- H1 series


def h1_daily(p: Panel, n: int) -> tuple[list[int], list[float], list[float], int]:
    """(days, r_alt, r_btc, member-days skipped) over every day of every week."""
    bars = {s: d.bars for s, d in p.data.items()}
    days, ya, xb = [], [], []
    skipped = 0
    for t in p.weeks:
        ms = members(p, t, n)
        for d in range(t, t + 7):
            r, k = index_return(ms, bars, d)
            rb = day_return(p.btc, d)
            skipped += len(ms) - k
            if r is not None and rb is not None:
                days.append(d)
                ya.append(r)
                xb.append(rb)
    return days, ya, xb, skipped


def h1_weekly(p: Panel, n: int) -> tuple[list[int], list[float], list[float]]:
    """Weekly close(t-1) -> close(t+6) returns; a member delisted mid-week carries its last close."""
    days, ya, xb = [], [], []
    for t in p.weeks:
        rs = []
        for s in members(p, t, n):
            b = p.data[s].bars
            e = b.close_on_or_before(t + 6, t - 1)
            if (t - 1) in b.close and e is not None:
                rs.append(b.close[e] / b.close[t - 1] - 1.0)
        if rs and (t - 1) in p.btc.close and (t + 6) in p.btc.close:
            days.append(t)
            ya.append(statistics.fmean(rs))
            xb.append(p.btc.close[t + 6] / p.btc.close[t - 1] - 1.0)
    return days, ya, xb


# ---------------------------------------------------------------- H2 basket


def basket_weeks(p: Panel, n: int) -> list[Week]:
    out = []
    for t in p.weeks:
        u = {c.symbol: c for c in p.uni[t][:n]}
        u[BTC] = p.btc_cand[t]
        out.append(Week(t, u, {s: x for s, x in p.legs[t].items() if s in u}))
    return out


def week_beta(p: Panel, wk: Week, n: int) -> float | None:
    return mean_beta([p.betas[(wk.day, s)] for s in members(p, wk.day, n) if s in wk.legs])


def make_choose(
    p: Panel, n: int, regime: Callable[[int], bool | None], beta_fn: Callable[[Week], float | None] | None = None
) -> Callable[[Week], Pick]:
    def choose(wk: Week) -> Pick:
        if not regime(wk.day):
            return Pick([], [])
        ms = [s for s in members(p, wk.day, n) if s in wk.legs]
        if BTC not in wk.legs:
            return Pick([], [])
        # registered: flat with fewer than 20 of 30 members; the exploratory sizes use the same 2/3 share
        return hedge_pick(ms, beta_fn(wk) if beta_fn else week_beta(p, wk, n), min_members=(2 * n) // 3)

    return choose


def reg_sma(p: Panel, w: int) -> Callable[[int], bool | None]:
    return lambda t: below_sma(p.btc, t, w)


def stats_line(label: str, xs: list[float], lag: int, unit: float = 100.0, suf: str = "pp/week") -> str:
    se = newey_west_se(xs, lag)
    return f"  {label:<46} n={len(xs):>4} SD={statistics.stdev(xs) * unit:7.3f} SE_NW={se * unit:6.3f} MDE={2.8 * se * unit:6.3f} {suf}"


# ---------------------------------------------------------------- stages


def stage_prereg(cache: Path) -> None:
    t = CARD.read_text()
    sec = t[t.index("## Попередня реєстрація") : t.index("## Вердикт")]
    p = cache / "prereg.json"
    if p.exists():
        old = rd(p)
        print(f"prereg already stored at {old['written_utc']}: sha256 {old['sha256']}; current {hashlib.sha256(sec.encode()).hexdigest()}")
        return
    wr(p, {"written_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "sha256": hashlib.sha256(sec.encode()).hexdigest()})
    print(rd(p))


def sanity(p: Panel, data_dir: Path) -> None:
    info = rd(data_dir / "exchange_info.json")["info"]
    d = int(datetime(2024, 12, 31, tzinfo=UTC).timestamp()) // 86400
    print(f"check BTCUSDT perp close 2024-12-31 = {p.btc.close.get(d)} (listing-drift / token-unlock / funding-crowding: 93548.9)")
    # ticker collisions: several eligible symbols on one asset -> compare price levels on a common day
    by_asset: dict[str, list[str]] = {}
    for s in p.data:
        by_asset.setdefault(normalize_asset(s[:-4], "binanceusdm")[0], []).append(s)
    multi = {a: ss for a, ss in by_asset.items() if len(ss) > 1}
    lines = []
    for a, ss in sorted(multi.items()):
        common = set.intersection(*(set(p.data[s].bars.close) for s in ss))
        if common:
            dd = max(common)
            lv = ", ".join(f"{s}={p.data[s].bars.close[dd]:.6g}" for s in ss)
            lines.append(f"{a} @{ymd(dd)}: {lv}")
        else:
            lines.append(f"{a}: {ss} never traded on the same day")
    print(f"assets with several symbols (one kept per week, larger turnover): {len(multi)}")
    for ln in lines:
        print(f"    {ln}")
    am = asset_map({s: d.bars for s, d in p.data.items()})
    print(
        f"    treated as different tokens (price levels disagree with multipliers): {sorted(s for ss in multi.values() for s in ss if am[s] == s)}"
    )
    # top-30 members: status today, big daily moves, TradFi leakage
    st: Counter[str] = Counter()
    delisted_syms: set[str] = set()
    jumps = []
    under: Counter[str] = Counter()
    seen: set[str] = set()
    for t in p.weeks:
        for s in members(p, t, N_MAIN):
            i = info.get(s) or {}
            status = i.get("status") or "REMOVED"
            st[status] += 1
            under[str(i.get("underlyingType"))] += 1
            if status != "TRADING":
                delisted_syms.add(s)
            if s in seen:
                continue
            seen.add(s)
    for s in seen:
        cl = p.data[s].bars.close
        for x in cl:
            if FIRST <= x <= LAST + 6 and x - 1 in cl:
                q = cl[x] / cl[x - 1]
                if q >= 3 or q <= 1 / 3:
                    jumps.append(f"{s} {ymd(x)} x{q:.2f}")
    tot = sum(st.values())
    print(
        f"top-{N_MAIN} member-weeks by today's status: {dict(st)}; delisted share {(tot - st['TRADING']) / tot:.1%}; "
        f"distinct members {len(seen)}, of them delisted today {len(delisted_syms)}: {sorted(delisted_syms)}"
    )
    print(f"top-{N_MAIN} member-weeks by underlyingType (None = not in today's exchangeInfo): {dict(under)}")
    print(f"day-over-day close moves >= x3 or <= x1/3 among top-{N_MAIN} members (whole history inside the period): {jumps}")
    iv: Counter[float] = Counter()
    for s in seen | {BTC}:
        for r in p.data[s].funding:
            iv[r[1]] += 1
    print(f"funding interval hours of top-{N_MAIN} members and BTC (settlement counts): {dict(sorted(iv.items()))}")


def coverage(p: Panel) -> None:
    ns = [len(p.uni[t]) for t in p.weeks]
    print(
        f"weeks {len(p.weeks)} ({ymd(p.weeks[0])} .. {ymd(p.weeks[-1])}); qualified universe (capped at {N_MAX}): min {min(ns)} median {statistics.median(ns)}"
    )
    print(f"weeks with < {N_MAIN} qualified: {sum(n < N_MAIN for n in ns)}; with < 50: {sum(n < 50 for n in ns)}")
    no_leg = sum(sum(1 for s in members(p, t, N_MAIN) if s not in p.legs[t]) for t in p.weeks)
    trunc = sum(sum(1 for s in members(p, t, N_MAIN) if s in p.legs[t] and p.legs[t][s].truncated) for t in p.weeks)
    gaps = sum(
        sum(1 for s in members(p, t, N_MAIN) if s in p.legs[t] and p.legs[t][s].hold_gaps and not p.legs[t][s].truncated) for t in p.weeks
    )
    bgaps = sum(1 for t in p.weeks if BTC in p.legs[t] and p.legs[t][BTC].hold_gaps)
    nobeta = sum(sum(1 for s in members(p, t, N_MAIN) if p.betas[(t, s)] is None) for t in p.weeks)
    print(
        f"top-{N_MAIN} member-weeks: without entry bar {no_leg}; truncated by delisting {trunc}; funding holes while held "
        f"(not truncated) {gaps}; BTC weeks with funding holes {bgaps}; members without trailing beta {nobeta}"
    )


def regime_info(p: Panel, w: int) -> tuple[list[bool], list[int]]:
    flags = [bool(below_sma(p.btc, t, w)) for t in p.weeks]
    return flags, episodes(flags)


def h2_series(p: Panel, n: int, regime: Callable[[int], bool | None], **kw: Any) -> tuple[list[WeekResult], list[tuple[int, float]]]:
    wks = basket_weeks(p, n)
    res = simulate(wks, make_choose(p, n, regime), **kw)
    return res, active_net(res)


def stage0(data_dir: Path, cache: Path) -> None:
    if not (cache / "prereg.json").exists():
        print("run the prereg stage first")
        return
    p = build_panel(data_dir)
    sanity(p, data_dir)
    coverage(p)
    flags, eps = regime_info(p, 50)
    print(
        f"regime BTC < SMA50 (close t-1): {sum(flags)} of {len(flags)} weeks; episodes {len(eps)}, lengths {eps}, "
        f"median {statistics.median(eps)} weeks"
    )
    days, ya, xb, skipped = h1_daily(p, N_MAIN)
    f = two_slope(ya, xb, lag=NW_DAILY)
    print()
    print("STAGE 0 (no means, no coefficients)")
    print(f"  H1 daily top-{N_MAIN}: n days {len(days)}, member-days skipped (no bar on d or d-1) {skipped}")
    print(f"  H1 SD r_alt {statistics.stdev(ya) * 100:.3f} pp/day, SD r_btc {statistics.stdev(xb) * 100:.3f} pp/day")
    print(f"  H1 SE(delta) NW lag {NW_DAILY}: {f.se_delta:.4f}; MDE(delta) {f.mde():.4f} (gate <= {GATE_H1})")
    _, act = h2_series(p, N_MAIN, reg_sma(p, 50))
    net = [x for _, x in act]
    print(stats_line("H2 regime-week net (per unit of alt-leg notional)", net, NW_WEEKLY))
    m2 = 2.8 * newey_west_se(net, NW_WEEKLY)
    g1, g2 = f.mde() <= GATE_H1, m2 <= GATE_H2
    print(f"GATE H1 MDE(delta) {f.mde():.4f} <= {GATE_H1}: {g1}")
    print(f"GATE H2 MDE {m2 * 100:.3f} pp/week <= {GATE_H2 * 100:.1f} pp: {g2}")
    wr(
        cache / "stage0.json",
        {"mde_h1": f.mde(), "mde_h2": m2, "gate_h1": g1, "gate_h2": g2, "n_days": len(days), "n_reg": len(net), "episodes": eps},
    )


def h1_report(label: str, ya: list[float], xb: list[float], lag: int, x_prev: list[float] | None = None) -> Any:
    f = two_slope(ya, xb, lag=lag, x_prev=x_prev)
    lo, hi = f.ci()
    print(
        f"  {label:<44} n={f.n:>4} b_dn {f.b_dn:.3f} (SE {f.se_b_dn:.3f}) b_up {f.b_up:.3f} (SE {f.se_b_up:.3f}) "
        f"delta {f.delta:+.3f} SE {f.se_delta:.3f} CI95 [{lo:+.3f}; {hi:+.3f}] a {f.a * 100:+.3f} pp/period"
    )
    return f


def h1_verdict(f: Any, halves: list[Any]) -> str:
    lo, hi = f.ci()
    if (f.delta < 0.1 and hi < 0.3) or (halves[0].delta * halves[1].delta < 0):
        return "X kill (a): no asymmetry of the claimed size (or sign flips across halves)"
    if lo > 0 and hi >= 0.3:
        return "CONFIRMED: lower CI > 0 and 0.3 not rejected"
    if lo > 0:
        return "DIRECTIONALLY RIGHT: asymmetry > 0 but smaller than 0.3"
    return "not determined"


def summarize(label: str, act: list[tuple[int, float]], *, block: int, boot: bool = True) -> dict[str, float]:
    xs = [x for _, x in act]
    m = statistics.fmean(xs)
    se = newey_west_se(xs, NW_WEEKLY)
    lo, hi = block_bootstrap_mean_ci(xs, block=block, n_boot=10_000 if boot else 2000, seed=SEED)
    t = m / se if se > 0 else float("nan")
    print(
        f"  {label:<52} n={len(xs):>3} net {pct(m)}/wk ({m * 52 * 100:+.1f}%/yr of active weeks) CI95 [{pct(lo)}; {pct(hi)}] t_NW {t:+.2f}"
    )
    return {"n": len(xs), "net": m, "lo": lo, "hi": hi, "t": t}


def components(res: list[WeekResult]) -> str:
    a = [r for r in res if r.n_side > 0]
    return (
        f"price {pct(statistics.fmean(r.price for r in a))} funding {pct(statistics.fmean(r.funding for r in a))} "
        f"cost {pct(sum(r.cost for r in res) / len(a))} turnover/active week {sum(r.turnover for r in res) / len(a):.2f}\n"
        # simulate's leg fields: long side = BTC per unit of its own notional, short side = alt leg per unit of its notional
        f"    funding by leg: short alt leg receives {pct(statistics.fmean(r.short_funding for r in a))}/wk of alt notional; "
        f"long BTC leg receives {pct(statistics.fmean(r.long_funding for r in a))}/wk of BTC notional"
    )


def stage_full(data_dir: Path, cache: Path) -> None:
    s0 = rd(cache / "stage0.json") if (cache / "stage0.json").exists() else None
    if not s0:
        print("stage0 not run: stop.")
        return
    p = build_panel(data_dir)
    bars = {s: d.bars for s, d in p.data.items()}
    flags, eps = regime_info(p, 50)
    L = max(4, int(statistics.median(eps)))
    print(f"regime weeks {sum(flags)}, episodes {len(eps)}, bootstrap block L = {L} weeks; gates: H1 {s0['gate_h1']}, H2 {s0['gate_h2']}")

    # ---------------- H1
    print()
    print(f"=== H1 (registered main spec): daily close-to-close, equal-weight top-{N_MAIN}, two slopes, NW lag {NW_DAILY} ===")
    days, ya, xb, _ = h1_daily(p, N_MAIN)
    f = h1_report(f"daily top-{N_MAIN}", ya, xb, NW_DAILY)
    halves = []
    for lab, sel in (("2022-01-02..2023-12-31", lambda d: d < SPLIT_H1), ("2024-01-01..2026-09-26", lambda d: d >= SPLIT_H1)):
        idx = [i for i, d in enumerate(days) if sel(d)]
        halves.append(h1_report(f"  half {lab}", [ya[i] for i in idx], [xb[i] for i in idx], NW_DAILY))
    print(f"  H1 VERDICT: {h1_verdict(f, halves)}")
    print()
    print("--- H1 robustness (secondary, no verdict) ---")
    wd, wa, wb = h1_weekly(p, N_MAIN)
    h1_report(f"weekly top-{N_MAIN} (close t-1 -> close t+6)", wa, wb, NW_WEEKLY)
    di = [i for i, d in enumerate(days) if day_return(p.btc, d - 1) is not None]
    h1_report(
        "Dimson, one BTC lag (sum of slopes)",
        [ya[i] for i in di],
        [xb[i] for i in di],
        NW_DAILY,
        x_prev=[day_return(p.btc, days[i] - 1) or 0.0 for i in di],
    )
    top5 = set(sorted(range(len(xb)), key=lambda i: -abs(xb[i]))[:5])
    print(f"    5 largest |r_btc| days: {[(ymd(days[i]), round(xb[i] * 100, 2)) for i in sorted(top5)]}")
    keep = [i for i in range(len(days)) if i not in top5]
    h1_report("without 5 largest |r_btc| days", [ya[i] for i in keep], [xb[i] for i in keep], NW_DAILY)
    nov = (int(datetime(2022, 11, 1, tzinfo=UTC).timestamp()) // 86400, int(datetime(2022, 12, 1, tzinfo=UTC).timestamp()) // 86400)
    keep = [i for i, d in enumerate(days) if not nov[0] <= d < nov[1]]
    h1_report("without November 2022 (FTX)", [ya[i] for i in keep], [xb[i] for i in keep], NW_DAILY)
    for n in (15, 50):
        _, a2, b2, _ = h1_daily(p, n)
        h1_report(f"daily top-{n}", a2, b2, NW_DAILY)
    # descriptive: conditional on the regime (not registered as a test; context for H2)
    rd_days = {t for t, fl in zip(p.weeks, flags, strict=True) if fl}
    ir = [i for i, d in enumerate(days) if d - (d - FIRST) % 7 in rd_days]
    nr = [i for i in range(len(days)) if i not in set(ir)]
    h1_report("(descriptive) days inside SMA50-regime weeks", [ya[i] for i in ir], [xb[i] for i in ir], NW_DAILY)
    h1_report("(descriptive) days outside the regime", [ya[i] for i in nr], [xb[i] for i in nr], NW_DAILY)

    # ---------------- H2
    print()
    print(f"=== H2 (registered main cell): SMA50 regime, top-{N_MAIN}, weekly, trailing 60d beta hedge; per unit of alt-leg notional ===")
    res, act = h2_series(p, N_MAIN, reg_sma(p, 50))
    S = summarize("regime basket net", act, block=L)
    print(f"    {components(res)}")
    a = [r for r in res if r.n_side > 0]
    bmean = statistics.fmean(week_beta(p, wk, N_MAIN) or 0.0 for wk in basket_weeks(p, N_MAIN) if wk.day in {r.day for r in a})
    print(
        f"    mean hedge beta in regime weeks {bmean:.3f}; return on capital at k x on both legs = net x k / (1 + beta): 2x {S['net'] * 2 / (1 + bmean) * 52:+.1%}/yr, 3x {S['net'] * 3 / (1 + bmean) * 52:+.1%}/yr of active weeks"
    )
    print(
        f"    MDE {2.8 * newey_west_se([x for _, x in act], NW_WEEKLY) * 100:.3f} pp/week (gate {GATE_H2 * 100:.1f}): passed {s0['gate_h2']}"
    )
    # always-on control and regime contrast
    res_all, act_all = h2_series(p, N_MAIN, lambda t: True)
    print("  control: the same basket held every week (no regime filter)")
    summarize("always-on basket net", act_all, block=L)
    print(f"    {components(res_all)}")
    days_all = [d for d, _ in act_all]
    fl_all = [bool(below_sma(p.btc, d, 50)) for d in days_all]
    x_all = [x for _, x in act_all]
    reg_m = statistics.fmean(x for x, fl in zip(x_all, fl_all, strict=True) if fl)
    non_m = statistics.fmean(x for x, fl in zip(x_all, fl_all, strict=True) if not fl)
    dlo, dhi = block_bootstrap_diff_ci(x_all, fl_all, block=L, seed=SEED)
    print(
        f"    always-on in regime weeks {pct(reg_m)}/wk (n={sum(fl_all)}); placebo outside (BTC >= SMA50) {pct(non_m)}/wk (n={len(fl_all) - sum(fl_all)})"
    )
    print(f"    regime - non-regime {pct(reg_m - non_m)}/wk CI95 [{pct(dlo)}; {pct(dhi)}] (block {L})")
    obs, p_shift, sh = circular_shift_test(x_all, fl_all)
    sh.sort()
    print(
        f"    circular shifts of the regime labels ({len(sh)}): observed {pct(obs)}, 95th pct {pct(sh[int(0.95 * len(sh))])}, p(one-sided) {p_shift:.4f}"
    )

    # ---------------- H3
    print()
    print("=== H3 decomposition of the price component (weekly two-slope OLS of the alt leg on BTC) ===")
    for lab, rr in (("regime weeks (H2)", a), ("always-on control", [r for r in res_all if r.n_side > 0])):
        ra, rb, bt = [], [], []
        for r in rr:
            lg = p.legs[r.day]
            ms = [s for s in members(p, r.day, N_MAIN) if s in lg]
            ra.append(statistics.fmean(lg[s].r for s in ms))
            rb.append(lg[BTC].r)
            wk = Week(r.day, {}, lg)
            bt.append(week_beta(p, wk, N_MAIN) or 0.0)
        h = decompose(ra, rb, bt)
        share = h.drift_share
        print(
            f"  {lab:<22} n={h.n} a {pct(h.a)} b_dn {h.b_dn:.3f} b_up {h.b_up:.3f} | price {pct(h.price)}/wk = intercept -a {pct(h.intercept)} "
            f"+ convexity {pct(h.convexity)} + BTC drift {pct(h.drift)}; drift share {'n/a (price <= 0)' if share is None else f'{share:.0%}'}; mean r_btc {pct(statistics.fmean(rb))}"
        )

    # ---------------- robustness of H2
    print()
    print("=== H2 robustness (registered) ===")
    xs = [x for _, x in act]
    half = len(act) // 2
    for lab, sub in (("first half of regime weeks", act[:half]), ("second half of regime weeks", act[half:])):
        print(f"  {lab} ({ymd(sub[0][0])}..{ymd(sub[-1][0])}): n={len(sub)} net {pct(statistics.fmean(x for _, x in sub))}/wk")
    srt = sorted(xs)
    print(
        f"  without the 5 best regime weeks: {pct(statistics.fmean(srt[:-5]))}/wk; without the 5 worst: {pct(statistics.fmean(srt[5:]))}/wk"
    )
    by_year: dict[str, list[float]] = {}
    for d, x in act:
        by_year.setdefault(ymd(d)[:4], []).append(x)
    print("  by year: " + "; ".join(f"{y} n={len(v)} {pct(statistics.fmean(v))}" for y, v in sorted(by_year.items())))
    ep_ret = []
    cur: list[float] = []
    prev_d = None
    for d, x in act:
        if prev_d is not None and d - prev_d != 7:
            ep_ret.append(cur)
            cur = []
        cur.append(x)
        prev_d = d
    if cur:
        ep_ret.append(cur)
    ep_sum = [sum(e) for e in ep_ret]
    print(
        f"  per episode (sum of weekly net): n={len(ep_sum)} mean {pct(statistics.fmean(ep_sum))}, positive {sum(v > 0 for v in ep_sum)}; {[round(v * 100, 1) for v in ep_sum]}"
    )
    print("  cost stress:")
    for lab, kw in (("fee 0.02 % (maker)", {"fee": 0.0002}), ("slippage x2", {"slip_mult": 2.0}), ("slippage x3", {"slip_mult": 3.0})):
        _, ac = h2_series(p, N_MAIN, reg_sma(p, 50), **kw)
        summarize(lab, ac, block=L, boot=False)

    # ---------------- tails and leg risk
    print()
    print("=== TAILS AND LEG RISK (regime basket) ===")
    worst = min(act, key=lambda z: z[1])
    print(f"  worst week {ymd(worst[0])} {pct(worst[1], 2)}; best week {pct(max(xs), 2)} (of alt-leg notional)")
    liq: Counter[str] = Counter()
    coin_weeks = 0
    ups = []
    worst_day = (0.0, "", 0)
    maes = []
    for r in a:
        lg = p.legs[r.day]
        ms = [s for s in members(p, r.day, N_MAIN) if s in lg]
        for s in ms:
            coin_weeks += 1
            ups.append(lg[s].up)
            for k in (2, 3):
                liq[f"{k}x"] += lg[s].up >= 1 / k - 0.025
            b = bars[s]
            for d in range(r.day, r.day + 7):
                if d in b.close:
                    prev_c = b.open[r.day] if d == r.day else b.close.get(d - 1)
                    if prev_c and b.close[d] / prev_c - 1 > worst_day[0]:
                        worst_day = (b.close[d] / prev_c - 1, s, d)
        m = path_mae(ms, bars, p.btc, week_beta(p, Week(r.day, {}, lg), N_MAIN) or 0.0, r.day)
        if m is not None:
            maes.append((m, r.day))
    ups.sort()
    print(
        f"  short-leg coin-weeks {coin_weeks}: max-high vs entry p95 {ups[int(0.95 * len(ups))]:.1%}, max {ups[-1]:.1%}; "
        f"isolated liquidation (up >= 1/k - 2.5 %): " + ", ".join(f"{k} {v} ({v / coin_weeks:.2%})" for k, v in sorted(liq.items()))
    )
    print(
        f"  worst single-coin day against the short leg: {worst_day[1]} {ymd(worst_day[2])} {worst_day[0]:+.1%} of that coin = {worst_day[0] / N_MAIN:+.2%} of alt-leg notional"
    )
    maes.sort()
    print(
        f"  worst intra-week drawdown of the hedged basket at daily closes: {pct(maes[0][0], 2)} ({ymd(maes[0][1])}); p05 {pct(maes[int(0.05 * len(maes))][0], 2)}"
    )
    held_gaps = sum(
        1
        for r in a
        for s in members(p, r.day, N_MAIN)
        if s in p.legs[r.day] and p.legs[r.day][s].hold_gaps and not p.legs[r.day][s].truncated
    )
    held_trunc = sum(1 for r in a for s in members(p, r.day, N_MAIN) if s in p.legs[r.day] and p.legs[r.day][s].truncated)
    print(f"  regime basket legs with funding holes while held (not delisting): {held_gaps}; legs closed early by delisting: {held_trunc}")

    # ---------------- exploratory
    print()
    print("=== EXPLORATORY (registered list; Holm across; cannot become the main cell) ===")
    expl: list[tuple[str, dict[str, float]]] = []
    for w in (100, 200):
        _, ac = h2_series(p, N_MAIN, reg_sma(p, w))
        expl.append((f"SMA{w}", summarize(f"SMA{w}", ac, block=L, boot=False)))
    for n in (15, 50):
        _, ac = h2_series(p, n, reg_sma(p, 50))
        expl.append((f"top-{n}", summarize(f"top-{n}", ac, block=L, boot=False)))
    _, ac = h2_series(p, N_MAIN, lambda t: negative_week(p.btc, t))
    expl.append(("7d BTC return < 0", summarize("7d BTC return < 0", ac, block=L, boot=False)))

    def dn_beta(wk: Week) -> float | None:
        ms = [s for s in members(p, wk.day, N_MAIN) if s in wk.legs]
        return trailing_index_downside_beta(ms, bars, p.btc, wk.day)

    wks = basket_weeks(p, N_MAIN)
    ac = active_net(simulate(wks, make_choose(p, N_MAIN, reg_sma(p, 50), dn_beta)))
    expl.append(("regime beta (trailing b_dn)", summarize("regime beta hedge (trailing 60d b_dn of the index)", ac, block=L, boot=False)))
    # daily rebalance: regime and weights every day (close d-1), universe and beta from the Sunday
    dweeks = []
    for t in p.weeks:
        u = {c.symbol: c for c in p.uni[t][:N_MAIN]}
        u[BTC] = p.btc_cand[t]
        for d in range(t, t + 7):
            lg = {}
            for s in u:
                sd = p.data[s]
                x = leg_week(s, sd.bars, sd.fslice((d - 2) * DAY_MS, (d + 3) * DAY_MS), d, hold=1)
                if x is not None:
                    lg[s] = x
            dweeks.append(Week(d, u, lg))
    sun = {d: d - (d - FIRST) % 7 for d in range(FIRST, LAST + 7)}

    def dchoose(wk: Week) -> Pick:
        if not below_sma(p.btc, wk.day, 50) or BTC not in wk.legs:
            return Pick([], [])
        t = sun[wk.day]
        ms = [s for s in members(p, t, N_MAIN) if s in wk.legs]
        return hedge_pick(ms, mean_beta([p.betas[(t, s)] for s in ms]))

    dact = active_net(simulate(dweeks, dchoose))
    dx = [x for _, x in dact]
    dm, dse = statistics.fmean(dx), newey_west_se(dx, NW_DAILY)
    print(f"  {'daily rebalance (x7 for a weekly equivalent)':<52} n={len(dx):>4} days net {pct(dm * 7)}/wk-equiv t_NW {dm / dse:+.2f}")
    expl.append(("daily rebalance", {"t": dm / dse, "net": dm * 7}))
    ps = [one_sided_p(o["t"]) for _, o in expl]
    for (lab, o), pv, ph in zip(expl, ps, holm(ps), strict=True):
        print(f"    {lab:<30} net {pct(o['net'])}/wk one-sided p {pv:.4f} Holm {ph:.4f}")

    # ---------------- verdict inputs
    print()
    print("=== REGISTERED VERDICT INPUTS ===")
    print(
        f"  H1: delta {f.delta:+.3f} CI95 [{f.ci()[0]:+.3f}; {f.ci()[1]:+.3f}], halves {halves[0].delta:+.3f} / {halves[1].delta:+.3f}; gate passed {s0['gate_h1']}"
    )
    print(f"  H2 gate passed: {s0['gate_h2']} (MDE {s0['mde_h2'] * 100:.3f} pp)")
    print(f"  H2 net {pct(S['net'])}/wk CI95 [{pct(S['lo'])}; {pct(S['hi'])}] (success: >= +0.300 % and lower > 0)")
    print(f"  regime - non-regime CI95 [{pct(dlo)}; {pct(dhi)}] (success: lower > 0); circular-shift p {p_shift:.4f} (success: <= 0.05)")
    print(f"  worst regime week {pct(worst[1], 2)} (kill (d) if < -15 % and net < 0.385 %/wk)")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["prereg", "stage0", "full"])
    ap.add_argument("--data-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/funding_crowding")
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/tradedesk-agents/alt_downside_beta")
    a = ap.parse_args()
    a.cache_dir = a.cache_dir.expanduser()
    a.cache_dir.mkdir(parents=True, exist_ok=True)
    a.data_dir = a.data_dir.expanduser()
    if a.stage == "prereg":
        stage_prereg(a.cache_dir)
    elif a.stage == "stage0":
        stage0(a.data_dir, a.cache_dir)
    elif a.stage == "full":
        stage_full(a.data_dir, a.cache_dir)


if __name__ == "__main__":
    main()
