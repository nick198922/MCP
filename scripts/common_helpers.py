"""Small stdlib helpers shared by scripts/*.py (the dashboard runs on the system python3: stdlib only, no tradedesk).

Read-only SQLite: a script that reads the live store opens it with ``mode=ro`` (no migration, no write) and the path
URL-quoted, so a ``?``, ``#`` or ``%`` in the path cannot cut the file name short or drop the read-only flag.
A verification script that works on a copy of the store makes it with ``backup_db`` (sqlite3 online backup).
Long verification runs log their peak memory with ``rss_mb``.
"""

from __future__ import annotations

import resource
import sqlite3
import urllib.parse
from pathlib import Path


def connect_ro(path: str | Path, timeout: float = 5) -> sqlite3.Connection:
    """A read-only connection (``file:<quoted path>?mode=ro``); raises ``sqlite3.Error`` as ``sqlite3.connect`` does."""
    return sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=timeout)


def open_ro(path: Path) -> tuple[sqlite3.Connection | None, str | None]:
    """(read-only connection, None), or (None, reason) with reason 'missing' (no file) or 'unreadable: <exception
    name>' (e.g. a PermissionError from exists() on a closed directory). Never raises; the caller closes it."""
    try:
        if not path.exists():
            return None, "missing"
        return connect_ro(path), None
    except (sqlite3.Error, OSError) as e:
        return None, f"unreadable: {type(e).__name__}"


def backup_db(src: str | Path, dst: Path) -> None:
    """Copy the store at ``src`` into ``dst`` with ``sqlite3`` online backup (consistent while the collector writes):
    ``src`` opened read-only through ``connect_ro``, ``dst``'s directory created if needed, an existing ``dst``
    overwritten. Raises ``sqlite3.Error`` (e.g. ``src`` missing) and closes both connections either way."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    a = connect_ro(src)
    try:
        b = sqlite3.connect(dst)
        try:
            a.backup(b)
        finally:
            b.close()
    finally:
        a.close()


def has_table(con: sqlite3.Connection, name: str) -> bool:
    """True when table ``name`` exists in the store; raises ``sqlite3.Error`` as any query does."""
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def rss_mb() -> float:
    """Peak resident memory of this process so far, in MB (``ru_maxrss`` is in KiB on Linux)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
