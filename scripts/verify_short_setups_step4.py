"""Step 4 of IDEA-20261024-short-setups-demo: demo S38 / S39 shorts vs a placebo on the same candle (card section
«Попередня реєстрація кроку 4 (25.11)»).

    uv run python scripts/verify_short_setups_step4.py --stage counts
    uv run python scripts/verify_short_setups_step4.py --stage run --out ~/.cache/tradedesk-agents/short-setups-step4/run

The placebo construction, the candles and the statistics are those of ``scripts/verify_demo_vs_placebo.py``
(protocol v2, K = 10), reused unchanged; this script fixes the window, the time of the run, the verdict and the
descriptive rows of the registration. ``counts`` reads the demo journal over a ``mode=ro`` connection and prints counts
only (no R, no Δ, no P&L) and the sha256 of the sample manifest. ``run`` refuses before the registered time, with a
non-terminal row in the window, when the manifest it reads differs from ``origin/main:MANIFEST_REL``, or when the
sample differs from that manifest; it reads when the manifest last reached ``origin/main`` (S39 precedence rule; a
manifest not readable there counts as an unknown time). Read-only public data: nothing here
places, changes or cancels an order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py
import demo_vs_backtest as dvb
import verify_demo_vs_placebo as P

from tradedesk.analytics import demo_placebo as dp
from tradedesk.analytics import short_setups_demo as ss
from tradedesk.analytics.demo_context_filters import trend_dirs
from tradedesk.analytics.demo_pnl_check import pnl_mismatches
from tradedesk.analytics.setup_signals import TF_MS
from tradedesk.execution.demo_trader import DEFAULT_DB, hold_ms  # read-only constants

SETUPS = ss.FAMILY  # ("S38", "S39"), all versions (untagged rows are v5 before the tag, reports 18.11 / 19.11)
SIGNAL_BEFORE = "2026-11-23T16:00"  # registered: signal_ts < this UTC minute (the earliest non-terminal signal at counts)
NOW_MS = 1_796_043_600_000  # registered: 2026-11-30 13:00 UTC, after the last placebo horizon (11-30 12:00 UTC)
EXPECTED = {"S38": {"closed": 55, "missed": 3}, "S39": {"closed": 37}}  # counts stage 2026-11-25 ≈ 02:50 UTC
DELTA_SEEN = "2026-11-18T17:00"  # closed before: in the 18.11 Δ run (K = 3 and K = 10 rows)
R_PUBLISHED = {"S38": "2026-11-19T04:00", "S39": "2026-11-22T13:15"}  # closed before: R demo in a published report
# «mismatch r» of the replay (reports 19.11 / 22.11: SYRUP signal 13.11, BSV signal 14.11), fixed by link_id at counts
PATH_MISMATCH = frozenset({"td-S38-s-4h-SYRUP-1794542400", "td-S39-s-4h-BSV-1794614400"})
DEFAULT_CACHE = "~/.cache/tradedesk-agents/demo-vs-placebo"  # the same candles as the 18.11 / v2 runs
DEFAULT_OUT = "~/.cache/tradedesk-agents/short-setups-step4/run"
BTC = "BTC/USDT:USDT"
BTC_DAYS = 150  # daily bars before the first signal (trend_dir needs 60)
DAY_MS = P.DAY_MS
REPO = Path(__file__).resolve().parent.parent
# registered sample manifest (tracked by git, written by ``--stage counts --write-manifest`` on 2026-11-25)
MANIFEST_REL = "docs/verification/data/2026-11-25-short-setups-step4-manifest.json"
MANIFEST = REPO / MANIFEST_REL
MAIN_REF = "origin/main"  # S39 precedence: the latest first-parent commit of the manifest on this ref


def ms(s: str) -> int:
    return P.cutoff_ms(s)


def latest_ts(git_log_out: str) -> int | None:
    """The largest of the unix-second timestamps (one per line of ``git log --format=%ct``), in ms; None if none."""
    ts = [int(x) for x in git_log_out.split() if x.strip().isdigit()]
    return max(ts) * 1000 if ts else None


def registration_main_ms(repo: Path = REPO, path: str = MANIFEST_REL, ref: str = MAIN_REF) -> int | None:
    """When the registered manifest reached main: committer time of the latest first-parent commit of ``ref`` touching
    ``path`` (a merge or squash commit carries the merge time; a later rewrite of the manifest moves it). None if git
    fails or the file is not on ``ref`` (then S39 is for the record only). Local refs only: fetch ``origin`` first."""
    try:
        p = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), "log", "--first-parent", "--format=%ct", ref, "--", path],  # noqa: S607
            capture_output=True, text=True, timeout=30, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return latest_ts(p.stdout) if p.returncode == 0 else None


def main_manifest_bytes(repo: Path = REPO, path: str = MANIFEST_REL, ref: str = MAIN_REF) -> bytes | None:
    """The manifest as committed on ``ref`` (``git show ref:path``); None if git fails or the file is not on ``ref``."""
    try:
        p = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo), "show", f"{ref}:{path}"],  # noqa: S607
            capture_output=True, timeout=30, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def manifest_doc(closed: list[dict[str, Any]], other: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "protocol": "IDEA-20261024-short-setups-demo step 4",
        "signal_before_utc": SIGNAL_BEFORE,
        "line": "link_id|status|closed_ts|r_multiple (closed rows, sorted by link_id, joined by \\n, UTF-8; r_multiple %.6f)",
        "setups": ss.sample_manifest(closed, other, SETUPS),
    }


def day_str(ts: int | None) -> str:
    return "—" if ts is None else datetime.fromtimestamp(int(ts) / 1000, UTC).strftime("%Y-%m-%d")


def base(sym: str) -> str:
    return str(sym).split("/")[0].upper()


def path_mismatch(r: dict[str, Any]) -> bool:
    """A trade of the fixed list of replay path mismatches (by link_id)."""
    return str(r.get("link_id")) in PATH_MISMATCH


def flags(rows: list[dict[str, Any]]) -> dict[str, dict[str, bool]]:
    """Per link_id: P&L mismatch (``pnl_mismatches``, as in ``demo_status``), path mismatch, Δ-new, R-new."""
    bad = {str(x["id"]) for x in pnl_mismatches(rows)}
    seen = ms(DELTA_SEEN)
    out = {}
    for r in rows:
        c = int(r["closed_ts"]) if r.get("closed_ts") is not None else None
        out[str(r["link_id"])] = {
            "pnl_mismatch": str(r["link_id"]) in bad,
            "path_mismatch": path_mismatch(r),
            "delta_new": c is not None and c >= seen,
            "r_new": c is not None and c >= ms(R_PUBLISHED[r["setup"]]),
        }
    return out


def counts(db: str, manifest: Path = MANIFEST, write_manifest: Path | None = None) -> int:
    """Counts and the sample manifest hashes only (no R, no Δ, no P&L, no network)."""
    rows = P.read_rows(db, SETUPS)
    sb = ms(SIGNAL_BEFORE)
    closed, other, block = dp.select_signal_window(rows, SETUPS, sb)
    fl = flags(closed)
    print(f"now {P.iso(int(time.time() * 1000))} UTC; window signal_ts < {SIGNAL_BEFORE} UTC; rows of S38,S39 in the journal {len(rows)}")
    days_all = {int(r["signal_ts"]) // DAY_MS for r in closed}
    for s in SETUPS:
        c = [r for r in closed if r["setup"] == s]
        o = Counter(str(r["status"]) for r in other if r["setup"] == s)
        b = [r for r in block if r["setup"] == s]
        exp = EXPECTED[s]
        match = len(c) == exp["closed"] and o.get("missed", 0) == exp.get("missed", 0) and not b
        print(
            f"  {s}: closed {len(c)} (r_multiple present {sum(r.get('r_multiple') is not None for r in c)}), other terminal "
            f"{dict(o)}, blocking {len(b)}; registered {exp} -> {'match' if match else 'DIFFERS'}"
        )
        print(
            f"     side {dict(Counter(r['side'] for r in c))}, tf {dict(Counter(r['timeframe'] for r in c))}, "
            f"version {dict(Counter(str(r.get('version')) for r in c))}; signal days {len({int(r['signal_ts']) // DAY_MS for r in c})}, "
            f"ISO weeks {len({dp.week_key(int(r['signal_ts'])) for r in c})}, 4h candles {len({int(r['signal_ts']) for r in c})}"
        )
        f = [fl[str(r["link_id"])] for r in c]
        print(
            f"     Δ seen 18.11 {sum(not x['delta_new'] for x in f)} / Δ-new {sum(x['delta_new'] for x in f)}; "
            f"R published {sum(not x['r_new'] for x in f)} / R-new (closed ≥ {R_PUBLISHED[s]}) {sum(x['r_new'] for x in f)}"
            f" on {len({int(r['signal_ts']) // DAY_MS for r in c if fl[str(r['link_id'])]['r_new']})} days"
        )
        pm = [f"{base(r['symbol'])} {day_str(r['signal_ts'])} ({r['link_id']})" for r in c if fl[str(r["link_id"])]["pnl_mismatch"]]
        pa = [f"{base(r['symbol'])} {day_str(r['signal_ts'])} ({r['link_id']})" for r in c if fl[str(r["link_id"])]["path_mismatch"]]
        gold = sum(dp.is_gold(r["symbol"]) for r in c)
        print(f"     P&L mismatch {len(pm)}: {', '.join(pm) or '—'}; path mismatch {len(pa)}: {', '.join(pa) or '—'}; gold {gold}")
        coins = Counter(base(r["symbol"]) for r in c)
        print(f"     coins {len(coins)}, with ≥ 3 trades {sum(v >= 3 for v in coins.values())}")
        if c:
            print(
                f"     signals {P.iso(int(c[0]['signal_ts']))} … {P.iso(max(int(r['signal_ts']) for r in c))} UTC, last close "
                f"{P.iso(max(int(r['closed_ts']) for r in c))} UTC, placebo horizon ends {P.iso(dp.placebo_horizon_end(c, TF_MS, hold_ms) or 0)} UTC"
            )
    hz = dp.placebo_horizon_end(closed, TF_MS, hold_ms)
    print(
        f"  pooled: closed {len(closed)}, signal days {len(days_all)}; last placebo horizon {P.iso(hz or 0)} UTC; "
        f"registered run time {P.iso(NOW_MS)} UTC ({'ok' if hz is not None and NOW_MS >= hz else 'TOO EARLY'})"
    )
    doc = manifest_doc(closed, other)
    for s, m in doc["setups"].items():
        print(f"  manifest {s}: n_closed {m['n_closed']}, other terminal {len(m['other_terminal'])}, sha256 {m['sha256']}")
    if write_manifest is not None:
        write_manifest.parent.mkdir(parents=True, exist_ok=True)
        write_manifest.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"  manifest written -> {write_manifest}")
    reg = load_manifest(manifest)
    if reg is None:
        print(f"  registered manifest {manifest}: absent or unreadable")
    else:
        diff = ss.manifest_diff(reg.get("setups", {}), doc["setups"])
        print(f"  registered manifest {manifest.name}: {'match' if not diff else 'DIFFERS'}")
        for d in diff:
            print(f"     {d}")
    return 0


async def btc_regimes(rows: list[dict[str, Any]], cache: Path, now: int) -> tuple[dict[str, int], int]:
    """BTC daily trend (+1 / 0 / −1, ``setup_signals.trend_dir``) on the last daily bar closed by each trade's entry
    (signal bar close). Bybit daily candles through the runtime provider, cached; (link_id -> regime, failures)."""
    rt, b, fetch, _ = P.open_sources(cache)
    try:
        first = min(int(r["signal_ts"]) for r in rows) // DAY_MS * DAY_MS - BTC_DAYS * DAY_MS
        last = max(int(r["signal_ts"]) for r in rows) + DAY_MS
        bars = dvb.to_bars(await fetch(BTC, "1d", first, min(last, now) - 1), DAY_MS)
        dirs = trend_dirs(bars)
        t = [int(x) for x in bars.t]
        out = {str(r["link_id"]): ss.regime_at(t, list(dirs), int(r["signal_ts"]) + TF_MS[r["tf"]], DAY_MS) for r in rows}
        return out, len(b.missing)
    finally:
        await rt.aclose()


def sub(ds: list[tuple[float, int, dict]], keep) -> list[tuple[float, int, dict]]:  # noqa: ANN001
    return [x for x in ds if keep(x[2])]


def report(  # noqa: PLR0915 - one linear report
    rows: list[dict[str, Any]], meta: dict[str, Any], now: int, v2_rows: list[dict[str, Any]] | None = None
) -> list[str]:
    """The registered tables; ``meta['registration_main_ms']`` decides whether S39 counts (precedence rule),
    ``v2_rows`` (rows.json of the v2 run, optional) prints the S39 Δ of v2 beside the step-4 one."""
    pools = [int(r["pool_n"]) for r in rows if r.get("pool_n") is not None]
    out = [
        f"short setups step 4: demo vs placebo (K = {P.K_WIDE}), signal_ts < {SIGNAL_BEFORE} UTC, now {P.iso(now)} UTC ({now})",
        f"run at {P.iso(int(meta['run_wall_ms'])) if meta.get('run_wall_ms') else '—'} UTC; listed perps {meta.get('perps')}; "
        f"pool per trade {min(pools) if pools else '—'} … {max(pools) if pools else '—'}; provider failures {meta.get('n_missing')} "
        f"(+ BTC daily {meta.get('btc_missing')})",
    ]
    st = Counter((r["setup"], r["status"]) for r in rows)
    out.append(
        "usable / excluded: "
        + ", ".join(f"{s} {st[(s, 'ok')]} / {sum(v for (a, b), v in st.items() if a == s and b != 'ok')}" for s in SETUPS)
    )
    for k in (P.K_WIDE, P.K_MAIN):
        out.append(
            f"placebo statuses (K={k}): {dict(Counter(p['1.0']['status'] for r in rows if r['status'] == 'ok' for p in r['placebo'][:k]))}"
        )
    main_ds = P.deltas(rows, P.K_WIDE)
    out.append("\nОсновне (Δ = R демо − середнє R десяти плацебо; boot — кластерний бутстреп за днем; t — CR1 за днем, df G−1):")
    stats: dict[str, tuple[dict, dict, float | None]] = {}
    for s in SETUPS:
        ds = [x for x in main_ds if x[2]["setup"] == s]
        txt, sm, td = P.line_v2(s, ds)
        new = [x[0] for x in ds if x[2].get("r_new")]
        stats[s] = (sm, td, sum(new) / len(new) if new else None)
        out.append(txt + f"  | R-new n {len(new)} Δ {'—' if not new else f'{sum(new) / len(new):+.3f}'}")
    elig = {s: max(v[0]["p_gt0"], v[1]["p_gt0"]) for s, v in stats.items() if v[0]["p_gt0"] is not None and v[1]["p_gt0"] is not None}
    adj = ss.holm_fixed(elig)
    verdicts = {s: ss.verdict_step4(stats[s][0], stats[s][1], p_holm=adj[s], new_mean=stats[s][2]) for s in SETUPS}

    def mde_max(s: str) -> str:
        a, b = stats[s][0]["mde80"], stats[s][1]["mde80"]
        return "—" if a is None or b is None else f"{max(a, b):.3f}"

    def p_raw(s: str) -> str:
        return "—" if s not in elig else f"{elig[s]:.3f}"

    out.append(
        "Вердикти: "
        + ", ".join(f"{s} {verdicts[s]} (p max(boot, t) {p_raw(s)}, Holm m 2 {adj[s]:.3f}, MDE80 max {mde_max(s)})" for s in SETUPS)
    )
    reg_ms = meta.get("registration_main_ms")
    blind = ss.s39_blind(reg_ms)
    out.append(
        f"Реєстрація в {MAIN_REF}: {'—' if reg_ms is None else P.iso(int(reg_ms)) + ' UTC'} (межа {P.iso(ss.S39_BLIND_DEADLINE_MS)} UTC) -> "
        + ("S39 сліпий щодо Δ, вердикт кроку 4 діє" if blind else "S39 НЕ сліпий щодо Δ (v2 28.11): S39 лише облік, статус картки без S39")
    )
    man = meta.get("manifest") or {}
    out.append(
        f"Маніфест {man.get('path', '—')}: sha256 "
        + ", ".join(f"{s} {h}" for s, h in (man.get("sha256") or {}).items())
        + f"; вибірка: {man.get('sample', '—')}; проти {MAIN_REF}: {man.get('main', '—')}"
    )
    out.append(f"Статус картки за правилом: {ss.step4_card_status(verdicts, blind)}")
    out.append(
        "Легенда (правило кроку 4, не кроку 1 і не v2): ✓ — обидві нижні межі > 0, Холм < 0.05, Δ ≥ +0.05R, max MDE80 ≤ 0.30R, "
        "R-нові > 0; ≈ — надлишок значущий (обидві нижні межі > 0, Холм < 0.05), але слабкий: менший за +0.05R, MDE80 > 0.30R "
        "або R-нові ≤ 0; ✗ — перевага над випадковим шортом < +0.05R (обидві верхні межі < +0.05R; Δ може бути й значуще > 0); "
        "◯ — n < 30, < 10 днів, нема CI або нічого не виключено."
    )
    out.append("✗ — односторонній тест рівня 2.5 % проти +0.05R на кожен сетап, без поправки на два сетапи (Холм лише для ✓ / ≈).")
    for s in SETUPS:
        a, b = stats[s][0]["mde80"], stats[s][1]["mde80"]
        mx = None if a is None or b is None else max(a, b)
        need = ss.days_to_gate(mx, int(stats[s][0]["clusters"]))
        out.append(f"  {s}: днів сигналу {stats[s][0]['clusters']}, до воріт MDE80 ≤ 0.30R ≈ {'—' if need is None else need} днів")
    out.append(
        "Старшинство: для рішень цієї картки діє вердикт кроку 4; S39 у v2 (картка demo-setups-depth, 28.11) — облік там. "
        "Якщо Δ S39 різняться, основна — кроку 4."
    )
    v2 = v2_rows
    if v2:
        mine = [x for x in main_ds if x[2]["setup"] == "S39"]
        other = [x for x in P.deltas(v2, P.K_WIDE) if x[2]["setup"] == "S39"]
        pool = lambda rs: [int(r["pool_n"]) for r in rs if r.get("setup") == "S39" and r.get("pool_n") is not None]  # noqa: E731
        p4, p2 = pool(rows), pool(v2)
        out.append(P.line_v2("S39 крок 4 (основна)", mine)[0])
        out.append(P.line_v2("S39 v2 28.11 (облік)", other)[0])
        if mine and other:
            d4, d2 = sum(x[0] for x in mine) / len(mine), sum(x[0] for x in other) / len(other)
            out.append(
                f"  Δ S39 крок 4 − v2 {d4 - d2:+.3f}; пул крок 4 {min(p4) if p4 else '—'} … {max(p4) if p4 else '—'}, "
                f"v2 {min(p2) if p2 else '—'} … {max(p2) if p2 else '—'}"
            )
    else:
        out.append("  рядки v2 не передано (--v2-rows): порівняння Δ S39 з v2 у звіті вручну")
    out.append("\nОписово (жоден рядок не вердикт):")
    groups = [("S38", ("S38",)), ("S39", ("S39",)), ("S38 + S39", SETUPS)]
    for name, ss_ in groups:
        ds = [x for x in main_ds if x[2]["setup"] in ss_]
        if not ds:
            continue
        rd = [x[2]["r_demo"] for x in ds]
        out.append(
            f"  {name}: середнє R демо {sum(rd) / len(rd):+.3f}, середнє плацебо K=10 {sum(x[2]['r_demo'] - x[0] for x in ds) / len(ds):+.3f}, n {len(ds)}"
        )
        top5 = ss.without_top([x[0] for x in ds], 5)
        best = ss.best_cluster([x[0] for x in ds], [x[1] for x in ds])
        days = sorted({x[1] for x in ds})
        half = days[len(days) // 2]
        pick = lambda k=P.K_WIDE, fee="1.0", only=ss_, **kw: [x for x in P.deltas(rows, k, fee, **kw) if x[2]["setup"] in only]  # noqa: E731
        lines = [
            (f"{name} pooled (одна оцінка на обидва)", ds) if len(ss_) > 1 else None,
            (f"{name} K=3", pick(P.K_MAIN)),
            (f"{name} без плацебо 'open'", pick(skip_open=True)),
            (f"{name} Δ_sim (монета сигналу в моделі)", pick(sim=True)),
            (f"{name} Δ_sim комісії x1.5", pick(fee="1.5", sim=True)),
            (f"{name} без топ-5 Δ", [ds[i] for i in top5]),
            (f"{name} без топ-3 |Δ|", sorted(ds, key=lambda x: -abs(x[0]))[3:]),
            (f"{name} без найкращого дня ({P.iso(best * DAY_MS)[:10]})", [x for x in ds if x[1] != best]),
            (f"{name} перша половина днів", [x for x in ds if x[1] < half]),
            (f"{name} друга половина днів", [x for x in ds if x[1] >= half]),
            (f"{name} Δ бачені 18.11", sub(ds, lambda r: not r.get("delta_new"))),
            (f"{name} Δ-нові (закриті ≥ {DELTA_SEEN})", sub(ds, lambda r: r.get("delta_new"))),
            (f"{name} R опубліковані", sub(ds, lambda r: not r.get("r_new"))),
            (f"{name} R-нові (у жодному звіті)", sub(ds, lambda r: r.get("r_new"))),
            (f"{name} лише v5", sub(ds, lambda r: r.get("version") == "v5")),
            (f"{name} без тегу версії", sub(ds, lambda r: r.get("version") != "v5")),
            (f"{name} без розбіжних P&L і шляху", sub(ds, lambda r: not (r.get("pnl_mismatch") or r.get("path_mismatch")))),
            (f"{name} без XAUT / PAXG (угоди і пул плацебо)", [x for x in pick(key="placebo_ng") if not dp.is_gold(x[2]["symbol"])]),
            (f"{name} BTC тренд −1", sub(ds, lambda r: r.get("btc_regime") == -1)),
            (f"{name} BTC тренд 0", sub(ds, lambda r: r.get("btc_regime") == 0)),
            (f"{name} BTC тренд +1", sub(ds, lambda r: r.get("btc_regime") == 1)),
            (
                f"{name} край виконання: R демо − модель монети",
                [(x[2]["r_demo"] - x[2]["sim_signal"]["1.0"]["r"], x[1], x[2]) for x in ds if x[2]["sim_signal"]["1.0"]["r"] is not None],
            ),
        ]
        for item in lines:
            if item is None:
                continue
            nm, xs = item
            out.append(P.line_v2(nm, xs)[0] if xs else f"  {nm:<40} n   0")
        out.append(P.leave_one_day(name, ds))
        by_c: dict[str, list[float]] = defaultdict(list)
        for x in ds:
            by_c[base(x[2]["symbol"])].append(x[0])
        bigc = {c: v for c, v in by_c.items() if len(v) >= 3}
        q = P.cochran(bigc)
        out.append(
            f"  {name} монет {len(by_c)} на {len(ds)} угод; з ≥ 3 угодами {len(bigc)}: "
            + ", ".join(f"{c} n{len(v)} {sum(v) / len(v):+.2f}" for c, v in sorted(bigc.items()))
            + (f"; Cochran Q {q[0]:.2f} df {q[1]}, I² {q[2]:.2f}" if q else "")
        )
    return out


def run(  # noqa: PLR0913
    db: str,
    cache: Path,
    now: int,
    out_dir: Path,
    *,
    manifest: Path = MANIFEST,
    v2_rows: Path | None = None,
    wall_ms: int | None = None,
    repo: Path = REPO,
) -> int:
    """The registered run. Refuses (exit 2, nothing computed) before the registered time, with a non-terminal row in
    the window, before the last placebo horizon, when the manifest differs from the one on ``MAIN_REF``, or when the
    sample differs from the registered manifest. A manifest not readable on ``MAIN_REF`` = unknown time (S39 record only)."""
    wall = int(time.time() * 1000) if wall_ms is None else wall_ms
    if now < NOW_MS or now > wall:
        print(f"refused: now {P.iso(now)} UTC must be ≥ the registered {P.iso(NOW_MS)} UTC and not in the future (wall {P.iso(wall)} UTC)")
        return 2
    rows, other, block = P.load_window(db, SETUPS, ms(SIGNAL_BEFORE))  # one read of the journal for every check
    print(
        f"signal_ts < {SIGNAL_BEFORE} UTC: closed {len(rows)}, other terminal {dict(Counter(str(r['status']) for r in other))}, "
        f"blocking {len(block)}",
        flush=True,
    )
    if block:
        print("refused: the window holds rows that are not terminal yet (registered rule: wait, never move the boundary)")
        return 2
    hz = dp.placebo_horizon_end(rows, TF_MS, hold_ms)
    if hz is not None and now < hz:
        print(f"refused: now {P.iso(now)} UTC is before the last placebo horizon {P.iso(hz)} UTC")
        return 2
    reg_doc = load_manifest(manifest)
    if reg_doc is None:
        print(f"refused: the registered manifest {manifest} is absent or unreadable")
        return 2
    on_main = main_manifest_bytes(repo)
    if on_main is not None and on_main != manifest.read_bytes():
        print(f"refused: the manifest {manifest} differs from {MAIN_REF}:{MANIFEST_REL} (registration 25.11; no run)")
        return 2
    main_match = "unknown" if on_main is None else "match"  # unknown: S39 for the record only, as an unknown merge time
    diff = ss.manifest_diff(reg_doc.get("setups", {}), ss.sample_manifest(rows, other, SETUPS))
    if diff:
        print("refused: the sample differs from the registered manifest (registration 25.11; explain in the report, no run):")
        for d in diff:
            print(f"  {d}")
        return 2
    short = ", ".join(f"{s} {str(m.get('sha256'))[:12]}" for s, m in reg_doc["setups"].items())
    print(f"manifest {manifest.name}: match ({short}); vs {MAIN_REF}: {main_match}")
    v2 = None
    if v2_rows is not None:
        v2 = (load_manifest(v2_rows) or {}).get("rows")
        if not v2:
            print(f"refused: --v2-rows {v2_rows} has no rows")
            return 2
    fl = flags(rows)
    reg_ms = registration_main_ms(repo) if on_main is not None else None
    print(f"step 4: closed rows {len(rows)}; now {P.iso(now)} ({now}); registration on {MAIN_REF} {reg_ms}", flush=True)
    got, meta = asyncio.run(P.collect(rows, cache, now, (1.0, 1.5), P.day_span(rows)))
    meta["registration_main_ms"] = reg_ms
    meta["manifest"] = {
        "path": str(manifest),
        "sha256": {s: str(m.get("sha256")) for s, m in reg_doc["setups"].items()},
        "sample": "match",  # a differing sample was refused above
        "main": main_match,
    }
    reg, meta["btc_missing"] = asyncio.run(btc_regimes(got, cache, now))
    for r in got:
        r.update(fl[str(r["link_id"])], btc_regime=reg.get(str(r["link_id"])))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rows.json").write_text(
        json.dumps(
            {"now_ms": now, "signal_before": SIGNAL_BEFORE, "protocol": "short-setups step 4", "meta": meta, "rows": got}, default=str
        ),
        encoding="utf-8",
    )
    txt = "\n".join(report(got, meta, now, v2))
    (out_dir / "report.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)
    print(f"peak RSS {cmn.rss_mb():.0f} MB; rows -> {out_dir / 'rows.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("counts", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--now-ms", type=int, default=NOW_MS, help="registered 1796043600000 (2026-11-30 13:00 UTC)")
    ap.add_argument("--manifest", default=str(MANIFEST), help="registered sample manifest (tracked by git)")
    ap.add_argument("--write-manifest", default=None, help="counts: write the current manifest here (registration only)")
    ap.add_argument("--v2-rows", default=None, help="run: rows.json of the v2 run (demo-setups-depth, 28.11) for the S39 Δ beside")
    a = ap.parse_args(argv)
    if a.stage == "counts":
        return counts(a.db, Path(a.manifest), Path(a.write_manifest) if a.write_manifest else None)
    v2 = Path(a.v2_rows).expanduser() if a.v2_rows else None
    return run(a.db, Path(a.cache_dir).expanduser(), a.now_ms, Path(a.out).expanduser(), manifest=Path(a.manifest), v2_rows=v2)


if __name__ == "__main__":
    sys.exit(main())
