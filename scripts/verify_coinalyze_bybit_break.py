"""Level break of Coinalyze Bybit ``.6`` daily liquidations against Binance ``.A`` at 2025-02-20 (card
IDEA-20261017-doubletop-to-ours, item 4c; registration in the card, section «Попередня реєстрація 4c-bybit»).

Bybit's V5 changelog (2025-02-20) added the ``allLiquidation`` topic and deprecated ``liquidation``, which "only
pushes 1 liquidation per second". If Coinalyze took Bybit from the old topic, ``.6`` before the change is thinned and
should jump relative to Binance (itself a thinned public stream, assumed unchanged at that date).

Reads the live database READ-ONLY (sqlite URI ``mode=ro``), table ``coinalyze_history``, metrics ``liq_usd`` and
``oi_usd``; no network. Stages:
* ``coverage``: row counts, date ranges and missing days per symbol/metric, and whether the registered windows are
  covered. Reads ``ts`` only, never a value.
* ``run``: refuses unless every series covers the wide window; then the registered statistic, placebo, bootstrap
  and verdict, full output to ``--out`` (JSON), a compact table on stdout.

    uv run python scripts/verify_coinalyze_bybit_break.py --stage coverage
    uv run python scripts/verify_coinalyze_bybit_break.py --stage run --out ~/.cache/tradedesk-agents/coinalyze-bybit-break/result.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import resource
import sqlite3
import sys
from pathlib import Path

from tradedesk.analytics import level_break as lb

DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
COINS = {"BTC": ("BTCUSDT_PERP.A", "BTCUSDT.6"), "ETH": ("ETHUSDT_PERP.A", "ETHUSDT.6")}
METRICS = ("liq_usd", "oi_usd")
SPLIT = lb.day_of(int(dt.datetime(2025, 2, 20, tzinfo=dt.UTC).timestamp()))  # Bybit changelog date
GAP_BEFORE, GAP_AFTER = 7, 15  # excluded 2025-02-13 .. 2025-03-06 (channel change + exchange hack aftermath)
WIDTH, WIDE = 90, 180
MIN_FRAC = 0.8  # placebo split counts only when each window has >= 80 % of its days
MAX_MISSING = 0.02  # run gate: at most 2 % missing days per series inside the wide span
BLOCK, REPS, SEED = 7, 2000, 20250220
MDE_FACTOR = 2.8  # (1.96 + 0.84) x SE: 80 % power at 5 % two-sided


def iso(day: int) -> str:
    return dt.datetime.fromtimestamp(day * lb.DAY, dt.UTC).date().isoformat()


def span(width: int) -> tuple[int, int]:
    """Inclusive day span the windows need (one extra day before: the OI of the previous day)."""
    before, after = lb.windows(SPLIT, width, GAP_BEFORE, GAP_AFTER)
    return before.start - 1, after.stop - 1


def connect(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def series_days(con: sqlite3.Connection, symbol: str, metric: str) -> list[int]:
    rows = con.execute("SELECT ts FROM coinalyze_history WHERE symbol = ? AND metric = ? ORDER BY ts", (symbol, metric))
    return [lb.day_of(r[0]) for r in rows]


def coverage(con: sqlite3.Connection) -> list[dict]:
    out = []
    for coin, syms in COINS.items():
        for sym in syms:
            for metric in METRICS:
                days = series_days(con, sym, metric)
                rec: dict = {"coin": coin, "symbol": sym, "metric": metric, "rows": len(days)}
                if days:
                    have = set(days)
                    rec.update(first=iso(days[0]), last=iso(days[-1]), dup_days=len(days) - len(have))
                    rec["gaps"] = [(iso(a), iso(b)) for a, b in lb.gaps(days)]
                    for name, w in (("primary", WIDTH), ("wide", WIDE)):
                        lo, hi = span(w)
                        miss = sum(1 for d in range(lo, hi + 1) if d not in have)
                        rec[f"{name}_missing"] = miss
                        rec[f"{name}_ok"] = days[0] <= lo and days[-1] >= hi and miss <= MAX_MISSING * (hi - lo + 1)
                out.append(rec)
    return out


def print_coverage(cov: list[dict]) -> bool:
    for name, w in (("primary", WIDTH), ("wide", WIDE)):
        lo, hi = span(w)
        print(f"{name} span needed: {iso(lo)} .. {iso(hi)} ({hi - lo + 1} days)")
    print(f"{'symbol':16} {'metric':8} {'rows':>5} {'first':10} {'last':10} dup gaps  prim  wide")
    ok = True
    for r in cov:
        if not r["rows"]:
            print(f"{r['symbol']:16} {r['metric']:8} {0:>5} (no rows)")
            ok = False
            continue
        g = r["gaps"]
        gtxt = f"{len(g)}" + "".join(f" {a}..{b}" for a, b in g[:5]) + (" ..." if len(g) > 5 else "")
        print(
            f"{r['symbol']:16} {r['metric']:8} {r['rows']:>5} {r['first']} {r['last']} {r['dup_days']:>3} {gtxt:5}"
            f" {'ok' if r['primary_ok'] else 'NO':>4}({r['primary_missing']}) {'ok' if r['wide_ok'] else 'NO':>4}"
            f"({r['wide_missing']})"
        )
        ok = ok and r["wide_ok"]
    return ok


def load(con: sqlite3.Connection, symbol: str) -> tuple[dict[int, float], dict[int, float]]:
    liq = {
        lb.day_of(ts): float(a) + float(b)
        for ts, a, b in con.execute(
            "SELECT ts, liq_long_usd, liq_short_usd FROM coinalyze_history WHERE symbol = ? AND metric = 'liq_usd'",
            (symbol,),
        )
        if a is not None and b is not None
    }
    oi = {
        lb.day_of(ts): float(c)
        for ts, c in con.execute("SELECT ts, close FROM coinalyze_history WHERE symbol = ? AND metric = 'oi_usd'", (symbol,))
        if c is not None
    }
    return liq, oi


def quantiles(v: list[float]) -> dict[str, float]:
    s = sorted(v)
    return {f"q{int(p * 100)}": lb._q(s, p) for p in (0.05, 0.5, 0.95)}


def run(con: sqlite3.Connection) -> dict:
    data = {coin: (load(con, a), load(con, b)) for coin, (a, b) in COINS.items()}
    ys, ya, yb, dropped, n_days = {}, {}, {}, {}, {}
    for coin, ((la, oa), (lb_, ob)) in data.items():
        ys[coin], dropped[coin] = lb.log_ratio_series(la, oa, lb_, ob)
        ya[coin], yb[coin] = lb.log_intensity(la, oa), lb.log_intensity(lb_, ob)
        n_days[coin] = len(set(la) | set(lb_))
    lo, hi = span(WIDE)
    in_span = {c: sum(1 for d in range(lo + 1, hi + 1) if d not in ys[c]) for c in COINS}
    dropped_frac = max(in_span[c] / (hi - lo) for c in COINS)

    res: dict = {"split": iso(SPLIT), "dropped": dropped, "dropped_in_wide_span": in_span, "windows": {}}
    for name, w in (("primary", WIDTH), ("wide", WIDE)):
        before, after = lb.windows(SPLIT, w, GAP_BEFORE, GAP_AFTER)
        per = {c: lb.split_stat(ys[c], SPLIT, w, GAP_BEFORE, GAP_AFTER, 0.0) for c in COINS}
        pooled = lb.pooled_stat(list(ys.values()), SPLIT, w, GAP_BEFORE, GAP_AFTER, 0.0)
        ci_lo, ci_hi, sd = lb.block_bootstrap(list(ys.values()), before, after, BLOCK, REPS, SEED)
        null = [v for _, v in lb.placebo(list(ys.values()), SPLIT, w, GAP_BEFORE, GAP_AFTER, MIN_FRAC)]
        p_up, p_two = lb.placebo_rank(pooled, null) if pooled is not None else (float("nan"),) * 2
        single = {}
        for tag, ser in (("binance_A", ya), ("bybit_6", yb)):
            st = lb.pooled_stat(list(ser.values()), SPLIT, w, GAP_BEFORE, GAP_AFTER, 0.0)
            nl = [v for _, v in lb.placebo(list(ser.values()), SPLIT, w, GAP_BEFORE, GAP_AFTER, MIN_FRAC)]
            single[tag] = {"stat": st, "p_two": lb.placebo_rank(st, nl)[1] if st is not None else None, "n_null": len(nl)}
        sums = {c: lb.sum_ratio_change(*data[c][0], *data[c][1], before, after) for c in COINS}
        res["windows"][name] = {
            "before": [iso(before.start), iso(before.stop - 1)],
            "after": [iso(after.start), iso(after.stop - 1)],
            "per_coin": {c: {"stat": s.stat, "n_before": s.n_before, "n_after": s.n_after} for c, s in per.items()},
            "pooled": pooled,
            "ci95": [ci_lo, ci_hi],
            "boot_sd": sd,
            "mde80": MDE_FACTOR * sd,
            "placebo": {"n": len(null), **(quantiles(null) if null else {}), "p_up": p_up, "p_two": p_two},
            "single_venue": single,
            "sum_ratio_change": sums,
        }
    p, wd = res["windows"]["primary"], res["windows"]["wide"]
    sa = p["single_venue"]
    venue_a_extreme = (
        sa["binance_A"]["stat"] is not None
        and sa["binance_A"]["stat"] <= -lb.Thresholds().material
        and (sa["binance_A"]["p_two"] or 1) <= 0.05
        and (sa["bybit_6"]["stat"] or 0) < lb.Thresholds().material
    )
    wide_agrees = wd["pooled"] is not None and p["pooled"] is not None and (wd["pooled"] > 0) == (p["pooled"] > 0)
    if p["pooled"] is None:
        res["verdict"] = ("inconclusive", "primary windows empty")
    else:
        res["verdict"] = lb.verdict(
            p["pooled"],
            tuple(p["ci95"]),
            p["placebo"]["p_up"],
            p["placebo"]["p_two"],
            {c: v["stat"] for c, v in p["per_coin"].items() if v["stat"] is not None},
            wide_agrees,
            dropped_frac,
            venue_a_extreme,
        )
    res["venue_a_extreme"], res["dropped_frac"] = venue_a_extreme, dropped_frac
    # Exploratory, not part of the verdict: where the pooled 90-day statistic peaks over the whole history.
    scan = []
    days = [d for y in ys.values() for d in y]
    for s in range(min(days) + GAP_BEFORE + WIDTH, max(days) - GAP_AFTER - WIDTH + 2):
        v = lb.pooled_stat(list(ys.values()), s, WIDTH, GAP_BEFORE, GAP_AFTER, MIN_FRAC)
        if v is not None:
            scan.append((v, iso(s)))
    res["exploratory_scan_top5"] = sorted(scan, reverse=True)[:5]
    res["n_days"] = n_days
    return res


def fmt(v: float | None) -> str:
    return "  n/a" if v is None else f"{v:+.3f}"


def print_run(res: dict) -> None:
    print(f"split {res['split']}  dropped days in wide span {res['dropped_in_wide_span']}")
    for name, w in res["windows"].items():
        pl = w["placebo"]
        print(f"[{name}] before {w['before'][0]}..{w['before'][1]}  after {w['after'][0]}..{w['after'][1]}")
        for c, v in w["per_coin"].items():
            print(f"  {c}: stat {fmt(v['stat'])}  n {v['n_before']}/{v['n_after']}  sums {fmt(w['sum_ratio_change'][c])}")
        print(
            f"  pooled {fmt(w['pooled'])}  CI95 [{fmt(w['ci95'][0])}, {fmt(w['ci95'][1])}]  MDE80 {w['mde80']:.3f}"
            f"  placebo n {pl['n']} q5/50/95 {fmt(pl.get('q5'))}/{fmt(pl.get('q50'))}/{fmt(pl.get('q95'))}"
            f"  p_up {pl['p_up']:.3f} p_two {pl['p_two']:.3f}"
        )
        for tag, s in w["single_venue"].items():
            print(f"  {tag} alone: stat {fmt(s['stat'])}  p_two {s['p_two']}")
    print(f"exploratory scan top5: {res['exploratory_scan_top5']}")
    print(f"VERDICT: {res['verdict'][0]} ({res['verdict'][1]})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("coverage", "run"), required=True)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    con = connect(a.db)
    try:
        cov = coverage(con)
        ok = print_coverage(cov)
        if a.stage == "coverage":
            return 0
        if not ok:
            print("REFUSED: some series do not cover the wide span (see the table); wait for the backfill.")
            return 2
        if a.out is None:
            print("REFUSED: --out is required for --stage run")
            return 2
        res = run(con)
    finally:
        con.close()
    res["coverage"] = cov
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1, default=str))
    print_run(res)
    print(f"wrote {a.out}; peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
