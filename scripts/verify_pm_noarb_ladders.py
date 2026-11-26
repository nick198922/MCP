"""Static no-arbitrage ladders of Polymarket BTC/ETH price markets: step 0 (pair counts) and step 1 (violations).

Card: docs/ideas/IDEA-20261024-polymarket-static-noarb-ladders.md, «Попередня реєстрація»; pairs and checks are in
src/tradedesk/analytics/pm_noarb.py. Only whitelisted columns of ``pm_gap_snapshot`` are ever read: a sqlite authorizer
on every connection denies any other column (the model columns of the running forwards) and any other table
(``pm_gap_outcome``); the extract copies just those columns of the fixed window into the cache.

Stages:
    extract     whitelisted columns of rows T_START <= ts < T_END from the live DB (read-only) -> <cache-dir>/rows.sqlite
    count       step 0: pairs per family and filter level (matched, books, depth); no cost and no price sum is computed
    violations  step 1: cost per share, violations, their registered H1 classes (crossed / sync / survived /
                skew_suspect, pm_noarb.classify_violations), episodes, the H1 decision on the headline line and the
                descriptive lines (literal A + TA, before the filters, lower fee bound); refuses unless --step1 is given
                and <cache-dir>/count.json was made from the same extract (counts, ts range and content sha256)

Usage:
    uv run python scripts/verify_pm_noarb_ladders.py --stage count [--refresh-db]
    uv run python scripts/verify_pm_noarb_ladders.py --stage violations --step1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)
from common_helpers import rss_mb

from tradedesk.analytics import pm_noarb as nb
from tradedesk.analytics.pm_touch import et_date

OUT = Path("~/.cache/tradedesk-agents/pm_noarb_ladders").expanduser()
DB_SRC = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
TABLE = "pm_gap_snapshot"
META = "noarb_meta"
# registered window (card, «Попередня реєстрація» p. 1): first hourly snapshot 2026-10-20 18:20 UTC .. fixed cut-off
T_START = int(datetime(2026, 10, 20, 18, 0, tzinfo=UTC).timestamp())
T_END = int(datetime(2026, 11, 26, 0, 0, tzinfo=UTC).timestamp())  # exclusive
COLUMNS = (
    "ts",
    "market_id",
    "asset",
    "kind",
    "direction",
    "strike",
    "end_ts",
    "window_start",
    "yes_ask",
    "yes_ask_usd",
    "no_ask",
    "no_ask_usd",
    "fee_rate",
    "fee_exp",
    "rules_hash",
    "rules_standard",
    "book_ts",
)
SQL_ROWS = f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE ts >= ? AND ts < ? ORDER BY ts, market_id"
SQL_FP = f"SELECT COUNT(ts), MIN(ts), MAX(ts), COUNT(DISTINCT ts) FROM {TABLE} WHERE ts >= ? AND ts < ?"


def guard(con: sqlite3.Connection, extra_tables: tuple[str, ...] = ()) -> sqlite3.Connection:
    """Deny every read except the whitelisted columns of ``pm_gap_snapshot`` (and whole ``extra_tables``)."""

    def auth(action: int, a1: str | None, a2: str | None, _db: str | None, _src: str | None) -> int:
        if action == sqlite3.SQLITE_READ and not (a1 in extra_tables or (a1 == TABLE and a2 in COLUMNS)):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    con.set_authorizer(auth)
    return con


def fingerprint(con: sqlite3.Connection) -> dict[str, Any]:
    """Sample identity: counts, ts range and a sha256 of every whitelisted value in (ts, market_id) order, so a changed
    value (a re-extract after a backfill or a collector migration) is caught, not only a dropped row."""
    n, lo, hi, nts = con.execute(SQL_FP, (T_START, T_END)).fetchone()
    h = hashlib.sha256()
    cur = con.execute(SQL_ROWS, (T_START, T_END))
    while chunk := cur.fetchmany(20_000):
        for r in chunk:
            h.update(json.dumps(list(r), separators=(",", ":")).encode())
            h.update(b"\n")
    return {"t_start": T_START, "t_end": T_END, "n_rows": n, "min_ts": lo, "max_ts": hi, "n_ts": nts, "sha256": h.hexdigest()}


def extract(src: str | Path, dst: Path) -> dict[str, Any]:
    """Copy the whitelisted columns of the registered window from ``src`` (read-only, guarded) into ``dst``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    a = guard(cmn.connect_ro(src, timeout=30))
    b = sqlite3.connect(tmp)
    try:
        b.execute(f"CREATE TABLE {TABLE} ({', '.join(COLUMNS)}, PRIMARY KEY (ts, market_id))")
        b.execute(f"CREATE TABLE {META} (key TEXT PRIMARY KEY, value TEXT)")
        cur = a.execute(SQL_ROWS, (T_START, T_END))
        while chunk := cur.fetchmany(20_000):
            b.executemany(f"INSERT INTO {TABLE} VALUES ({', '.join('?' * len(COLUMNS))})", chunk)
        b.commit()
        fp = fingerprint(b)  # of the copy that the stages read
        b.executemany(f"INSERT INTO {META} VALUES (?, ?)", [(k, str(v)) for k, v in fp.items()] + [("src", str(src))])
        b.commit()
    finally:
        a.close()
        b.close()
    tmp.replace(dst)
    return fp


