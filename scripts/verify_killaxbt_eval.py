"""Evaluation of @KillaXBT's calls (card IDEA-20261113-x-traders, item 3.1): pre-registered replay on hourly bars against
placebo trades of the same geometry. Rules: src/tradedesk/analytics/trader_eval.py and the card's registration.

    uv run python scripts/verify_killaxbt_eval.py --stage coverage            # counts only, no outcome metric
    uv run python scripts/verify_killaxbt_eval.py --stage full --out FILE.md  # the registered test
    uv run python scripts/verify_killaxbt_eval.py --stage full --smoke        # pipeline check on 5 rows, prints no metric

Bars: 1h perps (Binance USD-M, Bybit, OKX, Gate, Bitget, MEXC, then Hyperliquid), one file per venue and asset in
--cache-dir/bars; a call whose hour has no 1h bar falls back to 1d bars of the same venues (flagged).
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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path

from tradedesk.analytics.calls import Call, parse_calls
from tradedesk.analytics.trade_replay import Bar
from tradedesk.analytics.trader_eval import (
    Plan,
    Trade,
    cluster_bootstrap_ci,
    cochran_q,
    label_permutation_p,
    mde,
    per_call_r,
    permutation_p,
    placed_plan,
    plan_entry,
    power,
    relative_plan,
    sample_starts,
    simulate,
    snowflake_ms,
    verdict,
    week_of,
)

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "docs/calls/x/KillaXBT.csv"
MARKUP = ROOT / "docs/calls/x/KillaXBT-eval.csv"
CACHE = Path.home() / ".cache/tradedesk-agents/killaxbt_eval"
H, D, M = 3_600_000, 86_400_000, 60_000

# --- registered parameters (card item 3.1; do not change without a labelled amendment) ---
FILL_H = 168  # a limit / stop order waits 7 days
HOLD_H = 168  # a filled trade is held at most 7 days
COST = 0.0011 + 2 * 0.0002  # Bybit taker 0.055 % x 2 + slippage 0.02 % per side = 0.15 % round trip
STUDY = (date(2024, 10, 1), date(2026, 9, 30))
SPLIT = date(2025, 10, 1)  # time halves: 2024-10..2025-09 | 2025-10..2026-09
PERMS = 1000
SEED = 20261114
PRIOR_SD_R = 1.5  # assumed sd of R per call for the pre-data MDE
VENUES = ("binanceusdm", "bybit", "okx", "gate", "bitget", "mexc")
PAGE = {"binanceusdm": 1000, "bybit": 1000, "okx": 100, "bitget": 200}


@dataclass(frozen=True)
class Row:
    call: Call
    group: str
    kind: str
    t0: int
    cancel: int | None
    implicit_cancel: int | None
    author: tuple[str, int] | None
    chain_head: str
    flags: frozenset[str]

    @property
    def id(self) -> str:
        return self.call.id


def _utc(s: str) -> int | None:
    s = (s or "").strip()
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000) if s else None


def _day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


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
        tid = re.search(r"\d{18,20}", c.note)
        t0 = _utc(m["t0_utc"]) or (snowflake_ms(tid.group(0)) if tid else _day_ms(c.day) + D - M)
        ex = (m["author_exit"], _utc(m["author_exit_utc"])) if m["author_exit"] else None
        rows.append(
            Row(
                c,
                m["group"],
                m["kind"],
                t0 // M * M + M,  # the next minute: tweets are stamped to the second
                _utc(m["cancel_utc"]),
                _utc(m["implicit_cancel_utc"]),
                ex,  # type: ignore[arg-type]
                m["chain_head"],
                frozenset(f for f in m["flags"].split(";") if f),
            )
        )
    return rows, errors


def median_split(rows: list[Row]) -> int:
    """t0 that splits the primary sample into two halves by count (the later half starts here)."""
    ts = sorted(r.t0 for r in rows)
    return ts[len(ts) // 2]


def quarter(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


# --- bars ---------------------------------------------------------------------------------------------------------


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


async def fetch_series(rt, venue: str, asset: str, tf: str, since: int, until: int, cache: Path, markets: dict) -> list[Bar]:
    key = cache / "bars" / f"{venue}_{asset}_{tf}_{since}_{until}.json"
    if key.exists():
        return [Bar(*r) for r in json.loads(key.read_text())]
    step = H if tf == "1h" else D
    rows: list[list] = []
    if venue == "hyperliquid":
        raw = (await rt.hyperliquid.candles(asset, tf, since, until)).data or []
        rows = [[int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"])] for c in raw]
    else:
        if venue not in markets:
            markets[venue] = set(((await rt.exchanges.markets(venue)).data or {}).keys())
        sym = f"{asset}/USDT:USDT"
        if sym not in markets[venue]:
            return []
        page = PAGE.get(venue, 300)
        t = since
        while t <= until:
            end = min(until, t + (page - 1) * step)
            got = (await rt.exchanges.ohlcv(venue, sym, tf, page, since_ms=t, until_ms=end)).data or []
            rows += [r for r in got if t <= int(r[0]) <= end]
            t = end + step
    seen, bars = set(), []
    for r in sorted(rows, key=lambda r: int(r[0])):
        if int(r[0]) not in seen and None not in r[1:5]:
            seen.add(int(r[0]))
            bars.append(Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    if until < time.time() * 1000 - step:
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text(json.dumps([[b.t, b.open, b.high, b.low, b.close] for b in bars]))
    return bars


async def load_bars(rows: list[Row], cache: Path, tf: str = "1h") -> dict[str, tuple[str, list[Bar]]]:
    """Per asset: (venue, bars) of the first venue whose series starts by the asset's first call (else the earliest)."""
    from tradedesk.core.errors import ProviderError
    from tradedesk.server import build_server

    since = _day_ms(date(2024, 9, 23))
    now = int(time.time() * 1000)
    until = min(now - now % D - 1, _day_ms(date(2026, 12, 31)))  # whole days only, so the cache key is stable per day
    first: dict[str, int] = {}
    for r in rows:
        if r.group != "tradfi":
            first[r.call.asset] = min(first.get(r.call.asset, r.t0), r.t0)
    out: dict[str, tuple[str, list[Bar]]] = {}
    _, rt = build_server()
    markets: dict = {}
    try:
        for asset in sorted(first):
            best: tuple[str, list[Bar]] | None = None
            t_start = time.time()
            for venue in (*VENUES, "hyperliquid"):
                try:
                    bars = await fetch_series(rt, venue, asset, tf, since, until, cache, markets)
                except (ProviderError, KeyError, TypeError, ValueError) as e:
                    logging.warning("%s %s: %s", asset, venue, e)
                    continue
                if bars and (best is None or bars[0].t < best[1][0].t):
                    best = (venue, bars)
                if bars and bars[0].t <= first[asset] + 2 * H:
                    break
            if best:
                out[asset] = best
            v, b = best or ("—", [])
            print(
                f"  bars {tf} {asset:9} {v:12} n={len(b):6} from {_fmt(b[0].t) if b else '—'}  "
                f"{time.time() - t_start:5.1f}s  rss={_rss_mb():.0f}MB",
                flush=True,
            )
    finally:
        await rt.aclose()
    return out


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def window(bars: list[Bar], times: list[int], start: int, span: int) -> list[Bar]:
    from bisect import bisect_left

    a = bisect_left(times, start)
    b = bisect_left(times, start + span + 1)
    return bars[a:b]


