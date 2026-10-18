"""Assistant recommendation method (IDEA-20261018-assistant): historical replay of option structures.

Pre-registration: the idea card, section "Попередня реєстрація (квант, 2026-10-18)".

Part A (the 18.10 claim, today's prices): replay the payoff of today's Deribit structures (prototype
snapshot, 27NOV26 expiry, 40-day windows) over past BTC windows that started with RV30 in [25 %; 40 %],
fees 0.03 % per leg each way; out-of-sample split (< 2023 / >= 2023), daily delta-hedged replay, trend
control, DVOL conditioning.

Part B (the method as a recommendation engine, walk-forward): on every day from 2021-04-01 price the same
structures from that day's real Deribit marks (09:00 UTC surfaces of the put-skew cache), run the method
with history known at that moment only, take its pick, and book the pick's real outcome at the marks
(mark +- 1 vol pt, Deribit fees, held to the 08:00 delivery; unhedged as recommended, and daily
delta-hedged with perp funding for comparison).

No network except one cached fetch of Binance BTC/USDT 1d (through the runtime provider); everything
else comes from existing caches (put-skew surfaces, delivery prices, funding, DVOL; macro-regimes 1h
klines as a fallback / consistency check).

Usage:
    uv run python scripts/verify_assistant_recs.py --cache-dir ~/.cache/tradedesk-agents/assistant_recs --stage0
    uv run python scripts/verify_assistant_recs.py --cache-dir ~/.cache/tradedesk-agents/assistant_recs --out report.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import resource
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verify_lectures import fetch_history
from verify_put_skew import load_snapshots

from tradedesk.analytics.option_replay import (
    build_legs,
    method_summary,
    non_overlapping,
    recommend,
    replay_structure,
    rv_at,
    spearman,
    strike_roles,
    summarize_replay,
    window_starts,
)
from tradedesk.analytics.put_skew import FundingIndex, block_bootstrap_ci, chain, choose_expiry
from tradedesk.analytics.vrp_marks import bootstrap_summary, hedge_path, hedged_book, holm, mde

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

# ---- pre-registered constants (idea card, "Попередня реєстрація") ----
H_TODAY = 40  # replay window of part A, days (prototype)
T_TODAY = 39.7 / 365
CALM = (0.25, 0.40)  # RV30 band of the 18.10 claim
SPLIT = int(datetime(2023, 1, 1, tzinfo=UTC).timestamp() * 1000)
FEE_LEG = 0.0003  # method fee, per leg each way (card)
HEDGE_FEE = 0.0005
DVOL_TODAY = 0.3852  # prototype snapshot
DVOL_BAND = 0.075
RV_BUCKETS = (0.25, 0.40, 0.60)  # walk-forward regime = the fixed RV30 bucket of the decision day
RV_BAND = 0.075  # sensitivity: RV30 within +- 7.5 vol pts
ENTRY_START = int(datetime(2021, 4, 1, tzinfo=UTC).timestamp() * 1000)
LAST_EXPIRY = int(datetime(2026, 10, 13, 8, tzinfo=UTC).timestamp() * 1000)
MAIN = (40, 28, 56)  # target DTE, band
SHORT = (14, 7, 21)
SPREAD = 0.01  # buy at mark + 1 vp, sell at mark - 1 vp
SPREAD_STRESS = 0.02
DERIBIT = {"option_fee": 0.0003, "delivery_fee": 0.00015, "hedge_fee": 0.0005}
CORE = ["long_straddle", "short_straddle", "long_strangle25", "short_strangle25", "iron_condor"]
CHEAP = ["long_strangle10", "reverse_iron_fly", "call_debit_spread", "put_debit_spread", "long_straddle_14d", "long_strangle25_14d"]
ALL = CORE + CHEAP
H3_SET = ["long_straddle", "long_strangle25", "iron_condor", "long_strangle10", "reverse_iron_fly", "call_debit_spread", "put_debit_spread"]
H3_SET += ["long_straddle_14d", "long_strangle25_14d"]
SD_PRIOR = 0.11  # prior SD of a 40-day long-straddle-like P&L, fraction of spot (sigma 55 % * sqrt(40/365) * 0.6)
MDE_CAP = 0.06
REJECT_UPPER = 0.03
BLOCK = 3
N_BOOT = 10_000
SEED = 20261018
NAMES = {
    "long_straddle": "купівля стреддлу ATM",
    "short_straddle": "продаж стреддлу ATM",
    "long_strangle25": "купівля стренглу 25Δ",
    "short_strangle25": "продаж стренглу 25Δ",
    "iron_condor": "залізний кондор 25Δ/10Δ (продаж)",
    "long_strangle10": "купівля стренглу 10Δ",
    "reverse_iron_fly": "зворотний метелик (стреддл − стренгл 25Δ)",
    "call_debit_spread": "колл-спред ATM/25Δ",
    "put_debit_spread": "пут-спред ATM/25Δ",
    "long_straddle_14d": "купівля стреддлу ATM, ≈14 д",
    "long_strangle25_14d": "купівля стренглу 25Δ, ≈14 д",
    "fomc_straddle": "стреддл 30.10 під ФРС",
}


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def log(msg: str, t0: float) -> None:
    print(f"[{time.perf_counter() - t0:6.0f}s, rss {rss_mb():.0f} MB] {msg}", flush=True)


def ymd(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d")


def pc(x: float | None, nd: int = 2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.{nd}f}"


# ----------------------------------------------------------------------------- data


async def _fetch_1d(path: Path) -> None:
    from tradedesk.server import build_server

    _, rt = build_server()
    try:
        days = (int(time.time() * 1000) - int(datetime(2017, 8, 1, tzinfo=UTC).timestamp() * 1000)) // DAY_MS + 5
        bars = await fetch_history(rt, "binance", "BTC/USDT", "1d", int(days), page=1000)
        await asyncio.to_thread(path.write_text, json.dumps([[b["ts"], b["close"]] for b in bars]))
    finally:
        await rt.aclose()


def load_closes(cache: Path, kdir: Path, t0: float) -> tuple[list[int], list[float], dict[str, Any]]:
    """Binance BTC/USDT daily closes (stamp = day open, close at next 00:00 UTC), contiguous."""
    p = cache / "binance_BTCUSDT_1d.json"
    info: dict[str, Any] = {}
    if not p.exists():
        try:
            asyncio.run(_fetch_1d(p))
            log("fetched Binance 1d", t0)
        except Exception as e:  # network may be unavailable: fall back to the 1h cache
            info["fetch_error"] = repr(e)[:200]
    h1: dict[int, float] = {}
    for f in sorted(kdir.glob("BTCUSDT-1h-*.json")):
        for b in json.loads(f.read_text()):
            ts = int(b[0])
            if ts % DAY_MS == 23 * HOUR_MS:
                h1[ts - 23 * HOUR_MS] = float(b[4])
    src: dict[int, float] = {}
    if p.exists():
        src = {int(t): float(c) for t, c in json.loads(p.read_text())}
        info["source"] = "binance 1d"
        common = [d for d in src if d in h1]
        info["check_1h"] = (len(common), max((abs(src[d] / h1[d] - 1) for d in common), default=float("nan")))
    else:
        src = h1
        info["source"] = "binance 1h (23:00 close)"
    days = sorted(src)
    # keep the longest contiguous tail
    start = 0
    for i in range(1, len(days)):
        if days[i] - days[i - 1] != DAY_MS:
            start = i
    info["gaps_dropped_before"] = ymd(days[start]) if start else None
    days = days[start:]
    return days, [src[d] for d in days], info


def load_today(html: Path) -> dict[str, Any]:
    for line in html.read_text().splitlines():
        s = line.strip()
        if s.startswith("const D = "):
            return json.loads(s[len("const D = ") :].rstrip(";"))
    raise ValueError("prototype snapshot not found")


def today_structures(D: dict[str, Any]) -> dict[str, tuple[list[tuple[float, bool, float, float]], float, int, float]]:
    """name -> (relative legs, premium as fraction of F from the marks, horizon days, T years)."""
    ch, k = D["chain"], D["hist"]["strikes"]
    F = float(ch["F"])
    rows = {float(r["k"]): r for r in ch["rows"]}

    def leg(role: str, c: bool, q: float) -> tuple[tuple[float, bool, float, float], float]:
        K = float(k[role])
        side = rows[K]["c" if c else "p"]
        return (K / F, c, q, float(side["iv"])), q * float(side["mark_usd"]) / F

    roles = {"atm": "atm", "c25": "c25", "p25": "p25", "c10": "c10", "p10": "p10"}
    from tradedesk.analytics.option_replay import STRUCTURES

    out = {}
    for name, spec in STRUCTURES.items():
        legs, prem = [], 0.0
        for role, c, q in spec:
            lg, pr = leg(roles[role], c, q)
            legs.append(lg)
            prem += pr
        out[name] = (legs, prem, H_TODAY, T_TODAY)
    fm = D["hist"]["fomc_straddle"]
    iv = next((float(t["iv"]) for t in D.get("term", []) if t.get("e") == fm["expiry"]), float(ch["atm_iv"]))
    kf = float(fm["strike"]) / F
    out["fomc_straddle"] = (
        [(kf, True, 1.0, iv), (kf, False, 1.0, iv)],
        float(fm["cost_pct"]),
        round(float(fm["days"])),
        float(fm["days"]) / 365,
    )
    return out


# ----------------------------------------------------------------------------- part A


def part_a(L: list[str], days: list[int], cl: list[float], dvol_day: dict[int, float], D: dict[str, Any], t0: float) -> dict[str, Any]:
    st = today_structures(D)
    rv = [rv_at(cl, i) for i in range(len(cl))]
    n = len(cl)
    res: dict[str, Any] = {}

    def starts(h: int, keep: Any) -> list[int]:
        return window_starts(n - 1, h, keep)

    calm = lambda s: rv[s] is not None and CALM[0] <= rv[s] <= CALM[1]  # noqa: E731
    regimes = {
        "calm": calm,
        "calm_pre2023": lambda s: calm(s) and days[s] < SPLIT,
        "calm_post2023": lambda s: calm(s) and days[s] >= SPLIT,
        "all": lambda s: rv[s] is not None,
        "dvol_band": lambda s: (v := dvol_day.get(days[s])) is not None and abs(v - DVOL_TODAY) <= DVOL_BAND,
        "dvol_band_post2023": lambda s: (v := dvol_day.get(days[s])) is not None and abs(v - DVOL_TODAY) <= DVOL_BAND and days[s] >= SPLIT,
    }
    L.append("## Частина A. Твердження 18.10: сьогоднішні ціни, прогін через минулі вікна\n")
    L.append(
        f"Дані: Binance BTC/USDT 1d, {ymd(days[0])} … {ymd(days[-1])} ({n} днів). Сьогоднішні структури — знімок макета "
        f"{D['asof_utc']}, експірація {D['chain']['expiry']}, F = {D['chain']['F']:.0f}, премії — марки Deribit у % від F. "
        f"Вікно {H_TODAY} д (ФРС-стреддл — {st['fomc_straddle'][2]} д). Комісії {FEE_LEG * 100:.2f} % на ногу в кожен бік; хедж {HEDGE_FEE * 100:.2f} % обороту. "
        f"RV30 зараз {D['vol']['rv_30d'] * 100:.1f} %, DVOL {DVOL_TODAY * 100:.1f} % (смуга DVOL ±{DVOL_BAND * 100:.1f} вп).\n"
    )
    L.append(
        "| Структура | премія, % F | режим | хедж | n вікон | n незалежних | середнє | у плюсі | 5 % гірших | середнє незал. | 95 % CI незал. |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for name, (legs, prem, h, t) in st.items():
        for reg in ("calm", "calm_pre2023", "calm_post2023", "dvol_band", "dvol_band_post2023", "all"):
            ss = starts(h, regimes[reg])
            for hedged in (False, True):
                r = replay_structure(legs, cl, h, ss, t_years=t, fee_per_leg=FEE_LEG, hedged=hedged, hedge_fee=HEDGE_FEE, premium=prem)
                keys = sorted(r)
                ind = non_overlapping(keys, h)
                s = summarize_replay([r[x] for x in keys], [r[x] for x in ind])
                s["by_start"] = r
                s["indep"] = ind
                res[(name, reg, hedged)] = s
                if not s.get("n"):
                    continue
                L.append(
                    f"| {NAMES[name]} | {prem * 100:+.2f} | {reg} | {'так' if hedged else 'ні'} | {s['n']} | {s['n_indep']} | {pc(s['mean'])} | "
                    f"{s['win']:.0%} | {pc(s['q05'])} | {pc(s.get('mean_indep'))} | [{pc(s.get('ci_lo'))}; {pc(s.get('ci_hi'))}] |"
                )
    log("part A replays done", t0)

    # trend control on the headline structures, calm regime, independent windows
    L.append("\n### A3. Контроль тренду (calm, незалежні вікна): що дає рух BTC за вікно\n")
    L.append(
        "| Структура | n | без хеджу | з хеджем | різниця (тренд/напрям) | 95 % CI різниці (блоковий бутстреп) | кореляція P&L без хеджу з |R| | без топ-3 вікон |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    for name in ("long_straddle", "long_strangle25", "short_straddle", "iron_condor"):
        h = st[name][2]
        u, hd = res[(name, "calm", False)], res[(name, "calm", True)]
        ind = [x for x in u["indep"] if x in hd["by_start"]]
        du = [u["by_start"][x] for x in ind]
        dh = [hd["by_start"][x] for x in ind]
        diff = [a - b for a, b in zip(du, dh, strict=True)]
        b = bootstrap_summary(diff, block=BLOCK, n_boot=N_BOOT, seed=SEED)
        mv = [abs(cl[x + h] / cl[x] - 1) for x in ind]
        from tradedesk.analytics.carry import pearson

        cor = pearson(list(zip(mv, du, strict=True)))
        top = sorted(du, reverse=True)[3:] if "long" in name else sorted(du)[3:]
        L.append(
            f"| {NAMES[name]} | {len(ind)} | {pc(statistics.mean(du))} | {pc(statistics.mean(dh))} | {pc(b['mean'])} | "
            f"[{pc(b['ci'][0])}; {pc(b['ci'][1])}] | {cor:+.2f} | {pc(statistics.mean(top))} |"
        )
    # windows of the headline long straddle, calm, independent: year and move
    L.append("\n### Незалежні calm-вікна купівлі стреддлу (без хеджу / з хеджем), % F\n")
    L.append("| Старт | RV30 на старті | рух BTC за 40 д | реалізована вола вікна | без хеджу | з хеджем |\n|---|---|---|---|---|---|")
    u, hd = res[("long_straddle", "calm", False)], res[("long_straddle", "calm", True)]
    for x in u["indep"]:
        rvw = rv_at(cl, x + H_TODAY, H_TODAY)
        L.append(
            f"| {ymd(days[x])} | {rv[x] * 100:.1f} % | {pc(cl[x + H_TODAY] / cl[x] - 1, 1)} % | {rvw * 100:.0f} % | "
            f"{pc(u['by_start'][x], 1)} | {pc(hd['by_start'].get(x), 1)} |"
        )
    # A0 on the card's own independence rule (added after seeing that the greedy chain gives n = 30, card n = 18):
    # a fixed 40-day calendar grid over all starts, keeping the grid windows that start calm
    grid = list(range(30, n - H_TODAY, H_TODAY))
    L.append("\n### A0 (додано після перегляду): незалежні вікна на фіксованій 40-денній сітці, як у картці (82 вікна всього)\n")
    L.append("| Структура | вікон сітки | з них calm | середнє calm | 95 % CI | частка > 0 |\n|---|---|---|---|---|---|")
    for name in ("long_straddle", "long_strangle25", "short_straddle", "iron_condor"):
        r = res[(name, "calm", False)]["by_start"]
        v = [r[x] for x in grid if x in r]
        m = statistics.mean(v)
        se = statistics.stdev(v) / math.sqrt(len(v))
        L.append(
            f"| {NAMES[name]} | {len(grid)} | {len(v)} | {pc(m)} | [{pc(m - 1.96 * se)}; {pc(m + 1.96 * se)}] | {sum(x > 0 for x in v) / len(v):.0%} |"
        )
    # phase robustness of the independent chain (offsets 0..39)
    L.append("\n### A5. Стійкість до вибору незалежних вікон (40 зсувів старту), calm, % F\n")
    L.append("| Структура | хедж | середнє: мін / медіана / макс | частка зсувів з CI > 0 |\n|---|---|---|---|")
    for name in ("long_straddle", "long_strangle25"):
        for hedged in (False, True):
            r = res[(name, "calm", hedged)]["by_start"]
            keys = sorted(r)
            ms, pos = [], 0
            for off in range(H_TODAY):
                ind = non_overlapping(keys, H_TODAY, offset=off)
                v = [r[x] for x in ind]
                if len(v) < 3:
                    continue
                m = statistics.mean(v)
                se = statistics.stdev(v) / math.sqrt(len(v))
                ms.append(m)
                pos += m - 1.96 * se > 0
            L.append(
                f"| {NAMES[name]} | {'так' if hedged else 'ні'} | {pc(min(ms))} / {pc(statistics.median(ms))} / {pc(max(ms))} | {pos}/{len(ms)} |"
            )
    # realized vol vs implied in calm windows
    fw = [rv_at(cl, x + H_TODAY, H_TODAY) for x in res[("long_straddle", "calm", False)]["indep"]]
    L.append(
        f"\nРеалізована вола незалежних calm-вікон: середня {statistics.mean(fw) * 100:.1f} %, медіана {statistics.median(fw) * 100:.1f} % "
        f"(сьогоднішня ATM IV {D['chain']['atm_iv'] * 100:.1f} %). До 2023: "
        f"{statistics.mean([v for v, x in zip(fw, res[('long_straddle', 'calm', False)]['indep'], strict=True) if days[x] < SPLIT]) * 100:.1f} %, з 2023: "
        f"{statistics.mean([v for v, x in zip(fw, res[('long_straddle', 'calm', False)]['indep'], strict=True) if days[x] >= SPLIT] or [float('nan')]) * 100:.1f} %."
    )
    return res


# ----------------------------------------------------------------------------- part B


def day_structures(snap: dict[str, Any], spread: float) -> dict[str, dict[str, Any]]:
    """Structures priced from one surface: name -> {expiry, entry, absl, rel, h, T}."""
    out: dict[str, dict[str, Any]] = {}
    for tgt, lo, hi, names, suffix in ((*MAIN, CORE + CHEAP[:4], ""), (*SHORT, ["long_straddle", "long_strangle25"], "_14d")):
        e = choose_expiry(snap, tgt, lo, hi, rule="nearest")
        if e is None or e > LAST_EXPIRY:
            continue
        x = snap["expiries"][e]
        roles = strike_roles(x)
        for nm in names:
            b = build_legs(nm, roles, x["F"], spread_vol=spread)
            if b is None:
                continue
            out[nm + suffix] = {
                "expiry": e,
                "entry": {"day": snap["day"], "spot": snap["spot"], "F": x["F"], "T": x["T"], "ratio": x["ratio"]},
                "absl": b[0],
                "rel": b[1],
                "h": max(1, round(x["T"] * 365)),
                "T": x["T"],
            }
    return out


def bucket(v: float | None) -> int | None:
    if v is None:
        return None
    return sum(v >= e for e in RV_BUCKETS)


def run_method(
    sts: dict[str, dict[str, Any]], cl: list[float], end: int, keep: Any, cache: dict[Any, list[int]], key: Any
) -> dict[str, dict[str, Any]]:
    out = {}
    for nm, s in sts.items():
        h = s["h"]
        ck = (key, h)
        if ck not in cache:
            cache[ck] = window_starts(end, h, keep)
        out[nm] = method_summary(s["rel"], cl, h, cache[ck], t_years=s["T"], fee_per_leg=FEE_LEG)
    return out


def realize(days_s: dict[int, dict[str, Any]], s: dict[str, Any], inp: dict[str, Any], *, hedge: bool) -> dict[str, Any] | None:
    dp = inp["delivery"].get(ymd(s["expiry"]))
    if dp is None:
        return None
    path = hedge_path(days_s, s["entry"]["day"], s["expiry"], [k for k, _, _, _ in s["absl"]])
    r = hedged_book(s["entry"], s["absl"], path, s["expiry"], dp, funding=inp["fidx"] if hedge else (), hedge=hedge, **DERIBIT)
    r["debit"] = r["premium"] < 0  # hedged_book premium is the cash received
    return r


def part_b(
    L: list[str], days: list[int], cl: list[float], dvol_day: dict[int, float], ps: Path, cache: Path, a: Any, t0: float
) -> dict[str, Any]:
    surf, meta = load_snapshots(ps, "v2")
    log(f"surfaces: {len(surf)} days", t0)
    inp = {
        "delivery": {r["date"]: float(r["delivery_price"]) for r in json.loads((ps / "delivery_btc_usd.json").read_text())},
        "fidx": FundingIndex([(int(t), float(v)) for t, v in json.loads((ps / "funding_btc_perp.json").read_text())]),
    }
    idx = {d: i for i, d in enumerate(days)}
    rv = [rv_at(cl, i) for i in range(len(cl))]
    entries = sorted(d for d in surf if d >= ENTRY_START)
    recs: dict[int, dict[str, Any]] = {}
    for j, d in enumerate(entries):
        end = idx.get(d - DAY_MS)
        if end is None or rv[end] is None:
            continue
        sts = day_structures(surf[d], SPREAD)
        if "long_straddle" not in sts:
            continue
        b_now = bucket(rv[end])
        sc: dict[Any, list[int]] = {}
        meth = run_method(sts, cl, end, lambda s, b_now=b_now: bucket(rv[s]) == b_now, sc, "rvb")
        recs[d] = {
            "day": d,
            "expiry": sts["long_straddle"]["expiry"],
            "rv30": rv[end],
            "dvol": dvol_day.get(days[end]),
            "sts": sts,
            "method": meth,
            "pick": recommend(meth),
            "pick_noci": recommend(meth, require_ci=False),
        }
        if j % 200 == 0:
            log(f"method {j}/{len(entries)} days ({ymd(d)})", t0)
    log(f"method done on {len(recs)} days", t0)
    ch = chain(recs, ENTRY_START, max(recs))
    # variants on the chain only
    for d in ch:
        r = recs[d]
        end = idx[d - DAY_MS]
        dv_now = r["dvol"]
        sc: dict[Any, list[int]] = {}
        r["method_band"] = run_method(r["sts"], cl, end, lambda s, v=rv[end]: rv[s] is not None and abs(rv[s] - v) <= RV_BAND, sc, "band")
        r["method_all"] = run_method(r["sts"], cl, end, lambda s: rv[s] is not None, sc, "all")
        if dv_now is not None:
            r["method_dvol"] = run_method(
                r["sts"], cl, end, lambda s, v=dv_now: (x := dvol_day.get(days[s])) is not None and abs(x - v) <= DVOL_BAND, sc, "dvol"
            )
        for k in ("band", "all", "dvol"):
            m = r.get(f"method_{k}")
            r[f"pick_{k}"] = recommend(m) if m else None
    log(f"variants on chain ({len(ch)} decisions) done", t0)
    out = {"surf": surf, "inp": inp, "recs": recs, "chain": ch, "meta": meta}
    if a.stage0:
        return out
    # realized books: every structure on every day (unhedged) + hedged; stress spread on the chain
    for j, (d, r) in enumerate(sorted(recs.items())):
        r["real_u"], r["real_h"] = {}, {}
        for nm, s in r["sts"].items():
            u = realize(surf, s, inp, hedge=False)
            if u is not None:
                r["real_u"][nm] = u
                if d in ch:
                    r["real_h"][nm] = realize(surf, s, inp, hedge=True)
        if j % 200 == 0:
            log(f"books {j}/{len(recs)}", t0)
    for d in ch:
        r = recs[d]
        r["real_stress"] = {}
        for nm, s in day_structures(surf[d], SPREAD_STRESS).items():
            u = realize(surf, s, inp, hedge=False)
            if u is not None:
                r["real_stress"][nm] = u
    log("books done", t0)
    dump = {
        str(d): {
            "rv30": r["rv30"],
            "dvol": r["dvol"],
            "pick": r["pick"],
            "pick_noci": r["pick_noci"],
            "method": {k: {kk: v[kk] for kk in ("n", "n_indep", "mean", "ci_lo", "score")} for k, v in r["method"].items() if v.get("n")},
            "pnl_u": {k: v["pnl"] for k, v in r["real_u"].items()},
        }
        for d, r in sorted(recs.items())
    }
    (cache / "walkforward_days.json").write_text(json.dumps(dump))
    return out


def stage0_report(L: list[str], B: dict[str, Any]) -> dict[str, Any]:
    recs, ch = B["recs"], B["chain"]
    L.append("\n## Стадія 0 (без P&L): покриття, рішення методу, потужність\n")
    L.append(
        f"Днів з поверхнею і ATM на ≈40 д (з {ymd(ENTRY_START)}): {len(recs)}; неперекривних рішень (ланцюг до експірації): {len(ch)}.\n"
    )
    L.append("| Структура | днів покрито | рішень ланцюга покрито |\n|---|---|---|")
    for nm in ALL:
        L.append(f"| {NAMES[nm]} | {sum(nm in r['sts'] for r in recs.values())} | {sum(nm in recs[d]['sts'] for d in ch)} |")
    picks = [recs[d]["pick"] for d in ch]
    n_tr = sum(p is not None for p in picks)
    L.append(f"\nРішення методу на ланцюгу (основний варіант: RV30-кошик, CI > 0): угод {n_tr} з {len(ch)}; без угоди {len(ch) - n_tr}.")
    cnt: dict[str, int] = {}
    for p in picks:
        cnt[p or "—"] = cnt.get(p or "—", 0) + 1
    L.append("Вибір: " + ", ".join(f"{NAMES.get(k, k)} {v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])) + ".")
    for k, lab in (("pick_noci", "без умови CI"), ("pick_band", "RV30 ±7.5 вп"), ("pick_all", "без режиму"), ("pick_dvol", "DVOL ±7.5 вп")):
        c: dict[str, int] = {}
        for d in ch:
            p = recs[d].get(k)
            c[p or "—"] = c.get(p or "—", 0) + 1
        L.append(f"- варіант «{lab}»: " + ", ".join(f"{NAMES.get(x, x)} {v}" for x, v in sorted(c.items(), key=lambda kv: -kv[1])))
    b_cnt = {}
    for d in ch:
        b_cnt[bucket(recs[d]["rv30"])] = b_cnt.get(bucket(recs[d]["rv30"]), 0) + 1
    L.append(f"\nКошики RV30 на рішеннях ланцюга (0: <25 %, 1: 25–40 %, 2: 40–60 %, 3: ≥60 %): {dict(sorted(b_cnt.items()))}.")
    m1 = mde(SD_PRIOR, n_tr)
    L.append(
        f"\nMDE основної гіпотези (α = 0.05 двобічно, потужність 80 %, апріорне SD {SD_PRIOR * 100:.0f} % спота): n = {n_tr} → **{m1 * 100:.2f} %** спота за угоду "
        f"({'> ' if m1 > MDE_CAP else '≤ '}{MDE_CAP * 100:.0f} %: {'результат «не значуще» — лише описовий ◯' if m1 > MDE_CAP else 'ворота потужності пройдено'})."
    )
    n_cal = sum(1 for d in ch if bucket(recs[d]["rv30"]) == 1)
    L.append(f"MDE для рішень у режимі 25–40 % (n = {n_cal}): {mde(SD_PRIOR, n_cal) * 100:.2f} %.")
    return {"n_tr": n_tr, "mde": m1}


def bline(label: str, vals: list[float]) -> tuple[str, dict[str, Any]]:
    if len(vals) < 2:
        return f"| {label} | {len(vals)} | — | — | — | — | — | — |", {"n": len(vals), "mean": None, "ci": None, "p": None}
    b = bootstrap_summary(vals, block=BLOCK, n_boot=N_BOOT, seed=SEED)
    return (
        f"| {label} | {b['n']} | {pc(b['mean'])} | [{pc(b['ci'][0])}; {pc(b['ci'][1])}] | {b['p']:.3f} | "
        f"{sum(v > 0 for v in vals) / len(vals):.0%} | {pc(statistics.median(vals))} | {pc(min(vals))} |",
        b,
    )


HDR = "| Вибірка | n | середнє | 95 % CI (блоковий бутстреп) | p | частка > 0 | медіана | найгірше |\n|---|---|---|---|---|---|---|---|"


def full_report(L: list[str], B: dict[str, Any], s0: dict[str, Any], t0: float) -> dict[str, Any]:
    recs, ch = B["recs"], B["chain"]
    V: dict[str, Any] = {}

    def picks_pnl(key: str = "pick", book: str = "real_u", days_sel: list[int] | None = None) -> list[tuple[int, str, dict[str, Any]]]:
        out = []
        for d in days_sel or ch:
            p = recs[d].get(key)
            if p and p in recs[d].get(book, {}) and recs[d][book][p] is not None:
                out.append((d, p, recs[d][book][p]))
        return out

    L.append("\n## Частина B. Метод як генератор рекомендацій (walk-forward на реальних марках)\n")
    L.append(
        "P&L — % спота входу за угоду на номінал (1 BTC кожної ноги), USD-лінійно, після витрат: купівля за mark + 1 вп, продаж за mark − 1 вп, "
        "комісія 0.03 % спота (≤ 12.5 % ціни опціону), delivery 0.015 %; до розрахунку 08:00. «R» — P&L, поділений на ризик угоди "
        "(дебет — премія; кредит — |5 % квантиль симуляції методу|).\n"
    )
    # ---- H1
    pk = picks_pnl()
    v = [x[2]["pnl"] for x in pk]
    L.append("### H1 (основна): угоди, які обрав метод, без хеджу (як рекомендується)\n")
    L.append(HDR)
    line, b1 = bline("**усі рішення з угодою (основна)**", v)
    L.append(line)
    pre = [x[2]["pnl"] for x in pk if x[0] < SPLIT]
    post = [x[2]["pnl"] for x in pk if x[0] >= SPLIT]
    L.append(bline("до 2023", pre)[0])
    L.append(bline("з 2023 (поза вибіркою для параметрів макета)", post)[0])
    srt = sorted(v, reverse=True)
    L.append(bline("без топ-3 угод", srt[3:])[0])
    L.append(
        bline(
            "усі рішення, «без угоди» = 0",
            [recs[d]["real_u"].get(recs[d]["pick"], {"pnl": 0.0})["pnl"] if recs[d]["pick"] else 0.0 for d in ch],
        )[0]
    )
    risk = []
    for d, p, r in pk:
        m = recs[d]["method"][p]
        rk = -r["premium"] if r["premium"] < 0 else abs(m["q05"])
        risk.append(r["pnl"] / rk if rk > 0 else 0.0)
    L.append(bline("у R (P&L / ризик угоди)", risk)[0])
    hp = picks_pnl(book="real_h")
    L.append(bline("ті самі угоди з щоденним дельта-хеджем і funding", [x[2]["pnl"] for x in hp])[0])
    sp = picks_pnl(book="real_stress")
    L.append(bline("стрес: спред 2 вп", [x[2]["pnl"] for x in sp])[0])
    V["H1"] = b1
    V["H1_pre"], V["H1_post"] = (statistics.mean(pre) if pre else None), (statistics.mean(post) if post else None)
    # trend control
    mv = [x[2]["move"] for x in pk]
    if len(v) > 3:
        from tradedesk.analytics.carry import pearson

        L.append(
            f"\nКонтроль тренду: кореляція P&L угод методу з рухом BTC за угоду {pearson(list(zip(mv, v, strict=True))):+.2f}, з |рухом| "
            f"{pearson(list(zip([abs(x) for x in mv], v, strict=True))):+.2f}. Медіана |руху| {statistics.median(abs(x) for x in mv) * 100:.1f} %."
        )
        med = statistics.median(abs(x) for x in mv)
        L.append(
            f"Угоди з |рухом| ≤ медіани: {pc(statistics.mean([p for p, m in zip(v, mv, strict=True) if abs(m) <= med]))} (n = {sum(abs(m) <= med for m in mv)}); "
            f"> медіани: {pc(statistics.mean([p for p, m in zip(v, mv, strict=True) if abs(m) > med]))} (n = {sum(abs(m) > med for m in mv)})."
        )
    L.append("\nЖурнал рішень ланцюга:\n")
    L.append(
        "| Вхід | Експірація | RV30 | DVOL | Вибір | очікування методу | CI методу | P&L без хеджу | з хеджем | рух BTC |\n|---|---|---|---|---|---|---|---|---|---|"
    )
    for d in ch:
        r = recs[d]
        p = r["pick"]
        m = r["method"].get(p) if p else None
        u = r["real_u"].get(p) if p else None
        hh = r["real_h"].get(p) if p else None
        mvv = (r["real_u"].get("long_straddle") or {}).get("move")
        L.append(
            f"| {ymd(d)} | {ymd(r['expiry'])} | {r['rv30'] * 100:.0f} % | {('%.0f %%' % (r['dvol'] * 100)) if r['dvol'] else '—'} | {NAMES.get(p, '—') if p else '—'} | "
            f"{pc(m['mean'], 1) if m else '—'} | {('[' + pc(m['ci_lo'], 1) + '; ' + pc(m['ci_hi'], 1) + ']') if m else '—'} | "
            f"{pc(u['pnl'], 1) if u else '—'} | {pc(hh['pnl'], 1) if hh else '—'} | {pc(mvv, 1) if mvv is not None else '—'} |"
        )
    # ---- variants
    L.append("\n### Варіанти методу (усі, крім основного, — чутливість; DVOL — зареєстрована вторинна H1-DVOL)\n")
    L.append(HDR)
    for key, lab in (
        ("pick", "основний: RV30-кошик, CI > 0"),
        ("pick_noci", "без умови CI (лише середнє > 0)"),
        ("pick_band", "RV30 ±7.5 вп"),
        ("pick_all", "без режиму (уся історія)"),
        ("pick_dvol", "**H1-DVOL: DVOL ±7.5 вп (історія з 2021-03)**"),
    ):
        line, bb = bline(lab, [x[2]["pnl"] for x in picks_pnl(key)])
        L.append(line)
        V[key] = bb
    # ---- H2: every structure, unconditional and in the calm bucket
    L.append("\n### H2: кожна структура на кожному рішенні ланцюга, без хеджу / з хеджем (Holm по 11 структурах)\n")
    L.append(
        "| Структура | n | без хеджу: середнє | 95 % CI | p | Holm p | у плюсі | з хеджем: середнє | 95 % CI | режим 25–40 %: n / середнє | середня премія, % спота |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    rows, ps = [], []
    for nm in ALL:
        u = [recs[d]["real_u"][nm]["pnl"] for d in ch if nm in recs[d]["real_u"]]
        hh = [recs[d]["real_h"][nm]["pnl"] for d in ch if recs[d]["real_h"].get(nm)]
        cal = [recs[d]["real_u"][nm]["pnl"] for d in ch if nm in recs[d]["real_u"] and bucket(recs[d]["rv30"]) == 1]
        prem = [recs[d]["real_u"][nm]["premium"] for d in ch if nm in recs[d]["real_u"]]
        bu = bootstrap_summary(u, block=BLOCK, n_boot=N_BOOT, seed=SEED) if len(u) > 1 else None
        bh = bootstrap_summary(hh, block=BLOCK, n_boot=N_BOOT, seed=SEED) if len(hh) > 1 else None
        rows.append((nm, u, bu, bh, cal, prem))
        ps.append(bu["p"] if bu else 1.0)
    adj = holm(ps)
    V["H2"] = {}
    for (nm, u, bu, bh, cal, prem), pa in zip(rows, adj, strict=True):
        V["H2"][nm] = (bu, pa)
        L.append(
            f"| {NAMES[nm]} | {len(u)} | {pc(bu['mean']) if bu else '—'} | [{pc(bu['ci'][0]) if bu else '—'}; {pc(bu['ci'][1]) if bu else '—'}] | "
            f"{bu['p']:.3f} | {pa:.3f} | {sum(x > 0 for x in u) / max(1, len(u)):.0%} | {pc(bh['mean']) if bh else '—'} | "
            f"[{pc(bh['ci'][0]) if bh else '—'}; {pc(bh['ci'][1]) if bh else '—'}] | {len(cal)} / {pc(statistics.mean(cal)) if cal else '—'} | "
            f"{pc(-statistics.mean(prem)) if prem else '—'} |"
        )
    # ---- added after seeing the results (exploratory): the card's own cell, calm bucket on the chain
    L.append("\n#### Розвідувально, додано після перегляду: рішення ланцюга в режимі RV30 25–40 % (клітинка твердження картки)\n")
    L.append(HDR)
    calm_days = [d for d in ch if bucket(recs[d]["rv30"]) == 1]
    for nm in ("long_straddle", "long_strangle25", "short_straddle", "short_strangle25"):
        L.append(bline(f"{NAMES[nm]}, без хеджу", [recs[d]["real_u"][nm]["pnl"] for d in calm_days if nm in recs[d]["real_u"]])[0])
        L.append(bline(f"{NAMES[nm]}, з хеджем", [recs[d]["real_h"][nm]["pnl"] for d in calm_days if recs[d]["real_h"].get(nm)])[0])
    L.append(f"Дні: {', '.join(ymd(d) for d in calm_days)}.")
    # ---- H3: informativeness of the method's expectation
    L.append("\n### H3: чи несе очікування методу інформацію (усі дні, Спірмен очікування проти реалізованого P&L)\n")
    per_day = []
    for d in sorted(recs):
        r = recs[d]
        per_day.append(
            [(r["method"][nm]["mean"], r["real_u"][nm]["pnl"]) for nm in H3_SET if nm in r["real_u"] and r["method"].get(nm, {}).get("n")]
        )
    flat = [p for day in per_day for p in day]
    rho = spearman(flat)

    def stat(sample: list[list[tuple[float, float]]]) -> float | None:
        return spearman([p for day in sample for p in day])

    ci = block_bootstrap_ci(per_day, stat, block=60, n_boot=1000, seed=SEED)
    within = []
    for nm in H3_SET:
        pr = [
            (recs[d]["method"][nm]["mean"], recs[d]["real_u"][nm]["pnl"])
            for d in sorted(recs)
            if nm in recs[d]["real_u"] and recs[d]["method"].get(nm, {}).get("n")
        ]
        within.append((nm, len(pr), spearman(pr)))
    L.append(
        f"Пул {len(H3_SET)} структур без дзеркальних коротких, n пар = {len(flat)} ({len(per_day)} днів): ρ = {rho:+.3f}, "
        f"95 % CI (блоковий бутстреп по днях, блок 60 д) [{ci[0]:+.3f}; {ci[1]:+.3f}]."
        if ci
        else f"ρ = {rho}"
    )
    L.append(
        "Усередині структури (часовий ряд, перекривні дні, описово): "
        + "; ".join(f"{NAMES[n]} ρ = {r:+.2f} (n = {k})" for n, k, r in within if r is not None)
        + "."
    )
    # p for H3 from the bootstrap distribution (share of resampled rho <= 0)
    import random

    rng = random.Random(SEED)
    bs = []
    nd = len(per_day)
    for _ in range(1000):
        smp: list[list[tuple[float, float]]] = []
        while len(smp) < nd:
            i = rng.randrange(0, nd - 60 + 1)
            smp.extend(per_day[i : i + 60])
        x = stat(smp[:nd])
        if x is not None:
            bs.append(x)
    p3 = min(1.0, 2 * min(sum(x <= 0 for x in bs), sum(x >= 0 for x in bs)) / len(bs))
    V["H3"] = (rho, ci, p3)
    adj3 = holm([b1["p"] if b1.get("p") is not None else 1.0, V["pick_dvol"]["p"] if V["pick_dvol"].get("p") is not None else 1.0, p3])
    V["holm_primary"] = adj3
    L.append(f"p (бутстреп) = {p3:.3f}. Holm по сім'ї {{H1, H1-DVOL, H3}}: {', '.join(f'{x:.3f}' for x in adj3)}.")
    log("report part B done", t0)
    return V


def classify_h1(b: dict[str, Any], p_adj: float, pre: float | None, post: float | None, mde_v: float) -> str:
    if b.get("ci") is None:
        return "descriptive"
    lo, hi = b["ci"]
    if lo > 0 and p_adj < 0.05 and (pre or 0) > 0 and (post or 0) > 0:
        return "confirmed"
    if hi < REJECT_UPPER:
        return "rejected"
    if mde_v > MDE_CAP:
        return "descriptive"
    return "inconclusive"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--put-skew-cache", default="~/.cache/tradedesk-agents/put_skew")
    ap.add_argument("--klines-dir", default="~/.cache/tradedesk-agents/macro_regimes/klines")
    ap.add_argument("--prototype", default=str(Path(__file__).resolve().parents[1] / "docs/design/assistant-prototype.html"))
    ap.add_argument("--stage0", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.perf_counter()
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    ps = Path(a.put_skew_cache).expanduser()
    days, cl, info = load_closes(cache, Path(a.klines_dir).expanduser(), t0)
    log(f"closes: {len(cl)} days from {ymd(days[0])} ({info})", t0)
    dvol = [(int(t), float(v)) for t, v in json.loads((ps / "dvol_BTC.json").read_text())]
    dvol_day = {(t // DAY_MS) * DAY_MS: v for t, v in dvol}  # candle stamped day d, known at the close of day d
    D = load_today(Path(a.prototype))
    L = ["# Метод рекомендацій асистента: сирий вивід `scripts/verify_assistant_recs.py`\n"]
    L.append(f"Ціни закриття: {info}. DVOL BTC: {len(dvol)} днів ({ymd(dvol[0][0])} … {ymd(dvol[-1][0])}).\n")
    # sanity: known numbers
    rv_now = rv_at(cl, len(cl) - 1)
    L.append(
        f"Перевірка узгодженості: RV30 на останнє закриття ({ymd(days[-1])}) = {rv_now * 100:.1f} % (макет 18.10: {D['vol']['rv_30d'] * 100:.1f} %); "
        f"останнє закриття {cl[-1]:.0f} (макет: спот {D['spot']:.0f}).\n"
    )
    st = today_structures(D)
    L.append(
        "Премії сьогоднішніх структур (% F, з марок макета): "
        + ", ".join(f"{NAMES[k]} {v[1] * 100:+.2f}" for k, v in st.items())
        + f". Макет: стреддл {D['hist']['by']['long_straddle']['premium_pct'] * -100:+.2f}, стренгл {D['hist']['by']['long_strangle']['premium_pct'] * -100:+.2f}, "
        f"кондор {D['hist']['by']['iron_condor']['premium_pct'] * -100:+.2f}.\n"
    )
    if a.stage0:
        B = part_b(L, days, cl, dvol_day, ps, cache, a, t0)
        stage0_report(L, B)
    else:
        part_a(L, days, cl, dvol_day, D, t0)
        B = part_b(L, days, cl, dvol_day, ps, cache, a, t0)
        s0 = stage0_report(L, B)
        V = full_report(L, B, s0, t0)
        v1 = classify_h1(V["H1"], V["holm_primary"][0], V["H1_pre"], V["H1_post"], s0["mde"])
        vd = classify_h1(V["pick_dvol"], V["holm_primary"][1], None, None, s0["mde"]) if V["pick_dvol"].get("ci") else "descriptive"
        rho, ci, p3 = V["H3"]
        v3 = "confirmed" if ci and ci[0] > 0 and V["holm_primary"][2] < 0.05 else ("rejected" if ci and ci[1] < 0.05 else "inconclusive")
        L.append("\n## Механічна перевірка критеріїв реєстрації\n")
        b = V["H1"]
        L.append(
            f"- H1: середнє {pc(b['mean'])} %, CI [{pc(b['ci'][0])}; {pc(b['ci'][1])}], Holm p = {V['holm_primary'][0]:.3f}, до 2023 {pc(V['H1_pre'])}, "
            f"з 2023 {pc(V['H1_post'])}, MDE {s0['mde'] * 100:.2f} % → **{v1}**."
        )
        bd = V["pick_dvol"]
        if bd.get("ci"):
            L.append(
                f"- H1-DVOL: середнє {pc(bd['mean'])} %, CI [{pc(bd['ci'][0])}; {pc(bd['ci'][1])}], Holm p = {V['holm_primary'][1]:.3f} → **{vd}**."
            )
        L.append(f"- H3: ρ = {rho:+.3f}, CI [{ci[0]:+.3f}; {ci[1]:+.3f}], Holm p = {V['holm_primary'][2]:.3f} → **{v3}**.")
        pos2 = [n for n, (bu, pa) in V["H2"].items() if bu and bu["ci"][0] > 0 and pa < 0.05]
        L.append(f"- H2 (Holm по 11): структури з CI > 0 і Holm p < 0.05: {', '.join(NAMES[n] for n in pos2) or 'жодної'}.")
        tg = "з доказами" if v1 == "confirmed" else "лише з позначкою «гіпотеза»"
        L.append(f"- Правило для Telegram (зареєстроване): рекомендації йдуть **{tg}**.")
    report = "\n".join(L)
    print(report)
    if a.out:
        Path(a.out).expanduser().write_text(report + "\n")
    print(f"\npeak RSS {rss_mb():.0f} MB, {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
