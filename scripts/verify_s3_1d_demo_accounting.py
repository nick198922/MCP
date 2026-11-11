"""S3 on 1d: the demo's run of full stops against the S3 passport (IDEA-20261030-demo-setups-depth, item «S3 на 1d,
звірка демо з паспортом (облік)»; pre-registered in the card, section «Попередня реєстрація звірки S3 1d»).

    uv run python scripts/verify_s3_1d_demo_accounting.py --stage smoke   # counts and data checks, no outcome metric
    uv run python scripts/verify_s3_1d_demo_accounting.py --stage run     # the registered comparison

Demo side: closed S3 1d rows of ``demo_trade`` closed before ``--cutoff``, read over a ``mode=ro`` connection (nothing
is written), each replayed on Bybit candles through ``rt.exchanges`` with ``analytics/demo_vs_backtest.replay`` (the
live detector, the 1h path from the signal close; pages and per-response JSON cache as in
``scripts/verify_s12_demo_vs_passport.py``). Passport side: the S3 passport's candidate files
(``<passport-cache>/S3/cands``, rule B, demo costs; no network) with the demo list (top-100 by the last closed day's
Binance volume, 24h turnover >= $5M), the demo selection and the joint 4h + 1d one-trade-per-coin chain, as in
``scripts/verify_setup_passport.py``. R is per trade risk, not on capital.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import sqlite3
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn
import demo_vs_backtest as dvb
import numpy as np
import verify_s12_demo_vs_passport as s12  # cached paged fetch (one JSON per response)
import verify_setup_passport as vsp  # passport rows, demo list, chain (read-only reuse)

from tradedesk.analytics import s3_1d_audit as au
from tradedesk.analytics import setup_passport as sp
from tradedesk.analytics.demo_vs_backtest import LIVE_WINDOW, DemoTrade, Replay, replay
from tradedesk.analytics.liquidation_flush import cluster_bootstrap_ci, holm
from tradedesk.analytics.setup_signals import TF_MS
from tradedesk.config import Settings
from tradedesk.core.envelope import Builder
from tradedesk.execution.demo_trader import DEFAULT_DB, hold_ms  # read-only constants
from tradedesk.execution.guard import BE_PATH  # read-only constant
from tradedesk.runtime import Runtime

SETUP, TF = "S3", "1d"
CUTOFF = "2026-11-11T00:00"  # registered: S3 1d demo trades closed before this UTC minute
SEED = 20261030
N_BOOT = 10_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
P_RULE = 0.01  # registered threshold of the binomial / clustered tail
HOME = Path.home() / ".cache/tradedesk-agents"


def load_demo(db: str, cutoff_ms: int) -> dict[str, list[dict[str, Any]]]:
    """S3 rows by (timeframe, status) over a read-only connection; closed only when closed before the cutoff."""
    con = cmn.connect_ro(db)
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM demo_trade WHERE setup = ? ORDER BY signal_ts", (SETUP,))]
    finally:
        con.close()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        st = str(r.get("status"))
        if st == "closed" and (r.get("closed_ts") is None or int(r["closed_ts"]) >= cutoff_ms):
            st = "closed_after_cutoff"
        out.setdefault(f"{r.get('timeframe')}:{st}", []).append(r)
    return out


def passport(cache: Path, s1_cache: Path, data_dir: Path) -> dict[str, Any]:
    """The S3 passport rows and the registered masks (no outcome is computed here)."""
    T = vsp.load_rows(cache / SETUP)
    floor = vsp.SETUPS[SETUP][2]
    period = T["t_in"] >= vsp.PASSPORT_MS
    sel = (T["rr"] >= vsp.MIN_RR) & (T["stop_pct"] <= vsp.MAX_STOP) & (T["score"] >= vsp.MIN_SCORE)
    turn5 = np.nan_to_num(T["turn"], nan=-1) >= floor
    qv = vsp.s1.daily_qv(s1_cache, data_dir, vsp.s1.eligible_syms(data_dir))
    in_top = vsp.in_prev_day_top(T, vsp.top_n_days(qv, vsp.TOP_N))
    p100 = vsp.chain(T, period & sel & turn5 & in_top)  # the demo list, joint 4h + 1d chain (passport's P100)
    d1, h4 = T["tf"] == "1d", T["tf"] == "4h"
    oos = T["t_in"] >= vsp.OOS_MS
    fs = np.array([au.is_full_stop(str(k)) for k in T["B_k"]], dtype=float)
    return {
        "T": T,
        "fs": fs,
        "V": p100 & d1,  # primary: S3 1d trades the demo would have taken
        "OOS": oos,
        "list1d": period & turn5 & in_top & d1,  # every 1d candidate on the demo list, before selection and chain
        "sel": sel,
        "V4": p100 & h4,
        "h00": (T["t_in"] % DAY_MS) == 0,
    }


def smoke(a: argparse.Namespace, cutoff_ms: int) -> int:
    g = load_demo(a.db, cutoff_ms)
    print(f"S3 demo rows by timeframe:status {sorted((k, len(v)) for k, v in g.items())}")
    closed = g.get(f"{TF}:closed", [])
    print(
        f"S3 1d closed < cutoff: {len(closed)}; sides {dict(Counter(r['side'] for r in closed))}; venues {dict(Counter(r['venue'] for r in closed))}"
    )
    print(f"symbols {[r['symbol'] for r in closed]}")
    wk = au.day_sizes([int(r["signal_ts"]) + DAY_MS for r in closed], cutoff_ms + DAY_MS, bucket_ms=sp.WEEK_MS)
    print(f"entry-week (t_in // WEEK_MS) cluster sizes {wk}")
    print(
        f"signal-day cluster sizes {au.day_sizes([int(r['signal_ts']) for r in closed], cutoff_ms)}; days {sorted({cmn.ymd_ms(int(r['signal_ts'])) for r in closed})}"
    )
    print(
        f"entry kinds {dict(Counter(str(r.get('entry_kind')) for r in closed))}; versions {dict(Counter(str(r.get('version')) for r in closed))}"
    )
    miss = [
        k
        for k in ("qty", "risk_usd", "fees_usd", "fill", "exit", "exit_reason", "r_multiple", "pnl_usd", "opened_ts")
        if any(r.get(k) is None for r in closed)
    ]
    print(f"columns with a NULL in some closed 1d row: {miss}")
    unit = [abs(float(r["pnl_usd"]) / float(r["risk_usd"]) - float(r["r_multiple"])) for r in closed if r.get("risk_usd")]
    print(f"max |pnl/risk - r_multiple| {max(unit, default=float('nan')):.2e} (units check)")
    delay = [(int(r["opened_ts"]) - int(r["signal_ts"]) - DAY_MS) / 60_000 for r in closed if r.get("opened_ts")]
    print(f"open delay after the daily close, minutes: {sorted(round(x, 1) for x in delay)}")
    stop_pct = sorted(round(abs(float(r["entry"]) - float(r["stop"])) / float(r["entry"]), 4) for r in closed)
    print(f"demo planned stop distance (|entry-stop|/entry): {stop_pct}")
    t0 = time.monotonic()
    P = passport(Path(a.passport_cache), Path(a.s1_cache), Path(a.data_dir))
    T = P["T"]
    print(f"passport rows {T['B'].size}; loaded in {time.monotonic() - t0:.1f} s; peak RSS {cmn.rss_mb():.0f} MB")
    print(
        f"V 1d: 2021+ {int(P['V'].sum())}, OOS {int((P['V'] & P['OOS']).sum())}; weeks OOS {np.unique(T['week'][P['V'] & P['OOS']]).size}"
    )
    L = P["list1d"]
    print(f"1d on the demo list 2021+: selected {int((L & P['sel']).sum())}, unselected {int((L & ~P['sel']).sum())}")
    print(f"4h V 2021+: entry 00:00 UTC {int((P['V4'] & P['h00']).sum())}, other hours {int((P['V4'] & ~P['h00']).sum())}")
    syms = {str(r["symbol"]).replace("/", "").split(":")[0] for r in closed}
    print(f"demo coins in the passport V 1d 2021+: {int((P['V'] & np.isin(T['sym'], list(syms))).sum())} rows over {len(syms)} coins")
    if closed:
        rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
        b = Builder()

        async def go() -> None:
            fetch = s12.cached(dvb.venue_fetch(rt, b, a.venue), Path(a.cache_dir), a.venue)
            try:
                t1 = time.monotonic()
                rp, _ = await replay_one(closed[0], fetch, int(time.time() * 1000))
                print(
                    f"replay {closed[0]['symbol']}: signal found {rp.verdict not in ('no_signal', 'no_data', 'unsupported')}; |Δentry| {rp.entry_diff_r}, |Δstop| {rp.stop_diff_r} R; {time.monotonic() - t1:.1f} s"
                )
            finally:
                await rt.aclose()

        asyncio.run(go())
        print(f"missing: {[f'{m.field}: {m.reason}' for m in b.missing[:5]]}; peak RSS {cmn.rss_mb():.0f} MB")
    return 0


async def replay_one(row: dict[str, Any], fetch: dvb.Fetch, now_ms: int) -> tuple[Replay, int | None]:
    """The replay of one closed S3 1d trade and the backtest's first touch of the stop (1h bar open, ms)."""
    t = DemoTrade.from_row(row)
    tf_ms = TF_MS[t.timeframe]
    hold = hold_ms(t.setup, t.timeframe)
    close = t.signal_ts + tf_ms
    bars = dvb.to_bars(await fetch(t.symbol, t.timeframe, t.signal_ts - (LIVE_WINDOW - 1) * tf_ms, close - 1), tf_ms)
    path = dvb.to_bars(await fetch(t.symbol, "1h", close, min(close + hold, now_ms) - 1), HOUR_MS)
    rp = replay(t, bars, path, now_ms=now_ms, hold_ms=hold, be_frac=BE_PATH)
    touch = None if rp.bt_stop is None else au.first_touch(path.t, path.h, path.low, t.side, close, float(rp.bt_stop))
    return rp, touch