# --- coverage -----------------------------------------------------------------------------------------------------


def has_data(r: Row, series: dict[str, tuple[str, list[Bar]]], need_ms: int) -> str:
    """'ok' if the asset's bars hold the call's first hour and reach ``need_ms`` past it, 'short' if they start in time but
    end early, 'missing' otherwise. No prices are compared."""
    s = series.get(r.call.asset)
    if not s or not s[1]:
        return "missing"
    bars = s[1]
    if bars[0].t > r.t0 + 2 * H or bars[-1].t < r.t0:
        return "missing"
    return "ok" if bars[-1].t >= r.t0 + need_ms else "short"


def stage_coverage(rows: list[Row], errors: list[str], cache: Path) -> None:
    print(f"rows {len(rows)} (refused by the parser: {len(errors)})")
    for e in errors:
        print("  refused:", e)
    by_q = Counter((quarter(r.call.day), r.group) for r in rows)
    qs = sorted({q for q, _ in by_q})
    groups = ("main", "hedge", "cancelled", "spot", "tradfi")
    print("\nquarter  " + " ".join(f"{g:>9}" for g in groups))
    for q in qs:
        print(f"{q}  " + " ".join(f"{by_q[(q, g)]:9d}" for g in groups))
    main = [r for r in rows if r.group == "main"]
    with_stop = [r for r in main if r.call.stop is not None]
    print(
        f"\nmain {len(main)}: with stop {len(with_stop)}, with target {sum(r.call.target is not None for r in main)}, "
        f"stop+target {sum(r.call.stop is not None and r.call.target is not None for r in main)}"
    )
    print("main kinds:", dict(Counter(r.kind for r in main)), "| with stop:", dict(Counter(r.kind for r in with_stop)))
    print(
        "main sides:",
        dict(Counter(r.call.side for r in with_stop)),
        "| assets (with stop):",
        dict(Counter(r.call.asset for r in with_stop).most_common()),
    )
    print("flags in main with stop:", dict(Counter(f for r in with_stop for f in r.flags).most_common()))
    print("author exits in main with stop:", dict(Counter(r.author[0] for r in with_stop if r.author)))
    series = asyncio.run(load_bars([r for r in rows if r.group != "tradfi"], cache))
    need = (FILL_H + HOLD_H) * H
    cov = Counter((r.group, has_data(r, series, need)) for r in rows if r.group != "tradfi")
    print("\ndata (1h bars hold the call hour and reach 14 days past it):")
    for g in groups[:-1]:
        print(f"  {g:9} " + "  ".join(f"{k}={cov[(g, k)]}" for k in ("ok", "short", "missing")))
    prim = [r for r in with_stop if has_data(r, series, need) == "ok"]
    miss = [r.id for r in with_stop if has_data(r, series, need) != "ok"]
    print(f"primary sample (main, stop, 1h data ok): {len(prim)}; without 1h data: {len(miss)} {' '.join(miss)}")
    weeks = len({week_of(r.t0) for r in prim})
    split = median_split(prim)
    print(
        f"weekly clusters in the primary sample: {weeks}; calendar halves (split {SPLIT}): "
        f"{sum(r.call.day < SPLIT for r in prim)} | {sum(r.call.day >= SPLIT for r in prim)}; "
        f"median split {_fmt(split)}: {sum(r.t0 < split for r in prim)} | {sum(r.t0 >= split for r in prim)}"
    )
    for sd in (1.0, PRIOR_SD_R, 2.0):
        print(
            f"  MDE (one-sided a=0.05, power 0.8, sd {sd}R, n_eff=weeks {weeks}): {mde(sd, weeks):.2f} R per call; "
            f"power at 0.25R {power(0.25, sd, weeks):.2f}, at 0.5R {power(0.5, sd, weeks):.2f}"
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
    author: bool = True
    no_target: bool = False


def run_call(r: Row, series, cfg: Cfg, *, cancel: bool = False, implicit: bool = False) -> Trade:
    s = series.get(r.call.asset)
    if not s:
        return Trade("no_data")
    bars, times = s
    span = (cfg.fill_h + cfg.hold_h) * H + 2 * H
    w = window(bars, times, r.t0, span)
    if not w or w[0].t > r.t0 + 2 * H:
        return Trade("no_data")
    c = r.call
    if not 0.5 <= w[0].open / ((c.zone_low + c.zone_high) / 2) <= 1.5:
        return Trade("no_data")  # another token under the same ticker
    plan = plan_entry(
        c.side,
        c.zone_low,
        c.zone_high,
        w[0].open,
        disclosure=r.kind == "disclosure",
        stop=c.stop,
        target=None if cfg.no_target else c.target,
        style=cfg.style,
    )
    cx = r.cancel if cancel else None
    if implicit and r.implicit_cancel:
        cx = min(cx or r.implicit_cancel, r.implicit_cancel)
    return simulate(
        w,
        plan,
        start_ms=r.t0,
        fill_ms=cfg.fill_h * H,
        hold_ms=cfg.hold_h * H,
        cost_frac=cfg.cost,
        cancel_ms=cx,
        author=r.author if cfg.author else None,
        sorted_bars=True,
    )


def stats(rows: list[Row], trades: list[Trade]) -> dict:
    pairs = [(r, t) for r, t in zip(rows, trades, strict=True) if t.status == "no_fill" or (t.filled and t.r_net is not None)]
    v = per_call_r([t for _, t in pairs])
    if len(v) < 2:
        return {"n": len(v), "mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "filled": 0}
    lo, hi = cluster_bootstrap_ci(v, [week_of(r.t0) for r, _ in pairs], n_boot=10_000, seed=SEED)
    filled = [t for _, t in pairs if t.filled]
    return {
        "n": len(v),
        "filled": len(filled),
        "mean": statistics.fmean(v),
        "lo": lo,
        "hi": hi,
        "mean_filled": statistics.fmean([t.r_net for t in filled]) if filled else float("nan"),
        "ret_net": statistics.fmean([t.ret_net for t in filled]) if filled else float("nan"),
        "sd": statistics.pstdev(v),
        "weeks": len({week_of(r.t0) for r, _ in pairs}),
        "values": v,
        "rows": [r for r, _ in pairs],
    }


def placebo(rows: list[Row], series, cfg: Cfg, mode: str, n: int, seed: int) -> list[float]:
    """Mean R per call of ``n`` placebo sets. mode: time_side (random time and side; primary), time (random time, same
    side), side (same time, random side). Mechanical rules (no author exits)."""
    rnd = random.Random(seed)
    span = (cfg.fill_h + cfg.hold_h) * H + 2 * H
    lo_ms, hi_end = _day_ms(STUDY[0]), _day_ms(STUDY[1]) + D
    geo = []
    for r in rows:
        bars, times = series[r.call.asset]
        w = window(bars, times, r.t0, span)
        c = r.call
        p = plan_entry(
            c.side,
            c.zone_low,
            c.zone_high,
            w[0].open,
            disclosure=r.kind == "disclosure",
            stop=c.stop,
            target=None if cfg.no_target else c.target,
            style=cfg.style,
        )
        hi_ms = min(hi_end, times[-1] - span)
        geo.append((r, relative_plan(p, w[0].open), bars, times, hi_ms))
    out = []
    for _ in range(n):
        vals = []
        for r, rel, bars, times, hi_ms in geo:
            t = r.t0
            if mode in ("time_side", "time"):
                got = sample_starts(times, max(lo_ms, times[0]), hi_ms, 1, rnd)
                t = got[0] if got else r.t0
            side = r.call.side if mode == "time" else rnd.choice(("long", "short"))
            w = window(bars, times, t, span)
            tr = simulate(
                w,
                placed_plan(rel, side, w[0].open),
                start_ms=t,
                fill_ms=cfg.fill_h * H,
                hold_ms=cfg.hold_h * H,
                cost_frac=cfg.cost,
                sorted_bars=True,
            )
            vals += per_call_r([tr])
        out.append(statistics.fmean(vals) if vals else float("nan"))
    return out


def synthetic_series(rows: list[Row], seed: int = 7) -> dict[str, tuple[list[Bar], list[int]]]:
    """Hourly bars that never saw the market: price = mid of the latest call's zone on the asset times a mean-reverting
    random factor (sd ~3 %). Used by --smoke to exercise every code path without producing a real outcome."""
    rnd = random.Random(seed)
    lo, hi = _day_ms(date(2024, 9, 23)), _day_ms(date(2026, 11, 1))
    out = {}
    for asset in sorted({r.call.asset for r in rows if r.group != "tradfi"}):
        anchors = sorted((r.t0, (r.call.zone_low + r.call.zone_high) / 2) for r in rows if r.call.asset == asset)
        bars, x, k = [], 0.0, 0
        for t in range(lo, hi, H):
            while k + 1 < len(anchors) and anchors[k + 1][0] <= t:
                k += 1
            x = 0.98 * x + rnd.gauss(0.0, 0.006)
            o = anchors[k][1] * math.exp(x)
            c = o * math.exp(rnd.gauss(0.0, 0.004))
            bars.append(Bar(t, o, max(o, c) * (1 + abs(rnd.gauss(0, 0.002))), min(o, c) * (1 - abs(rnd.gauss(0, 0.002))), c))
        out[asset] = (bars, [b.t for b in bars])
    return out


def stage_full(rows: list[Row], cache: Path, out: Path | None, perms: int, smoke: bool) -> None:
    if smoke:  # synthetic bars, 3 placebo sets: checks the pipeline end to end, no real outcome is computed
        series = synthetic_series(rows)
        perms, out = 3, cache / "smoke_synthetic.md"
    else:
        raw = asyncio.run(load_bars([r for r in rows if r.group != "tradfi"], cache))
        series = {a: (b, [x.t for x in b]) for a, (_, b) in raw.items()}
    base = Cfg("primary")
    main = [r for r in rows if r.group == "main" and r.call.stop is not None]
    tr = [run_call(r, series, base) for r in main]
    usable = [(r, t) for r, t in zip(main, tr, strict=True) if t.status not in ("no_data", "pending")]
    prim_rows = [r for r, _ in usable]
    lines: list[str] = []

    def emit(s: str = "") -> None:
        lines.append(s)

    def row_fmt(name: str, st: dict) -> str:
        return (
            f"| {name} | {st['n']} | {st.get('filled', 0)} | {st['mean']:+.3f} | [{st['lo']:+.3f}; {st['hi']:+.3f}] | "
            f"{st.get('mean_filled', float('nan')):+.3f} | {st.get('ret_net', float('nan')) * 100:+.2f} % |"
        )

    head = "| Варіант | n | заповнено | E[R] net / заклик | 95 % CI (тижні) | E[R] / угоду | дохід / угоду (на номінал) |"
    sep = "|---|---|---|---|---|---|---|"
    prim = stats(prim_rows, [t for _, t in usable])
    mech_tr = [run_call(r, series, replace(base, author=False)) for r in prim_rows]
    mech = stats(prim_rows, mech_tr)
    print(f"primary n={prim['n']} filled={prim['filled']}; placebo {perms} x 3 ...", flush=True)
    t1 = time.time()
    pl = {
        m: placebo(prim_rows, series, replace(base, author=False), m, perms, SEED + i) for i, m in enumerate(("time_side", "time", "side"))
    }
    print(f"  placebo done in {time.time() - t1:.0f}s rss={_rss_mb():.0f}MB", flush=True)
    p_main = permutation_p(prim["mean"], pl["time_side"])
    p_mech = permutation_p(mech["mean"], pl["time_side"])
    split = median_split(prim_rows)
    halves = [stats([r for r in prim_rows if (r.t0 < split) == h], [t for r, t in usable if (r.t0 < split) == h]) for h in (True, False)]
    cal = [
        stats([r for r in prim_rows if (r.call.day < SPLIT) == h], [t for r, t in usable if (r.call.day < SPLIT) == h])
        for h in (True, False)
    ]
    no_rec = [(r, t) for r, t in usable if "recovered" not in r.flags]
    st_no_rec = stats([r for r, _ in no_rec], [t for _, t in no_rec])
    v = verdict(
        n_calls=prim["n"],
        n_filled=prim["filled"],
        mean_r=prim["mean"],
        ci_low=prim["lo"],
        p_placebo=p_main,
        p_placebo_mech=p_mech,
        halves_positive=all(h["mean"] > 0 for h in halves),
        without_recovered_positive=st_no_rec["mean"] > 0,
    )
    emit(f"# KillaXBT: оцінка трейдера (зареєстровано в картці, п. 3.1), прогін {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    emit()
    emit(f"Вердикт: **{v.mark}** — {v.reason}")
    emit()
    emit(
        f"Плацебо ({perms} наборів): p(час+сторона, первинне) = {p_main:.4f}; механічний проти механічного = {p_mech:.4f}; "
        f"p(лише час) = {permutation_p(prim['mean'], pl['time']):.4f}; p(лише сторона) = {permutation_p(prim['mean'], pl['side']):.4f}"
    )
    for m, xs in pl.items():
        xs2 = sorted(x for x in xs if not math.isnan(x))
        emit(f"- плацебо {m}: середнє {statistics.fmean(xs2):+.3f} R, 95-й перцентиль {xs2[int(0.95 * len(xs2))]:+.3f} R")
    emit(f"- MDE за фактичним sd {prim['sd']:.2f} R і {prim['weeks']} тижнями: {mde(prim['sd'], prim['weeks']):.3f} R")
    emit()
    emit(head)
    emit(sep)
    emit(row_fmt("первинний (вихід автора)", prim))
    emit(row_fmt("механічний (без виходів автора)", mech))
    emit(row_fmt(f"половина 1 (за медіаною, до {_fmt(split)})", halves[0]))
    emit(row_fmt("половина 2 (за медіаною)", halves[1]))
    emit(row_fmt("календарна половина 1 (до 2025-10), варіант", cal[0]))
    emit(row_fmt("календарна половина 2 (з 2025-10), варіант", cal[1]))
    emit(row_fmt("без відновлених", st_no_rec))
    # share filled and target before stop (mechanical, all main rows with data)
    all_main = [r for r in rows if r.group == "main"]
    mt = [(r, run_call(r, series, replace(base, author=False))) for r in all_main]
    known = [t for _, t in mt if t.status not in ("no_data", "pending")]
    ts = [t for r, t in mt if t.filled and r.call.stop is not None and r.call.target is not None and t.status in ("target", "stop")]
    emit()
    emit(
        f"Частка заповнених зон (усі main з даними, механічно, вікно {FILL_H} год): "
        f"{sum(t.filled for t in known)}/{len(known)} = {sum(t.filled for t in known) / max(1, len(known)):.1%}"
    )
    emit(
        f"Частка «ціль раніше стопу» (заповнені зі стопом і ціллю, утримання {HOLD_H} год): "
        f"{sum(t.status == 'target' for t in ts)}/{len(ts)}"
    )
    # sensitivity
    emit()
    emit("## Чутливість (усі варіанти; жоден не обрано після результатів)")
    emit()
    emit(head)
    emit(sep)
    cfgs = [
        Cfg("утримання 1 д", hold_h=24),
        Cfg("утримання 30 д", hold_h=720),
        Cfg("утримання 90 д", hold_h=2160),
        Cfg("вікно входу 72 год", fill_h=72),
        Cfg("вікно входу 30 д", fill_h=720),
        Cfg("витрати ×2", cost=2 * COST),
        Cfg("без цілі", no_target=True),
        Cfg("вхід як у денному трекері", style="limit"),
    ]
    for cfg in cfgs:
        trs = [run_call(r, series, cfg) for r in prim_rows]
        emit(row_fmt(cfg.name, stats(prim_rows, trs)))

    def subset(name: str, keep) -> None:
        sel = [(r, t) for r, t in usable if keep(r)]
        if sel:
            emit(row_fmt(name, stats([r for r, _ in sel], [t for _, t in sel])))

    for fl, nm in (
        ("boundary", "без межових"),
        ("open_zone", "без відкритих зон"),
        ("after_review", "без доданих після рев'ю"),
        ("in_profit", "без розкриттів уже в плюсі"),
    ):
        subset(nm, lambda r, fl=fl: fl not in r.flags)
    subset("без 10x-драбин, 07-07 і 08-15", lambda r: not r.flags & {"ladder10x", "example", "reader_call"})
    for k in ("disclosure", "setup", "conditional"):
        subset(f"тип: {k}", lambda r, k=k: r.kind == k)
    for s in ("long", "short"):
        subset(f"сторона: {s}", lambda r, s=s: r.call.side == s)
    subset("лише BTC", lambda r: r.call.asset == "BTC")
    subset("без BTC", lambda r: r.call.asset != "BTC")
    hype_touched = _hype_touched(rows, series)
    subset("одна ідея (ланцюги)", lambda r: not r.chain_head or (r.chain_head.endswith("?") and hype_touched))
    top = sorted(zip(prim["rows"], prim["values"], strict=True), key=lambda x: -x[1])[:5]
    top_ids = {r.id for r, _ in top}
    subset("без 5 найкращих", lambda r: r.id not in top_ids)
    implicit = [run_call(r, series, base, implicit=True) for r in prim_rows]
    emit(row_fmt("з неявними скасуваннями", stats(prim_rows, implicit)))
    for gname, keep in (
        ("+ хеджі за ланцюгом (хедж лише за словом)", lambda r: r.group == "main" or "hedge_chain" in r.flags),
        ("+ усі хеджі", lambda r: r.group in ("main", "hedge")),
        ("+ скасовані (вхід до скасування)", lambda r: r.group in ("main", "cancelled")),
        ("група: хеджі", lambda r: r.group == "hedge"),
        ("група: скасовані", lambda r: r.group == "cancelled"),
        ("група: спот", lambda r: r.group == "spot"),
    ):
        rs = [r for r in rows if keep(r) and r.call.stop is not None]
        trs = [run_call(r, series, base, cancel=True) for r in rs]
        ok = [(r, t) for r, t in zip(rs, trs, strict=True) if t.status not in ("no_data", "pending")]
        if len(ok) >= 2:
            emit(row_fmt(gname, stats([r for r, _ in ok], [t for _, t in ok])))
    emit(f"\nНайкращі 5 (виключено вище): {', '.join(f'{r.id} {x:+.2f}' for r, x in top)}")
    # benchmarks
    emit()
    emit("## Бенчмарки")
    bh, now_r = [], []
    for r in prim_rows:
        bars, times = series[r.call.asset]
        w = window(bars, times, r.t0, (FILL_H + HOLD_H) * H)
        bh.append(w[-1].close / w[0].open - 1 - COST)
        c = r.call
        t = simulate(
            w,
            Plan(c.side, "market", None, c.stop, c.target),
            start_ms=r.t0,
            fill_ms=H,
            hold_ms=HOLD_H * H,
            cost_frac=COST,
            sorted_bars=True,
        )
        if t.r_net is not None:
            now_r.append(t.r_net)
    emit(
        f"- купив і тримав (лонг активу, {FILL_H + HOLD_H} год від заклику, мінус витрати): середнє {statistics.fmean(bh) * 100:+.2f} % "
        f"на номінал проти {prim['ret_net'] * 100:+.2f} % на угоду трейдера (заповнені)"
    )
    emit(f"- «одразу» (ринок у момент заклику, ті самі стоп і ціль): E[R] {statistics.fmean(now_r):+.3f} R (n {len(now_r)})")
    # per asset
    emit()
    emit("## За активами (5a)")
    emit()
    emit("| Актив | n | E[R] | 95 % CI |")
    emit("|---|---|---|---|")
    by_a: dict[str, list[tuple[Row, float]]] = defaultdict(list)
    for r, x in zip(prim["rows"], prim["values"], strict=True):
        by_a[r.call.asset].append((r, x))
    means, ses = [], []
    for a, lst in sorted(by_a.items(), key=lambda kv: -len(kv[1])):
        xs = [x for _, x in lst]
        if len(xs) >= 5:
            lo, hi = cluster_bootstrap_ci(xs, [week_of(r.t0) for r, _ in lst], n_boot=4000, seed=SEED)
            means.append(statistics.fmean(xs))
            ses.append(max(1e-9, statistics.stdev(xs) / math.sqrt(len(xs))))
            emit(f"| {a} | {len(xs)} | {statistics.fmean(xs):+.3f} | [{lo:+.3f}; {hi:+.3f}] |")
        else:
            emit(f"| {a} | {len(xs)} | {statistics.fmean(xs):+.3f} | — |")
    if len(means) >= 2:
        q, df, i2 = cochran_q(means, ses)
        labels = [r.call.asset if len(by_a[r.call.asset]) >= 5 else "other" for r in prim["rows"]]
        emit(
            f"\nCochran Q = {q:.2f} (df {df}), I² = {i2:.0%}; перестановка міток активів: p = "
            f"{label_permutation_p(prim['values'], labels, n=2000, seed=SEED):.3f}"
        )
    first = {a for a, lst in by_a.items() if sum(r.call.day < SPLIT for r, _ in lst) >= 5 and sum(r.call.day >= SPLIT for r, _ in lst) >= 5}
    emit(
        f"Стійкість (активи з ≥ 5 закликами в обох половинах): {len(first)} — "
        + ("тест рангів не має сенсу (< 4 активів)" if len(first) < 4 else "див. таблицю")
    )
    emit()
    emit("## Рядки первинної вибірки")
    emit()
    emit("| id | тип | статус | вхід | вихід | R net |")
    emit("|---|---|---|---|---|---|")
    for r, t in usable:
        emit(
            f"| {r.id} | {r.kind} | {t.status} | {t.fill if t.fill is not None else '—'} | {t.exit if t.exit is not None else '—'} | "
            f"{'—' if t.r_net is None else f'{t.r_net:+.2f}'} |"
        )
    text = "\n".join(lines) + "\n"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    if smoke:
        print(f"smoke ok (synthetic bars): {len(lines)} report lines -> {out}; peak rss {_rss_mb():.0f} MB")
        return
    print("\n".join(lines[:40]))
    print(f"... full output -> {out}; peak rss {_rss_mb():.0f} MB")


def _hype_touched(rows: list[Row], series) -> bool:
    """Whether HYPE 48-52 (row 2026-05-29) was touched before 2026-07-28's call (card: then the two are separate)."""
    a = next((r for r in rows if r.id == "KX-2026-05-29-HYPE-LONG"), None)
    b = next((r for r in rows if r.id == "KX-2026-07-28-HYPE-LONG"), None)
    if not a or not b or "HYPE" not in series:
        return False
    bars, _ = series["HYPE"]
    return any(a.t0 <= x.t < b.t0 and x.low <= a.call.zone_high for x in bars)


def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("coverage", "full"), required=True)
    ap.add_argument("--cache-dir", type=Path, default=CACHE)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--perms", type=int, default=PERMS)
    ap.add_argument("--smoke", action="store_true", help="full pipeline on 5 rows; prints no outcome metric")
    a = ap.parse_args()
    rows, errors = load_rows()
    if a.stage == "coverage":
        stage_coverage(rows, errors, a.cache_dir.expanduser())
    else:
        stage_full(rows, a.cache_dir.expanduser(), a.out.expanduser() if a.out else None, a.perms, a.smoke)


if __name__ == "__main__":
    main()
