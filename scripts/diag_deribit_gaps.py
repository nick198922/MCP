"""Which collector series that read Deribit lose rows or legs, per UTC day, and how many losses fall right after a
service restart (item 460 of docs/ideas/IDEA-20261102-standing-improvements.md; docs/changes/2026-11-25-deribit-collector-gaps.md).

    uv run python scripts/diag_deribit_gaps.py [--db PATH] [--since 2026-11-13] [--restart-window 180]

Read-only: the store is opened through ``common_helpers.connect_ro`` (``mode=ro``: no migration, no write); no network,
no keys, nothing under ``execution/``. Series and what counts as a loss:

- ``atm_term`` / ``basis_hist`` (every 5 min): a run (distinct ``ts``) with one currency instead of two; a gap of more
  than 450 s between runs (a run that failed as a whole, or the service down).
- ``funding_hist`` (hourly): a run without its two ``deribit`` rows (the job swallows a Deribit failure).
- ``pm_gap_snapshot`` (hourly): a (snapshot, asset) whose rows all have ``surface_ts`` NULL (Deribit book summary failed:
  no model probability), or ``dvol`` NULL; rows with ``surface_ts - book_ts`` > 60 s (left out of the paper test).
- ``option_xvenue_snapshot`` (hourly rows): a row without the Deribit leg (``d_instrument`` NULL) or the Bybit leg.
- ``option_leg_snapshot`` (daily): a leg on the book-summary quote (``src = 'summary'``: its ticker failed).

Restarts are inferred from the 5-minute ``atm_term`` cadence: an interval between runs below 295 s, or one that is not
within 30 s of a multiple of 300 s, resets the phase (a failed run keeps it). A restart whose new phase lands near the
old one is not seen. Times are UTC.
"""

from __future__ import annotations

import argparse
import bisect
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M:%S")


def restarts(run_ts: list[int]) -> list[int]:
    """Run times that start a new phase of a 300 s job (see the module docstring)."""
    out = []
    for a, b in zip(run_ts, run_ts[1:], strict=False):
        d = b - a
        if d < 295 or abs(d - 300 * round(d / 300)) > 30:
            out.append(b)
    return out


