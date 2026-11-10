"""Carry A paper book: actual vs modelled leg-side costs by coin, leg, event and size, and reliability (d) by flag.

Descriptive breakdown of criteria (b) and (d) of the paper carry registration (docs/ideas/IDEA-20261017-forward-paper-carry.md);
registration of the breakdown: docs/verification/2026-11-10-carry-a-cost-breakdown.md. It prints costs and counters
only: no funding, price or P&L figure of any paper trade is read or printed.

Reads the collector DB through a READ-ONLY connection (``mode=ro``: no migration, no write, no network, no keys):
``cp_log`` rows of kind 'side' and 'event' (type and symbol only), ``cp_cycle`` and the payment times of ``cp_funding``
for the flagged symbols. ``--smoke`` prints only row counts (no cost ratio, no reliability share). The full output goes
to ``<cache-dir>/breakdown_<as_of>.json``.

    uv run python scripts/verify_carry_a_cost_breakdown.py --smoke
    uv run python scripts/verify_carry_a_cost_breakdown.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common_helpers import connect_ro, rss_mb, write_json, ymd_ms  # noqa: E402

from tradedesk.analytics import carry_cost_breakdown as cb  # noqa: E402
from tradedesk.analytics.carry_report import cost_realism  # noqa: E402

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_CACHE = Path.home() / ".cache/tradedesk-agents/carry-a-cost-breakdown"
PERP_VENUE = "binanceusdm"


def load(db: str, strategy: str) -> dict[str, Any]:
    con = connect_ro(db)
    try:
        con.execute("BEGIN")  # one read transaction: a consistent view while the collector writes
        sides = [
            {"cycle_ts": c, "trade_id": t, "payload": json.loads(p)}
            for c, t, p in con.execute(
                "SELECT cycle_ts, trade_id, payload FROM cp_log WHERE strategy = ? AND kind = 'side' ORDER BY id", (strategy,)
            )
        ]
        events = []
        for c, t, p in con.execute(
            "SELECT cycle_ts, trade_id, payload FROM cp_log WHERE strategy = ? AND kind = 'event' ORDER BY id", (strategy,)
        ):
            d = json.loads(p)
            events.append({"cycle_ts": c, "trade_id": t, "type": d.get("type"), "symbol": d.get("symbol")})  # nothing else
        cycles = [
            {"cycle_ts": c, "status": s, "payload": json.loads(p) if p else {}}
            for c, s, p in con.execute("SELECT cycle_ts, status, payload FROM cp_cycle WHERE strategy = ? ORDER BY cycle_ts", (strategy,))
        ]
        n_pos = con.execute("SELECT COUNT(*) FROM cp_position WHERE strategy = ?", (strategy,)).fetchone()[0]
        flagged = sorted(
            {str(f.get("symbol")) for c in cycles for f in (c["payload"].get("flags") or []) if isinstance(f, dict) and f.get("symbol")}
        )
        funding = {
            s: [r[0] for r in con.execute("SELECT ts FROM cp_funding WHERE venue = ? AND symbol = ? ORDER BY ts", (PERP_VENUE, s))]
            for s in flagged
        }
        con.execute("COMMIT")
    finally:
        con.close()
    return {"sides": sides, "events": events, "cycles": cycles, "n_positions": n_pos, "funding": funding}


def bp(x: Any) -> str:
    return "-" if x is None else f"{x * 1e4:.1f}"


def num(x: Any, nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def table(title: str, groups: dict[Any, dict[str, Any]], total_excess: float | None = None) -> list[str]:
    out = [
        f"\n== {title} ==",
        "group | sides | book | full | short | ratio | actual bp | model bp | fee bp | half-spr bp | impact bp | spread bp | excess share",
    ]
    for g, b in groups.items():
        share = b["excess_sum"] / total_excess if total_excess else None
        out.append(
            f"{g} | {b['sides']} | {b['with_book']} | {b['n']} | {b['depth_short']} | {num(b['ratio'])} | {bp(b['mean_actual'])} | "
            f"{bp(b['mean_model'])} | {bp(b['mean_fee'])} | {bp(b['mean_half_spread'])} | {bp(b['mean_impact'])} | "
            f"{num(b['mean_spread_bps'], 1)} | {num(share)}"
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector SQLite DB (opened read-only)")
    ap.add_argument("--strategy", default="A")
    ap.add_argument("--as-of-ms", type=int, default=0, help="cut-off UTC ms (default: now)")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--smoke", action="store_true", help="row counts only, no cost or reliability figure")
    ap.add_argument("--n-perm", type=int, default=10_000)
    a = ap.parse_args(argv)
    as_of = a.as_of_ms or int(time.time() * 1000)
    t0 = time.time()
    d = load(a.db, a.strategy)
    recs = cb.side_records(d["sides"], as_of_ms=as_of)
    print(f"strategy {a.strategy}, as_of {as_of} ({ymd_ms(as_of)}), loaded in {time.time() - t0:.1f}s, peak RSS {rss_mb():.0f} MB")
    if a.smoke:
        notionals = sorted({round(x["notional_usd"], 2) for x in recs})
        print(
            f"positions {d['n_positions']}; side rows {len(recs)}, with book {sum(x['has_book'] for x in recs)}, coins {len({x['coin'] for x in recs})}"
        )
        print(f"legs {sorted({x['leg'] for x in recs})}, events {sorted({x['event'] for x in recs})}, notionals {notionals}")
        print(
            f"cycles {len([c for c in d['cycles'] if c['cycle_ts'] <= as_of])}; flagged symbols {len(d['funding'])}; stored payments of them {sum(map(len, d['funding'].values()))}"
        )
        print(f"event rows {len(d['events'])}, types {sorted({str(e['type']) for e in d['events']})}")
        return 0

    out: list[str] = []
    pooled = cb.cost_block(recs)
    ref = cost_realism([x["payload"] for x in recs])
    out.append(
        f"(b) pooled: sides {pooled['sides']}, with book {pooled['with_book']}, full depth {pooled['n']}, ratio {num(pooled['ratio'], 3)}"
    )
    out.append(f"    consistency: carry_report.cost_realism ratio {num(ref.get('ratio'), 3)}, with_book {ref.get('with_book')}")
    tot = pooled["excess_sum"]
    out += table(
        "by coin (ranked by excess)", {c: cb.by_group(recs, lambda x: x["coin"])[c] for c in cb.concentration(recs)["ranked"]}, tot
    )
    out += table("by leg", cb.by_group(recs, lambda x: x["leg"]), tot)
    out += table("by event", cb.by_group(recs, lambda x: x["event"]), tot)
    out += table("by leg x event", cb.by_group(recs, lambda x: f"{x['leg']}/{x['event']}"), tot)
    out += table("by recorded notional USD", cb.by_group(recs, lambda x: round(x["notional_usd"], 2)), tot)
    conc = cb.concentration(recs)
    loo = [v for v in conc["leave_one_out"].values() if v is not None]
    out.append("\n== concentration (pre-registered reading) ==")
    out.append(
        f"pooled {num(conc['pooled'], 3)}; without top-1 {num(conc['without_top'].get(1), 3)}; without top-2 {num(conc['without_top'].get(2), 3)}; "
        f"top-2 share of excess {num(conc['top_excess_share'])}; leave-one-out min {num(min(loo) if loo else None, 3)} max {num(max(loo) if loo else None, 3)}"
    )
    out.append(
        f"coins with >= 2 full sides {conc['eligible']}, of them ratio > 1.5: {conc['above_threshold']}; reading: {cb.concentration_reading(conc)}"
    )
    out.append("\n== size: re-walk of the recorded books on m x notional ==")
    for c in cb.size_curve(recs):
        out.append(
            f"m {c['multiplier']}: n {c['n']}, depth short {c['depth_short']}, actual {bp(c['mean_actual'])} bp, ratio {num(c['ratio'], 3)}"
        )
    perm = cb.coin_permutation(recs, n_perm=a.n_perm)
    out.append(
        f"\n== heterogeneity across coins (labels permuted within leg x event) == n {perm['n']}, coins {perm['coins']}, p {num(perm['p'], 4)}"
    )
    ts = cb.time_split(recs)
    out.append(
        f"\n== time split at {ymd_ms(ts['cut_ts']) if ts['cut_ts'] else '-'} == first n {ts['first']['n']} ratio {num(ts['first']['ratio'], 3)}; "
        f"second n {ts['second']['n']} ratio {num(ts['second']['ratio'], 3)}; coins in both {ts.get('coins_both')}; persistence {ts['persistence']}"
    )
    nob = [x for x in recs if not x["has_book"]]
    out.append(f"\n== sides without a book ({len(nob)}) ==")
    out += [f"{x['coin']} {x['leg']}/{x['event']} {ymd_ms(x['cycle_ts'])}" for x in nob]

    rel = cb.reliability(d["cycles"], as_of_ms=as_of)
    out.append("\n== reliability (d) ==")
    out.append(
        f"cycles {rel['cycles']}, done {rel['done']}, on time {rel['on_time']} ({num(rel['share_on_time'], 3)}), "
        f"on time and complete {rel['good']} ({num(rel['share_good'], 3)}; threshold 0.95)"
    )
    out.append("incomplete cycles by issue / detail: " + "; ".join(f"{k} {v}" for k, v in rel["incomplete_by_issue"].items()))
    out.append("share (d) if cycles flagged ONLY by this issue were complete (descriptive, not the rule):")
    out += [f"  {k}: {num(v, 3)}" for k, v in rel["share_good_without"].items()]
    out.append("incomplete cycles by coin (issue / detail: cycles):")
    out += [f"  {c}: " + "; ".join(f"{k} {v}" for k, v in ks.items()) for c, ks in rel["incomplete_by_coin"].items()]
    out.append("incomplete cycles by UTC day: " + ", ".join(f"{k} {v}" for k, v in rel["incomplete_by_day"].items()))
    pub = cb.payment_published(rel["flags"], d["funding"])
    out.append("scheduled_payment_missing flags: was the payment stored later?")
    out += [
        f"  {x['coin']} {ymd_ms(x['cycle_ts'])} {x['cycle_ts']} {x['key']}: {x['status']}, interval before {num(x['interval_h_before'], 1)} h"
        for x in pub
    ]
    ev: dict[str, dict[str, int]] = {}
    for e in d["events"]:
        if e["cycle_ts"] <= as_of:
            coin = str(e["symbol"] or "-").split("/")[0]
            ev.setdefault(coin, {})[str(e["type"])] = ev.setdefault(coin, {}).get(str(e["type"]), 0) + 1
    out.append(
        "engine events by coin (type: count): "
        + "; ".join(f"{c} " + ", ".join(f"{k} {v}" for k, v in ks.items()) for c, ks in sorted(ev.items()))
    )
    out.append(f"\npeak RSS {rss_mb():.0f} MB")
    print("\n".join(out))
    path = Path(a.cache_dir) / f"breakdown_{as_of}.json"
    full = {k: v for k, v in rel.items()}
    write_json(
        path, {"as_of": as_of, "text": out, "reliability": full, "published": pub, "concentration": conc, "size": cb.size_curve(recs)}
    )
    print(f"written {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
