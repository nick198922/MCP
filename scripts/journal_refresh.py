"""Refresh the owner's real trades in the journal: Bybit (read-only key) and Hyperliquid (public fills by address).

    uv run python scripts/journal_refresh.py [--days 30]
    uv run python scripts/journal_refresh.py --set-hl-address 0xADDRESS     # once; kept outside git

Started by the «Оновити угоди» button of the dashboard's «Журнал» tab (scripts/dashboard.py, POST /journal/refresh).
Runs scripts/bybit_import.py import --days N and scripts/hyperliquid_import.py import ADDRESS one after the other; both
are read-only and skip records already in the journal, so a re-run only adds new trades. Progress and the result go
to a small JSON status file the dashboard reads; a lock file keeps one run at a time. Importer output is never copied
into the status (only how many rows each venue added and, on failure, a short reason): no keys, no addresses.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_DEFAULT = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
STATE = Path(os.environ.get("TRADEDESK_DASHBOARD_STATE", Path.home() / ".local/state/tradedesk-dashboard"))
STATUS = STATE / "journal-refresh.json"
LOCK = STATE / "journal-refresh.lock"
HL_ADDRESS_FILE = Path(os.environ.get("TRADEDESK_HL_ADDRESS_FILE", "/var/lib/tradedesk-mcp/journal/hl-address"))
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
TIMEOUT_S = 900
VENUES = ("bybit", "hyperliquid")


def count_rows(db: str, venue: str) -> int | None:
    """Imported rows of one venue (tags contain the venue and 'import'); None when the database is not readable."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            n = con.execute("SELECT count(*) FROM journal WHERE tags LIKE ? AND tags LIKE '%\"import\"%'", (f'%"{venue}"%',)).fetchone()[0]
        finally:
            con.close()
        return int(n)
    except sqlite3.Error:
        return None


def hl_address() -> str | None:
    try:
        m = ADDRESS_RE.search(HL_ADDRESS_FILE.read_text())
    except OSError:
        return None
    return m.group(0) if m else None


def write_status(status: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False))
    tmp.replace(STATUS)


def read_status() -> dict:
    try:
        d = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _reason(proc: subprocess.CompletedProcess | None, timeout: bool = False) -> str:
    """A short, safe reason: the exit code, plus the exception class of the importer's last traceback line if any."""
    if timeout:
        return f"не вклалось у {TIMEOUT_S // 60} хв"
    last = ""
    if proc is not None:
        lines = [x for x in (proc.stderr or "").strip().splitlines() if x.strip()]
        m = re.match(r"([A-Za-z_][\w.]*(?:Error|Exception))\b", lines[-1].strip()) if lines else None
        last = f" ({m.group(1)})" if m else ""
    return f"код виходу {proc.returncode if proc else '?'}{last}"


def run_venue(venue: str, cmd: list[str] | None, db: str) -> dict:
    if cmd is None:
        return {"ok": False, "skipped": True, "added": 0, "error": "адресу гаманця не задано: надішли її Claude"}
    before = count_rows(db, venue)
    try:
        proc = subprocess.run(cmd, cwd=HERE.parent, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "added": 0, "error": _reason(None, timeout=True)}
    after = count_rows(db, venue)
    added = after - before if before is not None and after is not None else 0
    if proc.returncode != 0:
        return {"ok": False, "added": max(added, 0), "error": _reason(proc)}
    return {"ok": True, "added": max(added, 0), "error": ""}


def refresh(days: int, db: str) -> dict:
    status = {"state": "running", "started": time.time(), "finished": None, "days": days, "venues": {}}
    write_status(status)
    py = sys.executable
    addr = hl_address()
    cmds = {
        "bybit": [py, str(HERE / "bybit_import.py"), "--db", db, "import", "--days", str(days)],
        "hyperliquid": [py, str(HERE / "hyperliquid_import.py"), "--db", db, "import", addr] if addr else None,
    }
    for venue in VENUES:
        status["venues"][venue] = run_venue(venue, cmds[venue], db)
        write_status(status)
    status["state"] = "done" if all(v["ok"] or v.get("skipped") for v in status["venues"].values()) else "error"
    status["finished"] = time.time()
    write_status(status)
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30, help="Bybit: how many days back to read (default 30)")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--set-hl-address", metavar="0xADDRESS", help="save the Hyperliquid wallet address (outside git)")
    a = ap.parse_args(argv)
    if a.set_hl_address:
        if not ADDRESS_RE.fullmatch(a.set_hl_address.strip()):
            print("not a 0x… address of 40 hex digits", file=sys.stderr)
            return 2
        HL_ADDRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        HL_ADDRESS_FILE.write_text(a.set_hl_address.strip() + "\n")
        print(f"saved to {HL_ADDRESS_FILE}")
        return 0
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("already running", file=sys.stderr)
            return 1
        s = refresh(max(1, min(a.days, 730)), a.db)
    print(json.dumps(s["venues"], ensure_ascii=False))
    return 0 if s["state"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
