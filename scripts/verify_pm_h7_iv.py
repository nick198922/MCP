"""H7' - weekend / hour-of-day profile on the paper's IV model (IDEA-20261031-structural-edges-depth, item 8k).

The pre-registration is in the idea card ("Пункт 8k. Попередня реєстрація"). Offline: a read-only ``sqlite3 backup`` copy
of the collector DB (``pm_gap_snapshot`` x ``pm_gap_outcome``) and the BTC 1h cache ``weekend_vol/klines`` for the
in-sample profile (2020-01 .. 2025-02, the same as item 5, not re-estimated). No network.

Stages:
    stage0  power gate from predictions only: reads which markets are resolved, never the outcome value
    full    the registered test; refuses unless the stage-0 gate in <out-dir>/stage0.json passed

Usage:
    uv run python scripts/verify_pm_h7_iv.py --stage stage0 [--refresh-db]
    uv run python scripts/verify_pm_h7_iv.py --stage full [--n-boot 10000]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import resource
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tradedesk.analytics import pm_h7_iv as h7
from tradedesk.analytics import pm_modules as pm
from tradedesk.analytics.pm_threshold import HOUR_S

OUT = Path("~/.cache/tradedesk-agents/pm_h7_iv").expanduser()
PM5 = Path("~/.cache/tradedesk-agents/pm_modules").expanduser()
DB_SRC = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
T_START = int(datetime(2026, 10, 6, tzinfo=UTC).timestamp())  # collector start
MIN_WEEKS = 20
SEED = 20261101
PLACEBO_DAYS = (1, 2, 3, 4, 5, 6)
MDE5, NEFF5, N5 = 2.341e-4, 11278, 17103  # item 5 (EWMA model, 12 h horizon): MDE at n_eff, n markets


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def week_of(ts: int) -> str:
    y, w, _ = datetime.fromtimestamp(ts, UTC).isocalendar()
    return f"{y}-W{w:02d}"


def _load(name: str):  # noqa: ANN202
    p = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def copy_db(dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    a = sqlite3.connect(f"file:{DB_SRC}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    with b:
        a.backup(b)
    a.close()
    b.close()


def is_profile() -> dict[tuple[int, bool], float]:
    """The item-5 IS profile (BTC 1h 2020-01-01 .. 2025-02-28), rebuilt from the same cache and checked against item 5."""
    vm = _load("verify_pm_modules")
    btc = vm.load_btc_long()
    prof = pm.intraday_profile(vm.bar_returns(btc, vm.IS_START, vm.IS_END))
    stored = None
    for p in sorted(PM5.glob("result*.json")):
        d = json.loads(p.read_text()).get("h7_profile_is")
        if d:
            stored = d
            break
    if stored:
        diff = max(abs(prof[(int(k[:2]), k[2] == "w")] - v) for k, v in stored.items())
        print(f"IS profile vs item-5 stored: max |diff| = {diff:.2e} (stored rounded to 1e-4)", flush=True)
        if diff > 1e-3:
            raise SystemExit("IS profile differs from item 5: stop")
    else:
        print("IS profile: item-5 stored copy not found, rebuilt only", flush=True)
    wd = sum(prof[(hh, False)] for hh in range(4, 16))
    we = sum(prof[(hh, True)] for hh in range(4, 16))
    print(f"IS profile: weekday 04-16 sum={wd:.2f}, weekend 04-16 sum={we:.2f} (item 5: 14.12 / 5.79)", flush=True)
    return prof


COLS = "s.ts, s.market_id, s.asset, s.kind, s.direction, s.strike, s.event_slug, s.end_ts, s.model_prob, s.model_iv, s.model_expiry, s.expiry_gap_hours, s.spot, s.model_covered"


def market_rows(con: sqlite3.Connection, with_outcome: bool) -> Iterator[tuple[str, list[tuple], int | None]]:
    """Per resolved market: its usable snapshot rows (ordered by ts) and, only for the full stage, the outcome."""
    sel = COLS + (", o.outcome_yes" if with_outcome else "")
    q = (
        f"SELECT {sel} FROM pm_gap_snapshot s JOIN pm_gap_outcome o ON o.market_id = s.market_id "
        "WHERE o.outcome_yes IS NOT NULL AND s.model_note = 'ok' AND s.model_prob IS NOT NULL AND s.model_iv > 0 "
        "AND s.spot > 0 AND s.model_expiry IS NOT NULL AND s.ts < s.end_ts AND s.end_ts >= ? ORDER BY s.market_id, s.ts"
    )
    cur_id, buf, y = None, [], None
    for r in con.execute(q, (T_START,)):
        if r[1] != cur_id:
            if cur_id is not None:
                yield cur_id, buf, y
            cur_id, buf = r[1], []
        buf.append(r[:14])
        y = int(r[14]) if with_outcome else None
    if cur_id is not None:
        yield cur_id, buf, y


def expiry_ts(r: tuple) -> tuple[int, bool]:
    """Expiry unix seconds from ``model_expiry`` and whether it agrees with end_ts + expiry_gap_hours within 60 s."""
    e = datetime.fromisoformat(str(r[10]).replace("Z", "+00:00"))
    if e.tzinfo is None:
        e = e.replace(tzinfo=UTC)
    et = int(e.timestamp())
    ok = r[11] is None or abs(et - (int(r[7]) + float(r[11]) * HOUR_S)) <= 60
    return et, ok


def weekend_window(ts: int, end: int) -> bool:
    t = ts
    while t < end:
        if pm.is_weekend(t):
            return True
        t += HOUR_S
    return pm.is_weekend(end - 1)


def probs(prof: dict, r: tuple, shift_h: int = 0) -> float | None:
    et, _ = expiry_ts(r)
    return h7.profile_prob(prof, str(r[3]), float(r[8]), float(r[9]), float(r[12]), float(r[5]), int(r[0]), int(r[7]), et, shift_h)


def stage0(db: Path, out: Path, covered_only: bool = True) -> dict[str, Any]:
    t = time.perf_counter()
    prof = is_profile()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    span = con.execute("SELECT min(ts), max(ts), count(*) FROM pm_gap_snapshot").fetchone()
    oc = con.execute(
        "SELECT count(*), sum(outcome_yes IS NOT NULL), sum(outcome_yes IS NULL AND end_ts < ?) FROM pm_gap_outcome WHERE end_ts >= ?",
        (int(time.time()) - 6 * HOUR_S, T_START),
    ).fetchone()
    print(
        f"snapshots: {span[2]} rows, {datetime.fromtimestamp(span[0], UTC):%Y-%m-%d %H:%M} .. {datetime.fromtimestamp(span[1], UTC):%Y-%m-%d %H:%M}; "
        f"outcome rows (end >= 2026-10-06): {oc[0]}, resolved {oc[1]}, ended > 6 h ago but unresolved {oc[2]}",
        flush=True,
    )
    terms: list[dict[str, float]] = []
    per: list[dict[str, Any]] = []
    why: Counter = Counter()
    rows_n = big1 = big275 = 0
    s_vals: dict[bool, list[float]] = {False: [], True: []}
    for mid, rows, _ in market_rows(con, with_outcome=False):
        if covered_only:
            rows = [r for r in rows if r[13] == 1]
        if not rows:
            why["no covered rows"] += 1
            continue
        pb, pp = [], []
        for r in rows:
            et, ok = expiry_ts(r)
            if not ok:
                why["expiry mismatch rows"] += 1
            p = probs(prof, r)
            if p is None:
                why["profile prob none rows"] += 1
                continue
            s = h7.profile_var_scale(prof, int(r[0]), int(r[7]), et)
            s_vals[weekend_window(int(r[0]), int(r[7]))].append(s if s is not None else float("nan"))
            pb.append(float(r[8]))
            pp.append(p)
            rows_n += 1
            big1 += abs(p - float(r[8])) > 0.01
            big275 += abs(p - float(r[8])) > 0.0275
        if not pb:
            continue
        tm = h7.gate_terms(pb, pp)
        terms.append(tm)
        r0 = rows[0]
        per.append(
            {
                "market_id": mid,
                "asset": r0[2],
                "kind": r0[3],
                "end_ts": int(r0[7]),
                "week": week_of(int(r0[7])),
                "weekend": any(weekend_window(int(r[0]), int(r[7])) for r in rows),
                "n_rows": len(pb),
                **tm,
            }
        )
    con.close()
    g = h7.gate_mde(terms)
    weeks = sorted({p["week"] for p in per})
    n = len(per)
    first_end = min((p["end_ts"] for p in per), default=T_START)
    last_end = max((p["end_ts"] for p in per), default=T_START)
    span_w = max((last_end - first_end) / (7 * 86400), 1e-9)
    rate = n / span_w
    n_need = h7.n_for_gate(g["target"], g["se0"], n) if g["target"] else None
    mon0 = datetime.fromtimestamp(first_end, UTC).date()
    mon0 = mon0 - timedelta(days=mon0.weekday())
    eta_weeks = mon0 + timedelta(weeks=MIN_WEEKS)
    eta_n = None
    if n_need is not None and rate > 0:
        eta_n = (datetime.fromtimestamp(last_end, UTC) + timedelta(weeks=max(0, n_need - n) / rate)).date()
    passed = bool(len(weeks) >= MIN_WEEKS and g["mde0"] is not None and g["target"] is not None and g["mde0"] <= g["target"])
    mde5 = MDE5 * math.sqrt(N5 / n) if n else None

    def q(xs: list[float], a: float) -> float | None:
        xs = sorted(x for x in xs if not math.isnan(x))
        return xs[min(len(xs) - 1, int(a * len(xs)))] if xs else None

    print(f"markets (resolved, {'covered' if covered_only else 'all'}): {n}; rows {rows_n}; reasons {dict(why)}", flush=True)
    print(
        "per asset/kind: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(f"{p['asset']}-{p['kind']}" for p in per).items())), flush=True
    )
    print(
        f"weeks (ISO week of end): {len(weeks)} {weeks}; markets with a weekend in the window: {sum(p['weekend'] for p in per)}", flush=True
    )
    for wk in (False, True):
        xs = s_vals[wk]
        print(
            f"  s (variance multiplier), window {'with' if wk else 'without'} weekend: rows {len(xs)}, "
            f"p5 {q(xs, 0.05)}, median {q(xs, 0.5)}, p95 {q(xs, 0.95)}",
            flush=True,
        )
    print(f"rows with |p_prof - model_prob| > 1 pp: {big1 / max(rows_n, 1):.3f}; > 2.75 pp: {big275 / max(rows_n, 1):.3f}", flush=True)
    print(
        f"GATE: n={n} weeks={len(weeks)} (min {MIN_WEEKS}) target D*={g['target']} SE0={g['se0']} MDE0={g['mde0']} -> pass={passed}",
        flush=True,
    )
    print(
        f"accrual {rate:.1f} markets/week over {span_w:.1f} weeks; markets needed for MDE0 <= D*: {n_need} (ETA {eta_n}); "
        f"20 weeks reached: {eta_weeks}; [descr.] item-5 MDE scaled to n: {mde5}",
        flush=True,
    )
    res = {
        "gate": {
            "n": n,
            "weeks": weeks,
            "target": g["target"],
            "se0": g["se0"],
            "mde0": g["mde0"],
            "n_need": n_need,
            "rate_per_week": rate,
            "eta_n": str(eta_n),
            "eta_weeks": str(eta_weeks),
            "mde5_scaled": mde5,
            "pass": passed,
        },
        "why": dict(why),
        "share_gt_1pp": big1 / max(rows_n, 1),
        "share_gt_275pp": big275 / max(rows_n, 1),
        "markets": per,
    }
    out.write_text(json.dumps(res, indent=1, default=str))
    print(f"stage0 {time.perf_counter() - t:.0f}s peak rss {rss_mb():.0f} MB -> {out}", flush=True)
    return res


def boot(units: list[tuple[str, float]], n_boot: int) -> dict[str, Any]:
    blocks: dict[str, list[float]] = defaultdict(list)
    for b, v in units:
        blocks[b].append(v)
    return pm.block_bootstrap_ratio([(sum(v), len(v)) for v in blocks.values()], n_boot=n_boot, seed=SEED)


def fmt(r: dict[str, Any], k: float = 1e4) -> str:
    if r.get("mean") is None:
        return "—"
    return f"{r['mean'] * k:+.3f} [{r['lo'] * k:+.3f}; {r['hi'] * k:+.3f}] p={r['p']:.3f} n={r['n']} blocks={r['n_blocks']}"


def stage_full(db: Path, gate_file: Path, out: Path, n_boot: int) -> None:
    if not gate_file.exists() or not json.loads(gate_file.read_text())["gate"]["pass"]:
        raise SystemExit("stage-0 gate not passed (or not run): the registered test is not computed (item 8k waits)")
    prof = is_profile()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    mk: list[dict[str, Any]] = []
    for mid, rows, y in market_rows(con, with_outcome=True):
        rows = [r for r in rows if r[13] == 1]
        acc: dict[int, list[float]] = defaultdict(list)  # shift days -> per-row dBrier
        ll: list[float] = []
        for r in rows:
            pb = float(r[8])
            for kd in (0, *PLACEBO_DAYS):
                p = probs(prof, r, 24 * kd)
                if p is None:
                    break
                acc[kd].append(pm.brier(pb, y) - pm.brier(p, y))
                if kd == 0:
                    ll.append(pm.log_loss(pb, y) - pm.log_loss(p, y))
        if not acc.get(0):
            continue
        r0 = rows[0]
        mk.append(
            {
                "id": mid,
                "asset": r0[2],
                "kind": r0[3],
                "event": r0[6] or mid,
                "week": week_of(int(r0[7])),
                "weekend": any(weekend_window(int(r[0]), int(r[7])) for r in rows),
                "d": {kd: statistics.fmean(v) for kd, v in acc.items()},
                "ll": statistics.fmean(ll),
            }
        )
    con.close()
    res: dict[str, Any] = {"n": len(mk)}
    main_r = boot([(m["week"], m["d"][0]) for m in mk], n_boot)
    res["primary"] = main_r
    print(f"PRIMARY dBrier x1e4 (base - profile, week blocks): {fmt(main_r)}", flush=True)
    print(f"  event blocks [descr.]: {fmt(boot([(m['event'], m['d'][0]) for m in mk], n_boot))}", flush=True)
    print(f"  log-loss x1e4 [descr.]: {fmt(boot([(m['week'], m['ll']) for m in mk], n_boot))}", flush=True)
    for lab, sel in (("weekend window", True), ("no weekend", False)):
        print(f"  {lab}: {fmt(boot([(m['week'], m['d'][0]) for m in mk if m['weekend'] is sel], n_boot))}", flush=True)
    for kind in ("above", "touch"):
        print(f"  {kind}: {fmt(boot([(m['week'], m['d'][0]) for m in mk if m['kind'] == kind], n_boot))}", flush=True)
    plc = {kd: statistics.fmean(m["d"][kd] for m in mk if kd in m["d"]) for kd in PLACEBO_DAYS}
    res["placebo"] = plc
    print("  placebo k*24h x1e4: " + ", ".join(f"k={k} {v * 1e4:+.3f}" for k, v in plc.items()), flush=True)
    weeks = sorted({m["week"] for m in mk})
    mid_w = weeks[len(weeks) // 2]
    halves = {}
    for lab, cond in (("1st", lambda w: w < mid_w), ("2nd", lambda w: w >= mid_w)):
        halves[lab] = boot([(m["week"], m["d"][0]) for m in mk if cond(m["week"])], n_boot)
        print(f"  half {lab} (split at {mid_w}): {fmt(halves[lab])}", flush=True)
    per_a = {}
    for a in sorted({m["asset"] for m in mk}):
        per_a[a] = boot([(m["week"], m["d"][0]) for m in mk if m["asset"] == a], n_boot)
        h1 = statistics.fmean([m["d"][0] for m in mk if m["asset"] == a and m["week"] < mid_w] or [float("nan")])
        h2 = statistics.fmean([m["d"][0] for m in mk if m["asset"] == a and m["week"] >= mid_w] or [float("nan")])
        print(f"  {a}: {fmt(per_a[a])} 1st={h1 * 1e4:+.3f} 2nd={h2 * 1e4:+.3f}", flush=True)
    ok = [r for r in per_a.values() if r.get("se")]
    if len(ok) >= 2:
        print(f"  cochran {pm.cochran_q([r['mean'] for r in ok], [r['se'] for r in ok])}", flush=True)
    res.update(halves=halves, per_asset=per_a)
    out.write_text(json.dumps(res, indent=1, default=str))
    print(f"full done, peak rss {rss_mb():.0f} MB -> {out}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("stage0", "full"), default="stage0")
    ap.add_argument("--out-dir", default=str(OUT))
    ap.add_argument("--refresh-db", action="store_true")
    ap.add_argument("--all-rows", action="store_true", help="stage0 descriptive: include model_covered = 0 rows")
    ap.add_argument("--n-boot", type=int, default=10_000)
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "tradedesk_copy.sqlite"
    if a.refresh_db or not db.exists():
        t = time.perf_counter()
        copy_db(db)
        print(f"db copy {time.perf_counter() - t:.0f}s -> {db}", flush=True)
    if a.stage == "stage0":
        stage0(db, out / ("stage0_all_rows.json" if a.all_rows else "stage0.json"), covered_only=not a.all_rows)
    else:
        stage_full(db, out / "stage0.json", out / "full.json", a.n_boot)


if __name__ == "__main__":
    main()