def open_extract(db: Path) -> sqlite3.Connection:
    return guard(cmn.connect_ro(db), extra_tables=(META,))


def iter_snapshots(con: sqlite3.Connection) -> Iterator[tuple[int, list[dict[str, Any]]]]:
    """(ts, rows of that snapshot) in ts order, one snapshot in memory at a time."""
    cur = con.execute(SQL_ROWS, (T_START, T_END))
    ts0: int | None = None
    rows: list[dict[str, Any]] = []
    for r in cur:
        d = dict(zip(COLUMNS, r, strict=True))
        if ts0 is not None and d["ts"] != ts0:
            yield ts0, rows
            rows = []
        ts0 = d["ts"]
        rows.append(d)
    if ts0 is not None:
        yield ts0, rows


def in_adequacy_set(p: dict[str, Any]) -> bool:
    """Pairs that measure the sample for the kill criterion: ladders adjacent only, TA / TB at the equal strike."""
    return p["family"] in nb.PRIMARY and p["adjacent"] is not False


def stage_count(db: Path, out: Path) -> dict[str, Any]:
    con = open_extract(db)
    fp = fingerprint(con)
    st: Counter = Counter()
    rows_n: Counter = Counter()
    obs_days: set[str] = set()
    units_ok: dict[str, set[str]] = defaultdict(set)
    asset_ok: Counter = Counter()
    any_rules: Counter = Counter()
    ok_span: list[int | None] = [None, None]  # first / last ts with an ok adequacy pair (the effective window)
    t0 = time.perf_counter()
    for i, (ts, rows) in enumerate(iter_snapshots(con)):
        rows_n["rows"] += len(rows)
        rows_n["rules_standard"] += sum(r["rules_standard"] == 1 for r in rows)
        rows_n["book_ts_null"] += sum(r["book_ts"] is None for r in rows)
        for p in nb.match_pairs(rows, standard_only=False):  # card step 0: matched pairs before the rules filter
            any_rules[p["family"]] += 1
        for p in nb.snapshot_pairs(rows, ts):
            fam, s = p["family"], p["status"]
            st[(fam, "all", s)] += 1
            if p["adjacent"]:
                st[(fam, "adjacent", s)] += 1
            if s == "ok":
                units_ok[fam].add(f"{p['asset']}:{p['unit']}")
                if in_adequacy_set(p):
                    obs_days.add(et_date(ts).isoformat())
                    asset_ok[p["asset"]] += 1
                    ok_span[0] = ts if ok_span[0] is None else ok_span[0]
                    ok_span[1] = ts
        if i % 200 == 0:
            print(f"  snapshot {i}/{fp['n_ts']} rss {rss_mb():.0f} MB {time.perf_counter() - t0:.0f}s", flush=True)
    con.close()
    fams: dict[str, Any] = {}
    for fam in nb.FAMILIES:
        for scope in ("all", "adjacent") if fam in ("A", "U", "D") else ("all",):
            c = {s: st[(fam, scope, s)] for s in nb.STATUSES}
            fams[f"{fam}/{scope}"] = {"matched": sum(c.values()), **c}
    n_ok_adequacy = sum(asset_ok.values())
    iso = [None if t is None else datetime.fromtimestamp(t, UTC).isoformat() for t in ok_span]
    res = {
        "fingerprint": fp,
        "rows": dict(rows_n),
        "families": fams,
        "matched_any_rules": {f: any_rules[f] for f in nb.FAMILIES},
        "units_with_ok_pair": {f: len(v) for f, v in units_ok.items()},
        "adequacy": {"n_ok_pairs": n_ok_adequacy, "n_obs_days": len(obs_days), "by_asset": dict(asset_ok)},
        "effective_window": {"first_ok_ts": ok_span[0], "last_ok_ts": ok_span[1], "utc": iso},
        "kill_sample_met": len(obs_days) >= 30 and n_ok_adequacy >= 300,
    }
    print(
        f"window {datetime.fromtimestamp(T_START, UTC):%Y-%m-%d %H:%M} .. {datetime.fromtimestamp(T_END, UTC):%Y-%m-%d %H:%M} UTC",
        flush=True,
    )
    print(f"fingerprint {fp}  rows {dict(rows_n)}", flush=True)
    print(f"{'family/scope':<16}{'matched':>9}" + "".join(f"{s:>9}" for s in nb.STATUSES), flush=True)
    for k, v in fams.items():
        print(f"{k:<16}{v['matched']:>9}" + "".join(f"{v[s]:>9}" for s in nb.STATUSES), flush=True)
    print(f"matched before the rules filter {res['matched_any_rules']}", flush=True)
    print(f"units with an ok pair {res['units_with_ok_pair']}  effective window {iso}", flush=True)
    print(f"adequacy {res['adequacy']} kill_sample_met={res['kill_sample_met']}  peak rss {rss_mb():.0f} MB", flush=True)
    cmn.write_json(out, res)
    print(f"count done -> {out}", flush=True)
    return res


