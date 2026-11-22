"""Post-run diagnostics for verify_pending_slots (EXPLORATORY, written after the registered run of 2026-11-18).

The registered run compares ``N(t) - P(t)`` with a fixed cap of 25, but ``guard.max_open_positions`` was 15 until the
ADR 0007 addendum of 10.11 (commit df8468e7). This script shows the open count in the guard's text over time, the
S17 / S28 rows, and recomputes variants A-D with the cap in force at each evaluation (taken from the data: the
switch time is the first decision row whose «N open positions = max» has N >= 25). Read-only DB (mode=ro).

    uv run python scripts/verify_pending_slots_diag.py
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_pending_slots as v  # noqa: E402

from tradedesk.analytics import pending_slots as ps  # noqa: E402
from tradedesk.analytics.sample_pace import WEEK_MS, poisson_ci, reason_category  # noqa: E402


def main() -> int:
    cutoff = v.ms("2026-11-18T18:00")
    d = v.load(v.DEFAULT_DB, cutoff)
    trades, first, decisions, con = d["trades"], d["first"], d["decisions"], d["con"]
    ivs, _ = v.intervals_of(trades, first, cutoff)
    by_lid = {t["link_id"]: t for t in trades}
    lim_list = [iv for lid, iv in ivs.items() if by_lid[lid]["setup"] in v.LIMIT_SETUPS]

    # 1. N in the guard text by day
    cap_rows = [(lid, r) for lid, r in decisions.items() if v.W0 <= r["ts"] < cutoff and ps.parse_open_count(r["violations"]) is not None]
    by_day: dict[str, Counter] = {}
    for _, r in cap_rows:
        by_day.setdefault(v.iso(r["ts"])[:10], Counter())[ps.parse_open_count(r["violations"])] += 1
    print("## N з тексту «N open positions = max» за днем (усі рядки рішення у вікні)")
    for day, c in sorted(by_day.items()):
        print(f"  {day}: {dict(sorted(c.items()))}")
    hi = [r["ts"] for _, r in cap_rows if (ps.parse_open_count(r["violations"]) or 0) >= 25]
    lo = [r["ts"] for _, r in cap_rows if (ps.parse_open_count(r["violations"]) or 0) < 25]
    switch = min(hi) if hi else cutoff
    print(f"останній рядок з N < 25: {v.iso(max(lo)) if lo else '—'}; перший з N ≥ 25: {v.iso(switch)}")

    # 2. S17 / S28 rows
    print("\n## Рядки S17 / S28 (поточний статус; мітки журналу до cut-off)")
    for t in sorted(trades, key=lambda x: v.ms_of(x["signal_ts"]) or 0):
        if t["setup"] not in v.LIMIT_SETUPS:
            continue
        f = first.get(t["link_id"], {})
        iv = ivs.get(t["link_id"])
        print(
            f"  {t['setup']} {t['symbol'].split('/')[0]:8s} {t['status']:8s} signal {v.iso(f.get('signal'))} sent {v.iso(f.get('sent'))}"
            f" filled(audit) {v.iso(f.get('filled'))} opened_ts {v.iso(v.ms_of(t['opened_ts']))} closed_ts {v.iso(v.ms_of(t['closed_ts']))}"
            f" rest_until {v.iso(v.ms_of(t['rest_until']))} interval {v.iso(iv.start) if iv else '—'}…{v.iso(iv.end) if iv else '—'}"
        )

    # 3. variants with the cap in force
    evals = []
    for lid, r in cap_rows:
        cats = {reason_category(x) for x in r["violations"]} - {"decided"}
        setup = r["setup"] or ""
        if r["kind"] != "skipped" or cats & v.STRUCT or setup in v.LIMIT_SETUPS or cats != {"positions_max"}:
            continue
        if r["score"] is None or int(r["score"]) < v.min_score(setup, r["ts"]):
            continue
        evals.append((r["ts"], lid, ps.parse_open_count(r["violations"]) or 0, int(r["score"])))
    rr = v.signal_rr(con, [e[1] for e in evals])
    con.close()
    holds = [
        (v.ms_of(t["closed_ts"]) or 0) - (v.ms_of(t["opened_ts"]) or 0)
        for t in trades
        if t["opened_ts"] is not None
        and v.W0 <= (v.ms_of(t["opened_ts"]) or 0)
        and t["closed_ts"] is not None
        and (v.ms_of(t["closed_ts"]) or 0) < cutoff
    ]
    h = int(statistics.median(holds))
    print(
        f"\nH {h / 3_600_000:.1f} год; придатних, зупинених лише лімітом: {len(evals)} (до перемикання {sum(1 for e in evals if e[0] < switch)})"
    )

    def cap_at(ts: int) -> int:
        # fix 1; shift n_open so that n' - P < 25  <=>  n - P < cap(t); admit_freed then frees cap(t) - (n - P) - held
        return 15 if ts < switch else 25

    # fix 2: ``opened_ts`` of a still-pending row is the placement time (ZEC, RARE), so the fill is the first audit
    # ``filled`` before the cut-off; a row without it rests until ``closed_ts`` (ended unfilled) or the cut-off
    lim_fix = []
    for t in trades:
        if t["setup"] not in v.LIMIT_SETUPS:
            continue
        f = first.get(t["link_id"], {})
        cl = v.ms_of(t["closed_ts"])
        cl = cl if f.get("filled") is None and cl is not None and cl < cutoff else None
        iv = ps.pending_interval(f.get("sent"), f.get("signal"), f.get("filled"), cl, cutoff)
        if iv is not None:
            lim_fix.append(iv)
            print(f"  fix: {t['setup']} {t['symbol'].split('/')[0]:8s} {v.iso(iv.start)}…{v.iso(iv.end)} filled={iv.filled}")
    ages = [a * v.DAY for a in (1, 3, 7, 14, 30)]
    s17_fix = [
        ps.pending_interval(first[t["link_id"]].get("sent"), None, first[t["link_id"]].get("filled"), None, cutoff)
        for t in trades
        if t["setup"] == "S17"
    ]
    print("  fix, S17 заповнення за віком:", ps.fill_by_age([iv for iv in s17_fix if iv], ages, cutoff))

    for label, t0, cap_fn, ivl in (
        ("ЛИШЕ виправлення 1 (ліміт на момент оцінки), усе вікно", v.W0, cap_at, lim_list),
        ("ЛИШЕ виправлення 2 (заповнення з журналу), ліміт 25 як у реєстрації", v.W0, lambda ts: 25, lim_fix),
        ("обидва виправлення, усе вікно", v.W0, cap_at, lim_fix),
        ("обидва виправлення, лише після перемикання на 25", switch, cap_at, lim_fix),
    ):
        lim_list_ = ivl
        weeks = (cutoff - t0) / WEEK_MS
        sel = [ps.Evaluation(ts, lid, n + 25 - cap_fn(ts), sc, rr.get(lid, 0.0)) for ts, lid, n, sc in evals if ts >= t0]
        print(f"\n## {label}: {v.iso(t0)} … {v.iso(cutoff)} ({weeks:.2f} тижня), оцінок {len(sel)}")
        print(f"  місце-дні S17+S28 у підвікні: {ps.slot_days(lim_list_, t0, cutoff):.2f}")
        for name, out in (
            ("A верхня межа", ps.freed_upper(sel, lim_list_, 25)),
            ("B цикл, без утримання", ps.admit_freed(sel, lim_list_, 25, None)),
            ("C основний, утримання H", ps.admit_freed(sel, lim_list_, 25, h)),
            ("C×0.5", ps.admit_freed(sel, lim_list_, 25, h // 2)),
            ("C×2", ps.admit_freed(sel, lim_list_, 25, 2 * h)),
            ("D утримання до cut-off", ps.admit_freed(sel, lim_list_, 25, cutoff)),
        ):
            k = len(out)
            lo_, hi_ = poisson_ci(k)
            print(f"  {name:24s} n {k:3d}  {k / weeks:6.2f} / тиждень [{lo_ / weeks:6.2f}; {hi_ / weeks:6.2f}]")
        c_ids = set(ps.admit_freed(sel, lim_list_, 25, h))
        per_day = Counter(v.iso(e.ts)[5:10] for e in sel if e.link_id in c_ids)
        print("  C за днями: " + ", ".join(f"{k} {n}" for k, n in sorted(per_day.items())))
        if ivl is lim_fix:
            no_zr = [iv for iv in lim_fix if iv.filled]  # without the two still-resting ZEC / RARE
            print(f"  C без живих на cut-off ZEC / RARE: {len(ps.admit_freed(sel, no_zr, 25, h))}")
        n_by_p = Counter(e.n_open - ps.count_active(lim_list_, e.ts) - 25 for e in sel)
        print(f"  (N − P) − ліміт на момент оцінки: {dict(sorted(n_by_p.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
