"""Step 1 of IDEA-20261028-hl-oi-cap-state-forward: gates G1/G2 on the collected OI-cap snapshots (counting only).

    uv run python scripts/verify_hl_oi_cap_gates.py --stage smoke [--out FILE]                       # coverage only
    uv run python scripts/verify_hl_oi_cap_gates.py --stage run --cutoff 2026-11-20T00:00 [--out FILE]  # registered run

Reads ``hl_oi_cap_snapshot`` from the collector DB over a read-only connection (``mode=ro``: no migration, no write),
no network, no keys, nothing from ``execution/``. ``smoke`` prints only coverage (rows, hours, gaps, coins per hour,
NULL funding), never ``at_cap`` or events. ``run`` counts the events registered in the card («Попередня реєстрація»,
step 1) with the pure ``tradedesk.analytics.hl_oi_cap_events`` and prints G1/G2 plus the descriptive variants (3 h,
12 h, no 24 h rule). No prices or returns are read. ``--out`` writes the same text plus a JSON block to a file.
"""

from __future__ import annotations

import argparse
import json
import resource
import sqlite3
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)

from tradedesk.analytics import hl_oi_cap_events as ev

DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
REGISTERED_CUTOFF = "2026-11-20T00:00"
HOUR = ev.HOUR_S


