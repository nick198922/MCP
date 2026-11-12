"""S12 demo vs the passport of the version traded (IDEA-20261030-demo-setups-depth, items 6 and 9; pre-registered in
the card, section «Попередня реєстрація звірки S12»).

    uv run python scripts/verify_s12_demo_vs_passport.py --stage smoke   # data checks, prints no outcome metric
    uv run python scripts/verify_s12_demo_vs_passport.py --stage run     # the registered comparison

The closed S12 demo trades (``demo_trade``, read over a ``mode=ro`` connection: nothing is written) closed before
``--cutoff`` are grouped by the version they ran under (``analytics/s12_demo_audit.traded_version``) and set against the
passport of that entry rule. Per trade: v4 / v9 trades are replayed with ``analytics/demo_vs_backtest.replay`` (the live
v4 detector, Bybit candles through ``rt.exchanges``, paged as in ``scripts/demo_vs_backtest.py``), v7 trades with
``s12_demo_audit.v7_replay`` (the v7 detector and the passport's limit model); fees and exit slippage against the
backtest convention (``s12_demo_audit.exec_costs``); missed entries and their shadow plans (``analytics/demo_shadow``).
CI: bootstrap clustered by the UTC day of the signal bar (``liquidation_flush.cluster_bootstrap_ci``). R is per trade
risk, not on capital. Every candle response is cached as one JSON file under ``--cache-dir`` (outside the repo).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import resource
import sqlite3
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)
import demo_vs_backtest as dvb  # scripts/demo_vs_backtest.py: read-only loaders, paged venue fetch, turnover

from tradedesk.analytics import demo_shadow
from tradedesk.analytics import s12_demo_audit as audit
from tradedesk.analytics.demo_vs_backtest import LIVE_WINDOW, DemoTrade, Replay, replay
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci
from tradedesk.analytics.setup_signals import TF_MS
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.execution.demo_trader import DEFAULT_DB, hold_ms  # read-only constants
from tradedesk.execution.guard import BE_PATH  # read-only constant
from tradedesk.runtime import Runtime

SETUP = "S12"
CUTOFF = "2026-11-09T18:00"  # registered: trades closed before this UTC minute (35 at registration)
PREV_CUTOFF_MS = int(datetime(2026, 11, 9, 18, tzinfo=UTC).timestamp() * 1000)  # the 09.11 run: later runs report the overlap
SEED = 20261030
N_BOOT = 10_000
MIN_GROUP = 10
HOUR_MS = 3_600_000
DEFAULT_CACHE = "~/.cache/tradedesk-agents/s12-demo-vs-passport"


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def load_s12(db: str, cutoff_ms: int) -> dict[str, list[dict[str, Any]]]:
    """S12 rows by status over a read-only connection: closed before the cutoff, missed and open signalled before it."""
    con = cmn.connect_ro(db)
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM demo_trade WHERE setup = ? ORDER BY signal_ts", (SETUP,))]
    finally:
        con.close()
    out: dict[str, list[dict[str, Any]]] = {"closed": [], "missed": [], "open": [], "orphaned": [], "other": []}
    for r in rows:
        st = r.get("status")
        if st == "closed" and r.get("closed_ts") is not None and int(r["closed_ts"]) < cutoff_ms:
            out["closed"].append(r)
        elif st in ("missed", "open", "orphaned") and int(r["signal_ts"]) < cutoff_ms:
            out[st].append(r)
        elif int(r["signal_ts"]) < cutoff_ms:
            out["other"].append(r)
    return out


def cached(fetch: dvb.Fetch, cache: Path, venue: str) -> dvb.Fetch:
    """The paged venue fetch with one JSON file per (venue, symbol, timeframe, since, until)."""

    async def f(symbol: str, tf: str, since_ms: int, until_ms: int) -> list[list[float]]:
        key = hashlib.sha1(f"{venue}|{symbol}|{tf}|{since_ms}|{until_ms}".encode()).hexdigest()[:16]
        p = cache / "ohlcv" / f"{symbol.split('/')[0]}_{tf}_{key}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        rows = await fetch(symbol, tf, since_ms, until_ms)
        if rows:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rows), encoding="utf-8")
        return rows

    return f


def cached_day(fetch: dvb.DayFetch, cache: Path) -> dvb.DayFetch:
    async def f(sym: str, start_ms: int, end_ms: int) -> list[Any]:
        p = cache / "day" / f"{sym}_{start_ms}_{end_ms}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        rows = await fetch(sym, start_ms, end_ms)
        if rows:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rows), encoding="utf-8")
        return rows

    return f


async def replay_one(row: dict[str, Any], version: str, fetch: dvb.Fetch, now_ms: int) -> tuple[Replay, dict[str, Any] | None]:
    """(the v4-detector replay, the v7 replay or None) of one closed S12 trade."""
    t = DemoTrade.from_row(row)
    tf_ms = TF_MS[t.timeframe]
    hold = hold_ms(t.setup, t.timeframe)
    close = t.signal_ts + tf_ms
    bars = dvb.to_bars(await fetch(t.symbol, t.timeframe, t.signal_ts - (LIVE_WINDOW - 1) * tf_ms, close - 1), tf_ms)
    v7 = None
    if version == "v7":
        end = int(row.get("rest_until") or close) + hold
        path = dvb.to_bars(await fetch(t.symbol, "1h", close, min(end, now_ms) - 1), HOUR_MS)
        v7 = audit.v7_replay(row, bars, path, now_ms=now_ms, hold_ms=hold)
        path4 = path
    else:
        path4 = dvb.to_bars(await fetch(t.symbol, "1h", close, min(close + hold, now_ms) - 1), HOUR_MS)
    return replay(t, bars, path4, now_ms=now_ms, hold_ms=hold, be_frac=BE_PATH), v7


def stat(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    pairs = [(float(r[key]), cmn.ymd_ms(int(r["signal_ts"]))) for r in rows if r.get(key) is not None]
    if not pairs:
        return {"n": 0}
    v = [p[0] for p in pairs]
    d = [p[1] for p in pairs]
    ci = cluster_bootstrap_ci(v, d, n_boot=N_BOOT, seed=SEED) if len(pairs) >= 2 else None
    return {"n": len(v), "days": len(set(d)), "mean": statistics.fmean(v), "ci": ci, "sum": sum(v), "win": sum(x > 0 for x in v) / len(v)}


def fmt(s: dict[str, Any]) -> str:
    if not s.get("n"):
        return "n 0"
    ci = f"[{s['ci'][0]:+.3f}; {s['ci'][1]:+.3f}]" if s.get("ci") else "[—]"
    tag = "" if s["n"] >= MIN_GROUP else "  (n < 10: невимірно)"
    return f"n {s['n']:>2}, днів {s['days']:>2}, середнє {s['mean']:+.3f} {ci}, сума {s['sum']:+.2f}, R>0 {s['win']:.0%}{tag}"


def classify(rows: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        v, src = audit.traded_version(r, now_ms=now_ms)
        out.append({**r, "ver": v, "ver_src": src, "passport": audit.PASSPORT_OF.get(v)})
    return out


def smoke(db: str, cutoff_ms: int, cache: Path, venue: str) -> int:
    """Data checks only: counts, version tags, units, one timed replay of each entry rule (levels, no R)."""
    g = load_s12(db, cutoff_ms)
    closed = classify(g["closed"], cutoff_ms)
    print(
        f"S12 closed < cutoff: {len(closed)}; missed {len(g['missed'])}; open {len(g['open'])}; "
        f"orphaned {len(g['orphaned'])}; other {len(g['other'])}"
    )
    print(f"versions (closed): {sorted(Counter((r['ver'], r['ver_src']) for r in closed).items())}")
    for st in ("missed", "open", "orphaned"):
        print(f"versions ({st}): {sorted(Counter(r['ver'] for r in classify(g[st], cutoff_ms)).items())}")
    v9 = [r for r in closed if r["ver"] == "v9"]
    if v9:
        days = sorted({cmn.ymd_ms(int(r["signal_ts"])) for r in v9})
        prev = sum(int(r["closed_ts"]) < PREV_CUTOFF_MS for r in v9)
        print(f"v9 closed: {len(v9)}, signal days {len(days)} ({days[0]} .. {days[-1]}); closed before the 09.11 cutoff {prev}")
    print(f"timeframes {dict(Counter(r['timeframe'] for r in closed))}; venues {dict(Counter(r['venue'] for r in closed))}")
    print(f"entry kinds {dict(Counter(str(r.get('entry_kind')) for r in closed))}; symbols {len({r['symbol'] for r in closed})}")
    # consistency of tags: the column against the open-time epoch, rest_until against v7
    bad = [r["symbol"] for r in closed if r["ver_src"] == "column" and (r["ver"] == "v7") != (r.get("rest_until") is not None)]
    print(f"tag vs rest_until inconsistent: {bad}")
    miss = [
        k
        for k in ("qty", "risk_usd", "fees_usd", "fill", "exit", "exit_reason", "r_multiple", "pnl_usd")
        if any(r.get(k) is None for r in closed)
    ]
    print(f"columns with a NULL in some closed row: {miss}")
    unit = [abs(float(r["pnl_usd"]) / float(r["risk_usd"]) - float(r["r_multiple"])) for r in closed if r.get("risk_usd")]
    print(f"max |pnl/risk - r_multiple| {max(unit, default=float('nan')):.2e} (units check)")
    picks = [r for r in (next((r for r in closed if r["ver"] == v), None) for v in ("v9", "v7")) if r is not None]
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()

    async def go() -> None:
        fetch = cached(dvb.venue_fetch(rt, b, venue), cache, venue)
        try:
            for r in picks:
                t0 = time.monotonic()
                rp, v7 = await replay_one(r, r["ver"], fetch, int(time.time() * 1000))
                lv = v7 if v7 is not None else rp.as_dict()
                ok = lv.get("verdict") not in ("no_signal", "no_data", "unsupported")
                print(
                    f"{r['ver']} {r['symbol']} {cmn.ymd_ms(int(r['signal_ts']))}: replay found the signal: {ok}; "
                    f"|Δentry| {lv.get('entry_diff_r')}, |Δstop| {lv.get('stop_diff_r')} R; {time.monotonic() - t0:.1f} s"
                )
        finally:
            await rt.aclose()

    asyncio.run(go())
    print(f"missing: {[f'{m.field}: {m.reason}' for m in b.missing[:5]]}; peak RSS {rss_mb():.0f} MB")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cutoff", default=CUTOFF, help="UTC YYYY-MM-DDTHH:MM; trades closed before it")
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--out-tag", default="s12", help="output names <tag>.txt / <tag>_rows.json in the cache dir")
    a = ap.parse_args(argv)
    cutoff_ms = int(datetime.strptime(a.cutoff, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000)
    cache = Path(a.cache_dir).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    if a.stage == "smoke":
        return smoke(a.db, cutoff_ms, cache, a.venue)
    return run(a.db, cutoff_ms, cache, a.venue, a.out_tag)


def run(db: str, cutoff_ms: int, cache: Path, venue: str, tag: str = "s12") -> int:  # noqa: PLR0915 - one linear report
    now = int(time.time() * 1000)
    g = load_s12(db, cutoff_ms)
    closed = classify(g["closed"], cutoff_ms)
    missed = classify(g["missed"], cutoff_ms)
    opened = classify(g["open"], cutoff_ms)
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()

    async def go() -> list[dict[str, Any]]:
        fetch = cached(dvb.venue_fetch(rt, b, venue), cache, venue)
        reps: list[Replay] = []
        v7s: list[dict[str, Any] | None] = []
        try:
            for i, r in enumerate(closed):
                rp, v7 = await replay_one(r, r["ver"], fetch, now)
                reps.append(rp)
                v7s.append(v7)
                print(f"  replay {i + 1}/{len(closed)} {r['symbol']} peak RSS {rss_mb():.0f} MB", flush=True)
            reps = await dvb.add_turnover(closed, reps, cached_day(dvb.bybit_day_fetch(rt, b), cache), now)
        finally:
            await rt.aclose()
        out = []
        for r, rp, v7 in zip(closed, reps, v7s, strict=True):
            d = {
                **rp.as_dict(),
                **{
                    k: r.get(k) for k in ("ver", "ver_src", "passport", "entry_kind", "exit_reason", "rest_until", "opened_ts", "closed_ts")
                },
            }
            d.update(audit.exec_costs(r))
            d["v7"] = v7
            if v7 is not None:  # the v7 trades are judged by the v7 replay, not by the v4 detector
                d["r_bt_model"] = v7.get("r_bt")
                d["bt_exit_model"] = v7.get("bt_exit")
                d["verdict_model"] = v7.get("verdict")
            else:
                d["r_bt_model"] = rp.r_bt
                d["bt_exit_model"] = rp.bt_exit
                d["verdict_model"] = rp.verdict
            out.append(d)
        return out

    rows = asyncio.run(go())
    L: list[str] = []
    p = L.append
    p(
        f"# S12: демо проти паспорта версії; cutoff {datetime.fromtimestamp(cutoff_ms / 1000, UTC):%Y-%m-%d %H:%M} UTC; повтор {datetime.fromtimestamp(now / 1000, UTC):%Y-%m-%d %H:%M} UTC, venue {venue}"
    )
    p(
        f"закритих {len(rows)}; версії {sorted(Counter((r['ver'], r['ver_src']) for r in rows).items())}; missed {len(missed)}; open {len(opened)}"
    )
    p(f"сигнальні дні: {len({cmn.ymd_ms(int(r['signal_ts'])) for r in rows})}; монет {len({r['symbol'] for r in rows})}")
    p("")
    p("## Пункт 6: R демо (од. демо, на ризик угоди, не на капітал) проти паспорта версії")
    groups = {
        "v9 (тег)": lambda r: r["ver"] == "v9",
        "v4 + v9 (вхід v4, один паспорт)": lambda r: r["passport"] == "v4",
        "без тегу: v4 (за часом відкриття)": lambda r: r["ver"] == "v4",
        "без тегу: v7 (rest_until)": lambda r: r["ver"] == "v7",
        "без тегу разом": lambda r: r["ver_src"] != "column",
        f"усі {len(rows)} (змішує два паспорти)": lambda r: True,
    }
    res: dict[str, Any] = {}
    for name, f in groups.items():
        sub = [r for r in rows if f(r)]
        s = stat(sub, "r_demo")
        res[name] = s
        p(f"- {name:<36} {fmt(s)}")
        pk = {r["passport"] for r in sub}
        if len(pk) == 1 and s.get("n"):
            pp = audit.PASSPORTS[next(iter(pk))]
            lo, hi = audit.scaled_band(pp, s["n"])
            ci = s.get("ci")
            diff = f"{s['mean'] - pp.oos_mean:+.3f} [{ci[0] - pp.oos_mean:+.3f}; {ci[1] - pp.oos_mean:+.3f}]" if ci else "—"
            p(
                f"    паспорт {next(iter(pk))} OOS {pp.oos_mean:+.3f}; смуга n=30 i.i.d. [{pp.band_iid[0]:+.3f}; {pp.band_iid[2]:+.3f}], "
                f"ковзна [{pp.band_sliding[0]:+.3f}; {pp.band_sliding[2]:+.3f}] -> {audit.band_verdict(s['mean'], pp)}; "
                f"на n={s['n']} (описово) [{lo:+.3f}; {hi:+.3f}]; демо − OOS {diff}"
            )
    # orphaned (filled, no close found on the venue): not in the sample; scenario «each closed at the initial stop, −1R»
    # as in the checkpoint report (docs/changes/2026-11-12-checkpoint-orphan-stop-scenario.md), descriptive only
    orph = classify(g["orphaned"], cutoff_ms)
    for name, f in (("v9", lambda r: r["ver"] == "v9"), ("v4 + v9", lambda r: r["passport"] == "v4")):
        oo = [dict(r, r_demo=-1.0) for r in orph if f(r)]
        s2 = stat([r for r in rows if f(r)] + oo, "r_demo")
        p(f"- orphaned {name}: {len(oo)}; сценарій «закрито за стопом, −1R» (описово): {fmt(s2) if oo else '—'}")
    p("")
    p("## Стійкість і розбивки (описово; R демо)")
    v49 = [r for r in rows if r["passport"] == "v4"]
    top = sorted(v49, key=lambda r: r["r_demo"] or 0.0, reverse=True)
    for k in (3, 5):
        p(f"- v4 + v9 без топ-{k}: {fmt(stat(top[k:], 'r_demo'))}")
    v9 = [r for r in rows if r["ver"] == "v9"]
    top9 = sorted(v9, key=lambda r: r["r_demo"] or 0.0, reverse=True)
    p(f"- v9 без топ-3: {fmt(stat(top9[3:], 'r_demo'))}")
    p(f"- v9 лише закриті після cutoff 09.11 (нові): {fmt(stat([r for r in v9 if int(r['closed_ts']) >= PREV_CUTOFF_MS], 'r_demo'))}")
    splits = {
        "лонг": lambda r: r["side"] == "long",
        "шорт": lambda r: r["side"] == "short",
        "вхід мейкер": lambda r: r.get("entry_kind") == "maker",
        "вхід тейкер": lambda r: r.get("entry_kind") == "taker",
        "оборот ≥ $5M": lambda r: r.get("turnover_group") == "ge5m",
        "оборот $1–5M": lambda r: r.get("turnover_group") == "1to5m",
        "оборот < $1M": lambda r: r.get("turnover_group") == "lt1m",
        "оборот невідомий": lambda r: r.get("turnover_group") not in ("ge5m", "1to5m", "lt1m"),
    }
    for name, f in splits.items():
        p(f"- v4+v9 {name:<18} {fmt(stat([r for r in v49 if f(r)], 'r_demo'))}")
    p(f"- угод на монету (v4+v9): {sorted(Counter(Counter(r['symbol'] for r in v49).values()).items())} (к-сть угод: монет)")
    p("")
    p("## Пункт 9: демо проти бектесту по угодах")
    p(f"вердикти повтору v4-детектором: {sorted(Counter((r['ver'], r['verdict']) for r in rows).items())}")
    p(f"вердикти моделі версії (v7 — v7_replay): {sorted(Counter((r['ver'], r['verdict_model']) for r in rows).items())}")
    p(f"причини розбіжностей (v4/v9): {dict(Counter(x for r in v49 for x in r.get('reasons') or []))}")
    p(f"вхід демо проти закриття свічки (entry_edge_r, + = демо краще) {fmt(stat(v49, 'entry_edge_r'))}")
    p(f"  з них план (plan_edge_r)                                    {fmt(stat(v49, 'plan_edge_r'))}")
    ex = [
        dict(r, exec_r=r["entry_edge_r"] - r["plan_edge_r"])
        for r in v49
        if r.get("entry_edge_r") is not None and r.get("plan_edge_r") is not None
    ]
    p(f"  виконання (entry − plan)                                    {fmt(stat(ex, 'exec_r'))}")
    for k in ("maker", "taker"):
        sub = [r for r in ex if r.get("entry_kind") == k]
        p(f"    виконання, вхід {k:<5}                                    {fmt(stat(sub, 'exec_r'))}")
    dmax = lambda key, rs: max((abs(r[key]) for r in rs if r.get(key) is not None), default=float("nan"))  # noqa: E731
    p(f"|Δ стопу| R макс {dmax('stop_diff_r', v49):.3f}; |Δ цілі| R макс {dmax('target_diff_r', v49):.3f}")
    p(f"r_diff (демо − бектест B, од. бектесту) v4+v9 {fmt(stat(v49, 'r_diff'))}")
    p(f"R бектесту B на тих самих угодах v4+v9 {fmt(stat(v49, 'r_bt'))}; Bx {fmt(stat(v49, 'r_bt_fast'))}")
    p(f"R бектесту від заповнення демо v4+v9 {fmt(stat(v49, 'r_bt_fill'))}")
    v7r = [r for r in rows if r["ver"] == "v7"]
    p(f"v7: R моделі паспорта (limit_trade) {fmt(stat(v7r, 'r_bt_model'))}; R демо {fmt(stat(v7r, 'r_demo'))}")
    p(f"виходи демо / бектест (v4+v9): {sorted(Counter((str(r.get('demo_exit')), str(r.get('bt_exit'))) for r in v49).items())}")
    p(f"виходи демо / модель (v7): {sorted(Counter((str(r.get('demo_exit')), str(r.get('bt_exit_model'))) for r in v7r).items())}")
    p(f"комісії демо, R {fmt(stat(rows, 'fee_demo_r'))}")
    p(f"комісії моделі бектесту, R {fmt(stat(rows, 'fee_model_r'))}; з фактичним видом входу {fmt(stat(rows, 'fee_entry_kind_r'))}")
    p(f"модель − демо (+ = демо заплатило менше) {fmt(stat(rows, 'fee_diff_r'))}")
    for k in ("stop", "breakeven", "target"):
        sub = [r for r in rows if r.get("exit_reason") == k]
        p(f"проковзування виходу {k:<9} демо {fmt(stat(sub, 'exit_slip_r'))}")
        p(f"      модель {fmt(stat(sub, 'model_slip_r'))}; демо − модель {fmt(stat(sub, 'slip_diff_r'))}")
    p("")
    p("## Незаповнені входи (status missed) за версією; розміщення = закриті + відкриті + missed")
    for v in ("v4", "v7", "v9"):
        nf = sum(r["ver"] == v for r in rows) + sum(r["ver"] == v for r in opened)
        nm = sum(r["ver"] == v for r in missed)
        share = f"{nf / (nf + nm):.0%}" if nf + nm else "—"
        p(f"- {v}: заповнено {nf}, не заповнено {nm}, частка заповнення {share}")
    v7_place = [dict(r, r_place=r["r_demo"]) for r in v7r] + [dict(r, r_place=0.0) for r in missed if r["ver"] == "v7"]
    p(f"v7 на розміщення (незаповнена = 0R; смуга паспорта на розміщення −0.305 … +0.310) {fmt(stat(v7_place, 'r_place'))}")
    trades, aud, plans, ne_err = dvb.load_not_executed(db)
    trades = [t for t in trades if int(t.get("signal_ts") or 0) < cutoff_ms]
    aud = [x for x in aud if int(x.get("ts") or 0) < cutoff_ms]
    srows = [x for x in demo_shadow.match_rows(trades, aud, plans, now_ms=now) if x.setup == SETUP and x.decided_ts < cutoff_ms]
    p(f"## Невиконані сигнали S12 проти тіньових планів (R без беззбитку, од. демо; t-CI з demo_shadow){' — ' + ne_err if ne_err else ''}")
    if srows:
        sm = demo_shadow.summary(srows, now_ms=now)
        for gname, s in sm["groups"].items():
            ci = f"[{s['ci95_r'][0]:+.2f}; {s['ci95_r'][1]:+.2f}]" if s.get("ci95_r") else "[—]"
            mr = f"{s['mean_r']:+.3f}" if s.get("mean_r") is not None else "—"
            p(f"- {gname}: n {s['n']}, з R {s['n_r']}, відкрито {s['open']}, без плану {s['no_plan']}, середній {mr} {ci}")
            for rr in s.get("reasons", [])[:5]:
                mr2 = f"{rr['mean_r']:+.2f}" if rr.get("mean_r") is not None else "—"
                p(f"    · {rr['reason'][:60]}: n {rr['n']}, з R {rr['n_r']}, середній {mr2}")
    p("")
    p("## По угодах")
    p(
        "дата | монета | версія | бік | вхід | вихід демо/бт | R демо | R бт (модель версії) | край входу R | комісії модель−демо R | вердикт | оборот"
    )
    for r in rows:
        f2 = lambda x: "—" if x is None else f"{x:+.2f}"  # noqa: E731
        p(
            f"{cmn.ymd_ms(int(r['signal_ts']))} | {r['symbol'].split('/')[0]} | {r['ver']} | {r['side']} | {r.get('entry_kind')} | "
            f"{r.get('demo_exit')}/{r.get('bt_exit_model')} | {f2(r.get('r_demo'))} | {f2(r.get('r_bt_model'))} | {f2(r.get('entry_edge_r'))} | "
            f"{f2(r.get('fee_diff_r'))} | {r.get('verdict_model')} {','.join(r.get('reasons') or [])} | {r.get('turnover_group')}"
        )
    p("")
    p(f"відкриті (не входять): {[(x['symbol'].split('/')[0], cmn.ymd_ms(int(x['signal_ts'])), x['ver']) for x in opened]}")
    p(f"missing: {[f'{m.field}: {m.reason}' for m in b.missing[:10]]}; warnings {b.warnings[:5]}")
    p(f"seed {SEED}; бутстреп {N_BOOT} кластерами за днем сигналу; peak RSS {rss_mb():.0f} MB")
    text = "\n".join(L) + "\n"
    print(text, end="")
    (cache / f"{tag}.txt").write_text(text, encoding="utf-8")
    (cache / f"{tag}_rows.json").write_text(
        json.dumps({"generated_ms": now, "cutoff_ms": cutoff_ms, "rows": rows}, default=str), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
