"""Part (a) of the weekly condor forward test (item 8j of docs/ideas/IDEA-20261031-structural-edges-depth.md): the
executable cost of the condor vs the model "mark -/+ 1.5 vol points" on the collector's `option_leg_snapshot` rows.

Prints ONLY what rule (a) lists ("звіт (а) друкує лише витрати"): actual, model, R with its CI and the verdict, the
half-spreads in vol points by role, the share of legs whose best-level size is < 1 coin, for (a1) the cost as a share
of the entry credit at the mark, counters and skip reasons. Never a Thursday mark / spot / credit / leg price, a
delivery price or a P&L: the report is built by `analytics.condor_forward.cost_report` (whitelisted keys only), the
script adds no field of its own and does not use the network.

Sample: the Fridays T0 ... T0 + 7 weeks (T0 default 2026-11-06); BTC primary, ETH descriptive; (a1) entry primary,
(a2) Thursday buy-back second. The registered run is after the last buy-back snapshot (Thursday T0 + 7 weeks + 6
days, 10:00 UTC); before it the script refuses unless ``--interim`` (marked INTERIM: (a1) without a verdict, (a2)
neither read, computed nor printed). Before EARLIEST_T0 (2026-11-06) 10:00 UTC, or with ``--t0`` earlier than it,
the script exits without touching the store, whatever the flags.

    uv run python scripts/condor_forward_costs.py [--db /var/lib/tradedesk-mcp/tradedesk.sqlite] [--t0 2026-11-06]
        [--out ~/.cache/tradedesk-agents/condor_forward/costs_a.md] [--interim]
"""

from __future__ import annotations

import argparse
import json
import resource
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tradedesk.analytics.condor_forward import (
    EARLIEST_T0,
    ENTRY_CUTOFF_HOUR_UTC,
    H1_QTY,
    cost_fridays,
    cost_report,
    foreign_keys,
    format_cost_report,
    json_safe,
)

DB_SRC = Path("/var/lib/tradedesk-mcp/tradedesk.sqlite")
CACHE = Path.home() / ".cache" / "tradedesk-agents" / "condor_forward"
ROLES = (*H1_QTY, *("hold_" + r for r in H1_QTY))
CURRENCIES = (("BTC", "primary"), ("ETH", "descriptive, outside the decision"))


def copy_db(src: Path, dst: Path) -> None:
    """A consistent copy of the live store through the sqlite3 backup API (the source opened read-only)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    with b:
        a.backup(b)
    a.close()
    b.close()


def load_rows(db: Path, fridays: list[date], *, with_hold: bool = True) -> list[dict]:
    """The H1 entry rows of the Fridays and (unless ``with_hold=False``, the interim look) the `hold_<role>` rows of
    the Thursdays after them, read-only."""
    days = [f.isoformat() for f in fridays]
    roles = ROLES if with_hold else tuple(H1_QTY)
    if with_hold:
        days += [(f + timedelta(days=6)).isoformat() for f in fridays]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = (
        f"SELECT * FROM option_leg_snapshot WHERE day IN ({','.join('?' * len(days))}) "
        f"AND currency IN ('BTC', 'ETH') AND role IN ({','.join('?' * len(roles))})"
    )
    rows = [dict(r) for r in con.execute(q, [*days, *roles])]
    con.close()
    return rows


def registered_end(t0: date) -> datetime:
    """The last buy-back snapshot of the sample: Thursday T0 + 7 weeks + 6 days, 10:00 UTC."""
    thu = cost_fridays(t0)[-1] + timedelta(days=6)
    return datetime(thu.year, thu.month, thu.day, ENTRY_CUTOFF_HOUR_UTC, tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _cut(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, ENTRY_CUTOFF_HOUR_UTC, tzinfo=UTC)


def build(rows: list[dict], fridays: list[date], *, interim: bool) -> tuple[list[str], dict]:
    """Report lines and the per-currency dicts; exits (writing nothing) if a key outside ``REPORT_KEYS`` appears."""
    L = ["## Part (a): condor cost, actual (bid / ask) vs model mark -/+ 1.5 vp (item 8j)", ""]
    if interim:
        L += [
            "**INTERIM: before the last buy-back snapshot of the sample; not the registered run, no verdict; (a2) buy-back not computed.**",
            "",
        ]
    L += [
        "Costs: |execution - mark| summed over the 4 H1 legs (lp sp10 sc10 lc), fractions of spot per 1 coin of notional "
        "per leg (shown in % of spot). R and its 95 % CI: ratio of sums over weeks, bootstrap over weeks, 10 000, seed "
        "20261031. Verdict: < 6 valid weeks of 8 -> ◯; CI upper < 1 -> conservative; CI lower > 1 -> optimistic; "
        "else consistent. Best-level size: the side the leg trades on.",
        "",
    ]
    reps = {}
    for cur, label in CURRENCIES:
        rep = cost_report(rows, fridays, cur, interim=interim)
        bad = foreign_keys(rep)
        if bad:  # the whitelist is enforced at run time, not only by the tests
            sys.exit(f"report of {cur} carries keys outside REPORT_KEYS: {sorted(bad)}; nothing written")
        reps[cur] = rep
        L += format_cost_report(rep, label)
    return L, reps


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_SRC, help="live SQLite store (copied first, read-only)")
    ap.add_argument("--t0", type=date.fromisoformat, default=EARLIEST_T0, help="first Friday of the sample (T0)")
    ap.add_argument("--out", type=Path, default=CACHE / "costs_a.md")
    ap.add_argument("--interim", action="store_true", help="allow a run before the last buy-back snapshot (marked INTERIM, (a1) only)")
    a = ap.parse_args(argv)
    if a.t0.weekday() != 4:
        sys.exit(f"T0 {a.t0} is not a Friday")
    if a.t0 < EARLIEST_T0:
        sys.exit(f"T0 {a.t0} is before the earliest registered T0 {EARLIEST_T0}")
    now = _now()
    for d in (EARLIEST_T0, a.t0):  # the registered T0 guards independently of --t0
        if now < _cut(d):
            sys.exit(f"before T0 ({_cut(d):%Y-%m-%d %H:%M} UTC): the registration forbids reading the snapshots")
    fridays = cost_fridays(a.t0)
    end = registered_end(a.t0)
    interim = now < end
    if interim and not a.interim:
        sys.exit(f"the sample ends {end:%Y-%m-%d %H:%M} UTC; rerun then, or pass --interim for a marked interim look")
    copy = CACHE / f"store_copy_{now:%Y%m%dT%H%M%S}.sqlite"
    print(f"copying {a.db} -> {copy}", flush=True)
    copy_db(a.db, copy)
    rows = load_rows(copy, fridays, with_hold=not interim)
    what = "entries only (interim)" if interim else "and their Thursdays"
    print(f"{len(rows)} rows read (Fridays {fridays[0]} … {fridays[-1]}, {what})", flush=True)
    L, reps = build(rows, fridays, interim=interim)
    L.append(
        f"run {now:%Y-%m-%d %H:%M} UTC, --t0 {a.t0}{' (INTERIM)' if interim else ''}, store copy {copy.name}, "
        f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB"
    )
    payload = json.dumps(json_safe(reps), indent=1, allow_nan=False) + "\n"  # NaN -> null: standard JSON
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(L) + "\n")
    a.out.with_suffix(".json").write_text(payload)
    print("\n".join(L))


if __name__ == "__main__":
    main()
