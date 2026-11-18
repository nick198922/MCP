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


def load_trades(db: str, cut: int) -> tuple[list[dict[str, Any]], Counter[tuple[str, str]]]:
    """(closed S38 / S39 / S40 rows closed before ``cut``, counts of every row by (setup, status) signalled before it)."""
    con = cmn.connect_ro(db)
    try:
        con.row_factory = sqlite3.Row
        q = f"SELECT * FROM demo_trade WHERE setup IN ({','.join('?' * len(SETUPS))}) ORDER BY signal_ts"
        rows = [dict(r) for r in con.execute(q, SETUPS)]
    finally:
        con.close()
    status = Counter((r["setup"], str(r["status"])) for r in rows if int(r["signal_ts"]) < cut)
    closed = [r for r in rows if r["status"] == "closed" and r.get("closed_ts") is not None and int(r["closed_ts"]) < cut]
    return closed, status


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cutoff", default=CUTOFF)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--out", default=DEFAULT_CACHE + "/run")
    ap.add_argument("--now-ms", type=int, default=None)
    ap.add_argument("--sample", type=int, default=2, help="smoke: the last N closed trades of each setup")
    a = ap.parse_args(argv)
    cut = cutoff_ms(a.cutoff)
    cache = Path(a.cache_dir).expanduser()
    if a.stage == "smoke":
        return smoke(a.db, cut, cache, a.sample)
    now = a.now_ms if a.now_ms is not None else int(time.time() * 1000)
    return run(a.db, cut, cache, now, Path(a.out).expanduser())


if __name__ == "__main__":
    sys.exit(main())
