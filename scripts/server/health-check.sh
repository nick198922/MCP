#!/bin/bash
# From the Mac: read-only health of the server — memory, OOM kills today, the services' memory, and the collector's
# newest rows (supply snapshot, strong traders). Changes nothing.
SERVER="${TRADEDESK_SERVER:-169.58.169.184}"
ssh nick@"$SERVER" 'echo "--- memory"; free -m; echo "--- load"; uptime
  echo "--- OOM kills today"; sudo journalctl -k --since today --no-pager -o short-iso | grep -i -E "out of memory|oom-kill|killed process" | tail -5
  echo "--- services (current / peak memory)"
  for u in tradedesk-mcp tradedesk-agents-loop tradedesk-tgbridge; do
    printf "%s: " "$u"; systemctl show "$u" -p MemoryCurrent -p MemoryPeak -p ActiveState --value | tr "\n" " "; echo
  done'
ssh tradedesk@"$SERVER" 'python3 - <<"PY"
import sqlite3
db = "/var/lib/tradedesk-mcp/tradedesk.sqlite"
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    print("--- collector (schema", con.execute("PRAGMA user_version").fetchone()[0], ")")
    for sql in ("SELECT COUNT(*), MAX(day) FROM supply_snapshot",
                "SELECT venue, COUNT(*), MAX(day) FROM trader_snapshot GROUP BY venue",
                "SELECT venue, COUNT(DISTINCT account), COUNT(*), MAX(ts) FROM trader_positions GROUP BY venue"):
        try:
            print(sql.split(" FROM ")[1].split()[0], con.execute(sql).fetchall())
        except sqlite3.Error as e:
            print("n/a:", e)
except sqlite3.Error as e:
    print("db:", e)
PY'
