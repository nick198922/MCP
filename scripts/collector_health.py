"""Collector health: share of the expected snapshots each switched-on collector stored over the last 7 days, its gaps
and its last snapshot (item 5 of docs/ideas/IDEA-20261102-standing-improvements.md).

    uv run python scripts/collector_health.py [--db PATH] [--features PATH] [--days 7] [--out FILE]

Which collectors: the read-only jobs switched on in ``deploy/features.env`` plus the ones the collector runs by default
(atm_term, basis, funding, supply, traders, trader_positions) unless features.env switches them off (the specs and their
cadences live in the pure ``tradedesk.analytics.collector_health``). Reads the collector DB through a read-only connection (``mode=ro``: no
migration, no write), no network, no keys; nothing under ``execution/`` is imported. Prints a text report (Ukrainian);
``--out`` also writes it to a file. The morning brief (scripts/assistant_brief_tg.py) calls :func:`brief_line` and adds
its line only when a series is below 90 % or has a fresh gap, when the DB's volume has less than 15 % or 5 GB free or
would fill within 60 days at the DB's growth over 7 days (:func:`disk_check`: file size, ``shutil.disk_usage`` and the
uncompressed sizes recorded in the daily backups' gzip trailers, all read only), when a ``demo_trade`` row is still
``open`` more than a day past its hold (:func:`stuck_demo_check`, item 133: «Завислі демо-угоди: N»), or when the
assistant snapshot file (a JSON file, not a table: :func:`snapshot_check`) is missing or older than twice its job's
cadence.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import common_helpers as cmn  # scripts/common_helpers.py (the script's directory is on sys.path)

from tradedesk import features
from tradedesk.analytics import collector_health as ch
from tradedesk.analytics import stuck_demo as sd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
DEFAULT_FEATURES = ROOT / "deploy" / "features.env"
# Same rule as SNAPSHOT_PATH in scripts/dashboard.py (not imported: that module is the whole dashboard).
_DATA_DIR = Path(os.environ.get("TRADEDESK_DATA_DIR", "/var/lib/tradedesk-mcp"))
SNAPSHOT_PATH = Path(os.environ.get("TRADEDESK_ASSISTANT_SNAPSHOT_PATH") or _DATA_DIR / "assistant" / "snapshot.json")
BACKUP_NAME = re.compile(r"^tradedesk-(\d{4}-\d{2}-\d{2}T\d{6}Z)\.sqlite\.gz$")
disk_usage = shutil.disk_usage  # module-level so the tests can put the DB on a fake volume


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


def snapshot_check(path: Path, switches: dict[str, str], now: int) -> str | None:
    """The assistant snapshot's line (ch.snapshot_line on the file's ``asof_utc``, read only), or None when the file is
    fresh or features.env switches the job off. Raises nothing for a missing, unreadable or corrupt file."""
    cadence = ch.snapshot_cadence(switches)
    if cadence is None:
        return None
    try:
        snap = cmn.read_json(path)
    except FileNotFoundError:
        return ch.snapshot_line(None, now, cadence, exists=False)
    except (OSError, ValueError):
        snap = None  # unreadable or corrupt: its time cannot be read
    return ch.snapshot_line(snap.get("asof_utc") if isinstance(snap, dict) else None, now, cadence)


def gz_size(path: Path, ref: int | None = None) -> int:
    """Uncompressed size of a gzip file from its trailer (ISIZE: the last 4 bytes, the size modulo 2**32), read only.
    With ``ref`` (the live DB's size) the wrapped value is lifted to the multiple of 2**32 nearest ``ref``, which holds
    while the DB changed by less than 2 GiB since that backup. Raises ``OSError`` when the file cannot be read."""
    with path.open("rb") as f:
        f.seek(-4, os.SEEK_END)
        isize = int.from_bytes(f.read(4), "little")
    return isize if ref is None else isize + round((ref - isize) / 2**32) * 2**32


def backup_sizes(backups: Path, ref: int | None = None) -> list[tuple[int, int]]:
    """(ts, bytes) of the DB at each daily backup (deploy/backup-sqlite.sh: ``tradedesk-YYYY-MM-DDTHHMMSSZ.sqlite.gz``,
    an online ``.backup`` copy, so its uncompressed size is the DB's size then). [] without the directory; a file that
    cannot be read is skipped."""
    out: list[tuple[int, int]] = []
    if not backups.is_dir():
        return out
    for p in backups.glob("tradedesk-*.sqlite.gz"):
        m = BACKUP_NAME.search(p.name)
        if not m:
            continue
        try:
            ts = int(datetime.strptime(m.group(1), "%Y-%m-%dT%H%M%SZ").replace(tzinfo=UTC).timestamp())
            out.append((ts, gz_size(p, ref)))
        except (OSError, ValueError):
            continue
    return out


def disk_check(db: str, now: int, backups: Path | None = None) -> dict[str, Any]:
    """ch.disk_health of the DB file (its size, may be absent), the free space of its volume (``disk_usage`` on the
    DB's directory, read only) and its growth over 7 days from the backups (``backups``, default ``backups/`` next to
    the DB) plus the live size. Without the DB file the newest backup's trailer size is the 2**32 lift's ``ref``: the
    backups' sizes then agree with each other (so the growth holds) though their level is known only modulo 2**32.
    Raises ``OSError`` when the DB's directory is not there."""
    p = Path(db)
    db_bytes = p.stat().st_size if p.is_file() else None
    usage = disk_usage(p.parent)
    folder = p.parent / "backups" if backups is None else backups
    ref = db_bytes
    if ref is None:
        raw = backup_sizes(folder)  # trailer sizes as stored (mod 2**32)
        ref = max(raw)[1] if raw else None  # the newest backup's
    points = backup_sizes(folder, ref)
    if db_bytes is not None:
        points.append((now, db_bytes))
    return ch.disk_health(db_bytes, usage.free, usage.total, ch.growth_per_day(points, now))


def stuck_demo_check(db: str, now: int) -> list[dict[str, Any]]:
    """sd.stuck_trades of the ``open`` demo_trade rows (read-only connection, ``now`` in unix seconds); [] without the
    table. Raises ``sqlite3.Error`` / ``FileNotFoundError`` when the DB cannot be read."""
    con = connect_ro(db)
    try:
        if not cmn.has_table(con, "demo_trade"):
            return []
        q = "SELECT link_id, setup, symbol, timeframe, status, signal_ts, opened_ts FROM demo_trade WHERE status = 'open'"
        cols = ("link_id", "setup", "symbol", "timeframe", "status", "signal_ts", "opened_ts")
        rows = [dict(zip(cols, r, strict=True)) for r in con.execute(q)]
    finally:
        con.close()
    return sd.stuck_trades(rows, now * 1000)


def brief_line(
    db: str = DEFAULT_DB, features_path: Path = DEFAULT_FEATURES, now: int | None = None, snapshot: Path | None = None
) -> str | None:
    """The morning brief's line (plain text), or None when every series, the DB's disk (:func:`disk_check`), the open
    demo trades (:func:`stuck_demo_check`) and the assistant snapshot file (``snapshot``, default
    :data:`SNAPSHOT_PATH`) are fine. Never raises: an unreadable DB gives a line saying the check did not run."""
    now = int(time.time()) if now is None else now
    switches: dict[str, str] = {}
    try:
        switches = load_switches(features_path)
        line = ch.brief_line(collect(db, switches, now))
    except (sqlite3.Error, OSError) as e:
        line = f"Здоров'я колекторів не перевірено: БД не відкрито ({type(e).__name__})."
    except Exception as e:  # noqa: BLE001 - the brief always goes out
        line = f"Здоров'я колекторів не перевірено ({type(e).__name__})."
    try:
        disk = ch.disk_line(disk_check(db, now))
    except Exception as e:  # noqa: BLE001 - the brief always goes out
        disk = f"Місце на диску БД не перевірено ({type(e).__name__})."
    try:
        stuck = sd.brief_line(stuck_demo_check(db, now))
    except FileNotFoundError:
        stuck = None  # no DB file: the collectors' line above already says so
    except Exception as e:  # noqa: BLE001 - the brief always goes out
        stuck = f"Завислі демо-угоди не перевірено ({type(e).__name__})."
    try:
        snap = snapshot_check(SNAPSHOT_PATH if snapshot is None else snapshot, switches, now)
    except Exception as e:  # noqa: BLE001 - the brief always goes out
        snap = f"Вік знімка асистента не перевірено ({type(e).__name__})."
    return " ".join(x for x in (line, disk, stuck, snap) if x) or None


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
    try:
        h = disk_check(a.db, now)
        disk_row = f"{'✗' if h['reasons'] else '✓'} Диск БД: {ch.describe_disk(h)}.\n"
        disk_brief = ch.disk_line(h)
    except OSError as e:
        disk_row = f"✗ Диск БД не перевірено ({type(e).__name__}).\n"
        disk_brief = f"Місце на диску БД не перевірено ({type(e).__name__})."  # as brief_line says it
    text = ch.render(results, now, a.days, extra=[disk_brief]) + disk_row
    try:
        stuck = stuck_demo_check(a.db, now)
        text += f"✗ {sd.brief_line(stuck)}\n" if stuck else "✓ Завислі демо-угоди: 0.\n"
    except (sqlite3.Error, OSError) as e:
        text += f"✗ Завислі демо-угоди не перевірено ({type(e).__name__}).\n"
    print(text, end="")
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
