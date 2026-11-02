"""Execution quality of the demo trades against the backtest's cost model (IDEA-20261102-standing-improvements, item 3).

    uv run python scripts/verify_demo_execution.py --replay FILE [--db PATH] [--out FILE] [--md FILE] [--now-ms MS]

Reads the demo store over a ``mode=ro`` SQLite connection (no migration, no write; nothing in ``execution/`` is
called) and the JSON that ``scripts/demo_vs_backtest.py --json`` wrote (the replay of each closed trade on mainnet
candles, and the not-executed block). Prints a compact summary; ``--out`` writes every number as JSON, ``--md`` the
per-trade table (Ukrainian) for the report. No network.
"""

from __future__ import annotations

import argparse
import json
import resource
import sqlite3
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

from tradedesk.analytics import demo_execution as de

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
MAX_ROWS = 100_000


def load_signals(db: str) -> list[dict[str, Any]]:
    """exec_audit rows of kind ``signal`` (payload parsed) over a read-only connection, for the selection check."""
    con = sqlite3.connect(f"file:{urllib.parse.quote(db)}?mode=ro", uri=True, timeout=5)
    try:
        out = []
        for ts, lid, setup, payload in con.execute(
            "SELECT ts, link_id, setup, payload FROM exec_audit WHERE kind = 'signal' ORDER BY id LIMIT ?", (MAX_ROWS,)
        ):
            try:
                p = json.loads(payload or "{}")
            except json.JSONDecodeError:
                p = {}
            out.append({"ts": ts, "link_id": lid, "setup": setup, "payload": p})
        return out
    finally:
        con.close()


