"""Collector health: share of the expected snapshots each switched-on collector stored over the last 7 days, its gaps
and its last snapshot (item 5 of docs/ideas/IDEA-20261102-standing-improvements.md).

    uv run python scripts/collector_health.py [--db PATH] [--features PATH] [--days 7] [--out FILE]

Which collectors: the read-only jobs switched on in ``deploy/features.env`` (the specs and their cadences live in the
pure ``tradedesk.analytics.collector_health``). Reads the collector DB through a read-only connection (``mode=ro``: no
migration, no write), no network, no keys; nothing under ``execution/`` is imported. Prints a text report (Ukrainian);
``--out`` also writes it to a file. The morning brief (scripts/assistant_brief_tg.py) calls :func:`brief_line` and adds
its line only when a series is below 90 % or has a fresh gap.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)

from tradedesk import features
from tradedesk.analytics import collector_health as ch

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_FEATURES = ROOT / "deploy" / "features.env"


def connect_ro(db: str) -> sqlite3.Connection:
    """A read-only connection; raises ``sqlite3.Error`` / ``FileNotFoundError`` when the DB is not there."""
    if not Path(db).exists():
        raise FileNotFoundError(db)
    return cmn.connect_ro(Path(db))


def load_switches(path: Path = DEFAULT_FEATURES) -> dict[str, str]:
    """Accepted switches of features.env (same parser as the service); {} when the file cannot be read."""
    try:
        return features.parse(path.read_text(encoding="utf-8"))[0]
    except OSError:
        return {}


def series(con: sqlite3.Connection, spec: ch.Spec, group: str, now: int, days: int) -> dict[str, Any]:
    """One series' health from the DB; a failed query gives an 'error' row (never raises sqlite3.Error)."""
    cond = f"({spec.where})" + (f" AND {spec.group_col} = ?" if spec.group_col else "")
    args: tuple[Any, ...] = (group,) if spec.group_col else ()
    p = spec.period_s
    try:
        first, last = con.execute(f"SELECT MIN({spec.ts_sql}), MAX({spec.ts_sql}) FROM {spec.table} WHERE {cond}", args).fetchone()
        lo = now - days * ch.DAY - p
        q = f"SELECT DISTINCT ({spec.ts_sql}) / {p} FROM {spec.table} WHERE {cond} AND {spec.ts_sql} >= ? AND {spec.ts_sql} <= ?"
        stamps = [int(b) * p for (b,) in con.execute(q, (*args, lo, now)) if b is not None]
    except sqlite3.Error as e:
        return ch.error_health(spec, group, f"запит не вдався: {type(e).__name__}")
    first = int(first) if first is not None else None
    last = int(last) if last is not None else None
    return ch.series_health(spec, group, stamps, first, last, now, days)


def collect(db: str, switches: dict[str, str], now: int, days: int = ch.WINDOW_DAYS) -> list[dict[str, Any]]:
    """Health of every series of the switched-on collectors. Raises ``sqlite3.Error`` / ``OSError`` only when the DB
    cannot be opened."""
    con = connect_ro(db)
    try:
        return [series(con, spec, g, now, days) for spec in ch.enabled_specs(switches) for g in spec.groups]
    finally:
        con.close()


def brief_line(db: str = DEFAULT_DB, features_path: Path = DEFAULT_FEATURES, now: int | None = None) -> str | None:
    """The morning brief's line (plain text), or None when every series is fine. Never raises: an unreadable DB gives a
    line saying the check did not run."""
    try:
        results = collect(db, load_switches(features_path), int(time.time()) if now is None else now)
    except (sqlite3.Error, OSError) as e:
        return f"Здоров'я колекторів не перевірено: БД не відкрито ({type(e).__name__})."
    except Exception as e:  # noqa: BLE001 - the brief always goes out
        return f"Здоров'я колекторів не перевірено ({type(e).__name__})."
    return ch.brief_line(results)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB, help="collector SQLite DB (opened read-only)")
    ap.add_argument("--features", type=Path, default=DEFAULT_FEATURES, help="features.env with the switched-on jobs")
    ap.add_argument("--days", type=int, default=ch.WINDOW_DAYS, help="window, days (default 7)")
    ap.add_argument("--out", help="also write the text to this file")
    a = ap.parse_args(argv)
    now = int(time.time())
    try:
        results = collect(a.db, load_switches(a.features), now, a.days)
    except (sqlite3.Error, OSError) as e:
        print(f"БД {a.db} не відкрито ({type(e).__name__})", file=sys.stderr)
        return 1
    text = ch.render(results, now, a.days)
    print(text, end="")
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
