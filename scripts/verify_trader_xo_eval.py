"""Evaluation of @Trader_XO's calls (card IDEA-20261113-x-traders, item 3.2): pre-registered replay on hourly bars against
placebo trades of the same geometry. Primary sample 2025-Q1 … 2026-Q4; 2020-Q3 … 2024-Q4 is a registered out-of-sample
check run by the same code without changes. Rules: src/tradedesk/analytics/trader_eval.py and the card's registration.

    uv run python scripts/verify_trader_xo_eval.py --stage coverage            # counts and data checks, no outcome metric
    uv run python scripts/verify_trader_xo_eval.py --stage full --out FILE.md  # the registered test
    uv run python scripts/verify_trader_xo_eval.py --stage full --smoke        # pipeline on synthetic bars, prints no metric

Primary metric: net return on notional per call (Trader_XO names a stop in 1 of 119 primary rows, so R is undefined).
Bars: 1h perps (Binance USD-M, Bybit, OKX, Gate, Bitget, MEXC, then Hyperliquid, then Binance spot as a flagged
fallback); a row's ``symbol`` ("venue:SYM") pins the market. One JSON file per venue, market and span in --cache-dir/bars.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import math
import random
import re
import resource
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from tradedesk.analytics.calls import Call, parse_calls
from tradedesk.analytics.trade_replay import Bar
from tradedesk.analytics.trader_eval import (
    Trade,
    call_moment,
    cluster_bootstrap_ci,
    cochran_q,
    funding_paid,
    label_permutation_p,
    mde,
    per_call_ret,
    permutation_p,
    placed_plan,
    plan_entry,
    power,
    relative_plan,
    sample_starts,
    simulate,
    trailing_vol,
    verdict_checks,
    week_of,
)

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "docs/calls/x/Trader_XO.csv"
MARKUP = ROOT / "docs/calls/x/Trader_XO-eval.csv"
CACHE = Path.home() / ".cache/tradedesk-agents/trader_xo_eval"
H, D, M = 3_600_000, 86_400_000, 60_000

# --- registered parameters (card item 3.2; do not change without a labelled amendment) ---
FILL_H = 168  # a limit / stop order waits 7 days
HOLD_H = 168  # a filled trade is held at most 7 days
COST = 0.0011 + 2 * 0.0002  # Bybit taker 0.055 % x 2 + slippage 0.02 % per side = 0.15 % round trip
SAMPLES = {"primary": (date(2025, 1, 1), date(2026, 12, 31)), "oos": (date(2020, 6, 11), date(2024, 12, 31))}
PAD_BEFORE_D, PAD_AFTER_D = 30, 60  # bars fetched per market: first call - 30 d … last call + 60 d
PERMS = 1000
SEED = 20261114
PRIOR_SD = (0.05, 0.08, 0.12)  # assumed sd of the net return per call for the pre-data MDE
VOL_BARS = 720  # trailing 30 days of 1h bars for the volatility-normalised variant
HOLM_P_KILLAXBT = 0.5115  # card 3.1, "Результат"
VENUES = ("binanceusdm", "bybit", "okx", "gate", "bitget", "mexc")
PAGE = {"binanceusdm": 1000, "bybit": 1000, "okx": 100, "bitget": 200, "binance": 1000}
GROUPS = ("main", "hedge", "cancelled", "spot", "tradfi")


@dataclass(frozen=True)
class Row:
    call: Call
    sample: str
    group: str
    kind: str
    t0: int
    t0_rule: str
    cancel: int | None
    chain_head: str
    flags: frozenset[str]

    @property
    def id(self) -> str:
        return self.call.id

    @property
    def key(self) -> str:
        """Market of the row: the pinned ``symbol`` or the asset."""
        return self.call.symbol or self.call.asset


def _utc(s: str) -> int | None:
    s = (s or "").strip()
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000) if s else None


def _day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def load_rows() -> tuple[list[Row], list[str]]:
    calls, errors = parse_calls(CALLS.read_text(encoding="utf-8"))
    lines = [ln for ln in MARKUP.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    mk = {r["id"]: r for r in csv.DictReader(io.StringIO("\n".join(lines)))}
    rows = []
    for c in calls:
        m = mk.get(c.id)
        if m is None:
            errors.append(f"{c.id}: no markup row")
            continue
        fixed = _utc(m["t0_utc"])
        t0, rule = (fixed, "markup") if fixed else call_moment(re.findall(r"\d{18,20}", c.note), _day_ms(c.day))
        rows.append(
            Row(
                c,
                m["sample"],
                m["group"],
                m["kind"],
                t0 // M * M + M,  # the next minute: tweets are stamped to the second
                rule,
                _utc(m["cancel_utc"]),
                m["chain_head"],
                frozenset(f for f in m["flags"].split(";") if f),
            )
        )
    return rows, errors


def eligible(rows: list[Row], sample: str, *, recovered: bool = False) -> list[Row]:
    """Main rows of a sample entering the result: not TradFi, not excluded by id in the batch notes, not recovered."""
    out = []
    for r in rows:
        if r.sample != sample or r.group != "main" or r.flags & {"excl_main", "not_auto", "collision"}:
            continue
        if "recovered" in r.flags and not recovered:
            continue
        out.append(r)
    return out


def median_split(rows: list[Row]) -> int:
    ts = sorted(r.t0 for r in rows)
    return ts[len(ts) // 2]


def quarter(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


# --- bars ---------------------------------------------------------------------------------------------------------


async def fetch_series(rt, venue: str, sym: str, since: int, until: int, cache: Path, markets: dict) -> list[Bar]:
    key = cache / "bars" / f"{venue}_{sym.replace('/', '-').replace(':', '-')}_1h_{since}_{until}.json"
    if key.exists():
        return [Bar(*r) for r in json.loads(key.read_text())]
    rows: list[list] = []
    if venue == "hyperliquid":
        raw = (await rt.hyperliquid.candles(sym, "1h", since, until)).data or []
        rows = [[int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])] for c in raw]
    else:
        if venue not in markets:
            markets[venue] = set(((await rt.exchanges.markets(venue)).data or {}).keys())
        if sym not in markets[venue]:
            return []
        page = PAGE.get(venue, 300)
        t = since
        while t <= until:
            end = min(until, t + (page - 1) * H)
            got = (await rt.exchanges.ohlcv(venue, sym, "1h", page, since_ms=t, until_ms=end)).data or []
            rows += [r for r in got if t <= int(r[0]) <= end]
            t = end + H
    seen, bars = set(), []
    for r in sorted(rows, key=lambda r: int(r[0])):
        if int(r[0]) not in seen and None not in r[1:5]:
            seen.add(int(r[0]))
            bars.append(Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    if until < time.time() * 1000 - H:
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps([[b.t, b.open, b.high, b.low, b.close] for b in bars]))
    return bars


def candidates(key: str) -> list[tuple[str, str]]:
    if ":" in key:  # pinned market, e.g. binance:ETH/BTC (spot) or hyperliquid:HYPE
        venue, sym = key.split(":", 1)
        if venue == "hyperliquid":  # Hyperliquid serves only the latest 5000 candles: older calls use the same perp elsewhere
            return [("hyperliquid", sym), *((v, f"{sym}/USDT:USDT") for v in VENUES)]
        return [(venue, sym)]
    return [*((v, f"{key}/USDT:USDT") for v in VENUES), ("hyperliquid", key), ("binance", f"{key}/USDT")]


def spans(rows: list[Row]) -> dict[str, tuple[int, int, int]]:
    """Per market: (fetch since, fetch until, first call t0); whole days so cache keys are stable."""
    now = int(time.time() * 1000)
    by: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by[r.key].append(r.t0)
    out = {}
    for k, ts in by.items():
        lo = _day_ms(datetime.fromtimestamp(min(ts) / 1000, UTC).date()) - PAD_BEFORE_D * D
        hi = _day_ms(datetime.fromtimestamp(max(ts) / 1000, UTC).date()) + PAD_AFTER_D * D
        out[k] = (lo, min(hi, now - now % D - 1), min(ts))
    return out


async def load_bars(rows: list[Row], cache: Path) -> dict[str, tuple[str, list[Bar]]]:
    """Per market: (venue:symbol, bars) of the first candidate whose series starts by the first call (else the earliest)."""
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    out: dict[str, tuple[str, list[Bar]]] = {}
    _, rt = build_server()
    markets: dict = {}
    try:
        for k, (since, until, first) in sorted(spans(rows).items()):
            best: tuple[str, list[Bar]] | None = None
            t_start = time.time()
            for venue, sym in candidates(k):
                try:
                    bars = await fetch_series(rt, venue, sym, since, until, cache, markets)
                except (ProviderError, KeyError, TypeError, ValueError) as e:
                    logging.warning("%s %s: %s", k, venue, e)
                    continue
                if bars and (best is None or bars[0].t < best[1][0].t):
                    best = (f"{venue}:{sym}", bars)
                if bars and bars[0].t <= first + 2 * H:
                    break
            if best:
                out[k] = best
            v, b = best or ("—", [])
            print(
                f"  bars {k:16} {v:28} n={len(b):6} from {_fmt(b[0].t) if b else '—'}  {time.time() - t_start:5.1f}s rss={_rss_mb():.0f}MB",
                flush=True,
            )
    finally:
        await rt.aclose()
    return out


def window(bars: list[Bar], times: list[int], start: int, span: int) -> list[Bar]:
    from bisect import bisect_left

    return bars[bisect_left(times, start) : bisect_left(times, start + span + 1)]


def has_data(r: Row, series: dict, need_ms: int) -> str:
    """'ok' if the market's bars hold the call's first hour and reach ``need_ms`` past it; no prices are compared."""
    s = series.get(r.key)
    if not s or not s[1]:
        return "missing"
    bars = s[1]
    if bars[0].t > r.t0 + 2 * H or bars[-1].t < r.t0:
        return "missing"
    return "ok" if bars[-1].t >= r.t0 + need_ms else "short"


# --- coverage -----------------------------------------------------------------------------------------------------


def stage_coverage(rows: list[Row], errors: list[str], cache: Path, samples: tuple[str, ...]) -> None:
    print(f"rows {len(rows)} (refused by the parser / without markup: {len(errors)})")
    for e in errors:
        print("  refused:", e)
    print("call moment rules:", dict(Counter(r.t0_rule for r in rows)))
    late = [r.id[3:] for r in rows if r.t0_rule != "same_day"]
    print("  rows not timed by an id of their own day:", " ".join(late) or "none")
    by_q = Counter((quarter(r.call.day), r.group) for r in rows)
    print("\nquarter  " + " ".join(f"{g:>9}" for g in GROUPS))
    for q in sorted({q for q, _ in by_q}):
        print(f"{q}  " + " ".join(f"{by_q[(q, g)]:9d}" for g in GROUPS))
    need = (FILL_H + HOLD_H) * H
    for s in samples:
        sel = [r for r in rows if r.sample == s]
        main = [r for r in sel if r.group == "main"]
        el = eligible(rows, s)
        print(f"\n=== sample {s}: {len(sel)} rows, groups {dict(Counter(r.group for r in sel))}")
        print(f"main {len(main)}: stop {sum(r.call.stop is not None for r in main)}, target {sum(r.call.target is not None for r in main)}")
        print("  excluded from the result: recovered", sum("recovered" in r.flags for r in main),
              "| excl_main", sum("excl_main" in r.flags for r in main), "| not_auto", sum("not_auto" in r.flags for r in main),
              "(overlapping)")  # fmt: skip
        print(f"eligible (main, not recovered / excluded): {len(el)}; kinds {dict(Counter(r.kind for r in el))}; "
              f"sides {dict(Counter(r.call.side for r in el))}")  # fmt: skip
        print("  flags:", dict(Counter(f for r in el for f in r.flags).most_common()))
        print("  markets:", dict(Counter(r.key for r in el).most_common(25)), f"(+{max(0, len({r.key for r in el}) - 25)} more)")
        series = asyncio.run(load_bars([r for r in sel if r.group != "tradfi"], cache))
        cov = Counter((r.group, has_data(r, series, need)) for r in sel if r.group != "tradfi")
        print("data (1h bars hold the call hour and reach 14 days past it):")
        for g in GROUPS[:-1]:
            print(f"  {g:9} " + "  ".join(f"{k}={cov[(g, k)]}" for k in ("ok", "short", "missing")))
        fallback = sorted({k for k, (v, _) in series.items() if v.startswith("binance:") and k.count(":") == 0})
        print("  markets on the Binance spot fallback:", " ".join(fallback) or "none")
        prim = [r for r in el if has_data(r, series, need) == "ok"]
        miss = [r.id[3:] for r in el if has_data(r, series, need) != "ok"]
        print(f"{s} sample with data: {len(prim)}; without: {len(miss)} {' '.join(miss)}")
        # price level only at the call (not an outcome): ticker collisions and units
        far = []
        for r in prim:
            bars = series[r.key][1]
            times = [b.t for b in bars]
            w = window(bars, times, r.t0, 2 * H)
            mid = (r.call.zone_low + r.call.zone_high) / 2
            if w and not 0.5 <= w[0].open / mid <= 2.0:
                far.append(f"{r.id[3:]} ({w[0].open / mid:.2f}x)")
        print(f"price at the call outside 0.5–2x of the zone mid (check ticker / units before the full run): {len(far)} {'; '.join(far)}")
        if not prim:
            continue
        weeks = len({week_of(r.t0) for r in prim})
        split = median_split(prim)
        print(
            f"weekly clusters: {weeks}; median split {_fmt(split)}: {sum(r.t0 < split for r in prim)} | {sum(r.t0 >= split for r in prim)}"
        )
        for sd in PRIOR_SD:
            print(
                f"  MDE (one-sided a=0.05, power 0.8, sd {sd:.0%}, n_eff=weeks {weeks}): {mde(sd, weeks):.2%} per call; "
                f"power at 0.5 % {power(0.005, sd, weeks):.2f}, at 1 % {power(0.01, sd, weeks):.2f}, at 2 % {power(0.02, sd, weeks):.2f}"
            )
    print(f"peak rss {_rss_mb():.0f} MB")


# --- full ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cfg:
    name: str
    fill_h: int = FILL_H
    hold_h: int = HOLD_H
    cost: float = COST
    style: str = "nearest"
    no_target: bool = False
    day_close_disclosure: bool = False


def run_call(r: Row, series, cfg: Cfg, *, cancel: bool = False, market: bool = False) -> Trade:
    s = series.get(r.key)
    if not s:
        return Trade("no_data")
    bars, times = s
    t0 = r.t0
    if cfg.day_close_disclosure and r.kind == "disclosure":
        t0 = (t0 // D + 1) * D  # entry at the close of the tweet's UTC day (variant)
    span = (cfg.fill_h + cfg.hold_h) * H + 2 * H
    w = window(bars, times, t0, span)
    if not w or w[0].t > t0 + 2 * H:
        return Trade("no_data")
    c = r.call
    plan = plan_entry(
        c.side, c.zone_low, c.zone_high, w[0].open, disclosure=market or r.kind == "disclosure", stop=c.stop,
        target=None if cfg.no_target else c.target, style=cfg.style,
    )  # fmt: skip
    author_entry = (c.zone_low + c.zone_high) / 2 if r.kind == "disclosure" else None
    return simulate(
        w, plan, start_ms=t0, fill_ms=cfg.fill_h * H, hold_ms=cfg.hold_h * H, cost_frac=cfg.cost,
        cancel_ms=r.cancel if cancel else None, sorted_bars=True, author_entry=author_entry,
    )  # fmt: skip


def stats(rows: list[Row], trades: list[Trade], *, transform=None) -> dict:
    pairs = [(r, t) for r, t in zip(rows, trades, strict=True) if t.status == "no_fill" or (t.filled and t.ret_net is not None)]
    v = per_call_ret([t for _, t in pairs])
    if transform is not None:
        v = [transform(r, x) for (r, _), x in zip(pairs, v, strict=True)]
        keep = [i for i, x in enumerate(v) if not math.isnan(x)]
        pairs, v = [pairs[i] for i in keep], [v[i] for i in keep]
    if len(v) < 2:
        return {"n": len(v), "filled": 0, "mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "too_few": True}
    lo, hi = cluster_bootstrap_ci(v, [week_of(r.t0) for r, _ in pairs], n_boot=10_000, seed=SEED)
    filled = [t for _, t in pairs if t.filled]
    return {
        "n": len(v),
        "filled": len(filled),
        "mean": statistics.fmean(v),
        "lo": lo,
        "hi": hi,
        "mean_filled": statistics.fmean([t.ret_net for t in filled]) if filled else float("nan"),
        "sd": statistics.pstdev(v),
        "weeks": len({week_of(r.t0) for r, _ in pairs}),
        "values": v,
        "rows": [r for r, _ in pairs],
    }


def placebo(rows: list[Row], series, cfg: Cfg, mode: str, n: int, seed: int) -> list[float]:
    """Mean net return per call of ``n`` placebo sets. mode: time_side (random time and side; primary), time (random
    time, same side), side (same time, random side). Random times: uniform over the market's fetched hours that leave
    a full fill + hold window."""
    rnd = random.Random(seed)
    span = (cfg.fill_h + cfg.hold_h) * H + 2 * H
    geo = []
    for r in rows:
        bars, times = series[r.key]
        w = window(bars, times, r.t0, span)
        c = r.call
        p = plan_entry(c.side, c.zone_low, c.zone_high, w[0].open, disclosure=r.kind == "disclosure", stop=c.stop,
                       target=None if cfg.no_target else c.target, style=cfg.style)  # fmt: skip
        geo.append((r, relative_plan(p, w[0].open), bars, times, times[-1] - span))
    out = []
    for _ in range(n):
        vals = []
        for r, rel, bars, times, hi_ms in geo:
            t = r.t0
            if mode in ("time_side", "time"):
                got = sample_starts(times, times[0], hi_ms, 1, rnd)
                t = got[0] if got else r.t0
            side = r.call.side if mode == "time" else rnd.choice(("long", "short"))
            w = window(bars, times, t, span)
            tr = simulate(w, placed_plan(rel, side, w[0].open), start_ms=t, fill_ms=cfg.fill_h * H, hold_ms=cfg.hold_h * H,
                          cost_frac=cfg.cost, sorted_bars=True)  # fmt: skip
            vals += per_call_ret([tr])
        out.append(statistics.fmean(vals) if vals else float("nan"))
    return out


def synthetic_series(rows: list[Row], seed: int = 7) -> dict[str, tuple[list[Bar], list[int]]]:
    """Hourly bars that never saw the market: price = mid of the latest call's zone on the market times a mean-reverting
    random factor (sd ~3 %). Used by --smoke to exercise every code path without producing a real outcome."""
    rnd = random.Random(seed)
    out = {}
    for k, (lo, hi, _) in spans([r for r in rows if r.group != "tradfi"]).items():
        anchors = sorted((r.t0, (r.call.zone_low + r.call.zone_high) / 2) for r in rows if r.key == k)
        bars, x, j = [], 0.0, 0
        for t in range(lo, hi, H):
            while j + 1 < len(anchors) and anchors[j + 1][0] <= t:
                j += 1
            x = 0.98 * x + rnd.gauss(0.0, 0.006)
            o = anchors[j][1] * math.exp(x)
            c = o * math.exp(rnd.gauss(0.0, 0.004))
            bars.append(Bar(t, o, max(o, c) * (1 + abs(rnd.gauss(0, 0.002))), min(o, c) * (1 - abs(rnd.gauss(0, 0.002))), c))
        out[k] = (bars, [b.t for b in bars])
    return out


async def load_funding(keys: list[str], cache: Path, since: int, until: int) -> dict[str, list[tuple[int, float]]]:
    """Binance USD-M funding settlements per market (sensitivity "with funding"); markets without a perp get none."""
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    out: dict[str, list[tuple[int, float]]] = {}
    _, rt = build_server()
    try:
        for k in keys:
            f = cache / "funding" / f"binanceusdm_{k.replace(':', '-').replace('/', '-')}_{since}_{until}.json"
            if f.exists():
                out[k] = [tuple(x) for x in json.loads(f.read_text())]  # type: ignore[misc]
                continue
            try:
                got = (await rt.exchanges.funding_rate_history_range("binanceusdm", f"{k}/USDT:USDT", since, until)).data or {}
            except (ProviderError, KeyError, TypeError, ValueError) as e:
                logging.warning("funding %s: %s", k, e)
                continue
            rows = sorted((int(x["timestamp"]), float(x["fundingRate"])) for x in got.get("rows", []) if x.get("fundingRate") is not None)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(rows))
            out[k] = rows
    finally:
        await rt.aclose()
    return out


def evaluate(sample: str, rows: list[Row], series, perms: int, emit, funding: dict | None) -> dict:
    """The registered test on one sample; writes its section through ``emit`` and returns the headline numbers."""
    base = Cfg("primary")
    el = eligible(rows, sample)
    tr = [run_call(r, series, base) for r in el]
    usable = [(r, t) for r, t in zip(el, tr, strict=True) if t.status not in ("no_data", "pending")]
    pr = [r for r, _ in usable]
    prim = stats(pr, [t for _, t in usable])
    if prim.get("too_few"):
        emit(f"## {sample}: n = {prim['n']} < 2, результат не визначено (◯)")
        return {"mark": "◯", "p": 1.0}
    print(f"{sample}: n={prim['n']}; placebo {perms} x 3 ...", flush=True)
    t1 = time.time()
    pl = {m: placebo(pr, series, base, m, perms, SEED + i) for i, m in enumerate(("time_side", "time", "side"))}
    print(f"  placebo done in {time.time() - t1:.0f}s rss={_rss_mb():.0f}MB", flush=True)
    p_main = permutation_p(prim["mean"], pl["time_side"])
    split = median_split(pr)
    halves = [stats([r for r in pr if (r.t0 < split) == h], [t for r, t in usable if (r.t0 < split) == h]) for h in (True, False)]
    cost2 = stats(pr, [run_call(r, series, Cfg("cost x2", cost=2 * COST)) for r in pr])
    checks = {
        "CI lower bound > 0": prim["lo"] > 0,
        "placebo p < 0.0125 (Holm-safe for 4 traders)": p_main < 0.0125,
        "both median halves > 0": all(h["mean"] > 0 for h in halves),
        "> 0 at cost x2": cost2["mean"] > 0,
    }
    v = verdict_checks(n_calls=prim["n"], n_filled=prim["filled"], mean_x=prim["mean"], p_placebo=p_main, checks=checks)
    head = "| Варіант | n | заповнено | E[дохід] net / заклик | 95 % CI (тижні) | E[дохід] / угоду |"

    def row_fmt(name: str, st: dict) -> str:
        if st.get("too_few"):
            return f"| {name} | {st['n']} | — | n = {st['n']} < 2, не визначено | — | — |"
        return (f"| {name} | {st['n']} | {st['filled']} | {st['mean']:+.2%} | [{st['lo']:+.2%}; {st['hi']:+.2%}] | "
                f"{st.get('mean_filled', float('nan')):+.2%} |")  # fmt: skip

    emit(f"## Вибірка {sample} ({SAMPLES[sample][0]} … {SAMPLES[sample][1]})")
    emit()
    emit(f"Вердикт: **{v.mark}** — {v.reason}")
    emit()
    emit(f"Плацебо ({perms} наборів): p(час+сторона, первинне) = {p_main:.4f}; p(лише час) = "
         f"{permutation_p(prim['mean'], pl['time']):.4f}; p(лише сторона) = {permutation_p(prim['mean'], pl['side']):.4f}")  # fmt: skip
    for m, xs in pl.items():
        xs2 = sorted(x for x in xs if not math.isnan(x))
        emit(f"- плацебо {m}: середнє {statistics.fmean(xs2):+.2%}, 95-й перцентиль {xs2[int(0.95 * len(xs2))]:+.2%}")
    emit(f"- MDE за фактичним sd {prim['sd']:.2%} і {prim['weeks']} тижнями: {mde(prim['sd'], prim['weeks']):.2%} на заклик")
    emit("- умови ✓: " + "; ".join(f"{k}: {'так' if ok else 'ні'}" for k, ok in checks.items()))
    emit()
    emit(head)
    emit("|---|---|---|---|---|---|")
    emit(row_fmt("первинний (механічно)", prim))
    emit(row_fmt(f"половина 1 (до {_fmt(split)})", halves[0]))
    emit(row_fmt("половина 2", halves[1]))
    emit(row_fmt("витрати ×2", cost2))
    known = [t for t in tr if t.status not in ("no_data", "pending")]
    emit()
    emit(f"Частка заповнених зон (вікно {FILL_H} год): {sum(t.filled for t in known)}/{len(known)}; "
         f"статуси: {dict(Counter(t.status for t in known))}")  # fmt: skip
    tt = [t for (r, t) in usable if t.filled and r.call.target is not None and t.status in ("target", "stop", "stopped_before", "time")]
    emit(f"Ціль досягнуто за {HOLD_H} год: {sum(t.status == 'target' for t in tt)}/{len(tt)} (заповнені з ціллю)")
    emit()
    emit("### Чутливість (усі зареєстровані варіанти; жоден не змінює вердикт)")
    emit()
    emit(head)
    emit("|---|---|---|---|---|---|")
    for cfg in (
        Cfg("утримання 1 д", hold_h=24),
        Cfg("утримання 3 д", hold_h=72),
        Cfg("утримання 30 д", hold_h=720),
        Cfg("вікно входу 72 год", fill_h=72),
        Cfg("вікно входу 30 д", fill_h=720),
        Cfg("без цілі", no_target=True),
        Cfg("вхід як у денному трекері", style="limit"),
        Cfg("розкриття: вхід за закриттям дня твіту", day_close_disclosure=True),
    ):
        emit(row_fmt(cfg.name, stats(pr, [run_call(r, series, cfg) for r in pr])))
    if funding is not None:
        fp = [Trade(t.status, t.fill_t, t.fill, t.exit_t, t.exit, t.risk, t.ret, t.r_gross, t.r_net,
                    None if t.ret_net is None or t.fill_t is None or t.exit_t is None
                    else t.ret_net - funding_paid(funding.get(r.key, []), r.call.side, t.fill_t, t.exit_t + H))
              for r, t in usable]  # fmt: skip
        emit(row_fmt("з фандингом Binance (перпи; без перпа — 0)", stats(pr, fp)))

    def volnorm(r: Row, x: float) -> float:
        bars, times = series[r.key]
        from bisect import bisect_left

        i = bisect_left(times, r.t0)
        sd = trailing_vol([b.close for b in bars[max(0, i - VOL_BARS - 1) : i]], VOL_BARS)
        return x / (sd * math.sqrt(HOLD_H)) if sd and not math.isnan(sd) else float("nan")

    st_vol = stats(pr, [t for _, t in usable], transform=volnorm)
    emit(f"| нормовано на волатильність (дохід / σ 7 д за 30 д до заклику; одиниці σ, не %) | {st_vol['n']} | {st_vol.get('filled', 0)} | "
         f"{st_vol['mean']:+.3f} σ | [{st_vol['lo']:+.3f}; {st_vol['hi']:+.3f}] | — |")  # fmt: skip

    def subset(name: str, keep) -> None:
        sel = [(r, t) for r, t in usable if keep(r)]
        emit(row_fmt(name, stats([r for r, _ in sel], [t for _, t in sel])))

    subset("без межових", lambda r: "boundary" not in r.flags)
    subset("без «з графіка»", lambda r: "from_chart" not in r.flags)
    subset("без «показано вже в плюсі»", lambda r: "in_profit" not in r.flags)
    subset("без варіантів «виключити»", lambda r: "excl_variant" not in r.flags)
    subset("ex ante (умовні + плани, без розкриттів)", lambda r: r.kind != "disclosure")
    for k in ("disclosure", "setup", "conditional"):
        subset(f"тип: {k}", lambda r, k=k: r.kind == k)
    subset("пробійні", lambda r: "breakout" in r.flags)
    for s in ("long", "short"):
        subset(f"сторона: {s}", lambda r, s=s: r.call.side == s)
    subset("лише BTC", lambda r: r.call.asset == "BTC")
    subset("без BTC", lambda r: r.call.asset != "BTC")
    subset("ціни в BTC (пари */BTC)", lambda r: "btc_quoted" in r.flags)
    subset("одна ідея (лише голови ланцюгів)", lambda r: not r.chain_head)
    top = {r.id for r, _ in sorted(zip(prim["rows"], prim["values"], strict=True), key=lambda x: -x[1])[:5]}
    subset("без 5 найкращих", lambda r: r.id not in top)
    years = sorted({r.call.day.year for r in pr})
    for y in years:
        subset(f"рік {y}", lambda r, y=y: r.call.day.year == y)
    for gname, keep in (
        ("+ відновлені (з цитат)", lambda r: r.group == "main" and not r.flags & {"excl_main", "not_auto", "collision"}),
        (
            "+ хеджі",
            lambda r: (r.group == "main" and not r.flags & {"recovered", "excl_main", "not_auto", "collision"}) or r.group == "hedge",
        ),
        ("група: хеджі", lambda r: r.group == "hedge"),
        ("група: спот (за перпом)", lambda r: r.group == "spot"),
        ("група: скасовані (вхід до скасування)", lambda r: r.group == "cancelled"),
    ):
        rs = [r for r in rows if r.sample == sample and keep(r)]
        trs = [run_call(r, series, base, cancel=True) for r in rs]
        ok = [(r, t) for r, t in zip(rs, trs, strict=True) if t.status not in ("no_data", "pending")]
        emit(row_fmt(gname, stats([r for r, _ in ok], [t for _, t in ok])))
    emit()
    emit(
        "Найкращі 5 (виключено вище): "
        + ", ".join(f"{r.id} {x:+.2%}" for r, x in sorted(zip(prim["rows"], prim["values"], strict=True), key=lambda x: -x[1])[:5])
    )
    emit()
    emit("### Бенчмарки")
    bh = []
    for r in pr:
        bars, times = series[r.key]
        w = window(bars, times, r.t0, (FILL_H + HOLD_H) * H)
        bh.append(w[-1].close / w[0].open - 1 - COST)
    filled_ret = prim.get("mean_filled", float("nan"))
    emit(f"- купив і тримав (лонг того самого ринку {FILL_H + HOLD_H} год від заклику, мінус витрати): {statistics.fmean(bh):+.2%} "
         f"на номінал проти {filled_ret:+.2%} на угоду трейдера (заповнені) і {prim['mean']:+.2%} на заклик")  # fmt: skip
    now = stats(pr, [run_call(r, series, base, market=True) for r in pr])
    emit(f"- «одразу» (ринок у момент заклику для всіх, ті самі ціль і стоп): {now['mean']:+.2%} на заклик (n {now['n']})")
    emit()
    emit("### За активами (5a)")
    emit()
    emit("| Ринок | n | E[дохід] | 95 % CI |")
    emit("|---|---|---|---|")
    by_a: dict[str, list[tuple[Row, float]]] = defaultdict(list)
    for r, x in zip(prim["rows"], prim["values"], strict=True):
        by_a[r.key].append((r, x))
    means, ses = [], []
    for a, lst in sorted(by_a.items(), key=lambda kv: -len(kv[1])):
        xs = [x for _, x in lst]
        if len(xs) >= 5:
            lo, hi = cluster_bootstrap_ci(xs, [week_of(r.t0) for r, _ in lst], n_boot=4000, seed=SEED)
            means.append(statistics.fmean(xs))
            ses.append(max(1e-9, statistics.stdev(xs) / math.sqrt(len(xs))))
            emit(f"| {a} | {len(xs)} | {statistics.fmean(xs):+.2%} | [{lo:+.2%}; {hi:+.2%}] |")
        else:
            emit(f"| {a} | {len(xs)} | {statistics.fmean(xs):+.2%} | — |")
    if len(means) >= 2:
        q, df, i2 = cochran_q(means, ses)
        labels = [r.key if len(by_a[r.key]) >= 5 else "other" for r in prim["rows"]]
        emit(f"\nCochran Q = {q:.2f} (df {df}), I² = {i2:.0%}; перестановка міток ринків: p = "
             f"{label_permutation_p(prim['values'], labels, n=2000, seed=SEED):.3f}")  # fmt: skip
    else:
        emit(f"\nринків з n ≥ 5: {len(means)} < 2, тест неоднорідності не визначено")
    both = [a for a, lst in by_a.items() if sum(r.t0 < split for r, _ in lst) >= 5 and sum(r.t0 >= split for r, _ in lst) >= 5]
    emit(f"Стійкість рангів (ринки з ≥ 5 закликами в обох половинах): {len(both)} — "
         + ("тест рангів не має сенсу (< 4 ринків)" if len(both) < 4 else ", ".join(sorted(both))))  # fmt: skip
    emit()
    emit("### Рядки вибірки")
    emit()
    emit("| id | тип | статус | вхід | вихід | дохід net |")
    emit("|---|---|---|---|---|---|")
    for r, t in usable:
        emit(f"| {r.id} | {r.kind} | {t.status} | {t.fill if t.fill is not None else '—'} | {t.exit if t.exit is not None else '—'} | "
             f"{'—' if t.ret_net is None else f'{t.ret_net:+.2%}'} |")  # fmt: skip
    emit()
    return {"mark": v.mark, "p": p_main, "mean": prim["mean"], "lo": prim["lo"]}


def stage_full(rows: list[Row], cache: Path, out: Path | None, perms: int, smoke: bool) -> None:
    lines: list[str] = []
    funding = None
    if smoke:  # synthetic bars, 3 placebo sets: checks the pipeline end to end, no real outcome is computed
        series = synthetic_series(rows)
        perms, out = 3, cache / "smoke_synthetic.md"
    else:
        raw = asyncio.run(load_bars([r for r in rows if r.group != "tradfi"], cache))
        series = {a: (b, [x.t for x in b]) for a, (_, b) in raw.items()}
        keys = sorted({r.key for r in rows if r.sample == "primary" and r.group != "tradfi" and ":" not in r.key})
        lo, hi = _day_ms(SAMPLES["primary"][0]), int(time.time() * 1000) // D * D - 1
        funding = asyncio.run(load_funding(keys, cache, lo, hi))
    lines.append(f"# Trader_XO: оцінка трейдера (зареєстровано в картці, п. 3.2), прогін {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    lines.append("")

    def emit(s: str = "") -> None:
        lines.append(s)

    res = {}
    for s in ("primary", "oos"):
        res[s] = evaluate(s, rows, series, perms, emit, funding if s == "primary" else None)
    from tradedesk.analytics.trader_eval import holm

    rej = holm([HOLM_P_KILLAXBT, res["primary"]["p"], 1.0, 1.0])
    lines.insert(2, f"Холм (m = 4; KillaXBT p = {HOLM_P_KILLAXBT}, Trader_XO p = {res['primary']['p']:.4f}, решта поки 1.0): "
                    f"Trader_XO відхиляє H0 — {'так' if rej[1] else 'ні'}. OOS (2020-Q3 … 2024-Q4) — окремий вердикт: "
                    f"{res['oos']['mark']}\n")  # fmt: skip
    text = "\n".join(lines) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if smoke:
        print(f"smoke ok (synthetic bars): {len(lines)} report lines -> {out}; peak rss {_rss_mb():.0f} MB")
        return
    print("\n".join(lines[:40]))
    print(f"... full output -> {out}; peak rss {_rss_mb():.0f} MB")


def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("coverage", "full"), required=True)
    ap.add_argument("--sample", choices=("primary", "oos", "both"), default="both", help="coverage only")
    ap.add_argument("--cache-dir", type=Path, default=CACHE)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--perms", type=int, default=PERMS)
    ap.add_argument("--smoke", action="store_true", help="full pipeline on synthetic bars; prints no outcome metric")
    a = ap.parse_args()
    rows, errors = load_rows()
    if a.stage == "coverage":
        samples = ("primary", "oos") if a.sample == "both" else (a.sample,)
        stage_coverage(rows, errors, a.cache_dir.expanduser(), samples)
    else:
        stage_full(rows, a.cache_dir.expanduser(), a.out.expanduser() if a.out else None, a.perms, a.smoke)


if __name__ == "__main__":
    main()
