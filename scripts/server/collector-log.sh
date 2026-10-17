#!/bin/bash
# From the Mac: recent collector lines of the MCP service journal (optionally filtered: collector-log.sh liquidat).
# Reads only.
pat="${1:-collector}"
[[ "$pat" =~ ^[A-Za-z0-9_:.-]+$ ]] || { echo "bad pattern"; exit 1; }
ssh tradedesk@"${TRADEDESK_SERVER:-169.58.169.184}" "journalctl -u tradedesk-mcp --since '-30 min' --no-pager -o short-iso 2>/dev/null | grep -i -- '$pat' | tail -25
python3 -I -c \"
import sqlite3, time
con = sqlite3.connect('file:/var/lib/tradedesk-mcp/tradedesk.sqlite?mode=ro', uri=True)
for v, n, last in con.execute('SELECT venue, count(*), max(ts) FROM liquidations GROUP BY venue'):
    print('liquidations', v, n, 'rows, newest', round(time.time() - last), 's ago')
\""