def _reversed(p: dict[str, Any]) -> dict[str, Any]:
    """Placebo of an A pair: YES(above K2) + NO(above K1) has no floor (pays 0 when K1 < close <= K2)."""
    return {**p, "family": "A_rev", "a": (p["b"][0], "yes"), "b": (p["a"][0], "no"), "id": ("A_rev", *p["id"][1:])}


def stage_violations(db: Path, count_json: Path, out: Path) -> dict[str, Any]:
    con = open_extract(db)
    fp = fingerprint(con)
    cnt = cmn.read_json(count_json)
    if cnt.get("fingerprint") != fp:
        raise SystemExit(f"count.json was made from another extract: {cnt.get('fingerprint')} != {fp}")
    tracker = nb.EpisodeTracker()
    recs: list[dict[str, Any]] = []  # violations at the lower fee bound (a superset of the main-fee ones)
    all_ts: list[int] = []
    desc: Counter = Counter()
    for ts, rows in iter_snapshots(con):
        all_ts.append(int(ts))
        by_id = {r["market_id"]: r for r in rows}
        flags: dict[Any, bool] = {}
        for p in nb.snapshot_pairs(rows, ts):
            rv = _reversed(p) if p["family"] == "A" else None
            if rv is not None and nb.pair_status(rv, by_id, ts) == "ok":
                desc["A_rev ok"] += 1
                desc["A_rev cost<=0.995"] += nb.is_violation(nb.pair_cost(rv, by_id))
            if p["status"] != "ok":
                continue
            cost = nb.pair_cost(p, by_id)
            cost_low = nb.pair_cost(p, by_id, nb.FEE_LOWER)
            raw = sum(float(by_id[m][f"{s}_ask"]) for m, s in (p["a"], p["b"]))
            desc[f"{p['family']} ok"] += 1
            desc[f"{p['family']} raw<1"] += raw < 1.0
            v = nb.is_violation(cost)
            flags[p["id"]] = v
            if nb.is_violation(cost_low):  # the lower fee is <= the main one, so every main violation is kept here too
                ra, rb = by_id[p["a"][0]], by_id[p["b"][0]]
                crossed_legs = [m["market_id"] for m in (ra, rb) if nb.crossed(m)]
                recs.append(
                    {
                        "id": p["id"],
                        "ts": ts,
                        "family": p["family"],
                        "asset": p["asset"],
                        "unit": p["unit"],
                        "adjacent": p["adjacent"],
                        "a": p["a"],
                        "b": p["b"],
                        "strikes": (ra["strike"], rb["strike"]),
                        "asks": (ra[f"{p['a'][1]}_ask"], rb[f"{p['b'][1]}_ask"]),
                        "ask_usd": (ra[f"{p['a'][1]}_ask_usd"], rb[f"{p['b'][1]}_ask_usd"]),
                        "fees": ((ra["fee_rate"], ra["fee_exp"]), (rb["fee_rate"], rb["fee_exp"])),
                        "cost": cost,
                        "cost_lower_fee": cost_low,
                        "main_fee_violation": v,
                        "book_ts": (ra["book_ts"], rb["book_ts"]),
                        "crossed_legs": crossed_legs,  # H1 rule (a): a leg whose own YES + NO asks sum below 1
                        "crossed": bool(crossed_legs),
                        **nb.package(p, by_id),
                    }
                )
        tracker.update(ts, flags)
    con.close()
    eps = tracker.close()
    viol = nb.classify_violations([r for r in recs if r["main_fee_violation"]], all_ts)  # decides H1
    viol_low = nb.classify_violations(recs, all_ts)  # lower-fee sensitivity, descriptive
    clean = {(r["id"], r["ts"]) for r in viol if not r["crossed"]}
    for e in eps:  # the same persistence rule as H1 (b): a non-crossed violation at the check snapshot
        e["survived_next"] = (e["id"], nb.check_ts(e["first_ts"], all_ts)) in clean
    ad = cnt["adequacy"]
    lines = {  # only "headline" decides; the others are registered descriptive lines (card p. 11)
        "headline": nb.violation_dates(viol, nb.PRIMARY, nb.COUNTED),
        "literal_A_TA": nb.violation_dates(viol, nb.LITERAL, nb.COUNTED),
        "before_filters": nb.violation_dates(viol, nb.PRIMARY, nb.H1_CLASSES),
        "lower_fee": nb.violation_dates(viol_low, nb.PRIMARY, nb.COUNTED),
    }
    verdicts = {k: nb.h1_verdict(len(d), ad["n_obs_days"], ad["n_ok_pairs"]) for k, d in lines.items()}
    verdict = verdicts["headline"]
    notes = [f"{k} line disagrees: {verdicts[k][0]}" for k in ("literal_A_TA", "before_filters") if verdicts[k][0] != verdict[0]]
    if len(lines["lower_fee"]) >= 5 > len(lines["headline"]):
        notes.append("the verdict depends on the fee formula (>= 5 dates at the lower fee bound)")
    counted = [v for v in viol if v["family"] in nb.PRIMARY and v["h1_class"] in nb.COUNTED]
    gains = sorted(v["floor_gain_usd"] for v in counted)
    qs = (("min", 0.0), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("max", 1.0))
    floor = {"n": len(gains), **{k: gains[min(len(gains) - 1, int(f * len(gains)))] for k, f in qs if gains}}
    by_date_max = {d: max(v["floor_gain_usd"] for v in counted if v["unit"] == d) for d in lines["headline"]}
    by_fam = Counter(v["family"] for v in viol)
    by_cls = Counter(f"{v['family']}:{v['h1_class']}" for v in viol)
    ep_fam = Counter(e["id"][0] for e in eps)
    res = {
        "fingerprint": fp,
        "verdict": verdict,
        "h1_lines": {k: {"n_dates": len(d), "dates": d, "verdict": verdicts[k]} for k, d in lines.items()},
        "notes": notes,
        "violations_by_family": dict(by_fam),
        "violations_by_family_class": dict(by_cls),
        "floor_gain_usd_counted": floor,
        "floor_gain_usd_max_by_date": by_date_max,
        "episodes_by_family": dict(ep_fam),
        "episodes_survived_next": sum(e["survived_next"] for e in eps if e["id"][0] in nb.PRIMARY),
        "descriptive": dict(desc),
        "violations": viol,
        "violations_lower_fee_only": [v for v in viol_low if not v["main_fee_violation"]],
        "episodes": eps,
    }
    print(f"violations by family {dict(by_fam)}  by class {dict(by_cls)}  episodes {dict(ep_fam)}", flush=True)
    for k, d in lines.items():
        print(f"H1 line {k:<15} dates {len(d):>3} -> {verdicts[k]}  {d[:12]}", flush=True)
    print(f"floor gain USD of counted violations {floor}", flush=True)
    print(f"descriptive {dict(desc)}", flush=True)
    for v in [v for v in viol if v["family"] in nb.PRIMARY][:30]:
        print(
            f"  {datetime.fromtimestamp(v['ts'], UTC):%m-%d %H:%M} {v['family']:<3}{v['asset']} {v['unit']} {v['h1_class']:<12} "
            f"K={v['strikes']} asks={v['asks']} usd={tuple(round(x) for x in v['ask_usd'])} cost={v['cost']:.4f} "
            f"floor=${v['floor_gain_usd']:.2f}",
            flush=True,
        )
    print(f"H1 verdict (headline, decides) {verdict}  notes {notes}  peak rss {rss_mb():.0f} MB", flush=True)
    cmn.write_json(out, res)
    print(f"violations done -> {out}", flush=True)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("extract", "count", "violations"), default="count")
    ap.add_argument("--src", default=DB_SRC, help="collector DB, opened read-only")
    ap.add_argument("--cache-dir", default=str(OUT))
    ap.add_argument("--refresh-db", action="store_true", help="re-extract the window from --src")
    ap.add_argument("--step1", action="store_true", help="required for the violations stage (step 1 of the card)")
    a = ap.parse_args()
    cache = Path(a.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    db = cache / "rows.sqlite"
    if a.stage == "violations":
        if not a.step1 or not (cache / "count.json").exists():
            raise SystemExit("step 1 refused: run --stage count first (step 0, its own run) and pass --step1")
        stage_violations(db, cache / "count.json", cache / "violations.json")
        return
    if a.refresh_db or not db.exists() or a.stage == "extract":
        t = time.perf_counter()
        fp = extract(a.src, db)
        print(f"extract {time.perf_counter() - t:.0f}s {fp} -> {db}", flush=True)
    if a.stage == "count":
        stage_count(db, cache / "count.json")


if __name__ == "__main__":
    main()
