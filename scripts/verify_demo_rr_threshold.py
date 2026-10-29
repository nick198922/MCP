#!/usr/bin/env python3
"""Does the demo guard's reward/risk-after-fees floor 1.5 cut profitable signals? (BACKLOG 0.5 (a), 29.10)

Read-only: takes the ``--json`` file of ``scripts/demo_vs_backtest.py`` (schema 2, the daily timer writes
/var/lib/tradedesk-mcp/demo-vs-backtest/latest.json), no network, no store. Compares the shadow plans of the refusals
'reward/risk N after fees < 1.5' with those of the filled demo trades ('taken'); pre-registration and verdict in
docs/verification/2026-10-29-demo-rr-threshold.md. Prints compact tables; the full output goes to
``--cache-dir``/result.json.

    uv run python scripts/verify_demo_rr_threshold.py
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradedesk.analytics import demo_rr_threshold as d  # noqa: E402
from tradedesk.analytics.per_asset import cluster_robust_q  # noqa: E402

DEFAULT_IN = Path("/var/lib/tradedesk-mcp/demo-vs-backtest/latest.json")
DEFAULT_CACHE = Path.home() / ".cache" / "tradedesk-agents" / "demo-rr-threshold"
HOLD_MS = {"4h": 7 * d.DAY_MS, "1d": 30 * d.DAY_MS}  # execution.demo_trader.MAX_HOLD_MS (read, not imported)
Row = Mapping[str, Any]


def _iso(ms: int | None) -> str:
    return "—" if ms is None else datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def _f(x: float | None, nd: int = 3) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.{nd}f}"


def _ci(c: Mapping[str, Any]) -> str:
    return f"{_f(c.get('mean'))} [{_f(c.get('lo'))}; {_f(c.get('hi'))}] G={c.get('g')}"


def _resolved(rows: Sequence[Row]) -> list[Row]:
    return [r for r in rows if r.get("status") == "resolved" and isinstance(r.get("r"), (int, float))]


def three_cis(rows: Sequence[Row]) -> dict[str, Any]:
    rs = _resolved(rows)
    v = [float(r["r"]) for r in rs]
    out = {
        "iid": d.mean_ci(v),
        "day": d.mean_ci(v, [d.day_key(int(r["decided_ts"])) for r in rs]),
        "symbol": d.mean_ci(v, [str(r["symbol"]) for r in rs]),
    }
    out["widest"] = dict(d.widest([out["iid"], out["day"], out["symbol"]]))
    out["win_share"] = sum(1 for x in v if x > 0) / len(v) if v else None
    return out


def diffs(a: Sequence[Row], b: Sequence[Row]) -> dict[str, Any]:
    ra, rb = _resolved(a), _resolved(b)
    va, vb = [float(r["r"]) for r in ra], [float(r["r"]) for r in rb]
    key: dict[str, Callable[[Row], Any]] = {
        "iid": lambda r: (r.get("link_id"), r["decided_ts"], r.get("group")),
        "day": lambda r: d.day_key(int(r["decided_ts"])),
        "symbol": lambda r: str(r["symbol"]),
    }
    out = {k: d.diff_ci(va, [f(r) for r in ra], vb, [f(r) for r in rb]) for k, f in key.items()}
    out["widest"] = dict(d.widest(list(out.values())))
    return out


def table(rows: Sequence[Row], by: Callable[[Row], str], title: str) -> list[dict[str, Any]]:
    print(f"\n{title}")
    print(f"  {'group':<14}{'n':>4}{'res':>5}{'open':>5}{'n_r':>5}  mean R [day CI]              win")
    groups: dict[str, list[Row]] = {}
    for r in rows:
        groups.setdefault(by(r), []).append(r)
    out = []
    for k in sorted(groups):
        g = groups[k]
        c = three_cis(g)
        n_open = sum(1 for r in g if r.get("status") == "open")
        n_res = sum(1 for r in g if r.get("status") == "resolved")
        out.append({"key": k, "n": len(g), "resolved": n_res, "open": n_open, **c})
        print(f"  {k:<14}{len(g):>4}{n_res:>5}{n_open:>5}{c['iid']['n']:>5}  {_ci(c['day']):<30}{_f(c['win_share'], 2)}")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="inp", type=Path, default=DEFAULT_IN)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    a = ap.parse_args(argv)
    data = json.loads(a.inp.read_text())
    now = int(data["generated_ms"])
    rows_all = data["not_executed"]["rows"]
    print(f"file {a.inp} schema {data.get('schema')} generated {_iso(now)} UTC, shadow rows {len(rows_all)}")

    rr_all = [r for r in rows_all if r.get("group") == "refused" and r.get("reason") == d.RR_REASON]
    taken_all = [r for r in rows_all if r.get("group") == "taken"]
    taken = d.first_decisions(taken_all, now_ms=now)
    taken_links = {str(r["link_id"]) for r in taken_all if r.get("link_id")}
    rr_first = d.first_decisions(rr_all, now_ms=now)
    later_taken = [r for r in rr_first if str(r.get("link_id")) in taken_links]
    rr = [r for r in rr_first if str(r.get("link_id")) not in taken_links]
    for r in [*rr_all, *rr_first]:
        r.setdefault("rr", d.rr_from_message(r.get("example")))  # type: ignore[union-attr]

    t0 = min(int(r["decided_ts"]) for r in rr_all)
    t1 = max(int(r["decided_ts"]) for r in rr_all)
    days = (t1 - t0) / d.DAY_MS
    print(f"refused R:R rows {len(rr_all)} | unique signals {len(rr_first)} | later taken {len(later_taken)} | primary {len(rr)}")
    print(f"period {_iso(t0)} .. {_iso(t1)} UTC ({days:.1f} days); taken rows {len(taken_all)}, unique {len(taken)}")
    print(f"status primary: { ({s: sum(1 for r in rr if r.get('status') == s) for s in ('resolved', 'open', 'no_plan')}) }")
    oc = {k: sum(1 for r in rr if r.get("outcome") == k) for k in ("target", "stop", "expired", "not_filled")}
    oc_t = {k: sum(1 for r in taken if r.get("outcome") == k) for k in ("target", "stop", "expired", "not_filled")}
    print(f"outcomes primary {oc} | taken {oc_t}")
    res: dict[str, Any] = {"generated_ms": now, "period": [t0, t1], "counts": {}}
    res["counts"] = {"rr_rows": len(rr_all), "rr_unique": len(rr_first), "later_taken": len(later_taken), "primary": len(rr)}

    print("\nH1/H2: mean R (live risk unit, no BE) — iid / day / symbol CR1, df G-1")
    variants = {"primary (first decision, not later taken)": rr, "incl. later taken": rr_first, "all rows (no dedup)": rr_all}
    res["h1"] = {}
    for name, rows in variants.items():
        c = three_cis(rows)
        res["h1"][name] = c
        print(f"  refused {name}: n_r={c['iid']['n']} win={_f(c['win_share'], 3)}")
        print(f"     iid {_ci(c['iid'])} | day {_ci(c['day'])} | symbol {_ci(c['symbol'])}")
    ct = three_cis(taken)
    res["taken"] = ct
    print(f"  taken: n_r={ct['iid']['n']} win={_f(ct['win_share'], 3)}")
    print(f"     iid {_ci(ct['iid'])} | day {_ci(ct['day'])} | symbol {_ci(ct['symbol'])}")
    dd = diffs(rr, taken)
    res["h2"] = dd
    print(
        "  diff refused(primary) - taken: "
        + " | ".join(f"{k} {_f(v['diff'])} [{_f(v['lo'])}; {_f(v['hi'])}] G={v['g']}" for k, v in dd.items() if k != "widest")
    )

    w = res["h1"]["primary (first decision, not later taken)"]["widest"]
    n_r = res["h1"]["primary (first decision, not later taken)"]["iid"]["n"]
    print("\nPower (widest SE of the primary mean)")
    se = w.get("se")
    rate = len(rr) / days if days > 0 else None
    res["power"] = {"se": se, "mde": d.mde(se), "rate_per_day": rate, "needed": {}}
    print(f"  se {_f(se)} MDE {_f(d.mde(se))} R; unique refusals per day {rate:.2f}")
    for eff in (0.10, 0.15, 0.20, 0.30):
        need = d.n_needed(se, n_r, eff)
        more = None if need is None or rate is None else max(0, need - n_r) / rate
        res["power"]["needed"][str(eff)] = {"n": need, "days_more": more}
        print(f"  effect {eff:.2f} R: n ≈ {need}, ≈ {more:.0f} more days at the current rate" if need else f"  effect {eff}: —")
    sed = dd["widest"].get("se")
    for eff in (0.20, 0.30):
        k = None if sed is None else (d.Z_MDE * sed / eff) ** 2
        print(f"  diff {eff:.2f} R: MDE now {_f(d.mde(sed))}; samples x{k:.1f} needed (both groups)" if k else "")

    res["by_bucket"] = table(rr, lambda r: d.rr_bucket(r.get("rr")), "By reward/risk bucket (primary)")
    res["by_setup"] = table(rr, lambda r: str(r.get("setup")), "By setup (primary)")
    res["by_setup_taken"] = table(taken, lambda r: str(r.get("setup")), "By setup (taken, reference)")
    res["by_tf_side"] = table(rr, lambda r: f"{r.get('timeframe')} {r.get('side')}", "By timeframe and side (primary)")
    mid = t0 + (t1 - t0) // 2
    res["by_half"] = table(rr, lambda r: "1st half" if int(r["decided_ts"]) < mid else "2nd half", f"Time split at {_iso(mid)} (primary)")

    rs = _resolved(rr)
    v = [float(r["r"]) for r in rs]
    vp = [float(r["r_plan"]) for r in rs if isinstance(r.get("r_plan"), (int, float))]
    print("\nFees / open plans / top contributors (primary)")
    print(f"  mean r (demo fees) {_f(sum(v) / len(v))} vs r_plan (flat 0.05% taker) {_f(sum(vp) / len(vp) if vp else None)} on {len(vp)}")
    print(
        f"  mean planned R:R of the target (guard message) {_f(sum(r['rr'] for r in rr if r.get('rr')) / max(1, sum(1 for r in rr if r.get('rr'))))}"
    )
    tgt = [float(r["r"]) for r in rs if r.get("outcome") == "target"]
    stp = [float(r["r"]) for r in rs if r.get("outcome") == "stop"]
    exp = [float(r["r"]) for r in rs if r.get("outcome") == "expired"]
    for name, xs in (("target", tgt), ("stop", stp), ("expired", exp)):
        print(f"  {name}: n {len(xs)} mean R {_f(sum(xs) / len(xs) if xs else None)}")
    if tgt and stp:
        be = -sum(stp) / len(stp) / (sum(tgt) / len(tgt) - sum(stp) / len(stp))
        print(f"  break-even win share given mean target/stop R (no expiries): {be:.3f}")
    ob = d.open_bounds(v, [r.get("rr") for r in rr if r.get("status") == "open"])
    res["open_bounds"] = ob
    print(f"  open {ob['n_open']}: mean if all stop {_f(ob['worst'])}, if all target {_f(ob['best'])}")
    res["without_top"] = {k: d.without_top(v, k) for k in (1, 3, 5)}
    print("  without top k: " + ", ".join(f"k={k} {_f(x)}" for k, x in res["without_top"].items()))

    print("\nNon-independence")
    ov = d.overlap_share(rr, HOLD_MS)
    ov_all = d.overlap_share(rr_all, HOLD_MS)
    res["overlap"] = {"primary": ov, "all_rows": ov_all}
    print(f"  primary: {ov['overlapping']}/{ov['n']} plans share symbol+side within the hold ({ov['keys']} symbol-sides)")
    print(f"  all rows: {ov_all['overlapping']}/{ov_all['n']}")
    per_day: dict[int, int] = {}
    for r in rs:
        per_day[d.day_key(int(r["decided_ts"]))] = per_day.get(d.day_key(int(r["decided_ts"])), 0) + 1
    print(
        f"  resolved per UTC day: {dict(sorted((datetime.fromtimestamp(k * 86400, UTC).strftime('%m-%d'), n) for k, n in per_day.items()))}"
    )
    q = cluster_robust_q(v, [str(r["symbol"]) for r in rs], [d.day_key(int(r["decided_ts"])) for r in rs], min_n=3)
    res["heterogeneity"] = q
    print(
        f"  per-symbol Cochran Q (symbols with >= 3 resolved, day clusters): k={q['k']} Q={_f(q['q'], 2)} df={q['df']} p={q['p']} I2={_f(q['i2'], 2)}"
    )
    sym_labels = [str(r["symbol"]) for r in rs]
    p_perm = d.label_permutation_p(v, sym_labels)
    multi = [i for i, s in enumerate(sym_labels) if sym_labels.count(s) >= 2]
    p_multi = d.label_permutation_p([v[i] for i in multi], [sym_labels[i] for i in multi])
    res["heterogeneity_perm"] = {"p_all": p_perm, "p_multi": p_multi, "n_multi": len(multi)}
    print(
        f"  symbol-label permutation of the between-symbol spread: p={p_perm:.3f} (all), p={p_multi:.3f} (symbols with >= 2, n={len(multi)})"
    )
    syms: dict[str, list[float]] = {}
    for r in rs:
        syms.setdefault(str(r["symbol"]), []).append(float(r["r"]))
    top = sorted(syms.items(), key=lambda kv: -len(kv[1]))[:8]
    print("  most refused symbols: " + ", ".join(f"{s.split('/')[0]} n{len(x)} {sum(x) / len(x):+.2f}" for s, x in top))
    first = [r for r in rs if int(r["decided_ts"]) < mid]
    second = [r for r in rs if int(r["decided_ts"]) >= mid]
    m1: dict[str, list[float]] = {}
    for r in first:
        m1.setdefault(str(r["symbol"]), []).append(float(r["r"]))
    ranked = sorted(m1, key=lambda s: -sum(m1[s]) / len(m1[s]))
    topq = set(ranked[: max(1, len(ranked) // 4)])
    a2 = [float(r["r"]) for r in second if str(r["symbol"]) in topq]
    b2 = [float(r["r"]) for r in second if str(r["symbol"]) in m1 and str(r["symbol"]) not in topq]
    res["persistence"] = {"top_q": sorted(topq), "oos_top": a2, "oos_rest": b2}
    print(
        f"  persistence: top quarter of 1st-half symbols ({len(topq)}) in 2nd half n={len(a2)} mean {_f(sum(a2) / len(a2) if a2 else None)}"
        f" vs rest n={len(b2)} mean {_f(sum(b2) / len(b2) if b2 else None)}"
    )

    a.cache_dir.mkdir(parents=True, exist_ok=True)
    out = a.cache_dir / "result.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"\nfull output {out}; peak RSS {rss:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
