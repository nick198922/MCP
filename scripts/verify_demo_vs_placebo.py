"""Demo S38 / S39 / S40 vs a placebo on the same candle (IDEA-20261030-demo-setups-depth, card section «Попередня
реєстрація — S38 / S39 / S40 проти плацебо тієї самої свічки»).

    uv run python scripts/verify_demo_vs_placebo.py --stage smoke [--sample 3]
    uv run python scripts/verify_demo_vs_placebo.py --stage run [--now-ms MS]

Closed demo trades are read over a ``mode=ro`` SQLite connection (no write). Candles come through the runtime providers
(``rt.exchanges``: Bybit OHLCV and v5 daily klines, paged, rate-limited) into a per-file JSON cache under
``--cache-dir``. ``smoke`` prints counts and coverage only (no R, no Δ); ``run`` prints the registered tables and writes
the rows to ``--out``. Read-only public data: nothing here places, changes or cancels an order.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)
import demo_vs_backtest as dvb  # read-only loaders, paged venue fetch, Bybit daily fetch
import verify_s12_demo_vs_passport as v12  # per-file JSON caches of the two fetches

from tradedesk.analytics import demo_placebo as dp
from tradedesk.analytics.demo_vs_backtest import DemoTrade, be_rules, bybit_linear_id, day_turnover
from tradedesk.analytics.liquidation_flush import holm
from tradedesk.analytics.s38_modules import draw_coins
from tradedesk.analytics.setup_signals import TF_MS
from tradedesk.analytics.stop_management import Fees
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.execution.demo_trader import DEFAULT_DB, hold_ms  # read-only constants
from tradedesk.execution.guard import BE_PATH, CRYPTO_KINDS, STABLES, universe  # read-only constants / pure ranking
from tradedesk.runtime import Runtime

SETUPS = ("S38", "S39", "S40")
CUTOFF = "2026-11-18T17:00"  # registered: demo trades closed before this UTC minute
K_MAIN, K_WIDE = 3, 10  # placebos per trade: registered K = 3 (the first three of the K = 10 draw)
TOP_N, MIN_TURNOVER = 100, 5e6  # pool: guard.universe on the previous UTC day's Bybit turnover
TARGET_R = 2.0
MIN_N, MDE_MAX, BAND = 30, 0.30, 0.30
SEED, N_BOOT = 20261030, 10_000
HOUR_MS, DAY_MS = 3_600_000, 86_400_000
ATR_BARS = 60  # signal-timeframe bars fetched up to the signal bar (Wilder ATR14 seeds after 15)
DEFAULT_CACHE = "~/.cache/tradedesk-agents/demo-vs-placebo"


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def cutoff_ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000)


def read_rows(db: str, setups: tuple[str, ...] = SETUPS) -> list[dict[str, Any]]:
    con = cmn.connect_ro(db)
    try:
        con.row_factory = sqlite3.Row
        q = f"SELECT * FROM demo_trade WHERE setup IN ({','.join('?' * len(setups))}) ORDER BY signal_ts"
        return [dict(r) for r in con.execute(q, setups)]
    finally:
        con.close()


def load_trades(db: str, cut: int) -> tuple[list[dict[str, Any]], Counter[tuple[str, str]]]:
    """(closed S38 / S39 / S40 rows closed before ``cut``, counts of every row by (setup, status) signalled before it)."""
    rows = read_rows(db)
    status = Counter((r["setup"], str(r["status"])) for r in rows if int(r["signal_ts"]) < cut)
    closed = [r for r in rows if r["status"] == "closed" and r.get("closed_ts") is not None and int(r["closed_ts"]) < cut]
    return closed, status


def load_window(db: str, setups: tuple[str, ...], signal_before: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Protocol v2: (closed, other terminal, blocking) rows of ``setups`` signalled before ``signal_before``."""
    return dp.select_signal_window(read_rows(db, setups), setups, signal_before)