def load(db: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(demo_trade rows, exec_audit rows of the entry life cycle) over a read-only connection."""
    con = sqlite3.connect(f"file:{urllib.parse.quote(db)}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        trades = [dict(r) for r in con.execute("SELECT * FROM demo_trade ORDER BY signal_ts LIMIT ?", (MAX_ROWS,))]
        audit = [
            dict(r)
            for r in con.execute(
                "SELECT ts, kind, link_id, symbol, setup, payload FROM exec_audit "
                "WHERE kind IN ('missed', 'expired', 'cancelled', 'chased', 'requote', 'filled', 'sent') ORDER BY id LIMIT ?",
                (MAX_ROWS,),
            )
        ]
        return trades, audit
    finally:
        con.close()


def _fmt(x: float | None, nd: int = 3) -> str:
    return "—" if x is None else f"{x:+.{nd}f}"


def _ci(c: tuple[float, float] | None) -> str:
    return "—" if not c else f"[{c[0]:+.3f}; {c[1]:+.3f}]"


def line(name: str, s: dict[str, Any]) -> str:
    return (
        f"{name:<22} n={s['n']:>3} days={s['n_days']:>3} mean={_fmt(s['mean'])} med={_fmt(s['median'])} sd={_fmt(s['sd'])} "
        f"t={_ci(s['t'])} boot={_ci(s['boot'])} day={_ci(s['day_boot'])} need_n={s['n_needed']} -> {s['verdict']}"
    )


KEYS = ("exec_edge_r", "entry_edge_r", "plan_edge_r", "fee_gap_r", "stop_gap_r", "gap_r", "r_diff")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--replay", required=True, help="JSON of scripts/demo_vs_backtest.py --json")
    ap.add_argument("--out")
    ap.add_argument("--md")
    ap.add_argument("--now-ms", type=int, help="as-of clock, unix ms (default: now); rerun a past report with its value")
    a = ap.parse_args(argv)
    now = a.now_ms or int(time.time() * 1000)
    trades, audit = load(a.db)
    rep = json.loads(Path(a.replay).read_text(encoding="utf-8"))
    by_link = {r["link_id"]: r for r in rep.get("rows", [])}
    ms = de.metrics(trades, now)
    for m in ms:
        r = by_link.get(m["link_id"], {})
        for k in ("entry_edge_r", "plan_edge_r", "r_diff", "r_demo_bt", "r_bt", "verdict", "bt_exit", "demo_exit", "turnover_group"):
            m[k] = r.get(k)
        m["r_model"] = None if m["r_multiple"] is None else m["r_multiple"] - m["gap_r"]
    closed = [t for t in trades if t.get("status") == "closed"]
    print(f"closed demo trades {len(closed)}, with metrics {len(ms)}, replayed {sum(1 for m in ms if m.get('verdict'))}")
    print("statuses:", dict(Counter(t.get("status") for t in trades)))
    print("entry_kind:", dict(Counter(m["entry_kind"] for m in ms)), "exit_kind:", dict(Counter(m["exit_kind"] for m in ms)))
    print("exit_reason:", dict(Counter(m["exit_reason"] for m in ms)))
    out: dict[str, Any] = {"generated_ms": now, "n_closed": len(closed), "pooled": {}, "by_kind": {}, "by_setup": {}, "by_turnover": {}}
    print("\n== pooled (R per trade, + = demo better than the backtest model) ==")
    for k in (*KEYS, "fee_r", "model_fee_r", "stop_slip_r", "model_slip_r", "target_slip_r", "pnl_gap_r", "r_multiple", "r_model"):
        s = de.summarise(ms, k)
        out["pooled"][k] = s
        print(line(k, s))
    lat = de.summarise(ms, "latency_min")
    print(f"latency_min: n={lat['n']} median={lat['median']} mean={_fmt(lat['mean'], 1)}")
    stops = [m["hours_to_close"] for m in ms if m["exit_reason"] == "stop" and m["hours_to_close"] is not None]
    stops.sort()
    if stops:
        print(
            f"hours fill->close for stop exits: n={len(stops)} median={stops[len(stops) // 2]:.1f} "
            f"<1h={sum(x < 1 for x in stops)} <4h={sum(x < 4 for x in stops)} <24h={sum(x < 24 for x in stops)}"
        )
    out["stop_hours"] = stops
    flags = [m for m in ms if m["pnl_flag"]]
    print(f"P&L flags (|gap| > {de.PNL_FLAG_R}R): {len(flags)} of {sum(1 for m in ms if m['pnl_gap_r'] is not None)}")
    for m in flags:
        print(f"  {m['link_id']} pnl_gap={m['pnl_gap_r']:+.2f}R r={_fmt(m['r_multiple'], 2)} exit={m['exit_reason']}")
    ex = Counter((m.get("demo_exit"), m.get("bt_exit")) for m in ms if m.get("bt_exit"))
    print("exit demo/bt:", dict(ex))
    print("\n== robustness of G (registered: time split, without the top contributors, cost stress) ==")
    out["robust"] = {}
    top = sorted(ms, key=lambda x: abs(x["gap_r"]), reverse=True)[:3]
    mid = sorted(x["closed_ts"] for x in ms)[len(ms) // 2] if ms else 0
    variants: list[tuple[str, list[dict[str, Any]], str]] = [
        ("without P&L-flagged", [x for x in ms if not x["pnl_flag"]], "gap_r"),
        ("without top-3 |G|", [x for x in ms if x not in top], "gap_r"),
        ("first half (closed)", [x for x in ms if x["closed_ts"] < mid], "gap_r"),
        ("second half (closed)", [x for x in ms if x["closed_ts"] >= mid], "gap_r"),
        ("entry_edge 1st half", [x for x in ms if x["closed_ts"] < mid], "entry_edge_r"),
        ("entry_edge 2nd half", [x for x in ms if x["closed_ts"] >= mid], "entry_edge_r"),
    ]
    for extra in (0.0003, 0.0008):
        variants.append((f"stress +{extra:.2%}/market side", [{**x, "g_stress": de.stress_gap(x, extra)} for x in ms], "g_stress"))
    # exploratory (chosen after the pooled result): S12 enters at the gap middle by design
    ex12 = [x for x in ms if x["setup"] != "S12"]
    variants += [
        ("EXPL entry_edge no S12", ex12, "entry_edge_r"),
        ("EXPL entry_edge no S12 long", [x for x in ex12 if x["side"] == "long"], "entry_edge_r"),
        ("EXPL entry_edge no S12 short", [x for x in ex12 if x["side"] == "short"], "entry_edge_r"),
        ("EXPL r_diff no S12", ex12, "r_diff"),
    ]
    print("top |G|:", [(x["link_id"], round(x["gap_r"], 3)) for x in top])
    for name, sub, key in variants:
        s = de.summarise(sub, key)
        out["robust"][name] = s
        print(line(name, s))
    # exploratory, added after critic B's review (seen results): is the entry edge a selection by the R:R guard / score,
    # which the demo computes from the last price at the decision while the stop and target come from the signal?
    print("\n== EXPL selection at the decision price (exploratory, after review; exec_audit 'signal', mode=ro, no S12) ==")
    taken = {str(t["link_id"]) for t in trades if t.get("status") in ("closed", "open", "pending", "missed") and t.get("link_id")}
    sel = de.decision_edges(load_signals(a.db), taken, now)
    out["selection"] = {}
    for name, sub in (
        ("EXPL sel all signals", sel),
        ("EXPL sel taken", [x for x in sel if x["group"] == "taken"]),
        ("EXPL sel refused R:R<1.5", [x for x in sel if x["group"] == "refused_rr"]),
        ("EXPL sel refused other", [x for x in sel if x["group"] == "refused_other"]),
    ):
        s = de.summarise(sub, "edge_r", n_boot=2000)
        s["share_pos"] = sum(x["edge_r"] > 0 for x in sub) / len(sub) if sub else None
        out["selection"][name] = s
        print(line(name, s) + f" share>0={_fmt(s['share_pos'], 2)}")
    print("\n== by entry kind (descriptive) ==")
    for kind in sorted({str(m["entry_kind"]) for m in ms}):
        sub = [m for m in ms if str(m["entry_kind"]) == kind]
        out["by_kind"][kind] = {k: de.summarise(sub, k) for k in KEYS}
        for k in ("exec_edge_r", "fee_gap_r", "gap_r"):
            print(line(f"{kind}:{k}", out["by_kind"][kind][k]))
    print("\n== by setup (descriptive) ==")
    for st in sorted({m["setup"] for m in ms}):
        sub = [m for m in ms if m["setup"] == st]
        out["by_setup"][st] = {k: de.summarise(sub, k) for k in KEYS}
        print(line(f"{st}:gap_r", out["by_setup"][st]["gap_r"]))
    print("\n== by turnover group (descriptive) ==")
    for g in sorted({str(m.get("turnover_group")) for m in ms}):
        sub = [m for m in ms if str(m.get("turnover_group")) == g]
        out["by_turnover"][g] = {k: de.summarise(sub, k) for k in KEYS}
        print(line(f"{g}:gap_r", out["by_turnover"][g]["gap_r"]))
    print("\n== not filled / audit ==")
    print("audit kinds:", dict(Counter(x["kind"] for x in audit)))
    why = Counter()
    for x in audit:
        if x["kind"] in ("missed", "expired", "cancelled"):
            try:
                p = json.loads(x["payload"] or "{}")
            except json.JSONDecodeError:
                p = {}
            why[(x["kind"], x["setup"], str(p.get("why") or p.get("reason") or "")[:70])] += 1
    for (k, st, w), n in sorted(why.items()):
        print(f"  {k} {st}: {n} — {w}")
    missed_rows = [t for t in trades if t.get("status") == "missed"]
    for t in missed_rows:
        print(f"  demo_trade missed: {t['link_id']} note={str(t.get('note') or '')[:70]}")
    ne = rep.get("not_executed", {}).get("groups", {})
    out["not_executed"] = {}
    for g, s in ne.items():
        out["not_executed"][g] = {
            k: s.get(k) for k in ("n", "resolved", "open", "no_plan", "n_r", "mean_r", "ci95_r", "sum_r", "conclusion")
        }
        print(
            f"  shadow {g}: n={s.get('n')} n_r={s.get('n_r')} mean_r={_fmt(s.get('mean_r'))} ci={s.get('ci95_r')} — {s.get('conclusion')}"
        )
        for x in (s.get("reasons") or [])[:6]:
            print(f"     {x['reason'][:60]}: n={x['n']} n_r={x['n_r']} mean={_fmt(x['mean_r'])}")
    out["missed_why"] = [{"kind": k, "setup": st, "why": w, "n": n} for (k, st, w), n in sorted(why.items())]
    out["trades"] = ms
    print(f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB")
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if a.md:
        rows = [
            "# Якість виконання на демо: кожна закрита угода (додаток)",
            "",
            "Звіт: [2026-11-02-demo-execution-quality.md](2026-11-02-demo-execution-quality.md). Згенеровано "
            "`scripts/verify_demo_execution.py --md`. R — одиниця ризику демо, «+» на користь угоди; «вхід проти закриття "
            "свічки» — в одиницях ризику бектесту (реплей); угода — монета і дата сигналу (UTC), L / S — лонг / шорт; "
            "вхід «—» — до 22.10 (поля ще не було); ⚠ — P&L журналу не узгоджується з його цінами (> 0.05R).",
            "",
            "| угода | сетап | вхід | затримка, хв | виконання входу, R | вхід проти закриття свічки (реплей), R | комісії факт / модель, R "
            "| вихід | проковзування стопу факт / модель, R | год до закриття | розрив G, R | R демо | R при моделі | P&L-розрив, R |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for m in ms:
            day = time.strftime("%m-%d", time.gmtime(m["signal_ts"] / 1000))
            sl = "—" if m["stop_slip_r"] is None else f"{m['stop_slip_r']:+.3f} / {m['model_slip_r']:+.3f}"
            fee = "—" if m["fee_r"] is None else f"{m['fee_r']:.3f} / {m['model_fee_r']:.3f}"
            pg = "—" if m["pnl_gap_r"] is None else f"{m['pnl_gap_r']:+.2f}" + (" ⚠" if m["pnl_flag"] else "")
            rows.append(
                f"| {m['symbol'].split('/')[0]} {day} | {m['setup']} {m['timeframe']} {'L' if m['side'] == 'long' else 'S'} "
                f"| {m['entry_kind'] or '—'} | {'—' if m['latency_min'] is None else round(m['latency_min'])} | {_fmt(m['exec_edge_r'])} "
                f"| {_fmt(m.get('entry_edge_r'))} | {fee} | {m['exit_reason']} | {sl} "
                f"| {'—' if m['hours_to_close'] is None else round(m['hours_to_close'], 1)} | {_fmt(m['gap_r'])} "
                f"| {_fmt(m['r_multiple'], 2)} | {_fmt(m['r_model'], 2)} | {pg} |"
            )
        p = Path(a.md)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