def iid_tail(v: np.ndarray, n: int, x: float, *, reps: int = N_BOOT, seed: int = SEED) -> float:
    """P(mean of n draws with replacement from v <= x)."""
    rng = np.random.default_rng(seed)
    m = v[rng.integers(0, v.size, (reps, n))].mean(axis=1)
    return float((np.sum(m <= x + 1e-12) + 1) / (reps + 1))


def share(T: dict[str, np.ndarray], fs: np.ndarray, m: np.ndarray) -> str:
    c = sp.mean_ci(fs[m], T["week"][m], n_boot=N_BOOT, seed=SEED)
    r = sp.mean_ci(T["B"][m], T["week"][m], n_boot=N_BOOT, seed=SEED)
    return (
        f"n {int(c['n'])}, тижнів {int(c['k'])}; частка повних стопів {c['m']:.3f} [{c['lo']:.3f}; {c['hi']:.3f}]; "
        f"E[R] {r['m']:+.3f} [{r['lo']:+.3f}; {r['hi']:+.3f}]; медіана стопу {np.median(T['stop_pct'][m]):.4f}"
    )


def run(a: argparse.Namespace, cutoff_ms: int) -> int:  # noqa: PLR0915 - one linear report
    now = int(time.time() * 1000)
    closed = load_demo(a.db, cutoff_ms).get(f"{TF}:closed", [])
    n = len(closed)
    days = [cmn.ymd_ms(int(r["signal_ts"])) for r in closed]
    sizes = au.day_sizes([int(r["signal_ts"]) for r in closed], cutoff_ms)
    wsizes = au.day_sizes([int(r["signal_ts"]) + DAY_MS for r in closed], cutoff_ms + DAY_MS, bucket_ms=sp.WEEK_MS)
    k_stop = sum(r.get("exit_reason") == "stop" for r in closed)
    rd = [float(r["r_multiple"]) for r in closed]
    out: list[str] = []

    def emit(s: str = "") -> None:
        print(s, flush=True)
        out.append(s)

    emit(f"# S3 1d: демо проти паспорта (cutoff {a.cutoff} UTC, прогін {cmn.ymd_ms(now)})")
    ci = cluster_bootstrap_ci(rd, days, n_boot=N_BOOT, seed=SEED) if n >= 2 else None
    emit(f"## Демо: n {n}, днів сигналу {len(set(days))}, кластери {sizes}, сторони {dict(Counter(r['side'] for r in closed))}")
    emit(f"повних стопів (exit_reason = stop) {k_stop} з {n}; середнє R {statistics.fmean(rd):+.3f} {ci}; сума {sum(rd):+.2f}")

    P = passport(Path(a.passport_cache), Path(a.s1_cache), Path(a.data_dir))
    T, fs = P["T"], P["fs"]
    emit("\n## H1. Частка повних стопів у паспорті S3 1d (список демо, відбір, ланцюг) і хвіст P(k з n)")
    for name, m in (("OOS (основна)", P["V"] & P["OOS"]), ("2021+", P["V"])):
        p_hat = float(fs[m].mean())
        c = sp.mean_ci(fs[m], T["week"][m], n_boot=N_BOOT, seed=SEED)
        emit(
            f"{name}: {share(T, fs, m)}\n  P_iid(≥{k_stop} з {n} | p̂) {au.binom_upper_tail(p_hat, n, k_stop):.2e}; "
            f"за верхньою межею CI p {au.binom_upper_tail(min(1.0, c['hi']), n, k_stop):.2e}"
        )
        for lab, bucket, sz in (("днями", DAY_MS, sizes), ("тижнями", sp.WEEK_MS, wsizes)):
            key = T["t_in"][m] // bucket
            ud, inv = np.unique(key, return_inverse=True)
            dn = np.bincount(inv).tolist()
            ds = np.bincount(inv, weights=fs[m]).astype(int).tolist()
            cl = au.cluster_all_stop_prob(dn, ds, sz) if k_stop == n else {"p": math.nan, "eligible": []}
            emit(f"  кластерно за {lab} {sz}: P(усі стоп) {cl['p']:.2e} (джерел на розмір {cl['eligible']}, з {ud.size})")
        order = np.argsort(T["t_in"][m], kind="stable")
        win = au.all_true_windows((fs[m][order] > 0).tolist(), n)
        emit(f"  вікна з {n} послідовних угод: усі повні стопи в {int(win['count'])} з {int(win['windows'])} ({win['share']:.2e})")
        for side in ("long", "short"):
            ms = m & (T["side"] == side)
            emit(f"  {side}: {share(T, fs, ms)}")
        v = T["B"][m][np.argsort(T["t_in"][m], kind="stable")]
        band = sp.r_band(v, n, reps=N_BOOT, seed=SEED)
        emit(
            f"  смуга середнього R {n} угод: i.i.d. {band['iid_p5']:+.3f} / {band['iid_p50']:+.3f} / {band['iid_p95']:+.3f}; "
            f"ковзна {band['roll_p5']:+.3f} / {band['roll_p50']:+.3f} / {band['roll_p95']:+.3f}; "
            f"P_iid(середнє ≤ демо) {iid_tail(v, n, statistics.fmean(rd)):.2e}"
        )
    syms = {str(r["symbol"]).replace("/", "").split(":")[0] for r in closed}
    emit(f"ті самі монети (описово), 2021+: {share(T, fs, P['V'] & np.isin(T['sym'], list(syms)))}")

    emit("\n## M1. Відбір демо лишає вузькі стопи? 1d на списку демо 2021+, без ланцюга: відібрані проти невідібраних")
    L, sel = P["list1d"], P["sel"]
    emit(f"відібрані: {share(T, fs, L & sel)}")
    emit(f"невідібрані: {share(T, fs, L & ~sel)}")
    d1 = sp.diff_ci(fs, L & sel, L & ~sel, T["week"], n_boot=N_BOOT, seed=SEED)
    dr = sp.diff_ci(T["B"], L & sel, L & ~sel, T["week"], n_boot=N_BOOT, seed=SEED)
    emit(
        f"Δ частки повних стопів {d1['d']:+.3f} [{d1['lo']:+.3f}; {d1['hi']:+.3f}] p {d1['p']:.3f}; Δ E[R] {dr['d']:+.3f} [{dr['lo']:+.3f}; {dr['hi']:+.3f}]"
    )
    for nm, mk in (("лише R:R ≥ 1.5", T["rr"] >= vsp.MIN_RR), ("лише бал ≥ 2", T["score"] >= vsp.MIN_SCORE)):
        emit(f"  {nm}: так — {share(T, fs, L & mk)}; ні — {share(T, fs, L & ~mk)}")
    emit("частка повних стопів за квартилями стопу (відібрані 1d, 2021+):")
    for q in au.by_quantile(T["stop_pct"][L & sel], fs[L & sel], 4):
        emit(f"  стоп {q['lo']:.4f} … {q['hi']:.4f}: n {int(q['n'])}, частка {q['mean']:.3f}")
    demo_sp = [abs(float(r["entry"]) - float(r["stop"])) / float(r["entry"]) for r in closed]
    emit(f"стопи демо: {sorted(round(x, 4) for x in demo_sp)}")

    emit("\n## M3. Тонкий час: 4h-угоди паспорта (V4, 2021+) із входом о 00:00 UTC проти інших годин")
    V4, h00 = P["V4"], P["h00"]
    emit(f"00:00 UTC: {share(T, fs, V4 & h00)}")
    emit(f"інші години: {share(T, fs, V4 & ~h00)}")
    d3 = sp.diff_ci(fs, V4 & h00, V4 & ~h00, T["week"], n_boot=N_BOOT, seed=SEED)
    emit(f"Δ частки повних стопів {d3['d']:+.3f} [{d3['lo']:+.3f}; {d3['hi']:+.3f}] p {d3['p']:.3f}")
    emit(f"Холм на M1 і M3 (p двосторонні): {[round(x, 3) for x in holm([d1['p'], d3['p']])]}")
    emit(f"описово, 4h V 2021+ усі години: {share(T, fs, V4)}")

    emit("\n## Повтор 9 угод на свічках Bybit (`demo_vs_backtest.replay`, правило беззбитку B)")
    rt = Runtime(dataclasses.replace(Settings.from_env(), db_path=""))
    b = Builder()

    async def go() -> list[tuple[dict[str, Any], Replay, int | None]]:
        fetch = s12.cached(dvb.venue_fetch(rt, b, a.venue), Path(a.cache_dir), a.venue)
        res = []
        try:
            for i, r in enumerate(closed):
                rp, touch = await replay_one(r, fetch, now)
                res.append((r, rp, touch))
                print(f"  replay {i + 1}/{n} {r['symbol']} peak RSS {cmn.rss_mb():.0f} MB", flush=True)
        finally:
            await rt.aclose()
        return res

    reps = asyncio.run(go())
    emit(
        "| монета | бік | день сигналу | вердикт | причини | вихід демо / бт | R демо | R бт | вхід +R | Δстоп R | год до стопу демо / бт |"
    )
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    for r, rp, touch in reps:
        close = int(r["signal_ts"]) + DAY_MS
        h_demo = (int(r["closed_ts"]) - close) / HOUR_MS
        h_bt = "—" if touch is None else f"{(touch - close) / HOUR_MS:.0f}"
        emit(
            f"| {r['symbol']} | {r['side']} | {cmn.ymd_ms(int(r['signal_ts']))} | {rp.verdict} | {','.join(rp.reasons) or '—'} | "
            f"{rp.demo_exit} / {rp.bt_exit} | {rp.r_demo if rp.r_demo is None else f'{rp.r_demo:+.3f}'} | "
            f"{rp.r_bt if rp.r_bt is None else f'{rp.r_bt:+.3f}'} | "
            f"{rp.entry_edge_r if rp.entry_edge_r is None else f'{rp.entry_edge_r:+.3f}'} | "
            f"{rp.stop_diff_r if rp.stop_diff_r is None else f'{rp.stop_diff_r:.3f}'} | {h_demo:.0f} / {h_bt} |"
        )
    rbt = [rp.r_bt for _, rp, _ in reps if rp.r_bt is not None]
    rdf = [rp.r_diff for _, rp, _ in reps if rp.r_diff is not None]
    emit(
        f"бектест на Bybit: виходів стопом {sum(rp.bt_exit == 'stop' for _, rp, _ in reps)} з {len(reps)}; "
        f"середнє R бт {statistics.fmean(rbt) if rbt else math.nan:+.3f}; середнє r_diff (демо − бт) {statistics.fmean(rdf) if rdf else math.nan:+.3f}, "
        f"медіана |r_diff| {statistics.median(abs(x) for x in rdf) if rdf else math.nan:.3f}; "
        f"вердикти {dict(Counter(rp.verdict for _, rp, _ in reps))}"
    )
    emit(f"missing: {[f'{m.field}: {m.reason}' for m in b.missing[:5]]}; peak RSS {cmn.rss_mb():.0f} MB")
    dst = Path(a.cache_dir) / "report.txt"
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    (Path(a.cache_dir) / "replays.json").write_text(json.dumps([rp.as_dict() for _, rp, _ in reps], default=str), encoding="utf-8")
    print(f"written {dst}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cutoff", default=CUTOFF, help="UTC YYYY-MM-DDTHH:MM; trades closed before it")
    ap.add_argument("--venue", default="bybit")
    ap.add_argument("--cache-dir", default=str(HOME / "s3-1d-demo-accounting"))
    ap.add_argument("--passport-cache", default=str(HOME / "setup_passport"))
    ap.add_argument("--s1-cache", default=str(HOME / "course_s14_ema"))
    ap.add_argument("--data-dir", default=str(HOME / "funding_crowding"))
    a = ap.parse_args(argv)
    cutoff_ms = int(datetime.strptime(a.cutoff, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp() * 1000)
    Path(a.cache_dir).expanduser().mkdir(parents=True, exist_ok=True)
    a.cache_dir = str(Path(a.cache_dir).expanduser())
    return smoke(a, cutoff_ms) if a.stage == "smoke" else run(a, cutoff_ms)


if __name__ == "__main__":
    raise SystemExit(main())