def after_restart(ts: int, rs: list[int], window: int) -> bool:
    """True when ``ts`` lies within ``window`` seconds after a restart in ``rs`` (sorted); a restart is matched up to
    300 s late because it is only seen at the first run of its new phase."""
    i = bisect.bisect_right(rs, ts + 300)
    return i > 0 and -300 <= ts - rs[i - 1] <= window


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--since", default="2026-11-13", help="first UTC day")
    ap.add_argument("--restart-window", type=int, default=180, help="seconds after a restart that count as 'at start'")
    a = ap.parse_args()
    since = int(datetime.fromisoformat(a.since).replace(tzinfo=UTC).timestamp())
    con = cmn.connect_ro(a.db)
    con.row_factory = sqlite3.Row
    w = a.restart_window

    atm = [r[0] for r in con.execute("SELECT DISTINCT ts FROM atm_term WHERE ts >= ? ORDER BY ts", (since - 3600,))]
    rs = restarts(atm)
    seen = [r for r in rs if r >= since]
    at_20 = sum(19 <= datetime.fromtimestamp(r, UTC).minute <= 23 for r in seen)
    print(f"restarts inferred from atm_term since {a.since}: {len(seen)}, {at_20} of them at :19-:23 past the hour")
    print("  last 10: " + ", ".join(_hm(r) for r in seen[-10:]))

    # day -> series -> [events, events at start, base]
    tab: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))

    def hit(series: str, ts: int, n: int = 1) -> None:
        c = tab[_day(ts)][series]
        c[0] += n
        c[1] += n if after_restart(ts, rs, w) else 0

    def base(series: str, ts: int, n: int = 1) -> None:
        tab[_day(ts)][series][2] += n

    for table in ("atm_term", "basis_hist"):
        runs = con.execute(f"SELECT ts, COUNT(DISTINCT currency) FROM {table} WHERE ts >= ? GROUP BY ts ORDER BY ts", (since,)).fetchall()
        for ts, ncur in runs:
            base(f"{table}: one-currency runs", ts)
            base(f"{table}: gaps > 450 s", ts)
            if ncur < 2:
                hit(f"{table}: one-currency runs", ts)
        for (a_ts, _), (b_ts, _) in zip(runs, runs[1:], strict=False):
            if b_ts - a_ts > 450:
                hit(f"{table}: gaps > 450 s", b_ts, max(1, round((b_ts - a_ts) / 300) - 1))

    for ts, n_der in con.execute(
        "SELECT ts, SUM(venue = 'deribit') FROM funding_hist WHERE ts >= ? GROUP BY ts ORDER BY ts", (since,)
    ).fetchall():
        base("funding_hist: runs without deribit", ts)
        if not n_der:
            hit("funding_hist: runs without deribit", ts)

    for r in con.execute(
        "SELECT ts, asset, COUNT(*) n, SUM(surface_ts IS NULL) no_surf, SUM(dvol IS NULL) no_dvol, "
        "SUM(surface_ts IS NOT NULL AND book_ts IS NOT NULL AND ABS(surface_ts - book_ts) > 60) late "
        "FROM pm_gap_snapshot WHERE ts >= ? GROUP BY ts, asset ORDER BY ts",
        (since,),
    ).fetchall():
        base("pm_gap: (snapshot, asset) without Deribit summary", r["ts"])
        base("pm_gap: (snapshot, asset) without DVOL", r["ts"])
        base("pm_gap: rows surface-book > 60 s", r["ts"], r["n"])
        if r["no_surf"] == r["n"]:
            hit("pm_gap: (snapshot, asset) without Deribit summary", r["ts"])
        if r["no_dvol"] == r["n"]:
            hit("pm_gap: (snapshot, asset) without DVOL", r["ts"])
        if r["late"]:
            hit("pm_gap: rows surface-book > 60 s", r["ts"], r["late"])

    for r in con.execute(
        "SELECT ts, COUNT(*) n, SUM(d_instrument IS NULL) nd, SUM(b_symbol IS NULL) nb FROM option_xvenue_snapshot "
        "WHERE ts >= ? AND mode = 'hourly' GROUP BY ts ORDER BY ts",
        (since,),
    ).fetchall():
        base("option_xvenue: rows without Deribit leg", r["ts"], r["n"])
        base("option_xvenue: rows without Bybit leg", r["ts"], r["n"])
        if r["nd"]:
            hit("option_xvenue: rows without Deribit leg", r["ts"], r["nd"])
        if r["nb"]:
            hit("option_xvenue: rows without Bybit leg", r["ts"], r["nb"])

    if cmn.has_table(con, "option_leg_snapshot"):
        for r in con.execute(
            "SELECT ts, COUNT(*) n, SUM(src = 'summary') ns FROM option_leg_snapshot WHERE ts >= ? GROUP BY ts ORDER BY ts", (since,)
        ).fetchall():
            base("option_legs: legs on summary quote (ticker failed)", r["ts"], r["n"])
            if r["ns"]:
                hit("option_legs: legs on summary quote (ticker failed)", r["ts"], r["ns"])

    series = sorted({s for d in tab.values() for s in d})
    print(f"\nlosses per UTC day: events / base (of them within {w} s after a restart)")
    for s in series:
        print(f"\n{s}")
        tot = [0, 0, 0]
        for day in sorted(tab):
            c = tab[day].get(s)
            if c is None:
                continue
            tot = [x + y for x, y in zip(tot, c, strict=True)]
            print(f"  {day}: {c[0]} / {c[2]} ({c[1]} at start)")
        print(f"  total: {tot[0]} / {tot[2]} ({tot[1]} at start)")

    print("\nprovider-health alerts for deribit (alerts table, channel log):")
    for r in con.execute(
        "SELECT ts, json_extract(payload, '$.message') m FROM alerts WHERE rule_id = 'health:deribit' AND channel = 'log' AND ts >= ? ORDER BY ts",
        (since,),
    ):
        print(f"  {_hm(r['ts'])} {r['m']}")
    con.close()


if __name__ == "__main__":
    main()