def _utc(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def parse_cutoff(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp())


def load(conn: sqlite3.Connection, cutoff: int) -> list[tuple[int, str, int, float | None]]:
    """(ts, coin, at_cap, funding) rows with ts < cutoff, oldest first."""
    cur = conn.execute("SELECT ts, coin, at_cap, funding FROM hl_oi_cap_snapshot WHERE ts < ? ORDER BY ts, coin", (cutoff,))
    return [(int(t), str(c), int(a), f) for t, c, a, f in cur.fetchall()]


def smoke(rows: list[tuple[int, str, int, float | None]], cutoff: int) -> tuple[list[str], dict]:
    """Coverage only: no at_cap, no events."""
    if not rows:
        return ["hl_oi_cap_snapshot: немає рядків до cutoff"], {"rows": 0}
    first = rows[0][0]
    per_hour: dict[int, int] = {}
    for ts, *_ in rows:
        per_hour[ts] = per_hour.get(ts, 0) + 1
    cov = ev.coverage(per_hour, first, cutoff)
    counts = list(per_hour.values())
    out = {
        "rows": len(rows),
        "first_ts": first,
        "last_ts": rows[-1][0],
        "coins": len({r[1] for r in rows}),
        "coins_per_hour": [min(counts), statistics.median(counts), max(counts)],
        "funding_null": sum(1 for r in rows if r[3] is None),
        **cov,
    }
    lines = [
        f"Період: {_utc(first)} → {_utc(rows[-1][0])} UTC (cutoff {_utc(cutoff)}); рядків {out['rows']}, монет {out['coins']}",
        f"Годин: очікувано {cov['expected']}, збережено {cov['stored']}, пропущено {cov['missing']},"
        f" найдовша дірка {cov['longest_gap_h']} год",
        f"Монет на годину: min {counts and min(counts)}, медіана {statistics.median(counts)}, max {max(counts)};"
        f" funding NULL: {out['funding_null']}",
    ]
    return lines, out


def run(rows: list[tuple[int, str, int, float | None]], cutoff: int) -> tuple[list[str], dict]:
    first = rows[0][0]
    main = ev.events(rows, first_ts=first, cutoff=cutoff)
    g = ev.gates(main, first_ts=first, cutoff=cutoff)
    ci = g["share_pos_ci95"]
    lines = [
        f"Експозиція: {_utc(first + 24 * HOUR)} → {_utc(cutoff)} UTC = {g['exposure_weeks']:.2f} тижня",
        f"Подій (≥ 6 год, вперше за 24 год): n = {g['n']}, монет {g['coins']},"
        f" частка найбільшої {g['top_coin_share'] if g['top_coin_share'] is None else round(g['top_coin_share'], 3)}",
        f"G1: темп {g['rate_per_week'] if g['rate_per_week'] is None else round(g['rate_per_week'], 2)} подій/тиждень"
        f" (поріг ≥ {ev.G1_MIN_PER_WEEK:g}) → {'пройдено' if g['g1_pass'] else 'ПРОВАЛ'}",
        f"G2: funding > 0 у {g['funding_pos']}/{g['funding_known']}"
        f" = {g['share_pos'] if g['share_pos'] is None else round(g['share_pos'], 3)}"
        f" (CI95 {None if ci is None else (round(ci[0], 3), round(ci[1], 3))}; межі {ev.G2_LOW}–{ev.G2_HIGH})"
        f" → {'пройдено' if g['g2_pass'] else 'ПРОВАЛ'}",
        "По тижнях назад від cutoff (0 = останній): " + str(g["per_week_back"]),
        "Монети (топ-10): " + ", ".join(f"{c} {k}" for c, k in g["top_coins"]),
        f"Ворота: {'обидва пройдено' if g['pass'] else 'провал (критерій «вбити»)'}",
        "",
        "Описові варіанти (рішення не змінюють):",
    ]
    variants = {}
    for name, kw in (("3h", {"min_hours": 3}), ("12h", {"min_hours": 12}), ("6h_no24h", {"lookback_s": None})):
        e2 = ev.events(rows, first_ts=first, cutoff=cutoff, **kw)
        g2 = ev.gates(e2, first_ts=first, cutoff=cutoff)
        variants[name] = {k: g2[k] for k in ("n", "rate_per_week", "coins", "funding_pos", "funding_known", "share_pos")}
        lines.append(
            f"  {name}: n {g2['n']}, темп {g2['rate_per_week'] and round(g2['rate_per_week'], 2)}/тижд,"
            f" монет {g2['coins']}, funding>0 {g2['funding_pos']}/{g2['funding_known']}"
        )
    if g["pass"] and g["rate_per_week"]:
        for target in (100, 250):
            weeks = max(target - g["n"], 0) / g["rate_per_week"]
            lines.append(f"Оцінка n ≥ {target}: ≈ {_utc(cutoff + weeks * ev.WEEK_S)[:10]} (за темпом {g['rate_per_week']:.1f}/тижд)")
    events_out = [{"coin": e.coin, "start": _utc(e.start_ts), "t0": _utc(e.t0), "funding": e.funding} for e in main]
    return lines, {"gates": {k: v for k, v in g.items() if k != "top_coins"}, "variants": variants, "events": events_out}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", choices=("smoke", "run"), required=True)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"collector SQLite, opened read-only (default {DEFAULT_DB})")
    ap.add_argument("--cutoff", default=REGISTERED_CUTOFF, help="UTC YYYY-MM-DDTHH:MM, exclusive (registered 2026-11-20T00:00)")
    ap.add_argument("--out", help="also write the text and a JSON block to this file")
    a = ap.parse_args(argv)
    if a.stage == "run" and a.cutoff != REGISTERED_CUTOFF:
        print(f"увага: cutoff {a.cutoff} відрізняється від зареєстрованого {REGISTERED_CUTOFF} (розвідувальний прогін)")
    cutoff = parse_cutoff(a.cutoff)
    conn, why = cmn.open_ro(Path(a.db))
    if conn is None:
        print(f"БД недоступна ({a.db}: {why}); нічого не пораховано")
        return 2
    try:
        rows = load(conn, cutoff)
    except sqlite3.Error as e:
        print(f"БД: помилка читання ({type(e).__name__}: {e})")
        return 2
    finally:
        conn.close()
    lines, data = smoke(rows, cutoff)
    if a.stage == "run" and rows:
        more, extra = run(rows, cutoff)
        lines += ["", *more]
        data.update(extra)
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    lines.append(f"peak RSS {rss_mb:.0f} МБ")
    text = "\n".join(lines)
    print(text)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n\n" + json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