def counts(db: str, setups: tuple[str, ...], signal_before: int) -> int:
    """Counts only (no R, no network): the signal_ts window and the latest boundary with every row in it terminal."""
    rows = read_rows(db, setups)
    print(f"now {iso(int(time.time() * 1000))} UTC; rows of {','.join(setups)}: {len(rows)}; boundary {iso(signal_before)} UTC")
    for s in setups:
        mine = [r for r in rows if r["setup"] == s]
        first_block = min((int(r["signal_ts"]) for r in mine if dp.select_signal_window([r], (s,), 1 << 62)[2]), default=None)
        closed, other, block = dp.select_signal_window(mine, (s,), signal_before)
        print(
            f"  {s}: all statuses {dict(Counter(str(r['status']) for r in mine))}; earliest blocking signal "
            f"{iso(first_block) if first_block is not None else '—'} UTC"
        )
        print(
            f"     window: closed {len(closed)} (r_multiple present {sum(r.get('r_multiple') is not None for r in closed)}), "
            f"other terminal {dict(Counter(str(r['status']) for r in other))}, blocking {len(block)}; "
            f"signal days {len({int(r['signal_ts']) // DAY_MS for r in closed})}, ISO weeks {len({dp.week_key(int(r['signal_ts'])) for r in closed})}, "
            f"tf {dict(Counter(r['timeframe'] for r in closed))}, version {dict(Counter(str(r.get('version')) for r in closed))}"
        )
        if closed:
            print(
                f"     signals {iso(int(closed[0]['signal_ts']))} … {iso(max(int(r['signal_ts']) for r in closed))} UTC, "
                f"last close {iso(max(int(r['closed_ts']) for r in closed))} UTC, placebo horizon ends "
                f"{iso(max(int(r['signal_ts']) + TF_MS[r['timeframe']] + hold_ms(s, r['timeframe']) for r in closed))} UTC"
            )
    return 0


def open_sources(cache: Path) -> tuple[Runtime, Builder, dvb.Fetch, dvb.DayFetch]:
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()
    return rt, b, v12.cached(dvb.venue_fetch(rt, b, "bybit"), cache, "bybit"), v12.cached_day(dvb.bybit_day_fetch(rt, b), cache)


async def crypto_perps(rt: Runtime, b: Builder) -> list[tuple[str, str]]:
    """(ccxt symbol, Bybit symbolType) of the Bybit USDT linear perpetuals listed now (survivorship: see the card)."""
    m = await b.guard("markets:bybit", "ccxt:bybit", rt.exchanges.markets("bybit"))
    out = []
    for sym, x in (m or {}).items():
        if not isinstance(x, dict) or not sym.endswith("/USDT:USDT") or not x.get("swap") or not x.get("linear"):
            continue
        kind = str((x.get("info") or {}).get("symbolType") or "")
        if kind in CRYPTO_KINDS and sym.split("/")[0] not in STABLES and bybit_linear_id(sym):
            out.append((sym, kind))
    return sorted(out)


async def day_turnovers(perps: list[tuple[str, str]], fday: dvb.DayFetch, d0: int, d1: int, now: int) -> dict[str, dict[int, float]]:
    """Previous-day turnovers {symbol: {day_ms: USD}} for days d0..d1 (one request per symbol, cached)."""
    out: dict[str, dict[int, float]] = {}
    t0 = time.time()
    for i, (sym, _) in enumerate(perps):
        kl = await fday(bybit_linear_id(sym) or "", d0, d1 + DAY_MS - 1)
        out[sym] = {d: v for d in range(d0, d1 + 1, DAY_MS) if (v := day_turnover(kl, d, now)) is not None}
        if (i + 1) % 100 == 0:
            print(f"  daily turnover {i + 1}/{len(perps)}  {time.time() - t0:.0f}s  peak RSS {cmn.rss_mb():.0f} MB", flush=True)
    return out


def pool_for(day: int, perps: list[tuple[str, str]], turn: dict[str, dict[int, float]], exclude: str) -> list[str]:
    tick = [(s, turn.get(s, {}).get(day - DAY_MS, 0.0), k) for s, k in perps]
    return [s for s in universe(tick, size=TOP_N, min_turnover_usd=MIN_TURNOVER) if s != exclude]


async def coin_bars(fetch: dvb.Fetch, sym: str, tf: str, sig: int, hold: int, now: int) -> tuple[Any, Any]:
    tf_ms = TF_MS[tf]
    close = sig + tf_ms
    bars = dvb.to_bars(await fetch(sym, tf, sig - (ATR_BARS - 1) * tf_ms, close - 1), tf_ms)
    path = dvb.to_bars(await fetch(sym, "1h", close, min(close + hold, now) - 1), HOUR_MS)
    return bars, path


async def evaluate(t: dict[str, Any], perps: list, turn: dict, fetch: dvb.Fetch, now: int, fees_k: tuple[float, ...]) -> dict[str, Any]:
    """One demo trade: its ATR geometry, the pool, K_WIDE placebo outcomes (fees x each of ``fees_k``) and the signal
    coin simulated with the placebo construction. R values stay in the returned row; ``smoke`` never prints them."""
    d = DemoTrade.from_row(t)
    tf = d.timeframe
    hold = hold_ms(d.setup, tf)
    sig = d.signal_ts
    _, rule, _ = be_rules(d.opened_ts if d.opened_ts is not None else sig + TF_MS[tf], d.leverage, be_frac=BE_PATH)
    row: dict[str, Any] = {
        "link_id": d.link_id,
        "setup": d.setup,
        "symbol": d.symbol,
        "side": d.side,
        "tf": tf,
        "signal_ts": sig,
        "day": sig // DAY_MS,
        "version": t.get("version"),
        "r_demo": d.r_multiple,
        "closed_ts_raw": t.get("closed_ts"),
        "be_rule": rule.name,
    }
    bars, path = await coin_bars(fetch, d.symbol, tf, sig, hold, now)
    a = dp.atr_at(bars, sig)
    k = dp.stop_atr_mult(d.entry, d.stop, a) if a is not None else None
    row.update(atr=a, k_atr=k, status="ok")
    if k is None:
        row["status"] = "no_signal_atr"
        return row
    row["sim_signal"] = {
        str(f): dataclasses.asdict(
            dp.placebo_outcome(d.side, bars, path, sig, k, hold, rule, now, target_r=TARGET_R, fees=Fees().scaled(f))
        )
        for f in fees_k
    }
    pool = pool_for(sig // DAY_MS * DAY_MS, perps, turn, d.symbol)
    row["pool_n"] = len(pool)
    coins = draw_coins(d.link_id, pool, K_WIDE)
    pl = []
    for c in coins:
        cb, cp = await coin_bars(fetch, c, tf, sig, hold, now)
        pl.append(
            {
                "coin": c,
                **{
                    str(f): dataclasses.asdict(
                        dp.placebo_outcome(d.side, cb, cp, sig, k, hold, rule, now, target_r=TARGET_R, fees=Fees().scaled(f))
                    )
                    for f in fees_k
                },
            }
        )
    row["placebo"] = pl
    return row


def day_span(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """(first, last) daily-candle open needed: the day before the first signal day … the last signal day."""
    days = sorted({int(r["signal_ts"]) // DAY_MS * DAY_MS for r in rows})
    return (days[0] - DAY_MS, days[-1]) if days else (0, -1)


async def collect(
    rows: list[dict[str, Any]], cache: Path, now: int, fees_k: tuple[float, ...], span: tuple[int, int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rt, b, fetch, fday = open_sources(cache)
    try:
        perps = await crypto_perps(rt, b)
        t0 = time.time()
        turn = await day_turnovers(perps, fday, span[0], span[1], now) if span[1] >= span[0] else {}
        meta = {"perps": len(perps), "turnover_s": round(time.time() - t0, 1)}
        out = []
        t1 = time.time()
        for i, r in enumerate(rows):
            out.append(await evaluate(r, perps, turn, fetch, now, fees_k))
            if (i + 1) % 10 == 0:
                print(f"  trades {i + 1}/{len(rows)}  {time.time() - t1:.0f}s  peak RSS {cmn.rss_mb():.0f} MB", flush=True)
        meta.update(trades_s=round(time.time() - t1, 1), missing=[str(x) for x in b.missing][:20], n_missing=len(b.missing))
        return out, meta
    finally:
        await rt.aclose()


# ---------------------------------------------------------------- statistics of the run stage


def deltas(
    rows: list[dict[str, Any]], k: int, fee: str = "1.0", *, sim: bool = False, skip_open: bool = False
) -> list[tuple[float, int, dict]]:
    """(Δ, signal day, row) of every usable trade: demo R (or the signal coin simulated, ``sim``) − mean of the first
    ``k`` placebos with an R."""
    out = []
    for r in rows:
        if r["status"] != "ok":
            continue
        pls = [p[fee] for p in r["placebo"][:k]]
        if skip_open and any(p["status"] == "open" for p in pls):
            continue
        base = r["sim_signal"][fee]["r"] if sim else r["r_demo"]
        dlt, _ = dp.trade_delta(base, [p["r"] for p in pls])
        if dlt is not None:
            out.append((dlt, r["day"], r))
    return out


def line(name: str, ds: list[tuple[float, int, dict]]) -> tuple[str, dict[str, Any]]:
    s = dp.summarize([x[0] for x in ds], [x[1] for x in ds], n_boot=N_BOOT, seed=SEED)
    f = lambda v: "—" if v is None else f"{v:+.3f}"  # noqa: E731
    ci = "—" if s["ci"] is None else f"[{s['ci'][0]:+.3f}; {s['ci'][1]:+.3f}]"
    return f"  {name:<34} n {s['n']:>3} дн {s['clusters']:>3}  Δ {f(s['mean'])} {ci}  MDE80 {f(s['mde80'])}", s


def report(rows: list[dict[str, Any]], meta: dict[str, Any], now: int) -> list[str]:  # noqa: PLR0915 - one linear report
    out = [f"demo vs placebo, cutoff {CUTOFF} UTC, now {iso(now)} UTC ({now}), perps {meta.get('perps')}"]
    st = Counter((r["setup"], r["status"]) for r in rows)
    out.append(
        "usable / excluded by setup: "
        + ", ".join(f"{s} {st[(s, 'ok')]} / {sum(v for (a, b), v in st.items() if a == s and b != 'ok')}" for s in SETUPS)
    )
    pst = Counter(p["1.0"]["status"] for r in rows if r["status"] == "ok" for p in r["placebo"][:K_MAIN])
    out.append(f"placebo statuses (K=3): {dict(pst)}")
    out.append("\nОсновне (Δ = R демо − середнє R трьох плацебо; CI — кластерний бутстреп за днем сигналу):")
    main = {}
    for s in SETUPS:
        txt, sm = line(s, [x for x in deltas(rows, K_MAIN) if x[2]["setup"] == s])
        main[s] = sm
        out.append(txt)
    eligible = [s for s in SETUPS if main[s]["n"] >= MIN_N and main[s]["p_gt0"] is not None]
    adj = dict(zip(eligible, holm([main[s]["p_gt0"] for s in eligible]), strict=True)) if eligible else {}
    out.append(
        "Вердикти: "
        + ", ".join(
            f"{s} {dp.verdict(main[s], min_n=MIN_N, mde_max=MDE_MAX, band=BAND, p_holm=adj.get(s))}"
            + (f" (Holm p {adj[s]:.3f})" if s in adj else "")
            for s in SETUPS
        )
    )
    out.append("\nОписово:")
    for name, sel in (("шорти S38 + S39", lambda r: r["setup"] in ("S38", "S39")), ("усі три", lambda r: True)):
        out.append(line(name, [x for x in deltas(rows, K_MAIN) if sel(x[2])])[0])
    for s in SETUPS:
        sub = [x for x in deltas(rows, K_MAIN) if x[2]["setup"] == s]
        if not sub:
            continue
        mean_demo = [x[2]["r_demo"] for x in sub]
        mean_pl = [x[2]["r_demo"] - x[0] for x in sub]
        out.append(f"  {s}: середнє R демо {sum(mean_demo) / len(mean_demo):+.3f}, середнє плацебо {sum(mean_pl) / len(mean_pl):+.3f}")
        out.append(line(f"{s} K=10", [x for x in deltas(rows, K_WIDE) if x[2]["setup"] == s])[0])
        out.append(line(f"{s} без плацебо 'open'", [x for x in deltas(rows, K_MAIN, skip_open=True) if x[2]["setup"] == s])[0])
        out.append(line(f"{s} Δ_sim (монета сигналу в моделі)", [x for x in deltas(rows, K_MAIN, sim=True) if x[2]["setup"] == s])[0])
        out.append(line(f"{s} Δ_sim комісії x1.5", [x for x in deltas(rows, K_MAIN, "1.5", sim=True) if x[2]["setup"] == s])[0])
        top = sorted(sub, key=lambda x: -abs(x[0]))[3:]
        out.append(line(f"{s} без топ-3 |Δ|", top)[0])
        days = sorted({x[1] for x in sub})
        half = days[len(days) // 2] if days else 0
        out.append(line(f"{s} перша половина днів", [x for x in sub if x[1] < half])[0])
        out.append(line(f"{s} друга половина днів", [x for x in sub if x[1] >= half])[0])
        by_v: dict[str, list] = defaultdict(list)
        for x in sub:
            by_v[str(x[2]["version"])].append(x)
        for v, xs in sorted(by_v.items()):
            out.append(line(f"{s} версія {v}", xs)[0])
        by_c: dict[str, list] = defaultdict(list)
        for x in sub:
            by_c[x[2]["symbol"].split("/")[0]].append(x[0])
        big = {c: v for c, v in by_c.items() if len(v) >= 3}
        q = cochran(big)
        out.append(
            f"  {s} монети з ≥ 3 угодами: {len(big)}; "
            + ", ".join(f"{c} n{len(v)} {sum(v) / len(v):+.2f}" for c, v in sorted(big.items()))
            + (f"; Cochran Q {q[0]:.2f} df {q[1]}, I² {q[2]:.2f}" if q else "")
        )
    return out


SEEN_CUTOFF = "2026-11-18T17:00"  # protocol v2: trades closed before the 18.11 run were seen there (descriptive split)


def line_v2(name: str, ds: list[tuple[float, int, dict]], min_cl: int = 10) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Protocol v2 row: day-clustered bootstrap and CR1 t by day and by ISO week; intervals only from ``min_cl`` days."""
    vals = [x[0] for x in ds]
    s = dp.summarize(vals, [x[1] for x in ds], n_boot=N_BOOT, seed=SEED)
    td = dp.cluster_t(vals, [x[1] for x in ds])
    tw = dp.cluster_t(vals, [dp.week_key(int(x[2]["signal_ts"])) for x in ds])
    f = lambda v: "—" if v is None else f"{v:+.3f}"  # noqa: E731
    g = lambda c: "—" if c is None else f"[{c[0]:+.3f}; {c[1]:+.3f}]"  # noqa: E731
    txt = f"  {name:<40} n {s['n']:>3} дн {s['clusters']:>3} тиж {tw['clusters']:>2}  Δ {f(s['mean'])}"
    if s["clusters"] >= min_cl:
        txt += (
            f"  boot {g(s['ci'])} MDE80 {f(s['mde80'])} p {s['p_gt0']:.3f} | t_day {g(td['ci'])} MDE80 {f(td['mde80'])} "
            f"p {td['p_gt0']:.3f} | t_week(df {tw['df']}) {g(tw['ci'])}"
        )
    else:
        txt += "  (< 10 днів: без CI)"
    return txt, s, td


def report_v2(rows: list[dict[str, Any]], meta: dict[str, Any], now: int, label: str, setups: tuple[str, ...]) -> list[str]:  # noqa: PLR0915
    """Registered tables of the 2026-11-25 registration (protocol v2): K = 10 is the main benchmark."""
    out = [f"demo vs placebo, protocol v2, {label}, now {iso(now)} UTC ({now}), perps {meta.get('perps')}, K main {K_WIDE}"]
    st = Counter((r["setup"], r["status"]) for r in rows)
    out.append(
        "usable / excluded: "
        + ", ".join(f"{s} {st[(s, 'ok')]} / {sum(v for (a, b), v in st.items() if a == s and b != 'ok')}" for s in setups)
    )
    for k in (K_WIDE, K_MAIN):
        pst = Counter(p["1.0"]["status"] for r in rows if r["status"] == "ok" for p in r["placebo"][:k])
        out.append(f"placebo statuses (K={k}): {dict(pst)}")
    out.append("\nОсновне (Δ = R демо − середнє R десяти плацебо; boot — кластерний бутстреп за днем; t — CR1, df G−1):")
    main: dict[str, tuple[dict, dict]] = {}
    for s in setups:
        txt, sm, td = line_v2(s, [x for x in deltas(rows, K_WIDE) if x[2]["setup"] == s])
        main[s] = (sm, td)
        out.append(txt)
    elig = [s for s in setups if main[s][0]["n"] >= MIN_N and main[s][0]["clusters"] >= 10 and main[s][1]["p_gt0"] is not None]
    adj = dict(zip(elig, holm([max(main[s][0]["p_gt0"], main[s][1]["p_gt0"]) for s in elig]), strict=True)) if elig else {}
    out.append(
        "Вердикти: "
        + ", ".join(
            f"{s} {dp.verdict_v2(*main[s], min_n=MIN_N, mde_max=MDE_MAX, band=BAND, p_holm=adj.get(s))}"
            + (f" (Holm p {adj[s]:.3f}, family {len(elig)})" if s in adj else "")
            for s in setups
        )
    )
    seen_cut = cutoff_ms(SEEN_CUTOFF)
    out.append("\nОписово:")
    for s in setups:
        sub = [x for x in deltas(rows, K_WIDE) if x[2]["setup"] == s]
        if not sub:
            continue
        rd = [x[2]["r_demo"] for x in sub]
        rp = [x[2]["r_demo"] - x[0] for x in sub]
        out.append(f"  {s}: середнє R демо {sum(rd) / len(rd):+.3f}, середнє плацебо K=10 {sum(rp) / len(rp):+.3f}")
        rows_d = [
            (f"{s} K=3", [x for x in deltas(rows, K_MAIN) if x[2]["setup"] == s]),
            (f"{s} без плацебо 'open'", [x for x in deltas(rows, K_WIDE, skip_open=True) if x[2]["setup"] == s]),
            (f"{s} Δ_sim (монета сигналу в моделі)", [x for x in deltas(rows, K_WIDE, sim=True) if x[2]["setup"] == s]),
            (f"{s} Δ_sim комісії x1.5", [x for x in deltas(rows, K_WIDE, "1.5", sim=True) if x[2]["setup"] == s]),
            (f"{s} без топ-3 |Δ|", sorted(sub, key=lambda x: -abs(x[0]))[3:]),
        ]
        days = sorted({x[1] for x in sub})
        half = days[len(days) // 2] if days else 0
        rows_d += [
            (f"{s} перша половина днів", [x for x in sub if x[1] < half]),
            (f"{s} друга половина днів", [x for x in sub if x[1] >= half]),
            (f"{s} бачені 18.11 (закриті < {SEEN_CUTOFF})", [x for x in sub if int(x[2]["closed_ts_raw"]) < seen_cut]),
            (f"{s} нові", [x for x in sub if int(x[2]["closed_ts_raw"]) >= seen_cut]),
        ]
        by_v: dict[str, list] = defaultdict(list)
        for x in sub:
            by_v[str(x[2]["version"])].append(x)
        rows_d += [(f"{s} версія {v}", xs) for v, xs in sorted(by_v.items())]
        own = [(x[2]["r_demo"] - x[2]["sim_signal"]["1.0"]["r"], x[1], x[2]) for x in sub if x[2]["sim_signal"]["1.0"]["r"] is not None]
        rows_d.append((f"{s} R демо − модель монети сигналу", own))
        for name, ds in rows_d:
            out.append(line_v2(name, ds)[0])
        per_day: dict[int, list[float]] = defaultdict(list)
        for x in sub:
            per_day[x[1]].append(x[0])
        loo = [(d, sum(x[0] for x in sub if x[1] != d) / (len(sub) - len(v))) for d, v in per_day.items() if len(sub) > len(v)]
        big = max(per_day, key=lambda d: len(per_day[d]))
        out.append(
            f"  {s} без одного дня: Δ від {min(v for _, v in loo):+.3f} до {max(v for _, v in loo):+.3f}; "
            f"найбільший день {iso(big * DAY_MS)[:10]} ({len(per_day[big])} угод), без нього {dict(loo)[big]:+.3f}"
        )
        by_c: dict[str, list] = defaultdict(list)
        for x in sub:
            by_c[x[2]["symbol"].split("/")[0]].append(x[0])
        bigc = {c: v for c, v in by_c.items() if len(v) >= 3}
        q = cochran(bigc)
        out.append(
            f"  {s} монет {len(by_c)} на {len(sub)} угод; з ≥ 3 угодами: {len(bigc)}; "
            + ", ".join(f"{c} n{len(v)} {sum(v) / len(v):+.2f}" for c, v in sorted(bigc.items()))
            + (f"; Cochran Q {q[0]:.2f} df {q[1]}, I² {q[2]:.2f}" if q else "")
        )
    return out


def cochran(groups: dict[str, list[float]]) -> tuple[float, int, float] | None:
    """Cochran's Q and I² of group means (inverse-variance weights from each group's own variance)."""
    ms, ws = [], []
    for v in groups.values():
        n = len(v)
        m = sum(v) / n
        var = sum((x - m) ** 2 for x in v) / (n - 1)
        if var > 0:
            ms.append(m)
            ws.append(n / var)
    if len(ms) < 2:
        return None
    mu = sum(w * m for w, m in zip(ws, ms, strict=True)) / sum(ws)
    qv = sum(w * (m - mu) ** 2 for w, m in zip(ws, ms, strict=True))
    df = len(ms) - 1
    return qv, df, max(0.0, (qv - df) / qv) if qv > 0 else 0.0


# ---------------------------------------------------------------- stages


def smoke(db: str, cut: int, cache: Path, sample: int) -> int:
    rows, status = load_trades(db, cut)
    print(f"cutoff {CUTOFF} UTC; closed S38/S39/S40 rows: {len(rows)}")
    for s in SETUPS:
        sel = [r for r in rows if r["setup"] == s]
        print(
            f"  {s}: closed {len(sel)}, r_multiple present {sum(r.get('r_multiple') is not None for r in sel)}, "
            f"signal days {len({int(r['signal_ts']) // DAY_MS for r in sel})}, tf {dict(Counter(r['timeframe'] for r in sel))}, "
            f"side {dict(Counter(r['side'] for r in sel))}, version {dict(Counter(str(r.get('version')) for r in sel))}"
        )
        print(f"     all statuses (signalled before cutoff): {dict((k[1], v) for k, v in status.items() if k[0] == s)}")
    if rows:
        print(f"  signal range {iso(int(rows[0]['signal_ts']))} … {iso(max(int(r['signal_ts']) for r in rows))} UTC")
    pick = []
    for s in SETUPS:
        pick += [r for r in rows if r["setup"] == s][-sample:]
    now = int(time.time() * 1000)
    t0 = time.time()
    got, meta = asyncio.run(collect(pick, cache, now, (1.0,), day_span(rows)))
    dt = time.time() - t0
    print(
        f"sample: {len(pick)} trades, {dt:.0f}s (daily turnover {meta['turnover_s']}s for {meta['perps']} perps; trades {meta['trades_s']}s), "
        f"provider failures {meta['n_missing']}, peak RSS {cmn.rss_mb():.0f} MB"
    )
    for r in got:  # coverage only: no R, no Δ
        pl = r.get("placebo", [])
        print(
            f"  {r['setup']} {r['symbol'].split('/')[0]:<10} {iso(r['signal_ts'])} status {r['status']}, pool {r.get('pool_n')}, "
            f"k_atr {'ok' if r.get('k_atr') else '—'}, signal sim {r.get('sim_signal', {}).get('1.0', {}).get('status')}, "
            f"placebo statuses K=3 {dict(Counter(p['1.0']['status'] for p in pl[:K_MAIN]))}, K=10 {dict(Counter(p['1.0']['status'] for p in pl))}"
        )
    per = meta["trades_s"] / max(1, len(pick))
    print(f"estimate for the run: {len(rows)} trades x {per:.1f}s ≈ {len(rows) * per / 60:.1f} min after the turnover step (cache reused)")
    return 0


def run(db: str, cut: int, cache: Path, now: int, out_dir: Path) -> int:
    rows, _ = load_trades(db, cut)
    print(f"cutoff {CUTOFF}; closed rows {len(rows)}; now {iso(now)} ({now})", flush=True)
    got, meta = asyncio.run(collect(rows, cache, now, (1.0, 1.5), day_span(rows)))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rows.json").write_text(
        json.dumps({"now_ms": now, "cutoff": CUTOFF, "meta": meta, "rows": got}, default=str), encoding="utf-8"
    )
    txt = "\n".join(report(got, meta, now))
    (out_dir / "report.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)
    print(f"peak RSS {cmn.rss_mb():.0f} MB; rows -> {out_dir / 'rows.json'}")
    return 0


def window_rows(db: str, setups: tuple[str, ...], sb: int) -> list[dict[str, Any]] | None:
    closed, other, block = load_window(db, setups, sb)
    print(
        f"signal_ts < {iso(sb)} UTC, setups {','.join(setups)}: closed {len(closed)}, other terminal "
        f"{dict(Counter(str(r['status']) for r in other))}, blocking {len(block)}",
        flush=True,
    )
    if block:
        print("refused: the window holds rows that are not terminal yet (registered rule: wait or move the boundary)")
        return None
    return closed


def smoke_v2(db: str, setups: tuple[str, ...], sb: int, cache: Path, sample: int) -> int:
    """Coverage only (no R, no Δ) on the last ``sample`` trades of the window."""
    rows = window_rows(db, setups, sb)
    if rows is None:
        return 2
    pick = rows[-sample:]
    t0 = time.time()
    got, meta = asyncio.run(collect(pick, cache, int(time.time() * 1000), (1.0,), day_span(rows)))
    print(
        f"sample {len(pick)} trades, {time.time() - t0:.0f}s, perps {meta['perps']}, failures {meta['n_missing']}, peak RSS {cmn.rss_mb():.0f} MB"
    )
    for r in got:
        pl = r.get("placebo", [])
        print(
            f"  {r['setup']} {r['symbol'].split('/')[0]:<10} {iso(r['signal_ts'])} status {r['status']}, pool {r.get('pool_n')}, "
            f"k_atr {'ok' if r.get('k_atr') else '—'}, placebo statuses K=10 {dict(Counter(p['1.0']['status'] for p in pl))}"
        )
    print(f"estimate for the run: {len(rows)} trades x {meta['trades_s'] / max(1, len(pick)):.1f}s after the turnover step")
    return 0


def run_v2(db: str, setups: tuple[str, ...], sb: int, cache: Path, now: int, out_dir: Path) -> int:
    if now > int(time.time() * 1000):
        print(f"refused: --now-ms {iso(now)} UTC is in the future (the registered run waits for it)")
        return 2
    rows = window_rows(db, setups, sb)
    if rows is None:
        return 2
    label = f"signal_ts < {iso(sb)} UTC"
    print(f"{label}; closed rows {len(rows)}; now {iso(now)} ({now})", flush=True)
    got, meta = asyncio.run(collect(rows, cache, now, (1.0, 1.5), day_span(rows)))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rows.json").write_text(
        json.dumps({"now_ms": now, "signal_before": iso(sb), "protocol": "v2", "meta": meta, "rows": got}, default=str), encoding="utf-8"
    )
    txt = "\n".join(report_v2(got, meta, now, label, setups))
    (out_dir / "report.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)
    print(f"peak RSS {cmn.rss_mb():.0f} MB; rows -> {out_dir / 'rows.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("counts", "smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cutoff", default=CUTOFF, help="protocol v1: demo trades closed before this UTC minute")
    ap.add_argument("--signal-before", default=None, help="protocol v2: trades signalled before this UTC minute, all terminal")
    ap.add_argument("--setups", default=",".join(SETUPS), help="protocol v2 / counts: comma-separated setups")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--out", default=DEFAULT_CACHE + "/run")
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--sample", type=int, default=2, help="smoke: the last N closed trades of each setup")
    a = ap.parse_args(argv)
    cut = cutoff_ms(a.cutoff)
    cache = Path(a.cache_dir).expanduser()
    setups = tuple(s for s in a.setups.split(",") if s)
    if a.stage == "counts":
        return counts(a.db, setups, cutoff_ms(a.signal_before) if a.signal_before else int(time.time() * 1000))
    if a.signal_before:  # protocol v2 (registration 2026-11-25)
        sb = cutoff_ms(a.signal_before)
        if a.stage == "smoke":
            return smoke_v2(a.db, setups, sb, cache, a.sample)
        now = a.now_ms if a.now_ms is not None else int(time.time() * 1000)
        return run_v2(a.db, setups, sb, cache, now, Path(a.out).expanduser())
    if a.stage == "smoke":
        return smoke(a.db, cut, cache, a.sample)
    now = a.now_ms if a.now_ms is not None else int(time.time() * 1000)
    return run(a.db, cut, cache, now, Path(a.out).expanduser())


if __name__ == "__main__":
    sys.exit(main())
